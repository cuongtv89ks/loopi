# P2b understanding + UI: pre-flight conflict scan

Plan: `docs/superpowers/plans/2026-10-16-p2b-understanding-ui.md` (8 409 lines, plan commit 1a9850d). Code under test:
`brain/` and `deploy/` of this worktree, which are byte-identical to `54e22f3` (`git diff 54e22f3 HEAD` is the plan file only).
Line numbers are plan lines unless a file is named. Task 14 was scanned only for what it consumes. The findings of
`scratchpad/p2b/p2b-plan-review.md` (I1–I9, M1–M16) are not repeated; plan rulings R1–R53 are not re-argued.

## Method and results

- **Replay in a scratch repo (never the worktree).** `git archive 54e22f3 brain deploy docs/protocol` into a throw-away git
  repo under the scratchpad. A script wrote the 40 `Create` blocks and applied the 59 `diff` blocks with `git apply`
  (exact context, no fuzz), task by task in plan order, and compared each task's `git add` list with the files it changed.
  - All 59 diffs apply. Every Step-2 and Step-1 anchor exists in the file as the previous task left it.
  - For every task 1–13: `Files:` list = `git add` list = set of changed files (same paths, no extra, none missing).
  - Task 1 Step 3 (spec): the Python script's `assert s.count(...) == 1` lines hold on `git show feat/phase1:…`; the result
    differs from `feat/phase1` by `4 insertions(+), 3 deletions(-)`, as stated (300).
  - `pytest --collect-only` after each task gives 345, 358, 372, 391, 406, 420, 432, 462, 471, 480, 491, 513, 516 tests =
    the plan's cumulative "N passed, 1 skipped" + 1 (344, 357, 371, 390, 405, 419, 431, 461, 470, 479, 490, 512, 515).
    Per-step totals also agree: 11, 43, 14, 19, 15, 14 and 40, 12, 30, 9, 25, 11, 52, 42.
  - Reproduced by running them: Task 2 Step 2 "3 failed, 31 passed" and Step 6 "43 passed"; Task 3 "14 passed"; Task 12 Step 2
    "1 failed, 30 passed" and `test_providers.py` 31 passed; Task 1 `test_days.py` 3; Task 6 `test_markdown.py` 3.
- **Whole suite with the database fixture stubbed (no Docker).** 218 passed, 1 skipped, 7 failed and 290 errors, all of
  them `psycopg` connection refused. So the 218 database-free tests pass on the replayed code (all of `test_llm`,
  `test_segment`, `test_days`, `test_markdown`, `test_config`, `test_providers`, `test_deploy`, 20 of 21 in `test_anthropic`,
  20 of 30 in `test_chat`). The other 297 tests need Postgres: **not executed by me**.
- **Static checks on the 51 changed `.py` files:** AST parse; undefined names; unused imports; `module.attr` references resolve
  (only hit: the local `retention` in `settings_routes.py`, see M6); 155 `conn.execute` calls with literal tuple or dict
  parameters have matching placeholder counts; every module imports in a fresh interpreter (no import cycle); the route
  table of the replayed app has every path Task 14 calls.
- **Briefs.** `.superpowers/sdd/…/task-N-brief.md` are verbatim slices of the plan (only a trailing `---` differs), so every
  correction below must also be re-sliced into the brief.
- **Not checked:** database-backed behaviour, coverage figures, Task 14's live steps, the Anthropic API facts (R26, R47).
- **Side effects of this scan:** only the scratch repo under the scratchpad and this file. No Docker command; read-only git
  (`archive`, `show`, `status` with optional locks off) on `loopi-p2` and this repo.

## A. Task-pair rows (shared file or interface)

| # | Tasks | Producer → consumer (exact names, plan lines) | Finding |
|---|---|---|---|
| A1 | 1→6 | `web/routes.py`: T1 removes `day_bounds_utc` and imports it from `days` (425–450); T6 changes the `_day_by_hour` SELECT and `@router.get("/")` → `"/hours"` (3650–3673). `item.raw_deleted_at` (311) → `_day_by_hour` (3658), `day_view` (3509). `tests/test_web.py:7` still imports `day_bounds_utc` from `web.routes` | match; P2a `test_day_bounds_utc_for_seoul` duplicates T1's `test_a_seoul_day_starts_at_15_utc…` (139–143) → M14 |
| A2 | 1→13 | `web/routes.py`: T13 adds `"understanding"` to `pipeline_page` (7941–7953); anchor is P2a text | match; File Structure (96) lists T1 and T6 for this file, not T13 → M12 |
| A3 | 2→4 | `helpers.py`: `FakeOllama.reply` (624–657) → T4/T5 `ollama.reply = lambda body: …` (1363, 2276); T4 hunks (1258–1323) are disjoint. `llm.ASR_CAVEAT`, `PICTURE_NOTE`, `AnswerInvalid`, `clip`, `clipped`, `parse_answer`, `spread`, `within` (823–889) → `chapters.py` imports (1823) | match |
| A4 | 2→7 | `config.py`: T7 hunk context = T2's three `llm_*` lines (4106–4108 ↔ 682–684) | match |
| A5 | 2→8 | `ollama.py`, `router.py`, `helpers.py`: T8 anchors on T2's `timeout=self._timeout_for(model)` (4879–4883 ↔ 790), `text_model` branch (reused by `_chat_payload`), `answer = self.caption if self.reply is None …` (4523 ↔ 651–653). `stream_chat` uses `_timeout_for` (4892); asserted 900 s (4652) | match (replayed) |
| A6 | 2→12 | `providers/config.py`, `router.py`, `test_providers.py`: T12 anchors on `TEXT_TASKS` and `ollama_text_models` (7424–7425, 7441 ↔ 699, 709–711) and on `text_timeout=settings.llm_timeout_s` (7724 ↔ 812); `providers_used()` (7444) is read by `build_router` (7732). T2 appends tests at EOF (545–594), T12 edits two P2a tests (7340–7366) | match |
| A7 | 4→5 | `jobs.py`, `status.py`: T5 anchors on T4's `AUTO_RERUN_TASKS` line (2543 ↔ 1665) and `TASK_LABELS` (2556–2559 ↔ 1756–1758). `jobs.enqueue(…, priority=None)` (1675–1679) → `defer_recap` (2845–2848); `LLM_LOCK`, `LLM_PRIORITY` (1669–1670) | match |
| A8 | 4→8 | `helpers.py`: T8 hunks (4503–4546) vs T4's (1262–1322): disjoint. `insert_moment(axis=None, …)` keeps P2a callers valid | match |
| A9 | 4→11 | `jobs.py`, `status.py`: T11 anchors on T5's multi-line `AUTO_RERUN_TASKS = (` (6808–6811 ↔ 2544–2546) and `TASK_LABELS`; `APPLY_RETENTION` joins `NOT_RETRIED` through `AUTO_RERUN_TASKS` (label test 6770–6775) | match |
| A10 | 4→13 | `status.py`: T13 adds `from .. import store` (7903) and `understanding_status` (7911–7933); reads `chapter.summary_signature`/`signature` (T1 330, 326) | match; the card counts every chapter with `summary_signature IS DISTINCT FROM signature` (7916), the summariser only closed ones or ones with ≥ 3 moments (2047–2048) → M11 |
| A11 | 5→11 | `jobs.py`, `status.py`: same hunks as A9 | match |
| A12 | 5→13 | `status.py`: card reads `procrastinate_jobs` with `task_name = jobs.WRITE_RECAP` (7922) and `day_recap.generated_at` (T1 345) | match |
| A13 | 6→7 | `app.py`, `base.html`: T7 anchors on T6's `Hôm nay` nav line (4444 ↔ 3821), `form.inline` rule (4434 ↔ 3812), `include_router(day_router)` (4170 ↔ 3691) | match |
| A14 | 6→9 | `app.py`, `base.html`: T9 anchors on T7's nav line (5923 ↔ 4445), `search_router` import (5900 ↔ 4129) and P2a `HX_ERRORS` (5928–5929) | match |
| A15 | 6→10 | `media.py`: T6 `inside` (3420–3427); T10 `remove_files` (6223–6236) calls `inside` and is placed after it (context 6219 ↔ 3427) | match |
| A16 | 6→13 | `test_web_pipeline.py`: T6's five `/hours` edits (3237–3279) vs T13's appended test (7833–7860); it uses `UTC_9AM_SEOUL`, `_fail_job`, `jobs`, `dt` of the P2a module. `routes.py` → A2 | match (replay: no undefined name) |
| A17 | 7→8 | `search_routes.py`, `search.html`: T8 anchors on T7's docstring, imports, `SEARCH_LIMIT` (5209–5236 ↔ 4320–4336) and the form/hits block (5301–5321 ↔ 4407–4421). `hit_view` dicts (4339–4354) → `_sources.html` includes `_hit.html` (5292 ↔ 4397–4401). `app.state.llm` (4163) → `request_router(state.settings, state.llm)` (5269) | match |
| A18 | 7→9 | `app.py`, `base.html`: same hunks as A14 | match |
| A19 | 7→12 | `config.py`: T12 hunk context = T7's `search_min_similarity` (7468 ↔ 4112) | match |
| A20 | 8→12 | `router.py`, `base.py`: T12's `_usable` rewrite (7683–7702) keeps T8's `stream_chat` call `self._usable(task, images=False)` (4961); a `ProviderUnavailable` raised there is a `ProviderError`, caught by `chat_events` (5188). `Provider.stream_chat` (4826–4828) = `AnthropicProvider.stream_chat` signature (7624–7626). T12's `base.py` hunk anchors on T8's `AsyncIterator` import (7384 ↔ 4817) | match |
| A21 | 9→10 | `settings_routes.py`, `settings.html`: T10 anchors on T9's docstring and import line (6448–6459 ↔ 5743, 5752) and the `#retention-result` block (6505–6507 ↔ 5863–5865) | match |
| A22 | 9→11 | same files: T11 anchors on T9's `settings_context` dict (7021–7024 ↔ 5791–5793) and the retention paragraph (7035 ↔ 5862) | match; T11's `from ..pipeline import retention` (7014) is shadowed by T9's local `retention` in `save_retention` (5807) → M6 |
| A23 | 9→12 | same files: T12 anchors on T9's `providers_view` (7747–7760 ↔ 5771–5782) and the `{% endif %}` after the privacy paragraph (7775–7777 ↔ 5844–5846) | match |
| A24 | 10→11 | same files: T11's `settings.html` hunk sits at the retention paragraph, before T10's section (7032–7040); `settings_routes.py` hunks differ only by offsets | match (replayed) |
| A25 | 10→12 | same files: no overlap | match |
| A26 | 11→12 | same files: T11 touches `settings_context` and the retention paragraph, T12 `providers_view` and the privacy paragraph | match |
| A27 | 11→13 | `status.py`: only the `store` import and the new function; `Retention.enabled`, `video_days`, `audio_days` (5631–5633) → `pipeline.html` (7970) | match |
| A28 | 1→4 | Tables `chapter`, `day_job_error`, `forget_log` (319–388) → `chapters.py` SQL (1936–1991, 2045–2086, 2115–2119, 2145–2164) and `state.py` (1715–1740); `moment_created_at` (314) → planner `created_at >` (2145); `chapter_time_order` (336) ↔ `max(p.end …)` (1971) | match |
| A29 | 1→5 | `day_recap(stale, generated_at)`, `commitment(source_moment_id … ON DELETE SET NULL)` (341–358) → `recap.py` (2814–2833, 2867–2874) | match |
| A30 | 1→9 | `setting(key, value, updated_at)` (363–367) → `store.py` upsert (5646–5647); `APP_TABLES` now holds `setting` (253), so a stored retention row does not leak between tests | match |
| A31 | 1→10 | `forget_log(pending_files, forget_range_order)` (371–379) → `forget.py` INSERT (6423–6424); T1 test (241–244) | match |
| A32 | 1→11 | `item.raw_deleted_at`, `item_retention` (311–312) → retention `_DUE` (6887), UPDATE (6962) | match |
| A33 | 2→5 | `llm` helpers → `recap.py` imports (2595): all five names exist (823–889) | match |
| A34 | 3→4 | `ChapterParams`, `Point`, `segment`, `closed_count`, `signature`, `unit` (924–929, 1095–1199) → `chapters.py` (1829, 1954–1971); `closed_count(segments, horizon, day_end)` ↔ call (1959) | match |
| A35 | 4→6 | chapter columns `title`, `summary`, `summary_signature`, `signature`, `last_moment_at`, `ts_start`, `ts_end` → `day_view` (3505, 3553–3561); `outdated` rule = R6 | match |
| A36 | 4→9 | `chapters.defer_build(pool, day, zone)` (2110) → `reprocess` (5732) | match |
| A37 | 4→10 | `defer_build` → `forget` (6434); `state.lock_day(conn, day)` (1728) → `forget` (6277, 6396); `forgotten()` counts `forget_log` rows (1734–1740) which T4/T5 tests insert with `id, range_start, range_end, counts` (1599, 2454): all NOT NULL columns given | match |
| A38 | 5→6 | `recap.defer_recap`, `set_commitment_status`, `STATUSES` (2842, 2891, 2616) → `day_routes` (3456, 3628, 3635–3638) | match for names; behaviour of a failed recap → I4 |
| A39 | 5→9 | `defer_recap` → `reprocess` (5734) | match |
| A40 | 5→10 | forget guard: `recap` stores only if `forgotten(...)` is unchanged under `lock_day` (2808–2812) ↔ forget takes the same lock and inserts the row (6396, 6423) | match |
| A41 | 6→8 | `TEMPLATES`, `require_htmx`, `MARKER_LABEL` (3457) → `search_routes` (5230, 4331); anchors `id="m-<id>"` (3707–3729) ↔ `hit_view.link` `/?day=…#m-<id>` (4353) | match |
| A42 | 6→11 | day page expiry text (3479–3480) and `/hours` "Đã hết hạn" (3874) ↔ retention marks `raw_deleted_at`, `view_path = NULL` (6962); test 3146–3154 | match |
| A43 | 9→13 | `store.get_retention(pool).enabled` (5636–5640) → `understanding_status` (7932) | match |
| A44 | 12→13 | `ANTHROPIC_API_KEY` alias (7471) ↔ compose `api`, `worker` (7991, 8002), `.env.example` (8044), `test_deploy` (7877–7883) | match; the providers assertion is weaker than its comment → M8 |
| A45 | 13→14 | Task 14 consumes: routes `/`, `/hours`, `/search`, `/settings`, `/pipeline`, POST `/chat` (all in the replayed route table); `/chat` form field `q` + `HX-Request` (5266, 5265); `setting.retention` JSON `{video_days, audio_days, enabled}` (5631–5633) ↔ SQL (8308); `schema_migration(name, applied_at)` (db.py 29–30); rollback task names (8136) = constants in T4/T5/T11; `procrastinate_events.type` `started`/`succeeded` exist; `models pull` and `SMOKE_ANALYSIS_TIMEOUT` exist (cli.py 29, smoke.sh 25); `feat/phase2` is an ancestor of `feat/p2b`; live checkout clean at 54e22f3 | match |
| A46 | 1–13→14 | Facts Task 14 states without a source: the "04:28 KST" gate (8117, 8390) is in no committed ledger (`grep -rn 04:28 docs`); `$ROLLBACK` (8129, 8194) is a shell variable that does not survive separate commands; the rollback note "no restore is needed" (8140) is true for the database only: files retention already deleted do not come back | M12, M16 |

## B. Per-task self-consistency (Tasks 1–13, Task 14 as a consumer)

| Task | Tests ↔ code (names, signatures, values) | "Expected" counts | `Files:` ↔ `git add` | Undefined references / anchors | Finding |
|---|---|---|---|---|---|
| B1 · T1 | `days.*` (397–421) ↔ tests (130–156); CHECK names (336, 354, 378) ↔ three `CheckViolation` asserts (232–244) | 11 = 3 + 8 (455) ✓; 344 + 1 skipped ✓ (345 collected); "4 failed, 2 passed, 2 errors" = 8 ✓ | 112–117 ↔ 463–465: same 7 paths ✓ | none; 4 diffs + spec script apply | M14 |
| B2 · T2 | `OllamaProvider` kwargs ↔ test (557–559); timeout dict (569) ↔ `_timeout_for` (759–763): pure run passes | "3 failed, 31 passed" and "43 passed" reproduced; 357 + 1 ✓ | 481–485 ↔ 902–904: same 9 ✓ | none | none |
| B3 · T3 | 14 tests ↔ `segment.py`: all pass on replay | 14 reproduced; 371 + 1 ✓ | 917–919 ↔ 1213: same 2 ✓ | none | none |
| B4 · T4 | names, `chapters.*` constants and `BuildResult` ↔ 19 tests; `devices.new_id`, `state.error_text`, `jobs_by_lock`, `make_ctx(ollama=)`, `run_jobs`, `fetch_one` exist | 19 ✓; 390 + 1 ✓ | 1227–1230 ↔ 2190–2192: same 6 ✓ | none; text at 1238 is stale | I1 (1554–1590); M1; M12 |
| B5 · T5 | `recap.*` constants, `Material`, 15 tests; `AnswerInvalid` imported from `llm` | 15 ✓; 405 + 1 ✓ | 2206–2208 ↔ 2925–2926: same 4 ✓ | none | I4 (2871–2873 vs 3750) |
| B6 · T6 | `day_view` keys (2956) ↔ template; nine `/` calls moved = 9 in P2a (test_web 3, web_pipeline 5, audio_playback 1) ↔ three diffs | 14 = 3 + 11 ✓; 40 = 12 + 14 + 14 ✓; "10 failed, 1 passed" = 11 ✓; "8 failed, 32 passed" = 40 ✓; 419 + 1 ✓ | 2940–2944 ↔ 3895–3900: same 15 ✓ | none | I4 (3622–3629, 3527–3533, 3750–3751); M9 (3706–3719) |
| B7 · T7 | `search.*` ↔ 12 tests; `<%%` escaping ↔ `_by_words` (4271); template strings ↔ asserts (4056–4058) | 12 ✓; 431 + 1 ✓ | 3918–3921 ↔ 4462–4464: same 8 ✓ | none | M10 (`request_router` None path untested) |
| B8 · T8 | NDJSON pieces of 8 characters ↔ `len(pieces) == 3` (4726); `names[:4]` (4643) ↔ `chat_events` order; `[2] or [1][3]` (4654) ↔ `ANSWER_SYSTEM` (5052) | 30 = 9 + 1 + 1 + 1 + 1 + 1 + 7 + 9 ✓; 461 + 1 ✓ | 4478–4480 ↔ 5382–5385: same 9 ✓ | none | I2 (5115–5116); M10 (5177–5180) |
| B9 · T9 | `Retention` fields ↔ asserts (5469, 5485–5488); `ReprocessResult` field names ↔ asserts (5565, 5587) | 9 ✓; 470 + 1 ✓ | 5399–5402 ↔ 5948–5950: same 8 ✓ | none | M6 (5807) |
| B10 · T10 | `ForgetCounts` field order (6286–6295) ↔ asserts (6086, 6122); `files=7` = raw ×3 + view ×2 + keyframe + thumb | 9 ✓; "25 passed" = 9 + 16 (`test_media`) ✓; 479 + 1 ✓ | 5964–5967 ↔ 6560–6562: same 7 ✓ | none | I3 (6359–6374); M8 (6166); M15 (6372) |
| B11 · T11 | `RetentionResult(video=2, audio=1, files=6)` ↔ test 1 (6661); `RETENTION_HOUR` ↔ 18:00 UTC (6707) | 11 ✓; 490 + 1 ✓ | 6576–6579 ↔ 7055–7057: same 7 ✓ | none | I3 (6899–6905); M6 |
| B12 · T12 | `FakeAnthropic` ↔ `AnthropicProvider` (headers, `thinking`, `max_tokens`, SSE); `THINKING_OFF` ↔ test (7171–7183) | "1 failed, 30 passed" reproduced; "52" = 31 + 21 ✓; 512 + 1 ✓ | 7071–7075 ↔ 7798–7801: same 9 ✓ | none | M4; M7 |
| B13 · T13 | card text ↔ test (7844–7859, whitespace collapsed); compose keys ↔ `test_deploy` (7873–7883) | "3 failed, 39 passed" and "42" = 19 + 15 + 8 ✓; 515 + 1 ✓ | 7815–7817 ↔ 8100–8102: same 9 ✓ | none | M8 (7883); M11 (7916); M13 (8091) |
| B14 · T14 | consumption only: A45, A46 | — | `docs/HANDOFF.md` exists; ledger file is created here (8120) | `p2b-progress.md` is also named at 18 as the place to copy rulings "before Task 1" | M12; M16 |

## C. Global Constraints (plan lines 37–49) vs the tasks

| # | Line | Constraint | What the tasks do | Verdict |
|---|---|---|---|---|
| C1 | 37 | spec is binding; D20 | D20 row (288) and §9.5 text (294) say deletion runs after 7 days; tasks ship it off (R46). R24's deletion of recaps differs from §9.5 "đánh dấu … sinh lại" and is tagged only in R24 (8365), not in Task 10 (5976) | M3 |
| C2 | 38 | local-first, allow-list, text only to Anthropic | `LOCAL_PROVIDERS` (7391), anthropic only for `TEXT_TASKS` at load (7435), images only to local routes (7689–7692), `IMAGE_TASKS` (7427); Cài đặt names the tasks that may send text (7780) | match |
| C3 | 39 | models | `DEFAULT_CONFIG` untouched; `providers.yaml` comment-only (8019–8032); P2a equality test stays green (test_providers 31 passed) | match |
| C4 | 40 | `<provider>:<model>` stored | `result.model_version` for chapters (2083), recap (2817), commitments (2832); forget clears it with the title (6412) | match |
| C5 | 41 | retries and dead letters | exceptions listed; planners, `build_chapters`, `apply_retention` in `AUTO_RERUN_TASKS`; `write_recap` keeps its button | I4, M1, M5 |
| C6 | 42 | live work first | worker concurrency 3 (8001), `LLM_PRIORITY` (1670), lock `llm`; chat not serialised (R28) | match |
| C7 | 43 | ingest and the device contract untouched | no task edits `api/` or the YAML; T7 only adds `create_app(llm_client=)` (4137) | match |
| C8 | 44 | retention rules | match except the spec text (C1). Media files are removed only in `media.remove_files`, which checks `inside` (6229); thumbnails are written, never deleted; `reprocess` deletes rows only | M3 |
| C9 | 45 | Vietnamese UI; `HX-Request` on every POST; model text escaped | all 7 new POST routes use `Depends(require_htmx)` (3622, 3632, 5265, 5802, 5818, 6480, 6490) with 403 tests; Jinja autoescape, `render_markdown`, `render_answer`, `textContent` for tokens | match |
| C10 | 46 | no new dependency | no `pyproject.toml` or `uv.lock` in any `git add`; Pillow, numpy, anyio, python-multipart, pyyaml are declared; `markupsafe` comes with jinja2 | match |
| C11 | 47 | TDD, coverage, commits, secrets, Docker | tests first in every task. Rubric hits: I2, I3, M8, M9, M15. 15 commit blocks hard-code "Claude Opus 5.5". `sk-ant-test-0000` (7109) is a test literal, no secret | I2, I3, M2 |
| C12 | 48 | P1 + P2a suite stays green; changes listed in R38 | R38 list = the nine existing test files the replay changed (conftest, helpers, test_db, test_providers, test_config, test_web, test_web_pipeline, test_audio_playback, test_deploy); baseline 341 collected = 340 + 1 | match |
| C13 | 49 | no Docker, `smoke.sh`, `build.sh` in Tasks 1–13 | every `Run:` line is pytest, except `bash -n ../deploy/smoke.sh` (8091) | M13 |

## Findings

No Critical finding. Four Important, sixteen Minor. A correction that changes plan text must be re-sliced into the briefs.

### Important

**I1. Two planner tests turn red as the real date moves on.** Lines 1554–1560, 1563–1572, 1575–1590 (code at 2145–2148).
- `_old()` writes `moment.created_at = now() - N days` from the **database clock**, while `plan_chapters` receives a **fixed**
  `now` (`NINE + 1 day`, `NINE + 60 days`) and keeps every day with `created_at > now − 26 h`.
- Today (2026-10-16) the 10-day-old row is "not recent". From 2026-10-23 22:00 UTC it is, so `old_day` is returned by the
  first branch and `assert old_day not in …` (1582) fails for good; the 30/31-day catch-up test does the same from 2027-01-10.
- **Smallest fix:** in `_old`, `UPDATE moment SET created_at = %s` with `NINE - dt.timedelta(days=days_ago)` (the clock the
  planner is given); drop `now()` and the `make_interval`.

**I2. Marker-note extraction and the hour formatter are written out again in three and two places, although `moments.marker_note` exists.**
Lines 2018–2020 (`chapters.py`), 2728–2730 (`recap.py`), 5115–5116 (`chat.py`); `_hm` at 1880–1881 and 2692–2693.
- `moments.marker_note(meta)` (P2a, `pipeline/moments.py:21`) returns the stripped note or `None`; T6/T7 already use it (3476, 4344).
  The other three re-implement `(meta or {}).get("note")` + `isinstance(note, str) and note.strip()`.
- The line `"<time> the user pressed the mark button[: note]"` is built identically in `chapter_lines` and `recap_material`; `_hm` is
  the same two lines in both modules.
- The reviewer rubric calls verbatim duplicated logic Important (plan-mandated); expect it in the Task 4/5/8 reviews.
- **Smallest fix:** call `moment_rules.marker_note(...)` in the three places (`from . import moments as moment_rules`;
  `from .pipeline import moments as moment_rules`), put `hm(ts, zone)` in `days.py` (Task 1) and a shared
  `marker_line(at, note)` in `llm.py` (Task 2), keep the asserted string (2320) unchanged.

**I3. The rule "which files belong to an item" exists twice.** `forget._files` (6359–6374) and `retention._paths` (6899–6905):
raw path; `view_path` for video; `media.audio_view_path(PurePosixPath(raw))` for audio. A new copy type changes one and not the
other; Task 10's "nothing outside the media root" and Task 11's retention must agree.
- **Smallest fix:** `media.item_files(kind, raw_path, view_path) -> list[str]` next to `remove_files` (Task 10, 6223); both
  modules call it; Task 11's `_paths` disappears.

**I4. After a failed recap the page advises a click that does not clear the failure, and the planner never plans that day again.**
Lines 3622–3629, 3527–3533, 3750–3751, 2871–2873.
- The Day page says "Bấm 'Tạo lại recap', hoặc 'Xử lý lại' ở trang Xử lý". `rewrite_recap` only marks the recap stale and
  defers a **new** job; the old `failed` job (queueing lock `recap:<day>`) stays.
- `day_view` keeps selecting that failed job, so the banner "Viết recap lỗi." stays next to the new recap, and `plan_recaps` skips
  every day with a failed `recap:<day>` job (2871–2873) forever, so a later change of the day's chapters is never written up
  unless the dead letter is retried from /pipeline. The test at 2478–2480 pins only the skip.
- **Smallest fix:** in `rewrite_recap`, before `defer_recap`, `DELETE FROM procrastinate_jobs WHERE queueing_lock = %s AND status = 'failed'`
  (events cascade; same idea as `dispatch.prune_failed_auto_runs`) and `clear_day_error`; extend
  `test_rewriting_the_recap_marks_it_stale_and_asks_for_a_new_one` (3157–3165): failed job + POST → no banner, and
  `plan_recaps` can plan the day again.

### Minor

- **M1. R40's cost line (8385) is wrong for today, yesterday and open days.** `summarise_chapters` has no cooldown (2045–2050);
  only the extra-days clause of the planner has one (2153). A chapter whose answer is always invalid costs the job and its 3
  retries, i.e. 4 `qwen3:4b` calls, **every hour**. Fix: add `AND (summary_error_at IS NULL OR summary_error_at < now() - 6 h OR attempt > 0)`
  to the SELECT with a test, or correct R40 ("4 calls per hour for today, yesterday and open days; 1 per 6 h for older days").
- **M2. Commit trailers.** 15 blocks (472, 908, 1217, 2196, 2930, 3906, 4468, 5389, 5954, 6566, 7061, 7805, 8106, 8162, 8328) hard-code
  `Co-Authored-By: Claude Opus 5.5`; `globals.md` and the implementer instructions say "naming your model" (the session's
  trailer names Sonnet 5.5; `git log` already holds both). Fix: say "trailers as in globals.md" in the plan, or use a placeholder.
- **M3. Spec and brief carry-over.** D20's text (288) and §9.5 (294) say the nightly job deletes; the code ships it off (R46, line 44).
  R24's recap deletion is a deliberate deviation from §9.5 that only R24 (8365) mentions; reviewers see Task 10 and `globals.md`.
  Fix: add "chỉ chạy sau khi người dùng bật trong Cài đặt (mặc định tắt)" to the D20 row in Step 3, and a "Pre-flight rulings"
  paragraph on R24 in the Task 10 brief.
- **M4. Three tests read the ambient environment.** With `ANTHROPIC_API_KEY` exported: `test_the_api_key_comes_from_…`
  (7284–7291; first assert has no `delenv`), `test_routes_to_anthropic_are_used_only_with_a_key` (7294–7317) and the settings-page
  test (7320–7336) fail, because the `settings` fixtures read the process environment. It is unset here (checked), so green today.
  Fix: autouse `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` in `conftest.py` (then in Task 12's Files and `git add`).
- **M5. Stalled per-day jobs have no error text.** `dispatch.recover_stalled` (P2a) stores the stalled error only for `item_id` and
  `keyframe_id` kwargs; a `build_chapters` or `write_recap` job dead-lettered by repeated worker kills shows on /pipeline and the Day
  page with an empty reason. Not covered by any task. Fix: an `elif "day" in target:` branch writing `day_job_error` (test-first), or list it under Deferred.
- **M6. Shadowing.** `save_retention` binds a local `retention` (5807) while Task 11 imports the module `retention` at the top of the
  same file (7014). It works; a reviewer will flag it. Fix: name the local `chosen`.
- **M7. Warning at every call.** `build_router` logs "ANTHROPIC_API_KEY is not set …" (7732–7733) each time it runs: per search and
  chat request (4377, 5269) and per /pipeline or /settings view (P2a `model_health`). Fix: warn once (module flag) or at start-up.
- **M8. Weak or environment-bound tests.** `assert json.dumps(chapters.SUMMARY_SCHEMA)` (1629) is always true (fix:
  `json.loads(json.dumps(x)) == x`); the new deploy assertion (7883) checks primary providers only and repeats P2a's
  `test_committed_providers_yaml_equals_built_in_defaults` (use `providers_used()` or drop it); `chmod 0o555` (6166) cannot stop a
  root process from deleting, so the test fails when the suite runs as root (add `skipif(os.geteuid() == 0)`).
- **M9. Duplicated markup and test helpers.** `day.html` repeats the `<figure>` block for `frames` and `more_frames` (3706–3719) (macro `frame(m)`);
  `_write` is verbatim in `test_forget.py` (6023–6027) and `test_retention.py` (6624–6628) (move to `helpers.py`); `NINE`, `DAY`, `HX`,
  `dev`, `web` repeat per test file (P2a style).
- **M10. Untested branches.** `search.request_router` returning `None` (4238–4244), `chat_events` with `router is None` (5177–5180) and the
  `except Exception` fallback of `chat_answer` (5281–5284). Fix: one test with a broken `providers_file` for each.
- **M11. Card number.** `understanding_status` counts every chapter with `summary_signature IS DISTINCT FROM signature` (7916), including
  today's open tail with fewer than 3 moments and chapters in their 6 h cooldown, so "chương chờ tóm tắt" can stay above 0 all day.
  Fix: `AND (closed OR moment_count >= 3)` as in 2048.
- **M12. Plan text drift.** 1238 says "Task 13's job wrapper passes `ctx.router`" (the wrapper is in Task 4, 2167–2171); File Structure (96)
  omits Task 13 for `web/routes.py`; rulings cite review ids M3, M10, M15, M16 and I1–I9 that live only in a scratch report; line 18 names
  the committed `docs/superpowers/ledgers/p2b-progress.md` as the place to copy rulings "before Task 1", yet Task 14 creates it
  (8120) and Step 1 (8148) needs a clean tree (the working ledger is the ignored `.superpowers/sdd/…/progress.md`); `$ROLLBACK`
  (8129, 8194) does not survive separate shell calls. Fix: reword each; say "substitute the SHA recorded in Step 3".
- **M13. `bash -n ../deploy/smoke.sh`** (8091) checks a file the plan leaves unchanged, repeats P2a's `test_smoke_script_is_valid_bash`, and
  names `smoke.sh` against the literal "never run `deploy/smoke.sh`" (49). Fix: drop it.
- **M14. Duplicate test.** T1's `test_a_seoul_day_starts_at_15_utc_the_day_before` (139–143) asserts what P2a's `test_day_bounds_utc_for_seoul`
  (`test_web.py` 22–25) already asserts. Keep one.
- **M15. Silent skips.** `forget._files` swallows `ValueError` with `pass` (6372–6373); `retryable` drops paths silently (6380–6384).
  Fix: `log.warning` in both.
- **M16. Unsourced gate.** "Not before 2026-10-17 04:28 KST" (8117, 8390, progress.md) appears in no committed ledger. Fix: cite the ledger
  line, or have Step 1 read the window start from the live worker (`docker compose -p deploy ps`) instead of the literal.

## Row count

Section A: 46 rows (27 shared-file pairs + 19 interface-only rows). Section B: 14 rows (Tasks 1–13 and Task 14 as a consumer).
Section C: 13 rows. Total 73 rows.
