//! rustls client configurations: ring provider, TLS 1.3, ChaCha20 first (no AES hardware on ARMv6).
use std::sync::Arc;

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{ring as ring_provider, verify_tls12_signature, verify_tls13_signature, CryptoProvider};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName, UnixTime};
use rustls::{ClientConfig, DigitallySignedStruct, RootCertStore, SignatureScheme};

#[derive(Debug, thiserror::Error)]
pub enum TlsError {
    #[error("invalid PEM: {0}")]
    Pem(String),
    #[error(transparent)]
    Rustls(#[from] rustls::Error),
}

pub fn provider() -> CryptoProvider {
    let mut provider = ring_provider::default_provider();
    provider.cipher_suites = vec![
        ring_provider::cipher_suite::TLS13_CHACHA20_POLY1305_SHA256,
        ring_provider::cipher_suite::TLS13_AES_128_GCM_SHA256,
        ring_provider::cipher_suite::TLS13_AES_256_GCM_SHA384,
    ];
    provider
}

fn pem_err(e: impl std::fmt::Display) -> TlsError {
    TlsError::Pem(e.to_string())
}

/// Client config for every request after pairing: trust only the brain CA, present the device cert.
pub fn mtls_config(ca_pem: &str, cert_pem: &str, key_pem: &str) -> Result<ClientConfig, TlsError> {
    let mut roots = RootCertStore::empty();
    for cert in CertificateDer::pem_slice_iter(ca_pem.as_bytes()) {
        roots.add(cert.map_err(pem_err)?)?;
    }
    if roots.is_empty() {
        return Err(TlsError::Pem("no CA certificate".into()));
    }
    let chain: Vec<CertificateDer<'static>> =
        CertificateDer::pem_slice_iter(cert_pem.as_bytes()).collect::<Result<_, _>>().map_err(pem_err)?;
    if chain.is_empty() {
        return Err(TlsError::Pem("no device certificate".into()));
    }
    let key = PrivateKeyDer::from_pem_slice(key_pem.as_bytes()).map_err(pem_err)?;
    Ok(ClientConfig::builder_with_provider(Arc::new(provider()))
        .with_protocol_versions(&[&rustls::version::TLS13])?
        .with_root_certificates(roots)
        .with_client_auth_cert(chain, key)?)
}

/// Pairing only: the device has no CA yet, so it accepts the server certificate and instead
/// checks the CA it receives against `brain.ca_fingerprint` (trust on first use).
pub fn pairing_config() -> Result<ClientConfig, TlsError> {
    let provider = Arc::new(provider());
    Ok(ClientConfig::builder_with_provider(provider.clone())
        .with_protocol_versions(&[&rustls::version::TLS13])?
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(AcceptAnyServerCert { provider }))
        .with_no_client_auth())
}

#[derive(Debug)]
struct AcceptAnyServerCert {
    provider: Arc<CryptoProvider>,
}

impl ServerCertVerifier for AcceptAnyServerCert {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(message, cert, dss, &self.provider.signature_verification_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider.signature_verification_algorithms.supported_schemes()
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use rcgen::{BasicConstraints, CertificateParams, IsCa, KeyPair, PKCS_ECDSA_P256_SHA256};

    /// (ca_pem, client_cert_pem, client_key_pem) signed by a throwaway CA.
    pub(crate) fn test_pki() -> (String, String, String) {
        let ca_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).unwrap();
        let mut ca_params = CertificateParams::new(Vec::<String>::new()).unwrap();
        ca_params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        let ca = ca_params.self_signed(&ca_key).unwrap();
        let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).unwrap();
        let cert = CertificateParams::new(vec!["loopi-device".to_string()])
            .unwrap()
            .signed_by(&key, &ca, &ca_key)
            .unwrap();
        (ca.pem(), cert.pem(), key.serialize_pem())
    }

    #[test]
    fn chacha20_is_preferred() {
        let first = provider().cipher_suites[0].suite();
        assert_eq!(first, rustls::CipherSuite::TLS13_CHACHA20_POLY1305_SHA256);
    }

    #[test]
    fn mtls_config_accepts_valid_material() {
        let (ca, cert, key) = test_pki();
        assert!(mtls_config(&ca, &cert, &key).is_ok());
    }

    #[test]
    fn mtls_config_rejects_garbage() {
        let (ca, cert, key) = test_pki();
        assert!(mtls_config("nope", &cert, &key).is_err());
        assert!(mtls_config(&ca, "nope", &key).is_err());
        assert!(mtls_config(&ca, &cert, "nope").is_err());
    }

    #[test]
    fn pairing_config_builds() {
        assert!(pairing_config().is_ok());
    }
}
