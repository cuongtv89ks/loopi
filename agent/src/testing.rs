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
