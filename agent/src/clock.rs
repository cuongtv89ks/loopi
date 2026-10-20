//! Wall clock plus "has the clock been synchronised yet" (`[clock] source`): the flag file systemd-timesyncd creates
//! (the Pi), or the kernel's own NTP state (a network camera's agent in the cameras container).
use std::path::PathBuf;

use chrono::{DateTime, Utc};

use crate::config::{ClockSource, Config};

pub trait Clock: Send + Sync {
    fn now(&self) -> DateTime<Utc>;
    fn is_synced(&self) -> bool;
}

/// The kernel's largest maximum error (µs) that still counts as synchronised: systemd's `ntp_synced()`, what
/// `timedatectl` shows as "System clock synchronized".
pub const KERNEL_MAX_ERROR_US: i64 = 16_000_000;

/// systemd's rule on what `adjtimex` returned: the call worked and the maximum error is below 16 s. The clock state
/// (STA_UNSYNC, TIME_ERROR) is ignored, as systemd ignores it: it may be set only to keep the kernel off the RTC.
pub fn kernel_synced(rc: i32, maxerror: i64) -> bool {
    rc != -1 && maxerror < KERNEL_MAX_ERROR_US
}

/// Reads the kernel's NTP state. `modes` 0 only reads: no CAP_SYS_TIME needed, and Docker's default seccomp profile
/// allows it.
#[cfg(target_os = "linux")]
pub fn kernel_clock_synced() -> bool {
    // SAFETY: an all-zero `timex` is a valid value (plain integers), and adjtimex only writes into it.
    let mut tx: libc::timex = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::adjtimex(&mut tx) };
    // `maxerror` is a C long: i32 on a 32-bit Pi, i64 here.
    #[allow(clippy::useless_conversion)]
    let maxerror = i64::from(tx.maxerror);
    kernel_synced(rc, maxerror)
}

/// Dev hosts other than Linux: never synced this way.
#[cfg(not(target_os = "linux"))]
pub fn kernel_clock_synced() -> bool {
    false
}

pub struct SystemClock {
    source: ClockSource,
    sync_flag: PathBuf,
}

impl SystemClock {
    pub fn new(source: ClockSource, sync_flag: PathBuf) -> Self {
        Self { source, sync_flag }
    }

    /// The check `[clock] source` names; `paths.sync_flag` is the flag file.
    pub fn from_config(config: &Config) -> Self {
        Self::new(config.clock.source, config.paths.sync_flag.clone())
    }
}

impl Clock for SystemClock {
    fn now(&self) -> DateTime<Utc> {
        Utc::now()
    }

    fn is_synced(&self) -> bool {
        match self.source {
            ClockSource::Flag => self.sync_flag.exists(),
            ClockSource::Kernel => kernel_clock_synced(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_kernel_is_synced_below_systemds_16_second_maximum_error() {
        assert!(!kernel_synced(-1, 0), "the call failed");
        assert!(kernel_synced(0, 15_999_999));
        assert!(!kernel_synced(0, 16_000_000));
        assert!(kernel_synced(5, 0), "TIME_ERROR (STA_UNSYNC set): systemd ignores the state, as here");
    }

    #[test]
    fn the_real_kernel_check_answers() {
        let _ = kernel_clock_synced();
    }

    #[test]
    fn the_configured_source_decides_and_the_kernel_never_looks_at_the_flag() {
        let dir = tempfile::tempdir().unwrap();
        let flag = dir.path().join("synchronized");
        let clock = |source| SystemClock::new(source, flag.clone());
        assert!(!clock(ClockSource::Flag).is_synced());
        assert_eq!(clock(ClockSource::Kernel).is_synced(), kernel_clock_synced());
        std::fs::write(&flag, "").unwrap();
        assert!(clock(ClockSource::Flag).is_synced());
        assert_eq!(clock(ClockSource::Kernel).is_synced(), kernel_clock_synced());

        let text = format!("[brain]\nurl = \"https://x\"\n[paths]\nsync_flag = {flag:?}\n");
        assert!(SystemClock::from_config(&Config::parse(&text).unwrap()).is_synced(), "flag by default");
        std::fs::remove_file(&flag).unwrap();
        let kernel = Config::parse(&format!("{text}[clock]\nsource = \"kernel\"\n")).unwrap();
        assert_eq!(SystemClock::from_config(&kernel).is_synced(), kernel_clock_synced());
    }
}
