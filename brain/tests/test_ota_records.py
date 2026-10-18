"""OTA update records (P4a design §6–§8, spec D27): request, device reports, expiry, bundle deletion."""
import asyncio

import psycopg
import pytest

import contract
from helpers import auth_headers, fetch_all, paired_device, write_bundle
from loopi_brain import devices, ota
from test_heartbeat_api import HEARTBEAT

NAME = "loopi-dev-2026.10.21-a1b2c3d.raucb"
STEM = "loopi-dev-2026.10.21-a1b2c3d"


@pytest.fixture
def ota_dir(pki_settings):
    write_bundle(pki_settings.ota_dir)
    return pki_settings.ota_dir


async def _device(pool, settings):
    return (await paired_device(pool, settings))["device_id"]


async def test_request_queues_an_update_command_and_a_record(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    command_id = await ota.request_update(pool, ota_dir, device, NAME)
    (pending,) = await devices.record_heartbeat(pool, device, {}, [])
    assert pending["id"] == command_id and pending["type"] == "update"
    contract.assert_matches("Command", {**pending, "payload": pending["payload"]})
    assert pending["payload"]["bundle"] == NAME and pending["payload"]["size"] == len(b"rauc-bundle-bytes")
    (row,) = await fetch_all(pool, "SELECT * FROM ota_update")
    assert (row["id"], row["device_id"], row["state"], row["version"], row["variant"]) == \
        (command_id, device, "requested", "2026.10.21-a1b2c3d", "dev")
    assert row["finished_at"] is None


@pytest.mark.parametrize("bundle", ["loopi-dev-unknown.raucb", "", "../x"])
async def test_unknown_bundle_is_refused(pool, pki_settings, ota_dir, bundle):
    device = await _device(pool, pki_settings)
    with pytest.raises(ota.UpdateRefused, match="Không tìm thấy"):
        await ota.request_update(pool, ota_dir, device, bundle)


async def test_unknown_device_is_a_lookup_error(pool, pki_settings, ota_dir):
    with pytest.raises(LookupError):
        await ota.request_update(pool, ota_dir, "01JA2B3C4D5E6F7G8H9J0KMNPQ", NAME)


async def test_revoked_device_is_refused(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    await devices.revoke_device(pool, device)
    with pytest.raises(ota.UpdateRefused, match="thu hồi"):
        await ota.request_update(pool, ota_dir, device, NAME)


async def test_one_unfinished_update_per_device(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    await ota.request_update(pool, ota_dir, device, NAME)
    with pytest.raises(ota.UpdateRefused, match="chưa xong"):
        await ota.request_update(pool, ota_dir, device, NAME)
    assert len(await fetch_all(pool, "SELECT id FROM device_command WHERE type = 'update'")) == 1


async def test_concurrent_requests_make_one_update(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    results = await asyncio.gather(
        *(ota.request_update(pool, ota_dir, device, NAME) for _ in range(4)), return_exceptions=True)
    assert sum(isinstance(r, str) for r in results) == 1
    assert all(isinstance(r, (str, ota.UpdateRefused)) for r in results)
    assert len(await fetch_all(pool, "SELECT id FROM device_command WHERE type = 'update'")) == 1
    assert len(await fetch_all(pool, "SELECT id FROM ota_update")) == 1


async def test_reports_move_the_record(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    command_id = await ota.request_update(pool, ota_dir, device, NAME)
    await ota.record_report(pool, device, {"command_id": command_id, "state": "downloading", "version": "v", "error": None})
    (row,) = await fetch_all(pool, "SELECT state, finished_at FROM ota_update")
    assert row == {"state": "downloading", "finished_at": None}
    await ota.record_report(pool, device, {"command_id": command_id, "state": "succeeded", "version": "v", "error": None})
    (row,) = await fetch_all(pool, "SELECT state, finished_at FROM ota_update")
    assert row["state"] == "succeeded" and row["finished_at"] is not None
    # finished: a new update may start
    await ota.request_update(pool, ota_dir, device, NAME)


async def test_reports_never_touch_another_devices_update(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    other = await _device(pool, pki_settings)
    command_id = await ota.request_update(pool, ota_dir, device, NAME)
    await ota.record_report(pool, other, {"command_id": command_id, "state": "failed", "version": "v", "error": "x"})
    await ota.record_report(pool, device, {"command_id": None, "state": "failed", "version": "v", "error": "x"})
    await ota.record_report(pool, device, {"state": "failed", "version": "v", "error": "x"})
    (row,) = await fetch_all(pool, "SELECT state FROM ota_update")
    assert row["state"] == "requested"


async def _age(pool, minutes: int) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE ota_update SET updated_at = now() - make_interval(mins => %s)", (minutes,))


async def test_silent_updates_expire_after_30_minutes(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    await ota.request_update(pool, ota_dir, device, NAME)
    await _age(pool, 29)
    assert await ota.expire_stale(pool) == []
    await _age(pool, 31)
    (expired,) = await ota.expire_stale(pool)
    (row,) = await fetch_all(pool, "SELECT id, state, error, finished_at FROM ota_update")
    assert (row["id"], row["state"], row["error"]) == (expired, "failed", ota.TIMEOUT_ERROR)
    assert row["finished_at"] is not None


async def test_expired_update_is_never_delivered(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    await ota.request_update(pool, ota_dir, device, NAME)
    await _age(pool, 31)
    await ota.expire_stale(pool)
    assert await devices.record_heartbeat(pool, device, {}, []) == []


async def test_an_acked_command_is_kept_on_expiry(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    command_id = await ota.request_update(pool, ota_dir, device, NAME)
    await devices.record_heartbeat(pool, device, {}, [command_id])
    await _age(pool, 31)
    await ota.expire_stale(pool)
    assert len(await fetch_all(pool, "SELECT id FROM device_command WHERE acked_at IS NOT NULL")) == 1


async def test_a_late_report_revives_an_expired_update(pool, pki_settings, ota_dir):
    """The device is the source of truth."""
    device = await _device(pool, pki_settings)
    command_id = await ota.request_update(pool, ota_dir, device, NAME)
    await _age(pool, 31)
    await ota.expire_stale(pool)
    await ota.record_report(pool, device, {"command_id": command_id, "state": "installing", "version": "v", "error": None})
    (row,) = await fetch_all(pool, "SELECT state, error, finished_at FROM ota_update")
    assert row == {"state": "installing", "error": None, "finished_at": None}


async def test_a_late_report_does_not_revive_over_a_newer_update(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    first = await ota.request_update(pool, ota_dir, device, NAME)
    await _age(pool, 31)
    await ota.expire_stale(pool)
    second = await ota.request_update(pool, ota_dir, device, NAME)
    await ota.record_report(pool, device, {"command_id": first, "state": "installing", "version": "v", "error": None})
    rows = {r["id"]: r for r in await fetch_all(pool, "SELECT id, state, finished_at FROM ota_update")}
    assert rows[first]["state"] == "failed" and rows[first]["finished_at"] is not None
    assert rows[second]["state"] == "requested"
    # and a final word about the old one still lands
    await ota.record_report(pool, device, {"command_id": first, "state": "succeeded", "version": "v", "error": None})
    rows = {r["id"]: r for r in await fetch_all(pool, "SELECT id, state FROM ota_update")}
    assert rows[first]["state"] == "succeeded"


async def test_heartbeat_records_the_ota_report(client, pool, pki_settings, ota_dir):
    dev = await paired_device(pool, pki_settings)
    command_id = await ota.request_update(pool, ota_dir, dev["device_id"], NAME)
    body = {**HEARTBEAT, "acked_command_ids": [command_id],
            "ota": {"command_id": command_id, "state": "rebooting", "version": "2026.10.21-a1b2c3d", "error": None}}
    r = await client.post("/v1/heartbeat", json=body, headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 200, r.text
    (row,) = await fetch_all(pool, "SELECT state FROM ota_update")
    assert row["state"] == "rebooting"


async def test_heartbeat_expires_a_stale_update_before_delivering_it(client, pool, pki_settings, ota_dir):
    """A device that returns after hours must not be handed an update the brain gave up on (nobody called expire_stale)."""
    dev = await paired_device(pool, pki_settings)
    command_id = await ota.request_update(pool, ota_dir, dev["device_id"], NAME)
    await _age(pool, 31)
    r = await client.post("/v1/heartbeat", json=HEARTBEAT, headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 200, r.text
    assert r.json()["commands"] == []
    (row,) = await fetch_all(pool, "SELECT id, state, error, finished_at FROM ota_update")
    assert (row["id"], row["state"], row["error"]) == (command_id, "failed", ota.TIMEOUT_ERROR)
    assert row["finished_at"] is not None
    assert await fetch_all(pool, "SELECT id FROM device_command WHERE type = 'update'") == []


async def test_heartbeat_with_a_fresh_report_does_not_expire_that_update(client, pool, pki_settings, ota_dir):
    """The device's own report refreshes updated_at first, so a live device is never expired by its own heartbeat."""
    dev = await paired_device(pool, pki_settings)
    command_id = await ota.request_update(pool, ota_dir, dev["device_id"], NAME)
    await _age(pool, 31)
    body = {**HEARTBEAT, "acked_command_ids": [command_id],
            "ota": {"command_id": command_id, "state": "downloading", "version": "2026.10.21-a1b2c3d", "error": None}}
    r = await client.post("/v1/heartbeat", json=body, headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 200, r.text
    (row,) = await fetch_all(pool, "SELECT state, error, finished_at FROM ota_update")
    assert row == {"state": "downloading", "error": None, "finished_at": None}
    # never expired and revived: the command the device just acked is still on record, acked
    (cmd,) = await fetch_all(pool, "SELECT id, acked_at FROM device_command WHERE type = 'update'")
    assert cmd["id"] == command_id and cmd["acked_at"] is not None


async def test_recent_updates_are_per_device_newest_first(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    ids = []
    for _ in range(7):
        command_id = await ota.request_update(pool, ota_dir, device, NAME)
        await ota.record_report(pool, device, {"command_id": command_id, "state": "failed", "version": "v", "error": "x"})
        ids.append(command_id)
    other = await _device(pool, pki_settings)
    history = await ota.recent_updates(pool, [device, other])
    assert [row["id"] for row in history[device]] == list(reversed(ids))[:5]
    assert history[other] == []
    assert await ota.recent_updates(pool, []) == {}


async def test_delete_removes_bundle_and_description(pool, pki_settings, ota_dir):
    await ota.delete_bundle(pool, ota_dir, STEM)
    assert list(ota_dir.iterdir()) == []


async def test_delete_is_refused_while_an_update_uses_the_bundle(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    await ota.request_update(pool, ota_dir, device, NAME)
    with pytest.raises(ota.DeleteRefused, match="cập nhật"):
        await ota.delete_bundle(pool, ota_dir, STEM)
    assert (ota_dir / NAME).exists()


async def test_delete_is_refused_while_a_device_runs_that_version(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    await devices.record_heartbeat(pool, device, {"os_version": "2026.10.21-a1b2c3d", "os_variant": "dev"}, [])
    with pytest.raises(ota.DeleteRefused, match="đang chạy"):
        await ota.delete_bundle(pool, ota_dir, STEM)
    # the same version in the other variant is another bundle
    await devices.record_heartbeat(pool, device, {"os_version": "2026.10.21-a1b2c3d", "os_variant": "prod"}, [])
    await ota.delete_bundle(pool, ota_dir, STEM)


async def test_revoked_devices_do_not_block_a_delete(pool, pki_settings, ota_dir):
    device = await _device(pool, pki_settings)
    await devices.record_heartbeat(pool, device, {"os_version": "2026.10.21-a1b2c3d", "os_variant": "dev"}, [])
    await devices.revoke_device(pool, device)
    await ota.delete_bundle(pool, ota_dir, STEM)


async def test_unusable_files_can_be_deleted(pool, pki_settings, ota_dir):
    (ota_dir / "loopi-prod-orphan.raucb").write_bytes(b"x")
    await ota.delete_bundle(pool, ota_dir, "loopi-prod-orphan")
    assert not (ota_dir / "loopi-prod-orphan.raucb").exists()


@pytest.mark.parametrize("stem", ["../loopi-dev-x", "loopi-dev-missing", "passwd", ""])
async def test_delete_of_unknown_or_odd_names_is_a_lookup_error(pool, pki_settings, ota_dir, stem):
    with pytest.raises(LookupError):
        await ota.delete_bundle(pool, ota_dir, stem)


async def test_command_table_accepts_update_and_still_refuses_unknown_types(pool, pki_settings):
    device = await _device(pool, pki_settings)
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                "INSERT INTO device_command (id, device_id, type) VALUES ('X', %s, 'reboot')", (device,))
