import datetime as dt
import hashlib

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from helpers import make_csr_pem
from loopi_brain import pki


def _load(path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def test_init_pki_creates_ca_and_server_cert(tmp_path):
    pki.init_pki(tmp_path, ["brain.lan"], ["192.168.1.10"])
    ca = _load(tmp_path / pki.CA_CERT)
    server = _load(tmp_path / pki.SERVER_CERT)
    server.verify_directly_issued_by(ca)
    san = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["brain.lan"]
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["192.168.1.10"]
    assert server.not_valid_after_utc - server.not_valid_before_utc <= dt.timedelta(days=800)
    assert (tmp_path / pki.CA_KEY).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / pki.SERVER_KEY).stat().st_mode & 0o777 == 0o600


def test_init_pki_refuses_to_overwrite(tmp_path):
    pki.init_pki(tmp_path, ["brain.lan"], [])
    with pytest.raises(pki.PkiError):
        pki.init_pki(tmp_path, ["brain.lan"], [])


def test_init_pki_requires_a_name(tmp_path):
    with pytest.raises(pki.PkiError):
        pki.init_pki(tmp_path, [], [])
    assert not (tmp_path / pki.CA_CERT).exists()


def test_sign_csr_issues_client_cert_for_device(tmp_path):
    pki.init_pki(tmp_path, ["brain.lan"], [])
    pem = pki.sign_csr(tmp_path, make_csr_pem(), "01JA0000000000000000000000")
    cert = x509.load_pem_x509_certificate(pem.encode())
    cert.verify_directly_issued_by(_load(tmp_path / pki.CA_CERT))
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "01JA0000000000000000000000"
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in eku


def test_sign_csr_rejects_garbage(tmp_path):
    pki.init_pki(tmp_path, ["brain.lan"], [])
    with pytest.raises(pki.PkiError):
        pki.sign_csr(tmp_path, "not a csr", "D1")


def test_sign_csr_rejects_non_p256_key(tmp_path):
    pki.init_pki(tmp_path, ["brain.lan"], [])
    with pytest.raises(pki.PkiError):
        pki.sign_csr(tmp_path, make_csr_pem(rsa_key=True), "D1")


def test_fingerprint_is_sha256_of_der(tmp_path):
    pki.init_pki(tmp_path, ["brain.lan"], [])
    pem = pki.ca_cert_pem(tmp_path)
    der = x509.load_pem_x509_certificate(pem.encode()).public_bytes(serialization.Encoding.DER)
    assert pki.fingerprint(pem) == hashlib.sha256(der).hexdigest()
