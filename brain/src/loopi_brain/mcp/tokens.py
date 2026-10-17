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
