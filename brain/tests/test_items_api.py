import hashlib
import json
from pathlib import Path

import pytest

import contract
from helpers import ITEM_END as END
from helpers import ITEM_START as START
from helpers import item_headers, paired_device
from loopi_brain import devices
from loopi_brain.app import create_app

ITEM_ID = "01JA2B3C4D5E6F7G8H9J0KMNPQ"


_headers = item_headers


@pytest.fixture
async def device(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _row(pool, item_id=ITEM_ID):
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM item WHERE id = %s", (item_id,))
        return await cur.fetchone()


async def test_video_upload_is_stored_then_duplicate(client, pool, pki_settings, device):
    body = b"\x00\x00\x00\x01video"
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body,
                         headers=_headers(device["fingerprint"], body, **{"X-Loopi-Video-Fps": "2"}))
    assert r.status_code == 201, r.text
    assert r.json() == {"id": ITEM_ID, "status": "created"}
    contract.assert_matches("ItemAck", r.json())
    row = await _row(pool)
    assert row["status"] == "stored" and row["meta"] == {"video_fps": 2}
    stored = pki_settings.media_root / row["raw_path"]
    assert stored.read_bytes() == body
    assert row["raw_path"] == f"raw/{device['device_id']}/2026/10/14/{ITEM_ID}.h264"
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body))
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    contract.assert_matches("ItemAck", r.json())


async def test_same_id_different_content_is_409(client, device):
    first, second = b"one", b"two"
    assert (await client.put(f"/v1/items/{ITEM_ID}", content=first,
                             headers=_headers(device["fingerprint"], first))).status_code == 201
    r = await client.put(f"/v1/items/{ITEM_ID}", content=second, headers=_headers(device["fingerprint"], second))
    assert r.status_code == 409


async def test_sha_mismatch_leaves_nothing_and_retry_succeeds(client, pool, pki_settings, device):
    body = b"complete-segment"
    truncated = body[:5]
    r = await client.put(f"/v1/items/{ITEM_ID}", content=truncated,
                         headers=_headers(device["fingerprint"], truncated, sha=hashlib.sha256(body).hexdigest()))
    assert r.status_code == 422
    assert await _row(pool) is None
    leftovers = [p for p in Path(pki_settings.media_root).rglob("*") if p.is_file()]
    assert leftovers == []
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body))
    assert r.status_code == 201


async def test_audio_is_ready_immediately(client, pool, device):
    body = b"OggS-fake"
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body, kind="audio"))
    assert r.status_code == 201
    contract.assert_matches("ItemAck", r.json())
    row = await _row(pool)
    assert row["status"] == "ready" and row["view_path"] == row["raw_path"]
    assert row["raw_path"].endswith(".opus")


async def test_marker_meta_is_stored(client, pool, device):
    body = json.dumps({"note": "chìa khoá"}).encode()
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body,
                         headers=_headers(device["fingerprint"], body, kind="marker", start=START, end=START))
    assert r.status_code == 201
    contract.assert_matches("ItemAck", r.json())
    assert (await _row(pool))["meta"] == {"note": "chìa khoá"}


@pytest.mark.parametrize(
    ("item_id", "overrides"),
    [
        ("not-a-ulid", {}),
        (ITEM_ID, {"kind": "photo"}),
        (ITEM_ID, {"ctype": "audio/ogg"}),
        (ITEM_ID, {"start": END, "end": START}),
        (ITEM_ID, {"start": "2026-10-14T01:00:00"}),
        (ITEM_ID, {"sha": "ABC"}),
        (ITEM_ID, {"X-Loopi-Video-Fps": "0"}),
        (ITEM_ID, {"start": "2026-10-14T01:00:00Z", "end": "2026-10-14T03:00:00Z"}),
    ],
)
async def test_invalid_headers_are_422(client, pool, device, item_id, overrides):
    body = b"x"
    r = await client.put(f"/v1/items/{item_id}", content=body, headers=_headers(device["fingerprint"], body, **overrides))
    assert r.status_code == 422
    assert await _row(pool) is None


async def test_bad_marker_json_is_422(client, device):
    body = b"[1, 2]"
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body,
                         headers=_headers(device["fingerprint"], body, kind="marker", end=START))
    assert r.status_code == 422


async def _assert_nothing_stored(pool, pki_settings):
    assert await _row(pool) is None
    media = Path(pki_settings.media_root)
    assert not media.exists() or [p for p in media.rglob("*") if p.is_file()] == []


@pytest.mark.parametrize("fingerprint", [None, "", "ab" * 32])
async def test_missing_empty_or_unknown_fingerprint_is_401(client, pool, pki_settings, fingerprint):
    body = b"x"
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(fingerprint, body))
    assert r.status_code == 401
    await _assert_nothing_stored(pool, pki_settings)


async def test_revoked_device_is_401(client, pool, pki_settings, device):
    body = b"x"
    await devices.revoke_device(pool, device["device_id"])
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body))
    assert r.status_code == 401
    await _assert_nothing_stored(pool, pki_settings)


async def test_old_agent_major_is_426(client, device):
    body = b"x"
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body,
                         headers=_headers(device["fingerprint"], body, **{"X-Loopi-Agent-Version": "1.0.0"}))
    assert r.status_code == 426


async def _client_with(pki_settings, pool, **overrides):
    import httpx

    app = create_app(pki_settings.model_copy(update=overrides), pool=pool)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test")


async def test_low_disk_is_507(pool, pki_settings, device):
    body = b"x"
    async with await _client_with(pki_settings, pool, min_free_bytes=10**18) as c:
        r = await c.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body))
    assert r.status_code == 507


async def test_oversize_body_is_413(pool, pki_settings, device):
    body = b"x" * 2048
    async with await _client_with(pki_settings, pool, max_item_bytes=1024) as c:
        r = await c.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body))
    assert r.status_code == 413
    assert await _row(pool) is None


def _media_files(pki_settings):
    media = Path(pki_settings.media_root)
    return [p for p in media.rglob("*") if p.is_file()] if media.exists() else []


async def test_insert_failure_after_rename_leaves_no_file_or_row(
    client, pool, pki_settings, device, monkeypatch
):
    from loopi_brain import items

    def boom(_meta):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(items, "Jsonb", boom)
    body = b"segment"
    with pytest.raises(RuntimeError):
        await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body))
    assert await _row(pool) is None
    assert _media_files(pki_settings) == []


async def test_store_fsyncs_part_file_then_directory(pool, pki_settings, device, monkeypatch):
    from loopi_brain import items

    body = b"segment"
    header = items.parse_header(
        ITEM_ID, {k.lower(): v for k, v in _headers(device["fingerprint"], body).items()}, 2
    )

    async def chunks():
        yield body

    real = items._fsync_path
    calls = []

    def recording_fsync(path):
        calls.append(path)
        real(path)

    monkeypatch.setattr(items, "_fsync_path", recording_fsync)
    status, _ = await items.store_item(pool, pki_settings, device["device_id"], header, chunks())
    assert status == 201
    # fsync'd the .part file first, then the parent directory after the rename
    assert calls[0].name.endswith(".part") and calls[1].is_dir()


def test_fsync_path_syncs_file_and_directory(tmp_path, monkeypatch):
    from loopi_brain import items

    synced = []
    monkeypatch.setattr(items.os, "fsync", lambda fd: synced.append(fd))
    f = tmp_path / "a"
    f.write_bytes(b"x")
    items._fsync_path(f)
    items._fsync_path(tmp_path)
    assert len(synced) == 2


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (b'{"a": "\xff\xfe"}', 422),
        (b'{"a": "x\\u0000y"}', 422),
        (b'{"\\u0000": 1}', 422),
        (b"[" * 50000, 422),
        (b'{"a": "' + b"x" * 70000 + b'"}', 413),
    ],
    ids=["bad-utf8", "nul-value", "nul-key", "deep-nesting", "oversize"],
)
async def test_hostile_marker_bodies_are_rejected_cleanly(client, pool, pki_settings, device, body, status):
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body,
                         headers=_headers(device["fingerprint"], body, kind="marker", end=START))
    assert r.status_code == status
    assert await _row(pool) is None
    assert _media_files(pki_settings) == []


async def test_trailing_newline_in_id_or_sha_is_422(client, pool, device):
    body = b"x"
    r = await client.put(f"/v1/items/{ITEM_ID}%0A", content=body, headers=_headers(device["fingerprint"], body))
    assert r.status_code == 422
    sha = hashlib.sha256(body).hexdigest() + "\n"
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body, sha=sha))
    assert r.status_code == 422
    assert await _row(pool) is None


class _FailingCommitPool:
    """Wraps a pool; the first transaction rolls back and then raises, as a failed commit would."""

    def __init__(self, pool):
        self._pool = pool
        self.failed = False

    def connection(self):
        outer = self._pool.connection()
        wrapper = self

        class _Conn:
            async def __aenter__(self):
                self.conn = await outer.__aenter__()
                return self

            async def __aexit__(self, *exc):
                return await outer.__aexit__(*exc)

            async def execute(self, *a, **k):
                return await self.conn.execute(*a, **k)

            def transaction(self):
                real = self.conn.transaction()

                class _Tx:
                    async def __aenter__(self):
                        return await real.__aenter__()

                    async def __aexit__(self, et, ev, tb):
                        if et is None and not wrapper.failed:
                            wrapper.failed = True
                            err = RuntimeError("commit failed")
                            await real.__aexit__(RuntimeError, err, None)
                            raise err
                        return await real.__aexit__(et, ev, tb)

                return _Tx()

        return _Conn()


async def test_commit_failure_keeps_file_and_retry_succeeds(pool, pki_settings, device):
    from loopi_brain import items

    body = b"segment-bytes"
    raw = {k.lower(): v for k, v in _headers(device["fingerprint"], body).items()}
    header = items.parse_header(ITEM_ID, raw, 2)

    async def chunks():
        yield body

    flaky = _FailingCommitPool(pool)
    with pytest.raises(RuntimeError, match="commit failed"):
        await items.store_item(flaky, pki_settings, device["device_id"], header, chunks())
    assert await _row(pool) is None
    stored = [p for p in _media_files(pki_settings) if not p.name.endswith(".part")]
    assert len(stored) == 1  # not deleted by a post-lock handler
    assert [p for p in _media_files(pki_settings) if p.name.endswith(".part")] == []

    status, _ = await items.store_item(flaky, pki_settings, device["device_id"], header, chunks())
    assert status == 201
    row = await _row(pool)
    assert (pki_settings.media_root / row["raw_path"]).read_bytes() == body
