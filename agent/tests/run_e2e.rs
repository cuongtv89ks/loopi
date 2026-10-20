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
use loopi_agent::config::{BrainConfig, CaptureConfig, ClockConfig, Config, GpioConfig, LedConfig, PathsConfig, ScheduleConfig};
use loopi_agent::recorder::audio::{AudioSource, AudioSourceFactory};
use loopi_agent::osinfo::OsInfo;
use loopi_agent::ota::task::OtaTimings;
use loopi_agent::ota::{Slot, Variant};
use loopi_agent::run::{run, Deps, OtaSystem, Timings, UPDATES_UNSUPPORTED};
use loopi_agent::sysinfo::NoProbe;
use loopi_agent::testing::{FakeFetcher, FakeRauc, FakeRebooter, FixedDisk, GatedFetcher, RecordingLed, SyncedClock, ToneSource};
use loopi_agent::transport::HttpTransport;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tokio::sync::{mpsc, watch};
use wiremock::matchers::{method, path, path_regex};
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

const PAUSE_ID: &str = "01JA2B3C4D5E6F7G8H9J0KMNPQ";
const UPDATE_ID: &str = "01JA2B3C4D5E6F7G8H9J0KMNPR";
const SECOND_UPDATE_ID: &str = "01JA2B3C4D5E6F7G8H9J0KMNPS";
const BUNDLE_BODY: &[u8] = b"a signed bundle, as far as the fake RAUC cares";
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

/// Largest |sample| of an uploaded Ogg Opus segment, decoded at 16 kHz.
fn decoded_peak(ogg: &[u8]) -> i32 {
    let mut reader = ogg::reading::PacketReader::new(std::io::Cursor::new(ogg));
    let mut decoder = opus::Decoder::new(16_000, opus::Channels::Mono).unwrap();
    let mut pcm = [0i16; 320];
    let mut peak = 0;
    // The first two packets are OpusHead and OpusTags.
    while let Some(packet) = reader.read_packet().unwrap() {
        if packet.data.starts_with(b"Opus") {
            continue;
        }
        let samples = decoder.decode(&packet.data, &mut pcm, false).unwrap();
        peak = pcm[..samples].iter().map(|s| i32::from(*s).abs()).fold(peak, i32::max);
    }
    peak
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
        clock: ClockConfig::default(),
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
        os: OsInfo::default(),
        ota: None,
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
    let loudest_audio = final_requests
        .iter()
        .filter(|r| r.method.as_str() == "PUT" && header(r, "x-loopi-kind") == Some("audio"))
        .map(|r| decoded_peak(&r.body))
        .max()
        .expect("audio was uploaded");
    // The tone "microphone" peaks at 8000 (-12 dBFS); the default 20 dB gain drives it into the -1 dBFS limiter.
    assert!(loudest_audio > 20_000, "the configured gain reached the uploaded audio (decoded peak {loudest_audio})");
    assert!(!stray_camera_processes(&camera), "no camera process outlives the agent");
    assert_eq!(violations.lock().unwrap().clone(), Vec::<String>::new(), "the mock brain saw contract violations");
}

/// A device whose kernel command line has no `rauc.slot=` (the sd-image era) cannot install a bundle: the update is
/// refused with a clear reason that reaches the brain, and the command is still acked so it is not re-sent.
#[tokio::test]
async fn an_update_on_a_system_without_ota_support_is_reported_failed() {
    let brain = MockServer::start().await;
    let violations: Violations = Arc::default();
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
            let update = json!({"id": UPDATE_ID, "type": "update", "payload": {
                "bundle": "loopi-dev-2026.10.21-a1b2c3d.raucb", "version": "2026.10.21-a1b2c3d",
                "variant": "dev", "sha256": "ab".repeat(32), "size": 75_000_000}});
            let commands = if calls.fetch_add(1, Ordering::SeqCst) == 0 { json!([update]) } else { json!([]) };
            ResponseTemplate::new(200).set_body_json(json!({ "commands": commands }))
        })
        .mount(&brain)
        .await;

    let tmp = tempfile::tempdir().unwrap();
    let data_dir = tmp.path().join("data");
    // Paused: nothing records, so the test needs no camera and no microphone.
    fs::create_dir_all(data_dir.join("config")).unwrap();
    fs::write(data_dir.join("config/state.json"), br#"{"paused":true,"audio_enabled":true}"#).unwrap();
    let config = Config {
        wifi: None,
        brain: BrainConfig { url: brain.uri(), pair_token: None, ca_fingerprint: None },
        capture: CaptureConfig::default(),
        schedule: ScheduleConfig::default(),
        led: LedConfig::default(),
        gpio: GpioConfig::default(),
        clock: ClockConfig::default(),
        paths: PathsConfig { data_dir: data_dir.clone(), sync_flag: tmp.path().join("unused") },
    };
    let factory: AudioSourceFactory = Box::new(|| Ok(Box::new(ToneSource::realtime()) as Box<dyn AudioSource>));
    let (_press_tx, presses) = mpsc::channel(1);
    let deps = Deps {
        config,
        clock: Arc::new(SyncedClock),
        disk: Arc::new(FixedDisk::new(10.0)),
        transport: Arc::new(HttpTransport::new(reqwest::Client::new(), &brain.uri())),
        audio_factory: factory,
        led: Some(Box::new(RecordingLed::default())),
        presses,
        probe: Arc::new(NoProbe),
        timings: Timings {
            heartbeat: Duration::from_millis(100),
            upload_idle: Duration::from_millis(50),
            video_poll: Duration::from_millis(100),
            disk_check: Duration::from_millis(200),
            tick: Duration::from_millis(50),
            led_frame: Duration::from_millis(20),
            audio_retry: Duration::from_millis(100),
            audio_segment: Some(AUDIO_SEGMENT),
        },
        liveness: loopi_agent::liveness::Liveness::new(),
        // A release file but no `rauc.slot=`: the version is known, the boot slot is not.
        os: OsInfo { version: Some("2026.10.14-0000000".into()), variant: Some(Variant::Dev), slot: None },
        ota: None,
    };
    let (stop_tx, stop_rx) = watch::channel(false);
    let agent = tokio::spawn(run(deps, stop_rx));

    let mut sent = Vec::new();
    for _ in 0..100 {
        sent = heartbeats(&brain.received_requests().await.unwrap());
        let acked = sent.iter().any(|hb| hb["acked_command_ids"] == json!([UPDATE_ID]));
        if acked && sent.iter().any(|hb| hb.get("ota").is_some()) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    stop_tx.send(true).unwrap();
    agent.await.unwrap().unwrap();

    let reported = sent.iter().find(|hb| hb.get("ota").is_some()).expect("a heartbeat carries the ota report")["ota"].clone();
    assert_eq!(reported["state"], "failed");
    assert_eq!(reported["error"], json!(UPDATES_UNSUPPORTED));
    assert_eq!((reported["command_id"].as_str(), reported["version"].as_str()), (Some(UPDATE_ID), Some("2026.10.21-a1b2c3d")));
    assert!(sent.iter().any(|hb| hb["acked_command_ids"] == json!([UPDATE_ID])), "the refused update is still acked");
    let first = &sent[0];
    assert_eq!((first["os_version"].as_str(), first["os_variant"].as_str()), (Some("2026.10.14-0000000"), Some("dev")));
    assert!(first.get("boot_slot").is_none(), "an unknown slot is left out, not guessed");
    assert_eq!(violations.lock().unwrap().clone(), Vec::<String>::new(), "the mock brain saw contract violations");
}

/// An `update` command on a device that has A/B slots runs the whole install: download, checksum, `rauc install`, the
/// "rebooting" report reaches the brain, then the trial reboot. A paused device with audio off stays that way (spec
/// §7.4: an update never touches the privacy state).
#[tokio::test]
async fn an_update_is_installed_reported_and_followed_by_a_trial_reboot() {
    const BUNDLE: &str = "loopi-dev-2026.10.21-a1b2c3d.raucb";
    let brain = MockServer::start().await;
    let violations: Violations = Arc::default();
    let heartbeat_log = violations.clone();
    let calls = AtomicUsize::new(0);
    let sha256 = hex::encode(Sha256::digest(BUNDLE_BODY));
    Mock::given(method("POST"))
        .and(path("/v1/heartbeat"))
        .respond_with(move |request: &Request| {
            let body: Value = serde_json::from_slice(&request.body).unwrap_or(Value::Null);
            let errors = schema_errors("Heartbeat", &body);
            if !record(&heartbeat_log, errors.into_iter().map(|e| format!("heartbeat: {e}")).collect()) {
                return ResponseTemplate::new(422);
            }
            let update = json!({"id": UPDATE_ID, "type": "update", "payload": {
                "bundle": BUNDLE, "version": "2026.10.21-a1b2c3d", "variant": "dev",
                "sha256": sha256, "size": BUNDLE_BODY.len()}});
            let commands = if calls.fetch_add(1, Ordering::SeqCst) == 0 { json!([update]) } else { json!([]) };
            ResponseTemplate::new(200).set_body_json(json!({ "commands": commands }))
        })
        .mount(&brain)
        .await;

    let tmp = tempfile::tempdir().unwrap();
    let data_dir = tmp.path().join("data");
    // Paused with audio off: nothing records (no camera or microphone needed) and the update must leave it so.
    let state_file = data_dir.join("config/state.json");
    fs::create_dir_all(state_file.parent().unwrap()).unwrap();
    let saved_state = br#"{"paused":true,"audio_enabled":false}"#;
    fs::write(&state_file, saved_state).unwrap();
    let config = Config {
        wifi: None,
        brain: BrainConfig { url: brain.uri(), pair_token: None, ca_fingerprint: None },
        capture: CaptureConfig::default(),
        schedule: ScheduleConfig::default(),
        led: LedConfig::default(),
        gpio: GpioConfig::default(),
        clock: ClockConfig::default(),
        paths: PathsConfig { data_dir: data_dir.clone(), sync_flag: tmp.path().join("unused") },
    };
    let (rauc, rebooter) = (Arc::new(FakeRauc::default()), Arc::new(FakeRebooter::default()));
    let fetcher = Arc::new(FakeFetcher::new(BUNDLE_BODY));
    let factory: AudioSourceFactory = Box::new(|| Ok(Box::new(ToneSource::realtime()) as Box<dyn AudioSource>));
    let (_press_tx, presses) = mpsc::channel(1);
    let deps = Deps {
        config,
        clock: Arc::new(SyncedClock),
        disk: Arc::new(FixedDisk::new(10.0)),
        transport: Arc::new(HttpTransport::new(reqwest::Client::new(), &brain.uri())),
        audio_factory: factory,
        led: Some(Box::new(RecordingLed::default())),
        presses,
        probe: Arc::new(NoProbe),
        timings: Timings {
            heartbeat: Duration::from_millis(100),
            upload_idle: Duration::from_millis(50),
            video_poll: Duration::from_millis(100),
            disk_check: Duration::from_millis(200),
            tick: Duration::from_millis(50),
            led_frame: Duration::from_millis(20),
            audio_retry: Duration::from_millis(100),
            audio_segment: Some(AUDIO_SEGMENT),
        },
        liveness: loopi_agent::liveness::Liveness::new(),
        os: OsInfo { version: Some("2026.10.14-0000000".into()), variant: Some(Variant::Dev), slot: Some(Slot::A) },
        ota: Some(OtaSystem {
            rauc: rauc.clone(),
            rebooter: rebooter.clone(),
            fetcher: fetcher.clone(),
            timings: OtaTimings {
                retry_first: Duration::from_millis(1),
                retry_max: Duration::from_millis(2),
                attempts: 2,
                reboot_report_wait: Duration::from_millis(10),
            },
        }),
    };
    let (stop_tx, stop_rx) = watch::channel(false);
    let agent = tokio::spawn(run(deps, stop_rx));

    let rebooting = |hb: &Value| hb["ota"]["state"] == "rebooting";
    let mut sent = Vec::new();
    for _ in 0..100 {
        sent = heartbeats(&brain.received_requests().await.unwrap());
        if sent.iter().any(rebooting) && *rebooter.calls.lock().unwrap() == 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    // One more heartbeat after the trial reboot was requested: the brain keeps hearing "rebooting" until the reboot.
    let before = sent.len();
    for _ in 0..100 {
        sent = heartbeats(&brain.received_requests().await.unwrap());
        if sent.len() > before {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    stop_tx.send(true).unwrap();
    agent.await.unwrap().unwrap();

    let report = sent.iter().find(|hb| rebooting(hb)).expect("a heartbeat reports the rebooting state")["ota"].clone();
    assert_eq!((report["command_id"].as_str(), report["version"].as_str()), (Some(UPDATE_ID), Some("2026.10.21-a1b2c3d")));
    assert!(report["error"].is_null(), "{report}");
    assert_eq!(*rebooter.calls.lock().unwrap(), 1, "exactly one trial reboot");
    assert_eq!(*fetcher.calls.lock().unwrap(), 1);
    assert_eq!(*rauc.calls.lock().unwrap(), vec![format!("install {BUNDLE} exists=true")]);
    assert!(sent.iter().any(|hb| hb["acked_command_ids"] == json!([UPDATE_ID])), "the update is acked");
    assert_eq!(sent[0]["boot_slot"], "A");
    let pending: Value = serde_json::from_slice(&fs::read(data_dir.join("ota/pending.json")).unwrap()).unwrap();
    assert_eq!((pending["phase"].as_str(), pending["target_slot"].as_str()), (Some("rebooting"), Some("B")));
    assert_eq!(files_under(&data_dir.join("ota")), vec![data_dir.join("ota/pending.json")], "no bundle is left on the card");

    // Privacy (spec §7.4): the update did not unpause the device or switch audio back on, before or after it ran.
    for hb in &sent {
        assert_eq!((hb["paused"].as_bool(), hb["audio_enabled"].as_bool()), (Some(true), Some(false)), "{hb}");
        assert_eq!((hb["recording_video"].as_bool(), hb["recording_audio"].as_bool()), (Some(false), Some(false)), "{hb}");
    }
    assert_eq!(fs::read(&state_file).unwrap(), saved_state, "the saved pause state is untouched");
    assert_eq!(violations.lock().unwrap().clone(), Vec::<String>::new(), "the mock brain saw contract violations");
}

/// A trial boot that cannot reach the brain keeps waiting for its first heartbeat; it is not confirmed (RAUC is never
/// told the new slot is good) and it must not stop the agent from shutting down.
#[tokio::test]
async fn an_unconfirmed_trial_is_not_marked_good_and_does_not_block_shutdown() {
    unconfirmed_trial_waits_for_a_heartbeat(
        br#"{"command_id":null,"bundle":null,"version":"2026.10.21-a1b2c3d","target_slot":"B","phase":"rebooting","error":null}"#,
    )
    .await;
}

/// The same trial when the previous agent recorded the reboot as failed (systemctl died with the service it stopped):
/// slot B and its version are running, so it is a trial too, stored as a pending one before the heartbeat.
#[tokio::test]
async fn a_trial_recorded_as_a_failed_reboot_is_a_trial_all_the_same() {
    unconfirmed_trial_waits_for_a_heartbeat(
        br#"{"command_id":null,"bundle":null,"version":"2026.10.21-a1b2c3d","target_slot":"B","phase":"failed","error":"reboot failed: systemctl reboot failed (signal: 15 (SIGTERM))"}"#,
    )
    .await;
}

async fn unconfirmed_trial_waits_for_a_heartbeat(pending_json: &[u8]) {
    let brain = MockServer::start().await;
    let violations: Violations = Arc::default();
    let heartbeat_log = violations.clone();
    Mock::given(method("POST"))
        .and(path("/v1/heartbeat"))
        .respond_with(move |request: &Request| {
            let body: Value = serde_json::from_slice(&request.body).unwrap_or(Value::Null);
            let errors = schema_errors("Heartbeat", &body);
            record(&heartbeat_log, errors.into_iter().map(|e| format!("heartbeat: {e}")).collect());
            ResponseTemplate::new(503)
        })
        .mount(&brain)
        .await;

    let tmp = tempfile::tempdir().unwrap();
    let data_dir = tmp.path().join("data");
    fs::create_dir_all(data_dir.join("config")).unwrap();
    fs::write(data_dir.join("config/state.json"), br#"{"paused":true,"audio_enabled":false}"#).unwrap();
    // What an install leaves behind just before the trial reboot (the shape migrate-to-ab.sh also writes).
    fs::create_dir_all(data_dir.join("ota")).unwrap();
    fs::write(data_dir.join("ota/pending.json"), pending_json).unwrap();
    let config = Config {
        wifi: None,
        brain: BrainConfig { url: brain.uri(), pair_token: None, ca_fingerprint: None },
        capture: CaptureConfig::default(),
        schedule: ScheduleConfig::default(),
        led: LedConfig::default(),
        gpio: GpioConfig::default(),
        clock: ClockConfig::default(),
        paths: PathsConfig { data_dir: data_dir.clone(), sync_flag: tmp.path().join("unused") },
    };
    let rauc = Arc::new(FakeRauc::default());
    let factory: AudioSourceFactory = Box::new(|| Ok(Box::new(ToneSource::realtime()) as Box<dyn AudioSource>));
    let (_press_tx, presses) = mpsc::channel(1);
    let deps = Deps {
        config,
        clock: Arc::new(SyncedClock),
        disk: Arc::new(FixedDisk::new(10.0)),
        transport: Arc::new(HttpTransport::new(reqwest::Client::new(), &brain.uri())),
        audio_factory: factory,
        led: Some(Box::new(RecordingLed::default())),
        presses,
        probe: Arc::new(NoProbe),
        timings: Timings {
            heartbeat: Duration::from_millis(100),
            upload_idle: Duration::from_millis(50),
            video_poll: Duration::from_millis(100),
            disk_check: Duration::from_millis(200),
            tick: Duration::from_millis(50),
            led_frame: Duration::from_millis(20),
            audio_retry: Duration::from_millis(100),
            audio_segment: Some(AUDIO_SEGMENT),
        },
        liveness: loopi_agent::liveness::Liveness::new(),
        os: OsInfo { version: Some("2026.10.21-a1b2c3d".into()), variant: Some(Variant::Dev), slot: Some(Slot::B) },
        ota: Some(OtaSystem {
            rauc: rauc.clone(),
            rebooter: Arc::new(FakeRebooter::default()),
            fetcher: Arc::new(FakeFetcher::new(b"unused")),
            timings: OtaTimings::default(),
        }),
    };
    let (stop_tx, stop_rx) = watch::channel(false);
    let agent = tokio::spawn(run(deps, stop_rx));

    let trying = |hb: &Value| hb["ota"]["state"] == "trying";
    let mut sent = Vec::new();
    for _ in 0..100 {
        sent = heartbeats(&brain.received_requests().await.unwrap());
        if sent.iter().any(trying) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    stop_tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(10), agent)
        .await
        .expect("the agent stops although the trial is still waiting for a heartbeat")
        .unwrap()
        .unwrap();

    let report = sent.iter().find(|hb| trying(hb)).expect("the brain is told the trial is running")["ota"].clone();
    assert_eq!((report["version"].as_str(), report.get("command_id")), (Some("2026.10.21-a1b2c3d"), None));
    assert!(rauc.calls.lock().unwrap().is_empty(), "no heartbeat got through: the new slot is not marked good");
    let pending: Value = serde_json::from_slice(&fs::read(data_dir.join("ota/pending.json")).unwrap()).unwrap();
    assert_eq!(pending["phase"], "rebooting", "the trial is still unconfirmed on disk");
    assert_eq!(violations.lock().unwrap().clone(), Vec::<String>::new(), "the mock brain saw contract violations");
}

/// Design §6.1: a second `update` while one runs is acked but ignored; the running update keeps reporting and the
/// second one is never started, not even after the first has failed or finished.
#[tokio::test]
async fn a_second_update_during_a_download_is_acked_noted_and_never_started() {
    const BUNDLE: &str = "loopi-dev-2026.10.21-a1b2c3d.raucb";
    let brain = MockServer::start().await;
    let violations: Violations = Arc::default();
    let heartbeat_log = violations.clone();
    let calls = AtomicUsize::new(0);
    let sha256 = hex::encode(Sha256::digest(BUNDLE_BODY));
    Mock::given(method("POST"))
        .and(path("/v1/heartbeat"))
        .respond_with(move |request: &Request| {
            let body: Value = serde_json::from_slice(&request.body).unwrap_or(Value::Null);
            let errors = schema_errors("Heartbeat", &body);
            if !record(&heartbeat_log, errors.into_iter().map(|e| format!("heartbeat: {e}")).collect()) {
                return ResponseTemplate::new(422);
            }
            let update = |id: &str, version: &str| json!({"id": id, "type": "update", "payload": {
                "bundle": format!("loopi-dev-{version}.raucb"), "version": version, "variant": "dev",
                "sha256": sha256, "size": BUNDLE_BODY.len()}});
            let commands = match calls.fetch_add(1, Ordering::SeqCst) {
                0 => json!([update(UPDATE_ID, "2026.10.21-a1b2c3d")]),
                1 => json!([update(SECOND_UPDATE_ID, "2026.10.22-bbbbbbb")]),
                _ => json!([]),
            };
            ResponseTemplate::new(200).set_body_json(json!({ "commands": commands }))
        })
        .mount(&brain)
        .await;

    let tmp = tempfile::tempdir().unwrap();
    let data_dir = tmp.path().join("data");
    fs::create_dir_all(data_dir.join("config")).unwrap();
    fs::write(data_dir.join("config/state.json"), br#"{"paused":true,"audio_enabled":false}"#).unwrap();
    let config = Config {
        wifi: None,
        brain: BrainConfig { url: brain.uri(), pair_token: None, ca_fingerprint: None },
        capture: CaptureConfig::default(),
        schedule: ScheduleConfig::default(),
        led: LedConfig::default(),
        gpio: GpioConfig::default(),
        clock: ClockConfig::default(),
        paths: PathsConfig { data_dir: data_dir.clone(), sync_flag: tmp.path().join("unused") },
    };
    let (rauc, rebooter) = (Arc::new(FakeRauc::default()), Arc::new(FakeRebooter::default()));
    let fetcher = Arc::new(GatedFetcher::new(BUNDLE_BODY));
    let factory: AudioSourceFactory = Box::new(|| Ok(Box::new(ToneSource::realtime()) as Box<dyn AudioSource>));
    let (_press_tx, presses) = mpsc::channel(1);
    let deps = Deps {
        config,
        clock: Arc::new(SyncedClock),
        disk: Arc::new(FixedDisk::new(10.0)),
        transport: Arc::new(HttpTransport::new(reqwest::Client::new(), &brain.uri())),
        audio_factory: factory,
        led: Some(Box::new(RecordingLed::default())),
        presses,
        probe: Arc::new(NoProbe),
        timings: Timings {
            heartbeat: Duration::from_millis(100),
            upload_idle: Duration::from_millis(50),
            video_poll: Duration::from_millis(100),
            disk_check: Duration::from_millis(200),
            tick: Duration::from_millis(50),
            led_frame: Duration::from_millis(20),
            audio_retry: Duration::from_millis(100),
            audio_segment: Some(AUDIO_SEGMENT),
        },
        liveness: loopi_agent::liveness::Liveness::new(),
        os: OsInfo { version: Some("2026.10.14-0000000".into()), variant: Some(Variant::Dev), slot: Some(Slot::A) },
        ota: Some(OtaSystem {
            rauc: rauc.clone(),
            rebooter: rebooter.clone(),
            fetcher: fetcher.clone(),
            timings: OtaTimings {
                retry_first: Duration::from_millis(1),
                retry_max: Duration::from_millis(2),
                attempts: 2,
                reboot_report_wait: Duration::from_millis(10),
            },
        }),
    };
    let (stop_tx, stop_rx) = watch::channel(false);
    let agent = tokio::spawn(run(deps, stop_rx));

    // The first update is inside the fetcher, which holds it there; the second one arrives meanwhile.
    tokio::time::timeout(Duration::from_secs(10), fetcher.entered.notified()).await.expect("the first update is downloading");
    let noted = |hb: &Value| {
        hb["last_error"].as_str().is_some_and(|e| e.contains(&format!("update {SECOND_UPDATE_ID} ignored: another update is in progress")))
    };
    let mut sent = Vec::new();
    for _ in 0..100 {
        sent = heartbeats(&brain.received_requests().await.unwrap());
        if sent.iter().any(noted) && sent.iter().any(|hb| hb["acked_command_ids"] == json!([SECOND_UPDATE_ID])) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(sent.iter().any(noted), "the ignored update is noted in last_error: {:?}", sent.last());
    assert!(sent.iter().any(|hb| hb["acked_command_ids"] == json!([SECOND_UPDATE_ID])), "the ignored update is still acked");
    let during = sent.iter().rev().find_map(|hb| hb.get("ota")).expect("the running update keeps reporting");
    assert_eq!((during["command_id"].as_str(), during["state"].as_str()), (Some(UPDATE_ID), Some("downloading")));
    assert_eq!(*fetcher.inner.calls.lock().unwrap(), 0, "nothing was fetched yet: the gate is closed");

    // Let the first update finish; the second must still not start.
    fetcher.release.notify_one();
    let rebooting = |hb: &Value| hb["ota"]["state"] == "rebooting";
    for _ in 0..100 {
        sent = heartbeats(&brain.received_requests().await.unwrap());
        if sent.iter().any(rebooting) && *rebooter.calls.lock().unwrap() == 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let before = sent.len();
    for _ in 0..100 {
        sent = heartbeats(&brain.received_requests().await.unwrap());
        if sent.len() >= before + 2 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    stop_tx.send(true).unwrap();
    agent.await.unwrap().unwrap();

    assert!(sent.iter().any(rebooting), "the first update reached its trial reboot");
    assert_eq!(*rebooter.calls.lock().unwrap(), 1);
    assert_eq!(*fetcher.inner.calls.lock().unwrap(), 1, "only the first update was ever fetched");
    assert_eq!(*rauc.calls.lock().unwrap(), vec![format!("install {BUNDLE} exists=true")]);
    for hb in &sent {
        if let Some(ota) = hb.get("ota") {
            assert_eq!(ota["command_id"], UPDATE_ID, "the second update never shows up in the report: {hb}");
        }
    }
    assert_eq!(violations.lock().unwrap().clone(), Vec::<String>::new(), "the mock brain saw contract violations");
}
