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
