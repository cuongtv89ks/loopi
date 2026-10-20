//! Heartbeat: report status every 15 s, receive commands, ack them on the next heartbeat.
//!
//! The brain answers a contract violation with 422 and records nothing, so every request built
//! here must satisfy the Heartbeat schema: bounded ack list, bounded `last_error`, finite
//! `disk_used_pct` within 0..=100, finite optional readings.
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tokio::sync::{mpsc, watch};

use crate::control::{parse_remote_command, Command, ControlState};
use crate::led::LED_UNAVAILABLE_ERROR;
use crate::osinfo::OsInfo;
use crate::ota::{is_valid_version, OtaReport};
use crate::queue::Queue;
use crate::status::{AgentStatus, LedHealth, StatusTx};
use crate::sysinfo::SysProbe;
use crate::transport::{Transport, TransportError};
use crate::wire::{HeartbeatRequest, RemoteCommand, AGENT_VERSION};

pub const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(15);
/// Contract `last_error.maxLength`, counted in Unicode scalar values.
const MAX_ERROR_LEN: usize = 500;
/// Contract `acked_command_ids.maxItems`.
pub const MAX_ACKED_PER_HEARTBEAT: usize = 100;

/// Reported when no finite disk reading has ever been seen: assume the worst.
const UNKNOWN_DISK_PCT: f32 = 100.0;
const DISK_UNREADABLE: &str = "disk usage unreadable";
const CONTROL_UNAVAILABLE: &str = "control loop unavailable";

/// Last-resort clamp into the contract range; `heartbeat_once` substitutes the last finite
/// reading before this runs, so a non-finite value here reports the pessimistic default.
fn sanitize_disk_pct(pct: f32) -> f32 {
    if pct.is_finite() {
        pct.clamp(0.0, 100.0)
    } else {
        UNKNOWN_DISK_PCT
    }
}

/// Contract `Ulid`: `^[0-9A-HJKMNP-TV-Z]{26}$` (Crockford base 32, upper case).
fn is_ulid(text: &str) -> bool {
    text.len() == 26 && text.chars().all(|c| c.is_ascii_digit() || (c.is_ascii_uppercase() && !matches!(c, 'I' | 'L' | 'O' | 'U')))
}

/// The OTA report as the heartbeat may carry it. `/data/ota/pending.json` is shared by two OS slots and can be written
/// by hand, so a report the contract would refuse is left out (and logged) rather than failing every heartbeat.
fn contract_ota(status: &AgentStatus) -> Option<OtaReport> {
    let report = status.ota.as_ref()?;
    if is_valid_version(&report.version) && report.command_id.as_deref().map_or(true, is_ulid) {
        return Some(report.clone());
    }
    tracing::warn!(version = %report.version, command_id = ?report.command_id, "not reporting an update the protocol cannot carry");
    None
}

pub fn build_request(
    status: &AgentStatus,
    control: &ControlState,
    queue_depth: u64,
    uptime_s: u64,
    probe: &dyn SysProbe,
    mut acked: Vec<String>,
    os: &OsInfo,
) -> HeartbeatRequest {
    acked.truncate(MAX_ACKED_PER_HEARTBEAT);
    HeartbeatRequest {
        agent_version: AGENT_VERSION.into(),
        uptime_s,
        soc_temp_c: probe.soc_temp_c().filter(|t| t.is_finite()),
        disk_used_pct: sanitize_disk_pct(status.disk_used_pct),
        queue_depth,
        wifi_rssi_dbm: probe.wifi_rssi_dbm(),
        clock_synced: status.clock_synced,
        paused: control.paused,
        audio_enabled: control.audio_enabled,
        recording_video: status.recording_video,
        recording_audio: status.recording_audio,
        mic_missing: status.mic_missing,
        camera_failures: status.camera_failures,
        last_error: status.last_error.as_ref().map(|e| e.chars().take(MAX_ERROR_LEN).collect()),
        dropped_items: status.dropped_since_heartbeat,
        acked_command_ids: acked,
        os_version: os.version.clone(),
        os_variant: os.variant,
        boot_slot: os.slot,
        ota: contract_ota(status),
    }
}

#[derive(Debug, Default, PartialEq)]
pub struct Translated {
    pub commands: Vec<Command>,
    pub acked: Vec<String>,
    pub errors: Vec<String>,
    /// Ids of the valid commands, parallel to `commands`.
    pub command_ids: Vec<String>,
    /// Ids of commands that failed to parse (acked immediately).
    pub invalid_ids: Vec<String>,
}

/// Every received command is acked — invalid ones too, so the brain stops re-sending them;
/// the reasons are reported through `last_error`.
pub fn translate(remote: Vec<RemoteCommand>) -> Translated {
    let mut out = Translated::default();
    for command in remote {
        let parsed = if command.kind == "update" {
            crate::ota::parse_update(&command.id, &command.payload).map(Command::Update)
        } else {
            parse_remote_command(&command.kind, &command.payload)
        };
        match parsed {
            Ok(parsed) => {
                out.commands.push(parsed);
                out.command_ids.push(command.id.clone());
            }
            Err(e) => {
                out.errors.push(format!("command {}: {e}", command.id));
                out.invalid_ids.push(command.id.clone());
            }
        }
        out.acked.push(command.id);
    }
    out
}

pub struct HeartbeatDeps {
    pub transport: Arc<dyn Transport>,
    pub queue: Arc<Queue>,
    pub status: StatusTx,
    pub control: watch::Receiver<ControlState>,
    pub commands: mpsc::Sender<Command>,
    pub probe: Arc<dyn SysProbe>,
    pub started: Instant,
    pub interval: Duration,
    /// Last queue depth that could be read; reported when the queue becomes unreadable.
    pub last_queue_depth: AtomicU64,
    /// What this device runs (version, variant, boot slot); reported in every heartbeat.
    pub os: OsInfo,
    /// Counts heartbeats the brain answered with 200; the OTA task waits on it (P4a design §6).
    pub delivered: Arc<watch::Sender<u64>>,
    memory: Mutex<Memory>,
}

/// State carried between heartbeats that is reported through `last_error` overlays.
#[derive(Default)]
struct Memory {
    /// Command problems not yet delivered to the brain; dropped once a heartbeat carrying them succeeds.
    pending_error: Option<String>,
    last_disk_pct: Option<f32>,
}

impl HeartbeatDeps {
    pub fn new(
        transport: Arc<dyn Transport>,
        queue: Arc<Queue>,
        status: StatusTx,
        control: watch::Receiver<ControlState>,
        commands: mpsc::Sender<Command>,
        probe: Arc<dyn SysProbe>,
        interval: Duration,
    ) -> Self {
        Self {
            transport,
            queue,
            status,
            control,
            commands,
            probe,
            started: Instant::now(),
            interval,
            last_queue_depth: AtomicU64::new(0),
            os: OsInfo::default(),
            delivered: Arc::new(watch::channel(0).0),
            memory: Mutex::default(),
        }
    }

    #[must_use]
    pub fn with_os(self, os: OsInfo) -> Self {
        Self { os, ..self }
    }

    #[must_use]
    pub fn with_delivered(self, delivered: Arc<watch::Sender<u64>>) -> Self {
        Self { delivered, ..self }
    }

    /// Queue depth plus, when the queue cannot be read, a message for `last_error`.
    fn queue_depth(&self) -> (u64, Option<String>) {
        match self.queue.len() {
            Ok(n) => {
                self.last_queue_depth.store(n as u64, Ordering::Relaxed);
                (n as u64, None)
            }
            Err(e) => (self.last_queue_depth.load(Ordering::Relaxed), Some(format!("queue unreadable: {e}"))),
        }
    }
}

/// One heartbeat. On failure the acks and the dropped-item count are kept for the next attempt;
/// at most `MAX_ACKED_PER_HEARTBEAT` acks go out at a time, the rest wait for the next one.
/// `last_error` is one slot that other tasks overwrite; while the LED is failed (recording
/// disabled) the brain must still learn why, so that reason is restated unless already present.
fn led_overlay(status: &AgentStatus) -> Option<String> {
    let already = status.last_error.as_deref().is_some_and(|e| e.starts_with(LED_UNAVAILABLE_ERROR));
    (status.led == LedHealth::Failed && !already).then(|| LED_UNAVAILABLE_ERROR.to_string())
}

pub async fn heartbeat_once(deps: &HeartbeatDeps, acked: &mut Vec<String>) -> Result<(), TransportError> {
    let (depth, queue_error) = deps.queue_depth();
    let (request, carried_error) = {
        let mut status = deps.status.borrow().clone();
        let mut memory = deps.memory.lock().unwrap_or_else(|p| p.into_inner());
        // Most important first: truncation to MAX_ERROR_LEN drops the tail, so the LED fault and
        // the status error (camera, disk, ...) go before the per-heartbeat overlays.
        let mut parts: Vec<String> = led_overlay(&status).into_iter().collect();
        parts.extend(status.last_error.take());
        parts.extend(queue_error);
        if status.disk_used_pct.is_finite() {
            memory.last_disk_pct = Some(status.disk_used_pct);
        } else {
            status.disk_used_pct = memory.last_disk_pct.unwrap_or(UNKNOWN_DISK_PCT);
            parts.push(DISK_UNREADABLE.into());
        }
        let carried = memory.pending_error.clone();
        parts.extend(carried.clone());
        status.last_error = (!parts.is_empty()).then(|| parts.join("; "));
        let control = *deps.control.borrow();
        let request =
            build_request(&status, &control, depth, deps.started.elapsed().as_secs(), deps.probe.as_ref(), acked.clone(), &deps.os);
        (request, carried)
    };
    let remote = deps.transport.heartbeat(&request).await?;
    deps.delivered.send_modify(|count| *count += 1);
    acked.drain(..request.acked_command_ids.len());
    let reported = request.dropped_items;
    deps.status.send_modify(|s| s.dropped_since_heartbeat = s.dropped_since_heartbeat.saturating_sub(reported));
    let translated = translate(remote);
    let mut errors = translated.errors;
    // Invalid commands are acked now; valid ones only once they are in the control channel.
    acked.extend(translated.invalid_ids);
    for (id, command) in translated.command_ids.into_iter().zip(translated.commands) {
        if deps.commands.send(command).await.is_err() {
            tracing::error!("control loop unavailable, leaving commands un-acked for redelivery");
            errors.push(CONTROL_UNAVAILABLE.into());
            break;
        }
        acked.push(id);
    }
    let mut memory = deps.memory.lock().unwrap_or_else(|p| p.into_inner());
    if memory.pending_error == carried_error {
        memory.pending_error = None;
    }
    if !errors.is_empty() {
        let error = errors.join("; ");
        tracing::warn!(%error, "command problem");
        memory.pending_error = Some(error);
    }
    Ok(())
}

pub async fn run_heartbeat(deps: HeartbeatDeps, mut shutdown: watch::Receiver<bool>) {
    let mut acked = Vec::new();
    let mut ticker = tokio::time::interval(deps.interval);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            _ = ticker.tick() => {}
            // A dropped sender means nobody can ever stop us: treat it as shutdown, never spin.
            changed = shutdown.changed() => {
                if changed.is_err() || *shutdown.borrow() {
                    return;
                }
                continue;
            }
        }
        let result = tokio::select! {
            result = heartbeat_once(&deps, &mut acked) => result,
            changed = shutdown.changed() => {
                if changed.is_err() || *shutdown.borrow() {
                    return;
                }
                continue;
            }
        };
        if let Err(e) = result {
            tracing::warn!(error = %e, "heartbeat failed");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::item::ItemKind;
    use crate::sysinfo::NoProbe;
    use std::sync::atomic::AtomicU64;
    use crate::status::status_channel;
    use crate::testing::FakeTransport;
    use crate::transport::TransportError;
    use crate::wire::contract::assert_matches;
    use serde_json::json;

    const CMD: &str = "01JA2B3C4D5E6F7G8H9J0KMNPQ";

    struct FixedProbe;
    impl SysProbe for FixedProbe {
        fn soc_temp_c(&self) -> Option<f32> {
            Some(47.5)
        }
        fn wifi_rssi_dbm(&self) -> Option<i32> {
            Some(-60)
        }
    }

    fn remote(kind: &str, payload: serde_json::Value) -> RemoteCommand {
        RemoteCommand { id: CMD.into(), kind: kind.into(), payload }
    }

    #[test]
    fn request_maps_status_and_matches_contract() {
        let status = AgentStatus {
            paired: true,
            clock_synced: true,
            recording_video: true,
            camera_failures: 2,
            disk_used_pct: 33.0,
            last_error: Some("x".repeat(900)),
            dropped_since_heartbeat: 4,
            ..AgentStatus::default()
        };
        let control = ControlState { paused: false, audio_enabled: true };
        let request = build_request(&status, &control, 7, 99, &FixedProbe, vec![CMD.into()], &OsInfo::default());
        assert_eq!((request.queue_depth, request.uptime_s, request.dropped_items), (7, 99, 4));
        assert_eq!((request.soc_temp_c, request.wifi_rssi_dbm), (Some(47.5), Some(-60)));
        assert_eq!(request.last_error.as_ref().unwrap().chars().count(), 500);
        assert_matches("Heartbeat", &serde_json::to_value(&request).unwrap());
    }

    #[test]
    fn the_internal_sign_in_flag_is_not_on_the_wire() {
        let control = ControlState { paused: false, audio_enabled: true };
        let wire = |sign_in_refused| {
            let status = AgentStatus { paired: true, camera_failures: 1, sign_in_refused, ..AgentStatus::default() };
            serde_json::to_value(build_request(&status, &control, 0, 1, &FixedProbe, vec![], &OsInfo::default())).unwrap()
        };
        assert_eq!(wire(true), wire(false), "the protocol carries no sign-in field");
        assert_matches("Heartbeat", &wire(true));
    }

    #[test]
    fn invalid_commands_are_acked_and_reported() {
        let t = translate(vec![remote("pause", json!({})), remote("reboot", json!({}))]);
        assert_eq!(t.commands, vec![Command::Pause]);
        assert_eq!(t.acked.len(), 2);
        assert_eq!(t.errors.len(), 1);
    }

    fn deps(transport: Arc<FakeTransport>, status: StatusTx) -> (HeartbeatDeps, mpsc::Receiver<Command>, tempfile::TempDir) {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        queue.enqueue_bytes(b"x", ItemKind::Audio, chrono::Utc::now(), chrono::Utc::now()).unwrap();
        let (_ctl_tx, ctl_rx) = watch::channel(ControlState::initial(true));
        let (cmd_tx, cmd_rx) = mpsc::channel(8);
        let deps = HeartbeatDeps {
            transport,
            queue: Arc::new(queue),
            status,
            control: ctl_rx,
            commands: cmd_tx,
            probe: Arc::new(FixedProbe),
            started: Instant::now(),
            interval: HEARTBEAT_INTERVAL,
            last_queue_depth: AtomicU64::new(0),
            os: OsInfo::default(),
            delivered: Arc::new(watch::channel(0).0),
            memory: Mutex::default(),
        };
        (deps, cmd_rx, tmp)
    }

    #[test]
    fn request_carries_os_info_and_the_ota_report_and_matches_contract() {
        use crate::ota::{OtaReport, OtaState, Slot, Variant};
        let status = AgentStatus {
            ota: Some(OtaReport::new(Some(CMD.into()), "2026.10.21-a1b2c3d", OtaState::Trying, None)),
            ..AgentStatus::default()
        };
        let os = OsInfo { version: Some("2026.10.21-a1b2c3d".into()), variant: Some(Variant::Dev), slot: Some(Slot::B) };
        let control = ControlState { paused: false, audio_enabled: true };
        let value = serde_json::to_value(build_request(&status, &control, 0, 1, &FixedProbe, vec![], &os)).unwrap();
        assert_eq!((value["os_version"].as_str(), value["os_variant"].as_str(), value["boot_slot"].as_str()),
                   (Some("2026.10.21-a1b2c3d"), Some("dev"), Some("B")));
        assert_eq!(value["ota"]["state"], "trying");
        assert_matches("Heartbeat", &value);
        let bare = serde_json::to_value(build_request(&AgentStatus::default(), &control, 0, 1, &FixedProbe, vec![],
                                                      &OsInfo::default())).unwrap();
        for key in ["os_version", "os_variant", "boot_slot", "ota"] {
            assert!(bare.get(key).is_none(), "{key} must be omitted when unknown");
        }
    }

    #[test]
    fn an_ota_report_the_contract_would_refuse_is_left_out_of_the_heartbeat() {
        use crate::ota::{OtaReport, OtaState};
        let control = ControlState { paused: false, audio_enabled: true };
        let with = |report: OtaReport| {
            let status = AgentStatus { ota: Some(report), ..AgentStatus::default() };
            serde_json::to_value(build_request(&status, &control, 0, 1, &FixedProbe, vec![], &OsInfo::default())).unwrap()
        };
        // Kept: a valid version, with a valid ULID or with none (the hand-made first move to A/B slots).
        for command_id in [Some(CMD.to_string()), None] {
            let value = with(OtaReport::new(command_id, "2026.10.21-a1b2c3d", OtaState::Trying, None));
            assert_eq!(value["ota"]["state"], "trying");
            assert_matches("Heartbeat", &value);
        }
        // Dropped: what a hand-written /data/ota/pending.json could hold. The rest of the heartbeat still goes out.
        let bad = [
            OtaReport::new(Some(CMD.into()), "has space", OtaState::Rebooting, None),
            OtaReport::new(Some(CMD.into()), "", OtaState::Rebooting, None),
            OtaReport::new(Some(CMD.into()), &"x".repeat(65), OtaState::Rebooting, None),
            OtaReport::new(Some("not-a-ulid".into()), "v1", OtaState::Rebooting, None),
            OtaReport::new(Some(CMD.to_lowercase()), "v1", OtaState::Rebooting, None),
            OtaReport::new(Some("01JA2B3C4D5E6F7G8H9J0KMNPU".into()), "v1", OtaState::Rebooting, None),
            OtaReport::new(Some(String::new()), "v1", OtaState::Rebooting, None),
        ];
        for report in bad {
            let value = with(report.clone());
            assert!(value.get("ota").is_none(), "{report:?} must not reach the brain");
            assert_eq!(value["uptime_s"], 1, "the heartbeat itself still goes out");
            assert_matches("Heartbeat", &value);
        }
    }

    #[test]
    fn update_commands_translate_with_their_id() {
        let t = translate(vec![RemoteCommand {
            id: CMD.into(),
            kind: "update".into(),
            payload: json!({"bundle": "loopi-dev-2026.10.21-a1b2c3d.raucb", "version": "2026.10.21-a1b2c3d",
                            "variant": "dev", "sha256": "ab".repeat(32), "size": 10}),
        }]);
        assert!(t.errors.is_empty());
        match &t.commands[..] {
            [Command::Update(request)] => assert_eq!(request.command_id.as_deref(), Some(CMD)),
            other => panic!("{other:?}"),
        }
        let bad = translate(vec![remote("update", json!({}))]);
        assert_eq!((bad.commands.len(), bad.invalid_ids.len()), (0, 1));
    }

    #[tokio::test]
    async fn a_delivered_heartbeat_is_counted() {
        let transport = Arc::new(FakeTransport::default());
        let (deps, _commands, _tmp) = deps(transport.clone(), status_channel(AgentStatus::default()));
        let delivered = Arc::new(watch::channel(0u64).0);
        let deps = deps.with_delivered(delivered.clone());
        heartbeat_once(&deps, &mut Vec::new()).await.unwrap();
        assert_eq!(*delivered.borrow(), 1);
        transport.push_heartbeat(Err(TransportError::Status(500)));
        assert!(heartbeat_once(&deps, &mut Vec::new()).await.is_err());
        assert_eq!(*delivered.borrow(), 1, "a failed heartbeat is not counted");
        heartbeat_once(&deps, &mut Vec::new()).await.unwrap();
        assert_eq!(*delivered.borrow(), 2);
    }

    #[tokio::test]
    async fn the_heartbeat_reports_the_os_it_was_given() {
        use crate::ota::{Slot, Variant};
        let transport = Arc::new(FakeTransport::default());
        let (deps, _commands, _tmp) = deps(transport.clone(), status_channel(AgentStatus::default()));
        let deps = deps.with_os(OsInfo { version: Some("v1".into()), variant: Some(Variant::Prod), slot: Some(Slot::A) });
        heartbeat_once(&deps, &mut Vec::new()).await.unwrap();
        let sent = transport.heartbeats.lock().unwrap();
        assert_eq!(
            (sent[0].os_version.as_deref(), sent[0].os_variant, sent[0].boot_slot),
            (Some("v1"), Some(Variant::Prod), Some(Slot::A))
        );
        assert_matches("Heartbeat", &serde_json::to_value(&sent[0]).unwrap());
    }

    #[tokio::test]
    async fn commands_are_forwarded_and_acked_on_the_next_heartbeat() {
        let transport = Arc::new(FakeTransport::default());
        transport.push_heartbeat(Ok(vec![remote("pause", json!({}))]));
        let status = status_channel(AgentStatus::default());
        let (deps, mut commands, _tmp) = deps(transport.clone(), status);
        let mut acked = Vec::new();
        heartbeat_once(&deps, &mut acked).await.unwrap();
        assert_eq!(commands.recv().await, Some(Command::Pause));
        assert_eq!(acked, vec![CMD.to_string()]);
        heartbeat_once(&deps, &mut acked).await.unwrap();
        let sent = transport.heartbeats.lock().unwrap();
        assert_eq!(sent[1].acked_command_ids, vec![CMD.to_string()]);
        assert_eq!(sent[1].queue_depth, 1);
        assert!(acked.is_empty(), "acks are cleared once delivered");
    }

    #[tokio::test]
    async fn dropped_count_survives_failed_heartbeat() {
        let transport = Arc::new(FakeTransport::default());
        transport.push_heartbeat(Err(TransportError::Network("down".into())));
        let status = status_channel(AgentStatus { dropped_since_heartbeat: 3, ..AgentStatus::default() });
        let (deps, _commands, _tmp) = deps(transport.clone(), status.clone());
        let mut acked = vec![CMD.to_string()];
        assert!(heartbeat_once(&deps, &mut acked).await.is_err());
        assert_eq!(status.borrow().dropped_since_heartbeat, 3);
        assert_eq!(acked, vec![CMD.to_string()], "acks are kept for the retry");
        heartbeat_once(&deps, &mut acked).await.unwrap();
        assert_eq!(status.borrow().dropped_since_heartbeat, 0);
        assert_eq!(transport.heartbeats.lock().unwrap()[1].dropped_items, 3);
    }

    fn ids(n: usize) -> Vec<String> {
        (0..n).map(|i| format!("01JA2B3C4D5E6F7G8H9J{i:06}")).collect()
    }

    #[tokio::test]
    async fn at_most_100_acks_per_heartbeat_and_the_rest_follow() {
        let transport = Arc::new(FakeTransport::default());
        let (deps, _commands, _tmp) = deps(transport.clone(), status_channel(AgentStatus::default()));
        let mut acked = ids(150);
        heartbeat_once(&deps, &mut acked).await.unwrap();
        assert_eq!(acked.len(), 50);
        heartbeat_once(&deps, &mut acked).await.unwrap();
        assert!(acked.is_empty());
        let sent = transport.heartbeats.lock().unwrap();
        assert_eq!(sent[0].acked_command_ids, ids(150)[..100].to_vec());
        assert_eq!(sent[1].acked_command_ids, ids(150)[100..].to_vec());
        for request in sent.iter() {
            assert_matches("Heartbeat", &serde_json::to_value(request).unwrap());
        }
    }

    #[test]
    fn build_request_caps_acks_at_100() {
        let request = build_request(&AgentStatus::default(), &ControlState::initial(true), 0, 0, &NoProbe, ids(150), &OsInfo::default());
        assert_eq!(request.acked_command_ids.len(), 100);
        assert_matches("Heartbeat", &serde_json::to_value(&request).unwrap());
    }

    #[test]
    fn disk_percentage_is_always_finite_and_in_range() {
        for (input, expected) in
            [(f32::NAN, 100.0), (150.0, 100.0), (-5.0, 0.0), (f32::INFINITY, 100.0), (f32::NEG_INFINITY, 100.0)]
        {
            let status = AgentStatus { disk_used_pct: input, ..AgentStatus::default() };
            let request = build_request(&status, &ControlState::initial(true), 0, 0, &NoProbe, vec![], &OsInfo::default());
            assert_eq!(request.disk_used_pct, expected, "input {input}");
            let value = serde_json::to_value(&request).unwrap();
            assert!(value["disk_used_pct"].is_number());
            assert_matches("Heartbeat", &value);
        }
    }

    #[test]
    fn all_optionals_null_matches_contract_with_strict_types() {
        let request = build_request(&AgentStatus::default(), &ControlState::initial(false), 0, 0, &NoProbe, vec![], &OsInfo::default());
        let value = serde_json::to_value(&request).unwrap();
        assert!(value["soc_temp_c"].is_null() && value["wifi_rssi_dbm"].is_null() && value["last_error"].is_null());
        assert!(value["uptime_s"].is_u64() && value["queue_depth"].is_u64() && value["paused"].is_boolean());
        assert_matches("Heartbeat", &value);
    }

    #[test]
    fn non_finite_temperature_is_dropped() {
        struct NanProbe;
        impl SysProbe for NanProbe {
            fn soc_temp_c(&self) -> Option<f32> {
                Some(f32::NAN)
            }
            fn wifi_rssi_dbm(&self) -> Option<i32> {
                None
            }
        }
        let request = build_request(&AgentStatus::default(), &ControlState::initial(true), 0, 0, &NanProbe, vec![], &OsInfo::default());
        assert_eq!(request.soc_temp_c, None);
        assert_matches("Heartbeat", &serde_json::to_value(&request).unwrap());
    }

    #[test]
    fn all_invalid_commands_are_collected() {
        let t = translate(vec![remote("reboot", json!({})), remote("pause", json!({"x": 1}))]);
        assert_eq!(t.errors.len(), 2);
        assert!(t.commands.is_empty());
    }

    #[tokio::test]
    async fn every_invalid_command_error_reaches_last_error() {
        let transport = Arc::new(FakeTransport::default());
        transport.push_heartbeat(Ok(vec![remote("reboot", json!({})), remote("wipe", json!({}))]));
        let status = status_channel(AgentStatus::default());
        let transport_ref = transport.clone();
        let (deps, _commands, _tmp) = deps(transport, status.clone());
        let mut acked = Vec::new();
        heartbeat_once(&deps, &mut acked).await.unwrap();
        assert_eq!(status.borrow().last_error, None, "overlay, not shared status");
        heartbeat_once(&deps, &mut acked).await.unwrap();
        let sent = transport_ref.heartbeats.lock().unwrap();
        let error = sent[1].last_error.clone().unwrap();
        assert!(error.contains("reboot") && error.contains("wipe"), "{error}");
        assert_eq!(sent[1].acked_command_ids.len(), 2);
    }

    #[tokio::test]
    async fn unreadable_queue_is_reported_with_best_known_depth() {
        let transport = Arc::new(FakeTransport::default());
        let (deps, _commands, tmp) = deps(transport.clone(), status_channel(AgentStatus::default()));
        let mut acked = Vec::new();
        heartbeat_once(&deps, &mut acked).await.unwrap();
        std::fs::remove_dir_all(tmp.path().join("q").join("items")).unwrap();
        heartbeat_once(&deps, &mut acked).await.unwrap();
        let sent = transport.heartbeats.lock().unwrap();
        assert_eq!(sent[0].queue_depth, 1);
        assert!(sent[0].last_error.is_none());
        assert_eq!(sent[1].queue_depth, 1, "best-known depth, not a silent zero");
        assert!(sent[1].last_error.as_deref().unwrap().starts_with("queue unreadable:"));
        assert_matches("Heartbeat", &serde_json::to_value(&sent[1]).unwrap());
    }

    // Real time on a multi-thread runtime: if the fix regresses and the loop spins, the timeout
    // still fires on the other worker, so the test fails instead of hanging.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn dropped_shutdown_sender_stops_the_heartbeat_loop() {
        let transport = Arc::new(FakeTransport::default());
        let (deps, _commands, _tmp) = deps(transport, status_channel(AgentStatus::default()));
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(run_heartbeat(deps, stop_rx));
        tokio::time::sleep(Duration::from_millis(100)).await;
        drop(stop_tx);
        tokio::time::timeout(Duration::from_secs(2), task).await.expect("loop must exit").unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn shutdown_signal_stops_the_heartbeat_loop() {
        let transport = Arc::new(FakeTransport::default());
        let (deps, _commands, _tmp) = deps(transport, status_channel(AgentStatus::default()));
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(run_heartbeat(deps, stop_rx));
        tokio::time::sleep(Duration::from_millis(100)).await;
        stop_tx.send(true).unwrap();
        tokio::time::timeout(Duration::from_secs(2), task).await.expect("loop must exit").unwrap();
    }

    #[tokio::test]
    async fn command_error_overlays_existing_error_then_clears() {
        let transport = Arc::new(FakeTransport::default());
        transport.push_heartbeat(Ok(vec![remote("reboot", json!({}))]));
        let status = status_channel(AgentStatus { last_error: Some("camera: gone".into()), ..AgentStatus::default() });
        let (deps, _commands, _tmp) = deps(transport.clone(), status.clone());
        let mut acked = Vec::new();
        for _ in 0..3 {
            heartbeat_once(&deps, &mut acked).await.unwrap();
        }
        let sent = transport.heartbeats.lock().unwrap();
        assert_eq!(sent[0].last_error.as_deref(), Some("camera: gone"));
        let second = sent[1].last_error.clone().unwrap();
        assert!(second.contains("reboot") && second.starts_with("camera: gone; "), "{second}");
        assert_eq!(sent[2].last_error.as_deref(), Some("camera: gone"), "command error dropped after delivery");
        assert_eq!(status.borrow().last_error.as_deref(), Some("camera: gone"));
        for r in sent.iter() {
            assert_matches("Heartbeat", &serde_json::to_value(r).unwrap());
        }
    }

    #[tokio::test]
    async fn command_error_survives_a_failed_heartbeat() {
        let transport = Arc::new(FakeTransport::default());
        transport.push_heartbeat(Ok(vec![remote("reboot", json!({}))]));
        transport.push_heartbeat(Err(TransportError::Network("down".into())));
        let (deps, _commands, _tmp) = deps(transport.clone(), status_channel(AgentStatus::default()));
        let mut acked = Vec::new();
        heartbeat_once(&deps, &mut acked).await.unwrap();
        assert!(heartbeat_once(&deps, &mut acked).await.is_err());
        heartbeat_once(&deps, &mut acked).await.unwrap();
        let sent = transport.heartbeats.lock().unwrap();
        assert!(sent[1].last_error.is_some() && sent[2].last_error.is_some());
    }

    #[tokio::test]
    async fn closed_command_channel_leaves_commands_unacked_and_reports() {
        let transport = Arc::new(FakeTransport::default());
        transport.push_heartbeat(Ok(vec![remote("pause", json!({})), remote("reboot", json!({}))]));
        let (deps, commands, _tmp) = deps(transport.clone(), status_channel(AgentStatus::default()));
        drop(commands);
        let mut acked = Vec::new();
        heartbeat_once(&deps, &mut acked).await.unwrap();
        assert_eq!(acked.len(), 1, "only the invalid command is acked; the valid one is redelivered");
        heartbeat_once(&deps, &mut acked).await.unwrap();
        let sent = transport.heartbeats.lock().unwrap();
        assert!(sent[1].last_error.as_deref().unwrap().contains("control loop unavailable"));
        assert_matches("Heartbeat", &serde_json::to_value(&sent[1]).unwrap());
    }

    #[tokio::test]
    async fn unreadable_disk_reports_last_finite_value_and_overlay() {
        let transport = Arc::new(FakeTransport::default());
        let status = status_channel(AgentStatus { disk_used_pct: f32::NAN, ..AgentStatus::default() });
        let (deps, _commands, _tmp) = deps(transport.clone(), status.clone());
        let mut acked = Vec::new();
        heartbeat_once(&deps, &mut acked).await.unwrap();
        status.send_modify(|s| s.disk_used_pct = 42.0);
        heartbeat_once(&deps, &mut acked).await.unwrap();
        status.send_modify(|s| s.disk_used_pct = f32::NAN);
        heartbeat_once(&deps, &mut acked).await.unwrap();
        let sent = transport.heartbeats.lock().unwrap();
        assert_eq!(sent[0].disk_used_pct, 100.0);
        assert_eq!(sent[1].disk_used_pct, 42.0);
        assert!(sent[1].last_error.is_none());
        assert_eq!(sent[2].disk_used_pct, 42.0);
        assert_eq!(sent[2].last_error.as_deref(), Some("disk usage unreadable"));
        for r in sent.iter() {
            assert_matches("Heartbeat", &serde_json::to_value(r).unwrap());
        }
    }

    #[tokio::test]
    async fn status_error_survives_truncation_of_long_overlays() {
        let transport = Arc::new(FakeTransport::default());
        transport.push_heartbeat(Ok(vec![remote("reboot", json!({"why": "x".repeat(700)}))]));
        let status = status_channel(AgentStatus {
            disk_used_pct: f32::NAN,
            last_error: Some("camera: gone".into()),
            ..AgentStatus::default()
        });
        let (deps, _commands, _tmp) = deps(transport.clone(), status);
        let mut acked = Vec::new();
        heartbeat_once(&deps, &mut acked).await.unwrap();
        heartbeat_once(&deps, &mut acked).await.unwrap();
        let sent = transport.heartbeats.lock().unwrap();
        let error = sent[1].last_error.clone().unwrap();
        assert_eq!(error.chars().count(), 500, "the long command error was truncated");
        assert!(error.starts_with("camera: gone; disk usage unreadable; command "), "{error}");
        assert_matches("Heartbeat", &serde_json::to_value(&sent[1]).unwrap());
    }

    #[tokio::test]
    async fn failed_led_is_reported_even_when_another_error_took_the_slot() {
        let transport = Arc::new(FakeTransport::default());
        let status = status_channel(AgentStatus {
            led: LedHealth::Failed,
            last_error: Some("network error: down".into()),
            ..AgentStatus::default()
        });
        let (deps, _commands, _tmp) = deps(transport.clone(), status.clone());
        heartbeat_once(&deps, &mut Vec::new()).await.unwrap();
        status.send_modify(|s| s.last_error = Some(LED_UNAVAILABLE_ERROR.into()));
        heartbeat_once(&deps, &mut Vec::new()).await.unwrap();
        status.send_modify(|s| s.led = LedHealth::Working);
        status.send_modify(|s| s.last_error = None);
        heartbeat_once(&deps, &mut Vec::new()).await.unwrap();
        let sent = transport.heartbeats.lock().unwrap();
        assert_eq!(sent[0].last_error.as_deref(), Some("privacy LED unavailable: recording disabled; network error: down"));
        assert_eq!(sent[1].last_error.as_deref(), Some(LED_UNAVAILABLE_ERROR), "not repeated");
        assert_eq!(sent[2].last_error, None);
    }

    #[tokio::test]
    async fn merged_last_error_stays_within_500_chars() {
        let transport = Arc::new(FakeTransport::default());
        let status = status_channel(AgentStatus {
            disk_used_pct: f32::NAN,
            last_error: Some("é".repeat(600)),
            ..AgentStatus::default()
        });
        let (deps, _commands, _tmp) = deps(transport.clone(), status);
        heartbeat_once(&deps, &mut Vec::new()).await.unwrap();
        let sent = transport.heartbeats.lock().unwrap();
        assert_eq!(sent[0].last_error.as_ref().unwrap().chars().count(), 500);
        assert_matches("Heartbeat", &serde_json::to_value(&sent[0]).unwrap());
    }
}
