# SDD ledger — plan: docs/superpowers/plans/2026-10-14-p1b-agent-v0.md

Branch: feat/phase1 (continues after P1a). Spec: docs/superpowers/specs/2026-10-14-loopi-design.md. Env: source .superpowers/sdd/env/env.sh (rustup 1.99 + cargo-llvm-cov 0.9.1 via Homebrew rustup; opus + pkgconf from Homebrew).
Ruling: install rustup (Homebrew, keg-only, no shell-profile edits) + cargo-llvm-cov on the host — plan Global Constraints list them as host prerequisites — cost if wrong: `brew uninstall rustup` + `rm ~/.cargo/bin/cargo-llvm-cov`
Pre-flight scan: STOPPED at usage limit (not complete) — re-run before dispatching P1b Task 1

## Pre-flight scan (99 rows: preflight.md; plan code trial-built per task on macOS: 77 lib tests + e2e pass, 92.4% host coverage; scratch removed for disk space)
Ruling: B1 test commands with several filters use `cargo test --lib -- a b` (cargo accepts one positional filter) — plan lines 1726, 3222 — cost: none
Ruling: B2 the "no aws-lc-rs" guard passes when `cargo tree -i aws-lc-rs` prints "nothing to print" or a "did not match" error; release binary size is informational — cargo 1.99 wording — cost: none
Ruling: B3 first-boot import stays in `loopi-agent provision`, run by P1c's loopi-provision.service before loopi-agent.service; `run` fails clearly if /data/config/loopi.toml is missing — spec §6.6 is met at the system level — cost if wrong: agent restarts until provisioning runs
Ruling: B4 Task 12 check_disk sets last_error "disk full: recording stopped" when the disk guard stops recording — spec §7.2/§12 require disk_full to reach the brain — cost: none
Ruling: B5 Task 9 sets recording_video before spawning rpicam-vid and clears it if spawn fails — privacy invariant (LED solid whenever a recorder runs), audio already does this — cost: LED may light for a failed spawn (fails safe)
Ruling: B6 Task 8 heartbeat sends at most 100 acked ids (constant matching the contract) and keeps the rest for the next beat — contract maxItems 100; brain also caps commands at 100 since P1a fix wave — cost: none
Ruling: B7 Task 8 queue depth I/O error is reported in last_error instead of silently 0 — no swallowed errors — cost: none
Ruling: B8 Task 8 reports all invalid-command errors joined, not only the last — cost: none
Ruling: B9 Task 5 map_put_status: 408, 425, 429 are retryable (Err(Status)), not quarantined — quarantine permanently drops data — cost: none
Ruling: B10 Task 12 a 401 during pairing goes to the 120 s "needs a fix" branch like NoToken — token used/expired won't fix itself — cost: slower retry if the brain was briefly misconfigured
Ruling: B11 dedupe: shared helper for the empty-file/enqueue loop (Task 9 collect vs recover_incoming); normalize_fingerprint reuses is_hex64 (Task 1); selftest uses DROP_ABOVE_PCT (Task 12) — cost: none
Ruling: B12 log instead of swallowing: queue metas() unreadable sidecars (Task 2), load_state non-NotFound errors (Task 3), blink_while LED errors (Task 4); SIGTERM handler setup uses ? not .expect (Task 12) — cost: none
Ruling: B13 (carried from P1a final review) Task 1 config validation rejects video_bitrate_bps × segment_seconds ÷ 8 > 64 MiB (contract x-loopi-max-item-bytes 67108864) and segment_seconds > 3600 (x-loopi-max-segment-seconds) — otherwise every segment 413s and is quarantined — cost: none
Note: host disk has ~4 GB free (colima VM 10 GB); P1c Buildroot needs ~20 GB+ — raise with user before P1c

## Tasks
Task 1: dispatched (base 18da73c, implementer sonnet)
Task 1: review 1 → Needs fixes (3 Important: open-network first boot never imported; MAX_SEGMENT_SECONDS dead gate [plan-mandated by B13]; wpa injection defence under-tested)
Ruling: B14 boot config is fresh when it holds a secret OR /data/config/loopi.toml does not exist; contract segment limit becomes a compile-time assert (agent max 600 ≤ contract 3600) instead of a dead runtime check — cost: changing an open network's SSID later needs a token or a data reset
Task 1: fix round 1 sent (review base 4f947f9)
Task 1: minor (deferred): boot file rewritten loses comments; write_atomic tmp cleanup/mode on pre-existing tmp, dir-fsync error swallowed; config_dir default mode; brain.url "https://" passes; ca_fingerprint stored un-normalized; schedule tests lack multi-range/boundary cases; invalid_values test asserts only is_err
Task 1: fix round 1/5 (3 addressed, 0 open — commits 4f947f9..1dc0445)
Task 1: complete (commits 18da73c..1dc0445, review clean)
Task 1: minor (deferred): deleting only /data/config/loopi.toml while wpa conf holds a PSK re-imports a scrubbed boot file and overwrites wpa conf with key_mgmt=NONE (guard: write wpa only if boot has a secret or wpa file absent); backslash-raw psk assumption untested against wpa_supplicant
Task 2: dispatched (base 1dc0445, implementer sonnet)
Task 2: review 1 → Needs fixes (3 Important: payload not fsynced before sidecar [plan-mandated]; recover deletes payloads (incl. audio) on unreadable sidecar [plan-mandated]; enforce O(n²) rescans)
Ruling: B15 fsync payload before rename; payloads with corrupt/mismatched/size-wrong sidecars go to quarantine/ (only true orphans without any sidecar are deleted+counted); enforce lists once per call — spec: no data loss, audio never auto-deleted — cost: quarantine/ can grow (not covered by disk guard; deferred)
Task 2: fix round 1 sent (review base b1a5200)
Task 2: minor (deferred): enqueue_bytes payload mode follows umask; .reason not fsynced; failed sidecar write leaves orphan until restart; ULID wall-clock ordering vs started_at; testing module not feature-gated; quarantine/ not covered by disk guard
Task 2: fix round 1/5 (3 addressed, 0 open — commits b1a5200..4892d0e)
Task 2: complete (commits 1dc0445..4892d0e, review clean)
Task 2: minor (deferred): no lock that a single process owns the queue dir; quarantine_raw partial failure leaves sidecar then deleted; same-stem multi-extension match on corrupt sidecar
Task 3: dispatched (base 4892d0e, implementer sonnet)
Task 3: review 1 → Approved with 2 Important (gate truth table not exhaustive; null payload accepted for pause/resume [plan-mandated by brief test])
Ruling: B16 null payload for pause/resume stays accepted (brief's test requires it; maps to the same command, no privacy or data risk; brain always sends {}) — cost if wrong: parser slightly more lenient than contract
Task 3: fix round 1 sent: exhaustive 2^6 gate test (review base 8127785)
Task 3: minor (deferred): SystemClock::is_synced hides permission errors as not-synced (fails safe, unlogged); corrupt state.json resets pause → resumes recording (other gates still apply); bool_assert_comparison style
Ruling: B17 corrupt/unreadable (non-NotFound) state.json → paused=true (fail closed), missing → normal defaults — privacy-first spec: a corrupt file must not silently resume recording a paused device — cost if wrong: recording stops until the user resumes from the brain
Task 3: fix round 1/5 (1 addressed + B17 applied, 0 open — commits 8127785..e8f431c)
Task 3: complete (commits 4892d0e..e8f431c, review clean)
Task 4: dispatched (base e8f431c, implementer sonnet)
NOTE: Linux-only code (gpiocdev/alsa/sd-notify) not compiled on macOS — plan a Linux cargo check (container) before P1b final review, disk permitting
Task 4: complete (commits e8f431c..c8896e1, review clean)
Task 4: minor (deferred): blink_while leaves LED off on exit (doc precondition: caller re-applies pattern); no reset if blink_while future dropped; button press timing uses read time not event.timestamp_ns; driver-error test doesn't count calls; classify exact 1000/1999 ms boundaries untested
Task 5: dispatched (base c8896e1, implementer sonnet)
Task 5: review 1 (opus) → Needs fixes (2 Important: redirects followed → 303 can fabricate Duplicate → local data deleted; network/decode errors drop source chain [plan-mandated])
Ruling: B18 fix both: Policy::none + https_only, 3xx retryable; join error source chain — data loss + undiagnosable device failures — cost: none
Ruling: B19 (for Task 6) when brain.ca_fingerprint is configured, the pairing TLS verifier checks DURING the handshake that the presented chain contains a cert whose SHA-256 equals ca_fingerprint and the leaf chains to it, before the token/CSR are sent — TOFU otherwise lets an active MITM steal the one-time token — cost if wrong: pairing fails if Caddy stops sending the CA in its chain (it does today, pki.py server.crt = leaf+CA)
Task 5: fix round 1 sent (review base 737af46)
Task 5: minor (deferred): response bodies read unbounded (cap 64 KiB); reqwest picks up proxy env (add .no_proxy()); single 120 s timeout covers the 15 s heartbeat; PairRequest Debug leaks token; test gaps (successful pair, audio/marker PUT, real handshake, build_client(mtls) builds); 404/405 quarantine on route regressions
Task 5: fix round 1/5 (2 addressed, 0 open — commits 737af46..3211453)
Task 5: complete (commits c8896e1..3211453, review clean)
Task 5: minor (deferred): https_only downgrade not tested (wiremock is http); error_chain may repeat inner text
USER (2026-10-14): finish P1b, then write README + committed context/handoff doc, push all source to git; user moves to Linux arm64 to build/develop (P1c there)
Task 6: dispatched (base 3211453, implementer sonnet)
Push target (user, 2026-10-14): https://github.com/cuongtv89ks/loopi.git
USER decision: keep repo PUBLIC and push (secret scan before push)
CARRY TO Task 12: build pairing client with build_client(pairing_config(cfg.brain.ca_fingerprint.as_deref()))
Task 6: review 1 (opus) → Needs fixes (3 Important: stray pairing.rs.new committed; no test that a clientAuth device cert is rejected as server; Identity Debug prints key_pem [plan-mandated])
Ruling: B20 also redact PairRequest Debug (one-time token) and validate the pair response with mtls_config before save_identity — secrets must not reach logs; inconsistent brain response must not brick the device — cost: a few lines
Task 6: fix round 1 sent (review base aad334e)
Task 6: minor (deferred): tls_server test harness can hang if post fails before connect; negative tests assert only is_err; misleading error variants for invalid fingerprint; load_identity uses exists() (hides EACCES), empty device_id accepted; keys dir default mode; ensure_paired lacks doc on required client
Task 6: fix round 1/5 (5 addressed, 0 open — commits aad334e..e60bb65)
Task 6: complete (commits 3211453..e60bb65, review clean)
Task 6: minor (deferred): check TlsError Display never includes PEM material (BadIdentity wraps it)
Task 7: dispatched (base e60bb65, implementer sonnet)
Task 7: review 1 → Needs fixes (2 Important: dropped shutdown sender → hot loop [plan-mandated]; quarantine tests only assert empty queue)
Ruling: B21 treat watch::changed() Err as shutdown in every loop (uploader now; carry to heartbeat/video/run loops) — a dropped sender must never make a loop spin — cost: none
CARRY TO Tasks 8, 9, 11: any select! on a shutdown watch treats changed() Err as shutdown
Task 7: fix round 1 sent (review base 6fda100)
Task 7: minor (deferred): upload_once not cancellable (relies on transport timeout); idle_poll ZERO spins with yield_now; Quarantined doesn't clear stale last_error; loop tests don't exercise BrainFull/Upgrade/Unauthorized or backoff growth
Task 7: fix round 1/5 (2 addressed, 1 new open — dropped-sender test hangs instead of failing on regression; commits 6fda100..a49b727); fix round 2 sent
Task 7: fix round 2/5 (1 addressed, 0 open — commits a49b727..fb67fec)
Task 7: complete (commits e60bb65..fb67fec, review clean)
Task 7: minor (deferred): zero-delay path checks only dropped sender, not a live `true` (shutdown waits for queue drain while items keep uploading)
Task 8: dispatched (base fb67fec, implementer sonnet)
CARRY TO Task 11/12: construct heartbeat deps with HeartbeatDeps::new(transport, queue, status, control, commands, probe, interval) (extra last_queue_depth field)
Task 8: review 1 → Needs fixes (3 Important: commands acked before delivery [plan-mandated]; invalid-command error clobbers last_error and never clears; NaN disk → 0 % hides fault)
Ruling: B22 ack a command only after delivery into the control channel (undelivered stay un-acked → redelivered); command/disk errors are request overlays merged into last_error and dropped after a successful heartbeat; NaN disk → last finite reading (or 100) + "disk usage unreadable" — brain must never forget an unapplied command or show a healthy disk on a fault — cost: persist failures still acked (control task must report save_state errors in last_error — CARRY TO Task 11)
CARRY TO Task 11: control loop puts save_state failures into status.last_error
Task 8: fix round 1 sent (review base 9a3064f)
Task 8: minor (deferred): no dedup of redelivered commands (pause/resume/set_config idempotent); dropped_items double-count if response lost (no idempotency key); poison 4xx heartbeat retried forever at 15 s without error-level log; commands.send().await can stall heartbeats; in-flight heartbeat cancelled on watch change to false; parse_wireless assumes dBm form; run_heartbeat interval/recovery untested
Task 8: fix round 1/5 (3 addressed, 0 open — commits 9a3064f..81b426d)
Task 8: complete (commits fb67fec..81b426d, review clean)
Task 8: minor (deferred): truncation drops the tail (camera error merged last is lost first when overlays are long)
Task 9: dispatched (base 81b426d, implementer sonnet)
Task 9: review 1 (opus) → Needs fixes (1 Important: dropped gate sender while open → unthrottled rpicam-vid respawn loop; B21 half done)
Ruling: B23 clamp segment start so End − Start ≤ 3600 s (contract) — otherwise a forward clock jump makes the brain 422 and the uploader quarantines the segment — cost: a few lines
CARRY TO Task 11: run_video incoming_dir = queue/incoming/video (not queue/incoming); call recover_incoming before run_video starts
Task 9: fix round 1 sent (review base 6dea1ab)
Task 9: minor (deferred): mid-collect enqueue error leaves stale last_end (overlapping times) and stops recover_incoming for other run dirs; failed final collect turns Shutdown/GateClosed into a camera failure and waits out backoff; B5 tests don't prove ordering on current_thread; exit.ok()/libc::kill/kill().await errors ignored; no getppid() check after prctl; camera_failures/last_error stay stale during a healthy run
Task 9: fix round 1/5 (2 addressed, 0 open — commits 6dea1ab..9dbedcf); ⚠️ resolved by controller: recover_incoming goes through enqueue_segments → segment_start (video.rs:129), so the B23 clamp covers it
Task 9: complete (commits 81b426d..9dbedcf, review clean)
Task 9: minor (deferred): dropped-gate test's fake camera writes no segments, so the partial-segment-queued path on Shutdown is not proven by that test
Task 10: dispatched (base 9dbedcf, implementer sonnet)
Task 10: review 1 → Needs fixes (3 Important: Ogg test not page-level/doesn't prove +960 granules; capture pauses at each boundary for fsync/enqueue → XRUN gaps and overstated times [plan-mandated]; ALSA rate not read back [plan-mandated])
Ruling: B24 finisher thread for finalize/fsync/enqueue, ended stamped at last frame and chained as next start; read back ALSA rate/channels — audio gaps every minute and wrong-speed audio are data corruption — cost: one extra thread on the Pi
Task 10: fix round 1 sent (review base eb5e832)
Task 10: minor (deferred): mic last_error never cleared on recovery; every record_segments error labeled as mic error/mic_missing; .part not retried until restart on write/finish/enqueue failure; backward clock step stretches a segment; up to ~5 s tail loss on hard crash (page flush cadence); expect in next_boundary; timing tests use real sleeps; recover_incoming aborts scan on one entry error
Task 10: fix round 1/5 (3 addressed, 0 open — commits eb5e832..82cfdaf)
Task 10: complete (commits 9dbedcf..82cfdaf, review clean)
Task 10: minor (deferred): finisher backlog full blocks capture (deliberate backpressure, undocumented); recording_audio held true while finisher drains; finish-error last_error never cleared; empty-segment unlink on capture thread
Task 11: dispatched (base 82cfdaf, implementer sonnet) — B4 applies here (check_disk is in run.rs), plus HeartbeatDeps::new, save_state→last_error, B21, contract-checking e2e mock
Task 11: review 1 (opus) → Needs fixes (2 Important: e2e misses Focus 5 incoming/-empty + partial-upload assertions; failed LED write never retried [plan-mandated]); LedPhase deviation accepted as improvement
Ruling: B25 run check_disk before the first gate computation — audio thread could start on a full disk — cost: none
Task 11: fix round 1 sent (review base 53605b2)
Task 11: minor (deferred): LED polled every 50 ms (add status.changed arm); blocking fs work (statvfs/fsync) inline on the runtime thread; panicked tasks only surface via panic=abort (debug builds: silent); "marker not saved"/"disk check" errors never cleared; e2e contract checks accept non-Z offsets, loose version/ULID regex, truncating seconds; no test shuts down while a recorder runs; fixed-sleep e2e timing; run() ~97 lines
Task 11: fix round 1/5 (3 addressed, 0 open — commits 53605b2..4eb7d0c); open question resolved by controller: gate watches start false (run.rs:245-246), check_disk (289) precedes first gate push (297)
Task 11: complete (commits 82cfdaf..4eb7d0c, review clean)
Task 11: minor (deferred): LED write failure warn! every frame (rate-limit), status churn via send_modify on each failure; partial-segment e2e assertion is the weaker reading; no run()-level ordering test for B25; FlakyLed test timing tight
Task 12: dispatched (base 4eb7d0c, implementer sonnet)
Task 12: review 1 → Needs fixes (3 Important, all plan-mandated: watchdog detached timer not liveness; READY=1 before pairing; selftest exits 0 on failures)
Ruling: B26 watchdog pings only while a liveness marker updated by run()'s loop and the pairing loop is fresh (≤ 20 s for WatchdogSec=30) — a wedged loop stops applying pause/gates (privacy) so systemd must restart it — cost: a stall > 20 s on a slow SD card restarts the agent
Ruling: B27 keep READY=1 at start (documented) + STATUS= updates — pairing may wait indefinitely for a token and a late READY under Type=notify would hit TimeoutStartSec and restart-loop; no units depend on loopi-agent — cost if wrong: systemd shows "active" while still pairing
Ruling: B28 selftest exits non-zero when any check fails — scripts must be able to gate on it — cost: none
Task 12: fix round 1 sent (review base 094dae5)
Task 12: minor (deferred): tracing to stdout mixes with selftest/provision output (use stderr); mic selftest false "busy" while the service holds ALSA; selftest unit test touches real ALSA on Linux hosts; camera check timeout without kill_on_drop; SIGTERM during pairing exits 1; early-signal watch race; no Stopping notify; pairing failures all warn! (no error level for bad token)
Host disk ~800 MB free after cargo clean (~/.cache/uv 4.3 GB is a shared user cache — not touched). No further builds on the Mac; remaining steps are read-only reviews + docs + push.
Task 12: fix round 1/5 (3 addressed, 0 open — commits 094dae5..beaa29e)
Task 12: complete (commits 4eb7d0c..beaa29e, review clean)
Task 12: minor (deferred): no liveness.beat() between pairing and run()'s first loop (slow recovery could exceed 20 s); STATUS "recording" set even when paused/gated; selftest exit path untested beyond failed_count
FINAL REVIEW: dispatched (range 18da73c..HEAD, opus)
Session switched (re-login): new commit trailer Claude-Session: https://claude.ai/code/session_0181MiuQwtvCKN2HePpjz9XC; P1b FINAL REVIEW re-dispatched after rate-limit failure
FINAL REVIEW (opus, 18da73c..beaa29e): With fixes — 1 Critical: LED open failure falls back to NoopLed and keeps recording (spec §7.4 violated; plan-mandated); 4 Important: startup recovery I/O error → restart loop; quarantine silent + systematic 4xx quarantines whole backlog; one 120 s timeout for all requests; server-name checked in mTLS but not in pairing (DHCP/IP change → silently dead device)
Ruling: B29 final fix wave = C1 (Linux: LED unavailable or ≥ 20 consecutive LED write failures → recording gates closed + last_error "privacy LED unavailable: recording disabled"; NoopLed only on non-Linux) + I2 per-entry recovery errors (only failing to create queue dirs is fatal) + I3 quarantine reported in last_error + counted, 400/404/405 retried, breaker after 3 consecutive same-status quarantines → Blocked w/ backoff + I4 heartbeat timeout 10 s, upload timeout max(120 s, size/64 KiB/s), tcp_keepalive 15 s, short pool idle, no_proxy, upload raced against shutdown + I5 mTLS verifies against the stored private CA without a server-name check (same tolerant verifier as pinned pairing; EKU serverAuth still enforced) + cheap privacy/robustness minors: selftest mic check lights LED Solid; config audio_enabled=false wins over state.json on import; 64 MiB check uses 75 % margin; heartbeat merge puts status error first; MissedTickBehavior::Delay; ReaderStream 64 KiB; a run()-level shutdown-while-recording test — spec privacy/durability outrank plan text — cost if wrong: a few hundred lines of churn before P1c
FINAL FIX WAVE: dispatched (base beaa29e, opus)
Ruling: B30 accept the fix wave's wider I3(b): only 409/413/422/missing payload quarantine, every other 4xx is retried (Blocked) — a misbehaving brain then stalls the queue visibly (last_error) instead of losing data — cost if wrong: one poison item with an unexpected 4xx blocks the oldest-first queue until fixed
FINAL FIX WAVE: re-review (opus) — C1, I2–I5, M1–M6 all ADDRESSED (commits beaa29e..4765370; 190 lib + 1 e2e + 4 run_safety tests)
Ruling: B31 residual Important (introduced by I2): Queue::recover continues after a ReadDir entry error, but std ends the listing at the first error, so payloads/sidecars not yet listed can be deleted as orphans (queue.rs:239-245 with :279-290) — no second fix wave per process; it is load-bearing for durability, so it is CARRIED as the FIRST fix on the Linux machine before any hardware test: track "listing incomplete" and, when set, skip every orphan/sidecar deletion and report them as left in place (+ test) — cost if wrong: a failing SD card could lose queued recordings at boot until fixed
Final fix wave: minor (deferred): heartbeat_once doc comment displaced by led_overlay; LedWriter skips unchanged-level writes (fault while steady Solid undetected); first confirming write is usually `false`; startup-recovery note / quarantine messages share one last_error slot; startup-recovery quarantines not counted; uploader blocks the queue on a payload with persistent non-NotFound I/O error; enqueue_file rename-ok/sidecar-fail reports "left in place" but next start deletes it as orphan; recover_incoming uses exists() (stat errors silent)
P1b: COMPLETE (18da73c..4765370) with one carried durability fix (B31)
