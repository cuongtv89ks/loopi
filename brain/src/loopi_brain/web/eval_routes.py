"""The Đánh giá page (spec §13, D21): the owner labels the golden set here — clips, keyframes, questions — and
starts evaluation runs. Everything shown comes from the database and LOOPI_EVAL_DIR; nothing is logged with text.

A form the owner filled in is never lost to an error: a value that cannot be saved comes back in the same form with
the reason next to it (a phone shows no message at the top of a long page)."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from .. import media, search
from ..evaluation import golden, questions, runner
from ..evaluation.golden import GoldenError
from .routes import TEMPLATES, require_htmx
from .search_routes import KIND_LABELS, OptionalDate, hit_view

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
        "question_target": questions.QUESTION_TARGET, **await _runs_context(request),
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


# ---- questions -----------------------------------------------------------------------------------------------------

DAYS_BACKWARDS = "Ngày đầu không được sau ngày cuối."
NO_QUESTION = "Câu hỏi cần có chữ."
RUN_VALUES = {"gain": "Gain tối đa là 0–30 dB (D19).", "vad": "Ngưỡng VAD nằm giữa 0 và 1.",
              "number": "Gain và ngưỡng VAD phải là số (hoặc để trống)."}


def _question_error(text: str, first: dt.date | None, last: dt.date | None) -> str | None:
    if first and last and first > last:
        return DAYS_BACKWARDS
    return None if golden.clean_text(text, questions.TEXT_MAX) else NO_QUESTION


def _error(text: str, tag: str = "p") -> HTMLResponse:
    return HTMLResponse(f'<{tag} class="err" role="alert">{text}</{tag}>')


@router.get("/eval/questions", response_class=HTMLResponse)
async def eval_questions_page(request: Request):
    pool = request.app.state.pool
    return TEMPLATES.TemplateResponse(request, "eval_questions.html", {
        "questions": await questions.list_questions(pool), "q": await questions.counts(pool),
        "target": questions.QUESTION_TARGET,
    })


@router.post("/eval/questions", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def new_question(request: Request, text: str = Form(..., max_length=2000),
                       first: OptionalDate = Form(default=None, alias="from"),
                       last: OptionalDate = Form(default=None, alias="to")):
    error = _question_error(text, first, last)
    if error:  # shown next to the form, which keeps what was typed
        return _error(error)
    question_id = await questions.create_question(request.app.state.pool, text, first, last)
    return HTMLResponse("", headers={"HX-Redirect": f"/eval/questions/{question_id}"})


async def _question_view(request: Request, question_id: str, template: str) -> HTMLResponse:
    """The question page, or just its list of correct moments, each with its time and kind."""
    question = await questions.get_question(request.app.state.pool, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="unknown question")
    zone = request.app.state.settings.zone
    answers = [{**a, "time": _local(a, "ts_start", zone), "kind_label": KIND_LABELS.get(a["kind"], a["kind"])}
               for a in question["answers"]]
    return TEMPLATES.TemplateResponse(request, template, {"question": question, "answers": answers})


@router.get("/eval/questions/{question_id}", response_class=HTMLResponse)
async def eval_question_page(request: Request, question_id: str):
    return await _question_view(request, question_id, "eval_question.html")


@router.post("/eval/questions/{question_id}", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def edit_question(request: Request, question_id: str, text: str = Form(..., max_length=2000),
                        first: OptionalDate = Form(default=None, alias="from"),
                        last: OptionalDate = Form(default=None, alias="to")):
    error = _question_error(text, first, last)
    if error:
        return _error(error, "span")
    if not await questions.update_question(request.app.state.pool, question_id, text, first, last):
        raise HTTPException(status_code=404, detail="unknown question")
    return HTMLResponse('<span class="online">Đã lưu câu hỏi.</span>')


@router.post("/eval/questions/{question_id}/delete", response_class=HTMLResponse,
             dependencies=[Depends(require_htmx)])
async def remove_question(request: Request, question_id: str):
    if not await questions.delete_question(request.app.state.pool, question_id):
        raise HTTPException(status_code=404, detail="unknown question")
    return HTMLResponse("", headers={"HX-Redirect": "/eval/questions"})


# A POST, although it changes nothing: the words searched for (often the question itself) stay out of URLs and
# so out of the access logs.
@router.post("/eval/questions/{question_id}/search", response_class=HTMLResponse,
             dependencies=[Depends(require_htmx)])
async def question_search(
    request: Request, question_id: str, q: str = Form(default="", max_length=2000),
    first: OptionalDate = Form(default=None, alias="from"),
    last: OptionalDate = Form(default=None, alias="to"),
):
    state = request.app.state
    zone = state.settings.zone
    if first and last and first > last:
        return _error(DAYS_BACKWARDS)
    hits = await questions.label_search(state.pool, search.request_router(state.settings, state.llm), q,
                                        first, last, zone)
    browsing = not search.clean_query(q)
    return TEMPLATES.TemplateResponse(request, "_eval_hits.html", {
        "question_id": question_id, "hits": [hit_view(hit, zone) for hit in hits], "browsing": browsing,
        # Browsing stops at BROWSE_LIMIT moments of the days: say so rather than let a busy day look complete.
        "cut_at": questions.BROWSE_LIMIT if browsing and len(hits) >= questions.BROWSE_LIMIT else None,
    })


@router.post("/eval/questions/{question_id}/answers", response_class=HTMLResponse,
             dependencies=[Depends(require_htmx)])
async def pick_answer(request: Request, question_id: str, moment_id: str = Form(...)):
    if not await questions.add_answer(request.app.state.pool, question_id, moment_id):
        raise HTTPException(status_code=404, detail="unknown question or moment")
    return await _question_view(request, question_id, "_eval_answers.html")


@router.post("/eval/questions/{question_id}/answers/{moment_id}/delete", response_class=HTMLResponse,
             dependencies=[Depends(require_htmx)])
async def drop_answer(request: Request, question_id: str, moment_id: str):
    await questions.remove_answer(request.app.state.pool, question_id, moment_id)
    return await _question_view(request, question_id, "_eval_answers.html")


# ---- runs ----------------------------------------------------------------------------------------------------------

TEMPLATES.env.filters["pct"] = runner.pct
TEMPLATES.env.filters["span"] = runner.span


async def _runs_context(request: Request, error: str | None = None) -> dict:
    state = request.app.state
    zone = state.settings.zone
    runs = await runner.list_runs(state.pool)
    texts = {q["id"]: q["text"] for q in await questions.list_questions(state.pool)}
    views = []
    for run in runs:
        view = {**run, "time": _local(run, "requested_at", zone), "missed": []}
        recall = (run["results"] or {}).get("recall", {})
        for row in recall.get("per_question", []):
            if not any(rank <= 5 for rank in row["ranks"]):
                view["missed"].append(texts.get(row["id"], "(câu hỏi đã xoá)"))
        views.append(view)
    return {"runs": views, "active": any(r["status"] in ("waiting", "running") for r in runs),
            "settings": state.settings, "wer_limit": runner.WER_LIMIT, "distances": runner.DISTANCES,
            "similarities": runner.SIMILARITIES, "variants": runner.VARIANTS, "error": error}


@router.get("/eval/runs", response_class=HTMLResponse)
async def runs_fragment(request: Request):
    return TEMPLATES.TemplateResponse(request, "_eval_runs.html", await _runs_context(request))


def _optional_float(value: str) -> float | None:
    return float(value) if value.strip() else None


@router.post("/eval/runs", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def start_run(request: Request, max_gain_db: str = Form(""), vad_threshold: str = Form("")):
    state = request.app.state
    error = None
    try:
        gain, vad = _optional_float(max_gain_db), _optional_float(vad_threshold)
    except ValueError:
        error = RUN_VALUES["number"]
    else:
        if gain is not None and not 0.0 <= gain <= runner.MAX_GAIN_DB:
            error = RUN_VALUES["gain"]
        elif vad is not None and not 0.0 < vad < 1.0:
            error = RUN_VALUES["vad"]
        else:
            await runner.start_run(state.pool, state.settings, max_gain_db=gain, vad_threshold=vad)
    return TEMPLATES.TemplateResponse(request, "_eval_runs.html", await _runs_context(request, error))
