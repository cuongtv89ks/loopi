"""Command line entry point: ``loopi-brain <command>``."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

from . import devices, pki
from .config import Settings
from .db import make_pool, migrate

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


def _migrate() -> int:
    applied = migrate(Settings().database_url)
    print(f"applied: {', '.join(applied)}" if applied else "database up to date")
    return 0


def _serve() -> int:  # pragma: no cover - blocks serving; exercised by deploy/smoke.sh
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
