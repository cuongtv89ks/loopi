"""The `loopi-brain worker` process: context, job app and the Procrastinate worker loop."""
from __future__ import annotations

import logging
from collections.abc import Callable

import anyio
import httpx

from ..config import Settings
from ..db import make_pool, migrate
from ..providers.config import load_providers
from ..providers.ollama import DEFAULT_TIMEOUT, OllamaProvider
from ..providers.router import build_router
from .asr import WHISPER_SIZE, WhisperTranscriber
from .context import PipelineContext, set_context
from .dispatch import recover_stalled
from .jobs import open_jobs

log = logging.getLogger(__name__)


async def run_worker(settings: Settings, **options) -> None:
    """Migrate, build the pipeline context and run jobs until stopped.

    ``options`` go to ``App.run_worker_async`` (tests pass ``wait=False`` to return once no job is left).
    """
    await anyio.to_thread.run_sync(migrate, settings.database_url)
    config = load_providers(settings.providers_file)  # a bad providers.yaml stops the worker here
    http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    pool = make_pool(settings.database_url)
    await pool.open()
    try:
        set_context(PipelineContext(pool, settings, build_router(config, settings, http), WhisperTranscriber(settings)))
        async with open_jobs(pool) as app:
            try:
                await recover_stalled(pool)  # jobs of a worker that died more than 30 s ago run again now
            except Exception:  # never keeps the worker down; the periodic retry_stalled_jobs tries again
                log.exception("could not recover stalled jobs; starting the worker anyway")
            await app.run_worker_async(**{"concurrency": settings.worker_concurrency, **options})
    finally:
        set_context(None)
        await pool.close()
        await http.aclose()


def download_whisper(settings: Settings) -> None:
    from faster_whisper import download_model

    download_model(WHISPER_SIZE, cache_dir=str(settings.model_dir / "whisper"))


async def pull_models(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    download: Callable[[Settings], None] = download_whisper,
    report: Callable[[str], None] = print,
) -> list[str]:
    """Pull every Ollama model the providers config names, then download the Whisper model."""
    config = load_providers(settings.providers_file)
    ollama = OllamaProvider(settings.ollama_url, keep_alive=settings.ollama_keep_alive, client=client)
    pulled: list[str] = []
    try:
        for model in config.ollama_models():
            report(f"pulling {model} …")
            await ollama.pull(model)
            pulled.append(model)
    finally:
        if client is None:
            await ollama.aclose()
    report(f"downloading faster-whisper {WHISPER_SIZE} into {settings.model_dir / 'whisper'} …")
    await anyio.to_thread.run_sync(download, settings)
    pulled.append(f"faster-whisper/{WHISPER_SIZE}")
    return pulled
