# SDD ledger — plan: docs/superpowers/plans/2026-10-17-p3a-mcp-server.md

Owner approval 2026-10-17: "ok" (plan reviewed; execution by subagent-driven development per CLAUDE.md). Spec: docs/superpowers/specs/2026-10-17-p3-mcp-vlog-design.md (approved "ok") + binding spec D24.
Branch feat/p3a from feat/p3 @ 8cb4943 in worktree /home/soul/loopi-p3. Task 5 (deploy) is controller-executed after the owner's yes.

## Plan rulings (R1–R11, copied from the plan)
- Ruling: R1 — When the embedding model cannot be reached, `search_memories` answers word matches with `"semantic": false` instead of the design's "search is not ready" error. The search page and the chat do the same, and the client can say "matched by words only". Cost if wrong: one boolean the client may ignore. Task 4 updates the design doc line.
- Ruling: R2 — "Each text is clipped at 500 characters" applies to moment texts and chapter summaries. The recap markdown is clipped at 8000, because a recap is 300–1500 characters and clipping it at 500 would cut most of it. Cost if wrong: a larger tool result.
- Ruling: R3 — The package is `loopi_brain/mcp/`, following the spec's `brain/…/mcp/` layout. Its modules import the SDK with absolute imports (`from mcp.server…`). `loopi_brain/mcp/server.py` does **not** use `from __future__ import annotations`, because the SDK reads the tool annotations when a tool is registered. Cost if wrong: a rename.
- Ruling: R4 — The tools reach the pool, settings and model client through a closure over a small `Deps` dataclass built in `create_app`, not through the SDK's lifespan context. The research verified both. The closure needs no extra lifespan. Cost if wrong: none.
- Ruling: R5 — `loopi-brain serve` calls `logging.basicConfig(level=logging.WARNING)` before building the app. `MCPServer()` calls `basicConfig(INFO)` when the root logger has no handler, which would start printing every INFO line of the api. Configuring the root logger first keeps today's level (warnings and errors). The `mcp` logger is set to WARNING when `loopi_brain.mcp.server` is imported. Cost if wrong: the log format changes to `LEVEL:logger:message`.
- Ruling: R6 — Tool errors are returned as `CallToolResult(is_error=True)` with a fixed text: `no such moment`, `date must be YYYY-MM-DD`, `start must be before end`, `query is empty`, `status must be open, done or dismissed`, `invalid arguments`, `internal error`. `ToolError` would add an `Error executing tool …` prefix. Dates are typed `str` and parsed by the brain, so a bad date never reaches pydantic's echoing validator. Cost if wrong: none.
- Ruling: R7 — DNS-rebinding protection is turned off (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`). Behind Caddy, every request carries the LAN Host header and would otherwise be refused with 421. The bearer gate decides who may call. Cost if wrong: none for a server that needs a token for every request.
- Ruling: R8 — The gate answers 405 to anything but POST once the token is good. Stateless JSON needs only POST, and a GET would hold an idle event stream open. Cost if wrong: a client that insists on GET streams; none known.
- Ruling: R9 — `last_used_at` is written at most once a minute per token. A tool call is one request, and Claude Code makes several a minute. Cost if wrong: "last used" is up to a minute old.
- Ruling: R10 — `loopi-brain mcp-token create|list|revoke` lets the controller and the owner make a token without the page. Task 5 uses it to register Claude Code without the token ever being printed into the session. Cost if wrong: one more CLI command.
- Ruling: R11 — No Caddyfile change other than the `/mcp*` block: request body ≤ 64 KB, fingerprint header stripped, no basic auth, no client certificate. TLS remains Caddy's `verify_if_given` policy, so Claude Code needs no client certificate, only `NODE_EXTRA_CA_CERTS` pointing at the Loopi CA. Cost if wrong: none.

## Pre-flight scan
Table: preflight-scan.md (this directory). Rulings:
- Ruling: T4 token-extraction test uses a regex (whitespace split missed the token inside <code>) — plan text fixed before dispatch — cost if wrong: none.
- Ruling: implementer instructions allow the smallest fix for an SDK attribute-name mismatch (research-verified, not a full prototype), reported — cost if wrong: a reviewer catches a wrong guess.

Task 1: dispatched (BASE 34e4ddc)
Task 1: implementer DONE 7f74819 (846 passed + 1 skipped, 99 %); uv.lock additions only (mcp, mcp-types, pyjwt, sse-starlette; httpx2/httpcore2/truststore were already locked).
Task 1: review dispatched (sonnet, review-34e4ddc..7f74819.diff)
Task 1: review ✅ spec, quality approved, 0 Critical/Important.
Task 1: minor (deferred, final review — fix in the final wave): a token name with NUL passes clean_name and raises psycopg.DataError (500 on Cài đặt, traceback in the CLI) instead of TokenError — drop control characters like search.clean_query; concurrent checks can write last_used_at more than once a minute (cosmetic; a guarded UPDATE fixes it); the "never repeats the name" assert only bites for the 61-char case; uv.lock gained a resolution-markers header (no version change).
Task 1: complete (commits 34e4ddc..7f74819, review clean, 0 fix rounds)
Task 2: dispatched (BASE 7f74819)
Task 2: implementer DONE b72b338 (875 passed + 1 skipped, 99 %); +2 tests beyond the brief (well-formed unknown id; visual moment without keyframe); day_routes keeps an unused logger.
Task 2: review dispatched (opus, review-7f74819..b72b338.diff)
Task 2: review ✅ spec, quality approved, 0 Critical/Important; 8 minors in task-2-review.md.
- Ruling: get_moment with images=True may write the keyframe's thumbnail cache file (thumbs/), exactly as viewing the Day page does — a cache, not data; "read only" means no row and no recording changes — cost if wrong: one 320 px JPEG per keyframe ever sent.
Task 2: minor (deferred, final review — fix in the final wave): no test pins the local `day` of a moment just after local midnight (UTC previous day); parse_when accepts basic ISO forms (20261014, 2026-W42-3) as midnight datetimes, so as an end bound they exclude that day while 2026-10-14 includes it — accept only YYYY-MM-DD or extended date-times; a dropped picture leaves no log line; golden-keyframe test gaps; day_routes keeps an unused logger; a test scoped to the old logger name.
Task 2: complete (commits 7f74819..b72b338, review clean, 0 fix rounds)
Task 3: dispatched (BASE b72b338)
Task 3: implementer DONE dbd0040 (892 passed + 1 skipped, 99 %; server.py 98 %); mutation-checked the echo middleware and the mcp logger level; SDK names matched.
Task 3: review dispatched (opus, review-b72b338..dbd0040.diff)
Task 3: review ❌ spec (plan-mandated), changes needed — I1: hide_validation_input matches only "1 validation error for"; with ≥ 2 bad fields pydantic writes "N validation errors for" and the whole argument dict (with the secret) reaches the client (reproduced both modes).
- Ruling: any tools/call error text starting with "Error executing tool " is the SDK's (our tools never produce it) → "invalid arguments" if it contains " validation error", else "internal error"; "Unknown tool: …" stays (names only the tool) — cost if wrong: an SDK error the owner might have wanted to see is generic.
- Ruling: fold Minors — a legacy-mode tool call test; `limit: null` behaves like a missing limit (10) — cost if wrong: none.
Task 3: fix round 1/5 dispatched (resume implementer)
Task 3: fix round 1 done 87960f7 (905 passed + 1 skipped, 99 %); SEARCH_LIMIT constant; SDK crash outside _run still logs a traceback (parked Minor 2; unreachable for real tools).
Task 3: fix round 1/5 (3 addressed, 0 open — every SDK "Error executing tool …" text becomes a fixed text, legacy-mode calls tested, limit null = default; commits dbd0040..87960f7)
Task 3: minor (deferred, final review — fix in the final wave): M2 an output-conversion failure outside _run would make the SDK log result data at ERROR (logger.exception with input_value) — add a filter on mcp.server.mcpserver.server that drops exc_info; M3 the gate fails open for non-HTTP scopes (unreachable via Route; fail closed); M4 a DB outage in check_token gives 500 + traceback — answer a fixed 503, log the class only; M5 the gate test's revoked-row check is vacuous and lacks `Bearer `/HEAD/OPTIONS/DELETE shapes; M6 no validation-error-with-secret case in the logs test; M7 the SDK client logs under the top-level "client" logger (comment overstates; tests only).
Task 3: complete (commits b72b338..87960f7, review clean after 1 fix round)
Task 4: dispatched (BASE 87960f7)
Task 4: implementer DONE 42d6130 (918 passed + 1 skipped, 99 %).
Task 4: review dispatched (sonnet, review-87960f7..42d6130.diff)
Task 4: review ✅ spec, quality approved, 0 Critical/Important (⚠ Caddy syntax/normalisation unverifiable without caddy — Task 5 runs `caddy validate`).
Task 4: minor (deferred, final review — fix in the final wave): the "Ảnh qua MCP: đang tắt" line is not refreshed after saving the switch (contradicts "Đã lưu"); NUL in a token name or a revoke id → 500 (same root as Task 1's minor); the shown token stays in the DOM/bfcache until a swap; README step 2 relies on $LOOPI_DATA_DIR being set in the user's shell; _mcp_tokens.html hard-codes the owner's data path as an example; test gaps (fragment headers, hx-confirm escaping, revoke forms method=post, order-dependent CLI line); `/mcp*` matcher wider than the one path.
Task 4: complete (commits 87960f7..42d6130, review clean, 0 fix rounds)
Tasks 1–4: complete. Final whole-branch review dispatched (opus, 34e4ddc..42d6130).
Final review (opus): With fixes — 0 Critical, 2 Important: I1 Caddy matches the cleaned path but forwards the raw one, so `POST /commitments/../mcp` skips basic auth and reaches a web handler (not exploitable today; P3b adds routes); `POST /mcp/` 307s to http://; I2 the "mcp <tool> ok" log lines never print in production (root WARNING) and carry no count (design §3.2).
- Ruling: final fix wave = I1 (`rewrite * /mcp` inside the Caddy block, pinned in test_deploy, a `curl --path-as-is` check added to the plan's Task 5), I2 (loopi_brain.mcp.server logger at INFO, result count in the line, a test under a WARNING root logger), and the reviewer's "fix now" minors #5 (local-day test after local midnight), #14 (gate shapes: empty Bearer, HEAD/OPTIONS/DELETE/PUT; direct revoked-row assert), #17 (picture-status line refreshed after saving), #20 (README names the CA path, `--scope user` in README and the token card); every other parked minor stays "leave" as triaged (final-review.md) — cost if wrong: none expected.
Final fix wave: done (commits ca71e66..bb5b198, 7 commits; 922 passed + 1 skipped, 99 %). Items 3/4 pinned behaviour that was already right (mutation-checked). Caddy rewrite checked by text only; Task 5 runs `caddy validate` + two `--path-as-is` curls.
- Ruling: the plan's Task 4 text keeps the old template/command/log snippet; code and README deliberately differ after the final wave — cost if wrong: a reader copying from Task 4 gets the older lines.
Final re-review (opus): all items ADDRESSED, no new Critical/Important (Caddy semantics reasoned from docs; Task 5's `caddy validate` + `--path-as-is` curls confirm live).
Final: minor (deferred): the Caddy test pins where the rewrite is written, not when it runs (Caddy orders directives itself; same result); /MCP, /mcpx, /mcp/x, //mcp now reach the gate (401) instead of 404 (intended); the recap tool logs "1 items" when no recap exists.
Final: out-of-scope finding (P2b, pre-existing): uvicorn's access log records query strings, and the web Search page uses GET /search?q=… — web search questions land in the api log (percent-encoded). /mcp is not affected (questions in the body).
- Ruling: report the access-log finding to the owner with a proposed follow-up (access log off or query strings stripped, test-first) instead of folding it into P3a — it is P2b code outside this plan — cost if wrong: questions keep landing in the api log until the owner decides.
Tasks 1–4: complete; final review clean after one fix wave. Brain 922 passed + 1 skipped, 99 %. Task 5 (deploy + Claude Code) waits for the owner's yes.
Owner 2026-10-17 (AskUserQuestion): Task 5 → "Đồng ý, làm hết" (deploy, register Claude Code, call the five tools); access-log finding → "Bỏ query khỏi log" (strip query strings from uvicorn's access log, test-first, reviewed, deployed with P3a).
Follow-up (access log): dispatched (BASE 6cbfa2e), brief followup-brief.md.
Follow-up (access log): implementer DONE 5f2c45a (942 passed + 1 skipped, 99 %); StripQuery via log_config on uvicorn's access handler; e2e test with a real uvicorn.Server.
Follow-up (access log): review ✅ spec, changes needed — I1 the e2e test's httpx client trusts HTTP_PROXY (fails behind a proxy) → trust_env=False.
- Ruling: park the WebSocket-handshake line on uvicorn.error that still carries a query (the brain has no WebSocket route; only a deliberate ws:// request could log one) — cost if wrong: a crafted ws request's query lands in the log.
Follow-up (access log): fix round 1/5 dispatched (resume implementer)
Follow-up (access log): fix round 1 done 146f716 (passes with and without HTTP_PROXY; 942 + 1 skipped).
Follow-up (access log): fix round 1/5 (1 addressed, 0 open; commits 5f2c45a..146f716). Follow-up minors (deferred): WebSocket handshake line on uvicorn.error keeps the query (no ws route); logs.py docstring nit; the serve test uses a fake uvicorn.run; no timeout on awaiting the e2e server.
Follow-up (access log): complete (commits 6cbfa2e..146f716, review clean after 1 fix round)
Task 5 Step 1-3 (16:45 KST): preconditions ok (both worktrees clean, ff possible); owner yes = "Đồng ý, làm hết"; pg_dump → /home/soul/loopi-backups/p2c-2026-10-17-1645.dump; ROLLBACK=29307515ce311e52974c83ab44c2847b1bbd674f; feat/phase2 fast-forwarded to feat/p3a.
Task 5 Step 3 (16:47 KST): `up -d --build` → api/worker healthy; `caddy validate` → Valid configuration; proxy restarted; smoke.sh PASSED (device 01M54D9WMVVJZ8HWX0FGRWPBQ2 revoked); migration 006_mcp.sql applied.
Task 5 Step 4: via Caddy POST /mcp, /mcp/ and (--path-as-is) /commitments/../mcp → 401 `WWW-Authenticate: Bearer` {"error": "unauthorized"} (the gate, never a web page); /settings → 401 (basic auth); inside: /settings 200 with the MCP card.
Task 5 Step 5: token "Claude Code (brain PC)" made with the CLI into ~/.config/loopi/mcp-token (600, never printed); `claude mcp add --scope user --transport http loopi https://localhost:8443/mcp` (header redacted by Claude Code); `claude mcp list` → loopi ✔ Connected (NODE_EXTRA_CA_CERTS=Loopi CA). `claude -p` called the five tools: search_memories ok (8), get_timeline ok (19), get_day_recap ok (exists), get_moment ok (1), list_commitments ok (6). No content recorded.
Task 5 Step 6: api 99 MiB, worker 104 MiB, models 3.0 GiB; 0 api log lines with the acceptance question; mcp lines = tool + time + count only; a /search?q=PRIVACYPROBE request logs "GET /search" (query stripped); 0 failed jobs; audio→transcript p95 0.2 min (n=180, 3 h); video: 168 segments analysed, 0 new keyframes (static room); 10 GB available, swap 1.4 GB.
Task 5: complete. P3a plan complete.
