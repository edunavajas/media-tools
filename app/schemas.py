"""Pydantic request/response schemas for the media-tools API."""
from typing import Any, Optional

from pydantic import BaseModel, Field


class WhisperJobRequest(BaseModel):
    channel_url: str
    language: str = "es"
    max_videos: Optional[int] = Field(default=None, ge=1, le=500)


class DownloadRequest(BaseModel):
    url: str


class JobCreated(BaseModel):
    job_id: str
    status: str = "queued"


class JobSummary(BaseModel):
    id: str
    kind: str
    status: str
    created_at: str
    updated_at: str
    finished_at: Optional[str] = None


class JobDetail(JobSummary):
    params: dict[str, Any]
    progress: dict[str, Any]
    error: Optional[str] = None
    output_dir: str


class TranscriptSegment(BaseModel):
    start: Optional[float] = None
    end: Optional[float] = None
    text: Optional[str] = None

    model_config = {"extra": "allow"}


class Transcript(BaseModel):
    video_id: str
    title: str
    text: str
    segments: list[dict[str, Any]] = []
    language: Optional[str] = None
    duration: Optional[float] = None


class ChannelVideoExport(BaseModel):
    video_id: str
    title: str
    url: str
    upload_date: Optional[str] = None
    duration: Optional[float] = None
    view_count: Optional[int] = None
    transcript: Optional[Transcript] = None
    error: Optional[str] = None


class ChannelExport(BaseModel):
    channel: Optional[str] = None
    channel_id: Optional[str] = None
    generated_at: str
    language: str
    videos: list[ChannelVideoExport]


class SupportedSite(BaseModel):
    name: str
    domains: list[str]


class SupportedSitesResponse(BaseModel):
    sites: list[SupportedSite]
    note: str
