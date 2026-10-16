"""The owner's questions and their correct moments (spec §13: 25 questions with the right moments, recall@5).

The owner writes a question in Vietnamese, optionally with the local days it is about (the search page's filter:
"hôm qua" means the day the question was written about, whenever it is evaluated), and picks its correct moments
from a labelling search that shows more than the live one: hybrid search with looser thresholds and a longer list,
or every moment of the chosen days in time order — so the truth does not depend on what retrieval finds today.

A correct moment is kept with what it came from (keyframe, recording and time, marker item): reprocessing a day
gives its moments new ids, and once the picked moment is gone the runner still recognises its successor
(``matches``). While the picked moment exists only that moment is correct.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from psycopg_pool import AsyncConnectionPool

from .. import devices, search
from ..days import range_utc
from ..providers.router import Router
from ..search import Hit
from .golden import clean_text

QUESTION_TARGET = 25
TEXT_MAX = 500  # characters, as the search page
LABEL_LIMIT = 50
LABEL_CANDIDATES = 100
LABEL_MAX_DISTANCE = 1.0  # every moment whose meaning is not opposite to the question's
LABEL_MIN_SIMILARITY = 0.1
BROWSE_LIMIT = 200
# A speech region transcribed again starts a little earlier or later: once the picked moment is gone, its successor
# still answers the question if it comes from the same recording and starts within the answer's span, give or take
# this much.
SPEECH_SLACK = dt.timedelta(seconds=2)


def check_question(
    text: str, first: dt.date | None, last: dt.date | None
) -> tuple[str, dt.date | None, dt.date | None]:
    """A question as it is kept; ValueError when it is empty or its days run backwards."""
    text = clean_text(text, TEXT_MAX)
    if not text:
        raise ValueError("a question needs words")
    if first and last and first > last:
        raise ValueError("the first day must not be after the last")
    return text, first, last


async def create_question(pool: AsyncConnectionPool, text: str, first: dt.date | None, last: dt.date | None) -> str:
    text, first, last = check_question(text, first, last)
    question_id = devices.new_id()
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO eval_question (id, text, first_day, last_day) VALUES (%s, %s, %s, %s)",
                           (question_id, text, first, last))
    return question_id


async def update_question(
    pool: AsyncConnectionPool, question_id: str, text: str, first: dt.date | None, last: dt.date | None
) -> bool:
    text, first, last = check_question(text, first, last)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE eval_question SET text = %s, first_day = %s, last_day = %s, updated_at = now() WHERE id = %s",
            (text, first, last, question_id),
        )
        return cur.rowcount == 1


async def delete_question(pool: AsyncConnectionPool, question_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM eval_question WHERE id = %s", (question_id,))
        return cur.rowcount == 1


async def list_questions(pool: AsyncConnectionPool) -> list[dict]:
    """Every question, oldest first, with how many correct moments it has."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT q.*, (SELECT count(*) FROM eval_answer a WHERE a.question_id = q.id) AS answers"
            " FROM eval_question q ORDER BY q.created_at, q.id"
        )
        return await cur.fetchall()


async def get_question(pool: AsyncConnectionPool, question_id: str) -> dict | None:
    """A question with its correct moments in time order. Each has ``live`` (its moment still exists) and, while it
    does, the ``text`` of that moment."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM eval_question WHERE id = %s", (question_id,))
        question = await cur.fetchone()
        if question is None:
            return None
        cur = await conn.execute(
            "SELECT a.*, m.text, (m.id IS NOT NULL) AS live FROM eval_answer a"
            " LEFT JOIN moment m ON m.id = a.moment_id"
            " WHERE a.question_id = %s ORDER BY a.ts_start, a.moment_id",
            (question_id,),
        )
        return {**question, "answers": await cur.fetchall()}


async def add_answer(pool: AsyncConnectionPool, question_id: str, moment_id: str) -> bool:
    """Make ``moment_id`` a correct moment of the question (again is fine); False when either is gone."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT 1 FROM eval_question WHERE id = %s FOR SHARE", (question_id,))
        if await cur.fetchone() is None:
            return False
        cur = await conn.execute(
            "INSERT INTO eval_answer (question_id, moment_id, kind, item_id, keyframe_id, ts_start, ts_end)"
            " SELECT %s, id, kind, item_id, keyframe_id, ts_start, ts_end FROM moment WHERE id = %s"
            " ON CONFLICT (question_id, moment_id) DO NOTHING RETURNING moment_id",
            (question_id, moment_id),
        )
        if await cur.fetchone() is not None:
            return True
        cur = await conn.execute("SELECT 1 FROM eval_answer WHERE question_id = %s AND moment_id = %s",
                                 (question_id, moment_id))
        return await cur.fetchone() is not None


async def remove_answer(pool: AsyncConnectionPool, question_id: str, moment_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM eval_answer WHERE question_id = %s AND moment_id = %s",
                                 (question_id, moment_id))
        return cur.rowcount == 1


async def label_search(
    pool: AsyncConnectionPool, router: Router | None, query: str, first: dt.date | None, last: dt.date | None,
    zone: ZoneInfo,
) -> list[Hit]:
    """What the owner picks correct moments from: with words, the hybrid search with looser thresholds and up to
    LABEL_LIMIT results; without words but with days, every moment of those days in time order (BROWSE_LIMIT)."""
    start, end = range_utc(first, last, zone)
    if search.clean_query(query):
        result = await search.search_moments(
            pool, router, query, start=start, end=end, limit=LABEL_LIMIT, max_distance=LABEL_MAX_DISTANCE,
            min_similarity=LABEL_MIN_SIMILARITY, candidates=LABEL_CANDIDATES,
        )
        return result.hits
    if start is None and end is None:
        return []
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT m.id, m.kind, m.ts_start, m.text, m.lang, m.keyframe_id, m.item_id, i.meta AS item_meta"
            " FROM moment m LEFT JOIN item i ON i.id = m.item_id"
            " WHERE (%(start)s::timestamptz IS NULL OR m.ts_start >= %(start)s)"
            " AND (%(end)s::timestamptz IS NULL OR m.ts_start < %(end)s) ORDER BY m.ts_start, m.id LIMIT %(limit)s",
            {"start": start, "end": end, "limit": BROWSE_LIMIT},
        )
        rows = await cur.fetchall()
    return [Hit(**row, distance=None, similarity=None, score=0.0) for row in rows]


def matches(answer: dict, hit: Hit) -> bool:
    """Is ``hit`` the correct moment ``answer``? The same moment always is. While the picked moment still exists
    (``answer["live"]``) nothing else is: a neighbouring speech region is another moment. Once it is gone (its day
    was reprocessed), the moment its source has now is: same keyframe, same marker item, or speech of the same
    recording starting within the answer's span give or take SPEECH_SLACK."""
    if hit.id == answer["moment_id"]:
        return True
    if answer["live"] or hit.kind != answer["kind"]:
        return False
    if answer["kind"] == "visual":
        return answer["keyframe_id"] is not None and hit.keyframe_id == answer["keyframe_id"]
    if answer["kind"] == "marker":
        return answer["item_id"] is not None and hit.item_id == answer["item_id"]
    return (answer["item_id"] is not None and hit.item_id == answer["item_id"]
            and answer["ts_start"] - SPEECH_SLACK <= hit.ts_start <= answer["ts_end"] + SPEECH_SLACK)


def ranks(answers: list[dict], hits: list[Hit]) -> list[int]:
    """The ranks (from 1) at which correct moments were found. A hit is one moment and counts for at most one answer:
    a hit inside two neighbouring speech answers (each with its slack) finds one of them, not both."""
    found_answers: set[int] = set()
    found = []
    for rank, hit in enumerate(hits, start=1):
        for k, answer in enumerate(answers):
            if k not in found_answers and matches(answer, hit):
                found_answers.add(k)
                found.append(rank)
                break
    return found


async def answered(pool: AsyncConnectionPool) -> list[dict]:
    """The questions that have at least one correct moment, each with its answers (what the runner scores; each
    answer says whether its moment still exists: ``live``).

    The two reads are not one snapshot: a question whose last correct moment the owner removes in between has none
    by the second read and is "not labelled yet" — left out, not an error."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT q.id, q.text, q.first_day, q.last_day FROM eval_question q"
            " WHERE EXISTS (SELECT 1 FROM eval_answer a WHERE a.question_id = q.id) ORDER BY q.created_at, q.id"
        )
        rows = await cur.fetchall()
        cur = await conn.execute(
            "SELECT a.*, EXISTS (SELECT 1 FROM moment m WHERE m.id = a.moment_id) AS live"
            " FROM eval_answer a ORDER BY a.ts_start"
        )
        answers = await cur.fetchall()
    by_question: dict[str, list[dict]] = {}
    for answer in answers:
        by_question.setdefault(answer["question_id"], []).append(answer)
    return [{**row, "answers": by_question[row["id"]]} for row in rows if row["id"] in by_question]


async def counts(pool: AsyncConnectionPool) -> dict:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS questions, count(*) FILTER (WHERE EXISTS (SELECT 1 FROM eval_answer a"
            " WHERE a.question_id = q.id)) AS answered FROM eval_question q"
        )
        return await cur.fetchone()
