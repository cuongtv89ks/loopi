//! OTA wire and state types: the brain's `update` command and the `OtaReport` the heartbeat carries (protocol v1).
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Contract `OtaReport.error.maxLength`, counted in Unicode scalar values.
pub const MAX_OTA_ERROR: usize = 500;
/// Contract `UpdateCommand.payload.size.maximum` (256 MiB).
pub const MAX_BUNDLE_BYTES: u64 = 256 * 1024 * 1024;
const UPDATE_KEYS: [&str; 5] = ["bundle", "version", "variant", "sha256", "size"];

/// RAUC bootnames: A = /dev/mmcblk0p2 + /boot/a/, B = /dev/mmcblk0p3 + /boot/b/.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Slot {
    A,
    B,
}

impl Slot {
    pub fn other(self) -> Self {
        match self {
            Slot::A => Slot::B,
            Slot::B => Slot::A,
        }
    }

    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "A" => Some(Slot::A),
            "B" => Some(Slot::B),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Variant {
    Dev,
    Prod,
}

impl Variant {
    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "dev" => Some(Variant::Dev),
            "prod" => Some(Variant::Prod),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Variant::Dev => "dev",
            Variant::Prod => "prod",
        }
    }
}

/// What the brain asked this device to install. `command_id` is `None` only for the hand-made first move to A/B slots.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UpdateRequest {
    pub command_id: Option<String>,
    pub bundle: String,
    pub version: String,
    pub variant: Variant,
    pub sha256: String,
    pub size: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OtaState {
    Downloading,
    Installing,
    Rebooting,
    Trying,
    Succeeded,
    RolledBack,
    Failed,
}

impl OtaState {
    pub fn is_terminal(self) -> bool {
        matches!(self, OtaState::Succeeded | OtaState::RolledBack | OtaState::Failed)
    }
}

/// Contract `OtaReport`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct OtaReport {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub command_id: Option<String>,
    pub state: OtaState,
    pub version: String,
    pub error: Option<String>,
}

impl OtaReport {
    pub fn new(command_id: Option<String>, version: &str, state: OtaState, error: Option<String>) -> Self {
        Self { command_id, state, version: version.to_string(), error: error.map(|e| e.chars().take(MAX_OTA_ERROR).collect()) }
    }
}

/// Contract `OsVersion`: `^[0-9A-Za-z.+-]{1,64}$`.
pub fn is_valid_version(version: &str) -> bool {
    !version.is_empty()
        && version.len() <= 64
        && version.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '+' | '-'))
}

/// Contract `BundleName`: `loopi-(dev|prod)-<version>.raucb`.
pub fn is_valid_bundle_name(name: &str) -> bool {
    let Some(rest) = name.strip_suffix(".raucb") else { return false };
    let version = rest.strip_prefix("loopi-dev-").or_else(|| rest.strip_prefix("loopi-prod-"));
    version.is_some_and(is_valid_version)
}

fn is_sha256(text: &str) -> bool {
    text.len() == 64 && text.chars().all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
}

/// Translate the brain's `update` command (contract `UpdateCommand`); refuses exactly what the brain refuses.
pub fn parse_update(id: &str, payload: &Value) -> Result<UpdateRequest, String> {
    let object = payload.as_object().ok_or_else(|| format!("update payload must be an object, got {payload}"))?;
    if object.len() != UPDATE_KEYS.len() || !UPDATE_KEYS.iter().all(|key| object.contains_key(*key)) {
        return Err(format!("update needs exactly {}, got {payload}", UPDATE_KEYS.join(", ")));
    }
    let text = |key: &str| object[key].as_str().ok_or_else(|| format!("update {key} must be a string"));
    let (bundle, version, sha256) = (text("bundle")?, text("version")?, text("sha256")?);
    let variant = Variant::parse(text("variant")?).ok_or("update variant must be dev or prod")?;
    let size = object["size"]
        .as_u64()
        .filter(|s| (1..=MAX_BUNDLE_BYTES).contains(s))
        .ok_or_else(|| format!("update size must be an integer from 1 to {MAX_BUNDLE_BYTES}"))?;
    if !is_valid_version(version) || !is_valid_bundle_name(bundle) || !is_sha256(sha256) {
        return Err(format!("update payload is malformed: {payload}"));
    }
    if bundle != format!("loopi-{}-{version}.raucb", variant.as_str()) {
        return Err(format!("update bundle {bundle} does not match variant {} and version {version}", variant.as_str()));
    }
    Ok(UpdateRequest {
        command_id: Some(id.to_string()),
        bundle: bundle.to_string(),
        version: version.to_string(),
        variant,
        sha256: sha256.to_string(),
        size,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::wire::contract::assert_matches;
    use serde_json::json;

    const ID: &str = "01JA2B3C4D5E6F7G8H9J0KMNPQ";

    fn payload() -> serde_json::Value {
        json!({"bundle": "loopi-prod-2026.10.21-a1b2c3d.raucb", "version": "2026.10.21-a1b2c3d",
               "variant": "prod", "sha256": "ab".repeat(32), "size": 75_000_000})
    }

    #[test]
    fn parses_an_update_the_contract_accepts() {
        assert_matches("Command", &json!({"id": ID, "type": "update", "payload": payload()}));
        let request = parse_update(ID, &payload()).unwrap();
        assert_eq!(request.command_id.as_deref(), Some(ID));
        assert_eq!(request.bundle, "loopi-prod-2026.10.21-a1b2c3d.raucb");
        assert_eq!((request.variant, request.size), (Variant::Prod, 75_000_000));
    }

    #[test]
    fn refuses_what_the_contract_refuses_and_names_that_disagree() {
        let mut cases = vec![
            json!({"bundle": "../x"}), json!({"version": "has space"}), json!({"variant": "beta"}),
            json!({"sha256": "AB".repeat(32)}), json!({"size": 0}), json!({"size": 268_435_457u64}),
            json!({"size": true}), json!({"size": "75"}), json!({"extra": 1}), json!({"variant": "dev"}),
        ];
        for override_ in cases.drain(..) {
            let mut p = payload();
            for (k, v) in override_.as_object().unwrap() {
                p[k] = v.clone();
            }
            assert!(parse_update(ID, &p).is_err(), "{p}");
        }
        let mut missing = payload();
        missing.as_object_mut().unwrap().remove("sha256");
        assert!(parse_update(ID, &missing).is_err());
        assert!(parse_update(ID, &json!(null)).is_err());
    }

    #[test]
    fn reports_serialize_as_the_contract_says() {
        let report = OtaReport::new(Some(ID.into()), "2026.10.21-a1b2c3d", OtaState::RolledBack, None);
        assert_eq!(
            serde_json::to_value(&report).unwrap(),
            json!({"command_id": ID, "state": "rolled_back", "version": "2026.10.21-a1b2c3d", "error": null})
        );
        assert_matches("OtaReport", &serde_json::to_value(&report).unwrap());
        let hand = OtaReport::new(None, "v1", OtaState::Succeeded, Some("é".repeat(600)));
        let value = serde_json::to_value(&hand).unwrap();
        assert!(value.get("command_id").is_none());
        assert_eq!(value["error"].as_str().unwrap().chars().count(), MAX_OTA_ERROR);
        assert_matches("OtaReport", &value);
    }

    #[test]
    fn slots_and_names() {
        assert_eq!((Slot::A.other(), Slot::B.other()), (Slot::B, Slot::A));
        assert_eq!(Slot::parse("B"), Some(Slot::B));
        assert_eq!(Slot::parse("b"), None);
        assert!(is_valid_version("2026.10.21-a1b2c3d-dirty"));
        assert!(!is_valid_version("") && !is_valid_version("a b") && !is_valid_version(&"x".repeat(65)));
        assert!(is_valid_bundle_name("loopi-dev-2026.10.21-a1b2c3d.raucb"));
        assert!(!is_valid_bundle_name("loopi-dev-.raucb") && !is_valid_bundle_name("loopi-x-1.raucb"));
        assert!(OtaState::Failed.is_terminal() && !OtaState::Trying.is_terminal());
    }
}
