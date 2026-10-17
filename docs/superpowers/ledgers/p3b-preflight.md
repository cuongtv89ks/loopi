# P3b pre-flight scan (controller, 2026-10-17)

Every task's code and tests were applied to the worktree in scratch and run before the plan was committed, then reverted:
T1+T2 17 passed; T3 9 passed (and the renderer on the brain image's ffmpeg 7.1.5: 240 segments, 152 s / 212 s CPU);
T4 with test_forget/test_web_pipeline/test_retention 104 passed; T5 with test_day_page/test_web 39 passed; full suite
981 passed (+ the two test_db lists that T1 updates).

## Task pairs sharing a file or an interface
| Tasks | Produced → consumed | Finding |
|---|---|---|
| T1→T2 | `Material`, `Candidate(ref, kind, at, frame, text, score, audio_item, audio_offset)`, `Frame` | match (run) |
| T1→T4 | `vlog_material(pool, day, zone)`; `APP_TABLES` gains `vlog` (T1) for T4/T5 tests | match (run) |
| T2→T4 | `write_script(router, material, zone) -> Script`, `to_json(script)` (zone dropped while writing) | match (run) |
| T3→T4 | `render.render(**kw) -> RenderResult(.out, .vtt, .seconds, .mode, .skipped)`, `Segment/Still/Keyframe`, `RenderError/InvalidMedia`; `helpers.make_mp4` (T3) used by T4 tests | match (run) |
| T4→T5 | `vlog_lock`, `vlog_rel`, `vtt_rel`, `request_rebuild`; job names | match (run) |
| T4↔T5 | T4 edits forget.py, status.py, pipeline.html, forget templates; T5 edits app.py, day_routes.py, day.html, base.html | no shared file |
| T1↔P3a | test_db lists after `006_mcp.sql` | match |

## Each task against itself
| Task | Tests vs code | Files | Finding |
|---|---|---|---|
| T1 | slideshow expectation follows `llm.spread` (fixed while writing) | ok | clean |
| T2 | `to_json(script)` signature fixed while writing | ok | clean |
| T3 | test segments made deterministic (`scenecut=0`) while writing; 9 tests | ok | clean |
| T4 | coverage of vlogs.py measured 87 % (failure paths); plan text says so | ok | clean |
| T5 | `base.html` has only a `content` block (title blocks dropped while writing) | ok | clean |

Rubric hits: none.
