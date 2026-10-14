//! Protocol v1 wire types (docs/protocol/v1.openapi.yaml).
use serde::{Deserialize, Serialize};

pub const AGENT_VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HwInfo {
    pub model: String,
    pub serial: String,
    pub agent_version: String,
}

#[derive(Clone, Serialize)]
pub struct PairRequest {
    pub token: String,
    pub csr_pem: String,
    pub hw_info: HwInfo,
}

impl std::fmt::Debug for PairRequest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PairRequest").field("token", &"<redacted>").field("hw_info", &self.hw_info).finish_non_exhaustive()
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct PairResponse {
    pub device_id: String,
    pub cert_pem: String,
    pub ca_cert_pem: String,
    pub ca_fingerprint: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct HeartbeatRequest {
    pub agent_version: String,
    pub uptime_s: u64,
    pub soc_temp_c: Option<f32>,
    pub disk_used_pct: f32,
    pub queue_depth: u64,
    pub wifi_rssi_dbm: Option<i32>,
    pub clock_synced: bool,
    pub paused: bool,
    pub audio_enabled: bool,
    pub recording_video: bool,
    pub recording_audio: bool,
    pub mic_missing: bool,
    pub camera_failures: u32,
    pub last_error: Option<String>,
    pub dropped_items: u32,
    pub acked_command_ids: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct RemoteCommand {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub payload: serde_json::Value,
}

#[derive(Debug, Clone, Deserialize)]
pub struct HeartbeatResponse {
    pub commands: Vec<RemoteCommand>,
}

#[cfg(test)]
pub(crate) mod contract {
    /// Validate `instance` against `#/components/schemas/<schema_name>` of the shared contract.
    pub fn assert_matches(schema_name: &str, instance: &serde_json::Value) {
        let spec_path = concat!(env!("CARGO_MANIFEST_DIR"), "/../docs/protocol/v1.openapi.yaml");
        let text = std::fs::read_to_string(spec_path).expect("contract file from P1a");
        let spec: serde_json::Value = serde_yaml::from_str(&text).unwrap();
        let schema = serde_json::json!({
            "$ref": format!("#/components/schemas/{schema_name}"),
            "components": spec["components"],
        });
        let validator = jsonschema::validator_for(&schema).unwrap();
        let errors: Vec<String> = validator.iter_errors(instance).map(|e| e.to_string()).collect();
        assert!(errors.is_empty(), "{schema_name} contract violation: {errors:?}\n{instance}");
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::contract::assert_matches;
    use super::*;
    use serde_json::json;

    pub(crate) fn sample_heartbeat() -> HeartbeatRequest {
        HeartbeatRequest {
            agent_version: AGENT_VERSION.into(),
            uptime_s: 60,
            soc_temp_c: None,
            disk_used_pct: 12.5,
            queue_depth: 2,
            wifi_rssi_dbm: Some(-61),
            clock_synced: true,
            paused: false,
            audio_enabled: true,
            recording_video: true,
            recording_audio: false,
            mic_missing: true,
            camera_failures: 0,
            last_error: None,
            dropped_items: 1,
            acked_command_ids: vec!["01JA2B3C4D5E6F7G8H9J0KMNPQ".into()],
        }
    }

    #[test]
    fn pair_request_matches_contract() {
        let request = PairRequest {
            token: "ABCD-EFGH".into(),
            csr_pem: "-----BEGIN CERTIFICATE REQUEST-----".into(),
            hw_info: HwInfo { model: "Raspberry Pi Zero W".into(), serial: "1".into(), agent_version: AGENT_VERSION.into() },
        };
        assert_matches("PairRequest", &serde_json::to_value(request).unwrap());
    }

    #[test]
    fn pair_request_debug_redacts_token() {
        let request = PairRequest {
            token: "SECRET-TOKEN".into(),
            csr_pem: "csr".into(),
            hw_info: HwInfo { model: "m".into(), serial: "1".into(), agent_version: AGENT_VERSION.into() },
        };
        assert!(!format!("{request:?}").contains("SECRET-TOKEN"));
    }

    #[test]
    fn heartbeat_request_matches_contract() {
        assert_matches("Heartbeat", &serde_json::to_value(sample_heartbeat()).unwrap());
    }

    #[test]
    fn heartbeat_response_from_contract_parses() {
        let value = json!({"commands": [{"id": "01JA2B3C4D5E6F7G8H9J0KMNPQ", "type": "set_config", "payload": {"audio_enabled": false}}]});
        assert_matches("HeartbeatResponse", &value);
        let parsed: HeartbeatResponse = serde_json::from_value(value).unwrap();
        assert_eq!(parsed.commands[0].kind, "set_config");
        assert_eq!(parsed.commands[0].payload, json!({"audio_enabled": false}));
    }

    #[test]
    fn heartbeat_response_with_each_command_type_matches_contract() {
        let value = json!({"commands": [
            {"id": "01JA2B3C4D5E6F7G8H9J0KMNPQ", "type": "pause", "payload": {}},
            {"id": "01JA2B3C4D5E6F7G8H9J0KMNPR", "type": "resume", "payload": {}},
            {"id": "01JA2B3C4D5E6F7G8H9J0KMNPS", "type": "set_config", "payload": {"audio_enabled": true}},
        ]});
        assert_matches("HeartbeatResponse", &value);
        let parsed: HeartbeatResponse = serde_json::from_value(value).unwrap();
        let kinds: Vec<&str> = parsed.commands.iter().map(|c| c.kind.as_str()).collect();
        assert_eq!(kinds, ["pause", "resume", "set_config"]);
    }

    #[test]
    fn set_config_command_matches_contract() {
        let value = json!({"id": "01JA2B3C4D5E6F7G8H9J0KMNPQ", "type": "set_config", "payload": {"audio_enabled": false}});
        assert_matches("Command", &value);
    }

    #[test]
    #[should_panic(expected = "contract violation")]
    fn contract_rejects_unknown_command_type() {
        assert_matches("Command", &json!({"id": "01JA2B3C4D5E6F7G8H9J0KMNPQ", "type": "reboot", "payload": {}}));
    }

    #[test]
    #[should_panic(expected = "contract violation")]
    fn contract_rejects_pause_with_payload() {
        assert_matches("Command", &json!({"id": "01JA2B3C4D5E6F7G8H9J0KMNPQ", "type": "pause", "payload": {"x": 1}}));
    }

    #[test]
    #[should_panic(expected = "contract violation")]
    fn contract_rejects_extra_heartbeat_field() {
        let mut value = serde_json::to_value(sample_heartbeat()).unwrap();
        value["bogus"] = json!(1);
        assert_matches("Heartbeat", &value);
    }

    #[test]
    fn pair_response_matches_contract() {
        let value = json!({"device_id": "01JA2B3C4D5E6F7G8H9J0KMNPQ", "cert_pem": "c", "ca_cert_pem": "ca", "ca_fingerprint": "a".repeat(64)});
        assert_matches("PairResponse", &value);
        let parsed: PairResponse = serde_json::from_value(value).unwrap();
        assert_eq!(parsed.device_id, "01JA2B3C4D5E6F7G8H9J0KMNPQ");
    }
}
