//! The whole agent against a mock brain, with a fake camera and a tone "microphone".
//!
//! The brain mock enforces the real contract (`docs/protocol/v1.openapi.yaml`): every heartbeat
//! body is validated against the `Heartbeat` schema and every PUT against its header rules, so
//! drift between agent and contract fails this test.
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::DateTime;
use loopi_agent::config::{BrainConfig, CaptureConfig, Config, GpioConfig, LedConfig, PathsConfig, ScheduleConfig};
use loopi_agent::recorder::audio::{AudioSource, AudioSourceFactory};
use loopi_agent::run::{run, Deps, Timings};
use loopi_agent::sysinfo::NoProbe;
use loopi_agent::testing::{FixedDisk, RecordingLed, SyncedClock, ToneSource};
use loopi_agent::transport::HttpTransport;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tokio::sync::{mpsc, watch};
use wiremock::matchers::{method, path, path_regex};
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

const PAUSE_ID: &str = "01JA2B3C4D5E6F7G8H9J0KMNPQ";
const AUDIO_SEGMENT: Duration = Duration::from_secs(1);
const MAX_SEGMENT_SECONDS: i64 = 3600;

type Violations = Arc<Mutex<Vec<String>>>;

fn fake_camera(dir: &Path) -> String {
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/fake-rpicam-vid.sh");
    let dst = dir.join("fake-rpicam-vid.sh");
    fs::copy(fixture, &dst).unwrap();
    fs::set_permissions(&dst, fs::Permissions::from_mode(0o755)).unwrap();
    dst.to_string_lossy().into_owned()
}

fn uploads(requests: &[Request], kind: &str) -> usize {
    requests
        .iter()
        .filter(|r| r.method.as_str() == "PUT")
        .filter(|r| r.headers.get("x-loopi-kind").is_some_and(|v| v == kind))
        .count()
}

fn heartbeats(requests: &[Request]) -> Vec<Value> {
    requests
        .iter()
        .filter(|r| r.url.path() == "/v1/heartbeat")
        .map(|r| serde_json::from_slice(&r.body).unwrap())
        .collect()
}

/// Contract violations of `instance` against `#/components/schemas/<schema_name>`.
fn schema_errors(schema_name: &str, instance: &Value) -> Vec<String> {
    let spec_path = concat!(env!("CARGO_MANIFEST_DIR"), "/../docs/protocol/v1.openapi.yaml");
    let spec: Value = serde_yaml::from_str(&fs::read_to_string(spec_path).expect("contract file from P1a")).unwrap();
    let schema = json!({"$ref": format!("#/components/schemas/{schema_name}"), "components": spec["components"]});
    let validator = jsonschema::validator_for(&schema).unwrap();
    validator.iter_errors(instance).map(|e| e.to_string()).collect()
}

fn header<'a>(request: &'a Request, name: &str) -> Option<&'a str> {
    request.headers.get(name).and_then(|v| v.to_str().ok())
}

/// Header rules of `PUT /v1/items/{id}`; returns every violation found.
fn put_violations(request: &Request) -> Vec<String> {
    let mut found = Vec::new();
    let kind = header(request, "x-loopi-kind").unwrap_or("");
    let content_type = match kind {
        "video" => "video/h264",
        "audio" => "audio/ogg",
        "marker" => "application/json",
        other => {
            found.push(format!("X-Loopi-Kind {other:?} is not video|audio|marker"));
            ""
        }
    };
    if header(request, "content-type") != Some(content_type) {
        found.push(format!("Content-Type {:?} does not match kind {kind}", header(request, "content-type")));
    }
    let time = |name: &str, found: &mut Vec<String>| {
        let parsed = header(request, name).and_then(|v| DateTime::parse_from_rfc3339(v).ok());
        if parsed.is_none() {
            found.push(format!("{name} missing or not RFC 3339: {:?}", header(request, name)));
        }
        parsed
    };
    if let (Some(start), Some(end)) = (time("x-loopi-start", &mut found), time("x-loopi-end", &mut found)) {
        let seconds = (end - start).num_seconds();
        if end < start || seconds > MAX_SEGMENT_SECONDS {
            found.push(format!("End - Start = {seconds} s is outside 0..={MAX_SEGMENT_SECONDS}"));
        }
    }
    let digest = hex::encode(Sha256::digest(&request.body));
    if header(request, "x-loopi-sha256") != Some(digest.as_str()) {
        found.push(format!("X-Loopi-Sha256 {:?} does not match body {digest}", header(request, "x-loopi-sha256")));
    }
    if !header(request, "x-loopi-agent-version").is_some_and(|v| v.split('.').count() >= 3) {
        found.push("X-Loopi-Agent-Version missing or malformed".into());
    }
    match (kind, header(request, "x-loopi-video-fps")) {
        ("video", Some(fps)) if fps.parse::<u32>().is_ok_and(|f| (1..=30).contains(&f)) => {}
        ("video", other) => found.push(format!("X-Loopi-Video-Fps {other:?} is not 1..=30")),
        (_, Some(_)) => found.push("X-Loopi-Video-Fps only belongs on video".into()),
        _ => {}
    }
    found
}

fn record(violations: &Violations, found: Vec<String>) -> bool {
    let ok = found.is_empty();
    violations.lock().unwrap().extend(found);
    ok
}

/// All regular files below `dir`, recursively (empty when `dir` is missing).
fn files_under(dir: &Path) -> Vec<std::path::PathBuf> {
    let Ok(entries) = fs::read_dir(dir) else { return Vec::new() };
    entries
        .filter_map(Result::ok)
        .flat_map(|e| if e.path().is_dir() { files_under(&e.path()) } else { vec![e.path()] })
        .collect()
}

fn stray_camera_processes(script: &str) -> bool {
    std::process::Command::new("pgrep").args(["-f", script]).output().unwrap().status.success()
}

#[tokio::test]
async fn records_uploads_and_obeys_a_remote_pause() {
    let brain = MockServer::start().await;
    let violations: Violations = Arc::default();
    let put_violations_log = violations.clone();
    Mock::given(method("PUT"))
        .and(path_regex(r"^/v1/items/[0-9A-Z]{26}$"))
        .respond_with(move |request: &Request| {
            let status = if record(&put_violations_log, put_violations(request)) { 201 } else { 422 };
            ResponseTemplate::new(status)
        })
        .mount(&brain)
        .await;
    // Heartbeat 0 (~0 s): nothing. Heartbeat 1 (~1.5 s): pause. Afterwards: nothing.
    let heartbeat_log = violations.clone();
    let calls = AtomicUsize::new(0);
    Mock::given(method("POST"))
        .and(path("/v1/heartbeat"))
        .respond_with(move |request: &Request| {
            let body: Value = serde_json::from_slice(&request.body).unwrap_or(Value::Null);
            let errors = schema_errors("Heartbeat", &body);
            if !record(&heartbeat_log, errors.into_iter().map(|e| format!("heartbeat: {e}")).collect()) {
                return ResponseTemplate::new(422);
            }
            let commands = match calls.fetch_add(1, Ordering::SeqCst) {
                1 => json!([{"id": PAUSE_ID, "type": "pause", "payload": {}}]),
                _ => json!([]),
            };
            ResponseTemplate::new(200).set_body_json(json!({ "commands": commands }))
        })
        .mount(&brain)
        .await;

    let tmp = tempfile::tempdir().unwrap();
    let data_dir = tmp.path().join("data");
    let camera = fake_camera(tmp.path());
    let config = Config {
        wifi: None,
        brain: BrainConfig { url: brain.uri(), pair_token: None, ca_fingerprint: None },
        capture: CaptureConfig { camera_cmd: camera.clone(), ..CaptureConfig::default() },
        schedule: ScheduleConfig::default(),
        led: LedConfig::default(),
        gpio: GpioConfig::default(),
        paths: PathsConfig { data_dir: data_dir.clone(), sync_flag: tmp.path().join("unused") },
    };
    let led = RecordingLed::default();
    let led_states = led.states.clone();
    let factory: AudioSourceFactory = Box::new(|| Ok(Box::new(ToneSource::realtime()) as Box<dyn AudioSource>));
    let (_press_tx, presses) = mpsc::channel(1);
    let liveness = loopi_agent::liveness::Liveness::new();
    let deps = Deps {
        config,
        clock: Arc::new(SyncedClock),
        disk: Arc::new(FixedDisk::new(10.0)),
        transport: Arc::new(HttpTransport::new(reqwest::Client::new(), &brain.uri())),
        audio_factory: factory,
        led: Some(Box::new(led)),
        presses,
        probe: Arc::new(NoProbe),
        timings: Timings {
            heartbeat: Duration::from_millis(1500),
            upload_idle: Duration::from_millis(50),
            video_poll: Duration::from_millis(100),
            disk_check: Duration::from_millis(200),
            tick: Duration::from_millis(50),
            led_frame: Duration::from_millis(20),
            audio_retry: Duration::from_millis(100),
            audio_segment: Some(AUDIO_SEGMENT),
        },
        liveness: liveness.clone(),
    };
    let (stop_tx, stop_rx) = watch::channel(false);
    let agent = tokio::spawn(run(deps, stop_rx));

    tokio::time::sleep(Duration::from_millis(1300)).await;
    let before_pause = brain.received_requests().await.unwrap();
    assert!(uploads(&before_pause, "video") >= 2, "video segments are uploaded while recording");
    assert!(liveness.beats() >= 10, "the main loop beats the liveness marker every tick");
    assert!(liveness.age() < Duration::from_secs(1));
    let states = led_states.lock().unwrap().clone();
    assert!(states.contains(&true), "the LED was on while recording");
    assert_eq!(states.last(), Some(&true), "the LED is solid while recording");

    // The pause arrives with the heartbeat at ~1.5 s; partial segments are flushed and uploaded.
    tokio::time::sleep(Duration::from_millis(1200)).await;
    let after_pause = brain.received_requests().await.unwrap();
    let (video, audio) = (uploads(&after_pause, "video"), uploads(&after_pause, "audio"));
    assert!(audio >= 1, "audio segments are uploaded");
    tokio::time::sleep(Duration::from_millis(1000)).await;
    let later = brain.received_requests().await.unwrap();
    assert_eq!((uploads(&later, "video"), uploads(&later, "audio")), (video, audio), "nothing is recorded while paused");

    let sent = heartbeats(&later);
    assert!(sent.iter().any(|hb| hb["acked_command_ids"] == json!([PAUSE_ID])), "the pause command is acked");
    assert!(sent.last().unwrap()["paused"].as_bool().unwrap());
    let state: Value = serde_json::from_slice(&fs::read(data_dir.join("config/state.json")).unwrap()).unwrap();
    assert_eq!(state["paused"], json!(true), "pause survives a reboot");

    stop_tx.send(true).unwrap();
    agent.await.unwrap().unwrap();
    assert_eq!(led_states.lock().unwrap().last(), Some(&false));
    assert_eq!(fs::read_dir(data_dir.join("queue/items")).unwrap().count(), 0, "everything was uploaded");
    let leftovers = files_under(&data_dir.join("queue/incoming"));
    assert!(leftovers.is_empty(), "nothing stays in incoming/: {leftovers:?}");
    let final_requests = brain.received_requests().await.unwrap();
    let shortest_audio = final_requests
        .iter()
        .filter(|r| r.method.as_str() == "PUT" && header(r, "x-loopi-kind") == Some("audio"))
        .map(|r| {
            let at = |name: &str| DateTime::parse_from_rfc3339(header(r, name).unwrap()).unwrap();
            (at("x-loopi-end") - at("x-loopi-start")).to_std().unwrap()
        })
        .min()
        .expect("audio was uploaded");
    assert!(shortest_audio < AUDIO_SEGMENT, "the pause cut a partial audio segment ({shortest_audio:?}) that was uploaded");
    assert!(!stray_camera_processes(&camera), "no camera process outlives the agent");
    assert_eq!(violations.lock().unwrap().clone(), Vec::<String>::new(), "the mock brain saw contract violations");
}
