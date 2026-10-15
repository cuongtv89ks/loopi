"""Per-process pipeline context: what every job needs, set once by the worker."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool

from ..config import Settings
from ..providers.router import Router

if TYPE_CHECKING:
    from .asr import Transcriber


@dataclass
class PipelineContext:
    pool: AsyncConnectionPool
    settings: Settings
    router: Router
    transcriber: Transcriber


_context: PipelineContext | None = None


def set_context(ctx: PipelineContext | None) -> None:
    global _context
    _context = ctx


def get_context() -> PipelineContext:
    if _context is None:
        raise RuntimeError("pipeline context not set: jobs run only inside `loopi-brain worker`")
    return _context
