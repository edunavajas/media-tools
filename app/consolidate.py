"""Build the consolidated channel.json export for a whisper job."""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.whisper_pipeline import sanitize_filename

logger = logging.getLogger(__name__)


def build_channel_json(
    job_dir: Path,
    index_videos: list[dict[str, Any]],
    language: str,
    errors: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """
    Consolidate per-video transcripts into a single channel.json.

    Every indexed video is present in the output; videos whose
    transcription failed get transcript=null plus an error note.

    Args:
        job_dir: Job output directory (contains one subdir per video)
        index_videos: Video dicts returned by index_channel
        language: Requested transcription language
        errors: Optional video_id -> error message map for failed videos

    Returns:
        The consolidated export dict (also written to job_dir/channel.json).
    """
    channel: Optional[str] = None
    channel_id: Optional[str] = None
    for video in index_videos:
        if video.get("channel"):
            channel = video["channel"]
        if video.get("channel_id"):
            channel_id = video["channel_id"]
        if channel and channel_id:
            break

    videos_out: list[dict[str, Any]] = []
    for video in index_videos:
        video_id = video["video_id"]
        transcript_path = job_dir / sanitize_filename(video_id) / "transcript.json"

        transcript: Optional[dict[str, Any]] = None
        error: Optional[str] = None
        if transcript_path.exists():
            try:
                transcript = json.loads(
                    transcript_path.read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, OSError) as e:
                error = f"corrupt transcript: {e}"
        else:
            error = (errors or {}).get(video_id, "transcription failed")

        entry: dict[str, Any] = {
            "video_id": video_id,
            "title": video.get("title", "Unknown"),
            "url": video.get("url")
            or f"https://www.youtube.com/watch?v={video_id}",
            "upload_date": video.get("upload_date"),
            "duration": video.get("duration"),
            "view_count": video.get("view_count"),
            "transcript": transcript,
        }
        if transcript is None:
            entry["error"] = error
        videos_out.append(entry)

    export = {
        "channel": channel,
        "channel_id": channel_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "language": language,
        "videos": videos_out,
    }

    job_dir.mkdir(parents=True, exist_ok=True)
    out_path = job_dir / "channel.json"
    out_path.write_text(
        json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"Wrote channel export: {out_path}")
    return export
