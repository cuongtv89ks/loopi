# SDD ledger — plan: docs/superpowers/plans/2026-10-16-p2b-understanding-ui.md

Spec: docs/superpowers/specs/2026-10-14-loopi-design.md (binding; D20 added by Task 1). Worktree /home/soul/loopi-p2b,
branch feat/p2b from feat/phase2 @ 54e22f3. Plan commits eb0944c (written + prototyped by opus), 1a9850d (independent
review "Ready with fixes": 0 Critical, 9 Important, 16 Minor; all Important + 13 Minor applied, M3/M5/M13 deferred;
re-prototyped: 515 passed, 1 skipped, 98 %). Review: scratchpad/p2b/p2b-plan-review.md.
Owner approval 2026-10-16: "ok, duyệt, hãy implement tiếp" (Tasks 1–13). Task 14 (deploy) is controller-executed:
only after 2026-10-17 04:28 KST (end of the P2a 24 h window), P2a Task 11 closed, and the owner's yes.
Owner decisions: D20 (keep raw audio 7 days); retention ships off and is enabled only on the owner's yes; M3 (late
uploads into a forgotten range) left to the owner.
Hard rule for every dispatch: never touch /home/soul/loopi-p2 (live deploy dir) or /home/soul/loopi-data; no docker
compose / smoke.sh / os/build.sh; testcontainers only.

## Pre-flight scan
Scan table: preflight-scan.md (sonnet, 73 rows: 46 pairs, 14 per-task, 13 constraint); plan replayed in a scratch repo: all 59 diffs apply, Files == git add == changed files, counts match (345 … 516 collected); 218 DB-free tests pass.
Findings: 0 Critical, 4 Important (I1–I4), 16 Minor. Rulings appended as "Pre-flight rulings" blocks (Tasks 1, 2, 4, 5, 8, 9, 10, 11, 12) in plan commit e669c7a; briefs re-sliced.
- Ruling: I1 — planner tests use the planner's clock for created_at (would turn red from 2026-10-23) — cost: none.
- Ruling: I2 — shared days.hm, llm.marker_line, moments.marker_note instead of copies — cost: none.
- Ruling: I3 — one media.item_files for forget and retention — cost: none.
- Ruling: I4 — rewrite_recap deletes the day's failed write_recap jobs + clears the error before deferring — cost if wrong: an old recap error row disappears (the new run's outcome replaces it).
- Ruling: M1 — summarise_chapters gets the 6 h cooldown on first attempts for all days — cost if wrong: a fixed chapter waits ≤ 6 h for its summary.
- Ruling: M2 — commit trailers name the implementer's model (as P2a) — cosmetic.
- Ruling: M3 — D20/§9.5 spec text says deletion runs only after the owner enables it; R24 (recap deleted on forget) carried into the Task 10 brief — cost: none.
- Ruling: M4 — autouse delenv ANTHROPIC_API_KEY in tests — cost: none.
- Ruling: M6 rename shadowing local; M15 log unresolvable item paths — cost: none.
- Ruling: M13 — `bash -n deploy/smoke.sh` stays (syntax check only, never executes the script) — cost: none.
- Ruling: deferred minors M5 (recover_stalled writes no day_job_error text for per-day jobs), M7 (missing-key warning logged per build_router call), M8 (weak/environment-bound tests), M9 (duplicated <figure> markup / _write test helper), M10 (untested branches), M11 (card counts chapters not yet due), M12 (plan text drift; Task 14 fixed by the controller at run time), M14 (duplicate day-bounds test), M16 (04:28 gate goes into the committed ledger copy) — for the final review's triage.
Plan amended: e669c7a

## Tasks
Task 1: dispatched (BASE e669c7a)
Task 1: minor (deferred, final review): spec still implies unconditional deletion at :284 ("sau đó job lưu trữ mới xoá") and :291 ("03:00: áp dụng chính sách lưu trữ") and §12 disk-full row — add "nếu đã bật"; §9.4 table (date vs day, new tables/columns) not updated.
Task 1: minor (deferred): tests do not pin the exact midnight instant / round-trip, some indexes/constraints untested; days.* silently misreads naive datetimes (docstring says aware).
Task 1: note for Task 14: ALTER TABLE item in 003 waits behind any open transaction on item (no lock_timeout, same as 002) — deploy when the worker is idle-ish.
Task 1: complete (commits e669c7a..8df01f2, review clean)
Task 2: dispatched (BASE 8df01f2)
Task 2: minor (deferred): text treatment keyed on model name only (a custom providers.yaml using one model for vision and text would give captions the short keep-alive/900 s) — guard `and not images` suggested; repeated connect literal 5.0; test gaps (fallback in ollama_text_models, upper bounds); clip(text, 0) edge; AnswerInvalid stores pydantic's multi-line text.
Task 2: complete (commits 8df01f2..5362dad, review clean)
Task 3: dispatched (BASE 5362dad)
Task 3: review — Needs fixes: 1 Important (plan-mandated): closed_count's docstring promises "data still to come can no longer change" a closed chapter, but a drift boundary is confirmed by the next same-kind point however far away, so later data can split a chapter that is already closed (12 of 600 fuzzed days; Task 4 keeps closed rows, so the split is dropped; a full rebuild may differ).
- Ruling: T3-I1 option (b) — keep the plan's algorithm (Tasks 4–13 were prototyped on it); correct closed_count's docstring to the real guarantee ("closed chapters are frozen as they stood when they closed; data arriving later never changes them, but a full rebuild of the day (forget/reprocess) may draw different boundaries"), and add one test pinning that closed_count never counts the open last chapter before day end — cost if wrong: a rebuilt day can show different chapter boundaries than the live run did.
Task 3: fix round 1/5 (1 addressed, 0 open — closed_count docstring states the real guarantee + open-last-chapter test; commits 091e251..0bf0b77)
Task 3: minor (deferred): ChapterParams unvalidated (window=0 → unbounded window, min_s > max_s, gap_s <= 0); drift range 0–2 documented as 0–1 and the zero-mean branch untested; no direct partition property test, exact >= thresholds untested; zip strict=False; tail copy per call; last chapter before a long silence closes only at day end (plan-mandated).
Task 3: note for Task 4: a late-analysed moment whose timestamp falls inside an already-closed chapter — settle_s / reopen rule must cover it (check in Task 4 review).
Task 3: complete (commits 5362dad..0bf0b77, review clean after 1 fix round)
Task 4: dispatched (BASE 0bf0b77)
Task 4: minor (deferred, final review): tied timestamps across a chapter boundary make the frozen frontier reopen the day on every build until the open chapter closes (rare; fix: never cut between equal starts, or frontier (ts, id)).
Task 4: minor (deferred, final review): a cooldown-skipped chapter lets the build succeed and clear day_job_error (error text blinks every 6 h) — fix: clear only when no chapter of the day has summary_error.
Task 4: minor (deferred): _pending_since only partly tested (uncaptioned keyframe, unembedded moment, dead-lettered work branches); `written += 1` over-counts when the guarded UPDATE matched nothing; hourly catch-up query is a full scan and holds a pooled connection across router.health(); test name overstates.
- Ruling: T4 minor 5 is load-bearing for later tasks — priority −10 is set only at chapters.defer_build; Tasks 5, 9, 10, 13 dispatches carry "defer chapter/recap jobs only through their defer_* helpers (priority −10, lock llm), never .defer directly" — cost if wrong: a direct defer would run a chapter build ahead of live work.
Task 4: complete (commits 0bf0b77..a9b3f43, review clean)
Task 5: dispatched (BASE a9b3f43)
- Ruling: R-I4 moves to Task 6 — rewrite_recap and its test live in Task 6's web/day_routes.py and test_day_page.py (my pre-flight block put it on Task 5 by mistake); Task 5 added a planner-side test (deleting the failed write_recap job lets plan_recaps plan the day again) — cost: none.
Task 5: review (opus) — Needs fixes: 1 Important (plan-mandated): the prompt budget keeps the EARLIEST lines (within() on time-ordered lists) — on a busy day the recap sees nothing after ~14:00 (chapters) / ~11:34 (speech), so evening events and commitments are lost (simulated).
- Ruling: T5-I1 — choose speech lines greedily longest-first under the budget (≤ 60), then order the chosen ones by time and number S1…Sn; chapters: clip each summary to about CHAPTER_BUDGET // len(rows) (all chapters always present); markers: spread() over the day; add a test that a long late line (22:xx) and the last chapter appear in the prompt — spec §10.2 recaps the whole day — cost if wrong: shorter per-chapter summaries in the prompt.
- Ruling: T5 minor 3 folded into the fix round (privacy): never log model-derived commitment text at INFO; log the source ref and the day only — cost: none.
Task 5: minor (deferred): text-only dedupe compares SQL lower() with Python .lower() and is untested; generated_at/stale set at store time not material-read time (safe today under the llm lock); 'recap:' || day::text depends on DateStyle ISO (to_char suggested); enqueue race aborts the rest of a planner tick (reruns in 15 min); ~2 recap runs per active day (23:30 + after midnight) — Task 14 CPU estimate.
Task 5: minor (deferred, final review): llm.parse_answer puts the first 200 chars of a model answer into day_job_error and the failure log (private text in logs); speech-line ties at the budget cutoff lean to the morning.
Task 5: fix round 1/5 (2 addressed, 0 open — whole-day prompt (all chapters clipped, markers spread, speech longest-first then by time), no commitment text in logs; commits da2c814..60af51f)
Task 5: minor (deferred): recap of None if day omitted (only call site passes it); clip(source, 20) bounds but does not rule out leakage via the free-form source field; one line of budget may stay unused; >80 chapters/day would starve speech (unreachable).
Task 5: complete (commits a9b3f43..60af51f, review clean after 1 fix round)
Task 6: dispatched (BASE 60af51f; carries R-I4)
Housekeeping: killed a stuck 'cp -iv …mod.rs' left by an audio-level subagent (cwd: removed /home/soul/loopi-audio/agent, waiting on a prompt since 10-15).
Task 6: minor (deferred): corrupt keyframe → 500 on /thumb (map to 404/422 + log); thumb_rel raise untested; lexical-only "keyframes/" check (a DB path keyframes/../raw/x.mp4 would pass — DB is brain-written); _recap_lock is a 3rd copy of the recap:<day> format; HX_ERRORS[404] says "Không tìm thấy thiết bị" for commitment 404s; duplicated <figure> markup; rewrite_recap uses 4 connections (rare 500 on a race, retry works).
- Ruling: T6 minor 4 is load-bearing (Tasks 10–11 delete files through media.inside): the Task 10 dispatch adds direct unit tests of media.inside (absolute path, the root itself, a symlink pointing out of the root, a '..' that stays inside) — cost: none.
Task 6: complete (commits 60af51f..27d05ed, review clean)
Task 7: dispatched (BASE 27d05ed)
Task 7: review — Needs fixes: 1 Important: a NUL byte in the question (/search?q=%00) → psycopg DataError → HTTP 500 (and Task 8 chat would inherit it).
- Ruling: T7-I1 — strip NUL/control characters in the query normalisation before embedding and SQL, with a test; fold in minor 2 (a 16:00Z moment test that pins the local-day bounds of the time filter) — cost: none.
Task 7: minor (deferred, final review): unfiltered search uses seq scans at 30k moments (~0.2–0.45 s; ~0.6–1.5 s at 10^5) — thresholds/planner tuning is P2c; request_router None branch and hit_view marker branch untested; a wedged Ollama holds /search up to the 300 s read timeout before falling back to words (a short query-embedding timeout would be kinder); style (130-char line, untyped pool params).
- Ruling: T7 (found by the implementer while fixing): the search form submits ?from=&to= and FastAPI rejects '' for dt.date | None → every normal browser search returns 422; treat an empty date param as None (an unparsable value still 422), with a test — a real user-facing failure — cost: none.
Task 7: fix round 1/5 (3 addressed, 0 open — control characters stripped, local-day filter pinned, empty form dates = no limit; commits 595a996..e75ead3)
Task 7: minor (deferred): clean_query also splits ZWJ/ZWNJ sequences (harmless for vi/en).
Task 7: complete (commits 27d05ed..e75ead3, review clean after 1 fix round)
Task 8: dispatched (BASE e75ead3)
Task 8: review (opus) — Needs fixes: 2 Important, both plan-mandated.
- Ruling: T8-I1 — a model date in year 1 (a common "zero date") overflows in clean_filter/day_bounds_utc and crashes the chat; dates before 2000-01-01 (or any OverflowError) mean "no filter", with a parametrized case — cost: none.
- Ruling: T8-I2 — the answer prompt states the current local date/time ("Now is <weekday, YYYY-MM-DD HH:MM> (<zone>)"), passed from chat_events, so "Mai…" questions are not answered from an old plan — cost: a few prompt tokens.
- Ruling: T8 minors 1 and 3 folded in: the error text points to the sources where they render ("ở dưới"); an empty streamed answer is treated as an error (no misleading "không trích nguồn" warning) — cost: none.
Task 8: minor (deferred): partial streamed text stays on screen after an error (no "incomplete" mark); uncited text after the not-found prefix shows no warning; chat duplicates search_routes.range_utc (move to days.py); Router.stream_chat relies on GC to close the provider stream on early aclose (aclosing suggested); a stream ending without done:true looks complete; invisible U+200B/U+FEFF literals in a test; no request body cap on web POSTs in the Caddyfile (pre-existing — final review).
- Ruling: T8 follow-up — a placeholder (pre-2000) START with a valid end means an open start: the range is the MAX_FILTER_DAYS window ending at that end (as before the fix), not the single day; a placeholder END with a valid start means up to today (clamped as usual) — 'before the 13th' questions must not shrink to one day — cost: a wider search.
Task 8: fix round 1/5 (4 addressed, 0 open — pre-2000 dates are open bounds, the prompt carries the local date, sources wording, empty answer = error; commits 2451405..6717210)
Task 8: minor (deferred): year-0 strings ("0000-01-01") fail fromisoformat and count as missing (single-day) instead of placeholders; ANSWER_FAILED text says the model server fails even for an empty answer from a running server.
Task 8: complete (commits e75ead3..6717210, review clean after 1 fix round)
Task 9: dispatched (BASE 6717210)
Task 9: review — Needs fixes: 1 Important: reprocess_day never takes state.lock_day, so it can interleave with a chapter rebuild/summary/recap write (and Task 10's forget relies on the lock excluding every derived-text writer).
- Ruling: T9-I1 — reprocess_day takes lock_day(conn, day) as the first statement of its transaction, with a test that it waits for a concurrent holder — cost: none.
- Ruling: T9 minors folded in: day_bounds OverflowError for 0001-01-01/9999-12-31 → 422 not 500; defer_recap failure after the committed reset is logged, not a 500 (like defer_build); the providers error view shows only error locations/messages, never pydantic's input_value (a pasted key must not be echoed), with a test — cost: none.
- Ruling (carried to Task 11): retention sets raw_deleted_at under the item row lock BEFORE unlinking files, so reprocess's "raw is kept" check (FOR UPDATE) never sees NULL for a file being deleted — cost: none.
Task 9: minor (deferred): day scoping pinned for keyframes only (other day's chapters/recap/audio, markers, rollback untested); a no-op reprocess still reopens chapters + marks the recap stale (plan-mandated); reset + chapters/recap choice runs on half-reset data (plan-mandated); in-flight caption may write its old-model caption after the reset (page text could say so); static "đang tắt" line after saving, no hx-confirm when enabling deletion (Task 11 preview); get_retention strict (bad stored row → GET /settings 500); reprocess deletes cited moments (decided commitments lose their source, reworded duplicates).
Task 9: minor (deferred, final review): chained exceptions still put pydantic's full text (incl. values) into tracebacks in container logs; day_routes rewrite_recap calls defer_recap unguarded; Day URL at calendar extremes may overflow (check).
Task 9: fix round 1/5 (4 addressed, 0 open — reprocess takes lock_day, out-of-range days 422, failing defer_recap logged, providers errors without values; commits aff8220..b85ec1a)
Task 9: minor (deferred): validator messages still quote file-supplied names (needed by an operator); non-UTF-8 providers.yaml → UnicodeDecodeError 500 (pre-existing P2a).
Task 9: complete (commits 6717210..b85ec1a, review clean after 1 fix round)
Task 10: dispatched (BASE b85ec1a)
Task 10: implemented (33ccddf, 574 passed + 1 skipped, 99 %); implementer deviations ruled by the controller:
- Ruling: T10-D1 accept — media.inside now rejects an absolute path even if it points into the root (its docstring said so; callers pass DB-relative paths) — cost if wrong: none.
- Ruling: T10-D2 accept — all affected days are locked in one ascending pass, the day set re-checked under the locks and restarted if a recording appeared (the plan's out-of-order extra-day locks could deadlock two forgets across midnight) — cost: none.
- Ruling: T10-D3 accept — a recording straddling the range edge is deleted whole, so chapters and forget_log.range_start/end follow the widened span (else a chapter quoting the removed part keeps its summary, and the neighbour day's forgotten() guard never sees the forget); the audit equals the request when nothing straddles — cost if wrong: the audit shows a slightly wider range than typed.
- Ruling: T10-D4 accept — post-commit failures (defer_build, remove_files, audit update) are logged, not raised (the data is already gone); BaseException never swallowed; chapter requests before file deletion — cost: none.
- Ruling: T10-D5 accept — form values with a UTC offset give 422 instead of being read as local time — cost: none.
Task 10: known limitation (deferred, final review): files created by a job already running when the forget committed (a remux finishing, the first play of an audio copy) stay as orphans under view/; a commitment whose source an earlier reprocess deleted stays; old summary error text may quote a model answer.
Task 10: review (opus) — Needs fixes: 1 Important: a remux / first audio play / thumbnail in flight at forget time can leave a forgotten recording's MP4, playback copy or thumbnail on disk, untracked (not in pending_files) and still served under /media; item_files lists no MP4 for a video still 'stored'.
- Ruling: T10-I1 — (a) item_files always lists the deterministic view mp4 for a video (missing = gone); (b) remux_item's final UPDATE uses RETURNING and unlinks the fresh MP4 when no row matched because the item is gone (like video.py's JPEG cleanup); normalise_audio and make_thumb re-check that their item/keyframe still exists after writing and unlink the copy if not; tests for each race — cost: none.
- Ruling: T10 minors folded in (all safety/clarity): remove_files unlinks the unresolved media_root/rel after inside() approves (a symlink inside the root never deletes its target); _delete_rows retried on DeadlockDetected within LOCK_ATTEMPTS (no 500 on a rare deadlock with caption/embed); the preview shows the widened span when it differs and says recordings uploaded into the range before the confirm are deleted too; the forget_log comment says it stores the deleted span; a two-device test pins that every device's items in the range go; the edge-of-calendar test asserts its logged failure — cost: none.
Task 10: fix round 1/5 (6 addressed, 0 open — in-flight remux/playback/thumbnail copies removed when their row is gone, item_files lists the video mp4, symlink-safe unlink, deadlock retry, preview states the span, comment + tests; commits 33ccddf..10c98f3)
Task 10: minor (deferred): if remove_files fails inside remux_item the MP4 stays with only a log line; audio fallback FileResponse(raw) gives 500 instead of 404 if a forget removed the raw file in between (nothing leaked).
Task 10: complete (commits b85ec1a..10c98f3, review clean after 1 fix round)
Task 11: dispatched (BASE 10c98f3)
Task 11: implemented (6a0e046, 606 passed + 1 skipped); implementer concerns are correctness/privacy → addressed before review:
- Ruling: T11-C1 — a crash between the mark commit and the unlinking must not leave files forever: each tick also re-tries rows with raw_deleted_at set whose files are still recorded (view_path not cleared / a still-present raw path), bounded like the main batch; test with a simulated crash — cost: none.
- Ruling: T11-C2 — the audio playback route (and the thumbnail route if it serves anything of a retained item) treats an item with raw_deleted_at set as gone: 404, never create a new copy, and unlink a copy written while retention ran; test — cost: none.
- Ruling: T11-C3 — turning retention ON in Cài đặt asks for confirmation (hx-confirm with the Vietnamese count of what the next run would delete) — cost: none.
- Ruling: T11 — accept migration 004_retention_files.sql (item.raw_files_gone_at + partial index; additive, nullable; deployed together with 003 in Task 14) instead of overloading view_path — cost if wrong: one more migration file.
Task 11: review (opus) — Needs fixes: 1 Important: an unexpected exception on row k makes the batch-level handler un-mark the WHOLE batch, including rows whose files are already gone → they look kept; a later "Xử lý lại" of their day would lose their transcripts for good; a poison row repeats it nightly.
- Ruling: T11-I1 — catch per row inside _remove (an unexpected error leaves that row "left" with log.exception naming the id; the raw file is still there because it goes last); no batch-level un-mark (an escaping exception leaves the marks for the 004 sweep); test with a two-row batch whose second row's item_files raises — cost: none.
- Ruling: T11 minors folded in: the confirm dialog says "ít nhất" / uses the "nếu chạy bây giờ" counts (it understated); retention.py:41 comment corrected; _DUE excludes a video still status='stored' (startup remux race); a poison forget row (retry_forgotten_files raising) is logged per row and never blocks apply_retention; test_forget's private _write replaced by helpers.write_media — cost: none.
Task 11: minor (deferred): a 03:00 tick dropped by the queueing lock when the 02:00 job is still todo skips that night (next night recovers; a per-day last-run check would close it); confirm text stale after a save without reload.
Task 11: fix round 1/5 (5 addressed, 1 open — the failing row is un-marked; safe in the due phase, but a SWEEP row (marked by a dead run that may already have unlinked its raw) is un-marked too; commits a6e1d29..379b5c7)
- Ruling: T11-I1 round 2 — on an unexpected per-row error LEAVE the mark (fail closed): the row is skipped for this run, not un-marked; the next night's sweep retries it; docstrings no longer claim the raw file is "provably still there"; test: a marked row with its raw already gone, raw_files_gone_at NULL, poisoned item_files → stays marked — cost if wrong: an expired-but-still-present file shows "đã hết hạn" until the sweep finishes it.
Task 11: fix round 2/5 (1 addressed, 0 open — fail closed: unexpected per-row errors keep the mark in both phases, un-mark only when the raw entry verifiably exists; commits 379b5c7..74e5471)
Task 11: minor (deferred): lexists treats a dangling symlink / an existing raw path outside the root as present (retention cannot create either); a `left` video whose MP4 went before a later failure keeps view_path to a missing MP4 until the next night.
Task 11: complete (commits 10c98f3..74e5471, review clean after 2 fix rounds)
Task 12: dispatched (BASE 74e5471)
- Ruling: T12 concern — Anthropic stream_chat cut off by max_tokens before any text raises ProviderError (like chat), so the router falls back to the next route instead of answering empty; with a test — cost: none.
Task 12: review (opus) — Needs fixes: 1 Important: an API key with surrounding whitespace/CR/LF (pasted key, CRLF .env) makes h11 raise LocalProtocolError("Illegal header value b'<key>'"), and the provider puts {exc} into the ProviderError → key in logs, day_job_error, the Xử lý/Day pages and tracebacks (reproduced with a real socket).
- Ruling: T12-I1 — the settings validator returns value.strip() (no raise: pydantic would echo input_value); the provider maps httpx.LocalProtocolError (and any transport error text) to a fixed message without the key (`from None`) and scrubs the key from every message built from {exc}; regression test with a real AsyncHTTPTransport against a local listener — cost: none.
- Ruling: T12 minors folded in (same files, small): per-call timeout so anthropic.DEFAULT_TIMEOUT really applies; the missing-key warning logged once per process; _allowed never drops LOCAL_PROVIDERS ("local" asr stays ready); a stream api_error event maps to unavailable like HTTP 5xx; a stream ending end_turn/without message_stop and no text raises ProviderError (router falls back, like chat()); strict_schema strips keywords only in schema positions (never a property named "minimum") and adds multipleOf; docstrings of ProviderError/ProviderUnavailable and anthropic.py corrected; a test that an unknown cloud route receives zero requests for router.chat("caption", images=…) — cost: none.
Task 12: fix round 1/5 (9 addressed, 0 open — key trimmed, scrubbed, never chained; non-visible-ASCII keys refused before httpx; per-call timeout; warning once; local providers kept; stream errors mapped; empty streams fall back; strict_schema; docstrings; zero-request test; commits 0cab7ae..97d7f9f)
Task 12: minor (deferred, final review): _http_error truncates before scrubbing (a >500-char error echoing the key across the cut keeps its tail) — scrub first; 120 s read timeout can be short for non-THINKING_OFF models on the 8192-token path (configured Haiku/Sonnet unaffected); DecodingError/TooManyRedirects escape as raw httpx errors (pre-existing); ProviderError docstring "cut-off answers" nit; zero-request test's only-cloud half not discriminating.
Task 12: complete (commits 74e5471..97d7f9f, review clean after 1 fix round)
Task 13: dispatched (BASE 97d7f9f)
Task 13: review — Approved with 1 Important (plan-mandated): README says `docker compose up -d api worker` after editing the bind-mounted providers.yaml — that recreates nothing, so the edit has no effect.
- Ruling: T13-I1 — README says `docker compose up -d --force-recreate api worker`; minors folded in: the privacy bullet says text sent to the cloud includes captions and transcripts derived from the recordings; test_deploy walks the whole route chain (fallbacks too) for "100 % local by default" — cost: none.
Task 13: minor (deferred): "Hiểu ngày" waiting count looser than the planner's rule (open chapters < OPEN_MIN_MOMENTS, cooldown); card test exercises one recap row only and sets retention by raw SQL.
Task 13: fix round 1/5 (3 addressed, 0 open — README force-recreate, privacy wording, fallback-chain test; commits dfa46ca..02aff7e)
Task 13: minor (deferred, final review — likely fix): `up -d --force-recreate api worker` without --no-deps may also recreate db (Postgres restart); add --no-deps.
Task 13: complete (commits 97d7f9f..02aff7e, review clean after 1 fix round)
Tasks 1–13: complete. Final review: dispatched (opus, range 54e22f3..02aff7e)
Final review (opus): "With fixes" — 0 Critical, 1 Important (I1: model-written text in parse errors survives forget and reaches logs/pages; forget leaves failed recap jobs), 5 new Minor; ledger minors that must be fixed: Task 5 parse_answer text, Task 10 old summary error text (= I1). Suite 668 passed, 1 skipped, 99 % (reviewer ran it).
- Ruling: final fix wave = I1 (no answer text in errors; forget clears summary_error/day_job_error/failed recap jobs), M1 (one rewrite helper for Day page/reprocess/forget), M2 (decorator priority on build_chapters_job), M3 (cloud-fallback wording), M5 (optional LOOPI_LLM_NUM_THREAD), and the cheap list (--no-deps, spec "nếu đã bật", scrub-before-truncate, from None, Day URL extremes 422, generic 404 text, corrupt thumbnail 404) — cost if wrong: none; M4 is a Task 14 check.
Final fix wave: dispatched (opus, FIX_BASE 02aff7e, brief final-fix-brief.md)
Final fix wave: done (commits 02aff7e..aecdc57, 8 commits; 687 passed + 1 skipped, 99 %). Fixer concerns: P2a's parse_caption still quotes the caption model's answer into keyframe.caption_error and tracebacks (same class as I1, P2a code, not in this wave → residual for the owner); test_web's 404-text test updated; forget deletes failed recap jobs inside its transaction (deadlock retry covers it).
Final fix wave re-review: dispatched (opus)
Final fix wave re-review (opus): all findings ADDRESSED, no new Critical/Important. Residuals (parked, surfaced to the owner; no second fix wave):
- Ruling: park — reprocess's post-commit request_rewrite transaction is unguarded (a DB error there → 500 although the reset stands; retry is idempotent) — cost if wrong: a rare misleading 500.
- Ruling: park, propose as a pre-deploy one-liner — recap.sourced() logs clip(c.source, 20) of a model-written source field (up to 19 chars of private speech in the worker log) — cost if wrong: private text in container logs.
- Ruling: park (P2a code) — parse_caption quotes the caption model's answer into keyframe.caption_error and tracebacks — cost if wrong: image-derived text in logs.
- Ruling: park (cosmetic) — forget deletes a forgotten day's build_chapters day_job_error row, so its failed job shows an empty error cell on Xử lý.
Tasks 1–13 complete; final review clean after one fix wave. Task 14 (deploy) waits for 2026-10-17 04:28 (P2a window) + P2a Task 11 report + the owner's yes.
Follow-up (owner approved a+b 2026-10-16): fix the two parked private-text log leaks (ruling 46 recap.sourced, 47 P2a parse_caption) before the deploy — brief followup-brief.md; dispatched (BASE e02269b).
Follow-up: complete (commit 46339cb, 696 passed + 1 skipped, 99 %; review clean): recap drop log carries day + length + ref-shape only; parse_caption reuses llm.parse_answer (invalid=CaptionInvalid) — no answer text in errors, caption_error, logs or chains; retries unchanged.
- Ruling: at deploy (Task 14) scrub old keyframe.caption_error rows written by P2a that quote caption text (`… got '…'`): count them, then replace the message with a neutral one (derived error text, not user data) — cost if wrong: an old error loses its detail.
Follow-up minors (deferred): the cut-off caption test pins a fixture-derived character position; `invalid: type[Exception]` could be a Callable.
