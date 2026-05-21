"""Tests for the Telegram user-account platform adapter plugin."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_mod = load_plugin_adapter("telegram_user")

TelegramUserAdapter = _mod.TelegramUserAdapter
_env_enablement = _mod._env_enablement
_resolve_session_path = _mod._resolve_session_path
check_requirements = _mod.check_requirements
validate_config = _mod.validate_config
register = _mod.register


@pytest.fixture(autouse=True)
def _clear_telegram_user_env(monkeypatch):
    for key in (
        "TELEGRAM_USER_API_ID",
        "TELEGRAM_USER_API_HASH",
        "TELEGRAM_USER_SESSION_PATH",
        "TELEGRAM_USER_SESSION_STRING",
        "TELEGRAM_USER_PHONE",
        "TELEGRAM_USER_ALLOWED_CHATS",
        "TELEGRAM_USER_ALLOWED_USERS",
        "TELEGRAM_USER_ALLOW_ALL_CHATS",
        "TELEGRAM_USER_REQUIRE_MENTION",
        "TELEGRAM_USER_HOME_CHANNEL",
        "TELEGRAM_USER_SEND_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)


class _FakeMessage:
    def __init__(
        self,
        *,
        message_id: int = 10,
        text: str = "hello",
        media=None,
        file=None,
        photo=None,
        voice=None,
        audio=None,
        video=None,
        document=None,
        media_bytes=None,
    ):
        self.id = message_id
        self.message = text
        self.date = datetime(2026, 5, 12, 12, 0, 0)
        self.reply_to = None
        self.media = media
        self.file = file
        self.photo = photo
        self.voice = voice
        self.audio = audio
        self.video = video
        self.document = document
        self._media_bytes = media_bytes
        self.download_targets = []

    async def download_media(self, file=None):
        self.download_targets.append(file)
        if isinstance(self._media_bytes, BaseException):
            raise self._media_bytes
        if isinstance(file, (str, Path)):
            Path(file).write_bytes(self._media_bytes or b"")
            return str(file)
        return self._media_bytes


class _FakeEvent:
    def __init__(
        self,
        *,
        chat_id: int = 123,
        sender_id: int = 456,
        text: str = "hello",
        is_private: bool = True,
        is_group: bool = False,
        is_channel: bool = False,
        out: bool = False,
        reply=None,
        media=None,
        file=None,
        photo=None,
        voice=None,
        audio=None,
        video=None,
        document=None,
        media_bytes=None,
    ):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.raw_text = text
        self.message = _FakeMessage(
            text=text,
            media=media,
            file=file,
            photo=photo,
            voice=voice,
            audio=audio,
            video=video,
            document=document,
            media_bytes=media_bytes,
        )
        self.is_private = is_private
        self.is_group = is_group
        self.is_channel = is_channel
        self.out = out
        self.is_reply = reply is not None
        self._reply = reply
        self._chat = SimpleNamespace(id=chat_id, title="Test Chat", username="testchat")
        self._sender = SimpleNamespace(id=sender_id, first_name="Alice", username="alice")

    async def get_chat(self):
        return self._chat

    async def get_sender(self):
        return self._sender

    async def get_reply_message(self):
        return self._reply


class _FakeClient:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.entities = {}
        self.requests = []

    async def send_message(self, entity, message, **kwargs):
        self.sent.append((entity, message, kwargs))
        return SimpleNamespace(id=99)

    async def edit_message(self, entity, message_id, text, **kwargs):
        self.edited.append((entity, message_id, text, kwargs))
        return SimpleNamespace(id=message_id)

    async def get_entity(self, entity):
        key = str(entity).lower()
        return self.entities.get(key, SimpleNamespace(id=123, username=str(entity).lstrip("@"), title="Entity"))

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace(chats=[SimpleNamespace(id=789, username="", title="Private Channel")])


def _make_adapter(extra=None) -> TelegramUserAdapter:
    return TelegramUserAdapter(PlatformConfig(enabled=True, extra=extra or {}))


def test_check_requirements_false_without_runtime_config(monkeypatch):
    for key in (
        "TELEGRAM_USER_API_ID",
        "TELEGRAM_USER_API_HASH",
        "TELEGRAM_USER_SESSION_PATH",
        "TELEGRAM_USER_SESSION_STRING",
    ):
        monkeypatch.delenv(key, raising=False)
    assert check_requirements() is False


def test_check_requirements_true_with_env_session(monkeypatch, tmp_path):
    session_base = tmp_path / "tg_user"
    session_base.with_suffix(".session").write_text("fake", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_USER_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_USER_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_USER_SESSION_PATH", str(session_base))
    monkeypatch.setattr(_mod, "_load_telethon", lambda: True)

    assert check_requirements() is True


def test_env_enablement_seeds_extra_and_home(monkeypatch, tmp_path):
    session_base = tmp_path / "tg_user"
    session_base.with_suffix(".session").write_text("fake", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_USER_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_USER_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_USER_SESSION_PATH", str(session_base))
    monkeypatch.setenv("TELEGRAM_USER_ALLOWED_CHATS", "123,@operator")
    monkeypatch.setenv("TELEGRAM_USER_HOME_CHANNEL", "123")
    monkeypatch.setenv("TELEGRAM_USER_REQUIRE_MENTION", "false")

    seed = _env_enablement()

    assert seed["api_id"] == 12345
    assert seed["api_hash"] == "hash"
    assert seed["session_path"] == str(session_base)
    assert seed["allowed_chats"] == ["123", "@operator"]
    assert seed["require_mention"] is False
    assert seed["home_channel"]["chat_id"] == "123"


def test_validate_config_requires_authorized_session_file(tmp_path):
    session_base = tmp_path / "tg_user"
    cfg = PlatformConfig(
        enabled=True,
        extra={"api_id": 123, "api_hash": "hash", "session_path": str(session_base)},
    )
    assert validate_config(cfg) is False

    session_base.with_suffix(".session").write_text("fake", encoding="utf-8")
    assert validate_config(cfg) is True


def test_adapter_default_denies_inbound_chat():
    adapter = _make_adapter()
    event = _FakeEvent(chat_id=123, sender_id=456)
    chat = SimpleNamespace(id=123, username="allowed")
    sender = SimpleNamespace(id=456, username="alice")

    assert adapter._event_allowed(event, chat, sender) is False


def test_adapter_allows_chat_by_id_or_username():
    adapter = _make_adapter({"allowed_chats": ["123", "@alice"]})
    event = _FakeEvent(chat_id=123, sender_id=456)
    chat = SimpleNamespace(id=123, username="room")
    sender = SimpleNamespace(id=456, username="alice")

    assert adapter._event_allowed(event, chat, sender) is True

    adapter = _make_adapter({"allowed_chats": ["@alice"]})
    assert adapter._event_allowed(event, chat, sender) is True


@pytest.mark.asyncio
async def test_build_message_event_maps_private_chat():
    adapter = _make_adapter({"allowed_chats": ["123"]})
    event = _FakeEvent(chat_id=123, sender_id=456, text="/status")
    chat = await event.get_chat()
    sender = await event.get_sender()

    msg_event = await adapter._build_message_event(event, chat, sender, "/status", "dm")

    assert msg_event.text == "/status"
    assert msg_event.is_command()
    assert msg_event.source.platform.value == "telegram_user"
    assert msg_event.source.chat_id == "123"
    assert msg_event.source.user_id == "456"
    assert msg_event.source.chat_type == "dm"
    assert msg_event.message_id == "10"


@pytest.mark.asyncio
async def test_handle_new_message_downloads_voice_only_media(monkeypatch, tmp_path):
    adapter = _make_adapter({"allowed_chats": ["123"]})
    adapter._account_user_id = "999"
    monkeypatch.setattr(_mod, "get_audio_cache_dir", lambda: tmp_path)

    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture

    event = _FakeEvent(
        chat_id=123,
        sender_id=456,
        text="",
        voice=SimpleNamespace(),
        file=SimpleNamespace(name="voice.ogg", ext=".ogg", mime_type="audio/ogg"),
        media_bytes=b"voice-bytes",
    )

    await adapter._handle_new_message(event)

    assert len(captured) == 1
    assert captured[0].text == ""
    assert captured[0].message_type == MessageType.VOICE
    media_path = Path(captured[0].media_urls[0])
    assert media_path.parent == tmp_path
    assert media_path.name.startswith("audio_")
    assert media_path.suffix == ".ogg"
    assert media_path.read_bytes() == b"voice-bytes"
    assert captured[0].media_types == ["audio/ogg"]
    assert event.message.download_targets
    assert event.message.download_targets[0] is not bytes


@pytest.mark.asyncio
async def test_handle_new_message_downloads_document_to_safe_cache_path(monkeypatch, tmp_path):
    adapter = _make_adapter({"allowed_chats": ["123"]})
    adapter._account_user_id = "999"
    monkeypatch.setattr(_mod, "get_document_cache_dir", lambda: tmp_path)

    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture

    event = _FakeEvent(
        chat_id=123,
        sender_id=456,
        text="",
        document=SimpleNamespace(mime_type="application/pdf", attributes=[]),
        file=SimpleNamespace(name="../../secret.pdf", ext=".pdf", mime_type="application/pdf"),
        media_bytes=b"%PDF-test",
    )

    await adapter._handle_new_message(event)

    assert len(captured) == 1
    assert captured[0].message_type == MessageType.DOCUMENT
    media_path = Path(captured[0].media_urls[0])
    assert media_path.parent == tmp_path
    assert media_path.name.startswith("doc_")
    assert media_path.name.endswith("_secret.pdf")
    assert media_path.read_bytes() == b"%PDF-test"
    assert event.message.download_targets[0] is not bytes


@pytest.mark.asyncio
async def test_recover_client_connection_reconnects_existing_client(monkeypatch):
    adapter = _make_adapter({"allowed_chats": ["123"]})
    status_writes = []

    class FakeClient:
        def __init__(self):
            self.disconnects = 0
            self.connects = 0

        async def disconnect(self):
            self.disconnects += 1

        async def connect(self):
            self.connects += 1

        async def is_user_authorized(self):
            return True

    async def no_sleep(_seconds):
        return None

    fake_client = FakeClient()
    adapter._client = fake_client
    adapter._running = True
    monkeypatch.setattr(_mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        adapter,
        "_write_runtime_status_safe",
        lambda context, **kwargs: status_writes.append((context, kwargs)),
    )

    await adapter._recover_client_connection("health_check_failed", RuntimeError("stale receiver"))

    assert fake_client.disconnects == 1
    assert fake_client.connects == 1
    assert adapter.is_connected is True
    assert any(context == "health_failed" for context, _ in status_writes)


@pytest.mark.asyncio
async def test_build_message_event_maps_group_auth_to_chat_id():
    adapter = _make_adapter({"allowed_chats": ["-100123"]})
    event = _FakeEvent(
        chat_id=-100123,
        sender_id=456,
        text="hermes: ping",
        is_private=False,
        is_group=True,
    )
    chat = await event.get_chat()
    sender = await event.get_sender()

    msg_event = await adapter._build_message_event(event, chat, sender, "ping", "group")

    assert msg_event.source.chat_id == "-100123"
    assert msg_event.source.user_id == "-100123"
    assert msg_event.source.user_id_alt == "456"
    assert msg_event.source.chat_type == "group"


@pytest.mark.asyncio
async def test_handle_new_message_requires_group_trigger():
    adapter = _make_adapter({"allowed_chats": ["123"], "require_mention": True})
    adapter._account_user_id = "999"
    adapter._account_username = "hermes"
    adapter._account_display_names = {"hermes"}

    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture

    event = _FakeEvent(
        chat_id=123,
        sender_id=456,
        text="just talking",
        is_private=False,
        is_group=True,
    )
    await adapter._handle_new_message(event)
    assert captured == []

    event = _FakeEvent(
        chat_id=123,
        sender_id=456,
        text="hermes: ping",
        is_private=False,
        is_group=True,
    )
    await adapter._handle_new_message(event)
    assert len(captured) == 1
    assert captured[0].text == "ping"


@pytest.mark.asyncio
async def test_send_uses_telethon_send_message():
    adapter = _make_adapter()
    adapter._client = _FakeClient()
    adapter.send_interval_seconds = 0

    result = await adapter.send("123", "hello")

    assert result.success is True
    assert result.message_id == "99"
    assert adapter._client.sent == [
        (123, "hello", {"reply_to": None, "parse_mode": "md", "link_preview": False})
    ]


@pytest.mark.asyncio
async def test_edit_message_uses_telethon_edit_message():
    adapter = _make_adapter()
    adapter._client = _FakeClient()
    adapter.send_interval_seconds = 0

    result = await adapter.edit_message("123", "99", "updated")

    assert result.success is True
    assert adapter._client.edited == [
        (123, 99, "updated", {"parse_mode": "md", "link_preview": False})
    ]


def test_subscription_store_round_trips(monkeypatch, tmp_path):
    store_path = tmp_path / "subscriptions.json"
    monkeypatch.setattr(_mod, "_subscription_store_path", lambda: store_path)

    adapter = _make_adapter()
    item = adapter._subscription_record(
        entity=SimpleNamespace(id=42, username="news", title="News", broadcast=True),
        target="@news",
        kind="channel",
        mode="notify",
    )

    assert item["key"] == "@news"
    assert item["mode"] == "notify"
    assert _mod._find_subscription_item("@news")[1]["title"] == "News"


@pytest.mark.asyncio
async def test_subscription_inbound_notify_does_not_dispatch_to_agent(monkeypatch, tmp_path):
    store_path = tmp_path / "subscriptions.json"
    monkeypatch.setattr(_mod, "_subscription_store_path", lambda: store_path)

    adapter = _make_adapter({"home_channel": {"chat_id": "973126834"}})
    adapter._client = _FakeClient()
    adapter.send_interval_seconds = 0
    adapter._subscription_record(
        entity=SimpleNamespace(id=123, username="news", title="News", broadcast=True),
        target="@news",
        kind="channel",
        mode="notify",
    )
    adapter.handle_message = AsyncMock()

    event = _FakeEvent(chat_id=123, sender_id=123, text="breaking", is_private=False, is_channel=True)
    event._chat = SimpleNamespace(id=123, title="News", username="news", broadcast=True)
    event._sender = SimpleNamespace(id=123, title="News", username="news")

    await adapter._handle_new_message(event)

    adapter.handle_message.assert_not_awaited()
    assert adapter._client.sent[0][0] == 973126834
    assert "breaking" in adapter._client.sent[0][1]


@pytest.mark.asyncio
async def test_tg_sub_hook_uses_live_adapter_and_replies(monkeypatch, tmp_path):
    store_path = tmp_path / "subscriptions.json"
    monkeypatch.setattr(_mod, "_subscription_store_path", lambda: store_path)

    manager = _make_adapter()
    manager.subscribe_public_channel = AsyncMock(
        return_value={"success": True, "item": {"username": "@news", "mode": "notify"}}
    )
    response_adapter = SimpleNamespace(send=AsyncMock())
    gateway = SimpleNamespace(
        adapters={Platform("telegram_user"): manager, Platform.TELEGRAM: response_adapter},
        _is_user_authorized=lambda _source: True,
    )
    event = MessageEvent(
        text="/tg-sub add @news --mode notify",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="973126834",
            user_id="973126834",
            chat_type="dm",
        ),
    )

    result = await _mod._handle_tg_sub_pre_dispatch(event=event, gateway=gateway)

    assert result == {"action": "skip", "reason": "telegram-user-subscription-command"}
    manager.subscribe_public_channel.assert_awaited_once_with("@news", mode="notify")
    response_adapter.send.assert_awaited_once()
    assert "@news" in response_adapter.send.await_args.args[1]


def test_register_exposes_platform_entry():
    class Ctx:
        def __init__(self):
            self.kwargs = None
            self.commands = {}
            self.hooks = {}

        def register_command(self, name, handler, **kwargs):
            self.commands[name] = {"handler": handler, **kwargs}

        def register_hook(self, name, handler):
            self.hooks[name] = handler

        def register_platform(self, **kwargs):
            self.kwargs = kwargs

    ctx = Ctx()
    register(ctx)

    assert ctx.kwargs["name"] == "telegram_user"
    assert ctx.kwargs["label"] == "Telegram User"
    assert ctx.kwargs["allowed_users_env"] == "TELEGRAM_USER_ALLOWED_CHATS"
    assert ctx.kwargs["allow_all_env"] == "TELEGRAM_USER_ALLOW_ALL_CHATS"
    assert callable(ctx.kwargs["standalone_sender_fn"])
    assert "tg-sub" in ctx.commands
    assert "pre_gateway_dispatch" in ctx.hooks
