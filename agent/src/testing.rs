//! Test doubles shared by unit tests and `tests/*.rs`. Not used by the production binary.
use std::fs;
use std::io;
use std::path::PathBuf;
use std::sync::Mutex;

use crate::disk::DiskUsage;

/// Disk usage fixed at a settable percentage.
pub struct FixedDisk(pub Mutex<f32>);

impl FixedDisk {
    pub fn new(pct: f32) -> Self {
        Self(Mutex::new(pct))
    }
}

impl DiskUsage for FixedDisk {
    fn used_pct(&self) -> io::Result<f32> {
        Ok(*self.0.lock().unwrap())
    }
}

/// Usage = `base + per_item × (queued items in dir)`, so deleting items frees "space".
pub struct CountingDisk {
    pub dir: PathBuf,
    pub base: f32,
    pub per_item: f32,
}

impl DiskUsage for CountingDisk {
    fn used_pct(&self) -> io::Result<f32> {
        let items = fs::read_dir(&self.dir)?
            .filter_map(Result::ok)
            .filter(|e| e.path().extension().is_some_and(|x| x == "meta"))
            .count();
        Ok(self.base + items as f32 * self.per_item)
    }
}

use chrono::{DateTime, Utc};
use std::sync::atomic::{AtomicBool, Ordering};

use crate::clock::Clock;

/// A clock that only moves when told to.
pub struct ManualClock {
    now: Mutex<DateTime<Utc>>,
    synced: AtomicBool,
}

impl ManualClock {
    pub fn new(now: DateTime<Utc>) -> Self {
        Self { now: Mutex::new(now), synced: AtomicBool::new(true) }
    }

    pub fn advance(&self, by: chrono::Duration) {
        let mut now = self.now.lock().unwrap();
        *now += by;
    }

    pub fn set_synced(&self, synced: bool) {
        self.synced.store(synced, Ordering::SeqCst);
    }
}

impl Clock for ManualClock {
    fn now(&self) -> DateTime<Utc> {
        *self.now.lock().unwrap()
    }

    fn is_synced(&self) -> bool {
        self.synced.load(Ordering::SeqCst)
    }
}

/// Real wall clock that always reports NTP as synchronised.
pub struct SyncedClock;

impl Clock for SyncedClock {
    fn now(&self) -> DateTime<Utc> {
        Utc::now()
    }

    fn is_synced(&self) -> bool {
        true
    }
}

use std::sync::Arc;

use crate::led::LedDriver;

/// LED that records every level it was set to.
#[derive(Default, Clone)]
pub struct RecordingLed {
    pub states: Arc<Mutex<Vec<bool>>>,
}

impl LedDriver for RecordingLed {
    fn set(&mut self, on: bool) -> io::Result<()> {
        self.states.lock().unwrap().push(on);
        Ok(())
    }
}

use std::collections::VecDeque;
use std::path::Path;

use async_trait::async_trait;

use crate::item::ItemMeta;
use crate::transport::{PutOutcome, Transport, TransportError};
use crate::wire::{HeartbeatRequest, RemoteCommand};

/// Scripted brain. Unscripted calls succeed (`Created` / no commands).
#[derive(Default)]
pub struct FakeTransport {
    pub put_results: Mutex<VecDeque<Result<PutOutcome, TransportError>>>,
    pub heartbeat_results: Mutex<VecDeque<Result<Vec<RemoteCommand>, TransportError>>>,
    pub puts: Mutex<Vec<ItemMeta>>,
    pub heartbeats: Mutex<Vec<HeartbeatRequest>>,
}

impl FakeTransport {
    pub fn push_put(&self, result: Result<PutOutcome, TransportError>) {
        self.put_results.lock().unwrap().push_back(result);
    }

    pub fn push_heartbeat(&self, result: Result<Vec<RemoteCommand>, TransportError>) {
        self.heartbeat_results.lock().unwrap().push_back(result);
    }
}

#[async_trait]
impl Transport for FakeTransport {
    async fn put_item(&self, meta: &ItemMeta, data: &Path) -> Result<PutOutcome, TransportError> {
        if !data.exists() {
            return Err(TransportError::Io(io::Error::new(io::ErrorKind::NotFound, "payload missing")));
        }
        self.puts.lock().unwrap().push(meta.clone());
        self.put_results.lock().unwrap().pop_front().unwrap_or(Ok(PutOutcome::Created))
    }

    async fn heartbeat(&self, request: &HeartbeatRequest) -> Result<Vec<RemoteCommand>, TransportError> {
        self.heartbeats.lock().unwrap().push(request.clone());
        self.heartbeat_results.lock().unwrap().pop_front().unwrap_or(Ok(Vec::new()))
    }
}

use crate::recorder::audio::AudioSource;
use crate::recorder::ogg_opus::{FRAME_SAMPLES, SAMPLE_RATE};

/// 440 Hz tone. `stepping` advances a ManualClock by 20 ms per frame and fails after `frames`
/// (simulating an unplugged mic); `realtime` sleeps 20 ms per frame and never fails.
pub struct ToneSource {
    frames_left: Option<u64>,
    step_clock: Option<Arc<ManualClock>>,
    realtime: bool,
    sample: u64,
}

impl ToneSource {
    pub fn stepping(clock: Arc<ManualClock>, frames: u64) -> Self {
        Self { frames_left: Some(frames), step_clock: Some(clock), realtime: false, sample: 0 }
    }

    pub fn realtime() -> Self {
        Self { frames_left: None, step_clock: None, realtime: true, sample: 0 }
    }
}

impl AudioSource for ToneSource {
    fn read_frame(&mut self, frame: &mut [i16; FRAME_SAMPLES]) -> io::Result<()> {
        if let Some(left) = self.frames_left.as_mut() {
            if *left == 0 {
                return Err(io::Error::new(io::ErrorKind::BrokenPipe, "microphone unplugged"));
            }
            *left -= 1;
        }
        for value in frame.iter_mut() {
            let t = self.sample as f32 / SAMPLE_RATE as f32;
            *value = ((t * 440.0 * std::f32::consts::TAU).sin() * 8000.0) as i16;
            self.sample += 1;
        }
        if let Some(clock) = &self.step_clock {
            clock.advance(chrono::Duration::milliseconds(20));
        }
        if self.realtime {
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        Ok(())
    }
}
