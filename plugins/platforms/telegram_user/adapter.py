"""Telegram user-account platform adapter for Hermes Agent.

This plugin uses Telethon / MTProto to connect as a regular Telegram user
account. It is intentionally separate from the built-in ``telegram`` Bot API
adapter and should normally be used with a dedicated Hermes-owned account.

Configuration in config.yaml::

    platforms:
      telegram_user:
        enabled: true
        extra:
          api_id: 123456
          api_hash: "..."
          session_path: "~/.hermes/secrets/telegram_user"
          allowed_chats: ["123456789", "@operator"]

Or via environment variables:

    TELEGRAM_USER_API_ID
    TELEGRAM_USER_API_HASH
    TELEGRAM_USER_SESSION_PATH
    TELEGRAM_USER_SESSION_STRING
    TELEGRAM_USER_ALLOWED_CHATS
    TELEGRAM_USER_HOME_CHANNEL
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shlex
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - PyYAML is normally present in Hermes.
    yaml = None  # type: ignore

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    SUPPORTED_DOCUMENT_TYPES,
    SUPPORTED_VIDEO_TYPES,
    get_audio_cache_dir,
    get_document_cache_dir,
    get_image_cache_dir,
    get_video_cache_dir,
    resolve_channel_prompt,
    utf16_len,
)
from hermes_constants import get_config_path, get_hermes_home
from utils import atomic_replace

logger = logging.getLogger("gateway.platforms.telegram_user")

# Register the dynamic enum member at import time for tests and config parsing.
Platform("telegram_user")

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_IMAGE_MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_IMAGE_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_AUDIO_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg",
    ".opus", ".wav", ".webm",
}
_AUDIO_MIME_TO_EXT = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mpga": ".mpga",
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/webm": ".webm",
    "audio/x-aac": ".aac",
    "audio/x-flac": ".flac",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
}
_AUDIO_EXT_TO_MIME = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".mp4": "audio/mp4",
    ".mpeg": "audio/mpeg",
    ".mpga": "audio/mpga",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}


TELETHON_AVAILABLE = False
TelegramClient: Any = None
events: Any = None
StringSession: Any = None
SessionPasswordNeededError: Any = None
RPCError: Any = None

SUBSCRIPTION_MODES = {"silent", "notify", "digest"}


def _load_telethon() -> bool:
    """Import Telethon lazily and bind module globals."""
    global TELETHON_AVAILABLE, TelegramClient, events, StringSession
    global SessionPasswordNeededError, RPCError
    if TELETHON_AVAILABLE:
        return True
    try:
        from telethon import TelegramClient as _TelegramClient
        from telethon import events as _events
        from telethon.errors import RPCError as _RPCError
        from telethon.errors import SessionPasswordNeededError as _SessionPasswordNeededError
        from telethon.sessions import StringSession as _StringSession
    except ImportError:
        TELETHON_AVAILABLE = False
        return False
    TelegramClient = _TelegramClient
    events = _events
    StringSession = _StringSession
    SessionPasswordNeededError = _SessionPasswordNeededError
    RPCError = _RPCError
    TELETHON_AVAILABLE = True
    return True


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        parsed = float(str(value).strip())
        if parsed < 0:
            return default
        return parsed
    except (TypeError, ValueError):
        return default


def _csv_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(v) for v in value]
    else:
        raw_items = str(value).split(",")
    return {
        _normalize_identifier(item)
        for item in raw_items
        if str(item).strip()
    }


def _normalize_identifier(value: Any) -> str:
    return str(value or "").strip().lower()


def _identifier_variants(value: Any) -> set[str]:
    raw = _normalize_identifier(value)
    if not raw:
        return set()
    variants = {raw}
    if raw.startswith("@"):
        variants.add(raw[1:])
    else:
        variants.add(f"@{raw}")
    return variants


def _normalize_ext(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if not raw.startswith("."):
        raw = f".{raw}"
    return raw


def _safe_ext(value: Any, fallback: str = "") -> str:
    ext = _normalize_ext(value) or _normalize_ext(fallback)
    if not ext or not re.fullmatch(r"\.[a-z0-9][a-z0-9]{0,15}", ext):
        return ""
    return ext


def _sanitize_cache_filename(value: Any, fallback: str = "telegram_user_attachment") -> str:
    name = Path(str(value or "")).name
    name = name.replace("\x00", "").strip()
    name = re.sub(r"[\x00-\x1f\x7f/\\:]", "_", name)
    return name if name and name not in {".", ".."} else fallback


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _looks_like_cached_image(path: Path) -> bool:
    try:
        header = path.read_bytes()[:12]
    except OSError:
        return False
    return (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or header.startswith(b"GIF87a")
        or header.startswith(b"GIF89a")
        or header.startswith(b"BM")
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
    )


def _document_attribute_filename(document: Any) -> str:
    for attr in getattr(document, "attributes", []) or []:
        filename = getattr(attr, "file_name", None)
        if filename:
            return str(filename)
    return ""


def _message_file_info(message: Any) -> tuple[str, str, str]:
    """Return ``(filename, ext, mime_type)`` for a Telethon message."""
    file_info = getattr(message, "file", None)
    document = getattr(message, "document", None)

    filename = str(getattr(file_info, "name", None) or "").strip()
    if not filename:
        filename = _document_attribute_filename(document)

    ext = _normalize_ext(getattr(file_info, "ext", None))
    if not ext and filename:
        ext = _normalize_ext(Path(filename).suffix)

    mime_type = str(
        getattr(file_info, "mime_type", None)
        or getattr(document, "mime_type", None)
        or getattr(message, "mime_type", None)
        or ""
    ).strip().lower()

    if not ext and mime_type:
        ext = _IMAGE_MIME_TO_EXT.get(mime_type, "")
        if not ext:
            ext = _AUDIO_MIME_TO_EXT.get(mime_type, "")
        if not ext:
            video_mime_to_ext = {v: k for k, v in SUPPORTED_VIDEO_TYPES.items()}
            ext = video_mime_to_ext.get(mime_type, "")
        if not ext:
            doc_mime_to_ext = {v: k for k, v in SUPPORTED_DOCUMENT_TYPES.items()}
            ext = doc_mime_to_ext.get(mime_type, "")

    if not mime_type and filename:
        mime_type = (mimetypes.guess_type(filename)[0] or "").lower()
    if not filename:
        filename = f"telegram_user_attachment{ext or ''}"

    return filename, ext, mime_type


def _media_cache_target(
    message_type: MessageType,
    filename: str,
    ext: str,
) -> Path:
    safe_ext = _safe_ext(ext)
    token = uuid.uuid4().hex[:12]

    if message_type == MessageType.PHOTO:
        cache_dir = get_image_cache_dir()
        target = cache_dir / f"img_{token}{safe_ext or '.jpg'}"
    elif message_type == MessageType.AUDIO:
        cache_dir = get_audio_cache_dir()
        safe_name = _sanitize_cache_filename(filename, "telegram_user_audio")
        if safe_ext:
            safe_stem = Path(safe_name).stem or "telegram_user_audio"
            safe_name = f"{safe_stem}{safe_ext}"
        elif not Path(safe_name).suffix:
            safe_name = f"{safe_name}.ogg"
        target = cache_dir / f"audio_{token}_{safe_name}"
    elif message_type == MessageType.VOICE:
        cache_dir = get_audio_cache_dir()
        target = cache_dir / f"audio_{token}{safe_ext or '.ogg'}"
    elif message_type == MessageType.VIDEO:
        cache_dir = get_video_cache_dir()
        target = cache_dir / f"video_{token}{safe_ext or '.mp4'}"
    else:
        cache_dir = get_document_cache_dir()
        safe_name = _sanitize_cache_filename(filename)
        if safe_ext and not Path(safe_name).suffix:
            safe_name = f"{safe_name}{safe_ext}"
        target = cache_dir / f"doc_{token}_{safe_name}"

    if not _path_is_within(target, cache_dir):
        raise ValueError(f"Unsafe Telegram User media cache path: {target}")
    return target


def _default_session_path() -> Path:
    return get_hermes_home() / "secrets" / "telegram_user"


def _resolve_session_path(value: Any = None) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return _default_session_path()
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return get_hermes_home() / "secrets" / path


def _session_file_candidates(path: Path) -> list[Path]:
    candidates = [path]
    if not str(path).endswith(".session"):
        candidates.append(Path(f"{path}.session"))
    return candidates


def _session_file_exists(path: Path) -> bool:
    return any(candidate.exists() for candidate in _session_file_candidates(path))


def _ensure_private_parent(path: Path) -> None:
    parent = path.expanduser().parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        parent.chmod(0o700)
    except OSError:
        pass


def _safe_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _subscription_store_path() -> Path:
    return get_hermes_home() / "platforms" / "telegram_user" / "subscriptions.json"


def _empty_subscription_store() -> dict[str, Any]:
    return {"version": 1, "items": {}}


def _load_subscription_store() -> dict[str, Any]:
    path = _subscription_store_path()
    if not path.exists():
        return _empty_subscription_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Telegram User: could not read subscription store %s", path)
        return _empty_subscription_store()
    if not isinstance(data, dict):
        return _empty_subscription_store()
    items = data.get("items")
    if not isinstance(items, dict):
        data["items"] = {}
    data.setdefault("version", 1)
    return data


def _save_subscription_store(data: dict[str, Any]) -> None:
    path = _subscription_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    atomic_replace(tmp, path)


def _normalize_subscription_mode(value: Any, default: str = "silent") -> str:
    mode = str(value or "").strip().lower() or default
    if mode not in SUBSCRIPTION_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(SUBSCRIPTION_MODES))}")
    return mode


def _normalize_public_target(target: str) -> str:
    raw = str(target or "").strip()
    match = re.match(r"^https?://t\.me/([A-Za-z0-9_]{5,})/?$", raw)
    if match:
        return f"@{match.group(1)}"
    return raw


def _extract_invite_hash(target: str) -> Optional[str]:
    raw = str(target or "").strip()
    patterns = (
        r"^https?://t\.me/\+([A-Za-z0-9_-]+)$",
        r"^https?://t\.me/joinchat/([A-Za-z0-9_-]+)$",
        r"^t\.me/\+([A-Za-z0-9_-]+)$",
        r"^t\.me/joinchat/([A-Za-z0-9_-]+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, raw)
        if match:
            return match.group(1)
    if raw.startswith("+") and len(raw) > 1:
        return raw[1:]
    return None


def _entity_username(entity: Any) -> str:
    username = str(getattr(entity, "username", "") or "").strip()
    return f"@{username.lower()}" if username else ""


def _entity_title(entity: Any) -> str:
    return _display_name(entity) or _entity_username(entity) or str(getattr(entity, "id", "") or "")


def _subscription_key_for_entity(entity: Any, fallback: str) -> str:
    username = _entity_username(entity)
    if username:
        return username
    entity_id = str(getattr(entity, "id", "") or "").strip()
    if entity_id:
        return entity_id
    return _normalize_identifier(fallback)


def _subscription_item_variants(item: dict[str, Any]) -> set[str]:
    variants: set[str] = set()
    for key in ("key", "target", "peer_id", "username", "title"):
        variants.update(_identifier_variants(item.get(key)))
    return variants


def _find_subscription_item(target: str) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    lookup = _identifier_variants(target)
    data = _load_subscription_store()
    for key, item in data.get("items", {}).items():
        if not isinstance(item, dict):
            continue
        if lookup & (_identifier_variants(key) | _subscription_item_variants(item)):
            return key, item
    return None, None


def _format_subscription_item(item: dict[str, Any]) -> str:
    label = item.get("username") or item.get("title") or item.get("target") or item.get("key")
    mode = item.get("mode", "silent")
    kind = item.get("kind", "chat")
    pending = int(item.get("pending_count") or 0)
    suffix = f", pending={pending}" if pending else ""
    return f"- {label} ({kind}, {mode}{suffix})"


def _load_config_platform_block() -> dict[str, Any]:
    """Read a lightweight telegram_user platform block from config.yaml."""
    if yaml is None:
        return {}
    try:
        path = get_config_path()
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    merged: dict[str, Any] = {}
    candidates = []
    gateway = data.get("gateway")
    if isinstance(gateway, dict):
        platforms = gateway.get("platforms")
        if isinstance(platforms, dict):
            candidates.append(platforms.get("telegram_user"))
    platforms = data.get("platforms")
    if isinstance(platforms, dict):
        candidates.append(platforms.get("telegram_user"))
    candidates.append(data.get("telegram_user"))

    for block in candidates:
        if isinstance(block, dict):
            extra = {**merged.get("extra", {}), **(block.get("extra") or {})}
            merged.update(block)
            if extra:
                merged["extra"] = extra
    return merged


def _config_value_from_block(block: dict[str, Any], key: str) -> Any:
    extra = block.get("extra") if isinstance(block.get("extra"), dict) else {}
    if key in extra:
        return extra[key]
    return block.get(key)


def _block_minimally_configured(block: dict[str, Any]) -> bool:
    if not block:
        return False
    if "enabled" in block and not _coerce_bool(block.get("enabled"), False):
        return False
    api_id = _coerce_int(_config_value_from_block(block, "api_id"), 0)
    api_hash = str(_config_value_from_block(block, "api_hash") or "").strip()
    if not (api_id and api_hash):
        return False
    session_string = str(_config_value_from_block(block, "session_string") or "").strip()
    if session_string:
        return True
    session_path = _resolve_session_path(_config_value_from_block(block, "session_path"))
    return _session_file_exists(session_path)


def _env_minimally_configured() -> bool:
    api_id = _coerce_int(os.getenv("TELEGRAM_USER_API_ID"), 0)
    api_hash = os.getenv("TELEGRAM_USER_API_HASH", "").strip()
    if not (api_id and api_hash):
        return False
    if os.getenv("TELEGRAM_USER_SESSION_STRING", "").strip():
        return True
    return _session_file_exists(
        _resolve_session_path(os.getenv("TELEGRAM_USER_SESSION_PATH"))
    )


def _runtime_minimally_configured() -> bool:
    return _env_minimally_configured() or _block_minimally_configured(_load_config_platform_block())


def _entity_arg(raw: Any) -> Any:
    value = str(raw or "").strip()
    if not value:
        return value
    try:
        return int(value)
    except ValueError:
        return value


def _display_name(entity: Any) -> Optional[str]:
    for attr in ("title", "first_name", "name"):
        value = getattr(entity, attr, None)
        if value:
            last_name = getattr(entity, "last_name", None)
            if attr == "first_name" and last_name:
                return f"{value} {last_name}"
            return str(value)
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return None


def _telethon_error_is_flood_wait(exc: BaseException) -> Optional[float]:
    name = exc.__class__.__name__.lower()
    seconds = getattr(exc, "seconds", None)
    if seconds is not None and "flood" in name:
        try:
            return float(seconds)
        except (TypeError, ValueError):
            return None
    return None


def _telethon_error_is_parse_failure(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "parse" in text or "entity" in text or "markdown" in text


class TelegramUserAdapter(BasePlatformAdapter):
    """Telethon-backed Telegram user account adapter."""

    MAX_MESSAGE_LENGTH = 4096

    @property
    def message_len_fn(self):
        return utf16_len

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("telegram_user"))
        extra = getattr(config, "extra", {}) or {}

        self.api_id = _coerce_int(os.getenv("TELEGRAM_USER_API_ID") or extra.get("api_id"), 0)
        self.api_hash = str(os.getenv("TELEGRAM_USER_API_HASH") or extra.get("api_hash") or "").strip()
        self.session_string = str(
            os.getenv("TELEGRAM_USER_SESSION_STRING") or extra.get("session_string") or ""
        ).strip()
        self.session_path = _resolve_session_path(
            os.getenv("TELEGRAM_USER_SESSION_PATH") or extra.get("session_path")
        )

        self.allowed_chats = _csv_set(
            os.getenv("TELEGRAM_USER_ALLOWED_CHATS") or extra.get("allowed_chats")
        )
        self.allowed_users = _csv_set(
            os.getenv("TELEGRAM_USER_ALLOWED_USERS") or extra.get("allowed_users")
        )
        self.allow_all_chats = _coerce_bool(
            os.getenv("TELEGRAM_USER_ALLOW_ALL_CHATS"),
            _coerce_bool(extra.get("allow_all_chats"), False),
        )
        self.require_mention = _coerce_bool(
            os.getenv("TELEGRAM_USER_REQUIRE_MENTION"),
            _coerce_bool(extra.get("require_mention"), True),
        )
        self.ignore_self_messages = _coerce_bool(
            os.getenv("TELEGRAM_USER_IGNORE_SELF_MESSAGES"),
            _coerce_bool(extra.get("ignore_self_messages"), True),
        )
        self.allow_outgoing = _coerce_bool(
            os.getenv("TELEGRAM_USER_ALLOW_OUTGOING"),
            _coerce_bool(extra.get("allow_outgoing"), False),
        )
        self.disable_link_previews = _coerce_bool(
            os.getenv("TELEGRAM_USER_DISABLE_LINK_PREVIEWS"),
            _coerce_bool(extra.get("disable_link_previews"), True),
        )
        self.send_interval_seconds = _coerce_float(
            os.getenv("TELEGRAM_USER_SEND_INTERVAL_SECONDS") or extra.get("send_interval_seconds"),
            1.0,
        )
        self.flood_wait_threshold_seconds = _coerce_float(
            os.getenv("TELEGRAM_USER_FLOOD_WAIT_THRESHOLD_SECONDS")
            or extra.get("flood_wait_threshold_seconds"),
            60.0,
        )
        self.health_check_seconds = _coerce_float(
            os.getenv("TELEGRAM_USER_HEALTH_CHECK_SECONDS")
            or extra.get("health_check_seconds"),
            60.0,
        )
        self.health_timeout_seconds = _coerce_float(
            os.getenv("TELEGRAM_USER_HEALTH_TIMEOUT_SECONDS")
            or extra.get("health_timeout_seconds"),
            15.0,
        )
        self.media_batch_delay_seconds = _coerce_float(
            os.getenv("TELEGRAM_USER_MEDIA_BATCH_DELAY_SECONDS")
            or extra.get("media_batch_delay_seconds"),
            2.0,
        )

        self._client: Any = None
        self._event_builder: Any = None
        self._health_task: Optional[asyncio.Task] = None
        self._audio_media_batches: Dict[str, Dict[str, Any]] = {}
        self._audio_media_flush_tasks: Dict[str, asyncio.Task] = {}
        self._disconnecting = False
        self._me: Any = None
        self._account_user_id: str = ""
        self._account_username: str = ""
        self._account_display_names: set[str] = set()
        self._send_lock = asyncio.Lock()
        self._last_send_at = 0.0

    @property
    def name(self) -> str:
        return "Telegram User"

    def _session_arg(self) -> Any:
        if self.session_string:
            return StringSession(self.session_string)
        _ensure_private_parent(self.session_path)
        return str(self.session_path)

    def _lock_identity(self) -> str:
        if self.session_string:
            return f"string:{_safe_hash(self.session_string)}"
        return str(self.session_path.expanduser())

    async def connect(self) -> bool:
        """Connect to Telegram as a user account and start receiving updates."""
        if not _load_telethon():
            logger.error("Telegram User: telethon not installed")
            return False
        if not self.api_id or not self.api_hash:
            logger.error("Telegram User: TELEGRAM_USER_API_ID and TELEGRAM_USER_API_HASH are required")
            self._set_fatal_error("config_missing", "TELEGRAM_USER_API_ID and TELEGRAM_USER_API_HASH are required", retryable=False)
            return False
        if not self.session_string and not _session_file_exists(self.session_path):
            message = (
                "Telethon session is missing. Run telegram_user setup/login first "
                "or set TELEGRAM_USER_SESSION_STRING."
            )
            logger.error("Telegram User: %s", message)
            self._set_fatal_error("session_missing", message, retryable=False)
            return False
        if not self._acquire_platform_lock(
            "telegram-user-session",
            self._lock_identity(),
            "Telegram user session",
        ):
            return False

        try:
            self._client = TelegramClient(
                self._session_arg(),
                self.api_id,
                self.api_hash,
                sequential_updates=True,
                flood_sleep_threshold=int(self.flood_wait_threshold_seconds),
            )
            await self._client.connect()
            if not await self._client.is_user_authorized():
                message = "Telethon session is not authorized. Run telegram_user setup/login again."
                self._set_fatal_error("session_unauthorized", message, retryable=False)
                logger.error("Telegram User: %s", message)
                await self._client.disconnect()
                self._client = None
                self._release_platform_lock()
                return False

            self._me = await self._client.get_me()
            self._account_user_id = str(getattr(self._me, "id", "") or "")
            self._account_username = str(getattr(self._me, "username", "") or "").strip().lower()
            self._account_display_names = {
                _normalize_identifier(v)
                for v in (
                    getattr(self._me, "first_name", None),
                    getattr(self._me, "username", None),
                    _display_name(self._me),
                )
                if v
            }

            if self.allow_outgoing:
                self._event_builder = events.NewMessage()
            else:
                self._event_builder = events.NewMessage(incoming=True)
            self._client.add_event_handler(self._handle_new_message, self._event_builder)
            self._mark_connected()
            self._disconnecting = False
            self._start_health_monitor()
            logger.info(
                "Telegram User: connected as user_id=%s username=%s",
                self._account_user_id or "<unknown>",
                f"@{self._account_username}" if self._account_username else "<none>",
            )
            if not self.allow_all_chats and not self.allowed_chats:
                logger.warning(
                    "Telegram User: no allowed chats configured; inbound messages will be ignored"
                )
            return True
        except Exception as exc:
            self._release_platform_lock()
            self._set_fatal_error("connect_error", f"Telegram user startup failed: {exc}", retryable=True)
            logger.error("Telegram User: failed to connect: %s", exc, exc_info=True)
            try:
                if self._client is not None:
                    await self._client.disconnect()
            except Exception:
                pass
            self._client = None
            return False

    async def disconnect(self) -> None:
        """Disconnect the Telethon client and release the session lock."""
        self._disconnecting = True
        await self._cancel_audio_media_batches()
        await self._stop_health_monitor()
        client = self._client
        if client is not None:
            try:
                if self._event_builder is not None:
                    client.remove_event_handler(self._handle_new_message, self._event_builder)
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception as exc:
                logger.warning("Telegram User: disconnect error: %s", exc, exc_info=True)
        self._release_platform_lock()
        self._client = None
        self._event_builder = None
        self._mark_disconnected()

    def _start_health_monitor(self) -> None:
        if self.health_check_seconds <= 0:
            return
        task = self._health_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._health_task = loop.create_task(self._health_monitor())

    async def _stop_health_monitor(self) -> None:
        task = self._health_task
        self._health_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Telegram User: health monitor shutdown failed", exc_info=True)

    async def _health_monitor(self) -> None:
        """Keep the Telethon receiver honest and reconnect after silent stalls."""
        while not self._disconnecting:
            await asyncio.sleep(self.health_check_seconds)
            if self._disconnecting or self._client is None:
                return
            try:
                await self._probe_client_health()
                if not self.is_connected:
                    self._mark_connected()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._recover_client_connection("health_check_failed", exc)

    async def _probe_client_health(self) -> None:
        client = self._client
        if client is None:
            raise RuntimeError("client is not initialized")
        if not client.is_connected():
            raise RuntimeError("client is disconnected")
        from telethon.functions import PingRequest  # type: ignore

        ping_id = int(time.time() * 1000) & 0x7FFFFFFFFFFFFFFF
        await asyncio.wait_for(
            client(PingRequest(ping_id=ping_id)),
            timeout=max(self.health_timeout_seconds, 1.0),
        )

    async def _recover_client_connection(self, reason: str, exc: BaseException) -> None:
        client = self._client
        if client is None or self._disconnecting:
            return
        logger.warning(
            "Telegram User: %s; reconnecting Telethon client: %s",
            reason,
            exc,
        )
        self._write_runtime_status_safe(
            "health_failed",
            platform_state="disconnected",
            error_code=reason,
            error_message=str(exc),
        )
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except Exception:
            logger.debug("Telegram User: disconnect during reconnect failed", exc_info=True)
        if self._disconnecting:
            return
        await asyncio.sleep(2)
        try:
            await asyncio.wait_for(client.connect(), timeout=max(self.health_timeout_seconds, 5.0))
            if not await client.is_user_authorized():
                message = "Telethon session is not authorized. Run telegram_user setup/login again."
                self._set_fatal_error("session_unauthorized", message, retryable=False)
                logger.error("Telegram User: %s", message)
                return
            self._mark_connected()
            logger.info("Telegram User: reconnected after %s", reason)
        except Exception as reconnect_exc:
            self._write_runtime_status_safe(
                "health_reconnect_failed",
                platform_state="disconnected",
                error_code="reconnect_failed",
                error_message=str(reconnect_exc),
            )
            logger.warning(
                "Telegram User: reconnect failed after %s: %s",
                reason,
                reconnect_exc,
                exc_info=True,
            )

    async def _rate_limit_send(self) -> None:
        if self.send_interval_seconds <= 0:
            return
        async with self._send_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self.send_interval_seconds - (now - self._last_send_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send_at = loop.time()

    async def _call_with_flood_wait(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        attempts = 2
        for attempt in range(attempts):
            try:
                await self._rate_limit_send()
                return await fn(*args, **kwargs)
            except Exception as exc:
                wait = _telethon_error_is_flood_wait(exc)
                if wait is None or wait > self.flood_wait_threshold_seconds or attempt >= attempts - 1:
                    raise
                logger.warning("Telegram User: flood wait %.1fs; retrying once", wait)
                await asyncio.sleep(wait)
        raise RuntimeError("unreachable")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message via Telethon."""
        del metadata
        if not self._client:
            return SendResult(success=False, error="Not connected")
        if not content or not content.strip():
            return SendResult(success=True, message_id=None)

        try:
            entity = _entity_arg(chat_id)
            chunks = self.truncate_message(content, self.MAX_MESSAGE_LENGTH, len_fn=utf16_len)
            message_ids: list[str] = []
            reply_to_id = _coerce_int(reply_to, 0) or None
            for index, chunk in enumerate(chunks):
                effective_reply_to = reply_to_id if index == 0 else None
                try:
                    msg = await self._call_with_flood_wait(
                        self._client.send_message,
                        entity,
                        chunk,
                        reply_to=effective_reply_to,
                        parse_mode="md",
                        link_preview=not self.disable_link_previews,
                    )
                except Exception as parse_exc:
                    if not _telethon_error_is_parse_failure(parse_exc):
                        raise
                    msg = await self._call_with_flood_wait(
                        self._client.send_message,
                        entity,
                        chunk,
                        reply_to=effective_reply_to,
                        parse_mode=None,
                        link_preview=not self.disable_link_previews,
                    )
                message_ids.append(str(getattr(msg, "id", "") or getattr(msg, "message_id", "")))
            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
                raw_response={"message_ids": message_ids},
            )
        except Exception as exc:
            logger.error("Telegram User: send failed: %s", exc, exc_info=True)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        del finalize
        if not self._client:
            return SendResult(success=False, error="Not connected")
        if not content or not content.strip():
            return SendResult(success=True, message_id=message_id)
        try:
            entity = _entity_arg(chat_id)
            text = self.truncate_message(content, self.MAX_MESSAGE_LENGTH, len_fn=utf16_len)[0]
            try:
                msg = await self._call_with_flood_wait(
                    self._client.edit_message,
                    entity,
                    int(message_id),
                    text,
                    parse_mode="md",
                    link_preview=not self.disable_link_previews,
                )
            except Exception as parse_exc:
                if not _telethon_error_is_parse_failure(parse_exc):
                    raise
                msg = await self._call_with_flood_wait(
                    self._client.edit_message,
                    entity,
                    int(message_id),
                    text,
                    parse_mode=None,
                    link_preview=not self.disable_link_previews,
                )
            return SendResult(success=True, message_id=str(getattr(msg, "id", message_id)))
        except Exception as exc:
            logger.debug("Telegram User: edit failed: %s", exc, exc_info=True)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        if not self._client:
            return False
        try:
            await self._client.delete_messages(_entity_arg(chat_id), [int(message_id)])
            return True
        except Exception:
            return False

    async def _send_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        *,
        force_document: bool = False,
        voice_note: bool = False,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Not connected")
        path = str(file_path or "").strip()
        if path.startswith("file://"):
            path = path[7:]
        if not path or not Path(path).exists():
            return SendResult(success=False, error=f"File not found: {file_path}")
        try:
            msg = await self._call_with_flood_wait(
                self._client.send_file,
                _entity_arg(chat_id),
                path,
                caption=caption or None,
                reply_to=_coerce_int(reply_to, 0) or None,
                force_document=force_document,
                voice_note=voice_note,
                parse_mode="md",
            )
            if isinstance(msg, list):
                msg = msg[-1] if msg else None
            return SendResult(success=True, message_id=str(getattr(msg, "id", "") or ""))
        except Exception as exc:
            logger.error("Telegram User: send file failed: %s", exc, exc_info=True)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del metadata
        return await self._send_file(chat_id, file_path, caption, reply_to, force_document=True)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del metadata
        return await self._send_file(chat_id, image_path, caption, reply_to, force_document=False)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del metadata
        if image_url.startswith("file://") or Path(image_url).exists():
            return await self._send_file(chat_id, image_url, caption, reply_to, force_document=False)
        if not self._client:
            return SendResult(success=False, error="Not connected")
        # Telethon can send HTTP URLs directly by passing them as file values.
        try:
            msg = await self._call_with_flood_wait(
                self._client.send_file,
                _entity_arg(chat_id),
                image_url,
                caption=caption or None,
                reply_to=_coerce_int(reply_to, 0) or None,
                force_document=False,
                parse_mode="md",
            )
            return SendResult(success=True, message_id=str(getattr(msg, "id", "") or ""))
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del metadata
        return await self._send_file(chat_id, video_path, caption, reply_to, force_document=False)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del metadata
        return await self._send_file(chat_id, audio_path, caption, reply_to, voice_note=True)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        del metadata
        if not self._client:
            return
        try:
            async with self._client.action(_entity_arg(chat_id), "typing"):
                await asyncio.sleep(0.1)
        except Exception:
            logger.debug("Telegram User: typing action failed", exc_info=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        if not self._client:
            return {"name": str(chat_id), "type": "dm", "chat_id": str(chat_id)}
        try:
            entity = await self._client.get_entity(_entity_arg(chat_id))
            return {
                "name": _display_name(entity) or str(chat_id),
                "type": self._chat_type_from_entity(entity),
                "chat_id": str(chat_id),
            }
        except Exception:
            return {"name": str(chat_id), "type": "dm", "chat_id": str(chat_id)}

    def _subscription_notice_target(self) -> str:
        home = os.getenv("TELEGRAM_USER_HOME_CHANNEL", "").strip()
        if home:
            return home
        extra_home = (getattr(self.config, "extra", {}) or {}).get("home_channel")
        if isinstance(extra_home, dict):
            return str(extra_home.get("chat_id") or "").strip()
        return str(extra_home or "").strip()

    def _subscription_for_event(
        self,
        event: Any,
        chat: Any,
        sender: Any,
    ) -> tuple[str, dict[str, Any]] | tuple[None, None]:
        candidates = self._identity_candidates(event, chat, sender)
        data = _load_subscription_store()
        for key, item in data.get("items", {}).items():
            if not isinstance(item, dict):
                continue
            if candidates & (_identifier_variants(key) | _subscription_item_variants(item)):
                return key, item
        return None, None

    def _save_subscription_item(self, key: str, item: dict[str, Any]) -> None:
        data = _load_subscription_store()
        data.setdefault("items", {})[key] = item
        _save_subscription_store(data)

    def _subscription_record(
        self,
        *,
        entity: Any,
        target: str,
        kind: str,
        mode: str,
        payload: str = "",
    ) -> dict[str, Any]:
        key = _subscription_key_for_entity(entity, target)
        now = datetime.now().isoformat(timespec="seconds")
        existing_key, existing = _find_subscription_item(key)
        item = dict(existing or {})
        item.update(
            {
                "key": existing_key or key,
                "target": target,
                "peer_id": str(getattr(entity, "id", "") or ""),
                "username": _entity_username(entity),
                "title": _entity_title(entity),
                "kind": kind,
                "mode": _normalize_subscription_mode(mode),
                "payload": payload,
                "updated_at": now,
            }
        )
        item.setdefault("created_at", now)
        item.setdefault("pending_count", 0)
        self._save_subscription_item(str(item["key"]), item)
        return item

    async def subscribe_public_channel(self, target: str, mode: str = "silent") -> dict[str, Any]:
        if not self._client:
            return {"error": "Telegram User adapter is not connected"}
        if not target.strip():
            return {"error": "Usage: /tg-sub add <@channel-or-t.me-link> [--mode silent|notify|digest]"}
        if _extract_invite_hash(target):
            return {"error": "Invite links must use: /tg-sub join <invite-link>"}
        if not _load_telethon():
            return {"error": "Telethon is not installed"}
        from telethon.tl.functions.channels import JoinChannelRequest

        normalized = _normalize_public_target(target)
        try:
            entity = await self._client.get_entity(normalized)
            try:
                await self._client(JoinChannelRequest(entity))
            except Exception as exc:
                if "already" not in str(exc).lower():
                    raise
            entity = await self._client.get_entity(normalized)
            item = self._subscription_record(
                entity=entity,
                target=normalized,
                kind=self._chat_type_from_entity(entity),
                mode=mode,
            )
            return {"success": True, "item": item}
        except Exception as exc:
            return {"error": f"Telegram join failed: {exc}"}

    async def join_private_invite(self, invite_link: str, mode: str = "silent") -> dict[str, Any]:
        if not self._client:
            return {"error": "Telegram User adapter is not connected"}
        invite_hash = _extract_invite_hash(invite_link)
        if not invite_hash:
            return {"error": "Usage: /tg-sub join <https://t.me/+invitehash> [--mode silent|notify|digest]"}
        if not _load_telethon():
            return {"error": "Telethon is not installed"}
        from telethon.tl.functions.messages import ImportChatInviteRequest

        try:
            result = await self._client(ImportChatInviteRequest(invite_hash))
            chats = list(getattr(result, "chats", None) or [])
            entity = chats[0] if chats else None
            if entity is None:
                return {"error": "Telegram accepted the invite but did not return a chat entity"}
            item = self._subscription_record(
                entity=entity,
                target=invite_link,
                kind=self._chat_type_from_entity(entity),
                mode=mode,
            )
            return {"success": True, "item": item}
        except Exception as exc:
            if "already" in str(exc).lower():
                return {"error": "Already joined; use /tg-sub add @username if the chat has a public username."}
            return {"error": f"Telegram invite join failed: {exc}"}

    async def start_bot_subscription(
        self,
        bot_target: str,
        payload: str = "",
        mode: str = "notify",
    ) -> dict[str, Any]:
        if not self._client:
            return {"error": "Telegram User adapter is not connected"}
        if not bot_target.strip():
            return {"error": "Usage: /tg-sub bot <@bot> [payload] [--mode silent|notify|digest]"}
        if not _load_telethon():
            return {"error": "Telethon is not installed"}
        from telethon.tl.functions.messages import StartBotRequest

        normalized = _normalize_public_target(bot_target)
        try:
            bot = await self._client.get_entity(normalized)
            await self._client(StartBotRequest(bot=bot, peer=bot, start_param=payload or ""))
            item = self._subscription_record(
                entity=bot,
                target=normalized,
                kind="bot",
                mode=mode,
                payload=payload,
            )
            return {"success": True, "item": item}
        except Exception as exc:
            return {"error": f"Telegram bot start failed: {exc}"}

    async def unsubscribe_target(self, target: str, *, leave: bool = False) -> dict[str, Any]:
        key, item = _find_subscription_item(target)
        if not key or not item:
            return {"error": f"No Telegram User subscription found for {target}"}
        if leave:
            if not self._client:
                return {"error": "Telegram User adapter is not connected"}
            try:
                from telethon.tl.functions.channels import LeaveChannelRequest

                entity_ref = item.get("username") or item.get("peer_id") or item.get("target") or target
                entity = await self._client.get_entity(entity_ref)
                if item.get("kind") == "bot":
                    await self.send(str(entity_ref), "/stop")
                else:
                    await self._client(LeaveChannelRequest(entity))
            except Exception as exc:
                return {"error": f"Telegram leave failed: {exc}"}
        data = _load_subscription_store()
        data.get("items", {}).pop(str(key), None)
        _save_subscription_store(data)
        return {"success": True, "item": item}

    async def set_subscription_mode(self, target: str, mode: str) -> dict[str, Any]:
        key, item = _find_subscription_item(target)
        if not key or not item:
            return {"error": f"No Telegram User subscription found for {target}"}
        try:
            item["mode"] = _normalize_subscription_mode(mode)
        except ValueError as exc:
            return {"error": str(exc)}
        item["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_subscription_item(str(key), item)
        return {"success": True, "item": item}

    async def send_subscription_message(self, target: str, text: str) -> dict[str, Any]:
        key, item = _find_subscription_item(target)
        if not key or not item:
            return {"error": f"No Telegram User subscription found for {target}"}
        entity_ref = item.get("username") or item.get("peer_id") or item.get("target") or target
        result = await self.send(str(entity_ref), text)
        if result.success:
            return {"success": True, "item": item, "message_id": result.message_id}
        return {"error": result.error or "send failed"}

    async def _handle_subscription_message(
        self,
        key: str,
        item: dict[str, Any],
        event: Any,
        chat: Any,
        sender: Any,
        text: str,
    ) -> None:
        mode = str(item.get("mode") or "silent").lower()
        if mode not in SUBSCRIPTION_MODES:
            mode = "silent"
        message = getattr(event, "message", event)
        item["last_message_id"] = str(getattr(message, "id", "") or "")
        item["last_seen_at"] = datetime.now().isoformat(timespec="seconds")
        item["last_text"] = text[:2000]
        if mode == "digest":
            item["pending_count"] = int(item.get("pending_count") or 0) + 1
        self._save_subscription_item(key, item)

        if mode != "notify":
            return
        home = self._subscription_notice_target()
        if not home:
            logger.info("Telegram User: subscription notification skipped; no home channel configured")
            return
        title = item.get("title") or _display_name(chat) or item.get("target") or key
        sender_label = _display_name(sender)
        prefix = f"Telegram subscription update from {title}"
        if sender_label and sender_label != title:
            prefix += f" ({sender_label})"
        snippet = text.strip()
        if len(snippet) > 1200:
            snippet = snippet[:1197].rstrip() + "..."
        await self.send(home, f"{prefix}:\n\n{snippet}")

    @staticmethod
    def _chat_type_from_event(event: Any, chat: Any = None) -> str:
        if getattr(event, "is_private", False):
            return "dm"
        if getattr(event, "is_group", False):
            return "group"
        if getattr(event, "is_channel", False):
            return "channel"
        if getattr(chat, "broadcast", False):
            return "channel"
        if getattr(chat, "megagroup", False):
            return "group"
        return "dm"

    @staticmethod
    def _chat_type_from_entity(entity: Any) -> str:
        if getattr(entity, "bot", False) or entity.__class__.__name__.lower() == "user":
            return "dm"
        if getattr(entity, "broadcast", False):
            return "channel"
        return "group"

    def _identity_candidates(self, event: Any, chat: Any, sender: Any) -> set[str]:
        values: list[Any] = [
            getattr(event, "chat_id", None),
            getattr(event, "sender_id", None),
            getattr(chat, "id", None),
            getattr(sender, "id", None),
            getattr(chat, "username", None),
            getattr(sender, "username", None),
            getattr(chat, "phone", None),
            getattr(sender, "phone", None),
            _display_name(chat),
            _display_name(sender),
        ]
        candidates: set[str] = set()
        for value in values:
            candidates.update(_identifier_variants(value))
        return candidates

    def _event_allowed(self, event: Any, chat: Any, sender: Any) -> bool:
        if self.allow_all_chats:
            return True
        candidates = self._identity_candidates(event, chat, sender)
        if candidates & self.allowed_chats:
            return True
        return False

    def _extract_group_trigger(self, text: str) -> tuple[bool, str]:
        raw = text or ""
        stripped = raw.lstrip()
        lowered = stripped.lower()
        trigger_tokens: set[str] = set()
        if self._account_username:
            trigger_tokens.add(f"@{self._account_username}")
            trigger_tokens.add(self._account_username)
        trigger_tokens.update(v for v in self._account_display_names if v)

        for token in sorted(trigger_tokens, key=len, reverse=True):
            if not token:
                continue
            if lowered == token.lower():
                return True, ""
            prefixes = [f"{token}:", f"{token},", f"{token} "]
            for prefix in prefixes:
                if lowered.startswith(prefix.lower()):
                    return True, stripped[len(prefix):].strip()
        if self._account_username and f"@{self._account_username}" in lowered:
            return True, raw
        return False, raw

    async def _is_reply_to_self(self, event: Any) -> bool:
        if not self._account_user_id or not getattr(event, "is_reply", False):
            return False
        try:
            replied = await event.get_reply_message()
        except Exception:
            return False
        return str(getattr(replied, "sender_id", "") or "") == self._account_user_id

    @staticmethod
    def _message_has_downloadable_media(message: Any) -> bool:
        if getattr(message, "media", None) is not None:
            return True
        for attr in ("photo", "document", "voice", "audio", "video", "file"):
            if getattr(message, attr, None) is not None:
                return True
        return False

    async def _download_message_media_to_path(
        self,
        message: Any,
        target_path: Path,
    ) -> Optional[str]:
        """Download Telethon media directly into a preselected cache path."""
        download_media = getattr(message, "download_media", None)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not _path_is_within(target_path, target_path.parent):
            raise ValueError(f"Unsafe Telegram User media target path: {target_path}")

        tmp_path = target_path.with_name(
            f".{target_path.name}.{uuid.uuid4().hex[:8]}.part"
        )
        result = None
        downloaded_path: Optional[Path] = None
        try:
            if callable(download_media):
                result = await download_media(file=str(tmp_path))
            elif self._client is not None:
                client_download = getattr(self._client, "download_media", None)
                if callable(client_download):
                    result = await client_download(message, file=str(tmp_path))

            if isinstance(result, bytes):
                tmp_path.write_bytes(result)
                downloaded_path = tmp_path
            elif isinstance(result, bytearray):
                tmp_path.write_bytes(bytes(result))
                downloaded_path = tmp_path
            elif isinstance(result, str):
                downloaded_path = Path(result).expanduser()
            elif tmp_path.exists():
                downloaded_path = tmp_path

            if downloaded_path is None and tmp_path.exists():
                downloaded_path = tmp_path
            if downloaded_path is None or not downloaded_path.exists():
                return None
            if not downloaded_path.is_file() or downloaded_path.stat().st_size <= 0:
                return None
            if not _path_is_within(downloaded_path, target_path.parent):
                raise ValueError(
                    f"Telegram User media download escaped cache dir: {downloaded_path}"
                )

            atomic_replace(downloaded_path, target_path)
            return str(target_path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    async def _cache_inbound_media(
        self,
        message: Any,
    ) -> tuple[List[str], List[str], Optional[MessageType]]:
        """Download one Telethon message attachment into Hermes' media cache."""
        if not self._message_has_downloadable_media(message):
            return [], [], None

        filename, ext, mime_type = _message_file_info(message)
        is_photo = bool(getattr(message, "photo", None))
        is_video = bool(getattr(message, "video", None))
        is_voice = bool(getattr(message, "voice", None))
        is_audio = bool(getattr(message, "audio", None))

        if is_photo or mime_type.startswith("image/") or ext in _IMAGE_EXTENSIONS:
            image_ext = (
                ext if ext in _IMAGE_EXTENSIONS
                else _IMAGE_MIME_TO_EXT.get(mime_type, ".jpg")
            )
            try:
                target_path = _media_cache_target(MessageType.PHOTO, filename, image_ext)
                cached_path = await self._download_message_media_to_path(message, target_path)
            except Exception as exc:
                logger.warning(
                    "Telegram User: failed to download image-like media: %s",
                    exc,
                    exc_info=True,
                )
                return [], [], None
            if cached_path and _looks_like_cached_image(Path(cached_path)):
                return (
                    [cached_path],
                    [_IMAGE_EXT_TO_MIME.get(image_ext, mime_type or "image/jpeg")],
                    MessageType.PHOTO,
                )
            if cached_path:
                fallback_path = _media_cache_target(MessageType.DOCUMENT, filename, ext)
                atomic_replace(cached_path, fallback_path)
                media_type = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
                return [str(fallback_path)], [media_type], MessageType.DOCUMENT
            logger.info("Telegram User: inbound image media download returned no data")
            return [], [], None

        if is_video or mime_type.startswith("video/") or ext in SUPPORTED_VIDEO_TYPES:
            video_ext = ext if ext in SUPPORTED_VIDEO_TYPES else ".mp4"
            try:
                target_path = _media_cache_target(MessageType.VIDEO, filename, video_ext)
                cached_path = await self._download_message_media_to_path(message, target_path)
            except Exception as exc:
                logger.warning(
                    "Telegram User: failed to download video media: %s",
                    exc,
                    exc_info=True,
                )
                return [], [], None
            if not cached_path:
                logger.info("Telegram User: inbound video media download returned no data")
                return [], [], None
            return (
                [cached_path],
                [SUPPORTED_VIDEO_TYPES.get(video_ext, mime_type or "video/mp4")],
                MessageType.VIDEO,
            )

        if is_voice or is_audio or mime_type.startswith("audio/") or ext in _AUDIO_EXTENSIONS:
            audio_ext = (
                ext if ext in _AUDIO_EXTENSIONS
                else _AUDIO_MIME_TO_EXT.get(mime_type, ".ogg" if is_voice else ".mp3")
            )
            if audio_ext == ".opus":
                audio_ext = ".ogg"
            try:
                target_path = _media_cache_target(
                    MessageType.VOICE if is_voice else MessageType.AUDIO,
                    filename,
                    audio_ext,
                )
                cached_path = await self._download_message_media_to_path(message, target_path)
            except Exception as exc:
                logger.warning(
                    "Telegram User: failed to download audio media: %s",
                    exc,
                    exc_info=True,
                )
                return [], [], None
            if not cached_path:
                logger.info("Telegram User: inbound audio media download returned no data")
                return [], [], None
            media_type = (
                mime_type if mime_type.startswith("audio/")
                else _AUDIO_EXT_TO_MIME.get(audio_ext, "audio/ogg")
            )
            return (
                [cached_path],
                [media_type],
                MessageType.VOICE if is_voice else MessageType.AUDIO,
            )

        if not mime_type:
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            target_path = _media_cache_target(MessageType.DOCUMENT, filename, ext)
            cached_path = await self._download_message_media_to_path(message, target_path)
        except Exception as exc:
            logger.warning(
                "Telegram User: failed to download document media: %s",
                exc,
                exc_info=True,
            )
            return [], [], None
        if not cached_path:
            logger.info("Telegram User: inbound document media download returned no data")
            return [], [], None
        return [cached_path], [mime_type], MessageType.DOCUMENT

    def _is_audio_file_attachment(self, message: Any) -> bool:
        """Return True for Telegram audio files, but not voice messages."""
        if not self._message_has_downloadable_media(message):
            return False
        if getattr(message, "voice", None) is not None:
            return False
        filename, ext, mime_type = _message_file_info(message)
        return (
            bool(getattr(message, "audio", None))
            or mime_type.startswith("audio/")
            or ext in _AUDIO_EXTENSIONS
        )

    def _audio_media_batch_key(
        self,
        event: Any,
        chat: Any,
        sender: Any,
        chat_type: str,
    ) -> str:
        message = getattr(event, "message", event)
        chat_id = str(getattr(event, "chat_id", "") or getattr(chat, "id", ""))
        sender_id = str(
            getattr(event, "sender_id", "")
            or getattr(sender, "id", "")
            or chat_id
        )
        thread_id = self._message_thread_id(message, chat_type) or ""
        return f"{chat_type}:{chat_id}:{sender_id}:{thread_id}"

    @staticmethod
    def _append_audio_batch_text(batch: Dict[str, Any], text: str) -> None:
        clean_text = str(text or "").strip()
        if not clean_text:
            return
        texts = batch.setdefault("texts", [])
        if clean_text not in texts:
            texts.append(clean_text)
        event = batch.get("event")
        if event is not None:
            event.text = "\n\n".join(texts)

    async def _queue_audio_media_batch_item(
        self,
        key: str,
        event: Any,
        chat: Any,
        sender: Any,
        text: str,
        chat_type: str,
    ) -> None:
        batch = self._audio_media_batches.get(key)
        if batch is None:
            msg_event = await self._build_message_event(event, chat, sender, "", chat_type)
            batch = {
                "event": msg_event,
                "texts": [],
                "download_tasks": [],
                "created_at": time.monotonic(),
                "flush_after": 0.0,
            }
            self._audio_media_batches[key] = batch
        self._append_audio_batch_text(batch, text)

        loop = asyncio.get_running_loop()
        task = loop.create_task(self._cache_inbound_media(getattr(event, "message", event)))
        batch.setdefault("download_tasks", []).append(task)
        self._schedule_audio_media_batch_flush(key)

    async def _attach_text_to_audio_media_batch(self, key: str, text: str) -> bool:
        batch = self._audio_media_batches.get(key)
        if batch is None:
            return False
        self._append_audio_batch_text(batch, text)
        self._schedule_audio_media_batch_flush(key)
        return True

    def _schedule_audio_media_batch_flush(self, key: str) -> None:
        batch = self._audio_media_batches.get(key)
        if batch is None:
            return
        delay = max(self.media_batch_delay_seconds, 0.0)
        batch["flush_after"] = time.monotonic() + delay
        task = self._audio_media_flush_tasks.get(key)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._audio_media_flush_tasks[key] = loop.create_task(
            self._flush_audio_media_batch_when_ready(key)
        )

    async def _flush_audio_media_batch_when_ready(self, key: str) -> None:
        try:
            while True:
                batch = self._audio_media_batches.get(key)
                if batch is None:
                    return
                flush_after = float(batch.get("flush_after") or 0.0)
                wait_seconds = flush_after - time.monotonic()
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                    continue

                tasks = list(batch.get("download_tasks", []) or [])
                if tasks:
                    await asyncio.gather(
                        *(asyncio.shield(task) for task in tasks),
                        return_exceptions=True,
                    )

                current = self._audio_media_batches.get(key)
                if current is not batch:
                    return
                latest_tasks = list(batch.get("download_tasks", []) or [])
                if (
                    len(latest_tasks) == len(tasks)
                    and time.monotonic() >= float(batch.get("flush_after") or 0.0)
                ):
                    break

            batch = self._audio_media_batches.pop(key, None)
            if batch is None:
                return

            media_urls: List[str] = []
            media_types: List[str] = []
            for task in batch.get("download_tasks", []) or []:
                try:
                    result = task.result()
                except Exception as exc:
                    logger.warning(
                        "Telegram User: failed to download batched audio: %s",
                        exc,
                        exc_info=True,
                    )
                    continue
                if not result:
                    continue
                urls, types, message_type = result
                if message_type != MessageType.AUDIO:
                    continue
                media_urls.extend(urls)
                media_types.extend(types)

            msg_event = batch.get("event")
            if msg_event is None:
                return
            if media_urls:
                msg_event.media_urls = media_urls
                msg_event.media_types = media_types
                msg_event.message_type = MessageType.AUDIO
            elif not str(getattr(msg_event, "text", "") or "").strip():
                msg_event.text = (
                    "[The user sent Telegram audio attachments, "
                    "but Hermes could not download them.]"
                )
            await self.handle_message(msg_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Telegram User: audio batch flush failed: %s", exc, exc_info=True)
        finally:
            task = self._audio_media_flush_tasks.get(key)
            if task is asyncio.current_task():
                self._audio_media_flush_tasks.pop(key, None)

    async def _cancel_audio_media_batches(self) -> None:
        flush_tasks = list(self._audio_media_flush_tasks.values())
        download_tasks = []
        for batch in self._audio_media_batches.values():
            download_tasks.extend(batch.get("download_tasks", []) or [])
        self._audio_media_flush_tasks.clear()
        self._audio_media_batches.clear()
        for task in flush_tasks + download_tasks:
            if task is not None and not task.done():
                task.cancel()
        pending = [
            task
            for task in flush_tasks + download_tasks
            if task is not None and task is not asyncio.current_task()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _handle_new_message(self, event: Any) -> None:
        """Telethon NewMessage callback."""
        try:
            message = getattr(event, "message", None)
            if message is None:
                return
            text = str(getattr(event, "raw_text", "") or getattr(message, "message", "") or "")
            has_media = self._message_has_downloadable_media(message)
            if not text.strip() and not has_media:
                return
            if getattr(event, "out", False) and not self.allow_outgoing:
                return

            chat = await event.get_chat()
            sender = await event.get_sender()
            sender_id = str(getattr(sender, "id", "") or getattr(event, "sender_id", "") or "")
            if self.ignore_self_messages and self._account_user_id and sender_id == self._account_user_id:
                return
            sub_key, sub_item = self._subscription_for_event(event, chat, sender)
            if sub_key and sub_item:
                await self._handle_subscription_message(sub_key, sub_item, event, chat, sender, text)
                return
            if not self._event_allowed(event, chat, sender):
                logger.debug("Telegram User: ignoring message from non-allowlisted chat")
                return

            chat_type = self._chat_type_from_event(event, chat)
            if chat_type != "dm" and self.allowed_users:
                candidates = self._identity_candidates(event, chat, sender)
                if not (candidates & self.allowed_users):
                    logger.debug("Telegram User: ignoring group message from non-allowlisted sender")
                    return
            if chat_type != "dm" and self.require_mention:
                triggered, cleaned = self._extract_group_trigger(text)
                if not triggered and not await self._is_reply_to_self(event):
                    return
                text = cleaned or text

            audio_batch_key = self._audio_media_batch_key(event, chat, sender, chat_type)
            if has_media and self._is_audio_file_attachment(message):
                await self._queue_audio_media_batch_item(
                    audio_batch_key,
                    event,
                    chat,
                    sender,
                    text,
                    chat_type,
                )
                return
            if text.strip() and not has_media and not text.lstrip().startswith("/"):
                if await self._attach_text_to_audio_media_batch(audio_batch_key, text):
                    return

            media_urls: List[str] = []
            media_types: List[str] = []
            media_message_type: Optional[MessageType] = None
            if has_media:
                media_urls, media_types, media_message_type = await self._cache_inbound_media(message)
                if not media_urls and not text.strip():
                    text = "[The user sent a Telegram attachment, but Hermes could not download it.]"

            msg_event = await self._build_message_event(event, chat, sender, text, chat_type)
            if media_urls and media_message_type is not None:
                msg_event.media_urls = media_urls
                msg_event.media_types = media_types
                msg_event.message_type = media_message_type
            await self.handle_message(msg_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Telegram User: inbound handler failed: %s", exc, exc_info=True)

    @staticmethod
    def _message_thread_id(message: Any, chat_type: str) -> Optional[str]:
        if chat_type == "dm":
            return None
        reply_to = getattr(message, "reply_to", None)
        if reply_to is None:
            return None
        for attr in ("reply_to_top_id", "reply_to_msg_id"):
            value = getattr(reply_to, attr, None)
            if value:
                return str(value)
        return None

    async def _build_message_event(
        self,
        event: Any,
        chat: Any,
        sender: Any,
        text: str,
        chat_type: str,
    ) -> MessageEvent:
        message = getattr(event, "message", event)
        chat_id = str(getattr(event, "chat_id", "") or getattr(chat, "id", ""))
        sender_id = str(getattr(event, "sender_id", "") or getattr(sender, "id", "") or chat_id)
        auth_user_id = sender_id if chat_type == "dm" else chat_id
        thread_id = self._message_thread_id(message, chat_type)

        reply_to_id = None
        reply_to_text = None
        if getattr(event, "is_reply", False):
            try:
                replied = await event.get_reply_message()
                if replied is not None:
                    reply_to_id = str(getattr(replied, "id", "") or "")
                    reply_to_text = getattr(replied, "raw_text", None) or getattr(replied, "message", None)
            except Exception:
                pass

        source = self.build_source(
            chat_id=chat_id,
            chat_name=_display_name(chat),
            chat_type=chat_type,
            user_id=auth_user_id,
            user_name=_display_name(sender),
            thread_id=thread_id,
            user_id_alt=sender_id if chat_type != "dm" else None,
            message_id=str(getattr(message, "id", "") or ""),
        )
        channel_prompt = resolve_channel_prompt(
            self.config.extra,
            thread_id or chat_id,
            chat_id if thread_id else None,
        )
        timestamp = getattr(message, "date", None)
        if not isinstance(timestamp, datetime):
            timestamp = datetime.now()
        return MessageEvent(
            text=text,
            message_type=MessageType.COMMAND if text.startswith("/") else MessageType.TEXT,
            source=source,
            raw_message=message,
            message_id=str(getattr(message, "id", "") or ""),
            reply_to_message_id=reply_to_id,
            reply_to_text=reply_to_text,
            channel_prompt=channel_prompt,
            timestamp=timestamp,
        )


def _config_values(config: Any) -> dict[str, Any]:
    extra = getattr(config, "extra", {}) or {}
    return {
        "api_id": os.getenv("TELEGRAM_USER_API_ID") or extra.get("api_id"),
        "api_hash": os.getenv("TELEGRAM_USER_API_HASH") or extra.get("api_hash"),
        "session_string": os.getenv("TELEGRAM_USER_SESSION_STRING") or extra.get("session_string"),
        "session_path": os.getenv("TELEGRAM_USER_SESSION_PATH") or extra.get("session_path"),
    }


def validate_config(config: Any) -> bool:
    values = _config_values(config)
    if not _coerce_int(values.get("api_id"), 0):
        return False
    if not str(values.get("api_hash") or "").strip():
        return False
    if str(values.get("session_string") or "").strip():
        return True
    return _session_file_exists(_resolve_session_path(values.get("session_path")))


def is_connected(config: Any) -> bool:
    return validate_config(config)


def check_requirements() -> bool:
    """Return True when runtime config exists and Telethon is importable."""
    if not _runtime_minimally_configured():
        return False
    if _load_telethon():
        return True
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("platform.telegram_user", prompt=False)
    except Exception:
        return False
    return _load_telethon()


def _env_enablement() -> dict | None:
    api_id = os.getenv("TELEGRAM_USER_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_USER_API_HASH", "").strip()
    session_string = os.getenv("TELEGRAM_USER_SESSION_STRING", "").strip()
    session_path = os.getenv("TELEGRAM_USER_SESSION_PATH", "").strip()
    if not (api_id and api_hash):
        return None
    if not session_string and not _session_file_exists(_resolve_session_path(session_path)):
        return None

    seed: dict[str, Any] = {
        "api_id": _coerce_int(api_id, 0),
        "api_hash": api_hash,
    }
    if session_string:
        seed["session_string"] = session_string
    if session_path:
        seed["session_path"] = session_path
    allowed_chats = os.getenv("TELEGRAM_USER_ALLOWED_CHATS", "").strip()
    if allowed_chats:
        seed["allowed_chats"] = [item.strip() for item in allowed_chats.split(",") if item.strip()]
    allowed_users = os.getenv("TELEGRAM_USER_ALLOWED_USERS", "").strip()
    if allowed_users:
        seed["allowed_users"] = [item.strip() for item in allowed_users.split(",") if item.strip()]
    for env_name, key in (
        ("TELEGRAM_USER_ALLOW_ALL_CHATS", "allow_all_chats"),
        ("TELEGRAM_USER_REQUIRE_MENTION", "require_mention"),
        ("TELEGRAM_USER_IGNORE_SELF_MESSAGES", "ignore_self_messages"),
        ("TELEGRAM_USER_ALLOW_OUTGOING", "allow_outgoing"),
    ):
        raw = os.getenv(env_name)
        if raw is not None and raw.strip():
            seed[key] = _coerce_bool(raw, False)
    interval = os.getenv("TELEGRAM_USER_SEND_INTERVAL_SECONDS", "").strip()
    if interval:
        seed["send_interval_seconds"] = _coerce_float(interval, 1.0)

    home = os.getenv("TELEGRAM_USER_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("TELEGRAM_USER_HOME_CHANNEL_NAME", "Telegram User Home"),
        }
    return seed


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Send a message without a live gateway adapter."""
    del thread_id
    if not _load_telethon():
        return {"error": "Telethon is not installed. Install telethon==1.43.0."}
    cfg = PlatformConfig(enabled=True, extra=dict(getattr(pconfig, "extra", {}) or {}))
    adapter = TelegramUserAdapter(cfg)
    adapter._client = TelegramClient(
        adapter._session_arg(),
        adapter.api_id,
        adapter.api_hash,
        sequential_updates=True,
        receive_updates=False,
        flood_sleep_threshold=int(adapter.flood_wait_threshold_seconds),
    )
    try:
        await adapter._client.connect()
        if not await adapter._client.is_user_authorized():
            return {"error": "Telegram user session is not authorized"}
        if media_files:
            last_id = None
            for file_path in media_files:
                result = await adapter._send_file(
                    chat_id,
                    file_path,
                    caption=message if last_id is None else None,
                    force_document=force_document,
                )
                if not result.success:
                    return {"error": result.error or "send_file failed"}
                last_id = result.message_id
            return {"success": True, "message_id": last_id}
        result = await adapter.send(chat_id, message)
        if result.success:
            return {"success": True, "message_id": result.message_id}
        return {"error": result.error or "send failed"}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {"error": f"Telegram user standalone send failed: {exc}"}
    finally:
        try:
            await adapter._client.disconnect()
        except Exception:
            pass


def _tg_sub_usage() -> str:
    return (
        "Usage:\n"
        "/tg-sub add <@channel|https://t.me/name> [--mode silent|notify|digest]\n"
        "/tg-sub join <https://t.me/+invitehash> [--mode silent|notify|digest]\n"
        "/tg-sub bot <@bot> [payload] [--mode silent|notify|digest]\n"
        "/tg-sub send <target> <message>\n"
        "/tg-sub mode <target> <silent|notify|digest>\n"
        "/tg-sub list\n"
        "/tg-sub digest [--clear]\n"
        "/tg-sub remove <target>\n"
        "/tg-sub leave <target>"
    )


def _split_tg_sub_args(raw_args: str) -> list[str]:
    try:
        return shlex.split(raw_args or "")
    except ValueError as exc:
        raise ValueError(f"Could not parse arguments: {exc}") from exc


def _pop_mode(tokens: list[str], default: str) -> tuple[list[str], str]:
    remaining: list[str] = []
    mode = default
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"--mode", "-m"}:
            if i + 1 >= len(tokens):
                raise ValueError("--mode requires a value")
            mode = _normalize_subscription_mode(tokens[i + 1], default)
            i += 2
            continue
        if token.startswith("--mode="):
            mode = _normalize_subscription_mode(token.split("=", 1)[1], default)
            i += 1
            continue
        remaining.append(token)
        i += 1
    return remaining, mode


def _format_tg_sub_result(action: str, result: dict[str, Any]) -> str:
    if result.get("error"):
        return f"Telegram subscription {action} failed: {result['error']}"
    item = result.get("item") or {}
    label = item.get("username") or item.get("title") or item.get("target") or item.get("key")
    mode = item.get("mode", "silent")
    return f"Telegram subscription {action}: {label} ({mode})"


def _format_tg_sub_list() -> str:
    items = [
        item for item in _load_subscription_store().get("items", {}).values()
        if isinstance(item, dict)
    ]
    if not items:
        return "No Telegram User subscriptions yet."
    lines = ["Telegram User subscriptions:"]
    lines.extend(_format_subscription_item(item) for item in sorted(
        items,
        key=lambda item: str(item.get("username") or item.get("title") or item.get("target") or ""),
    ))
    return "\n".join(lines)


def _format_tg_sub_digest(clear: bool = False) -> str:
    data = _load_subscription_store()
    rows = []
    changed = False
    for key, item in data.get("items", {}).items():
        if not isinstance(item, dict):
            continue
        pending = int(item.get("pending_count") or 0)
        if pending <= 0:
            continue
        label = item.get("username") or item.get("title") or item.get("target") or key
        text = str(item.get("last_text") or "").strip()
        if len(text) > 500:
            text = text[:497].rstrip() + "..."
        rows.append(f"- {label}: {pending} pending\n  Last: {text or '<no text>'}")
        if clear:
            item["pending_count"] = 0
            changed = True
    if changed:
        _save_subscription_store(data)
    if not rows:
        return "No pending Telegram subscription digest items."
    suffix = "\n\nDigest counters cleared." if clear else ""
    return "Telegram subscription digest:\n" + "\n".join(rows) + suffix


async def _handle_tg_sub_command(adapter: TelegramUserAdapter, raw_args: str) -> str:
    try:
        tokens = _split_tg_sub_args(raw_args)
    except ValueError as exc:
        return str(exc)
    if not tokens or tokens[0] in {"help", "-h", "--help"}:
        return _tg_sub_usage()

    action = tokens[0].lower()
    args = tokens[1:]

    try:
        if action in {"list", "ls"}:
            return _format_tg_sub_list()
        if action == "digest":
            return _format_tg_sub_digest(clear="--clear" in args)
        if action in {"add", "subscribe"}:
            args, mode = _pop_mode(args, "silent")
            if not args:
                return "Usage: /tg-sub add <@channel|https://t.me/name> [--mode silent|notify|digest]"
            return _format_tg_sub_result(
                "added",
                await adapter.subscribe_public_channel(args[0], mode=mode),
            )
        if action == "join":
            args, mode = _pop_mode(args, "silent")
            if not args:
                return "Usage: /tg-sub join <https://t.me/+invitehash> [--mode silent|notify|digest]"
            return _format_tg_sub_result(
                "joined",
                await adapter.join_private_invite(args[0], mode=mode),
            )
        if action in {"bot", "start-bot", "startbot"}:
            args, mode = _pop_mode(args, "notify")
            if not args:
                return "Usage: /tg-sub bot <@bot> [payload] [--mode silent|notify|digest]"
            target = args[0]
            payload = " ".join(args[1:]).strip()
            return _format_tg_sub_result(
                "bot started",
                await adapter.start_bot_subscription(target, payload=payload, mode=mode),
            )
        if action == "send":
            if len(args) < 2:
                return "Usage: /tg-sub send <target> <message>"
            return _format_tg_sub_result(
                "message sent to",
                await adapter.send_subscription_message(args[0], " ".join(args[1:])),
            )
        if action == "mode":
            if len(args) != 2:
                return "Usage: /tg-sub mode <target> <silent|notify|digest>"
            return _format_tg_sub_result(
                "mode updated for",
                await adapter.set_subscription_mode(args[0], args[1]),
            )
        if action in {"remove", "rm", "unsubscribe"}:
            if len(args) != 1:
                return "Usage: /tg-sub remove <target>"
            return _format_tg_sub_result(
                "removed",
                await adapter.unsubscribe_target(args[0], leave=False),
            )
        if action == "leave":
            if len(args) != 1:
                return "Usage: /tg-sub leave <target>"
            return _format_tg_sub_result(
                "left",
                await adapter.unsubscribe_target(args[0], leave=True),
            )
    except ValueError as exc:
        return str(exc)

    return _tg_sub_usage()


async def _handle_tg_sub_pre_dispatch(**kwargs: Any) -> Optional[dict[str, str]]:
    event = kwargs.get("event")
    gateway = kwargs.get("gateway")
    if event is None or gateway is None:
        return None
    command = event.get_command() if hasattr(event, "get_command") else None
    if command not in {"tg-sub", "tg_sub"}:
        return None

    source = getattr(event, "source", None)
    if source is None:
        return {"action": "skip", "reason": "telegram-user-subscription-missing-source"}
    try:
        if not gateway._is_user_authorized(source):  # noqa: SLF001 - gateway owns auth policy.
            logger.warning(
                "Telegram User: unauthorized /tg-sub from user=%s platform=%s",
                getattr(source, "user_id", None),
                getattr(getattr(source, "platform", None), "value", None),
            )
            return {"action": "skip", "reason": "telegram-user-subscription-unauthorized"}
    except Exception as exc:
        logger.warning("Telegram User: could not authorize /tg-sub: %s", exc)
        return {"action": "skip", "reason": "telegram-user-subscription-auth-error"}

    manager = getattr(gateway, "adapters", {}).get(Platform("telegram_user"))
    response_adapter = getattr(gateway, "adapters", {}).get(source.platform)
    if manager is None:
        response = "Telegram User adapter is not connected."
    else:
        response = await _handle_tg_sub_command(manager, event.get_command_args())

    if response_adapter is not None:
        await response_adapter.send(source.chat_id, response)
    return {"action": "skip", "reason": "telegram-user-subscription-command"}


def interactive_setup() -> None:
    """Interactive gateway setup flow for the Telegram user platform."""
    from hermes_cli.setup import (
        get_env_value,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt,
        prompt_yes_no,
        save_env_value,
    )

    print_header("Telegram User")
    print_info("Use a dedicated Telegram account for Hermes. The Telethon session is full account access.")

    api_id = prompt(
        "Telegram API ID from https://my.telegram.org/apps",
        default=get_env_value("TELEGRAM_USER_API_ID") or "",
    ).strip()
    if not api_id:
        print_warning("API ID is required")
        return
    try:
        int(api_id)
    except ValueError:
        print_warning("API ID must be numeric")
        return
    save_env_value("TELEGRAM_USER_API_ID", api_id)

    api_hash = prompt(
        "Telegram API hash",
        default=get_env_value("TELEGRAM_USER_API_HASH") or "",
        password=True,
    ).strip()
    if not api_hash:
        print_warning("API hash is required")
        return
    save_env_value("TELEGRAM_USER_API_HASH", api_hash)

    default_path = str(_default_session_path())
    session_path = prompt(
        "Telethon session path",
        default=get_env_value("TELEGRAM_USER_SESSION_PATH") or default_path,
    ).strip() or default_path
    save_env_value("TELEGRAM_USER_SESSION_PATH", session_path)

    allowed = prompt(
        "Allowed Telegram chat IDs/usernames (comma-separated, e.g. 123456789,@operator)",
        default=get_env_value("TELEGRAM_USER_ALLOWED_CHATS") or "",
    ).strip()
    if allowed:
        save_env_value("TELEGRAM_USER_ALLOWED_CHATS", allowed.replace(" ", ""))
    else:
        save_env_value("TELEGRAM_USER_ALLOWED_CHATS", "")
        print_warning("No allowed chats configured. Hermes will ignore inbound messages until you add one.")

    home = prompt(
        "Home chat ID for cron/notifications (optional)",
        default=get_env_value("TELEGRAM_USER_HOME_CHANNEL") or "",
    ).strip()
    if home:
        save_env_value("TELEGRAM_USER_HOME_CHANNEL", home)

    if not prompt_yes_no("Login and create the Telethon session now?", True):
        print_info("Configuration saved. Run setup again after the account is ready to create the session.")
        return

    if not _load_telethon():
        try:
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("platform.telegram_user", prompt=True)
        except Exception as exc:
            print_warning(f"Could not install Telethon automatically: {exc}")
            print_info("Install manually: uv pip install telethon==1.43.0")
            return
        if not _load_telethon():
            print_warning("Telethon still unavailable after install attempt")
            return

    phone = prompt(
        "Telegram phone number for the dedicated Hermes account",
        default=get_env_value("TELEGRAM_USER_PHONE") or "",
    ).strip()
    if not phone:
        print_warning("Phone number is required for first login")
        return
    save_env_value("TELEGRAM_USER_PHONE", phone)

    async def _login() -> None:
        path = _resolve_session_path(session_path)
        _ensure_private_parent(path)
        client = TelegramClient(str(path), int(api_id), api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                await client.send_code_request(phone)
                code = prompt("Telegram login code").strip()
                try:
                    await client.sign_in(phone=phone, code=code)
                except SessionPasswordNeededError:
                    password = prompt("Telegram 2FA password", password=True)
                    await client.sign_in(password=password)
            me = await client.get_me()
            print_success(
                f"Telegram session ready for user_id={getattr(me, 'id', '<unknown>')}"
            )
        finally:
            await client.disconnect()

    try:
        asyncio.run(_login())
    except Exception as exc:
        print_warning(f"Telegram login failed: {exc}")
        return
    print_success("Telegram User configuration saved. Restart the gateway to connect.")


def register(ctx: Any) -> None:
    """Plugin entry point."""
    ctx.register_command(
        "tg-sub",
        lambda _args: _tg_sub_usage(),
        description="Manage Telegram User channel and bot subscriptions",
        args_hint="<add|join|bot|list|send|mode|digest|remove|leave>",
    )
    ctx.register_hook("pre_gateway_dispatch", _handle_tg_sub_pre_dispatch)
    ctx.register_platform(
        name="telegram_user",
        label="Telegram User",
        adapter_factory=lambda cfg: TelegramUserAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["TELEGRAM_USER_API_ID", "TELEGRAM_USER_API_HASH"],
        install_hint="uv pip install telethon==1.43.0",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="TELEGRAM_USER_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        # For DMs, chat_id and user_id are the same. For groups/channels, the
        # adapter sets SessionSource.user_id to chat_id and preserves the real
        # sender in user_id_alt, so the same env var gates allowed chats while
        # TELEGRAM_USER_ALLOWED_USERS remains an adapter-level sender filter.
        allowed_users_env="TELEGRAM_USER_ALLOWED_CHATS",
        allow_all_env="TELEGRAM_USER_ALLOW_ALL_CHATS",
        max_message_length=TelegramUserAdapter.MAX_MESSAGE_LENGTH,
        pii_safe=True,
        emoji="TG",
        allow_update_command=True,
        platform_hint=(
            "You are chatting through a Telegram user-account transport "
            "(MTProto), not a bot. Keep responses natural and avoid implying "
            "bot-only controls such as inline buttons. Telegram supports basic "
            "markdown, replies, edits, and file delivery. The account is "
            "dedicated to Hermes and inbound chats are allowlisted."
        ),
    )
