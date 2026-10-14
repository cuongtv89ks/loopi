//! Upload loop: oldest item first; quarantine what the brain will never accept; back off on errors.
//!
//! Every quarantine is reported (`last_error` plus the dropped-items count of the next heartbeat),
//! and a breaker stops a systematic rejection from draining the whole backlog into quarantine.
use std::io;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::watch;

use crate::item::ItemMeta;
use crate::queue::Queue;
use crate::status::StatusTx;
use crate::transport::{PutOutcome, Transport, TransportError};

pub const MAX_BACKOFF: Duration = Duration::from_secs(300);
/// Quarantines in a row with the same HTTP status after which further such items are held.
pub const BREAKER_LIMIT: u32 = 3;

pub fn backoff_delay(failures: u32) -> Duration {
    let secs = 1u64.checked_shl(failures.min(20)).unwrap_or(u64::MAX);
    Duration::from_secs(secs).min(MAX_BACKOFF)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UploadStep {
    Uploaded(String),
    Idle,
    Quarantined { id: String, reason: String },
    Blocked(String),
}

/// After `BREAKER_LIMIT` quarantines in a row with the same HTTP status (a misconfigured brain or
/// proxy rather than bad items), items answered with that status are held with backoff instead
/// of quarantined, until the brain accepts an item or answers with a different status.
#[derive(Debug, Default)]
pub struct QuarantineBreaker {
    status: Option<u16>,
    count: u32,
}

impl QuarantineBreaker {
    fn holds(&self, status: u16) -> bool {
        self.status == Some(status) && self.count >= BREAKER_LIMIT
    }

    fn record(&mut self, status: u16) {
        self.count = if self.status == Some(status) { self.count.saturating_add(1) } else { 1 };
        self.status = Some(status);
    }

    fn reset(&mut self) {
        *self = Self::default();
    }
}

fn rejection_reason(status: u16) -> &'static str {
    match status {
        409 => "brain already has a different item with this id",
        413 => "item is larger than the brain accepts",
        422 => "brain rejected the item's headers or checksum",
        _ => "brain rejected the item",
    }
}

fn quarantine(queue: &Queue, meta: &ItemMeta, reason: String) -> io::Result<UploadStep> {
    tracing::warn!(id = %meta.id, %reason, "quarantining item");
    queue.quarantine(meta, &reason)?;
    Ok(UploadStep::Quarantined { id: meta.id.clone(), reason })
}

fn reject(queue: &Queue, meta: &ItemMeta, status: u16, breaker: &mut QuarantineBreaker) -> io::Result<UploadStep> {
    if breaker.holds(status) {
        return Ok(UploadStep::Blocked(format!(
            "brain rejected {BREAKER_LIMIT} items in a row with HTTP {status}; holding the queue instead of quarantining more"
        )));
    }
    let step = quarantine(queue, meta, format!("HTTP {status}: {}", rejection_reason(status)))?;
    breaker.record(status);
    Ok(step)
}

pub async fn upload_once(queue: &Queue, transport: &dyn Transport, breaker: &mut QuarantineBreaker) -> io::Result<UploadStep> {
    let Some(item) = queue.oldest()? else { return Ok(UploadStep::Idle) };
    match transport.put_item(&item.meta, &item.data_path).await {
        Ok(PutOutcome::Created | PutOutcome::Duplicate) => {
            queue.remove(&item.meta)?;
            breaker.reset();
            Ok(UploadStep::Uploaded(item.meta.id))
        }
        Ok(PutOutcome::Conflict) => reject(queue, &item.meta, 409, breaker),
        Ok(PutOutcome::Rejected(status)) => reject(queue, &item.meta, status, breaker),
        // Local and certainly item-specific; it never feeds the breaker, which could otherwise
        // hold an item that no brain answer can ever release.
        Err(TransportError::Io(e)) if e.kind() == io::ErrorKind::NotFound => {
            quarantine(queue, &item.meta, "payload file missing".into())
        }
        Ok(PutOutcome::BrainFull) => Ok(UploadStep::Blocked("brain storage is full".into())),
        Ok(PutOutcome::Unauthorized) => Ok(UploadStep::Blocked("brain rejected the device certificate".into())),
        Ok(PutOutcome::UpgradeRequired) => Ok(UploadStep::Blocked("agent is too old for the brain".into())),
        Err(e) => Ok(UploadStep::Blocked(e.to_string())),
    }
}

/// A quarantined item never reaches the brain: count it with the dropped items and say why.
fn report_quarantine(status: &StatusTx, id: &str, reason: &str) {
    let message = format!("quarantined {id}: {reason}");
    status.send_modify(|s| {
        s.dropped_since_heartbeat = s.dropped_since_heartbeat.saturating_add(1);
        s.last_error = Some(message);
    });
}

pub async fn run_uploader(
    queue: Arc<Queue>,
    transport: Arc<dyn Transport>,
    status: StatusTx,
    mut shutdown: watch::Receiver<bool>,
    idle_poll: Duration,
) {
    let mut failures = 0u32;
    let mut breaker = QuarantineBreaker::default();
    // Only clear an error this loop reported, never one set by another task.
    let mut reported: Option<String> = None;
    loop {
        if *shutdown.borrow() {
            return;
        }
        // A PUT is idempotent, so a slow upload is simply abandoned on shutdown.
        let step = tokio::select! {
            step = upload_once(&queue, transport.as_ref(), &mut breaker) => step,
            changed = shutdown.changed() => {
                if changed.is_err() || *shutdown.borrow() {
                    return;
                }
                continue;
            }
        };
        let delay = match step {
            Ok(UploadStep::Uploaded(_)) => {
                failures = 0;
                if let Some(old) = reported.take() {
                    status.send_if_modified(|s| {
                        let ours = s.last_error.as_ref() == Some(&old);
                        if ours {
                            s.last_error = None;
                        }
                        ours
                    });
                }
                Duration::ZERO
            }
            Ok(UploadStep::Quarantined { id, reason }) => {
                failures = 0;
                // Not remembered in `reported`: a later upload must not clear it before the
                // heartbeat has carried it to the brain.
                report_quarantine(&status, &id, &reason);
                Duration::ZERO
            }
            Ok(UploadStep::Idle) => {
                failures = 0;
                idle_poll
            }
            Ok(UploadStep::Blocked(reason)) => {
                let delay = backoff_delay(failures);
                failures = failures.saturating_add(1);
                tracing::warn!(%reason, retry_in = ?delay, "upload blocked");
                reported = Some(reason.clone());
                status.send_modify(|s| s.last_error = Some(reason));
                delay
            }
            Err(e) => {
                let delay = backoff_delay(failures);
                failures = failures.saturating_add(1);
                tracing::error!(error = %e, "queue error");
                let message = format!("queue error: {e}");
                reported = Some(message.clone());
                status.send_modify(|s| s.last_error = Some(message));
                delay
            }
        };
        if delay.is_zero() {
            // Dropped sender = no one can ever ask us to stop; do not drain forever.
            if shutdown.has_changed().is_err() {
                return;
            }
            tokio::task::yield_now().await;
            continue;
        }
        tokio::select! {
            _ = tokio::time::sleep(delay) => {}
            changed = shutdown.changed() => {
                // A dropped sender can never signal again: treat it as shutdown instead of spinning.
                if changed.is_err() {
                    return;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::item::ItemKind;
    use crate::status::{status_channel, AgentStatus};
    use crate::testing::FakeTransport;
    use chrono::Utc;

    fn queue_with(n: usize) -> (tempfile::TempDir, Queue) {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        for _ in 0..n {
            queue.enqueue_bytes(b"x", ItemKind::Audio, Utc::now(), Utc::now()).unwrap();
            std::thread::sleep(Duration::from_millis(2));
        }
        (tmp, queue)
    }

    #[tokio::test]
    async fn created_and_duplicate_items_are_removed_oldest_first() {
        let (_t, queue) = queue_with(2);
        let first = queue.oldest().unwrap().unwrap().meta.id;
        let transport = FakeTransport::default();
        transport.push_put(Ok(PutOutcome::Duplicate));
        assert_eq!(upload_once(&queue, &transport, &mut QuarantineBreaker::default()).await.unwrap(), UploadStep::Uploaded(first.clone()));
        assert!(matches!(upload_once(&queue, &transport, &mut QuarantineBreaker::default()).await.unwrap(), UploadStep::Uploaded(_)));
        assert_eq!(upload_once(&queue, &transport, &mut QuarantineBreaker::default()).await.unwrap(), UploadStep::Idle);
        assert_eq!(transport.puts.lock().unwrap()[0].id, first);
    }

    #[tokio::test]
    async fn rejected_and_conflicting_items_are_quarantined() {
        let (tmp, queue) = queue_with(2);
        let transport = FakeTransport::default();
        transport.push_put(Ok(PutOutcome::Conflict));
        transport.push_put(Ok(PutOutcome::Rejected(422)));
        let ids: Vec<String> = queue.items_of_kind(ItemKind::Audio).unwrap().iter().map(|i| i.meta.id.clone()).collect();
        assert!(matches!(upload_once(&queue, &transport, &mut QuarantineBreaker::default()).await.unwrap(), UploadStep::Quarantined { .. }));
        assert!(matches!(upload_once(&queue, &transport, &mut QuarantineBreaker::default()).await.unwrap(), UploadStep::Quarantined { .. }));
        assert!(queue.is_empty().unwrap());
        let quarantine = tmp.path().join("q").join("quarantine");
        for (id, expected) in ids.iter().zip(["409", "422"]) {
            assert!(quarantine.join(format!("{id}.{}", ItemKind::Audio.extension())).exists(), "payload kept");
            let reason = std::fs::read_to_string(quarantine.join(format!("{id}.reason"))).unwrap();
            assert!(reason.contains(expected), "reason {reason:?} should mention {expected}");
        }
    }

    #[tokio::test]
    async fn brain_full_auth_and_network_errors_keep_the_item() {
        let (_t, queue) = queue_with(1);
        let transport = FakeTransport::default();
        transport.push_put(Ok(PutOutcome::BrainFull));
        transport.push_put(Ok(PutOutcome::Unauthorized));
        transport.push_put(Err(TransportError::Network("unreachable".into())));
        let steps = [
            upload_once(&queue, &transport, &mut QuarantineBreaker::default()).await.unwrap(),
            upload_once(&queue, &transport, &mut QuarantineBreaker::default()).await.unwrap(),
            upload_once(&queue, &transport, &mut QuarantineBreaker::default()).await.unwrap(),
        ];
        assert_eq!(steps[0], UploadStep::Blocked("brain storage is full".into()));
        assert!(matches!(steps[1], UploadStep::Blocked(_)));
        assert!(matches!(&steps[2], UploadStep::Blocked(m) if m.contains("unreachable")));
        assert_eq!(queue.len().unwrap(), 1);
    }

    #[tokio::test]
    async fn missing_payload_is_quarantined_not_retried_forever() {
        let (tmp, queue) = queue_with(1);
        let item = queue.oldest().unwrap().unwrap();
        std::fs::remove_file(&item.data_path).unwrap();
        let transport = FakeTransport::default();
        assert!(matches!(upload_once(&queue, &transport, &mut QuarantineBreaker::default()).await.unwrap(), UploadStep::Quarantined { .. }));
        assert!(queue.is_empty().unwrap());
        let quarantine = tmp.path().join("q").join("quarantine");
        let reason = std::fs::read_to_string(quarantine.join(format!("{}.reason", item.meta.id))).unwrap();
        assert!(reason.contains("payload file missing"));
        assert!(quarantine.join(format!("{}.meta", item.meta.id)).exists(), "sidecar kept");
    }

    #[tokio::test]
    async fn retryable_statuses_keep_the_item() {
        let (_t, queue) = queue_with(1);
        let transport = FakeTransport::default();
        for code in [400u16, 404, 405, 500] {
            transport.push_put(Err(TransportError::Status(code)));
        }
        let mut breaker = QuarantineBreaker::default();
        for code in [400u16, 404, 405, 500] {
            let step = upload_once(&queue, &transport, &mut breaker).await.unwrap();
            assert!(matches!(&step, UploadStep::Blocked(m) if m.contains(&code.to_string())), "{code}: {step:?}");
        }
        assert_eq!(queue.len().unwrap(), 1);
    }

    fn ids_of(queue: &Queue) -> Vec<String> {
        queue.items_of_kind(ItemKind::Audio).unwrap().into_iter().map(|i| i.meta.id).collect()
    }

    #[tokio::test]
    async fn breaker_holds_the_queue_after_three_same_status_quarantines() {
        let (_t, queue) = queue_with(6);
        let ids = ids_of(&queue);
        let transport = FakeTransport::default();
        for _ in 0..5 {
            transport.push_put(Ok(PutOutcome::Rejected(422)));
        }
        let mut breaker = QuarantineBreaker::default();
        for id in &ids[..3] {
            let step = upload_once(&queue, &transport, &mut breaker).await.unwrap();
            assert!(matches!(&step, UploadStep::Quarantined { id: q, reason } if q == id && reason.starts_with("HTTP 422")), "{step:?}");
        }
        for _ in 0..2 {
            let step = upload_once(&queue, &transport, &mut breaker).await.unwrap();
            assert!(matches!(&step, UploadStep::Blocked(m) if m.contains("HTTP 422")), "{step:?}");
        }
        assert_eq!(ids_of(&queue), ids[3..].to_vec(), "held items stay queued");

        // The brain accepts the held item: the breaker resets and a later 422 quarantines again.
        assert_eq!(upload_once(&queue, &transport, &mut breaker).await.unwrap(), UploadStep::Uploaded(ids[3].clone()));
        transport.push_put(Ok(PutOutcome::Rejected(422)));
        assert!(matches!(upload_once(&queue, &transport, &mut breaker).await.unwrap(), UploadStep::Quarantined { .. }));
    }

    #[tokio::test]
    async fn breaker_counts_only_the_same_status_in_a_row() {
        let (_t, queue) = queue_with(5);
        let transport = FakeTransport::default();
        for outcome in [PutOutcome::Rejected(422), PutOutcome::Rejected(422), PutOutcome::Conflict, PutOutcome::Rejected(422), PutOutcome::Rejected(422)] {
            transport.push_put(Ok(outcome));
        }
        let mut breaker = QuarantineBreaker::default();
        for _ in 0..5 {
            let step = upload_once(&queue, &transport, &mut breaker).await.unwrap();
            assert!(matches!(step, UploadStep::Quarantined { .. }), "{step:?}");
        }
        assert!(queue.is_empty().unwrap());
    }

    #[tokio::test]
    async fn missing_payloads_never_trip_the_breaker() {
        let (_t, queue) = queue_with(4);
        for item in queue.items_of_kind(ItemKind::Audio).unwrap() {
            std::fs::remove_file(&item.data_path).unwrap();
        }
        let mut breaker = QuarantineBreaker::default();
        for _ in 0..4 {
            let step = upload_once(&queue, &FakeTransport::default(), &mut breaker).await.unwrap();
            assert!(matches!(step, UploadStep::Quarantined { .. }), "{step:?}");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn every_quarantine_is_reported_and_counted_as_dropped() {
        let (_t, queue) = queue_with(2);
        let ids = ids_of(&queue);
        let queue = Arc::new(queue);
        let transport = Arc::new(FakeTransport::default());
        transport.push_put(Ok(PutOutcome::Conflict));
        let status = status_channel(AgentStatus { dropped_since_heartbeat: 1, ..AgentStatus::default() });
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(run_uploader(queue.clone(), transport.clone(), status.clone(), stop_rx, Duration::from_millis(10)));
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(queue.is_empty().unwrap(), "the second item was uploaded");
        let snapshot = status.borrow().clone();
        assert_eq!(snapshot.dropped_since_heartbeat, 2, "1 carried + 1 quarantined");
        let error = snapshot.last_error.unwrap();
        assert!(error.starts_with(&format!("quarantined {}: HTTP 409", ids[0])), "{error}");
        stop_tx.send(true).unwrap();
        task.await.unwrap();
    }

    #[test]
    fn backoff_doubles_and_caps() {
        assert_eq!(backoff_delay(0), Duration::from_secs(1));
        assert_eq!(backoff_delay(3), Duration::from_secs(8));
        assert_eq!(backoff_delay(9), MAX_BACKOFF);
        assert_eq!(backoff_delay(u32::MAX), MAX_BACKOFF);
    }

    #[tokio::test(start_paused = true)]
    async fn run_uploader_retries_after_network_error_then_drains() {
        let (_t, queue) = queue_with(3);
        let queue = Arc::new(queue);
        let transport = Arc::new(FakeTransport::default());
        transport.push_put(Err(TransportError::Network("down".into())));
        let status = status_channel(AgentStatus::default());
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(run_uploader(queue.clone(), transport.clone(), status.clone(), stop_rx, Duration::from_millis(10)));
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert_eq!(queue.len().unwrap(), 3);
        assert!(status.borrow().last_error.as_deref().unwrap_or_default().contains("down"));
        for _ in 0..100 {
            if queue.is_empty().unwrap() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        assert!(queue.is_empty().unwrap());
        assert_eq!(transport.puts.lock().unwrap().len(), 4);
        assert_eq!(status.borrow().last_error, None);
        stop_tx.send(true).unwrap();
        task.await.unwrap();
    }

    struct HangingTransport;

    #[async_trait::async_trait]
    impl Transport for HangingTransport {
        async fn put_item(&self, _meta: &ItemMeta, _data: &std::path::Path) -> Result<PutOutcome, TransportError> {
            std::future::pending().await
        }

        async fn heartbeat(
            &self,
            _request: &crate::wire::HeartbeatRequest,
        ) -> Result<Vec<crate::wire::RemoteCommand>, TransportError> {
            std::future::pending().await
        }
    }

    #[tokio::test]
    async fn shutdown_abandons_an_upload_in_flight() {
        let (_t, queue) = queue_with(1);
        let queue = Arc::new(queue);
        let status = status_channel(AgentStatus::default());
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(run_uploader(queue.clone(), Arc::new(HangingTransport), status, stop_rx, Duration::from_millis(10)));
        tokio::time::sleep(Duration::from_millis(50)).await;
        stop_tx.send(true).unwrap();
        tokio::time::timeout(Duration::from_secs(2), task).await.expect("the upload is raced against shutdown").unwrap();
        assert_eq!(queue.len().unwrap(), 1, "the item stays queued for the next start");
    }

    // Real time on a multi-thread runtime: if the fix regresses and the loop never yields, the
    // timeout still fires on the other worker, so the test fails instead of hanging.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn dropped_shutdown_sender_stops_the_loop_without_hammering() {
        let (_t, queue) = queue_with(2);
        let queue = Arc::new(queue);
        let transport = Arc::new(FakeTransport::default());
        for _ in 0..50 {
            transport.push_put(Err(TransportError::Network("down".into())));
        }
        let status = status_channel(AgentStatus::default());
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(run_uploader(queue.clone(), transport.clone(), status, stop_rx, Duration::from_millis(10)));
        tokio::time::sleep(Duration::from_millis(200)).await;
        drop(stop_tx);
        tokio::time::timeout(Duration::from_secs(2), task).await.expect("loop must exit").unwrap();
        assert!(transport.puts.lock().unwrap().len() <= 2, "no burst of retries");
        assert_eq!(queue.len().unwrap(), 2);
    }
}
