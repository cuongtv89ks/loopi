# Pre-flight scan: P1a plan (protocol, brain ingest, web)

Plan: `docs/superpowers/plans/2026-10-14-p1a-protocol-brain-ingest.md` (3027 lines, 9 tasks). Spec: `docs/superpowers/specs/2026-10-14-loopi-design.md`.
Date: 2026-10-14. Machine: macOS arm64, Docker via colima.

Method. The plan's own code blocks were extracted mechanically (no authoring) into a scratch project outside the repo and executed task by task, then the Dockerfile was built and the plan's Caddyfile, api, db and `smoke.sh` were run end to end in containers. "(run)" marks a result I observed rather than inferred. Nothing in the repo or the plan was changed.

Headline results (run): full suite 70 passed, 1 failed, coverage 95.45% (gate 80%). The single failure is F1. Docker image builds. Caddy 2.10 accepts the Caddyfile. `smoke.sh` passes against real Caddy (Review Focus 1 spoof step returns 401), after patching only the `docker compose exec` token line.

## A. Task pairs that share a file or an interface

| # | Tasks | Produces vs consumes (exact names) | Finding |
|---|---|---|---|
| A1 | 1-2 | `Settings.pki_dir` env `LOOPI_PKI_DIR` vs `cli.py` default `os.environ.get("LOOPI_PKI_DIR")` | OK |
| A2 | 1-3 | tables `device, pair_token, device_command` (cols id, cert_fingerprint, revoked, last_heartbeat, last_heartbeat_at, dropped_total, expires_at, used_at, acked_at) vs SQL in `devices.py` 893-1020; `pool` fixture TRUNCATE list (conftest 197) covers every FK table | OK (run, 15 passed) |
| A3 | 1-4 | `Settings`, `make_pool` (dict rows), fixtures `settings`->`pki_settings`->`client`; `create_app(settings, pool)` | OK (run) |
| A4 | 1-5 | `item` columns and CHECKs (kind, status in stored/ready/error, item_time_order) vs the 11-column INSERT at 1877-1881; `Settings.media_root/min_free_bytes/max_item_bytes/default_video_fps` | OK (run) |
| A5 | 1-6 | `item.view_path/status/error` vs `remux_item` UPDATEs 2096-2103 | OK |
| A6 | 1-7 | `device.last_heartbeat` (JSONB), `dropped_total` vs `record_heartbeat` 996-999 | OK |
| A7 | 1-8 | `item(id,kind,started_at,ended_at,status,view_path,meta)` and `device` columns vs SELECTs 2446, 1017 | OK |
| A8 | 1-9 | `migrate()` returns list, CLI prints "database up to date"; `make_pool` | OK (run, 3 passed) |
| A9 | 2-3 | `pki.sign_csr(pki_dir, csr_pem, device_id)`, `pki.fingerprint(cert_pem)`, `PkiError` rolls the transaction back and keeps the token usable; `helpers.make_csr_pem` | OK |
| A10 | 2-4 | `sign_csr`, `ca_cert_pem`, `fingerprint`, `PkiError` mapped to 422 in `pair` (1491) | OK |
| A11 | 2-8 | `ca_cert_pem`, `fingerprint` in `_ca_fingerprint` (2460) | OK |
| A12 | 2-9 | `cli.py` created in Task 2, edited in Task 9 (imports, 3 `add_parser` lines, `main` replaced); `pair-token` prints `pki.fingerprint` on line 3 | OK (run) |
| A13 | 2-9 | `tests/test_cli.py` created in 2, extended in 9 (3 tests total, "3 passed" at 2803 is right) | OK (run) |
| A14 | 3-4 | `register_device(pool, token, hw_info, issue_cert)` vs `pair` (token `.strip().upper()`, alphabet is uppercase), `get_active_device` vs `current_device` (lowercases) | OK |
| A15 | 3-5 | `current_device` returns `{id,name}`; tests use `paired_device`, `revoke_device` | OK |
| A16 | 3-6 | `paired_device` used by test_media | OK |
| A17 | 3-7 | `record_heartbeat(pool, device_id, status, acked_ids)` returns rows `{id,type,payload}` vs `Command(**row)`; `dropped_items` is summed, which matches the P1b delta field `dropped_since_heartbeat` | OK |
| A18 | 3-8 | `enqueue_command` raises `psycopg.errors.ForeignKeyViolation` for unknown device (caught 2522); `list_devices` columns vs `devices.html`; `create_pair_token` returns `token, expires_at`; `revoke_device` | OK (run) |
| A19 | 3-9 | `create_pair_token` -> `token["token"]`, `["expires_at"]` in `_pair_token` CLI | OK |
| A20 | 1,3,4 | `tests/conftest.py` created in 1, appended in 3 (`pki_settings`) and 4 (`client`, with `import httpx` mid-file); `tests/helpers.py` created in 2, appended in 3 | OK (run) |
| A21 | 4-5 | `device_routes.py`: Task 5 replaces the 3-line import block at 1893-1900; the old block matches Task 4 lines 1453-1456 exactly | OK (run) |
| A22 | 4-7 | `Heartbeat` pydantic model (2234) vs contract `Heartbeat` schema (1202-1226) | Lenient drift, see F11 |
| A23 | 4-9 | contract paths `/v1/*` vs Caddy `@device path /v1/*`; smoke payloads | OK (run) |
| A24 | 4-6 | `client` fixture used by test_media; `put_item` consumes `current_device`, `check_agent_version` | OK |
| A25 | 4-8 | `app.py`: Task 8 replaces the exact two lines `app.include_router(device_router)` / `return app` (1412-1413) | OK (run) |
| A26 | 5-6 | `put_item` rewritten in Task 6 (2123-2140), `store_item` returns `(status, ack)`; Task 6 runs remux for every 201 video, so Task 5's `test_video_upload_is_stored_then_duplicate` (1572-1581) sees `error`, not `stored` | MISMATCH, see F1 |
| A27 | 5-7 | both append to `device_routes.py`; Task 7's "add Literal + change pydantic import" is compatible with Task 6's fastapi import | OK (run) |
| A28 | 5-8 | item rows -> timeline: audio `view_path == raw_path`, marker `view_path NULL` + `meta.note`, video `stored` shows "Dang xu ly" | OK (run) |
| A29 | 5-9 | smoke marker upload headers vs `parse_header`; 201, 200, 401 | OK (run) |
| A30 | 6-7 | see A27 | OK |
| A31 | 6-8 | `view_path` `view/<dev>/Y/m/d/<id>.mp4` vs `/media` StaticFiles root `media_root` and template `/media/{{ it.view_path }}` | OK (a `stored` item can stay "Dang xu ly" forever, see F7) |
| A32 | 6-9 | Dockerfile `apt-get install ffmpeg`; image has `/usr/bin/ffmpeg` | OK (run) |
| A33 | 7-8 | keys read by `devices.html`: recording_video, recording_audio, paused, audio_enabled, queue_depth, disk_used_pct, soc_temp_c, wifi_rssi_dbm, camera_failures, mic_missing, last_error, all present in `Heartbeat.model_dump` | OK |
| A34 | 7-9 | smoke `HB` body validates and returns 200 | OK (run) |
| A35 | 8-9 | basic auth + `HX-Request` over Caddy: `/devices` 200, bad pw 401, POST without HX 403, with HX 200, forged fingerprint on web path 401, `/static/htmx.min.js` 200 | OK (run) |
| A36 | 2-5, 2-6, 2-7, 3-... remaining pairs | no shared file or interface beyond fixtures already covered above | OK |

## B. Each task agrees with itself

| # | Task | Tests vs code vs commands vs files | Finding |
|---|---|---|---|
| B1 | 1 | names, fixtures, `driver=None`, CheckViolation, idempotent migrate all line up; `uv sync` before `src/` exists still works (run) | OK (run, 2 passed) |
| B2 | 2 | 7 pki tests + 1 cli test = "8 passed" (run); signatures match | OK (run) |
| B3 | 3 | "15 incl. parametrized" is exact (run) | OK (run) |
| B4 | 4 | 10 tests pass (run). Files list (1040) says `api/__init__.py` "(empty)" but no step creates it; namespace import still works | F16 (low) |
| B5 | 5 | 18 tests pass. Step 2 (1692) expects "405 Method Not Allowed" before the route exists; actual is 404 (run) | F5 (low) |
| B6 | 6 | Step 5 (2145) expects `test_items_api.py` all passed; one test fails (run). Duration assert sits on its lower bound | F1, F15 |
| B7 | 7 | 3 tests pass (run) | OK (run) |
| B8 | 8 | 8 tests pass (run); `web/__init__.py` in Files list (2297) has no creating step; `htmx.min.js` downloaded 50 917 bytes (run) | F16 (low) |
| B9 | 9 | CLI tests, Dockerfile build, Caddy adapt, smoke all work (run). Step 9 (3011) uses Linux-only `hostname -I` and `/srv`; `docker compose` missing here | F3 |

## C. Global Constraints and spec

| # | Item | Finding |
|---|---|---|
| C1 | Python `>=3.13` (line 17, 109) | `uv sync` resolved CPython 3.14.4 here (run) while Dockerfile pins 3.13 (2808): tests do not run on the production minor, see F4 |
| C2 | pgvector/pg17 image in tests and compose (196, 2828) | OK |
| C3 | timestamptz UTC, `LOOPI_TZ` default Asia/Seoul; 23:30Z lands on next local day (Review Focus 4) | OK (run) |
| C4 | "responses must validate against the contract" (20) | `ItemAck` never validated, Heartbeat model looser than contract, see F11 |
| C5 | fingerprint header only; empty/unknown/revoked 401; Caddy overwrites header | OK (run end to end with real Caddy, forged header 401 on both `/v1/items` and web routes) |
| C6 | all keys P-256; server cert validity at most 800 days (22) | P-256 OK. Server cert spans 800 d plus 5 min because `not_before` is backdated (551), and the test only asserts at most 825 (431): F10 |
| C7 | pair token `XXXX-XXXX`, alphabet of 32, TTL 30 min, one time | OK (run) |
| C8 | web POST needs `HX-Request: true` else 403 | OK (run, all three POST routes) |
| C9 | UI text Vietnamese (26) | `_queued.html` prints raw slugs "audio-off", "revoked" and says revoke applies "within 15 seconds": F13 |
| C10 | coverage at least 80% | 95.45% (run) |
| C11 | conventional commits | OK |
| C12 | spec §8 status codes: pair 201/401/422; items 201/200/409/413/422/507; heartbeat 200; 426 for unsupported major | Items/heartbeat OK. `/v1/pair` does not enforce 426 and the contract omits it: F12 |
| C13 | spec §8 header names, Fps header video only | OK |
| C14 | spec §8 Caddy: `verify_if_given`, trust pool = brain CA, `/v1/pair` without cert; spec says "subject into header", plan forwards fingerprint (consistent with §9.4 `cert_fingerprint`) | OK (run) |
| C15 | spec §9.1 media path `{raw,...}/YYYY/MM/DD/` vs plan `raw/<device_id>/YYYY/MM/DD/` (1789); spec §9.4 `device.paused, audio_enabled` columns vs plan keeps them in `last_heartbeat` | Spec drift, F14 (low) |
| C16 | spec §10.6 Devices page (pause/resume, audio, revoke; QR pairing) and §14 P1 (raw timeline, pause/resume from web) | OK; QR is not required in P1 (pairing via `loopi.toml`) |
| C17 | spec §12 disk full -> 507, duplicate -> 200, conflict -> 409 | OK (run) |
| C18 | Review Focus 1-5 tests exist under the exact names and pass | OK (run) |
| C19 | P1b/P1c interface expectations (header names, `ca_fingerprint`, heartbeat 15 s vs ONLINE_WINDOW 45 s, `dropped_total`, `queue_depth`, "trực tuyến") | OK |

## D. Things the plan mandates that a reviewer would flag

| # | Item | Finding |
|---|---|---|
| D1 | `test_pair_token_partial_shows_token_and_ca` (2382-2385) | Asserts only the literals `pair_token` and `ca_fingerprint`, which are static text in `_pair_token.html` (2650-2651): passes even if no token or fingerprint is rendered. F6 |
| D2 | duplicated blocks | test_media builds the same header dict twice (2000-2008, 2023-2030) instead of reusing `_headers` (1547); `_token` duplicated (756 vs 1298); `_issuer` lambda duplicates `paired_device` (752 vs 736). F9 |
| D3 | background remux error handling (2064-2104) | Only non-zero ffmpeg exit becomes `MediaError`; `FileNotFoundError` or any DB error escapes the BackgroundTask, item stays `stored` forever; tasks are lost on restart with no sweep. F7 |
| D4 | `_write_private_key` (532-540) | writes then `chmod 0o600`: CA key is briefly readable under default umask. F8 |
| D5 | `test_remux_respects_fps` bound (1984) | measured duration is exactly 2.5 s, on the lower bound. F15 |
| D6 | `_remove_empty_dirs` (1828-1833, 1885) | race: a failing upload can rmdir the day directory a concurrent upload just created, giving 500. Reasoned, not reproduced. F17 |
| D7 | `test_check_agent_version` is sync but takes async `pool` fixture (1365) | works with pytest-asyncio 1.4 (run); OK |
| D8 | `ORDER BY id` relies on ULID order (1008) | python-ulid 4.0.1 produced 0 inversions in 20 000 ids (run); lock pins it. `>=3.0` (119) not checked on 3.0 | OK |

## E. Environment (this machine)

| # | Check | Result |
|---|---|---|
| E1 | docker daemon | colima running (aarch64). BUT no `/var/run/docker.sock` and `DOCKER_HOST` unset, so testcontainers errors (run). F2 |
| E2 | image pulls | `~/.docker/config.json` has `credsStore: desktop`, helper missing: `docker pull` and testcontainers (also Ryuk) fail (run). F2 |
| E3 | with `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock` + `TESTCONTAINERS_RYUK_DISABLED=true` + pre-pulled image | tests run (run) |
| E4 | ffmpeg, ffprobe, libx264 | present; sample generation and remux work; garbage input returns rc 234 (run) |
| E5 | `caddy` on host | not installed; not needed (Caddy runs in Docker, image pulled with the workaround) |
| E6 | uv 0.6.17, deps resolve | fastapi 0.143, starlette 1.8, pytest 9.1, pytest-asyncio 1.4, testcontainers 4.15 (deprecation warning for `testcontainers.postgres` import, harmless) |
| E7 | `docker compose` plugin | missing: `~/.docker/cli-plugins/docker-compose` is a dangling symlink to Docker.app. F3 |
| E8 | Step 9 / README commands | `hostname -I` illegal on macOS; `/srv` is read-only on macOS; `smoke.sh` reads `LOOPI_DATA_DIR` from the shell, not from `.env`. F3 |
| E9 | Dockerfile build | builds (legacy builder); uv 0.5.11 reads the lock written by uv 0.6.17 (run) |
| E10 | Caddy 2.10 | Caddyfile adapts; stack + `smoke.sh` pass (run) |
| E11 | colima `$HOME` mount | writable (run), usable for `LOOPI_DATA_DIR` |

## Non-OK findings (numbered)

- F1 Task 6 breaks a Task 5 test (A26, B6): plan 1572-1581 vs 2138-2139, 2145-2146.
- F2 testcontainers cannot reach or pull on this machine (E1-E3): plan 232-233.
- F3 `docker compose` missing and Linux-only commands in Task 9 (E7, E8): plan 2952, 3011, 2995, 2937.
- F4 local tests run on Python 3.14, image on 3.13 (C1): plan 17, 109, 2808.
- F5 Task 5 Step 2 expected RED text says 405, actual 404: plan 1692.
- F6 weak pair-token test (D1): plan 2382-2385.
- F7 remux failures outside `MediaError` and restarts leave items `stored` forever (D3): plan 2064-2104.
- F8 CA private key permission race (D4): plan 532-540.
- F9 verbatim duplicated test helpers (D2): plan 756, 1298, 752, 2000, 2023.
- F10 server cert 800 d plus 5 min (C6): plan 22, 551, 431.
- F11 contract coverage gaps (C4, A22): plan 20, 1577, 1584, 1206, 2252, 2235.
- F12 `/v1/pair` ignores agent-version 426 (C12): plan 1480, 1061-1077.
- F13 Vietnamese UI gaps in `_queued.html` (C9): plan 2658.
- F14 spec drift: media path layout, missing device columns (C15): plan 1789, 313-323.
- F15 fragile duration bound (D5): plan 1984.
- F16 no step creates `api/__init__.py` and `web/__init__.py` (B4, B8): plan 1040, 2297.
- F17 empty-dir cleanup race (D6): plan 1828-1833, 1885.

Rows checked: 36 (A) + 9 (B) + 19 (C) + 8 (D) + 11 (E) = 83.
