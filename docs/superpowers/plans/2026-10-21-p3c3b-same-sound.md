# P3c-3b Same-Sound Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two different conversations at the same time in two rooms are both transcribed: a speech region is covered
by another microphone's chosen region only when the two are the same sound (their loudness envelopes match), not
merely when they overlap in time.

**Architecture:** Detection stores each region's loudness envelope (dB of the raw audio every 100 ms). Grouping loads
it into `speech.Span`; `speech.decide` counts a chosen span towards covering another only when `same_sound` finds a
correlation of at least 0.5 at some lag within ±2 s. Regions stored before this version have no envelope and keep
today's time-only rule.

**Tech Stack:** Python 3.13 (uv), numpy, Postgres 17 (`real[]`), Procrastinate 3.10.

**Spec:** `docs/superpowers/specs/2026-10-19-p3c-multicam-design.md` §4.3 and D33 (binding spec row) — amended by the
owner on 2026-10-21 (question I3 after the P3c-3 final review: "Thêm kiểm tra" — compare the two regions' loudness
shape; same → one transcript, different → both). Builds on P3c-3 (`plans/2026-10-21-p3c3-multicam-understanding.md`).

## Global Constraints

- Brain package coverage ≥ 80 %; TDD; conventional commits ending with the session's Co-Authored-By trailer.
- No change to the device↔brain contract, the agent, or `docs/protocol/v1.openapi.yaml`.
- User-facing text Vietnamese; code, comments, commits English.
- Next migration is `014_*.sql`; pins: `tests/test_db.py` (the `applied` set and the two exact migration-name lists;
  014 sits before the Procrastinate schema step); no new table, so `APP_TABLES` and `EXPECTED_TABLES` stay.
- With one microphone nothing changes; with two, a region overlapping a chosen region of another microphone whose
  envelope matches is covered exactly as today.
- Tests use the `pki_settings` fixture for paired devices; vlog tests need `PATH=/usr/bin:$PATH` (not touched here).

## Rulings (the owner reviews them with this plan)

| # | Ruling | Why |
|---|---|---|
| S1 | The envelope is the 20 ms frame RMS of the raw audio averaged into 100 ms steps (`ENVELOPE_HOP_S = 0.1`), in dBFS, floored at `NOISE_FLOOR_MIN_DBFS` (−70); stored as `speech_region.envelope real[]` (≤ 200 values for a 20 s region) | loudness shape survives different gains, rooms and codecs; 100 ms keeps syllable rhythm without storing audio |
| S2 | `same_sound(a, b)`: the best Pearson correlation of the two envelopes over the lags that align them within ±`GROUP_SLACK` (2 s, in 100 ms steps), using only the frames both cover, at least `SAME_SOUND_MIN_FRAMES = 10` (1 s) of them; same when ≥ `SAME_SOUND_MIN = 0.5`. Fewer than 10 common frames, or a span without an envelope (stored before this version), counts as same (today's rule). Amended during execution: `SAME_SOUND_MIN` = 0.7 (at 0.5, 12.8 % of unrelated synthetic pairs matched); only lags whose common frames reach max(10, half of the shorter envelope) are compared. | two copies of one sentence correlate highly at the right lag; two unrelated conversations do not; 0.5 is tuned at acceptance §8.2 with real cameras |
| S3 | In `decide`, a chosen span of another microphone counts towards covering `s` (the union, R2) and can be its `covered_by` only when `same_sound(s, c)`; the winner rule (R2) is unchanged | one transcript per sentence, both transcripts for two different conversations |

## Review Focus

1. **The same sentence on two microphones** (one lags by up to 2 s, one quieter, one noisier) must still be covered —
   `test_the_same_sentence_on_two_microphones_matches` (Task 2).
2. **Two different conversations at once** must both be chosen — `test_two_different_conversations_are_both_kept`
   (Task 2).
3. **Regions without an envelope** (stored before this version, pending at the deploy) keep today's rule —
   `test_a_region_without_an_envelope_keeps_the_time_rule` (Task 2).
4. **A flat envelope** (constant level, zero variance) never divides by zero — `test_a_flat_envelope_is_not_a_match`
   (Task 2).
5. **Detection stores the envelope with the region** and grouping reads it back — `test_detection_stores_each_regions_envelope`
   (Task 1), `test_grouping_reads_the_envelope` (Task 2).

---

### Task 1: Migration 014 and the envelope at detection

**Files:**
- Create: `brain/src/loopi_brain/migrations/014_speech_envelope.sql`
- Modify: `brain/src/loopi_brain/pipeline/speech.py` (`ENVELOPE_HOP_S`, `envelope`), `pipeline/asr.py`
  (`detect_file` returns the envelope; `_detect` stores it), `brain/tests/test_db.py` (pins)
- Test: `brain/tests/test_speech_envelope.py` (create)

**Interfaces:**
- Produces: column `speech_region.envelope real[]` (nullable); `speech.ENVELOPE_HOP_S = 0.1`;
  `speech.envelope(raw: np.ndarray, start_s: float, end_s: float) -> list[float]`;
  `asr.detect_file(...) -> list[tuple[Region, float, list[float]]]` (region, SNR, envelope).

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_speech_envelope.py`:

```python
import numpy as np

from helpers import fetch_one
from loopi_brain.pipeline import asr, speech

RATE = speech.SAMPLE_RATE


def test_the_envelope_follows_the_loudness_every_100_ms():
    audio = np.zeros(RATE * 2, dtype=np.float32)
    audio[RATE // 2: RATE] = 0.1  # loud from 0.5 s to 1.0 s
    env = speech.envelope(audio, 0.0, 2.0)
    assert len(env) == 20
    assert env[0] == speech.NOISE_FLOOR_MIN_DBFS  # digital silence is floored, never -inf
    assert abs(env[7] - 20 * np.log10(0.1)) < 0.5  # 0.7 s: the loud part, -20 dBFS
    assert speech.envelope(audio, 1.95, 2.0) == []  # less than one 100 ms step


def test_detection_returns_each_regions_envelope(tmp_path, monkeypatch):
    from helpers import make_opus

    path = tmp_path / "a.opus"
    make_opus(path, seconds=3)
    monkeypatch.setattr(asr, "speech_regions", lambda _audio, _t: [asr.Region(0.5, 2.5)])
    ((region, snr, env),) = asr.detect_file(path)
    assert region == asr.Region(0.5, 2.5) and len(env) == 20
```

Add to the existing detection tests (`tests/test_detect_speech.py`) one test,
`test_detection_stores_each_regions_envelope`: after `asr.detect_speech(...)` with one monkeypatched region of 2.5 s,
`SELECT array_length(envelope, 1) AS n FROM speech_region` gives `{"n": 25}`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_speech_envelope.py tests/test_detect_speech.py -q`
Expected: FAIL — `speech.envelope` does not exist; `detect_file` returns pairs.

- [ ] **Step 3: Implement**

`brain/src/loopi_brain/migrations/014_speech_envelope.sql`:

```sql
-- P3c-3b (the owner's I3 ruling, 2026-10-21): each speech region's loudness envelope (dBFS every 100 ms of the raw
-- audio), so grouping covers a region only by the same sound on another microphone, not by time alone. NULL for
-- regions stored before this version (they keep the time-only rule).
ALTER TABLE speech_region ADD COLUMN envelope real[];
```

`speech.py`:

```python
ENVELOPE_HOP_S = 0.1  # S1: one loudness value per 100 ms


def envelope(raw: np.ndarray, start_s: float, end_s: float) -> list[float]:
    """S1: the region's loudness every ENVELOPE_HOP_S (mean of the 20 ms frame RMS), in dBFS, floored at
    NOISE_FLOOR_MIN_DBFS; only whole steps."""
    rms = frame_rms(raw[int(start_s * SAMPLE_RATE): int(end_s * SAMPLE_RATE)])
    per = round(ENVELOPE_HOP_S * SAMPLE_RATE / audio_level.FRAME)  # 5 frames of 20 ms
    steps = len(rms) // per
    floor = 10 ** (NOISE_FLOOR_MIN_DBFS / 20)
    means = rms[: steps * per].reshape(steps, per).mean(axis=1) if steps else np.zeros(0)
    return [float(20 * np.log10(max(m, floor))) for m in means]
```

`asr.py`: `detect_file` returns `[(r, speech.snr_db(raw, r.start, r.end, noise), speech.envelope(raw, r.start,
r.end)) for r in regions]` (update its annotation and docstring); `_detect` iterates `for region, snr, env in found:`
and inserts `envelope` (`… snr_db, envelope) VALUES (…, %s, %s)`, passing `env` — psycopg adapts a list of floats to
`real[]`). Every other caller of `detect_file` (grep it: tests) is updated to three values.

`tests/test_db.py`: add `"014_speech_envelope.sql"` to the `applied` set and to the two exact migration-name lists,
before the Procrastinate schema step (as 013 is).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_speech_envelope.py tests/test_detect_speech.py tests/test_db.py tests/test_asr.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain brain/tests
git commit -m "feat(brain): detection stores each speech region's loudness envelope (P3c-3b, D33)"
```

---

### Task 2: `same_sound` in the decision

**Files:**
- Modify: `brain/src/loopi_brain/pipeline/speech.py` (`Span.envelope`, `SAME_SOUND_MIN`,
  `SAME_SOUND_MIN_FRAMES`, `same_sound`, `decide`), `pipeline/grouping.py` (`_SPANS` selects `envelope`; `_span`
  passes it)
- Test: `brain/tests/test_same_sound.py` (create); `tests/test_speech_groups.py` and `tests/test_group_speech.py`
  stay green unchanged (their spans have no envelope → today's rule)

**Interfaces:**
- Consumes: `speech_region.envelope` (Task 1), `speech.ENVELOPE_HOP_S`, `speech.GROUP_SLACK`.
- Produces: `Span(..., state="waiting", envelope: tuple[float, ...] | None = None)`; `speech.SAME_SOUND_MIN = 0.7`,
  `speech.SAME_SOUND_MIN_FRAMES = 10`; `speech.same_sound(a: Span, b: Span, slack=GROUP_SLACK) -> bool`.

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_same_sound.py`:

```python
import datetime as dt

import numpy as np

from loopi_brain.pipeline import speech
from loopi_brain.pipeline.speech import Decision, Span

T = dt.datetime(2026, 10, 21, 1, 0, tzinfo=dt.UTC)
HOP = speech.ENVELOPE_HOP_S


def talk(seconds, seed):
    """A speech-like loudness envelope: syllables of random length and level over a -60 dB room."""
    rng = np.random.default_rng(seed)
    env, n = [], int(seconds / HOP)
    while len(env) < n:
        env += [-60.0] * int(rng.integers(1, 4)) + [float(rng.uniform(-35, -15))] * int(rng.integers(2, 6))
    return env[:n]


def span(id, device, start, env, snr, state="waiting"):
    return Span(id, device, T + dt.timedelta(seconds=start), T + dt.timedelta(seconds=start + len(env) * HOP), snr,
                state, tuple(env))


def test_the_same_sentence_on_two_microphones_matches():
    pi_env = talk(6, seed=1)
    noise = np.random.default_rng(9).normal(0, 1.5, len(pi_env))
    cam_env = [v - 12 + float(n) for v, n in zip(pi_env, noise)]  # quieter, noisier
    pi, cam = span("a", "pi", 0, pi_env, 25.0), span("b", "cam", 0.8, cam_env, 12.0)  # the camera lags 0.8 s
    assert speech.same_sound(cam, pi)
    assert speech.decide(speech.groups([pi, cam])[0]) == [Decision("a", "chosen"), Decision("b", "covered", "a")]


def test_two_different_conversations_are_both_kept():
    living, bedroom = span("a", "pi", 0, talk(6, seed=1), 25.0), span("b", "cam", 0.3, talk(6, seed=2), 15.0)
    assert not speech.same_sound(bedroom, living)
    assert speech.decide(speech.groups([living, bedroom])[0]) == [Decision("a", "chosen"), Decision("b", "chosen")]


def test_a_region_without_an_envelope_keeps_the_time_rule():
    pi = span("a", "pi", 0, talk(6, seed=1), 25.0)
    old = Span("b", "cam", T + dt.timedelta(seconds=0.5), T + dt.timedelta(seconds=6), 12.0)  # stored before 014
    assert speech.same_sound(old, pi)
    assert speech.decide(speech.groups([pi, old])[0])[1] == Decision("b", "covered", "a")


def test_a_flat_envelope_is_not_a_match():
    pi = span("a", "pi", 0, talk(6, seed=1), 25.0)
    hum = span("b", "cam", 0, [-40.0] * 60, 15.0)  # a steady fan: no shape to compare
    assert not speech.same_sound(hum, pi)


def test_too_little_common_time_counts_as_the_same():
    pi = span("a", "pi", 0, talk(6, seed=1), 25.0)
    edge = span("b", "cam", 7.9, talk(3, seed=3), 15.0)  # same group (within the slack), < 1 s common at every lag
    assert speech.same_sound(edge, pi)
```

Add to `tests/test_group_speech.py` one test, `test_grouping_reads_the_envelope`: two microphones whose regions
overlap in time with envelopes from two different `talk(...)` seeds (insert them with `envelope`) → after
`group_speech`, both regions are `chosen` (the `_detected` helper gains an optional `envelopes` argument).

The seeds are fixed, so each test is deterministic. If, once implemented, `test_two_different_conversations_are_both_kept`
finds two unrelated `talk` envelopes correlating at ≥ 0.5 at some lag, do not change the seeds: report the measured
correlations (NEEDS_CONTEXT) — that would mean S2's threshold or frame count needs a ruling.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_same_sound.py tests/test_group_speech.py -q`
Expected: FAIL — `Span` takes no envelope; `same_sound` does not exist.

- [ ] **Step 3: Implement**

`speech.py` — `Span` gains its last field `envelope: tuple[float, ...] | None = None`; then:

```python
SAME_SOUND_MIN = 0.5  # S2: the correlation two copies of one sentence reach; tuned at acceptance §8.2
SAME_SOUND_MIN_FRAMES = 10  # S2: at least 1 s in common to compare at all


def same_sound(a: Span, b: Span, slack: dt.timedelta = GROUP_SLACK) -> bool:
    """S2: do ``a`` and ``b`` (two microphones) hold the same sound? The best Pearson correlation of their loudness
    envelopes over the lags within ±slack, on the frames both cover. True when either has no envelope (stored before
    the check existed) or they share under SAME_SOUND_MIN_FRAMES at every lag — today's time-only rule."""
    if not a.envelope or not b.envelope:
        return True
    ea, eb = np.asarray(a.envelope), np.asarray(b.envelope)
    shift = round((b.start - a.start).total_seconds() / ENVELOPE_HOP_S)  # b's frame 0 sits at a's frame `shift`
    reach = round(slack.total_seconds() / ENVELOPE_HOP_S)
    best, compared = -1.0, False
    for lag in range(-reach, reach + 1):
        off = shift + lag  # a[i] pairs with b[i - off]
        lo, hi = max(0, off), min(len(ea), len(eb) + off)
        if hi - lo < SAME_SOUND_MIN_FRAMES:
            continue
        compared = True
        x, y = ea[lo:hi], eb[lo - off:hi - off]
        if x.std() == 0 or y.std() == 0:
            continue  # a flat stretch has no shape to match
        best = max(best, float(np.corrcoef(x, y)[0, 1]))
    return best >= SAME_SOUND_MIN if compared else True
```

In `decide`, the loop over the remaining waiting spans uses only chosen spans of other microphones that hold the same
sound:

```python
        others = [c for c in chosen if c.device_id != s.device_id and same_sound(s, c, slack)]
```

(the rest of the loop — the union, `best`, the share — is unchanged; update `decide`'s docstring: "… covered by the
chosen span of another microphone that holds the same sound (S3) and hears most of it …").

`grouping.py`: `_SPANS` selects `envelope` too; `_span(r)` passes `tuple(r["envelope"]) if r["envelope"] else None`
as the Span's last argument.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_same_sound.py tests/test_speech_groups.py tests/test_group_speech.py tests/test_multimic_paths.py -q`
Expected: PASS. Then the full suite: `cd brain && PATH=/usr/bin:$PATH uv run pytest --cov=loopi_brain -q` (≥ 80 %).

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain brain/tests
git commit -m "feat(brain): a region is covered only by the same sound on another microphone (P3c-3b, D33)"
```

---

### Task 3: Docs

**Files:**
- Modify: `docs/superpowers/specs/2026-10-19-p3c-multicam-design.md` (§4.3), `docs/superpowers/specs/2026-10-14-loopi-design.md`
  (D33 row), `deploy/README.md` (the network cameras bullet on microphones)

- [ ] **Step 1: The specs (Vietnamese)**

- §4.3, after the "Chi tiết (plan P3c-3 …)" sentence: "Chủ dự án chốt thêm ngày 2026-10-21 (I3): một đoạn chỉ bị bỏ
  khi đoạn đã chọn ở mic khác là **cùng một âm thanh** — đường âm lượng (dBFS mỗi 100 ms của audio thô) của hai đoạn
  tương quan từ 0,5 trở lên ở một độ lệch trong ±2 giây; hai cuộc nói chuyện khác nhau cùng lúc ở hai phòng đều được
  chép (plan P3c-3b)."
- The binding spec's D33 row: append "; chỉ coi là một nhóm khi cùng một âm thanh (đường âm lượng khớp), chủ dự án chốt
  2026-10-21".

- [ ] **Step 2: The operator guide**

`deploy/README.md`: the microphones bullet gains "A region heard on another microphone is skipped only when its
loudness shape matches the chosen one (the same sentence); two different conversations at once are both transcribed."

- [ ] **Step 3: Commit**

```bash
git add docs deploy/README.md
git commit -m "docs: the same-sound check in the specs and the operator guide (P3c-3b)"
```

---

### Task 4: Deploy (controller)

Back up the database, fast-forward `feat/phase2` in `/home/soul/loopi-p2` to the branch, `docker compose -p deploy up
-d --build`, smoke test, check that new speech regions carry an envelope
(`SELECT count(*) FROM speech_region WHERE envelope IS NOT NULL AND created_at > <deploy time>`) and that no
"Tìm tiếng nói" job failed. Acceptance with a camera with sound (§8.2): record the correlation of a sentence heard on
both microphones and of two different conversations; tune `SAME_SOUND_MIN` only with those numbers and the owner's yes.
