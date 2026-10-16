import contextlib
import datetime as dt

import pytest

from helpers import FakeOllama, captioned_keyframe, fetch_all, insert_item, insert_moment, paired_device
from loopi_brain import search
from loopi_brain.evaluation import questions
from loopi_brain.providers.config import DEFAULT_CONFIG
from loopi_brain.providers.router import build_router
from loopi_brain.search import Hit

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
DAY = dt.date(2026, 10, 14)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


def _hit(moment_id, kind, ts, item_id=None, keyframe_id=None) -> Hit:
    return Hit(moment_id, kind, ts, "x", None, keyframe_id, item_id, None, None, None, 0.0)


async def test_questions_are_checked_and_kept(pool):
    qid = await questions.create_question(pool, "  Hôm qua mình\nhẹn ai?\x00 ", DAY, DAY)
    [row] = await questions.list_questions(pool)
    assert (row["id"], row["text"], row["first_day"], row["last_day"], row["answers"]) == (
        qid, "Hôm qua mình hẹn ai?", DAY, DAY, 0)
    for text, first, last in ((" \x00 ", None, None), ("Khi nào?", DAY, DAY - dt.timedelta(days=1))):
        with pytest.raises(ValueError):
            await questions.create_question(pool, text, first, last)
    assert len((await questions.get_question(pool, await questions.create_question(pool, "x" * 900, None, None)))
               ["text"]) == questions.TEXT_MAX
    assert await questions.update_question(pool, qid, "Sáng nay mình làm gì?", None, None)
    assert (await questions.get_question(pool, qid))["first_day"] is None
    assert await questions.delete_question(pool, qid) and not await questions.delete_question(pool, qid)
    assert await questions.get_question(pool, qid) is None and not await questions.update_question(pool, qid, "x", None, None)


async def test_a_correct_moment_keeps_what_it_came_from(pool, pki_settings, dev):
    d = dev["device_id"]
    kf = await captioned_keyframe(pool, pki_settings, d, NINE)
    video = (await fetch_all(pool, "SELECT item_id FROM keyframe"))[0]["item_id"]
    visual = await insert_moment(pool, d, kind="visual", ts=NINE, item_id=video, text="A person cooks")
    async with pool.connection() as conn:
        await conn.execute("UPDATE moment SET keyframe_id = %s WHERE id = %s", (kf, visual))
    audio = await insert_item(pool, d, kind="audio", started_at=NINE)
    speech = await insert_moment(pool, d, kind="speech", ts=NINE + dt.timedelta(seconds=5), seconds=4, item_id=audio,
                                 text="Mai đi chợ")
    qid = await questions.create_question(pool, "Ai nấu ăn?", None, None)
    assert await questions.add_answer(pool, qid, visual) and await questions.add_answer(pool, qid, speech)
    assert await questions.add_answer(pool, qid, speech)  # picked twice: still one correct moment
    assert not await questions.add_answer(pool, qid, "01NOSUCHMOMENT000000000000")
    assert not await questions.add_answer(pool, "01NOSUCHQUESTION0000000000", visual)
    answers = (await questions.get_question(pool, qid))["answers"]
    assert [(a["moment_id"], a["kind"], a["item_id"], a["keyframe_id"], a["text"]) for a in answers] == [
        (visual, "visual", video, kf, "A person cooks"), (speech, "speech", audio, None, "Mai đi chợ")]
    assert answers[1]["ts_end"] - answers[1]["ts_start"] == dt.timedelta(seconds=4)
    assert (await questions.counts(pool)) == {"questions": 1, "answered": 1}
    async with pool.connection() as conn:  # the day is reprocessed: the moments go, the answers stay
        await conn.execute("DELETE FROM moment")
    assert [a["text"] for a in (await questions.get_question(pool, qid))["answers"]] == [None, None]
    assert await questions.remove_answer(pool, qid, visual) and not await questions.remove_answer(pool, qid, visual)
    assert [q["id"] for q in await questions.answered(pool)] == [qid]


class _PoolWhoseSecondStatementFollowsABeat:
    """A pool whose connections run ``beat`` (the owner acting in another session) just before their second statement."""

    def __init__(self, pool, beat):
        self._pool, self._beat = pool, beat

    @contextlib.asynccontextmanager
    async def connection(self):
        async with self._pool.connection() as conn:
            yield _BeforeTheSecond(conn, self._beat)


class _BeforeTheSecond:
    def __init__(self, conn, beat):
        self._conn, self._beat, self._calls = conn, beat, 0

    async def execute(self, *args, **kwargs):
        self._calls += 1
        if self._calls == 2:
            await self._beat()
        return await self._conn.execute(*args, **kwargs)


async def test_a_question_whose_last_correct_moment_goes_between_the_two_reads_is_not_labelled_yet(pool, dev):
    d = dev["device_id"]
    gone_moment = await insert_moment(pool, d, kind="marker", ts=NINE)
    kept_moment = await insert_moment(pool, d, kind="marker", ts=NINE)
    gone = await questions.create_question(pool, "Ai nấu ăn?", None, None)
    kept = await questions.create_question(pool, "Mai đi đâu?", None, None)
    assert await questions.add_answer(pool, gone, gone_moment) and await questions.add_answer(pool, kept, kept_moment)

    async def the_owner_removes_the_last_correct_moment():
        assert await questions.remove_answer(pool, gone, gone_moment)

    racing = _PoolWhoseSecondStatementFollowsABeat(pool, the_owner_removes_the_last_correct_moment)
    assert [(q["id"], [a["moment_id"] for a in q["answers"]]) for q in await questions.answered(racing)] == [
        (kept, [kept_moment])]
    assert [q["id"] for q in await questions.answered(pool)] == [kept]


def test_an_answer_is_recognised_after_its_day_is_reprocessed():
    speech = {"moment_id": "M1", "kind": "speech", "item_id": "I1", "keyframe_id": None,
              "ts_start": NINE + dt.timedelta(seconds=5), "ts_end": NINE + dt.timedelta(seconds=9)}
    picture = {"moment_id": "M2", "kind": "visual", "item_id": "V1", "keyframe_id": "K1", "ts_start": NINE, "ts_end": NINE}
    marker = {"moment_id": "M3", "kind": "marker", "item_id": "K9", "keyframe_id": None, "ts_start": NINE, "ts_end": NINE}
    assert questions.matches(speech, _hit("M1", "speech", NINE))  # the same moment
    assert questions.matches(speech, _hit("N1", "speech", NINE + dt.timedelta(seconds=4), item_id="I1"))
    assert questions.matches(speech, _hit("N1", "speech", NINE + dt.timedelta(seconds=11), item_id="I1"))
    assert not questions.matches(speech, _hit("N1", "speech", NINE + dt.timedelta(seconds=12), item_id="I1"))
    assert not questions.matches(speech, _hit("N2", "speech", NINE + dt.timedelta(seconds=6), item_id="I2"))
    assert questions.matches(picture, _hit("N3", "visual", NINE, item_id="V1", keyframe_id="K1"))
    assert not questions.matches(picture, _hit("N4", "visual", NINE, item_id="V1", keyframe_id="K2"))
    assert questions.matches(marker, _hit("N5", "marker", NINE, item_id="K9"))
    assert not questions.matches(marker, _hit("N6", "speech", NINE, item_id="K9"))
    hits = [_hit("X", "visual", NINE), _hit("N1", "speech", NINE + dt.timedelta(seconds=5), item_id="I1"),
            _hit("Y", "speech", NINE), _hit("N3", "visual", NINE, keyframe_id="K1")]
    assert questions.ranks([speech, picture, marker], hits) == [2, 4]
    after = {**speech, "moment_id": "M4", "ts_start": NINE + dt.timedelta(seconds=10),
             "ts_end": NINE + dt.timedelta(seconds=14)}  # the next speech region of the same recording
    inside_both = _hit("N7", "speech", NINE + dt.timedelta(seconds=9), item_id="I1")
    assert questions.ranks([speech, after], [inside_both]) == [1]  # one moment found, not two
    assert questions.ranks([speech, after], [inside_both, _hit("N8", "speech", NINE + dt.timedelta(seconds=12),
                                                               item_id="I1")]) == [1, 2]


async def test_the_labelling_search_shows_more_than_the_live_one_and_lets_the_owner_browse(pool, pki_settings, dev):
    d = dev["device_id"]
    router = build_router(DEFAULT_CONFIG, pki_settings, FakeOllama().client())  # the question is the axis-0 vector
    for n in range(60):
        await insert_moment(pool, d, kind="speech", ts=NINE + dt.timedelta(minutes=n), axis=1, text=f"đi chợ lần {n}")
    unrelated = await insert_moment(pool, d, kind="visual", ts=NINE + dt.timedelta(days=1), axis=2, text="A dark room")
    live = await search.search_moments(pool, router, "đi chợ", limit=50)
    assert len(live.hits) == search.CANDIDATES  # the live search keeps 40 per search
    found = await questions.label_search(pool, router, "đi chợ", None, None, pki_settings.zone)
    assert len(found) == questions.LABEL_LIMIT
    next_day = DAY + dt.timedelta(days=1)
    start, end = (NINE + dt.timedelta(hours=15), NINE + dt.timedelta(hours=39))  # that day in Seoul
    assert (await search.search_moments(pool, router, "căn phòng", start=start, end=end)).hits == []  # too far
    assert [h.id for h in await questions.label_search(pool, router, "căn phòng", next_day, next_day,
                                                       pki_settings.zone)] == [unrelated]  # meaning, loosely
    day = await questions.label_search(pool, router, "", DAY, DAY, pki_settings.zone)
    assert len(day) == 60 and [h.ts_start for h in day] == sorted(h.ts_start for h in day)
    assert [h.id for h in await questions.label_search(pool, router, " ", next_day, None,
                                                       pki_settings.zone)] == [unrelated]
    assert await questions.label_search(pool, router, "", None, None, pki_settings.zone) == []
    assert len(await questions.label_search(pool, None, "đi chợ", DAY, DAY, pki_settings.zone)) == 50  # words only
