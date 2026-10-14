//! Video capture: supervise `rpicam-vid` writing fixed-length H.264 segments and queue each one
//! once it is closed. Raw H.264 carries no timestamps, so times come from file mtimes.
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Utc};
use tokio::process::{Child, Command};
use tokio::sync::watch;
use tokio::time::Instant;

use crate::clock::Clock;
use crate::config::{CaptureConfig, CONTRACT_MAX_SEGMENT_SECONDS};
use crate::item::{ItemKind, ItemMeta};
use crate::queue::Queue;
use crate::status::StatusTx;

pub const SEGMENT_PATTERN: &str = "v_%06d.h264";
const INTRA_SECONDS: u32 = 5;
const FALLBACK_SEGMENT_SECONDS: i64 = 60;

#[derive(Debug, Clone)]
pub struct VideoOptions {
    pub camera_cmd: String,
    pub width: u32,
    pub height: u32,
    pub fps: u32,
    pub bitrate_bps: u32,
    pub segment: Duration,
    pub incoming_dir: PathBuf,
    pub poll: Duration,
    pub stop_grace: Duration,
    pub max_backoff: Duration,
    pub healthy_run: Duration,
}

impl VideoOptions {
    pub fn from_config(capture: &CaptureConfig, incoming_dir: PathBuf) -> Self {
        Self {
            camera_cmd: capture.camera_cmd.clone(),
            width: capture.video_width,
            height: capture.video_height,
            fps: capture.video_fps,
            bitrate_bps: capture.video_bitrate_bps,
            segment: Duration::from_secs(capture.segment_seconds.into()),
            incoming_dir,
            poll: Duration::from_secs(2),
            stop_grace: Duration::from_secs(5),
            max_backoff: Duration::from_secs(60),
            healthy_run: Duration::from_secs(60),
        }
    }
}

/// `--inline` repeats SPS/PPS so every segment decodes on its own; `--intra` keeps I-frames
/// frequent enough that `--segment` can cut close to the requested length.
pub fn rpicam_args(opts: &VideoOptions, run_dir: &Path) -> Vec<String> {
    let mut args: Vec<String> = ["-t", "0", "-n", "--codec", "h264", "--inline"].iter().map(|s| s.to_string()).collect();
    args.extend([
        "--intra".to_string(),
        (opts.fps * INTRA_SECONDS).to_string(),
        "--framerate".to_string(),
        opts.fps.to_string(),
        "--width".to_string(),
        opts.width.to_string(),
        "--height".to_string(),
        opts.height.to_string(),
        "--bitrate".to_string(),
        opts.bitrate_bps.to_string(),
        "--segment".to_string(),
        opts.segment.as_millis().to_string(),
        "-o".to_string(),
        run_dir.join(SEGMENT_PATTERN).to_string_lossy().into_owned(),
    ]);
    args
}

/// A segment starts where the previous one ended. If the wall clock jumped backwards, fall back
/// to `ended - segment` so `ended_at` is never before `started_at`.
pub fn segment_start(previous_end: DateTime<Utc>, ended: DateTime<Utc>, segment: chrono::Duration) -> DateTime<Utc> {
    let start = if ended > previous_end { previous_end } else { ended - segment };
    // The brain rejects items longer than the contract maximum (a forward clock jump or a camera
    // stall can open a long gap), so never let End - Start exceed it.
    start.max(ended - chrono::Duration::seconds(CONTRACT_MAX_SEGMENT_SECONDS.into()))
}

fn list_segments(dir: &Path) -> io::Result<Vec<PathBuf>> {
    let mut files: Vec<PathBuf> = fs::read_dir(dir)?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| {
            let name = p.file_name().and_then(|n| n.to_str()).unwrap_or_default();
            name.starts_with("v_") && name.ends_with(".h264")
        })
        .collect();
    files.sort();
    Ok(files)
}

fn mtime(path: &Path) -> io::Result<DateTime<Utc>> {
    Ok(fs::metadata(path)?.modified()?.into())
}

pub(crate) fn to_chrono(d: Duration) -> chrono::Duration {
    chrono::Duration::from_std(d).unwrap_or(chrono::Duration::seconds(FALLBACK_SEGMENT_SECONDS))
}

/// Queue finished segment files in order, discarding empty ones. Each segment starts where the
/// previous one ended; the first starts at `chain_from`, or `ended - segment` when unknown.
/// Returns the queued items and the end time of the last one.
fn enqueue_segments(
    queue: &Queue,
    files: Vec<PathBuf>,
    chain_from: Option<DateTime<Utc>>,
    segment: chrono::Duration,
    fps: u32,
) -> io::Result<(Vec<ItemMeta>, Option<DateTime<Utc>>)> {
    let mut last_end = chain_from;
    let mut queued = Vec::with_capacity(files.len());
    for path in files {
        if fs::metadata(&path)?.len() == 0 {
            fs::remove_file(&path)?;
            continue;
        }
        let ended = mtime(&path)?;
        let started = segment_start(last_end.unwrap_or(ended - segment), ended, segment);
        queued.push(queue.enqueue_file(&path, ItemKind::Video, started, ended, Some(fps))?);
        last_end = Some(ended);
    }
    Ok((queued, last_end))
}

pub struct SegmentCollector {
    run_dir: PathBuf,
    last_end: DateTime<Utc>,
    segment: chrono::Duration,
    fps: u32,
}

impl SegmentCollector {
    pub fn new(run_dir: PathBuf, run_started: DateTime<Utc>, segment: Duration, fps: u32) -> Self {
        Self { run_dir, last_end: run_started, segment: to_chrono(segment), fps }
    }

    /// Queue every closed segment. While rpicam-vid runs the newest file is still being written,
    /// so it is only included once the process has exited (`include_last`).
    pub fn collect(&mut self, queue: &Queue, include_last: bool) -> io::Result<Vec<ItemMeta>> {
        let mut files = list_segments(&self.run_dir)?;
        if !include_last {
            files.pop();
        }
        let (queued, last_end) = enqueue_segments(queue, files, Some(self.last_end), self.segment, self.fps)?;
        if let Some(end) = last_end {
            self.last_end = end;
        }
        Ok(queued)
    }
}

/// After a crash or power cut, queue segments left in old run directories.
pub fn recover_incoming(incoming: &Path, queue: &Queue, segment: Duration, fps: u32) -> io::Result<u32> {
    if !incoming.exists() {
        return Ok(0);
    }
    let mut recovered = 0;
    for entry in fs::read_dir(incoming)? {
        let run_dir = entry?.path();
        if !run_dir.is_dir() {
            continue;
        }
        let (queued, _) = enqueue_segments(queue, list_segments(&run_dir)?, None, to_chrono(segment), fps)?;
        recovered += queued.len() as u32;
        let _ = fs::remove_dir(&run_dir);
    }
    Ok(recovered)
}

pub fn camera_backoff(failures: u32, max: Duration) -> Duration {
    Duration::from_secs(1u64 << failures.min(6)).min(max)
}

/// Make the child receive SIGTERM when the agent dies, so no recorder outlives it.
#[cfg(target_os = "linux")]
fn die_with_parent(command: &mut Command) {
    // SAFETY: prctl(2) is async-signal-safe; this runs in the child between fork and exec.
    unsafe {
        command.pre_exec(|| {
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) != 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }
}

#[cfg(not(target_os = "linux"))]
fn die_with_parent(_command: &mut Command) {}

fn spawn_camera(cmd: &str, args: &[String]) -> io::Result<Child> {
    let mut command = Command::new(cmd);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .kill_on_drop(true);
    die_with_parent(&mut command);
    command.spawn()
}

/// SIGINT lets rpicam-vid close its file cleanly; SIGKILL after `grace`.
async fn stop_child(child: &mut Child, grace: Duration) {
    if let Some(pid) = child.id() {
        // SAFETY: kill(2) on our own child process.
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGINT);
        }
    }
    if tokio::time::timeout(grace, child.wait()).await.is_err() {
        let _ = child.kill().await;
    }
}

enum RunEnd {
    GateClosed,
    Shutdown,
    Exited { ran_for: Duration, code: Option<i32> },
}

/// A `changed()` result means shutdown when the value is true or the sender is gone.
fn is_shutdown(changed: &Result<(), watch::error::RecvError>, rx: &watch::Receiver<bool>) -> bool {
    changed.is_err() || *rx.borrow()
}

async fn record_once(
    opts: &VideoOptions,
    queue: &Queue,
    clock: &dyn Clock,
    gate: &mut watch::Receiver<bool>,
    status: &StatusTx,
    shutdown: &mut watch::Receiver<bool>,
) -> io::Result<RunEnd> {
    let run_dir = opts.incoming_dir.join(ulid::Ulid::new().to_string());
    fs::create_dir_all(&run_dir)?;
    // Privacy invariant: the LED must already be solid when the camera process comes to life.
    status.send_modify(|s| s.recording_video = true);
    let mut child = match spawn_camera(&opts.camera_cmd, &rpicam_args(opts, &run_dir)) {
        Ok(child) => child,
        Err(e) => {
            status.send_modify(|s| s.recording_video = false);
            let _ = fs::remove_dir(&run_dir);
            return Err(e);
        }
    };
    let started = Instant::now();
    let mut collector = SegmentCollector::new(run_dir.clone(), clock.now(), opts.segment, opts.fps);
    let mut ticker = tokio::time::interval(opts.poll);
    let end = loop {
        tokio::select! {
            _ = ticker.tick() => {
                if let Err(e) = collector.collect(queue, false) {
                    tracing::warn!(error = %e, "collecting video segments failed");
                }
            }
            exit = child.wait() => {
                break RunEnd::Exited { ran_for: started.elapsed(), code: exit.ok().and_then(|s| s.code()) };
            }
            changed = gate.changed() => {
                if changed.is_err() {
                    stop_child(&mut child, opts.stop_grace).await;
                    break RunEnd::Shutdown;
                }
                if !*gate.borrow() {
                    stop_child(&mut child, opts.stop_grace).await;
                    break RunEnd::GateClosed;
                }
            }
            changed = shutdown.changed() => {
                if is_shutdown(&changed, shutdown) {
                    stop_child(&mut child, opts.stop_grace).await;
                    break RunEnd::Shutdown;
                }
            }
        }
    };
    let collected = collector.collect(queue, true);
    let _ = fs::remove_dir(&run_dir);
    status.send_modify(|s| s.recording_video = false);
    collected?;
    Ok(end)
}

/// Wait until recording is allowed; false means the agent is shutting down.
async fn wait_for_gate(gate: &mut watch::Receiver<bool>, shutdown: &mut watch::Receiver<bool>) -> bool {
    loop {
        if *shutdown.borrow_and_update() || shutdown.has_changed().is_err() || gate.has_changed().is_err() {
            return false;
        }
        if *gate.borrow_and_update() {
            return true;
        }
        tokio::select! {
            changed = gate.changed() => if changed.is_err() { return false; },
            changed = shutdown.changed() => if is_shutdown(&changed, shutdown) { return false; },
        }
    }
}

async fn sleep_or_shutdown(delay: Duration, shutdown: &mut watch::Receiver<bool>) -> bool {
    let deadline = Instant::now() + delay;
    loop {
        tokio::select! {
            _ = tokio::time::sleep_until(deadline) => return true,
            changed = shutdown.changed() => if is_shutdown(&changed, shutdown) { return false; },
        }
    }
}

pub async fn run_video(
    opts: VideoOptions,
    queue: Arc<Queue>,
    clock: Arc<dyn Clock>,
    mut gate: watch::Receiver<bool>,
    status: StatusTx,
    mut shutdown: watch::Receiver<bool>,
) {
    let mut failures = 0u32;
    loop {
        if !wait_for_gate(&mut gate, &mut shutdown).await {
            return;
        }
        let message = match record_once(&opts, &queue, clock.as_ref(), &mut gate, &status, &mut shutdown).await {
            Ok(RunEnd::Shutdown) => return,
            Ok(RunEnd::GateClosed) => {
                failures = 0;
                status.send_modify(|s| s.camera_failures = 0);
                continue;
            }
            Ok(RunEnd::Exited { ran_for, code }) => {
                failures = if ran_for >= opts.healthy_run { 1 } else { failures.saturating_add(1) };
                format!("rpicam-vid exited with {code:?} after {} s", ran_for.as_secs())
            }
            Err(e) => {
                failures = failures.saturating_add(1);
                format!("video recorder: {e}")
            }
        };
        tracing::warn!(%message, failures, "camera problem");
        status.send_modify(|s| {
            s.camera_failures = failures;
            s.last_error = Some(message);
        });
        if !sleep_or_shutdown(camera_backoff(failures, opts.max_backoff), &mut shutdown).await {
            return;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::control::ControlState;
    use crate::led::{led_pattern, LedPattern, CAMERA_FAILURE_LIMIT};
    use crate::status::{status_channel, AgentStatus};
    use crate::testing::SyncedClock;
    use chrono::TimeZone;
    use std::os::unix::fs::PermissionsExt;

    fn executable(dir: &Path, name: &str, body: &[u8]) -> String {
        let path = dir.join(name);
        fs::write(&path, body).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        path.to_string_lossy().into_owned()
    }

    fn fake_camera(dir: &Path) -> String {
        let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/fake-rpicam-vid.sh");
        executable(dir, "fake-rpicam-vid.sh", &fs::read(fixture).unwrap())
    }

    fn opts(dir: &Path, camera_cmd: String) -> VideoOptions {
        VideoOptions {
            camera_cmd,
            width: 640,
            height: 480,
            fps: 2,
            bitrate_bps: 500_000,
            segment: Duration::from_secs(60),
            incoming_dir: dir.join("incoming"),
            poll: Duration::from_millis(50),
            stop_grace: Duration::from_secs(2),
            max_backoff: Duration::from_millis(50),
            healthy_run: Duration::from_secs(60),
        }
    }

    fn set_mtime(path: &Path, t: DateTime<Utc>) {
        let file = fs::File::options().write(true).open(path).unwrap();
        file.set_modified(t.into()).unwrap();
    }

    fn files_under(dir: &Path) -> usize {
        if !dir.exists() {
            return 0;
        }
        fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().path())
            .map(|p| if p.is_dir() { files_under(&p) } else { 1 })
            .sum()
    }

    async fn eventually(mut check: impl FnMut() -> bool) -> bool {
        for _ in 0..100 {
            if check() {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        false
    }

    #[test]
    fn args_request_segmented_inline_h264() {
        let args = rpicam_args(&opts(Path::new("/tmp"), "rpicam-vid".into()), Path::new("/data/run"));
        let joined = args.join(" ");
        assert!(joined.starts_with("-t 0 -n --codec h264 --inline"));
        assert!(joined.contains("--intra 10 --framerate 2 --width 640 --height 480 --bitrate 500000"));
        assert!(joined.contains("--segment 60000"));
        assert_eq!(args.last().unwrap(), "/data/run/v_%06d.h264");
    }

    #[test]
    fn segment_start_survives_clock_jump() {
        let segment = chrono::Duration::seconds(60);
        let previous_end = Utc.with_ymd_and_hms(2026, 10, 14, 1, 1, 0).unwrap();
        assert_eq!(segment_start(previous_end, previous_end + segment, segment), previous_end);
        let jumped_end = previous_end - chrono::Duration::seconds(30);
        let start = segment_start(previous_end, jumped_end, segment);
        assert_eq!(start, jumped_end - segment);
        assert!(start < jumped_end);
    }

    #[test]
    fn collector_queues_closed_segments_in_order() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let run = tmp.path().join("run");
        fs::create_dir_all(&run).unwrap();
        let t0 = Utc.with_ymd_and_hms(2026, 10, 14, 1, 0, 0).unwrap();
        for i in 0..3 {
            let path = run.join(format!("v_{i:06}.h264"));
            fs::write(&path, format!("segment-{i}")).unwrap();
            set_mtime(&path, t0 + chrono::Duration::seconds(60 * (i + 1)));
        }
        let mut collector = SegmentCollector::new(run.clone(), t0, Duration::from_secs(60), 2);
        let first = collector.collect(&queue, false).unwrap();
        assert_eq!(first.len(), 2, "the newest file may still be written");
        assert_eq!(first[0].started_at, t0);
        assert_eq!(first[0].ended_at, t0 + chrono::Duration::seconds(60));
        assert_eq!(first[1].started_at, first[0].ended_at);
        assert_eq!(first[0].video_fps, Some(2));

        fs::write(run.join("v_000003.h264"), b"").unwrap();
        assert_eq!(collector.collect(&queue, false).unwrap().len(), 1);
        assert_eq!(collector.collect(&queue, true).unwrap().len(), 0, "empty segments are discarded");
        assert_eq!(files_under(&run), 0);
        assert_eq!(queue.len().unwrap(), 3);
    }

    #[test]
    fn recover_incoming_queues_leftover_segments() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let incoming = tmp.path().join("incoming");
        let run = incoming.join("01JA2B3C4D5E6F7G8H9J0KMNPQ");
        fs::create_dir_all(&run).unwrap();
        let ended = Utc.with_ymd_and_hms(2026, 10, 14, 1, 0, 0).unwrap();
        fs::write(run.join("v_000000.h264"), b"left over").unwrap();
        set_mtime(&run.join("v_000000.h264"), ended);

        assert_eq!(recover_incoming(&incoming, &queue, Duration::from_secs(60), 2).unwrap(), 1);
        let item = queue.oldest().unwrap().unwrap().meta;
        assert_eq!((item.started_at, item.ended_at), (ended - chrono::Duration::seconds(60), ended));
        assert!(!run.exists());
    }

    #[tokio::test]
    async fn closing_gate_queues_partial_segment() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let queue = Arc::new(queue);
        let status = status_channel(AgentStatus::default());
        let (gate_tx, gate_rx) = watch::channel(true);
        let (stop_tx, stop_rx) = watch::channel(false);
        let options = opts(tmp.path(), fake_camera(tmp.path()));
        let incoming = options.incoming_dir.clone();
        let task = tokio::spawn(run_video(options, queue.clone(), Arc::new(SyncedClock), gate_rx, status.clone(), stop_rx));

        assert!(eventually(|| queue.len().unwrap() >= 2).await, "segments are queued while recording");
        assert!(status.borrow().recording_video);

        gate_tx.send(false).unwrap();
        assert!(eventually(|| !status.borrow().recording_video).await, "recorder stops when the gate closes");
        let queued = queue.len().unwrap();
        assert_eq!(files_under(&incoming), 0, "the partial segment was queued, nothing left behind");
        tokio::time::sleep(Duration::from_millis(400)).await;
        assert_eq!(queue.len().unwrap(), queued, "nothing is recorded while the gate is closed");

        stop_tx.send(true).unwrap();
        task.await.unwrap();
    }

    #[tokio::test]
    async fn crashing_camera_backs_off_and_counts_failures() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let status = status_channel(AgentStatus { paired: true, clock_synced: true, ..AgentStatus::default() });
        let (_gate_tx, gate_rx) = watch::channel(true);
        let (stop_tx, stop_rx) = watch::channel(false);
        let camera = executable(tmp.path(), "broken-camera.sh", b"#!/bin/sh\nexit 1\n");
        let task = tokio::spawn(run_video(
            opts(tmp.path(), camera),
            Arc::new(queue),
            Arc::new(SyncedClock),
            gate_rx,
            status.clone(),
            stop_rx,
        ));

        assert!(eventually(|| status.borrow().camera_failures >= CAMERA_FAILURE_LIMIT).await);
        let snapshot = status.borrow().clone();
        assert!(!snapshot.recording_video);
        assert!(snapshot.last_error.as_deref().unwrap_or_default().contains("exited"));
        assert_eq!(led_pattern(&snapshot, &ControlState::initial(true)), LedPattern::FastBlink);

        stop_tx.send(true).unwrap();
        task.await.unwrap();
    }

    #[tokio::test]
    async fn recording_flag_is_set_before_the_camera_process_exists() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let status = status_channel(AgentStatus::default());
        let (_gate_tx, gate_rx) = watch::channel(true);
        let (stop_tx, stop_rx) = watch::channel(false);
        let marker = tmp.path().join("child-started");
        let script = format!("#!/bin/sh\ntouch '{}'\ntrap 'exit 0' INT TERM\nwhile :; do sleep 0.1; done\n", marker.display());
        let camera = executable(tmp.path(), "marker-camera.sh", script.as_bytes());
        let task = tokio::spawn(run_video(opts(tmp.path(), camera), Arc::new(queue), Arc::new(SyncedClock), gate_rx, status.clone(), stop_rx));

        assert!(eventually(|| marker.exists()).await, "child started");
        assert!(status.borrow().recording_video, "flag was already true when the child existed");

        stop_tx.send(true).unwrap();
        task.await.unwrap();
        assert!(!status.borrow().recording_video);
    }

    #[tokio::test]
    async fn failed_spawn_clears_recording_flag_and_counts_failure() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let status = status_channel(AgentStatus::default());
        let (_gate_tx, gate_rx) = watch::channel(true);
        let (stop_tx, stop_rx) = watch::channel(false);
        let missing = tmp.path().join("no-such-camera").to_string_lossy().into_owned();
        let task = tokio::spawn(run_video(opts(tmp.path(), missing), Arc::new(queue), Arc::new(SyncedClock), gate_rx, status.clone(), stop_rx));

        assert!(eventually(|| status.borrow().camera_failures >= 1).await);
        assert!(!status.borrow().recording_video);

        stop_tx.send(true).unwrap();
        task.await.unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn dropped_shutdown_sender_stops_the_camera_and_returns() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let queue = Arc::new(queue);
        let status = status_channel(AgentStatus::default());
        let (_gate_tx, gate_rx) = watch::channel(true);
        let (stop_tx, stop_rx) = watch::channel(false);
        let options = opts(tmp.path(), fake_camera(tmp.path()));
        let incoming = options.incoming_dir.clone();
        let task = tokio::spawn(run_video(options, queue.clone(), Arc::new(SyncedClock), gate_rx, status.clone(), stop_rx));
        assert!(eventually(|| status.borrow().recording_video).await);

        drop(stop_tx);
        let finished = tokio::time::timeout(Duration::from_secs(5), task).await;
        assert!(matches!(finished, Ok(Ok(()))), "run_video must return when the shutdown sender is dropped");
        assert!(!status.borrow().recording_video);
        assert_eq!(files_under(&incoming), 0);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn dropped_shutdown_sender_while_gate_closed_returns() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let status = status_channel(AgentStatus::default());
        let (_gate_tx, gate_rx) = watch::channel(false);
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(run_video(opts(tmp.path(), "unused".into()), Arc::new(queue), Arc::new(SyncedClock), gate_rx, status, stop_rx));
        tokio::time::sleep(Duration::from_millis(100)).await;

        drop(stop_tx);
        assert!(matches!(tokio::time::timeout(Duration::from_secs(5), task).await, Ok(Ok(()))));
    }

    #[test]
    fn camera_backoff_grows_to_the_cap() {
        let cap = Duration::from_secs(60);
        let steps: Vec<u64> = (0..9).map(|n| camera_backoff(n, cap).as_secs()).collect();
        assert_eq!(steps, vec![1, 2, 4, 8, 16, 32, 60, 60, 60]);
    }

    #[test]
    fn segment_start_is_clamped_to_contract_maximum() {
        let segment = chrono::Duration::seconds(60);
        let previous_end = Utc.with_ymd_and_hms(2026, 10, 14, 1, 0, 0).unwrap();
        let ended = previous_end + chrono::Duration::seconds(7200);
        let start = segment_start(previous_end, ended, segment);
        assert_eq!(start, ended - chrono::Duration::seconds(3600));
        assert!(start <= ended);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn dropped_gate_sender_while_recording_returns_without_respawning() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let status = status_channel(AgentStatus::default());
        let (gate_tx, gate_rx) = watch::channel(true);
        let (_stop_tx, stop_rx) = watch::channel(false);
        let spawns = tmp.path().join("spawns");
        let script = format!("#!/bin/sh\necho x >> '{}'\ntrap 'exit 0' INT TERM\nwhile :; do sleep 0.1; done\n", spawns.display());
        let camera = executable(tmp.path(), "counting-camera.sh", script.as_bytes());
        let options = opts(tmp.path(), camera);
        let incoming = options.incoming_dir.clone();
        let task = tokio::spawn(run_video(options, Arc::new(queue), Arc::new(SyncedClock), gate_rx, status.clone(), stop_rx));
        assert!(eventually(|| spawns.exists()).await);

        drop(gate_tx);
        let finished = tokio::time::timeout(Duration::from_secs(5), task).await;
        assert!(matches!(finished, Ok(Ok(()))), "run_video must return when the gate sender is dropped");
        let count = fs::read_to_string(&spawns).unwrap().lines().count();
        assert_eq!(count, 1, "camera spawned once, not in a respawn loop");
        assert!(!status.borrow().recording_video);
        assert_eq!(files_under(&incoming), 0);
        assert!(!incoming.exists() || fs::read_dir(&incoming).unwrap().next().is_none(), "no stray run dirs");
    }
}
