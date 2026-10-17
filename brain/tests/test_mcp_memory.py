import datetime as dt
import io

import pytest
from PIL import Image

from helpers import FakeOllama, captioned_keyframe, insert_chapter, insert_moment, paired_device
from loopi_brain import search, store, thumbs
from loopi_brain.mcp import memory
from loopi_brain.providers.config import DEFAULT_CONFIG
from loopi_brain.providers.router import build_router

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
DAY = "2026-10-14"
LATE = dt.datetime(2026, 10, 14, 15, 30, tzinfo=dt.UTC)  # 00:30 on the 15th in Seoul: the UTC day is the 14th


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
def router(pki_settings):
    return build_router(DEFAULT_CONFIG, pki_settings, FakeOllama().client())


async def test_search_answers_moments_with_local_times_their_day_and_end(pool, pki_settings, dev, router):
    moment = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, seconds=4, axis=0, lang="vi",
                                 text="Mai mình đi chợ")
    found = await memory.search_memories(pool, router, pki_settings, "đi chợ", None, None, 10)
    assert found.not_found is False and found.semantic is True
    [ref] = found.moments
    assert (ref.moment_id, ref.kind, ref.lang, ref.day) == (moment, "speech", "vi", DAY)
    assert (ref.ts_start, ref.ts_end) == ("2026-10-14T09:00:00+09:00", "2026-10-14T09:00:04+09:00")
    assert ref.text == "Mai mình đi chợ"


async def test_search_without_the_embedding_model_matches_words(pool, pki_settings, dev):
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, text="Mai mình đi chợ")
    found = await memory.search_memories(pool, None, pki_settings, "đi chợ", None, None, 10)
    assert found.semantic is False and len(found.moments) == 1


async def test_search_finds_nothing_and_says_so(pool, pki_settings, dev, router):
    found = await memory.search_memories(pool, router, pki_settings, "không có gì", None, None, 10)
    assert (found.moments, found.not_found) == ([], True)


async def test_a_date_alone_is_the_whole_local_day_and_a_naive_time_is_local(pool, pki_settings, dev, router):
    zone = pki_settings.zone
    assert memory.parse_when("2026-10-14", zone, end=False) == dt.datetime(2026, 10, 13, 15, 0, tzinfo=dt.UTC)
    assert memory.parse_when("2026-10-14", zone, end=True) == dt.datetime(2026, 10, 14, 15, 0, tzinfo=dt.UTC)
    assert memory.parse_when("2026-10-14T08:00", zone, end=False) == dt.datetime(2026, 10, 13, 23, 0, tzinfo=dt.UTC)
    assert memory.parse_when("2026-10-14T08:00:00+00:00", zone, end=True) == dt.datetime(2026, 10, 14, 8, 0, tzinfo=dt.UTC)
    assert memory.parse_when(None, zone, end=True) is None and memory.parse_when("  ", zone, end=True) is None
    inside = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(hours=14), text="đi chợ")
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(days=1), text="đi chợ")
    found = await memory.search_memories(pool, None, pki_settings, "đi chợ", DAY, DAY, 10)  # 23:00 on the 14th counts
    assert [m.moment_id for m in found.moments] == [inside]


@pytest.mark.parametrize(("start", "end", "message"), [
    ("2026-10-15", "2026-10-14", "start must be before end"),
    ("yesterday-SECRET", None, "start and end must be YYYY-MM-DD or ISO 8601 date-times"),
    ("2026-13-45", None, "date must be YYYY-MM-DD"),
])
async def test_unusable_bounds_are_fixed_errors(pool, pki_settings, start, end, message):
    with pytest.raises(memory.BadInput) as exc:
        await memory.search_memories(pool, None, pki_settings, "x", start, end, 10)
    assert str(exc.value) == message


async def test_an_empty_query_is_a_fixed_error(pool, pki_settings):
    with pytest.raises(memory.BadInput, match="^query is empty$"):
        await memory.search_memories(pool, None, pki_settings, " \x00 ", None, None, 10)


async def test_search_keeps_the_limit_between_1_and_50(pool, pki_settings, monkeypatch):
    asked = []

    async def fake_search(_pool, _router, _query, **kwargs):
        asked.append(kwargs["limit"])
        return search.SearchResult([], semantic=False)

    monkeypatch.setattr(memory.search, "search_moments", fake_search)
    for limit in (500, 0, 7):
        await memory.search_memories(pool, None, pki_settings, "x", None, None, limit)
    assert asked == [50, 1, 7]


async def test_the_timeline_has_chapters_markers_and_counts(pool, pki_settings, dev):
    zone = pki_settings.zone
    day = dt.date(2026, 10, 14)
    chapter = await insert_chapter(pool, day, NINE, NINE + dt.timedelta(minutes=30), title="Buổi sáng", summary="S" * 600)
    marker = await insert_moment(pool, dev["device_id"], kind="marker", ts=NINE + dt.timedelta(minutes=5), text="Đánh dấu")
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(minutes=6), text="x")
    await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(days=1), text="other day")
    timeline = await memory.get_timeline(pool, DAY, zone)
    assert timeline.day == DAY and timeline.counts == {"visual": 0, "speech": 1, "marker": 1}
    [c] = timeline.chapters
    assert (c.id, c.title, c.closed, c.ts_start) == (chapter, "Buổi sáng", True, "2026-10-14T09:00:00+09:00")
    assert len(c.summary) == memory.TEXT_MAX
    assert [(m.moment_id, m.text) for m in timeline.markers] == [(marker, "Đánh dấu")]
    empty = await memory.get_timeline(pool, "2026-10-20", zone)
    assert (empty.chapters, empty.markers, empty.counts) == ([], [], {"visual": 0, "speech": 0, "marker": 0})


async def test_a_moment_just_after_local_midnight_belongs_to_the_next_local_day(pool, pki_settings, dev):
    """Days are local (LOOPI_TZ): every other fixture sits between 00:00 and 14:59 UTC, where the local and the UTC day agree."""
    zone = pki_settings.zone
    spoken = await insert_moment(pool, dev["device_id"], kind="speech", ts=LATE, seconds=30, axis=0, text="đi chợ")
    marker = await insert_moment(pool, dev["device_id"], kind="marker", ts=LATE, text="Đánh dấu")
    found = await memory.search_memories(pool, None, pki_settings, "đi chợ", None, None, 10)
    [ref] = found.moments
    assert (ref.moment_id, ref.day, ref.ts_start) == (spoken, "2026-10-15", "2026-10-15T00:30:00+09:00")
    out, _ = await memory.get_moment(pool, pki_settings, spoken, images=False)
    assert (out.day, out.ts_start, out.ts_end) == ("2026-10-15", "2026-10-15T00:30:00+09:00", "2026-10-15T00:30:30+09:00")
    on_15 = await memory.get_timeline(pool, "2026-10-15", zone)
    on_14 = await memory.get_timeline(pool, DAY, zone)
    assert on_15.counts == {"visual": 0, "speech": 1, "marker": 1}
    assert [(m.moment_id, m.ts_start) for m in on_15.markers] == [(marker, "2026-10-15T00:30:00+09:00")]
    assert on_14.counts == {"visual": 0, "speech": 0, "marker": 0} and on_14.markers == []
    window = await memory.search_memories(pool, None, pki_settings, "đi chợ", DAY, DAY, 10)  # the 14th, local
    assert window.moments == []
    window = await memory.search_memories(pool, None, pki_settings, "đi chợ", "2026-10-15", "2026-10-15", 10)
    assert [m.moment_id for m in window.moments] == [spoken]


@pytest.mark.parametrize("bad", ["2026-13-45", "tomorrow", "2026-1-5", ""])
async def test_a_day_must_be_yyyy_mm_dd(pool, pki_settings, bad):
    with pytest.raises(memory.BadInput, match="^date must be YYYY-MM-DD$"):
        await memory.get_timeline(pool, bad, pki_settings.zone)


async def test_the_recap_or_that_there_is_none(pool, pki_settings):
    zone = pki_settings.zone
    missing = await memory.get_day_recap(pool, DAY, zone)
    assert (missing.exists, missing.markdown, missing.generated_at, missing.stale) == (False, None, None, False)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO day_recap (day, markdown, model_version, generated_at, stale) VALUES (%s, %s, 'm', %s, true)",
            (dt.date(2026, 10, 14), "# Ngày\n" + "x" * 9000, NINE),
        )
    recap = await memory.get_day_recap(pool, DAY, zone)
    assert recap.exists and recap.stale and recap.generated_at == "2026-10-14T09:00:00+09:00"
    assert recap.markdown.startswith("# Ngày") and len(recap.markdown) == memory.MARKDOWN_MAX


async def test_a_spoken_moment_comes_back_with_its_text_clipped(pool, pki_settings, dev):
    moment = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, seconds=3, lang="ko", text="말" * 700)
    out, picture = await memory.get_moment(pool, pki_settings, moment.lower(), images=True)
    assert (out.moment_id, out.kind, out.lang, out.caption, out.image, picture) == (moment, "speech", "ko", None, False, None)
    assert len(out.text) == memory.TEXT_MAX and out.ts_end == "2026-10-14T09:00:03+09:00"


async def test_a_visual_moment_sends_its_picture_only_when_allowed(pool, pki_settings, dev):
    caption = {"scene": "kitchen", "activity": "cooking", "people_count": 2, "objects": ["pot", 3, "spoon"],
               "text_seen": "", "notable": False}
    keyframe = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE, caption=caption)
    moment = await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, text="Two people cook",
                                 keyframe_id=keyframe)
    out, picture = await memory.get_moment(pool, pki_settings, moment, images=False)
    assert (out.image, picture) == (False, None)
    assert out.caption == memory.CaptionOut(scene="kitchen", activity="cooking", objects=["pot", "spoon"], people_count=2)
    out, picture = await memory.get_moment(pool, pki_settings, moment, images=True)
    assert out.image is True
    with Image.open(io.BytesIO(picture)) as img:
        assert (img.format, img.width) == ("JPEG", thumbs.THUMB_WIDTH)


async def test_a_golden_keyframe_is_never_sent(pool, pki_settings, dev):
    keyframe = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    moment = await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, text="x", keyframe_id=keyframe)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO eval_caption (id, keyframe_id, path) VALUES ('S1', %s, 'keyframes/S1.jpg')",
                           (keyframe,))
    out, picture = await memory.get_moment(pool, pki_settings, moment, images=True)
    assert (out.image, picture) == (False, None)


async def test_a_missing_keyframe_file_only_drops_the_picture(pool, pki_settings, dev):
    keyframe = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    moment = await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, text="x", keyframe_id=keyframe)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT path FROM keyframe WHERE id = %s", (keyframe,))
        (pki_settings.media_root / (await cur.fetchone())["path"]).unlink()
    out, picture = await memory.get_moment(pool, pki_settings, moment, images=True)
    assert (out.image, picture, out.text) == (False, None, "x")


@pytest.mark.parametrize("bad", ["01NOSUCHMOMENT000000000000", "not-an-id", "", "x" * 3000])
async def test_an_unknown_moment_is_a_fixed_error(pool, pki_settings, bad):
    with pytest.raises(memory.NotFound, match="^no such moment$"):
        await memory.get_moment(pool, pki_settings, bad, images=False)


async def test_a_well_formed_id_that_matches_no_moment_is_the_same_fixed_error(pool, pki_settings):
    with pytest.raises(memory.NotFound, match="^no such moment$"):
        await memory.get_moment(pool, pki_settings, "01ARZ3NDEKTSV4RRFFQ69G5FAV", images=False)


async def test_a_visual_moment_without_a_keyframe_has_no_caption_and_no_picture(pool, pki_settings, dev):
    moment = await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, text="x")
    out, picture = await memory.get_moment(pool, pki_settings, moment, images=True)
    assert (out.kind, out.caption, out.image, picture) == ("visual", None, False, None)


async def test_commitments_newest_first_and_filtered(pool):
    async with pool.connection() as conn:
        for n, (day, status) in enumerate([(14, "open"), (15, "done"), (16, "open")]):
            await conn.execute(
                "INSERT INTO commitment (id, day, text, status, model_version) VALUES (%s, %s, %s, %s, 'm')",
                (f"C{n}", dt.date(2026, 10, day), f"việc {n}", status),
            )
    every = await memory.list_commitments(pool, None)
    assert [c.id for c in every.commitments] == ["C2", "C1", "C0"]
    assert every.commitments[0].day == "2026-10-16" and every.commitments[0].source_moment_id is None
    assert [c.id for c in (await memory.list_commitments(pool, "open")).commitments] == ["C2", "C0"]
    with pytest.raises(memory.BadInput, match="^status must be open, done or dismissed$"):
        await memory.list_commitments(pool, "SECRET")


async def test_commitments_stop_at_200(pool):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO commitment (id, day, text, status, model_version)"
            " SELECT 'C' || n, DATE '2026-10-14', 'x', 'open', 'm' FROM generate_series(1, 205) AS n"
        )
    assert len((await memory.list_commitments(pool, None)).commitments) == memory.COMMITMENTS_MAX


async def test_the_picture_switch_is_off_until_turned_on(pool):
    assert (await store.get_mcp(pool)).images is False
    await store.set_mcp(pool, store.McpSettings(images=True))
    assert (await store.get_mcp(pool)).images is True
    assert (await store.get_retention(pool)).enabled is False  # the retention setting is a separate row


async def test_an_unknown_keyframe_has_no_thumbnail(pool, pki_settings):
    with pytest.raises(thumbs.ThumbUnavailable, match="^unknown keyframe$"):
        await thumbs.keyframe_thumb(pool, pki_settings.media_root, "01NOSUCHKEYFRAME0000000000")
