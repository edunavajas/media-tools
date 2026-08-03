#!/bin/sh
set -e

if [ "$YTDLP_AUTOUPDATE" = "1" ]; then
    echo "YTDLP_AUTOUPDATE=1: upgrading yt-dlp..."
    pip install --no-cache-dir -U yt-dlp
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
