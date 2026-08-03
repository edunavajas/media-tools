"""FastAPI application: whisper channel transcription + video download jobs."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import yt_dlp
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from app import config, workers
from app.auth import require_token
from app.downloader import SUPPORTED_SITES, SUPPORTED_SITES_NOTE
from app.jobs_store import JobStore
from app.schemas import (
    DownloadRequest,
    JobCreated,
    JobDetail,
    JobSummary,
    SupportedSitesResponse,
    WhisperJobRequest,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_store: Optional[JobStore] = None
_cleaner: Optional[workers.TtlCleaner] = None


def get_store() -> JobStore:
    if _store is None:
        raise RuntimeError("job store not initialized")
    return _store


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store, _cleaner
    data_dir = config.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    _store = JobStore(data_dir / "jobs.db")
    recovered = _store.recover_interrupted_jobs()
    if recovered:
        logger.warning(f"Recovered {recovered} interrupted jobs -> failed")
    _cleaner = workers.TtlCleaner(_store)
    _cleaner.start()
    yield
    _cleaner.stop()
    workers.shutdown_pools()
    _store.close()
    _store = None


app = FastAPI(title="media-tools", lifespan=lifespan)

AUTH = [Depends(require_token)]


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "ytdlp_version": yt_dlp.version.__version__}


# ---------------------------------------------------------------- jobs (whisper)


@app.post("/jobs/whisper", response_model=JobCreated, status_code=202,
          dependencies=AUTH)
def create_whisper_job(req: WhisperJobRequest) -> JobCreated:
    store = get_store()
    job_id = store.create_job(
        "whisper",
        {
            "channel_url": req.channel_url,
            "language": req.language,
            "max_videos": req.max_videos,
        },
        config.data_dir() / "whisper" / "pending",
    )
    # Final output dir is namespaced by job id.
    store.set_output_dir(job_id, config.data_dir() / "whisper" / job_id)
    workers.submit(store, job_id, "whisper")
    return JobCreated(job_id=job_id)


@app.get("/jobs", response_model=list[JobSummary], dependencies=AUTH)
def list_jobs(
    kind: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
) -> list[dict]:
    return get_store().list_jobs(kind=kind, status=status)


@app.get("/jobs/{job_id}", response_model=JobDetail, dependencies=AUTH)
def get_job(job_id: str) -> dict:
    job = get_store().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/jobs/{job_id}/download", dependencies=AUTH)
def download_channel_json(job_id: str) -> FileResponse:
    job = get_store().get_job(job_id)
    if job is None or job["kind"] != "whisper":
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] == "expired":
        raise HTTPException(status_code=410, detail="job expired and files deleted")
    if job["status"] in ("queued", "running"):
        raise HTTPException(status_code=409, detail="job is still running")
    channel_json = Path(job["output_dir"]) / "channel.json"
    if not channel_json.exists():
        raise HTTPException(status_code=404, detail="channel.json not found")
    return FileResponse(channel_json, media_type="application/json",
                        filename="channel.json")


# ---------------------------------------------------------------- download


@app.post("/download", response_model=JobCreated, status_code=202,
          dependencies=AUTH)
def create_download_job(req: DownloadRequest) -> JobCreated:
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="only http/https URLs")
    store = get_store()
    job_id = store.create_job(
        "download", {"url": req.url}, config.data_dir() / "download" / "pending"
    )
    store.set_output_dir(job_id, config.data_dir() / "download" / job_id)
    workers.submit(store, job_id, "download")
    return JobCreated(job_id=job_id)


@app.get("/download/supported", response_model=SupportedSitesResponse,
         dependencies=AUTH)
def supported_sites() -> SupportedSitesResponse:
    return SupportedSitesResponse(
        sites=SUPPORTED_SITES, note=SUPPORTED_SITES_NOTE
    )


def _get_download_job(job_id: str) -> dict:
    job = get_store().get_job(job_id)
    if job is None or job["kind"] != "download":
        raise HTTPException(status_code=404, detail="download job not found")
    return job


@app.get("/download/{job_id}", dependencies=AUTH)
def get_download_job(job_id: str) -> dict:
    job = _get_download_job(job_id)
    progress = job["progress"]
    return {
        "job_id": job["id"],
        "status": job["status"],
        "percent": progress.get("percent"),
        "metadata": progress.get("metadata"),
        "error": job["error"],
    }


@app.get("/download/{job_id}/file", dependencies=AUTH)
def get_download_file(job_id: str) -> FileResponse:
    job = _get_download_job(job_id)
    if job["status"] == "expired":
        raise HTTPException(status_code=410, detail="job expired and files deleted")
    if job["status"] in ("queued", "running"):
        raise HTTPException(status_code=409, detail="download is still running")
    output_dir = Path(job["output_dir"])
    mp4s = sorted(output_dir.glob("*.mp4")) if output_dir.exists() else []
    if not mp4s:
        raise HTTPException(status_code=404, detail="downloaded file not found")
    return FileResponse(
        mp4s[0], media_type="video/mp4", filename=mp4s[0].name
    )
