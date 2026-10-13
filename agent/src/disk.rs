//! Disk guard: keep the SD card from filling up while the brain is unreachable.
use std::ffi::CString;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

use crate::item::ItemKind;
use crate::queue::Queue;

pub const DROP_ABOVE_PCT: f32 = 85.0;
pub const DROP_UNTIL_PCT: f32 = 80.0;
pub const STOP_ABOVE_PCT: f32 = 95.0;

pub trait DiskUsage: Send + Sync {
    fn used_pct(&self) -> io::Result<f32>;
}

pub struct StatvfsUsage {
    path: CString,
}

impl StatvfsUsage {
    pub fn new(path: &Path) -> io::Result<Self> {
        let path = CString::new(path.as_os_str().as_bytes()).map_err(io::Error::other)?;
        Ok(Self { path })
    }
}

impl DiskUsage for StatvfsUsage {
    fn used_pct(&self) -> io::Result<f32> {
        // SAFETY: `stat` is plain old data and `path` is a valid NUL-terminated string.
        let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::statvfs(self.path.as_ptr(), &mut stat) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let used = stat.f_blocks as f64 - stat.f_bfree as f64;
        let denominator = used + stat.f_bavail as f64;
        Ok(if denominator <= 0.0 { 0.0 } else { (used / denominator * 100.0) as f32 })
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DiskReport {
    pub used_pct: f32,
    pub dropped: u32,
    pub stop: bool,
}

/// At ≥ 85 % delete the oldest *video* items until usage is below 80 %. Audio and markers are
/// never deleted. `stop` is set when usage is still ≥ 95 % afterwards.
pub fn enforce(queue: &Queue, disk: &dyn DiskUsage) -> io::Result<DiskReport> {
    let mut used = disk.used_pct()?;
    let mut dropped = 0;
    if used >= DROP_ABOVE_PCT {
        while used >= DROP_UNTIL_PCT {
            let Some(item) = queue.oldest_of_kind(ItemKind::Video)? else { break };
            queue.remove(&item.meta)?;
            dropped += 1;
            used = disk.used_pct()?;
        }
    }
    Ok(DiskReport { used_pct: used, dropped, stop: used >= STOP_ABOVE_PCT })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::CountingDisk;
    use chrono::Utc;

    fn queue_with(kinds: &[ItemKind]) -> (tempfile::TempDir, Queue) {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        for kind in kinds {
            queue.enqueue_bytes(b"x", *kind, Utc::now(), Utc::now()).unwrap();
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
        (tmp, queue)
    }

    fn disk(queue: &Queue, base: f32, per_item: f32) -> CountingDisk {
        CountingDisk { dir: queue.items_dir().to_path_buf(), base, per_item }
    }

    #[test]
    fn below_threshold_nothing_happens() {
        let (_t, queue) = queue_with(&[ItemKind::Video]);
        let report = enforce(&queue, &disk(&queue, 10.0, 1.0)).unwrap();
        assert_eq!(report, DiskReport { used_pct: 11.0, dropped: 0, stop: false });
    }

    #[test]
    fn enforce_drops_only_video() {
        use ItemKind::*;
        let (_t, queue) = queue_with(&[Video, Audio, Video, Marker, Video, Audio]);
        // 64 + 6×4 = 88 % → drop videos until < 80 %: 84, 80, 76
        let report = enforce(&queue, &disk(&queue, 64.0, 4.0)).unwrap();
        assert_eq!(report.dropped, 3);
        assert!(!report.stop);
        assert!(queue.oldest_of_kind(Video).unwrap().is_none());
        assert_eq!(queue.len().unwrap(), 3, "both audio items and the marker survive");
    }

    #[test]
    fn stop_when_still_full_after_dropping_all_video() {
        let (_t, queue) = queue_with(&[ItemKind::Video, ItemKind::Audio]);
        let report = enforce(&queue, &disk(&queue, 95.0, 1.0)).unwrap();
        assert_eq!(report.dropped, 1);
        assert!(report.stop);
        assert_eq!(queue.len().unwrap(), 1);
    }

    #[test]
    fn statvfs_reports_a_percentage() {
        let pct = StatvfsUsage::new(Path::new("/")).unwrap().used_pct().unwrap();
        assert!((0.0..=100.0).contains(&pct));
    }
}
