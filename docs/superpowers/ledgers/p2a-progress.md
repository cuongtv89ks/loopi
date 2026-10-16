# SDD ledger — plan: docs/superpowers/plans/2026-10-15-p2a-perception-pipeline.md

Spec: docs/superpowers/specs/2026-10-14-loopi-design.md (binding). Worktree /home/soul/loopi-p2, branch feat/phase2.
Base: main = feat/phase1 = 7021705 (fast-forwarded and pushed 2026-10-15 17:10 KST at the owner's request).
Plan commit: 29e27df. Baseline: brain 133 passed 98 %, agent 205+1+4, no aws-lc-rs.
Owner approval: 2026-10-15 "trước tiên chạy P2 hãy merge phase 1 vào main, sau đó thực hiện P2 trên branch phase 2".
Task 11 is controller-executed and needs a separate owner yes after the P1 hardware soak; not in this run.
Pre-flight review already done (scratchpad/p2/p2a-plan-review.md): I1–I8 + 11 minors applied; M6, M7, M8, M14 deferred
(listed in the plan's "Deferred minors").
Hard rule for every dispatch: no `docker compose`, no `deploy/smoke.sh` in Tasks 1–10 (live stack = project `deploy`).

## Pre-flight scan
Scan table: preflight-scan.md in this workspace (opus, 75 rows: 52 task-pair, 10 per-task, 13 global-constraint).
Plan code blocks were byte-identical to the prototype; prototype suite 284 passed, 1 skipped; every "Expected: PASS (N)" agrees.
Findings: 0 Critical, 2 Important (F1, F2), 13 Minor (F3–F15). Plan amended in commit below; behavioral rulings are
appended to the owning tasks as "Pre-flight rulings" blocks so the briefs carry them to implementer and reviewer.

- Ruling: F1 — Global Constraints lines 36/37/40 now name their deliberate exceptions (caption_model/embed_model/
  loopi:marker-v1; outage ends caption/embed jobs without a retry; embedding run + periodic tasks have no button;
  only own JPEGs and smoke-device files are removed) — reviewers were going to flag the plan's own design as violations — cost if wrong: none (wording).
- Ruling: F2 — Task 3 adds prune_failed_embed_runs (keep only the newest failed embed_moments row) + test; Task 9 puts the
  three periodic tasks in NOT_RETRIED with labels and jobs.* constants — persistent embed errors would add ~480 failed
  rows/day and hide real dead letters — cost if wrong: an old embed error row disappears (the newest stays visible).
  Periodic-task failure pile-up is NOT pruned (needs FK checks on procrastinate_periodic_defers; a failing tick is a
  visible bug state) — cost if wrong: a broken dispatcher floods the failed list until fixed.
- Ruling: F3 — Task 9: keyframes with a failed caption job are not counted as waiting and show "Lỗi mô tả" — spec §12
  wants dead letters visible, "Đang chờ" forever misleads — cost if wrong: one extra label.
- Ruling: F4 — helpers.insert_moment added in Task 3, reused in Tasks 5 and 9; Task 3's _insert_keyframe stays local
  (no JPEG, takes a caption: a different helper from Task 6's) — cost if wrong: small test churn.
- Ruling: F5 — Task 11 Step 7 now tunes KeyframeParams too; the cosmetic duplicates (DISPATCH_MAX_QUEUED, 60 s heartbeat
  age, STATUS_HTTP_TIMEOUT) are deferred minors for the final review — cost if wrong: two places to edit later.
- Ruling: F6 — Task 4 adds state.lock_pending_item / state.mark_item_done, used by Tasks 4, 5, 7; finish_item unchanged
  (own connection, done|skipped, pending guard) — cost if wrong: none (refactor of identical SQL).
- Ruling: F7, F9, F10, F12, F13, F14 — wording fixed in the plan (docstrings, README, test comment, full-suite runs in
  Tasks 2/5/7, expected outputs, file map) — cost if wrong: none.
- Ruling: F8 — keep CAPTION_PROMPT "up to 10 objects" with the schema/limit at 20: the prompt asks for few, the cap
  tolerates more before truncating (num_predict 400) — cost if wrong: captions list ≤10 objects.
- Ruling: F11 — controller commits a sanitized copy of this ledger to docs/superpowers/ledgers/p2a-progress.md after the
  Tasks 1–10 final review; Task 11 updates it — cost if wrong: none.
- Ruling: F15 — Task 10 revokes the smoke device by fingerprint when it exits before DEVICE_ID is known — cost if wrong: none.
Plan amended: 0154d12

## Tasks
Task 1: dispatched (BASE 0154d12)
- Ruling: commit trailers — subagent commits name the model that wrote them (Task 1: "Claude Sonnet 5.5") instead of
  globals.md's Opus line; the Claude-Session line is kept — accurate attribution beats uniformity — cost if wrong: cosmetic.
- Task 1 ⚠️ resolved: uv 0.5.11 (the Dockerfile's pin) runs `uv export --frozen` on the revision-3 lock fine (checked
  in scratchpad/lockcheck: procrastinate 3.10.0, faster-whisper 1.2.1, av 18.1.0); no aws-* package.
Task 1: minor (deferred): db.migrate always appends the Procrastinate schema after every *.sql — a future 003 that references procrastinate_jobs fails on a fresh DB; undocumented.
Task 1: minor (deferred): keyframe.dhash BIGINT needs a signed 64-bit value (Task 4 must convert); no SQL comment.
Task 1: minor (deferred): test_pipeline_defaults does not clear LOOPI_* env vars.
Task 1: minor (deferred): no evidence test_concurrent_migrations fails without the advisory lock.
Task 1: minor (deferred): helpers.fresh_database builds DROP/CREATE DATABASE with f-strings (test constants only); function-body imports.
Task 1: complete (commits 0154d12..2667493, review clean)
Task 2: dispatched (BASE 2667493)
Task 2: minor (deferred): no test for the allow_cloud_images=True direction; "last error" test uses identical errors.
Task 2: minor (deferred, P2b brief): privacy gate is a deny-list (CLOUD_PROVIDERS) — fails open for a new provider not listed; prefer a LOCAL_PROVIDERS allow-list when P2b adds Anthropic; PrivacyBlocked docstring says audio but only images are checked.
Task 2: minor (deferred, P2b brief): task_ready ignores the privacy filter (a cloud fallback counts as ready for an image task).
Task 2: minor (deferred): ollama.py error path — non-dict JSON body on HTTP>=400 raises AttributeError (escapes ProviderError, no fallback); also UnicodeDecodeError in load_providers, TypeError on non-list embeddings, IndexError on empty messages, UnsupportedProtocol for a malformed URL → ProviderError not Unavailable.
Task 2: minor (deferred): health() drops the failure reason (no log); "models": null → TypeError; fallback loop duplicated in chat/embed (plan-mandated); assert under -O; small polish (Provider protocol lacks health, DEFAULT_CONFIG singleton returned, aclose untested, `or` vs `is None`).
Task 2: complete (commits 2667493..795b4d6, review clean)
Task 3: dispatched (BASE 795b4d6)
Task 3: minor (deferred): outage log line lists every missing Ollama model (incl. qwen3:4b/8b) instead of the caption/embed routes' models (dispatch.py:175).
Task 3: minor (deferred): jobs.py:74 re-raise comment says "dispatcher retries next tick" — if the holder is `doing`, the next tick would create a second job (unreachable today: no orphan jobs).
Task 3: minor (deferred): race test does not pin the FOR UPDATE row lock (queueing-lock index alone makes it pass); no test for DISPATCH_BATCH 200 cap, periodic task bodies, defer_keyframes, keyframe branch of recover_stalled.
Task 3: minor (deferred): recover_stalled marks failed and writes WorkerLost text in two transactions (crash between → failed job with no reason).
Task 3: minor (deferred, final review): fast path defers analyze_video before the remux and the dispatcher does not filter item.status — Task 4's "skip status='error'" check is racy (harmless: retried, then skipped). Plan-mandated order.
Task 3: minor (deferred): `_item_job(kind, "")[0]` dummy device id to get a task name.
Task 3: complete (commits 795b4d6..6df0053, review clean)
Task 4: dispatched (BASE 6df0053)
Task 4: minor (deferred): test_ffmpeg_timeout_kills_the_process does not assert the process is gone (code verified correct by the reviewer with a fake ffmpeg).
Task 4: minor (deferred): no integration test compares a later segment against a stored top-bit-set (negative BIGINT) dHash.
Task 4: minor (deferred): video.py `stored = True` set before COMMIT (failed COMMIT → orphan JPEGs; arguably safe); log line "deleted during its analysis" also fires for "no longer pending".
Task 4: minor (deferred, final review): select_keyframes PIL/numpy work runs on the worker event loop (sub-second per hour; to_thread would free the loop); extract_jpeg decodes from frame 0 per keyframe (O(index)); asyncio.TimeoutError alias.
Task 4: complete (commits 6df0053..3f0b855, review clean)
Task 5: dispatched (BASE 3f0b855)
Task 5: minor (deferred): embed test does not check each row got its own vector (FakeOllama one-hot makes it checkable); no partial-progress outage test; wrong-dimension test does not assert nothing was written; `done` over-counts rows skipped by a concurrent run.
Task 5: ⚠️ open → check after Task 6: index_marker_job / embed_moments_job wrappers executed by a real-worker test (moments.py coverage).
Task 5: complete (commits 3f0b855..a1ea4ea, review clean)
Task 6: dispatched (BASE a1ea4ea)
Task 5 ⚠️ resolved: Task 6's real-worker tests execute index_marker_job and embed_moments_job (moments.py lines 88/95 covered).
Task 6: minor (deferred): outage UPDATE in caption.py:163-167 lacks `AND caption IS NULL` (could stamp an error on a row a concurrent job captioned).
Task 6: minor (deferred): pydantic lax mode coerces "3"/true/"yes" — the "wrong type raises" boundary is untested.
Task 6: minor (deferred): the 404 (model not pulled) test does not assert the caption resumes after the model appears.
Task 6: minor (deferred): overlong-answer test lacks jobs_open (defer_embed logs); helpers.insert_keyframe vs test_dispatch._insert_keyframe (ruled distinct in R-F4); run_jobs resets context to None; dots-only parts give ". .".
Task 6: note (residual, final review): a connection dropped mid-request (Ollama restart) is ProviderError and spends one retry (Task 2 mapping).
Task 6: complete (commits a1ea4ea..860f27c, review clean)
Task 7: dispatched (BASE 860f27c)
Task 7: review (opus) — Needs fixes: 2 Important, both plan-mandated (asr.py hallucination filter).
- Ruling: T7-I1 — "hẹn gặp lại" alone drops real family farewells ("Hẹn gặp lại em", "Ừ, hẹn gặp lại") under the 80 %
  rule; it becomes a FRAGMENT that counts toward coverage only when a full outro phrase also matched in the same
  segment; the other phrases stay standalone; add the full Korean form "구독과 좋아요 부탁드립니다" and the spelling
  "đăng kí kênh" — spec §9.2 transcribes family speech and P2b will delete raw audio after transcription, so a dropped
  real sentence becomes permanent — cost if wrong: a standalone hallucinated "Hẹn gặp lại" on noise is kept when the
  confidence filters miss it (P2c's golden set measures this).
- Ruling: T7-I2 — keep_segment drops text with no letters/digits (`not _normalise(seg.text)`): "..."/"…"/"-" at normal
  confidence would otherwise become a speech moment and an embedding run (Review Focus 4) — cost if wrong: none.
Task 7: fix round 1/5 (2 addressed, 0 open — fragment rule for "hẹn gặp lại", letterless text dropped; commits 197b154..eb02303)
Task 7: minor (deferred): the "error after the item disappeared is a no-op" branch (asr.py ~279-281) has no test.
Task 7: minor (deferred, P2c tuning): the 20 s cap splits first and drops pieces < 0.5 s afterwards — a short "Ừ" before a long turn can be lost.
Task 7: minor (deferred, P2c tuning): phrase-list gaps — "MBC 뉴스 …입니다", "Hãy đăng ký kênh để ủng hộ mình nhé".
Task 7: minor (deferred): faster_whisper.tokenizer._LANGUAGE_CODES is a private import (pinned by uv.lock).
Task 7: complete (commits 860f27c..eb02303, review clean after 1 fix round)
Task 8: dispatched (BASE eb02303)
Task 8: minor (deferred): cli.py hard-codes "60 s" instead of health.HEARTBEAT_MAX_AGE_S (pairs with pre-flight F5 cosmetic duplicates).
Task 8: minor (deferred): worker-health prints a traceback when the DB is down / env missing (exit 1 still correct).
Task 8: minor (deferred): no test for "worker stops at startup on a broken providers.yaml"; http client created before the try in run_worker; real ollama.aclose/pull error paths untested.
Task 8: complete (commits eb02303..3a8a96d, review clean)
Task 9: dispatched (BASE 3a8a96d)
- Ruling: T9 concern — non-retryable cell text per task: "tự chạy lại mỗi phút" for embed_moments and dispatch_pending, "tự chạy lại theo lịch" for retry_stalled_jobs and remove_old_jobs (the brief's single text was false for the 10-min and daily tasks) — cost if wrong: wording.
Task 9: minor (deferred): retry_failed_job check-then-act not atomic (procrastinate_retry_job_v2 also accepts `doing`); `except ConnectorException` broader than its comment (hides DB errors as "no longer failed").
Task 9: minor (deferred): timeline `{% if kf.caption %}` treats `{}` as waiting while /pipeline does not (caption stage never stores {}).
Task 9: minor (deferred): test gaps — model-supplied caption escaping, FAILED_LIMIT truncation, audio latency with rows.
Task 9: minor (deferred, final review): failed AUTO_RERUN rows (periodic ticks) are never cleared and stay in "Job lỗi (N)" — pairs with the F2 ruling's unpruned periodic failures.
Task 9: minor (deferred): a retried row keeps its button until reload (second click → 409 text). Brief-verbatim.
Task 9: complete (commits 3a8a96d..24af7ba, review clean)
Task 10: dispatched (BASE 24af7ba)
Task 10: review (opus) — Needs fixes: 2 Important, both plan-mandated (deploy/smoke.sh).
- Ruling: T10-I1 — the smoke video uses a static source (`smptebars=size=320x240:rate=2`) instead of animated testsrc2:
  testsrc2's per-frame motion (8.3–18.7) exceeds keyframe_motion_max 8.0, so the smoke item would end `done` with no
  keyframe and every real smoke run would fail; smptebars measured motion ≤ 0.04 and one keyframe (luma 93.7) — cost if
  wrong: the smoke video no longer exercises motion filtering (unit tests do).
- Ruling: T10-I2 — on_exit always attempts the idempotent revoke (by id when DEVICE_ID is a valid ULID, else by fingerprint
  when FP is valid), whatever happened to the DELETE/rm; branch on ULID validity, not `-n`; README step 6 and a committed
  assertion in test_smoke_script_removes_what_it_created_on_every_exit follow; the poll loop also stops on `skipped`
  (a final state) — R-F15's invariant: no failed run leaves an active smoke device — cost if wrong: none.
Task 10: fix round 1/5 (2 addressed, 0 open — static smptebars smoke video, always-revoke on exit + skipped poll + README + test pins; commits 0ded32a..377840f)
Task 10: minor (deferred): if pairing returns 201 but cert parsing/FP fails, FP stays empty and the trap cannot revoke (README tells the owner to revoke a leftover "smoke" device).
Task 10: minor (deferred, later phase): worker gets the whole data dir read-write incl. pki/ (CA key) and LOOPI_PKI_DIR though it needs no PKI (api has the same exposure). Plan-mandated.
Task 10: ⚠️ for Task 11: SMOKE_ANALYSIS_TIMEOUT 90 s may be short on the live stack because the migration marks every P1 recording pending (backlog ordered by started_at); plan's Task 11 already uses 600.
Task 10: complete (commits 24af7ba..377840f, review clean after 1 fix round)
Final review: dispatched (opus, range 7021705..377840f, report final-review.md)
Final review (opus): "Ready to merge with fixes" — 1 Critical, 1 Important, 9 Minor; ledger minors: none must-fix alone, T9 + T3-keyframe-branch folded in.
- C1: a stalled job (embed run / periodic tick) next to a `todo` one with the same queueing lock makes retry_job raise
  UniqueViolation → worker crashes at every startup (reproduced by the reviewer).
- I1: every real marker ({} body) shows "★ Marked moment" (English) on the Vietnamese timeline (plan-mandated text).
- Ruling: F2 corrected — failed periodic jobs CAN be pruned (Procrastinate's BEFORE DELETE trigger nulls
  periodic_defers.job_id); the prune now keeps the newest failed row per auto-rerun task (embed + 3 periodic) — cost if wrong: an old periodic error row disappears (the newest stays).
- Ruling: final fix wave = C1 (recover_stalled tolerates the clash without a dead letter, row ids re-pointed; startup
  recovery never raises; regression tests + keyframe-branch test), I1 (marker label = note or "Đánh dấu"; embedding
  text stays English), T9 (above), M1 (retry endpoint refuses auto-rerun tasks), M2 (embed `lock`), M4 (docstring) —
  the rest of the minors stay deferred per the reviewer's triage table — cost if wrong: deferred items resurface in P2b/P2c.
Final fix wave: dispatched (FIX_BASE 377840f, brief final-fix-brief.md)
Final fix wave: done (commits 377840f..9dba2f4: 022d723 C1, 47d18f1 I1, 74e446c T9+M1, 9dba2f4 M2+M4); 312 passed, 1 skipped, 98 %.
  Fixer choices: C1 deletes the stalled job and re-points the row to the waiting job in one transaction (counts as
  "retried"); T9 prune runs at the start of every dispatcher tick. Fixer concerns: after a < 30 s restart a waiting
  embed run can wait up to 10 min for retry_stalled_jobs (video/ASR locks behave the same); the 409 text for
  auto-rerun jobs says "not in the failed state" (no UI path reaches it).
Final fix wave re-review: dispatched (opus)
Final fix wave re-review (opus): all 6 findings ADDRESSED, no new breakage.
- Ruling: T9 prune runs at the start of every dispatcher tick (not only before defer_embed as the fix brief said) —
  otherwise failed periodic rows are pruned only while moments wait and the embed model is up — cost if wrong: one
  cheap DELETE per minute.
- Ruling: accept the M2 side effect — after a < 30 s worker restart a waiting embed run can wait ≤ 10 min for
  retry_stalled_jobs (embeddings are outside the spec's ≤ 15 min caption/transcript target; video:/asr locks already
  behave so) — cost if wrong: embeddings lag up to 10 min after a crash.
Final: minor (deferred, Task 11 measure): after a fast restart (OOM / SIGKILL past the grace period) the startup pass
  misses the dead worker's jobs, so video:<device>/asr work waits ≤ 10 min for retry_stalled_jobs — counts against the
  ≤ 15 min target; Task 11 should time recovery after a worker kill.
Final: minor (deferred): a stalled job out of attempts is dead-lettered even when a waiting twin exists (ties to final minor 5, no per-row embedding isolation).
Final: deferred per the final reviewer's triage table (final-review.md): every other ledger minor; final minors 3, 5, 6 (docs), 7, 8.
- Ruling: HANDOFF.md refresh deferred to the P1c finish, which must rewrite it anyway (it still says P1c "not started");
  one rewrite covering P1c + P2a avoids two diverging edits on feat/phase1 and feat/phase2 — cost if wrong: a session
  reading HANDOFF before then sees stale state (the memory file and this ledger copy point to the truth).
- Ruling: keep this workspace until Task 11 is done (Task 11 is part of this plan and updates this ledger); the
  committed copy is docs/superpowers/ledgers/p2a-progress.md — cost if wrong: none.
Verification at 9dba2f4: brain 312 passed, 1 skipped, 98 %; agent/, os/, docs/protocol/ untouched since 7021705; live stack `deploy` never recreated (checked after Tasks 10 and the fix wave).
Tasks 1–10: complete. Task 11: waiting for the owner's yes after the P1 hardware soak.

## 2026-10-15 20:05 — audio level finding (owner: "check audio ko thấy có tiếng gì")
- Live device mic: C-Media 8086:0808 mini USB mic. Mixer at power-on: Mic Capture Volume 0/16 (range 0–23.8 dB), AGC on;
  the image never sets mixer levels. Recordings all day/night: RMS ≈ −55 dBFS, peaks ≈ −30 dBFS (noise floor level).
- Tests (temporary, RAM-only mixer changes on the device): volume 16 + AGC on → +2–3 dB only; volume 16 + AGC off →
  −70 dBFS (worse). Restored AGC on, volume left at 16 (reverts on reboot).
- Silero VAD finds faint speech in a few segments; Whisper tiny finds nothing in a raw segment but Vietnamese after
  +34 dB loudness normalisation → P2a ASR must normalise level before VAD/Whisper (open item, owner to decide the fix).

## P2a Task 10a/10b — audio level (executed from docs/superpowers/plans/2026-10-15-audio-level.md, ledger there)
Owner decision 2026-10-15: device gain (+20 dB, Part A on feat/audio-gain) combined with brain normalisation (Part B here).
Task 10a (audio level Task 3): complete (commits 2a5c2c3..96b90c1, review clean) — audio_level.level_gain, ASR normalises before VAD/Whisper, LOOPI_ASR_MAX_GAIN_DB (worker env default 30).
Task 10b (audio level Task 4): complete (commits 96b90c1..2e68443, review clean) — GET /audio/{id}.opus cached normalised copy under view/, raw fallback; media.run_ffmpeg shared with frames.
- Ruling: brain gain never lifts the noise floor (10th pct of 20 ms frame RMS) above −40 dBFS — cost if wrong: quiet speech in a loud room gets less gain.
- Ruling: playback uses the fixed 30 dB rule, not LOOPI_ASR_MAX_GAIN_DB; delete view/**/*.opus to rebuild copies — cost if wrong: tuning ASR does not change playback.
- Ruling: one ffmpeg helper (media.run_ffmpeg) for remux, playback and frames (frames wraps it, MediaError → FrameError) — cost if wrong: a regression caught by test_media/test_frames/test_video.
- Ruling: P2b retention, when it deletes raw audio after transcription, must delete view/…/<id>.opus too — cost if wrong: orphaned copies use disk.
- Ruling: Task 11 adds a playback check (one old + one new recording through /audio; count "plays at its raw level" warnings) and uses SMOKE_ANALYSIS_TIMEOUT=600 — cost if wrong: a broken filter chain on the api image's ffmpeg shows only as log lines.
- Ruling: M5 (-compression_level 5) deferred; 60 s segments first-play in 0.6 s — cost if wrong: ~6.6 s first play of a 600 s segment.
Real-data check by the final reviewer (76 segments): old recordings +≈30 dB; day speech ≈ −19 dBFS, night hiss ≈ −43 dBFS on playback; VAD 0 s on night noise before/after, day 328 s vs 275 s.
Audio-level Part B final review + fix wave (606faa2, fdf72ce: bitexact copies, no-store fallback, traceback for unexpected errors, real cleanup tests, README 30 dB note): re-review all addressed; brain 340 passed + 1 skipped, 98 %.
Task 11: owner 2026-10-15 22:0x — "okie tạm thời dùng bản smoke7 trước để chạy phase 2z. mai tôi sẽ sử dụng smoke8" = go-ahead for Task 11 now with the device on smoke7 (24 h soak passed; P1 hardware tests move to smoke8 tomorrow).
Task 11 Step 1: live checkout clean on feat/phase1 @ 7021705, already an ancestor of feat/phase2 (no merge needed).
Task 11 Step 2: pg_dump → ~/loopi-backups/p1-2026-10-15-2215.dump (291 KB); deploy/.env copied to the worktree (600, git-ignored, not printed).
Task 11 Step 5 (24 h window, 2026-10-16 04:28 → 2026-10-17 04:28 KST): second sample taken; no container restarted. Worker 25 082 CPU-s (6.97 CPU-h/day), models 8 756 CPU-s (2.43 CPU-h/day); memory peaks unchanged (reached in the backlog); host 12.4 GiB available, swap 4.6 GiB used (2.3 GiB idle worker pages).
Task 11 Step 6: window keyframes 131 for 23.7 h video (inside 100–300); captions avg 16.7 s / p95 28.8 s (n=131); ASR RTF 0.067 (n=1 426), transcribe 1.60 h wall; live latency 08–22 video→caption 0.6 / 1.0 min (n=127), audio→transcript 0.3 / 1.7 min (n=831); 0 dead letters, 0 todo/doing jobs.
Task 11 Step 7: skipped — keyframes per day inside the budget.
Task 11 Step 8: docs/measurements/2026-10-p2a-pipeline.md, numbers only.
- Ruling: no caption or transcript samples in the report (owner's private data; no question to the owner needed when nothing is quoted) — cost if wrong: the owner adds samples later.
- Ruling: §9.6 budgets read as job run time (captions 0.61 h, ASR 1.60 h: met); cgroup CPU-seconds reported beside them — cost if wrong: the spec meant CPU-h and both are over (2.43 / 6.97).
- Ruling: worker-kill recovery not timed on the live P2a stack (a deliberate SIGKILL of the live worker for a ≤ 10 min bound already known from the code); carried to P2c acceptance — cost if wrong: the bound is measured one phase later.
- Ruling: Step 9's HANDOFF row folds into the one HANDOFF rewrite at the P2b deploy (P2b Task 14 Step 9), as ruled for P1c — cost if wrong: HANDOFF stays stale until that deploy.
The live stack (compose project `deploy`) now runs from the worktree `/home/soul/loopi-p2/deploy`: it builds from there and bind-mounts `Caddyfile` and `providers.yaml` from there. **Before the worktree is ever removed** (e.g. by `finishing-a-development-branch`), redeploy from the main checkout on a branch that contains P2: `cd /home/soul/loopi && git checkout <branch with P2> && cd deploy && docker compose -p deploy up -d --build`, then `COMPOSE_PROJECT_NAME=deploy ./smoke.sh`.
Task 11: complete. P2a plan complete.
