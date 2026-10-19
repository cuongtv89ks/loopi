//! The update in progress, kept in /data/ota/pending.json across reboots (P4a design §6). `/data` is shared by both
//! slots, so this format must stay readable by the agent versions before and after this one.
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use super::{OtaReport, OtaState, Slot};
use crate::fsutil::write_atomic;

pub const PENDING_FILE: &str = "pending.json";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Downloading,
    Installing,
    Rebooting,
    Succeeded,
    RolledBack,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Pending {
    pub command_id: Option<String>,
    /// `None` for the hand-made first move to A/B slots (os/scripts/migrate-to-ab.sh).
    pub bundle: Option<String>,
    pub version: String,
    pub target_slot: Slot,
    pub phase: Phase,
    #[serde(default)]
    pub error: Option<String>,
}

impl Pending {
    pub fn report(&self, state: OtaState) -> OtaReport {
        OtaReport::new(self.command_id.clone(), &self.version, state, self.error.clone())
    }

    /// The state to report for a finished update, or `None` while it runs.
    pub fn outcome(&self) -> Option<OtaState> {
        match self.phase {
            Phase::Succeeded => Some(OtaState::Succeeded),
            Phase::RolledBack => Some(OtaState::RolledBack),
            Phase::Failed => Some(OtaState::Failed),
            Phase::Downloading | Phase::Installing | Phase::Rebooting => None,
        }
    }
}

/// What the last boot left behind, and what to do about it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BootAction {
    Idle,
    /// A trial boot of the slot an update installed: confirm it once a heartbeat gets through.
    Confirm(Pending),
    /// The trial did not confirm itself and the firmware went back to the good slot.
    RolledBack(Pending),
    /// Power or the agent went away before the reboot (while downloading or installing).
    Interrupted(Pending),
    /// An outcome the brain may not have heard yet.
    Deliver(Pending),
}

/// A `Failed` record for the slot that is running now is a trial to confirm like a `Rebooting` one (its error, which
/// no longer holds, is dropped): running the slot the update installed means the reboot happened, and that slot is
/// only bootable through tryboot.txt, which exists only after a successful install. This also carries a device whose
/// previous agent recorded such a reboot as failed (systemctl dies with the service it stops).
pub fn reconcile(pending: Option<Pending>, booted: Slot) -> BootAction {
    let Some(mut pending) = pending else { return BootAction::Idle };
    match pending.phase {
        Phase::Downloading | Phase::Installing => BootAction::Interrupted(pending),
        Phase::Rebooting if pending.target_slot == booted => BootAction::Confirm(pending),
        Phase::Rebooting => BootAction::RolledBack(pending),
        Phase::Failed if pending.target_slot == booted => {
            pending.error = None;
            BootAction::Confirm(pending)
        }
        Phase::Succeeded | Phase::RolledBack | Phase::Failed => BootAction::Deliver(pending),
    }
}

pub struct PendingStore {
    dir: PathBuf,
}

impl PendingStore {
    pub fn new(data_dir: &Path) -> Self {
        Self { dir: data_dir.join("ota") }
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// `Ok(None)` when no update is pending; a corrupt file is `InvalidData`.
    pub fn load(&self) -> io::Result<Option<Pending>> {
        match fs::read(self.dir.join(PENDING_FILE)) {
            Ok(bytes) => serde_json::from_slice(&bytes).map(Some).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e)),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e),
        }
    }

    pub fn save(&self, pending: &Pending) -> io::Result<()> {
        fs::create_dir_all(&self.dir)?;
        let bytes = serde_json::to_vec(pending).map_err(io::Error::other)?;
        write_atomic(&self.dir.join(PENDING_FILE), &bytes, 0o600)
    }

    pub fn remove(&self) -> io::Result<()> {
        match fs::remove_file(self.dir.join(PENDING_FILE)) {
            Err(e) if e.kind() != io::ErrorKind::NotFound => Err(e),
            _ => Ok(()),
        }
    }

    pub fn bundle_path(&self, bundle: &str) -> PathBuf {
        self.dir.join(bundle)
    }

    pub fn part_path(&self, bundle: &str) -> PathBuf {
        self.dir.join(format!("{bundle}.part"))
    }

    /// Delete every downloaded or half-downloaded bundle.
    pub fn clear_downloads(&self) -> io::Result<()> {
        let entries = match fs::read_dir(&self.dir) {
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
            other => other?,
        };
        for entry in entries {
            let path = entry?.path();
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or_default();
            if name.ends_with(".raucb") || name.ends_with(".raucb.part") {
                fs::remove_file(&path)?;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pending(phase: Phase, target: Slot) -> Pending {
        Pending { command_id: Some("C".into()), bundle: Some("b.raucb".into()), version: "v".into(), target_slot: target, phase, error: None }
    }

    #[test]
    fn reconcile_decides_from_the_phase_and_the_booted_slot() {
        assert_eq!(reconcile(None, Slot::A), BootAction::Idle);
        for phase in [Phase::Downloading, Phase::Installing] {
            assert!(matches!(reconcile(Some(pending(phase, Slot::B)), Slot::A), BootAction::Interrupted(_)));
        }
        assert!(matches!(reconcile(Some(pending(Phase::Rebooting, Slot::B)), Slot::B), BootAction::Confirm(_)));
        assert!(matches!(reconcile(Some(pending(Phase::Rebooting, Slot::B)), Slot::A), BootAction::RolledBack(_)));
        for phase in [Phase::Succeeded, Phase::RolledBack, Phase::Failed] {
            assert!(matches!(reconcile(Some(pending(phase, Slot::B)), Slot::A), BootAction::Deliver(_)));
        }
        for phase in [Phase::Succeeded, Phase::RolledBack] {
            assert!(matches!(reconcile(Some(pending(phase, Slot::B)), Slot::B), BootAction::Deliver(_)));
        }
    }

    #[test]
    fn a_failed_record_for_the_slot_now_running_is_a_trial_to_confirm_without_its_error() {
        let mut failed = pending(Phase::Failed, Slot::B);
        failed.error = Some("reboot failed: systemctl reboot failed (signal: 15 (SIGTERM))".into());
        let BootAction::Confirm(confirm) = reconcile(Some(failed.clone()), Slot::B) else { panic!("expected Confirm") };
        assert_eq!((confirm.error, confirm.target_slot, confirm.version), (None, Slot::B, "v".to_string()));
        assert!(matches!(reconcile(Some(failed), Slot::A), BootAction::Deliver(p) if p.error.is_some()));
    }

    #[test]
    fn store_round_trips_and_reports_corruption() {
        let dir = tempfile::tempdir().unwrap();
        let store = PendingStore::new(dir.path());
        assert_eq!(store.load().unwrap(), None);
        store.save(&pending(Phase::Rebooting, Slot::B)).unwrap();
        assert_eq!(store.load().unwrap(), Some(pending(Phase::Rebooting, Slot::B)));
        store.remove().unwrap();
        store.remove().unwrap();
        std::fs::write(dir.path().join("ota").join(PENDING_FILE), b"{not json").unwrap();
        assert_eq!(store.load().unwrap_err().kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn reads_what_the_migration_script_writes() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("ota")).unwrap();
        std::fs::write(
            dir.path().join("ota").join(PENDING_FILE),
            br#"{"command_id":null,"bundle":null,"version":"2026.10.21-a1b2c3d","target_slot":"B","phase":"rebooting","error":null}"#,
        )
        .unwrap();
        let loaded = PendingStore::new(dir.path()).load().unwrap().unwrap();
        assert_eq!((loaded.command_id, loaded.target_slot, loaded.phase), (None, Slot::B, Phase::Rebooting));
    }

    #[test]
    fn clear_downloads_keeps_the_state_file() {
        let dir = tempfile::tempdir().unwrap();
        let store = PendingStore::new(dir.path());
        store.save(&pending(Phase::Downloading, Slot::B)).unwrap();
        std::fs::write(store.part_path("x.raucb"), b"1").unwrap();
        std::fs::write(store.bundle_path("y.raucb"), b"2").unwrap();
        store.clear_downloads().unwrap();
        assert!(store.load().unwrap().is_some());
        assert!(!store.part_path("x.raucb").exists() && !store.bundle_path("y.raucb").exists());
        PendingStore::new(&dir.path().join("none")).clear_downloads().unwrap();
    }
}
