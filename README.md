# media-tools

FastAPI microservice for two media jobs, built for edunavajas-hub
(issue edunavajas/edunavajas-hub#59):

- **whisper**: index a YouTube channel with yt-dlp, download each video's
  audio and transcribe it against a [speaches](https://github.com/speaches-ai/speaches)
  (faster-whisper) server, and consolidate everything into a `channel.json`.
- **download**: download a single video as mp4 from any yt-dlp supported
  site (~1000+), with progress tracking.

The whisper pipeline is vendored and adapted from
[yt-channel-whisper](https://github.com/edunavajas/yt-channel-whisper)
(`tools/channel_index.py`, `tools/transcribe_channel.py`): CLI scripts with
`sys.exit` were converted into exception/return-value based functions safe
to run inside worker threads.

## Layout

```
app/
├── main.py              # FastAPI app + endpoints
├── auth.py              # bearer dependency (fail-closed)
├── config.py            # env configuration
├── schemas.py           # pydantic models
├── jobs_store.py        # SQLite job store (WAL, single locked connection)
├── workers.py           # thread pools, job runners, TTL cleaner
├── whisper_pipeline.py  # vendored indexing/download/transcription logic
├── downloader.py        # yt_dlp Python API download + supported sites
└── consolidate.py       # channel.json builder
tests/                   # pytest, fully mocked (no network)
```

## API

All endpoints require `Authorization: Bearer $MEDIA_TOOLS_TOKEN` except
`GET /health`. If `MEDIA_TOOLS_TOKEN` is unset the service fails closed
(500 on protected routes).

| Endpoint | Description |
|---|---|
| `GET /health` | `{status, ytdlp_version}` |
| `POST /jobs/whisper` | `{channel_url, language="es", max_videos?: 1-500}` → `202 {job_id}` |
| `GET /jobs?kind=&status=` | List jobs |
| `GET /jobs/{id}` | Job detail + progress + error |
| `GET /jobs/{id}/download` | `channel.json` (409 running, 410 expired) |
| `POST /download` | `{url}` (http/https only) → `202 {job_id}` |
| `GET /download/{id}` | Download state + metadata |
| `GET /download/{id}/file` | mp4 attachment (409 running, 410 expired) |
| `GET /download/supported` | Curated supported-sites list |

### Job lifecycle

`queued → running → done|failed`, and `done|failed → expired` 24h after
`finished_at` (a daemon cleaner thread deletes the output dir hourly).
On startup, jobs left in `queued|running` are marked `failed` with
`"service restarted"`.

Whisper progress: `{total, done, failed: [{video_id,title,error}], current}`.
Partial failures do not fail the job: failed videos appear in
`progress.failed` and in `channel.json` with `transcript: null` + `error`.

Download progress: `{percent, downloaded_bytes, total_bytes, speed, eta,
filename, metadata: {title, extractor, duration, filesize, thumbnail}}`.

## Configuration

See `.env.example`. Highlights:

- `MEDIA_TOOLS_TOKEN` (required, fail-closed)
- `SPEACHES_BASE_URL`, `SPEACHES_ENDPOINT=/v1/audio/transcriptions`,
  `SPEACHES_MODE=openai` (falls back to the generic `/transcribe`
  endpoint), `SPEACHES_MODEL`, `TIMEOUT_SECONDS=1800`
- `DATA_DIR=/data` (SQLite `jobs.db` + job outputs)
- `YTDLP_AUTOUPDATE=1` upgrades yt-dlp at container start (YouTube
  extraction breaks often; the pinned version is in `requirements.txt`)
- `WORKER_MODE=sync` runs jobs inline (used by the test suite)

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
MEDIA_TOOLS_TOKEN=secret SPEACHES_BASE_URL=http://localhost:8000 \
  .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Docker:

```bash
docker build -t media-tools .
docker run -p 8000:8000 -v media-tools-data:/data \
  -e MEDIA_TOOLS_TOKEN=secret -e SPEACHES_BASE_URL=http://speaches:8000 \
  media-tools
```

`docker-compose.yml` spins up the app plus a CPU speaches instance for
local testing.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/pytest
```

Everything is mocked (no network). `WORKER_MODE=sync` makes jobs run
inline inside the test client.

## Verified end-to-end

- Real whisper job against a local CPU speaches container
  (`ghcr.io/speaches-ai/speaches:latest-cpu`, model
  `Systran/faster-whisper-tiny` — pull it first with
  `POST /v1/models/Systran/faster-whisper-tiny`, speaches 404s on
  unknown models) with `max_videos=2` on NASA's public Twitch clips page
  `https://www.twitch.tv/nasa/clips?filter=clips&range=all` (all clips
  <= 60s) → valid `channel.json` with non-empty `transcript.text` and
  timestamped segments.
  Note: YouTube media downloads are bot-check blocked from datacenter
  IPs (indexing works, `bestaudio` downloads get "Sign in to confirm
  you're not a bot"); the job then completes with partial failures in
  `progress.failed` and `transcript: null` entries, as designed.
  Any yt-dlp-supported channel URL works as `channel_url`.
- Real mp4 download of the public-domain clip
  `https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4`
  (Big Buck Bunny, 10s) → valid h264/mp4 per `ffprobe`, full metadata.
