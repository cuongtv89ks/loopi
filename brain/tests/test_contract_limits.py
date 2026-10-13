"""The contract must declare, and the server must enforce, exactly the same limits."""
import datetime as dt
import html
import json
import re
import socket
import ssl
import threading
import tomllib

import pytest
from cryptography import x509

import contract
from helpers import ITEM_START, auth_headers, item_headers, make_csr_pem, paired_device
from loopi_brain import devices, items, pki
from loopi_brain.config import Settings
from test_heartbeat_api import HEARTBEAT

OP = contract._SPEC["paths"]["/v1/items/{id}"]["put"]
ITEM_ID = "01JA2B3C4D5E6F7G8H9J0KMNPQ"
ULID = "01JA2B3C4D5E6F7G8H9J0KMNP{}"


def test_contract_numbers_equal_server_constants():
    assert OP["x-loopi-max-item-bytes"] == Settings.model_fields["max_item_bytes"].default
    assert OP["x-loopi-max-marker-bytes"] == items.MARKER_MAX_BYTES
    assert OP["x-loopi-max-segment-seconds"] == items.MAX_SEGMENT_DURATION.total_seconds()
    schemas = contract._SPEC["components"]["schemas"]
    ack = schemas["Heartbeat"]["properties"]["acked_command_ids"]["maxItems"]
    assert ack == devices.MAX_HEARTBEAT_COMMANDS
    assert schemas["HeartbeatResponse"]["properties"]["commands"]["maxItems"] == ack
    assert schemas["Heartbeat"]["properties"]["last_error"]["maxLength"] == 500
    content = OP["requestBody"]["content"]
    assert set(content) == set(items.KIND_CONTENT_TYPES.values())
    for number in (OP["x-loopi-max-item-bytes"], OP["x-loopi-max-marker-bytes"]):
        assert str(number) in OP["responses"]["413"]["description"]


@pytest.mark.parametrize("type_, payload", [
    ("pause", {}), ("resume", {}), ("set_config", {"audio_enabled": False}),
])
def test_valid_commands_match_contract(type_, payload):
    devices.validate_command(type_, payload)
    contract.assert_matches("Command", {"id": ULID.format("Q"), "type": type_, "payload": payload})


@pytest.mark.parametrize("type_, payload", [
    ("pause", {"x": 1}), ("set_config", {}), ("set_config", {"audio_enabled": 1}),
    ("set_config", {"audio_enabled": True, "x": 1}), ("set_config", {"other": True}),
])
def test_invalid_commands_rejected_by_server_and_contract(type_, payload):
    with pytest.raises(ValueError):
        devices.validate_command(type_, payload)
    with pytest.raises(Exception):
        contract.assert_matches("Command", {"id": ULID.format("Q"), "type": type_, "payload": payload})


async def test_heartbeat_never_returns_more_commands_than_it_can_ack(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    for _ in range(devices.MAX_HEARTBEAT_COMMANDS + 1):
        await devices.enqueue_command(pool, dev["device_id"], "pause")
    headers = auth_headers(dev["fingerprint"])
    first = (await client.post("/v1/heartbeat", json=HEARTBEAT, headers=headers)).json()
    contract.assert_matches("HeartbeatResponse", first)
    assert len(first["commands"]) == 100
    acked = {**HEARTBEAT, "acked_command_ids": [c["id"] for c in first["commands"]]}
    r = await client.post("/v1/heartbeat", json=acked, headers=headers)
    assert r.status_code == 200
    assert len(r.json()["commands"]) == 1
    last = {**HEARTBEAT, "acked_command_ids": [r.json()["commands"][0]["id"]]}
    r = await client.post("/v1/heartbeat", json=last, headers=headers)
    assert r.json() == {"commands": []}


async def test_heartbeat_limits_accepted_at_edge_rejected_past_it(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    headers = auth_headers(dev["fingerprint"])
    ids = [ULID.format(c) for c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ"] * 4
    for edge, over in (
        ({"acked_command_ids": ids[:100]}, {"acked_command_ids": ids[:101]}),
        ({"last_error": "e" * 500}, {"last_error": "e" * 501}),
    ):
        ok = await client.post("/v1/heartbeat", json={**HEARTBEAT, **edge}, headers=headers)
        assert ok.status_code == 200, ok.text
        bad = await client.post("/v1/heartbeat", json={**HEARTBEAT, **over}, headers=headers)
        assert bad.status_code == 422


def _marker(size: int) -> bytes:
    prefix, suffix = b'{"a":"', b'"}'
    return prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix


async def test_marker_exactly_64kib_accepted_one_more_rejected(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    edge = _marker(items.MARKER_MAX_BYTES)
    assert len(edge) == 65536
    r = await client.put(f"/v1/items/{ITEM_ID}", content=edge, headers=item_headers(
        dev["fingerprint"], edge, kind="marker", end=ITEM_START))
    assert r.status_code == 201, r.text
    over = _marker(items.MARKER_MAX_BYTES + 1)
    r = await client.put("/v1/items/01JA2B3C4D5E6F7G8H9J0KMNPR", content=over, headers=item_headers(
        dev["fingerprint"], over, kind="marker", end=ITEM_START))
    assert r.status_code == 413


async def test_segment_duration_exactly_one_hour_accepted_one_second_more_rejected(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    start = dt.datetime.fromisoformat("2026-10-14T01:00:00+00:00")
    body = b"x"
    for item_id, extra, status in (
        (ITEM_ID, 3600, 201), ("01JA2B3C4D5E6F7G8H9J0KMNPR", 3601, 422),
        ("01JA2B3C4D5E6F7G8H9J0KMNPS", -1, 422),
    ):
        end = (start + dt.timedelta(seconds=extra)).isoformat().replace("+00:00", "Z")
        r = await client.put(f"/v1/items/{item_id}", content=body, headers=item_headers(
            dev["fingerprint"], body, kind="audio", start=ITEM_START, end=end))
        assert r.status_code == status, (extra, r.text)


async def test_pair_token_snippet_is_a_complete_brain_table(client, pki_settings):
    r = await client.post("/devices/pair-token", headers={"HX-Request": "true"})
    block = html.unescape(re.search(r"<pre>(.*?)</pre>", r.text, re.S).group(1))
    brain = tomllib.loads(block)["brain"]
    assert brain["url"] == "https://brain.test"
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", brain["pair_token"])
    assert brain["ca_fingerprint"] == pki.fingerprint(pki.ca_cert_pem(pki_settings.pki_dir))
    assert "[brain]" in r.text and "thay" in r.text


def _tls_handshake(pki_dir) -> str:
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(pki_dir / pki.SERVER_CERT, pki_dir / pki.SERVER_KEY)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    errors: list[Exception] = []

    def serve():
        try:
            conn, _ = listener.accept()
            with server_ctx.wrap_socket(conn, server_side=True) as tls:
                tls.recv(1)
        except Exception as exc:  # the client assertion reports the real failure
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client_ctx = ssl.create_default_context(cafile=str(pki_dir / pki.CA_CERT))
    assert client_ctx.verify_flags & ssl.VERIFY_X509_STRICT
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with client_ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
            tls.send(b"x")
            version = tls.version()
    thread.join(5)
    listener.close()
    return version


def test_server_cert_passes_strict_default_tls_client(pki_settings):
    assert _tls_handshake(pki_settings.pki_dir).startswith("TLSv1")


def _assert_identity_extensions(cert: x509.Certificate, ca: x509.Certificate):
    ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    aki = cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    assert aki.key_identifier == ski.digest
    cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical and usage.value.digital_signature and not usage.value.key_cert_sign


def test_leaf_certificates_carry_ski_aki_and_key_usage(pki_settings):
    pki_dir = pki_settings.pki_dir
    ca = x509.load_pem_x509_certificate((pki_dir / pki.CA_CERT).read_bytes())
    server = x509.load_pem_x509_certificate((pki_dir / pki.SERVER_CERT).read_bytes())
    device = x509.load_pem_x509_certificate(pki.sign_csr(pki_dir, make_csr_pem(), "dev").encode())
    _assert_identity_extensions(server, ca)
    _assert_identity_extensions(device, ca)
