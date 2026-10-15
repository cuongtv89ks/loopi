# P2a perception pipeline: pre-flight conflict scan

Plan: `docs/superpowers/plans/2026-10-15-p2a-perception-pipeline.md` (6515 lines, plan commit 29e27df). Line numbers
below are plan lines unless a file is named. Task 11 was scanned only for the interfaces it consumes. Findings
already made by `scratchpad/p2/p2a-plan-review.md` (I1–I8, M1–M15) are not repeated.

## Method and results

- **Plan code vs prototype.** A script pulled every file-targeted code block out of the plan (61 blocks) and diffed each
  one against the prototype `scratchpad/p2impl`:
  - All 50 Create/Replace blocks are byte-identical to prototype HEAD. Task 3's `app.py` matches the Task 3 commit
    `f1e88a4`. `jobs.py` differs only by the `TASK_MODULES` lines that Tasks 4–7 add.
  - `helpers.py` rebuilt from P1 + `import contextlib` + the six appended slices, in plan order, is identical to
    prototype HEAD.
  - `conftest.py` after Task 1 Step 7 is identical to `fbf9aa9`. `device_routes.py` after Task 3 Step 5 is identical
    to HEAD.
- **Prototype base = current worktree.** Prototype commit `ee58603` has the same `brain/src`, `brain/tests`, `deploy`,
  `pyproject.toml` and `uv.lock` as `/home/soul/loopi-p2` today.
- **Prototype suite.** `uv run pytest -q -p no:cacheprovider` (testcontainers only): **284 passed, 1 skipped in 24.6 s**.
- **Test counts.** Collected per file in the prototype: config 2, db 7, providers 27, dispatch 18, frames 14,
  video 11, moments 10, caption 14, asr 29 (28 + 1 skip), worker 8, cli 3, web_pipeline 8, deploy 6. Every
  "Expected: PASS (N)" agrees. P1 collected in the worktree: 133.
- **Lock vs Docker image.**
  - Host uv 0.10.9 writes `uv.lock` as `revision = 3` (P1's is `revision = 2`). The unchanged Dockerfile builds with
    `uv:0.5.11 … uv sync --frozen`.
  - I fetched uv 0.5.11 into uv's own tool cache (`uv tool run --from uv==0.5.11`). It parses the prototype lock
    (`uv export --frozen --no-dev` succeeds).
  - cp313 manylinux x86_64 wheels are locked for onnxruntime 1.31.0, ctranslate2 4.8.3 (bundles libgomp), av 18.1.0,
    numpy 2.5.4 and pillow 12.3.0.
  - All P1 pins are unchanged in the new lock.
- **Procrastinate 3.10 sources** (prototype venv):
  - `App.configure_task` calls `perform_import_paths()`. So the API imports every `TASK_MODULES` module on its first
    defer: numpy and PIL, but not faster-whisper. This is consistent with Task 7's lazy-import ruling.
  - The queueing-lock index covers only `status = 'todo'`.
  - The event types `started` and `succeeded` used by Task 11 exist.
- **Side effects of this scan.** I ran one `pytest --collect-only` in the worktree; it changed no tracked file
  (`git status` is clean). I ran no docker command apart from the prototype's testcontainers.

## A. Task-pair rows (shared file or interface)

| # | Tasks | Producer → consumer (exact names, plan lines) | Finding |
|---|---|---|---|
| A1 | 1→2 | `Settings.ollama_url`/`ollama_keep_alive` (393–394) → `build_router` (1409–1411), `SETTINGS` in tests (722); `httpx`, `pyyaml` move from dev to runtime deps (155–156) → `providers/ollama.py`, `providers/config.py` | match |
| A2 | 1→3 | `conftest.py`: T1 Step 7 replaces `APP_TABLES` (567–570); T3 replaces the whole file (1518–1592) with the same `APP_TABLES`, P1's `database_url/pool/settings/pki_settings`, new `jobs_open`, `client(…, jobs_open)`. `helpers.py`: T1 `import contextlib` + `fresh_database` (342–361) precede T3's appends (1460–1515), both use P1's `dt` | match (P1→T3 diff = APP_TABLES + jobs_open + client dependency only) |
| A3 | 1→3 | `item.analysis_state/analysis_job_id` (425–427), `item_analysis_pending` (430), `keyframe.caption_job_id` (444), Procrastinate tables from `migrate()` (536) → `_defer_item` (2096–2105), `defer_keyframe` (2128–2136), `_queued` (2163), `dispatch_pending` (2190–2214); `Settings.dispatch_max_queued` (398) → `dispatch_pending_job` (2260) | match; `DISPATCH_MAX_QUEUED = 20` (2079) repeats the setting's default → F5 |
| A4 | 1→4 | `keyframe` id/item_id/device_id/ts/frame_index/path/`dhash BIGINT`/luma (432–448) → INSERT (3102–3104) via `to_signed64` (3094); `Settings.keyframe_*` (399–402) → `KeyframeParams.from_settings` (2825–2831); P1 `default_video_fps` → 3076 | match; `KeyframeParams` repeats the defaults (2819–2822) → F5 |
| A5 | 1→5 | `moment` (468–483), `embedding vector(1024)`, pool `configure=register_vector_async` (558) → `Vector(vector)` UPDATE (3386–3387), `.to_list()` (3275) | match |
| A6 | 1→6 | `keyframe.caption/caption_model/captioned_at/caption_job_id/caption_error` (441–445) → `_caption` UPDATE (3874–3876), outage UPDATE (3866–3868); `moment.keyframe_id … ON DELETE CASCADE` (477) | match; the spec §9.4 column `keyframe.model_version` is named `caption_model` → F1 |
| A7 | 1→7 | `transcript_segment` (452–464) → INSERT, 10 columns (4517–4523); `moment.transcript_segment_id … CASCADE` (478) → the re-run DELETE relies on the cascade (4507–4508); `asr_language_list`, `whisper_cpu_threads`, `model_dir` (396, 403–412) → `WhisperTranscriber.__init__` (4440–4445) | match |
| A8 | 1→8 | `migrate()` under `pg_advisory_lock` (517–544) → `run_worker` (4821), concurrent with api `serve`; `make_pool` (547–559) → 4824; `worker_concurrency`/`model_dir`/`providers_file` → 4830/4840/4822; `procrastinate_workers.last_heartbeat` → `health.worker_alive` (4785) | match |
| A9 | 1→9 | `analysis_*`, `keyframe.captioned_at/caption_error` → `PENDING_ITEMS` (5276–5281), `FAILED_JOBS` (5264–5274), `VIDEO/AUDIO_LATENCY` (5255–5263), `_day_by_hour` (5511–5527); `providers_file`/`ollama_url` → `model_health` (5335–5339) | match |
| A10 | 1→10 | env `LOOPI_OLLAMA_URL`, `LOOPI_PROVIDERS_FILE`, `LOOPI_MODEL_DIR` (5969–5970, 5995–5997) ↔ fields with `env_prefix="LOOPI_"` (381); compose comment `LOOPI_WHISPER_CPU_THREADS` (6000) ↔ `whisper_cpu_threads` (404); new `uv.lock` (Step 1, 184) ↔ unchanged Dockerfile with uv 0.5.11 (ruling 5857) | match (lock revision 3 parsed by uv 0.5.11; wheels present, see Method) |
| A11 | 1→11 | Step 7 tunes `keyframe_hash_threshold`/`keyframe_min_gap_s` in config.py + the tuple assertion in test_config (6472 ↔ 207–209); Step 4/6 SQL uses `caption`, `caption_error`, `caption_job_id`, `analysis_job_id`, `analyzed_at`, `received_at` | columns match; tuning leaves `KeyframeParams` defaults (2819–2822) and `test_frames.PARAMS` (2470) on the old values → F5 |
| A12 | 2→3 | `Router.health()` (1397), `task_ready(task, health)` (1401–1406), `build_router(config, settings, client)` (1409), `DEFAULT_CONFIG`; `OllamaHealth.reachable/missing` → log text (2219–2222); `FakeOllama.down/.models/.paths()` (642–691) → 1769, 1781, 1785, 1803 | match (strings asserted at 1774/1784 equal the f-string output) |
| A13 | 2→4 | `build_router`, `DEFAULT_CONFIG`, `FakeOllama` → `helpers.make_ctx` (2436–2443) | match |
| A14 | 2→5 | `Router.embed("embed", texts) -> EmbedResult(vectors, model_version)` (1379–1395), dimension check `ProviderError …1024` (1384–1388), `ProviderUnavailable` → `embed_pending` (3379–3382); `FakeOllama(dims=768)` (3291) | match |
| A15 | 2→6 | `Router.chat(task, messages, images=, json_schema=, temperature=, max_tokens=) -> ChatResult` (1355–1377) ← `_caption` (3853–3860); `max_tokens` → `options.num_predict` (1253–1254), asserted (3576); HTTP 404 → `ProviderUnavailable` (1228–1229) ← outage path (3861), asserted "HTTP 404" (3640) | match |
| A16 | 2→7 | `Router.model_version("asr")` = `local:faster-whisper/large-v3-turbo-int8` (941) ← 4497, asserted 4132; `LOCAL_ASR_MODELS` (1058) ↔ `WHISPER_SIZE`/`WHISPER_COMPUTE` (4297–4298) | match (the agreement is by convention, not checked in code) |
| A17 | 2→8 | `load_providers` (1133), `build_router(config, settings, http)` (4827), `OllamaProvider(url, keep_alive=, client=).pull/aclose` (4851–4860), `DEFAULT_TIMEOUT` (4823), `ollama_models()` (1111) ← `pull_models`; `DEFAULT_OLLAMA_MODELS` (631) asserted (4736) | match (task order caption, embed, chapter_summary, recap = list order) |
| A18 | 2→9 | `load_providers`, `ProvidersConfigError` (1061), `build_router(...).health()` → `model_health` (5332–5339); `HEALTH_TIMEOUT` sent per request (1282) vs new client timeout `STATUS_HTTP_TIMEOUT` (5395) | match; the second timeout repeats the first → F5 |
| A19 | 2→10 | providers.yaml header "Mounted read-only into api and worker at /etc/loopi/providers.yaml" (1150–1151) ↔ compose mounts (5973, 6006) and `PROVIDERS_MOUNT` (5872) | match |
| A20 | 3→4 | `dispatch.defer_keyframes(pool, ids)` (2143) ← 3121; `jobs.job_app/RETRY/ANALYZE_VIDEO` ← 3137; `get_context`/`PipelineContext` ← 3035; `TASK_MODULES` += video (3145–3148); defer queue `video`, lock `video:<device>` (2088) = decorator queue (3137); helpers `T0/insert_item/fetch_*/jobs_by_lock`, fixture `jobs_open` | match |
| A21 | 3→5 | `defer_embed` (2148) ← 3350; `INDEX_MARKER` queue `light` (2091) = decorator (3392); queueing lock `embed` in `enqueue` (2152) and decorator (3399); dispatcher defers embed only when moments wait and the model is ready (2212–2217) = T5 ruling (3188, 3397–3398); `TASK_MODULES` += moments (3407–3411) | match; once an embed run dead-letters, the next tick defers a new one (queueing lock covers `todo` only) → F2 |
| A22 | 3→6 | `defer_keyframe` (2123); `dispatch_pending(pool, router)` ← 3652/3666; the dispatcher selects `caption IS NULL AND caption_job_id IS NULL` (2206) ↔ the outage path sets `caption_job_id = NULL` (3866–3868), so the next healthy tick defers again; `CAPTION_KEYFRAME` queue `caption`, lock `kf:<id>` (2134) = decorator (3905); `recover_stalled` writes `caption_error` (2248–2251); `TASK_MODULES` += caption | match |
| A23 | 3→7 | `TRANSCRIBE_AUDIO` queue `audio`, lock `asr` (2090) = decorator (4556); `defer_embed` ← 4539; `PipelineContext.transcriber` typed through a `TYPE_CHECKING` import of `.asr.Transcriber` (2030–2031) ↔ protocol (4341); `TASK_MODULES` += asr | match |
| A24 | 3→8 | `open_jobs` (1974) ← 4828; `recover_stalled` (2228) ← 4829; `set_context` ← 4827/4832; `PIPELINE_TASKS`, `DISPATCH_PENDING`, `RETRY_STALLED`, `REMOVE_OLD_JOBS` ← 4654; `job_app.periodic_registry` swapped in 4696 (and by T6 `run_jobs`, 3464–3471) | match |
| A25 | 3→9 | `app.py`: T3 `create_app(settings, pool)` + `open_jobs` lifespan (2318–2358) replaced by T9 (5398–5447); `job_app.job_manager.retry_job_by_id_async` (5350) needs that lifespan; `STALLED_ERROR` (2082) shown through `analysis_error` in `FAILED_JOBS`; `TASK_LABELS`/`NOT_RETRIED` (5244–5250, 5283) repeat task names as literals | match (T3→T9 diff adds only `httpx`, `STATUS_HTTP_TIMEOUT`, `http_client`, `owns_http`, `app.state.http`; T3's lifespan test 1910–1919 still holds). The periodic tasks (1959–1961) have no label and would get a retry button → F2, F5 |
| A26 | 3→10 | fast path `put_item` (2287–2292) + dispatcher → smoke waits for `analysis_state='done'` (6202–6214); the per-device `video:<device>` lock keeps the smoke video off the owner's chain | match |
| A27 | 3→11 | cap of 20 → Step 3 "behind up to ~20 queued video jobs … 600 s" (6371); Step 6 SQL reads `procrastinate_events` `started`/`succeeded` (6429–6440) ↔ `remove_old_jobs` deletes succeeded jobs and their events after 7 days (2269–2273) | match (Step 5 measures within 24 h of the drain) |
| A28 | 4→5 | `state.record_item_error` (2992) ← 3359; `make_ctx`; T5's `TASK_MODULES` keeps `video` (3407–3411) | match; the in-transaction "mark done" SQL is repeated (3346–3349 vs 3106–3110) → F6 |
| A29 | 4→6 | `state.error_text`, `record_keyframe_error`, `keyframe_exists` (2978, 2996, 3016) ← 3742, 3898–3901; `keyframe.path` relative to `media_root` (3090–3093) ← `media_root / kf["path"]` (3851); `helpers.insert_keyframe` path (3485) = `keyframe_rel_path` layout (2903–2905) for UTC `ts` | match |
| A30 | 4→7 | `error_text/finish_item/item_exists/record_item_error` ← 4288; "raw file missing → skipped" (3072–3074) mirrored in 4493–4495; `make_ctx(transcriber=)` | match; the lock and mark-done SQL are repeated (3096–3098 vs 4501–4503; 3106–3110 vs 4534–4537) → F6 |
| A31 | 4→8 | `analyze_video_job` under the real worker (4659–4682), `make_h264`, `TWO_SCENES` | match (2 keyframes → 2 captions, 4676) |
| A32 | 4→9 | `keyframe.path` → `<img src="/media/{{ kf.path }}">` (5716), `/media` = `media_root` (5443) | match |
| A33 | 4→10 | smoke video testsrc2 320x240, 10 frames (6195–6196) must yield ≥ 1 keyframe with the default params; `rm -rf …/keyframes/$DEVICE_ID` (6107) ↔ `keyframes/<device>/…` (2905) | match (testsrc2 frame 0 is picked in T4's own test, 2567); not runnable here (compose forbidden) |
| A34 | 5→6 | `embed_moments` registered in T5 before T6's real-worker tests run captions that defer it (rationale 3186) | match |
| A35 | 5→7 | speech moments call `defer_embed` only when `moments > 0` (4538–4539); "no embedding run" asserted on hallucinations (4160) | match |
| A36 | 5→8 | `index_marker_job` + `embed_moments_job` under the real worker: an unreachable Ollama ends the run quietly (3380–3382) → `embedding: None`, job `succeeded`, `attempts: 2` (4699–4702) | match |
| A37 | 5→9 | `NOT_RETRIED = ("embed_moments",)` (5283) = `jobs.EMBED_MOMENTS` (1958); failed jobs are kept (2272) and "Xử lý lại tất cả" skips embed (5361) | names match; failed runs accumulate → F2 |
| A38 | 6→7 | T7's `TASK_MODULES` (4564–4570) keeps `caption`; helpers append order T6 then T7 | match |
| A39 | 6→8 | `run_jobs(ctx)` (3454) ← 4672; `caption_keyframe_job(context, keyframe_id)` with `pass_context=True` (3905–3907) | match |
| A40 | 6→9 | `helpers.insert_keyframe` ← 5106–5107, 5139; `Caption.scene/activity` stored through `model_dump()` (3789–3790, 3876) ← template (5716–5718); `captioned_at` ← `VIDEO_LATENCY` (5256); `caption_error` ← `FAILED_JOBS` (5267) | match. A keyframe whose caption job is dead-lettered still counts as waiting (5306) and shows "Đang chờ mô tả…" (5719) → F3 |
| A41 | 7→8 | `WhisperTranscriber(settings)` ← 4827; `WHISPER_SIZE` ← `download_whisper` (4840) and the label `faster-whisper/large-v3-turbo` (4863, asserted 4737); `download_root=model_dir/whisper` (4445, 4456) = `cache_dir=model_dir/whisper` (4840); `make_opus`, `FakeTranscriber`; `asr.speech_regions` monkeypatched (4667) | match |
| A42 | 7→9 | speech moments `kind='speech'` + `lang` → `m.lang\|upper` (5725); `AUDIO_LATENCY` uses `analyzed_at` set at 4535 | match |
| A43 | 7→10 | `HF_HOME=/srv/loopi/models/hf`, `LOOPI_MODEL_DIR=/srv/loopi/models` (5997–5999) → Whisper cache `<model_dir>/whisper`; `OMP/OPENBLAS_NUM_THREADS=1` (6002–6003) next to `cpu_threads` (4456) | match |
| A44 | 8→9 | `health.HEARTBEAT_MAX_AGE_S = 60` (4778) vs `pipeline_status` hard-coding `timedelta(seconds=60)` (5320) | values match, two sources → F5 |
| A45 | 8→10 | CLI `worker` (4994), `worker-health` (4996, reads `LOOPI_DATABASE_URL`), `models pull` (4998) ← compose `command` (5988), healthcheck (6009), README step 4 (6252), smoke (6218) | match; interval 30 s (6010) = ruling (4606) |
| A46 | 8→11 | `models pull` via `docker compose -p deploy run --rm --no-deps worker` (6365); `worker-health` | match |
| A47 | 9→10 | README "Pipeline (P2a)" (6269–6270) describes `/pipeline`; api gets `LOOPI_PROVIDERS_FILE` + mount (5969–5973) for the status page (ruling 5041) | mismatch: "each with a 'Xử lý lại' button", but `embed_moments` has none (5805–5806) → F9 |
| A48 | 9→11 | `/pipeline` used in Step 5 (6399); Step 6 latency SQL (6444, 6450) = `VIDEO_LATENCY`/`AUDIO_LATENCY` (5256, 5261) | match |
| A49 | 10→11 | `SMOKE_ANALYSIS_TIMEOUT` (6092) ← 6368; `COMPOSE_PROJECT_NAME=deploy` works because smoke uses plain `docker compose exec` (6103–6109); service names → `deploy-$s-1` (6405); volume `ollama` (6060) | match |
| A50 | 6→10 | the smoke pipeline block needs no model (6194): only `analyze_video` must run | match |
| A51 | 2→11 | `deploy/providers.yaml` is bind-mounted from the worktree after Step 3 (6314) | match (covered by Task 11 "Facts") |
| A52 | 5→10 | the smoke marker becomes a `marker` moment; cleanup deletes `moment` before `item` (6104); visual and speech moments carry `item_id` (3883, 4531) and cascade | match |

## B. Per-task self-consistency (Tasks 1–10)

| Task | Tests ↔ code (names, signatures, expected values) | "Expected: PASS (N)" | Files ↔ `git add` | Undefined references | Finding |
|---|---|---|---|---|---|
| B1 · T1 | `PROCRASTINATE_SCHEMA`, `migrate`, `fresh_database`, P1 `paired_device`/`pool`/`pki_settings`; Step 3 failure texts match the code; returned lists (289, 312) match `migrate()` order | 9 = config 2 + db 7 (576) ✓ | 110–114 ↔ 584–586: same 9 paths ✓ | none; Step 2/7 anchors exist in P1 (`import datetime as dt`, the `APP_TABLES` line) | OK |
| B2 · T2 | error messages vs `match=` (823–826 ↔ 1227); timeout dict (885 ↔ 1186); `StubProvider` signature = `Provider` protocol | 27 (1417) ✓ | 600–603 ↔ 1422 ✓ | none | docstrings 1016/1020 contradict the ruling (614) → F7; no full-suite run → F12 |
| B3 · T3 | log strings (1774, 1784, 1841) ↔ 2119, 2219–2222; monkeypatched `jobs.enqueue` (1835) and `job_app.configure_task` (1882) are looked up as attributes (2104, 2133, 2152, 2001); `remove_old_jobs(SimpleNamespace(app=…))` (1897) ↔ 2271–2273 | 18 (2364) ✓ | 1436–1439 ↔ 2372–2373 ✓ (directory add) | none; the `put_item` anchor exists (P1 device_routes.py 77–79) | Step 2 says P1 `client` tests fail "until Step 5" (1925); they pass again after Step 3 → F13 |
| B4 · T4 | `video.devices.new_id` (2729), `video.extract_jpeg`/`read_small_frames` (2747, 2763) and `frames.FFMPEG_TIMEOUT_S` (2602) are all module-global lookups in the code; `KeyframeParams(10, 3, 20, 60)` follows field order | 25 = 14 + 11 (3154) ✓ | 2387–2390 ↔ 3162–3164 ✓ | none | duplicated defaults → F5 |
| B5 · T5 | `moments.marker_text` patch (3260) ↔ global call (3343); "embedding paused" (3284 ↔ 3381) | 10 (3417) ✓ | 3177–3180 ↔ 3422 ✓ | none | no full-suite run → F12; `_moment` re-defined → F4 |
| B6 · T6 | `caption.prepare_image` patch (3707) ↔ module-global lookup (3851); `ctx.router.chat` instance patch (3684); "five" worker runs (3924) = 1 + 3 + 1 ✓ | 14 (3924) ✓ | 3435–3439 ↔ 3932–3933 ✓ | none | prompt "up to 10" vs limit 20 → F8 |
| B7 · T7 | `asr.speech_regions` patch ↔ global lookup (4477); `faster_whisper.WhisperModel` patch ↔ lazy import (4452); `ValueError` match "LOOPI_ASR_LANGUAGES" (4241 ↔ 4442) | 28 + 1 skip (4576) ✓ | 3946–3950 ↔ 4583–4584 ✓ | none | no full-suite run → F12 |
| B8 · T8 | `worker.pull_models` (4747) and `faster_whisper.download_model` (4756) patches ↔ lazy imports (4977, 4838); "ready: …" (4749 ↔ 4980); "worker alive" (4718 ↔ 4971) | 8 + 3 (5006) ✓ | 4597–4600 ↔ 5014–5015 ✓ | none | File Structure line 88 places `worker_alive` in `worker.py` → F14 |
| B9 · T9 | every asserted HTML string exists in the specified templates; the 403/409 paths match `require_htmx`/`HTTPException(409)` | 8 (5825) ✓ | 5028–5032 ↔ 5833–5834 ✓ (`web/` directory add) | none | static 409 assertion → F10; waiting-count asymmetry → F3 |
| B10 · T10 | smoke string and order assertions (5924–5933) match the script (first `expect 201 "${MTLS[@]}"` at 6183, `DEVICE_ID="$(` at 6166, trap at 6120) | 6 (6281) ✓ | 5847–5849 ↔ 6289 ✓ | none (`deploy/.env.example` exists, tracked) | Step 2 "FAIL — KeyError" while 2 of 6 tests are already green; `bash -n` runs from the repo root (6229) → F13; README → F9; trap ruling text → F15 |

## C. Global Constraints (plan lines 31–45) vs the tasks

| # | Line | Constraint | Tasks | Finding |
|---|---|---|---|---|
| C1 | 33 | spec binding | all | spec §9.4 names `keyframe.model_version`; plan uses `caption_model` (442) → F1 |
| C2 | 34 | local-first, images/audio never to cloud | T2 `Router._usable` (1346–1353) + test 964–979; T7 ASR forced `local` by the validator (1105–1108) | match |
| C3 | 35 | model names | `DEFAULT_CONFIG` (1121–1130) = `deploy/providers.yaml` (1154–1163), asserted equal (731–736); `EMBED_DIM` 1024; Whisper int8 CPU (4454–4457) | match |
| C4 | 36 | every derived row stores `model_version = "<provider>:<model>"` | T1 schema (442, 462, 480–481), T5 `loopi:marker-v1` (3320), T6 (3876, 3884), T7 (4497) | keyframe stores it as `caption_model`, embeddings as `embed_model`; markers are not `<provider>:<model>` → F1 |
| C5 | 37 | 3 retries → dead letter + reprocess button | T3 `RETRY` (1968), `recover_stalled` cap (2237–2252); T5 outage ends quietly (3380–3382); T6 outage burns no retry (3445, 3861–3870); T9 `NOT_RETRIED` (5283) | sanctioned exceptions are not in the constraint → F1; failed embed runs pile up → F2 |
| C6 | 38 | ingest unaffected | T3 fast path logs and swallows (2115–2120), test 1831–1843; T6 upload during outage = 201 (3660–3663); compose `api` does not depend on `models` (5981–5983) | match |
| C7 | 39 | contract file untouched | no task names `docs/protocol` | match |
| C8 | 40 | P2a deletes no media | T4 unlinks its own JPEGs (3112–3115); T10 smoke `rm -rf raw/ view/ keyframes/` of the smoke device (6106–6107); T11 Step 4, owner-gated (6395) | sanctioned exceptions are not in the constraint → F1 |
| C9 | 41 | versions | `procrastinate==3.10.0` (148), `PG_IMAGE` (1529), compose images (5948, 6024); Dockerfile unchanged | match (lock/uv check in Method) |
| C10 | 42 | Vietnamese UI, HX-Request on POSTs | T9 both POST routes `Depends(require_htmx)` (5631, 5637), 403 tests (5189, 5210) | match (technical loanwords "Worker", "Job", "model", "embedding" remain) |
| C11 | 43 | TDD, ≥ 80 %, conventional commits, no secrets, no Docker pruning | every task writes tests first; subjects `feat(brain)`/`feat(deploy)`; trailers present; `git add` lists only tracked paths, and the directory adds (`providers/`, `pipeline/`, `web/`) are covered by `**/__pycache__/` in .gitignore; coverage runs in T8–T10 | match. No test without an assertion: all 151 new tests assert, use `pytest.raises` or `check=True`. Duplication a reviewer will flag → F4, F6 |
| C12 | 44 | P1 suite (133) green, run from `brain/` | T1 keeps both P1 db tests (245–273); T3 keeps P1 fixtures; T8 keeps `init-pki/migrate/serve/pair-token` (test_cli 3); T9 keeps P1 routes and strings asserted by test_web | match (prototype 284/1); `bash -n deploy/smoke.sh` (6229) is the only command not run from `brain/` → F13 |
| C13 | 45 | Tasks 1–10 never run compose or smoke.sh | all 16 distinct "Run:" lines in Tasks 1–10 checked: none calls docker; T10 restates it (5855) | match |

## Findings

### Important

**F1. Three Global Constraints state absolutes that the tasks break on purpose. They are copied word for word into
`.superpowers/sdd/…/globals.md` ("bind every task").**

Lines involved: 36, 37, 40.
- **Line 37** ("retried 3 times, then dead-lettered … reprocess button"). Broken on purpose by:
  - Task 5: a model-server outage ends an embedding run quietly (3188, 3380–3382).
  - Task 6: an outage gives the caption job back without spending a retry (3445, 3861–3870).
  - Task 9: the embedding run gets no button (5041, 5283).
- **Line 40** ("P2a deletes no media"). Broken on purpose by:
  - Task 4: removes its own JPEGs when the transaction fails (2400, 3112–3115).
  - Task 10: smoke cleanup removes the smoke device's `raw/`, `view/` and `keyframes/` (5857, 6106–6107).
- **Line 36** (`model_version`). Not followed as written:
  - The keyframe column is `caption_model` (442), while spec §9.4 names `keyframe.model_version`.
  - Embeddings carry `embed_model` (480).
  - Markers use `loopi:marker-v1` (3320), which is not `<provider>:<model>`.

Reviewers who check each task against `globals.md` will raise these rulings as violations. An implementer may also
"correct" the code toward the constraint.

**Smallest fix:** add the exceptions in parentheses on lines 36, 37 and 40, and in the copy in `globals.md`:
- Line 36: "(stored as `keyframe.caption_model`, `moment.model_version`/`embed_model`,
  `transcript_segment.model_version`; marker moments use `loopi:marker-v1`)".
- Line 37: "(except: an outage ends a caption or embedding job without spending a retry, Tasks 5–6; the embedding
  run has no button because the dispatcher asks for a new one, Task 9)".
- Line 40: "deletes no recorded media (only its own JPEGs after a failed transaction, Task 4, and the smoke device's
  own rows and files, Task 10)".

**F2. Failed embedding runs pile up on the dead-letter list forever.**

Lines involved: 2148–2156, 2212–2217, 2269–2273, 3399–3401, 5243, 5264–5274, 5283, 5357–5365.
- **How it builds up.** The queueing lock `embed` covers only `todo` jobs. After an embedding run fails four times,
  the next dispatcher tick defers a new run, and that one fails too. With a persistent `ProviderError` (Ollama HTTP 500
  under memory pressure, a wrong-dimension model in providers.yaml, read timeouts) this adds about one failed run per
  ~3 min (155 s of retry waits + runtime), so ≈ 480 a day.
- **Nothing removes them.** `remove_old_jobs` keeps failed jobs, there is no button, and "Xử lý lại tất cả" skips them.
- **They hide real failures.** `FAILED_JOBS` shows the newest 100, so these rows push real item and keyframe dead
  letters off the page, and "Job lỗi (N)" counts them.
- **Same risk for periodic tasks.** The three periodic tasks (2256–2273) have no `RetryStrategy`, no label in
  `TASK_LABELS` and are not in `NOT_RETRIED`, so a failing tick would get a pointless retry button.

**Smallest fix:**
- In `dispatch_pending`, right before `defer_embed`, keep only the newest failed embedding run:
  `DELETE FROM procrastinate_jobs WHERE task_name = 'embed_moments' AND status = 'failed' AND id < (SELECT max(id)
  FROM procrastinate_jobs WHERE task_name = 'embed_moments' AND status = 'failed')`. Events cascade, and embed jobs
  have no periodic-defer rows.
- Add `jobs.DISPATCH_PENDING`, `RETRY_STALLED` and `REMOVE_OLD_JOBS` to `NOT_RETRIED`.
- Add one Task 3 test: two failed embed runs + one tick → one failed row.

### Minor

**F3. A dead-lettered caption looks like it is still waiting.** Lines 5276–5281, 5306–5307, 5512, 5518–5521,
5718–5719, 5745–5747.
- Items whose job is dead-lettered are left out of the "chờ" count and show "Lỗi phân tích".
- A keyframe whose caption job is dead-lettered still counts in "Khung hình chờ mô tả" and shows "Đang chờ mô tả…"
  forever.

**Fix:**
- In `pipeline_status`, count waiting keyframes with the same `LEFT JOIN procrastinate_jobs j ON j.id =
  k.caption_job_id AND j.status = 'failed' … AND j.id IS NULL` as `PENDING_ITEMS` uses.
- In `_day_by_hour`, select `j.status IS NOT DISTINCT FROM 'failed' AS caption_failed` and show "Lỗi mô tả".
- If the asymmetry is kept on purpose, record a ruling instead.

**F4. Test helpers are re-defined, although `helpers.py` says "later tasks import from here instead of redefining"
(P1 `helpers.py` line 1).**
- An `INSERT INTO moment` helper exists three times:
  - `test_dispatch._moment_waiting` (1788–1794)
  - `test_moments._moment` (3209–3217)
  - `test_web_pipeline._moment` (5090–5096)
- `test_dispatch._insert_keyframe` (1635–1644) duplicates Task 6's `helpers.insert_keyframe` (3482–3493).

**Fix:** add `helpers.insert_moment(pool, device_id, *, kind="marker", ts=T0, text="x", lang=None) -> str` in Task 3
and use it in Tasks 5 and 9, or record a ledger ruling that test-local inserts stay.

**F5. Constants and defaults kept in two places.**
- **The one with an effect:** `frames.KeyframeParams` defaults (2819–2822) repeat `Settings.keyframe_*` (399–402).
  Task 11 Step 7 (6472) tunes only `config.py` + `test_config.py`, so `test_frames.PARAMS = KeyframeParams()` (2470),
  including Review Focus tests 1–2, keeps testing the old values.
- **Cosmetic:**
  - `DISPATCH_MAX_QUEUED` (2079) repeats `Settings.dispatch_max_queued` (398).
  - `pipeline_status` hard-codes 60 s (5320) instead of `health.HEARTBEAT_MAX_AGE_S` (4778; a psycopg-only module).
  - `STATUS_HTTP_TIMEOUT` (5395) repeats `HEALTH_TIMEOUT` (1186), which `health()` already sends with each request.
  - `TASK_LABELS`/`NOT_RETRIED` (5244–5250, 5283) use string literals instead of `jobs.*`.

**Fix:** in Task 11 Step 7, add "and the same values in `frames.KeyframeParams`". For the others, import the
existing constant (one line each) or record a ruling.

**F6. The same transaction SQL is repeated in three stages.**
- `UPDATE item SET analysis_state = 'done', analyzed_at = now(), analysis_error = NULL WHERE id = %s` appears in
  video.py (3106–3110), moments.py (3346–3349) and asr.py (4534–4537), next to `state.finish_item` (3000–3007).
- `SELECT 1 FROM item WHERE id = %s AND analysis_state = 'pending' FOR UPDATE` appears in video.py (3096–3098) and
  asr.py (4501–4503).

**Fix:** add `state.lock_pending_item(conn, item_id) -> bool` and `state.mark_item_done(conn, item_id)` in Task 4 and
use them in Tasks 5 and 7, or record a ruling.

**F7. `providers/base.py` docstrings contradict the Task 2 error-mapping ruling.** Lines 1015–1020 vs 614.
- `ProviderError` says "answered, but with an error (HTTP >= 400 …)". In fact HTTP 404 is `ProviderUnavailable`, and
  read timeouts or dropped connections (no answer at all) are `ProviderError`.
- `ProviderUnavailable` says "(connection refused, DNS failure, timeout)". It omits 404 and suggests read timeouts.

**Fix:** reword both docstrings to match line 614.

**F8. The caption prompt and the caption limits disagree.** `CAPTION_PROMPT` asks for "up to 10 notable objects"
(3750–3751), while `MAX_OBJECTS = 20`, the schema's `maxItems: 20` (3755, 3768) and the Interfaces (3443) say 20.
Tests assert 20 (3577, 3592).

**Fix:** write "up to 20" in the prompt.

**F9. The README promises a button that does not exist for every job.** Lines 6269–6270 say every failed job has a
"Xử lý lại" button, but `embed_moments` shows "tự chạy lại mỗi phút" instead (5283, 5805–5806).

**Fix:** append "(the embedding run restarts by itself)".

**F10. One assertion in the retry test checks static text, not the retry.** Line 5196 asserts
`'409: "Job không còn ở trạng thái lỗi'` on GET `/pipeline`. That string is a JS constant in `base.html`
(5681–5682) and appears on every page, whatever the retry did. The comment "explained in Vietnamese" suggests it
checks the refusal, which it does not.

**Fix:** move it into its own one-line test of the error map, or rename the comment to say it checks the template.

**F11. The committed ledger file is never created before Task 11.** Lines 18, 6307 ("Modify"), 6492–6497.
- Nothing in Tasks 1–10 creates or commits `docs/superpowers/ledgers/p2a-progress.md`. The run's ledger lives in the
  git-ignored `.superpowers/sdd/…/progress.md`.
- Task 11 Step 9's `git add` fails unless the controller has copied it.
- Task 11 waits for the hardware soak, so rulings stay uncommitted until then.

**Fix:** have the final whole-branch review commit copy `progress.md` to `docs/superpowers/ledgers/p2a-progress.md`,
and change "Modify" (6307) to "Create or update".

**F12. Three tasks never run the full suite.** Tasks 2 (1414–1417), 5 (3414–3417) and 7 (4573–4576) run only their
own test file, while every other task ends with the full suite. Task 2 edits `helpers.py`; Tasks 5 and 7 edit
`TASK_MODULES`, which the later real-worker tests import.

**Fix:** add "Run: `cd brain && uv run pytest -q` → all pass" to those three steps.

**F13. Some expected outputs and one command path are worded inaccurately.**
- Task 3 Step 2 (1925): P1 `client` tests fail "until Step 5". They pass again after Step 3, because `client` needs
  only `pipeline/jobs.py`.
- Task 10 Step 2 (5939): "FAIL — KeyError: 'worker'". But `test_every_image_is_pinned` and
  `test_smoke_script_is_valid_bash` already pass on the P1 files.
- Task 10 Step 4 (6229): `bash -n deploy/smoke.sh` must run from the repo root, while line 44 says to run everything
  from `brain/`.

**Fix:** reword both expected outputs; write `cd .. && bash -n deploy/smoke.sh`, or "from the repo root".

**F14. The File Structure puts a function in the wrong file.** Line 88 lists `worker_alive` in `pipeline/worker.py`,
but Task 8 defines it in `health.py` (4604, 4781); line 69 is already right.

**Fix:** drop `worker_alive` from line 88.

**F15. The Task 10 ruling promises more cleanup than the script gives.**
- The ruling (5857) says the exit trap cleans up "as soon as the smoke device exists".
- In the script, `DEVICE_ID` is still empty between the pairing call (6162) and the lookup (6166–6168).
- A failure there (certificate extraction, or the lookup's "FAIL: smoke device not found") leaves an active,
  unrevoked smoke device. It holds no data.

**Fix:** reword the ruling to "as soon as its id is known", or revoke by fingerprint in the lookup's failure branch
(`UPDATE device SET revoked = TRUE WHERE cert_fingerprint = '$FP'`).

Rows: 52 (A) + 10 (B) + 13 (C) = 75. No Critical finding.
