"""Service configuration loaded from environment variables.

Every getter reads os.environ on each call so tests can monkeypatch env
vars without reloading modules.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def media_tools_token() -> str | None:
    return os.environ.get("MEDIA_TOOLS_TOKEN") or None


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def speaches_base_url() -> str:
    return os.environ.get("SPEACHES_BASE_URL", "http://localhost:8000")


def speaches_endpoint() -> str:
    return os.environ.get("SPEACHES_ENDPOINT", "/v1/audio/transcriptions")


def speaches_mode() -> str:
    return os.environ.get("SPEACHES_MODE", "openai")


def speaches_model() -> str:
    return os.environ.get("SPEACHES_MODEL", "Systran/faster-whisper-small")


def timeout_seconds() -> int:
    return int(os.environ.get("TIMEOUT_SECONDS", "1800"))


def worker_mode() -> str:
    """"sync" runs jobs inline (used by tests); anything else uses thread pools."""
    return os.environ.get("WORKER_MODE", "async")


def ytdlp_autoupdate() -> bool:
    return os.environ.get("YTDLP_AUTOUPDATE", "0") == "1"


def ytdlp_options() -> dict[str, str]:
    """Return optional yt-dlp Python API options from the environment."""
    options: dict[str, str] = {}
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
    proxy = os.environ.get("YTDLP_PROXY")
    if cookies_file:
        options["cookiefile"] = cookies_file
    if proxy:
        options["proxy"] = proxy
    return options


def ytdlp_cli_args() -> list[str]:
    """Return optional yt-dlp CLI arguments without exposing their values."""
    args: list[str] = []
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
    proxy = os.environ.get("YTDLP_PROXY")
    if cookies_file:
        args.extend(["--cookies", cookies_file])
    if proxy:
        args.extend(["--proxy", proxy])
    return args
