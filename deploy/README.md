# Loopi brain — deployment

Requirements: Docker with Compose v2 on the Linux PC, port 8443 reachable on the LAN.

1. `cp deploy/.env.example deploy/.env` and fill in the values (password hash command is in the file).
   Generate `LOOPI_DB_PASSWORD` with `openssl rand -hex 24` (hex only: it is embedded unencoded in the database URL, so `/`, `@` or `:` would break it).
   Set `LOOPI_UID` / `LOOPI_GID` to the output of `id -u` / `id -g`: the api container runs as that user, so files in the data dir belong to you.
2. Create the data dir: `sudo mkdir -p /srv/loopi && sudo chown "$USER" /srv/loopi`.
3. Create the CA and server cert (use the PC's LAN IP; keep `localhost` for the smoke test):
   `cd deploy && docker compose run --rm --no-deps api loopi-brain init-pki --host localhost --host brain.lan --ip "$(hostname -I | cut -d' ' -f1)"`
   (The `hostname -I` detection is Linux-only; on macOS use `--ip "$(ipconfig getifaddr en0)"`.)
   Write down the printed CA fingerprint.
4. Download the AI models before the pipeline starts (≈ 11.5 GB, once): `minicpm-v4.6:1b` 1.6 GB, `qwen3:8b` 5.2 GB,
   `qwen3:4b` 2.5 GB and `qwen3-embedding:0.6b` 0.6 GB go into the `ollama` volume, Whisper large-v3-turbo
   (≈ 1.6 GB) into `$LOOPI_DATA_DIR/models/whisper`. `deploy/providers.yaml` (committed, no secrets) names them.
   `docker compose up -d models && docker compose run --rm --no-deps worker loopi-brain models pull`
   (Without them the worker still extracts keyframes and the first transcription downloads Whisper itself;
   captions and embeddings simply wait, and the "Xử lý" page lists the missing models.)
5. `docker compose up -d --build` — starts `db`, `api`, `worker` (pipeline jobs), `models` (Ollama) and `proxy`.
6. `./smoke.sh` (waits up to 60 s for the stack to become ready) → must print `OK: smoke test passed`.
   (`LOOPI_DATA_DIR` is read from `deploy/.env` if not exported; falls back to `/srv/loopi`.)
   It also checks the stack through an IP-literal URL (`https://<LAN IP>:8443`, no TLS SNI), the way devices and browsers
   connect. The IP defaults to the first `hostname -I` address, as in step 3; set `LOOPI_SMOKE_IP` if the cert holds another one.
   It also uploads a 5 s test video (static colour bars) and waits for the worker to analyse it and extract its
   keyframe (90 s; behind a backlog of older recordings set e.g. `SMOKE_ANALYSIS_TIMEOUT=600`).
   On every exit, passed or failed, it first tries to delete the smoke device's moments and items (their keyframes
   and transcripts go with them) and its `raw/`, `view/` and `keyframes/` media directories, and then always tries
   to revoke the device: by id, or by certificate fingerprint when the script stopped before it had looked the id
   up. A failed deletion does not stop the revocation. A step that fails (for example because the stack is down)
   prints a `WARN:` line and the script keeps its exit status; revoke a leftover `smoke` device on the Devices page.
7. Browser: import `/srv/loopi/pki/ca.crt` into the OS trust store, then open `https://<LAN IP>:8443/`
   and log in with `LOOPI_WEB_USER`.
8. Devices page → "Tạo token ghép cặp" → copy `pair_token` and `ca_fingerprint` into the device's `loopi.toml`.

## Pipeline (P2a)

- The "Xử lý" page (`/pipeline`) shows the backlog, the recording → caption/transcript latency of the last 24 h,
  whether Ollama is reachable and has every model, and the jobs that failed 4 times, each with a "Xử lý lại" button
  (the embedding run has none: it restarts by itself every minute).
- Upgrading from P1: the migration marks every existing recording as pending, so the worker analyses the
  recordings made before the upgrade too; on a CPU-only PC that backlog drains over hours.
- Model server down: captions and embeddings wait and resume by themselves; uploads are never affected.
- Audio level (spec D19): the device's microphone records speech very quietly, so the worker raises each recording
  before VAD and Whisper (the 99.9th percentile of the samples to -6 dBFS, at most `LOOPI_ASR_MAX_GAIN_DB`, default
  30 dB, and never a quiet room's hiss above -40 dBFS). Only the decoded copy in memory changes; raw files stay as recorded.
- Audio playback: the timeline plays `/audio/<item id>.opus`, a copy raised by the same rule through a -1 dBFS limiter,
  but always with a fixed 30 dB ceiling: `LOOPI_ASR_MAX_GAIN_DB` affects only the transcription, never playback.
  The api makes it on the first play and keeps it under `view/` next to the video MP4s; if ffmpeg cannot read the
  recording, the raw file plays instead (a `plays at its raw level` warning in `docker compose logs api`). Delete
  `view/**/*.opus` to rebuild the cached copies.
- `docker compose logs -f worker` follows the pipeline; `docker compose exec worker loopi-brain worker-health`
  checks its heartbeat.

## Understanding and pages (P2b)

- **Hôm nay** (`/`, any day with `/?day=YYYY-MM-DD`): the day's recap, the "việc cần làm hoặc đã hứa" list (Xong /
  Bỏ qua / Mở lại), then the chapters in order with their pictures (320 px thumbnails, made on first view under
  `thumbs/`), marked moments and speech (with playback while the raw audio is kept). "Chi tiết theo giờ" (`/hours`)
  is the P2a hourly view.
- Chapters are built every hour (minute 5) by `qwen3:4b`; a day's chapters settle ("closed") once everything recorded
  up to them is analysed and 30 minutes old. The recap (`qwen3:8b`) is written at 23:30 (again at 23:30 if
  "Tạo lại recap" wrote one earlier), and for an earlier day as soon as all its chapters are settled, and once more
  if its chapters change afterwards. Days analysed before this upgrade get chapters one day per hour, newest first,
  then their recaps. A failed recap shows on its Day page and on "Xử lý".
- The worker runs 3 jobs at once (`LOOPI_WORKER_CONCURRENCY`); chapter and recap jobs take at most one slot and
  wait behind captions and transcriptions (lowest priority), so live recordings keep their latency.
- **Tìm kiếm** (`/search`): plain search by meaning and by words; "Hỏi" answers from the matching moments only, cites
  them, and streams the answer (on a CPU the first words take tens of seconds). No matching moment: "Không tìm thấy".
- **Cài đặt** (`/settings`): providers (read only; edit `deploy/providers.yaml`, then
  `docker compose -p deploy up -d --force-recreate api worker`: the file is bind-mounted, so plain `up -d` sees no
  change and the worker only reads it at start), how many days raw video and audio are kept (7 and 7 by default, spec D20),
  "Quên khoảng thời gian" (preview, then permanent deletion of everything recorded in the range, at most 7 days at a
  time; an audit row keeps the range and the counts, never content), and reprocessing a day with the current models.
- Retention is **off** after installing: Cài đặt shows how many recordings (and GB) it would delete; once turned on
  there, it runs at 03:00 local time: raw files (and their MP4/playback copies) of finished recordings older than the
  kept days are deleted and the pages say "đã hết hạn". Keyframes, transcripts, chapters and recaps stay. The same
  03:00 run (on or off) retries files a "Quên khoảng thời gian" could not delete at once.
- Memory: Ollama keeps at most 2 models (`OLLAMA_MAX_LOADED_MODELS`). Measured: minicpm-v ≈ 2.9 GB, qwen3-embedding
  ≈ 1.3 GB; `qwen3:8b` with its 8192-token context ≈ 6–6.5 GB and `qwen3:4b` ≈ 3.5 GB, so a recap or a chat raises
  Ollama from ≈ 4.2 GB to ≈ 9.4–10 GB; text models unload 1 minute after their last request. A text request may
  take up to 15 minutes on the CPU (`LOOPI_LLM_TIMEOUT_S`, 900 s).
- CPU: by default Ollama runs a text model on all physical cores. If the recording → caption/transcript latency on
  "Xử lý" goes above 15 minutes while chapters and recaps run, add `LOOPI_LLM_NUM_THREAD: "2"` (threads per
  text-model request; captions and embeddings keep Ollama's default) to the `environment:` of both `api` and `worker`
  in `deploy/docker-compose.yml`, then `docker compose -p deploy up -d --force-recreate --no-deps api worker`.
  Recaps and chapters then take longer; unset, Ollama decides.
- Optional Anthropic text provider: set `ANTHROPIC_API_KEY` in `deploy/.env` and route a text task to `anthropic` in
  `deploy/providers.yaml` (examples there). Only text goes to the cloud provider (including captions and transcripts
  derived from your recordings); images and audio are never sent. Without the key those routes are skipped.
  A cloud fallback (e.g. `recap: ollama → anthropic`) is used whenever the local step fails, not only when Ollama is
  down: also when the local model errors or runs past `LOOPI_LLM_TIMEOUT_S` (15 minutes by default) under load, so
  on a busy CPU that task's text (for a recap: the day's transcripts and captions) goes to Anthropic.
