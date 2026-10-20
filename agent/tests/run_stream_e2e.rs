//! The whole agent as a network camera's recorder (P3c-2): ffmpeg (a test double) for the picture and the sound, no
//! LED, no button, against the same contract-checking mock brain as `run_e2e.rs`.
//!
//! The tests share one process, so they run one at a time (`SERIAL`): "no child process is left over" is then a
//! statement about the test that just ended, and the log they capture is one log.
use std::fs;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use chrono::DateTime;
use loopi_agent::config::{
    BrainConfig, CaptureConfig, CaptureSource, Config, GpioConfig, LedConfig, LedSource, PathsConfig, ScheduleConfig, Secret,
};
use loopi_agent::led::NullLed;
use loopi_agent::osinfo::OsInfo;
use loopi_agent::recorder::audio::AudioSourceFactory;
use loopi_agent::recorder::{stream, stream_audio};
use loopi_agent::run::{run, Deps, Timings};
use loopi_agent::sysinfo::NoProbe;
use loopi_agent::testing::{FixedDisk, SyncedClock};
use loopi_agent::transport::HttpTransport;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;
use wiremock::matchers::{method, path, path_regex};
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

const PASSWORD: &str = "S3cr3t";
const STREAM_URL: &str = "rtsp://192.0.2.10/cam";
const SET_AUDIO_ON_ID: &str = "01JA2B3C4D5E6F7G8H9J0KMNPQ";
const AUDIO_SEGMENT: Duration = Duration::from_secs(1);
const MAX_SEGMENT_SECONDS: i64 = 3600;

type Violations = Arc<Mutex<Vec<String>>>;

/// One test at a time: they count this process's child processes.
static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

// ---- everything the agent logs, in one buffer ----------------------------------------------------------------------

#[derive(Clone, Default)]
struct LogBuffer(Arc<Mutex<Vec<u8>>>);

impl io::Write for LogBuffer {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl LogBuffer {
    fn text(&self) -> String {
        String::from_utf8_lossy(&self.0.lock().unwrap()).into_owned()
    }
}

/// The first call installs a subscriber for the whole process (every level, every thread) that keeps its output here.
fn captured_log() -> LogBuffer {
    static LOG: OnceLock<LogBuffer> = OnceLock::new();
    LOG.get_or_init(|| {
        let buffer = LogBuffer::default();
        let writer = buffer.clone();
        let subscriber = tracing_subscriber::fmt()
            .with_max_level(tracing::Level::TRACE)
            .with_ansi(false)
            .with_writer(move || writer.clone())
            .finish();
        let _ = tracing::subscriber::set_global_default(subscriber);
        buffer
    })
    .clone()
}

// ---- the mock brain (as in run_e2e.rs) -----------------------------------------------------------------------------

fn header<'a>(request: &'a Request, name: &str) -> Option<&'a str> {
    request.headers.get(name).and_then(|v| v.to_str().ok())
}

fn puts<'a>(requests: &'a [Request], kind: &'a str) -> impl Iterator<Item = &'a Request> {
    requests.iter().filter(move |r| r.method.as_str() == "PUT" && header(r, "x-loopi-kind") == Some(kind))
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

/// A brain that accepts every upload and answers heartbeat number `n` (from 0) with `commands(n)`; each heartbeat is
/// checked against the `Heartbeat` schema.
async fn mount_brain(brain: &MockServer, violations: &Violations, commands: impl Fn(usize) -> Value + Send + Sync + 'static) {
    let put_log = violations.clone();
    Mock::given(method("PUT"))
        .and(path_regex(r"^/v1/items/[0-9A-Z]{26}$"))
        .respond_with(move |request: &Request| {
            ResponseTemplate::new(if record(&put_log, put_violations(request)) { 201 } else { 422 })
        })
        .mount(brain)
        .await;
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
            let commands = commands(calls.fetch_add(1, Ordering::SeqCst));
            ResponseTemplate::new(200).set_body_json(json!({ "commands": commands }))
        })
        .mount(brain)
        .await;
}

/// Poll the mock brain every 50 ms until `done` or `limit`; returns the requests seen last.
async fn wait_for(brain: &MockServer, limit: Duration, done: impl Fn(&[Request]) -> bool) -> Vec<Request> {
    let deadline = Instant::now() + limit;
    loop {
        let requests = brain.received_requests().await.unwrap();
        if done(&requests) || Instant::now() >= deadline {
            return requests;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

// ---- the camera, the sound and the agent ---------------------------------------------------------------------------

fn fake_ffmpeg(dir: &Path) -> String {
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/fake-ffmpeg.sh");
    let dst = dir.join("fake-ffmpeg.sh");
    fs::copy(fixture, &dst).unwrap();
    fs::set_permissions(&dst, fs::Permissions::from_mode(0o755)).unwrap();
    dst.to_string_lossy().into_owned()
}

/// The fake with `FAKE_FFMPEG_FAIL=1` in its environment: it prints a 401 line that quotes the address and exits 1.
fn failing_ffmpeg(dir: &Path, fake: &str) -> String {
    let wrapper = dir.join("fake-ffmpeg-fail.sh");
    fs::write(&wrapper, format!("#!/bin/sh\nFAKE_FFMPEG_FAIL=1 exec '{fake}' \"$@\"\n")).unwrap();
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o755)).unwrap();
    wrapper.to_string_lossy().into_owned()
}

fn stray_camera_processes(script: &str) -> bool {
    std::process::Command::new("pgrep").args(["-f", script]).output().unwrap().status.success()
}

/// Child processes of this process still alive (name and pid). The sound's ffmpeg double `exec`s into `cat`, so
/// `pgrep -f <script>` cannot see it; its parent is this process.
fn children_of_this_process() -> Vec<String> {
    let me = std::process::id();
    let Ok(entries) = fs::read_dir("/proc") else { return Vec::new() };
    entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let pid: u32 = entry.file_name().to_str()?.parse().ok()?;
            let stat = fs::read_to_string(entry.path().join("stat")).ok()?;
            // "pid (comm) state ppid ...": comm may hold spaces and brackets, so split after the last ')'.
            let (head, tail) = stat.rsplit_once(')')?;
            let parent: u32 = tail.split_whitespace().nth(1)?.parse().ok()?;
            (parent == me).then(|| format!("{} (pid {pid})", head.split_once('(').map_or("?", |(_, comm)| comm)))
        })
        .collect()
}

struct StreamAgent {
    stop: watch::Sender<bool>,
    task: JoinHandle<anyhow::Result<()>>,
    _presses: mpsc::Sender<loopi_agent::button::Press>,
}

impl StreamAgent {
    async fn stop(self) {
        self.stop.send(true).unwrap();
        self.task.await.unwrap().unwrap();
    }
}

fn stream_config(brain: &MockServer, tmp: &Path, camera: &str, audio_enabled: bool) -> Config {
    Config {
        wifi: None,
        brain: BrainConfig { url: brain.uri(), pair_token: None, ca_fingerprint: None },
        capture: CaptureConfig {
            source: CaptureSource::Stream,
            camera_cmd: camera.to_string(),
            stream_url: STREAM_URL.into(),
            stream_username: "admin".into(),
            stream_password: Secret(PASSWORD.into()),
            audio_enabled,
            ..CaptureConfig::default()
        },
        schedule: ScheduleConfig::default(),
        led: LedConfig { source: LedSource::None, ..LedConfig::default() },
        gpio: GpioConfig::default(),
        paths: PathsConfig { data_dir: tmp.join("data"), sync_flag: tmp.join("unused") },
    }
}

fn start_agent(brain: &MockServer, config: Config, audio_factory: AudioSourceFactory, audio_retry: Duration) -> StreamAgent {
    let (press_tx, presses) = mpsc::channel(1);
    let deps = Deps {
        config,
        clock: Arc::new(SyncedClock),
        disk: Arc::new(FixedDisk::new(10.0)),
        transport: Arc::new(HttpTransport::new(reqwest::Client::new(), &brain.uri())),
        audio_factory,
        led: Some(Box::new(NullLed)),
        presses,
        probe: Arc::new(NoProbe),
        timings: Timings {
            heartbeat: Duration::from_millis(100),
            upload_idle: Duration::from_millis(50),
            video_poll: Duration::from_millis(100),
            disk_check: Duration::from_millis(200),
            tick: Duration::from_millis(50),
            led_frame: Duration::from_millis(20),
            audio_retry,
            audio_segment: Some(AUDIO_SEGMENT),
        },
        liveness: loopi_agent::liveness::Liveness::new(),
        os: OsInfo::default(),
        ota: None,
    };
    let (stop, stop_rx) = watch::channel(false);
    StreamAgent { stop, task: tokio::spawn(run(deps, stop_rx)), _presses: press_tx }
}

fn sound_factory(config: &Config, ffmpeg: &str) -> AudioSourceFactory {
    stream_audio::factory(ffmpeg.to_string(), stream::audio_input(&config.capture))
}

fn assert_no_secret(text: &str, what: &str) {
    for needle in [PASSWORD, "rtsp://"] {
        assert!(!text.contains(needle), "{what} contains {needle:?}");
    }
}

// ---- tests ---------------------------------------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread")]
async fn a_network_camera_records_and_uploads_video_and_sound() {
    let _one_at_a_time = SERIAL.lock().await;
    let log = captured_log();
    let brain = MockServer::start().await;
    let violations: Violations = Arc::default();
    mount_brain(&brain, &violations, |_| json!([])).await;

    let tmp = tempfile::tempdir().unwrap();
    let ffmpeg = fake_ffmpeg(tmp.path());
    let config = stream_config(&brain, tmp.path(), &ffmpeg, true);
    let factory = sound_factory(&config, &ffmpeg);
    let agent = start_agent(&brain, config, factory, Duration::from_millis(100));

    let seen = wait_for(&brain, Duration::from_secs(10), |requests| {
        puts(requests, "video").next().is_some()
            && puts(requests, "audio").next().is_some()
            && heartbeats(requests).iter().any(|hb| hb["recording_video"] == json!(true))
    })
    .await;
    let running = children_of_this_process();
    agent.stop().await;

    assert_eq!(running.len(), 2, "one ffmpeg for the picture and one for the sound ran: {running:?}");
    let seen = if puts(&seen, "audio").next().is_some() { seen } else { brain.received_requests().await.unwrap() };
    let videos: Vec<&Request> = puts(&seen, "video").collect();
    assert!(!videos.is_empty(), "a video segment reached the brain");
    assert!(videos.iter().all(|r| header(r, "content-type") == Some("video/h264")), "video goes up as raw H.264");
    let sounds: Vec<&Request> = puts(&seen, "audio").collect();
    assert!(!sounds.is_empty(), "a sound segment reached the brain");
    assert!(sounds.iter().all(|r| header(r, "content-type") == Some("audio/ogg")), "sound goes up as Ogg");
    assert!(sounds.iter().all(|r| r.body.starts_with(b"OggS")), "the sound is Ogg Opus, encoded from ffmpeg's PCM");
    let beats = heartbeats(&seen);
    assert!(!beats.is_empty());
    assert!(beats.iter().any(|hb| hb["recording_video"] == json!(true)), "a heartbeat reports the camera recording");

    assert!(!stray_camera_processes(&ffmpeg), "no ffmpeg for the picture outlives the agent");
    assert_eq!(children_of_this_process(), Vec::<String>::new(), "no ffmpeg (picture or sound) outlives the agent");
    assert_no_secret(&log.text(), "the log");
    assert_eq!(violations.lock().unwrap().clone(), Vec::<String>::new(), "the mock brain saw contract violations");
}

#[tokio::test(flavor = "multi_thread")]
async fn the_password_never_reaches_the_log_or_the_heartbeat() {
    let _one_at_a_time = SERIAL.lock().await;
    let log = captured_log();
    let brain = MockServer::start().await;
    let violations: Violations = Arc::default();
    mount_brain(&brain, &violations, |_| json!([])).await;

    let tmp = tempfile::tempdir().unwrap();
    let fake = fake_ffmpeg(tmp.path());
    // Both the picture's and the sound's ffmpeg fail at once and print the address with the password.
    let ffmpeg = failing_ffmpeg(tmp.path(), &fake);
    let config = stream_config(&brain, tmp.path(), &ffmpeg, true);
    let factory = sound_factory(&config, &ffmpeg);
    // The sound's retry is slow on purpose: its "microphone: ..." message shares `last_error` with the camera's, and
    // retrying every 100 ms would keep overwriting the camera's before a heartbeat could carry it.
    let agent = start_agent(&brain, config, factory, Duration::from_secs(10));

    let phrase = "refused the user name or password";
    let seen = wait_for(&brain, Duration::from_secs(10), |requests| {
        let beats = heartbeats(requests);
        beats.iter().any(|hb| hb["camera_failures"].as_u64().is_some_and(|n| n >= 1))
            && beats.iter().any(|hb| hb["last_error"].as_str().is_some_and(|e| e.contains(phrase)))
    })
    .await;
    agent.stop().await;

    let seen = if heartbeats(&seen).is_empty() { brain.received_requests().await.unwrap() } else { seen };
    let beats = heartbeats(&seen);
    assert!(beats.iter().any(|hb| hb["camera_failures"].as_u64().is_some_and(|n| n >= 1)), "a heartbeat counts the failure");
    assert!(
        beats.iter().any(|hb| hb["last_error"].as_str().is_some_and(|e| e.contains(phrase))),
        "last_error says why in words: {:?}",
        beats.iter().filter_map(|hb| hb["last_error"].as_str()).collect::<Vec<_>>()
    );
    for request in seen.iter().filter(|r| r.url.path() == "/v1/heartbeat") {
        assert_no_secret(&String::from_utf8_lossy(&request.body), "a heartbeat body");
    }
    let log = log.text();
    assert!(log.contains("camera problem"), "the failure was logged (so the log check below looks at something)");
    assert_no_secret(&log, "the log");
    assert!(!stray_camera_processes(&fake), "no ffmpeg outlives the agent");
    assert_eq!(children_of_this_process(), Vec::<String>::new(), "no ffmpeg outlives the agent");
    assert_eq!(violations.lock().unwrap().clone(), Vec::<String>::new(), "the mock brain saw contract violations");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_camera_without_sound_never_opens_it_even_when_asked() {
    let _one_at_a_time = SERIAL.lock().await;
    let log = captured_log();
    let brain = MockServer::start().await;
    let violations: Violations = Arc::default();
    // The first heartbeat is answered with "turn the sound on".
    mount_brain(&brain, &violations, |n| match n {
        0 => json!([{"id": SET_AUDIO_ON_ID, "type": "set_config", "payload": {"audio_enabled": true}}]),
        _ => json!([]),
    })
    .await;

    let tmp = tempfile::tempdir().unwrap();
    let ffmpeg = fake_ffmpeg(tmp.path());
    let config = stream_config(&brain, tmp.path(), &ffmpeg, false);
    let opened = Arc::new(AtomicUsize::new(0));
    let counted = opened.clone();
    let real = sound_factory(&config, &ffmpeg);
    let factory: AudioSourceFactory = Box::new(move || {
        counted.fetch_add(1, Ordering::SeqCst);
        real()
    });
    let agent = start_agent(&brain, config, factory, Duration::from_millis(100));

    // The same 1 s sound segments that reached the brain within a few seconds in the first test.
    tokio::time::sleep(Duration::from_secs(4)).await;
    let seen = brain.received_requests().await.unwrap();
    let children_while_running = children_of_this_process();
    agent.stop().await;

    assert!(puts(&seen, "video").next().is_some(), "the agent ran and recorded video meanwhile");
    assert_eq!(puts(&seen, "audio").count(), 0, "no sound segment within 4 s");
    let beats = heartbeats(&seen);
    assert!(beats.len() >= 5, "heartbeats kept coming: {}", beats.len());
    assert!(beats.iter().all(|hb| hb["audio_enabled"] == json!(false)), "audio_enabled stays false in every heartbeat");
    assert!(
        beats.iter().any(|hb| hb["acked_command_ids"] == json!([SET_AUDIO_ON_ID])),
        "the command was received and acked, then kept off by capture.audio_enabled = false"
    );
    assert_eq!(opened.load(Ordering::SeqCst), 0, "the sound was never opened");
    assert_eq!(children_while_running.len(), 1, "only the picture's ffmpeg ran: {children_while_running:?}");
    assert_eq!(children_of_this_process(), Vec::<String>::new(), "no ffmpeg outlives the agent");
    assert_no_secret(&log.text(), "the log");
    assert_eq!(violations.lock().unwrap().clone(), Vec::<String>::new(), "the mock brain saw contract violations");
}
