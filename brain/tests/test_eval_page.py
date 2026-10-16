import datetime as dt
import re

import httpx
import pytest

from helpers import (
    QUIET_SPEECH,
    FakeOllama,
    analysed_audio,
    captioned_keyframe,
    fetch_all,
    insert_moment,
    insert_segment,
    paired_device,
)
from loopi_brain.app import create_app
from loopi_brain.evaluation import golden, questions

HX = {"HX-Request": "true"}
NINE = dt.datetime(2026, 10, 20, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul, after the device gain change (D19)
SPEECH = "Mai mình đi chợ mua rau nhé"


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
async def web(pool, pki_settings, jobs_open):
    ollama = FakeOllama()
    app = create_app(pki_settings, pool=pool, http_client=ollama.client(), llm_client=ollama.client())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _spoken(pool, settings, device_id, minutes, lang, text, start=NINE) -> str:
    at = start + dt.timedelta(minutes=minutes)
    item = await analysed_audio(pool, settings, device_id, at, source=QUIET_SPEECH)
    await insert_segment(pool, item, device_id, at, text, lang=lang)
    return item


def _flat(html: str) -> str:
    return " ".join(html.split())


async def test_the_evaluation_page_shows_how_far_the_labelling_is(web):
    page = await web.get("/eval")
    assert page.status_code == 200
    text = _flat(page.text)
    assert '<a href="/eval">Đánh giá</a>' in text  # in the menu
    assert "Âm thanh</a>: 0/22 đoạn" in text and "khoảng 2–3 giờ" in text
    assert ("Tiếng Việt 0/6 · Tiếng Hàn 0/6 · Tiếng Anh 0/4 · Không có lời nói (Whisper có viết chữ) 0/6" in text)
    assert "Mô tả khung hình</a>: 0/30 khung hình" in text and "Câu hỏi</a>: 0/25 câu" in text


async def test_the_owner_labels_a_proposed_clip_and_corrects_it(pool, pki_settings, dev, web):
    item = await _spoken(pool, pki_settings, dev["device_id"], 0, "vi", SPEECH)
    page = _flat((await web.get("/eval/audio")).text)
    assert f'src="/audio/{item}.opus"' in page and f">{SPEECH}</textarea>" in page
    assert '<input type="radio" name="lang" value="vi" checked>' in page
    assert "Chỉ gợi ý đoạn ghi từ 16/10/2026 08:41" in page and "đừng giữ chữ của Whisper" in page
    form = {"item_id": item, "lang": "vi", "reference": SPEECH + " sớm"}
    assert (await web.post("/eval/audio", data=form)).status_code == 403  # no HX-Request: refused
    saved = await web.post("/eval/audio", data=form, headers=HX)
    [sample] = await golden.audio_samples(pool)
    assert saved.status_code == 200 and "Đã lưu" in saved.text and f'/eval/audio/{sample["id"]}.opus' in saved.text
    assert (sample["reference"], sample["prefilled"]) == (SPEECH + " sớm", True)
    played = await web.get(f"/eval/audio/{sample['id']}.opus")
    assert played.status_code == 200 and played.headers["content-type"] == "audio/ogg"
    view = pki_settings.eval_dir / golden.audio_paths(sample["id"])[1]
    assert played.content == view.read_bytes()  # the copy raised to a level you can hear
    page = _flat((await web.get("/eval/audio")).text)
    assert "Đã gán nhãn (1)" in page and f'name="item_id" value="{item}"' not in page
    fixed = await web.post(f"/eval/audio/{sample['id']}", data={"no_speech": "on"}, headers=HX)
    assert fixed.status_code == 200 and (await golden.audio_samples(pool))[0]["no_speech"] is True
    wrong = await web.post(f"/eval/audio/{sample['id']}", data={"lang": "ja", "reference": "chữ đã gõ"}, headers=HX)
    assert wrong.status_code == 200 and "Chọn ngôn ngữ chính của đoạn này" in wrong.text
    assert ">chữ đã gõ</textarea>" in wrong.text and "Đã lưu" not in wrong.text  # what was typed stays
    assert (await golden.audio_samples(pool))[0]["no_speech"] is True  # nothing saved
    view.unlink()  # no playback copy: the copy as recorded plays
    assert (await web.get(f"/eval/audio/{sample['id']}.opus")).status_code == 200
    gone = await web.post(f"/eval/audio/{sample['id']}/delete", headers=HX)
    assert "Đã xoá" in gone.text and await golden.audio_samples(pool) == []
    assert (await web.get(f"/eval/audio/{sample['id']}.opus")).status_code == 404
    assert (await web.post(f"/eval/audio/{sample['id']}/delete", headers=HX)).status_code == 404
    assert (await web.post(f"/eval/audio/{sample['id']}", data={"no_speech": "on"}, headers=HX)).status_code == 404


async def test_the_owner_can_listen_first_and_type_without_seeing_whisper(pool, pki_settings, dev, web):
    item = await _spoken(pool, pki_settings, dev["device_id"], 0, "vi", SPEECH)
    page = _flat((await web.get("/eval/audio", params={"blind": "1"})).text)
    assert 'placeholder="Gõ những gì bạn nghe được"></textarea>' in page  # the box starts empty
    assert "Xem transcript của Whisper (sau khi đã tự gõ)" in page and 'name="prefilled" value="0"' in page
    saved = await web.post("/eval/audio", data={"item_id": item, "lang": "vi", "reference": SPEECH,
                                                "prefilled": "0"}, headers=HX)
    assert "Đã lưu" in saved.text and (await golden.audio_samples(pool))[0]["prefilled"] is False


async def test_a_clip_that_cannot_be_saved_says_why_and_keeps_what_was_typed(pool, pki_settings, dev, web):
    item = await _spoken(pool, pki_settings, dev["device_id"], 0, "vi", SPEECH)
    empty = await web.post("/eval/audio", data={"item_id": item, "lang": "vi", "reference": " "}, headers=HX)
    assert empty.status_code == 200 and "Nhập những gì thật sự được nói" in empty.text
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET raw_deleted_at = now()")  # expired since the page was opened
    answer = await web.post("/eval/audio", data={"item_id": item, "lang": "vi", "reference": "bản đã sửa"},
                            headers=HX)
    assert answer.status_code == 200 and "Không lưu được" in answer.text and ">bản đã sửa</textarea>" in answer.text
    gone = await web.post("/eval/audio", data={"item_id": "01NOSUCH000000000000000000", "lang": "vi",
                                               "reference": "x"}, headers=HX)
    assert "Không lưu được" in gone.text
    assert await fetch_all(pool, "SELECT id FROM eval_audio") == []


async def test_scarce_kinds_of_clip_are_reported_not_faked(pool, pki_settings, dev, web):
    for minutes in range(golden.AUDIO_SUGGESTED["vi"]):  # enough Vietnamese, no Korean, English or noise at all
        await _spoken(pool, pki_settings, dev["device_id"], minutes, "vi", SPEECH)
    page = _flat((await web.get("/eval/audio")).text)
    assert "Tiếng Hàn: chỉ còn 0 đoạn ghi để chọn, còn thiếu 6 so với gợi ý 6." in page
    assert "Tiếng Anh: chỉ còn 0 đoạn ghi để chọn" in page and "Tiếng Việt: chỉ còn" not in page
    assert "Không có lời nói (Whisper có viết chữ): chỉ còn 0 đoạn ghi để chọn, còn thiếu 6" in page


async def test_recordings_from_before_the_gain_change_are_not_proposed(pool, pki_settings, dev, web):
    early = await _spoken(pool, pki_settings, dev["device_id"], 0, "vi", SPEECH,
                          start=dt.datetime(2026, 10, 15, 0, 0, tzinfo=dt.UTC))
    late = await _spoken(pool, pki_settings, dev["device_id"], 0, "vi", SPEECH)
    page = (await web.get("/eval/audio")).text
    assert f'value="{late}"' in page and f'value="{early}"' not in page


async def test_maybe_noise_is_proposed_apart_with_its_hint(pool, pki_settings, dev, web):
    noise = await _spoken(pool, pki_settings, dev["device_id"], 0, "en", "Thank you.")
    page = _flat((await web.get("/eval/audio")).text)
    assert "<h2>Có chữ nhưng có thể chỉ là tiếng ồn" in page and "cần ít nhất 6 đoạn như vậy" in page
    saved = await web.post("/eval/audio", data={"item_id": noise, "no_speech": "on"}, headers=HX)
    assert "Đã lưu" in saved.text and (await golden.progress(pool))["audio"]["none"] == 1


async def test_the_owner_scores_a_proposed_keyframe_and_its_next_caption(pool, pki_settings, dev, web):
    kf = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    page = _flat((await web.get("/eval/captions")).text)
    assert f'src="/thumb/{kf}.jpg"' in page and "A small home office with a desk by a window" in page
    unscored = await web.post("/eval/captions", data={"keyframe_id": kf, "invented_objects": "on"}, headers=HX)
    assert unscored.status_code == 200 and "Chọn điểm từ 1 đến 5." in unscored.text and f'value="{kf}"' in unscored.text
    saved = await web.post("/eval/captions", data={"keyframe_id": kf, "score": "4", "invented_objects": "on"},
                           headers=HX)
    [sample] = await golden.caption_samples(pool)
    assert saved.status_code == 200 and "ollama:minicpm-v4.6:1b: 4/5 · bịa đồ vật" in _flat(saved.text)
    picture = await web.get(f"/eval/keyframes/{sample['id']}.jpg")
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/jpeg"
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption_model = 'ollama:new'")
    page = _flat((await web.get("/eval/captions")).text)
    assert "Mô tả của model hiện tại chưa được chấm." in page and "Đã chấm (1)" in page
    again = await web.post(f"/eval/captions/{sample['id']}/score", data={"score": "2", "wrong_people": "on"},
                           headers=HX)
    assert "ollama:new: 2/5 · sai số người" in _flat(again.text)
    for bad in ({"score": "0"}, {"score": "6"}, {"score": "four"}):
        answer = await web.post(f"/eval/captions/{sample['id']}/score", data=bad, headers=HX)
        assert answer.status_code == 200 and "Chọn điểm từ 1 đến 5." in answer.text
    again = await web.post("/eval/captions", data={"keyframe_id": kf, "score": "3"}, headers=HX)
    assert "Không lưu được" in again.text  # already golden
    assert "Không lưu được" in (await web.post("/eval/captions", data={"keyframe_id": "01NOSUCH", "score": "3"},
                                               headers=HX)).text
    async with pool.connection() as conn:
        await conn.execute("UPDATE keyframe SET caption = NULL")
    assert "Không lưu được" in (await web.post(f"/eval/captions/{sample['id']}/score", data={"score": "3"},
                                               headers=HX)).text
    assert "Đã xoá" in (await web.post(f"/eval/captions/{sample['id']}/delete", headers=HX)).text
    assert (await web.get(f"/eval/keyframes/{sample['id']}.jpg")).status_code == 404
    assert (await web.post(f"/eval/captions/{sample['id']}/delete", headers=HX)).status_code == 404


async def test_transcripts_and_captions_cannot_inject_markup(pool, pki_settings, dev, web):
    await _spoken(pool, pki_settings, dev["device_id"], 0, "en", "<script>alert(1)</script> and more words here")
    await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE, caption={
        "scene": "<img src=x onerror=alert(2)>", "activity": "x", "people_count": 0, "objects": ["<b>"],
        "text_seen": "", "notable": False})
    audio = (await web.get("/eval/audio")).text
    captions = (await web.get("/eval/captions")).text
    assert "<script>alert(1)" not in audio and "&lt;script&gt;alert(1)&lt;/script&gt;" in audio
    assert "<img src=x" not in captions and "&lt;b&gt;" in captions


async def test_golden_files_are_served_only_for_their_samples(pool, pki_settings, web):
    assert (await web.get("/eval/audio/01NOSUCH000000000000000000.opus")).status_code == 404
    assert (await web.get("/eval/keyframes/01NOSUCH000000000000000000.jpg")).status_code == 404
    async with pool.connection() as conn:  # a row without its copy (the copy was lost)
        await conn.execute("INSERT INTO device (id, name, cert_fingerprint) VALUES ('D1', 'd', 'fp')")
        await conn.execute("INSERT INTO item (id, device_id, kind, started_at, ended_at, sha256, size_bytes, raw_path,"
                           " status) VALUES ('I1', 'D1', 'audio', now(), now(), 'x', 1, 'raw/x.opus', 'ready')")
        await conn.execute("INSERT INTO eval_audio (id, item_id, path, no_speech) VALUES ('A1', 'I1', 'audio/A1.opus', true)")
    assert (await web.get("/eval/audio/A1.opus")).status_code == 404
    with pytest.raises(ValueError):
        await golden.sample_exists(pool, "item", "A1")


async def test_the_page_fits_a_phone(web):
    css = (await web.get("/eval")).text
    assert "textarea, input, select { max-width: 100%; box-sizing: border-box; }" in css


async def test_a_golden_copy_that_leads_outside_the_eval_dir_is_not_served(pool, pki_settings, dev, web, tmp_path):
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"not for the browser")
    item = await _spoken(pool, pki_settings, dev["device_id"], 0, "vi", SPEECH)
    await web.post("/eval/audio", data={"item_id": item, "lang": "vi", "reference": SPEECH}, headers=HX)
    [clip] = await golden.audio_samples(pool)
    for rel in golden.audio_paths(clip["id"]):  # both copies replaced by links to a file outside
        (pki_settings.eval_dir / rel).unlink(missing_ok=True)
        (pki_settings.eval_dir / rel).symlink_to(secret)
    served = await web.get(f"/eval/audio/{clip['id']}.opus")
    assert served.status_code == 404 and b"not for the browser" not in served.content
    kf = await captioned_keyframe(pool, pki_settings, dev["device_id"], NINE)
    await web.post("/eval/captions", data={"keyframe_id": kf, "score": "4"}, headers=HX)
    [picture] = await golden.caption_samples(pool)
    path = pki_settings.eval_dir / golden.keyframe_path(picture["id"])
    path.unlink()
    path.symlink_to(secret)
    served = await web.get(f"/eval/keyframes/{picture['id']}.jpg")
    assert served.status_code == 404 and b"not for the browser" not in served.content


async def test_a_sample_deleted_meanwhile_answers_404_even_for_a_label_that_was_wrong(pool, web):
    for gone in ("/eval/audio/01NOSUCH000000000000000000", "/eval/captions/01NOSUCH000000000000000000/score"):
        wrong = await web.post(gone, data={"lang": "ja", "score": "0"}, headers=HX)
        assert wrong.status_code == 404  # the page shows "Không tìm thấy", never a 422


async def test_the_owner_writes_a_question_and_picks_its_correct_moments(pool, pki_settings, dev, web):
    d = dev["device_id"]
    market = await insert_moment(pool, d, kind="speech", ts=NINE, axis=0, text="Mai mình đi chợ <b>sớm</b>")
    await insert_moment(pool, d, kind="visual", ts=NINE + dt.timedelta(hours=1), axis=3, text="A dark room")
    form = {"text": "Khi nào đi chợ?", "from": "2026-10-20", "to": "2026-10-20"}
    assert (await web.post("/eval/questions", data=form)).status_code == 403
    created = await web.post("/eval/questions", data=form, headers=HX)
    question_id = created.headers["HX-Redirect"].rsplit("/", 1)[1]
    page = _flat((await web.get(f"/eval/questions/{question_id}")).text)
    assert 'name="q" value="Khi nào đi chợ?"' in page and 'name="from" value="2026-10-20"' in page
    assert "Chưa chọn moment đúng nào" in page
    search = f"/eval/questions/{question_id}/search"
    assert (await web.post(search, data={"q": "đi chợ"})).status_code == 403  # a POST, so no words in any URL
    found = (await web.post(search, data={"q": "đi chợ", "from": "2026-10-20", "to": "2026-10-20"}, headers=HX)).text
    assert "&lt;b&gt;sớm&lt;/b&gt;" in found and f'"moment_id": "{market}"' in found
    picked = await web.post(f"/eval/questions/{question_id}/answers", data={"moment_id": market}, headers=HX)
    assert "Moment đúng (1)" in picked.text and "&lt;b&gt;sớm" in picked.text
    browsed = (await web.post(search, data={"q": "", "from": "2026-10-20", "to": "2026-10-20"}, headers=HX)).text
    assert browsed.count("Chọn là đúng") == 2  # every moment of the day, in time order
    assert "Chọn ngày" in (await web.post(search, data={"q": ""}, headers=HX)).text
    backwards = await web.post(search, data={"q": "x", "from": "2026-10-21", "to": "2026-10-20"}, headers=HX)
    assert backwards.status_code == 200 and "Ngày đầu không được sau ngày cuối." in backwards.text
    assert "chọn 1–3 khoảnh khắc tiêu biểu" in _flat((await web.get("/eval/questions")).text)
    listing = _flat((await web.get("/eval/questions")).text)
    assert "1/25 câu hỏi, 1 đã có moment đúng" in listing and "Khi nào đi chợ?</a>" in listing
    dropped = await web.post(f"/eval/questions/{question_id}/answers/{market}/delete", headers=HX)
    assert "Moment đúng (0)" in dropped.text
    assert (await web.post(f"/eval/questions/{question_id}/answers", data={"moment_id": "01NOSUCH"},
                           headers=HX)).status_code == 404
    edited = await web.post(f"/eval/questions/{question_id}", data={"text": "Đi chợ lúc mấy giờ?", "from": "", "to": ""},
                            headers=HX)
    assert "Đã lưu câu hỏi." in edited.text
    assert (await questions.get_question(pool, question_id))["first_day"] is None
    empty = await web.post(f"/eval/questions/{question_id}", data={"text": " "}, headers=HX)
    assert empty.status_code == 200 and "Câu hỏi cần có chữ." in empty.text
    for bad in ({"text": "x", "from": "2026-10-21", "to": "2026-10-20"}, {"text": " \x00 "}):
        answer = await web.post("/eval/questions", data=bad, headers=HX)
        assert answer.status_code == 200 and "HX-Redirect" not in answer.headers and 'class="err"' in answer.text
    removed = await web.post(f"/eval/questions/{question_id}/delete", headers=HX)
    assert removed.headers["HX-Redirect"] == "/eval/questions"
    for gone in (web.get(f"/eval/questions/{question_id}"),
                 web.post(f"/eval/questions/{question_id}/delete", headers=HX),
                 web.post(f"/eval/questions/{question_id}", data={"text": "x"}, headers=HX)):
        assert (await gone).status_code == 404


async def test_a_run_is_started_from_the_page_and_its_numbers_are_shown(pool, pki_settings, dev, web, monkeypatch):
    from helpers import FakeTranscriber, make_ctx, run_jobs
    from loopi_brain.pipeline import asr
    from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult

    item = await _spoken(pool, pki_settings, dev["device_id"], 0, "vi", "Mai mình đi chợ")
    await golden.add_audio(pool, pki_settings, item, golden.AudioLabel("vi", False, "Mai mình đi chợ nhé"))
    missed = await questions.create_question(pool, "Phòng tối lúc nào?", None, None)
    room = await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, axis=3, text="A dark room")
    await questions.add_answer(pool, missed, room)
    assert "Chưa chạy lần nào." in (await web.get("/eval")).text
    for bad, said in (({"vad_threshold": "2"}, "Ngưỡng VAD nằm giữa 0 và 1."), ({"max_gain_db": "31"}, "0–30 dB"),
                      ({"max_gain_db": "loud"}, "phải là số")):
        answer = await web.post("/eval/runs", data=bad, headers=HX)
        assert answer.status_code == 200 and said in answer.text and "Chưa chạy lần nào." in answer.text
    started = await web.post("/eval/runs", data={"max_gain_db": "", "vad_threshold": "0.4"}, headers=HX)
    assert "đang chờ" in started.text and 'hx-trigger="every 15s"' in started.text and "vad_threshold=0.4" in started.text
    again = await web.post("/eval/runs", data={}, headers=HX)
    assert again.text.count("đang chờ") == 1  # one run at a time
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _threshold: [Region(0.0, 2.0)])
    heard = RegionResult("vi", 0.9, [SegmentResult(0.0, 2.0, "Mai mình đi chợ", -0.3, 0.05, 1.2)])
    await run_jobs(make_ctx(pool, pki_settings, transcriber=FakeTranscriber(heard)))
    page = _flat((await web.get("/eval")).text)
    assert "xong" in page and "20.0 % <span class=\"muted\">(1 đoạn)</span>" in page
    assert "hit@5 0.0 % = 0/1 (95 % 0.0–79.3 %; mục tiêu 70 %, D22)" in page and "(0/1)</span>" in page
    assert "WER (95 %)" in page and "ghi từ 2026-10-20 đến 2026-10-20" in page and "0 đoạn (0 từ)" in page
    assert "Chưa có moment đúng trong 5 kết quả đầu:</p><ul><li>Phòng tối lúc nào?</li>" in page
    order = [page.index(f"<tr><td>{name}") for name in ("live", "none", "outro", "lone_phrase", "short_foreign", "all")]
    assert order == sorted(order)
    # every rate of the rule-set table carries its n, and its interval (none yet: one clip cannot resample), and every
    # secondary search number its n
    assert page.count('20.0 % <span class="muted">(1 đoạn; 95 % —)</span>') == 6
    assert "recall@5 có giới hạn 0.0 % (n = 1), hit@10 0.0 % (n = 1), MRR 0.00 (n = 1)" in page
    assert 'hx-trigger="every 15s"' not in page  # nothing running any more
    fragment = await web.get("/eval/runs")  # what the page polls while a run waits or runs
    assert fragment.text.startswith('<div id="runs">') and "xong" in fragment.text


async def test_every_question_and_run_post_refuses_a_request_without_htmx(pool, pki_settings, dev, web):
    market = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, axis=0, text="Mai mình đi chợ")
    question_id = await questions.create_question(pool, "Khi nào đi chợ?", None, None)
    await questions.add_answer(pool, question_id, market)
    base = f"/eval/questions/{question_id}"
    posts = [("/eval/questions", {"text": "Một câu hỏi mới"}), (base, {"text": "Sửa câu hỏi"}), (f"{base}/delete", {}),
             (f"{base}/search", {"q": "đi chợ", "from": "2026-10-20"}), (f"{base}/answers", {"moment_id": market}),
             (f"{base}/answers/{market}/delete", {}), ("/eval/runs", {"vad_threshold": "0.4"})]
    for path, data in posts:
        assert (await web.post(path, data=data)).status_code == 403, path
    after = await questions.get_question(pool, question_id)
    assert after["text"] == "Khi nào đi chợ?" and [a["moment_id"] for a in after["answers"]] == [market]
    assert (await questions.counts(pool))["questions"] == 1
    assert await fetch_all(pool, "SELECT id FROM eval_run") == []  # nothing was asked for


async def test_a_submit_without_htmx_is_a_post_so_words_never_reach_a_url(pool, pki_settings, dev, web):
    question_id = await questions.create_question(pool, "Khi nào đi chợ?", None, None)
    device = dev["device_id"]
    # Every form of the audio and caption pages must render: a clip to label, a labelled clip, a keyframe to caption
    # and a scored one. Without htmx a form is submitted as a GET of the page, which would put the typed reference
    # transcript in the URL (access log, browser history).
    candidate = await _spoken(pool, pki_settings, device, 0, "vi", SPEECH)
    labelled = await _spoken(pool, pki_settings, device, 10, "vi", SPEECH)
    await web.post("/eval/audio", data={"item_id": labelled, "lang": "vi", "reference": SPEECH}, headers=HX)
    [clip] = await golden.audio_samples(pool)
    proposed = await captioned_keyframe(pool, pki_settings, device, NINE + dt.timedelta(hours=3))
    scored = await captioned_keyframe(pool, pki_settings, device, NINE)
    await web.post("/eval/captions", data={"keyframe_id": scored, "score": "4"}, headers=HX)
    [picture] = await golden.caption_samples(pool)
    pages = {
        "/eval": [],
        "/eval/questions": [],
        f"/eval/questions/{question_id}": [],
        "/eval/audio": [f'src="/audio/{candidate}.opus"', f'src="/eval/audio/{clip["id"]}.opus"',
                        'hx-post="/eval/audio"', f'hx-post="/eval/audio/{clip["id"]}"'],
        "/eval/audio?blind=1": [f'src="/audio/{candidate}.opus"', f'src="/eval/audio/{clip["id"]}.opus"',
                                'hx-post="/eval/audio"', f'hx-post="/eval/audio/{clip["id"]}"'],
        "/eval/captions": [f'src="/thumb/{proposed}.jpg"', f'src="/eval/keyframes/{picture["id"]}.jpg"',
                           'hx-post="/eval/captions"', f'hx-post="/eval/captions/{picture["id"]}/score"'],
    }
    for path, rendered in pages.items():
        html = (await web.get(path)).text
        assert all(part in html for part in rendered), (path, [part for part in rendered if part not in html])
        forms = re.findall(r"<form\b[^>]*>", html)
        assert forms, path
        for tag in forms:
            assert 'method="post"' in tag, (path, tag)


async def test_browsing_a_busy_day_says_when_the_list_stops(pool, dev, web, monkeypatch):
    for minute in range(3):
        await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE + dt.timedelta(minutes=minute), axis=0)
    question_id = await questions.create_question(pool, "Một câu hỏi", None, None)
    search = f"/eval/questions/{question_id}/search"
    day = {"q": "", "from": "2026-10-20", "to": "2026-10-20"}
    assert "Chỉ hiện" not in (await web.post(search, data=day, headers=HX)).text
    monkeypatch.setattr(questions, "BROWSE_LIMIT", 2)
    cut = (await web.post(search, data=day, headers=HX)).text
    assert cut.count("Chọn là đúng") == 2 and "Chỉ hiện 2 khoảnh khắc đầu của các ngày này" in cut


async def test_the_run_table_shows_a_failed_run_a_running_one_and_a_run_over_nothing(pool, pki_settings, web):
    from psycopg.types.json import Jsonb

    from helpers import make_ctx, run_jobs
    from loopi_brain import devices

    async with pool.connection() as conn:
        await conn.execute("INSERT INTO eval_run (id, status, config, error) VALUES (%s, 'failed', %s, %s)",
                           (devices.new_id(), Jsonb({"overrides": {}}), "TimeoutError: the model server"))
    page = _flat((await web.get("/eval/runs")).text)
    assert "lỗi: TimeoutError: the model server" in page and 'hx-trigger="every 15s"' not in page
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO eval_run (id, status, config) VALUES (%s, 'running', %s)",
                           (devices.new_id(), Jsonb({"overrides": {"max_gain_db": 12.0}})))
    page = _flat((await web.get("/eval/runs")).text)
    assert "đang chạy (0/0 đoạn)" in page and "max_gain_db=12.0" in page and 'hx-trigger="every 15s"' in page
    async with pool.connection() as conn:  # a worker that finished it: no golden clip, no question
        await conn.execute("UPDATE eval_run SET status = 'failed', error = 'stopped' WHERE status = 'running'")
    await web.post("/eval/runs", data={}, headers=HX)
    await run_jobs(make_ctx(pool, pki_settings))
    page = _flat((await web.get("/eval/runs")).text)
    assert "xong" in page and "chưa đo được (không có đoạn nào)" in page and "chưa chấm." in page
    assert "Tìm kiếm: chưa đo (chưa có câu hỏi nào có moment đúng)" in page and "<span class=\"muted\">chưa đo</span>" in page


async def test_a_question_deleted_after_a_run_is_still_listed_as_missed(pool, pki_settings, dev, web):
    from helpers import make_ctx, run_jobs

    question_id = await questions.create_question(pool, "Phòng tối lúc nào?", None, None)
    room = await insert_moment(pool, dev["device_id"], kind="visual", ts=NINE, axis=3, text="A dark room")
    await questions.add_answer(pool, question_id, room)
    await web.post("/eval/runs", data={}, headers=HX)
    await run_jobs(make_ctx(pool, pki_settings))
    assert "<li>Phòng tối lúc nào?</li>" in (await web.get("/eval/runs")).text
    await questions.delete_question(pool, question_id)
    assert "<li>(câu hỏi đã xoá)</li>" in (await web.get("/eval/runs")).text


async def test_the_words_of_a_question_and_of_a_search_stay_out_of_the_logs(pool, pki_settings, dev, web, caplog):
    import logging

    from helpers import make_ctx, run_jobs

    caplog.set_level(logging.DEBUG)
    moment = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, axis=0, text="chuyện bí mật gia đình")
    created = await web.post("/eval/questions", data={"text": "chuyện bí mật gia đình", "from": "2026-10-20"}, headers=HX)
    base = created.headers["HX-Redirect"]
    assert "bí mật" not in base  # the redirect is a URL
    question_id = base.rsplit("/", 1)[1]
    search = await web.post(f"{base}/search", data={"q": "bí mật gia đình", "from": "2026-10-20"}, headers=HX)
    assert moment in search.text
    await web.post(f"{base}/answers", data={"moment_id": moment}, headers=HX)
    await web.post(base, data={"text": "chuyện bí mật khác"}, headers=HX)
    await web.post(f"{base}/search", data={"q": "bí\x00 mật", "from": "2026-10-20"}, headers=HX)  # reaches the database
    await web.post("/eval/runs", data={}, headers=HX)
    await run_jobs(make_ctx(pool, pki_settings))
    await web.post(f"/eval/questions/{question_id}/delete", headers=HX)
    assert caplog.records and "bí mật" not in caplog.text and "bí\x00" not in caplog.text


async def test_an_unknown_question_answers_404_on_search_pick_and_drop(pool, dev, web):
    moment = await insert_moment(pool, dev["device_id"], kind="speech", ts=NINE, axis=0, text="Mai mình đi chợ")
    base = "/eval/questions/01NOSUCHQUESTION"
    for path, data in ((f"{base}/search", {"q": "đi chợ", "from": "2026-10-20"}),
                       (f"{base}/search", {"q": "", "from": "2026-10-20"}),
                       (f"{base}/answers", {"moment_id": moment}), (f"{base}/answers/{moment}/delete", {})):
        answer = await web.post(path, data=data, headers=HX)
        assert answer.status_code == 404, path
        assert "Chọn là đúng" not in answer.text


async def test_the_rule_set_table_prints_each_rate_with_its_clips_and_its_interval(
    pool, pki_settings, dev, web, monkeypatch
):
    from helpers import FakeTranscriber, make_ctx, run_jobs
    from loopi_brain.evaluation import runner
    from loopi_brain.pipeline import asr
    from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult

    for minute, reference in ((0, "Mai mình đi chợ nhé"), (1, "Mai mình đi chợ")):
        item = await _spoken(pool, pki_settings, dev["device_id"], minute, "vi", "Mai mình đi chợ")
        await golden.add_audio(pool, pki_settings, item, golden.AudioLabel("vi", False, reference))
    await web.post("/eval/runs", data={}, headers=HX)
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _threshold: [Region(0.0, 2.0)])
    heard = RegionResult("vi", 0.9, [SegmentResult(0.0, 2.0, "Mai mình đi chợ", -0.3, 0.05, 1.2)])
    await run_jobs(make_ctx(pool, pki_settings, transcriber=FakeTranscriber(heard)))
    page = _flat((await web.get("/eval/runs")).text)
    variants = (await fetch_all(pool, "SELECT results FROM eval_run"))[0]["results"]["asr"]["variants"]
    assert set(variants) == set(runner.VARIANTS)
    for name, variant in variants.items():
        row = variant["languages"]["vi"]
        assert row["clips"] == 2 and row["score_ci"] is not None, name
        cell = f'{runner.pct(row["score"])} <span class="muted">(2 đoạn; 95 % {runner.span(row["score_ci"])})</span>'
        assert cell in page, name


async def test_text_over_a_limit_is_refused_with_its_reason_and_never_echoed(pool, pki_settings, dev, web):
    long_text = "bímật " * 400  # 2400 characters: over the old limit of the form field (2000) and over the real one
    answer = await web.post("/eval/questions", data={"text": long_text}, headers=HX)
    assert answer.status_code == 200 and "Câu hỏi dài tối đa 500 ký tự." in answer.text
    assert "bímật" not in answer.text and "HX-Redirect" not in answer.headers
    answer = await web.post("/eval/questions", data={"text": "a" * 501}, headers=HX)
    assert answer.status_code == 200 and "Câu hỏi dài tối đa 500 ký tự." in answer.text
    assert (await questions.counts(pool))["questions"] == 0
    assert "HX-Redirect" in (await web.post("/eval/questions", data={"text": "a" * 500}, headers=HX)).headers
    question_id = (await questions.list_questions(pool))[0]["id"]
    edited = await web.post(f"/eval/questions/{question_id}", data={"text": long_text}, headers=HX)
    assert edited.status_code == 200 and "Câu hỏi dài tối đa 500 ký tự." in edited.text and "bímật" not in edited.text
    assert (await questions.get_question(pool, question_id))["text"] == "a" * 500
    searched = await web.post(f"/eval/questions/{question_id}/search", data={"q": long_text, "from": "2026-10-20"},
                              headers=HX)
    assert searched.status_code == 200 and "Từ tìm kiếm dài tối đa 500 ký tự." in searched.text
    assert "bímật" not in searched.text
    # a reference over the limit comes back in its form with the reason (never FastAPI's 422)
    item = await _spoken(pool, pki_settings, dev["device_id"], 0, "vi", SPEECH)
    for size in (golden.REFERENCE_MAX + 1, 20_001):
        refused = await web.post("/eval/audio", data={"item_id": item, "lang": "vi", "reference": "r" * size},
                                 headers=HX)
        assert refused.status_code == 200 and "Transcript dài tối đa 5000 ký tự." in refused.text
    assert await fetch_all(pool, "SELECT id FROM eval_audio") == []
    sample = await golden.add_audio(pool, pki_settings, item, golden.AudioLabel("vi", False, SPEECH))
    kept = await web.post(f"/eval/audio/{sample}", data={"lang": "vi", "reference": "r" * 20_001}, headers=HX)
    assert kept.status_code == 200 and "Transcript dài tối đa 5000 ký tự." in kept.text
    assert (await fetch_all(pool, "SELECT reference FROM eval_audio"))[0]["reference"] == SPEECH


async def test_a_second_start_says_its_values_were_not_used(pool, web):
    first = await web.post("/eval/runs", data={"vad_threshold": "0.4"}, headers=HX)
    assert "đang chờ" in first.text and "chưa được dùng" not in first.text
    second = await web.post("/eval/runs", data={"max_gain_db": "12", "vad_threshold": "0.3"}, headers=HX)
    assert "Đang có một lần chạy chưa xong nên các giá trị bạn nhập chưa được dùng." in second.text
    assert second.text.count("đang chờ") == 1 and "max_gain_db=12" not in second.text and "vad_threshold=0.4" in second.text
    nothing_typed = await web.post("/eval/runs", data={"max_gain_db": "", "vad_threshold": ""}, headers=HX)
    assert "chưa được dùng" not in nothing_typed.text and nothing_typed.text.count("đang chờ") == 1
    assert len(await fetch_all(pool, "SELECT id FROM eval_run")) == 1
