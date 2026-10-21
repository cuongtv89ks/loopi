# SDD ledger — plan: docs/superpowers/plans/2026-10-19-p3c1-storage-display.md
Spec: docs/superpowers/specs/2026-10-19-p3c-multicam-design.md (approved 2026-10-19, D30–D36) + binding spec 2026-10-14-loopi-design.md (D37 on this branch).
Worktree /home/soul/loopi-p3c, branch feat/p3c1, start d47da2a (= main = feat/phase2; live stack resumed 2026-10-20 13:56 KST).
Owner: "chạy tiếp p3c" (2026-10-20) after the plan was presented = go for SDD; Task 8 (deploy) still needs the owner's explicit yes (irreversible deletions).

## Pre-flight scan
| Pair / task | Produces → consumes | Finding |
|---|---|---|
| T1↔T2 | test_db.py lists: T1 adds 009, T2 adds 010; T2's migration test applies 001…009 by file name (needs T1's file) | consistent in sequence |
| T1↔T2 | T1's retention/forget test stores Retention(enabled=True) explicitly | unaffected by T2's default |
| T2↔T3 | test_db.py lists (011); T3's migration test applies 001…010 | consistent in sequence |
| T3↔T4 | places.py: T3 creates clean_location/set_location/several/at; T4 adds day_places/known_device; routes.py: T3 adds save_location, T4 edits _day_by_hour/timeline; CSS .place from T3 | distinct functions; consistent |
| T3↔T5, T3↔T6 | places.several, places.at(location, named, sep="") used by chat (no sep) and chapters (",") / recap (no sep) | signatures match |
| T4↔T5 | search links stay /?day=…#m-id (no filter) | consistent |
| T2↔T7 | T2 edits deploy/README.md; T7 edits the two specs | distinct files |
| T1 | tests ↔ code: OneCopyResult, drop_raw_video, drop_raw_video_job, DROP_RAW_VIDEO, TASK_LABELS, cron */10; hours-page video fragment matches timeline.html | agrees; test name "…audio_markers_and_expired…" creates no marker (naming only) |
| T2 | migration test executes 001…009 raw in a fresh DB; test updates in 5 existing files described in prose | agrees (implementer reads each pinned test) |
| T3 | tests ↔ route/template/places; test_web_ota update_form helper fix mandated | agrees |
| T4 | tests ↔ day_view/_day_by_hour changes (prose); `known_device` defined in Step 3 and used by both routes; _day_by_hour keeps its return type | agrees |
| T5 | tests ↔ Hit fields with defaults, hit_view(named), source_lines format, MCP location fields | agrees |
| T6 | tests ↔ chapter_lines/recap_material formats; distinct minutes avoid same-ms ULID order | agrees |
| T7 | docs only | n/a |
| T8 | controller; asks the owner before deploy | n/a |
No contradictions with the Global Constraints found.
Ruling: implementers run on sonnet (tasks touch several files and run the Docker-backed suite), task reviewers on sonnet, the final review on opus — turn count beats token price — cost if wrong: a slower or costlier task.
Task 1: dispatched implementer (sonnet), BASE d47da2a
Task 1: implementer DONE (03cde39; 1190 passed, 99 %; concern: a segment without a usable MP4 logs one warning every 10 min)
Task 1: review dispatched (sonnet) on review-d47da2a..03cde39.diff
Task 1: review (sonnet) — Spec ✅, Approved, 0 Critical / 0 Important. ⚠️ items checked by the controller: commit trailer present (git log), test output warnings per report.
Task 1: minor (deferred): _has_mp4 checks size > 0 but the remux does tmp.replace(dst) without fsync and _DUE has no minimum age, so a power loss within ~30 s of the remux could leave a partial MP4 after the raw file went — candidate for the final fix wave (fsync the MP4 in _drop before unlinking the raw file).
Task 1: minor (deferred): a segment with no usable MP4 logs one warning per run (every 10 min); consider one count line per run.
Task 1: minor (deferred): test_audio_markers_and_expired_segments_are_not_touched creates no marker; monkeypatch.undo() also reverts autouse env patches; no test that a dropped segment still feeds vlog material; MAX_BATCHES cap untested.
Task 1: minor (deferred): the batch-and-skip loop mirrors retention.py (acceptable at two copies).
Task 1: complete (commits d47da2a..03cde39, review clean)
Task 2: dispatched implementer (sonnet), BASE 03cde39
Task 2: implementer DONE_WITH_CONCERNS (5892212; 1194 passed, 99 %; HANDOFF still says retention ships off — wrap-up; an extra settings-page test updated; test_web_pipeline uses store.set_retention)
Task 2: review dispatched (sonnet) on review-03cde39..5892212.diff
Task 2: review (sonnet) — Spec ✅, Approved, 0 Critical / 0 Important; the reviewer ran 010 against a throwaway container (missing-key, {} and owner rows all valid). Trailer checked.
Task 2: minor (deferred): retention.py:230 apply_retention_job docstring still says "when the owner turned it on" — candidate for the final fix wave.
Task 2: minor (deferred): test_mcp_memory.py:237 now asserts enabled is True after set_mcp, which no longer proves set_mcp leaves retention alone (store enabled=False first and assert it stays False) — candidate for the final fix wave.
Task 2: minor (deferred): no migration test for a stored retention row without the "enabled" key.
Task 2: minor (deferred): docs/HANDOFF.md still says retention ships off — wrap-up HANDOFF update.
Task 2: minor (deferred): settings.html:34 asks "Bật dọn dữ liệu cũ?" on every save while on (pre-existing; now the common path).
Task 2: complete (commits 03cde39..5892212, review clean)
Task 3: dispatched implementer (sonnet), BASE 5892212
Task 3: implementer DONE (76e7da6; 1205 passed, 99 %; two existing tests that INSERT bare device rows now pass location)
Task 3: review dispatched (sonnet) on review-5892212..76e7da6.diff
Task 3: review (sonnet) — Spec ✅, Approved, 0 Critical / 0 Important; htmx inheritance and the pairing INSERT checked clear.
Task 3: minor (deferred): after a save the card title (<strong>{{ d.location }}</strong>) keeps the old name until reload; only the form swaps — candidate for the final fix wave (hx-swap-oob the title).
Task 3: minor (deferred): an invalid name for an unknown/revoked device answers 200 with the form, not 404 (validation runs before the device check).
Task 3: minor (deferred): maxlength="60" duplicates places.LOCATION_MAX in _location.html.
Task 3: minor (deferred): escape test covers element context, not the value="" attribute; route-level refusal tested for blank only; no Unicode NFC normalisation of location names.
Task 3: complete (commits 5892212..76e7da6, review clean)
Task 4: dispatched implementer (sonnet), BASE 76e7da6
Task 4: implementer DONE (de3a7da; 1216 passed, 99 %; 4 extra tests; implementer treats a blank ?device= as no filter)
Ruling: a blank ?device= means "Tất cả" (no filter) on the Day and hours pages — a browser form or a hand-edited URL can send it, and showing an empty day would mislead — cost if wrong: one condition.
Task 4: review dispatched (sonnet) on review-76e7da6..de3a7da.diff
Task 4: review (sonnet) — Spec ✅, Approved, 0 Critical / 0 Important; chapter/loose assembly, SQL parameter order and untouched exact-fragment tests checked.
Task 4: minor (deferred): with a camera filter, has_data counts only that camera's moments, so the page says "Không có dữ liệu cho ngày này." and hides "Tạo lại recap" although other cameras recorded; a filtered camera absent that day is not named in the bar — candidate for the final fix wave.
Task 4: minor (deferred): no test for a chapter holding both cameras' moments under a filter (the chapter stays with only the chosen camera's moments).
Task 4: minor (deferred): the {% set dq %} and the include leave one extra blank line when the bar is hidden (whitespace only).
Task 4: minor (deferred): the device check (blank → None, known_device → 422) and the labels rule are repeated in day_page/timeline and day_view/timeline.
Task 4: complete (commits 76e7da6..de3a7da, review clean)
Task 5: dispatched implementer (sonnet), BASE de3a7da
Task 5: implementer DONE (f819459; 1222 passed, 99 %; one extra /chat sources test)
Task 5: review dispatched (sonnet) on review-de3a7da..f819459.diff
Task 5: review (sonnet) — Spec ✅, Approved, 0 Critical / 0 Important; Hit constructors, hit_view callers and MCP model compatibility checked.
Task 5: minor (deferred): test_results_from_one_camera_are_not_tagged only asserts a negative (would pass on an empty result) — add a positive anchor.
Task 5: minor (deferred): no escaping test at the _hit.html boundary; named+hit_view pairing repeated at two call sites; two ~130-char query lines.
Task 5: complete (commits de3a7da..f819459, review clean)
Task 6: dispatched implementer (sonnet), BASE f819459
Task 6: implementer DONE (e4fc5ac; 1226 passed, 99 %)
Task 6: review dispatched (sonnet) on review-f819459..e4fc5ac.diff
Task 6: review (sonnet) — Spec ✅, Approved, 0 Critical / 0 Important; prompt budgets, per-camera de-dup logic and chapter staleness checked.
Task 6: minor (deferred): no test pins per-camera de-dup with two cameras present (A "x", B "y", A "x" → the second A line dropped) — candidate for the final fix wave (one line + one assert).
Task 6: minor (deferred): no test for a moment whose device row is gone (LEFT JOIN → no place).
Task 6: minor (deferred): renaming a location does not refresh already written multi-camera chapter summaries (signature hashes moment ids only) — a design note for the handoff.
Task 6: complete (commits f819459..e4fc5ac, review clean)
Task 7: dispatched implementer (sonnet), BASE e4fc5ac
Task 7: implementer DONE (1141a3e; all 12 quoted edits matched)
Task 7: review dispatched (sonnet) on review-e4fc5ac..1141a3e.diff
Task 7: review (sonnet) — Spec ✅, Approved, 0 Critical / 0 Important; D30–D36 rows verbatim, nothing else changed, no contradictions among the edits.
Task 7: minor (deferred): P3c design line 4 ("plan … viết sau khi P4 kết thúc; P3c-1 có plan ngày …") and lines 24/119 still say every P3c plan comes after P4 and ask about retention by 25/10 (moot with D32) — candidate for the final fix wave.
Task 7: minor (deferred): binding spec line ~310 "áp dụng chính sách lưu trữ (nếu đã bật trong Cài đặt)" and D20's "kèm bản xem/nghe đã chuyển đổi" read as before D32; §7.4 bullet 3 unqualified next to the new D31 bullet.
Task 7: complete (commits e4fc5ac..1141a3e, review clean)
Final review: dispatched (opus) on review-d47da2a..1141a3e.diff
Final review (opus): With fixes — 0 Critical, 1 Important (I1: one_copy deletes the fsynced raw H.264 while the remuxed MP4 may still be unflushed; a power cut in ~30 s loses the only copy), 9 Minor; full suite 1226 passed at 1141a3e; first run on the ≈ 8 000-segment backlog judged safe (16 batches, SKIP LOCKED with retention, forget waits).
Ruling: ONE final fix wave = I1 + M1 (one count line per run) + M3 (recap marker "at Bếp," separator) + M4 (retention comments/docstring) + M5 (Cài đặt wording after D32; ask "Bật dọn dữ liệu cũ?" only when turning it on) + M6 as a rollback note in deploy/README (no column default: an empty default would hide a code path that forgets the location) + three M9 test fixes (test_mcp_memory:237 intent, two-camera per-camera de-dup, positive anchor in the one-camera search test) + the three stale P3c design lines (4, 24, 119) — all cheap and they touch what ships now — cost if wrong: a bigger fix diff to review.
Ruling: M2 (search/chat label only when the result set spans two devices), M7 (card title stale after a save; invalid name for an unknown device answers 200) and M8 (has_data under a filter) wait for P3c-2, which adds the second camera that makes them visible — with one camera they have no effect — cost if wrong: a confusing label or message once a second camera arrives before P3c-2's fixes.
Ruling: the vlog-material and MAX_BATCHES test gaps wait (reviewer: can wait); HANDOFF wording ("retention ships off") is the controller's wrap-up edit in Task 8.
Final fix wave: DONE_WITH_CONCERNS (55146ff..f5fd9a2, 7 commits; 1233 passed, 99 %; settings_routes.py:82 and pipeline.html:33 still say 'video thô'; the confirm is drawn only when off at page load)
Final fix wave: scoped re-review dispatched (sonnet) on review-1141a3e..f5fd9a2.diff
Final fix wave: scoped re-review (sonnet) — all 8 findings ADDRESSED; new breakage Low only.
Ruling: fsync inside the FOR UPDATE batch transaction (bounded by 500 file fsyncs + one per directory; near no-ops for MP4s older than 10 min) — accepted: the unlinks already run there and a lock wait only delays a forget — cost if wrong: a stalled disk holds row locks for the batch.
Ruling: a directory fsync failing after the remux's rename leaves the MP4 under its final name with the item marked error and its raw file kept — accepted: fail-safe, item_files finds the orphan by its fixed name, a re-remux replaces it — cost if wrong: one orphan MP4 until retention or forget.
Ruling: the two Low test-hygiene points ("7" in the log text can match a ULID digit; monkeypatch.undo() undoes shared-fixture patches) — parked, the real assertions are sound — cost if wrong: a misleading pass in a future edit.
Final fix wave: deferred (P3c-2): "video thô" wording left in web/settings_routes.py:82, templates/pipeline.html:33, _vlog_card.html:9 and settings.html:44; the confirm drawn only when retention is off at page load; README rollback command lacks -p deploy (resolves the same from deploy/); plus M2, M7, M8 by the earlier ruling.
Live numbers for Task 8 (2026-10-20, read-only): first one-copy run 6 959 segments / 39 GB raw H.264, none not yet eligible; nothing ends before the 7-day cutoff of the 03:00 run on 10-21, so retention's first deletions are at 03:00 on 10-22 (recordings of 10-14); 10-14/10-15 carry items of four device ids (loopi-57s1 14, loopi-qh07 96, loopi-a0t1 revoked 4 089, loopi-ve1y) → tags and a filter bar on those two days; SSD 143 GB free (raw 41 GB, view 39 GB).
Tasks 1–7 and the final review: complete (d47da2a..f5fd9a2). Task 8: waiting for the owner's yes.
Task 8: the owner said yes ("Deploy ngay", 2026-10-20); deploying f5fd9a2
Task 8: backup ~/loopi-backups/p3c1-2026-10-20-1621.dump (19 MB); feat/phase2 fast-forwarded to f5fd9a2 in the live worktree; `docker compose -p deploy up -d --build` 16:21:50–16:22:09; smoke OK (smoke device data removed and revoked).
Task 8: checks — migrations 009/010/011 applied 16:22:06; retention {"enabled": true, "audio_days": 7, "video_days": 7}; device.location backfilled (= name) for all 17 devices; drop_raw_video ran at once: 7 019 segments with h264_deleted_at, 0 eligible left, no .h264 file left under media/raw (1.2 GB of audio and markers remain); SSD free 143 → 181 GB; the newest dropped segment's MP4 is served (206, video/mp4, ftyp isom); Day page of 10-20 (one camera): no bar, no tags; 10-14: bar "Tất cả · loopi-57s1 · loopi-a0t1 · loopi-qh07" with tags (old pairings); location save (no-op, same name) answers "Đã lưu."; Cài đặt says "Giữ video … ngày"; search shows place tags where hits span devices.
Task 8: pending (acceptance 6, P3c design §8): on the mornings of 10-21 and 10-22 record df, du of media/raw and media/view, GB added in 24 h, and that no video/audio file is older than 7 days after the 03:00 run of 10-22 (its first deletions: recordings of 10-14) — carried in docs/HANDOFF.md.
Ruling: the Pi keeps its location "loopi-ve1y" until the owner names the room; the three old non-revoked pairings (loopi-57s1, loopi-qh07, loopi-f9cn) are left as they are — naming or revoking them is the owner's call (the plan's "name the Pi Phòng khách" was an example, not the owner's room) — cost if wrong: two days (10-14, 10-15) show technical names in the filter bar until renamed.
Finish: full brain suite on f5fd9a2 — 1233 passed, 1 skipped, 99 %; ledger copied to docs/superpowers/ledgers/p3c1-progress.md

Acceptance 6, morning 1 (2026-10-21 09:26 KST): disk 281 GB used, 163 GB free (64 %); media/raw 1.4 GB, media/view 45 GB; added in the last 24 h: video 7.02 GB, audio 0.20 GB; the oldest kept recordings are of 2026-10-14 19:37 (video) and 19:49 (audio) — the first retention deletions are due at 03:00 on 2026-10-22. Morning 2 (2026-10-22) decides whether free space holds steady.
