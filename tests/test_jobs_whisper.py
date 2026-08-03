"""Whisper job flow with mocked indexing/download/transcription (no network)."""
import httpx

from app import whisper_pipeline
from app.schemas import ChannelExport
from app.whisper_pipeline import TranscriptionConfig
from tests.conftest import AUTH_HEADERS

INDEXED_VIDEOS = [
    {
        "video_id": "vid1",
        "title": "First video",
        "url": "https://www.youtube.com/watch?v=vid1",
        "upload_date": "20240101",
        "duration": 61.0,
        "view_count": 100,
        "channel": "Test Channel",
        "channel_id": "UCtest",
    },
    {
        "video_id": "vid2",
        "title": "Second video",
        "url": "https://www.youtube.com/watch?v=vid2",
        "upload_date": None,
        "duration": None,
        "view_count": None,
        "channel": "Test Channel",
        "channel_id": "UCtest",
    },
    {
        "video_id": "vid3",
        "title": "Third video",
        "url": "https://www.youtube.com/watch?v=vid3",
        "upload_date": "20240103",
        "duration": 30.0,
        "view_count": 5,
        "channel": "Test Channel",
        "channel_id": "UCtest",
    },
]

TRANSCRIPT_FIXTURE = {
    "text": "hola mundo esto es una transcripción",
    "segments": [
        {"start": 0.0, "end": 1.5, "text": "hola mundo"},
        {"start": 1.5, "end": 3.0, "text": "esto es una transcripción"},
    ],
    "language": "es",
    "duration": 3.0,
}


def _mock_pipeline(monkeypatch, fail_video_id=None):
    """Mock index_channel, download_audio and transcribe_audio."""
    monkeypatch.setattr(
        whisper_pipeline, "index_channel", lambda url: list(INDEXED_VIDEOS)
    )

    def fake_download(video_url, output_path, cache_dir):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake mp3 bytes")
        return True

    monkeypatch.setattr(whisper_pipeline, "download_audio", fake_download)

    def fake_transcribe(audio_path, config):
        if fail_video_id and audio_path.parent.name == fail_video_id:
            return None
        return dict(TRANSCRIPT_FIXTURE)

    monkeypatch.setattr(whisper_pipeline, "transcribe_audio", fake_transcribe)
    # No real backoff sleeping in tests.
    monkeypatch.setattr(whisper_pipeline.time, "sleep", lambda s: None)


def test_whisper_job_happy_path(client, monkeypatch):
    _mock_pipeline(monkeypatch)

    resp = client.post(
        "/jobs/whisper",
        json={
            "channel_url": "https://www.youtube.com/@test/videos",
            "language": "es",
            "max_videos": 2,
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # WORKER_MODE=sync: the job already ran inline.
    detail = client.get(f"/jobs/{job_id}", headers=AUTH_HEADERS)
    assert detail.status_code == 200
    job = detail.json()
    assert job["status"] == "done"
    assert job["progress"]["total"] == 2  # max_videos applied
    assert job["progress"]["done"] == 2
    assert job["progress"]["failed"] == []

    resp = client.get(f"/jobs/{job_id}/download", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    export = ChannelExport(**resp.json())
    assert export.channel == "Test Channel"
    assert export.language == "es"
    assert len(export.videos) == 2
    for video in export.videos:
        assert video.transcript is not None
        assert video.transcript.text == TRANSCRIPT_FIXTURE["text"]
        assert len(video.transcript.segments) == 2


def test_whisper_job_partial_failure(client, monkeypatch):
    _mock_pipeline(monkeypatch, fail_video_id="vid2")

    resp = client.post(
        "/jobs/whisper",
        json={"channel_url": "https://www.youtube.com/@test/videos"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    job = client.get(f"/jobs/{job_id}", headers=AUTH_HEADERS).json()
    # Partial failures do not fail the whole job.
    assert job["status"] == "done"
    assert job["progress"]["done"] == 3
    assert len(job["progress"]["failed"]) == 1
    assert job["progress"]["failed"][0]["video_id"] == "vid2"

    export = ChannelExport(
        **client.get(f"/jobs/{job_id}/download", headers=AUTH_HEADERS).json()
    )
    by_id = {v.video_id: v for v in export.videos}
    assert by_id["vid1"].transcript is not None
    assert by_id["vid2"].transcript is None
    assert by_id["vid2"].error
    assert by_id["vid3"].transcript is not None


def test_speaches_verbose_json_parsing(tmp_path, monkeypatch):
    """transcribe_audio parses a real speaches verbose_json response."""
    speaches_payload = {
        "text": "hola mundo esto es una transcripción",
        "segments": [
            {
                "id": 0,
                "seek": 0,
                "start": 0.0,
                "end": 1.5,
                "text": " hola mundo",
                "tokens": [50364, 6041],
                "temperature": 0.0,
                "avg_logprob": -0.25,
                "compression_ratio": 1.1,
                "no_speech_prob": 0.01,
            },
            {
                "id": 1,
                "seek": 150,
                "start": 1.5,
                "end": 3.0,
                "text": " esto es una transcripción",
                "tokens": [6041, 9000],
                "temperature": 0.0,
                "avg_logprob": -0.3,
                "compression_ratio": 1.2,
                "no_speech_prob": 0.02,
            },
        ],
        "language": "es",
        "duration": 3.0,
    }

    captured = {}

    def fake_post(self, url, files=None, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        return httpx.Response(
            200,
            json=speaches_payload,
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake mp3")
    config = TranscriptionConfig(
        base_url="http://speaches.test",
        endpoint="/v1/audio/transcriptions",
        mode="openai",
        model="Systran/faster-whisper-small",
        language="es",
        timeout=60,
    )

    result = whisper_pipeline.transcribe_audio(audio, config)

    assert result is not None
    assert result["text"] == speaches_payload["text"]
    assert result["language"] == "es"
    assert len(result["segments"]) == 2
    assert result["segments"][0]["start"] == 0.0
    assert result["segments"][1]["end"] == 3.0
    # OpenAI-style endpoint with the anti-translation hints.
    assert captured["url"] == "http://speaches.test/v1/audio/transcriptions"
    assert captured["data"]["task"] == "transcribe"
    assert captured["data"]["response_format"] == "verbose_json"
    assert "español" in captured["data"]["initial_prompt"]


def test_whisper_job_index_failure_marks_failed(client, monkeypatch):
    def boom(url):
        raise RuntimeError("yt-dlp exploded")

    monkeypatch.setattr(whisper_pipeline, "index_channel", boom)

    resp = client.post(
        "/jobs/whisper",
        json={"channel_url": "https://www.youtube.com/@test/videos"},
        headers=AUTH_HEADERS,
    )
    job_id = resp.json()["job_id"]
    job = client.get(f"/jobs/{job_id}", headers=AUTH_HEADERS).json()
    assert job["status"] == "failed"
    assert "yt-dlp exploded" in job["error"]


def test_job_not_found(client):
    resp = client.get("/jobs/nope", headers=AUTH_HEADERS)
    assert resp.status_code == 404
