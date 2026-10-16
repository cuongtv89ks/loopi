"""caption_keyframe: describe one keyframe with the vision model (spec §9.2 step 5)."""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Annotated

import anyio
from PIL import Image
from procrastinate import JobContext
from psycopg.types.json import Jsonb
from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator

from .. import devices
from ..llm import parse_answer
from ..providers.base import ProviderUnavailable
from . import dispatch, jobs
from .context import PipelineContext, get_context
from .jobs import job_app
from .state import error_text, keyframe_exists, record_keyframe_error

log = logging.getLogger(__name__)
MAX_SIDE = 896
JPEG_QUALITY = 85
CAPTION_PROMPT = (
    "This is one frame from a camera that stands still in a home. Describe it for a personal diary, in English. "
    "Answer with JSON: scene = the place and what is visible, one short sentence; activity = what the people "
    "are doing, or 'nobody' when nobody is visible; people_count = number of people visible; objects = up to 10 "
    "notable objects; text_seen = legible text, or an empty string; notable = true only if something unusual "
    "or memorable is happening."
)
MAX_TEXT = 300
MAX_OBJECTS = 20
MAX_PEOPLE = 50
# Output tokens per caption: a model that loops ("chair", "chair", …) stops here instead of filling its context.
CAPTION_MAX_TOKENS = 400
RETRY_TEMPERATURE = 0.3  # a retry samples instead of repeating the same greedy answer
# Sent as Ollama's `format`: the decoder is constrained to this shape and these limits. Caption below
# applies the same limits to whatever comes back, truncating instead of rejecting.
CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string", "maxLength": MAX_TEXT},
        "activity": {"type": "string", "maxLength": MAX_TEXT},
        "people_count": {"type": "integer", "minimum": 0, "maximum": MAX_PEOPLE},
        "objects": {"type": "array", "items": {"type": "string", "maxLength": MAX_TEXT}, "maxItems": MAX_OBJECTS},
        "text_seen": {"type": "string", "maxLength": MAX_TEXT},
        "notable": {"type": "boolean"},
    },
    "required": ["scene", "activity", "people_count", "objects", "text_seen", "notable"],
}


def _clip_text(value):
    return value[:MAX_TEXT] if isinstance(value, str) else value


Short = Annotated[str, BeforeValidator(_clip_text)]


class Caption(BaseModel):
    """A caption within the limits: over-long text and lists are cut, the people count is clamped;
    only unparseable JSON, a missing field or a wrong type is invalid."""

    model_config = ConfigDict(extra="ignore")

    scene: Short
    activity: Short
    people_count: int
    objects: list[Short]
    text_seen: Short
    notable: bool

    @field_validator("people_count")
    @classmethod
    def _clamp_people(cls, value: int) -> int:
        return min(max(value, 0), MAX_PEOPLE)

    @field_validator("objects")
    @classmethod
    def _cap_objects(cls, value: list[str]) -> list[str]:
        return value[:MAX_OBJECTS]


class CaptionInvalid(Exception):
    """The model answered, but not with a valid caption (retried like any job error)."""


def parse_caption(text: str) -> Caption:
    """The caption model's JSON answer; CaptionInvalid says where and why it is not one, never what it said
    (the error is stored on the keyframe, shown on "Xử lý" and logged: a caption describes the home)."""
    return parse_answer(text, Caption, CaptionInvalid)


def caption_text(c: Caption) -> str:
    """The searchable text of a visual moment."""
    parts = [c.scene.strip(), c.activity.strip()]
    if c.people_count:
        parts.append("1 person" if c.people_count == 1 else f"{c.people_count} people")
    objects = [o.strip() for o in c.objects if o.strip()]
    if objects:
        parts.append("Objects: " + ", ".join(objects))
    if c.text_seen.strip():
        parts.append(f'Text: "{c.text_seen.strip()}"')
    text = ". ".join(p.rstrip(".") for p in parts if p)
    return f"{text}." if text else "Unremarkable scene."


def prepare_image(path: Path) -> bytes:
    """The keyframe as JPEG (quality 85), downscaled so its longer side is at most 896 px."""
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


async def _caption(ctx: PipelineContext, keyframe_id: str, attempt: int) -> bool:
    pool = ctx.pool
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, item_id, device_id, ts, path, caption FROM keyframe WHERE id = %s", (keyframe_id,)
        )
        kf = await cur.fetchone()
    if kf is None or kf["caption"] is not None:
        return False
    image = await anyio.to_thread.run_sync(prepare_image, ctx.settings.media_root / kf["path"])
    try:
        result = await ctx.router.chat(
            "caption",
            [{"role": "user", "content": CAPTION_PROMPT}],
            images=[image],
            json_schema=CAPTION_SCHEMA,
            temperature=0.0 if attempt == 0 else RETRY_TEMPERATURE,
            max_tokens=CAPTION_MAX_TOKENS,
        )
    except ProviderUnavailable as exc:
        # The model server is down: give the job back without burning a retry; the
        # dispatcher defers this keyframe again on its next tick.
        log.warning("keyframe %s: model server unavailable, will retry later: %s", keyframe_id, exc)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE keyframe SET caption_job_id = NULL, caption_error = %s WHERE id = %s",
                (error_text(exc), keyframe_id),
            )
        return False
    caption = parse_caption(result.text)
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "UPDATE keyframe SET caption = %s, caption_model = %s, captioned_at = now(), caption_error = NULL"
            " WHERE id = %s AND caption IS NULL RETURNING id",
            (Jsonb(caption.model_dump()), result.model_version, keyframe_id),
        )
        if await cur.fetchone() is None:  # deleted (or captioned) while the model was answering
            return False
        await conn.execute(
            "INSERT INTO moment (id, device_id, ts_start, ts_end, kind, text, lang, item_id, keyframe_id, model_version)"
            " VALUES (%s, %s, %s, %s, 'visual', %s, 'en', %s, %s, %s)",
            (devices.new_id(), kf["device_id"], kf["ts"], kf["ts"], caption_text(caption), kf["item_id"],
             keyframe_id, result.model_version),
        )
    await dispatch.defer_embed(pool)
    return True


async def caption_keyframe(ctx: PipelineContext, keyframe_id: str, attempt: int = 0) -> bool:
    """Caption one keyframe and add its visual moment; False if there was nothing to do (or no model server).

    ``attempt`` is the job's previous attempts: retries sample at a higher temperature.
    """
    try:
        return await _caption(ctx, keyframe_id, attempt)
    except Exception as exc:
        if not await keyframe_exists(ctx.pool, keyframe_id):  # deleted while we worked (e.g. its JPEG went first)
            log.info("keyframe %s was deleted during captioning: %s", keyframe_id, error_text(exc))
            return False
        await record_keyframe_error(ctx.pool, keyframe_id, exc)
        raise


@job_app.task(name=jobs.CAPTION_KEYFRAME, queue="caption", retry=jobs.RETRY, pass_context=True)
async def caption_keyframe_job(context: JobContext, keyframe_id: str) -> None:
    await caption_keyframe(get_context(), keyframe_id, attempt=context.job.attempts)
