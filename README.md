# media-tools

Microservicio **FastAPI** para tres trabajos con vídeo/audio. Nace como pieza del sistema self-hosted de "[Mi Empleado Digital](https://www.youtube.com/@DevKnives)" y es el motor de transcripción que alimenta la base de conocimiento del canal.

- **whisper** — indexa un canal de YouTube con `yt-dlp`, descarga el audio de cada vídeo y lo transcribe con un modelo Whisper (vía API compatible con OpenAI). Lo consolida todo en un `channel.json`.
- **download** — descarga un vídeo como mp4 desde cualquier sitio soportado por `yt-dlp` (1000+), con progreso.
- **transcript** — transcripción de una **URL suelta** desde cualquier sitio soportado por `yt-dlp`. Usa los subtítulos existentes del vídeo si los hay y, si no, cae al pipeline de Whisper. Genera `transcript.txt`, `transcript.json` y `transcript.srt`.

El pipeline de Whisper está adaptado de **[yt-channel-whisper](https://github.com/edunavajas/yt-channel-whisper)** (`tools/channel_index.py`, `tools/transcribe_channel.py`): los scripts de CLI con `sys.exit` se convirtieron en funciones con excepciones, seguras para correr dentro de hilos de trabajo.

---

## Para qué sirve dentro de la serie

En el Episodio 2 el objetivo es darle "memoria" a la documentación. Para la demo del canal, `media-tools` es el que convierte los vídeos en texto:

```
yt-channel-whisper (enumera el canal)
        │
        ▼
media-tools  ──(Whisper)──▶  transcripciones
        │
        ▼
Markdown con frontmatter  ──▶  n8n + pgvector  ──▶  el agente lo consulta por MCP
```

Puedes usar `media-tools` de dos formas: por su **API** (arriba) o reutilizando directamente las herramientas de `yt-channel-whisper` en local.

---

## Estructura

```
app/
├── main.py              # App FastAPI + endpoints
├── auth.py              # Dependencia bearer (fail-closed)
├── config.py            # Configuración por entorno
├── schemas.py           # Modelos pydantic
├── jobs_store.py        # Store de jobs en SQLite (WAL, conexión única)
├── workers.py           # Pools de hilos, runners de jobs, limpieza TTL
├── whisper_pipeline.py  # Lógica de indexado/descarga/transcripción
├── downloader.py        # Descarga con yt-dlp + sitios soportados
├── consolidate.py       # Constructor de channel.json
└── transcript.py        # Transcripción de una URL suelta
tests/                   # pytest, todo mockeado (sin red)
```

---

## API

Todas las rutas requieren `Authorization: Bearer $MEDIA_TOOLS_TOKEN` salvo `GET /health`. Si `MEDIA_TOOLS_TOKEN` no está definido, el servicio falla cerrado (500 en las rutas protegidas).

| Endpoint | Descripción |
|---|---|
| `GET /health` | `{status, ytdlp_version}` |
| `POST /jobs/whisper` | `{channel_url, language="es", max_videos?: 1-500}` → `202 {job_id}` |
| `GET /jobs?kind=&status=` | Lista de jobs |
| `GET /jobs/{id}` | Detalle + progreso + error |
| `GET /jobs/{id}/download` | `channel.json` (409 si corre, 410 si expiró) |
| `POST /download` | `{url}` (solo http/https) → `202 {job_id}` |
| `GET /download/{id}` | Estado + metadatos de la descarga |
| `GET /download/{id}/file` | mp4 como adjunto (409/410) |
| `GET /download/supported` | Lista curada de sitios soportados |
| `POST /transcript` | `{url, language="es"}` (solo http/https) → `202 {job_id}` |
| `GET /transcript/{id}` | Estado + metadatos (`source: subtitles\|whisper`) |
| `GET /transcript/{id}/file?format=srt\|txt\|json` | Fichero de transcripción (409/410) |

### Ciclo de vida de un job

`queued → running → done|failed`, y `done|failed → expired` 2 h después de `finished_at` (el limpiador corre al arrancar y cada minuto). Un mp4 completado se borra cuando su descarga llega a EOF; las descargas interrumpidas conservan el fichero hasta el TTL. Las transcripciones quedan disponibles hasta ese mismo TTL. Al arrancar, los jobs en `queued|running` se marcan como `failed` con `"service restarted"`.

- **Progreso de Whisper:** `{total, done, failed: [{video_id,title,error}], current}`. Los fallos parciales no tumban el job: aparecen en `progress.failed` y en el `channel.json` como `transcript: null` + `error`.
- **Progreso de descarga:** `{percent, downloaded_bytes, total_bytes, speed, eta, filename, metadata}`.

---

## Configuración

Ver `.env.example`. Lo importante:

| Variable | Para qué |
|---|---|
| `MEDIA_TOOLS_TOKEN` | Bearer obligatorio. Si falta, el servicio falla cerrado. |
| `TRANSCRIPTION_PROVIDER` | `nan` (por defecto) o `groq`. |
| `NAN_BASE_URL`, `NAN_API_KEY`, `NAN_MODEL` | Proveedor Nan Builders (compatible con OpenAI). |
| `GROQ_API_KEY`, `GROQ_MODEL` | Proveedor Groq (`whisper-large-v3-turbo` por defecto). |
| `TIMEOUT_SECONDS` | Timeout por petición de transcripción (1800 por defecto). |
| `DATA_DIR` | Directorio de datos (SQLite `jobs.db` + salidas). |
| `YTDLP_AUTOUPDATE` | `1` actualiza `yt-dlp` al arrancar (YouTube cambia a menudo). |
| `YTDLP_COOKIES_FILE` | Cookies de una sesión de YouTube. **Recomendado**: sin ellas, desde IPs de datacenter YouTube responde "Sign in to confirm you're not a bot". |
| `YTDLP_PROXY` | Proxy opcional para `yt-dlp`. |
| `YTDLP_JS_RUNTIME` | Runtime JS para resolver los retos de YouTube (`deno` viene en la imagen). |
| `WORKER_MODE` | `sync` ejecuta los jobs en línea (solo tests). |

---

## Arrancar

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
MEDIA_TOOLS_TOKEN=secret NAN_API_KEY=... \
  .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

La transcripción usa Nan Builders por defecto (`NAN_BASE_URL`, `NAN_API_KEY`, `NAN_MODEL=whisper`), que alcanza `POST /audio/transcriptions` con Bearer. Para usar Groq, pon `TRANSCRIPTION_PROVIDER=groq` y `GROQ_API_KEY`.

Docker:

```bash
docker build -t media-tools .
docker run -p 8000:8000 -v media-tools-data:/data \
  -e MEDIA_TOOLS_TOKEN=secret -e NAN_API_KEY=... \
  media-tools
```

---

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/pytest
```

Todo está mockeado (sin red). `WORKER_MODE=sync` hace que los jobs corran en línea dentro del cliente de test.

---

## Verificado end-to-end

- Job real de whisper con `max_videos=2` sobre una página pública de clips (`https://www.twitch.tv/nasa/clips?filter=clips&range=all`) → `channel.json` válido con `transcript.text` no vacío y segmentos con marcas de tiempo.
  *Nota:* desde IPs de datacenter, YouTube bloquea la descarga de medios (el indexado funciona, pero `bestaudio` da "Sign in to confirm you're not a bot"). El job termina con fallos parciales en `progress.failed` y entradas `transcript: null`, como está diseñado. Cualquier URL soportada por `yt-dlp` vale como `channel_url`.
- Descarga real de mp4 de un clip de dominio público (`test-videos.co.uk`, Big Buck Bunny, 10 s) → h264/mp4 válido según `ffprobe`, con metadatos completos.

---

## Licencia

MIT © Eduardo Navajas
