"""Command line entry point: ``loopi-brain <command>``."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

# Subcommands import what they need: `worker-health` runs every 30 s and must stay light.
DEFAULT_PKI_DIR = "/srv/loopi/pki"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loopi-brain")
    sub = parser.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init-pki", help="create the brain CA and server certificate")
    init.add_argument("--pki-dir", default=os.environ.get("LOOPI_PKI_DIR", DEFAULT_PKI_DIR))
    init.add_argument("--host", action="append", default=[], help="DNS name (repeatable)")
    init.add_argument("--ip", action="append", default=[], help="IP address (repeatable)")
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("serve", help="apply migrations, then serve the API on 0.0.0.0:8000")
    sub.add_parser("pair-token", help="print a new one-time pairing token")
    sub.add_parser("worker", help="apply migrations, then run the pipeline worker")
    sub.add_parser("worker-health", help="exit 0 if a worker sent a heartbeat in the last 60 s")
    models = sub.add_parser("models", help="AI models")
    models_sub = models.add_subparsers(dest="models_cmd", required=True)
    models_sub.add_parser("pull", help="download the Ollama models in providers.yaml and the Whisper model")
    return parser


def _init_pki(args: argparse.Namespace) -> int:
    from . import pki

    pki_dir = Path(args.pki_dir)
    try:
        pki.init_pki(pki_dir, args.host, args.ip)
    except pki.PkiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"CA fingerprint: {pki.fingerprint(pki.ca_cert_pem(pki_dir))}")
    return 0


def _migrate() -> int:
    from .config import Settings
    from .db import migrate

    applied = migrate(Settings().database_url)
    print(f"applied: {', '.join(applied)}" if applied else "database up to date")
    return 0


def _serve() -> int:  # pragma: no cover - blocks serving; exercised by deploy/smoke.sh
    import uvicorn

    from .app import create_app
    from .config import Settings
    from .db import migrate

    settings = Settings()
    migrate(settings.database_url)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8000, proxy_headers=False)
    return 0


async def _pair_token() -> int:
    from . import devices, pki
    from .config import Settings
    from .db import make_pool

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


def _worker() -> int:  # pragma: no cover - runs until stopped; exercised by deploy/smoke.sh
    from .config import Settings
    from .pipeline.worker import run_worker

    asyncio.run(run_worker(Settings()))
    return 0


def _worker_health() -> int:
    from .health import worker_alive

    alive = worker_alive(os.environ["LOOPI_DATABASE_URL"])
    print("worker alive" if alive else "no worker heartbeat in the last 60 s")
    return 0 if alive else 1


def _models_pull() -> int:
    from .config import Settings
    from .pipeline.worker import pull_models

    pulled = asyncio.run(pull_models(Settings()))
    print(f"ready: {', '.join(pulled)}")
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
    if args.cmd == "worker":
        return _worker()
    if args.cmd == "worker-health":
        return _worker_health()
    if args.cmd == "models":
        return _models_pull()
    return 2
