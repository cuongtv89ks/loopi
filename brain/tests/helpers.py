"""Shared test helpers; later tasks import from here instead of redefining."""
import contextlib
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


@contextlib.contextmanager
def fresh_database(database_url: str, name: str):
    """Create an empty database next to the test database; yield its URL; drop it afterwards."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_conninfo(database_url, dbname=name)
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


DEFAULT_CAPTION = {
    "scene": "A small home office with a desk by a window",
    "activity": "A person is typing on a laptop",
    "people_count": 1,
    "objects": ["laptop", "desk lamp", "mug"],
    "text_seen": "",
    "notable": False,
}
DEFAULT_OLLAMA_MODELS = ["minicpm-v4.6:1b", "qwen3-embedding:0.6b", "qwen3:4b", "qwen3:8b"]


class FakeOllama:
    """In-process stand-in for the Ollama HTTP API, used as an ``httpx.MockTransport`` handler.

    Set ``down = True`` to simulate a stopped model server (every request raises ConnectError), or
    ``error = 500`` to make chat and embed requests fail with that HTTP status.
    """

    def __init__(self, caption: dict | str | None = None, models: list[str] | None = None, dims: int = 1024):
        self.down = False
        self.error: int | None = None
        self.caption = DEFAULT_CAPTION if caption is None else caption
        self.models = list(DEFAULT_OLLAMA_MODELS if models is None else models)
        self.dims = dims
        self.requests: list[tuple[str, dict]] = []
        self.timeouts: list[tuple[str, dict | None]] = []

    def __call__(self, request):
        import json

        import httpx

        if self.down:
            raise httpx.ConnectError("[Errno 111] Connection refused", request=request)
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.requests.append((path, body))
        self.timeouts.append((path, request.extensions.get("timeout")))
        if path in ("/api/chat", "/api/embed"):
            if self.error is not None:
                return httpx.Response(self.error, json={"error": "model runner has unexpectedly stopped"})
            if body["model"] not in self.models:
                return httpx.Response(404, json={"error": f"model '{body['model']}' not found"})
        if path == "/api/chat":
            content = self.caption if isinstance(self.caption, str) else json.dumps(self.caption)
            return httpx.Response(
                200, json={"model": body["model"], "message": {"role": "assistant", "content": content}, "done": True}
            )
        if path == "/api/embed":
            vectors = []
            for i, _text in enumerate(body["input"]):
                v = [0.0] * self.dims
                v[i % self.dims] = 1.0
                vectors.append(v)
            return httpx.Response(200, json={"model": body["model"], "embeddings": vectors})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m, "model": m} for m in self.models]})
        if path == "/api/pull":
            self.models.append(body["model"])
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(404, json={"error": "not found"})

    def client(self):
        import httpx

        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def paths(self) -> list[str]:
        return [p for p, _ in self.requests]
