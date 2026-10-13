"""Shared test helpers; later tasks import from here instead of redefining."""
import datetime as dt

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID


def make_csr_pem(rsa_key: bool = False) -> str:
    key = rsa.generate_private_key(65537, 2048) if rsa_key else ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "loopi-device")]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode()


HW = {"model": "Raspberry Pi Zero W", "serial": "000000001", "agent_version": "0.1.0"}


async def create_token(pool, minutes: int = 5) -> str:
    """Create a one-time pair token valid for ``minutes`` (negative = already expired)."""
    from loopi_brain import devices

    return (await devices.create_pair_token(pool, dt.timedelta(minutes=minutes)))["token"]


async def paired_device(pool, settings) -> dict:
    from loopi_brain import devices, pki

    return await devices.register_device(
        pool,
        await create_token(pool),
        HW,
        lambda device_id: pki.sign_csr(settings.pki_dir, make_csr_pem(), device_id),
    )


def auth_headers(fingerprint: str | None = None, agent_version: str | None = "0.1.0") -> dict:
    """Headers Caddy/the agent send on device requests (omit a header by passing None)."""
    headers = {}
    if fingerprint is not None:
        headers["X-Loopi-Client-Fingerprint"] = fingerprint
    if agent_version is not None:
        headers["X-Loopi-Agent-Version"] = agent_version
    return headers


ITEM_CONTENT_TYPES = {"video": "video/h264", "audio": "audio/ogg", "marker": "application/json"}
ITEM_START = "2026-10-14T01:00:00Z"
ITEM_END = "2026-10-14T01:01:00Z"


def item_headers(
    fingerprint: str | None,
    body: bytes,
    kind: str = "video",
    sha: str | None = None,
    ctype: str | None = None,
    start: str = ITEM_START,
    end: str = ITEM_END,
    **extra: str,
) -> dict:
    """Headers for PUT /v1/items/{id} carrying ``body`` (fingerprint None = header absent)."""
    import hashlib

    headers = {
        "X-Loopi-Kind": kind,
        "X-Loopi-Start": start,
        "X-Loopi-End": end,
        "X-Loopi-Sha256": sha or hashlib.sha256(body).hexdigest(),
        "Content-Type": ctype or ITEM_CONTENT_TYPES.get(kind, "application/octet-stream"),
    }
    if fingerprint is not None:
        headers["X-Loopi-Client-Fingerprint"] = fingerprint
    headers.update(extra)
    return headers
