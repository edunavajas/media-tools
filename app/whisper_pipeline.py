"""Whisper transcription pipeline vendored from yt-channel-whisper.

Adapted from tools/channel_index.py and tools/transcribe_channel.py:
- No sys.exit anywhere: failures raise exceptions or return None so worker
  threads can mark jobs failed properly.
- index_channel returns an in-memory list[dict] instead of writing JSONL.
- process_video accepts an on_progress(event, ok, error) callback invoked
  when a video finishes (successfully or after the last retry). ``event``
  is the VideoTask that was processed.
- tqdm removed; logging kept.
"""
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionConfig:
    """Configuration for transcription service."""
    base_url: str
    endpoint: str
    mode: str  # "openai" or "generic"
    model: str
    language: str
    timeout: int


@dataclass
class VideoTask:
    """Video processing task."""
    video_id: str
    title: str
    url: str
    output_dir: Path


def sanitize_filename(filename: str) -> str:
    """Sanitize filenames for Windows compatibility."""
    invalid_chars = '<>:"|?*\\/'
    for char in invalid_chars:
        filename = filename.replace(char, "_")
    # Limit length
    if len(filename) > 200:
        filename = filename[:200]
    return filename


def index_channel(channel_url: str) -> list[dict[str, Any]]:
    """
    Index all videos from a YouTube channel using yt-dlp.

    Args:
        channel_url: YouTube channel URL (videos page)

    Returns:
        List of video dicts (video_id, title, url, upload_date, duration,
        view_count, channel, channel_id). upload_date/duration/view_count
        may be None.

    Raises:
        RuntimeError: if yt-dlp fails or its output cannot be parsed.
    """
    logger.info(f"Indexing channel: {channel_url}")

    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--extractor-args", "youtube:skip=authcheck",
        channel_url,
    ]

    logger.debug(f"Running: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"yt-dlp failed with exit code {e.returncode}: {e.stderr}"
        ) from e
    except FileNotFoundError as e:
        raise RuntimeError("yt-dlp not found. Install it: pip install yt-dlp") from e

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"failed to parse yt-dlp JSON output: {e}") from e

    entries = data.get("entries", [])
    if not entries:
        logger.warning("No videos found in channel")
        return []

    logger.info(f"Found {len(entries)} videos")

    # Flat playlist entries often lack channel fields; fall back to the
    # playlist-level metadata.
    default_channel = data.get("channel") or data.get("uploader")
    default_channel_id = data.get("channel_id") or data.get("uploader_id")

    videos: list[dict[str, Any]] = []
    for entry in entries:
        if not entry:  # Skip None entries
            continue

        video_id = entry.get("id")
        if not video_id:
            logger.warning("Skipping entry without ID")
            continue

        videos.append({
            "video_id": video_id,
            "title": entry.get("title", "Unknown"),
            "url": entry.get("url") or f"https://www.youtube.com/watch?v={video_id}",
            "upload_date": entry.get("upload_date"),
            "duration": entry.get("duration"),
            "view_count": entry.get("view_count"),
            "channel": entry.get("channel") or default_channel,
            "channel_id": entry.get("channel_id") or default_channel_id,
        })

    return videos


def load_videos(input_path: Path) -> list[dict[str, Any]]:
    """Load videos from JSONL file.

    Raises:
        FileNotFoundError: if the input file does not exist.
    """
    videos = []

    if not input_path.exists():
        raise FileNotFoundError(f"input file not found: {input_path}")

    with input_path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                video = json.loads(line)
                videos.append(video)
            except json.JSONDecodeError as e:
                logger.warning(f"Skipping invalid JSON at line {line_num}: {e}")

    logger.info(f"Loaded {len(videos)} videos from {input_path}")
    return videos


def download_audio(video_url: str, output_path: Path, cache_dir: Path) -> bool:
    """
    Download audio from YouTube video using yt-dlp.

    Args:
        video_url: YouTube video URL
        output_path: Path to save audio file
        cache_dir: Cache directory for yt-dlp

    Returns:
        True if successful, False otherwise
    """
    if output_path.exists():
        logger.debug(f"Audio already exists: {output_path}")
        return True

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Use yt-dlp to download best audio
    cmd = [
        "yt-dlp",
        "-f", "bestaudio/best",
        "-x",  # Extract audio
        "--audio-format", "mp3",
        "--audio-quality", "0",  # Best quality
        "-o", str(output_path.with_suffix(".%(ext)s")),
        "--cache-dir", str(cache_dir),
        "--no-warnings",
        "--no-playlist",
        video_url,
    ]

    logger.debug(f"Downloading audio: {' '.join(cmd)}")

    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
        )

        # yt-dlp adds .mp3 extension
        actual_path = output_path.with_suffix(".mp3")
        if actual_path != output_path:
            actual_path.rename(output_path)

        if output_path.exists():
            logger.debug(f"Downloaded: {output_path}")
            return True
        else:
            logger.error(f"Download completed but file not found: {output_path}")
            return False

    except subprocess.CalledProcessError as e:
        logger.error(f"Download failed: {e.stderr}")
        return False
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False


def _apply_language_hint(data: dict[str, str], language: str) -> None:
    """Add initial_prompt to help force language detection (Whisper hint)."""
    if language == "es":
        data["initial_prompt"] = (
            "Transcribe en español sin traducir. No traduzcas. "
            "Mantén el idioma original."
        )
    elif language == "en":
        data["initial_prompt"] = (
            "Transcribe in English without translating. Do not translate."
        )


def _check_language(result: dict[str, Any], requested: str) -> None:
    detected_lang = result.get("language", "unknown")
    logger.info(
        f"Transcription successful: {len(result.get('text', ''))} chars, "
        f"detected_language={detected_lang}"
    )
    # Warn if language mismatch
    if detected_lang != requested and detected_lang != "unknown":
        logger.warning(
            f"Language mismatch! Requested={requested}, Detected={detected_lang}"
        )


def transcribe_audio_openai_style(
    audio_path: Path,
    config: TranscriptionConfig,
    client: httpx.Client,
) -> Optional[dict[str, Any]]:
    """
    Transcribe audio using OpenAI-compatible API endpoint.

    Returns:
        Transcription result dict or None if failed
    """
    url = f"{config.base_url.rstrip('/')}{config.endpoint}"

    logger.debug(f"Transcribing via OpenAI-style API: {url}")

    try:
        with audio_path.open("rb") as audio_file:
            files = {"file": (audio_path.name, audio_file, "audio/mpeg")}
            data = {
                "model": config.model,
                "language": config.language,
                "task": "transcribe",  # CRITICAL: Force transcribe, not translate
                "translate": "false",   # Some servers honor this flag
                "output_language": config.language,  # Some servers use this
                "response_format": "verbose_json",
            }
            _apply_language_hint(data, config.language)

            logger.info(
                f"Sending transcription request: model={config.model}, "
                f"language={config.language}, task=transcribe, translate=false"
            )

            response = client.post(
                url,
                files=files,
                data=data,
                timeout=config.timeout,
            )
            response.raise_for_status()

            result = response.json()
            _check_language(result, config.language)
            return result

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.debug("Endpoint not found (404), will try fallback")
            return None
        logger.error(f"HTTP error {e.response.status_code}: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return None


def transcribe_audio_generic_style(
    audio_path: Path,
    config: TranscriptionConfig,
    client: httpx.Client,
) -> Optional[dict[str, Any]]:
    """
    Transcribe audio using generic API endpoint.

    Returns:
        Transcription result dict or None if failed
    """
    url = f"{config.base_url.rstrip('/')}/transcribe"

    logger.debug(f"Transcribing via generic API: {url}")

    try:
        with audio_path.open("rb") as audio_file:
            files = {"file": (audio_path.name, audio_file, "audio/mpeg")}
            data = {
                "model": config.model,
                "language": config.language,
                "task": "transcribe",  # CRITICAL: Force transcribe, not translate
                "translate": "false",   # Some servers honor this flag
                "output_language": config.language,  # Some servers use this
            }
            _apply_language_hint(data, config.language)

            logger.info(
                f"Sending transcription request: model={config.model}, "
                f"language={config.language}, task=transcribe, translate=false"
            )

            response = client.post(
                url,
                files=files,
                data=data,
                timeout=config.timeout,
            )
            response.raise_for_status()

            result = response.json()
            _check_language(result, config.language)
            return result

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error {e.response.status_code}: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return None


def transcribe_audio(
    audio_path: Path,
    config: TranscriptionConfig,
) -> Optional[dict[str, Any]]:
    """
    Transcribe audio using configured service with automatic fallback.

    Returns:
        Transcription result dict or None if failed
    """
    with httpx.Client() as client:
        if config.mode == "openai":
            # Try OpenAI-compatible endpoint first
            result = transcribe_audio_openai_style(audio_path, config, client)
            if result is not None:
                return result

            # Fallback to generic endpoint
            logger.info("Falling back to generic endpoint")
            result = transcribe_audio_generic_style(audio_path, config, client)
            if result is not None:
                return result
        else:
            # Try generic endpoint
            result = transcribe_audio_generic_style(audio_path, config, client)
            if result is not None:
                return result

    return None


def format_timestamp(seconds: float) -> str:
    """Format seconds to SRT timestamp format (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def generate_srt(segments: list[dict[str, Any]]) -> str:
    """Generate SRT subtitle format from segments."""
    srt_lines = []

    for i, segment in enumerate(segments, 1):
        start = segment.get("start", 0)
        end = segment.get("end", 0)
        text = segment.get("text", "").strip()

        if not text:
            continue

        srt_lines.append(f"{i}")
        srt_lines.append(f"{format_timestamp(start)} --> {format_timestamp(end)}")
        srt_lines.append(text)
        srt_lines.append("")  # Empty line between subtitles

    return "\n".join(srt_lines)


def save_transcription(
    result: dict[str, Any],
    output_dir: Path,
    video_id: str,
    title: str,
) -> None:
    """
    Save transcription results to files.

    Args:
        result: Transcription result from API
        output_dir: Output directory for this video
        video_id: Video ID
        title: Video title
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Extract text
    text = result.get("text", "")

    # Save plain text
    txt_path = output_dir / "transcript.txt"
    txt_path.write_text(text, encoding="utf-8")
    logger.debug(f"Saved text: {txt_path}")

    # Save JSON with metadata
    json_data = {
        "video_id": video_id,
        "title": title,
        "text": text,
        "segments": result.get("segments", []),
        "language": result.get("language"),
        "duration": result.get("duration"),
    }
    json_path = output_dir / "transcript.json"
    json_path.write_text(
        json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.debug(f"Saved JSON: {json_path}")

    # Generate and save SRT if segments available
    segments = result.get("segments", [])
    if segments:
        srt_content = generate_srt(segments)
        srt_path = output_dir / "transcript.srt"
        srt_path.write_text(srt_content, encoding="utf-8")
        logger.debug(f"Saved SRT: {srt_path}")
    else:
        logger.warning("No segments available for SRT generation")


def process_video(
    task: VideoTask,
    config: TranscriptionConfig,
    cache_dir: Path,
    delete_audio: bool = True,
    skip_existing: bool = False,
    retries: int = 3,
    retry_backoff: float = 5.0,
    on_progress: Optional[Callable[[VideoTask, bool, Optional[str]], None]] = None,
) -> tuple[str, bool, Optional[str]]:
    """
    Process a single video: download audio and transcribe.

    Args:
        task: Video task
        config: Transcription configuration
        cache_dir: Cache directory
        delete_audio: Whether to delete audio after processing
        skip_existing: Whether to skip if outputs exist
        retries: Number of retries on failure
        retry_backoff: Backoff time between retries (seconds)
        on_progress: Optional callback invoked as
            on_progress(task, ok, error) when the video reaches a final state.

    Returns:
        Tuple of (video_id, success, error_message)
    """
    def _finish(ok: bool, error: Optional[str]) -> tuple[str, bool, Optional[str]]:
        if on_progress is not None:
            try:
                on_progress(task, ok, error)
            except Exception:
                logger.exception("on_progress callback failed")
        return (task.video_id, ok, error)

    # Check if already processed
    txt_path = task.output_dir / "transcript.txt"
    json_path = task.output_dir / "transcript.json"

    if skip_existing and txt_path.exists() and json_path.exists():
        logger.info(f"Skipping {task.video_id} (already processed)")
        return _finish(True, None)

    logger.info(f"Processing: {task.title[:50]}...")

    # Download audio
    audio_path = task.output_dir / "audio.mp3"

    for attempt in range(retries):
        if attempt > 0:
            logger.info(f"Retry {attempt}/{retries - 1} for {task.video_id}")
            time.sleep(retry_backoff * attempt)

        # Download
        if not download_audio(task.url, audio_path, cache_dir):
            if attempt == retries - 1:
                return _finish(False, "Audio download failed")
            continue

        # Transcribe
        result = transcribe_audio(audio_path, config)
        if result is None:
            if attempt == retries - 1:
                return _finish(False, "Transcription failed")
            continue

        # Save results
        try:
            save_transcription(result, task.output_dir, task.video_id, task.title)
        except Exception as e:
            logger.error(f"Failed to save results: {e}")
            if attempt == retries - 1:
                return _finish(False, f"Save failed: {e}")
            continue

        # Clean up audio if requested
        if delete_audio and audio_path.exists():
            audio_path.unlink()
            logger.debug(f"Deleted audio: {audio_path}")

        return _finish(True, None)

    return _finish(False, "Max retries exceeded")
