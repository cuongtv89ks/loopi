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
4. `docker compose up -d --build`
5. `./smoke.sh` (waits up to 60 s for the stack to become ready) → must print `OK: smoke test passed`.
   (`LOOPI_DATA_DIR` is read from `deploy/.env` if not exported; falls back to `/srv/loopi`.)
   It also checks the stack through an IP-literal URL (`https://<LAN IP>:8443`, no TLS SNI), the way devices and browsers
   connect. The IP defaults to the first `hostname -I` address, as in step 3; set `LOOPI_SMOKE_IP` if the cert holds another one.
6. Browser: import `/srv/loopi/pki/ca.crt` into the OS trust store, then open `https://<LAN IP>:8443/`
   and log in with `LOOPI_WEB_USER`.
7. Devices page → "Tạo token ghép cặp" → copy `pair_token` and `ca_fingerprint` into the device's `loopi.toml`.
