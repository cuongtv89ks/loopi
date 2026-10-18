# Audio Level Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Loopi's recordings audible and transcribable: the agent amplifies the USB microphone by a configurable digital gain through a peak limiter before Opus encoding, and the brain normalises the level before VAD/Whisper and for timeline playback, without ever changing a raw file.

**Architecture:** Part A (agent, Rust, branch `feat/audio-gain`): a pure `recorder::gain` module (gain plus an instant-attack, 200 dB/s-release limiter at −1 dBFS) wraps the ALSA source factory in `run.rs`, so the capture loop and its tests stay as they are; `capture.audio_gain_db` (default 20 dB, 0–36) sets it and `selftest` reports the level before and after it. Part B (brain, Python, branch `feat/phase2`, slots into P2a before its Task 11): one numpy helper `audio_level.level_gain` computes the gain (99.9th percentile of |x| to −6 dBFS, at most +30 dB, never below 1, and never lifting the room's noise floor above −40 dBFS); ASR applies it to the decoded array before Silero VAD and Whisper; a new `GET /audio/{id}.opus` route serves a cached normalised Ogg Opus (ffmpeg `volume` + `alimiter`) from `view/`, falling back to the raw file. Task 5 (controller) builds image smoke8, the owner flashes it, and the controller checks that new segments arrive about 20 dB louder.

**Tech Stack:** Rust (edition 2021, rust-version 1.80; host toolchain 1.93) with the existing `opus` 0.3 / `ogg` 0.9 crates, no new crates; Python 3.13 + uv, numpy, anyio, FastAPI 0.143 / Starlette 1.8 `FileResponse`, pydantic `Field`; ffmpeg 9 (`volume`, `alimiter`, `libopus`), faster-whisper's bundled Silero VAD; pytest + testcontainers; Buildroot 2026.02 image build in the `loopi-os-builder` container.

**Spec:** `docs/superpowers/specs/2026-10-14-loopi-design.md` (Vietnamese, binding). Task 2 adds decision **D19** (§3) and updates the "Sửa đổi" header line, the §6.6 `loopi.toml` example, the §7.2 `recorder::audio` row, the §9.2 audio steps and §13. The design brief (owner decision 2026-10-15, "hướng 2 kết hợp với hướng 1") is session scratch (`/tmp/claude-1000/-home-soul-loopi/1af02cf5-04d9-4c40-b829-5e3b9b346da7/scratchpad/audio/design.md`); its decisions and evidence are restated below so this plan stands alone. The P2a plan `docs/superpowers/plans/2026-10-15-p2a-perception-pipeline.md` (on `feat/phase2`) governs Tasks 3–4 too.

**Evidence (measured 2026-10-15 on the live device and the brain's data):**
- Mic: C-Media 8086:0808 mini USB mic ("USB PnP Sound Device"). Mixer at power-on: Mic Capture Volume 0/16 (0–23.8 dB), Auto Gain Control on; the image never sets mixer levels. Volume 16 with AGC on gave only +2–3 dB; AGC off gave −70 dBFS (worse). Hardware gain cannot fix it; AGC stays on.
- All recorded segments of two days (1 337 measured): `ffmpeg volumedetect` mean_volume median −56.4 dB (10th–90th percentile −60.4 … −53.3 dB), max_volume median −31.1 dB, day and night alike. Isolated one-sample glitches reach 0 dBFS, so a peak-based gain would do nothing: use a high percentile.
- Silero VAD finds faint speech in a few raw segments; Whisper tiny transcribes nothing from a raw segment but Vietnamese after ≈ +34 dB normalisation. Browser playback is inaudible.
- Prototype measurements behind the noise-floor rule (Task 3): white/pink/brown noise at the mic's level gets no Silero speech even at +30 dB; a real speech segment attenuated to raw level gets +28.2 dB from the rule and Silero then finds 31.1 s of speech instead of 18.0 s.

**Branches, worktrees and order:**

| Task | Branch | Worktree | Notes |
|---|---|---|---|
| 1 `recorder::gain` | `feat/audio-gain` (= `feat/phase1` @ 7021705) | `/home/soul/loopi-audio` | |
| 2 config, wiring, selftest, docs, D19 | `feat/audio-gain` | `/home/soul/loopi-audio` | needs Task 1 |
| 3 (P2a Task 10a) level helper + ASR | `feat/phase2` (P2a Tasks 1–10 done, @ 2a5c2c3) | `/home/soul/loopi-p2` | independent of Tasks 1–2 |
| 4 (P2a Task 10b) playback route + timeline | `feat/phase2` | `/home/soul/loopi-p2` | needs Task 3 |
| 5 image smoke8, flash, level check | `feat/audio-gain` (no commit) | `/home/soul/loopi-audio` | controller only; needs Tasks 1–2 and the owner's yes |

Part A and Part B share no code (the brain normalises whatever arrives), so they can run in either order. Each branch gets its own final whole-branch review: `7021705..feat/audio-gain` and `2a5c2c3..feat/phase2`. P2a Task 11 (real-data run, from the P2a plan) comes after Task 4; it merges `feat/phase1` into `feat/phase2`, which is how D19 reaches `feat/phase2` once `feat/audio-gain` is in `feat/phase1` (Task 5, Step 7).

**Ledger:** every ruling goes to `docs/superpowers/ledgers/audio-level-progress.md`, committed on `feat/audio-gain` with the final review; rulings about Tasks 3–4 are also appended to `docs/superpowers/ledgers/p2a-progress.md` on `feat/phase2`. The rulings taken while writing this plan are listed at the end ("Rulings taken while writing this plan"): copy them into the ledger first.

**Prototype:** every code block below ran in throw-away worktrees of both branches, replayed task by task from the branch bases (7021705 and 2a5c2c3); every "Expected" is what that run printed. Baselines: agent 205 lib + 1 e2e + 4 safety tests; brain 312 passed, 1 skipped, 98 %.

## Global Constraints

- The spec is binding; D19 is the owner-approved direction (brief above). D19 lands on `feat/audio-gain` (Task 2); on `feat/phase2` the Part B comments cite "spec D19" until P2a Task 11 merges `feat/phase1` in.
- Device↔brain contract unchanged: `docs/protocol/v1.openapi.yaml` is not modified, and the gain is not a remote setting (`set_config` keeps only `audio_enabled`).
- Device gain: `capture.audio_gain_db` in `loopi.toml`, `f32`, default `20.0`, valid `0.0..=36.0`; NaN, ±inf and out-of-range values are rejected with the existing `ConfigError::Invalid` style: `capture.audio_gain_db must be 0..=36 dB (got …)`. 0 dB is the exact identity.
- Limiter: ceiling −1 dBFS (`29_204.0` of i16 full scale), instant attack, release 200 dB/s (≈ 100 ms after a 20 dB reduction); output never overflows i16; below the ceiling the output is exactly `round(x × gain)`; every (re)open of the mic starts with a fresh limiter.
- Brain level gain (`audio_level.level_gain`): the 99.9th percentile of |x| goes to `0.5` (−6 dBFS); the gain stays within `[1.0, 10 ** (max_gain_db / 20)]` with `max_gain_db = 30.0` (`LOOPI_ASR_MAX_GAIN_DB`, `0..=60` for ASR) and never lifts the noise floor (10th percentile of the 20 ms frame RMS) above `0.01` (−40 dBFS); empty or silent input gives `1.0` (no division by zero).
- Playback copy: `view/<device>/YYYY/MM/DD/<id>.opus`, made by ffmpeg with `volume=<gain>,alimiter=limit=0.891:attack=5:release=50:level=false:latency=true`, libopus 32 kb/s, 16 kHz mono, at most 120 s per ffmpeg pass; when it cannot be made, the raw file is served and a warning is logged.
- Raw media is never modified or deleted ("Audio and markers are never deleted automatically"; P2a: "P2a deletes no recorded media").
- Agent (CLAUDE.md): TDD, coverage ≥ 80 % of the agent library (`cargo llvm-cov --ignore-filename-regex 'main.rs'`), rustls with the ring provider only — `cargo tree -i aws-lc-rs` must find nothing; no new crates (`agent/Cargo.toml` and `agent/Cargo.lock` unchanged); Linux-only code must compile on the host (`cargo build`); the cross build happens only in Buildroot.
- Privacy invariants untouched: `recording_audio` is raised before the (wrapped) factory opens the mic, recorders die with the agent, pause persists and fails closed, no key turns the LED off.
- Durability untouched: queue, upload and recovery code is not changed.
- Brain (P2a Global Constraints): "TDD, coverage ≥ 80 % for `loopi_brain`, conventional commits, never commit secrets, never prune/delete Docker resources you did not create (testcontainers' own containers are fine)." "The P1 suite (133 tests) stays green. Run everything from `brain/`: `cd brain && uv run pytest`." "UI text Vietnamese; web POSTs need `HX-Request: true`." (the new route is a GET). Python ≥ 3.13, uv-locked: no new dependency.
- "**Tasks 1–10 never run `docker compose …` or `deploy/smoke.sh`.** The worktree's `deploy/` and the live checkout's `deploy/` both resolve to compose project `deploy`, which is the owner's live stack" (P2a). Here: Tasks 1–4 never run `docker compose`, `deploy/smoke.sh`, `os/build.sh` or any Docker command except testcontainers. Only Task 5 (the controller, after the owner's yes) runs `os/build.sh` and the `loopi-os-builder` container.
- Never commit or print secrets (`deploy/.env`, `os/out/wifi-psk`, PKI material, pair tokens); never modify tracked files in `/home/soul/loopi`; `/home/soul/loopi-data` is read only in Task 5 Step 6.
- Conventional commits ending with the session's attribution lines; code, comments and commit messages in English; brain UI text and the spec amendment in Vietnamese, in the spec's style.

## Review Focus

1. **A clap or a door slam right next to the mic, after +20 dB of gain:** the loud frames are held at −1 dBFS (never clipped or wrapped around), the quiet speech just before is untouched (no look-ahead), and full gain is back within 150 ms (still lowered at 50 ms, so no pumping click). Tests: Task 1 (`a_clap_next_to_the_mic_is_limited_and_speech_after_it_returns_within_150_ms`, `full_scale_input_never_overflows_and_never_passes_the_ceiling`), Task 2 (`microphone_level_is_reported_before_and_after_the_gain`, the full-scale case).
2. **A noise-only night segment:** the room's −55 dBFS hiss is not raised to speech loudness on playback (it stays at or below −38 dBFS RMS) and does not turn into speech for the real Silero VAD or into a transcript; after the device's own +20 dB it gets no brain gain at all. Tests: Task 3 (`test_a_noise_only_night_segment_stays_a_soft_hiss`, `test_room_noise_stays_without_speech_after_normalisation`), Task 4 (`test_a_noise_only_night_segment_plays_as_a_soft_hiss`).
3. **Single-sample glitches at 0 dBFS** (seen in real segments): one full-scale sample per second changes the gain by under 1 %, where a peak-based gain would be 1.0. Test: Task 3 (`test_isolated_one_sample_glitches_do_not_spoil_the_gain`).
4. **A corrupt or undecodable Opus file, or an ffmpeg that hangs or refuses, on the playback route:** the raw bytes are served with 200 and a warning naming the item is logged; no partial or empty copy is left under `view/`; a stuck ffmpeg is killed at the timeout. Tests: Task 4 (`test_an_undecodable_file_falls_back_to_the_raw_bytes[corrupt]`, `test_a_stuck_or_failing_ffmpeg_leaves_no_copy_and_the_raw_file_plays[ffmpeg-stuck]` and `[encoder-fails]`).
5. **Very long or zero-length files:** a 10-minute segment (the agent's longest) is normalised whole, its end included; a zero-length raw file is served as is without a copy; empty arrays and arrays shorter than one 20 ms frame give a bounded gain without numpy errors. Tests: Task 4 (`test_a_ten_minute_segment_is_normalised_whole`, `test_an_undecodable_file_falls_back_to_the_raw_bytes[zero-length]`), Task 3 (`test_silence_and_empty_input_get_no_gain`, `test_less_than_one_frame_has_no_noise_floor_but_still_a_bounded_gain`).

---

## File Structure

```
agent/src/recorder/gain.rs           (new, Task 1)  Gain + limiter on i16 frames; GainSource; with_gain(factory, db)
agent/src/recorder/mod.rs            (Task 1)       pub mod gain
agent/src/config.rs                  (Task 2)       capture.audio_gain_db, DEFAULT_/MAX_AUDIO_GAIN_DB, validation, tests
agent/src/run.rs                     (Task 2)       wraps the audio factory with the configured gain
agent/src/selftest.rs                (Task 2)       mic_level_detail(): level before and after the gain
agent/tests/run_e2e.rs               (Task 2)       decodes an uploaded segment: the default gain reached it
os/board/loopi/loopi.toml.example    (Task 2)       audio_gain_db = 20.0
os/README.md                         (Task 2)       "Microphone level" section
os/tests/check-image.sh              (Task 2)       2 checks: shipped agent knows audio_gain_db; example sets it (88 -> 90)
docs/superpowers/specs/2026-10-14-loopi-design.md (Task 2)  D19 + §6.6, §7.2, §9.2, §13
brain/src/loopi_brain/audio_level.py (new, Task 3)  level_gain(), noise_floor(), dbfs()
brain/src/loopi_brain/config.py      (Task 3)       asr_max_gain_db (LOOPI_ASR_MAX_GAIN_DB)
brain/src/loopi_brain/pipeline/asr.py (Task 3)      normalise(); transcribe_file() normalises before VAD/Whisper
brain/tests/helpers.py               (Task 3)       FakeTranscriber.peaks
brain/tests/test_audio_level.py      (new, Task 3)
brain/tests/test_asr.py, test_config.py (Task 3)
brain/src/loopi_brain/media.py       (Task 4)       _ffmpeg() helper (remux uses it too), audio_view_path(), normalise_audio()
brain/src/loopi_brain/web/routes.py  (Task 4)       GET /audio/{item_id}.opus
brain/src/loopi_brain/web/templates/timeline.html (Task 4)  <audio src="/audio/<id>.opus">
brain/tests/test_audio_playback.py   (new, Task 4)
deploy/README.md                     (Tasks 3, 4)   "Pipeline (P2a)" bullets for the audio level and playback
```

---

### Task 1: Digital gain and peak limiter (`recorder::gain`) — branch `feat/audio-gain`, worktree `/home/soul/loopi-audio`

**Files:**
- Create: `agent/src/recorder/gain.rs`
- Modify: `agent/src/recorder/mod.rs:1-4`
- Test: `agent/src/recorder/gain.rs` (`mod tests`)

**Interfaces:**
- Consumes: `recorder::audio::{AudioSource, AudioSourceFactory}` (`AudioSource::read_frame(&mut self, frame: &mut [i16; FRAME_SAMPLES]) -> io::Result<()>`, `AudioSourceFactory = Box<dyn Fn() -> io::Result<Box<dyn AudioSource>> + Send + Sync>`), `recorder::ogg_opus::{FRAME_SAMPLES, SAMPLE_RATE}` (320, 16 000).
- Produces (Task 2 relies on these):
  - `pub const CEILING: f32 = 29_204.0;`
  - `pub struct Gain` (`Debug, Clone`) with `pub fn from_db(db: f32) -> Gain`, `pub fn linear(&self) -> f32`, `pub fn apply(&mut self, frame: &mut [i16])` (any slice length; limiter state carries over between calls).
  - `pub struct GainSource` (an `AudioSource`).
  - `pub fn with_gain(factory: AudioSourceFactory, gain_db: f32) -> AudioSourceFactory` — every source the returned factory opens is amplified, with a fresh limiter; open and read errors pass through unchanged.

All commands run in `/home/soul/loopi-audio/agent`.

- [ ] **Step 1: Write the failing tests**

Create `agent/src/recorder/gain.rs` with only the test module:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::recorder::ogg_opus::FRAME_SAMPLES;

    /// Deterministic full-range "noise" (xorshift32): no extra crate for property-style tests.
    struct Noise(u32);

    impl Noise {
        fn next(&mut self) -> i16 {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 17;
            self.0 ^= self.0 << 5;
            (self.0 >> 16) as u16 as i16
        }

        fn frame(&mut self, limit: i16) -> [i16; FRAME_SAMPLES] {
            let mut frame = [0i16; FRAME_SAMPLES];
            for s in frame.iter_mut() {
                *s = (self.next() as i32 % (limit as i32 + 1)) as i16;
            }
            frame
        }
    }

    fn sine(amplitude: f32, frames: usize) -> Vec<i16> {
        (0..frames * FRAME_SAMPLES)
            .map(|i| ((i as f32 / 16_000.0 * 440.0 * std::f32::consts::TAU).sin() * amplitude) as i16)
            .collect()
    }

    fn times(input: &[i16], factor: i16) -> Vec<i16> {
        input.iter().map(|s| s * factor).collect()
    }

    fn run(gain: &mut Gain, input: &[i16]) -> Vec<i16> {
        let mut out = input.to_vec();
        for frame in out.chunks_mut(FRAME_SAMPLES) {
            gain.apply(frame);
        }
        out
    }

    #[test]
    fn zero_db_is_the_identity_even_at_full_scale() {
        let mut noise = Noise(7);
        let mut gain = Gain::from_db(0.0);
        assert_eq!(gain.linear(), 1.0);
        let mut edges = [0i16; FRAME_SAMPLES];
        edges[..4].copy_from_slice(&[i16::MIN, i16::MAX, -1, 1]);
        for input in [edges, noise.frame(i16::MAX), noise.frame(i16::MAX)] {
            let mut frame = input;
            gain.apply(&mut frame);
            assert_eq!(frame, input);
        }
    }

    #[test]
    fn signals_below_the_ceiling_get_exactly_the_linear_gain() {
        assert_eq!(Gain::from_db(20.0).linear(), 10.0);
        let mut noise = Noise(11);
        let mut gain = Gain::from_db(20.0);
        for _ in 0..200 {
            // 2920 x 10 = 29200 < CEILING (29204): the limiter never engages.
            let input = noise.frame(2920);
            let mut frame = input;
            gain.apply(&mut frame);
            assert_eq!(frame.to_vec(), times(&input, 10));
        }
        let mut six = Gain::from_db(6.0);
        let input = noise.frame(10_000);
        let mut frame = input;
        six.apply(&mut frame);
        for (out, x) in frame.iter().zip(input) {
            assert_eq!(*out, (x as f32 * six.linear()).round() as i16, "6 dB is x {}", six.linear());
        }
    }

    #[test]
    fn full_scale_input_never_overflows_and_never_passes_the_ceiling() {
        let mut noise = Noise(3);
        for db in [0.5, 6.0, 20.0, 36.0] {
            let mut gain = Gain::from_db(db);
            let mut square = [i16::MAX; FRAME_SAMPLES];
            for (i, s) in square.iter_mut().enumerate() {
                if i % 2 == 1 {
                    *s = i16::MIN;
                }
            }
            for input in [square, noise.frame(i16::MAX), [i16::MIN; FRAME_SAMPLES], noise.frame(i16::MAX)] {
                let mut frame = input;
                gain.apply(&mut frame);
                let peak = frame.iter().map(|s| (*s as i32).abs()).max().unwrap();
                assert!(peak <= CEILING as i32, "{db} dB gave a peak of {peak}");
            }
        }
    }

    #[test]
    fn a_clap_next_to_the_mic_is_limited_and_speech_after_it_returns_within_150_ms() {
        let mut gain = Gain::from_db(20.0);
        // Quiet speech, a 20 ms full-scale clap, then quiet speech again (1000 x 10 = 10000 < ceiling).
        let before = sine(1000.0, 10);
        let clap = vec![i16::MAX; FRAME_SAMPLES];
        let after = sine(1000.0, 25);
        let out_before = run(&mut gain, &before);
        let out_clap = run(&mut gain, &clap);
        let out_after = run(&mut gain, &after);

        assert_eq!(out_before, times(&before, 10), "no look-ahead: nothing before the clap changes");
        assert!(out_clap.iter().all(|s| (*s as i32) <= CEILING as i32), "the clap is held at the ceiling, not clipped");
        assert!(out_clap.iter().all(|s| (*s as i32) >= CEILING as i32 - 1), "and is not squashed below it");
        let full = times(&after, 10);
        let at = |ms: usize| ms * 16;
        assert!((0..at(50)).all(|i| out_after[i].abs() <= full[i].abs()), "the gain comes back smoothly, not at once");
        assert!(
            (at(20)..at(50)).any(|i| (out_after[i] as i32).abs() < (full[i] as i32).abs() - 100),
            "50 ms after the clap the speech is still turned down"
        );
        assert_eq!(out_after[at(150)..], full[at(150)..], "full gain again by 150 ms");
    }

    /// Plays its frames once, then reports the microphone as unplugged.
    struct Scripted(std::collections::VecDeque<[i16; FRAME_SAMPLES]>);

    impl AudioSource for Scripted {
        fn read_frame(&mut self, frame: &mut [i16; FRAME_SAMPLES]) -> io::Result<()> {
            *frame = self.0.pop_front().ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "microphone unplugged"))?;
            Ok(())
        }
    }

    #[test]
    fn a_wrapped_factory_amplifies_frames_and_starts_each_open_at_full_gain() {
        let quiet = [1000i16; FRAME_SAMPLES];
        let clap = [i16::MAX; FRAME_SAMPLES];
        let opens = std::sync::atomic::AtomicU32::new(0);
        let factory: AudioSourceFactory = Box::new(move || {
            let script = match opens.fetch_add(1, std::sync::atomic::Ordering::SeqCst) {
                0 => vec![clap, quiet],
                _ => vec![quiet],
            };
            Ok(Box::new(Scripted(script.into())) as Box<dyn AudioSource>)
        });
        let factory = with_gain(factory, 20.0);
        let mut frame = [0i16; FRAME_SAMPLES];

        let mut first = factory().unwrap();
        first.read_frame(&mut frame).unwrap();
        assert!(frame.iter().all(|s| *s as i32 == CEILING as i32), "the clap is limited");
        first.read_frame(&mut frame).unwrap();
        assert!(frame[0] < 10_000, "the same source is still recovering from the clap");
        let error = first.read_frame(&mut frame).unwrap_err();
        assert_eq!((error.kind(), error.to_string()), (io::ErrorKind::BrokenPipe, "microphone unplugged".into()));

        let mut reopened = factory().unwrap();
        reopened.read_frame(&mut frame).unwrap();
        assert_eq!(frame, [10_000i16; FRAME_SAMPLES], "a reopened microphone starts at full gain");
    }

    #[test]
    fn open_errors_pass_through() {
        let failing: AudioSourceFactory =
            Box::new(|| Err(io::Error::new(io::ErrorKind::NotFound, "no USB microphone found")));
        let error = with_gain(failing, 20.0)().err().expect("the open error is returned");
        assert_eq!((error.kind(), error.to_string()), (io::ErrorKind::NotFound, "no USB microphone found".into()));
    }
}
```

In `agent/src/recorder/mod.rs` replace the whole file with:

```rust
//! Capture of video (rpicam-vid) and audio (ALSA -> gain -> Ogg Opus).
pub mod audio;
pub mod gain;
pub mod ogg_opus;
pub mod video;
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cargo test --lib recorder::gain 2>&1 | grep -E '^error' | head -3`
Expected: compile errors, starting with
```
error[E0425]: cannot find type `Gain` in this scope
error[E0425]: cannot find value `CEILING` in this scope
error[E0425]: cannot find value `CEILING` in this scope
```
and ending with `error: could not compile `loopi-agent` (lib test) due to 24 previous errors; 1 warning emitted`.

- [ ] **Step 3: Write the implementation**

Insert at the top of `agent/src/recorder/gain.rs`, above `#[cfg(test)]`:

```rust
//! Digital gain with a peak limiter, applied to every captured frame before Opus encoding (spec D19).
//!
//! The USB microphone delivers speech at about -55 dBFS RMS even with its own AGC on, so the agent
//! amplifies it (`capture.audio_gain_db`, default 20 dB). The limiter keeps a loud sound next to the
//! mic (a clap, a door) from clipping: instant attack, so no sample ever passes the -1 dBFS ceiling;
//! release at 200 dB/s, so the full gain is back about 100 ms after a 20 dB reduction.
use std::io;

use crate::recorder::audio::{AudioSource, AudioSourceFactory};
use crate::recorder::ogg_opus::{FRAME_SAMPLES, SAMPLE_RATE};

/// Limiter ceiling: -1 dBFS of i16 full scale (32767 x 10^(-1/20)).
pub const CEILING: f32 = 29_204.0;
const RELEASE_DB_PER_SECOND: f32 = 200.0;

#[derive(Debug, Clone)]
pub struct Gain {
    linear: f32,
    /// Limiter gain (1.0 = not limiting); multiplied by `release` per sample back toward 1.0.
    envelope: f32,
    release: f32,
}

impl Gain {
    /// `db` comes from a validated config (`0.0..=36.0`); 0 dB leaves every sample untouched.
    pub fn from_db(db: f32) -> Self {
        Self {
            linear: 10f32.powf(db / 20.0),
            envelope: 1.0,
            release: 10f32.powf(RELEASE_DB_PER_SECOND / 20.0 / SAMPLE_RATE as f32),
        }
    }

    pub fn linear(&self) -> f32 {
        self.linear
    }

    pub fn apply(&mut self, frame: &mut [i16]) {
        if self.linear == 1.0 {
            return;
        }
        for sample in frame.iter_mut() {
            let x = f32::from(*sample) * self.linear;
            let needed = if x.abs() > CEILING { CEILING / x.abs() } else { 1.0 };
            self.envelope = needed.min((self.envelope * self.release).min(1.0));
            // |x x envelope| <= CEILING < i16::MAX, so the cast never saturates.
            *sample = (x * self.envelope).round() as i16;
        }
    }
}

/// An `AudioSource` whose frames come out amplified and limited.
pub struct GainSource {
    inner: Box<dyn AudioSource>,
    gain: Gain,
}

impl AudioSource for GainSource {
    fn read_frame(&mut self, frame: &mut [i16; FRAME_SAMPLES]) -> io::Result<()> {
        self.inner.read_frame(frame)?;
        self.gain.apply(frame);
        Ok(())
    }
}

/// Wrap `factory` so every source it opens is amplified by `gain_db`; each open starts with a
/// fresh limiter.
pub fn with_gain(factory: AudioSourceFactory, gain_db: f32) -> AudioSourceFactory {
    Box::new(move || {
        let inner = factory()?;
        Ok(Box::new(GainSource { inner, gain: Gain::from_db(gain_db) }) as Box<dyn AudioSource>)
    })
}

```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cargo test --lib recorder::gain`
Expected: `test result: ok. 6 passed; 0 failed; 0 ignored; 0 measured; 205 filtered out`

Then the whole suite: `cargo test 2>&1 | grep 'test result'`
Expected (in this order): `211 passed` (lib), `0 passed` (main), `1 passed` (run_e2e), `4 passed` (run_safety), `0 passed` (doc), all `0 failed`.

The tests were checked against mutants in the prototype: a release of 60 dB/s or 2000 dB/s, an instant release, no limiter at all, and no 0 dB bypass each make at least one of them fail.

- [ ] **Step 5: Commit**

```bash
git -C /home/soul/loopi-audio add agent/src/recorder/gain.rs agent/src/recorder/mod.rs
git -C /home/soul/loopi-audio commit -F - <<'EOF'
feat(agent): digital gain with a -1 dBFS peak limiter for audio frames

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 2: `capture.audio_gain_db`, wiring, selftest, docs and spec D19 — branch `feat/audio-gain`, worktree `/home/soul/loopi-audio`

**Files:**
- Modify: `agent/src/config.rs` (constants after line 29, `CaptureConfig` lines 87–111, `Config::validate` before line 252, tests above `the_privacy_led_defaults_to_the_act_led` at line 384)
- Modify: `agent/src/run.rs` (imports line 21; before the audio thread, line 278)
- Modify: `agent/src/selftest.rs` (imports line 9; new fn before `camera_check` line 69; lines 99, 103, 119; test above `report_lists_every_check_and_a_summary` line 178)
- Modify: `agent/tests/run_e2e.rs` (helper before `fn header` line 65; assertion after the `shortest_audio` assertion line 247)
- Modify: `os/board/loopi/loopi.toml.example:15-17`, `os/README.md` (new section before "## USB port modes"), `os/tests/check-image.sh` (before the final `exit $fail`)
- Modify: `docs/superpowers/specs/2026-10-14-loopi-design.md` (lines 5, 78, 193, 216, 278–280, 413)

**Interfaces:**
- Consumes: Task 1 `recorder::gain::{Gain, with_gain}`.
- Produces:
  - `config::DEFAULT_AUDIO_GAIN_DB: f32 = 20.0`, `config::MAX_AUDIO_GAIN_DB: f32 = 36.0`, `CaptureConfig::audio_gain_db: f32` (serde key `audio_gain_db` under `[capture]`, default 20.0).
  - `run::run` wraps `Deps::audio_factory` with `gain::with_gain(audio_factory, config.capture.audio_gain_db)`; `Deps` and `main.rs` are unchanged.
  - `selftest::mic_level_detail(samples: &[i16], gain_db: f32) -> String` → e.g. `"1 s captured, level -55.0 dBFS, -35.0 dBFS after the +20 dB gain"`; the selftest microphone check prints it.
  - Image checks: 90 (was 88).

All `cargo` commands run in `/home/soul/loopi-audio/agent`.

- [ ] **Step 1: Write the failing config tests**

In `agent/src/config.rs`, inside `mod tests`, insert above `    #[test]\n    fn the_privacy_led_defaults_to_the_act_led() {`:

```rust
    #[test]
    fn audio_gain_defaults_to_20_db_and_takes_whole_or_fractional_db() {
        assert_eq!(Config::parse(MINIMAL).unwrap().capture.audio_gain_db, 20.0);
        for (value, db) in [("0", 0.0), ("36", 36.0), ("12.5", 12.5), ("0.0", 0.0)] {
            let c = Config::parse(&format!("{MINIMAL}[capture]\naudio_gain_db = {value}\n")).unwrap();
            assert_eq!(c.capture.audio_gain_db, db, "audio_gain_db = {value}");
            assert_eq!(Config::parse(&toml::to_string(&c).unwrap()).unwrap(), c, "survives the import round trip");
        }
    }

    #[test]
    fn audio_gain_outside_0_to_36_db_is_rejected() {
        for value in ["-1", "-0.5", "36.5", "100", "nan", "inf", "-inf"] {
            let err = Config::parse(&format!("{MINIMAL}[capture]\naudio_gain_db = {value}\n")).unwrap_err().to_string();
            assert!(err.contains("capture.audio_gain_db must be 0..=36 dB"), "{value}: {err}");
        }
        assert!(Config::parse(&format!("{MINIMAL}[capture]\naudio_gain_db = \"20\"\n")).is_err(), "a string is not a number");
    }

```

- [ ] **Step 2: Run them to verify they fail**

Run: `cargo test --lib config:: 2>&1 | grep -E '^error'`
Expected:
```
error[E0609]: no field `audio_gain_db` on type `config::CaptureConfig`
error[E0609]: no field `audio_gain_db` on type `config::CaptureConfig`
error: could not compile `loopi-agent` (lib test) due to 2 previous errors
```

- [ ] **Step 3: Add the setting and its validation**

In `agent/src/config.rs`, after the line `const _: () = assert!(SEGMENT_SECONDS_MAX <= CONTRACT_MAX_SEGMENT_SECONDS);` add:

```rust
/// `capture.audio_gain_db`: digital microphone gain before encoding (spec D19). The USB mic records speech at
/// about -55 dBFS RMS even with its own AGC on.
pub const DEFAULT_AUDIO_GAIN_DB: f32 = 20.0;
pub const MAX_AUDIO_GAIN_DB: f32 = 36.0;
```

In `pub struct CaptureConfig`, between `pub audio_device: String,` and `pub camera_cmd: String,` add:

```rust
    pub audio_gain_db: f32,
```

In `impl Default for CaptureConfig`, between `audio_device: "auto".into(),` and `camera_cmd: "rpicam-vid".into(),` add:

```rust
            audio_gain_db: DEFAULT_AUDIO_GAIN_DB,
```

In `Config::validate`, directly above `        if c.audio_device.trim().is_empty() || c.camera_cmd.trim().is_empty() {` add:

```rust
        // NaN fails `contains` too.
        if !(0.0..=MAX_AUDIO_GAIN_DB).contains(&c.audio_gain_db) {
            return invalid(format!("capture.audio_gain_db must be 0..={MAX_AUDIO_GAIN_DB} dB (got {})", c.audio_gain_db));
        }
```

(`CaptureConfig` derives `PartialEq` only, not `Eq`, so an `f32` field is fine; `tests/run_e2e.rs` builds it with `..CaptureConfig::default()` and needs no change.)

- [ ] **Step 4: Run the config tests to verify they pass**

Run: `cargo test --lib config::`
Expected: `test result: ok. 19 passed; 0 failed; 0 ignored; 0 measured; 194 filtered out`

- [ ] **Step 5: Write the failing end-to-end check that the gain reaches the uploads**

In `agent/tests/run_e2e.rs`, insert above `fn header<'a>(request: &'a Request, name: &str) -> Option<&'a str> {`:

```rust
/// Largest |sample| of an uploaded Ogg Opus segment, decoded at 16 kHz.
fn decoded_peak(ogg: &[u8]) -> i32 {
    let mut reader = ogg::reading::PacketReader::new(std::io::Cursor::new(ogg));
    let mut decoder = opus::Decoder::new(16_000, opus::Channels::Mono).unwrap();
    let mut pcm = [0i16; 320];
    let mut peak = 0;
    // The first two packets are OpusHead and OpusTags.
    while let Some(packet) = reader.read_packet().unwrap() {
        if packet.data.starts_with(b"Opus") {
            continue;
        }
        let samples = decoder.decode(&packet.data, &mut pcm, false).unwrap();
        peak = pcm[..samples].iter().map(|s| i32::from(*s).abs()).fold(peak, i32::max);
    }
    peak
}

```

In `records_uploads_and_obeys_a_remote_pause`, directly after the line
`    assert!(shortest_audio < AUDIO_SEGMENT, "the pause cut a partial audio segment ({shortest_audio:?}) that was uploaded");` add:

```rust
    let loudest_audio = final_requests
        .iter()
        .filter(|r| r.method.as_str() == "PUT" && header(r, "x-loopi-kind") == Some("audio"))
        .map(|r| decoded_peak(&r.body))
        .max()
        .expect("audio was uploaded");
    // The tone "microphone" peaks at 8000 (-12 dBFS); the default 20 dB gain drives it into the -1 dBFS limiter.
    assert!(loudest_audio > 20_000, "the configured gain reached the uploaded audio (decoded peak {loudest_audio})");
```

(`opus` and `ogg` are regular dependencies of the crate, so integration tests can use them.)

- [ ] **Step 6: Run it to verify it fails**

Run: `cargo test --test run_e2e 2>&1 | grep -E 'panicked|decoded peak|test result'`
Expected:
```
thread 'records_uploads_and_obeys_a_remote_pause' (…) panicked at tests/run_e2e.rs:272:5:
the configured gain reached the uploaded audio (decoded peak 8816)
test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
```
(The decoded peak of the ungained tone varies by a few tens between runs: 8809 and 8816 were seen.)

- [ ] **Step 7: Wrap the audio factory with the configured gain**

In `agent/src/run.rs`, after `use crate::recorder::audio::{self, AudioOptions, AudioSourceFactory};` add:

```rust
use crate::recorder::gain;
```

In `pub async fn run`, directly above `    let audio_thread = {` add:

```rust
    // Every frame is amplified before encoding (spec D19); the raw ALSA level is far too quiet.
    let audio_factory = gain::with_gain(audio_factory, config.capture.audio_gain_db);
```

The factory is still called inside `run_audio_blocking` after `recording_audio` is raised, so the LED rule ("solid before the device opens") is untouched.

- [ ] **Step 8: Run it to verify it passes**

Run: `cargo test --test run_e2e 2>&1 | grep 'test result'`
Expected: `test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out` (the prototype's decoded peak with the gain was 31600: the limiter's 29 204 plus Opus overshoot).

- [ ] **Step 9: Write the failing selftest test**

In `agent/src/selftest.rs`, inside `mod tests`, insert above `    #[test]\n    fn report_lists_every_check_and_a_summary() {`:

```rust
    #[test]
    fn microphone_level_is_reported_before_and_after_the_gain() {
        // 58 / 32768 is the -55 dBFS the USB mic gives for speech in a room.
        assert_eq!(
            mic_level_detail(&[58; 16_000], 20.0),
            "1 s captured, level -55.0 dBFS, -35.0 dBFS after the +20 dB gain"
        );
        assert_eq!(mic_level_detail(&[58; 16_000], 0.0), "1 s captured, level -55.0 dBFS, -55.0 dBFS after the +0 dB gain");
        assert_eq!(
            mic_level_detail(&[0; 16_000], 12.5),
            "1 s captured, level -120.0 dBFS, -120.0 dBFS after the +12.5 dB gain"
        );
        // A clap right at the mic: the limiter holds the amplified level at its -1 dBFS ceiling.
        assert_eq!(
            mic_level_detail(&[i16::MAX; 16_000], 20.0),
            "1 s captured, level -0.0 dBFS, -1.0 dBFS after the +20 dB gain"
        );
    }

```

- [ ] **Step 10: Run it to verify it fails**

Run: `cargo test --lib selftest:: 2>&1 | grep -E '^error' | head -1`
Expected: `error[E0425]: cannot find function `mic_level_detail` in this scope`

- [ ] **Step 11: Report the level before and after the gain**

In `agent/src/selftest.rs`, after `use crate::pairing::load_identity;` add:

```rust
use crate::recorder::gain::Gain;
```

Directly above `async fn camera_check(camera_cmd: &str) -> Check {` add:

```rust
/// The captured level, and the level the recorder encodes once `gain_db` is applied (spec D19).
pub fn mic_level_detail(samples: &[i16], gain_db: f32) -> String {
    let mut amplified = samples.to_vec();
    Gain::from_db(gain_db).apply(&mut amplified);
    format!(
        "1 s captured, level {:.1} dBFS, {:.1} dBFS after the +{gain_db} dB gain",
        rms_dbfs(samples),
        rms_dbfs(&amplified)
    )
}

```

In the Linux `mic_check`, replace
`    with_privacy_led(led.as_mut(), || capture_one_second(&config.capture.audio_device))`
with
```rust
    with_privacy_led(led.as_mut(), || capture_one_second(&config.capture.audio_device, config.capture.audio_gain_db))
```

Replace `fn capture_one_second(device: &str) -> Check {` with
```rust
fn capture_one_second(device: &str, gain_db: f32) -> Check {
```

and its last line `    Check::pass("microphone", format!("1 s captured, level {:.1} dBFS", rms_dbfs(&samples)))` with
```rust
    Check::pass("microphone", mic_level_detail(&samples, gain_db))
```

- [ ] **Step 12: Run the selftest tests to verify they pass**

Run: `cargo test --lib selftest::`
Expected: `test result: ok. 11 passed; 0 failed; 0 ignored; 0 measured; 203 filtered out` (on Linux this includes `microphone_check_uses_the_configured_led`, which opens the non-existent PCM `loopi-test-no-such-pcm` and so also compiles the changed Linux-only `capture_one_second`).

- [ ] **Step 13: Document the setting**

In `os/board/loopi/loopi.toml.example`, replace
```toml
[capture]
video_fps = 2
audio_enabled = true
```
with
```toml
[capture]
video_fps = 2
audio_enabled = true
audio_gain_db = 20.0              # microphone gain before encoding, 0-36 dB (the USB mic records speech very quietly)
```

In `os/README.md`, insert directly above `## USB port modes (one per boot)`:

```markdown
## Microphone level
The USB microphone records speech very quietly (about -55 dBFS RMS, measured on two days of recordings with its own
automatic gain control on; raising its mixer volume adds only 2-3 dB, and the image leaves the mixer at its power-on
settings). So the agent amplifies every captured frame by `[capture]` `audio_gain_db` in `loopi.toml` (default 20 dB,
0-36) before encoding, through a limiter that holds loud sounds at -1 dBFS instead of letting them clip (spec D19).
`loopi-agent selftest` prints the level of one second before and after the gain. If speech in the room still reads far
below -30 dBFS after the gain, raise it; if the limiter flattens normal speech, lower it. The brain normalises the level
again for transcription and playback, so the raw files only need to be loud enough to keep speech above the encoder's
noise.

```

In `os/tests/check-image.sh`, insert directly above the final line `exit $fail` (after the `usbmode_fails_closed` check and its blank line):

```bash
# --- Audio level (spec D19): the agent amplifies the USB microphone before encoding (capture.audio_gain_db). The agent is
# a local Buildroot package, rsynced only on a fresh build: an image made without `loopi-agent-dirclean` silently ships the
# previous agent, and this check catches that.
agent_binary_amplifies_the_microphone() {
  local text; text="$(rootfs_cat usr/bin/loopi-agent | strings -a)" || return 1
  grep -F 'capture.audio_gain_db must be 0..=' <<<"$text"
}
example_sets_the_microphone_gain() { boot_cat loopi.toml.example | grep -E '^audio_gain_db = 20\.0 '; }
check "the shipped agent amplifies the microphone before encoding (capture.audio_gain_db, spec D19)" agent_binary_amplifies_the_microphone
check "loopi.toml.example shows the microphone gain (audio_gain_db = 20.0)" example_sets_the_microphone_gain

```

Run: `bash -n /home/soul/loopi-audio/os/tests/check-image.sh && grep -c '^check "' /home/soul/loopi-audio/os/tests/check-image.sh`
Expected: `90`

(In the prototype both new functions were run against squashfs images made with `mksquashfs`, with `boot_cat` replaced by `cat` because mtools is only in the builder container: the host build of this branch's agent and the new example pass; the previous ARM agent (`/home/soul/loopi/os/out/loopi-agent`, 7021705) and the previous example fail.)

- [ ] **Step 14: Amend the spec (D19)**

In `docs/superpowers/specs/2026-10-14-loopi-design.md`:

Line 5, at the end of the "Sửa đổi" line, replace `đèn báo ghi hình là LED ACT có sẵn trên board thay cho LED đỏ gắn ngoài (D18)` with
```markdown
đèn báo ghi hình là LED ACT có sẵn trên board thay cho LED đỏ gắn ngoài (D18); 2026-10-15: agent khuếch đại âm thanh (có bộ giới hạn đỉnh) trước khi encode, brain chuẩn hoá mức âm thanh trước ASR và khi phát lại (D19)
```

After the D18 row of the §3 table (line 78) add the row:
```markdown
| D19 | Mức âm thanh | USB mic thu tiếng nói rất nhỏ: đo trên hơn 1 300 đoạn ghi trong 2 ngày, RMS khoảng −55 dBFS cả ngày lẫn đêm, kể cả khi bật AGC của mic; tăng volume mixer chỉ được thêm 2–3 dB. Vì vậy agent khuếch đại số mỗi khung âm thanh trước khi encode Opus, qua một bộ giới hạn đỉnh −1 dBFS (attack tức thì, nhả khoảng 100 ms) để tiếng động lớn sát mic không bị vỡ. Mức khuếch đại là `capture.audio_gain_db` trong `loopi.toml` (mặc định 20 dB, cho phép 0–36 dB), không chỉnh từ xa; protocol v1 giữ nguyên. Brain chuẩn hoá mức âm thanh trước VAD/ASR và cho bản nghe trên web: đưa phân vị 99,9 của biên độ lên −6 dBFS (phân vị chứ không phải đỉnh, vì có những mẫu nhiễu lẻ chạm 0 dBFS), tối đa +30 dB, không bao giờ giảm, và không nâng nền nhiễu của phòng lên quá −40 dBFS. File thô giữ nguyên như lúc thu |
```

In the §6.6 example, after `audio_device    = "auto"        # thiết bị ALSA; "auto" = thiết bị USB capture đầu tiên` add:
```toml
audio_gain_db   = 20.0          # khuếch đại số trước khi encode, 0–36 dB (D19)
```

In the §7.2 `recorder::audio` row, replace `Encode Opus ~24kbps thành file Ogg 60s căn theo phút đồng hồ.` with
```markdown
Mỗi khung được khuếch đại `audio_gain_db` (mặc định 20 dB) qua bộ giới hạn đỉnh −1 dBFS rồi mới encode (D19). Encode Opus ~24kbps thành file Ogg 60s căn theo phút đồng hồ.
```

In §9.2, replace the three "Mỗi đoạn audio" steps with:
```markdown
1. Chuẩn hoá mức âm thanh (D19): phân vị 99,9 của biên độ lên −6 dBFS, tối đa +30 dB, không giảm, nền nhiễu không quá −40 dBFS. Chỉ bản trong bộ nhớ được chuẩn hoá, file thô không đổi.
2. Silero VAD tìm các đoạn có tiếng nói.
3. faster-whisper large-v3-turbo (int8, CPU) tự nhận ngôn ngữ cho từng đoạn và trả transcript kèm timestamp.
4. Xoá audio thô sau khi có transcript (mặc định; chỉnh được).
```

In §13 (Agent bullet), replace `kiểm USB mic có mặt và mức âm thanh,` with `kiểm USB mic có mặt và mức âm thanh (trước và sau khuếch đại),`.

Run: `git -C /home/soul/loopi-audio diff --stat -- docs/`
Expected: `1 file changed, 9 insertions(+), 6 deletions(-)`

- [ ] **Step 15: Run the whole agent verification**

```bash
cargo test 2>&1 | grep 'test result'
cargo llvm-cov --ignore-filename-regex 'main.rs' 2>&1 | grep -E '^TOTAL|^recorder/gain.rs|^config.rs|^selftest.rs|^run.rs'
cargo tree -i aws-lc-rs; echo "rc=$?"
cargo build --release 2>&1 | tail -1 && strings -a target/release/loopi-agent | grep -c -F 'capture.audio_gain_db must be 0..='
cargo clippy --all-targets 2>&1 | grep -E '^warning|^error' | sort | uniq -c
git -C /home/soul/loopi-audio diff --stat 7021705 -- agent/Cargo.toml agent/Cargo.lock docs/protocol
```
Expected:
- `214 passed` (lib), `0 passed` (main), `1 passed` (run_e2e), `4 passed` (run_safety), `0 passed` (doc), all `0 failed`.
- Lines column: `config.rs … 99.20%`, `recorder/gain.rs … 100.00%`, `run.rs … 93.99%`, `selftest.rs … 91.04%`, `TOTAL … 95.25%`.
- `error: package ID specification `aws-lc-rs` did not match any packages` and `rc=101` (absent = good).
- `Finished `release` profile …` then `1`.
- Only the two existing `used `assert_eq!` with a literal bool` warnings (in `control.rs`) and their summary line; nothing from this change.
- No output from the last command (no new crates, contract untouched).

- [ ] **Step 16: Commit**

```bash
cd /home/soul/loopi-audio
git add agent/src/config.rs agent/src/run.rs agent/src/selftest.rs agent/tests/run_e2e.rs \
  os/board/loopi/loopi.toml.example os/README.md os/tests/check-image.sh docs/superpowers/specs/2026-10-14-loopi-design.md
git commit -F - <<'EOF'
feat(agent): amplify the microphone by capture.audio_gain_db before encoding (spec D19)

The USB mic records speech at about -55 dBFS RMS even with its own AGC on. Every captured frame now goes
through the -1 dBFS limited gain (default 20 dB, 0-36); selftest prints the level before and after it,
and two image checks catch an image that still ships the previous agent.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

---

### Task 3 (P2a Task 10a): Shared level helper and ASR normalisation — branch `feat/phase2`, worktree `/home/soul/loopi-p2`

This task and Task 4 slot into the P2a plan between its Tasks 10 and 11; they are executed from this plan. The P2a Global Constraints apply in full.

**Files:**
- Create: `brain/src/loopi_brain/audio_level.py`, `brain/tests/test_audio_level.py`
- Modify: `brain/src/loopi_brain/config.py` (import line 5, field after line 32), `brain/src/loopi_brain/pipeline/asr.py` (docstring line 1, import line 19, new `normalise` before `speech_regions` line 173, `transcribe_file` lines 226–228, `_transcribe` line 249), `brain/tests/helpers.py:358-367` (`FakeTranscriber`), `brain/tests/test_asr.py` (import line 19; new tests after `test_silence_has_no_speech_regions`), `brain/tests/test_config.py`, `deploy/README.md` ("Pipeline (P2a)" section)

**Interfaces:**
- Consumes: `asr.load_audio(path) -> np.ndarray` (16 kHz mono float32, PyAV), `asr.speech_regions`, `asr.transcribe_audio(ctx, item_id) -> int`, `PipelineContext.settings`, test helpers `insert_item`, `fetch_one`, `fetch_all`, `make_opus(path, source, seconds)`, `make_ctx`, `FakeTranscriber`, fixtures `pool`, `pki_settings`, `jobs_open`, `dev` (in `test_asr.py`).
- Produces (Task 4 relies on these):
  - `loopi_brain.audio_level`: constants `SAMPLE_RATE = 16_000`, `FRAME = 320`, `TARGET_PEAK = 0.5`, `PEAK_PERCENTILE = 99.9`, `MAX_GAIN_DB = 30.0`, `NOISE_FLOOR_MAX = 0.01`, `FLOOR_PERCENTILE = 10.0`; `dbfs(value: float) -> float` (−120.0 at or below 1e-6); `noise_floor(samples: np.ndarray) -> float`; `level_gain(samples: np.ndarray, target_peak: float = TARGET_PEAK, max_gain_db: float = MAX_GAIN_DB) -> float`.
  - `asr.normalise(audio: np.ndarray, max_gain_db: float = audio_level.MAX_GAIN_DB) -> np.ndarray` (float32, clipped to [-1, 1]; the same array object when the gain is 1).
  - `asr.transcribe_file(path, transcriber, max_gain_db: float = audio_level.MAX_GAIN_DB)`.
  - `Settings.asr_max_gain_db: float = 30.0` (`LOOPI_ASR_MAX_GAIN_DB`, `ge=0.0, le=60.0`; NaN is rejected).
  - `FakeTranscriber.peaks: list[float]` (largest |sample| of each chunk).

All commands run in `/home/soul/loopi-p2/brain`.

- [ ] **Step 1: Write the failing helper tests**

Create `brain/tests/test_audio_level.py`:

```python
import numpy as np
import pytest

from loopi_brain import audio_level
from loopi_brain.audio_level import dbfs, level_gain, noise_floor

SR = audio_level.SAMPLE_RATE


def speech_like(peak_db: float, floor_db: float, seconds: float = 10, seed: int = 1) -> np.ndarray:
    """Half-second 440 Hz bursts peaking at ``peak_db`` every other half second, over white noise with an
    RMS of ``floor_db`` (both dBFS), as 16 kHz float32."""
    t = np.arange(int(seconds * SR)) / SR
    bursts = np.sin(2 * np.pi * 440 * t) * (np.floor(t * 2) % 2 == 0) * 10 ** (peak_db / 20)
    noise = np.random.default_rng(seed).normal(0.0, 10 ** (floor_db / 20), t.size)
    return (bursts + noise).astype(np.float32)


def test_quiet_speech_is_raised_to_minus_6_dbfs_at_the_99_9th_percentile():
    quiet = speech_like(-30, -70)
    gain = level_gain(quiet)
    assert gain == pytest.approx(0.5 / np.percentile(np.abs(quiet), 99.9))
    assert dbfs(gain) == pytest.approx(24.0, abs=0.2)
    assert np.percentile(np.abs(quiet * gain), 99.9) == pytest.approx(0.5)


def test_loud_audio_is_never_attenuated():
    assert level_gain(speech_like(-3, -50)) == 1.0
    assert level_gain(np.full(SR, 0.9, dtype=np.float32)) == 1.0


@pytest.mark.parametrize("samples", [np.zeros(SR, dtype=np.float32), np.zeros(0, dtype=np.float32)])
def test_silence_and_empty_input_get_no_gain(samples):
    with np.errstate(all="raise"):  # no division by zero, no log of zero
        assert level_gain(samples) == 1.0
        assert noise_floor(samples) == 0.0


def test_less_than_one_frame_has_no_noise_floor_but_still_a_bounded_gain():
    assert noise_floor(np.full(100, 0.001, dtype=np.float32)) == 0.0
    assert level_gain(np.full(100, 0.001, dtype=np.float32)) == pytest.approx(10 ** (30 / 20))


def test_the_gain_stops_at_max_gain_db():
    whisper = speech_like(-60, -100)  # wants +54 dB
    assert level_gain(whisper) == pytest.approx(10 ** (30 / 20))
    assert level_gain(whisper, max_gain_db=12.0) == pytest.approx(10 ** (12 / 20))
    assert level_gain(whisper, max_gain_db=0.0) == 1.0
    assert level_gain(whisper, max_gain_db=-5.0) == 1.0


def test_isolated_one_sample_glitches_do_not_spoil_the_gain():
    # Real segments carry single-sample glitches at 0 dBFS; a peak-based gain would be 1.0 here.
    speech = speech_like(-30, -75, seconds=60)
    glitched = speech.copy()
    glitched[::SR] = 1.0  # one full-scale sample per second
    assert np.abs(glitched).max() == 1.0
    assert level_gain(glitched) == pytest.approx(level_gain(speech), rel=0.01)
    assert dbfs(level_gain(glitched)) == pytest.approx(24.0, abs=0.2)


def test_a_noise_only_night_segment_stays_a_soft_hiss():
    # The USB mic's room noise as measured on 1 336 segments: about -55 dBFS RMS, day and night.
    night = np.random.default_rng(3).normal(0.0, 10 ** (-55 / 20), 60 * SR).astype(np.float32)
    gain = level_gain(night)
    assert 1.0 < gain < level_gain(speech_like(-40, -75, seconds=60)), "noise gets less gain than speech"
    assert dbfs(noise_floor(night * gain)) == pytest.approx(-40.0, abs=0.01), "the hiss stops at -40 dBFS"
    # After the device's own +20 dB the same room is already at -35 dBFS: no further gain.
    assert level_gain(night * np.float32(10.0)) == 1.0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_audio_level.py -q 2>&1 | grep -E '^E   ImportError|error in'`
Expected:
```
E   ImportError: cannot import name 'audio_level' from 'loopi_brain' (…/brain/src/loopi_brain/__init__.py)
1 warning, 1 error
```

- [ ] **Step 3: Write the helper**

Create `brain/src/loopi_brain/audio_level.py`:

```python
"""How loud the brain makes the audio it listens to and plays back (spec D19). Raw files stay as captured.

The device's USB microphone records speech far below full scale, so the brain raises each file by one
gain: the 99.9th percentile of |x| goes to -6 dBFS (a percentile, not the peak, because isolated
one-sample glitches reach 0 dBFS in real recordings), the gain never attenuates and stops at
``max_gain_db``, and it never lifts the room's noise floor (the 10th percentile of the 20 ms frame RMS)
above -40 dBFS, so a quiet night segment stays a soft hiss instead of being raised to speech loudness.
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16_000
FRAME = SAMPLE_RATE // 50  # 20 ms
TARGET_PEAK = 0.5  # -6 dBFS
PEAK_PERCENTILE = 99.9
MAX_GAIN_DB = 30.0
NOISE_FLOOR_MAX = 0.01  # -40 dBFS
FLOOR_PERCENTILE = 10.0


def dbfs(value: float) -> float:
    """``value`` (a linear amplitude, 1.0 = full scale) in dBFS; -120 for silence."""
    return 20 * float(np.log10(value)) if value > 1e-6 else -120.0


def noise_floor(samples: np.ndarray) -> float:
    """RMS of the quietest 20 ms frames (their 10th percentile); 0.0 for less than one frame."""
    usable = samples[: samples.size // FRAME * FRAME].astype(np.float64)
    if usable.size == 0:
        return 0.0
    rms = np.sqrt(np.mean(usable.reshape(-1, FRAME) ** 2, axis=1))
    return float(np.percentile(rms, FLOOR_PERCENTILE))


def level_gain(samples: np.ndarray, target_peak: float = TARGET_PEAK, max_gain_db: float = MAX_GAIN_DB) -> float:
    """Linear gain for 16 kHz mono float samples in [-1, 1]: brings the 99.9th percentile of |x| to
    ``target_peak``, within [1, 10 ** (max_gain_db / 20)] and the noise-floor limit; 1.0 for empty or
    silent input (no division by zero)."""
    if samples.size == 0:
        return 1.0
    peak = float(np.percentile(np.abs(samples), PEAK_PERCENTILE))
    if not peak > 0.0:  # digital silence (or NaN)
        return 1.0
    limit = 10 ** (max(max_gain_db, 0.0) / 20)
    floor = noise_floor(samples)
    if floor > 0.0:
        limit = min(limit, NOISE_FLOOR_MAX / floor)
    return max(1.0, min(target_peak / peak, limit))
```

- [ ] **Step 4: Run the helper tests to verify they pass**

Run: `uv run pytest tests/test_audio_level.py -q`
Expected: `8 passed, 1 warning`

- [ ] **Step 5: Write the failing ASR and settings tests**

In `brain/tests/helpers.py`, replace the whole `FakeTranscriber` class with:

```python
class FakeTranscriber:
    """Stands in for Whisper: returns the scripted RegionResults in order, cycling. Records each chunk's
    length (``chunks``) and largest |sample| (``peaks``)."""

    def __init__(self, *results):
        self.results = list(results)
        self.chunks: list[int] = []
        self.peaks: list[float] = []

    def transcribe(self, chunk):
        self.chunks.append(len(chunk))
        self.peaks.append(float(abs(chunk).max()) if len(chunk) else 0.0)
        return self.results[(len(self.chunks) - 1) % len(self.results)]
```

In `brain/tests/test_asr.py`, add `from loopi_brain import audio_level` directly above `from loopi_brain.config import Settings`, and insert after `test_silence_has_no_speech_regions` (above `async def test_speech_regions_become_segments_and_moments`):

```python
QUIET_SPEECH = "aevalsrc=0.02*sin(2*PI*440*t)*mod(floor(2*t)\\,2):s=16000"  # -34 dBFS bursts, silence between
LOUD_TONE = "aevalsrc=0.7*sin(2*PI*440*t):s=16000"  # -3 dBFS
ROOM_NOISE = "anoisesrc=colour=white:amplitude=0.003:sample_rate=16000:seed=1"  # -55 dBFS RMS, like the USB mic


def _p999(audio: np.ndarray) -> float:
    return float(np.percentile(np.abs(audio), 99.9))


async def _vad_input(pool, settings, device_id, source, monkeypatch, seconds=4):
    """Transcribe an audio item made from the lavfi ``source``; return (the decoded file, what VAD got,
    the FakeTranscriber)."""
    item_id = await insert_item(pool, device_id, kind="audio", seconds=seconds)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    path = make_opus(settings.media_root / row["raw_path"], source, seconds)
    seen = []

    def regions(audio):
        seen.append(audio)
        return [Region(0.0, 2.0)]

    monkeypatch.setattr(asr, "speech_regions", regions)
    fake = FakeTranscriber(RegionResult("vi", 0.9, [seg("Mai mình đi chợ")]))
    assert await asr.transcribe_audio(make_ctx(pool, settings, transcriber=fake), item_id) == 1
    return asr.load_audio(path), seen[0], fake


async def test_quiet_speech_reaches_vad_and_whisper_normalised(pool, pki_settings, dev, jobs_open, monkeypatch):
    raw, heard, fake = await _vad_input(pool, pki_settings, dev["device_id"], QUIET_SPEECH, monkeypatch)
    assert _p999(raw) == pytest.approx(0.02, rel=0.1)
    assert heard.dtype == np.float32 and len(heard) == len(raw)
    assert _p999(heard) == pytest.approx(0.5, rel=0.01), "the loud end is brought to -6 dBFS"
    assert np.abs(heard).max() <= 1.0
    assert fake.peaks[0] == pytest.approx(float(np.abs(heard[: 2 * SR]).max()))
    assert fake.peaks[0] > 10 * float(np.abs(raw[: 2 * SR]).max()), "Whisper hears the normalised audio"


@pytest.mark.parametrize("source", [LOUD_TONE, "anullsrc=r=16000:cl=mono"], ids=["loud", "silence"])
async def test_loud_audio_and_silence_reach_vad_unchanged(pool, pki_settings, dev, jobs_open, monkeypatch, source):
    raw, heard, _fake = await _vad_input(pool, pki_settings, dev["device_id"], source, monkeypatch)
    assert np.array_equal(heard, raw)


async def test_the_asr_gain_ceiling_is_a_setting(pool, pki_settings, dev, jobs_open, monkeypatch):
    capped = pki_settings.model_copy(update={"asr_max_gain_db": 6.0})
    raw, heard, _fake = await _vad_input(pool, capped, dev["device_id"], QUIET_SPEECH, monkeypatch)
    assert _p999(heard) == pytest.approx(_p999(raw) * 10 ** (6 / 20), rel=0.01)


async def test_room_noise_stays_without_speech_after_normalisation(pool, pki_settings, dev, jobs_open):
    # The real Silero VAD on the mic's -55 dBFS room noise, raised by the level gain (+15 dB here).
    item_id = await insert_item(pool, dev["device_id"], kind="audio", seconds=10)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    path = make_opus(pki_settings.media_root / row["raw_path"], ROOM_NOISE, 10)
    assert audio_level.dbfs(audio_level.level_gain(asr.load_audio(path))) > 10, "the noise is raised before VAD"
    fake = FakeTranscriber()
    assert await asr.transcribe_audio(make_ctx(pool, pki_settings, transcriber=fake), item_id) == 0
    assert fake.chunks == [], "no speech region reaches Whisper"
    assert await fetch_all(pool, "SELECT id FROM transcript_segment") == []
    assert (await fetch_one(pool, "SELECT analysis_state FROM item WHERE id = %s", (item_id,)))["analysis_state"] == "done"


```

In `brain/tests/test_config.py`, in `test_pipeline_defaults` add after `    assert s.whisper_cpu_threads == 4`:

```python
    assert s.asr_max_gain_db == 30.0
```

and append to the file:

```python


def test_asr_gain_ceiling_from_env(monkeypatch):
    import pydantic
    import pytest

    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("LOOPI_ASR_MAX_GAIN_DB", "12")
    assert Settings().asr_max_gain_db == 12.0
    for bad in ("-1", "61", "nan"):
        monkeypatch.setenv("LOOPI_ASR_MAX_GAIN_DB", bad)
        with pytest.raises(pydantic.ValidationError, match="asr_max_gain_db"):
            Settings()
```

- [ ] **Step 6: Run them to verify they fail**

Run: `uv run pytest tests/test_asr.py tests/test_config.py -q 2>&1 | grep -E '^FAILED|passed|failed'`
Expected:
```
FAILED tests/test_asr.py::test_quiet_speech_reaches_vad_and_whisper_normalised
FAILED tests/test_asr.py::test_the_asr_gain_ceiling_is_a_setting - assert 0.0...
FAILED tests/test_config.py::test_pipeline_defaults - AttributeError: 'Settin...
FAILED tests/test_config.py::test_asr_gain_ceiling_from_env - AttributeError:...
4 failed, 44 passed, 1 skipped, 1 warning
```
(The quiet test fails with `assert 0.020172119140625 == 0.5 ± 0.005`: VAD still gets the raw level. `test_room_noise_stays_without_speech_after_normalisation` and the loud/silence cases already pass: they pin behaviour the change must keep.)

- [ ] **Step 7: Add the setting and normalise before VAD and Whisper**

In `brain/src/loopi_brain/config.py`, add `from pydantic import Field` directly above `from pydantic_settings import BaseSettings, SettingsConfigDict`, and after `    whisper_cpu_threads: int = 4` add:

```python
    asr_max_gain_db: float = Field(default=30.0, ge=0.0, le=60.0)  # level gain before VAD/Whisper (spec D19)
```

In `brain/src/loopi_brain/pipeline/asr.py`:

Replace the first docstring line `"""transcribe_audio: Silero VAD + faster-whisper per speech region (spec §9.2 audio steps 1-2).` with
```python
"""transcribe_audio: level normalisation, then Silero VAD + faster-whisper per speech region (spec §9.2, D19).
```

Replace `from .. import devices` with `from .. import audio_level, devices`.

Directly above `def speech_regions(audio: np.ndarray) -> list[Region]:` add:

```python
def normalise(audio: np.ndarray, max_gain_db: float = audio_level.MAX_GAIN_DB) -> np.ndarray:
    """``audio`` raised by the shared level gain (spec D19) and clipped to [-1, 1]; unchanged when the
    gain is 1 (loud enough already, or silence)."""
    gain = audio_level.level_gain(audio, max_gain_db=max_gain_db)
    if gain == 1.0:
        return audio
    return np.clip(audio * np.float32(gain), -1.0, 1.0)


```

Replace the first three lines of `transcribe_file`:
```python
def transcribe_file(path: Path, transcriber: Transcriber) -> list[tuple[Region, RegionResult]]:
    """Blocking: decode, find speech, transcribe each region. Segment times stay region-relative."""
    audio = load_audio(path)
```
with
```python
def transcribe_file(
    path: Path, transcriber: Transcriber, max_gain_db: float = audio_level.MAX_GAIN_DB
) -> list[tuple[Region, RegionResult]]:
    """Blocking: decode, normalise the level, find speech, transcribe each region. Segment times stay
    region-relative."""
    audio = normalise(load_audio(path), max_gain_db)
```

In `_transcribe`, replace `    results = await anyio.to_thread.run_sync(transcribe_file, path, ctx.transcriber)` with
```python
    results = await anyio.to_thread.run_sync(transcribe_file, path, ctx.transcriber, ctx.settings.asr_max_gain_db)
```

In `deploy/README.md`, in "## Pipeline (P2a)", directly after the bullet `- Model server down: captions and embeddings wait and resume by themselves; uploads are never affected.` add:

```markdown
- Audio level (spec D19): the device's microphone records speech very quietly, so the worker raises each recording
  before VAD and Whisper (the 99.9th percentile of the samples to -6 dBFS, at most `LOOPI_ASR_MAX_GAIN_DB`, default
  30 dB, and never a quiet room's hiss above -40 dBFS). Only the decoded copy in memory changes; raw files stay as recorded.
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_asr.py tests/test_config.py tests/test_audio_level.py -q`
Expected: `56 passed, 1 skipped, 1 warning` (the skip is the opt-in real-Whisper test).

Then the whole suite with coverage: `uv run pytest --cov=loopi_brain -q 2>&1 | grep -E 'audio_level|config.py|asr.py|TOTAL|passed'`
Expected:
```
src/loopi_brain/audio_level.py              28      0   100%
src/loopi_brain/config.py                   34      0   100%
src/loopi_brain/pipeline/asr.py            190      2    99%
src/loopi_brain/providers/config.py         62      0   100%
TOTAL                                     1930     33    98%
326 passed, 1 skipped, 1 warning
```

- [ ] **Step 9: Commit**

```bash
cd /home/soul/loopi-p2
git add brain/src/loopi_brain/audio_level.py brain/src/loopi_brain/config.py brain/src/loopi_brain/pipeline/asr.py \
  brain/tests/helpers.py brain/tests/test_audio_level.py brain/tests/test_asr.py brain/tests/test_config.py deploy/README.md
git commit -F - <<'EOF'
feat(brain): normalise the audio level before VAD and Whisper (spec D19)

The device records speech at about -55 dBFS. The worker now raises each decoded recording so its
99.9th percentile reaches -6 dBFS (at most LOOPI_ASR_MAX_GAIN_DB, default 30 dB, never attenuating,
never lifting the room's noise floor above -40 dBFS). Raw files are not touched.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-15; they amend the steps above):**
- **R-F2:** also update the module docstring of `brain/src/loopi_brain/pipeline/asr.py` (line 3 says "Step 3 (deleting raw audio)") to the renumbered spec step (§9.2 step 4 after D19).
- **R-F4:** put `QUIET_SPEECH` and `ROOM_NOISE` (the shared test signals) in `brain/tests/helpers.py` once and import them in `test_audio_level.py`/`test_asr.py`; Task 4's `test_audio_playback.py` imports them too instead of redefining them.
- **R-F5:** the README advertises `LOOPI_ASR_MAX_GAIN_DB`, but the compose `worker` lists its environment explicitly: add `LOOPI_ASR_MAX_GAIN_DB: ${LOOPI_ASR_MAX_GAIN_DB:-30}` to the worker's `environment` in `deploy/docker-compose.yml` (adjust `tests/test_deploy.py` only if it pins the worker's environment keys). Never run docker compose.
- **R-F10:** if a test looks flaky under load, re-run it once before blaming your change and say so in the report.

---

### Task 4 (P2a Task 10b): Normalised playback route and timeline — branch `feat/phase2`, worktree `/home/soul/loopi-p2`

**Files:**
- Modify: `brain/src/loopi_brain/media.py:1-50` (everything above `_mark_error` at line 51: module docstring, imports, constants, new `_ffmpeg`, `remux_to_mp4` on top of it, new `audio_view_path` and `normalise_audio`)
- Modify: `brain/src/loopi_brain/web/routes.py` (imports lines 4–14, logger above line 18, new route above `devices_page` at line 115)
- Modify: `brain/src/loopi_brain/web/templates/timeline.html:42`
- Modify: `deploy/README.md` ("Pipeline (P2a)" section)
- Create: `brain/tests/test_audio_playback.py`

**Interfaces:**
- Consumes: Task 3 `audio_level.level_gain`, `audio_level.dbfs`, `audio_level.SAMPLE_RATE`; `asr.load_audio` (tests only); `item.raw_path` (audio: `raw/<device>/YYYY/MM/DD/<id>.opus`); `MediaError`, `STDERR_TAIL`, `FFMPEG_TIMEOUT_S` (P1 `media.py`).
- Produces:
  - `media._ffmpeg(args: list[str], timeout: float) -> bytes` (stdout; `MediaError` on a non-zero exit or timeout; the process is killed and reaped on any exception). `remux_to_mp4` now uses it (same behaviour).
  - `media.AUDIO_TIMEOUT_S = 120.0`, `media.AUDIO_LIMITER` (read at call time, so tests can patch them).
  - `media.audio_view_path(raw_rel: PurePosixPath) -> PurePosixPath` (`raw/…/<id>.opus` → `view/…/<id>.opus`).
  - `media.normalise_audio(src: Path, dst: Path) -> float` (the gain applied; `dst` appears complete or not at all).
  - `GET /audio/{item_id}.opus` → 200 `audio/ogg` (Range requests give 206); 404 for unknown ids, non-audio items and missing raw files.

All commands run in `/home/soul/loopi-p2/brain`.

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_audio_playback.py`:

```python
import datetime as dt
import hashlib
import logging

import httpx
import numpy as np
import pytest

from helpers import fetch_one, insert_item, make_opus, new_ulid, paired_device
from loopi_brain import audio_level, media
from loopi_brain.app import create_app
from loopi_brain.pipeline.asr import load_audio

QUIET_SPEECH = "aevalsrc=0.02*sin(2*PI*440*t)*mod(floor(2*t)\\,2):s=16000"  # -34 dBFS bursts, silence between
ROOM_NOISE = "anoisesrc=colour=white:amplitude=0.003:sample_rate=16000:seed=1"  # -55 dBFS RMS, like the USB mic
DAY = "2026-10-14"
UTC_9AM_SEOUL = dt.datetime(2026, 10, 14, 0, 30, tzinfo=dt.UTC)


@pytest.fixture
async def dev(pool, pki_settings):
    return await paired_device(pool, pki_settings)


@pytest.fixture
async def web(pool, pki_settings, jobs_open):
    app = create_app(pki_settings, pool=pool)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain.test") as c:
        yield c


async def _audio(pool, settings, device_id, source=QUIET_SPEECH, seconds=10, body: bytes | None = None):
    """An audio item whose raw file is made from the lavfi ``source`` (or is ``body``); (id, raw path)."""
    item_id = await insert_item(pool, device_id, kind="audio", started_at=UTC_9AM_SEOUL, seconds=seconds)
    row = await fetch_one(pool, "SELECT raw_path FROM item WHERE id = %s", (item_id,))
    raw = settings.media_root / row["raw_path"]
    if body is None:
        make_opus(raw, source, seconds)
    else:
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(body)
    return item_id, raw


def _p999_db(path) -> float:
    return audio_level.dbfs(float(np.percentile(np.abs(load_audio(path)), 99.9)))


def _view(settings, raw):
    return settings.media_root / media.audio_view_path(raw.relative_to(settings.media_root))


def _leftovers(settings) -> list:
    return list(settings.media_root.rglob("*.part.opus"))


async def test_quiet_audio_plays_louder_by_the_level_gain(web, pool, pki_settings, dev):
    item_id, raw = await _audio(pool, pki_settings, dev["device_id"])
    expected = audio_level.dbfs(audio_level.level_gain(load_audio(raw)))
    r = await web.get(f"/audio/{item_id}.opus")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/ogg"
    view = _view(pki_settings, raw)
    assert r.content == view.read_bytes()
    assert expected == pytest.approx(28.0, abs=0.5)  # -34 dBFS bursts -> -6 dBFS
    assert _p999_db(view) - _p999_db(raw) == pytest.approx(expected, abs=1.0)
    assert _leftovers(pki_settings) == []


async def test_the_copy_is_made_once_and_the_raw_file_is_never_changed(web, pool, pki_settings, dev, monkeypatch):
    item_id, raw = await _audio(pool, pki_settings, dev["device_id"])
    raw_sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    first = await web.get(f"/audio/{item_id}.opus")

    async def must_not_run(*_a):
        raise AssertionError("the cached copy is served")

    monkeypatch.setattr(media, "normalise_audio", must_not_run)
    second = await web.get(f"/audio/{item_id}.opus", headers={"Range": "bytes=0-99"})
    assert second.status_code == 206 and second.content == first.content[:100], "browsers seek with Range"
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == raw_sha


async def test_unknown_items_videos_and_missing_files_are_404(web, pool, pki_settings, dev):
    assert (await web.get(f"/audio/{new_ulid()}.opus")).status_code == 404
    video = await insert_item(pool, dev["device_id"], kind="video")
    assert (await web.get(f"/audio/{video}.opus")).status_code == 404
    gone = await insert_item(pool, dev["device_id"], kind="audio")  # no file written
    assert (await web.get(f"/audio/{gone}.opus")).status_code == 404


@pytest.mark.parametrize("body", [b"OggS-not-really", b""], ids=["corrupt", "zero-length"])
async def test_an_undecodable_file_falls_back_to_the_raw_bytes(web, pool, pki_settings, dev, caplog, body):
    item_id, raw = await _audio(pool, pki_settings, dev["device_id"], body=body)
    with caplog.at_level(logging.WARNING, logger="loopi_brain.web.routes"):
        r = await web.get(f"/audio/{item_id}.opus")
    assert r.status_code == 200 and r.content == body
    assert f"audio {item_id} plays at its raw level" in caplog.text
    assert not _view(pki_settings, raw).exists() and _leftovers(pki_settings) == []
    assert raw.read_bytes() == body


@pytest.mark.parametrize(
    ("name", "value"),
    [("AUDIO_TIMEOUT_S", 0.001), ("AUDIO_LIMITER", "alimiter=limit=5")],  # limit must be <= 1: ffmpeg refuses it
    ids=["ffmpeg-stuck", "encoder-fails"],
)
async def test_a_stuck_or_failing_ffmpeg_leaves_no_copy_and_the_raw_file_plays(
    web, pool, pki_settings, dev, monkeypatch, name, value
):
    item_id, raw = await _audio(pool, pki_settings, dev["device_id"])
    monkeypatch.setattr(media, name, value)
    r = await web.get(f"/audio/{item_id}.opus")
    assert r.status_code == 200 and r.content == raw.read_bytes()
    assert not _view(pki_settings, raw).exists() and _leftovers(pki_settings) == []


async def test_a_noise_only_night_segment_plays_as_a_soft_hiss(web, pool, pki_settings, dev):
    item_id, raw = await _audio(pool, pki_settings, dev["device_id"], source=ROOM_NOISE)
    assert (await web.get(f"/audio/{item_id}.opus")).status_code == 200
    rms = {name: audio_level.dbfs(float(np.sqrt(np.mean(load_audio(path) ** 2))))
           for name, path in (("raw", raw), ("view", _view(pki_settings, raw)))}
    assert rms["raw"] == pytest.approx(-55.0, abs=1.0)
    assert rms["raw"] + 5 < rms["view"] <= -38.0, f"raised, but no louder than a soft hiss: {rms}"


async def test_a_ten_minute_segment_is_normalised_whole(web, pool, pki_settings, dev):
    # The agent's longest segment (capture.segment_seconds <= 600).
    item_id, raw = await _audio(pool, pki_settings, dev["device_id"], seconds=600)
    assert (await web.get(f"/audio/{item_id}.opus")).status_code == 200
    view = load_audio(_view(pki_settings, raw))
    assert len(view) == pytest.approx(600 * audio_level.SAMPLE_RATE, abs=audio_level.SAMPLE_RATE // 10)
    tail = view[-10 * audio_level.SAMPLE_RATE:]
    assert audio_level.dbfs(float(np.percentile(np.abs(tail), 99.9))) == pytest.approx(-6.0, abs=1.0), "the end is raised"


async def test_the_timeline_plays_audio_through_the_normalising_route(web, pool, pki_settings, dev):
    item_id, _raw = await _audio(pool, pki_settings, dev["device_id"])
    text = (await web.get("/", params={"day": DAY})).text
    assert f'<audio controls preload="none" src="/audio/{item_id}.opus"></audio>' in text
    assert "/media/raw/" not in text, "the raw file is not linked"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_audio_playback.py -q 2>&1 | grep -E '^FAILED|passed|failed'`
Expected:
```
FAILED tests/test_audio_playback.py::test_quiet_audio_plays_louder_by_the_level_gain
FAILED tests/test_audio_playback.py::test_the_copy_is_made_once_and_the_raw_file_is_never_changed
FAILED tests/test_audio_playback.py::test_an_undecodable_file_falls_back_to_the_raw_bytes[corrupt]
FAILED tests/test_audio_playback.py::test_an_undecodable_file_falls_back_to_the_raw_bytes[zero-length]
FAILED tests/test_audio_playback.py::test_a_stuck_or_failing_ffmpeg_leaves_no_copy_and_the_raw_file_plays[ffmpeg-stuck]
FAILED tests/test_audio_playback.py::test_a_stuck_or_failing_ffmpeg_leaves_no_copy_and_the_raw_file_plays[encoder-fails]
FAILED tests/test_audio_playback.py::test_a_noise_only_night_segment_plays_as_a_soft_hiss
FAILED tests/test_audio_playback.py::test_a_ten_minute_segment_is_normalised_whole
FAILED tests/test_audio_playback.py::test_the_timeline_plays_audio_through_the_normalising_route
9 failed, 1 passed, 1 warning
```
(404s where 200 is expected, and `AttributeError: … has no attribute 'AUDIO_TIMEOUT_S'` / `'audio_view_path'`; the 404 test already passes because the route does not exist yet.)

- [ ] **Step 3: Make the normalised copy (`media.py`)**

In `brain/src/loopi_brain/media.py`, replace everything from the top of the file down to (not including) the line `async def _mark_error(pool: AsyncConnectionPool, item_id: str, message: str) -> None:` with:

```python
"""Browser-playable views of raw media: raw H.264 -> MP4 without re-encoding, and audio at a level you
can hear (spec D19). Raw files are only ever read."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from pathlib import Path, PurePosixPath

import anyio
import numpy as np
from psycopg_pool import AsyncConnectionPool

from . import audio_level
from .config import Settings

log = logging.getLogger(__name__)
STDERR_TAIL = 500
FFMPEG_TIMEOUT_S = 300.0
AUDIO_TIMEOUT_S = 120.0  # per ffmpeg pass; a 10-minute segment (the agent's longest) takes a few seconds
# -1 dBFS ceiling; level=false: no automatic make-up gain on top of ours; latency=true: no 5 ms shift.
AUDIO_LIMITER = "alimiter=limit=0.891:attack=5:release=50:level=false:latency=true"


class MediaError(Exception):
    """ffmpeg failed to process a file."""


async def _ffmpeg(args: list[str], timeout: float) -> bytes:
    """Run ffmpeg with ``args``; its stdout. MediaError on a non-zero exit or after ``timeout`` seconds,
    and the process never outlives the call."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            raise MediaError(f"ffmpeg timed out after {timeout}s") from exc
        if proc.returncode != 0:
            raise MediaError(stderr.decode(errors="replace")[-STDERR_TAIL:] or f"ffmpeg exit {proc.returncode}")
        return stdout
    except BaseException:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            await asyncio.shield(proc.wait())
        raise


# Assumption: raw input has no VUI timing (Pi encoder output), so -framerate sets the pace;
# streams that carry VUI timing (e.g. libx264) make ffmpeg ignore -framerate.
async def remux_to_mp4(src: Path, dst: Path, fps: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex}.part.mp4")
    try:
        await _ffmpeg(
            ["-framerate", str(fps), "-f", "h264", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(tmp)],
            FFMPEG_TIMEOUT_S,
        )
        tmp.replace(dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def audio_view_path(raw_rel: PurePosixPath) -> PurePosixPath:
    """``raw/<device>/YYYY/MM/DD/<id>.opus`` -> ``view/<device>/YYYY/MM/DD/<id>.opus``."""
    return PurePosixPath("view", *raw_rel.parts[1:]).with_suffix(".opus")


async def normalise_audio(src: Path, dst: Path) -> float:
    """Write ``src`` to ``dst`` as Ogg Opus raised by the shared level gain, through a -1 dBFS limiter so
    nothing clips; return the gain. ``src`` is only read; ``dst`` appears complete or not at all."""
    pcm = await _ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", str(audio_level.SAMPLE_RATE), "-f", "f32le", "-"],
                        AUDIO_TIMEOUT_S)
    gain = await anyio.to_thread.run_sync(audio_level.level_gain, np.frombuffer(pcm, dtype=np.float32))
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex}.part.opus")
    try:
        await _ffmpeg(
            ["-i", str(src), "-vn", "-ac", "1", "-ar", str(audio_level.SAMPLE_RATE),
             "-af", f"volume={gain:.6f},{AUDIO_LIMITER}", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", str(tmp)],
            AUDIO_TIMEOUT_S,
        )
        tmp.replace(dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return gain
```

The remux behaviour is unchanged (same ffmpeg arguments, timeout, `.part.mp4` clean-up); only the subprocess handling moved into `_ffmpeg`. Both ffmpeg passes run with a 16 kHz mono output: the encode pass is 3× faster than at libopus' default 48 kHz (2.1 s against 6.2 s for 10 minutes in the prototype).

- [ ] **Step 4: Serve it (`routes.py`, `timeline.html`)**

In `brain/src/loopi_brain/web/routes.py`:
- add `import logging` after `import datetime as dt`;
- replace `from pathlib import Path` with `from pathlib import Path, PurePosixPath`;
- replace `from fastapi.responses import HTMLResponse` with `from fastapi.responses import FileResponse, HTMLResponse`;
- replace `from .. import devices, pki` with `from .. import audio_level, devices, media, pki`;
- add `log = logging.getLogger(__name__)` directly above `TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))`;
- insert directly above `@router.get("/devices", response_class=HTMLResponse)`:

```python
@router.get("/audio/{item_id}.opus")
async def audio_playback(request: Request, item_id: str):
    """An audio segment at a level you can hear (spec D19): a normalised copy under ``view/``, made on the
    first request; the raw file when that fails. Raw files are never changed."""
    settings = request.app.state.settings
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute("SELECT raw_path FROM item WHERE id = %s AND kind = 'audio'", (item_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown audio item")
    raw_rel = PurePosixPath(row["raw_path"])
    raw = settings.media_root / raw_rel
    if not raw.is_file():
        raise HTTPException(status_code=404, detail="audio file missing")
    view = settings.media_root / media.audio_view_path(raw_rel)
    if not view.is_file():
        try:
            gain = await media.normalise_audio(raw, view)
        except Exception as exc:
            log.warning("audio %s plays at its raw level: %s", item_id, exc)
            return FileResponse(raw, media_type="audio/ogg")
        log.info("audio %s normalised: %+.1f dB", item_id, audio_level.dbfs(gain))
    return FileResponse(view, media_type="audio/ogg")
```

(two blank lines separate it from `@router.get("/devices", …)`, as between the other routes)

In `brain/src/loopi_brain/web/templates/timeline.html`, replace
`          <audio controls preload="none" src="/media/{{ it.view_path }}"></audio>`
with
```html
          <audio controls preload="none" src="/audio/{{ it.id }}.opus"></audio>
```

(Caddy's catch-all `handle` already sends `/audio/*` to the api behind basic auth; no Caddyfile change.)

In `deploy/README.md`, directly after the "Audio level (spec D19)" bullet from Task 3, add:

```markdown
- Audio playback: the timeline plays `/audio/<item id>.opus`, a copy raised by the same rule through a -1 dBFS limiter.
  The api makes it on the first play and keeps it under `view/` next to the video MP4s; if ffmpeg cannot read the
  recording, the raw file plays instead (a `plays at its raw level` warning in `docker compose logs api`). Deleting
  `view/**/*.opus` makes the copies again.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_audio_playback.py tests/test_media.py -q`
Expected: `26 passed, 1 warning` (10 new + the 16 P1 media tests on the refactored remux).

Then the whole suite with coverage: `uv run pytest --cov=loopi_brain -q 2>&1 | grep -E 'audio_level|config.py|media.py|asr.py|routes.py|TOTAL|passed'`
Expected:
```
src/loopi_brain/audio_level.py              28      0   100%
src/loopi_brain/config.py                   34      0   100%
src/loopi_brain/media.py                    97      2    98%
src/loopi_brain/pipeline/asr.py            190      2    99%
src/loopi_brain/providers/config.py         62      0   100%
src/loopi_brain/web/routes.py              124      0   100%
TOTAL                                     1978     33    98%
336 passed, 1 skipped, 1 warning
```
(The two uncovered `media.py` lines are P1's `requeue_stored` error branch, uncovered before too. `test_a_ten_minute_segment_is_normalised_whole` takes about 5 s.)

- [ ] **Step 6: Commit**

```bash
cd /home/soul/loopi-p2
git add brain/src/loopi_brain/media.py brain/src/loopi_brain/web/routes.py brain/src/loopi_brain/web/templates/timeline.html \
  brain/tests/test_audio_playback.py deploy/README.md
git commit -F - <<'EOF'
feat(brain): play audio at a level you can hear (spec D19)

GET /audio/<id>.opus serves a copy raised by the shared level gain through a -1 dBFS limiter, made by
ffmpeg on the first play and kept under view/; when ffmpeg cannot make it, the raw file plays and a
warning is logged. Raw recordings are never changed. The timeline uses the new URL.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01ETsyruQruHz9Y9A3r351By
EOF
```

**Pre-flight rulings for this task (controller, 2026-10-15; they amend the steps above):**
- **R-F1 (one ffmpeg helper):** `pipeline/frames.py` has its own near-identical `_ffmpeg`. Make it a thin wrapper over the new media helper (exposed as `media.run_ffmpeg(args, timeout) -> bytes`; keep `media._ffmpeg` only if you name it that way consistently everywhere): `frames._ffmpeg(args)` calls it with `FFMPEG_TIMEOUT_S` and re-raises `MediaError` as `FrameError` (`raise FrameError(str(exc)) from exc`), so `video.py` and the P2a frame tests keep working unchanged. The extra `-y` is harmless for frames (it writes to a fresh temp path). Run `tests/test_frames.py` and `tests/test_video.py` too.
- **R-F8:** keep two blank lines before `_mark_error` (PEP 8) after replacing the media block.
- **R-F9:** one raw→view path mapping: extract a small helper (e.g. `_view_rel(raw_rel, suffix)`) used by both `remux_item` (`.mp4`) and `audio_view_path` (`.opus`).
- **R-F3:** the Step 5 coverage grep for `routes.py` also prints `api/device_routes.py`; that extra line is expected.
- **R-F4:** import `QUIET_SPEECH`/`ROOM_NOISE` from `tests/helpers.py` (added in Task 3) instead of redefining them.

---

### Task 5: Image smoke8, owner flash and the +20 dB check (controller-executed) — branch `feat/audio-gain`, worktree `/home/soul/loopi-audio`

The controller runs this task itself, not a subagent. It needs Tasks 1–2 committed and reviewed. It ends the P1c Task 6 run on smoke7 and wipes the device's `/data` (new pairing, new device id), so it starts only after the owner's yes. Nothing here is committed except the ledger lines.

**Files:**
- Create (git-ignored): `/home/soul/loopi-audio/os/out/loopi-dev.img`, `/home/soul/loopi-audio/os/out/loopi-dev-smoke8.img`, the build log in your scratchpad
- Modify: the ledger (`docs/superpowers/ledgers/audio-level-progress.md` and the SDD progress copy)

**Interfaces:**
- Consumes: `os/build.sh image` (Buildroot in the `loopi-os-builder` image, volume `loopi-buildroot`; it ends by running `os/tests/check-image.sh`), `/home/soul/loopi/os/out/flash-dev.sh` (git-ignored owner tool: size-guarded `/dev/sdb`, reads `os/out/wifi-psk` next to itself, makes a fresh pair token on the live brain, `HOST_MODE=1` comments out the dwc2 overlay), `~/.ssh/id_ed25519.pub`.
- Produces: `os/out/loopi-dev-smoke8.img` (image of the reviewed `feat/audio-gain` head + `loopi-authorized_keys`), the owner's flashed device, a level report.

Set `SCRATCH` to your scratchpad directory first (`SCRATCH=/tmp/claude-…/scratchpad`).

- [ ] **Step 1: Preconditions — ask the owner**

Ask: "Soak P1c Task 6 trên smoke7 đã xong chưa (24 h, SIGKILL/LED, 10 lần rút điện)? Flash smoke8 sẽ kết thúc lượt chạy đó và xoá `/data` của thiết bị (ghép cặp lại, device_id mới). Trên trang Thiết bị, hàng đợi của thiết bị cũ phải về 0 trước khi rút điện." Continue only on yes.

```bash
git -C /home/soul/loopi-audio status --short        # must print nothing
git -C /home/soul/loopi-audio log --oneline -3      # Task 2 and Task 1 commits on top of 7021705
```

- [ ] **Step 2: Rebuild the agent package and the image**

The agent is a local Buildroot package, rsynced only on a fresh build: without the dirclean the image keeps the previous agent (P1c final-fix report). Nothing else needs a dirclean (`config.txt` is unchanged; `post-image.sh` copies `loopi.toml.example` on every build).

```bash
cd /home/soul/loopi-audio
docker run --rm -v loopi-buildroot:/br loopi-os-builder runuser -u builder -- make -C /br/buildroot O=/br/output loopi-agent-dirclean
./os/build.sh image 2>&1 | tee "$SCRATCH/build-smoke8.log"; echo "build rc=${PIPESTATUS[0]}"
grep -c '^\[ OK \]' "$SCRATCH/build-smoke8.log"; grep -c '^\[FAIL\]' "$SCRATCH/build-smoke8.log"
grep -E 'verify-config|audio_gain_db|amplifies the microphone' "$SCRATCH/build-smoke8.log"
```
Expected: `build rc=0`; `90` and `0`; `verify-config: all fragment lines applied`, `[ OK ] the shipped agent amplifies the microphone before encoding (capture.audio_gain_db, spec D19)` and `[ OK ] loopi.toml.example shows the microphone gain (audio_gain_db = 20.0)`. Run the build in the background (Monitor on the log): the agent rebuild takes several minutes. The build volume is shared with `/home/soul/loopi`: the next image built from there needs the same `loopi-agent-dirclean` first.

- [ ] **Step 3: Make smoke8 (image + SSH key on the boot partition)**

```bash
cd /home/soul/loopi-audio
cp os/out/loopi-dev.img os/out/loopi-dev-smoke8.img
docker run --rm -v /home/soul/loopi-audio/os/out:/w -v "$HOME/.ssh/id_ed25519.pub:/k.pub:ro" loopi-os-builder \
  mcopy -o -i /w/loopi-dev-smoke8.img@@4194304 /k.pub ::loopi-authorized_keys
docker run --rm -v /home/soul/loopi-audio/os/out:/w loopi-os-builder mdir -i /w/loopi-dev-smoke8.img@@4194304 ::
docker run --rm -v /home/soul/loopi-audio/os/out:/w loopi-os-builder mtype -i /w/loopi-dev-smoke8.img@@4194304 ::loopi.toml.example | grep audio_gain_db
sha256sum os/out/loopi-dev-smoke8.img | cut -c1-8
```
Expected: `mdir` lists `loopi-authorized_keys` and `loopi.toml.example` and no `loopi.toml`; the example line `audio_gain_db = 20.0 …`; an 8-hex-digit hash to quote to the owner. (The boot partition starts at 4 MiB, as in `flash-dev.sh`.) Do not copy `wifi-psk`: the build does not use it, and `flash-dev.sh` reads its own copy in `/home/soul/loopi/os/out`.

- [ ] **Step 4: Hand the image to the owner (hardware step)**

Tell the owner (Vietnamese): "Image smoke8 sẵn sàng: `/home/soul/loopi-audio/os/out/loopi-dev-smoke8.img` (sha256 `<8 ký tự>`). Bạn chạy `HOST_MODE=1 /home/soul/loopi/os/out/flash-dev.sh /home/soul/loopi-audio/os/out/loopi-dev-smoke8.img` (script tạo pair token mới trên brain và ghi `loopi.toml` vào bản sao image), cắm thẻ vào Pi, mic vào cổng `USB`, sạc vào `PWR`, rồi báo mình giờ cắm điện." Stop until the owner reports the power-on time `T0` (KST). Run `flash-dev.sh` from `/home/soul/loopi/os/out`, never from a worktree: from a worktree it would look for `deploy/.env` there and fail to make the pair token.

- [ ] **Step 5: Wait for the new device's segments (read-only)**

```bash
NEW="$(ls -td /home/soul/loopi-data/media/raw/*/ | head -n1)"; echo "$NEW"
```
Expected: a device directory other than `01M4X6BPHRZH3ZNJRAV12AA0T1/` (the smoke7 device), created after `T0`. Wait (Monitor with an until-loop, about 10 minutes) until at least 8 `.opus` files newer than `T0` exist under it: `find "$NEW" -name '*.opus' -newermt "<T0, e.g. 2026-10-15T22:10:00+09:00>" | wc -l`.

- [ ] **Step 6: Measure the level (read-only)**

```bash
find "$NEW" -name '*.opus' -newermt "<T0>" | sort | tail -n 8 | while read -r f; do
  printf '%s ' "$(basename "$f")"
  ffmpeg -hide_banner -nostats -i "$f" -af volumedetect -f null - 2>&1 | grep -oE '(mean|max)_volume: [-0-9.]+' | awk '{printf "%s ", $2}'
  echo
done
```
Expected: mean_volume around −36 dB (the 1 337 segments before: median −56.4 dB, 10th–90th percentile −60.4 … −53.3 dB; +20 dB puts the median at −36.4, so −41 … −32 dB per segment is a pass), max_volume around −11 dB for a quiet room and never above about 0 dB (limiter at −1 dBFS plus up to 1 dB of Opus overshoot). A mean still near −56 dB means the old agent shipped: check Step 2's two new image checks. Optional, with the owner's OK (it stops recording for a few seconds): `ssh root@<device WiFi IP> 'systemctl stop loopi-agent && loopi-agent selftest; systemctl start loopi-agent'` should print `[ OK ] microphone: 1 s captured, level … dBFS, … dBFS after the +20 dB gain` about 20 dB apart.

- [ ] **Step 7: Record and report**

Append to the ledger: the image hash, the new device id, `T0`, the eight measured lines and the verdict. Report to the owner in Vietnamese (before/after medians, pass or fail). Then ask two things: whether to revoke the smoke7 device `01M4X6BPHRZH3ZNJRAV12AA0T1` on the Devices page, and whether to merge `feat/audio-gain` into `feat/phase1` (fast-forward; the live checkout `/home/soul/loopi` is on `feat/phase1`, and only `agent/`, `os/` and `docs/` change, so the running brain is not affected). The brain side (Tasks 3–4) is checked on real recordings in P2a Task 11, once the P2a stack is deployed.

---

## Rulings taken while writing this plan

Copy these into the ledger before Task 1.

- Ruling: the brain gain also never lifts the noise floor (10th percentile of the 20 ms frame RMS) above −40 dBFS. The brief's rule alone (99.9th percentile to −6 dBFS, ≤ +30 dB) would raise a noise-only segment recorded after the device's +20 dB to speech loudness: in the prototype, white noise at −35.5 dBFS RMS (the room after the device gain) got +23.2 dB and would play at −12.3 dBFS RMS; with the floor rule it gets no gain, and the brief's own signature `level_gain(samples, target_peak=0.5, max_gain_db=30.0)` is kept. On the prototype's real speech sample the rule changes ASR gain by under 2 dB (+28.2 instead of +30) and Silero still finds 31.1 s of speech instead of 18.0 s. Also written into D19 — cost if wrong: quiet speech in a loud room gets less gain; raise `NOISE_FLOOR_MAX`.
- Ruling: playback uses ffmpeg (decode to f32le for the gain, then `volume` + `alimiter` + libopus at 16 kHz), not PyAV: one bounded subprocess helper for both passes, no faster-whisper import in the api process, and `alimiter` gives the limiter. ASR keeps PyAV (`load_audio`) — cost if wrong: two decoders could round differently; the gain is a percentile, so this is negligible.
- Ruling: `remux_to_mp4` moves onto the new `_ffmpeg` helper (identical arguments, timeout and clean-up; the 16 P1 media tests cover it) rather than keeping two copies of the kill-and-reap logic — cost if wrong: a P1 remux regression, caught by `tests/test_media.py`.
- Ruling: the gain is applied by wrapping the audio factory in `run::run` (`gain::with_gain`), not in `main.rs` or the ALSA module: the wiring is in the tested library (the e2e test decodes an upload to prove it) and the capture loop is untouched — cost if wrong: none expected.
- Ruling: playback uses one fixed rule (`audio_level` defaults, 30 dB) and is not tied to `LOOPI_ASR_MAX_GAIN_DB`, so cached copies never depend on a setting that can change; delete `view/**/*.opus` to rebuild them — cost if wrong: an owner who tunes the ASR ceiling hears no difference on playback.
- Ruling: two image checks (88 → 90) guard Task 5: the shipped agent must contain the new config error text, and `loopi.toml.example` must set `audio_gain_db = 20.0`. Task 5 runs `loopi-agent-dirclean` before `./os/build.sh image`; `./os/build.sh image` alone would ship the previous agent — cost if wrong: one more build.
- Ruling: Task 5 does not copy `os/out/wifi-psk` into the worktree (the brief asked for it): neither the build nor anything else in the worktree reads it, and the owner's `flash-dev.sh` must run from `/home/soul/loopi/os/out`, where its own copy is — one less copy of a secret — cost if wrong: none; copy it with `install -m 600` if a later step needs it.
- Ruling: P2b retention, when it deletes raw audio after transcription, must delete the matching `view/…/<id>.opus` copy too (the route already answers 404 once the raw file is gone, so a forgotten copy is never served) — cost if wrong: orphaned playback copies use disk; carried to the P2b plan.
- Ruling: Tasks 3–4 are named P2a Task 10a/10b and executed from this plan; the P2a plan file is not edited — cost if wrong: none.
