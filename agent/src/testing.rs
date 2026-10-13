//! Test doubles shared by unit tests and `tests/*.rs`. Not used by the production binary.
use std::fs;
use std::io;
use std::path::PathBuf;
use std::sync::Mutex;

use crate::disk::DiskUsage;

/// Disk usage fixed at a settable percentage.
pub struct FixedDisk(pub Mutex<f32>);

impl FixedDisk {
    pub fn new(pct: f32) -> Self {
        Self(Mutex::new(pct))
    }
}

impl DiskUsage for FixedDisk {
    fn used_pct(&self) -> io::Result<f32> {
        Ok(*self.0.lock().unwrap())
    }
}

/// Usage = `base + per_item × (queued items in dir)`, so deleting items frees "space".
pub struct CountingDisk {
    pub dir: PathBuf,
    pub base: f32,
    pub per_item: f32,
}

impl DiskUsage for CountingDisk {
    fn used_pct(&self) -> io::Result<f32> {
        let items = fs::read_dir(&self.dir)?
            .filter_map(Result::ok)
            .filter(|e| e.path().extension().is_some_and(|x| x == "meta"))
            .count();
        Ok(self.base + items as f32 * self.per_item)
    }
}

use chrono::{DateTime, Utc};
use std::sync::atomic::{AtomicBool, Ordering};

use crate::clock::Clock;

/// A clock that only moves when told to.
pub struct ManualClock {
    now: Mutex<DateTime<Utc>>,
    synced: AtomicBool,
}

impl ManualClock {
    pub fn new(now: DateTime<Utc>) -> Self {
        Self { now: Mutex::new(now), synced: AtomicBool::new(true) }
    }

    pub fn advance(&self, by: chrono::Duration) {
        let mut now = self.now.lock().unwrap();
        *now += by;
    }

    pub fn set_synced(&self, synced: bool) {
        self.synced.store(synced, Ordering::SeqCst);
    }
}

impl Clock for ManualClock {
    fn now(&self) -> DateTime<Utc> {
        *self.now.lock().unwrap()
    }

    fn is_synced(&self) -> bool {
        self.synced.load(Ordering::SeqCst)
    }
}

/// Real wall clock that always reports NTP as synchronised.
pub struct SyncedClock;

impl Clock for SyncedClock {
    fn now(&self) -> DateTime<Utc> {
        Utc::now()
    }

    fn is_synced(&self) -> bool {
        true
    }
}

use std::sync::Arc;

use crate::led::LedDriver;

/// LED that records every level it was set to.
#[derive(Default, Clone)]
pub struct RecordingLed {
    pub states: Arc<Mutex<Vec<bool>>>,
}

impl LedDriver for RecordingLed {
    fn set(&mut self, on: bool) -> io::Result<()> {
        self.states.lock().unwrap().push(on);
        Ok(())
    }
}
