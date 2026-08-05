"""Background job workers: thread pools, job runners and the TTL cleaner.

WORKER_MODE=sync runs jobs inline in the caller thread (used by tests);
otherwise jobs are submitted to per-kind ThreadPoolExecutors.
"""
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app import config, downloader, whisper_pipeline
from app.consolidate import build_channel_json
from app.jobs_store import JobStore
from app.whisper_pipeline import TranscriptionConfig, VideoTask

logger = logging.getLogger(__name__)

whisper_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whisper")
download_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="download")

JOB_TTL_HOURS = 2
CLEANER_INTERVAL_SECONDS = 60

_artifact_lock = threading.Lock()
_active_downloads: set[str] = set()


def _transcription_config(language: str) -> TranscriptionConfig:
    return TranscriptionConfig(
        base_url=config.speaches_base_url(),
        endpoint=config.speaches_endpoint(),
        mode=config.speaches_mode(),
        model=config.speaches_model(),
        language=language,
        timeout=config.timeout_seconds(),
    )


def run_whisper_job(store: JobStore, job_id: str) -> None:
    """Run a whisper job end to end. Never raises: failures mark the job."""
    output_dir: Optional[Path] = None
    try:
        job = store.get_job(job_id)
        if job is None:
            logger.error(f"whisper job {job_id} not found")
            return
        params = job["params"]
        output_dir = Path(job["output_dir"])
        language = params.get("language", "es")
        max_videos = params.get("max_videos")

        store.set_status(job_id, "running")

        videos = whisper_pipeline.index_channel(params["channel_url"])
        if max_videos:
            videos = videos[:max_videos]
        if not videos:
            raise RuntimeError("no videos found in channel")

        store.update_progress(
            job_id, total=len(videos), done=0, failed=[], current=None
        )

        transcription = _transcription_config(language)
        cache_dir = output_dir / ".cache"

        def on_progress(
            task: VideoTask, ok: bool, error: Optional[str]
        ) -> None:
            def _mutate(progress: dict[str, Any]) -> None:
                progress["done"] = int(progress.get("done", 0)) + 1
                progress["current"] = task.video_id
                if not ok:
                    progress.setdefault("failed", []).append({
                        "video_id": task.video_id,
                        "title": task.title,
                        "error": error,
                    })

            store.mutate_progress(job_id, _mutate)

        tasks = [
            VideoTask(
                video_id=v["video_id"],
                title=v.get("title", "Unknown"),
                url=v["url"],
                output_dir=output_dir
                / whisper_pipeline.sanitize_filename(v["video_id"]),
            )
            for v in videos
        ]

        # Inner pool: process up to 2 videos concurrently per job.
        with ThreadPoolExecutor(max_workers=2) as video_pool:
            futures = [
                video_pool.submit(
                    whisper_pipeline.process_video,
                    task,
                    transcription,
                    cache_dir,
                    True,   # delete_audio
                    False,  # skip_existing
                    3,      # retries
                    5.0,    # retry_backoff
                    on_progress,
                )
                for task in tasks
            ]
            results = [f.result() for f in futures]

        errors = {
            video_id: error
            for video_id, ok, error in results
            if not ok and error
        }
        if not any(ok for _, ok, _ in results):
            details = "; ".join(
                f"{video_id}: {error or 'unknown error'}"
                for video_id, _, error in results
            )
            raise RuntimeError(f"all whisper videos failed: {details}")
        build_channel_json(output_dir, videos, language, errors=errors)
        store.set_status(job_id, "done", finished=True)
    except Exception as e:
        logger.exception(f"whisper job {job_id} failed")
        if output_dir is not None:
            shutil.rmtree(output_dir, ignore_errors=True)
        try:
            store.set_status(job_id, "failed", error=str(e), finished=True)
        except Exception:
            logger.exception(f"could not mark whisper job {job_id} as failed")


def run_download_job(store: JobStore, job_id: str) -> None:
    """Run a download job end to end. Never raises: failures mark the job."""
    output_dir: Optional[Path] = None
    try:
        job = store.get_job(job_id)
        if job is None:
            logger.error(f"download job {job_id} not found")
            return
        url = job["params"]["url"]
        output_dir = Path(job["output_dir"])

        store.set_status(job_id, "running")
        store.update_progress(
            job_id,
            percent=0.0,
            downloaded_bytes=0,
            total_bytes=None,
            speed=None,
            eta=None,
            filename=None,
            metadata=None,
        )

        def on_progress(event: dict[str, Any]) -> None:
            store.update_progress(
                job_id,
                percent=event.get("percent"),
                downloaded_bytes=event.get("downloaded_bytes"),
                total_bytes=event.get("total_bytes"),
                speed=event.get("speed"),
                eta=event.get("eta"),
                filename=event.get("filename"),
            )

        final_path, metadata = downloader.download_video(
            url, output_dir, progress_hook=on_progress
        )

        store.update_progress(
            job_id,
            percent=100.0,
            filename=final_path.name,
            metadata=metadata,
        )
        store.set_status(job_id, "done", finished=True)
    except Exception as e:
        logger.exception(f"download job {job_id} failed")
        if output_dir is not None:
            shutil.rmtree(output_dir, ignore_errors=True)
        try:
            store.set_status(job_id, "failed", error=str(e), finished=True)
        except Exception:
            logger.exception(f"could not mark download job {job_id} as failed")


def submit(store: JobStore, job_id: str, kind: str) -> None:
    """Dispatch a job to its runner, inline (sync) or via the pools."""
    runner = run_whisper_job if kind == "whisper" else run_download_job
    if config.worker_mode() == "sync":
        runner(store, job_id)
    else:
        pool = whisper_pool if kind == "whisper" else download_pool
        pool.submit(runner, store, job_id)


def begin_download(
    store: JobStore, job_id: str
) -> tuple[Optional[Path], str]:
    """Reserve a completed download while its file is streamed to one client."""
    with _artifact_lock:
        job = store.get_job(job_id)
        if job is None or job["kind"] != "download":
            return None, "not_found"
        if job["status"] != "done":
            return None, job["status"]
        if job_id in _active_downloads:
            return None, "busy"
        output_dir = Path(job["output_dir"])
        mp4s = sorted(output_dir.glob("*.mp4")) if output_dir.exists() else []
        if not mp4s:
            return None, "missing"
        _active_downloads.add(job_id)
        return mp4s[0], "ready"


def finish_download(job_id: str, output_dir: Path, completed: bool) -> None:
    """Release a download and remove its artifact only after EOF."""
    with _artifact_lock:
        _active_downloads.discard(job_id)
        if completed:
            shutil.rmtree(output_dir, ignore_errors=True)


def cleanup_expired_jobs(store: JobStore, ttl_hours: int = JOB_TTL_HOURS) -> int:
    """Expire done|failed jobs older than the TTL and delete their files."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    ).isoformat()
    with _artifact_lock:
        expired = store.expire_finished_jobs(
            cutoff, exclude_ids=set(_active_downloads)
        )
        for job in expired:
            output_dir = Path(job["output_dir"])
            if output_dir.exists():
                shutil.rmtree(output_dir, ignore_errors=True)
                logger.info(f"Expired job {job['id']}: removed {output_dir}")
    return len(expired)


class TtlCleaner(threading.Thread):
    """Daemon thread expiring jobs every CLEANER_INTERVAL_SECONDS."""

    def __init__(self, store: JobStore):
        super().__init__(daemon=True, name="ttl-cleaner")
        self._store = store
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(CLEANER_INTERVAL_SECONDS):
            try:
                cleanup_expired_jobs(self._store)
            except Exception:
                logger.exception("TTL cleaner iteration failed")

    def stop(self) -> None:
        self._stop_event.set()


def shutdown_pools() -> None:
    whisper_pool.shutdown(wait=False, cancel_futures=True)
    download_pool.shutdown(wait=False, cancel_futures=True)
