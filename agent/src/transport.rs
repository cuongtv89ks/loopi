//! HTTP transport to the brain (protocol v1).
use std::path::Path;
use std::time::Duration;

use async_trait::async_trait;
use chrono::{DateTime, SecondsFormat, Utc};
use reqwest::{header, Client, StatusCode};
use tokio_util::io::ReaderStream;

use crate::item::{ItemKind, ItemMeta};
use crate::wire::{HeartbeatRequest, HeartbeatResponse, PairRequest, PairResponse, RemoteCommand, AGENT_VERSION};

pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
pub const REQUEST_TIMEOUT: Duration = Duration::from_secs(120);

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

fn network(e: reqwest::Error) -> TransportError {
    TransportError::Network(e.to_string())
}

pub fn build_client(tls: rustls::ClientConfig) -> Result<Client, TransportError> {
    Client::builder()
        .use_preconfigured_tls(tls)
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .build()
        .map_err(network)
}

pub fn map_put_status(status: u16) -> Result<PutOutcome, TransportError> {
    match status {
        201 => Ok(PutOutcome::Created),
        200 => Ok(PutOutcome::Duplicate),
        409 => Ok(PutOutcome::Conflict),
        401 | 403 => Ok(PutOutcome::Unauthorized),
        // Transient client-side statuses: retry with backoff, never quarantine (that drops data).
        408 | 425 | 429 => Err(TransportError::Status(status)),
        426 => Ok(PutOutcome::UpgradeRequired),
        507 => Ok(PutOutcome::BrainFull),
        400..=499 => Ok(PutOutcome::Rejected(status)),
        _ => Err(TransportError::Status(status)),
    }
}

pub struct HttpTransport {
    client: Client,
    base_url: String,
}

impl HttpTransport {
    pub fn new(client: Client, base_url: &str) -> Self {
        Self { client, base_url: base_url.trim_end_matches('/').to_string() }
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
            .body(reqwest::Body::wrap_stream(ReaderStream::new(file)));
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
            .json(request)
            .send()
            .await
            .map_err(network)?;
        if response.status() != StatusCode::OK {
            return Err(TransportError::Status(response.status().as_u16()));
        }
        let body: HeartbeatResponse = response.json().await.map_err(|e| TransportError::Decode(e.to_string()))?;
        Ok(body.commands)
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
    response.json().await.map_err(|e| TransportError::Decode(e.to_string()))
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
    fn transient_4xx_statuses_are_retryable() {
        for code in [408u16, 425, 429] {
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
}
