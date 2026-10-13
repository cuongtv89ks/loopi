//! Wall clock plus "has NTP synchronised yet" (systemd-timesyncd creates a flag file).
use std::path::PathBuf;

use chrono::{DateTime, Utc};

pub trait Clock: Send + Sync {
    fn now(&self) -> DateTime<Utc>;
    fn is_synced(&self) -> bool;
}

pub struct SystemClock {
    sync_flag: PathBuf,
}

impl SystemClock {
    pub fn new(sync_flag: PathBuf) -> Self {
        Self { sync_flag }
    }
}

impl Clock for SystemClock {
    fn now(&self) -> DateTime<Utc> {
        Utc::now()
    }

    fn is_synced(&self) -> bool {
        self.sync_flag.exists()
    }
}
