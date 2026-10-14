//! Durable on-disk upload queue.
//!
//! `<root>/items/<id>.<ext>` holds the payload and `<root>/items/<id>.meta` its JSON `ItemMeta`.
//! The payload is renamed into place first, then the sidecar is written atomically, so only items
//! with a sidecar are visible. `open()` removes anything a power cut left half-written.
//! `<root>/quarantine/` keeps items the brain will never accept, with a `.reason` file.
//!
//! Recovery never aborts on one bad entry (an unreadable file on a failing SD card would otherwise
//! stop the agent on every start): each problem is logged, the file is left in place or
//! quarantined, and the problem is reported in the `RecoveryReport`.
use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use sha2::{Digest, Sha256};

use crate::fsutil::write_atomic;
use crate::item::{ItemKind, ItemMeta};

const META_EXT: &str = "meta";
const TMP_EXT: &str = "tmp";
const FILE_MODE: u32 = 0o640;

#[derive(Debug, Clone, PartialEq)]
pub struct QueuedItem {
    pub meta: ItemMeta,
    pub data_path: PathBuf,
}

#[derive(Debug)]
pub struct Queue {
    items: PathBuf,
    quarantine: PathBuf,
}

/// Outcome of a crash-recovery pass over the queue or an `incoming/` directory.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct RecoveryReport {
    /// Interrupted segments queued.
    pub recovered: u32,
    /// Leftovers deleted (temp files, orphans).
    pub dropped: u32,
    /// One line per entry that could not be handled; those files were left where they were.
    pub problems: Vec<String>,
}

impl RecoveryReport {
    /// Log and record a problem with `path`; recovery carries on with the next entry.
    pub fn problem(&mut self, path: &Path, what: &str, error: &dyn std::fmt::Display) {
        tracing::error!(path = %path.display(), %error, "recovery: {what}");
        self.problems.push(format!("{}: {what}: {error}", path.display()));
    }

    #[must_use]
    pub fn merge(self, other: RecoveryReport) -> RecoveryReport {
        RecoveryReport {
            recovered: self.recovered + other.recovered,
            dropped: self.dropped + other.dropped,
            problems: self.problems.into_iter().chain(other.problems).collect(),
        }
    }

    /// Short note for `last_error`, or `None` when everything was handled.
    pub fn summary(&self) -> Option<String> {
        let first = self.problems.first()?;
        Some(format!("startup recovery: {} file(s) left in place; first: {first}", self.problems.len()))
    }
}

pub fn sha256_file(path: &Path) -> io::Result<(String, u64)> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 64 * 1024];
    let mut size = 0u64;
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        size += n as u64;
    }
    Ok((hex::encode(hasher.finalize()), size))
}

fn remove_if_exists(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Err(e) if e.kind() != io::ErrorKind::NotFound => Err(e),
        _ => Ok(()),
    }
}

fn read_meta(path: &Path) -> io::Result<ItemMeta> {
    serde_json::from_slice(&fs::read(path)?).map_err(io::Error::other)
}

impl Queue {
    /// Only failing to create the queue directories is an error; everything recovery cannot
    /// handle is reported in the `RecoveryReport` instead.
    pub fn open(root: &Path) -> io::Result<(Self, RecoveryReport)> {
        let queue = Self { items: root.join("items"), quarantine: root.join("quarantine") };
        fs::create_dir_all(&queue.items)?;
        fs::create_dir_all(&queue.quarantine)?;
        let report = queue.recover();
        Ok((queue, report))
    }

    pub fn items_dir(&self) -> &Path {
        &self.items
    }

    fn data_path(&self, id: &str, kind: ItemKind) -> PathBuf {
        self.items.join(format!("{id}.{}", kind.extension()))
    }

    fn meta_path(&self, id: &str) -> PathBuf {
        self.items.join(format!("{id}.{META_EXT}"))
    }

    /// Move `src` (same filesystem) into the queue and record its metadata.
    pub fn enqueue_file(
        &self,
        src: &Path,
        kind: ItemKind,
        started_at: DateTime<Utc>,
        ended_at: DateTime<Utc>,
        video_fps: Option<u32>,
    ) -> io::Result<ItemMeta> {
        let (sha256, size) = sha256_file(src)?;
        // The durable sidecar asserts this sha/size, so the payload must be on disk first.
        File::open(src)?.sync_all()?;
        let id = ulid::Ulid::new().to_string();
        fs::rename(src, self.data_path(&id, kind))?;
        let meta = ItemMeta { id, kind, started_at, ended_at, sha256, size, video_fps };
        let json = serde_json::to_vec(&meta).map_err(io::Error::other)?;
        write_atomic(&self.meta_path(&meta.id), &json, FILE_MODE)?;
        Ok(meta)
    }

    pub fn enqueue_bytes(
        &self,
        bytes: &[u8],
        kind: ItemKind,
        started_at: DateTime<Utc>,
        ended_at: DateTime<Utc>,
    ) -> io::Result<ItemMeta> {
        let tmp = self.items.join(format!(".{}.{TMP_EXT}", ulid::Ulid::new()));
        {
            let mut file = File::create(&tmp)?;
            file.write_all(bytes)?;
            file.sync_all()?;
        }
        self.enqueue_file(&tmp, kind, started_at, ended_at, None)
    }

    fn metas(&self) -> io::Result<Vec<ItemMeta>> {
        let mut metas = Vec::new();
        for entry in fs::read_dir(&self.items)? {
            let path = entry?.path();
            if path.extension().is_some_and(|e| e == META_EXT) {
                match read_meta(&path) {
                    Ok(meta) => metas.push(meta),
                    Err(e) => tracing::warn!(path = %path.display(), error = %e, "skipping unreadable queue sidecar"),
                }
            }
        }
        metas.sort_by(|a, b| a.id.cmp(&b.id));
        Ok(metas)
    }

    fn queued(&self, meta: ItemMeta) -> QueuedItem {
        let data_path = self.data_path(&meta.id, meta.kind);
        QueuedItem { meta, data_path }
    }

    pub fn oldest(&self) -> io::Result<Option<QueuedItem>> {
        Ok(self.metas()?.into_iter().next().map(|m| self.queued(m)))
    }

    /// All items of `kind`, oldest first, from a single directory scan.
    pub fn items_of_kind(&self, kind: ItemKind) -> io::Result<Vec<QueuedItem>> {
        Ok(self.metas()?.into_iter().filter(|m| m.kind == kind).map(|m| self.queued(m)).collect())
    }

    pub fn oldest_of_kind(&self, kind: ItemKind) -> io::Result<Option<QueuedItem>> {
        Ok(self.metas()?.into_iter().find(|m| m.kind == kind).map(|m| self.queued(m)))
    }

    pub fn len(&self) -> io::Result<usize> {
        Ok(self.metas()?.len())
    }

    pub fn is_empty(&self) -> io::Result<bool> {
        Ok(self.len()? == 0)
    }

    pub fn remove(&self, meta: &ItemMeta) -> io::Result<()> {
        remove_if_exists(&self.meta_path(&meta.id))?;
        remove_if_exists(&self.data_path(&meta.id, meta.kind))
    }

    pub fn quarantine(&self, meta: &ItemMeta, reason: &str) -> io::Result<()> {
        let data = self.data_path(&meta.id, meta.kind);
        if data.exists() {
            fs::rename(&data, self.quarantine.join(format!("{}.{}", meta.id, meta.kind.extension())))?;
        }
        fs::write(self.quarantine.join(format!("{}.reason", meta.id)), reason)?;
        fs::rename(self.meta_path(&meta.id), self.quarantine.join(format!("{}.{META_EXT}", meta.id)))
    }

    /// Move a payload and its bad sidecar into `quarantine/` with a `.reason` file (best effort per file).
    fn quarantine_raw(&self, stem: &str, payload: Option<&Path>, sidecar: &Path, reason: &str) -> io::Result<()> {
        tracing::warn!(id = stem, reason, "quarantining queue item during recovery");
        if let Some(p) = payload {
            if let Some(name) = p.file_name() {
                fs::rename(p, self.quarantine.join(name))?;
            }
        }
        fs::write(self.quarantine.join(format!("{stem}.reason")), reason)?;
        fs::rename(sidecar, self.quarantine.join(format!("{stem}.{META_EXT}")))
    }

    /// Crash recovery. Deleted and counted: temp files, true orphans (payload with no sidecar file)
    /// and sidecars without a payload. Payloads whose sidecar is unreadable, mismatched or disagrees
    /// on size are quarantined, never deleted, so audio and markers survive a corrupt sidecar.
    fn recover(&self) -> RecoveryReport {
        let mut report = RecoveryReport::default();
        let mut sidecars: Vec<(String, PathBuf)> = Vec::new();
        let mut payloads: Vec<(String, PathBuf)> = Vec::new();
        let entries = match fs::read_dir(&self.items) {
            Ok(entries) => entries,
            Err(e) => {
                report.problem(&self.items, "cannot list queue items", &e);
                return report;
            }
        };
        for entry in entries {
            let path = match entry {
                Ok(entry) => entry.path(),
                Err(e) => {
                    report.problem(&self.items, "cannot read a queue entry", &e);
                    continue;
                }
            };
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or_default().to_string();
            let ext = path.extension().and_then(|e| e.to_str()).unwrap_or_default().to_string();
            let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or_default().to_string();
            if ext == TMP_EXT || name.starts_with('.') {
                self.delete(&path, "cannot delete temp file", &mut report);
            } else if ext == META_EXT {
                sidecars.push((stem, path));
            } else {
                payloads.push((stem, path));
            }
        }
        let mut claimed = HashSet::new();
        for (stem, sidecar) in &sidecars {
            let found = payloads.iter().find(|(s, _)| s == stem).map(|(_, p)| p.clone());
            match (self.check_sidecar(stem, sidecar), found) {
                (SidecarCheck::Valid, _) => {
                    claimed.insert(stem.clone());
                }
                (SidecarCheck::Bad(reason), Some(payload)) => {
                    // Claimed even if the move fails: the payload must never be deleted as an orphan.
                    claimed.insert(stem.clone());
                    if let Err(e) = self.quarantine_raw(stem, Some(&payload), sidecar, &reason) {
                        report.problem(sidecar, "cannot quarantine", &e);
                    }
                }
                (SidecarCheck::NoPayload, Some(payload)) => {
                    claimed.insert(stem.clone());
                    let reason = "payload extension does not match the sidecar kind";
                    if let Err(e) = self.quarantine_raw(stem, Some(&payload), sidecar, reason) {
                        report.problem(sidecar, "cannot quarantine", &e);
                    }
                }
                (SidecarCheck::Bad(_) | SidecarCheck::NoPayload, None) => {
                    tracing::warn!(path = %sidecar.display(), "dropping sidecar without payload");
                    self.delete(sidecar, "cannot delete sidecar without payload", &mut report);
                }
            }
        }
        for (stem, path) in payloads {
            if !claimed.contains(&stem) {
                tracing::warn!(path = %path.display(), "dropping orphan payload without sidecar");
                self.delete(&path, "cannot delete orphan payload", &mut report);
            }
        }
        report
    }

    fn check_sidecar(&self, stem: &str, sidecar: &Path) -> SidecarCheck {
        let meta = match read_meta(sidecar) {
            Ok(meta) => meta,
            Err(e) => return SidecarCheck::Bad(format!("unreadable sidecar: {e}")),
        };
        if meta.id != stem {
            return SidecarCheck::Bad(format!("sidecar id {} does not match file name", meta.id));
        }
        match fs::metadata(self.data_path(&meta.id, meta.kind)) {
            Ok(md) if md.len() == meta.size => SidecarCheck::Valid,
            Ok(md) => SidecarCheck::Bad(format!("payload size {} != sidecar size {}", md.len(), meta.size)),
            Err(e) if e.kind() == io::ErrorKind::NotFound => SidecarCheck::NoPayload,
            // EIO and friends: the payload may well be there, so it must not count as missing.
            Err(e) => SidecarCheck::Bad(format!("payload unreadable: {e}")),
        }
    }

    /// Delete a leftover and count it; on failure leave it and report.
    fn delete(&self, path: &Path, what: &str, report: &mut RecoveryReport) {
        match fs::remove_file(path) {
            Ok(()) => report.dropped += 1,
            Err(e) => report.problem(path, what, &e),
        }
    }
}

enum SidecarCheck {
    Valid,
    /// Quarantine the payload with this reason (or drop the sidecar if there is no payload).
    Bad(String),
    /// No payload at the path the sidecar names.
    NoPayload,
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn ts(sec: u32) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 10, 14, 1, 0, sec).unwrap()
    }

    fn payload(dir: &Path, name: &str, body: &[u8]) -> PathBuf {
        let p = dir.join(name);
        fs::write(&p, body).unwrap();
        p
    }

    #[test]
    fn enqueue_moves_file_and_writes_sidecar() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, report) = Queue::open(&tmp.path().join("q")).unwrap();
        assert_eq!(report, RecoveryReport::default());
        let src = payload(tmp.path(), "seg.h264", b"video-bytes");
        let meta = queue.enqueue_file(&src, ItemKind::Video, ts(0), ts(59), Some(2)).unwrap();
        assert!(!src.exists());
        assert_eq!(meta.size, 11);
        assert_eq!(meta.sha256, hex::encode(Sha256::digest(b"video-bytes")));
        let item = queue.oldest().unwrap().unwrap();
        assert_eq!(item.meta, meta);
        assert_eq!(fs::read(&item.data_path).unwrap(), b"video-bytes");
        assert_eq!(queue.len().unwrap(), 1);
    }

    #[test]
    fn oldest_is_first_enqueued_and_remove_works() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let a = queue.enqueue_bytes(b"{}", ItemKind::Marker, ts(0), ts(0)).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(2));
        let b = queue.enqueue_bytes(b"{}", ItemKind::Marker, ts(1), ts(1)).unwrap();
        assert_eq!(queue.oldest().unwrap().unwrap().meta.id, a.id);
        queue.remove(&a).unwrap();
        assert_eq!(queue.oldest().unwrap().unwrap().meta.id, b.id);
    }

    #[test]
    fn oldest_of_kind_filters() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        queue.enqueue_bytes(b"a", ItemKind::Audio, ts(0), ts(1)).unwrap();
        let v = queue
            .enqueue_file(&payload(tmp.path(), "v", b"v"), ItemKind::Video, ts(2), ts(3), Some(2))
            .unwrap();
        assert_eq!(queue.oldest_of_kind(ItemKind::Video).unwrap().unwrap().meta.id, v.id);
    }

    #[test]
    fn quarantine_moves_item_out_of_the_queue() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("q");
        let (queue, _) = Queue::open(&root).unwrap();
        let m = queue.enqueue_bytes(b"x", ItemKind::Audio, ts(0), ts(1)).unwrap();
        queue.quarantine(&m, "brain said 409").unwrap();
        assert_eq!(queue.len().unwrap(), 0);
        let reason = fs::read_to_string(root.join("quarantine").join(format!("{}.reason", m.id))).unwrap();
        assert_eq!(reason, "brain said 409");
        assert!(root.join("quarantine").join(format!("{}.opus", m.id)).exists());
    }

    #[test]
    fn recover_removes_orphans_and_counts_them() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("q");
        let (queue, _) = Queue::open(&root).unwrap();
        let kept = queue.enqueue_bytes(b"ok", ItemKind::Marker, ts(0), ts(0)).unwrap();
        let items = root.join("items");
        // 1. Power cut after the payload rename, before the sidecar.
        fs::write(items.join("01JA2B3C4D5E6F7G8H9J0KMNPQ.h264"), b"orphan").unwrap();
        // 2. Sidecar whose id does not match its file name / payload.
        fs::write(items.join("01JA2B3C4D5E6F7G8H9J0KMNPR.meta"), serde_json::to_vec(&kept).unwrap()).unwrap();
        // 3. Half-written temp file.
        fs::write(items.join(".x.meta.tmp"), b"partial").unwrap();
        // 4. Corrupt sidecar; its payload then has no valid sidecar (5).
        fs::write(items.join("01JA2B3C4D5E6F7G8H9J0KMNPS.meta"), b"{not json").unwrap();
        fs::write(items.join("01JA2B3C4D5E6F7G8H9J0KMNPS.opus"), b"x").unwrap();
        drop(queue);

        let (queue, report) = Queue::open(&root).unwrap();
        assert_eq!(report.dropped, 3, "tmp file, true orphan, sidecar without payload");
        assert!(report.problems.is_empty(), "{:?}", report.problems);
        assert_eq!(queue.len().unwrap(), 1);
        assert_eq!(queue.oldest().unwrap().unwrap().meta.id, kept.id);
        let leftovers: Vec<_> = fs::read_dir(&items).unwrap().map(|e| e.unwrap().file_name()).collect();
        assert_eq!(leftovers.len(), 2, "only the kept payload and its sidecar remain: {leftovers:?}");
        let q = root.join("quarantine");
        assert!(q.join("01JA2B3C4D5E6F7G8H9J0KMNPS.opus").exists(), "audio under a corrupt sidecar is quarantined, not deleted");
        assert!(q.join("01JA2B3C4D5E6F7G8H9J0KMNPS.reason").exists());
        assert!(!q.join("01JA2B3C4D5E6F7G8H9J0KMNPQ.h264").exists());
    }

    #[test]
    fn sidecar_with_wrong_payload_size_is_not_left_in_the_queue() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("q");
        let (queue, _) = Queue::open(&root).unwrap();
        let m = queue.enqueue_bytes(b"audio-bytes", ItemKind::Audio, ts(0), ts(1)).unwrap();
        fs::write(queue.items_dir().join(format!("{}.opus", m.id)), b"trunc").unwrap();
        drop(queue);
        let (queue, report) = Queue::open(&root).unwrap();
        assert_eq!(report.dropped, 0);
        assert_eq!(queue.len().unwrap(), 0);
        assert!(root.join("quarantine").join(format!("{}.opus", m.id)).exists());
    }

    /// A non-empty directory where a file is expected cannot be read or removed: a stand-in for
    /// an unreadable file on a failing SD card that works for root too.
    fn stuck_entry(path: &Path) {
        fs::create_dir_all(path).unwrap();
        fs::write(path.join("x"), b"x").unwrap();
    }

    #[test]
    fn unhandled_entries_are_reported_and_do_not_abort_recovery() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("q");
        let (queue, _) = Queue::open(&root).unwrap();
        let kept = queue.enqueue_bytes(b"ok", ItemKind::Audio, ts(0), ts(1)).unwrap();
        let items = root.join("items");
        stuck_entry(&items.join(".stuck.tmp"));
        stuck_entry(&items.join("01JA2B3C4D5E6F7G8H9J0KMNPQ.h264"));
        stuck_entry(&items.join("01JA2B3C4D5E6F7G8H9J0KMNPR.meta"));
        fs::write(items.join(".x.meta.tmp"), b"partial").unwrap();
        fs::write(items.join("01JA2B3C4D5E6F7G8H9J0KMNPS.h264"), b"orphan").unwrap();
        drop(queue);

        let (queue, report) = Queue::open(&root).unwrap();
        assert_eq!(report.dropped, 2, "the removable leftovers are still cleaned up");
        assert_eq!(report.problems.len(), 3, "{:?}", report.problems);
        assert!(report.summary().unwrap().starts_with("startup recovery: 3 file(s) left in place; first: "));
        assert_eq!(queue.oldest().unwrap().unwrap().meta.id, kept.id, "valid items survive");
        assert!(items.join(".stuck.tmp").exists() && items.join("01JA2B3C4D5E6F7G8H9J0KMNPQ.h264").exists());
    }

    #[test]
    fn payload_is_kept_when_its_quarantine_fails() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("q");
        let (queue, _) = Queue::open(&root).unwrap();
        let m = queue.enqueue_bytes(b"audio-bytes", ItemKind::Audio, ts(0), ts(1)).unwrap();
        fs::write(queue.items_dir().join(format!("{}.opus", m.id)), b"trunc").unwrap();
        // A directory where the .reason file must go makes the quarantine fail half-way.
        stuck_entry(&root.join("quarantine").join(format!("{}.reason", m.id)));
        drop(queue);
        let (_queue, report) = Queue::open(&root).unwrap();
        assert_eq!(report.problems.len(), 1, "{:?}", report.problems);
        let moved = root.join("quarantine").join(format!("{}.opus", m.id));
        let left = root.join("items").join(format!("{}.opus", m.id));
        assert!(moved.exists() || left.exists(), "audio is never deleted");
    }

    #[test]
    fn unlistable_items_dir_is_a_problem_not_an_error() {
        let tmp = tempfile::tempdir().unwrap();
        let queue = Queue { items: tmp.path().join("missing"), quarantine: tmp.path().join("quarantine") };
        let report = queue.recover();
        assert_eq!(report.problems.len(), 1);
    }

    #[test]
    fn creating_the_queue_directories_is_still_fatal() {
        let tmp = tempfile::tempdir().unwrap();
        let blocker = tmp.path().join("file");
        fs::write(&blocker, b"x").unwrap();
        assert!(Queue::open(&blocker.join("q")).is_err());
    }

    #[test]
    fn reports_merge_and_summarise() {
        let a = RecoveryReport { recovered: 1, dropped: 2, problems: vec!["a".into()] };
        let b = RecoveryReport { recovered: 3, dropped: 0, problems: vec!["b".into(), "c".into()] };
        let merged = a.merge(b);
        assert_eq!((merged.recovered, merged.dropped), (4, 2));
        assert_eq!(merged.summary().as_deref(), Some("startup recovery: 3 file(s) left in place; first: a"));
        assert_eq!(RecoveryReport::default().summary(), None);
    }

    #[test]
    fn corrupt_sidecar_is_skipped_and_does_not_break_listing() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let good = queue.enqueue_bytes(b"ok", ItemKind::Marker, ts(0), ts(0)).unwrap();
        fs::write(queue.items_dir().join("01JA2B3C4D5E6F7G8H9J0KMNPS.meta"), b"{not json").unwrap();
        assert_eq!(queue.len().unwrap(), 1);
        assert_eq!(queue.oldest().unwrap().unwrap().meta.id, good.id);
    }
}
