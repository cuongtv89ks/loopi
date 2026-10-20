# Final whole-branch review: P3c-3 multi-camera understanding

Range adf07fd..be293e9 (feat/p3c3, 18 commits). I reviewed it in four passes: the audio pipeline (migration, speech, grouping, asr, dispatch, reprocess, forget, status); chapters and rename; the vlog (material, script, render, vlogs); the docs. Then I ran the cross-cutting checks from the brief. 123 focused tests pass (`test_speech_groups`, `test_vlog_{runs,render,cameras,script}`, `test_group_speech`, `test_multimic_paths`, `test_detect_speech`, `test_transcribe_chosen`, `test_rename_refresh`; PATH=/usr/bin first). The repros below are scratch tests outside the checkout, in `/tmp/claude-1000/-home-soul-loopi/1af02cf5-04d9-4c40-b829-5e3b9b346da7/scratchpad/` (`chain.py`, `test_chain_db.py`, `test_res_change.py`, `test_res_reinit.py`). No worktree was created and no testcontainer is left running.

### Strengths
- **The Pi alone is safe.** Two regions from the same microphone never join a group directly, so each group holds one region. Speech that runs past a minute's end waits only for the next segment (`test_speech_at_a_minutes_end_waits_for_the_next_segment`).
  - With one camera the vlog is byte-identical: one run, no label, and the same list and chunk names, step names and argv. `test_vlog_render.py` is unchanged and passes.
  - The script prompt is also unchanged for one camera; the stored script only gains `device_id`.
- **Durability holds.**
  - Retention only touches `done`/`skipped` items (`retention.py:57`), so a pending item never loses its file.
  - Every stray path leaves the item pending, never done: an old R8 job, a stalled re-run, a transcribe job that finds waiting regions.
  - Detection writes its regions and `speech_detected_at` in one transaction under the item row lock.
  - A repeated detection keeps its regions.
- **The deploy-time lifecycle is handled.**
  - Old `transcribe_audio` jobs (lock `asr`, queueing lock `item:<id>`) take the R8 path: they detect, and grouping defers the real transcription within a minute.
  - Pending items with no job go through the dispatcher to `detect_speech`.
  - After a restart, stalled jobs are recovered and either re-detect or skip.
  - A dead-lettered detection counts as settled, so it does not block the minute it belongs to.
- **No lock cycle.** The order is always: day locks, then the grouping advisory lock, then item rows, then region rows. Nothing takes an item row lock and then the grouping lock.
  - `_detect` and `_transcribe` never take the grouping lock.
  - Grouping only updates `waiting` regions, and `_transcribe` only updates `chosen` ones. The `covered_by` foreign key check takes KEY SHARE, which is compatible with the NO KEY UPDATE of `moment_id`.
  - `_settle_items` skips an item whose transcribe job is todo or doing, so no second `asr:<id>` job is created. `asr` is held only by jobs that can run, apart from R8's short detection.
- **Migration 013 is instant on the big table.** It adds a nullable column with no default (a metadata-only change) and a new table with indexes. The range queries are covered by `item_started_at` and `speech_region_start`.
- **The Xử lý page and job system are wired correctly.** Labels exist for both new tasks. `detect_speech` and `transcribe_audio` keep retry buttons; `group_speech` is auto-rerun (its failed runs are pruned).
- **I re-checked the rulings.** These are sound:
  - union-based coverage
  - skipping a detection when `speech_detected_at` is already set
  - grouping after every detection
  - the R3 refinement: wait for audio that arrived unless its job is dead-lettered, END_SLACK, `mic_missing`
  - the namespaced lock
  - the waiting-regions guard
  - places taken from the shown and merged devices
  - runs per camera
  - spreading the leftover frames evenly
  - the font, its guard and the top-left label

  Two reservations, found as defects below: R3's per-minute bound is defeated by groups that chain across minutes (C1), and R10's 14 days are longer than the 7-day video retention (C2).

### Issues

#### Critical (Must Fix)

**C1. A conversation that two microphones hear is not transcribed until it pauses at a minute's end. The wait has no bound.**
- **Where:** `brain/src/loopi_brain/pipeline/grouping.py:111-119` together with `pipeline/speech.py:83-105`.
- **What:** `speech.groups` joins regions transitively across minutes. `_decide` decides a group only when *every* minute any of its spans touches is ready. In an ongoing conversation the newest region runs to its item's end, so it touches the next minute, which has not arrived yet. When that minute arrives, its regions extend the chain into the minute after. The group never becomes ready while speech keeps crossing minute ends.
- **Repro (DB, real `group_speech`):** two microphones, 10 minutes of talk (15 s regions, 1.5 s pauses). Every minute was delivered and detected on time by both, and `group_speech` ran after each minute. After 10 minutes, **80 of 80 regions were still `waiting`**.
- **Repro (pure functions):** a 10-minute conversation forms one group of 129 spans touching 12 minutes. With 1–6 s pauses, groups still span 3–5 minutes.
- **Why it matters:** picture a TV or a dinner conversation within earshot of both the Pi and a camera microphone. There is no transcript for the whole period, then the whole period's Whisper work is queued at once. This breaks:
  - D33's "Trong một phút … gom thành nhóm" and "chờ tối đa 3 phút";
  - the 15-minute average latency of §8.4;
  - the README bullet "waits up to 3 minutes".

  The Pi alone is unaffected.
- **Fix:** bound the decision per minute. In `_decide`, still pick the winner from the whole current group, but only apply decisions to waiting spans whose own `minutes_touched` are all ready. The not-ready tail stays waiting and is decided on a later pass through the R4 path, against the decisions already taken. The alternative is to build groups inside a minute window, as the spec words it.
- **Test:** 10 minutes of continuous speech on two microphones; the first minute's regions are decided within about 3 minutes of their arrival.

**C2. Renaming a place, or pressing "Lưu" without changing it, re-scripts 14 days of vlogs. Days 8–14 permanently lose their timelapse.**
- **Where:**
  - `pipeline/chapters.py:196-230`: `refresh_places` stales each day's recap unconditionally at line 224.
  - `web/routes.py:290-295`: the refresh runs after every save, changed or not.
  - `pipeline/vlogs.py:367`: a recap newer than the script makes the vlog due again.
  - `vlog/material.py:77`: timelapse segments need `raw_deleted_at IS NULL`; video retention is 7 days (`store.py:19`).
- **Scenario:** two weeks after cameras are added, the owner moves an iPhone and renames it, or re-saves "Phòng khách".
  1. Every day in the last 14 with moments from two devices gets its recap rewritten.
  2. `plan_vlogs` then re-scripts and re-renders each of those days' vlogs, one earlier day per 15 minutes.
  3. Days 8–14 have no MP4 left. Their timelapse vlogs are replaced by keyframe slideshows, and their conversation sound is lost too (`_sound` returns None because the raw audio has expired).

  This loss cannot be undone. On top of it come hours of work on the single `llm` lock (14 chapter builds, 14 recaps, 14 scripts) plus 14 renders, all of it pointless when the name did not change.
- **Fix:**
  1. Skip the refresh when the cleaned name equals the stored one: read it before `set_location`, or have `set_location` report whether it changed.
  2. Never let a rename replace a rendered video vlog with a slideshow. Two options:
     - (a) Limit the refresh to days whose video is still kept, `min(RENAME_REFRESH_DAYS, video_days)`. This changes R10 and the "14 ngày" wording added to spec §4.4, so it needs the owner's ruling.
     - (b) Have `plan_vlogs` skip re-scripting a day whose vlog `source = 'video'` once that day's video has expired. The spec only promises refreshed summaries and recaps, so this needs no spec change, and it also protects the existing reprocess-a-recap path.

  This escalates the Task 7 parked Minor.

#### Important (Should Fix)

**I1. After "Xử lý lại → transcripts" for a day, live transcripts wait behind that day's whole Whisper backlog, even on the Pi alone.**
- **Where:** `pipeline/grouping.py:147`. `_settle_items` defers `transcribe_audio` with no cap at priority 0. This breaks the promise in `pipeline/dispatch.py:129-145` ("a fresh upload … never queues behind a whole backlog").
- **Scenario:**
  1. A reprocess resets about 1440 items.
  2. The dispatcher feeds 20 detections at a time. Detection is fast and has no lock, so within about an hour grouping has queued a transcription for every item with speech — hundreds of jobs, first-in first-out under `asr`.
  3. A fresh upload's transcription queues behind all of them, so live transcripts are hours late.

  Before this branch, at most about 20 backlog transcriptions were ahead of a fresh one. This escalates the Task 3 parked Minor.
- **Fix:** in Procrastinate 3.10, `procrastinate_fetch_job_v2` lets a higher-priority todo job with the same lock run before an older one (I checked the installed schema). So:
  - Defer backlog transcriptions at a lower priority, for example −1 when the item was received more than about 15 minutes ago. Fresh ones stay at 0, and evaluation stays lowest.
  - Or cap old items at `dispatch_max_queued` waiting transcribe jobs.
- **Test:** queue backlog transcriptions, then a fresh one; the fresh one runs first.

**I2. If one camera's resolution changes during a day, that day's vlog fails every time.**
- **Where:** `vlog/render.py:520` keys runs on `(camera, label)` only, and `Track` (`render.py:94`) has no dimensions.
- **Repro (host ffmpeg 6.1.1):** one camera, a 640x360 segment then a 360x640 one, gives `RenderError: timelapse-full … ffmpeg exited 0` from the frame-count check. The render job retries twice and then lands on the dead-letter list, and "Dựng lại vlog" fails the same way.
- **Triggers:** an iPhone streaming app restarted in the other orientation or at another quality (the owner's two iPhones), or an Imou main/sub stream switch. This escalates the Task 9 open item.
- **Fix:** read width and height in `probe_mp4` (tkhd, or avc1 in stsd) and start a new run when they change, keying runs on camera, label and size.
  - `-reinit_filter 0` is not a good alternative. It keeps the frame count (measured: 48 of 48 frames), but `scale` then keeps the first segment's output size, so the portrait part would be stretched.

**I3. Two different conversations at the same time in two rooms: the louder one wins and the other room's speech is never transcribed.** This is a spec-level issue and needs the owner's ruling.
- **Where:** `pipeline/speech.py:83-105` and `126-135`. Groups are formed by time overlap alone, and SNR then compares two unrelated sounds.
- **Scenario:** a TV plays in the living room (Imou) while people talk in the kitchen (iPhone), at overlapping times. If the TV region has the higher SNR, the kitchen sentence is marked covered and points at the TV transcript. The hallucination or outro rules may drop that TV transcript too. The kitchen sentence is lost silently.
- **Why it matters:** this contradicts D33's "Câu chỉ một phòng nghe thấy vẫn được chép". §8.2 (one sentence heard by two microphones) would still pass, so acceptance would not catch it.
- **Fix:** since D33 defines groups by overlap, the owner should decide. Before covering a region, require both regions to be the same sound. For example, store each region's 20 ms RMS envelope at detection (`speech.frame_rms` already computes it; about 1000 values for 20 s) and correlate the two at lags within ±`GROUP_SLACK`. Regions that do not correlate are not grouped. If the owner declines, record it as a known limitation.

#### Minor
- **M1.** `grouping.py:97` vs `:117`. Waiting spans are read at the start of the pass and minute readiness is checked later, in separate READ COMMITTED statements. A detection that commits in between makes its minute ready while its regions are missing from this pass. They are then decided next pass as late audio (R4), so the winner may not be the clearest microphone. Fix: re-read the group's spans after its minutes are found ready.
- **M2.** `chapters.py:218`. `refresh_places` runs inside the HTMX request, and `_points` loads and normalises the embedding of every moment in the 14 days, only to hash ids and places. With several cameras that is tens of thousands of vectors and seconds of request time. Fix: a signature-only query (id, device_id, location).
- **M3.** `web/templates/pipeline.html:9` and `status.py:118`. "Tiếng nói chờ mic khác" also counts speech waiting for its own microphone's next segment, which is all it counts on the Pi alone. Suggested wording: "Tiếng nói chờ ghép (mic khác hoặc phút sau)".
- **M4.** `deploy/README.md` has no "Rolling back P3c-3" note. An older image's worker fails queued `detect_speech` jobs as an unknown task, and retrying them fails again. At deploy time, a new api overlapping the old worker for a few seconds can fail a fresh upload's `detect_speech` the same way. Suggested notes:
  - Recreate api and worker together.
  - After the deploy, check Xử lý for failed "Tìm tiếng nói" jobs and retry them.
  - For a rollback, clear `analysis_job_id` of items whose `detect_speech` job is todo or failed, before starting the old image.

### Parked findings you would escalate
- **Task 3 Minor**, "transcribe jobs deferred by grouping bypass DISPATCH_MAX_QUEUED": escalated to **I1**, with a concrete multi-hour delay on the Pi alone after a reprocess.
- **Task 7 Minor**, "an unchanged name still stales up to 14 recaps": escalated into **C2**, because the staled recaps re-script the vlogs, and days past the 7-day video retention become slideshows permanently.
- **Task 9 open**, "a camera whose resolution changes mid-day still goes through one ffmpeg": escalated to **I2** (reproduced).
- **Not escalated (stay Minor):**
  - the ON_SCHEDULE label for `group_speech`
  - `covered_by` left NULL after the chosen side is reprocessed or forgotten
  - a ≤ 2 s segment straddling a minute
  - one-sample rounding of region ends
  - reprocess taking the grouping lock for every choice
  - no per-day try in `refresh_places`
  - the guard covering a missing font but not an ffmpeg without drawtext
  - tiny targets giving a segment 0 frames
  - `named` counting highlights whose picture is gone
  - the two adjacent "(chi tiết …)" parentheticals in the D33 row

### Merge with main: likely conflicts
- A trial merge (`git merge-tree <base> f84bcb9 be293e9`, which writes nothing) shows **no conflicts**.
- `deploy/README.md`: both sides add bullets to the network-camera list at different lines (the branch after "The camera shows up as a device…", main after "Thử kết nối…"). Git merges them automatically.
- `devices.html`, `test_web_cameras.py`, `agent/` and the P3c-2 ledger change on main only.
- **No semantic conflict.** While a refused login is waiting, main's agent sets `mic_missing` (`agent/src/recorder/audio.rs`), and grouping's `_expected` already skips such devices, so a locked-out camera is not waited for.
- Run the brain suite once after the merge, since main brought new web tests.

### Declined to judge
- R10's 14-day window and the hours of LLM work that a *real* rename costs: an owner-approved ruling. I flagged only the retention interplay and the no-op save (C2).
- D34's model that a place name is the device's current name, so a moved camera relabels its past summaries: a spec decision.
- The thresholds GROUP_SLACK 2 s, COVERED_SHARE 0.5, WAIT 3 min, EXPECTED_WINDOW 45 s and the −70 dBFS floor: owner-approved rulings; I did not re-tune them.
- Speaker identity and diarisation: out of scope in spec §11. I3 is about detecting the same sound, not about identifying speakers.
- The agent and the protocol: the branch has no diff under `agent/` or `docs/protocol/`.
- A reprocess that resets an item while its Whisper run is in flight (the run then stores over the reset): this race existed before the branch.
- The upload fast path, which defers jobs with no cap after a brain outage: unchanged by the branch.
- An evaluation clip can now take `asr` in the gap between a live item's detection and its transcription job. That is at most one clip, a few seconds; negligible.
- Whether the image's ffmpeg 7.1 has drawtext, and how DejaVu renders Vietnamese, including decomposed marks: Task 11 checks this inside the image.
- Detection throughput and CPU on the i5: not measured.
- The Vietnamese wording of the new labels, beyond M3.

### Recommendations
1. Fix C1 and C2 before a second microphone records for long, and before any camera has a week of history. Turn the scratch repros (`test_chain_db.py`, `test_res_change.py`) into regression tests.
2. Fix I1 with a lower priority for backlog transcriptions, and I2 by splitting runs on size. Ask the owner for a ruling on I3. Record all three rulings in the ledger.
3. Task 11 checks:
   - drawtext and the font inside the image;
   - on the Pi alone, "Tiếng nói chờ mic khác" stays near 0 and no old `transcribe_audio` job is still todo a few minutes after the deploy;
   - no failed "Tìm tiếng nói" or "Nhận dạng giọng nói" jobs;
   - the audio latency on Xử lý is about the same as before the deploy.
4. Once C1 is fixed, re-check the README bullet "waits up to 3 minutes" so it is true.

### Assessment
**Ready to deploy?** With fixes

**Reasoning:** On the Pi alone the branch is safe: transcripts are unchanged apart from at most one segment's wait, the vlog argv is byte-identical, there is no lock cycle, the migration is instant and in-flight jobs are handled. But the multi-camera use it exists for has two must-fix defects. Any conversation two microphones hear goes untranscribed until it pauses at a minute's end (reproduced: 80 of 80 regions undecided after 10 minutes). A rename, even a no-op save, rewrites 14 days of recaps and permanently turns the timelapse vlogs of days 8–14 into slideshows.
