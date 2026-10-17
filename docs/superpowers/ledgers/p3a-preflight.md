# P3a pre-flight scan (controller, 2026-10-17)

## Task pairs sharing a file or an interface
| Tasks | Produced → consumed | Finding |
|---|---|---|
| T1→T3 | `tokens.check_token(pool, token) -> str|None`, `create_token`, `revoke_token`, `list_tokens` (gate + tests) | match |
| T1→T4 | `create_token`, `list_tokens`, `revoke_token`, `TokenError`, `NAME_MAX` (Cài đặt routes, CLI) | match |
| T1→T5 | migration 006, `loopi-brain mcp-token create` (T4 CLI) | match |
| T1 conftest `APP_TABLES` → T2–T4 tests | `mcp_token` truncated per test | match; T4 CLI test uses `database_url` (no truncate) and finds its token by name — ok |
| T2→T3 | `memory.search_memories(pool, router, settings, query, start, end, limit)`, `get_timeline(pool, day, zone)`, `get_day_recap(pool, day, zone)`, `get_moment(pool, settings, id, images) -> (MomentOut, bytes|None)`, `list_commitments(pool, status)`; output models; `store.get_mcp`; `helpers.insert_moment(keyframe_id=)` | match |
| T2→T4 | `store.McpSettings`, `get_mcp`, `set_mcp` | match |
| T3↔T4 | both edit `cli.py` (T3 `_serve` logging, T4 `mcp-token`): different functions; T3's `/settings` test runs before T4 adds the card (renders without it) | no conflict |
| T2↔T3 day_routes/thumbs | T2 refactors `/thumb` into `thumbs.keyframe_thumb`; T3 does not touch it | match |

## Each task against itself
| Task | Tests vs code | Files created vs touched | Finding |
|---|---|---|---|
| T1 | messages fixed; `"Bearer "+token` refused by prefix; list keys exact | ok | clean |
| T2 | bounds/empty/limit tests match `parse_when`/`search_memories` order (bounds first, then query); limit test patches `search.search_moments` (40 candidates cap made a 60-row test impossible — fixed while writing) | `helpers.insert_moment` changed in Step 1 before use | clean |
| T3 | error table texts = R6 texts; crash test patches `memory.get_timeline` looked up at call time; logging test relies on `logging.getLogger("mcp").setLevel(WARNING)` at import | ok | ⚠ SDK attribute names (`output_schema`, `mime_type`) from research — brief allows the smallest fix |
| T4 | token extraction used whitespace splitting that misses `<code>loopi_…</code>` → switched to a regex (plan fixed) | ok | fixed |
| T5 | controller-only | — | — |

Rubric hits: none (no assert-nothing test, no duplicated logic block).
