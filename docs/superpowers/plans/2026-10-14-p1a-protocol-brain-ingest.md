# P1a — Sync Protocol v1 + Brain Ingest & Web Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the device↔brain contract (`docs/protocol/v1.openapi.yaml`) and a brain that pairs devices with a private CA, ingests video/audio/marker segments over mTLS, remuxes video for the browser, queues pause/resume/audio commands, and shows a raw per-day timeline and a devices page.

**Architecture:** One Python package `loopi_brain` (FastAPI, async psycopg 3 on Postgres 17, `cryptography` for the CA). Caddy terminates TLS: device routes `/v1/*` use optional client certs and forward the cert's SHA-256 fingerprint in `X-Loopi-Client-Fingerprint`; every other route sits behind HTTP basic auth and has that header stripped. Media files live on disk; Postgres holds metadata only.

**Tech Stack:** Python 3.13, uv, FastAPI, uvicorn, psycopg 3 + psycopg-pool, cryptography, Jinja2 + HTMX 2.0.4, Postgres 17 (`pgvector/pgvector:pg17` image), Caddy 2.10, ffmpeg, pytest + pytest-asyncio + testcontainers, jsonschema.

**Spec:** `docs/superpowers/specs/2026-10-14-loopi-design.md` (§4, §8, §9.1, §9.4, §10.6, §12, §14 P1)

**Phase 1 is split into three plans, executed in order:** P1a (this) → P1b agent v0 (`2026-10-14-p1b-agent-v0.md`) → P1c Buildroot dev image + P1 acceptance (`2026-10-14-p1c-buildroot-dev-image.md`).

## Global Constraints

- Python `>=3.13`, dependencies managed by `uv` and locked in `brain/uv.lock`.
- Postgres 17 via image `pgvector/pgvector:pg17` (same image in tests and compose).
- All timestamps are stored as `timestamptz` in UTC; the web UI renders in `LOOPI_TZ` (default `Asia/Seoul`).
- `docs/protocol/v1.openapi.yaml` is the single source of truth for device endpoints; responses must validate against it (contract tests).
- A device is identified **only** by `X-Loopi-Client-Fingerprint` (lowercase hex SHA-256 of the client cert DER), set by Caddy. Empty, unknown or revoked → `401`.
- All keys are ECDSA P-256. Server cert validity ≤ 800 days (Apple rejects TLS server certs > 825 days).
- Pair tokens are one-time, format `XXXX-XXXX` from alphabet `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, default TTL 30 minutes.
- Web POST endpoints require header `HX-Request: true` (CSRF guard); otherwise `403`.
- No cloud calls in P1; raw media never leaves the brain.
- User-facing UI text is Vietnamese.
- Test coverage of `loopi_brain` ≥ 80%.
- Commits use conventional commit messages (`feat:`, `test:`, `chore:`, `docs:`).

## Review Focus

1. A LAN client **without** a client certificate sends a forged `X-Loopi-Client-Fingerprint` → must get `401` (app rejects empty/unknown/revoked; Caddy overwrites the header). Tests: Task 4 (`test_current_device_rejects_missing_unknown_and_revoked`), Task 9 smoke script (spoof step).
2. An upload whose body is truncated or corrupted in transit (sha256 mismatch) → `422`, no DB row, no file or `.part` leftovers, and an immediate retry with the full body succeeds. Test: Task 5 (`test_sha_mismatch_leaves_nothing_and_retry_succeeds`).
3. The same pair token submitted twice concurrently → exactly one device is registered. Test: Task 3 (`test_concurrent_registration_with_same_token_pairs_once`).
4. A segment recorded at 23:30 UTC must appear under the **next** local day for `Asia/Seoul`, not the UTC day. Test: Task 8 (`test_timeline_uses_local_day_boundaries`).
5. A heartbeat that acks a command id belonging to another device must not ack it. Test: Task 3 (`test_ack_from_other_device_is_ignored`).

---

## File Structure

```
.gitignore
docs/protocol/v1.openapi.yaml           # contract (Task 4)
brain/
├── pyproject.toml                      # Task 1
├── Dockerfile                          # Task 9
├── src/loopi_brain/
│   ├── __init__.py
│   ├── config.py                       # Settings (env LOOPI_*)
│   ├── db.py                           # migrate(), make_pool()
│   ├── migrations/__init__.py
│   ├── migrations/001_init.sql
│   ├── pki.py                          # CA, server cert, CSR signing, fingerprints
│   ├── devices.py                      # tokens, devices, heartbeats, commands
│   ├── items.py                        # header validation + streaming ingest
│   ├── media.py                        # ffmpeg remux to MP4
│   ├── app.py                          # create_app()
│   ├── cli.py                          # init-pki, migrate, serve, pair-token
│   ├── api/__init__.py
│   ├── api/device_auth.py              # current_device, check_agent_version
│   ├── api/device_routes.py            # /v1/pair, /v1/items/{id}, /v1/heartbeat
│   └── web/
│       ├── __init__.py
│       ├── routes.py                   # timeline, devices, commands, pair token
│       ├── static/htmx.min.js
│       └── templates/{base,timeline,devices,_pair_token,_queued}.html
└── tests/
    ├── conftest.py  helpers.py  contract.py
    └── test_*.py
deploy/
├── docker-compose.yml  Caddyfile  .env.example  smoke.sh  README.md
```

---

### Task 1: Brain scaffold, settings and database schema

**Files:**
- Create: `.gitignore`, `brain/pyproject.toml`, `brain/src/loopi_brain/__init__.py`, `brain/src/loopi_brain/config.py`, `brain/src/loopi_brain/db.py`, `brain/src/loopi_brain/migrations/__init__.py`, `brain/src/loopi_brain/migrations/001_init.sql`
- Test: `brain/tests/conftest.py`, `brain/tests/test_db.py`

**Interfaces:**
- Produces: `Settings` (fields `database_url, media_root, pki_dir, tz, min_free_bytes, max_item_bytes, pair_token_ttl_minutes, supported_agent_major, default_video_fps`; property `zone -> ZoneInfo`); `migrate(database_url: str) -> list[str]`; `make_pool(database_url: str) -> AsyncConnectionPool` (rows as dicts, not opened). Fixtures `database_url`, `pool`, `settings`.

- [ ] **Step 1: Create the repo-level `.gitignore`**

```gitignore
# brain
brain/.venv/
**/__pycache__/
.pytest_cache/
.coverage
# agent
agent/target/
# os
os/out/
# deploy secrets
deploy/.env
```

- [ ] **Step 2: Create `brain/pyproject.toml`**

```toml
[project]
name = "loopi-brain"
version = "0.1.0"
description = "Loopi brain: ingest, storage and web UI for the Loopi lifelog device"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "psycopg[binary]>=3.2",
    "psycopg-pool>=3.2",
    "cryptography>=43",
    "pydantic-settings>=2.6",
    "jinja2>=3.1",
    "python-multipart>=0.0.17",
    "python-ulid>=3.0",
    "anyio>=4.6",
]

[project.scripts]
loopi-brain = "loopi_brain.cli:main"

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "pytest-cov>=6.0",
    "httpx>=0.28",
    "testcontainers[postgres]>=4.8",
    "pyyaml>=6.0",
    "jsonschema>=4.23",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/loopi_brain"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
```

- [ ] **Step 3: Write the failing test `brain/tests/test_db.py`**

```python
import psycopg
import pytest

from loopi_brain.db import migrate

EXPECTED_TABLES = {"device", "pair_token", "item", "device_command", "schema_migration"}


def test_migrate_is_idempotent_and_creates_schema(database_url):
    # The session fixture already migrated; a second run applies nothing.
    assert migrate(database_url) == []
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    assert EXPECTED_TABLES <= {r[0] for r in rows}


def test_item_end_before_start_is_rejected(database_url):
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO device (id, name, cert_fingerprint) VALUES ('D-order', 'd', 'fp-order')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256,
                                     size_bytes, raw_path, status)
                   VALUES ('I-order', 'D-order', 'video', '2026-10-14T10:01:00Z',
                           '2026-10-14T10:00:00Z', 'x', 1, 'raw/x', 'stored')"""
            )
        conn.rollback()
```

- [ ] **Step 4: Write `brain/tests/conftest.py`**

```python
import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from loopi_brain.config import Settings
from loopi_brain.db import make_pool, migrate

PG_IMAGE = "pgvector/pgvector:pg17"
APP_TABLES = "device_command, item, pair_token, device"


@pytest.fixture(scope="session")
def database_url():
    with PostgresContainer(PG_IMAGE, driver=None) as pg:
        url = pg.get_connection_url()
        migrate(url)
        yield url


@pytest.fixture
async def pool(database_url):
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {APP_TABLES} CASCADE")
    p = make_pool(database_url)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def settings(database_url, tmp_path):
    return Settings(
        database_url=database_url,
        media_root=tmp_path / "media",
        pki_dir=tmp_path / "pki",
        min_free_bytes=0,
    )
```

- [ ] **Step 5: Run the test to verify it fails**

Run: `cd brain && uv sync && uv run pytest tests/test_db.py -v`
Expected: FAIL / collection error `ModuleNotFoundError: No module named 'loopi_brain.db'` (Docker must be running for testcontainers).

- [ ] **Step 6: Implement `config.py`, `db.py`, `__init__.py` files and the migration**

`brain/src/loopi_brain/__init__.py` and `brain/src/loopi_brain/migrations/__init__.py`: empty files.

`brain/src/loopi_brain/config.py`:

```python
"""Runtime settings, read from LOOPI_* environment variables."""
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOOPI_")

    database_url: str
    media_root: Path = Path("/srv/loopi/media")
    pki_dir: Path = Path("/srv/loopi/pki")
    tz: str = "Asia/Seoul"
    min_free_bytes: int = 2 * 1024**3
    max_item_bytes: int = 64 * 1024**2
    pair_token_ttl_minutes: int = 30
    supported_agent_major: int = 0
    default_video_fps: int = 2

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)
```

`brain/src/loopi_brain/db.py`:

```python
"""Database access: SQL migrations and the async connection pool."""
from importlib import resources

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

MIGRATIONS_PACKAGE = "loopi_brain.migrations"


def migrate(database_url: str) -> list[str]:
    """Apply pending ``migrations/*.sql`` files in name order; return the names applied."""
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT name FROM schema_migration")}
        files = sorted(
            (f for f in resources.files(MIGRATIONS_PACKAGE).iterdir() if f.name.endswith(".sql")),
            key=lambda f: f.name,
        )
        for sql_file in files:
            if sql_file.name in done:
                continue
            with conn.transaction():
                conn.execute(sql_file.read_text())
                conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (sql_file.name,))
            applied.append(sql_file.name)
    return applied


def make_pool(database_url: str) -> AsyncConnectionPool:
    """Create a closed pool; callers open it (app lifespan or test fixture)."""
    return AsyncConnectionPool(
        database_url, open=False, min_size=1, max_size=10, kwargs={"row_factory": dict_row}
    )
```

`brain/src/loopi_brain/migrations/001_init.sql`:

```sql
CREATE TABLE device (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    cert_fingerprint   TEXT NOT NULL UNIQUE,
    hw_info            JSONB NOT NULL DEFAULT '{}'::jsonb,
    revoked            BOOLEAN NOT NULL DEFAULT FALSE,
    last_heartbeat     JSONB,
    last_heartbeat_at  TIMESTAMPTZ,
    dropped_total      BIGINT NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE pair_token (
    token       TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    device_id   TEXT REFERENCES device(id)
);

CREATE TABLE item (
    id           TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL REFERENCES device(id),
    kind         TEXT NOT NULL CHECK (kind IN ('video', 'audio', 'marker')),
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ NOT NULL,
    sha256       TEXT NOT NULL,
    size_bytes   BIGINT NOT NULL,
    raw_path     TEXT NOT NULL,
    view_path    TEXT,
    meta         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status       TEXT NOT NULL CHECK (status IN ('stored', 'ready', 'error')),
    error        TEXT,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT item_time_order CHECK (ended_at >= started_at)
);
CREATE INDEX item_started_at ON item (started_at);

CREATE TABLE device_command (
    id          TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES device(id),
    type        TEXT NOT NULL CHECK (type IN ('pause', 'resume', 'set_config')),
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    acked_at    TIMESTAMPTZ
);
CREATE INDEX device_command_pending ON device_command (device_id, id) WHERE acked_at IS NULL;
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_db.py -v`
Expected: 2 passed.

- [ ] **Step 8: Commit**

```bash
git add .gitignore brain/pyproject.toml brain/uv.lock brain/src brain/tests
git commit -m "feat(brain): scaffold package, settings and initial schema"
```

---

### Task 2: Private CA, server certificate and CSR signing

**Files:**
- Create: `brain/src/loopi_brain/pki.py`, `brain/src/loopi_brain/cli.py`, `brain/tests/helpers.py`
- Test: `brain/tests/test_pki.py`, `brain/tests/test_cli.py`

**Interfaces:**
- Produces: constants `CA_CERT="ca.crt"`, `CA_KEY="ca.key"`, `SERVER_CERT="server.crt"`, `SERVER_KEY="server.key"`; `class PkiError(Exception)`; `init_pki(pki_dir: Path, hostnames: list[str], ips: list[str]) -> None`; `sign_csr(pki_dir: Path, csr_pem: str, device_id: str) -> str` (PEM); `fingerprint(cert_pem: str) -> str` (lowercase hex SHA-256 of DER); `ca_cert_pem(pki_dir: Path) -> str`; `cli.main(argv: list[str] | None = None) -> int` with subcommand `init-pki --pki-dir --host --ip`; test helper `make_csr_pem(rsa_key: bool = False) -> str`.

- [ ] **Step 1: Write test helper `brain/tests/helpers.py`**

```python
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
```

- [ ] **Step 2: Write the failing tests `brain/tests/test_pki.py`**

```python
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
    assert (server.not_valid_after_utc - server.not_valid_before_utc).days <= 825
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
```

`brain/tests/test_cli.py`:

```python
from loopi_brain import cli


def test_init_pki_command_prints_ca_fingerprint(tmp_path, capsys):
    assert cli.main(["init-pki", "--pki-dir", str(tmp_path), "--host", "brain.lan"]) == 0
    assert "CA fingerprint:" in capsys.readouterr().out
    assert cli.main(["init-pki", "--pki-dir", str(tmp_path), "--host", "brain.lan"]) == 1
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_pki.py tests/test_cli.py -v`
Expected: FAIL with `ImportError: cannot import name 'pki'`.

- [ ] **Step 4: Implement `brain/src/loopi_brain/pki.py`**

```python
"""The brain's private CA: server certificate for Caddy and client certificates for devices."""
from __future__ import annotations

import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_CERT = "ca.crt"
CA_KEY = "ca.key"
SERVER_CERT = "server.crt"
SERVER_KEY = "server.key"

CA_VALIDITY = dt.timedelta(days=3650)
SERVER_VALIDITY = dt.timedelta(days=800)  # Apple platforms reject TLS server certs > 825 days
DEVICE_VALIDITY = dt.timedelta(days=3650)
CLOCK_SKEW = dt.timedelta(minutes=5)
CA_NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Loopi Brain CA")])


class PkiError(Exception):
    """Raised for invalid PKI operations or inputs."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _write_private_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)


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
        .sign(ca_key, hashes.SHA256())
    )

    _write_private_key(pki_dir / CA_KEY, ca_key)
    (pki_dir / CA_CERT).write_bytes(_pem(ca_cert))
    _write_private_key(pki_dir / SERVER_KEY, server_key)
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
        .sign(ca_key, hashes.SHA256())
    )
    return _pem(cert).decode()


def fingerprint(cert_pem: str) -> str:
    """Lowercase hex SHA-256 of the first certificate's DER (matches Caddy's placeholder)."""
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return cert.fingerprint(hashes.SHA256()).hex()


def ca_cert_pem(pki_dir: Path) -> str:
    return (pki_dir / CA_CERT).read_text()
```

Note: `server.crt` contains the leaf followed by the CA so TLS clients receive the full chain.

- [ ] **Step 5: Implement `brain/src/loopi_brain/cli.py`**

```python
"""Command line entry point: ``loopi-brain <command>``."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import pki

DEFAULT_PKI_DIR = "/srv/loopi/pki"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loopi-brain")
    sub = parser.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init-pki", help="create the brain CA and server certificate")
    init.add_argument("--pki-dir", default=os.environ.get("LOOPI_PKI_DIR", DEFAULT_PKI_DIR))
    init.add_argument("--host", action="append", default=[], help="DNS name (repeatable)")
    init.add_argument("--ip", action="append", default=[], help="IP address (repeatable)")
    return parser


def _init_pki(args: argparse.Namespace) -> int:
    pki_dir = Path(args.pki_dir)
    try:
        pki.init_pki(pki_dir, args.host, args.ip)
    except pki.PkiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"CA fingerprint: {pki.fingerprint(pki.ca_cert_pem(pki_dir))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init-pki":
        return _init_pki(args)
    return 2
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_pki.py tests/test_cli.py -v`
Expected: 8 passed.

- [ ] **Step 7: Commit**

```bash
git add brain/src/loopi_brain/pki.py brain/src/loopi_brain/cli.py brain/tests/helpers.py brain/tests/test_pki.py brain/tests/test_cli.py
git commit -m "feat(brain): private CA, server cert and device CSR signing"
```

---

### Task 3: Device registry — pair tokens, devices, heartbeats, commands

**Files:**
- Create: `brain/src/loopi_brain/devices.py`
- Modify: `brain/tests/conftest.py` (add `pki_settings` fixture), `brain/tests/helpers.py` (add `HW`, `paired_device`)
- Test: `brain/tests/test_devices.py`

**Interfaces:**
- Consumes: `pki.sign_csr`, `pki.fingerprint`, `make_pool` rows as dicts.
- Produces: `new_id() -> str` (ULID); `create_pair_token(pool, ttl: timedelta) -> dict{token, expires_at}`; `register_device(pool, token: str, hw_info: dict, issue_cert: Callable[[str], str]) -> dict{device_id, cert_pem, fingerprint} | None`; `get_active_device(pool, fingerprint: str) -> dict{id, name} | None`; `revoke_device(pool, device_id) -> bool`; `validate_command(type_: str, payload: dict) -> None` (raises `ValueError`); `enqueue_command(pool, device_id, type_, payload=None) -> str`; `record_heartbeat(pool, device_id, status: dict, acked_ids: list[str]) -> list[dict{id, type, payload}]`; `list_devices(pool) -> list[dict]`. Test fixture `pki_settings`; helpers `HW`, `paired_device(pool, settings) -> dict`.

- [ ] **Step 1: Add fixtures and helpers**

Append to `brain/tests/conftest.py`:

```python
@pytest.fixture
def pki_settings(settings):
    from loopi_brain import pki

    pki.init_pki(settings.pki_dir, ["brain.test"], ["127.0.0.1"])
    return settings
```

Append to `brain/tests/helpers.py`:

```python
import datetime as dt

HW = {"model": "Raspberry Pi Zero W", "serial": "000000001", "agent_version": "0.1.0"}


async def paired_device(pool, settings) -> dict:
    from loopi_brain import devices, pki

    token = (await devices.create_pair_token(pool, dt.timedelta(minutes=5)))["token"]
    return await devices.register_device(
        pool, token, HW, lambda device_id: pki.sign_csr(settings.pki_dir, make_csr_pem(), device_id)
    )
```

- [ ] **Step 2: Write the failing tests `brain/tests/test_devices.py`**

```python
import asyncio
import datetime as dt

import pytest

from helpers import HW, make_csr_pem, paired_device
from loopi_brain import devices, pki


def _issuer(settings):
    return lambda device_id: pki.sign_csr(settings.pki_dir, make_csr_pem(), device_id)


async def _token(pool, minutes=5):
    return (await devices.create_pair_token(pool, dt.timedelta(minutes=minutes)))["token"]


async def test_pair_token_format(pool):
    token = await _token(pool)
    assert len(token) == 9 and token[4] == "-"
    assert set(token.replace("-", "")) <= set(devices.TOKEN_ALPHABET)


async def test_register_device_with_valid_token(pool, pki_settings):
    result = await devices.register_device(pool, await _token(pool), HW, _issuer(pki_settings))
    assert result is not None
    found = await devices.get_active_device(pool, result["fingerprint"])
    assert found["id"] == result["device_id"]


async def test_token_cannot_be_reused(pool, pki_settings):
    token = await _token(pool)
    assert await devices.register_device(pool, token, HW, _issuer(pki_settings)) is not None
    assert await devices.register_device(pool, token, HW, _issuer(pki_settings)) is None


async def test_expired_token_is_rejected(pool, pki_settings):
    token = (await devices.create_pair_token(pool, dt.timedelta(seconds=-1)))["token"]
    assert await devices.register_device(pool, token, HW, _issuer(pki_settings)) is None


async def test_concurrent_registration_with_same_token_pairs_once(pool, pki_settings):
    token = await _token(pool)
    results = await asyncio.gather(
        devices.register_device(pool, token, HW, _issuer(pki_settings)),
        devices.register_device(pool, token, HW, _issuer(pki_settings)),
    )
    assert sum(r is not None for r in results) == 1
    assert len(await devices.list_devices(pool)) == 1


async def test_failed_cert_issue_keeps_token_usable(pool, pki_settings):
    token = await _token(pool)

    def broken(_device_id):
        raise pki.PkiError("bad csr")

    with pytest.raises(pki.PkiError):
        await devices.register_device(pool, token, HW, broken)
    assert await devices.register_device(pool, token, HW, _issuer(pki_settings)) is not None


async def test_get_active_device_ignores_revoked_and_empty(pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    assert await devices.get_active_device(pool, "") is None
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
    await devices.record_heartbeat(pool, bob["device_id"], {}, [cmd])
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_devices.py -v`
Expected: FAIL with `ImportError: cannot import name 'devices'`.

- [ ] **Step 4: Implement `brain/src/loopi_brain/devices.py`**

```python
"""Device registry: pairing tokens, devices, heartbeats and queued commands."""
from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import Callable

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from ulid import ULID

from . import pki

TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
COMMAND_TYPES = frozenset({"pause", "resume", "set_config"})
SET_CONFIG_KEYS: dict[str, type] = {"audio_enabled": bool}


def new_id() -> str:
    return str(ULID())


def _new_token() -> str:
    raw = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


async def create_pair_token(pool: AsyncConnectionPool, ttl: dt.timedelta) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pair_token (token, expires_at) VALUES (%s, now() + %s)"
            " RETURNING token, expires_at",
            (_new_token(), ttl),
        )
        return await cur.fetchone()


async def _consume_token(conn: AsyncConnection, token: str) -> bool:
    cur = await conn.execute(
        "UPDATE pair_token SET used_at = now()"
        " WHERE token = %s AND used_at IS NULL AND expires_at > now() RETURNING token",
        (token,),
    )
    return await cur.fetchone() is not None


async def register_device(
    pool: AsyncConnectionPool,
    token: str,
    hw_info: dict,
    issue_cert: Callable[[str], str],
) -> dict | None:
    """Consume ``token`` and create a device. Returns None if the token is unusable.

    If ``issue_cert`` raises, the transaction rolls back and the token stays usable.
    """
    device_id = new_id()
    async with pool.connection() as conn:
        async with conn.transaction():
            if not await _consume_token(conn, token):
                return None
            cert_pem = issue_cert(device_id)
            fp = pki.fingerprint(cert_pem)
            await conn.execute(
                "INSERT INTO device (id, name, cert_fingerprint, hw_info) VALUES (%s, %s, %s, %s)",
                (device_id, f"loopi-{device_id[-4:].lower()}", fp, Jsonb(hw_info)),
            )
            await conn.execute(
                "UPDATE pair_token SET device_id = %s WHERE token = %s", (device_id, token)
            )
    return {"device_id": device_id, "cert_pem": cert_pem, "fingerprint": fp}


async def get_active_device(pool: AsyncConnectionPool, fingerprint: str) -> dict | None:
    if not fingerprint:
        return None
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, name FROM device WHERE cert_fingerprint = %s AND NOT revoked",
            (fingerprint,),
        )
        return await cur.fetchone()


async def revoke_device(pool: AsyncConnectionPool, device_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE device SET revoked = TRUE WHERE id = %s RETURNING id", (device_id,)
        )
        return await cur.fetchone() is not None


def validate_command(type_: str, payload: dict) -> None:
    if type_ not in COMMAND_TYPES:
        raise ValueError(f"unknown command type {type_!r}")
    if type_ != "set_config":
        if payload:
            raise ValueError(f"{type_} takes no payload")
        return
    if not payload:
        raise ValueError("set_config needs at least one key")
    for key, value in payload.items():
        expected = SET_CONFIG_KEYS.get(key)
        if expected is None:
            raise ValueError(f"set_config key {key!r} is not allowed")
        if not isinstance(value, expected):
            raise ValueError(f"set_config key {key!r} must be {expected.__name__}")


async def enqueue_command(
    pool: AsyncConnectionPool, device_id: str, type_: str, payload: dict | None = None
) -> str:
    payload = payload or {}
    validate_command(type_, payload)
    command_id = new_id()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO device_command (id, device_id, type, payload) VALUES (%s, %s, %s, %s)",
            (command_id, device_id, type_, Jsonb(payload)),
        )
    return command_id


async def record_heartbeat(
    pool: AsyncConnectionPool, device_id: str, status: dict, acked_ids: list[str]
) -> list[dict]:
    """Store the latest status, ack this device's commands, return still-pending ones."""
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE device SET last_heartbeat = %s, last_heartbeat_at = now(),"
                " dropped_total = dropped_total + %s WHERE id = %s",
                (Jsonb(status), int(status.get("dropped_items", 0)), device_id),
            )
            if acked_ids:
                await conn.execute(
                    "UPDATE device_command SET acked_at = now()"
                    " WHERE device_id = %s AND id = ANY(%s) AND acked_at IS NULL",
                    (device_id, acked_ids),
                )
            cur = await conn.execute(
                "SELECT id, type, payload FROM device_command"
                " WHERE device_id = %s AND acked_at IS NULL ORDER BY id",
                (device_id,),
            )
            return await cur.fetchall()


async def list_devices(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, name, revoked, hw_info, last_heartbeat, last_heartbeat_at,"
            " dropped_total, created_at FROM device ORDER BY created_at"
        )
        return await cur.fetchall()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_devices.py -v`
Expected: all passed (15 incl. parametrized).

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/devices.py brain/tests/conftest.py brain/tests/helpers.py brain/tests/test_devices.py
git commit -m "feat(brain): device registry with one-time pair tokens and command queue"
```

---

### Task 4: Protocol contract, app factory, device auth and `POST /v1/pair`

**Files:**
- Create: `docs/protocol/v1.openapi.yaml`, `brain/src/loopi_brain/app.py`, `brain/src/loopi_brain/api/__init__.py` (empty), `brain/src/loopi_brain/api/device_auth.py`, `brain/src/loopi_brain/api/device_routes.py`, `brain/tests/contract.py`
- Modify: `brain/tests/conftest.py` (add `client` fixture)
- Test: `brain/tests/test_pair_api.py`

**Interfaces:**
- Consumes: `devices.register_device`, `devices.get_active_device`, `pki.sign_csr`, `pki.ca_cert_pem`, `pki.fingerprint`, `Settings.supported_agent_major`.
- Produces: `create_app(settings: Settings, pool: AsyncConnectionPool | None = None) -> FastAPI` (stores `app.state.settings`, `app.state.pool`); `current_device(request, x_loopi_client_fingerprint: str | None) -> dict{id, name}` (raises 401); `check_agent_version(request, x_loopi_agent_version: str | None) -> None` (raises 426); `device_routes.router`; test helper `contract.assert_matches(schema_name: str, instance) -> None`; fixture `client` (httpx AsyncClient against the app).

- [ ] **Step 1: Write the contract `docs/protocol/v1.openapi.yaml`**

```yaml
openapi: 3.1.0
info:
  title: Loopi device-brain sync protocol
  version: 1.0.0
  description: |
    Device endpoints served by the brain behind Caddy. Every endpoint except
    /v1/pair requires mTLS. Caddy forwards the client certificate's SHA-256
    fingerprint (lowercase hex of DER) as X-Loopi-Client-Fingerprint and the
    brain maps it to a device. Timestamps are RFC 3339 in UTC.
paths:
  /v1/pair:
    post:
      operationId: pair
      summary: Exchange a one-time token and a P-256 CSR for a client certificate.
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/PairRequest' }
      responses:
        '201':
          description: Paired.
          content:
            application/json:
              schema: { $ref: '#/components/schemas/PairResponse' }
        '401': { $ref: '#/components/responses/Error' }
        '422': { $ref: '#/components/responses/Error' }
  /v1/items/{id}:
    put:
      operationId: putItem
      summary: Upload one captured segment or marker. Idempotent per id.
      parameters:
        - { name: id, in: path, required: true, schema: { $ref: '#/components/schemas/Ulid' } }
        - name: X-Loopi-Kind
          in: header
          required: true
          schema: { type: string, enum: [video, audio, marker] }
        - { name: X-Loopi-Start, in: header, required: true, schema: { type: string, format: date-time } }
        - { name: X-Loopi-End, in: header, required: true, schema: { type: string, format: date-time } }
        - name: X-Loopi-Sha256
          in: header
          required: true
          schema: { type: string, pattern: '^[0-9a-f]{64}$' }
        - name: X-Loopi-Video-Fps
          in: header
          required: false
          description: Frame rate of a raw H.264 video segment (raw H.264 has no timing); defaults to 2.
          schema: { type: integer, minimum: 1, maximum: 30 }
        - { $ref: '#/components/parameters/AgentVersion' }
      requestBody:
        required: true
        content:
          video/h264: { schema: { type: string, format: binary } }
          audio/ogg: { schema: { type: string, format: binary } }
          application/json: { schema: { $ref: '#/components/schemas/Marker' } }
      responses:
        '201':
          description: Stored.
          content:
            application/json:
              schema: { $ref: '#/components/schemas/ItemAck' }
        '200':
          description: Already stored with the same sha256.
          content:
            application/json:
              schema: { $ref: '#/components/schemas/ItemAck' }
        '401': { $ref: '#/components/responses/Error' }
        '409': { $ref: '#/components/responses/Error' }
        '413': { $ref: '#/components/responses/Error' }
        '422': { $ref: '#/components/responses/Error' }
        '426': { $ref: '#/components/responses/Error' }
        '507': { $ref: '#/components/responses/Error' }
  /v1/heartbeat:
    post:
      operationId: heartbeat
      summary: Report device status, ack commands, receive pending commands.
      parameters:
        - { $ref: '#/components/parameters/AgentVersion' }
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/Heartbeat' }
      responses:
        '200':
          description: Pending commands, oldest first.
          content:
            application/json:
              schema: { $ref: '#/components/schemas/HeartbeatResponse' }
        '401': { $ref: '#/components/responses/Error' }
        '422': { $ref: '#/components/responses/Error' }
        '426': { $ref: '#/components/responses/Error' }
components:
  parameters:
    AgentVersion:
      name: X-Loopi-Agent-Version
      in: header
      required: false
      schema: { type: string, pattern: '^[0-9]+\.[0-9]+\.[0-9]+' }
  responses:
    Error:
      description: Error with a human-readable detail.
      content:
        application/json:
          schema: { $ref: '#/components/schemas/Error' }
  schemas:
    Ulid:
      type: string
      pattern: '^[0-9A-HJKMNP-TV-Z]{26}$'
    Error:
      type: object
      required: [detail]
      properties:
        detail: {}
    HwInfo:
      type: object
      additionalProperties: false
      required: [model, serial, agent_version]
      properties:
        model: { type: string, maxLength: 100 }
        serial: { type: string, maxLength: 100 }
        agent_version: { type: string, maxLength: 32 }
    PairRequest:
      type: object
      additionalProperties: false
      required: [token, csr_pem, hw_info]
      properties:
        token: { type: string, minLength: 1, maxLength: 32 }
        csr_pem: { type: string, maxLength: 8192 }
        hw_info: { $ref: '#/components/schemas/HwInfo' }
    PairResponse:
      type: object
      additionalProperties: false
      required: [device_id, cert_pem, ca_cert_pem, ca_fingerprint]
      properties:
        device_id: { $ref: '#/components/schemas/Ulid' }
        cert_pem: { type: string }
        ca_cert_pem: { type: string }
        ca_fingerprint: { type: string, pattern: '^[0-9a-f]{64}$' }
    Marker:
      type: object
      additionalProperties: false
      properties:
        note: { type: string, maxLength: 500 }
    ItemAck:
      type: object
      additionalProperties: false
      required: [id, status]
      properties:
        id: { $ref: '#/components/schemas/Ulid' }
        status: { type: string, enum: [created, duplicate] }
    Heartbeat:
      type: object
      additionalProperties: false
      required: [agent_version, uptime_s, disk_used_pct, queue_depth, clock_synced, paused,
                 audio_enabled, recording_video, recording_audio, acked_command_ids]
      properties:
        agent_version: { type: string, maxLength: 32 }
        uptime_s: { type: integer, minimum: 0 }
        soc_temp_c: { type: [number, 'null'] }
        disk_used_pct: { type: number, minimum: 0, maximum: 100 }
        queue_depth: { type: integer, minimum: 0 }
        wifi_rssi_dbm: { type: [integer, 'null'] }
        clock_synced: { type: boolean }
        paused: { type: boolean }
        audio_enabled: { type: boolean }
        recording_video: { type: boolean }
        recording_audio: { type: boolean }
        mic_missing: { type: boolean }
        camera_failures: { type: integer, minimum: 0 }
        last_error: { type: [string, 'null'], maxLength: 500 }
        dropped_items: { type: integer, minimum: 0 }
        acked_command_ids:
          type: array
          maxItems: 100
          items: { $ref: '#/components/schemas/Ulid' }
    Command:
      type: object
      additionalProperties: false
      required: [id, type, payload]
      properties:
        id: { $ref: '#/components/schemas/Ulid' }
        type: { type: string, enum: [pause, resume, set_config] }
        payload: { type: object }
    HeartbeatResponse:
      type: object
      additionalProperties: false
      required: [commands]
      properties:
        commands:
          type: array
          items: { $ref: '#/components/schemas/Command' }
```

- [ ] **Step 2: Write the contract helper `brain/tests/contract.py`**

```python
"""Validate payloads against docs/protocol/v1.openapi.yaml component schemas."""
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "protocol" / "v1.openapi.yaml"
_SPEC = yaml.safe_load(SPEC_PATH.read_text())


def assert_matches(schema_name: str, instance) -> None:
    # Root the $ref at a document that carries `components`, so internal refs resolve.
    schema = {"$ref": f"#/components/schemas/{schema_name}", "components": _SPEC["components"]}
    Draft202012Validator(schema).validate(instance)
```

- [ ] **Step 3: Add the `client` fixture to `brain/tests/conftest.py`**

```python
import httpx


@pytest.fixture
async def client(pki_settings, pool):
    from loopi_brain.app import create_app

    app = create_app(pki_settings, pool=pool)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://brain.test"
    ) as c:
        yield c
```

- [ ] **Step 4: Write the failing tests `brain/tests/test_pair_api.py`**

```python
import datetime as dt
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

from contract import assert_matches
from helpers import HW, make_csr_pem, paired_device
from loopi_brain import devices, pki
from loopi_brain.api.device_auth import check_agent_version, current_device


async def _token(pool):
    return (await devices.create_pair_token(pool, dt.timedelta(minutes=5)))["token"]


async def test_pair_returns_cert_and_ca(client, pool):
    body = {"token": await _token(pool), "csr_pem": make_csr_pem(), "hw_info": HW}
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
    token = await _token(pool)
    r = await client.post(
        "/v1/pair", json={"token": f" {token.lower()} ", "csr_pem": make_csr_pem(), "hw_info": HW}
    )
    assert r.status_code == 201


async def test_pair_with_used_token_is_401(client, pool):
    token = await _token(pool)
    body = {"token": token, "csr_pem": make_csr_pem(), "hw_info": HW}
    assert (await client.post("/v1/pair", json=body)).status_code == 201
    r = await client.post("/v1/pair", json=body)
    assert r.status_code == 401
    assert_matches("Error", r.json())


async def test_pair_with_bad_csr_is_422_and_token_survives(client, pool):
    token = await _token(pool)
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
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_pair_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'loopi_brain.app'`.

- [ ] **Step 6: Implement app, auth and the pair route**

`brain/src/loopi_brain/app.py`:

```python
"""FastAPI application factory."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from .api.device_routes import router as device_router
from .config import Settings
from .db import make_pool


def create_app(settings: Settings, pool: AsyncConnectionPool | None = None) -> FastAPI:
    owns_pool = pool is None
    active_pool = pool if pool is not None else make_pool(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if owns_pool:
            await active_pool.open()
        try:
            yield
        finally:
            if owns_pool:
                await active_pool.close()

    app = FastAPI(title="Loopi Brain", lifespan=lifespan)
    app.state.settings = settings
    app.state.pool = active_pool
    app.include_router(device_router)
    return app
```

`brain/src/loopi_brain/api/device_auth.py`:

```python
"""Device authentication: Caddy forwards the verified client-cert fingerprint."""
from fastapi import Header, HTTPException, Request

from .. import devices


async def current_device(
    request: Request, x_loopi_client_fingerprint: str | None = Header(default=None)
) -> dict:
    fingerprint = (x_loopi_client_fingerprint or "").strip().lower()
    device = await devices.get_active_device(request.app.state.pool, fingerprint)
    if device is None:
        raise HTTPException(status_code=401, detail="unknown or revoked device certificate")
    return device


def check_agent_version(
    request: Request, x_loopi_agent_version: str | None = Header(default=None)
) -> None:
    if x_loopi_agent_version is None:
        return
    major = x_loopi_agent_version.split(".", 1)[0]
    supported = request.app.state.settings.supported_agent_major
    if not major.isdigit() or int(major) != supported:
        raise HTTPException(
            status_code=426,
            detail=f"agent {x_loopi_agent_version} unsupported; need major {supported}",
        )
```

`brain/src/loopi_brain/api/device_routes.py`:

```python
"""Device-facing endpoints of sync protocol v1 (docs/protocol/v1.openapi.yaml)."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import devices, pki

router = APIRouter()


class HwInfo(BaseModel):
    model: str = Field(max_length=100)
    serial: str = Field(max_length=100)
    agent_version: str = Field(max_length=32)


class PairRequest(BaseModel):
    token: str = Field(min_length=1, max_length=32)
    csr_pem: str = Field(max_length=8192)
    hw_info: HwInfo


class PairResponse(BaseModel):
    device_id: str
    cert_pem: str
    ca_cert_pem: str
    ca_fingerprint: str


@router.post("/v1/pair", status_code=201, response_model=PairResponse)
async def pair(body: PairRequest, request: Request) -> PairResponse:
    settings = request.app.state.settings

    def issue(device_id: str) -> str:
        return pki.sign_csr(settings.pki_dir, body.csr_pem, device_id)

    try:
        result = await devices.register_device(
            request.app.state.pool, body.token.strip().upper(), body.hw_info.model_dump(), issue
        )
    except pki.PkiError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=401, detail="pair token invalid, used or expired")
    ca_pem = pki.ca_cert_pem(settings.pki_dir)
    return PairResponse(
        device_id=result["device_id"],
        cert_pem=result["cert_pem"],
        ca_cert_pem=ca_pem,
        ca_fingerprint=pki.fingerprint(ca_pem),
    )
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_pair_api.py -v`
Expected: all passed.

- [ ] **Step 8: Commit**

```bash
git add docs/protocol/v1.openapi.yaml brain/src/loopi_brain/app.py brain/src/loopi_brain/api brain/tests/contract.py brain/tests/conftest.py brain/tests/test_pair_api.py
git commit -m "feat(brain): protocol v1 contract, device auth and pairing endpoint"
```

---

### Task 5: `PUT /v1/items/{id}` — validated, streaming, idempotent ingest

**Files:**
- Create: `brain/src/loopi_brain/items.py`
- Modify: `brain/src/loopi_brain/api/device_routes.py` (add the PUT route)
- Test: `brain/tests/test_items_api.py`

**Interfaces:**
- Consumes: `current_device`, `check_agent_version`, `Settings.media_root/min_free_bytes/max_item_bytes/default_video_fps`.
- Produces: `KIND_CONTENT_TYPES`, `KIND_EXT`; `@dataclass(frozen=True) ItemHeader(id, kind, started_at, ended_at, sha256, content_type, video_fps)`; `class ItemRejected(Exception)` with `.status: int`, `.detail: str`; `parse_header(item_id: str, headers: Mapping[str, str], default_fps: int) -> ItemHeader`; `media_rel_path(device_id: str, header: ItemHeader) -> PurePosixPath`; `async store_item(pool, settings, device_id: str, header: ItemHeader, body: AsyncIterator[bytes]) -> tuple[int, dict]`. Video rows are stored with `status='stored'` and `meta={"video_fps": n}`; audio rows `status='ready'`, `view_path=raw_path`; marker rows `status='ready'`, `meta` = parsed JSON.

- [ ] **Step 1: Write the failing tests `brain/tests/test_items_api.py`**

```python
import hashlib
import json
from pathlib import Path

import pytest

from helpers import paired_device
from loopi_brain import devices
from loopi_brain.app import create_app

ITEM_ID = "01JA2B3C4D5E6F7G8H9J0KMNPQ"
START = "2026-10-14T01:00:00Z"
END = "2026-10-14T01:01:00Z"


def _headers(fp, body, kind="video", sha=None, ctype=None, start=START, end=END, **extra):
    ctypes = {"video": "video/h264", "audio": "audio/ogg", "marker": "application/json"}
    headers = {
        "X-Loopi-Client-Fingerprint": fp,
        "X-Loopi-Kind": kind,
        "X-Loopi-Start": start,
        "X-Loopi-End": end,
        "X-Loopi-Sha256": sha or hashlib.sha256(body).hexdigest(),
        "Content-Type": ctype or ctypes.get(kind, "application/octet-stream"),
    }
    headers.update(extra)
    return headers


@pytest.fixture
async def device(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _row(pool, item_id=ITEM_ID):
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM item WHERE id = %s", (item_id,))
        return await cur.fetchone()


async def test_video_upload_is_stored_then_duplicate(client, pool, pki_settings, device):
    body = b"\x00\x00\x00\x01video"
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body,
                         headers=_headers(device["fingerprint"], body, **{"X-Loopi-Video-Fps": "2"}))
    assert r.status_code == 201, r.text
    assert r.json() == {"id": ITEM_ID, "status": "created"}
    row = await _row(pool)
    assert row["status"] == "stored" and row["meta"] == {"video_fps": 2}
    stored = pki_settings.media_root / row["raw_path"]
    assert stored.read_bytes() == body
    assert row["raw_path"] == f"raw/{device['device_id']}/2026/10/14/{ITEM_ID}.h264"
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body))
    assert r.status_code == 200 and r.json()["status"] == "duplicate"


async def test_same_id_different_content_is_409(client, device):
    first, second = b"one", b"two"
    assert (await client.put(f"/v1/items/{ITEM_ID}", content=first,
                             headers=_headers(device["fingerprint"], first))).status_code == 201
    r = await client.put(f"/v1/items/{ITEM_ID}", content=second, headers=_headers(device["fingerprint"], second))
    assert r.status_code == 409


async def test_sha_mismatch_leaves_nothing_and_retry_succeeds(client, pool, pki_settings, device):
    body = b"complete-segment"
    truncated = body[:5]
    r = await client.put(f"/v1/items/{ITEM_ID}", content=truncated,
                         headers=_headers(device["fingerprint"], truncated, sha=hashlib.sha256(body).hexdigest()))
    assert r.status_code == 422
    assert await _row(pool) is None
    leftovers = [p for p in Path(pki_settings.media_root).rglob("*") if p.is_file()]
    assert leftovers == []
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body))
    assert r.status_code == 201


async def test_audio_is_ready_immediately(client, pool, device):
    body = b"OggS-fake"
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body, kind="audio"))
    assert r.status_code == 201
    row = await _row(pool)
    assert row["status"] == "ready" and row["view_path"] == row["raw_path"]
    assert row["raw_path"].endswith(".opus")


async def test_marker_meta_is_stored(client, pool, device):
    body = json.dumps({"note": "chìa khoá"}).encode()
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body,
                         headers=_headers(device["fingerprint"], body, kind="marker", start=START, end=START))
    assert r.status_code == 201
    assert (await _row(pool))["meta"] == {"note": "chìa khoá"}


@pytest.mark.parametrize(
    ("item_id", "overrides"),
    [
        ("not-a-ulid", {}),
        (ITEM_ID, {"kind": "photo"}),
        (ITEM_ID, {"ctype": "audio/ogg"}),
        (ITEM_ID, {"start": END, "end": START}),
        (ITEM_ID, {"start": "2026-10-14T01:00:00"}),
        (ITEM_ID, {"sha": "ABC"}),
        (ITEM_ID, {"X-Loopi-Video-Fps": "0"}),
        (ITEM_ID, {"start": "2026-10-14T01:00:00Z", "end": "2026-10-14T03:00:00Z"}),
    ],
)
async def test_invalid_headers_are_422(client, pool, device, item_id, overrides):
    body = b"x"
    r = await client.put(f"/v1/items/{item_id}", content=body, headers=_headers(device["fingerprint"], body, **overrides))
    assert r.status_code == 422
    assert await _row(pool) is None


async def test_bad_marker_json_is_422(client, device):
    body = b"[1, 2]"
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body,
                         headers=_headers(device["fingerprint"], body, kind="marker", end=START))
    assert r.status_code == 422


async def test_unauthenticated_and_revoked_are_401(client, pool, device):
    body = b"x"
    assert (await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers("", body))).status_code == 401
    await devices.revoke_device(pool, device["device_id"])
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body))
    assert r.status_code == 401


async def test_old_agent_major_is_426(client, device):
    body = b"x"
    r = await client.put(f"/v1/items/{ITEM_ID}", content=body,
                         headers=_headers(device["fingerprint"], body, **{"X-Loopi-Agent-Version": "1.0.0"}))
    assert r.status_code == 426


async def _client_with(pki_settings, pool, **overrides):
    import httpx

    app = create_app(pki_settings.model_copy(update=overrides), pool=pool)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test")


async def test_low_disk_is_507(pool, pki_settings, device):
    body = b"x"
    async with await _client_with(pki_settings, pool, min_free_bytes=10**18) as c:
        r = await c.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body))
    assert r.status_code == 507


async def test_oversize_body_is_413(pool, pki_settings, device):
    body = b"x" * 2048
    async with await _client_with(pki_settings, pool, max_item_bytes=1024) as c:
        r = await c.put(f"/v1/items/{ITEM_ID}", content=body, headers=_headers(device["fingerprint"], body))
    assert r.status_code == 413
    assert await _row(pool) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_items_api.py -v`
Expected: FAIL — PUT returns `405 Method Not Allowed` (route missing).

- [ ] **Step 3: Implement `brain/src/loopi_brain/items.py`**

```python
"""Ingest of uploaded segments: header validation, streaming write, idempotency."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
import shutil
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import anyio
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .config import Settings

KIND_CONTENT_TYPES = {"video": "video/h264", "audio": "audio/ogg", "marker": "application/json"}
KIND_EXT = {"video": "h264", "audio": "opus", "marker": "json"}
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_SEGMENT_DURATION = dt.timedelta(hours=1)
FPS_RANGE = range(1, 31)


@dataclass(frozen=True)
class ItemHeader:
    id: str
    kind: str
    started_at: dt.datetime
    ended_at: dt.datetime
    sha256: str
    content_type: str
    video_fps: int | None


class ItemRejected(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _reject(detail: str, status: int = 422) -> ItemRejected:
    return ItemRejected(status, detail)


def _parse_time(name: str, value: str | None) -> dt.datetime:
    if not value:
        raise _reject(f"missing {name}")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise _reject(f"{name} is not RFC 3339") from exc
    if parsed.tzinfo is None:
        raise _reject(f"{name} must include a timezone")
    return parsed.astimezone(dt.UTC)


def parse_header(item_id: str, headers: Mapping[str, str], default_fps: int) -> ItemHeader:
    if not ULID_RE.match(item_id):
        raise _reject("item id must be a ULID")
    kind = headers.get("x-loopi-kind", "")
    if kind not in KIND_CONTENT_TYPES:
        raise _reject(f"unknown kind {kind!r}")
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != KIND_CONTENT_TYPES[kind]:
        raise _reject(f"content-type for {kind} must be {KIND_CONTENT_TYPES[kind]}")
    started = _parse_time("X-Loopi-Start", headers.get("x-loopi-start"))
    ended = _parse_time("X-Loopi-End", headers.get("x-loopi-end"))
    if ended < started:
        raise _reject("X-Loopi-End is before X-Loopi-Start")
    if ended - started > MAX_SEGMENT_DURATION:
        raise _reject("segment longer than 1 hour")
    sha = headers.get("x-loopi-sha256", "")
    if not SHA_RE.match(sha):
        raise _reject("X-Loopi-Sha256 must be 64 lowercase hex chars")
    fps = None
    if kind == "video":
        raw_fps = headers.get("x-loopi-video-fps")
        try:
            fps = int(raw_fps) if raw_fps is not None else default_fps
        except ValueError as exc:
            raise _reject("X-Loopi-Video-Fps must be an integer") from exc
        if fps not in FPS_RANGE:
            raise _reject("X-Loopi-Video-Fps must be 1..30")
    return ItemHeader(item_id, kind, started, ended, sha, content_type, fps)


def media_rel_path(device_id: str, header: ItemHeader) -> PurePosixPath:
    return PurePosixPath(
        "raw", device_id, header.started_at.strftime("%Y/%m/%d"), f"{header.id}.{KIND_EXT[header.kind]}"
    )


async def _existing(conn, item_id: str) -> dict | None:
    cur = await conn.execute("SELECT sha256, device_id FROM item WHERE id = %s", (item_id,))
    return await cur.fetchone()


def _duplicate_or_conflict(existing: dict, device_id: str, header: ItemHeader) -> tuple[int, dict]:
    if existing["sha256"] == header.sha256 and existing["device_id"] == device_id:
        return 200, {"id": header.id, "status": "duplicate"}
    raise _reject("item id already exists with different content", status=409)


async def _write_body(tmp: Path, body: AsyncIterator[bytes], limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    async with await anyio.open_file(tmp, "wb") as out:
        async for chunk in body:
            size += len(chunk)
            if size > limit:
                raise _reject(f"item larger than {limit} bytes", status=413)
            digest.update(chunk)
            await out.write(chunk)
    return digest.hexdigest(), size


def _marker_meta(path: Path) -> dict:
    try:
        meta = json.loads(path.read_bytes() or b"{}")
    except json.JSONDecodeError as exc:
        raise _reject("marker body is not JSON") from exc
    if not isinstance(meta, dict):
        raise _reject("marker body must be a JSON object")
    return meta


def _remove_empty_dirs(start: Path, stop: Path) -> None:
    """After a rejected upload, drop directories we created but left empty."""
    current = start
    while current != stop and current.is_dir() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


async def store_item(
    pool: AsyncConnectionPool,
    settings: Settings,
    device_id: str,
    header: ItemHeader,
    body: AsyncIterator[bytes],
) -> tuple[int, dict]:
    """Store one item. Returns (201|200, ack) or raises ItemRejected."""
    async with pool.connection() as conn:
        existing = await _existing(conn, header.id)
    if existing is not None:
        return _duplicate_or_conflict(existing, device_id, header)

    settings.media_root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(settings.media_root).free < settings.min_free_bytes:
        raise _reject("brain storage is full", status=507)

    rel = media_rel_path(device_id, header)
    final = settings.media_root / rel
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f".{final.name}.{secrets.token_hex(4)}.part")
    try:
        sha, size = await _write_body(tmp, body, settings.max_item_bytes)
        if sha != header.sha256:
            raise _reject("sha256 mismatch: body corrupted or truncated")
        meta: dict = {}
        status, view_path = "ready", str(rel)
        if header.kind == "video":
            meta, status, view_path = {"video_fps": header.video_fps}, "stored", None
        elif header.kind == "marker":
            meta, view_path = _marker_meta(tmp), None

        async with pool.connection() as conn:
            async with conn.transaction():
                # Serialise concurrent uploads of the same id.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (header.id,))
                existing = await _existing(conn, header.id)
                if existing is not None:
                    return _duplicate_or_conflict(existing, device_id, header)
                tmp.replace(final)
                await conn.execute(
                    "INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes,"
                    " raw_path, view_path, meta, status)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (header.id, device_id, header.kind, header.started_at, header.ended_at,
                     header.sha256, size, str(rel), view_path, Jsonb(meta), status),
                )
    finally:
        tmp.unlink(missing_ok=True)
        _remove_empty_dirs(final.parent, settings.media_root)
    return 201, {"id": header.id, "status": "created"}
```

- [ ] **Step 4: Add the route to `brain/src/loopi_brain/api/device_routes.py`**

Change the imports at the top to:

```python
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import devices, items, pki
from .device_auth import check_agent_version, current_device
```

Append:

```python
@router.put("/v1/items/{item_id}", dependencies=[Depends(check_agent_version)])
async def put_item(item_id: str, request: Request, device: dict = Depends(current_device)) -> JSONResponse:
    settings = request.app.state.settings
    try:
        header = items.parse_header(item_id, request.headers, settings.default_video_fps)
        status, ack = await items.store_item(
            request.app.state.pool, settings, device["id"], header, request.stream()
        )
    except items.ItemRejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return JSONResponse(ack, status_code=status)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_items_api.py -v`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/items.py brain/src/loopi_brain/api/device_routes.py brain/tests/test_items_api.py
git commit -m "feat(brain): idempotent streaming item ingest with sha256 verification"
```

---

### Task 6: Remux raw H.264 to MP4 for the browser

**Files:**
- Create: `brain/src/loopi_brain/media.py`
- Modify: `brain/src/loopi_brain/api/device_routes.py` (`put_item` schedules remux)
- Test: `brain/tests/test_media.py`

**Interfaces:**
- Consumes: item rows from Task 5 (`raw_path`, `meta.video_fps`, `status='stored'`).
- Produces: `class MediaError(Exception)`; `async remux_to_mp4(src: Path, dst: Path, fps: int) -> None`; `async remux_item(pool, settings, item_id: str) -> None` → sets `status='ready'`, `view_path='view/<device>/<Y>/<m>/<d>/<id>.mp4'` or `status='error'`, `error=<stderr tail>`.

- [ ] **Step 1: Write the failing tests `brain/tests/test_media.py`**

```python
import hashlib
import shutil
import subprocess

import pytest

from helpers import paired_device
from loopi_brain import media

ITEM_ID = "01JA2B3C4D5E6F7G8H9J0KMNPR"


@pytest.fixture(scope="session")
def h264_sample(tmp_path_factory) -> bytes:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe are required for media tests (brew install ffmpeg)")
    out = tmp_path_factory.mktemp("media") / "sample.h264"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=2",
         "-frames:v", "6", "-c:v", "libx264", "-g", "2", "-bsf:v", "h264_mp4toannexb", "-f", "h264", str(out)],
        check=True,
    )
    return out.read_bytes()


def _duration(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


async def test_remux_respects_fps(tmp_path, h264_sample):
    src = tmp_path / "in.h264"
    src.write_bytes(h264_sample)
    dst = tmp_path / "out" / "in.mp4"
    await media.remux_to_mp4(src, dst, fps=2)
    assert 2.5 <= _duration(dst) <= 3.5
    assert not list(tmp_path.rglob("*.part.mp4"))


async def test_remux_of_garbage_raises(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg required")
    src = tmp_path / "bad.h264"
    src.write_bytes(b"not video")
    with pytest.raises(media.MediaError):
        await media.remux_to_mp4(src, tmp_path / "bad.mp4", fps=2)
    assert not (tmp_path / "bad.mp4").exists()


async def test_uploaded_video_becomes_ready(client, pool, pki_settings, h264_sample):
    dev = await paired_device(pool, pki_settings)
    headers = {
        "X-Loopi-Client-Fingerprint": dev["fingerprint"],
        "X-Loopi-Kind": "video",
        "X-Loopi-Start": "2026-10-14T01:00:00Z",
        "X-Loopi-End": "2026-10-14T01:00:03Z",
        "X-Loopi-Sha256": hashlib.sha256(h264_sample).hexdigest(),
        "X-Loopi-Video-Fps": "2",
        "Content-Type": "video/h264",
    }
    r = await client.put(f"/v1/items/{ITEM_ID}", content=h264_sample, headers=headers)
    assert r.status_code == 201
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT status, view_path FROM item WHERE id = %s", (ITEM_ID,))).fetchone()
    assert row["status"] == "ready"
    assert row["view_path"] == f"view/{dev['device_id']}/2026/10/14/{ITEM_ID}.mp4"
    assert (pki_settings.media_root / row["view_path"]).is_file()


async def test_failed_remux_marks_item_error(client, pool, pki_settings):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg required")
    dev = await paired_device(pool, pki_settings)
    body = b"definitely not h264"
    headers = {
        "X-Loopi-Client-Fingerprint": dev["fingerprint"],
        "X-Loopi-Kind": "video",
        "X-Loopi-Start": "2026-10-14T01:00:00Z",
        "X-Loopi-End": "2026-10-14T01:01:00Z",
        "X-Loopi-Sha256": hashlib.sha256(body).hexdigest(),
        "Content-Type": "video/h264",
    }
    assert (await client.put(f"/v1/items/{ITEM_ID}", content=body, headers=headers)).status_code == 201
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT status, error FROM item WHERE id = %s", (ITEM_ID,))).fetchone()
    assert row["status"] == "error" and row["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_media.py -v`
Expected: FAIL with `ImportError: cannot import name 'media'`.

- [ ] **Step 3: Implement `brain/src/loopi_brain/media.py`**

```python
"""Browser-playable views of raw media (raw H.264 → MP4 without re-encoding)."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path, PurePosixPath

from psycopg_pool import AsyncConnectionPool

from .config import Settings

log = logging.getLogger(__name__)
STDERR_TAIL = 500


class MediaError(Exception):
    """ffmpeg failed to process a file."""


async def remux_to_mp4(src: Path, dst: Path, fps: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".part.mp4")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-framerate", str(fps), "-f", "h264", "-i", str(src),
        "-c", "copy", "-movflags", "+faststart", str(tmp),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise MediaError(stderr.decode(errors="replace")[-STDERR_TAIL:] or f"ffmpeg exit {proc.returncode}")
    tmp.replace(dst)


async def remux_item(pool: AsyncConnectionPool, settings: Settings, item_id: str) -> None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT raw_path, meta FROM item WHERE id = %s AND kind = 'video'", (item_id,)
        )
        row = await cur.fetchone()
    if row is None:
        return
    raw_rel = PurePosixPath(row["raw_path"])
    view_rel = PurePosixPath("view", *raw_rel.parts[1:]).with_suffix(".mp4")
    fps = int(row["meta"].get("video_fps", settings.default_video_fps))
    try:
        await remux_to_mp4(settings.media_root / raw_rel, settings.media_root / view_rel, fps)
    except MediaError as exc:
        log.warning("remux failed for %s: %s", item_id, exc)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE item SET status = 'error', error = %s WHERE id = %s", (str(exc), item_id)
            )
        return
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE item SET status = 'ready', view_path = %s, error = NULL WHERE id = %s",
            (str(view_rel), item_id),
        )
```

- [ ] **Step 4: Schedule remux from `put_item`**

In `brain/src/loopi_brain/api/device_routes.py` change the imports to:

```python
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import devices, items, media, pki
from .device_auth import check_agent_version, current_device
```

and replace `put_item` with:

```python
@router.put("/v1/items/{item_id}", dependencies=[Depends(check_agent_version)])
async def put_item(
    item_id: str,
    request: Request,
    background: BackgroundTasks,
    device: dict = Depends(current_device),
) -> JSONResponse:
    settings = request.app.state.settings
    try:
        header = items.parse_header(item_id, request.headers, settings.default_video_fps)
        status, ack = await items.store_item(
            request.app.state.pool, settings, device["id"], header, request.stream()
        )
    except items.ItemRejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    if status == 201 and header.kind == "video":
        background.add_task(media.remux_item, request.app.state.pool, settings, header.id)
    return JSONResponse(ack, status_code=status)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_media.py tests/test_items_api.py -v`
Expected: all passed (requires `ffmpeg` with libx264 on the dev machine).

- [ ] **Step 6: Commit**

```bash
git add brain/src/loopi_brain/media.py brain/src/loopi_brain/api/device_routes.py brain/tests/test_media.py
git commit -m "feat(brain): remux uploaded H.264 segments to MP4 in the background"
```

---

### Task 7: `POST /v1/heartbeat`

**Files:**
- Modify: `brain/src/loopi_brain/api/device_routes.py`
- Test: `brain/tests/test_heartbeat_api.py`

**Interfaces:**
- Consumes: `devices.record_heartbeat`, `current_device`, `check_agent_version`.
- Produces: route returning `{"commands": [{"id", "type", "payload"}]}`; stores the heartbeat body (minus `acked_command_ids`) in `device.last_heartbeat`.

- [ ] **Step 1: Write the failing tests `brain/tests/test_heartbeat_api.py`**

```python
from contract import assert_matches
from helpers import paired_device
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
                          headers={"X-Loopi-Client-Fingerprint": dev["fingerprint"]})
    assert r.status_code == 200, r.text
    assert_matches("HeartbeatResponse", r.json())
    assert r.json()["commands"] == [{"id": cmd, "type": "pause", "payload": {}}]
    (row,) = await devices.list_devices(pool)
    assert row["last_heartbeat"]["queue_depth"] == 3
    assert "acked_command_ids" not in row["last_heartbeat"]

    acked = {**HEARTBEAT, "acked_command_ids": [cmd]}
    r = await client.post("/v1/heartbeat", json=acked,
                          headers={"X-Loopi-Client-Fingerprint": dev["fingerprint"]})
    assert r.json() == {"commands": []}


async def test_heartbeat_requires_device(client):
    assert (await client.post("/v1/heartbeat", json=HEARTBEAT)).status_code == 401


async def test_heartbeat_rejects_invalid_body(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    r = await client.post("/v1/heartbeat", json={**HEARTBEAT, "disk_used_pct": 140},
                          headers={"X-Loopi-Client-Fingerprint": dev["fingerprint"]})
    assert r.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_heartbeat_api.py -v`
Expected: FAIL — `404 Not Found` for `/v1/heartbeat`.

- [ ] **Step 3: Implement the route**

In `brain/src/loopi_brain/api/device_routes.py` add `from typing import Literal` and change the pydantic import to `from pydantic import BaseModel, ConfigDict, Field`, then append:

```python
class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_version: str = Field(max_length=32)
    uptime_s: int = Field(ge=0)
    soc_temp_c: float | None = None
    disk_used_pct: float = Field(ge=0, le=100)
    queue_depth: int = Field(ge=0)
    wifi_rssi_dbm: int | None = None
    clock_synced: bool
    paused: bool
    audio_enabled: bool
    recording_video: bool
    recording_audio: bool
    mic_missing: bool = False
    camera_failures: int = Field(default=0, ge=0)
    last_error: str | None = Field(default=None, max_length=500)
    dropped_items: int = Field(default=0, ge=0)
    acked_command_ids: list[str] = Field(default_factory=list, max_length=100)


class Command(BaseModel):
    id: str
    type: Literal["pause", "resume", "set_config"]
    payload: dict


class HeartbeatResponse(BaseModel):
    commands: list[Command]


@router.post(
    "/v1/heartbeat",
    response_model=HeartbeatResponse,
    dependencies=[Depends(check_agent_version)],
)
async def heartbeat(
    body: Heartbeat, request: Request, device: dict = Depends(current_device)
) -> HeartbeatResponse:
    status = body.model_dump(exclude={"acked_command_ids"})
    pending = await devices.record_heartbeat(
        request.app.state.pool, device["id"], status, body.acked_command_ids
    )
    return HeartbeatResponse(commands=[Command(**row) for row in pending])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_heartbeat_api.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/api/device_routes.py brain/tests/test_heartbeat_api.py
git commit -m "feat(brain): heartbeat endpoint with command delivery and acks"
```

---

### Task 8: Web UI — raw timeline, devices, commands, pair token

**Files:**
- Create: `brain/src/loopi_brain/web/__init__.py` (empty), `brain/src/loopi_brain/web/routes.py`, `brain/src/loopi_brain/web/templates/base.html`, `timeline.html`, `devices.html`, `_pair_token.html`, `_queued.html`, `brain/src/loopi_brain/web/static/htmx.min.js` (downloaded)
- Modify: `brain/src/loopi_brain/app.py` (include web router, mount `/static` and `/media`)
- Test: `brain/tests/test_web.py`

**Interfaces:**
- Consumes: `devices.*`, `pki.fingerprint`, `pki.ca_cert_pem`, `Settings.zone`, `Settings.pair_token_ttl_minutes`.
- Produces: `GET /` (`?day=YYYY-MM-DD`), `GET /devices`, `POST /devices/pair-token`, `POST /devices/{id}/commands/{action}` (`pause|resume|audio-on|audio-off`), `POST /devices/{id}/revoke`; `require_htmx()` dependency (403 without `HX-Request: true`); `day_bounds_utc(day: date, zone) -> tuple[datetime, datetime]`; static `/media/<rel path>`.

- [ ] **Step 1: Download HTMX (pinned)**

Run:
```bash
mkdir -p brain/src/loopi_brain/web/static brain/src/loopi_brain/web/templates
curl -fsSL -o brain/src/loopi_brain/web/static/htmx.min.js https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js
wc -c brain/src/loopi_brain/web/static/htmx.min.js
```
Expected: roughly 50 000 bytes of minified JavaScript.

- [ ] **Step 2: Write the failing tests `brain/tests/test_web.py`**

```python
import datetime as dt
from zoneinfo import ZoneInfo

from helpers import paired_device
from loopi_brain import devices
from loopi_brain.web.routes import day_bounds_utc

HX = {"HX-Request": "true"}


async def _insert_item(pool, device_id, item_id, started_at, kind="audio"):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes,"
            " raw_path, view_path, status) VALUES (%s, %s, %s, %s, %s, 'x', 1, %s, %s, 'ready')",
            (item_id, device_id, kind, started_at, started_at + dt.timedelta(minutes=1),
             f"raw/{item_id}.opus", f"raw/{item_id}.opus"),
        )


def test_day_bounds_utc_for_seoul():
    start, end = day_bounds_utc(dt.date(2026, 10, 15), ZoneInfo("Asia/Seoul"))
    assert start == dt.datetime(2026, 10, 14, 15, 0, tzinfo=dt.UTC)
    assert end == dt.datetime(2026, 10, 15, 15, 0, tzinfo=dt.UTC)


async def test_timeline_uses_local_day_boundaries(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    late = dt.datetime(2026, 10, 14, 23, 30, tzinfo=dt.UTC)  # 08:30 on Oct 15 in Seoul
    await _insert_item(pool, dev["device_id"], "01JA2B3C4D5E6F7G8H9J0KMNPS", late)
    on_15 = await client.get("/", params={"day": "2026-10-15"})
    on_14 = await client.get("/", params={"day": "2026-10-14"})
    assert on_15.status_code == 200
    assert "01JA2B3C4D5E6F7G8H9J0KMNPS" in on_15.text and "08:30:00" in on_15.text
    assert "01JA2B3C4D5E6F7G8H9J0KMNPS" not in on_14.text


async def test_timeline_rejects_bad_day(client):
    assert (await client.get("/", params={"day": "yesterday"})).status_code == 422


async def test_devices_page_shows_online_device(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    await devices.record_heartbeat(pool, dev["device_id"], {"paused": True, "queue_depth": 4}, [])
    r = await client.get("/devices")
    assert r.status_code == 200
    assert "trực tuyến" in r.text and "loopi-" in r.text


async def test_commands_require_htmx_header(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    assert (await client.post(f"/devices/{dev['device_id']}/commands/pause")).status_code == 403
    r = await client.post(f"/devices/{dev['device_id']}/commands/audio-off", headers=HX)
    assert r.status_code == 200
    pending = await devices.record_heartbeat(pool, dev["device_id"], {}, [])
    assert [(c["type"], c["payload"]) for c in pending] == [("set_config", {"audio_enabled": False})]


async def test_unknown_action_or_device_is_404(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    assert (await client.post(f"/devices/{dev['device_id']}/commands/reboot", headers=HX)).status_code == 404
    assert (await client.post("/devices/NOPE/commands/pause", headers=HX)).status_code == 404


async def test_pair_token_partial_shows_token_and_ca(client):
    r = await client.post("/devices/pair-token", headers=HX)
    assert r.status_code == 200
    assert "pair_token" in r.text and "ca_fingerprint" in r.text


async def test_revoke_device(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    assert (await client.post(f"/devices/{dev['device_id']}/revoke", headers=HX)).status_code == 200
    assert await devices.get_active_device(pool, dev["fingerprint"]) is None
    assert (await client.post("/devices/NOPE/revoke", headers=HX)).status_code == 404
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_web.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'loopi_brain.web'`.

- [ ] **Step 4: Implement `brain/src/loopi_brain/web/routes.py`**

```python
"""Server-rendered web UI (Jinja + HTMX). Protected by Caddy basic auth."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import devices, pki

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
ONLINE_WINDOW = dt.timedelta(seconds=45)
COMMANDS: dict[str, tuple[str, dict]] = {
    "pause": ("pause", {}),
    "resume": ("resume", {}),
    "audio-on": ("set_config", {"audio_enabled": True}),
    "audio-off": ("set_config", {"audio_enabled": False}),
}

router = APIRouter()


def require_htmx(hx_request: str | None = Header(default=None)) -> None:
    """CSRF guard: cross-site forms cannot set custom headers."""
    if hx_request != "true":
        raise HTTPException(status_code=403, detail="missing HX-Request header")


def day_bounds_utc(day: dt.date, zone: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=zone)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=zone)
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


async def _items_by_hour(pool, day: dt.date, zone: ZoneInfo) -> dict[int, list[dict]]:
    start, end = day_bounds_utc(day, zone)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, kind, started_at, ended_at, status, view_path, meta FROM item"
            " WHERE started_at >= %s AND started_at < %s ORDER BY started_at",
            (start, end),
        )
        rows = await cur.fetchall()
    by_hour: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        local_start = row["started_at"].astimezone(zone)
        by_hour[local_start.hour].append(
            {**row, "local_start": local_start, "local_end": row["ended_at"].astimezone(zone)}
        )
    return dict(sorted(by_hour.items()))


def _ca_fingerprint(request: Request) -> str:
    return pki.fingerprint(pki.ca_cert_pem(request.app.state.settings.pki_dir))


@router.get("/", response_class=HTMLResponse)
async def timeline(request: Request, day: dt.date | None = Query(default=None)):
    zone = request.app.state.settings.zone
    day = day or dt.datetime.now(zone).date()
    hours = await _items_by_hour(request.app.state.pool, day, zone)
    return TEMPLATES.TemplateResponse(
        request,
        "timeline.html",
        {"day": day, "prev_day": day - dt.timedelta(days=1), "next_day": day + dt.timedelta(days=1), "hours": hours},
    )


@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request):
    zone = request.app.state.settings.zone
    now = dt.datetime.now(dt.UTC)
    rows = []
    for row in await devices.list_devices(request.app.state.pool):
        seen = row["last_heartbeat_at"]
        rows.append({
            **row,
            "online": seen is not None and now - seen < ONLINE_WINDOW,
            "hb": row["last_heartbeat"] or {},
            "seen_local": seen.astimezone(zone) if seen else None,
        })
    return TEMPLATES.TemplateResponse(
        request, "devices.html", {"devices": rows, "ca_fingerprint": _ca_fingerprint(request)}
    )


@router.post("/devices/pair-token", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def new_pair_token(request: Request):
    settings = request.app.state.settings
    token = await devices.create_pair_token(
        request.app.state.pool, dt.timedelta(minutes=settings.pair_token_ttl_minutes)
    )
    return TEMPLATES.TemplateResponse(
        request,
        "_pair_token.html",
        {
            "token": token["token"],
            "expires_at": token["expires_at"].astimezone(settings.zone),
            "ca_fingerprint": _ca_fingerprint(request),
        },
    )


@router.post(
    "/devices/{device_id}/commands/{action}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_htmx)],
)
async def send_command(request: Request, device_id: str, action: str):
    if action not in COMMANDS:
        raise HTTPException(status_code=404, detail="unknown action")
    type_, payload = COMMANDS[action]
    try:
        await devices.enqueue_command(request.app.state.pool, device_id, type_, payload)
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(status_code=404, detail="unknown device") from exc
    return TEMPLATES.TemplateResponse(request, "_queued.html", {"action": action})


@router.post("/devices/{device_id}/revoke", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def revoke(request: Request, device_id: str):
    if not await devices.revoke_device(request.app.state.pool, device_id):
        raise HTTPException(status_code=404, detail="unknown device")
    return TEMPLATES.TemplateResponse(request, "_queued.html", {"action": "revoked"})
```

- [ ] **Step 5: Write the templates**

`brain/src/loopi_brain/web/templates/base.html`:

```html
<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Loopi</title>
  <script src="/static/htmx.min.js"></script>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { max-width: 960px; margin: 0 auto; padding: 16px; }
    header nav a { margin-right: 16px; }
    .hour h2 { font-size: 1rem; border-bottom: 1px solid #8884; }
    .item { display: flex; gap: 12px; align-items: center; padding: 4px 0; list-style: none; }
    .item video { max-width: 320px; }
    .err { color: #c33; }
    .card { border: 1px solid #8886; border-radius: 8px; padding: 12px; margin: 12px 0; }
    .online { color: #2a2; } .offline { color: #999; }
    code { word-break: break-all; }
  </style>
</head>
<body>
  <header><nav><a href="/">Dòng thời gian</a><a href="/devices">Thiết bị</a></nav></header>
  <main>{% block content %}{% endblock %}</main>
</body>
</html>
```

`brain/src/loopi_brain/web/templates/timeline.html`:

```html
{% extends "base.html" %}
{% block content %}
<nav class="days">
  <a href="/?day={{ prev_day }}">← {{ prev_day }}</a>
  <strong>{{ day }}</strong>
  <a href="/?day={{ next_day }}">{{ next_day }} →</a>
</nav>
{% if not hours %}<p>Không có dữ liệu cho ngày này.</p>{% endif %}
{% for hour, items in hours.items() %}
<section class="hour">
  <h2>{{ "%02d:00"|format(hour) }}</h2>
  <ul>
  {% for it in items %}
    <li class="item {{ it.kind }}" id="item-{{ it.id }}">
      <time>{{ it.local_start.strftime("%H:%M:%S") }}–{{ it.local_end.strftime("%H:%M:%S") }}</time>
      {% if it.kind == "video" %}
        {% if it.status == "ready" %}<video controls preload="none" src="/media/{{ it.view_path }}"></video>
        {% elif it.status == "error" %}<span class="err">Không chuyển được video</span>
        {% else %}<span>Đang xử lý…</span>{% endif %}
      {% elif it.kind == "audio" %}
        <audio controls preload="none" src="/media/{{ it.view_path }}"></audio>
      {% else %}
        <span>★ {{ it.meta.get("note", "Đánh dấu") }}</span>
      {% endif %}
      <small>{{ it.id }}</small>
    </li>
  {% endfor %}
  </ul>
</section>
{% endfor %}
{% endblock %}
```

`brain/src/loopi_brain/web/templates/devices.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Thiết bị</h1>
<p>Fingerprint CA của brain: <code>{{ ca_fingerprint }}</code></p>
<button hx-post="/devices/pair-token" hx-target="#pair-token">Tạo token ghép cặp</button>
<div id="pair-token"></div>
{% for d in devices %}
<div class="card" id="device-{{ d.id }}">
  <strong>{{ d.name }}</strong>
  {% if d.revoked %}<span class="err">đã thu hồi</span>
  {% elif d.online %}<span class="online">trực tuyến</span>
  {% else %}<span class="offline">ngoại tuyến</span>{% endif %}
  <ul>
    <li>Lần cuối: {{ d.seen_local.strftime("%Y-%m-%d %H:%M:%S") if d.seen_local else "chưa có" }}</li>
    <li>Đang ghi hình: {{ d.hb.get("recording_video") }} · ghi âm: {{ d.hb.get("recording_audio") }}
        · tạm dừng: {{ d.hb.get("paused") }} · audio bật: {{ d.hb.get("audio_enabled") }}</li>
    <li>Hàng đợi: {{ d.hb.get("queue_depth") }} · thẻ nhớ: {{ d.hb.get("disk_used_pct") }}%
        · nhiệt độ: {{ d.hb.get("soc_temp_c") }}°C · WiFi: {{ d.hb.get("wifi_rssi_dbm") }} dBm</li>
    <li>Camera lỗi: {{ d.hb.get("camera_failures") }} lần · mic: {{ "mất" if d.hb.get("mic_missing") else "ổn" }}
        · đã phải xoá: {{ d.dropped_total }} đoạn · lỗi gần nhất: {{ d.hb.get("last_error") or "không" }}</li>
  </ul>
  {% if not d.revoked %}
  <div hx-target="#result-{{ d.id }}">
    <button hx-post="/devices/{{ d.id }}/commands/pause">Tạm dừng</button>
    <button hx-post="/devices/{{ d.id }}/commands/resume">Tiếp tục</button>
    <button hx-post="/devices/{{ d.id }}/commands/audio-off">Tắt audio</button>
    <button hx-post="/devices/{{ d.id }}/commands/audio-on">Bật audio</button>
    <button hx-post="/devices/{{ d.id }}/revoke" hx-confirm="Thu hồi thiết bị này?">Thu hồi</button>
  </div>
  <div id="result-{{ d.id }}"></div>
  {% endif %}
</div>
{% else %}
<p>Chưa có thiết bị nào.</p>
{% endfor %}
{% endblock %}
```

`brain/src/loopi_brain/web/templates/_pair_token.html`:

```html
<div class="card">
  <p>Token: <strong><code>{{ token }}</code></strong> (hết hạn {{ expires_at.strftime("%H:%M") }})</p>
  <p>Ghi vào <code>loopi.toml</code> trên thẻ SD:</p>
  <pre>[brain]
pair_token = "{{ token }}"
ca_fingerprint = "{{ ca_fingerprint }}"</pre>
</div>
```

`brain/src/loopi_brain/web/templates/_queued.html`:

```html
<span>Đã gửi lệnh: {{ action }}. Thiết bị sẽ áp dụng trong vòng 15 giây.</span>
```

- [ ] **Step 6: Wire the web router and static mounts into `create_app`**

In `brain/src/loopi_brain/app.py` add imports:

```python
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from .web.routes import router as web_router
```

and replace the last two lines of `create_app` (`app.include_router(device_router)` and `return app`) with:

```python
    app.include_router(device_router)
    app.include_router(web_router)
    settings.media_root.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=settings.media_root), name="media")
    app.mount(
        "/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static"
    )
    return app
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_web.py -v`
Expected: all passed.

- [ ] **Step 8: Commit**

```bash
git add brain/src/loopi_brain/web brain/src/loopi_brain/app.py brain/tests/test_web.py
git commit -m "feat(brain): web timeline and devices page with pause/resume/audio commands"
```

---

### Task 9: Deployment — CLI, Docker, Caddy mTLS, smoke test

**Files:**
- Modify: `brain/src/loopi_brain/cli.py` (add `migrate`, `serve`, `pair-token`)
- Create: `brain/Dockerfile`, `deploy/docker-compose.yml`, `deploy/Caddyfile`, `deploy/.env.example`, `deploy/smoke.sh`, `deploy/README.md`
- Test: `brain/tests/test_cli.py` (extend)

**Interfaces:**
- Consumes: `create_app`, `migrate`, `make_pool`, `devices.create_pair_token`, `pki.*`.
- Produces: `loopi-brain migrate | serve | pair-token | init-pki`; `pair-token` prints the token on line 1, `expires: …` on line 2, `ca_fingerprint: …` on line 3. Stack reachable at `https://<brain>:8443`.

- [ ] **Step 1: Extend the failing CLI tests in `brain/tests/test_cli.py`**

```python
def test_migrate_command(database_url, monkeypatch, capsys):
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    assert cli.main(["migrate"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_pair_token_command(database_url, pki_settings, monkeypatch, capsys):
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    monkeypatch.setenv("LOOPI_PKI_DIR", str(pki_settings.pki_dir))
    assert cli.main(["pair-token"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines[0]) == 9 and lines[0][4] == "-"
    assert lines[2].startswith("ca_fingerprint: ")
```

Run: `cd brain && uv run pytest tests/test_cli.py -v`
Expected: FAIL — `SystemExit: 2` (`invalid choice: 'migrate'`).

- [ ] **Step 2: Implement the new subcommands in `brain/src/loopi_brain/cli.py`**

Add imports:

```python
import asyncio
import datetime as dt

from . import devices
from .config import Settings
from .db import make_pool, migrate
```

In `build_parser()` before `return parser`:

```python
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("serve", help="apply migrations, then serve the API on 0.0.0.0:8000")
    sub.add_parser("pair-token", help="print a new one-time pairing token")
```

Add these functions and replace `main`:

```python
def _migrate() -> int:
    applied = migrate(Settings().database_url)
    print(f"applied: {', '.join(applied)}" if applied else "database up to date")
    return 0


def _serve() -> int:
    import uvicorn

    from .app import create_app

    settings = Settings()
    migrate(settings.database_url)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8000, proxy_headers=False)
    return 0


async def _pair_token() -> int:
    settings = Settings()
    pool = make_pool(settings.database_url)
    await pool.open()
    try:
        token = await devices.create_pair_token(
            pool, dt.timedelta(minutes=settings.pair_token_ttl_minutes)
        )
    finally:
        await pool.close()
    print(token["token"])
    print(f"expires: {token['expires_at'].astimezone(settings.zone):%Y-%m-%d %H:%M %Z}")
    print(f"ca_fingerprint: {pki.fingerprint(pki.ca_cert_pem(settings.pki_dir))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init-pki":
        return _init_pki(args)
    if args.cmd == "migrate":
        return _migrate()
    if args.cmd == "serve":
        return _serve()
    if args.cmd == "pair-token":
        return asyncio.run(_pair_token())
    return 2
```

Run: `cd brain && uv run pytest tests/test_cli.py -v`
Expected: 3 passed.

- [ ] **Step 3: Write `brain/Dockerfile`**

```dockerfile
FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["loopi-brain", "serve"]
```

- [ ] **Step 4: Write `deploy/docker-compose.yml`**

```yaml
services:
  db:
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_USER: loopi
      POSTGRES_PASSWORD: ${LOOPI_DB_PASSWORD:?set LOOPI_DB_PASSWORD in deploy/.env}
      POSTGRES_DB: loopi
    volumes:
      - db:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U loopi"]
      interval: 5s
      retries: 20
    restart: unless-stopped

  api:
    build: ../brain
    environment:
      LOOPI_DATABASE_URL: postgresql://loopi:${LOOPI_DB_PASSWORD}@db:5432/loopi
      LOOPI_MEDIA_ROOT: /srv/loopi/media
      LOOPI_PKI_DIR: /srv/loopi/pki
      LOOPI_TZ: ${LOOPI_TZ:-Asia/Seoul}
    volumes:
      - ${LOOPI_DATA_DIR:-/srv/loopi}:/srv/loopi
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

  proxy:
    image: caddy:2.10
    ports:
      - "8443:8443"
    environment:
      LOOPI_WEB_USER: ${LOOPI_WEB_USER:?set LOOPI_WEB_USER}
      LOOPI_WEB_PASSWORD_HASH: ${LOOPI_WEB_PASSWORD_HASH:?set LOOPI_WEB_PASSWORD_HASH}
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ${LOOPI_DATA_DIR:-/srv/loopi}/pki:/pki:ro
      - caddy_data:/data
    depends_on:
      - api
    restart: unless-stopped

volumes:
  db: {}
  caddy_data: {}
```

- [ ] **Step 5: Write `deploy/Caddyfile`**

```caddyfile
{
	auto_https off
	admin off
}

:8443 {
	tls /pki/server.crt /pki/server.key {
		client_auth {
			mode verify_if_given
			trust_pool file {
				pem_file /pki/ca.crt
			}
		}
	}

	# Device API: identity comes only from the verified client certificate.
	@device path /v1/*
	handle @device {
		request_body {
			max_size 80MB
		}
		reverse_proxy api:8000 {
			header_up X-Loopi-Client-Fingerprint {http.request.tls.client.fingerprint}
		}
	}

	# Web UI and media: basic auth; never trust a client-supplied fingerprint header.
	handle {
		basic_auth {
			{$LOOPI_WEB_USER} {$LOOPI_WEB_PASSWORD_HASH}
		}
		reverse_proxy api:8000 {
			header_up -X-Loopi-Client-Fingerprint
		}
	}
}
```

- [ ] **Step 6: Write `deploy/.env.example`**

```dotenv
# Copy to deploy/.env (git-ignored) and fill in.
LOOPI_DB_PASSWORD=change-me
LOOPI_DATA_DIR=/srv/loopi
LOOPI_TZ=Asia/Seoul
LOOPI_WEB_USER=admin
# Generate with: docker run --rm caddy:2.10 caddy hash-password --plaintext 'your-password'
# Keep the single quotes: bcrypt hashes contain '$'.
LOOPI_WEB_PASSWORD_HASH='paste-the-bcrypt-hash-here'
```

- [ ] **Step 7: Write `deploy/smoke.sh` (end-to-end check against the running stack)**

```bash
#!/usr/bin/env bash
# End-to-end check of a running brain: pairing, mTLS upload, duplicate, spoofing, heartbeat, web auth.
set -euo pipefail
cd "$(dirname "$0")"
BASE="${LOOPI_SMOKE_URL:-https://localhost:8443}"
PKI="${LOOPI_DATA_DIR:-/srv/loopi}/pki"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

expect() {  # expect <http-code> <curl args...>
  local want="$1"; shift
  local got
  got="$(curl -s -o "$WORK/body" -w '%{http_code}' --cacert "$PKI/ca.crt" "$@")"
  if [[ "$got" != "$want" ]]; then
    echo "FAIL: expected $want, got $got for: $*" >&2
    cat "$WORK/body" >&2
    exit 1
  fi
}

TOKEN="$(docker compose exec -T api loopi-brain pair-token | head -n1 | tr -d '\r')"
openssl ecparam -name prime256v1 -genkey -noout -out "$WORK/device.key"
openssl req -new -key "$WORK/device.key" -subj "/CN=smoke" -out "$WORK/device.csr"
python3 - "$TOKEN" "$WORK/device.csr" > "$WORK/pair.json" <<'PY'
import json, sys
print(json.dumps({"token": sys.argv[1], "csr_pem": open(sys.argv[2]).read(),
                  "hw_info": {"model": "smoke", "serial": "0", "agent_version": "0.1.0"}}))
PY
expect 201 -H 'Content-Type: application/json' --data @"$WORK/pair.json" "$BASE/v1/pair"
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["cert_pem"], end="")' "$WORK/body" > "$WORK/device.crt"
FP="$(openssl x509 -in "$WORK/device.crt" -outform DER | sha256sum | cut -d' ' -f1)"
MTLS=(--cert "$WORK/device.crt" --key "$WORK/device.key")

ID="$(python3 -c 'import secrets,time
A="0123456789ABCDEFGHJKMNPQRSTVWXYZ"; t=int(time.time()*1000)
print("".join(A[(t>>(5*i))&31] for i in range(9,-1,-1))+"".join(secrets.choice(A) for _ in range(16)))')"
printf '{"note":"smoke"}' > "$WORK/marker.json"
SHA="$(sha256sum "$WORK/marker.json" | cut -d' ' -f1)"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ITEM=(-X PUT -H "Content-Type: application/json" -H "X-Loopi-Kind: marker"
      -H "X-Loopi-Start: $NOW" -H "X-Loopi-End: $NOW" -H "X-Loopi-Sha256: $SHA"
      --data-binary @"$WORK/marker.json")

expect 201 "${MTLS[@]}" "${ITEM[@]}" "$BASE/v1/items/$ID"
expect 200 "${MTLS[@]}" "${ITEM[@]}" "$BASE/v1/items/$ID"
expect 401 -H "X-Loopi-Client-Fingerprint: $FP" "${ITEM[@]}" "$BASE/v1/items/$ID"

HB='{"agent_version":"0.1.0","uptime_s":1,"disk_used_pct":1.0,"queue_depth":0,"clock_synced":true,"paused":false,"audio_enabled":true,"recording_video":false,"recording_audio":false,"acked_command_ids":[]}'
expect 200 "${MTLS[@]}" -H 'Content-Type: application/json' --data "$HB" "$BASE/v1/heartbeat"
expect 401 "$BASE/"
echo "OK: smoke test passed (device fingerprint $FP)"
```

Run: `chmod +x deploy/smoke.sh`

- [ ] **Step 8: Write `deploy/README.md`**

```markdown
# Loopi brain — deployment

Requirements: Docker with Compose v2 on the Linux PC, port 8443 reachable on the LAN.

1. `cp deploy/.env.example deploy/.env` and fill in the values (password hash command is in the file).
2. Create the data dir: `sudo mkdir -p /srv/loopi && sudo chown "$USER" /srv/loopi`.
3. Create the CA and server cert (use the PC's LAN IP; keep `localhost` for the smoke test):
   `cd deploy && docker compose run --rm --no-deps api loopi-brain init-pki --host localhost --host brain.lan --ip 192.168.1.10`
   Write down the printed CA fingerprint.
4. `docker compose up -d --build`
5. `./smoke.sh` → must print `OK: smoke test passed`.
6. Browser: import `/srv/loopi/pki/ca.crt` into the OS trust store, then open `https://<LAN IP>:8443/`
   and log in with `LOOPI_WEB_USER`.
7. Devices page → "Tạo token ghép cặp" → copy `pair_token` and `ca_fingerprint` into the device's `loopi.toml`.
```

- [ ] **Step 9: Build and run the stack, then the smoke test**

Run (on the Linux PC, from the repo root):
```bash
cd deploy && cp -n .env.example .env   # then edit .env
docker compose run --rm --no-deps api loopi-brain init-pki --host localhost --ip "$(hostname -I | cut -d' ' -f1)"
docker compose up -d --build
./smoke.sh
```
Expected: `OK: smoke test passed (device fingerprint …)`. If the spoof step returns anything other than `401`, stop: the Caddyfile is not overwriting the header.

- [ ] **Step 10: Run the whole suite with coverage**

Run: `cd brain && uv run pytest --cov=loopi_brain --cov-report=term-missing --cov-fail-under=80`
Expected: all tests pass, total coverage ≥ 80%.

- [ ] **Step 11: Commit**

```bash
git add brain/src/loopi_brain/cli.py brain/tests/test_cli.py brain/Dockerfile deploy
git commit -m "feat(deploy): docker compose stack with Caddy mTLS, smoke test and CLI commands"
```
