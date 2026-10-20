# P3c-3 Multi-Camera Understanding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When several cameras listen, each sentence is transcribed once from the microphone that hears it best (D33); a
renamed place refreshes the multi-camera summaries; the daily vlog uses one camera per active 10-minute block and
labels every scene with its place (D35).

**Architecture:** The audio job splits in two around a new grouping step: `detect_speech` (VAD + a signal-to-noise
measure per speech region, stored in a new `speech_region` table) → `group_speech` (waits up to 3 minutes for every
recording microphone's audio of the same minute, groups overlapping regions across microphones, keeps the clearest) →
`transcribe_audio` (Whisper on the kept regions only). Chapter signatures include place names when a chapter names
places, and a rename re-signs the recent multi-camera days. The vlog picks a camera per 10-minute block and renders the
timelapse one camera-run at a time, with an ffmpeg `drawtext` place label.

**Tech Stack:** Python 3.13 (uv), FastAPI/Jinja/HTMX, Postgres 17 + pgvector, Procrastinate 3.10.0, numpy,
faster-whisper (Silero VAD, Whisper large-v3-turbo int8), ffmpeg 7.1 in the brain image (drawtext + DejaVu Sans
present, checked 2026-10-21).

**Spec:** `docs/superpowers/specs/2026-10-19-p3c-multicam-design.md` (D33, D34, D35; §4.3, §4.4, §7, §8.2, §8.5, §9.3)
and the binding spec `docs/superpowers/specs/2026-10-14-loopi-design.md` (D33, D35 rows).

## Global Constraints

- Brain package coverage ≥ 80 % (`cd brain && uv run pytest --cov=loopi_brain`); TDD; conventional commits
  (`feat:`, `fix:`, `test:`, `docs:`, `chore:`); every commit ends with the session's Co-Authored-By trailer.
- The device↔brain contract does not change: `docs/protocol/v1.openapi.yaml`, the agent and its contract tests stay as
  they are. This plan touches only `brain/`, `docs/` and `deploy/README.md`.
- User-facing brain text is Vietnamese; code, comments, commit messages and logs are English.
- D33: "Phát hiện tiếng nói (VAD) chạy trên mọi mic … mỗi nhóm chỉ chép lời một lần, từ mic nghe rõ nhất (tỉ số tiếng
  nói trên nền nhiễu của chính mic đó cao nhất). Câu chỉ một phòng nghe thấy vẫn được chép. Brain chờ tối đa 3 phút
  cho audio của phút đó từ mọi camera."
- §4.3: the SNR is measured on the raw audio, inside the speech regions, against that microphone's own noise floor;
  the other regions of a group are marked "đã chép ở mic khác" and point to the chosen transcript; audio of every
  microphone is still kept 7 days.
- One Whisper run at a time: the transcription job keeps the job lock `"asr"`. A job that waits must never hold `"asr"`
  (Procrastinate's fetch ignores `scheduled_at` for locks: a waiting job holding a lock blocks every later one).
- D35: "Với mỗi 10 phút có hoạt động, phần tua nhanh lấy camera có nhiều moment nhất trong 10 phút đó; ảnh highlight có
  thể từ bất kỳ camera nào; mỗi cảnh có nhãn vị trí nhỏ."
- Migrations are `brain/src/loopi_brain/migrations/NNN_name.sql`; this plan adds `013_speech_region.sql` only. Its
  pins: `brain/tests/conftest.py` `APP_TABLES`, `brain/tests/test_db.py` `EXPECTED_TABLES`, the `applied` set and the
  two exact migration-name lists; cascading foreign keys must be indexed (`test_cascading_foreign_keys_are_indexed`).
- Never print or commit secrets (`deploy/.env`, PKI material, pair tokens, camera passwords); deploy only from
  `/home/soul/loopi-p2/deploy` with `-p deploy`; never prune or delete Docker resources not created by the task.
- With only one camera (today's Pi), results must not change: the same transcripts (one more job hop, no extra wait
  except while speech runs across a minute's end), the same vlog argv (no label).

## Rulings (the owner reviews them with this plan)

| # | Ruling | Why |
|---|---|---|
| R1 | Regions on two microphones belong to one group when they overlap after widening by **2 s** (`GROUP_SLACK`); groups join transitively; regions of the same microphone never join each other directly | a network camera's sound reaches the agent about a second after the Pi's; the agent stamps it on arrival |
| R2 | A fresh group's **winner** is the microphone of its best-SNR region (then the earliest, then the smaller device id); all the winner's regions are transcribed. Any other region is **covered** (not transcribed, `covered_by` the chosen region that hears most of it) when chosen regions of other microphones overlap at least **half** of it; otherwise it is transcribed too. Amended during execution: the overlap test uses the union of the chosen regions of the other microphones (each widened by 2 s), not their sum; that union must cover at least half of the region | "mỗi nhóm … từ mic nghe rõ nhất" without losing speech the winner did not hear (someone walks to another room mid-sentence) |
| R3 | The 3-minute wait (`WAIT`) runs from the **first arrival** (`item.received_at`) of any audio of that minute. The microphones waited for are those heard within 45 s whose heartbeat says `recording_audio`, plus any that already sent audio of that minute. A microphone has delivered a minute when one of its audio items ends at or after the minute's end (−2 s) and its speech detection is done, or — when it no longer records — when all its items of that minute are detected. Amended during execution: the 3-minute wait excuses only audio that has not arrived; audio that has arrived but is still being analysed is always waited for, unless its analysis failed; only audio overlapping the minute by more than 2 s counts for that minute; a camera whose microphone is missing (`mic_missing`) is not waited for | counting from the minute's end would call a camera late whenever the brain was down or its queue was long |
| R4 | Audio that arrives after its minute was decided never changes the decision: its regions are covered by an overlapping chosen region when R2 says so, otherwise transcribed | "camera đến trễ hơn được coi như không có tiếng nói" for the choice, while "câu chỉ một phòng nghe thấy vẫn được chép" |
| R5 | SNR = 20·log10(speech / noise): speech = the 90th percentile of the region's 20 ms frame RMS on the raw audio; noise = `audio_level.noise_floor` of the whole raw segment, but not below **−70 dBFS** | a camera's noise gate sends digital silence; a floor near zero would make that microphone win every group |
| R6 | Three jobs: `detect_speech` (queue `audio`, no lock, deferred by the dispatcher like today's audio job), `group_speech` (queue `light`, queueing lock `group_speech`, no job lock; run at the end of every detection and every minute), `transcribe_audio` (queue `audio`, lock `asr`, queueing lock `asr:<item>`, deferred by `group_speech` only). An item stays `pending` until its regions are decided and its chosen regions transcribed | keeps the `asr` lock free of waiting jobs (Global Constraints) and the dispatcher's one-job-per-item rule |
| R7 | A covered region points to the chosen region (`covered_by`); the chosen region records its speech moment (`moment_id`) after Whisper. The Xử lý page shows how many regions wait for another microphone; nothing else in the UI changes | the spec asks for the mark and the pointer; the Day page already shows the chosen transcript |
| R8 | A `transcribe_audio` job queued before this version (an item never detected) detects first instead of marking the item done | nothing in flight at the deploy is lost |
| R9 | A chapter's signature includes its moments' place names **only when its moments come from two or more devices** (the same rule its prompt uses) | single-camera chapters keep their signatures: no mass re-summarise at the deploy |
| R10 | Renaming a place re-signs the chapters of the last **14 days** (`RENAME_REFRESH_DAYS`) on which that device and another one have moments, marks those days' recaps stale and asks for a chapter build; the recap rewrite then re-scripts the vlog. Amended during execution: the window is the retention video days (7 by default, at most 14); an unchanged name refreshes nothing | older summaries keep the old wording; every live view (timeline, search, chat, MCP) shows the new name at once; the single `llm` lock is not flooded |
| R11 | The vlog's block is 10 minutes from the local day's start. A block counts when it has an active segment (today's rule: a moment within 5 minutes). Its camera is the one with the most moments in the block among the cameras with an active segment there; ties: more segments, then the smaller device id | D35 |
| R12 | The timelapse renders one **run** (consecutive blocks of one camera) per ffmpeg; every selected segment gets the same share of the timelapse, so a 2 fps camera's minute lasts as long as the Pi's. A single run keeps today's argv and file names. Amended during execution: the leftover frames are spread evenly over the day, so every segment gets the same share within one frame | a filtergraph reinit on a resolution change resets `select`'s frame counter; equal time per segment is what `SECONDS_PER_SEGMENT` means |
| R13 | Place labels (`drawtext`, DejaVu Sans, white on a translucent box, bottom left, `expansion=none`) appear on timelapse runs and stills **only when the vlog shows two or more devices**; the names are read at render time. Amended during execution: labels sit at the top left (they never cover the subtitles at the bottom); the brain image installs `fonts-dejavu-core`; without the font the vlog renders without labels | one camera needs no label; a rename shows in the next render |

## Review Focus

1. **One camera only (the Pi today):** a minute's speech is transcribed as soon as that minute's audio is detected, and
   speech crossing the minute's end waits only for the next segment — never 3 minutes. Test:
   `test_one_microphone_is_decided_without_waiting` (Task 4).
2. **A microphone that stops sending** (camera unplugged, Wi-Fi down) holds the others for at most 3 minutes after the
   first arrival. Test: `test_a_silent_microphone_is_waited_for_three_minutes_only` (Task 4).
3. **A noise-gated microphone** (digital silence between words) does not win a group it hears badly. Test:
   `test_a_noise_gate_does_not_make_a_quiet_microphone_win` (Task 2).
4. **Reprocess, forget and retention** leave no region pointing at a deleted row and no item stuck `pending`. Tests:
   `test_reprocessing_transcripts_detects_again`, `test_forgetting_a_range_takes_its_regions` (Task 6).
5. **Mixed cameras in one vlog** (1080p 24 fps next to a 2 fps camera) give exactly the planned frame count, and one
   camera keeps today's argv. Tests: `test_two_cameras_render_their_runs_in_one_vlog`,
   `test_one_camera_keeps_one_run_and_no_label` (Task 9).

---

## File Structure

| File | Responsibility |
|---|---|
| `brain/src/loopi_brain/migrations/013_speech_region.sql` (new) | `speech_region` table, `item.speech_detected_at` |
| `brain/src/loopi_brain/pipeline/speech.py` (new) | pure D33 logic: frame RMS, noise level, SNR, groups, decisions |
| `brain/src/loopi_brain/pipeline/grouping.py` (new) | `group_speech`: the wait (minute readiness), applying decisions, deferring Whisper or finishing items; its periodic job |
| `brain/src/loopi_brain/pipeline/asr.py` | `detect_speech` (VAD + SNR, stores regions), `transcribe_audio` (Whisper on chosen regions) |
| `brain/src/loopi_brain/pipeline/{dispatch,jobs,status}.py` | the audio item's first job is `detect_speech`; job names, labels; the Xử lý count |
| `brain/src/loopi_brain/reprocess.py` | reprocessing transcripts detects again |
| `brain/src/loopi_brain/pipeline/{segment,chapters}.py`, `web/routes.py` | place-aware signature; `refresh_places` after a rename |
| `brain/src/loopi_brain/vlog/{material,script,render}.py`, `pipeline/vlogs.py` | camera per block, devices on frames, runs and labels |

---

### Task 1: Migration 013 — speech regions

**Files:**
- Create: `brain/src/loopi_brain/migrations/013_speech_region.sql`
- Modify: `brain/tests/conftest.py` (`APP_TABLES`), `brain/tests/test_db.py` (`EXPECTED_TABLES`, the `applied` set,
  the exact lists in `test_p1_rows_become_pending_after_upgrade` and `test_concurrent_migrations_apply_each_step_once`)
- Test: `brain/tests/test_speech_region_schema.py` (create)

**Interfaces:**
- Produces: table `speech_region(id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db, state,
  covered_by, moment_id, decided_at, created_at)`; column `item.speech_detected_at TIMESTAMPTZ`.

- [ ] **Step 1: Write the failing test**

`brain/tests/test_speech_region_schema.py`:

```python
import datetime as dt

import psycopg
import pytest

from helpers import T0, fetch_one, insert_item, new_ulid, paired_device


async def _region(pool, item_id, device_id, *, state="waiting", covered_by=None, offset=1.0):
    region_id = new_ulid()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO speech_region (id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db, state,"
            " covered_by) VALUES (%s, %s, %s, %s, %s, %s, 2.0, 20.0, %s, %s)",
            (region_id, item_id, device_id, T0 + dt.timedelta(seconds=offset),
             T0 + dt.timedelta(seconds=offset + 2), offset, state, covered_by),
        )
    return region_id


async def test_regions_go_with_their_item_and_a_cover_is_cleared(pool, settings):
    a, b = await paired_device(pool, settings), await paired_device(pool, settings)
    item_a = await insert_item(pool, a["device_id"], kind="audio")
    item_b = await insert_item(pool, b["device_id"], kind="audio")
    chosen = await _region(pool, item_a, a["device_id"], state="chosen")
    covered = await _region(pool, item_b, b["device_id"], state="covered", covered_by=chosen)
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM item WHERE id = %s", (item_a,))
    assert await fetch_one(pool, "SELECT count(*) AS n FROM speech_region WHERE item_id = %s", (item_a,)) == {"n": 0}
    assert await fetch_one(pool, "SELECT covered_by FROM speech_region WHERE id = %s", (covered,)) == {"covered_by": None}


async def test_a_region_state_is_one_of_three(pool, settings):
    a = await paired_device(pool, settings)
    item = await insert_item(pool, a["device_id"], kind="audio")
    with pytest.raises(psycopg.errors.CheckViolation):
        await _region(pool, item, a["device_id"], state="maybe")


async def test_items_start_without_speech_detection(pool, settings):
    a = await paired_device(pool, settings)
    item = await insert_item(pool, a["device_id"], kind="audio")
    assert await fetch_one(pool, "SELECT speech_detected_at FROM item WHERE id = %s", (item,)) == {
        "speech_detected_at": None}
```

(`fetch_one(pool, sql, params)` is in `tests/helpers.py`; check its signature and adapt the call if it differs.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd brain && uv run pytest tests/test_speech_region_schema.py -q`
Expected: FAIL — `relation "speech_region" does not exist`.

- [ ] **Step 3: Write the migration and update the pins**

`brain/src/loopi_brain/migrations/013_speech_region.sql`:

```sql
-- P3c-3 (D33, P3c design §4.3): the speech found in each audio item, measured, then grouped across microphones
-- before Whisper runs. `state`: waiting (not decided yet), chosen (Whisper transcribes it), covered (another
-- microphone's chosen region holds the same speech: "đã chép ở mic khác"; `covered_by` points to it). The chosen
-- region records its speech moment in `moment_id` once transcribed.
ALTER TABLE item ADD COLUMN speech_detected_at TIMESTAMPTZ;

CREATE TABLE speech_region (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES item (id) ON DELETE CASCADE,
    device_id TEXT NOT NULL,
    start_ts TIMESTAMPTZ NOT NULL,
    end_ts TIMESTAMPTZ NOT NULL,
    offset_s DOUBLE PRECISION NOT NULL CHECK (offset_s >= 0),
    length_s DOUBLE PRECISION NOT NULL CHECK (length_s > 0),
    snr_db DOUBLE PRECISION NOT NULL,
    state TEXT NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting', 'chosen', 'covered')),
    covered_by TEXT REFERENCES speech_region (id) ON DELETE SET NULL,
    moment_id TEXT REFERENCES moment (id) ON DELETE SET NULL,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_ts > start_ts)
);
CREATE INDEX speech_region_item ON speech_region (item_id);
CREATE INDEX speech_region_start ON speech_region (start_ts);
CREATE INDEX speech_region_waiting ON speech_region (start_ts) WHERE state = 'waiting';
CREATE INDEX speech_region_covered_by ON speech_region (covered_by);
CREATE INDEX speech_region_moment ON speech_region (moment_id);
```

Pins:
- `tests/conftest.py` `APP_TABLES`: add `speech_region, ` at the start of the list (it depends on `item` and `moment`).
- `tests/test_db.py`: add `"speech_region"` to `EXPECTED_TABLES`, `"013_speech_region"` to the `applied` set, and
  `"013_speech_region"` at the end of the two exact migration-name lists.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_speech_region_schema.py tests/test_db.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/migrations/013_speech_region.sql brain/tests
git commit -m "feat(brain): speech_region table for grouping speech across microphones (P3c-3, D33)"
```

---

### Task 2: Pure D33 logic — SNR, groups, decisions

**Files:**
- Create: `brain/src/loopi_brain/pipeline/speech.py`
- Test: `brain/tests/test_speech_groups.py` (create)

**Interfaces:**
- Consumes: `audio_level.FRAME` (320 samples), `audio_level.noise_floor(samples) -> float` (linear RMS; check).
- Produces: constants `SAMPLE_RATE = 16_000`, `NOISE_FLOOR_MIN_DBFS = -70.0`, `SPEECH_PERCENTILE = 90`,
  `GROUP_SLACK = timedelta(seconds=2)`, `COVERED_SHARE = 0.5`, `WAIT = timedelta(minutes=3)`;
  `frame_rms(samples) -> np.ndarray`, `noise_level(raw) -> float`, `snr_db(raw, start_s, end_s, noise) -> float`;
  frozen dataclasses `Span(id, device_id, start, end, snr_db, state="waiting")`, `Decision(id, state, covered_by=None)`;
  `groups(spans, slack=GROUP_SLACK) -> list[list[Span]]`, `decide(group, slack=GROUP_SLACK, share=COVERED_SHARE) ->
  list[Decision]`.

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_speech_groups.py`:

```python
import datetime as dt

import numpy as np

from loopi_brain.pipeline import speech
from loopi_brain.pipeline.speech import Decision, Span

T = dt.datetime(2026, 10, 21, 1, 0, tzinfo=dt.UTC)
RATE = speech.SAMPLE_RATE


def span(id, device, start, end, snr, state="waiting"):
    return Span(id, device, T + dt.timedelta(seconds=start), T + dt.timedelta(seconds=end), snr, state)


def tone(seconds, level):
    t = np.arange(int(seconds * RATE)) / RATE
    return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_snr_is_speech_over_the_microphones_own_noise():
    rng = np.random.default_rng(1)
    noise = (rng.standard_normal(RATE * 4) * 0.001).astype(np.float32)  # ≈ -60 dBFS
    audio = noise.copy()
    audio[RATE:RATE * 2] += tone(1, 0.1)  # a loud second
    level = speech.noise_level(audio)
    assert 30 < speech.snr_db(audio, 1.0, 2.0, level) < 45
    assert speech.snr_db(audio, 3.0, 3.5, level) < 6  # noise only
    assert speech.snr_db(audio, 4.0, 4.5, level) == 0.0  # past the end: no frames


def test_a_noise_gate_does_not_make_a_quiet_microphone_win():
    gated = np.zeros(RATE * 4, dtype=np.float32)  # digital silence between words
    gated[RATE:RATE * 2] = tone(1, 0.003)  # the speech, far away: ≈ -53 dBFS
    rng = np.random.default_rng(2)
    near = (rng.standard_normal(RATE * 4) * 0.003).astype(np.float32)  # a real room: ≈ -50 dBFS noise
    near[RATE:RATE * 2] += tone(1, 0.3)  # the same speech, close
    far_snr = speech.snr_db(gated, 1.0, 2.0, speech.noise_level(gated))
    near_snr = speech.snr_db(near, 1.0, 2.0, speech.noise_level(near))
    assert speech.noise_level(gated) == 10 ** (speech.NOISE_FLOOR_MIN_DBFS / 20)
    assert near_snr > far_snr


def test_overlapping_speech_on_two_microphones_is_one_group_and_the_clearer_one_is_kept():
    pi, cam = span("a", "pi", 0, 4, 25.0), span("b", "cam", 0.8, 4.6, 18.0)  # the camera lags by 0.8 s
    (group,) = speech.groups([pi, cam])
    assert speech.decide(group) == [Decision("a", "chosen"), Decision("b", "covered", "a")]


def test_speech_only_one_room_heard_is_kept_on_its_own():
    alone = span("a", "pi", 0, 3, 12.0)
    elsewhere = span("b", "cam", 30, 33, 15.0)
    groups = speech.groups([alone, elsewhere])
    assert [g[0].id for g in groups] == ["a", "b"]
    assert [speech.decide(g) for g in groups] == [[Decision("a", "chosen")], [Decision("b", "chosen")]]


def test_a_region_the_winner_hardly_heard_is_transcribed_too():
    pi = span("a", "pi", 0, 4, 25.0)
    cam = span("b", "cam", 3, 12, 15.0)  # overlaps 3 s (with the 2 s slack) of its 9 s
    assert speech.decide(speech.groups([pi, cam])[0]) == [Decision("a", "chosen"), Decision("b", "chosen")]


def test_the_same_microphones_regions_do_not_chain_into_one_group():
    first, second = span("a", "pi", 0, 2, 20.0), span("b", "pi", 2.5, 4, 20.0)
    assert len(speech.groups([first, second])) == 2


def test_late_audio_keeps_the_decision_already_made():
    chosen = span("a", "cam", 0, 4, 10.0, state="chosen")
    late = span("b", "pi", 0, 4, 30.0)  # clearer, but too late to be chosen
    assert speech.decide(speech.groups([chosen, late])[0]) == [Decision("b", "covered", "a")]


def test_three_microphones_keep_one_transcript():
    a, b, c = span("a", "pi", 0, 4, 20.0), span("b", "cam1", 0.5, 4.5, 26.0), span("c", "cam2", 0.2, 4.2, 14.0)
    assert speech.decide(speech.groups([a, b, c])[0]) == [
        Decision("b", "chosen"), Decision("a", "covered", "b"), Decision("c", "covered", "b")]


def test_ties_go_to_the_earlier_region_then_the_smaller_device():
    a, b = span("a", "pi", 0.5, 4, 20.0), span("b", "cam", 0, 4, 20.0)
    assert speech.decide(speech.groups([a, b])[0])[0] == Decision("b", "chosen")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_speech_groups.py -q`
Expected: FAIL — `ImportError: cannot import name 'speech'`.

- [ ] **Step 3: Write the module**

`brain/src/loopi_brain/pipeline/speech.py`:

```python
"""D33 (P3c design §4.3): one transcript per sentence when several microphones hear it. Pure functions:
``detect_speech`` measures each speech region with ``snr_db``; ``group_speech`` joins the regions that overlap across
microphones with ``groups`` and keeps the clearest with ``decide`` (plan rulings R1, R2, R4, R5)."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np

from .. import audio_level

SAMPLE_RATE = 16_000
NOISE_FLOOR_MIN_DBFS = -70.0  # a noise gate sends digital silence: a floor of ~0 must not win every group (R5)
SPEECH_PERCENTILE = 90  # the speech level: the loud frames of a region, whatever its pauses
GROUP_SLACK = dt.timedelta(seconds=2)  # a network camera's sound reaches its agent about a second late (R1)
COVERED_SHARE = 0.5  # covered when chosen speech elsewhere overlaps at least half of the region (R2)
WAIT = dt.timedelta(minutes=3)  # D33: the longest wait for a minute's audio from every microphone (R3)


def frame_rms(samples: np.ndarray) -> np.ndarray:
    """RMS of each whole 20 ms frame."""
    n = len(samples) // audio_level.FRAME
    if n == 0:
        return np.zeros(0)
    frames = np.asarray(samples[: n * audio_level.FRAME], dtype=np.float64).reshape(n, audio_level.FRAME)
    return np.sqrt((frames ** 2).mean(axis=1))


def noise_level(raw: np.ndarray) -> float:
    """This microphone's noise: the quiet frames of the whole segment, never below NOISE_FLOOR_MIN_DBFS."""
    return max(audio_level.noise_floor(raw), 10 ** (NOISE_FLOOR_MIN_DBFS / 20))


def snr_db(raw: np.ndarray, start_s: float, end_s: float, noise: float) -> float:
    """Speech over noise, in dB, for the region [start_s, end_s) of the raw (not normalised) audio; 0 when the region
    holds no whole frame."""
    rms = frame_rms(raw[int(start_s * SAMPLE_RATE): int(end_s * SAMPLE_RATE)])
    if rms.size == 0:
        return 0.0
    speech = float(np.percentile(rms, SPEECH_PERCENTILE))
    return 20 * math.log10(max(speech, 1e-9) / noise)


@dataclass(frozen=True)
class Span:
    """A speech region as grouping sees it (times are absolute, UTC)."""

    id: str
    device_id: str
    start: dt.datetime
    end: dt.datetime
    snr_db: float
    state: str = "waiting"  # waiting | chosen | covered


@dataclass(frozen=True)
class Decision:
    id: str
    state: str  # chosen | covered
    covered_by: str | None = None


def _heard(a: Span, b: Span, slack: dt.timedelta) -> float:
    """Seconds of ``a`` that ``b``, widened by ``slack`` on both sides, overlaps."""
    lo, hi = max(a.start, b.start - slack), min(a.end, b.end + slack)
    return max(0.0, (hi - lo).total_seconds())


def groups(spans: list[Span], slack: dt.timedelta = GROUP_SLACK) -> list[list[Span]]:
    """Spans of different microphones that overlap within ``slack``, joined transitively; each group in time order,
    the groups by their first span."""
    ordered = sorted(spans, key=lambda s: (s.start, s.device_id, s.id))
    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(ordered):
        for j in range(i + 1, len(ordered)):
            b = ordered[j]
            if b.start >= a.end + slack:
                break
            if b.device_id != a.device_id and b.start < a.end + slack and a.start < b.end + slack:
                parent[find(j)] = find(i)
    out: dict[int, list[Span]] = {}
    for i, s in enumerate(ordered):
        out.setdefault(find(i), []).append(s)
    return sorted(out.values(), key=lambda g: (g[0].start, g[0].id))


def decide(group: list[Span], slack: dt.timedelta = GROUP_SLACK, share: float = COVERED_SHARE) -> list[Decision]:
    """Decisions for the group's waiting spans (R2, R4). A group without decisions picks a winner — the microphone of
    its best-SNR span (then the earliest, then the smaller device id) — and chooses all the winner's spans. A group
    that already has decisions (late audio) keeps them. Every other waiting span, best SNR first, is covered by the
    chosen span of another microphone that hears most of it when chosen spans hear at least ``share`` of it, and is
    chosen otherwise."""
    waiting = [s for s in group if s.state == "waiting"]
    if not waiting:
        return []
    chosen = [s for s in group if s.state == "chosen"]
    decisions: list[Decision] = []
    if len(waiting) == len(group):
        winner = sorted(waiting, key=lambda s: (-s.snr_db, s.start, s.device_id))[0].device_id
        for s in waiting:
            if s.device_id == winner:
                decisions.append(Decision(s.id, "chosen"))
                chosen.append(s)
        waiting = [s for s in waiting if s.device_id != winner]
    for s in sorted(waiting, key=lambda s: (-s.snr_db, s.start, s.id)):
        others = [c for c in chosen if c.device_id != s.device_id]
        length = (s.end - s.start).total_seconds()
        heard = min(length, sum(_heard(s, c, slack) for c in others))
        best = max(others, key=lambda c: (_heard(s, c, slack), c.snr_db), default=None)
        if best is not None and length > 0 and heard >= share * length:
            decisions.append(Decision(s.id, "covered", best.id))
        else:
            decisions.append(Decision(s.id, "chosen"))
            chosen.append(s)
    return decisions
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_speech_groups.py -q`
Expected: PASS. If `test_snr_is_speech_over_the_microphones_own_noise` misses its band because `noise_floor` is not
linear RMS, read `audio_level.noise_floor` and convert in `noise_level`, not in the test.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/pipeline/speech.py brain/tests/test_speech_groups.py
git commit -m "feat(brain): measure speech against each microphone's own noise and keep the clearest of a group (P3c-3, D33)"
```

---

### Task 3: `detect_speech` — the audio item's first job

**Files:**
- Modify: `brain/src/loopi_brain/pipeline/jobs.py` (`DETECT_SPEECH = "detect_speech"`, `GROUP_SPEECH =
  "group_speech"`; `PIPELINE_TASKS` gains `DETECT_SPEECH`; `AUTO_RERUN_TASKS` gains `GROUP_SPEECH`; `TASK_MODULES` gains
  the new `grouping` module of Task 4 — add it in Task 4), `pipeline/dispatch.py` (`_item_job`: audio →
  `(jobs.DETECT_SPEECH, "audio", None)`), `pipeline/status.py` (`TASK_LABELS[jobs.DETECT_SPEECH] = "Tìm tiếng nói"`,
  `TASK_LABELS[jobs.GROUP_SPEECH] = "Ghép tiếng nói giữa các mic"`), `pipeline/asr.py` (`detect_file`, `detect_speech`,
  its job)
- Test: `brain/tests/test_detect_speech.py` (create); update the pins in `tests/test_dispatch.py` (the audio job shape:
  `(DETECT_SPEECH, "audio", None)`, args `{"item_id": id}`, queueing lock `item:<id>`), `tests/test_worker.py` (the
  task registry and the `PIPELINE_TASKS` set) and `tests/test_dispatch.py` periodic registry when Task 4 adds its job

**Interfaces:**
- Consumes: `speech.noise_level`, `speech.snr_db` (Task 2); `asr.load_audio`, `asr.normalise`, `asr.speech_regions`
  (the test seam: tests monkeypatch `asr.speech_regions(audio, threshold)`), `asr.Region`.
- Produces: `asr.detect_file(path, max_gain_db, vad_threshold) -> list[tuple[Region, float]]`;
  `async asr.detect_speech(ctx, item_id) -> int` (regions stored); job `detect_speech_job(item_id)`.
  Task 4 provides `grouping.group_speech(pool, now=None)`; until then `detect_speech` calls it through
  `from . import grouping` — write Task 3 so that the call is `await grouping.group_speech(ctx.pool)` inside
  `try/except Exception: log.exception(...)`, and create `pipeline/grouping.py` in this task with a stub
  `async def group_speech(pool, now=None): return None` that Task 4 replaces (a real module, not a placeholder in the
  plan: it is the seam this task tests against).

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_detect_speech.py`:

```python
import datetime as dt

from helpers import T0, fetch_all, fetch_one, insert_item, make_ctx, make_opus, paired_device
from loopi_brain.pipeline import asr, dispatch, grouping, jobs
from loopi_brain.pipeline.asr import Region


async def _audio(pool, settings, device_id, start=T0, seconds=10):
    item_id = await insert_item(pool, device_id, kind="audio", started_at=start, seconds=seconds)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    make_opus(settings.media_root / row["raw_path"], seconds=seconds)
    return item_id


async def test_speech_regions_are_stored_with_their_snr_and_the_item_waits(pool, settings, monkeypatch):
    dev = await paired_device(pool, settings)
    item_id = await _audio(pool, settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(1.0, 3.5), Region(6.0, 8.0)])
    groupings = []

    async def fake_group(pool, now=None):
        groupings.append(now)

    monkeypatch.setattr(grouping, "group_speech", fake_group)
    assert await asr.detect_speech(make_ctx(pool, settings), item_id) == 2
    rows = await fetch_all(
        pool, "SELECT device_id, start_ts, end_ts, offset_s, length_s, state FROM speech_region ORDER BY start_ts")
    assert [(r["offset_s"], r["length_s"], r["state"]) for r in rows] == [(1.0, 2.5, "waiting"), (6.0, 2.0, "waiting")]
    assert rows[0]["start_ts"] == T0 + dt.timedelta(seconds=1) and rows[0]["device_id"] == dev["device_id"]
    item = await fetch_one(pool, "SELECT analysis_state, speech_detected_at FROM item WHERE id = %s", (item_id,))
    assert item["analysis_state"] == "pending" and item["speech_detected_at"] is not None
    assert groupings == [None]  # grouping runs right after the detection


async def test_audio_without_speech_is_done_at_once(pool, settings, monkeypatch):
    dev = await paired_device(pool, settings)
    item_id = await _audio(pool, settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [])
    assert await asr.detect_speech(make_ctx(pool, settings), item_id) == 0
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "done"


async def test_detecting_again_replaces_the_regions(pool, settings, monkeypatch):
    dev = await paired_device(pool, settings)
    item_id = await _audio(pool, settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(1.0, 3.0)])
    ctx = make_ctx(pool, settings)
    await asr.detect_speech(ctx, item_id)
    await asr.detect_speech(ctx, item_id)
    assert (await fetch_one(pool, "SELECT count(*) AS n FROM speech_region"))["n"] == 1


async def test_a_missing_file_skips_the_item(pool, settings):
    dev = await paired_device(pool, settings)
    item_id = await insert_item(pool, dev["device_id"], kind="audio")
    await asr.detect_speech(make_ctx(pool, settings), item_id)
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "skipped"


async def test_a_grouping_failure_does_not_fail_the_detection(pool, settings, monkeypatch):
    dev = await paired_device(pool, settings)
    item_id = await _audio(pool, settings, dev["device_id"])
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(1.0, 3.0)])

    async def broken(pool, now=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(grouping, "group_speech", broken)
    assert await asr.detect_speech(make_ctx(pool, settings), item_id) == 1


async def test_the_dispatcher_starts_audio_with_detection(pool, settings, jobs_open):
    dev = await paired_device(pool, settings)
    item_id = await insert_item(pool, dev["device_id"], kind="audio")
    await dispatch.defer_item(pool, item_id)
    job = await fetch_one(pool, "SELECT task_name, queue_name, lock, queueing_lock, args FROM procrastinate_jobs")
    assert (job["task_name"], job["queue_name"], job["lock"]) == (jobs.DETECT_SPEECH, "audio", None)
    assert job["queueing_lock"] == f"item:{item_id}" and job["args"] == {"item_id": item_id}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_detect_speech.py -q`
Expected: FAIL — `AttributeError: module 'loopi_brain.pipeline.asr' has no attribute 'detect_speech'`.

- [ ] **Step 3: Implement**

`pipeline/jobs.py`: next to `TRANSCRIBE_AUDIO`, add `DETECT_SPEECH = "detect_speech"` and
`GROUP_SPEECH = "group_speech"`; `PIPELINE_TASKS = (ANALYZE_VIDEO, CAPTION_KEYFRAME, DETECT_SPEECH, TRANSCRIBE_AUDIO,
INDEX_MARKER, EMBED_MOMENTS)`; add `GROUP_SPEECH` to `AUTO_RERUN_TASKS`.

`pipeline/dispatch.py` `_item_job`:

```python
    if kind == "audio":
        return jobs.DETECT_SPEECH, "audio", None  # VAD + SNR; Whisper runs later under "asr" (grouping.group_speech)
```

`pipeline/status.py` `TASK_LABELS`: add `jobs.DETECT_SPEECH: "Tìm tiếng nói"` and
`jobs.GROUP_SPEECH: "Ghép tiếng nói giữa các mic"`.

`pipeline/grouping.py` (the seam; Task 4 replaces the body):

```python
"""group_speech (D33): filled in by plan Task 4."""
from __future__ import annotations


async def group_speech(pool, now=None):
    return None
```

`pipeline/asr.py` — imports: `from . import dispatch, grouping, jobs, speech`; then, after `transcribe_file`:

```python
def detect_file(path: Path, max_gain_db: float = audio_level.MAX_GAIN_DB,
                vad_threshold: float = VAD_THRESHOLD) -> list[tuple[Region, float]]:
    """Blocking: decode; find speech on the level-normalised audio (what Whisper will hear); measure each region's
    SNR on the raw audio against this microphone's own noise (D33, plan R5)."""
    raw = load_audio(path)
    noise = speech.noise_level(raw)
    regions = speech_regions(normalise(raw, max_gain_db), vad_threshold)
    return [(r, speech.snr_db(raw, r.start, r.end, noise)) for r in regions]


async def _detect(ctx: PipelineContext, item_id: str) -> int:
    pool = ctx.pool
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT device_id, kind, started_at, raw_path, analysis_state FROM item WHERE id = %s", (item_id,)
        )
        item = await cur.fetchone()
    if item is None or item["kind"] != "audio" or item["analysis_state"] != "pending":
        return 0
    path = ctx.settings.media_root / item["raw_path"]
    if not path.is_file():
        await finish_item(pool, item_id, "skipped")
        return 0
    found = await anyio.to_thread.run_sync(
        detect_file, path, ctx.settings.asr_max_gain_db, ctx.settings.asr_vad_threshold
    )
    start: dt.datetime = item["started_at"]
    async with pool.connection() as conn, conn.transaction():
        if not await lock_pending_item(conn, item_id):
            return 0
        await conn.execute("DELETE FROM speech_region WHERE item_id = %s", (item_id,))
        for region, snr in found:
            await conn.execute(
                "INSERT INTO speech_region (id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (devices.new_id(), item_id, item["device_id"], start + dt.timedelta(seconds=region.start),
                 start + dt.timedelta(seconds=region.end), region.start, region.end - region.start, snr),
            )
        await conn.execute("UPDATE item SET speech_detected_at = now() WHERE id = %s", (item_id,))
        if not found:
            await mark_item_done(conn, item_id)
    if found:
        try:
            await grouping.group_speech(pool)
        except Exception:  # the periodic group_speech tick tries again within a minute
            log.exception("grouping after the detection of item %s failed", item_id)
    log.info("item %s: %d speech regions", item_id, len(found))
    return len(found)


async def detect_speech(ctx: PipelineContext, item_id: str) -> int:
    """Find and measure the speech of a pending audio item; return the number of regions stored."""
    try:
        return await _detect(ctx, item_id)
    except Exception as exc:
        if not await item_exists(ctx.pool, item_id):
            log.info("item %s was deleted during its speech detection: %s", item_id, error_text(exc))
            return 0
        await record_item_error(ctx.pool, item_id, exc)
        raise


@job_app.task(name=jobs.DETECT_SPEECH, queue="audio", retry=jobs.RETRY)
async def detect_speech_job(item_id: str) -> None:
    await detect_speech(get_context(), item_id)
```

(`grouping.group_speech` is looked up on the module at call time, so the tests' `monkeypatch.setattr(grouping, ...)`
reaches it.) Update the pinned tests named under **Files** to the new audio job shape.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_detect_speech.py tests/test_dispatch.py tests/test_worker.py tests/test_asr.py -q`
Expected: PASS (`test_asr.py` still tests today's `transcribe_audio`, unchanged until Task 5).

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/pipeline brain/tests
git commit -m "feat(brain): detect_speech stores each speech region with its SNR before any Whisper run (P3c-3, D33)"
```

---

### Task 4: `group_speech` — the wait and the decision

**Files:**
- Modify: `brain/src/loopi_brain/pipeline/grouping.py` (replace the seam), `pipeline/jobs.py` (`TASK_MODULES` gains
  `"loopi_brain.pipeline.grouping"` — match the existing entries' form), `tests/test_dispatch.py` (periodic registry:
  `group_speech` every minute)
- Test: `brain/tests/test_group_speech.py` (create)

**Interfaces:**
- Consumes: `speech.Span`, `speech.groups`, `speech.decide`, `speech.GROUP_SLACK`, `speech.WAIT` (Task 2);
  `jobs.enqueue(conn, task, *, queue, queueing_lock, lock=None, priority=None, **kwargs) -> int`;
  `state.lock_pending_item`, `state.mark_item_done`.
- Produces: `EXPECTED_WINDOW = timedelta(seconds=45)`, `END_SLACK = timedelta(seconds=2)`, `GROUP_LOCK = 330_033`
  (advisory lock key), dataclass `Grouped(decided: int, transcribing: list[str], finished: list[str])`,
  `async group_speech(pool, now=None) -> Grouped`, `async minute_ready(conn, minute, now, expected) -> bool`,
  periodic job `group_speech_job` (cron every minute, queue `light`, queueing lock `group_speech`).

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_group_speech.py`:

```python
import datetime as dt

from psycopg.types.json import Jsonb

from helpers import fetch_all, fetch_one, insert_item, new_ulid, paired_device
from loopi_brain.pipeline import grouping, jobs

M = dt.datetime(2026, 10, 21, 1, 0, tzinfo=dt.UTC)  # a minute's start
NOW = M + dt.timedelta(minutes=1, seconds=5)


async def _hb(pool, device_id, *, recording_audio=True, at=NOW):
    """A heartbeat 5 s before ``at`` (the grouping time it must be fresh for)."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE device SET last_heartbeat = %s, last_heartbeat_at = %s WHERE id = %s",
            (Jsonb({"recording_audio": recording_audio}), at - dt.timedelta(seconds=5), device_id),
        )


async def _detected(pool, device_id, regions, *, start=M, seconds=60, received=None, detected=True):
    """An audio item whose speech detection found ``regions`` [(offset, length, snr)]; return (item, region ids)."""
    item_id = await insert_item(pool, device_id, kind="audio", started_at=start, seconds=seconds)
    ids = []
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE item SET received_at = %s, speech_detected_at = %s WHERE id = %s",
            (received or start + dt.timedelta(seconds=seconds, milliseconds=500), NOW if detected else None, item_id),
        )
        for offset, length, snr in regions:
            ids.append(new_ulid())
            await conn.execute(
                "INSERT INTO speech_region (id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (ids[-1], item_id, device_id, start + dt.timedelta(seconds=offset),
                 start + dt.timedelta(seconds=offset + length), offset, length, snr),
            )
    return item_id, ids


async def _states(pool):
    return {r["id"]: (r["state"], r["covered_by"])
            for r in await fetch_all(pool, "SELECT id, state, covered_by FROM speech_region")}


async def test_one_microphone_is_decided_without_waiting(pool, settings, jobs_open):
    pi = await paired_device(pool, settings)
    await _hb(pool, pi["device_id"])
    item, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 20.0)])
    result = await grouping.group_speech(pool, now=NOW)
    assert await _states(pool) == {region: ("chosen", None)}
    assert result.transcribing == [item]
    job = await fetch_one(pool, "SELECT task_name, lock, queueing_lock, args FROM procrastinate_jobs")
    assert (job["task_name"], job["lock"], job["queueing_lock"]) == (jobs.TRANSCRIBE_AUDIO, "asr", f"asr:{item}")
    assert (await fetch_one(pool, "SELECT analysis_job_id FROM item WHERE id = %s", (item,)))["analysis_job_id"]


async def test_speech_at_a_minutes_end_waits_for_the_next_segment(pool, settings, jobs_open):
    pi = await paired_device(pool, settings)
    await _hb(pool, pi["device_id"])
    item, (region,) = await _detected(pool, pi["device_id"], [(57.5, 2.5, 20.0)])  # runs into the next minute
    await grouping.group_speech(pool, now=NOW)
    assert await _states(pool) == {region: ("waiting", None)}
    nxt, _ = await _detected(pool, pi["device_id"], [], start=M + dt.timedelta(minutes=1))
    await grouping.group_speech(pool, now=NOW + dt.timedelta(minutes=1))
    assert (await _states(pool))[region] == ("chosen", None)


async def test_two_microphones_wait_for_each_other_then_keep_the_clearer(pool, settings, jobs_open):
    pi, cam = await paired_device(pool, settings), await paired_device(pool, settings)
    await _hb(pool, pi["device_id"])
    await _hb(pool, cam["device_id"])
    pi_item, (pi_region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)])
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[pi_region] == ("waiting", None)  # the camera's minute has not arrived
    cam_item, (cam_region,) = await _detected(pool, cam["device_id"], [(10.8, 3, 15.0)])
    result = await grouping.group_speech(pool, now=NOW + dt.timedelta(seconds=20))
    assert await _states(pool) == {pi_region: ("chosen", None), cam_region: ("covered", pi_region)}
    assert result.transcribing == [pi_item] and result.finished == [cam_item]
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (cam_item,)))["analysis_state"] == "done"


async def test_a_silent_microphone_is_waited_for_three_minutes_only(pool, settings, jobs_open):
    pi, cam = await paired_device(pool, settings), await paired_device(pool, settings)
    arrived = M + dt.timedelta(minutes=1, seconds=1)
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)], received=arrived)
    almost, late = arrived + dt.timedelta(minutes=2, seconds=59), arrived + dt.timedelta(minutes=3)
    for at in (almost, late):  # the camera keeps saying it records, but its audio never comes
        await _hb(pool, pi["device_id"], at=at)
        await _hb(pool, cam["device_id"], at=at)
        await grouping.group_speech(pool, now=at)
        assert (await _states(pool))[region] == (("waiting" if at == almost else "chosen"), None)


async def test_a_microphone_that_stopped_recording_is_not_waited_for(pool, settings, jobs_open):
    pi, cam = await paired_device(pool, settings), await paired_device(pool, settings)
    await _hb(pool, pi["device_id"])
    await _hb(pool, cam["device_id"], recording_audio=False)  # paused
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)])
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[region] == ("chosen", None)


async def test_a_microphone_still_detecting_its_audio_is_waited_for(pool, settings, jobs_open):
    pi, cam = await paired_device(pool, settings), await paired_device(pool, settings)
    await _hb(pool, pi["device_id"])
    await _hb(pool, cam["device_id"])
    _, (region,) = await _detected(pool, pi["device_id"], [(10, 3, 22.0)])
    await _detected(pool, cam["device_id"], [], detected=False)  # arrived, detection still running
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[region] == ("waiting", None)


async def test_late_audio_does_not_change_the_decision(pool, settings, jobs_open):
    pi, cam = await paired_device(pool, settings), await paired_device(pool, settings)
    await _hb(pool, cam["device_id"], recording_audio=False)
    await _hb(pool, pi["device_id"])
    _, (pi_region,) = await _detected(pool, pi["device_id"], [(10, 3, 12.0)])
    await grouping.group_speech(pool, now=NOW)
    assert (await _states(pool))[pi_region] == ("chosen", None)
    late_item, (late_region,) = await _detected(pool, cam["device_id"], [(10.5, 3, 30.0)],
                                                received=NOW + dt.timedelta(minutes=10))
    await grouping.group_speech(pool, now=NOW + dt.timedelta(minutes=10))
    assert await _states(pool) == {pi_region: ("chosen", None), late_region: ("covered", pi_region)}


async def test_an_item_is_not_queued_twice(pool, settings, jobs_open):
    pi = await paired_device(pool, settings)
    await _hb(pool, pi["device_id"])
    await _detected(pool, pi["device_id"], [(10, 3, 20.0)])
    await grouping.group_speech(pool, now=NOW)
    async with pool.connection() as conn:
        await conn.execute("UPDATE procrastinate_jobs SET status = 'doing'")  # Whisper is running
    await grouping.group_speech(pool, now=NOW + dt.timedelta(minutes=1))
    assert (await fetch_one(pool, "SELECT count(*) AS n FROM procrastinate_jobs"))["n"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_group_speech.py -q`
Expected: FAIL — `AttributeError: 'NoneType' object has no attribute 'transcribing'` (the seam returns None).

- [ ] **Step 3: Implement**

`brain/src/loopi_brain/pipeline/grouping.py`:

```python
"""group_speech (D33, P3c design §4.3; plan rulings R2–R4, R6): decide which speech regions Whisper transcribes.

A region waits until every minute its group touches is ready: every microphone that records audio has delivered that
minute (``minute_ready``), or WAIT has passed since the minute's first audio arrived. Then ``speech.decide`` keeps the
clearest region of each group; an item whose regions are all decided gets its Whisper job (lock "asr") when it has a
chosen region, and is done otherwise. Runs at the end of every detection and every minute."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from . import jobs, speech
from .context import get_context
from .jobs import job_app
from .state import lock_pending_item, mark_item_done

log = logging.getLogger(__name__)
EXPECTED_WINDOW = dt.timedelta(seconds=45)  # a microphone heard from this recently and recording is waited for
END_SLACK = dt.timedelta(seconds=2)  # an item ending this close to a minute's end has delivered that minute
GROUP_LOCK = 330_033  # pg advisory lock: one grouping at a time (detections end concurrently)
MINUTE = dt.timedelta(minutes=1)


@dataclass
class Grouped:
    decided: int = 0
    transcribing: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)


def _minute(t: dt.datetime) -> dt.datetime:
    return t.replace(second=0, microsecond=0)


def minutes_touched(span: speech.Span, slack: dt.timedelta = speech.GROUP_SLACK) -> list[dt.datetime]:
    """Every wall-clock minute the span reaches, widened by ``slack`` (speech near a minute's end needs the next)."""
    out, m = [], _minute(span.start - slack)
    while m <= span.end + slack:
        out.append(m)
        m += MINUTE
    return out


async def _expected(conn: AsyncConnection, now: dt.datetime) -> set[str]:
    cur = await conn.execute(
        "SELECT id FROM device WHERE NOT revoked AND last_heartbeat_at > %s"
        " AND last_heartbeat @> '{\"recording_audio\": true}'",
        (now - EXPECTED_WINDOW,),
    )
    return {r["id"] for r in await cur.fetchall()}


async def minute_ready(conn: AsyncConnection, minute: dt.datetime, now: dt.datetime, expected: set[str]) -> bool:
    """R3: every microphone waited for has delivered ``minute``, or WAIT has passed since its first audio arrived."""
    end = minute + MINUTE
    cur = await conn.execute(
        "SELECT device_id, ended_at, received_at, speech_detected_at, analysis_state FROM item"
        " WHERE kind = 'audio' AND started_at < %s AND ended_at > %s",
        (end, minute),
    )
    rows = await cur.fetchall()
    if not rows:
        return not expected or now >= end + speech.WAIT
    if now >= min(r["received_at"] for r in rows) + speech.WAIT:
        return True

    def detected(r) -> bool:
        return r["speech_detected_at"] is not None or r["analysis_state"] != "pending"

    for device in expected | {r["device_id"] for r in rows}:
        mine = [r for r in rows if r["device_id"] == device]
        if any(r["ended_at"] >= end - END_SLACK and detected(r) for r in mine):
            continue
        if device not in expected and mine and all(detected(r) for r in mine):
            continue
        return False
    return True


_SPANS = ("SELECT id, device_id, start_ts, end_ts, snr_db, state FROM speech_region"
          " WHERE {} ORDER BY start_ts, id")


def _span(r) -> speech.Span:
    return speech.Span(r["id"], r["device_id"], r["start_ts"], r["end_ts"], r["snr_db"], r["state"])


async def _decide(conn: AsyncConnection, now: dt.datetime) -> int:
    cur = await conn.execute(_SPANS.format("state = 'waiting'"))
    waiting = [_span(r) for r in await cur.fetchall()]
    if not waiting:
        return 0
    reach = 2 * speech.GROUP_SLACK
    cur = await conn.execute(
        _SPANS.format("state <> 'waiting' AND start_ts < %s AND end_ts > %s"),
        (max(s.end for s in waiting) + reach, min(s.start for s in waiting) - reach),
    )
    decided = [_span(r) for r in await cur.fetchall()]
    expected = await _expected(conn, now)
    ready: dict[dt.datetime, bool] = {}
    count = 0
    for group in speech.groups(waiting + decided):
        if not any(s.state == "waiting" for s in group):
            continue
        minutes = sorted({m for s in group for m in minutes_touched(s)})
        for m in minutes:
            if m not in ready:
                ready[m] = await minute_ready(conn, m, now, expected)
        if not all(ready[m] for m in minutes):
            continue
        for d in speech.decide(group):
            await conn.execute(
                "UPDATE speech_region SET state = %s, covered_by = %s, decided_at = %s WHERE id = %s AND state = 'waiting'",
                (d.state, d.covered_by, now, d.id),
            )
            count += 1
    return count


async def _settle_items(conn: AsyncConnection, result: Grouped) -> None:
    cur = await conn.execute(
        "SELECT i.id, j.task_name, j.status, bool_or(r.state = 'waiting') AS waiting,"
        " bool_or(r.state = 'chosen') AS chosen"
        " FROM item i JOIN speech_region r ON r.item_id = i.id LEFT JOIN procrastinate_jobs j ON j.id = i.analysis_job_id"
        " WHERE i.kind = 'audio' AND i.analysis_state = 'pending' AND i.speech_detected_at IS NOT NULL"
        " GROUP BY i.id, j.task_name, j.status ORDER BY i.id"
    )
    for row in await cur.fetchall():
        if row["waiting"]:
            continue
        if row["chosen"]:
            if row["task_name"] == jobs.TRANSCRIBE_AUDIO and row["status"] in ("todo", "doing", "failed"):
                continue  # queued, running, or on the dead-letter list (retried from Xử lý)
            if not await lock_pending_item(conn, row["id"]):
                continue
            job_id = await jobs.enqueue(conn, jobs.TRANSCRIBE_AUDIO, queue="audio", lock="asr",
                                        queueing_lock=f"asr:{row['id']}", item_id=row["id"])
            await conn.execute("UPDATE item SET analysis_job_id = %s WHERE id = %s", (job_id, row["id"]))
            result.transcribing.append(row["id"])
        elif await lock_pending_item(conn, row["id"]):
            await mark_item_done(conn, row["id"])  # every region was heard better elsewhere
            result.finished.append(row["id"])


async def group_speech(pool: AsyncConnectionPool, now: dt.datetime | None = None) -> Grouped:
    """One grouping pass (see the module docstring)."""
    now = now or dt.datetime.now(dt.UTC)
    result = Grouped()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (GROUP_LOCK,))
        result.decided = await _decide(conn, now)
        await _settle_items(conn, result)
    if result.decided or result.transcribing or result.finished:
        log.info("speech grouping: %d regions decided, %d items to transcribe, %d items covered elsewhere",
                 result.decided, len(result.transcribing), len(result.finished))
    return result


@job_app.periodic(cron="* * * * *")
@job_app.task(name=jobs.GROUP_SPEECH, queue="light", queueing_lock="group_speech")
async def group_speech_job(timestamp: int | None = None) -> None:
    await group_speech(get_context().pool)
```

Add `grouping` to `TASK_MODULES` in `jobs.py` and the `group_speech` periodic entry to the registry pin in
`tests/test_dispatch.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_group_speech.py tests/test_detect_speech.py tests/test_dispatch.py tests/test_worker.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/pipeline brain/tests
git commit -m "feat(brain): group_speech waits up to 3 minutes for every microphone and keeps one region per sentence (P3c-3, D33)"
```

---

### Task 5: `transcribe_audio` — Whisper on the chosen regions

**Files:**
- Modify: `brain/src/loopi_brain/pipeline/asr.py` (`transcribe_regions`, `_transcribe`; `transcribe_file` keeps its
  behaviour for the evaluation runner)
- Modify: `brain/tests/test_asr.py` (its job tests now seed regions or run detection first; keep every assertion about
  segments, moments, rules and languages)
- Test: `brain/tests/test_transcribe_chosen.py` (create)

**Interfaces:**
- Consumes: `speech_region` rows with `state = 'chosen'` (Tasks 1, 4); `asr.detect_speech` (Task 3, for R8).
- Produces: `asr.transcribe_regions(path, transcriber, regions: list[Region], max_gain_db) ->
  list[tuple[Region, RegionResult]]`; `transcribe_audio(ctx, item_id) -> int` (speech moments written) now
  transcribes only the item's chosen regions and sets `speech_region.moment_id`.

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_transcribe_chosen.py`:

```python
import datetime as dt

from helpers import T0, FakeTranscriber, fetch_all, fetch_one, insert_item, make_ctx, make_opus, new_ulid, paired_device
from loopi_brain.pipeline import asr, grouping
from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult

HELLO = RegionResult("vi", 0.9, [SegmentResult(0.0, 1.5, "xin chào", -0.2, 0.05, 1.2)])


async def _item(pool, settings, device_id, regions):
    item_id = await insert_item(pool, device_id, kind="audio", started_at=T0, seconds=10)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    make_opus(settings.media_root / row["raw_path"], seconds=10)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET speech_detected_at = now() WHERE id = %s", (item_id,))
        for offset, length, state in regions:
            await conn.execute(
                "INSERT INTO speech_region (id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db, state)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, 20, %s)",
                (new_ulid(), item_id, device_id, T0 + dt.timedelta(seconds=offset),
                 T0 + dt.timedelta(seconds=offset + length), offset, length, state),
            )
    return item_id


async def test_only_the_chosen_regions_reach_whisper(pool, settings):
    dev = await paired_device(pool, settings)
    item_id = await _item(pool, settings, dev["device_id"], [(1.0, 2.0, "chosen"), (5.0, 2.0, "covered")])
    whisper = FakeTranscriber(HELLO)
    assert await asr.transcribe_audio(make_ctx(pool, settings, transcriber=whisper), item_id) == 1
    assert whisper.chunks == [2 * asr.SAMPLE_RATE]
    moment = await fetch_one(pool, "SELECT id, ts_start, text FROM moment")
    assert moment["text"] == "xin chào" and moment["ts_start"] == T0 + dt.timedelta(seconds=1)
    region = await fetch_one(pool, "SELECT moment_id FROM speech_region WHERE state = 'chosen'")
    assert region["moment_id"] == moment["id"]
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "done"


async def test_transcribing_again_replaces_the_transcript(pool, settings):
    dev = await paired_device(pool, settings)
    item_id = await _item(pool, settings, dev["device_id"], [(1.0, 2.0, "chosen")])
    ctx = make_ctx(pool, settings, transcriber=FakeTranscriber(HELLO))
    await asr.transcribe_audio(ctx, item_id)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'pending' WHERE id = %s", (item_id,))
    await asr.transcribe_audio(ctx, item_id)
    assert len(await fetch_all(pool, "SELECT id FROM moment")) == 1


async def test_a_job_from_before_this_version_detects_first(pool, settings, monkeypatch):
    dev = await paired_device(pool, settings)
    item_id = await insert_item(pool, dev["device_id"], kind="audio", started_at=T0, seconds=10)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    make_opus(settings.media_root / row["raw_path"], seconds=10)
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(1.0, 3.0)])

    async def no_grouping(pool, now=None):
        return None

    monkeypatch.setattr(grouping, "group_speech", no_grouping)
    whisper = FakeTranscriber(HELLO)
    await asr.transcribe_audio(make_ctx(pool, settings, transcriber=whisper), item_id)
    assert whisper.chunks == []  # detected, now waiting for grouping; nothing lost
    assert (await fetch_one(pool, "SELECT state FROM speech_region"))["state"] == "waiting"
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "pending"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_transcribe_chosen.py -q`
Expected: FAIL — today's `transcribe_audio` runs VAD itself (the chunk lengths and the R8 case differ).

- [ ] **Step 3: Implement**

In `asr.py`, split `transcribe_file`:

```python
def transcribe_regions(path: Path, transcriber: Transcriber, regions: list[Region],
                       max_gain_db: float = audio_level.MAX_GAIN_DB) -> list[tuple[Region, RegionResult]]:
    """Blocking: decode, normalise the level, transcribe each given region (segment times stay region-relative)."""
    audio = normalise(load_audio(path), max_gain_db)
    return [(r, transcriber.transcribe(audio[int(r.start * SAMPLE_RATE): int(r.end * SAMPLE_RATE)]))
            for r in regions]


def transcribe_file(path, transcriber, max_gain_db=audio_level.MAX_GAIN_DB, vad_threshold=VAD_THRESHOLD):
    """Blocking: decode, normalise the level, find speech, transcribe each region (the evaluation runner's way)."""
    audio = normalise(load_audio(path), max_gain_db)
    results = []
    for region in speech_regions(audio, vad_threshold):
        chunk = audio[int(region.start * SAMPLE_RATE): int(region.end * SAMPLE_RATE)]
        results.append((region, transcriber.transcribe(chunk)))
    return results
```

Rewrite `_transcribe` (keep the insertion code of today; the changes are the item query, the chosen regions, the
R8 branch and `moment_id`):

```python
async def _transcribe(ctx: PipelineContext, item_id: str) -> int:
    pool = ctx.pool
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT device_id, kind, started_at, raw_path, analysis_state, speech_detected_at FROM item WHERE id = %s",
            (item_id,),
        )
        item = await cur.fetchone()
    if item is None or item["kind"] != "audio" or item["analysis_state"] != "pending":
        return 0
    if item["speech_detected_at"] is None:  # R8: a job queued before detect_speech existed: detect, then wait
        await _detect(ctx, item_id)
        return 0
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, offset_s, length_s FROM speech_region WHERE item_id = %s AND state = 'chosen' ORDER BY offset_s",
            (item_id,),
        )
        chosen = await cur.fetchall()
    path = ctx.settings.media_root / item["raw_path"]
    if not path.is_file():
        await finish_item(pool, item_id, "skipped")
        return 0
    settings = ctx.settings
    regions = [Region(r["offset_s"], r["offset_s"] + r["length_s"]) for r in chosen]
    results = await anyio.to_thread.run_sync(transcribe_regions, path, ctx.transcriber, regions,
                                             settings.asr_max_gain_db)
    version = ctx.router.model_version("asr")
    start: dt.datetime = item["started_at"]
    moments = 0
    async with pool.connection() as conn, conn.transaction():
        if not await lock_pending_item(conn, item_id):
            log.info("item %s was deleted during its transcription; nothing stored", item_id)
            return 0
        await conn.execute("DELETE FROM transcript_segment WHERE item_id = %s", (item_id,))
        for row, (region, result) in zip(chosen, results):
            kept = kept_segments(result, settings.asr_rule_set)
            if not kept:
                continue
            # ... today's INSERT INTO transcript_segment loop, unchanged (segment_ids) ...
            moment_id = devices.new_id()
            # ... today's INSERT INTO moment, with moment_id as its id ...
            await conn.execute("UPDATE speech_region SET moment_id = %s WHERE id = %s", (moment_id, row["id"]))
            moments += 1
        await mark_item_done(conn, item_id)
    if moments:
        await dispatch.defer_embed(pool)
    log.info("item %s: %d chosen regions, %d kept", item_id, len(chosen), moments)
    return moments
```

(The two `# ...` lines are today's code from `_transcribe`, moved as they are: the `transcript_segment` INSERT loop and
the `moment` INSERT, whose first value becomes `moment_id`.) `transcribe_audio`, `transcribe_audio_job` keep their
names, queue and retry; the job is now deferred by `grouping` with lock `asr`.

Update `tests/test_asr.py`: its job-level tests seed a chosen region per scripted VAD region (use the `_item` helper
pattern above) or call `detect_speech` and then `group_speech` with one heartbeat-less device (no expected microphone →
decided at once); pure-function tests (`merge_regions`, `kept_segments`, `pick_language`, `transcribe_file`) stay.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_transcribe_chosen.py tests/test_asr.py tests/test_worker.py tests/test_eval_runner.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/pipeline/asr.py brain/tests
git commit -m "feat(brain): Whisper transcribes only the regions grouping chose; a covered region points to them (P3c-3, D33)"
```

---

### Task 6: The day's other paths — reprocess, forget, the Xử lý page, end to end

**Files:**
- Modify: `brain/src/loopi_brain/reprocess.py` (transcripts), `pipeline/status.py` and its template (the waiting
  count), `templates/pipeline.html`
- Test: `brain/tests/test_multimic_paths.py` (create)

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: `status` data key `speech_waiting: int`; the Xử lý page line `Tiếng nói chờ mic khác: N đoạn`.

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_multimic_paths.py`:

```python
import datetime as dt

from psycopg.types.json import Jsonb

from helpers import FakeTranscriber, fetch_all, fetch_one, insert_item, make_ctx, make_opus, paired_device, run_jobs
from loopi_brain import forget, reprocess
from loopi_brain.pipeline import asr, dispatch
from loopi_brain.pipeline.asr import Region, RegionResult, SegmentResult

NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul
HELLO = RegionResult("vi", 0.9, [SegmentResult(0.0, 1.5, "xin chào", -0.2, 0.05, 1.2)])


async def _recorded(pool, settings, device_id):
    item_id = await insert_item(pool, device_id, kind="audio", started_at=NINE, seconds=60)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    make_opus(settings.media_root / row["raw_path"], seconds=60)
    return item_id


async def test_one_sentence_heard_by_two_microphones_gives_one_transcript(pool, settings, jobs_open, monkeypatch):
    """§8.2 in miniature: the whole pipeline with real jobs."""
    pi, cam = await paired_device(pool, settings), await paired_device(pool, settings)
    for dev in (pi, cam):
        async with pool.connection() as conn:
            await conn.execute("UPDATE device SET last_heartbeat = %s, last_heartbeat_at = now() WHERE id = %s",
                               (Jsonb({"recording_audio": True}), dev["device_id"]))
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(10.0, 13.0)])
    items = [await _recorded(pool, settings, d["device_id"]) for d in (pi, cam)]
    for item_id in items:
        await dispatch.defer_item(pool, item_id)
    await run_jobs(make_ctx(pool, settings, transcriber=FakeTranscriber(HELLO)))
    assert len(await fetch_all(pool, "SELECT id FROM moment WHERE kind = 'speech'")) == 1
    states = sorted(r["state"] for r in await fetch_all(pool, "SELECT state FROM speech_region"))
    assert states == ["chosen", "covered"]
    assert {r["analysis_state"] for r in await fetch_all(pool, "SELECT analysis_state FROM item")} == {"done"}


async def test_reprocessing_transcripts_detects_again(pool, settings, jobs_open, monkeypatch):
    dev = await paired_device(pool, settings)
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(10.0, 13.0)])
    item_id = await _recorded(pool, settings, dev["device_id"])
    await dispatch.defer_item(pool, item_id)
    await run_jobs(make_ctx(pool, settings, transcriber=FakeTranscriber(HELLO)))
    await reprocess.reprocess_day(pool, NINE.date(), settings.zone, {"transcripts"})
    row = await fetch_one(pool, "SELECT analysis_state, analysis_job_id, speech_detected_at FROM item WHERE id = %s",
                          (item_id,))
    assert row == {"analysis_state": "pending", "analysis_job_id": None, "speech_detected_at": None}
    assert await fetch_all(pool, "SELECT id FROM speech_region") == []


async def test_forgetting_a_range_takes_its_regions(pool, settings, jobs_open, monkeypatch):
    dev = await paired_device(pool, settings)
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [Region(10.0, 13.0)])
    item_id = await _recorded(pool, settings, dev["device_id"])
    await dispatch.defer_item(pool, item_id)
    await run_jobs(make_ctx(pool, settings, transcriber=FakeTranscriber(HELLO)))
    await forget.forget_range(pool, settings, NINE, NINE + dt.timedelta(minutes=2))
    assert await fetch_all(pool, "SELECT id FROM speech_region") == []


async def test_the_pipeline_page_counts_speech_waiting_for_another_microphone(client, pool, settings):
    dev = await paired_device(pool, settings)
    item_id = await insert_item(pool, dev["device_id"], kind="audio", started_at=NINE)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO speech_region (id, item_id, device_id, start_ts, end_ts, offset_s, length_s, snr_db)"
            " VALUES ('r1', %s, %s, %s, %s, 1, 2, 20)",
            (item_id, dev["device_id"], NINE + dt.timedelta(seconds=1), NINE + dt.timedelta(seconds=3)))
    assert "Tiếng nói chờ mic khác: 1 đoạn" in (await client.get("/pipeline")).text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_multimic_paths.py -q`
Expected: FAIL — reprocess leaves `speech_detected_at` and the regions; the page has no such line.

- [ ] **Step 3: Implement**

`reprocess.py`, in the `"transcripts"` branch, before the `UPDATE item`:

```python
            await conn.execute("DELETE FROM speech_region WHERE item_id = ANY(%s)", (redo,))
```

and the `UPDATE item` also sets `speech_detected_at = NULL`. `forget.py` needs no change if it deletes the items (the
regions cascade); if the forget test shows regions left (it deletes moments or transcripts without their items), add
`DELETE FROM speech_region WHERE start_ts >= %s AND start_ts < %s` beside its transcript deletion.

`pipeline/status.py`: in the function that builds the page's data, add
`speech_waiting = (await (await conn.execute("SELECT count(*) AS n FROM speech_region WHERE state = 'waiting'")).fetchone())["n"]`
to the returned data as `speech_waiting`; `templates/pipeline.html`, next to the pending-items line:
`<li>Tiếng nói chờ mic khác: {{ speech_waiting }} đoạn</li>` (match the list markup there).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: PASS, coverage ≥ 80 %.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain brain/tests
git commit -m "feat(brain): reprocess and forget handle speech regions; Xử lý shows speech waiting for another microphone (P3c-3)"
```

---

### Task 7: Places in the chapter signature; a rename refreshes the summaries

**Files:**
- Modify: `brain/src/loopi_brain/pipeline/segment.py` (`Point`, `signature`), `pipeline/chapters.py` (`_points`,
  `RENAME_REFRESH_DAYS`, `refresh_places`), `web/routes.py` (`save_location` calls `refresh_places`)
- Test: `brain/tests/test_rename_refresh.py` (create); `tests/test_segment.py` keeps
  `test_signature_names_the_members` green

**Interfaces:**
- Consumes: `places.set_location`, `state.lock_day`, `chapters.defer_build(pool, day, zone)`.
- Produces: `Point(..., device_id: str | None = None, place: str | None = None)`; `signature(points)` place-aware per
  R9; `chapters.RENAME_REFRESH_DAYS = 14`; `async chapters.refresh_places(pool, device_id, zone, now=None) ->
  list[date]`.

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_rename_refresh.py`:

```python
import datetime as dt

from helpers import FakeOllama, fetch_all, fetch_one, insert_moment, make_ctx, paired_device
from loopi_brain import places
from loopi_brain.pipeline import chapters, segment
from loopi_brain.pipeline.segment import Point

NOW = dt.datetime(2026, 10, 21, 3, 0, tzinfo=dt.UTC)
NINE = dt.datetime(2026, 10, 20, 0, 0, tzinfo=dt.UTC)  # 09:00 in Seoul on 10-20
HX = {"HX-Request": "true"}


def test_a_chapter_from_one_device_keeps_its_old_signature():
    one = [Point("a", "visual", NINE, NINE, device_id="pi", place="Bếp"),
           Point("b", "speech", NINE, NINE, device_id="pi", place="Bếp")]
    assert segment.signature(one) == segment.signature([Point("a", "visual", NINE, NINE), Point("b", "speech", NINE, NINE)])


def test_a_chapter_from_two_devices_signs_its_places():
    two = [Point("a", "visual", NINE, NINE, device_id="pi", place="Bếp"),
           Point("b", "speech", NINE, NINE, device_id="cam", place="Phòng khách")]
    renamed = [two[0], Point("b", "speech", NINE, NINE, device_id="cam", place="Phòng ngủ")]
    assert segment.signature(two) != segment.signature(renamed)


async def _day(pool, settings):
    pi, cam = await paired_device(pool, settings), await paired_device(pool, settings)
    await places.set_location(pool, pi["device_id"], "Bếp")
    await places.set_location(pool, cam["device_id"], "Phòng khách")
    for k in range(4):
        await insert_moment(pool, pi["device_id"], kind="speech", ts=NINE + dt.timedelta(minutes=k), text=f"nấu {k}")
        await insert_moment(pool, cam["device_id"], kind="visual", ts=NINE + dt.timedelta(minutes=k, seconds=30),
                            text=f"phòng {k}")
    ctx = make_ctx(pool, settings, ollama=FakeOllama())
    await chapters.build_chapters(ctx, NINE.date(), now=NOW)
    return pi, cam


async def test_renaming_a_place_re_signs_that_days_chapters_and_stales_its_recap(pool, settings, jobs_open):
    pi, cam = await _day(pool, settings)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO day_recap (day, markdown, model_version, generated_at) VALUES (%s, 'x', 't', now())",
                           (NINE.date(),))
    before = await fetch_all(pool, "SELECT id, signature, summary_signature FROM chapter")
    assert all(r["signature"] == r["summary_signature"] for r in before)
    await places.set_location(pool, cam["device_id"], "Phòng ngủ")
    assert await chapters.refresh_places(pool, cam["device_id"], settings.zone, now=NOW) == [NINE.date()]
    after = await fetch_all(pool, "SELECT signature, summary_signature FROM chapter")
    assert any(r["signature"] != r["summary_signature"] for r in after)  # out of date: summarised again
    assert (await fetch_one(pool, "SELECT stale FROM day_recap"))["stale"] is True
    job = await fetch_one(pool, "SELECT task_name FROM procrastinate_jobs WHERE task_name = 'build_chapters'")
    assert job is not None


async def test_days_older_than_two_weeks_or_with_one_device_are_left_alone(pool, settings, jobs_open):
    pi, cam = await _day(pool, settings)
    later = NOW + dt.timedelta(days=chapters.RENAME_REFRESH_DAYS + 1)
    assert await chapters.refresh_places(pool, cam["device_id"], settings.zone, now=later) == []
    alone = await paired_device(pool, settings)  # the only device on its day
    await insert_moment(pool, alone["device_id"], kind="visual", ts=NINE - dt.timedelta(days=2), text="một mình")
    assert await chapters.refresh_places(pool, alone["device_id"], settings.zone, now=NOW) == []


async def test_the_rename_form_refreshes_the_summaries(client, pool, settings, jobs_open, monkeypatch):
    monkeypatch.setattr(chapters, "RENAME_REFRESH_DAYS", 100_000)  # the route uses the real clock
    pi, cam = await _day(pool, settings)
    r = await client.post(f"/devices/{cam['device_id']}/location", data={"location": "Phòng ngủ"}, headers=HX)
    assert r.status_code == 200
    rows = await fetch_all(pool, "SELECT signature, summary_signature FROM chapter")
    assert any(r["signature"] != r["summary_signature"] for r in rows)
```

(`refresh_places` reads `RENAME_REFRESH_DAYS` from the module at call time, so the monkeypatch reaches it.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_rename_refresh.py -q`
Expected: FAIL — `Point` has no `device_id`; `refresh_places` does not exist.

- [ ] **Step 3: Implement**

`segment.py`:

```python
@dataclass(frozen=True)
class Point:
    """One moment as the segmentation sees it."""

    id: str
    kind: str  # visual | speech | marker
    start: dt.datetime
    end: dt.datetime
    vector: np.ndarray | None = None  # unit-length embedding; None until embedded (markers never drift)
    device_id: str | None = None
    place: str | None = None  # the device's place name (P3c-3: part of the signature when places are named)


def signature(points: list[Point]) -> str:
    """Identifies a chapter's members: equal signatures mean the same moments — and, when they come from two or more
    devices (the chapter's text then names each place), the same place names (plan R9)."""
    named = len({p.device_id for p in points if p.device_id}) >= 2
    lines = (f"{p.id}\t{p.place or ''}" if named else p.id for p in points)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:32]
```

`chapters.py` `_points`:

```python
    cur = await conn.execute(
        "SELECT m.id, m.kind, m.ts_start, m.ts_end, m.embedding, m.device_id, d.location FROM moment m"
        " LEFT JOIN device d ON d.id = m.device_id"
        " WHERE m.ts_start >= %s AND m.ts_start < %s AND (%s::timestamptz IS NULL OR m.ts_start > %s)"
        " ORDER BY m.ts_start, m.id",
        (start, end, after, after),
    )
    return [
        Point(r["id"], r["kind"], r["ts_start"], max(r["ts_end"], r["ts_start"]),
              None if r["embedding"] is None else unit(r["embedding"].to_numpy()), r["device_id"], r["location"])
        for r in await cur.fetchall()
    ]
```

`chapters.py`, after `rebuild_chapters`:

```python
RENAME_REFRESH_DAYS = 14  # plan R10


async def refresh_places(pool: AsyncConnectionPool, device_id: str, zone, now: dt.datetime | None = None) -> list[dt.date]:
    """After a place was renamed: re-sign the chapters of the last RENAME_REFRESH_DAYS days on which the device and
    another one have moments (only chapters that name places change signature, R9), so their summaries are written
    again; mark those days' recaps stale; ask for a chapter build. Returns the days that changed."""
    now = now or dt.datetime.now(dt.UTC)
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT day FROM (SELECT (ts_start AT TIME ZONE %s)::date AS day, device_id FROM moment"
            " WHERE ts_start >= %s) t GROUP BY day HAVING bool_or(device_id = %s) AND count(DISTINCT device_id) >= 2"
            " ORDER BY day",
            (str(zone), now - dt.timedelta(days=RENAME_REFRESH_DAYS), device_id),
        )
        days = [r["day"] for r in await cur.fetchall()]
    changed_days = []
    for day in days:
        changed = False
        async with pool.connection() as conn, conn.transaction():
            await lock_day(conn, day)
            cur = await conn.execute(
                "SELECT id, ts_start, last_moment_at, signature FROM chapter WHERE day = %s ORDER BY ts_start", (day,)
            )
            for c in await cur.fetchall():
                points = await _points(conn, c["ts_start"], c["last_moment_at"] + dt.timedelta(microseconds=1), None)
                sig = signature(points)
                if sig != c["signature"]:
                    await conn.execute("UPDATE chapter SET signature = %s, updated_at = now() WHERE id = %s",
                                       (sig, c["id"]))
                    changed = True
            cur = await conn.execute("UPDATE day_recap SET stale = true WHERE day = %s", (day,))
            changed = changed or cur.rowcount > 0
        if changed:
            await defer_build(pool, day, zone)
            changed_days.append(day)
    return changed_days
```

(Pass the zone to SQL the way `plan_chapters` does if `str(zone)` is not what it uses.)

`web/routes.py` `save_location`, after a successful `places.set_location(...)`:

```python
        try:
            await chapter_rules.refresh_places(pool, device_id, request.app.state.settings.zone)
        except Exception:  # the new name is saved; summaries refresh by hand (Xử lý lại) if this fails
            log.exception("could not refresh the summaries after renaming %s", device_id)
```

(import `chapters` as the module already imports it elsewhere in `web/` — e.g. `from ..pipeline import chapters as
chapter_rules`; use the zone attribute the route module already uses.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_rename_refresh.py tests/test_segment.py tests/test_chapters.py tests/test_places.py tests/test_prompt_places.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain brain/tests
git commit -m "feat(brain): chapters sign their place names; a rename refreshes the recent multi-camera summaries (P3c-3, D34)"
```

---

### Task 8: Vlog material — one camera per 10-minute block

**Files:**
- Modify: `brain/src/loopi_brain/vlog/material.py` (`BLOCK`, `Segment.device_id`, `Frame.device_id`,
  `Material.places`, `choose_cameras`, the queries), `vlog/script.py` (`to_json` stores `device_id` per highlight;
  `script_messages` names the place when the material has two or more devices)
- Test: `brain/tests/test_vlog_cameras.py` (create); keep `tests/test_vlog_material.py`, `test_vlog_script.py` green

**Interfaces:**
- Consumes: `days.day_bounds_utc`.
- Produces: `material.BLOCK = timedelta(minutes=10)`; `Segment(item_id, path, start, device_id=None)`;
  `Frame(keyframe_id, path, at, device_id=None)`; `Material(..., places: dict[str, str] = {})` (device id → location of
  every device in the material); `choose_cameras(segments, counts: dict[tuple[int, str], int], start) ->
  list[Segment]`; script highlight JSON key `"device_id"`.

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_vlog_cameras.py`:

```python
import datetime as dt
from zoneinfo import ZoneInfo

from helpers import insert_item, insert_moment, paired_device
from loopi_brain import places
from loopi_brain.vlog import material
from loopi_brain.vlog.material import Segment

DAY = dt.date(2026, 10, 14)
ZONE = ZoneInfo("Asia/Seoul")
START = dt.datetime(2026, 10, 13, 15, 0, tzinfo=dt.UTC)  # the local day's start
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)


def seg(item, device, minutes):
    return Segment(item, f"view/{item}.mp4", START + dt.timedelta(minutes=minutes), device)


def test_each_block_keeps_the_camera_with_the_most_moments():
    segments = [seg("p1", "pi", 541), seg("c1", "cam", 541), seg("p2", "pi", 552), seg("c2", "cam", 553)]
    counts = {(54, "pi"): 2, (54, "cam"): 5, (55, "pi"): 7, (55, "cam"): 1}
    assert [s.item_id for s in material.choose_cameras(segments, counts, START)] == ["c1", "p2"]


def test_ties_go_to_more_segments_then_the_smaller_id():
    segments = [seg("a1", "a", 1), seg("b1", "b", 2), seg("b2", "b", 3)]
    assert [s.item_id for s in material.choose_cameras(segments, {}, START)] == ["b1", "b2"]
    assert [s.item_id for s in material.choose_cameras([seg("x", "b", 1), seg("y", "a", 2)], {}, START)] == ["y"]


def test_one_camera_keeps_every_active_segment():
    segments = [seg("p1", "pi", 1), seg("p2", "pi", 12), seg("p3", "pi", 25)]
    assert material.choose_cameras(segments, {}, START) == segments


async def _video(pool, device_id, start):
    item_id = await insert_item(pool, device_id, kind="video", started_at=start)
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET analysis_state = 'done', view_path = %s WHERE id = %s",
                           (f"view/{item_id}.mp4", item_id))
    return item_id


async def test_the_material_takes_one_camera_per_block_and_names_the_places(pool, settings):
    pi, cam = await paired_device(pool, settings), await paired_device(pool, settings)
    await places.set_location(pool, pi["device_id"], "Bếp")
    await places.set_location(pool, cam["device_id"], "Phòng khách")
    pi_seg = await _video(pool, pi["device_id"], NINE)
    cam_seg = await _video(pool, cam["device_id"], NINE)
    for k in range(3):
        await insert_moment(pool, cam["device_id"], kind="visual", ts=NINE + dt.timedelta(seconds=10 * k), text="x")
    await insert_moment(pool, pi["device_id"], kind="visual", ts=NINE, text="y")
    got = await material.vlog_material(pool, DAY, ZONE)
    assert [s.item_id for s in got.segments] == [cam_seg] and got.segments[0].device_id == cam["device_id"]
    assert got.places == {cam["device_id"]: "Phòng khách"}  # only the devices the material shows
    assert pi_seg not in [s.item_id for s in got.segments]
```

Add to `tests/test_vlog_script.py`: `to_json` of a script whose candidate frame has `device_id="cam"` stores
`"device_id": "cam"` for that highlight; `script_messages` of a material whose `places` holds two devices puts the
frame's place after the time (`"H1 · 09:00 · Bếp · …"`), and the single-device message stays exactly as today.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_vlog_cameras.py -q`
Expected: FAIL — `Segment` takes no device; `choose_cameras` does not exist.

- [ ] **Step 3: Implement**

`material.py`:

```python
from collections import Counter
from dataclasses import dataclass, field

BLOCK = dt.timedelta(minutes=10)  # D35: each active block shows one camera (plan R11)


@dataclass(frozen=True)
class Segment:
    item_id: str
    path: str  # the MP4 view, relative to the media root
    start: dt.datetime
    device_id: str | None = None


@dataclass(frozen=True)
class Frame:
    keyframe_id: str
    path: str  # the JPEG, relative to the media root
    at: dt.datetime
    device_id: str | None = None
```

`Material` gains `places: dict[str, str] = field(default_factory=dict)` after `candidates`.

Queries: `_SEGMENTS` selects `i.device_id` too; `_FRAMES = "SELECT id, path, ts, caption, device_id FROM keyframe
WHERE …"`; new:

```python
_BLOCK_MOMENTS = """
SELECT device_id, floor(extract(epoch FROM ts_start - %(start)s) / %(block)s)::int AS block, count(*) AS n
  FROM moment WHERE ts_start >= %(start)s AND ts_start < %(end)s GROUP BY 1, 2"""
_PLACES = "SELECT id, location FROM device WHERE id = ANY(%s)"


def choose_cameras(segments: list[Segment], counts: dict[tuple[int, str], int], start: dt.datetime) -> list[Segment]:
    """D35 (R11): in each BLOCK from ``start``, the segments of one camera — the one with the most moments in the
    block (``counts[(block, device)]``) among those with a segment there; ties: more segments, then the smaller id."""
    by_block: dict[int, list[Segment]] = {}
    for s in segments:
        by_block.setdefault((s.start - start) // BLOCK, []).append(s)
    kept: list[Segment] = []
    for block, segs in sorted(by_block.items()):
        per_device = Counter(s.device_id for s in segs)
        winner = min(per_device, key=lambda d: (-counts.get((block, d), 0), -per_device[d], d or ""))
        kept += [s for s in segs if s.device_id == winner]
    return sorted(kept, key=lambda s: (s.start, s.item_id))
```

In `vlog_material`: `params` gains `"block": BLOCK.total_seconds()`; segments are built with `r["device_id"]`, then
`counts = {(r["block"], r["device_id"]): r["n"] for r in <_BLOCK_MOMENTS rows>}` and
`segments = choose_cameras(segments, counts, start)`; frames with `r["device_id"]`; after the queries,
`ids = sorted({s.device_id for s in segments} | {f.device_id for f in frames} - {None})` and `places = {r["id"]:
r["location"] for r in <_PLACES rows with (ids,)>}`; return `Material(day, segments, spread(frames, SLIDES_MAX),
merge(found), places)`.

`script.py` `to_json`: each highlight dict gains `"device_id": h.candidate.frame.device_id`. In `script_messages`,
when `len(material.places) >= 2` and the candidate frame's device has a place, insert `f"{place} · "` after the time
in the candidate's line (read the function: the line format is `"H1 · 09:00 · <kind> · <text>"`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_vlog_cameras.py tests/test_vlog_material.py tests/test_vlog_script.py tests/test_vlog_jobs.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/vlog brain/tests
git commit -m "feat(brain): the vlog takes one camera per active 10-minute block; highlights keep their camera (P3c-3, D35)"
```

---

### Task 9: Vlog render — one run per camera, place labels

**Files:**
- Modify: `brain/src/loopi_brain/vlog/render.py` (`LABEL_FONT`, `label_filter`, `Segment.label`, `Still.label`,
  `timelapse_argv(..., label=None)`, `kenburns_argv(..., label=None)`, step 1 of `render` per run),
  `pipeline/vlogs.py` (labels when two or more devices)
- Test: `brain/tests/test_vlog_runs.py` (create); keep `tests/test_vlog_render.py` and `tests/test_vlog_jobs.py` green
  unchanged (they are the single-camera proof)

**Interfaces:**
- Consumes: `material.Segment.device_id`, `Material.places`, the script's highlight `device_id` (Task 8);
  `places.several(ids) -> bool`.
- Produces: `render.LABEL_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")`, `render.LABEL_STYLE`,
  `render.label_filter(textfile: Path) -> str`, `render.Segment(path, start, label=None)`,
  `render.Still(jpeg, at, caption, clip_ogg=None, clip_offset=0.0, label=None)`,
  `render.segment_shares(n_segments: int, n_out: int) -> list[int]`.

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_vlog_runs.py`:

```python
import datetime as dt
import shutil
import subprocess

import pytest

from helpers import make_mp4, write_jpeg
from loopi_brain.vlog import render

T = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)
needs_drawtext = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or not render.LABEL_FONT.exists()
    or b" drawtext " not in subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True).stdout,
    reason="needs ffmpeg with drawtext and DejaVu Sans",
)


def test_segments_share_the_timelapse_equally():
    assert render.segment_shares(4, 10) == [3, 3, 2, 2]
    assert sum(render.segment_shares(7, 240)) == 240


def test_label_paths_are_refused_when_they_would_need_quoting(tmp_path):
    with pytest.raises(render.InvalidMedia):
        render.label_filter(tmp_path / "a:b.txt")
    assert "drawtext=fontfile=" in render.label_filter(tmp_path / "label_0.txt")
    assert "expansion=none" in render.label_filter(tmp_path / "label_0.txt")


async def test_one_camera_keeps_one_run_and_no_label(tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    make_mp4(a, seconds=4, fps=4, size="320x180")
    make_mp4(b, seconds=4, fps=4, size="320x180")
    steps = []

    async def runner(step, argv, feed):
        steps.append((step, argv))
        await render.run_step(step, argv, feed)

    await render.render(segments=[render.Segment(a, T), render.Segment(b, T + dt.timedelta(seconds=4))],
                        stills=[], keyframes=[], target_seconds=1.0, workdir=tmp_path / "w",
                        out=tmp_path / "out.mp4", title="Hôm nay", runner=runner)
    timelapses = [argv for step, argv in steps if step.startswith("timelapse")]
    assert len(timelapses) == 1 and not any("drawtext" in x for x in timelapses[0])
    assert str(tmp_path / "w" / "segments.ffconcat") in timelapses[0]


@needs_drawtext
async def test_two_cameras_render_their_runs_in_one_vlog(tmp_path):
    pi, cam = tmp_path / "pi.mp4", tmp_path / "cam.mp4"
    make_mp4(pi, seconds=4, fps=24, size="640x360", gop=24)
    make_mp4(cam, seconds=4, fps=6, size="320x180", gop=6)  # a slower, smaller camera: 24 frames
    jpeg = tmp_path / "s.jpg"
    write_jpeg(jpeg)
    still = render.Still(jpeg, T + dt.timedelta(seconds=5), "Bếp", label="Bếp")
    result = await render.render(
        segments=[render.Segment(pi, T, "Bếp"), render.Segment(cam, T + dt.timedelta(seconds=4), "Phòng khách")],
        stills=[still], keyframes=[], target_seconds=2.0, workdir=tmp_path / "w", out=tmp_path / "out.mp4",
        still_seconds=1.0,
    )
    assert result.frames == 2 * render.FPS + render.FPS  # 24 + 24 frames of the two runs, then the 1 s still
    assert render.video_track(result.out).samples == result.frames
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_vlog_runs.py -q`
Expected: FAIL — `segment_shares`, `label_filter`, `Segment.label` do not exist.

- [ ] **Step 3: Implement**

`render.py` additions:

```python
LABEL_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")  # in the brain image (Debian fonts-dejavu-core)
LABEL_STYLE = "fontsize=30:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=10:x=28:y=h-th-28:expansion=none"
_PLAIN_PATH = re.compile(r"[\w./-]+")


def label_filter(textfile: Path) -> str:
    """drawtext reading the place from ``textfile`` (no text escaping; expansion off so '%' is shown as it is).
    Paths are used unquoted, so one that would need filtergraph quoting is refused."""
    for p in (LABEL_FONT.as_posix(), textfile.as_posix()):
        if not _PLAIN_PATH.fullmatch(p):
            raise InvalidMedia(f"label path {p!r} would need quoting")
    return f"drawtext=fontfile={LABEL_FONT.as_posix()}:textfile={textfile.as_posix()}:{LABEL_STYLE}"


def segment_shares(n_segments: int, n_out: int) -> list[int]:
    """Output frames per segment: every segment the same share of the timelapse, the first ones one more (R12)."""
    base, extra = divmod(n_out, n_segments)
    return [base + (1 if k < extra else 0) for k in range(n_segments)]
```

`timelapse_argv(..., label: Path | None = None)`: `vf = f"{uniform_select(n_out, n_in)},setpts=N/({FPS}*TB),fps={FPS},
{NORMALISE}" + (f",{label_filter(label)}" if label else "")`. `kenburns_argv(..., label: Path | None = None)`: append
`f",{label_filter(label)}"` to its `vf` when given. `Segment` gains `label: str | None = None`; `Still` gains
`label: str | None = None` (last field).

`render()` step 1, the `if good:` branch, becomes per run (the `else` slideshow branch is unchanged):

```python
    if good:
        runs: list[list[tuple[Segment, Track]]] = []
        for s, t in good:  # consecutive segments of one camera (one label) form a run (R12)
            if runs and runs[-1][-1][0].label == s.label:
                runs[-1].append((s, t))
            else:
                runs.append([(s, t)])
        shares = segment_shares(len(good), n_out)
        plans = []  # per run: (units, n_in, n_out, keyframes_only)
        k = 0
        for run in runs:
            want = sum(shares[k:k + len(run)])
            k += len(run)
            keys = sum(t.sync_samples for _, t in run)
            frames_in = sum(t.samples for _, t in run)
            kf_only = allow_keyframes_only and keys >= KEYFRAMES_PER_OUTPUT_MIN * want
            units = [(s, t.sync_samples if kf_only else t.samples, t.seconds) for s, t in run]
            r_in = keys if kf_only else frames_in
            plans.append((units, r_in, min(want, r_in), kf_only))

        def units_before(units, at: datetime) -> int:
            m = 0
            for s, u, secs in units:
                into = (at - s.start).total_seconds()
                if into >= secs:
                    m += u
                elif into > 0:
                    m += math.ceil(u * into / secs)
            return m

        def run_of(at: datetime) -> int:
            for r, run in enumerate(runs):
                last, track = run[-1]
                if at < last.start + timedelta(seconds=track.seconds):
                    return r
            return len(runs) - 1

        placed = []  # per still: (run, boundary in that run)
        for st, _ in ok_stills:
            r = run_of(st.at)
            units, r_in, r_out, _ = plans[r]
            placed.append((r, chunk_boundaries([units_before(units, st.at)], r_in, r_out)[0] if r_out else 0))
        run_chunks: list[tuple[list[Path], list[int]]] = []
        for r, (run, (units, r_in, r_out, kf_only)) in enumerate(zip(runs, plans)):
            if r_out == 0:
                run_chunks.append(([], [0]))
                continue
            prefix = "" if r == 0 else f"run{r}_"
            lst = workdir / ("segments.ffconcat" if r == 0 else f"segments_{r}.ffconcat")
            pat = workdir / f"{prefix}chunk_%03d.mp4"
            await asyncio.to_thread(_write_concat, lst, [s.path for s, _ in run])
            label = None
            if run[0][0].label:
                label = workdir / f"label_{r}.txt"
                label.write_text(run[0][0].label, encoding="utf-8")
            cut = sorted({b for rr, b in placed if rr == r and 0 < b < r_out})
            await runner("timelapse" if r == 0 else f"timelapse[{r}]",
                         timelapse_argv(lst, pat, n_out=r_out, n_in=r_in, keyframes_only=kf_only, boundaries=cut,
                                        label=label), None)
            edges = [0, *cut, r_out]
            run_chunks.append(([Path(str(pat).replace("%03d", f"{i:03d}")) for i in range(len(edges) - 1)], edges))
        n_out = sum(p[2] for p in plans)
        mode = "timelapse-keyframes" if all(p[3] for p in plans) else "timelapse-full"
```

The slideshow branch keeps building `bounds`, `cut` and `n_out` as today and ends with

```python
        chunk_edges = [0, *cut, n_out] if n_out else [0]
        run_chunks = [([Path(str(chunk_pat).replace("%03d", f"{i:03d}")) for i in range(len(chunk_edges) - 1)],
                       chunk_edges)]
        placed = [(0, b) for b in bounds]
```

Today's common chunk check and `order` construction become:

```python
    for chunks_r, edges in run_chunks:
        for c, (a, b) in zip(chunks_r, zip(edges, edges[1:])):
            try:
                got = video_track(c).samples
            except InvalidMedia:
                got = 0
            if got != b - a:  # e.g. a segment vanished/corrupted after inspection: concat stops there, rc 0
                raise RenderError(mode, 0, f"{c.name}: {got} frames, expected {b - a}")
    # ... step 2 (Ken Burns) as today, with label= for a still that has one ...
    order: list[tuple[Path, int, Still | None]] = []
    for r, (chunks_r, edges) in enumerate(run_chunks):
        order += [(still_parts[j], kb_frames, ok_stills[j][0]) for j, (rr, b) in enumerate(placed) if rr == r and b == 0]
        for ci, c in enumerate(chunks_r):
            order.append((c, edges[ci + 1] - edges[ci], None))
            order += [(still_parts[j], kb_frames, ok_stills[j][0])
                      for j, (rr, b) in enumerate(placed) if rr == r and b == edges[ci + 1] and b > 0]
    if not any(chunks_r for chunks_r, _ in run_chunks):
        order = [(still_parts[j], kb_frames, ok_stills[j][0]) for j in range(len(ok_stills))]
```

(`# ... step 2 ...` is today's Ken Burns loop, unchanged except `kenburns_argv(p, seconds=still_seconds,
label=<label_still_{i}.txt when st.label>)`.) Everything after `order` is unchanged. Step 2
(Ken Burns) passes `label=` a `label_still_{i}.txt` file when `st.label` is set. With one run and no labels the argv,
file names and frame counts are exactly today's (`tests/test_vlog_render.py` proves it).

`pipeline/vlogs.py` (render step): `named = places.several([s.device_id for s in material.segments] +
[h.get("device_id") for h in script.get("highlights", [])])`; segments get
`label=material.places.get(s.device_id) if named else None`; `_stills(pool, root, script)` gains a `labels: dict[str,
str]` argument (`material.places if named else {}`) and passes `label=labels.get(h.get("device_id"))` to each `Still`.
`plan_seconds(len(segments), len(stills))` is unchanged (it now counts one camera per block).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_vlog_runs.py tests/test_vlog_render.py tests/test_vlog_jobs.py -q`
Expected: PASS. Then the full suite: `cd brain && uv run pytest --cov=loopi_brain -q` (≥ 80 %).

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain brain/tests
git commit -m "feat(brain): the vlog renders one run per camera and labels each scene with its place (P3c-3, D35)"
```

---

### Task 10: Docs

**Files:**
- Modify: `docs/superpowers/specs/2026-10-19-p3c-multicam-design.md` (§4.3, §4.4, the "Trạng thái" line),
  `docs/superpowers/specs/2026-10-14-loopi-design.md` (D33 and D35 rows: one clause each), `deploy/README.md`

- [ ] **Step 1: The P3c design (Vietnamese)**

- §4.3 gains, after its bullets: "Chi tiết (plan P3c-3, R1–R8): hai đoạn tiếng nói ở hai mic thuộc một nhóm khi chúng
  chồng nhau sau khi nới mỗi bên 2 giây; mic thắng là mic có đoạn SNR cao nhất, mọi đoạn của nó được chép; đoạn của
  mic khác chỉ bỏ khi mic đã chọn nghe ít nhất một nửa đoạn đó. SNR = mức tiếng nói (phân vị 90 của RMS khung 20 ms)
  trên nền nhiễu của chính đoạn audio thô (không thấp hơn −70 dBFS). 3 phút tính từ lúc audio đầu tiên của phút đó tới
  brain; audio đến muộn hơn không đổi quyết định nhưng câu chỉ mic đó nghe vẫn được chép."
- §4.4: after the vlog bullet: "Nhãn vị trí vẽ bằng ffmpeg `drawtext` (DejaVu Sans), chỉ khi vlog có ≥ 2 camera; đổi
  tên vị trí làm mới tóm tắt chương và recap của 14 ngày gần nhất có ≥ 2 camera (R9, R10)."
- "Trạng thái": append "; P3c-3 có plan ngày 2026-10-21 (`plans/2026-10-21-p3c3-multicam-understanding.md`)".

- [ ] **Step 2: The binding spec**

D33 row: append "(chi tiết: P3c design §4.3)"; D35 row: append "; nhãn chỉ hiện khi vlog có ≥ 2 camera".

- [ ] **Step 3: The operator guide**

`deploy/README.md`, the network cameras section, one bullet: "With two or more microphones, a sentence is transcribed
once, from the clearest one; the brain waits up to 3 minutes for every recording microphone's audio of a minute. The
Xử lý page shows how many speech regions wait for another microphone."

- [ ] **Step 4: Commit**

```bash
git add docs deploy/README.md
git commit -m "docs: P3c-3 rulings in the specs and the operator guide (P3c-3)"
```

---

### Task 11: Deploy and accept (controller, with the owner)

- [ ] **Step 1: Deploy (after the owner's yes)**

Back up the database (`docker compose -p deploy exec -T db pg_dump -U loopi -Fc loopi >
~/loopi-backups/p3c3-<date>.dump`), fast-forward `feat/phase2` in `/home/soul/loopi-p2` to the branch,
`docker compose -p deploy up -d --build` from `/home/soul/loopi-p2/deploy`, `COMPOSE_PROJECT_NAME=deploy ./smoke.sh`.
Check: the Pi's next minutes get transcripts as before (`moment` rows of kind `speech` keep coming), the Xử lý page
shows "Tiếng nói chờ ghép (mic khác hoặc phút sau): 0 đoạn" most of the time, no job of `group_speech` fails.

- [ ] **Step 2: Acceptance (P3c design §8)**

- §8.2 (needs a network camera with sound next to the Pi): one sentence spoken between the two microphones gives one
  transcript; its region on the other microphone is `covered`. Record the two SNRs.
- §8.5: on a day with two active cameras, the vlog uses both (its runs and labels), and its length follows D25.
- A rename of a place on Thiết bị: the next build re-summarises that day's multi-camera chapters.
- Record every number in the ledger.
