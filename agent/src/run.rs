//! Process-wide orchestration: recorders, uploader, heartbeat, control loop, LED and disk guard.
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Context;
use tokio::sync::{mpsc, watch};
use tokio::task::JoinSet;
use tokio::time::{Instant, MissedTickBehavior};

use crate::button::Press;
use crate::clock::Clock;
use crate::config::Config;
use crate::control::{self, recording_gates, Command, ControlState};
use crate::disk::{self, DiskUsage};
use crate::heartbeat::{run_heartbeat, HeartbeatDeps, HEARTBEAT_INTERVAL};
use crate::item::ItemKind;
use crate::led::{led_pattern, level_at, LedDriver, LedPattern, LedWriter, LED_UNAVAILABLE_ERROR};
use crate::liveness::Liveness;
use crate::queue::Queue;
use crate::recorder::audio::{self, AudioOptions, AudioSourceFactory};
use crate::recorder::gain;
use crate::recorder::video::{self, VideoOptions};
use crate::schedule::Schedule;
use crate::status::{status_channel, AgentStatus, LedHealth, StatusTx};
use crate::sysinfo::SysProbe;
use crate::transport::Transport;
use crate::uploader::run_uploader;

/// `status.last_error` while the disk guard has stopped recording (cleared when it resumes).
pub const DISK_FULL_ERROR: &str = "disk full: recording stopped";
const STATE_NOT_SAVED_PREFIX: &str = "state not saved: ";
const COMMAND_CHANNEL_CAPACITY: usize = 32;

#[derive(Debug, Clone)]
pub struct Timings {
    pub heartbeat: Duration,
    pub upload_idle: Duration,
    pub video_poll: Duration,
    pub disk_check: Duration,
    pub tick: Duration,
    pub led_frame: Duration,
    pub audio_retry: Duration,
    /// Overrides `capture.segment_seconds` for audio (tests only).
    pub audio_segment: Option<Duration>,
}

impl Default for Timings {
    fn default() -> Self {
        Self {
            heartbeat: HEARTBEAT_INTERVAL,
            upload_idle: Duration::from_secs(2),
            video_poll: Duration::from_secs(2),
            disk_check: Duration::from_secs(30),
            tick: Duration::from_secs(5),
            led_frame: Duration::from_millis(50),
            audio_retry: Duration::from_secs(10),
            audio_segment: None,
        }
    }
}

pub struct Paths {
    pub queue: PathBuf,
    pub video_incoming: PathBuf,
    pub audio_incoming: PathBuf,
    pub state: PathBuf,
}

impl Paths {
    pub fn new(data_dir: &Path) -> Self {
        let queue = data_dir.join("queue");
        let incoming = queue.join("incoming");
        Self {
            video_incoming: incoming.join("video"),
            audio_incoming: incoming.join("audio"),
            queue,
            state: data_dir.join("config").join("state.json"),
        }
    }
}

pub struct Deps {
    pub config: Config,
    pub clock: Arc<dyn Clock>,
    pub disk: Arc<dyn DiskUsage>,
    pub transport: Arc<dyn Transport>,
    pub audio_factory: AudioSourceFactory,
    /// `None` when the privacy LED could not be opened: heartbeats and uploads still run, but the
    /// recording gates stay closed for the life of the process.
    pub led: Option<Box<dyn LedDriver>>,
    pub presses: mpsc::Receiver<Press>,
    pub probe: Arc<dyn SysProbe>,
    pub timings: Timings,
    /// Beaten on every main-loop iteration; the systemd watchdog pings only while it is fresh.
    pub liveness: Liveness,
}

/// Where the LED loop is in its life: normal patterns, forced on while recorders wind down, done.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LedPhase {
    Running,
    Draining,
    Done,
}

fn set_if_changed(slot: &mut bool, value: bool) -> bool {
    let changed = *slot != value;
    *slot = value;
    changed
}

fn apply_command(control: &watch::Sender<ControlState>, state_path: &Path, status: &StatusTx, command: &Command) {
    let next = control.borrow().apply(command);
    let saved = control::save_state(state_path, &next);
    // Apply even when the save fails: the user's pause must take effect now.
    control.send_replace(next);
    match saved {
        Ok(()) => status.send_if_modified(|s| {
            let ours = s.last_error.as_deref().is_some_and(|e| e.starts_with(STATE_NOT_SAVED_PREFIX));
            if ours {
                s.last_error = None;
            }
            ours
        }),
        Err(e) => {
            tracing::error!(error = %e, "could not persist control state");
            status.send_modify(|s| s.last_error = Some(format!("{STATE_NOT_SAVED_PREFIX}{e}")));
            true
        }
    };
    tracing::info!(?command, ?next, "control state changed");
}

fn handle_press(
    press: Press,
    queue: &Queue,
    clock: &dyn Clock,
    control: &watch::Sender<ControlState>,
    state_path: &Path,
    status: &StatusTx,
) {
    match press {
        Press::Short => {
            let now = clock.now();
            if let Err(e) = queue.enqueue_bytes(b"{}", ItemKind::Marker, now, now) {
                tracing::error!(error = %e, "could not queue marker");
                status.send_modify(|s| s.last_error = Some(format!("marker not saved: {e}")));
            }
        }
        Press::Long => apply_command(control, state_path, status, &Command::TogglePause),
    }
}

fn check_disk(queue: &Queue, disk: &dyn DiskUsage, status: &StatusTx) {
    match disk::enforce(queue, disk) {
        Ok(report) => status.send_modify(|s| {
            s.disk_used_pct = report.used_pct;
            s.disk_stopped = report.stop;
            s.dropped_since_heartbeat = s.dropped_since_heartbeat.saturating_add(report.dropped);
            if report.stop {
                s.last_error = Some(DISK_FULL_ERROR.into());
            } else if s.last_error.as_deref() == Some(DISK_FULL_ERROR) {
                s.last_error = None;
            }
        }),
        Err(e) => status.send_modify(|s| s.last_error = Some(format!("disk check: {e}"))),
    }
}

/// Drives the LED from status. Privacy invariant: once shutdown starts the LED is forced on until
/// `run` reports (`LedPhase::Done`) that both recorders have actually stopped; only then it goes off.
/// Failed writes are retried every frame; `LedWriter` reports them and, past the limit, marks the
/// LED failed so the recording gates close.
async fn led_loop(
    led: Box<dyn LedDriver>,
    status: StatusTx,
    control: watch::Receiver<ControlState>,
    frame: Duration,
    mut phase: watch::Receiver<LedPhase>,
) {
    let start = Instant::now();
    let mut ticker = tokio::time::interval(frame);
    ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let mut writer = LedWriter::new(led);
    loop {
        let current = *phase.borrow();
        let pattern = match current {
            LedPhase::Done => break,
            LedPhase::Draining => LedPattern::Solid,
            LedPhase::Running => led_pattern(&status.borrow(), &control.borrow()),
        };
        writer.set(level_at(pattern, start.elapsed().as_millis() as u64), Some(&status));
        tokio::select! {
            _ = ticker.tick() => {}
            // A dropped sender means `run` is gone: treat it as the end, never spin.
            changed = phase.changed() => if changed.is_err() { break },
        }
    }
    writer.set(false, None);
}

/// Initial `status.led` and `last_error`: without a driver the LED can never be confirmed.
fn initial_led_status(led: Option<&dyn LedDriver>) -> (LedHealth, Option<String>) {
    match led {
        Some(_) => (LedHealth::Unconfirmed, None),
        None => {
            tracing::error!("{LED_UNAVAILABLE_ERROR}");
            (LedHealth::Failed, Some(LED_UNAVAILABLE_ERROR.into()))
        }
    }
}

pub async fn run(deps: Deps, mut shutdown: watch::Receiver<bool>) -> anyhow::Result<()> {
    let Deps { config, clock, disk, transport, audio_factory, led, mut presses, probe, timings, liveness } = deps;
    let paths = Paths::new(&config.paths.data_dir);
    let segment = Duration::from_secs(config.capture.segment_seconds.into());

    // Only failing to create the queue directories is fatal; a file recovery cannot handle is
    // left in place and reported, so one bad sector cannot stop the agent on every start.
    let (queue, queue_recovery) = Queue::open(&paths.queue).context("opening the upload queue")?;
    let recovery = queue_recovery
        .merge(video::recover_incoming(&paths.video_incoming, &queue, segment, config.capture.video_fps))
        .merge(audio::recover_incoming(&paths.audio_incoming, &queue, segment));
    tracing::info!(recovered = recovery.recovered, dropped = recovery.dropped, problems = recovery.problems.len(), "queue ready");
    let queue = Arc::new(queue);
    let schedule = Schedule::from_config(&config.schedule).map_err(anyhow::Error::msg)?;

    let (control_tx, control_rx) = watch::channel(control::load_state(&paths.state, config.capture.audio_enabled));
    // The gates stay closed until the LED loop's first successful write confirms the LED works.
    let (led_health, led_error) = initial_led_status(led.as_deref());
    let startup_errors: Vec<String> = led_error.into_iter().chain(recovery.summary()).collect();
    let status = status_channel(AgentStatus {
        paired: true,
        clock_synced: clock.is_synced(),
        quiet: schedule.is_quiet(clock.now()),
        led: led_health,
        last_error: (!startup_errors.is_empty()).then(|| startup_errors.join("; ")),
        dropped_since_heartbeat: recovery.dropped,
        ..AgentStatus::default()
    });
    let (video_gate_tx, video_gate_rx) = watch::channel(false);
    let (audio_gate_tx, audio_gate_rx) = watch::channel(false);
    let (led_phase_tx, led_phase_rx) = watch::channel(LedPhase::Running);
    let (command_tx, mut command_rx) = mpsc::channel::<Command>(COMMAND_CHANNEL_CAPACITY);

    let mut tasks = JoinSet::new();
    tasks.spawn(run_uploader(queue.clone(), transport.clone(), status.clone(), shutdown.clone(), timings.upload_idle));
    tasks.spawn(run_heartbeat(
        HeartbeatDeps::new(
            transport,
            queue.clone(),
            status.clone(),
            control_rx.clone(),
            command_tx,
            probe,
            timings.heartbeat,
        ),
        shutdown.clone(),
    ));
    if let Some(led) = led {
        tasks.spawn(led_loop(led, status.clone(), control_rx, timings.led_frame, led_phase_rx));
    }

    let video_opts = VideoOptions { poll: timings.video_poll, ..VideoOptions::from_config(&config.capture, paths.video_incoming.clone()) };
    let video_task = tokio::spawn(video::run_video(
        video_opts,
        queue.clone(),
        clock.clone(),
        video_gate_rx,
        status.clone(),
        shutdown.clone(),
    ));
    let audio_opts = AudioOptions {
        segment: timings.audio_segment.unwrap_or(segment),
        incoming_dir: paths.audio_incoming.clone(),
        retry: timings.audio_retry,
    };
    // Every frame is amplified before encoding (spec D19); the raw ALSA level is far too quiet.
    let audio_factory = gain::with_gain(audio_factory, config.capture.audio_gain_db);
    let audio_thread = {
        let (queue, clock, status, shutdown) = (queue.clone(), clock.clone(), status.clone(), shutdown.clone());
        std::thread::Builder::new()
            .name("audio".into())
            .spawn(move || audio::run_audio_blocking(audio_factory, audio_opts, queue, clock, audio_gate_rx, status, shutdown))
            .context("starting the audio thread")?
    };

    // Before the first gate computation: recording must never start on a full disk.
    check_disk(&queue, disk.as_ref(), &status);
    let mut tick = tokio::time::interval(timings.tick);
    tick.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let mut disk_tick = tokio::time::interval(timings.disk_check);
    disk_tick.set_missed_tick_behavior(MissedTickBehavior::Delay);
    // Any status change (an LED fault above all) re-evaluates the gates at once, not at the next tick.
    let mut status_changes = status.subscribe();
    loop {
        liveness.beat();
        if *shutdown.borrow() {
            break;
        }
        let gates = recording_gates(&control_tx.borrow(), &status.borrow());
        video_gate_tx.send_if_modified(|g| set_if_changed(g, gates.video));
        audio_gate_tx.send_if_modified(|g| set_if_changed(g, gates.audio));
        tokio::select! {
            Some(command) = command_rx.recv() => apply_command(&control_tx, &paths.state, &status, &command),
            Some(press) = presses.recv() => handle_press(press, &queue, clock.as_ref(), &control_tx, &paths.state, &status),
            _ = tick.tick() => {
                let (synced, quiet) = (clock.is_synced(), schedule.is_quiet(clock.now()));
                status.send_if_modified(|s| set_if_changed(&mut s.clock_synced, synced) | set_if_changed(&mut s.quiet, quiet));
            }
            _ = disk_tick.tick() => check_disk(&queue, disk.as_ref(), &status),
            // `status` lives as long as this loop, so this never reports a closed channel.
            _ = status_changes.changed() => {}
            // A dropped shutdown sender can never fire again: shut down rather than spin.
            changed = shutdown.changed() => if changed.is_err() || *shutdown.borrow() { break },
        }
    }

    // Shutdown: hold the LED on, close the gates, and only turn it off once recorders are gone.
    led_phase_tx.send_replace(LedPhase::Draining);
    video_gate_tx.send_replace(false);
    audio_gate_tx.send_replace(false);
    let video_result = video_task.await;
    let audio_result = tokio::task::spawn_blocking(move || audio_thread.join()).await;
    led_phase_tx.send_replace(LedPhase::Done);
    while tasks.join_next().await.is_some() {}
    video_result.map_err(|e| anyhow::anyhow!("video task failed: {e}"))?;
    audio_result?.map_err(|_| anyhow::anyhow!("audio thread panicked"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::{CountingDisk, FixedDisk, RecordingLed};

    fn open_queue(dir: &Path) -> Queue {
        Queue::open(&dir.join("q")).unwrap().0
    }

    #[test]
    fn disk_full_message_is_set_and_cleared_and_drops_are_counted() {
        let tmp = tempfile::tempdir().unwrap();
        let queue = open_queue(tmp.path());
        let status = status_channel(AgentStatus { dropped_since_heartbeat: 1, ..AgentStatus::default() });
        let full = FixedDisk::new(97.0);
        check_disk(&queue, &full, &status);
        assert!(status.borrow().disk_stopped);
        assert_eq!(status.borrow().last_error.as_deref(), Some(DISK_FULL_ERROR));
        *full.0.lock().unwrap() = 40.0;
        check_disk(&queue, &full, &status);
        assert!(!status.borrow().disk_stopped);
        assert_eq!(status.borrow().last_error, None);

        status.send_modify(|s| s.last_error = Some("camera".into()));
        check_disk(&queue, &full, &status);
        assert_eq!(status.borrow().last_error.as_deref(), Some("camera"), "other errors are left alone");

        let now = chrono::Utc::now();
        queue.enqueue_bytes(b"x", ItemKind::Video, now, now).unwrap();
        queue.enqueue_bytes(b"x", ItemKind::Video, now, now).unwrap();
        let counting = CountingDisk { dir: queue.items_dir().to_path_buf(), base: 80.0, per_item: 4.0 };
        check_disk(&queue, &counting, &status);
        assert_eq!(status.borrow().dropped_since_heartbeat, 3, "1 carried + 2 deleted (88 -> 84 -> 80)");
    }

    #[test]
    fn unsaved_state_is_reported_not_swallowed_and_applies_anyway() {
        let tmp = tempfile::tempdir().unwrap();
        let blocker = tmp.path().join("file");
        std::fs::write(&blocker, b"x").unwrap();
        let state_path = blocker.join("state.json");
        let (control, _rx) = watch::channel(ControlState::initial(true));
        let status = status_channel(AgentStatus::default());
        apply_command(&control, &state_path, &status, &Command::Pause);
        assert!(control.borrow().paused);
        assert!(status.borrow().last_error.as_deref().unwrap().starts_with(STATE_NOT_SAVED_PREFIX));

        let good = tmp.path().join("ok").join("state.json");
        apply_command(&control, &good, &status, &Command::Resume);
        assert_eq!(status.borrow().last_error, None, "cleared once a save works");
    }

    #[test]
    fn short_press_queues_a_marker_and_long_press_toggles_pause() {
        let tmp = tempfile::tempdir().unwrap();
        let queue = open_queue(tmp.path());
        let (control, _rx) = watch::channel(ControlState::initial(true));
        let status = status_channel(AgentStatus::default());
        let state_path = tmp.path().join("state.json");
        let clock = crate::testing::SyncedClock;
        handle_press(Press::Short, &queue, &clock, &control, &state_path, &status);
        assert_eq!(queue.len().unwrap(), 1);
        handle_press(Press::Long, &queue, &clock, &control, &state_path, &status);
        assert!(control.borrow().paused);
    }

    #[tokio::test]
    async fn led_stays_on_while_draining_and_goes_off_when_done() {
        let led = RecordingLed::default();
        let states = led.states.clone();
        let status = status_channel(AgentStatus::default());
        let (_control_tx, control_rx) = watch::channel(ControlState::initial(true));
        let (phase_tx, phase_rx) = watch::channel(LedPhase::Running);
        let task = tokio::spawn(led_loop(Box::new(led), status.clone(), control_rx, Duration::from_millis(5), phase_rx));
        tokio::time::sleep(Duration::from_millis(30)).await;
        phase_tx.send_replace(LedPhase::Draining);
        tokio::time::sleep(Duration::from_millis(60)).await;
        assert_eq!(states.lock().unwrap().last(), Some(&true), "recorders may still run");
        phase_tx.send_replace(LedPhase::Done);
        task.await.unwrap();
        assert_eq!(states.lock().unwrap().last(), Some(&false));
    }

    #[tokio::test]
    async fn dropped_phase_sender_ends_the_led_loop() {
        let led = RecordingLed::default();
        let status = status_channel(AgentStatus::default());
        let (_control_tx, control_rx) = watch::channel(ControlState::initial(true));
        let (phase_tx, phase_rx) = watch::channel(LedPhase::Running);
        let task = tokio::spawn(led_loop(Box::new(led), status.clone(), control_rx, Duration::from_millis(5), phase_rx));
        drop(phase_tx);
        tokio::time::timeout(Duration::from_secs(2), task).await.unwrap().unwrap();
    }

    struct FlakyLed {
        failures_left: u32,
        on: Arc<std::sync::Mutex<bool>>,
    }

    impl LedDriver for FlakyLed {
        fn set(&mut self, on: bool) -> std::io::Result<()> {
            if self.failures_left > 0 {
                self.failures_left -= 1;
                return Err(std::io::Error::other("gpio busy"));
            }
            *self.on.lock().unwrap() = on;
            Ok(())
        }
    }

    #[tokio::test]
    async fn failed_led_writes_are_retried_and_reported_until_they_succeed() {
        let on = Arc::new(std::sync::Mutex::new(false));
        let led = FlakyLed { failures_left: 3, on: on.clone() };
        let status = status_channel(AgentStatus { recording_video: true, ..AgentStatus::default() });
        let (_control_tx, control_rx) = watch::channel(ControlState::initial(true));
        let (phase_tx, phase_rx) = watch::channel(LedPhase::Running);
        let task = tokio::spawn(led_loop(Box::new(led), status.clone(), control_rx, Duration::from_millis(5), phase_rx));
        tokio::time::sleep(Duration::from_millis(8)).await;
        assert!(status.borrow().last_error.as_deref().is_some_and(|e| e.starts_with(crate::led::LED_WRITE_ERROR_PREFIX)));
        tokio::time::sleep(Duration::from_millis(80)).await;
        assert!(*on.lock().unwrap(), "LED ends up on while recording despite early failures");
        assert_eq!(status.borrow().last_error, None, "error cleared after a successful write");
        phase_tx.send_replace(LedPhase::Done);
        task.await.unwrap();
    }

    #[test]
    fn disk_is_checked_before_recording_can_start() {
        // check_disk is what run() calls before the first gate computation.
        let tmp = tempfile::tempdir().unwrap();
        let queue = open_queue(tmp.path());
        let status = status_channel(AgentStatus {
            paired: true,
            clock_synced: true,
            led: LedHealth::Working,
            ..AgentStatus::default()
        });
        let state = ControlState::initial(true);
        assert!(recording_gates(&state, &status.borrow()).video, "open before the check");
        check_disk(&queue, &FixedDisk::new(99.0), &status);
        let gates = recording_gates(&state, &status.borrow());
        assert!(!gates.video && !gates.audio, "closed once the full disk is seen");
    }
}
