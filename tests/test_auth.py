"""Auth: fail-closed without token, 401 on bad token, open /health."""
from tests.conftest import AUTH_HEADERS


def test_health_is_open(no_token_client):
    resp = no_token_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["ytdlp_version"]


def test_missing_token_config_fails_closed(no_token_client):
    resp = no_token_client.get("/jobs", headers=AUTH_HEADERS)
    assert resp.status_code == 500


def test_bad_token_rejected(client):
    resp = client.get("/jobs", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_no_token_header_rejected(client):
    resp = client.get("/jobs")
    assert resp.status_code == 401


def test_good_token_accepted(client):
    resp = client.get("/jobs", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == []
