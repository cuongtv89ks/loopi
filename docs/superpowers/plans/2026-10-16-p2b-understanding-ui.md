# P2b — Understanding + UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the brain its understanding layer and the pages to use it: hourly chapters with Vietnamese titles and summaries, the 23:30 recap with sourced commitments, the Day page by chapters, hybrid search and cited chat answers streamed over SSE, the Cài đặt page (providers, retention, "Quên khoảng thời gian", reprocess a day), nightly retention (new decision D20: raw audio is kept 7 days, like video) and an optional Anthropic provider for text tasks.

**Architecture:** Same shape as P2a: every stage is a plain async function over a `PipelineContext`, wrapped by a thin Procrastinate task, so tests call stages directly. Per-day jobs (`build_chapters`, `write_recap`) are deferred by periodic planners, identified by queueing locks (`chapters:<day>`, `recap:<day>`) and serialised on one `llm` job lock. Chapter boundaries come from a pure, deterministic segmentation of the day's moments (gaps, caption drift and speech-topic drift on the P2a embeddings); settled chapters are closed and never recomputed, the open tail is rebuilt every hour, and only chapters whose moments changed are summarised again. The api serves the Day, Tìm kiếm & Chat and Cài đặt pages (Jinja + HTMX; the chat answer streams Server-Sent Events over a `fetch` POST) and calls the provider router itself for query embeddings and answers; the router gains streaming, per-model options for the text models, a privacy allow-list and an Anthropic provider (plain httpx, text only).

**Tech Stack:** Python 3.13, uv, FastAPI/Starlette (`StreamingResponse`, `Form`), Jinja2 + MarkupSafe, HTMX (already vendored), vanilla JS `fetch` streams, psycopg 3, Procrastinate 3.10.0, pgvector 0.8 (HNSW, iterative scans), pg_trgm `word_similarity`, NumPy, Pillow, httpx; Ollama 0.35.1 (`qwen3:4b`, `qwen3:8b`, `qwen3-embedding:0.6b`), Anthropic Messages API (optional); pytest + pytest-asyncio + testcontainers (`pgvector/pgvector:pg17`), `httpx.MockTransport` fakes. **No new dependency.**

**Spec:** `docs/superpowers/specs/2026-10-14-loopi-design.md` (Vietnamese, binding): §9.2 (periodic jobs), §9.3 (provider router), §9.4 (tables), §9.5 (retention), §10.1, §10.2, §10.4, §10.6, §12 ("Đổi model" → reprocess by day), §14 P2. Task 1 adds decision **D20**. The design brief (owner direction 2026-10-16, "giờ hãy chỉ tập trung vào P2") is session scratch; its decisions are restated here so this plan stands alone:
- P2 is split: **P2a** perception (done; live since 2026-10-15 23:51 KST), **P2b** understanding + UI (this plan), **P2c** evaluation + acceptance (golden sets, WER/CER, recall@5, tuning — not here). Vlog (§10.3) and MCP (§10.5, including the MCP token UI) are P3.
- **D20 (owner, 2026-10-16):** the brain keeps raw audio 7 days, like video (spec §9.5 said "delete right after the transcript"), editable in Cài đặt: transcripts are not reliable yet (Whisper hallucinations on noise, faint speech) and the owner replays normalised audio (D19).
- Work in the worktree **`/home/soul/loopi-p2b`**, branch **`feat/p2b`** (from `feat/phase2` @ `54e22f3`: P1 + P2a + audio level). The live stack (compose project `deploy`) runs from `/home/soul/loopi-p2/deploy` and bind-mounts `Caddyfile` and `providers.yaml` from there. Task 14 (controller, after the owner's yes) fast-forwards `feat/phase2` and redeploys.

**Plan shape:** 14 tasks. **Tasks 1–6 are shippable on their own** (schema, chapters with the catch-up of old days, recap and commitments, Day page) — an early deploy of them also needs Task 13's compose change (`LOOPI_WORKER_CONCURRENCY: "3"`) and Task 14's gates and checks. Tasks 7–13 add search and chat, Cài đặt, forgetting, retention, Anthropic and the deploy files; Task 14 deploys.

**Ledger:** every ruling taken while executing goes to `docs/superpowers/ledgers/p2b-progress.md` (CLAUDE.md). Copy "Rulings taken while writing this plan" (end of this file) into it before Task 1.

**Prototype:** every code block below ran in a throw-away worktree replayed task by task from `54e22f3`, and every "Expected" is what that run printed. Baseline: brain **340 passed, 1 skipped, 98 %**; after Task 13: **515 passed, 1 skipped, 98 %**. This revision applies the independent plan review of 2026-10-16 ("Ready with fixes": I1–I9 and the cheap minors; the controller's rulings are in R39–R53).

**How to read the code blocks:** new files are given whole. Changes to existing files are unified diffs against the state the previous task left: apply them from the repository root (`git apply`, e.g. `git apply <<'PATCH' … PATCH`) or edit by hand — every context line is exact.

**Facts the code relies on** (checked in the prototype against the installed packages and the test database):
- Procrastinate's periodic deferrer runs `croniter` on epoch seconds, so cron strings are evaluated in **UTC** (the containers run in UTC too). Local times (the 23:30 recap, the 03:00 retention) are therefore decided in Python from `LOOPI_TZ`. Periodic tasks receive the scheduled `timestamp` keyword.
- pgvector is 0.8.7 in `pgvector/pgvector:pg17` (compose pins 0.8.1); both accept `SET LOCAL hnsw.iterative_scan = relaxed_order`.
- The test database and the live one both classify characters with `en_US.utf8`, so pg_trgm splits Vietnamese words: `word_similarity('đi chợ', 'Mai mình đi chợ mua rau')` = 1.0, but `'di cho'` (no diacritics) only 0.29. With query parameters the `<%` operator is written `<%%`.
- `pg_advisory_xact_lock(bigint, integer)` does not exist (the two-key form is `(int, int)`): one key from `hashtextextended(text, 0)` is used.
- Ollama `POST /api/chat` with `"stream": true` answers one JSON object per line (`message.content` pieces, a last line with `"done": true`, or `{"error": …}`); `format`, `think: false`, `keep_alive` and `options.num_ctx` apply per request.
- Anthropic Messages API (raw HTTP, as the brief asks — no SDK): `POST /v1/messages` with `x-api-key` and `anthropic-version: 2023-06-01`; `system` is top-level; structured output is `output_config.format = {"type": "json_schema", "schema": …}` with no length/number limits and `additionalProperties: false` on every object; `output_config.effort`; Claude Haiku 5.5 and Sonnet 5.5 reject a non-default `temperature` (HTTP 400) and think by default (an answer can start with `thinking` blocks); streaming is SSE (`content_block_delta` with `text_delta`, `message_delta.stop_reason` `refusal`, `error` events such as `overloaded_error`); 429, 5xx and 529 are temporary.
- Starlette's `StreamingResponse` streams through `httpx.ASGITransport` (tests read it with `client.stream(...)`); `Jinja2Templates.get_template(name).render(...)` renders a fragment outside a response.
- Procrastinate 3.10 jobs carry a priority: `configure_task(name, priority=…)`; a worker takes the highest-priority `todo` job first (`ORDER BY priority DESC, id`), default 0.
- The host (read-only facts from the review, 2026-10-16): Intel i5-1135G7, 4 cores / 8 threads, 15.7 GB RAM with 7.5 GB available and 3.4 GB of swap in use; Ollama runners today: minicpm-v ≈ 2.93 GB, qwen3-embedding ≈ 1.27 GB; the worker ≈ 1.23 GB. qwen3:8b with an 8192-token context needs ≈ 6–6.5 GB, qwen3:4b ≈ 3.5 GB; on this CPU qwen3:8b reads ≈ 30–80 tokens/s and writes ≈ 5–7 tokens/s.

## Global Constraints

- The spec is binding (§9.2, §9.3, §9.4, §9.5, §10.1, §10.2, §10.4, §10.6, §12, §14 P2); D20 is the owner's decision of 2026-10-16: "brain giữ audio thô 7 ngày như video, chỉnh được trong Cài đặt".
- Local-first (§9.3, D3): the default configuration is 100 % local; images and audio never reach a cloud provider (the privacy gate is an allow-list, `LOCAL_PROVIDERS = {"ollama", "local"}`); Anthropic serves text tasks only (`chapter_summary`, `recap`, `chat`), only when `ANTHROPIC_API_KEY` is set **and** `providers.yaml` routes there; a backlog never escalates to the cloud.
- Models (§9.3): chapter_summary `ollama` `qwen3:4b`; recap and chat `ollama` `qwen3:8b`; embed `ollama` `qwen3-embedding:0.6b`; optional cloud models `claude-haiku-5-5`, `claude-sonnet-5-5`.
- Every derived row stores the model that wrote it as `"<provider>:<model>"` (§12 "Đổi model"): `chapter.model_version`, `day_recap.model_version`, `commitment.model_version`.
- Jobs are retried 3 times, then dead-lettered and shown with a retry button (§12) — with P2a's exceptions kept: a model-server outage (`ProviderUnavailable`) ends a chapter or recap run without spending a retry; runs that start again by themselves (the planners, `build_chapters`, `apply_retention`) have no retry button and only their newest failure is kept. `write_recap` has a retry button, and a failed recap is also shown on its Day page and on "Xử lý".
- Live work first (§14 P2: ≤ 15 min recording → caption/transcript): chapter and recap jobs run one at a time (job lock `llm`), with the lowest priority (`LLM_PRIORITY = -10`), on a worker with 3 slots (`LOOPI_WORKER_CONCURRENCY: "3"`); old days are caught up one per hour. Text-model requests wait up to `LOOPI_LLM_TIMEOUT_S` (900 s); Ollama keeps at most 2 models loaded and text models unload 1 minute after their last request.
- Model or cloud errors never affect ingest (§12): `PUT /v1/items` behaves exactly as before; the device contract `docs/protocol/v1.openapi.yaml` is not modified.
- Retention (§9.5, D20): raw video and raw audio are kept 7 days by default, editable on Cài đặt; **retention ships switched off** and runs only after the owner turns it on (Cài đặt shows what it would delete); only finished recordings lose their raw file and its view/playback copy; keyframes, thumbnails, transcripts, captions, moments, chapters, recaps and markers are never deleted by retention. Media is deleted only by retention (once switched on) and by "Quên khoảng thời gian" (after the owner's confirmation); every path is checked to lie inside the media root first.
- UI text Vietnamese; every web POST needs `HX-Request: true` (the chat POST included); text written by a model is always HTML-escaped.
- Python ≥ 3.13, uv-locked; no new dependency (`brain/pyproject.toml` and `brain/uv.lock` unchanged).
- TDD, coverage ≥ 80 % for `loopi_brain`, conventional commits ending with the session's attribution lines, never commit secrets (`deploy/.env`, API keys, PKI material), never prune or delete Docker resources you did not create (testcontainers' own containers are fine).
- The P1 + P2a suite stays green (340 passed, 1 skipped at `54e22f3`); the P2a tests this plan deliberately changes are listed in ruling R38. Run everything from `brain/`: `cd brain && uv run pytest`.
- **Tasks 1–13 never run `docker compose …`, `deploy/smoke.sh`, `os/build.sh` or any Docker command except testcontainers**, and never touch `/home/soul/loopi`, `/home/soul/loopi-p2` or `/home/soul/loopi-data`: the worktree's `deploy/` also resolves to compose project `deploy`, the owner's live stack. Only Task 14 — the controller, after the owner's yes — deploys.

## Review Focus

1. **Whisper's invented lines and recognition mistakes in the day's transcripts** (a lone "Thank you.", YouTube outros, half-heard words) reach chapter summaries, the recap and chat answers → every prompt that quotes speech says so and tells the model to ignore them; the recap reads the day's *longest* lines first; a commitment must cite a line of that day's material (an invented reference is dropped, never stored). Tests: Task 2 (`test_the_asr_caveat_names_the_known_hallucinations`), Task 4 (`test_a_settled_day_becomes_closed_chapters_with_vietnamese_summaries`), Task 5 (`test_a_day_gets_a_vietnamese_recap_and_its_sourced_commitments`, `test_a_long_day_fits_the_prompt_budget`), Task 8 (`test_a_question_gets_sources_then_a_streamed_cited_answer`).
2. **A backlog analysed hours or days late** (P1/P2a recordings, a device that was offline): a day's chapters stay open while analysis of that day is still to come, a moment that lands inside closed chapters reopens the day, an earlier day gets its recap once all its chapters are settled — and again once if they change afterwards — and days analysed before P2b get chapters by themselves, one per hour. Tests: Task 4 (`test_analysis_still_to_come_keeps_chapters_open`, `test_moments_appearing_inside_closed_chapters_reopen_the_day`, `test_the_planner_catches_up_one_unbuilt_day_per_run_newest_first`), Task 5 (`test_the_planner_writes_today_at_23_30_and_earlier_days_once_settled`, `test_an_earlier_day_is_written_again_once_after_its_chapters_change`, `test_today_is_written_again_at_23_30_after_an_earlier_recap`).
3. **The model server is down (or `qwen3:8b` is not pulled, or too slow) at 23:30, during the hourly build, or while someone asks a question** → no retry spent and nothing dead-lettered for an outage; chapters exist without summaries and get them at a later build, also on older settled days; no recap job is created while the recap model cannot run; a recap that keeps failing is shown on its Day page; search falls back to words and chat still shows the matching moments, then says it cannot answer. Tests: Task 4 (`test_a_model_server_outage_leaves_summaries_for_the_next_build`, `test_the_planner_comes_back_for_summaries_an_outage_or_a_failure_left_out`), Task 5 (`test_a_model_server_outage_writes_nothing_and_spends_no_retry`, `test_the_planner_skips_fresh_and_failed_recaps_and_waits_for_the_model`), Task 6 (`test_a_failed_recap_is_shown_on_its_day`), Task 7 (`test_without_the_embedding_model_words_still_find_moments`, `test_a_stopped_model_server_is_explained`), Task 8 (`test_no_time_filter_when_the_model_server_is_down`, `test_an_answer_that_breaks_off_is_reported_after_the_sources`).
4. **A question with no relevant memory, or an answer that cites sources that do not exist** → "Không tìm thấy trong ký ức." without asking the model to answer at all; citation numbers outside the sources are dropped; an answer citing nothing carries a visible warning; model text is escaped everywhere (answers, sources, recap markdown, chapter titles). Tests: Task 7 (`test_nothing_relevant_finds_nothing`), Task 8 (`test_nothing_relevant_says_not_found_and_asks_no_model_for_an_answer`, `test_the_chat_endpoint_streams_server_sent_events`, `test_the_chat_endpoint_flags_uncited_answers_and_not_found`), Task 6 (`test_model_text_cannot_inject_markup`, `test_a_day_shows_its_recap_commitments_and_chapters_in_order`).
5. **"Quên khoảng thời gian" on an awkward range or at an awkward moment** — across midnight, cutting a 60 s segment in half, a marker exactly on an edge, a stored path that leaves the media root, while a summary or the recap of that day is being written, or with a file that cannot be deleted right now → a recording that overlaps the range goes whole, one that only touches its start stays, both days' chapters and recaps are redone, a summary or recap in flight is not stored (it could quote forgotten text), nothing outside the media root is touched, a file left behind is retried at 03:00, and the audit row holds counts and paths but no content. Tests: Task 10 (`test_a_recording_that_straddles_the_edge_goes_whole_one_that_only_touches_it_stays`, `test_a_range_across_midnight_touches_both_days`, `test_files_outside_the_media_root_are_never_deleted`, `test_forgetting_removes_the_rows_files_and_derived_text_of_the_range`, `test_a_forget_waits_while_a_summary_or_recap_of_its_day_is_being_stored`, `test_a_file_that_cannot_be_deleted_now_stays_listed_for_a_retry`), Task 4 (`test_a_forget_while_the_model_writes_stores_no_summary`), Task 5 (`test_a_forget_while_the_recap_is_written_stores_nothing`), Task 11 (`test_files_a_forget_left_behind_are_deleted_at_three_even_with_retention_off`).

---

## File Structure

```
docs/superpowers/specs/2026-10-14-loopi-design.md   # Task 1: D19 text from feat/phase1, then D20 (§3, Sửa đổi, §9.2, §9.5)
brain/src/loopi_brain/
├── migrations/003_understanding.sql   # Task 1: chapter, day_recap, commitment, setting, forget_log, day_job_error,
│                                      #         item.raw_deleted_at, indexes
├── days.py                            # Task 1: local day bounds, local "now"
├── config.py                          # Task 2: text-model keep-alive/context; Task 7: search thresholds;
│                                      # Task 12: ANTHROPIC_API_KEY
├── llm.py                             # Task 2: ASR caveat, answer parsing, clipping, prompt budgets
├── providers/
│   ├── base.py                        # Task 8: stream_chat in the protocol; Task 12: LOCAL_PROVIDERS allow-list
│   ├── config.py                      # Task 2: TEXT_TASKS; Task 12: anthropic for text tasks only, IMAGE_TASKS
│   ├── ollama.py                      # Task 2: per-model keep_alive/num_ctx; Task 8: stream_chat (NDJSON)
│   ├── anthropic.py                   # Task 12: Messages API client (text only, SSE streaming)
│   └── router.py                      # Task 2: text options; Task 8: stream_chat with fallback;
│                                      # Task 12: privacy-aware task_ready, Anthropic when keyed
├── pipeline/
│   ├── segment.py                     # Task 3: pure chapter boundaries
│   ├── chapters.py                    # Task 4: build + summaries + hourly planner (catch-up, repair, priority)
│   ├── recap.py                       # Task 5: recap + commitments + planner (23:30, freshness, forget guard)
│   ├── retention.py                   # Task 11: 03:00 retention (off until the owner's yes), forget retries
│   ├── jobs.py, state.py, status.py   # Tasks 4, 5, 11, 13: names, priority, day lock, day errors, labels, card
│   └── asr.py                         # Task 11: docstring (D20)
├── search.py                          # Task 7: hybrid retrieval (pgvector + pg_trgm, RRF)
├── chat.py                            # Task 8: time filter, cited answer events
├── store.py                           # Task 9: editable settings (retention days, on/off)
├── reprocess.py                       # Task 9: reprocess one day
├── forget.py                          # Task 10: "Quên khoảng thời gian"
├── thumbs.py                          # Task 6: 320 px keyframe thumbnails
├── media.py                           # Task 6: media-root path guard; Task 10: remove_files
├── app.py                             # Tasks 6, 7, 9: new routers, the LLM http client
└── web/
    ├── routes.py                      # Task 1: day bounds from days.py; Task 6: hourly view moves to /hours
    ├── day_routes.py                  # Task 6: Day page, thumbnails, recap and commitment buttons
    ├── search_routes.py               # Task 7: /search; Task 8: /chat (SSE)
    ├── settings_routes.py             # Tasks 9–12: /settings, retention (+ preview), reprocess, forget, key
    ├── markdown.py                    # Task 6: safe markdown for recaps
    └── templates/                     # day, _commitment, search, _hit, _sources, settings, _reprocessed,
                                       # _forget_preview, _forgotten; base/timeline/pipeline edited
brain/tests/  conftest.py, helpers.py (shared fakes and inserts) and
              test_{days,llm,segment,chapters,recap,markdown,day_page,search,chat,settings_page,forget,retention,
              anthropic}.py (new); test_{db,config,providers,web,web_pipeline,audio_playback,deploy}.py (edited)
deploy/docker-compose.yml, providers.yaml, .env.example, README.md   # Task 13 (worker concurrency 3, 2 models)
docs/HANDOFF.md, docs/superpowers/ledgers/p2b-progress.md            # Task 14 (controller)
```

---
### Task 1: Spec D20, the understanding schema (migration 003) and local days

**Files:**
- Modify: `docs/superpowers/specs/2026-10-14-loopi-design.md` (D19 text from `feat/phase1`, then D20)
- Create: `brain/src/loopi_brain/migrations/003_understanding.sql`, `brain/src/loopi_brain/days.py`
- Modify: `brain/src/loopi_brain/web/routes.py` (day bounds come from `days.py`)
- Modify: `brain/tests/conftest.py` (truncation list), `brain/tests/test_db.py`
- Test: `brain/tests/test_days.py` (new), `brain/tests/test_db.py`

**Interfaces:**
- Consumes: P2a `db.migrate()` (applies `migrations/*.sql` in name order, then the Procrastinate schema), `web.routes.day_bounds_utc` (moves here).
- Produces:
  - `days.day_bounds_utc(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]` (UTC `[start, end)`), `days.local_day(ts: datetime, zone) -> date`, `days.local_now(zone, now: datetime | None = None) -> datetime`. `web.routes` imports `day_bounds_utc` from `days` (the name stays importable from `web.routes`).
  - Tables (columns exactly as in `003_understanding.sql` below): `item.raw_deleted_at`; `chapter` (`day`, `ts_start`, `ts_end`, `last_moment_at`, `moment_count`, `signature`, `closed`, `title`, `summary`, `summary_signature`, `model_version`, `summary_error`, `summary_error_at`, `created_at`, `updated_at`); `day_recap` (`day` PK, `markdown`, `model_version`, `generated_at`, `stale`); `commitment` (`day`, `text`, `source_moment_id` → `moment` ON DELETE SET NULL, `status` open|done|dismissed, `model_version`); `setting` (`key`, `value` jsonb); `forget_log` (`range_start`, `range_end`, `requested_at`, `counts` jsonb, `pending_files` jsonb list of media paths still to delete); `day_job_error` (`task`, `day`, `error`); indexes `item_retention`, `moment_created_at`, `chapter_day`, `commitment_day`, `commitment_source`.
  - `conftest.APP_TABLES` truncates the new tables between tests.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_days.py`:

```python
import datetime as dt
from zoneinfo import ZoneInfo

from loopi_brain.days import day_bounds_utc, local_day, local_now

SEOUL = ZoneInfo("Asia/Seoul")


def test_a_seoul_day_starts_at_15_utc_the_day_before():
    assert day_bounds_utc(dt.date(2026, 10, 15), SEOUL) == (
        dt.datetime(2026, 10, 14, 15, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 10, 15, 15, 0, tzinfo=dt.UTC),
    )


def test_a_dst_day_is_25_hours_long():
    start, end = day_bounds_utc(dt.date(2026, 10, 25), ZoneInfo("Europe/Berlin"))  # clocks go back
    assert end - start == dt.timedelta(hours=25)


def test_local_day_and_now_follow_the_zone():
    late_utc = dt.datetime(2026, 10, 14, 23, 30, tzinfo=dt.UTC)  # 08:30 on Oct 15 in Seoul
    assert local_day(late_utc, SEOUL) == dt.date(2026, 10, 15)
    assert local_now(SEOUL, late_utc).hour == 8
    assert local_now(SEOUL).tzinfo == SEOUL
```

Apply to `brain/tests/test_db.py` (the P2a migration lists gain `003_understanding.sql`; a new test pins the commitment and chapter rules):

```diff
diff --git a/brain/tests/test_db.py b/brain/tests/test_db.py
--- a/brain/tests/test_db.py
+++ b/brain/tests/test_db.py
@@ -11,6 +11,7 @@ from loopi_brain.db import PROCRASTINATE_SCHEMA, migrate
 EXPECTED_TABLES = {
     "device", "pair_token", "item", "device_command", "schema_migration",
     "keyframe", "transcript_segment", "moment",
+    "chapter", "day_recap", "commitment", "setting", "forget_log", "day_job_error",
     "procrastinate_jobs", "procrastinate_events", "procrastinate_workers", "procrastinate_periodic_defers",
 }
 
@@ -24,7 +25,7 @@ def test_migrate_is_idempotent_and_creates_schema(database_url):
         ).fetchall()
         names = {r[0] for r in conn.execute("SELECT name FROM schema_migration")}
     assert EXPECTED_TABLES <= {r[0] for r in rows}
-    assert {"001_init.sql", "002_pipeline.sql", PROCRASTINATE_SCHEMA} <= names
+    assert {"001_init.sql", "002_pipeline.sql", "003_understanding.sql", PROCRASTINATE_SCHEMA} <= names
 
 
 def test_recorded_procrastinate_schema_matches_installed_version():
@@ -59,7 +60,7 @@ def test_p1_rows_become_pending_after_upgrade(database_url):
                 "INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes, raw_path, status)"
                 " VALUES ('I1', 'D1', 'audio', now(), now(), 'x', 1, 'raw/x.opus', 'ready')"
             )
-        assert migrate(url) == ["002_pipeline.sql", PROCRASTINATE_SCHEMA]
+        assert migrate(url) == ["002_pipeline.sql", "003_understanding.sql", PROCRASTINATE_SCHEMA]
         with psycopg.connect(url) as conn:
             state = conn.execute("SELECT analysis_state, analysis_job_id FROM item WHERE id = 'I1'").fetchone()
         assert state == ("pending", None)
@@ -82,7 +83,9 @@ def test_concurrent_migrations_apply_each_step_once(database_url):
         for t in threads:
             t.join()
     assert errors == []
-    assert sorted(results, key=len) == [[], ["001_init.sql", "002_pipeline.sql", PROCRASTINATE_SCHEMA]]
+    assert sorted(results, key=len) == [
+        [], ["001_init.sql", "002_pipeline.sql", "003_understanding.sql", PROCRASTINATE_SCHEMA]
+    ]
 
 
 def test_cascading_foreign_keys_are_indexed(database_url):
@@ -96,7 +99,8 @@ def test_cascading_foreign_keys_are_indexed(database_url):
             )
         }
     assert {("keyframe", "item_id"), ("transcript_segment", "item_id"), ("moment", "item_id"),
-            ("moment", "keyframe_id"), ("moment", "transcript_segment_id")} <= indexed
+            ("moment", "keyframe_id"), ("moment", "transcript_segment_id"),
+            ("commitment", "source_moment_id")} <= indexed
 
 
 async def test_pool_connections_read_and_write_vectors(pool, pki_settings):
@@ -110,3 +114,33 @@ async def test_pool_connections_read_and_write_vectors(pool, pki_settings):
         )
         row = await (await conn.execute("SELECT embedding FROM moment WHERE id = 'M1'")).fetchone()
     assert row["embedding"].to_list()[:2] == [1.0, 0.0]
+
+
+async def test_understanding_tables_keep_their_rules(pool, pki_settings):
+    dev = await paired_device(pool, pki_settings)
+    async with pool.connection() as conn:
+        await conn.execute(
+            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, model_version)"
+            " VALUES ('M1', %s, now(), now(), 'speech', 'mai đi chợ', 'test')",
+            (dev["device_id"],),
+        )
+        await conn.execute(
+            "INSERT INTO commitment (id, day, text, source_moment_id, model_version)"
+            " VALUES ('C1', '2026-10-14', 'Đi chợ', 'M1', 'test')"
+        )
+        await conn.execute("DELETE FROM moment WHERE id = 'M1'")  # a reprocessed transcript
+        row = await (await conn.execute("SELECT source_moment_id, status FROM commitment")).fetchone()
+        assert row == {"source_moment_id": None, "status": "open"}  # the user's list keeps the item
+        with pytest.raises(psycopg.errors.CheckViolation):
+            await conn.execute("UPDATE commitment SET status = 'later'")
+    async with pool.connection() as conn:
+        with pytest.raises(psycopg.errors.CheckViolation):
+            await conn.execute(
+                "INSERT INTO chapter (id, day, ts_start, ts_end, last_moment_at, moment_count, signature)"
+                " VALUES ('CH1', '2026-10-14', now(), now() - interval '1 minute', now(), 1, 's')"
+            )
+    async with pool.connection() as conn:
+        with pytest.raises(psycopg.errors.CheckViolation):
+            await conn.execute(
+                "INSERT INTO forget_log (id, range_start, range_end, counts) VALUES ('F1', now(), now(), '{}')"
+            )
```

Apply to `brain/tests/conftest.py`:

```diff
diff --git a/brain/tests/conftest.py b/brain/tests/conftest.py
--- a/brain/tests/conftest.py
+++ b/brain/tests/conftest.py
@@ -8,7 +8,8 @@ from loopi_brain.db import make_pool, migrate
 
 PG_IMAGE = "pgvector/pgvector:pg17"
 APP_TABLES = (
-    "moment, transcript_segment, keyframe, device_command, item, pair_token, device,"
+    "commitment, chapter, day_recap, setting, forget_log, day_job_error,"
+    " moment, transcript_segment, keyframe, device_command, item, pair_token, device,"
     " procrastinate_events, procrastinate_periodic_defers, procrastinate_jobs, procrastinate_workers"
 )
 
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_days.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'loopi_brain.days'`.

Run: `cd brain && uv run pytest tests/test_db.py -q`
Expected: FAIL — `4 failed, 2 passed, 2 errors`: the expected tables and `003_understanding.sql` are missing, and the truncation errors with `psycopg.errors.UndefinedTable: relation "commitment" does not exist`.

- [ ] **Step 3: Add D20 to the spec (on top of D19)**

`feat/p2b` was branched before the D19 spec text landed (it is on `feat/phase1`), although the brain code already cites D19. Take the spec from `feat/phase1` (its only difference is D19), then add D20 in the spec's style (Vietnamese):

```bash
git show feat/phase1:docs/superpowers/specs/2026-10-14-loopi-design.md > docs/superpowers/specs/2026-10-14-loopi-design.md
python3 - <<'PY'
from pathlib import Path
p = Path("docs/superpowers/specs/2026-10-14-loopi-design.md")
s = p.read_text()
old_rev = "brain chuẩn hoá mức âm thanh trước ASR và khi phát lại (D19)\n"
assert s.count(old_rev) == 1
s = s.replace(old_rev, "brain chuẩn hoá mức âm thanh trước ASR và khi phát lại (D19); 2026-10-16: brain giữ audio thô 7 ngày như video thay vì xoá ngay sau khi có transcript, số ngày chỉnh được trong trang Cài đặt (D20)\n")
d19_end = "File thô giữ nguyên như lúc thu |\n"
assert s.count(d19_end) == 1
s = s.replace(d19_end, d19_end + "| D20 | Lưu audio thô | Brain giữ audio thô 7 ngày như video, không xoá ngay sau khi có transcript: transcript chưa đủ tin cậy (Whisper có thể bịa câu trên tiếng ồn hoặc bỏ sót tiếng nói nhỏ) và người dùng cần nghe lại bản đã chuẩn hoá mức âm thanh (D19). Số ngày giữ video thô và audio thô chỉnh được trong trang Cài đặt (mặc định 7 và 7). Job lưu trữ lúc 03:00 chỉ xoá file thô (kèm bản xem/nghe đã chuyển đổi) của những đoạn đã phân tích xong; keyframe, transcript, caption, chương và recap vẫn giữ vĩnh viễn |\n")
old_step = "4. Xoá audio thô sau khi có transcript (mặc định; chỉnh được).\n"
assert s.count(old_step) == 1
s = s.replace(old_step, "4. Giữ audio thô 7 ngày như video, sau đó job lưu trữ mới xoá (D20; chỉnh được trong trang Cài đặt).\n")
old_ret = "- Audio thô xoá ngay sau khi có transcript.\n"
assert s.count(old_ret) == 1
s = s.replace(old_ret, "- Audio thô giữ 7 ngày như video (D20). Số ngày giữ của cả hai chỉnh được trong trang Cài đặt; file thô chỉ bị xoá khi đoạn đó đã phân tích xong.\n")
p.write_text(s)
PY
git diff --stat feat/phase1 -- docs/superpowers/specs/2026-10-14-loopi-design.md
```

Expected: `1 file changed, 4 insertions(+), 3 deletions(-)` (the D20 row, the "Sửa đổi" line, audio step 4 and §9.5).

- [ ] **Step 4: Write the migration**

Create `brain/src/loopi_brain/migrations/003_understanding.sql`:

```sql
-- P2b: chapters, day recaps, commitments, editable settings, the "forget a time range" audit,
-- errors of the per-day jobs, and the retention mark on items (spec §9.4, §9.5, §10, D20).

-- Retention deleted the raw file (and its playback copy); the row, keyframes and transcripts stay.
ALTER TABLE item ADD COLUMN raw_deleted_at TIMESTAMPTZ;
CREATE INDEX item_retention ON item (ended_at) WHERE raw_deleted_at IS NULL AND kind IN ('video', 'audio');
-- The hourly chapter planner looks for days whose moments were written recently.
CREATE INDEX moment_created_at ON moment (created_at);

-- One chapter of a local day (all devices). Members are the day's moments whose ts_start lies in
-- [ts_start, last_moment_at]; `signature` hashes their ids, so a summary is rewritten only when they change.
-- Closed chapters are settled and never recomputed until the day is reopened (forget, reprocess).
CREATE TABLE chapter (
    id                 TEXT PRIMARY KEY,
    day                DATE NOT NULL,
    ts_start           TIMESTAMPTZ NOT NULL,
    ts_end             TIMESTAMPTZ NOT NULL,
    last_moment_at     TIMESTAMPTZ NOT NULL,
    moment_count       INT NOT NULL CHECK (moment_count > 0),
    signature          TEXT NOT NULL,
    closed             BOOLEAN NOT NULL DEFAULT FALSE,
    title              TEXT,
    summary            TEXT,
    summary_signature  TEXT,
    model_version      TEXT,
    summary_error      TEXT,
    summary_error_at   TIMESTAMPTZ,            -- the planner leaves a failing chapter alone for a while
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chapter_time_order CHECK (ts_end >= ts_start AND last_moment_at >= ts_start)
);
CREATE INDEX chapter_day ON chapter (day, ts_start);

-- A day with no data never gets a row. `stale`: reprocessing changed the day; the planner writes it again.
CREATE TABLE day_recap (
    day            DATE PRIMARY KEY,
    markdown       TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    generated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    stale          BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE commitment (
    id                TEXT PRIMARY KEY,
    day               DATE NOT NULL,
    text              TEXT NOT NULL,
    source_moment_id  TEXT REFERENCES moment(id) ON DELETE SET NULL,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dismissed')),
    model_version     TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX commitment_day ON commitment (day);
CREATE INDEX commitment_source ON commitment (source_moment_id);

-- Settings edited on the Cài đặt page (e.g. retention days); absent keys use the built-in defaults.
CREATE TABLE setting (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Audit of "Quên khoảng thời gian": what was asked and how much went (no content). `pending_files` lists the media
-- paths still to delete after the commit; the nightly retention run retries them until the list is empty.
CREATE TABLE forget_log (
    id             TEXT PRIMARY KEY,
    range_start    TIMESTAMPTZ NOT NULL,
    range_end      TIMESTAMPTZ NOT NULL,
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    counts         JSONB NOT NULL,
    pending_files  JSONB NOT NULL DEFAULT '[]'::jsonb,
    CONSTRAINT forget_range_order CHECK (range_end > range_start)
);

-- Last error of a per-day job (chapters, recap), shown next to its dead letter; cleared on success.
CREATE TABLE day_job_error (
    task   TEXT NOT NULL,
    day    DATE NOT NULL,
    error  TEXT NOT NULL,
    at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task, day)
);
```

It references no Procrastinate table (P2a deferred minor: `db.migrate()` applies the Procrastinate schema after every `*.sql`, so a migration that did would fail on a fresh database).

- [ ] **Step 5: Write the local-day helpers and use them in the web routes**

Create `brain/src/loopi_brain/days.py`:

```python
"""Local days. The household timeline is cut into days in one timezone (LOOPI_TZ, default Asia/Seoul),
across every device: chapters, recaps and commitments belong to a local day."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


def day_bounds_utc(day: dt.date, zone: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    """[start, end) of local ``day`` in UTC (23 or 25 hours long on a DST change)."""
    start = dt.datetime.combine(day, dt.time.min, tzinfo=zone)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=zone)
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


def local_day(ts: dt.datetime, zone: ZoneInfo) -> dt.date:
    """The local day an aware timestamp falls on."""
    return ts.astimezone(zone).date()


def local_now(zone: ZoneInfo, now: dt.datetime | None = None) -> dt.datetime:
    """``now`` (default: the current time) in ``zone``."""
    return (now or dt.datetime.now(dt.UTC)).astimezone(zone)
```

Apply to `brain/src/loopi_brain/web/routes.py`:

```diff
diff --git a/brain/src/loopi_brain/web/routes.py b/brain/src/loopi_brain/web/routes.py
--- a/brain/src/loopi_brain/web/routes.py
+++ b/brain/src/loopi_brain/web/routes.py
@@ -13,6 +13,7 @@ from fastapi.responses import FileResponse, HTMLResponse
 from fastapi.templating import Jinja2Templates
 
 from .. import audio_level, devices, media, pki
+from ..days import day_bounds_utc
 from ..pipeline import moments as moment_rules
 from ..pipeline import status
 
@@ -43,12 +44,6 @@ def require_htmx(hx_request: str | None = Header(default=None)) -> None:
         raise HTTPException(status_code=403, detail="missing HX-Request header")
 
 
-def day_bounds_utc(day: dt.date, zone: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
-    start = dt.datetime.combine(day, dt.time.min, tzinfo=zone)
-    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=zone)
-    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)
-
-
 def _hour() -> dict[str, list]:
     return {"keyframes": [], "speech": [], "markers": [], "segments": []}
 
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_days.py tests/test_db.py -q`
Expected: `11 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `344 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-10-14-loopi-design.md brain/src/loopi_brain/migrations/003_understanding.sql \
  brain/src/loopi_brain/days.py brain/src/loopi_brain/web/routes.py brain/tests/conftest.py brain/tests/test_db.py \
  brain/tests/test_days.py
git commit -F - <<'EOF'
feat(brain): understanding schema (chapters, recaps, commitments, settings) and spec D20

Raw audio is kept 7 days like video (D20, owner decision 2026-10-16); the spec takes the D19 text
from feat/phase1 first.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-16; they amend the steps above):**
- **R-I2 (part):** `days.py` also provides `hm(ts, zone) -> str` (local "HH:MM"), used by Tasks 4 and 5 instead of their private `_hm` copies.
- **R-M3 (part):** the D20 row and §9.5 text you add to the spec must also say that the nightly deletion runs only after the owner turns it on in Cài đặt (mặc định tắt).
- **R-M2:** commit trailers as in globals.md (name your own model); the plan's snippets show the controller's Opus line.

---

### Task 2: Text-model plumbing — short keep-alive and a context window for text models, shared answer helpers

**Files:**
- Modify: `brain/src/loopi_brain/config.py`, `brain/src/loopi_brain/providers/config.py`, `brain/src/loopi_brain/providers/ollama.py`, `brain/src/loopi_brain/providers/router.py`
- Create: `brain/src/loopi_brain/llm.py`
- Modify: `brain/tests/helpers.py` (`FakeOllama.reply`)
- Test: `brain/tests/test_llm.py` (new), `brain/tests/test_providers.py`, `brain/tests/test_config.py`

**Interfaces:**
- Consumes: P2a `OllamaProvider`, `build_router(config, settings, client)`, `ProvidersConfig`, `FakeOllama`.
- Produces:
  - `Settings.ollama_text_keep_alive: str = "1m"` (`LOOPI_OLLAMA_TEXT_KEEP_ALIVE`), `Settings.llm_num_ctx: int = 8192` (`LOOPI_LLM_NUM_CTX`, 2048..131072), `Settings.llm_timeout_s: float = 900.0` (`LOOPI_LLM_TIMEOUT_S`, 60..3600).
  - `providers.config.TEXT_TASKS = ("chapter_summary", "recap", "chat")`; `ProvidersConfig.ollama_text_models() -> frozenset[str]`.
  - `OllamaProvider(base_url, *, keep_alive="30m", client=None, text_models=frozenset(), text_keep_alive="1m", num_ctx=None, text_timeout=None)`: a chat to a text model sends `keep_alive = text_keep_alive` and `options.num_ctx = num_ctx` and waits up to `text_timeout` seconds (`httpx.Timeout(text_timeout, connect=5.0)`, from `_timeout_for(model)`); every other request is unchanged. `build_router` fills these from the config and settings.
  - `llm.ASR_CAVEAT`, `llm.PICTURE_NOTE` (prompt text); `llm.AnswerInvalid(Exception)`; `llm.parse_answer(text: str, model: type[M]) -> M`; `llm.clip(text, limit) -> str`; `llm.clipped(limit)` (a pydantic `str` type that is clipped, not rejected); `llm.spread(items, limit) -> list`; `llm.within(lines, budget) -> list[str]`.
  - `helpers.FakeOllama.reply`: optional callable `body -> str | dict` answering `/api/chat` (default: the caption).

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_llm.py`:

```python
import pytest
from pydantic import BaseModel

from loopi_brain.llm import ASR_CAVEAT, AnswerInvalid, clip, clipped, parse_answer, spread, within


class Answer(BaseModel):
    title: clipped(10)
    n: int


def test_answers_are_parsed_and_long_text_is_clipped_not_rejected():
    answer = parse_answer('{"title": "Một buổi sáng rất dài ở nhà", "n": 2, "extra": 1}', Answer)
    assert answer.title == "Một buổi…" and len(answer.title) <= 10 and answer.n == 2


@pytest.mark.parametrize("text", ["not json", "[]", '{"title": "x"}', '{"title": "x", "n": "many"}'])
def test_unusable_answers_are_invalid(text):
    with pytest.raises(AnswerInvalid):
        parse_answer(text, Answer)


def test_clip_flattens_whitespace():
    assert clip("  a\n b  ", 10) == "a b"
    assert clip("abcdefghij", 5) == "abcd…"


def test_spread_keeps_order_and_covers_the_whole_list():
    assert spread(list(range(10)), 3) == [0, 3, 6]
    assert spread([1, 2], 5) == [1, 2]
    assert spread([1, 2], 0) == []


def test_within_stops_at_the_budget():
    assert within(["aaaa", "bbbb", "cccc"], 10) == ["aaaa", "bbbb"]
    assert within(["a" * 20], 10) == []


def test_the_asr_caveat_names_the_known_hallucinations():
    assert "Thank you." in ASR_CAVEAT and "Hẹn gặp lại các bạn" in ASR_CAVEAT
```

Apply to `brain/tests/test_providers.py` and `brain/tests/test_config.py`:

```diff
diff --git a/brain/tests/test_providers.py b/brain/tests/test_providers.py
--- a/brain/tests/test_providers.py
+++ b/brain/tests/test_providers.py
@@ -289,3 +289,44 @@ async def test_router_rejects_wrong_embedding_dimensions():
         await router.embed("embed", ["a"])
     result = await _router(DEFAULT_CONFIG, ollama=StubProvider()).embed("embed", ["a", "b"])
     assert len(result.vectors) == 2 and result.model_version == "ollama:qwen3-embedding:0.6b"
+
+
+async def test_text_models_leave_memory_soon_and_get_a_context_window():
+    fake = FakeOllama()
+    provider = OllamaProvider("http://models.test:11434", keep_alive="30m", client=fake.client(),
+                              text_models=frozenset({"qwen3:8b"}), text_keep_alive="1m", num_ctx=8192,
+                              text_timeout=900.0)
+    await provider.chat("qwen3:8b", [{"role": "user", "content": "x"}], max_tokens=100)
+    await provider.chat("minicpm-v4.6:1b", [{"role": "user", "content": "x"}], images=[b"img"])
+    await provider.embed("qwen3-embedding:0.6b", ["a"])
+    (_, text), (_, vision), (_, embed) = fake.requests
+    assert text["keep_alive"] == "1m" and text["options"] == {"temperature": 0.0, "num_predict": 100, "num_ctx": 8192}
+    assert vision["keep_alive"] == "30m" and "num_ctx" not in vision["options"]
+    assert embed["keep_alive"] == "30m"
+    # A recap on a CPU takes minutes: only the text model waits that long.
+    (_, text_timeout), (_, vision_timeout), _ = fake.timeouts
+    assert text_timeout == {"connect": 5.0, "read": 900.0, "write": 900.0, "pool": 900.0}
+    assert vision_timeout["read"] == 5.0  # the client's own timeout
+
+
+async def test_build_router_gives_every_text_task_model_the_text_options():
+    assert DEFAULT_CONFIG.ollama_text_models() == {"qwen3:4b", "qwen3:8b"}
+    fake = FakeOllama()
+    settings = Settings(database_url="postgresql://unused", ollama_url="http://models.test:11434",
+                        ollama_text_keep_alive="2m", llm_num_ctx=4096, llm_timeout_s=600)
+    router = build_router(DEFAULT_CONFIG, settings, fake.client())
+    for task in ("chapter_summary", "recap", "chat"):
+        await router.chat(task, [{"role": "user", "content": "x"}])
+    await router.chat("caption", [{"role": "user", "content": "x"}], images=[b"img"])
+    assert [(b["model"], b["keep_alive"], b["options"].get("num_ctx")) for _, b in fake.requests] == [
+        ("qwen3:4b", "2m", 4096), ("qwen3:8b", "2m", 4096), ("qwen3:8b", "2m", 4096),
+        ("minicpm-v4.6:1b", "30m", None),
+    ]
+    assert [t["read"] for _, t in fake.timeouts] == [600.0, 600.0, 600.0, 5.0]
+
+
+async def test_fake_ollama_answers_chats_with_its_reply_hook():
+    fake = FakeOllama()
+    fake.reply = lambda body: {"echo": body["model"]}
+    provider = OllamaProvider("http://models.test:11434", client=fake.client())
+    assert json.loads(await provider.chat("qwen3:4b", [{"role": "user", "content": "x"}])) == {"echo": "qwen3:4b"}
```

```diff
diff --git a/brain/tests/test_config.py b/brain/tests/test_config.py
--- a/brain/tests/test_config.py
+++ b/brain/tests/test_config.py
@@ -40,3 +40,19 @@ def test_asr_gain_ceiling_from_env(monkeypatch):
         monkeypatch.setenv("LOOPI_ASR_MAX_GAIN_DB", bad)
         with pytest.raises(pydantic.ValidationError, match="asr_max_gain_db"):
             Settings()
+
+
+def test_text_model_defaults_and_limits(monkeypatch):
+    import pydantic
+    import pytest
+
+    s = Settings(database_url="postgresql://x")
+    assert (s.ollama_text_keep_alive, s.llm_num_ctx, s.llm_timeout_s) == ("1m", 8192, 900.0)
+    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
+    monkeypatch.setenv("LOOPI_LLM_NUM_CTX", "1024")
+    with pytest.raises(pydantic.ValidationError, match="llm_num_ctx"):
+        Settings()
+    monkeypatch.setenv("LOOPI_LLM_NUM_CTX", "8192")
+    monkeypatch.setenv("LOOPI_LLM_TIMEOUT_S", "10")
+    with pytest.raises(pydantic.ValidationError, match="llm_timeout_s"):
+        Settings()
```

Apply to `brain/tests/helpers.py` (`FakeOllama.reply`):

```diff
diff --git a/brain/tests/helpers.py b/brain/tests/helpers.py
--- a/brain/tests/helpers.py
+++ b/brain/tests/helpers.py
@@ -111,7 +111,9 @@ class FakeOllama:
     """In-process stand-in for the Ollama HTTP API, used as an ``httpx.MockTransport`` handler.
 
     Set ``down = True`` to simulate a stopped model server (every request raises ConnectError), or
-    ``error = 500`` to make chat and embed requests fail with that HTTP status.
+    ``error = 500`` to make chat and embed requests fail with that HTTP status. ``reply``, if set, is called
+    with each /api/chat request body and returns the answer (a str, or a dict sent as JSON); by default
+    every chat answers with ``caption``.
     """
 
     def __init__(self, caption: dict | str | None = None, models: list[str] | None = None, dims: int = 1024):
@@ -120,6 +122,7 @@ class FakeOllama:
         self.caption = DEFAULT_CAPTION if caption is None else caption
         self.models = list(DEFAULT_OLLAMA_MODELS if models is None else models)
         self.dims = dims
+        self.reply = None
         self.requests: list[tuple[str, dict]] = []
         self.timeouts: list[tuple[str, dict | None]] = []
 
@@ -140,7 +143,8 @@ class FakeOllama:
             if body["model"] not in self.models:
                 return httpx.Response(404, json={"error": f"model '{body['model']}' not found"})
         if path == "/api/chat":
-            content = self.caption if isinstance(self.caption, str) else json.dumps(self.caption)
+            answer = self.caption if self.reply is None else self.reply(body)
+            content = answer if isinstance(answer, str) else json.dumps(answer)
             return httpx.Response(
                 200, json={"model": body["model"], "message": {"role": "assistant", "content": content}, "done": True}
             )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_llm.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'loopi_brain.llm'`.

Run: `cd brain && uv run pytest tests/test_providers.py tests/test_config.py -q`
Expected: FAIL — `3 failed, 31 passed`: `TypeError: OllamaProvider.__init__() got an unexpected keyword argument 'text_models'`, `AttributeError: 'ProvidersConfig' object has no attribute 'ollama_text_models'`, `AttributeError: 'Settings' object has no attribute 'ollama_text_keep_alive'`.

- [ ] **Step 3: Add the settings and the text-task list**

Apply to `brain/src/loopi_brain/config.py` and `brain/src/loopi_brain/providers/config.py`:

```diff
diff --git a/brain/src/loopi_brain/config.py b/brain/src/loopi_brain/config.py
--- a/brain/src/loopi_brain/config.py
+++ b/brain/src/loopi_brain/config.py
@@ -32,6 +32,12 @@ class Settings(BaseSettings):
     asr_languages: str = "vi,ko,en"
     whisper_cpu_threads: int = 4
     asr_max_gain_db: float = Field(default=30.0, ge=0.0, le=60.0)  # level gain before VAD/Whisper (spec D19)
+    # Understanding (P2b): text models (chapters, recap, chat) leave memory a minute after answering (the host is
+    # short of memory: P2a deferred M14), get a context window large enough for their bounded prompts, and may
+    # take long on a CPU: a request waits up to llm_timeout_s seconds for the whole answer.
+    ollama_text_keep_alive: str = "1m"
+    llm_num_ctx: int = Field(default=8192, ge=2048, le=131072)
+    llm_timeout_s: float = Field(default=900.0, ge=60.0, le=3600.0)
 
     @property
     def zone(self) -> ZoneInfo:
```

```diff
diff --git a/brain/src/loopi_brain/providers/config.py b/brain/src/loopi_brain/providers/config.py
--- a/brain/src/loopi_brain/providers/config.py
+++ b/brain/src/loopi_brain/providers/config.py
@@ -9,6 +9,8 @@ from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
 # P2a ships the Ollama provider and the in-process ASR ("local"); cloud providers arrive in P2b.
 KNOWN_PROVIDERS = frozenset({"ollama", "local"})
 TASKS = ("caption", "asr", "embed", "chapter_summary", "recap", "chat")
+# Tasks that send only text (chapter summaries, the day recap, chat answers).
+TEXT_TASKS = ("chapter_summary", "recap", "chat")
 LOCAL_ASR_MODELS = frozenset({"faster-whisper/large-v3-turbo-int8"})
 
 
@@ -71,6 +73,12 @@ class ProvidersConfig(BaseModel):
                     models.append(step.model)
         return models
 
+    def ollama_text_models(self) -> frozenset[str]:
+        """Ollama models that serve a text task (chapters, recap, chat), fallbacks included."""
+        return frozenset(
+            step.model for task in TEXT_TASKS for step in self.tasks[task].chain() if step.provider == "ollama"
+        )
+
 
 DEFAULT_CONFIG = ProvidersConfig(
     tasks={
```

- [ ] **Step 4: Give text models their keep-alive and context window**

Apply to `brain/src/loopi_brain/providers/ollama.py` and `brain/src/loopi_brain/providers/router.py`:

```diff
diff --git a/brain/src/loopi_brain/providers/ollama.py b/brain/src/loopi_brain/providers/ollama.py
--- a/brain/src/loopi_brain/providers/ollama.py
+++ b/brain/src/loopi_brain/providers/ollama.py
@@ -29,10 +29,27 @@ def _tagged(model: str) -> str:
 class OllamaProvider:
     name = "ollama"
 
-    def __init__(self, base_url: str, *, keep_alive: str = "30m", client: httpx.AsyncClient | None = None):
+    def __init__(
+        self,
+        base_url: str,
+        *,
+        keep_alive: str = "30m",
+        client: httpx.AsyncClient | None = None,
+        text_models: frozenset[str] = frozenset(),
+        text_keep_alive: str = "1m",
+        num_ctx: int | None = None,
+        text_timeout: float | None = None,
+    ):
+        """``text_models`` (the big text models) stay loaded only ``text_keep_alive`` after a request, get a
+        ``num_ctx`` context window and wait up to ``text_timeout`` seconds for an answer; every other model stays
+        ``keep_alive``, with Ollama's default context and the client's timeout."""
         self._base = base_url.rstrip("/")
         self._keep_alive = keep_alive
         self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
+        self._text_models = text_models
+        self._text_keep_alive = text_keep_alive
+        self._num_ctx = num_ctx
+        self._text_timeout = text_timeout
 
     async def aclose(self) -> None:
         await self._client.aclose()
@@ -64,6 +81,12 @@ class OllamaProvider:
             raise ProviderError(f"ollama {path}: unexpected response {str(data)[:ERROR_TAIL]}")
         return data
 
+    def _timeout_for(self, model: str) -> httpx.Timeout | None:
+        """A text model writes a whole recap before Ollama replies (CPU: minutes); others keep the client's timeout."""
+        if model in self._text_models and self._text_timeout is not None:
+            return httpx.Timeout(self._text_timeout, connect=5.0)
+        return None
+
     async def chat(
         self,
         model: str,
@@ -77,20 +100,23 @@ class OllamaProvider:
         msgs = [dict(m) for m in messages]
         if images:
             msgs[-1]["images"] = [base64.b64encode(img).decode("ascii") for img in images]
+        text_model = model in self._text_models
         options: dict = {"temperature": temperature}
         if max_tokens is not None:
             options["num_predict"] = max_tokens  # a looping model stops instead of filling the context
+        if text_model and self._num_ctx is not None:
+            options["num_ctx"] = self._num_ctx  # Ollama silently drops the start of a longer prompt
         payload: dict = {
             "model": model,
             "messages": msgs,
             "stream": False,
             "think": False,
-            "keep_alive": self._keep_alive,
+            "keep_alive": self._text_keep_alive if text_model else self._keep_alive,
             "options": options,
         }
         if json_schema is not None:
             payload["format"] = json_schema
-        data = await self._request("POST", "/api/chat", payload)
+        data = await self._request("POST", "/api/chat", payload, timeout=self._timeout_for(model))
         content = (data.get("message") or {}).get("content")
         if not isinstance(content, str):
             raise ProviderError("ollama /api/chat: response has no message content")
```

```diff
diff --git a/brain/src/loopi_brain/providers/router.py b/brain/src/loopi_brain/providers/router.py
--- a/brain/src/loopi_brain/providers/router.py
+++ b/brain/src/loopi_brain/providers/router.py
@@ -105,5 +105,13 @@ class Router:
 
 
 def build_router(config: ProvidersConfig, settings: Settings, client: httpx.AsyncClient | None = None) -> Router:
-    ollama = OllamaProvider(settings.ollama_url, keep_alive=settings.ollama_keep_alive, client=client)
+    ollama = OllamaProvider(
+        settings.ollama_url,
+        keep_alive=settings.ollama_keep_alive,
+        client=client,
+        text_models=config.ollama_text_models(),
+        text_keep_alive=settings.ollama_text_keep_alive,
+        num_ctx=settings.llm_num_ctx,
+        text_timeout=settings.llm_timeout_s,
+    )
     return Router(config, {"ollama": ollama})
```

Why `num_ctx`: Ollama silently drops the beginning of a prompt longer than the context window — the system prompt, with its instructions about Vietnamese and invented transcript lines, would be the first thing lost. Every prompt in this plan is bounded well inside 8192 tokens (Tasks 4, 5, 8). Why the timeout: a non-streaming chat returns nothing until the whole answer is written; on this 4-core CPU a recap is several minutes (≈ 3–4 k prompt tokens at 30–80 tokens/s, up to 1000 output tokens at 5–7 tokens/s, plus loading 5 GB), beyond the 300 s client default. Why "1m": the host has ≈ 7.5 GB free; a big text model leaves memory soon after its job (chat follow-ups reload it from the page cache).

- [ ] **Step 5: Write the shared answer helpers**

Create `brain/src/loopi_brain/llm.py`:

```python
"""Shared pieces of the text tasks (chapter summaries, day recaps, chat): what the prompts say about the
material, answer parsing, and bounding the material so a prompt always fits the context window."""
from __future__ import annotations

import json
from typing import Annotated, TypeVar

from pydantic import BaseModel, BeforeValidator, ValidationError

T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)

# Every prompt that quotes transcripts says this (P2c filters hallucinations better; prompts must not trust them).
ASR_CAVEAT = (
    "Speech lines come from automatic speech recognition of a home microphone. They can contain recognition "
    "mistakes, filler words, and lines invented on silence or noise, such as a lone \"Thank you.\" or a "
    "YouTube-style outro (\"Hẹn gặp lại các bạn trong những video tiếp theo\", \"Subscribe to the channel\"). "
    "Ignore such lines and anything that makes no sense in context; never build on them."
)
PICTURE_NOTE = "Picture lines are short English descriptions of single frames of a still camera, written by a vision model."


class AnswerInvalid(Exception):
    """The model answered, but not with the JSON that was asked for (retried like any job error)."""


def parse_answer(text: str, model: type[M]) -> M:
    """The model's JSON answer as ``model``; AnswerInvalid if it is not JSON or misses a field."""
    try:
        return model.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:  # JSONDecodeError is a ValueError
        raise AnswerInvalid(f"{exc}; got {text[:200]!r}") from exc


def clip(text: str, limit: int) -> str:
    """``text`` on one line (whitespace collapsed), cut to ``limit`` characters; "…" marks a cut."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def clipped(limit: int):
    """A pydantic string type that is clipped to ``limit`` characters instead of rejected (truncate, don't reject)."""
    return Annotated[str, BeforeValidator(lambda v: clip(v, limit) if isinstance(v, str) else v)]


def spread(items: list[T], limit: int) -> list[T]:
    """At most ``limit`` items, evenly spread over ``items``, in their order."""
    if limit <= 0:
        return []
    if len(items) <= limit:
        return list(items)
    step = len(items) / limit
    return [items[int(k * step)] for k in range(limit)]


def within(lines: list[str], budget: int) -> list[str]:
    """The leading ``lines`` whose total length (with newlines) stays within ``budget`` characters."""
    kept: list[str] = []
    used = 0
    for line in lines:
        used += len(line) + 1
        if used > budget:
            break
        kept.append(line)
    return kept
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_llm.py tests/test_providers.py tests/test_config.py -q`
Expected: `43 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `357 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/config.py brain/src/loopi_brain/llm.py brain/src/loopi_brain/providers/config.py \
  brain/src/loopi_brain/providers/ollama.py brain/src/loopi_brain/providers/router.py brain/tests/helpers.py \
  brain/tests/test_config.py brain/tests/test_llm.py brain/tests/test_providers.py
git commit -F - <<'EOF'
feat(brain): text models unload after a minute, get an 8192-token context and a 15-minute timeout

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-16; they amend the steps above):**
- **R-I2 (part):** `llm.py` also provides `marker_line(at: str, note: str | None) -> str` building exactly the line `"<time> the user pressed the mark button[: note]"` (the string Task 5's test asserts), used by Tasks 4 and 5 instead of building it twice.

---

### Task 3: Chapter boundaries (pure segmentation)

**Files:**
- Create: `brain/src/loopi_brain/pipeline/segment.py`
- Test: `brain/tests/test_segment.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (NumPy only).
- Produces (used by Task 4):
  - `ChapterParams(gap_s=1200, min_s=600, max_s=10800, visual_drift=0.35, speech_drift=0.45, window=4, settle_s=1800)` (frozen dataclass).
  - `Point(id: str, kind: str, start: datetime, end: datetime, vector: np.ndarray | None = None)` — `kind` is `visual | speech | marker`; `vector` is unit length or None (not embedded yet; markers never drift).
  - `unit(values) -> np.ndarray | None`; `drift(vector, window: list[np.ndarray]) -> float` (cosine distance to the window's mean direction).
  - `boundaries(points, params=ChapterParams()) -> list[int]` (indexes where a chapter starts; `points` sorted by start, then id); `segment(points, params) -> list[list[Point]]`.
  - `closed_count(segments, horizon: datetime, day_end: datetime) -> int` — how many leading segments are settled.
  - `signature(points) -> str` (32 hex characters, identifies a chapter's members).

The rules, in order, for each moment after the first of a chapter: a gap of at least 20 minutes since the latest end so far starts a new chapter; so does a chapter reaching 3 hours; otherwise, once the chapter is at least 10 minutes old, a caption whose embedding drifts more than 0.35 (cosine distance) from the mean of the chapter's last 4 captions — or a speech line drifting more than 0.45 from the last 4 speech lines — starts a new chapter **if the next moment of the same kind drifts too** (one odd frame or one off-topic sentence never splits). Markers and moments without an embedding follow time only. A segment is settled when the next one starts at or before the horizon (the last one: when the whole day is).

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_segment.py` (synthetic embeddings: unit vectors on one axis mean "the same subject", on two axes "unrelated"):

```python
import datetime as dt

import numpy as np

from loopi_brain.pipeline.segment import (
    ChapterParams,
    Point,
    boundaries,
    closed_count,
    drift,
    segment,
    signature,
    unit,
)

T = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
DIMS = 16


def vec(axis: int, wobble: float = 0.0) -> np.ndarray:
    """A unit vector along ``axis``, nudged a little towards the last axis (same subject, another frame)."""
    v = np.zeros(DIMS)
    v[axis] = 1.0
    v[-1] = wobble
    return unit(v)


def points(*runs) -> list[Point]:
    """``runs`` of (kind, axis or None, count, minutes between points, minutes of gap before the run)."""
    out: list[Point] = []
    t = T
    for kind, axis, count, every, gap in runs:
        t += dt.timedelta(minutes=gap)
        for n in range(count):
            vector = None if axis is None else vec(axis, wobble=0.05 * (n % 3))
            out.append(Point(f"m{len(out):04d}", kind, t, t + dt.timedelta(seconds=5), vector))
            t += dt.timedelta(minutes=every)
    return out


def test_drift_is_zero_for_the_same_direction_and_one_for_unrelated_ones():
    assert drift(vec(0), [vec(0), vec(0)]) < 1e-9
    assert abs(drift(vec(1), [vec(0)]) - 1.0) < 1e-9
    assert unit([0.0, 0.0]) is None


def test_a_long_gap_starts_a_new_chapter():
    day = points(("visual", 0, 15, 1, 0), ("visual", 0, 10, 1, 25))
    assert boundaries(day) == [0, 15]


def test_a_change_of_scene_splits_the_chapter_where_it_happens():
    day = points(("visual", 0, 15, 1, 0), ("visual", 1, 15, 1, 0))
    chapters = segment(day)
    assert [len(c) for c in chapters] == [15, 15]
    assert chapters[1][0].start == T + dt.timedelta(minutes=15)


def test_one_odd_frame_does_not_split_a_chapter():
    # Someone crosses the room once: the next caption is like the earlier ones again.
    day = points(("visual", 0, 15, 1, 0), ("visual", 1, 1, 1, 0), ("visual", 0, 15, 1, 0))
    assert boundaries(day) == [0]


def test_a_change_soon_after_a_chapter_starts_joins_it():
    # Five minutes of one scene are too short a chapter: the minimum length wins over the drift.
    day = points(("visual", 0, 5, 1, 0), ("visual", 1, 20, 1, 0))
    assert boundaries(day) == [0]


def test_a_change_of_topic_in_speech_splits_the_chapter():
    day = points(("speech", 2, 12, 1, 0), ("speech", 3, 12, 1, 0))
    assert boundaries(day) == [0, 12]


def test_pictures_and_speech_drift_separately():
    # Captions keep the same scene while two people talk on and on about the same thing: one chapter.
    day = sorted(points(("visual", 0, 30, 1, 0)) + [
        Point(f"s{n:03d}", "speech", T + dt.timedelta(minutes=n, seconds=30),
              T + dt.timedelta(minutes=n, seconds=40), vec(5)) for n in range(30)
    ], key=lambda p: (p.start, p.id))
    assert boundaries(day) == [0]


def test_a_long_activity_is_cut_at_the_maximum_length():
    day = points(("visual", 0, 85, 5, 0))  # one scene every 5 minutes for 7 hours
    chapters = segment(day)
    assert [len(c) for c in chapters] == [36, 36, 13]
    assert all(c[-1].start - c[0].start < dt.timedelta(hours=3) for c in chapters)


def test_markers_and_moments_without_embeddings_follow_time_only():
    day = points(("visual", None, 15, 1, 0), ("marker", None, 1, 1, 0), ("speech", None, 15, 1, 0))
    assert boundaries(day) == [0]
    assert boundaries(points(("marker", None, 1, 1, 0), ("marker", None, 1, 1, 30))) == [0, 1]


def test_the_open_end_of_the_day_waits_for_confirmation():
    # The newest caption differs, but nothing after it yet: no split until the next one shows the change.
    day = points(("visual", 0, 15, 1, 0), ("visual", 1, 1, 1, 0))
    assert boundaries(day) == [0]
    later = Point("z", "visual", T + dt.timedelta(minutes=17), T + dt.timedelta(minutes=17, seconds=5), vec(1))
    assert boundaries(day + [later]) == [0, 15]


def test_no_moments_no_chapters():
    assert boundaries([]) == [] and segment([]) == []


def test_closing_needs_the_next_chapter_settled_and_the_last_needs_the_day_over():
    day = points(("visual", 0, 15, 1, 0), ("visual", 1, 15, 1, 0), ("visual", 0, 10, 1, 30))
    chapters = segment(day)
    assert [c[0].start for c in chapters] == [T, T + dt.timedelta(minutes=15), T + dt.timedelta(minutes=60)]
    day_end = T + dt.timedelta(hours=15)
    assert closed_count(chapters, T + dt.timedelta(minutes=14), day_end) == 0
    assert closed_count(chapters, T + dt.timedelta(minutes=15), day_end) == 1
    assert closed_count(chapters, T + dt.timedelta(hours=2), day_end) == 2
    assert closed_count(chapters, day_end, day_end) == 3


def test_signature_names_the_members():
    day = points(("visual", 0, 3, 1, 0))
    assert signature(day) == signature(list(day)) and len(signature(day)) == 32
    assert signature(day[:2]) != signature(day)


def test_parameters_can_be_tuned():
    day = points(("visual", 0, 15, 1, 0), ("visual", 0, 10, 1, 25))
    assert boundaries(day, ChapterParams(gap_s=60 * 60)) == [0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_segment.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'loopi_brain.pipeline.segment'`.

- [ ] **Step 3: Write the segmentation**

Create `brain/src/loopi_brain/pipeline/segment.py`:

```python
"""Chapter boundaries of a local day (spec §10.1): a gap, a change of picture, or a change of topic in speech.

Pure and deterministic: the same moments always give the same chapters, so a chapter's summary is only
rewritten when its moments change. Unit-tested with synthetic embeddings.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass

import numpy as np

DRIFT_KINDS = ("visual", "speech")


@dataclass(frozen=True)
class ChapterParams:
    gap_s: float = 20 * 60  # no moment for this long (nobody home, paused, asleep): a new chapter
    min_s: float = 10 * 60  # a change of picture or topic splits only a chapter at least this long
    max_s: float = 3 * 3600  # no chapter runs longer than this
    visual_drift: float = 0.35  # cosine distance of a caption from the chapter's recent captions
    speech_drift: float = 0.45  # the same for speech: a change of topic
    window: int = 4  # how many recent captions (or speech lines) a new one is compared with
    settle_s: float = 30 * 60  # a chapter closes once everything up to its end is this old and analysed


@dataclass(frozen=True)
class Point:
    """One moment as the segmentation sees it."""

    id: str
    kind: str  # visual | speech | marker
    start: dt.datetime
    end: dt.datetime
    vector: np.ndarray | None = None  # unit-length embedding; None until embedded (markers never drift)


def unit(values) -> np.ndarray | None:
    """``values`` as a unit-length float vector; None for a zero vector."""
    vector = np.asarray(values, dtype=np.float64)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else None


def drift(vector: np.ndarray, window: list[np.ndarray]) -> float:
    """Cosine distance between ``vector`` and the mean direction of ``window`` (0 same, 1 unrelated)."""
    centre = np.mean(window, axis=0)
    norm = float(np.linalg.norm(centre))
    if norm == 0:
        return 1.0
    return float(1.0 - np.dot(vector, centre) / norm)


def _limit(kind: str, params: ChapterParams) -> float:
    return params.visual_drift if kind == "visual" else params.speech_drift


def _shifted(points: list[Point], i: int, windows: dict[str, list[np.ndarray]], params: ChapterParams) -> bool:
    """Point ``i`` differs from the chapter's recent points of its kind, and so does the next point of that
    kind: one odd frame (someone crossing the room) or one off-topic sentence does not split a chapter."""
    point = points[i]
    window = windows.get(point.kind)
    if point.vector is None or not window:
        return False
    limit = _limit(point.kind, params)
    if drift(point.vector, window) <= limit:
        return False
    for later in points[i + 1:]:
        if later.kind == point.kind and later.vector is not None:
            return drift(later.vector, window) > limit
    return False  # nothing to confirm it yet: decided when more moments arrive


def _remember(windows: dict[str, list[np.ndarray]], point: Point, size: int) -> None:
    if point.vector is not None and point.kind in windows:
        windows[point.kind] = (windows[point.kind] + [point.vector])[-size:]


def boundaries(points: list[Point], params: ChapterParams = ChapterParams()) -> list[int]:
    """Indexes of ``points`` (sorted by start, then id) where a chapter starts; [] for no points."""
    if not points:
        return []
    starts = [0]
    chapter_start, last_end = points[0].start, points[0].end
    windows: dict[str, list[np.ndarray]] = {kind: [] for kind in DRIFT_KINDS}
    _remember(windows, points[0], params.window)
    for i in range(1, len(points)):
        point = points[i]
        age = (point.start - chapter_start).total_seconds()
        gap = (point.start - last_end).total_seconds()
        if gap >= params.gap_s or age >= params.max_s or (age >= params.min_s and _shifted(points, i, windows, params)):
            starts.append(i)
            chapter_start = point.start
            windows = {kind: [] for kind in DRIFT_KINDS}
        last_end = max(last_end, point.end)
        _remember(windows, point, params.window)
    return starts


def segment(points: list[Point], params: ChapterParams = ChapterParams()) -> list[list[Point]]:
    """``points`` cut into chapters (each a non-empty list, in order)."""
    starts = boundaries(points, params)
    return [points[a:b] for a, b in zip(starts, [*starts[1:], len(points)], strict=False)]


def closed_count(segments: list[list[Point]], horizon: dt.datetime, day_end: dt.datetime) -> int:
    """How many leading segments are settled: everything up to the start of the next segment (the end of the
    day, for the last one) lies before ``horizon``, so data still to come can no longer change them."""
    count = 0
    for k in range(len(segments)):
        limit = segments[k + 1][0].start if k + 1 < len(segments) else day_end
        if limit > horizon:
            break
        count += 1
    return count


def signature(points: list[Point]) -> str:
    """Identifies a chapter's members: equal signatures mean the same moments."""
    return hashlib.sha256("\n".join(p.id for p in points).encode()).hexdigest()[:32]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_segment.py -q`
Expected: `14 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `371 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/pipeline/segment.py brain/tests/test_segment.py
git commit -F - <<'EOF'
feat(brain): chapter boundaries from gaps, picture drift and topic shift (spec 10.1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 4: Chapter builds, Vietnamese summaries and the hourly planner

**Files:**
- Create: `brain/src/loopi_brain/pipeline/chapters.py`
- Modify: `brain/src/loopi_brain/pipeline/jobs.py` (task names, the `llm` lock, `LLM_PRIORITY`, `enqueue(priority=…)`), `brain/src/loopi_brain/pipeline/state.py` (per-day job errors, the day lock, the forget counter), `brain/src/loopi_brain/pipeline/status.py` (labels, day errors on the dead-letter list)
- Modify: `brain/tests/helpers.py` (`insert_moment` gains an embedding, an item and a length; `insert_chapter`)
- Test: `brain/tests/test_chapters.py`

**Interfaces:**
- Consumes: Task 1 `day_bounds_utc`, `local_now`, tables `chapter`, `day_job_error`, `forget_log`; Task 2 `ASR_CAVEAT`, `PICTURE_NOTE`, `AnswerInvalid`, `parse_answer`, `clip`, `clipped`, `spread`, `within`, `FakeOllama.reply`; Task 3 `ChapterParams`, `Point`, `segment`, `closed_count`, `signature`, `unit`; P2a `jobs.enqueue`, `jobs.RETRY`, `PipelineContext`, `get_context`, `Router.chat`, `Router.health`, `Router.task_ready`, `ProviderUnavailable`/`ProviderError`.
- Produces:
  - `jobs.PLAN_CHAPTERS = "plan_chapters"` (periodic, cron `"5 * * * *"`, queue `light`), `jobs.BUILD_CHAPTERS = "build_chapters"` (queue `understand`, lock `jobs.LLM_LOCK = "llm"`, priority `jobs.LLM_PRIORITY = -10`, queueing lock `chapters:<YYYY-MM-DD>`, kwargs `{"day": "YYYY-MM-DD"}`); both in `jobs.AUTO_RERUN_TASKS`. `jobs.enqueue(conn, task, *, queue, queueing_lock, lock=None, priority=None, **kwargs)` (Task 5 uses it too).
  - `chapters.build_chapters(ctx, day: date, attempt=0, params=ChapterParams(), now=None) -> BuildResult(chapters, closed, summarised)`; raises `ChapterSummaryFailed` when a model answer was unusable (job retried); a model-server outage is not an error.
  - `chapters.rebuild_chapters(pool, day, zone, params, now) -> tuple[int, int]`, `chapters.summarise_chapters(ctx, day, attempt) -> tuple[int, list[str]]`, `chapters.chapter_lines(pool, first, last, zone) -> list[str]`, `chapters.summary_messages(lines, first, end, zone) -> list[dict]`.
  - `chapters.defer_build(pool, day, zone) -> int | None` (only for a day with moments or chapters; errors logged), `chapters.plan_chapters(pool, router, zone, now=None) -> list[date]` (Task 13's job wrapper passes `ctx.router`).
  - Constants `OPEN_MIN_MOMENTS = 3`, `TITLE_MAX = 80`, `SUMMARY_MAX = 600`, `SUMMARY_MAX_TOKENS = 400`, `RETRY_TEMPERATURE = 0.3`, `PROMPT_BUDGET = 7000`, `MAX_PICTURE_LINES = 40`, `MAX_SPEECH_LINES = 60`, `MAX_MARKER_LINES = 20`, `RECENT = 26 h`, `CATCHUP_PER_RUN = 1`, `ERROR_COOLDOWN = 6 h`, `SUMMARY_SCHEMA`.
  - `state.record_day_error(pool, task, day, exc)`, `state.clear_day_error(pool, task, day)`; `state.lock_day(conn, day)` (transaction advisory lock `day:<YYYY-MM-DD>`: chapter builds, stored summaries and recaps, and "Quên khoảng thời gian" (Task 10) take it), `state.forgotten(conn, start, end) -> int` (how many forget requests overlap the range: read before a model call, re-read under `lock_day` before storing its answer; a different number → store nothing).
  - `/pipeline` failed list: target = the job's `day` for per-day jobs, error = `day_job_error.error`; labels "Lên lịch chia chương", "Chia chương" (`status.CHAPTER_RERUN` = "tự chạy lại ở lần lên lịch chia chương (mỗi giờ; chương tóm tắt lỗi: sau 6 giờ)").
  - `helpers.insert_moment(pool, device_id, *, kind, ts, text, lang, axis=None, item_id=None, seconds=0)`: `axis` gives the moment the 1024-d unit vector on that axis; `helpers.insert_chapter(pool, day, ts_start, ts_end, *, title=None, summary=None, closed=True, moment_count=1) -> str` (Task 5 onwards use it).

How a build works (`rebuild_chapters`, one transaction under `lock_day`):
1. Closed chapters are kept as they are, unless the number of moments in their range changed (a late moment arrived, or one was deleted): then every chapter of the day is reopened.
2. The moments after the last closed chapter are segmented (Task 3).
3. The horizon is `now − 30 min`, or earlier: the start of the first analysis of that day still to come (pending item, uncaptioned keyframe, unembedded moment — dead-lettered work does not count). Segments settled by that horizon are written closed.
4. A segment keeps the row of an open chapter with the same signature (same moments: title and summary stay), else the row of an open chapter starting at the same moment (the old summary stays visible, now out of date), else gets a new row; open rows left over are deleted. A row is written only when something changed, so `updated_at` is the time of the chapter's last real change (Task 5's recap planner compares it with the recap).

Then `summarise_chapters` asks `chapter_summary` (`qwen3:4b`) for every chapter whose signature differs from its summary's — closed ones, and today's open one once it has 3 moments — with the chapter's lines (≤ 40 distinct captions, ≤ 60 speech lines, ≤ 20 markers, spread over the chapter, ≤ 7000 characters), JSON schema `{title, summary}`, `think: false`, ≤ 400 tokens; over-long answers are clipped. A failed answer stores `summary_error` and `summary_error_at`. Each answer is stored under `lock_day` only if no "Quên khoảng thời gian" overlapping the day arrived since the build started (else nothing more is stored; the forget asked for a new build).

The hourly planner asks for a build of: today, yesterday, every day with moments written in the last 26 hours, every day with open chapters; while `chapter_summary` can run, every day with a chapter still waiting for its summary (an outage or failure on an older settled day; a chapter whose summary failed less than 6 hours ago does not count); and the newest day that has moments but no chapter at all (days analysed before P2b), one per run. Builds have priority −10: on the worker's 3 slots (Task 13) they wait behind every live caption, transcription and embedding job.

- [ ] **Step 1: Write the failing tests**

Apply to `brain/tests/helpers.py`:

```diff
diff --git a/brain/tests/helpers.py b/brain/tests/helpers.py
--- a/brain/tests/helpers.py
+++ b/brain/tests/helpers.py
@@ -218,15 +218,29 @@ async def insert_moment(
     ts: dt.datetime | None = None,
     text: str = "x",
     lang: str | None = None,
+    axis: int | None = None,
+    item_id: str | None = None,
+    seconds: float = 0,
 ) -> str:
-    """Insert a moment row without an embedding (both timestamps = ``ts``, default now); return its id."""
+    """Insert a moment row (it starts at ``ts``, default now, and lasts ``seconds``); return its id.
+
+    ``axis`` gives it an embedding: the unit vector along that axis (moments on one axis are "the same
+    subject", on two axes unrelated). Without it the moment waits for its embedding."""
+    from pgvector import Vector
+
     moment_id = new_ulid()
     ts = ts or dt.datetime.now(dt.UTC)
+    vector = None
+    if axis is not None:
+        values = [0.0] * 1024
+        values[axis] = 1.0
+        vector = Vector(values)
     async with pool.connection() as conn:
         await conn.execute(
-            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, lang, model_version)"
-            " VALUES (%s, %s, %s, %s, %s, %s, %s, 'test')",
-            (moment_id, device_id, ts, ts, kind, text, lang),
+            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, lang, item_id, embedding, embed_model,"
+            " model_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'test')",
+            (moment_id, device_id, ts, ts + dt.timedelta(seconds=seconds), kind, text, lang, item_id, vector,
+             None if vector is None else "test"),
         )
     return moment_id
 
@@ -377,3 +391,26 @@ class FakeTranscriber:
         self.chunks.append(len(chunk))
         self.peaks.append(float(abs(chunk).max()) if len(chunk) else 0.0)
         return self.results[(len(self.chunks) - 1) % len(self.results)]
+
+
+async def insert_chapter(
+    pool,
+    day: dt.date,
+    ts_start: dt.datetime,
+    ts_end: dt.datetime,
+    *,
+    title: str | None = None,
+    summary: str | None = None,
+    closed: bool = True,
+    moment_count: int = 1,
+) -> str:
+    """Insert a chapter row directly (its moments are those starting in [ts_start, ts_end]); return its id."""
+    chapter_id = new_ulid()
+    async with pool.connection() as conn:
+        await conn.execute(
+            "INSERT INTO chapter (id, day, ts_start, ts_end, last_moment_at, moment_count, signature, closed,"
+            " title, summary, summary_signature, model_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
+            (chapter_id, day, ts_start, ts_end, ts_end, moment_count, chapter_id, closed, title, summary,
+             chapter_id if title else None, "test" if title else None),
+        )
+    return chapter_id
```

Create `brain/tests/test_chapters.py`:

```python
import datetime as dt
import json
import logging

import pytest

from helpers import (
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_chapter,
    insert_item,
    insert_moment,
    jobs_by_lock,
    make_ctx,
    paired_device,
    run_jobs,
)
from loopi_brain.llm import ASR_CAVEAT
from loopi_brain.pipeline import chapters, jobs, status

DAY = dt.date(2026, 10, 14)
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
NEXT_NOON = dt.datetime(2026, 10, 15, 3, 0, tzinfo=dt.UTC)  # the day is over and settled


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    fake = FakeOllama()
    count = iter(range(1, 1000))
    fake.reply = lambda body: {"title": f"Chương {next(count)}", "summary": "Buổi sáng ở nhà. Mọi người làm việc."}
    return fake


@pytest.fixture
def ctx(pool, pki_settings, ollama):
    return make_ctx(pool, pki_settings, ollama=ollama)


async def _run(pool, device_id, start, count, axis, kind="visual", every=60, text=None, lang=None):
    for n in range(count):
        await insert_moment(pool, device_id, kind=kind, ts=start + dt.timedelta(seconds=n * every), axis=axis,
                            text=text or f"{kind} {axis} {n}", lang=lang, seconds=5)


async def _two_scenes(pool, device_id):
    await _run(pool, device_id, NINE, 15, axis=0, text="A person reads at the desk")
    await _run(pool, device_id, NINE + dt.timedelta(minutes=15), 15, axis=1, text="Two people cook in the kitchen")
    await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=20), axis=7,
                        text="Mai mình đi chợ nhé", lang="vi", seconds=3)


def _chats(ollama) -> list[dict]:
    return [body for path, body in ollama.requests if path == "/api/chat"]


async def _chapters(pool) -> list[dict]:
    return await fetch_all(pool, "SELECT * FROM chapter ORDER BY ts_start")


async def test_a_settled_day_becomes_closed_chapters_with_vietnamese_summaries(pool, dev, ctx, ollama):
    await _two_scenes(pool, dev["device_id"])
    result = await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    assert result == chapters.BuildResult(chapters=2, closed=2, summarised=2)
    first, second = await _chapters(pool)
    assert (first["ts_start"], first["last_moment_at"], first["moment_count"]) == (
        NINE, NINE + dt.timedelta(minutes=14), 15)
    assert (second["ts_start"], second["moment_count"]) == (NINE + dt.timedelta(minutes=15), 16)
    assert first["closed"] and second["closed"]
    assert (first["title"], first["summary"]) == ("Chương 1", "Buổi sáng ở nhà. Mọi người làm việc.")
    assert first["model_version"] == "ollama:qwen3:4b" and first["summary_signature"] == first["signature"]
    body = _chats(ollama)[1]
    assert body["model"] == "qwen3:4b" and body["format"] == chapters.SUMMARY_SCHEMA and body["think"] is False
    system, user = body["messages"]
    assert ASR_CAVEAT in system["content"] and "Vietnamese" in system["content"]
    assert user["content"].startswith("Part of the day from 09:15 to 09:29:")
    assert "09:15 picture: Two people cook in the kitchen" in user["content"]
    assert "09:20 speech (vi): Mai mình đi chợ nhé" in user["content"]
    assert user["content"].count("picture:") == 1  # the same caption fifteen times is one line
    assert await fetch_all(pool, "SELECT * FROM day_job_error") == []


async def test_a_second_build_of_a_settled_day_calls_no_model(pool, dev, ctx, ollama):
    await _two_scenes(pool, dev["device_id"])
    await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    before = [(c["id"], c["updated_at"]) for c in await _chapters(pool)]
    result = await chapters.build_chapters(ctx, DAY, now=NEXT_NOON + dt.timedelta(hours=5))
    assert result == chapters.BuildResult(chapters=2, closed=2, summarised=0)
    # Nothing changed, so no row was touched (the recap planner compares updated_at with its recap).
    assert [(c["id"], c["updated_at"]) for c in await _chapters(pool)] == before and len(_chats(ollama)) == 2


async def test_an_unchanged_open_chapter_is_not_touched_by_the_next_build(pool, dev, ctx):
    await _run(pool, dev["device_id"], NINE, 5, axis=0)
    await chapters.build_chapters(ctx, DAY, now=NINE + dt.timedelta(minutes=10))
    [before] = await _chapters(pool)
    await chapters.build_chapters(ctx, DAY, now=NINE + dt.timedelta(minutes=20))
    [after] = await _chapters(pool)
    assert (after["id"], after["updated_at"], after["closed"]) == (before["id"], before["updated_at"], False)


async def test_today_keeps_its_tail_open_and_rewrites_only_what_changed(pool, dev, ctx, ollama):
    device_id = dev["device_id"]
    await _two_scenes(pool, device_id)
    result = await chapters.build_chapters(ctx, DAY, now=NINE + dt.timedelta(minutes=40))
    assert result == chapters.BuildResult(chapters=2, closed=0, summarised=2)  # nothing is 30 minutes old yet
    first_id, second_id = [c["id"] for c in await _chapters(pool)]
    await _run(pool, device_id, NINE + dt.timedelta(minutes=31), 9, axis=1)  # the kitchen scene goes on
    result = await chapters.build_chapters(ctx, DAY, now=NINE + dt.timedelta(minutes=80))
    assert result == chapters.BuildResult(chapters=2, closed=1, summarised=1)
    first, second = await _chapters(pool)
    assert (first["id"], first["closed"], first["title"]) == (first_id, True, "Chương 1")  # untouched
    assert (second["id"], second["closed"], second["moment_count"]) == (second_id, False, 25)
    assert second["title"] == "Chương 3" and second["summary_signature"] == second["signature"]
    assert len(_chats(ollama)) == 3


async def test_analysis_still_to_come_keeps_chapters_open(pool, dev, ctx):
    device_id = dev["device_id"]
    await _two_scenes(pool, device_id)
    item = await insert_item(pool, device_id, kind="audio", started_at=NINE + dt.timedelta(minutes=20))
    result = await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    assert (result.chapters, result.closed) == (2, 1)  # the second chapter waits for that recording
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (item,))
    assert (await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)).closed == 2


async def test_moments_appearing_inside_closed_chapters_reopen_the_day(pool, dev, ctx, ollama, caplog):
    caplog.set_level(logging.INFO, logger="loopi_brain.pipeline.chapters")
    device_id = dev["device_id"]
    await _two_scenes(pool, device_id)
    await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    # A backlog recording of 09:05 is analysed only now.
    await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=5), axis=9,
                        text="Con đi học về rồi", lang="vi")
    result = await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    assert result == chapters.BuildResult(chapters=2, closed=2, summarised=1)
    first, second = await _chapters(pool)
    assert first["moment_count"] == 16 and first["title"] == "Chương 3" and second["title"] == "Chương 2"
    assert "recomputing the day" in caplog.text


async def test_a_model_server_outage_leaves_summaries_for_the_next_build(pool, dev, ctx, ollama):
    await _two_scenes(pool, dev["device_id"])
    ollama.down = True
    result = await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    assert result == chapters.BuildResult(chapters=2, closed=2, summarised=0)  # no error: no retry is spent
    assert [c["title"] for c in await _chapters(pool)] == [None, None]
    ollama.down = False
    assert (await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)).summarised == 2


async def test_bad_answers_are_kept_on_the_chapter_and_fail_the_job(pool, dev, ctx, ollama):
    await _two_scenes(pool, dev["device_id"])
    good = ollama.reply
    ollama.reply = lambda body: "Sorry, I cannot help with that."
    with pytest.raises(chapters.ChapterSummaryFailed, match="2 chapter summaries failed; last: AnswerInvalid"):
        await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    assert all(c["summary_error"].startswith("AnswerInvalid") and c["summary_error_at"] for c in await _chapters(pool))
    error = await fetch_one(pool, "SELECT task, day, error FROM day_job_error")
    assert (error["task"], error["day"]) == ("build_chapters", DAY) and "2 chapter summaries" in error["error"]
    ollama.reply = good
    await chapters.build_chapters(ctx, DAY, attempt=1, now=NEXT_NOON)
    assert [(c["summary_error"], c["summary_error_at"]) for c in await _chapters(pool)] == [(None, None)] * 2
    assert await fetch_all(pool, "SELECT * FROM day_job_error") == []
    assert _chats(ollama)[-1]["options"]["temperature"] == chapters.RETRY_TEMPERATURE


async def test_overlong_answers_are_cut_not_rejected(pool, dev, ctx, ollama):
    await _run(pool, dev["device_id"], NINE, 5, axis=0)
    ollama.reply = lambda body: {"title": "Tiêu đề " * 30, "summary": "Câu. " * 300}
    await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    [row] = await _chapters(pool)
    assert len(row["title"]) <= chapters.TITLE_MAX and len(row["summary"]) <= chapters.SUMMARY_MAX
    assert row["summary"].endswith("…")


async def test_the_open_chapter_waits_for_three_moments(pool, dev, ctx, ollama):
    await _run(pool, dev["device_id"], NINE, 2, axis=0)
    result = await chapters.build_chapters(ctx, DAY, now=NINE + dt.timedelta(minutes=5))
    assert result == chapters.BuildResult(chapters=1, closed=0, summarised=0) and _chats(ollama) == []


async def test_a_long_chapter_fits_the_prompt_budget(pool, dev, ctx, ollama):
    await _run(pool, dev["device_id"], NINE, 300, axis=4, kind="speech", every=30, lang="ko",
               text="오늘은 정말 긴 하루였어요 " * 20)
    await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    user = _chats(ollama)[0]["messages"][1]["content"]
    lines = user.split("\n")[1:]
    assert 0 < len(lines) <= chapters.MAX_SPEECH_LINES
    assert sum(len(line) + 1 for line in lines) <= chapters.PROMPT_BUDGET
    assert lines[0].startswith("09:00 speech (ko): 오늘은")


async def test_a_day_whose_moments_are_gone_loses_its_chapters(pool, dev, ctx):
    await _two_scenes(pool, dev["device_id"])
    await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM moment")
    assert await chapters.build_chapters(ctx, DAY, now=NEXT_NOON) == chapters.BuildResult(0, 0, 0)
    assert await _chapters(pool) == []


async def test_the_planner_asks_once_for_each_day_with_data(pool, dev, ctx, pki_settings, jobs_open):
    device_id = dev["device_id"]
    await insert_moment(pool, device_id, ts=NINE)  # today
    await insert_moment(pool, device_id, ts=NINE - dt.timedelta(days=5))  # a backlog day, analysed just now
    now = NINE + dt.timedelta(hours=3)
    zone = pki_settings.zone
    assert await chapters.plan_chapters(pool, ctx.router, zone, now) == [DAY - dt.timedelta(days=5), DAY]
    assert await chapters.plan_chapters(pool, ctx.router, zone, now) == [DAY - dt.timedelta(days=5), DAY]
    locks = await jobs_by_lock(pool)
    for day in ("2026-10-09", "2026-10-14"):
        [job] = locks[f"chapters:{day}"]
        assert (job["task_name"], job["queue_name"], job["lock"], job["args"]) == (
            "build_chapters", "understand", "llm", {"day": day})
        assert job["priority"] == jobs.LLM_PRIORITY  # live captions and transcripts go first
    assert "chapters:2026-10-13" not in locks  # yesterday had no data


async def _old(pool, device_id, days_ago: int) -> str:
    """A moment of ``days_ago`` days before DAY that was analysed (written) that long ago."""
    moment = await insert_moment(pool, device_id, ts=NINE - dt.timedelta(days=days_ago))
    async with pool.connection() as conn:
        await conn.execute("UPDATE moment SET created_at = now() - make_interval(days => %s) WHERE id = %s",
                           (days_ago, moment))
    return moment


async def test_the_planner_catches_up_one_unbuilt_day_per_run_newest_first(pool, dev, ctx, pki_settings, jobs_open):
    # Days P2a analysed before P2b existed have moments and no chapter.
    for days_ago in (30, 31):
        await _old(pool, dev["device_id"], days_ago)
    now = NINE + dt.timedelta(days=60)
    zone = pki_settings.zone
    assert await chapters.plan_chapters(pool, ctx.router, zone, now) == [DAY - dt.timedelta(days=30)]
    await insert_chapter(pool, DAY - dt.timedelta(days=30), NINE - dt.timedelta(days=30), NINE - dt.timedelta(days=30),
                         title="x", summary="y")
    assert await chapters.plan_chapters(pool, ctx.router, zone, now) == [DAY - dt.timedelta(days=31)]


async def test_the_planner_comes_back_for_summaries_an_outage_or_a_failure_left_out(pool, dev, ctx, ollama,
                                                                                    pki_settings, jobs_open):
    old_day = DAY - dt.timedelta(days=10)
    await _old(pool, dev["device_id"], 10)
    chapter = await insert_chapter(pool, old_day, NINE - dt.timedelta(days=10), NINE - dt.timedelta(days=10))
    now, zone = NINE + dt.timedelta(days=1), pki_settings.zone
    ollama.models.remove("qwen3:4b")  # the summary model cannot run: no build just to fail
    assert old_day not in await chapters.plan_chapters(pool, ctx.router, zone, now)
    ollama.models.append("qwen3:4b")
    async with pool.connection() as conn:
        await conn.execute("UPDATE chapter SET summary_error = 'x', summary_error_at = %s WHERE id = %s",
                           (now - dt.timedelta(hours=1), chapter))
    assert old_day not in await chapters.plan_chapters(pool, ctx.router, zone, now)  # failed an hour ago: wait
    async with pool.connection() as conn:
        await conn.execute("UPDATE chapter SET summary_error_at = %s", (now - dt.timedelta(hours=7),))
    assert old_day in await chapters.plan_chapters(pool, ctx.router, zone, now)


async def test_a_forget_while_the_model_writes_stores_no_summary(pool, dev, ctx, monkeypatch):
    await _two_scenes(pool, dev["device_id"])
    chat = ctx.router.chat

    async def chat_during_a_forget(task, messages, **kwargs):
        async with pool.connection() as conn:  # "Quên khoảng thời gian" commits while the model is writing
            await conn.execute("INSERT INTO forget_log (id, range_start, range_end, counts) VALUES ('F1', %s, %s, '{}')",
                               (NINE, NINE + dt.timedelta(minutes=5)))
        return await chat(task, messages, **kwargs)

    monkeypatch.setattr(ctx.router, "chat", chat_during_a_forget)
    result = await chapters.build_chapters(ctx, DAY, now=NEXT_NOON)
    assert result.summarised == 0 and [c["title"] for c in await _chapters(pool)] == [None, None]


async def test_the_worker_builds_the_chapters_a_planner_asked_for(pool, dev, ctx, pki_settings, jobs_open):
    await _two_scenes(pool, dev["device_id"])
    assert await chapters.defer_build(pool, DAY, pki_settings.zone) is not None
    await run_jobs(ctx)
    assert [(c["closed"], c["title"]) for c in await _chapters(pool)] == [(True, "Chương 1"), (True, "Chương 2")]


async def test_a_failed_build_is_listed_with_its_error_and_runs_again_by_itself(pool, dev, pki_settings, jobs_open):
    await insert_moment(pool, dev["device_id"], ts=NINE)
    job_id = await chapters.defer_build(pool, DAY, pki_settings.zone)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))
        await conn.execute("INSERT INTO day_job_error (task, day, error) VALUES ('build_chapters', %s, 'boom')", (DAY,))
    [row] = (await status.pipeline_status(pool))["failed"]
    assert (row["label"], row["target"], row["error"], row["retryable"]) == ("Chia chương", "2026-10-14", "boom", False)
    assert row["rerun"] == "tự chạy lại ở lần lên lịch chia chương (mỗi giờ; chương tóm tắt lỗi: sau 6 giờ)"


def test_the_planner_runs_every_hour():
    periodic = {(p.task.name, p.cron) for p in jobs.job_app.periodic_registry.periodic_tasks.values()}
    assert (jobs.PLAN_CHAPTERS, "5 * * * *") in periodic
    assert json.dumps(chapters.SUMMARY_SCHEMA)  # sent as Ollama's `format`
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_chapters.py -q`
Expected: FAIL — `ImportError: cannot import name 'chapters' from 'loopi_brain.pipeline'`.

- [ ] **Step 3: Register the task names, the job lock and priority, the day lock and the per-day errors**

Apply to `brain/src/loopi_brain/pipeline/jobs.py`, `brain/src/loopi_brain/pipeline/state.py` and `brain/src/loopi_brain/pipeline/status.py`:

```diff
diff --git a/brain/src/loopi_brain/pipeline/jobs.py b/brain/src/loopi_brain/pipeline/jobs.py
--- a/brain/src/loopi_brain/pipeline/jobs.py
+++ b/brain/src/loopi_brain/pipeline/jobs.py
@@ -16,6 +16,7 @@ TASK_MODULES = [
     "loopi_brain.pipeline.moments",
     "loopi_brain.pipeline.caption",
     "loopi_brain.pipeline.asr",
+    "loopi_brain.pipeline.chapters",
 ]
 
 ANALYZE_VIDEO = "analyze_video"
@@ -26,10 +27,18 @@ EMBED_MOMENTS = "embed_moments"
 DISPATCH_PENDING = "dispatch_pending"
 RETRY_STALLED = "retry_stalled_jobs"
 REMOVE_OLD_JOBS = "remove_old_jobs"
+PLAN_CHAPTERS = "plan_chapters"
+BUILD_CHAPTERS = "build_chapters"
 PIPELINE_TASKS = (ANALYZE_VIDEO, CAPTION_KEYFRAME, TRANSCRIBE_AUDIO, INDEX_MARKER, EMBED_MOMENTS)
 # Runs that start again by themselves (the dispatcher asks for embedding runs; the scheduler starts
-# each periodic tick), so a failed one is never retried and only the newest failed one is kept.
-AUTO_RERUN_TASKS = (EMBED_MOMENTS, DISPATCH_PENDING, RETRY_STALLED, REMOVE_OLD_JOBS)
+# each periodic tick; the hourly planner asks for each day's chapters again), so a failed one is never
+# retried and only the newest failed one is kept.
+AUTO_RERUN_TASKS = (EMBED_MOMENTS, DISPATCH_PENDING, RETRY_STALLED, REMOVE_OLD_JOBS, PLAN_CHAPTERS, BUILD_CHAPTERS)
+# The text models run one job at a time (chapters, recaps): each one takes most of the CPU. They also wait behind
+# every live job (video, captions, ASR, markers, embeddings: priority 0) for a free worker slot, so recordings keep
+# their 15-minute latency target while chapters and recaps are being written (spec §14 P2).
+LLM_LOCK = "llm"
+LLM_PRIORITY = -10
 
 # Spec §12: a failed job is retried 3 times, then stays 'failed' (the dead-letter list).
 # RetryStrategy compares max_attempts with the attempts already made (0 on the first run),
@@ -60,16 +69,18 @@ async def enqueue(
     queue: str,
     queueing_lock: str,
     lock: str | None = None,
+    priority: int | None = None,
     **task_kwargs,
 ) -> int:
-    """Defer ``task`` inside the caller's transaction and return the job id.
+    """Defer ``task`` inside the caller's transaction and return the job id (``priority``: higher runs first,
+    default 0).
 
     If a 'todo' job already holds ``queueing_lock``, return that job's id instead.
     """
     try:
         async with conn.transaction():  # savepoint: a unique violation must not abort the caller
             deferrer = job_app.configure_task(
-                task, queue=queue, lock=lock, queueing_lock=queueing_lock, connection=conn
+                task, queue=queue, lock=lock, queueing_lock=queueing_lock, priority=priority, connection=conn
             )
             return await deferrer.defer_async(**task_kwargs)
     except AlreadyEnqueued:
```

```diff
diff --git a/brain/src/loopi_brain/pipeline/state.py b/brain/src/loopi_brain/pipeline/state.py
--- a/brain/src/loopi_brain/pipeline/state.py
+++ b/brain/src/loopi_brain/pipeline/state.py
@@ -6,6 +6,7 @@ before or during its job (e.g. by the smoke test's cleanup) makes the job a no-o
 """
 from __future__ import annotations
 
+import datetime as dt
 import logging
 
 from psycopg import AsyncConnection
@@ -73,3 +74,36 @@ async def keyframe_exists(pool: AsyncConnectionPool, keyframe_id: str) -> bool:
     async with pool.connection() as conn:
         cur = await conn.execute("SELECT 1 FROM keyframe WHERE id = %s", (keyframe_id,))
         return await cur.fetchone() is not None
+
+
+async def record_day_error(pool: AsyncConnectionPool, task: str, day: dt.date, exc: BaseException) -> None:
+    """Store the last error of a per-day job (chapters, recap) for the dead-letter list."""
+    try:
+        async with pool.connection() as conn:
+            await conn.execute(
+                "INSERT INTO day_job_error (task, day, error) VALUES (%s, %s, %s)"
+                " ON CONFLICT (task, day) DO UPDATE SET error = EXCLUDED.error, at = now()",
+                (task, day, error_text(exc)),
+            )
+    except Exception:
+        log.exception("could not record the %s error of %s", task, day)
+
+
+async def lock_day(conn: AsyncConnection, day: dt.date) -> None:
+    """Serialise, for one local day and inside the caller's transaction, everything that writes text derived from
+    that day (chapters, summaries, the recap) with "Quên khoảng thời gian"."""
+    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"day:{day.isoformat()}",))
+
+
+async def forgotten(conn: AsyncConnection, start: dt.datetime, end: dt.datetime) -> int:
+    """How many "Quên khoảng thời gian" requests overlap [start, end): read before a model call and again (under
+    ``lock_day``) before writing its answer; a different number means the material may hold forgotten text."""
+    cur = await conn.execute(
+        "SELECT count(*) AS n FROM forget_log WHERE range_start < %s AND range_end > %s", (end, start)
+    )
+    return (await cur.fetchone())["n"]
+
+
+async def clear_day_error(pool: AsyncConnectionPool, task: str, day: dt.date) -> None:
+    async with pool.connection() as conn:
+        await conn.execute("DELETE FROM day_job_error WHERE task = %s AND day = %s", (task, day))
```

```diff
diff --git a/brain/src/loopi_brain/pipeline/status.py b/brain/src/loopi_brain/pipeline/status.py
--- a/brain/src/loopi_brain/pipeline/status.py
+++ b/brain/src/loopi_brain/pipeline/status.py
@@ -26,6 +26,8 @@ TASK_LABELS = {
     jobs.DISPATCH_PENDING: "Điều phối xử lý",
     jobs.RETRY_STALLED: "Khôi phục job bị treo",
     jobs.REMOVE_OLD_JOBS: "Dọn job cũ",
+    jobs.PLAN_CHAPTERS: "Lên lịch chia chương",
+    jobs.BUILD_CHAPTERS: "Chia chương",
 }
 _LATENCY = (
     "SELECT count(*) AS n, avg(s) AS avg, percentile_cont(0.95) WITHIN GROUP (ORDER BY s) AS p95 FROM ({}) t"
@@ -42,11 +44,12 @@ AUDIO_LATENCY = _LATENCY.format(
 )
 FAILED_JOBS = """
 SELECT j.id, j.task_name, j.attempts,
-       COALESCE(j.args->>'item_id', j.args->>'keyframe_id') AS target,
-       COALESCE(i.analysis_error, k.caption_error) AS error
+       COALESCE(j.args->>'item_id', j.args->>'keyframe_id', j.args->>'day') AS target,
+       COALESCE(i.analysis_error, k.caption_error, e.error) AS error
   FROM procrastinate_jobs j
   LEFT JOIN item i ON i.id = j.args->>'item_id'
   LEFT JOIN keyframe k ON k.id = j.args->>'keyframe_id'
+  LEFT JOIN day_job_error e ON e.task = j.task_name AND e.day::text = j.args->>'day'
  WHERE j.status = 'failed'
  ORDER BY j.id DESC
  LIMIT %s
@@ -65,12 +68,15 @@ SELECT count(*) AS n FROM keyframe k
  WHERE k.caption IS NULL AND j.id IS NULL
 """
 # jobs.AUTO_RERUN_TASKS have no retry button: the dispatcher requests a new embedding run every
-# minute, and the scheduler starts the next tick of each periodic task by itself (retry_stalled_jobs
-# every 10 minutes, remove_old_jobs daily). The value says when that happens.
+# minute, the scheduler starts the next tick of each periodic task by itself (retry_stalled_jobs
+# every 10 minutes, remove_old_jobs daily), and the hourly chapter planner asks for a day's build again
+# (a chapter whose summary failed: after 6 hours). The value says when that happens.
 EVERY_MINUTE = "tự chạy lại mỗi phút"
+CHAPTER_RERUN = "tự chạy lại ở lần lên lịch chia chương (mỗi giờ; chương tóm tắt lỗi: sau 6 giờ)"
 ON_SCHEDULE = "tự chạy lại theo lịch"
 AUTO_RERUN = {
-    task: EVERY_MINUTE if task in (jobs.EMBED_MOMENTS, jobs.DISPATCH_PENDING) else ON_SCHEDULE
+    task: EVERY_MINUTE if task in (jobs.EMBED_MOMENTS, jobs.DISPATCH_PENDING)
+    else CHAPTER_RERUN if task == jobs.BUILD_CHAPTERS else ON_SCHEDULE
     for task in jobs.AUTO_RERUN_TASKS
 }
 NOT_RETRIED = jobs.AUTO_RERUN_TASKS
```

- [ ] **Step 4: Write the chapter module**

Create `brain/src/loopi_brain/pipeline/chapters.py`:

```python
"""Chapters of a local day (spec §10.1; §9.2 "Mỗi giờ: chia chương cho phần dữ liệu mới").

Every hour the planner asks for a build of each day that may have changed. A build cuts the day's moments
into chapters (``segment.py``), keeps the settled ("closed") chapters as they are, recomputes only the open
tail, and has the chapter_summary model write a Vietnamese title and summary for each chapter whose
moments changed. A day is reopened (all of its chapters recomputed) when moments appear inside its closed
part, or when "Quên khoảng thời gian" or reprocessing changes it.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from procrastinate import JobContext
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from .. import devices
from ..days import day_bounds_utc, local_now
from ..llm import ASR_CAVEAT, PICTURE_NOTE, AnswerInvalid, clip, clipped, parse_answer, spread, within
from ..providers.base import ProviderError, ProviderUnavailable
from ..providers.router import Router
from . import jobs
from .context import PipelineContext, get_context
from .jobs import job_app
from .segment import ChapterParams, Point, closed_count, segment, signature, unit
from .state import clear_day_error, error_text, forgotten, lock_day, record_day_error

log = logging.getLogger(__name__)
OPEN_MIN_MOMENTS = 3  # the open chapter at the end of today gets a summary once it has this many moments
RECENT = dt.timedelta(hours=26)  # the planner rebuilds every day that got new moments this recently
CATCHUP_PER_RUN = 1  # days with moments but no chapter yet (analysed before P2b) built per hourly run, newest first
ERROR_COOLDOWN = dt.timedelta(hours=6)  # a chapter whose summary failed is not the reason for a build before this
TITLE_MAX = 80
SUMMARY_MAX = 600
SUMMARY_MAX_TOKENS = 400
RETRY_TEMPERATURE = 0.3
PROMPT_BUDGET = 7000  # characters of moment lines in one prompt (fits LOOPI_LLM_NUM_CTX 8192 with room)
MAX_PICTURE_LINES = 40
MAX_SPEECH_LINES = 60
MAX_MARKER_LINES = 20
SUMMARY_SYSTEM = (
    "You keep a personal diary, in Vietnamese, of what a still camera and a microphone in a home recorded. "
    f"{PICTURE_NOTE} {ASR_CAVEAT} "
    "Use only what the lines support; never invent people, names, places or events. "
    "Answer with JSON: title = a short Vietnamese title for this part of the day (at most 8 words); "
    "summary = 2 to 3 Vietnamese sentences about what happened in it."
)
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": TITLE_MAX},
        "summary": {"type": "string", "maxLength": SUMMARY_MAX},
    },
    "required": ["title", "summary"],
}


class ChapterSummary(BaseModel):
    """Over-long answers are cut, not rejected."""

    title: clipped(TITLE_MAX)
    summary: clipped(SUMMARY_MAX)


class ChapterSummaryFailed(Exception):
    """At least one chapter got no summary because the model answered badly (the job is retried)."""


@dataclass(frozen=True)
class BuildResult:
    chapters: int
    closed: int
    summarised: int


def _hm(ts: dt.datetime, zone) -> str:
    return ts.astimezone(zone).strftime("%H:%M")


async def _pending_since(conn: AsyncConnection, start: dt.datetime, end: dt.datetime) -> dt.datetime | None:
    """Earliest moment of [start, end) whose analysis is still to come: a pending item, an uncaptioned
    keyframe or an unembedded moment (work on the dead-letter list does not count: it may never come)."""
    cur = await conn.execute(
        """SELECT least(
             (SELECT min(i.started_at) FROM item i
                LEFT JOIN procrastinate_jobs j ON j.id = i.analysis_job_id AND j.status = 'failed'
               WHERE i.analysis_state = 'pending' AND j.id IS NULL AND i.ended_at >= %(start)s AND i.started_at < %(end)s),
             (SELECT min(k.ts) FROM keyframe k
                LEFT JOIN procrastinate_jobs j ON j.id = k.caption_job_id AND j.status = 'failed'
               WHERE k.caption IS NULL AND j.id IS NULL AND k.ts >= %(start)s AND k.ts < %(end)s),
             (SELECT min(m.ts_start) FROM moment m
               WHERE m.embedding IS NULL AND m.ts_start >= %(start)s AND m.ts_start < %(end)s)
           ) AS t""",
        {"start": start, "end": end},
    )
    return (await cur.fetchone())["t"]


async def _points(conn: AsyncConnection, start: dt.datetime, end: dt.datetime, after: dt.datetime | None) -> list[Point]:
    cur = await conn.execute(
        "SELECT id, kind, ts_start, ts_end, embedding FROM moment"
        " WHERE ts_start >= %s AND ts_start < %s AND (%s::timestamptz IS NULL OR ts_start > %s)"
        " ORDER BY ts_start, id",
        (start, end, after, after),
    )
    return [
        Point(r["id"], r["kind"], r["ts_start"], max(r["ts_end"], r["ts_start"]),
              None if r["embedding"] is None else unit(r["embedding"].to_numpy()))
        for r in await cur.fetchall()
    ]


async def rebuild_chapters(
    pool: AsyncConnectionPool,
    day: dt.date,
    zone,
    params: ChapterParams = ChapterParams(),
    now: dt.datetime | None = None,
) -> tuple[int, int]:
    """Recompute the open chapters of ``day``; return (chapters, closed chapters).

    Closed chapters stay unless moments appeared or vanished inside them (then the whole day is
    recomputed). An open chapter keeps its row (and its summary) when its moments are unchanged, and keeps
    its row (with the old summary, now out of date) when it still starts at the same moment.
    """
    now = now or dt.datetime.now(dt.UTC)
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn, conn.transaction():
        # One writer of a day's chapters at a time (the "llm" job lock serialises jobs; this also covers a forget
        # and callers outside the worker).
        await lock_day(conn, day)
        cur = await conn.execute(
            "SELECT id, ts_start, ts_end, last_moment_at, moment_count, signature, closed FROM chapter"
            " WHERE day = %s ORDER BY ts_start",
            (day,),
        )
        chapters = await cur.fetchall()
        closed = [c for c in chapters if c["closed"]]
        frontier = closed[-1]["last_moment_at"] if closed else None
        if closed:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM moment WHERE ts_start >= %s AND ts_start <= %s", (start, frontier)
            )
            if (await cur.fetchone())["n"] != sum(c["moment_count"] for c in closed):
                log.info("chapters of %s: moments changed inside closed chapters; recomputing the day", day)
                await conn.execute("UPDATE chapter SET closed = false WHERE day = %s", (day,))
                for c in chapters:
                    c["closed"] = False
                closed, frontier = [], None
        segments = segment(await _points(conn, start, end, frontier), params)
        pending = await _pending_since(conn, start, end)
        horizon = now - dt.timedelta(seconds=params.settle_s)
        if pending is not None:
            horizon = min(horizon, pending)
        settled = closed_count(segments, horizon, end)
        open_rows = [c for c in chapters if not c["closed"]]
        by_signature = {c["signature"]: c for c in open_rows}
        by_start = {c["ts_start"]: c for c in open_rows}
        kept: set[str] = set()
        for k, seg in enumerate(segments):
            sig = signature(seg)
            row = by_signature.get(sig)
            if row is None or row["id"] in kept:
                row = by_start.get(seg[0].start)
                if row is not None and row["id"] in kept:
                    row = None
            values = (seg[0].start, max(p.end for p in seg), seg[-1].start, len(seg), sig, k < settled)
            if row is None:
                chapter_id = devices.new_id()
                await conn.execute(
                    "INSERT INTO chapter (id, day, ts_start, ts_end, last_moment_at, moment_count, signature, closed)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (chapter_id, day, *values),
                )
            else:
                chapter_id = row["id"]
                current = (row["ts_start"], row["ts_end"], row["last_moment_at"], row["moment_count"],
                           row["signature"], row["closed"])
                if values != current:  # updated_at moves only when the chapter changes (the recap planner reads it)
                    await conn.execute(
                        "UPDATE chapter SET ts_start = %s, ts_end = %s, last_moment_at = %s, moment_count = %s,"
                        " signature = %s, closed = %s, updated_at = now() WHERE id = %s",
                        (*values, chapter_id),
                    )
            kept.add(chapter_id)
        await conn.execute(
            "DELETE FROM chapter WHERE day = %s AND NOT closed AND NOT (id = ANY(%s))", (day, list(kept))
        )
    return len(closed) + len(segments), len(closed) + settled


async def chapter_lines(pool: AsyncConnectionPool, first: dt.datetime, last: dt.datetime, zone) -> list[str]:
    """What happened in a chapter, one line per moment, bounded: at most 40 picture lines (repeats dropped),
    60 speech lines and 20 markers, spread over the chapter, within PROMPT_BUDGET characters."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT m.kind, m.ts_start, m.text, m.lang, i.meta AS item_meta FROM moment m"
            " LEFT JOIN item i ON i.id = m.item_id"
            " WHERE m.ts_start >= %s AND m.ts_start <= %s ORDER BY m.ts_start, m.id",
            (first, last),
        )
        rows = await cur.fetchall()
    pictures, speech, markers = [], [], []
    previous_picture = None
    for r in rows:
        at = _hm(r["ts_start"], zone)
        if r["kind"] == "visual":
            if r["text"] != previous_picture:
                pictures.append((r["ts_start"], f"{at} picture: {clip(r['text'], 200)}"))
            previous_picture = r["text"]
        elif r["kind"] == "speech":
            speech.append((r["ts_start"], f"{at} speech ({r['lang'] or '?'}): {clip(r['text'], 300)}"))
        else:
            note = (r["item_meta"] or {}).get("note")
            text = f": {clip(note, 100)}" if isinstance(note, str) and note.strip() else ""
            markers.append((r["ts_start"], f"{at} the user pressed the mark button{text}"))
    chosen = (spread(pictures, MAX_PICTURE_LINES) + spread(speech, MAX_SPEECH_LINES)
              + spread(markers, MAX_MARKER_LINES))
    return within([line for _, line in sorted(chosen, key=lambda pair: pair[0])], PROMPT_BUDGET)


def summary_messages(lines: list[str], first: dt.datetime, end: dt.datetime, zone) -> list[dict]:
    return [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": f"Part of the day from {_hm(first, zone)} to {_hm(end, zone)}:\n" + "\n".join(lines)},
    ]


async def summarise_chapters(ctx: PipelineContext, day: dt.date, attempt: int = 0) -> tuple[int, list[str]]:
    """Write the title and summary of every chapter of ``day`` whose moments changed since its summary
    (closed chapters, and the open one once it has OPEN_MIN_MOMENTS moments). Returns (written, errors).

    A model server outage stops here without an error: the next hourly build continues. A bad answer is
    stored on its chapter and the other chapters go on. If part of the day is forgotten while the model writes,
    nothing more is stored: the forget asked for a new build.
    """
    zone = ctx.settings.zone
    start, end = day_bounds_utc(day, zone)
    async with ctx.pool.connection() as conn:
        generation = await forgotten(conn, start, end)
        cur = await conn.execute(
            "SELECT id, ts_start, ts_end, last_moment_at, signature FROM chapter"
            " WHERE day = %s AND summary_signature IS DISTINCT FROM signature"
            " AND (closed OR moment_count >= %s) ORDER BY ts_start",
            (day, OPEN_MIN_MOMENTS),
        )
        rows = await cur.fetchall()
    written, errors = 0, []
    for row in rows:
        lines = await chapter_lines(ctx.pool, row["ts_start"], row["last_moment_at"], zone)
        try:
            result = await ctx.router.chat(
                "chapter_summary",
                summary_messages(lines, row["ts_start"], row["ts_end"], zone),
                json_schema=SUMMARY_SCHEMA,
                temperature=0.0 if attempt == 0 else RETRY_TEMPERATURE,
                max_tokens=SUMMARY_MAX_TOKENS,
            )
            answer = parse_answer(result.text, ChapterSummary)
        except ProviderUnavailable as exc:
            log.warning("chapters of %s: model server unavailable, summaries wait for the next build: %s", day, exc)
            break
        except (ProviderError, AnswerInvalid) as exc:
            errors.append(error_text(exc))
            async with ctx.pool.connection() as conn:
                await conn.execute(
                    "UPDATE chapter SET summary_error = %s, summary_error_at = now() WHERE id = %s",
                    (errors[-1], row["id"]),
                )
            continue
        async with ctx.pool.connection() as conn, conn.transaction():
            await lock_day(conn, day)
            if await forgotten(conn, start, end) != generation:
                log.info("chapters of %s: part of the day was forgotten during the summary; not stored", day)
                break
            await conn.execute(
                "UPDATE chapter SET title = %s, summary = %s, model_version = %s, summary_signature = %s,"
                " summary_error = NULL, summary_error_at = NULL, updated_at = now() WHERE id = %s AND signature = %s",
                (answer.title, answer.summary, result.model_version, row["signature"], row["id"], row["signature"]),
            )
        written += 1
    return written, errors


async def build_chapters(
    ctx: PipelineContext,
    day: dt.date,
    attempt: int = 0,
    params: ChapterParams = ChapterParams(),
    now: dt.datetime | None = None,
) -> BuildResult:
    """Rebuild the chapters of ``day`` and summarise the ones that changed."""
    try:
        chapters, closed = await rebuild_chapters(ctx.pool, day, ctx.settings.zone, params, now)
        summarised, errors = await summarise_chapters(ctx, day, attempt)
        if errors:
            raise ChapterSummaryFailed(f"{len(errors)} chapter summaries failed; last: {errors[-1]}")
    except Exception as exc:
        await record_day_error(ctx.pool, jobs.BUILD_CHAPTERS, day, exc)
        raise
    await clear_day_error(ctx.pool, jobs.BUILD_CHAPTERS, day)
    log.info("chapters of %s: %d (%d closed), %d summaries written", day, chapters, closed, summarised)
    return BuildResult(chapters, closed, summarised)


async def defer_build(pool: AsyncConnectionPool, day: dt.date, zone) -> int | None:
    """Ask for a build of ``day`` if it has moments or chapters; one waiting build per day (errors are logged)."""
    start, end = day_bounds_utc(day, zone)
    try:
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT EXISTS (SELECT 1 FROM moment WHERE ts_start >= %s AND ts_start < %s)"
                " OR EXISTS (SELECT 1 FROM chapter WHERE day = %s) AS needed",
                (start, end, day),
            )
            if not (await cur.fetchone())["needed"]:
                return None
            return await jobs.enqueue(
                conn, jobs.BUILD_CHAPTERS, queue="understand", lock=jobs.LLM_LOCK, priority=jobs.LLM_PRIORITY,
                queueing_lock=f"chapters:{day.isoformat()}", day=day.isoformat(),
            )
    except Exception:
        log.exception("could not ask for the chapters of %s", day)
        return None


async def plan_chapters(
    pool: AsyncConnectionPool, router: Router, zone, now: dt.datetime | None = None
) -> list[dt.date]:
    """The hourly planner; returns the days it asked a build for:
    - today, yesterday, every day that got moments in the last 26 hours (a backlog being analysed) and every day
      with open chapters;
    - while the chapter_summary model can run: every day with a chapter still waiting for its summary (an outage
      or a failure on an older, settled day), except chapters whose summary failed less than 6 hours ago;
    - one day with moments but no chapter at all (days analysed before P2b was installed), the newest first.
    """
    now = now or dt.datetime.now(dt.UTC)
    today = local_now(zone, now).date()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT (ts_start AT TIME ZONE %s)::date AS day FROM moment WHERE created_at > %s"
            " UNION SELECT day FROM chapter WHERE NOT closed",
            (zone.key, now - RECENT),
        )
        days = {r["day"] for r in await cur.fetchall()} | {today, today - dt.timedelta(days=1)}
        if router.task_ready("chapter_summary", await router.health()):
            cur = await conn.execute(
                "SELECT DISTINCT day FROM chapter WHERE summary_signature IS DISTINCT FROM signature"
                " AND (closed OR moment_count >= %s) AND (summary_error_at IS NULL OR summary_error_at < %s)",
                (OPEN_MIN_MOMENTS, now - ERROR_COOLDOWN),
            )
            days |= {r["day"] for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT d.day FROM (SELECT DISTINCT (ts_start AT TIME ZONE %s)::date AS day FROM moment) d"
            " WHERE NOT EXISTS (SELECT 1 FROM chapter c WHERE c.day = d.day) ORDER BY d.day DESC",
            (zone.key,),
        )
        unbuilt = [r["day"] for r in await cur.fetchall() if r["day"] not in days]
        days |= set(unbuilt[:CATCHUP_PER_RUN])
    return [day for day in sorted(days) if await defer_build(pool, day, zone) is not None]


@job_app.periodic(cron="5 * * * *")
@job_app.task(name=jobs.PLAN_CHAPTERS, queue="light", queueing_lock="plan_chapters")
async def plan_chapters_job(timestamp: int | None = None) -> None:
    ctx = get_context()
    await plan_chapters(ctx.pool, ctx.router, ctx.settings.zone)


@job_app.task(name=jobs.BUILD_CHAPTERS, queue="understand", lock=jobs.LLM_LOCK, retry=jobs.RETRY, pass_context=True)
async def build_chapters_job(context: JobContext, day: str) -> None:
    await build_chapters(get_context(), dt.date.fromisoformat(day), attempt=context.job.attempts)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_chapters.py -q`
Expected: `19 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `390 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/pipeline/chapters.py brain/src/loopi_brain/pipeline/jobs.py \
  brain/src/loopi_brain/pipeline/state.py brain/src/loopi_brain/pipeline/status.py brain/tests/helpers.py \
  brain/tests/test_chapters.py
git commit -F - <<'EOF'
feat(brain): hourly chapters with Vietnamese titles and summaries (spec 10.1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-16; they amend the steps above):**
- **R-I1:** in the planner tests' `_old()` helper set `moment.created_at` from the clock the planner is given (`NINE - dt.timedelta(days=days_ago)`), not the database clock (`now()`), so the tests stay green as the real date moves on.
- **R-I2:** use `moments.marker_note(meta)` (P2a, `pipeline/moments.py`) for the marker note, `days.hm` for "HH:MM" and `llm.marker_line` for the marker line — no private copies.
- **R-M1:** `summarise_chapters` skips a chapter whose `summary_error_at` is within the last 6 hours on its first attempt (same cooldown the planner applies to older days), with a test — so a chapter whose answer is always invalid does not cost 4 model calls every hour.

---

### Task 5: The day recap, its commitments and the recap planner (23:30, and again when the day changed)

**Files:**
- Create: `brain/src/loopi_brain/pipeline/recap.py`
- Modify: `brain/src/loopi_brain/pipeline/jobs.py`, `brain/src/loopi_brain/pipeline/status.py`
- Test: `brain/tests/test_recap.py`

**Interfaces:**
- Consumes: Task 1 tables `day_recap`, `commitment`, `forget_log`, `day_bounds_utc`, `local_now`; Task 2 `ASR_CAVEAT`, `clip`, `clipped`, `parse_answer`, `within`; Task 4 `jobs.LLM_LOCK`, `jobs.LLM_PRIORITY`, `jobs.enqueue(priority=…)`, `state.record_day_error`/`clear_day_error`, `state.lock_day`, `state.forgotten`, chapter rows (`closed`, `updated_at`), `helpers.insert_chapter`; P2a `Router.chat`, `Router.health`, `Router.task_ready`.
- Produces:
  - `jobs.PLAN_RECAPS = "plan_recaps"` (periodic, cron `"*/15 * * * *"`, in `AUTO_RERUN_TASKS`), `jobs.WRITE_RECAP = "write_recap"` (queue `understand`, lock `llm`, priority `LLM_PRIORITY`, queueing lock `recap:<YYYY-MM-DD>`, kwargs `{"day": …}`; **not** auto-rerun: its dead letter has a retry button).
  - `recap.write_recap(ctx, day, attempt=0) -> bool` (False: no data — any old row is deleted —, the model server is down, or part of the day was forgotten while the model wrote: nothing stored, the planner asks again); `recap.defer_recap(pool, day) -> int`; `recap.plan_recaps(pool, router, zone, now=None) -> list[date]`; `recap.set_commitment_status(pool, commitment_id, status) -> dict | None` (`ValueError` for a status outside `STATUSES = ("open", "done", "dismissed")`).
  - `recap.recap_material(pool, day, zone) -> Material | None`, `recap.recap_messages(material, day)`, `recap.compose_markdown(answer, commitments, zone) -> str`, `recap.sourced(answer, refs)`; constants `RECAP_TIME = 23:30`, `HEADINGS`, `NOTHING = "Không có."`, `RECAP_SCHEMA`, `ITEM_MAX = 200`, `MAX_EVENTS = 8`, `MAX_MEMORABLE = 5`, `MAX_COMMITMENTS = 8`, `RECAP_MAX_TOKENS = 1000`, `MAX_SPEECH_LINES = 60`, `PROMPT_BUDGET = 9000`, `RETRY_TEMPERATURE = 0.3`.
  - Labels "Lên lịch recap" (no button, "tự chạy lại theo lịch") and "Viết recap" (retry button).

The recap model (`recap`, `qwen3:8b`) reads: the day's chapters (time range, title, summary), every marked moment as `[M1]`, `[M2]`, …, and the day's 60 *longest* speech lines in time order as `[S1]`, `[S2]`, … (≤ 9000 characters in all). It answers JSON `{main_events: [...], memorable: [...], commitments: [{text, source}]}`. The brain writes the markdown — always the three sections of spec §10.2 — and stores each commitment whose `source` is one of the references it was shown (the real moment id behind it), status `open`. Writing again keeps the commitments the user marked done or dismissed and skips new ones with the same source moment or the same text.

The answer is kept small so a CPU writes it in minutes (≤ 1000 tokens, ≤ 8 + 5 bullets of ≤ 200 characters, ≤ 8 commitments; Task 2 gives text models a 900 s timeout). The whole run reads the forget counter first and stores the recap and commitments under `lock_day` only if no "Quên khoảng thời gian" overlapping the day arrived meanwhile (else the recap could quote forgotten speech).

The planner (every 15 minutes): nothing while the recap model cannot run (`task_ready`); never a day whose recap job waits, runs or failed. Otherwise, among days with chapters:
- **today** from 23:30 local time, unless a fresh recap was written at or after 23:30 — a recap written earlier (e.g. by "Tạo lại recap" at noon) is written again at 23:30;
- **an earlier day** once all its chapters are closed, if it has no fresh recap or its recap is older than its last chapter change (`max(chapter.updated_at)`: late uploads, a backlog, summaries that came later) — at most once per change;
- a `stale` recap (reprocessing) counts as no recap.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_recap.py`:

```python
import datetime as dt

import pytest

from helpers import (
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_chapter,
    insert_item,
    insert_moment,
    jobs_by_lock,
    make_ctx,
    paired_device,
    run_jobs,
)
from loopi_brain.llm import ASR_CAVEAT, AnswerInvalid
from loopi_brain.pipeline import jobs, recap, status

DAY = dt.date(2026, 10, 14)
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
ANSWER = {
    "main_events": ["Buổi sáng đọc sách", "Trưa nấu ăn cùng nhau"],
    "memorable": ["Đánh dấu lúc tìm thấy chìa khoá"],
    "commitments": [
        {"text": "Đi khám răng lúc 9 giờ sáng mai", "source": "S2"},
        {"text": "Một việc bịa ra", "source": "S99"},  # not a line of the day: dropped
    ],
}
MARKDOWN = (
    "## Diễn biến chính\n- Buổi sáng đọc sách\n- Trưa nấu ăn cùng nhau\n\n"
    "## Khoảnh khắc đáng nhớ\n- Đánh dấu lúc tìm thấy chìa khoá\n\n"
    "## Việc cần làm hoặc đã hứa\n- Đi khám răng lúc 9 giờ sáng mai (09:20)"
)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    fake = FakeOllama()
    fake.reply = lambda body: ANSWER
    return fake


@pytest.fixture
def ctx(pool, pki_settings, ollama):
    return make_ctx(pool, pki_settings, ollama=ollama)


async def _day(pool, device_id) -> dict:
    await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(minutes=30), title="Đọc sách",
                         summary="Một người đọc sách bên bàn.")
    await insert_chapter(pool, DAY, NINE + dt.timedelta(hours=1), NINE + dt.timedelta(hours=2), title="Nấu ăn",
                         summary="Hai người nấu ăn trong bếp.")
    thanks = await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=10),
                                 text="Thank you.", lang="en")
    dentist = await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=20),
                                  text="Mai 9 giờ mình phải đi khám răng nhé", lang="vi")
    item = await insert_item(pool, device_id, kind="marker", started_at=NINE + dt.timedelta(minutes=40), seconds=0,
                             meta={"note": "chìa khoá"})
    marker = await insert_moment(pool, device_id, kind="marker", ts=NINE + dt.timedelta(minutes=40), item_id=item,
                                 text="chìa khoá")
    return {"thanks": thanks, "dentist": dentist, "marker": marker}


def _chats(ollama) -> list[dict]:
    return [body for path, body in ollama.requests if path == "/api/chat"]


async def test_a_day_gets_a_vietnamese_recap_and_its_sourced_commitments(pool, dev, ctx, ollama):
    ids = await _day(pool, dev["device_id"])
    assert await recap.write_recap(ctx, DAY) is True
    row = await fetch_one(pool, "SELECT * FROM day_recap WHERE day = %s", (DAY,))
    assert row["markdown"] == MARKDOWN and row["model_version"] == "ollama:qwen3:8b" and row["stale"] is False
    [commitment] = await fetch_all(pool, "SELECT * FROM commitment")
    assert (commitment["day"], commitment["text"], commitment["source_moment_id"], commitment["status"]) == (
        DAY, "Đi khám răng lúc 9 giờ sáng mai", ids["dentist"], "open")
    [body] = _chats(ollama)
    assert body["model"] == "qwen3:8b" and body["format"] == recap.RECAP_SCHEMA and body["keep_alive"] == "1m"
    system, user = body["messages"]
    assert ASR_CAVEAT in system["content"]
    assert "09:00–09:30 Đọc sách: Một người đọc sách bên bàn." in user["content"]
    assert "[S1] 09:10 (en) Thank you." in user["content"]
    assert "[S2] 09:20 (vi) Mai 9 giờ mình phải đi khám răng nhé" in user["content"]
    assert "[M1] 09:40 the user pressed the mark button: chìa khoá" in user["content"]


async def test_a_day_without_data_gets_no_recap_row(pool, dev, ctx, ollama):
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'old', 'x')", (DAY,))
    assert await recap.write_recap(ctx, DAY) is False  # e.g. the whole day was forgotten
    assert await fetch_all(pool, "SELECT * FROM day_recap") == [] and _chats(ollama) == []


async def test_writing_again_keeps_the_users_decisions(pool, dev, ctx, ollama):
    ids = await _day(pool, dev["device_id"])
    await recap.write_recap(ctx, DAY)
    [first] = await fetch_all(pool, "SELECT id FROM commitment")
    await recap.set_commitment_status(pool, first["id"], "done")
    ollama.reply = lambda body: {**ANSWER, "commitments": [
        {"text": "Đi khám răng lúc 9 giờ sáng mai", "source": "S2"},  # already done: not added again
        {"text": "Mua sữa", "source": "[s1]"},
    ]}
    await recap.write_recap(ctx, DAY)
    rows = await fetch_all(pool, "SELECT text, source_moment_id, status FROM commitment ORDER BY status")
    assert rows == [
        {"text": "Đi khám răng lúc 9 giờ sáng mai", "source_moment_id": ids["dentist"], "status": "done"},
        {"text": "Mua sữa", "source_moment_id": ids["thanks"], "status": "open"},
    ]


async def test_a_model_server_outage_writes_nothing_and_spends_no_retry(pool, dev, ctx, ollama):
    await _day(pool, dev["device_id"])
    ollama.down = True
    assert await recap.write_recap(ctx, DAY) is False
    assert await fetch_all(pool, "SELECT * FROM day_recap") == []
    assert await fetch_all(pool, "SELECT * FROM day_job_error") == []


async def test_an_unusable_answer_fails_the_job_and_leaves_its_error(pool, dev, ctx, ollama):
    await _day(pool, dev["device_id"])
    ollama.reply = lambda body: "Here is your recap: it was a nice day."
    with pytest.raises(AnswerInvalid):
        await recap.write_recap(ctx, DAY)
    error = await fetch_one(pool, "SELECT task, error FROM day_job_error")
    assert error["task"] == "write_recap" and error["error"].startswith("AnswerInvalid")
    ollama.reply = lambda body: ANSWER
    assert await recap.write_recap(ctx, DAY, attempt=1) is True
    assert await fetch_all(pool, "SELECT * FROM day_job_error") == []
    assert _chats(ollama)[-1]["options"]["temperature"] == recap.RETRY_TEMPERATURE


async def test_an_answer_with_too_much_is_cut_and_blank_lines_dropped(pool, dev, ctx, ollama):
    await _day(pool, dev["device_id"])
    ollama.reply = lambda body: {"main_events": ["  ", *[f"Việc {n}" for n in range(30)]], "memorable": [],
                                 "commitments": []}
    await recap.write_recap(ctx, DAY)
    markdown = (await fetch_one(pool, "SELECT markdown FROM day_recap"))["markdown"]
    assert markdown.count("- Việc ") == recap.MAX_EVENTS and "- Việc 0\n" in markdown
    assert "## Khoảnh khắc đáng nhớ\nKhông có." in markdown and markdown.endswith("## Việc cần làm hoặc đã hứa\nKhông có.")


async def test_a_long_day_fits_the_prompt_budget(pool, dev, ctx, ollama):
    device_id = dev["device_id"]
    await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(hours=10), title="Cả ngày", summary="Một ngày dài.")
    for n in range(500):
        await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=n), lang="vi",
                            text=" ".join(["lời"] * (n % 40 + 1)))
    await recap.write_recap(ctx, DAY)
    user = _chats(ollama)[0]["messages"][1]["content"]
    speech = [line for line in user.split("\n") if line.startswith("[S")]
    assert 0 < len(speech) <= recap.MAX_SPEECH_LINES
    assert sum(len(line) + 1 for line in user.split("\n") if line.startswith(("[S", "[M", "09:"))) <= recap.PROMPT_BUDGET
    assert all(line.count("lời") >= 30 for line in speech)  # the longest lines, not the first ones


async def test_the_planner_writes_today_at_23_30_and_earlier_days_once_settled(pool, dev, ctx, pki_settings, jobs_open):
    zone = pki_settings.zone
    await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(hours=1), closed=False)  # today, still open
    await insert_chapter(pool, DAY - dt.timedelta(days=1), NINE - dt.timedelta(days=1),
                         NINE - dt.timedelta(days=1, hours=-1))  # yesterday: settled
    await insert_chapter(pool, DAY - dt.timedelta(days=3), NINE - dt.timedelta(days=3),
                         NINE - dt.timedelta(days=3, hours=-1), closed=False)  # a backlog day still analysed
    evening = dt.datetime(2026, 10, 14, 22, 0, tzinfo=zone)
    assert await recap.plan_recaps(pool, ctx.router, zone, evening) == [DAY - dt.timedelta(days=1)]
    late = dt.datetime(2026, 10, 14, 23, 31, tzinfo=zone)
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == [DAY]  # yesterday's job already waits
    locks = await jobs_by_lock(pool)
    assert [j["args"] for j in locks["recap:2026-10-13"]] == [{"day": "2026-10-13"}]
    [job] = locks["recap:2026-10-14"]
    assert (job["task_name"], job["queue_name"], job["lock"], job["priority"]) == (
        "write_recap", "understand", "llm", jobs.LLM_PRIORITY)
    assert "recap:2026-10-11" not in locks


async def test_today_is_written_again_at_23_30_after_an_earlier_recap(pool, dev, ctx, pki_settings, jobs_open):
    zone = pki_settings.zone
    await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(hours=1), closed=False)
    async with pool.connection() as conn:  # "Tạo lại recap" pressed at noon
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version, generated_at) VALUES (%s, 'x', 'x', %s)",
                           (DAY, dt.datetime(2026, 10, 14, 12, 0, tzinfo=zone)))
    assert await recap.plan_recaps(pool, ctx.router, zone, dt.datetime(2026, 10, 14, 23, 0, tzinfo=zone)) == []
    assert await recap.plan_recaps(pool, ctx.router, zone, dt.datetime(2026, 10, 14, 23, 31, tzinfo=zone)) == [DAY]
    async with pool.connection() as conn:  # written at 23:35: done for today
        await conn.execute("DELETE FROM procrastinate_jobs")
        await conn.execute("UPDATE day_recap SET generated_at = %s", (dt.datetime(2026, 10, 14, 23, 35, tzinfo=zone),))
    assert await recap.plan_recaps(pool, ctx.router, zone, dt.datetime(2026, 10, 14, 23, 50, tzinfo=zone)) == []


async def test_an_earlier_day_is_written_again_once_after_its_chapters_change(pool, dev, ctx, pki_settings, jobs_open):
    zone = pki_settings.zone
    past = DAY - dt.timedelta(days=2)
    await insert_chapter(pool, past, NINE - dt.timedelta(days=2), NINE - dt.timedelta(days=2, hours=-1))
    written = dt.datetime(2026, 10, 12, 23, 30, tzinfo=zone)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version, generated_at) VALUES (%s, 'x', 'x', %s)",
                           (past, written))
        await conn.execute("UPDATE chapter SET updated_at = %s", (written - dt.timedelta(hours=1),))
    now = dt.datetime(2026, 10, 14, 10, 0, tzinfo=zone)
    assert await recap.plan_recaps(pool, ctx.router, zone, now) == []  # the recap is newer than its chapters
    async with pool.connection() as conn:  # a summary arrived after the recap (an outage, a late upload)
        await conn.execute("UPDATE chapter SET updated_at = %s", (written + dt.timedelta(hours=3),))
    assert await recap.plan_recaps(pool, ctx.router, zone, now) == [past]
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM procrastinate_jobs")
        await conn.execute("UPDATE day_recap SET generated_at = %s", (written + dt.timedelta(hours=4),))
    assert await recap.plan_recaps(pool, ctx.router, zone, now) == []  # written again once
    async with pool.connection() as conn:  # reopened: wait until it settles again
        await conn.execute("UPDATE chapter SET closed = false, updated_at = %s", (written + dt.timedelta(hours=5),))
    assert await recap.plan_recaps(pool, ctx.router, zone, now) == []


async def test_a_forget_while_the_recap_is_written_stores_nothing(pool, dev, ctx, monkeypatch):
    await _day(pool, dev["device_id"])
    chat = ctx.router.chat

    async def chat_during_a_forget(task, messages, **kwargs):
        async with pool.connection() as conn:  # "Quên khoảng thời gian" commits while the model is writing
            await conn.execute("INSERT INTO forget_log (id, range_start, range_end, counts) VALUES ('F1', %s, %s, '{}')",
                               (NINE + dt.timedelta(minutes=15), NINE + dt.timedelta(minutes=25)))
        return await chat(task, messages, **kwargs)

    monkeypatch.setattr(ctx.router, "chat", chat_during_a_forget)
    assert await recap.write_recap(ctx, DAY) is False  # its text may quote the forgotten line
    assert await fetch_all(pool, "SELECT * FROM day_recap") == [] and await fetch_all(pool, "SELECT * FROM commitment") == []


async def test_the_planner_skips_fresh_and_failed_recaps_and_waits_for_the_model(pool, dev, ctx, ollama, pki_settings,
                                                                                 jobs_open):
    zone = pki_settings.zone
    late = dt.datetime(2026, 10, 14, 23, 45, tzinfo=zone)
    await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(hours=1))
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'x', 'x')", (DAY,))
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == []
    async with pool.connection() as conn:
        await conn.execute("UPDATE day_recap SET stale = true")  # reprocessing changed the day
    ollama.models.remove("qwen3:8b")
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == []  # no job just to fail
    ollama.models.append("qwen3:8b")
    assert "recap:2026-10-14" not in await jobs_by_lock(pool)
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == [DAY]
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed' WHERE queueing_lock = 'recap:2026-10-14'")
    assert await recap.plan_recaps(pool, ctx.router, zone, late) == []  # on the dead-letter list instead


async def test_the_worker_writes_the_recap_and_a_failure_can_be_retried(pool, dev, ctx, jobs_open):
    await _day(pool, dev["device_id"])
    job_id = await recap.defer_recap(pool, DAY)
    assert await recap.defer_recap(pool, DAY) == job_id  # one waiting request per day
    await run_jobs(ctx)
    assert (await fetch_one(pool, "SELECT markdown FROM day_recap"))["markdown"] == MARKDOWN
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))
        await conn.execute("INSERT INTO day_job_error (task, day, error) VALUES ('write_recap', %s, 'boom')", (DAY,))
    [row] = (await status.pipeline_status(pool))["failed"]
    assert (row["label"], row["target"], row["error"], row["retryable"]) == ("Viết recap", "2026-10-14", "boom", True)


async def test_commitment_status_changes(pool, dev, ctx):
    await _day(pool, dev["device_id"])
    await recap.write_recap(ctx, DAY)
    [row] = await fetch_all(pool, "SELECT id FROM commitment")
    assert (await recap.set_commitment_status(pool, row["id"], "dismissed"))["status"] == "dismissed"
    assert await recap.set_commitment_status(pool, "01NOSUCHCOMMITMENT00000000", "done") is None
    with pytest.raises(ValueError):
        await recap.set_commitment_status(pool, row["id"], "later")


def test_the_recap_planner_runs_every_quarter_hour():
    periodic = {(p.task.name, p.cron) for p in jobs.job_app.periodic_registry.periodic_tasks.values()}
    assert (jobs.PLAN_RECAPS, "*/15 * * * *") in periodic
    assert jobs.WRITE_RECAP not in jobs.AUTO_RERUN_TASKS and jobs.PLAN_RECAPS in jobs.AUTO_RERUN_TASKS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_recap.py -q`
Expected: FAIL — `ImportError: cannot import name 'recap' from 'loopi_brain.pipeline'`.

- [ ] **Step 3: Register the recap tasks**

Apply to `brain/src/loopi_brain/pipeline/jobs.py` and `brain/src/loopi_brain/pipeline/status.py`:

```diff
diff --git a/brain/src/loopi_brain/pipeline/jobs.py b/brain/src/loopi_brain/pipeline/jobs.py
--- a/brain/src/loopi_brain/pipeline/jobs.py
+++ b/brain/src/loopi_brain/pipeline/jobs.py
@@ -17,6 +17,7 @@ TASK_MODULES = [
     "loopi_brain.pipeline.caption",
     "loopi_brain.pipeline.asr",
     "loopi_brain.pipeline.chapters",
+    "loopi_brain.pipeline.recap",
 ]
 
 ANALYZE_VIDEO = "analyze_video"
@@ -29,11 +30,15 @@ RETRY_STALLED = "retry_stalled_jobs"
 REMOVE_OLD_JOBS = "remove_old_jobs"
 PLAN_CHAPTERS = "plan_chapters"
 BUILD_CHAPTERS = "build_chapters"
+PLAN_RECAPS = "plan_recaps"
+WRITE_RECAP = "write_recap"
 PIPELINE_TASKS = (ANALYZE_VIDEO, CAPTION_KEYFRAME, TRANSCRIBE_AUDIO, INDEX_MARKER, EMBED_MOMENTS)
 # Runs that start again by themselves (the dispatcher asks for embedding runs; the scheduler starts
 # each periodic tick; the hourly planner asks for each day's chapters again), so a failed one is never
 # retried and only the newest failed one is kept.
-AUTO_RERUN_TASKS = (EMBED_MOMENTS, DISPATCH_PENDING, RETRY_STALLED, REMOVE_OLD_JOBS, PLAN_CHAPTERS, BUILD_CHAPTERS)
+AUTO_RERUN_TASKS = (
+    EMBED_MOMENTS, DISPATCH_PENDING, RETRY_STALLED, REMOVE_OLD_JOBS, PLAN_CHAPTERS, BUILD_CHAPTERS, PLAN_RECAPS,
+)
 # The text models run one job at a time (chapters, recaps): each one takes most of the CPU. They also wait behind
 # every live job (video, captions, ASR, markers, embeddings: priority 0) for a free worker slot, so recordings keep
 # their 15-minute latency target while chapters and recaps are being written (spec §14 P2).
```

```diff
diff --git a/brain/src/loopi_brain/pipeline/status.py b/brain/src/loopi_brain/pipeline/status.py
--- a/brain/src/loopi_brain/pipeline/status.py
+++ b/brain/src/loopi_brain/pipeline/status.py
@@ -28,6 +28,8 @@ TASK_LABELS = {
     jobs.REMOVE_OLD_JOBS: "Dọn job cũ",
     jobs.PLAN_CHAPTERS: "Lên lịch chia chương",
     jobs.BUILD_CHAPTERS: "Chia chương",
+    jobs.PLAN_RECAPS: "Lên lịch recap",
+    jobs.WRITE_RECAP: "Viết recap",
 }
 _LATENCY = (
     "SELECT count(*) AS n, avg(s) AS avg, percentile_cont(0.95) WITHIN GROUP (ORDER BY s) AS p95 FROM ({}) t"
```

- [ ] **Step 4: Write the recap module**

Create `brain/src/loopi_brain/pipeline/recap.py`:

```python
"""The end-of-day recap and its commitments (spec §10.2; §9.2 "23:30: recap cuối ngày, trích việc cần làm").

The recap model reads the day's chapters, marked moments and the most substantial speech lines, and answers
with JSON: the main course of the day, memorable moments, and the things someone said they must do or
promised, each citing the reference of the line it comes from. The brain writes the Vietnamese markdown
(three fixed sections) and stores each commitment whose reference is a real moment of that day.

A recap is written again when the day changed after it: today's at 23:30 if it was written earlier (e.g. by
"Tạo lại recap"), an earlier day's once its chapters are settled again after a change (late uploads, a
backlog, summaries that came later) — at most one more run per change.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from procrastinate import JobContext
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, field_validator

from .. import devices
from ..days import day_bounds_utc, local_now
from ..llm import ASR_CAVEAT, clip, clipped, parse_answer, within
from ..providers.base import ProviderUnavailable
from ..providers.router import Router
from . import jobs
from .context import PipelineContext, get_context
from .jobs import job_app
from .state import clear_day_error, forgotten, lock_day, record_day_error

log = logging.getLogger(__name__)
RECAP_TIME = dt.time(23, 30)  # local time today's recap is written (spec §9.2)
# Output stays small: on the 4-core CPU qwen3:8b writes ≈ 5–7 tokens/s, so 1000 tokens are ≈ 3 minutes.
ITEM_MAX = 200
MAX_EVENTS = 8
MAX_MEMORABLE = 5
MAX_COMMITMENTS = 8
RECAP_MAX_TOKENS = 1000
RETRY_TEMPERATURE = 0.3
MAX_SPEECH_LINES = 60  # the longest speech lines of the day ("Thank you." and other short noise go first)
CHAPTER_BUDGET = 3500
MARKER_BUDGET = 1000
PROMPT_BUDGET = 9000
STATUSES = ("open", "done", "dismissed")
HEADINGS = ("Diễn biến chính", "Khoảnh khắc đáng nhớ", "Việc cần làm hoặc đã hứa")
NOTHING = "Không có."
RECAP_SYSTEM = (
    "You write the end-of-day recap of a personal diary, in Vietnamese, from what a still camera and a microphone "
    "in a home recorded during one day. Chapter lines are earlier Vietnamese summaries of parts of the day. "
    f"{ASR_CAVEAT} Use only what the material supports; never invent people, names or events. Answer with JSON: "
    "main_events = 3 to 8 short Vietnamese bullet points on the main course of the day, in time order; "
    "memorable = up to 5 short Vietnamese bullet points on memorable moments (marked moments, notable events, "
    "important or funny things said); commitments = the things someone said they must do or promised to do "
    "(a task, an appointment, a purchase), each with text = the task in Vietnamese and source = the reference "
    "in square brackets of the speech or marked line it comes from, such as \"S3\" or \"M1\". Leave commitments "
    "empty when nobody said such a thing."
)
RECAP_SCHEMA = {
    "type": "object",
    "properties": {
        "main_events": {"type": "array", "items": {"type": "string", "maxLength": ITEM_MAX}, "maxItems": MAX_EVENTS},
        "memorable": {"type": "array", "items": {"type": "string", "maxLength": ITEM_MAX}, "maxItems": MAX_MEMORABLE},
        "commitments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string", "maxLength": ITEM_MAX}, "source": {"type": "string"}},
                "required": ["text", "source"],
            },
            "maxItems": MAX_COMMITMENTS,
        },
    },
    "required": ["main_events", "memorable", "commitments"],
}


class CommitmentAnswer(BaseModel):
    text: clipped(ITEM_MAX)
    source: str


class RecapAnswer(BaseModel):
    """Over-long text and lists are cut, not rejected; blank entries are dropped."""

    main_events: list[clipped(ITEM_MAX)]
    memorable: list[clipped(ITEM_MAX)]
    commitments: list[CommitmentAnswer]

    @field_validator("main_events", "memorable")
    @classmethod
    def _non_blank(cls, value: list[str]) -> list[str]:
        return [v for v in value if v.strip()]

    @field_validator("main_events")
    @classmethod
    def _cap_events(cls, value: list[str]) -> list[str]:
        return value[:MAX_EVENTS]

    @field_validator("memorable")
    @classmethod
    def _cap_memorable(cls, value: list[str]) -> list[str]:
        return value[:MAX_MEMORABLE]

    @field_validator("commitments")
    @classmethod
    def _cap_commitments(cls, value: list[CommitmentAnswer]) -> list[CommitmentAnswer]:
        return [c for c in value if c.text.strip()][:MAX_COMMITMENTS]


@dataclass
class Material:
    """What the recap model reads; ``refs`` maps "S3"/"M1" to (moment id, its time)."""

    chapters: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    speech: list[str] = field(default_factory=list)
    refs: dict[str, tuple[str, dt.datetime]] = field(default_factory=dict)


def _hm(ts: dt.datetime, zone) -> str:
    return ts.astimezone(zone).strftime("%H:%M")


async def recap_material(pool: AsyncConnectionPool, day: dt.date, zone) -> Material | None:
    """The day's chapters, marked moments and longest speech lines, within PROMPT_BUDGET characters;
    None when the day has no moment at all."""
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT EXISTS (SELECT 1 FROM moment WHERE ts_start >= %s AND ts_start < %s) AS any",
                                 (start, end))
        if not (await cur.fetchone())["any"]:
            return None
        cur = await conn.execute(
            "SELECT ts_start, ts_end, title, summary FROM chapter WHERE day = %s ORDER BY ts_start", (day,)
        )
        chapter_rows = await cur.fetchall()
        cur = await conn.execute(
            "SELECT m.id, m.ts_start, i.meta AS item_meta FROM moment m LEFT JOIN item i ON i.id = m.item_id"
            " WHERE m.kind = 'marker' AND m.ts_start >= %s AND m.ts_start < %s ORDER BY m.ts_start, m.id",
            (start, end),
        )
        marker_rows = await cur.fetchall()
        cur = await conn.execute(
            "SELECT id, ts_start, lang, text FROM moment"
            " WHERE kind = 'speech' AND ts_start >= %s AND ts_start < %s ORDER BY ts_start, id",
            (start, end),
        )
        speech_rows = await cur.fetchall()
    material = Material()
    material.chapters = within([
        f"{_hm(c['ts_start'], zone)}–{_hm(c['ts_end'], zone)} {clip(c['title'], 80)}: {clip(c['summary'], 600)}"
        if c["title"] else f"{_hm(c['ts_start'], zone)}–{_hm(c['ts_end'], zone)} (no summary yet)"
        for c in chapter_rows
    ], CHAPTER_BUDGET)
    for n, row in enumerate(marker_rows, start=1):
        note = (row["item_meta"] or {}).get("note")
        text = f": {clip(note, 100)}" if isinstance(note, str) and note.strip() else ""
        material.markers.append(f"[M{n}] {_hm(row['ts_start'], zone)} the user pressed the mark button{text}")
        material.refs[f"M{n}"] = (row["id"], row["ts_start"])
    material.markers = within(material.markers, MARKER_BUDGET)
    longest = sorted(speech_rows, key=lambda r: (-len(r["text"].split()), r["ts_start"], r["id"]))[:MAX_SPEECH_LINES]
    budget = PROMPT_BUDGET - sum(len(line) + 1 for line in material.chapters + material.markers)
    lines = []
    for n, row in enumerate(sorted(longest, key=lambda r: (r["ts_start"], r["id"])), start=1):
        lines.append(f"[S{n}] {_hm(row['ts_start'], zone)} ({row['lang'] or '?'}) {clip(row['text'], 250)}")
        material.refs[f"S{n}"] = (row["id"], row["ts_start"])
    material.speech = within(lines, budget)
    shown = {line[1:].split("]", 1)[0] for line in material.markers + material.speech}
    material.refs = {ref: value for ref, value in material.refs.items() if ref in shown}
    return material


def recap_messages(material: Material, day: dt.date) -> list[dict]:
    parts = [
        f"Day: {day.isoformat()}",
        "Chapters:\n" + ("\n".join(material.chapters) or "(none yet)"),
        "Marked moments:\n" + ("\n".join(material.markers) or "(none)"),
        "Speech lines:\n" + ("\n".join(material.speech) or "(none)"),
    ]
    return [{"role": "system", "content": RECAP_SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]


def compose_markdown(answer: RecapAnswer, commitments: list[tuple[str, dt.datetime]], zone) -> str:
    """The recap in markdown: the three sections of spec §10.2, always present, in a fixed order."""

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) if items else NOTHING

    todo = [f"{text} ({_hm(ts, zone)})" for text, ts in commitments]
    bodies = (bullets(answer.main_events), bullets(answer.memorable), bullets(todo))
    return "\n\n".join(f"## {heading}\n{body}" for heading, body in zip(HEADINGS, bodies, strict=True))


def sourced(answer: RecapAnswer, refs: dict[str, tuple[str, dt.datetime]]) -> list[tuple[str, str, dt.datetime]]:
    """(text, moment id, time) of each commitment whose source is a line of the material; others are dropped."""
    out, seen = [], set()
    for c in answer.commitments:
        ref = c.source.strip().strip("[]").upper()
        if ref not in refs:
            log.info("recap commitment %r dropped: unknown source %r", c.text, c.source)
            continue
        moment_id, ts = refs[ref]
        key = (moment_id, c.text.strip().lower())
        if key not in seen:
            seen.add(key)
            out.append((c.text.strip(), moment_id, ts))
    return out


async def write_recap(ctx: PipelineContext, day: dt.date, attempt: int = 0) -> bool:
    """Write (or rewrite) the recap of ``day`` and its open commitments; False if nothing was written (no data;
    the model server is down; or part of the day was forgotten while the model wrote, so its text may quote
    forgotten moments — in the last two cases the planner asks again)."""
    pool, zone = ctx.pool, ctx.settings.zone
    start, end = day_bounds_utc(day, zone)
    try:
        async with pool.connection() as conn:
            generation = await forgotten(conn, start, end)
        material = await recap_material(pool, day, zone)
        if material is None:
            async with pool.connection() as conn:
                await conn.execute("DELETE FROM day_recap WHERE day = %s", (day,))
            await clear_day_error(pool, jobs.WRITE_RECAP, day)
            return False
        try:
            result = await ctx.router.chat(
                "recap", recap_messages(material, day), json_schema=RECAP_SCHEMA,
                temperature=0.0 if attempt == 0 else RETRY_TEMPERATURE, max_tokens=RECAP_MAX_TOKENS,
            )
        except ProviderUnavailable as exc:
            log.warning("recap of %s waits: model server unavailable: %s", day, exc)
            return False
        answer = parse_answer(result.text, RecapAnswer)
        commitments = sourced(answer, material.refs)
        markdown = compose_markdown(answer, [(text, ts) for text, _, ts in commitments], zone)
        async with pool.connection() as conn, conn.transaction():
            await lock_day(conn, day)
            if await forgotten(conn, start, end) != generation:
                log.info("recap of %s: part of the day was forgotten while it was written; not stored", day)
                return False
            await conn.execute(
                "INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, %s, %s)"
                " ON CONFLICT (day) DO UPDATE SET markdown = EXCLUDED.markdown,"
                " model_version = EXCLUDED.model_version, generated_at = now(), stale = false",
                (day, markdown, result.model_version),
            )
            # The user's decisions stay; open commitments are replaced by the new ones.
            cur = await conn.execute(
                "SELECT lower(text) AS text, source_moment_id FROM commitment WHERE day = %s AND status <> 'open'",
                (day,),
            )
            decided = await cur.fetchall()
            await conn.execute("DELETE FROM commitment WHERE day = %s AND status = 'open'", (day,))
            for text, moment_id, _ts in commitments:
                if any(d["source_moment_id"] == moment_id or d["text"] == text.lower() for d in decided):
                    continue
                await conn.execute(
                    "INSERT INTO commitment (id, day, text, source_moment_id, model_version)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (devices.new_id(), day, text, moment_id, result.model_version),
                )
    except Exception as exc:
        await record_day_error(pool, jobs.WRITE_RECAP, day, exc)
        raise
    await clear_day_error(pool, jobs.WRITE_RECAP, day)
    log.info("recap of %s written with %d commitments", day, len(commitments))
    return True


async def defer_recap(pool: AsyncConnectionPool, day: dt.date) -> int:
    """Ask for the recap of ``day`` (one waiting request per day)."""
    async with pool.connection() as conn, conn.transaction():
        return await jobs.enqueue(
            conn, jobs.WRITE_RECAP, queue="understand", lock=jobs.LLM_LOCK, priority=jobs.LLM_PRIORITY,
            queueing_lock=f"recap:{day.isoformat()}", day=day.isoformat(),
        )


async def plan_recaps(pool: AsyncConnectionPool, router: Router, zone, now: dt.datetime | None = None) -> list[dt.date]:
    """Ask for the recap of every day with chapters that needs one, and return those days:
    - today from 23:30 local time, unless its recap was written at or after 23:30;
    - an earlier day once all its chapters are settled, if it has no recap, or its recap is older than the
      day's last chapter change;
    - a stale recap (reprocessing) by the same rules.
    Never a day whose recap job waits, runs or failed (the failure is on the dead-letter list, with a retry
    button), and nothing while the recap model cannot run (no job is created just to fail)."""
    local = local_now(zone, now)
    today = local.date()
    if not router.task_ready("recap", await router.health()):
        log.warning("recaps wait: the recap model is not available")
        return []
    tonight = dt.datetime.combine(today, RECAP_TIME, tzinfo=zone)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT c.day, bool_and(c.closed) AS settled, max(c.updated_at) AS changed,
                      max(r.generated_at) AS written, bool_or(r.stale) AS stale
                 FROM chapter c LEFT JOIN day_recap r ON r.day = c.day
                WHERE c.day <= %(today)s
                  AND NOT EXISTS (SELECT 1 FROM procrastinate_jobs j
                                   WHERE j.queueing_lock = 'recap:' || c.day::text
                                     AND j.status IN ('todo', 'doing', 'failed'))
                GROUP BY c.day ORDER BY c.day""",
            {"today": today},
        )
        rows = await cur.fetchall()
    due = []
    for r in rows:
        fresh = r["written"] is not None and not r["stale"]
        if r["day"] == today:
            if local >= tonight and not (fresh and r["written"] >= tonight):
                due.append(r["day"])
        elif r["settled"] and not (fresh and r["written"] >= r["changed"]):
            due.append(r["day"])
    for day in due:
        await defer_recap(pool, day)
    return due


async def set_commitment_status(pool: AsyncConnectionPool, commitment_id: str, status: str) -> dict | None:
    """Mark a commitment open, done or dismissed; the updated row, or None if there is no such commitment."""
    if status not in STATUSES:
        raise ValueError(f"unknown commitment status {status!r}")
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE commitment SET status = %s, updated_at = now() WHERE id = %s RETURNING *", (status, commitment_id)
        )
        return await cur.fetchone()


@job_app.periodic(cron="*/15 * * * *")
@job_app.task(name=jobs.PLAN_RECAPS, queue="light", queueing_lock="plan_recaps")
async def plan_recaps_job(timestamp: int | None = None) -> None:
    ctx = get_context()
    await plan_recaps(ctx.pool, ctx.router, ctx.settings.zone)


@job_app.task(name=jobs.WRITE_RECAP, queue="understand", lock=jobs.LLM_LOCK, retry=jobs.RETRY, pass_context=True)
async def write_recap_job(context: JobContext, day: str) -> None:
    await write_recap(get_context(), dt.date.fromisoformat(day), attempt=context.job.attempts)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_recap.py -q`
Expected: `15 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `405 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/pipeline/recap.py brain/src/loopi_brain/pipeline/jobs.py \
  brain/src/loopi_brain/pipeline/status.py brain/tests/test_recap.py
git commit -F - <<'EOF'
feat(brain): 23:30 recap in Vietnamese with sourced commitments (spec 10.2)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-16; they amend the steps above):**
- **R-I2:** use `moments.marker_note`, `days.hm` and `llm.marker_line` (Tasks 1–2) instead of private copies.
- **R-I4:** `rewrite_recap` first deletes the day's failed `write_recap` jobs (`DELETE FROM procrastinate_jobs WHERE queueing_lock = %s AND status = 'failed'`, events cascade) and clears the day's recap error, then defers the new run; extend `test_rewriting_the_recap_marks_it_stale_and_asks_for_a_new_one`: a failed job + the POST → no failure banner and `plan_recaps` can plan the day again.

---

### Task 6: The Day page — chapters, recap, commitments and thumbnails

**Files:**
- Create: `brain/src/loopi_brain/web/day_routes.py`, `brain/src/loopi_brain/web/markdown.py`, `brain/src/loopi_brain/thumbs.py`
- Create: `brain/src/loopi_brain/web/templates/day.html`, `brain/src/loopi_brain/web/templates/_commitment.html`
- Modify: `brain/src/loopi_brain/media.py` (`inside`), `brain/src/loopi_brain/web/routes.py` (hourly view at `/hours`), `brain/src/loopi_brain/app.py`, `brain/src/loopi_brain/web/templates/base.html`, `brain/src/loopi_brain/web/templates/timeline.html`
- Modify: `brain/tests/test_web.py`, `brain/tests/test_web_pipeline.py`, `brain/tests/test_audio_playback.py` (the hourly view's new URL; its thumbnails)
- Test: `brain/tests/test_day_page.py`, `brain/tests/test_markdown.py` (new)

**Interfaces:**
- Consumes: Task 1 `day_bounds_utc`, `local_now`, `item.raw_deleted_at`; Task 4 chapter rows; Task 5 `recap.defer_recap`, `recap.set_commitment_status`, `recap.STATUSES`, `day_recap`, `commitment`; P2a `web.routes.TEMPLATES`, `require_htmx`, `MARKER_LABEL`, `moments.marker_note`, `/audio/{id}.opus`.
- Produces:
  - `GET /` (and `/?day=YYYY-MM-DD`) — the Day page; `GET /hours?day=…` — the P2a hourly view ("Chi tiết theo giờ"), moved from `/`.
  - Every moment on the Day page has the anchor `id="m-<moment id>"` (chat citations link to `/?day=<local day>#m-<id>`; a small script opens the folded section that holds it).
  - `GET /thumb/{keyframe_id}.jpg` — 320 px JPEG, made on the first request under `thumbs/<device>/YYYY/MM/DD/<id>.jpg`.
  - `POST /day/{day}/recap` ("Tạo lại recap": marks the recap stale, defers `write_recap`), `POST /commitments/{id}/{status}` (returns the `_commitment.html` fragment); both need `HX-Request: true`.
  - `web.markdown.render_markdown(text, heading_offset=1) -> Markup` (headings, `-` lists, paragraphs, `**bold**`; everything escaped first).
  - `thumbs.THUMB_WIDTH = 320`, `thumbs.thumb_rel(keyframe_rel: PurePosixPath) -> PurePosixPath` (ValueError unless under `keyframes/`), `thumbs.make_thumb(src, dst)`.
  - `media.inside(media_root, rel) -> Path` (ValueError for an absolute path or one leaving the media root; used by Tasks 10 and 11 too).
  - `web.day_routes.day_view(pool, day, zone, today=None) -> dict` (keys `sections` — chapters and the moments outside any chapter, in time order —, `earlier_open`, `recap_failed`, `recap`, `recap_html`, `recap_at`, `commitments`, `has_data`), `day_title(day) -> str` ("Thứ Tư, 14/10/2026"), `OPEN_DAYS_BACK = 7`.

What the page shows, top to bottom: date navigation and "Chi tiết theo giờ"; the recap (safe markdown), or why there is none, and — when the day's recap job failed — "Viết recap lỗi: …" with a pointer to "Xử lý"; the day's commitments; on today's page also the commitments still open from the previous 7 days ("Việc còn mở từ những ngày trước"); then the chapters and the moments outside any chapter ("Chưa chia chương", e.g. before the first hourly build), in time order.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_markdown.py`:

```python
from loopi_brain.web.markdown import render_markdown


def test_headings_lists_paragraphs_and_bold():
    html = render_markdown("## Diễn biến chính\n- Sáng **đọc sách**\n- Trưa nấu ăn\n\nMột đoạn\nhai dòng.")
    assert html == (
        "<h3>Diễn biến chính</h3>\n<ul><li>Sáng <strong>đọc sách</strong></li><li>Trưa nấu ăn</li></ul>\n"
        "<p>Một đoạn hai dòng.</p>"
    )


def test_model_text_cannot_inject_markup():
    html = render_markdown('- <script>alert(1)</script> [x](javascript:alert(1)) <img src=x onerror=y>\n# <b>t</b>')
    assert "<script>" not in html and "<img" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html and "<h2>&lt;b&gt;t&lt;/b&gt;</h2>" in html
    assert "href" not in html  # links are text


def test_a_list_after_a_paragraph_and_back():
    assert render_markdown("Mở đầu\n- một\nKết") == "<p>Mở đầu</p>\n<ul><li>một</li></ul>\n<p>Kết</p>"
    assert render_markdown("") == ""
```

Create `brain/tests/test_day_page.py`:

```python
import datetime as dt

import httpx
import pytest
from PIL import Image

from helpers import (
    FakeOllama,
    fetch_one,
    insert_chapter,
    insert_item,
    insert_keyframe,
    insert_moment,
    jobs_by_lock,
    paired_device,
    write_jpeg,
)
from loopi_brain.app import create_app
from loopi_brain.days import local_now

HX = {"HX-Request": "true"}
DAY = dt.date(2026, 10, 14)
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
RECAP = "## Diễn biến chính\n- Sáng **đọc sách**\n- <script>alert(1)</script>\n\n## Khoảnh khắc đáng nhớ\nKhông có."


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
async def web(pool, pki_settings, jobs_open):
    app = create_app(pki_settings, pool=pool, http_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _day(pool, settings, device_id) -> dict:
    video = await insert_item(pool, device_id, kind="video", started_at=NINE)
    frames = []
    for n in range(8):
        kf = await insert_keyframe(pool, settings, video, device_id, NINE + dt.timedelta(minutes=n))
        frames.append(await _visual(pool, device_id, kf, NINE + dt.timedelta(minutes=n), f"Scene {n} <b>"))
    audio = await insert_item(pool, device_id, kind="audio", started_at=NINE + dt.timedelta(minutes=10))
    speech = await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=10, seconds=42),
                                 text="Mai mình đi chợ <nhé>", lang="vi", item_id=audio)
    note = await insert_item(pool, device_id, kind="marker", started_at=NINE + dt.timedelta(minutes=20), seconds=0,
                             meta={"note": "chìa khoá"})
    marker = await insert_moment(pool, device_id, kind="marker", ts=NINE + dt.timedelta(minutes=20), item_id=note)
    first = await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(minutes=12), title="Đọc sách buổi sáng",
                                 summary="Một người đọc sách. Sau đó hẹn đi chợ.")
    second = await insert_chapter(pool, DAY, NINE + dt.timedelta(minutes=20), NINE + dt.timedelta(minutes=20))
    late = await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(hours=2), text="Mới nói xong",
                               lang="vi")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, %s, 'ollama:qwen3:8b')",
                           (DAY, RECAP))
        await conn.execute(
            "INSERT INTO commitment (id, day, text, source_moment_id, model_version)"
            " VALUES ('C1', %s, 'Đi chợ', %s, 'ollama:qwen3:8b')", (DAY, speech))
    return {"frames": frames, "speech": speech, "audio": audio, "marker": marker, "first": first,
            "second": second, "late": late}


async def _visual(pool, device_id, keyframe_id, ts, text) -> str:
    moment_id = await insert_moment(pool, device_id, kind="visual", ts=ts, text=text, lang="en")
    async with pool.connection() as conn:
        await conn.execute("UPDATE moment SET keyframe_id = %s WHERE id = %s", (keyframe_id, moment_id))
    return moment_id


async def test_a_day_shows_its_recap_commitments_and_chapters_in_order(web, pool, pki_settings, dev):
    ids = await _day(pool, pki_settings, dev["device_id"])
    text = (await web.get("/", params={"day": "2026-10-14"})).text
    assert "<h1>Thứ Tư, 14/10/2026</h1>" in text and 'href="/hours?day=2026-10-14"' in text
    assert 'href="/?day=2026-10-13"' in text and 'href="/?day=2026-10-15"' in text
    # The recap: rendered markdown, with the model's text escaped.
    assert "<h3>Diễn biến chính</h3>" in text and "<li>Sáng <strong>đọc sách</strong></li>" in text
    assert "&lt;script&gt;" in text and "<script>alert" not in text
    assert 'hx-post="/day/2026-10-14/recap"' in text
    # The commitment, linked to the line it came from, with its buttons.
    assert f'href="/?day=2026-10-14#m-{ids["speech"]}">09:10</a>' in text
    assert 'hx-post="/commitments/C1/done"' in text and "Chưa xong" in text
    # Chapters in order: time range, title, summary, six thumbnails then the rest folded, speech with its audio.
    assert text.index("Đọc sách buổi sáng") < text.index("Đang tóm tắt…") < text.index("Chưa chia chương (1 khoảnh khắc)")
    assert "<time>09:00–09:12</time> Đọc sách buổi sáng" in text and "Một người đọc sách. Sau đó hẹn đi chợ." in text
    first = text.split(f'id="ch-{ids["first"]}"')[1].split("</section>")[0]
    assert first.count('src="/thumb/') == 8 and "Tất cả khung hình (8)" in first
    assert f'id="m-{ids["frames"][7]}"' in first and "Scene 7 &lt;b&gt;" in first
    assert f'<audio controls preload="none" src="/audio/{ids["audio"]}.opus#t=42"></audio>' in first
    assert "Mai mình đi chợ &lt;nhé&gt;" in first
    second = text.split(f'id="ch-{ids["second"]}"')[1].split("</section>")[0]
    assert f'<p class="marker" id="m-{ids["marker"]}"><time>09:20:00</time> ★ chìa khoá</p>' in second
    assert f'id="m-{ids["late"]}"' in text.split(f'id="loose-{ids["late"]}"')[1]


async def test_moments_outside_chapters_are_shown_in_time_order(web, pool, dev):
    early = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="Sớm", lang="vi")
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(hours=1), text="Giữa", lang="vi")
    late = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(hours=3), text="Muộn",
                               lang="vi")
    chapter = await insert_chapter(pool, DAY, NINE + dt.timedelta(hours=1), NINE + dt.timedelta(hours=1), title="Trưa")
    text = (await web.get("/", params={"day": "2026-10-14"})).text
    assert text.index(f'id="loose-{early}"') < text.index(f'id="ch-{chapter}"') < text.index(f'id="loose-{late}"')
    assert "<time>09:00</time> Chưa chia chương (1 khoảnh khắc)" in text


async def test_today_also_lists_the_commitments_left_open_on_earlier_days(web, pool, pki_settings):
    today = local_now(pki_settings.zone).date()
    async with pool.connection() as conn:
        for cid, days_ago, status in (("C1", 2, "open"), ("C2", 3, "done"), ("C3", 8, "open"), ("C4", 0, "open")):
            await conn.execute("INSERT INTO commitment (id, day, text, status, model_version)"
                               " VALUES (%s, %s, %s, %s, 'x')", (cid, today - dt.timedelta(days=days_ago),
                                                                 f"việc {cid}", status))
    text = (await web.get("/")).text
    earlier = text.split('id="earlier-commitments"')[1].split("</section>")[0]
    assert 'id="c-C1"' in earlier and f"{today - dt.timedelta(days=2):%d/%m}" in earlier
    assert 'id="c-C2"' not in text and 'id="c-C3"' not in text  # done, or more than 7 days old
    assert 'id="c-C4"' in text.split('id="commitments"')[1].split("</section>")[0]  # today's own list
    other_day = (await web.get("/", params={"day": (today - dt.timedelta(days=1)).isoformat()})).text
    assert "earlier-commitments" not in other_day


async def test_a_failed_recap_is_shown_on_its_day(web, pool, pki_settings, dev, jobs_open):
    from loopi_brain.pipeline import recap

    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="x", lang="vi")
    job_id = await recap.defer_recap(pool, DAY)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 4 WHERE id = %s", (job_id,))
        await conn.execute("INSERT INTO day_job_error (task, day, error) VALUES ('write_recap', %s, "
                           "'ProviderError: ollama /api/chat: ReadTimeout')", (DAY,))
    text = (await web.get("/", params={"day": "2026-10-14"})).text
    assert "Viết recap lỗi: ProviderError: ollama /api/chat: ReadTimeout." in text and 'href="/pipeline"' in text


async def test_a_day_without_data_says_so(web):
    text = (await web.get("/", params={"day": "2026-10-01"})).text
    assert "Không có dữ liệu cho ngày này." in text and "Tạo lại recap" not in text


async def test_a_day_with_moments_but_nothing_written_yet(web, pool, dev):
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="Chào buổi sáng", lang="vi")
    text = (await web.get("/", params={"day": "2026-10-14"})).text
    assert "Chưa có recap." in text and "Tạo lại recap" in text and "Chưa chia chương (1 khoảnh khắc)" in text


async def test_the_landing_page_is_today(web, pki_settings):
    today = local_now(pki_settings.zone).date()
    text = (await web.get("/")).text
    assert "<h1>Hôm nay · " in text and f"{today:%d/%m/%Y}</h1>" in text
    assert (await web.get("/", params={"day": "yesterday"})).status_code == 422


async def test_audio_that_retention_removed_says_so(web, pool, pki_settings, dev):
    ids = await _day(pool, pki_settings, dev["device_id"])
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (ids["audio"],))
    day = (await web.get("/", params={"day": "2026-10-14"})).text
    assert f'/audio/{ids["audio"]}.opus' not in day and "(âm thanh đã hết hạn)" in day
    hours = (await web.get("/hours", params={"day": "2026-10-14"})).text
    row = hours.split(f'id="item-{ids["audio"]}"')[1].split("</li>")[0]
    assert "Đã hết hạn" in row and "<audio" not in row


async def test_rewriting_the_recap_marks_it_stale_and_asks_for_a_new_one(web, pool, pki_settings, dev):
    await _day(pool, pki_settings, dev["device_id"])
    assert (await web.post("/day/2026-10-14/recap")).status_code == 403
    r = await web.post("/day/2026-10-14/recap", headers=HX)
    assert r.status_code == 200 and "Đã yêu cầu viết lại recap" in r.text
    assert (await fetch_one(pool, "SELECT stale FROM day_recap"))["stale"] is True
    [job] = (await jobs_by_lock(pool))["recap:2026-10-14"]
    assert job["task_name"] == "write_recap" and job["status"] == "todo"
    assert "Recap này đã cũ" in (await web.get("/", params={"day": "2026-10-14"})).text


async def test_commitment_buttons_change_its_status(web, pool, pki_settings, dev):
    await _day(pool, pki_settings, dev["device_id"])
    assert (await web.post("/commitments/C1/done")).status_code == 403
    r = await web.post("/commitments/C1/done", headers=HX)
    assert r.status_code == 200 and 'class="commitment done"' in r.text and "Đã xong" in r.text
    assert 'hx-post="/commitments/C1/open"' in r.text and 'hx-post="/commitments/C1/done"' not in r.text
    assert (await fetch_one(pool, "SELECT status FROM commitment"))["status"] == "done"
    assert (await web.post("/commitments/C1/later", headers=HX)).status_code == 404
    assert (await web.post("/commitments/NOPE/done", headers=HX)).status_code == 404


async def test_thumbnails_are_small_cached_and_never_leave_the_media_root(web, pool, pki_settings, dev):
    device_id = dev["device_id"]
    video = await insert_item(pool, device_id, kind="video", started_at=NINE)
    kf = await insert_keyframe(pool, pki_settings, video, device_id, NINE)
    r = await web.get(f"/thumb/{kf}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    row = await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (kf,))
    thumb = pki_settings.media_root / row["path"].replace("keyframes/", "thumbs/", 1)
    with Image.open(thumb) as img:
        assert img.size == (320, 180)
    made = thumb.stat().st_mtime_ns
    assert (await web.get(f"/thumb/{kf}.jpg")).status_code == 200 and thumb.stat().st_mtime_ns == made
    assert (await web.get("/thumb/NOPE.jpg")).status_code == 404
    (pki_settings.media_root / row["path"]).unlink()
    thumb.unlink()
    assert (await web.get(f"/thumb/{kf}.jpg")).status_code == 404  # the keyframe file is gone
    write_jpeg(pki_settings.media_root.parent / "secret.jpg")
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET path = '../secret.jpg' WHERE id = %s", (kf,))
    assert (await web.get(f"/thumb/{kf}.jpg")).status_code == 404
    assert not (pki_settings.media_root.parent / "thumbs").exists()
```

The P2a tests of the hourly view now call `/hours`, and its pictures are thumbnails linked to the keyframe. Apply:

```diff
diff --git a/brain/tests/test_web.py b/brain/tests/test_web.py
--- a/brain/tests/test_web.py
+++ b/brain/tests/test_web.py
@@ -29,15 +29,15 @@ async def test_timeline_uses_local_day_boundaries(client, pool, pki_settings):
     dev = await paired_device(pool, pki_settings)
     late = dt.datetime(2026, 10, 14, 23, 30, tzinfo=dt.UTC)  # 08:30 on Oct 15 in Seoul
     await _insert_item(pool, dev["device_id"], "01JA2B3C4D5E6F7G8H9J0KMNPS", late)
-    on_15 = await client.get("/", params={"day": "2026-10-15"})
-    on_14 = await client.get("/", params={"day": "2026-10-14"})
+    on_15 = await client.get("/hours", params={"day": "2026-10-15"})
+    on_14 = await client.get("/hours", params={"day": "2026-10-14"})
     assert on_15.status_code == 200
     assert "01JA2B3C4D5E6F7G8H9J0KMNPS" in on_15.text and "08:30:00" in on_15.text
     assert "01JA2B3C4D5E6F7G8H9J0KMNPS" not in on_14.text
 
 
 async def test_timeline_rejects_bad_day(client):
-    assert (await client.get("/", params={"day": "yesterday"})).status_code == 422
+    assert (await client.get("/hours", params={"day": "yesterday"})).status_code == 422
 
 
 async def test_devices_page_shows_online_device(client, pool, pki_settings):
```

```diff
diff --git a/brain/tests/test_web_pipeline.py b/brain/tests/test_web_pipeline.py
--- a/brain/tests/test_web_pipeline.py
+++ b/brain/tests/test_web_pipeline.py
@@ -62,11 +62,11 @@ async def test_timeline_shows_keyframes_speech_and_markers_by_local_hour(web, po
     marker = await insert_item(pool, device_id, kind="marker", started_at=UTC_9AM_SEOUL + dt.timedelta(minutes=7),
                                seconds=0, meta={"note": "chìa khoá"})
     assert await moments.index_marker(make_ctx(pool, pki_settings), marker)
-    text = (await web.get("/", params={"day": DAY})).text
+    text = (await web.get("/hours", params={"day": DAY})).text
     assert "<h2>09:00</h2>" in text
     assert f'id="kf-{kf}"' in text and 'loading="lazy"' in text
     row = await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (kf,))
-    assert f'src="/media/{row["path"]}"' in text
+    assert f'href="/media/{row["path"]}"><img src="/thumb/{kf}.jpg"' in text  # a thumbnail, linked to the keyframe
     assert f"{DEFAULT_CAPTION['scene']} — {DEFAULT_CAPTION['activity']}" in text
     assert f'id="kf-{waiting}"' in text and "Đang chờ mô tả…" in text
     assert '<span class="lang">VI</span> Mai mình đi chợ &lt;b&gt;nhé&lt;/b&gt;' in text
@@ -78,7 +78,7 @@ async def test_a_marker_without_a_note_reads_danh_dau_on_the_timeline(web, pool,
     # The device uploads every button press as an empty JSON object.
     marker = await insert_item(pool, dev["device_id"], kind="marker", started_at=UTC_9AM_SEOUL, seconds=0, meta={})
     assert await moments.index_marker(make_ctx(pool, pki_settings), marker)
-    text = (await web.get("/", params={"day": DAY})).text
+    text = (await web.get("/hours", params={"day": DAY})).text
     assert '<p class="marker"><time>09:30:00</time> ★ Đánh dấu</p>' in text
     assert moments.MARKER_TEXT not in text  # the English text is only what gets embedded
 
@@ -87,7 +87,7 @@ async def test_analysed_items_are_not_marked_pending(web, pool, dev):
     item_id = await insert_item(pool, dev["device_id"], kind="audio", started_at=UTC_9AM_SEOUL)
     async with pool.connection() as conn:
         await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (item_id,))
-    text = (await web.get("/", params={"day": DAY})).text
+    text = (await web.get("/hours", params={"day": DAY})).text
     assert f'id="item-{item_id}"' in text and "Đang phân tích…" not in text
 
 
@@ -100,7 +100,7 @@ async def test_a_keyframe_whose_caption_job_failed_is_not_waiting(web, pool, pki
     assert await dispatch.defer_keyframe(pool, queued) is not None  # a caption job that is still queued
     pipeline = (await web.get("/pipeline")).text
     assert "Khung hình chờ mô tả: 1" in pipeline  # only the queued one
-    timeline = (await web.get("/", params={"day": DAY})).text
+    timeline = (await web.get("/hours", params={"day": DAY})).text
     assert timeline.count("Lỗi mô tả") == 1 and timeline.count("Đang chờ mô tả…") == 1
     failed_figure = timeline.split(f'id="kf-{broken}"')[1].split("</figure>")[0]
     assert "Lỗi mô tả" in failed_figure and "Đang chờ mô tả…" not in failed_figure
@@ -139,7 +139,7 @@ async def test_pipeline_page_shows_backlog_latency_models_and_dead_letters(web,
     tick_row = text.split(f'id="job-{tick_id}"')[1].split("</tr>")[0]
     assert "Điều phối xử lý" in tick_row and "hx-post" not in tick_row
     broken_day = (now - dt.timedelta(hours=3)).astimezone(pki_settings.zone).date().isoformat()
-    timeline = (await web.get("/", params={"day": broken_day})).text
+    timeline = (await web.get("/hours", params={"day": broken_day})).text
     assert f'id="item-{broken}"' in timeline and "Lỗi phân tích" in timeline
 
 
```

```diff
diff --git a/brain/tests/test_audio_playback.py b/brain/tests/test_audio_playback.py
--- a/brain/tests/test_audio_playback.py
+++ b/brain/tests/test_audio_playback.py
@@ -209,6 +209,6 @@ async def test_a_ten_minute_segment_is_normalised_whole(web, pool, pki_settings,
 
 async def test_the_timeline_plays_audio_through_the_normalising_route(web, pool, pki_settings, dev):
     item_id, _raw = await _audio(pool, pki_settings, dev["device_id"])
-    text = (await web.get("/", params={"day": DAY})).text
+    text = (await web.get("/hours", params={"day": DAY})).text
     assert f'<audio controls preload="none" src="/audio/{item_id}.opus"></audio>' in text
     assert "/media/raw/" not in text, "the raw file is not linked"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_markdown.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'loopi_brain.web.markdown'`.

Run: `cd brain && uv run pytest tests/test_day_page.py -q`
Expected: FAIL — `10 failed, 1 passed` (`/` still shows the hourly view).

Run: `cd brain && uv run pytest tests/test_web.py tests/test_web_pipeline.py tests/test_audio_playback.py -q`
Expected: FAIL — `8 failed, 32 passed` (`/hours` does not exist yet).

- [ ] **Step 3: Write the markdown renderer and the thumbnails**

Create `brain/src/loopi_brain/web/markdown.py`:

```python
"""A small, safe markdown renderer for recaps: headings, "- " lists, paragraphs and **bold**.

Every character is HTML-escaped before any markup is added, so text written by a model can never inject
HTML, links, images or scripts. Nothing else of markdown is supported (and nothing else is needed).
"""
from __future__ import annotations

import re

from markupsafe import Markup, escape

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"(#{1,4})\s+(.*)")


def _inline(text: str) -> str:
    return _BOLD.sub(r"<strong>\1</strong>", str(escape(text)))


def render_markdown(text: str, heading_offset: int = 1) -> Markup:
    """``text`` as HTML; ``## x`` becomes ``<h3>`` with the default offset (the page already has h1/h2)."""
    html: list[str] = []
    items: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if items:
            html.append("<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
            items.clear()
        if paragraph:
            html.append("<p>" + " ".join(paragraph) + "</p>")
            paragraph.clear()

    for raw in text.splitlines():
        line = raw.strip()
        heading = _HEADING.fullmatch(line)
        if not line:
            flush()
        elif heading:
            flush()
            level = min(len(heading.group(1)) + heading_offset, 6)
            html.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif line.startswith(("- ", "* ")):
            if paragraph:
                flush()
            items.append(_inline(line[2:].strip()))
        else:
            if items:
                flush()
            paragraph.append(_inline(line))
    flush()
    return Markup("\n".join(html))
```

Create `brain/src/loopi_brain/thumbs.py`:

```python
"""Small JPEG thumbnails of keyframes (P2a deferred M8: pages loaded the 1080p keyframes).

``keyframes/<device>/YYYY/MM/DD/<id>.jpg`` gets ``thumbs/<device>/YYYY/MM/DD/<id>.jpg``, 320 px wide, made on the
first request and kept (keyframes never change). "Quên khoảng thời gian" deletes both.
"""
from __future__ import annotations

import uuid
from pathlib import Path, PurePosixPath

from PIL import Image

THUMB_WIDTH = 320
THUMB_QUALITY = 80


def thumb_rel(keyframe_rel: PurePosixPath) -> PurePosixPath:
    """``keyframes/…/<id>.jpg`` -> ``thumbs/…/<id>.jpg``; ValueError for any other path."""
    if keyframe_rel.is_absolute() or len(keyframe_rel.parts) < 2 or keyframe_rel.parts[0] != "keyframes":
        raise ValueError(f"not a keyframe path: {keyframe_rel}")
    return PurePosixPath("thumbs", *keyframe_rel.parts[1:])


def make_thumb(src: Path, dst: Path) -> None:
    """Write ``src`` scaled to THUMB_WIDTH pixels wide (never enlarged) as JPEG to ``dst`` (temp file, then rename)."""
    with Image.open(src) as img:
        img = img.convert("RGB")
        if img.width > THUMB_WIDTH:
            img = img.resize((THUMB_WIDTH, max(1, round(img.height * THUMB_WIDTH / img.width))), Image.Resampling.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.stem}.{uuid.uuid4().hex}.part.jpg")
        try:
            img.save(tmp, format="JPEG", quality=THUMB_QUALITY)
            tmp.replace(dst)
        finally:
            tmp.unlink(missing_ok=True)
```

Apply to `brain/src/loopi_brain/media.py`:

```diff
diff --git a/brain/src/loopi_brain/media.py b/brain/src/loopi_brain/media.py
--- a/brain/src/loopi_brain/media.py
+++ b/brain/src/loopi_brain/media.py
@@ -50,6 +50,16 @@ async def run_ffmpeg(args: list[str], timeout: float) -> bytes:
         raise
 
 
+def inside(media_root: Path, rel: str | PurePosixPath) -> Path:
+    """``media_root / rel`` resolved; ValueError for an absolute path or one that leaves the media root (a path
+    read from the database is still checked before a file is read, written or deleted)."""
+    root = media_root.resolve()
+    path = (root / rel).resolve()
+    if path == root or not path.is_relative_to(root):
+        raise ValueError(f"path outside the media root: {rel}")
+    return path
+
+
 def _view_rel(raw_rel: PurePosixPath, suffix: str) -> PurePosixPath:
     """``raw/<device>/YYYY/MM/DD/<id>.<ext>`` -> ``view/<device>/YYYY/MM/DD/<id><suffix>``."""
     return PurePosixPath("view", *raw_rel.parts[1:]).with_suffix(suffix)
```

- [ ] **Step 4: Write the Day page routes and move the hourly view**

Create `brain/src/loopi_brain/web/day_routes.py`:

```python
"""The Day page ("Hôm nay" / "Ngày", spec §10.6): chapters in order with their pictures, speech and marked
moments, the recap and the commitments of one local day; keyframe thumbnails; the recap and commitment buttons."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import PurePosixPath

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from .. import media, thumbs
from ..days import day_bounds_utc, local_now
from ..pipeline import moments as moment_rules
from ..pipeline import recap as recap_rules
from .markdown import render_markdown
from .routes import MARKER_LABEL, TEMPLATES, require_htmx

log = logging.getLogger(__name__)
WEEKDAYS = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật")
STATUS_LABELS = {"open": "Chưa xong", "done": "Đã xong", "dismissed": "Đã bỏ qua"}
FRAMES_SHOWN = 6  # pictures per chapter shown at once; the others are behind "Tất cả khung hình"
OPEN_DAYS_BACK = 7  # today's page also lists the open commitments of the previous days
TEMPLATES.env.globals["STATUS_LABELS"] = STATUS_LABELS

router = APIRouter()


def day_title(day: dt.date) -> str:
    return f"{WEEKDAYS[day.weekday()]}, {day:%d/%m/%Y}"


def _moment_view(row: dict, zone) -> dict:
    view = {**row, "time": row["ts_start"].astimezone(zone).strftime("%H:%M:%S")}
    if row["kind"] == "marker":
        view["label"] = moment_rules.marker_note(row["item_meta"] or {}) or MARKER_LABEL
    if row["kind"] == "speech" and row["item_id"] and row["item_kind"] == "audio":
        offset = max(0, int((row["ts_start"] - row["item_start"]).total_seconds()))
        view["audio"] = None if row["raw_deleted_at"] else f"/audio/{row['item_id']}.opus#t={offset}"
        view["expired"] = row["raw_deleted_at"] is not None
    return view


def _group(title: str | None, rows: list[dict]) -> dict:
    frames = [r for r in rows if r["kind"] == "visual" and r["keyframe_id"]]
    return {
        "frames": frames[:FRAMES_SHOWN],
        "more_frames": frames[FRAMES_SHOWN:],
        "speech": [r for r in rows if r["kind"] == "speech"],
        "markers": [r for r in rows if r["kind"] == "marker"],
        "title": title,
    }


_COMMITMENTS = (
    "SELECT c.*, m.ts_start AS source_ts FROM commitment c LEFT JOIN moment m ON m.id = c.source_moment_id"
)


async def day_view(pool, day: dt.date, zone, today: dt.date | None = None) -> dict:
    """Everything the Day page shows for local ``day`` (on ``today``'s page: also the commitments still open
    from the previous 7 days)."""
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM chapter WHERE day = %s ORDER BY ts_start", (day,))
        chapter_rows = await cur.fetchall()
        cur = await conn.execute(
            "SELECT m.id, m.kind, m.ts_start, m.text, m.lang, m.keyframe_id, m.item_id, k.path AS keyframe_path,"
            " i.kind AS item_kind, i.meta AS item_meta, i.started_at AS item_start, i.raw_deleted_at"
            " FROM moment m LEFT JOIN item i ON i.id = m.item_id LEFT JOIN keyframe k ON k.id = m.keyframe_id"
            " WHERE m.ts_start >= %s AND m.ts_start < %s ORDER BY m.ts_start, m.id",
            (start, end),
        )
        moment_rows = [_moment_view(r, zone) for r in await cur.fetchall()]
        cur = await conn.execute("SELECT * FROM day_recap WHERE day = %s", (day,))
        recap = await cur.fetchone()
        cur = await conn.execute(f"{_COMMITMENTS} WHERE c.day = %s ORDER BY c.created_at, c.id", (day,))
        commitments = [_commitment_view(r, zone) for r in await cur.fetchall()]
        earlier = []
        if day == today:
            cur = await conn.execute(
                f"{_COMMITMENTS} WHERE c.status = 'open' AND c.day < %s AND c.day >= %s"
                " ORDER BY c.day DESC, c.created_at, c.id",
                (day, day - dt.timedelta(days=OPEN_DAYS_BACK)),
            )
            earlier = [_commitment_view(r, zone) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT e.error FROM procrastinate_jobs j"
            " LEFT JOIN day_job_error e ON e.task = j.task_name AND e.day = %s"
            " WHERE j.queueing_lock = %s AND j.status = 'failed' ORDER BY j.id DESC LIMIT 1",
            (day, f"recap:{day.isoformat()}"),
        )
        recap_failed = await cur.fetchone()
    # Chapters and the moments outside any chapter (not built yet, or arrived since), in time order.
    sections, loose = [], []
    k = 0

    def flush_loose() -> None:
        if loose:
            sections.append({**_group(None, list(loose)), "kind": "loose", "id": loose[0]["id"], "count": len(loose),
                             "start": loose[0]["ts_start"].astimezone(zone).strftime("%H:%M")})
            loose.clear()

    for row in chapter_rows:
        members = []
        while k < len(moment_rows) and moment_rows[k]["ts_start"] <= row["last_moment_at"]:
            if moment_rows[k]["ts_start"] >= row["ts_start"]:
                members.append(moment_rows[k])
            else:
                loose.append(moment_rows[k])
            k += 1
        flush_loose()
        sections.append({
            **_group(row["title"], members),
            "kind": "chapter",
            "id": row["id"],
            "summary": row["summary"],
            "outdated": row["summary"] is not None and row["summary_signature"] != row["signature"],
            "start": row["ts_start"].astimezone(zone).strftime("%H:%M"),
            "end": row["ts_end"].astimezone(zone).strftime("%H:%M"),
        })
    loose.extend(moment_rows[k:])
    flush_loose()
    return {
        "sections": sections,
        "earlier_open": earlier,
        "recap_failed": recap_failed,
        "recap": recap,
        "recap_html": render_markdown(recap["markdown"]) if recap else None,
        "recap_at": recap["generated_at"].astimezone(zone).strftime("%d/%m %H:%M") if recap else None,
        "commitments": commitments,
        "has_data": bool(moment_rows),
    }


def _commitment_view(row: dict, zone) -> dict:
    return {**row, "source_time": row["source_ts"].astimezone(zone).strftime("%H:%M") if row["source_ts"] else None}


@router.get("/", response_class=HTMLResponse)
async def day_page(request: Request, day: dt.date | None = Query(default=None)):
    zone = request.app.state.settings.zone
    today = local_now(zone).date()
    day = day or today
    return TEMPLATES.TemplateResponse(
        request,
        "day.html",
        {
            **await day_view(request.app.state.pool, day, zone, today),
            "day": day,
            "title": day_title(day),
            "is_today": day == today,
            "prev_day": day - dt.timedelta(days=1),
            "next_day": day + dt.timedelta(days=1),
        },
    )


@router.get("/thumb/{keyframe_id}.jpg")
async def thumbnail(request: Request, keyframe_id: str):
    """A keyframe 320 px wide, made on the first request and kept under ``thumbs/``."""
    settings = request.app.state.settings
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute("SELECT path FROM keyframe WHERE id = %s", (keyframe_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown keyframe")
    try:
        rel = PurePosixPath(row["path"])
        src = media.inside(settings.media_root, rel)
        dst = media.inside(settings.media_root, thumbs.thumb_rel(rel))
    except ValueError as exc:
        log.warning("keyframe %s has an unusable path: %s", keyframe_id, exc)
        raise HTTPException(status_code=404, detail="keyframe file missing") from exc
    if not src.is_file():
        raise HTTPException(status_code=404, detail="keyframe file missing")
    if not dst.is_file():
        await anyio.to_thread.run_sync(thumbs.make_thumb, src, dst)
    return FileResponse(dst, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/day/{day}/recap", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def rewrite_recap(request: Request, day: dt.date):
    """"Tạo lại recap": mark the current recap out of date and ask for a new one now."""
    pool = request.app.state.pool
    async with pool.connection() as conn:
        await conn.execute("UPDATE day_recap SET stale = true WHERE day = %s", (day,))
    await recap_rules.defer_recap(pool, day)
    return HTMLResponse("<span>Đã yêu cầu viết lại recap. Tải lại trang sau vài phút để xem.</span>")


@router.post("/commitments/{commitment_id}/{status}", response_class=HTMLResponse,
             dependencies=[Depends(require_htmx)])
async def commitment_status(request: Request, commitment_id: str, status: str):
    if status not in recap_rules.STATUSES:
        raise HTTPException(status_code=404, detail="unknown status")
    pool = request.app.state.pool
    if await recap_rules.set_commitment_status(pool, commitment_id, status) is None:
        raise HTTPException(status_code=404, detail="unknown commitment")
    async with pool.connection() as conn:
        cur = await conn.execute(f"{_COMMITMENTS} WHERE c.id = %s", (commitment_id,))
        row = await cur.fetchone()
    return TEMPLATES.TemplateResponse(
        request, "_commitment.html", {"c": _commitment_view(row, request.app.state.settings.zone)}
    )
```

Apply to `brain/src/loopi_brain/web/routes.py` and `brain/src/loopi_brain/app.py`:

```diff
diff --git a/brain/src/loopi_brain/web/routes.py b/brain/src/loopi_brain/web/routes.py
--- a/brain/src/loopi_brain/web/routes.py
+++ b/brain/src/loopi_brain/web/routes.py
@@ -54,6 +54,7 @@ async def _day_by_hour(pool, day: dt.date, zone: ZoneInfo) -> dict[int, dict[str
     async with pool.connection() as conn:
         cur = await conn.execute(
             "SELECT i.id, i.kind, i.started_at, i.ended_at, i.status, i.view_path, i.meta, i.analysis_state,"
+            " i.raw_deleted_at,"
             " j.status IS NOT DISTINCT FROM 'failed' AS analysis_failed"
             " FROM item i LEFT JOIN procrastinate_jobs j ON j.id = i.analysis_job_id"
             " WHERE i.started_at >= %s AND i.started_at < %s ORDER BY i.started_at",
@@ -97,8 +98,9 @@ def _ca_fingerprint(request: Request) -> str:
     return pki.fingerprint(pki.ca_cert_pem(request.app.state.settings.pki_dir))
 
 
-@router.get("/", response_class=HTMLResponse)
+@router.get("/hours", response_class=HTMLResponse)
 async def timeline(request: Request, day: dt.date | None = Query(default=None)):
+    """"Chi tiết theo giờ": every keyframe, speech line, marker and segment of a day, by local hour (P2a)."""
     zone = request.app.state.settings.zone
     day = day or dt.datetime.now(zone).date()
     hours = await _day_by_hour(request.app.state.pool, day, zone)
```

```diff
diff --git a/brain/src/loopi_brain/app.py b/brain/src/loopi_brain/app.py
--- a/brain/src/loopi_brain/app.py
+++ b/brain/src/loopi_brain/app.py
@@ -14,6 +14,7 @@ from .api.device_routes import router as device_router
 from .config import Settings
 from .db import make_pool
 from .pipeline.jobs import open_jobs
+from .web.day_routes import router as day_router
 from .web.routes import router as web_router
 
 log = logging.getLogger(__name__)
@@ -67,6 +68,7 @@ def create_app(
 
     app.include_router(device_router)
     app.include_router(web_router)
+    app.include_router(day_router)
     settings.media_root.mkdir(parents=True, exist_ok=True)
     app.mount("/media", StaticFiles(directory=settings.media_root), name="media")
     app.mount(
```

- [ ] **Step 5: Write the templates**

Create `brain/src/loopi_brain/web/templates/day.html`:

```html
{% extends "base.html" %}
{% macro moments(g) %}
  {% if g.frames %}
  <div class="frames">
  {% for m in g.frames %}
    <figure id="m-{{ m.id }}"><a href="/media/{{ m.keyframe_path }}"><img src="/thumb/{{ m.keyframe_id }}.jpg" loading="lazy" alt="{{ m.text }}"></a>
      <figcaption><time>{{ m.time }}</time> {{ m.text }}</figcaption></figure>
  {% endfor %}
  </div>
  {% endif %}
  {% if g.more_frames %}
  <details><summary>Tất cả khung hình ({{ g.frames|length + g.more_frames|length }})</summary>
  <div class="frames">
  {% for m in g.more_frames %}
    <figure id="m-{{ m.id }}"><a href="/media/{{ m.keyframe_path }}"><img src="/thumb/{{ m.keyframe_id }}.jpg" loading="lazy" alt="{{ m.text }}"></a>
      <figcaption><time>{{ m.time }}</time> {{ m.text }}</figcaption></figure>
  {% endfor %}
  </div>
  </details>
  {% endif %}
  {% for m in g.markers %}
  <p class="marker" id="m-{{ m.id }}"><time>{{ m.time }}</time> ★ {{ m.label }}</p>
  {% endfor %}
  {% if g.speech %}
  <details><summary>Lời nói ({{ g.speech|length }})</summary>
  {% for m in g.speech %}
    <p class="speech" id="m-{{ m.id }}"><time>{{ m.time }}</time> <span class="lang">{{ (m.lang or "?")|upper }}</span> {{ m.text }}
      {% if m.audio %}<audio controls preload="none" src="{{ m.audio }}"></audio>{% elif m.expired %}<span class="muted">(âm thanh đã hết hạn)</span>{% endif %}</p>
  {% endfor %}
  </details>
  {% endif %}
{% endmacro %}
{% block content %}
<nav class="days">
  <a href="/?day={{ prev_day }}">← {{ prev_day }}</a>
  <a href="/?day={{ next_day }}">{{ next_day }} →</a>
  <form class="inline" method="get" action="/"><input type="date" name="day" value="{{ day }}"><button>Xem</button></form>
  <a href="/hours?day={{ day }}">Chi tiết theo giờ</a>
</nav>
<h1>{% if is_today %}Hôm nay · {% endif %}{{ title }}</h1>
<section class="card" id="recap">
  <h2>Recap</h2>
  {% if recap %}
    {% if recap.stale %}<p class="muted">Recap này đã cũ và sẽ được viết lại.</p>{% endif %}
    {{ recap_html }}
    <p class="muted">Viết lúc {{ recap_at }} · {{ recap.model_version }}</p>
  {% elif has_data %}<p>Chưa có recap. Recap được viết lúc 23:30, hoặc khi mọi chương của ngày đã xong.</p>
  {% else %}<p>Không có dữ liệu cho ngày này.</p>{% endif %}
  {% if recap_failed %}<p class="err">Viết recap lỗi{% if recap_failed.error %}: {{ recap_failed.error }}{% endif %}.
    Bấm "Tạo lại recap", hoặc "Xử lý lại" ở trang <a href="/pipeline">Xử lý</a>.</p>{% endif %}
  {% if has_data %}<button hx-post="/day/{{ day }}/recap" hx-target="#recap-request">Tạo lại recap</button>
  <span id="recap-request"></span>{% endif %}
</section>
{% if commitments %}
<section class="card" id="commitments">
  <h2>Việc cần làm hoặc đã hứa</h2>
  {% for c in commitments %}{% include "_commitment.html" %}{% endfor %}
</section>
{% endif %}
{% if earlier_open %}
<section class="card" id="earlier-commitments">
  <h2>Việc còn mở từ những ngày trước</h2>
  {% for c in earlier_open %}<div class="muted">{{ c.day.strftime("%d/%m") }}</div>{% include "_commitment.html" %}{% endfor %}
</section>
{% endif %}
{% for s in sections %}
{% if s.kind == "chapter" %}
<section class="chapter" id="ch-{{ s.id }}">
  <h2><time>{{ s.start }}–{{ s.end }}</time> {{ s.title or "Đang tóm tắt…" }}</h2>
  {% if s.summary %}<p>{{ s.summary }}{% if s.outdated %} <span class="muted">(đang cập nhật)</span>{% endif %}</p>{% endif %}
  {{ moments(s) }}
</section>
{% else %}
<section class="chapter loose" id="loose-{{ s.id }}">
  <h2><time>{{ s.start }}</time> Chưa chia chương ({{ s.count }} khoảnh khắc)</h2>
  <p class="muted">Các chương được chia mỗi giờ.</p>
  {{ moments(s) }}
</section>
{% endif %}
{% endfor %}
{% endblock %}
```

Create `brain/src/loopi_brain/web/templates/_commitment.html`:

```html
<div class="commitment {{ c.status }}" id="c-{{ c.id }}">
  <span>{{ c.text }}</span>
  {% if c.source_time %}<a href="/?day={{ c.day }}#m-{{ c.source_moment_id }}">{{ c.source_time }}</a>{% endif %}
  <span class="muted">{{ STATUS_LABELS[c.status] }}</span>
  {% if c.status != "done" %}<button hx-post="/commitments/{{ c.id }}/done" hx-target="#c-{{ c.id }}" hx-swap="outerHTML">Xong</button>{% endif %}
  {% if c.status != "dismissed" %}<button hx-post="/commitments/{{ c.id }}/dismissed" hx-target="#c-{{ c.id }}" hx-swap="outerHTML">Bỏ qua</button>{% endif %}
  {% if c.status != "open" %}<button hx-post="/commitments/{{ c.id }}/open" hx-target="#c-{{ c.id }}" hx-swap="outerHTML">Mở lại</button>{% endif %}
</div>
```

Apply to `brain/src/loopi_brain/web/templates/base.html` (nav "Hôm nay", styles, the script that opens a cited moment) and `brain/src/loopi_brain/web/templates/timeline.html` (`/hours` links, thumbnails, "Đã hết hạn"):

```diff
diff --git a/brain/src/loopi_brain/web/templates/base.html b/brain/src/loopi_brain/web/templates/base.html
--- a/brain/src/loopi_brain/web/templates/base.html
+++ b/brain/src/loopi_brain/web/templates/base.html
@@ -15,6 +15,11 @@
     .frames { display: flex; flex-wrap: wrap; gap: 8px; }
     .frames figure { margin: 0; width: 224px; }
     .frames img { width: 224px; height: auto; border-radius: 4px; }
+    .chapter h2 { font-size: 1.1rem; border-bottom: 1px solid #8884; }
+    .chapter h2 time, .speech time, .marker time { font-variant-numeric: tabular-nums; color: #888; }
+    .commitment { display: flex; gap: 8px; align-items: center; padding: 4px 0; }
+    .commitment.done span:first-child, .commitment.dismissed span:first-child { text-decoration: line-through; color: #888; }
+    form.inline { display: inline; }
     .frames figcaption, .muted { font-size: 0.85rem; color: #888; }
     .lang { font-size: 0.75rem; border: 1px solid #8886; border-radius: 4px; padding: 0 4px; }
     table { border-collapse: collapse; width: 100%; }
@@ -26,7 +31,7 @@
   </style>
 </head>
 <body>
-  <header><nav><a href="/">Dòng thời gian</a><a href="/pipeline">Xử lý</a><a href="/devices">Thiết bị</a></nav></header>
+  <header><nav><a href="/">Hôm nay</a><a href="/pipeline">Xử lý</a><a href="/devices">Thiết bị</a></nav></header>
   <div id="hx-error" class="err" role="alert"></div>
   <main>{% block content %}{% endblock %}</main>
   <script>
@@ -42,6 +47,15 @@
     document.body.addEventListener("htmx:beforeRequest", () => {
       document.getElementById("hx-error").textContent = "";
     });
+    // A link to one moment (a chat citation) opens the folded section that holds it.
+    function revealHash() {
+      const target = location.hash && document.getElementById(location.hash.slice(1));
+      if (!target) return;
+      for (let d = target.closest("details"); d; d = d.parentElement.closest("details")) d.open = true;
+      target.scrollIntoView();
+    }
+    window.addEventListener("hashchange", revealHash);
+    revealHash();
   </script>
 </body>
 </html>
```

```diff
diff --git a/brain/src/loopi_brain/web/templates/timeline.html b/brain/src/loopi_brain/web/templates/timeline.html
--- a/brain/src/loopi_brain/web/templates/timeline.html
+++ b/brain/src/loopi_brain/web/templates/timeline.html
@@ -1,9 +1,10 @@
 {% extends "base.html" %}
 {% block content %}
 <nav class="days">
-  <a href="/?day={{ prev_day }}">← {{ prev_day }}</a>
+  <a href="/hours?day={{ prev_day }}">← {{ prev_day }}</a>
   <strong>{{ day }}</strong>
-  <a href="/?day={{ next_day }}">{{ next_day }} →</a>
+  <a href="/hours?day={{ next_day }}">{{ next_day }} →</a>
+  <a href="/?day={{ day }}">Theo chương</a>
 </nav>
 {% if not hours %}<p>Không có dữ liệu cho ngày này.</p>{% endif %}
 {% for hour, h in hours.items() %}
@@ -13,7 +14,7 @@
   <div class="frames">
   {% for kf in h.keyframes %}
     <figure id="kf-{{ kf.id }}">
-      <img src="/media/{{ kf.path }}" loading="lazy" alt="{{ kf.caption.scene if kf.caption else 'Khung hình' }}">
+      <a href="/media/{{ kf.path }}"><img src="/thumb/{{ kf.id }}.jpg" loading="lazy" alt="{{ kf.caption.scene if kf.caption else 'Khung hình' }}"></a>
       <figcaption><time>{{ kf.local_ts.strftime("%H:%M:%S") }}</time>
         {% if kf.caption %}{{ kf.caption.scene }} — {{ kf.caption.activity }}
         {% elif kf.caption_failed %}<span class="err">Lỗi mô tả</span>
@@ -34,7 +35,8 @@
     {% for it in h.segments %}
       <li class="item {{ it.kind }}" id="item-{{ it.id }}">
         <time>{{ it.local_start.strftime("%H:%M:%S") }}–{{ it.local_end.strftime("%H:%M:%S") }}</time>
-        {% if it.kind == "video" %}
+        {% if it.raw_deleted_at %}<span class="muted">Đã hết hạn</span>
+        {% elif it.kind == "video" %}
           {% if it.status == "ready" %}<video controls preload="none" src="/media/{{ it.view_path }}"></video>
           {% elif it.status == "error" %}<span class="err">Không chuyển được video</span>
           {% else %}<span>Đang xử lý…</span>{% endif %}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_markdown.py tests/test_day_page.py -q`
Expected: `14 passed`.

Run: `cd brain && uv run pytest tests/test_web.py tests/test_web_pipeline.py tests/test_audio_playback.py -q`
Expected: `40 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `419 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/web/day_routes.py brain/src/loopi_brain/web/markdown.py brain/src/loopi_brain/thumbs.py \
  brain/src/loopi_brain/media.py brain/src/loopi_brain/web/routes.py brain/src/loopi_brain/app.py \
  brain/src/loopi_brain/web/templates/day.html brain/src/loopi_brain/web/templates/_commitment.html \
  brain/src/loopi_brain/web/templates/base.html brain/src/loopi_brain/web/templates/timeline.html \
  brain/tests/test_day_page.py brain/tests/test_markdown.py brain/tests/test_web.py brain/tests/test_web_pipeline.py \
  brain/tests/test_audio_playback.py
git commit -F - <<'EOF'
feat(brain): Day page by chapters with recap, commitments and keyframe thumbnails

The P2a hourly view moves to /hours ("Chi tiết theo giờ") and shows thumbnails (P2a deferred M8).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Tasks 1–6 are shippable on their own** (chapters with the catch-up of old days, recap, commitments, Day page). The final whole-branch review may be run at this point first if the owner wants the first half deployed early; such a deploy also needs Task 13's `LOOPI_WORKER_CONCURRENCY: "3"` and Task 14's gates (I7) and checks.

---

### Task 7: Hybrid search and the plain search page

**Files:**
- Create: `brain/src/loopi_brain/search.py`, `brain/src/loopi_brain/web/search_routes.py`
- Create: `brain/src/loopi_brain/web/templates/search.html`, `brain/src/loopi_brain/web/templates/_hit.html`
- Modify: `brain/src/loopi_brain/config.py` (search thresholds), `brain/src/loopi_brain/app.py` (an http client for the models, the router), `brain/src/loopi_brain/web/templates/base.html` (nav, styles)
- Test: `brain/tests/test_search.py`

**Interfaces:**
- Consumes: Task 1 `day_bounds_utc`; Task 6 `TEMPLATES`, `MARKER_LABEL`, `/thumb/…`, the `#m-<id>` anchors; P2a `Router.embed`, `build_router`, `load_providers`, the `moment` table with its HNSW and trigram indexes.
- Produces:
  - `Settings.search_max_distance: float = 0.6` (`LOOPI_SEARCH_MAX_DISTANCE`), `Settings.search_min_similarity: float = 0.3` (`LOOPI_SEARCH_MIN_SIMILARITY`).
  - `search.Hit(id, kind, ts_start, text, lang, keyframe_id, item_id, item_meta, distance, similarity, score)`, `search.SearchResult(hits: list[Hit], semantic: bool)`.
  - `search.search_moments(pool, router: Router | None, query, *, start=None, end=None, limit=8, max_distance=0.6, min_similarity=0.3) -> SearchResult` — `semantic` is False when the question could not be embedded (words only).
  - `search.request_router(settings, client) -> Router | None` (None when `providers.yaml` is invalid); `search.QUERY_INSTRUCTION` (Qwen3-Embedding query prefix).
  - `create_app(settings, pool=None, http_client=None, llm_client=None)`; `app.state.llm` (300 s timeout) carries embeddings and answers; tests pass `llm_client=FakeOllama().client()`.
  - `GET /search?q=…&from=YYYY-MM-DD&to=YYYY-MM-DD`; `web.search_routes.hit_view(hit, zone) -> dict` (`time`, `kind_label`, `text`, `keyframe_id`, `link` = `/?day=…#m-<id>`), `range_utc(first, last, zone)`, `KIND_LABELS`.

Retrieval: the question (whitespace collapsed, ≤ 500 characters) is embedded with the query instruction; up to 40 moments by cosine distance (≤ `max_distance`) and up to 40 by `word_similarity` (≥ `min_similarity`, the `<%` operator so the trigram index is used), both inside the time range, are merged by reciprocal rank fusion (k = 60). A moment found both ways ranks first.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_search.py`:

```python
import datetime as dt

import httpx
import pytest

from helpers import FakeOllama, insert_item, insert_keyframe, insert_moment, paired_device
from loopi_brain import search
from loopi_brain.app import create_app
from loopi_brain.config import Settings
from loopi_brain.providers.config import DEFAULT_CONFIG
from loopi_brain.providers.router import build_router

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
# FakeOllama embeds the question as the unit vector on axis 0: moments on axis 0 mean the same thing.


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    return FakeOllama()


@pytest.fixture
def router(pki_settings, ollama):
    return build_router(DEFAULT_CONFIG, pki_settings, ollama.client())


@pytest.fixture
async def web(pool, pki_settings, jobs_open, ollama):
    app = create_app(pki_settings, pool=pool, http_client=ollama.client(), llm_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _memories(pool, device_id) -> dict:
    return {
        "kitchen": await insert_moment(pool, device_id, kind="visual", ts=NINE, axis=0, lang="en",
                                       text="Two people cook noodles in the kitchen"),
        "market": await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=5), axis=1,
                                      lang="vi", text="Mai mình đi chợ mua rau nhé"),
        "both": await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=9), axis=0,
                                    lang="vi", text="Nấu mì xong rồi đi chợ"),
        "other": await insert_moment(pool, device_id, kind="visual", ts=NINE + dt.timedelta(days=1), axis=2,
                                     lang="en", text="An empty room at night"),
    }


async def test_meaning_and_words_are_both_searched_and_merged(pool, dev, router, ollama):
    ids = await _memories(pool, dev["device_id"])
    result = await search.search_moments(pool, router, "đi chợ")
    assert result.semantic is True
    assert [h.id for h in result.hits] == [ids["both"], ids["kitchen"], ids["market"]]  # found twice ranks first
    both, kitchen, market = result.hits
    assert both.distance == pytest.approx(0.0) and both.similarity == pytest.approx(1.0)
    assert kitchen.similarity is None and market.distance is None
    [(_, body)] = [r for r in ollama.requests if r[0] == "/api/embed"]
    assert body["input"] == [search.QUERY_INSTRUCTION + "đi chợ"] and body["model"] == "qwen3-embedding:0.6b"


async def test_a_time_range_limits_both_searches(pool, dev, router):
    ids = await _memories(pool, dev["device_id"])
    tomorrow = NINE + dt.timedelta(days=1)
    result = await search.search_moments(pool, router, "đi chợ", start=tomorrow - dt.timedelta(hours=9))
    assert result.hits == []
    result = await search.search_moments(pool, router, "empty room", start=tomorrow - dt.timedelta(hours=9))
    assert [h.id for h in result.hits] == [ids["other"]]
    result = await search.search_moments(pool, router, "đi chợ", end=NINE + dt.timedelta(minutes=6))
    assert [h.id for h in result.hits] == [ids["kitchen"], ids["market"]]


async def test_without_the_embedding_model_words_still_find_moments(pool, dev, router, ollama):
    ids = await _memories(pool, dev["device_id"])
    ollama.down = True
    result = await search.search_moments(pool, router, "đi chợ")
    assert result.semantic is False and [h.id for h in result.hits] == [ids["both"], ids["market"]]
    result = await search.search_moments(pool, None, "đi chợ")  # providers.yaml unusable
    assert result.semantic is False and len(result.hits) == 2


async def test_nothing_relevant_finds_nothing(pool, dev, router):
    await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, axis=3, text="A cat sleeps on the sofa")
    assert (await search.search_moments(pool, router, "khám răng")).hits == []


async def test_an_empty_question_searches_nothing(pool, dev, router, ollama):
    await _memories(pool, dev["device_id"])
    assert (await search.search_moments(pool, router, "   \n ")).hits == [] and ollama.requests == []


async def test_moments_without_an_embedding_are_found_by_their_words(pool, dev, router):
    waiting = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, lang="vi",
                                  text="Chiều nay đón con lúc năm giờ")
    assert [h.id for h in (await search.search_moments(pool, router, "đón con")).hits] == [waiting]


async def test_thresholds_decide_what_is_relevant(pool, dev, router):
    ids = await _memories(pool, dev["device_id"])
    result = await search.search_moments(pool, router, "đi chợ", max_distance=2.0, min_similarity=0.99)
    assert {h.id for h in result.hits} == set(ids.values())  # every embedded moment is close enough at 2
    result = await search.search_moments(pool, router, "đi chợ", limit=1)
    assert [h.id for h in result.hits] == [ids["both"]]


async def test_the_search_page_lists_time_kind_thumbnail_and_a_link_to_the_day(web, pool, pki_settings, dev):
    device_id = dev["device_id"]
    video = await insert_item(pool, device_id, kind="video", started_at=NINE)
    kf = await insert_keyframe(pool, pki_settings, video, device_id, NINE)
    moment = await insert_moment(pool, device_id, kind="visual", ts=NINE, axis=0, lang="en",
                                 text="A <b>birthday</b> cake on the table")
    async with pool.connection() as conn:
        await conn.execute("UPDATE moment SET keyframe_id = %s WHERE id = %s", (kf, moment))
    text = (await web.get("/search", params={"q": "sinh nhật"})).text
    assert '<a href="/search">Tìm kiếm</a>' in text and 'value="sinh nhật"' in text
    assert f'<a href="/?day=2026-10-14#m-{moment}"><img src="/thumb/{kf}.jpg"' in text
    assert "<time>14/10/2026 09:00</time></a> · Hình ảnh" in text
    assert "A &lt;b&gt;birthday&lt;/b&gt; cake on the table" in text


async def test_the_search_page_says_when_nothing_is_found_and_keeps_the_dates(web, pool, dev):
    await _memories(pool, dev["device_id"])
    text = (await web.get("/search", params={"q": "đi chợ", "from": "2026-10-15", "to": "2026-10-15"})).text
    assert "Không tìm thấy khoảnh khắc nào." in text
    assert 'name="from" value="2026-10-15"' in text and 'name="to" value="2026-10-15"' in text
    text = (await web.get("/search", params={"q": "đi chợ", "from": "2026-10-14", "to": "2026-10-14"})).text
    assert text.count('class="hit"') == 3
    assert (await web.get("/search", params={"q": "x", "from": "hôm qua"})).status_code == 422


async def test_the_search_page_without_a_question_shows_the_form(web, ollama):
    text = (await web.get("/search")).text
    assert 'name="q"' in text and "Không tìm thấy khoảnh khắc" not in text and ollama.requests == []


async def test_a_stopped_model_server_is_explained(web, pool, dev, ollama):
    await _memories(pool, dev["device_id"])
    ollama.down = True
    text = (await web.get("/search", params={"q": "đi chợ"})).text
    assert "Đang chỉ tìm theo chữ" in text and text.count('class="hit"') == 2


def test_search_thresholds_are_settings(monkeypatch):
    s = Settings(database_url="postgresql://x")
    assert (s.search_max_distance, s.search_min_similarity) == (0.6, 0.3)
    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("LOOPI_SEARCH_MAX_DISTANCE", "0.45")
    assert Settings().search_max_distance == 0.45
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_search.py -q`
Expected: FAIL — `ImportError: cannot import name 'search' from 'loopi_brain'`.

- [ ] **Step 3: Add the thresholds and the models' http client**

Apply to `brain/src/loopi_brain/config.py` and `brain/src/loopi_brain/app.py`:

```diff
diff --git a/brain/src/loopi_brain/config.py b/brain/src/loopi_brain/config.py
--- a/brain/src/loopi_brain/config.py
+++ b/brain/src/loopi_brain/config.py
@@ -38,6 +38,10 @@ class Settings(BaseSettings):
     ollama_text_keep_alive: str = "1m"
     llm_num_ctx: int = Field(default=8192, ge=2048, le=131072)
     llm_timeout_s: float = Field(default=900.0, ge=60.0, le=3600.0)
+    # Search (spec §10.4): a moment is relevant when its embedding is this close to the question's (cosine
+    # distance), or when its text matches the question's words this well (pg_trgm word similarity). P2c tunes both.
+    search_max_distance: float = Field(default=0.6, gt=0.0, le=2.0)
+    search_min_similarity: float = Field(default=0.3, gt=0.0, le=1.0)
 
     @property
     def zone(self) -> ZoneInfo:
```

```diff
diff --git a/brain/src/loopi_brain/app.py b/brain/src/loopi_brain/app.py
--- a/brain/src/loopi_brain/app.py
+++ b/brain/src/loopi_brain/app.py
@@ -14,8 +14,10 @@ from .api.device_routes import router as device_router
 from .config import Settings
 from .db import make_pool
 from .pipeline.jobs import open_jobs
+from .providers.ollama import DEFAULT_TIMEOUT
 from .web.day_routes import router as day_router
 from .web.routes import router as web_router
+from .web.search_routes import router as search_router
 
 log = logging.getLogger(__name__)
 
@@ -28,11 +30,16 @@ def create_app(
     settings: Settings,
     pool: AsyncConnectionPool | None = None,
     http_client: httpx.AsyncClient | None = None,
+    llm_client: httpx.AsyncClient | None = None,
 ) -> FastAPI:
+    """``http_client`` asks the model server for its health (short timeout); ``llm_client`` carries search
+    embeddings and chat answers (CPU inference: long timeout)."""
     owns_pool = pool is None
     active_pool = pool if pool is not None else make_pool(settings.database_url)
     owns_http = http_client is None
     http = http_client if http_client is not None else httpx.AsyncClient(timeout=STATUS_HTTP_TIMEOUT)
+    owns_llm = llm_client is None
+    llm = llm_client if llm_client is not None else httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
 
     @asynccontextmanager
     async def lifespan(_app: FastAPI):
@@ -54,6 +61,8 @@ def create_app(
         finally:
             if owns_http:
                 await http.aclose()
+            if owns_llm:
+                await llm.aclose()
             if owns_pool:
                 await active_pool.close()
 
@@ -61,6 +70,7 @@ def create_app(
     app.state.settings = settings
     app.state.pool = active_pool
     app.state.http = http
+    app.state.llm = llm
 
     @app.get("/healthz", include_in_schema=False)
     async def healthz() -> dict[str, str]:
@@ -69,6 +79,7 @@ def create_app(
     app.include_router(device_router)
     app.include_router(web_router)
     app.include_router(day_router)
+    app.include_router(search_router)
     settings.media_root.mkdir(parents=True, exist_ok=True)
     app.mount("/media", StaticFiles(directory=settings.media_root), name="media")
     app.mount(
```

- [ ] **Step 4: Write the retrieval**

Create `brain/src/loopi_brain/search.py`:

```python
"""Hybrid retrieval over moments (spec §10.4 step 2).

Two searches, each limited to the time filter: pgvector cosine distance between the question's embedding and
the moments' embeddings (meaning, across languages: a Vietnamese question finds an English caption), and
pg_trgm word similarity between the question and the moments' text (exact words, names, moments without an
embedding yet). Each keeps only results within its threshold; the two lists are merged by reciprocal rank
fusion. No result means nothing relevant: the caller says "không tìm thấy" instead of guessing.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import httpx
from pgvector import Vector
from psycopg_pool import AsyncConnectionPool

from .config import Settings
from .providers.base import ProviderError
from .providers.config import ProvidersConfigError, load_providers
from .providers.router import Router, build_router

log = logging.getLogger(__name__)
# Qwen3-Embedding: queries carry an instruction, documents do not (P2a embeds moments without one).
QUERY_INSTRUCTION = "Instruct: Given a question about a personal diary, retrieve the moments that answer it\nQuery: "
CANDIDATES = 40  # per search, before the merge
RRF_K = 60
QUERY_MAX = 500  # characters of a question that are searched
_COLUMNS = (
    "m.id, m.kind, m.ts_start, m.text, m.lang, m.keyframe_id, m.item_id, i.meta AS item_meta"
)
_FILTER = "(%(start)s::timestamptz IS NULL OR m.ts_start >= %(start)s) AND (%(end)s::timestamptz IS NULL OR m.ts_start < %(end)s)"


@dataclass(frozen=True)
class Hit:
    id: str
    kind: str
    ts_start: dt.datetime
    text: str
    lang: str | None
    keyframe_id: str | None
    item_id: str | None
    item_meta: dict | None
    distance: float | None  # cosine distance to the question (None: found by words only)
    similarity: float | None  # word similarity (None: found by meaning only)
    score: float


@dataclass(frozen=True)
class SearchResult:
    hits: list[Hit]
    semantic: bool  # False when the question could not be embedded (model server down): words only


def request_router(settings: Settings, client: httpx.AsyncClient) -> Router | None:
    """A router for the api process (search, chat); None if providers.yaml is invalid."""
    try:
        return build_router(load_providers(settings.providers_file), settings, client)
    except ProvidersConfigError as exc:
        log.warning("search and chat run without models: %s", exc)
        return None


async def _by_meaning(pool, router: Router, query: str, params: dict, max_distance: float) -> list[dict] | None:
    try:
        result = await router.embed("embed", [QUERY_INSTRUCTION + query])
    except ProviderError as exc:
        log.warning("searching by words only: the question could not be embedded: %s", exc)
        return None
    async with pool.connection() as conn, conn.transaction():
        # With a time filter, keep scanning the HNSW index until enough rows pass the filter (pgvector >= 0.8).
        await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        cur = await conn.execute(
            f"SELECT {_COLUMNS}, (m.embedding <=> %(vector)s)::float8 AS distance FROM moment m"
            f" LEFT JOIN item i ON i.id = m.item_id WHERE m.embedding IS NOT NULL AND {_FILTER}"
            " ORDER BY m.embedding <=> %(vector)s LIMIT %(limit)s",
            {**params, "vector": Vector(result.vectors[0])},
        )
        rows = await cur.fetchall()
    return sorted((r for r in rows if r["distance"] <= max_distance), key=lambda r: (r["distance"], r["id"]))


async def _by_words(pool, query: str, params: dict, min_similarity: float) -> list[dict]:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)", (str(min_similarity),))
        cur = await conn.execute(
            f"SELECT {_COLUMNS}, word_similarity(%(query)s, m.text)::float8 AS similarity FROM moment m"
            f" LEFT JOIN item i ON i.id = m.item_id WHERE %(query)s <%% m.text AND {_FILTER}"
            " ORDER BY similarity DESC, m.ts_start DESC, m.id LIMIT %(limit)s",
            {**params, "query": query},
        )
        return await cur.fetchall()


async def search_moments(
    pool: AsyncConnectionPool,
    router: Router | None,
    query: str,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    limit: int = 8,
    max_distance: float = 0.6,
    min_similarity: float = 0.3,
) -> SearchResult:
    """The ``limit`` most relevant moments for ``query`` with ts_start in [start, end) (either bound optional)."""
    query = " ".join(query.split())[:QUERY_MAX]
    if not query:
        return SearchResult([], semantic=router is not None)
    params = {"start": start, "end": end, "limit": CANDIDATES}
    meaning = await _by_meaning(pool, router, query, params, max_distance) if router is not None else None
    words = await _by_words(pool, query, params, min_similarity)
    rows: dict[str, dict] = {}
    scores: dict[str, float] = {}
    for found in (meaning or [], words):
        for rank, row in enumerate(found, start=1):
            rows.setdefault(row["id"], {}).update(row)
            scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (RRF_K + rank)
    ordered = sorted(scores, key=lambda moment_id: (-scores[moment_id], rows[moment_id]["ts_start"], moment_id))
    hits = [
        Hit(
            id=moment_id, kind=row["kind"], ts_start=row["ts_start"], text=row["text"], lang=row["lang"],
            keyframe_id=row["keyframe_id"], item_id=row["item_id"], item_meta=row["item_meta"],
            distance=row.get("distance"), similarity=row.get("similarity"), score=scores[moment_id],
        )
        for moment_id in ordered[:limit]
        for row in (rows[moment_id],)
    ]
    return SearchResult(hits, semantic=meaning is not None)
```

- [ ] **Step 5: Write the search page**

Create `brain/src/loopi_brain/web/search_routes.py`:

```python
"""The "Tìm kiếm & Chat" page (spec §10.4, §10.6): plain hybrid search here; cited chat answers in Task 8."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import search
from ..days import day_bounds_utc
from ..pipeline import moments as moment_rules
from .routes import MARKER_LABEL, TEMPLATES

KIND_LABELS = {"visual": "Hình ảnh", "speech": "Lời nói", "marker": "Đánh dấu"}
SEARCH_LIMIT = 30

router = APIRouter()


def hit_view(hit: search.Hit, zone) -> dict:
    """A moment as a search result or a chat source: time, kind, text, thumbnail, link to its Day page."""
    local = hit.ts_start.astimezone(zone)
    text = hit.text
    if hit.kind == "marker":
        text = moment_rules.marker_note(hit.item_meta or {}) or MARKER_LABEL
    return {
        "id": hit.id,
        "kind": hit.kind,
        "kind_label": KIND_LABELS.get(hit.kind, hit.kind),
        "time": local.strftime("%d/%m/%Y %H:%M"),
        "text": text,
        "lang": hit.lang,
        "keyframe_id": hit.keyframe_id,
        "link": f"/?day={local.date().isoformat()}#m-{hit.id}",
    }


def range_utc(first: dt.date | None, last: dt.date | None, zone) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Local days [first, last] (either optional) as a UTC range [start, end)."""
    start = day_bounds_utc(first, zone)[0] if first else None
    end = day_bounds_utc(last, zone)[1] if last else None
    return start, end


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = Query(default="", max_length=2000),
    first: dt.date | None = Query(default=None, alias="from"),
    last: dt.date | None = Query(default=None, alias="to"),
):
    state = request.app.state
    zone = state.settings.zone
    result = None
    if q.strip():
        start, end = range_utc(first, last, zone)
        result = await search.search_moments(
            state.pool, search.request_router(state.settings, state.llm), q, start=start, end=end,
            limit=SEARCH_LIMIT, max_distance=state.settings.search_max_distance,
            min_similarity=state.settings.search_min_similarity,
        )
    return TEMPLATES.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "first": first,
            "last": last,
            "hits": [hit_view(hit, zone) for hit in result.hits] if result else None,
            "semantic": result.semantic if result else True,
        },
    )
```

Create `brain/src/loopi_brain/web/templates/_hit.html`:

```html
<li class="hit" id="hit-{{ h.id }}">
  {% if h.keyframe_id %}<a href="{{ h.link }}"><img src="/thumb/{{ h.keyframe_id }}.jpg" loading="lazy" alt="" width="160"></a>{% endif %}
  <div><a href="{{ h.link }}"><time>{{ h.time }}</time></a> · {{ h.kind_label }}{% if h.lang and h.kind == "speech" %} <span class="lang">{{ h.lang|upper }}</span>{% endif %}
    <p>{{ h.text }}</p></div>
</li>
```

Create `brain/src/loopi_brain/web/templates/search.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Tìm kiếm & Chat</h1>
<form method="get" action="/search" class="card">
  <input type="search" name="q" value="{{ q }}" placeholder="Tìm trong ký ức…" required maxlength="500" size="40">
  <label>Từ ngày <input type="date" name="from" value="{{ first or '' }}"></label>
  <label>đến ngày <input type="date" name="to" value="{{ last or '' }}"></label>
  <button>Tìm</button>
</form>
{% if hits is not none %}
  {% if not semantic %}<p class="muted">Đang chỉ tìm theo chữ: máy chủ model chưa sẵn sàng nên chưa tìm được theo nghĩa.</p>{% endif %}
  {% if hits %}
  <ul class="hits">{% for h in hits %}{% include "_hit.html" %}{% endfor %}</ul>
  {% else %}<p>Không tìm thấy khoảnh khắc nào.</p>{% endif %}
{% endif %}
{% endblock %}
```

Apply to `brain/src/loopi_brain/web/templates/base.html`:

```diff
diff --git a/brain/src/loopi_brain/web/templates/base.html b/brain/src/loopi_brain/web/templates/base.html
--- a/brain/src/loopi_brain/web/templates/base.html
+++ b/brain/src/loopi_brain/web/templates/base.html
@@ -20,6 +20,8 @@
     .commitment { display: flex; gap: 8px; align-items: center; padding: 4px 0; }
     .commitment.done span:first-child, .commitment.dismissed span:first-child { text-decoration: line-through; color: #888; }
     form.inline { display: inline; }
+    .hits { padding: 0; } .hit { display: flex; gap: 12px; list-style: none; padding: 6px 0; border-bottom: 1px solid #8884; }
+    .hit p { margin: 4px 0; }
     .frames figcaption, .muted { font-size: 0.85rem; color: #888; }
     .lang { font-size: 0.75rem; border: 1px solid #8886; border-radius: 4px; padding: 0 4px; }
     table { border-collapse: collapse; width: 100%; }
@@ -31,7 +33,7 @@
   </style>
 </head>
 <body>
-  <header><nav><a href="/">Hôm nay</a><a href="/pipeline">Xử lý</a><a href="/devices">Thiết bị</a></nav></header>
+  <header><nav><a href="/">Hôm nay</a><a href="/search">Tìm kiếm</a><a href="/pipeline">Xử lý</a><a href="/devices">Thiết bị</a></nav></header>
   <div id="hx-error" class="err" role="alert"></div>
   <main>{% block content %}{% endblock %}</main>
   <script>
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_search.py -q`
Expected: `12 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `431 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/search.py brain/src/loopi_brain/web/search_routes.py brain/src/loopi_brain/config.py \
  brain/src/loopi_brain/app.py brain/src/loopi_brain/web/templates/search.html brain/src/loopi_brain/web/templates/_hit.html \
  brain/src/loopi_brain/web/templates/base.html brain/tests/test_search.py
git commit -F - <<'EOF'
feat(brain): hybrid search over moments by meaning and by words (spec 10.4)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 8: Cited answers streamed over Server-Sent Events

**Files:**
- Create: `brain/src/loopi_brain/chat.py`, `brain/src/loopi_brain/web/templates/_sources.html`
- Modify: `brain/src/loopi_brain/providers/base.py` (`stream_chat` in the protocol), `brain/src/loopi_brain/providers/ollama.py` (`stream_chat`), `brain/src/loopi_brain/providers/router.py` (`stream_chat` with fallback), `brain/src/loopi_brain/web/search_routes.py` (`POST /chat`), `brain/src/loopi_brain/web/templates/search.html` (chat box + script)
- Modify: `brain/tests/helpers.py` (`FakeOllama` streams)
- Test: `brain/tests/test_chat.py`

**Interfaces:**
- Consumes: Task 1 `day_bounds_utc`, `local_now`; Task 2 `ASR_CAVEAT`, `PICTURE_NOTE`, `AnswerInvalid`, `clip`, `parse_answer`; Task 7 `search_moments`, `Hit`, `request_router`, `hit_view`, `_hit.html`, `app.state.llm`; Task 6 `require_htmx`, `TEMPLATES`.
- Produces:
  - `OllamaProvider.stream_chat(model, messages, *, temperature=0.0, max_tokens=None) -> AsyncIterator[str]` (NDJSON; a text model waits up to Task 2's `text_timeout`; connection refused / 404 → `ProviderUnavailable`, broken stream → `ProviderError`); the `Provider` protocol declares it (Task 12's Anthropic provider implements it).
  - `Router.stream_chat(task, messages, temperature=0.0, max_tokens=None) -> AsyncIterator[tuple[str, str]]` — `(piece, model_version)`; falls back to the next route only before the first piece.
  - `chat.chat_events(pool, router, question, settings, now=None) -> AsyncIterator[tuple[str, dict]]` — events `status {text}`, `filter {text}`, `sources {hits: list[Hit]}`, `token {text}`, `answer {text, not_found}`, `error {text}`, `done {}`.
  - `chat.time_filter(router, question, today) -> tuple[date | None, date | None]`, `chat.clean_filter(first, last, today)`, `chat.cited(text, count) -> list[int]`, `chat.mentions_time(question) -> bool`, `chat.TIME_WORDS`, `chat.CITATION`, `chat.NOT_FOUND = "Không tìm thấy trong ký ức."`, `chat.SOURCES = 8`, `chat.ANSWER_MAX_TOKENS = 600`, `chat.FILTER_SCHEMA`.
  - `POST /chat` (form `q`, `HX-Request: true`) → `text/event-stream`; each event is `event: <name>\ndata: <JSON>\n\n`; `sources` and `answer` carry server-escaped HTML (`{"html": …}`), the others text. `web.search_routes.render_answer(text, sources) -> Markup`, `sse(event, payload) -> str`.
  - `helpers.FakeOllama`: a chat with `"stream": true` is answered in 8-character NDJSON pieces; `stream_fail_after = n` breaks the stream with an error line after n pieces.

The steps of one answer: "Đang hiểu câu hỏi…" → if the question has a time word (`chat.mentions_time`: Vietnamese, English or Korean time words, or a date such as 14/10), the chat model reads the dates it is about (JSON `{"from", "to"}`, YYYY-MM-DD or empty; cleaned: swapped ends put in order, one end copies the other, the future is cut, ≤ 366 days; anything unusable → no filter) — without a time word that `qwen3:8b` call is skipped → "Đang tìm trong ký ức…" → hybrid search (≤ 8 sources) → none: "Không tìm thấy trong ký ức." and **no answer is asked of the model** → else the sources are shown, then the answer streams; it cites `[n]`; the final HTML links each valid `[n]` to `/?day=…#m-<id>`, drops numbers that are not sources, and warns when nothing is cited — unless the model itself answered "Không tìm thấy trong ký ức.", which is shown as a plain not-found answer.

- [ ] **Step 1: Write the failing tests**

Apply to `brain/tests/helpers.py`:

```diff
diff --git a/brain/tests/helpers.py b/brain/tests/helpers.py
--- a/brain/tests/helpers.py
+++ b/brain/tests/helpers.py
@@ -113,7 +113,8 @@ class FakeOllama:
     Set ``down = True`` to simulate a stopped model server (every request raises ConnectError), or
     ``error = 500`` to make chat and embed requests fail with that HTTP status. ``reply``, if set, is called
     with each /api/chat request body and returns the answer (a str, or a dict sent as JSON); by default
-    every chat answers with ``caption``.
+    every chat answers with ``caption``. A chat with ``stream: true`` gets the answer in pieces of 8 characters,
+    one JSON line each; ``stream_fail_after = n`` breaks such a stream with an error line after n pieces.
     """
 
     def __init__(self, caption: dict | str | None = None, models: list[str] | None = None, dims: int = 1024):
@@ -123,6 +124,7 @@ class FakeOllama:
         self.models = list(DEFAULT_OLLAMA_MODELS if models is None else models)
         self.dims = dims
         self.reply = None
+        self.stream_fail_after: int | None = None
         self.requests: list[tuple[str, dict]] = []
         self.timeouts: list[tuple[str, dict | None]] = []
 
@@ -145,6 +147,8 @@ class FakeOllama:
         if path == "/api/chat":
             answer = self.caption if self.reply is None else self.reply(body)
             content = answer if isinstance(answer, str) else json.dumps(answer)
+            if body.get("stream"):
+                return httpx.Response(200, content=self._stream(body["model"], content))
             return httpx.Response(
                 200, json={"model": body["model"], "message": {"role": "assistant", "content": content}, "done": True}
             )
@@ -162,6 +166,20 @@ class FakeOllama:
             return httpx.Response(200, json={"status": "success"})
         return httpx.Response(404, json={"error": "not found"})
 
+    def _stream(self, model: str, content: str) -> bytes:
+        import json
+
+        pieces = [content[i:i + 8] for i in range(0, len(content), 8)]
+        lines = []
+        for n, piece in enumerate(pieces):
+            if self.stream_fail_after is not None and n == self.stream_fail_after:
+                lines.append({"error": "model runner has unexpectedly stopped"})
+                break
+            lines.append({"model": model, "message": {"role": "assistant", "content": piece}, "done": False})
+        else:
+            lines.append({"model": model, "message": {"role": "assistant", "content": ""}, "done": True})
+        return "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines).encode()
+
     def client(self):
         import httpx
 
```

Create `brain/tests/test_chat.py`:

```python
import datetime as dt
import json

import httpx
import pytest

from helpers import FakeOllama, insert_moment, paired_device
from loopi_brain import chat
from loopi_brain.app import create_app
from loopi_brain.llm import ASR_CAVEAT
from loopi_brain.providers.base import ProviderError, ProviderUnavailable
from loopi_brain.providers.config import DEFAULT_CONFIG, ProvidersConfig
from loopi_brain.providers.ollama import OllamaProvider
from loopi_brain.providers.router import build_router

HX = {"HX-Request": "true"}
TODAY = dt.date(2026, 10, 14)
NOW = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.UTC)  # 21:00 in Seoul, a Wednesday
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)
ANSWER = "Sáng mai bạn đi khám răng lúc 9 giờ [1]. Trưa nay có nấu mì [2][9]."


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    fake = FakeOllama()
    fake.reply = lambda body: {"from": "", "to": ""} if "format" in body else ANSWER
    return fake


@pytest.fixture
def router(pki_settings, ollama):
    return build_router(DEFAULT_CONFIG, pki_settings, ollama.client())


@pytest.fixture
async def web(pool, pki_settings, jobs_open, ollama):
    app = create_app(pki_settings, pool=pool, http_client=ollama.client(), llm_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _memories(pool, device_id) -> dict:
    return {
        "dentist": await insert_moment(pool, device_id, kind="speech", ts=NINE, axis=0, lang="vi",
                                       text="Mai 9 giờ mình đi khám răng <nhé>"),
        "noodles": await insert_moment(pool, device_id, kind="visual", ts=NINE + dt.timedelta(hours=3), axis=0,
                                       lang="en", text="Two people cook noodles"),
    }


async def _events(ctx_gen) -> list[tuple[str, dict]]:
    return [event async for event in ctx_gen]


@pytest.mark.parametrize(("answer", "expected"), [
    ({"from": "2026-10-13", "to": "2026-10-13"}, (TODAY - dt.timedelta(days=1), TODAY - dt.timedelta(days=1))),
    ({"from": "2026-10-12", "to": ""}, (dt.date(2026, 10, 12), dt.date(2026, 10, 12))),
    ({"from": "2026-10-14", "to": "2026-10-05"}, (dt.date(2026, 10, 5), TODAY)),  # put in order
    ({"from": "2026-10-10", "to": "2026-12-31"}, (dt.date(2026, 10, 10), TODAY)),  # the future is cut off
    ({"from": "2027-01-01", "to": "2027-01-02"}, (None, None)),  # all in the future: no filter
    ({"from": "2020-01-01", "to": "2026-10-14"}, (TODAY - dt.timedelta(days=365), TODAY)),  # at most 366 days
    ({"from": "", "to": ""}, (None, None)),
    ({"from": "hôm qua", "to": "13/10"}, (None, None)),
    ("not json at all", (None, None)),
])
async def test_the_time_filter_is_read_by_the_model_and_cleaned(router, ollama, answer, expected):
    ollama.reply = lambda body: answer
    assert await chat.time_filter(router, "Hôm qua mình làm gì?", TODAY) == expected
    body = ollama.requests[-1][1]
    assert body["format"] == chat.FILTER_SCHEMA and body["model"] == "qwen3:8b"
    assert "Today is Wednesday, 2026-10-14." in body["messages"][0]["content"]


async def test_no_time_filter_when_the_model_server_is_down(router, ollama):
    ollama.down = True
    assert await chat.time_filter(router, "Hôm qua mình làm gì?", TODAY) == (None, None)


async def test_a_question_gets_sources_then_a_streamed_cited_answer(pool, dev, router, ollama, pki_settings):
    ids = await _memories(pool, dev["device_id"])
    events = await _events(chat.chat_events(pool, router, "Mai mình phải làm gì?", pki_settings, now=NOW))
    names = [name for name, _ in events]
    assert names[:4] == ["status", "status", "sources", "status"] and names[-2:] == ["answer", "done"]
    assert set(names[4:-2]) == {"token"} and len(names[4:-2]) > 1  # streamed in pieces
    assert [h.id for h in events[2][1]["hits"]] == [ids["dentist"], ids["noodles"]]
    assert "".join(p["text"] for n, p in events if n == "token") == ANSWER
    assert events[-2][1] == {"text": ANSWER, "not_found": False}
    stream = [(b, t) for (path, b), (_, t) in zip(ollama.requests, ollama.timeouts, strict=True)
              if path == "/api/chat" and b["stream"]]
    [(body, timeout)] = stream
    assert body["think"] is False and body["options"]["num_predict"] == chat.ANSWER_MAX_TOKENS
    assert timeout["read"] == 900.0  # a CPU answer may take minutes (LOOPI_LLM_TIMEOUT_S)
    system, user = body["messages"]
    assert "[2] or [1][3]" in system["content"] and chat.NOT_FOUND in system["content"]
    assert ASR_CAVEAT in system["content"]  # transcripts may hold invented lines
    assert user["content"].startswith("Sources:\n[1] 2026-10-14 09:00, speech (vi): Mai 9 giờ mình đi khám răng <nhé>")
    assert "[2] 2026-10-14 12:00, picture: Two people cook noodles" in user["content"]
    assert user["content"].endswith("Question: Mai mình phải làm gì?")


async def test_nothing_relevant_says_not_found_and_asks_no_model_for_an_answer(pool, dev, router, ollama,
                                                                               pki_settings):
    await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, axis=5, text="An empty room")
    events = await _events(chat.chat_events(pool, router, "Khi nào mình đi Busan?", pki_settings, now=NOW))
    assert events[-2:] == [("answer", {"text": chat.NOT_FOUND, "not_found": True}), ("done", {})]
    assert "sources" not in [n for n, _ in events]
    assert not any(b.get("stream") for path, b in ollama.requests if path == "/api/chat")


async def test_a_time_filter_is_announced_and_limits_the_sources(pool, dev, router, ollama, pki_settings):
    await _memories(pool, dev["device_id"])
    ollama.reply = lambda body: {"from": "2026-10-13", "to": "2026-10-13"} if "format" in body else ANSWER
    events = await _events(chat.chat_events(pool, router, "Hôm qua có gì?", pki_settings, now=NOW))
    assert ("filter", {"text": "Tìm trong ngày 13/10/2026"}) in events
    assert events[-2][1]["text"] == chat.NOT_FOUND  # the memories are from today


async def test_an_answer_that_breaks_off_is_reported_after_the_sources(pool, dev, router, ollama, pki_settings):
    await _memories(pool, dev["device_id"])
    ollama.stream_fail_after = 2
    events = await _events(chat.chat_events(pool, router, "Mai mình phải làm gì?", pki_settings, now=NOW))
    names = [n for n, _ in events]
    assert "sources" in names and names[-2:] == ["error", "done"] and "answer" not in names
    assert "máy chủ model" in events[-2][1]["text"]


@pytest.mark.parametrize(("question", "expected"), [
    ("Hôm qua mình làm gì?", True), ("Mai mình phải làm gì?", True), ("What did we cook last week?", True),
    ("어제 뭐 했어?", True), ("Chuyện ngày 14/10 là gì?", True),
    ("Chìa khoá để ở đâu?", False), ("Ai đã gọi điện cho bố?", False),
])
def test_only_questions_with_a_time_word_need_a_time_filter(question, expected):
    assert chat.mentions_time(question) is expected


async def test_a_question_without_a_time_word_asks_the_model_once(pool, dev, router, ollama, pki_settings):
    await _memories(pool, dev["device_id"])
    events = await _events(chat.chat_events(pool, router, "Chìa khoá để ở đâu?", pki_settings, now=NOW))
    assert events[-1] == ("done", {})
    chats = [b for path, b in ollama.requests if path == "/api/chat"]
    assert len(chats) == 1 and chats[0]["stream"] is True  # no time-filter call, only the answer


async def test_a_model_that_finds_nothing_is_shown_as_not_found(web, pool, dev, ollama):
    await _memories(pool, dev["device_id"])
    ollama.reply = lambda body: {"from": "", "to": ""} if "format" in body else chat.NOT_FOUND
    async with web.stream("POST", "/chat", data={"q": "Mai mình phải làm gì?"}, headers=HX) as r:
        events = _parse((await r.aread()).decode())
    assert events[-2] == ("answer", {"html": f"<p>{chat.NOT_FOUND}</p>"})  # no "không trích nguồn" warning


async def test_an_empty_question_is_not_sent_anywhere(pool, router, ollama, pki_settings):
    events = await _events(chat.chat_events(pool, router, "   ", pki_settings, now=NOW))
    assert events[0] == ("answer", {"text": "Hãy nhập một câu hỏi.", "not_found": True}) and ollama.requests == []


def test_only_existing_sources_count_as_citations():
    assert chat.cited("A [1]. B [3, 2][2] C [7] [x]", 3) == [1, 3, 2]
    assert chat.cited("nothing cited", 3) == []


async def test_ollama_streams_ndjson_and_reports_broken_streams():
    fake = FakeOllama(caption="Một câu trả lời khá dài")
    provider = OllamaProvider("http://models.test:11434", client=fake.client())
    pieces = [p async for p in provider.stream_chat("qwen3:8b", [{"role": "user", "content": "x"}])]
    assert "".join(pieces) == "Một câu trả lời khá dài" and len(pieces) == 3
    assert fake.requests[0][1]["stream"] is True
    fake.stream_fail_after = 1
    with pytest.raises(ProviderError, match="unexpectedly stopped"):
        _ = [p async for p in provider.stream_chat("qwen3:8b", [{"role": "user", "content": "x"}])]
    with pytest.raises(ProviderUnavailable, match="HTTP 404"):
        _ = [p async for p in provider.stream_chat("missing:1b", [{"role": "user", "content": "x"}])]
    fake.down = True
    with pytest.raises(ProviderUnavailable):
        _ = [p async for p in provider.stream_chat("qwen3:8b", [{"role": "user", "content": "x"}])]
    garbage = OllamaProvider("http://models.test:11434", client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>\n"))))
    with pytest.raises(ProviderError, match="not JSON"):
        _ = [p async for p in garbage.stream_chat("qwen3:8b", [{"role": "user", "content": "x"}])]


async def test_a_stream_falls_back_only_before_its_first_piece(pki_settings, ollama):
    tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
    tasks["chat"] = {"provider": "ollama", "model": "qwen3:8b", "fallback": {"provider": "ollama", "model": "qwen3:4b"}}
    router = build_router(ProvidersConfig.model_validate({"tasks": tasks}), pki_settings, ollama.client())
    ollama.models.remove("qwen3:8b")  # not pulled: the fallback answers
    pieces = [p async for p in router.stream_chat("chat", [{"role": "user", "content": "x"}])]
    assert {version for _, version in pieces} == {"ollama:qwen3:4b"}
    ollama.models.append("qwen3:8b")
    ollama.stream_fail_after = 1  # the primary breaks after it started: no fallback, the error goes up
    with pytest.raises(ProviderError):
        _ = [p async for p in router.stream_chat("chat", [{"role": "user", "content": "x"}])]
    ollama.down = True
    with pytest.raises(ProviderUnavailable):
        _ = [p async for p in router.stream_chat("chat", [{"role": "user", "content": "x"}])]


def _parse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


async def test_the_chat_endpoint_streams_server_sent_events(web, pool, dev):
    ids = await _memories(pool, dev["device_id"])
    assert (await web.post("/chat", data={"q": "Mai làm gì?"})).status_code == 403
    async with web.stream("POST", "/chat", data={"q": "Mai mình phải làm gì?"}, headers=HX) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = _parse((await r.aread()).decode())
    sources = dict(events)["sources"]["html"]
    assert sources.startswith('<ol class="hits">') and sources.count('class="hit"') == 2
    assert "Mai 9 giờ mình đi khám răng &lt;nhé&gt;" in sources and f"#m-{ids['dentist']}" in sources
    answer = events[-2][1]["html"]
    assert f'<a class="cite" href="/?day=2026-10-14#m-{ids["dentist"]}">[1]</a>' in answer
    assert "[9]" not in answer and "[2]</a>" in answer
    assert events[-1] == ("done", {})


async def test_the_chat_endpoint_flags_uncited_answers_and_not_found(web, pool, dev, ollama):
    await _memories(pool, dev["device_id"])
    ollama.reply = lambda body: {"from": "", "to": ""} if "format" in body else "Bạn đi khám răng <script>."
    async with web.stream("POST", "/chat", data={"q": "Mai mình phải làm gì?"}, headers=HX) as r:
        events = _parse((await r.aread()).decode())
    answer = events[-2][1]["html"]
    assert "&lt;script&gt;" in answer and "không trích nguồn nào" in answer
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM moment")  # nothing left to find
    async with web.stream("POST", "/chat", data={"q": "Mai mình phải làm gì?"}, headers=HX) as r:
        events = _parse((await r.aread()).decode())
    assert events[-2] == ("answer", {"html": f"<p>{chat.NOT_FOUND}</p>"})


async def test_the_search_page_carries_the_chat_box(web):
    text = (await web.get("/search")).text
    assert 'id="chat-form"' in text and 'fetch("/chat"' in text and '"HX-Request": "true"' in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_chat.py -q`
Expected: FAIL — `ImportError: cannot import name 'chat' from 'loopi_brain'`.

- [ ] **Step 3: Stream answers from Ollama, through the router**

Apply to `brain/src/loopi_brain/providers/base.py`, `brain/src/loopi_brain/providers/ollama.py` and `brain/src/loopi_brain/providers/router.py`:

```diff
diff --git a/brain/src/loopi_brain/providers/base.py b/brain/src/loopi_brain/providers/base.py
--- a/brain/src/loopi_brain/providers/base.py
+++ b/brain/src/loopi_brain/providers/base.py
@@ -1,6 +1,7 @@
 """Provider interface and the errors every provider raises."""
 from __future__ import annotations
 
+from collections.abc import AsyncIterator
 from typing import Protocol
 
 EMBED_DIM = 1024
@@ -34,3 +35,7 @@ class Provider(Protocol):
     ) -> str: ...
 
     async def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...
+
+    def stream_chat(
+        self, model: str, messages: list[dict], *, temperature: float, max_tokens: int | None
+    ) -> AsyncIterator[str]: ...
```

```diff
diff --git a/brain/src/loopi_brain/providers/ollama.py b/brain/src/loopi_brain/providers/ollama.py
--- a/brain/src/loopi_brain/providers/ollama.py
+++ b/brain/src/loopi_brain/providers/ollama.py
@@ -2,6 +2,8 @@
 from __future__ import annotations
 
 import base64
+import json
+from collections.abc import AsyncIterator
 from dataclasses import dataclass
 
 import httpx
@@ -65,14 +67,7 @@ class OllamaProvider:
         except httpx.TransportError as exc:  # read/write timeout, dropped connection: this request failed
             raise ProviderError(f"ollama {path}: {type(exc).__name__}: {exc}") from exc
         if response.status_code >= 400:
-            try:
-                detail = response.json().get("error") or response.text
-            except ValueError:
-                detail = response.text
-            message = f"ollama {path} HTTP {response.status_code}: {str(detail)[-ERROR_TAIL:]}"
-            if response.status_code == 404:  # model not pulled: configuration, shown on the status page
-                raise ProviderUnavailable(message)
-            raise ProviderError(message)
+            raise _http_error(path, response)
         try:
             data = response.json()
         except ValueError as exc:
@@ -97,6 +92,14 @@ class OllamaProvider:
         temperature: float = 0.0,
         max_tokens: int | None = None,
     ) -> str:
+        payload = self._chat_payload(model, messages, images, json_schema, temperature, max_tokens)
+        data = await self._request("POST", "/api/chat", payload, timeout=self._timeout_for(model))
+        content = (data.get("message") or {}).get("content")
+        if not isinstance(content, str):
+            raise ProviderError("ollama /api/chat: response has no message content")
+        return content
+
+    def _chat_payload(self, model, messages, images, json_schema, temperature, max_tokens) -> dict:
         msgs = [dict(m) for m in messages]
         if images:
             msgs[-1]["images"] = [base64.b64encode(img).decode("ascii") for img in images]
@@ -116,11 +119,41 @@ class OllamaProvider:
         }
         if json_schema is not None:
             payload["format"] = json_schema
-        data = await self._request("POST", "/api/chat", payload, timeout=self._timeout_for(model))
-        content = (data.get("message") or {}).get("content")
-        if not isinstance(content, str):
-            raise ProviderError("ollama /api/chat: response has no message content")
-        return content
+        return payload
+
+    async def stream_chat(
+        self, model: str, messages: list[dict], *, temperature: float = 0.0, max_tokens: int | None = None
+    ) -> AsyncIterator[str]:
+        """Yield the answer's text as Ollama writes it (``stream: true``: one JSON object per line)."""
+        payload = self._chat_payload(model, messages, None, None, temperature, max_tokens)
+        payload["stream"] = True
+        timeout = self._timeout_for(model)
+        kwargs: dict = {"timeout": timeout} if timeout is not None else {}
+        try:
+            async with self._client.stream("POST", f"{self._base}/api/chat", json=payload, **kwargs) as response:
+                if response.status_code >= 400:
+                    await response.aread()
+                    raise _http_error("/api/chat", response)
+                async for line in response.aiter_lines():
+                    if not line.strip():
+                        continue
+                    try:
+                        data = json.loads(line)
+                    except ValueError as exc:
+                        raise ProviderError("ollama /api/chat: a streamed line is not JSON") from exc
+                    if not isinstance(data, dict):
+                        raise ProviderError(f"ollama /api/chat: unexpected stream line {line[:ERROR_TAIL]}")
+                    if data.get("error"):
+                        raise ProviderError(f"ollama /api/chat: {str(data['error'])[-ERROR_TAIL:]}")
+                    piece = (data.get("message") or {}).get("content")
+                    if isinstance(piece, str) and piece:
+                        yield piece
+                    if data.get("done"):
+                        return
+        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
+            raise ProviderUnavailable(f"ollama /api/chat: {type(exc).__name__}: {exc}") from exc
+        except httpx.TransportError as exc:
+            raise ProviderError(f"ollama /api/chat: {type(exc).__name__}: {exc}") from exc
 
     async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
         data = await self._request(
@@ -147,3 +180,14 @@ class OllamaProvider:
         )
         if data.get("status") != "success":
             raise ProviderError(f"ollama pull {model}: {data}")
+
+
+def _http_error(path: str, response: httpx.Response) -> ProviderError:
+    try:
+        detail = response.json().get("error") or response.text
+    except ValueError:
+        detail = response.text
+    message = f"ollama {path} HTTP {response.status_code}: {str(detail)[-ERROR_TAIL:]}"
+    if response.status_code == 404:  # model not pulled: configuration, shown on the status page
+        return ProviderUnavailable(message)
+    return ProviderError(message)
```

```diff
diff --git a/brain/src/loopi_brain/providers/router.py b/brain/src/loopi_brain/providers/router.py
--- a/brain/src/loopi_brain/providers/router.py
+++ b/brain/src/loopi_brain/providers/router.py
@@ -2,6 +2,7 @@
 from __future__ import annotations
 
 import logging
+from collections.abc import AsyncIterator
 from dataclasses import dataclass
 
 import httpx
@@ -74,6 +75,33 @@ class Router:
         assert error is not None
         raise error
 
+    async def stream_chat(
+        self, task: str, messages: list[dict], temperature: float = 0.0, max_tokens: int | None = None
+    ) -> AsyncIterator[tuple[str, str]]:
+        """Yield (piece of text, model_version) as the answer is written. A route that fails before its first piece
+        falls back to the next one; a failure after that is raised (part of an answer has been shown)."""
+        error: ProviderError | None = None
+        for step in self._usable(task, images=False):
+            stream = self.providers[step.provider].stream_chat(
+                step.model, messages, temperature=temperature, max_tokens=max_tokens
+            )
+            try:
+                first = await anext(stream)
+            except StopAsyncIteration:
+                return  # an empty answer
+            except ProviderError as exc:
+                log.warning("task %s: %s failed: %s", task, model_version(step), exc)
+                error = exc
+                await stream.aclose()
+                continue
+            version = model_version(step)
+            yield first, version
+            async for piece in stream:
+                yield piece, version
+            return
+        assert error is not None
+        raise error
+
     async def embed(self, task: str, texts: list[str]) -> EmbedResult:
         error: ProviderError | None = None
         for step in self._usable(task, images=False):
```

- [ ] **Step 4: Write the chat module**

Create `brain/src/loopi_brain/chat.py`:

```python
"""Questions about one's own memories, answered with citations (spec §10.4).

1. The chat model reads the time a question refers to as structured output (dates relative to the local
   today); anything unusable means no time filter. A question without a time word skips this call (it costs a
   whole qwen3:8b run on a CPU).
2. Hybrid retrieval (``search.py``) within that time.
3. No relevant moment: "Không tìm thấy trong ký ức." -- the model is not asked to answer at all.
4. Otherwise the chat model answers from the numbered sources only, citing them as [n]; the answer streams.

``chat_events`` yields (event name, payload) pairs that the web layer sends as Server-Sent Events.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import AsyncIterator

from pydantic import BaseModel, ConfigDict, Field
from psycopg_pool import AsyncConnectionPool

from .config import Settings
from .days import day_bounds_utc, local_now
from .llm import ASR_CAVEAT, PICTURE_NOTE, AnswerInvalid, clip, parse_answer
from .providers.base import ProviderError
from .providers.router import Router
from .search import Hit, search_moments

log = logging.getLogger(__name__)
NOT_FOUND = "Không tìm thấy trong ký ức."
QUESTION_MAX = 500
SOURCES = 8
SOURCE_TEXT_MAX = 400
ANSWER_MAX_TOKENS = 600
FILTER_MAX_TOKENS = 60
MAX_FILTER_DAYS = 366
CITATION = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
# Words (vi, en, ko) or date-like numbers that can name a time; without one, no time filter is asked for.
TIME_WORDS = re.compile(
    r"\b(?:hôm|ngày|tuần|tháng|năm|sáng|trưa|chiều|tối|đêm|khuya|thứ|chủ nhật|lúc|giờ|mai|qua|nay|kia|trước|gần đây"
    r"|today|yesterday|tomorrow|morning|noon|afternoon|evening|night|week|month|year|monday|tuesday|wednesday"
    r"|thursday|friday|saturday|sunday|last|ago|recently|when)\b"
    r"|어제|오늘|내일|그제|아침|점심|저녁|밤|주말|지난|요일|\d{1,4}\s*[/.\-]\s*\d{1,2}",
    re.IGNORECASE,
)
FILTER_SCHEMA = {
    "type": "object",
    "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
    "required": ["from", "to"],
}
FILTER_SYSTEM = (
    "You read a question about the user's personal diary and say which local dates it is about. "
    "Today is {today}. Answer with JSON: from and to = the first and the last date the question refers to, as "
    "YYYY-MM-DD (the same date for a single day: \"hôm qua\" is yesterday, \"tuần trước\" is Monday to Sunday of "
    "last week, \"sáng nay\" is today); both empty strings when the question names no time."
)
ANSWER_SYSTEM = (
    "You answer questions about the user's own life, using only the numbered diary sources given, which a still "
    f"camera and a microphone in their home recorded. {PICTURE_NOTE} {ASR_CAVEAT} Answer briefly, in the "
    "language of the question (usually Vietnamese). Cite every fact with the number of its source in square "
    "brackets, like [2] or [1][3]. If the sources do not answer the question, say exactly "
    f"\"{NOT_FOUND}\" and do not guess."
)
KIND_WORDS = {"visual": "picture", "speech": "speech", "marker": "marked moment"}


class FilterAnswer(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    first: str = Field(alias="from")
    last: str = Field(alias="to")


def mentions_time(question: str) -> bool:
    return TIME_WORDS.search(question) is not None


def _date(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(text.strip())
    except ValueError:
        return None


def clean_filter(first: dt.date | None, last: dt.date | None, today: dt.date) -> tuple[dt.date | None, dt.date | None]:
    """A usable [first, last] range: one missing end copies the other, swapped ends are put in order, the future
    is cut off (all of it in the future: no filter) and at most MAX_FILTER_DAYS are kept (the latest ones)."""
    if first is None and last is None:
        return None, None
    first, last = first or last, last or first
    if first > last:
        first, last = last, first
    if first > today:
        return None, None
    last = min(last, today)
    first = max(first, last - dt.timedelta(days=MAX_FILTER_DAYS - 1))
    return first, last


async def time_filter(router: Router, question: str, today: dt.date) -> tuple[dt.date | None, dt.date | None]:
    """The local days ``question`` is about, or (None, None) for no filter (also when the model cannot say)."""
    messages = [
        {"role": "system", "content": FILTER_SYSTEM.format(today=today.strftime("%A, %Y-%m-%d"))},
        {"role": "user", "content": question},
    ]
    try:
        result = await router.chat("chat", messages, json_schema=FILTER_SCHEMA, temperature=0.0,
                                   max_tokens=FILTER_MAX_TOKENS)
        answer = parse_answer(result.text, FilterAnswer)
    except (ProviderError, AnswerInvalid) as exc:
        log.warning("chat: no time filter (%s)", exc)
        return None, None
    return clean_filter(_date(answer.first), _date(answer.last), today)


def source_lines(hits: list[Hit], zone) -> list[str]:
    lines = []
    for n, hit in enumerate(hits, start=1):
        kind = KIND_WORDS.get(hit.kind, hit.kind)
        if hit.kind == "speech":
            kind = f"speech ({hit.lang or '?'})"
        text = hit.text
        if hit.kind == "marker":
            note = (hit.item_meta or {}).get("note")
            text = f"the user marked this moment: {note}" if isinstance(note, str) and note.strip() else "the user marked this moment"
        lines.append(f"[{n}] {hit.ts_start.astimezone(zone):%Y-%m-%d %H:%M}, {kind}: {clip(text, SOURCE_TEXT_MAX)}")
    return lines


def answer_messages(question: str, hits: list[Hit], zone) -> list[dict]:
    sources = "\n".join(source_lines(hits, zone))
    return [
        {"role": "system", "content": ANSWER_SYSTEM},
        {"role": "user", "content": f"Sources:\n{sources}\n\nQuestion: {question}"},
    ]


def cited(text: str, count: int) -> list[int]:
    """The source numbers ``text`` cites that exist (1..count), in first-cited order."""
    numbers: list[int] = []
    for match in CITATION.finditer(text):
        for part in re.split(r"[,;]", match.group(1)):
            n = int(part)
            if 1 <= n <= count and n not in numbers:
                numbers.append(n)
    return numbers


def describe_filter(first: dt.date, last: dt.date) -> str:
    if first == last:
        return f"Tìm trong ngày {first:%d/%m/%Y}"
    return f"Tìm từ {first:%d/%m/%Y} đến {last:%d/%m/%Y}"


async def chat_events(
    pool: AsyncConnectionPool,
    router: Router | None,
    question: str,
    settings: Settings,
    now: dt.datetime | None = None,
) -> AsyncIterator[tuple[str, dict]]:
    """The steps of one answer, as (event, payload): status, filter, sources (Hit list), token, answer, error, done."""
    zone = settings.zone
    question = clip(question, QUESTION_MAX)
    if not question:
        yield "answer", {"text": "Hãy nhập một câu hỏi.", "not_found": True}
        yield "done", {}
        return
    yield "status", {"text": "Đang hiểu câu hỏi…"}
    first = last = None
    if router is not None and mentions_time(question):
        first, last = await time_filter(router, question, local_now(zone, now).date())
    if first is not None:
        yield "filter", {"text": describe_filter(first, last)}
    yield "status", {"text": "Đang tìm trong ký ức…"}
    start = day_bounds_utc(first, zone)[0] if first else None
    end = day_bounds_utc(last, zone)[1] if last else None
    result = await search_moments(pool, router, question, start=start, end=end, limit=SOURCES,
                                  max_distance=settings.search_max_distance,
                                  min_similarity=settings.search_min_similarity)
    if not result.hits:
        yield "answer", {"text": NOT_FOUND, "not_found": True}
        yield "done", {}
        return
    yield "sources", {"hits": result.hits}
    if router is None:
        yield "error", {"text": "Không trả lời được: cấu hình provider lỗi. Các nguồn liên quan ở trên."}
        yield "done", {}
        return
    yield "status", {"text": "Đang viết câu trả lời…"}
    pieces: list[str] = []
    try:
        async for piece, _version in router.stream_chat("chat", answer_messages(question, result.hits, zone),
                                                        max_tokens=ANSWER_MAX_TOKENS):
            pieces.append(piece)
            yield "token", {"text": piece}
    except ProviderError as exc:
        log.warning("chat answer failed: %s", exc)
        yield "error", {"text": "Không trả lời được lúc này: máy chủ model đang lỗi hoặc không chạy. "
                                "Các nguồn liên quan ở trên."}
        yield "done", {}
        return
    text = "".join(pieces).strip()
    # "Không tìm thấy" from the model, citing nothing, is a not-found answer, not an uncited one.
    not_found = text.startswith(NOT_FOUND.rstrip(".")) and not cited(text, len(result.hits))
    yield "answer", {"text": text, "not_found": not_found}
    yield "done", {}
```

- [ ] **Step 5: Write the endpoint and the chat box**

Apply to `brain/src/loopi_brain/web/search_routes.py`:

```diff
diff --git a/brain/src/loopi_brain/web/search_routes.py b/brain/src/loopi_brain/web/search_routes.py
--- a/brain/src/loopi_brain/web/search_routes.py
+++ b/brain/src/loopi_brain/web/search_routes.py
@@ -1,15 +1,22 @@
-"""The "Tìm kiếm & Chat" page (spec §10.4, §10.6): plain hybrid search here; cited chat answers in Task 8."""
+"""The "Tìm kiếm & Chat" page (spec §10.4, §10.6): plain hybrid search, and answers with citations streamed
+as Server-Sent Events."""
 from __future__ import annotations
 
 import datetime as dt
+import json
+import logging
 
-from fastapi import APIRouter, Query, Request
-from fastapi.responses import HTMLResponse
+from fastapi import APIRouter, Depends, Form, Query, Request
+from fastapi.responses import HTMLResponse, StreamingResponse
+from markupsafe import Markup, escape
 
-from .. import search
+from .. import chat, search
 from ..days import day_bounds_utc
 from ..pipeline import moments as moment_rules
-from .routes import MARKER_LABEL, TEMPLATES
+from .routes import MARKER_LABEL, TEMPLATES, require_htmx
+
+log = logging.getLogger(__name__)
+NO_CITATION = "Câu trả lời không trích nguồn nào, có thể không chính xác."
 
 KIND_LABELS = {"visual": "Hình ảnh", "speech": "Lời nói", "marker": "Đánh dấu"}
 SEARCH_LIMIT = 30
@@ -70,3 +77,49 @@ async def search_page(
             "semantic": result.semantic if result else True,
         },
     )
+
+
+def render_answer(text: str, sources: list[dict]) -> Markup:
+    """The answer as HTML: text escaped, each valid [n] a link to source n, invalid numbers dropped, and a warning
+    when nothing is cited."""
+    parts: list[str] = []
+    position = 0
+    for match in chat.CITATION.finditer(text):
+        parts.append(str(escape(text[position:match.start()])))
+        for n in chat.cited(match.group(0), len(sources)):
+            parts.append(f'<a class="cite" href="{escape(sources[n - 1]["link"])}">[{n}]</a>')
+        position = match.end()
+    parts.append(str(escape(text[position:])))
+    html = "<p>" + "".join(parts).replace("\n", "<br>") + "</p>"
+    if sources and not chat.cited(text, len(sources)):
+        html += f'<p class="muted">{NO_CITATION}</p>'
+    return Markup(html)
+
+
+def sse(event: str, payload: dict) -> str:
+    """One Server-Sent Event; the payload is JSON, so it never contains a raw newline."""
+    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
+
+
+@router.post("/chat", dependencies=[Depends(require_htmx)])
+async def chat_answer(request: Request, q: str = Form(..., max_length=2000)):
+    state = request.app.state
+    zone = state.settings.zone
+    router_ = search.request_router(state.settings, state.llm)
+
+    async def events():
+        sources: list[dict] = []
+        try:
+            async for event, payload in chat.chat_events(state.pool, router_, q, state.settings):
+                if event == "sources":
+                    sources = [hit_view(hit, zone) for hit in payload["hits"]]
+                    payload = {"html": TEMPLATES.get_template("_sources.html").render(hits=sources)}
+                elif event == "answer":
+                    payload = {"html": str(render_answer(payload["text"], [] if payload["not_found"] else sources))}
+                yield sse(event, payload)
+        except Exception:
+            log.exception("chat answer failed")
+            yield sse("error", {"text": "Có lỗi khi trả lời, thử lại."})
+            yield sse("done", {})
+
+    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
```

Create `brain/src/loopi_brain/web/templates/_sources.html`:

```html
<ol class="hits">{% for h in hits %}{% include "_hit.html" %}{% endfor %}</ol>
```

Apply to `brain/src/loopi_brain/web/templates/search.html`:

```diff
diff --git a/brain/src/loopi_brain/web/templates/search.html b/brain/src/loopi_brain/web/templates/search.html
--- a/brain/src/loopi_brain/web/templates/search.html
+++ b/brain/src/loopi_brain/web/templates/search.html
@@ -1,6 +1,16 @@
 {% extends "base.html" %}
 {% block content %}
 <h1>Tìm kiếm & Chat</h1>
+<form id="chat-form" class="card">
+  <input id="chat-q" name="q" required maxlength="500" size="50" placeholder="Hỏi về ký ức, ví dụ: Hôm qua mình hẹn làm gì?">
+  <button>Hỏi</button>
+  <div id="chat" hidden>
+    <p id="chat-status" class="muted" role="status"></p>
+    <p id="chat-filter" class="muted"></p>
+    <div id="chat-answer"></div>
+    <div id="chat-sources"></div>
+  </div>
+</form>
 <form method="get" action="/search" class="card">
   <input type="search" name="q" value="{{ q }}" placeholder="Tìm trong ký ức…" required maxlength="500" size="40">
   <label>Từ ngày <input type="date" name="from" value="{{ first or '' }}"></label>
@@ -13,4 +23,50 @@
   <ul class="hits">{% for h in hits %}{% include "_hit.html" %}{% endfor %}</ul>
   {% else %}<p>Không tìm thấy khoảnh khắc nào.</p>{% endif %}
 {% endif %}
+<script>
+  // The answer streams as Server-Sent Events over a POST (fetch, so the HX-Request CSRF header can be sent).
+  // Sources and the final answer arrive as HTML the server has already escaped; tokens are plain text.
+  const chatForm = document.getElementById("chat-form");
+  chatForm.addEventListener("submit", async (e) => {
+    e.preventDefault();
+    const box = document.getElementById("chat"), status = document.getElementById("chat-status");
+    const filter = document.getElementById("chat-filter"), answer = document.getElementById("chat-answer");
+    const sources = document.getElementById("chat-sources"), button = chatForm.querySelector("button");
+    box.hidden = false; filter.textContent = ""; answer.textContent = ""; sources.innerHTML = "";
+    status.textContent = "Đang gửi câu hỏi…"; button.disabled = true;
+    const handle = (name, data) => {
+      if (name === "status" || name === "error") status.textContent = data.text;
+      else if (name === "filter") filter.textContent = data.text;
+      else if (name === "sources") sources.innerHTML = data.html;
+      else if (name === "token") answer.textContent += data.text;
+      else if (name === "answer") { answer.innerHTML = data.html; status.textContent = ""; }
+    };
+    try {
+      const res = await fetch("/chat", {method: "POST", headers: {"HX-Request": "true"},
+                                        body: new URLSearchParams({q: document.getElementById("chat-q").value})});
+      if (!res.ok) throw new Error(res.status);
+      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
+      let buffer = "";
+      for (;;) {
+        const {value, done} = await reader.read();
+        if (done) break;
+        buffer += value;
+        for (let cut = buffer.indexOf("\n\n"); cut >= 0; cut = buffer.indexOf("\n\n")) {
+          const block = buffer.slice(0, cut);
+          buffer = buffer.slice(cut + 2);
+          let name = "message", data = "";
+          for (const line of block.split("\n")) {
+            if (line.startsWith("event: ")) name = line.slice(7);
+            else if (line.startsWith("data: ")) data += line.slice(6);
+          }
+          handle(name, JSON.parse(data || "{}"));
+        }
+      }
+    } catch (err) {
+      status.textContent = "Có lỗi, thử lại";
+    } finally {
+      button.disabled = false;
+    }
+  });
+</script>
 {% endblock %}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_chat.py -q`
Expected: `30 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `461 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/chat.py brain/src/loopi_brain/providers/base.py brain/src/loopi_brain/providers/ollama.py \
  brain/src/loopi_brain/providers/router.py brain/src/loopi_brain/web/search_routes.py \
  brain/src/loopi_brain/web/templates/_sources.html brain/src/loopi_brain/web/templates/search.html \
  brain/tests/helpers.py brain/tests/test_chat.py
git commit -F - <<'EOF'
feat(brain): cited answers about memories, streamed over SSE (spec 10.4)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-16; they amend the steps above):**
- **R-I2:** use `moments.marker_note` for the marker note instead of re-implementing it.

---

### Task 9: Cài đặt — providers, retention days and reprocessing a day

**Files:**
- Create: `brain/src/loopi_brain/store.py`, `brain/src/loopi_brain/reprocess.py`, `brain/src/loopi_brain/web/settings_routes.py`
- Create: `brain/src/loopi_brain/web/templates/settings.html`, `brain/src/loopi_brain/web/templates/_reprocessed.html`
- Modify: `brain/src/loopi_brain/app.py`, `brain/src/loopi_brain/web/templates/base.html` (nav "Cài đặt", a Vietnamese message for 422)
- Test: `brain/tests/test_settings_page.py`

**Interfaces:**
- Consumes: Task 1 tables `setting`, `chapter`, `day_recap`; Task 4 `chapters.defer_build`; Task 5 `recap.defer_recap`; P2a `status.model_health`, `load_providers`, `model_version`, the dispatcher (re-captions keyframes whose `caption_job_id` is NULL, re-analyses `pending` items).
- Produces:
  - `store.Retention(video_days: int = 7, audio_days: int = 7, enabled: bool = False)` (days 1..`store.MAX_DAYS` = 3650, `extra="forbid"`; **retention ships off**), `store.get_retention(pool) -> Retention`, `store.set_retention(pool, retention)` (row `setting.key = "retention"`). Task 11 reads it and shows what it would delete.
  - `reprocess.CHOICES = ("captions", "transcripts", "chapters", "recap")`, `reprocess.ReprocessResult(keyframes, audio_items, expired_audio, chapters, recap)`, `reprocess.reprocess_day(pool, day, zone, what: set[str]) -> ReprocessResult` (ValueError for an empty or unknown choice).
  - `GET /settings`; `POST /settings/retention` (form `video_days`, `audio_days`, checkbox `enabled`; 422 when invalid); `POST /settings/reprocess` (form `day`, `what` repeated); `web.settings_routes.providers_view(settings) -> dict`, `settings_context(request)`, `TASK_LABELS`, `CHOICE_LABELS`. Tasks 10 and 12 extend this page.

What reprocessing does (spec §12 "Đổi model"), all in one transaction:
- **captions**: the day's keyframes lose caption, model, job id and error, and their visual moments go → the dispatcher captions them again with the configured model.
- **transcripts**: `done` audio items of the day whose raw file is still kept lose their transcript (their speech moments go with it) and become `pending` → the dispatcher transcribes them again; items whose raw file retention removed are counted, not touched.
- **chapters**: the day's chapters are reopened and must be summarised again; a build is asked for now.
- **recap**: a new recap is asked for now.
- Any choice marks the day's recap stale (the planner rewrites it once the day has settled); captions, transcripts and chapters reopen the day's chapters.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_settings_page.py`:

```python
import datetime as dt

import httpx
import pydantic
import pytest
from psycopg.types.json import Jsonb

from helpers import (
    DEFAULT_CAPTION,
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_chapter,
    insert_item,
    insert_keyframe,
    insert_moment,
    jobs_by_lock,
    new_ulid,
    paired_device,
)
from loopi_brain import reprocess, store
from loopi_brain.app import create_app

HX = {"HX-Request": "true"}
DAY = dt.date(2026, 10, 14)
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def ollama():
    return FakeOllama()


@pytest.fixture
async def web(pool, pki_settings, jobs_open, ollama):
    app = create_app(pki_settings, pool=pool, http_client=ollama.client(), llm_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def test_retention_defaults_to_seven_days_and_is_stored(pool):
    assert await store.get_retention(pool) == store.Retention(video_days=7, audio_days=7, enabled=False)  # off
    await store.set_retention(pool, store.Retention(video_days=14, audio_days=3, enabled=True))
    assert await store.get_retention(pool) == store.Retention(video_days=14, audio_days=3, enabled=True)
    for bad in ({"video_days": 0}, {"audio_days": 3651}, {"video_days": "a week"}, {"keep": 1}):
        with pytest.raises(pydantic.ValidationError):
            store.Retention(**bad)


async def test_the_settings_page_shows_providers_privacy_health_and_retention(web, ollama):
    ollama.models.remove("qwen3:8b")
    text = (await web.get("/settings")).text
    assert '<a href="/settings">Cài đặt</a>' in text
    assert "<td>Recap cuối ngày</td><td><code>ollama:qwen3:8b</code></td>" in text
    assert "<td>Nhận dạng giọng nói</td><td><code>local:faster-whisper/large-v3-turbo-int8</code></td>" in text
    assert "Ảnh gửi lên cloud: không" in text and "âm thanh gửi lên cloud: không" in text
    assert "Thiếu model: qwen3:8b." in text and "Asia/Seoul" in text
    assert 'name="video_days" min="1" max="3650" required value="7"' in text
    assert 'name="audio_days" min="1" max="3650" required value="7"' in text
    assert '<input type="checkbox" name="enabled"> Bật dọn dữ liệu cũ' in text  # not checked
    assert "Dọn dữ liệu cũ: <strong>đang tắt</strong> (chưa xoá gì)" in text
    assert 'href="/pipeline"' in text and 'hx-post="/settings/reprocess"' in text


async def test_a_broken_providers_file_is_shown(pool, pki_settings, jobs_open, tmp_path):
    bad = tmp_path / "providers.yaml"
    bad.write_text("tasks: {}\n")
    app = create_app(pki_settings.model_copy(update={"providers_file": bad}), pool=pool,
                     http_client=FakeOllama().client(), llm_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        assert "Cấu hình provider lỗi" in (await c.get("/settings")).text


async def test_the_retention_form_saves_validated_days(web, pool):
    assert (await web.post("/settings/retention", data={"video_days": "5", "audio_days": "9"})).status_code == 403
    r = await web.post("/settings/retention", data={"video_days": "5", "audio_days": "9"}, headers=HX)
    assert r.status_code == 200 and "giữ video thô 5 ngày, audio thô 9 ngày" in r.text and "đang tắt" in r.text
    assert await store.get_retention(pool) == store.Retention(video_days=5, audio_days=9, enabled=False)
    for bad in ("0", "3651", "abc", "-2"):
        r = await web.post("/settings/retention", data={"video_days": bad, "audio_days": "9"}, headers=HX)
        assert r.status_code == 422
    assert await store.get_retention(pool) == store.Retention(video_days=5, audio_days=9)
    assert "value=\"5\"" in (await web.get("/settings")).text
    r = await web.post("/settings/retention", data={"video_days": "5", "audio_days": "9", "enabled": "on"}, headers=HX)
    assert "Dọn dữ liệu cũ đang bật: chạy lúc 03:00" in r.text and (await store.get_retention(pool)).enabled
    assert 'name="enabled" checked' in (await web.get("/settings")).text


async def _captioned_keyframe(pool, settings, device_id, ts) -> tuple[str, str]:
    item = await insert_item(pool, device_id, kind="video", started_at=ts)
    kf = await insert_keyframe(pool, settings, item, device_id, ts)
    moment = await insert_moment(pool, device_id, kind="visual", ts=ts, text="old caption", lang="en")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE keyframe SET caption = %s, caption_model = 'ollama:old', captioned_at = now(), caption_job_id = 7"
            " WHERE id = %s", (Jsonb(DEFAULT_CAPTION), kf))
        await conn.execute("UPDATE moment SET keyframe_id = %s, item_id = %s WHERE id = %s", (kf, item, moment))
    return kf, moment


async def test_reprocessing_captions_resets_the_days_keyframes(web, pool, pki_settings, dev):
    device_id = dev["device_id"]
    kf, moment = await _captioned_keyframe(pool, pki_settings, device_id, NINE)
    other, other_moment = await _captioned_keyframe(pool, pki_settings, device_id, NINE + dt.timedelta(days=1))
    await insert_chapter(pool, DAY, NINE, NINE)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'x', 'x')", (DAY,))
    r = await web.post("/settings/reprocess", data={"day": "2026-10-14", "what": ["captions"]}, headers=HX)
    assert r.status_code == 200
    assert "Ngày 14/10/2026 sẽ được xử lý lại (mô tả khung hình)" in r.text and "1 khung hình mô tả lại" in r.text
    assert "1 chương chia lại, recap sẽ được viết lại" in r.text
    row = await fetch_one(pool, "SELECT caption, caption_model, caption_job_id FROM keyframe WHERE id = %s", (kf,))
    assert row == {"caption": None, "caption_model": None, "caption_job_id": None}  # the dispatcher captions it again
    assert [m["id"] for m in await fetch_all(pool, "SELECT id FROM moment")] == [other_moment]
    assert (await fetch_one(pool, "SELECT caption_model FROM keyframe WHERE id = %s", (other,)))["caption_model"]
    assert (await fetch_one(pool, "SELECT closed FROM chapter"))["closed"] is False
    assert (await fetch_one(pool, "SELECT stale FROM day_recap"))["stale"] is True


async def test_reprocessing_transcripts_only_where_the_raw_audio_is_kept(pool, pki_settings, dev):
    device_id = dev["device_id"]
    kept = await insert_item(pool, device_id, kind="audio", started_at=NINE)
    gone = await insert_item(pool, device_id, kind="audio", started_at=NINE + dt.timedelta(minutes=1))
    for item in (kept, gone):
        segment = new_ulid()
        async with pool.connection() as conn:
            await conn.execute("UPDATE item SET analysis_state = 'done', analysis_job_id = 3 WHERE id = %s", (item,))
            await conn.execute(
                "INSERT INTO transcript_segment (id, item_id, device_id, start_ts, end_ts, lang, text, model_version)"
                " VALUES (%s, %s, %s, %s, %s, 'vi', 'xin chào', 'old')", (segment, item, device_id, NINE, NINE))
            await conn.execute(
                "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, item_id, transcript_segment_id,"
                " model_version) VALUES (%s, %s, %s, %s, 'speech', 'xin chào', %s, %s, 'old')",
                (new_ulid(), device_id, NINE, NINE, item, segment))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (gone,))
    result = await reprocess.reprocess_day(pool, DAY, pki_settings.zone, {"transcripts"})
    assert result == reprocess.ReprocessResult(audio_items=1, expired_audio=1)
    rows = await fetch_all(pool, "SELECT id, analysis_state, analysis_job_id FROM item ORDER BY started_at")
    assert rows == [{"id": kept, "analysis_state": "pending", "analysis_job_id": None},
                    {"id": gone, "analysis_state": "done", "analysis_job_id": 3}]
    assert [r["item_id"] for r in await fetch_all(pool, "SELECT item_id FROM moment")] == [gone]


async def test_reprocessing_chapters_forces_new_summaries_now(pool, pki_settings, dev, jobs_open):
    await insert_moment(pool, dev["device_id"], ts=NINE)
    await insert_chapter(pool, DAY, NINE, NINE, title="Cũ", summary="Tóm tắt cũ.")
    result = await reprocess.reprocess_day(pool, DAY, pki_settings.zone, {"chapters"})
    assert result.chapters == 1
    row = await fetch_one(pool, "SELECT closed, summary_signature, title FROM chapter")
    assert row == {"closed": False, "summary_signature": None, "title": "Cũ"}  # shown until the new one is written
    assert [j["task_name"] for j in (await jobs_by_lock(pool))["chapters:2026-10-14"]] == ["build_chapters"]


async def test_reprocessing_the_recap_marks_it_stale_and_asks_now(pool, pki_settings, jobs_open):
    await insert_chapter(pool, DAY, NINE, NINE)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'x', 'x')", (DAY,))
    result = await reprocess.reprocess_day(pool, DAY, pki_settings.zone, {"recap"})
    assert result == reprocess.ReprocessResult(recap=True)  # chapters are not touched
    assert (await fetch_one(pool, "SELECT closed FROM chapter"))["closed"] is True
    assert "recap:2026-10-14" in await jobs_by_lock(pool)


async def test_reprocessing_needs_a_day_and_a_choice(web, pool, pki_settings):
    assert (await web.post("/settings/reprocess", data={"day": "2026-10-14"}, headers=HX)).status_code == 422
    assert (await web.post("/settings/reprocess", data={"day": "2026-10-14", "what": ["vlog"]},
                           headers=HX)).status_code == 422
    assert (await web.post("/settings/reprocess", data={"day": "hôm qua", "what": ["recap"]},
                           headers=HX)).status_code == 422
    assert (await web.post("/settings/reprocess", data={"day": "2026-10-14", "what": ["recap"]})).status_code == 403
    with pytest.raises(ValueError):
        await reprocess.reprocess_day(pool, DAY, pki_settings.zone, set())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_settings_page.py -q`
Expected: FAIL — `ImportError: cannot import name 'reprocess' from 'loopi_brain'`.

- [ ] **Step 3: Write the settings store and the reprocessing**

Create `brain/src/loopi_brain/store.py`:

```python
"""Settings edited on the Cài đặt page, kept in the ``setting`` table; a missing key means the built-in default."""
from __future__ import annotations

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

RETENTION_KEY = "retention"
MAX_DAYS = 3650


class Retention(BaseModel):
    """How many days raw recordings are kept (spec §9.5; D20: audio like video, 7 days by default), and whether the
    nightly deletion runs at all: it is **off** until the owner turns it on in Cài đặt, after seeing what would go
    (deleting recordings is never done without the owner's yes)."""

    model_config = ConfigDict(extra="forbid")

    video_days: int = Field(default=7, ge=1, le=MAX_DAYS)
    audio_days: int = Field(default=7, ge=1, le=MAX_DAYS)
    enabled: bool = False


async def get_retention(pool: AsyncConnectionPool) -> Retention:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT value FROM setting WHERE key = %s", (RETENTION_KEY,))
        row = await cur.fetchone()
    return Retention.model_validate(row["value"]) if row else Retention()


async def set_retention(pool: AsyncConnectionPool, retention: Retention) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO setting (key, value) VALUES (%s, %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (RETENTION_KEY, Jsonb(retention.model_dump())),
        )
```

Create `brain/src/loopi_brain/reprocess.py`:

```python
"""Reprocess one local day (spec §12 "Đổi model: ... có lệnh xử lý lại theo ngày").

Each choice resets what the pipeline derived, so the usual machinery does the work again with the models now
configured: the dispatcher re-captions keyframes and re-transcribes audio items whose raw file still exists,
the chapter planner rebuilds the reopened chapters, and the recap planner rewrites the recap once the day has
settled (or at once, when the recap itself is chosen).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from .days import day_bounds_utc
from .pipeline import chapters as chapter_rules
from .pipeline import recap as recap_rules

CHOICES = ("captions", "transcripts", "chapters", "recap")


@dataclass(frozen=True)
class ReprocessResult:
    keyframes: int = 0  # keyframes to caption again
    audio_items: int = 0  # recordings to transcribe again
    expired_audio: int = 0  # recordings whose raw file retention already removed (not transcribed again)
    chapters: int = 0  # chapters reopened
    recap: bool = False  # a recap exists and was marked out of date


async def reprocess_day(pool: AsyncConnectionPool, day: dt.date, zone, what: set[str]) -> ReprocessResult:
    unknown = what - set(CHOICES)
    if not what or unknown:
        raise ValueError(f"choose among {', '.join(CHOICES)} (got {sorted(what)})")
    start, end = day_bounds_utc(day, zone)
    keyframes = audio_items = expired = 0
    async with pool.connection() as conn, conn.transaction():
        if "captions" in what:
            await conn.execute(
                "DELETE FROM moment WHERE kind = 'visual' AND keyframe_id IN"
                " (SELECT id FROM keyframe WHERE ts >= %s AND ts < %s)",
                (start, end),
            )
            cur = await conn.execute(
                "UPDATE keyframe SET caption = NULL, caption_model = NULL, captioned_at = NULL,"
                " caption_job_id = NULL, caption_error = NULL WHERE ts >= %s AND ts < %s",
                (start, end),
            )
            keyframes = cur.rowcount
        if "transcripts" in what:
            cur = await conn.execute(
                "SELECT id, raw_deleted_at FROM item WHERE kind = 'audio' AND analysis_state = 'done'"
                " AND started_at >= %s AND started_at < %s FOR UPDATE",
                (start, end),
            )
            rows = await cur.fetchall()
            redo = [r["id"] for r in rows if r["raw_deleted_at"] is None]
            expired = len(rows) - len(redo)
            # Moments go with their transcript segments (ON DELETE CASCADE).
            await conn.execute("DELETE FROM transcript_segment WHERE item_id = ANY(%s)", (redo,))
            await conn.execute(
                "UPDATE item SET analysis_state = 'pending', analysis_job_id = NULL, analysis_error = NULL,"
                " analyzed_at = NULL WHERE id = ANY(%s)",
                (redo,),
            )
            audio_items = len(redo)
        reopened = 0
        if what & {"captions", "transcripts", "chapters"}:  # the day's moments (or their summaries) change
            cur = await conn.execute(
                "UPDATE chapter SET closed = false,"
                " summary_signature = CASE WHEN %s THEN NULL ELSE summary_signature END WHERE day = %s",
                ("chapters" in what, day),
            )
            reopened = cur.rowcount
        cur = await conn.execute("UPDATE day_recap SET stale = true WHERE day = %s", (day,))
        stale = cur.rowcount > 0
    if "chapters" in what:
        await chapter_rules.defer_build(pool, day, zone)
    if "recap" in what:
        await recap_rules.defer_recap(pool, day)
    return ReprocessResult(keyframes, audio_items, expired, reopened, stale)
```

- [ ] **Step 4: Write the page**

Create `brain/src/loopi_brain/web/settings_routes.py`:

```python
"""The Cài đặt page (spec §10.6): providers (read only), retention, reprocessing a day, failed jobs."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from .. import reprocess, store
from ..pipeline import status
from ..providers.config import TASKS, ProvidersConfigError, load_providers
from ..providers.router import model_version
from .routes import TEMPLATES, require_htmx

TASK_LABELS = {
    "caption": "Mô tả khung hình",
    "asr": "Nhận dạng giọng nói",
    "embed": "Embedding",
    "chapter_summary": "Tóm tắt chương",
    "recap": "Recap cuối ngày",
    "chat": "Hỏi đáp",
}
CHOICE_LABELS = {"captions": "mô tả khung hình", "transcripts": "transcript", "chapters": "chương", "recap": "recap"}

router = APIRouter()


def providers_view(settings) -> dict:
    """Each task's route chain ("ollama:qwen3:8b → …"), the privacy flags, or the configuration error."""
    try:
        config = load_providers(settings.providers_file)
    except ProvidersConfigError as exc:
        return {"error": str(exc)[:500], "tasks": [], "privacy": None}
    tasks = [
        {"task": task, "label": TASK_LABELS[task],
         "chain": " → ".join(model_version(step) for step in config.tasks[task].chain())}
        for task in TASKS
    ]
    return {"error": None, "tasks": tasks, "privacy": config.privacy}


async def settings_context(request: Request) -> dict:
    state = request.app.state
    return {
        "providers": providers_view(state.settings),
        "health": await status.model_health(state.settings, state.http),
        "tz": state.settings.tz,
        "retention": await store.get_retention(state.pool),
        "max_days": store.MAX_DAYS,
        "choices": CHOICE_LABELS,
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "settings.html", await settings_context(request))


@router.post("/settings/retention", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def save_retention(
    request: Request, video_days: str = Form(...), audio_days: str = Form(...), enabled: str = Form("")
):
    try:
        retention = store.Retention(video_days=video_days, audio_days=audio_days, enabled=enabled == "on")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"days must be whole numbers from 1 to {store.MAX_DAYS}") from exc
    await store.set_retention(request.app.state.pool, retention)
    state = "đang bật: chạy lúc 03:00" if retention.enabled else "đang tắt: không xoá gì"
    return HTMLResponse(
        f"<span>Đã lưu: giữ video thô {retention.video_days} ngày, audio thô {retention.audio_days} ngày."
        f" Dọn dữ liệu cũ {state}.</span>"
    )


@router.post("/settings/reprocess", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def reprocess_day(request: Request, day: dt.date = Form(...), what: list[str] = Form(default=[])):
    try:
        result = await reprocess.reprocess_day(request.app.state.pool, day, request.app.state.settings.zone, set(what))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TEMPLATES.TemplateResponse(
        request, "_reprocessed.html", {"day": day, "r": result, "what": [CHOICE_LABELS[c] for c in reprocess.CHOICES
                                                                       if c in what]}
    )
```

Create `brain/src/loopi_brain/web/templates/settings.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Cài đặt</h1>
<section class="card" id="providers">
  <h2>Provider và model</h2>
  {% if providers.error %}<p class="err">Cấu hình provider lỗi: {{ providers.error }}</p>
  {% else %}
  <table>
    <tr><th>Tác vụ</th><th>Provider:model (dự phòng)</th></tr>
    {% for t in providers.tasks %}<tr><td>{{ t.label }}</td><td><code>{{ t.chain }}</code></td></tr>{% endfor %}
  </table>
  <p>Ảnh gửi lên cloud: {{ "có" if providers.privacy.allow_cloud_images else "không" }} ·
     âm thanh gửi lên cloud: {{ "có" if providers.privacy.allow_cloud_audio else "không" }}</p>
  {% endif %}
  {% if not health.error %}
    {% if not health.reachable %}<p class="err">Không kết nối được Ollama ({{ health.url }}).</p>
    {% elif health.missing %}<p class="err">Thiếu model: {{ health.missing|join(", ") }}.</p>
    {% else %}<p class="online">Ollama sẵn sàng, đủ model.</p>{% endif %}
  {% endif %}
  <p class="muted">Sửa trong <code>deploy/providers.yaml</code> rồi khởi động lại api và worker. Múi giờ của dòng thời gian: {{ tz }}.</p>
</section>
<section class="card" id="retention">
  <h2>Lưu trữ</h2>
  <form hx-post="/settings/retention" hx-target="#retention-result">
    <label>Giữ video thô <input type="number" name="video_days" min="1" max="{{ max_days }}" required value="{{ retention.video_days }}"> ngày</label>
    <label>Giữ audio thô <input type="number" name="audio_days" min="1" max="{{ max_days }}" required value="{{ retention.audio_days }}"> ngày</label>
    <label><input type="checkbox" name="enabled"{% if retention.enabled %} checked{% endif %}> Bật dọn dữ liệu cũ</label>
    <button>Lưu</button>
  </form>
  <p>Dọn dữ liệu cũ: {% if retention.enabled %}<strong>đang bật</strong>{% else %}<strong>đang tắt</strong> (chưa xoá gì){% endif %}.</p>
  <p class="muted">Khi bật, lúc 03:00 mỗi đêm file thô cũ hơn số ngày này bị xoá vĩnh viễn (chỉ những đoạn đã phân tích xong). Keyframe, transcript, chương và recap được giữ mãi.</p>
  <div id="retention-result"></div>
</section>
<section class="card" id="reprocess">
  <h2>Xử lý lại một ngày</h2>
  <form hx-post="/settings/reprocess" hx-target="#reprocess-result" hx-confirm="Xử lý lại ngày này bằng các model hiện tại?">
    <input type="date" name="day" required>
    {% for value, label in choices.items() %}<label><input type="checkbox" name="what" value="{{ value }}"> {{ label }}</label> {% endfor %}
    <button>Xử lý lại</button>
  </form>
  <p class="muted">Transcript chỉ làm lại được khi audio thô còn giữ.</p>
  <div id="reprocess-result"></div>
</section>
<section class="card">
  <h2>Job lỗi</h2>
  <p>Job lỗi và nút xử lý lại ở trang <a href="/pipeline">Xử lý</a>.</p>
</section>
{% endblock %}
```

Create `brain/src/loopi_brain/web/templates/_reprocessed.html`:

```html
<span>Ngày {{ day.strftime("%d/%m/%Y") }} sẽ được xử lý lại ({{ what|join(", ") }}):
{{ r.keyframes }} khung hình mô tả lại, {{ r.audio_items }} đoạn âm thanh nhận dạng lại{% if r.expired_audio %} ({{ r.expired_audio }} đoạn đã hết hạn, không làm lại được){% endif %},
{{ r.chapters }} chương chia lại{% if r.recap %}, recap sẽ được viết lại{% endif %}.</span>
```

Apply to `brain/src/loopi_brain/app.py` and `brain/src/loopi_brain/web/templates/base.html`:

```diff
diff --git a/brain/src/loopi_brain/app.py b/brain/src/loopi_brain/app.py
--- a/brain/src/loopi_brain/app.py
+++ b/brain/src/loopi_brain/app.py
@@ -18,6 +18,7 @@ from .providers.ollama import DEFAULT_TIMEOUT
 from .web.day_routes import router as day_router
 from .web.routes import router as web_router
 from .web.search_routes import router as search_router
+from .web.settings_routes import router as settings_router
 
 log = logging.getLogger(__name__)
 
@@ -80,6 +81,7 @@ def create_app(
     app.include_router(web_router)
     app.include_router(day_router)
     app.include_router(search_router)
+    app.include_router(settings_router)
     settings.media_root.mkdir(parents=True, exist_ok=True)
     app.mount("/media", StaticFiles(directory=settings.media_root), name="media")
     app.mount(
```

```diff
diff --git a/brain/src/loopi_brain/web/templates/base.html b/brain/src/loopi_brain/web/templates/base.html
--- a/brain/src/loopi_brain/web/templates/base.html
+++ b/brain/src/loopi_brain/web/templates/base.html
@@ -33,12 +33,13 @@
   </style>
 </head>
 <body>
-  <header><nav><a href="/">Hôm nay</a><a href="/search">Tìm kiếm</a><a href="/pipeline">Xử lý</a><a href="/devices">Thiết bị</a></nav></header>
+  <header><nav><a href="/">Hôm nay</a><a href="/search">Tìm kiếm</a><a href="/pipeline">Xử lý</a><a href="/devices">Thiết bị</a><a href="/settings">Cài đặt</a></nav></header>
   <div id="hx-error" class="err" role="alert"></div>
   <main>{% block content %}{% endblock %}</main>
   <script>
     const HX_ERRORS = {403: "Yêu cầu không hợp lệ", 404: "Không tìm thấy thiết bị",
-                       409: "Job không còn ở trạng thái lỗi, hãy tải lại trang"};
+                       409: "Job không còn ở trạng thái lỗi, hãy tải lại trang",
+                       422: "Giá trị không hợp lệ, hãy kiểm tra lại"};
     document.body.addEventListener("htmx:responseError", (e) => {
       document.getElementById("hx-error").textContent =
         HX_ERRORS[e.detail.xhr.status] || "Có lỗi, thử lại";
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_settings_page.py -q`
Expected: `9 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `470 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/store.py brain/src/loopi_brain/reprocess.py brain/src/loopi_brain/web/settings_routes.py \
  brain/src/loopi_brain/web/templates/settings.html brain/src/loopi_brain/web/templates/_reprocessed.html \
  brain/src/loopi_brain/app.py brain/src/loopi_brain/web/templates/base.html brain/tests/test_settings_page.py
git commit -F - <<'EOF'
feat(brain): Cài đặt page with providers, retention days and reprocess-by-day (spec 10.6, 12)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-16; they amend the steps above):**
- **R-M6:** rename the local variable `retention` in `save_retention` so it does not shadow the imported module.

---

### Task 10: "Quên khoảng thời gian"

**Files:**
- Create: `brain/src/loopi_brain/forget.py`
- Create: `brain/src/loopi_brain/web/templates/_forget_preview.html`, `brain/src/loopi_brain/web/templates/_forgotten.html`
- Modify: `brain/src/loopi_brain/media.py` (`remove_files`), `brain/src/loopi_brain/web/settings_routes.py`, `brain/src/loopi_brain/web/templates/settings.html`
- Test: `brain/tests/test_forget.py`

**Interfaces:**
- Consumes: Task 1 tables (`forget_log.pending_files`), `local_day`; Task 4 `chapters.defer_build`, `state.lock_day`; Task 6 `media.inside`, `thumbs.thumb_rel`; Task 9 the Cài đặt page; P2a `media.audio_view_path`, `devices.new_id`.
- Produces:
  - `media.remove_files(media_root, rels: list[str]) -> tuple[int, list[str]]` (blocking; files deleted, paths that could not be; a missing file counts as gone; a path outside the media root is refused and reported). Task 11 uses it.
  - `forget.MAX_RANGE = 7 days`, `forget.ForgetCounts(items, keyframes, transcript_segments, moments, commitments, chapters, recaps, files, files_failed)`, `forget.check_range(start, end)` (ValueError: naive, empty, reversed or too long), `forget.days_touched(start, end, zone) -> list[date]`, `forget.preview(pool, start, end, zone) -> ForgetCounts` (changes nothing), `forget.forget_range(pool, settings, start, end) -> ForgetCounts`, `forget.retryable(media_root, rels) -> list[str]` (the paths inside the media root: Task 11 retries them).
  - `POST /settings/forget/preview` (form `start`, `end` as `datetime-local` values in local time) → counts + a confirmation form; `POST /settings/forget` (same + `confirm=yes`) → what went. Both need `HX-Request: true`; invalid ranges → 422.

What goes, in one transaction that first takes `lock_day` for every touched day (in day order): every item that overlaps `[start, end)` (whole, even a 60 s segment half inside; a zero-length marker inside the range), its keyframes, transcript segments and moments (embeddings included), every other moment starting in the range, the commitments sourced in those moments, and the recaps of every local day the range (or a deleted moment) touches; the chapters overlapping the range lose their title and summary at once, and every chapter of those days is reopened; an audit row (`forget_log`) keeps the range, the counts and the list of media paths to delete — no content. Because a summary or recap of those days is stored only under the same lock and only if no new forget row appeared (Tasks 4–5), a model answer written from the forgotten material is never stored. After the commit: the raw files, video MP4s, audio playback copies, keyframes and thumbnails are deleted (each checked with `media.inside`), the audit row gets the file counts and keeps only the paths that could not be deleted (Task 11's 03:00 run retries them), and a chapter build is asked for each touched day. At most 7 days per request, so one request stays a few seconds.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_forget.py`:

```python
import asyncio
import datetime as dt

import httpx
import pytest

from helpers import (
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_chapter,
    insert_item,
    insert_keyframe,
    insert_moment,
    jobs_by_lock,
    new_ulid,
    paired_device,
    write_jpeg,
)
from loopi_brain import forget, media
from loopi_brain.app import create_app

HX = {"HX-Request": "true"}
DAY = dt.date(2026, 10, 14)
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
HALF_PAST = NINE + dt.timedelta(minutes=30)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
async def web(pool, pki_settings, jobs_open):
    app = create_app(pki_settings, pool=pool, http_client=FakeOllama().client(), llm_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


def _write(settings, rel: str) -> str:
    path = settings.media_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return rel


async def _recorded(pool, settings, device_id, kind, start, seconds=60) -> dict:
    """An item with its raw file (and its view copy for video and audio) on disk."""
    item = await insert_item(pool, device_id, kind=kind, started_at=start, seconds=seconds)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,))
    files = [_write(settings, row["raw_path"])]
    if kind == "video":
        view = row["raw_path"].replace("raw/", "view/", 1).replace(".h264", ".mp4")
        files.append(_write(settings, view))
        async with pool.connection() as conn:
            await conn.execute("UPDATE item SET view_path = %s WHERE id = %s", (view, item))
    if kind == "audio":
        from pathlib import PurePosixPath
        files.append(_write(settings, str(media.audio_view_path(PurePosixPath(row["raw_path"])))))
    return {"id": item, "files": files}


async def _morning(pool, settings, device_id) -> dict:
    video = await _recorded(pool, settings, device_id, "video", NINE)
    kf = await insert_keyframe(pool, settings, video["id"], device_id, NINE + dt.timedelta(seconds=10))
    kf_path = (await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (kf,)))["path"]
    thumb = kf_path.replace("keyframes/", "thumbs/", 1)
    write_jpeg(settings.media_root / thumb, (32, 18))
    visual = await insert_moment(pool, device_id, kind="visual", ts=NINE + dt.timedelta(seconds=10), item_id=video["id"],
                                 text="A person reads")
    audio = await _recorded(pool, settings, device_id, "audio", NINE + dt.timedelta(minutes=5))
    segment = new_ulid()
    async with pool.connection() as conn:
        await conn.execute("UPDATE moment SET keyframe_id = %s WHERE id = %s", (kf, visual))
        await conn.execute(
            "INSERT INTO transcript_segment (id, item_id, device_id, start_ts, end_ts, lang, text, model_version)"
            " VALUES (%s, %s, %s, %s, %s, 'vi', 'mai đi khám răng', 'x')",
            (segment, audio["id"], device_id, NINE + dt.timedelta(minutes=5), NINE + dt.timedelta(minutes=5)))
    speech = await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(minutes=5),
                                 item_id=audio["id"], text="mai đi khám răng", lang="vi")
    marker = await _recorded(pool, settings, device_id, "marker", NINE + dt.timedelta(minutes=10), seconds=0)
    await insert_moment(pool, device_id, kind="marker", ts=NINE + dt.timedelta(minutes=10), item_id=marker["id"])
    later = await _recorded(pool, settings, device_id, "audio", NINE + dt.timedelta(hours=1))
    kept_moment = await insert_moment(pool, device_id, kind="speech", ts=NINE + dt.timedelta(hours=1),
                                      item_id=later["id"], text="chiều nay họp", lang="vi")
    inside = await insert_chapter(pool, DAY, NINE, NINE + dt.timedelta(minutes=10), title="Buổi sáng",
                                  summary="Hẹn đi khám răng.")
    outside = await insert_chapter(pool, DAY, NINE + dt.timedelta(hours=1), NINE + dt.timedelta(hours=1),
                                   title="Họp", summary="Chiều họp.")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'Khám răng', 'x')", (DAY,))
        await conn.execute("INSERT INTO commitment (id, day, text, source_moment_id, model_version)"
                           " VALUES ('C1', %s, 'Đi khám răng', %s, 'x')", (DAY, speech))
        await conn.execute("INSERT INTO commitment (id, day, text, source_moment_id, model_version)"
                           " VALUES ('C2', %s, 'Đi họp', %s, 'x')", (DAY, kept_moment))
    return {"files": video["files"] + [kf_path, thumb] + audio["files"] + marker["files"], "later": later,
            "inside": inside, "outside": outside, "kept_moment": kept_moment}


async def test_forgetting_removes_the_rows_files_and_derived_text_of_the_range(pool, pki_settings, dev, jobs_open):
    morning = await _morning(pool, pki_settings, dev["device_id"])
    counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert counts == forget.ForgetCounts(items=3, keyframes=1, transcript_segments=1, moments=3, commitments=1,
                                         chapters=1, recaps=1, files=7, files_failed=0)
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM item")] == [morning["later"]["id"]]
    assert await fetch_all(pool, "SELECT id FROM keyframe") == []
    assert await fetch_all(pool, "SELECT id FROM transcript_segment") == []
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM moment")] == [morning["kept_moment"]]
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM commitment")] == ["C2"]
    assert await fetch_all(pool, "SELECT day FROM day_recap") == []  # it quoted what is forgotten
    chapters = {r["id"]: r for r in await fetch_all(pool, "SELECT * FROM chapter")}
    assert chapters[morning["inside"]]["title"] is None and chapters[morning["inside"]]["summary"] is None
    assert chapters[morning["outside"]]["title"] == "Họp"
    assert not any(c["closed"] for c in chapters.values())  # the day is rebuilt
    assert all(not (pki_settings.media_root / rel).exists() for rel in morning["files"])
    assert all((pki_settings.media_root / rel).exists() for rel in morning["later"]["files"])
    [audit] = await fetch_all(pool, "SELECT * FROM forget_log")
    assert (audit["range_start"], audit["range_end"]) == (NINE, HALF_PAST)
    assert audit["counts"]["files"] == 7 and "khám" not in str(audit["counts"])  # counts only, no content
    assert audit["pending_files"] == []  # every file went
    assert "chapters:2026-10-14" in await jobs_by_lock(pool)


async def test_a_recording_that_straddles_the_edge_goes_whole_one_that_only_touches_it_stays(pool, pki_settings, dev):
    device_id = dev["device_id"]
    straddling = await insert_item(pool, device_id, kind="audio", started_at=NINE - dt.timedelta(seconds=30))
    before = await insert_item(pool, device_id, kind="video", started_at=NINE - dt.timedelta(minutes=1))
    marker = await insert_item(pool, device_id, kind="marker", started_at=NINE, seconds=0)
    at_end = await insert_item(pool, device_id, kind="marker", started_at=HALF_PAST, seconds=0)
    counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert counts.items == 2
    remaining = {r["id"] for r in await fetch_all(pool, "SELECT id FROM item")}
    assert remaining == {before, at_end} and not remaining & {straddling, marker}


async def test_the_preview_counts_and_changes_nothing(pool, pki_settings, dev):
    morning = await _morning(pool, pki_settings, dev["device_id"])
    counts = await forget.preview(pool, NINE, HALF_PAST, pki_settings.zone)
    assert counts == forget.ForgetCounts(items=3, keyframes=1, transcript_segments=1, moments=3, commitments=1,
                                         chapters=1, recaps=1)
    assert len(await fetch_all(pool, "SELECT id FROM item")) == 4
    assert all((pki_settings.media_root / rel).exists() for rel in morning["files"])


async def test_a_range_across_midnight_touches_both_days(pool, pki_settings, dev):
    midnight = dt.datetime(2026, 10, 14, 15, 0, tzinfo=dt.UTC)  # 00:00 on Oct 15 in Seoul
    for day, ts in ((DAY, midnight - dt.timedelta(hours=1)), (DAY + dt.timedelta(days=1), midnight)):
        await insert_chapter(pool, day, ts, ts, title="x", summary="y")
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO day_recap (day, markdown, model_version) VALUES (%s, 'r', 'x')", (day,))
    assert forget.days_touched(midnight - dt.timedelta(hours=2), midnight + dt.timedelta(hours=1),
                               pki_settings.zone) == [DAY, DAY + dt.timedelta(days=1)]
    assert forget.days_touched(NINE, midnight, pki_settings.zone) == [DAY]  # the end is not included
    counts = await forget.forget_range(pool, pki_settings, midnight - dt.timedelta(hours=2),
                                       midnight + dt.timedelta(hours=1))
    assert (counts.chapters, counts.recaps) == (2, 2)


async def test_ranges_are_checked(pool, pki_settings):
    for start, end in ((HALF_PAST, NINE), (NINE, NINE), (NINE, NINE + dt.timedelta(days=8))):
        with pytest.raises(ValueError):
            await forget.forget_range(pool, pki_settings, start, end)
    with pytest.raises(ValueError):
        forget.check_range(NINE.replace(tzinfo=None), HALF_PAST)


async def test_files_outside_the_media_root_are_never_deleted(pool, pki_settings, dev):
    outside = pki_settings.media_root.parent / "outside.opus"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"keep me")
    await insert_item(pool, dev["device_id"], kind="audio", started_at=NINE, raw_path="../outside.opus")
    counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    assert counts.items == 1 and counts.files_failed == 1  # the raw path leaves the media root: refused
    assert outside.read_bytes() == b"keep me"
    assert (await fetch_one(pool, "SELECT pending_files FROM forget_log"))["pending_files"] == []  # never retried


async def test_a_file_that_cannot_be_deleted_now_stays_listed_for_a_retry(pool, pki_settings, dev):
    item = await insert_item(pool, dev["device_id"], kind="marker", started_at=NINE, seconds=0)
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,)))["raw_path"]
    _write(pki_settings, raw)
    folder = (pki_settings.media_root / raw).parent
    folder.chmod(0o555)  # the file cannot be unlinked right now
    try:
        counts = await forget.forget_range(pool, pki_settings, NINE, HALF_PAST)
    finally:
        folder.chmod(0o755)
    assert (counts.items, counts.files, counts.files_failed) == (1, 0, 1)
    assert (await fetch_one(pool, "SELECT pending_files FROM forget_log"))["pending_files"] == [raw]


async def test_a_forget_waits_while_a_summary_or_recap_of_its_day_is_being_stored(pool, pki_settings, dev):
    from loopi_brain.pipeline.state import lock_day

    await insert_item(pool, dev["device_id"], kind="marker", started_at=NINE, seconds=0)
    async with pool.connection() as conn, conn.transaction():
        await lock_day(conn, DAY)  # a recap of that day is being stored
        task = asyncio.create_task(forget.forget_range(pool, pki_settings, NINE, HALF_PAST))
        await asyncio.sleep(0.3)
        assert not task.done()
    assert (await task).items == 1  # it went on once the recap was stored


async def test_the_settings_page_previews_then_forgets_after_confirmation(web, pool, pki_settings, dev):
    await _morning(pool, pki_settings, dev["device_id"])
    assert 'hx-post="/settings/forget/preview"' in (await web.get("/settings")).text
    form = {"start": "2026-10-14T09:00", "end": "2026-10-14T09:30"}
    assert (await web.post("/settings/forget/preview", data=form)).status_code == 403
    r = await web.post("/settings/forget/preview", data=form, headers=HX)
    assert r.status_code == 200 and "Từ 14/10/2026 09:00 đến 14/10/2026 09:30 sẽ bị xoá vĩnh viễn" in r.text
    assert "3 đoạn ghi, 1 khung hình, 1 đoạn transcript" in r.text
    assert 'name="confirm" value="yes"' in r.text and 'value="2026-10-14T09:00"' in r.text
    assert len(await fetch_all(pool, "SELECT id FROM item")) == 4
    assert (await web.post("/settings/forget", data=form, headers=HX)).status_code == 422  # not confirmed
    r = await web.post("/settings/forget", data={**form, "confirm": "yes"}, headers=HX)
    assert r.status_code == 200 and "Đã quên khoảng 14/10/2026 09:00 – 14/10/2026 09:30" in r.text
    assert "7 file" in r.text and len(await fetch_all(pool, "SELECT id FROM item")) == 1
    for bad in ({"start": "hôm qua", "end": "2026-10-14T09:30"}, {"start": "2026-10-14T10:00", "end": "2026-10-14T09:30"},
                {"start": "2026-09-01T00:00", "end": "2026-10-14T00:00"}):
        assert (await web.post("/settings/forget/preview", data=bad, headers=HX)).status_code == 422
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_forget.py -q`
Expected: FAIL — `ImportError: cannot import name 'forget' from 'loopi_brain'`.

- [ ] **Step 3: Delete media files safely**

Apply to `brain/src/loopi_brain/media.py`:

```diff
diff --git a/brain/src/loopi_brain/media.py b/brain/src/loopi_brain/media.py
--- a/brain/src/loopi_brain/media.py
+++ b/brain/src/loopi_brain/media.py
@@ -60,6 +60,22 @@ def inside(media_root: Path, rel: str | PurePosixPath) -> Path:
     return path
 
 
+def remove_files(media_root: Path, rels: list[str]) -> tuple[int, list[str]]:
+    """Delete media files (blocking); return (files deleted, paths that could not be deleted). A missing file
+    counts as gone; a path outside the media root is refused and reported, never touched."""
+    deleted, failed = 0, []
+    for rel in rels:
+        try:
+            path = inside(media_root, rel)
+            if path.is_file():
+                path.unlink()
+                deleted += 1
+        except (ValueError, OSError) as exc:
+            log.warning("could not delete media file %s: %s", rel, exc)
+            failed.append(rel)
+    return deleted, failed
+
+
 def _view_rel(raw_rel: PurePosixPath, suffix: str) -> PurePosixPath:
     """``raw/<device>/YYYY/MM/DD/<id>.<ext>`` -> ``view/<device>/YYYY/MM/DD/<id><suffix>``."""
     return PurePosixPath("view", *raw_rel.parts[1:]).with_suffix(suffix)
```

- [ ] **Step 4: Write the module**

Create `brain/src/loopi_brain/forget.py`:

```python
""""Quên khoảng thời gian" (spec §9.5, §11): forget everything recorded in a time range, for good.

In one transaction: the recordings (items) that touch the range, with their keyframes, transcript segments
and moments (embeddings included), every other moment of the range, the commitments sourced in it, and the
recaps of the days it touches; the chapters of those days are reopened and the ones that overlap the range
lose their title and summary (they quoted what is forgotten). The transaction holds each touched day's lock
(``state.lock_day``), so a summary or recap being stored waits for it — and then sees the new audit row and
stores nothing. An audit row records the range, the counts and the media paths still to delete, never any
content. After the commit the media files go: raw files, playback/view copies, keyframes and their thumbnails,
each checked to lie inside the media root; a file that could not be deleted stays listed on the audit row and
the nightly retention run tries again. The affected days' chapters are rebuilt.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

import anyio
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from . import devices, media, thumbs
from .config import Settings
from .days import local_day
from .pipeline import chapters as chapter_rules
from .pipeline.state import lock_day

log = logging.getLogger(__name__)
MAX_RANGE = dt.timedelta(days=7)  # one request stays a few seconds: ≈ 20 000 files and their rows at most
# An item touches [start, end) if it overlaps it, or is a zero-length item (a marker) inside it.
_ITEMS = "started_at < %(end)s AND (ended_at > %(start)s OR started_at >= %(start)s)"


@dataclass(frozen=True)
class ForgetCounts:
    items: int = 0
    keyframes: int = 0
    transcript_segments: int = 0
    moments: int = 0
    commitments: int = 0
    chapters: int = 0  # chapters that lose their title and summary
    recaps: int = 0
    files: int = 0
    files_failed: int = 0


def check_range(start: dt.datetime, end: dt.datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("the range needs time zones")
    if end <= start:
        raise ValueError("the end of the range must be after its start")
    if end - start > MAX_RANGE:
        raise ValueError(f"at most {MAX_RANGE.days} days at a time")


def days_touched(start: dt.datetime, end: dt.datetime, zone) -> list[dt.date]:
    """The local days that [start, end) touches."""
    first, last = local_day(start, zone), local_day(end - dt.timedelta(microseconds=1), zone)
    return [first + dt.timedelta(days=n) for n in range((last - first).days + 1)]


async def _targets(conn: AsyncConnection, start: dt.datetime, end: dt.datetime, lock: bool) -> dict:
    window = {"start": start, "end": end}
    cur = await conn.execute(
        f"SELECT id, kind, raw_path, view_path FROM item WHERE {_ITEMS}{' FOR UPDATE' if lock else ''}", window
    )
    items = await cur.fetchall()
    item_ids = [r["id"] for r in items]
    cur = await conn.execute(
        "SELECT id, path FROM keyframe WHERE item_id = ANY(%(items)s) OR (ts >= %(start)s AND ts < %(end)s)",
        {**window, "items": item_ids},
    )
    keyframes = await cur.fetchall()
    cur = await conn.execute(
        "SELECT id, ts_start FROM moment WHERE item_id = ANY(%(items)s) OR keyframe_id = ANY(%(keyframes)s)"
        " OR (ts_start >= %(start)s AND ts_start < %(end)s)",
        {**window, "items": item_ids, "keyframes": [k["id"] for k in keyframes]},
    )
    moments = await cur.fetchall()
    return {"items": items, "keyframes": keyframes, "moments": moments}


async def _derived(conn: AsyncConnection, targets: dict, days: list[dt.date], start, end) -> dict:
    moment_ids = [m["id"] for m in targets["moments"]]
    cur = await conn.execute("SELECT count(*) AS n FROM commitment WHERE source_moment_id = ANY(%s)", (moment_ids,))
    commitments = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT count(*) AS n FROM chapter WHERE day = ANY(%s) AND ts_start < %s AND ts_end >= %s", (days, end, start)
    )
    chapters = (await cur.fetchone())["n"]
    cur = await conn.execute("SELECT count(*) AS n FROM day_recap WHERE day = ANY(%s)", (days,))
    return {"commitments": commitments, "chapters": chapters, "recaps": (await cur.fetchone())["n"]}


async def preview(pool: AsyncConnectionPool, start: dt.datetime, end: dt.datetime, zone) -> ForgetCounts:
    """What ``forget_range`` would delete; changes nothing."""
    check_range(start, end)
    async with pool.connection() as conn:
        targets = await _targets(conn, start, end, lock=False)
        derived = await _derived(conn, targets, days_touched(start, end, zone), start, end)
        item_ids = [r["id"] for r in targets["items"]]
        cur = await conn.execute("SELECT count(*) AS n FROM transcript_segment WHERE item_id = ANY(%s)", (item_ids,))
        segments = (await cur.fetchone())["n"]
    return ForgetCounts(items=len(targets["items"]), keyframes=len(targets["keyframes"]), transcript_segments=segments,
                        moments=len(targets["moments"]), **derived)


def _files(targets: dict) -> list[str]:
    """Every media file of the forgotten rows (relative to the media root)."""
    paths: list[str] = []
    for item in targets["items"]:
        paths.append(item["raw_path"])
        if item["kind"] == "video" and item["view_path"]:
            paths.append(item["view_path"])
        if item["kind"] == "audio":
            paths.append(str(media.audio_view_path(PurePosixPath(item["raw_path"]))))
    for keyframe in targets["keyframes"]:
        paths.append(keyframe["path"])
        try:
            paths.append(str(thumbs.thumb_rel(PurePosixPath(keyframe["path"]))))
        except ValueError:
            pass
    return paths


def retryable(media_root, rels: list[str]) -> list[str]:
    """The paths worth trying again later: inside the media root (others are refused for ever)."""
    out = []
    for rel in rels:
        try:
            media.inside(media_root, rel)
        except ValueError:
            continue
        out.append(rel)
    return out


async def forget_range(pool: AsyncConnectionPool, settings: Settings, start: dt.datetime, end: dt.datetime) -> ForgetCounts:
    """Forget [start, end) for good; return what went."""
    check_range(start, end)
    zone = settings.zone
    days = days_touched(start, end, zone)
    async with pool.connection() as conn, conn.transaction():
        for day in days:  # in day order: no deadlock with another forget
            await lock_day(conn, day)
        targets = await _targets(conn, start, end, lock=True)
        extra = sorted({local_day(m["ts_start"], zone) for m in targets["moments"]} - set(days))
        for day in extra:  # a recording that started on the previous day
            await lock_day(conn, day)
        days = sorted(set(days) | set(extra))
        derived = await _derived(conn, targets, days, start, end)
        item_ids = [r["id"] for r in targets["items"]]
        moment_ids = [m["id"] for m in targets["moments"]]
        await conn.execute("DELETE FROM commitment WHERE source_moment_id = ANY(%s)", (moment_ids,))
        await conn.execute("DELETE FROM moment WHERE id = ANY(%s)", (moment_ids,))
        cur = await conn.execute("DELETE FROM transcript_segment WHERE item_id = ANY(%s)", (item_ids,))
        segments = cur.rowcount
        await conn.execute("DELETE FROM keyframe WHERE id = ANY(%s)", ([k["id"] for k in targets["keyframes"]],))
        await conn.execute("DELETE FROM item WHERE id = ANY(%s)", (item_ids,))
        await conn.execute(
            "UPDATE chapter SET title = NULL, summary = NULL, summary_signature = NULL, model_version = NULL,"
            " updated_at = now() WHERE day = ANY(%s) AND ts_start < %s AND ts_end >= %s",
            (days, end, start),
        )
        await conn.execute("UPDATE chapter SET closed = false WHERE day = ANY(%s)", (days,))
        await conn.execute("DELETE FROM day_recap WHERE day = ANY(%s)", (days,))
        counts = ForgetCounts(items=len(item_ids), keyframes=len(targets["keyframes"]), transcript_segments=segments,
                              moments=len(moment_ids), **derived)
        log_id = devices.new_id()
        files = _files(targets)
        await conn.execute(
            "INSERT INTO forget_log (id, range_start, range_end, counts, pending_files) VALUES (%s, %s, %s, %s, %s)",
            (log_id, start, end, Jsonb(asdict(counts)), Jsonb(files)),
        )
    deleted, failed = await anyio.to_thread.run_sync(media.remove_files, settings.media_root, files)
    counts = ForgetCounts(**{**asdict(counts), "files": deleted, "files_failed": len(failed)})
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE forget_log SET counts = %s, pending_files = %s WHERE id = %s",
            (Jsonb(asdict(counts)), Jsonb(retryable(settings.media_root, failed)), log_id),
        )
    for day in days:
        await chapter_rules.defer_build(pool, day, zone)
    log.info("forgot %s .. %s: %s", start.isoformat(), end.isoformat(), asdict(counts))
    return counts
```

- [ ] **Step 5: Add the preview and the confirmation to Cài đặt**

Apply to `brain/src/loopi_brain/web/settings_routes.py` and `brain/src/loopi_brain/web/templates/settings.html`:

```diff
diff --git a/brain/src/loopi_brain/web/settings_routes.py b/brain/src/loopi_brain/web/settings_routes.py
--- a/brain/src/loopi_brain/web/settings_routes.py
+++ b/brain/src/loopi_brain/web/settings_routes.py
@@ -1,4 +1,5 @@
-"""The Cài đặt page (spec §10.6): providers (read only), retention, reprocessing a day, failed jobs."""
+"""The Cài đặt page (spec §10.6): providers (read only), retention, "Quên khoảng thời gian", reprocessing a day,
+failed jobs."""
 from __future__ import annotations
 
 import datetime as dt
@@ -7,7 +8,7 @@ from fastapi import APIRouter, Depends, Form, HTTPException, Request
 from fastapi.responses import HTMLResponse
 from pydantic import ValidationError
 
-from .. import reprocess, store
+from .. import forget, reprocess, store
 from ..pipeline import status
 from ..providers.config import TASKS, ProvidersConfigError, load_providers
 from ..providers.router import model_version
@@ -83,3 +84,34 @@ async def reprocess_day(request: Request, day: dt.date = Form(...), what: list[s
         request, "_reprocessed.html", {"day": day, "r": result, "what": [CHOICE_LABELS[c] for c in reprocess.CHOICES
                                                                        if c in what]}
     )
+
+
+def _local_range(start: str, end: str, zone) -> tuple[dt.datetime, dt.datetime]:
+    """Two ``datetime-local`` form values (local time, no zone) as an aware range; 422 when unusable."""
+    try:
+        first = dt.datetime.fromisoformat(start).replace(tzinfo=zone)
+        last = dt.datetime.fromisoformat(end).replace(tzinfo=zone)
+        forget.check_range(first, last)
+    except ValueError as exc:
+        raise HTTPException(status_code=422, detail=str(exc)) from exc
+    return first, last
+
+
+@router.post("/settings/forget/preview", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
+async def forget_preview(request: Request, start: str = Form(...), end: str = Form(...)):
+    zone = request.app.state.settings.zone
+    first, last = _local_range(start, end, zone)
+    counts = await forget.preview(request.app.state.pool, first, last, zone)
+    return TEMPLATES.TemplateResponse(
+        request, "_forget_preview.html", {"start": start, "end": end, "first": first, "last": last, "c": counts}
+    )
+
+
+@router.post("/settings/forget", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
+async def forget_now(request: Request, start: str = Form(...), end: str = Form(...), confirm: str = Form("")):
+    if confirm != "yes":
+        raise HTTPException(status_code=422, detail="not confirmed")
+    state = request.app.state
+    first, last = _local_range(start, end, state.settings.zone)
+    counts = await forget.forget_range(state.pool, state.settings, first, last)
+    return TEMPLATES.TemplateResponse(request, "_forgotten.html", {"first": first, "last": last, "c": counts})
```

```diff
diff --git a/brain/src/loopi_brain/web/templates/settings.html b/brain/src/loopi_brain/web/templates/settings.html
--- a/brain/src/loopi_brain/web/templates/settings.html
+++ b/brain/src/loopi_brain/web/templates/settings.html
@@ -31,6 +31,16 @@
   <p class="muted">Khi bật, lúc 03:00 mỗi đêm file thô cũ hơn số ngày này bị xoá vĩnh viễn (chỉ những đoạn đã phân tích xong). Keyframe, transcript, chương và recap được giữ mãi.</p>
   <div id="retention-result"></div>
 </section>
+<section class="card" id="forget">
+  <h2>Quên khoảng thời gian</h2>
+  <form hx-post="/settings/forget/preview" hx-target="#forget-result">
+    <label>Từ <input type="datetime-local" name="start" required></label>
+    <label>đến <input type="datetime-local" name="end" required></label>
+    <button>Xem trước</button>
+  </form>
+  <p class="muted">Xoá vĩnh viễn mọi đoạn ghi, khung hình, transcript và khoảnh khắc trong khoảng này (tối đa 7 ngày một lần).</p>
+  <div id="forget-result"></div>
+</section>
 <section class="card" id="reprocess">
   <h2>Xử lý lại một ngày</h2>
   <form hx-post="/settings/reprocess" hx-target="#reprocess-result" hx-confirm="Xử lý lại ngày này bằng các model hiện tại?">
```

Create `brain/src/loopi_brain/web/templates/_forget_preview.html`:

```html
<div class="card">
  <p>Từ {{ first.strftime("%d/%m/%Y %H:%M") }} đến {{ last.strftime("%d/%m/%Y %H:%M") }} sẽ bị xoá vĩnh viễn:
     {{ c.items }} đoạn ghi, {{ c.keyframes }} khung hình, {{ c.transcript_segments }} đoạn transcript,
     {{ c.moments }} khoảnh khắc, {{ c.commitments }} việc cần làm; {{ c.chapters }} chương mất tóm tắt và
     {{ c.recaps }} recap sẽ được viết lại.</p>
  <p class="muted">Dữ liệu thiết bị chưa gửi lên brain (ví dụ khi mất mạng) không bị xoá.</p>
  <form hx-post="/settings/forget" hx-target="#forget-result" hx-confirm="Xoá vĩnh viễn? Không thể hoàn tác.">
    <input type="hidden" name="start" value="{{ start }}"><input type="hidden" name="end" value="{{ end }}">
    <input type="hidden" name="confirm" value="yes">
    <button>Quên vĩnh viễn</button>
  </form>
</div>
```

Create `brain/src/loopi_brain/web/templates/_forgotten.html`:

```html
<span>Đã quên khoảng {{ first.strftime("%d/%m/%Y %H:%M") }} – {{ last.strftime("%d/%m/%Y %H:%M") }}:
{{ c.items }} đoạn ghi, {{ c.keyframes }} khung hình, {{ c.transcript_segments }} đoạn transcript, {{ c.moments }} khoảnh khắc,
{{ c.commitments }} việc cần làm, {{ c.files }} file{% if c.files_failed %} <span class="err">({{ c.files_failed }} file không xoá được, xem log)</span>{% endif %}.
Chương và recap của những ngày này sẽ được làm lại.</span>
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_forget.py tests/test_media.py -q`
Expected: `25 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `479 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/forget.py brain/src/loopi_brain/media.py brain/src/loopi_brain/web/settings_routes.py \
  brain/src/loopi_brain/web/templates/settings.html brain/src/loopi_brain/web/templates/_forget_preview.html \
  brain/src/loopi_brain/web/templates/_forgotten.html brain/tests/test_forget.py
git commit -F - <<'EOF'
feat(brain): "Quên khoảng thời gian" deletes a range for good, with preview and audit (spec 9.5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-16; they amend the steps above):**
- **R-I3:** one rule for "which files belong to an item": `media.item_files(kind, raw_path, view_path) -> list[str]` (raw path; `view_path` for video; `media.audio_view_path` for audio) next to `remove_files`; `forget` uses it (Task 11's retention uses it too).
- **R-M15:** an item path that cannot be resolved inside the media root is logged (warning naming the item), never skipped silently.
- **R-M3 (part):** deleting the day's recap on forget is a deliberate deviation from §9.5 (ruling R24): the recap is regenerated from what remains.

---

### Task 11: Retention at 03:00 (D20) — off until the owner turns it on

**Files:**
- Create: `brain/src/loopi_brain/pipeline/retention.py`
- Modify: `brain/src/loopi_brain/pipeline/jobs.py`, `brain/src/loopi_brain/pipeline/status.py`, `brain/src/loopi_brain/pipeline/asr.py` (docstring)
- Modify: `brain/src/loopi_brain/web/settings_routes.py`, `brain/src/loopi_brain/web/templates/settings.html` (what retention would delete)
- Test: `brain/tests/test_retention.py`

**Interfaces:**
- Consumes: Task 1 `item.raw_deleted_at`, `forget_log.pending_files`, `local_now`; Task 9 `store.get_retention` (`enabled`), `settings_context`; Task 10 `media.remove_files`, `forget.retryable`; P2a `media.audio_view_path`, `jobs`, `get_context`.
- Produces:
  - `jobs.APPLY_RETENTION = "apply_retention"` (periodic, cron `"0 * * * *"`, queue `light`, in `AUTO_RERUN_TASKS`; label "Dọn dữ liệu cũ", "tự chạy lại theo lịch").
  - `retention.apply_retention(pool, settings, now=None) -> RetentionResult(video, audio, files, files_failed)` (all zero while retention is off); `retention.preview_retention(pool, now=None) -> {"video": {"recordings", "bytes"}, "audio": {…}}` (read only); `retention.retry_forgotten_files(pool, settings) -> int`; `retention.due(timestamp: int | None, zone) -> bool` (the tick's local hour is `RETENTION_HOUR = 3`); `BATCH = 500`, `MAX_BATCHES = 40` per kind per run.
  - Cài đặt shows "Nếu chạy bây giờ sẽ xoá: video thô N đoạn (x GB), audio thô M đoạn (y GB)." next to the on/off state.

Rules: **nothing is deleted while retention is off (the default)**; the owner turns it on in Cài đặt after seeing the preview (Task 14 stops to ask). When on, for each kind (video, audio), items with `analysis_state` `done` or `skipped`, no `raw_deleted_at`, and `ended_at` older than the kept days lose their raw file and the copy the web plays (video `view_path`, audio `view/…/<id>.opus`). Each batch runs in one transaction that holds its rows (`FOR UPDATE SKIP LOCKED`) while their files go, so a reprocess setting an item back to `pending` waits and then sees `raw_deleted_at`; a row someone else holds is left for the next night. A row whose file cannot be deleted keeps its mark empty and is tried again the next night. Markers, pending items, keyframes, thumbnails, transcripts, moments, chapters and recaps are never touched. Idempotent. The same 03:00 tick, on or off, first retries the media paths a "Quên khoảng thời gian" left on its audit row.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_retention.py`:

```python
import asyncio
import datetime as dt
import logging
from pathlib import PurePosixPath

import httpx
import pytest
from psycopg.types.json import Jsonb

from helpers import FakeOllama, fetch_all, fetch_one, insert_item, insert_keyframe, make_ctx, paired_device
from loopi_brain import media, store
from loopi_brain.app import create_app
from loopi_brain.pipeline import context, jobs, retention, status

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)
NOW = NINE + dt.timedelta(days=8)  # NINE's recordings are 8 days old


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
async def on(pool):
    """The owner turned retention on in Cài đặt (it ships off)."""
    await store.set_retention(pool, store.Retention(enabled=True))


def _write(settings, rel: str) -> str:
    path = settings.media_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return rel


async def _recording(pool, settings, device_id, kind, start, state="done") -> dict:
    item = await insert_item(pool, device_id, kind=kind, started_at=start)
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,)))["raw_path"]
    files = [_write(settings, raw)]
    if kind == "video":
        view = raw.replace("raw/", "view/", 1).replace(".h264", ".mp4")
        files.append(_write(settings, view))
    else:
        view = None
        if kind == "audio":
            files.append(_write(settings, str(media.audio_view_path(PurePosixPath(raw)))))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = %s, view_path = %s WHERE id = %s", (state, view, item))
    return {"id": item, "files": files}


def _exists(settings, rec) -> list[bool]:
    return [(settings.media_root / rel).exists() for rel in rec["files"]]


async def test_old_finished_recordings_lose_their_raw_files_and_keep_their_rows(pool, pki_settings, dev, on):
    device_id = dev["device_id"]
    video = await _recording(pool, pki_settings, device_id, "video", NINE)
    keyframe = await insert_keyframe(pool, pki_settings, video["id"], device_id, NINE)
    audio = await _recording(pool, pki_settings, device_id, "audio", NINE)
    pending = await _recording(pool, pki_settings, device_id, "audio", NINE, state="pending")
    skipped = await _recording(pool, pki_settings, device_id, "video", NINE, state="skipped")
    recent = await _recording(pool, pki_settings, device_id, "video", NOW - dt.timedelta(days=6))
    marker = await _recording(pool, pki_settings, device_id, "marker", NINE)
    result = await retention.apply_retention(pool, pki_settings, now=NOW)
    assert result == retention.RetentionResult(video=2, audio=1, files=6, files_failed=0)
    assert _exists(pki_settings, video) == [False, False] and _exists(pki_settings, audio) == [False, False]
    assert _exists(pki_settings, skipped) == [False, False]
    for kept in (pending, recent, marker):  # analysis not finished, too recent, markers never
        assert all(_exists(pki_settings, kept))
    rows = {r["id"]: r for r in await fetch_all(pool, "SELECT id, raw_deleted_at, view_path FROM item")}
    assert rows[video["id"]]["raw_deleted_at"] is not None and rows[video["id"]]["view_path"] is None
    assert rows[pending["id"]]["raw_deleted_at"] is None and rows[marker["id"]]["raw_deleted_at"] is None
    kf_path = (await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (keyframe,)))["path"]
    assert (pki_settings.media_root / kf_path).exists()  # keyframes are kept for ever
    assert await retention.apply_retention(pool, pki_settings, now=NOW) == retention.RetentionResult()  # idempotent


async def test_the_kept_days_come_from_the_settings(pool, pki_settings, dev):
    video = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    audio = await _recording(pool, pki_settings, dev["device_id"], "audio", NINE)
    await store.set_retention(pool, store.Retention(video_days=7, audio_days=30, enabled=True))
    result = await retention.apply_retention(pool, pki_settings, now=NOW)
    assert (result.video, result.audio) == (1, 0)
    assert _exists(pki_settings, video) == [False, False] and all(_exists(pki_settings, audio))


async def test_a_file_that_cannot_be_deleted_is_tried_again_next_night(pool, pki_settings, dev, on, caplog):
    outside = pki_settings.media_root.parent / "outside.h264"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"keep")
    item = await insert_item(pool, dev["device_id"], kind="video", started_at=NINE, raw_path="../outside.h264")
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (item,))
    with caplog.at_level(logging.WARNING):
        result = await retention.apply_retention(pool, pki_settings, now=NOW)
    assert result == retention.RetentionResult(files_failed=1) and outside.exists()
    assert (await fetch_one(pool, "SELECT raw_deleted_at FROM item"))["raw_deleted_at"] is None
    assert "could not delete media file ../outside.h264" in caplog.text


async def test_a_large_backlog_is_taken_in_bounded_batches(pool, pki_settings, dev, on, monkeypatch):
    monkeypatch.setattr(retention, "BATCH", 2)
    monkeypatch.setattr(retention, "MAX_BATCHES", 2)
    for n in range(5):
        await _recording(pool, pki_settings, dev["device_id"], "video", NINE + dt.timedelta(minutes=n))
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).video == 4
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).video == 1


def test_the_hourly_tick_works_only_at_three_local_time(pki_settings):
    three_in_seoul = int(dt.datetime(2026, 10, 14, 18, 0, tzinfo=dt.UTC).timestamp())
    assert retention.due(three_in_seoul, pki_settings.zone)
    assert not retention.due(three_in_seoul - 3600, pki_settings.zone)
    periodic = {(p.task.name, p.cron) for p in jobs.job_app.periodic_registry.periodic_tasks.values()}
    assert (jobs.APPLY_RETENTION, "0 * * * *") in periodic


async def test_the_job_applies_retention_only_on_the_three_oclock_tick(pool, pki_settings, dev, on):
    video = await _recording(pool, pki_settings, dev["device_id"], "video", NINE - dt.timedelta(days=30))
    context.set_context(make_ctx(pool, pki_settings))
    try:
        await retention.apply_retention_job(timestamp=int(dt.datetime(2026, 10, 14, 17, 0, tzinfo=dt.UTC).timestamp()))
        assert all(_exists(pki_settings, video))
        await retention.apply_retention_job(timestamp=int(dt.datetime(2026, 10, 14, 18, 0, tzinfo=dt.UTC).timestamp()))
        assert not any(_exists(pki_settings, video))
    finally:
        context.set_context(None)


async def test_nothing_is_deleted_until_the_owner_turns_retention_on(pool, pki_settings, dev, caplog):
    video = await _recording(pool, pki_settings, dev["device_id"], "video", NINE)
    audio = await _recording(pool, pki_settings, dev["device_id"], "audio", NINE)
    await _recording(pool, pki_settings, dev["device_id"], "audio", NOW - dt.timedelta(days=1))  # too recent
    assert await retention.preview_retention(pool, now=NOW) == {
        "video": {"recordings": 1, "bytes": 1}, "audio": {"recordings": 1, "bytes": 1}}  # what it would delete
    with caplog.at_level(logging.INFO, logger="loopi_brain.pipeline.retention"):
        assert await retention.apply_retention(pool, pki_settings, now=NOW) == retention.RetentionResult()
    assert all(_exists(pki_settings, video)) and all(_exists(pki_settings, audio))
    assert "retention is off" in caplog.text


async def test_a_recording_someone_else_holds_is_left_for_the_next_night(pool, pki_settings, dev, on):
    held = await _recording(pool, pki_settings, dev["device_id"], "audio", NINE)
    free = await _recording(pool, pki_settings, dev["device_id"], "audio", NINE + dt.timedelta(minutes=1))
    async with pool.connection() as conn, conn.transaction():  # e.g. "Xử lý lại" is resetting it right now
        await conn.execute("SELECT 1 FROM item WHERE id = %s FOR UPDATE", (held["id"],))
        result = await asyncio.wait_for(retention.apply_retention(pool, pki_settings, now=NOW), 10)
    assert result.audio == 1 and all(_exists(pki_settings, held)) and not any(_exists(pki_settings, free))
    assert (await retention.apply_retention(pool, pki_settings, now=NOW)).audio == 1  # the next night


async def test_files_a_forget_left_behind_are_deleted_at_three_even_with_retention_off(pool, pki_settings, dev):
    left = _write(pki_settings, "raw/x/2026/10/14/left.opus")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO forget_log (id, range_start, range_end, counts, pending_files)"
                           " VALUES ('F1', %s, %s, '{}', %s)", (NINE, NINE + dt.timedelta(hours=1), Jsonb([left])))
    context.set_context(make_ctx(pool, pki_settings))
    try:
        await retention.apply_retention_job(timestamp=int(dt.datetime(2026, 10, 14, 18, 0, tzinfo=dt.UTC).timestamp()))
    finally:
        context.set_context(None)
    assert not (pki_settings.media_root / left).exists()
    assert (await fetch_one(pool, "SELECT pending_files FROM forget_log"))["pending_files"] == []


async def test_the_settings_page_shows_what_retention_would_delete(pool, pki_settings, dev, jobs_open):
    await _recording(pool, pki_settings, dev["device_id"], "video", dt.datetime.now(dt.UTC) - dt.timedelta(days=30))
    app = create_app(pki_settings, pool=pool, http_client=FakeOllama().client(), llm_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        text = (await c.get("/settings")).text
    assert "Nếu chạy bây giờ sẽ xoá: video thô 1 đoạn (0.0 GB)" in text and "audio thô 0 đoạn" in text


async def test_a_failed_retention_run_waits_for_its_schedule(pool, jobs_open):
    async with pool.connection() as conn:
        job_id = await jobs.enqueue(conn, jobs.APPLY_RETENTION, queue="light", queueing_lock="test-retention")
        await conn.execute("UPDATE procrastinate_jobs SET status = 'failed', attempts = 1 WHERE id = %s", (job_id,))
    [row] = (await status.pipeline_status(pool))["failed"]
    assert (row["label"], row["retryable"], row["rerun"]) == ("Dọn dữ liệu cũ", False, "tự chạy lại theo lịch")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_retention.py -q`
Expected: FAIL — `ImportError: cannot import name 'retention' from 'loopi_brain.pipeline'`.

- [ ] **Step 3: Register the task**

Apply to `brain/src/loopi_brain/pipeline/jobs.py`, `brain/src/loopi_brain/pipeline/status.py` and `brain/src/loopi_brain/pipeline/asr.py`:

```diff
diff --git a/brain/src/loopi_brain/pipeline/jobs.py b/brain/src/loopi_brain/pipeline/jobs.py
--- a/brain/src/loopi_brain/pipeline/jobs.py
+++ b/brain/src/loopi_brain/pipeline/jobs.py
@@ -18,6 +18,7 @@ TASK_MODULES = [
     "loopi_brain.pipeline.asr",
     "loopi_brain.pipeline.chapters",
     "loopi_brain.pipeline.recap",
+    "loopi_brain.pipeline.retention",
 ]
 
 ANALYZE_VIDEO = "analyze_video"
@@ -32,12 +33,14 @@ PLAN_CHAPTERS = "plan_chapters"
 BUILD_CHAPTERS = "build_chapters"
 PLAN_RECAPS = "plan_recaps"
 WRITE_RECAP = "write_recap"
+APPLY_RETENTION = "apply_retention"
 PIPELINE_TASKS = (ANALYZE_VIDEO, CAPTION_KEYFRAME, TRANSCRIBE_AUDIO, INDEX_MARKER, EMBED_MOMENTS)
 # Runs that start again by themselves (the dispatcher asks for embedding runs; the scheduler starts
 # each periodic tick; the hourly planner asks for each day's chapters again), so a failed one is never
 # retried and only the newest failed one is kept.
 AUTO_RERUN_TASKS = (
     EMBED_MOMENTS, DISPATCH_PENDING, RETRY_STALLED, REMOVE_OLD_JOBS, PLAN_CHAPTERS, BUILD_CHAPTERS, PLAN_RECAPS,
+    APPLY_RETENTION,
 )
 # The text models run one job at a time (chapters, recaps): each one takes most of the CPU. They also wait behind
 # every live job (video, captions, ASR, markers, embeddings: priority 0) for a free worker slot, so recordings keep
```

```diff
diff --git a/brain/src/loopi_brain/pipeline/status.py b/brain/src/loopi_brain/pipeline/status.py
--- a/brain/src/loopi_brain/pipeline/status.py
+++ b/brain/src/loopi_brain/pipeline/status.py
@@ -30,6 +30,7 @@ TASK_LABELS = {
     jobs.BUILD_CHAPTERS: "Chia chương",
     jobs.PLAN_RECAPS: "Lên lịch recap",
     jobs.WRITE_RECAP: "Viết recap",
+    jobs.APPLY_RETENTION: "Dọn dữ liệu cũ",
 }
 _LATENCY = (
     "SELECT count(*) AS n, avg(s) AS avg, percentile_cont(0.95) WITHIN GROUP (ORDER BY s) AS p95 FROM ({}) t"
```

```diff
diff --git a/brain/src/loopi_brain/pipeline/asr.py b/brain/src/loopi_brain/pipeline/asr.py
--- a/brain/src/loopi_brain/pipeline/asr.py
+++ b/brain/src/loopi_brain/pipeline/asr.py
@@ -1,8 +1,7 @@
 """transcribe_audio: level normalisation, then Silero VAD + faster-whisper per speech region (spec §9.2, D19).
 
-Step 4 (deleting raw audio after transcription) is retention, which lands in P2b: P2a deletes no media.
-When P2b deletes a raw recording it must delete the playback copy ``view/<device>/YYYY/MM/DD/<id>.opus``
-(made on the first play, see ``media.normalise_audio``) with it, or the family's audio stays on disk.
+Step 4: the raw audio is kept like video (spec D20, 7 days by default); ``retention.py`` then deletes it
+together with its playback copy ``view/<device>/YYYY/MM/DD/<id>.opus`` (see ``media.normalise_audio``).
 """
 from __future__ import annotations
 
```

- [ ] **Step 4: Write the module**

Create `brain/src/loopi_brain/pipeline/retention.py`:

```python
"""Retention (spec §9.5, D20; §9.2 "03:00: áp dụng chính sách lưu trữ").

Every night at 03:00 local time — **only once the owner turned it on in Cài đặt** (it ships off; the page shows
what it would delete first) — raw recordings older than the kept days (7 and 7 by default) lose their raw file
and the copy the web plays (video MP4, normalised audio): only recordings whose analysis is finished, never
markers. The item row stays, marked ``raw_deleted_at`` so the pages say "đã hết hạn"; keyframes, thumbnails,
transcripts, captions, moments, chapters and recaps are never deleted here. The same nightly run (on or off)
retries the media files a "Quên khoảng thời gian" could not delete at once.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import PurePosixPath

import anyio
from psycopg_pool import AsyncConnectionPool

from psycopg.types.json import Jsonb

from .. import forget, media, store
from ..config import Settings
from ..days import local_now
from . import jobs
from .context import get_context
from .jobs import job_app

log = logging.getLogger(__name__)
RETENTION_HOUR = 3
BATCH = 500
MAX_BATCHES = 40  # at most 20 000 recordings of each kind per night; a bigger backlog takes more nights


_DUE = (
    "kind = %s AND raw_deleted_at IS NULL AND analysis_state IN ('done', 'skipped') AND ended_at < %s"
)


@dataclass(frozen=True)
class RetentionResult:
    video: int = 0
    audio: int = 0
    files: int = 0
    files_failed: int = 0


def _paths(kind: str, row: dict) -> list[str]:
    paths = [row["raw_path"]]
    if kind == "video" and row["view_path"]:
        paths.append(row["view_path"])
    if kind == "audio":
        paths.append(str(media.audio_view_path(PurePosixPath(row["raw_path"]))))
    return paths


def _remove(settings: Settings, kind: str, rows: list[dict]) -> tuple[list[str], int, int]:
    """Delete each row's files; (ids whose files are all gone, files deleted, files that could not be deleted)."""
    done, deleted, failed = [], 0, 0
    for row in rows:
        count, failures = media.remove_files(settings.media_root, _paths(kind, row))
        deleted += count
        failed += len(failures)
        if not failures:
            done.append(row["id"])
    return done, deleted, failed


async def preview_retention(pool: AsyncConnectionPool, now: dt.datetime | None = None) -> dict:
    """What a run would delete now with the kept days in Cài đặt (read only): per kind, recordings and bytes."""
    now = now or dt.datetime.now(dt.UTC)
    retention = await store.get_retention(pool)
    out = {}
    async with pool.connection() as conn:
        for kind, days in (("video", retention.video_days), ("audio", retention.audio_days)):
            cur = await conn.execute(
                f"SELECT count(*) AS recordings, coalesce(sum(size_bytes), 0)::bigint AS bytes FROM item WHERE {_DUE}",
                (kind, now - dt.timedelta(days=days)),
            )
            out[kind] = await cur.fetchone()
    return out


async def apply_retention(pool: AsyncConnectionPool, settings: Settings, now: dt.datetime | None = None) -> RetentionResult:
    """Delete the raw files of finished recordings older than the kept days, in batches; return the counts.
    Nothing while retention is off (the default)."""
    now = now or dt.datetime.now(dt.UTC)
    retention = await store.get_retention(pool)
    if not retention.enabled:
        log.info("retention is off (Cài đặt): no recording deleted")
        return RetentionResult()
    counts = {"video": 0, "audio": 0}
    files = failed_files = 0
    for kind, days in (("video", retention.video_days), ("audio", retention.audio_days)):
        cutoff = now - dt.timedelta(days=days)
        skip: list[str] = []  # rows whose files could not be deleted: retried next night, not in this loop
        for _ in range(MAX_BATCHES):
            # Rows stay locked while their files go: a reprocess (which sets an item back to pending) waits, then
            # sees raw_deleted_at; a row someone else holds is left for the next night.
            async with pool.connection() as conn, conn.transaction():
                cur = await conn.execute(
                    f"SELECT id, raw_path, view_path FROM item WHERE {_DUE} AND NOT (id = ANY(%s))"
                    " ORDER BY ended_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
                    (kind, cutoff, skip, BATCH),
                )
                rows = await cur.fetchall()
                if not rows:
                    break
                done, deleted, failed = await anyio.to_thread.run_sync(_remove, settings, kind, rows)
                await conn.execute(
                    "UPDATE item SET raw_deleted_at = now(), view_path = NULL WHERE id = ANY(%s)", (done,)
                )
            skip.extend(sorted({row["id"] for row in rows} - set(done)))
            counts[kind] += len(done)
            files += deleted
            failed_files += failed
    result = RetentionResult(counts["video"], counts["audio"], files, failed_files)
    log.info("retention (video %d days, audio %d days): %s", retention.video_days, retention.audio_days, result)
    return result


async def retry_forgotten_files(pool: AsyncConnectionPool, settings: Settings) -> int:
    """Delete the media files "Quên khoảng thời gian" could not delete at once; return how many went."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id, pending_files FROM forget_log WHERE pending_files <> '[]'::jsonb")
        rows = await cur.fetchall()
    total = 0
    for row in rows:
        deleted, failed = await anyio.to_thread.run_sync(media.remove_files, settings.media_root, row["pending_files"])
        total += deleted
        async with pool.connection() as conn:
            await conn.execute("UPDATE forget_log SET pending_files = %s WHERE id = %s",
                               (Jsonb(forget.retryable(settings.media_root, failed)), row["id"]))
    return total


def due(timestamp: int | None, zone) -> bool:
    """Is the hourly tick scheduled at ``timestamp`` (default: now) the 03:00 one, in local time?"""
    at = dt.datetime.fromtimestamp(timestamp, dt.UTC) if timestamp is not None else None
    return local_now(zone, at).hour == RETENTION_HOUR


@job_app.periodic(cron="0 * * * *")
@job_app.task(name=jobs.APPLY_RETENTION, queue="light", queueing_lock="apply_retention")
async def apply_retention_job(timestamp: int | None = None) -> None:
    ctx = get_context()
    if due(timestamp, ctx.settings.zone):
        await retry_forgotten_files(ctx.pool, ctx.settings)
        await apply_retention(ctx.pool, ctx.settings)
```

Apply to `brain/src/loopi_brain/web/settings_routes.py` and `brain/src/loopi_brain/web/templates/settings.html` (the preview on Cài đặt):

```diff
diff --git a/brain/src/loopi_brain/web/settings_routes.py b/brain/src/loopi_brain/web/settings_routes.py
--- a/brain/src/loopi_brain/web/settings_routes.py
+++ b/brain/src/loopi_brain/web/settings_routes.py
@@ -9,7 +9,7 @@ from fastapi.responses import HTMLResponse
 from pydantic import ValidationError
 
 from .. import forget, reprocess, store
-from ..pipeline import status
+from ..pipeline import retention, status
 from ..providers.config import TASKS, ProvidersConfigError, load_providers
 from ..providers.router import model_version
 from .routes import TEMPLATES, require_htmx
@@ -48,6 +48,7 @@ async def settings_context(request: Request) -> dict:
         "health": await status.model_health(state.settings, state.http),
         "tz": state.settings.tz,
         "retention": await store.get_retention(state.pool),
+        "would_delete": await retention.preview_retention(state.pool),
         "max_days": store.MAX_DAYS,
         "choices": CHOICE_LABELS,
     }
```

```diff
diff --git a/brain/src/loopi_brain/web/templates/settings.html b/brain/src/loopi_brain/web/templates/settings.html
--- a/brain/src/loopi_brain/web/templates/settings.html
+++ b/brain/src/loopi_brain/web/templates/settings.html
@@ -28,6 +28,9 @@
     <button>Lưu</button>
   </form>
   <p>Dọn dữ liệu cũ: {% if retention.enabled %}<strong>đang bật</strong>{% else %}<strong>đang tắt</strong> (chưa xoá gì){% endif %}.</p>
+  {% set w = would_delete %}
+  <p>Nếu chạy bây giờ sẽ xoá: video thô {{ w.video.recordings }} đoạn ({{ "%.1f"|format(w.video.bytes / 1e9) }} GB),
+     audio thô {{ w.audio.recordings }} đoạn ({{ "%.1f"|format(w.audio.bytes / 1e9) }} GB).</p>
   <p class="muted">Khi bật, lúc 03:00 mỗi đêm file thô cũ hơn số ngày này bị xoá vĩnh viễn (chỉ những đoạn đã phân tích xong). Keyframe, transcript, chương và recap được giữ mãi.</p>
   <div id="retention-result"></div>
 </section>
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_retention.py -q`
Expected: `11 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `490 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/pipeline/retention.py brain/src/loopi_brain/pipeline/jobs.py \
  brain/src/loopi_brain/pipeline/status.py brain/src/loopi_brain/pipeline/asr.py \
  brain/src/loopi_brain/web/settings_routes.py brain/src/loopi_brain/web/templates/settings.html brain/tests/test_retention.py
git commit -F - <<'EOF'
feat(brain): 03:00 retention of raw video and audio, off until the owner turns it on (spec 9.5, D20)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-16; they amend the steps above):**
- **R-I3:** use `media.item_files` (Task 10) instead of a private `_paths`.

---

### Task 12: Optional Anthropic text provider, privacy allow-list first

**Files:**
- Create: `brain/src/loopi_brain/providers/anthropic.py`
- Modify: `brain/src/loopi_brain/providers/base.py` (allow-list), `brain/src/loopi_brain/providers/config.py` (anthropic for text tasks only), `brain/src/loopi_brain/providers/router.py` (privacy-aware `task_ready`, routes without a key skipped, Anthropic when keyed), `brain/src/loopi_brain/config.py` (`ANTHROPIC_API_KEY`)
- Modify: `brain/src/loopi_brain/web/settings_routes.py`, `brain/src/loopi_brain/web/templates/settings.html` (key set or not; never the key)
- Modify: `brain/tests/test_providers.py` (two P2a assumptions change, see R27)
- Test: `brain/tests/test_anthropic.py`

**Interfaces:**
- Consumes: Task 2 `TEXT_TASKS`; Task 8 `Router.stream_chat`, the `Provider.stream_chat` protocol; Task 9 `providers_view`; P2a `Router._usable`, `task_ready`, `PrivacyBlocked`.
- Produces:
  - `providers.base.LOCAL_PROVIDERS = {"ollama", "local"}`, `is_cloud(provider) -> bool` (anything else is cloud); `CLOUD_PROVIDERS` is removed.
  - `providers.config.KNOWN_PROVIDERS` gains `"anthropic"`, allowed only on `TEXT_TASKS` (a route of `caption`, `embed` or `asr` to anthropic is a load error); `IMAGE_TASKS = ("caption",)`; `ProvidersConfig.providers_used() -> set[str]`.
  - `Router._allowed(task, images)` (routes that may take the request: configured providers only; with images, local only unless `allow_cloud_images`); `_usable` raises `PrivacyBlocked` (images, only cloud routes) or `ProviderUnavailable` ("no configured provider … ANTHROPIC_API_KEY") when nothing is left; `task_ready(task, health)` counts only those routes.
  - `Settings.anthropic_api_key: SecretStr | None` (env `ANTHROPIC_API_KEY`, no `LOOPI_` prefix; blank = None), `Settings.anthropic_url = "https://api.anthropic.com"`; `build_router` adds `AnthropicProvider` only when the key is set (else logs that anthropic routes are skipped).
  - `providers.anthropic.AnthropicProvider(api_key, *, base_url="https://api.anthropic.com", client=None)` with `chat` (text only: images → `PrivacyBlocked`), `stream_chat` (SSE), `embed` (always `ProviderError`); `strict_schema(schema)`. HTTP 429/500/502/503/504/529 and connection failures → `ProviderUnavailable` (fallback, no retry spent); other errors, refusals, empty answers and an answer cut off by `max_tokens` before any text → `ProviderError`.
  - Thinking is set explicitly (Haiku 5.5 and Sonnet 5.5 think by default and `max_tokens` caps thinking plus answer): `THINKING_OFF = {"claude-haiku-5-5": {"type": "disabled"}, "claude-sonnet-5-5": {"type": "between_tools"}}` (Sonnet 5.5 rejects "disabled"); with thinking off `max_tokens` is at least `MIN_MAX_TOKENS = 1024`; any other model keeps thinking and gets `THINKING_MAX_TOKENS = 8192`.
  - Cài đặt says which tasks may send text to the cloud ("Gửi text lên cloud (Anthropic) khi dùng tuyến đó: …") when a key is set and a route uses anthropic.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_anthropic.py`:

```python
import json

import httpx
import pytest
from pydantic import SecretStr

from helpers import FakeOllama
from loopi_brain.app import create_app
from loopi_brain.config import Settings
from loopi_brain.pipeline import chapters, recap
from loopi_brain.providers.anthropic import AnthropicProvider, strict_schema
from loopi_brain.providers.base import PrivacyBlocked, ProviderError, ProviderUnavailable, is_cloud
from loopi_brain.providers.config import DEFAULT_CONFIG, ProvidersConfig, TaskRoute
from loopi_brain.providers.ollama import OllamaHealth
from loopi_brain.providers.router import Router, build_router

KEY = "sk-ant-test-0000"


class FakeAnthropic:
    """An in-process stand-in for the Messages API (an httpx.MockTransport handler)."""

    def __init__(self, text: str = "Xin chào", status: int = 200, stop_reason: str = "end_turn"):
        self.text, self.status, self.stop_reason = text, status, stop_reason
        self.requests: list[tuple[dict, dict]] = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((dict(request.headers), body))
        if self.status != 200:
            return httpx.Response(self.status, json={"type": "error", "error": {"type": "x", "message": "nope"}})
        if body.get("stream"):
            events = [
                {"type": "message_start", "message": {"id": "m"}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": ""}},
                {"type": "ping"},
                *[{"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": self.text[i:i + 4]}}
                  for i in range(0, len(self.text), 4)],
                {"type": "message_delta", "delta": {"stop_reason": self.stop_reason}},
                {"type": "message_stop"},
            ]
            sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "type": "message", "stop_reason": self.stop_reason,
            "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": self.text}],
        })

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def _provider(fake) -> AnthropicProvider:
    return AnthropicProvider(KEY, base_url="https://api.anthropic.test", client=fake.client())


async def test_chat_sends_text_only_with_the_system_prompt_on_top_and_no_sampling_parameters():
    fake = FakeAnthropic(text='{"title": "Sáng", "summary": "Đọc sách."}')
    text = await _provider(fake).chat(
        "claude-haiku-5-5",
        [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Tóm tắt"}],
        json_schema=chapters.SUMMARY_SCHEMA, temperature=0.3, max_tokens=400,
    )
    assert json.loads(text) == {"title": "Sáng", "summary": "Đọc sách."}
    [(headers, body)] = fake.requests
    assert headers["x-api-key"] == KEY and headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-haiku-5-5" and body["system"] == "Be brief."
    # Thinking off, and a cap the answer always fits in (the brain clips longer answers).
    assert body["thinking"] == {"type": "disabled"} and body["max_tokens"] == 1024
    assert body["messages"] == [{"role": "user", "content": "Tóm tắt"}]
    assert "temperature" not in body and body["output_config"]["effort"] == "low"
    schema = body["output_config"]["format"]["schema"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False and "maxLength" not in json.dumps(schema)
    assert repr(_provider(fake)) == "AnthropicProvider('https://api.anthropic.test')"  # no key in logs


@pytest.mark.parametrize(("model", "thinking", "max_tokens"), [
    ("claude-haiku-5-5", {"type": "disabled"}, 1024),
    ("claude-sonnet-5-5", {"type": "between_tools"}, 1024),  # Sonnet 5.5 rejects "disabled"
    ("claude-opus-5-5", None, 8192),  # always thinks: thinking and answer share the budget
])
async def test_thinking_is_turned_off_where_allowed_and_otherwise_budgeted(model, thinking, max_tokens):
    fake = FakeAnthropic()
    await _provider(fake).chat(model, [{"role": "user", "content": "x"}], max_tokens=60)
    pieces = [p async for p in _provider(fake).stream_chat(model, [{"role": "user", "content": "x"}], max_tokens=600)]
    assert pieces
    for _, body in fake.requests:
        assert body.get("thinking") == thinking and body["max_tokens"] == max_tokens


async def test_an_answer_cut_off_before_any_text_is_an_error():
    with pytest.raises(ProviderError, match="max_tokens was used up"):
        await _provider(FakeAnthropic(text="", stop_reason="max_tokens")).chat("m", [{"role": "user", "content": "x"}])


def test_strict_schema_closes_every_object_and_drops_limits():
    schema = strict_schema(recap.RECAP_SCHEMA)
    item = schema["properties"]["commitments"]["items"]
    assert item["additionalProperties"] is False and schema["additionalProperties"] is False
    assert "maxItems" not in json.dumps(schema) and "maxLength" not in json.dumps(schema)
    assert recap.RECAP_SCHEMA["properties"]["main_events"]["maxItems"] == recap.MAX_EVENTS  # the original is unchanged


@pytest.mark.parametrize(("status", "kind"), [
    (429, ProviderUnavailable), (529, ProviderUnavailable), (503, ProviderUnavailable),
    (401, ProviderError), (400, ProviderError), (404, ProviderError),
])
async def test_temporary_errors_are_unavailable_and_mistakes_are_errors(status, kind):
    with pytest.raises(kind) as info:
        await _provider(FakeAnthropic(status=status)).chat("claude-haiku-5-5", [{"role": "user", "content": "x"}])
    assert f"HTTP {status}: nope" in str(info.value)
    if kind is ProviderError:
        assert not isinstance(info.value, ProviderUnavailable)


async def test_refusals_empty_answers_and_broken_connections_are_errors():
    with pytest.raises(ProviderError, match="declined"):
        await _provider(FakeAnthropic(stop_reason="refusal")).chat("m", [{"role": "user", "content": "x"}])
    with pytest.raises(ProviderError, match="no text"):
        await _provider(FakeAnthropic(text="")).chat("m", [{"role": "user", "content": "x"}])

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    down = AnthropicProvider(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(refused)))
    with pytest.raises(ProviderUnavailable):
        await down.chat("m", [{"role": "user", "content": "x"}])
    with pytest.raises(ProviderUnavailable):
        _ = [p async for p in down.stream_chat("m", [{"role": "user", "content": "x"}])]


async def test_images_and_embeddings_are_never_sent():
    fake = FakeAnthropic()
    with pytest.raises(PrivacyBlocked):
        await _provider(fake).chat("m", [{"role": "user", "content": "x"}], images=[b"jpeg"])
    with pytest.raises(ProviderError):
        await _provider(fake).embed("m", ["x"])
    assert fake.requests == []


async def test_streaming_reads_text_deltas_only():
    fake = FakeAnthropic(text="Sáng mai đi khám răng [1].")
    pieces = [p async for p in _provider(fake).stream_chat("claude-sonnet-5-5", [{"role": "user", "content": "x"}])]
    assert "".join(pieces) == "Sáng mai đi khám răng [1]." and len(pieces) > 1
    assert fake.requests[0][1]["stream"] is True
    with pytest.raises(ProviderError, match="declined"):
        _ = [p async for p in _provider(FakeAnthropic(stop_reason="refusal")).stream_chat("m", [{"role": "user", "content": "x"}])]
    with pytest.raises(ProviderUnavailable):
        _ = [p async for p in _provider(FakeAnthropic(status=529)).stream_chat("m", [{"role": "user", "content": "x"}])]

    def overloaded(request):
        event = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        return httpx.Response(200, content=f"event: error\ndata: {json.dumps(event)}\n\n".encode())

    mid = AnthropicProvider(KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(overloaded)))
    with pytest.raises(ProviderUnavailable, match="Overloaded"):
        _ = [p async for p in mid.stream_chat("m", [{"role": "user", "content": "x"}])]


def test_anthropic_may_serve_text_tasks_only():
    tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
    tasks["recap"] = {"provider": "ollama", "model": "qwen3:8b",
                      "fallback": {"provider": "anthropic", "model": "claude-haiku-5-5"}}
    tasks["chat"] = {"provider": "anthropic", "model": "claude-sonnet-5-5",
                     "fallback": {"provider": "ollama", "model": "qwen3:8b"}}
    config = ProvidersConfig.model_validate({"tasks": tasks})
    assert config.providers_used() == {"ollama", "local", "anthropic"}
    for task in ("caption", "embed", "asr"):
        with pytest.raises(ValueError):
            ProvidersConfig.model_validate({"tasks": {**tasks, task: {"provider": "anthropic", "model": "c"}}})


def test_the_privacy_gate_is_an_allow_list():
    assert not is_cloud("ollama") and not is_cloud("local")
    assert is_cloud("anthropic") and is_cloud("some-future-provider")


async def test_an_unknown_provider_never_gets_images_and_task_ready_follows_the_privacy_rule():
    config = ProvidersConfig.model_validate({"tasks": {n: r.model_dump() for n, r in DEFAULT_CONFIG.tasks.items()}})
    config.tasks["caption"] = TaskRoute.model_construct(
        provider="newcloud", model="m", fallback=TaskRoute(provider="ollama", model="minicpm-v4.6:1b"))
    stub = AnthropicProvider(KEY, client=FakeAnthropic().client())
    router = Router(config, {"newcloud": stub, "ollama": stub})
    without_vision = OllamaHealth(reachable=True, present=[], missing=["minicpm-v4.6:1b"])
    assert not router.task_ready("caption", without_vision)  # the cloud route may not take the images
    config.privacy.allow_cloud_images = True
    assert router.task_ready("caption", without_vision)


def test_the_api_key_comes_from_anthropic_api_key_and_blank_means_none(monkeypatch):
    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
    assert Settings().anthropic_api_key is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    assert Settings().anthropic_api_key is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    settings = Settings()
    assert settings.anthropic_api_key.get_secret_value() == KEY and KEY not in repr(settings)


async def test_routes_to_anthropic_are_used_only_with_a_key(pki_settings, caplog):
    tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
    tasks["chat"] = {"provider": "anthropic", "model": "claude-sonnet-5-5",
                     "fallback": {"provider": "ollama", "model": "qwen3:8b"}}
    config = ProvidersConfig.model_validate({"tasks": tasks})
    ollama = FakeOllama(caption="từ Ollama")
    no_key = build_router(config, pki_settings, ollama.client())
    assert "ANTHROPIC_API_KEY is not set" in caplog.text
    assert (await no_key.chat("chat", [{"role": "user", "content": "x"}])).model_version == "ollama:qwen3:8b"
    fake = FakeAnthropic(text="từ Claude")

    def both(request):
        return fake(request) if request.url.host == "api.anthropic.test" else ollama(request)

    keyed = pki_settings.model_copy(update={"anthropic_api_key": SecretStr(KEY), "anthropic_url": "https://api.anthropic.test"})
    router = build_router(config, keyed, httpx.AsyncClient(transport=httpx.MockTransport(both)))
    result = await router.chat("chat", [{"role": "user", "content": "x"}])
    assert (result.text, result.model_version) == ("từ Claude", "anthropic:claude-sonnet-5-5")
    fake.status = 529  # overloaded: the local fallback answers, streamed too
    pieces = [p async for p in router.stream_chat("chat", [{"role": "user", "content": "x"}])]
    assert "".join(t for t, _ in pieces) == "từ Ollama" and pieces[0][1] == "ollama:qwen3:8b"
    only_cloud = ProvidersConfig.model_validate({"tasks": {**tasks, "chat": {"provider": "anthropic", "model": "c"}}})
    with pytest.raises(ProviderUnavailable, match="ANTHROPIC_API_KEY"):
        await build_router(only_cloud, pki_settings, ollama.client()).chat("chat", [{"role": "user", "content": "x"}])


async def test_the_settings_page_says_whether_the_key_is_set_and_never_shows_it(pool, pki_settings, jobs_open, tmp_path):
    tasks = {name: route.model_dump() for name, route in DEFAULT_CONFIG.tasks.items()}
    tasks["recap"] = {"provider": "ollama", "model": "qwen3:8b",
                      "fallback": {"provider": "anthropic", "model": "claude-haiku-5-5"}}
    routed = tmp_path / "providers.yaml"
    routed.write_text(json.dumps({"tasks": tasks}))
    for update, expected in (({}, "chưa đặt"), ({"anthropic_api_key": SecretStr(KEY)}, "đã đặt")):
        app = create_app(pki_settings.model_copy(update={"providers_file": routed, **update}), pool=pool,
                         http_client=FakeOllama().client(), llm_client=FakeOllama().client())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
            text = (await c.get("/settings")).text
        assert f"ANTHROPIC_API_KEY: {expected}" in text and KEY not in text
        assert "<code>ollama:qwen3:8b → anthropic:claude-haiku-5-5</code>" in text
        assert ("bị bỏ qua" in text) == (expected == "chưa đặt")
        # With a key, the page says which tasks may send text to the cloud.
        assert ("Gửi text lên cloud (Anthropic) khi dùng tuyến đó: Recap cuối ngày" in text) == (expected == "đã đặt")
```

Apply to `brain/tests/test_providers.py` (a recap fallback to anthropic is valid now — a caption or embedding route to it is not; a cloud route counts as ready only when its key is set):

```diff
diff --git a/brain/tests/test_providers.py b/brain/tests/test_providers.py
--- a/brain/tests/test_providers.py
+++ b/brain/tests/test_providers.py
@@ -47,7 +47,8 @@ def test_committed_providers_yaml_equals_built_in_defaults():
         {"asr": {"provider": "ollama", "model": "whisper"}},
         {"asr": {"provider": "local", "model": "faster-whisper/tiny"}},
         {"embed": {"provider": "local", "model": "x"}},
-        {"recap": {"provider": "ollama", "model": "qwen3:8b", "fallback": {"provider": "anthropic", "model": "c"}}},
+        {"caption": {"provider": "ollama", "model": "m", "fallback": {"provider": "anthropic", "model": "c"}}},
+        {"embed": {"provider": "anthropic", "model": "c"}},
         {"vlog": {"provider": "ollama", "model": "qwen3:8b"}},
     ],
 )
@@ -197,9 +198,10 @@ async def test_health_reports_missing_models_and_unreachable_server():
 def test_routes_the_health_check_cannot_see_count_as_ready():
     config = _config()
     config.tasks["recap"] = TaskRoute.model_construct(provider="anthropic", model="claude", fallback=None)
-    router = Router(config, {})
+    router = Router(config, {"anthropic": StubProvider()})  # ANTHROPIC_API_KEY is set
     nothing = OllamaHealth(reachable=False, present=[], missing=config.ollama_models())
     assert router.task_ready("recap", nothing) and not router.task_ready("caption", nothing)
+    assert not Router(config, {}).task_ready("recap", nothing)  # no key: that route cannot run
 
 
 async def test_pull_downloads_a_model():
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_anthropic.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'loopi_brain.providers.anthropic'`.

Run: `cd brain && uv run pytest tests/test_providers.py -q`
Expected: FAIL — `1 failed, 30 passed` (`test_routes_the_health_check_cannot_see_count_as_ready`: a route without its provider still counts as ready).

- [ ] **Step 3: Make the privacy gate an allow-list and `task_ready` follow it (P2a deferred items)**

Apply to `brain/src/loopi_brain/providers/base.py`, `brain/src/loopi_brain/providers/config.py` and `brain/src/loopi_brain/config.py`:

```diff
diff --git a/brain/src/loopi_brain/providers/base.py b/brain/src/loopi_brain/providers/base.py
--- a/brain/src/loopi_brain/providers/base.py
+++ b/brain/src/loopi_brain/providers/base.py
@@ -5,7 +5,13 @@ from collections.abc import AsyncIterator
 from typing import Protocol
 
 EMBED_DIM = 1024
-CLOUD_PROVIDERS = frozenset({"anthropic"})
+# Providers that run on this machine. Every other provider is a cloud provider, including any added later:
+# the privacy gate is an allow-list, so a new provider cannot receive images by accident (P2a deferred item).
+LOCAL_PROVIDERS = frozenset({"ollama", "local"})
+
+
+def is_cloud(provider: str) -> bool:
+    return provider not in LOCAL_PROVIDERS
 
 
 class ProviderError(Exception):
@@ -19,7 +25,8 @@ class ProviderUnavailable(ProviderError):
 
 
 class PrivacyBlocked(Exception):
-    """The request carries images or audio and every route for the task is a cloud provider."""
+    """The request carries images and every route for the task is a cloud provider (audio never reaches the
+    router: ASR runs in the worker)."""
 
 
 class Provider(Protocol):
```

```diff
diff --git a/brain/src/loopi_brain/providers/config.py b/brain/src/loopi_brain/providers/config.py
--- a/brain/src/loopi_brain/providers/config.py
+++ b/brain/src/loopi_brain/providers/config.py
@@ -6,11 +6,13 @@ from pathlib import Path
 import yaml
 from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
 
-# P2a ships the Ollama provider and the in-process ASR ("local"); cloud providers arrive in P2b.
-KNOWN_PROVIDERS = frozenset({"ollama", "local"})
+# The Ollama provider, the in-process ASR ("local"), and Anthropic for text tasks only (P2b, spec §9.3).
+KNOWN_PROVIDERS = frozenset({"ollama", "local", "anthropic"})
 TASKS = ("caption", "asr", "embed", "chapter_summary", "recap", "chat")
 # Tasks that send only text (chapter summaries, the day recap, chat answers).
 TEXT_TASKS = ("chapter_summary", "recap", "chat")
+# Tasks whose requests carry images.
+IMAGE_TASKS = ("caption",)
 LOCAL_ASR_MODELS = frozenset({"faster-whisper/large-v3-turbo-int8"})
 
 
@@ -62,6 +64,8 @@ class ProvidersConfig(BaseModel):
                     raise ValueError(f"task {task}: provider 'local' serves asr only, and asr must be local")
                 if task == "asr" and step.model not in LOCAL_ASR_MODELS:
                     raise ValueError(f"task asr: unsupported model {step.model!r}")
+                if step.provider == "anthropic" and task not in TEXT_TASKS:
+                    raise ValueError(f"task {task}: provider 'anthropic' serves text tasks only ({', '.join(TEXT_TASKS)})")
         return self
 
     def ollama_models(self) -> list[str]:
@@ -79,6 +83,9 @@ class ProvidersConfig(BaseModel):
             step.model for task in TEXT_TASKS for step in self.tasks[task].chain() if step.provider == "ollama"
         )
 
+    def providers_used(self) -> set[str]:
+        return {step.provider for route in self.tasks.values() for step in route.chain()}
+
 
 DEFAULT_CONFIG = ProvidersConfig(
     tasks={
```

```diff
diff --git a/brain/src/loopi_brain/config.py b/brain/src/loopi_brain/config.py
--- a/brain/src/loopi_brain/config.py
+++ b/brain/src/loopi_brain/config.py
@@ -2,7 +2,7 @@
 from pathlib import Path
 from zoneinfo import ZoneInfo
 
-from pydantic import Field
+from pydantic import Field, SecretStr, field_validator
 from pydantic_settings import BaseSettings, SettingsConfigDict
 
 
@@ -42,6 +42,15 @@ class Settings(BaseSettings):
     # distance), or when its text matches the question's words this well (pg_trgm word similarity). P2c tunes both.
     search_max_distance: float = Field(default=0.6, gt=0.0, le=2.0)
     search_min_similarity: float = Field(default=0.3, gt=0.0, le=1.0)
+    # Optional cloud provider for text tasks (spec §9.3): only used when this key is set AND providers.yaml routes a
+    # text task to "anthropic". Read from ANTHROPIC_API_KEY (no LOOPI_ prefix); never logged or shown.
+    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
+    anthropic_url: str = "https://api.anthropic.com"
+
+    @field_validator("anthropic_api_key", mode="before")
+    @classmethod
+    def _blank_key_is_no_key(cls, value):
+        return None if value is None or (isinstance(value, str) and not value.strip()) else value
 
     @property
     def zone(self) -> ZoneInfo:
```

- [ ] **Step 4: Write the Anthropic provider and route to it**

Create `brain/src/loopi_brain/providers/anthropic.py`:

```python
"""Anthropic Messages API client for text tasks only (spec §9.3: optional, cloud gets text only).

Plain httpx, no SDK dependency. Requests never carry images or audio. Notes on the API this relies on:
- ``POST /v1/messages`` with headers ``x-api-key`` and ``anthropic-version: 2023-06-01``; the system prompt is
  the top-level ``system`` field, not a message.
- Structured output is ``output_config.format = {"type": "json_schema", "schema": ...}``; the schema may not use
  length or number limits and every object needs ``additionalProperties: false`` (``strict_schema``). The
  caller still clips over-long answers, as for Ollama.
- Claude Haiku 5.5 and Sonnet 5.5 reject non-default sampling parameters, so no ``temperature`` is sent.
- They think by default, and ``max_tokens`` caps thinking **and** answer together: a small cap can end before
  any text. So thinking is turned off where the model allows it (Haiku 5.5: ``{"type": "disabled"}``, valid at
  effort ≤ high; Sonnet 5.5 rejects "disabled" and uses ``{"type": "between_tools"}``), ``max_tokens`` is at least
  1024, and any other model gets 8192 so its thinking leaves room for the answer; ``output_config.effort`` is
  "low"; only ``text`` content blocks are read; an answer cut off before any text is an error.
- Streaming is Server-Sent Events; text arrives as ``content_block_delta`` events with a ``text_delta``.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from .base import PrivacyBlocked, ProviderError, ProviderUnavailable

API_VERSION = "2023-06-01"
DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
DEFAULT_MAX_TOKENS = 2048
EFFORT = "low"
THINKING_OFF = {"claude-haiku-5-5": {"type": "disabled"}, "claude-sonnet-5-5": {"type": "between_tools"}}
MIN_MAX_TOKENS = 1024  # thinking off: room for any answer the brain asks for (it clips longer ones)
THINKING_MAX_TOKENS = 8192  # a model that always thinks: thinking and answer share this budget
ERROR_TAIL = 500
# Rate limits, overload (529) and server errors pass: the router falls back, a job does not spend a retry.
TEMPORARY = frozenset({429, 500, 502, 503, 504, 529})
_UNSUPPORTED = frozenset({"maxLength", "minLength", "minimum", "maximum", "maxItems", "minItems"})


def strict_schema(schema):
    """``schema`` without the limits structured output does not support, every object closed."""
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {key: strict_schema(value) for key, value in schema.items() if key not in _UNSUPPORTED}
    if out.get("type") == "object":
        out["additionalProperties"] = False
    return out


def _error(path: str, response: httpx.Response) -> ProviderError:
    try:
        detail = (response.json().get("error") or {}).get("message") or response.text
    except (ValueError, AttributeError):
        detail = response.text
    message = f"anthropic {path} HTTP {response.status_code}: {str(detail)[-ERROR_TAIL:]}"
    return ProviderUnavailable(message) if response.status_code in TEMPORARY else ProviderError(message)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, *, base_url: str = "https://api.anthropic.com",
                 client: httpx.AsyncClient | None = None):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    def __repr__(self) -> str:  # never shows the key
        return f"AnthropicProvider({self._base!r})"

    def _headers(self) -> dict:
        return {"x-api-key": self._key, "anthropic-version": API_VERSION, "content-type": "application/json"}

    @staticmethod
    def _body(model: str, messages: list[dict], json_schema: dict | None, max_tokens: int | None) -> dict:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        thinking = THINKING_OFF.get(model)
        floor = MIN_MAX_TOKENS if thinking else THINKING_MAX_TOKENS
        body: dict = {
            "model": model,
            "max_tokens": max(max_tokens or DEFAULT_MAX_TOKENS, floor),
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"],
            "output_config": {"effort": EFFORT},
        }
        if thinking:
            body["thinking"] = dict(thinking)
        if system:
            body["system"] = system
        if json_schema is not None:
            body["output_config"]["format"] = {"type": "json_schema", "schema": strict_schema(json_schema)}
        return body

    async def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        images: list[bytes] | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.0,  # not sent (see the module docstring)
        max_tokens: int | None = None,
    ) -> str:
        if images:
            raise PrivacyBlocked("images are never sent to anthropic")
        try:
            response = await self._client.post(
                f"{self._base}/v1/messages", headers=self._headers(), json=self._body(model, messages, json_schema, max_tokens)
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderUnavailable(f"anthropic /v1/messages: {type(exc).__name__}: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"anthropic /v1/messages: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise _error("/v1/messages", response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("anthropic /v1/messages: response is not JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError("anthropic /v1/messages: unexpected response")
        if data.get("stop_reason") == "refusal":
            raise ProviderError("anthropic /v1/messages: the model declined the request")
        text = "".join(
            block.get("text", "") for block in data.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text:
            if data.get("stop_reason") == "max_tokens":
                raise ProviderError("anthropic /v1/messages: max_tokens was used up before any answer text")
            raise ProviderError(f"anthropic /v1/messages: no text in the answer (stop_reason {data.get('stop_reason')})")
        return text

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        raise ProviderError("anthropic serves no embeddings")

    async def stream_chat(
        self, model: str, messages: list[dict], *, temperature: float = 0.0, max_tokens: int | None = None
    ) -> AsyncIterator[str]:
        body = {**self._body(model, messages, None, max_tokens), "stream": True}
        try:
            async with self._client.stream(
                "POST", f"{self._base}/v1/messages", headers=self._headers(), json=body
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _error("/v1/messages", response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # "event:" lines repeat the type that the data carries
                    try:
                        data = json.loads(line[5:])
                    except ValueError as exc:
                        raise ProviderError("anthropic /v1/messages: a streamed event is not JSON") from exc
                    kind = data.get("type")
                    if kind == "error":
                        error = data.get("error") or {}
                        message = f"anthropic /v1/messages: {error.get('type')}: {error.get('message')}"
                        raise ProviderUnavailable(message) if error.get("type") == "overloaded_error" else ProviderError(message)
                    if kind == "content_block_delta" and (data.get("delta") or {}).get("type") == "text_delta":
                        text = data["delta"].get("text") or ""
                        if text:
                            yield text
                    elif kind == "message_delta" and (data.get("delta") or {}).get("stop_reason") == "refusal":
                        raise ProviderError("anthropic /v1/messages: the model declined the request")
                    elif kind == "message_stop":
                        return
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderUnavailable(f"anthropic /v1/messages: {type(exc).__name__}: {exc}") from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"anthropic /v1/messages: {type(exc).__name__}: {exc}") from exc
```

Apply to `brain/src/loopi_brain/providers/router.py`:

```diff
diff --git a/brain/src/loopi_brain/providers/router.py b/brain/src/loopi_brain/providers/router.py
--- a/brain/src/loopi_brain/providers/router.py
+++ b/brain/src/loopi_brain/providers/router.py
@@ -8,8 +8,9 @@ from dataclasses import dataclass
 import httpx
 
 from ..config import Settings
-from .base import CLOUD_PROVIDERS, EMBED_DIM, PrivacyBlocked, Provider, ProviderError
-from .config import ProvidersConfig, TaskRoute
+from .anthropic import AnthropicProvider
+from .base import EMBED_DIM, PrivacyBlocked, Provider, ProviderError, ProviderUnavailable, is_cloud
+from .config import IMAGE_TASKS, ProvidersConfig, TaskRoute
 from .ollama import OllamaHealth, OllamaProvider
 
 log = logging.getLogger(__name__)
@@ -42,13 +43,20 @@ class Router:
     def model_version(self, task: str) -> str:
         return model_version(self.route(task))
 
-    def _usable(self, task: str, *, images: bool) -> list[TaskRoute]:
-        steps = self.route(task).chain()
+    def _allowed(self, task: str, *, images: bool) -> list[TaskRoute]:
+        """The routes of ``task`` that may receive this request: with images, local providers only (unless
+        allow_cloud_images); routes whose provider is not set up (no ANTHROPIC_API_KEY) are left out."""
+        steps = [s for s in self.route(task).chain() if s.provider in self.providers]
         if images and not self.config.privacy.allow_cloud_images:
-            allowed = [s for s in steps if s.provider not in CLOUD_PROVIDERS]
-            if not allowed:
+            steps = [s for s in steps if not is_cloud(s.provider)]
+        return steps
+
+    def _usable(self, task: str, *, images: bool) -> list[TaskRoute]:
+        steps = self._allowed(task, images=images)
+        if not steps:
+            if images and any(is_cloud(s.provider) for s in self.route(task).chain()):
                 raise PrivacyBlocked(f"task {task}: images may not be sent to a cloud provider")
-            return allowed
+            raise ProviderUnavailable(f"task {task}: no configured provider (is ANTHROPIC_API_KEY set?)")
         return steps
 
     async def chat(
@@ -125,11 +133,11 @@ class Router:
         return await self.providers["ollama"].health(self.config.ollama_models())
 
     def task_ready(self, task: str, health: OllamaHealth) -> bool:
-        """Can ``task`` run now: does one of its routes have its model on the reachable Ollama server?
-
-        Routes to other providers cannot be checked here and count as ready.
-        """
-        return any(s.provider != "ollama" or s.model in health.present for s in self.route(task).chain())
+        """Can ``task`` run now: does one of the routes its requests may take have its model on the reachable
+        Ollama server? A cloud route counts only for text, and only when its provider is set up; it cannot be
+        checked from here and counts as ready."""
+        steps = self._allowed(task, images=task in IMAGE_TASKS)
+        return any(s.provider != "ollama" or s.model in health.present for s in steps)
 
 
 def build_router(config: ProvidersConfig, settings: Settings, client: httpx.AsyncClient | None = None) -> Router:
@@ -142,4 +150,11 @@ def build_router(config: ProvidersConfig, settings: Settings, client: httpx.Asyn
         num_ctx=settings.llm_num_ctx,
         text_timeout=settings.llm_timeout_s,
     )
-    return Router(config, {"ollama": ollama})
+    providers: dict[str, Provider] = {"ollama": ollama}
+    if settings.anthropic_api_key is not None:
+        providers["anthropic"] = AnthropicProvider(
+            settings.anthropic_api_key.get_secret_value(), base_url=settings.anthropic_url, client=client
+        )
+    elif "anthropic" in config.providers_used():
+        log.warning("providers.yaml routes text tasks to anthropic but ANTHROPIC_API_KEY is not set: those routes are skipped")
+    return Router(config, providers)
```

- [ ] **Step 5: Show the key state on Cài đặt**

Apply to `brain/src/loopi_brain/web/settings_routes.py` and `brain/src/loopi_brain/web/templates/settings.html`:

```diff
diff --git a/brain/src/loopi_brain/web/settings_routes.py b/brain/src/loopi_brain/web/settings_routes.py
--- a/brain/src/loopi_brain/web/settings_routes.py
+++ b/brain/src/loopi_brain/web/settings_routes.py
@@ -29,16 +29,20 @@ router = APIRouter()
 
 def providers_view(settings) -> dict:
     """Each task's route chain ("ollama:qwen3:8b → …"), the privacy flags, or the configuration error."""
+    key_set = settings.anthropic_api_key is not None
     try:
         config = load_providers(settings.providers_file)
     except ProvidersConfigError as exc:
-        return {"error": str(exc)[:500], "tasks": [], "privacy": None}
+        return {"error": str(exc)[:500], "tasks": [], "privacy": None, "key_set": key_set, "key_missing": False}
     tasks = [
         {"task": task, "label": TASK_LABELS[task],
          "chain": " → ".join(model_version(step) for step in config.tasks[task].chain())}
         for task in TASKS
     ]
-    return {"error": None, "tasks": tasks, "privacy": config.privacy}
+    cloud_text = [TASK_LABELS[task] for task in TASKS
+                  if key_set and any(step.provider == "anthropic" for step in config.tasks[task].chain())]
+    return {"error": None, "tasks": tasks, "privacy": config.privacy, "key_set": key_set,
+            "key_missing": "anthropic" in config.providers_used() and not key_set, "cloud_text": cloud_text}
 
 
 async def settings_context(request: Request) -> dict:
```

```diff
diff --git a/brain/src/loopi_brain/web/templates/settings.html b/brain/src/loopi_brain/web/templates/settings.html
--- a/brain/src/loopi_brain/web/templates/settings.html
+++ b/brain/src/loopi_brain/web/templates/settings.html
@@ -12,6 +12,10 @@
   <p>Ảnh gửi lên cloud: {{ "có" if providers.privacy.allow_cloud_images else "không" }} ·
      âm thanh gửi lên cloud: {{ "có" if providers.privacy.allow_cloud_audio else "không" }}</p>
   {% endif %}
+  <p>ANTHROPIC_API_KEY: {{ "đã đặt" if providers.key_set else "chưa đặt" }}{% if providers.key_missing %}
+     <span class="err">— providers.yaml dùng anthropic nhưng chưa có key, nên các tuyến đó bị bỏ qua.</span>{% endif %}</p>
+  {% if providers.cloud_text %}<p>Gửi text lên cloud (Anthropic) khi dùng tuyến đó: {{ providers.cloud_text|join(", ") }}
+     — transcript, mô tả khung hình và tóm tắt của ngày, không bao giờ ảnh hay âm thanh.</p>{% endif %}
   {% if not health.error %}
     {% if not health.reachable %}<p class="err">Không kết nối được Ollama ({{ health.url }}).</p>
     {% elif health.missing %}<p class="err">Thiếu model: {{ health.missing|join(", ") }}.</p>
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_anthropic.py tests/test_providers.py -q`
Expected: `52 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `512 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/providers/anthropic.py brain/src/loopi_brain/providers/base.py \
  brain/src/loopi_brain/providers/config.py brain/src/loopi_brain/providers/router.py brain/src/loopi_brain/config.py \
  brain/src/loopi_brain/web/settings_routes.py brain/src/loopi_brain/web/templates/settings.html \
  brain/tests/test_anthropic.py brain/tests/test_providers.py
git commit -F - <<'EOF'
feat(brain): optional Anthropic provider for text tasks behind a privacy allow-list (spec 9.3)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-16; they amend the steps above):**
- **R-M4:** tests must not read the ambient `ANTHROPIC_API_KEY`: add an autouse fixture in `brain/tests/conftest.py` that runs `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` (add conftest.py to the task's files and `git add`).

---

### Task 13: The "Hiểu ngày" card, compose (3 worker slots, 2 models) and README

**Files:**
- Modify: `brain/src/loopi_brain/pipeline/status.py` (`understanding_status`), `brain/src/loopi_brain/web/routes.py`, `brain/src/loopi_brain/web/templates/pipeline.html`
- Modify: `deploy/docker-compose.yml`, `deploy/providers.yaml` (comments only), `deploy/.env.example`, `deploy/README.md`
- Test: `brain/tests/test_web_pipeline.py`, `brain/tests/test_deploy.py`

**Interfaces:**
- Consumes: Task 4 `LLM_LOCK`/`LLM_PRIORITY`, the catch-up of old days; Task 5 `jobs.WRITE_RECAP`; Task 9/11 `store.get_retention` (`enabled`); P2a `/pipeline`, `tests/test_deploy.py` (parses the compose file with PyYAML only).
- Produces:
  - `status.understanding_status(pool, zone) -> {"chapters_waiting": int, "recaps_failed": int, "last_recap": {"day", "at"} | None, "retention": Retention}`; `/pipeline` shows it in a "Hiểu ngày" card: the three schedules, chapters waiting for a summary, the newest recap, failed recaps, and whether retention is on.
  - Compose: `worker` gets `LOOPI_WORKER_CONCURRENCY: "3"` (two slots for live work, one for at most one chapter/recap job); `api` and `worker` get `ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}`; `models` keeps `OLLAMA_MAX_LOADED_MODELS: "2"` (with the measured numbers in a comment) and gets no `mem_limit` (an OOM kill inside the container could stop a caption mid-job; Task 14 measures memory instead). `.env.example` lists an empty `ANTHROPIC_API_KEY=`. `providers.yaml` keeps every route local (the Anthropic examples stay commented out). README section "Understanding and pages (P2b)".

- [ ] **Step 1: Write the failing tests**

Apply to `brain/tests/test_web_pipeline.py` and `brain/tests/test_deploy.py` (the card test reads the card's text with whitespace collapsed, so template indentation cannot break it):

```diff
diff --git a/brain/tests/test_web_pipeline.py b/brain/tests/test_web_pipeline.py
--- a/brain/tests/test_web_pipeline.py
+++ b/brain/tests/test_web_pipeline.py
@@ -212,3 +212,26 @@ async def test_retry_all_requeues_every_dead_letter_but_the_embedding_run(web, p
     assert r.status_code == 200 and "Đã đưa 2 job vào hàng đợi xử lý lại." in r.text
     statuses = {r["task_name"]: r["status"] for r in await fetch_all(pool, "SELECT task_name, status FROM procrastinate_jobs")}
     assert statuses == {"index_marker": "todo", "embed_moments": "failed"}
+
+
+async def test_the_pipeline_page_shows_the_understanding_schedule(web, pool, pki_settings):
+    from helpers import insert_chapter
+
+    text = (await web.get("/pipeline")).text
+    card = " ".join(text.split('<h2>Hiểu ngày</h2>')[1].split("</div>")[0].split())  # whitespace-insensitive
+    assert "Chia chương: mỗi giờ (phút thứ 5) · chương chờ tóm tắt: 0" in card
+    assert "gần nhất: chưa có" in card and "<strong>đang tắt</strong>, không xoá gì" in card
+    day = dt.date(2026, 10, 14)
+    await insert_chapter(pool, day, UTC_9AM_SEOUL, UTC_9AM_SEOUL)  # no summary yet
+    async with pool.connection() as conn:
+        await conn.execute("INSERT INTO day_recap (day, markdown, model_version, generated_at)"
+                           " VALUES (%s, 'x', 'x', '2026-10-14T14:30:00Z')", (day,))
+        await conn.execute("INSERT INTO setting (key, value) VALUES ('retention',"
+                           " '{\"video_days\": 7, \"audio_days\": 7, \"enabled\": true}')")
+        job_id = await jobs.enqueue(conn, jobs.WRITE_RECAP, queue="understand", queueing_lock="recap:2026-10-13",
+                                    day="2026-10-13")
+    await _fail_job(pool, job_id)
+    text = (await web.get("/pipeline")).text
+    assert "chương chờ tóm tắt: 1" in text
+    assert '<a href="/?day=2026-10-14">2026-10-14</a> (viết lúc 14/10 23:30)' in text
+    assert "1 recap lỗi (xem Job lỗi)" in text and "đang bật, giữ video thô 7 ngày, audio thô 7 ngày" in text
```

```diff
diff --git a/brain/tests/test_deploy.py b/brain/tests/test_deploy.py
--- a/brain/tests/test_deploy.py
+++ b/brain/tests/test_deploy.py
@@ -85,3 +85,17 @@ def test_smoke_script_removes_what_it_created_on_every_exit():
     # selection and yields no keyframe), and the wait ends on either final analysis state.
     assert "smptebars=size=320x240:rate=2" in script and "testsrc" not in script
     assert '[[ "$STATE" == "done" || "$STATE" == "skipped" ]] && break' in script
+
+
+def test_the_worker_has_a_third_slot_for_one_text_model_job():
+    assert SERVICES["worker"]["environment"]["LOOPI_WORKER_CONCURRENCY"] == "3"
+    assert "mem_limit" not in SERVICES["models"]  # a kill would stop a caption mid-job; memory is checked in Task 14
+
+
+def test_the_anthropic_key_is_optional_and_comes_only_from_the_env_file():
+    for name in ("api", "worker"):
+        assert SERVICES[name]["environment"]["ANTHROPIC_API_KEY"] == "${ANTHROPIC_API_KEY:-}"
+    example = (DEPLOY / ".env.example").read_text()
+    assert "\nANTHROPIC_API_KEY=\n" in example  # empty: 100 % local unless the owner adds a key
+    providers = yaml.safe_load((DEPLOY / "providers.yaml").read_text())
+    assert all(step["provider"] != "anthropic" for step in providers["tasks"].values())  # routes stay commented out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_chapters.py tests/test_web_pipeline.py tests/test_deploy.py -q`
Expected: FAIL — `3 failed, 39 passed` (`test_the_pipeline_page_shows_the_understanding_schedule`, `test_the_worker_has_a_third_slot_for_one_text_model_job`, `test_the_anthropic_key_is_optional_and_comes_only_from_the_env_file`).

- [ ] **Step 3: Show the understanding jobs on /pipeline**

Apply to `brain/src/loopi_brain/pipeline/status.py`, `brain/src/loopi_brain/web/routes.py` and `brain/src/loopi_brain/web/templates/pipeline.html`:

```diff
diff --git a/brain/src/loopi_brain/pipeline/status.py b/brain/src/loopi_brain/pipeline/status.py
--- a/brain/src/loopi_brain/pipeline/status.py
+++ b/brain/src/loopi_brain/pipeline/status.py
@@ -8,6 +8,7 @@ import httpx
 from procrastinate.exceptions import ConnectorException
 from psycopg_pool import AsyncConnectionPool
 
+from .. import store
 from ..config import Settings
 from ..health import HEARTBEAT_MAX_AGE_S
 from ..providers.config import ProvidersConfigError, load_providers
@@ -138,6 +139,31 @@ async def pipeline_status(pool: AsyncConnectionPool) -> dict:
     }
 
 
+async def understanding_status(pool: AsyncConnectionPool, zone) -> dict:
+    """The understanding jobs at a glance: chapters waiting for a summary, the newest recap, failed recaps,
+    retention (days, on or off)."""
+    async with pool.connection() as conn:
+        cur = await conn.execute(
+            "SELECT count(*) AS n FROM chapter WHERE summary_signature IS DISTINCT FROM signature"
+        )
+        waiting = (await cur.fetchone())["n"]
+        cur = await conn.execute("SELECT day, generated_at FROM day_recap ORDER BY generated_at DESC LIMIT 1")
+        recap = await cur.fetchone()
+        cur = await conn.execute(
+            "SELECT count(*) AS n FROM procrastinate_jobs WHERE task_name = %s AND status = 'failed'",
+            (jobs.WRITE_RECAP,),
+        )
+        recaps_failed = (await cur.fetchone())["n"]
+    return {
+        "chapters_waiting": waiting,
+        "recaps_failed": recaps_failed,
+        "last_recap": (
+            {"day": recap["day"], "at": recap["generated_at"].astimezone(zone).strftime("%d/%m %H:%M")} if recap else None
+        ),
+        "retention": await store.get_retention(pool),
+    }
+
+
 async def model_health(settings: Settings, client: httpx.AsyncClient) -> dict:
     """Is the Ollama server reachable, and does it have every model providers.yaml names?"""
     try:
```

```diff
diff --git a/brain/src/loopi_brain/web/routes.py b/brain/src/loopi_brain/web/routes.py
--- a/brain/src/loopi_brain/web/routes.py
+++ b/brain/src/loopi_brain/web/routes.py
@@ -208,6 +208,7 @@ async def pipeline_page(request: Request):
         {
             **await status.pipeline_status(state.pool),
             "health": await status.model_health(state.settings, state.http),
+            "understanding": await status.understanding_status(state.pool, state.settings.zone),
         },
     )
 
```

```diff
diff --git a/brain/src/loopi_brain/web/templates/pipeline.html b/brain/src/loopi_brain/web/templates/pipeline.html
--- a/brain/src/loopi_brain/web/templates/pipeline.html
+++ b/brain/src/loopi_brain/web/templates/pipeline.html
@@ -21,6 +21,16 @@
   {% endfor %}
   </ul>
 </div>
+<div class="card">
+  <h2>Hiểu ngày</h2>
+  {% set u = understanding %}
+  <ul>
+    <li>Chia chương: mỗi giờ (phút thứ 5) · chương chờ tóm tắt: {{ u.chapters_waiting }}</li>
+    <li>Recap: lúc 23:30, và cho ngày trước khi mọi chương của ngày đó đã xong · gần nhất:
+      {% if u.last_recap %}<a href="/?day={{ u.last_recap.day }}">{{ u.last_recap.day }}</a> (viết lúc {{ u.last_recap.at }}){% else %}chưa có{% endif %}{% if u.recaps_failed %} · <span class="err">{{ u.recaps_failed }} recap lỗi (xem Job lỗi)</span>{% endif %}</li>
+    <li>Dọn dữ liệu cũ: 03:00 mỗi đêm · {% if u.retention.enabled %}đang bật, giữ video thô {{ u.retention.video_days }} ngày, audio thô {{ u.retention.audio_days }} ngày{% else %}<strong>đang tắt</strong>, không xoá gì{% endif %} (<a href="/settings">Cài đặt</a>)</li>
+  </ul>
+</div>
 <div class="card">
   <h2>Máy chủ model</h2>
   {% if health.error %}<p class="err">Cấu hình provider lỗi: {{ health.error }}</p>
```

- [ ] **Step 4: Update the deploy files (never run them here)**

Apply to `deploy/docker-compose.yml`, `deploy/providers.yaml`, `deploy/.env.example` and `deploy/README.md`:

```diff
diff --git a/deploy/docker-compose.yml b/deploy/docker-compose.yml
--- a/deploy/docker-compose.yml
+++ b/deploy/docker-compose.yml
@@ -23,6 +23,8 @@ services:
       LOOPI_TZ: ${LOOPI_TZ:-Asia/Seoul}
       LOOPI_OLLAMA_URL: http://models:11434
       LOOPI_PROVIDERS_FILE: /etc/loopi/providers.yaml
+      # Optional (spec §9.3): text tasks may use Anthropic only when this is set AND providers.yaml routes them there.
+      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
     volumes:
       - ${LOOPI_DATA_DIR:-/srv/loopi}:/srv/loopi
       - ./providers.yaml:/etc/loopi/providers.yaml:ro
@@ -51,6 +53,10 @@ services:
       LOOPI_PROVIDERS_FILE: /etc/loopi/providers.yaml
       LOOPI_MODEL_DIR: /srv/loopi/models
       LOOPI_ASR_MAX_GAIN_DB: ${LOOPI_ASR_MAX_GAIN_DB:-30}
+      # Two slots for live work (video, captions, ASR, markers, embeddings) and one more, used by at most one
+      # chapter/recap job at a time (job lock "llm"; those jobs also have the lowest priority).
+      LOOPI_WORKER_CONCURRENCY: "3"
+      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
       # The container user has no home directory; Hugging Face caches go next to the models.
       HF_HOME: /srv/loopi/models/hf
       # Whisper sets its own thread count (LOOPI_WHISPER_CPU_THREADS); keep every other library
@@ -80,6 +86,10 @@ services:
     image: ollama/ollama:0.35.1
     environment:
       OLLAMA_KEEP_ALIVE: 30m
+      # Two models at most (measured 2026-10-16: minicpm-v ≈ 2.9 GB, qwen3-embedding ≈ 1.3 GB, qwen3:8b with an
+      # 8192-token context ≈ 6–6.5 GB, qwen3:4b ≈ 3.5 GB; the host had 7.5 GB available). A chapter, recap or
+      # chat run unloads one of the two all-day models and leaves 1 minute after its last request
+      # (LOOPI_OLLAMA_TEXT_KEEP_ALIVE): peak ≈ 9.4–10 GB instead of ≈ 4.2 GB.
       OLLAMA_MAX_LOADED_MODELS: "2"
       OLLAMA_NUM_PARALLEL: "1"
     volumes:
```

```diff
diff --git a/deploy/providers.yaml b/deploy/providers.yaml
--- a/deploy/providers.yaml
+++ b/deploy/providers.yaml
@@ -12,5 +12,7 @@ tasks:
   chapter_summary: { provider: ollama, model: "qwen3:4b" }
   recap:           { provider: ollama, model: "qwen3:8b" }
   chat:            { provider: ollama, model: "qwen3:8b" }
-  # Cloud fallbacks need the Anthropic provider (P2b) and ANTHROPIC_API_KEY, e.g.:
+  # Optional cloud routes, for text tasks only (chapter_summary, recap, chat), used only when ANTHROPIC_API_KEY is
+  # set in deploy/.env (without it they are skipped). Captions, embeddings and ASR always stay local. E.g.:
   # recap: { provider: ollama, model: "qwen3:8b", fallback: { provider: anthropic, model: claude-haiku-5-5 } }
+  # chat:  { provider: anthropic, model: claude-sonnet-5-5, fallback: { provider: ollama, model: "qwen3:8b" } }
```

```diff
diff --git a/deploy/.env.example b/deploy/.env.example
--- a/deploy/.env.example
+++ b/deploy/.env.example
@@ -11,3 +11,6 @@ LOOPI_WEB_USER=admin
 # Generate with: docker run --rm caddy:2.10.2 caddy hash-password --plaintext 'your-password'
 # Keep the single quotes: bcrypt hashes contain '$'.
 LOOPI_WEB_PASSWORD_HASH='paste-the-bcrypt-hash-here'
+# Optional: lets deploy/providers.yaml route text tasks (chapters, recap, chat) to Anthropic. Only text is ever
+# sent. Leave empty to stay 100 % local.
+ANTHROPIC_API_KEY=
```

```diff
diff --git a/deploy/README.md b/deploy/README.md
--- a/deploy/README.md
+++ b/deploy/README.md
@@ -50,3 +50,33 @@ Requirements: Docker with Compose v2 on the Linux PC, port 8443 reachable on the
   `view/**/*.opus` to rebuild the cached copies.
 - `docker compose logs -f worker` follows the pipeline; `docker compose exec worker loopi-brain worker-health`
   checks its heartbeat.
+
+## Understanding and pages (P2b)
+
+- **Hôm nay** (`/`, any day with `/?day=YYYY-MM-DD`): the day's recap, the "việc cần làm hoặc đã hứa" list (Xong /
+  Bỏ qua / Mở lại), then the chapters in order with their pictures (320 px thumbnails, made on first view under
+  `thumbs/`), marked moments and speech (with playback while the raw audio is kept). "Chi tiết theo giờ" (`/hours`)
+  is the P2a hourly view.
+- Chapters are built every hour (minute 5) by `qwen3:4b`; a day's chapters settle ("closed") once everything recorded
+  up to them is analysed and 30 minutes old. The recap (`qwen3:8b`) is written at 23:30 (again at 23:30 if
+  "Tạo lại recap" wrote one earlier), and for an earlier day as soon as all its chapters are settled, and once more
+  if its chapters change afterwards. Days analysed before this upgrade get chapters one day per hour, newest first,
+  then their recaps. A failed recap shows on its Day page and on "Xử lý".
+- The worker runs 3 jobs at once (`LOOPI_WORKER_CONCURRENCY`); chapter and recap jobs take at most one slot and
+  wait behind captions and transcriptions (lowest priority), so live recordings keep their latency.
+- **Tìm kiếm** (`/search`): plain search by meaning and by words; "Hỏi" answers from the matching moments only, cites
+  them, and streams the answer (on a CPU the first words take tens of seconds). No matching moment: "Không tìm thấy".
+- **Cài đặt** (`/settings`): providers (read only; edit `deploy/providers.yaml`, then
+  `docker compose up -d api worker`), how many days raw video and audio are kept (7 and 7 by default, spec D20),
+  "Quên khoảng thời gian" (preview, then permanent deletion of everything recorded in the range, at most 7 days at a
+  time; an audit row keeps the counts only), and reprocessing a day with the current models.
+- Retention is **off** after installing: Cài đặt shows how many recordings (and GB) it would delete; once turned on
+  there, it runs at 03:00 local time: raw files (and their MP4/playback copies) of finished recordings older than the
+  kept days are deleted and the pages say "đã hết hạn". Keyframes, transcripts, chapters and recaps stay. The same
+  03:00 run (on or off) retries files a "Quên khoảng thời gian" could not delete at once.
+- Memory: Ollama keeps at most 2 models (`OLLAMA_MAX_LOADED_MODELS`). Measured: minicpm-v ≈ 2.9 GB, qwen3-embedding
+  ≈ 1.3 GB; `qwen3:8b` with its 8192-token context ≈ 6–6.5 GB and `qwen3:4b` ≈ 3.5 GB, so a recap or a chat raises
+  Ollama from ≈ 4.2 GB to ≈ 9.4–10 GB; text models unload 1 minute after their last request. A text request may
+  take up to 15 minutes on the CPU (`LOOPI_LLM_TIMEOUT_S`, 900 s).
+- Optional Anthropic text provider: set `ANTHROPIC_API_KEY` in `deploy/.env` and route a text task to `anthropic` in
+  `deploy/providers.yaml` (examples there). Images and audio are never sent; without the key those routes are skipped.
```

`deploy/smoke.sh` is unchanged: it cannot log in to the web UI (only the password hash is known), and the P2b pages are checked in Task 14 through the api container.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_chapters.py tests/test_web_pipeline.py tests/test_deploy.py -q && bash -n ../deploy/smoke.sh && echo smoke-syntax-ok`
Expected: `42 passed` and `smoke-syntax-ok`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `515 passed, 1 skipped`, `TOTAL … 98%`.

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/pipeline/status.py brain/src/loopi_brain/web/routes.py \
  brain/src/loopi_brain/web/templates/pipeline.html brain/tests/test_web_pipeline.py brain/tests/test_deploy.py \
  deploy/docker-compose.yml deploy/providers.yaml deploy/.env.example deploy/README.md
git commit -F - <<'EOF'
feat(deploy): P2b on /pipeline, 3 worker slots, 2 loaded models, optional Anthropic key

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

After this task: the final whole-branch review (`54e22f3..feat/p2b`) and its fix wave, as in P2a, before Task 14.

---

### Task 14: Deploy on the live stack, first chapters and recaps, measurements, handoff (controller-executed)

This task replaces the live P2a stack with P2b and runs it on the owner's real recordings. The controller runs it, not a subagent, after the Tasks 1–13 final review and fix wave, **not before 2026-10-17 04:28 KST** (the end of P2a Task 11's 24 h CPU window: restarting the worker earlier invalidates it) and **only after the owner's yes**. Stop and ask the owner before Step 3 (it migrates the live database), at Step 7 (memory threshold), at Step 8 (retention deletes recordings) and before committing anything that quotes recorded speech or captions (nothing in this task should).

**Files:**
- Modify: `docs/HANDOFF.md` (one rewrite), create `docs/superpowers/ledgers/p2b-progress.md` (the sanitized ledger)
- Live, not committed: branch `feat/phase2` (fast-forwarded), the compose project `deploy` (rebuilt), `~/loopi-backups/`

**Interfaces:**
- Consumes: the whole branch; the live compose project `deploy` running from `/home/soul/loopi-p2/deploy` (data dir and secrets from `/home/soul/loopi-p2/deploy/.env`, volumes `deploy_db`, `deploy_ollama`, `deploy_caddy_data`).
- Produces: P2b live; first chapters, recaps and commitments on real data; latency, memory and timing numbers in the ledger; the owner's decision on retention; the handoff.

Facts: every compose command names the project (`-p deploy`); never pass `-v` to `down`, never prune. The P2b worker sets `LOOPI_WORKER_CONCURRENCY=3` and Ollama keeps 2 models (Task 13).

**Rollback to P2a** (any step; uses `$ROLLBACK` from Step 3):

```bash
cd /home/soul/loopi-p2/deploy
docker compose -p deploy stop worker
docker compose -p deploy exec -T db psql -U loopi -d loopi -c "
DELETE FROM procrastinate_jobs WHERE task_name IN
  ('plan_chapters', 'build_chapters', 'plan_recaps', 'write_recap', 'apply_retention');"
cd /home/soul/loopi-p2 && git checkout --detach "$ROLLBACK" && cd deploy && docker compose -p deploy up -d --build
```

The worker is stopped first so no P2b job is running; P2b's job rows (waiting, failed or done) are removed because the P2a worker does not know those task names and would dead-letter them; their periodic records are unlinked by Procrastinate's delete trigger. P2a ignores the new tables and the new column, so no restore is needed (restore the Step 2 dump only if the migrated database itself must go). Back on P2b later: `git -C /home/soul/loopi-p2 checkout feat/phase2` and `up -d --build`.

- [ ] **Step 1: Preconditions**

```bash
date '+%F %T %Z'                                            # must be after 2026-10-17 04:28 KST
git -C /home/soul/loopi-p2 log -1 --format=%s -- docs/superpowers/ledgers/p2a-progress.md
grep -n "Task 11" /home/soul/loopi-p2/docs/superpowers/ledgers/p2a-progress.md | tail -3   # Task 11 closed?
git -C /home/soul/loopi-p2b status --short                 # must print nothing
git -C /home/soul/loopi-p2 status --short                  # must print nothing (deploy/.env is git-ignored)
git -C /home/soul/loopi-p2 rev-parse --abbrev-ref HEAD     # expected: feat/phase2
git -C /home/soul/loopi-p2b merge-base --is-ancestor feat/phase2 feat/p2b && echo "fast-forward possible"
```

If it is earlier than 2026-10-17 04:28 KST, or the P2a ledger does not record Task 11's measurement window as finished, stop and wait. If `feat/phase2` moved since `54e22f3` (e.g. the P2a Task 11 report), merge it into `feat/p2b` first, test, commit, and check again:

```bash
cd /home/soul/loopi-p2b && git merge --no-ff --no-commit feat/phase2
cd brain && uv run pytest -q      # all pass; resolve conflicts test-first and record them in the ledger
cd .. && git commit -F - <<'EOF'
chore: merge feat/phase2 into feat/p2b before the P2b deploy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

Ask the owner (Vietnamese): "P2b đã qua review. Mình sẽ đưa `feat/phase2` lên `feat/p2b`, sao lưu database rồi build lại stack đang chạy (api, worker, models; mất vài phút). Việc dọn dữ liệu cũ (xoá video/audio thô cũ hơn 7 ngày) vẫn tắt cho tới khi bạn đồng ý riêng. Đồng ý không?" Continue only on yes.

- [ ] **Step 2: Baselines, then back up the live database**

Before anything changes, record the P2a latency and memory baseline in the ledger (open `https://<LAN IP>:8443/pipeline` with the owner, or read the same numbers in SQL):

```bash
cd /home/soul/loopi-p2/deploy
docker compose -p deploy exec -T db psql -U loopi -d loopi -c "
SELECT 'video→caption' AS path, count(*), round((percentile_cont(0.95) WITHIN GROUP (ORDER BY s) / 60)::numeric, 1) AS p95_min
  FROM (SELECT extract(epoch FROM k.captioned_at - i.ended_at)::float8 AS s FROM keyframe k JOIN item i ON i.id = k.item_id
         WHERE k.captioned_at > now() - interval '24 hours') t
UNION ALL
SELECT 'audio→transcript', count(*), round((percentile_cont(0.95) WITHIN GROUP (ORDER BY s) / 60)::numeric, 1)
  FROM (SELECT extract(epoch FROM analyzed_at - ended_at)::float8 AS s FROM item
         WHERE kind = 'audio' AND analysis_state = 'done' AND analyzed_at > now() - interval '24 hours') t;"
free -m; swapon --show
mkdir -p ~/loopi-backups
docker compose -p deploy exec -T db pg_dump -U loopi -Fc loopi > ~/loopi-backups/p2a-$(date +%F-%H%M).dump
ls -l ~/loopi-backups/
```

Expected: two latency rows (the P2a baseline), the memory and swap figures, and a new, non-empty `.dump` file.

- [ ] **Step 3: Fast-forward `feat/phase2`, check the models, redeploy, smoke test**

```bash
ROLLBACK="$(git -C /home/soul/loopi-p2 rev-parse feat/phase2)"; echo "rollback target: $ROLLBACK"   # into the ledger
git -C /home/soul/loopi-p2 merge --ff-only feat/p2b
git -C /home/soul/loopi-p2 log --oneline -1                # the feat/p2b head
cd /home/soul/loopi-p2/deploy
docker compose -p deploy exec -T models ollama list         # minicpm-v4.6:1b, qwen3-embedding:0.6b, qwen3:4b, qwen3:8b
```

If a model is missing: `docker compose -p deploy run --rm --no-deps worker loopi-brain models pull`.

```bash
docker compose -p deploy up -d --build                      # api applies 003_understanding.sql at start
docker compose -p deploy ps                                 # db, api, worker, models, proxy: running / healthy
COMPOSE_PROJECT_NAME=deploy SMOKE_ANALYSIS_TIMEOUT=600 ./smoke.sh   # → OK: smoke test passed
docker compose -p deploy exec -T db psql -U loopi -d loopi -tAc "SELECT name FROM schema_migration ORDER BY applied_at DESC LIMIT 2"
docker compose -p deploy exec -T db psql -U loopi -d loopi -tAc "SELECT value FROM setting WHERE key = 'retention'"
```

Expected: the smoke test passes; `003_understanding.sql` is listed; no `retention` row (retention is off by default).

- [ ] **Step 4: The pages answer**

Inside the network the api needs no password (Caddy does the basic auth):

```bash
docker compose -p deploy exec -T api python - <<'PY'
import urllib.request
for path in ("/", "/hours", "/search?q=test", "/settings", "/pipeline"):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=60) as r:
        print(path, r.status, len(r.read()))
PY
```

Expected: `200` for each path. Then ask the owner to open `https://<LAN IP>:8443/` and check "Hôm nay", "Tìm kiếm", "Cài đặt" (retention: "đang tắt") and the "Hiểu ngày" card on "Xử lý".

- [ ] **Step 5: First chapters, recaps and commitments on real data**

The hourly planner runs at minute 5: it builds today and yesterday at once and the older days analysed by P2a one per hour, newest first (Task 4). Wait (Monitor with an until-loop) until `SELECT count(*) FROM chapter` is above 0 and no `build_chapters` job is `todo` or `doing`, then:

```bash
docker compose -p deploy exec -T db psql -U loopi -d loopi <<'SQL'
SELECT day, count(*) AS chapters, count(*) FILTER (WHERE closed) AS closed, count(summary) AS summarised,
       min(model_version) AS model FROM chapter GROUP BY day ORDER BY day;
SELECT day, length(markdown) AS chars, model_version, generated_at, stale FROM day_recap ORDER BY day;
SELECT day, status, count(*) FROM commitment GROUP BY 1, 2 ORDER BY 1, 2;
SELECT task_name, status, count(*) FROM procrastinate_jobs
 WHERE task_name IN ('plan_chapters', 'build_chapters', 'plan_recaps', 'write_recap', 'apply_retention')
 GROUP BY 1, 2 ORDER BY 1, 2;
SELECT task, day, left(error, 200) AS error FROM day_job_error;
SQL
```

Expected: the days built so far have chapters, closed and summarised by `ollama:qwen3:4b`; each settled day gets a recap (`ollama:qwen3:8b`) within about 15 minutes of its chapters closing; no `failed` job and no `day_job_error` row. Show titles, summaries and the recaps to the owner **in the browser** (nothing quoted into the repository); ask whether the chapter cuts look right (P2c tunes `ChapterParams` and the prompts; note the owner's remarks in the ledger).

- [ ] **Step 6: Live latency during and after the catch-up**

While old days are being caught up (chapter and recap jobs running), and again the next day, run the latency query of Step 2 and compare with the baseline. Expected: the p95 recording → caption and recording → transcript stay within the spec's 15 minutes between 08:00 and 22:00 (chapter and recap jobs have the lowest priority and at most one of the worker's 3 slots). If p95 grows beyond 15 minutes during the catch-up, record it and tell the owner; the catch-up ends by itself (one old day per hour).

- [ ] **Step 7: Memory and timing of the text models — a controlled peak test**

Measure memory before, during and after the first recap and a chat, while captions run:

```bash
cd /home/soul/loopi-p2/deploy
free -m; swapon --show; docker compose -p deploy exec -T models ollama ps     # before
```

Ask for a recap of an earlier day now (the owner clicks "Tạo lại recap" on that day's page) and, while it runs, ask one question (chat latency, event names and times only, never text):

```bash
docker compose -p deploy exec -T api python - <<'PY'
import time, urllib.parse, urllib.request
body = urllib.parse.urlencode({"q": "Hôm qua mình đã làm gì?"}).encode()
request = urllib.request.Request("http://127.0.0.1:8000/chat", data=body, headers={"HX-Request": "true"})
start, first_token = time.monotonic(), None
with urllib.request.urlopen(request, timeout=1200) as response:
    for raw in response:
        line = raw.decode().strip()
        if line.startswith("event: "):
            name = line[7:]
            if name == "token" and first_token is None:
                first_token = time.monotonic() - start
            if name in ("filter", "sources", "answer", "error", "done"):
                print(f"{name} at {time.monotonic() - start:.1f} s")
print(f"first token at {first_token:.1f} s" if first_token else "no token (not found, or an error)")
PY
```

During the recap, sample every 30 s (Monitor) `free -m`, `swapon --show` and `docker compose -p deploy exec -T models ollama ps`; after it, read `docker compose -p deploy exec -T models cat /sys/fs/cgroup/memory.peak` and the job times:

```bash
docker compose -p deploy exec -T db psql -U loopi -d loopi -c "
SELECT j.task_name, j.args->>'day' AS day, extract(epoch FROM s.at - st.at)::int AS seconds
  FROM procrastinate_jobs j
  JOIN LATERAL (SELECT max(at) AS at FROM procrastinate_events WHERE job_id = j.id AND type = 'started') st ON true
  JOIN LATERAL (SELECT max(at) AS at FROM procrastinate_events WHERE job_id = j.id AND type = 'succeeded') s ON true
 WHERE j.task_name IN ('build_chapters', 'write_recap') AND s.at IS NOT NULL ORDER BY j.id;"
```

Written thresholds (record every number in the ledger):
- **Memory:** if `available` in `free -m` drops below 1 GB, or swap use grows by more than 1 GB during the test, the host swaps hard: stop, tell the owner, and roll back (see above) unless the owner prefers to stay on P2b with `LOOPI_LLM_NUM_CTX=4096` (smaller KV cache; then also lower the recap's `PROMPT_BUDGET` test-first) — record the ruling.
- **Timing:** if a recap run takes more than 630 s (70 % of `LOOPI_LLM_TIMEOUT_S`), tell the owner and lower `RECAP_MAX_TOKENS` / `PROMPT_BUDGET` test-first (or raise the timeout), and record the ruling. A recap that still times out is retried 3 times and then shown as failed on its Day page and on "Xử lý" — never silently.
- **Streaming:** ask the owner to ask one question in the browser and confirm the answer appears piece by piece (Caddy must flush `text/event-stream`; if it only appears at the end, add `flush_interval -1` to the web `reverse_proxy` in `deploy/Caddyfile`, test-first in `test_deploy.py`, and record the ruling).

- [ ] **Step 8: Retention — stop and ask before anything is deleted**

Retention is off. Read what it would delete with the current kept days (read only):

```bash
docker compose -p deploy exec -T db psql -U loopi -d loopi -c "
SELECT kind, count(*) AS recordings, round(sum(size_bytes) / 1e9, 2) AS gb FROM item
 WHERE kind IN ('video', 'audio') AND raw_deleted_at IS NULL AND analysis_state IN ('done', 'skipped')
   AND ended_at < now() - interval '7 days' GROUP BY kind;"
```

(The same numbers are on Cài đặt: "Nếu chạy bây giờ sẽ xoá …".) Tell the owner the counts and GB and ask: "Bật dọn dữ liệu cũ không? Mỗi đêm lúc 03:00 sẽ xoá vĩnh viễn video và audio thô cũ hơn 7 ngày (đã phân tích xong; keyframe, transcript, chương, recap vẫn giữ). Bản sao lưu database không chứa file media." **Only on an explicit yes**, the owner ticks "Bật dọn dữ liệu cũ" on Cài đặt (or, with that yes, the controller runs `docker compose -p deploy exec -T db psql -U loopi -d loopi -c "INSERT INTO setting (key, value) VALUES ('retention', '{\"video_days\": 7, \"audio_days\": 7, \"enabled\": true}') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"`). Without a yes, retention stays off; record the decision as a ruling. The day after it is turned on, check:

```bash
docker compose -p deploy exec -T db psql -U loopi -d loopi -c "
SELECT kind, count(*) FILTER (WHERE raw_deleted_at IS NOT NULL) AS expired, count(*) AS total FROM item GROUP BY kind;"
docker compose -p deploy logs --since 24h worker | grep -c "retention (video"
```

Expected: one retention log line per night; only video and audio rows older than the kept days are expired.

- [ ] **Step 9: Handoff and ledger**

Rewrite `docs/HANDOFF.md` once, covering: P1c (done, with waivers: its execution ledger is the git-ignored `/home/soul/loopi/.superpowers/sdd/2026-10-14-p1c-buildroot-dev-image/`; ask the owner before committing a sanitized copy as `docs/superpowers/ledgers/p1c-progress.md`), the audio level work (D19; the agent gain is on `feat/phase1`, smoke8 flashed), P2a (live since 2026-10-15 23:51 KST; its Task 11 results), D20 and P2b (this plan, commit range, Task 14 results: latency before/during/after, memory peak, recap and chat times, the retention decision), where the live stack runs from (`/home/soul/loopi-p2/deploy`, branch `feat/phase2`) and the warning that this worktree must not be removed before the stack is redeployed elsewhere, the rollback procedure above, the branches (`feat/phase1`: agent + OS + D19 spec; `feat/phase2` = P2; `main`), and what comes next (P2c evaluation; P3 MCP + vlog). Copy the sanitized ledger to `docs/superpowers/ledgers/p2b-progress.md`.

```bash
cd /home/soul/loopi-p2b
git add docs/HANDOFF.md docs/superpowers/ledgers/p2b-progress.md
git commit -F - <<'EOF'
docs: handoff after P2b (P1c, P2a, audio level D19/D20, P2b) and the P2b ledger

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
git -C /home/soul/loopi-p2 merge --ff-only feat/p2b   # docs only: no redeploy
```

Pushing any branch is outward-facing: ask the owner first.

---

## Rulings taken while writing this plan

Copy these into the ledger before Task 1. Each: decision — why — cost if wrong.

- **R1** Task 1 first takes the spec from `feat/phase1` (its only difference is the D19 text), then adds D20; the brief's "§9.2 audio step 3" is step 4 once D19 is in — `feat/p2b` was branched before D19 reached the spec although the brain code already cites "spec D19" — cost if wrong: the next merge of `feat/phase1` conflicts on four spec lines, resolved by taking `feat/p2b`'s (a superset).
- **R2** Local schedules are decided in Python, not by cron: Procrastinate evaluates cron in UTC and `LOOPI_TZ` is a runtime setting (DST too). Chapters: planner at `5 * * * *` (hourly in any whole-hour zone); recap: planner every 15 minutes, today from 23:30 local; retention: hourly tick that acts only when the tick's local hour is 3 — cost if wrong: a recap can come up to 15 minutes after 23:30; a half-hour time zone gets chapters at :35.
- **R3** The recap planner also writes earlier days once every chapter of the day is closed (backlog days, a worker that was down at 23:30), and again once when the day changed after its recap (R39); it never re-defers a day whose recap job waits, runs or failed — spec: "Recap tự sinh mỗi tối" must hold after outages — cost if wrong: a backlog of days produces several recap runs in a row (CPU, at night, lowest priority).
- **R4** A chapter closes when the next one starts before the horizon `min(now − 30 min, first analysis still to come that day)` (pending items, uncaptioned keyframes, unembedded moments; dead-lettered work does not count); the last one when the whole day is behind it. Moments appearing or vanishing inside closed chapters (count mismatch) reopen the day. This is how "only the open tail is recomputed" stays correct with backlogs and offline devices — cost if wrong: a day with a moment that never gets embedded stays open (rebuilt every hour without model calls until it settles).
- **R5** Segmentation parameters live in `ChapterParams` (gap 20 min, min 10 min, max 3 h, caption drift 0.35, speech drift 0.45, window 4, confirmation by the next moment of the same kind), not in env settings — P2c tunes them with the golden set; fewer knobs — cost if wrong: a change needs a deploy.
- **R6** A summary is rewritten only when the chapter's members change (`signature` vs `summary_signature`); today's open chapter is summarised once it has 3 moments; a changed chapter keeps its row and shows the old summary marked "(đang cập nhật)" until the new one is written — cost if wrong: the open chapter is summarised again up to once an hour (≈ 30–60 s of `qwen3:4b`).
- **R7** No per-day bookkeeping table: per-day jobs are found by their queueing locks (`chapters:<day>`, `recap:<day>`) and their last error is in `day_job_error` (Procrastinate keeps no error text) — cost if wrong: queries on `procrastinate_jobs` by queueing lock (a small table, pruned weekly).
- **R8** `build_chapters` and the planners are auto-rerun (no retry button, newest failure kept; the label says when: "tự chạy lại ở lần lên lịch chia chương (mỗi giờ; chương tóm tắt lỗi: sau 6 giờ)", R40); `write_recap` keeps its retry button, "Tạo lại recap" on the Day page asks for a new one at any time, and a failed recap is shown on its Day page and on the "Hiểu ngày" card (R44) — cost if wrong: a failed recap waits for a click.
- **R9** Outages: `ProviderUnavailable` while summarising stops the build without an error; the recap job returns without writing; the recap planner defers nothing while `task_ready("recap")` is false (P2a's "no job just to fail") — cost if wrong: with an Anthropic-only route (which cannot be health-checked) an outage costs one cheap failing run per 15 minutes.
- **R10** The recap model answers JSON lists; the brain writes the markdown with the three fixed headings of §10.2; commitments cite short references (`S3`, `M1`) that the brain maps to moment ids — small models mangle 26-character ULIDs, and the recap's third section and the commitment list always agree — cost if wrong: the model cannot style the markdown.
- **R11** The recap reads the chapters, every marker and the day's 60 longest speech lines (≤ 9000 characters): a lone "Thank you." goes first — cost if wrong: a short but real promise ("Ừ, mai 9 giờ") can be left out on a talkative day.
- **R12** Writing a recap again keeps the commitments the user marked done or dismissed and skips new ones with the same source moment or text — cost if wrong: a reworded duplicate reappears as open.
- **R13** `commitment.source_moment_id` is `ON DELETE SET NULL` (reprocessing transcripts must not erase the user's list); "Quên khoảng thời gian" deletes the commitments sourced in its range explicitly — cost if wrong: a reprocessed day's commitments lose their link to the source.
- **R14** Text models get `keep_alive` 1 minute (`LOOPI_OLLAMA_TEXT_KEEP_ALIVE`), `num_ctx` 8192 (`LOOPI_LLM_NUM_CTX`) and a 900 s timeout (`LOOPI_LLM_TIMEOUT_S`) per request; `OLLAMA_MAX_LOADED_MODELS` **stays 2** and the models service gets no `mem_limit` (R43) — cost if wrong: a chapter, recap or chat run unloads the vision or embedding model, which reloads afterwards (seconds from the page cache).
- **R15** Prompts are bounded in characters (7000 for a chapter, 9000 for the recap, 8 × 400 for chat sources), not tokens — no tokenizer in the brain — cost if wrong: dense Korean text can approach 6000 tokens, still inside 8192 with the answer.
- **R16** Chat citations are `[n]` source numbers that the brain maps to `moment_id`s (spec "trích nguồn bằng moment_id"); numbers that are not sources are dropped; an answer citing nothing gets a warning — cost if wrong: none expected.
- **R17** The time filter is days only (local), read by the chat model as JSON `{"from", "to"}` and cleaned (order, one end copies the other, no future, ≤ 366 days); anything unusable means no filter (the brief's "deterministic fallback"); a question without a time word (`chat.mentions_time`, vi/en/ko words or a date) skips that call (review M16) — cost if wrong: "sáng nay" searches the whole day; a question with a time word costs one more `qwen3:8b` call (5–15 s on a CPU); a time expression the word list misses gets no filter.
- **R18** Retrieval: cosine distance ≤ 0.6 (`LOOPI_SEARCH_MAX_DISTANCE`), word similarity ≥ 0.3 (`LOOPI_SEARCH_MIN_SIMILARITY`), 40 candidates each, RRF k = 60, 8 chat sources, 30 search results; the question carries Qwen3-Embedding's query instruction — P2c sets the thresholds from recall@5 — cost if wrong: until tuned, weak sources or "không tìm thấy" for paraphrases; Vietnamese typed without diacritics only matches by meaning (trigram similarity 0.29).
- **R19** The chat answer streams as SSE over a `fetch` POST with `HX-Request: true` (an `EventSource` can only GET, which would break the CSRF rule); payloads are JSON; sources and the final answer are server-escaped HTML, tokens plain text — cost if wrong: needs a browser with fetch streams (every current one).
- **R20** The Day page is the landing page `/` ("Hôm nay"); the P2a hourly view moves to `/hours` ("Chi tiết theo giờ"). Nine P2a test calls in `test_web.py`, `test_web_pipeline.py` and `test_audio_playback.py` move to `/hours`, and the hourly view's picture assertion expects a thumbnail — cost if wrong: an old bookmark of `/?day=…` shows the chapter view of the same day.
- **R21** Thumbnails are made on demand by the api (Pillow, 320 px wide, kept under `thumbs/`), also on `/hours` (P2a deferred M8); every stored path is checked with `media.inside` before a file is read, written or deleted — cost if wrong: the first view of a busy day makes ~300 thumbnails (≈ 10 ms each).
- **R22** A 40-line markdown renderer of our own (headings, lists, paragraphs, bold; everything escaped first) instead of a dependency — cost if wrong: richer markdown from a model shows as plain text.
- **R23** Retention days and the on/off switch live in the `setting` table (default 7 and 7, D20, **off**), edited on Cài đặt (R46); when on, each batch holds its rows (`FOR UPDATE SKIP LOCKED`) while their files go, then marks them (`raw_deleted_at`, `view_path` NULL); a row whose file cannot be deleted is tried again the next night; at most 40 batches of 500 recordings of each kind per night — cost if wrong: a backlog above 20 000 recordings of a kind takes more than one night.
- **R24** "Quên khoảng thời gian": a recording that overlaps the range goes whole; at most **7 days** per request (review M10: a request stays a few seconds); the recaps of touched days are **deleted**, not marked for regeneration — a **deliberate deviation from §9.5's wording** ("đánh dấu … cần sinh lại"), because they quote what is forgotten; the planners regenerate them (review M15) — and overlapping chapters lose their title and summary at once; the transaction holds the touched days' locks (R41); the audit row keeps the range, counts and the paths still to delete (R49), never content; files go after the commit. Uploads the device still holds for that range (offline) are not blocked — the device contract cannot change, and the page says so (review M3, deferred) — cost if wrong: up to 60 s beyond each edge is forgotten too; data the device sends later reappears.
- **R25** Reprocess-by-day offers captions, transcripts (only while the raw audio is kept), chapters and recap; keyframe selection is not redone (keyframes are kept for ever; the raw video may be gone) — cost if wrong: re-captioning a whole day queues ≈ 300 caption jobs.
- **R26** Anthropic is called with plain httpx (the brief: no SDK dependency), for text tasks only, without `temperature` (Claude Haiku 5.5 and Sonnet 5.5 reject non-default sampling), `effort: "low"`, thinking set explicitly with a `max_tokens` floor (R47), strict JSON schemas (limits stripped; answers still clipped by the brain); 429/5xx/529 and connection failures are `ProviderUnavailable` (fall back, no retry spent), 400/401/403/404, refusals, empty answers and answers cut off before any text `ProviderError` (fall back; dead letter when nothing is left). A route to anthropic without `ANTHROPIC_API_KEY` is skipped and Cài đặt says so — cost if wrong: a model id the account lacks shows as dead letters on chat/recap if no local fallback is configured.
- **R27** Privacy: `LOCAL_PROVIDERS = {"ollama", "local"}` is an allow-list (every other provider is cloud); `task_ready` counts only routes that may take the request (images: local only) and whose provider is set up. Two P2a tests change accordingly: `test_invalid_routes_are_rejected_at_load_time` loses the recap→anthropic case (valid now) and gains caption→anthropic and embed→anthropic; `test_routes_the_health_check_cannot_see_count_as_ready` gives the router a configured anthropic provider and asserts that without one the route is not ready — cost if wrong: none expected.
- **R28** Chapter builds and recaps share one job lock (`llm`) so two text-model runs never compete for the CPU; chat (in the api) is not serialised with them, and with `OLLAMA_NUM_PARALLEL=1` a question asked during the 23:30 recap waits until the recap's model call ends (minutes; review M11) — the 900 s timeout covers it and the time-word check (R17) usually saves one of its two calls — cost if wrong: a question during the recap takes minutes to start answering.
- **R29** The hourly planner (Task 4) also builds the newest day that has moments and no chapter (days P2a analysed before this upgrade), **one per run** (review I4/M8: the catch-up must not crowd out live work, and it now ships with the first half) — cost if wrong: one `DISTINCT` scan of `moment` per hour (≈ 0.1 s with a year of data); N old days take N hours.
- **R30** `/pipeline` gets a "Hiểu ngày" card (schedules, chapters waiting for a summary, newest recap, failed recaps, retention on/off and kept days) and the new tasks get Vietnamese labels on the failed list — the brief's "appear on /pipeline" — cost if wrong: none.
- **R31** The timezone is not editable in the UI: `LOOPI_TZ` (env) is shown read-only on Cài đặt — changing it would move every day boundary, chapter and recap — cost if wrong: changing the zone needs an env edit and a reprocess of the affected days.
- **R32** `deploy/smoke.sh` is not changed: it cannot log in to the web UI (only the bcrypt hash is known); Task 14 checks the pages through the api container. A smoke run's marker can appear in a chapter until the next hourly build (its moments are deleted; the count check reopens the day) — cost if wrong: a "smoke" line in a chapter summary for up to an hour.
- **R33** The api gets a second http client for the models (300 s timeout; `create_app(llm_client=…)`); the 5 s status client is unchanged — cost if wrong: none.
- **R34** Shared test helpers grow instead of per-file copies: `FakeOllama.reply`, NDJSON streaming and `stream_fail_after`; `insert_moment(axis=…, item_id=…, seconds=…)`; `insert_chapter` — cost if wrong: none.
- **R35** The spec's §9.4 table lists "trường chính"; the extra chapter columns (`closed`, `signature`, `summary_signature`, `last_moment_at`, …) and the tables `setting`, `forget_log`, `day_job_error` are documented in `003_understanding.sql`, not added to the spec — cost if wrong: a reader of the spec alone misses bookkeeping columns.
- **R36** Task 14 does not merge `feat/phase1` (agent and OS only; the brain does not need it) — cost if wrong: the branches stay apart until the owner merges them; the spec merge is covered by R1.
- **R37** Execution: subagent-driven, one implementer and one reviewer per task, then a final whole-branch review and one fix wave before Task 14, as in P2a.
- **R38** P2a tests changed on purpose (nothing removed): `test_db.py` (migration lists, tables and indexed foreign keys gain the 003 objects; Task 1), `test_providers.py` (`text_models` options and timeouts, Task 2; R27, Task 12), `test_config.py` (new settings, Task 2), `test_web.py`, `test_web_pipeline.py`, `test_audio_playback.py` (`/hours` and its thumbnails, R20; the "Hiểu ngày" card, Task 13), `test_deploy.py` (worker concurrency and the key, Task 13), `helpers.py` and `conftest.py` (extended) — cost if wrong: none; each change is shown as a diff in its task.


### Rulings on the independent plan review (2026-10-16, "Ready with fixes"; controller rulings applied)

- **R39 (I1)** A recap is written again when the day changed after it: today's at 23:30 if a fresh recap exists from before 23:30 (e.g. "Tạo lại recap" at noon); an earlier day's once all its chapters are closed again and `max(chapter.updated_at)` is newer than the recap; `rebuild_chapters` writes a row only when it changed, so `updated_at` means a real change (review M14) — cost if wrong: at most one extra night-time recap per day and per change.
- **R40 (I2)** The hourly planner also asks for a build of every day with a chapter still waiting for its summary (closed, or ≥ 3 moments), while `chapter_summary` can run (`task_ready`), except chapters whose summary failed less than 6 hours ago (`chapter.summary_error_at`, new in migration 003); the `build_chapters` label is corrected accordingly — cost if wrong: a chapter that always fails costs one model call every 6 hours.
- **R41 (I3)** One transaction lock per local day (`state.lock_day`, `day:<date>`) is taken by chapter rebuilds, stored summaries, stored recaps and "Quên khoảng thời gian"; summaries and recaps read the number of overlapping forget requests (`state.forgotten`) before calling the model and store their answer only if it is unchanged under the lock (else nothing is stored and the planners ask again) — cost if wrong: a forget during a summary or recap discards that model run.
- **R42 (I4)** The worker gets 3 slots (`LOOPI_WORKER_CONCURRENCY: "3"`); chapter and recap jobs keep the `llm` lock (at most one at a time) and get priority −10 (`jobs.LLM_PRIORITY`, Procrastinate 3.10 `priority`), so a free slot always takes waiting live work first; the catch-up of old days is one day per hourly run (R29); Task 14 records caption/transcript p95 latency before the deploy, during the catch-up and the next day — cost if wrong: a third concurrent job (a caption or an ASR run) competes for the 4 cores with a text model.
- **R43 (I5)** Memory: Ollama keeps at most **2** models (measured: minicpm-v ≈ 2.9 GB, qwen3-embedding ≈ 1.3 GB, qwen3:8b at 8192 tokens ≈ 6–6.5 GB, qwen3:4b ≈ 3.5 GB; 7.5 GB available, 3.4 GB of swap in use): peak ≈ 9.4–10 GB instead of ≈ 4.2 GB; text models unload after 1 minute; **no `mem_limit`** (an OOM kill inside the models container could stop a caption mid-job); Task 14 runs a controlled peak test (recap + chat while captions run) with written thresholds (available < 1 GB or swap growth > 1 GB → stop, roll back or shrink `LOOPI_LLM_NUM_CTX` on the owner's word) — cost if wrong: the host swaps during the test, caught there.
- **R44 (I6)** Text-model requests wait up to 900 s (`LOOPI_LLM_TIMEOUT_S`, streams too) instead of the client's 300 s; the recap answer is capped at 1000 tokens with smaller lists (8 events, 5 memorable, 8 commitments, 200 characters each); a recap that still times out is retried 3 times, then shown on its Day page ("Viết recap lỗi: …") and counted on the "Hiểu ngày" card, never silently; Task 14 stops if a recap run takes more than 630 s — cost if wrong: a slow night costs up to 4 × 15 minutes of CPU before the failure shows.
- **R45 (I7)** Task 14 starts only after 2026-10-17 04:28 KST with P2a Task 11 closed and the owner's yes; the rollback target is `git rev-parse feat/phase2` recorded before the fast-forward; rolling back stops the worker and deletes P2b's job rows (the P2a worker would dead-letter unknown task names) before redeploying P2a — cost if wrong: none expected.
- **R46 (I8)** Retention ships **off** (`Retention.enabled = False`): Cài đặt shows what it would delete (recordings and GB per kind) and has the switch; Task 14 stops to show the owner the counts and turns it on only on an explicit yes, otherwise it stays off and the decision is a ruling — cost if wrong: raw files pile up until the owner decides (≈ 5–10 GB of video per day).
- **R47 (I9)** Anthropic thinking is set explicitly: Haiku 5.5 `{"type": "disabled"}`, Sonnet 5.5 `{"type": "between_tools"}` (it rejects "disabled"), each with `max_tokens` ≥ 1024; any other model keeps thinking with `max_tokens` 8192; an answer cut off by `max_tokens` before any text is a `ProviderError`. Anthropic stays optional and off by default — cost if wrong: a slightly larger token bill on the rare long answer.
- **R48 (M1)** Today's page also lists the commitments still open from the previous 7 days ("Việc còn mở từ những ngày trước") — cost if wrong: none.
- **R49 (M2)** "Quên khoảng thời gian" stores the media paths to delete on its audit row (`forget_log.pending_files`, paths only) and keeps the ones it could not delete; the 03:00 tick (retention on or off) retries them — cost if wrong: a file that can never be deleted is retried every night (logged).
- **R50 (M4)** Retention holds each batch's rows while deleting their files, so a reprocess that resets an item waits and then sees `raw_deleted_at` (it only resets `done` items whose raw file is kept) — cost if wrong: none expected.
- **R51 (M6, M16)** A model answer of "Không tìm thấy trong ký ức." that cites nothing is shown as a not-found answer, without the "không trích nguồn" warning; questions without a time word skip the time-filter call (R17) — cost if wrong: none expected.
- **R52 (M7, M8, M9, M12, M14, M15)** Moments outside any chapter are shown in time order between the chapters (each run its own "Chưa chia chương" block); the "first half shippable" claim now says what an early deploy also needs; Cài đặt says which tasks send text to the cloud when a key is set; the "Hiểu ngày" test reads the card with whitespace collapsed; rebuilds touch only changed rows; R24 tags the recap deletion as a deliberate deviation from §9.5 — cost if wrong: none expected.
- **R53 (deferred minors)** M3 (drop late uploads inside a forgotten range: changes "PUT behaves exactly as before", the owner's call — listed under Deferred), M5 (dead-lettered items keep their raw files: the safe direction; `skipped` items lose theirs after the kept days as the spec says — listed), M11 (documented in R28), M13 (a caption job already running when "captions" are reprocessed stores its old-model caption: rare, and a second reprocess fixes it — listed) — cost if wrong: see each item under Deferred.

## Deferred (out of scope for P2b)

- Hallucination filtering itself, chapter and retrieval tuning, golden sets, recall@5, WER/CER: **P2c** (the prompts here only tell the models not to trust invented lines).
- Vlog (§10.3), MCP server and its token UI (§10.5): **P3**.
- Forgetting data the device has not uploaded yet (would need a device-contract change).
- Vietnamese search without diacritics (needs `unaccent` or a normalised text column): P2c, with recall@5.
- Orphan JPEGs after a worker crash (P2a deferred M7): retention does not sweep files without a row.
- Review M3: ingest could accept (201) but discard items whose `started_at` falls in a forgotten range — it would change "PUT /v1/items behaves exactly as before", so it is the owner's decision.
- Review M5: show on /pipeline how many dead-lettered items keep their raw files; keep `skipped` (unusable) recordings longer for investigation.
- Review M13: a caption job already running when "captions" are reprocessed stores the old model's caption (check the job id on write).
