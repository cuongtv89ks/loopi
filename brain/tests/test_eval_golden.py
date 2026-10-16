import datetime as dt
import logging
import traceback
from pathlib import Path

import httpx
import pytest

from helpers import (
    QUIET_SPEECH,
    FakeOllama,
    analysed_audio,
    captioned_keyframe,
    fetch_all,
    fetch_one,
    insert_moment,
    insert_segment,
    make_ctx,
    paired_device,
)
from loopi_brain import forget
from loopi_brain.app import create_app
from loopi_brain.evaluation import golden
from loopi_brain.evaluation.golden import GoldenError
from loopi_brain.pipeline import context, retention

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
THREE_IN_SEOUL = int(dt.datetime(2026, 10, 14, 18, 0, tzinfo=dt.UTC).timestamp())
VI = golden.AudioLabel("vi", False, "Mai mình đi chợ nhé")


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


async def _spoken(pool, settings, device_id, start, lang, text, avg_logprob=-0.3, **kw) -> str:
    item_id = await analysed_audio(pool, settings, device_id, start, **kw)
    await insert_segment(pool, item_id, device_id, start, text, lang=lang, avg_logprob=avg_logprob)
    return item_id


def _hours(n: float) -> dt.timedelta:
    return dt.timedelta(hours=n)


KO = "네 알겠어요 내일 회의는 열 시예요"


async def test_clips_are_proposed_by_language_part_of_the_day_and_maybe_noise(pool, pki_settings, dev):
    d = dev["device_id"]
    morning = [await _spoken(pool, pki_settings, d, NINE + dt.timedelta(minutes=m), "vi",
                             f"Sáng nay mình nói câu số {m}") for m in (0, 10, 20)]
    korean = await _spoken(pool, pki_settings, d, NINE + _hours(5), "ko", KO)  # 14:00
    english = await _spoken(pool, pki_settings, d, NINE + _hours(12), "en", "Can you pass me the salt please")
    stock = await _spoken(pool, pki_settings, d, NINE + _hours(12.5), "en", "Thank you.")  # 21:30, two words
    unsure = await _spoken(pool, pki_settings, d, NINE + _hours(13), "vi", "Hãy đăng ký kênh để ủng hộ mình nhé",
                           avg_logprob=-0.9)  # 22:00, long but unsure
    silent = await analysed_audio(pool, pki_settings, d, NINE - _hours(6))  # 03:00, Whisper heard nothing
    expired = await _spoken(pool, pki_settings, d, NINE + _hours(1), "vi", "Câu này đã hết hạn rồi nhé")
    pending = await _spoken(pool, pki_settings, d, NINE + _hours(2), "vi", "Câu này chưa phân tích xong đâu")
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (expired,))
        await conn.execute("UPDATE item SET analysis_state = 'pending' WHERE id = %s", (pending,))
    strata = await golden.audio_candidates(pool, pki_settings.zone)
    assert set(strata) == {"vi", "ko", "en", "noise", ""}
    vi = strata["vi"]
    assert vi["available"] == 3 and len(vi["items"]) == golden.PER_PART  # 2 of the 3 morning recordings
    assert {i["id"] for i in vi["items"]} < set(morning)
    assert all(i["part"] == "sáng (6–12 giờ)" and i["lang"] == "vi" for i in vi["items"])
    assert [i["transcript"] for i in strata["ko"]["items"]] == [KO]
    assert [(i["id"], i["part"]) for i in strata["en"]["items"]] == [(english, "tối (18–24 giờ)")]
    noise = strata["noise"]  # what the no-speech check needs: Whisper wrote something, maybe on noise
    assert noise["available"] == 2 and {(i["id"], i["lang"]) for i in noise["items"]} == {(stock, "en"), (unsure, "vi")}
    assert [(i["id"], i["transcript"]) for i in strata[""]["items"]] == [(silent, "")]
    assert strata["ko"]["items"][0]["id"] == korean
    await golden.add_audio(pool, pki_settings, korean, golden.AudioLabel("ko", False, KO))
    again = await golden.audio_candidates(pool, pki_settings.zone)
    assert again["ko"] == {"available": 0, "items": []}  # a golden clip is not proposed again
    other = await golden.audio_candidates(pool, pki_settings.zone, seed=1)
    assert len(other["vi"]["items"]) == 2  # another draw ("Gợi ý khác") of the same recordings
    later = await golden.audio_candidates(pool, pki_settings.zone, since=NINE + _hours(3))
    assert later["vi"] == {"available": 0, "items": []} and later["en"]["available"] == 1  # recorded since then only


def test_clips_are_proposed_from_the_device_gain_change_on(monkeypatch):
    from zoneinfo import ZoneInfo

    import pydantic

    from loopi_brain.config import Settings

    since = Settings(database_url="postgresql://x").eval_audio_since
    assert since == dt.datetime(2026, 10, 16, 8, 41, tzinfo=ZoneInfo("Asia/Seoul"))  # smoke8: +20 dB (D19)
    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("LOOPI_EVAL_AUDIO_SINCE", "2026-10-20T00:00:00+09:00")
    assert Settings().eval_audio_since == dt.datetime(2026, 10, 19, 15, 0, tzinfo=dt.UTC)
    monkeypatch.setenv("LOOPI_EVAL_AUDIO_SINCE", "2026-10-20T00:00:00")
    with pytest.raises(pydantic.ValidationError, match="UTC offset"):
        Settings()


async def test_a_labelled_clip_is_copied_and_outlives_retention(pool, pki_settings, dev):
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "mai mình đi chợ", source=QUIET_SPEECH)
    raw = pki_settings.media_root / (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,)))["raw_path"]
    sample = await golden.add_audio(pool, pki_settings, item, VI)
    copy, view = (pki_settings.eval_dir / rel for rel in golden.audio_paths(sample))
    assert copy.read_bytes() == raw.read_bytes()  # the runner hears exactly what the live stage heard
    assert view.is_file() and view.stat().st_size > 0  # the page plays it at a level you can hear (D19)
    raw.unlink()  # retention removed the raw recording (D20)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (item,))
    [row] = await golden.audio_samples(pool)
    assert (row["id"], row["item_id"], row["lang"], row["no_speech"], row["reference"]) == (
        sample, item, "vi", False, "Mai mình đi chợ nhé")
    assert copy.is_file() and view.is_file()


async def test_a_clip_needs_a_kept_finished_recording_and_a_valid_label(pool, pki_settings, dev):
    d = dev["device_id"]
    item = await _spoken(pool, pki_settings, d, NINE, "vi", "x")
    with pytest.raises(GoldenError):
        await golden.add_audio(pool, pki_settings, "01UNKNOWN0000000000000000", VI)
    await golden.add_audio(pool, pki_settings, item, VI)
    with pytest.raises(GoldenError, match="already"):
        await golden.add_audio(pool, pki_settings, item, VI)
    expired = await _spoken(pool, pki_settings, d, NINE + _hours(1), "vi", "x")
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now() WHERE id = %s", (expired,))
    with pytest.raises(GoldenError):
        await golden.add_audio(pool, pki_settings, expired, VI)
    for lang, no_speech, reference in (("ja", False, "x"), ("vi", False, " \x00 "), ("", False, "x")):
        with pytest.raises(ValueError):
            golden.audio_label(lang, no_speech, reference)
    assert golden.audio_label("", True, "ignored") == golden.AudioLabel(None, True, "")
    assert golden.audio_label("ko", False, " 네\n알겠어요\x00 ") == golden.AudioLabel("ko", False, "네 알겠어요")
    assert len(await fetch_all(pool, "SELECT id FROM eval_audio")) == 1


@pytest.mark.parametrize(
    ("lang", "no_speech", "reference"),
    [
        pytest.param("ja", False, "x", id="other-language"), pytest.param(None, False, "x", id="no-language"),
        pytest.param("", False, "x", id="empty-language"),  # a language of the household is needed
        pytest.param("vi", False, "", id="no-reference"), pytest.param("vi", False, " \t\n ", id="blank-reference"),
        pytest.param("vi", True, "", id="speechless-with-language"),  # no speech: no language, no reference
        pytest.param("vi", True, "x", id="speechless-with-both"), pytest.param(None, True, "x", id="speechless-with-text"),
        pytest.param("vi", False, "x" * (golden.REFERENCE_MAX + 1), id="too-long"),  # and not an unbounded one
    ],
)
def test_a_label_the_table_would_refuse_cannot_be_built(lang, no_speech, reference):
    with pytest.raises(ValueError) as refused:
        golden.AudioLabel(lang, no_speech, reference)
    assert reference.strip() == "" or reference not in str(refused.value)  # the reason never repeats the text


def test_a_label_the_table_accepts_can_be_built():
    assert golden.AudioLabel("vi", False, "x" * golden.REFERENCE_MAX).lang == "vi"
    assert golden.AudioLabel("en", False, "Thank you.").reference == "Thank you."
    assert golden.AudioLabel(None, True, "") == golden.audio_label("ko", True, "ignored")


async def test_a_database_refusal_never_carries_the_reference_into_an_error(pool, pki_settings, dev):
    # Defence in depth: AudioLabel refuses a bad label itself, but psycopg's message for a CHECK violation holds
    # PostgreSQL's "Failing row contains (...)" with the reference, which a log or a page must never get.
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    secret = "bí mật không được lọt ra"
    sample = await golden.add_audio(pool, pki_settings, item, VI)
    bad = golden.AudioLabel(None, True, "")
    object.__setattr__(bad, "reference", secret)  # a no-speech clip with a reference: the table's CHECK refuses it
    other = await _spoken(pool, pki_settings, dev["device_id"], NINE + _hours(1), "vi", "y")
    for refused in (golden.add_audio(pool, pki_settings, other, bad), golden.update_audio(pool, sample, bad)):
        with pytest.raises(GoldenError, match="^the label could not be saved$") as caught:
            await refused
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
        assert secret not in "".join(traceback.format_exception(caught.value))
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM eval_audio")] == [sample]  # nothing was stored
    assert (await golden.audio_samples(pool))[0]["reference"] == VI.reference  # and nothing changed


async def test_a_recording_whose_file_is_missing_leaves_no_sample(pool, pki_settings, dev, caplog):
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    raw = (await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item,)))["raw_path"]
    (pki_settings.media_root / raw).unlink()
    with caplog.at_level(logging.WARNING), pytest.raises(GoldenError, match="copied"):
        await golden.add_audio(pool, pki_settings, item, golden.AudioLabel("vi", False, "bí mật riêng tư"))
    assert await fetch_all(pool, "SELECT id FROM eval_audio") == []
    assert "bí mật" not in caplog.text  # a reference is never logged


async def test_labels_can_be_corrected_and_clips_deleted_with_their_copies(pool, pki_settings, dev):
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "en", "Thank you.")
    sample = await golden.add_audio(pool, pki_settings, item, golden.AudioLabel("en", False, "Thank you."),
                                    prefilled=False)
    assert await golden.update_audio(pool, sample, golden.audio_label("", True, ""))
    [row] = await golden.audio_samples(pool)
    assert (row["no_speech"], row["had_text"], row["prefilled"]) == (True, True, False)
    silent = await analysed_audio(pool, pki_settings, dev["device_id"], NINE + _hours(1))
    await golden.add_audio(pool, pki_settings, silent, golden.AudioLabel(None, True, ""))
    found = await golden.progress(pool)
    assert found["audio"] == {"vi": 0, "ko": 0, "en": 0, "none": 1, "silent": 1} and found["audio_total"] == 2
    assert await golden.delete_audio(pool, pki_settings, sample)
    assert not any((pki_settings.eval_dir / rel).exists() for rel in golden.audio_paths(sample))
    assert not await golden.delete_audio(pool, pki_settings, sample)
    assert not await golden.update_audio(pool, sample, VI)


async def test_keyframes_are_proposed_one_per_day_and_three_hour_block(pool, pki_settings, dev):
    d = dev["device_id"]
    first = await captioned_keyframe(pool, pki_settings, d, NINE)
    same_block = await captioned_keyframe(pool, pki_settings, d, NINE + dt.timedelta(minutes=30))
    afternoon = await captioned_keyframe(pool, pki_settings, d, NINE + _hours(4))  # 13:00
    next_day = await captioned_keyframe(pool, pki_settings, d, NINE + _hours(24))
    uncaptioned = await captioned_keyframe(pool, pki_settings, d, NINE + _hours(7))
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = NULL WHERE id = %s", (uncaptioned,))
    found = await golden.caption_candidates(pool, pki_settings.zone)
    assert len(found) == 3 and [k["ts"] for k in found] == sorted(k["ts"] for k in found)
    assert {k["id"] for k in found} - {first, same_block} == {afternoon, next_day}
    assert len({first, same_block} & {k["id"] for k in found}) == 1
    assert len(await golden.caption_candidates(pool, pki_settings.zone, limit=2)) == 2


async def test_a_scored_keyframe_is_copied_and_scored_again_for_a_new_caption_model(pool, pki_settings, dev):
    kf = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    src = pki_settings.media_root / (await fetch_one(pool, "SELECT path FROM keyframe WHERE id = %s", (kf,)))["path"]
    sample = await golden.add_caption(pool, pki_settings, kf, 4, {"invented_objects"})
    assert (pki_settings.eval_dir / golden.keyframe_path(sample)).read_bytes() == src.read_bytes()
    [s] = await golden.caption_samples(pool)
    assert s["needs_score"] is False and [(x["model_version"], x["score"], x["invented_objects"], x["wrong_people"])
                                          for x in s["scores"]] == [("ollama:minicpm-v4.6:1b", 4, True, False)]
    with pytest.raises(GoldenError, match="already"):
        await golden.add_caption(pool, pki_settings, kf, 3, set())
    async with pool.connection() as conn:  # "Xử lý lại → mô tả khung hình" with another model
        await conn.execute("UPDATE keyframe SET caption = '{\"scene\": \"A room\"}', caption_model = 'ollama:new'")
    [s] = await golden.caption_samples(pool)
    assert s["needs_score"] is True
    assert await golden.score_caption(pool, sample, 2, {"wrong_people", "wrong_activity"})
    [s] = await golden.caption_samples(pool)
    assert [(x["model_version"], x["score"], x["caption"]) for x in s["scores"]] == [
        ("ollama:minicpm-v4.6:1b", 4, s["scores"][0]["caption"]), ("ollama:new", 2, {"scene": "A room"})]
    assert s["needs_score"] is False
    assert await golden.progress(pool) == {"audio": {"vi": 0, "ko": 0, "en": 0, "none": 0, "silent": 0},
                                           "audio_total": 0, "captions": 1, "captions_scored": 1}
    for bad in ((0, set()), (6, set()), (3, {"blurry"})):
        with pytest.raises(ValueError):
            await golden.score_caption(pool, sample, *bad)
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = NULL")  # being captioned again: nothing to score now
    assert not await golden.score_caption(pool, sample, 3, set())
    assert await golden.delete_caption(pool, pki_settings, sample)
    assert not (pki_settings.eval_dir / golden.keyframe_path(sample)).exists()


async def test_an_uncaptioned_keyframe_is_not_a_sample(pool, pki_settings, dev):
    kf = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = NULL")
    with pytest.raises(GoldenError):
        await golden.add_caption(pool, pki_settings, kf, 3, set())


async def test_the_sweep_deletes_copies_without_a_sample(pool, pki_settings, dev):
    assert await golden.sweep(pool, pki_settings.eval_dir) == (0, [])  # no eval dir yet
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    sample = await golden.add_audio(pool, pki_settings, item, VI)
    stray = [pki_settings.eval_dir / "audio" / "01STRAY.opus", pki_settings.eval_dir / "audio" / "01STRAY.ab12.part",
             pki_settings.eval_dir / "keyframes" / "01GONE.jpg"]
    # A half-made copy is taken only once its row is gone: while the row exists the copy may be being written.
    half_made = pki_settings.eval_dir / "audio" / f"{sample}.ab12.part"
    for path in (*stray, half_made):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    assert await golden.sweep(pool, pki_settings.eval_dir) == (3, [])
    assert not any(p.exists() for p in stray) and half_made.is_file()
    assert all((pki_settings.eval_dir / rel).is_file() for rel in golden.audio_paths(sample))
    await golden.delete_audio(pool, pki_settings, sample)
    half_made.write_bytes(b"x")  # the row is gone now: its half-made copy goes with the next sweep
    assert await golden.sweep(pool, pki_settings.eval_dir) == (1, []) and not half_made.exists()


def _stuck(monkeypatch, *prefixes: str) -> None:
    """Copies whose name starts with one of ``prefixes`` cannot be deleted (a read-only folder, say)."""
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name.startswith(prefixes):
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)


async def test_the_sweep_says_which_copies_it_could_not_delete(pool, pki_settings, dev, monkeypatch, caplog):
    for name in ("audio/01STUCK.opus", "audio/01GONE.opus", "keyframes/01STUCK.jpg"):
        (pki_settings.eval_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (pki_settings.eval_dir / name).write_bytes(b"x")
    _stuck(monkeypatch, "01STUCK")
    with caplog.at_level(logging.INFO):
        assert await golden.sweep(pool, pki_settings.eval_dir) == (1, ["audio/01STUCK.opus", "keyframes/01STUCK.jpg"])
    assert "1 copies without a sample deleted, 2 could not be" in caplog.text
    monkeypatch.undo()
    assert await golden.sweep(pool, pki_settings.eval_dir) == (2, [])  # the next night's sweep finishes it


async def test_a_forget_counts_the_golden_copies_it_could_not_delete(pool, pki_settings, dev, monkeypatch):
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    clip = await golden.add_audio(pool, pki_settings, item, VI)
    _stuck(monkeypatch, clip)  # both copies of the clip
    counts = await forget.forget_range(pool, pki_settings, NINE - dt.timedelta(minutes=1), NINE + _hours(1))
    assert counts.golden == 1 and counts.files_failed == 2  # the owner is told something stays
    assert all((pki_settings.eval_dir / rel).is_file() for rel in golden.audio_paths(clip))
    assert await fetch_all(pool, "SELECT id FROM eval_audio") == []  # the row is gone: the sweep takes the copies
    monkeypatch.undo()
    assert await golden.sweep(pool, pki_settings.eval_dir) == (2, [])
    again = await forget.forget_range(pool, pki_settings, NINE - dt.timedelta(minutes=1), NINE + _hours(1))
    assert again.files_failed == 0


async def test_the_nightly_sweep_warns_about_copies_it_could_not_delete(pool, pki_settings, dev, monkeypatch, caplog):
    stuck = pki_settings.eval_dir / "audio" / "01STUCK.opus"
    stuck.parent.mkdir(parents=True, exist_ok=True)
    stuck.write_bytes(b"x")
    _stuck(monkeypatch, "01STUCK")
    context.set_context(make_ctx(pool, pki_settings))
    try:
        with caplog.at_level(logging.WARNING):
            await retention.apply_retention_job(timestamp=THREE_IN_SEOUL)
    finally:
        context.set_context(None)
    assert "1 golden copies could not be deleted; the next night's sweep tries again" in caplog.text


async def test_forgetting_a_range_takes_its_golden_samples_and_their_copies(pool, pki_settings, dev):
    d = dev["device_id"]
    clip_item = await _spoken(pool, pki_settings, d, NINE, "vi", "x")
    clip = await golden.add_audio(pool, pki_settings, clip_item, VI)
    kf = await captioned_keyframe(pool, pki_settings, d, NINE + dt.timedelta(minutes=5))
    picture = await golden.add_caption(pool, pki_settings, kf, 5, set())
    speech = await insert_moment(pool, d, kind="speech", ts=NINE, item_id=clip_item, text="x")
    later_item = await _spoken(pool, pki_settings, d, NINE + _hours(3), "vi", "y")
    later = await golden.add_audio(pool, pki_settings, later_item, VI)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO eval_question (id, text) VALUES ('Q1', 'Sáng nay mình nói gì?')")
        await conn.execute(
            "INSERT INTO eval_answer (question_id, moment_id, kind, item_id, ts_start, ts_end)"
            " VALUES ('Q1', %s, 'speech', %s, %s, %s)", (speech, clip_item, NINE, NINE))
    start, end = NINE - dt.timedelta(minutes=1), NINE + _hours(1)
    assert (await forget.preview(pool, start, end, pki_settings.zone)).golden == 3
    counts = await forget.forget_range(pool, pki_settings, start, end)
    assert counts.golden == 3
    assert [r["id"] for r in await fetch_all(pool, "SELECT id FROM eval_audio")] == [later]
    assert await fetch_all(pool, "SELECT * FROM eval_caption") == [] and await fetch_all(pool, "SELECT * FROM eval_answer") == []
    assert await fetch_all(pool, "SELECT id FROM eval_question") == [{"id": "Q1"}]  # the owner's question stays
    gone = [*golden.audio_paths(clip), golden.keyframe_path(picture)]
    assert not any((pki_settings.eval_dir / rel).exists() for rel in gone)
    assert all((pki_settings.eval_dir / rel).is_file() for rel in golden.audio_paths(later))


async def test_retention_never_expires_golden_copies_and_its_tick_sweeps_stray_ones(pool, pki_settings, dev):
    from loopi_brain import store

    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    sample = await golden.add_audio(pool, pki_settings, item, VI)
    stray = pki_settings.eval_dir / "keyframes" / "01GONE.jpg"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"x")
    await store.set_retention(pool, store.Retention(enabled=True))
    async with pool.connection() as conn:  # the recording is old enough to expire
        await conn.execute("UPDATE item SET started_at = started_at - interval '30 days',"
                           " ended_at = ended_at - interval '30 days'")
    context.set_context(make_ctx(pool, pki_settings))
    try:
        await retention.apply_retention_job(timestamp=THREE_IN_SEOUL)
    finally:
        context.set_context(None)
    assert (await fetch_one(pool, "SELECT raw_deleted_at FROM item"))["raw_deleted_at"] is not None
    assert all((pki_settings.eval_dir / rel).is_file() for rel in golden.audio_paths(sample))
    assert not stray.exists()


@pytest.mark.parametrize("kind", ["audio", "caption"])
async def test_a_sample_forgotten_while_its_copy_is_written_leaves_no_copy(pool, pki_settings, dev, monkeypatch, kind):
    import psycopg

    real_copy = golden._copy

    def copy_then_forget(src, dst):  # runs in a worker thread, like the real copy
        real_copy(src, dst)
        with psycopg.connect(pki_settings.database_url, autocommit=True) as conn:
            conn.execute("DELETE FROM item")  # "Quên khoảng thời gian" took the recording (cascades to the sample)

    monkeypatch.setattr(golden, "_copy", copy_then_forget)
    if kind == "audio":
        item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
        with pytest.raises(GoldenError, match="forgotten"):
            await golden.add_audio(pool, pki_settings, item, VI)
    else:
        kf = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
        with pytest.raises(GoldenError, match="forgotten"):
            await golden.add_caption(pool, pki_settings, kf, 3, set())
    assert not [p for p in pki_settings.eval_dir.rglob("*") if p.is_file()]


async def test_a_keyframe_whose_picture_is_missing_leaves_no_sample(pool, pki_settings, dev):
    kf = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    (pki_settings.media_root / (await fetch_one(pool, "SELECT path FROM keyframe"))["path"]).unlink()
    with pytest.raises(GoldenError, match="copied"):
        await golden.add_caption(pool, pki_settings, kf, 3, set())
    assert await fetch_all(pool, "SELECT id FROM eval_caption") == []


async def test_a_clip_ffmpeg_cannot_raise_is_kept_and_plays_as_recorded(pool, pki_settings, dev, monkeypatch, caplog):
    from loopi_brain import media

    async def broken(src, dst):
        raise media.MediaError("ffmpeg exit 1")

    monkeypatch.setattr(media, "normalise_audio", broken)
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    with caplog.at_level(logging.WARNING):
        sample = await golden.add_audio(pool, pki_settings, item, VI)
    copy, view = (pki_settings.eval_dir / rel for rel in golden.audio_paths(sample))
    assert copy.is_file() and not view.exists() and "no playback copy" in caplog.text


async def test_a_failing_sweep_stops_neither_a_forget_nor_retention(pool, pki_settings, dev, monkeypatch, caplog):
    async def broken(pool, eval_dir):
        raise OSError("disk unplugged")

    monkeypatch.setattr(golden, "sweep", broken)
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    await golden.add_audio(pool, pki_settings, item, VI)
    with caplog.at_level(logging.ERROR):
        counts = await forget.forget_range(pool, pki_settings, NINE - dt.timedelta(minutes=1), NINE + _hours(1))
    assert counts.golden == 1 and counts.items == 1 and "could not sweep the golden copies" in caplog.text
    assert counts.files_failed == 1  # how many copies stay is unknown: the owner is told at least one may
    calls = []

    async def apply(pool, settings, now=None):
        calls.append(now)

    monkeypatch.setattr(retention, "apply_retention", apply)
    context.set_context(make_ctx(pool, pki_settings))
    try:
        await retention.apply_retention_job(timestamp=THREE_IN_SEOUL)
    finally:
        context.set_context(None)
    assert calls == [None]


async def test_the_settings_page_counts_the_golden_samples_a_forget_takes_and_says_the_questions_stay(
    pool, pki_settings, dev
):
    item = await _spoken(pool, pki_settings, dev["device_id"], NINE, "vi", "x")
    await golden.add_audio(pool, pki_settings, item, VI)
    app = create_app(pki_settings, pool=pool, http_client=FakeOllama().client(), llm_client=FakeOllama().client())
    form = {"start": "2026-10-14T09:00", "end": "2026-10-14T09:30"}
    hx = {"HX-Request": "true"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as web:
        preview = await web.post("/settings/forget/preview", data=form, headers=hx)
        assert "1 mẫu đánh giá" in preview.text and "câu hỏi đánh giá vẫn" in preview.text
        done = await web.post("/settings/forget", data={**form, "confirm": "yes"}, headers=hx)
    assert "1 mẫu đánh giá" in done.text and await fetch_all(pool, "SELECT id FROM eval_audio") == []
