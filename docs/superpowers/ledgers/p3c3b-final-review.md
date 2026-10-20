# P3c-3b final whole-branch review (same-sound check)

Range a3b4ad9..d5143d9 (`feat/p3c3b`; 7 commits, ce38e3c..d5143d9). Read-only review. Pure tests run at d5143d9:
`test_same_sound.py`, `test_speech_groups.py`, `test_speech_envelope.py` gave 34 passed. Simulations are in the
scratchpad (`…/scratchpad/p3c3b/*.py`). Every number below is synthetic; see "Declined to judge".

### Strengths
- The change is small and in the right place. `Span.envelope` is a defaulted last field, so every P3c-3 test and
  caller stays as it was. `same_sound` is a pure function, and `decide` changes in one line (speech.py:178).
- It is monotone towards duplicates (check 3). A "different" verdict only removes `c` from `others`. `heard` can then
  only fall, so a covered span can turn chosen but a chosen span never turns covered. A span chosen because of the
  verdict can cover a later span only through its own `same_sound` check, and it is transcribed itself. A false
  "different" (a clipped, gated or AGC-pumped copy) therefore gives a duplicate, never a loss. This holds for the
  winner pass, for the C1 prefix parts and for late audio (R4), because all three go through the same loop.
- Deploy-time behaviour is sound (check 1):
  - Migration 014 is `ADD COLUMN … real[]`, nullable with no default, so it only changes metadata on PG 17. It holds
    an ACCESS EXCLUSIVE lock for a moment and at most waits behind one grouping transaction.
  - A P3c-3 worker that is still running during the api/worker swap inserts with explicit columns, so its rows get a
    NULL envelope and follow the time rule.
  - A rollback image ignores the column.
  - Regions pending at the deploy keep the time rule. The NULL test is symmetric
    (`not a.envelope or not b.envelope`), so a decision between an old and a new region is the same whichever side
    waits. `_span` also maps `[]` to "no envelope".
- The Pi alone is unchanged (check 2). `groups` joins only spans of different devices, and `others` filters on
  `device_id`, so `same_sound` is never called. Detection adds an O(n) envelope and about 10 floats per second of
  speech.
- The flat-envelope guard (`ptp`), the pinned sign of `shift`, and the half-of-the-shorter rule against chance
  matches on a few frames all have tests. The ledger records measured numbers for each threshold change.
- The envelope is stored, so the §8.2 tuning can be recomputed offline from the database without keeping audio.

### Issues

#### Critical
None.

#### Important

**I1. A chosen span that cannot be compared still covers by time, alone or in a union. A different conversation's
region is lost.** `speech.py:126-131` (`same_sound` returns True when `best_correlation` is None), used at `:178`.
- **What happens:** S2 keeps "nothing to compare → same" for spans where both sides have an envelope. Two cases reach
  that path in a real two-room day:
  1. A short chosen span, under 10 frames.
  2. Long chosen neighbours that reach `s` mostly through the 2 s slack widening, under half of the shorter span at
     every lag.

  `_heard` widens each chosen span by 2 s on both sides, and the union (R2) adds them up. So these spans cover `s`
  without its sound ever being compared. This is the loss the owner's I3 ruling forbids, and it contradicts the
  ledger's own rule that a duplicate beats a lost transcript.
- **Reproduced** with `speech.decide`. The Pi has the higher SNR, and the camera region holds a different
  conversation:
  - **A blip.** A 0.5 s Pi "ừ" at 3.0 s (`[-60,-25,-22,-30,-60]`) inside an 8 s camera region: `best_correlation`
    is None, and the region is covered by the blip (heard 4.5 s ≥ 4 s). A 0.9 s blip gives the same result.
  - **A union.** Pi regions of 20 s ending at 5 s and starting at 15 s, with a 20 s camera region from 0 to 20: both
    pairs are None, heard 7 + 7 ≥ 10, and the region is covered. The same holds with 8 s Pi regions around a 10 s
    camera region.
  - **A simulated day.** In a two-room hour from the `talk()` generator, camera `c00771` (3.1 s) scored 0.477
    against the region that actually overlaps it, so that verdict was "different". It was still covered through its
    uncompared neighbours, heard 0.6 s and 1.1 s.
- **Fix** (strictly more conservative; for any pair the result is unchanged or False):
  ```python
  def same_sound(a, b, slack=GROUP_SLACK):
      if not a.envelope or not b.envelope:
          return True                      # stored before 014: the time rule
      best = best_correlation(a, b, slack)
      if best is None:                     # b too short, or too little in common at every lag
          return len(a.envelope) < SAME_SOUND_MIN_FRAMES   # only a region too short to have a shape keeps the time rule
      return best >= SAME_SOUND_MIN
  ```
  `same_sound` becomes asymmetric, `a` being the waiting span, so say so in its docstring.
- **Test changes:**
  - Invert `test_too_little_common_time_counts_as_the_same` (:59) and the `same_sound` assertion in
    `test_a_short_common_stretch_is_not_compared` (:74). With the fix patched in, only these two of the 34 pure tests
    fail, and their decisions do not change.
  - Add decide-level tests for the blip and the union, where both regions are chosen.
- **Measured effect** of the fix on three simulated hours (about 360 camera regions each):

  | Scenario | Current | With the fix |
  |---|---|---|
  | Two rooms, camera regions lost | 48 / 47 / 48 | 34 / 37 / 39 |
  | Two rooms, regions ≥ 6 s lost (one hour, of 206) | 10 | 5 |
  | Same room, camera regions duplicated (of about 326) | 3 / 3 / 0 | 8 / 5 / 0 |

  Record it in the ledger as an amendment of S2.

**I2. For short utterances the 100 ms envelope cannot tell two sounds apart, and the docs promise more than the code
does.** `speech.py:19-23, 100-123`. Docs at `p3c-multicam-design.md:75`, the D33 row (`loopi-design.md:93`) and
`deploy/README.md:59-61`.
- **What happens:** the best Pearson correlation over 41 lags on 10 to 30 frames often reaches 0.7 by chance. A
  short VAD region is a "bump" with low padded edges, and the lag search can line its edges up with any stretch of
  the other room.
- **Measured** (synthetic):

  | Measurement | 1 s | 1.5 s | 2 s | 3 s | 4 s | 6 s | 10–20 s |
  |---|---|---|---|---|---|---|---|
  | Unrelated `talk()` pairs ≥ 0.7, equal lengths (780 pairs) | – | 22 % | 32 % | 13–18 % | 5–9 % | 0.1–0.6 % | 0 % |
  | Kitchen utterance lost inside a chosen 20 s "TV" region (400 trials) | 78 % | 41 % | 24 % | 3 % | 0.8 % | 0 % | 0 % |

  Regions under 1 s always keep the time rule, so they are always lost in this case.
  - Across a simulated two-room hour: P3c-3 loses 100 % of the other room. This branch loses about 10 %, and those
    are mostly regions under 3 s, plus all 14 regions under 1 s.
  - So the branch is a large improvement and no regression. But "two different conversations at once are both
    transcribed" (README, spec §4.3) is not true for short utterances. Short utterances are common at home: "Ăn cơm
    chưa?", "ừ", "con đi đây".
- **Why the tests do not show it:**
  - The threshold pin `test_no_unrelated_pair_of_talk_envelopes_reaches_the_threshold` (:81) uses only 6 s
    envelopes.
  - Its margin is a seed artefact. Adding seed 40 already gives the pair (32, 40) = 0.707 at offset 0.
  - This goes beyond the parked "numpy stream" Minor: the pin claims a property that does not hold in general, and
    it hides the dependence on length.
- **Fix before the deploy** (docs and ledger only):
  - State the limit in §4.3, the D33 row and the README. For example: "đoạn ngắn dưới khoảng 3 giây có thể vẫn bị
    coi là cùng âm thanh; đoạn dưới 1 giây và đoạn không so được giữ luật thời gian". With I1 fixed, that becomes
    "dưới 1 giây giữ luật thời gian".
  - Rename the pin, or comment that it covers 6 s regions only.
  - Record in the ledger that short regions are a known limitation, to be tuned at §8.2.
- **After §8.2** (owner's ruling, with real numbers), possible options:
  - A threshold that rises as the compared frames fall, for example a significance level corrected for 41 lags.
    About 0.85–0.9 at 15–30 frames, at the cost of more duplicates of short sentences.
  - The 20 ms frame RMS, which `frame_rms` already computes, for regions under about 4 s.
  - A lag window narrowed to each camera's measured latency.

#### Minor

**M1. Cost: every waiting span is correlated with every chosen span of the other microphone in its group, including
spans minutes away.** `speech.py:178`.
- **Measured:** a 4 h same-sound conversation decided as one group (2206 regions) takes 2.6 s in P3c-3. This branch
  takes 15–44 s (the machine was loaded) and makes 1.2 M `best_correlation` calls. With a time filter first it takes
  3.0 s, with identical decisions.
- **Impact:** normal operation, and the outage catch-up via WAIT and late audio, decide a minute or so per pass, so
  this is not a blocker. A pass that decides hours at once, for example after the "light" queue has stalled, holds
  the grouping lock for that long.
- **Fix:** `others = [c for c in chosen if c.device_id != s.device_id and _heard(s, c, slack) > 0 and same_sound(s, c, slack)]`.
  - This is behaviour-preserving. A span with zero `heard` adds nothing to the union. It can be `best` only when
    every span heard is 0, and then `s` is chosen either way.

**M2. Acceptance cannot see the lag.** `best_correlation` returns the value but not the lag, and nothing is logged.
- **Why it matters:** the lag search is tied to `GROUP_SLACK` (±2 s). If a camera's audio latency exceeds about 2 s,
  the true alignment falls outside the search, and every sentence on that camera becomes "different", so it is
  duplicated.
- **Fix:** return `(corr, lag)` from a helper, or ship a read-only acceptance script over the stored envelopes. At
  §8.2, record the best lag per camera next to the correlation.

**M3. Spec §8 has no acceptance item for I3.** §8.2 tests only "one sentence → one transcript", and the plan's Task 4
mentions the two-conversation measurement only in passing.
- **Suggestion** (a spec change, so the owner's): "TV ở phòng khách và nói chuyện ở bếp cùng lúc: cả hai được chép,
  kể cả câu ngắn", measured by region length.

### Answers to the cross-cutting checks
1. **Deploy time.** Consistent and safe (see Strengths). Migration 014 only changes metadata.
2. **The Pi alone.** Unchanged.
3. **"Different" for the same sentence.** It gives duplicates only, never a loss. The loss path is the opposite case,
   "can't compare → same" (I1).
4. **Cost.** Not bounded: O(waiting × chosen) correlation calls per group. It is fine for incremental passes, and a
   one-line filter bounds it (M1).
5. **Real-world robustness.** Synthetic syllabic speech went through two chains:
   - Pi chain: room noise at −60 dB and libopus at 24 kbps.
   - Camera chain: reverb, −12 dB, a 1 s AGC, a −50 dB noise gate (digital silence floored at −70), AAC at 32 kbps,
     0.8 s late, and its own VAD edges (±0.3 s).

   Results (40 trials per cell):

   | Region length | Same sentence: median correlation | Same sentence below 0.7 (duplicate) | Different: 90th percentile | Different ≥ 0.7 (lost) |
   |---|---|---|---|---|
   | 15 s | 0.77–0.90 | 0–10 % | 0.27–0.33 | 0 % |
   | 6 s | 0.81–0.94 | 0–10 % | 0.48–0.57 | 0–2 % |
   | 1.5–3 s | 0.73–0.93 | 2–30 % | 0.68–0.79 | 8–28 % |

   - The worst figure, 30 % of short same sentences below 0.7, is heavy reverb (RT60 1.2 s).
   - The gate, the AGC and the codecs barely matter. Reverb and region length dominate.
   - **Verdict:** 0.7 is a sane default for regions of 6 s and longer. For short regions, no threshold separates the
     two cases at 100 ms resolution (I2).
   - **Measure at §8.2,** per length bucket (< 1 s, 1–3 s, 3–6 s, ≥ 6 s) and per camera, the correlation and best
     lag for:
     - (a) one sentence heard on the Pi and a camera in the same room;
     - (b) a sentence leaking faintly into the next room's camera;
     - (c) TV or talk in one room with talk in the other.

     Also count overlapping chosen pairs per day, which gives the duplicate rate.

### Declined to judge
- **Real speech.** All numbers come from the `talk()` generator or synthetic syllabic noise, not from the owner's
  cameras. I did not read the live media: it is private and it belongs to the live stack. Real envelopes can match by
  chance more or less often than these.
- **Database-backed tests.** Not run: testcontainers needs Docker, which the brief puts out of bounds. I relied on the
  ledger's full-suite results (1508 passed, 99 %). The tests of `group_speech` and `multimic_paths` use no envelopes,
  or the same source on both microphones, so I1's fix would not change them.
- **Commit count.** The brief says 9 commits; the range holds 7. As far as the ledger goes, nothing is missing.
- **Thresholds.** GROUP_SLACK, COVERED_SHARE and the −70 dB floor are owner-approved, and I did not re-tune them.
  Whether to trade duplicates for losses on short regions is the owner's call.

### Assessment
**Ready to deploy?** With fixes.

**Reasoning:** The branch is a large, strictly loss-reducing improvement over the live P3c-3: in a synthetic two-room
hour, the other room's lost regions fall from 100 % to about 10 %, and the cost is only duplicates. Deploy time and
the Pi alone are safe. Before the deploy:
- Apply I1 (five lines and the test inversions) and M1 (one filter).
- Correct the docs' promise for short utterances (I2).
- Tune short regions at §8.2 with per-length numbers.
