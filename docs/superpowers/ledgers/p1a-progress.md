# SDD ledger — plan: docs/superpowers/plans/2026-10-14-p1a-protocol-brain-ingest.md

Branch: feat/phase1 (from main 2d2db5d). Spec: docs/superpowers/specs/2026-10-14-loopi-design.md
Ruling: work on branch feat/phase1 instead of a worktree — repo has no origin remote and user did not ask for a worktree — cost if wrong: none, branch is isolated from main

## Pre-flight scan (83 rows: preflight.md in this workspace; plan code was trial-run: 70/71 tests pass, 95% coverage, smoke passes)
Env: tests/builds source .superpowers/sdd/env/env.sh (colima DOCKER_HOST, TESTCONTAINERS_RYUK_DISABLED, empty DOCKER_CONFIG, rustup PATH).
Ruling: R1 Task 6 adds an autouse fixture in tests/test_items_api.py that patches the remux background call to a no-op — otherwise Task 5's "stored" assertion fails once remux runs on garbage bodies — cost if wrong: none (remux has its own tests)
Ruling: R2 run testcontainers against colima via env.sh, no repo change — host has no /var/run/docker.sock and a broken desktop credsStore — cost if wrong: other hosts need their own docker env
Ruling: R3 Task 9 scripts stay Linux-targeted (brain host is a Linux PC); smoke.sh reads LOOPI_DATA_DIR from deploy/.env; local verification uses brew docker-compose as a CLI plugin via env DOCKER_CONFIG and a data dir under $HOME — cost if wrong: macOS-only rough edges in README commands
Ruling: R4 Task 1 adds brain/.python-version = 3.13 — uv picked 3.14 locally while Dockerfile pins 3.13 — cost if wrong: none
Ruling: R5 Task 5 Step 2 expected status is 404, not 405 — doc slip — cost: none
Ruling: R6 Task 8 test_pair_token_partial asserts a real XXXX-XXXX token and the real CA fingerprint, not static template text — plan-mandated weak test — cost: none
Ruling: R7 Task 6 background remux catches MediaError and OSError (logs + marks item error, never silent) and app startup re-queues video items left in status stored — otherwise a crash or missing ffmpeg leaves items unplayable forever — cost if wrong: a few lines beyond plan; P2 job queue replaces it
Ruling: R8 Task 2 creates private key files with os.open(path, O_WRONLY|O_CREAT|O_TRUNC, 0o600) — plan wrote then chmod'ed (brief world-readable window) — cost: none
Ruling: R9 shared test helpers (pair-token helper, item header builders) live in brain/tests/helpers.py and are imported, never redefined per test file — plan duplicated them verbatim in Tasks 3/4/6 — cost: none
Ruling: R10 Task 2 server cert total validity (incl. backdated not_before) <= 800 days; test asserts <= 800 days — Global Constraint says <= 800 — cost: none
Ruling: R11 Task 5 tests validate ItemAck bodies with contract.assert_matches; Task 7 Heartbeat model makes acked_command_ids required like the contract — contract is source of truth — cost: none (P1b always sends it)
Ruling: R12 Task 4 adds check_agent_version to /v1/pair and a 426 response to the contract — spec §8 checks every agent request; P1b pair() already sends X-Loopi-Agent-Version and a missing header stays allowed — cost: none
Ruling: R13 Task 8 _queued.html shows Vietnamese command labels and a distinct revoke message — UI text must be Vietnamese — cost: none
Ruling: R14 spec updated to the plan: media path raw/<device_id>/YYYY/MM/DD; paused/audio_enabled read from last_heartbeat (no columns) — device owns that state (state.json) — cost if wrong: a migration later if the brain must store desired state
Ruling: R15 Task 6 test_remux_respects_fps uses bounds with margin (measured 2.5 s sat exactly on the bound) — flaky boundary — cost: none
Ruling: R16 Tasks 4 and 8 create api/__init__.py and web/__init__.py (File Structure lists them; no step created them) — cost: none
Ruling: R17 Task 5 drops _remove_empty_dirs cleanup — can delete a directory a concurrent upload just created; Review Focus only requires no leftover files — cost if wrong: empty date dirs remain after failed uploads
Incident: the scan subagent ran `docker container prune -f` (all stopped containers on the colima daemon, 11.7 MB reclaimed); running containers laida-qdrant/laida-mongodb unaffected. Reported to user. Scan leftovers (~/.cache/loopi-pf, image loopi-brain-scan) removed.

## Tasks
Task 1: dispatched (base 25c89c4, implementer sonnet)
Task 1: complete (commits 25c89c4..677a2c4, review clean)
Task 1: minor (deferred): testcontainers DeprecationWarning from plan-mandated import `testcontainers.postgres` (conftest.py) — switch import or filterwarnings
Task 1: minor (deferred): migrate() has no advisory lock (concurrent brain starts could race)
Task 1: minor (deferred): read_text() without encoding="utf-8" in db.py
Task 2: dispatched (base 677a2c4, implementer sonnet)
Task 2: review 1 → Needs fixes (1 Important: CA signature-rejection branch + cert constraints untested); fix round 1 sent to implementer (review base e7ff37c)
Task 2: minor (deferred): sign_csr leaks FileNotFoundError/ValueError instead of PkiError
Task 2: minor (deferred): key files opened without O_EXCL; re-run after partial init overwrites ca.key
Task 2: minor (deferred): device cert validity (3650d) can outlive the CA (plan-mandated, both 3650d)
Task 2: minor (deferred): leaf certs lack KeyUsage/SKI/AKI; hostnames not validated (empty DNSName); test_cli weak on stderr/invalid IP
Task 2: fix round 1/5 (1 addressed, 0 open — PKI security tests added; commits e7ff37c..be93a2a)
Task 2: complete (commits 677a2c4..be93a2a, review clean)
Task 3: dispatched (base be93a2a, implementer sonnet)
Task 3: ⚠️ resolved by controller — test pool max_size=10 (db.py:37), per-test TRUNCATE (conftest.py:23)
Task 3: complete (commits be93a2a..9878919, review clean)
Task 3: minor (deferred → carried to Task 7): record_heartbeat int(dropped_items) unvalidated; negative decrements dropped_total — heartbeat endpoint must validate non-negative ints
Task 3: minor (deferred): _issuer lambda duplicated in test_devices.py vs helpers.paired_device; record_heartbeat ignores 0-row update; enqueue_command raw FK error
Task 4: dispatched (base 9878919, implementer sonnet)
Task 4: review → Approved with 1 Important labeled plan-mandated (device auth only tested by direct call, not over HTTP)
Ruling: R18 the HTTP-level device-auth test moves to Task 5 (first route using current_device): PUT /v1/items with absent / empty / unknown / revoked X-Loopi-Client-Fingerprint → 401 through the ASGI client; Caddy header overwrite stays covered by Task 9 smoke spoof step — no route exists in Task 4 to test it on — cost if wrong: none, Task 5 reviewer checks it
Task 4: complete (commits 9878919..5f8691c, review clean after ruling R18)
Task 4: minor (deferred): HwInfo/PairRequest pydantic models lack extra="forbid" vs contract additionalProperties:false; assert_matches lacks a date-time format checker; status codes not checked against contract; sign_csr sync in async handler
Task 5: dispatched (base 5f8691c, implementer sonnet)
Task 5: review 1 → Needs fixes (4 Important: orphan file on insert failure [plan-mandated], no fsync [plan-mandated], marker body 500s/no cap, ULID/SHA regex trailing newline)
Ruling: R19 Task 5 fixes the two plan-mandated findings — unlink the final file on any failure after rename, and fsync the .part file + parent dir before commit/ack — device deletes its copy after the ack, so a lost/truncated file is unrecoverable data loss (spec: no data loss) — cost if wrong: a few ms per 60 s segment
Task 5: fix round 1 sent (review base 43cb990)
Task 5: minor (deferred): mid-stream ENOSPC → 500 not 507; ClientDisconnect untested; int()/fromisoformat accept non-RFC forms; missing tests (concurrent same-id, cross-device same id, leftovers on 413/507, missing headers); leftover alias _headers, inline imports
Task 5: fix round 1/5 (4 addressed, 1 new open — outer commit-failure unlink after lock release can delete a committed file; commits 43cb990..7b54ad5)
Ruling: R20 on commit failure after the rename, leave the file and propagate 5xx (no unlink outside the advisory lock) — an unlink outside the lock can delete a committed row's file (data loss); a stale file is overwritten by the device's retry — cost if wrong: an orphan file until retry
Task 5: minor (deferred): marker bodies with lone-surrogate escapes or NaN/Infinity pass json.loads but jsonb rejects → 500 after rename (file cleaned); fsync covers only the final date dir, not newly created parents
Task 5: fix round 2 sent (review base 7b54ad5)
Task 5: fix round 2/5 (1 addressed, 0 open — commit-failure path keeps file; commits 7b54ad5..c0493d3)
Task 5: complete (commits 5f8691c..c0493d3, review clean)
Task 5: minor (deferred): a commit failure with no retry leaves a row-less media file (no sweep)
Task 6: dispatched (base c0493d3, implementer sonnet)
Task 6: implementer DONE_WITH_CONCERNS (commits c0493d3..bf7b914): ffmpeg ignores -framerate when H.264 has VUI timing; fps test needed h264_videotoolbox (skips on Linux)
Ruling: R21 fps test uses a committed timing-free H.264 fixture (generated once with videotoolbox) and never skips — brain/CI run on Linux — cost: a few KB binary in tests/fixtures
Ruling: R22 keep -framerate remux (Pi raw H.264 from raspivid/rpicam-vid carries no VUI timing, the reason -framerate exists); P1c e2e step must assert a remuxed 60 s segment plays ≈60 s on real hardware — cost if wrong: timeline plays at wrong speed until a setts-based remux is added (raw files intact)
CARRY TO P1c: add remuxed-duration ≈ segment-length check to the Task 5 Step 7 end-to-end step
Task 6: pre-review change sent to implementer (R21)
Task 6: review 1 → Needs fixes (5 Important: stored-only guard, requeue error breaks shutdown/pool close, shared temp path, no ffmpeg timeout/kill, unguarded status updates); fix round 1 sent (review base 631f848)
Task 6: minor (deferred): int(video_fps) parse outside try; requeue sequential, no per-item-failure test; CI must install ffmpeg so media tests don't skip
Task 6: fix round 1/5 (5 addressed, 0 open — commits 631f848..aa1d90a)
Task 6: complete (commits c0493d3..aa1d90a, review clean)
Task 6: minor (carried to Task 7): stray brain/tests/test_media.py.bak committed — delete
Task 6: minor (deferred): second cancel during shielded proc.wait() skips temp unlink; timeout test's leftover assertion is weak (fake ffmpeg never creates temp)
Task 7: dispatched (base aa1d90a, implementer sonnet)
Task 7: review 1 → Needs fixes (2 Important, test-only: endpoint-level cross-device ack test; nullable/omitted optional heartbeat fields untested); fix round 1 sent (review base 9bb7edf)
Task 7: minor (deferred): dropped_total accumulation never asserted non-zero; last_error >500 → 422 untested (P1b truncates to 500); no 426 test on heartbeat route
Task 7: fix round 1/5 (2 addressed, 0 open — commits 9bb7edf..1540dd6)
Task 7: complete (commits aa1d90a..1540dd6, review clean)
Task 8: dispatched (base 1540dd6, implementer sonnet)
Task 8: review 1 → Needs fixes (3 Important); Important 2 (null ended_at → 500) ruled out: item.ended_at is NOT NULL (001_init.sql:26)
Ruling: R23 devices page renders booleans as có/không and missing values as "—" (plan-mandated raw values conflict with Vietnamese-UI constraint); HTMX errors show a Vietnamese message — cost: small template/JS change
Task 8: fix round 1 sent (review base 8933105)
Task 8: minor (deferred): extreme day values overflow → 500; no DST/edge tests for day_bounds_utc; no static/media serving or /media/../ test; revoke doesn't refresh card, commands accepted for revoked device; "Tắt audio" vs "Tắt ghi âm" wording; /media exposes whole media_root incl. temp remux files
Task 8: fix round 1/5 (2 addressed, 0 open — commits 8933105..88ddf11)
Task 8: complete (commits 1540dd6..88ddf11, review clean)
Task 8: minor (deferred): mic_missing missing → "ổn" and last_error missing → "không" (should be "—"); no zero-value test for val(); htmx error test is string-presence only
Task 9: dispatched (base 88ddf11, implementer sonnet); host: brew docker-compose 5.6.0 linked as CLI plugin via env DOCKER_CONFIG
Task 9: implementer still running at usage limit (started from 88ddf11); on resume check git log + task-9-report.md, then review; verify 'docker compose -p loopi-smoke ls' is torn down
Task 9: implementer DONE (commits 88ddf11..2d954d0): 116 tests, coverage 98.09%, smoke OK incl. forged-fingerprint 401; loopi-smoke torn down. NEXT: dispatch Task 9 review (base 88ddf11), then P1a final whole-branch review
Task 9: review 1 → Needs fixes (3 Important: root container [plan-mandated], no readiness wait/healthcheck, floating image tags [plan-mandated])
Ruling: R24 fix the plan-mandated container-as-root and floating base images — non-root USER + compose user ${LOOPI_UID}:${LOOPI_GID} so host-owned /srv/loopi stays readable; pin exact patch tags for reproducible builds — cost: one more env var to set on the brain PC
Task 9: fix round 1 sent (review base 2d954d0)
Task 9: minor (deferred): test_pair_token_command doesn't assert the `expires:` line; smoke.sh curl -s hides errors (use -sS); smoke leaves a smoke device + marker in the live DB; DB password unencoded in URL; non-device header stripping not smoke-tested
Task 9: fix round 1/5 (3 addressed, 0 open — commits 2d954d0..2fe10ab)
Task 9: complete (commits 88ddf11..2fe10ab, review clean)
Task 9: minor (deferred): smoke poll curl lacks --max-time; Dockerfile hardcodes uid 1000 (compose overrides); basic-auth success path not smoke-tested
FINAL REVIEW: dispatched (range 25c89c4..2fe10ab, opus)
FINAL REVIEW (opus, 25c89c4..2fe10ab): With fixes — 3 Important: (1) pending commands unbounded vs acked_command_ids maxItems 100 → heartbeat 422 forever; (2) contract omits enforced item limits (64 MiB body, marker 64 KiB, End≥Start, ≤1 h, kind→Content-Type) and P1b config can exceed 64 MiB; (3) PKI certs lack SKI/AKI/KeyUsage → strict TLS clients fail, costly to change after pairing
Ruling: R25 one final fix wave = 3 Important + fix-before-merge doc (DB password charset) + cheap hardening the final reviewer recommended: pair-token snippet renders a full [brain] block with url; Marker/Command.payload contract aligned with server; Caddy mounts only server.crt/server.key/ca.crt (not ca.key); small request_body limits on /v1/pair and /v1/heartbeat; X-Frame-Options DENY on web; smoke revokes its device; contract-edge test (server accepts exactly-at-limit payloads) — all cheap now, costly after devices pair or P1b is built on the contract — cost if wrong: a few extra lines
CARRY TO P1b: config validation must reject video_bitrate_bps × segment_seconds ÷ 8 > declared max item bytes (64 MiB); agent must not quarantine on Caddy's non-JSON 413 without that guard
FINAL FIX WAVE: dispatched (base 2fe10ab, sonnet)
FINAL FIX WAVE: 11/11 addressed, re-review clean (commits 2fe10ab..18da73c; 133 tests, 98% coverage, smoke OK on regenerated PKI)
Final fix wave: minor (deferred): pair snippet url from raw Host not TOML-escaped (admin-only page; use tojson or validate Host); Caddy 64KB limit and X-Frame-Options not probed directly; Command response model looser than contract (validate_command guards enqueue)
P1a: COMPLETE (25c89c4..18da73c). Ruling: R26 keep this workspace until Phase 1 (P1a+P1b+P1c) finishes and defer finishing-a-development-branch (merge to main needs user approval) — P1b/P1c continue on feat/phase1 and the rulings list is reported at the end — cost: none (git-ignored)
