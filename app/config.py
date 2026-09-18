"""Service configuration loaded from environment variables.

Every getter reads os.environ on each call so tests can monkeypatch env
vars without reloading modules.
"""
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def media_tools_token() -> str | None:
    return os.environ.get("MEDIA_TOOLS_TOKEN") or None


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def nan_base_url() -> str:
    return os.environ.get("NAN_BASE_URL", "https://api.nan.builders/v1")


def nan_api_key() -> str | None:
    return os.environ.get("NAN_API_KEY") or None


def nan_model() -> str:
    return os.environ.get("NAN_MODEL", "whisper")


def transcription_provider() -> str:
    return os.environ.get("TRANSCRIPTION_PROVIDER", "nan").strip().lower()


def groq_api_key() -> str | None:
    return os.environ.get("GROQ_API_KEY") or None


def groq_model() -> str:
    return os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")


def timeout_seconds() -> int:
    return int(os.environ.get("TIMEOUT_SECONDS", "1800"))


def worker_mode() -> str:
    """"sync" runs jobs inline (used by tests); anything else uses thread pools."""
    return os.environ.get("WORKER_MODE", "async")


def ytdlp_autoupdate() -> bool:
    return os.environ.get("YTDLP_AUTOUPDATE", "0") == "1"


def ytdlp_js_runtime() -> str:
    return os.environ.get("YTDLP_JS_RUNTIME", "deno")


def ytdlp_options() -> dict[str, Any]:
    """Return yt-dlp Python API options from the environment."""
    options: dict[str, Any] = {"js_runtimes": {ytdlp_js_runtime(): {}}}
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
    proxy = os.environ.get("YTDLP_PROXY")
    if cookies_file:
        options["cookiefile"] = cookies_file
    if proxy:
        options["proxy"] = proxy
    return options


def ytdlp_cli_args() -> list[str]:
    """Return yt-dlp CLI arguments without exposing secret values."""
    args: list[str] = ["--js-runtimes", ytdlp_js_runtime()]
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
    proxy = os.environ.get("YTDLP_PROXY")
    if cookies_file:
        args.extend(["--cookies", cookies_file])
    if proxy:
        args.extend(["--proxy", proxy])
    return args
