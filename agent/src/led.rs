//! Privacy LED. Solid whenever anything is being recorded — errors never hide recording.
use std::future::Future;
use std::io;
use std::time::Duration;

use tokio::time::Instant;

use crate::control::ControlState;
use crate::status::{AgentStatus, LedHealth, StatusTx};

pub const CAMERA_FAILURE_LIMIT: u32 = 5;
/// Consecutive failed LED writes after which the LED counts as broken and recording stops.
pub const LED_FAILURE_LIMIT: u32 = 20;
/// `status.last_error` (prefix) while recording is disabled because the LED does not work.
pub const LED_UNAVAILABLE_ERROR: &str = "privacy LED unavailable: recording disabled";
pub(crate) const LED_WRITE_ERROR_PREFIX: &str = "privacy LED write failed: ";
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
    // A failed LED keeps blinking: the toggling writes are what detect its recovery.
    let error = !status.paired
        || !status.clock_synced
        || status.disk_stopped
        || status.led == LedHealth::Failed
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

/// Dev hosts only (they have no LED). It does not exist in Linux builds, so a missing GPIO LED on
/// the device can never be papered over with a driver that pretends to work.
#[cfg(not(target_os = "linux"))]
pub struct NoopLed;

#[cfg(not(target_os = "linux"))]
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

/// Run `capture` with the LED solid, then switch it off. `capture` does not run at all when the
/// LED cannot be switched on: nothing is captured with the LED dark.
pub fn while_solid<T>(led: &mut dyn LedDriver, capture: impl FnOnce() -> T) -> io::Result<T> {
    led.set(true)?;
    let out = capture();
    set_logged(led, false);
    Ok(out)
}

/// Drives the privacy LED and keeps `status.led` / `status.last_error` in step with whether its
/// writes work. `LED_FAILURE_LIMIT` failures in a row mark it `Failed` (the recording gates
/// close); the next successful write marks it `Working` again.
pub struct LedWriter {
    driver: Box<dyn LedDriver>,
    /// Level of the last *successful* write, so a failed one is retried on the next frame.
    last: Option<bool>,
    failures: u32,
}

impl LedWriter {
    pub fn new(driver: Box<dyn LedDriver>) -> Self {
        Self { driver, last: None, failures: 0 }
    }

    /// Write `on` unless it is already the confirmed level. `status` is `None` only for the
    /// final write after shutdown, when nobody reads it any more.
    pub fn set(&mut self, on: bool, status: Option<&StatusTx>) {
        if self.last == Some(on) {
            return;
        }
        match self.driver.set(on) {
            Ok(()) => {
                self.last = Some(on);
                if self.failures >= LED_FAILURE_LIMIT {
                    tracing::info!(failures = self.failures, "privacy LED works again");
                }
                self.failures = 0;
                if let Some(status) = status {
                    status.send_if_modified(mark_working);
                }
            }
            Err(e) => {
                self.failures = self.failures.saturating_add(1);
                if self.failures == 1 || self.failures == LED_FAILURE_LIMIT {
                    tracing::error!(error = %e, on, failures = self.failures, "privacy LED write failed");
                }
                let Some(status) = status else { return };
                let failed = self.failures >= LED_FAILURE_LIMIT;
                let message = if failed {
                    format!("{LED_UNAVAILABLE_ERROR} (writes keep failing: {e})")
                } else {
                    format!("{LED_WRITE_ERROR_PREFIX}{e}")
                };
                // Below the limit the health is left as it was (a single glitch does not stop
                // recording); only the report changes.
                status.send_if_modified(|s| {
                    let health = if failed { LedHealth::Failed } else { s.led };
                    let changed = s.led != health || s.last_error.as_deref() != Some(message.as_str());
                    s.led = health;
                    s.last_error = Some(message);
                    changed
                });
            }
        }
    }
}

fn mark_working(s: &mut AgentStatus) -> bool {
    let ours = s
        .last_error
        .as_deref()
        .is_some_and(|e| e.starts_with(LED_WRITE_ERROR_PREFIX) || e.starts_with(LED_UNAVAILABLE_ERROR));
    if ours {
        s.last_error = None;
    }
    let changed = ours || s.led != LedHealth::Working;
    s.led = LedHealth::Working;
    changed
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
        AgentStatus { paired: true, clock_synced: true, led: LedHealth::Working, ..AgentStatus::default() }
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
        assert_eq!(led_pattern(&AgentStatus { led: LedHealth::Failed, ..ok_status() }, &state), LedPattern::FastBlink);
        assert_eq!(
            led_pattern(&AgentStatus { led: LedHealth::Unconfirmed, ..ok_status() }, &state),
            LedPattern::Off,
            "no flash at startup before the first write"
        );
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

    #[test]
    fn while_solid_lights_the_led_only_around_the_capture() {
        let mut led = RecordingLed::default();
        let states = led.states.clone();
        let seen = while_solid(&mut led, || states.lock().unwrap().clone()).unwrap();
        assert_eq!(seen, vec![true], "solid during the capture");
        assert_eq!(*led.states.lock().unwrap(), vec![true, false], "off afterwards");
    }

    #[test]
    fn while_solid_does_not_capture_with_a_dark_led() {
        let mut ran = false;
        assert!(while_solid(&mut FailingLed, || ran = true).is_err());
        assert!(!ran);
    }

    /// Fails while `broken` is set.
    struct SwitchLed(std::sync::Arc<std::sync::atomic::AtomicBool>);
    impl LedDriver for SwitchLed {
        fn set(&mut self, _on: bool) -> io::Result<()> {
            if self.0.load(std::sync::atomic::Ordering::SeqCst) {
                Err(io::Error::other("gpio gone"))
            } else {
                Ok(())
            }
        }
    }

    #[test]
    fn writer_marks_the_led_failed_after_the_limit_and_working_again_on_success() {
        use crate::status::status_channel;
        let broken = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let mut writer = LedWriter::new(Box::new(SwitchLed(broken.clone())));
        let status = status_channel(AgentStatus::default());
        assert_eq!(status.borrow().led, LedHealth::Unconfirmed);
        writer.set(false, Some(&status));
        assert_eq!(status.borrow().led, LedHealth::Working, "confirmed by the first successful write");

        broken.store(true, std::sync::atomic::Ordering::SeqCst);
        for _ in 0..LED_FAILURE_LIMIT - 1 {
            writer.set(true, Some(&status));
        }
        assert_eq!(status.borrow().led, LedHealth::Working, "a few glitches do not stop recording");
        assert!(status.borrow().last_error.as_deref().unwrap().starts_with(LED_WRITE_ERROR_PREFIX));
        writer.set(true, Some(&status));
        assert_eq!(status.borrow().led, LedHealth::Failed);
        assert!(status.borrow().last_error.as_deref().unwrap().starts_with(LED_UNAVAILABLE_ERROR));
        assert_eq!(led_pattern(&status.borrow(), &ControlState::initial(true)), LedPattern::FastBlink);

        broken.store(false, std::sync::atomic::Ordering::SeqCst);
        writer.set(true, Some(&status));
        assert_eq!(status.borrow().led, LedHealth::Working);
        assert_eq!(status.borrow().last_error, None, "our error is cleared");
    }

    #[test]
    fn writer_leaves_other_errors_alone_on_success() {
        use crate::status::status_channel;
        let mut writer = LedWriter::new(Box::new(RecordingLed::default()));
        let status = status_channel(AgentStatus { last_error: Some("camera: gone".into()), ..AgentStatus::default() });
        writer.set(true, Some(&status));
        assert_eq!(status.borrow().last_error.as_deref(), Some("camera: gone"));
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
