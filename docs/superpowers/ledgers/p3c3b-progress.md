# SDD ledger — plan: docs/superpowers/plans/2026-10-21-p3c3b-same-sound.md
Spec: P3c design §4.3 / D33, amended by the owner 2026-10-21 (I3: "Thêm kiểm tra" — a same-sound check). Worktree /home/soul/loopi-p3c3, branch feat/p3c3b from main a3b4ad9; plan commit ce38e3c. Owner approved the direction in the I3 question; S1–S3 are the plan's rulings. Task 4 (deploy) is the controller's, under the owner's "chạy hết P3c-3".

## Pre-flight scan
| Pair / task | Produces → consumes | Finding |
|---|---|---|
| T1↔T2 | speech_region.envelope real[], speech.envelope, ENVELOPE_HOP_S → Span.envelope via grouping._span, same_sound | consistent |
| T1 | detect_file now returns 3-tuples; every caller (tests) updated | agrees |
| T2 | Span gains a defaulted last field — existing tests construct Span positionally with ≤ 6 args → unchanged | agrees |
| T2 tests | fixed seeds; if unrelated envelopes correlate ≥ 0.5 the implementer reports numbers (no seed shopping) | noted |
| T3 | docs | n/a |
Scan clean.
Task 1: dispatched implementer (sonnet), BASE ce38e3c
Task 1: implementer DONE (7e1087e; brain 1496 passed, 1 skipped, 99 %). Note: a < 100 ms region would store [] (cannot happen: regions ≥ 0.5 s); grouping maps [] and NULL to no envelope.
Task 1: review Approved (no Critical/Important; real[] round trip checked). Minor → Task 2: drop the unused import; pin that detect_file's envelope is the raw audio's (equals speech.envelope(load_audio(path), r.start, r.end)).
Task 1: complete (commit 7e1087e)
Task 2: dispatched implementer (sonnet), BASE 7e1087e
Task 2: implementer DONE_WITH_CONCERNS (d765bfc; brain 1502 passed, 1 skipped, 99 %). Correlations: same sentence 0.996, different conversations 0.361. test_multimic_paths' end-to-end test recorded digital silence on both mics (flat envelopes never match) → now records QUIET_SPEECH. Synthetic margin: 741 unrelated talk pairs (seeds 1–39) → best-lag correlation mean 0.39, max 0.68, 12.8 % ≥ 0.5.
Ruling: accept the end-to-end test's QUIET_SPEECH input (two flat envelopes stay "different": a duplicate transcript is the safer failure than a lost one).
Ruling: raise SAME_SOUND_MIN to 0.7 (amends S2) — with 0.5, 12.8 % of unrelated synthetic pairs would be merged and one conversation lost; at 0.7 none of the 741 are, while the same sentence reaches 0.996; a false "different" only duplicates a transcript — cost if wrong: real same-sentence pairs between rooms below 0.7 are transcribed twice until §8.2 tunes it. A regression test pins that no unrelated pair of seeds 1–39 reaches SAME_SOUND_MIN.
Task 2: fix round 1 dispatched (resume implementer)
Task 2: fix round 1 DONE (e017d82; SAME_SOUND_MIN 0.7, best_correlation helper, 741-pair pin worst 0.681 at a 0.3 s offset; brain 1503 passed, 1 skipped, 99 %). Concern: at larger offsets between two unrelated 6 s regions, few common frames give chance matches ≥ 0.7: 1.0 s 0.4 %, 1.5 s 2.7 %, 2.0 s 4.5 %, 3–4 s 25 %.
Ruling (amends S2): compare only lags whose common frames are at least max(SAME_SOUND_MIN_FRAMES, half of the shorter envelope) — chance correlations on a few frames must not merge two conversations; measure the offset table again and pin the offsets that are now clean — cost if wrong: a real same sentence whose copies overlap less than half (the shorter) falls back to the time rule only when no lag qualifies.
Task 2: fix round 2 dispatched (resume implementer)
Task 2: fix round 2 DONE (81abeb2; brain 1508 passed, 1 skipped, 99 %). Unrelated pairs reaching 0.7 (of 741): 0 % at offsets 0–0.6 s, 0.4 % at 1.0–4.0 s, 0.3 % at 5.0 s, none beyond (the time rule then does not cover); the same sentence 0.996 at 0.8 s and 1.5 s lags. Residual false matches go to acceptance §8.2 (ruling: no further tuning without real numbers).
Task 2: review Needs fixes (test only) — the sign/size of `shift` is unpinned: a sign-flipped mutant passes all 68 tests and would duplicate a region that starts > 1 s into another mic's region. Minor: the 0.7 comment holds only for offsets ≤ 0.6 s; the flat test does not pin the ptp guard (no-warning assertion); numpy stream stability vs the 0.681 margin (uv.lock pins numpy). Note: the reviewer removed its own old scratch dir under the scratchpad (scratch only, not the repo).
Ruling: fix round 3 (tests + one comment): the shifted-region test (cam = pi seconds 3–8, starting at 3 s → same, ≥ 0.9), the comment "at start offsets ≤ 0.6 s", a no-RuntimeWarning assertion on the flat test.
Task 2: fix round 3 dispatched (resume implementer)
Task 2: fix round 3 DONE (03262e7, tests + comment; 69 passed; the sign-flipped mutant and the guard-less mutant each fail a test — controller accepts the implementer's mutant evidence as the scoped re-review for this test-only round).
Task 2: complete (commits d765bfc..03262e7)
Task 3: dispatched implementer (haiku), BASE 03262e7
Task 3: implementer DONE (d5143d9; test_deploy 19 passed). Controller read the word diff (spec §4.3 with 0,7 and the half-of-the-shorter rule; binding D33 row; README; plan S2 "Amended during execution" + Interfaces 0.7) — accepted as the task review for docs. Older 0.5 mentions in the plan's body stay as history under the amendment note.
Task 3: complete (commit d5143d9)
Final review: dispatched (opus), range a3b4ad9..d5143d9
Final review (opus): Ready to deploy — With fixes. Strengths: a wrong "different" only adds duplicates; deploy-time safe (014 metadata-only; NULL envelopes keep the time rule; symmetric); the Pi alone unchanged. Important: I1 uncompared chosen spans (both have envelopes, no qualifying lag) still cover by time, alone or in a union → a different conversation lost (blip inside a long region; two neighbours union); I2 short utterances: 100 ms envelopes over 10–30 frames reach 0.7 by chance (2 s: 32 % of unrelated pairs) — the docs over-promise "both transcribed". Minor: M1 correlation cost unbounded (no time filter before same_sound); M2 acceptance cannot see the lag; M3 no §8 acceptance item for I3 (spec change, the owner's call). Robustness (synthetic codecs/reverb/AGC/gate): ≥ 6 s regions: same sentence median 0.77–0.94, different ≥ 0.7 in 0–2 %; 1.5–3 s: 8–28 % lost.
Ruling (amends S2): when both regions have envelopes and no lag qualifies, they are the same only if the waiting region is shorter than SAME_SOUND_MIN_FRAMES (under 1 s keeps the time rule); otherwise different — strictly more conservative (more duplicates, fewer losses: simulated two-room losses 48→34–39 per hour, same-room duplicates 3→5–8) — cost if wrong: a few more duplicate transcripts.
Ruling: M1 — only chosen spans that overlap s in time (with slack) are correlated (no decision changes). I2 — the docs state the limit: short phrases (under about 3 s) can still be taken as the same sound; tuned at §8.2 with per-length numbers. M2 (lag visibility) → measured offline from the stored envelopes at §8.2; M3 → asked with the owner's next acceptance.
Final fix wave: dispatched (resume the Task 2 implementer), BASE d5143d9
Final fix wave: DONE (8e4b8ac I1 + M1, d5c9cb2 docs I2; brain 1513 passed, 1 skipped, 99 %; speech.py 100 %; mutants: dropping the time filter fails the M1 test, the old fallback fails four tests). Controller read the diff (same_sound fallback, the _heard filter) — accepted as the scoped re-review. Accepted the extra spec clause ("không so được thì giữ đoạn lại, trừ đoạn dưới 1 giây").
Final review: clean after the fix wave.
Task 4 (deploy): backup ~/loopi-backups/p3c3b-2026-10-21-0826.dump (20 MB); feat/phase2 fast-forwarded to d5c9cb2; `up -d --build`; smoke OK; migration 014_speech_envelope applied; no failed detect/group/transcribe job. Since the P3c-3 deploy at 06:22 the Pi produced one speech region (chosen, transcribed → one speech moment): the live path detect → group → transcribe works on the Pi alone. That region predates 014 (no envelope); new regions carry one.
Pending: §8.2 with a camera with sound (record per-length correlations and the best lag per camera from the stored envelopes; tune SAME_SOUND_MIN for short regions with the owner's yes); M3 — the owner decides whether §8 gets an acceptance item for two simultaneous conversations.
