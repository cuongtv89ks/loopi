//! rustls client configurations: ring provider, TLS 1.3, ChaCha20 first (no AES hardware on ARMv6).
use std::sync::Arc;

use rustls::client::WebPkiServerVerifier;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{ring as ring_provider, verify_tls12_signature, verify_tls13_signature, CryptoProvider};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName, UnixTime};
use sha2::{Digest, Sha256};
use crate::config::normalize_fingerprint;
use rustls::{CertificateError, ClientConfig, DigitallySignedStruct, RootCertStore, SignatureScheme};

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

/// Client config for every request after pairing: present the device certificate and accept
/// only a server that chains to the stored brain CA (see `CaVerifier`: EKU serverAuth, validity
/// and signatures are enforced, the host name is not).
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
    let provider = Arc::new(provider());
    let verifier = CaVerifier::new(roots, provider.clone())?;
    Ok(ClientConfig::builder_with_provider(provider)
        .with_protocol_versions(&[&rustls::version::TLS13])?
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(verifier))
        .with_client_auth_cert(chain, key)?)
}

/// Pairing only: the device has no CA yet. Without `ca_fingerprint` this is plain trust on first
/// use (any chain, handshake signatures still verified). With it, the handshake itself fails
/// unless the server presents a certificate with that SHA-256 and the leaf chains to it, so the
/// pairing token and CSR are never sent to an impostor.
pub fn pairing_config(ca_fingerprint: Option<&str>) -> Result<ClientConfig, TlsError> {
    let fingerprint = match ca_fingerprint {
        Some(raw) => Some(normalize_fingerprint(raw).ok_or_else(|| TlsError::Pem("invalid ca_fingerprint".into()))?),
        None => None,
    };
    let provider = Arc::new(provider());
    Ok(ClientConfig::builder_with_provider(provider.clone())
        .with_protocol_versions(&[&rustls::version::TLS13])?
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(PairingVerifier { provider, fingerprint }))
        .with_no_client_auth())
}

/// The one server-certificate policy, used after pairing and by pinned pairing: webpki validates
/// the chain against `roots` (signatures, validity, EKU serverAuth, so the clientAuth-only device
/// certificates the same CA issues are rejected), but the host name is not checked. The brain is
/// reached by a LAN address that may be missing from its certificate or change with DHCP, and
/// it is authenticated by its private CA, not by DNS.
#[derive(Debug)]
struct CaVerifier {
    inner: Arc<WebPkiServerVerifier>,
}

impl CaVerifier {
    fn new(roots: RootCertStore, provider: Arc<CryptoProvider>) -> Result<Self, rustls::Error> {
        let inner = WebPkiServerVerifier::builder_with_provider(Arc::new(roots), provider)
            .build()
            .map_err(|e| rustls::Error::General(e.to_string()))?;
        Ok(Self { inner })
    }
}

impl ServerCertVerifier for CaVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        // Any valid name works: webpki validates the chain before the name, and a name mismatch
        // (the only error tolerated below) therefore implies the chain was good.
        let name = ServerName::try_from("loopi.invalid").expect("static name");
        match self.inner.verify_server_cert(end_entity, intermediates, &name, &[], now) {
            Ok(verified) => Ok(verified),
            Err(rustls::Error::InvalidCertificate(
                CertificateError::NotValidForName | CertificateError::NotValidForNameContext { .. },
            )) => Ok(ServerCertVerified::assertion()),
            Err(e) => Err(e),
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        self.inner.verify_tls12_signature(message, cert, dss)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        self.inner.verify_tls13_signature(message, cert, dss)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.inner.supported_verify_schemes()
    }
}

#[derive(Debug)]
struct PairingVerifier {
    provider: Arc<CryptoProvider>,
    /// Normalized (lowercase hex) SHA-256 of the pinned CA certificate DER.
    fingerprint: Option<String>,
}

impl PairingVerifier {
    /// The leaf must chain to the pinned CA, which must itself be in the presented chain.
    fn verify_pinned(
        &self,
        pin: &str,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        let ca = std::iter::once(end_entity)
            .chain(intermediates)
            .find(|cert| hex::encode(Sha256::digest(cert.as_ref())) == pin)
            .ok_or_else(|| rustls::Error::General("server did not present the pinned CA certificate".into()))?;
        let mut roots = RootCertStore::empty();
        roots.add(ca.clone().into_owned())?;
        CaVerifier::new(roots, self.provider.clone())?.verify_server_cert(end_entity, intermediates, server_name, &[], now)
    }
}

impl ServerCertVerifier for PairingVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        match &self.fingerprint {
            Some(pin) => self.verify_pinned(pin, end_entity, intermediates, server_name, now),
            None => Ok(ServerCertVerified::assertion()),
        }
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
        test_pki_with(|_| {})
    }

    /// Like `test_pki`, letting the caller adjust the leaf certificate parameters.
    pub(crate) fn test_pki_with(adjust: impl FnOnce(&mut CertificateParams)) -> (String, String, String) {
        let ca = TestCa::new();
        let (cert, key) = ca.leaf(adjust);
        (ca.pem(), cert, key)
    }

    /// A throwaway CA that can issue several leaves (SAN "loopi-device" unless adjusted).
    pub(crate) struct TestCa {
        key: KeyPair,
        cert: rcgen::Certificate,
    }

    impl TestCa {
        pub(crate) fn new() -> Self {
            let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).unwrap();
            let mut params = CertificateParams::new(Vec::<String>::new()).unwrap();
            params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
            let cert = params.self_signed(&key).unwrap();
            Self { key, cert }
        }

        pub(crate) fn pem(&self) -> String {
            self.cert.pem()
        }

        /// (cert_pem, key_pem)
        pub(crate) fn leaf(&self, adjust: impl FnOnce(&mut CertificateParams)) -> (String, String) {
            let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).unwrap();
            let mut params = CertificateParams::new(vec!["loopi-device".to_string()]).unwrap();
            adjust(&mut params);
            let cert = params.signed_by(&key, &self.cert, &self.key).unwrap();
            (cert.pem(), key.serialize_pem())
        }
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
        assert!(pairing_config(None).is_ok());
        assert!(pairing_config(Some(&"ab".repeat(32))).is_ok());
        assert!(pairing_config(Some("nope")).is_err());
    }

    /// One-shot TLS server on 127.0.0.1 serving `chain_pems` (leaf first). Returns the port and a
    /// handle yielding whether any application bytes (an HTTP request) reached the server.
    fn tls_server(chain_pems: &str, key_pem: &str) -> (u16, std::thread::JoinHandle<bool>) {
        tls_server_with(chain_pems, key_pem, None)
    }

    /// Like `tls_server`; with `client_ca` the server requires a client certificate from that CA.
    fn tls_server_with(chain_pems: &str, key_pem: &str, client_ca: Option<&str>) -> (u16, std::thread::JoinHandle<bool>) {
        use std::io::{Read, Write};
        let chain: Vec<CertificateDer<'static>> =
            CertificateDer::pem_slice_iter(chain_pems.as_bytes()).collect::<Result<_, _>>().unwrap();
        let key = PrivateKeyDer::from_pem_slice(key_pem.as_bytes()).unwrap();
        let builder = rustls::ServerConfig::builder_with_provider(Arc::new(provider()))
            .with_protocol_versions(&[&rustls::version::TLS13])
            .unwrap();
        let builder = match client_ca {
            Some(ca_pem) => {
                let mut roots = RootCertStore::empty();
                roots.add(CertificateDer::from_pem_slice(ca_pem.as_bytes()).unwrap()).unwrap();
                let verifier =
                    rustls::server::WebPkiClientVerifier::builder_with_provider(Arc::new(roots), Arc::new(provider()))
                        .build()
                        .unwrap();
                builder.with_client_cert_verifier(verifier)
            }
            None => builder.with_no_client_auth(),
        };
        let config = builder.with_single_cert(chain, key).unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let handle = std::thread::spawn(move || {
            let (mut tcp, _) = listener.accept().unwrap();
            let mut conn = rustls::ServerConnection::new(Arc::new(config)).unwrap();
            let mut stream = rustls::Stream::new(&mut conn, &mut tcp);
            let mut buf = [0u8; 4096];
            match stream.read(&mut buf) {
                Ok(n) if n > 0 => {
                    let _ = stream.write_all(b"HTTP/1.1 201 Created\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}");
                    let _ = stream.flush();
                    true
                }
                _ => false,
            }
        });
        (port, handle)
    }

    async fn post(config: ClientConfig, port: u16) -> Result<u16, String> {
        let client = crate::transport::build_client(config).map_err(|e| e.to_string())?;
        let response = client.post(format!("https://127.0.0.1:{port}/v1/pair")).body("secret-token").send().await;
        response.map(|r| r.status().as_u16()).map_err(|e| e.to_string())
    }

    fn fingerprint(ca_pem: &str) -> String {
        crate::pairing::sha256_of_first_cert(ca_pem).unwrap()
    }

    #[tokio::test]
    async fn pinned_ca_in_presented_chain_is_accepted() {
        let (ca, leaf, key) = test_pki();
        let (port, server) = tls_server(&format!("{leaf}{ca}"), &key);
        let status = post(pairing_config(Some(&fingerprint(&ca).to_uppercase())).unwrap(), port).await;
        assert_eq!(status, Ok(201));
        assert!(server.join().unwrap());
    }

    #[tokio::test]
    async fn wrong_fingerprint_fails_before_any_request_bytes() {
        let (ca, leaf, key) = test_pki();
        let (port, server) = tls_server(&format!("{leaf}{ca}"), &key);
        let status = post(pairing_config(Some(&"cd".repeat(32))).unwrap(), port).await;
        assert!(status.is_err(), "{status:?}");
        assert!(!server.join().unwrap(), "server must not observe a request");
    }

    #[tokio::test]
    async fn leaf_not_issued_by_pinned_ca_is_rejected() {
        let (_, leaf, key) = test_pki();
        let (other_ca, _, _) = test_pki();
        let (port, server) = tls_server(&format!("{leaf}{other_ca}"), &key);
        let status = post(pairing_config(Some(&fingerprint(&other_ca))).unwrap(), port).await;
        assert!(status.is_err(), "{status:?}");
        assert!(!server.join().unwrap());
    }

    #[tokio::test]
    async fn missing_ca_in_chain_is_rejected_when_pinned() {
        let (ca, leaf, key) = test_pki();
        let (port, server) = tls_server(&leaf, &key);
        let status = post(pairing_config(Some(&fingerprint(&ca))).unwrap(), port).await;
        assert!(status.is_err(), "{status:?}");
        assert!(!server.join().unwrap());
    }

    #[tokio::test]
    async fn client_auth_only_leaf_cannot_impersonate_the_brain() {
        let (ca, leaf, key) = test_pki_with(|p| {
            p.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ClientAuth];
        });
        let (port, server) = tls_server(&format!("{leaf}{ca}"), &key);
        let status = post(pairing_config(Some(&fingerprint(&ca))).unwrap(), port).await;
        assert!(status.is_err(), "{status:?}");
        assert!(!server.join().unwrap());
    }

    #[tokio::test]
    async fn server_auth_leaf_is_accepted_when_eku_present() {
        let (ca, leaf, key) = test_pki_with(|p| {
            p.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ServerAuth];
        });
        let (port, server) = tls_server(&format!("{leaf}{ca}"), &key);
        assert_eq!(post(pairing_config(Some(&fingerprint(&ca))).unwrap(), port).await, Ok(201));
        assert!(server.join().unwrap());
    }

    #[tokio::test]
    async fn expired_leaf_is_rejected() {
        let (ca, leaf, key) = test_pki_with(|p| {
            p.not_before = rcgen::date_time_ymd(2020, 1, 1);
            p.not_after = rcgen::date_time_ymd(2021, 1, 1);
        });
        let (port, server) = tls_server(&format!("{leaf}{ca}"), &key);
        let status = post(pairing_config(Some(&fingerprint(&ca))).unwrap(), port).await;
        assert!(status.is_err(), "{status:?}");
        assert!(!server.join().unwrap());
    }

    fn server_auth(p: &mut CertificateParams) {
        p.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ServerAuth];
    }

    fn client_auth(p: &mut CertificateParams) {
        p.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ClientAuth];
    }

    /// Device identity from `ca` and the matching mTLS client config.
    fn device_config(ca: &TestCa) -> ClientConfig {
        let (cert, key) = ca.leaf(client_auth);
        mtls_config(&ca.pem(), &cert, &key).unwrap()
    }

    #[tokio::test]
    async fn mtls_accepts_a_server_whose_name_does_not_match_the_url_when_chained_to_the_ca() {
        let ca = TestCa::new();
        // SAN "brain.example" while the URL host is 127.0.0.1 (a DHCP change, an IP not in the SANs).
        let (server_cert, server_key) = ca.leaf(|p| {
            server_auth(p);
            p.subject_alt_names = vec![rcgen::SanType::DnsName("brain.example".try_into().unwrap())];
        });
        let (port, server) = tls_server_with(&server_cert, &server_key, Some(&ca.pem()));
        assert_eq!(post(device_config(&ca), port).await, Ok(201));
        assert!(server.join().unwrap(), "the request reached the brain over mTLS");
    }

    #[tokio::test]
    async fn mtls_rejects_a_server_from_another_ca() {
        let ca = TestCa::new();
        let other = TestCa::new();
        let (server_cert, server_key) = other.leaf(server_auth);
        let (port, server) = tls_server_with(&format!("{server_cert}{}", other.pem()), &server_key, None);
        let status = post(device_config(&ca), port).await;
        assert!(status.is_err(), "{status:?}");
        assert!(!server.join().unwrap(), "nothing is sent to an impostor");
    }

    #[tokio::test]
    async fn mtls_rejects_a_client_auth_only_certificate_from_the_stored_ca() {
        // What another paired device holds: it must not be able to pose as the brain.
        let ca = TestCa::new();
        let (server_cert, server_key) = ca.leaf(client_auth);
        let (port, server) = tls_server_with(&server_cert, &server_key, None);
        let status = post(device_config(&ca), port).await;
        assert!(status.is_err(), "{status:?}");
        assert!(!server.join().unwrap());
    }

    #[tokio::test]
    async fn mtls_rejects_an_expired_server_certificate() {
        let ca = TestCa::new();
        let (server_cert, server_key) = ca.leaf(|p| {
            p.not_before = rcgen::date_time_ymd(2020, 1, 1);
            p.not_after = rcgen::date_time_ymd(2021, 1, 1);
        });
        let (port, server) = tls_server_with(&server_cert, &server_key, None);
        assert!(post(device_config(&ca), port).await.is_err());
        assert!(!server.join().unwrap());
    }

    #[tokio::test]
    async fn without_fingerprint_any_server_is_accepted_tofu() {
        let (ca, leaf, key) = test_pki();
        let (port, server) = tls_server(&format!("{leaf}{ca}"), &key);
        assert_eq!(post(pairing_config(None).unwrap(), port).await, Ok(201));
        assert!(server.join().unwrap());
    }
}
