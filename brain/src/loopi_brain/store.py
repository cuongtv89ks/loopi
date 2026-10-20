"""Settings edited on the Cài đặt page, kept in the ``setting`` table; a missing key means the built-in default."""
from __future__ import annotations

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

RETENTION_KEY = "retention"
MAX_DAYS = 3650


class Retention(BaseModel):
    """How many days raw recordings are kept (spec §9.5; D20: audio like video, 7 days by default), and whether the
    nightly deletion runs: **on** by default since D32 (P3c-1, one copy per segment); the owner can change the days
    or turn it off in Cài đặt, which shows what a run would delete."""

    model_config = ConfigDict(extra="forbid")

    video_days: int = Field(default=7, ge=1, le=MAX_DAYS)
    audio_days: int = Field(default=7, ge=1, le=MAX_DAYS)
    enabled: bool = True


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
