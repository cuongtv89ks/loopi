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
/// Brain per-segment duration limit: `x-loopi-max-segment-seconds` in `docs/protocol/v1.openapi.yaml`.
pub const MAX_SEGMENT_SECONDS: u32 = 3600;

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
            camera_cmd: "rpicam-vid".into(),
        }
    }
}

/// Upper bound on the size of one video segment at the configured bitrate.
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
        if c.segment_seconds > MAX_SEGMENT_SECONDS {
            return invalid(format!(
                "capture.segment_seconds must be at most {MAX_SEGMENT_SECONDS}, the brain's limit (got {})",
                c.segment_seconds
            ));
        }
        if !(10..=600).contains(&c.segment_seconds) {
            return invalid(format!("capture.segment_seconds must be 10..=600 (got {})", c.segment_seconds));
        }
        let max_bytes = max_segment_bytes(c);
        if max_bytes > MAX_ITEM_BYTES {
            return invalid(format!(
                "capture.video_bitrate_bps x segment_seconds allows {max_bytes}-byte segments, \
                 above the brain's {MAX_ITEM_BYTES}-byte item limit"
            ));
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
    let printable = wifi.psk.chars().all(|c| c.is_ascii() && !c.is_ascii_control() && c != '"');
    if !(8..=63).contains(&wifi.psk.len()) || !printable {
        return Err("wifi.psk must be 8..=63 printable ASCII characters without '\"' (or 64 hex chars)".into());
    }
    Ok(())
}

/// wpa_supplicant config. The SSID is hex-encoded so any UTF-8 name works without escaping.
pub fn render_wpa_conf(wifi: &WifiConfig) -> String {
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
    let fresh = config.wifi.as_ref().is_some_and(|w| !w.psk.is_empty()) || config.brain.pair_token.is_some();
    if !fresh {
        return Ok(ImportOutcome::AlreadyImported);
    }
    let config_dir = data_dir.join("config");
    fs::create_dir_all(&config_dir).map_err(io_err(&config_dir))?;
    let data_config = config_dir.join(CONFIG_FILE);
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
    fn default_config_fits_brain_item_limit() {
        let c = CaptureConfig::default();
        assert!(max_segment_bytes(&c) <= MAX_ITEM_BYTES);
        assert!(Config::parse(MINIMAL).is_ok());
    }

    #[test]
    fn segment_that_can_exceed_brain_item_limit_is_rejected() {
        // 10 Mbps x 600 s / 8 = 750 MB > 64 MiB.
        let text = format!("{MINIMAL}[capture]\nvideo_bitrate_bps = 10000000\nsegment_seconds = 600\n");
        let err = Config::parse(&text).unwrap_err().to_string();
        assert!(err.contains("67108864"), "{err}");
        // Just under the limit passes: 8 Mbps x 60 s / 8 = 60 MB.
        let ok = format!("{MINIMAL}[capture]\nvideo_bitrate_bps = 8000000\nsegment_seconds = 60\n");
        assert!(Config::parse(&ok).is_ok());
    }

    #[test]
    fn segment_longer_than_brain_limit_is_rejected() {
        let mut config = Config::parse(MINIMAL).unwrap();
        config.capture.segment_seconds = MAX_SEGMENT_SECONDS + 1;
        let err = config.validate().unwrap_err().to_string();
        assert!(err.contains("3600"), "{err}");
        config.capture.segment_seconds = MAX_SEGMENT_SECONDS;
        assert!(config.validate().is_err(), "still outside the agent's own 10..=600 range");
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
}
