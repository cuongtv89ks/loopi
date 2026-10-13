"""The brain's private CA: server certificate for Caddy and client certificates for devices."""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_CERT = "ca.crt"
CA_KEY = "ca.key"
SERVER_CERT = "server.crt"
SERVER_KEY = "server.key"

CLOCK_SKEW = dt.timedelta(minutes=5)
CA_VALIDITY = dt.timedelta(days=3650)
# Total validity (including the backdated skew) must stay <= 800 days: Apple platforms
# reject TLS server certs > 825 days and the plan's global constraint is 800.
MAX_SERVER_TOTAL_VALIDITY = dt.timedelta(days=800)
SERVER_VALIDITY = MAX_SERVER_TOTAL_VALIDITY - CLOCK_SKEW
DEVICE_VALIDITY = dt.timedelta(days=3650)
CA_NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Loopi Brain CA")])
PRIVATE_KEY_MODE = 0o600
_LEAF_KEY_USAGE = x509.KeyUsage(
    digital_signature=True, content_commitment=False, key_encipherment=False,
    data_encipherment=False, key_agreement=False, key_cert_sign=False,
    crl_sign=False, encipher_only=False, decipher_only=False,
)


class PkiError(Exception):
    """Raised for invalid PKI operations or inputs."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _write_private_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    data = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    # Created with 0600 from the start: no world-readable window.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_KEY_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _builder(subject: x509.Name, issuer: x509.Name, public_key, validity: dt.timedelta):
    now = _now()
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - CLOCK_SKEW)
        .not_valid_after(now + validity)
    )


def init_pki(pki_dir: Path, hostnames: list[str], ips: list[str]) -> None:
    """Create the CA and a server cert for ``hostnames``/``ips``. Refuses to overwrite."""
    sans: list[x509.GeneralName] = [x509.DNSName(h) for h in hostnames]
    try:
        sans += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    except ValueError as exc:
        raise PkiError(f"invalid IP address: {exc}") from exc
    if not sans:
        raise PkiError("at least one --host or --ip is required")
    pki_dir.mkdir(parents=True, exist_ok=True)
    if (pki_dir / CA_CERT).exists():
        raise PkiError(f"PKI already initialised in {pki_dir}")

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        _builder(CA_NAME, CA_NAME, ca_key.public_key(), CA_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    server_key = ec.generate_private_key(ec.SECP256R1())
    common_name = (hostnames or ips)[0]
    server_cert = (
        _builder(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]),
            CA_NAME, server_key.public_key(), SERVER_VALIDITY,
        )
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(_LEAF_KEY_USAGE, critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    _write_private_key(pki_dir / CA_KEY, ca_key)
    (pki_dir / CA_CERT).write_bytes(_pem(ca_cert))
    _write_private_key(pki_dir / SERVER_KEY, server_key)
    # Leaf followed by the CA so TLS clients receive the full chain.
    (pki_dir / SERVER_CERT).write_bytes(_pem(server_cert) + _pem(ca_cert))


def sign_csr(pki_dir: Path, csr_pem: str, device_id: str) -> str:
    """Issue a client-auth certificate with CN=device_id for a P-256 CSR."""
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
    except ValueError as exc:
        raise PkiError("CSR is not valid PEM") from exc
    if not csr.is_signature_valid:
        raise PkiError("CSR signature is invalid")
    public_key = csr.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise PkiError("CSR key must be ECDSA P-256")
    ca_key = serialization.load_pem_private_key((pki_dir / CA_KEY).read_bytes(), password=None)
    cert = (
        _builder(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)]),
            CA_NAME, public_key, DEVICE_VALIDITY,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(_LEAF_KEY_USAGE, critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    return _pem(cert).decode()


def fingerprint(cert_pem: str) -> str:
    """Lowercase hex SHA-256 of the first certificate's DER (matches Caddy's placeholder)."""
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return cert.fingerprint(hashes.SHA256()).hex()


def ca_cert_pem(pki_dir: Path) -> str:
    return (pki_dir / CA_CERT).read_text()
