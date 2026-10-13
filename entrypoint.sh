#!/bin/sh
set -e

if [ "$YTDLP_AUTOUPDATE" = "1" ]; then
    echo "YTDLP_AUTOUPDATE=1: upgrading yt-dlp..."
    # Best-effort: a PyPI outage must not crash-loop the container. The image
    # already ships a pinned yt-dlp, so falling back to it is safe.
    # [default] keeps yt-dlp-ejs (JS challenge runtime) locked to yt-dlp.
    pip install --no-cache-dir -U "yt-dlp[default]" \
        || echo "WARN: yt-dlp autoupdate failed; using image version"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
