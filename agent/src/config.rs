//! Agent configuration (`loopi.toml`) and first-boot import from the boot partition.
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::fsutil::write_atomic;
use crate::schedule::Schedule;

pub const CONFIG_FILE: &str = "loopi.toml";
pub const WPA_CONF_FILE: &str = "wpa_supplicant-wlan0.conf";
const PRIVATE: u32 = 0o600;
const PUBLIC: u32 = 0o644;

/// Brain per-item upload limit: `x-loopi-max-item-bytes` in `docs/protocol/v1.openapi.yaml` (64 MiB).
pub const MAX_ITEM_BYTES: u64 = 67_108_864;
/// Share of `MAX_ITEM_BYTES` a segment may use at the *nominal* bitrate. The rest absorbs encoder
/// overshoot (H.264 rate control is not exact) and segments that run past their nominal length.
const ITEM_BUDGET_PERCENT: u64 = 75;
/// Largest nominal segment (`bitrate x segment_seconds / 8`) the config accepts.
pub const MAX_NOMINAL_SEGMENT_BYTES: u64 = MAX_ITEM_BYTES * ITEM_BUDGET_PERCENT / 100;
/// Brain per-segment duration limit: `x-loopi-max-segment-seconds` in `docs/protocol/v1.openapi.yaml`.
pub const CONTRACT_MAX_SEGMENT_SECONDS: u32 = 3600;
/// The agent's own accepted segment length range (validated in `Config::validate`).
const SEGMENT_SECONDS_MIN: u32 = 10;
const SEGMENT_SECONDS_MAX: u32 = 600;
// The agent can never be configured past the brain's segment duration limit.
const _: () = assert!(SEGMENT_SECONDS_MAX <= CONTRACT_MAX_SEGMENT_SECONDS);
/// `capture.audio_gain_db`: digital microphone gain before encoding (spec D19). The USB mic records speech at
/// about -55 dBFS RMS even with its own AGC on.
pub const DEFAULT_AUDIO_GAIN_DB: f32 = 20.0;
pub const MAX_AUDIO_GAIN_DB: f32 = 36.0;

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("cannot access {path}: {source}")]
    Io { path: PathBuf, source: std::io::Error },
    #[error("invalid TOML: {0}")]
    Toml(#[from] toml::de::Error),
    #[error("cannot serialise config: {0}")]
    Serialize(#[from] toml::ser::Error),
    #[error("invalid config: {0}")]
    Invalid(String),
}

fn io_err(path: &Path) -> impl FnOnce(std::io::Error) -> ConfigError + '_ {
    move |source| ConfigError::Io { path: path.to_path_buf(), source }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wifi: Option<WifiConfig>,
    pub brain: BrainConfig,
    #[serde(default)]
    pub capture: CaptureConfig,
    #[serde(default)]
    pub schedule: ScheduleConfig,
    #[serde(default)]
    pub led: LedConfig,
    #[serde(default)]
    pub gpio: GpioConfig,
    #[serde(default)]
    pub paths: PathsConfig,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WifiConfig {
    pub ssid: String,
    #[serde(default)]
    pub psk: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub country: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BrainConfig {
    pub url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pair_token: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ca_fingerprint: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct CaptureConfig {
    pub video_width: u32,
    pub video_height: u32,
    pub video_fps: u32,
    pub video_bitrate_bps: u32,
    pub segment_seconds: u32,
    pub audio_enabled: bool,
    pub audio_device: String,
    pub audio_gain_db: f32,
    pub camera_cmd: String,
}

impl Default for CaptureConfig {
    fn default() -> Self {
        Self {
            video_width: 1920,
            video_height: 1080,
            video_fps: 2,
            video_bitrate_bps: 800_000,
            segment_seconds: 60,
            audio_enabled: true,
            audio_device: "auto".into(),
            audio_gain_db: DEFAULT_AUDIO_GAIN_DB,
            camera_cmd: "rpicam-vid".into(),
        }
    }
}

/// Size of one video segment at the configured (nominal) bitrate.
pub fn max_segment_bytes(capture: &CaptureConfig) -> u64 {
    u64::from(capture.video_bitrate_bps) * u64::from(capture.segment_seconds) / 8
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ScheduleConfig {
    pub timezone: String,
    pub quiet_hours: Vec<String>,
}

impl Default for ScheduleConfig {
    fn default() -> Self {
        Self { timezone: "Asia/Seoul".into(), quiet_hours: Vec::new() }
    }
}

/// The privacy LED when nothing else is configured: the Pi Zero W's green ACT LED (spec D18).
pub const DEFAULT_LED_PATH: &str = "/sys/class/leds/ACT";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LedSource {
    /// A Linux LED class device: the directory `led.path`.
    #[default]
    Sysfs,
    /// An external LED on line `gpio.led` of `gpio.chip`.
    Gpio,
}

/// `[led]`: which LED is the privacy LED. The default needs no config (the ACT LED).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct LedConfig {
    pub source: LedSource,
    /// LED class directory, used when `source = "sysfs"`.
    pub path: PathBuf,
}

impl Default for LedConfig {
    fn default() -> Self {
        Self { source: LedSource::Sysfs, path: DEFAULT_LED_PATH.into() }
    }
}

/// `[gpio]`: the button line and, with `led.source = "gpio"`, the LED line.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct GpioConfig {
    pub chip: String,
    pub led: u32,
    pub button: u32,
}

impl Default for GpioConfig {
    fn default() -> Self {
        Self { chip: "/dev/gpiochip0".into(), led: 17, button: 27 }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct PathsConfig {
    pub data_dir: PathBuf,
    pub sync_flag: PathBuf,
}

impl Default for PathsConfig {
    fn default() -> Self {
        Self { data_dir: "/data".into(), sync_flag: "/run/systemd/timesync/synchronized".into() }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImportOutcome {
    NoBootConfig,
    AlreadyImported,
    Imported,
}

impl Config {
    pub fn parse(text: &str) -> Result<Self, ConfigError> {
        let config: Config = toml::from_str(text)?;
        config.validate()?;
        Ok(config)
    }

    pub fn load(path: &Path) -> Result<Self, ConfigError> {
        let text = fs::read_to_string(path).map_err(io_err(path))?;
        Self::parse(&text)
    }

    pub fn validate(&self) -> Result<(), ConfigError> {
        let invalid = |msg: String| Err(ConfigError::Invalid(msg));
        if !self.brain.url.starts_with("https://") {
            return invalid(format!("brain.url must start with https:// (got {:?})", self.brain.url));
        }
        if let Some(fp) = &self.brain.ca_fingerprint {
            if normalize_fingerprint(fp).is_none() {
                return invalid("brain.ca_fingerprint must be a SHA-256 hex fingerprint".into());
            }
        }
        if self.led.source == LedSource::Sysfs && !self.led.path.is_absolute() {
            return invalid(format!(
                "led.path must be an absolute LED class directory such as {DEFAULT_LED_PATH} (got {:?})",
                self.led.path
            ));
        }
        let c = &self.capture;
        if !(1..=30).contains(&c.video_fps) {
            return invalid(format!("capture.video_fps must be 1..=30 (got {})", c.video_fps));
        }
        if !(64..=4608).contains(&c.video_width) || c.video_width % 2 != 0 {
            return invalid(format!("capture.video_width must be even and 64..=4608 (got {})", c.video_width));
        }
        if !(64..=2592).contains(&c.video_height) || c.video_height % 2 != 0 {
            return invalid(format!("capture.video_height must be even and 64..=2592 (got {})", c.video_height));
        }
        if !(100_000..=10_000_000).contains(&c.video_bitrate_bps) {
            return invalid(format!(
                "capture.video_bitrate_bps must be 100000..=10000000 (got {})",
                c.video_bitrate_bps
            ));
        }
        if !(SEGMENT_SECONDS_MIN..=SEGMENT_SECONDS_MAX).contains(&c.segment_seconds) {
            return invalid(format!(
                "capture.segment_seconds must be {SEGMENT_SECONDS_MIN}..={SEGMENT_SECONDS_MAX} (got {})",
                c.segment_seconds
            ));
        }
        let max_bytes = max_segment_bytes(c);
        if max_bytes > MAX_NOMINAL_SEGMENT_BYTES {
            return invalid(format!(
                "capture.video_bitrate_bps x segment_seconds allows {max_bytes}-byte segments, above \
                 {MAX_NOMINAL_SEGMENT_BYTES} bytes ({ITEM_BUDGET_PERCENT} % of the brain's {MAX_ITEM_BYTES}-byte \
                 item limit, leaving room for encoder overshoot)"
            ));
        }
        // NaN fails `contains` too.
        if !(0.0..=MAX_AUDIO_GAIN_DB).contains(&c.audio_gain_db) {
            return invalid(format!("capture.audio_gain_db must be 0..={MAX_AUDIO_GAIN_DB} dB (got {})", c.audio_gain_db));
        }
        if c.audio_device.trim().is_empty() || c.camera_cmd.trim().is_empty() {
            return invalid("capture.audio_device and capture.camera_cmd must not be empty".into());
        }
        Schedule::from_config(&self.schedule).map_err(ConfigError::Invalid)?;
        if let Some(wifi) = &self.wifi {
            validate_wifi(wifi).map_err(ConfigError::Invalid)?;
        }
        Ok(())
    }
}

fn is_hex64(s: &str) -> bool {
    s.len() == 64 && s.chars().all(|c| c.is_ascii_hexdigit())
}

/// Lowercase hex without separators, or `None` if `raw` is not a SHA-256 fingerprint.
pub fn normalize_fingerprint(raw: &str) -> Option<String> {
    let cleaned: String = raw
        .chars()
        .filter(|c| *c != ':' && !c.is_whitespace())
        .collect::<String>()
        .to_ascii_lowercase();
    is_hex64(&cleaned).then_some(cleaned)
}

fn validate_wifi(wifi: &WifiConfig) -> Result<(), String> {
    if wifi.ssid.is_empty() || wifi.ssid.len() > 32 {
        return Err("wifi.ssid must be 1..=32 bytes".into());
    }
    if let Some(cc) = &wifi.country {
        if cc.len() != 2 || !cc.chars().all(|c| c.is_ascii_uppercase()) {
            return Err("wifi.country must be a 2-letter uppercase code such as KR".into());
        }
    }
    // Empty psk means an open network (or a scrubbed boot copy).
    if wifi.psk.is_empty() || is_hex64(&wifi.psk) {
        return Ok(());
    }
    // WPA passphrases are 8..=63 printable ASCII. A backslash is allowed: wpa_supplicant reads the
    // quoted value raw (no escape processing). `"` and control characters (newline!) would let a
    // value break out of the quoted line and inject config, so they are rejected.
    let printable = wifi.psk.chars().all(|c| c.is_ascii() && !c.is_ascii_control() && c != '"');
    if !(8..=63).contains(&wifi.psk.len()) || !printable {
        return Err("wifi.psk must be 8..=63 printable ASCII characters without '\"' (or 64 hex chars)".into());
    }
    Ok(())
}

/// wpa_supplicant config. The SSID is hex-encoded so any UTF-8 name works without escaping.
/// `wifi` must already have passed `Config::validate`: psk and country are interpolated unescaped.
pub(crate) fn render_wpa_conf(wifi: &WifiConfig) -> String {
    let mut out = String::from("ctrl_interface=/run/wpa_supplicant\nupdate_config=0\n");
    if let Some(cc) = &wifi.country {
        out.push_str(&format!("country={cc}\n"));
    }
    out.push_str("network={\n");
    out.push_str(&format!("    ssid={}\n", hex::encode(wifi.ssid.as_bytes())));
    if wifi.psk.is_empty() {
        out.push_str("    key_mgmt=NONE\n");
    } else if is_hex64(&wifi.psk) {
        out.push_str(&format!("    psk={}\n", wifi.psk.to_ascii_lowercase()));
    } else {
        out.push_str(&format!("    psk=\"{}\"\n", wifi.psk));
    }
    out.push_str("}\n");
    out
}

/// Copy of `config` without the WiFi passphrase and the pairing token.
pub fn scrub_secrets(config: &Config) -> Config {
    let mut scrubbed = config.clone();
    if let Some(wifi) = scrubbed.wifi.as_mut() {
        wifi.psk.clear();
    }
    scrubbed.brain.pair_token = None;
    scrubbed
}

/// First-boot provisioning: copy a *fresh* boot-partition config (one that still carries a WiFi
/// passphrase or a pair token) to `<data_dir>/config/`, write the wpa_supplicant config, then
/// remove the secrets from the boot copy. Nothing is written if the boot config is invalid.
pub fn import_boot_config(boot_toml: &Path, data_dir: &Path) -> Result<ImportOutcome, ConfigError> {
    if !boot_toml.exists() {
        return Ok(ImportOutcome::NoBootConfig);
    }
    let config = Config::load(boot_toml)?;
    let config_dir = data_dir.join("config");
    let data_config = config_dir.join(CONFIG_FILE);
    // Fresh = still holds a secret, or was never imported (covers open-network first boot).
    let has_secret = config.wifi.as_ref().is_some_and(|w| !w.psk.is_empty())
        || config.brain.pair_token.as_deref().is_some_and(|t| !t.is_empty());
    if !has_secret && data_config.exists() {
        return Ok(ImportOutcome::AlreadyImported);
    }
    fs::create_dir_all(&config_dir).map_err(io_err(&config_dir))?;
    write_atomic(&data_config, toml::to_string(&config)?.as_bytes(), PRIVATE).map_err(io_err(&data_config))?;
    if let Some(wifi) = &config.wifi {
        let wpa = config_dir.join(WPA_CONF_FILE);
        write_atomic(&wpa, render_wpa_conf(wifi).as_bytes(), PRIVATE).map_err(io_err(&wpa))?;
    }
    write_atomic(boot_toml, toml::to_string(&scrub_secrets(&config))?.as_bytes(), PUBLIC)
        .map_err(io_err(boot_toml))?;
    Ok(ImportOutcome::Imported)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    const FP: &str = "abababababababababababababababababababababababababababababababab";
    const MINIMAL: &str = "[brain]\nurl = \"https://192.168.1.10:8443\"\n";

    fn fresh() -> String {
        format!(
            "[wifi]\nssid = \"Nhà Cường\"\npsk = \"secret-password\"\ncountry = \"KR\"\n\n\
             [brain]\nurl = \"https://192.168.1.10:8443\"\npair_token = \"ABCD-EFGH\"\nca_fingerprint = \"{FP}\"\n"
        )
    }

    #[test]
    fn minimal_config_gets_defaults() {
        let c = Config::parse(MINIMAL).unwrap();
        assert_eq!(c.capture, CaptureConfig::default());
        assert_eq!((c.capture.video_fps, c.capture.segment_seconds), (2, 60));
        assert_eq!(c.capture.audio_device, "auto");
        assert_eq!(c.schedule.timezone, "Asia/Seoul");
        assert_eq!(c.gpio.led, 17);
        assert_eq!(c.paths.data_dir, PathBuf::from("/data"));
    }

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
    fn a_capture_table_without_audio_gain_db_keeps_the_default() {
        // The shape of an already provisioned device (flash-dev.sh writes `[capture]` with video_fps and
        // audio_enabled only): the other keys of the table must still default, `audio_gain_db` to 20 dB, not 0.
        let c = Config::parse(&format!("{MINIMAL}[capture]\nvideo_fps = 2\n")).unwrap();
        assert_eq!(c.capture.audio_gain_db, DEFAULT_AUDIO_GAIN_DB);
        assert_eq!(c.capture.audio_gain_db, 20.0);
        let c = Config::parse(&format!("{MINIMAL}[capture]\nvideo_fps = 1\naudio_enabled = false\n")).unwrap();
        assert_eq!((c.capture.video_fps, c.capture.audio_enabled), (1, false));
        assert_eq!(c.capture.audio_gain_db, DEFAULT_AUDIO_GAIN_DB);
    }

    #[test]
    fn audio_gain_outside_0_to_36_db_is_rejected() {
        for value in ["-1", "-0.5", "36.5", "100", "nan", "inf", "-inf"] {
            let err = Config::parse(&format!("{MINIMAL}[capture]\naudio_gain_db = {value}\n")).unwrap_err().to_string();
            assert!(err.contains("capture.audio_gain_db must be 0..=36 dB"), "{value}: {err}");
        }
        assert!(Config::parse(&format!("{MINIMAL}[capture]\naudio_gain_db = \"20\"\n")).is_err(), "a string is not a number");
    }

    #[test]
    fn the_privacy_led_defaults_to_the_act_led() {
        let c = Config::parse(MINIMAL).unwrap();
        assert_eq!(c.led, LedConfig { source: LedSource::Sysfs, path: PathBuf::from("/sys/class/leds/ACT") });
        assert_eq!((c.gpio.chip.as_str(), c.gpio.led, c.gpio.button), ("/dev/gpiochip0", 17, 27));
    }

    #[test]
    fn the_led_source_is_selectable_and_survives_the_import_round_trip() {
        let gpio = Config::parse(&format!("{MINIMAL}[led]\nsource = \"gpio\"\n[gpio]\nled = 22\n")).unwrap();
        assert_eq!((gpio.led.source, gpio.gpio.chip.as_str(), gpio.gpio.led), (LedSource::Gpio, "/dev/gpiochip0", 22));
        let other = Config::parse(&format!("{MINIMAL}[led]\npath = \"/sys/class/leds/led0\"\n")).unwrap();
        assert_eq!(other.led, LedConfig { source: LedSource::Sysfs, path: "/sys/class/leds/led0".into() });
        // `import_boot_config` writes the data copy with toml::to_string.
        for config in [gpio, other, Config::parse(MINIMAL).unwrap()] {
            assert_eq!(Config::parse(&toml::to_string(&config).unwrap()).unwrap(), config);
        }
    }

    #[test]
    fn default_config_fits_brain_item_limit() {
        let c = CaptureConfig::default();
        assert!(max_segment_bytes(&c) <= MAX_NOMINAL_SEGMENT_BYTES);
        assert_eq!(MAX_NOMINAL_SEGMENT_BYTES, 50_331_648, "75 % of 64 MiB");
        assert!(Config::parse(MINIMAL).is_ok());
    }

    #[test]
    fn segment_that_can_exceed_brain_item_limit_is_rejected() {
        // 10 Mbps x 600 s / 8 = 750 MB > 64 MiB.
        let text = format!("{MINIMAL}[capture]\nvideo_bitrate_bps = 10000000\nsegment_seconds = 600\n");
        let err = Config::parse(&text).unwrap_err().to_string();
        assert!(err.contains("67108864"), "{err}");
        // 8 Mbps x 60 s / 8 = 60 MB fits 64 MiB only nominally: no room for overshoot.
        let tight = format!("{MINIMAL}[capture]\nvideo_bitrate_bps = 8000000\nsegment_seconds = 60\n");
        assert!(Config::parse(&tight).is_err());
        // 6 Mbps x 60 s / 8 = 45 MB is inside the 75 % budget.
        let ok = format!("{MINIMAL}[capture]\nvideo_bitrate_bps = 6000000\nsegment_seconds = 60\n");
        assert!(Config::parse(&ok).is_ok());
    }

    #[test]
    fn item_size_limit_boundary_is_exact() {
        let with = |bitrate: u32, secs: u32| {
            let mut c = Config::parse(MINIMAL).unwrap();
            c.capture.video_bitrate_bps = bitrate;
            c.capture.segment_seconds = secs;
            c
        };
        // 786_432 bps x 512 s / 8 == 50_331_648 bytes == 75 % of 64 MiB exactly.
        let at_limit = with(786_432, 512);
        assert_eq!(max_segment_bytes(&at_limit.capture), MAX_NOMINAL_SEGMENT_BYTES);
        assert!(at_limit.validate().is_ok());
        // 786_433 bps x 512 s / 8 == 50_331_712 bytes, over the budget.
        let over = with(786_433, 512);
        assert!(max_segment_bytes(&over.capture) > MAX_NOMINAL_SEGMENT_BYTES);
        assert!(over.validate().is_err());
    }

    #[test]
    fn wifi_values_that_could_inject_wpa_config_are_rejected() {
        // Built directly (not via TOML) so raw control characters reach `validate`.
        let with = |psk: &str, country: Option<&str>| {
            let mut c = Config::parse(MINIMAL).unwrap();
            c.wifi = Some(WifiConfig { ssid: "x".into(), psk: psk.into(), country: country.map(Into::into) });
            c
        };
        assert!(with("longenough", Some("KR")).validate().is_ok());
        let bad_psks = [
            "password\nnetwork={",
            "pass\rword1",
            "pass\u{7}word1",
            "pass\tword1",
            "pass\u{7f}word1",
            "pässword123",
            "pass\"word12",
        ];
        for psk in bad_psks {
            assert!(with(psk, None).validate().is_err(), "psk {psk:?} should be rejected");
        }
        for cc in ["kr", "K", "KRR", "K\nR", "K1", "한국", ""] {
            assert!(with("longenough", Some(cc)).validate().is_err(), "country {cc:?} should be rejected");
        }
    }

    #[test]
    fn backslash_in_psk_is_allowed_and_rendered_raw() {
        let text = format!("[wifi]\nssid = \"x\"\npsk = \"back\\\\slash1\"\n{MINIMAL}");
        let config = Config::parse(&text).unwrap();
        let wifi = config.wifi.unwrap();
        assert_eq!(wifi.psk, "back\\slash1");
        assert!(render_wpa_conf(&wifi).contains("psk=\"back\\slash1\"\n"));
    }

    #[test]
    fn invalid_values_are_rejected() {
        let cases = [
            "[brain]\nurl = \"http://192.168.1.10:8443\"\n".to_string(),
            format!("{MINIMAL}[capture]\nvideo_fps = 0\n"),
            format!("{MINIMAL}[capture]\nvideo_width = 1921\n"),
            format!("{MINIMAL}[capture]\nsegment_seconds = 5\n"),
            format!("{MINIMAL}[capture]\nvideo_bitrate_bps = 10\n"),
            format!("{MINIMAL}[schedule]\ntimezone = \"Nowhere\"\n"),
            format!("{MINIMAL}[schedule]\nquiet_hours = [\"bad\"]\n"),
            format!("{MINIMAL}[capture]\nunknown_key = 1\n"),
            format!("[wifi]\nssid = \"x\"\npsk = \"short\"\n{MINIMAL}"),
            format!("[wifi]\nssid = \"x\"\npsk = \"has\\\"quote-inside\"\n{MINIMAL}"),
            format!("[wifi]\nssid = \"x\"\npsk = \"longenough\"\ncountry = \"korea\"\n{MINIMAL}"),
            "[brain]\nurl = \"https://x\"\nca_fingerprint = \"zz\"\n".to_string(),
            format!("{MINIMAL}[led]\nsource = \"pwm\"\n"),
            format!("{MINIMAL}[led]\npath = \"leds/ACT\"\n"),
            format!("{MINIMAL}[led]\npath = \"\"\n"),
            format!("{MINIMAL}[led]\ncolour = \"red\"\n"),
        ];
        for case in cases {
            assert!(Config::parse(&case).is_err(), "should reject:\n{case}");
        }
    }

    #[test]
    fn fingerprints_are_normalised() {
        let colon = FP
            .as_bytes()
            .chunks(2)
            .map(|c| std::str::from_utf8(c).unwrap().to_uppercase())
            .collect::<Vec<_>>()
            .join(":");
        assert_eq!(normalize_fingerprint(&colon).as_deref(), Some(FP));
        assert_eq!(normalize_fingerprint("abc"), None);
    }

    #[test]
    fn wpa_conf_hex_encodes_ssid_and_quotes_passphrase() {
        let w = WifiConfig { ssid: "Nhà".into(), psk: "secret-password".into(), country: Some("KR".into()) };
        let conf = render_wpa_conf(&w);
        assert!(conf.contains("country=KR\n"));
        assert!(conf.contains(&format!("ssid={}\n", hex::encode("Nhà"))));
        assert!(conf.contains("psk=\"secret-password\"\n"));
    }

    #[test]
    fn wpa_conf_raw_psk_and_open_network() {
        let raw = WifiConfig { ssid: "a".into(), psk: FP.to_uppercase(), country: None };
        assert!(render_wpa_conf(&raw).contains(&format!("psk={FP}\n")));
        let open = WifiConfig { ssid: "a".into(), psk: String::new(), country: None };
        assert!(render_wpa_conf(&open).contains("key_mgmt=NONE"));
    }

    #[test]
    fn fresh_boot_config_is_imported_and_scrubbed() {
        let dir = tempfile::tempdir().unwrap();
        let boot = dir.path().join("loopi.toml");
        let data = dir.path().join("data");
        std::fs::write(&boot, fresh()).unwrap();

        assert_eq!(import_boot_config(&boot, &data).unwrap(), ImportOutcome::Imported);

        let data_config = data.join("config").join(CONFIG_FILE);
        let saved = Config::load(&data_config).unwrap();
        assert_eq!(saved.brain.pair_token.as_deref(), Some("ABCD-EFGH"));
        assert_eq!(saved.wifi.as_ref().unwrap().psk, "secret-password");
        let mode = std::fs::metadata(&data_config).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
        let wpa = std::fs::read_to_string(data.join("config").join(WPA_CONF_FILE)).unwrap();
        assert!(wpa.contains("psk=\"secret-password\""));

        let boot_after = std::fs::read_to_string(&boot).unwrap();
        assert!(!boot_after.contains("secret-password"));
        assert!(!boot_after.contains("ABCD-EFGH"));
        assert!(boot_after.contains(FP), "non-secret settings stay on the boot partition");

        // Second boot: nothing fresh, data copy untouched.
        assert_eq!(import_boot_config(&boot, &data).unwrap(), ImportOutcome::AlreadyImported);
        assert_eq!(Config::load(&data_config).unwrap(), saved);
    }

    #[test]
    fn missing_boot_config_is_not_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let outcome = import_boot_config(&dir.path().join("none.toml"), dir.path()).unwrap();
        assert_eq!(outcome, ImportOutcome::NoBootConfig);
    }

    #[test]
    fn invalid_boot_config_writes_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let boot = dir.path().join("loopi.toml");
        std::fs::write(&boot, "[brain]\nurl = \"http://insecure\"\npair_token = \"X\"\n").unwrap();
        assert!(import_boot_config(&boot, &dir.path().join("data")).is_err());
        assert!(!dir.path().join("data").exists());
    }

    #[test]
    fn open_network_first_boot_is_imported_once() {
        let dir = tempfile::tempdir().unwrap();
        let boot = dir.path().join("loopi.toml");
        let data = dir.path().join("data");
        std::fs::write(&boot, format!("[wifi]\nssid = \"cafe\"\n{MINIMAL}")).unwrap();

        assert_eq!(import_boot_config(&boot, &data).unwrap(), ImportOutcome::Imported);
        let wpa = std::fs::read_to_string(data.join("config").join(WPA_CONF_FILE)).unwrap();
        assert!(wpa.contains("key_mgmt=NONE"));
        assert!(wpa.contains(&format!("ssid={}", hex::encode("cafe"))));
        assert_eq!(import_boot_config(&boot, &data).unwrap(), ImportOutcome::AlreadyImported);
    }

    #[test]
    fn empty_pair_token_is_not_a_secret() {
        let dir = tempfile::tempdir().unwrap();
        let boot = dir.path().join("loopi.toml");
        let data = dir.path().join("data");
        std::fs::write(&boot, "[brain]\nurl = \"https://x\"\npair_token = \"\"\n").unwrap();
        assert_eq!(import_boot_config(&boot, &data).unwrap(), ImportOutcome::Imported);
        // Data copy now exists and nothing secret remains: not fresh any more.
        assert_eq!(import_boot_config(&boot, &data).unwrap(), ImportOutcome::AlreadyImported);
    }
}
