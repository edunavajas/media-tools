"""channel.json consolidation: success, failure (transcript null), schema."""
import json

from app.consolidate import build_channel_json
from app.schemas import ChannelExport

INDEX_VIDEOS = [
    {
        "video_id": "ok1",
        "title": "Worked",
        "url": "https://www.youtube.com/watch?v=ok1",
        "upload_date": "20240101",
        "duration": 42.0,
        "view_count": 7,
        "channel": "Chan",
        "channel_id": "UC1",
    },
    {
        "video_id": "bad1",
        "title": "Broke",
        "url": "https://www.youtube.com/watch?v=bad1",
        "upload_date": None,
        "duration": None,
        "view_count": None,
        "channel": "Chan",
        "channel_id": "UC1",
    },
]


def test_build_channel_json(tmp_path):
    ok_dir = tmp_path / "ok1"
    ok_dir.mkdir()
    (ok_dir / "transcript.json").write_text(
        json.dumps({
            "video_id": "ok1",
            "title": "Worked",
            "text": "texto transcrito",
            "segments": [{"start": 0.0, "end": 1.0, "text": "texto"}],
            "language": "es",
            "duration": 42.0,
        }),
        encoding="utf-8",
    )

    export = build_channel_json(
        tmp_path, INDEX_VIDEOS, "es", errors={"bad1": "Audio download failed"}
    )

    # Written to disk.
    on_disk = json.loads((tmp_path / "channel.json").read_text("utf-8"))
    assert on_disk == export

    # Validates against the API schema.
    parsed = ChannelExport(**export)
    assert parsed.channel == "Chan"
    assert parsed.channel_id == "UC1"
    assert parsed.language == "es"
    assert len(parsed.videos) == 2

    ok = next(v for v in parsed.videos if v.video_id == "ok1")
    assert ok.transcript is not None
    assert ok.transcript.text == "texto transcrito"
    assert ok.error is None

    bad = next(v for v in parsed.videos if v.video_id == "bad1")
    assert bad.transcript is None
    assert bad.error == "Audio download failed"
    # Nullable index fields survive as null.
    assert bad.upload_date is None
    assert bad.duration is None
    assert bad.view_count is None
