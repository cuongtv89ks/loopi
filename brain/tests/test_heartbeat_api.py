import pytest

from contract import assert_matches
from helpers import auth_headers, paired_device
from loopi_brain import devices

HEARTBEAT = {
    "agent_version": "0.1.0",
    "uptime_s": 120,
    "soc_temp_c": 48.2,
    "disk_used_pct": 12.5,
    "queue_depth": 3,
    "wifi_rssi_dbm": -61,
    "clock_synced": True,
    "paused": False,
    "audio_enabled": True,
    "recording_video": True,
    "recording_audio": True,
    "mic_missing": False,
    "camera_failures": 0,
    "last_error": None,
    "dropped_items": 0,
    "acked_command_ids": [],
}


async def test_heartbeat_stores_status_and_returns_commands(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    cmd = await devices.enqueue_command(pool, dev["device_id"], "pause")
    assert_matches("Heartbeat", HEARTBEAT)
    r = await client.post("/v1/heartbeat", json=HEARTBEAT,
                          headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 200, r.text
    assert_matches("HeartbeatResponse", r.json())
    assert r.json()["commands"] == [{"id": cmd, "type": "pause", "payload": {}}]
    (row,) = await devices.list_devices(pool)
    assert row["last_heartbeat"]["queue_depth"] == 3
    assert "acked_command_ids" not in row["last_heartbeat"]

    acked = {**HEARTBEAT, "acked_command_ids": [cmd]}
    r = await client.post("/v1/heartbeat", json=acked,
                          headers=auth_headers(dev["fingerprint"]))
    assert_matches("HeartbeatResponse", r.json())
    assert r.json() == {"commands": []}


async def test_heartbeat_requires_device(client):
    assert (await client.post("/v1/heartbeat", json=HEARTBEAT)).status_code == 401


@pytest.mark.parametrize(
    "override",
    [
        {"disk_used_pct": 140},
        {"dropped_items": -1},
        {"dropped_items": 1.5},
        {"dropped_items": "3"},
        {"queue_depth": -1},
        {"uptime_s": -5},
        {"camera_failures": -2},
        {"unknown_field": 1},
        {"acked_command_ids": ["not-a-ulid"]},
    ],
)
async def test_heartbeat_rejects_invalid_body_and_records_nothing(
    client, pool, pki_settings, override
):
    dev = await paired_device(pool, pki_settings)
    r = await client.post("/v1/heartbeat", json={**HEARTBEAT, **override},
                          headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 422
    (row,) = await devices.list_devices(pool)
    assert row["last_heartbeat"] is None
    assert row["dropped_total"] == 0


async def test_heartbeat_requires_acked_command_ids(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    body = {k: v for k, v in HEARTBEAT.items() if k != "acked_command_ids"}
    r = await client.post("/v1/heartbeat", json=body, headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 422


async def test_heartbeat_ack_of_other_devices_command_is_ignored(client, pool, pki_settings):
    dev_a = await paired_device(pool, pki_settings)
    dev_b = await paired_device(pool, pki_settings)
    cmd = await devices.enqueue_command(pool, dev_a["device_id"], "pause")

    r = await client.post("/v1/heartbeat", json={**HEARTBEAT, "acked_command_ids": [cmd]},
                          headers=auth_headers(dev_b["fingerprint"]))
    assert r.status_code == 200, r.text
    assert r.json() == {"commands": []}

    r = await client.post("/v1/heartbeat", json=HEARTBEAT,
                          headers=auth_headers(dev_a["fingerprint"]))
    assert_matches("HeartbeatResponse", r.json())
    assert r.json()["commands"] == [{"id": cmd, "type": "pause", "payload": {}}]


async def test_heartbeat_accepts_agent_null_shape(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    body = {**HEARTBEAT, "soc_temp_c": None, "wifi_rssi_dbm": None, "last_error": None}
    assert_matches("Heartbeat", body)
    r = await client.post("/v1/heartbeat", json=body, headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 200, r.text
    assert_matches("HeartbeatResponse", r.json())
    (row,) = await devices.list_devices(pool)
    hb = row["last_heartbeat"]
    assert hb["soc_temp_c"] is None and hb["wifi_rssi_dbm"] is None and hb["last_error"] is None


async def test_heartbeat_omitted_optional_fields_use_defaults(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    optional = {"soc_temp_c", "wifi_rssi_dbm", "mic_missing", "camera_failures",
                "last_error", "dropped_items"}
    body = {k: v for k, v in HEARTBEAT.items() if k not in optional}
    assert_matches("Heartbeat", body)
    r = await client.post("/v1/heartbeat", json=body, headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 200, r.text
    assert_matches("HeartbeatResponse", r.json())
    (row,) = await devices.list_devices(pool)
    hb = row["last_heartbeat"]
    assert hb["mic_missing"] is False
    assert hb["camera_failures"] == 0
    assert hb["dropped_items"] == 0
    assert hb["last_error"] is None
    assert row["dropped_total"] == 0
