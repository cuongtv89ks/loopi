"""The Đánh giá page (spec §13, D21): the owner labels the golden set here — clips, keyframes, questions — and
starts evaluation runs. Everything shown comes from the database and LOOPI_EVAL_DIR; nothing is logged with text.

A form the owner filled in is never lost to an error: a value that cannot be saved comes back in the same form with
the reason next to it (a phone shows no message at the top of a long page)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from .. import media
from ..evaluation import golden, questions
from ..evaluation.golden import GoldenError
from .routes import TEMPLATES, require_htmx

LANGUAGE_LABELS = {"vi": "Tiếng Việt", "ko": "Tiếng Hàn", "en": "Tiếng Anh",
                   golden.NOISE: "Có chữ nhưng có thể chỉ là tiếng ồn", "": "Whisper không nghe thấy lời nào",
                   golden.NO_SPEECH: "Không có lời nói (Whisper có viết chữ)",
                   golden.SILENT: "không có lời nói, Whisper cũng không viết gì"}
GROUP_HINTS = {
    golden.NOISE: "Whisper viết chữ cho các đoạn này nhưng câu rất ngắn hoặc không chắc. Nghe thử: nếu không có ai nói"
                  " (chỉ tiếng ồn, TV, im lặng) thì đánh dấu \"Không có lời nói\" — cần ít nhất 6 đoạn như vậy để đo"
                  " việc Whisper bịa câu. Nếu có người nói thật thì sửa transcript như mọi đoạn khác.",
    "": "Whisper không viết gì cho các đoạn này. Chỉ chọn nếu bạn nghe thấy có người nói (để đo lời nói bị bỏ sót);"
        " một đoạn im lặng ở đây không đo được gì.",
}
FLAG_LABELS = {"wrong_people": "sai số người", "invented_objects": "bịa đồ vật", "wrong_activity": "sai hoạt động"}
NOT_SAVED = ("Không lưu được: đoạn ghi đã bị xoá, đã hết hạn, chưa phân tích xong hoặc đã có trong bộ đánh giá."
             " Tải lại trang.")
NOT_SCORED = ("Không lưu được: khung hình đã bị xoá, chưa có mô tả (đang mô tả lại) hoặc đã có trong bộ đánh giá."
              " Tải lại trang.")
NO_LANGUAGE = "Chọn ngôn ngữ chính của đoạn này, hoặc đánh dấu \"Không có lời nói\"."
NO_REFERENCE = "Nhập những gì thật sự được nói, hoặc đánh dấu \"Không có lời nói\"."
NO_SCORE = "Chọn điểm từ 1 đến 5."
TEMPLATES.env.globals["LANGUAGE_LABELS"] = LANGUAGE_LABELS
TEMPLATES.env.globals["FLAG_LABELS"] = FLAG_LABELS

router = APIRouter()


def _local(row: dict, key: str, zone) -> str:
    return row[key].astimezone(zone).strftime("%d/%m/%Y %H:%M")


@router.get("/eval", response_class=HTMLResponse)
async def eval_page(request: Request):
    pool = request.app.state.pool
    return TEMPLATES.TemplateResponse(request, "eval.html", {
        "p": await golden.progress(pool), "q": await questions.counts(pool), "suggested": golden.AUDIO_SUGGESTED,
        "audio_target": golden.AUDIO_TARGET, "caption_target": golden.CAPTION_TARGET,
        "question_target": questions.QUESTION_TARGET,
    })


def _scarce(strata: dict, progress: dict) -> list[str]:
    """Kinds of clip that cannot reach their suggested number with the recordings still proposed."""
    notes = []
    for lang, group in (*((lang, lang) for lang in golden.LANGUAGES), (golden.NO_SPEECH, golden.NOISE)):
        missing = golden.AUDIO_SUGGESTED[lang] - progress["audio"][lang]
        available = strata[group]["available"]
        if missing > available:
            notes.append(f"{LANGUAGE_LABELS[lang]}: chỉ còn {available} đoạn ghi để chọn, còn thiếu {missing} so với"
                         f" gợi ý {golden.AUDIO_SUGGESTED[lang]}. Báo cáo nghiệm thu sẽ ghi đúng số đoạn có được.")
    return notes


def _candidate_view(item: dict, zone) -> dict:
    return {**item, "time": _local(item, "started_at", zone)}


@router.get("/eval/audio", response_class=HTMLResponse)
async def eval_audio_page(request: Request, seed: int = Query(default=0, ge=0, le=1_000_000),
                          blind: bool = Query(default=False)):
    state = request.app.state
    zone = state.settings.zone
    since = state.settings.eval_audio_since
    strata = await golden.audio_candidates(state.pool, zone, seed, since=since)
    for stratum in strata.values():
        stratum["items"] = [_candidate_view(item, zone) for item in stratum["items"]]
    progress = await golden.progress(state.pool)
    samples = [{**s, "time": _local(s, "started_at", zone)} for s in await golden.audio_samples(state.pool)]
    return TEMPLATES.TemplateResponse(request, "eval_audio.html", {
        "strata": strata, "samples": samples, "p": progress, "scarce": _scarce(strata, progress), "seed": seed,
        "suggested": golden.AUDIO_SUGGESTED, "target": golden.AUDIO_TARGET, "languages": golden.LANGUAGES,
        "blind": blind, "hints": GROUP_HINTS, "since": since.astimezone(zone).strftime("%d/%m/%Y %H:%M"),
    })


def _label_error(lang: str, no_speech: bool, reference: str) -> tuple[golden.AudioLabel | None, str | None]:
    """The label, or None and what to fix (in Vietnamese)."""
    if not no_speech and lang not in golden.LANGUAGES:
        return None, NO_LANGUAGE
    try:
        return golden.audio_label(lang, no_speech, reference), None
    except ValueError:
        return None, NO_REFERENCE


async def _candidate(pool, zone, item_id: str) -> dict | None:
    """A proposed recording as the page shows it (to give a form back with an error)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT i.id, i.started_at, {golden.DOMINANT_LANG} AS lang FROM item i"
            " WHERE i.id = %s AND i.kind = 'audio'",
            (item_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    texts = await golden.transcripts(pool, [item_id])
    return _candidate_view({**row, "part": golden.day_part(row["started_at"], zone),
                            "transcript": texts.get(item_id, "")}, zone)


async def _sample_view(request: Request, sample_id: str, typed: dict | None = None,
                       error: str | None = None) -> HTMLResponse:
    zone = request.app.state.settings.zone
    found = [s for s in await golden.audio_samples(request.app.state.pool) if s["id"] == sample_id]
    if not found:
        raise HTTPException(status_code=404, detail="unknown golden clip")
    return TEMPLATES.TemplateResponse(request, "_eval_clip.html", {
        "s": {**found[0], "time": _local(found[0], "started_at", zone), **(typed or {})},
        "languages": golden.LANGUAGES, "saved": error is None, "error": error})


@router.post("/eval/audio", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def add_clip(request: Request, item_id: str = Form(...), lang: str = Form(""), no_speech: str = Form(""),
                   reference: str = Form("", max_length=20_000), prefilled: str = Form("1")):
    state = request.app.state
    label, error = _label_error(lang, no_speech == "on", reference)
    if label is not None:
        try:
            sample_id = await golden.add_audio(state.pool, state.settings, item_id, label, prefilled=prefilled != "0")
        except GoldenError:
            error = NOT_SAVED
        else:
            return await _sample_view(request, sample_id)
    item = await _candidate(state.pool, state.settings.zone, item_id)
    if item is None:
        return HTMLResponse(f'<p class="err">{NOT_SAVED}</p>')
    return TEMPLATES.TemplateResponse(request, "_eval_candidate.html", {
        "it": item, "languages": golden.LANGUAGES, "blind": prefilled == "0",
        "error": error, "typed": {"reference": reference, "lang": lang, "no_speech": no_speech == "on"}})


@router.post("/eval/audio/{sample_id}", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def update_clip(request: Request, sample_id: str, lang: str = Form(""), no_speech: str = Form(""),
                      reference: str = Form("", max_length=20_000)):
    label, error = _label_error(lang, no_speech == "on", reference)
    if label is None:
        return await _sample_view(request, sample_id, {"reference": reference, "lang": lang,
                                                       "no_speech": no_speech == "on"}, error)
    if not await golden.update_audio(request.app.state.pool, sample_id, label):
        raise HTTPException(status_code=404, detail="unknown golden clip")
    return await _sample_view(request, sample_id)


@router.post("/eval/audio/{sample_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def delete_clip(request: Request, sample_id: str):
    if not await golden.delete_audio(request.app.state.pool, request.app.state.settings, sample_id):
        raise HTTPException(status_code=404, detail="unknown golden clip")
    return HTMLResponse('<p class="muted">Đã xoá đoạn này khỏi bộ đánh giá.</p>')


def _eval_file(request: Request, rel: str, media_type: str) -> FileResponse:
    try:
        path = media.inside(request.app.state.settings.eval_dir, rel)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="golden file missing") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="golden file missing")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})


@router.get("/eval/audio/{sample_id}.opus")
async def clip_audio(request: Request, sample_id: str):
    """The golden clip at a level you can hear (its playback copy), or as recorded when there is none."""
    if not await golden.sample_exists(request.app.state.pool, "eval_audio", sample_id):
        raise HTTPException(status_code=404, detail="unknown golden clip")
    copy, view = golden.audio_paths(sample_id)
    if (request.app.state.settings.eval_dir / view).is_file():
        return _eval_file(request, view, "audio/ogg")
    return _eval_file(request, copy, "audio/ogg")


@router.get("/eval/captions", response_class=HTMLResponse)
async def eval_captions_page(request: Request, seed: int = Query(default=0, ge=0, le=1_000_000)):
    state = request.app.state
    zone = state.settings.zone
    candidates = [{**k, "time": _local(k, "ts", zone)} for k in await golden.caption_candidates(state.pool, zone, seed)]
    samples = [{**s, "time": _local(s, "ts", zone)} for s in await golden.caption_samples(state.pool)]
    return TEMPLATES.TemplateResponse(request, "eval_captions.html", {
        "candidates": candidates, "samples": samples, "p": await golden.progress(state.pool), "seed": seed,
        "target": golden.CAPTION_TARGET,
    })


def _flags(wrong_people: str, invented_objects: str, wrong_activity: str) -> set[str]:
    chosen = {"wrong_people": wrong_people, "invented_objects": invented_objects, "wrong_activity": wrong_activity}
    return {name for name, value in chosen.items() if value == "on"}


def _score(value: str) -> int | None:
    try:
        return golden.check_score(int(value))
    except ValueError:
        return None


async def _picture_view(request: Request, sample_id: str, error: str | None = None) -> HTMLResponse:
    zone = request.app.state.settings.zone
    found = [s for s in await golden.caption_samples(request.app.state.pool) if s["id"] == sample_id]
    if not found:
        raise HTTPException(status_code=404, detail="unknown golden keyframe")
    return TEMPLATES.TemplateResponse(request, "_eval_picture.html", {
        "s": {**found[0], "time": _local(found[0], "ts", zone)}, "saved": error is None, "error": error})


async def _keyframe_view(request: Request, keyframe_id: str, error: str) -> HTMLResponse:
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute("SELECT id, ts, caption, caption_model FROM keyframe WHERE id = %s", (keyframe_id,))
        row = await cur.fetchone()
    if row is None:
        return HTMLResponse(f'<p class="err">{NOT_SCORED}</p>')
    return TEMPLATES.TemplateResponse(request, "_eval_keyframe.html", {
        "k": {**row, "time": _local(row, "ts", request.app.state.settings.zone)}, "error": error})


@router.post("/eval/captions", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def add_picture(request: Request, keyframe_id: str = Form(...), score: str = Form(""),
                      wrong_people: str = Form(""), invented_objects: str = Form(""), wrong_activity: str = Form("")):
    state = request.app.state
    chosen = _score(score)
    if chosen is None:
        return await _keyframe_view(request, keyframe_id, NO_SCORE)
    try:
        sample_id = await golden.add_caption(state.pool, state.settings, keyframe_id, chosen,
                                             _flags(wrong_people, invented_objects, wrong_activity))
    except GoldenError:
        return await _keyframe_view(request, keyframe_id, NOT_SCORED)
    return await _picture_view(request, sample_id)


@router.post("/eval/captions/{sample_id}/score", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def score_picture(request: Request, sample_id: str, score: str = Form(""), wrong_people: str = Form(""),
                        invented_objects: str = Form(""), wrong_activity: str = Form("")):
    chosen = _score(score)
    if chosen is None:
        return await _picture_view(request, sample_id, NO_SCORE)
    if not await golden.score_caption(request.app.state.pool, sample_id, chosen,
                                      _flags(wrong_people, invented_objects, wrong_activity)):
        return await _picture_view(request, sample_id, NOT_SCORED)
    return await _picture_view(request, sample_id)


@router.post("/eval/captions/{sample_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def delete_picture(request: Request, sample_id: str):
    if not await golden.delete_caption(request.app.state.pool, request.app.state.settings, sample_id):
        raise HTTPException(status_code=404, detail="unknown golden keyframe")
    return HTMLResponse('<p class="muted">Đã xoá khung hình này khỏi bộ đánh giá.</p>')


@router.get("/eval/keyframes/{sample_id}.jpg")
async def picture_file(request: Request, sample_id: str):
    if not await golden.sample_exists(request.app.state.pool, "eval_caption", sample_id):
        raise HTTPException(status_code=404, detail="unknown golden keyframe")
    return _eval_file(request, golden.keyframe_path(sample_id), "image/jpeg")
