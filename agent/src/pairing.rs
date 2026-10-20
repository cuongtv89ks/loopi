//! Device identity: P-256 key + CSR, pairing with the brain (trust on first use), persistence.
use std::fs;
use std::io;
use std::path::Path;

use rcgen::{CertificateParams, DistinguishedName, DnType, KeyPair, PKCS_ECDSA_P256_SHA256};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::CertificateDer;
use sha2::{Digest, Sha256};

use crate::config::{normalize_fingerprint, Config};
use crate::fsutil::write_atomic;
use crate::tls::{mtls_config, TlsError};
use crate::transport::{pair, TransportError};
use crate::wire::{HwInfo, PairRequest};

pub const DEVICE_ID_FILE: &str = "device_id";
pub const KEY_FILE: &str = "device.key";
pub const CERT_FILE: &str = "device.crt";
pub const CA_FILE: &str = "ca.crt";

#[derive(Clone, PartialEq, Eq)]
pub struct Identity {
    pub device_id: String,
    pub key_pem: String,
    pub cert_pem: String,
    pub ca_cert_pem: String,
}

impl std::fmt::Debug for Identity {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Identity").field("device_id", &self.device_id).field("key_pem", &"<redacted>").finish_non_exhaustive()
    }
}

#[derive(Debug, thiserror::Error)]
pub enum PairError {
    #[error("brain returned an unusable identity: {0}")]
    BadIdentity(#[from] TlsError),
    #[error("device is not paired and brain.pair_token is not configured")]
    NoToken,
    #[error("CA fingerprint mismatch: expected {expected}, got {actual}")]
    CaMismatch { expected: String, actual: String },
    #[error("brain sent an unreadable CA certificate")]
    BadCa,
    #[error("pairing request failed: {0}")]
    Transport(#[from] TransportError),
    #[error("key generation failed: {0}")]
    Key(#[from] rcgen::Error),
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
}

/// `device_id` is written last, so its presence means the identity is complete.
pub fn load_identity(dir: &Path) -> io::Result<Option<Identity>> {
    if !dir.join(DEVICE_ID_FILE).exists() {
        return Ok(None);
    }
    let read = |name: &str| fs::read_to_string(dir.join(name));
    Ok(Some(Identity {
        device_id: read(DEVICE_ID_FILE)?.trim().to_string(),
        key_pem: read(KEY_FILE)?,
        cert_pem: read(CERT_FILE)?,
        ca_cert_pem: read(CA_FILE)?,
    }))
}

fn save_identity(dir: &Path, identity: &Identity) -> io::Result<()> {
    fs::create_dir_all(dir)?;
    let files = [
        (KEY_FILE, &identity.key_pem),
        (CERT_FILE, &identity.cert_pem),
        (CA_FILE, &identity.ca_cert_pem),
        (DEVICE_ID_FILE, &identity.device_id),
    ];
    for (name, contents) in files {
        write_atomic(&dir.join(name), contents.as_bytes(), 0o600)?;
    }
    Ok(())
}

pub fn generate_key_and_csr() -> Result<(String, String), rcgen::Error> {
    let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256)?;
    let mut params = CertificateParams::new(Vec::<String>::new())?;
    let mut name = DistinguishedName::new();
    name.push(DnType::CommonName, "loopi-device");
    params.distinguished_name = name;
    let csr = params.serialize_request(&key)?;
    Ok((key.serialize_pem(), csr.pem()?))
}

/// Lowercase hex SHA-256 of the first certificate's DER in `pem`.
pub fn sha256_of_first_cert(pem: &str) -> Option<String> {
    let der = CertificateDer::pem_slice_iter(pem.as_bytes()).next()?.ok()?;
    Some(hex::encode(Sha256::digest(der.as_ref())))
}

/// The CA must match the fingerprint the brain claims and, if configured, the one in `loopi.toml`.
pub fn verify_ca(ca_pem: &str, response_fp: &str, configured: Option<&str>) -> Result<(), PairError> {
    let actual = sha256_of_first_cert(ca_pem).ok_or(PairError::BadCa)?;
    if normalize_fingerprint(response_fp).as_deref() != Some(actual.as_str()) {
        return Err(PairError::CaMismatch { expected: response_fp.into(), actual });
    }
    if let Some(expected) = configured {
        let expected = normalize_fingerprint(expected).ok_or(PairError::BadCa)?;
        if expected != actual {
            return Err(PairError::CaMismatch { expected, actual });
        }
    }
    Ok(())
}

pub async fn ensure_paired(
    config: &Config,
    keys_dir: &Path,
    hw: HwInfo,
    client: &reqwest::Client,
) -> Result<Identity, PairError> {
    ensure_paired_with(config, keys_dir, hw, client, generate_key_and_csr).await
}

async fn ensure_paired_with(
    config: &Config,
    keys_dir: &Path,
    hw: HwInfo,
    client: &reqwest::Client,
    keygen: impl FnOnce() -> Result<(String, String), rcgen::Error>,
) -> Result<Identity, PairError> {
    if let Some(identity) = load_identity(keys_dir)? {
        return Ok(identity);
    }
    let token = config.brain.pair_token.clone().ok_or(PairError::NoToken)?;
    let (key_pem, csr_pem) = keygen()?;
    let response = pair(client, &config.brain.url, &PairRequest { token, csr_pem, hw_info: hw }).await?;
    verify_ca(&response.ca_cert_pem, &response.ca_fingerprint, config.brain.ca_fingerprint.as_deref())?;
    // The cert must match our key and chain to the CA; otherwise saving it would wedge the device.
    mtls_config(&response.ca_cert_pem, &response.cert_pem, &key_pem)?;
    let identity = Identity {
        device_id: response.device_id,
        key_pem,
        cert_pem: response.cert_pem,
        ca_cert_pem: response.ca_cert_pem,
    };
    save_identity(keys_dir, &identity)?;
    Ok(identity)
}

/// Delay before the next pairing attempt after a network or server hiccup.
pub const RETRY_FAST: std::time::Duration = std::time::Duration::from_secs(30);
/// Delay when the failure needs a human fix (bad token, CA mismatch, rejected request).
pub const RETRY_NEEDS_FIX: std::time::Duration = std::time::Duration::from_secs(120);

/// 401 (token used/expired/unknown), 422 and 426 will not fix themselves; network and 5xx might.
pub fn retry_delay(error: &PairError) -> std::time::Duration {
    match error {
        PairError::NoToken | PairError::CaMismatch { .. } | PairError::BadCa => RETRY_NEEDS_FIX,
        PairError::Transport(TransportError::Status(401 | 422 | 426)) => RETRY_NEEDS_FIX,
        _ => RETRY_FAST,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{BrainConfig, CaptureConfig, ClockConfig, GpioConfig, LedConfig, PathsConfig, ScheduleConfig};
    use crate::tls::tests::test_pki;
    use serde_json::json;
    use std::os::unix::fs::PermissionsExt;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    const DEVICE: &str = "01JA2B3C4D5E6F7G8H9J0KMNPQ";

    fn config(url: &str, token: Option<&str>, ca_fp: Option<&str>) -> Config {
        Config {
            wifi: None,
            brain: BrainConfig { url: url.into(), pair_token: token.map(Into::into), ca_fingerprint: ca_fp.map(Into::into) },
            capture: CaptureConfig::default(),
            schedule: ScheduleConfig::default(),
            led: LedConfig::default(),
            gpio: GpioConfig::default(),
            clock: ClockConfig::default(),
            paths: PathsConfig::default(),
        }
    }

    fn hw() -> HwInfo {
        HwInfo { model: "test".into(), serial: "1".into(), agent_version: "0.1.0".into() }
    }

    /// Keygen returning the test PKI's client key, so the mock brain's cert matches the key.
    fn keygen_for(key_pem: String) -> impl FnOnce() -> Result<(String, String), rcgen::Error> {
        move || {
            let key = KeyPair::from_pem(&key_pem)?;
            let csr = CertificateParams::new(Vec::<String>::new())?.serialize_request(&key)?;
            Ok((key_pem, csr.pem()?))
        }
    }

    async fn brain(ca_pem: &str, cert_pem: &str, ca_fp: &str, expected_calls: u64) -> MockServer {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/v1/pair"))
            .respond_with(ResponseTemplate::new(201).set_body_json(json!({
                "device_id": DEVICE, "cert_pem": cert_pem, "ca_cert_pem": ca_pem, "ca_fingerprint": ca_fp
            })))
            .expect(expected_calls)
            .mount(&server)
            .await;
        server
    }

    #[test]
    fn generated_key_and_csr_are_pem() {
        let (key, csr) = generate_key_and_csr().unwrap();
        assert!(key.contains("BEGIN PRIVATE KEY"));
        assert!(csr.contains("BEGIN CERTIFICATE REQUEST"));
    }

    #[test]
    fn identity_without_device_id_marker_is_unpaired() {
        let keys = tempfile::tempdir().unwrap();
        for name in [KEY_FILE, CERT_FILE, CA_FILE] {
            std::fs::write(keys.path().join(name), "x").unwrap();
        }
        assert_eq!(load_identity(keys.path()).unwrap(), None);
    }

    #[tokio::test]
    async fn pairing_persists_identity_and_is_not_repeated() {
        let (ca, cert, key) = test_pki();
        let fp = sha256_of_first_cert(&ca).unwrap();
        let server = brain(&ca, &cert, &fp, 1).await;
        let keys = tempfile::tempdir().unwrap();
        let cfg = config(&server.uri(), Some("ABCD-EFGH"), Some(&fp.to_uppercase()));

        let identity =
            ensure_paired_with(&cfg, keys.path(), hw(), &reqwest::Client::new(), keygen_for(key)).await.unwrap();
        assert!(!format!("{identity:?}").contains("PRIVATE KEY"));
        assert_eq!(identity.device_id, DEVICE);
        assert_eq!(load_identity(keys.path()).unwrap(), Some(identity.clone()));
        let mode = std::fs::metadata(keys.path().join(KEY_FILE)).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);

        // Already paired: no second request (the mock expects exactly one call).
        let again = ensure_paired(&cfg, keys.path(), hw(), &reqwest::Client::new()).await.unwrap();
        assert_eq!(again, identity);
    }

    #[tokio::test]
    async fn configured_fingerprint_mismatch_aborts_without_saving() {
        let (ca, cert, key) = test_pki();
        let fp = sha256_of_first_cert(&ca).unwrap();
        let server = brain(&ca, &cert, &fp, 1).await;
        let keys = tempfile::tempdir().unwrap();
        let wrong = "cd".repeat(32);
        let cfg = config(&server.uri(), Some("ABCD-EFGH"), Some(&wrong));
        let err = ensure_paired_with(&cfg, keys.path(), hw(), &reqwest::Client::new(), keygen_for(key))
            .await
            .unwrap_err();
        assert!(matches!(err, PairError::CaMismatch { .. }), "{err}");
        assert_eq!(load_identity(keys.path()).unwrap(), None);
    }

    #[tokio::test]
    async fn response_fingerprint_must_match_its_ca() {
        let (ca, cert, key) = test_pki();
        let server = brain(&ca, &cert, &"ef".repeat(32), 1).await;
        let keys = tempfile::tempdir().unwrap();
        let cfg = config(&server.uri(), Some("ABCD-EFGH"), None);
        let err = ensure_paired_with(&cfg, keys.path(), hw(), &reqwest::Client::new(), keygen_for(key))
            .await
            .unwrap_err();
        assert!(matches!(err, PairError::CaMismatch { .. }));
    }

    #[tokio::test]
    async fn cert_not_matching_generated_key_is_not_saved() {
        let (ca, cert, _) = test_pki();
        let fp = sha256_of_first_cert(&ca).unwrap();
        let server = brain(&ca, &cert, &fp, 1).await;
        let keys = tempfile::tempdir().unwrap();
        let cfg = config(&server.uri(), Some("ABCD-EFGH"), Some(&fp));
        // Real keygen: a fresh key that the mock's cert was not issued for.
        let err = ensure_paired(&cfg, keys.path(), hw(), &reqwest::Client::new()).await.unwrap_err();
        assert!(matches!(err, PairError::BadIdentity(_)), "{err}");
        assert_eq!(load_identity(keys.path()).unwrap(), None);
        assert!(!keys.path().join(KEY_FILE).exists());
    }

    #[tokio::test]
    async fn missing_token_and_rejected_token() {
        let keys = tempfile::tempdir().unwrap();
        let err = ensure_paired(&config("http://127.0.0.1:9", None, None), keys.path(), hw(), &reqwest::Client::new())
            .await
            .unwrap_err();
        assert!(matches!(err, PairError::NoToken));

        let server = MockServer::start().await;
        Mock::given(method("POST")).and(path("/v1/pair")).respond_with(ResponseTemplate::new(401)).mount(&server).await;
        let err = ensure_paired(&config(&server.uri(), Some("USED-TOKN"), None), keys.path(), hw(), &reqwest::Client::new())
            .await
            .unwrap_err();
        assert!(matches!(err, PairError::Transport(TransportError::Status(401))));
    }

    #[test]
    fn retry_delay_classifies_errors() {
        let status = |c| PairError::Transport(TransportError::Status(c));
        assert_eq!(retry_delay(&PairError::NoToken), RETRY_NEEDS_FIX);
        assert_eq!(retry_delay(&PairError::BadCa), RETRY_NEEDS_FIX);
        assert_eq!(retry_delay(&PairError::CaMismatch { expected: "a".into(), actual: "b".into() }), RETRY_NEEDS_FIX);
        for code in [401, 422, 426] {
            assert_eq!(retry_delay(&status(code)), RETRY_NEEDS_FIX, "{code}");
        }
        assert_eq!(retry_delay(&status(503)), RETRY_FAST);
        assert_eq!(retry_delay(&PairError::Transport(TransportError::Network("down".into()))), RETRY_FAST);
    }
}
