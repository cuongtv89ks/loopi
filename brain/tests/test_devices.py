import asyncio
import datetime as dt

import pytest

from helpers import HW, create_token, make_csr_pem, paired_device
from loopi_brain import devices, pki


def _issuer(settings):
    return lambda device_id: pki.sign_csr(settings.pki_dir, make_csr_pem(), device_id)


async def test_pair_token_format(pool):
    token = await create_token(pool)
    assert len(token) == 9 and token[4] == "-"
    assert set(token.replace("-", "")) <= set(devices.TOKEN_ALPHABET)


async def test_register_device_with_valid_token(pool, pki_settings):
    result = await devices.register_device(pool, await create_token(pool), HW, _issuer(pki_settings))
    assert result is not None
    found = await devices.get_active_device(pool, result["fingerprint"])
    assert found["id"] == result["device_id"]


async def test_token_cannot_be_reused(pool, pki_settings):
    token = await create_token(pool)
    assert await devices.register_device(pool, token, HW, _issuer(pki_settings)) is not None
    assert await devices.register_device(pool, token, HW, _issuer(pki_settings)) is None


async def test_expired_token_is_rejected(pool, pki_settings):
    token = (await devices.create_pair_token(pool, dt.timedelta(seconds=-1)))["token"]
    assert await devices.register_device(pool, token, HW, _issuer(pki_settings)) is None


async def test_unknown_token_is_rejected(pool, pki_settings):
    assert await devices.register_device(pool, "ZZZZ-ZZZZ", HW, _issuer(pki_settings)) is None


async def test_concurrent_registration_with_same_token_pairs_once(pool, pki_settings):
    token = await create_token(pool)
    issued = []
    gate = asyncio.Event()

    def issuer(device_id):
        issued.append(device_id)
        return _issuer(pki_settings)(device_id)

    async def attempt():
        await gate.wait()
        return await devices.register_device(pool, token, HW, issuer)

    tasks = [asyncio.create_task(attempt()) for _ in range(5)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert sum(r is not None for r in results) == 1
    assert len(issued) == 1  # losers never reached cert issuance
    assert len(await devices.list_devices(pool)) == 1


async def test_failed_cert_issue_keeps_token_usable(pool, pki_settings):
    token = await create_token(pool)

    def broken(_device_id):
        raise pki.PkiError("bad csr")

    with pytest.raises(pki.PkiError):
        await devices.register_device(pool, token, HW, broken)
    assert await devices.list_devices(pool) == []
    assert await devices.register_device(pool, token, HW, _issuer(pki_settings)) is not None


async def test_get_active_device_ignores_revoked_and_empty(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    assert await devices.get_active_device(pool, "") is None
    assert await devices.get_active_device(pool, "0" * 64) is None
    assert await devices.revoke_device(pool, dev["device_id"]) is True
    assert await devices.get_active_device(pool, dev["fingerprint"]) is None
    assert await devices.revoke_device(pool, "nope") is False


async def test_heartbeat_returns_pending_commands_in_order_and_acks(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    first = await devices.enqueue_command(pool, dev["device_id"], "pause")
    second = await devices.enqueue_command(pool, dev["device_id"], "set_config", {"audio_enabled": False})
    pending = await devices.record_heartbeat(pool, dev["device_id"], {"paused": False}, [])
    assert [c["id"] for c in pending] == [first, second]
    assert pending[1]["payload"] == {"audio_enabled": False}
    pending = await devices.record_heartbeat(pool, dev["device_id"], {"paused": True}, [first])
    assert [c["id"] for c in pending] == [second]


async def test_ack_from_other_device_is_ignored(pool, pki_settings):
    alice = await paired_device(pool, pki_settings)
    bob = await paired_device(pool, pki_settings)
    cmd = await devices.enqueue_command(pool, alice["device_id"], "pause")
    bobs_pending = await devices.record_heartbeat(pool, bob["device_id"], {}, [cmd])
    assert bobs_pending == []
    pending = await devices.record_heartbeat(pool, alice["device_id"], {}, [])
    assert [c["id"] for c in pending] == [cmd]


async def test_heartbeat_accumulates_dropped_total(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    await devices.record_heartbeat(pool, dev["device_id"], {"dropped_items": 2}, [])
    await devices.record_heartbeat(pool, dev["device_id"], {"dropped_items": 3}, [])
    (row,) = await devices.list_devices(pool)
    assert row["dropped_total"] == 5
    assert row["last_heartbeat"] == {"dropped_items": 3}


@pytest.mark.parametrize(
    ("type_", "payload"),
    [
        ("reboot", {}),
        ("pause", {"x": 1}),
        ("set_config", {}),
        ("set_config", {"video_fps": 5}),
        ("set_config", {"audio_enabled": "yes"}),
    ],
)
def test_validate_command_rejects_bad_commands(type_, payload):
    with pytest.raises(ValueError):
        devices.validate_command(type_, payload)
