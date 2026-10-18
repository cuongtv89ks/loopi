"""Device registry: pairing tokens, devices, heartbeats and queued commands."""
from __future__ import annotations

import datetime as dt
import re
import secrets
from collections.abc import Callable

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from ulid import ULID

from . import pki

TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
COMMAND_TYPES = frozenset({"pause", "resume", "set_config", "update"})
SET_CONFIG_KEYS: dict[str, type] = {"audio_enabled": bool}
# Update bundles (P4a design §7; contract BundleName, OsVersion, UpdateCommand).
BUNDLE_RE = re.compile(r"^loopi-(dev|prod)-[0-9A-Za-z.+-]{1,64}\.raucb$")
VERSION_RE = re.compile(r"^[0-9A-Za-z.+-]{1,64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BUNDLE_BYTES = 256 * 1024**2
UPDATE_KEYS = ("bundle", "version", "variant", "sha256", "size")
# One heartbeat returns at most this many commands, and may ack at most this many ids.
MAX_HEARTBEAT_COMMANDS = 100


def new_id() -> str:
    return str(ULID())


def _new_token() -> str:
    raw = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


async def create_pair_token(pool: AsyncConnectionPool, ttl: dt.timedelta) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pair_token (token, expires_at) VALUES (%s, now() + %s)"
            " RETURNING token, expires_at",
            (_new_token(), ttl),
        )
        return await cur.fetchone()


async def _consume_token(conn: AsyncConnection, token: str) -> bool:
    cur = await conn.execute(
        "UPDATE pair_token SET used_at = now()"
        " WHERE token = %s AND used_at IS NULL AND expires_at > now() RETURNING token",
        (token,),
    )
    return await cur.fetchone() is not None


async def register_device(
    pool: AsyncConnectionPool,
    token: str,
    hw_info: dict,
    issue_cert: Callable[[str], str],
) -> dict | None:
    """Consume ``token`` and create a device. Returns None if the token is unusable.

    If ``issue_cert`` raises, the transaction rolls back and the token stays usable.
    """
    device_id = new_id()
    async with pool.connection() as conn:
        async with conn.transaction():
            if not await _consume_token(conn, token):
                return None
            cert_pem = issue_cert(device_id)
            fp = pki.fingerprint(cert_pem)
            await conn.execute(
                "INSERT INTO device (id, name, cert_fingerprint, hw_info) VALUES (%s, %s, %s, %s)",
                (device_id, f"loopi-{device_id[-4:].lower()}", fp, Jsonb(hw_info)),
            )
            await conn.execute(
                "UPDATE pair_token SET device_id = %s WHERE token = %s", (device_id, token)
            )
    return {"device_id": device_id, "cert_pem": cert_pem, "fingerprint": fp}


async def get_active_device(pool: AsyncConnectionPool, fingerprint: str) -> dict | None:
    if not fingerprint:
        return None
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, name FROM device WHERE cert_fingerprint = %s AND NOT revoked",
            (fingerprint,),
        )
        return await cur.fetchone()


async def revoke_device(pool: AsyncConnectionPool, device_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE device SET revoked = TRUE WHERE id = %s RETURNING id", (device_id,)
        )
        return await cur.fetchone() is not None


def _validate_update(payload: dict) -> None:
    if sorted(payload) != sorted(UPDATE_KEYS):
        raise ValueError(f"update needs exactly {', '.join(UPDATE_KEYS)}")
    bundle, version, variant, sha256, size = (payload[key] for key in UPDATE_KEYS)
    if not (isinstance(bundle, str) and BUNDLE_RE.fullmatch(bundle)):
        raise ValueError("update bundle is not a Loopi bundle name")
    if not (isinstance(version, str) and VERSION_RE.fullmatch(version)):
        raise ValueError("update version is malformed")
    if variant not in ("dev", "prod") or bundle != f"loopi-{variant}-{version}.raucb":
        raise ValueError("update variant and version must match the bundle name")
    if not (isinstance(sha256, str) and SHA256_RE.fullmatch(sha256)):
        raise ValueError("update sha256 must be 64 lowercase hex digits")
    if type(size) is not int or not 1 <= size <= MAX_BUNDLE_BYTES:
        raise ValueError(f"update size must be an integer from 1 to {MAX_BUNDLE_BYTES}")


def validate_command(type_: str, payload: dict) -> None:
    if type_ not in COMMAND_TYPES:
        raise ValueError(f"unknown command type {type_!r}")
    if type_ == "update":
        _validate_update(payload)
        return
    if type_ != "set_config":
        if payload:
            raise ValueError(f"{type_} takes no payload")
        return
    if not payload:
        raise ValueError("set_config needs at least one key")
    for key, value in payload.items():
        expected = SET_CONFIG_KEYS.get(key)
        if expected is None:
            raise ValueError(f"set_config key {key!r} is not allowed")
        if not isinstance(value, expected):
            raise ValueError(f"set_config key {key!r} must be {expected.__name__}")


async def enqueue_command(
    pool: AsyncConnectionPool, device_id: str, type_: str, payload: dict | None = None
) -> str:
    payload = payload or {}
    validate_command(type_, payload)
    command_id = new_id()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO device_command (id, device_id, type, payload) VALUES (%s, %s, %s, %s)",
            (command_id, device_id, type_, Jsonb(payload)),
        )
    return command_id


async def record_heartbeat(
    pool: AsyncConnectionPool, device_id: str, status: dict, acked_ids: list[str]
) -> list[dict]:
    """Store the latest status, ack this device's commands, return still-pending ones."""
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE device SET last_heartbeat = %s, last_heartbeat_at = now(),"
                " dropped_total = dropped_total + %s WHERE id = %s",
                (Jsonb(status), int(status.get("dropped_items", 0)), device_id),
            )
            if acked_ids:
                await conn.execute(
                    "UPDATE device_command SET acked_at = now()"
                    " WHERE device_id = %s AND id = ANY(%s) AND acked_at IS NULL",
                    (device_id, acked_ids),
                )
            cur = await conn.execute(
                "SELECT id, type, payload FROM device_command"
                " WHERE device_id = %s AND acked_at IS NULL ORDER BY id LIMIT %s",
                (device_id, MAX_HEARTBEAT_COMMANDS),
            )
            return await cur.fetchall()


async def list_devices(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, name, revoked, hw_info, last_heartbeat, last_heartbeat_at,"
            " dropped_total, created_at FROM device ORDER BY created_at"
        )
        return await cur.fetchall()
