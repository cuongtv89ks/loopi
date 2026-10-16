import base64
import datetime as dt
import io
import json
import traceback

import psycopg
import pytest
from PIL import Image

from helpers import (
    DEFAULT_CAPTION,
    T0,
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_item,
    insert_keyframe,
    item_headers,
    jobs_by_lock,
    make_ctx,
    new_ulid,
    paired_device,
    run_jobs,
    write_jpeg,
)
from loopi_brain.pipeline import caption, dispatch
from loopi_brain.pipeline.caption import CAPTION_SCHEMA, Caption, CaptionInvalid, caption_text
from loopi_brain.providers.base import ProviderError


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
async def keyframe(pool, pki_settings, dev):
    item_id = await insert_item(pool, dev["device_id"], kind="video")
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done' WHERE id = %s", (item_id,))
    return await insert_keyframe(pool, pki_settings, item_id, dev["device_id"], T0)


def test_caption_text_reads_like_a_sentence():
    c = Caption(scene="A kitchen.", activity="Someone is cooking", people_count=2, objects=["pan", " ", "kettle"],
                text_seen="EXIT", notable=False)
    assert caption_text(c) == 'A kitchen. Someone is cooking. 2 people. Objects: pan, kettle. Text: "EXIT".'
    one = Caption(scene="A desk", activity="nobody", people_count=1, objects=[], text_seen="", notable=True)
    assert caption_text(one) == "A desk. nobody. 1 person."
    empty = Caption(scene=" ", activity="", people_count=0, objects=[], text_seen="", notable=False)
    assert caption_text(empty) == "Unremarkable scene."


def test_prepare_image_caps_the_longer_side(tmp_path):
    big = caption.prepare_image(write_jpeg(tmp_path / "big.jpg", (1920, 1080)))
    small = caption.prepare_image(write_jpeg(tmp_path / "small.jpg", (640, 360)))
    with Image.open(io.BytesIO(big)) as img:
        assert (img.format, img.size) == ("JPEG", (896, 504))
    with Image.open(io.BytesIO(small)) as img:
        assert img.size == (640, 360)


async def test_caption_is_stored_with_its_model_and_a_visual_moment(pool, pki_settings, dev, keyframe, jobs_open):
    fake = FakeOllama()
    assert await caption.caption_keyframe(make_ctx(pool, pki_settings, ollama=fake), keyframe) is True
    kf = await fetch_one(pool, "SELECT caption, caption_model, captioned_at, caption_error FROM keyframe WHERE id = %s",
                         (keyframe,))
    assert kf["caption"] == DEFAULT_CAPTION and kf["caption_model"] == "ollama:minicpm-v4.6:1b"
    assert kf["captioned_at"] is not None and kf["caption_error"] is None
    [moment] = await fetch_all(pool, "SELECT * FROM moment")
    assert (moment["kind"], moment["lang"], moment["keyframe_id"], moment["device_id"]) == (
        "visual", "en", keyframe, dev["device_id"])
    assert moment["ts_start"] == moment["ts_end"] == T0
    assert moment["text"] == caption_text(Caption(**DEFAULT_CAPTION))
    assert moment["model_version"] == "ollama:minicpm-v4.6:1b" and moment["embedding"] is None
    [(path, body)] = fake.requests
    assert path == "/api/chat" and body["format"] == CAPTION_SCHEMA and body["model"] == "minicpm-v4.6:1b"
    assert body["options"] == {"temperature": 0.0, "num_predict": 400}  # greedy, bounded output
    assert CAPTION_SCHEMA["properties"]["objects"]["maxItems"] == 20
    with Image.open(io.BytesIO(base64.b64decode(body["messages"][0]["images"][0]))) as img:
        assert img.size == (896, 504)
    assert "embed" in await jobs_by_lock(pool)
    assert await caption.caption_keyframe(make_ctx(pool, pki_settings, ollama=fake), keyframe) is False  # done already
    assert len(fake.requests) == 1


async def test_overlong_answers_are_cut_to_the_limits_not_rejected(pool, pki_settings, keyframe):
    rambling = {**DEFAULT_CAPTION, "scene": "a room " * 100, "people_count": 99,
                "objects": [f"chair {i}" for i in range(25)]}
    ctx = make_ctx(pool, pki_settings, ollama=FakeOllama(caption=rambling))
    assert await caption.caption_keyframe(ctx, keyframe) is True
    stored = (await fetch_one(pool, "SELECT caption FROM keyframe WHERE id = %s", (keyframe,)))["caption"]
    assert len(stored["scene"]) == 300 and stored["people_count"] == 50
    assert stored["objects"] == [f"chair {i}" for i in range(20)]


@pytest.mark.parametrize(
    "answer",
    [
        "not json at all",
        {**DEFAULT_CAPTION, "people_count": "many"},
        {**DEFAULT_CAPTION, "objects": "chair"},
        {k: v for k, v in DEFAULT_CAPTION.items() if k != "notable"},
    ],
)
async def test_invalid_answers_raise_and_keep_the_job(pool, pki_settings, keyframe, jobs_open, answer):
    await dispatch.defer_keyframe(pool, keyframe)
    with pytest.raises(CaptionInvalid):
        await caption.caption_keyframe(make_ctx(pool, pki_settings, ollama=FakeOllama(caption=answer)), keyframe)
    kf = await fetch_one(pool, "SELECT caption, caption_job_id, caption_error FROM keyframe WHERE id = %s", (keyframe,))
    assert kf["caption"] is None and kf["caption_job_id"] is not None
    assert kf["caption_error"].startswith("CaptionInvalid: ")
    assert await fetch_all(pool, "SELECT id FROM moment") == []


# What a caption model can see in a home: a legible paper, a name on a screen. None of it may reach an error text.
PRIVATE_CAPTION = {**DEFAULT_CAPTION, "scene": "Mẹ đọc đơn thuốc của bác sĩ Lan", "text_seen": "Huyết áp 150/95"}
PRIVATE_WORDS = ("Mẹ", "đơn thuốc", "bác sĩ", "Lan", "Huyết", "150/95")
WRONG_TYPES = [
    pytest.param(json.dumps(PRIVATE_CAPTION, ensure_ascii=False)[:60],
                 "the answer is not JSON (Unterminated string starting at, character 57)", id="cut-off"),
    pytest.param({**PRIVATE_CAPTION, "people_count": "Mẹ và bác sĩ Lan"},
                 "the answer does not fit (people_count: int_parsing)", id="wrong-number"),
    pytest.param({**PRIVATE_CAPTION, "objects": "đơn thuốc"},
                 "the answer does not fit (objects: list_type)", id="wrong-list"),
]


@pytest.mark.parametrize(("answer", "message"), WRONG_TYPES)
def test_an_invalid_caption_says_where_and_why_but_never_quotes_the_answer(answer, message):
    text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    with pytest.raises(CaptionInvalid) as caught:
        caption.parse_caption(text)
    assert str(caught.value) == f"{message}; answer of {len(text)} characters"
    assert caught.value.__cause__ is None and caught.value.__context__ is None  # nothing chained that quotes it
    printed = "".join(traceback.format_exception(caught.value))
    assert not any(word in printed for word in PRIVATE_WORDS)


@pytest.mark.parametrize(("answer", "message"), WRONG_TYPES)
async def test_an_invalid_caption_is_stored_without_the_answer(pool, pki_settings, keyframe, jobs_open, answer, message):
    await dispatch.defer_keyframe(pool, keyframe)
    with pytest.raises(CaptionInvalid) as caught:
        await caption.caption_keyframe(make_ctx(pool, pki_settings, ollama=FakeOllama(caption=answer)), keyframe)
    stored = (await fetch_one(pool, "SELECT caption_error FROM keyframe WHERE id = %s", (keyframe,)))["caption_error"]
    assert stored.startswith(f"CaptionInvalid: {message}; answer of ")
    assert not any(word in stored for word in PRIVATE_WORDS)
    printed = "".join(traceback.format_exception(caught.value))  # what the worker logs
    assert not any(word in printed for word in PRIVATE_WORDS)


async def test_a_retried_caption_job_samples_instead_of_repeating_itself(pool, pki_settings, keyframe, jobs_open):
    fake = FakeOllama()
    job_id = await dispatch.defer_keyframe(pool, keyframe)
    async with pool.connection() as conn:  # as after one failed attempt
        await conn.execute("UPDATE procrastinate_jobs SET attempts = 1 WHERE id = %s", (job_id,))
    await run_jobs(make_ctx(pool, pki_settings, ollama=fake))
    [(_path, body)] = [r for r in fake.requests if r[0] == "/api/chat"]
    assert body["options"]["temperature"] == 0.3


async def test_server_errors_use_the_normal_retries(pool, pki_settings, keyframe, jobs_open):
    await dispatch.defer_keyframe(pool, keyframe)
    fake = FakeOllama()
    fake.error = 500
    with pytest.raises(ProviderError, match="HTTP 500"):
        await caption.caption_keyframe(make_ctx(pool, pki_settings, ollama=fake), keyframe)
    kf = await fetch_one(pool, "SELECT caption_job_id, caption_error FROM keyframe WHERE id = %s", (keyframe,))
    assert kf["caption_job_id"] is not None and "HTTP 500" in kf["caption_error"]


async def test_missing_caption_model_waits_like_an_outage(pool, pki_settings, keyframe, jobs_open):
    await dispatch.defer_keyframe(pool, keyframe)
    ctx = make_ctx(pool, pki_settings, ollama=FakeOllama(models=[]))
    assert await caption.caption_keyframe(ctx, keyframe) is False  # no exception: no retry is burned
    kf = await fetch_one(pool, "SELECT caption, caption_job_id, caption_error FROM keyframe WHERE id = %s", (keyframe,))
    assert kf["caption"] is None and kf["caption_job_id"] is None
    assert kf["caption_error"].startswith("ProviderUnavailable: ") and "HTTP 404" in kf["caption_error"]


async def test_model_server_outage_dead_letters_nothing_and_captions_resume(
    client, pool, pki_settings, dev, keyframe, jobs_open
):
    fake = FakeOllama()
    fake.down = True
    ctx = make_ctx(pool, pki_settings, ollama=fake)
    await dispatch.defer_keyframe(pool, keyframe)  # as analyze_video does right after extracting it
    await run_jobs(ctx)  # the job meets the outage: it gives the keyframe back without burning a retry
    for _tick in range(3):  # three dispatcher ticks while the model server is down: no new job at all
        assert await dispatch.dispatch_pending(pool, ctx.router) == (0, 0)
        await run_jobs(ctx)
        kf = await fetch_one(pool, "SELECT caption, caption_job_id, caption_error FROM keyframe WHERE id = %s",
                             (keyframe,))
        assert kf["caption"] is None and kf["caption_job_id"] is None
        assert kf["caption_error"].startswith("ProviderUnavailable: ")
    jobs = await fetch_all(pool, "SELECT status FROM procrastinate_jobs WHERE task_name = 'caption_keyframe'")
    assert [j["status"] for j in jobs] == ["succeeded"]
    body = json.dumps({"note": "still recording"}).encode()
    r = await client.put(f"/v1/items/{new_ulid()}", content=body,
                         headers=item_headers(dev["fingerprint"], body, kind="marker", end="2026-10-14T01:00:00Z"))
    assert r.status_code == 201  # ingest does not care about the model server

    fake.down = False  # the model server is back: the next tick captions the keyframe
    assert await dispatch.dispatch_pending(pool, ctx.router) == (0, 1)
    await run_jobs(ctx)
    kf = await fetch_one(pool, "SELECT caption, caption_error FROM keyframe WHERE id = %s", (keyframe,))
    assert kf["caption"] == DEFAULT_CAPTION and kf["caption_error"] is None
    assert [r["kind"] for r in await fetch_all(pool, "SELECT kind FROM moment WHERE keyframe_id = %s", (keyframe,))] == ["visual"]
    failed = await fetch_all(pool, "SELECT id FROM procrastinate_jobs WHERE status = 'failed'")
    assert failed == []


async def test_keyframe_deleted_while_the_model_answers_stores_nothing(pool, pki_settings, keyframe, jobs_open, monkeypatch):
    ctx = make_ctx(pool, pki_settings)
    real_chat = ctx.router.chat

    async def chat_while_deleted(*args, **kwargs):
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM keyframe WHERE id = %s", (keyframe,))
        return await real_chat(*args, **kwargs)

    monkeypatch.setattr(ctx.router, "chat", chat_while_deleted)
    assert await caption.caption_keyframe(ctx, keyframe) is False
    assert await fetch_all(pool, "SELECT id FROM moment") == []
    assert "embed" not in await jobs_by_lock(pool)


async def test_keyframe_deleted_with_its_jpeg_is_a_no_op(pool, pki_settings, dev, keyframe, monkeypatch):
    fake = FakeOllama()
    ctx = make_ctx(pool, pki_settings, ollama=fake)
    row = await fetch_one(pool, "SELECT item_id, path FROM keyframe WHERE id = %s", (keyframe,))
    async with pool.connection() as conn:  # gone before the job starts
        await conn.execute("DELETE FROM keyframe WHERE id = %s", (keyframe,))
    (pki_settings.media_root / row["path"]).unlink()
    assert await caption.caption_keyframe(ctx, keyframe) is False
    later = await insert_keyframe(pool, pki_settings, row["item_id"], dev["device_id"], T0 + dt.timedelta(minutes=1))
    real_prepare = caption.prepare_image

    def vanish_then_read(path):  # gone while the job runs: the JPEG first, then the row
        path.unlink()
        with psycopg.connect(pki_settings.database_url, autocommit=True) as conn:
            conn.execute("DELETE FROM keyframe WHERE id = %s", (later,))
        return real_prepare(path)

    monkeypatch.setattr(caption, "prepare_image", vanish_then_read)
    assert await caption.caption_keyframe(ctx, later) is False  # FileNotFoundError, but the row is gone too
    assert fake.requests == [] and await fetch_all(pool, "SELECT id FROM moment") == []
