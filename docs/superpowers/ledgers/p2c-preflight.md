# P2c pre-flight conflict scan

Plan: `docs/superpowers/plans/2026-10-16-p2c-evaluation-acceptance.md` (6 287 lines, plan HEAD `2c2eb6d`). Scanned against the
worktree `/home/soul/loopi-p2c` (HEAD clean). Line numbers below are plan line numbers unless a file is named.

How it was checked (nothing in the repo was touched; all scratch work is under the session scratchpad):

- **Mechanical replay**: every ` ```diff ` block was applied with `git apply`, and every "Create `path`" block and the Task 1 spec
  script was written/run, in document order, task by task, into a scratch copy of `HEAD`. All 17 diff blocks applied cleanly, with `git apply`
  and again with GNU `patch` (no offset or fuzz reported, so the hunk headers are exact; both trees are identical), the spec script printed
  `1 file changed, 8 insertions(+), 4 deletions(-)` (matches line 352).
- **Counts**: `pytest --collect-only` after each task gave 699, 707, 724, 741, 746, 763, 774, 776, 782, 783 tests = the plan's
  698/706/723/740/745/762/773/775/781/782 passed + 1 skipped. Per-file counts reproduce every group "Expected N passed" (see B rows).
- **Pure tests executed** (no database, `DOCKER_HOST` pointed at a dead socket so no Docker call is possible): metrics (8), config (7),
  days (5), deploy (9), the asr rule/threshold tests (16), `test_clips_are_proposed_from_the_device_gain_change_on`,
  `test_a_question_not_embedded_beforehand…`, `test_an_answer_is_recognised_after_its_day_is_reprocessed`, the acceptance CPU and
  "could not measure" tests: all pass. Database tests could not run here (no Docker allowed).
- **Template/format checks**: Task 7/8 templates rendered with synthetic context through the real Jinja environment; every substring the
  page tests assert was found (the one miss was my own synthetic data). `runner.format_run`, `format_compare`, `asr_numbers` were run on the
  Task 6 test scenarios: all asserted strings and numbers hold. SQL placeholders of the big queries converted statically with psycopg's parser.
- **`git add` vs changed files**: per task, the set of files changed by the replay equals the `git add` list and the `**Files:**` list exactly (T1 6,
  T2 3, T3 5, T4 8, T5 6, T6 5, T7 13, T8 8, T9 3, T10 3).
- **Spot checks against current code**: helper/fixture names, `search.py`, `providers`, `jobs.enqueue`, `forget._derived`, `media.*`, `days.*`,
  `FakeOllama`/`FakeTranscriber`, `VadOptions` defaults, Procrastinate `fetch_job_v2` semantics, `model_version` strings, task queues/locks.

Result in one line: **no Critical finding**; 2 Important, 13 Minor (section "Findings"). The plan is internally consistent with HEAD.

---

## A. Task pairs that share a file or an interface

| # | Tasks | Shared file / interface | What one produces vs what the other consumes (exact names) | Result | Plan lines |
|---|---|---|---|---|---|
| A1 | T1–T3 | `config.py`, `test_config.py` | T1 adds `eval_dir`; T3 adds `ASR_RULES`, `asr_vad_threshold`, `asr_rules`, `_known_rules`, `asr_rule_set`; hunks do not overlap, both apply in order | match | 467–481, 154–171 / 980–1029, 931–958 |
| A2 | T1–T3 | `transcript_segment.lang_prob` | column added by the migration; T3 INSERT writes `result.lang_prob`; helper `insert_segment(lang_prob=…)` (T4) writes it | match | 365 / 1181–1187 / 1272 |
| A3 | T1–T4 | `config.py` | T4's hunks use T1's `eval_dir` comment and T3's `@field_validator("asr_rules")` as context, add `import datetime as dt`, `eval_audio_since`, `_aware` | match (T4 needs T1+T3, which come first) | 2133–2156 |
| A4 | T1–T4 | tables `eval_audio`, `eval_caption`, `eval_caption_score`, `eval_answer` | columns in the migration = columns in golden.py SQL (`id,item_id,path,lang,no_speech,reference,had_text,prefilled`; `id,keyframe_id,path`; `sample_id,model_version,caption,score,wrong_*`; `item_id,keyframe_id,moment_id` in `forget_rows`) | match | 371–407, 424–434 / 1870–1872, 1963, 1991–1996, 2054–2062, 2076–2078 |
| A5 | T1–T4 | `Settings.eval_dir`, `conftest.settings.eval_dir` | tests' `pki_settings.eval_dir` (tmp) is what `_copy`, `remove_files`, `sweep` write to; a Settings built without it only reads a missing `/srv/loopi/eval` (`sweep` returns 0) | match | 477, 150 / 1878, 1884, 1918, 2105 |
| A6 | T1–T5 | `eval_question`, `eval_answer` | questions.py SQL columns = migration columns; `kind` CHECK (`visual|speech|marker`) = `moment.kind` CHECK (migrations/003) | match | 411–434 / 2601, 2612, 2628, 2642, 2656–2658, 2733, 2748 |
| A7 | T1–T6 | `eval_run`, `eval_run_audio` | runner SQL columns (`status,config,results,error,requested_at,started_at,finished_at`; `run_id,sample_id,segments,error`) = migration; `queueing_lock` is a `procrastinate_jobs` column | match | 440–460 / 3300–3314, 3469–3473, 3494, 3504–3518, 3547–3553 |
| A8 | T1–T9 | `eval_run` in acceptance tests | `INSERT INTO eval_run (id, status, config, results, requested_at)` fits the table | match | 5279–5282 |
| A9 | T1–T10 | `LOOPI_EVAL_DIR` | name and default `/srv/loopi/eval` equal in config, compose (api and worker) and `test_deploy` | match | 477, 163–166 / 5863–5864, 5872, 5791 |
| A10 | T2–T6 | `evaluation.metrics` | `Errors`, `wer`, `cer`, `words`, `bootstrap(clips)`, `wilson(successes, n)`, `hit_at(ranks, k)`, `recall_at(ranks, relevant, k)`, `reciprocal_rank(ranks)` called with the same argument order | match | 517, 641–762 / 3356–3377, 3429–3436 |
| A11 | T3–T4 | `config.py` order | T4's `_aware` hunk is anchored just before T3's `_known_rules` | match (applies) | 2142–2156 |
| A12 | T3–T6 | ASR path | `transcribe_file(path, transcriber, max_gain_db, vad_threshold)` is called positionally by the runner; `kept_segments(result, frozenset)`; `RegionResult(lang, lang_prob, segments)`; `SegmentResult(**s)` keys = dataclass fields (asr.py:69–82); `asr_rule_set`, `asr_vad_threshold`, `asr_max_gain_db`, `ASR_RULES` | match | 1138–1144, 1099–1111 / 3322–3336, 3513–3515, 3538–3540, 3240, 3261 |
| A13 | T3–T6 | `speech_regions(audio, threshold)` stand-ins | T3 rewrites every existing one-argument stand-in; T6 tests use two-argument stand-ins | match | 820–823, 829–831, 839–840, 848–849, 966–967 / 2846, 2948 |
| A14 | T3–T10 | rules and VAD not in compose | `test_deploy` forbids `LOOPI_ASR_RULES`/`LOOPI_ASR_VAD_THRESHOLD` in the worker env; the compose diff adds none; README says the default is changed in code | match | 5793–5795 / 5859–5875 / 5842–5843 |
| A15 | T3–T13 | tests that Task 13 edits | `test_asr_rules_and_vad_threshold` (T3), `test_pipeline_defaults`, `test_search_thresholds_are_settings` (test_search.py:145), `test_worker_runs_the_pipeline_with_the_api_image_and_data` (test_deploy.py:12) all exist | match | 940 / 6104 |
| A16 | T4–T5 | `golden.clean_text` | defined in T4, imported and used by questions.py | match | 1722 / 2571, 2589 |
| A17 | T4–T6 | test helpers, clip path | `analysed_audio`, `captioned_keyframe`, `insert_segment` (T4) used by T6 tests; `eval_audio.path` = `audio/<id>.opus` is what `media.inside(eval_dir, clip["path"])` reads | match | 1255–1292 / 2809–2822, 3102 / 1749, 3537 |
| A18 | T4–T7 | `golden.*` | every name, signature and dict key the routes and templates use exists (`audio_candidates(…, since=)`, `progress`, `audio_samples`, `caption_*`, `add_*`, `score_caption`, `delete_*`, `sample_exists`, `audio_paths`, `keyframe_path`, `transcripts`, `check_score`, `audio_label`, `DAY_PARTS`, `AUDIO_SUGGESTED`, `AUDIO_TARGET`, `CAPTION_TARGET`, `NOISE`, `NO_SPEECH`, `SILENT`) | match | 1231–1237 / 4225–4445 |
| A19 | T4–T7 | dominant-language subquery, day part | route `_candidate` re-implements golden's `SELECT s.lang … ORDER BY sum(extract(epoch …)) DESC, s.lang LIMIT 1` and `DAY_PARTS[hour // 6]` | match (works) but duplicated logic, see M6 | 1787–1790 / 4275–4289 |
| A20 | T4–T8 | `golden.clean_text` | `_question_error` uses it with `questions.TEXT_MAX` | match | 1722 / 4855 |
| A21 | T4–T12 | progress counts | T12's SQL `CASE WHEN NOT no_speech THEN lang WHEN had_text THEN 'none' ELSE 'silent' END` = `golden.progress` | match | 6028 / 2054–2056 |
| A22 | T5–T6 | questions + search | `answered`, `ranks`, `range_utc`, `search_moments(candidates=)` (T5) used by the runner; `QUERY_INSTRUCTION`, `clean_query` exist (search.py:27, 58); `_CachedEmbeddings.embed` only needs `router.embed` (search.py:76) | match | 2320, 2498–2517 / 3248, 3394–3414, 3423–3427, 3445 |
| A23 | T5–T7 | `questions.counts` | produced `{"questions","answered"}`, read as `q.questions`/`q.answered` | match | 2745 / 4225, 4467–4468 |
| A24 | T5–T8 | `questions.*`, `OptionalDate`, `KIND_LABELS`, `hit_view` | all names exist (search_routes.py:23–54); `hit_view` keys `id,kind,kind_label,time,text,lang,keyframe_id` cover `_eval_hits.html`; `Hit(**row, …)` matches `Hit` fields | match | 2597–2751 / 4825, 4855–4950, 5077–5090 |
| A25 | T5–HEAD | `range_utc` move | `days.range_utc` added, `search_routes` imports it, `dt` is still used there (`OptionalDate`) | match | 2486–2492, 2526 |
| A26 | T6–T8 | runner → run table | `list_runs`, `start_run`, `pct`, `span`, `MAX_GAIN_DB`, `WER_LIMIT`, `DISTANCES`, `SIMILARITIES`, `VARIANTS`; result keys read by `_eval_runs.html` = keys written by `asr_numbers`, `caption_numbers`, `_recall` (rendered with real data: all T8 test-2 substrings hold) | match | 2790–2793, 3482–3491 / 4955–5001, 5095–5143 |
| A27 | T6–T9 | acceptance reads a run | `latest_run(pool, finished=True)`, `pct`, `span`, results keys `languages.*`, `no_speech.*`, `recall.*`, `captions.*`, `prefilled`, `blind`, `recorded` | match on names; which run is read: see I2 | 5596–5642, 5672 |
| A28 | T6–T9 | `cli.py` | T9 hunks anchor on T6's `impact.add_argument` and the end of `_eval`; no name clash (`acceptance` parser var, `_acceptance`) | match | 5686–5734 |
| A29 | T6/T9–T10 | README commands | `eval run|show|compare|rules-impact`, `acceptance --days --cpu-samples -` exist with those flags; `test_deploy` asserts exactly the strings README has (executed: passes) | match | 3801–3814, 5690–5694 / 5797–5799, 5818–5844 |
| A30 | T6–T12/T13 | CLI used by the controller | `--vad-threshold`, `--max-gain-db`, `eval compare`, `rules-impact --days 7`; "after 22:00" stated in T12 and T13 Step 1 but not T13 Step 4 | match on names; time rule gap, see M1 | 6054–6056, 6081–6085, 6118 |
| A31 | T7–T8 | `eval_routes.py`, `eval.html`, `test_eval_page.py` | T8 diffs anchor on T7 output; `eval_page` calls `_runs_context`, defined later in the file (fine at call time) | match | 4809–4835, 5152–5155, 4694–4712 |
| A32 | T7–T8 | T7 tests after T8 | T8's run section on `/eval` does not disturb T7's asserted strings (rendered both ways) | match | 4012–4014 / 5156–5167 |
| A33 | T7–T11 | pages named in Step 4 | `/eval`, `/eval/audio`, `/eval/captions`, `/eval/questions` routes exist | match | 4221, 4247, 4363, 4862 / 5975 |
| A34 | T8–T12 | "Chi tiết" | the run row has `<summary>Chi tiết</summary>` | match | 5113 / 6063 |
| A35 | T9–T14 | CPU sample format | `parse_cpu` (5 tokens `usage_usec … started=…`, 3 tokens `memory.peak`) = script output = the real P2a file (`~/loopi-backups/p2a-cpu-samples.txt`, 4 lines: first sample only, second due 2026-10-17 04:28 KST) | match on format; T14 `Files:` names an undated file, see M2 | 5531–5541 / 6153–6158 / 6134 |
| A36 | T10–T14 | CPU loop | README loop writes usage lines only; T14's script also writes `memory.peak` lines that the report's "Memory peak" column reads | mismatch, see M3 | 5850–5855 / 6151–6159 / 5655 |
| A37 | T9/T13–T14 | which run the report reads | `latest_run(finished=True)` = newest done run, header prints no gain/VAD override; T13 Step 3 "skip to Step 5" leaves a knob run (with overrides) as the newest | mismatch, see I2 | 5672, 5604 / 6081–6084, 6100 |
| A38 | T1–T11 | migration listing | `schema_migration(name, applied_at)` exists (db.py:29–30); `ORDER BY applied_at DESC LIMIT 2` lists 005 | match | 5965 |
| A39 | T9–T14 | `acceptance` arguments | `--days N --cpu-samples - [--exclude-day YYYY-MM-DD …]` | match | 5690–5694 / 6171, 6181 |
| A40 | T4–T6 | forget vs run results | `eval_run_audio` cascades from `eval_audio`, which `forget_rows` deletes; `eval_run` keeps numbers | match | 454–455, 270 / 2076 |
| A41 | T4–HEAD | `ForgetCounts` | new field `golden: int = 0` keeps `ForgetCounts(items=3, …, files=7, files_failed=0)` equality in test_forget.py:129, 237 | match | 2182 |
| A42 | T6–HEAD | task registration | `TASK_MODULES` gains `loopi_brain.evaluation.runner`; `RUN_EVAL`; label in `TASK_LABELS`; `run_eval` is not in `AUTO_RERUN_TASKS`, which is right (`run_step` never raises) | match | 3886–3913 |
| A43 | T3–T6 | live ASR lock | evaluation clip jobs use `queue="light"`, lock `asr`; the live job is deferred with queue `audio`, lock `asr` (dispatch.py:30); Procrastinate `fetch_job_v2` blocks a job while a same-lock job is `doing` or `todo` with higher priority (schema.sql:225–247) | match | 3275–3276, 3576 / Facts 27 |

## B. One row per task (does its own text agree with itself)

| # | Task | Tests vs code | "Expected …" counts | `Files:` vs diffs vs `git add` | Undefined references | Edit anchors | Notes |
|---|---|---|---|---|---|---|---|
| B1 | T1 (112–505) | every assertion matches migration/config; spec script anchors found exactly once | red `5 failed, 7 passed, 3 errors` = 15 tests (6 test_config + 9 test_db: 5 rules/lists/FK/config, 3 `pool` errors from the truncation list); green `15 passed`; suite `698+1` = 699 collected | 6 = 6 = 6 | none | all hunks apply to HEAD; spec edit `8 insertions, 4 deletions` reproduced | test_db hunk adds three blank lines before the new test (230–232), see M4 |
| B2 | T2 (509–788) | metrics tests pass (executed) | `8 passed`; `706+1` = 707 collected | 3 = 3 = 3 | none | new files only | none |
| B3 | T3 (792–1215) | rule and threshold tests pass (executed); DB tests consistent by reading | red `25 failed, 54 passed, 1 skipped` = 80 tests: 17 new + 8 existing stand-in users (4 via `_vad_input`, 3 asr, 1 worker); green `79 passed, 1 skipped`; `723+1` = 724 | 5 = 5 = 5 | none (`unicodedata`, `np`, `SR`, `fetch_all`, `make_ctx`, `_audio_item` present) | all hunks apply | defaults keep P2a behaviour (see C4) |
| B4 | T4 (1218–2303) | SQL placeholders convert (6 and 3 params); `forget_rows` count 3 = clip + keyframe + answer; sweep naming consistent with `normalise_audio`'s `<id>.view.<hex>.part.opus` | red `1 error` collection; green `82` = 17 + 37 + 28; `740+1` = 741 | 8 = 8 = 8 | none (`store.Retention`, `set_retention`, `apply_retention(pool, settings, now)`, `insert_moment(item_id, seconds, axis)` exist) | all hunks apply (forget, retention, config, 2 templates) | retention import order, see M5; no existing test breaks: `ForgetCounts` equality unaffected, no test reads the two templates |
| B5 | T5 (2307–2776) | `matches`/`ranks` test passes (executed); search expectations follow `_by_meaning`'s `distance <= max_distance` (search.py:90) | red `2 errors`; green `29` = 4 + 5 + 20; `745+1` = 746 | 6 = 6 = 6 | none | days.py, search.py, search_routes.py apply | `answered()` can KeyError, see M11 |
| B6 | T6 (2780–3938) | test 1, rule-drop, 30 % tests re-run on runner code (pure part): all asserted strings and numbers hold; `Wilson(1,1)` 0.2065 and `Wilson(1,3)` 6.1–79.2 % verified | red `1 error`; green `31` = 17 + 3 + 11; `762+1` = 763 | 5 = 5 = 5 | none (`paths()`, `requests`, `error`, `down`, `chunks`, cycling `FakeTranscriber`, `run_jobs`, `make_ctx(ollama=)` exist; `model_version` strings equal) | cli.py, jobs.py, status.py apply | `_finish` blocks the event loop, see I1 |
| B7 | T7 (3941–4673) | every asserted substring found by rendering the templates (audio page, candidate, clip, keyframe, picture, captions page, escaping, CSS rule) | red `10 failed, 1 passed` (11 tests; the passing one only expects 404s); green `23` = 11 + 12; `773+1` = 774 | 13 = 13 = 13 (1 route + 9 templates + app.py + base.html + test) | none | app.py, base.html apply | unused `log`, duplicated SQL, CSS-string test: M6, M7, M8 |
| B8 | T8 (4677–5192) | question/run templates rendered with real `asr_numbers`/`wilson`: all test-2 substrings hold (`0.0–79.3 %`, `(0/1)`, row order, missed list, polling flag) | red `2 failed, 11 passed`; green `13`; `775+1` = 776 | 8 = 8 = 8 | none (`search.request_router`, `app.state.llm`, `KIND_LABELS`, `OptionalDate`, `hit_view`) | eval_routes.py, eval.html, test hunks apply | none |
| B9 | T9 (5196–5759) | CPU, "could not measure" tests pass (executed); latency/recap/report asserted strings follow the code by reading (p95 234/87 s, `1.0 min`, `2 min`, `27.3 %`) | red `1 error`; green `9` = 6 + 3; `781+1` = 782 | 3 = 3 = 3 | none (`local_now(zone, now)`, `day_recap.generated_at/stale` exist) | cli.py hunks apply | report ignores a run's overrides, see I2 |
| B10 | T10 (5763–5896) | `test_deploy` new test passes against the README and compose diffs (executed) | red `1 failed, 8 passed` (`KeyError`); green `9`; `782+1` = 783 | 3 = 3 = 3 | none | README (last 3 lines) and compose hunks apply | README loop vs T14 script, see M3 |
| B11 | T11 (5899–6003), consumes | consumes migration 005 (A38), pages (A33), `smoke.sh` `SMOKE_ANALYSIS_TIMEOUT` (smoke.sh:25), `psql -U loopi -d loopi` (compose db env), `feat/phase2` is an ancestor of `feat/p2c` now, P2b ledger path exists | n/a | n/a | `$ROLLBACK` is set in Step 3 but used by a separate Rollback block (5913–5919), see M12 | n/a | `/home/soul/loopi-data` holds `web-password.txt`; the tar in T12 only takes `eval` (6043) |
| B12 | T12 (6006–6064), consumes | consumes `/eval` pages, progress SQL (A21), `eval run` twice, `eval compare`, "after 22:00" (6051) | n/a | n/a | none | n/a | `tar` exists in the api image (`python:3.13.16-slim`) |
| B13 | T13 (6067–6126), consumes | consumes runner overrides, `rules-impact`, variants, grid, the four tests named in Step 4 (A15) | n/a | n/a | none | n/a | skip path leaves a knob run newest (I2); confirmation run has no night rule (M1) |
| B14 | T14 (6129–6209), consumes | consumes `acceptance` flags (A39), CPU format (A35), P2a file (4 lines now) | n/a | n/a | none | n/a | `Files:` file name, loop differences: M2, M3 |

## C. One row per Global Constraint line (lines 44–57)

| # | Line | Constraint (short) | Check | Result |
|---|---|---|---|---|
| C1 | 44 | spec binding, D21 | T1 spec script (282–347) adds D21/D22 to §3, "Sửa đổi", §10.6, §13, §14, §17; anchors unique; no spec decision changed | ok |
| C2 | 45 | no golden-set text anywhere; any example text needs owner approval | logs carry ids/counts (1880, 1886, 1972, 3454, 3497, 3544); tests assert text-free logs and report (1439, 2916, 2926, 3042, 5363). Fixtures are invented or Whisper stock phrases, but no ruling records the owner's approval of example text, and R53 says "no real channel name" while fixtures and the regex use "MBC" | minor wording gap, M9 |
| C3 | 46 | `LOOPI_EVAL_DIR`; retention never deletes; forget deletes with copies | retention only sweeps copies with no row (2227–2253); forget deletes rows then sweeps (2156–2214); compose sets the path (5864, 5872) | ok |
| C4 | 47 | live ASR unchanged by default | `asr_rules` empty (1002), `asr_vad_threshold` 0.5 (1000), `VAD_THRESHOLD` 0.5 (1050) = installed `VadOptions.threshold` default 0.5; `kept_segments(result, frozenset())` = P2a filter (871); gain default untouched | ok |
| C5 | 48 | evaluation never ahead of live work | clip jobs: queue `light`, lock `asr`, priority −20 (3275–3276, 3576), verified against `fetch_job_v2`; the numbers job (lock `eval`) runs CPU-bound Python on the worker's event loop for ~28 s with a realistic golden set | partly contradicted, I1 |
| C6 | 49 | targets, acceptance numbers, "every rate with n and a 95 % interval", not measurable when 0 | `AUDIO_SUGGESTED` sums 22 (1701), 30 (1702), 25 (2573); "not measurable (no clip)" (3640, 5617); intervals on per-language, no-speech, hit@5 (3642–3647, 3658, 5621–5636); capped recall@5, hit@10, MRR and the rule-set table cells have n but no interval | ok for the §14 criteria; wording overbroad, M13 |
| C7 | 50 | D22 | `PRIMARY` vi/en WER, ko CER (3257); WER and CER shown for every language (3642–3644, 5621–5623); hit@5 is the target (3658, 5635) | ok |
| C8 | 51 | never above 30 dB; runs outside 08:00–22:00 | `0 ≤ gain ≤ 30` in `start_run` and the route (3288–3289, 4995); night rule in README (5839), page text (5165), T12 (6051), T13 Step 1 (6077); missing at T13 Step 4's confirmation run (6118) | minor gap, M1 |
| C9 | 52 | Python ≥ 3.13, no new dependency | no diff touches `pyproject.toml`/`uv.lock`; new modules import stdlib (`math`, `random`, `unicodedata`), `anyio`, existing packages only | ok |
| C10 | 53 | UI Vietnamese; every POST needs HX-Request; owner/model text escaped | all 13 eval POST routes carry `require_htmx` (4303, 4323, 4335, 4406, 4421, 4433, 4871, 4898, 4910, 4920, 4938, 4946, 4986); raw f-string responses carry constants only (4317, 4339, 4437, 4859); escaping verified by rendering (`&lt;script&gt;`, `&lt;img`, `&lt;b&gt;`) | ok |
| C11 | 54 | TDD, coverage ≥ 80 %, conventional commits with the session's attribution lines, no secrets, no Docker pruning | every task is test-first with a red Step 2; 99 % claims not re-verifiable without Docker; no secret or private text in any fixture; 13 commit blocks hard-code `Co-Authored-By: Claude Opus 5.5` | ok, attribution note M10 |
| C12 | 55 | P1+P2a+P2b suite stays green; R29 lists changed tests | totals reproduce (696+1 → 782+1); R29 (6244) = the eight test files the diffs touch; existing `ForgetCounts` equality tests unaffected | ok |
| C13 | 56 | device contract untouched | no diff touches `docs/protocol/v1.openapi.yaml` or the ingest code | ok |
| C14 | 57 | Tasks 1–10 never run Docker except testcontainers, never touch the live dirs | T1–T10 steps are `uv run pytest` and `git`; paths are inside the worktree or `tmp_path`; T11–T14 only touch the live stack and ask first (5948, 6021, 6098, 6191) | ok |

---

## Findings

No Critical finding.

### Important

**I1. Task 6 `_finish` runs CPU-bound scoring on the worker's event loop (lines 3482–3491, `asr_numbers` 3343–3378, `align` 691–711).**
`asr_numbers` aligns every clip against its reference for each of the six rule sets, in pure Python (O(n·m) Levenshtein), and `_finish` is an `async`
function without a thread hand-off. Measured here with 18 clips of 150 words (about 60 s of speech, the agent's cut length): 0.30 s per CER alignment,
**28 s for one `asr_numbers` pass over the six rule sets**, all on the event loop that also serves the other two job slots, Procrastinate's heartbeat and
the periodic `retry_stalled_jobs`. This contradicts the intent of C5 ("never ahead of live work") and gets worse with longer references; the tests
(tiny strings) cannot show it.
Smallest correction: in `_finish` compute the variants in a thread, e.g.
`variants = await anyio.to_thread.run_sync(lambda: {name: {"rules": rules, **asr_numbers(heard, rules)} for name, rules in rule_sets(config["rules"]).items()})`
(`anyio` is already imported at 3235) and use it in the `results` dict; optionally memoise `metrics.wer/cer` on `(reference, heard)` because most rule
sets leave the speech clips unchanged. No test changes.

**I2. The acceptance report can be built from a knob run, not from the accepted configuration (lines 5672, 5604, 6081–6084, 6100, 6181).**
`acceptance.report` reads `runner.latest_run(pool, finished=True)` (the newest done run) and its header does not show the run's `overrides`. Task 13 Step 1
ends with knob runs (`--vad-threshold 0.35`, `0.65`, `--max-gain-db 20`, `24`); Step 3 says "With no approved change, skip to Step 5", and only Step 4
(an approved change) ends with a confirmation run without overrides. In the no-change path the newest finished run is `--max-gain-db 24`, and T14 Step 3
(after which no run is allowed, 6142) prints its WER/CER/hit@5 as the P2 numbers.
Smallest correction: add to T13 Step 5 and T14 Step 3 "after the last knob run, run `loopi-brain eval run` once more without overrides (after 22:00), so the
newest finished run is the accepted configuration; check with `loopi-brain eval show`". Optionally print `config.overrides` in `_evaluation_lines`' header
(`run.get("config", {}).get("overrides")`; the Task 9 fixtures have `config '{}'`).

### Minor

- **M1. Night rule missing at Task 13 Step 4 (6118, GC 51, R40).** The confirmation `eval run` has no "after 22:00" (Steps 1 of T12/T13 have it). Add "(after 22:00)" to the Step 4 heading or the line before the code block.
- **M2. Task 14 `Files:` names `~/loopi-backups/p2-cpu-samples.txt` (6134)**; the script, README and Step 3 use the dated `p2-cpu-samples-<date>.txt` (6149, 5854, 6182). Change the `Files:` entry.
- **M3. README CPU loop vs Task 14 script (5850–5855 vs 6151–6159).** The README loop writes usage lines only (README says "two lines per container"); T14's script also writes `memory.peak` lines that the report's "Memory peak" column reads (5655); T10's Interfaces says the README holds "the CPU-sample loop Task 14 uses" (5770). Add the `memory.peak` loop to the README block (the test only needs the `for s in api worker models db; do` string) or say in the README that the column stays "—".
- **M4. Task 1 `test_db.py` hunk adds three blank lines before the new test (230–232)**, PEP 8 E303 (two are expected). Drop one `+` blank line and change `@@ -148,3 +156,44 @@` (226) to `+156,43`.
- **M5. Task 4 `retention.py` import order (2235–2238):** `from ..evaluation import golden` is placed before `from ..config import Settings` and `from ..days import local_now`. Put it after the `..days` line (the `forget.py` hunk 2174 is already sorted).
- **M6. Task 7 `_candidate` duplicates logic (4275–4289):** the dominant-language subquery repeats `_AUDIO_CANDIDATES` (1787–1788) character for character and `hour // 6` repeats the SQL's `/ 6` (1790). Expose one function in `golden.py` (e.g. `candidate(pool, zone, item_id)`) or share the subquery as a constant.
- **M7. Task 7 declares `log = logging.getLogger(__name__)` and never uses it (4181, 4191)**; Task 8 does not use it either. Drop the import and the variable, or log the failed saves with ids.
- **M8. Task 7 `test_the_page_fits_a_phone` (4159–4161) only checks that a CSS string is present** in the page. Acceptable as a regression pin; say so in the test comment, or drop it.
- **M9. C2 wording vs fixtures (45, 6271).** "Any example text in the repository needs the owner's explicit approval" has no ruling covering the fixture sentences (e.g. 862–866, 1349, 3109, 5269), including "Hãy đăng ký kênh để ủng hộ mình nhé", already in the committed P2a ledger (p2a-progress.md:101); R53 says fixtures carry no real channel's name while 864, 885 and the regex at 1066 use "MBC". Add one ruling: fixtures are invented text or stock Whisper phrases already in git, not golden-set text, and "MBC" is the broadcaster's name inside a stock phrase.
- **M10. Hard-coded attribution (502, 785, 1212, 2300, 2773, 3935, 4670, 5189, 5756, 5893, 5943, 6113, 6203):** 13 commit blocks say `Claude Opus 5.5`; C11 (54) says "the session's attribution lines" and this session's reminder says `Claude Sonnet 5.5`. `.superpowers/sdd/…/globals.md` already tells implementers to name their own model, so only the plan text is stale. Replace the model name by a placeholder in the heredocs or leave as is.
- **M11. `questions.answered` can raise `KeyError` (2742):** the two SELECTs are separate statements; if the owner removes a question's last correct moment between them, `by_question[row["id"]]` fails and the run is marked failed. Use `by_question.get(row["id"])` and skip rows without answers.
- **M12. `$ROLLBACK` does not survive into the Rollback block (5913–5919, 5959):** it is a shell variable set in Step 3, used by a block run in another shell. Say "paste the commit id recorded in the ledger" in the rollback block.
- **M13. C6 "every rate … with its sample size and a 95 % interval" (49) is wider than what is built:** capped recall@5, hit@10, MRR (3659–3660, 5637–5639) and the rule-set table cells (3627–3628) carry n but no interval. R46 (6264) says "every criterion"; reword C6 to "every §14 criterion".
