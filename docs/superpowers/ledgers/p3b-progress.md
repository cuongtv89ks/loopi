# SDD ledger — plan: docs/superpowers/plans/2026-10-17-p3b-vlog.md

Owner approval 2026-10-17: "ok" (plan reviewed; execution by subagent-driven development per CLAUDE.md). Spec: docs/superpowers/specs/2026-10-17-p3-mcp-vlog-design.md §4 + binding spec D15/D23/D25.
Branch feat/p3b from feat/p3 @ 22f523c in /home/soul/loopi-p3. Task 6 (deploy) is controller-executed after the owner's yes.

## Plan rulings (R1–R11, copied from the plan)
- Ruling: R1 — The vlog script uses the `recap` route (same model, D23), not a new provider task. Adding a task would change `providers.yaml`, the provider router and the spec's provider table for one prompt. Cost if wrong: the vlog cannot get its own model without a config change.
- Ruling: R2 — The renderer is the prototype verified on 2026-10-17, transcribed. It ran on the brain image's ffmpeg 7.1.5: 240 real 1080p 2 fps segments plus 10 stills in **152 s wall / 212 s CPU, peak 198 MB, 12.7 MB out**, plus robustness cases. Its error message is reduced to step and exit code. The stderr tail is kept on the exception, never logged or stored. Cost if wrong: an ffmpeg upgrade may need re-measuring.
- Ruling: R3 — Keyframe-only decoding (`-skip_frame nokey`) is used when the day has ≥ 3 keyframes per output frame. Measured: 2.3× less CPU, with steps of 9–12 s instead of 10 s. Otherwise every frame is decoded. Cost if wrong: a slightly uneven timelapse pace on long days.
- Ruling: R4 — Two jobs per day share the queueing lock `vlog:<day>`. The script job adds its render job in the same transaction that stores the script. The planner re-defers a render for a row left `scripted` with no job (a crash in between). Cost if wrong: none.
- Ruling: R5 — Keep the vlog that is already there until its replacement is stored. A rebuild keeps `path`, `duration_s` and `generated_at` while the row is `scripted` again. The Vlog page shows "đang dựng lại" next to the old video. Cost if wrong: an outdated vlog is visible for a few minutes.
- Ruling: R6 — The planner needs the day's recap (§9.2 "23:30: recap … dựng vlog"). It asks again once when the recap is rewritten after the vlog's script, and once per stale mark. Earlier days catch up one per run, newest first; today is never held back. "Dựng lại vlog" asks directly, even without a recap. Cost if wrong: a day without a recap gets no automatic vlog.
- Ruling: R7 — A manual "Tạo lại recap" at noon today makes a noon vlog, and the 23:30 recap makes it again. Cost if wrong: one extra render, at most 3 minutes of CPU.
- Ruling: R8 — The subtitle cue of a picture is its line. For a conversation highlight it is the line plus the quoted transcript sentence (≤ 120 characters), in the original language. The title shows for the first 3 s. Cost if wrong: Vietnamese and Korean subtitles mixed in one vlog.
- Ruling: R9 — A day with no active recording but with keyframes renders a slideshow of at most 240 keyframes spread over the day, 0.25 s each (≤ 60 s). This covers a day whose raw video retention deleted, and a day whose only recordings had no moment. Cost if wrong: a short slideshow instead of nothing.
- Ruling: R10 — The Vlog page's player plays `/vlog/<day>.mp4` with a `<track>` of `/vlog/<day>.vtt`, because browsers do not show `mov_text`. The download is the same MP4, carrying its `mov_text`. Cost if wrong: none.
- Ruling: R11 — The vlog's sound comes from the normalised playback copy. The render job makes the copy when it is missing, exactly as the `/audio` route does, and removes it again if the item was forgotten meanwhile. Cost if wrong: one extra Opus file per conversation highlight, deleted with its item.

## Pre-flight scan
Table: preflight-scan.md. Clean (every task's tests ran green against the plan's code before the plan was committed).

Task 1: dispatched (BASE cc424c1)
Task 1: implementer DONE 08ce305 (951 passed + 1 skipped, 99 %).
Task 1: review ✅ spec, quality approved, 0 Critical/Important.
Task 1: minor (deferred, final review — fix in the final wave): no keyframe(ts) index, so _FRAMES scans the whole keyframe table per vlog (add `CREATE INDEX keyframe_ts ON keyframe (ts)` to 007 before deploy); Candidate.text sits in the dataclass repr (field(repr=False)); a recording belongs to the day it starts on while chapters follow moment time (bounded, one segment per midnight); nearest/merge/talk_windows edge lines untested.
Task 1: complete (commits cc424c1..08ce305, review clean, 0 fix rounds)
Task 2: dispatched (BASE 08ce305)
Task 2: implementer DONE 0c7e62a (959 passed + 1 skipped, 99 %).
Task 2: review ✅ spec, quality approved, 0 Critical/Important (16 malformed answers + 4 router errors all end in the rule script).
- Ruling: for Task 4, a picture's caption adds the conversation sentence only when it differs from the line (the rule script's talk line IS the sentence; it would show twice) — carried into the Task 4 dispatch — cost if wrong: none.
Task 2: minor (deferred, final review): rule_script can emit a blank line for a candidate with empty text (fix in the final wave: fall back to the kind's label); no test pins the prompt size (40×200 ≈ 10 000 chars, within the recap's envelope); max_tokens 800 tight for 10 full lines; empty-material guard and rule audio cap untested.
Task 2: complete (commits 08ce305..0c7e62a, review clean, 0 fix rounds)
Task 3: dispatched (BASE 0c7e62a)
Task 3: implementer DONE 6165a34 (968 passed + 1 skipped, 99 %; render.py 92 %); render.py/tests/helper byte-identical to the brief (scripted extraction).
Task 3: review ✅ spec, quality approved, 0 Critical/Important (byte-identical to the brief; a focused run showed no ffmpeg survives cancel/timeout/feed error/broken pipe).
- Ruling: Task 4 passes the day's keyframes to render() always (render falls back to them only when no segment is usable — every MP4 missing must still give a slideshow, not a failure) — carried into the Task 4 dispatch — cost if wrong: none.
Task 3: minor (deferred, final review — fix in the final wave): ogg_opus_duration raises IndexError/struct.error on an Ogg under ~40 bytes and the clip except does not catch them (whole render fails instead of skipping the clip); the frame check catches short output only (an over-long keyframe decode would be cut by -frames:v silently — under R3); no title + no still → empty SRT → exit 183 (latent: Task 4 always titles); run_step kill/timeout/non-zero paths untested (work in a focused run).
Task 3: complete (commits 0c7e62a..6165a34, review clean, 0 fix rounds)
Task 4: dispatched (BASE 6165a34)
Task 4: implementer stopped on the previous account's weekly limit (uncommitted work kept); resumed after /login. Session trailer from here on: session_011TQCixsXdy7Z69snyJQU6U.
Task 4: implementer DONE c4968eb (1003 passed + 1 skipped, 99 %; vlogs.py, forget.py 100 %); both rulings test-first; extra assertions in test_forget/test_web_pipeline for the new sentences and labels.
Task 4: review dispatched (opus, review-6165a34..c4968eb.diff)
Task 4: review ✅ spec, changes needed — I1 forget/empty delete vlog files only via the row's path (a commit failing after os.replace leaves an orphan for ever); I2 a SIGKILLed render leaves vlog/.tmp/<day>-<uuid> for ever (grace 2 min < a 2.5-min render); I3 a model line with NUL/lone surrogate makes the JSONB insert fail and psycopg quotes ~50 bytes of it into day_job_error/the log (same exposure in caption.py since P2).
- Ruling: I1 forget and the empty branch delete the fixed paths vlog_rel(day)+.vtt of every touched day; I2 each render sweeps vlog/.tmp first (one render at a time) and forget removes vlog/.tmp/<day>-*; I3 root fix in llm.clip (drop NUL and lone surrogates) so captions/chapters/recaps are covered too — cost if wrong: none.
- Ruling: fold Minors 1 (rebuild deletes a todo render first), 2 (anchor the pipeline-card assertion), 3 (vlog files never in pending_files: fixed paths are remade by the planner and the 03:00 retry would delete a regenerated vlog), 7 (read the forget count before the row); park 4 (one wasted render before a re-script), 5 (an enqueue race ends one planner run), 6 (two blocking unlinks in async code).
Task 4: fix round 1/5 dispatched (resume implementer)
Task 4: fix round 1 done 7d1f118 (1016 passed + 1 skipped, 99 %); a forget during a render now fails that render's attempt (its tmp dir is removed) — the retry finds the row gone and returns False.
Task 4: fix round 1/5 (7 addressed, 0 open — fixed-name vlog deletion, tmp sweep + forget tmp cleanup, llm.clip drops NUL/lone surrogates, rebuild replaces a waiting render, anchored assertion, no vlog paths in pending_files, forget count read first; commits c4968eb..7d1f118)
Task 4: minor (deferred, final review): A the render-start tmp sweep relies on the vlog lock, which stalled-job recovery of a live-but-silent worker could break (cost: one failed attempt, nothing stored); B a forget during a render records a failed attempt (on the last attempt the day's planning pauses until retry/rebuild); C a rebuild commits the deletion of a waiting render before queueing the script (a queueing failure leaves a recap-less day without a job); out of scope — caption.py cuts captions with its own slice, so the P2 NUL exposure (caption.py:154) stays open: route caption fields through llm.clip in the final wave.
Task 4: complete (commits 6165a34..7d1f118, review clean after 1 fix round)
Task 5: dispatched (BASE 7d1f118)
Task 5: implementer DONE_WITH_CONCERNS 243bb17 (1025 passed + 1 skipped, 99 %); README writes <LOOPI_DATA_DIR>/media/vlog/… (the P3a README test scans to the end of the file and forbids `$LOOPI_DATA_DIR`).
- Ruling: accept `<LOOPI_DATA_DIR>` in the vlog README bullet (same notation as the P3a section); bounding that test's section at the next heading goes to the final wave — cost if wrong: none.
Task 5: review ✅ spec, changes needed (plan-mandated) — I1 the MP4 is cached `private, max-age=3600` but a rebuild replaces it under the same URL (stale video/VTT pairing); I2 GET /vlog runs up to 361 sequential queries (vlog_view per day, two unindexed job scans each).
- Ruling: I1 `private, no-cache` on MP4, download and VTT (ETag revalidation); I2 a batch vlog_views(pool, days, zone) (rows + failed + waiting jobs in three queries), vlog_view = one-day call — cost if wrong: none.
- Ruling: fold Minors — no "None" in the card; "Đang dựng vlog…" alone on a first build; rebuild refuses a future day (200 with a reason, nothing queued; button hidden); a test of the failed-job path.
Task 5: fix round 1/5 dispatched (resume implementer)
Task 5: fix round 1 done 6fe6a1b (1034 passed + 1 skipped, 99 %); Starlette 1.8 FileResponse does not answer 304 (full 200 on If-None-Match) — no-cache still correct, the player uses Range.
Task 5: fix round 1/5 (6 addressed, 0 open — no-cache on files, batched vlog_views, card shows only existing parts, first build shows only "Đang dựng", future day refused, failed-job path tested; commits 243bb17..6fe6a1b)
Task 5: minor (deferred, final review): the future-day test reads the real clock (could flake at local midnight); the Day page shows the rebuild button for a past day with no data; file-gone 404 and rebuild-not-queued message untested; Starlette 1.8 FileResponse never answers 304 (no-cache re-sends the file on a plain GET).
Task 5: complete (commits 7d1f118..6fe6a1b, review clean after 1 fix round)
Tasks 1–5: complete. Final whole-branch review dispatched (opus, cc424c1..6fe6a1b).
Final review (opus): With fixes (Minors only) — no Critical/Important; forget during script/render/rebuild traced, no forgotten material stored, no text in logs/errors, planner cannot loop, D25 holds.
- Ruling: final fix wave = keyframe_ts index in 007 (before the live migration), rule-script blank line → kind label, truncated Opus → InvalidMedia (skip the clip), caption.py drops the unstorable characters (keeps its slice), M1 render retry wait=60 s fixed (exponential would hold the vlog lock for an hour), M5 "Đang dựng" only while a job is live, M8 MP4 checks in a thread, M6 the list's "lúc dựng" column; everything else "leave" as triaged in final-review.md — cost if wrong: none.
Final fix wave: done (commits d42fbf8..2cb460a, 8 commits; 1046 passed + 1 skipped, 99 %); llm.storable() helper shared by clip and caption; two older tests now defer a live job for 'building'.
Final re-review (opus): all 8 ADDRESSED, no new Critical/Important.
Final: minor (deferred): a cancel at the exact moment _write_concat runs can leave a work folder under vlog/.tmp (paths only; the next render or a forget sweeps it); the list page's status cell can be blank for a scripted row with no live job until the planner's next tick.
Tasks 1–5: complete; final review clean after one fix wave. Brain 1046 passed + 1 skipped, 99 %. Task 6 (deploy, first vlog, three evenings) waits for the owner's yes.
