"""Command line entry point: ``loopi-brain <command>``."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
import time
from pathlib import Path

# Subcommands import what they need: `worker-health` runs every 30 s and must stay light.
DEFAULT_PKI_DIR = "/srv/loopi/pki"
EVAL_POLL_S = 10.0  # how often `eval run` looks whether the worker finished the run


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
    evaluation = sub.add_parser("eval", help="evaluate the pipeline on the owner's golden set (spec §13)")
    eval_sub = evaluation.add_subparsers(dest="eval_cmd", required=True)
    run = eval_sub.add_parser("run", help="ask the worker for a run, wait for it and print its numbers")
    run.add_argument("--max-gain-db", type=float, help="the ASR gain ceiling for this run only")
    run.add_argument("--vad-threshold", type=float, help="the VAD threshold for this run only")
    run.add_argument("--no-wait", action="store_true", help="print the run id and return")
    run.add_argument("--timeout", type=float, default=3 * 3600, help="seconds to wait (default 3 h)")
    show = eval_sub.add_parser("show", help="print the numbers of a run (default: the newest)")
    show.add_argument("run_id", nargs="?")
    compare = eval_sub.add_parser("compare", help="two finished runs side by side (the noise floor, a tuning change)")
    compare.add_argument("first")
    compare.add_argument("second")
    impact = eval_sub.add_parser("rules-impact", help="what each rule would drop from the live transcripts (counts)")
    impact.add_argument("--days", type=int, default=7, help="transcripts written in the last DAYS days (default 7)")
    acceptance = sub.add_parser("acceptance", help="print the P2 acceptance numbers (spec §14) as Markdown")
    acceptance.add_argument("--days", type=int, default=7, help="complete local days before today (default 7)")
    acceptance.add_argument("--cpu-samples", help="the cgroup CPU samples of the measurement window ('-': stdin)")
    acceptance.add_argument("--exclude-day", action="append", default=[], type=dt.date.fromisoformat,
                            help="a local day (YYYY-MM-DD) left out of the latency, e.g. one reprocessed (repeatable)")
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


async def _eval(args: argparse.Namespace) -> int:
    from .config import Settings
    from .db import make_pool
    from .evaluation import runner
    from .pipeline.jobs import open_jobs

    settings = Settings()
    pool = make_pool(settings.database_url)
    await pool.open()
    try:
        if args.eval_cmd == "show":
            run = await runner.latest_run(pool, args.run_id)
            if run is None:
                print("no evaluation run yet" if args.run_id is None else f"no run {args.run_id}")
                return 1
            print(runner.format_run(run, settings.zone))
            return 0
        if args.eval_cmd == "compare":
            runs = [await runner.latest_run(pool, run_id) for run_id in (args.first, args.second)]
            if None in runs:
                print("no such run")
                return 1
            print(runner.format_compare(*runs))
            return 0
        if args.eval_cmd == "rules-impact":
            since = dt.datetime.now(dt.UTC) - dt.timedelta(days=max(args.days, 1))
            print(runner.format_rules_impact(await runner.rules_impact(pool, since)))
            return 0
        try:
            async with open_jobs(pool):
                run_id, created = await runner.start_run(
                    pool, settings, max_gain_db=args.max_gain_db, vad_threshold=args.vad_threshold
                )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"{'started' if created else 'already waiting or running'}: {run_id}")
        if args.no_wait:
            return 0
        deadline = time.monotonic() + args.timeout
        while (run := await runner.latest_run(pool, run_id))["status"] not in ("done", "failed"):
            if time.monotonic() > deadline:
                print(f"still {run['status']} ({run['heard']}/{run['clips']} clips); see `loopi-brain eval show {run_id}`")
                return 1
            await asyncio.sleep(EVAL_POLL_S)
        print(runner.format_run(run, settings.zone))
        return 0 if run["status"] == "done" else 1
    finally:
        await pool.close()


async def _acceptance(args: argparse.Namespace) -> int:
    from .acceptance import report
    from .config import Settings
    from .db import make_pool

    if args.days < 1:
        print("error: --days must be at least 1", file=sys.stderr)
        return 2
    samples = None
    if args.cpu_samples == "-":
        samples = sys.stdin.read()
    elif args.cpu_samples:
        samples = Path(args.cpu_samples).read_text()
    settings = Settings()
    pool = make_pool(settings.database_url)
    await pool.open()
    try:
        print(await report(pool, settings.zone, args.days, samples, exclude=args.exclude_day), end="")
    finally:
        await pool.close()
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
    if args.cmd == "eval":
        return asyncio.run(_eval(args))
    if args.cmd == "acceptance":
        return asyncio.run(_acceptance(args))
    return 2
