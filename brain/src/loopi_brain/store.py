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
