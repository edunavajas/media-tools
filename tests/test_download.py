"""Download job flow with a fake YoutubeDL, plus TTL expiry."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import downloader, main, workers
from tests.conftest import AUTH_HEADERS

FAKE_FILE_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048


class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL: fires hooks, writes the mp4."""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=True):
        out_dir = Path(self.opts["outtmpl"]).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / "Fake video [abc123].mp4"

        for hook in self.opts["progress_hooks"]:
            hook({
                "status": "downloading",
                "downloaded_bytes": len(FAKE_FILE_BYTES) // 2,
                "total_bytes": len(FAKE_FILE_BYTES),
                "speed": 1024.0,
                "eta": 1,
                "filename": str(target),
            })

        target.write_bytes(FAKE_FILE_BYTES)

        for hook in self.opts["progress_hooks"]:
            hook({
                "status": "finished",
                "downloaded_bytes": len(FAKE_FILE_BYTES),
                "total_bytes": len(FAKE_FILE_BYTES),
                "filename": str(target),
            })

        return {
            "title": "Fake video",
            "extractor": "generic",
            "duration": 12,
            "filesize": len(FAKE_FILE_BYTES),
            "thumbnail": "https://example.com/thumb.jpg",
            "requested_downloads": [{"filepath": str(target)}],
        }


def _mock_ydl(monkeypatch):
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)


def test_download_job_happy_path(client, monkeypatch):
    _mock_ydl(monkeypatch)

    resp = client.post(
        "/download",
        json={"url": "https://example.com/watch?v=abc123"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    state = client.get(f"/download/{job_id}", headers=AUTH_HEADERS).json()
    assert state["status"] == "done"
    assert state["percent"] == 100.0
    metadata = state["metadata"]
    assert metadata["title"] == "Fake video"
    assert metadata["extractor"] == "generic"
    assert metadata["duration"] == 12
    assert metadata["filesize"] == len(FAKE_FILE_BYTES)
    assert metadata["thumbnail"] == "https://example.com/thumb.jpg"

    resp = client.get(f"/download/{job_id}/file", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.content == FAKE_FILE_BYTES


def test_download_rejects_non_http(client):
    resp = client.post(
        "/download", json={"url": "ftp://example.com/x"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 422


def test_download_not_found(client):
    resp = client.get("/download/nope", headers=AUTH_HEADERS)
    assert resp.status_code == 404


def test_supported_sites(client):
    resp = client.get("/download/supported", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    names = {s["name"] for s in body["sites"]}
    assert "YouTube" in names
    assert "TikTok" in names
    assert any("youtube.com" in s["domains"] for s in body["sites"])
    assert "1000+" in body["note"]


def test_ttl_expiry_makes_file_gone(client, monkeypatch):
    _mock_ydl(monkeypatch)

    resp = client.post(
        "/download",
        json={"url": "https://example.com/watch?v=abc123"},
        headers=AUTH_HEADERS,
    )
    job_id = resp.json()["job_id"]
    assert client.get(f"/download/{job_id}/file",
                      headers=AUTH_HEADERS).status_code == 200

    # Forge finished_at 25h in the past and run the cleaner.
    store = main.get_store()
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    with store._lock:
        store._conn.execute(
            "UPDATE jobs SET finished_at = ? WHERE id = ?", (old, job_id)
        )
        store._conn.commit()

    output_dir = Path(store.get_job(job_id)["output_dir"])
    assert output_dir.exists()

    expired = workers.cleanup_expired_jobs(store)
    assert expired == 1
    assert not output_dir.exists()

    job = client.get(f"/jobs/{job_id}", headers=AUTH_HEADERS).json()
    assert job["status"] == "expired"

    resp = client.get(f"/download/{job_id}/file", headers=AUTH_HEADERS)
    assert resp.status_code == 410
