//! Progress marker for the systemd watchdog: the main loops beat it, the watchdog only pings
//! while it is fresh, so a wedged loop gets the unit restarted instead of silently surviving.
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

/// Ping only if progress was seen this recently (systemd `WatchdogSec=30`).
pub const MAX_STALE: Duration = Duration::from_secs(20);

pub fn is_fresh(age: Duration, max_stale: Duration) -> bool {
    age <= max_stale
}

#[derive(Clone, Debug)]
pub struct Liveness {
    start: Instant,
    last_ms: Arc<AtomicU64>,
    beats: Arc<AtomicU64>,
}

impl Default for Liveness {
    fn default() -> Self {
        Self::new()
    }
}

impl Liveness {
    pub fn new() -> Self {
        Self { start: Instant::now(), last_ms: Arc::new(AtomicU64::new(0)), beats: Arc::new(AtomicU64::new(0)) }
    }

    /// Record that the owning loop made progress just now.
    pub fn beat(&self) {
        self.last_ms.store(self.start.elapsed().as_millis() as u64, Ordering::Relaxed);
        self.beats.fetch_add(1, Ordering::Relaxed);
    }

    pub fn beats(&self) -> u64 {
        self.beats.load(Ordering::Relaxed)
    }

    /// Time since the last beat (since creation if it never beat).
    pub fn age(&self) -> Duration {
        let now = self.start.elapsed().as_millis() as u64;
        Duration::from_millis(now.saturating_sub(self.last_ms.load(Ordering::Relaxed)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn freshness_boundary() {
        assert!(is_fresh(Duration::from_secs(0), MAX_STALE));
        assert!(is_fresh(MAX_STALE, MAX_STALE));
        assert!(!is_fresh(MAX_STALE + Duration::from_millis(1), MAX_STALE));
    }

    #[test]
    fn beat_resets_age_and_counts() {
        let live = Liveness::new();
        std::thread::sleep(Duration::from_millis(30));
        assert!(live.age() >= Duration::from_millis(30));
        live.beat();
        assert!(live.age() < Duration::from_millis(30));
        assert_eq!(live.beats(), 1);
        let clone = live.clone();
        clone.beat();
        assert_eq!(live.beats(), 2);
    }
}
