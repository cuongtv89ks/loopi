# P2c — Evaluation + P2 Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure the P2 pipeline against the owner's own golden set and close P2: a Vietnamese "Đánh giá" page where the owner labels 20 clips, 30 keyframes and 25 questions (data outside git, new decision D21), an evaluation runner (`loopi-brain eval run` and a button) for WER/CER, invented text on silence, caption scores and recall@5, switchable ASR hallucination rules that are measured before any default changes, and `loopi-brain acceptance`, which prints the spec §14 P2 numbers for the acceptance report.

**Architecture:** The golden set lives in seven new tables (migration 005) and in copies under `LOOPI_EVAL_DIR` (`/srv/loopi/eval`): a golden clip is a whole recording copied byte for byte (plus a playback copy at a level you can hear), a golden keyframe a JPEG copy with the owner's scores per caption model, a question its correct moments anchored to their sources so reprocessing does not lose them. "Quên khoảng thời gian" deletes the samples of its range and sweeps their copies; retention never touches them. A run is a chain of Procrastinate jobs on the worker — one golden clip per job through the live ASR path (`asr.transcribe_file`: level normalisation, Silero VAD with the run's threshold, Whisper), lock `asr`, priority −20, so live recordings always go first — and a last job that scores every rule set from the stored segments, sums the caption scores and asks `search_moments` each answered question. Pages are Jinja + HTMX like P2b; numbers leave the brain only through `loopi-brain eval show` and `loopi-brain acceptance`, never text.

**Tech Stack:** Python 3.13, uv, FastAPI/Starlette (`Form`, `FileResponse`, `HX-Redirect`), Jinja2 + HTMX (vendored), psycopg 3, Procrastinate 3.10.0 (job locks and priorities), pgvector/pg_trgm search from P2b, faster-whisper 1.2.1 (Silero `VadOptions(threshold=…)`), ffmpeg (playback copies, as `/audio`), NumPy; pytest + pytest-asyncio + testcontainers (`pgvector/pgvector:pg17`), `FakeOllama`, `FakeTranscriber`. **No new dependency**: Levenshtein, WER and CER are written here (none is in `uv.lock`).

**Spec:** `docs/superpowers/specs/2026-10-14-loopi-design.md` (Vietnamese, binding): §13 ("Đánh giá chất lượng AI": 30 keyframes with hand-scored captions, 20 clips vi/ko/en with reference transcripts → WER/CER, 25 questions with their correct moments → recall@5), §14 P2 (average latency recording → caption/transcript ≤ 15 min between 08:00 and 22:00; a recap every evening; a WER/CER baseline for vi/ko/en, WER > 30 % → change the model or the configuration; recall@5 ≥ 70 %; CPU time per day measured), §9.2 (ASR path), §9.5 (retention, "Quên khoảng thời gian"), §10.4 (search), §10.6 (pages), D19, D20. Task 1 adds decision **D21**. The design brief (owner direction 2026-10-16) is session scratch; its decisions are restated here so this plan stands alone:
- P2 is split: **P2a** perception (live since 2026-10-15 23:51 KST), **P2b** understanding + UI (implemented on `feat/p2b`, deployed by its Task 14 after 2026-10-17 04:28 KST), **P2c** evaluation + acceptance (this plan). Vlog and MCP are P3. New models only if the 30 % rule triggers — and then Task 13 proposes options and stops for the owner.
- **D21 (owner, 2026-10-16):** golden-set data stays OUT of git: real audio clips, keyframe copies, reference transcripts and questions live under the brain's data dir (`/srv/loopi/eval` in the containers = `/home/soul/loopi-data/eval` on the host; setting `LOOPI_EVAL_DIR`) and in the brain's own database; the repo gets only result NUMBERS (WER, CER, scores, recall).
- **The owner labels** through the new Vietnamese "Đánh giá" page (1–2 h, possibly in several sittings).
- Work in the worktree **`/home/soul/loopi-p2c`**, branch **`feat/p2c`** (from `feat/p2b` @ `80a88b8` = P1 + P2a + audio level + P2b). The live stack (compose project `deploy`) runs from `/home/soul/loopi-p2/deploy` (branch `feat/phase2`). Real-world order, each step needing the owner's yes: deploy P2c (after P2b is live) → the owner labels → evaluation runs → tuning decisions → acceptance report `docs/measurements/2026-10-p2-acceptance.md` (numbers only).

**Plan shape:** 14 tasks. Tasks 1–10 are code (each one a reviewed commit on `feat/p2c`); **Tasks 11–14 are controller-executed** on the live stack, after the final review and with the owner's yes at each stop: 11 deploy, 12 the owner labels + the baseline run, 13 evidence-first tuning, 14 the CPU window, the acceptance report and the handoff.

**Ledger:** every ruling taken while executing goes to `docs/superpowers/ledgers/p2c-progress.md` (CLAUDE.md). Copy "Rulings taken while writing this plan" (end of this file) into it before Task 1.

**Prototype:** every code block below ran in a throw-away worktree replayed task by task from `80a88b8`, and every "Expected" is what that run printed (red runs: the task's test changes on the previous task's code; green runs: the whole suite with coverage). Baseline: brain **696 passed, 1 skipped, 99 %**; after Task 10: **769 passed, 1 skipped, 99 %**.

**How to read the code blocks:** new files are given whole. Changes to existing files are unified diffs against the state the previous task left: apply them from the repository root (`git apply <<'PATCH' … PATCH`) or edit by hand — every context line is exact.

**Facts the code relies on** (checked in the prototype against the installed packages and the test database):
- Procrastinate 3.10's `procrastinate_fetch_job_v2` skips a job while another job with the same `lock` is `doing`, or is `todo` with a higher priority (or the same priority and a lower id). An evaluation job (lock `asr`, priority −20) therefore never starts while a live transcription (lock `asr`, priority 0) waits, and holds Whisper for one clip only; the task decorator also takes `lock` and `priority`.
- JSONB keeps no key order (keys come back sorted by length, then bytes): the rule sets are shown in a fixed order (`runner.VARIANTS`).
- faster-whisper 1.2.1: `VadOptions(threshold=0.5, neg_threshold=None, min_speech_duration_ms=0, max_speech_duration_s=inf, min_silence_duration_ms=2000, speech_pad_ms=400)`; P2a passes `min_silence_duration_ms=500, speech_pad_ms=200`. `asr.pick_language` keeps Whisper's probability of the chosen language (`RegionResult.lang_prob`), which P2a never stored.
- `transcribe_file` is the whole live ASR path (decode, D19 level gain, VAD, Whisper per region); the P2a filters and the new rules run on its output, so one transcription can be scored under every rule set.
- Starlette's `Jinja2Templates` autoescapes; htmx follows an `HX-Redirect` response header with a full page load; a submit button with its own `hx-post` inside a form does not submit the form.
- uvicorn's access log prints each request line with its query string (`/search?q=…` already does, P2b): the labelling search is therefore a POST, so question words never reach a URL.
- PostgreSQL `percentile_cont(0.95)` interpolates: the p95 of 120 s and 240 s is 234 s. `docker inspect` start times carry nanoseconds; Python's `datetime.fromisoformat` reads them (to microseconds) and the parser compares them as strings only.
- The api and the worker mount the same data dir (`${LOOPI_DATA_DIR:-/srv/loopi}:/srv/loopi`) as the same user (`LOOPI_UID:LOOPI_GID`): the api writes golden copies, the worker's runs read them; no new volume.
- The P2a CPU window (2026-10-16 04:28 → 2026-10-17 04:28 KST) sampled the `worker` and `models` cgroups into `~/loopi-backups/p2a-cpu-samples.txt` as `<service> <UTC time> usage_usec <n> started=<container start>` plus `<service> memory.peak <bytes>` lines (script `scratchpad/p2a-sample-24h.sh`); that window predates chapters, recaps and evaluation runs.
- What motivates the rules (P2a's live findings, Task 11): most English segments are Whisper's stock inventions on normalised room noise — the lone "Thank you." / "Yeah." class, dozens a night — with avg_logprob ≈ −0.8…−0.9 and no_speech_prob 0.00 (VAD-cut regions), so the P2a filter keeps them; and the phrase-list gaps recorded in the committed P2a ledger (a newscast sign-off, a channel-subscribe request worded differently).

## Global Constraints

- The spec is binding (§9.2, §9.5, §10.4, §10.6, §13, §14 P2, D19, D20); D21 is the owner's decision of 2026-10-16: "golden-set data stays OUT of git … the repo gets only result NUMBERS (WER, CER, scores, recall)".
- No golden-set text — reference transcripts, transcripts of golden clips, captions, questions — ever enters git, a plan, a commit message, a log line, an error message or the acceptance report; logs carry ids and counts. Any example text in the repository needs the owner's explicit approval.
- `LOOPI_EVAL_DIR` = `/srv/loopi/eval` in the containers (`/home/soul/loopi-data/eval` on the host); retention never deletes it; "Quên khoảng thời gian" deletes the samples of its range with their copies (§9.5, §11).
- The live ASR behaviour is unchanged by default: `LOOPI_ASR_RULES` empty, VAD threshold 0.5, gain ceiling 30 dB (D19). A default changes only in a controller task, with before/after numbers from the owner's golden set and the owner's yes.
- Evaluation never runs ahead of live work (§14 P2 latency): one golden clip per job, queue `light`, job lock `asr`, priority −20 (below chapter and recap jobs at −10).
- Golden-set targets (§13): 30 keyframes, 20 clips vi/ko/en, 25 questions. Acceptance (§14 P2): average latency ≤ 15 min for recordings between 08:00 and 22:00; a recap every evening; a WER/CER baseline for vi/ko/en with WER > 30 % → change the model or the configuration; recall@5 ≥ 70 %; CPU time per day measured. A language the household rarely speaks is reported with its real number of clips, never padded.
- Python ≥ 3.13, uv-locked; no new dependency (`brain/pyproject.toml` and `brain/uv.lock` unchanged); WER/CER/Levenshtein in-house.
- UI text Vietnamese; every web POST needs `HX-Request: true`; text written by a model or by the owner is always HTML-escaped.
- TDD, coverage ≥ 80 % for `loopi_brain`, conventional commits ending with the session's attribution lines, never commit secrets (`deploy/.env`, API keys, PKI material), never prune or delete Docker resources you did not create (testcontainers' own containers are fine).
- The P1 + P2a + P2b suite stays green (696 passed, 1 skipped at `80a88b8`); the earlier tests this plan deliberately changes are listed in ruling R29. Run everything from `brain/`: `cd brain && uv run pytest`.
- The device contract `docs/protocol/v1.openapi.yaml` is not modified; `PUT /v1/items` behaves exactly as before.
- **Tasks 1–10 never run `docker compose …`, `deploy/smoke.sh`, `os/build.sh` or any Docker command except testcontainers**, and never touch `/home/soul/loopi`, `/home/soul/loopi-p2`, `/home/soul/loopi-p2b` or `/home/soul/loopi-data`: the worktree's `deploy/` also resolves to compose project `deploy`, the owner's live stack. Only Tasks 11–14 — the controller, after the owner's yes — touch it.

## Review Focus

1. **A recording expires or is forgotten while the owner labels it** (the page stays open for hours; the 03:00 retention once it is on; "Quên khoảng thời gian" in another tab) → saving says "Không lưu được", no half-made sample and no orphan copy stays; a sample whose range is forgotten later goes with its copies; a sweep that fails stops neither the forget nor retention. Tests: Task 4 (`test_a_sample_forgotten_while_its_copy_is_written_leaves_no_copy`, `test_a_recording_whose_file_is_missing_leaves_no_sample`, `test_forgetting_a_range_takes_its_golden_samples_and_their_copies`, `test_a_failing_sweep_stops_neither_a_forget_nor_retention`), Task 7 (`test_a_clip_that_cannot_be_saved_says_why`).
2. **A household language that barely occurs** (English is mostly Whisper's inventions at home; Korean may be rare in some weeks) → the page says how many recordings are left to choose from, and the acceptance report prints the real count ("no clip"), never an extrapolation. Tests: Task 7 (`test_a_language_the_home_rarely_speaks_is_reported_not_faked`), Task 9 (`test_the_report_holds_the_numbers_and_never_the_text`, the `| en | 0 | — | — | no clip |` row), Task 6 (`test_a_language_above_30_percent_is_flagged`).
3. **A day reprocessed after it was labelled** (a new caption model, re-transcription after a rule change) → the questions' correct moments are still recognised by their source, golden keyframes ask for a score of the new caption, golden clips are untouched (they are copies). Tests: Task 5 (`test_an_answer_is_recognised_after_its_day_is_reprocessed`, `test_a_correct_moment_keeps_what_it_came_from`), Task 4 (`test_a_scored_keyframe_is_copied_and_scored_again_for_a_new_caption_model`).
4. **An evaluation run while the house is recording, or a worker restart during a run** → a waiting live transcription always starts before the next evaluation job and Whisper is held for one clip at a time; a run whose job vanished does not block the next one; an unreadable clip is counted, not fatal; with the embedding model down, recall is "not measured", not a words-only number. Tests: Task 6 (`test_a_waiting_evaluation_never_goes_ahead_of_a_live_transcription`, `test_one_run_at_a_time_and_a_run_whose_job_is_gone_does_not_block_the_next`, `test_a_clip_whisper_cannot_read_is_counted_and_the_run_goes_on`, `test_recall_waits_for_the_embedding_model`).
5. **Golden-set text leaking out** (logs, error messages, the CLI, the acceptance report, URLs in access logs) → numbers only everywhere; the labelling search is a POST. Tests: Task 4 (`test_a_recording_whose_file_is_missing_leaves_no_sample`), Task 6 (`test_one_transcription_scores_every_rule_set`: log and report free of the clips' text), Task 8 (`test_the_owner_writes_a_question_and_picks_its_correct_moments`: the labelling search is a POST and refuses a request without `HX-Request`), Task 9 (`test_the_report_holds_the_numbers_and_never_the_text`).

---

## File Structure

```
docs/superpowers/specs/2026-10-14-loopi-design.md   # Task 1: D21 (§3, Sửa đổi, §10.6, §13, §17)
brain/src/loopi_brain/
├── migrations/005_evaluation.sql      # Task 1: transcript_segment.lang_prob; eval_audio, eval_caption,
│                                      #         eval_caption_score, eval_question, eval_answer, eval_run, eval_run_audio
├── config.py                          # Task 1: eval_dir; Task 3: ASR_RULES, asr_vad_threshold, asr_rules
├── evaluation/
│   ├── __init__.py                    # Task 2: what lives where (D21)
│   ├── metrics.py                     # Task 2: normalisation, Levenshtein, WER, CER, recall@k, hit@k, MRR
│   ├── golden.py                      # Task 4: proposals, copies, labels, scores, delete, sweep, forget rows
│   ├── questions.py                   # Task 5: questions, correct moments, labelling search, matching, ranks
│   └── runner.py                      # Task 6: runs (job chain), rule sets, caption and recall numbers, format_run
├── pipeline/asr.py                    # Task 3: VAD threshold, region language probability, switchable rules
├── pipeline/jobs.py, pipeline/status.py   # Task 6: run_eval task (registered, labelled)
├── pipeline/retention.py              # Task 4: the 03:00 tick sweeps golden copies (on or off)
├── forget.py                          # Task 4: counts and deletes the range's golden samples, then sweeps
├── days.py                            # Task 5: range_utc (moved from web/search_routes.py)
├── search.py                          # Task 5: search_moments(candidates=…)
├── acceptance.py                      # Task 9: the §14 P2 numbers as Markdown
├── cli.py                             # Task 6: `eval run` / `eval show`; Task 9: `acceptance`
├── app.py                             # Task 7: the eval router
└── web/
    ├── eval_routes.py                 # Task 7: /eval, clips, keyframes, golden files; Task 8: questions, runs
    ├── search_routes.py               # Task 5: range_utc from days.py
    └── templates/                     # Task 7: eval, eval_audio, eval_captions, _eval_clip, _eval_picture,
                                       #   _eval_caption, _eval_score; base (menu); _forget_preview/_forgotten (Task 4)
                                       # Task 8: eval_questions, eval_question, _eval_answers, _eval_hits, _eval_runs
brain/tests/  conftest.py, helpers.py (shared fakes and inserts, edited) and
              test_eval_{metrics,golden,questions,runner,page}.py, test_acceptance.py (new);
              test_{db,config,asr,worker,days,deploy}.py (edited)
deploy/docker-compose.yml, deploy/README.md          # Task 10 (LOOPI_EVAL_DIR, the P2c section)
docs/measurements/2026-10-p2-acceptance.md           # Task 14 (controller; numbers only)
docs/HANDOFF.md, docs/superpowers/ledgers/p2c-progress.md   # Task 14 (controller)
```

---
### Task 1: Spec D21, the golden-set schema (migration 005) and `LOOPI_EVAL_DIR`

**Files:**
- Modify: `docs/superpowers/specs/2026-10-14-loopi-design.md` (D21 in §3, "Sửa đổi", §10.6, §13, §17)
- Create: `brain/src/loopi_brain/migrations/005_evaluation.sql`
- Modify: `brain/src/loopi_brain/config.py` (`eval_dir`)
- Modify: `brain/tests/conftest.py` (truncation list, `settings.eval_dir`), `brain/tests/test_db.py`, `brain/tests/test_config.py`

**Interfaces:**
- Consumes: P2b `db.migrate()` (applies `migrations/*.sql` in name order, then the Procrastinate schema; a migration must not reference a Procrastinate table), tables `item`, `keyframe`, `transcript_segment`.
- Produces:
  - `Settings.eval_dir: Path` (default `/srv/loopi/eval`, env `LOOPI_EVAL_DIR`); the test `settings` fixture sets it to `tmp_path / "eval"`.
  - Column `transcript_segment.lang_prob REAL` (NULL for P2a/P2b rows).
  - Tables (columns exactly as in `005_evaluation.sql` below): `eval_audio` (`id`, `item_id` UNIQUE → `item` ON DELETE CASCADE, `path`, `lang` vi|ko|en, `no_speech`, `reference`; check: no speech ⇔ no language and an empty reference), `eval_caption` (`id`, `keyframe_id` UNIQUE → `keyframe` CASCADE, `path`), `eval_caption_score` (PK `sample_id` + `model_version`, `caption` jsonb, `score` 1–5, `wrong_people`, `invented_objects`, `wrong_activity`), `eval_question` (`id`, `text` non-blank, `first_day` ≤ `last_day`), `eval_answer` (PK `question_id` + `moment_id`, `kind`, `item_id` → `item` CASCADE, `keyframe_id` → `keyframe` CASCADE, `ts_start`, `ts_end`), `eval_run` (`id`, `status` waiting|running|done|failed, `config` jsonb, `results` jsonb, `error`, times), `eval_run_audio` (PK `run_id` + `sample_id`, `segments` jsonb, `error`).
  - `conftest.APP_TABLES` truncates the new tables between tests.

- [ ] **Step 1: Write the failing tests**

Apply to `brain/tests/test_db.py`, `brain/tests/conftest.py` and `brain/tests/test_config.py` (the migration lists gain `005_evaluation.sql`; a new test pins the evaluation tables' rules, including that a forgotten recording takes its golden clip and that clip's run results, while the run's numbers stay):

```diff
diff --git a/brain/tests/conftest.py b/brain/tests/conftest.py
--- a/brain/tests/conftest.py
+++ b/brain/tests/conftest.py
@@ -8,7 +8,8 @@ from loopi_brain.db import make_pool, migrate
 
 PG_IMAGE = "pgvector/pgvector:pg17"
 APP_TABLES = (
-    "commitment, chapter, day_recap, setting, forget_log, day_job_error,"
+    "eval_run_audio, eval_run, eval_answer, eval_question, eval_caption_score, eval_caption, eval_audio,"
+    " commitment, chapter, day_recap, setting, forget_log, day_job_error,"
     " moment, transcript_segment, keyframe, device_command, item, pair_token, device,"
     " procrastinate_events, procrastinate_periodic_defers, procrastinate_jobs, procrastinate_workers"
 )
@@ -46,6 +47,7 @@ def settings(database_url, tmp_path):
         database_url=database_url,
         media_root=tmp_path / "media",
         pki_dir=tmp_path / "pki",
+        eval_dir=tmp_path / "eval",
         min_free_bytes=0,
     )
 
diff --git a/brain/tests/test_config.py b/brain/tests/test_config.py
--- a/brain/tests/test_config.py
+++ b/brain/tests/test_config.py
@@ -18,6 +18,14 @@ def test_pipeline_defaults():
     assert s.asr_max_gain_db == 30.0
 
 
+def test_the_golden_set_lives_in_the_data_dir_outside_git(monkeypatch):
+    # Spec D21: the owner's clips, keyframe copies and references stay in the brain's data dir, never in the repo.
+    assert Settings(database_url="postgresql://x").eval_dir == Path("/srv/loopi/eval")
+    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
+    monkeypatch.setenv("LOOPI_EVAL_DIR", "/data/eval")
+    assert Settings().eval_dir == Path("/data/eval")
+
+
 def test_pipeline_settings_from_env(monkeypatch):
     monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
     monkeypatch.setenv("LOOPI_ASR_LANGUAGES", " ko , en ,")
diff --git a/brain/tests/test_db.py b/brain/tests/test_db.py
--- a/brain/tests/test_db.py
+++ b/brain/tests/test_db.py
@@ -12,6 +12,8 @@ EXPECTED_TABLES = {
     "device", "pair_token", "item", "device_command", "schema_migration",
     "keyframe", "transcript_segment", "moment",
     "chapter", "day_recap", "commitment", "setting", "forget_log", "day_job_error",
+    "eval_audio", "eval_caption", "eval_caption_score", "eval_question", "eval_answer", "eval_run",
+    "eval_run_audio",
     "procrastinate_jobs", "procrastinate_events", "procrastinate_workers", "procrastinate_periodic_defers",
 }
 
@@ -25,7 +27,8 @@ def test_migrate_is_idempotent_and_creates_schema(database_url):
         ).fetchall()
         names = {r[0] for r in conn.execute("SELECT name FROM schema_migration")}
     assert EXPECTED_TABLES <= {r[0] for r in rows}
-    applied = {"001_init.sql", "002_pipeline.sql", "003_understanding.sql", "004_retention_files.sql"}
+    applied = {"001_init.sql", "002_pipeline.sql", "003_understanding.sql", "004_retention_files.sql",
+               "005_evaluation.sql"}
     assert applied | {PROCRASTINATE_SCHEMA} <= names
 
 
@@ -62,7 +65,8 @@ def test_p1_rows_become_pending_after_upgrade(database_url):
                 " VALUES ('I1', 'D1', 'audio', now(), now(), 'x', 1, 'raw/x.opus', 'ready')"
             )
         assert migrate(url) == [
-            "002_pipeline.sql", "003_understanding.sql", "004_retention_files.sql", PROCRASTINATE_SCHEMA
+            "002_pipeline.sql", "003_understanding.sql", "004_retention_files.sql", "005_evaluation.sql",
+            PROCRASTINATE_SCHEMA,
         ]
         with psycopg.connect(url) as conn:
             state = conn.execute("SELECT analysis_state, analysis_job_id FROM item WHERE id = 'I1'").fetchone()
@@ -88,7 +92,8 @@ def test_concurrent_migrations_apply_each_step_once(database_url):
     assert errors == []
     assert sorted(results, key=len) == [
         [],
-        ["001_init.sql", "002_pipeline.sql", "003_understanding.sql", "004_retention_files.sql", PROCRASTINATE_SCHEMA],
+        ["001_init.sql", "002_pipeline.sql", "003_understanding.sql", "004_retention_files.sql", "005_evaluation.sql",
+         PROCRASTINATE_SCHEMA],
     ]
 
 
@@ -104,7 +109,10 @@ def test_cascading_foreign_keys_are_indexed(database_url):
         }
     assert {("keyframe", "item_id"), ("transcript_segment", "item_id"), ("moment", "item_id"),
             ("moment", "keyframe_id"), ("moment", "transcript_segment_id"),
-            ("commitment", "source_moment_id")} <= indexed
+            ("commitment", "source_moment_id"),
+            ("eval_audio", "item_id"), ("eval_caption", "keyframe_id"), ("eval_caption_score", "sample_id"),
+            ("eval_answer", "question_id"), ("eval_answer", "item_id"), ("eval_answer", "keyframe_id"),
+            ("eval_run_audio", "run_id"), ("eval_run_audio", "sample_id")} <= indexed
 
 
 async def test_pool_connections_read_and_write_vectors(pool, pki_settings):
@@ -148,3 +156,42 @@ async def test_understanding_tables_keep_their_rules(pool, pki_settings):
             await conn.execute(
                 "INSERT INTO forget_log (id, range_start, range_end, counts) VALUES ('F1', now(), now(), '{}')"
             )
+
+
+
+async def test_evaluation_tables_keep_their_rules(pool, pki_settings):
+    dev = await paired_device(pool, pki_settings)
+    async with pool.connection() as conn:
+        await conn.execute(
+            "INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes, raw_path, status)"
+            " VALUES ('I1', %s, 'audio', now(), now(), 'x', 1, 'raw/x.opus', 'ready')",
+            (dev["device_id"],),
+        )
+        await conn.execute(
+            "INSERT INTO eval_audio (id, item_id, path, lang, reference) VALUES ('A1', 'I1', 'audio/A1.opus', 'vi', 'ừ')"
+        )
+        await conn.execute("INSERT INTO eval_run (id, config) VALUES ('R1', '{}')")
+        await conn.execute("INSERT INTO eval_run_audio (run_id, sample_id, segments) VALUES ('R1', 'A1', '[]')")
+        bad = [
+            (psycopg.errors.UniqueViolation,  # one golden clip per recording
+             "INSERT INTO eval_audio (id, item_id, path, lang, reference) VALUES ('A2', 'I1', 'audio/A2.opus', 'vi', 'x')"),
+            (psycopg.errors.CheckViolation, "UPDATE eval_audio SET lang = 'ja'"),  # vi, ko or en
+            (psycopg.errors.CheckViolation, "UPDATE eval_audio SET reference = ' '"),  # speech needs its reference
+            # "không có lời nói" has no language and no reference
+            (psycopg.errors.CheckViolation, "UPDATE eval_audio SET no_speech = true"),
+            (psycopg.errors.CheckViolation,
+             "INSERT INTO eval_question (id, text, first_day, last_day) VALUES ('Q1', 'Hôm qua?', '2026-10-14',"
+             " '2026-10-13')"),
+            (psycopg.errors.CheckViolation, "INSERT INTO eval_question (id, text) VALUES ('Q2', '  ')"),
+            (psycopg.errors.CheckViolation, "UPDATE eval_run SET status = 'later'"),
+        ]
+        for error, sql in bad:
+            with pytest.raises(error):
+                async with conn.transaction():
+                    await conn.execute(sql)
+        await conn.execute("UPDATE eval_audio SET no_speech = true, lang = NULL, reference = ''")
+    async with pool.connection() as conn:
+        await conn.execute("DELETE FROM item WHERE id = 'I1'")  # forgotten: the golden clip and its results go too
+        assert await (await conn.execute("SELECT id FROM eval_audio")).fetchall() == []
+        assert await (await conn.execute("SELECT run_id FROM eval_run_audio")).fetchall() == []
+        assert await (await conn.execute("SELECT id FROM eval_run")).fetchall() == [{"id": "R1"}]  # numbers stay
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_config.py tests/test_db.py -q`
Expected: FAIL — `5 failed, 7 passed, 3 errors`: `AttributeError: 'Settings' object has no attribute 'eval_dir'`, the migration lists and table sets lack the 005 objects, and the truncation errors with `psycopg.errors.UndefinedTable: relation "eval_run_audio" does not exist`.

- [ ] **Step 3: Add D21 to the spec**

Run from the repository root (the spec is Vietnamese; each replaced line must occur exactly once):

```bash
python3 - <<'PY'
from pathlib import Path

p = Path("docs/superpowers/specs/2026-10-14-loopi-design.md")
s = p.read_text()


def swap(old: str, new: str) -> None:
    global s
    assert s.count(old) == 1, old
    s = s.replace(old, new)


swap(
    "số ngày chỉnh được trong trang Cài đặt (D20)\n",
    "số ngày chỉnh được trong trang Cài đặt (D20); 2026-10-16: dữ liệu của bộ đánh giá (golden set) nằm ngoài git,"
    " trong thư mục dữ liệu và database của brain, người dùng tự gán nhãn trên trang Đánh giá, repo chỉ giữ code và"
    " các con số kết quả (D21)\n",
)
d20_end = "keyframe, transcript, caption, chương và recap vẫn giữ vĩnh viễn |\n"
swap(
    d20_end,
    d20_end
    + "| D21 | Bộ đánh giá chất lượng AI | Dữ liệu của bộ đánh giá (golden set) không vào git: các đoạn audio thật, bản"
    " sao keyframe, transcript chuẩn và câu hỏi nằm trong thư mục dữ liệu của brain (`/srv/loopi/eval` trong"
    " container, đặt bằng `LOOPI_EVAL_DIR`) và trong database của brain. Repo chỉ chứa code đánh giá"
    " (`brain/src/loopi_brain/evaluation/`) và các con số kết quả (WER, CER, điểm caption, recall), không chứa"
    " transcript hay câu hỏi nào. Người dùng tự gán nhãn trên trang \"Đánh giá\" của brain: nghe từng đoạn audio đã"
    " chuẩn hoá mức âm thanh rồi sửa transcript thành bản chuẩn (hoặc đánh dấu \"không có lời nói\"), chấm điểm"
    " caption của keyframe, viết câu hỏi và chọn moment đúng. Job lưu trữ không xoá bản sao trong bộ đánh giá;"
    " \"Quên khoảng thời gian\" xoá cả các mẫu đánh giá lấy từ khoảng đó |\n",
)
swap(
    "| Cài đặt | Provider, chính sách lưu trữ, \"Quên khoảng thời gian\", token MCP, job lỗi và nút xử lý lại |\n",
    "| Cài đặt | Provider, chính sách lưu trữ, \"Quên khoảng thời gian\", token MCP, job lỗi và nút xử lý lại |\n"
    "| Đánh giá | Gán nhãn bộ đánh giá (audio, caption, câu hỏi), chạy đánh giá và xem kết quả các lần chạy (D21) |\n",
)
swap(
    "- **Đánh giá chất lượng AI (golden set, lưu ở `brain/eval/`):**\n",
    "- **Đánh giá chất lượng AI (golden set do người dùng gán nhãn trên trang Đánh giá; dữ liệu nằm ngoài git, D21):**\n",
)
swap(
    "  - 25 câu hỏi kèm moment đúng (recall@5)\n",
    "  - 25 câu hỏi kèm moment đúng (recall@5)\n"
    "  - Chạy bằng `loopi-brain eval run` hoặc nút trên trang Đánh giá: nhận dạng lại từng đoạn audio bằng đường ASR"
    " hiện tại (chuẩn hoá mức âm thanh, VAD, Whisper, bộ lọc), tính WER/CER theo ngôn ngữ, tỉ lệ bịa câu trên các đoạn"
    " không có lời nói, điểm caption theo model và recall@5 qua tìm kiếm hybrid. Mỗi lần chạy lưu cấu hình và các"
    " con số; chỉ các con số được đưa vào repo\n",
)
swap(
    "├── brain/            # Python (uv): api/, worker/, pipeline/, web/, mcp/, eval/\n",
    "├── brain/            # Python (uv): api/, worker/, pipeline/, web/, mcp/, evaluation/ (code; dữ liệu đánh giá ngoài git, D21)\n",
)
p.write_text(s)
PY
git diff --stat -- docs/superpowers/specs/2026-10-14-loopi-design.md
```

Expected: `1 file changed, 6 insertions(+), 3 deletions(-)` (the "Sửa đổi" line, the D21 row, the Đánh giá page row in §10.6, the §13 heading and its new line about runs, §17).

- [ ] **Step 4: Write the migration**

Create `brain/src/loopi_brain/migrations/005_evaluation.sql`:

```sql
-- P2c: the owner's golden sets and the evaluation runs (spec §13, D21), and Whisper's per-region language
-- probability. Golden-set media are copies under LOOPI_EVAL_DIR (paths here are relative to it), never in git;
-- retention never touches them. "Quên khoảng thời gian" deletes the samples of its range (and the cascades below
-- are the backstop): a sample never outlives the recording it was taken from.

-- The probability Whisper gave the language it chose for the segment's speech region (NULL before P2c).
ALTER TABLE transcript_segment ADD COLUMN lang_prob REAL;

-- A recording the owner listened to and wrote down by hand (the reference transcript), or marked as having no
-- speech at all (silence, TV, noise: the hallucination check). `path`: a byte copy of the raw recording.
CREATE TABLE eval_audio (
    id          TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL UNIQUE REFERENCES item(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    lang        TEXT CHECK (lang IN ('vi', 'ko', 'en')),
    no_speech   BOOLEAN NOT NULL DEFAULT FALSE,
    reference   TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT eval_audio_label CHECK (
        CASE WHEN no_speech THEN lang IS NULL AND reference = ''
             ELSE lang IS NOT NULL AND btrim(reference) <> '' END
    )
);

-- A keyframe the owner scores; `path`: a copy of its JPEG.
CREATE TABLE eval_caption (
    id           TEXT PRIMARY KEY,
    keyframe_id  TEXT NOT NULL UNIQUE REFERENCES keyframe(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The owner's score of one caption of a sample, per caption model: a new model's caption is scored again.
CREATE TABLE eval_caption_score (
    sample_id         TEXT NOT NULL REFERENCES eval_caption(id) ON DELETE CASCADE,
    model_version     TEXT NOT NULL,
    caption           JSONB NOT NULL,              -- the caption that was scored
    score             SMALLINT NOT NULL CHECK (score BETWEEN 1 AND 5),
    wrong_people      BOOLEAN NOT NULL DEFAULT FALSE,   -- "sai số người"
    invented_objects  BOOLEAN NOT NULL DEFAULT FALSE,   -- "bịa đồ vật"
    wrong_activity    BOOLEAN NOT NULL DEFAULT FALSE,   -- "sai hoạt động"
    scored_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sample_id, model_version)
);

-- A question the owner asks of the memories, with the local days it is about (optional, as the search page's
-- filter: "hôm qua" in a question means the day it was written about, not the day it is evaluated).
CREATE TABLE eval_question (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL CHECK (btrim(text) <> ''),
    first_day   DATE,
    last_day    DATE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT eval_question_days CHECK (first_day IS NULL OR last_day IS NULL OR first_day <= last_day)
);

-- A correct moment of a question. Moment ids change when a day is reprocessed (new captions, new transcripts), so
-- the answer also keeps what the moment came from: its keyframe (pictures), its recording and time (speech), its
-- marker item; the runner matches on those.
CREATE TABLE eval_answer (
    question_id  TEXT NOT NULL REFERENCES eval_question(id) ON DELETE CASCADE,
    moment_id    TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('visual', 'speech', 'marker')),
    item_id      TEXT REFERENCES item(id) ON DELETE CASCADE,
    keyframe_id  TEXT REFERENCES keyframe(id) ON DELETE CASCADE,
    ts_start     TIMESTAMPTZ NOT NULL,
    ts_end       TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (question_id, moment_id)
);
CREATE INDEX eval_answer_item ON eval_answer (item_id);
CREATE INDEX eval_answer_keyframe ON eval_answer (keyframe_id);

-- One evaluation run: the configuration it ran with (ASR settings and rules, model versions, search thresholds)
-- and its numbers (`results`: numbers only, never text).
CREATE TABLE eval_run (
    id            TEXT PRIMARY KEY,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'running', 'done', 'failed')),
    config        JSONB NOT NULL,
    results       JSONB,
    error         TEXT
);

-- What the current ASR path heard in one golden clip during a run: every Whisper segment with its region, language
-- and probability, before any filter (the rule sets are applied afterwards), or the error.
CREATE TABLE eval_run_audio (
    run_id     TEXT NOT NULL REFERENCES eval_run(id) ON DELETE CASCADE,
    sample_id  TEXT NOT NULL REFERENCES eval_audio(id) ON DELETE CASCADE,
    segments   JSONB,
    error      TEXT,
    PRIMARY KEY (run_id, sample_id)
);
CREATE INDEX eval_run_audio_sample ON eval_run_audio (sample_id);
```

It references no Procrastinate table (P2a deferred minor: `db.migrate()` applies the Procrastinate schema after every `*.sql`).

- [ ] **Step 5: Add the setting**

```diff
diff --git a/brain/src/loopi_brain/config.py b/brain/src/loopi_brain/config.py
--- a/brain/src/loopi_brain/config.py
+++ b/brain/src/loopi_brain/config.py
@@ -49,6 +49,9 @@ class Settings(BaseSettings):
     # text task to "anthropic". Read from ANTHROPIC_API_KEY (no LOOPI_ prefix); never logged or shown.
     anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
     anthropic_url: str = "https://api.anthropic.com"
+    # Evaluation (P2c, spec D21): the owner's golden set — copies of the chosen recordings and keyframes — lives
+    # here, in the brain's data dir and never in git; retention never deletes it.
+    eval_dir: Path = Path("/srv/loopi/eval")
 
     @field_validator("anthropic_api_key", mode="before")
     @classmethod
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_config.py tests/test_db.py -q`
Expected: `15 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `698 passed, 1 skipped`, `TOTAL … 99%`.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-10-14-loopi-design.md brain/src/loopi_brain/migrations/005_evaluation.sql \
  brain/src/loopi_brain/config.py brain/tests/conftest.py brain/tests/test_db.py brain/tests/test_config.py
git commit -F - <<'EOF'
feat(brain): golden-set schema (migration 005), LOOPI_EVAL_DIR and spec D21

The owner's golden set stays out of git (D21, owner decision 2026-10-16): copies under LOOPI_EVAL_DIR, labels in
the brain's database; the repo gets only numbers.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 2: WER, CER, recall@k and MRR

**Files:**
- Create: `brain/src/loopi_brain/evaluation/__init__.py`, `brain/src/loopi_brain/evaluation/metrics.py`
- Test: `brain/tests/test_eval_metrics.py` (new)

**Interfaces:**
- Consumes: nothing (pure functions).
- Produces (`loopi_brain.evaluation.metrics`): `normalise(text) -> str` (NFC, lower case, every punctuation mark and symbol a space, whitespace collapsed); `words(text) -> list[str]`; `chars(text) -> list[str]` (no spaces); `Errors(substitutions=0, deletions=0, insertions=0, reference=0)` frozen dataclass with `.edits`, `.rate -> float | None` (None without reference tokens) and `+`; `align(reference, hypothesis) -> Errors`; `wer(reference, hypothesis) -> Errors` (words); `cer(reference, hypothesis) -> Errors` (characters without spaces); `recall_at(ranks, relevant, k) -> float` (ValueError when `relevant` ≤ 0); `hit_at(ranks, k) -> float`; `reciprocal_rank(ranks) -> float`. `ranks` are the 1-based ranks at which correct moments were found.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_eval_metrics.py`:

```python
import unicodedata

import pytest

from loopi_brain.evaluation import metrics
from loopi_brain.evaluation.metrics import Errors


def test_text_is_compared_without_case_punctuation_or_unicode_form():
    assert metrics.normalise("Mai, mình đi CHỢ nhé!") == "mai mình đi chợ nhé"
    decomposed = unicodedata.normalize("NFD", "Mai mình đi chợ")
    assert decomposed != "Mai mình đi chợ" and metrics.words(decomposed) == ["mai", "mình", "đi", "chợ"]
    assert metrics.normalise("  Thank you.  ") == metrics.normalise("thank you") == "thank you"
    assert metrics.normalise("내일 회의는 열 시예요.") == "내일 회의는 열 시예요"
    assert metrics.normalise("9:30 — “ok” ...") == "9 30 ok"  # symbols and punctuation are spaces
    assert metrics.words("...") == [] and metrics.chars(" - ") == []


def test_word_errors_count_substitutions_deletions_and_insertions():
    assert metrics.wer("a b c d", "a x c d e") == Errors(substitutions=1, deletions=0, insertions=1, reference=4)
    assert metrics.wer("a b c d", "a x c d e").rate == 0.5
    assert metrics.wer("mai mình đi chợ mua rau", "Mai mình đi chợ mua rao.").rate == pytest.approx(1 / 6)
    assert metrics.wer("một hai ba", "") == Errors(0, 3, 0, 3)  # nothing heard: every word deleted
    assert metrics.wer("", "thank you").rate is None  # no reference words: no rate (the no-speech check counts these)
    assert metrics.wer("", "thank you") == Errors(0, 0, 2, 0)
    assert metrics.wer("đi chợ", "đi chợ") == Errors(0, 0, 0, 2) and metrics.wer("đi chợ", "đi chợ").rate == 0.0


def test_korean_is_scored_by_characters_without_spaces():
    # The space mistake ("열 시" heard as "열시") is no error; the dropped particle "는" is one.
    errors = metrics.cer("내일 회의는 열 시예요", "내일 회의 열시예요")
    assert errors == Errors(0, 1, 0, 9) and errors.rate == pytest.approx(1 / 9)


def test_corpus_rates_add_the_errors_of_every_clip():
    total = sum([metrics.wer("a b c d", "a x c d"), metrics.wer("e f", "")], Errors())
    assert total == Errors(1, 2, 0, 6) and total.rate == 0.5  # 3 errors over 6 words, not the mean of 25 % and 100 %


def test_an_alignment_of_long_clips_stays_exact():
    reference = " ".join(f"w{n}" for n in range(400))
    heard = " ".join(f"w{n}" for n in range(400) if n % 10)  # every tenth word missed
    assert metrics.wer(reference, heard) == Errors(0, 40, 0, 400)


def test_recall_hits_and_reciprocal_rank():
    # Two correct moments: one found first, one found at rank 7.
    assert metrics.recall_at([1, 7], relevant=2, k=5) == 0.5
    assert metrics.recall_at([1, 7], relevant=2, k=10) == 1.0
    assert metrics.hit_at([7], k=5) == 0.0 and metrics.hit_at([7], k=10) == 1.0
    assert metrics.reciprocal_rank([7, 3]) == pytest.approx(1 / 3)
    assert metrics.recall_at([], relevant=3, k=5) == 0.0 and metrics.reciprocal_rank([]) == 0.0
    with pytest.raises(ValueError):
        metrics.recall_at([1], relevant=0, k=5)  # a question without a correct moment is not scored
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_eval_metrics.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'loopi_brain.evaluation'` (1 error during collection).

- [ ] **Step 3: Write the package and the metrics**

Create `brain/src/loopi_brain/evaluation/__init__.py`:

```python
"""Evaluation of the AI pipeline against the owner's golden set (spec §13, D21).

The golden set itself — clips, keyframe copies, reference transcripts, questions — lives in the brain's data dir
(``LOOPI_EVAL_DIR``) and database, never in git; this package holds only the code. Logs name samples by id and
carry counts, never their text.
"""
```

Create `brain/src/loopi_brain/evaluation/metrics.py`:

```python
"""The numbers of an evaluation (spec §13): word and character error rates, recall@k and the reciprocal rank.

Text is compared after NFC normalisation, in lower case, with every punctuation mark and symbol read as a space.
Vietnamese and English are scored by words (a Vietnamese word is a syllable: WER is the syllable error rate);
Korean by characters without spaces (CER: Whisper's spacing of Korean is no error). Rates over several clips add
their errors first (corpus WER), so a long clip weighs more than a short one.
"""
from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass


def normalise(text: str) -> str:
    """NFC, lower case, punctuation and symbols as spaces, whitespace collapsed."""
    text = unicodedata.normalize("NFC", text).lower()
    text = "".join(" " if unicodedata.category(c)[0] in "PS" else c for c in text)
    return " ".join(text.split())


def words(text: str) -> list[str]:
    return normalise(text).split()


def chars(text: str) -> list[str]:
    return list(normalise(text).replace(" ", ""))


@dataclass(frozen=True)
class Errors:
    """An alignment of a hypothesis with its reference: the edits and the length of the reference."""

    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0
    reference: int = 0

    @property
    def edits(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def rate(self) -> float | None:
        """Edits per reference token; None without reference tokens."""
        return self.edits / self.reference if self.reference else None

    def __add__(self, other: Errors) -> Errors:
        return Errors(self.substitutions + other.substitutions, self.deletions + other.deletions,
                      self.insertions + other.insertions, self.reference + other.reference)


def align(reference: Sequence[str], hypothesis: Sequence[str]) -> Errors:
    """The Levenshtein alignment (unit costs) of two token lists; of equally cheap ones, the one that prefers a
    match or a substitution over a deletion, and a deletion over an insertion (deterministic)."""
    # Each cell: (cost, substitutions, deletions, insertions); one row at a time.
    previous = [(j, 0, 0, j) for j in range(len(hypothesis) + 1)]
    for i in range(1, len(reference) + 1):
        current = [(i, 0, i, 0)]
        for j in range(1, len(hypothesis) + 1):
            cost, sub, dele, ins = previous[j - 1]
            if reference[i - 1] == hypothesis[j - 1]:
                diagonal = (cost, sub, dele, ins)
            else:
                diagonal = (cost + 1, sub + 1, dele, ins)
            cost, sub, dele, ins = previous[j]
            up = (cost + 1, sub, dele + 1, ins)
            cost, sub, dele, ins = current[j - 1]
            left = (cost + 1, sub, dele, ins + 1)
            current.append(min(diagonal, up, left, key=lambda cell: cell[0]))
        previous = current
    _cost, sub, dele, ins = previous[-1]
    return Errors(sub, dele, ins, len(reference))


def wer(reference: str, hypothesis: str) -> Errors:
    return align(words(reference), words(hypothesis))


def cer(reference: str, hypothesis: str) -> Errors:
    return align(chars(reference), chars(hypothesis))


def recall_at(ranks: Sequence[int], relevant: int, k: int) -> float:
    """The share of a question's ``relevant`` correct moments found in the first ``k`` results; ``ranks`` holds,
    for each correct moment that was found, the rank (from 1) of its result."""
    if relevant <= 0:
        raise ValueError("a question needs at least one correct moment")
    return sum(1 for rank in ranks if rank <= k) / relevant


def hit_at(ranks: Sequence[int], k: int) -> float:
    """1.0 when a correct moment is in the first ``k`` results, else 0.0."""
    return 1.0 if any(rank <= k for rank in ranks) else 0.0


def reciprocal_rank(ranks: Sequence[int]) -> float:
    """1 / the rank of the first correct result; 0.0 when none was found."""
    return 1.0 / min(ranks) if ranks else 0.0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_eval_metrics.py -q`
Expected: `6 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `704 passed, 1 skipped`, `TOTAL … 99%` (`evaluation/metrics.py` 100 %).

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/evaluation/__init__.py brain/src/loopi_brain/evaluation/metrics.py \
  brain/tests/test_eval_metrics.py
git commit -F - <<'EOF'
feat(brain): WER, CER, recall@k and MRR for the evaluation

Written here (no Levenshtein package is locked): Vietnamese and English by words after NFC/lower case/punctuation
normalisation, Korean by characters without spaces; corpus rates add the errors of every clip.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 3: Switchable hallucination rules, a VAD threshold setting and the region's language probability

**Files:**
- Modify: `brain/src/loopi_brain/config.py` (`ASR_RULES`, `asr_vad_threshold`, `asr_rules`, `asr_rule_set`)
- Modify: `brain/src/loopi_brain/pipeline/asr.py`
- Modify: `brain/tests/test_asr.py`, `brain/tests/test_config.py`, `brain/tests/test_worker.py`

**Interfaces:**
- Consumes: P2a `asr.keep_segment`, `asr.RegionResult(lang, lang_prob, segments)`, `asr.SegmentResult(start, end, text, avg_logprob, no_speech_prob, compression_ratio)`, `asr.transcribe_file`; Task 1's `transcript_segment.lang_prob`.
- Produces:
  - `config.ASR_RULES = ("outro", "lone_phrase", "short_foreign")`; `Settings.asr_vad_threshold: float` (default 0.5, 0 < t < 1, env `LOOPI_ASR_VAD_THRESHOLD`); `Settings.asr_rules: str` (default `""`, comma-separated names from `ASR_RULES`, an unknown name is a validation error mentioning "unknown ASR rules"); `Settings.asr_rule_set -> frozenset[str]`.
  - `asr.RULES` (= `ASR_RULES`), `asr.VAD_THRESHOLD = 0.5`, `asr.OUTRO_PATTERNS`, `asr.LONE_PHRASES`, `asr.FOREIGN_LANGUAGES = {"en"}`, `asr.FOREIGN_MAX_PROB = 0.5`, `asr.FOREIGN_MAX_WORDS = 3`, `asr.is_outro(text) -> bool`.
  - `asr.kept_segments(result: RegionResult, rules: frozenset[str] = frozenset()) -> list[SegmentResult]` — `keep_segment` on each segment, then the switched-on rules; with no rule it equals P2a's filter.
  - `asr.speech_regions(audio, threshold=VAD_THRESHOLD)`; `asr.transcribe_file(path, transcriber, max_gain_db=…, vad_threshold=VAD_THRESHOLD)` (it calls `speech_regions(audio, vad_threshold)` positionally — monkeypatched stand-ins take two arguments).
  - The live stage applies `settings.asr_rule_set` and `settings.asr_vad_threshold` and stores `RegionResult.lang_prob` in `transcript_segment.lang_prob`.

- [ ] **Step 1: Write the failing tests**

Apply to `brain/tests/test_asr.py`, `brain/tests/test_config.py` and `brain/tests/test_worker.py` (the P2a `speech_regions` stand-ins take the threshold now; new tests pin each rule on its known inventions, the real speech every rule must keep, the threshold reaching Silero, and the live stage reading its settings and keeping the probability):

```diff
diff --git a/brain/tests/test_asr.py b/brain/tests/test_asr.py
--- a/brain/tests/test_asr.py
+++ b/brain/tests/test_asr.py
@@ -155,7 +155,7 @@ async def _vad_input(pool, settings, device_id, source, monkeypatch, seconds=4):
     path = make_opus(settings.media_root / row["raw_path"], source, seconds)
     seen = []
 
-    def regions(audio):
+    def regions(audio, _threshold):
         seen.append(audio)
         return [Region(0.0, 2.0)]
 
@@ -202,7 +202,8 @@ async def test_room_noise_stays_without_speech_after_normalisation(pool, pki_set
 
 async def test_speech_regions_become_segments_and_moments(pool, pki_settings, dev, jobs_open, monkeypatch):
     item_id = await _audio_item(pool, pki_settings, dev["device_id"])
-    monkeypatch.setattr(asr, "speech_regions", lambda _audio: [Region(0.5, 2.5), Region(3.0, 4.5), Region(4.6, 4.9)])
+    monkeypatch.setattr(asr, "speech_regions",
+                        lambda _audio, _threshold: [Region(0.5, 2.5), Region(3.0, 4.5), Region(4.6, 4.9)])
     fake = FakeTranscriber(
         RegionResult("vi", 0.9, [seg("Mai mình đi chợ", 0.0, 1.0), seg("nhớ mua trứng nhé", 1.0, 2.0)]),
         RegionResult("ko", 0.8, [seg("네 알겠어요", 0.2, 1.4)]),
@@ -234,7 +235,7 @@ async def test_speech_regions_become_segments_and_moments(pool, pki_settings, de
 
 async def test_hallucinated_text_on_noise_leaves_no_transcript(pool, pki_settings, dev, jobs_open, monkeypatch):
     item_id = await _audio_item(pool, pki_settings, dev["device_id"])
-    monkeypatch.setattr(asr, "speech_regions", lambda _audio: [Region(0.0, 4.0)])
+    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _threshold: [Region(0.0, 4.0)])
     tv_noise = RegionResult("vi", 0.5, [
         seg("Hẹn gặp lại các bạn trong những video tiếp theo", 0.0, 2.0),
         seg("Thanks for watching", 2.0, 3.0),
@@ -257,7 +258,7 @@ async def test_real_silence_is_done_without_calling_whisper(pool, pki_settings,
 
 async def test_item_deleted_during_transcription_stores_nothing(pool, pki_settings, dev, jobs_open, monkeypatch):
     item_id = await _audio_item(pool, pki_settings, dev["device_id"])
-    monkeypatch.setattr(asr, "speech_regions", lambda _audio: [Region(0.0, 2.0)])
+    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _threshold: [Region(0.0, 2.0)])
 
     class DeletingTranscriber(FakeTranscriber):
         def transcribe(self, chunk):  # runs in the worker thread, like Whisper
@@ -337,3 +338,77 @@ def test_real_whisper_model_transcribes_a_chunk(tmp_path):
     result = whisper.transcribe(tone)
     assert result.lang in {"vi", "ko", "en"} and 0.0 <= result.lang_prob <= 1.0
     assert all(isinstance(s, SegmentResult) for s in result.segments)
+
+
+@pytest.mark.parametrize(
+    ("rule", "region"),
+    [
+        ("outro", RegionResult("vi", 0.9, [seg("Hãy đăng ký kênh để ủng hộ mình nhé")])),
+        ("outro", RegionResult("vi", 0.9, [seg("Hãy subscribe cho kênh Ghiền Mì Gõ để xem những video mới nhất")])),
+        ("outro", RegionResult("ko", 0.9, [seg("MBC 뉴스 김지경입니다.")])),
+        ("lone_phrase", RegionResult("en", 0.9, [seg("Thank you.")])),
+        ("lone_phrase", RegionResult("en", 0.9, [seg("Yeah."), seg("Mm-hmm.", 2.0, 3.0)])),
+        ("short_foreign", RegionResult("en", 0.3, [seg("Okay, let's go.")])),
+    ],
+)
+def test_a_switched_on_rule_drops_what_whisper_writes_on_noise(rule, region):
+    assert asr.kept_segments(region) == region.segments, "the P2a filters alone keep it"
+    assert asr.kept_segments(region, frozenset({rule})) == []
+    assert asr.kept_segments(region, frozenset(asr.RULES)) == []
+
+
+@pytest.mark.parametrize(
+    "region",
+    [
+        RegionResult("vi", 0.9, [seg("Anh đã đăng ký kênh K+ chưa?")]),
+        RegionResult("vi", 0.9, [seg("Con hãy đăng ký kênh này giúp mẹ")]),  # no video, no "ủng hộ": a request
+        RegionResult("en", 0.9, [seg("Yeah."), seg("I'll call you tomorrow.", 2.0, 4.0)]),  # not alone
+        RegionResult("en", 0.9, [seg("Okay, let's go.")]),  # English, but Whisper is sure of it
+        RegionResult("en", 0.3, [seg("Can you pass me the salt please")]),  # unsure, but a whole sentence
+        RegionResult("vi", 0.2, [seg("Ừ")]),  # a household language
+        RegionResult("ko", 0.9, [seg("MBC 뉴스 보고 있었어요")]),  # talking about the news is no sign-off
+    ],
+)
+def test_every_rule_keeps_real_speech(region):
+    assert asr.kept_segments(region, frozenset(asr.RULES)) == region.segments
+
+
+def test_the_p2a_filters_still_apply_before_the_rules():
+    region = RegionResult("vi", 0.9, [seg("Cảm ơn các bạn đã theo dõi"), seg("Mai đi chợ", 2.0, 3.0)])
+    assert asr.kept_segments(region) == [region.segments[1]]
+
+
+def test_the_vad_threshold_reaches_silero(monkeypatch):
+    import faster_whisper.vad
+
+    seen = []
+    monkeypatch.setattr(faster_whisper.vad, "get_speech_timestamps",
+                        lambda audio, options, rate: seen.append(options) or [])
+    assert asr.speech_regions(np.zeros(SR, dtype=np.float32), 0.35) == []
+    assert asr.speech_regions(np.zeros(SR, dtype=np.float32)) == []
+    assert [o.threshold for o in seen] == [0.35, 0.5]
+    assert (seen[0].min_silence_duration_ms, seen[0].speech_pad_ms) == (500, 200)
+
+
+async def test_the_rules_and_vad_threshold_come_from_settings_and_the_language_probability_is_kept(
+    pool, pki_settings, dev, jobs_open, monkeypatch
+):
+    thresholds = []
+
+    def regions(_audio, threshold):
+        thresholds.append(threshold)
+        return [Region(0.0, 2.0), Region(2.5, 4.5)]
+
+    monkeypatch.setattr(asr, "speech_regions", regions)
+    results = (RegionResult("en", 0.3, [seg("Thank you.")]), RegionResult("vi", 0.8, [seg("Mai mình đi chợ")]))
+    item_id = await _audio_item(pool, pki_settings, dev["device_id"])
+    assert await asr.transcribe_audio(make_ctx(pool, pki_settings, transcriber=FakeTranscriber(*results)), item_id) == 2
+    rows = await fetch_all(pool, "SELECT lang, lang_prob, text FROM transcript_segment ORDER BY start_ts")
+    assert [(r["lang"], r["lang_prob"], r["text"]) for r in rows] == [
+        ("en", pytest.approx(0.3), "Thank you."), ("vi", pytest.approx(0.8), "Mai mình đi chợ")]
+    ruled = pki_settings.model_copy(update={"asr_rules": "lone_phrase,short_foreign", "asr_vad_threshold": 0.4})
+    async with pool.connection() as conn:
+        await conn.execute("UPDATE item SET analysis_state = 'pending' WHERE id = %s", (item_id,))
+    assert await asr.transcribe_audio(make_ctx(pool, ruled, transcriber=FakeTranscriber(*results)), item_id) == 1
+    assert [r["text"] for r in await fetch_all(pool, "SELECT text FROM transcript_segment")] == ["Mai mình đi chợ"]
+    assert thresholds == [0.5, 0.4]
diff --git a/brain/tests/test_config.py b/brain/tests/test_config.py
--- a/brain/tests/test_config.py
+++ b/brain/tests/test_config.py
@@ -78,3 +78,24 @@ def test_text_model_defaults_and_limits(monkeypatch):
     monkeypatch.setenv("LOOPI_LLM_TIMEOUT_S", "10")
     with pytest.raises(pydantic.ValidationError, match="llm_timeout_s"):
         Settings()
+
+
+def test_asr_rules_and_vad_threshold(monkeypatch):
+    import pydantic
+    import pytest
+
+    s = Settings(database_url="postgresql://x")
+    assert (s.asr_rule_set, s.asr_vad_threshold) == (frozenset(), 0.5)  # the P2a behaviour until the numbers say so
+    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
+    monkeypatch.setenv("LOOPI_ASR_RULES", " outro, lone_phrase ,")
+    monkeypatch.setenv("LOOPI_ASR_VAD_THRESHOLD", "0.35")
+    s = Settings()
+    assert s.asr_rule_set == frozenset({"outro", "lone_phrase"}) and s.asr_vad_threshold == 0.35
+    monkeypatch.setenv("LOOPI_ASR_RULES", "outro,typo")
+    with pytest.raises(pydantic.ValidationError, match="unknown ASR rules: typo"):
+        Settings()
+    monkeypatch.setenv("LOOPI_ASR_RULES", "")
+    for bad in ("0", "1", "-0.1"):
+        monkeypatch.setenv("LOOPI_ASR_VAD_THRESHOLD", bad)
+        with pytest.raises(pydantic.ValidationError, match="asr_vad_threshold"):
+            Settings()
diff --git a/brain/tests/test_worker.py b/brain/tests/test_worker.py
--- a/brain/tests/test_worker.py
+++ b/brain/tests/test_worker.py
@@ -53,7 +53,7 @@ async def test_a_real_worker_runs_every_stage(pool, pki_settings, dev, jobs_open
     await _with_file(pool, pki_settings, audio, lambda p: make_opus(p, seconds=5))
     marker = await insert_item(pool, device_id, kind="marker", started_at=T0 + dt.timedelta(minutes=3), seconds=0,
                                meta={"note": "chìa khoá"})
-    monkeypatch.setattr(asr, "speech_regions", lambda _audio: [Region(1.0, 3.0)])
+    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _threshold: [Region(1.0, 3.0)])
     speech = RegionResult("vi", 0.9, [SegmentResult(0.0, 2.0, "Chìa khoá ở trên bàn", -0.2, 0.02, 1.1)])
     ctx = make_ctx(pool, pki_settings, ollama=FakeOllama(), transcriber=FakeTranscriber(speech))
 
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_asr.py tests/test_config.py tests/test_worker.py -q`
Expected: FAIL — `25 failed, 54 passed, 1 skipped`: `AttributeError: module 'loopi_brain.pipeline.asr' has no attribute 'kept_segments'`, the settings lack `asr_rule_set`, and the stand-ins that now take the threshold are called with one argument (`TypeError`).

- [ ] **Step 3: Add the settings**

```diff
diff --git a/brain/src/loopi_brain/config.py b/brain/src/loopi_brain/config.py
--- a/brain/src/loopi_brain/config.py
+++ b/brain/src/loopi_brain/config.py
@@ -5,6 +5,11 @@ from zoneinfo import ZoneInfo
 from pydantic import Field, SecretStr, field_validator
 from pydantic_settings import BaseSettings, SettingsConfigDict
 
+# Hallucination rules the ASR stage can switch on beyond its P2a filters (implemented in pipeline/asr.py). None is
+# on by default: the evaluation runner measures each on the owner's golden set first (spec §13), and a rule becomes a
+# default only when those numbers say so.
+ASR_RULES = ("outro", "lone_phrase", "short_foreign")
+
 
 class Settings(BaseSettings):
     model_config = SettingsConfigDict(env_prefix="LOOPI_")
@@ -32,6 +37,9 @@ class Settings(BaseSettings):
     asr_languages: str = "vi,ko,en"
     whisper_cpu_threads: int = 4
     asr_max_gain_db: float = Field(default=30.0, ge=0.0, le=60.0)  # level gain before VAD/Whisper (spec D19)
+    asr_vad_threshold: float = Field(default=0.5, gt=0.0, lt=1.0)  # Silero's speech probability for a region
+    # Comma-separated names from ASR_RULES that are switched on (none by default).
+    asr_rules: str = ""
     # Understanding (P2b): text models (chapters, recap, chat) leave memory a minute after answering (the host is
     # short of memory: P2a deferred M14), get a context window large enough for their bounded prompts, and may
     # take long on a CPU: a request waits up to llm_timeout_s seconds for the whole answer.
@@ -62,6 +70,14 @@ class Settings(BaseSettings):
             return value.strip() or None
         return value
 
+    @field_validator("asr_rules")
+    @classmethod
+    def _known_rules(cls, value: str) -> str:
+        unknown = sorted({r.strip() for r in value.split(",") if r.strip()} - set(ASR_RULES))
+        if unknown:
+            raise ValueError(f"unknown ASR rules: {', '.join(unknown)} (known: {', '.join(ASR_RULES)})")
+        return value
+
     @property
     def zone(self) -> ZoneInfo:
         return ZoneInfo(self.tz)
@@ -69,3 +85,7 @@ class Settings(BaseSettings):
     @property
     def asr_language_list(self) -> list[str]:
         return [code.strip() for code in self.asr_languages.split(",") if code.strip()]
+
+    @property
+    def asr_rule_set(self) -> frozenset[str]:
+        return frozenset(r.strip() for r in self.asr_rules.split(",") if r.strip())
```

- [ ] **Step 4: Add the rules, the threshold and the probability to the ASR stage**

```diff
diff --git a/brain/src/loopi_brain/pipeline/asr.py b/brain/src/loopi_brain/pipeline/asr.py
--- a/brain/src/loopi_brain/pipeline/asr.py
+++ b/brain/src/loopi_brain/pipeline/asr.py
@@ -18,7 +18,7 @@ import anyio
 import numpy as np
 
 from .. import audio_level, devices
-from ..config import Settings
+from ..config import ASR_RULES, Settings
 from . import dispatch, jobs
 from .context import PipelineContext, get_context
 from .jobs import job_app
@@ -31,6 +31,7 @@ MIN_REGION_S = 0.5
 # Whisper detects one language per region from its start, so regions stay short enough for a
 # Vietnamese–Korean conversation to change language between them.
 MAX_REGION_S = 20.0
+VAD_THRESHOLD = 0.5  # Silero's default speech probability; LOOPI_ASR_VAD_THRESHOLD
 WHISPER_SIZE = "large-v3-turbo"
 WHISPER_COMPUTE = "int8"
 # Whisper invents these on silence, music or TV noise. A segment is dropped only when such phrases
@@ -57,6 +58,24 @@ HALLUCINATION_FRAGMENTS = (
     "hẹn gặp lại",
 )
 HALLUCINATION_COVERAGE = 0.8
+# Rules that can be switched on (LOOPI_ASR_RULES; none by default, spec §13: only on the golden set's numbers).
+# Each one drops more of what Whisper writes on silence, TV or room noise, at the risk of dropping real speech.
+RULES = ASR_RULES
+# "outro": a whole segment that is a video's or a newscast's sign-off, worded in ways the phrase list above does not
+# cover ("Hãy subscribe cho kênh … video …", "Hãy đăng ký kênh để ủng hộ mình nhé", "MBC 뉴스 …입니다"). Matched on
+# the text in lower case with punctuation as spaces.
+OUTRO_PATTERNS = (
+    re.compile(r"\bhãy (?:like (?:và )?)?(?:subscribe|đăng k[ýí]) (?:cho )?kênh\b.*\b(?:video|ủng hộ)\b"),
+    re.compile(r"^mbc 뉴스 \w+입니다$"),
+)
+# "lone_phrase": a speech region whose every segment is one of these (lower case, no spaces or punctuation): what
+# Whisper wrote most often on a quiet home's normalised noise (P2a: "Thank you." 26 times in one night, "Yeah.").
+LONE_PHRASES = frozenset({"thankyou", "thankyouverymuch", "thanks", "yeah", "amen", "mmhmm", "bye", "you"})
+# "short_foreign": a short region in a language the household rarely speaks, chosen with low confidence (on noise
+# Whisper often picks English with avg_logprob ≈ -0.85 and no_speech_prob 0.0, which the P2a filter keeps).
+FOREIGN_LANGUAGES = frozenset({"en"})
+FOREIGN_MAX_PROB = 0.5
+FOREIGN_MAX_WORDS = 3
 
 
 @dataclass(frozen=True)
@@ -91,6 +110,15 @@ def _normalise(text: str) -> str:
     return re.sub(r"[\W_]+", "", text)
 
 
+def _spaced(text: str) -> str:
+    """Lower case, NFC, punctuation as spaces, whitespace collapsed (what the outro patterns read)."""
+    return " ".join(re.sub(r"[\W_]+", " ", unicodedata.normalize("NFC", text).lower()).split())
+
+
+def is_outro(text: str) -> bool:
+    return any(pattern.search(_spaced(text)) for pattern in OUTRO_PATTERNS)
+
+
 _HALLUCINATIONS = tuple(sorted({_normalise(p) for p in HALLUCINATIONS}, key=len, reverse=True))
 _FRAGMENTS = tuple(sorted({_normalise(p) for p in HALLUCINATION_FRAGMENTS}, key=len, reverse=True))
 
@@ -130,6 +158,21 @@ def keep_segment(seg: SegmentResult) -> bool:
     return not is_hallucination(seg.text)
 
 
+def kept_segments(result: RegionResult, rules: frozenset[str] = frozenset()) -> list[SegmentResult]:
+    """The segments of one speech region worth storing: each one that passes ``keep_segment``, then the rules
+    switched on in ``rules`` (names from RULES)."""
+    kept = [s for s in result.segments if keep_segment(s)]
+    if "outro" in rules:
+        kept = [s for s in kept if not is_outro(s.text)]
+    if kept and "lone_phrase" in rules and all(_normalise(s.text) in LONE_PHRASES for s in kept):
+        return []
+    if (kept and "short_foreign" in rules and result.lang in FOREIGN_LANGUAGES
+            and result.lang_prob < FOREIGN_MAX_PROB
+            and sum(len(s.text.split()) for s in kept) <= FOREIGN_MAX_WORDS):
+        return []
+    return kept
+
+
 def _cap(chunks: list[tuple[float, float]]) -> list[Region]:
     """One merged group of speech chunks -> regions of at most MAX_REGION_S: split at the longest
     silence between chunks (of equally long ones, the one nearest the middle) while possible,
@@ -180,11 +223,14 @@ def normalise(audio: np.ndarray, max_gain_db: float = audio_level.MAX_GAIN_DB) -
     return np.clip(audio * np.float32(gain), -1.0, 1.0)
 
 
-def speech_regions(audio: np.ndarray) -> list[Region]:
-    """Where someone speaks, by the Silero VAD model bundled with faster-whisper (no download)."""
+def speech_regions(audio: np.ndarray, threshold: float = VAD_THRESHOLD) -> list[Region]:
+    """Where someone speaks, by the Silero VAD model bundled with faster-whisper (no download); ``threshold`` is the
+    speech probability a frame needs."""
     from faster_whisper.vad import VadOptions, get_speech_timestamps
 
-    raw = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=500, speech_pad_ms=200), SAMPLE_RATE)
+    raw = get_speech_timestamps(
+        audio, VadOptions(threshold=threshold, min_silence_duration_ms=500, speech_pad_ms=200), SAMPLE_RATE
+    )
     return merge_regions(raw)
 
 
@@ -234,13 +280,16 @@ class WhisperTranscriber:
 
 
 def transcribe_file(
-    path: Path, transcriber: Transcriber, max_gain_db: float = audio_level.MAX_GAIN_DB
+    path: Path,
+    transcriber: Transcriber,
+    max_gain_db: float = audio_level.MAX_GAIN_DB,
+    vad_threshold: float = VAD_THRESHOLD,
 ) -> list[tuple[Region, RegionResult]]:
     """Blocking: decode, normalise the level, find speech, transcribe each region. Segment times stay
-    region-relative."""
+    region-relative. The live stage and the evaluation runner both go this way."""
     audio = normalise(load_audio(path), max_gain_db)
     results = []
-    for region in speech_regions(audio):
+    for region in speech_regions(audio, vad_threshold):
         chunk = audio[int(region.start * SAMPLE_RATE): int(region.end * SAMPLE_RATE)]
         results.append((region, transcriber.transcribe(chunk)))
     return results
@@ -259,7 +308,10 @@ async def _transcribe(ctx: PipelineContext, item_id: str) -> int:
     if not path.is_file():
         await finish_item(pool, item_id, "skipped")
         return 0
-    results = await anyio.to_thread.run_sync(transcribe_file, path, ctx.transcriber, ctx.settings.asr_max_gain_db)
+    settings = ctx.settings
+    results = await anyio.to_thread.run_sync(
+        transcribe_file, path, ctx.transcriber, settings.asr_max_gain_db, settings.asr_vad_threshold
+    )
     version = ctx.router.model_version("asr")
     start: dt.datetime = item["started_at"]
     moments = 0
@@ -270,7 +322,7 @@ async def _transcribe(ctx: PipelineContext, item_id: str) -> int:
         # Re-running replaces the item's transcript (moments go with their segments: ON DELETE CASCADE).
         await conn.execute("DELETE FROM transcript_segment WHERE item_id = %s", (item_id,))
         for region, result in results:
-            kept = [s for s in result.segments if keep_segment(s)]
+            kept = kept_segments(result, settings.asr_rule_set)
             if not kept:
                 continue
             segment_ids = []
@@ -279,11 +331,12 @@ async def _transcribe(ctx: PipelineContext, item_id: str) -> int:
                 segment_ids.append(segment_id)
                 await conn.execute(
                     "INSERT INTO transcript_segment (id, item_id, device_id, start_ts, end_ts, lang, text,"
-                    " avg_logprob, no_speech_prob, model_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
+                    " avg_logprob, no_speech_prob, lang_prob, model_version)"
+                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     (segment_id, item_id, item["device_id"],
                      start + dt.timedelta(seconds=region.start + seg.start),
                      start + dt.timedelta(seconds=region.start + seg.end),
-                     result.lang, seg.text.strip(), seg.avg_logprob, seg.no_speech_prob, version),
+                     result.lang, seg.text.strip(), seg.avg_logprob, seg.no_speech_prob, result.lang_prob, version),
                 )
             await conn.execute(
                 "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, lang, item_id,"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_asr.py tests/test_config.py tests/test_worker.py -q`
Expected: `79 passed, 1 skipped`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `721 passed, 1 skipped`, `TOTAL … 99%`.

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/config.py brain/src/loopi_brain/pipeline/asr.py brain/tests/test_asr.py \
  brain/tests/test_config.py brain/tests/test_worker.py
git commit -F - <<'EOF'
feat(brain): switchable ASR hallucination rules, a VAD threshold setting and the region language probability

The rules (outro patterns, a lone stock phrase, a short unsure English region) are off by default: the live
transcription is unchanged until the owner's golden set says otherwise (spec §13).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---
### Task 4: Golden clips and keyframes — proposals, copies, labels, scores; forgetting and retention reach them

**Files:**
- Create: `brain/src/loopi_brain/evaluation/golden.py`
- Modify: `brain/src/loopi_brain/forget.py`, `brain/src/loopi_brain/pipeline/retention.py`
- Modify: `brain/src/loopi_brain/web/templates/_forget_preview.html`, `brain/src/loopi_brain/web/templates/_forgotten.html`
- Modify: `brain/tests/helpers.py` (`analysed_audio`, `insert_segment`, `captioned_keyframe`)
- Test: `brain/tests/test_eval_golden.py` (new)

**Interfaces:**
- Consumes: Task 1's tables and `Settings.eval_dir`; P2b `media.inside(root, rel)`, `media.remove_files(root, rels) -> (deleted, failed)` (root-generic), `media.normalise_audio(src, dst)` (D19 playback copy), `devices.new_id()`, `forget._delete_rows` / `forget._derived` / `forget.forget_range`, `retention.apply_retention_job`.
- Produces (`loopi_brain.evaluation.golden`):
  - Constants `LANGUAGES = ("vi", "ko", "en")`, `NO_SPEECH = "none"`, `AUDIO_TARGET = 20`, `AUDIO_SUGGESTED = {"vi": 6, "ko": 6, "en": 4, "none": 4}`, `CAPTION_TARGET = 30`, `PER_PART = 2`, `DAY_PARTS` (4 Vietnamese labels), `CAPTIONS_SHOWN = 12`, `FLAGS = ("wrong_people", "invented_objects", "wrong_activity")`; `GoldenError`; `AudioLabel(lang, no_speech, reference)`.
  - `clean_text(text, limit=5000) -> str`; `audio_label(lang, no_speech, reference) -> AudioLabel` (ValueError); `check_score(score) -> int` (ValueError outside 1–5); `audio_paths(sample_id) -> (copy, view)` = `("audio/<id>.opus", "audio/<id>.view.opus")`; `keyframe_path(sample_id)` = `"keyframes/<id>.jpg"` (relative to the eval dir); `sample_exists(pool, table, sample_id) -> bool` (`table` is `eval_audio` or `eval_caption`, else ValueError).
  - `audio_candidates(pool, zone, seed=0) -> dict[str, {"available": int, "items": [{"id", "started_at", "part", "transcript"}]}]` keyed by `"vi"`, `"ko"`, `"en"` and `""` (Whisper heard nothing); `transcripts(pool, item_ids) -> dict[str, str]`.
  - `add_audio(pool, settings, item_id, label) -> str` (GoldenError), `update_audio(pool, sample_id, label) -> bool`, `audio_samples(pool) -> list[dict]` (`id`, `item_id`, `lang`, `no_speech`, `reference`, `started_at`), `delete_audio(pool, settings, sample_id) -> bool`.
  - `caption_candidates(pool, zone, seed=0, limit=12) -> list[dict]` (`id`, `ts`, `caption`, `caption_model`); `add_caption(pool, settings, keyframe_id, score, flags) -> str` (GoldenError, ValueError); `score_caption(pool, sample_id, score, flags) -> bool`; `caption_samples(pool) -> list[dict]` (`id`, `keyframe_id`, `ts`, `caption`, `caption_model`, `scores`, `needs_score`); `delete_caption(pool, settings, sample_id) -> bool`.
  - `progress(pool) -> {"audio": {lang|"none": n}, "audio_total", "captions", "captions_scored"}`.
  - `forget_rows(conn, item_ids, keyframe_ids, moment_ids, delete: bool) -> int` and `sweep(pool, eval_dir) -> int`.
  - `forget.ForgetCounts.golden` (counted in the preview, deleted with the range, shown on the preview and the result); `forget_range` and the 03:00 retention tick (on or off) call `golden.sweep` — a failure there is logged and stops nothing.
  - Test helpers: `analysed_audio(pool, settings, device_id, start, *, seconds=5, source=…) -> item id` (a finished recording with a real Ogg/Opus file), `insert_segment(pool, item_id, device_id, start, text, *, lang="vi", seconds=2, lang_prob=0.9) -> segment id`, `captioned_keyframe(pool, settings, device_id, ts, *, caption=None, model="ollama:minicpm-v4.6:1b") -> keyframe id`.

- [ ] **Step 1: Write the failing tests**

Apply to `brain/tests/helpers.py`:

```diff
diff --git a/brain/tests/helpers.py b/brain/tests/helpers.py
--- a/brain/tests/helpers.py
+++ b/brain/tests/helpers.py
@@ -440,3 +440,42 @@ def write_media(settings, rel: str, data: bytes = b"x") -> str:
     path.parent.mkdir(parents=True, exist_ok=True)
     path.write_bytes(data)
     return rel
+
+
+async def analysed_audio(pool, settings, device_id: str, start: dt.datetime, *, seconds: float = 5,
+                         source: str = "anullsrc=r=16000:cl=mono") -> str:
+    """A finished audio recording with a real Ogg/Opus file (``source``: an ffmpeg lavfi signal); return its id."""
+    item_id = await insert_item(pool, device_id, kind="audio", started_at=start, seconds=seconds)
+    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
+    make_opus(settings.media_root / row["raw_path"], source, seconds)
+    async with pool.connection() as conn:
+        await conn.execute("UPDATE item SET analysis_state = 'done', analyzed_at = now() WHERE id = %s", (item_id,))
+    return item_id
+
+
+async def insert_segment(pool, item_id: str, device_id: str, start: dt.datetime, text: str, *, lang: str = "vi",
+                         seconds: float = 2, lang_prob: float | None = 0.9) -> str:
+    """A transcript segment of ``item_id`` starting at ``start``; return its id."""
+    segment_id = new_ulid()
+    async with pool.connection() as conn:
+        await conn.execute(
+            "INSERT INTO transcript_segment (id, item_id, device_id, start_ts, end_ts, lang, text, lang_prob,"
+            " model_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'test')",
+            (segment_id, item_id, device_id, start, start + dt.timedelta(seconds=seconds), lang, text, lang_prob),
+        )
+    return segment_id
+
+
+async def captioned_keyframe(pool, settings, device_id: str, ts: dt.datetime, *, caption: dict | None = None,
+                             model: str = "ollama:minicpm-v4.6:1b") -> str:
+    """A keyframe (with its video item and a real JPEG) captioned by ``model``; return the keyframe id."""
+    from psycopg.types.json import Jsonb
+
+    item_id = await insert_item(pool, device_id, kind="video", started_at=ts - dt.timedelta(seconds=10))
+    kf_id = await insert_keyframe(pool, settings, item_id, device_id, ts)
+    async with pool.connection() as conn:
+        await conn.execute(
+            "UPDATE keyframe SET caption = %s, caption_model = %s, captioned_at = now() WHERE id = %s",
+            (Jsonb(caption or DEFAULT_CAPTION), model, kf_id),
+        )
+    return kf_id
```

Create `brain/tests/test_eval_golden.py`:

```python
import datetime as dt
import logging

import pytest

from helpers import (
    QUIET_SPEECH,
    analysed_audio,
    captioned_keyframe,
    fetch_all,
    fetch_one,
    insert_moment,
    insert_segment,
    make_ctx,
    paired_device,
)
from loopi_brain import forget
from loopi_brain.evaluation import golden
from loopi_brain.evaluation.golden import GoldenError
from loopi_brain.pipeline import context, retention

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
THREE_IN_SEOUL = int(dt.datetime(2026, 10, 14, 18, 0, tzinfo=dt.UTC).timestamp())
VI = golden.AudioLabel("vi", False, "Mai mình đi chợ nhé")


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _spoken(pool, settings, device_id, start, lang, text, **kw) -> str:
    item_id = await analysed_audio(pool, settings, device_id, start, **kw)
    await insert_segment(pool, item_id, device_id, start, text, lang=lang)
    return item_id


def _hours(n: float) -> dt.timedelta:
    return dt.timedelta(hours=n)


async def test_clips_are_proposed_by_language_and_part_of_the_day(pool, pki_settings, dev):
    d = dev["device_id"]
    morning = [await _spoken(pool, pki_settings, d, NINE + dt.timedelta(minutes=m), "vi", f"câu {m}") for m in (0, 10, 20)]
    korean = await _spoken(pool, pki_settings, d, NINE + _hours(5), "ko", "네 알겠어요")  # 14:00
    english = await _spoken(pool, pki_settings, d, NINE + _hours(12), "en", "Thank you.")  # 21:00
    silent = await analysed_audio(pool, pki_settings, d, NINE - _hours(6))  # 03:00, Whisper heard nothing
    expired = await _spoken(pool, pki_settings, d, NINE + _hours(1), "vi", "đã hết hạn")
    pending = await _spoken(pool, pki_settings, d, NINE + _hours(2), "vi", "chưa xong")
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (expired,))
        await conn.execute("UPDATE item SET analysis_state = 'pending' WHERE id = %s", (pending,))
    strata = await golden.audio_candidates(pool, pki_settings.zone)
    assert set(strata) == {"vi", "ko", "en", ""}
    vi = strata["vi"]
    assert vi["available"] == 3 and len(vi["items"]) == golden.PER_PART  # 2 of the 3 morning recordings
    assert {i["id"] for i in vi["items"]} < set(morning)
    assert all(i["part"] == "sáng (6–12 giờ)" for i in vi["items"])
    assert [i["transcript"] for i in strata["ko"]["items"]] == ["네 알겠어요"]
    assert [(i["id"], i["part"]) for i in strata["en"]["items"]] == [(english, "tối (18–24 giờ)")]
    assert [(i["id"], i["transcript"]) for i in strata[""]["items"]] == [(silent, "")]
    assert strata["ko"]["items"][0]["id"] == korean
    await golden.add_audio(pool, pki_settings, korean, golden.AudioLabel("ko", False, "네 알겠어요"))
    again = await golden.audio_candidates(pool, pki_settings.zone)
    assert again["ko"] == {"available": 0, "items": []}  # a golden clip is not proposed again
    other = await golden.audio_candidates(pool, pki_settings.zone, seed=1)
    assert len(other["vi"]["items"]) == 2  # another draw ("Gợi ý khác") of the same recordings


async def test_a_labelled_clip_is_copied_and_outlives_retention(pool, pki_settings, dev):
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "mai mình đi chợ", source=QUIET_SPEECH)
    raw = pki_settings.media_root / (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,)))["raw_path"]
    sample = await golden.add_audio(pool, pki_settings, item, VI)
    copy, view = (pki_settings.eval_dir / rel for rel in golden.audio_paths(sample))
    assert copy.read_bytes() == raw.read_bytes()  # the runner hears exactly what the live stage heard
    assert view.is_file() and view.stat().st_size > 0  # the page plays it at a level you can hear (D19)
    raw.unlink()  # retention removed the raw recording (D20)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (item,))
    [row] = await golden.audio_samples(pool)
    assert (row["id"], row["item_id"], row["lang"], row["no_speech"], row["reference"]) == (
        sample, item, "vi", False, "Mai mình đi chợ nhé")
    assert copy.is_file() and view.is_file()


async def test_a_clip_needs_a_kept_finished_recording_and_a_valid_label(pool, pki_settings, dev):
    d = dev["device_id"]
    item = await _spoken(pool, pki_settings, d, NINE, "vi", "x")
    with pytest.raises(GoldenError):
        await golden.add_audio(pool, pki_settings, "01UNKNOWN0000000000000000", VI)
    await golden.add_audio(pool, pki_settings, item, VI)
    with pytest.raises(GoldenError, match="already"):
        await golden.add_audio(pool, pki_settings, item, VI)
    expired = await _spoken(pool, pki_settings, d, NINE + _hours(1), "vi", "x")
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (expired,))
    with pytest.raises(GoldenError):
        await golden.add_audio(pool, pki_settings, expired, VI)
    for lang, no_speech, reference in (("ja", False, "x"), ("vi", False, " \x00 "), ("", False, "x")):
        with pytest.raises(ValueError):
            golden.audio_label(lang, no_speech, reference)
    assert golden.audio_label("", True, "ignored") == golden.AudioLabel(None, True, "")
    assert golden.audio_label("ko", False, " 네\n알겠어요\x00 ") == golden.AudioLabel("ko", False, "네 알겠어요")
    assert len(await fetch_all(pool, "SELECT id FROM eval_audio")) == 1


async def test_a_recording_whose_file_is_missing_leaves_no_sample(pool, pki_settings, dev, caplog):
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,)))["raw_path"]
    (pki_settings.media_root / raw).unlink()
    with caplog.at_level(logging.WARNING), pytest.raises(GoldenError, match="copied"):
        await golden.add_audio(pool, pki_settings, item, golden.AudioLabel("vi", False, "bí mật riêng tư"))
    assert await fetch_all(pool, "SELECT id FROM eval_audio") == []
    assert "bí mật" not in caplog.text  # a reference is never logged


async def test_labels_can_be_corrected_and_clips_deleted_with_their_copies(pool, pki_settings, dev):
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "en", "Thank you.")
    sample = await golden.add_audio(pool, pki_settings, item, golden.AudioLabel("en", False, "Thank you."))
    assert await golden.update_audio(pool, sample, golden.audio_label("", True, ""))
    assert (await golden.audio_samples(pool))[0]["no_speech"] is True
    assert await golden.delete_audio(pool, pki_settings, sample)
    assert not any((pki_settings.eval_dir / rel).exists() for rel in golden.audio_paths(sample))
    assert not await golden.delete_audio(pool, pki_settings, sample)
    assert not await golden.update_audio(pool, sample, VI)


async def test_keyframes_are_proposed_one_per_day_and_three_hour_block(pool, pki_settings, dev):
    d = dev["device_id"]
    first = await captioned_keyframe(pool, pki_settings, d, NINE)
    same_block = await captioned_keyframe(pool, pki_settings, d, NINE + dt.timedelta(minutes=30))
    afternoon = await captioned_keyframe(pool, pki_settings, d, NINE + _hours(4))  # 13:00
    next_day = await captioned_keyframe(pool, pki_settings, d, NINE + _hours(24))
    uncaptioned = await captioned_keyframe(pool, pki_settings, d, NINE + _hours(7))
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = NULL WHERE id = %s", (uncaptioned,))
    found = await golden.caption_candidates(pool, pki_settings.zone)
    assert len(found) == 3 and [k["ts"] for k in found] == sorted(k["ts"] for k in found)
    assert {k["id"] for k in found} - {first, same_block} == {afternoon, next_day}
    assert len({first, same_block} & {k["id"] for k in found}) == 1
    assert len(await golden.caption_candidates(pool, pki_settings.zone, limit=2)) == 2


async def test_a_scored_keyframe_is_copied_and_scored_again_for_a_new_caption_model(pool, pki_settings, dev):
    kf = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    src = pki_settings.media_root / (await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (kf,)))["path"]
    sample = await golden.add_caption(pool, pki_settings, kf, 4, {"invented_objects"})
    assert (pki_settings.eval_dir / golden.keyframe_path(sample)).read_bytes() == src.read_bytes()
    [s] = await golden.caption_samples(pool)
    assert s["needs_score"] is False and [(x["model_version"], x["score"], x["invented_objects"], x["wrong_people"])
                                          for x in s["scores"]] == [("ollama:minicpm-v4.6:1b", 4, True, False)]
    with pytest.raises(GoldenError, match="already"):
        await golden.add_caption(pool, pki_settings, kf, 3, set())
    async with pool.connection() as conn:  # "Xử lý lại → mô tả khung hình" with another model
        await conn.execute("UPDATE keyframe SET caption = '{\"scene\": \"A room\"}', caption_model = 'ollama:new'")
    [s] = await golden.caption_samples(pool)
    assert s["needs_score"] is True
    assert await golden.score_caption(pool, sample, 2, {"wrong_people", "wrong_activity"})
    [s] = await golden.caption_samples(pool)
    assert [(x["model_version"], x["score"], x["caption"]) for x in s["scores"]] == [
        ("ollama:minicpm-v4.6:1b", 4, s["scores"][0]["caption"]), ("ollama:new", 2, {"scene": "A room"})]
    assert s["needs_score"] is False
    assert await golden.progress(pool) == {"audio": {"vi": 0, "ko": 0, "en": 0, "none": 0}, "audio_total": 0,
                                           "captions": 1, "captions_scored": 1}
    for bad in ((0, set()), (6, set()), (3, {"blurry"})):
        with pytest.raises(ValueError):
            await golden.score_caption(pool, sample, *bad)
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = NULL")  # being captioned again: nothing to score now
    assert not await golden.score_caption(pool, sample, 3, set())
    assert await golden.delete_caption(pool, pki_settings, sample)
    assert not (pki_settings.eval_dir / golden.keyframe_path(sample)).exists()


async def test_an_uncaptioned_keyframe_is_not_a_sample(pool, pki_settings, dev):
    kf = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = NULL")
    with pytest.raises(GoldenError):
        await golden.add_caption(pool, pki_settings, kf, 3, set())


async def test_the_sweep_deletes_copies_without_a_sample(pool, pki_settings, dev):
    assert await golden.sweep(pool, pki_settings.eval_dir) == 0  # no eval dir yet
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    sample = await golden.add_audio(pool, pki_settings, item, VI)
    stray = [pki_settings.eval_dir / "audio" / "01STRAY.opus", pki_settings.eval_dir / "audio" / "01STRAY.ab12.part",
             pki_settings.eval_dir / "keyframes" / "01GONE.jpg"]
    for path in stray:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    assert await golden.sweep(pool, pki_settings.eval_dir) == 3
    assert not any(p.exists() for p in stray)
    assert all((pki_settings.eval_dir / rel).is_file() for rel in golden.audio_paths(sample))


async def test_forgetting_a_range_takes_its_golden_samples_and_their_copies(pool, pki_settings, dev):
    d = dev["device_id"]
    clip_item = await _spoken(pool, pki_settings, d, NINE, "vi", "x")
    clip = await golden.add_audio(pool, pki_settings, clip_item, VI)
    kf = await captioned_keyframe(pool, pki_settings, d, NINE + dt.timedelta(minutes=5))
    picture = await golden.add_caption(pool, pki_settings, kf, 5, set())
    speech = await insert_moment(pool, d, kind="speech", ts=NINE, item_id=clip_item, text="x")
    later_item = await _spoken(pool, pki_settings, d, NINE + _hours(3), "vi", "y")
    later = await golden.add_audio(pool, pki_settings, later_item, VI)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO eval_question (id, text) VALUES ('Q1', 'Sáng nay mình nói gì?')")
        await conn.execute(
            "INSERT INTO eval_answer (question_id, moment_id, kind, item_id, ts_start, ts_end)"
            " VALUES ('Q1', %s, 'speech', %s, %s, %s)", (speech, clip_item, NINE, NINE))
    start, end = NINE - dt.timedelta(minutes=1), NINE + _hours(1)
    assert (await forget.preview(pool, start, end, pki_settings.zone)).golden == 3
    counts = await forget.forget_range(pool, pki_settings, start, end)
    assert counts.golden == 3
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM eval_audio")] == [later]
    assert await fetch_all(pool, "SELECT * FROM eval_caption") == [] and await fetch_all(pool, "SELECT * FROM eval_answer") == []
    assert await fetch_all(pool, "SELECT id FROM eval_question") == [{"id": "Q1"}]  # the owner's question stays
    gone = [*golden.audio_paths(clip), golden.keyframe_path(picture)]
    assert not any((pki_settings.eval_dir / rel).exists() for rel in gone)
    assert all((pki_settings.eval_dir / rel).is_file() for rel in golden.audio_paths(later))


async def test_retention_never_expires_golden_copies_and_its_tick_sweeps_stray_ones(pool, pki_settings, dev):
    from loopi_brain import store

    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    sample = await golden.add_audio(pool, pki_settings, item, VI)
    stray = pki_settings.eval_dir / "keyframes" / "01GONE.jpg"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"x")
    await store.set_retention(pool, store.Retention(enabled=True))
    async with pool.connection() as conn:  # the recording is old enough to expire
        await conn.execute("UPDATE item SET started_at = started_at - interval '30 days',"
                           " ended_at = ended_at - interval '30 days'")
    context.set_context(make_ctx(pool, pki_settings))
    try:
        await retention.apply_retention_job(timestamp=THREE_IN_SEOUL)
    finally:
        context.set_context(None)
    assert (await fetch_one(pool, "SELECT raw_deleted_at FROM item"))["raw_deleted_at"] is not None
    assert all((pki_settings.eval_dir / rel).is_file() for rel in golden.audio_paths(sample))
    assert not stray.exists()


@pytest.mark.parametrize("kind", ["audio", "caption"])
async def test_a_sample_forgotten_while_its_copy_is_written_leaves_no_copy(pool, pki_settings, dev, monkeypatch, kind):
    import psycopg

    real_copy = golden._copy

    def copy_then_forget(src, dst):  # runs in a worker thread, like the real copy
        real_copy(src, dst)
        with psycopg.connect(pki_settings.database_url, autocommit=True) as conn:
            conn.execute("DELETE FROM item")  # "Quên khoảng thời gian" took the recording (cascades to the sample)

    monkeypatch.setattr(golden, "_copy", copy_then_forget)
    if kind == "audio":
        item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
        with pytest.raises(GoldenError, match="forgotten"):
            await golden.add_audio(pool, pki_settings, item, VI)
    else:
        kf = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
        with pytest.raises(GoldenError, match="forgotten"):
            await golden.add_caption(pool, pki_settings, kf, 3, set())
    assert not [p for p in pki_settings.eval_dir.rglob("*") if p.is_file()]


async def test_a_keyframe_whose_picture_is_missing_leaves_no_sample(pool, pki_settings, dev):
    kf = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    (pki_settings.media_root / (await fetch_one(pool, "SELECT path FROM keyframe"))["path"]).unlink()
    with pytest.raises(GoldenError, match="copied"):
        await golden.add_caption(pool, pki_settings, kf, 3, set())
    assert await fetch_all(pool, "SELECT id FROM eval_caption") == []


async def test_a_clip_ffmpeg_cannot_raise_is_kept_and_plays_as_recorded(pool, pki_settings, dev, monkeypatch, caplog):
    from loopi_brain import media

    async def broken(src, dst):
        raise media.MediaError("ffmpeg exit 1")

    monkeypatch.setattr(media, "normalise_audio", broken)
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    with caplog.at_level(logging.WARNING):
        sample = await golden.add_audio(pool, pki_settings, item, VI)
    copy, view = (pki_settings.eval_dir / rel for rel in golden.audio_paths(sample))
    assert copy.is_file() and not view.exists() and "no playback copy" in caplog.text


async def test_a_failing_sweep_stops_neither_a_forget_nor_retention(pool, pki_settings, dev, monkeypatch, caplog):
    async def broken(pool, eval_dir):
        raise OSError("disk unplugged")

    monkeypatch.setattr(golden, "sweep", broken)
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    await golden.add_audio(pool, pki_settings, item, VI)
    with caplog.at_level(logging.ERROR):
        counts = await forget.forget_range(pool, pki_settings, NINE - dt.timedelta(minutes=1), NINE + _hours(1))
    assert counts.golden == 1 and counts.items == 1 and "could not sweep the golden copies" in caplog.text
    calls = []

    async def apply(pool, settings, now=None):
        calls.append(now)

    monkeypatch.setattr(retention, "apply_retention", apply)
    context.set_context(make_ctx(pool, pki_settings))
    try:
        await retention.apply_retention_job(timestamp=THREE_IN_SEOUL)
    finally:
        context.set_context(None)
    assert calls == [None]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_eval_golden.py -q`
Expected: FAIL — `1 error` during collection: `ImportError: cannot import name 'golden' from 'loopi_brain.evaluation'`.

- [ ] **Step 3: Write the golden-set store**

Create `brain/src/loopi_brain/evaluation/golden.py`:

```python
"""The owner's golden set of clips and keyframes (spec §13, D21): what to propose, the copies, the labels.

A golden clip is one whole audio recording (the agent cuts them at 60 s) that the owner listened to on the Đánh giá
page and wrote down by hand — the reference transcript — or marked as having no speech at all (silence, TV, room
noise: the check for invented text). A golden keyframe carries the owner's 1–5 score of its caption, per caption
model, and three ticks ("sai số người", "bịa đồ vật", "sai hoạt động").

Each sample gets its own copy under LOOPI_EVAL_DIR, so it outlives retention (D20 deletes raw audio after 7 days):
``audio/<id>.opus`` (the raw recording, byte for byte: the runner hears what the live stage heard),
``audio/<id>.view.opus`` (the same at a level you can hear, D19) and ``keyframes/<id>.jpg``. The row is committed
before its files are written, so ``sweep`` — which deletes every file whose sample has no row — never takes a copy
being made; "Quên khoảng thời gian" deletes the samples of its range (``forget_rows``) and then sweeps. Logs carry
ids and counts, never a reference or a caption.
"""
from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import anyio
import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .. import devices, media
from ..config import Settings

log = logging.getLogger(__name__)
LANGUAGES = ("vi", "ko", "en")
NO_SPEECH = "none"  # the language bucket of the clips marked "không có lời nói"
# The page's target: 20 clips, about this many of each kind (a scarce language is reported, never faked).
AUDIO_TARGET = 20
AUDIO_SUGGESTED = {"vi": 6, "ko": 6, "en": 4, NO_SPEECH: 4}
CAPTION_TARGET = 30
PER_PART = 2  # proposals per detected language and part of the day
DAY_PARTS = ("đêm (0–6 giờ)", "sáng (6–12 giờ)", "chiều (12–18 giờ)", "tối (18–24 giờ)")
CAPTIONS_SHOWN = 12
REFERENCE_MAX = 5000  # characters of a reference transcript
AUDIO_DIR = "audio"
KEYFRAME_DIR = "keyframes"


class GoldenError(Exception):
    """A sample cannot be made: its recording or keyframe is gone, expired, not analysed, or already a sample."""


@dataclass(frozen=True)
class AudioLabel:
    lang: str | None
    no_speech: bool
    reference: str


def clean_text(text: str, limit: int = REFERENCE_MAX) -> str:
    """Text as the owner typed it, without control characters (PostgreSQL text cannot hold NUL) and with its
    whitespace collapsed; at most ``limit`` characters."""
    return " ".join("".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in text).split())[:limit]


def audio_label(lang: str, no_speech: bool, reference: str) -> AudioLabel:
    """The label of a clip: "không có lời nói", or a language of the household and a non-empty reference.
    ValueError otherwise."""
    if no_speech:
        return AudioLabel(None, True, "")
    if lang not in LANGUAGES:
        raise ValueError(f"the language must be one of {', '.join(LANGUAGES)}")
    reference = clean_text(reference)
    if not reference:
        raise ValueError("a clip with speech needs its reference transcript")
    return AudioLabel(lang, False, reference)


def check_score(score: int) -> int:
    if not 1 <= score <= 5:
        raise ValueError("a caption score is 1 to 5")
    return score


def audio_paths(sample_id: str) -> tuple[str, str]:
    """(byte copy, playback copy) of a clip, relative to the eval dir."""
    return f"{AUDIO_DIR}/{sample_id}.opus", f"{AUDIO_DIR}/{sample_id}.view.opus"


def keyframe_path(sample_id: str) -> str:
    return f"{KEYFRAME_DIR}/{sample_id}.jpg"


def _copy(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` through a temporary name (the copy appears whole or not at all)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name.split('.')[0]}.{uuid.uuid4().hex}.part")
    try:
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


async def sample_exists(pool: AsyncConnectionPool, table: str, sample_id: str) -> bool:
    """Is there a golden clip (``table`` "eval_audio") or keyframe ("eval_caption") with this id?"""
    if table not in ("eval_audio", "eval_caption"):
        raise ValueError(f"not a golden-set table: {table}")
    async with pool.connection() as conn:
        cur = await conn.execute(f"SELECT 1 FROM {table} WHERE id = %s", (sample_id,))
        return await cur.fetchone() is not None


async def _drop(pool: AsyncConnectionPool, settings: Settings, table: str, sample_id: str, files: list[str]) -> None:
    async with pool.connection() as conn:
        await conn.execute(f"DELETE FROM {table} WHERE id = %s", (sample_id,))
    await anyio.to_thread.run_sync(media.remove_files, settings.eval_dir, files)


# ---- clips -------------------------------------------------------------------------------------------------------

_AUDIO_CANDIDATES = """
WITH c AS (
    SELECT i.id, i.started_at,
           (SELECT s.lang FROM transcript_segment s WHERE s.item_id = i.id GROUP BY s.lang
             ORDER BY sum(extract(epoch FROM s.end_ts - s.start_ts)) DESC, s.lang LIMIT 1) AS lang,
           extract(hour FROM i.started_at AT TIME ZONE %(tz)s)::int / 6 AS part
      FROM item i
     WHERE i.kind = 'audio' AND i.analysis_state = 'done' AND i.raw_deleted_at IS NULL
       AND NOT EXISTS (SELECT 1 FROM eval_audio e WHERE e.item_id = i.id)
), r AS (
    SELECT *, coalesce(lang, '') AS stratum, count(*) OVER (PARTITION BY coalesce(lang, '')) AS available,
           row_number() OVER (PARTITION BY coalesce(lang, ''), part ORDER BY md5(id || %(seed)s), id) AS n
      FROM c
)
SELECT id, started_at, stratum, part, available FROM r WHERE n <= %(per_part)s ORDER BY stratum, started_at
"""


async def transcripts(pool: AsyncConnectionPool, item_ids: list[str]) -> dict[str, str]:
    """The current transcript of each recording: its segments in time order, joined by spaces."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT item_id, string_agg(text, ' ' ORDER BY start_ts) AS text FROM transcript_segment"
            " WHERE item_id = ANY(%s) GROUP BY item_id",
            (item_ids,),
        )
        return {r["item_id"]: r["text"] for r in await cur.fetchall()}


async def audio_candidates(pool: AsyncConnectionPool, zone, seed: int = 0) -> dict[str, dict]:
    """Recordings to propose, by the language Whisper heard most in them ('' for none heard): up to PER_PART per
    part of the day (local time), spread over the days by a stable pseudo-random order that ``seed`` changes. Only
    finished recordings whose raw audio is still kept and that are not golden clips yet; each with its current
    transcript. Every language of the household is listed, with how many recordings are available."""
    async with pool.connection() as conn:
        cur = await conn.execute(_AUDIO_CANDIDATES, {"tz": str(zone), "seed": str(seed), "per_part": PER_PART})
        rows = await cur.fetchall()
    texts = await transcripts(pool, [r["id"] for r in rows])
    strata: dict[str, dict] = {lang: {"available": 0, "items": []} for lang in (*LANGUAGES, "")}
    for row in rows:
        stratum = strata.setdefault(row["stratum"], {"available": 0, "items": []})
        stratum["available"] = row["available"]
        stratum["items"].append({"id": row["id"], "started_at": row["started_at"], "part": DAY_PARTS[row["part"]],
                                 "transcript": texts.get(row["id"], "")})
    return strata


async def add_audio(pool: AsyncConnectionPool, settings: Settings, item_id: str, label: AudioLabel) -> str:
    """Make a finished recording whose raw audio is kept a golden clip with ``label``; return the sample id.
    GoldenError when it cannot be (gone, expired, not analysed, already a clip, or its file cannot be copied)."""
    sample_id = devices.new_id()
    copy, view = audio_paths(sample_id)
    try:
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT raw_path FROM item WHERE id = %s AND kind = 'audio' AND analysis_state = 'done'"
                " AND raw_deleted_at IS NULL FOR SHARE",
                (item_id,),
            )
            item = await cur.fetchone()
            if item is None:
                raise GoldenError("this recording is gone, expired or not analysed yet")
            await conn.execute(
                "INSERT INTO eval_audio (id, item_id, path, lang, no_speech, reference) VALUES (%s, %s, %s, %s, %s, %s)",
                (sample_id, item_id, copy, label.lang, label.no_speech, label.reference),
            )
    except psycopg.errors.UniqueViolation as exc:
        raise GoldenError("this recording is already a golden clip") from exc
    try:
        src = media.inside(settings.media_root, item["raw_path"])
        await anyio.to_thread.run_sync(_copy, src, settings.eval_dir / copy)
    except (OSError, ValueError) as exc:
        log.warning("golden clip %s: the recording %s could not be copied: %s", sample_id, item_id, exc)
        await _drop(pool, settings, "eval_audio", sample_id, [copy])
        raise GoldenError("the recording's file could not be copied") from exc
    try:
        await media.normalise_audio(settings.eval_dir / copy, settings.eval_dir / view)
    except Exception:  # the page plays the byte copy instead
        log.warning("golden clip %s: no playback copy; the page plays it at its raw level", sample_id, exc_info=True)
    if not await sample_exists(pool, "eval_audio", sample_id):  # forgotten while the files were written
        await anyio.to_thread.run_sync(media.remove_files, settings.eval_dir, [copy, view])
        raise GoldenError("this recording was forgotten meanwhile")
    log.info("golden clip %s added from recording %s", sample_id, item_id)
    return sample_id


async def update_audio(pool: AsyncConnectionPool, sample_id: str, label: AudioLabel) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE eval_audio SET lang = %s, no_speech = %s, reference = %s, updated_at = now() WHERE id = %s",
            (label.lang, label.no_speech, label.reference, sample_id),
        )
        return cur.rowcount == 1


async def audio_samples(pool: AsyncConnectionPool) -> list[dict]:
    """Every golden clip, oldest recording first: its label and when it was recorded."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT e.id, e.item_id, e.lang, e.no_speech, e.reference, i.started_at FROM eval_audio e"
            " JOIN item i ON i.id = e.item_id ORDER BY i.started_at, e.id"
        )
        return await cur.fetchall()


async def delete_audio(pool: AsyncConnectionPool, settings: Settings, sample_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM eval_audio WHERE id = %s", (sample_id,))
        found = cur.rowcount == 1
    await anyio.to_thread.run_sync(media.remove_files, settings.eval_dir, list(audio_paths(sample_id)))
    return found


# ---- keyframes ---------------------------------------------------------------------------------------------------

_CAPTION_CANDIDATES = """
WITH c AS (
    SELECT k.id, k.ts, k.caption, k.caption_model,
           (k.ts AT TIME ZONE %(tz)s)::date AS day, extract(hour FROM k.ts AT TIME ZONE %(tz)s)::int / 3 AS block
      FROM keyframe k
     WHERE k.caption IS NOT NULL AND NOT EXISTS (SELECT 1 FROM eval_caption e WHERE e.keyframe_id = k.id)
), r AS (
    SELECT *, row_number() OVER (PARTITION BY day, block ORDER BY md5(id || %(seed)s), id) AS n FROM c
)
SELECT id, ts, caption, caption_model FROM r WHERE n = 1 ORDER BY md5(id || %(seed)s), id LIMIT %(limit)s
"""


async def caption_candidates(pool: AsyncConnectionPool, zone, seed: int = 0, limit: int = CAPTIONS_SHOWN) -> list[dict]:
    """Captioned keyframes that are not golden yet, spread over days and hours: at most one per local day and
    3-hour block, ``limit`` of those in a stable pseudo-random order (``seed``), shown in time order."""
    async with pool.connection() as conn:
        cur = await conn.execute(_CAPTION_CANDIDATES, {"tz": str(zone), "seed": str(seed), "limit": limit})
        rows = await cur.fetchall()
    return sorted(rows, key=lambda r: (r["ts"], r["id"]))


async def add_caption(
    pool: AsyncConnectionPool, settings: Settings, keyframe_id: str, score: int, flags: set[str]
) -> str:
    """Make a captioned keyframe a golden one with the owner's score of its current caption; return the sample id.
    ``flags``: any of ``wrong_people``, ``invented_objects``, ``wrong_activity``. GoldenError when it cannot be."""
    check_score(score)
    sample_id = devices.new_id()
    copy = keyframe_path(sample_id)
    try:
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT path, caption, caption_model FROM keyframe WHERE id = %s AND caption IS NOT NULL FOR SHARE",
                (keyframe_id,),
            )
            kf = await cur.fetchone()
            if kf is None:
                raise GoldenError("this keyframe is gone or has no caption yet")
            await conn.execute("INSERT INTO eval_caption (id, keyframe_id, path) VALUES (%s, %s, %s)",
                               (sample_id, keyframe_id, copy))
            await _store_score(conn, sample_id, kf["caption_model"], kf["caption"], score, flags)
    except psycopg.errors.UniqueViolation as exc:
        raise GoldenError("this keyframe is already a golden one") from exc
    try:
        src = media.inside(settings.media_root, kf["path"])
        await anyio.to_thread.run_sync(_copy, src, settings.eval_dir / copy)
    except (OSError, ValueError) as exc:
        log.warning("golden keyframe %s: keyframe %s could not be copied: %s", sample_id, keyframe_id, exc)
        await _drop(pool, settings, "eval_caption", sample_id, [copy])
        raise GoldenError("the keyframe's picture could not be copied") from exc
    if not await sample_exists(pool, "eval_caption", sample_id):
        await anyio.to_thread.run_sync(media.remove_files, settings.eval_dir, [copy])
        raise GoldenError("this keyframe was forgotten meanwhile")
    log.info("golden keyframe %s added from keyframe %s", sample_id, keyframe_id)
    return sample_id


FLAGS = ("wrong_people", "invented_objects", "wrong_activity")


async def _store_score(conn: AsyncConnection, sample_id: str, model: str, caption: dict, score: int,
                       flags: set[str]) -> None:
    unknown = set(flags) - set(FLAGS)
    if unknown:
        raise ValueError(f"unknown caption flags: {', '.join(sorted(unknown))}")
    await conn.execute(
        "INSERT INTO eval_caption_score (sample_id, model_version, caption, score, wrong_people, invented_objects,"
        " wrong_activity) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (sample_id, model_version) DO UPDATE SET caption = EXCLUDED.caption, score = EXCLUDED.score,"
        " wrong_people = EXCLUDED.wrong_people, invented_objects = EXCLUDED.invented_objects,"
        " wrong_activity = EXCLUDED.wrong_activity, scored_at = now()",
        (sample_id, model, Jsonb(caption), score, *(f in flags for f in FLAGS)),
    )


async def score_caption(pool: AsyncConnectionPool, sample_id: str, score: int, flags: set[str]) -> bool:
    """Score the golden keyframe's current caption (its model's score is replaced); False when the sample is gone
    or its keyframe has no caption right now (being captioned again)."""
    check_score(score)
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT k.caption, k.caption_model FROM eval_caption e JOIN keyframe k ON k.id = e.keyframe_id"
            " WHERE e.id = %s AND k.caption IS NOT NULL FOR SHARE OF k",
            (sample_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        await _store_score(conn, sample_id, row["caption_model"], row["caption"], score, flags)
    return True


async def caption_samples(pool: AsyncConnectionPool) -> list[dict]:
    """Every golden keyframe in time order: its current caption and model, its scores (one per caption model) and
    whether the current caption still waits for a score."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT e.id, e.keyframe_id, k.ts, k.caption, k.caption_model FROM eval_caption e"
            " JOIN keyframe k ON k.id = e.keyframe_id ORDER BY k.ts, e.id"
        )
        samples = await cur.fetchall()
        cur = await conn.execute("SELECT * FROM eval_caption_score ORDER BY scored_at")
        scores = await cur.fetchall()
    by_sample: dict[str, list[dict]] = {}
    for score in scores:
        by_sample.setdefault(score["sample_id"], []).append(score)
    return [
        {**s, "scores": by_sample.get(s["id"], []),
         "needs_score": s["caption"] is not None
         and all(x["model_version"] != s["caption_model"] for x in by_sample.get(s["id"], []))}
        for s in samples
    ]


async def delete_caption(pool: AsyncConnectionPool, settings: Settings, sample_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM eval_caption WHERE id = %s", (sample_id,))
        found = cur.rowcount == 1
    await anyio.to_thread.run_sync(media.remove_files, settings.eval_dir, [keyframe_path(sample_id)])
    return found


# ---- the whole set ------------------------------------------------------------------------------------------------

async def progress(pool: AsyncConnectionPool) -> dict:
    """How far the labelling is: clips per language (NO_SPEECH for "không có lời nói"), golden keyframes and how many
    of them have a score for their current caption."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT coalesce(lang, %s) AS lang, count(*) AS n FROM eval_audio GROUP BY 1", (NO_SPEECH,)
        )
        audio = {lang: 0 for lang in AUDIO_SUGGESTED} | {r["lang"]: r["n"] for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT count(*) AS n, count(*) FILTER (WHERE EXISTS (SELECT 1 FROM eval_caption_score s"
            " WHERE s.sample_id = e.id AND s.model_version = k.caption_model)) AS scored"
            " FROM eval_caption e JOIN keyframe k ON k.id = e.keyframe_id"
        )
        captions = await cur.fetchone()
    return {"audio": audio, "audio_total": sum(audio.values()), "captions": captions["n"],
            "captions_scored": captions["scored"]}


async def forget_rows(conn: AsyncConnection, item_ids: list[str], keyframe_ids: list[str], moment_ids: list[str],
                      delete: bool) -> int:
    """The golden samples "Quên khoảng thời gian" takes with it: clips of its recordings, golden keyframes of its
    keyframes, and the correct moments of questions that point into it. Counted, and deleted when ``delete`` (inside
    the caller's transaction); the copies go afterwards, by ``sweep``."""
    params = {"items": item_ids, "keyframes": keyframe_ids, "moments": moment_ids}
    statements = (
        ("eval_audio", "item_id = ANY(%(items)s)"),
        ("eval_caption", "keyframe_id = ANY(%(keyframes)s)"),
        ("eval_answer", "item_id = ANY(%(items)s) OR keyframe_id = ANY(%(keyframes)s) OR moment_id = ANY(%(moments)s)"),
    )
    total = 0
    for table, where in statements:
        if delete:
            cur = await conn.execute(f"DELETE FROM {table} WHERE {where}", params)
            total += cur.rowcount
        else:
            cur = await conn.execute(f"SELECT count(*) AS n FROM {table} WHERE {where}", params)
            total += (await cur.fetchone())["n"]
    return total


def _files(eval_dir: Path) -> list[tuple[str, str, str]]:
    """(folder, sample id, path relative to the eval dir) of every file in the copy folders."""
    out = []
    for folder in (AUDIO_DIR, KEYFRAME_DIR):
        directory = eval_dir / folder
        if directory.is_dir():
            out.extend((folder, entry.name.split(".")[0], str(PurePosixPath(folder, entry.name)))
                       for entry in directory.iterdir() if entry.is_file() or entry.is_symlink())
    return out


async def sweep(pool: AsyncConnectionPool, eval_dir: Path) -> int:
    """Delete every copy whose sample has no row (forgotten, deleted, or a copy a crash left half made); return how
    many went. Runs after "Quên khoảng thời gian" and every night at 03:00."""
    files = await anyio.to_thread.run_sync(_files, eval_dir)
    if not files:
        return 0
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM eval_audio")
        kept = {(AUDIO_DIR, r["id"]) for r in await cur.fetchall()}
        cur = await conn.execute("SELECT id FROM eval_caption")
        kept |= {(KEYFRAME_DIR, r["id"]) for r in await cur.fetchall()}
    stale = [rel for folder, sample_id, rel in files if (folder, sample_id) not in kept]
    deleted, failed = await anyio.to_thread.run_sync(media.remove_files, eval_dir, stale)
    if stale:
        log.info("golden set: %d copies without a sample deleted, %d could not be", deleted, len(failed))
    return deleted
```

- [ ] **Step 4: Forgetting takes the range's golden samples; the 03:00 tick sweeps their copies**

```diff
diff --git a/brain/src/loopi_brain/forget.py b/brain/src/loopi_brain/forget.py
--- a/brain/src/loopi_brain/forget.py
+++ b/brain/src/loopi_brain/forget.py
@@ -4,7 +4,9 @@ In one transaction: the recordings (items) that touch the range, with their keyf
 and moments (embeddings included), every other moment of the range, the commitments sourced in it, and the
 recaps of the days it touches; the chapters of those days are reopened and the ones that overlap what goes lose
 their title, summary and summary error (they quoted it); the days' stored job errors and failed recap runs go too
-(an error may quote a model's answer; a failed run would keep the planner from writing the recap again). A
+(an error may quote a model's answer; a failed run would keep the planner from writing the recap again), and so do
+the golden-set samples taken from it (spec D21: clips, keyframes, correct moments of questions; their copies under
+LOOPI_EVAL_DIR are swept after the commit). A
 recording is deleted whole even when only part of it lies in the range, so "what goes" is the range widened to
 those recordings (the span): chapters, recaps and the audit row follow the span, not just the range, and may reach
 into the neighbouring day.
@@ -33,6 +35,7 @@ from psycopg_pool import AsyncConnectionPool
 from . import devices, media, thumbs
 from .config import Settings
 from .days import local_day
+from .evaluation import golden
 from .pipeline import chapters as chapter_rules
 from .pipeline import recap as recap_rules
 from .pipeline.state import lock_day
@@ -55,6 +58,7 @@ class ForgetCounts:
     commitments: int = 0
     chapters: int = 0  # chapters that lose their title and summary
     recaps: int = 0
+    golden: int = 0  # golden clips, keyframes and correct moments of questions
     files: int = 0
     files_failed: int = 0
 
@@ -127,7 +131,10 @@ async def _derived(conn: AsyncConnection, targets: dict, days: list[dt.date], sp
     )
     chapters = (await cur.fetchone())["n"]
     cur = await conn.execute("SELECT count(*) AS n FROM day_recap WHERE day = ANY(%s)", (days,))
-    return {"commitments": commitments, "chapters": chapters, "recaps": (await cur.fetchone())["n"]}
+    recaps = (await cur.fetchone())["n"]
+    golden_count = await golden.forget_rows(conn, [i["id"] for i in targets["items"]],
+                                            [k["id"] for k in targets["keyframes"]], moment_ids, delete=False)
+    return {"commitments": commitments, "chapters": chapters, "recaps": recaps, "golden": golden_count}
 
 
 async def preview_span(
@@ -207,6 +214,7 @@ async def _delete_rows(
         derived = await _derived(conn, targets, affected, span)
         item_ids = [r["id"] for r in targets["items"]]
         moment_ids = [m["id"] for m in targets["moments"]]
+        await golden.forget_rows(conn, item_ids, [k["id"] for k in targets["keyframes"]], moment_ids, delete=True)
         await conn.execute("DELETE FROM commitment WHERE source_moment_id = ANY(%s)", (moment_ids,))
         await conn.execute("DELETE FROM moment WHERE id = ANY(%s)", (moment_ids,))
         cur = await conn.execute("DELETE FROM transcript_segment WHERE item_id = ANY(%s)", (item_ids,))
@@ -267,6 +275,10 @@ async def forget_range(pool: AsyncConnectionPool, settings: Settings, start: dt.
     except Exception:  # the audit row still lists every path: the nightly run deletes them
         log.exception("deleting the media files failed; they stay listed on the audit row %s", log_id)
         deleted, failed = 0, files
+    try:  # the golden copies of the forgotten samples; anything left is swept again at 03:00
+        await golden.sweep(pool, settings.eval_dir)
+    except Exception:
+        log.exception("could not sweep the golden copies after the forget %s; the 03:00 run tries again", log_id)
     counts = ForgetCounts(**{**asdict(counts), "files": deleted, "files_failed": len(failed)})
     keep = await anyio.to_thread.run_sync(retryable, settings.media_root, failed, owners)
     try:
diff --git a/brain/src/loopi_brain/pipeline/retention.py b/brain/src/loopi_brain/pipeline/retention.py
--- a/brain/src/loopi_brain/pipeline/retention.py
+++ b/brain/src/loopi_brain/pipeline/retention.py
@@ -4,8 +4,9 @@ Every night at 03:00 local time — **only once the owner turned it on in Cài 
 what it would delete first) — raw recordings older than the kept days (7 and 7 by default) lose their raw file
 and the copy the web plays (video MP4, normalised audio): only recordings whose analysis is finished, never
 markers. The item row stays, marked ``raw_deleted_at`` so the pages say "đã hết hạn"; keyframes, thumbnails,
-transcripts, captions, moments, chapters and recaps are never deleted here. The same nightly run (on or off)
-retries the media files a "Quên khoảng thời gian" could not delete at once.
+transcripts, captions, moments, chapters and recaps are never deleted here, nor the golden-set copies under
+LOOPI_EVAL_DIR (spec D21). The same nightly run (on or off) retries the media files a "Quên khoảng thời gian" could
+not delete at once and sweeps the golden copies whose sample is gone (``evaluation.golden.sweep``).
 
 Order of work per batch: the rows are marked ``raw_deleted_at`` under their row locks (``FOR UPDATE SKIP LOCKED``:
 a row someone else holds, say a reprocess resetting it, is left for the next night) and that is committed
@@ -35,6 +36,7 @@ from psycopg.types.json import Jsonb
 from psycopg_pool import AsyncConnectionPool
 
 from .. import forget, media, store
+from ..evaluation import golden
 from ..config import Settings
 from ..days import local_now
 from . import jobs
@@ -225,8 +227,12 @@ def due(timestamp: int | None, zone) -> bool:
 @job_app.task(name=jobs.APPLY_RETENTION, queue="light", queueing_lock="apply_retention")
 async def apply_retention_job(timestamp: int | None = None) -> None:
     """Hourly tick (Procrastinate's cron runs in UTC, so the local 03:00 is found here): retry the files of a
-    forgotten range, on or off; then apply retention when the owner turned it on."""
+    forgotten range and sweep the golden copies, on or off; then apply retention when the owner turned it on."""
     ctx = get_context()
     if due(timestamp, ctx.settings.zone):
         await retry_forgotten_files(ctx.pool, ctx.settings)
+        try:
+            await golden.sweep(ctx.pool, ctx.settings.eval_dir)
+        except Exception:  # never stops retention
+            log.exception("could not sweep the golden copies")
         await apply_retention(ctx.pool, ctx.settings)
diff --git a/brain/src/loopi_brain/web/templates/_forget_preview.html b/brain/src/loopi_brain/web/templates/_forget_preview.html
--- a/brain/src/loopi_brain/web/templates/_forget_preview.html
+++ b/brain/src/loopi_brain/web/templates/_forget_preview.html
@@ -1,7 +1,7 @@
 <div class="card">
   <p>Từ {{ first.strftime("%d/%m/%Y %H:%M") }} đến {{ last.strftime("%d/%m/%Y %H:%M") }} sẽ bị xoá vĩnh viễn:
      {{ c.items }} đoạn ghi, {{ c.keyframes }} khung hình, {{ c.transcript_segments }} đoạn transcript,
-     {{ c.moments }} khoảnh khắc, {{ c.commitments }} việc cần làm; {{ c.chapters }} chương mất tóm tắt và
+     {{ c.moments }} khoảnh khắc, {{ c.commitments }} việc cần làm, {{ c.golden }} mẫu đánh giá; {{ c.chapters }} chương mất tóm tắt và
      {{ c.recaps }} recap sẽ được viết lại.</p>
   {% if widened %}<p><strong>Thực tế sẽ xoá từ {{ span_start.strftime("%d/%m/%Y %H:%M:%S") }} đến {{ span_end.strftime("%d/%m/%Y %H:%M:%S") }}</strong>,
      vì có đoạn ghi nằm vắt qua ranh giới của khoảng này và chỉ xoá được trọn vẹn.</p>{% endif %}
diff --git a/brain/src/loopi_brain/web/templates/_forgotten.html b/brain/src/loopi_brain/web/templates/_forgotten.html
--- a/brain/src/loopi_brain/web/templates/_forgotten.html
+++ b/brain/src/loopi_brain/web/templates/_forgotten.html
@@ -1,4 +1,4 @@
 <span>Đã quên khoảng {{ first.strftime("%d/%m/%Y %H:%M") }} – {{ last.strftime("%d/%m/%Y %H:%M") }}:
 {{ c.items }} đoạn ghi, {{ c.keyframes }} khung hình, {{ c.transcript_segments }} đoạn transcript, {{ c.moments }} khoảnh khắc,
-{{ c.commitments }} việc cần làm, {{ c.files }} file{% if c.files_failed %} <span class="err">({{ c.files_failed }} file không xoá được, xem log)</span>{% endif %}.
+{{ c.commitments }} việc cần làm, {{ c.golden }} mẫu đánh giá, {{ c.files }} file{% if c.files_failed %} <span class="err">({{ c.files_failed }} file không xoá được, xem log)</span>{% endif %}.
 Chương và recap của những ngày này sẽ được làm lại.</span>
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_eval_golden.py tests/test_forget.py tests/test_retention.py -q`
Expected: `81 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `737 passed, 1 skipped`, `TOTAL … 99%` (`forget.py`, `pipeline/retention.py` 100 %; `evaluation/golden.py` 99 %: `sample_exists`' guard is exercised by Task 7's page test).

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/evaluation/golden.py brain/src/loopi_brain/forget.py \
  brain/src/loopi_brain/pipeline/retention.py brain/src/loopi_brain/web/templates/_forget_preview.html \
  brain/src/loopi_brain/web/templates/_forgotten.html brain/tests/helpers.py brain/tests/test_eval_golden.py
git commit -F - <<'EOF'
feat(brain): golden clips and keyframes with their copies, labels and scores; forget and retention reach them

A golden clip is a byte copy of a whole recording (and a playback copy), a golden keyframe a copy of its JPEG with
the owner's score per caption model; both outlive retention (D20). "Quên khoảng thời gian" deletes the samples of
its range and sweeps their copies (D21).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 5: The owner's questions, their correct moments and the labelling search

**Files:**
- Create: `brain/src/loopi_brain/evaluation/questions.py`
- Modify: `brain/src/loopi_brain/days.py` (`range_utc`), `brain/src/loopi_brain/web/search_routes.py` (imports it), `brain/src/loopi_brain/search.py` (`candidates`)
- Modify: `brain/tests/test_days.py`
- Test: `brain/tests/test_eval_questions.py` (new)

**Interfaces:**
- Consumes: P2b `search.search_moments(pool, router, query, *, start, end, limit, max_distance, min_similarity)`, `search.Hit(id, kind, ts_start, text, lang, keyframe_id, item_id, item_meta, distance, similarity, score)`, `search.clean_query`; Task 4's `golden.clean_text`.
- Produces:
  - `days.range_utc(first, last, zone) -> (start | None, end | None)` (moved from `web/search_routes.py`, still importable there).
  - `search.search_moments(…, candidates: int = CANDIDATES)`: each of the two searches keeps `candidates` rows before the merge.
  - `loopi_brain.evaluation.questions`: `QUESTION_TARGET = 25`, `TEXT_MAX = 500`, `LABEL_LIMIT = 50`, `LABEL_CANDIDATES = 100`, `LABEL_MAX_DISTANCE = 1.0`, `LABEL_MIN_SIMILARITY = 0.1`, `BROWSE_LIMIT = 200`, `SPEECH_SLACK = 2 s`; `check_question(text, first, last)`; `create_question(pool, text, first, last) -> str`; `update_question(…) -> bool`; `delete_question(pool, id) -> bool`; `list_questions(pool)` (with `answers` counts); `get_question(pool, id) -> dict | None` (with `answers`, each with the moment's current `text` or None); `add_answer(pool, question_id, moment_id) -> bool`; `remove_answer(…) -> bool`; `label_search(pool, router, query, first, last, zone) -> list[Hit]`; `matches(answer, hit) -> bool`; `ranks(answers, hits) -> list[int]`; `answered(pool) -> list[dict]` (questions with ≥ 1 answer, each with `answers`); `counts(pool) -> {"questions", "answered"}`.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_eval_questions.py`:

```python
import datetime as dt

import pytest

from helpers import FakeOllama, captioned_keyframe, fetch_all, insert_item, insert_moment, paired_device
from loopi_brain import search
from loopi_brain.evaluation import questions
from loopi_brain.providers.config import DEFAULT_CONFIG
from loopi_brain.providers.router import build_router
from loopi_brain.search import Hit

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
DAY = dt.date(2026, 10, 14)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


def _hit(moment_id, kind, ts, item_id=None, keyframe_id=None) -> Hit:
    return Hit(moment_id, kind, ts, "x", None, keyframe_id, item_id, None, None, None, 0.0)


async def test_questions_are_checked_and_kept(pool):
    qid = await questions.create_question(pool, "  Hôm qua mình\nhẹn ai?\x00 ", DAY, DAY)
    [row] = await questions.list_questions(pool)
    assert (row["id"], row["text"], row["first_day"], row["last_day"], row["answers"]) == (
        qid, "Hôm qua mình hẹn ai?", DAY, DAY, 0)
    for text, first, last in ((" \x00 ", None, None), ("Khi nào?", DAY, DAY - dt.timedelta(days=1))):
        with pytest.raises(ValueError):
            await questions.create_question(pool, text, first, last)
    assert len((await questions.get_question(pool, await questions.create_question(pool, "x" * 900, None, None)))
               ["text"]) == questions.TEXT_MAX
    assert await questions.update_question(pool, qid, "Sáng nay mình làm gì?", None, None)
    assert (await questions.get_question(pool, qid))["first_day"] is None
    assert await questions.delete_question(pool, qid) and not await questions.delete_question(pool, qid)
    assert await questions.get_question(pool, qid) is None and not await questions.update_question(pool, qid, "x", None, None)


async def test_a_correct_moment_keeps_what_it_came_from(pool, pki_settings, dev):
    d = dev["device_id"]
    kf = await captioned_keyframe(pool, pki_settings, d, NINE)
    video = (await fetch_all(pool, "SELECT item_id FROM keyframe"))[0]["item_id"]
    visual = await insert_moment(pool, d, kind="visual", ts=NINE, item_id=video, text="A person cooks")
    async with pool.connection() as conn:
        await conn.execute("UPDATE moment SET keyframe_id = %s WHERE id = %s", (kf, visual))
    audio = await insert_item(pool, d, kind="audio", started_at=NINE)
    speech = await insert_moment(pool, d, kind="speech", ts=NINE + dt.timedelta(seconds=5), seconds=4, item_id=audio,
                                 text="Mai đi chợ")
    qid = await questions.create_question(pool, "Ai nấu ăn?", None, None)
    assert await questions.add_answer(pool, qid, visual) and await questions.add_answer(pool, qid, speech)
    assert await questions.add_answer(pool, qid, speech)  # picked twice: still one correct moment
    assert not await questions.add_answer(pool, qid, "01NOSUCHMOMENT000000000000")
    assert not await questions.add_answer(pool, "01NOSUCHQUESTION0000000000", visual)
    answers = (await questions.get_question(pool, qid))["answers"]
    assert [(a["moment_id"], a["kind"], a["item_id"], a["keyframe_id"], a["text"]) for a in answers] == [
        (visual, "visual", video, kf, "A person cooks"), (speech, "speech", audio, None, "Mai đi chợ")]
    assert answers[1]["ts_end"] - answers[1]["ts_start"] == dt.timedelta(seconds=4)
    assert (await questions.counts(pool)) == {"questions": 1, "answered": 1}
    async with pool.connection() as conn:  # the day is reprocessed: the moments go, the answers stay
        await conn.execute("DELETE FROM moment")
    assert [a["text"] for a in (await questions.get_question(pool, qid))["answers"]] == [None, None]
    assert await questions.remove_answer(pool, qid, visual) and not await questions.remove_answer(pool, qid, visual)
    assert [q["id"] for q in await questions.answered(pool)] == [qid]


def test_an_answer_is_recognised_after_its_day_is_reprocessed():
    speech = {"moment_id": "M1", "kind": "speech", "item_id": "I1", "keyframe_id": None,
              "ts_start": NINE + dt.timedelta(seconds=5), "ts_end": NINE + dt.timedelta(seconds=9)}
    picture = {"moment_id": "M2", "kind": "visual", "item_id": "V1", "keyframe_id": "K1", "ts_start": NINE, "ts_end": NINE}
    marker = {"moment_id": "M3", "kind": "marker", "item_id": "K9", "keyframe_id": None, "ts_start": NINE, "ts_end": NINE}
    assert questions.matches(speech, _hit("M1", "speech", NINE))  # the same moment
    assert questions.matches(speech, _hit("N1", "speech", NINE + dt.timedelta(seconds=4), item_id="I1"))
    assert questions.matches(speech, _hit("N1", "speech", NINE + dt.timedelta(seconds=11), item_id="I1"))
    assert not questions.matches(speech, _hit("N1", "speech", NINE + dt.timedelta(seconds=12), item_id="I1"))
    assert not questions.matches(speech, _hit("N2", "speech", NINE + dt.timedelta(seconds=6), item_id="I2"))
    assert questions.matches(picture, _hit("N3", "visual", NINE, item_id="V1", keyframe_id="K1"))
    assert not questions.matches(picture, _hit("N4", "visual", NINE, item_id="V1", keyframe_id="K2"))
    assert questions.matches(marker, _hit("N5", "marker", NINE, item_id="K9"))
    assert not questions.matches(marker, _hit("N6", "speech", NINE, item_id="K9"))
    hits = [_hit("X", "visual", NINE), _hit("N1", "speech", NINE + dt.timedelta(seconds=5), item_id="I1"),
            _hit("Y", "speech", NINE), _hit("N3", "visual", NINE, keyframe_id="K1")]
    assert questions.ranks([speech, picture, marker], hits) == [2, 4]


async def test_the_labelling_search_shows_more_than_the_live_one_and_lets_the_owner_browse(pool, pki_settings, dev):
    d = dev["device_id"]
    router = build_router(DEFAULT_CONFIG, pki_settings, FakeOllama().client())  # the question is the axis-0 vector
    for n in range(60):
        await insert_moment(pool, d, kind="speech", ts=NINE + dt.timedelta(minutes=n), axis=1, text=f"đi chợ lần {n}")
    unrelated = await insert_moment(pool, d, kind="visual", ts=NINE + dt.timedelta(days=1), axis=2, text="A dark room")
    live = await search.search_moments(pool, router, "đi chợ", limit=50)
    assert len(live.hits) == search.CANDIDATES  # the live search keeps 40 per search
    found = await questions.label_search(pool, router, "đi chợ", None, None, pki_settings.zone)
    assert len(found) == questions.LABEL_LIMIT
    next_day = DAY + dt.timedelta(days=1)
    start, end = (NINE + dt.timedelta(hours=15), NINE + dt.timedelta(hours=39))  # that day in Seoul
    assert (await search.search_moments(pool, router, "căn phòng", start=start, end=end)).hits == []  # too far
    assert [h.id for h in await questions.label_search(pool, router, "căn phòng", next_day, next_day,
                                                       pki_settings.zone)] == [unrelated]  # meaning, loosely
    day = await questions.label_search(pool, router, "", DAY, DAY, pki_settings.zone)
    assert len(day) == 60 and [h.ts_start for h in day] == sorted(h.ts_start for h in day)
    assert [h.id for h in await questions.label_search(pool, router, " ", next_day, None,
                                                       pki_settings.zone)] == [unrelated]
    assert await questions.label_search(pool, router, "", None, None, pki_settings.zone) == []
    assert len(await questions.label_search(pool, None, "đi chợ", DAY, DAY, pki_settings.zone)) == 50  # words only
```

Apply to `brain/tests/test_days.py`:

```diff
diff --git a/brain/tests/test_days.py b/brain/tests/test_days.py
--- a/brain/tests/test_days.py
+++ b/brain/tests/test_days.py
@@ -1,7 +1,7 @@
 import datetime as dt
 from zoneinfo import ZoneInfo
 
-from loopi_brain.days import day_bounds_utc, hm, local_day, local_now
+from loopi_brain.days import day_bounds_utc, hm, local_day, local_now, range_utc
 
 SEOUL = ZoneInfo("Asia/Seoul")
 
@@ -28,3 +28,11 @@ def test_local_day_and_now_follow_the_zone():
 def test_hm_is_the_local_clock_time_zero_padded():
     assert hm(dt.datetime(2026, 10, 14, 23, 5, tzinfo=dt.UTC), SEOUL) == "08:05"
     assert hm(dt.datetime(2026, 10, 15, 6, 45, tzinfo=dt.UTC), ZoneInfo("Europe/Berlin")) == "08:45"
+
+
+def test_a_range_of_local_days_in_utc():
+    day = dt.date(2026, 10, 15)
+    assert range_utc(day, day, SEOUL) == day_bounds_utc(day, SEOUL)
+    assert range_utc(None, day, SEOUL) == (None, dt.datetime(2026, 10, 15, 15, 0, tzinfo=dt.UTC))
+    assert range_utc(day, None, SEOUL) == (dt.datetime(2026, 10, 14, 15, 0, tzinfo=dt.UTC), None)
+    assert range_utc(None, None, SEOUL) == (None, None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_eval_questions.py tests/test_days.py -q`
Expected: FAIL — `2 errors` during collection: `ImportError: cannot import name 'questions' from 'loopi_brain.evaluation'` and `ImportError: cannot import name 'range_utc' from 'loopi_brain.days'`.

- [ ] **Step 3: Move `range_utc` to `days.py` and let the search keep more candidates**

```diff
diff --git a/brain/src/loopi_brain/days.py b/brain/src/loopi_brain/days.py
--- a/brain/src/loopi_brain/days.py
+++ b/brain/src/loopi_brain/days.py
@@ -13,6 +13,15 @@ def day_bounds_utc(day: dt.date, zone: ZoneInfo) -> tuple[dt.datetime, dt.dateti
     return start.astimezone(dt.UTC), end.astimezone(dt.UTC)
 
 
+def range_utc(
+    first: dt.date | None, last: dt.date | None, zone: ZoneInfo
+) -> tuple[dt.datetime | None, dt.datetime | None]:
+    """Local days [first, last] (either optional) as a UTC range [start, end)."""
+    start = day_bounds_utc(first, zone)[0] if first else None
+    end = day_bounds_utc(last, zone)[1] if last else None
+    return start, end
+
+
 def local_day(ts: dt.datetime, zone: ZoneInfo) -> dt.date:
     """The local day an aware timestamp falls on."""
     return ts.astimezone(zone).date()
diff --git a/brain/src/loopi_brain/search.py b/brain/src/loopi_brain/search.py
--- a/brain/src/loopi_brain/search.py
+++ b/brain/src/loopi_brain/search.py
@@ -112,12 +112,14 @@ async def search_moments(
     limit: int = 8,
     max_distance: float = 0.6,
     min_similarity: float = 0.3,
+    candidates: int = CANDIDATES,
 ) -> SearchResult:
-    """The ``limit`` most relevant moments for ``query`` with ts_start in [start, end) (either bound optional)."""
+    """The ``limit`` most relevant moments for ``query`` with ts_start in [start, end) (either bound optional);
+    each of the two searches keeps its ``candidates`` best before they are merged."""
     query = clean_query(query)
     if not query:
         return SearchResult([], semantic=router is not None)
-    params = {"start": start, "end": end, "limit": CANDIDATES}
+    params = {"start": start, "end": end, "limit": candidates}
     meaning = await _by_meaning(pool, router, query, params, max_distance) if router is not None else None
     words = await _by_words(pool, query, params, min_similarity)
     rows: dict[str, dict] = {}
diff --git a/brain/src/loopi_brain/web/search_routes.py b/brain/src/loopi_brain/web/search_routes.py
--- a/brain/src/loopi_brain/web/search_routes.py
+++ b/brain/src/loopi_brain/web/search_routes.py
@@ -13,7 +13,7 @@ from markupsafe import Markup, escape
 from pydantic import BeforeValidator
 
 from .. import chat, search
-from ..days import day_bounds_utc
+from ..days import range_utc
 from ..pipeline import moments as moment_rules
 from .routes import MARKER_LABEL, TEMPLATES, require_htmx
 
@@ -52,13 +52,6 @@ def hit_view(hit: search.Hit, zone) -> dict:
     }
 
 
-def range_utc(first: dt.date | None, last: dt.date | None, zone) -> tuple[dt.datetime | None, dt.datetime | None]:
-    """Local days [first, last] (either optional) as a UTC range [start, end)."""
-    start = day_bounds_utc(first, zone)[0] if first else None
-    end = day_bounds_utc(last, zone)[1] if last else None
-    return start, end
-
-
 @router.get("/search", response_class=HTMLResponse)
 async def search_page(
     request: Request,
```

- [ ] **Step 4: Write the questions module**

Create `brain/src/loopi_brain/evaluation/questions.py`:

```python
"""The owner's questions and their correct moments (spec §13: 25 questions with the right moments, recall@5).

The owner writes a question in Vietnamese, optionally with the local days it is about (the search page's filter:
"hôm qua" means the day the question was written about, whenever it is evaluated), and picks its correct moments
from a labelling search that shows more than the live one: hybrid search with looser thresholds and a longer list,
or every moment of the chosen days in time order — so the truth does not depend on what retrieval finds today.

A correct moment is kept with what it came from (keyframe, recording and time, marker item): reprocessing a day
gives its moments new ids, and the runner still recognises them (``matches``).
"""
from __future__ import annotations

import datetime as dt

from psycopg_pool import AsyncConnectionPool

from .. import devices, search
from ..days import range_utc
from ..providers.router import Router
from ..search import Hit
from .golden import clean_text

QUESTION_TARGET = 25
TEXT_MAX = 500  # characters, as the search page
LABEL_LIMIT = 50
LABEL_CANDIDATES = 100
LABEL_MAX_DISTANCE = 1.0  # every moment whose meaning is not opposite to the question's
LABEL_MIN_SIMILARITY = 0.1
BROWSE_LIMIT = 200
# A speech region transcribed again starts a little earlier or later: its moment still answers the question if it
# comes from the same recording and starts within the answer's span, give or take this much.
SPEECH_SLACK = dt.timedelta(seconds=2)


def check_question(
    text: str, first: dt.date | None, last: dt.date | None
) -> tuple[str, dt.date | None, dt.date | None]:
    """A question as it is kept; ValueError when it is empty or its days run backwards."""
    text = clean_text(text, TEXT_MAX)
    if not text:
        raise ValueError("a question needs words")
    if first and last and first > last:
        raise ValueError("the first day must not be after the last")
    return text, first, last


async def create_question(pool: AsyncConnectionPool, text: str, first: dt.date | None, last: dt.date | None) -> str:
    text, first, last = check_question(text, first, last)
    question_id = devices.new_id()
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO eval_question (id, text, first_day, last_day) VALUES (%s, %s, %s, %s)",
                           (question_id, text, first, last))
    return question_id


async def update_question(
    pool: AsyncConnectionPool, question_id: str, text: str, first: dt.date | None, last: dt.date | None
) -> bool:
    text, first, last = check_question(text, first, last)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE eval_question SET text = %s, first_day = %s, last_day = %s, updated_at = now() WHERE id = %s",
            (text, first, last, question_id),
        )
        return cur.rowcount == 1


async def delete_question(pool: AsyncConnectionPool, question_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM eval_question WHERE id = %s", (question_id,))
        return cur.rowcount == 1


async def list_questions(pool: AsyncConnectionPool) -> list[dict]:
    """Every question, oldest first, with how many correct moments it has."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT q.*, (SELECT count(*) FROM eval_answer a WHERE a.question_id = q.id) AS answers"
            " FROM eval_question q ORDER BY q.created_at, q.id"
        )
        return await cur.fetchall()


async def get_question(pool: AsyncConnectionPool, question_id: str) -> dict | None:
    """A question with its correct moments in time order (each with the text of its moment while it exists)."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM eval_question WHERE id = %s", (question_id,))
        question = await cur.fetchone()
        if question is None:
            return None
        cur = await conn.execute(
            "SELECT a.*, m.text FROM eval_answer a LEFT JOIN moment m ON m.id = a.moment_id"
            " WHERE a.question_id = %s ORDER BY a.ts_start, a.moment_id",
            (question_id,),
        )
        return {**question, "answers": await cur.fetchall()}


async def add_answer(pool: AsyncConnectionPool, question_id: str, moment_id: str) -> bool:
    """Make ``moment_id`` a correct moment of the question (again is fine); False when either is gone."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT 1 FROM eval_question WHERE id = %s FOR SHARE", (question_id,))
        if await cur.fetchone() is None:
            return False
        cur = await conn.execute(
            "INSERT INTO eval_answer (question_id, moment_id, kind, item_id, keyframe_id, ts_start, ts_end)"
            " SELECT %s, id, kind, item_id, keyframe_id, ts_start, ts_end FROM moment WHERE id = %s"
            " ON CONFLICT (question_id, moment_id) DO NOTHING RETURNING moment_id",
            (question_id, moment_id),
        )
        if await cur.fetchone() is not None:
            return True
        cur = await conn.execute("SELECT 1 FROM eval_answer WHERE question_id = %s AND moment_id = %s",
                                 (question_id, moment_id))
        return await cur.fetchone() is not None


async def remove_answer(pool: AsyncConnectionPool, question_id: str, moment_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM eval_answer WHERE question_id = %s AND moment_id = %s",
                                 (question_id, moment_id))
        return cur.rowcount == 1


async def label_search(
    pool: AsyncConnectionPool, router: Router | None, query: str, first: dt.date | None, last: dt.date | None, zone
) -> list[Hit]:
    """What the owner picks correct moments from: with words, the hybrid search with looser thresholds and up to
    LABEL_LIMIT results; without words but with days, every moment of those days in time order (BROWSE_LIMIT)."""
    start, end = range_utc(first, last, zone)
    if search.clean_query(query):
        result = await search.search_moments(
            pool, router, query, start=start, end=end, limit=LABEL_LIMIT, max_distance=LABEL_MAX_DISTANCE,
            min_similarity=LABEL_MIN_SIMILARITY, candidates=LABEL_CANDIDATES,
        )
        return result.hits
    if start is None and end is None:
        return []
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT m.id, m.kind, m.ts_start, m.text, m.lang, m.keyframe_id, m.item_id, i.meta AS item_meta"
            " FROM moment m LEFT JOIN item i ON i.id = m.item_id"
            " WHERE (%(start)s::timestamptz IS NULL OR m.ts_start >= %(start)s)"
            " AND (%(end)s::timestamptz IS NULL OR m.ts_start < %(end)s) ORDER BY m.ts_start, m.id LIMIT %(limit)s",
            {"start": start, "end": end, "limit": BROWSE_LIMIT},
        )
        rows = await cur.fetchall()
    return [Hit(**row, distance=None, similarity=None, score=0.0) for row in rows]


def matches(answer: dict, hit: Hit) -> bool:
    """Is ``hit`` the correct moment ``answer`` — the same moment, or the one its source has after a reprocess?"""
    if hit.id == answer["moment_id"]:
        return True
    if hit.kind != answer["kind"]:
        return False
    if answer["kind"] == "visual":
        return answer["keyframe_id"] is not None and hit.keyframe_id == answer["keyframe_id"]
    if answer["kind"] == "marker":
        return answer["item_id"] is not None and hit.item_id == answer["item_id"]
    return (answer["item_id"] is not None and hit.item_id == answer["item_id"]
            and answer["ts_start"] - SPEECH_SLACK <= hit.ts_start <= answer["ts_end"] + SPEECH_SLACK)


def ranks(answers: list[dict], hits: list[Hit]) -> list[int]:
    """For each correct moment found among ``hits``, the rank (from 1) of the first hit that is it."""
    found = []
    for answer in answers:
        rank = next((n for n, hit in enumerate(hits, start=1) if matches(answer, hit)), None)
        if rank is not None:
            found.append(rank)
    return sorted(found)


async def answered(pool: AsyncConnectionPool) -> list[dict]:
    """The questions that have at least one correct moment, each with its answers (what the runner scores)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT q.id, q.text, q.first_day, q.last_day FROM eval_question q"
            " WHERE EXISTS (SELECT 1 FROM eval_answer a WHERE a.question_id = q.id) ORDER BY q.created_at, q.id"
        )
        rows = await cur.fetchall()
        cur = await conn.execute("SELECT * FROM eval_answer ORDER BY ts_start")
        answers = await cur.fetchall()
    by_question: dict[str, list[dict]] = {}
    for answer in answers:
        by_question.setdefault(answer["question_id"], []).append(answer)
    return [{**row, "answers": by_question[row["id"]]} for row in rows]


async def counts(pool: AsyncConnectionPool) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS questions, count(*) FILTER (WHERE EXISTS (SELECT 1 FROM eval_answer a"
            " WHERE a.question_id = q.id)) AS answered FROM eval_question q"
        )
        return await cur.fetchone()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_eval_questions.py tests/test_days.py tests/test_search.py -q`
Expected: `29 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `742 passed, 1 skipped`, `TOTAL … 99%`.

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/evaluation/questions.py brain/src/loopi_brain/days.py brain/src/loopi_brain/search.py \
  brain/src/loopi_brain/web/search_routes.py brain/tests/test_days.py brain/tests/test_eval_questions.py
git commit -F - <<'EOF'
feat(brain): the owner's questions and their correct moments, with a labelling search

A correct moment keeps its source (keyframe, recording and time, marker item) so a reprocessed day still matches;
the labelling search is wider than the live one, or lists a day's moments in time order.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 6: Evaluation runs and `loopi-brain eval`

**Files:**
- Create: `brain/src/loopi_brain/evaluation/runner.py`
- Modify: `brain/src/loopi_brain/pipeline/jobs.py` (`RUN_EVAL`, task module), `brain/src/loopi_brain/pipeline/status.py` (label), `brain/src/loopi_brain/cli.py` (`eval run`, `eval show`)
- Test: `brain/tests/test_eval_runner.py` (new)

**Interfaces:**
- Consumes: Task 2 metrics; Task 3 `asr.transcribe_file(path, transcriber, max_gain_db, vad_threshold)`, `asr.kept_segments`, `asr.RegionResult`, `asr.SegmentResult`, `ASR_RULES`; Task 4 golden clips (`eval_audio`), `eval_caption_score`; Task 5 `questions.answered`, `questions.ranks`, `days.range_utc`, `search.search_moments`; P2b `jobs.enqueue(conn, task, *, queue, queueing_lock, lock, priority, **kwargs)`, `state.error_text`, `PipelineContext(pool, settings, router, transcriber)`, `router.health()`, `router.task_ready("embed", health)`, `router.model_version(task)`, `media.inside`.
- Produces (`loopi_brain.evaluation.runner`):
  - `EVAL_PRIORITY = -20`, `WER_LIMIT = 0.30`, `RECALL_TARGET = 0.70`, `PRIMARY = {"vi": "wer", "en": "wer", "ko": "cer"}`, `DISTANCES = (0.5, 0.6, 0.7, 0.8)`, `SIMILARITIES = (0.2, 0.3, 0.4)`, `VARIANTS = ("live", "none", "outro", "lone_phrase", "short_foreign", "all")`; `jobs.RUN_EVAL = "run_eval"` (queue `light`, lock `asr`, priority −20, queueing lock `eval:<run id>`).
  - `start_run(pool, settings, *, max_gain_db=None, vad_threshold=None) -> (run_id, created)` (ValueError outside 0–60 dB / 0–1; one run at a time; a run without a live job is marked failed first); `run_step(ctx, run_id) -> "clip" | "done" | "failed" | "nothing"`; `latest_run(pool, run_id=None, finished=False) -> dict | None` and `list_runs(pool, limit=20)` (rows with `heard` and `clips` counts); `format_run(run, zone) -> str` (Markdown, numbers only); `pct(value) -> str`; `rule_sets(live) -> dict`; `hypothesis(regions, rules) -> str`; `asr_numbers(clips, rules)`, `caption_numbers(pool)`, `recall_numbers(pool, router, config, zone)`.
  - `eval_run.config`: `max_distance`, `min_similarity`, `overrides` (from the requester), then — when the worker starts the run — `max_gain_db`, `vad_threshold`, `rules`, `models` (`asr`, `caption`, `embed`).
  - `eval_run.results`: `{"asr": {"clips", "failed", "variants": {name: {"rules", "languages": {lang: {"clips", "words", "chars", "wer", "cer", "metric", "score", "over_limit"}}, "no_speech": {"clips", "invented", "words", "rate"}}}}, "captions": {model: {"scored", "mean", "good", "wrong_people", "invented_objects", "wrong_activity"}}, "recall": {"questions", "at5", "at10", "hit5", "mrr", "semantic", "meets_target", "per_question": [{"id", "relevant", "ranks"}], "grid": [{"max_distance", "min_similarity", "at5"}]} | {"questions", "error"} | {"questions": 0}}`.
  - CLI: `loopi-brain eval run [--max-gain-db X] [--vad-threshold Y] [--no-wait] [--timeout S]` (prints the run id, waits, prints `format_run`; exit 0 when done, 1 failed or timed out, 2 bad value), `loopi-brain eval show [RUN_ID]`; `cli.EVAL_POLL_S = 10.0`.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_eval_runner.py`:

```python
import datetime as dt
import logging
import threading
import time

import psycopg
import pytest

from helpers import (
    FakeOllama,
    FakeTranscriber,
    analysed_audio,
    captioned_keyframe,
    fetch_all,
    fetch_one,
    insert_item,
    insert_moment,
    make_ctx,
    make_opus,
    paired_device,
    run_jobs,
)
from loopi_brain import cli
from loopi_brain.evaluation import golden, questions, runner
from loopi_brain.pipeline import asr
from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
SECRET = "Mai mình đi chợ nhé"


def seg(text, start=0.0, end=2.0) -> SegmentResult:
    return SegmentResult(start, end, text, -0.3, 0.05, 1.2)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def one_region(monkeypatch):
    """Every clip has one speech region (the real Silero VAD finds none in a synthetic file); records thresholds."""
    thresholds = []

    def regions(_audio, threshold):
        thresholds.append(threshold)
        return [Region(0.0, 2.0)]

    monkeypatch.setattr(asr, "speech_regions", regions)
    return thresholds


async def _clip(pool, settings, device_id, minutes, label) -> str:
    item = await analysed_audio(pool, settings, device_id, NINE + dt.timedelta(minutes=minutes))
    return await golden.add_audio(pool, settings, item, label)


async def _golden_clips(pool, settings, device_id) -> None:
    """A Vietnamese clip, a Korean one and one without speech, created in that order (the order they are heard)."""
    await _clip(pool, settings, device_id, 0, golden.AudioLabel("vi", False, SECRET))
    await _clip(pool, settings, device_id, 1, golden.AudioLabel("ko", False, "네 알겠어요"))
    await _clip(pool, settings, device_id, 2, golden.AudioLabel(None, True, ""))


HEARD = (
    RegionResult("vi", 0.9, [seg("Mai mình đi chợ")]),  # one word missed: WER 1/5
    RegionResult("ko", 0.9, [seg("네 알겠어요")]),  # exact: CER 0
    RegionResult("en", 0.3, [seg("Thank you.")]),  # invented on silence
)


async def _run(pool, settings, transcriber, ollama=None, **overrides) -> dict:
    ctx = make_ctx(pool, settings, ollama=ollama or FakeOllama(), transcriber=transcriber)
    run_id, created = await runner.start_run(pool, settings, **overrides)
    assert created
    await run_jobs(ctx)
    return await runner.latest_run(pool, run_id)


async def test_one_transcription_scores_every_rule_set(pool, pki_settings, dev, jobs_open, one_region, caplog):
    await _golden_clips(pool, pki_settings, dev["device_id"])
    fake = FakeTranscriber(*HEARD)
    with caplog.at_level(logging.INFO):
        run = await _run(pool, pki_settings, fake)
    assert run["status"] == "done" and len(fake.chunks) == 3  # each clip heard once, whatever the rule sets
    audio = run["results"]["asr"]
    assert (audio["clips"], audio["failed"]) == (3, 0)
    assert set(audio["variants"]) == {"live", "none", "outro", "lone_phrase", "short_foreign", "all"}
    live = audio["variants"]["live"]
    assert live["rules"] == [] and live["languages"]["vi"]["score"] == pytest.approx(0.2)
    assert live["languages"]["vi"]["metric"] == "wer" and live["languages"]["vi"]["words"] == 5
    assert live["languages"]["ko"]["metric"] == "cer" and live["languages"]["ko"]["score"] == 0.0
    assert live["languages"]["vi"]["over_limit"] is False
    assert live["no_speech"] == {"clips": 1, "invented": 1, "words": 2, "rate": 1.0}
    for name in ("lone_phrase", "short_foreign", "all"):
        assert audio["variants"][name]["no_speech"]["invented"] == 0, name
        assert audio["variants"][name]["languages"]["vi"]["score"] == pytest.approx(0.2), name  # real speech kept
    assert audio["variants"]["outro"]["no_speech"]["invented"] == 1
    assert run["config"]["models"] == {"asr": "local:faster-whisper/large-v3-turbo-int8",
                                       "caption": "ollama:minicpm-v4.6:1b", "embed": "ollama:qwen3-embedding:0.6b"}
    jobs = await fetch_all(pool, "SELECT task_name, status, lock, priority, queue_name FROM procrastinate_jobs")
    assert len(jobs) == 4  # one job per clip, then the numbers
    assert {(j["task_name"], j["status"], j["lock"], j["priority"], j["queue_name"]) for j in jobs} == {
        ("run_eval", "succeeded", "asr", runner.EVAL_PRIORITY, "light")}
    heard = await fetch_all(pool, "SELECT segments FROM eval_run_audio ORDER BY sample_id")
    assert heard[2]["segments"][0]["lang"] == "en" and heard[2]["segments"][0]["lang_prob"] == pytest.approx(0.3)
    assert SECRET not in caplog.text and "Thank you" not in caplog.text
    report = runner.format_run(run, pki_settings.zone)
    assert "| live (none) | 20.0 % (1 clips) | 0.0 % (1 clips) | — | 1/1 (2 words) |" in report
    rows = [line.split(" |")[0] for line in report.splitlines() if line.startswith("| ") and "WER" not in line]
    assert rows == ["| live (none)", "| none", "| outro", "| lone_phrase", "| short_foreign", "| all"]
    assert "| all | 20.0 % (1 clips) | 0.0 % (1 clips) | — | 0/1 (0 words) |" in report
    assert SECRET not in report and "Thank you" not in report  # numbers only


async def test_a_waiting_evaluation_never_goes_ahead_of_a_live_transcription(pool, pki_settings, dev, jobs_open,
                                                                              one_region):
    from loopi_brain.pipeline import dispatch

    await _golden_clips(pool, pki_settings, dev["device_id"])
    await runner.start_run(pool, pki_settings)  # its first step waits: lock "asr", priority -20
    recorded = await insert_item(pool, dev["device_id"], kind="audio", started_at=NINE + dt.timedelta(hours=1),
                                 seconds=5)
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (recorded,)))["raw_path"]
    make_opus(pki_settings.media_root / raw, seconds=5)
    assert await dispatch.defer_item(pool, recorded) is not None  # a live recording arrives after the request
    await run_jobs(make_ctx(pool, pki_settings, transcriber=FakeTranscriber(*HEARD)))
    order = await fetch_all(pool, "SELECT j.task_name FROM procrastinate_events e JOIN procrastinate_jobs j"
                                  " ON j.id = e.job_id WHERE e.type = 'started' ORDER BY e.id")
    # The live transcription and the embedding of its moment run first, then the four evaluation jobs.
    assert [r["task_name"] for r in order] == ["transcribe_audio", "embed_moments"] + ["run_eval"] * 4


async def test_a_language_above_30_percent_is_flagged(pool, pki_settings, dev, jobs_open, one_region):
    await _clip(pool, pki_settings, dev["device_id"], 0, golden.AudioLabel("vi", False, SECRET))
    run = await _run(pool, pki_settings, FakeTranscriber(RegionResult("vi", 0.9, [seg("Mai đi")])))
    vi = run["results"]["asr"]["variants"]["live"]["languages"]["vi"]
    assert vi["score"] == pytest.approx(0.6) and vi["over_limit"] is True
    assert "60.0 % (1 clips) > 30 %" in runner.format_run(run, pki_settings.zone)


async def test_the_live_rules_and_the_overrides_are_what_the_run_hears_with(pool, pki_settings, dev, jobs_open,
                                                                             one_region):
    await _golden_clips(pool, pki_settings, dev["device_id"])
    worker = pki_settings.model_copy(update={"asr_rules": "lone_phrase", "asr_vad_threshold": 0.45})
    ctx = make_ctx(pool, worker, transcriber=FakeTranscriber(*HEARD))  # the api's settings have no rule on
    run_id, _ = await runner.start_run(pool, pki_settings, max_gain_db=12.0, vad_threshold=0.3)
    assert "not started yet" in runner.format_run(await runner.latest_run(pool, run_id), pki_settings.zone)
    await run_jobs(ctx)
    run = await runner.latest_run(pool, run_id)
    assert one_region == [0.3, 0.3, 0.3]
    assert run["config"]["max_gain_db"] == 12.0 and run["config"]["overrides"] == {"max_gain_db": 12.0,
                                                                                   "vad_threshold": 0.3}
    live = run["results"]["asr"]["variants"]["live"]
    assert live["rules"] == ["lone_phrase"] and live["no_speech"]["invented"] == 0  # the worker's live rules
    run_id, _ = await runner.start_run(pool, pki_settings)
    await run_jobs(ctx)
    plain = await runner.latest_run(pool, run_id)
    assert (plain["config"]["max_gain_db"], plain["config"]["vad_threshold"]) == (30.0, 0.45)
    assert one_region[3:] == [0.45, 0.45, 0.45]
    for bad in ({"max_gain_db": 61.0}, {"vad_threshold": 1.0}):
        with pytest.raises(ValueError):
            await runner.start_run(pool, pki_settings, **bad)


async def test_a_clip_whisper_cannot_read_is_counted_and_the_run_goes_on(pool, pki_settings, dev, jobs_open,
                                                                         one_region):
    await _golden_clips(pool, pki_settings, dev["device_id"])
    [first] = await fetch_all(pool, "SELECT id, path FROM eval_audio ORDER BY id LIMIT 1")
    (pki_settings.eval_dir / first["path"]).write_bytes(b"OggS-not-audio")
    run = await _run(pool, pki_settings, FakeTranscriber(*HEARD[1:]))
    assert run["status"] == "done" and run["results"]["asr"]["failed"] == 1
    row = await fetch_one(pool, "SELECT segments, error FROM eval_run_audio WHERE sample_id = %s", (first["id"],))
    assert row["segments"] is None and row["error"]
    assert "vi" not in run["results"]["asr"]["variants"]["live"]["languages"]


async def test_captions_are_summed_per_caption_model(pool, pki_settings, dev, jobs_open):
    d = dev["device_id"]
    for minutes, score, flags in ((0, 5, set()), (60, 2, {"wrong_people", "invented_objects"}), (120, 4, set())):
        kf = await captioned_keyframe(pool, pki_settings, d, NINE + dt.timedelta(minutes=minutes))
        await golden.add_caption(pool, pki_settings, kf, score, flags)
    run = await _run(pool, pki_settings, FakeTranscriber())
    assert run["results"]["captions"] == {"ollama:minicpm-v4.6:1b": {
        "scored": 3, "mean": pytest.approx(11 / 3), "good": 2, "wrong_people": 1, "invented_objects": 1,
        "wrong_activity": 0}}
    assert run["results"]["asr"]["clips"] == 0
    assert "- ollama:minicpm-v4.6:1b: 3 scored, mean 3.67, ≥ 4: 2;" in runner.format_run(run, pki_settings.zone)


async def test_recall_is_measured_through_the_hybrid_search(pool, pki_settings, dev, jobs_open):
    d = dev["device_id"]
    # FakeOllama embeds every question as the axis-0 vector: moments on axis 0 mean the same, axis 3 is unrelated.
    market = await insert_moment(pool, d, kind="speech", ts=NINE, axis=0, text="Mai mình đi chợ")
    room = await insert_moment(pool, d, kind="visual", ts=NINE + dt.timedelta(hours=1), axis=3, text="A dark room")
    found = await questions.create_question(pool, "Khi nào đi chợ?", None, None)
    await questions.add_answer(pool, found, market)
    missed = await questions.create_question(pool, "Phòng tối lúc nào?", None, None)
    await questions.add_answer(pool, missed, room)
    elsewhere = await questions.create_question(pool, "Đi chợ hôm sau?", dt.date(2026, 10, 15), dt.date(2026, 10, 15))
    await questions.add_answer(pool, elsewhere, market)  # the question's days exclude its answer: not found
    await questions.create_question(pool, "Chưa có câu trả lời", None, None)  # not scored
    run = await _run(pool, pki_settings, FakeTranscriber())
    recall = run["results"]["recall"]
    assert recall["questions"] == 3 and recall["semantic"] is True
    assert (recall["at5"], recall["at10"], recall["hit5"], recall["mrr"]) == pytest.approx((1 / 3, 1 / 3, 1 / 3, 1 / 3))
    assert recall["meets_target"] is False
    assert [q["ranks"] for q in recall["per_question"]] == [[1], [], []]
    assert len(recall["grid"]) == len(runner.DISTANCES) * len(runner.SIMILARITIES)
    report = runner.format_run(run, pki_settings.zone)
    assert "Recall (3 questions, max distance 0.6, min similarity 0.3): @5 33.3 % (target 70 %: not met)" in report
    assert "đi chợ" not in report.lower()


async def test_recall_waits_for_the_embedding_model(pool, pki_settings, dev, jobs_open):
    market = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, axis=0, text="Mai mình đi chợ")
    await questions.add_answer(pool, await questions.create_question(pool, "Đi chợ?", None, None), market)
    ollama = FakeOllama()
    ollama.down = True
    run = await _run(pool, pki_settings, FakeTranscriber(), ollama=ollama)
    assert run["status"] == "done"
    assert run["results"]["recall"] == {"questions": 1, "error": "the embedding model is not available"}
    assert "Recall: not measured (the embedding model is not available)" in runner.format_run(run, pki_settings.zone)


async def test_one_run_at_a_time_and_a_run_whose_job_is_gone_does_not_block_the_next(pool, pki_settings, jobs_open):
    first, created = await runner.start_run(pool, pki_settings)
    assert created and await runner.start_run(pool, pki_settings) == (first, False)
    async with pool.connection() as conn:  # the worker died and the job was dead-lettered and cleared
        await conn.execute("DELETE FROM procrastinate_jobs")
    second, created = await runner.start_run(pool, pki_settings)
    assert created and second != first
    stopped = await runner.latest_run(pool, first)
    assert stopped["status"] == "failed" and "start a new one" in stopped["error"]
    assert "Progress: 0/0 clips; error: the run stopped" in runner.format_run(stopped, pki_settings.zone)
    assert [r["id"] for r in await runner.list_runs(pool)] == [second, first]


async def test_an_unexpected_error_fails_the_run_without_a_retry(pool, pki_settings, jobs_open, monkeypatch):
    async def broken(pool):
        raise RuntimeError("database went away")

    monkeypatch.setattr(runner.questions, "answered", broken)
    run = await _run(pool, pki_settings, FakeTranscriber())
    assert run["status"] == "failed" and run["error"] == "RuntimeError: database went away"
    jobs = await fetch_all(pool, "SELECT status FROM procrastinate_jobs")
    assert [j["status"] for j in jobs] == ["succeeded"]
    ctx = make_ctx(pool, pki_settings, transcriber=FakeTranscriber())
    assert await runner.run_step(ctx, run["id"]) == "nothing"  # a finished run is never resumed


def test_the_cli_starts_a_run_waits_and_shows_it(database_url, monkeypatch, capsys):
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    monkeypatch.setattr(cli, "EVAL_POLL_S", 0.05)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE eval_run CASCADE")
        conn.execute("DELETE FROM procrastinate_jobs")
    assert cli.main(["eval", "show"]) == 1 and "no evaluation run yet" in capsys.readouterr().out
    assert cli.main(["eval", "run", "--no-wait"]) == 0
    run_id = capsys.readouterr().out.split(": ")[1].strip()
    assert cli.main(["eval", "run", "--timeout", "0"]) == 1  # no worker here: it is still waiting
    out = capsys.readouterr().out
    assert f"already waiting or running: {run_id}" in out and f"see `loopi-brain eval show {run_id}`" in out

    def finish_soon():  # the worker, elsewhere, gives up on the run
        time.sleep(0.3)
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute("UPDATE eval_run SET status = 'failed', error = 'RuntimeError: x' WHERE id = %s", (run_id,))

    worker = threading.Thread(target=finish_soon)
    worker.start()
    try:
        assert cli.main(["eval", "run"]) == 1
    finally:
        worker.join()
    assert "— failed" in capsys.readouterr().out
    assert cli.main(["eval", "show", run_id]) == 0 and "error: RuntimeError: x" in capsys.readouterr().out
    assert cli.main(["eval", "show", "01NOSUCHRUN000000000000000"]) == 1
    assert cli.main(["eval", "run", "--vad-threshold", "2"]) == 2
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE eval_run CASCADE")
        conn.execute("DELETE FROM procrastinate_jobs")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_eval_runner.py -q`
Expected: FAIL — `1 error` during collection: `ImportError: cannot import name 'runner' from 'loopi_brain.evaluation'`.

- [ ] **Step 3: Write the runner**

Create `brain/src/loopi_brain/evaluation/runner.py`:

```python
"""Evaluation runs (spec §13): the owner's golden set against the pipeline as it is configured now.

A run transcribes every golden clip again through the live ASR path — level normalisation (D19), Silero VAD, Whisper
and the P2a filters, with the run's gain ceiling and VAD threshold — and keeps every Whisper segment before the
switchable rules, so that one transcription scores every rule set: the live one (LOOPI_ASR_RULES), none, each rule
alone and all of them (``RULE_SETS``). Then it sums the owner's caption scores per caption model and asks the
hybrid search every question that has a correct moment: recall@5, recall@10, hit@5 and MRR with the live
thresholds, and recall@5 over a small grid of thresholds around them.

One clip per job: each job holds Whisper (job lock "asr") for one clip and waits behind every live transcription
(lowest priority, ``EVAL_PRIORITY``), so recordings keep their latency target while a run goes on (spec §14 P2);
the last job computes the numbers. A run's results are numbers only; the segments it heard stay in the database
next to the golden clips (deleted with them). Logs carry ids and counts, never text.
"""
from __future__ import annotations

import datetime as dt
import logging

import anyio
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .. import devices, media
from ..config import ASR_RULES, Settings
from ..days import range_utc
from ..pipeline import asr, jobs
from ..pipeline.context import PipelineContext, get_context
from ..pipeline.jobs import job_app
from ..pipeline.state import error_text
from ..providers.router import Router
from ..search import search_moments
from . import metrics, questions

log = logging.getLogger(__name__)
EVAL_PRIORITY = -20  # below the chapter and recap jobs (-10): an evaluation never runs ahead of live work
WER_LIMIT = 0.30  # spec §14 P2: above 30 %, change the model or the configuration
RECALL_TARGET = 0.70  # spec §14 P2: recall@5 ≥ 70 %
PRIMARY = {"vi": "wer", "en": "wer", "ko": "cer"}  # the rate the 30 % rule reads, per language
DISTANCES = (0.5, 0.6, 0.7, 0.8)  # the threshold grid of the recall section
SIMILARITIES = (0.2, 0.3, 0.4)
RESULTS_SHOWN = 10  # recall@10 needs the first 10 results
VARIANTS = ("live", "none", *ASR_RULES, "all")  # the rule sets in the order they are shown (JSONB keeps no order)


def rule_sets(live: list[str]) -> dict[str, list[str]]:
    """The rule sets a run scores: the live one, none, each rule alone, all of them (in VARIANTS order)."""
    return {"live": sorted(live), "none": [], **{rule: [rule] for rule in ASR_RULES}, "all": list(ASR_RULES)}


def run_lock(run_id: str) -> str:
    return f"eval:{run_id}"


async def _defer_step(conn, run_id: str) -> int:
    return await jobs.enqueue(conn, jobs.RUN_EVAL, queue="light", lock="asr", priority=EVAL_PRIORITY,
                              queueing_lock=run_lock(run_id), run_id=run_id)


async def start_run(
    pool: AsyncConnectionPool, settings: Settings, *, max_gain_db: float | None = None,
    vad_threshold: float | None = None,
) -> tuple[str, bool]:
    """Ask for a run (``max_gain_db`` and ``vad_threshold`` override the ASR's for this run only); return (run id,
    created). The search thresholds are this process's (the api serves the search); the ASR settings, rules and
    model versions are the worker's, recorded when it starts the run. One run at a time: while one waits or runs,
    its id is returned. A run whose job is gone (the worker died and the job was dead-lettered, say) is marked
    failed first."""
    if max_gain_db is not None and not 0.0 <= max_gain_db <= 60.0:
        raise ValueError("the gain ceiling is 0 to 60 dB")
    if vad_threshold is not None and not 0.0 < vad_threshold < 1.0:
        raise ValueError("the VAD threshold lies between 0 and 1")
    config = {
        "max_distance": settings.search_max_distance,
        "min_similarity": settings.search_min_similarity,
        "overrides": {k: v for k, v in (("max_gain_db", max_gain_db), ("vad_threshold", vad_threshold)) if v is not None},
    }
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('eval_run', 0))")
        cur = await conn.execute(
            "SELECT r.id, EXISTS (SELECT 1 FROM procrastinate_jobs j WHERE j.queueing_lock = 'eval:' || r.id"
            " AND j.status IN ('todo', 'doing')) AS alive FROM eval_run r WHERE r.status IN ('waiting', 'running')"
        )
        for row in await cur.fetchall():
            if row["alive"]:
                return row["id"], False
            await conn.execute(
                "UPDATE eval_run SET status = 'failed', finished_at = now(),"
                " error = 'the run stopped (its job is gone: a worker crash or restart); start a new one' WHERE id = %s",
                (row["id"],),
            )
        run_id = devices.new_id()
        await conn.execute("INSERT INTO eval_run (id, config) VALUES (%s, %s)", (run_id, Jsonb(config)))
        await _defer_step(conn, run_id)
    log.info("evaluation run %s requested", run_id)
    return run_id, True


def _regions(results: list[tuple[asr.Region, asr.RegionResult]]) -> list[dict]:
    return [
        {"start": region.start, "end": region.end, "lang": result.lang, "lang_prob": result.lang_prob,
         "segments": [{"start": s.start, "end": s.end, "text": s.text, "avg_logprob": s.avg_logprob,
                       "no_speech_prob": s.no_speech_prob, "compression_ratio": s.compression_ratio}
                      for s in result.segments]}
        for region, result in results
    ]


def hypothesis(regions: list[dict], rules: list[str]) -> str:
    """What the live stage would store for a clip under ``rules``: the kept segments' text, in order."""
    kept = []
    for region in regions:
        result = asr.RegionResult(region["lang"], region["lang_prob"],
                                  [asr.SegmentResult(**s) for s in region["segments"]])
        kept.extend(s.text.strip() for s in asr.kept_segments(result, frozenset(rules)))
    return " ".join(kept)


def asr_numbers(clips: list[dict], rules: list[str]) -> dict:
    """WER and CER per language and the no-speech check for one rule set, over the clips of a run."""
    word: dict[str, metrics.Errors] = {}
    char: dict[str, metrics.Errors] = {}
    count: dict[str, int] = {}
    silent = invented = invented_words = 0
    for clip in clips:
        heard = hypothesis(clip["segments"], rules)
        if clip["no_speech"]:
            silent += 1
            invented += bool(metrics.words(heard))
            invented_words += len(metrics.words(heard))
            continue
        lang = clip["lang"]
        word[lang] = word.get(lang, metrics.Errors()) + metrics.wer(clip["reference"], heard)
        char[lang] = char.get(lang, metrics.Errors()) + metrics.cer(clip["reference"], heard)
        count[lang] = count.get(lang, 0) + 1
    languages = {}
    for lang in sorted(count):
        rates = {"wer": word[lang].rate, "cer": char[lang].rate}
        score = rates[PRIMARY.get(lang, "wer")]
        languages[lang] = {"clips": count[lang], "words": word[lang].reference, "chars": char[lang].reference,
                           **rates, "metric": PRIMARY.get(lang, "wer"), "score": score,
                           "over_limit": score is not None and score > WER_LIMIT}
    return {"languages": languages,
            "no_speech": {"clips": silent, "invented": invented, "words": invented_words,
                          "rate": invented / silent if silent else None}}


async def caption_numbers(pool: AsyncConnectionPool) -> dict:
    """The owner's caption scores per caption model."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT model_version, count(*) AS scored, avg(score)::float8 AS mean,"
            " count(*) FILTER (WHERE score >= 4) AS good, count(*) FILTER (WHERE wrong_people) AS wrong_people,"
            " count(*) FILTER (WHERE invented_objects) AS invented_objects,"
            " count(*) FILTER (WHERE wrong_activity) AS wrong_activity"
            " FROM eval_caption_score GROUP BY model_version ORDER BY model_version"
        )
        return {r["model_version"]: {k: v for k, v in r.items() if k != "model_version"} for r in await cur.fetchall()}


class _CachedEmbeddings:
    """The router as the search sees it, with the question embeddings computed once per run (the grid asks each
    question twelve times)."""

    def __init__(self, router: Router):
        self._router = router
        self._cache: dict[tuple[str, ...], object] = {}

    async def embed(self, task: str, texts: list[str]):
        key = (task, *texts)
        if key not in self._cache:
            self._cache[key] = await self._router.embed(task, texts)
        return self._cache[key]


async def _recall(pool, router, asked: list[dict], zone, max_distance: float, min_similarity: float) -> dict:
    at5 = at10 = hit5 = mrr = 0.0
    semantic = True
    per_question = []
    for question in asked:
        start, end = range_utc(question["first_day"], question["last_day"], zone)
        result = await search_moments(pool, router, question["text"], start=start, end=end, limit=RESULTS_SHOWN,
                                      max_distance=max_distance, min_similarity=min_similarity)
        semantic = semantic and result.semantic
        ranks = questions.ranks(question["answers"], result.hits)
        relevant = len(question["answers"])
        at5 += metrics.recall_at(ranks, relevant, 5)
        at10 += metrics.recall_at(ranks, relevant, 10)
        hit5 += metrics.hit_at(ranks, 5)
        mrr += metrics.reciprocal_rank(ranks)
        per_question.append({"id": question["id"], "relevant": relevant, "ranks": ranks})
    n = len(asked)
    return {"at5": at5 / n, "at10": at10 / n, "hit5": hit5 / n, "mrr": mrr / n, "semantic": semantic,
            "per_question": per_question}


async def recall_numbers(pool: AsyncConnectionPool, router: Router, config: dict, zone) -> dict:
    """Recall of the hybrid search over the answered questions, with the live thresholds and over the grid. Not
    measured while the embedding model cannot run (words alone would understate it)."""
    asked = await questions.answered(pool)
    if not asked:
        return {"questions": 0}
    if not router.task_ready("embed", await router.health()):
        return {"questions": len(asked), "error": "the embedding model is not available"}
    cached = _CachedEmbeddings(router)
    live = await _recall(pool, cached, asked, zone, config["max_distance"], config["min_similarity"])
    grid = []
    for distance in DISTANCES:
        for similarity in SIMILARITIES:
            found = await _recall(pool, cached, asked, zone, distance, similarity)
            grid.append({"max_distance": distance, "min_similarity": similarity, "at5": found["at5"]})
    return {"questions": len(asked), **live, "meets_target": live["at5"] >= RECALL_TARGET, "grid": grid}


async def _clips(pool: AsyncConnectionPool, run_id: str) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT e.id, e.lang, e.no_speech, e.reference, r.segments, r.error FROM eval_run_audio r"
            " JOIN eval_audio e ON e.id = r.sample_id WHERE r.run_id = %s ORDER BY e.id",
            (run_id,),
        )
        return await cur.fetchall()


async def _finish(ctx: PipelineContext, run_id: str, config: dict) -> None:
    clips = await _clips(ctx.pool, run_id)
    heard = [c for c in clips if c["segments"] is not None]
    results = {
        "asr": {"clips": len(clips), "failed": len(clips) - len(heard),
                "variants": {name: {"rules": rules, **asr_numbers(heard, rules)}
                             for name, rules in rule_sets(config["rules"]).items()}},
        "captions": await caption_numbers(ctx.pool),
        "recall": await recall_numbers(ctx.pool, ctx.router, config, ctx.settings.zone),
    }
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE eval_run SET status = 'done', finished_at = now(), results = %s WHERE id = %s",
            (Jsonb(results), run_id),
        )
    log.info("evaluation run %s done: %d clips (%d failed), %d questions", run_id, len(clips),
             len(clips) - len(heard), results["recall"]["questions"])


async def _step(ctx: PipelineContext, run_id: str) -> str:
    pool = ctx.pool
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT status, config FROM eval_run WHERE id = %s FOR UPDATE", (run_id,))
        run = await cur.fetchone()
        if run is None or run["status"] in ("done", "failed"):
            return "nothing"
        config = run["config"]
        if run["status"] == "waiting":  # the live ASR settings are the worker's
            asr_settings = ctx.settings
            config = {
                **config,
                "max_gain_db": config["overrides"].get("max_gain_db", asr_settings.asr_max_gain_db),
                "vad_threshold": config["overrides"].get("vad_threshold", asr_settings.asr_vad_threshold),
                "rules": sorted(asr_settings.asr_rule_set),
                "models": {task: ctx.router.model_version(task) for task in ("asr", "caption", "embed")},
            }
            await conn.execute("UPDATE eval_run SET status = 'running', started_at = now(), config = %s WHERE id = %s",
                               (Jsonb(config), run_id))
        cur = await conn.execute(
            "SELECT e.id, e.path FROM eval_audio e WHERE NOT EXISTS (SELECT 1 FROM eval_run_audio r"
            " WHERE r.run_id = %s AND r.sample_id = e.id) ORDER BY e.id LIMIT 1",
            (run_id,),
        )
        clip = await cur.fetchone()
    if clip is None:
        await _finish(ctx, run_id, config)
        return "done"
    segments = error = None
    try:
        path = media.inside(ctx.settings.eval_dir, clip["path"])
        heard = await anyio.to_thread.run_sync(
            asr.transcribe_file, path, ctx.transcriber, config["max_gain_db"], config["vad_threshold"]
        )
        segments = _regions(heard)
    except Exception as exc:  # an unreadable or misplaced copy is counted as failed; the run goes on
        error = error_text(exc)
        log.warning("evaluation run %s: golden clip %s could not be transcribed: %s", run_id, clip["id"], error)
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO eval_run_audio (run_id, sample_id, segments, error)"
            " SELECT %s, id, %s, %s FROM eval_audio WHERE id = %s ON CONFLICT DO NOTHING",
            (run_id, None if segments is None else Jsonb(segments), error, clip["id"]),
        )
        await _defer_step(conn, run_id)
    return "clip"


async def run_step(ctx: PipelineContext, run_id: str) -> str:
    """One job of a run: the next golden clip ("clip"), or the numbers once every clip is heard ("done"). An
    unexpected error marks the run failed (with the error's type and message, never text from the golden set)."""
    try:
        return await _step(ctx, run_id)
    except Exception as exc:
        log.exception("evaluation run %s failed", run_id)
        async with ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE eval_run SET status = 'failed', finished_at = now(), error = %s WHERE id = %s",
                (error_text(exc), run_id),
            )
        return "failed"


@job_app.task(name=jobs.RUN_EVAL, queue="light", lock="asr", priority=EVAL_PRIORITY)
async def run_eval_job(run_id: str) -> None:
    await run_step(get_context(), run_id)


async def latest_run(pool: AsyncConnectionPool, run_id: str | None = None, finished: bool = False) -> dict | None:
    """``run_id``'s run, or the newest one (the newest finished one when ``finished``), with its progress."""
    where = "WHERE r.id = %(id)s" if run_id else ("WHERE r.status = 'done'" if finished else "")
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT r.*, (SELECT count(*) FROM eval_run_audio a WHERE a.run_id = r.id) AS heard,"
            f" (SELECT count(*) FROM eval_audio) AS clips FROM eval_run r {where}"
            " ORDER BY r.requested_at DESC, r.id DESC LIMIT 1",
            {"id": run_id},
        )
        return await cur.fetchone()


async def list_runs(pool: AsyncConnectionPool, limit: int = 20) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT r.*, (SELECT count(*) FROM eval_run_audio a WHERE a.run_id = r.id) AS heard,"
            " (SELECT count(*) FROM eval_audio) AS clips FROM eval_run r ORDER BY r.requested_at DESC, r.id DESC"
            " LIMIT %s",
            (limit,),
        )
        return await cur.fetchall()


def pct(value: float | None) -> str:
    """A rate as a percentage with one decimal; — when there is none."""
    return "—" if value is None else f"{100 * value:.1f} %"


def format_run(run: dict, zone) -> str:
    """A run's numbers as Markdown (no text from the golden set: the report and the CLI print this)."""
    when = run["requested_at"].astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
    config = run["config"]
    models = config.get("models", {})
    lines = [f"Evaluation run {run['id']} — {run['status']}, requested {when}", ""]
    if models:
        lines.append(f"ASR: {models['asr']}, gain ≤ {config['max_gain_db']:g} dB, VAD threshold "
                     f"{config['vad_threshold']:g}, live rules: {', '.join(config['rules']) or 'none'}")
    else:
        lines.append("ASR: not started yet (the worker records its settings when it starts the run)")
    if run["status"] != "done":
        lines.append(f"Progress: {run.get('heard', 0)}/{run.get('clips', 0)} clips"
                     + (f"; error: {run['error']}" if run.get("error") else ""))
        return "\n".join(lines)
    results = run["results"]
    audio = results["asr"]
    lines += ["", f"Clips: {audio['clips']} ({audio['failed']} could not be transcribed)", "",
              "| Rules | vi WER | ko CER | en WER | no speech: invented |", "|---|---|---|---|---|"]
    for name in (v for v in VARIANTS if v in audio["variants"]):
        variant = audio["variants"][name]
        cells = []
        for lang in ("vi", "ko", "en"):
            row = variant["languages"].get(lang)
            cells.append("—" if row is None else
                         f"{pct(row['score'])} ({row['clips']} clips){' > 30 %' if row['over_limit'] else ''}")
        silent = variant["no_speech"]
        label = f"{name} ({', '.join(variant['rules']) or 'none'})" if name == "live" else name
        lines.append(f"| {label} | {' | '.join(cells)} | {silent['invented']}/{silent['clips']} "
                     f"({silent['words']} words) |")
    lines += ["", "Captions (owner's scores, 1–5):"]
    for model, row in results["captions"].items():
        lines.append(f"- {model}: {row['scored']} scored, mean {row['mean']:.2f}, ≥ 4: {row['good']}; sai số người "
                     f"{row['wrong_people']}, bịa đồ vật {row['invented_objects']}, sai hoạt động "
                     f"{row['wrong_activity']}")
    if not results["captions"]:
        lines.append("- none scored")
    recall = results["recall"]
    lines.append("")
    if "at5" in recall:
        lines.append(
            f"Recall ({recall['questions']} questions, max distance {config['max_distance']:g}, min similarity "
            f"{config['min_similarity']:g}): @5 {pct(recall['at5'])} (target 70 %: "
            f"{'met' if recall['meets_target'] else 'not met'}), @10 {pct(recall['at10'])}, hit@5 "
            f"{pct(recall['hit5'])}, MRR {recall['mrr']:.2f}"
            + ("" if recall["semantic"] else " — words only (the embedding model failed)"))
        best = max(recall["grid"], key=lambda g: (g["at5"], -abs(g["max_distance"] - config["max_distance"])))
        lines.append(f"Best of the grid: @5 {pct(best['at5'])} at max distance {best['max_distance']:g}, "
                     f"min similarity {best['min_similarity']:g}")
    else:
        lines.append(f"Recall: not measured ({recall.get('error', 'no question has a correct moment yet')})")
    return "\n".join(lines)
```

- [ ] **Step 4: Register the task, label it, add the CLI**

```diff
diff --git a/brain/src/loopi_brain/cli.py b/brain/src/loopi_brain/cli.py
--- a/brain/src/loopi_brain/cli.py
+++ b/brain/src/loopi_brain/cli.py
@@ -6,10 +6,12 @@ import asyncio
 import datetime as dt
 import os
 import sys
+import time
 from pathlib import Path
 
 # Subcommands import what they need: `worker-health` runs every 30 s and must stay light.
 DEFAULT_PKI_DIR = "/srv/loopi/pki"
+EVAL_POLL_S = 10.0  # how often `eval run` looks whether the worker finished the run
 
 
 def build_parser() -> argparse.ArgumentParser:
@@ -27,6 +29,15 @@ def build_parser() -> argparse.ArgumentParser:
     models = sub.add_parser("models", help="AI models")
     models_sub = models.add_subparsers(dest="models_cmd", required=True)
     models_sub.add_parser("pull", help="download the Ollama models in providers.yaml and the Whisper model")
+    evaluation = sub.add_parser("eval", help="evaluate the pipeline on the owner's golden set (spec §13)")
+    eval_sub = evaluation.add_subparsers(dest="eval_cmd", required=True)
+    run = eval_sub.add_parser("run", help="ask the worker for a run, wait for it and print its numbers")
+    run.add_argument("--max-gain-db", type=float, help="the ASR gain ceiling for this run only")
+    run.add_argument("--vad-threshold", type=float, help="the VAD threshold for this run only")
+    run.add_argument("--no-wait", action="store_true", help="print the run id and return")
+    run.add_argument("--timeout", type=float, default=3 * 3600, help="seconds to wait (default 3 h)")
+    show = eval_sub.add_parser("show", help="print the numbers of a run (default: the newest)")
+    show.add_argument("run_id", nargs="?")
     return parser
 
 
@@ -110,6 +121,46 @@ def _models_pull() -> int:
     return 0
 
 
+async def _eval(args: argparse.Namespace) -> int:
+    from .config import Settings
+    from .db import make_pool
+    from .evaluation import runner
+    from .pipeline.jobs import open_jobs
+
+    settings = Settings()
+    pool = make_pool(settings.database_url)
+    await pool.open()
+    try:
+        if args.eval_cmd == "show":
+            run = await runner.latest_run(pool, args.run_id)
+            if run is None:
+                print("no evaluation run yet" if args.run_id is None else f"no run {args.run_id}")
+                return 1
+            print(runner.format_run(run, settings.zone))
+            return 0
+        try:
+            async with open_jobs(pool):
+                run_id, created = await runner.start_run(
+                    pool, settings, max_gain_db=args.max_gain_db, vad_threshold=args.vad_threshold
+                )
+        except ValueError as exc:
+            print(f"error: {exc}", file=sys.stderr)
+            return 2
+        print(f"{'started' if created else 'already waiting or running'}: {run_id}")
+        if args.no_wait:
+            return 0
+        deadline = time.monotonic() + args.timeout
+        while (run := await runner.latest_run(pool, run_id))["status"] not in ("done", "failed"):
+            if time.monotonic() > deadline:
+                print(f"still {run['status']} ({run['heard']}/{run['clips']} clips); see `loopi-brain eval show {run_id}`")
+                return 1
+            await asyncio.sleep(EVAL_POLL_S)
+        print(runner.format_run(run, settings.zone))
+        return 0 if run["status"] == "done" else 1
+    finally:
+        await pool.close()
+
+
 def main(argv: list[str] | None = None) -> int:
     args = build_parser().parse_args(argv)
     if args.cmd == "init-pki":
@@ -126,4 +177,6 @@ def main(argv: list[str] | None = None) -> int:
         return _worker_health()
     if args.cmd == "models":
         return _models_pull()
+    if args.cmd == "eval":
+        return asyncio.run(_eval(args))
     return 2
diff --git a/brain/src/loopi_brain/pipeline/jobs.py b/brain/src/loopi_brain/pipeline/jobs.py
--- a/brain/src/loopi_brain/pipeline/jobs.py
+++ b/brain/src/loopi_brain/pipeline/jobs.py
@@ -19,6 +19,7 @@ TASK_MODULES = [
     "loopi_brain.pipeline.chapters",
     "loopi_brain.pipeline.recap",
     "loopi_brain.pipeline.retention",
+    "loopi_brain.evaluation.runner",
 ]
 
 ANALYZE_VIDEO = "analyze_video"
@@ -34,6 +35,7 @@ BUILD_CHAPTERS = "build_chapters"
 PLAN_RECAPS = "plan_recaps"
 WRITE_RECAP = "write_recap"
 APPLY_RETENTION = "apply_retention"
+RUN_EVAL = "run_eval"
 PIPELINE_TASKS = (ANALYZE_VIDEO, CAPTION_KEYFRAME, TRANSCRIBE_AUDIO, INDEX_MARKER, EMBED_MOMENTS)
 # Runs that start again by themselves (the dispatcher asks for embedding runs; the scheduler starts
 # each periodic tick; the hourly planner asks for each day's chapters again), so a failed one is never
diff --git a/brain/src/loopi_brain/pipeline/status.py b/brain/src/loopi_brain/pipeline/status.py
--- a/brain/src/loopi_brain/pipeline/status.py
+++ b/brain/src/loopi_brain/pipeline/status.py
@@ -32,6 +32,7 @@ TASK_LABELS = {
     jobs.PLAN_RECAPS: "Lên lịch recap",
     jobs.WRITE_RECAP: "Viết recap",
     jobs.APPLY_RETENTION: "Dọn dữ liệu cũ",
+    jobs.RUN_EVAL: "Chạy đánh giá",
 }
 _LATENCY = (
     "SELECT count(*) AS n, avg(s) AS avg, percentile_cont(0.95) WITHIN GROUP (ORDER BY s) AS p95 FROM ({}) t"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_eval_runner.py tests/test_cli.py tests/test_worker.py -q`
Expected: `25 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `753 passed, 1 skipped`, `TOTAL … 99%` (`evaluation/runner.py` 100 %).

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/evaluation/runner.py brain/src/loopi_brain/pipeline/jobs.py \
  brain/src/loopi_brain/pipeline/status.py brain/src/loopi_brain/cli.py brain/tests/test_eval_runner.py
git commit -F - <<'EOF'
feat(brain): evaluation runs (one golden clip per job, every rule set, captions, recall) and loopi-brain eval

Each job hears one golden clip through the live ASR path and waits behind live transcriptions (lock asr, priority
-20); the last job scores every rule set, the caption scores and recall@5 through the hybrid search. Numbers only.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---
### Task 7: The Đánh giá page — overview, golden clips and golden keyframes

**Files:**
- Create: `brain/src/loopi_brain/web/eval_routes.py`
- Create: `brain/src/loopi_brain/web/templates/eval.html`, `eval_audio.html`, `eval_captions.html`, `_eval_clip.html`, `_eval_picture.html`, `_eval_caption.html`, `_eval_score.html`
- Modify: `brain/src/loopi_brain/app.py` (router), `brain/src/loopi_brain/web/templates/base.html` (menu)
- Test: `brain/tests/test_eval_page.py` (new)

**Interfaces:**
- Consumes: Task 4's `golden.*`, Task 5's `questions.counts`; P2b `web.routes.TEMPLATES`, `require_htmx`, the `/audio/{item}.opus` and `/thumb/{keyframe}.jpg` routes, `media.inside`, the `client`-style test app (`create_app(settings, pool=…, http_client=…, llm_client=…)`).
- Produces:
  - `GET /eval` (progress); `GET /eval/audio?seed=N` (proposals by language with the current transcript in a textarea, scarce languages named, the labelled clips); `POST /eval/audio` (`item_id`, `lang`, `no_speech`, `reference`) → the saved clip's form, or "Không lưu được …" (200) when the recording cannot be one, 422 for a bad label; `POST /eval/audio/{id}` (correct a label), `POST /eval/audio/{id}/delete`; `GET /eval/audio/{id}.opus` (the playback copy, else the byte copy).
  - `GET /eval/captions?seed=N`; `POST /eval/captions` (`keyframe_id`, `score`, `wrong_people`, `invented_objects`, `wrong_activity`); `POST /eval/captions/{id}/score`; `POST /eval/captions/{id}/delete`; `GET /eval/keyframes/{id}.jpg`.
  - Jinja globals `LANGUAGE_LABELS`, `FLAG_LABELS`; the menu gains "Đánh giá".

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_eval_page.py`:

```python
import datetime as dt

import httpx
import pytest

from helpers import (
    QUIET_SPEECH,
    FakeOllama,
    analysed_audio,
    captioned_keyframe,
    fetch_all,
    insert_segment,
    paired_device,
)
from loopi_brain.app import create_app
from loopi_brain.evaluation import golden

HX = {"HX-Request": "true"}
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
async def web(pool, pki_settings, jobs_open):
    ollama = FakeOllama()
    app = create_app(pki_settings, pool=pool, http_client=ollama.client(), llm_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _spoken(pool, settings, device_id, minutes, lang, text) -> str:
    item = await analysed_audio(pool, settings, device_id, NINE + dt.timedelta(minutes=minutes), source=QUIET_SPEECH)
    await insert_segment(pool, item, device_id, NINE + dt.timedelta(minutes=minutes), text, lang=lang)
    return item


def _flat(html: str) -> str:
    return " ".join(html.split())


async def test_the_evaluation_page_shows_how_far_the_labelling_is(web):
    page = await web.get("/eval")
    assert page.status_code == 200
    text = _flat(page.text)
    assert '<a href="/eval">Đánh giá</a>' in text  # in the menu
    assert "Âm thanh</a>: 0/20 đoạn" in text and "Tiếng Việt 0/6 · Tiếng Hàn 0/6 · Tiếng Anh 0/4 · Không có lời nói 0/4" in text
    assert "Mô tả khung hình</a>: 0/30 khung hình" in text and "Câu hỏi</a>: 0/25 câu" in text


async def test_the_owner_labels_a_proposed_clip_and_corrects_it(pool, pki_settings, dev, web):
    item = await _spoken(pool, pki_settings, dev["device_id"], 0, "vi", "Mai mình đi chợ")
    page = _flat((await web.get("/eval/audio")).text)
    assert f'src="/audio/{item}.opus"' in page and ">Mai mình đi chợ</textarea>" in page
    assert '<input type="radio" name="lang" value="vi" checked>' in page
    form = {"item_id": item, "lang": "vi", "reference": "Mai mình đi chợ nhé"}
    assert (await web.post("/eval/audio", data=form)).status_code == 403  # no HX-Request: refused
    saved = await web.post("/eval/audio", data=form, headers=HX)
    [sample] = await golden.audio_samples(pool)
    assert saved.status_code == 200 and "Đã lưu" in saved.text and f'/eval/audio/{sample["id"]}.opus' in saved.text
    assert sample["reference"] == "Mai mình đi chợ nhé"
    played = await web.get(f"/eval/audio/{sample['id']}.opus")
    assert played.status_code == 200 and played.headers["content-type"] == "audio/ogg"
    view = pki_settings.eval_dir / golden.audio_paths(sample["id"])[1]
    assert played.content == view.read_bytes()  # the copy raised to a level you can hear
    page = _flat((await web.get("/eval/audio")).text)
    assert "Đã gán nhãn (1)" in page and f'name="item_id" value="{item}"' not in page
    fixed = await web.post(f"/eval/audio/{sample['id']}", data={"no_speech": "on"}, headers=HX)
    assert fixed.status_code == 200 and (await golden.audio_samples(pool))[0]["no_speech"] is True
    assert (await web.post(f"/eval/audio/{sample['id']}", data={"lang": "ja", "reference": "x"},
                           headers=HX)).status_code == 422
    view.unlink()  # no playback copy: the copy as recorded plays
    assert (await web.get(f"/eval/audio/{sample['id']}.opus")).status_code == 200
    gone = await web.post(f"/eval/audio/{sample['id']}/delete", headers=HX)
    assert "Đã xoá" in gone.text and await golden.audio_samples(pool) == []
    for path in (f"/eval/audio/{sample['id']}.opus",):
        assert (await web.get(path)).status_code == 404
    assert (await web.post(f"/eval/audio/{sample['id']}/delete", headers=HX)).status_code == 404
    assert (await web.post(f"/eval/audio/{sample['id']}", data={"no_speech": "on"}, headers=HX)).status_code == 404


async def test_a_clip_that_cannot_be_saved_says_why(pool, pki_settings, dev, web):
    item = await _spoken(pool, pki_settings, dev["device_id"], 0, "vi", "x")
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now()")  # expired since the page was opened
    answer = await web.post("/eval/audio", data={"item_id": item, "lang": "vi", "reference": "x"}, headers=HX)
    assert answer.status_code == 200 and "Không lưu được" in answer.text
    empty = await web.post("/eval/audio", data={"item_id": item, "lang": "vi", "reference": " "}, headers=HX)
    assert empty.status_code == 422
    assert await fetch_all(pool, "SELECT id FROM eval_audio") == []


async def test_a_language_the_home_rarely_speaks_is_reported_not_faked(pool, pki_settings, dev, web):
    for minutes in range(golden.AUDIO_SUGGESTED["vi"]):  # enough Vietnamese, no Korean or English at all
        await _spoken(pool, pki_settings, dev["device_id"], minutes, "vi", "x")
    page = _flat((await web.get("/eval/audio")).text)
    assert "Tiếng Hàn: chỉ còn 0 đoạn ghi để chọn, còn thiếu 6 so với gợi ý 6." in page
    assert "Tiếng Anh: chỉ còn 0 đoạn ghi để chọn" in page and "Tiếng Việt: chỉ còn" not in page


async def test_the_owner_scores_a_proposed_keyframe_and_its_next_caption(pool, pki_settings, dev, web):
    kf = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    page = _flat((await web.get("/eval/captions")).text)
    assert f'src="/thumb/{kf}.jpg"' in page and "A small home office with a desk by a window" in page
    saved = await web.post("/eval/captions", data={"keyframe_id": kf, "score": "4", "invented_objects": "on"},
                           headers=HX)
    [sample] = await golden.caption_samples(pool)
    assert saved.status_code == 200 and "ollama:minicpm-v4.6:1b: 4/5 · bịa đồ vật" in _flat(saved.text)
    picture = await web.get(f"/eval/keyframes/{sample['id']}.jpg")
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/jpeg"
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption_model = 'ollama:new'")
    page = _flat((await web.get("/eval/captions")).text)
    assert "Mô tả của model hiện tại chưa được chấm." in page and "Đã chấm (1)" in page
    again = await web.post(f"/eval/captions/{sample['id']}/score", data={"score": "2", "wrong_people": "on"},
                           headers=HX)
    assert "ollama:new: 2/5 · sai số người" in _flat(again.text)
    for bad in ({"score": "0"}, {"score": "6"}):
        assert (await web.post(f"/eval/captions/{sample['id']}/score", data=bad, headers=HX)).status_code == 422
    assert (await web.post("/eval/captions", data={"keyframe_id": kf, "score": "9"}, headers=HX)).status_code == 422
    again = await web.post("/eval/captions", data={"keyframe_id": kf, "score": "3"}, headers=HX)
    assert "Không lưu được" in again.text  # already golden
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = NULL")
    assert "Không lưu được" in (await web.post(f"/eval/captions/{sample['id']}/score", data={"score": "3"},
                                               headers=HX)).text
    assert "Đã xoá" in (await web.post(f"/eval/captions/{sample['id']}/delete", headers=HX)).text
    assert (await web.get(f"/eval/keyframes/{sample['id']}.jpg")).status_code == 404
    assert (await web.post(f"/eval/captions/{sample['id']}/delete", headers=HX)).status_code == 404


async def test_transcripts_and_captions_cannot_inject_markup(pool, pki_settings, dev, web):
    await _spoken(pool, pki_settings, dev["device_id"], 0, "en", "<script>alert(1)</script>")
    await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE, caption={
        "scene": "<img src=x onerror=alert(2)>", "activity": "x", "people_count": 0, "objects": ["<b>"],
        "text_seen": "", "notable": False})
    audio = (await web.get("/eval/audio")).text
    captions = (await web.get("/eval/captions")).text
    assert "<script>alert(1)" not in audio and "&lt;script&gt;alert(1)&lt;/script&gt;" in audio
    assert "<img src=x" not in captions and "&lt;b&gt;" in captions


async def test_golden_files_are_served_only_for_their_samples(pool, pki_settings, web):
    assert (await web.get("/eval/audio/01NOSUCH000000000000000000.opus")).status_code == 404
    assert (await web.get("/eval/keyframes/01NOSUCH000000000000000000.jpg")).status_code == 404
    async with pool.connection() as conn:  # a row without its copy (the copy was lost)
        await conn.execute("INSERT INTO device (id, name, cert_fingerprint) VALUES ('D1', 'd', 'fp')")
        await conn.execute("INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes, raw_path,"
                           " status) VALUES ('I1', 'D1', 'audio', now(), now(), 'x', 1, 'raw/x.opus', 'ready')")
        await conn.execute("INSERT INTO eval_audio (id, item_id, path, no_speech) VALUES ('A1', 'I1', 'audio/A1.opus', true)")
    assert (await web.get("/eval/audio/A1.opus")).status_code == 404
    with pytest.raises(ValueError):
        await golden.sample_exists(pool, "item", "A1")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_eval_page.py -q`
Expected: FAIL — `6 failed, 1 passed`: no Đánh giá route exists yet (`assert 404 == 200`, `… in '{"detail":"Not Found"}'`); the one that passes only checks that unknown golden files answer 404.

- [ ] **Step 3: Write the routes**

Create `brain/src/loopi_brain/web/eval_routes.py`:

```python
"""The Đánh giá page (spec §13, D21): the owner labels the golden set here — clips, keyframes, questions — and
starts evaluation runs. Everything shown comes from the database and LOOPI_EVAL_DIR; nothing is logged with text."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from .. import media
from ..evaluation import golden, questions
from ..evaluation.golden import GoldenError
from .routes import TEMPLATES, require_htmx

log = logging.getLogger(__name__)
LANGUAGE_LABELS = {"vi": "Tiếng Việt", "ko": "Tiếng Hàn", "en": "Tiếng Anh", "": "Whisper không nghe thấy lời nào",
                   golden.NO_SPEECH: "Không có lời nói"}
FLAG_LABELS = {"wrong_people": "sai số người", "invented_objects": "bịa đồ vật", "wrong_activity": "sai hoạt động"}
NOT_SAVED = ("Không lưu được: đoạn ghi đã bị xoá, đã hết hạn, chưa phân tích xong hoặc đã có trong bộ đánh giá."
             " Tải lại trang.")
NOT_SCORED = ("Không lưu được: khung hình đã bị xoá, chưa có mô tả (đang mô tả lại) hoặc đã có trong bộ đánh giá."
              " Tải lại trang.")
TEMPLATES.env.globals["LANGUAGE_LABELS"] = LANGUAGE_LABELS
TEMPLATES.env.globals["FLAG_LABELS"] = FLAG_LABELS

router = APIRouter()


def _sent(text: str) -> HTMLResponse:
    return HTMLResponse(f'<p class="err">{text}</p>')


def _local(row: dict, key: str, zone) -> str:
    return row[key].astimezone(zone).strftime("%d/%m/%Y %H:%M")


@router.get("/eval", response_class=HTMLResponse)
async def eval_page(request: Request):
    pool = request.app.state.pool
    return TEMPLATES.TemplateResponse(request, "eval.html", {
        "p": await golden.progress(pool), "q": await questions.counts(pool), "suggested": golden.AUDIO_SUGGESTED,
        "audio_target": golden.AUDIO_TARGET, "caption_target": golden.CAPTION_TARGET,
        "question_target": questions.QUESTION_TARGET,
    })


def _scarce(strata: dict, progress: dict) -> list[str]:
    """Languages that cannot reach their suggested number of clips with the recordings still kept."""
    notes = []
    for lang in golden.LANGUAGES:
        missing = golden.AUDIO_SUGGESTED[lang] - progress["audio"][lang]
        available = strata[lang]["available"]
        if missing > available:
            notes.append(f"{LANGUAGE_LABELS[lang]}: chỉ còn {available} đoạn ghi để chọn, còn thiếu {missing} so với"
                         f" gợi ý {golden.AUDIO_SUGGESTED[lang]}. Báo cáo nghiệm thu sẽ ghi đúng số đoạn có được.")
    return notes


@router.get("/eval/audio", response_class=HTMLResponse)
async def eval_audio_page(request: Request, seed: int = Query(default=0, ge=0, le=1_000_000)):
    state = request.app.state
    zone = state.settings.zone
    strata = await golden.audio_candidates(state.pool, zone, seed)
    for stratum in strata.values():
        for item in stratum["items"]:
            item["time"] = _local(item, "started_at", zone)
    progress = await golden.progress(state.pool)
    samples = [{**s, "time": _local(s, "started_at", zone)} for s in await golden.audio_samples(state.pool)]
    return TEMPLATES.TemplateResponse(request, "eval_audio.html", {
        "strata": strata, "samples": samples, "p": progress, "scarce": _scarce(strata, progress), "seed": seed,
        "suggested": golden.AUDIO_SUGGESTED, "target": golden.AUDIO_TARGET, "languages": golden.LANGUAGES,
    })


def _label(lang: str, no_speech: str, reference: str) -> golden.AudioLabel:
    try:
        return golden.audio_label(lang, no_speech == "on", reference)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _sample_view(request: Request, sample_id: str) -> HTMLResponse:
    zone = request.app.state.settings.zone
    found = [s for s in await golden.audio_samples(request.app.state.pool) if s["id"] == sample_id]
    if not found:
        raise HTTPException(status_code=404, detail="unknown golden clip")
    return TEMPLATES.TemplateResponse(request, "_eval_clip.html", {
        "s": {**found[0], "time": _local(found[0], "started_at", zone)}, "languages": golden.LANGUAGES,
        "saved": True})


@router.post("/eval/audio", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def add_clip(request: Request, item_id: str = Form(...), lang: str = Form(""), no_speech: str = Form(""),
                   reference: str = Form("", max_length=20_000)):
    state = request.app.state
    try:
        sample_id = await golden.add_audio(state.pool, state.settings, item_id, _label(lang, no_speech, reference))
    except GoldenError:
        return _sent(NOT_SAVED)
    return await _sample_view(request, sample_id)


@router.post("/eval/audio/{sample_id}", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def update_clip(request: Request, sample_id: str, lang: str = Form(""), no_speech: str = Form(""),
                      reference: str = Form("", max_length=20_000)):
    if not await golden.update_audio(request.app.state.pool, sample_id, _label(lang, no_speech, reference)):
        raise HTTPException(status_code=404, detail="unknown golden clip")
    return await _sample_view(request, sample_id)


@router.post("/eval/audio/{sample_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def delete_clip(request: Request, sample_id: str):
    if not await golden.delete_audio(request.app.state.pool, request.app.state.settings, sample_id):
        raise HTTPException(status_code=404, detail="unknown golden clip")
    return HTMLResponse('<p class="muted">Đã xoá đoạn này khỏi bộ đánh giá.</p>')


def _eval_file(request: Request, rel: str, media_type: str) -> FileResponse:
    try:
        path = media.inside(request.app.state.settings.eval_dir, rel)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="golden file missing") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="golden file missing")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})


@router.get("/eval/audio/{sample_id}.opus")
async def clip_audio(request: Request, sample_id: str):
    """The golden clip at a level you can hear (its playback copy), or as recorded when there is none."""
    if not await golden.sample_exists(request.app.state.pool, "eval_audio", sample_id):
        raise HTTPException(status_code=404, detail="unknown golden clip")
    copy, view = golden.audio_paths(sample_id)
    if (request.app.state.settings.eval_dir / view).is_file():
        return _eval_file(request, view, "audio/ogg")
    return _eval_file(request, copy, "audio/ogg")


@router.get("/eval/captions", response_class=HTMLResponse)
async def eval_captions_page(request: Request, seed: int = Query(default=0, ge=0, le=1_000_000)):
    state = request.app.state
    zone = state.settings.zone
    candidates = [{**k, "time": _local(k, "ts", zone)} for k in await golden.caption_candidates(state.pool, zone, seed)]
    samples = [{**s, "time": _local(s, "ts", zone)} for s in await golden.caption_samples(state.pool)]
    return TEMPLATES.TemplateResponse(request, "eval_captions.html", {
        "candidates": candidates, "samples": samples, "p": await golden.progress(state.pool), "seed": seed,
        "target": golden.CAPTION_TARGET,
    })


def _flags(wrong_people: str, invented_objects: str, wrong_activity: str) -> set[str]:
    chosen = {"wrong_people": wrong_people, "invented_objects": invented_objects, "wrong_activity": wrong_activity}
    return {name for name, value in chosen.items() if value == "on"}


async def _picture_view(request: Request, sample_id: str) -> HTMLResponse:
    zone = request.app.state.settings.zone
    found = [s for s in await golden.caption_samples(request.app.state.pool) if s["id"] == sample_id]
    if not found:
        raise HTTPException(status_code=404, detail="unknown golden keyframe")
    return TEMPLATES.TemplateResponse(request, "_eval_picture.html", {
        "s": {**found[0], "time": _local(found[0], "ts", zone)}, "saved": True})


@router.post("/eval/captions", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def add_picture(request: Request, keyframe_id: str = Form(...), score: int = Form(...),
                      wrong_people: str = Form(""), invented_objects: str = Form(""), wrong_activity: str = Form("")):
    state = request.app.state
    try:
        sample_id = await golden.add_caption(state.pool, state.settings, keyframe_id, score,
                                             _flags(wrong_people, invented_objects, wrong_activity))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GoldenError:
        return _sent(NOT_SCORED)
    return await _picture_view(request, sample_id)


@router.post("/eval/captions/{sample_id}/score", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def score_picture(request: Request, sample_id: str, score: int = Form(...), wrong_people: str = Form(""),
                        invented_objects: str = Form(""), wrong_activity: str = Form("")):
    try:
        scored = await golden.score_caption(request.app.state.pool, sample_id, score,
                                            _flags(wrong_people, invented_objects, wrong_activity))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not scored:
        return _sent(NOT_SCORED)
    return await _picture_view(request, sample_id)


@router.post("/eval/captions/{sample_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def delete_picture(request: Request, sample_id: str):
    if not await golden.delete_caption(request.app.state.pool, request.app.state.settings, sample_id):
        raise HTTPException(status_code=404, detail="unknown golden keyframe")
    return HTMLResponse('<p class="muted">Đã xoá khung hình này khỏi bộ đánh giá.</p>')


@router.get("/eval/keyframes/{sample_id}.jpg")
async def picture_file(request: Request, sample_id: str):
    if not await golden.sample_exists(request.app.state.pool, "eval_caption", sample_id):
        raise HTTPException(status_code=404, detail="unknown golden keyframe")
    return _eval_file(request, golden.keyframe_path(sample_id), "image/jpeg")
```

- [ ] **Step 4: Write the templates**

Create `brain/src/loopi_brain/web/templates/eval.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Đánh giá</h1>
<p class="muted">Bộ đánh giá (golden set) do bạn gán nhãn: nằm trong thư mục dữ liệu và database của brain, không bao giờ
  vào git (D21). Lần chạy đánh giá chỉ ghi lại các con số.</p>
<section class="card">
  <h2><a href="/eval/audio">Âm thanh</a>: {{ p.audio_total }}/{{ audio_target }} đoạn</h2>
  <p>{% for lang, n in suggested.items() %}{{ LANGUAGE_LABELS[lang] }} {{ p.audio[lang] }}/{{ n }}{% if not loop.last %} · {% endif %}{% endfor %}</p>
  <p class="muted">Nghe từng đoạn, sửa transcript hiện tại thành bản chuẩn, hoặc đánh dấu "không có lời nói".</p>
</section>
<section class="card">
  <h2><a href="/eval/captions">Mô tả khung hình</a>: {{ p.captions }}/{{ caption_target }} khung hình</h2>
  <p>Đã chấm với model hiện tại: {{ p.captions_scored }}</p>
</section>
<section class="card">
  <h2><a href="/eval/questions">Câu hỏi</a>: {{ q.questions }}/{{ question_target }} câu</h2>
  <p>Đã chọn moment đúng: {{ q.answered }}</p>
</section>
{% endblock %}
```

Create `brain/src/loopi_brain/web/templates/eval_audio.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Đánh giá · Âm thanh</h1>
<p><a href="/eval">← Đánh giá</a> · {{ p.audio_total }}/{{ target }} đoạn:
  {% for lang, n in suggested.items() %}{{ LANGUAGE_LABELS[lang] }} {{ p.audio[lang] }}/{{ n }}{% if not loop.last %} · {% endif %}{% endfor %}</p>
<p class="muted">Nghe đoạn ghi (đã chuẩn hoá mức âm thanh), sửa transcript hiện tại thành đúng những gì được nói, chọn
  ngôn ngữ chính, rồi lưu. Đoạn chỉ có tiếng ồn, TV hay im lặng: đánh dấu "Không có lời nói" (để đo việc Whisper bịa câu).</p>
{% for note in scarce %}<p class="err">{{ note }}</p>{% endfor %}
{% for lang, stratum in strata.items() %}
<section>
  <h2>{{ LANGUAGE_LABELS[lang] }} <span class="muted">({{ stratum.available }} đoạn có thể chọn)</span></h2>
  {% for it in stratum["items"] %}
  <form class="card" hx-post="/eval/audio" hx-target="this" hx-swap="outerHTML">
    <input type="hidden" name="item_id" value="{{ it.id }}">
    <p><time>{{ it.time }}</time> · {{ it.part }} <audio controls preload="none" src="/audio/{{ it.id }}.opus"></audio></p>
    <textarea name="reference" rows="3" cols="80" maxlength="5000" placeholder="Transcript chuẩn">{{ it.transcript }}</textarea>
    <p>{% for code in languages %}<label><input type="radio" name="lang" value="{{ code }}"{% if code == lang %} checked{% endif %}> {{ LANGUAGE_LABELS[code] }}</label> {% endfor %}
      <label><input type="checkbox" name="no_speech"> Không có lời nói</label>
      <button>Lưu vào bộ đánh giá</button></p>
  </form>
  {% else %}<p class="muted">Không còn đoạn nào để chọn.</p>{% endfor %}
</section>
{% endfor %}
<p><a href="/eval/audio?seed={{ seed + 1 }}">Gợi ý khác</a></p>
<h2>Đã gán nhãn ({{ samples|length }})</h2>
{% for s in samples %}{% include "_eval_clip.html" %}{% else %}<p class="muted">Chưa có đoạn nào.</p>{% endfor %}
{% endblock %}
```

Create `brain/src/loopi_brain/web/templates/_eval_clip.html`:

```html
<form class="card" id="clip-{{ s.id }}" hx-post="/eval/audio/{{ s.id }}" hx-target="this" hx-swap="outerHTML">
  <p><time>{{ s.time }}</time> <audio controls preload="none" src="/eval/audio/{{ s.id }}.opus"></audio>
    {% if saved %}<span class="online">Đã lưu</span>{% endif %}</p>
  <textarea name="reference" rows="3" cols="80" maxlength="5000" placeholder="Transcript chuẩn">{{ s.reference }}</textarea>
  <p>{% for lang in languages %}<label><input type="radio" name="lang" value="{{ lang }}"{% if s.lang == lang %} checked{% endif %}> {{ LANGUAGE_LABELS[lang] }}</label> {% endfor %}
    <label><input type="checkbox" name="no_speech"{% if s.no_speech %} checked{% endif %}> Không có lời nói</label>
    <button>Lưu</button>
    <button hx-post="/eval/audio/{{ s.id }}/delete" hx-target="#clip-{{ s.id }}" hx-swap="outerHTML"
            hx-confirm="Xoá đoạn này khỏi bộ đánh giá?">Xoá</button></p>
</form>
```

Create `brain/src/loopi_brain/web/templates/eval_captions.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Đánh giá · Mô tả khung hình</h1>
<p><a href="/eval">← Đánh giá</a> · {{ p.captions }}/{{ target }} khung hình, {{ p.captions_scored }} đã chấm với model hiện tại</p>
<p class="muted">Chấm mô tả từ 1 (sai hẳn) đến 5 (đúng và đủ), và đánh dấu lỗi nếu có.</p>
<section>
  <h2>Gợi ý (mỗi ngày, mỗi khung 3 giờ một khung hình)</h2>
  {% for k in candidates %}
  <form class="card" hx-post="/eval/captions" hx-target="this" hx-swap="outerHTML">
    <input type="hidden" name="keyframe_id" value="{{ k.id }}">
    <img src="/thumb/{{ k.id }}.jpg" loading="lazy" alt="" width="320">
    <p><time>{{ k.time }}</time> · {{ k.caption_model }}</p>
    {% set c = k.caption %}{% include "_eval_caption.html" %}
    {% set button = "Lưu vào bộ đánh giá" %}{% include "_eval_score.html" %}
  </form>
  {% else %}<p class="muted">Không còn khung hình nào để chọn.</p>{% endfor %}
  <p><a href="/eval/captions?seed={{ seed + 1 }}">Gợi ý khác</a></p>
</section>
<h2>Đã chấm ({{ samples|length }})</h2>
{% for s in samples %}{% include "_eval_picture.html" %}{% else %}<p class="muted">Chưa có khung hình nào.</p>{% endfor %}
{% endblock %}
```

Create `brain/src/loopi_brain/web/templates/_eval_picture.html`:

```html
<div class="card" id="picture-{{ s.id }}">
  <img src="/eval/keyframes/{{ s.id }}.jpg" loading="lazy" alt="" width="320">
  <p><time>{{ s.time }}</time> · {{ s.caption_model }} {% if saved %}<span class="online">Đã lưu</span>{% endif %}</p>
  {% set c = s.caption %}{% include "_eval_caption.html" %}
  <ul>{% for x in s.scores %}<li>{{ x.model_version }}: {{ x.score }}/5{% for name, label in FLAG_LABELS.items() %}{% if x[name] %} · {{ label }}{% endif %}{% endfor %}</li>{% endfor %}</ul>
  {% if s.caption %}
  <form hx-post="/eval/captions/{{ s.id }}/score" hx-target="#picture-{{ s.id }}" hx-swap="outerHTML">
    {% if s.needs_score %}<p class="err">Mô tả của model hiện tại chưa được chấm.</p>{% endif %}
    {% set button = "Chấm mô tả hiện tại" %}{% include "_eval_score.html" %}
  </form>
  {% endif %}
  <button hx-post="/eval/captions/{{ s.id }}/delete" hx-target="#picture-{{ s.id }}" hx-swap="outerHTML"
          hx-confirm="Xoá khung hình này khỏi bộ đánh giá?">Xoá</button>
</div>
```

Create `brain/src/loopi_brain/web/templates/_eval_caption.html`:

```html
{% if c %}<p>{{ c.scene }} — {{ c.activity }} · {{ c.people_count }} người{% if c.objects %} · đồ vật: {{ c.objects|join(", ") }}{% endif %}{% if c.text_seen %} · chữ: “{{ c.text_seen }}”{% endif %}</p>
{% else %}<p class="muted">Đang chờ mô tả lại.</p>{% endif %}
```

Create `brain/src/loopi_brain/web/templates/_eval_score.html`:

```html
<p>{% for n in range(1, 6) %}<label><input type="radio" name="score" value="{{ n }}" required> {{ n }}</label> {% endfor %}
  {% for name, label in FLAG_LABELS.items() %}<label><input type="checkbox" name="{{ name }}"> {{ label }}</label> {% endfor %}
  <button>{{ button }}</button></p>
```

- [ ] **Step 5: Mount the router and add the menu entry**

```diff
diff --git a/brain/src/loopi_brain/app.py b/brain/src/loopi_brain/app.py
--- a/brain/src/loopi_brain/app.py
+++ b/brain/src/loopi_brain/app.py
@@ -16,6 +16,7 @@ from .db import make_pool
 from .pipeline.jobs import open_jobs
 from .providers.ollama import DEFAULT_TIMEOUT
 from .web.day_routes import router as day_router
+from .web.eval_routes import router as eval_router
 from .web.routes import router as web_router
 from .web.search_routes import router as search_router
 from .web.settings_routes import router as settings_router
@@ -82,6 +83,7 @@ def create_app(
     app.include_router(day_router)
     app.include_router(search_router)
     app.include_router(settings_router)
+    app.include_router(eval_router)
     settings.media_root.mkdir(parents=True, exist_ok=True)
     app.mount("/media", StaticFiles(directory=settings.media_root), name="media")
     app.mount(
diff --git a/brain/src/loopi_brain/web/templates/base.html b/brain/src/loopi_brain/web/templates/base.html
--- a/brain/src/loopi_brain/web/templates/base.html
+++ b/brain/src/loopi_brain/web/templates/base.html
@@ -33,7 +33,7 @@
   </style>
 </head>
 <body>
-  <header><nav><a href="/">Hôm nay</a><a href="/search">Tìm kiếm</a><a href="/pipeline">Xử lý</a><a href="/devices">Thiết bị</a><a href="/settings">Cài đặt</a></nav></header>
+  <header><nav><a href="/">Hôm nay</a><a href="/search">Tìm kiếm</a><a href="/pipeline">Xử lý</a><a href="/devices">Thiết bị</a><a href="/settings">Cài đặt</a><a href="/eval">Đánh giá</a></nav></header>
   <div id="hx-error" class="err" role="alert"></div>
   <main>{% block content %}{% endblock %}</main>
   <script>
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_eval_page.py tests/test_web.py -q`
Expected: `19 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `760 passed, 1 skipped`, `TOTAL … 99%`.

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/web/eval_routes.py brain/src/loopi_brain/web/templates/eval.html \
  brain/src/loopi_brain/web/templates/eval_audio.html brain/src/loopi_brain/web/templates/eval_captions.html \
  brain/src/loopi_brain/web/templates/_eval_clip.html brain/src/loopi_brain/web/templates/_eval_picture.html \
  brain/src/loopi_brain/web/templates/_eval_caption.html brain/src/loopi_brain/web/templates/_eval_score.html \
  brain/src/loopi_brain/app.py brain/src/loopi_brain/web/templates/base.html brain/tests/test_eval_page.py
git commit -F - <<'EOF'
feat(brain): the Đánh giá page: label golden clips and score golden keyframes

Proposed recordings by detected language and part of the day (a scarce language is said, never padded), proposed
keyframes one per day and 3-hour block; the owner corrects the transcript or marks "không có lời nói", scores
captions 1-5 with three error ticks.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 8: Đánh giá — questions, their correct moments, and runs with their numbers

**Files:**
- Modify: `brain/src/loopi_brain/web/eval_routes.py`, `brain/src/loopi_brain/web/templates/eval.html`
- Create: `brain/src/loopi_brain/web/templates/eval_questions.html`, `eval_question.html`, `_eval_answers.html`, `_eval_hits.html`, `_eval_runs.html`
- Modify: `brain/tests/test_eval_page.py`

**Interfaces:**
- Consumes: Task 5's `questions.*`, Task 6's `runner.start_run`, `runner.list_runs`, `runner.pct`, `runner.WER_LIMIT`, `runner.DISTANCES`, `runner.SIMILARITIES`, `runner.VARIANTS`; P2b `search.request_router(settings, client)`, `search_routes.hit_view`, `search_routes.KIND_LABELS`, `search_routes.OptionalDate`.
- Produces:
  - `GET /eval/questions`; `POST /eval/questions` (`text`, `from`, `to`) → `HX-Redirect: /eval/questions/{id}`; `GET /eval/questions/{id}`; `POST /eval/questions/{id}` (edit); `POST /eval/questions/{id}/delete` → `HX-Redirect: /eval/questions`; `POST /eval/questions/{id}/search` (`q`, `from`, `to`; a POST so the words stay out of URLs) → hits with "Chọn là đúng"; `POST /eval/questions/{id}/answers` (`moment_id`) and `POST /eval/questions/{id}/answers/{moment_id}/delete` → the list of correct moments.
  - `GET /eval/runs` (the run table; it polls itself every 15 s while a run waits or runs); `POST /eval/runs` (`max_gain_db`, `vad_threshold`, both optional) → the run table; the `/eval` page shows the run form and table. Jinja filter `pct`.

- [ ] **Step 1: Write the failing tests**

Apply to `brain/tests/test_eval_page.py`:

```diff
diff --git a/brain/tests/test_eval_page.py b/brain/tests/test_eval_page.py
--- a/brain/tests/test_eval_page.py
+++ b/brain/tests/test_eval_page.py
@@ -9,11 +9,12 @@ from helpers import (
     analysed_audio,
     captioned_keyframe,
     fetch_all,
+    insert_moment,
     insert_segment,
     paired_device,
 )
 from loopi_brain.app import create_app
-from loopi_brain.evaluation import golden
+from loopi_brain.evaluation import golden, questions
 
 HX = {"HX-Request": "true"}
 NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
@@ -154,3 +155,77 @@ async def test_golden_files_are_served_only_for_their_samples(pool, pki_settings
     assert (await web.get("/eval/audio/A1.opus")).status_code == 404
     with pytest.raises(ValueError):
         await golden.sample_exists(pool, "item", "A1")
+
+
+async def test_the_owner_writes_a_question_and_picks_its_correct_moments(pool, pki_settings, dev, web):
+    d = dev["device_id"]
+    market = await insert_moment(pool, d, kind="speech", ts=NINE, axis=0, text="Mai mình đi chợ <b>sớm</b>")
+    await insert_moment(pool, d, kind="visual", ts=NINE + dt.timedelta(hours=1), axis=3, text="A dark room")
+    form = {"text": "Khi nào đi chợ?", "from": "2026-10-14", "to": "2026-10-14"}
+    assert (await web.post("/eval/questions", data=form)).status_code == 403
+    created = await web.post("/eval/questions", data=form, headers=HX)
+    question_id = created.headers["HX-Redirect"].rsplit("/", 1)[1]
+    page = _flat((await web.get(f"/eval/questions/{question_id}")).text)
+    assert 'name="q" value="Khi nào đi chợ?"' in page and 'name="from" value="2026-10-14"' in page
+    assert "Chưa chọn moment đúng nào" in page
+    search = f"/eval/questions/{question_id}/search"
+    assert (await web.post(search, data={"q": "đi chợ"})).status_code == 403  # a POST, so no words in any URL
+    found = (await web.post(search, data={"q": "đi chợ", "from": "2026-10-14", "to": "2026-10-14"}, headers=HX)).text
+    assert "&lt;b&gt;sớm&lt;/b&gt;" in found and f'"moment_id": "{market}"' in found
+    picked = await web.post(f"/eval/questions/{question_id}/answers", data={"moment_id": market}, headers=HX)
+    assert "Moment đúng (1)" in picked.text and "&lt;b&gt;sớm" in picked.text
+    browsed = (await web.post(search, data={"q": "", "from": "2026-10-14", "to": "2026-10-14"}, headers=HX)).text
+    assert browsed.count("Chọn là đúng") == 2  # every moment of the day, in time order
+    assert "Chọn ngày" in (await web.post(search, data={"q": ""}, headers=HX)).text
+    assert (await web.post(search, data={"q": "x", "from": "2026-10-15", "to": "2026-10-14"},
+                           headers=HX)).status_code == 422
+    listing = _flat((await web.get("/eval/questions")).text)
+    assert "1/25 câu hỏi, 1 đã có moment đúng" in listing and "Khi nào đi chợ?</a>" in listing
+    dropped = await web.post(f"/eval/questions/{question_id}/answers/{market}/delete", headers=HX)
+    assert "Moment đúng (0)" in dropped.text
+    assert (await web.post(f"/eval/questions/{question_id}/answers", data={"moment_id": "01NOSUCH"},
+                           headers=HX)).status_code == 404
+    edited = await web.post(f"/eval/questions/{question_id}", data={"text": "Đi chợ lúc mấy giờ?", "from": "", "to": ""},
+                            headers=HX)
+    assert "Đã lưu câu hỏi." in edited.text
+    assert (await questions.get_question(pool, question_id))["first_day"] is None
+    assert (await web.post(f"/eval/questions/{question_id}", data={"text": " "}, headers=HX)).status_code == 422
+    for bad in ({"text": "x", "from": "2026-10-15", "to": "2026-10-14"}, {"text": " \x00 "}):
+        assert (await web.post("/eval/questions", data=bad, headers=HX)).status_code == 422
+    removed = await web.post(f"/eval/questions/{question_id}/delete", headers=HX)
+    assert removed.headers["HX-Redirect"] == "/eval/questions"
+    for gone in (web.get(f"/eval/questions/{question_id}"),
+                 web.post(f"/eval/questions/{question_id}/delete", headers=HX),
+                 web.post(f"/eval/questions/{question_id}", data={"text": "x"}, headers=HX)):
+        assert (await gone).status_code == 404
+
+
+async def test_a_run_is_started_from_the_page_and_its_numbers_are_shown(pool, pki_settings, dev, web, monkeypatch):
+    from helpers import FakeTranscriber, make_ctx, run_jobs
+    from loopi_brain.pipeline import asr
+    from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult
+
+    item = await _spoken(pool, pki_settings, dev["device_id"], 0, "vi", "Mai mình đi chợ")
+    await golden.add_audio(pool, pki_settings, item, golden.AudioLabel("vi", False, "Mai mình đi chợ nhé"))
+    missed = await questions.create_question(pool, "Phòng tối lúc nào?", None, None)
+    room = await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, axis=3, text="A dark room")
+    await questions.add_answer(pool, missed, room)
+    assert "Chưa chạy lần nào." in (await web.get("/eval")).text
+    assert (await web.post("/eval/runs", data={"vad_threshold": "2"}, headers=HX)).status_code == 422
+    assert (await web.post("/eval/runs", data={"max_gain_db": "loud"}, headers=HX)).status_code == 422
+    started = await web.post("/eval/runs", data={"max_gain_db": "", "vad_threshold": "0.4"}, headers=HX)
+    assert "đang chờ" in started.text and 'hx-trigger="every 15s"' in started.text and "vad_threshold=0.4" in started.text
+    again = await web.post("/eval/runs", data={}, headers=HX)
+    assert again.text.count("đang chờ") == 1  # one run at a time
+    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _threshold: [Region(0.0, 2.0)])
+    heard = RegionResult("vi", 0.9, [SegmentResult(0.0, 2.0, "Mai mình đi chợ", -0.3, 0.05, 1.2)])
+    await run_jobs(make_ctx(pool, pki_settings, transcriber=FakeTranscriber(heard)))
+    page = _flat((await web.get("/eval")).text)
+    assert "xong" in page and "20.0 % <span class=\"muted\">(1 đoạn)</span>" in page
+    assert "Recall (1 câu hỏi): @5 0.0 % (mục tiêu 70 %)" in page
+    assert "Chưa có moment đúng trong 5 kết quả đầu:</p><ul><li>Phòng tối lúc nào?</li>" in page
+    order = [page.index(f"<tr><td>{name}") for name in ("live", "none", "outro", "lone_phrase", "short_foreign", "all")]
+    assert order == sorted(order)
+    assert 'hx-trigger="every 15s"' not in page  # nothing running any more
+    fragment = await web.get("/eval/runs")  # what the page polls while a run waits or runs
+    assert fragment.text.startswith('<div id="runs">') and "xong" in fragment.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_eval_page.py -q`
Expected: FAIL — `2 failed, 7 passed`: the question routes do not exist (`assert 404 == 403`) and `/eval` has no run table (`assert 'Chưa chạy lần nào.' in …`).

- [ ] **Step 3: Add the routes**

```diff
diff --git a/brain/src/loopi_brain/web/eval_routes.py b/brain/src/loopi_brain/web/eval_routes.py
--- a/brain/src/loopi_brain/web/eval_routes.py
+++ b/brain/src/loopi_brain/web/eval_routes.py
@@ -2,15 +2,17 @@
 starts evaluation runs. Everything shown comes from the database and LOOPI_EVAL_DIR; nothing is logged with text."""
 from __future__ import annotations
 
+import datetime as dt
 import logging
 
 from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
 from fastapi.responses import FileResponse, HTMLResponse
 
-from .. import media
-from ..evaluation import golden, questions
+from .. import media, search
+from ..evaluation import golden, questions, runner
 from ..evaluation.golden import GoldenError
 from .routes import TEMPLATES, require_htmx
+from .search_routes import KIND_LABELS, OptionalDate, hit_view
 
 log = logging.getLogger(__name__)
 LANGUAGE_LABELS = {"vi": "Tiếng Việt", "ko": "Tiếng Hàn", "en": "Tiếng Anh", "": "Whisper không nghe thấy lời nào",
@@ -40,7 +42,7 @@ async def eval_page(request: Request):
     return TEMPLATES.TemplateResponse(request, "eval.html", {
         "p": await golden.progress(pool), "q": await questions.counts(pool), "suggested": golden.AUDIO_SUGGESTED,
         "audio_target": golden.AUDIO_TARGET, "caption_target": golden.CAPTION_TARGET,
-        "question_target": questions.QUESTION_TARGET,
+        "question_target": questions.QUESTION_TARGET, **await _runs_context(request),
     })
 
 
@@ -201,3 +203,147 @@ async def picture_file(request: Request, sample_id: str):
     if not await golden.sample_exists(request.app.state.pool, "eval_caption", sample_id):
         raise HTTPException(status_code=404, detail="unknown golden keyframe")
     return _eval_file(request, golden.keyframe_path(sample_id), "image/jpeg")
+
+
+# ---- questions -----------------------------------------------------------------------------------------------------
+
+def _days(first: dt.date | None, last: dt.date | None) -> tuple[dt.date | None, dt.date | None]:
+    if first and last and first > last:
+        raise HTTPException(status_code=422, detail="the first day must not be after the last")
+    return first, last
+
+
+@router.get("/eval/questions", response_class=HTMLResponse)
+async def eval_questions_page(request: Request):
+    pool = request.app.state.pool
+    return TEMPLATES.TemplateResponse(request, "eval_questions.html", {
+        "questions": await questions.list_questions(pool), "q": await questions.counts(pool),
+        "target": questions.QUESTION_TARGET,
+    })
+
+
+@router.post("/eval/questions", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
+async def new_question(request: Request, text: str = Form(..., max_length=2000),
+                       first: OptionalDate = Form(default=None, alias="from"),
+                       last: OptionalDate = Form(default=None, alias="to")):
+    try:
+        question_id = await questions.create_question(request.app.state.pool, text, *_days(first, last))
+    except ValueError as exc:
+        raise HTTPException(status_code=422, detail=str(exc)) from exc
+    return HTMLResponse("", headers={"HX-Redirect": f"/eval/questions/{question_id}"})
+
+
+async def _question_view(request: Request, question_id: str, template: str) -> HTMLResponse:
+    """The question page, or just its list of correct moments, each with its time and kind."""
+    question = await questions.get_question(request.app.state.pool, question_id)
+    if question is None:
+        raise HTTPException(status_code=404, detail="unknown question")
+    zone = request.app.state.settings.zone
+    answers = [{**a, "time": _local(a, "ts_start", zone), "kind_label": KIND_LABELS.get(a["kind"], a["kind"])}
+               for a in question["answers"]]
+    return TEMPLATES.TemplateResponse(request, template, {"question": question, "answers": answers})
+
+
+@router.get("/eval/questions/{question_id}", response_class=HTMLResponse)
+async def eval_question_page(request: Request, question_id: str):
+    return await _question_view(request, question_id, "eval_question.html")
+
+
+@router.post("/eval/questions/{question_id}", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
+async def edit_question(request: Request, question_id: str, text: str = Form(..., max_length=2000),
+                        first: OptionalDate = Form(default=None, alias="from"),
+                        last: OptionalDate = Form(default=None, alias="to")):
+    try:
+        found = await questions.update_question(request.app.state.pool, question_id, text, *_days(first, last))
+    except ValueError as exc:
+        raise HTTPException(status_code=422, detail=str(exc)) from exc
+    if not found:
+        raise HTTPException(status_code=404, detail="unknown question")
+    return HTMLResponse('<span class="online">Đã lưu câu hỏi.</span>')
+
+
+@router.post("/eval/questions/{question_id}/delete", response_class=HTMLResponse,
+             dependencies=[Depends(require_htmx)])
+async def remove_question(request: Request, question_id: str):
+    if not await questions.delete_question(request.app.state.pool, question_id):
+        raise HTTPException(status_code=404, detail="unknown question")
+    return HTMLResponse("", headers={"HX-Redirect": "/eval/questions"})
+
+
+# A POST, although it changes nothing: the words searched for (often the question itself) stay out of URLs and
+# so out of the access logs.
+@router.post("/eval/questions/{question_id}/search", response_class=HTMLResponse,
+             dependencies=[Depends(require_htmx)])
+async def question_search(
+    request: Request, question_id: str, q: str = Form(default="", max_length=2000),
+    first: OptionalDate = Form(default=None, alias="from"),
+    last: OptionalDate = Form(default=None, alias="to"),
+):
+    state = request.app.state
+    zone = state.settings.zone
+    hits = await questions.label_search(state.pool, search.request_router(state.settings, state.llm), q,
+                                        *_days(first, last), zone)
+    return TEMPLATES.TemplateResponse(request, "_eval_hits.html", {
+        "question_id": question_id, "hits": [hit_view(hit, zone) for hit in hits], "browsing": not search.clean_query(q),
+    })
+
+
+@router.post("/eval/questions/{question_id}/answers", response_class=HTMLResponse,
+             dependencies=[Depends(require_htmx)])
+async def pick_answer(request: Request, question_id: str, moment_id: str = Form(...)):
+    if not await questions.add_answer(request.app.state.pool, question_id, moment_id):
+        raise HTTPException(status_code=404, detail="unknown question or moment")
+    return await _question_view(request, question_id, "_eval_answers.html")
+
+
+@router.post("/eval/questions/{question_id}/answers/{moment_id}/delete", response_class=HTMLResponse,
+             dependencies=[Depends(require_htmx)])
+async def drop_answer(request: Request, question_id: str, moment_id: str):
+    await questions.remove_answer(request.app.state.pool, question_id, moment_id)
+    return await _question_view(request, question_id, "_eval_answers.html")
+
+
+# ---- runs ----------------------------------------------------------------------------------------------------------
+
+TEMPLATES.env.filters["pct"] = runner.pct
+
+
+async def _runs_context(request: Request) -> dict:
+    state = request.app.state
+    zone = state.settings.zone
+    runs = await runner.list_runs(state.pool)
+    texts = {q["id"]: q["text"] for q in await questions.list_questions(state.pool)}
+    views = []
+    for run in runs:
+        view = {**run, "time": _local(run, "requested_at", zone), "missed": []}
+        recall = (run["results"] or {}).get("recall", {})
+        for row in recall.get("per_question", []):
+            if not any(rank <= 5 for rank in row["ranks"]):
+                view["missed"].append(texts.get(row["id"], "(câu hỏi đã xoá)"))
+        views.append(view)
+    return {"runs": views, "active": any(r["status"] in ("waiting", "running") for r in runs),
+            "settings": state.settings, "wer_limit": runner.WER_LIMIT, "distances": runner.DISTANCES,
+            "similarities": runner.SIMILARITIES, "variants": runner.VARIANTS}
+
+
+@router.get("/eval/runs", response_class=HTMLResponse)
+async def runs_fragment(request: Request):
+    return TEMPLATES.TemplateResponse(request, "_eval_runs.html", await _runs_context(request))
+
+
+def _optional_float(value: str) -> float | None:
+    try:
+        return float(value) if value.strip() else None
+    except ValueError as exc:
+        raise HTTPException(status_code=422, detail="not a number") from exc
+
+
+@router.post("/eval/runs", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
+async def start_run(request: Request, max_gain_db: str = Form(""), vad_threshold: str = Form("")):
+    state = request.app.state
+    try:
+        await runner.start_run(state.pool, state.settings, max_gain_db=_optional_float(max_gain_db),
+                               vad_threshold=_optional_float(vad_threshold))
+    except ValueError as exc:
+        raise HTTPException(status_code=422, detail=str(exc)) from exc
+    return TEMPLATES.TemplateResponse(request, "_eval_runs.html", await _runs_context(request))
```

- [ ] **Step 4: Write the templates**

Create `brain/src/loopi_brain/web/templates/eval_questions.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Đánh giá · Câu hỏi</h1>
<p><a href="/eval">← Đánh giá</a> · {{ q.questions }}/{{ target }} câu hỏi, {{ q.answered }} đã có moment đúng</p>
<p class="muted">Viết câu hỏi như khi hỏi trang Tìm kiếm, rồi chọn (các) khoảnh khắc trả lời đúng câu đó. "Hôm qua",
  "sáng nay" trong câu hỏi: chọn ngày mà câu hỏi nói tới.</p>
<form class="card" hx-post="/eval/questions">
  <input name="text" required maxlength="500" size="60" placeholder="Ví dụ: Hôm qua mình hẹn đi đâu?">
  <label>Từ ngày <input type="date" name="from"></label>
  <label>đến ngày <input type="date" name="to"></label>
  <button>Thêm câu hỏi</button>
</form>
<table>
  <tr><th>Câu hỏi</th><th>Ngày</th><th>Moment đúng</th></tr>
  {% for row in questions %}
  <tr><td><a href="/eval/questions/{{ row.id }}">{{ row.text }}</a></td>
    <td>{% if row.first_day or row.last_day %}{{ row.first_day or "…" }} – {{ row.last_day or "…" }}{% else %}—{% endif %}</td>
    <td>{% if row.answers %}{{ row.answers }}{% else %}<span class="err">chưa chọn</span>{% endif %}</td></tr>
  {% else %}<tr><td colspan="3" class="muted">Chưa có câu hỏi nào.</td></tr>{% endfor %}
</table>
{% endblock %}
```

Create `brain/src/loopi_brain/web/templates/eval_question.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Đánh giá · Câu hỏi</h1>
<p><a href="/eval/questions">← Câu hỏi</a></p>
<form class="card" hx-post="/eval/questions/{{ question.id }}" hx-target="#saved">
  <input name="text" required maxlength="500" size="60" value="{{ question.text }}">
  <label>Từ ngày <input type="date" name="from" value="{{ question.first_day or '' }}"></label>
  <label>đến ngày <input type="date" name="to" value="{{ question.last_day or '' }}"></label>
  <button>Lưu</button> <span id="saved"></span>
  <button hx-post="/eval/questions/{{ question.id }}/delete" hx-confirm="Xoá câu hỏi này?">Xoá câu hỏi</button>
</form>
{% include "_eval_answers.html" %}
<form class="card" hx-post="/eval/questions/{{ question.id }}/search" hx-target="#hits">
  <h2>Tìm khoảnh khắc đúng</h2>
  <input type="search" name="q" value="{{ question.text }}" maxlength="500" size="50">
  <label>Từ ngày <input type="date" name="from" value="{{ question.first_day or '' }}"></label>
  <label>đến ngày <input type="date" name="to" value="{{ question.last_day or '' }}"></label>
  <button>Tìm</button>
  <p class="muted">Tìm rộng hơn trang Tìm kiếm (tối đa 50 kết quả). Để trống ô chữ và chọn ngày để xem mọi khoảnh khắc của ngày đó theo giờ.</p>
</form>
<div id="hits"></div>
{% endblock %}
```

Create `brain/src/loopi_brain/web/templates/_eval_answers.html`:

```html
<div id="answers">
  <h2>Moment đúng ({{ answers|length }})</h2>
  {% for a in answers %}
  <p class="hit"><time>{{ a.time }}</time> · {{ a.kind_label }} · {% if a.text is not none %}{{ a.text }}{% else %}<span class="muted">(đã xử lý lại; vẫn nhận ra theo nguồn)</span>{% endif %}
    <button hx-post="/eval/questions/{{ question.id }}/answers/{{ a.moment_id }}/delete" hx-target="#answers" hx-swap="outerHTML">Bỏ</button></p>
  {% else %}<p class="err">Chưa chọn moment đúng nào: câu hỏi này chưa được tính vào recall.</p>{% endfor %}
</div>
```

Create `brain/src/loopi_brain/web/templates/_eval_hits.html`:

```html
{% if hits %}
<ul class="hits">
  {% for h in hits %}
  <li class="hit">
    {% if h.keyframe_id %}<img src="/thumb/{{ h.keyframe_id }}.jpg" loading="lazy" alt="" width="160">{% endif %}
    <div><time>{{ h.time }}</time> · {{ h.kind_label }}{% if h.lang and h.kind == "speech" %} <span class="lang">{{ h.lang|upper }}</span>{% endif %}
      <p>{{ h.text }}</p>
      <button hx-post="/eval/questions/{{ question_id }}/answers" hx-vals='{"moment_id": "{{ h.id }}"}' hx-target="#answers" hx-swap="outerHTML">Chọn là đúng</button></div>
  </li>
  {% endfor %}
</ul>
{% elif browsing %}<p class="muted">Chọn ngày để xem mọi khoảnh khắc của ngày đó, hoặc nhập từ để tìm.</p>
{% else %}<p>Không tìm thấy khoảnh khắc nào.</p>{% endif %}
```

Create `brain/src/loopi_brain/web/templates/_eval_runs.html`:

```html
<div id="runs"{% if active %} hx-get="/eval/runs" hx-trigger="every 15s" hx-swap="outerHTML"{% endif %}>
  <table>
    <tr><th>Lần chạy</th><th>Trạng thái</th><th>Tiếng Việt (WER)</th><th>Tiếng Hàn (CER)</th><th>Tiếng Anh (WER)</th>
      <th>Không lời: bịa câu</th><th>Recall@5</th></tr>
    {% for r in runs %}
    {% set live = r.results.asr.variants.live if r.results else none %}
    <tr>
      <td>{{ r.time }}{% if r.config.overrides %}<br><span class="muted">{% for k, v in r.config.overrides.items() %}{{ k }}={{ v }} {% endfor %}</span>{% endif %}</td>
      <td>{% if r.status == "done" %}xong{% elif r.status == "failed" %}<span class="err">lỗi: {{ r.error }}</span>{% elif r.status == "running" %}đang chạy ({{ r.heard }}/{{ r.clips }} đoạn){% else %}đang chờ{% endif %}</td>
      {% for lang in ("vi", "ko", "en") %}
      {% set row = live.languages.get(lang) if live else none %}
      <td>{% if row %}{% if row.over_limit %}<strong class="err">{{ row.score|pct }}</strong>{% else %}{{ row.score|pct }}{% endif %} <span class="muted">({{ row.clips }} đoạn)</span>{% else %}—{% endif %}</td>
      {% endfor %}
      <td>{% if live and live.no_speech.clips %}{{ live.no_speech.invented }}/{{ live.no_speech.clips }}{% else %}—{% endif %}</td>
      <td>{% if r.results and r.results.recall.at5 is defined %}{{ r.results.recall.at5|pct }}{% elif r.results %}<span class="muted">chưa đo</span>{% else %}—{% endif %}</td>
    </tr>
    {% if r.results %}
    <tr><td colspan="7"><details><summary>Chi tiết</summary>
      <p>ASR {{ r.config.models.asr if r.config.models else "" }}, gain ≤ {{ r.config.max_gain_db }} dB, VAD {{ r.config.vad_threshold }},
        luật đang bật: {{ r.config.rules|join(", ") or "không" }}. Lỗi trên {{ (wer_limit * 100)|round|int }} % thì phải đổi model hoặc cấu hình (§14).</p>
      <table><tr><th>Luật lọc</th><th>vi</th><th>ko</th><th>en</th><th>Không lời: bịa câu (số từ)</th></tr>
      {% for name in variants if name in r.results.asr.variants %}{% set v = r.results.asr.variants[name] %}
        <tr><td>{{ name }}{% if name == "live" %} ({{ v.rules|join(", ") or "không" }}){% endif %}</td>
        {% for lang in ("vi", "ko", "en") %}{% set row = v.languages.get(lang) %}<td>{{ row.score|pct if row else "—" }}</td>{% endfor %}
        <td>{{ v.no_speech.invented }}/{{ v.no_speech.clips }} ({{ v.no_speech.words }})</td></tr>
      {% endfor %}</table>
      <p>Mô tả khung hình: {% for model, c in r.results.captions.items() %}{{ model }}: {{ c.scored }} khung hình, điểm trung bình {{ "%.2f"|format(c.mean) }}, ≥ 4: {{ c.good }}; sai số người {{ c.wrong_people }}, bịa đồ vật {{ c.invented_objects }}, sai hoạt động {{ c.wrong_activity }}. {% else %}chưa chấm. {% endfor %}</p>
      {% set rc = r.results.recall %}
      {% if rc.at5 is defined %}
      <p>Recall ({{ rc.questions }} câu hỏi): @5 {{ rc.at5|pct }} (mục tiêu 70 %), @10 {{ rc.at10|pct }}, hit@5 {{ rc.hit5|pct }}, MRR {{ "%.2f"|format(rc.mrr) }}{% if not rc.semantic %} — chỉ tìm theo chữ{% endif %}.</p>
      <table><tr><th>Khoảng cách tối đa</th>{% for s in similarities %}<th>độ giống chữ ≥ {{ s }}</th>{% endfor %}</tr>
      {% for d in distances %}<tr><td>{{ d }}</td>{% for g in rc.grid if g.max_distance == d %}<td>{{ g.at5|pct }}</td>{% endfor %}</tr>{% endfor %}</table>
      {% if r.missed %}<p>Chưa có moment đúng trong 5 kết quả đầu:</p><ul>{% for text in r.missed %}<li>{{ text }}</li>{% endfor %}</ul>{% endif %}
      {% else %}<p>Recall: chưa đo ({{ rc.error or "chưa có câu hỏi nào có moment đúng" }}).</p>{% endif %}
    </details></td></tr>
    {% endif %}
    {% else %}<tr><td colspan="7" class="muted">Chưa chạy lần nào.</td></tr>{% endfor %}
  </table>
</div>
```

Apply to `brain/src/loopi_brain/web/templates/eval.html`:

```diff
diff --git a/brain/src/loopi_brain/web/templates/eval.html b/brain/src/loopi_brain/web/templates/eval.html
--- a/brain/src/loopi_brain/web/templates/eval.html
+++ b/brain/src/loopi_brain/web/templates/eval.html
@@ -16,4 +16,15 @@
   <h2><a href="/eval/questions">Câu hỏi</a>: {{ q.questions }}/{{ question_target }} câu</h2>
   <p>Đã chọn moment đúng: {{ q.answered }}</p>
 </section>
+<section class="card" id="evaluation-runs">
+  <h2>Chạy đánh giá</h2>
+  <form hx-post="/eval/runs" hx-target="#runs" hx-swap="outerHTML">
+    <label>Gain tối đa (dB) <input type="number" name="max_gain_db" min="0" max="60" step="0.5" placeholder="{{ settings.asr_max_gain_db }}"></label>
+    <label>Ngưỡng VAD <input type="number" name="vad_threshold" min="0.05" max="0.95" step="0.05" placeholder="{{ settings.asr_vad_threshold }}"></label>
+    <button>Chạy đánh giá</button>
+  </form>
+  <p class="muted">Nhận dạng lại mọi đoạn trong bộ đánh giá bằng đường ASR hiện tại (để trống = cấu hình đang chạy), chấm mọi bộ
+    luật lọc, tổng hợp điểm mô tả và đo recall. Worker làm từng đoạn một, sau mọi việc đang chờ, nên mất vài phút.</p>
+  {% include "_eval_runs.html" %}
+</section>
 {% endblock %}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_eval_page.py -q`
Expected: `9 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `762 passed, 1 skipped`, `TOTAL … 99%`.

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/web/eval_routes.py brain/src/loopi_brain/web/templates/eval.html \
  brain/src/loopi_brain/web/templates/eval_questions.html brain/src/loopi_brain/web/templates/eval_question.html \
  brain/src/loopi_brain/web/templates/_eval_answers.html brain/src/loopi_brain/web/templates/_eval_hits.html \
  brain/src/loopi_brain/web/templates/_eval_runs.html brain/tests/test_eval_page.py
git commit -F - <<'EOF'
feat(brain): Đánh giá: the owner's questions and their correct moments, and evaluation runs with their numbers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 9: `loopi-brain acceptance` — the spec §14 P2 numbers

**Files:**
- Create: `brain/src/loopi_brain/acceptance.py`
- Modify: `brain/src/loopi_brain/cli.py`
- Test: `brain/tests/test_acceptance.py` (new)

**Interfaces:**
- Consumes: Task 6's `runner.latest_run(pool, finished=True)`, `runner.pct`, `runner.asr_numbers` (tests); P2b `days.day_bounds_utc`, `days.local_now`; tables `keyframe`, `item`, `moment`, `day_recap`, `transcript_segment`.
- Produces (`loopi_brain.acceptance`): `LATENCY_TARGET_S = 900`, `DAY_HOURS = (8, 22)`; `window(zone, days, now=None) -> (dates, start, end)` (the last `days` complete local days; ValueError below 1); `latency(pool, zone, days, now=None) -> {"days", "video", "audio", "video_by_day", "audio_by_day"}` (each `{"n", "avg", "p95"}` in seconds); `recaps(pool, zone, days, now=None) -> [{"day", "moments", "recap_at", "stale"}]`; `per_day(pool, zone, days, now=None) -> [{"day", "keyframes", "speech_min"}]`; `CpuSample(service, at, usage_usec, started)`; `parse_cpu(text) -> (samples, peaks)`; `cpu_per_day(samples) -> {service: {"first", "last", "hours", "restarted", "cpu_s", "per_day_s"}}`; `report(pool, zone, days, samples_text=None, now=None) -> str` (Markdown). CLI: `loopi-brain acceptance [--days 7] [--cpu-samples FILE|-]` (exit 2 for `--days` < 1).

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_acceptance.py`:

```python
import datetime as dt
import io
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.types.json import Jsonb

from helpers import insert_item, insert_keyframe, insert_moment, insert_segment, paired_device
from loopi_brain import acceptance, cli
from loopi_brain.evaluation import runner

SEOUL = ZoneInfo("Asia/Seoul")
NOW = dt.datetime(2026, 10, 17, 10, 0, tzinfo=SEOUL)  # the window is 15 and 16 October (complete days)
CPU = """\
worker 2026-10-15T19:28:10Z usage_usec 47672930298 started=2026-10-15T14:51:34.148257075Z
models 2026-10-15T19:28:10Z usage_usec 11584004465 started=2026-10-15T13:42:15.15953307Z
worker memory.peak 4498079744
models memory.peak 7954718720
worker 2026-10-16T19:28:10Z usage_usec 90872930298 started=2026-10-15T14:51:34.148257075Z
models 2026-10-16T19:28:10Z usage_usec 21584004465 started=2026-10-16T01:00:00Z
worker memory.peak 4600000000
"""


def local(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=SEOUL)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _captioned(pool, settings, device_id, ended: dt.datetime, delay_s: float) -> None:
    item = await insert_item(pool, device_id, kind="video", started_at=ended - dt.timedelta(seconds=60))
    kf = await insert_keyframe(pool, settings, item, device_id, ended - dt.timedelta(seconds=30))
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = '{}', captioned_at = %s WHERE id = %s",
                           (ended + dt.timedelta(seconds=delay_s), kf))


async def _transcribed(pool, device_id, ended: dt.datetime, delay_s: float) -> str:
    item = await insert_item(pool, device_id, kind="audio", started_at=ended - dt.timedelta(seconds=60))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done', analyzed_at = %s WHERE id = %s",
                           (ended + dt.timedelta(seconds=delay_s), item))
    return item


async def _evaluated(pool) -> None:
    def clip(lang, reference, heard):
        return {"no_speech": False, "lang": lang, "reference": reference, "segments": [
            {"start": 0, "end": 2, "lang": lang, "lang_prob": 0.9, "segments": [
                {"start": 0, "end": 2, "text": heard, "avg_logprob": -0.3, "no_speech_prob": 0.0,
                 "compression_ratio": 1.2}]}]}

    clips = [clip("vi", "một hai ba bốn", "một hai ba"), clip("ko", "네 알겠어요", "네 알겠어요")]
    results = {
        "asr": {"clips": 2, "failed": 0, "variants": {"live": {"rules": [], **runner.asr_numbers(clips, [])}}},
        "captions": {"ollama:minicpm-v4.6:1b": {"scored": 30, "mean": 3.5, "good": 18, "wrong_people": 2,
                                                "invented_objects": 4, "wrong_activity": 1}},
        "recall": {"questions": 25, "at5": 0.72, "at10": 0.8, "hit5": 0.84, "mrr": 0.61, "semantic": True,
                   "meets_target": True, "per_question": [], "grid": []},
    }
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO eval_run (id, status, config, results, requested_at) VALUES"
                           " ('R1', 'done', '{}', %s, %s)", (Jsonb(results), NOW - dt.timedelta(days=1)))
        await conn.execute("INSERT INTO eval_run (id, status, config, requested_at) VALUES ('R2', 'running', '{}', %s)",
                           (NOW,))  # newer, but not finished: the report uses R1


async def test_the_latency_counts_recordings_that_ended_between_8_and_22(pool, pki_settings, dev):
    d = dev["device_id"]
    await _captioned(pool, pki_settings, d, local(15, 9), 120)
    await _captioned(pool, pki_settings, d, local(15, 12), 240)
    await _captioned(pool, pki_settings, d, local(15, 23), 3600)  # at night: not counted
    await _captioned(pool, pki_settings, d, local(15, 7, 30), 3600)
    await _captioned(pool, pki_settings, d, local(17, 9), 3600)  # today: not a complete day
    await _transcribed(pool, d, local(16, 10), 30)
    await _transcribed(pool, d, local(16, 21, 59), 90)
    found = await acceptance.latency(pool, SEOUL, 2, now=NOW)
    assert found["days"] == [dt.date(2026, 10, 15), dt.date(2026, 10, 16)]
    assert (found["video"]["n"], found["video"]["avg"], found["video"]["p95"]) == (2, 180.0, pytest.approx(234.0))
    assert (found["audio"]["n"], found["audio"]["avg"], found["audio"]["p95"]) == (2, 60.0, pytest.approx(87.0))
    assert set(found["video_by_day"]) == {dt.date(2026, 10, 15)} and set(found["audio_by_day"]) == {dt.date(2026, 10, 16)}
    with pytest.raises(ValueError):
        acceptance.window(SEOUL, 0)


async def test_every_evening_with_recordings_should_have_its_recap(pool, pki_settings, dev):
    d = dev["device_id"]
    await insert_moment(pool, d, kind="speech", ts=local(15, 9), text="x")
    await insert_moment(pool, d, kind="visual", ts=local(16, 9), text="y")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version, generated_at) VALUES"
                           " ('2026-10-15', 'x', 'test', %s)", (local(15, 23, 31),))
    rows = await acceptance.recaps(pool, SEOUL, 2, now=NOW)
    assert [(r["day"], r["moments"], r["recap_at"]) for r in rows] == [
        (dt.date(2026, 10, 15), 1, local(15, 23, 31)), (dt.date(2026, 10, 16), 1, None)]
    lines = acceptance._recap_lines(rows)
    assert "| 2026-10-15 | 1 | 2026-10-15 23:31 |" in lines and "| 2026-10-16 | 1 | **none** |" in lines
    assert lines[-1] == "Days with recordings: 2; with a recap: 1 (missing: 2026-10-16)"


def test_cpu_time_per_day_comes_from_two_samples_of_one_container_life():
    samples, peaks = acceptance.parse_cpu(CPU + "garbage line\n")
    assert len(samples) == 4 and peaks == {"worker": 4600000000, "models": 7954718720}
    per_day = acceptance.cpu_per_day(samples)
    assert per_day["worker"]["hours"] == 24.0 and per_day["worker"]["cpu_s"] == pytest.approx(43200.0)
    assert per_day["worker"]["per_day_s"] == pytest.approx(43200.0)
    assert per_day["models"]["restarted"] is True and per_day["models"]["cpu_s"] is None
    one = acceptance.cpu_per_day(acceptance.parse_cpu(CPU.splitlines()[0])[0])
    assert one["worker"]["cpu_s"] is None and one["worker"]["restarted"] is False
    lines = acceptance._cpu_lines(CPU)
    assert any(line.startswith("| worker | 2026-10-15 19:28 → 2026-10-16 19:28 UTC (24.0 h) | 12.0 CPU-h | "
                               "43200 s (12.0 CPU-h) | 4.60 GB |") for line in lines)
    assert any("| models |" in line and "restarted in between" in line for line in lines)
    assert acceptance._cpu_lines(None)[-1] == "No CPU samples given (`--cpu-samples`)."
    assert acceptance._cpu_lines("nothing here")[-1] == "No CPU sample lines found."
    assert "needs two samples" in "".join(acceptance._cpu_lines(CPU.splitlines()[0]))


async def test_the_report_holds_the_numbers_and_never_the_text(pool, pki_settings, dev):
    d = dev["device_id"]
    await _captioned(pool, pki_settings, d, local(16, 9), 60)
    audio = await _transcribed(pool, d, local(16, 10), 30)
    await insert_segment(pool, audio, d, local(16, 9, 59), "bí mật của nhà mình", seconds=120)
    await insert_moment(pool, d, kind="speech", ts=local(16, 9, 59), text="bí mật của nhà mình")
    assert "No finished evaluation run yet" in await acceptance.report(pool, SEOUL, 2, now=NOW)
    await _evaluated(pool)
    text = await acceptance.report(pool, SEOUL, 2, CPU, now=NOW)
    assert text.startswith("## P2 acceptance numbers (spec §14) — 2026-10-15 … 2026-10-16 (2 days, Asia/Seoul)")
    assert "| video → caption | 1 | 1.0 min | 1.0 min | met |" in text
    assert "| audio → transcript | 1 | 0.5 min | 0.5 min | met |" in text
    assert "### ASR (evaluation run R1," in text
    assert "| vi | 1 | WER | 25.0 % | yes |" in text and "| ko | 1 | CER | 0.0 % | yes |" in text
    assert "| en | 0 | — | — | no clip |" in text
    assert "- ollama:minicpm-v4.6:1b: 30 keyframes, mean 3.50/5, ≥ 4: 18" in text
    assert "72.0 % over 25 questions (met); @10 80.0 %, hit@5 84.0 %, MRR 0.61" in text
    assert "| 2026-10-16 | 1 | 2 min |" in text  # keyframes and speech of the day
    assert "bí mật" not in text and "một hai" not in text  # numbers only


def _rows(database_url) -> int:
    with psycopg.connect(database_url) as conn:
        return conn.execute("SELECT (SELECT count(*) FROM eval_run) + (SELECT count(*) FROM item)").fetchone()[0]


def test_the_cli_prints_the_report(database_url, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    before = _rows(database_url)
    samples = tmp_path / "cpu.txt"
    samples.write_text(CPU)
    assert cli.main(["acceptance", "--days", "1", "--cpu-samples", str(samples)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("## P2 acceptance numbers") and "| worker |" in out
    monkeypatch.setattr("sys.stdin", io.StringIO(CPU))
    assert cli.main(["acceptance", "--cpu-samples", "-"]) == 0 and "| models |" in capsys.readouterr().out
    assert cli.main(["acceptance", "--days", "0"]) == 2
    assert _rows(database_url) == before  # read only


def test_what_the_last_run_could_not_measure_is_said():
    run = {"id": "R", "requested_at": NOW, "results": {
        "asr": {"variants": {"live": {"rules": ["outro"], "languages": {},
                                      "no_speech": {"clips": 0, "invented": 0, "words": 0, "rate": None}}}},
        "captions": {}, "recall": {"questions": 0}}}
    lines = acceptance._evaluation_lines(run, SEOUL)
    assert lines[0] == "### ASR (evaluation run R, 2026-10-17 10:00; rules: outro)"
    assert "- none scored" in lines and lines[-1] == "Not measured: no question with a correct moment"
    assert "No-speech clips: 0, with invented text: 0 (—, 0 words)" in lines
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_acceptance.py -q`
Expected: FAIL — `1 error` during collection: `ImportError: cannot import name 'acceptance' from 'loopi_brain'`.

- [ ] **Step 3: Write the acceptance module**

Create `brain/src/loopi_brain/acceptance.py`:

```python
"""The P2 acceptance numbers (spec §14 P2), from the brain's database and the CPU samples of a measurement window.

- Latency from recording to caption (a video segment's end → its keyframe's caption) and to transcript (an audio
  segment's end → its transcription) for recordings that ended between 08:00 and 22:00 local time, over the last
  N complete local days: average and p95, overall and per day. Target: average ≤ 15 minutes.
- A recap every evening: for each of those days with recorded moments, whether its recap exists and when it was
  written.
- WER/CER per language (the 30 % rule) and recall@5 (target 70 %) from the newest finished evaluation run.
- CPU time per day from two cgroup samples per service, taken on the host as the P2a measurement window did
  (``<service> <UTC time> usage_usec <n> started=<container start>`` and ``<service> memory.peak <bytes>`` lines).
- For context (spec §9.6): keyframes and minutes of transcribed speech per day.

The report holds numbers only: never a transcript, a caption or a question.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from .days import day_bounds_utc, local_now
from .evaluation import runner

LATENCY_TARGET_S = 15 * 60  # spec §14 P2: average ≤ 15 minutes, 08:00–22:00
DAY_HOURS = (8, 22)
_IN_HOURS = (
    "{ended} >= %(start)s AND {ended} < %(end)s"
    " AND extract(hour FROM {ended} AT TIME ZONE %(tz)s) >= %(first_hour)s"
    " AND extract(hour FROM {ended} AT TIME ZONE %(tz)s) < %(last_hour)s"
)
VIDEO_DELAYS = (
    "SELECT (i.ended_at AT TIME ZONE %(tz)s)::date AS day, extract(epoch FROM k.captioned_at - i.ended_at)::float8 AS s"
    " FROM keyframe k JOIN item i ON i.id = k.item_id WHERE k.captioned_at IS NOT NULL AND "
    + _IN_HOURS.format(ended="i.ended_at")
)
AUDIO_DELAYS = (
    "SELECT (ended_at AT TIME ZONE %(tz)s)::date AS day, extract(epoch FROM analyzed_at - ended_at)::float8 AS s"
    " FROM item WHERE kind = 'audio' AND analysis_state = 'done' AND analyzed_at IS NOT NULL AND "
    + _IN_HOURS.format(ended="ended_at")
)
_SUMMARY = (
    "SELECT {by} count(*) AS n, avg(s) AS avg, percentile_cont(0.95) WITHIN GROUP (ORDER BY s) AS p95"
    " FROM ({delays}) t {group}"
)


def window(zone, days: int, now: dt.datetime | None = None) -> tuple[list[dt.date], dt.datetime, dt.datetime]:
    """The last ``days`` complete local days before today, and their UTC bounds."""
    if days < 1:
        raise ValueError("at least one day")
    today = local_now(zone, now).date()
    dates = [today - dt.timedelta(days=n) for n in range(days, 0, -1)]
    return dates, day_bounds_utc(dates[0], zone)[0], day_bounds_utc(dates[-1], zone)[1]


async def latency(pool: AsyncConnectionPool, zone, days: int, now: dt.datetime | None = None) -> dict:
    """Recording → caption ("video") and recording → transcript ("audio") delays in seconds, overall and per day."""
    dates, start, end = window(zone, days, now)
    params = {"tz": str(zone), "start": start, "end": end, "first_hour": DAY_HOURS[0], "last_hour": DAY_HOURS[1]}
    out: dict = {"days": dates}
    async with pool.connection() as conn:
        for name, delays in (("video", VIDEO_DELAYS), ("audio", AUDIO_DELAYS)):
            cur = await conn.execute(_SUMMARY.format(by="", delays=delays, group=""), params)
            out[name] = await cur.fetchone()
            cur = await conn.execute(_SUMMARY.format(by="day,", delays=delays, group="GROUP BY day ORDER BY day"),
                                     params)
            out[f"{name}_by_day"] = {r["day"]: r for r in await cur.fetchall()}
    return out


async def recaps(pool: AsyncConnectionPool, zone, days: int, now: dt.datetime | None = None) -> list[dict]:
    """For each day of the window: its moments, and its recap's local writing time (None: no recap)."""
    dates, start, end = window(zone, days, now)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT (ts_start AT TIME ZONE %s)::date AS day, count(*) AS n FROM moment"
            " WHERE ts_start >= %s AND ts_start < %s GROUP BY 1",
            (str(zone), start, end),
        )
        moments = {r["day"]: r["n"] for r in await cur.fetchall()}
        cur = await conn.execute("SELECT day, generated_at, stale FROM day_recap WHERE day = ANY(%s)", (dates,))
        written = {r["day"]: r for r in await cur.fetchall()}
    return [{"day": day, "moments": moments.get(day, 0),
             "recap_at": written[day]["generated_at"].astimezone(zone) if day in written else None,
             "stale": written[day]["stale"] if day in written else False}
            for day in dates]


async def per_day(pool: AsyncConnectionPool, zone, days: int, now: dt.datetime | None = None) -> list[dict]:
    """Keyframes and minutes of transcribed speech of each day of the window (spec §9.6's estimates)."""
    dates, start, end = window(zone, days, now)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT (ts AT TIME ZONE %s)::date AS day, count(*) AS n FROM keyframe WHERE ts >= %s AND ts < %s"
            " GROUP BY 1", (str(zone), start, end))
        keyframes = {r["day"]: r["n"] for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT (start_ts AT TIME ZONE %s)::date AS day, sum(extract(epoch FROM end_ts - start_ts))::float8 AS s"
            " FROM transcript_segment WHERE start_ts >= %s AND start_ts < %s GROUP BY 1", (str(zone), start, end))
        speech = {r["day"]: r["s"] for r in await cur.fetchall()}
    return [{"day": day, "keyframes": keyframes.get(day, 0), "speech_min": speech.get(day, 0.0) / 60} for day in dates]


@dataclass(frozen=True)
class CpuSample:
    service: str
    at: dt.datetime
    usage_usec: int
    started: str


def parse_cpu(text: str) -> tuple[list[CpuSample], dict[str, int]]:
    """The samples and the newest ``memory.peak`` (bytes) per service; other lines are ignored."""
    samples, peaks = [], {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[2] == "usage_usec" and parts[4].startswith("started="):
            samples.append(CpuSample(parts[0], dt.datetime.fromisoformat(parts[1]), int(parts[3]),
                                     parts[4].split("=", 1)[1]))
        elif len(parts) == 3 and parts[1] == "memory.peak" and parts[2].isdigit():
            peaks[parts[0]] = int(parts[2])
    return samples, peaks


def cpu_per_day(samples: list[CpuSample]) -> dict[str, dict]:
    """Per service, from its first to its last sample: hours between them, CPU seconds used, CPU seconds per day.
    A container that restarted in between (another start time) has no usable number."""
    out: dict[str, dict] = {}
    for service in dict.fromkeys(s.service for s in samples):
        mine = sorted((s for s in samples if s.service == service), key=lambda s: s.at)
        first, last = mine[0], mine[-1]
        hours = (last.at - first.at).total_seconds() / 3600
        row = {"first": first.at, "last": last.at, "hours": hours, "restarted": first.started != last.started}
        if len(mine) < 2 or hours <= 0 or row["restarted"]:
            out[service] = {**row, "cpu_s": None, "per_day_s": None}
            continue
        cpu_s = (last.usage_usec - first.usage_usec) / 1e6
        out[service] = {**row, "cpu_s": cpu_s, "per_day_s": cpu_s / hours * 24}
    return out


def _minutes(seconds: float | None) -> str:
    return "—" if seconds is None else f"{seconds / 60:.1f} min"


def _latency_lines(found: dict) -> list[str]:
    lines = [f"### Latency, recordings that ended {DAY_HOURS[0]:02d}:00–{DAY_HOURS[1]:02d}:00 "
             "(target: average ≤ 15 min)", "",
             "| Path | Recordings | Average | p95 | Target |", "|---|---|---|---|---|"]
    for name, label in (("video", "video → caption"), ("audio", "audio → transcript")):
        row = found[name]
        verdict = "—" if not row["n"] else ("met" if row["avg"] <= LATENCY_TARGET_S else "not met")
        lines.append(f"| {label} | {row['n']} | {_minutes(row['avg'])} | {_minutes(row['p95'])} | {verdict} |")
    lines += ["", "| Day | video avg | video p95 | audio avg | audio p95 |", "|---|---|---|---|---|"]
    for day in found["days"]:
        video, audio = found["video_by_day"].get(day), found["audio_by_day"].get(day)
        lines.append(f"| {day} | {_minutes(video and video['avg'])} | {_minutes(video and video['p95'])} | "
                     f"{_minutes(audio and audio['avg'])} | {_minutes(audio and audio['p95'])} |")
    return lines


def _recap_lines(rows: list[dict]) -> list[str]:
    lines = ["### A recap every evening", "", "| Day | Moments | Recap written |", "|---|---|---|"]
    for row in rows:
        written = row["recap_at"].strftime("%Y-%m-%d %H:%M") if row["recap_at"] else "**none**"
        lines.append(f"| {row['day']} | {row['moments']} | {written}{' (stale)' if row['stale'] else ''} |")
    with_data = [r for r in rows if r["moments"]]
    missing = [str(r["day"]) for r in with_data if r["recap_at"] is None]
    lines += ["", f"Days with recordings: {len(with_data)}; with a recap: {len(with_data) - len(missing)}"
                  f" (missing: {', '.join(missing) or 'none'})"]
    return lines


def _evaluation_lines(run: dict | None, zone) -> list[str]:
    if run is None:
        return ["### Evaluation", "", "No finished evaluation run yet (`loopi-brain eval run`)."]
    results = run["results"]
    live = results["asr"]["variants"]["live"]
    when = run["requested_at"].astimezone(zone).strftime("%Y-%m-%d %H:%M")
    lines = [f"### ASR (evaluation run {run['id']}, {when}; rules: {', '.join(live['rules']) or 'none'})", "",
             "| Language | Clips | Metric | Rate | ≤ 30 % |", "|---|---|---|---|---|"]
    for lang in ("vi", "ko", "en"):
        row = live["languages"].get(lang)
        if row is None:
            lines.append(f"| {lang} | 0 | — | — | no clip |")
            continue
        lines.append(f"| {lang} | {row['clips']} | {row['metric'].upper()} | {runner.pct(row['score'])} | "
                     f"{'no — change the model or the configuration' if row['over_limit'] else 'yes'} |")
    silent = live["no_speech"]
    lines += ["", f"No-speech clips: {silent['clips']}, with invented text: {silent['invented']} "
                  f"({runner.pct(silent['rate'])}, {silent['words']} words)", "", "### Captions (owner's scores)", ""]
    lines += [f"- {model}: {row['scored']} keyframes, mean {row['mean']:.2f}/5, ≥ 4: {row['good']}"
              for model, row in results["captions"].items()] or ["- none scored"]
    recall = results["recall"]
    lines += ["", "### Recall@5 (target ≥ 70 %)", ""]
    if "at5" in recall:
        lines.append(f"{runner.pct(recall['at5'])} over {recall['questions']} questions "
                     f"({'met' if recall['meets_target'] else 'not met'}); @10 {runner.pct(recall['at10'])}, "
                     f"hit@5 {runner.pct(recall['hit5'])}, MRR {recall['mrr']:.2f}")
    else:
        lines.append(f"Not measured: {recall.get('error', 'no question with a correct moment')}")
    return lines


def _cpu_lines(samples_text: str | None) -> list[str]:
    lines = ["### CPU time per day", ""]
    if samples_text is None:
        return [*lines, "No CPU samples given (`--cpu-samples`)."]
    samples, peaks = parse_cpu(samples_text)
    if not samples:
        return [*lines, "No CPU sample lines found."]
    lines += ["| Service | Window | CPU time | CPU per day | Memory peak |", "|---|---|---|---|---|"]
    for service, row in cpu_per_day(samples).items():
        span = f"{row['first']:%Y-%m-%d %H:%M} → {row['last']:%Y-%m-%d %H:%M} UTC ({row['hours']:.1f} h)"
        peak = f"{peaks[service] / 1e9:.2f} GB" if service in peaks else "—"
        if row["cpu_s"] is None:
            why = "restarted in between" if row["restarted"] else "needs two samples"
            lines.append(f"| {service} | {span} | — ({why}) | — | {peak} |")
        else:
            lines.append(f"| {service} | {span} | {row['cpu_s'] / 3600:.1f} CPU-h | "
                         f"{row['per_day_s']:.0f} s ({row['per_day_s'] / 3600:.1f} CPU-h) | {peak} |")
    return lines


async def report(pool: AsyncConnectionPool, zone, days: int, samples_text: str | None = None,
                 now: dt.datetime | None = None) -> str:
    """The P2 acceptance numbers as Markdown."""
    dates, _start, _end = window(zone, days, now)
    lines = [f"## P2 acceptance numbers (spec §14) — {dates[0]} … {dates[-1]} ({days} days, {zone})", ""]
    lines += _latency_lines(await latency(pool, zone, days, now)) + [""]
    lines += _recap_lines(await recaps(pool, zone, days, now)) + [""]
    lines += _evaluation_lines(await runner.latest_run(pool, finished=True), zone) + [""]
    lines += _cpu_lines(samples_text) + [""]
    lines += ["### Context (spec §9.6: 100–300 keyframes and 1–3 h of speech a day)", "",
              "| Day | Keyframes | Speech |", "|---|---|---|"]
    lines += [f"| {r['day']} | {r['keyframes']} | {r['speech_min']:.0f} min |" for r in await per_day(pool, zone, days, now)]
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Add the command**

```diff
diff --git a/brain/src/loopi_brain/cli.py b/brain/src/loopi_brain/cli.py
--- a/brain/src/loopi_brain/cli.py
+++ b/brain/src/loopi_brain/cli.py
@@ -38,6 +38,9 @@ def build_parser() -> argparse.ArgumentParser:
     run.add_argument("--timeout", type=float, default=3 * 3600, help="seconds to wait (default 3 h)")
     show = eval_sub.add_parser("show", help="print the numbers of a run (default: the newest)")
     show.add_argument("run_id", nargs="?")
+    acceptance = sub.add_parser("acceptance", help="print the P2 acceptance numbers (spec §14) as Markdown")
+    acceptance.add_argument("--days", type=int, default=7, help="complete local days before today (default 7)")
+    acceptance.add_argument("--cpu-samples", help="the cgroup CPU samples of the measurement window ('-': stdin)")
     return parser
 
 
@@ -161,6 +164,29 @@ async def _eval(args: argparse.Namespace) -> int:
         await pool.close()
 
 
+async def _acceptance(args: argparse.Namespace) -> int:
+    from .acceptance import report
+    from .config import Settings
+    from .db import make_pool
+
+    if args.days < 1:
+        print("error: --days must be at least 1", file=sys.stderr)
+        return 2
+    samples = None
+    if args.cpu_samples == "-":
+        samples = sys.stdin.read()
+    elif args.cpu_samples:
+        samples = Path(args.cpu_samples).read_text()
+    settings = Settings()
+    pool = make_pool(settings.database_url)
+    await pool.open()
+    try:
+        print(await report(pool, settings.zone, args.days, samples), end="")
+    finally:
+        await pool.close()
+    return 0
+
+
 def main(argv: list[str] | None = None) -> int:
     args = build_parser().parse_args(argv)
     if args.cmd == "init-pki":
@@ -179,4 +205,6 @@ def main(argv: list[str] | None = None) -> int:
         return _models_pull()
     if args.cmd == "eval":
         return asyncio.run(_eval(args))
+    if args.cmd == "acceptance":
+        return asyncio.run(_acceptance(args))
     return 2
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_acceptance.py tests/test_cli.py -q`
Expected: `9 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `768 passed, 1 skipped`, `TOTAL … 99%` (`acceptance.py` 100 %).

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/acceptance.py brain/src/loopi_brain/cli.py brain/tests/test_acceptance.py
git commit -F - <<'EOF'
feat(brain): loopi-brain acceptance prints the P2 acceptance numbers

Latency 08:00-22:00 (average and p95, per day), a recap for every evening with recordings, WER/CER with the 30 %
rule and recall@5 from the newest finished run, CPU time per day from two cgroup samples. Numbers only.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 10: Deploy files — `LOOPI_EVAL_DIR` and the README

**Files:**
- Modify: `deploy/docker-compose.yml`, `deploy/README.md`, `brain/tests/test_deploy.py`

**Interfaces:**
- Consumes: Tasks 1–9 (setting names, commands, pages).
- Produces: `LOOPI_EVAL_DIR: /srv/loopi/eval` in the `api` and `worker` environments (under the data-dir mount); no `LOOPI_ASR_RULES` / `LOOPI_ASR_VAD_THRESHOLD` pass-through (an empty variable would override a default the golden set chose); README section "Evaluation and acceptance (P2c)", including the CPU-sample loop Task 14 uses.

- [ ] **Step 1: Write the failing test**

Apply to `brain/tests/test_deploy.py`:

```diff
diff --git a/brain/tests/test_deploy.py b/brain/tests/test_deploy.py
--- a/brain/tests/test_deploy.py
+++ b/brain/tests/test_deploy.py
@@ -102,3 +102,18 @@ def test_the_anthropic_key_is_optional_and_comes_only_from_the_env_file():
         while route:
             assert route["provider"] != "anthropic", f"{task} routes to anthropic; keep the examples commented out"
             route = route.get("fallback")
+
+
+def test_the_golden_set_lives_in_the_data_dir_for_the_page_and_the_runner():
+    # Spec D21: the api writes the owner's golden copies, the worker's evaluation runs read them; both under the
+    # mounted data dir (not in git, not in an image), at the same path.
+    for name in ("api", "worker"):
+        service = SERVICES[name]
+        assert service["environment"]["LOOPI_EVAL_DIR"] == "/srv/loopi/eval"
+        assert "${LOOPI_DATA_DIR:-/srv/loopi}:/srv/loopi" in service["volumes"]
+    # The rules and the VAD threshold are not passed through: an empty variable would override a default the
+    # golden set's numbers chose (spec §13), so they change only in the brain's code.
+    assert not {"LOOPI_ASR_RULES", "LOOPI_ASR_VAD_THRESHOLD"} & set(SERVICES["worker"]["environment"])
+    readme = (DEPLOY / "README.md").read_text()
+    assert "## Evaluation and acceptance (P2c)" in readme and "loopi-brain eval run" in readme
+    assert "loopi-brain acceptance" in readme
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd brain && uv run pytest tests/test_deploy.py -q`
Expected: FAIL — `1 failed, 8 passed`: `KeyError: 'LOOPI_EVAL_DIR'`.

- [ ] **Step 3: Edit the compose file and the README**

````diff
diff --git a/deploy/README.md b/deploy/README.md
--- a/deploy/README.md
+++ b/deploy/README.md
@@ -91,3 +91,34 @@ Requirements: Docker with Compose v2 on the Linux PC, port 8443 reachable on the
   A cloud fallback (e.g. `recap: ollama → anthropic`) is used whenever the local step fails, not only when Ollama is
   down: also when the local model errors or runs past `LOOPI_LLM_TIMEOUT_S` (15 minutes by default) under load, so
   on a busy CPU that task's text (for a recap: the day's transcripts and captions) goes to Anthropic.
+
+## Evaluation and acceptance (P2c)
+
+- **Đánh giá** (`/eval`): you label the golden set (spec §13, D21) — about 20 recordings (listen at a level you can
+  hear, correct the current transcript into what was really said, or tick "Không có lời nói" for silence, TV or
+  noise), 30 keyframes (score the caption 1–5, tick "sai số người", "bịa đồ vật", "sai hoạt động") and 25 questions
+  (write the question, then pick its correct moments with the page's wider search, or browse a day's moments). It can
+  be done in several sittings; recordings are proposed only while their raw audio is kept (7 days, D20).
+- The golden set is **never in git**: the database holds the labels and questions, `$LOOPI_DATA_DIR/eval` the copies
+  (`audio/<id>.opus` as recorded, `audio/<id>.view.opus` to listen to, `keyframes/<id>.jpg`). Retention never deletes
+  them; "Quên khoảng thời gian" deletes the samples of its range with their copies. A `pg_dump` holds the labels but
+  not the copies: back up `$LOOPI_DATA_DIR/eval` with the database if you want to keep the set.
+- A run: the "Chạy đánh giá" button, or `docker compose -p deploy exec -T api loopi-brain eval run` (it waits and
+  prints the numbers; `--vad-threshold 0.35` or `--max-gain-db 24` try other ASR settings for that run only;
+  `loopi-brain eval show [run id]` prints a finished one). The worker transcribes every golden clip again, one per job
+  and after every waiting live job, then scores WER (Vietnamese, English) and CER (Korean) for every hallucination rule
+  set, the invented text on the no-speech clips, the caption scores per model and recall@5 through the search (with a
+  small grid of search thresholds). Runs keep numbers only.
+- The hallucination rules (`outro`, `lone_phrase`, `short_foreign`) are off until the golden set's numbers say
+  otherwise; switching one on is a change of the brain's default (`LOOPI_ASR_RULES`), committed with those numbers.
+- `docker compose -p deploy exec -T api loopi-brain acceptance --days 7 --cpu-samples - < ~/loopi-backups/p2-cpu-samples.txt`
+  prints the spec §14 P2 numbers as Markdown: latency 08:00–22:00, a recap each evening, WER/CER with the 30 % rule,
+  recall@5 and CPU time per day. It prints no transcript, caption or question. The CPU samples are two lines per
+  container, taken at least 24 h apart with no restart in between (`deploy/` as the working directory):
+
+  ```bash
+  for s in api worker models; do
+    echo "$s $(date -u +%FT%TZ) $(docker compose -p deploy exec -T $s cat /sys/fs/cgroup/cpu.stat | grep usage_usec)" \
+         "started=$(docker inspect -f '{{.State.StartedAt}}' deploy-$s-1)"
+  done >> ~/loopi-backups/p2-cpu-samples.txt
+  ```
diff --git a/deploy/docker-compose.yml b/deploy/docker-compose.yml
--- a/deploy/docker-compose.yml
+++ b/deploy/docker-compose.yml
@@ -23,6 +23,8 @@ services:
       LOOPI_TZ: ${LOOPI_TZ:-Asia/Seoul}
       LOOPI_OLLAMA_URL: http://models:11434
       LOOPI_PROVIDERS_FILE: /etc/loopi/providers.yaml
+      # The owner's golden set (spec D21): copies of the chosen recordings and keyframes, in the data dir, never in git.
+      LOOPI_EVAL_DIR: /srv/loopi/eval
       # Optional (spec §9.3): text tasks may use Anthropic only when this is set AND providers.yaml routes them there.
       ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
     volumes:
@@ -52,6 +54,7 @@ services:
       LOOPI_OLLAMA_URL: http://models:11434
       LOOPI_PROVIDERS_FILE: /etc/loopi/providers.yaml
       LOOPI_MODEL_DIR: /srv/loopi/models
+      LOOPI_EVAL_DIR: /srv/loopi/eval  # evaluation runs read the golden copies the api wrote
       LOOPI_ASR_MAX_GAIN_DB: ${LOOPI_ASR_MAX_GAIN_DB:-30}
       # Two slots for live work (video, captions, ASR, markers, embeddings) and one more, used by at most one
       # chapter/recap job at a time (job lock "llm"; those jobs also have the lowest priority).
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_deploy.py -q`
Expected: `9 passed`.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: `769 passed, 1 skipped`, `TOTAL … 99%`.

- [ ] **Step 5: Commit**

```bash
git add deploy/docker-compose.yml deploy/README.md brain/tests/test_deploy.py
git commit -F - <<'EOF'
docs(deploy): the Đánh giá page, evaluation runs and the acceptance numbers; LOOPI_EVAL_DIR

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---
### Task 11: Deploy P2c on the live stack (controller-executed)

The controller runs this task, not a subagent, after the Tasks 1–10 final review and fix wave, **only once P2b is live** (P2b's Task 14 finished and recorded in `docs/superpowers/ledgers/p2b-progress.md`) and **only after the owner's yes**. Stop and ask the owner at Step 2 (it migrates the live database and restarts api and worker).

**Files:**
- Live, not committed: branch `feat/phase2` (fast-forwarded), the compose project `deploy` (rebuilt), `~/loopi-backups/`
- Possibly: a merge commit on `feat/p2c` (Step 1)

**Interfaces:**
- Consumes: the whole branch; the live compose project `deploy` running from `/home/soul/loopi-p2/deploy` (data dir and secrets from `/home/soul/loopi-p2/deploy/.env`; volumes `deploy_db`, `deploy_ollama`, `deploy_caddy_data`).
- Produces: P2c live (migration 005 applied, the Đánh giá page served, `lang_prob` stored for new transcripts), the rollback target in the ledger.

Facts: every compose command names the project (`-p deploy`); never pass `-v` to `down`, never prune. Migration 005 only adds tables and one nullable column (`ALTER TABLE transcript_segment ADD COLUMN lang_prob REAL`: instant, but it waits behind an open transaction on that table — deploy between two transcriptions, as P2b did).

**Rollback to P2b** (any step; uses `$ROLLBACK` from Step 3):

```bash
cd /home/soul/loopi-p2/deploy
docker compose -p deploy stop worker
docker compose -p deploy exec -T db psql -U loopi -d loopi -c "DELETE FROM procrastinate_jobs WHERE task_name = 'run_eval';"
cd /home/soul/loopi-p2 && git checkout --detach "$ROLLBACK" && cd deploy && docker compose -p deploy up -d --build
```

The P2b worker does not know `run_eval` and would dead-letter it; P2b ignores the `eval_*` tables and the new column, so no restore is needed (restore the Step 3 dump only if the migrated database itself must go). Back on P2c later: `git -C /home/soul/loopi-p2 checkout feat/phase2` and `up -d --build`.

- [ ] **Step 1: Preconditions**

```bash
date '+%F %T %Z'
grep -n "Task 14" /home/soul/loopi-p2b/docs/superpowers/ledgers/p2b-progress.md | tail -3   # P2b deployed?
git -C /home/soul/loopi-p2c status --short                 # must print nothing
git -C /home/soul/loopi-p2 status --short                  # must print nothing (deploy/.env is git-ignored)
git -C /home/soul/loopi-p2 rev-parse --abbrev-ref HEAD     # expected: feat/phase2
git -C /home/soul/loopi-p2c merge-base --is-ancestor feat/phase2 feat/p2c && echo "fast-forward possible"
```

If P2b's ledger does not record its Task 14 as done, stop and wait. If `feat/phase2` is not an ancestor of `feat/p2c` (P2b's Task 14 commits its handoff and ledger, and any fix it ruled, on `feat/p2b` and fast-forwards `feat/phase2`), merge it first, test, commit, and check again:

```bash
cd /home/soul/loopi-p2c && git merge --no-ff --no-commit feat/phase2
cd brain && uv run pytest -q      # all pass; resolve conflicts test-first and record them in the ledger
cd .. && git commit -F - <<'EOF'
chore: merge feat/phase2 into feat/p2c before the P2c deploy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

- [ ] **Step 2: Ask the owner**

Ask (Vietnamese): "P2c đã qua review. Mình sẽ đưa `feat/phase2` lên `feat/p2c`, sao lưu database rồi build lại api và worker (vài phút). P2c thêm trang Đánh giá và các bảng của bộ đánh giá; nhận dạng giọng nói vẫn chạy như cũ (các luật lọc mới đang tắt). Đồng ý không?" Continue only on yes.

- [ ] **Step 3: Back up, fast-forward, redeploy, smoke test**

```bash
cd /home/soul/loopi-p2/deploy
mkdir -p ~/loopi-backups
docker compose -p deploy exec -T db pg_dump -U loopi -Fc loopi > ~/loopi-backups/p2b-$(date +%F-%H%M).dump
ls -l ~/loopi-backups/
ROLLBACK="$(git -C /home/soul/loopi-p2 rev-parse feat/phase2)"; echo "rollback target: $ROLLBACK"   # into the ledger
git -C /home/soul/loopi-p2 merge --ff-only feat/p2c
git -C /home/soul/loopi-p2 log --oneline -1                 # the feat/p2c head
docker compose -p deploy up -d --build                      # api applies 005_evaluation.sql at start
docker compose -p deploy ps                                 # db, api, worker, models, proxy: running / healthy
COMPOSE_PROJECT_NAME=deploy SMOKE_ANALYSIS_TIMEOUT=600 ./smoke.sh   # → OK: smoke test passed
docker compose -p deploy exec -T db psql -U loopi -d loopi -tAc "SELECT name FROM schema_migration ORDER BY applied_at DESC LIMIT 2"
```

Expected: a new, non-empty `.dump`; the smoke test passes; `005_evaluation.sql` is listed.

- [ ] **Step 4: The pages answer**

```bash
docker compose -p deploy exec -T api python - <<'PY'
import urllib.request
for path in ("/eval", "/eval/audio", "/eval/captions", "/eval/questions", "/", "/search?q=test", "/pipeline"):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=120) as r:
        print(path, r.status, len(r.read()))
PY
```

Expected: `200` for each path. Ask the owner to open `https://<LAN IP>:8443/eval` and check that the menu shows "Đánh giá" and that proposed recordings play.

- [ ] **Step 5: Live checks after an hour**

```bash
docker compose -p deploy exec -T db psql -U loopi -d loopi <<'SQL'
SELECT count(*) AS segments, count(lang_prob) AS with_probability FROM transcript_segment
 WHERE created_at > now() - interval '1 hour';
SELECT 'video→caption' AS path, count(*), round((avg(s) / 60)::numeric, 1) AS avg_min,
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY s) / 60)::numeric, 1) AS p95_min
  FROM (SELECT extract(epoch FROM k.captioned_at - i.ended_at)::float8 AS s FROM keyframe k JOIN item i ON i.id = k.item_id
         WHERE k.captioned_at > now() - interval '1 hour') t
UNION ALL
SELECT 'audio→transcript', count(*), round((avg(s) / 60)::numeric, 1),
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY s) / 60)::numeric, 1)
  FROM (SELECT extract(epoch FROM analyzed_at - ended_at)::float8 AS s FROM item
         WHERE kind = 'audio' AND analysis_state = 'done' AND analyzed_at > now() - interval '1 hour') t;
SELECT task_name, status, count(*) FROM procrastinate_jobs WHERE status = 'failed' GROUP BY 1, 2;
SQL
```

Expected: every new segment has a probability; latency as before the deploy (P2b's numbers in its ledger); no new failed job. Record the numbers in the ledger.

---

### Task 12: The owner labels the golden set; the baseline run (controller-executed)

The owner's own time (1–2 h, possibly several sittings). Nothing here is committed; the golden set never leaves the brain.

**Files:**
- Live only: the `eval_*` rows and `LOOPI_EVAL_DIR` (written by the owner through the page); `~/loopi-backups/` (local backups); the ledger (numbers)

**Interfaces:**
- Consumes: Task 11 (P2c live).
- Produces: the golden set (about 20 clips, 30 keyframes, 25 questions), the baseline run's numbers.

- [ ] **Step 1: Tell the owner what to do**

Say (Vietnamese): "Trang Đánh giá đã sẵn sàng ở `https://<IP LAN>:8443/eval`. Bạn làm được nhiều lần, tổng cộng khoảng 1–2 giờ: (1) **Âm thanh**: khoảng 20 đoạn — gợi ý 6 tiếng Việt, 6 tiếng Hàn, 4 tiếng Anh, 4 không có lời nói. Nghe từng đoạn, sửa transcript cho đúng từng chữ đã nói, chọn ngôn ngữ chính, hoặc đánh dấu "Không có lời nói" nếu chỉ có tiếng ồn, TV hay im lặng. (2) **Mô tả khung hình**: 30 khung hình, chấm 1–5 và đánh dấu lỗi nếu có. (3) **Câu hỏi**: 25 câu hỏi tiếng Việt như khi hỏi trang Tìm kiếm; với mỗi câu, chọn (các) khoảnh khắc trả lời đúng (có thể để trống ô chữ và chọn ngày để xem mọi khoảnh khắc). Đoạn ghi chỉ được gợi ý khi audio thô còn giữ (7 ngày). Ngôn ngữ nào ít khi nói ở nhà thì cứ để thiếu, báo cáo sẽ ghi đúng số có được. Dữ liệu này nằm trong máy brain, không đưa vào git. Xong thì nhắn mình."

- [ ] **Step 2: Stop and wait for the owner**

While waiting, progress is read as counts only (never text):

```bash
cd /home/soul/loopi-p2/deploy
docker compose -p deploy exec -T db psql -U loopi -d loopi <<'SQL'
SELECT coalesce(lang, 'none') AS lang, count(*) FROM eval_audio GROUP BY 1 ORDER BY 1;
SELECT count(*) AS keyframes, (SELECT count(*) FROM eval_caption_score) AS scores FROM eval_caption;
SELECT count(*) AS questions,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM eval_answer a WHERE a.question_id = q.id)) AS answered
  FROM eval_question q;
SQL
```

Continue only when the owner says the labelling is done. Record the final counts per language in the ledger; a language below its suggested count is recorded as it is, with the owner's reason (e.g. "ko: only 3 recordings with Korean in the kept week"), never padded.

- [ ] **Step 3: Back up the golden set (local only)**

```bash
docker compose -p deploy exec -T db pg_dump -U loopi -Fc loopi > ~/loopi-backups/p2c-golden-$(date +%F-%H%M).dump
docker compose -p deploy exec -T api tar -C /srv/loopi -cf - eval > ~/loopi-backups/p2c-eval-$(date +%F-%H%M).tar
ls -l ~/loopi-backups/
```

Expected: two new, non-empty files. They hold the owner's private recordings and text: never copy them into a repository or a scratch file that is shared.

- [ ] **Step 4: The baseline run**

```bash
docker compose -p deploy exec -T api loopi-brain eval run      # waits; prints the numbers when the worker is done
```

While it runs, read the live latency query of Task 11 Step 5 once: recordings keep their latency (the run waits behind every live job). Expected: `Evaluation run <id> — done`, the rule-set table, the caption scores and the recall line. Copy the printed numbers (they are numbers only) into the ledger with the run id. If it prints `— failed`, read `loopi-brain eval show <id>` and `docker compose -p deploy logs --since 1h worker | grep -i "evaluation run"`, fix test-first if it is a bug, and run again.

- [ ] **Step 5: Show the owner**

Ask the owner to open `/eval` → "Chi tiết" of the run in the browser: the rule-set table, the questions not found in the first 5 results, the caption scores. Note the owner's remarks in the ledger as decisions, without quoting any text.

---

### Task 13: Tuning, evidence first (controller-executed)

**Files:**
- Modify (only for an approved change, test-first): `brain/src/loopi_brain/config.py` (defaults), `brain/tests/test_config.py`; for the gain also `deploy/docker-compose.yml` and `brain/tests/test_deploy.py`; for the search thresholds also `brain/tests/test_search.py`
- Ledger: every run, every proposal, every ruling

**Interfaces:**
- Consumes: Task 12's golden set and baseline run; the runner's per-run overrides; the rule-set variants of every run; the recall grid.
- Produces: the configuration P2 is accepted with, each change backed by before/after numbers and the owner's yes.

- [ ] **Step 1: Runs for the knobs**

```bash
cd /home/soul/loopi-p2/deploy
docker compose -p deploy exec -T api loopi-brain eval run --vad-threshold 0.35
docker compose -p deploy exec -T api loopi-brain eval run --vad-threshold 0.65
docker compose -p deploy exec -T api loopi-brain eval run --max-gain-db 24
docker compose -p deploy exec -T api loopi-brain eval run --max-gain-db 36
```

Each run transcribes every golden clip again (about 20 × a few seconds of CPU, one clip per job). Record each run's id and numbers in the ledger.

- [ ] **Step 2: Apply the written decision rules**

From the baseline run and Step 1 (numbers only), build the proposal table:
- **Hallucination rules:** a rule set is proposed as the new default only if, compared with `none`, the no-speech clips with invented text go down and no language's score rises by more than 1 percentage point (it must not drop real speech). Prefer `all` if it qualifies; otherwise the single rules that qualify (two qualifying single rules are proposed together and confirmed by the run after the switch, since their union is not a measured variant).
- **VAD threshold / gain ceiling:** proposed only if a run lowers the worst language's score by at least 2 points without more invented text.
- **Search thresholds:** proposed only if the grid's best recall@5 beats the live thresholds by at least 4 points (one question in 25); keep the live pair on a tie.
- **Keyframe thresholds:** the golden set holds no truth about missed moments; report keyframes per day against §9.6's 100–300 (Task 14) and change nothing unless the owner asks.

- [ ] **Step 3: Ask the owner**

Ask (Vietnamese), one line per proposed change with its numbers, e.g.: "Bật luật lọc `lone_phrase` và `short_foreign`: đoạn không có lời nói bị bịa câu giảm từ X/4 xuống Y/4; WER tiếng Việt A % → B %, tiếng Anh …, CER tiếng Hàn … (không tệ hơn quá 1 điểm). Đồng ý đổi mặc định không?" Apply only what the owner approves; record each answer as a ruling. With no approved change, skip to Step 5.

- [ ] **Step 4: Apply an approved change test-first, redeploy, confirm**

In `/home/soul/loopi-p2c`: change the asserted default first (`test_asr_rules_and_vad_threshold` in `tests/test_config.py` for `asr_rules` / `asr_vad_threshold`; `test_search_thresholds_are_settings` in `tests/test_search.py` for `search_max_distance` / `search_min_similarity`; `test_pipeline_defaults` in `tests/test_config.py` and `test_worker_runs_the_pipeline_with_the_api_image_and_data` in `tests/test_deploy.py` for the gain, which also lives in `deploy/docker-compose.yml` as `${LOOPI_ASR_MAX_GAIN_DB:-30}`), see it fail, change the default in `config.py` (and the compose default for the gain), run the whole suite, commit with the numbers in the message body (numbers and run ids only), then deploy:

```bash
cd /home/soul/loopi-p2c/brain && uv run pytest --cov=loopi_brain -q          # all pass
cd /home/soul/loopi-p2c && git commit -F - <<'EOF'
feat(brain): <the approved default> on the owner's golden set

<run ids; per language before → after; invented text before → after>

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
git -C /home/soul/loopi-p2 merge --ff-only feat/p2c
cd /home/soul/loopi-p2/deploy && docker compose -p deploy up -d --build --no-deps api worker
docker compose -p deploy exec -T api loopi-brain eval run      # confirmation: "live rules" = the new default
```

Expected: the confirmation run's `live` row equals the variant the decision was taken on (± one clip's rounding).

- [ ] **Step 5: The 30 % rule**

If, with the accepted configuration, any language's score (WER for vi and en, CER for ko) is above 30 %, P2 needs a model or configuration change (§14). Do not change models in P2c: write the options with their costs into the ledger — PhoWhisper (Vietnamese), a Korean fine-tune of Whisper, the non-turbo large-v3 (slower: check the ≤ 15 min latency), decoding settings (beam size, temperature fallback), an initial prompt with household words, and cloud ASR only if the owner sets `allow_cloud_audio` (spec §9.3, privacy) — and ask the owner (Vietnamese): "WER/CER của <ngôn ngữ> là X % (trên 30 %). Các lựa chọn: … Bạn muốn thử cách nào?" Stop until the owner decides; a model change is a new plan.

---

### Task 14: The CPU window, the acceptance report and the handoff (controller-executed)

**Files:**
- Create: `docs/measurements/2026-10-p2-acceptance.md` (numbers only)
- Modify: `docs/HANDOFF.md` (one rewrite), create `docs/superpowers/ledgers/p2c-progress.md` (the sanitized ledger)
- Live, not committed: `~/loopi-backups/p2-cpu-samples.txt`, `~/loopi-backups/p2-acceptance-*.md`

**Interfaces:**
- Consumes: Tasks 11–13 (the accepted configuration, live for at least a day), `loopi-brain acceptance`, the P2a CPU samples (`~/loopi-backups/p2a-cpu-samples.txt`).
- Produces: the P2 acceptance report, the handoff.

- [ ] **Step 1: Start the 24 h CPU window after the last restart**

Write `scratchpad/p2-cpu-sample.sh` (it samples now — at least 30 minutes after the last `up -d`, when the backlog of the restart is gone — and again 24 h later; read only):

```bash
#!/usr/bin/env bash
# The P2 CPU window (P2c Task 14): two cgroup samples per container, 24 h apart. Read only.
set -u
cd /home/soul/loopi-p2/deploy || exit 1
sample() {
  for s in api worker models; do
    echo "$s $(date -u +%FT%TZ) $(docker compose -p deploy exec -T $s cat /sys/fs/cgroup/cpu.stat | grep usage_usec)" \
         "started=$(docker inspect -f '{{.State.StartedAt}}' deploy-$s-1)"
  done | tee -a ~/loopi-backups/p2-cpu-samples.txt
  for s in api worker models; do
    echo "$s memory.peak $(docker compose -p deploy exec -T $s cat /sys/fs/cgroup/memory.peak)"
  done | tee -a ~/loopi-backups/p2-cpu-samples.txt
}
sample
sleep $((24 * 3600))
sample
```

Run it in the background and wait with a Monitor (until-loop on the line count of `~/loopi-backups/p2-cpu-samples.txt` reaching 12). No restart of any container in between (a restart makes that container's number unusable; then start the window again).

- [ ] **Step 2: The P2a window, for comparison (perception only)**

```bash
cd /home/soul/loopi-p2/deploy
docker compose -p deploy exec -T api loopi-brain acceptance --days 1 --cpu-samples - < ~/loopi-backups/p2a-cpu-samples.txt
```

Only its "CPU time per day" section is meaningful (worker and models, 2026-10-16 04:28 → 2026-10-17 04:28 KST, before chapters, recaps and evaluation). Record it in the ledger.

- [ ] **Step 3: The acceptance numbers**

Once the window is complete: N = the number of complete local days since the P2b deploy, at most 7.

```bash
docker compose -p deploy exec -T api loopi-brain acceptance --days "$N" --cpu-samples - \
  < ~/loopi-backups/p2-cpu-samples.txt > ~/loopi-backups/p2-acceptance-$(date +%F).md
cat ~/loopi-backups/p2-acceptance-$(date +%F).md
```

Expected: the latency table (both paths "met" if the averages are ≤ 15 min), a recap for every day with recordings ("missing: none"), the ASR table with the 30 % column, the caption scores, recall@5 with "met"/"not met", the CPU table, keyframes and speech per day. A missing recap or a "not met" is a finding: tell the owner, record it, and do not hide it in the report.

- [ ] **Step 4: Write the report**

Create `docs/measurements/2026-10-p2-acceptance.md`: a header (date, the `feat/phase2` commit, the window, the time zone), a verdict table with one row per §14 P2 criterion (latency, recap every evening, WER/CER baseline with the 30 % rule, recall@5 ≥ 70 %, CPU time per day) giving the number and met / not met, the command's output from Step 3 verbatim, the golden set's size per language (and the owner's reason for any short language), the tuning decisions of Task 13 with their before/after numbers and run ids, the P2a CPU window of Step 2, and what remains (P3: MCP, vlog). **Numbers only**: no transcript, caption, question or file name from the golden set; an example sentence only with the owner's explicit approval. Then ask the owner (Vietnamese): "Báo cáo nghiệm thu P2 chỉ có các con số (không có câu nào từ ghi âm, mô tả hay câu hỏi). Mình commit vào repo nhé?" Commit only on yes.

- [ ] **Step 5: Ledger, handoff, commit**

Rewrite `docs/HANDOFF.md` once: P2c (this plan, commit range, D21), where the golden set lives and how it is backed up, the acceptance verdict, the live stack (`/home/soul/loopi-p2/deploy`, branch `feat/phase2`), the rollback of Task 11, the branches, and what comes next (P3). Copy the sanitized ledger (numbers, ids and rulings; no text from the golden set) to `docs/superpowers/ledgers/p2c-progress.md`.

```bash
cd /home/soul/loopi-p2c
git add docs/measurements/2026-10-p2-acceptance.md docs/HANDOFF.md docs/superpowers/ledgers/p2c-progress.md
git commit -F - <<'EOF'
docs: P2 acceptance report, the P2c ledger and the handoff

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
git -C /home/soul/loopi-p2 merge --ff-only feat/p2c   # docs only: no redeploy
```

Pushing any branch is outward-facing: ask the owner first.

---
## Rulings taken while writing this plan

Copy these into the ledger before Task 1. Each: decision — why — cost if wrong.

- **R1** The code lives in `brain/src/loopi_brain/evaluation/` (package `evaluation`, not `eval`, which would shadow Python's builtin wherever it is imported by name); no `brain/eval/` directory is created — D21 allows only code or format docs there, and the format is documented in the module docstrings, migration 005 and `deploy/README.md`; the D21 text in the spec names the real path — cost if wrong: none (a path in the spec).
- **R2** A golden clip is one whole audio recording (the agent cuts audio at 60 s), copied byte for byte: the runner must hear exactly what the live stage heard, and the D19 level gain is computed over the whole file, so a cut-out piece would be normalised differently — cost if wrong: a reference covers up to 60 s of speech (more typing for the owner).
- **R3** Each clip has one main language chosen by the owner (vi, ko or en); a clip mixing Vietnamese and Korean is scored with its main language's metric — cost if wrong: code-switching raises that language's error rate.
- **R4** WER for Vietnamese and English on words after NFC, lower case and punctuation/symbols-as-spaces (a Vietnamese word is a syllable, so this is the syllable error rate); CER for Korean on characters without spaces (Whisper's Korean spacing is no error); both rates are computed for every language; corpus rates (errors summed over the clips); no number normalisation ("2" vs "hai" is an error) — cost if wrong: numbers and spelling variants raise vi/en WER a little.
- **R5** The 30 % rule reads WER for vi and en and CER for ko — the spec says "WER > 30 %", but Korean WER over eojeol is not a meaningful ASR measure; both are reported — cost if wrong: if the owner meant Korean WER, the flag fires later than he expects.
- **R6** recall@5 is the macro-average over answered questions of (correct moments in the first 5 results) / (correct moments); hit@5, recall@10 and MRR are shown next to it; acceptance reads recall@5 — cost if wrong: stricter than hit@5 when a question has several correct moments.
- **R7** A correct moment is kept with its source besides its id — the keyframe (pictures), the recording and the time ± 2 s (speech), the marker item — because reprocessing gives moments new ids — cost if wrong: a re-transcribed region whose start moves more than 2 s outside the answer's span is counted as not found.
- **R8** The labelling search is the hybrid search with loose thresholds (cosine distance ≤ 1.0, word similarity ≥ 0.1, 100 candidates per search, 50 results) or, without words, every moment of the chosen days in time order (≤ 200): the owner's truth must not depend on what the live retrieval finds — cost if wrong: more scrolling for the owner.
- **R9** A question stores the local days it is about (the search page's filter) and the runner searches with them; it never asks the chat model for a time filter: recall@5 measures retrieval (§10.4 step 2), and "hôm qua" must not drift with the evaluation date — cost if wrong: the time-filter step (§10.4 step 1) is not measured (Deferred).
- **R10** A run is a chain of `run_eval` jobs — one golden clip per job, queue `light`, lock `asr`, priority −20, queueing lock `eval:<run id>` — and a last job computes the numbers; one run at a time; a run whose job is gone is marked failed when the next one is asked for — cost if wrong: about 20 job round trips per run (seconds each).
- **R11** A run transcribes each clip once and scores every rule set (live, none, each rule alone, all) from the stored segments; the VAD threshold and the gain ceiling are per-run overrides (CLI flags and page fields), measured by separate runs — cost if wrong: a union of two rules other than "all" is measured only after it is switched on (Task 13 confirms it).
- **R12** An unexpected error fails the run (no Procrastinate retry: the next run is one click); a clip Whisper cannot read is counted as failed and the run goes on; recall is "not measured" while the embedding model is unavailable (a words-only number would understate it); a run's job dead-lettered by repeated worker deaths shows on "Xử lý" as "Chạy đánh giá" with a retry button — cost if wrong: a transient database error costs a new run.
- **R13** The hallucination rules ship off (`LOOPI_ASR_RULES` empty); a default changes only in Task 13 with the owner's yes and the numbers; the rules and the VAD threshold are not passed through compose, because an empty variable from `${VAR:-}` would override a default the golden set chose — cost if wrong: a helpful rule reaches live data only after Task 13's deploy.
- **R14** The rules' parameters are code constants: outro patterns (`hãy … subscribe/đăng ký … kênh … video|ủng hộ`, `MBC 뉴스 …입니다`), the lone stock phrases (thank you, thank you very much, thanks, yeah, amen, mm-hmm, bye, you — not "ok", common in real Vietnamese speech), and English regions of at most 3 words with a language probability below 0.5 — taken from P2a's live findings — cost if wrong: a better parameter needs a commit and a deploy.
- **R15** Migration 005 adds `transcript_segment.lang_prob`; rows from before P2c stay NULL (a reprocess fills them) — cost if wrong: none.
- **R16** The ASR settings of a run (rules, gain, VAD threshold, model versions) are the worker's, recorded when it starts the run; the search thresholds are the requester's (the api serves the search) — the two processes could be configured differently — cost if wrong: none.
- **R17** A golden sample's row is committed before its copies are written, and re-checked after; `golden.sweep` deletes every copy whose sample has no row — after each "Quên khoảng thời gian" and at the 03:00 tick (retention on or off) — instead of a list of pending files — cost if wrong: a copy that could not be deleted lingers until the next sweep (a day at most).
- **R18** "Quên khoảng thời gian" deletes the golden clips, golden keyframes and correct moments of its range explicitly (counted as "mẫu đánh giá" in its preview and audit row), with the foreign-key cascades as a backstop; the owner's questions stay (only their correct moments in the range go) — cost if wrong: a question can lose all its answers and drop out of recall until relabelled (the page says "chưa chọn").
- **R19** Golden keyframes are copied although retention never deletes keyframes: D21 names "keyframe copies", and a copy keeps the set intact if keyframe files are ever cleaned up — cost if wrong: about 30 × 0.3 MB.
- **R20** Caption scores are kept per caption model; after "Xử lý lại → mô tả khung hình" with another model the page asks for a new score; the runner never re-captions (a caption has no automatic reference) — cost if wrong: comparing caption models needs the owner's rescoring.
- **R21** Proposals: recordings by the language Whisper heard most in them (and "Whisper heard nothing") × 4 parts of the day, 2 each, in a stable pseudo-random order (`md5(id || seed)`, "Gợi ý khác" takes the next seed); keyframes one per local day and 3-hour block, 12 at a time — cost if wrong: the strata follow Whisper's language detection (a Korean clip heard as Vietnamese is proposed under Vietnamese; the owner sets the language).
- **R22** The target of 20 clips is suggested as 6 vi, 6 ko, 4 en, 4 without speech; the page names a language that cannot reach its suggestion with the recordings still kept; nothing is enforced and the report prints the real counts — cost if wrong: none.
- **R23** The labelling search is a POST: question words never appear in a URL, so never in uvicorn's access log — cost if wrong: a labelling search cannot be bookmarked.
- **R24** The acceptance window is the last N complete local days (default 7); latency counts recordings that ended between 08:00 and 21:59 local time; recording → caption is the segment's end → its keyframe's caption (every keyframe counts), recording → transcript the segment's end → its transcription (silent recordings count too) — the same definitions as the "Xử lý" page — cost if wrong: minutes with several new keyframes weigh more.
- **R25** "Recap mỗi tối" is checked as: every day of the window with moments has a recap; its writing time is printed (a recap written after midnight for a backlog day still counts as present) — cost if wrong: none (the times are in the report).
- **R26** CPU per day comes from two host-side cgroup samples per container (the P2a format), 24 h apart with no restart, parsed by `loopi-brain acceptance`; the P2a window (perception only) is reported next to a new window taken after Task 13's last deploy (the whole of P2: chapters, recaps, evaluation) — cost if wrong: the report waits a day longer.
- **R27** The report's numbers come from `loopi-brain acceptance` (Markdown, numbers only); the controller writes `docs/measurements/2026-10-p2-acceptance.md` from it with the verdicts and decisions — cost if wrong: none.
- **R28** `range_utc` moves from `web/search_routes.py` to `days.py` (P2b deferred minor: the chat duplicated it; questions need it too) and stays importable from `web/search_routes.py` — cost if wrong: none.
- **R29** Earlier tests changed on purpose (nothing removed): `test_db.py` (005 in the migration lists, the new tables and indexed foreign keys; Task 1), `test_config.py` (new settings; Tasks 1, 3), `conftest.py` (truncation list, `settings.eval_dir`; Task 1), `helpers.py` (three new helpers; Task 4), `test_asr.py` and `test_worker.py` (the `speech_regions` stand-ins take the threshold; Task 3), `test_days.py` (`range_utc`; Task 5), `test_deploy.py` (the eval dir; Task 10) — cost if wrong: none; each change is shown as a diff in its task.
- **R30** Keyframe thresholds are not tuned from the golden set (it holds no truth about missed moments); the report shows keyframes per day against §9.6's 100–300 and a change is the owner's call — cost if wrong: none.
- **R31** Written bars for Task 13 (always with the owner's yes): a rule set becomes the default only if invented text on no-speech clips goes down and no language worsens by more than 1 point; a VAD threshold or gain only if the worst language improves by at least 2 points without more invented text; search thresholds only if recall@5 improves by at least 4 points (one question in 25) — cost if wrong: a real improvement smaller than its bar is left for later.
- **R32** D21 is written into the spec's §3 table, "Sửa đổi", §10.6 (the Đánh giá page), §13 (the golden set and how a run works) and §17 (`evaluation/`) — cost if wrong: none.
- **R33** WER or CER above 30 % after Task 13: the controller writes the options and their costs (PhoWhisper, a Korean fine-tune, non-turbo large-v3, decoding settings, an initial prompt, cloud ASR only with `allow_cloud_audio`) and stops for the owner; P2c changes no model (brief item 8) — cost if wrong: none.
- **R34** Execution: subagent-driven, one implementer and one reviewer per task for Tasks 1–10, then a final whole-branch review and one fix wave before Task 11, as in P2a and P2b — cost if wrong: none.
- **R35** Tasks 11–14 start only after P2b is live (its Task 14 done) and with the owner's yes; `feat/phase2` is merged into `feat/p2c` first when P2b's Task 14 moved it — cost if wrong: none.

## Deferred (out of scope for P2c)

- Measuring the chat model's time filter (§10.4 step 1) and the answers themselves; recall@5 measures retrieval only (R9).
- Vietnamese search without diacritics (`unaccent` or a normalised text column; P2b deferred): do it if Task 12's missed questions show it, as a new plan item with its own recall numbers.
- Re-captioning the golden keyframes with a candidate model inside a run (an automatic caption comparison); per-segment language scoring for mixed vi/ko clips.
- A per-clip view of what a run heard against the reference (the segments are stored in `eval_run_audio` for it).
- The search page's `GET /search?q=…` puts the query in uvicorn's access log (P2b, pre-existing): a POST or `access_log=False` is the owner's call.
- Tuning the chapter parameters (P2b R5): §13 has no golden set for chapters; the owner's remarks from P2b's Task 14 decide.
- CPU sampling inside the brain (the worker cannot read the `models` container's cgroup): the host-side samples of Task 14 stay.
- MCP server and vlog: **P3**.
