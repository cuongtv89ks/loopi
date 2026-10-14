//! Upload loop: oldest item first; quarantine what the brain will never accept; back off on errors.
use std::io;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::watch;

use crate::queue::Queue;
use crate::status::StatusTx;
use crate::transport::{PutOutcome, Transport, TransportError};

pub const MAX_BACKOFF: Duration = Duration::from_secs(300);

pub fn backoff_delay(failures: u32) -> Duration {
    let secs = 1u64.checked_shl(failures.min(20)).unwrap_or(u64::MAX);
    Duration::from_secs(secs).min(MAX_BACKOFF)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UploadStep {
    Uploaded(String),
    Idle,
    Quarantined(String),
    Blocked(String),
}

pub async fn upload_once(queue: &Queue, transport: &dyn Transport) -> io::Result<UploadStep> {
    let Some(item) = queue.oldest()? else { return Ok(UploadStep::Idle) };
    let id = item.meta.id.clone();
    let quarantine = |reason: &str| -> io::Result<UploadStep> {
        tracing::warn!(id = %id, reason, "quarantining item");
        queue.quarantine(&item.meta, reason)?;
        Ok(UploadStep::Quarantined(id.clone()))
    };
    match transport.put_item(&item.meta, &item.data_path).await {
        Ok(PutOutcome::Created | PutOutcome::Duplicate) => {
            queue.remove(&item.meta)?;
            Ok(UploadStep::Uploaded(id))
        }
        Ok(PutOutcome::Conflict) => quarantine("brain reported a conflicting item with the same id (409)"),
        Ok(PutOutcome::Rejected(code)) => quarantine(&format!("brain rejected the item (HTTP {code})")),
        Err(TransportError::Io(e)) if e.kind() == io::ErrorKind::NotFound => quarantine("payload file missing"),
        Ok(PutOutcome::BrainFull) => Ok(UploadStep::Blocked("brain storage is full".into())),
        Ok(PutOutcome::Unauthorized) => Ok(UploadStep::Blocked("brain rejected the device certificate".into())),
        Ok(PutOutcome::UpgradeRequired) => Ok(UploadStep::Blocked("agent is too old for the brain".into())),
        Err(e) => Ok(UploadStep::Blocked(e.to_string())),
    }
}

pub async fn run_uploader(
    queue: Arc<Queue>,
    transport: Arc<dyn Transport>,
    status: StatusTx,
    mut shutdown: watch::Receiver<bool>,
    idle_poll: Duration,
) {
    let mut failures = 0u32;
    // Only clear an error this loop reported, never one set by another task.
    let mut reported: Option<String> = None;
    loop {
        if *shutdown.borrow() {
            return;
        }
        let delay = match upload_once(&queue, transport.as_ref()).await {
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
            Ok(UploadStep::Quarantined(_)) => {
                failures = 0;
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
            tokio::task::yield_now().await;
            continue;
        }
        tokio::select! {
            _ = tokio::time::sleep(delay) => {}
            _ = shutdown.changed() => {}
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
        assert_eq!(upload_once(&queue, &transport).await.unwrap(), UploadStep::Uploaded(first.clone()));
        assert!(matches!(upload_once(&queue, &transport).await.unwrap(), UploadStep::Uploaded(_)));
        assert_eq!(upload_once(&queue, &transport).await.unwrap(), UploadStep::Idle);
        assert_eq!(transport.puts.lock().unwrap()[0].id, first);
    }

    #[tokio::test]
    async fn rejected_and_conflicting_items_are_quarantined() {
        let (_t, queue) = queue_with(2);
        let transport = FakeTransport::default();
        transport.push_put(Ok(PutOutcome::Conflict));
        transport.push_put(Ok(PutOutcome::Rejected(422)));
        assert!(matches!(upload_once(&queue, &transport).await.unwrap(), UploadStep::Quarantined(_)));
        assert!(matches!(upload_once(&queue, &transport).await.unwrap(), UploadStep::Quarantined(_)));
        assert!(queue.is_empty().unwrap());
    }

    #[tokio::test]
    async fn brain_full_auth_and_network_errors_keep_the_item() {
        let (_t, queue) = queue_with(1);
        let transport = FakeTransport::default();
        transport.push_put(Ok(PutOutcome::BrainFull));
        transport.push_put(Ok(PutOutcome::Unauthorized));
        transport.push_put(Err(TransportError::Network("unreachable".into())));
        for _ in 0..3 {
            assert!(matches!(upload_once(&queue, &transport).await.unwrap(), UploadStep::Blocked(_)));
        }
        assert_eq!(queue.len().unwrap(), 1);
    }

    #[tokio::test]
    async fn missing_payload_is_quarantined_not_retried_forever() {
        let (_t, queue) = queue_with(1);
        let item = queue.oldest().unwrap().unwrap();
        std::fs::remove_file(&item.data_path).unwrap();
        let transport = FakeTransport::default();
        assert!(matches!(upload_once(&queue, &transport).await.unwrap(), UploadStep::Quarantined(_)));
        assert!(queue.is_empty().unwrap());
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
}

