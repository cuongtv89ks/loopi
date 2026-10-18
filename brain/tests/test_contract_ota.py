"""P4a protocol additions: the update command, the heartbeat's OS and OTA fields, the bundle download path. The contract
and the server must accept and refuse exactly the same things."""
import pytest

import contract
from helpers import auth_headers, paired_device
from loopi_brain import devices
from test_heartbeat_api import HEARTBEAT

ULID = "01JA2B3C4D5E6F7G8H9J0KMNPQ"
UPDATE = {
    "bundle": "loopi-prod-2026.10.21-a1b2c3d.raucb",
    "version": "2026.10.21-a1b2c3d",
    "variant": "prod",
    "sha256": "ab" * 32,
    "size": 75_000_000,
}
OS_FIELDS = {
    "os_version": "2026.10.21-a1b2c3d",
    "os_variant": "dev",
    "boot_slot": "B",
    "ota": {"command_id": ULID, "state": "trying", "version": "2026.10.21-a1b2c3d", "error": None},
}


def test_update_command_is_valid_for_server_and_contract():
    devices.validate_command("update", UPDATE)
    contract.assert_matches("Command", {"id": ULID, "type": "update", "payload": UPDATE})


@pytest.mark.parametrize("override", [
    {"bundle": "../etc/passwd"},
    {"bundle": "loopi-prod-x.tar"},
    {"bundle": "loopi-beta-1.raucb"},
    {"version": "has space"},
    {"version": ""},
    {"variant": "beta"},
    {"sha256": "AB" * 32},
    {"sha256": "ab"},
    {"size": 0},
    {"size": 268_435_457},
    {"size": True},
    {"size": "75"},
    {"extra": 1},
])
def test_bad_update_payloads_are_refused_by_server_and_contract(override):
    payload = {**UPDATE, **override}
    with pytest.raises(ValueError):
        devices.validate_command("update", payload)
    with pytest.raises(Exception):
        contract.assert_matches("Command", {"id": ULID, "type": "update", "payload": payload})


@pytest.mark.parametrize("missing", devices.UPDATE_KEYS)
def test_update_payload_needs_every_key(missing):
    payload = {k: v for k, v in UPDATE.items() if k != missing}
    with pytest.raises(ValueError):
        devices.validate_command("update", payload)
    with pytest.raises(Exception):
        contract.assert_matches("Command", {"id": ULID, "type": "update", "payload": payload})


def test_server_also_checks_that_variant_matches_the_bundle_name():
    """A cross-field rule the JSON schema cannot express."""
    with pytest.raises(ValueError, match="variant"):
        devices.validate_command("update", {**UPDATE, "variant": "dev"})


def test_contract_limits_equal_server_constants():
    schemas = contract._SPEC["components"]["schemas"]
    payload = schemas["UpdateCommand"]["properties"]["payload"]["properties"]
    assert payload["size"]["maximum"] == devices.MAX_BUNDLE_BYTES
    assert schemas["BundleName"]["pattern"] == devices.BUNDLE_RE.pattern
    assert schemas["OsVersion"]["pattern"] == devices.VERSION_RE.pattern
    assert payload["sha256"]["pattern"] == devices.SHA256_RE.pattern
    assert schemas["OtaReport"]["properties"]["error"]["maxLength"] == 500
    assert set(schemas["Command"]["discriminator"]["mapping"]) == devices.COMMAND_TYPES
    assert "/v1/ota/{bundle}" in contract._SPEC["paths"]


async def test_heartbeat_with_os_and_ota_fields_is_accepted(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    body = {**HEARTBEAT, **OS_FIELDS}
    contract.assert_matches("Heartbeat", body)
    r = await client.post("/v1/heartbeat", json=body, headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 200, r.text
    (row,) = await devices.list_devices(pool)
    assert row["last_heartbeat"]["os_version"] == "2026.10.21-a1b2c3d"
    assert row["last_heartbeat"]["boot_slot"] == "B"
    assert row["last_heartbeat"]["ota"]["state"] == "trying"


async def test_heartbeat_without_os_fields_still_works(client, pool, pki_settings):
    """Agents older than P4a send none of them."""
    dev = await paired_device(pool, pki_settings)
    r = await client.post("/v1/heartbeat", json=HEARTBEAT, headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("override", [
    {"os_variant": "beta"},
    {"boot_slot": "C"},
    {"os_version": "has space"},
    {"os_version": "x" * 65},
    {"ota": {**OS_FIELDS["ota"], "state": "done"}},
    {"ota": {**OS_FIELDS["ota"], "error": "x" * 501}},
    {"ota": {**OS_FIELDS["ota"], "extra": 1}},
    {"ota": {**OS_FIELDS["ota"], "command_id": "nope"}},
    {"ota": {"state": "trying"}},
])
async def test_bad_os_and_ota_fields_are_refused_by_server_and_contract(client, pool, pki_settings, override):
    dev = await paired_device(pool, pki_settings)
    body = {**HEARTBEAT, **OS_FIELDS, **override}
    with pytest.raises(Exception):
        contract.assert_matches("Heartbeat", body)
    r = await client.post("/v1/heartbeat", json=body, headers=auth_headers(dev["fingerprint"]))
    assert r.status_code == 422


def test_ota_report_without_command_id_matches_contract():
    """The hand-made first move to A/B slots reports without a command id."""
    body = {**HEARTBEAT, "ota": {"state": "succeeded", "version": "2026.10.21-a1b2c3d", "error": None}}
    contract.assert_matches("Heartbeat", body)
