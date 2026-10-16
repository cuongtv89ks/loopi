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
    ``error = 500`` to make chat and embed requests fail with that HTTP status. ``reply``, if set, is called
    with each /api/chat request body and returns the answer (a str, or a dict sent as JSON); by default
    every chat answers with ``caption``.
    """

    def __init__(self, caption: dict | str | None = None, models: list[str] | None = None, dims: int = 1024):
        self.down = False
        self.error: int | None = None
        self.caption = DEFAULT_CAPTION if caption is None else caption
        self.models = list(DEFAULT_OLLAMA_MODELS if models is None else models)
        self.dims = dims
        self.reply = None
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
            answer = self.caption if self.reply is None else self.reply(body)
            content = answer if isinstance(answer, str) else json.dumps(answer)
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


T0 = dt.datetime(2026, 10, 14, 1, 0, tzinfo=dt.UTC)


def new_ulid() -> str:
    from ulid import ULID

    return str(ULID())


async def insert_item(
    pool,
    device_id: str,
    *,
    kind: str = "video",
    started_at: dt.datetime = T0,
    seconds: float = 60,
    item_id: str | None = None,
    raw_path: str | None = None,
    status: str = "ready",
    meta: dict | None = None,
) -> str:
    """Insert an item row directly (no file is written unless the test does it); return its id."""
    from psycopg.types.json import Jsonb

    item_id = item_id or new_ulid()
    ext = {"video": "h264", "audio": "opus", "marker": "json"}[kind]
    raw_path = raw_path or f"raw/{device_id}/{started_at:%Y/%m/%d}/{item_id}.{ext}"
    if meta is None:
        meta = {"video_fps": 2} if kind == "video" else {}
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes, raw_path, meta, status)"
            " VALUES (%s, %s, %s, %s, %s, 'x', 1, %s, %s, %s)",
            (item_id, device_id, kind, started_at, started_at + dt.timedelta(seconds=seconds), raw_path,
             Jsonb(meta), status),
        )
    return item_id


async def insert_moment(
    pool,
    device_id: str,
    *,
    kind: str = "marker",
    ts: dt.datetime | None = None,
    text: str = "x",
    lang: str | None = None,
) -> str:
    """Insert a moment row without an embedding (both timestamps = ``ts``, default now); return its id."""
    moment_id = new_ulid()
    ts = ts or dt.datetime.now(dt.UTC)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, lang, model_version)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, 'test')",
            (moment_id, device_id, ts, ts, kind, text, lang),
        )
    return moment_id


async def fetch_all(pool, sql: str, params: tuple = ()) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(pool, sql: str, params: tuple = ()) -> dict | None:
    rows = await fetch_all(pool, sql, params)
    return rows[0] if rows else None


async def stall_jobs(pool, *job_ids: int, attempts: int = 0) -> None:
    """Make ``job_ids`` look like jobs a worker was running when it died (no heartbeat for 5 minutes)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now() - interval '5 minutes') RETURNING id"
        )
        await conn.execute(
            "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s, attempts = %s WHERE id = ANY(%s)",
            ((await cur.fetchone())["id"], attempts, list(job_ids)),
        )


async def jobs_by_lock(pool) -> dict[str, list[dict]]:
    """Procrastinate jobs grouped by queueing lock."""
    grouped: dict[str, list[dict]] = {}
    for row in await fetch_all(pool, "SELECT * FROM procrastinate_jobs ORDER BY id"):
        grouped.setdefault(row["queueing_lock"], []).append(row)
    return grouped


def make_h264(path, source: str, seconds: float, fps: int = 2):
    """Encode an ffmpeg lavfi ``source`` (e.g. ``testsrc2=size=640x360``) as raw Annex-B H.264."""
    import shutil
    import subprocess

    import pytest

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required for video tests")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source, "-t", str(seconds),
         "-r", str(fps), "-c:v", "libx264", "-g", str(fps * 2), "-pix_fmt", "yuv420p",
         "-bsf:v", "h264_mp4toannexb", "-f", "h264", str(path)],
        check=True,
    )
    return path


# 40 s of testsrc2 then 40 s of SMPTE bars at 640x360: two scenes, one hard cut at frame 80.
TWO_SCENES = (
    "testsrc2=size=640x360:rate=2:duration=40[a];"
    "smptebars=size=640x360:rate=2:duration=40[b];[a][b]concat=n=2:v=1:a=0"
)
NIGHT = "color=c=0x101010:size=640x360:rate=2"


def make_ctx(pool, settings, *, ollama=None, transcriber=None):
    """A PipelineContext whose Ollama is ``ollama`` (a FakeOllama; a fresh one if None)."""
    from loopi_brain.pipeline.context import PipelineContext
    from loopi_brain.providers.config import DEFAULT_CONFIG
    from loopi_brain.providers.router import build_router

    ollama = ollama or FakeOllama()
    return PipelineContext(pool, settings, build_router(DEFAULT_CONFIG, settings, ollama.client()), transcriber)


async def run_jobs(ctx) -> None:
    """Run every due job with a real Procrastinate worker, then return (periodic tasks off).

    Needs the job app open on ``ctx.pool`` (the ``jobs_open`` fixture).
    """
    from procrastinate.periodic import PeriodicRegistry

    from loopi_brain.pipeline import context
    from loopi_brain.pipeline.jobs import job_app

    registry = job_app.periodic_registry
    job_app.periodic_registry = PeriodicRegistry()
    context.set_context(ctx)
    try:
        await job_app.run_worker_async(wait=False, concurrency=1, install_signal_handlers=False, listen_notify=False)
    finally:
        context.set_context(None)
        job_app.periodic_registry = registry


def write_jpeg(path, size=(1280, 720), color=(90, 120, 150)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


async def insert_keyframe(pool, settings, item_id: str, device_id: str, ts: dt.datetime, *, size=(1280, 720)) -> str:
    """Insert an uncaptioned keyframe row with a real JPEG on disk; return its id."""
    kf_id = new_ulid()
    rel = f"keyframes/{device_id}/{ts:%Y/%m/%d}/{kf_id}.jpg"
    write_jpeg(settings.media_root / rel, size)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO keyframe (id, item_id, device_id, ts, frame_index, path, dhash, luma)"
            " VALUES (%s, %s, %s, %s, (SELECT count(*) FROM keyframe WHERE item_id = %s), %s, 0, 100)",
            (kf_id, item_id, device_id, ts, item_id, rel),
        )
    return kf_id


# Shared ffmpeg lavfi test signals (make_opus ``source``) for the audio-level tests.
QUIET_SPEECH = "aevalsrc=0.02*sin(2*PI*440*t)*mod(floor(2*t)\\,2):s=16000"  # -34 dBFS bursts, silence between
ROOM_NOISE = "anoisesrc=colour=white:amplitude=0.003:sample_rate=16000:seed=1"  # -55 dBFS RMS, like the USB mic


def make_opus(path, source: str = "anullsrc=r=16000:cl=mono", seconds: float = 5):
    """Encode an ffmpeg lavfi audio ``source`` (default: silence) as Ogg/Opus, like the device does."""
    import shutil
    import subprocess

    import pytest

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required for audio tests")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source, "-t", str(seconds),
         "-c:a", "libopus", "-f", "ogg", str(path)],
        check=True,
    )
    return path


class FakeTranscriber:
    """Stands in for Whisper: returns the scripted RegionResults in order, cycling. Records each chunk's
    length (``chunks``) and largest |sample| (``peaks``)."""

    def __init__(self, *results):
        self.results = list(results)
        self.chunks: list[int] = []
        self.peaks: list[float] = []

    def transcribe(self, chunk):
        self.chunks.append(len(chunk))
        self.peaks.append(float(abs(chunk).max()) if len(chunk) else 0.0)
        return self.results[(len(self.chunks) - 1) % len(self.results)]
