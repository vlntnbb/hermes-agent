#!/usr/bin/env python3
"""
Transcription Tools Module

Provides speech-to-text transcription with eight providers:

  - **local** (default, free) — faster-whisper running locally, no API key needed.
    Auto-downloads the model (~150 MB for ``base``) on first use.
  - **groq** (free tier) — Groq Whisper API, requires ``GROQ_API_KEY``.
  - **openai** (paid) — OpenAI Whisper API, requires ``VOICE_TOOLS_OPENAI_KEY``.
  - **mistral** — Mistral Voxtral Transcribe API, requires ``MISTRAL_API_KEY``.
  - **xai** — xAI Grok STT API, requires ``XAI_API_KEY``. High accuracy,
    Inverse Text Normalization, diarization, 21 languages.
  - **gigaam** — local GigaAM model for Russian speech, no API key needed.
    Requires the ``gigaam`` package and ``ffmpeg`` for non-WAV inputs.
  - **ideal_rus** — high-accuracy Russian pipeline: GigaAM + Groq Whisper,
    then an auxiliary LLM merge into one clean transcript.

Used by the messaging gateway to automatically transcribe voice messages
sent by users on Telegram, Discord, WhatsApp, Slack, and Signal.

Supported input formats: mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg, aac

Usage::

    from tools.transcription_tools import transcribe_audio

    result = transcribe_audio("/path/to/audio.ogg")
    if result["success"]:
        print(result["transcript"])
"""

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import wave
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional, Dict, Any, Mapping
from urllib.parse import urljoin

from utils import is_truthy_value
from tools.managed_tool_gateway import resolve_managed_tool_gateway
from tools.tool_backend_helpers import managed_nous_tools_enabled, resolve_openai_audio_api_key

logger = logging.getLogger(__name__)

def get_env_value(name, default=None):
    """Read env values through the live config module.

    Tests may monkeypatch and later restore ``hermes_cli.config.get_env_value``
    before this module is imported. Resolve the helper at call time so STT does
    not keep a stale imported function for the rest of the test process.
    """
    try:
        from hermes_cli.config import get_env_value as _get_env_value
    except ImportError:
        return os.getenv(name, default)
    value = _get_env_value(name)
    return default if value is None else value

# ---------------------------------------------------------------------------
# Optional imports — graceful degradation
# ---------------------------------------------------------------------------

import importlib.util as _ilu


def _safe_find_spec(module_name: str) -> bool:
    try:
        return _ilu.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return module_name in globals() or module_name in os.sys.modules


_HAS_FASTER_WHISPER = _safe_find_spec("faster_whisper")
_HAS_OPENAI = _safe_find_spec("openai")
_HAS_MISTRAL = _safe_find_spec("mistralai")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER = "local"
DEFAULT_LOCAL_MODEL = "base"
DEFAULT_LOCAL_STT_LANGUAGE = "en"
DEFAULT_STT_MODEL = os.getenv("STT_OPENAI_MODEL", "whisper-1")
DEFAULT_GROQ_STT_MODEL = os.getenv("STT_GROQ_MODEL", "whisper-large-v3-turbo")
DEFAULT_MISTRAL_STT_MODEL = os.getenv("STT_MISTRAL_MODEL", "voxtral-mini-latest")
DEFAULT_GIGAAM_STT_MODEL = "v3_e2e_rnnt"
LOCAL_STT_COMMAND_ENV = "HERMES_LOCAL_STT_COMMAND"
LOCAL_STT_LANGUAGE_ENV = "HERMES_LOCAL_STT_LANGUAGE"
COMMON_LOCAL_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
DEFAULT_GROQ_CHUNK_SECONDS = 600
DEFAULT_GROQ_CHUNK_BITRATE = "64k"
DEFAULT_GROQ_RATE_LIMIT_MAX_WAIT_SECONDS = 24 * 60 * 60
DEFAULT_GROQ_RATE_LIMIT_FALLBACK_WAIT_SECONDS = 60
DEFAULT_IDEAL_RUS_MERGE_TASK = "transcription_merge"

GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
OPENAI_BASE_URL = os.getenv("STT_OPENAI_BASE_URL", "https://api.openai.com/v1")
XAI_STT_BASE_URL = os.getenv("XAI_STT_BASE_URL", "https://api.x.ai/v1")

SUPPORTED_FORMATS = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".aac", ".flac"}
LOCAL_NATIVE_AUDIO_FORMATS = {".wav", ".aiff", ".aif"}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB
PROVIDERS_WITH_INTERNAL_SIZE_HANDLING = {"local", "local_command", "gigaam", "groq", "ideal_rus"}

# Known model sets for auto-correction
OPENAI_MODELS = {"whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"}
GROQ_MODELS = {"whisper-large-v3", "whisper-large-v3-turbo", "distil-whisper-large-v3-en"}
GIGAAM_MODEL_ALIASES = {"rnnt": "v2_rnnt", "ctc": "v2_ctc"}
GIGAAM_FALLBACK_MODELS = {
    "v3_e2e_rnnt": "v2_rnnt",
    "v3_rnnt": "v2_rnnt",
    "v3_e2e_ctc": "v2_ctc",
    "v3_ctc": "v2_ctc",
}
IDEAL_RUS_PROVIDER_ALIASES = {"ideal", "ideal-rus", "ideal_rus", "russian_ideal", "gigaam_groq"}

# Singleton for the local model — loaded once, reused across calls
_local_model: Optional[object] = None
_local_model_name: Optional[str] = None

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------



def _load_stt_config() -> dict:
    """Load the ``stt`` section from user config, falling back to defaults."""
    try:
        from hermes_cli.config import load_config
        return load_config().get("stt", {})
    except Exception:
        return {}


def is_stt_enabled(stt_config: Optional[dict] = None) -> bool:
    """Return whether STT is enabled in config."""
    if stt_config is None:
        stt_config = _load_stt_config()
    enabled = stt_config.get("enabled", True)
    return is_truthy_value(enabled, default=True)


def _has_openai_audio_backend() -> bool:
    """Return True when OpenAI audio can use config credentials, env credentials, or the managed gateway."""
    try:
        _resolve_openai_audio_client_config()
        return True
    except ValueError:
        return False


def _find_binary(binary_name: str) -> Optional[str]:
    """Find a local binary, checking common Homebrew/local prefixes as well as PATH."""
    for directory in COMMON_LOCAL_BIN_DIRS:
        candidate = Path(directory) / binary_name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(binary_name)


def _find_ffmpeg_binary() -> Optional[str]:
    return _find_binary("ffmpeg")


def _find_whisper_binary() -> Optional[str]:
    return _find_binary("whisper")


def _get_local_command_template() -> Optional[str]:
    configured = os.getenv(LOCAL_STT_COMMAND_ENV, "").strip()
    if configured:
        return configured

    whisper_binary = _find_whisper_binary()
    if whisper_binary:
        quoted_binary = shlex.quote(whisper_binary)
        return (
            f"{quoted_binary} {{input_path}} --model {{model}} --output_format txt "
            "--output_dir {output_dir} --language {language}"
        )
    return None


def _has_local_command() -> bool:
    return _get_local_command_template() is not None


def _normalize_local_model(model_name: Optional[str]) -> str:
    """Return a valid faster-whisper model size, mapping cloud-only names to the default.

    Cloud providers like OpenAI use names such as ``whisper-1`` which are not
    valid for faster-whisper (which expects ``tiny``, ``base``, ``small``,
    ``medium``, or ``large-v*``).  When such a name is detected we fall back to
    the default local model and emit a warning so the user knows what happened.
    """
    if not model_name or model_name in OPENAI_MODELS or model_name in GROQ_MODELS:
        if model_name and (model_name in OPENAI_MODELS or model_name in GROQ_MODELS):
            logger.warning(
                "STT model '%s' is a cloud-only name and cannot be used with the local "
                "provider. Falling back to '%s'. Set stt.local.model to a valid "
                "faster-whisper size (tiny, base, small, medium, large-v3).",
                model_name,
                DEFAULT_LOCAL_MODEL,
            )
        return DEFAULT_LOCAL_MODEL
    return model_name


def _normalize_local_command_model(model_name: Optional[str]) -> str:
    return _normalize_local_model(model_name)


def _get_provider(stt_config: dict) -> str:
    """Determine which STT provider to use.

    When ``stt.provider`` is explicitly set in config, that choice is
    honoured — no silent cloud fallback.  When no provider is configured,
    auto-detect tries: local > groq (free) > openai (paid).
    """
    if not is_stt_enabled(stt_config):
        return "none"

    explicit = "provider" in stt_config
    provider = stt_config.get("provider", DEFAULT_PROVIDER)
    provider_normalized = str(provider or "").strip().lower()

    # --- Explicit provider: respect the user's choice ----------------------

    if explicit:
        if provider_normalized in IDEAL_RUS_PROVIDER_ALIASES:
            if _HAS_OPENAI and get_env_value("GROQ_API_KEY"):
                return "ideal_rus"
            logger.warning(
                "STT provider 'ideal_rus' configured but GROQ_API_KEY is not set "
                "or the openai package is unavailable"
            )
            return "none"

        if provider == "local":
            if _HAS_FASTER_WHISPER:
                return "local"
            if _has_local_command():
                return "local_command"
            logger.warning(
                "STT provider 'local' configured but unavailable "
                "(install faster-whisper or set HERMES_LOCAL_STT_COMMAND)"
            )
            return "none"

        if provider == "local_command":
            if _has_local_command():
                return "local_command"
            if _HAS_FASTER_WHISPER:
                logger.info("Local STT command unavailable, using local faster-whisper")
                return "local"
            logger.warning(
                "STT provider 'local_command' configured but unavailable"
            )
            return "none"

        if provider == "gigaam":
            return "gigaam"

        if provider == "groq":
            if _HAS_OPENAI and get_env_value("GROQ_API_KEY"):
                return "groq"
            logger.warning(
                "STT provider 'groq' configured but GROQ_API_KEY not set"
            )
            return "none"

        if provider == "openai":
            if _HAS_OPENAI and _has_openai_audio_backend():
                return "openai"
            logger.warning(
                "STT provider 'openai' configured but no API key available"
            )
            return "none"

        if provider == "mistral":
            # `mistralai` PyPI package was quarantined on 2026-05-12 after a
            # malicious 2.4.6 release. Refuse to use this provider until it's
            # available again so we surface a clear message instead of an
            # opaque ImportError mid-call.
            logger.warning(
                "STT provider 'mistral' (Voxtral Transcribe) is temporarily "
                "disabled — `mistralai` PyPI package is quarantined "
                "(malicious 2.4.6 release on 2026-05-12). Falling back to "
                "another provider. Set stt.provider in config.yaml to 'local' "
                "or 'openai' to silence this warning."
            )
            return "none"

        if provider == "xai":
            from tools.xai_http import resolve_xai_http_credentials

            if resolve_xai_http_credentials().get("api_key"):
                return "xai"
            logger.warning(
                "STT provider 'xai' configured but no xAI credentials are available"
            )
            return "none"

        return provider  # Unknown — let it fail downstream

    # --- Auto-detect (no explicit provider): local > groq > openai > xai ---
    # GigaAM is opt-in only: loading it can lazily install a package and
    # download large model weights, so do not select it implicitly.
    # mistral is intentionally skipped while `mistralai` is quarantined on
    # PyPI (malicious 2.4.6 release on 2026-05-12).

    if _HAS_FASTER_WHISPER:
        return "local"
    if _has_local_command():
        return "local_command"
    if _HAS_OPENAI and get_env_value("GROQ_API_KEY"):
        logger.info("No local STT available, using Groq Whisper API")
        return "groq"
    if _HAS_OPENAI and _has_openai_audio_backend():
        logger.info("No local STT available, using OpenAI Whisper API")
        return "openai"
    try:
        from tools.xai_http import resolve_xai_http_credentials

        if resolve_xai_http_credentials().get("api_key"):
            logger.info("No local STT available, using xAI Grok STT API")
            return "xai"
    except Exception:
        pass
    return "none"

# ---------------------------------------------------------------------------
# Shared validation
# ---------------------------------------------------------------------------


def _validate_audio_file(
    file_path: str,
    *,
    enforce_size_limit: bool = True,
) -> Optional[Dict[str, Any]]:
    """Validate the audio file.  Returns an error dict or None if OK."""
    audio_path = Path(file_path)

    if not audio_path.exists():
        return {"success": False, "transcript": "", "error": f"Audio file not found: {file_path}"}
    if not audio_path.is_file():
        return {"success": False, "transcript": "", "error": f"Path is not a file: {file_path}"}
    if audio_path.suffix.lower() not in SUPPORTED_FORMATS:
        return {
            "success": False,
            "transcript": "",
            "error": f"Unsupported format: {audio_path.suffix}. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}",
        }
    try:
        file_size = audio_path.stat().st_size
        if enforce_size_limit and file_size > MAX_FILE_SIZE:
            return {
                "success": False,
                "transcript": "",
                "error": f"File too large: {file_size / (1024*1024):.1f}MB (max {MAX_FILE_SIZE / (1024*1024):.0f}MB)",
            }
    except OSError as e:
        return {"success": False, "transcript": "", "error": f"Failed to access file: {e}"}

    return None

# ---------------------------------------------------------------------------
# Provider: local (faster-whisper)
# ---------------------------------------------------------------------------


# Substrings that identify a missing/unloadable CUDA runtime library.  When
# ctranslate2 (the backend for faster-whisper) cannot dlopen one of these, the
# "auto" device picker has already committed to CUDA and the model can no
# longer be used — we fall back to CPU and reload.
#
# Deliberately narrow: we match on library-name tokens and dlopen phrasing so
# we DO NOT accidentally catch legitimate runtime failures like "CUDA out of
# memory" — those should surface to the user, not silently fall back to CPU
# (a 32GB audio clip on CPU at int8 isn't useful either).
_CUDA_LIB_ERROR_MARKERS = (
    "libcublas",
    "libcudnn",
    "libcudart",
    "cannot be loaded",
    "cannot open shared object",
    "no kernel image is available",
    "no CUDA-capable device",
    "CUDA driver version is insufficient",
)


def _looks_like_cuda_lib_error(exc: BaseException) -> bool:
    """Heuristic: is this exception a missing/broken CUDA runtime library?

    ctranslate2 raises plain RuntimeError with messages like
    ``Library libcublas.so.12 is not found or cannot be loaded``.  We want to
    catch missing/unloadable shared libs and driver-mismatch errors, NOT
    legitimate runtime failures ("CUDA out of memory", model bugs, etc.).
    """
    msg = str(exc)
    return any(marker in msg for marker in _CUDA_LIB_ERROR_MARKERS)


def _load_local_whisper_model(model_name: str):
    """Load faster-whisper with graceful CUDA → CPU fallback.

    faster-whisper's ``device="auto"`` picks CUDA when the ctranslate2 wheel
    ships CUDA shared libs, even on hosts where the NVIDIA runtime
    (``libcublas.so.12`` / ``libcudnn*``) isn't installed — common on WSL2
    without CUDA-on-WSL, headless servers, and CPU-only developer machines.
    On those hosts the load itself sometimes succeeds and the dlopen failure
    only surfaces at first ``transcribe()`` call.

    We try ``auto`` first (fast CUDA path when it works), and on any CUDA
    library load failure fall back to CPU + int8.
    """
    from faster_whisper import WhisperModel
    try:
        return WhisperModel(model_name, device="auto", compute_type="auto")
    except Exception as exc:
        if not _looks_like_cuda_lib_error(exc):
            raise
        logger.warning(
            "faster-whisper CUDA load failed (%s) — falling back to CPU (int8). "
            "Install the NVIDIA CUDA runtime (libcublas/libcudnn) to use GPU.",
            exc,
        )
        return WhisperModel(model_name, device="cpu", compute_type="int8")


def _transcribe_local(file_path: str, model_name: str) -> Dict[str, Any]:
    """Transcribe using faster-whisper (local, free)."""
    global _local_model, _local_model_name

    if not _HAS_FASTER_WHISPER:
        return {"success": False, "transcript": "", "error": "faster-whisper not installed"}

    try:
        # Lazy-load the model (downloads on first use, ~150 MB for 'base')
        if _local_model is None or _local_model_name != model_name:
            logger.info("Loading faster-whisper model '%s' (first load downloads the model)...", model_name)
            _local_model = _load_local_whisper_model(model_name)
            _local_model_name = model_name

        # Language: config.yaml (stt.local.language) > env var > auto-detect.
        _forced_lang = (
            _load_stt_config().get("local", {}).get("language")
            or os.getenv(LOCAL_STT_LANGUAGE_ENV)
            or None
        )
        transcribe_kwargs = {"beam_size": 5}
        if _forced_lang:
            transcribe_kwargs["language"] = _forced_lang

        try:
            segments, info = _local_model.transcribe(file_path, **transcribe_kwargs)
            transcript = " ".join(segment.text.strip() for segment in segments)
        except Exception as exc:
            # CUDA runtime libs sometimes only fail at dlopen-on-first-use,
            # AFTER the model loaded successfully.  Evict the broken cached
            # model, reload on CPU, retry once.  Without this the module-
            # global `_local_model` is poisoned and every subsequent voice
            # message on this process fails identically until restart.
            if not _looks_like_cuda_lib_error(exc):
                raise
            logger.warning(
                "faster-whisper CUDA runtime failed mid-transcribe (%s) — "
                "evicting cached model and retrying on CPU (int8).",
                exc,
            )
            _local_model = None
            _local_model_name = None
            from faster_whisper import WhisperModel
            _local_model = WhisperModel(model_name, device="cpu", compute_type="int8")
            _local_model_name = model_name
            segments, info = _local_model.transcribe(file_path, **transcribe_kwargs)
            transcript = " ".join(segment.text.strip() for segment in segments)

        logger.info(
            "Transcribed %s via local whisper (%s, lang=%s, %.1fs audio)",
            Path(file_path).name, model_name, info.language, info.duration,
        )

        return {"success": True, "transcript": transcript, "provider": "local"}

    except Exception as e:
        logger.error("Local transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Local transcription failed: {e}"}


def _prepare_local_audio(file_path: str, work_dir: str) -> tuple[Optional[str], Optional[str]]:
    """Normalize audio for local CLI STT when needed."""
    audio_path = Path(file_path)
    if audio_path.suffix.lower() in LOCAL_NATIVE_AUDIO_FORMATS:
        return file_path, None

    ffmpeg = _find_ffmpeg_binary()
    if not ffmpeg:
        return None, "Local STT fallback requires ffmpeg for non-WAV inputs, but ffmpeg was not found"

    converted_path = os.path.join(work_dir, f"{audio_path.stem}.wav")
    command = [ffmpeg, "-y", "-i", file_path, converted_path]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return converted_path, None
    except subprocess.CalledProcessError as e:
        details = e.stderr.strip() or e.stdout.strip() or str(e)
        logger.error("ffmpeg conversion failed for %s: %s", file_path, details)
        return None, f"Failed to convert audio for local STT: {details}"


def _transcribe_local_command(file_path: str, model_name: str) -> Dict[str, Any]:
    """Run the configured local STT command template and read back a .txt transcript."""
    command_template = _get_local_command_template()
    if not command_template:
        return {
            "success": False,
            "transcript": "",
            "error": (
                f"{LOCAL_STT_COMMAND_ENV} not configured and no local whisper binary was found"
            ),
        }

    # Language: config.yaml (stt.local.language) > env var > "en" default.
    language = (
        _load_stt_config().get("local", {}).get("language")
        or os.getenv(LOCAL_STT_LANGUAGE_ENV)
        or DEFAULT_LOCAL_STT_LANGUAGE
    )
    normalized_model = _normalize_local_command_model(model_name)

    try:
        with tempfile.TemporaryDirectory(prefix="hermes-local-stt-") as output_dir:
            prepared_input, prep_error = _prepare_local_audio(file_path, output_dir)
            if prep_error:
                return {"success": False, "transcript": "", "error": prep_error}

            command = command_template.format(
                input_path=shlex.quote(prepared_input),
                output_dir=shlex.quote(output_dir),
                language=shlex.quote(language),
                model=shlex.quote(normalized_model),
            )
            # User-provided templates (env var) may contain shell syntax; auto-detected commands are safe for list mode.
            use_shell = bool(os.getenv(LOCAL_STT_COMMAND_ENV, "").strip())
            if use_shell:
                subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
            else:
                subprocess.run(shlex.split(command), check=True, capture_output=True, text=True)
            

            txt_files = sorted(Path(output_dir).glob("*.txt"))
            if not txt_files:
                return {
                    "success": False,
                    "transcript": "",
                    "error": "Local STT command completed but did not produce a .txt transcript",
                }

            transcript_text = txt_files[0].read_text(encoding="utf-8").strip()
            logger.info(
                "Transcribed %s via local STT command (%s, %d chars)",
                Path(file_path).name,
                normalized_model,
                len(transcript_text),
            )
            return {"success": True, "transcript": transcript_text, "provider": "local_command"}

    except KeyError as e:
        return {
            "success": False,
            "transcript": "",
            "error": f"Invalid {LOCAL_STT_COMMAND_ENV} template, missing placeholder: {e}",
        }
    except subprocess.CalledProcessError as e:
        details = e.stderr.strip() or e.stdout.strip() or str(e)
        logger.error("Local STT command failed for %s: %s", file_path, details)
        return {"success": False, "transcript": "", "error": f"Local STT failed: {details}"}
    except Exception as e:
        logger.error("Unexpected error during local command transcription: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Local transcription failed: {e}"}


# ---------------------------------------------------------------------------
# Provider: GigaAM (local Russian ASR)
# ---------------------------------------------------------------------------


def _first_config_or_env(
    config: Dict[str, Any],
    key: str,
    env_names: tuple[str, ...],
    default: Any = None,
) -> Any:
    if key in config:
        value = config.get(key)
        if value not in (None, ""):
            return value
    for env_name in env_names:
        value = get_env_value(env_name)
        if value not in (None, ""):
            return value
    return default


def _normalize_gigaam_model_name(model_name: Optional[str]) -> str:
    model_name = (model_name or "").strip() or DEFAULT_GIGAAM_STT_MODEL
    return GIGAAM_MODEL_ALIASES.get(model_name, model_name)


def _gigaam_chunk_sec(gigaam_config: Dict[str, Any]) -> float:
    raw = _first_config_or_env(gigaam_config, "chunk_sec", ("GIGAAM_CHUNK_SEC",), 20.0)
    try:
        chunk_sec = float(raw)
    except (TypeError, ValueError):
        chunk_sec = 20.0
    return min(24.0, max(5.0, chunk_sec))


def _wav_duration_sec(wav_path: str) -> float:
    try:
        with wave.open(wav_path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 0
            if rate <= 0:
                return 0.0
            return float(frames) / float(rate)
    except Exception:
        return 0.0


def _prepare_gigaam_audio(file_path: str, work_dir: str, ffmpeg_bin: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Convert input media to 16 kHz mono WAV for GigaAM."""
    audio_path = Path(file_path)
    if not ffmpeg_bin:
        if audio_path.suffix.lower() == ".wav":
            return file_path, None
        return None, "GigaAM STT requires ffmpeg for non-WAV inputs, but ffmpeg was not found"

    converted_path = os.path.join(work_dir, f"{audio_path.stem}.gigaam.wav")
    command = [
        ffmpeg_bin,
        "-y",
        "-i",
        file_path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        converted_path,
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return converted_path, None
    except subprocess.CalledProcessError as e:
        details = e.stderr.strip() or e.stdout.strip() or str(e)
        logger.error("ffmpeg conversion failed for GigaAM STT (%s): %s", file_path, details)
        return None, f"Failed to convert audio for GigaAM STT: {details}"


def _ffmpeg_wav_chunk(
    *,
    src_wav: str,
    dst_wav: str,
    start_sec: float,
    dur_sec: float,
    ffmpeg_bin: str,
) -> None:
    command = [
        ffmpeg_bin,
        "-y",
        "-ss",
        f"{max(0.0, float(start_sec)):.3f}",
        "-t",
        f"{max(0.0, float(dur_sec)):.3f}",
        "-i",
        src_wav,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        dst_wav,
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)


def _ensure_gigaam_available() -> None:
    try:
        from tools.lazy_deps import ensure
    except Exception:
        return
    ensure("stt.gigaam", prompt=False)


@lru_cache(maxsize=4)
def _load_gigaam_model(
    model_name: str,
    device: str,
    fp16_encoder: bool,
    use_flash: bool,
    download_root: Optional[str],
):
    _ensure_gigaam_available()
    from gigaam import load_model

    return load_model(
        model_name=model_name,
        device=device,
        fp16_encoder=fp16_encoder,
        use_flash=use_flash,
        download_root=download_root,
    )


def _load_gigaam_model_with_fallback(
    requested_model: str,
    device: str,
    fp16_encoder: bool,
    use_flash: bool,
    download_root: Optional[str],
) -> tuple[Any, str]:
    model_name = _normalize_gigaam_model_name(requested_model)
    try:
        model = _load_gigaam_model(model_name, device, fp16_encoder, use_flash, download_root)
        return model, model_name
    except Exception as exc:
        fallback_name = GIGAAM_FALLBACK_MODELS.get(model_name)
        if not fallback_name:
            raise
        logger.warning(
            "GigaAM model '%s' is unavailable in this install (%s). "
            "Falling back to '%s' without v3 punctuation.",
            model_name,
            exc,
            fallback_name,
        )
        model = _load_gigaam_model(fallback_name, device, fp16_encoder, use_flash, download_root)
        return model, fallback_name


def _gigaam_longform_segments(model: Any, wav_path: str) -> list[tuple[float, float, str]]:
    parts = model.transcribe_longform(wav_path)
    segments: list[tuple[float, float, str]] = []
    texts: list[str] = []
    for part in parts or []:
        text = str((part or {}).get("transcription") or "").strip()
        boundaries = (part or {}).get("boundaries")
        if text:
            texts.append(text)
        if isinstance(boundaries, (tuple, list)) and len(boundaries) == 2:
            try:
                start = float(boundaries[0])
                end = float(boundaries[1])
            except (TypeError, ValueError):
                start, end = 0.0, 0.0
            segments.append((start, end, text))
    if segments:
        return segments
    full_text = " ".join(texts).strip()
    return [(0.0, _wav_duration_sec(wav_path), full_text)]


def _gigaam_chunked_segments(
    model: Any,
    wav_path: str,
    *,
    duration: float,
    chunk_sec: float,
    ffmpeg_bin: str,
) -> list[tuple[float, float, str]]:
    segments: list[tuple[float, float, str]] = []
    with tempfile.TemporaryDirectory(prefix="hermes-gigaam-chunks-") as chunk_dir:
        t = 0.0
        idx = 0
        while t < max(duration, 0.001):
            part_duration = min(chunk_sec, max(0.0, duration - t))
            if part_duration <= 0.01:
                break
            chunk_path = os.path.join(chunk_dir, f"chunk_{idx:04d}.wav")
            _ffmpeg_wav_chunk(
                src_wav=wav_path,
                dst_wav=chunk_path,
                start_sec=t,
                dur_sec=part_duration,
                ffmpeg_bin=ffmpeg_bin,
            )
            text = str(model.transcribe(chunk_path) or "").strip()
            segments.append((t, min(duration, t + part_duration), text))
            t += part_duration
            idx += 1
    return segments


def _segments_text(segments: list[tuple[float, float, str]]) -> str:
    return " ".join(text.strip() for _start, _end, text in segments if text and text.strip()).strip()


def _transcribe_gigaam(file_path: str, model_name: str) -> Dict[str, Any]:
    """Transcribe using local GigaAM.

    The implementation follows the local Content Agent transcriber: normalize
    inputs to 16 kHz mono WAV, prefer the requested v3 model, fall back to v2
    when the installed package does not expose v3 weights, then use long-form
    or short chunked transcription for audio longer than GigaAM's native limit.
    """
    stt_config = _load_stt_config()
    gigaam_config = stt_config.get("gigaam", {}) if isinstance(stt_config.get("gigaam"), dict) else {}
    device = str(_first_config_or_env(gigaam_config, "device", ("GIGAAM_DEVICE", "DEVICE"), "cpu")).strip() or "cpu"
    hf_token = _first_config_or_env(gigaam_config, "hf_token", ("HF_TOKEN",), None)
    hf_token = str(hf_token).strip() if hf_token else None
    fp16_encoder = is_truthy_value(
        _first_config_or_env(gigaam_config, "fp16_encoder", ("GIGAAM_FP16_ENCODER",), True),
        default=True,
    )
    use_flash = is_truthy_value(
        _first_config_or_env(gigaam_config, "use_flash", ("GIGAAM_USE_FLASH",), False),
        default=False,
    )
    download_root = _first_config_or_env(gigaam_config, "download_root", ("GIGAAM_DOWNLOAD_ROOT",), None)
    download_root = str(download_root).strip() if download_root else None
    fallback_chunking = is_truthy_value(
        _first_config_or_env(gigaam_config, "fallback_chunking", ("GIGAAM_FALLBACK_CHUNKING",), False),
        default=False,
    )
    ffmpeg_bin = str(
        _first_config_or_env(gigaam_config, "ffmpeg_bin", ("GIGAAM_FFMPEG_BIN", "FFMPEG_BIN"), "")
        or _find_ffmpeg_binary()
        or ""
    ).strip()

    if hf_token:
        os.environ.setdefault("HF_TOKEN", hf_token)

    try:
        with tempfile.TemporaryDirectory(prefix="hermes-gigaam-stt-") as work_dir:
            wav_path, prep_error = _prepare_gigaam_audio(file_path, work_dir, ffmpeg_bin or None)
            if prep_error:
                return {"success": False, "transcript": "", "error": prep_error}

            model, resolved_model = _load_gigaam_model_with_fallback(
                model_name,
                device,
                fp16_encoder,
                use_flash,
                download_root,
            )
            duration = _wav_duration_sec(wav_path)
            started = time.monotonic()

            try:
                text = str(model.transcribe(wav_path) or "").strip()
                segments = [(0.0, duration, text)]
                mode = "short"
            except ValueError as exc:
                if "Too long wav file" not in str(exc):
                    raise
                if hf_token:
                    try:
                        segments = _gigaam_longform_segments(model, wav_path)
                        mode = "longform"
                    except Exception as longform_exc:
                        if not fallback_chunking:
                            raise RuntimeError(
                                "GigaAM long-form failed. Check HF_TOKEN access to "
                                "pyannote/voice-activity-detection and pyannote/segmentation, "
                                "install gigaam[longform], or enable stt.gigaam.fallback_chunking."
                            ) from longform_exc
                        logger.warning("GigaAM long-form failed, falling back to chunking: %s", longform_exc)
                        if not ffmpeg_bin:
                            return {
                                "success": False,
                                "transcript": "",
                                "error": "GigaAM chunking fallback requires ffmpeg, but ffmpeg was not found",
                            }
                        chunk_sec = _gigaam_chunk_sec(gigaam_config)
                        segments = _gigaam_chunked_segments(
                            model,
                            wav_path,
                            duration=duration,
                            chunk_sec=chunk_sec,
                            ffmpeg_bin=ffmpeg_bin,
                        )
                        mode = "chunking"
                else:
                    if not ffmpeg_bin:
                        return {
                            "success": False,
                            "transcript": "",
                            "error": "GigaAM long audio requires ffmpeg for chunking, but ffmpeg was not found",
                        }
                    chunk_sec = _gigaam_chunk_sec(gigaam_config)
                    segments = _gigaam_chunked_segments(
                        model,
                        wav_path,
                        duration=duration,
                        chunk_sec=chunk_sec,
                        ffmpeg_bin=ffmpeg_bin,
                    )
                    mode = "chunking"

            transcript_text = _segments_text(segments)
            logger.info(
                "Transcribed %s via GigaAM (%s, mode=%s, %.1fs audio, %.1fs wall, %d chars)",
                Path(file_path).name,
                resolved_model,
                mode,
                duration,
                time.monotonic() - started,
                len(transcript_text),
            )
            return {"success": True, "transcript": transcript_text, "provider": "gigaam"}

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except Exception as e:
        logger.error("GigaAM transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"GigaAM transcription failed: {e}"}

# ---------------------------------------------------------------------------
# Provider: groq (Whisper API — free tier)
# ---------------------------------------------------------------------------


def _groq_upload_limit_bytes(groq_config: Dict[str, Any]) -> int:
    raw_mb = _first_config_or_env(
        groq_config,
        "max_upload_mb",
        ("STT_GROQ_MAX_UPLOAD_MB",),
        24,
    )
    try:
        mb = float(raw_mb)
    except (TypeError, ValueError):
        mb = 24.0
    return int(max(1.0, mb) * 1024 * 1024)


def _groq_chunk_sec(groq_config: Dict[str, Any]) -> int:
    raw = _first_config_or_env(
        groq_config,
        "chunk_sec",
        ("STT_GROQ_CHUNK_SEC",),
        DEFAULT_GROQ_CHUNK_SECONDS,
    )
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        seconds = DEFAULT_GROQ_CHUNK_SECONDS
    return max(60, min(1800, seconds))


def _groq_chunk_bitrate(groq_config: Dict[str, Any]) -> str:
    raw = str(
        _first_config_or_env(
            groq_config,
            "chunk_bitrate",
            ("STT_GROQ_CHUNK_BITRATE",),
            DEFAULT_GROQ_CHUNK_BITRATE,
        )
        or ""
    ).strip().lower()
    if raw and raw[:-1].isdigit() and raw[-1] in {"k", "m"}:
        return raw
    return DEFAULT_GROQ_CHUNK_BITRATE


def _groq_language(groq_config: Dict[str, Any]) -> str:
    value = _first_config_or_env(
        groq_config,
        "language",
        ("STT_GROQ_LANGUAGE", LOCAL_STT_LANGUAGE_ENV),
        "",
    )
    return str(value or "").strip()


def _groq_rate_limit_max_wait_seconds(groq_config: Dict[str, Any]) -> float:
    raw = _first_config_or_env(
        groq_config,
        "rate_limit_max_wait_sec",
        ("STT_GROQ_RATE_LIMIT_MAX_WAIT_SEC",),
        DEFAULT_GROQ_RATE_LIMIT_MAX_WAIT_SECONDS,
    )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_GROQ_RATE_LIMIT_MAX_WAIT_SECONDS)


def _groq_rate_limit_fallback_wait_seconds(groq_config: Dict[str, Any]) -> float:
    raw = _first_config_or_env(
        groq_config,
        "rate_limit_fallback_wait_sec",
        ("STT_GROQ_RATE_LIMIT_FALLBACK_WAIT_SEC",),
        DEFAULT_GROQ_RATE_LIMIT_FALLBACK_WAIT_SECONDS,
    )
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_GROQ_RATE_LIMIT_FALLBACK_WAIT_SECONDS)


def _headers_from_error(exc: BaseException) -> Mapping[str, Any]:
    headers = getattr(exc, "headers", None)
    response = getattr(exc, "response", None)
    if headers is None and response is not None:
        headers = getattr(response, "headers", None)
    if headers is None:
        return {}
    return headers


def _header_value(headers: Mapping[str, Any], name: str) -> Optional[str]:
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is None:
            value = getter(name.lower())
        if value is None:
            value = getter(name.upper())
        if value is not None:
            return str(value)
    lowered = {str(k).lower(): v for k, v in dict(headers).items()}
    value = lowered.get(name.lower())
    return None if value is None else str(value)


def _parse_wait_seconds(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        seconds = float(text)
        return seconds if seconds > 0 else None
    except ValueError:
        pass

    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError, OSError):
        pass

    total = 0.0
    matched = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(ms|s|sec|secs|m|min|mins|h|hr|hrs)", text.lower()):
        matched = True
        amount = float(value)
        if unit == "ms":
            total += amount / 1000.0
        elif unit in {"s", "sec", "secs"}:
            total += amount
        elif unit in {"m", "min", "mins"}:
            total += amount * 60.0
        elif unit in {"h", "hr", "hrs"}:
            total += amount * 3600.0
    if matched and total > 0:
        return total
    return None


def _is_groq_rate_limit_error(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    return (
        "ratelimit" in name
        or "rate limit" in text
        or "rate_limit" in text
        or "too many requests" in text
        or "quota exceeded" in text
    )


def _groq_rate_limit_wait_seconds(
    exc: BaseException,
    groq_config: Dict[str, Any],
    attempt: int,
) -> float:
    headers = _headers_from_error(exc)

    retry_after = _parse_wait_seconds(_header_value(headers, "retry-after"))
    if retry_after is not None:
        return max(1.0, retry_after + 1.0)

    reset_values = []
    for key, value in dict(headers).items():
        lowered = str(key).lower()
        if lowered.startswith("x-ratelimit-reset"):
            parsed = _parse_wait_seconds(value)
            if parsed is not None:
                reset_values.append(parsed)
    if reset_values:
        return max(1.0, max(reset_values) + 1.0)

    fallback = _groq_rate_limit_fallback_wait_seconds(groq_config)
    return min(max(1.0, fallback * (2 ** max(0, attempt - 1))), 15 * 60.0)


def _groq_rate_limit_sleep(seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            from tools.interrupt import is_interrupted
            if is_interrupted():
                raise RuntimeError("Groq STT rate-limit wait interrupted")
        except ImportError:
            pass
        time.sleep(min(30.0, remaining))


def _groq_create_transcription_with_rate_limit_wait(
    *,
    client: Any,
    kwargs: Dict[str, Any],
    groq_config: Dict[str, Any],
    chunk_index: int,
    chunk_total: int,
    source_name: str,
) -> Any:
    started = time.monotonic()
    attempt = 0
    max_wait = _groq_rate_limit_max_wait_seconds(groq_config)
    while True:
        attempt += 1
        try:
            return client.audio.transcriptions.create(**kwargs)
        except Exception as exc:
            if not _is_groq_rate_limit_error(exc):
                raise
            wait_seconds = _groq_rate_limit_wait_seconds(exc, groq_config, attempt)
            elapsed = time.monotonic() - started
            if max_wait and elapsed + wait_seconds > max_wait:
                raise RuntimeError(
                    "Groq STT rate limit did not recover within "
                    f"{max_wait:.0f}s while transcribing {source_name}"
                ) from exc
            logger.warning(
                "Groq STT rate-limited on chunk %d/%d for %s; waiting %.0fs before retry",
                chunk_index,
                chunk_total,
                source_name,
                wait_seconds,
            )
            _groq_rate_limit_sleep(wait_seconds)


def _prepare_groq_audio_chunks(
    file_path: str,
    work_dir: str,
    groq_config: Dict[str, Any],
) -> tuple[list[str], Optional[str]]:
    """Return one or more uploadable files for Groq STT."""
    source = Path(file_path)
    limit = _groq_upload_limit_bytes(groq_config)
    try:
        if source.stat().st_size <= limit:
            return [file_path], None
    except OSError as exc:
        return [], f"Failed to access file: {exc}"

    ffmpeg_bin = str(
        _first_config_or_env(groq_config, "ffmpeg_bin", ("STT_GROQ_FFMPEG_BIN", "FFMPEG_BIN"), "")
        or _find_ffmpeg_binary()
        or ""
    )
    if not ffmpeg_bin:
        return [], (
            "Groq STT requires ffmpeg to chunk files larger than "
            f"{limit / (1024 * 1024):.0f}MB"
        )

    chunk_sec = _groq_chunk_sec(groq_config)
    bitrate = _groq_chunk_bitrate(groq_config)
    last_error = ""

    while chunk_sec >= 60:
        for old in Path(work_dir).glob("groq_chunk_*.mp3"):
            try:
                old.unlink()
            except OSError:
                pass

        pattern = os.path.join(work_dir, "groq_chunk_%04d.mp3")
        command = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            file_path,
            "-vn",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            bitrate,
            "-f",
            "segment",
            "-segment_time",
            str(chunk_sec),
            "-reset_timestamps",
            "1",
            pattern,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            details = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            return [], f"Failed to chunk audio for Groq STT: {details}"

        chunks = sorted(str(path) for path in Path(work_dir).glob("groq_chunk_*.mp3"))
        if not chunks:
            return [], "Failed to chunk audio for Groq STT: ffmpeg produced no chunks"

        oversized = []
        for chunk in chunks:
            try:
                if Path(chunk).stat().st_size > limit:
                    oversized.append(chunk)
            except OSError as exc:
                return [], f"Failed to access Groq STT chunk: {exc}"
        if not oversized:
            logger.info(
                "Prepared %d Groq STT chunk(s) for %s (segment=%ss, bitrate=%s)",
                len(chunks),
                source.name,
                chunk_sec,
                bitrate,
            )
            return chunks, None

        last_error = (
            f"{len(oversized)} Groq STT chunk(s) still exceeded "
            f"{limit / (1024 * 1024):.0f}MB at {chunk_sec}s"
        )
        chunk_sec //= 2

    return [], last_error or "Failed to create Groq STT chunks under upload limit"


def _groq_transcription_create_kwargs(
    model_name: str,
    audio_file: Any,
    groq_config: Dict[str, Any],
) -> Dict[str, Any]:
    kwargs = {
        "model": model_name,
        "file": audio_file,
        "response_format": "text",
    }
    language = _groq_language(groq_config)
    if language:
        kwargs["language"] = language
    return kwargs


def _transcribe_groq(file_path: str, model_name: str) -> Dict[str, Any]:
    """Transcribe using Groq Whisper API (free tier available)."""
    api_key = get_env_value("GROQ_API_KEY")
    if not api_key:
        return {"success": False, "transcript": "", "error": "GROQ_API_KEY not set"}

    if not _HAS_OPENAI:
        return {"success": False, "transcript": "", "error": "openai package not installed"}

    # Auto-correct model if caller passed an OpenAI-only model
    if model_name in OPENAI_MODELS:
        logger.info("Model %s not available on Groq, using %s", model_name, DEFAULT_GROQ_STT_MODEL)
        model_name = DEFAULT_GROQ_STT_MODEL

    try:
        from openai import OpenAI, APIError, APIConnectionError, APITimeoutError
        stt_config = _load_stt_config()
        groq_config = stt_config.get("groq", {})
        if not isinstance(groq_config, dict):
            groq_config = {}
        client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL, timeout=30, max_retries=0)
        try:
            with tempfile.TemporaryDirectory(prefix="hermes-groq-stt-") as work_dir:
                upload_paths, prep_error = _prepare_groq_audio_chunks(
                    file_path,
                    work_dir,
                    groq_config,
                )
                if prep_error:
                    return {"success": False, "transcript": "", "error": prep_error}

                transcript_parts: list[str] = []
                for index, upload_path in enumerate(upload_paths):
                    with open(upload_path, "rb") as audio_file:
                        transcription = _groq_create_transcription_with_rate_limit_wait(
                            client=client,
                            kwargs=_groq_transcription_create_kwargs(
                                model_name,
                                audio_file,
                                groq_config,
                            ),
                            groq_config=groq_config,
                            chunk_index=index + 1,
                            chunk_total=len(upload_paths),
                            source_name=Path(file_path).name,
                        )
                    part_text = str(transcription).strip()
                    if part_text:
                        transcript_parts.append(part_text)
                    logger.info(
                        "Groq STT chunk %d/%d complete for %s (%d chars)",
                        index + 1,
                        len(upload_paths),
                        Path(file_path).name,
                        len(part_text),
                    )

            transcript_text = "\n\n".join(transcript_parts).strip()
            logger.info(
                "Transcribed %s via Groq API (%s, chunks=%d, %d chars)",
                Path(file_path).name,
                model_name,
                len(upload_paths),
                len(transcript_text),
            )

            return {"success": True, "transcript": transcript_text, "provider": "groq"}
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except APIConnectionError as e:
        return {"success": False, "transcript": "", "error": f"Connection error: {e}"}
    except APITimeoutError as e:
        return {"success": False, "transcript": "", "error": f"Request timeout: {e}"}
    except APIError as e:
        return {"success": False, "transcript": "", "error": f"API error: {e}"}
    except Exception as e:
        logger.error("Groq transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Transcription failed: {e}"}


# ---------------------------------------------------------------------------
# Provider: ideal_rus (GigaAM + Groq + LLM merge)
# ---------------------------------------------------------------------------


def _ideal_rus_merge_messages(gigaam_text: str, groq_text: str) -> list[dict[str, str]]:
    system = (
        "You are an expert Russian transcription editor. Create one clean, "
        "faithful transcript from two ASR drafts of the same audio. Preserve "
        "the speaker's meaning, order, names, numbers, and terminology. "
        "Prefer GigaAM for Russian word forms and endings. Prefer Groq/Whisper "
        "for English words, mixed Russian/English phrases, names, product "
        "terms, acronyms, and punctuation when it is clearly better. Remove "
        "ASR artifacts, repeated fragments, hallucinated filler, and obvious "
        "recognition errors. Return only the final transcript, not a summary."
    )
    user = (
        "GigaAM draft:\n"
        f"{gigaam_text.strip()}\n\n"
        "Groq Whisper draft:\n"
        f"{groq_text.strip()}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return None
    return coerced if coerced > 0 else None


def _merge_ideal_rus_transcripts(
    gigaam_text: str,
    groq_text: str,
    ideal_config: Dict[str, Any],
) -> tuple[str, Dict[str, Any]]:
    gigaam_text = (gigaam_text or "").strip()
    groq_text = (groq_text or "").strip()
    if not gigaam_text:
        return groq_text, {"used": False, "reason": "gigaam_empty"}
    if not groq_text:
        return gigaam_text, {"used": False, "reason": "groq_empty"}
    if not is_truthy_value(ideal_config.get("merge", True), default=True):
        return (
            f"GigaAM:\n{gigaam_text}\n\nGroq Whisper:\n{groq_text}",
            {"used": False, "reason": "merge_disabled"},
        )

    task = str(ideal_config.get("merge_task") or DEFAULT_IDEAL_RUS_MERGE_TASK).strip()
    provider = str(ideal_config.get("merge_provider") or "").strip() or None
    model = str(ideal_config.get("merge_model") or "").strip() or None
    base_url = str(ideal_config.get("merge_base_url") or "").strip() or None
    api_key = str(ideal_config.get("merge_api_key") or "").strip() or None
    timeout_raw = ideal_config.get("merge_timeout")
    timeout = None
    if timeout_raw not in (None, ""):
        try:
            timeout = float(timeout_raw)
        except (TypeError, ValueError):
            timeout = None

    try:
        from agent.auxiliary_client import call_llm, extract_content_or_reasoning

        response = call_llm(
            task=task,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            messages=_ideal_rus_merge_messages(gigaam_text, groq_text),
            temperature=0,
            max_tokens=_coerce_optional_int(ideal_config.get("merge_max_tokens")),
            timeout=timeout,
            usage_source="stt.ideal_rus",
        )
        merged = extract_content_or_reasoning(response).strip()
        if merged:
            return merged, {
                "used": True,
                "task": task,
                "provider": provider or "auto",
                "model": model or "",
            }
        return (
            f"GigaAM:\n{gigaam_text}\n\nGroq Whisper:\n{groq_text}",
            {"used": False, "error": "LLM merge returned empty text", "task": task},
        )
    except Exception as exc:
        logger.warning("Ideal Russian STT LLM merge failed: %s", exc, exc_info=True)
        return (
            f"GigaAM:\n{gigaam_text}\n\nGroq Whisper:\n{groq_text}",
            {"used": False, "error": str(exc), "task": task},
        )


def _transcribe_ideal_rus(file_path: str, model_name: Optional[str] = None) -> Dict[str, Any]:
    """Transcribe Russian audio with GigaAM and Groq, then merge both drafts."""
    stt_config = _load_stt_config()
    ideal_config = stt_config.get("ideal_rus", {})
    if not isinstance(ideal_config, dict):
        ideal_config = {}
    gigaam_config = stt_config.get("gigaam", {})
    if not isinstance(gigaam_config, dict):
        gigaam_config = {}
    groq_config = stt_config.get("groq", {})
    if not isinstance(groq_config, dict):
        groq_config = {}

    gigaam_model = _normalize_gigaam_model_name(
        ideal_config.get("gigaam_model")
        or gigaam_config.get("model")
        or get_env_value("GIGAAM_MODEL")
        or DEFAULT_GIGAAM_STT_MODEL
    )
    groq_model = (
        model_name
        or ideal_config.get("groq_model")
        or groq_config.get("model")
        or DEFAULT_GROQ_STT_MODEL
    )

    started = time.monotonic()
    gigaam_result = _transcribe_gigaam(file_path, gigaam_model)
    groq_result = _transcribe_groq(file_path, groq_model)

    gigaam_text = str(gigaam_result.get("transcript") or "").strip() if gigaam_result.get("success") else ""
    groq_text = str(groq_result.get("transcript") or "").strip() if groq_result.get("success") else ""

    if not gigaam_text and not groq_text:
        error_parts = []
        if gigaam_result.get("error"):
            error_parts.append(f"GigaAM: {gigaam_result['error']}")
        if groq_result.get("error"):
            error_parts.append(f"Groq: {groq_result['error']}")
        return {
            "success": False,
            "transcript": "",
            "provider": "ideal_rus",
            "error": "; ".join(error_parts) or "Both GigaAM and Groq returned empty transcripts",
            "gigaam": gigaam_result,
            "groq": groq_result,
        }

    transcript, merge_info = _merge_ideal_rus_transcripts(gigaam_text, groq_text, ideal_config)
    logger.info(
        "Transcribed %s via ideal_rus (gigaam=%s, groq=%s, merge=%s, %.1fs wall, %d chars)",
        Path(file_path).name,
        bool(gigaam_text),
        bool(groq_text),
        merge_info.get("used"),
        time.monotonic() - started,
        len(transcript),
    )
    return {
        "success": True,
        "transcript": transcript,
        "provider": "ideal_rus",
        "gigaam": gigaam_result,
        "groq": groq_result,
        "merge": merge_info,
    }

# ---------------------------------------------------------------------------
# Provider: openai (Whisper API)
# ---------------------------------------------------------------------------


def _transcribe_openai(file_path: str, model_name: str) -> Dict[str, Any]:
    """Transcribe using OpenAI Whisper API (paid)."""
    try:
        api_key, base_url = _resolve_openai_audio_client_config()
    except ValueError as exc:
        return {
            "success": False,
            "transcript": "",
            "error": str(exc),
        }

    if not _HAS_OPENAI:
        return {"success": False, "transcript": "", "error": "openai package not installed"}

    # Auto-correct model if caller passed a Groq-only model
    if model_name in GROQ_MODELS:
        logger.info("Model %s not available on OpenAI, using %s", model_name, DEFAULT_STT_MODEL)
        model_name = DEFAULT_STT_MODEL

    try:
        from openai import OpenAI, APIError, APIConnectionError, APITimeoutError
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=30, max_retries=0)
        try:
            with open(file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    model=model_name,
                    file=audio_file,
                    response_format="text" if model_name == "whisper-1" else "json",
                )

            transcript_text = _extract_transcript_text(transcription)
            logger.info("Transcribed %s via OpenAI API (%s, %d chars)",
                         Path(file_path).name, model_name, len(transcript_text))

            return {"success": True, "transcript": transcript_text, "provider": "openai"}
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except APIConnectionError as e:
        return {"success": False, "transcript": "", "error": f"Connection error: {e}"}
    except APITimeoutError as e:
        return {"success": False, "transcript": "", "error": f"Request timeout: {e}"}
    except APIError as e:
        return {"success": False, "transcript": "", "error": f"API error: {e}"}
    except Exception as e:
        logger.error("OpenAI transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Transcription failed: {e}"}

# ---------------------------------------------------------------------------
# Provider: mistral (Voxtral Transcribe API)
# ---------------------------------------------------------------------------


def _transcribe_mistral(file_path: str, model_name: str) -> Dict[str, Any]:
    """Transcribe using Mistral Voxtral Transcribe API.

    Uses the ``mistralai`` Python SDK to call ``/v1/audio/transcriptions``.
    Requires ``MISTRAL_API_KEY`` environment variable.
    """
    api_key = get_env_value("MISTRAL_API_KEY")
    if not api_key:
        return {"success": False, "transcript": "", "error": "MISTRAL_API_KEY not set"}

    try:
        from mistralai.client import Mistral

        with Mistral(api_key=api_key) as client:
            with open(file_path, "rb") as audio_file:
                result = client.audio.transcriptions.complete(
                    model=model_name,
                    file={"content": audio_file, "file_name": Path(file_path).name},
                )

            transcript_text = _extract_transcript_text(result)
            logger.info(
                "Transcribed %s via Mistral API (%s, %d chars)",
                Path(file_path).name, model_name, len(transcript_text),
            )
            return {"success": True, "transcript": transcript_text, "provider": "mistral"}

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except Exception as e:
        logger.error("Mistral transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Mistral transcription failed: {type(e).__name__}"}


# ---------------------------------------------------------------------------
# Provider: xAI (Grok STT API)
# ---------------------------------------------------------------------------


def _transcribe_xai(file_path: str, model_name: str) -> Dict[str, Any]:
    """Transcribe using xAI Grok STT API.

    Uses the ``POST /v1/stt`` REST endpoint with multipart/form-data.
    Supports Inverse Text Normalization, diarization, and word-level timestamps.
    Requires ``XAI_API_KEY`` environment variable.
    """
    from tools.xai_http import resolve_xai_http_credentials

    creds = resolve_xai_http_credentials()
    api_key = str(creds.get("api_key") or "").strip()
    if not api_key:
        return {
            "success": False,
            "transcript": "",
            "error": "No xAI credentials found. Configure xAI OAuth in `hermes model` or set XAI_API_KEY",
        }

    stt_config = _load_stt_config()
    xai_config = stt_config.get("xai", {})
    base_url = str(
        xai_config.get("base_url")
        or get_env_value("XAI_STT_BASE_URL")
        or creds.get("base_url")
        or XAI_STT_BASE_URL
    ).strip().rstrip("/")
    language = str(
        xai_config.get("language")
        or os.getenv("HERMES_LOCAL_STT_LANGUAGE")
        or DEFAULT_LOCAL_STT_LANGUAGE
    ).strip()
    # .get("format", True) already defaults to True when the key is absent;
    # is_truthy_value only normalizes truthy/falsy strings from config.
    use_format = is_truthy_value(xai_config.get("format", True))
    use_diarize = is_truthy_value(xai_config.get("diarize", False))

    try:
        import requests
        from tools.xai_http import hermes_xai_user_agent

        data: Dict[str, str] = {}
        if language:
            data["language"] = language
        if use_format:
            data["format"] = "true"
        if use_diarize:
            data["diarize"] = "true"

        with open(file_path, "rb") as audio_file:
            response = requests.post(
                f"{base_url}/stt",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": hermes_xai_user_agent(),
                },
                files={
                    "file": (Path(file_path).name, audio_file),
                },
                data=data,
                timeout=120,
            )

        if response.status_code != 200:
            detail = ""
            try:
                err_body = response.json()
                detail = err_body.get("error", {}).get("message", "") or response.text[:300]
            except Exception:
                detail = response.text[:300]
            return {
                "success": False,
                "transcript": "",
                "error": f"xAI STT API error (HTTP {response.status_code}): {detail}",
            }

        result = response.json()
        transcript_text = result.get("text", "").strip()

        if not transcript_text:
            return {
                "success": False,
                "transcript": "",
                "error": "xAI STT returned empty transcript",
            }

        logger.info(
            "Transcribed %s via xAI Grok STT (lang=%s, %.1fs audio, %d chars)",
            Path(file_path).name,
            result.get("language", language),
            result.get("duration", 0),
            len(transcript_text),
        )

        return {"success": True, "transcript": transcript_text, "provider": "xai"}

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except Exception as e:
        logger.error("xAI STT transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"xAI STT transcription failed: {e}"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def transcribe_audio(file_path: str, model: Optional[str] = None) -> Dict[str, Any]:
    """
    Transcribe an audio file using the configured STT provider.

    Provider priority:
      1. User config (``stt.provider`` in config.yaml)
      2. Auto-detect: local faster-whisper (free) > Groq (free tier) > OpenAI (paid) > xAI

    Args:
        file_path: Absolute path to the audio file to transcribe.
        model:     Override the model. If None, uses config or provider default.

    Returns:
        dict with keys:
          - "success" (bool): Whether transcription succeeded
          - "transcript" (str): The transcribed text (empty on failure)
          - "error" (str, optional): Error message if success is False
          - "provider" (str, optional): Which provider was used
    """
    # Validate basic path/format first. Cloud size limits are provider-specific,
    # so defer the 25 MB check until after provider resolution.
    error = _validate_audio_file(file_path, enforce_size_limit=False)
    if error:
        return error

    # Load config and determine provider
    stt_config = _load_stt_config()
    if not is_stt_enabled(stt_config):
        return {
            "success": False,
            "transcript": "",
            "error": "STT is disabled in config.yaml (stt.enabled: false).",
        }

    provider = _get_provider(stt_config)
    if provider not in PROVIDERS_WITH_INTERNAL_SIZE_HANDLING:
        error = _validate_audio_file(file_path)
        if error:
            return error

    if provider == "local":
        local_cfg = stt_config.get("local", {})
        model_name = _normalize_local_model(
            model or local_cfg.get("model", DEFAULT_LOCAL_MODEL)
        )
        return _transcribe_local(file_path, model_name)

    if provider == "local_command":
        local_cfg = stt_config.get("local", {})
        model_name = _normalize_local_command_model(
            model or local_cfg.get("model", DEFAULT_LOCAL_MODEL)
        )
        return _transcribe_local_command(file_path, model_name)

    if provider == "gigaam":
        gigaam_cfg = stt_config.get("gigaam", {})
        if not isinstance(gigaam_cfg, dict):
            gigaam_cfg = {}
        model_name = _normalize_gigaam_model_name(
            model
            or gigaam_cfg.get("model")
            or get_env_value("GIGAAM_MODEL")
            or DEFAULT_GIGAAM_STT_MODEL
        )
        return _transcribe_gigaam(file_path, model_name)

    if provider == "ideal_rus":
        ideal_cfg = stt_config.get("ideal_rus", {})
        if not isinstance(ideal_cfg, dict):
            ideal_cfg = {}
        groq_cfg = stt_config.get("groq", {})
        if not isinstance(groq_cfg, dict):
            groq_cfg = {}
        model_name = model or ideal_cfg.get("groq_model") or groq_cfg.get("model") or DEFAULT_GROQ_STT_MODEL
        return _transcribe_ideal_rus(file_path, model_name)

    if provider == "groq":
        groq_cfg = stt_config.get("groq", {})
        if not isinstance(groq_cfg, dict):
            groq_cfg = {}
        model_name = model or groq_cfg.get("model") or DEFAULT_GROQ_STT_MODEL
        return _transcribe_groq(file_path, model_name)

    if provider == "openai":
        openai_cfg = stt_config.get("openai", {})
        model_name = model or openai_cfg.get("model", DEFAULT_STT_MODEL)
        return _transcribe_openai(file_path, model_name)

    if provider == "mistral":
        mistral_cfg = stt_config.get("mistral", {})
        model_name = model or mistral_cfg.get("model", DEFAULT_MISTRAL_STT_MODEL)
        return _transcribe_mistral(file_path, model_name)

    if provider == "xai":
        # xAI Grok STT doesn't use a model parameter — pass through for logging
        model_name = model or "grok-stt"
        return _transcribe_xai(file_path, model_name)

    # No provider available
    return {
        "success": False,
        "transcript": "",
        "error": (
            "No STT provider available. Install faster-whisper for free local "
            f"transcription, configure {LOCAL_STT_COMMAND_ENV} or install a local whisper CLI, "
            "set stt.provider: ideal_rus for GigaAM + Groq + LLM Russian transcription, "
            "set stt.provider: gigaam for local Russian GigaAM, set GROQ_API_KEY for free "
            "Groq Whisper, set MISTRAL_API_KEY for Mistral Voxtral Transcribe, configure xAI OAuth "
            "or set XAI_API_KEY for xAI Grok STT, or set VOICE_TOOLS_OPENAI_KEY or OPENAI_API_KEY "
            "for the OpenAI Whisper API."
        ),
    }


def _resolve_openai_audio_client_config() -> tuple[str, str]:
    """Return direct OpenAI audio config or a managed gateway fallback."""
    stt_config = _load_stt_config()
    openai_cfg = stt_config.get("openai", {})
    cfg_api_key = openai_cfg.get("api_key", "")
    cfg_base_url = openai_cfg.get("base_url", "")
    if cfg_api_key:
        return cfg_api_key, (cfg_base_url or OPENAI_BASE_URL)

    direct_api_key = resolve_openai_audio_api_key()
    if direct_api_key:
        return direct_api_key, OPENAI_BASE_URL

    managed_gateway = resolve_managed_tool_gateway("openai-audio")
    if managed_gateway is None:
        message = "Neither stt.openai.api_key in config nor VOICE_TOOLS_OPENAI_KEY/OPENAI_API_KEY is set"
        if managed_nous_tools_enabled():
            message += ", and the managed OpenAI audio gateway is unavailable"
        raise ValueError(message)

    return managed_gateway.nous_user_token, urljoin(
        f"{managed_gateway.gateway_origin.rstrip('/')}/", "v1"
    )


def _extract_transcript_text(transcription: Any) -> str:
    """Normalize text and JSON transcription responses to a plain string."""
    if isinstance(transcription, str):
        return transcription.strip()

    if hasattr(transcription, "text"):
        value = getattr(transcription, "text")
        if isinstance(value, str):
            return value.strip()

    if isinstance(transcription, dict):
        value = transcription.get("text")
        if isinstance(value, str):
            return value.strip()

    return str(transcription).strip()
