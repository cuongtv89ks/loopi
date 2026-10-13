from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

from contract import assert_matches
from helpers import HW, auth_headers, create_token, make_csr_pem, paired_device
from loopi_brain import devices, pki
from loopi_brain.api.device_auth import check_agent_version, current_device


async def test_pair_returns_cert_and_ca(client, pool):
    body = {"token": await create_token(pool), "csr_pem": make_csr_pem(), "hw_info": HW}
    assert_matches("PairRequest", body)
    r = await client.post("/v1/pair", json=body)
    assert r.status_code == 201, r.text
    data = r.json()
    assert_matches("PairResponse", data)
    assert data["ca_fingerprint"] == pki.fingerprint(data["ca_cert_pem"])
    cert = x509.load_pem_x509_certificate(data["cert_pem"].encode())
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == data["device_id"]
    assert await devices.get_active_device(pool, pki.fingerprint(data["cert_pem"])) is not None


async def test_pair_token_is_normalised(client, pool):
    token = await create_token(pool)
    r = await client.post(
        "/v1/pair", json={"token": f" {token.lower()} ", "csr_pem": make_csr_pem(), "hw_info": HW}
    )
    assert r.status_code == 201


async def test_pair_with_used_token_is_401(client, pool):
    token = await create_token(pool)
    body = {"token": token, "csr_pem": make_csr_pem(), "hw_info": HW}
    assert (await client.post("/v1/pair", json=body)).status_code == 201
    r = await client.post("/v1/pair", json=body)
    assert r.status_code == 401
    assert_matches("Error", r.json())


async def test_pair_with_bad_csr_is_422_and_token_survives(client, pool):
    token = await create_token(pool)
    r = await client.post(
        "/v1/pair", json={"token": token, "csr_pem": make_csr_pem(rsa_key=True), "hw_info": HW}
    )
    assert r.status_code == 422
    r = await client.post("/v1/pair", json={"token": token, "csr_pem": make_csr_pem(), "hw_info": HW})
    assert r.status_code == 201


async def test_pair_with_missing_fields_is_422(client):
    assert (await client.post("/v1/pair", json={"token": "ABCD-EFGH"})).status_code == 422


def _request(pool, major=0):
    settings = SimpleNamespace(supported_agent_major=major)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool, settings=settings)))


async def test_current_device_rejects_missing_unknown_and_revoked(pool, pki_settings):
    req = _request(pool)
    for fp in [None, "", "  ", "00" * 32]:
        with pytest.raises(HTTPException) as exc:
            await current_device(req, fp)
        assert exc.value.status_code == 401
    dev = await paired_device(pool, pki_settings)
    assert (await current_device(req, dev["fingerprint"].upper()))["id"] == dev["device_id"]
    await devices.revoke_device(pool, dev["device_id"])
    with pytest.raises(HTTPException):
        await current_device(req, dev["fingerprint"])


@pytest.mark.parametrize(("version", "ok"), [(None, True), ("0.1.0", True), ("1.0.0", False), ("x", False)])
def test_check_agent_version(pool, version, ok):
    if ok:
        check_agent_version(_request(pool), version)
    else:
        with pytest.raises(HTTPException) as exc:
            check_agent_version(_request(pool), version)
        assert exc.value.status_code == 426


async def test_pair_with_unsupported_agent_major_is_426_and_keeps_token(client, pool):
    token = await create_token(pool)
    body = {"token": token, "csr_pem": make_csr_pem(), "hw_info": HW}
    r = await client.post("/v1/pair", json=body, headers=auth_headers(agent_version="1.0.0"))
    assert r.status_code == 426
    assert_matches("Error", r.json())
    r = await client.post("/v1/pair", json=body)
    assert r.status_code == 201
