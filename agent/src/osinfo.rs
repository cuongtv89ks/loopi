//! What this device runs: the OS version and variant written at build time (/etc/loopi-release) and the boot slot on
//! the kernel command line (`rauc.slot=`, P4a design §4). Reported in every heartbeat.
use std::fs;
use std::path::Path;

use crate::ota::{is_valid_version, Slot, Variant};

pub const RELEASE_PATH: &str = "/etc/loopi-release";
pub const CMDLINE_PATH: &str = "/proc/cmdline";

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct OsInfo {
    pub version: Option<String>,
    pub variant: Option<Variant>,
    pub slot: Option<Slot>,
}

impl OsInfo {
    /// Unreadable or malformed files give unknown fields, never an error: a heartbeat with a value the contract
    /// refuses would be refused as a whole.
    pub fn read(release: &Path, cmdline: &Path) -> Self {
        let (version, variant) = fs::read_to_string(release).map(|text| parse_release(&text)).unwrap_or((None, None));
        let slot = fs::read_to_string(cmdline).ok().and_then(|text| parse_cmdline_slot(&text));
        Self { version, variant, slot }
    }
}

/// `LOOPI_OS_VERSION=<version>` and `LOOPI_OS_VARIANT=dev|prod` lines; anything else is ignored.
pub fn parse_release(text: &str) -> (Option<String>, Option<Variant>) {
    let value = |key: &str| text.lines().find_map(|line| line.trim().strip_prefix(key)).map(str::trim);
    let version = value("LOOPI_OS_VERSION=").filter(|v| is_valid_version(v)).map(String::from);
    let variant = value("LOOPI_OS_VARIANT=").and_then(Variant::parse);
    (version, variant)
}

/// The `rauc.slot=A|B` word of a kernel command line.
pub fn parse_cmdline_slot(cmdline: &str) -> Option<Slot> {
    cmdline.split_whitespace().find_map(|word| word.strip_prefix("rauc.slot=")).and_then(Slot::parse)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_version_variant_and_slot() {
        let dir = tempfile::tempdir().unwrap();
        let (release, cmdline) = (dir.path().join("loopi-release"), dir.path().join("cmdline"));
        std::fs::write(&release, "LOOPI_OS_VERSION=2026.10.21-a1b2c3d\nLOOPI_OS_VARIANT=prod\n").unwrap();
        std::fs::write(&cmdline, "root=/dev/mmcblk0p3 ro quiet rauc.slot=B panic=10\n").unwrap();
        let os = OsInfo::read(&release, &cmdline);
        assert_eq!(os, OsInfo { version: Some("2026.10.21-a1b2c3d".into()), variant: Some(Variant::Prod), slot: Some(Slot::B) });
    }

    #[test]
    fn os_info_drops_values_the_contract_refuses() {
        assert_eq!(parse_release("LOOPI_OS_VERSION=has space\nLOOPI_OS_VARIANT=beta\n"), (None, None));
        assert_eq!(parse_release("LOOPI_OS_VERSION=\n"), (None, None));
        assert_eq!(parse_cmdline_slot("root=/dev/mmcblk0p2 rauc.slot=C"), None);
        assert_eq!(parse_cmdline_slot("root=/dev/mmcblk0p2 xrauc.slot=A"), None);
        assert_eq!(parse_cmdline_slot("root=/dev/mmcblk0p2"), None);
    }

    #[test]
    fn missing_files_are_unknown_not_errors() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(OsInfo::read(&dir.path().join("no"), &dir.path().join("no")), OsInfo::default());
    }
}
