//! The OTA task (P4a design §6): finish what the last boot left (confirm a trial, report a rollback or an
//! interruption), then install the updates the brain sends, one at a time.
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::{mpsc, watch};

use super::pending::{reconcile, BootAction, Pending, PendingStore, Phase};
use super::system::{BundleFetcher, Rauc, Rebooter};
use super::{OtaReport, OtaState, Slot, UpdateRequest};
use crate::disk::DiskUsage;
use crate::status::StatusTx;
use crate::transport::TransportError;

/// Updates need room: a ~75 MB bundle on a card above this would push the queue toward the 85 % video drop.
pub const MAX_DISK_PCT: f32 = 80.0;
pub const IN_PROGRESS: &str = "another update is in progress";
pub const INTERRUPTED: &str = "interrupted before the update was installed (power loss or restart)";

#[derive(Debug, Clone, Copy)]
pub struct OtaTimings {
    pub retry_first: Duration,
    pub retry_max: Duration,
    pub attempts: u32,
    /// How long to let the brain hear "rebooting" before the device goes quiet.
    pub reboot_report_wait: Duration,
}

impl Default for OtaTimings {
    fn default() -> Self {
        Self { retry_first: Duration::from_secs(1), retry_max: Duration::from_secs(300), attempts: 10,
               reboot_report_wait: Duration::from_secs(45) }
    }
}

pub struct OtaDeps {
    pub store: PendingStore,
    pub rauc: Arc<dyn Rauc>,
    pub rebooter: Arc<dyn Rebooter>,
    pub fetcher: Arc<dyn BundleFetcher>,
    pub disk: Arc<dyn DiskUsage>,
    pub status: StatusTx,
    /// Heartbeats the brain answered with 200 since the agent started.
    pub delivered: watch::Receiver<u64>,
    /// True while the task owns an update (its boot reconciliation, or from taking a request until the outcome is
    /// delivered). `run` sets it when it hands over a request and refuses further updates while it is set; the task
    /// clears it when idle again. Without it a second update would wait unread in the channel behind the first.
    pub busy: Arc<AtomicBool>,
    pub booted: Slot,
    pub timings: OtaTimings,
}

/// Holds `OtaDeps::busy` for as long as it lives, on every exit path.
struct BusyGuard(Arc<AtomicBool>);

impl BusyGuard {
    fn hold(flag: &Arc<AtomicBool>) -> Self {
        flag.store(true, Ordering::SeqCst);
        Self(flag.clone())
    }
}

impl Drop for BusyGuard {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

/// Note in `last_error` that an update was acked but not started (P4a design §6.1); the running one keeps its report.
pub fn ignore_update(status: &StatusTx, command_id: Option<&str>, reason: &str) {
    let id = command_id.unwrap_or("-");
    tracing::warn!(id, reason, "update ignored");
    status.send_modify(|s| s.last_error = Some(format!("update {id} ignored: {reason}")));
}

pub async fn run_ota(mut deps: OtaDeps, mut updates: mpsc::Receiver<UpdateRequest>, mut shutdown: watch::Receiver<bool>) {
    {
        let _busy = BusyGuard::hold(&deps.busy);
        on_boot(&mut deps, &mut shutdown).await;
    }
    loop {
        // Checked before waiting: on_boot() and install() may have used up the shutdown notification (`changed()`
        // fires once per change), and waiting again would then never end.
        if stopping(&shutdown) {
            return;
        }
        tokio::select! {
            update = updates.recv() => match update {
                Some(request) => {
                    let _busy = BusyGuard::hold(&deps.busy);
                    install(&mut deps, request, &mut shutdown).await;
                }
                None => return,
            },
            changed = shutdown.changed() => if changed.is_err() || *shutdown.borrow() { return },
        }
    }
}

fn publish(status: &StatusTx, report: Option<OtaReport>) {
    status.send_modify(|s| s.ota = report);
}

fn stopping(shutdown: &watch::Receiver<bool>) -> bool {
    *shutdown.borrow()
}

/// Wait until at least `count` heartbeats got through; false on shutdown.
async fn heartbeats(delivered: &mut watch::Receiver<u64>, count: u64, shutdown: &mut watch::Receiver<bool>) -> bool {
    loop {
        if *delivered.borrow_and_update() >= count {
            return true;
        }
        tokio::select! {
            changed = delivered.changed() => if changed.is_err() { return false },
            changed = shutdown.changed() => if changed.is_err() || *shutdown.borrow() { return false },
        }
    }
}

/// Keep `report` in the heartbeat until two more heartbeats got through (the first may have been built before it was
/// set), then forget the update.
async fn deliver(deps: &mut OtaDeps, report: OtaReport, shutdown: &mut watch::Receiver<bool>) {
    publish(&deps.status, Some(report.clone()));
    let target = *deps.delivered.borrow() + 2;
    if heartbeats(&mut deps.delivered, target, shutdown).await {
        if let Err(e) = deps.store.remove() {
            tracing::warn!(error = %e, "could not delete the finished update's state");
        }
        deps.status.send_modify(|s| {
            if s.ota.as_ref() == Some(&report) {
                s.ota = None;
            }
        });
    }
}

fn save(deps: &OtaDeps, pending: &Pending) {
    if let Err(e) = deps.store.save(pending) {
        tracing::error!(error = %e, "could not save the update state");
    }
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> Result<T, String> + Send + 'static) -> Result<T, String> {
    tokio::task::spawn_blocking(f).await.unwrap_or_else(|e| Err(format!("worker thread failed: {e}")))
}

async fn finish(deps: &mut OtaDeps, mut pending: Pending, phase: Phase, error: Option<String>,
                shutdown: &mut watch::Receiver<bool>) {
    pending.phase = phase;
    pending.error = error;
    save(deps, &pending);
    let state = pending.outcome().expect("finish() takes a final phase");
    tracing::info!(?state, version = %pending.version, error = ?pending.error, "update finished");
    deliver(deps, pending.report(state), shutdown).await;
}

async fn on_boot(deps: &mut OtaDeps, shutdown: &mut watch::Receiver<bool>) {
    let pending = deps.store.load().unwrap_or_else(|e| {
        tracing::warn!(error = %e, "unreadable update state; forgetting it");
        let _ = deps.store.remove();
        None
    });
    match reconcile(pending, deps.booted) {
        BootAction::Idle => {}
        BootAction::Deliver(pending) => {
            let state = pending.outcome().expect("Deliver carries a final phase");
            deliver(deps, pending.report(state), shutdown).await;
        }
        BootAction::Interrupted(pending) => {
            if let Err(e) = deps.store.clear_downloads() {
                tracing::warn!(error = %e, "could not delete partial bundles");
            }
            finish(deps, pending, Phase::Failed, Some(INTERRUPTED.into()), shutdown).await;
        }
        BootAction::RolledBack(pending) => {
            let rauc = deps.rauc.clone();
            if let Err(e) = blocking(move || rauc.mark_bad_other()).await {
                tracing::warn!(error = %e, "could not mark the failed slot bad");
            }
            finish(deps, pending, Phase::RolledBack, None, shutdown).await;
        }
        BootAction::Confirm(pending) => {
            publish(&deps.status, Some(pending.report(OtaState::Trying)));
            tracing::info!(version = %pending.version, "trial boot: confirming after the first heartbeat");
            if !heartbeats(&mut deps.delivered, 1, shutdown).await {
                return;
            }
            let rauc = deps.rauc.clone();
            match blocking(move || rauc.mark_good()).await {
                Ok(()) => finish(deps, pending, Phase::Succeeded, None, shutdown).await,
                Err(e) => finish(deps, pending, Phase::Failed, Some(format!("could not confirm the new version: {e}")), shutdown).await,
            }
        }
    }
}

enum Download {
    Done,
    Failed(String),
    Shutdown,
}

async fn download(deps: &OtaDeps, request: &UpdateRequest, part: &Path, shutdown: &mut watch::Receiver<bool>) -> Download {
    let mut delay = deps.timings.retry_first;
    let mut last = String::new();
    for attempt in 1..=deps.timings.attempts {
        let result = tokio::select! {
            result = deps.fetcher.fetch(&request.bundle, part, request.size) => result,
            _ = shutdown.changed() => return Download::Shutdown,
        };
        match result {
            Ok(()) => return Download::Done,
            Err(TransportError::Status(404)) => return Download::Failed("the brain no longer offers this bundle".into()),
            Err(e) => {
                tracing::warn!(attempt, error = %e, "bundle download failed");
                last = e.to_string();
            }
        }
        if attempt == deps.timings.attempts {
            break;
        }
        tokio::select! {
            _ = tokio::time::sleep(delay) => {}
            _ = shutdown.changed() => return Download::Shutdown,
        }
        delay = (delay * 2).min(deps.timings.retry_max);
    }
    Download::Failed(format!("download failed: {last}"))
}

async fn install(deps: &mut OtaDeps, request: UpdateRequest, shutdown: &mut watch::Receiver<bool>) {
    match deps.store.load() {
        // Backstop for `busy`: an update still unfinished on disk (waiting for its trial boot) blocks a new one.
        Ok(Some(pending)) if pending.outcome().is_none() => {
            return ignore_update(&deps.status, request.command_id.as_deref(), IN_PROGRESS);
        }
        Err(e) => tracing::warn!(error = %e, "unreadable update state; starting over"),
        _ => {}
    }
    let mut pending = Pending {
        command_id: request.command_id.clone(),
        bundle: Some(request.bundle.clone()),
        version: request.version.clone(),
        target_slot: deps.booted.other(),
        phase: Phase::Downloading,
        error: None,
    };
    match deps.disk.used_pct() {
        Ok(pct) if pct <= MAX_DISK_PCT => {}
        Ok(pct) => {
            let reason = format!("the card is {pct:.0}% full; updates need it at most {MAX_DISK_PCT:.0}% full");
            return finish(deps, pending, Phase::Failed, Some(reason), shutdown).await;
        }
        Err(e) => return finish(deps, pending, Phase::Failed, Some(format!("disk usage unreadable: {e}")), shutdown).await,
    }
    save(deps, &pending);
    publish(&deps.status, Some(pending.report(OtaState::Downloading)));
    let (part, bundle) = (deps.store.part_path(&request.bundle), deps.store.bundle_path(&request.bundle));
    match download(deps, &request, &part, shutdown).await {
        Download::Done => {}
        Download::Shutdown => return,
        Download::Failed(reason) => {
            let _ = std::fs::remove_file(&part);
            return finish(deps, pending, Phase::Failed, Some(reason), shutdown).await;
        }
    }
    let check = part.clone();
    let digest = blocking(move || crate::queue::sha256_file(&check).map_err(|e| e.to_string())).await;
    if !matches!(&digest, Ok((sha, size)) if *sha == request.sha256 && *size == request.size) {
        let _ = std::fs::remove_file(&part);
        return finish(deps, pending, Phase::Failed, Some("bundle checksum mismatch".into()), shutdown).await;
    }
    if let Err(e) = std::fs::rename(&part, &bundle) {
        return finish(deps, pending, Phase::Failed, Some(format!("could not keep the bundle: {e}")), shutdown).await;
    }
    pending.phase = Phase::Installing;
    save(deps, &pending);
    publish(&deps.status, Some(pending.report(OtaState::Installing)));
    let (rauc, path) = (deps.rauc.clone(), bundle.clone());
    let installed = blocking(move || rauc.install(&path)).await;
    let _ = std::fs::remove_file(&bundle);
    if let Err(e) = installed {
        return finish(deps, pending, Phase::Failed, Some(format!("install failed: {e}")), shutdown).await;
    }
    pending.phase = Phase::Rebooting;
    save(deps, &pending);
    publish(&deps.status, Some(pending.report(OtaState::Rebooting)));
    let target = *deps.delivered.borrow() + 2;
    let wait = deps.timings.reboot_report_wait;
    let _ = tokio::time::timeout(wait, heartbeats(&mut deps.delivered, target, shutdown)).await;
    if stopping(shutdown) {
        return;
    }
    let rebooter = deps.rebooter.clone();
    if let Err(e) = blocking(move || rebooter.reboot_into_trial()).await {
        finish(deps, pending, Phase::Failed, Some(format!("reboot failed: {e}")), shutdown).await;
    }
    // On success systemd is stopping us now; the next boot continues in on_boot().
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ota::pending::{Pending, Phase, PendingStore};
    use crate::ota::{OtaState, Slot, UpdateRequest, Variant};
    use crate::disk::DiskUsage;
    use crate::status::{status_channel, AgentStatus};
    use crate::testing::{FakeFetcher, FakeRauc, FakeRebooter, FixedDisk, GatedFetcher};
    use sha2::{Digest, Sha256};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use std::time::Duration;
    use tokio::sync::{mpsc, watch};

    const BODY: &[u8] = b"signed-bundle";
    const BUNDLE: &str = "loopi-dev-2026.10.21-a1b2c3d.raucb";

    struct Rig {
        dir: tempfile::TempDir,
        rauc: Arc<FakeRauc>,
        rebooter: Arc<FakeRebooter>,
        fetcher: Arc<FakeFetcher>,
        disk: Arc<FixedDisk>,
        status: StatusTx,
        delivered: Arc<watch::Sender<u64>>,
        busy: Arc<AtomicBool>,
        shutdown: watch::Sender<bool>,
    }

    impl Rig {
        fn new() -> Self {
            Self {
                dir: tempfile::tempdir().unwrap(),
                rauc: Arc::default(),
                rebooter: Arc::default(),
                fetcher: Arc::new(FakeFetcher::new(BODY)),
                disk: Arc::new(FixedDisk::new(40.0)),
                status: status_channel(AgentStatus::default()),
                delivered: Arc::new(watch::channel(0).0),
                busy: Arc::default(),
                shutdown: watch::channel(false).0,
            }
        }

        fn store(&self) -> PendingStore {
            PendingStore::new(self.dir.path())
        }

        fn deps(&self, booted: Slot) -> OtaDeps {
            OtaDeps {
                store: self.store(),
                rauc: self.rauc.clone(),
                rebooter: self.rebooter.clone(),
                fetcher: self.fetcher.clone(),
                disk: self.disk.clone(),
                status: self.status.clone(),
                delivered: self.delivered.subscribe(),
                busy: self.busy.clone(),
                booted,
                timings: OtaTimings { retry_first: Duration::from_millis(1), retry_max: Duration::from_millis(2),
                                      attempts: 3, reboot_report_wait: Duration::from_millis(20) },
            }
        }

        fn start(&self, booted: Slot) -> (mpsc::Sender<UpdateRequest>, tokio::task::JoinHandle<()>) {
            let (tx, rx) = mpsc::channel(4);
            (tx, tokio::spawn(run_ota(self.deps(booted), rx, self.shutdown.subscribe())))
        }

        fn heartbeat(&self) {
            self.delivered.send_modify(|n| *n += 1);
        }

        fn busy(&self) -> bool {
            self.busy.load(Ordering::SeqCst)
        }

        fn ota(&self) -> Option<OtaReport> {
            self.status.borrow().ota.clone()
        }

        async fn until(&self, mut done: impl FnMut(&Self) -> bool) {
            for _ in 0..500 {
                if done(self) {
                    return;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
            panic!("condition not reached; status.ota = {:?}", self.ota());
        }
    }

    fn request(sha: &str) -> UpdateRequest {
        UpdateRequest { command_id: Some("01JA2B3C4D5E6F7G8H9J0KMNPQ".into()), bundle: BUNDLE.into(),
                        version: "2026.10.21-a1b2c3d".into(), variant: Variant::Dev, sha256: sha.into(), size: BODY.len() as u64 }
    }

    fn good_sha() -> String {
        hex::encode(Sha256::digest(BODY))
    }

    #[tokio::test]
    async fn an_update_downloads_installs_and_reboots_into_the_trial() {
        let rig = Rig::new();
        let (tx, _task) = rig.start(Slot::A);
        tx.send(request(&good_sha())).await.unwrap();
        rig.until(|r| *r.rebooter.calls.lock().unwrap() == 1).await;
        assert_eq!(*rig.rauc.calls.lock().unwrap(), vec![format!("install {BUNDLE} exists=true")]);
        let pending = rig.store().load().unwrap().unwrap();
        assert_eq!((pending.phase, pending.target_slot), (Phase::Rebooting, Slot::B));
        assert_eq!(rig.ota().unwrap().state, OtaState::Rebooting);
        assert!(!rig.store().bundle_path(BUNDLE).exists() && !rig.store().part_path(BUNDLE).exists());
    }

    #[tokio::test]
    async fn a_full_card_refuses_before_downloading() {
        let rig = Rig::new();
        *rig.disk.0.lock().unwrap() = 81.0;
        let (tx, _task) = rig.start(Slot::A);
        tx.send(request(&good_sha())).await.unwrap();
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Failed)).await;
        assert!(rig.ota().unwrap().error.unwrap().contains("80%"));
        assert_eq!(*rig.fetcher.calls.lock().unwrap(), 0);
    }

    #[tokio::test]
    async fn a_checksum_mismatch_fails_without_installing() {
        let rig = Rig::new();
        let (tx, _task) = rig.start(Slot::A);
        tx.send(request(&"0".repeat(64))).await.unwrap();
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Failed)).await;
        assert!(rig.ota().unwrap().error.unwrap().contains("checksum"));
        assert!(rig.rauc.calls.lock().unwrap().is_empty());
        assert!(!rig.store().part_path(BUNDLE).exists());
    }

    #[tokio::test]
    async fn downloads_are_retried_but_a_withdrawn_bundle_is_not() {
        let rig = Rig::new();
        rig.fetcher.failures.lock().unwrap().extend([TransportError::Network("x".into()), TransportError::Status(503)]);
        let (tx, _task) = rig.start(Slot::A);
        tx.send(request(&good_sha())).await.unwrap();
        rig.until(|r| *r.rebooter.calls.lock().unwrap() == 1).await;
        assert_eq!(*rig.fetcher.calls.lock().unwrap(), 3);

        let rig = Rig::new();
        rig.fetcher.failures.lock().unwrap().push_back(TransportError::Status(404));
        let (tx, _task) = rig.start(Slot::A);
        tx.send(request(&good_sha())).await.unwrap();
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Failed)).await;
        assert_eq!(*rig.fetcher.calls.lock().unwrap(), 1);
        assert!(rig.ota().unwrap().error.unwrap().contains("no longer offers"));
    }

    #[tokio::test]
    async fn an_install_error_is_reported_and_nothing_reboots() {
        let rig = Rig::new();
        rig.rauc.results.lock().unwrap().push_back(Err("signature verification failed".into()));
        let (tx, _task) = rig.start(Slot::A);
        tx.send(request(&good_sha())).await.unwrap();
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Failed)).await;
        assert!(rig.ota().unwrap().error.unwrap().contains("signature verification failed"));
        assert_eq!(*rig.rebooter.calls.lock().unwrap(), 0);
        assert!(!rig.store().bundle_path(BUNDLE).exists());
    }

    #[tokio::test]
    async fn a_failed_reboot_is_reported() {
        let rig = Rig::new();
        *rig.rebooter.result.lock().unwrap() = Some("Access denied".into());
        let (tx, _task) = rig.start(Slot::A);
        tx.send(request(&good_sha())).await.unwrap();
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Failed)).await;
        assert!(rig.ota().unwrap().error.unwrap().contains("reboot failed"));
    }

    #[tokio::test]
    async fn a_second_update_while_one_runs_is_ignored_and_said_in_last_error() {
        let rig = Rig::new();
        let (tx, _task) = rig.start(Slot::A);
        tx.send(request(&good_sha())).await.unwrap();
        // Backstop: the fake reboot returns, so the task is idle while pending.json says rebooting (run() would normally
        // stop a second update before it got here, see `forward_update`).
        rig.until(|r| *r.rebooter.calls.lock().unwrap() == 1).await;
        tx.send(request(&good_sha())).await.unwrap();
        rig.until(|r| r.status.borrow().last_error.as_deref().is_some_and(|e| e.contains(IN_PROGRESS))).await;
        assert_eq!(rig.ota().unwrap().state, OtaState::Rebooting);
        assert_eq!(*rig.fetcher.calls.lock().unwrap(), 1);
    }

    #[tokio::test]
    async fn a_trial_is_confirmed_only_after_a_heartbeat_got_through() {
        let rig = Rig::new();
        rig.store().save(&Pending { command_id: Some("X".into()), bundle: Some(BUNDLE.into()), version: "v".into(),
                                    target_slot: Slot::B, phase: Phase::Rebooting, error: None }).unwrap();
        let (_tx, _task) = rig.start(Slot::B);
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Trying)).await;
        assert!(rig.busy(), "a trial waiting for its heartbeat keeps the task busy");
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert!(rig.rauc.calls.lock().unwrap().is_empty(), "no mark-good before a heartbeat got through");
        rig.heartbeat();
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Succeeded)).await;
        assert_eq!(*rig.rauc.calls.lock().unwrap(), vec!["mark-good".to_string()]);
        assert_eq!(rig.store().load().unwrap().unwrap().phase, Phase::Succeeded);
        rig.heartbeat();
        rig.heartbeat();
        rig.until(|r| r.ota().is_none()).await;
        assert_eq!(rig.store().load().unwrap(), None);
        rig.until(|r| !r.busy()).await;
    }

    #[tokio::test]
    async fn a_confirmation_that_fails_is_reported_failed() {
        let rig = Rig::new();
        rig.rauc.results.lock().unwrap().push_back(Err("no tryboot.txt".into()));
        rig.store().save(&Pending { command_id: None, bundle: None, version: "v".into(),
                                    target_slot: Slot::B, phase: Phase::Rebooting, error: None }).unwrap();
        let (_tx, _task) = rig.start(Slot::B);
        rig.heartbeat();
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Failed)).await;
        assert!(rig.ota().unwrap().error.unwrap().contains("no tryboot.txt"));
    }

    #[tokio::test]
    async fn back_on_the_old_slot_means_rolled_back() {
        let rig = Rig::new();
        rig.store().save(&Pending { command_id: Some("X".into()), bundle: Some(BUNDLE.into()), version: "v".into(),
                                    target_slot: Slot::B, phase: Phase::Rebooting, error: None }).unwrap();
        let (_tx, _task) = rig.start(Slot::A);
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::RolledBack)).await;
        assert_eq!(*rig.rauc.calls.lock().unwrap(), vec!["mark-bad other".to_string()]);
        rig.heartbeat();
        rig.heartbeat();
        rig.until(|r| r.ota().is_none()).await;
        assert_eq!(rig.store().load().unwrap(), None);
    }

    #[tokio::test]
    async fn an_interrupted_download_is_failed_and_cleaned_up() {
        let rig = Rig::new();
        rig.store().save(&Pending { command_id: Some("X".into()), bundle: Some(BUNDLE.into()), version: "v".into(),
                                    target_slot: Slot::B, phase: Phase::Downloading, error: None }).unwrap();
        std::fs::write(rig.store().part_path(BUNDLE), b"half").unwrap();
        let (_tx, _task) = rig.start(Slot::A);
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Failed)).await;
        assert_eq!(rig.ota().unwrap().error.as_deref(), Some(INTERRUPTED));
        assert!(!rig.store().part_path(BUNDLE).exists());
    }

    #[tokio::test]
    async fn a_corrupt_state_file_is_forgotten() {
        let rig = Rig::new();
        std::fs::create_dir_all(rig.store().dir()).unwrap();
        std::fs::write(rig.store().dir().join("pending.json"), b"{nope").unwrap();
        let (tx, _task) = rig.start(Slot::A);
        tx.send(request(&good_sha())).await.unwrap();
        rig.until(|r| *r.rebooter.calls.lock().unwrap() == 1).await;
    }

    #[tokio::test]
    async fn shutdown_during_a_download_leaves_it_for_the_next_start() {
        let rig = Rig::new();
        rig.fetcher.failures.lock().unwrap().extend((0..10).map(|_| TransportError::Network("down".into())));
        let mut deps = rig.deps(Slot::A);
        deps.timings.retry_first = Duration::from_secs(60);
        let (tx, rx) = mpsc::channel(4);
        let task = tokio::spawn(run_ota(deps, rx, rig.shutdown.subscribe()));
        tx.send(request(&good_sha())).await.unwrap();
        rig.until(|r| *r.fetcher.calls.lock().unwrap() == 1).await;
        rig.shutdown.send_replace(true);
        task.await.unwrap();
        assert_eq!(rig.store().load().unwrap().unwrap().phase, Phase::Downloading);
    }

    #[tokio::test]
    async fn an_outcome_the_brain_may_not_have_heard_is_reported_again_after_a_restart() {
        let rig = Rig::new();
        rig.store().save(&Pending { command_id: Some("X".into()), bundle: Some(BUNDLE.into()), version: "v".into(),
                                    target_slot: Slot::B, phase: Phase::Failed, error: Some("boom".into()) }).unwrap();
        let (_tx, _task) = rig.start(Slot::A);
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Failed)).await;
        assert_eq!(rig.ota().unwrap().error.as_deref(), Some("boom"));
        assert!(rig.rauc.calls.lock().unwrap().is_empty(), "delivering an outcome touches nothing else");
        rig.heartbeat();
        rig.heartbeat();
        rig.until(|r| r.ota().is_none()).await;
        assert_eq!(rig.store().load().unwrap(), None);
    }

    #[tokio::test]
    async fn downloads_give_up_after_the_attempts_and_clean_up() {
        let rig = Rig::new();
        rig.fetcher.failures.lock().unwrap().extend((0..3).map(|_| TransportError::Network("down".into())));
        let (tx, _task) = rig.start(Slot::A);
        tx.send(request(&good_sha())).await.unwrap();
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Failed)).await;
        assert_eq!(rig.ota().unwrap().error.as_deref(), Some("download failed: network error: down"));
        assert_eq!(*rig.fetcher.calls.lock().unwrap(), 3);
        assert!(rig.rauc.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn an_unreadable_disk_refuses_the_update() {
        struct BrokenDisk;
        impl DiskUsage for BrokenDisk {
            fn used_pct(&self) -> std::io::Result<f32> {
                Err(std::io::Error::other("statvfs failed"))
            }
        }
        let rig = Rig::new();
        let mut deps = rig.deps(Slot::A);
        deps.disk = Arc::new(BrokenDisk);
        let (tx, rx) = mpsc::channel(4);
        let _task = tokio::spawn(run_ota(deps, rx, rig.shutdown.subscribe()));
        tx.send(request(&good_sha())).await.unwrap();
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Failed)).await;
        assert!(rig.ota().unwrap().error.unwrap().contains("disk usage unreadable: statvfs failed"));
        assert_eq!(*rig.fetcher.calls.lock().unwrap(), 0);
    }

    #[tokio::test]
    async fn shutdown_while_waiting_to_confirm_leaves_the_trial_pending() {
        let rig = Rig::new();
        rig.store().save(&Pending { command_id: Some("X".into()), bundle: Some(BUNDLE.into()), version: "v".into(),
                                    target_slot: Slot::B, phase: Phase::Rebooting, error: None }).unwrap();
        let (_tx, task) = rig.start(Slot::B);
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Trying)).await;
        rig.shutdown.send_replace(true);
        task.await.unwrap();
        assert!(rig.rauc.calls.lock().unwrap().is_empty(), "an unconfirmed trial is never marked good");
        assert_eq!(rig.store().load().unwrap().unwrap().phase, Phase::Rebooting);
    }

    #[tokio::test]
    async fn the_task_is_busy_from_taking_an_update_until_its_outcome_is_delivered_and_free_after_a_failure() {
        let rig = Rig::new();
        let gated = Arc::new(GatedFetcher::new(BODY));
        let mut deps = rig.deps(Slot::A);
        deps.fetcher = gated.clone();
        let (tx, rx) = mpsc::channel(4);
        let _task = tokio::spawn(run_ota(deps, rx, rig.shutdown.subscribe()));
        rig.until(|r| !r.busy()).await;

        // The first update is rejected by the checksum, but only after the fetcher is let go.
        tx.send(request(&"0".repeat(64))).await.unwrap();
        tokio::time::timeout(Duration::from_secs(5), gated.entered.notified()).await.unwrap();
        assert!(rig.busy(), "busy while downloading");
        gated.release.notify_one();
        rig.until(|r| r.ota().is_some_and(|o| o.state == OtaState::Failed)).await;
        assert!(rig.busy(), "the failed outcome still occupies the report until the brain heard it");
        rig.heartbeat();
        rig.heartbeat();
        rig.until(|r| r.ota().is_none() && !r.busy()).await;

        // Free again: the next update is accepted and runs to the trial reboot.
        gated.release.notify_one();
        tx.send(request(&good_sha())).await.unwrap();
        rig.until(|r| *r.rebooter.calls.lock().unwrap() == 1).await;
        assert_eq!(*gated.inner.calls.lock().unwrap(), 2);
        rig.until(|r| !r.busy()).await;
    }
}
