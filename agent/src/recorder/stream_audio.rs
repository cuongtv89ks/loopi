//! Sound from a network camera (P3c design §3): ffmpeg decodes `audio_url` (WAV or PCM over HTTP) or the camera's
//! stream to 16 kHz mono s16le on a pipe; one frame is 320 samples (640 bytes). A read waits at most `timeout` for
//! data, so a stalled network never blocks the sound thread (and the agent's shutdown). ffmpeg's error output is
//! discarded: it can quote the address with the password.
use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::process::{Child, ChildStdout, Command, Stdio};
use std::time::{Duration, Instant};

use crate::child::die_with_parent_std;
use crate::recorder::audio::{AudioSource, AudioSourceFactory};
use crate::recorder::ogg_opus::FRAME_SAMPLES;
use crate::recorder::stream::{input_args, StreamInput};

pub const READ_TIMEOUT: Duration = Duration::from_secs(10);

pub fn audio_args(url_with_credentials: &str) -> Vec<String> {
    let mut args = input_args(url_with_credentials);
    args.extend(["-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-y", "pipe:1"].map(String::from));
    args
}

pub struct StreamAudioSource {
    child: Child,
    stdout: ChildStdout,
    timeout: Duration,
}

impl StreamAudioSource {
    pub fn open(cmd: &str, input: &StreamInput, timeout: Duration) -> io::Result<Self> {
        let mut command = Command::new(cmd);
        command.args(audio_args(&input.url_with_credentials())).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null());
        die_with_parent_std(&mut command);
        let mut child = command.spawn()?;
        let stdout = child.stdout.take().ok_or_else(|| io::Error::other("ffmpeg has no stdout"))?;
        Ok(Self { child, stdout, timeout })
    }

    /// Wait until the pipe has data, at most until `deadline`.
    fn wait_readable(&self, deadline: Instant) -> io::Result<()> {
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Err(io::Error::new(io::ErrorKind::TimedOut, "the camera's sound stream stalled"));
            }
            let mut fds = libc::pollfd { fd: self.stdout.as_raw_fd(), events: libc::POLLIN, revents: 0 };
            let ms = left.as_millis().min(i32::MAX as u128) as i32;
            // SAFETY: one valid pollfd for the duration of the call.
            match unsafe { libc::poll(&mut fds, 1, ms) } {
                n if n > 0 => return Ok(()),
                0 => continue,
                _ => {
                    let e = io::Error::last_os_error();
                    if e.kind() != io::ErrorKind::Interrupted {
                        return Err(e);
                    }
                }
            }
        }
    }
}

impl AudioSource for StreamAudioSource {
    fn read_frame(&mut self, frame: &mut [i16; FRAME_SAMPLES]) -> io::Result<()> {
        let mut buf = [0u8; FRAME_SAMPLES * 2];
        let mut filled = 0;
        let deadline = Instant::now() + self.timeout;
        while filled < buf.len() {
            self.wait_readable(deadline)?;
            match self.stdout.read(&mut buf[filled..])? {
                0 => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "the camera's sound stream ended")),
                n => filled += n,
            }
        }
        for (i, sample) in frame.iter_mut().enumerate() {
            *sample = i16::from_le_bytes([buf[2 * i], buf[2 * i + 1]]);
        }
        Ok(())
    }
}

impl Drop for StreamAudioSource {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Each call (each time the sound gate opens) starts a fresh ffmpeg.
pub fn factory(cmd: String, input: StreamInput) -> AudioSourceFactory {
    Box::new(move || Ok(Box::new(StreamAudioSource::open(&cmd, &input, READ_TIMEOUT)?) as Box<dyn AudioSource>))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Secret;
    use std::os::unix::fs::PermissionsExt;

    fn script(dir: &std::path::Path, body: &str) -> String {
        let path = dir.join("fake-ffmpeg");
        std::fs::write(&path, body).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path.to_string_lossy().into_owned()
    }

    fn input() -> StreamInput {
        StreamInput { url: "http://192.0.2.10/audio".into(), username: String::new(), password: Secret::default() }
    }

    #[test]
    fn sound_is_decoded_to_16_khz_mono_on_a_pipe() {
        let args = audio_args("http://192.0.2.10/audio");
        assert_eq!(args[args.len() - 9..], ["-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-y", "pipe:1"].map(String::from));
    }

    #[test]
    fn frames_are_read_from_the_pipe() {
        let tmp = tempfile::tempdir().unwrap();
        let cmd = script(tmp.path(), "#!/bin/sh\nexec cat /dev/zero\n");
        let mut source = StreamAudioSource::open(&cmd, &input(), Duration::from_secs(2)).unwrap();
        let mut frame = [1i16; FRAME_SAMPLES];
        source.read_frame(&mut frame).unwrap();
        assert!(frame.iter().all(|s| *s == 0));
    }

    #[test]
    fn a_sound_stream_that_ends_is_an_error() {
        let tmp = tempfile::tempdir().unwrap();
        let cmd = script(tmp.path(), "#!/bin/sh\nexit 1\n");
        let mut source = StreamAudioSource::open(&cmd, &input(), Duration::from_secs(2)).unwrap();
        let err = source.read_frame(&mut [0i16; FRAME_SAMPLES]).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::UnexpectedEof);
    }

    #[test]
    fn a_stalled_sound_stream_times_out() {
        let tmp = tempfile::tempdir().unwrap();
        let cmd = script(tmp.path(), "#!/bin/sh\nexec sleep 30\n");
        let mut source = StreamAudioSource::open(&cmd, &input(), Duration::from_millis(300)).unwrap();
        let started = std::time::Instant::now();
        let err = source.read_frame(&mut [0i16; FRAME_SAMPLES]).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::TimedOut);
        assert!(started.elapsed() < Duration::from_secs(5));
    }
}
