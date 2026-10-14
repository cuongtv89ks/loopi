//! Audio capture: 20 ms frames from a source → wall-clock-aligned Ogg Opus segments.
use std::fs::{self, File};
use std::io::{self, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Utc};
use tokio::sync::watch;

use crate::clock::Clock;
use crate::item::ItemKind;
use crate::queue::Queue;
use crate::recorder::ogg_opus::{OggOpusWriter, FRAME_SAMPLES};
use crate::recorder::video::{segment_start, to_chrono};
use crate::status::StatusTx;

const GATE_POLL: Duration = Duration::from_millis(200);
const SHUTDOWN_POLL: Duration = Duration::from_millis(100);
const PART_SUFFIX: &str = ".opus.part";

pub trait AudioSource: Send {
    /// Block until one 20 ms mono frame at 16 kHz is available.
    fn read_frame(&mut self, frame: &mut [i16; FRAME_SAMPLES]) -> io::Result<()>;
}

pub type AudioSourceFactory = Box<dyn Fn() -> io::Result<Box<dyn AudioSource>> + Send + Sync>;

#[derive(Debug, Clone)]
pub struct AudioOptions {
    pub segment: Duration,
    pub incoming_dir: PathBuf,
    pub retry: Duration,
}

/// First USB audio card in `/proc/asound/cards` (" 1 [Device ]: USB-Audio - ...").
pub fn parse_usb_card(cards: &str) -> Option<u32> {
    cards
        .lines()
        .filter(|line| line.contains("USB-Audio"))
        .find_map(|line| line.split_whitespace().next()?.parse().ok())
}

/// `plughw` lets ALSA convert rate/channels when the mic does not support 16 kHz mono natively.
pub fn resolve_device(configured: &str, proc_cards: &str) -> Option<String> {
    if configured != "auto" {
        return Some(configured.to_string());
    }
    parse_usb_card(proc_cards).map(|card| format!("plughw:{card},0"))
}

/// Next multiple of `segment` (since the Unix epoch) strictly after `t`.
pub fn next_boundary(t: DateTime<Utc>, segment: Duration) -> DateTime<Utc> {
    let seconds = segment.as_secs().max(1) as i64;
    let next = (t.timestamp().div_euclid(seconds) + 1) * seconds;
    DateTime::from_timestamp(next, 0).expect("timestamp in range")
}

/// Queue `.opus.part` files left by a crash; they lack the end-of-stream page but still decode.
pub fn recover_incoming(dir: &Path, queue: &Queue, segment: Duration) -> io::Result<u32> {
    if !dir.exists() {
        return Ok(0);
    }
    let segment = to_chrono(segment);
    let mut recovered = 0;
    for entry in fs::read_dir(dir)? {
        let path = entry?.path();
        let is_part = path.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.ends_with(PART_SUFFIX));
        if !is_part {
            continue;
        }
        // Audio is never deleted automatically: a part file that cannot be read or queued stays
        // where it is (and is retried on the next start) after the error is logged.
        match recover_part(&path, queue, segment) {
            Ok(true) => recovered += 1,
            Ok(false) => {}
            Err(error) => tracing::error!(path = %path.display(), %error, "cannot recover partial audio segment; keeping file"),
        }
    }
    Ok(recovered)
}

/// `Ok(false)` for an empty file (nothing to keep, so it is removed).
fn recover_part(path: &Path, queue: &Queue, segment: chrono::Duration) -> io::Result<bool> {
    let metadata = fs::metadata(path)?;
    if metadata.len() == 0 {
        fs::remove_file(path)?;
        return Ok(false);
    }
    let ended: DateTime<Utc> = metadata.modified()?.into();
    let started = segment_start(ended - segment, ended, segment);
    queue.enqueue_file(path, ItemKind::Audio, started, ended, None)?;
    Ok(true)
}

/// Record consecutive segments until the gate closes, shutdown, or a read error (returned
/// after the current partial segment has been queued).
fn record_segments(
    source: &mut dyn AudioSource,
    opts: &AudioOptions,
    queue: &Queue,
    clock: &dyn Clock,
    gate: &watch::Receiver<bool>,
    shutdown: &watch::Receiver<bool>,
) -> io::Result<()> {
    fs::create_dir_all(&opts.incoming_dir)?;
    let segment = to_chrono(opts.segment);
    let mut frame = [0i16; FRAME_SAMPLES];
    loop {
        let started = clock.now();
        let boundary = next_boundary(started, opts.segment);
        let part = opts.incoming_dir.join(format!("{}{PART_SUFFIX}", ulid::Ulid::new()));
        let serial = (ulid::Ulid::new().random() & 0xffff_ffff) as u32;
        let mut writer = OggOpusWriter::new(BufWriter::new(File::create(&part)?), serial).map_err(io::Error::other)?;
        let (stop, read_result) = loop {
            if should_stop(gate, shutdown) {
                break (true, Ok(()));
            }
            if let Err(e) = source.read_frame(&mut frame) {
                break (true, Err(e));
            }
            writer.write_frame(&frame).map_err(io::Error::other)?;
            if clock.now() >= boundary {
                break (false, Ok(()));
            }
        };
        let frames = writer.frames();
        let mut out = writer.finish().map_err(io::Error::other)?;
        out.flush()?;
        out.get_ref().sync_all()?;
        drop(out);
        if frames > 0 {
            // Same ordering and 3600 s contract clamp as video segments.
            let ended = clock.now();
            let started = segment_start(started, ended, segment);
            queue.enqueue_file(&part, ItemKind::Audio, started, ended, None)?;
        } else {
            fs::remove_file(&part)?;
        }
        read_result?;
        if stop {
            return Ok(());
        }
    }
}

/// Stop when shutdown is requested, the gate closes, or either sender is gone (nobody can
/// ever reopen the gate or request shutdown again).
fn should_stop(gate: &watch::Receiver<bool>, shutdown: &watch::Receiver<bool>) -> bool {
    shutdown_requested(shutdown) || gate.has_changed().is_err() || !*gate.borrow()
}

fn shutdown_requested(shutdown: &watch::Receiver<bool>) -> bool {
    shutdown.has_changed().is_err() || *shutdown.borrow()
}

fn report_mic_error(status: &StatusTx, error: &io::Error) {
    tracing::warn!(%error, "microphone unavailable");
    status.send_modify(|s| {
        s.mic_missing = true;
        s.last_error = Some(format!("microphone: {error}"));
    });
}

fn sleep_unless_shutdown(total: Duration, shutdown: &watch::Receiver<bool>) {
    let mut waited = Duration::ZERO;
    while waited < total && !shutdown_requested(shutdown) {
        std::thread::sleep(SHUTDOWN_POLL.min(total - waited));
        waited += SHUTDOWN_POLL;
    }
}

/// Blocking loop for a dedicated thread. `recording_audio` is raised *before* the device is
/// opened so the LED is never off while the microphone could be capturing.
pub fn run_audio_blocking(
    factory: AudioSourceFactory,
    opts: AudioOptions,
    queue: Arc<Queue>,
    clock: Arc<dyn Clock>,
    gate: watch::Receiver<bool>,
    status: StatusTx,
    shutdown: watch::Receiver<bool>,
) {
    loop {
        if shutdown_requested(&shutdown) || gate.has_changed().is_err() {
            return;
        }
        if !*gate.borrow() {
            std::thread::sleep(GATE_POLL);
            continue;
        }
        status.send_modify(|s| s.recording_audio = true);
        let result = match factory() {
            Ok(mut source) => {
                status.send_modify(|s| s.mic_missing = false);
                record_segments(source.as_mut(), &opts, &queue, clock.as_ref(), &gate, &shutdown)
            }
            Err(e) => Err(e),
        };
        status.send_modify(|s| s.recording_audio = false);
        if let Err(e) = result {
            report_mic_error(&status, &e);
            sleep_unless_shutdown(opts.retry, &shutdown);
        }
    }
}

#[cfg(target_os = "linux")]
pub mod alsa_source {
    //! ALSA capture from the USB microphone.
    use std::io;

    use alsa::pcm::{Access, Format, HwParams, PCM};
    use alsa::{Direction, ValueOr};

    use super::{resolve_device, AudioSource, AudioSourceFactory};
    use crate::recorder::ogg_opus::{FRAME_SAMPLES, SAMPLE_RATE};

    fn to_io(e: alsa::Error) -> io::Error {
        io::Error::other(e)
    }

    pub struct AlsaSource {
        pcm: PCM,
    }

    impl AlsaSource {
        pub fn open(device: &str) -> io::Result<Self> {
            let pcm = PCM::new(device, Direction::Capture, false).map_err(to_io)?;
            {
                let hw = HwParams::any(&pcm).map_err(to_io)?;
                hw.set_channels(1).map_err(to_io)?;
                hw.set_rate(SAMPLE_RATE, ValueOr::Nearest).map_err(to_io)?;
                hw.set_format(Format::s16()).map_err(to_io)?;
                hw.set_access(Access::RWInterleaved).map_err(to_io)?;
                pcm.hw_params(&hw).map_err(to_io)?;
            }
            pcm.prepare().map_err(to_io)?;
            Ok(Self { pcm })
        }
    }

    impl AudioSource for AlsaSource {
        fn read_frame(&mut self, frame: &mut [i16; FRAME_SAMPLES]) -> io::Result<()> {
            let io = self.pcm.io_i16().map_err(to_io)?;
            let mut filled = 0;
            while filled < FRAME_SAMPLES {
                match io.readi(&mut frame[filled..]) {
                    Ok(n) => filled += n,
                    // Overrun (EPIPE): recover and keep going; anything else means the mic is gone.
                    Err(e) if e.errno() == libc::EPIPE => self.pcm.try_recover(e, true).map_err(to_io)?,
                    Err(e) => return Err(to_io(e)),
                }
            }
            Ok(())
        }
    }

    pub fn factory(device_config: String) -> AudioSourceFactory {
        Box::new(move || {
            let cards = std::fs::read_to_string("/proc/asound/cards").unwrap_or_default();
            let device = resolve_device(&device_config, &cards)
                .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no USB microphone found"))?;
            Ok(Box::new(AlsaSource::open(&device)?) as Box<dyn AudioSource>)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::status::{status_channel, AgentStatus};
    use crate::testing::{ManualClock, SyncedClock, ToneSource};
    use chrono::TimeZone;
    use std::sync::atomic::{AtomicU32, Ordering};

    const CARDS: &str = " 0 [b1             ]: bcm2835_hdmi - bcm2835 HDMI 1\n                      bcm2835 HDMI 1\n \
1 [Device         ]: USB-Audio - USB PnP Sound Device\n                      C-Media USB PnP Sound Device at usb-20980000.usb-1\n";

    #[test]
    fn usb_card_is_found() {
        assert_eq!(parse_usb_card(CARDS), Some(1));
        assert_eq!(parse_usb_card(" 0 [b1 ]: bcm2835_hdmi - bcm2835 HDMI 1\n"), None);
        assert_eq!(resolve_device("auto", CARDS).as_deref(), Some("plughw:1,0"));
        assert_eq!(resolve_device("hw:2,0", "").as_deref(), Some("hw:2,0"));
        assert_eq!(resolve_device("auto", ""), None);
    }

    #[test]
    fn boundaries_align_to_the_segment_grid() {
        let t = Utc.with_ymd_and_hms(2026, 10, 14, 12, 0, 30).unwrap();
        assert_eq!(next_boundary(t, Duration::from_secs(60)), Utc.with_ymd_and_hms(2026, 10, 14, 12, 1, 0).unwrap());
        let on_grid = Utc.with_ymd_and_hms(2026, 10, 14, 12, 1, 0).unwrap();
        assert_eq!(next_boundary(on_grid, Duration::from_secs(60)), Utc.with_ymd_and_hms(2026, 10, 14, 12, 2, 0).unwrap());
    }

    #[test]
    fn segments_follow_the_wall_clock_and_end_on_mic_loss() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let t0 = Utc.with_ymd_and_hms(2026, 10, 14, 1, 0, 0).unwrap();
        let clock = Arc::new(ManualClock::new(t0));
        let mut source = ToneSource::stepping(clock.clone(), 120);
        let opts = AudioOptions { segment: Duration::from_secs(1), incoming_dir: tmp.path().join("in"), retry: Duration::from_millis(10) };
        let (_gate_tx, gate) = watch::channel(true);
        let (_stop_tx, stop) = watch::channel(false);

        let result = record_segments(&mut source, &opts, &queue, clock.as_ref(), &gate, &stop);
        assert!(result.is_err(), "the read error is reported after the partial segment is saved");

        let mut items = Vec::new();
        while let Some(item) = queue.oldest().unwrap() {
            queue.remove(&item.meta).unwrap();
            items.push(item.meta);
        }
        // ULIDs created within the same millisecond are not ordered, so sort by capture time.
        items.sort_by_key(|m| m.started_at);
        let spans: Vec<_> = items.iter().map(|m| (m.started_at, m.ended_at)).collect();
        let ms = |n| t0 + chrono::Duration::milliseconds(n);
        assert_eq!(spans, vec![(ms(0), ms(1000)), (ms(1000), ms(2000)), (ms(2000), ms(2400))]);
        assert!(items.iter().all(|m| m.kind == ItemKind::Audio));
        assert_eq!(std::fs::read_dir(tmp.path().join("in")).unwrap().count(), 0);
    }

    #[test]
    fn recover_incoming_queues_partial_files() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let dir = tmp.path().join("in");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("01JA2B3C4D5E6F7G8H9J0KMNPQ.opus.part"), b"OggS").unwrap();
        assert_eq!(recover_incoming(&dir, &queue, Duration::from_secs(60)).unwrap(), 1);
        assert_eq!(queue.oldest().unwrap().unwrap().meta.kind, ItemKind::Audio);
    }

    #[test]
    fn missing_mic_is_reported_then_recording_resumes() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let queue = Arc::new(queue);
        let status = status_channel(AgentStatus::default());
        let (gate_tx, gate) = watch::channel(true);
        let (stop_tx, stop) = watch::channel(false);
        let calls = Arc::new(AtomicU32::new(0));
        let calls_in = calls.clone();
        let factory: AudioSourceFactory = Box::new(move || {
            if calls_in.fetch_add(1, Ordering::SeqCst) == 0 {
                Err(io::Error::new(io::ErrorKind::NotFound, "no USB microphone found"))
            } else {
                Ok(Box::new(ToneSource::realtime()) as Box<dyn AudioSource>)
            }
        });
        let opts = AudioOptions { segment: Duration::from_secs(1), incoming_dir: tmp.path().join("in"), retry: Duration::from_millis(50) };
        let (q, s) = (queue.clone(), status.clone());
        let worker = std::thread::spawn(move || run_audio_blocking(factory, opts, q, Arc::new(SyncedClock), gate, s, stop));

        let wait = |check: &dyn Fn(&AgentStatus) -> bool| {
            for _ in 0..100 {
                if check(&status.borrow()) {
                    return true;
                }
                std::thread::sleep(Duration::from_millis(30));
            }
            false
        };
        assert!(wait(&|s| s.recording_audio && !s.mic_missing), "recording resumes after the retry");
        assert!(status.borrow().last_error.as_deref().unwrap_or_default().contains("microphone"));
        std::thread::sleep(Duration::from_millis(200));
        gate_tx.send(false).unwrap();
        assert!(wait(&|s| !s.recording_audio));
        assert!(queue.len().unwrap() >= 1, "the partial segment is queued when the gate closes");
        stop_tx.send(true).unwrap();
        worker.join().unwrap();
        assert!(calls.load(Ordering::SeqCst) >= 2);
    }

    fn drain(queue: &Queue) -> Vec<crate::item::ItemMeta> {
        let mut items = Vec::new();
        while let Some(item) = queue.oldest().unwrap() {
            queue.remove(&item.meta).unwrap();
            items.push(item.meta);
        }
        items
    }

    /// Moves the clock by `jump` on the first read, then fails.
    struct JumpSource {
        clock: Arc<ManualClock>,
        jump: chrono::Duration,
        done: bool,
    }

    impl AudioSource for JumpSource {
        fn read_frame(&mut self, _frame: &mut [i16; FRAME_SAMPLES]) -> io::Result<()> {
            if self.done {
                return Err(io::Error::other("gone"));
            }
            self.done = true;
            self.clock.advance(self.jump);
            Ok(())
        }
    }

    fn record_with_jump(jump: chrono::Duration) -> Vec<crate::item::ItemMeta> {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let clock = Arc::new(ManualClock::new(Utc.with_ymd_and_hms(2026, 10, 14, 1, 0, 0).unwrap()));
        let mut source = JumpSource { clock: clock.clone(), jump, done: false };
        let opts = AudioOptions { segment: Duration::from_secs(60), incoming_dir: tmp.path().join("in"), retry: Duration::from_millis(10) };
        let (_g, gate) = watch::channel(true);
        let (_s, stop) = watch::channel(false);
        assert!(record_segments(&mut source, &opts, &queue, clock.as_ref(), &gate, &stop).is_err());
        drain(&queue)
    }

    #[test]
    fn segment_length_is_clamped_to_the_contract_maximum() {
        let items = record_with_jump(chrono::Duration::hours(2));
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].ended_at - items[0].started_at, chrono::Duration::seconds(3600));
    }

    #[test]
    fn backward_clock_jump_never_ends_before_start() {
        let items = record_with_jump(chrono::Duration::seconds(-10));
        assert_eq!(items.len(), 1);
        assert!(items[0].ended_at >= items[0].started_at);
    }

    #[test]
    fn unreadable_part_file_is_kept_and_others_are_still_recovered() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let dir = tmp.path().join("in");
        // A directory with a .opus.part name cannot be hashed, so queueing it fails.
        let bad = dir.join("01JA2B3C4D5E6F7G8H9J0KMNPR.opus.part");
        std::fs::create_dir_all(&bad).unwrap();
        std::fs::write(dir.join("01JA2B3C4D5E6F7G8H9J0KMNPQ.opus.part"), b"OggS").unwrap();
        assert_eq!(recover_incoming(&dir, &queue, Duration::from_secs(60)).unwrap(), 1);
        assert!(bad.exists(), "unrecoverable audio is not deleted");
        assert_eq!(queue.len().unwrap(), 1);
    }

    #[test]
    fn empty_part_file_is_dropped() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let dir = tmp.path().join("in");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("01JA2B3C4D5E6F7G8H9J0KMNPQ.opus.part"), b"").unwrap();
        assert_eq!(recover_incoming(&dir, &queue, Duration::from_secs(60)).unwrap(), 0);
        assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 0);
    }

    #[test]
    fn recording_flag_is_raised_before_the_device_opens_and_cleared_on_open_failure() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let status = status_channel(AgentStatus::default());
        let (_gate_tx, gate) = watch::channel(true);
        let (stop_tx, stop) = watch::channel(false);
        let seen_during_open = Arc::new(std::sync::Mutex::new(Vec::new()));
        let (seen, st) = (seen_during_open.clone(), status.clone());
        let factory: AudioSourceFactory = Box::new(move || {
            seen.lock().unwrap().push(st.borrow().recording_audio);
            Err(io::Error::new(io::ErrorKind::NotFound, "no USB microphone found"))
        });
        let opts = AudioOptions { segment: Duration::from_secs(1), incoming_dir: tmp.path().join("in"), retry: Duration::from_millis(20) };
        let (q, s) = (Arc::new(queue), status.clone());
        let worker = std::thread::spawn(move || run_audio_blocking(factory, opts, q, Arc::new(SyncedClock), gate, s, stop));
        std::thread::sleep(Duration::from_millis(150));
        stop_tx.send(true).unwrap();
        worker.join().unwrap();
        let seen = seen_during_open.lock().unwrap();
        assert!(seen.len() >= 2 && seen.iter().all(|raised| *raised), "flag is up whenever the device is being opened");
        assert!(!status.borrow().recording_audio, "flag is down after a failed open");
        assert!(status.borrow().mic_missing);
    }

    #[test]
    fn dropped_senders_stop_the_worker_instead_of_spinning() {
        for drop_gate in [true, false] {
            let tmp = tempfile::tempdir().unwrap();
            let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
            let status = status_channel(AgentStatus::default());
            let (gate_tx, gate) = watch::channel(true);
            let (stop_tx, stop) = watch::channel(false);
            let factory: AudioSourceFactory = Box::new(|| Ok(Box::new(ToneSource::realtime()) as Box<dyn AudioSource>));
            let opts = AudioOptions { segment: Duration::from_secs(1), incoming_dir: tmp.path().join("in"), retry: Duration::from_millis(20) };
            let (q, s) = (Arc::new(queue), status.clone());
            let worker = std::thread::spawn(move || run_audio_blocking(factory, opts, q, Arc::new(SyncedClock), gate, s, stop));
            std::thread::sleep(Duration::from_millis(100));
            if drop_gate {
                drop(gate_tx);
                std::mem::forget(stop_tx);
            } else {
                drop(stop_tx);
                std::mem::forget(gate_tx);
            }
            worker.join().unwrap();
            assert!(!status.borrow().recording_audio);
        }
    }
}
