//! HTTP transport to the brain (protocol v1).
use std::path::Path;
use std::time::Duration;

use async_trait::async_trait;
use chrono::{DateTime, SecondsFormat, Utc};
use reqwest::{header, Client, StatusCode};
use tokio::io::AsyncWriteExt;
use tokio_util::io::ReaderStream;

use crate::item::{ItemKind, ItemMeta};
use crate::ota::system::BundleFetcher;
use crate::wire::{HeartbeatRequest, HeartbeatResponse, PairRequest, PairResponse, RemoteCommand, AGENT_VERSION};

pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
/// Client default, used by requests without their own timeout (pairing).
pub const REQUEST_TIMEOUT: Duration = Duration::from_secs(120);
/// A hung heartbeat would delay remote commands such as Pause, so it gives up quickly.
pub const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(10);
/// Every upload gets at least this long...
pub const UPLOAD_TIMEOUT_FLOOR: Duration = Duration::from_secs(120);
/// ...and as long as its body takes at this rate, so a large item on a slow link can finish.
pub const UPLOAD_MIN_BYTES_PER_SEC: u64 = 64 * 1024;
/// Dead connections (WiFi dropped mid-request) are noticed by keepalive probes, and pooled
/// connections are not reused after they may have been silently dropped by the AP or a NAT.
const TCP_KEEPALIVE: Duration = Duration::from_secs(15);
const POOL_IDLE_TIMEOUT: Duration = Duration::from_secs(30);
/// Read size for streamed upload bodies (the default 4 KiB costs many syscalls on a Pi Zero).
const UPLOAD_CHUNK_BYTES: usize = 64 * 1024;

/// Per-request time limits; overridable for tests.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Timeouts {
    pub heartbeat: Duration,
    pub upload_floor: Duration,
    pub upload_min_bytes_per_sec: u64,
}

impl Default for Timeouts {
    fn default() -> Self {
        Self {
            heartbeat: HEARTBEAT_TIMEOUT,
            upload_floor: UPLOAD_TIMEOUT_FLOOR,
            upload_min_bytes_per_sec: UPLOAD_MIN_BYTES_PER_SEC,
        }
    }
}

impl Timeouts {
    /// `max(upload_floor, size / upload_min_bytes_per_sec)`.
    pub fn upload(&self, size: u64) -> Duration {
        let millis = size.saturating_mul(1000).div_ceil(self.upload_min_bytes_per_sec.max(1));
        let at_min_rate = Duration::from_millis(millis);
        at_min_rate.max(self.upload_floor)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PutOutcome {
    Created,
    Duplicate,
    Conflict,
    Rejected(u16),
    BrainFull,
    UpgradeRequired,
    Unauthorized,
}

#[derive(Debug, thiserror::Error)]
pub enum TransportError {
    #[error("network error: {0}")]
    Network(String),
    #[error("brain returned HTTP {0}")]
    Status(u16),
    #[error("unexpected response: {0}")]
    Decode(String),
    #[error("local I/O error: {0}")]
    Io(#[from] std::io::Error),
}

#[async_trait]
pub trait Transport: Send + Sync {
    async fn put_item(&self, meta: &ItemMeta, data: &Path) -> Result<PutOutcome, TransportError>;
    async fn heartbeat(&self, request: &HeartbeatRequest) -> Result<Vec<RemoteCommand>, TransportError>;
}

pub fn rfc3339(t: DateTime<Utc>) -> String {
    t.to_rfc3339_opts(SecondsFormat::Millis, true)
}

/// Full error chain ("outer: inner: root"); reqwest's Display alone hides the root cause
/// (UnknownIssuer, connection refused, DNS failure...), which is the only on-device diagnostic.
fn error_chain(e: &dyn std::error::Error) -> String {
    let mut message = e.to_string();
    let mut source = e.source();
    while let Some(inner) = source {
        message.push_str(": ");
        message.push_str(&inner.to_string());
        source = inner.source();
    }
    message
}

fn network(e: reqwest::Error) -> TransportError {
    TransportError::Network(error_chain(&e))
}

fn decode(e: reqwest::Error) -> TransportError {
    TransportError::Decode(error_chain(&e))
}

/// Shared by production and tests. Redirects are never followed: a 303 would turn a PUT into a
/// GET whose 200 looks like "duplicate" and the uploader would delete unsent data. No proxy: the
/// brain is on the LAN and an environment proxy would only break mTLS.
fn base_builder() -> reqwest::ClientBuilder {
    Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .tcp_keepalive(TCP_KEEPALIVE)
        .pool_idle_timeout(POOL_IDLE_TIMEOUT)
}

/// Production client: https only (wiremock tests use plain http, so they build from `base_builder`).
pub fn build_client(tls: rustls::ClientConfig) -> Result<Client, TransportError> {
    base_builder().use_preconfigured_tls(tls).https_only(true).build().map_err(network)
}

/// Only the contract's item-specific rejections (409, 413, 422) lead to quarantine. Every other
/// status is retried with backoff, because quarantining drops data from the brain's view: 400,
/// 404 and 405 mean a proxy or URL problem that would hit every item, 408/425/429 and 5xx are
/// transient, and 3xx (redirects are not followed) is a misconfiguration.
pub fn map_put_status(status: u16) -> Result<PutOutcome, TransportError> {
    match status {
        201 => Ok(PutOutcome::Created),
        200 => Ok(PutOutcome::Duplicate),
        409 => Ok(PutOutcome::Conflict),
        413 | 422 => Ok(PutOutcome::Rejected(status)),
        401 | 403 => Ok(PutOutcome::Unauthorized),
        426 => Ok(PutOutcome::UpgradeRequired),
        507 => Ok(PutOutcome::BrainFull),
        _ => Err(TransportError::Status(status)),
    }
}

pub struct HttpTransport {
    client: Client,
    base_url: String,
    timeouts: Timeouts,
}

impl HttpTransport {
    pub fn new(client: Client, base_url: &str) -> Self {
        Self { client, base_url: base_url.trim_end_matches('/').to_string(), timeouts: Timeouts::default() }
    }

    #[must_use]
    pub fn with_timeouts(self, timeouts: Timeouts) -> Self {
        Self { timeouts, ..self }
    }
}

#[async_trait]
impl Transport for HttpTransport {
    async fn put_item(&self, meta: &ItemMeta, data: &Path) -> Result<PutOutcome, TransportError> {
        let file = tokio::fs::File::open(data).await?;
        let size = file.metadata().await?.len();
        let mut request = self
            .client
            .put(format!("{}/v1/items/{}", self.base_url, meta.id))
            .header("X-Loopi-Kind", meta.kind.as_str())
            .header("X-Loopi-Start", rfc3339(meta.started_at))
            .header("X-Loopi-End", rfc3339(meta.ended_at))
            .header("X-Loopi-Sha256", meta.sha256.as_str())
            .header("X-Loopi-Agent-Version", AGENT_VERSION)
            .header(header::CONTENT_TYPE, meta.kind.content_type())
            .header(header::CONTENT_LENGTH, size)
            .timeout(self.timeouts.upload(size))
            .body(reqwest::Body::wrap_stream(ReaderStream::with_capacity(file, UPLOAD_CHUNK_BYTES)));
        if let (ItemKind::Video, Some(fps)) = (meta.kind, meta.video_fps) {
            request = request.header("X-Loopi-Video-Fps", fps);
        }
        let response = request.send().await.map_err(network)?;
        map_put_status(response.status().as_u16())
    }

    async fn heartbeat(&self, request: &HeartbeatRequest) -> Result<Vec<RemoteCommand>, TransportError> {
        let response = self
            .client
            .post(format!("{}/v1/heartbeat", self.base_url))
            .header("X-Loopi-Agent-Version", AGENT_VERSION)
            .timeout(self.timeouts.heartbeat)
            .json(request)
            .send()
            .await
            .map_err(network)?;
        if response.status() != StatusCode::OK {
            return Err(TransportError::Status(response.status().as_u16()));
        }
        let body: HeartbeatResponse = response.json().await.map_err(decode)?;
        Ok(body.commands)
    }
}

#[async_trait]
impl BundleFetcher for HttpTransport {
    /// GET /v1/ota/{bundle} into `dest`, resuming with `Range: bytes=<have>-` (P4a design §6). A 200 answer to a range
    /// request restarts the file; the body may never grow past `size`.
    async fn fetch(&self, bundle: &str, dest: &Path, size: u64) -> Result<(), TransportError> {
        let mut have = match tokio::fs::metadata(dest).await {
            Ok(meta) => meta.len(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => 0,
            Err(e) => return Err(e.into()),
        };
        if have > size {
            tokio::fs::remove_file(dest).await?;
            have = 0;
        }
        if have == size {
            return Ok(());
        }
        let mut request = self
            .client
            .get(format!("{}/v1/ota/{bundle}", self.base_url))
            .header("X-Loopi-Agent-Version", AGENT_VERSION)
            .timeout(self.timeouts.upload(size - have));
        if have > 0 {
            request = request.header(header::RANGE, format!("bytes={have}-"));
        }
        let mut response = request.send().await.map_err(network)?;
        let mut written = match response.status().as_u16() {
            206 if have > 0 => have,
            200 => 0,
            status => return Err(TransportError::Status(status)),
        };
        let mut file = if written == 0 {
            tokio::fs::File::create(dest).await?
        } else {
            tokio::fs::OpenOptions::new().append(true).open(dest).await?
        };
        while let Some(chunk) = response.chunk().await.map_err(network)? {
            written += chunk.len() as u64;
            if written > size {
                drop(file);
                tokio::fs::remove_file(dest).await?;
                return Err(TransportError::Decode(format!("bundle larger than the {size} bytes announced")));
            }
            file.write_all(&chunk).await?;
        }
        file.sync_all().await?;
        if written != size {
            return Err(TransportError::Decode(format!("bundle ended at {written} of {size} bytes")));
        }
        Ok(())
    }
}

pub async fn pair(client: &Client, base_url: &str, request: &PairRequest) -> Result<PairResponse, TransportError> {
    let response = client
        .post(format!("{}/v1/pair", base_url.trim_end_matches('/')))
        .header("X-Loopi-Agent-Version", AGENT_VERSION)
        .json(request)
        .send()
        .await
        .map_err(network)?;
    if response.status() != StatusCode::CREATED {
        return Err(TransportError::Status(response.status().as_u16()));
    }
    response.json().await.map_err(decode)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;
    use serde_json::json;
    use wiremock::matchers::{body_bytes, header, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    const ID: &str = "01JA2B3C4D5E6F7G8H9J0KMNPQ";

    fn video_item(dir: &Path) -> (ItemMeta, std::path::PathBuf) {
        let data = dir.join("seg.h264");
        std::fs::write(&data, b"frames").unwrap();
        let (sha256, size) = crate::queue::sha256_file(&data).unwrap();
        let meta = ItemMeta {
            id: ID.into(),
            kind: ItemKind::Video,
            started_at: Utc.with_ymd_and_hms(2026, 10, 14, 1, 0, 0).unwrap(),
            ended_at: Utc.with_ymd_and_hms(2026, 10, 14, 1, 1, 0).unwrap(),
            sha256,
            size,
            video_fps: Some(2),
        };
        (meta, data)
    }

    #[tokio::test]
    async fn put_item_sends_protocol_headers_and_body() {
        let server = MockServer::start().await;
        let dir = tempfile::tempdir().unwrap();
        let (meta, data) = video_item(dir.path());
        Mock::given(method("PUT"))
            .and(path(format!("/v1/items/{ID}")))
            .and(header("X-Loopi-Kind", "video"))
            .and(header("X-Loopi-Start", "2026-10-14T01:00:00.000Z"))
            .and(header("X-Loopi-End", "2026-10-14T01:01:00.000Z"))
            .and(header("X-Loopi-Sha256", meta.sha256.as_str()))
            .and(header("X-Loopi-Video-Fps", "2"))
            .and(header("Content-Type", "video/h264"))
            .and(header("X-Loopi-Agent-Version", AGENT_VERSION))
            .and(body_bytes(b"frames".to_vec()))
            .respond_with(ResponseTemplate::new(201))
            .expect(1)
            .mount(&server)
            .await;
        let transport = HttpTransport::new(Client::new(), &server.uri());
        assert_eq!(transport.put_item(&meta, &data).await.unwrap(), PutOutcome::Created);
    }

    #[test]
    fn status_codes_map_to_outcomes() {
        assert_eq!(map_put_status(201).unwrap(), PutOutcome::Created);
        assert_eq!(map_put_status(200).unwrap(), PutOutcome::Duplicate);
        assert_eq!(map_put_status(409).unwrap(), PutOutcome::Conflict);
        assert_eq!(map_put_status(401).unwrap(), PutOutcome::Unauthorized);
        assert_eq!(map_put_status(426).unwrap(), PutOutcome::UpgradeRequired);
        assert_eq!(map_put_status(507).unwrap(), PutOutcome::BrainFull);
        assert_eq!(map_put_status(422).unwrap(), PutOutcome::Rejected(422));
        assert!(matches!(map_put_status(503), Err(TransportError::Status(503))));
        assert!(matches!(map_put_status(502), Err(TransportError::Status(502))));
        assert_eq!(map_put_status(413).unwrap(), PutOutcome::Rejected(413));
    }

    #[test]
    fn non_item_specific_4xx_statuses_are_retryable() {
        for code in [400u16, 404, 405, 408, 410, 415, 425, 429, 431] {
            assert!(matches!(map_put_status(code), Err(TransportError::Status(c)) if c == code));
        }
    }

    #[tokio::test]
    async fn missing_payload_is_an_io_error() {
        let dir = tempfile::tempdir().unwrap();
        let (meta, data) = video_item(dir.path());
        std::fs::remove_file(&data).unwrap();
        let transport = HttpTransport::new(Client::new(), "http://127.0.0.1:9");
        assert!(matches!(transport.put_item(&meta, &data).await, Err(TransportError::Io(_))));
    }

    #[tokio::test]
    async fn heartbeat_returns_commands_and_fails_on_non_200() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/v1/heartbeat"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"commands": [{"id": ID, "type": "pause", "payload": {}}]})))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        Mock::given(method("POST")).and(path("/v1/heartbeat")).respond_with(ResponseTemplate::new(401)).mount(&server).await;
        let transport = HttpTransport::new(Client::new(), &format!("{}/", server.uri()));
        let request = crate::wire::tests::sample_heartbeat();
        let commands = transport.heartbeat(&request).await.unwrap();
        assert_eq!(commands[0].kind, "pause");
        assert!(matches!(transport.heartbeat(&request).await, Err(TransportError::Status(401))));
    }

    #[tokio::test]
    async fn pair_requires_201() {
        let server = MockServer::start().await;
        Mock::given(method("POST")).and(path("/v1/pair")).respond_with(ResponseTemplate::new(401)).mount(&server).await;
        let request = PairRequest {
            token: "ABCD-EFGH".into(),
            csr_pem: "csr".into(),
            hw_info: crate::wire::HwInfo { model: "m".into(), serial: "s".into(), agent_version: AGENT_VERSION.into() },
        };
        assert!(matches!(pair(&Client::new(), &server.uri(), &request).await, Err(TransportError::Status(401))));
    }

    #[tokio::test]
    async fn redirects_are_not_followed_and_never_mean_duplicate() {
        for code in [303u16, 307] {
            let server = MockServer::start().await;
            let dir = tempfile::tempdir().unwrap();
            let (meta, data) = video_item(dir.path());
            Mock::given(method("PUT"))
                .respond_with(ResponseTemplate::new(code).insert_header("Location", "/elsewhere"))
                .mount(&server)
                .await;
            Mock::given(path("/elsewhere")).respond_with(ResponseTemplate::new(200)).mount(&server).await;
            let transport = HttpTransport::new(base_builder().build().unwrap(), &server.uri());
            let result = transport.put_item(&meta, &data).await;
            assert!(matches!(result, Err(TransportError::Status(c)) if c == code), "{code}: {result:?}");
        }
    }

    #[tokio::test]
    async fn network_error_includes_root_cause() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let dir = tempfile::tempdir().unwrap();
        let (meta, data) = video_item(dir.path());
        let transport = HttpTransport::new(base_builder().build().unwrap(), &format!("http://127.0.0.1:{port}"));
        let Err(TransportError::Network(message)) = transport.put_item(&meta, &data).await else { panic!("expected network error") };
        assert!(message.contains(": "), "{message}");
        assert!(message.to_lowercase().contains("refused") || message.contains("os error"), "{message}");
    }

    #[test]
    fn upload_timeout_grows_with_size_above_the_floor() {
        let t = Timeouts::default();
        assert_eq!(t.upload(0), Duration::from_secs(120));
        assert_eq!(t.upload(64 * 1024 * 120), Duration::from_secs(120));
        assert_eq!(t.upload(64 * 1024 * 121), Duration::from_secs(121));
        assert_eq!(t.upload(crate::config::MAX_ITEM_BYTES), Duration::from_secs(1024), "64 MiB at 64 KiB/s");
        assert_eq!(t.heartbeat, Duration::from_secs(10));
        let zero_rate = Timeouts { upload_min_bytes_per_sec: 0, ..t };
        assert_eq!(zero_rate.upload(10), Duration::from_secs(120), "no division by zero");
    }

    fn short(heartbeat_ms: u64, upload_floor_ms: u64, rate: u64) -> Timeouts {
        Timeouts {
            heartbeat: Duration::from_millis(heartbeat_ms),
            upload_floor: Duration::from_millis(upload_floor_ms),
            upload_min_bytes_per_sec: rate,
        }
    }

    #[tokio::test]
    async fn a_hanging_heartbeat_gives_up_after_its_timeout() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"commands": []})).set_delay(Duration::from_millis(1500)))
            .mount(&server)
            .await;
        let transport = HttpTransport::new(base_builder().build().unwrap(), &server.uri()).with_timeouts(short(200, 60_000, 1));
        let started = std::time::Instant::now();
        let result = transport.heartbeat(&crate::wire::tests::sample_heartbeat()).await;
        assert!(matches!(&result, Err(TransportError::Network(m)) if m.contains("timed out")), "{result:?}");
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[tokio::test]
    async fn upload_timeout_follows_the_item_size() {
        let server = MockServer::start().await;
        Mock::given(method("PUT")).respond_with(ResponseTemplate::new(201).set_delay(Duration::from_millis(400))).mount(&server).await;
        let dir = tempfile::tempdir().unwrap();
        let (meta, data) = video_item(dir.path());
        let client = base_builder().build().unwrap();
        // 6 bytes at 1 B/s: 6 s allowed, so the slow brain answers in time.
        let patient = HttpTransport::new(client.clone(), &server.uri()).with_timeouts(short(60_000, 100, 1));
        assert_eq!(patient.put_item(&meta, &data).await.unwrap(), PutOutcome::Created);
        // A fast minimum rate leaves only the 100 ms floor.
        let hasty = HttpTransport::new(client, &server.uri()).with_timeouts(short(60_000, 100, u64::MAX));
        assert!(matches!(hasty.put_item(&meta, &data).await, Err(TransportError::Network(_))));
    }

    #[test]
    fn production_client_builds() {
        assert!(build_client(crate::tls::pairing_config(None).unwrap()).is_ok());
    }

    mod bundles {
        use super::*;
        use crate::ota::system::BundleFetcher;

        const NAME: &str = "loopi-dev-2026.10.21-a1b2c3d.raucb";
        const BODY: &[u8] = b"0123456789abcdef";

        fn transport(server: &MockServer) -> HttpTransport {
            HttpTransport::new(base_builder().build().unwrap(), &server.uri())
        }

        #[tokio::test]
        async fn downloads_a_whole_bundle() {
            let server = MockServer::start().await;
            Mock::given(method("GET")).and(path(format!("/v1/ota/{NAME}")))
                .and(header("X-Loopi-Agent-Version", AGENT_VERSION))
                .respond_with(ResponseTemplate::new(200).set_body_bytes(BODY))
                .expect(1).mount(&server).await;
            let dir = tempfile::tempdir().unwrap();
            let dest = dir.path().join("b.part");
            transport(&server).fetch(NAME, &dest, BODY.len() as u64).await.unwrap();
            assert_eq!(std::fs::read(&dest).unwrap(), BODY);
        }

        #[tokio::test]
        async fn resumes_with_range() {
            let server = MockServer::start().await;
            Mock::given(method("GET")).and(header("Range", "bytes=6-"))
                .respond_with(ResponseTemplate::new(206).set_body_bytes(&BODY[6..]))
                .expect(1).mount(&server).await;
            let dir = tempfile::tempdir().unwrap();
            let dest = dir.path().join("b.part");
            std::fs::write(&dest, &BODY[..6]).unwrap();
            transport(&server).fetch(NAME, &dest, BODY.len() as u64).await.unwrap();
            assert_eq!(std::fs::read(&dest).unwrap(), BODY);
        }

        #[tokio::test]
        async fn a_server_that_ignores_range_restarts_the_file() {
            let server = MockServer::start().await;
            Mock::given(method("GET")).respond_with(ResponseTemplate::new(200).set_body_bytes(BODY)).mount(&server).await;
            let dir = tempfile::tempdir().unwrap();
            let dest = dir.path().join("b.part");
            std::fs::write(&dest, b"zzzzzz").unwrap();
            transport(&server).fetch(NAME, &dest, BODY.len() as u64).await.unwrap();
            assert_eq!(std::fs::read(&dest).unwrap(), BODY);
        }

        #[tokio::test]
        async fn a_complete_file_needs_no_request() {
            let server = MockServer::start().await;
            Mock::given(method("GET")).respond_with(ResponseTemplate::new(500)).expect(0).mount(&server).await;
            let dir = tempfile::tempdir().unwrap();
            let dest = dir.path().join("b.part");
            std::fs::write(&dest, BODY).unwrap();
            transport(&server).fetch(NAME, &dest, BODY.len() as u64).await.unwrap();
        }

        #[tokio::test]
        async fn short_long_and_refused_bodies_are_errors() {
            let dir = tempfile::tempdir().unwrap();
            for (status, body, expect) in [(200, &BODY[..4], "ended"), (200, b"0123456789abcdefXX".as_slice(), "larger"),
                                            (404, b"".as_slice(), "404"), (416, b"".as_slice(), "416")] {
                let server = MockServer::start().await;
                Mock::given(method("GET")).respond_with(ResponseTemplate::new(status).set_body_bytes(body)).mount(&server).await;
                let dest = dir.path().join(format!("{status}-{}.part", body.len()));
                let err = transport(&server).fetch(NAME, &dest, BODY.len() as u64).await.unwrap_err();
                assert!(err.to_string().contains(expect), "{err}");
            }
        }

        #[tokio::test]
        async fn an_oversized_partial_file_is_dropped() {
            let server = MockServer::start().await;
            Mock::given(method("GET")).respond_with(ResponseTemplate::new(200).set_body_bytes(BODY)).mount(&server).await;
            let dir = tempfile::tempdir().unwrap();
            let dest = dir.path().join("b.part");
            std::fs::write(&dest, b"0123456789abcdef0123").unwrap();
            transport(&server).fetch(NAME, &dest, BODY.len() as u64).await.unwrap();
            assert_eq!(std::fs::read(&dest).unwrap(), BODY);
        }

        #[tokio::test]
        async fn a_short_body_keeps_the_partial_file_and_a_long_one_drops_it() {
            let dir = tempfile::tempdir().unwrap();
            let short = MockServer::start().await;
            Mock::given(method("GET")).respond_with(ResponseTemplate::new(200).set_body_bytes(&BODY[..4])).mount(&short).await;
            let kept = dir.path().join("short.part");
            transport(&short).fetch(NAME, &kept, BODY.len() as u64).await.unwrap_err();
            assert_eq!(std::fs::read(&kept).unwrap(), &BODY[..4], "the next attempt resumes from here");

            let long = MockServer::start().await;
            Mock::given(method("GET")).respond_with(ResponseTemplate::new(200).set_body_bytes(b"0123456789abcdefXX".as_slice())).mount(&long).await;
            let dropped = dir.path().join("long.part");
            transport(&long).fetch(NAME, &dropped, BODY.len() as u64).await.unwrap_err();
            assert!(!dropped.exists(), "a body past the announced size is poison");
        }

        #[tokio::test]
        async fn a_partial_answer_to_a_plain_request_is_refused() {
            let server = MockServer::start().await;
            Mock::given(method("GET")).respond_with(ResponseTemplate::new(206).set_body_bytes(&BODY[6..])).mount(&server).await;
            let dir = tempfile::tempdir().unwrap();
            let err = transport(&server).fetch(NAME, &dir.path().join("b.part"), BODY.len() as u64).await.unwrap_err();
            assert!(matches!(err, TransportError::Status(206)), "{err}");
        }

        #[tokio::test]
        async fn a_dead_brain_is_a_network_error_that_keeps_the_partial_file() {
            let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            let port = listener.local_addr().unwrap().port();
            drop(listener);
            let dir = tempfile::tempdir().unwrap();
            let dest = dir.path().join("b.part");
            std::fs::write(&dest, &BODY[..6]).unwrap();
            let transport = HttpTransport::new(base_builder().build().unwrap(), &format!("http://127.0.0.1:{port}"));
            let err = transport.fetch(NAME, &dest, BODY.len() as u64).await.unwrap_err();
            assert!(matches!(err, TransportError::Network(_)), "{err}");
            assert_eq!(std::fs::read(&dest).unwrap(), &BODY[..6]);
        }
    }
}
