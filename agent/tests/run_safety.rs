//! `run()` privacy and robustness guards, with a scripted brain (`FakeTransport`), a fake camera
//! that records its PID, and a tone "microphone" that reports whether it is open.
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use loopi_agent::config::{BrainConfig, CaptureConfig, ClockConfig, Config, GpioConfig, LedConfig, PathsConfig, ScheduleConfig};
use loopi_agent::led::{LedDriver, LED_UNAVAILABLE_ERROR};
use loopi_agent::osinfo::OsInfo;
use loopi_agent::recorder::audio::{AudioSource, AudioSourceFactory};
use loopi_agent::recorder::ogg_opus::FRAME_SAMPLES;
use loopi_agent::run::{run, Deps, Timings};
use loopi_agent::sysinfo::NoProbe;
use loopi_agent::testing::{FakeTransport, FixedDisk, SyncedClock, ToneSource};
use loopi_agent::wire::{HeartbeatRequest, RemoteCommand};
use tokio::sync::{mpsc, watch};

/// Camera double: appends its PID to `spawns` and runs until SIGINT/SIGTERM.
fn pid_camera(dir: &Path, spawns: &Path) -> String {
    let path = dir.join("pid-camera.sh");
    let script = format!("#!/bin/sh\necho $$ >> '{}'\ntrap 'exit 0' INT TERM\nwhile :; do sleep 0.05; done\n", spawns.display());
    fs::write(&path, script).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    path.to_string_lossy().into_owned()
}

fn camera_pids(spawns: &Path) -> Vec<i32> {
    fs::read_to_string(spawns).unwrap_or_default().lines().filter_map(|l| l.trim().parse().ok()).collect()
}

fn any_camera_alive(spawns: &Path) -> bool {
    // SAFETY: signal 0 only checks that the process exists.
    camera_pids(spawns).into_iter().any(|pid| unsafe { libc::kill(pid, 0) } == 0)
}

/// Tone source that flags whether a microphone is currently open (cleared on drop).
struct TrackedMic {
    inner: ToneSource,
    open: Arc<AtomicBool>,
}

impl AudioSource for TrackedMic {
    fn read_frame(&mut self, frame: &mut [i16; FRAME_SAMPLES]) -> std::io::Result<()> {
        self.inner.read_frame(frame)
    }
}

impl Drop for TrackedMic {
    fn drop(&mut self) {
        self.open.store(false, Ordering::SeqCst);
    }
}

/// One level written to the LED and what was running at that moment.
#[derive(Debug, Clone, Copy)]
struct LedWrite {
    on: bool,
    shutting_down: bool,
    mic_open: bool,
    camera_alive: bool,
}

/// LED that fails while `broken` is set and logs every successful write with the recorder state.
#[derive(Clone)]
struct ProbeLed {
    broken: Arc<AtomicBool>,
    shutting_down: Arc<AtomicBool>,
    mic_open: Arc<AtomicBool>,
    spawns: PathBuf,
    writes: Arc<Mutex<Vec<LedWrite>>>,
}

impl LedDriver for ProbeLed {
    fn set(&mut self, on: bool) -> std::io::Result<()> {
        if self.broken.load(Ordering::SeqCst) {
            return Err(std::io::Error::other("gpio gone"));
        }
        self.writes.lock().unwrap().push(LedWrite {
            on,
            shutting_down: self.shutting_down.load(Ordering::SeqCst),
            mic_open: self.mic_open.load(Ordering::SeqCst),
            camera_alive: any_camera_alive(&self.spawns),
        });
        Ok(())
    }
}

struct Harness {
    _tmp: tempfile::TempDir,
    data_dir: PathBuf,
    camera: String,
    spawns: PathBuf,
    mic_open: Arc<AtomicBool>,
    mic_opens: Arc<AtomicU32>,
    transport: Arc<FakeTransport>,
    led: ProbeLed,
}

impl Harness {
    fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let data_dir = tmp.path().join("data");
        let spawns = tmp.path().join("camera-pids");
        let camera = pid_camera(tmp.path(), &spawns);
        let mic_open = Arc::new(AtomicBool::new(false));
        let led = ProbeLed {
            broken: Arc::default(),
            shutting_down: Arc::default(),
            mic_open: mic_open.clone(),
            spawns: spawns.clone(),
            writes: Arc::default(),
        };
        Self {
            _tmp: tmp,
            data_dir,
            camera,
            spawns,
            mic_open,
            mic_opens: Arc::default(),
            transport: Arc::default(),
            led,
        }
    }

    fn deps(&self, led: Option<Box<dyn LedDriver>>) -> (Deps, watch::Sender<bool>, watch::Receiver<bool>) {
        self.deps_with(led, true)
    }

    /// `audio_enabled` is the provisioned `capture.audio_enabled`.
    fn deps_with(
        &self,
        led: Option<Box<dyn LedDriver>>,
        audio_enabled: bool,
    ) -> (Deps, watch::Sender<bool>, watch::Receiver<bool>) {
        let config = Config {
            wifi: None,
            brain: BrainConfig { url: "https://brain.invalid:8443".into(), pair_token: None, ca_fingerprint: None },
            capture: CaptureConfig { camera_cmd: self.camera.clone(), audio_enabled, ..CaptureConfig::default() },
            schedule: ScheduleConfig::default(),
            led: LedConfig::default(),
            gpio: GpioConfig::default(),
            clock: ClockConfig::default(),
            paths: PathsConfig { data_dir: self.data_dir.clone(), sync_flag: self.data_dir.join("unused") },
        };
        let (open, opens) = (self.mic_open.clone(), self.mic_opens.clone());
        let factory: AudioSourceFactory = Box::new(move || {
            opens.fetch_add(1, Ordering::SeqCst);
            open.store(true, Ordering::SeqCst);
            Ok(Box::new(TrackedMic { inner: ToneSource::realtime(), open: open.clone() }) as Box<dyn AudioSource>)
        });
        let (_press_tx, presses) = mpsc::channel(1);
        let deps = Deps {
            config,
            clock: Arc::new(SyncedClock),
            disk: Arc::new(FixedDisk::new(10.0)),
            transport: self.transport.clone(),
            audio_factory: factory,
            led,
            presses,
            probe: Arc::new(NoProbe),
            timings: Timings {
                heartbeat: Duration::from_millis(100),
                upload_idle: Duration::from_millis(20),
                video_poll: Duration::from_millis(50),
                disk_check: Duration::from_millis(200),
                tick: Duration::from_millis(50),
                led_frame: Duration::from_millis(5),
                audio_retry: Duration::from_millis(50),
                audio_segment: Some(Duration::from_secs(1)),
            },
            liveness: loopi_agent::liveness::Liveness::new(),
            os: OsInfo::default(),
            ota: None,
        };
        let (stop_tx, stop_rx) = watch::channel(false);
        (deps, stop_tx, stop_rx)
    }

    fn recorder_started(&self) -> bool {
        !camera_pids(&self.spawns).is_empty() || self.mic_opens.load(Ordering::SeqCst) > 0
    }

    fn last_heartbeat(&self) -> HeartbeatRequest {
        self.transport.heartbeats.lock().unwrap().last().cloned().expect("heartbeats keep running")
    }
}

async fn eventually(mut check: impl FnMut() -> bool) -> bool {
    for _ in 0..200 {
        if check() {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    false
}

#[tokio::test]
async fn without_an_led_nothing_records_but_heartbeats_and_uploads_run() {
    let h = Harness::new();
    {
        let (queue, _) = loopi_agent::queue::Queue::open(&h.data_dir.join("queue")).unwrap();
        let now = chrono::Utc::now();
        queue.enqueue_bytes(b"{}", loopi_agent::item::ItemKind::Marker, now, now).unwrap();
    }
    let (deps, stop_tx, stop_rx) = h.deps(None);
    let agent = tokio::spawn(run(deps, stop_rx));

    assert!(eventually(|| h.transport.heartbeats.lock().unwrap().len() >= 3).await);
    assert!(!h.recorder_started(), "no recorder may start without a privacy LED");
    let heartbeat = h.last_heartbeat();
    assert!(heartbeat.last_error.as_deref().unwrap_or_default().starts_with(LED_UNAVAILABLE_ERROR), "{heartbeat:?}");
    assert!(!heartbeat.recording_video && !heartbeat.recording_audio);
    assert_eq!(h.transport.puts.lock().unwrap().len(), 1, "the queued marker was still uploaded");

    stop_tx.send(true).unwrap();
    agent.await.unwrap().unwrap();
}

#[tokio::test]
async fn a_failing_led_keeps_recorders_off_until_its_writes_succeed() {
    let h = Harness::new();
    h.led.broken.store(true, Ordering::SeqCst);
    let (deps, stop_tx, stop_rx) = h.deps(Some(Box::new(h.led.clone())));
    let agent = tokio::spawn(run(deps, stop_rx));

    // 5 ms LED frames: the failure limit is reached long before this.
    assert!(
        eventually(|| h.transport.heartbeats.lock().unwrap().last().is_some_and(|hb| hb
            .last_error
            .as_deref()
            .is_some_and(|e| e.starts_with(LED_UNAVAILABLE_ERROR))))
        .await,
        "the brain learns that recording is disabled"
    );
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(!h.recorder_started(), "a failing LED never lets a recorder start");

    h.led.broken.store(false, Ordering::SeqCst);
    assert!(eventually(|| !camera_pids(&h.spawns).is_empty() && h.mic_open.load(Ordering::SeqCst)).await, "recording resumes");
    assert!(eventually(|| h.led.writes.lock().unwrap().last().is_some_and(|w| w.on)).await, "LED solid while recording");
    assert!(eventually(|| h.last_heartbeat().last_error.is_none()).await, "the LED error is cleared");

    stop_tx.send(true).unwrap();
    agent.await.unwrap().unwrap();
}

#[tokio::test]
async fn shutdown_while_recording_keeps_the_led_solid_until_both_recorders_stopped() {
    let h = Harness::new();
    let (deps, stop_tx, stop_rx) = h.deps(Some(Box::new(h.led.clone())));
    let agent = tokio::spawn(run(deps, stop_rx));

    assert!(eventually(|| any_camera_alive(&h.spawns) && h.mic_open.load(Ordering::SeqCst)).await, "recording");
    assert!(eventually(|| h.led.writes.lock().unwrap().last().is_some_and(|w| w.on)).await);

    h.led.shutting_down.store(true, Ordering::SeqCst);
    stop_tx.send(true).unwrap();
    agent.await.unwrap().unwrap();

    let writes = h.led.writes.lock().unwrap().clone();
    let after: Vec<LedWrite> = writes.into_iter().filter(|w| w.shutting_down).collect();
    let dark_while_recording: Vec<&LedWrite> = after.iter().filter(|w| !w.on && (w.mic_open || w.camera_alive)).collect();
    assert!(dark_while_recording.is_empty(), "LED went dark while a recorder ran: {dark_while_recording:?}");
    assert!(after.last().is_some_and(|w| !w.on), "the LED is off once everything stopped: {after:?}");
    assert!(!any_camera_alive(&h.spawns), "no camera process outlives run()");
    assert!(!h.mic_open.load(Ordering::SeqCst), "the microphone is closed");
}

/// A non-empty directory where a file is expected: cannot be read, hashed or removed (even by
/// root), standing in for an unreadable file on a failing SD card.
fn stuck_entry(path: &Path) {
    fs::create_dir_all(path.join("inner")).unwrap();
}

#[tokio::test]
async fn unreadable_leftovers_in_every_recovery_path_do_not_stop_the_agent() {
    let h = Harness::new();
    let queue_dir = h.data_dir.join("queue");
    stuck_entry(&queue_dir.join("items").join(".stuck.tmp"));
    let run_dir = queue_dir.join("incoming/video/01JA2B3C4D5E6F7G8H9J0KMNPQ");
    stuck_entry(&run_dir.join("v_000000.h264"));
    fs::write(run_dir.join("v_000001.h264"), b"video after the bad one").unwrap();
    let audio_dir = queue_dir.join("incoming/audio");
    stuck_entry(&audio_dir.join("01JA2B3C4D5E6F7G8H9J0KMNPR.opus.part"));
    fs::write(audio_dir.join("01JA2B3C4D5E6F7G8H9J0KMNPS.opus.part"), b"OggS").unwrap();
    // Paused, so only recovery and uploads are exercised.
    fs::create_dir_all(h.data_dir.join("config")).unwrap();
    fs::write(h.data_dir.join("config/state.json"), br#"{"paused":true,"audio_enabled":true}"#).unwrap();

    let (deps, stop_tx, stop_rx) = h.deps(Some(Box::new(h.led.clone())));
    let agent = tokio::spawn(run(deps, stop_rx));
    assert!(eventually(|| h.transport.puts.lock().unwrap().len() >= 2).await, "recovered segments are uploaded");
    assert!(eventually(|| !h.transport.heartbeats.lock().unwrap().is_empty()).await);
    let error = h.transport.heartbeats.lock().unwrap()[0].last_error.clone().unwrap_or_default();
    assert!(error.starts_with("startup recovery: 3 file(s) left in place"), "{error}");

    stop_tx.send(true).unwrap();
    agent.await.unwrap().unwrap();
    let kinds: Vec<_> = h.transport.puts.lock().unwrap().iter().map(|m| m.kind).collect();
    assert!(kinds.contains(&loopi_agent::item::ItemKind::Video) && kinds.contains(&loopi_agent::item::ItemKind::Audio), "{kinds:?}");
    assert!(run_dir.join("v_000000.h264").exists(), "the unreadable segment is left for the next start");
    assert!(audio_dir.join("01JA2B3C4D5E6F7G8H9J0KMNPR.opus.part").exists(), "audio is never deleted");
}

/// Privacy (R4): a file without sound is a ceiling. The brain's "audio on" is acknowledged but never reopens the
/// microphone, and the heartbeat keeps saying that audio is off.
#[tokio::test]
async fn a_remote_audio_on_cannot_reopen_a_microphone_the_config_turned_off() {
    const AUDIO_ON_ID: &str = "01JA2B3C4D5E6F7G8H9J0KMNPQ";
    let h = Harness::new();
    h.transport.push_heartbeat(Ok(vec![RemoteCommand {
        id: AUDIO_ON_ID.into(),
        kind: "set_config".into(),
        payload: serde_json::json!({ "audio_enabled": true }),
    }]));
    let (deps, stop_tx, stop_rx) = h.deps_with(Some(Box::new(h.led.clone())), false);
    let agent = tokio::spawn(run(deps, stop_rx));

    let acked = |h: &Harness| h.transport.heartbeats.lock().unwrap().iter().any(|hb| hb.acked_command_ids == [AUDIO_ON_ID]);
    assert!(eventually(|| acked(&h)).await, "the command reached the agent and was acknowledged");
    assert!(eventually(|| !camera_pids(&h.spawns).is_empty()).await, "video records: only the sound is off");
    let after = h.transport.heartbeats.lock().unwrap().len();
    assert!(eventually(|| h.transport.heartbeats.lock().unwrap().len() >= after + 3).await);

    let heartbeat = h.last_heartbeat();
    assert!(!heartbeat.audio_enabled && !heartbeat.recording_audio, "{heartbeat:?}");
    assert_eq!(h.mic_opens.load(Ordering::SeqCst), 0, "the microphone was never opened");

    stop_tx.send(true).unwrap();
    agent.await.unwrap().unwrap();
}
