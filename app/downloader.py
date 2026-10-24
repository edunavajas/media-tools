"""Video download via the yt_dlp Python API, plus the supported-sites list."""
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

import yt_dlp

from app import config

logger = logging.getLogger(__name__)

# ext=mp4 does not filter codecs: YouTube serves AV1 inside MP4, which Premiere
# Pro cannot decode. Pin H.264/AAC; H.264 tops out at 1080p so 4K now yields
# 1080p, and a video with no H.264 format fails instead of silently giving AV1.
# Extractors label H.264 inconsistently (YouTube: "avc1", TikTok: "h264"), so
# both tags must be tried before giving up — commit 983400b broke TikTok by
# matching "avc1" only (prod: "Requested format is not available").
DOWNLOAD_FORMAT = (
    "bv*[vcodec^=avc1]+ba[acodec^=mp4a]"
    "/bv*[vcodec^=h264]+ba[acodec^=aac]"
    "/b[vcodec^=avc1]"
    "/b[vcodec^=h264]"
)
# Last-resort selector used only when the pinned branches match nothing: some
# extractors report vcodec=None for H.264 streams, which the labels above
# cannot see. _assert_h264_video still rejects the file if it is not really
# H.264, so this never delivers AV1/VP9 to Premiere.
FALLBACK_FORMAT = "bv*+ba/b"
FORMAT_UNAVAILABLE_MARKER = "Requested format is not available"

SUPPORTED_SITES: list[dict[str, Any]] = [
    {"name": "YouTube", "domains": ["youtube.com", "youtu.be"]},
    {"name": "TikTok", "domains": ["tiktok.com"]},
    {"name": "Instagram", "domains": ["instagram.com"]},
    {"name": "X / Twitter", "domains": ["x.com", "twitter.com"]},
    {"name": "Facebook", "domains": ["facebook.com", "fb.watch"]},
    {"name": "LinkedIn", "domains": ["linkedin.com"]},
    {"name": "Twitch", "domains": ["twitch.tv"]},
    {"name": "Reddit", "domains": ["reddit.com"]},
    {"name": "Vimeo", "domains": ["vimeo.com"]},
    {"name": "Dailymotion", "domains": ["dailymotion.com", "dai.ly"]},
]

SUPPORTED_SITES_NOTE = (
    "yt-dlp soporta ~1000+ sitios; esta es una lista curada de los más "
    "habituales. Cualquier URL soportada por yt-dlp debería funcionar."
)


def extract_metadata(info: dict[str, Any]) -> dict[str, Any]:
    """Pull the curated metadata subset out of a yt-dlp info dict."""
    return {
        "title": info.get("title"),
        "extractor": info.get("extractor"),
        "duration": info.get("duration"),
        "filesize": info.get("filesize") or info.get("filesize_approx"),
        "thumbnail": info.get("thumbnail"),
    }


def _hhmmss_tag(seconds: Optional[float]) -> str:
    """Render seconds as zero-padded HH-MM-SS; None (open end) as END."""
    if seconds is None:
        return "END"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}-{minutes:02d}-{secs:02d}"


def _download_ranges(
    start_seconds: Optional[float], end_seconds: Optional[float]
) -> Callable[[dict[str, Any], Any], list[dict[str, Any]]]:
    """Build the yt-dlp download_ranges callable for a time fragment.

    A fragment that covers the whole video is NOT a trim: returning the bare
    ``{}`` marker makes yt-dlp skip its ffmpeg section/merge path and stream the
    file as-is (see YoutubeDL.process_info), avoiding a full re-encode. Clients
    commonly send ``start=0, end=duration`` from a "full video" default, and
    treating that as a trim used to re-encode hours of video.
    """

    def _ranges(
        info_dict: dict[str, Any], ydl: Any
    ) -> list[dict[str, Any]]:
        start = float(start_seconds or 0.0)
        end = float("inf") if end_seconds is None else float(end_seconds)
        duration = info_dict.get("duration")
        covers_whole_video = start <= 0 and (
            end == float("inf")
            or (isinstance(duration, (int, float)) and end >= duration - 0.5)
        )
        if covers_whole_video:
            return [{}]
        return [{"start_time": start, "end_time": end}]

    return _ranges


def _assert_h264_video(path: Path) -> None:
    """Fail closed unless ffprobe reports the video stream as h264.

    The format selector asks for avc1, but the produced file is the contract
    with Premiere Pro: exit code alone is useless (a valid AV1 file exits 0) and
    a corrupt file exits 1 with empty stdout, so both must be checked.
    """
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"could not validate downloaded file {path}: ffprobe exited "
            f"{result.returncode}: {result.stderr.strip()}"
        )
    codec = result.stdout.strip()
    if codec != "h264":
        raise RuntimeError(
            f"unsupported video codec {codec!r}: expected h264 "
            "(Premiere Pro cannot decode AV1/VP9)"
        )


def download_video(
    url: str,
    output_dir: Path,
    progress_hook: Optional[Callable[[dict[str, Any]], None]] = None,
    hook_throttle_seconds: float = 1.0,
    start_seconds: Optional[float] = None,
    end_seconds: Optional[float] = None,
) -> tuple[Path, dict[str, Any]]:
    """
    Download a video as mp4 into output_dir.

    Args:
        url: Video URL (any yt-dlp supported site)
        output_dir: Directory for the output file
        progress_hook: Called with throttled progress dicts:
            {percent, downloaded_bytes, total_bytes, speed, eta, filename}
        hook_throttle_seconds: Minimum interval between "downloading" updates
        start_seconds: Optional fragment start (seconds, inclusive)
        end_seconds: Optional fragment end (seconds, exclusive)

    Returns:
        (final mp4 path, metadata dict)

    Raises:
        RuntimeError: if yt-dlp fails or no mp4 is produced.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    last_emit = 0.0

    def _hook(d: dict[str, Any]) -> None:
        nonlocal last_emit
        if progress_hook is None:
            return
        status = d.get("status")
        if status == "downloading":
            now = time.monotonic()
            if now - last_emit < hook_throttle_seconds:
                return
            last_emit = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100.0) if total else None
            progress_hook({
                "status": "downloading",
                "percent": percent,
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                "speed": d.get("speed"),
                "eta": d.get("eta"),
                "filename": d.get("filename"),
            })
        elif status == "finished":
            progress_hook({
                "status": "finished",
                "percent": 100.0,
                "downloaded_bytes": d.get("downloaded_bytes")
                or d.get("total_bytes"),
                "total_bytes": d.get("total_bytes"),
                "speed": None,
                "eta": 0,
                "filename": d.get("filename"),
            })

    has_range = start_seconds is not None or end_seconds is not None
    if has_range:
        trim_tag = (
            f" [trim-{_hhmmss_tag(start_seconds)}-{_hhmmss_tag(end_seconds)}]"
        )
        outtmpl = str(
            output_dir / f"%(title).80s [%(id)s]{trim_tag}.%(ext)s"
        )
    else:
        outtmpl = str(output_dir / "%(title).80s [%(id)s].%(ext)s")

    ydl_opts: dict[str, Any] = {
        "format": DOWNLOAD_FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "no_warnings": True,
        "progress_hooks": [_hook],
    }
    if has_range:
        ydl_opts["download_ranges"] = _download_ranges(
            start_seconds, end_seconds
        )
    ydl_opts.update(config.ytdlp_options())

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        if FORMAT_UNAVAILABLE_MARKER not in str(e):
            raise RuntimeError(f"yt-dlp download failed: {e}") from e
        # The pinned codec branches matched no format. Retry codec-agnostic
        # once: either the stream really lacks H.264 (the ffprobe guard below
        # then fails the job with a clear codec error) or the extractor
        # mislabeled the codecs and this recovers the download.
        logger.warning(
            f"pinned H.264 format unavailable for {url}; "
            "retrying with best available codec"
        )
        try:
            with yt_dlp.YoutubeDL({**ydl_opts, "format": FALLBACK_FORMAT}) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as retry_exc:
            raise RuntimeError(
                f"yt-dlp download failed: {retry_exc}"
            ) from retry_exc

    if info is None:
        raise RuntimeError("yt-dlp returned no info for the URL")

    metadata = extract_metadata(info)

    # Resolve the final file path: requested_downloads carries the
    # post-processed filepath in recent yt-dlp versions; fall back to glob.
    final_path: Optional[Path] = None
    requested = info.get("requested_downloads") or []
    if requested and requested[0].get("filepath"):
        candidate = Path(requested[0]["filepath"])
        if candidate.exists():
            final_path = candidate
    if final_path is None:
        mp4s = sorted(output_dir.glob("*.mp4"))
        if mp4s:
            final_path = mp4s[0]
    if final_path is None:
        raise RuntimeError("download finished but no mp4 file was produced")

    # Covers the full download and the trim path alike: download_ranges
    # stream-copies, so a wrong-codec source survives the trim untouched.
    _assert_h264_video(final_path)

    # Some extractors (e.g. generic) report no filesize; stat the file.
    if metadata.get("filesize") is None:
        metadata["filesize"] = final_path.stat().st_size

    logger.info(f"Downloaded {url} -> {final_path}")
    return final_path, metadata


def probe_video(url: str) -> dict[str, Any]:
    """
    Extract metadata for a URL without downloading it.

    Raises:
        RuntimeError: if yt-dlp fails or returns no info.
    """
    ydl_opts: dict[str, Any] = {
        "skip_download": True,
        "noplaylist": True,
        "no_warnings": True,
    }
    ydl_opts.update(config.ytdlp_options())

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(f"yt-dlp probe failed: {e}") from e

    if info is None:
        raise RuntimeError("yt-dlp returned no info for the URL")

    duration = info.get("duration")
    return {
        "url": url,
        "title": info.get("title"),
        "duration": float(duration) if duration is not None else None,
        "extractor": info.get("extractor"),
        "thumbnail": info.get("thumbnail"),
        "is_live": bool(info.get("is_live")),
    }
