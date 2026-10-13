//! User-controllable state (pause, audio on/off), remote command parsing and recording gates.
use std::io;
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::fsutil::write_atomic;
use crate::status::AgentStatus;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ControlState {
    pub paused: bool,
    pub audio_enabled: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Pause,
    Resume,
    TogglePause,
    SetAudio(bool),
}

impl ControlState {
    pub fn initial(audio_enabled: bool) -> Self {
        Self { paused: false, audio_enabled }
    }

    #[must_use]
    pub fn apply(self, command: &Command) -> Self {
        match command {
            Command::Pause => Self { paused: true, ..self },
            Command::Resume => Self { paused: false, ..self },
            Command::TogglePause => Self { paused: !self.paused, ..self },
            Command::SetAudio(on) => Self { audio_enabled: *on, ..self },
        }
    }
}

fn is_empty_payload(payload: &Value) -> bool {
    payload.is_null() || payload.as_object().is_some_and(|o| o.is_empty())
}

/// Translate a brain command (protocol v1 `Command`). Only `set_config {audio_enabled}` is allowed.
pub fn parse_remote_command(kind: &str, payload: &Value) -> Result<Command, String> {
    match kind {
        "pause" if is_empty_payload(payload) => Ok(Command::Pause),
        "resume" if is_empty_payload(payload) => Ok(Command::Resume),
        "set_config" => {
            let object = payload.as_object().ok_or_else(|| "set_config payload must be an object".to_string())?;
            match (object.len(), object.get("audio_enabled")) {
                (1, Some(Value::Bool(on))) => Ok(Command::SetAudio(*on)),
                _ => Err(format!("set_config supports only {{\"audio_enabled\": bool}}, got {payload}")),
            }
        }
        other => Err(format!("unsupported command {other:?} with payload {payload}")),
    }
}

/// Persisted state. A missing file silently yields defaults; any other read error or a corrupt
/// file is logged with the path and also yields defaults.
pub fn load_state(path: &Path, default_audio: bool) -> ControlState {
    let default = ControlState::initial(default_audio);
    match std::fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes).unwrap_or_else(|e| {
            tracing::warn!(error = %e, path = %path.display(), "corrupt control state, using defaults");
            default
        }),
        Err(e) if e.kind() == io::ErrorKind::NotFound => default,
        Err(e) => {
            tracing::warn!(error = %e, path = %path.display(), "cannot read control state, using defaults");
            default
        }
    }
}

pub fn save_state(path: &Path, state: &ControlState) -> io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let json = serde_json::to_vec(state).map_err(io::Error::other)?;
    write_atomic(path, &json, 0o600)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Gates {
    pub video: bool,
    pub audio: bool,
}

pub fn recording_gates(state: &ControlState, status: &AgentStatus) -> Gates {
    let video = status.paired && status.clock_synced && !state.paused && !status.quiet && !status.disk_stopped;
    Gates { video, audio: video && state.audio_enabled }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn apply_returns_new_state() {
        let s = ControlState::initial(true);
        assert_eq!(s.apply(&Command::Pause), ControlState { paused: true, audio_enabled: true });
        assert_eq!(s.apply(&Command::TogglePause).apply(&Command::TogglePause), s);
        assert_eq!(s.apply(&Command::SetAudio(false)).audio_enabled, false);
        assert_eq!(s.apply(&Command::Pause).apply(&Command::Resume), s);
        assert!(!s.paused, "original value is untouched");
    }

    #[test]
    fn remote_commands_are_parsed_strictly() {
        assert_eq!(parse_remote_command("pause", &json!({})), Ok(Command::Pause));
        assert_eq!(parse_remote_command("resume", &serde_json::Value::Null), Ok(Command::Resume));
        assert_eq!(
            parse_remote_command("set_config", &json!({"audio_enabled": false})),
            Ok(Command::SetAudio(false))
        );
        for (kind, payload) in [
            ("reboot", json!({})),
            ("pause", json!({"x": 1})),
            ("set_config", json!({})),
            ("set_config", json!({"audio_enabled": "no"})),
            ("set_config", json!({"audio_enabled": true, "video_fps": 5})),
            ("set_config", json!([1])),
        ] {
            assert!(parse_remote_command(kind, &payload).is_err(), "{kind} {payload}");
        }
    }

    #[test]
    fn state_round_trips_and_defaults_on_corruption() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.json");
        assert_eq!(load_state(&path, false), ControlState::initial(false));
        let s = ControlState { paused: true, audio_enabled: false };
        save_state(&path, &s).unwrap();
        assert_eq!(load_state(&path, true), s);
        std::fs::write(&path, b"garbage").unwrap();
        assert_eq!(load_state(&path, true), ControlState::initial(true));
    }

    #[test]
    fn unreadable_state_path_defaults() {
        // A directory at the state path yields a non-NotFound read error.
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(load_state(dir.path(), true), ControlState::initial(true));
    }

    #[test]
    fn gates_need_every_condition() {
        let ready = AgentStatus { paired: true, clock_synced: true, ..AgentStatus::default() };
        let on = ControlState::initial(true);
        assert_eq!(recording_gates(&on, &ready), Gates { video: true, audio: true });
        assert_eq!(recording_gates(&on.apply(&Command::SetAudio(false)), &ready), Gates { video: true, audio: false });
        let blocked = [
            AgentStatus { paired: false, ..ready.clone() },
            AgentStatus { clock_synced: false, ..ready.clone() },
            AgentStatus { quiet: true, ..ready.clone() },
            AgentStatus { disk_stopped: true, ..ready.clone() },
        ];
        for status in blocked {
            assert_eq!(recording_gates(&on, &status), Gates { video: false, audio: false }, "{status:?}");
        }
        assert_eq!(recording_gates(&on.apply(&Command::Pause), &ready), Gates { video: false, audio: false });
    }

    #[test]
    fn gates_match_truth_table_for_every_combination() {
        for bits in 0u32..64 {
            let flag = |n: u32| bits & (1 << n) != 0;
            let (paired, synced, paused, quiet, disk_stopped, audio_enabled) =
                (flag(0), flag(1), flag(2), flag(3), flag(4), flag(5));
            let status = AgentStatus {
                paired,
                clock_synced: synced,
                quiet,
                disk_stopped,
                ..AgentStatus::default()
            };
            let state = ControlState { paused, audio_enabled };
            let expected_video = paired && synced && !paused && !quiet && !disk_stopped;
            let expected_audio = expected_video && audio_enabled;
            assert_eq!(
                recording_gates(&state, &status),
                Gates { video: expected_video, audio: expected_audio },
                "bits {bits:06b}"
            );
        }
    }
}
