//! A network camera's stream for ffmpeg (P3c design §3, D30): its address with credentials, how ffmpeg reads it,
//! how it is re-encoded into rpicam-vid-like segments, and what its error output means — in words without the address,
//! because ffmpeg quotes the URL (with the password) in its errors.
use std::path::Path;
use std::time::Duration;

use crate::config::{CaptureConfig, Secret};
use crate::recorder::video::SEGMENT_PATTERN;

/// No data for 10 s ends ffmpeg (microseconds for -timeout and -rw_timeout); the recorder then restarts it.
const STREAM_TIMEOUT_US: &str = "10000000";
/// The long side of a network camera's picture, in pixels (720p for 16:9; smaller pictures keep their size).
pub const MAX_LONG_SIDE: u32 = 1280;
/// An I-frame every 5 s, as the Pi's camera.
const INTRA_SECONDS: u32 = 5;

#[derive(Clone, PartialEq, Eq)]
pub struct StreamInput {
    pub url: String,
    pub username: String,
    pub password: Secret,
}

impl std::fmt::Debug for StreamInput {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("StreamInput").field("url", &self.url).field("username", &self.username).finish_non_exhaustive()
    }
}

impl StreamInput {
    /// The address ffmpeg opens: `scheme://user:password@rest`, both percent-encoded; the address as it is without a
    /// user name.
    pub fn url_with_credentials(&self) -> String {
        if self.username.is_empty() {
            return self.url.clone();
        }
        let (scheme, rest) = self.url.split_once("://").unwrap_or(("", &self.url));
        format!(
            "{scheme}://{}:{}@{rest}",
            percent_encode(&self.username),
            percent_encode(self.password.expose())
        )
    }
}

/// Everything except the URI "unreserved" characters (RFC 3986), byte by byte.
fn percent_encode(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for b in text.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// The camera's picture.
pub fn video_input(capture: &CaptureConfig) -> StreamInput {
    StreamInput {
        url: capture.stream_url.clone(),
        username: capture.stream_username.clone(),
        password: capture.stream_password.clone(),
    }
}

/// The camera's sound: `audio_url` when the picture stream carries none (the Loopi ESP32 firmware), else the stream.
pub fn audio_input(capture: &CaptureConfig) -> StreamInput {
    StreamInput { url: capture.audio_url.clone().unwrap_or_else(|| capture.stream_url.clone()), ..video_input(capture) }
}

/// How ffmpeg opens a stream: RTSP over TCP; MJPEG over HTTP has no timestamps, so frames are timed when they arrive.
pub fn input_args(url_with_credentials: &str) -> Vec<String> {
    let mut args: Vec<String> = ["-nostdin", "-hide_banner", "-loglevel", "error"].map(String::from).to_vec();
    if url_with_credentials.starts_with("rtsp://") {
        args.extend(["-rtsp_transport", "tcp", "-timeout", STREAM_TIMEOUT_US].map(String::from));
    } else {
        args.extend(["-rw_timeout", STREAM_TIMEOUT_US, "-use_wallclock_as_timestamps", "1"].map(String::from));
    }
    args.extend(["-i".to_string(), url_with_credentials.to_string()]);
    args
}

/// Segments like rpicam-vid's (`v_%06d.h264`, raw H.264): `fps` frames a second, the long side at most
/// MAX_LONG_SIDE, about `bitrate_bps`, an IDR frame with SPS/PPS at the start of every segment so each one decodes
/// on its own (the brain remuxes each segment alone).
pub fn video_args(input: &StreamInput, fps: u32, bitrate_bps: u32, segment: Duration, run_dir: &Path) -> Vec<String> {
    let seconds = segment.as_secs().max(1);
    let gop = (fps * INTRA_SECONDS).to_string();
    let bps = bitrate_bps.to_string();
    let mut args = input_args(&input.url_with_credentials());
    args.extend([
        "-an".to_string(),
        "-vf".to_string(),
        format!(
            "fps={fps},scale=w='min({MAX_LONG_SIDE},iw)':h='min({MAX_LONG_SIDE},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
        ),
    ]);
    args.extend(
        ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p"].map(String::from),
    );
    args.extend(["-b:v".into(), bps.clone(), "-maxrate".into(), bps, "-bufsize".into(), (u64::from(bitrate_bps) * 2).to_string()]);
    args.extend(["-g".into(), gop.clone(), "-keyint_min".into(), gop, "-sc_threshold".into(), "0".into()]);
    args.extend(["-force_key_frames".into(), format!("expr:gte(t,n_forced*{seconds})")]);
    args.extend(["-x264-params", "repeat-headers=1"].map(String::from));
    args.extend(["-f".into(), "segment".into(), "-segment_time".into(), seconds.to_string()]);
    args.extend(["-segment_format", "h264", "-reset_timestamps", "1"].map(String::from));
    args.push(run_dir.join(SEGMENT_PATTERN).to_string_lossy().into_owned());
    args
}

/// What a line of ffmpeg's error output says, without repeating it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StreamProblem {
    SignInRefused,
    NotFound,
    Refused,
    TimedOut,
    Other,
}

impl StreamProblem {
    /// Words for `last_error` (English, like the agent's other messages); never the address.
    pub fn describe(self) -> &'static str {
        match self {
            Self::SignInRefused => "the camera refused the user name or password",
            Self::NotFound => "the stream address was not found on the camera",
            Self::Refused => "the camera refused the connection (check the port)",
            Self::TimedOut => "the camera did not answer (network or address)",
            Self::Other => "the stream could not be read",
        }
    }
}

pub fn classify_stream_error(line: &str) -> Option<StreamProblem> {
    let l = line.to_ascii_lowercase();
    if l.trim().is_empty() {
        None
    } else if l.contains("401") || l.contains("403") || l.contains("unauthorized") || l.contains("authorization failed") {
        Some(StreamProblem::SignInRefused)
    } else if l.contains("404") || l.contains("not found") {
        Some(StreamProblem::NotFound)
    } else if l.contains("connection refused") {
        Some(StreamProblem::Refused)
    } else if l.contains("timed out") || l.contains("timeout") || l.contains("no route to host") {
        Some(StreamProblem::TimedOut)
    } else {
        Some(StreamProblem::Other)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CaptureConfig, CaptureSource, Secret};

    fn input(url: &str, user: &str, pass: &str) -> StreamInput {
        StreamInput { url: url.into(), username: user.into(), password: Secret(pass.into()) }
    }

    #[test]
    fn credentials_go_into_the_url_percent_encoded() {
        assert_eq!(input("rtsp://192.0.2.10:554/cam", "admin", "AB12").url_with_credentials(), "rtsp://admin:AB12@192.0.2.10:554/cam");
        assert_eq!(input("http://192.0.2.10/s", "u@x", "p:w/ ").url_with_credentials(), "http://u%40x:p%3Aw%2F%20@192.0.2.10/s");
        assert_eq!(input("http://192.0.2.10/s", "", "").url_with_credentials(), "http://192.0.2.10/s");
    }

    #[test]
    fn the_password_never_shows_in_debug_output() {
        let shown = format!("{:?}", input("rtsp://192.0.2.10/cam", "admin", "S3cr3t"));
        assert!(!shown.contains("S3cr3t") && shown.contains("rtsp://192.0.2.10/cam"), "{shown}");
    }

    #[test]
    fn rtsp_goes_over_tcp_and_mjpeg_is_timed_on_arrival() {
        let rtsp = input_args("rtsp://192.0.2.10/cam");
        assert!(rtsp.windows(2).any(|w| w == ["-rtsp_transport", "tcp"]));
        assert!(rtsp.windows(2).any(|w| w == ["-timeout", "10000000"]));
        assert_eq!(rtsp[rtsp.len() - 2..], ["-i".to_string(), "rtsp://192.0.2.10/cam".to_string()]);
        let http = input_args("http://192.0.2.10/stream");
        assert!(http.windows(2).any(|w| w == ["-rw_timeout", "10000000"]));
        assert!(http.windows(2).any(|w| w == ["-use_wallclock_as_timestamps", "1"]));
        assert!(!http.iter().any(|a| a == "-rtsp_transport"));
    }

    #[test]
    fn video_args_make_rpicam_like_segments() {
        let args = video_args(&input("rtsp://192.0.2.10/cam", "admin", "pw"), 2, 400_000, Duration::from_secs(60), Path::new("/q/run"));
        let s = args.join(" ");
        assert!(s.contains("-an "), "{s}");
        assert!(s.contains("-vf fps=2,scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"), "{s}");
        assert!(s.contains("-c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p"), "{s}");
        assert!(s.contains("-b:v 400000 -maxrate 400000 -bufsize 800000"), "{s}");
        assert!(s.contains("-g 10 -keyint_min 10 -sc_threshold 0 -force_key_frames expr:gte(t,n_forced*60)"), "{s}");
        assert!(s.contains("-x264-params repeat-headers=1"), "{s}");
        assert!(s.ends_with("-f segment -segment_time 60 -segment_format h264 -reset_timestamps 1 /q/run/v_%06d.h264"), "{s}");
    }

    #[test]
    fn sign_in_and_network_errors_are_named_without_the_address() {
        let line = "[rtsp @ 0x55] method DESCRIBE failed: 401 Unauthorized (rtsp://admin:S3cr3t@192.0.2.10/cam)";
        assert_eq!(classify_stream_error(line), Some(StreamProblem::SignInRefused));
        assert_eq!(classify_stream_error("Server returned 404 Not Found"), Some(StreamProblem::NotFound));
        assert_eq!(classify_stream_error("Connection to tcp://192.0.2.10:554 failed: Connection refused"), Some(StreamProblem::Refused));
        assert_eq!(classify_stream_error("Connection timed out"), Some(StreamProblem::TimedOut));
        assert_eq!(classify_stream_error("something odd"), Some(StreamProblem::Other));
        assert_eq!(classify_stream_error("   "), None);
        for p in [StreamProblem::SignInRefused, StreamProblem::NotFound, StreamProblem::Refused, StreamProblem::TimedOut, StreamProblem::Other] {
            assert!(!p.describe().contains("://"));
        }
    }

    #[test]
    fn inputs_come_from_the_capture_config() {
        let c = CaptureConfig {
            source: CaptureSource::Stream,
            stream_url: "rtsp://192.0.2.10/cam".into(),
            stream_username: "admin".into(),
            stream_password: Secret("pw".into()),
            audio_url: Some("http://192.0.2.10/audio".into()),
            ..CaptureConfig::default()
        };
        assert_eq!(video_input(&c).url, "rtsp://192.0.2.10/cam");
        assert_eq!(audio_input(&c).url, "http://192.0.2.10/audio");
        assert_eq!(audio_input(&c).username, "admin");
        let no_audio_url = CaptureConfig { audio_url: None, ..c };
        assert_eq!(audio_input(&no_audio_url).url, "rtsp://192.0.2.10/cam");
    }

    /// ffmpeg reads an MJPEG file served over HTTP by a tiny server and writes decodable segments.
    #[tokio::test]
    async fn real_ffmpeg_turns_mjpeg_over_http_into_segments() {
        let have = std::process::Command::new("ffmpeg").args(["-hide_banner", "-encoders"]).output();
        if !have.map(|o| String::from_utf8_lossy(&o.stdout).contains("libx264")).unwrap_or(false) {
            eprintln!("skipped: no ffmpeg with libx264");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let sample = tmp.path().join("sample.mjpeg");
        let made = std::process::Command::new("ffmpeg")
            .args(["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=10", "-t", "6", "-f", "mpjpeg"])
            .arg(&sample)
            .status()
            .unwrap();
        assert!(made.success());
        let body = std::fs::read(&sample).unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        std::thread::spawn(move || {
            if let Ok((mut socket, _)) = listener.accept() {
                use std::io::{Read, Write};
                let mut request = [0u8; 1024];
                let _ = socket.read(&mut request);
                let _ = socket.write_all(b"HTTP/1.0 200 OK\r\nContent-Type: multipart/x-mixed-replace;boundary=ffmpeg\r\n\r\n");
                let _ = socket.write_all(&body);
            }
        });
        let run_dir = tmp.path().join("run");
        std::fs::create_dir_all(&run_dir).unwrap();
        let input = StreamInput { url: format!("http://{addr}/stream"), username: String::new(), password: Secret::default() };
        let status = std::process::Command::new("ffmpeg")
            .args(video_args(&input, 2, 400_000, Duration::from_secs(2), &run_dir))
            .status()
            .unwrap();
        assert!(status.success(), "ffmpeg ends at the end of the stream");
        let first = run_dir.join("v_000000.h264");
        assert!(std::fs::metadata(&first).unwrap().len() > 0);
        let probe = std::process::Command::new("ffprobe")
            .args(["-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0"])
            .arg(&first)
            .output()
            .unwrap();
        assert_eq!(String::from_utf8_lossy(&probe.stdout).trim(), "h264,1280,720");
    }
}
