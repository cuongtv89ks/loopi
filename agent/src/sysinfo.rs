//! Device readings for the heartbeat and pairing (Linux /proc and /sys).
use std::fs;
use std::path::Path;

use crate::wire::{HwInfo, AGENT_VERSION};

const SOC_TEMP_PATH: &str = "/sys/class/thermal/thermal_zone0/temp";
const WIRELESS_PATH: &str = "/proc/net/wireless";
const WIFI_IFACE: &str = "wlan0";
const MAX_FIELD: usize = 100;

pub trait SysProbe: Send + Sync {
    fn soc_temp_c(&self) -> Option<f32>;
    fn wifi_rssi_dbm(&self) -> Option<i32>;
}

pub struct LinuxProbe;

impl SysProbe for LinuxProbe {
    fn soc_temp_c(&self) -> Option<f32> {
        read_soc_temp(Path::new(SOC_TEMP_PATH))
    }

    fn wifi_rssi_dbm(&self) -> Option<i32> {
        fs::read_to_string(WIRELESS_PATH).ok().and_then(|c| parse_wireless(&c, WIFI_IFACE))
    }
}

pub struct NoProbe;

impl SysProbe for NoProbe {
    fn soc_temp_c(&self) -> Option<f32> {
        None
    }

    fn wifi_rssi_dbm(&self) -> Option<i32> {
        None
    }
}

/// Temperature in degrees C from a millidegree sysfs file; non-finite values are rejected.
pub fn read_soc_temp(path: &Path) -> Option<f32> {
    let millis: f32 = fs::read_to_string(path).ok()?.trim().parse().ok()?;
    let celsius = millis / 1000.0;
    celsius.is_finite().then_some(celsius)
}

/// Signal level (dBm) of `iface` from `/proc/net/wireless` ("wlan0: 0000 54. -56. -256 ...").
pub fn parse_wireless(contents: &str, iface: &str) -> Option<i32> {
    contents.lines().find_map(|line| {
        let rest = line.trim_start().strip_prefix(iface)?.strip_prefix(':')?;
        let level = rest.split_whitespace().nth(2)?;
        let value = level.trim_end_matches('.').parse::<f32>().ok()?;
        value.is_finite().then_some(value as i32)
    })
}

pub fn parse_cpu_serial(cpuinfo: &str) -> Option<String> {
    cpuinfo.lines().find_map(|line| {
        let (key, value) = line.split_once(':')?;
        (key.trim() == "Serial").then(|| value.trim().to_string())
    })
}

fn clip(s: &str) -> String {
    s.chars().take(MAX_FIELD).collect()
}

pub fn hw_info() -> HwInfo {
    let model = fs::read_to_string("/proc/device-tree/model")
        .map(|m| m.trim_end_matches('\0').trim().to_string())
        .unwrap_or_else(|_| "unknown".into());
    let serial = fs::read_to_string("/proc/cpuinfo")
        .ok()
        .and_then(|c| parse_cpu_serial(&c))
        .unwrap_or_else(|| "unknown".into());
    HwInfo { model: clip(&model), serial: clip(&serial), agent_version: AGENT_VERSION.into() }
}

#[cfg(test)]
mod tests {
    use super::*;

    const WIRELESS: &str = "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n \
face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n \
 wlan0: 0000   54.  -56.  -256        0      0      0      0      0        0\n";

    #[test]
    fn rssi_is_read_from_proc_net_wireless() {
        assert_eq!(parse_wireless(WIRELESS, "wlan0"), Some(-56));
        assert_eq!(parse_wireless(WIRELESS, "wlan1"), None);
    }

    #[test]
    fn soc_temperature_is_millidegrees() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("temp");
        std::fs::write(&path, "48230\n").unwrap();
        assert_eq!(read_soc_temp(&path), Some(48.23));
        assert_eq!(read_soc_temp(&dir.path().join("missing")), None);
    }

    #[test]
    fn cpu_serial_is_parsed() {
        let cpuinfo = "processor\t: 0\nHardware\t: BCM2835\nSerial\t\t: 00000000a1b2c3d4\nModel\t\t: Raspberry Pi Zero W Rev 1.1\n";
        assert_eq!(parse_cpu_serial(cpuinfo).as_deref(), Some("00000000a1b2c3d4"));
    }

    #[test]
    fn non_finite_readings_are_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("temp");
        std::fs::write(&path, "NaN\n").unwrap();
        assert_eq!(read_soc_temp(&path), None);
        assert_eq!(parse_wireless(" wlan0: 0000 54. nan. -256\n", "wlan0"), None);
    }
}
