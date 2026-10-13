//! Privacy LED. Solid whenever anything is being recorded — errors never hide recording.
use std::future::Future;
use std::io;
use std::time::Duration;

use tokio::time::Instant;

use crate::control::ControlState;
use crate::status::AgentStatus;

pub const CAMERA_FAILURE_LIMIT: u32 = 5;
const SLOW_PERIOD_MS: u64 = 1000;
const FAST_PERIOD_MS: u64 = 200;
const BLINK_FRAME: Duration = Duration::from_millis(50);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LedPattern {
    Off,
    Solid,
    SlowBlink,
    FastBlink,
}

pub fn led_pattern(status: &AgentStatus, state: &ControlState) -> LedPattern {
    if status.recording_video || status.recording_audio {
        return LedPattern::Solid;
    }
    let error = !status.paired
        || !status.clock_synced
        || status.disk_stopped
        || status.camera_failures >= CAMERA_FAILURE_LIMIT;
    if error {
        LedPattern::FastBlink
    } else if state.paused || status.quiet {
        LedPattern::SlowBlink
    } else {
        LedPattern::Off
    }
}

pub fn level_at(pattern: LedPattern, elapsed_ms: u64) -> bool {
    match pattern {
        LedPattern::Off => false,
        LedPattern::Solid => true,
        LedPattern::SlowBlink => elapsed_ms % SLOW_PERIOD_MS < SLOW_PERIOD_MS / 2,
        LedPattern::FastBlink => elapsed_ms % FAST_PERIOD_MS < FAST_PERIOD_MS / 2,
    }
}

pub trait LedDriver: Send {
    fn set(&mut self, on: bool) -> io::Result<()>;
}

pub struct NoopLed;

impl LedDriver for NoopLed {
    fn set(&mut self, _on: bool) -> io::Result<()> {
        Ok(())
    }
}

fn set_logged(led: &mut dyn LedDriver, on: bool) {
    if let Err(e) = led.set(on) {
        tracing::warn!(error = %e, on, "LED driver set failed");
    }
}

/// Fast-blink `led` until `fut` completes (used while pairing), then switch it off.
pub async fn blink_while<F: Future>(led: &mut dyn LedDriver, fut: F) -> F::Output {
    tokio::pin!(fut);
    let start = Instant::now();
    let mut ticker = tokio::time::interval(BLINK_FRAME);
    loop {
        tokio::select! {
            out = &mut fut => {
                set_logged(led, false);
                return out;
            }
            _ = ticker.tick() => {
                let on = level_at(LedPattern::FastBlink, start.elapsed().as_millis() as u64);
                set_logged(led, on);
            }
        }
    }
}

#[cfg(target_os = "linux")]
pub struct GpioLed {
    request: gpiocdev::Request,
    line: u32,
}

#[cfg(target_os = "linux")]
impl GpioLed {
    pub fn open(chip: &str, line: u32) -> Result<Self, gpiocdev::Error> {
        let request = gpiocdev::Request::builder()
            .on_chip(chip)
            .with_consumer("loopi-led")
            .with_line(line)
            .as_output(gpiocdev::line::Value::Inactive)
            .request()?;
        Ok(Self { request, line })
    }
}

#[cfg(target_os = "linux")]
impl LedDriver for GpioLed {
    fn set(&mut self, on: bool) -> io::Result<()> {
        let value = if on { gpiocdev::line::Value::Active } else { gpiocdev::line::Value::Inactive };
        self.request.set_value(self.line, value).map(|_| ()).map_err(io::Error::other)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::control::Command;
    use crate::testing::RecordingLed;

    fn ok_status() -> AgentStatus {
        AgentStatus { paired: true, clock_synced: true, ..AgentStatus::default() }
    }

    #[test]
    fn recording_is_always_solid_even_with_errors() {
        let state = ControlState::initial(true);
        let recording = AgentStatus { recording_audio: true, camera_failures: 9, disk_stopped: true, ..ok_status() };
        assert_eq!(led_pattern(&recording, &state), LedPattern::Solid);
    }

    #[test]
    fn errors_blink_fast_and_pause_blinks_slow() {
        let state = ControlState::initial(true);
        assert_eq!(led_pattern(&AgentStatus { paired: false, ..ok_status() }, &state), LedPattern::FastBlink);
        assert_eq!(led_pattern(&AgentStatus { camera_failures: 5, ..ok_status() }, &state), LedPattern::FastBlink);
        assert_eq!(led_pattern(&ok_status(), &state.apply(&Command::Pause)), LedPattern::SlowBlink);
        assert_eq!(led_pattern(&AgentStatus { quiet: true, ..ok_status() }, &state), LedPattern::SlowBlink);
        assert_eq!(led_pattern(&ok_status(), &state), LedPattern::Off);
    }

    #[test]
    fn levels_follow_the_pattern() {
        assert!(level_at(LedPattern::Solid, 123));
        assert!(!level_at(LedPattern::Off, 123));
        assert!(level_at(LedPattern::SlowBlink, 100) && !level_at(LedPattern::SlowBlink, 600));
        assert!(level_at(LedPattern::FastBlink, 50) && !level_at(LedPattern::FastBlink, 150));
    }

    #[tokio::test]
    async fn blink_while_blinks_then_turns_off() {
        let mut led = RecordingLed::default();
        let out = blink_while(&mut led, async {
            tokio::time::sleep(std::time::Duration::from_millis(300)).await;
            42
        })
        .await;
        assert_eq!(out, 42);
        let states = led.states.lock().unwrap().clone();
        assert!(states.contains(&true) && states.contains(&false));
        assert_eq!(states.last(), Some(&false));
    }

    #[test]
    fn recording_is_solid_for_every_other_input_combination() {
        for recording_video in [false, true] {
            for recording_audio in [false, true] {
                if !(recording_video || recording_audio) {
                    continue;
                }
                for paired in [false, true] {
                    for clock_synced in [false, true] {
                        for quiet in [false, true] {
                            for disk_stopped in [false, true] {
                                for camera_failures in [0, CAMERA_FAILURE_LIMIT, 99] {
                                    for paused in [false, true] {
                                        let mut state = ControlState::initial(true);
                                        if paused {
                                            state = state.apply(&Command::Pause);
                                        }
                                        let status = AgentStatus {
                                            paired,
                                            clock_synced,
                                            quiet,
                                            disk_stopped,
                                            camera_failures,
                                            recording_video,
                                            recording_audio,
                                            ..AgentStatus::default()
                                        };
                                        assert_eq!(led_pattern(&status, &state), LedPattern::Solid);
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    struct FailingLed;
    impl LedDriver for FailingLed {
        fn set(&mut self, _on: bool) -> io::Result<()> {
            Err(io::Error::other("gpio gone"))
        }
    }

    #[tokio::test]
    async fn blink_while_survives_driver_errors() {
        let out = blink_while(&mut FailingLed, async {
            tokio::time::sleep(Duration::from_millis(120)).await;
            7
        })
        .await;
        assert_eq!(out, 7);
    }
}
