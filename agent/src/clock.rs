//! Wall clock plus "has the clock been synchronised yet" (`[clock] source`): the flag file systemd-timesyncd creates
//! (the Pi), or the kernel's own NTP state (a network camera's agent in the cameras container).
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

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

/// The wall clock and its "synchronised" answer for the configured source.
///
/// The kernel source is latched: once `adjtimex` has said "synced", this clock stays synced for its lifetime. The kernel's
/// maximum error grows 500 µs/s while no NTP server is reachable, so after about 8.9 h without internet it would cross
/// 16 s and say "not synced" again, and every network camera would stop recording on a clock that is still right to the
/// second. This is the same meaning the Pi's flag file has: it is created once and stays ("synced since boot"). The flag
/// source is read every time, as before.
pub struct SystemClock {
    source: ClockSource,
    sync_flag: PathBuf,
    kernel_check: Box<dyn Fn() -> bool + Send + Sync>,
    /// Set by the first synced kernel answer, never cleared. Relaxed is enough: it guards no other data.
    kernel_was_synced: AtomicBool,
}

impl SystemClock {
    pub fn new(source: ClockSource, sync_flag: PathBuf) -> Self {
        Self::with_kernel_check(source, sync_flag, kernel_clock_synced)
    }

    /// `new` with the kernel question injected: the tests answer it, production asks `adjtimex`.
    pub fn with_kernel_check(source: ClockSource, sync_flag: PathBuf, kernel_check: impl Fn() -> bool + Send + Sync + 'static) -> Self {
        Self { source, sync_flag, kernel_check: Box::new(kernel_check), kernel_was_synced: AtomicBool::new(false) }
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
            ClockSource::Kernel => {
                self.kernel_was_synced.load(Ordering::Relaxed) || {
                    let synced = (self.kernel_check)();
                    if synced {
                        self.kernel_was_synced.store(true, Ordering::Relaxed);
                    }
                    synced
                }
            }
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

    /// The answers a scripted kernel gives, one per question; after the last one it says "not synced".
    fn scripted_kernel(answers: &'static [bool]) -> SystemClock {
        let asked = std::sync::atomic::AtomicUsize::new(0);
        SystemClock::with_kernel_check(ClockSource::Kernel, PathBuf::from("/nonexistent/synchronized"), move || {
            let n = asked.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            answers.get(n).copied().unwrap_or(false)
        })
    }

    /// The kernel's maximum error grows 500 µs/s without NTP, so after about 8.9 h offline `adjtimex` says "not synced"
    /// again: once it has said "synced" the clock stays synced, or every network camera would stop.
    #[test]
    fn a_kernel_clock_that_was_synced_once_stays_synced() {
        let clock = scripted_kernel(&[false, false, true, false, false, false]);
        let seen: Vec<bool> = (0..6).map(|_| clock.is_synced()).collect();
        assert_eq!(seen, [false, false, true, true, true, true]);
    }

    #[test]
    fn a_kernel_clock_that_was_never_synced_is_not_synced() {
        let clock = scripted_kernel(&[false, false, false]);
        assert!(!clock.is_synced() && !clock.is_synced() && !clock.is_synced());
        assert!(!scripted_kernel(&[]).is_synced());
    }

    /// Each clock latches for itself: a new one starts from the kernel's answer.
    #[test]
    fn the_latch_belongs_to_one_clock() {
        let synced = scripted_kernel(&[true]);
        assert!(synced.is_synced());
        assert!(!scripted_kernel(&[false]).is_synced());
    }

    /// The flag file is the Pi's "synced since boot" and is read every time: removing it is "not synced" again.
    #[test]
    fn the_flag_source_is_not_latched() {
        let dir = tempfile::tempdir().unwrap();
        let flag = dir.path().join("synchronized");
        let clock = SystemClock::with_kernel_check(ClockSource::Flag, flag.clone(), || panic!("the flag source never asks the kernel"));
        assert!(!clock.is_synced());
        std::fs::write(&flag, "").unwrap();
        assert!(clock.is_synced());
        std::fs::remove_file(&flag).unwrap();
        assert!(!clock.is_synced());
    }
}
