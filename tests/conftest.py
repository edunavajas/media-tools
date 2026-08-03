"""Shared fixtures: env monkeypatched, WORKER_MODE=sync, TestClient."""
import pytest
from fastapi.testclient import TestClient

TEST_TOKEN = "test-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with token configured and jobs running inline."""
    monkeypatch.setenv("MEDIA_TOOLS_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKER_MODE", "sync")
    monkeypatch.setenv("SPEACHES_BASE_URL", "http://speaches.test")
    monkeypatch.setenv("SPEACHES_ENDPOINT", "/v1/audio/transcriptions")
    monkeypatch.setenv("SPEACHES_MODE", "openai")
    monkeypatch.setenv("SPEACHES_MODEL", "test-model")
    monkeypatch.setenv("TIMEOUT_SECONDS", "60")
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def no_token_client(tmp_path, monkeypatch):
    """TestClient without MEDIA_TOOLS_TOKEN configured (fail-closed)."""
    monkeypatch.delenv("MEDIA_TOOLS_TOKEN", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKER_MODE", "sync")
    from app.main import app

    with TestClient(app) as c:
        yield c
