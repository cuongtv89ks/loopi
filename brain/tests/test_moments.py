import datetime as dt

import pytest

from helpers import (
    T0,
    FakeOllama,
    fetch_all,
    fetch_one,
    insert_item,
    insert_moment,
    jobs_by_lock,
    make_ctx,
    paired_device,
)
from loopi_brain.pipeline import moments
from loopi_brain.providers.base import ProviderError


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.mark.parametrize(
    ("meta", "text"),
    [({"note": " chìa khoá "}, "chìa khoá"), ({}, "Marked moment"), ({"note": "  "}, "Marked moment"),
     ({"note": 42}, "Marked moment")],
)
async def test_marker_becomes_one_moment(pool, pki_settings, dev, jobs_open, meta, text):
    item_id = await insert_item(pool, dev["device_id"], kind="marker", seconds=0, meta=meta)
    ctx = make_ctx(pool, pki_settings)
    assert await moments.index_marker(ctx, item_id) is True
    assert await moments.index_marker(ctx, item_id) is False  # already done
    [moment] = await fetch_all(pool, "SELECT * FROM moment")
    assert (moment["kind"], moment["text"], moment["item_id"], moment["ts_start"]) == ("marker", text, item_id, T0)
    assert moment["model_version"] == "loopi:marker-v1" and moment["lang"] is None
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item == {"analysis_state": "done", "analysis_error": None}
    assert "embed" in await jobs_by_lock(pool)


async def test_reindexing_a_marker_replaces_its_moment(pool, pki_settings, dev, jobs_open):
    item_id = await insert_item(pool, dev["device_id"], kind="marker", seconds=0)
    ctx = make_ctx(pool, pki_settings)
    await moments.index_marker(ctx, item_id)
    async with pool.connection() as conn:  # e.g. a crash before the job was acknowledged
        await conn.execute("UPDATE item SET analysis_state = 'pending' WHERE id = %s", (item_id,))
    assert await moments.index_marker(ctx, item_id) is True
    assert len(await fetch_all(pool, "SELECT id FROM moment")) == 1


async def test_other_items_are_not_markers(pool, pki_settings, dev):
    video = await insert_item(pool, dev["device_id"], kind="video")
    assert await moments.index_marker(make_ctx(pool, pki_settings), video) is False
    assert await moments.index_marker(make_ctx(pool, pki_settings), "01NOSUCHITEM0000000000000") is False


async def test_marker_failure_is_recorded_on_the_item(pool, pki_settings, dev, monkeypatch):
    item_id = await insert_item(pool, dev["device_id"], kind="marker", seconds=0)

    def boom(_meta):
        raise RuntimeError("bad marker")

    monkeypatch.setattr(moments, "marker_text", boom)
    with pytest.raises(RuntimeError):
        await moments.index_marker(make_ctx(pool, pki_settings), item_id)
    item = await fetch_one(pool, "SELECT analysis_state, analysis_error FROM item WHERE id = %s", (item_id,))
    assert item == {"analysis_state": "pending", "analysis_error": "RuntimeError: bad marker"}


async def test_embed_pending_embeds_everything_in_batches(pool, pki_settings, dev):
    for n in range(70):
        await insert_moment(pool, dev["device_id"], text=f"moment {n}", ts=T0 + dt.timedelta(minutes=n))
    fake = FakeOllama()
    assert await moments.embed_pending(make_ctx(pool, pki_settings, ollama=fake)) == 70
    assert [len(body["input"]) for _, body in fake.requests] == [32, 32, 6]
    assert all(body["model"] == "qwen3-embedding:0.6b" for _, body in fake.requests)
    rows = await fetch_all(pool, "SELECT embedding, embed_model FROM moment")
    assert all(r["embed_model"] == "ollama:qwen3-embedding:0.6b" and len(r["embedding"].to_list()) == 1024 for r in rows)
    assert await moments.embed_pending(make_ctx(pool, pki_settings, ollama=fake)) == 0


async def test_embedding_pauses_while_the_model_server_is_down(pool, pki_settings, dev, caplog):
    await insert_moment(pool, dev["device_id"], text="hello")
    fake = FakeOllama()
    fake.down = True
    assert await moments.embed_pending(make_ctx(pool, pki_settings, ollama=fake)) == 0
    assert "embedding paused" in caplog.text
    assert (await fetch_one(pool, "SELECT embedding FROM moment"))["embedding"] is None


async def test_wrong_dimensions_fail_the_embedding_job(pool, pki_settings, dev):
    await insert_moment(pool, dev["device_id"], text="hello")
    with pytest.raises(ProviderError, match="1024"):
        await moments.embed_pending(make_ctx(pool, pki_settings, ollama=FakeOllama(dims=768)))
