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
- `docker compose logs -f worker` follows the pipeline; `docker compose exec worker loopi-brain worker-health`
  checks its heartbeat.
