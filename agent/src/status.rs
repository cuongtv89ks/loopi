//! Live agent status shared between tasks; feeds the LED, the gates and the heartbeat.
use std::sync::Arc;

use tokio::sync::watch;

/// Whether the privacy LED is known to work. Recording is allowed only while it is `Working`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum LedHealth {
    /// No LED write has succeeded yet.
    #[default]
    Unconfirmed,
    /// The most recent write succeeded (or fewer than `led::LED_FAILURE_LIMIT` failed since).
    Working,
    /// The LED could not be opened, or `led::LED_FAILURE_LIMIT` writes in a row failed.
    Failed,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct AgentStatus {
    pub paired: bool,
    pub clock_synced: bool,
    pub quiet: bool,
    pub recording_video: bool,
    pub recording_audio: bool,
    pub camera_failures: u32,
    pub mic_missing: bool,
    pub disk_used_pct: f32,
    pub disk_stopped: bool,
    pub led: LedHealth,
    pub last_error: Option<String>,
    pub dropped_since_heartbeat: u32,
    /// The current or latest update, until the brain has heard its outcome (P4a design §6).
    pub ota: Option<crate::ota::OtaReport>,
}

/// Shared sender: every task updates its own fields with `send_modify`; readers `subscribe()`.
pub type StatusTx = Arc<watch::Sender<AgentStatus>>;

pub fn status_channel(initial: AgentStatus) -> StatusTx {
    Arc::new(watch::channel(initial).0)
}
