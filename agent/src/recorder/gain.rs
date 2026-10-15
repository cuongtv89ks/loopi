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
