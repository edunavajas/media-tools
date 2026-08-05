FROM denoland/deno:bin-2.8.3 AS deno

FROM python:3.12-slim

COPY --from=deno /deno /usr/local/bin/deno

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENV DATA_DIR=/data
ENV YTDLP_JS_RUNTIME=deno
VOLUME /data
EXPOSE 8000

ENTRYPOINT ["/srv/entrypoint.sh"]
