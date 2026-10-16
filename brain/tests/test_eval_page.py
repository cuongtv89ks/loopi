import datetime as dt

import httpx
import pytest

from helpers import (
    QUIET_SPEECH,
    FakeOllama,
    analysed_audio,
    captioned_keyframe,
    fetch_all,
    insert_segment,
    paired_device,
)
from loopi_brain.app import create_app
from loopi_brain.evaluation import golden

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
