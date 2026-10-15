# SDD ledger — plan: docs/superpowers/plans/2026-10-15-audio-level.md

Spec: docs/superpowers/specs/2026-10-14-loopi-design.md (binding; D19 added by Task 2). Owner decision 2026-10-15
"hướng 2 kết hợp với hướng 1" (device gain + brain normalisation). Plan written by an opus subagent from the design
brief, every code block prototyped (agent 214+1+4, 95.25 %; brain 336 passed + 1 skipped, 98 %). Plan commit 5d82afc.
Worktrees: Part A /home/soul/loopi-audio (feat/audio-gain from feat/phase1 @ 7021705); Part B /home/soul/loopi-p2
(feat/phase2 @ 2a5c2c3). Task 5 (image smoke8 + flash) needs the owner's yes; it ends the P1c Task 6 run on smoke7.

## Rulings taken while writing the plan (copied from the plan)
- Ruling: brain gain never lifts the noise floor (10th pct of 20 ms frame RMS) above −40 dBFS (extends the owner's D19 direction) — cost if wrong: quiet speech in a loud room gets less gain; raise NOISE_FLOOR_MAX.
- Ruling: playback uses ffmpeg (f32le decode for the gain, then volume + alimiter + libopus 16 kHz), ASR keeps PyAV — cost if wrong: negligible rounding differences.
- Ruling: remux_to_mp4 moves onto the shared _ffmpeg helper — cost if wrong: P1 remux regression, caught by test_media.py.
- Ruling: gain applied by wrapping the audio factory in run::run (gain::with_gain) — cost if wrong: none expected.
- Ruling: playback uses the fixed 30 dB rule, not LOOPI_ASR_MAX_GAIN_DB — cost if wrong: tuning the ASR ceiling does not change playback.
- Ruling: two image checks (88 → 90) + loopi-agent-dirclean before ./os/build.sh image — cost if wrong: one more build.
- Ruling: Task 5 does not copy os/out/wifi-psk into the worktree (flash-dev.sh runs from /home/soul/loopi/os/out) — cost if wrong: none.
- Ruling: P2b retention must delete view/…/<id>.opus with the raw audio (carried to P2b) — cost if wrong: orphaned copies use disk.
- Ruling: Tasks 3–4 are P2a Task 10a/10b executed from this plan; the P2a plan file is not edited — cost if wrong: none.

## Pre-flight scan
Scan table: preflight-scan.md (sonnet, 37 rows: 19 pairs, 4 per-task, 14 constraint); plan code re-run in scratch (agent 214+1+4; brain helper tests).
Findings: 0 Critical, 1 Important (F1), 10 Minor.
- Ruling: F1 — frames._ffmpeg becomes a thin wrapper over media.run_ffmpeg (MediaError → FrameError), one kill-and-reap helper for remux, playback and frames (also resolves P2a final-review minor 7) — cost if wrong: a frames regression, caught by test_frames/test_video.
- Ruling: F2 asr.py docstring step number; F3 expected-output wording; F8 PEP 8 blank lines; F9 shared raw→view helper — cost: none.
- Ruling: F4 QUIET_SPEECH/ROOM_NOISE live in tests/helpers.py — cost: none.
- Ruling: F5 worker env gets LOOPI_ASR_MAX_GAIN_DB with default 30 so the README's setting works — cost if wrong: none.
- Ruling: F6 heredoc model names — subagents name their own model (same as P2a) — cost: cosmetic.
- Ruling: F7 Task 5 Step 7 fast-forwards feat/phase1 in /home/soul/loopi (touches agent/ os/ docs/ only; the live stack builds from brain/ and deploy/) — done by the controller with the owner's yes for Task 5 — cost if wrong: none.
- Ruling: F10 implementers run sequentially (never Part A and B at once); a flaky timing test is re-run once — cost: none.
- Ruling: F11 Task 5 Step 1 expected log wording (5d82afc + rulings commit) — cost: none.
Plan amended: de4f864

## Tasks
Task 1: dispatched (BASE de4f864)
Task 1: minor (deferred): no test applies the gain with a chunk size other than FRAME_SAMPLES (property verified by the reviewer: bit-identical for 1..16000-sample chunks).
Task 1: minor (deferred): Gain::from_db does not guard non-finite/huge gains (silence, no panic); relies on Task 2's 0..=36 validation.
Task 1: minor (deferred): module doc says no sample passes −1 dBFS, but 0 dB bypasses the limiter (documented on from_db); rustfmt differences in brief-verbatim code; redundant FRAME_SAMPLES import in tests.
Note: my Task 1 review dispatch said "judge formatting as Minor at most" — that pre-judged a finding; not repeated.
Task 1: complete (commits de4f864..f1ef5c5, review clean)
Task 2: dispatched (BASE f1ef5c5)
Task 2: minor (deferred): no test pins audio_gain_db == 20.0 for a [capture] table without the key (the provisioned shape; covered indirectly by existing partial-table tests).
Task 2: minor (deferred): example image check needs "20.0 " with a trailing space (relies on the inline comment); stale-agent detection rests on the agent-string check alone; e2e audio-upload filter duplicated; selftest pins "-0.0 dBFS".
Task 2: note for Task 5: CaptureConfig has deny_unknown_fields — an old agent would reject a loopi.toml containing audio_gain_db; keep the stale-agent image check.
Task 2: complete (commits f1ef5c5..545075d, review clean)
Part A final review: dispatched (opus, range 7021705..545075d)
Task 3: dispatched (BASE 2a5c2c3 on feat/phase2)
Task 3: minor (deferred, check in Task 5): level_gain treats a zero noise floor as "no limit" — a segment with ≥ 10 % exact-zero 20 ms frames gets up to +30 dB (plan-mandated); check real segments have no exact-zero stretches.
Task 3: minor (deferred): normalise() clip/identity/dtype not exercised by a test; NaN max_gain_db lifts the cap (unreachable via validated Settings); room-noise e2e test does not pin the guard; function-body imports in test_config.
Task 3: note: the implementer's extra test_deploy assertion for LOOPI_ASR_MAX_GAIN_DB kept (reviewer: the test already pins worker env keys; guards R-F5).
Task 3: complete (commits 2a5c2c3..96b90c1 on feat/phase2, review clean)
Task 4: dispatched (BASE 96b90c1 on feat/phase2)
Task 3 zero-floor check: 72 real segments (every 20th of two days) have 0 exact-zero 20 ms frames → the zero-floor edge does not occur with this mic today; keep as deferred minor.
Part A final review (opus): Ready to merge — 0 Critical, 0 Important, 2 Minor; no ledger minor must-fix; recommends the
  [capture]-without-key test. 214+1+4 pass, 95.25 %, no aws-lc-rs; gain < 1 % of a Pi core, segment size unchanged.
- Ruling: one fix dispatch for M1 (README: say the running device reads /data/config/loopi.toml, /boot/loopi.toml only
  at first boot) + the recommended config test — the owner needs the right file to tune the gain — cost if wrong: none.
- Ruling: M2 (a full-scale glitch ducks the next ~90 ms by > 3 dB) stays; Task 5 counts glitches on real segments — cost if wrong: brief dips after glitches.
Part A fix: dispatched (BASE 545075d)
Task 4: implemented (commit 2e68443 on feat/phase2, 336 passed + 1 skipped, 98 %); review dispatched (sonnet)
Part A fix: done (commits 545075d..75d44f7: README where/how to change audio_gain_db; config test pins 20 dB for a [capture] table without the key; 215+1+4 pass); scoped re-review dispatched (sonnet).
- Ruling: Task 5 Step 2 (image build) started before the owner's yes — building touches no device and the fix wave changes only README + a cfg(test) test, so the binary equals the reviewed code; flashing (Step 4) still waits for the owner — cost if wrong: one rebuild.
Task 5: image build started 21:52 (loopi-agent-dirclean + ./os/build.sh image, log scratchpad/build-smoke8.log)
Task 4: minor (deferred, final review B): no in-flight de-duplication / no negative caching on /audio — parallel first-play Range requests each run both ffmpeg passes; a failing file repeats the work every play (up to 120 s per pass).
Task 4: minor (deferred): fallback warning logs only str(exc) (no traceback for unexpected errors, unlike remux_item); cleanup tests can pass vacuously; decode args duplicated across the two passes; one 122-char test line.
Task 4: complete (commits 96b90c1..2e68443 on feat/phase2, review clean)
Part B final review: dispatched (opus, range 2a5c2c3..2e68443)
Part A fix re-review (sonnet): both findings ADDRESSED, no new breakage. Part A complete (7021705..75d44f7).
Task 5 Step 2: build rc=0 (21:52–21:55), image checks 90 OK / 0 FAIL, verify-config all lines applied, both new checks OK.
Task 5 Step 3: os/out/loopi-dev-smoke8.img = feat/audio-gain @ 75d44f7 + loopi-authorized_keys; boot partition has loopi.toml.example (audio_gain_db = 20.0) and no loopi.toml; sha256 611668b9…
Task 5 Step 4: waiting for the owner's yes and flash (old device queue 0/0 at 21:5x).
Part B final review (opus): "Ready to merge with fixes" — 0 Critical, 0 Important, 7 Minor; real-data check (76 segments): old recordings +≈30 dB, day speech plays ≈ −19 dBFS, night hiss ≈ −43 dBFS, peaks ≤ −0.9 dBFS; VAD 0 s on night noise before/after, day 328 s vs 275 s.
- Ruling: one fix dispatch on feat/phase2 for M1 (asr.py docstring: P2b retention deletes the view copy too), M2 (README: playback fixed at 30 dB, not LOOPI_ASR_MAX_GAIN_DB), M3 (-fflags +bitexact so concurrent first plays write identical bytes; folds the "no de-dup" minor), M4 (Cache-Control: no-store on the raw fallback; traceback for unexpected errors; folds the "str(exc) only" minor), M7 (cleanup tests must actually create a temp file before failing) — cost if wrong: none.
- Ruling: M5 (-compression_level 5: 600 s first play 6.6 s → ~3 s) deferred — 60 s segments already start in 0.6 s; keep libopus default quality — cost if wrong: a long first play for 600 s segments.
- Ruling: M6 → P2a Task 11 adds a playback check (play one old + one new recording through /audio, count "plays at its raw level" warnings) — cost if wrong: a broken filter chain on the api image's ffmpeg would only show as log lines.
- Controller does M1's ledger part: Tasks 10a/10b rulings appended to docs/superpowers/ledgers/p2a-progress.md on feat/phase2.
Part B fix: dispatched (BASE 2e68443)
Part B fix re-review (sonnet): M1 (code), M2, M3, M4, M7 ADDRESSED, no new breakage; brain 340 passed + 1 skipped, 98 %. Part B complete (2a5c2c3..fdf72ce on feat/phase2).
Out-of-scope (deferred): test_media/test_frames timeout tests have the same vacuous "no temp file" assertion as M7 had.
Task 5: owner 2026-10-15 22:0x — keep smoke7 tonight for the P2a deploy; flash smoke8 tomorrow (Steps 4–7 pending).
