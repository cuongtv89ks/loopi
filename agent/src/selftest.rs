//! `loopi-agent selftest`: quick on-device checks printed as a report.
use std::path::Path;
use std::time::Duration;

use crate::config::Config;
use crate::disk::{DiskUsage, StatvfsUsage, DROP_ABOVE_PCT};
#[cfg(any(test, target_os = "linux"))]
use crate::led::{while_solid, LedDriver};
use crate::pairing::load_identity;

const CAMERA_TIMEOUT: Duration = Duration::from_secs(20);
const SILENCE_DBFS: f32 = -120.0;

#[derive(Debug, Clone, PartialEq)]
pub struct Check {
    pub name: &'static str,
    pub ok: bool,
    pub detail: String,
}

impl Check {
    pub fn pass(name: &'static str, detail: impl Into<String>) -> Self {
        Self { name, ok: true, detail: detail.into() }
    }

    pub fn fail(name: &'static str, detail: impl Into<String>) -> Self {
        Self { name, ok: false, detail: detail.into() }
    }
}

pub fn format_report(checks: &[Check]) -> String {
    let mut out = String::new();
    for check in checks {
        let tag = if check.ok { "[ OK ]" } else { "[FAIL]" };
        out.push_str(&format!("{tag} {}: {}\n", check.name, check.detail));
    }
    let passed = checks.iter().filter(|c| c.ok).count();
    out.push_str(&format!("{passed}/{} checks passed\n", checks.len()));
    out
}

pub fn failed_count(checks: &[Check]) -> usize {
    checks.iter().filter(|c| !c.ok).count()
}

/// First sensor line of `rpicam-vid --list-cameras`, e.g. "0 : ov5647 [2592x1944 ...] (...)".
pub fn parse_camera_list(output: &str) -> Option<String> {
    if !output.contains("Available cameras") {
        return None;
    }
    output
        .lines()
        .map(str::trim)
        .find(|line| line.starts_with(|c: char| c.is_ascii_digit()) && line.contains(" : "))
        .map(str::to_string)
}

pub fn rms_dbfs(samples: &[i16]) -> f32 {
    if samples.is_empty() {
        return SILENCE_DBFS;
    }
    let mean_square = samples.iter().map(|&s| (s as f64).powi(2)).sum::<f64>() / samples.len() as f64;
    if mean_square == 0.0 {
        return SILENCE_DBFS;
    }
    (20.0 * (mean_square.sqrt() / 32768.0).log10()) as f32
}

async fn camera_check(camera_cmd: &str) -> Check {
    let run = tokio::process::Command::new(camera_cmd).arg("--list-cameras").output();
    match tokio::time::timeout(CAMERA_TIMEOUT, run).await {
        Ok(Ok(output)) => {
            let text = format!("{}{}", String::from_utf8_lossy(&output.stdout), String::from_utf8_lossy(&output.stderr));
            match parse_camera_list(&text) {
                Some(sensor) => Check::pass("camera", sensor),
                None => Check::fail("camera", text.lines().last().unwrap_or("no output").to_string()),
            }
        }
        Ok(Err(e)) => Check::fail("camera", format!("cannot run {camera_cmd}: {e}")),
        Err(_) => Check::fail("camera", "timed out listing cameras"),
    }
}

/// Privacy: the microphone is only opened while the LED is solid (and never with it dark).
#[cfg(any(test, target_os = "linux"))]
fn with_privacy_led(led: &mut dyn LedDriver, capture: impl FnOnce() -> Check) -> Check {
    while_solid(led, capture)
        .unwrap_or_else(|e| Check::fail("microphone", format!("privacy LED unavailable ({e}): microphone not tested")))
}

#[cfg(target_os = "linux")]
fn mic_check(config: &Config) -> Check {
    let mut led = match crate::led::GpioLed::open(&config.gpio.chip, config.gpio.led) {
        Ok(led) => led,
        Err(e) => {
            return Check::fail("microphone", format!("privacy LED unavailable ({e}): microphone not tested"));
        }
    };
    with_privacy_led(&mut led, || capture_one_second(&config.capture.audio_device))
}

#[cfg(target_os = "linux")]
fn capture_one_second(device: &str) -> Check {
    use crate::recorder::audio::alsa_source::factory;
    use crate::recorder::ogg_opus::FRAME_SAMPLES;

    let mut source = match factory(device.to_string())() {
        Ok(source) => source,
        Err(e) => return Check::fail("microphone", e.to_string()),
    };
    let mut frame = [0i16; FRAME_SAMPLES];
    let mut samples = Vec::with_capacity(FRAME_SAMPLES * 50);
    for _ in 0..50 {
        if let Err(e) = source.read_frame(&mut frame) {
            return Check::fail("microphone", e.to_string());
        }
        samples.extend_from_slice(&frame);
    }
    Check::pass("microphone", format!("1 s captured, level {:.1} dBFS", rms_dbfs(&samples)))
}

#[cfg(not(target_os = "linux"))]
fn mic_check(_config: &Config) -> Check {
    Check::fail("microphone", "audio capture needs Linux/ALSA")
}

pub async fn run_selftest(config_path: &Path) -> Vec<Check> {
    let config = match Config::load(config_path) {
        Ok(config) => config,
        Err(e) => return vec![Check::fail("config", e.to_string())],
    };
    let data = &config.paths.data_dir;
    let mut checks = vec![Check::pass("config", config_path.display().to_string())];
    checks.push(match load_identity(&data.join("keys")) {
        Ok(Some(identity)) => Check::pass("pairing", format!("device {}", identity.device_id)),
        Ok(None) => Check::fail("pairing", "not paired yet"),
        Err(e) => Check::fail("pairing", e.to_string()),
    });
    checks.push(if config.paths.sync_flag.exists() {
        Check::pass("clock", "NTP synchronised")
    } else {
        Check::fail("clock", "waiting for NTP")
    });
    checks.push(camera_check(&config.capture.camera_cmd).await);
    checks.push(mic_check(&config));
    checks.push(match StatvfsUsage::new(data).and_then(|d| d.used_pct()) {
        Ok(pct) => Check { name: "disk", ok: pct < DROP_ABOVE_PCT, detail: format!("{pct:.1}% used") },
        Err(e) => Check::fail("disk", e.to_string()),
    });
    checks
}

#[cfg(test)]
mod tests {
    use super::*;

    const CAMERAS: &str = "Available cameras\n-----------------\n0 : ov5647 [2592x1944 10-bit GBRG] (/base/soc/i2c0mux/i2c@1/ov5647@36)\n    Modes: 'SGBRG10_CSI2P' : 640x480 [58.92 fps - (16, 0)/2560x1920 crop]\n";

    #[test]
    fn camera_list_is_parsed() {
        assert_eq!(
            parse_camera_list(CAMERAS).as_deref(),
            Some("0 : ov5647 [2592x1944 10-bit GBRG] (/base/soc/i2c0mux/i2c@1/ov5647@36)")
        );
        assert_eq!(parse_camera_list("No cameras available!"), None);
    }

    #[test]
    fn rms_is_reported_in_dbfs() {
        assert_eq!(rms_dbfs(&[0; 320]), -120.0);
        let full_scale = vec![i16::MAX; 320];
        assert!(rms_dbfs(&full_scale) > -0.01);
        let half = vec![16384; 320];
        assert!((rms_dbfs(&half) + 6.02).abs() < 0.1);
    }

    #[test]
    fn report_lists_every_check_and_a_summary() {
        let report = format_report(&[Check::pass("config", "ok"), Check::fail("camera", "No cameras available")]);
        assert!(report.contains("[ OK ] config: ok"));
        assert!(report.contains("[FAIL] camera: No cameras available"));
        assert!(report.ends_with("1/2 checks passed\n"));
    }

    #[tokio::test]
    async fn missing_config_is_a_single_failed_check() {
        let checks = run_selftest(Path::new("/nonexistent/loopi.toml")).await;
        assert_eq!(checks.len(), 1);
        assert!(!checks[0].ok);
        assert_eq!(checks[0].name, "config");
    }

    #[tokio::test]
    async fn unpaired_device_reports_each_check() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("loopi.toml");
        let toml = format!(
            "[brain]\nurl = \"https://192.168.1.10:8443\"\n[capture]\ncamera_cmd = \"/nonexistent/rpicam-vid\"\n[paths]\ndata_dir = {:?}\nsync_flag = {:?}\n",
            dir.path(),
            dir.path().join("synced")
        );
        std::fs::write(&path, toml).unwrap();
        let checks = run_selftest(&path).await;
        let by_name = |n: &str| checks.iter().find(|c| c.name == n).unwrap().clone();
        assert!(by_name("config").ok);
        assert_eq!(by_name("pairing").detail, "not paired yet");
        assert!(!by_name("clock").ok);
        assert!(by_name("camera").detail.contains("cannot run"));
        assert!(by_name("disk").detail.ends_with("% used"));
        std::fs::write(dir.path().join("synced"), "").unwrap();
        assert!(run_selftest(&path).await.iter().any(|c| c.name == "clock" && c.ok));
    }

    #[test]
    fn failed_count_counts_only_failures() {
        assert_eq!(failed_count(&[Check::pass("a", ""), Check::fail("b", ""), Check::fail("c", "")]), 2);
        assert_eq!(failed_count(&[]), 0);
    }

    #[test]
    fn microphone_is_captured_only_while_the_led_is_solid() {
        let mut led = crate::testing::RecordingLed::default();
        let states = led.states.clone();
        let check = with_privacy_led(&mut led, || {
            assert_eq!(*states.lock().unwrap(), vec![true], "LED solid during capture");
            Check::pass("microphone", "1 s captured")
        });
        assert!(check.ok);
        assert_eq!(*led.states.lock().unwrap(), vec![true, false], "LED off afterwards");
    }

    #[test]
    fn microphone_is_not_opened_when_the_led_fails() {
        struct DeadLed;
        impl LedDriver for DeadLed {
            fn set(&mut self, _on: bool) -> std::io::Result<()> {
                Err(std::io::Error::other("line busy"))
            }
        }
        let check = with_privacy_led(&mut DeadLed, || panic!("must not capture"));
        assert!(!check.ok);
        assert!(check.detail.contains("privacy LED unavailable"), "{}", check.detail);
    }

    #[test]
    fn empty_input_is_silence() {
        assert_eq!(rms_dbfs(&[]), -120.0);
    }
}
