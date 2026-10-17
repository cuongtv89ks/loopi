# P3a — Read-only MCP server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude Code (and any MCP client) can call five read-only tools on the owner's memories: `search_memories`, `get_timeline`, `get_day_recap`, `get_moment`, `list_commitments`. They are served at `https://<brain>:8443/mcp` behind a bearer token made on Cài đặt.

**Architecture:**
- The official MCP SDK's `MCPServer` (package `mcp` 2.3.0) runs Streamable HTTP in stateless JSON mode.
- It is served at exactly `/mcp` by a Starlette `Route` inside the existing api process. Its session manager runs inside the FastAPI lifespan.
- A pure-ASGI bearer gate answers 401 before any MCP code runs. It checks the token's SHA-256 against a new `mcp_token` table.
- Each tool is a thin wrapper around a plain async function in `loopi_brain/mcp/memory.py`, which reuses P2b's search and tables.
- Caddy routes `/mcp*` to the api without basic auth.

**Tech Stack:** Python 3.13, FastAPI/Starlette, psycopg 3 + pool, pydantic 2, `mcp==2.3.0` (it brings `httpx2`), pytest + pytest-asyncio + testcontainers, Caddy 2.10.

**Spec:**
- `docs/superpowers/specs/2026-10-17-p3-mcp-vlog-design.md` §1–§3: P3 design, approved by the owner on 2026-10-17.
- The binding spec `docs/superpowers/specs/2026-10-14-loopi-design.md`: §9.3, §10.5, §11, §14 (P3 row), D21, D24.

## Global Constraints

- The server is **read only**: no tool writes anything, apart from the token's `last_used_at` bookkeeping.
- **D24:** `get_moment` attaches a keyframe picture only when the Cài đặt switch "Cho MCP gửi ảnh keyframe" is on (default **off**). The picture is the 320 px thumbnail. A keyframe that is a golden-set sample (`eval_caption`, D21) is never sent.
- Tokens:
  - `loopi_` + 32 random bytes (base64url), shown once.
  - Only their SHA-256 is stored.
  - Missing, wrong, malformed or revoked → HTTP **401** with the fixed body `{"error": "unauthorized"}`, before any MCP code runs.
  - A token and its hash never reach a log line, an error message or a page after the one that shows it.
- **No user data in logs or errors:** no query, no moment text, no recap or commitment text, no tool result. Tool errors are fixed English texts that never repeat an argument. Argument-validation errors from the SDK, which echo the input, are replaced by `invalid arguments`.
- Times: local (`LOOPI_TZ`) ISO 8601 with offset, `timespec="seconds"`. Days: `YYYY-MM-DD` local.
- Size limits:
  - moment and summary text clipped to **500** characters;
  - recap markdown to **8000**;
  - `search_memories.limit` 1–50, default 10;
  - `list_commitments` at most 200, newest first.
- `mcp` is pinned exactly: `mcp==2.3.0`. Its 2.x API differs from 1.x:
  - the server class is `MCPServer`, not `FastMCP`;
  - `stateless_http` / `json_response` are arguments of `streamable_http_app()`;
  - the client is `streamable_http_client(url, http_client=httpx2.AsyncClient(...))`;
  - result fields are snake_case (`structured_content`, `is_error`, `mime_type`).
- Coverage ≥ 80 % for the brain package (it is 99 % now). TDD: every step that adds behaviour starts with a failing test.
- Conventional commits, each ending with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
  ```
- Never run `docker compose` or `deploy/smoke.sh` from a task (Task 5 is controller-run). Never touch `/home/soul/loopi-p2` (the live deploy dir) or `/home/soul/loopi-data`.

## Review Focus

1. **Claude Code's real handshake** may use the legacy protocol (2025-11-25 `initialize`) or the 2026 `server/discover` probe. Both must list the five tools: the Task 3 test runs the client in `mode="auto"` and `mode="legacy"`.
2. **Loose search bounds**, e.g. `start="2026-10-16"`, `end="2026-10-16"`, a naive `2026-10-16T08:00`, or `end` before `start`. A date alone means the whole local day, so the end day is included. A naive time is local. A reversed range gets a fixed error. Task 2 tests pin each case.
3. **A crash inside a tool** (DB down, a bug) must come back as `internal error`, with no traceback carrying the query in the log. The SDK would log `Tool … raised an unexpected exception` with the exception text. Task 3 tests this with a patched function raising `RuntimeError("SECRET")`.
4. **A browser or curl hitting `/mcp`.** A `GET` with a good token would hold an idle event stream open forever in stateless mode. The gate answers 405 (with a token) or 401 (without one), and nothing hangs. Task 3 tests it.
5. **Validation errors echoing private input**, for example `status="SECRET"`, `limit=1000` or a 3000-character query. The client sees `invalid arguments` or a fixed message, never the value. Task 3 tests it.

## Rulings taken while writing this plan

Copy these into the ledger before Task 1. Each gives the decision, why, and the cost if wrong.

- **R1** When the embedding model cannot be reached, `search_memories` answers word matches with `"semantic": false` instead of the design's "search is not ready" error. The search page and the chat do the same, and the client can say "matched by words only". Cost if wrong: one boolean the client may ignore. Task 4 updates the design doc line.
- **R2** "Each text is clipped at 500 characters" applies to moment texts and chapter summaries. The recap markdown is clipped at 8000, because a recap is 300–1500 characters and clipping it at 500 would cut most of it. Cost if wrong: a larger tool result.
- **R3** The package is `loopi_brain/mcp/`, following the spec's `brain/…/mcp/` layout. Its modules import the SDK with absolute imports (`from mcp.server…`). `loopi_brain/mcp/server.py` does **not** use `from __future__ import annotations`, because the SDK reads the tool annotations when a tool is registered. Cost if wrong: a rename.
- **R4** The tools reach the pool, settings and model client through a closure over a small `Deps` dataclass built in `create_app`, not through the SDK's lifespan context. The research verified both. The closure needs no extra lifespan. Cost if wrong: none.
- **R5** `loopi-brain serve` calls `logging.basicConfig(level=logging.WARNING)` before building the app. `MCPServer()` calls `basicConfig(INFO)` when the root logger has no handler, which would start printing every INFO line of the api. Configuring the root logger first keeps today's level (warnings and errors). The `mcp` logger is set to WARNING when `loopi_brain.mcp.server` is imported. Cost if wrong: the log format changes to `LEVEL:logger:message`.
- **R6** Tool errors are returned as `CallToolResult(is_error=True)` with a fixed text: `no such moment`, `date must be YYYY-MM-DD`, `start must be before end`, `query is empty`, `status must be open, done or dismissed`, `invalid arguments`, `internal error`. `ToolError` would add an `Error executing tool …` prefix. Dates are typed `str` and parsed by the brain, so a bad date never reaches pydantic's echoing validator. Cost if wrong: none.
- **R7** DNS-rebinding protection is turned off (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`). Behind Caddy, every request carries the LAN Host header and would otherwise be refused with 421. The bearer gate decides who may call. Cost if wrong: none for a server that needs a token for every request.
- **R8** The gate answers 405 to anything but POST once the token is good. Stateless JSON needs only POST, and a GET would hold an idle event stream open. Cost if wrong: a client that insists on GET streams; none known.
- **R9** `last_used_at` is written at most once a minute per token. A tool call is one request, and Claude Code makes several a minute. Cost if wrong: "last used" is up to a minute old.
- **R10** `loopi-brain mcp-token create|list|revoke` lets the controller and the owner make a token without the page. Task 5 uses it to register Claude Code without the token ever being printed into the session. Cost if wrong: one more CLI command.
- **R11** No Caddyfile change other than the `/mcp*` block: request body ≤ 64 KB, fingerprint header stripped, no basic auth, no client certificate. TLS remains Caddy's `verify_if_given` policy, so Claude Code needs no client certificate, only `NODE_EXTRA_CA_CERTS` pointing at the Loopi CA. Cost if wrong: none.

---

## File structure

| File | Responsibility |
|---|---|
| `brain/pyproject.toml`, `brain/uv.lock` | add `mcp==2.3.0` |
| `brain/src/loopi_brain/migrations/006_mcp.sql` | the `mcp_token` table |
| `brain/src/loopi_brain/mcp/__init__.py` | package docstring |
| `brain/src/loopi_brain/mcp/tokens.py` | make, check, list and revoke tokens (no SDK import, so the CLI stays light) |
| `brain/src/loopi_brain/mcp/memory.py` | the five read functions and their pydantic output models (no SDK import) |
| `brain/src/loopi_brain/mcp/server.py` | `MCPServer`, the five tool wrappers, the validation-echo middleware, `BearerGate`, `mount()` |
| `brain/src/loopi_brain/store.py` | `McpSettings` (D24 switch) next to `Retention` |
| `brain/src/loopi_brain/thumbs.py` | `keyframe_thumb()` shared by `/thumb/{id}.jpg` and `get_moment` |
| `brain/src/loopi_brain/web/day_routes.py` | the thumbnail route calls `thumbs.keyframe_thumb` |
| `brain/src/loopi_brain/app.py` | mount `/mcp`, run its session manager in the lifespan |
| `brain/src/loopi_brain/cli.py` | `mcp-token` commands; `serve` configures logging first |
| `brain/src/loopi_brain/web/settings_routes.py`, `templates/settings.html`, `templates/_mcp_tokens.html` | the "MCP (Claude Code)" card |
| `deploy/Caddyfile`, `deploy/README.md` | `/mcp*` route; how to connect Claude Code |
| `brain/tests/test_mcp_tokens.py`, `test_mcp_memory.py`, `test_mcp_server.py`, `test_settings_mcp.py` | new tests |
| `brain/tests/conftest.py`, `test_db.py`, `test_deploy.py`, `test_cli.py`, `helpers.py` | extended |

---

### Task 1: The `mcp` dependency, migration 006 and the tokens

**Files:**
- Modify: `brain/pyproject.toml`, `brain/uv.lock`
- Create: `brain/src/loopi_brain/migrations/006_mcp.sql`, `brain/src/loopi_brain/mcp/__init__.py`, `brain/src/loopi_brain/mcp/tokens.py`
- Modify: `brain/tests/conftest.py` (`APP_TABLES`), `brain/tests/test_db.py`
- Test: `brain/tests/test_mcp_tokens.py` (new)

**Interfaces:**
- Consumes: `psycopg_pool.AsyncConnectionPool` with `dict_row` rows (as everywhere in the brain), `ulid.ULID`.
- Produces (`loopi_brain.mcp.tokens`):
  - constants `PREFIX = "loopi_"`, `NAME_MAX = 60`, `TOKEN_MAX = 200`, `TOUCH_EVERY = timedelta(minutes=1)`;
  - `class TokenError(ValueError)`;
  - `digest(token: str) -> str` (SHA-256 hex);
  - `clean_name(name: str) -> str`;
  - `async create_token(pool, name: str) -> tuple[str, str]` → `(token_id, token)`;
  - `async check_token(pool, token: str, now: datetime | None = None) -> str | None`;
  - `async list_tokens(pool) -> list[dict]` (keys `id, name, created_at, last_used_at, revoked_at`);
  - `async revoke_token(pool, token_id: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_mcp_tokens.py`:

```python
import datetime as dt

import pytest

from helpers import fetch_all
from loopi_brain.mcp import tokens

T0 = dt.datetime(2026, 10, 17, 1, 0, tzinfo=dt.UTC)


async def test_a_new_token_is_shown_once_and_stored_only_as_its_hash(pool):
    token_id, token = await tokens.create_token(pool, "  Claude   Code ")
    assert token.startswith("loopi_") and len(token) >= 40
    [row] = await fetch_all(pool, "SELECT * FROM mcp_token")
    assert row["id"] == token_id and row["name"] == "Claude Code"
    assert row["token_sha256"] == tokens.digest(token) and token not in str(row)
    assert row["last_used_at"] is None and row["revoked_at"] is None
    assert await tokens.check_token(pool, token) == token_id


async def test_wrong_malformed_and_revoked_tokens_are_refused(pool):
    token_id, token = await tokens.create_token(pool, "a")
    for bad in ("", "loopi_", token + "x", token[len("loopi_"):], "loopi_" + "a" * 300, "Bearer " + token):
        assert await tokens.check_token(pool, bad) is None
    assert await tokens.revoke_token(pool, token_id) is True
    assert await tokens.check_token(pool, token) is None
    assert await tokens.revoke_token(pool, token_id) is False  # already revoked
    assert await tokens.revoke_token(pool, "01NOSUCHTOKEN0000000000000") is False


async def test_last_use_is_written_at_most_once_a_minute(pool):
    _, token = await tokens.create_token(pool, "a")
    await tokens.check_token(pool, token, now=T0)
    await tokens.check_token(pool, token, now=T0 + dt.timedelta(seconds=30))
    [row] = await fetch_all(pool, "SELECT last_used_at FROM mcp_token")
    assert row["last_used_at"] == T0
    await tokens.check_token(pool, token, now=T0 + dt.timedelta(seconds=61))
    [row] = await fetch_all(pool, "SELECT last_used_at FROM mcp_token")
    assert row["last_used_at"] == T0 + dt.timedelta(seconds=61)


@pytest.mark.parametrize("name", ["", "   ", "x" * 61])
async def test_a_name_must_be_short_and_not_empty(pool, name):
    with pytest.raises(tokens.TokenError) as exc:
        await tokens.create_token(pool, name)
    assert "x" * 61 not in str(exc.value)  # the message never repeats the name
    assert await fetch_all(pool, "SELECT * FROM mcp_token") == []


async def test_the_list_is_newest_first_and_never_shows_a_hash(pool):
    first, _ = await tokens.create_token(pool, "first")
    second, _ = await tokens.create_token(pool, "second")
    await tokens.revoke_token(pool, first)
    listed = await tokens.list_tokens(pool)
    assert [t["id"] for t in listed] == [second, first]
    assert listed[0]["revoked_at"] is None and listed[1]["revoked_at"] is not None
    assert all(set(t) == {"id", "name", "created_at", "last_used_at", "revoked_at"} for t in listed)
```

In `brain/tests/test_db.py`:
- add `"mcp_token"` to `EXPECTED_TABLES`;
- add `"006_mcp.sql"` to the `applied` set in `test_migrate_is_idempotent_and_creates_schema`;
- add `"006_mcp.sql"` after `"005_evaluation.sql"` in the two migration lists (the upgrade test's `assert migrate(url) == [...]` and `test_concurrent_migrations_apply_each_step_once`).

In `brain/tests/conftest.py`, add `mcp_token, ` at the start of the `APP_TABLES` string so it reads:

```python
APP_TABLES = (
    "mcp_token, eval_run_audio, eval_run, eval_answer, eval_question, eval_caption_score, eval_caption, eval_audio,"
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `cd brain && uv run pytest -q tests/test_mcp_tokens.py tests/test_db.py`
Expected: collection error `ModuleNotFoundError: No module named 'loopi_brain.mcp'`, or `relation "mcp_token" does not exist`.

- [ ] **Step 3: Add the dependency**

In `brain/pyproject.toml`, append to `dependencies` after `"pyyaml>=6.0",`:

```toml
    # The official MCP SDK (spec §10.5). 2.x: MCPServer (no FastMCP) and its own httpx2 client; pinned exactly because
    # this plan was verified against 2.3.0 and parts of its server API (middleware) are marked provisional.
    "mcp==2.3.0",
```

Run: `cd brain && uv lock && uv sync`
Then: `git diff uv.lock | grep -E '^-version' | wc -l`
Expected: `0`. No existing package changes version. The new packages are `mcp`, `mcp-types`, `httpx2`, `httpcore2`, `truststore`, `pyjwt` and `sse-starlette`. `jsonschema` becomes a runtime dependency at its locked version.

- [ ] **Step 4: Write the migration**

Create `brain/src/loopi_brain/migrations/006_mcp.sql`:

```sql
-- P3a: bearer tokens of the read-only MCP server (spec §10.5, P3 design §3.1). Only a token's SHA-256 is stored; the
-- token itself is shown once, when it is made (Cài đặt or `loopi-brain mcp-token create`). A revoked token keeps
-- its row (the page lists it as "đã thu hồi").
CREATE TABLE mcp_token (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 60),
    token_sha256  TEXT NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
```

- [ ] **Step 5: Write the tokens module**

Create `brain/src/loopi_brain/mcp/__init__.py`:

```python
"""The read-only MCP server (spec §10.5, P3 design §3): tokens, the data the tools read, and the server itself."""
```

Create `brain/src/loopi_brain/mcp/tokens.py`:

```python
"""Bearer tokens of the read-only MCP server (spec §10.5, P3 design §3.1): made on Cài đặt or with
``loopi-brain mcp-token create``, shown once, stored only as SHA-256, revocable. A token never reaches a log line,
an error message or the database in clear. This module does not import the MCP SDK (the CLI uses it)."""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from psycopg_pool import AsyncConnectionPool
from ulid import ULID

PREFIX = "loopi_"
NAME_MAX = 60
TOKEN_MAX = 200  # longer bearer strings are refused before they are hashed
TOUCH_EVERY = dt.timedelta(minutes=1)  # last_used_at is written at most once a minute per token


class TokenError(ValueError):
    """A token name that cannot be used; the message is fixed text (it never repeats the name)."""


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_name(name: str) -> str:
    """``name`` with its whitespace collapsed; TokenError when that is empty or longer than NAME_MAX."""
    cleaned = " ".join(name.split())
    if not cleaned:
        raise TokenError("the name is empty")
    if len(cleaned) > NAME_MAX:
        raise TokenError(f"the name is longer than {NAME_MAX} characters")
    return cleaned


async def create_token(pool: AsyncConnectionPool, name: str) -> tuple[str, str]:
    """A new live token called ``name``: (its id, the token). The token itself is not stored anywhere."""
    cleaned = clean_name(name)
    token = PREFIX + secrets.token_urlsafe(32)
    token_id = str(ULID())
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO mcp_token (id, name, token_sha256) VALUES (%s, %s, %s)", (token_id, cleaned, digest(token))
        )
    return token_id, token


async def check_token(pool: AsyncConnectionPool, token: str, now: dt.datetime | None = None) -> str | None:
    """The id of the live token ``token``, else None (malformed, unknown or revoked). The lookup is by the token's
    SHA-256, so its timing tells nothing about the token. Marks the token used, at most once a minute."""
    if not token.startswith(PREFIX) or len(token) > TOKEN_MAX:
        return None
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, last_used_at FROM mcp_token WHERE token_sha256 = %s AND revoked_at IS NULL", (digest(token),)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        now = now or dt.datetime.now(dt.UTC)
        if row["last_used_at"] is None or now - row["last_used_at"] >= TOUCH_EVERY:
            await conn.execute("UPDATE mcp_token SET last_used_at = %s WHERE id = %s", (now, row["id"]))
    return row["id"]


async def list_tokens(pool: AsyncConnectionPool) -> list[dict]:
    """Every token, newest first: id, name, created_at, last_used_at, revoked_at (never its hash)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, name, created_at, last_used_at, revoked_at FROM mcp_token ORDER BY created_at DESC, id DESC"
        )
        return await cur.fetchall()


async def revoke_token(pool: AsyncConnectionPool, token_id: str) -> bool:
    """Revoke a live token; False when there is no such token or it was revoked already."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE mcp_token SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (token_id,)
        )
        return cur.rowcount == 1
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `cd brain && uv run pytest -q tests/test_mcp_tokens.py tests/test_db.py`
Expected: all pass.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: all pass (the suite was 839 passed + 1 skipped before this task), coverage ≥ 99 %.

- [ ] **Step 7: Commit**

```bash
git add brain/pyproject.toml brain/uv.lock brain/src/loopi_brain/migrations/006_mcp.sql brain/src/loopi_brain/mcp \
        brain/tests/test_mcp_tokens.py brain/tests/test_db.py brain/tests/conftest.py
git commit -F - <<'EOF'
feat(brain): MCP bearer tokens (made once, stored as SHA-256, revocable) and the mcp SDK

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 2: What the tools read: `memory.py`, the D24 switch and the shared thumbnail

**Files:**
- Create: `brain/src/loopi_brain/mcp/memory.py`
- Modify: `brain/src/loopi_brain/store.py`, `brain/src/loopi_brain/thumbs.py`, `brain/src/loopi_brain/web/day_routes.py` (the `/thumb/{keyframe_id}.jpg` route), `brain/tests/helpers.py` (`insert_moment` gains `keyframe_id`)
- Test: `brain/tests/test_mcp_memory.py` (new)

**Interfaces:**
- Consumes:
  - `search.search_moments(pool, router, query, *, start, end, limit, max_distance, min_similarity) -> SearchResult(hits: list[Hit], semantic: bool)`;
  - `search.clean_query`;
  - `days.day_bounds_utc(day, zone) -> (start_utc, end_utc)`;
  - `Settings.zone`, `Settings.media_root`, `Settings.search_max_distance`, `Settings.search_min_similarity`;
  - `media.inside(root, rel) -> Path` (ValueError outside the root);
  - `pipeline.state.keyframe_exists(pool, keyframe_id)`;
  - `thumbs.thumb_rel`, `thumbs.make_thumb`.
- Produces:
  - `store`:
    - `MCP_KEY = "mcp"`;
    - `class McpSettings(BaseModel)` with `images: bool = False`;
    - `async get_mcp(pool) -> McpSettings`;
    - `async set_mcp(pool, value: McpSettings) -> None`.
  - `thumbs`:
    - `class ThumbUnavailable(Exception)`, where `str(exc)` is one of `unknown keyframe`, `keyframe file missing`, `keyframe unreadable`;
    - `async keyframe_thumb(pool, media_root: Path, keyframe_id: str) -> Path`.
  - `loopi_brain.mcp.memory`:
    - constants `TEXT_MAX = 500`, `MARKDOWN_MAX = 8000`, `SEARCH_LIMIT_MAX = 50`, `COMMITMENTS_MAX = 200`, `KINDS`, `STATUSES`;
    - `class NotFound(Exception)`, `class BadInput(Exception)`;
    - models `MomentRef`, `SearchOut`, `ChapterOut`, `MarkerOut`, `TimelineOut`, `RecapOut`, `CaptionOut`, `MomentOut`, `CommitmentOut`, `CommitmentsOut`;
    - `iso(ts, zone) -> str`, `parse_day(value) -> date`, `parse_when(value, zone, *, end) -> datetime | None`;
    - `async search_memories(pool, router, settings, query, start, end, limit) -> SearchOut`;
    - `async get_timeline(pool, day, zone) -> TimelineOut`;
    - `async get_day_recap(pool, day, zone) -> RecapOut`;
    - `async get_moment(pool, settings, moment_id, images: bool) -> tuple[MomentOut, bytes | None]`;
    - `async list_commitments(pool, status: str | None) -> CommitmentsOut`.
  - `helpers.insert_moment(..., keyframe_id: str | None = None)`.

- [ ] **Step 1: Let the test helper link a moment to its keyframe**

In `brain/tests/helpers.py`, make three changes to `insert_moment`:
- add the parameter `keyframe_id: str | None = None` after `seconds: float = 0`;
- add `keyframe_id` to the INSERT column list right after `item_id`;
- add `keyframe_id` to the values tuple in the same position.

The resulting INSERT:

```python
        await conn.execute(
            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, lang, item_id, keyframe_id, embedding,"
            " embed_model, model_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'test')",
            (moment_id, device_id, ts, ts + dt.timedelta(seconds=seconds), kind, text, lang, item_id, keyframe_id,
             vector, None if vector is None else "test"),
        )
```

Run: `cd brain && uv run pytest -q`
Expected: all pass. The new parameter defaults to None, so no caller changes.

- [ ] **Step 2: Write the failing tests**

Create `brain/tests/test_mcp_memory.py`:

```python
import datetime as dt
import io

import pytest
from PIL import Image

from helpers import FakeOllama, captioned_keyframe, insert_chapter, insert_moment, paired_device
from loopi_brain import search, store, thumbs
from loopi_brain.mcp import memory
from loopi_brain.providers.config import DEFAULT_CONFIG
from loopi_brain.providers.router import build_router

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
DAY = "2026-10-14"


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def router(pki_settings):
    return build_router(DEFAULT_CONFIG, pki_settings, FakeOllama().client())


async def test_search_answers_moments_with_local_times_their_day_and_end(pool, pki_settings, dev, router):
    moment = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, seconds=4, axis=0, lang="vi",
                                 text="Mai mình đi chợ")
    found = await memory.search_memories(pool, router, pki_settings, "đi chợ", None, None, 10)
    assert found.not_found is False and found.semantic is True
    [ref] = found.moments
    assert (ref.moment_id, ref.kind, ref.lang, ref.day) == (moment, "speech", "vi", DAY)
    assert (ref.ts_start, ref.ts_end) == ("2026-10-14T09:00:00+09:00", "2026-10-14T09:00:04+09:00")
    assert ref.text == "Mai mình đi chợ"


async def test_search_without_the_embedding_model_matches_words(pool, pki_settings, dev):
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="Mai mình đi chợ")
    found = await memory.search_memories(pool, None, pki_settings, "đi chợ", None, None, 10)
    assert found.semantic is False and len(found.moments) == 1


async def test_search_finds_nothing_and_says_so(pool, pki_settings, dev, router):
    found = await memory.search_memories(pool, router, pki_settings, "không có gì", None, None, 10)
    assert (found.moments, found.not_found) == ([], True)


async def test_a_date_alone_is_the_whole_local_day_and_a_naive_time_is_local(pool, pki_settings, dev, router):
    zone = pki_settings.zone
    assert memory.parse_when("2026-10-14", zone, end=False) == dt.datetime(2026, 10, 13, 15, 0, tzinfo=dt.UTC)
    assert memory.parse_when("2026-10-14", zone, end=True) == dt.datetime(2026, 10, 14, 15, 0, tzinfo=dt.UTC)
    assert memory.parse_when("2026-10-14T08:00", zone, end=False) == dt.datetime(2026, 10, 13, 23, 0, tzinfo=dt.UTC)
    assert memory.parse_when("2026-10-14T08:00:00+00:00", zone, end=True) == dt.datetime(2026, 10, 14, 8, 0, tzinfo=dt.UTC)
    assert memory.parse_when(None, zone, end=True) is None and memory.parse_when("  ", zone, end=True) is None
    inside = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(hours=14), text="đi chợ")
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(days=1), text="đi chợ")
    found = await memory.search_memories(pool, None, pki_settings, "đi chợ", DAY, DAY, 10)  # 23:00 on the 14th counts
    assert [m.moment_id for m in found.moments] == [inside]


@pytest.mark.parametrize(("start", "end", "message"), [
    ("2026-10-15", "2026-10-14", "start must be before end"),
    ("yesterday-SECRET", None, "start and end must be YYYY-MM-DD or ISO 8601 date-times"),
    ("2026-13-45", None, "date must be YYYY-MM-DD"),
])
async def test_unusable_bounds_are_fixed_errors(pool, pki_settings, start, end, message):
    with pytest.raises(memory.BadInput) as exc:
        await memory.search_memories(pool, None, pki_settings, "x", start, end, 10)
    assert str(exc.value) == message


async def test_an_empty_query_is_a_fixed_error(pool, pki_settings):
    with pytest.raises(memory.BadInput, match="^query is empty$"):
        await memory.search_memories(pool, None, pki_settings, " \x00 ", None, None, 10)


async def test_search_keeps_the_limit_between_1_and_50(pool, pki_settings, monkeypatch):
    asked = []

    async def fake_search(_pool, _router, _query, **kwargs):
        asked.append(kwargs["limit"])
        return search.SearchResult([], semantic=False)

    monkeypatch.setattr(memory.search, "search_moments", fake_search)
    for limit in (500, 0, 7):
        await memory.search_memories(pool, None, pki_settings, "x", None, None, limit)
    assert asked == [50, 1, 7]


async def test_the_timeline_has_chapters_markers_and_counts(pool, pki_settings, dev):
    zone = pki_settings.zone
    day = dt.date(2026, 10, 14)
    chapter = await insert_chapter(pool, day, NINE, NINE + dt.timedelta(minutes=30), title="Buổi sáng", summary="S" * 600)
    marker = await insert_moment(pool, dev["device_id"], kind="marker", ts=NINE + dt.timedelta(minutes=5), text="Đánh dấu")
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(minutes=6), text="x")
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(days=1), text="other day")
    timeline = await memory.get_timeline(pool, DAY, zone)
    assert timeline.day == DAY and timeline.counts == {"visual": 0, "speech": 1, "marker": 1}
    [c] = timeline.chapters
    assert (c.id, c.title, c.closed, c.ts_start) == (chapter, "Buổi sáng", True, "2026-10-14T09:00:00+09:00")
    assert len(c.summary) == memory.TEXT_MAX
    assert [(m.moment_id, m.text) for m in timeline.markers] == [(marker, "Đánh dấu")]
    empty = await memory.get_timeline(pool, "2026-10-20", zone)
    assert (empty.chapters, empty.markers, empty.counts) == ([], [], {"visual": 0, "speech": 0, "marker": 0})


@pytest.mark.parametrize("bad", ["2026-13-45", "tomorrow", "2026-1-5", ""])
async def test_a_day_must_be_yyyy_mm_dd(pool, pki_settings, bad):
    with pytest.raises(memory.BadInput, match="^date must be YYYY-MM-DD$"):
        await memory.get_timeline(pool, bad, pki_settings.zone)


async def test_the_recap_or_that_there_is_none(pool, pki_settings):
    zone = pki_settings.zone
    missing = await memory.get_day_recap(pool, DAY, zone)
    assert (missing.exists, missing.markdown, missing.generated_at, missing.stale) == (False, None, None, False)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO day_recap (day, markdown, model_version, generated_at, stale) VALUES (%s, %s, 'm', %s, true)",
            (dt.date(2026, 10, 14), "# Ngày\n" + "x" * 9000, NINE),
        )
    recap = await memory.get_day_recap(pool, DAY, zone)
    assert recap.exists and recap.stale and recap.generated_at == "2026-10-14T09:00:00+09:00"
    assert recap.markdown.startswith("# Ngày") and len(recap.markdown) == memory.MARKDOWN_MAX


async def test_a_spoken_moment_comes_back_with_its_text_clipped(pool, pki_settings, dev):
    moment = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, seconds=3, lang="ko", text="말" * 700)
    out, picture = await memory.get_moment(pool, pki_settings, moment.lower(), images=True)
    assert (out.moment_id, out.kind, out.lang, out.caption, out.image, picture) == (moment, "speech", "ko", None, False, None)
    assert len(out.text) == memory.TEXT_MAX and out.ts_end == "2026-10-14T09:00:03+09:00"


async def test_a_visual_moment_sends_its_picture_only_when_allowed(pool, pki_settings, dev):
    caption = {"scene": "kitchen", "activity": "cooking", "people_count": 2, "objects": ["pot", 3, "spoon"],
               "text_seen": "", "notable": False}
    keyframe = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE, caption=caption)
    moment = await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, text="Two people cook",
                                 keyframe_id=keyframe)
    out, picture = await memory.get_moment(pool, pki_settings, moment, images=False)
    assert (out.image, picture) == (False, None)
    assert out.caption == memory.CaptionOut(scene="kitchen", activity="cooking", objects=["pot", "spoon"], people_count=2)
    out, picture = await memory.get_moment(pool, pki_settings, moment, images=True)
    assert out.image is True
    with Image.open(io.BytesIO(picture)) as img:
        assert (img.format, img.width) == ("JPEG", thumbs.THUMB_WIDTH)


async def test_a_golden_keyframe_is_never_sent(pool, pki_settings, dev):
    keyframe = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    moment = await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, text="x", keyframe_id=keyframe)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO eval_caption (id, keyframe_id, path) VALUES ('S1', %s, 'keyframes/S1.jpg')",
                           (keyframe,))
    out, picture = await memory.get_moment(pool, pki_settings, moment, images=True)
    assert (out.image, picture) == (False, None)


async def test_a_missing_keyframe_file_only_drops_the_picture(pool, pki_settings, dev):
    keyframe = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    moment = await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, text="x", keyframe_id=keyframe)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT path FROM keyframe WHERE id = %s", (keyframe,))
        (pki_settings.media_root / (await cur.fetchone())["path"]).unlink()
    out, picture = await memory.get_moment(pool, pki_settings, moment, images=True)
    assert (out.image, picture, out.text) == (False, None, "x")


@pytest.mark.parametrize("bad", ["01NOSUCHMOMENT000000000000", "not-an-id", "", "x" * 3000])
async def test_an_unknown_moment_is_a_fixed_error(pool, pki_settings, bad):
    with pytest.raises(memory.NotFound, match="^no such moment$"):
        await memory.get_moment(pool, pki_settings, bad, images=False)


async def test_commitments_newest_first_and_filtered(pool):
    async with pool.connection() as conn:
        for n, (day, status) in enumerate([(14, "open"), (15, "done"), (16, "open")]):
            await conn.execute(
                "INSERT INTO commitment (id, day, text, status, model_version) VALUES (%s, %s, %s, %s, 'm')",
                (f"C{n}", dt.date(2026, 10, day), f"việc {n}", status),
            )
    every = await memory.list_commitments(pool, None)
    assert [c.id for c in every.commitments] == ["C2", "C1", "C0"]
    assert every.commitments[0].day == "2026-10-16" and every.commitments[0].source_moment_id is None
    assert [c.id for c in (await memory.list_commitments(pool, "open")).commitments] == ["C2", "C0"]
    with pytest.raises(memory.BadInput, match="^status must be open, done or dismissed$"):
        await memory.list_commitments(pool, "SECRET")


async def test_commitments_stop_at_200(pool):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO commitment (id, day, text, status, model_version)"
            " SELECT 'C' || n, DATE '2026-10-14', 'x', 'open', 'm' FROM generate_series(1, 205) AS n"
        )
    assert len((await memory.list_commitments(pool, None)).commitments) == memory.COMMITMENTS_MAX


async def test_the_picture_switch_is_off_until_turned_on(pool):
    assert (await store.get_mcp(pool)).images is False
    await store.set_mcp(pool, store.McpSettings(images=True))
    assert (await store.get_mcp(pool)).images is True
    assert (await store.get_retention(pool)).enabled is False  # the retention setting is a separate row


async def test_an_unknown_keyframe_has_no_thumbnail(pool, pki_settings):
    with pytest.raises(thumbs.ThumbUnavailable, match="^unknown keyframe$"):
        await thumbs.keyframe_thumb(pool, pki_settings.media_root, "01NOSUCHKEYFRAME0000000000")
```

- [ ] **Step 3: Run the tests to see them fail**

Run: `cd brain && uv run pytest -q tests/test_mcp_memory.py`
Expected: collection error `ImportError: cannot import name 'memory' from 'loopi_brain.mcp'`.

- [ ] **Step 4: The D24 switch in `store.py`**

In `brain/src/loopi_brain/store.py`:
- replace the bodies of `get_retention` / `set_retention` with calls to two private helpers;
- add the MCP switch.

The module after the `Retention` class reads:

```python
MCP_KEY = "mcp"


class McpSettings(BaseModel):
    """D24: whether ``get_moment`` may attach a keyframe picture (320 px) for the MCP client, which forwards tool
    results to a model in the cloud. Off by default; a golden-set keyframe (D21) is never sent either way."""

    model_config = ConfigDict(extra="forbid")

    images: bool = False


async def _get(pool: AsyncConnectionPool, key: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT value FROM setting WHERE key = %s", (key,))
        row = await cur.fetchone()
    return row["value"] if row else None


async def _put(pool: AsyncConnectionPool, key: str, value: dict) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO setting (key, value) VALUES (%s, %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, Jsonb(value)),
        )


async def get_retention(pool: AsyncConnectionPool) -> Retention:
    value = await _get(pool, RETENTION_KEY)
    return Retention.model_validate(value) if value is not None else Retention()


async def set_retention(pool: AsyncConnectionPool, retention: Retention) -> None:
    await _put(pool, RETENTION_KEY, retention.model_dump())


async def get_mcp(pool: AsyncConnectionPool) -> McpSettings:
    value = await _get(pool, MCP_KEY)
    return McpSettings.model_validate(value) if value is not None else McpSettings()


async def set_mcp(pool: AsyncConnectionPool, value: McpSettings) -> None:
    await _put(pool, MCP_KEY, value.model_dump())
```

- [ ] **Step 5: One thumbnail function for the Day page and the MCP**

In `brain/src/loopi_brain/thumbs.py`, add these imports and the new names. Keep `thumb_rel` and `make_thumb` unchanged.

```python
import logging

import anyio
from psycopg_pool import AsyncConnectionPool

from . import media
from .pipeline.state import keyframe_exists

log = logging.getLogger(__name__)


class ThumbUnavailable(Exception):
    """No thumbnail for a keyframe; ``str(exc)`` is a fixed reason: unknown keyframe, keyframe file missing,
    keyframe unreadable."""


async def keyframe_thumb(pool: AsyncConnectionPool, media_root: Path, keyframe_id: str) -> Path:
    """The path of ``keyframe_id``'s thumbnail, made now if it is not there yet (the Day page's ``/thumb`` route and
    the MCP ``get_moment`` share this); ThumbUnavailable when there is nothing to show."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT path FROM keyframe WHERE id = %s", (keyframe_id,))
        row = await cur.fetchone()
    if row is None:
        raise ThumbUnavailable("unknown keyframe")
    try:
        rel = PurePosixPath(row["path"])
        src = media.inside(media_root, rel)
        dst = media.inside(media_root, thumb_rel(rel))
    except ValueError as exc:
        log.warning("keyframe %s has an unusable path: %s", keyframe_id, exc)
        raise ThumbUnavailable("keyframe file missing") from exc
    if not src.is_file():
        raise ThumbUnavailable("keyframe file missing")
    if not dst.is_file():
        try:
            await anyio.to_thread.run_sync(make_thumb, src, dst)
        except OSError as exc:  # not a readable picture (PIL.UnidentifiedImageError is one), or the disk refused it
            log.warning("keyframe %s: no thumbnail: %s", keyframe_id, exc)
            raise ThumbUnavailable("keyframe unreadable") from exc
        if not await keyframe_exists(pool, keyframe_id):  # forgotten while the thumbnail was made
            media.remove_files(media_root, [str(thumb_rel(rel))])
            raise ThumbUnavailable("unknown keyframe")
    return dst
```

In `brain/src/loopi_brain/web/day_routes.py`, replace the body of `thumbnail` with:

```python
@router.get("/thumb/{keyframe_id}.jpg")
async def thumbnail(request: Request, keyframe_id: str):
    """A keyframe 320 px wide, made on the first request and kept under ``thumbs/``."""
    state = request.app.state
    try:
        dst = await thumbs.keyframe_thumb(state.pool, state.settings.media_root, keyframe_id)
    except thumbs.ThumbUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(dst, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
```

Remove the imports in `day_routes.py` that become unused: `anyio`, `PurePosixPath`, `keyframe_exists`, and `media` if nothing else in the file uses it. Use `grep -n "media\.\|anyio\.\|PurePosixPath\|keyframe_exists" brain/src/loopi_brain/web/day_routes.py` to check each one.

- [ ] **Step 6: The read functions**

Create `brain/src/loopi_brain/mcp/memory.py`:

```python
"""What the MCP tools read (spec §10.5, P3 design §3.2): plain async functions over the database, one per tool,
returning pydantic models (their JSON schema becomes each tool's output schema). Times are local (``LOOPI_TZ``)
ISO 8601 with offset, days ``YYYY-MM-DD``; moment texts and summaries are clipped to TEXT_MAX characters, the recap
to MARKDOWN_MAX. Errors are NotFound or BadInput, whose messages are fixed English text that never repeats what the
caller sent. This module does not import the MCP SDK."""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

import anyio
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from .. import search, thumbs
from ..config import Settings
from ..days import day_bounds_utc
from ..providers.router import Router

TEXT_MAX = 500
MARKDOWN_MAX = 8000
SEARCH_LIMIT_MAX = 50
COMMITMENTS_MAX = 200
OBJECTS_MAX = 20  # caption objects listed for one moment
KINDS = ("visual", "speech", "marker")
STATUSES = ("open", "done", "dismissed")
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class NotFound(Exception):
    """Nothing with that id; the message is fixed text."""


class BadInput(Exception):
    """An argument the tool cannot use; the message is fixed text."""


class MomentRef(BaseModel):
    moment_id: str
    kind: str
    ts_start: str
    ts_end: str
    day: str
    text: str
    lang: str | None


class SearchOut(BaseModel):
    moments: list[MomentRef]
    not_found: bool
    semantic: bool  # False: the embedding model could not be reached, so moments were matched by their words only


class ChapterOut(BaseModel):
    id: str
    ts_start: str
    ts_end: str
    title: str | None
    summary: str | None
    closed: bool


class MarkerOut(BaseModel):
    moment_id: str
    ts_start: str
    text: str


class TimelineOut(BaseModel):
    day: str
    chapters: list[ChapterOut]
    markers: list[MarkerOut]
    counts: dict[str, int]  # the day's moments per kind: visual, speech, marker


class RecapOut(BaseModel):
    day: str
    exists: bool
    markdown: str | None
    generated_at: str | None
    stale: bool


class CaptionOut(BaseModel):
    scene: str | None
    activity: str | None
    objects: list[str]
    people_count: int | None


class MomentOut(BaseModel):
    moment_id: str
    kind: str
    ts_start: str
    ts_end: str
    day: str
    text: str
    lang: str | None
    caption: CaptionOut | None
    image: bool  # whether a 320 px keyframe picture is attached to this result (D24)


class CommitmentOut(BaseModel):
    id: str
    day: str
    text: str
    status: str
    source_moment_id: str | None


class CommitmentsOut(BaseModel):
    commitments: list[CommitmentOut]


def iso(ts: dt.datetime, zone: ZoneInfo) -> str:
    return ts.astimezone(zone).isoformat(timespec="seconds")


def _clip(text: str | None, limit: int = TEXT_MAX) -> str | None:
    return None if text is None else text[:limit]


def parse_day(value: str) -> dt.date:
    """``YYYY-MM-DD`` -> a date; BadInput otherwise."""
    text = value.strip() if isinstance(value, str) else ""
    if not _DAY.fullmatch(text):
        raise BadInput("date must be YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise BadInput("date must be YYYY-MM-DD") from None


def parse_when(value: str | None, zone: ZoneInfo, *, end: bool) -> dt.datetime | None:
    """A search bound. ``YYYY-MM-DD``: a start bound is that day's local midnight, an end bound the next one (the end
    day is included). Otherwise an ISO 8601 date-time; one without an offset is local. None or blank stays None."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    if _DAY.fullmatch(text):
        start, stop = day_bounds_utc(parse_day(text), zone)
        return stop if end else start
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        raise BadInput("start and end must be YYYY-MM-DD or ISO 8601 date-times") from None
    return when.replace(tzinfo=zone) if when.tzinfo is None else when


async def search_memories(
    pool: AsyncConnectionPool, router: Router | None, settings: Settings, query: str, start: str | None,
    end: str | None, limit: int,
) -> SearchOut:
    """The hybrid search of the Search page (P2b): meaning and words, the same thresholds."""
    zone = settings.zone
    first, last = parse_when(start, zone, end=False), parse_when(end, zone, end=True)
    if first is not None and last is not None and first >= last:
        raise BadInput("start must be before end")
    if not search.clean_query(query):
        raise BadInput("query is empty")
    result = await search.search_moments(
        pool, router, query, start=first, end=last, limit=max(1, min(SEARCH_LIMIT_MAX, limit)),
        max_distance=settings.search_max_distance, min_similarity=settings.search_min_similarity,
    )
    ends: dict[str, dt.datetime] = {}
    if result.hits:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT id, ts_end FROM moment WHERE id = ANY(%s)", ([h.id for h in result.hits],))
            ends = {row["id"]: row["ts_end"] for row in await cur.fetchall()}
    moments = [  # a moment forgotten since the search is left out
        MomentRef(moment_id=h.id, kind=h.kind, ts_start=iso(h.ts_start, zone), ts_end=iso(ends[h.id], zone),
                  day=h.ts_start.astimezone(zone).date().isoformat(), text=_clip(h.text), lang=h.lang)
        for h in result.hits if h.id in ends
    ]
    return SearchOut(moments=moments, not_found=not moments, semantic=result.semantic)


async def get_timeline(pool: AsyncConnectionPool, day_text: str, zone: ZoneInfo) -> TimelineOut:
    day = parse_day(day_text)
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, ts_start, ts_end, title, summary, closed FROM chapter WHERE day = %s ORDER BY ts_start, id",
            (day,),
        )
        chapters = await cur.fetchall()
        cur = await conn.execute(
            "SELECT id, ts_start, text FROM moment WHERE kind = 'marker' AND ts_start >= %s AND ts_start < %s"
            " ORDER BY ts_start, id",
            (start, end),
        )
        markers = await cur.fetchall()
        cur = await conn.execute(
            "SELECT kind, count(*) AS n FROM moment WHERE ts_start >= %s AND ts_start < %s GROUP BY kind", (start, end)
        )
        counts = {kind: 0 for kind in KINDS} | {row["kind"]: row["n"] for row in await cur.fetchall()}
    return TimelineOut(
        day=day.isoformat(),
        chapters=[
            ChapterOut(id=c["id"], ts_start=iso(c["ts_start"], zone), ts_end=iso(c["ts_end"], zone), title=c["title"],
                       summary=_clip(c["summary"]), closed=c["closed"])
            for c in chapters
        ],
        markers=[MarkerOut(moment_id=m["id"], ts_start=iso(m["ts_start"], zone), text=_clip(m["text"])) for m in markers],
        counts=counts,
    )


async def get_day_recap(pool: AsyncConnectionPool, day_text: str, zone: ZoneInfo) -> RecapOut:
    day = parse_day(day_text)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT markdown, generated_at, stale FROM day_recap WHERE day = %s", (day,))
        row = await cur.fetchone()
    if row is None:
        return RecapOut(day=day.isoformat(), exists=False, markdown=None, generated_at=None, stale=False)
    return RecapOut(day=day.isoformat(), exists=True, markdown=_clip(row["markdown"], MARKDOWN_MAX),
                    generated_at=iso(row["generated_at"], zone), stale=row["stale"])


def _caption(caption: dict | None) -> CaptionOut | None:
    if not isinstance(caption, dict):
        return None
    objects = [o[:100] for o in caption.get("objects") or [] if isinstance(o, str)][:OBJECTS_MAX]
    people = caption.get("people_count")
    return CaptionOut(
        scene=_clip(caption.get("scene")) if isinstance(caption.get("scene"), str) else None,
        activity=_clip(caption.get("activity")) if isinstance(caption.get("activity"), str) else None,
        objects=objects,
        people_count=people if isinstance(people, int) and not isinstance(people, bool) else None,
    )


async def get_moment(
    pool: AsyncConnectionPool, settings: Settings, moment_id: str, images: bool
) -> tuple[MomentOut, bytes | None]:
    """One moment with its caption; with ``images`` (the D24 switch) also its keyframe's 320 px thumbnail as JPEG,
    unless the keyframe is a golden-set sample (D21) or its file cannot be read (the picture is dropped, not the
    answer)."""
    wanted = moment_id.strip().upper() if isinstance(moment_id, str) else ""
    if not _ULID.fullmatch(wanted):
        raise NotFound("no such moment")
    zone = settings.zone
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT m.id, m.kind, m.ts_start, m.ts_end, m.text, m.lang, m.keyframe_id, k.caption,"
            " EXISTS (SELECT 1 FROM eval_caption e WHERE e.keyframe_id = m.keyframe_id) AS golden"
            " FROM moment m LEFT JOIN keyframe k ON k.id = m.keyframe_id WHERE m.id = %s",
            (wanted,),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound("no such moment")
    picture = None
    if images and row["keyframe_id"] and not row["golden"]:
        try:
            path = await thumbs.keyframe_thumb(pool, settings.media_root, row["keyframe_id"])
            picture = await anyio.to_thread.run_sync(path.read_bytes)
        except (thumbs.ThumbUnavailable, OSError):
            picture = None
    out = MomentOut(
        moment_id=row["id"], kind=row["kind"], ts_start=iso(row["ts_start"], zone), ts_end=iso(row["ts_end"], zone),
        day=row["ts_start"].astimezone(zone).date().isoformat(), text=_clip(row["text"]), lang=row["lang"],
        caption=_caption(row["caption"]) if row["kind"] == "visual" else None, image=picture is not None,
    )
    return out, picture


async def list_commitments(pool: AsyncConnectionPool, status: str | None) -> CommitmentsOut:
    if status is not None and status not in STATUSES:
        raise BadInput("status must be open, done or dismissed")
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, day, text, status, source_moment_id FROM commitment"
            " WHERE (%(status)s::text IS NULL OR status = %(status)s)"
            " ORDER BY day DESC, created_at DESC, id DESC LIMIT %(limit)s",
            {"status": status, "limit": COMMITMENTS_MAX},
        )
        rows = await cur.fetchall()
    return CommitmentsOut(commitments=[
        CommitmentOut(id=r["id"], day=r["day"].isoformat(), text=_clip(r["text"]), status=r["status"],
                      source_moment_id=r["source_moment_id"])
        for r in rows
    ])
```

- [ ] **Step 7: Run the tests to see them pass**

Run: `cd brain && uv run pytest -q tests/test_mcp_memory.py tests/test_day_page.py tests/test_store.py`
Expected: all pass. If `tests/test_store.py` does not exist, drop it from the command. The existing thumbnail tests on the Day page still pass unchanged.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: all pass; `mcp/memory.py`, `store.py` and `thumbs.py` at 100 %.

- [ ] **Step 8: Commit**

```bash
git add brain/src/loopi_brain/mcp/memory.py brain/src/loopi_brain/store.py brain/src/loopi_brain/thumbs.py \
        brain/src/loopi_brain/web/day_routes.py brain/tests/helpers.py brain/tests/test_mcp_memory.py
git commit -F - <<'EOF'
feat(brain): what the MCP tools read (search, timeline, recap, moment, commitments) and the D24 picture switch

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 3: The MCP server at `/mcp`: tools, bearer gate, lifespan

**Files:**
- Create: `brain/src/loopi_brain/mcp/server.py`
- Modify: `brain/src/loopi_brain/app.py`, `brain/src/loopi_brain/cli.py` (`_serve` configures logging first)
- Test: `brain/tests/test_mcp_server.py` (new)

**Interfaces:**
- Consumes: Task 1 `tokens.check_token`; Task 2 `memory.*`, `store.get_mcp`; `search.request_router(settings, client) -> Router | None`. From the SDK (`mcp==2.3.0`), verified:
  - `from mcp.server.mcpserver import Image, MCPServer`;
  - `from mcp.server import ServerRequestContext`;
  - `from mcp.server.transport_security import TransportSecuritySettings`;
  - `from mcp.types import CallToolResult, TextContent`;
  - `MCPServer(name=..., instructions=..., middleware=[...])`;
  - `@server.tool()`;
  - `server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, transport_security=...)` returns a Starlette app with `Route("/mcp")`;
  - `server.session_manager.run()` is an async context manager. It must wrap serving, exactly once per server instance.
  - Client: `from mcp import Client` and `from mcp.client.streamable_http import streamable_http_client`; usage `Client(streamable_http_client(url, http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=...)), mode="auto" | "legacy")`. `client.list_tools().tools[i].name / .output_schema`. `client.call_tool(name, args)` returns `.content`, `.structured_content` and `.is_error`. Image items have `.data` (base64) and `.mime_type`.
- Produces (`loopi_brain.mcp.server`):
  - `PATH = "/mcp"`, `INVALID_ARGUMENTS`, `INTERNAL_ERROR`;
  - `@dataclass(frozen=True) Deps(pool, settings, llm)`;
  - `build_server(deps) -> MCPServer`;
  - `BearerGate(app, pool)`;
  - `mount(app: FastAPI, deps: Deps) -> MCPServer`.
  - `app.state.mcp` is the mounted `MCPServer`.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_mcp_server.py`:

```python
import asyncio
import base64
import contextlib
import datetime as dt
import io
import logging

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

from helpers import FakeOllama, captioned_keyframe, fetch_all, insert_moment, paired_device
from loopi_brain import store
from loopi_brain.app import create_app
from loopi_brain.mcp import memory, tokens

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
TOOLS = {"search_memories", "get_timeline", "get_day_recap", "get_moment", "list_commitments"}
SECRET = "SECRET-7f3a"


@contextlib.asynccontextmanager
async def running(app):
    """Run the app's lifespan in one task. The MCP session manager's task group must be left by the task that
    entered it, and pytest-asyncio runs a fixture's setup and teardown in different tasks."""
    started, stop = asyncio.Event(), asyncio.Event()

    async def run():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(run())
    waiter = asyncio.create_task(started.wait())
    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        waiter.cancel()
        task.result()  # the lifespan failed to start: re-raise its error
    try:
        yield app
    finally:
        stop.set()
        await task


@pytest.fixture
async def app(pool, pki_settings):
    ollama = FakeOllama()
    application = create_app(pki_settings, pool=pool, http_client=ollama.client(), llm_client=ollama.client())
    async with running(application):
        yield application


@pytest.fixture
async def token(pool):
    _, value = await tokens.create_token(pool, "test")
    return value


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@contextlib.asynccontextmanager
async def mcp_client(app, token, mode="auto"):
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client("https://brain.test/mcp", http_client=http), mode=mode) as client:
        yield client


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_the_five_tools_are_listed_with_their_output_schemas(app, token, mode):
    async with mcp_client(app, token, mode) as client:
        listed = (await client.list_tools()).tools
    assert {t.name for t in listed} == TOOLS
    assert all(t.output_schema for t in listed) and all(t.description for t in listed)


async def test_search_answers_moments_that_cite_their_ids(app, token, dev, pool):
    moment = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, axis=0, lang="vi", text="đi chợ")
    async with mcp_client(app, token) as client:
        result = await client.call_tool("search_memories", {"query": "đi chợ", "start": "2026-10-14", "end": "2026-10-14"})
    assert result.is_error is False
    assert [m["moment_id"] for m in result.structured_content["moments"]] == [moment]
    assert result.structured_content["moments"][0]["ts_start"] == "2026-10-14T09:00:00+09:00"


async def test_every_tool_answers_for_a_day(app, token, dev, pool):
    marker = await insert_moment(pool, dev["device_id"], kind="marker", ts=NINE, text="Đánh dấu")
    async with mcp_client(app, token) as client:
        timeline = await client.call_tool("get_timeline", {"date": "2026-10-14"})
        recap = await client.call_tool("get_day_recap", {"date": "2026-10-14"})
        moment = await client.call_tool("get_moment", {"moment_id": marker})
        todo = await client.call_tool("list_commitments", {"status": "open"})
    assert timeline.structured_content["markers"][0]["moment_id"] == marker
    assert recap.structured_content["exists"] is False
    assert (moment.structured_content["text"], moment.structured_content["image"]) == ("Đánh dấu", False)
    assert todo.structured_content == {"commitments": []}
    assert not any(r.is_error for r in (timeline, recap, moment, todo))


async def test_get_moment_sends_a_picture_only_when_the_owner_allows_it(app, token, dev, pool, pki_settings):
    keyframe = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    moment = await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, text="x", keyframe_id=keyframe)
    async with mcp_client(app, token) as client:
        off = await client.call_tool("get_moment", {"moment_id": moment})
        await store.set_mcp(pool, store.McpSettings(images=True))
        on = await client.call_tool("get_moment", {"moment_id": moment})
    assert [c.type for c in off.content] == ["text"] and off.structured_content["image"] is False
    assert [c.type for c in on.content] == ["text", "image"] and on.structured_content["image"] is True
    assert on.content[1].mime_type == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(on.content[1].data))) as img:
        assert img.width == 320


@pytest.mark.parametrize(("tool", "args", "text"), [
    ("get_moment", {"moment_id": f"01{SECRET}"}, "no such moment"),
    ("get_timeline", {"date": f"2026-13-45-{SECRET}"}, "date must be YYYY-MM-DD"),
    ("search_memories", {"query": "x", "start": f"{SECRET}"}, "start and end must be YYYY-MM-DD or ISO 8601 date-times"),
    ("search_memories", {"query": " "}, "query is empty"),
    ("search_memories", {"query": SECRET, "limit": 1000}, "invalid arguments"),
    ("list_commitments", {"status": SECRET}, "invalid arguments"),
    ("get_moment", {}, "invalid arguments"),
])
async def test_errors_are_fixed_texts_that_never_repeat_the_input(app, token, tool, args, text):
    async with mcp_client(app, token) as client:
        result = await client.call_tool(tool, args)
    assert result.is_error is True and [c.text for c in result.content] == [text]


async def test_a_crash_inside_a_tool_is_an_internal_error_without_its_text_in_the_logs(app, token, monkeypatch, caplog):
    async def broken(*_args, **_kwargs):
        raise RuntimeError(f"connection lost while reading {SECRET}")

    monkeypatch.setattr(memory, "get_timeline", broken)
    caplog.set_level(logging.DEBUG)
    async with mcp_client(app, token) as client:
        result = await client.call_tool("get_timeline", {"date": "2026-10-14"})
    assert result.is_error is True and [c.text for c in result.content] == ["internal error"]
    assert SECRET not in caplog.text and "RuntimeError" in caplog.text


async def test_questions_and_answers_never_reach_the_logs(app, token, dev, pool, caplog):
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, axis=0, text=f"nói {SECRET} hôm nay")
    caplog.set_level(logging.DEBUG)
    async with mcp_client(app, token) as client:
        result = await client.call_tool("search_memories", {"query": f"{SECRET} hôm nay"})
    assert result.structured_content["moments"]  # the secret was searched and found …
    assert SECRET not in caplog.text  # … and appears in no log record, from the server, the SDK or its client
    assert "mcp search_memories ok" in caplog.text


async def test_the_gate_refuses_before_any_mcp_code_runs(app, token, pool):
    revoked_id, revoked = await tokens.create_token(pool, "old")
    await tokens.revoke_token(pool, revoked_id)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://brain.test") as c:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": token},
                        {"Authorization": f"Basic {token}"}, {"Authorization": f"Bearer {revoked}"}):
            r = await c.post("/mcp", json=body, headers=headers)
            assert (r.status_code, r.json()) == (401, {"error": "unauthorized"})
            assert r.headers["www-authenticate"] == "Bearer"
        r = await c.get("/mcp", headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"})
        assert (r.status_code, r.headers["allow"]) == (405, "POST")
        assert (await c.get("/mcp")).status_code == 401
    assert all(t["last_used_at"] is None for t in await tokens.list_tokens(pool) if t["id"] == revoked_id)


async def test_a_good_call_marks_its_token_used(app, token, pool):
    async with mcp_client(app, token) as client:
        await client.list_tools()
    [row] = await fetch_all(pool, "SELECT last_used_at FROM mcp_token WHERE name = 'test'")
    assert row["last_used_at"] is not None


async def test_the_web_pages_still_answer_next_to_mcp(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://brain.test") as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}
        assert (await c.get("/settings")).status_code == 200
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `cd brain && uv run pytest -q tests/test_mcp_server.py`
Expected: FAIL. `POST /mcp` answers 404 or 405 (no route), and the client cannot connect.

- [ ] **Step 3: Write the server**

Create `brain/src/loopi_brain/mcp/server.py`:

```python
"""The read-only MCP server (spec §10.5, P3 design §3): the official SDK's MCPServer speaking Streamable HTTP,
stateless with JSON answers, served at exactly ``/mcp`` inside the api process behind a bearer-token gate.

This module deliberately does not use ``from __future__ import annotations``: the SDK reads the tools' annotations
when they are registered, to build their input and output schemas."""
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI
from mcp.server import ServerRequestContext
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import search, store
from ..config import Settings
from . import memory, tokens

log = logging.getLogger(__name__)

PATH = "/mcp"
INVALID_ARGUMENTS = "invalid arguments"
INTERNAL_ERROR = "internal error"
INSTRUCTIONS = (
    "Loopi is a private, local-first diary recorded at home by a fixed camera and microphone. These read-only tools "
    "return its moments (picture captions, speech transcripts, marked moments), the day's chapters and recap, and the "
    "commitments found in conversations. Cite the moment_id of every moment you use. Times are local with an offset."
)
UNAUTHORIZED = json.dumps({"error": "unauthorized"}).encode()
NOT_ALLOWED = json.dumps({"error": "method_not_allowed"}).encode()

# The SDK logs tool failures with their text (INFO) and its client logs request arguments (DEBUG); none of that may
# carry a question or a memory. The tool wrappers below log only tool names, durations and error classes.
logging.getLogger("mcp").setLevel(logging.WARNING)


@dataclass(frozen=True)
class Deps:
    pool: AsyncConnectionPool
    settings: Settings
    llm: httpx.AsyncClient  # the model client search_memories embeds questions with


def _result(model: BaseModel, picture: bytes | None = None) -> CallToolResult:
    content: list[Any] = [TextContent(type="text", text=model.model_dump_json())]
    if picture is not None:
        content.append(Image(data=picture, format="jpeg").to_image_content())
    return CallToolResult(content=content, structured_content=model.model_dump(mode="json"))


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


async def _run(name: str, call: Callable[[], Awaitable[CallToolResult]]) -> CallToolResult:
    """Run one tool body. NotFound and BadInput become tool errors with their fixed text, anything else
    INTERNAL_ERROR; only the tool's name, its time and an error's class are logged, never an argument or a result."""
    started = time.monotonic()
    try:
        result = await call()
    except (memory.NotFound, memory.BadInput) as exc:
        log.info("mcp %s: %s in %.2f s", name, type(exc).__name__, time.monotonic() - started)
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001 - the client gets a fixed text; the class is logged, not the message
        log.error("mcp %s failed: %s", name, type(exc).__name__)
        return _error(INTERNAL_ERROR)
    log.info("mcp %s ok in %.2f s", name, time.monotonic() - started)
    return result


async def hide_validation_input(ctx: ServerRequestContext[Any, Any], call_next: Any) -> Any:
    """The SDK's argument-validation errors quote the value that was sent; answer them with a fixed text."""
    result = await call_next(ctx)
    if ctx.method == "tools/call" and isinstance(result, dict) and result.get("isError") is True:
        text = (result.get("content") or [{}])[0].get("text", "")
        if " validation error for " in text:
            return {**result, "content": [{"type": "text", "text": INVALID_ARGUMENTS}]}
    return result


def build_server(deps: Deps) -> MCPServer:
    server = MCPServer(name="loopi", instructions=INSTRUCTIONS, middleware=[hide_validation_input])
    zone = deps.settings.zone

    @server.tool()
    async def search_memories(
        query: Annotated[str, Field(description="What to look for, in any language (the first 500 characters count).")],
        start: Annotated[str | None, Field(description="Earliest moment: YYYY-MM-DD (from that local day) or an ISO 8601 date-time (local when it has no offset).")] = None,
        end: Annotated[str | None, Field(description="Latest moment: YYYY-MM-DD (that local day included) or an ISO 8601 date-time.")] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="How many moments to return.")] = 10,
    ) -> Annotated[CallToolResult, memory.SearchOut]:
        """Search the owner's memories (picture captions, speech, marked moments) by meaning and by words. Cite moment_id."""

        async def call() -> CallToolResult:
            router = search.request_router(deps.settings, deps.llm)
            return _result(await memory.search_memories(deps.pool, router, deps.settings, query, start, end, limit))

        return await _run("search_memories", call)

    @server.tool()
    async def get_timeline(
        date: Annotated[str, Field(description="The local day, YYYY-MM-DD.")],
    ) -> Annotated[CallToolResult, memory.TimelineOut]:
        """The chapters of one day (title, summary, times), its marked moments and how many moments of each kind it has."""
        return await _run("get_timeline", lambda: _as_result(memory.get_timeline(deps.pool, date, zone)))

    @server.tool()
    async def get_day_recap(
        date: Annotated[str, Field(description="The local day, YYYY-MM-DD.")],
    ) -> Annotated[CallToolResult, memory.RecapOut]:
        """The end-of-day recap of one day, as Vietnamese markdown (main events, memorable moments, commitments)."""
        return await _run("get_day_recap", lambda: _as_result(memory.get_day_recap(deps.pool, date, zone)))

    @server.tool()
    async def get_moment(
        moment_id: Annotated[str, Field(description="A moment_id from search_memories or get_timeline.")],
    ) -> Annotated[CallToolResult, memory.MomentOut]:
        """One moment: its text, times, language and picture caption; its keyframe picture only if the owner allows it."""

        async def call() -> CallToolResult:
            images = (await store.get_mcp(deps.pool)).images
            out, picture = await memory.get_moment(deps.pool, deps.settings, moment_id, images)
            return _result(out, picture)

        return await _run("get_moment", call)

    @server.tool()
    async def list_commitments(
        status: Annotated[Literal["open", "done", "dismissed"] | None, Field(description="Only this status; all when omitted.")] = None,
    ) -> Annotated[CallToolResult, memory.CommitmentsOut]:
        """Things to do or promised, found in conversations, newest first (at most 200)."""
        return await _run("list_commitments", lambda: _as_result(memory.list_commitments(deps.pool, status)))

    return server


async def _as_result(read: Awaitable[BaseModel]) -> CallToolResult:
    return _result(await read)


async def _send_json(send: Send, status: int, body: bytes, extra: list[tuple[bytes, bytes]]) -> None:
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *extra]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class BearerGate:
    """Every request to /mcp: a live bearer token, or 401 with a fixed body before any MCP code runs; then POST only
    (stateless JSON needs nothing else, and a GET would hold an idle event stream open)."""

    def __init__(self, app: ASGIApp, pool: AsyncConnectionPool) -> None:
        self.app, self.pool = app, pool

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        scheme, _, token = raw.partition(" ")
        token = token.strip()
        token_id = await tokens.check_token(self.pool, token) if scheme.lower() == "bearer" and token else None
        if token_id is None:
            await _send_json(send, 401, UNAUTHORIZED, [(b"www-authenticate", b"Bearer")])
            return
        if scope["method"] != "POST":
            await _send_json(send, 405, NOT_ALLOWED, [(b"allow", b"POST")])
            return
        await self.app({**scope, "state": {**scope.get("state", {}), "mcp_token_id": token_id}}, receive, send)


def mount(app: FastAPI, deps: Deps) -> MCPServer:
    """Serve the MCP server at exactly PATH: a Starlette Route, because a mount would redirect ``POST /mcp`` to
    ``/mcp/``. The app's lifespan must run ``server.session_manager.run()`` (once) around serving."""
    server = build_server(deps)
    asgi = server.streamable_http_app(
        streamable_http_path=PATH,
        stateless_http=True,
        json_response=True,
        # Behind Caddy every request carries the LAN Host header, which the SDK's DNS-rebinding check would refuse
        # (421); the bearer gate, not the Host header, decides who may call.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.router.routes.append(Route(PATH, endpoint=BearerGate(asgi, deps.pool)))
    return server
```

- [ ] **Step 4: Mount it in the app and run its session manager in the lifespan**

In `brain/src/loopi_brain/app.py`:
- add the import `from .mcp import server as mcp_server`.
- In `lifespan`, wrap the `yield` in the MCP session manager. The `try:` block inside `open_jobs` becomes:

```python
                try:
                    async with _app.state.mcp.session_manager.run():  # the MCP server's task group (spec §10.5)
                        yield
                finally:
```

- After `app.include_router(eval_router)`, add:

```python
    app.state.mcp = mcp_server.mount(app, mcp_server.Deps(pool=active_pool, settings=settings, llm=llm))
```

In `brain/src/loopi_brain/cli.py`, in `_serve`:
- add `import logging` at the top of the function, next to `import uvicorn`;
- before `settings = Settings()`, add:

```python
    # The MCP SDK calls logging.basicConfig(INFO) when the root logger has no handler; configure it first so the api
    # keeps logging what it logged before (warnings and errors; uvicorn keeps its own access log).
    logging.basicConfig(level=logging.WARNING)
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd brain && uv run pytest -q tests/test_mcp_server.py`
Expected: all pass, including both client modes.

If `t.output_schema` or `.mime_type` is named differently in the installed SDK, read `brain/.venv/lib/python3.13/site-packages/mcp_types/` (or `mcp/types`), use the real attribute names, and say so in the report.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: all pass; `mcp/server.py` ≥ 95 % (the non-HTTP scope branch of the gate may stay uncovered), total ≥ 99 %.

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/mcp/server.py brain/src/loopi_brain/app.py brain/src/loopi_brain/cli.py \
        brain/tests/test_mcp_server.py
git commit -F - <<'EOF'
feat(brain): the read-only MCP server at /mcp (five tools, bearer gate, no user text in logs or errors)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 4: Cài đặt "MCP (Claude Code)", `loopi-brain mcp-token`, Caddy and the README

**Files:**
- Modify: `brain/src/loopi_brain/web/settings_routes.py`, `brain/src/loopi_brain/web/templates/settings.html`
- Create: `brain/src/loopi_brain/web/templates/_mcp_tokens.html`
- Modify: `brain/src/loopi_brain/cli.py` (`mcp-token`), `deploy/Caddyfile`, `deploy/README.md`, `docs/superpowers/specs/2026-10-17-p3-mcp-vlog-design.md` (R1, R2 wording)
- Test: `brain/tests/test_settings_mcp.py` (new), `brain/tests/test_cli.py`, `brain/tests/test_deploy.py`

**Interfaces:**
- Consumes:
  - Task 1 `tokens.create_token`, `list_tokens`, `revoke_token`, `TokenError`, `NAME_MAX`;
  - Task 2 `store.get_mcp`, `set_mcp`, `McpSettings`;
  - P2b `web.routes.TEMPLATES`, `require_htmx` (POSTs need `HX-Request: true`, else 403).
- Produces:
  - `POST /settings/mcp/tokens` (`name`): the `#mcp-tokens` fragment, showing the new token once (`Cache-Control: no-store`), or the reason when the name cannot be used (200);
  - `POST /settings/mcp/tokens/{token_id}/revoke`: the fragment (404 for an unknown or already revoked token);
  - `POST /settings/mcp/images` (`enabled` = "on" | ""): a saved message;
  - CLI `loopi-brain mcp-token create NAME` (prints only the token), `mcp-token list`, `mcp-token revoke ID` (exit 1 when there is no live token with that id; 2 for a bad name).

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_settings_mcp.py`:

```python
import re

import httpx
import pytest

from helpers import FakeOllama, fetch_all
from loopi_brain import store
from loopi_brain.app import create_app
from loopi_brain.mcp import tokens

HX = {"HX-Request": "true"}


@pytest.fixture
async def web(pool, pki_settings, jobs_open):
    app = create_app(pki_settings, pool=pool, http_client=FakeOllama().client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def test_the_settings_page_has_the_mcp_card_with_pictures_off(web):
    text = (await web.get("/settings")).text
    assert 'id="mcp"' in text and "MCP (Claude Code)" in text
    assert "Chữ trả về qua MCP sẽ được client gửi lên model trên cloud" in text
    assert "Ảnh qua MCP: <strong>đang tắt</strong>" in text and "Chưa có token nào." in text


async def test_a_new_token_is_shown_once_with_the_command_to_connect(web, pool):
    r = await web.post("/settings/mcp/tokens", data={"name": "Claude Code"}, headers=HX)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    [row] = await fetch_all(pool, "SELECT id, token_sha256 FROM mcp_token")
    shown = set(re.findall(r"loopi_[A-Za-z0-9_-]{40,}", r.text))
    assert len(shown) == 1 and tokens.digest(next(iter(shown))) == row["token_sha256"]
    assert "claude mcp add --transport http loopi https://localhost:8443/mcp" in r.text
    page = (await web.get("/settings")).text
    assert next(iter(shown)) not in page and "Claude Code" in page and "chưa dùng" in page  # never shown again


@pytest.mark.parametrize("name", ["", "   ", "x" * 61])
async def test_an_unusable_name_gets_its_reason_next_to_the_form(web, pool, name):
    r = await web.post("/settings/mcp/tokens", data={"name": name}, headers=HX)
    assert r.status_code == 200 and "Tên token phải có từ 1 tới 60 ký tự." in r.text
    assert "x" * 61 not in r.text and await fetch_all(pool, "SELECT * FROM mcp_token") == []


async def test_a_token_can_be_revoked_once(web, pool):
    token_id, token = await tokens.create_token(pool, "old")
    r = await web.post(f"/settings/mcp/tokens/{token_id}/revoke", headers=HX)
    assert r.status_code == 200 and "đã thu hồi" in r.text
    assert await tokens.check_token(pool, token) is None
    assert (await web.post(f"/settings/mcp/tokens/{token_id}/revoke", headers=HX)).status_code == 404
    assert (await web.post("/settings/mcp/tokens/01NOSUCH/revoke", headers=HX)).status_code == 404


async def test_the_picture_switch_saves(web, pool):
    r = await web.post("/settings/mcp/images", data={"enabled": "on"}, headers=HX)
    assert r.status_code == 200 and "được gửi" in r.text and (await store.get_mcp(pool)).images is True
    r = await web.post("/settings/mcp/images", data={}, headers=HX)
    assert "không gửi ảnh" in r.text and (await store.get_mcp(pool)).images is False


@pytest.mark.parametrize("path", ["/settings/mcp/tokens", "/settings/mcp/tokens/x/revoke", "/settings/mcp/images"])
async def test_every_mcp_post_needs_htmx_and_every_form_is_a_post(web, pool, path):
    assert (await web.post(path, data={"name": "a", "enabled": "on"})).status_code == 403
    assert await fetch_all(pool, "SELECT * FROM mcp_token") == [] and (await store.get_mcp(pool)).images is False
    page = (await web.get("/settings")).text
    card = page.split('id="mcp"', 1)[1].split("</section>", 1)[0]
    assert card.count("<form") == card.count('method="post"') == 2
```

Append to `brain/tests/test_cli.py`:

```python
def test_mcp_token_commands_print_the_token_once_and_list_without_it(database_url, monkeypatch, capsys):
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    assert cli.main(["mcp-token", "create", "Claude Code"]) == 0
    token = capsys.readouterr().out.strip()
    assert token.startswith("loopi_") and "\n" not in token
    assert cli.main(["mcp-token", "list"]) == 0
    listed = capsys.readouterr().out
    [line] = [row for row in listed.splitlines() if row.endswith("Claude Code")]
    assert "live" in line and token not in listed
    token_id = line.split()[0]
    assert cli.main(["mcp-token", "revoke", token_id]) == 0
    assert cli.main(["mcp-token", "revoke", token_id]) == 1
    assert cli.main(["mcp-token", "create", " "]) == 2
```

`test_cli.py` already imports `cli`. This test talks to the session database directly (no `pool` fixture, so nothing is truncated first); it finds its own token by name, so rows other tests left behind do not matter.

Append to `brain/tests/test_deploy.py`:

```python
def test_mcp_goes_to_the_api_without_basic_auth_and_without_a_client_fingerprint():
    caddy = (DEPLOY / "Caddyfile").read_text()
    assert "\thandle /mcp* {" in caddy
    block = caddy.split("\thandle /mcp* {", 1)[1].split("\n\t}\n", 1)[0]
    assert "reverse_proxy api:8000" in block and "header_up -X-Loopi-Client-Fingerprint" in block
    assert "max_size 64KB" in block and "basic_auth" not in block
    assert caddy.index("\thandle /mcp* {") < caddy.index("\thandle {\n")


def test_the_readme_says_how_to_connect_claude_code():
    readme = (DEPLOY / "README.md").read_text()
    assert "## MCP for Claude Code (P3a)" in readme
    assert "NODE_EXTRA_CA_CERTS" in readme and "claude mcp add --transport http loopi" in readme
    assert "loopi-brain mcp-token create" in readme
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `cd brain && uv run pytest -q tests/test_settings_mcp.py tests/test_cli.py tests/test_deploy.py`
Expected: FAIL. There is no MCP card, the POSTs answer 404, `mcp-token` is an invalid choice, and the Caddyfile has no `/mcp*` block.

- [ ] **Step 3: The routes**

In `brain/src/loopi_brain/web/settings_routes.py`:
- add `from ..mcp import tokens as mcp_tokens` to the imports;
- in `settings_context`, add three keys: `"mcp": await store.get_mcp(state.pool)`, `"mcp_tokens": await mcp_tokens.list_tokens(state.pool)` and `"zone": state.settings.zone`.

Then append:

```python
NAME_REASON = f"Tên token phải có từ 1 tới {mcp_tokens.NAME_MAX} ký tự."


async def _tokens_view(request: Request, *, created: str | None = None, error: str | None = None) -> HTMLResponse:
    """The token list; right after a token is made it is shown once, so the answer must never be cached."""
    state = request.app.state
    return TEMPLATES.TemplateResponse(
        request,
        "_mcp_tokens.html",
        {"mcp_tokens": await mcp_tokens.list_tokens(state.pool), "created": created, "error": error,
         "zone": state.settings.zone},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/settings/mcp/tokens", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def create_mcp_token(request: Request, name: str = Form("")):
    try:
        _, token = await mcp_tokens.create_token(request.app.state.pool, name)
    except mcp_tokens.TokenError:
        return await _tokens_view(request, error=NAME_REASON)
    return await _tokens_view(request, created=token)


@router.post("/settings/mcp/tokens/{token_id}/revoke", response_class=HTMLResponse,
             dependencies=[Depends(require_htmx)])
async def revoke_mcp_token(request: Request, token_id: str):
    if not await mcp_tokens.revoke_token(request.app.state.pool, token_id):
        raise HTTPException(status_code=404, detail="unknown token")
    return await _tokens_view(request)


@router.post("/settings/mcp/images", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def save_mcp_images(request: Request, enabled: str = Form("")):
    chosen = store.McpSettings(images=enabled == "on")
    await store.set_mcp(request.app.state.pool, chosen)
    what = "<strong>được gửi</strong> ảnh keyframe (320 px)" if chosen.images else "không gửi ảnh, chỉ gửi chữ"
    return HTMLResponse(f"<span>Đã lưu: MCP {what}.</span>")
```

- [ ] **Step 4: The card**

Create `brain/src/loopi_brain/web/templates/_mcp_tokens.html`:

```html
<div id="mcp-tokens">
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
  {% if created %}
  <p><strong>Token mới (chỉ hiện một lần, hãy chép ngay):</strong></p>
  <pre><code>{{ created }}</code></pre>
  <p>Kết nối Claude Code trên máy brain (đường dẫn CA nằm trong thư mục dữ liệu, ví dụ <code>/home/soul/loopi-data/pki/ca.crt</code>):</p>
  <pre><code>export NODE_EXTRA_CA_CERTS=&lt;thư mục dữ liệu&gt;/pki/ca.crt
claude mcp add --transport http loopi https://localhost:8443/mcp --header "Authorization: Bearer {{ created }}"</code></pre>
  {% endif %}
  {% if mcp_tokens %}
  <div class="scroll"><table>
    <tr><th>Tên</th><th>Tạo lúc</th><th>Dùng lần cuối</th><th></th></tr>
    {% for t in mcp_tokens %}
    <tr id="mcp-token-{{ t.id }}">
      <td>{{ t.name }}</td>
      <td>{{ t.created_at.astimezone(zone).strftime("%d/%m/%Y %H:%M") }}</td>
      <td>{% if t.last_used_at %}{{ t.last_used_at.astimezone(zone).strftime("%d/%m/%Y %H:%M") }}{% else %}chưa dùng{% endif %}</td>
      <td>{% if t.revoked_at %}đã thu hồi{% else %}
        <form method="post" hx-post="/settings/mcp/tokens/{{ t.id }}/revoke" hx-target="#mcp-tokens" hx-swap="outerHTML"
              hx-confirm="Thu hồi token “{{ t.name }}”? Client dùng token này sẽ không đọc được nữa."><button>Thu hồi</button></form>
      {% endif %}</td>
    </tr>
    {% endfor %}
  </table></div>
  {% else %}<p class="muted">Chưa có token nào.</p>{% endif %}
</div>
```

Note: the revoke forms inside the table do not count toward the card's two forms in the test. The test counts `<form` and `method="post"` equally, and a table with tokens adds the same number of each.

In `brain/src/loopi_brain/web/templates/settings.html`, insert before the `Job lỗi` section:

```html
<section class="card" id="mcp">
  <h2>MCP (Claude Code)</h2>
  <p>Claude Code và các client MCP khác đọc trí nhớ (chỉ đọc) qua <code>/mcp</code>, mỗi client một token riêng.
     <strong>Chữ trả về qua MCP sẽ được client gửi lên model trên cloud.</strong></p>
  <form method="post" hx-post="/settings/mcp/images" hx-target="#mcp-images-result"
        hx-confirm="Bật gửi ảnh? Ảnh keyframe (320 px) của các khoảnh khắc được hỏi sẽ được client MCP gửi lên model trên cloud."
        hx-on::confirm="if (!this.elements.enabled.checked) { event.preventDefault(); event.detail.issueRequest(true); }">
    <label><input type="checkbox" name="enabled"{% if mcp.images %} checked{% endif %}> Cho MCP gửi ảnh keyframe (320 px)</label>
    <button>Lưu</button>
  </form>
  <p>Ảnh qua MCP: {% if mcp.images %}<strong>đang bật</strong>{% else %}<strong>đang tắt</strong> (chỉ gửi chữ){% endif %}. Ảnh của bộ đánh giá không bao giờ được gửi.</p>
  <div id="mcp-images-result"></div>
  <form method="post" hx-post="/settings/mcp/tokens" hx-target="#mcp-tokens" hx-swap="outerHTML">
    <label>Tên token <input name="name" maxlength="60" required placeholder="Claude Code trên máy brain"></label>
    <button>Tạo token</button>
  </form>
  {% include "_mcp_tokens.html" %}
</section>
```

The test `test_every_mcp_post_needs_htmx_and_every_form_is_a_post` runs with no tokens, so the card holds exactly two forms.

- [ ] **Step 5: The CLI**

In `brain/src/loopi_brain/cli.py`, in `build_parser`, after the `acceptance` parser, add:

```python
    mcp_token = sub.add_parser("mcp-token", help="bearer tokens of the read-only MCP server (spec §10.5)")
    mcp_sub = mcp_token.add_subparsers(dest="mcp_cmd", required=True)
    create = mcp_sub.add_parser("create", help="make a token and print it once (only its hash is kept)")
    create.add_argument("name")
    mcp_sub.add_parser("list", help="list the tokens (never the tokens themselves)")
    revoke = mcp_sub.add_parser("revoke", help="revoke a token by its id")
    revoke.add_argument("token_id")
```

Add the command:

```python
async def _mcp_token(args: argparse.Namespace) -> int:
    from .config import Settings
    from .db import make_pool
    from .mcp import tokens

    settings = Settings()
    pool = make_pool(settings.database_url)
    await pool.open()
    try:
        if args.mcp_cmd == "create":
            try:
                _, token = await tokens.create_token(pool, args.name)
            except tokens.TokenError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(token)
            return 0
        if args.mcp_cmd == "list":
            for t in await tokens.list_tokens(pool):
                state = "revoked" if t["revoked_at"] else "live"
                used = t["last_used_at"].astimezone(settings.zone).strftime("%Y-%m-%d %H:%M") if t["last_used_at"] else "never"
                print(f"{t['id']}  {state:7}  last used {used}  {t['name']}")
            return 0
        if await tokens.revoke_token(pool, args.token_id):
            print("revoked")
            return 0
        print("error: no live token with that id", file=sys.stderr)
        return 1
    finally:
        await pool.close()
```

Then, in `main`, before `return 2`, add:

```python
    if args.cmd == "mcp-token":
        return asyncio.run(_mcp_token(args))
```

- [ ] **Step 6: Caddy and the README**

In `deploy/Caddyfile`, insert this block before the `# Web UI and media: basic auth; …` comment (one tab of indentation, like the other blocks):

```
	# MCP (spec §10.5): read only; the brain checks the bearer token itself. No basic auth, no client certificate
	# (the site's verify_if_given policy lets a client without one through); small JSON requests only.
	handle /mcp* {
		request_body {
			max_size 64KB
		}
		reverse_proxy api:8000 {
			header_up -X-Loopi-Client-Fingerprint
		}
	}

```

Append to `deploy/README.md`:

````markdown
## MCP for Claude Code (P3a)

The brain serves a read-only MCP server at `https://<brain>:8443/mcp` (spec §10.5) with five tools: `search_memories`,
`get_timeline`, `get_day_recap`, `get_moment`, `list_commitments`. Every client needs its own bearer token.

1. Make a token on Cài đặt → "MCP (Claude Code)" → "Tạo token". The page shows it once; only its SHA-256 is kept.
   On the brain PC you can also run
   `docker compose -p deploy exec -T api loopi-brain mcp-token create "Claude Code" > ~/.config/loopi/mcp-token`
   (run `umask 077 && mkdir -p ~/.config/loopi` first). `mcp-token list` and `mcp-token revoke <id>` manage tokens.
2. Claude Code (Node) must trust the Loopi CA: `export NODE_EXTRA_CA_CERTS=$LOOPI_DATA_DIR/pki/ca.crt` in the shell
   (or the profile) that starts `claude`.
3. `claude mcp add --transport http loopi https://localhost:8443/mcp --header "Authorization: Bearer <token>"`.
   From another machine in the LAN, copy `ca.crt` there and use `https://<brain LAN IP>:8443/mcp`. The server
   certificate must name that address (`init-pki --ip`).
4. `claude mcp list` shows `loopi` as connected. In a session, `/mcp` lists its tools.

Privacy: text returned by the tools (captions, transcripts, recaps, commitments) goes to the model behind the
client, in the cloud. Keyframe pictures are sent only when "Cho MCP gửi ảnh keyframe" is on (default off, spec D24),
as 320 px thumbnails. Golden-set pictures are never sent. Revoke a token on Cài đặt to cut its client off at once.
````

In `docs/superpowers/specs/2026-10-17-p3-mcp-vlog-design.md` §3.2, make two edits:
- In the `search_memories` row, replace "Không có gì liên quan thì trả danh sách rỗng kèm `"not_found": true`" with "Không có gì liên quan thì trả danh sách rỗng kèm `"not_found": true`; không gọi được model embedding thì vẫn tìm theo chữ và trả `"semantic": false` (như trang Tìm kiếm)".
- In the "Khi có lỗi" bullet, remove the sentence "Không có model embedding: `search_memories` trả lỗi "search is not ready"." In the "Quy ước chung" bullet, replace "Mỗi chữ trả về được cắt ở 500 ký tự" with "Chữ của moment và tóm tắt chương được cắt ở 500 ký tự, recap ở 8000".

- [ ] **Step 7: Run the tests to see them pass**

Run: `cd brain && uv run pytest -q tests/test_settings_mcp.py tests/test_cli.py tests/test_deploy.py`
Expected: all pass.

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: all pass, total ≥ 99 %.

- [ ] **Step 8: Commit**

```bash
git add brain/src/loopi_brain/web/settings_routes.py brain/src/loopi_brain/web/templates/settings.html \
        brain/src/loopi_brain/web/templates/_mcp_tokens.html brain/src/loopi_brain/cli.py deploy/Caddyfile \
        deploy/README.md docs/superpowers/specs/2026-10-17-p3-mcp-vlog-design.md \
        brain/tests/test_settings_mcp.py brain/tests/test_cli.py brain/tests/test_deploy.py
git commit -F - <<'EOF'
feat(brain): MCP tokens and the picture switch on Cài đặt, loopi-brain mcp-token, the /mcp route in Caddy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 5: Deploy on the live stack and call the five tools from Claude Code (controller-executed)

The controller runs this task, not a subagent. It starts after the Tasks 1–4 final review and fix wave, and only after the owner's yes. Stop and ask the owner at Step 2: it migrates the live database, restarts api, worker and proxy, and registers an MCP server in the owner's Claude Code configuration.

**Files:**
- Live only, not committed: branch `feat/phase2` (fast-forwarded), compose project `deploy`, `~/loopi-backups/`, `~/.config/loopi/mcp-token` (mode 600, never printed), the owner's Claude Code MCP config.
- Committed: `docs/superpowers/ledgers/p3a-progress.md`, a `docs/HANDOFF.md` P3a row.

**Rollback** (any step; uses `$ROLLBACK` from Step 3):

```bash
cd /home/soul/loopi-p2 && git checkout --detach "$ROLLBACK" && cd deploy && docker compose -p deploy up -d --build \
  && docker compose -p deploy restart proxy
claude mcp remove loopi --scope user
```

The previous brain ignores `mcp_token`, so no restore is needed. Back on P3a: `git -C /home/soul/loopi-p2 checkout feat/phase2`, `up -d --build`, `restart proxy`.

- [ ] **Step 1: Preconditions**

```bash
date '+%F %T %Z'
git -C /home/soul/loopi-p3 status --short                      # nothing
git -C /home/soul/loopi-p2 status --short                      # nothing
git -C /home/soul/loopi-p2 rev-parse --abbrev-ref HEAD         # feat/phase2
git -C /home/soul/loopi-p3 merge-base --is-ancestor feat/phase2 feat/p3a && echo "fast-forward possible"
```

If `feat/phase2` moved (for example P2c Task 12–14 commits), merge it into `feat/p3a` first:

```bash
cd /home/soul/loopi-p3 && git checkout feat/p3a && git merge --no-ff --no-commit feat/phase2
cd brain && uv run pytest -q
cd .. && git commit -F - <<'EOF'
chore: merge feat/phase2 into feat/p3a before the P3a deploy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

- [ ] **Step 2: Ask the owner**

"P3a (MCP) đã qua review. Mình sẽ sao lưu database, đưa `feat/phase2` lên `feat/p3a`, build lại api/worker và khởi động lại Caddy (thiết bị tự gửi lại sau vài giây). Sau đó mình tạo một token, lưu vào `~/.config/loopi/mcp-token` (không in ra), đăng ký server `loopi` trong Claude Code của bạn (`claude mcp add --scope user`) và gọi thử 5 tool. Ảnh qua MCP vẫn tắt. Đồng ý không?" Continue only on yes.

- [ ] **Step 3: Back up, fast-forward, redeploy, smoke test**

```bash
cd /home/soul/loopi-p2/deploy
docker compose -p deploy exec -T db pg_dump -U loopi -Fc loopi > ~/loopi-backups/p2c-$(date +%F-%H%M).dump
ls -l ~/loopi-backups/ | tail -3
ROLLBACK="$(git -C /home/soul/loopi-p2 rev-parse feat/phase2)"; echo "rollback target: $ROLLBACK"   # into the ledger
git -C /home/soul/loopi-p2 merge --ff-only feat/p3a
docker compose -p deploy up -d --build                             # api applies 006_mcp.sql at start
docker compose -p deploy exec -T proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose -p deploy restart proxy                             # the single-file Caddyfile mount needs a restart
docker compose -p deploy ps
COMPOSE_PROJECT_NAME=deploy SMOKE_ANALYSIS_TIMEOUT=600 ./smoke.sh
docker compose -p deploy exec -T db psql -U loopi -d loopi -tAc "SELECT name FROM schema_migration ORDER BY applied_at DESC LIMIT 1"
```

Expected:
- a new, non-empty dump;
- `Valid configuration`;
- every service running and healthy;
- `OK: smoke test passed`;
- `006_mcp.sql`.

- [ ] **Step 4: The route answers through Caddy**

```bash
curl -s -o /dev/null -w '%{http_code}\n' --cacert /home/soul/loopi-data/pki/ca.crt -X POST https://localhost:8443/mcp \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'      # 401
curl -s -o /dev/null -w '%{http_code}\n' --cacert /home/soul/loopi-data/pki/ca.crt https://localhost:8443/settings   # 401 (basic auth)
curl -s -o /dev/null -w '%{http_code}\n' --cacert /home/soul/loopi-data/pki/ca.crt --path-as-is -X POST \
  https://localhost:8443/commitments/../mcp      # 401: the bearer gate, never a web handler
curl -s -o /dev/null -w '%{http_code}\n' --cacert /home/soul/loopi-data/pki/ca.crt -X POST \
  https://localhost:8443/mcp/                    # 401, not a 307 to http://
docker compose -p deploy exec -T api python - <<'PY'
import urllib.request
with urllib.request.urlopen("http://127.0.0.1:8000/settings", timeout=60) as r:
    page = r.read().decode()
print(r.status, 'id="mcp"' in page)
PY
```

Expected: `401`, `401`, `401`, `401`, `200 True`.

- [ ] **Step 5: A token, Claude Code, the five tools**

```bash
umask 077 && mkdir -p ~/.config/loopi
cd /home/soul/loopi-p2/deploy && docker compose -p deploy exec -T api loopi-brain mcp-token create "Claude Code (brain PC)" \
  > ~/.config/loopi/mcp-token && test -s ~/.config/loopi/mcp-token && echo "token saved"
claude mcp add --scope user --transport http loopi https://localhost:8443/mcp \
  --header "Authorization: Bearer $(cat ~/.config/loopi/mcp-token)"
NODE_EXTRA_CA_CERTS=/home/soul/loopi-data/pki/ca.crt claude mcp list | grep loopi       # loopi: … ✓ Connected
NODE_EXTRA_CA_CERTS=/home/soul/loopi-data/pki/ca.crt claude -p --allowedTools \
  "mcp__loopi__search_memories,mcp__loopi__get_timeline,mcp__loopi__get_day_recap,mcp__loopi__get_moment,mcp__loopi__list_commitments" \
  "Call each of the five loopi MCP tools exactly once: search_memories with query 'bữa tối', get_timeline and get_day_recap for yesterday's local date, get_moment with a moment_id from the search (or the timeline), list_commitments with no status. Reply ONLY with a Markdown table: tool name, ok or error, number of items returned. Do not quote, summarise or describe any content."
```

Expected:
- `token saved`;
- `loopi` connected;
- a table of five rows, all `ok`.

Record the table in the ledger. It has no content. If Claude Code's answer quotes any content anyway, do not copy it into the ledger.

Then tell the owner how to use the server in their own sessions. They add `export NODE_EXTRA_CA_CERTS=/home/soul/loopi-data/pki/ca.crt` to their shell profile, and `/mcp` in Claude Code lists the tools.

- [ ] **Step 6: Live checks**

```bash
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep deploy-api
docker compose -p deploy logs --since 15m api 2>&1 | grep -c "bữa tối"            # 0: the question is in no log line
docker compose -p deploy logs --since 15m api 2>&1 | grep "mcp " | tail -5          # tool names, times, counts only
docker compose -p deploy exec -T db psql -U loopi -d loopi -tAc "SELECT task_name, count(*) FROM procrastinate_jobs WHERE status = 'failed' GROUP BY 1"
```

Expected:
- the api's memory grows by tens of MB, not hundreds;
- `0`;
- only `mcp <tool> ok in … s (<n> items)` lines (tool name, time and result count: the line is logged at INFO by the module's own logger, which `serve`'s WARNING root level does not silence);
- no failed jobs.

Run the P2b latency query once (P2b plan Task 14 Step 2) and record it.

- [ ] **Step 7: Ledger, handoff, push question**

- Copy the sanitized ledger to `docs/superpowers/ledgers/p3a-progress.md`.
- Add a P3a row to `docs/HANDOFF.md` §1: plan, state "complete", commit range, "MCP live at /mcp; Claude Code registered as `loopi`; pictures off (D24)".
- Commit on `feat/p3a`, then fast-forward `feat/p3` and `feat/phase2` (docs only, no redeploy).
- Ask the owner before pushing.

```bash
cd /home/soul/loopi-p3 && git add docs/superpowers/ledgers/p3a-progress.md docs/HANDOFF.md && git commit -F - <<'EOF'
docs: P3a ledger and handoff (MCP server live, five tools called from Claude Code)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
git branch -f feat/p3 feat/p3a
git -C /home/soul/loopi-p2 merge --ff-only feat/p3a
```
