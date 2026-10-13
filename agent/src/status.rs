//! Live agent status shared between tasks; feeds the LED, the gates and the heartbeat.
use std::sync::Arc;

use tokio::sync::watch;

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
    pub last_error: Option<String>,
    pub dropped_since_heartbeat: u32,
}

/// Shared sender: every task updates its own fields with `send_modify`; readers `subscribe()`.
pub type StatusTx = Arc<watch::Sender<AgentStatus>>;

pub fn status_channel(initial: AgentStatus) -> StatusTx {
    Arc::new(watch::channel(initial).0)
}
