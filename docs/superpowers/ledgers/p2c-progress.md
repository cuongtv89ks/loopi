# SDD ledger — plan: docs/superpowers/plans/2026-10-16-p2c-evaluation-acceptance.md

Spec: docs/superpowers/specs/2026-10-14-loopi-design.md (binding; D21 + D22 added by Task 1). Worktree /home/soul/loopi-p2c,
branch feat/p2c from feat/p2b @ 80a88b8. Plan commits e7d2d3d (written + prototyped by opus), 2c2eb6d (independent
review "Ready with fixes": 0 Critical, 7 Important, 13 Minor — all applied; re-prototyped: 782 passed, 1 skipped, 99 %).
Review: scratchpad/p2c/p2c-plan-review.md.
Owner decisions 2026-10-16: D21 (golden-set data out of git, under the data dir + DB), the owner labels via "Đánh giá",
D22 (§14 Recall@5 = hit@5; Korean judged on CER for the 30 % rule). Approval 2026-10-17 03:1x: "okie hãy implement p2c"
(Tasks 1–10). Tasks 11–14 are controller-executed, each with the owner's yes (P2c deploys after P2b is live).
Hard rule for every dispatch: never touch /home/soul/loopi-p2 (live deploy dir), /home/soul/loopi-p2b or
/home/soul/loopi-data; no docker compose / smoke.sh / os/build.sh; testcontainers only; rm/cp aliased → command rm -f.

## Pre-flight scan
Scan table: preflight-scan.md (sonnet, 71 rows: 43 pairs, 14 per-task, 14 constraint); all 17 diff blocks apply cleanly, counts match (699 … 783 collected).
Findings: 0 Critical, 2 Important, 13 Minor. Rulings appended as "Pre-flight rulings" blocks (Tasks 5, 6, 7, 9, 10) + one Global Constraint wording fix, plan commit below.
- Ruling: I1 — rule-set scoring runs in a thread (anyio.to_thread.run_sync), not on the worker event loop — cost: none.
- Ruling: I2 — the acceptance report names its run and that run's overrides; the controller runs a final no-override run before Task 14 — cost: one extra night run.
- Ruling: GC wording — acceptance criteria carry n + 95 % interval; secondary metrics carry n; run comparisons use the noise floor — cost if wrong: secondary numbers without an interval.
- Ruling: M2/M3 README CPU loop writes memory.peak to the dated file; M6 reuse golden's helpers; M7 drop unused log; M11 answered() tolerates a question losing its moments — cost: none.
- Ruling: M9 — fixture sentences are invented text, not recorded speech (no owner approval needed); "MBC" in the outro regex is the well-known Korean broadcast-outro hallucination pattern, not private data — cost: none.
- Ruling: M10 trailers name the implementer's model (as P2a/P2b); M1 (confirmation run after 22:00) and M12 ($ROLLBACK across shells) are controller-task notes I apply when running Tasks 13/11 — cost: none.
- Ruling: deferred minors M4 (blank lines), M5 (import order), M8 (weak phone-fit test) — for the final review's triage.
Plan amended: 37b0db3

## Tasks
Task 1: dispatched (sonnet implementer a33aafb01170ae20d, BASE 37b0db3)
Task 1: minor (deferred, final review): spec §9.5 forget bullet and §13 "recall@5" not updated for D21/D22; eval_dir accepts empty/relative values (and its default test is not hermetic); btrim CHECKs accept "\n"/"\t"; eval_answer with both item_id and keyframe_id NULL would never cascade + no ts_start <= ts_end CHECK; FK cascades only partly pinned by tests; three blank lines (report said two).
Task 1: complete (commits 37b0db3..9a1f5a8, review clean)
Task 2: dispatched (sonnet implementer a2cc53e09f5b6cf7c, BASE 9a1f5a8)
Task 2: minor (deferred, final review — likely fix): metrics.py holds raw combining characters instead of \u escapes (an NFC-normalising tool would break the regex at import); Wilson bounds not exactly 0/1 at the extremes; bootstrap percentile index asymmetric for iterations not divisible by 40, iterations=0 IndexError; recall_at/reciprocal_rank unguarded inputs; test gaps (WER>100 %, NFD, ngã, seeds); format chars (ZWSP/BOM) count as words; apostrophes split English contractions (plan-mandated); align 12.7 s on 3000×3000 tokens → keep it off the event loop (Task 6 R-I1).
Task 2: complete (commits 9a1f5a8..54f2ddc, review clean)
Task 3: dispatched (sonnet implementer ab0ce03ce7bc24fc7, BASE 54f2ddc)
Task 3: review (opus) — Needs fixes: 1 Important (plan-mandated): the `outro` rule's unanchored search drops a whole segment of real speech that merely contains a sign-off ("Mai mình đi chợ nhé, hãy đăng ký kênh…") — against P2a's coverage principle; the golden set would not reveal it. Default live output verified identical to P2a (+ lang_prob).
- Ruling: T3-I1 — the outro rule drops a segment only when its match covers ≥ HALLUCINATION_COVERAGE of the normalised segment (P2a's rule), with keep tests for mixed segments; folded minors: kept_segments rejects unknown rule names (ValueError), the rule set is parsed once per stage (not per region), the VAD comment says "per frame" — cost if wrong: a few more outros kept.
Task 3: minor (deferred → Task 12/13 note): lone_phrase ignores language and confidence (a confident "Thank you." is dropped) — the golden set needs a clip with a real lone short phrase before turning it on; the live-stage test cannot tell the two rules apart.
Task 3: fix round 1/5 (4 addressed, 0 open — outro needs ≥ 80 % coverage, kept_segments rejects unknown rules, parse once, comment; commits bc9c461..74976dc)
Task 3: minor (deferred, final review): outro particle tail has no \b ("nha" matches "nhanh") and an unbounded * (absorbs a following "mình …" clause) — rule ships off; harden before turning it on.
Task 3: complete (commits 54f2ddc..74976dc, review clean after 1 fix round)
Task 4: dispatched (BASE 74976dc)
Task 4: implementer DONE 5f86b3b (745 passed + 1 skipped, 99 %); concern: invented vi/ko fixture sentences from the brief.
- Ruling: invented fixture sentences written in the owner-approved plan count as approved example text (they are not from any recording) — cost if wrong: mechanical swap to neutral placeholders.
Task 4: review dispatched (opus, package review-74976dc..5f86b3b.diff)
Task 4: review ✅ spec, quality approved, 0 Critical/Important; 7 minors in task-4-review.md.
Task 4: minor (deferred, final review — fix in the final wave): M1 AudioLabel unvalidated → a malformed label could put the reference text into a DB error (globals: no golden text in errors); M2 forget result does not report eval copies it failed to delete; M3 docstring: retention window between commit and copy fails closed; M4 sweep docstring overclaims; M5 small plan-mandated duplication (_drop, copy-failure blocks, FLAGS placement); M6 test gaps; M7 media.remove_files wording says "media root" for the eval dir.
Task 4: complete (commits 74976dc..5f86b3b, review clean, 0 fix rounds)
Task 5: dispatched (BASE 5f86b3b)
Task 5: implementer DONE 63c64c2 (751 passed + 1 skipped, 99 %); R-M11 guarded with a real concurrent-removal test.
Task 5: review dispatched (sonnet, review-5f86b3b..63c64c2.diff)
Task 5: review ✅ spec, changes needed — 1 Important (plan-mandated): ±2 s speech slack counts a neighbouring unpicked VAD region as correct even while the picked moment exists → inflates hit@5.
- Ruling: slack and keyframe/item fallbacks apply only to answers whose moment is gone (answers carry `live` from answered/get_question; matches reads answer["live"]) — the fallback exists only for reprocessed days; a live answer's neighbour is a wrong hit — cost if wrong: a reprocessed-then-restored id edge case scores exact-only.
- Ruling: fold Minor 4 (invented "Mai đi đâu?" → approved sentence) and Minor 6 (lower-bound test, zone annotation) into fix round 1; park Minors 1 (greedy ranks, hit@5 unaffected), 2 (chat.py inline range_utc), 3 (BROWSE_LIMIT truncation — UI task must say so), 5 (two-statement read, R-M11 makes it safe) for the final review.
Task 5: fix round 1/5 dispatched (resume implementer)
Task 5: fix round 1 done afc4496 (751 passed + 1 skipped, 99 %); Task 6 runner must take answers from answered() (matches reads answer['live']).
Task 5: fix round 1/5 (3 addressed, 0 open — live-only exact match, approved fixture sentence, lower-bound test + zone annotation; commits 63c64c2..afc4496)
Task 5: complete (commits 5f86b3b..afc4496, review clean after 1 fix round)
Task 6: dispatched (BASE afc4496)
Task 6: implementer DONE 8ecf42a (770 passed + 1 skipped, 99 %); R-I1 via anyio thread with a spy test; extra runner-level live-ruling test.
Task 6: review dispatched (opus, review-afc4496..8ecf42a.diff)
Task 6: review ✅ spec, quality approved, 0 Critical/Important.
Task 6: minor (deferred, final review): dead-lettered run job leaves the run "running" (eval run waits ≤ 3 h; next start_run marks it failed); no test of recover_stalled paths for eval jobs; format_compare rates lack n (globals: secondary metrics carry n); rules_impact may undercount lone_phrase on pre-P2c transcripts (no lang_prob); "not measurable (no clip)" wording when all clips of a language failed; brief-copied hygiene nits.
Task 6: complete (commits afc4496..8ecf42a, review clean, 0 fix rounds)
Task 7: dispatched (BASE 8ecf42a)
Task 7: implementer DONE 8b0a368 (783 passed + 1 skipped, 99 %); R-M6 via golden.DOMINANT_LANG + day_part(); /eval/questions link 404 until Task 8.
Task 7: review dispatched (sonnet, review-8ecf42a..8b0a368.diff)
Task 7: review ✅ spec, quality approved, 0 Critical/Important; 9 minors in task-7-review.md.
Task 7: minor (deferred, final review — privacy-relevant, fix in the final wave): eval forms have hx-post but no method="post" — without htmx a submit becomes a GET and the typed transcript lands in the access-log URL (_eval_candidate.html:1, _eval_clip.html:1); 403-without-HX-Request tested on 1 of 6 POST routes; .view.opus probe outside media.inside (eval_routes.py:181); others in the review file.
Task 7: complete (commits 8ecf42a..8b0a368, review clean, 0 fix rounds)
Task 8: dispatched (BASE 8b0a368)
Task 8: dispatched (BASE 8b0a368), carrying Task 7 notes (method=post on new forms; 403 test per POST route)
Task 8: implementer DONE_WITH_CONCERNS e5a47f2 (791 passed + 1 skipped, 99 %); method=post on new forms, 403 test on all 7 new POSTs.
- Ruling: keep the implementer's "browsing stops at 200 moments" note in _eval_hits.html (it closes parked Task 5 Minor 3) — cost if wrong: one muted line to remove.
Task 8: review dispatched (sonnet, review-8b0a368..e5a47f2.diff)
Task 8: review ✅ spec, changes needed — I1 question_search 200 for an unknown question (404 elsewhere; no 404 tests for search/pick/drop); I2 (plan-mandated) rule-set table rates without n / 95 % interval (globals).
- Ruling: fix I2 per globals (n + interval on every acceptance rate, n on secondary) — the plan's own Global Constraints outrank its template text — cost if wrong: none (more numbers shown).
- Ruling: fold Minors "FastAPI 422 echoes a > 2000-char question" (length checked in code instead) and "second start silently ignores typed values" (note on top) into fix round 1 — cost if wrong: none.
Task 8: fix round 1/5 dispatched (resume implementer)
Task 8: fix round 1 done 58320df (795 passed + 1 skipped, 99 %); question limit now enforced at 500 (refused with a reason, was silently cut); reference max_length dropped on Task 7 routes too.
Task 8: fix round 1/5 (4 addressed, 0 open — unknown-question 404 + tests, n/interval on rates, no 422 echo (lengths in code), busy second start note; commits e5a47f2..58320df)
Task 8: minor (deferred, final review): P2b search_routes.py:58,109 keep Form max_length=2000 → the same 422 echo of the search words (P2b code); add_clip test does not assert the typed text is back in the textarea.
Task 8: complete (commits 8b0a368..58320df, review clean after 1 fix round)
Task 9: dispatched (BASE 58320df)
Task 9: implementer DONE 03861c0 (803 passed + 1 skipped, 99 %); R-I2 header names the run, its gain/VAD/rules/overrides; WARNING line when config.overrides is non-empty.
- Ruling: R-I2 "override" = a per-run gain/VAD override (what start_run accepts); the live LOOPI_ASR_RULES are shown in the header, not warned on — the final no-override run uses the live settings by definition — cost if wrong: a run on non-default live rules is not flagged loudly (its rules are still printed).
Task 9: review dispatched (sonnet, review-58320df..03861c0.diff)
Task 9: review ✅ spec, changes needed — I1 (plan-mandated): cpu_per_day extrapolates a < 24 h window to "per day" unmarked.
- Ruling: keep the extrapolated number but label it "extrapolated from N h" below 24 h (the acceptance needs a fresh 24 h window anyway) — cost if wrong: none.
- Ruling: fold Minors one-clock report, n per day in the latency table, pending/failed items in the window shown, exclude-day outside the window named — cost if wrong: none.
Task 9: fix round 1/5 dispatched (resume implementer)
Task 9: fix round 1 done 7003a0e (808 passed + 1 skipped, 99 %); video waiting/failed sums keyframes + segments (labelled).
Task 9: fix round 1/5 (5 addressed, 0 open — extrapolated CPU day labelled, one clock, n per day, waiting/failed counts, exclude-day outside window named; commits 03861c0..7003a0e)
Task 9: minor (deferred, final review): CLI test has a sub-second race at local midnight (patch local_now); "Recordings" header counts captioned keyframes for video; other review minors.
Task 9: complete (commits 58320df..7003a0e, review clean after 1 fix round)
Task 10: dispatched (BASE 7003a0e)
Task 10: implementer DONE_WITH_CONCERNS a643ecb (810 passed + 1 skipped, 99 %); README loop tested end-to-end against parse_cpu/cpu_per_day with stand-in docker/date.
- Ruling: accept `window=${window:-$(date +%F)}` (one file per window; the brief's date-named file would split a 24 h window over two files) — cost if wrong: the owner must set window= for the second sample (README says so).
- Ruling: the stdin hazard of `docker compose exec -T` inside a piped script goes to the final wave (`</dev/null` on each exec, README and Task 14 script) — cost if wrong: a loop pasted through `bash -s` stops after its first exec.
Task 10: review dispatched (sonnet, review-7003a0e..a643ecb.diff)
Task 10: review ✅ spec, quality approved, 0 Critical/Important (⚠️ cgroup files in db/api containers unverified from the diff — check at Task 11/14 on the live stack).
Task 10: complete (commits 7003a0e..a643ecb, review clean, 0 fix rounds)
Tasks 1–10: complete. Final whole-branch review dispatched (opus, 37b0db3..a643ecb).
Final review (opus): With fixes — 0 Critical, 2 Important (I1 four eval forms without method="post" → typed transcript in a GET URL/access log; I2 acceptance hides failed-clip counts, a language whose clips all failed reads "no clip"); parked-minor triage in final-review.md.
- Ruling: final fix wave = I1, I2 + the reviewer's "fix now" minors (metrics \u escapes + test; drop Cf chars in normalise; outro \b; AudioLabel validation + IntegrityError → GoldenError from None; forget reports failed golden deletions + sweep docstring; format_compare n; acceptance per-path units; 403/method tests on all eval POST routes and pages; README loop </dev/null, mkdir, placeholder, restore-order note + Task 14 script note in the plan; spec §9.5/§13 wording aligned with D21/D22, no decision change) — cost if wrong: none expected; every other parked minor stays as triaged "leave".
- Ruling: spec wording edits that only restate D21/D22 (already decided by the owner) are not a spec decision change — cost if wrong: the owner rewords two lines.
Final fix wave: dispatched (sonnet, BASE a643ecb, final-fix-brief.md, 12 items)
Final fix wave: done (commits 132ec88..783a24e, 8 commits; 839 passed + 1 skipped, 99 %). Fixer notes: a sweep that raises inside a forget counts as 1 failed copy; item 10 pinned existing behaviour (mutation-checked instead of RED); P2b settings/search forms also lack method="post" (no golden text there).
- Ruling: the plan's Task 9 code and Task 10 README block keep their old wording; the code and README deliberately differ (final fix wave) — cost if wrong: a reader copying from the plan gets the older loop.
Final re-review (opus): all 12 ADDRESSED, no new Critical/Important.
Final: minor (deferred): forget.py:284 a sweep that raises reports "1 file không xoá được" even with no eval sample in range; forget.py:281 the count includes leftover copies from earlier forgets/deletes; eval_routes.py:166 update_clip does not catch GoldenError (500 with a fixed text; unreachable while labels validate).
- Ruling: park the three residual minors (no privacy or data effect; wording/edge only) — cost if wrong: a misleading count on the forget page in a rare failure.
- Ruling: keep this workspace and branch until the controller-run Tasks 11–14 (deploy after P2b, the owner labels, baseline ×2, tuning, final run, report) are done, as for P2a — cost if wrong: none.
Tasks 1–10: complete; final review clean after one fix wave. Brain 839 passed + 1 skipped, 99 %. Tasks 11–14 wait for P2b live (owner's memory decision) and the owner's yes for each step.
