# P3c-2: add any LAN camera — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner adds an IP camera, a phone running a streaming app, or an ESP32 camera from the Thiết bị page; the
brain writes that camera's config, a `cameras` container runs one Loopi agent per camera (ffmpeg pulls RTSP or MJPEG
over HTTP, plus an optional separate audio URL), and every added camera is recorded and analysed like the Pi.

**Architecture:** The agent gains a second capture source, `stream` (ffmpeg re-encodes the stream into 60 s raw H.264
segments named like rpicam-vid's, and a second ffmpeg decodes sound to 16 kHz PCM for the existing Opus pipeline),
a "no LED" driver valid only for that source (D31), and `run --config PATH`. A new binary in the same crate,
`loopi-cameras`, supervises one `loopi-agent run --config <dir>/loopi.toml` per camera directory under
`/srv/loopi/cameras`. The brain gets a `network_camera` table, a form that probes a stream with ffprobe and writes the
camera's `loopi.toml` (mode 600) with a one-time pair token, a 7-day capacity estimate, and a "Đang ghi: N camera"
line on every page. Protocol v1 does not change: a camera is an ordinary device to the brain.

**Tech Stack:** Rust 2021 (tokio current-thread, rustls ring), ffmpeg/ffprobe, Docker Compose; brain Python 3.13,
FastAPI + Jinja + HTMX, psycopg 3, Postgres 17.

**Spec:** `docs/superpowers/specs/2026-10-19-p3c-multicam-design.md` (approved 2026-10-19; this plan is its P3c-2,
§9.2; read D30, D31, D36, §2, §3, §4.1, §4.5, §6, §7, §8) and the binding spec
`docs/superpowers/specs/2026-10-14-loopi-design.md` (D37: no BLE presence, so audio is never limited by phones).

## Global Constraints

- Protocol v1 does not change: no edit to `docs/protocol/v1.openapi.yaml`, no new heartbeat field (its schema has
  `additionalProperties: false`). The agent version stays `0.1.0`.
- Agent: Rust edition 2021, `rust-version = "1.80"`; rustls with the **ring** provider only — `cargo tree -i aws-lc-rs`
  must print nothing; **no new crate dependency** (use `std`, `libc`, `tokio`); `cd agent && cargo test` green;
  library coverage ≥ 80 % with `cargo llvm-cov --ignore-filename-regex 'main.rs'`. Keep `src/main.rs` and
  `src/bin/loopi-cameras.rs` thin (no logic that needs a test).
- The Pi is unchanged: a config without the new keys behaves exactly as today; the Buildroot recipe keeps building only
  `--bin loopi-agent`.
- Privacy: the LED is solid whenever a recorder runs (csi); `[led] source = "none"` is accepted **only** with
  `capture.source = "stream"` (D31); recorders die with their agent (PR_SET_PDEATHSIG) and agents die with
  `loopi-cameras`; `capture.audio_enabled = false` in a file means that agent never opens a sound source.
- Secrets: a camera's password is stored **only** in its `loopi.toml` (mode 600). It never goes into the database, a
  log line, a heartbeat, an error message, a web page (the form never shows it back) or a test fixture committed with a
  real value. ffmpeg/ffprobe error output is never logged or shown as it is (it can contain the URL).
- Durability: unchanged — an agent deletes a queued item only after the brain answers 201/200.
- Brain: Python 3.13; `cd brain && uv sync && uv run pytest --cov=loopi_brain` (Docker for testcontainers); coverage
  ≥ 80 %. The next migration is `012_network_camera.sql`; `brain/tests/test_db.py` pins migration lists in three
  places and `EXPECTED_TABLES`; `brain/tests/conftest.py` `APP_TABLES` and the `settings` fixture must learn the new
  table and `cameras_dir`.
- Every web POST has `Depends(require_htmx)`; refused input is answered 200 with the form and the reason (htmx does
  not swap 4xx); every text the web shows is Vietnamese; code, comments and commits are English.
- Conventional commits; each commit ends with the single line `Co-Authored-By: <your model name> <noreply@anthropic.com>`.
- Implementers never run `docker compose`, `docker build`, `deploy/smoke.sh`, never touch `/home/soul/loopi-p2`,
  `/home/soul/loopi-data`, the live stack or the device. Task 12 is the controller's, with the owner.
- Nobody — implementer or controller — prints a camera's `loopi.toml` or password (the Imou safety code included).

## Decisions this plan takes where the spec's words do not fit the code (rulings, recorded in Task 11)

| # | Spec text | Plan | Why |
|---|---|---|---|
| R1 | `led.kind = "none"` (§3, D31) | `[led] source = "none"` | every config table rejects unknown keys and `source` already selects the LED |
| R2 | the camera agent uses "cùng URL brain trong LAN" (§2) | `brain.url = "https://proxy:8443"` (the compose network) | the agent pins the brain CA and ignores the host name (`agent/src/tls.rs`), Caddy has one catch-all site; no NAT hairpin to the host's own LAN IP |
| R3 | "agent được cấu hình coi đồng hồ là đã đồng bộ" (§2) | the container mounts the host's `/run/systemd/timesync` read-only; the agent keeps its default sync flag | a camera then records only while the PC's clock is synced — the Pi's rule, not a pretence. Superseded after the final review: the camera agents ask the kernel (`[clock] source = "kernel"`, adjtimex, latched once synced) and the container mounts no host directory; see the ledger. |
| R4 | camera added "không có âm thanh" does not open audio | the brain writes `capture.audio_enabled = false`; that becomes a hard ceiling: a runtime "Bật audio" cannot reopen sound over it (every agent) | no new key; the old behaviour (a remote command overriding the file until restart) contradicted the file's intent |
| R5 | — | a camera with sound uses two ffmpeg connections (picture, sound) | each recorder keeps its own lifecycle and privacy rules; an app that accepts a single client loses sound (`mic_missing`), noted as a limitation |
| R6 | pairing "bằng token một lần" | camera tokens live 24 h; "Ghép cặp lại" issues a new one; the supervisor restarts an agent whose `loopi.toml` changed | a token must not die while the cameras container is down |
| R7 | `audio_gain_db` default 0 for `stream` | the brain writes `audio_gain_db = 0.0`; the agent default stays 20 | no per-source default in the agent |
| R8 | — | the stream program is `capture.camera_cmd` unless it is the default `rpicam-vid`, then `ffmpeg` | tests point `camera_cmd` at a fake ffmpeg |
| R9 | "Gỡ camera: ghi file disabled, thu hồi thiết bị" | also rewrite its `loopi.toml` without the password; the directory and its unsent queue stay | the secret does not outlive the camera; nothing recorded is dropped |

## Review Focus

1. A stream URL, a password, or ffmpeg/ffprobe error text that quotes the URL must never reach a log line, a
   heartbeat's `last_error`, a web page or the database (Task 2 `classify_stream_error` tests, Task 3
   `the_password_never_reaches_the_log_or_the_heartbeat`, Task 6 `test_the_password_is_only_in_the_mode_600_file`,
   Task 7 `test_ffprobe_errors_never_echo_the_address`).
2. A camera that drops off the network or stalls: the agent restarts ffmpeg with backoff, the heartbeat shows
   `camera_failures`/`last_error` in words without the URL, and a stalled sound pipe never hangs shutdown (Task 2
   `a_stream_that_dies_is_restarted_and_reported`, Task 3 `a_stalled_sound_stream_times_out`).
3. The pair token expires before the cameras container reads it: "Ghép cặp lại" issues a new token, rewrites the file
   keeping the password, and the supervisor restarts that agent (Task 4 `a_changed_config_restarts_its_agent`,
   Task 6 `test_repairing_issues_a_new_token_and_keeps_the_password`).
4. Removing a camera stops its agent within one scan, scrubs the password from its file, revokes its device, keeps
   its recordings (Task 4 `a_disabled_camera_is_stopped`, Task 6 `test_removing_a_camera_scrubs_the_password_and_revokes`).
5. A directory under `/srv/loopi/cameras` that is not a camera id, or has no `loopi.toml`, is ignored, and each camera
   has its own data directory (Task 4 `only_camera_directories_with_a_config_run`).

## File structure

| File | Responsibility | Task |
|---|---|---|
| `agent/src/config.rs` | `CaptureSource`, stream keys, `Secret`, `LedSource::None`, validation | 1 |
| `agent/src/led.rs` | `NullLed` | 1 |
| `agent/src/cli.rs` (new) | `run [--config PATH]` parsing | 1 |
| `agent/src/control.rs` | `cap_audio` (R4) | 1 |
| `agent/src/child.rs` (new) | PDEATHSIG helpers shared by recorders and the supervisor | 2 |
| `agent/src/recorder/stream.rs` (new) | `StreamInput`, credentials in URLs, ffmpeg input/video args, error classes | 2 |
| `agent/src/recorder/video.rs` | `VideoSource`, stream spawn with classified stderr, healthy-run reset | 2 |
| `agent/src/recorder/stream_audio.rs` (new) | `StreamAudioSource` (ffmpeg → s16le pipe, poll timeout) | 3 |
| `agent/src/main.rs`, `agent/src/run.rs` | wiring for `stream` | 3 |
| `agent/src/cameras.rs` (new), `agent/src/bin/loopi-cameras.rs` (new) | the supervisor | 4 |
| `deploy/cameras.Dockerfile` (new), `.dockerignore` (new), `deploy/docker-compose.yml`, `brain/tests/test_deploy.py` | the container | 5 |
| `brain/src/loopi_brain/migrations/012_network_camera.sql`, `brain/src/loopi_brain/cameras.py` (new), `devices.py`, `config.py`, `app.py` | cameras in the brain | 6 |
| `brain/src/loopi_brain/camera_probe.py` (new) | "Thử kết nối" | 7 |
| `brain/src/loopi_brain/capacity.py` (new), `web/camera_routes.py` (new), templates | the camera form and list, capacity | 8 |
| `web/routes.py`, `templates/base.html`, `templates/_recording_now.html` (new) | "Đang ghi: N camera" | 9 |
| `places.py`, `web/search_routes.py`, `chat.py`, `web/day_routes.py`, `web/routes.py`, templates | P3c-1 leftovers | 10 |
| specs, `deploy/README.md` | rulings and the operator guide | 11 |

---

### Task 1: The agent can be configured as a network camera

**Files:**
- Modify: `agent/src/config.rs` (new `CaptureSource`, `Secret`, `CaptureConfig` keys, `LedSource::None`, validation)
- Modify: `agent/src/led.rs` (`NullLed`, `open`)
- Create: `agent/src/cli.rs`; Modify: `agent/src/lib.rs` (`pub mod cli;`), `agent/src/main.rs` (use it)
- Modify: `agent/src/control.rs` (`cap_audio`), `agent/src/run.rs` (apply it to remote and button commands)
- Test: unit tests in the same files

**Interfaces:**
- Produces:
  - `config::CaptureSource { Csi (default), Stream }` (TOML `"csi"`, `"stream"`), `config::DEFAULT_CAMERA_CMD = "rpicam-vid"`.
  - `config::Secret(pub String)` — serde transparent, `Debug` prints `"***"` (or `""` when empty), `expose(&self) -> &str`.
  - `CaptureConfig` new fields: `source: CaptureSource`, `stream_url: String`, `stream_username: String`,
    `stream_password: Secret`, `audio_url: Option<String>`; method `recorder_cmd(&self) -> &str` (R8).
  - `config::LedSource::None` (TOML `"none"`).
  - `led::NullLed: LedDriver`.
  - `cli::DEFAULT_CONFIG = "/data/config/loopi.toml"`, `cli::run_config_path(args: &[String]) -> Result<PathBuf, String>`.
  - `control::cap_audio(command: Command, config_audio_enabled: bool) -> Command`.

- [ ] **Step 1: Write the failing tests**

In `agent/src/config.rs` `mod tests`, add:

```rust
    const STREAM: &str = r#"
[brain]
url = "https://proxy:8443"
[capture]
source = "stream"
stream_url = "rtsp://192.0.2.10:554/cam/realmonitor?channel=1&subtype=0"
stream_username = "admin"
stream_password = "S3cr3t"
audio_gain_db = 0.0
[led]
source = "none"
[paths]
data_dir = "/srv/loopi/cameras/01J0000000000000000000000A/data"
"#;

    #[test]
    fn a_network_camera_config_parses_and_round_trips() {
        let c = Config::parse(STREAM).unwrap();
        assert_eq!(c.capture.source, CaptureSource::Stream);
        assert_eq!(c.capture.stream_password.expose(), "S3cr3t");
        assert_eq!(c.capture.audio_url, None);
        assert_eq!(c.led.source, LedSource::None);
        assert_eq!(c.capture.recorder_cmd(), "ffmpeg");
        assert_eq!(Config::parse(&toml::to_string(&c).unwrap()).unwrap(), c);
    }

    #[test]
    fn the_password_never_shows_in_debug_output() {
        let c = Config::parse(STREAM).unwrap();
        let shown = format!("{c:?}");
        assert!(!shown.contains("S3cr3t") && shown.contains("\"***\""), "{shown}");
        assert_eq!(format!("{:?}", Secret::default()), "\"\"");
    }

    #[test]
    fn a_pi_config_is_unchanged() {
        let c = Config::parse("[brain]\nurl = \"https://brain.lan:8443\"\n").unwrap();
        assert_eq!(c.capture.source, CaptureSource::Csi);
        assert_eq!(c.capture.recorder_cmd(), "rpicam-vid");
        assert!(!toml::to_string(&c).unwrap().contains("audio_url"));
    }

    #[test]
    fn a_custom_camera_cmd_wins_for_a_stream() {
        let mut c = Config::parse(STREAM).unwrap();
        c.capture.camera_cmd = "/opt/ffmpeg/bin/ffmpeg".into();
        assert_eq!(c.capture.recorder_cmd(), "/opt/ffmpeg/bin/ffmpeg");
    }

    #[test]
    fn bad_network_camera_configs_are_refused() {
        let cases = [
            (r#"[led]
source = "none""#, "only allowed for a network camera"),
            (r#"[capture]
source = "stream""#, "capture.stream_url"),
            (r#"[capture]
source = "stream"
stream_url = "ftp://192.0.2.10/x""#, "rtsp://, http:// or https://"),
            (r#"[capture]
source = "stream"
stream_url = "rtsp://admin:pw@192.0.2.10/x""#, "stream_username"),
            (r#"[capture]
source = "stream"
stream_url = "rtsp:///x""#, "host"),
            (r#"[capture]
source = "stream"
stream_url = "rtsp://192.0.2.10/a b""#, "spaces"),
            (r#"[capture]
source = "stream"
stream_url = "rtsp://192.0.2.10/x"
audio_url = "http://u:p@192.0.2.10/audio""#, "stream_username"),
        ];
        for (body, needle) in cases {
            let text = format!("[brain]\nurl = \"https://proxy:8443\"\n{body}\n");
            let err = Config::parse(&text).unwrap_err().to_string();
            assert!(err.contains(needle), "{body:?} -> {err}");
        }
    }
```

In `agent/src/led.rs` `mod tests`:

```rust
    #[test]
    fn the_null_led_of_a_network_camera_always_works() {
        let led_config = LedConfig { source: LedSource::None, ..LedConfig::default() };
        let mut led = open(&led_config, &GpioConfig::default()).unwrap();
        assert!(led.set(true).is_ok() && led.set(false).is_ok());
    }
```

Create `agent/src/cli.rs` with only the tests first (the functions come in Step 3):

```rust
#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn run_uses_the_pi_config_unless_told_otherwise() {
        assert_eq!(run_config_path(&args(&[])).unwrap(), PathBuf::from(DEFAULT_CONFIG));
        assert_eq!(run_config_path(&args(&["--config", "/srv/c/loopi.toml"])).unwrap(), PathBuf::from("/srv/c/loopi.toml"));
        assert!(run_config_path(&args(&["--config"])).unwrap_err().contains("needs a path"));
        assert!(run_config_path(&args(&["--data", "/x"])).unwrap_err().contains("unknown flag"));
    }
}
```

In `agent/src/control.rs` `mod tests`:

```rust
    #[test]
    fn a_file_without_sound_is_a_ceiling_for_remote_commands() {
        assert_eq!(cap_audio(Command::SetAudio(true), false), Command::SetAudio(false));
        assert_eq!(cap_audio(Command::SetAudio(true), true), Command::SetAudio(true));
        assert_eq!(cap_audio(Command::SetAudio(false), true), Command::SetAudio(false));
        assert_eq!(cap_audio(Command::Pause, false), Command::Pause);
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd agent && cargo test --lib config:: led:: cli:: control::`
Expected: compile errors — `CaptureSource`, `Secret`, `LedSource::None`, `run_config_path`, `cap_audio` do not exist.

- [ ] **Step 3: Implement**

`agent/src/config.rs` — add after `DEFAULT_AUDIO_GAIN_DB` and before `CaptureConfig`:

```rust
/// The program `capture.camera_cmd` names when nothing else is configured: the Pi's camera.
pub const DEFAULT_CAMERA_CMD: &str = "rpicam-vid";

/// Where video and sound come from (P3c design §3, D30).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CaptureSource {
    /// The Pi's CSI camera through rpicam-vid and a USB microphone through ALSA.
    #[default]
    Csi,
    /// A network camera: ffmpeg pulls `stream_url` (RTSP, or MJPEG over HTTP), and for sound `audio_url` or the same
    /// stream.
    Stream,
}

/// A secret kept in a config file: written as it is, never shown by `Debug`.
#[derive(Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Secret(pub String);

impl Secret {
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(if self.0.is_empty() { "\"\"" } else { "\"***\"" })
    }
}
```

`CaptureConfig` gains, after `camera_cmd`:

```rust
    pub source: CaptureSource,
    /// `stream`: the camera's address without credentials (rtsp://, http:// or https://).
    pub stream_url: String,
    pub stream_username: String,
    pub stream_password: Secret,
    /// `stream`: a separate sound stream (WAV or PCM over HTTP, as the Loopi ESP32 firmware serves); the camera's
    /// stream itself when absent.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub audio_url: Option<String>,
```

its `Default` gains `source: CaptureSource::Csi, stream_url: String::new(), stream_username: String::new(),
stream_password: Secret::default(), audio_url: None`, `camera_cmd: DEFAULT_CAMERA_CMD.into()`, and add:

```rust
impl CaptureConfig {
    /// The program that records video: `camera_cmd`, except that a network camera with the Pi's default runs ffmpeg.
    pub fn recorder_cmd(&self) -> &str {
        if self.source == CaptureSource::Stream && self.camera_cmd == DEFAULT_CAMERA_CMD {
            "ffmpeg"
        } else {
            &self.camera_cmd
        }
    }
}
```

`LedSource` gains, after `Gpio`:

```rust
    /// No LED Loopi controls: a network camera (D31). Only allowed with `capture.source = "stream"`.
    None,
```

In `Config::validate`, after the `led.path` check:

```rust
        if self.led.source == LedSource::None && self.capture.source != CaptureSource::Stream {
            return invalid("led.source = \"none\" is only allowed for a network camera (capture.source = \"stream\")".into());
        }
        if self.capture.source == CaptureSource::Stream {
            check_stream_url("capture.stream_url", &self.capture.stream_url).map_err(ConfigError::Invalid)?;
            if let Some(url) = &self.capture.audio_url {
                check_stream_url("capture.audio_url", url).map_err(ConfigError::Invalid)?;
            }
        }
```

and add next to `validate_wifi`:

```rust
const STREAM_URL_MAX: usize = 1000;

/// A network camera's address: rtsp://, http:// or https://, a host, no spaces or control characters, and no
/// credentials (they live in `stream_username`/`stream_password`, so the address can be shown and logged).
fn check_stream_url(key: &str, url: &str) -> Result<(), String> {
    let Some(rest) = ["rtsp://", "http://", "https://"].iter().find_map(|s| url.strip_prefix(s)) else {
        return Err(format!("{key} must start with rtsp://, http:// or https:// (got {} characters)", url.len()));
    };
    if url.len() > STREAM_URL_MAX {
        return Err(format!("{key} is longer than {STREAM_URL_MAX} characters"));
    }
    if url.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err(format!("{key} must not contain spaces or control characters"));
    }
    let authority = rest.split(['/', '?', '#']).next().unwrap_or_default();
    if authority.contains('@') {
        return Err(format!("{key} must not hold credentials; put them in capture.stream_username and stream_password"));
    }
    if authority.is_empty() {
        return Err(format!("{key} has no host"));
    }
    Ok(())
}
```

Update every struct literal the new fields break (the compiler lists them; the exploration found `LedConfig { source,
path }` in `led.rs:329`, `config.rs:427`, `config.rs:436` — add `..LedConfig::default()` is not possible for a
two-field literal that names both; nothing to add since `LedConfig` gains no field). `CaptureConfig` literals all use
`..CaptureConfig::default()`.

`agent/src/led.rs`:

```rust
/// A network camera has no LED that Loopi drives (D31): every write succeeds, so the recording gates open as they do
/// for a working LED. `Config::validate` allows it only with `capture.source = "stream"`.
pub struct NullLed;

impl LedDriver for NullLed {
    fn set(&mut self, _on: bool) -> io::Result<()> {
        Ok(())
    }
}
```

and in `open`: `LedSource::None => Ok(Box::new(NullLed)),`.

`agent/src/cli.rs` (above its tests):

```rust
//! Command-line arguments of the binaries, kept out of `main.rs` so they are tested.
use std::path::PathBuf;

/// The Pi's config, imported from the boot partition by `loopi-agent provision`.
pub const DEFAULT_CONFIG: &str = "/data/config/loopi.toml";

/// `loopi-agent run [--config PATH]`: the config the agent runs with (a network camera's lives in its own directory).
pub fn run_config_path(args: &[String]) -> Result<PathBuf, String> {
    let mut path = PathBuf::from(DEFAULT_CONFIG);
    let mut it = args.iter();
    while let Some(flag) = it.next() {
        match flag.as_str() {
            "--config" => path = it.next().ok_or_else(|| "--config needs a path".to_string())?.into(),
            other => return Err(format!("unknown flag {other:?} for run; use run [--config PATH]")),
        }
    }
    Ok(path)
}
```

`agent/src/lib.rs`: add `pub mod cli;`.

`agent/src/main.rs`: replace `const DATA_CONFIG` with `use loopi_agent::cli::{run_config_path, DEFAULT_CONFIG};`;
`None => runtime().and_then(|rt| rt.block_on(agent_main(PathBuf::from(DEFAULT_CONFIG))))`,
`Some("run") => run_config_path(&args[1..]).map_err(|e| anyhow::anyhow!(e)).and_then(|path| runtime().and_then(|rt| rt.block_on(agent_main(path))))`;
`selftest` and `led-off` use `Path::new(DEFAULT_CONFIG)`; `agent_main(config_path: PathBuf)` loads `&config_path`, and
when it is missing bails with `"not provisioned: run `loopi-agent provision` first"` for the default path and
`"config {path} not found"` otherwise.

`agent/src/control.rs`:

```rust
/// Privacy (R4): `capture.audio_enabled = false` in the file is a ceiling. A remote "audio on" (or any other path that
/// turns sound on) cannot reopen the microphone over it; the agent turns it off instead.
pub fn cap_audio(command: Command, config_audio_enabled: bool) -> Command {
    match command {
        Command::SetAudio(true) if !config_audio_enabled => Command::SetAudio(false),
        other => other,
    }
}
```

`agent/src/run.rs`: where the main loop hands a control command to `apply_command` (remote commands and button
presses), pass `&control::cap_audio(command, config.capture.audio_enabled)` instead of the command itself, and log
once at info level when a `SetAudio(true)` was capped (no other change).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd agent && cargo test`
Expected: PASS, every test (the Pi's e2e and safety suites unchanged).

- [ ] **Step 5: Commit**

```bash
git add agent/src
git commit -m "feat(agent): network camera config (stream source, no-LED source, run --config, audio ceiling) (P3c-2, D30, D31)"
```

---

### Task 2: Video from a network camera

**Files:**
- Create: `agent/src/child.rs` (move `die_with_parent` here, for `tokio::process::Command` and `std::process::Command`)
- Create: `agent/src/recorder/stream.rs`; Modify: `agent/src/recorder/mod.rs` (`pub mod stream;`), `agent/src/lib.rs` (`pub mod child;`)
- Modify: `agent/src/recorder/video.rs` (`VideoSource`, stream spawn, classified stderr, healthy-run reset)
- Create: `agent/tests/fixtures/fake-ffmpeg.sh`
- Test: unit tests in `stream.rs` and `video.rs`

**Interfaces:**
- Consumes: Task 1's `CaptureSource`, `Secret`, `CaptureConfig::recorder_cmd`.
- Produces:
  - `child::die_with_parent_tokio(&mut tokio::process::Command)`, `child::die_with_parent_std(&mut std::process::Command)`.
  - `recorder::stream::StreamInput { url: String, username: String, password: Secret }` with
    `url_with_credentials(&self) -> String` (its `Debug` never shows the password);
    `stream::video_input(&CaptureConfig) -> StreamInput`, `stream::audio_input(&CaptureConfig) -> StreamInput`.
  - `stream::input_args(url_with_credentials: &str) -> Vec<String>`.
  - `stream::video_args(input: &StreamInput, fps: u32, bitrate_bps: u32, segment: Duration, run_dir: &Path) -> Vec<String>`.
  - `stream::StreamProblem { SignInRefused, NotFound, Refused, TimedOut, Other }`, `stream::classify_stream_error(line: &str) -> Option<StreamProblem>`, `StreamProblem::describe(self) -> &'static str`.
  - `video::VideoSource { Rpicam, Stream(StreamInput) }`, field `VideoOptions.source`.

- [ ] **Step 1: Write the failing tests**

`agent/tests/fixtures/fake-ffmpeg.sh` (executable):

```sh
#!/bin/sh
# Test double for ffmpeg as a network camera's recorder. Video: the last argument is the segment pattern
# <dir>/v_%06d.h264; write a numbered segment every 0.2 s until SIGINT/SIGTERM. Sound: the last argument is pipe:1;
# write silence (s16le) until killed. FAKE_FFMPEG_FAIL=1 prints an RTSP sign-in error that quotes the URL and exits 1.
for last; do :; done
if [ "${FAKE_FFMPEG_FAIL:-}" = 1 ]; then
  for a; do case "$a" in rtsp://*|http://*) url="$a";; esac; done
  echo "[rtsp @ 0x55] method DESCRIBE failed: 401 Unauthorized ($url)" >&2
  exit 1
fi
trap 'exit 0' INT TERM
if [ "$last" = "pipe:1" ]; then
  exec cat /dev/zero
fi
dir=$(dirname "$last")
i=0
while :; do
  printf 'segment-%d' "$i" > "$dir/$(printf 'v_%06d.h264' "$i")"
  i=$((i + 1))
  sleep 0.2
done
```

`agent/src/recorder/stream.rs` tests (module body comes in Step 3):

```rust
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
}
```

In `agent/src/recorder/video.rs` `mod tests`, add (the existing `opts()` helper gains `source: VideoSource::Rpicam`;
add a `stream_opts` helper next to it):

```rust
    fn fake_ffmpeg(dir: &Path) -> String {
        let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/fake-ffmpeg.sh");
        executable(dir, "fake-ffmpeg.sh", &fs::read(fixture).unwrap())
    }

    fn stream_opts(dir: &Path, cmd: String) -> VideoOptions {
        let input = crate::recorder::stream::StreamInput {
            url: "rtsp://192.0.2.10/cam".into(),
            username: "admin".into(),
            password: crate::config::Secret("S3cr3t".into()),
        };
        VideoOptions { source: VideoSource::Stream(input), ..opts(dir, cmd) }
    }

    #[tokio::test]
    async fn a_network_camera_records_segments_like_the_pi() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let queue = Arc::new(queue);
        let status = status_channel(AgentStatus::default());
        let (_gate_tx, gate_rx) = watch::channel(true);
        let (stop_tx, stop_rx) = watch::channel(false);
        let options = stream_opts(tmp.path(), fake_ffmpeg(tmp.path()));
        let task = tokio::spawn(run_video(options, queue.clone(), Arc::new(SyncedClock), gate_rx, status.clone(), stop_rx));
        assert!(eventually(|| queue.len().unwrap() >= 2).await, "segments are queued");
        assert!(status.borrow().recording_video);
        stop_tx.send(true).unwrap();
        task.await.unwrap();
    }

    #[tokio::test]
    async fn a_stream_that_dies_is_restarted_and_reported() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let status = status_channel(AgentStatus::default());
        let (_gate_tx, gate_rx) = watch::channel(true);
        let (stop_tx, stop_rx) = watch::channel(false);
        let script = executable(
            tmp.path(),
            "failing-ffmpeg.sh",
            b"#!/bin/sh\necho '[rtsp @ 0x1] 401 Unauthorized (rtsp://admin:S3cr3t@192.0.2.10/cam)' >&2\nexit 1\n",
        );
        let task = tokio::spawn(run_video(stream_opts(tmp.path(), script), Arc::new(queue), Arc::new(SyncedClock), gate_rx, status.clone(), stop_rx));
        assert!(eventually(|| status.borrow().camera_failures >= 2).await, "the recorder retries");
        let error = status.borrow().last_error.clone().unwrap();
        assert!(error.contains("refused the user name or password"), "{error}");
        assert!(!error.contains("S3cr3t") && !error.contains("rtsp://"), "{error}");
        stop_tx.send(true).unwrap();
        task.await.unwrap();
    }

    #[tokio::test]
    async fn a_run_that_lasts_clears_the_failure_count() {
        let tmp = tempfile::tempdir().unwrap();
        let (queue, _) = Queue::open(&tmp.path().join("q")).unwrap();
        let status = status_channel(AgentStatus { camera_failures: 4, ..AgentStatus::default() });
        let (_gate_tx, gate_rx) = watch::channel(true);
        let (stop_tx, stop_rx) = watch::channel(false);
        let options = VideoOptions { healthy_run: Duration::from_millis(300), ..opts(tmp.path(), fake_camera(tmp.path())) };
        let task = tokio::spawn(run_video(options, Arc::new(queue), Arc::new(SyncedClock), gate_rx, status.clone(), stop_rx));
        assert!(eventually(|| status.borrow().recording_video && status.borrow().camera_failures == 0).await);
        stop_tx.send(true).unwrap();
        task.await.unwrap();
    }
```

And one test with the real ffmpeg (skipped when ffmpeg with libx264 is not on PATH), in `agent/src/recorder/stream.rs`
`mod tests`:

```rust
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd agent && cargo test --lib recorder::`
Expected: compile errors — `stream` module, `VideoSource`, `VideoOptions.source` do not exist.

- [ ] **Step 3: Implement**

`agent/src/child.rs`:

```rust
//! Child processes that must not outlive the process that started them (privacy: no recorder without its agent,
//! no agent without loopi-cameras). PR_SET_PDEATHSIG fires when the thread that forked exits: the binaries use a
//! current-thread runtime, so that is the main thread.
use std::io;

/// SIGTERM to the child when its parent dies (Linux; nothing elsewhere).
pub fn die_with_parent_tokio(command: &mut tokio::process::Command) {
    #[cfg(target_os = "linux")]
    // SAFETY: prctl(2) is async-signal-safe; this runs in the child between fork and exec.
    unsafe {
        command.pre_exec(|| set_pdeathsig());
    }
    #[cfg(not(target_os = "linux"))]
    let _ = command;
}

/// The same for `std::process::Command` (the sound thread spawns its ffmpeg with it).
pub fn die_with_parent_std(command: &mut std::process::Command) {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        // SAFETY: as above.
        unsafe {
            command.pre_exec(|| set_pdeathsig());
        }
    }
    #[cfg(not(target_os = "linux"))]
    let _ = command;
}

#[cfg(target_os = "linux")]
fn set_pdeathsig() -> io::Result<()> {
    // SAFETY: prctl(2) with PR_SET_PDEATHSIG only reads its integer arguments.
    if unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}
```

(`video.rs` uses `crate::child::die_with_parent_tokio` and drops its own `die_with_parent`.)

`agent/src/recorder/stream.rs`:

```rust
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
```

`agent/src/recorder/video.rs`:
- add `pub source: VideoSource` to `VideoOptions` and
  ```rust
  /// What runs the camera: rpicam-vid on the Pi, or ffmpeg pulling a network camera's stream (P3c-2).
  #[derive(Debug, Clone)]
  pub enum VideoSource {
      Rpicam,
      Stream(crate::recorder::stream::StreamInput),
  }
  ```
  `from_config` sets `camera_cmd: capture.recorder_cmd().to_string()` and `source` = `Stream(stream::video_input(capture))`
  for `CaptureSource::Stream`, else `Rpicam`;
- `record_once` builds its argv with `match &opts.source { VideoSource::Rpicam => rpicam_args(opts, &run_dir),
  VideoSource::Stream(input) => stream::video_args(input, opts.fps, opts.bitrate_bps, opts.segment, &run_dir) }`;
- for `VideoSource::Stream` spawn with `.stderr(Stdio::piped())` and start a task that reads the stderr lines
  (`tokio::io::BufReader::new(stderr).lines()`), keeps the first `Some(problem)` other than `Other` (or `Other` when
  nothing more specific came), and **never logs or stores a line**; the task's result is awaited after the child exits;
  for `Rpicam` keep `Stdio::inherit()`;
- `RunEnd::Exited` gains `problem: Option<StreamProblem>`; `run_video`'s message becomes
  `"rpicam-vid exited with {code:?} after {n} s"` for the Pi and
  `"ffmpeg exited with {code:?} after {n} s: {problem.describe()}"` for a stream (`Other` when no line came);
- inside `record_once`'s loop add a one-shot `tokio::time::sleep(opts.healthy_run)` branch that sets
  `status.camera_failures = 0` once the run has lasted `healthy_run` (finding: a recovered camera kept reporting old
  failures); the existing counting in `run_video` stays.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd agent && cargo test`
Expected: PASS (the real-ffmpeg test passes here — ffmpeg 9 with libx264 is on PATH — or prints "skipped").

- [ ] **Step 5: Commit**

```bash
git add agent/src agent/tests/fixtures/fake-ffmpeg.sh
git commit -m "feat(agent): a network camera's video through ffmpeg in rpicam-like segments, errors named without the URL (P3c-2)"
```

---

### Task 3: Sound from a network camera, and the agent wired for it

**Files:**
- Create: `agent/src/recorder/stream_audio.rs`; Modify: `agent/src/recorder/mod.rs`
- Modify: `agent/src/main.rs` (stream wiring), `agent/src/sysinfo.rs` only if `NoProbe` is not public yet
- Create: `agent/tests/run_stream_e2e.rs`

**Interfaces:**
- Consumes: Task 1 (`CaptureSource`, `NullLed`, `cap_audio`), Task 2 (`stream::input_args`, `stream::audio_input`, `child::die_with_parent_std`, the fake ffmpeg).
- Produces: `stream_audio::audio_args(url_with_credentials: &str) -> Vec<String>`,
  `stream_audio::StreamAudioSource::open(cmd: &str, input: &StreamInput, timeout: Duration) -> io::Result<StreamAudioSource>`,
  `stream_audio::factory(cmd: String, input: StreamInput) -> AudioSourceFactory`, `stream_audio::READ_TIMEOUT = 10 s`.

- [ ] **Step 1: Write the failing tests**

`agent/src/recorder/stream_audio.rs` tests:

```rust
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
```

`agent/tests/run_stream_e2e.rs`: one whole-agent test, built like `tests/run_e2e.rs::records_uploads_and_obeys_a_remote_pause`
(read that file first and reuse its mock-brain helpers by copying the few you need: the wiremock heartbeat responder
that validates against the OpenAPI schema and the PUT validator). Its `Deps`:
`config` = a `Config` whose `capture` is `CaptureConfig { source: CaptureSource::Stream, camera_cmd: <fake ffmpeg path>,
stream_url: "rtsp://192.0.2.10/cam".into(), stream_username: "admin".into(), stream_password: Secret("S3cr3t".into()),
..CaptureConfig::default() }`, `led: LedConfig { source: LedSource::None, ..LedConfig::default() }`, `paths` as in
run_e2e; `led: Some(Box::new(NullLed))`; `audio_factory: stream_audio::factory(<fake ffmpeg>, stream::audio_input(&capture))`;
`clock: Arc::new(SyncedClock)`; `ota: None`; `probe: Arc::new(NoProbe)`. Tests:

```rust
#[tokio::test(flavor = "multi_thread")]
async fn a_network_camera_records_and_uploads_video_and_sound() {
    // run the agent for up to 10 s; assert at least one video PUT (X-Loopi-Kind: video, Content-Type video/h264) and one
    // audio PUT reached the mock brain, every heartbeat matched the Heartbeat schema with recording_video true at some
    // point, then shut down and assert no fake-ffmpeg process outlives the agent (pgrep -f on the script path).
}

#[tokio::test(flavor = "multi_thread")]
async fn the_password_never_reaches_the_log_or_the_heartbeat() {
    // FAKE_FFMPEG_FAIL=1 in the environment of the fake (write a wrapper script that sets it), run until a heartbeat
    // carries camera_failures >= 1; assert no heartbeat body contains "S3cr3t" or "rtsp://", and last_error contains
    // "refused the user name or password".
}

#[tokio::test(flavor = "multi_thread")]
async fn a_camera_without_sound_never_opens_it_even_when_asked() {
    // capture.audio_enabled = false; the mock brain answers the first heartbeat with a set_config {audio_enabled: true}
    // command; assert no audio PUT within 4 s, heartbeats keep audio_enabled false, the audio factory was never called
    // (a factory closure that counts calls).
}
```

Write each body completely (the comments above say what each must assert).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd agent && cargo test --lib recorder::stream_audio && cargo test --test run_stream_e2e`
Expected: compile errors — `stream_audio` does not exist.

- [ ] **Step 3: Implement**

`agent/src/recorder/stream_audio.rs` (above its tests):

```rust
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
```

(`FRAME_SAMPLES` lives in `recorder::ogg_opus`; if it is not `pub`, make it `pub`.)

`agent/src/main.rs` (`agent_main`, after loading the config):

```rust
    let stream = config.capture.source == loopi_agent::config::CaptureSource::Stream;
```

and in `Deps`: `audio_factory: if stream { stream_audio::factory(config.capture.recorder_cmd().to_string(),
stream::audio_input(&config.capture)) } else { audio_factory(&config) }`, `presses: if stream { mpsc::channel(1).1 }
else { open_button(&config) }`, `probe: if stream { Arc::new(NoProbe) } else { Arc::new(LinuxProbe) }`,
`ota: if stream { None } else { Some(OtaSystem { .. }) }` (a network camera has no A/B slots; an `update` command is
refused as today). `open_led` already returns the `NullLed` for `source = "none"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd agent && cargo test && cargo llvm-cov --ignore-filename-regex 'main.rs' --summary-only | tail -3 && cargo tree -i aws-lc-rs`
Expected: PASS; library coverage ≥ 80 %; `cargo tree -i aws-lc-rs` reports no match.

- [ ] **Step 5: Commit**

```bash
git add agent/src agent/tests/run_stream_e2e.rs
git commit -m "feat(agent): a network camera's sound through ffmpeg, and the agent wired for a stream source (P3c-2)"
```

---

### Task 4: `loopi-cameras`, one agent per camera

**Files:**
- Create: `agent/src/cameras.rs`, `agent/src/bin/loopi-cameras.rs`; Modify: `agent/src/lib.rs` (`pub mod cameras;`)
- Create: `agent/tests/fixtures/fake-agent.sh`

**Interfaces:**
- Consumes: `child::die_with_parent_tokio` (Task 2), `cli::run_config_path`'s `--config` flag (Task 1).
- Produces: `cameras::CONFIG_FILE = "loopi.toml"`, `cameras::DISABLED_FILE = "disabled"`,
  `cameras::is_camera_id(&str) -> bool`, `cameras::scan(root: &Path) -> io::Result<Vec<Wanted>>`,
  `cameras::SupervisorOptions { root, agent_cmd, scan, stop_grace, min_backoff, max_backoff, healthy_run }`,
  `cameras::options_from_args(args: &[String]) -> Result<SupervisorOptions, String>`,
  `cameras::supervise(opts, shutdown: watch::Receiver<bool>) -> io::Result<()>`.

- [ ] **Step 1: Write the failing tests**

`agent/tests/fixtures/fake-agent.sh` (executable):

```sh
#!/bin/sh
# Test double for `loopi-agent run --config <dir>/loopi.toml`: appends "<pid> <config>" to <dir>/runs.log, says hello
# on stderr, and runs until SIGTERM. FAKE_AGENT_EXIT=1 makes it exit at once.
config="$3"
dir=$(dirname "$config")
echo "$$ $config" >> "$dir/runs.log"
echo "hello from the agent" >&2
[ "${FAKE_AGENT_EXIT:-}" = 1 ] && exit 3
trap 'exit 0' TERM
while :; do sleep 0.1; done
```

`agent/src/cameras.rs` tests:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    const A: &str = "01J00000000000000000000AAA";
    const B: &str = "01J00000000000000000000BBB";

    fn camera(root: &Path, id: &str) -> PathBuf {
        let dir = root.join(id);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join(CONFIG_FILE), "[brain]\nurl = \"https://proxy:8443\"\n").unwrap();
        dir
    }

    fn options(root: &Path, agent: PathBuf) -> SupervisorOptions {
        SupervisorOptions {
            root: root.to_path_buf(),
            agent_cmd: agent,
            scan: Duration::from_millis(100),
            stop_grace: Duration::from_secs(2),
            min_backoff: Duration::from_millis(100),
            max_backoff: Duration::from_millis(400),
            healthy_run: Duration::from_secs(60),
        }
    }

    fn fake_agent(dir: &Path) -> PathBuf {
        let path = dir.join("fake-agent.sh");
        fs::copy(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/fake-agent.sh"), &path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    fn pids(dir: &Path) -> Vec<i32> {
        fs::read_to_string(dir.join("runs.log"))
            .unwrap_or_default()
            .lines()
            .filter_map(|l| l.split_whitespace().next()?.parse().ok())
            .collect()
    }

    fn alive(pid: i32) -> bool {
        // SAFETY: signal 0 only checks that the process exists.
        unsafe { libc::kill(pid, 0) == 0 }
    }

    async fn eventually(check: impl Fn() -> bool) -> bool {
        for _ in 0..100 {
            if check() {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        false
    }

    #[test]
    fn only_camera_directories_with_a_config_run() {
        let tmp = tempfile::tempdir().unwrap();
        camera(tmp.path(), A);
        fs::create_dir_all(tmp.path().join(B)).unwrap(); // no config yet
        camera(tmp.path(), "not-a-camera");
        let disabled = camera(tmp.path(), "01J00000000000000000000CCC");
        fs::write(disabled.join(DISABLED_FILE), "").unwrap();
        let wanted: Vec<String> = scan(tmp.path()).unwrap().into_iter().map(|w| w.id).collect();
        assert_eq!(wanted, vec![A.to_string()]);
        assert!(is_camera_id(A) && !is_camera_id("not-a-camera") && !is_camera_id("01j00000000000000000000aaa"));
    }

    #[tokio::test]
    async fn every_camera_gets_its_own_agent_and_all_stop_with_the_supervisor() {
        let tmp = tempfile::tempdir().unwrap();
        let (a, b) = (camera(tmp.path(), A), camera(tmp.path(), B));
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(supervise(options(tmp.path(), fake_agent(tmp.path())), stop_rx));
        assert!(eventually(|| pids(&a).len() == 1 && pids(&b).len() == 1).await);
        let started = [pids(&a)[0], pids(&b)[0]];
        stop_tx.send(true).unwrap();
        task.await.unwrap().unwrap();
        assert!(started.iter().all(|pid| !alive(*pid)), "no agent outlives the supervisor");
    }

    #[tokio::test]
    async fn a_disabled_camera_is_stopped() {
        let tmp = tempfile::tempdir().unwrap();
        let a = camera(tmp.path(), A);
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(supervise(options(tmp.path(), fake_agent(tmp.path())), stop_rx));
        assert!(eventually(|| pids(&a).len() == 1).await);
        let pid = pids(&a)[0];
        fs::write(a.join(DISABLED_FILE), "").unwrap();
        assert!(eventually(|| !alive(pid)).await, "the agent stops within a scan");
        tokio::time::sleep(Duration::from_millis(400)).await;
        assert_eq!(pids(&a).len(), 1, "and is not started again");
        stop_tx.send(true).unwrap();
        task.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn a_changed_config_restarts_its_agent() {
        let tmp = tempfile::tempdir().unwrap();
        let a = camera(tmp.path(), A);
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(supervise(options(tmp.path(), fake_agent(tmp.path())), stop_rx));
        assert!(eventually(|| pids(&a).len() == 1).await);
        fs::write(a.join(CONFIG_FILE), "[brain]\nurl = \"https://proxy:8443\"\npair_token = \"ABCD-EFGH\"\n").unwrap();
        assert!(eventually(|| pids(&a).len() == 2).await, "a new agent runs with the new config");
        assert!(!alive(pids(&a)[0]) && alive(pids(&a)[1]));
        stop_tx.send(true).unwrap();
        task.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn an_agent_that_exits_is_started_again_with_backoff() {
        let tmp = tempfile::tempdir().unwrap();
        let a = camera(tmp.path(), A);
        let exiting = tmp.path().join("exiting-agent.sh");
        fs::write(&exiting, format!("#!/bin/sh\nFAKE_AGENT_EXIT=1 exec {} \"$@\"\n", fake_agent(tmp.path()).display())).unwrap();
        fs::set_permissions(&exiting, fs::Permissions::from_mode(0o755)).unwrap();
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(supervise(options(tmp.path(), exiting), stop_rx));
        assert!(eventually(|| pids(&a).len() >= 3).await, "restarted after each exit");
        stop_tx.send(true).unwrap();
        task.await.unwrap().unwrap();
    }

    #[test]
    fn options_have_the_container_defaults() {
        let o = options_from_args(&[]).unwrap();
        assert_eq!(o.root, PathBuf::from("/srv/loopi/cameras"));
        assert_eq!((o.scan, o.stop_grace), (Duration::from_secs(5), Duration::from_secs(15)));
        let o = options_from_args(&["--root".into(), "/x".into(), "--agent".into(), "/bin/agent".into()]).unwrap();
        assert_eq!((o.root, o.agent_cmd), (PathBuf::from("/x"), PathBuf::from("/bin/agent")));
        assert!(options_from_args(&["--bogus".into()]).is_err());
    }

    #[test]
    fn agent_lines_carry_the_camera_id() {
        assert_eq!(prefixed(A, "paired"), format!("[{A}] paired"));
    }
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd agent && cargo test --lib cameras::`
Expected: compile error — module `cameras` does not exist.

- [ ] **Step 3: Implement**

`agent/src/cameras.rs` (above its tests):

```rust
//! `loopi-cameras` (P3c design §2, D30): one `loopi-agent run --config <dir>/loopi.toml` per network camera directory
//! under the root (`/srv/loopi/cameras` in the container). The brain writes the directories; this supervisor starts an
//! agent for every `<ULID>/loopi.toml` without a `disabled` file, restarts an agent that exits (with backoff) or whose
//! config changed (at once), stops the agent of a disabled or removed camera, and on SIGTERM stops them all. Agents die
//! with it (PR_SET_PDEATHSIG). Their output lines are passed on prefixed with the camera id.
use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, SystemTime};

use tokio::io::{AsyncBufReadExt, AsyncRead, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::watch;
use tokio::time::Instant;

use crate::child::die_with_parent_tokio;

pub const CONFIG_FILE: &str = "loopi.toml";
pub const DISABLED_FILE: &str = "disabled";
pub const DEFAULT_ROOT: &str = "/srv/loopi/cameras";

#[derive(Debug, Clone)]
pub struct SupervisorOptions {
    pub root: PathBuf,
    pub agent_cmd: PathBuf,
    pub scan: Duration,
    pub stop_grace: Duration,
    pub min_backoff: Duration,
    pub max_backoff: Duration,
    pub healthy_run: Duration,
}

/// `loopi-cameras [--root DIR] [--agent PATH]`; the agent defaults to `loopi-agent` next to this binary.
pub fn options_from_args(args: &[String]) -> Result<SupervisorOptions, String> {
    let next_to_us = std::env::current_exe().ok().and_then(|p| p.parent().map(|d| d.join("loopi-agent")));
    let mut opts = SupervisorOptions {
        root: DEFAULT_ROOT.into(),
        agent_cmd: next_to_us.filter(|p| p.exists()).unwrap_or_else(|| "loopi-agent".into()),
        scan: Duration::from_secs(5),
        stop_grace: Duration::from_secs(15),
        min_backoff: Duration::from_secs(2),
        max_backoff: Duration::from_secs(60),
        healthy_run: Duration::from_secs(60),
    };
    let mut it = args.iter();
    while let Some(flag) = it.next() {
        let value = it.next().ok_or_else(|| format!("{flag} needs a value"))?;
        match flag.as_str() {
            "--root" => opts.root = value.into(),
            "--agent" => opts.agent_cmd = value.into(),
            other => return Err(format!("unknown flag {other:?}; use loopi-cameras [--root DIR] [--agent PATH]")),
        }
    }
    Ok(opts)
}

/// A camera id is the brain's ULID: 26 characters of 0-9 and A-Z.
pub fn is_camera_id(name: &str) -> bool {
    name.len() == 26 && name.bytes().all(|b| b.is_ascii_digit() || b.is_ascii_uppercase())
}

/// What tells a changed config apart: its modification time and length.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Stamp {
    modified: Option<SystemTime>,
    len: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Wanted {
    pub id: String,
    pub config: PathBuf,
    pub stamp: Stamp,
}

/// The cameras to run now, in id order.
pub fn scan(root: &Path) -> io::Result<Vec<Wanted>> {
    let mut wanted = Vec::new();
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let Some(id) = entry.file_name().to_str().map(str::to_string) else { continue };
        let dir = entry.path();
        if !is_camera_id(&id) || !dir.is_dir() || dir.join(DISABLED_FILE).exists() {
            continue;
        }
        let config = dir.join(CONFIG_FILE);
        let Ok(meta) = fs::metadata(&config) else { continue };
        if meta.is_file() {
            wanted.push(Wanted { id, config, stamp: Stamp { modified: meta.modified().ok(), len: meta.len() } });
        }
    }
    wanted.sort_by(|a, b| a.id.cmp(&b.id));
    Ok(wanted)
}

pub fn prefixed(id: &str, line: &str) -> String {
    format!("[{id}] {line}")
}

struct Running {
    child: Child,
    stamp: Stamp,
    started: Instant,
}

fn backoff(failures: u32, min: Duration, max: Duration) -> Duration {
    min.saturating_mul(1u32 << failures.saturating_sub(1).min(6)).min(max)
}

fn forward_lines<R: AsyncRead + Unpin + Send + 'static>(id: String, stream: R) {
    tokio::spawn(async move {
        let mut lines = BufReader::new(stream).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            eprintln!("{}", prefixed(&id, &line));
        }
    });
}

fn spawn_agent(agent: &Path, wanted: &Wanted) -> io::Result<Child> {
    let mut command = Command::new(agent);
    command
        .arg("run")
        .arg("--config")
        .arg(&wanted.config)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    die_with_parent_tokio(&mut command);
    let mut child = command.spawn()?;
    if let Some(out) = child.stdout.take() {
        forward_lines(wanted.id.clone(), out);
    }
    if let Some(err) = child.stderr.take() {
        forward_lines(wanted.id.clone(), err);
    }
    Ok(child)
}

fn terminate(child: &Child) {
    if let Some(pid) = child.id() {
        // SAFETY: kill(2) on our own child.
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGTERM);
        }
    }
}

/// SIGTERM, then SIGKILL after `grace` (the agent drains its recorders within 15 s).
async fn stop(child: &mut Child, grace: Duration) {
    terminate(child);
    if tokio::time::timeout(grace, child.wait()).await.is_err() {
        let _ = child.kill().await;
    }
}

pub async fn supervise(opts: SupervisorOptions, mut shutdown: watch::Receiver<bool>) -> io::Result<()> {
    let mut running: HashMap<String, Running> = HashMap::new();
    let mut retry: HashMap<String, (u32, Instant)> = HashMap::new(); // failures, not before
    let mut ticker = tokio::time::interval(opts.scan);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            _ = ticker.tick() => {}
            changed = shutdown.changed() => if changed.is_err() || *shutdown.borrow() { break; },
        }
        let exited: Vec<(String, Duration)> = running
            .iter_mut()
            .filter_map(|(id, r)| match r.child.try_wait() {
                Ok(Some(_)) | Err(_) => Some((id.clone(), r.started.elapsed())),
                Ok(None) => None,
            })
            .collect();
        for (id, ran_for) in exited {
            running.remove(&id);
            let failures = if ran_for >= opts.healthy_run { 1 } else { retry.get(&id).map_or(1, |r| r.0 + 1) };
            let delay = backoff(failures, opts.min_backoff, opts.max_backoff);
            tracing::warn!(camera = %id, ran_s = ran_for.as_secs(), retry_s = delay.as_secs_f32(), "agent exited");
            retry.insert(id, (failures, Instant::now() + delay));
        }
        let wanted = match scan(&opts.root) {
            Ok(w) => w,
            Err(e) => {
                tracing::error!(error = %e, root = %opts.root.display(), "cannot read the cameras directory");
                continue;
            }
        };
        let by_id: HashMap<&str, &Wanted> = wanted.iter().map(|w| (w.id.as_str(), w)).collect();
        let to_stop: Vec<String> = running
            .iter()
            .filter(|(id, r)| by_id.get(id.as_str()).is_none_or(|w| w.stamp != r.stamp))
            .map(|(id, _)| id.clone())
            .collect();
        for id in to_stop {
            if let Some(mut r) = running.remove(&id) {
                tracing::info!(camera = %id, "stopping the agent (camera disabled, removed or reconfigured)");
                stop(&mut r.child, opts.stop_grace).await;
                retry.remove(&id);
            }
        }
        retry.retain(|id, _| by_id.contains_key(id.as_str()));
        for w in &wanted {
            if running.contains_key(&w.id) || retry.get(&w.id).is_some_and(|r| Instant::now() < r.1) {
                continue;
            }
            match spawn_agent(&opts.agent_cmd, w) {
                Ok(child) => {
                    tracing::info!(camera = %w.id, "agent started");
                    running.insert(w.id.clone(), Running { child, stamp: w.stamp, started: Instant::now() });
                }
                Err(e) => {
                    let failures = retry.get(&w.id).map_or(1, |r| r.0 + 1);
                    tracing::error!(camera = %w.id, error = %e, "cannot start the agent");
                    retry.insert(w.id.clone(), (failures, Instant::now() + backoff(failures, opts.min_backoff, opts.max_backoff)));
                }
            }
        }
    }
    for r in running.values() {
        terminate(&r.child);
    }
    for (_, mut r) in running.drain() {
        stop(&mut r.child, opts.stop_grace).await;
    }
    Ok(())
}

/// SIGTERM or SIGINT ends `supervise`.
pub fn shutdown_signal() -> io::Result<watch::Receiver<bool>> {
    use tokio::signal::unix::{signal, SignalKind};
    let (tx, rx) = watch::channel(false);
    let mut term = signal(SignalKind::terminate())?;
    let mut int = signal(SignalKind::interrupt())?;
    tokio::spawn(async move {
        tokio::select! { _ = term.recv() => {}, _ = int.recv() => {} }
        let _ = tx.send(true);
    });
    Ok(rx)
}
```

(`Option::is_none_or` needs Rust 1.82; with `rust-version = "1.80"` write
`by_id.get(id.as_str()).map_or(true, |w| w.stamp != r.stamp)` instead.)

`agent/src/bin/loopi-cameras.rs`:

```rust
//! The cameras container's entry point; the logic is `loopi_agent::cameras`.
use std::process::ExitCode;

fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let opts = match loopi_agent::cameras::options_from_args(&args) {
        Ok(opts) => opts,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(2);
        }
    };
    let runtime = match tokio::runtime::Builder::new_current_thread().enable_all().build() {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    let result = runtime.block_on(async {
        let shutdown = loopi_agent::cameras::shutdown_signal()?;
        loopi_agent::cameras::supervise(opts, shutdown).await
    });
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            tracing::error!(error = %e, "loopi-cameras stopped");
            ExitCode::FAILURE
        }
    }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd agent && cargo test && cargo build --bins && cargo llvm-cov --ignore-filename-regex 'main.rs' --summary-only | tail -3`
Expected: PASS; both binaries build; coverage ≥ 80 %.

- [ ] **Step 5: Commit**

```bash
git add agent/src agent/tests/fixtures/fake-agent.sh
git commit -m "feat(agent): loopi-cameras runs one agent per network camera directory (P3c-2, D30)"
```

---

### Task 5: The `cameras` container

**Files:**
- Create: `deploy/cameras.Dockerfile`, `.dockerignore` (repo root)
- Modify: `deploy/docker-compose.yml` (service `cameras`)
- Test: `brain/tests/test_deploy.py`

**Interfaces:**
- Consumes: Task 4's binaries.
- Produces: compose service `cameras` (no ports), mounting `${LOOPI_DATA_DIR:-/srv/loopi}/cameras` at
  `/srv/loopi/cameras` and `/run/systemd/timesync` read-only.

- [ ] **Step 1: Write the failing test**

In `brain/tests/test_deploy.py` (it already loads `deploy/docker-compose.yml`; reuse its loader):

```python
def test_the_cameras_service_runs_the_agents_with_only_their_directory():
    service = compose()["services"]["cameras"]
    assert service["build"] == {"context": "..", "dockerfile": "deploy/cameras.Dockerfile"}
    assert service["user"] == "${LOOPI_UID:-1000}:${LOOPI_GID:-1000}"
    assert service["init"] is True
    assert service["volumes"] == [
        "${LOOPI_DATA_DIR:-/srv/loopi}/cameras:/srv/loopi/cameras",
        "/run/systemd/timesync:/run/systemd/timesync:ro",
    ]
    assert service["depends_on"] == {"api": {"condition": "service_healthy"}}
    assert service["restart"] == "unless-stopped"
    assert "ports" not in service and "environment" not in service


def test_the_cameras_image_builds_both_binaries_and_runs_as_the_data_owner():
    text = (DEPLOY / "cameras.Dockerfile").read_text()
    assert "cargo build --release --locked --bin loopi-agent --bin loopi-cameras" in text
    assert "ffmpeg" in text and "USER loopi" in text
    assert 'CMD ["loopi-cameras", "--root", "/srv/loopi/cameras"]' in text
    ignore = (DEPLOY.parent / ".dockerignore").read_text().split()
    assert ignore[:2] == ["*", "!agent/"] and "agent/target/" in ignore
```

(Use the module's existing names for the compose loader and the deploy directory; adapt `compose()` / `DEPLOY` to them.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd brain && uv run pytest tests/test_deploy.py -q`
Expected: FAIL — no `cameras` service, no Dockerfile.

- [ ] **Step 3: Implement**

`deploy/cameras.Dockerfile`:

```dockerfile
# Network cameras (P3c design §2, D30): one Loopi agent per camera, supervised by loopi-cameras. Build context: the
# repository root (see .dockerignore: only agent/ is sent).
FROM rust:1.93-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libasound2-dev libopus-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY agent/ ./agent/
RUN cd agent && cargo build --release --locked --bin loopi-agent --bin loopi-cameras

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libasound2 libopus0 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 1000 loopi && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin loopi
COPY --from=build /src/agent/target/release/loopi-agent /src/agent/target/release/loopi-cameras /usr/local/bin/
USER loopi
CMD ["loopi-cameras", "--root", "/srv/loopi/cameras"]
```

`.dockerignore` (repository root):

```
*
!agent/
agent/target/
```

`deploy/docker-compose.yml`, a new service after `proxy`:

```yaml
  # Network cameras (P3c-2): one agent per camera directory the brain writes under <data>/cameras. Only that directory
  # is mounted (never the pki keys); the host's timesync flag tells the agents the clock is synced, as on the Pi.
  cameras:
    build:
      context: ..
      dockerfile: deploy/cameras.Dockerfile
    user: "${LOOPI_UID:-1000}:${LOOPI_GID:-1000}"
    init: true
    volumes:
      - ${LOOPI_DATA_DIR:-/srv/loopi}/cameras:/srv/loopi/cameras
      - /run/systemd/timesync:/run/systemd/timesync:ro
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_deploy.py -q`
Expected: PASS (and the rest of `test_deploy.py`).

- [ ] **Step 5: Commit**

```bash
git add deploy/cameras.Dockerfile .dockerignore deploy/docker-compose.yml brain/tests/test_deploy.py
git commit -m "feat(deploy): a cameras container runs loopi-cameras with only the cameras directory (P3c-2)"
```

---

### Task 6: Cameras in the brain: table, config files, pairing link

**Files:**
- Create: `brain/src/loopi_brain/migrations/012_network_camera.sql`, `brain/src/loopi_brain/cameras.py`
- Modify: `brain/src/loopi_brain/devices.py` (`create_pair_token` takes `camera_id`; `register_device` links)
- Modify: `brain/src/loopi_brain/config.py` (`cameras_dir`, `camera_brain_url`), `brain/src/loopi_brain/app.py` (create the dir)
- Test: `brain/tests/test_cameras.py` (create); `brain/tests/conftest.py` (`APP_TABLES`, `settings`), `brain/tests/test_db.py`

**Interfaces:**
- Produces:
  - table `network_camera(id, kind, location, video_url, audio_url, username, has_audio, device_id, created_at, removed_at)`, column `pair_token.camera_id`.
  - `Settings.cameras_dir: Path = Path("/srv/loopi/cameras")`, `Settings.camera_brain_url: str = "https://proxy:8443"`.
  - `cameras.KINDS: dict[str, CameraKind]` (`CameraKind(label, video_template, username, has_audio, hint)`),
    `cameras.CameraError(ValueError)` (Vietnamese message), `cameras.CameraInput`, `cameras.clean_input(...) -> CameraInput`,
    `cameras.with_credentials(url, username, password) -> str`,
    `async cameras.add_camera(pool, settings, cam: CameraInput) -> str` (the camera id),
    `async cameras.repair_camera(pool, settings, camera_id) -> bool`, `async cameras.remove_camera(pool, settings, camera_id) -> bool`,
    `async cameras.list_cameras(pool) -> list[dict]`, constants `CAMERA_TOKEN_TTL = timedelta(hours=24)`,
    `CONTAINER_ROOT = "/srv/loopi/cameras"`, `CONFIG_FILE = "loopi.toml"`, `DISABLED_FILE = "disabled"`.
  - `devices.create_pair_token(pool, ttl, camera_id: str | None = None)`.

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_cameras.py`:

```python
import datetime as dt
import stat
import tomllib

import pytest

from helpers import fetch_one, make_csr_pem, HW
from loopi_brain import cameras, devices, pki

PASSWORD = "S3cr3t-pw"


def cam(**over) -> cameras.CameraInput:
    fields = dict(kind="ip", location="Phòng khách", video_url="rtsp://192.0.2.10:554/cam/realmonitor?channel=1&subtype=0",
                  audio_url="", username="admin", password=PASSWORD, has_audio=True)
    fields.update(over)
    return cameras.clean_input(**fields)


def config_of(settings, camera_id) -> dict:
    return tomllib.loads((settings.cameras_dir / camera_id / "loopi.toml").read_text())


async def test_a_camera_gets_a_mode_600_config_its_agent_can_run(pool, pki_settings):
    camera_id = await cameras.add_camera(pool, pki_settings, cam())
    path = pki_settings.cameras_dir / camera_id / "loopi.toml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    config = config_of(pki_settings, camera_id)
    assert config["brain"]["url"] == "https://proxy:8443"
    assert config["brain"]["ca_fingerprint"] == pki.fingerprint(pki.ca_cert_pem(pki_settings.pki_dir))
    token = config["brain"]["pair_token"]
    row = await fetch_one(pool, "SELECT camera_id, expires_at - now() AS ttl FROM pair_token WHERE token = %s", (token,))
    assert row["camera_id"] == camera_id and dt.timedelta(hours=23) < row["ttl"] <= dt.timedelta(hours=24)
    capture = config["capture"]
    assert capture["source"] == "stream" and capture["stream_url"].startswith("rtsp://192.0.2.10:554/")
    assert (capture["stream_username"], capture["stream_password"]) == ("admin", PASSWORD)
    assert capture["audio_enabled"] is True and capture["audio_gain_db"] == 0.0
    assert (capture["video_fps"], capture["video_bitrate_bps"]) == (2, 400000) and "audio_url" not in capture
    assert config["led"] == {"source": "none"}
    assert config["paths"]["data_dir"] == f"/srv/loopi/cameras/{camera_id}/data"
    assert config["schedule"]["timezone"] == pki_settings.tz


async def test_the_password_is_only_in_the_mode_600_file(pool, pki_settings):
    camera_id = await cameras.add_camera(pool, pki_settings, cam())
    row = await fetch_one(pool, "SELECT * FROM network_camera WHERE id = %s", (camera_id,))
    assert PASSWORD not in repr(row)
    assert row["username"] == "admin" and row["video_url"].startswith("rtsp://192.0.2.10")
    listed = await cameras.list_cameras(pool)
    assert PASSWORD not in repr(listed)


async def test_pairing_links_the_camera_and_names_its_place(pool, pki_settings):
    camera_id = await cameras.add_camera(pool, pki_settings, cam(location="Bếp"))
    token = config_of(pki_settings, camera_id)["brain"]["pair_token"]
    paired = await devices.register_device(pool, token, HW, lambda device_id: pki.sign_csr(pki_settings.pki_dir, make_csr_pem(), device_id))
    camera = await fetch_one(pool, "SELECT device_id FROM network_camera WHERE id = %s", (camera_id,))
    device = await fetch_one(pool, "SELECT location FROM device WHERE id = %s", (paired["device_id"],))
    assert camera["device_id"] == paired["device_id"] and device["location"] == "Bếp"


async def test_a_sound_url_and_a_camera_without_sound(pool, pki_settings):
    esp = await cameras.add_camera(pool, pki_settings, cam(kind="esp32", video_url="http://192.0.2.20/stream",
                                                            audio_url="http://192.0.2.20/audio", username="", password=""))
    assert config_of(pki_settings, esp)["capture"]["audio_url"] == "http://192.0.2.20/audio"
    silent = await cameras.add_camera(pool, pki_settings, cam(kind="esp32", video_url="http://192.0.2.21:81/stream",
                                                               username="", password="", has_audio=False))
    assert config_of(pki_settings, silent)["capture"]["audio_enabled"] is False


async def test_repairing_issues_a_new_token_and_keeps_the_password(pool, pki_settings):
    camera_id = await cameras.add_camera(pool, pki_settings, cam())
    first = config_of(pki_settings, camera_id)["brain"]["pair_token"]
    assert await cameras.repair_camera(pool, pki_settings, camera_id) is True
    config = config_of(pki_settings, camera_id)
    assert config["brain"]["pair_token"] != first and config["capture"]["stream_password"] == PASSWORD
    assert await cameras.repair_camera(pool, pki_settings, "01J0000000000000000000NOPE") is False


async def test_removing_a_camera_scrubs_the_password_and_revokes(pool, pki_settings):
    camera_id = await cameras.add_camera(pool, pki_settings, cam())
    token = config_of(pki_settings, camera_id)["brain"]["pair_token"]
    paired = await devices.register_device(pool, token, HW, lambda d: pki.sign_csr(pki_settings.pki_dir, make_csr_pem(), d))
    assert await cameras.remove_camera(pool, pki_settings, camera_id) is True
    camera_dir = pki_settings.cameras_dir / camera_id
    assert (camera_dir / "disabled").exists()
    assert config_of(pki_settings, camera_id)["capture"]["stream_password"] == ""
    assert (await fetch_one(pool, "SELECT revoked FROM device WHERE id = %s", (paired["device_id"],)))["revoked"] is True
    assert await cameras.list_cameras(pool) == []
    assert await cameras.remove_camera(pool, pki_settings, camera_id) is False  # already removed


@pytest.mark.parametrize(("over", "message"), [
    (dict(location="  "), "Tên vị trí không được để trống."),
    (dict(kind="webcam"), "Loại camera không hợp lệ."),
    (dict(video_url="rtsp://<IP>:554/cam"), "Thay <IP> bằng địa chỉ của camera."),
    (dict(video_url="ftp://192.0.2.10/x"), "Địa chỉ phải bắt đầu bằng rtsp://, http:// hoặc https://."),
    (dict(video_url="rtsp://admin:pw@192.0.2.10/x"), "Đừng ghi tên đăng nhập hay mật khẩu vào địa chỉ"),
    (dict(video_url="rtsp://192.0.2.10/a b"), "Địa chỉ không được có dấu cách."),
    (dict(audio_url="ftp://192.0.2.10/a"), "Địa chỉ âm thanh phải bắt đầu bằng http://, https:// hoặc rtsp://."),
])
def test_bad_camera_input_is_refused_with_a_vietnamese_reason(over, message):
    with pytest.raises(cameras.CameraError, match=message):
        cam(**over)


def test_credentials_are_percent_encoded_for_ffprobe():
    assert cameras.with_credentials("rtsp://192.0.2.10/cam", "admin", "p:w@") == "rtsp://admin:p%3Aw%40@192.0.2.10/cam"
    assert cameras.with_credentials("http://192.0.2.10/s", "", "") == "http://192.0.2.10/s"


async def test_a_failed_file_write_leaves_no_camera_behind(pool, pki_settings, monkeypatch):
    monkeypatch.setattr(cameras, "write_config", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        await cameras.add_camera(pool, pki_settings, cam())
    assert await fetch_one(pool, "SELECT count(*) AS n FROM network_camera") == {"n": 0}
```

`brain/tests/conftest.py`: add `"network_camera"` to `APP_TABLES`, `cameras_dir=tmp_path / "cameras"` to the
`settings` fixture. `brain/tests/test_db.py`: add `"network_camera"` to `EXPECTED_TABLES` and
`"012_network_camera.sql"` after `"011_device_location.sql"` in the three lists.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_cameras.py tests/test_db.py -q`
Expected: FAIL — no `cameras` module, no table.

- [ ] **Step 3: Implement**

`brain/src/loopi_brain/migrations/012_network_camera.sql`:

```sql
-- P3c-2 (design 2026-10-19-p3c-multicam-design.md §4.1, D30, D36): network cameras added from the Thiết bị page. A
-- camera is an ordinary device to the protocol once its agent pairs; this row keeps what the form said (never the
-- password, which lives only in the camera's loopi.toml, mode 600) and links the device the camera's pair token made.
CREATE TABLE network_camera (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('ip', 'phone', 'esp32', 'other')),
    location    TEXT NOT NULL CHECK (btrim(location) <> '' AND length(location) <= 60),
    video_url   TEXT NOT NULL,
    audio_url   TEXT,
    username    TEXT NOT NULL DEFAULT '',
    has_audio   BOOLEAN NOT NULL,
    device_id   TEXT REFERENCES device(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at  TIMESTAMPTZ
);
CREATE INDEX network_camera_device ON network_camera (device_id);
ALTER TABLE pair_token ADD COLUMN camera_id TEXT REFERENCES network_camera(id);
```

`brain/src/loopi_brain/config.py`: after `ota_dir`:

```python
    # P3c-2: one directory per network camera (loopi.toml + the agent's data), shared with the cameras container.
    cameras_dir: Path = Path("/srv/loopi/cameras")
    # How camera agents in the compose network reach Caddy (R2: the agent pins the brain CA, not the host name).
    camera_brain_url: str = "https://proxy:8443"
```

`brain/src/loopi_brain/app.py`: next to `settings.media_root.mkdir(...)` add
`settings.cameras_dir.mkdir(parents=True, exist_ok=True)`.

`brain/src/loopi_brain/devices.py`:
- `create_pair_token(pool, ttl, camera_id: str | None = None)` inserts `camera_id` too;
- in `register_device`, after `UPDATE pair_token SET device_id = …`:
  ```python
            cur = await conn.execute(
                "UPDATE network_camera c SET device_id = %s FROM pair_token t"
                " WHERE t.token = %s AND t.camera_id = c.id AND c.device_id IS NULL RETURNING c.location",
                (device_id, token),
            )
            camera = await cur.fetchone()
            if camera is not None:  # a network camera's agent: its device takes the camera's place name (D34)
                await conn.execute("UPDATE device SET location = %s WHERE id = %s", (camera["location"], device_id))
  ```

`brain/src/loopi_brain/cameras.py`:

```python
"""Network cameras (P3c design §2, §4.1, D30, D36). The Thiết bị form adds one: the brain stores what the form said
(never the password), creates a pair token tied to it (24 h, R6) and writes `<cameras_dir>/<id>/loopi.toml` (mode
600) for the cameras container, whose supervisor starts an agent for it within seconds. Removing a camera writes a
`disabled` file (its agent stops), rewrites the file without the password (R9), and revokes its device; what it
recorded stays until retention or "Quên"."""
from __future__ import annotations

import datetime as dt
import json
import os
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from . import devices, media, pki, places
from .config import Settings

CAMERA_TOKEN_TTL = dt.timedelta(hours=24)
CONTAINER_ROOT = "/srv/loopi/cameras"  # the same path inside the cameras container
CONFIG_FILE = "loopi.toml"
DISABLED_FILE = "disabled"
URL_MAX = 1000
VIDEO_FPS = 2
VIDEO_BITRATE_BPS = 400_000


@dataclass(frozen=True)
class CameraKind:
    label: str
    video_template: str
    username: str
    has_audio: bool
    hint: str


KINDS: dict[str, CameraKind] = {
    "ip": CameraKind("Camera IP (RTSP)", "rtsp://<IP>:554/cam/realmonitor?channel=1&subtype=0", "admin", True,
                     "Camera Imou: tên đăng nhập admin, mật khẩu là mã an toàn in dưới đáy camera."),
    "phone": CameraKind("Điện thoại — iPhone/Android (app phát RTSP hoặc MJPEG)", "http://<IP>:8080/video", "", True,
                        "Android \"IP Webcam\": http://<IP>:8080/video. iPhone: dùng địa chỉ RTSP mà app hiện ra."),
    "esp32": CameraKind("ESP32 có camera (MJPEG qua HTTP)", "http://<IP>:81/stream", "", False,
                        "Firmware có sẵn: http://<IP>:81/stream, không có âm thanh. Firmware Loopi: http://<IP>/stream"
                        " và âm thanh ở http://<IP>/audio."),
    "other": CameraKind("Khác (URL RTSP/HTTP tuỳ ý)", "", "", True, "Dán địa chỉ RTSP hoặc HTTP của luồng."),
}


class CameraError(ValueError):
    """Input the form refuses; the message is shown to the owner as it is."""


@dataclass(frozen=True)
class CameraInput:
    kind: str
    location: str
    video_url: str
    audio_url: str | None
    username: str
    password: str
    has_audio: bool

    def __repr__(self) -> str:  # never the password
        return f"CameraInput(kind={self.kind!r}, location={self.location!r}, video_url={self.video_url!r})"


def _clean_url(text: str, *, schemes: tuple[str, ...], what: str) -> str:
    url = text.strip()
    if not url.startswith(schemes):
        joined = ", ".join(schemes[:-1]) + f" hoặc {schemes[-1]}"
        raise CameraError(f"{what} phải bắt đầu bằng {joined}.")
    if "<" in url or ">" in url:
        raise CameraError("Thay <IP> bằng địa chỉ của camera.")
    if any(c.isspace() for c in url) or not url.isprintable():
        raise CameraError("Địa chỉ không được có dấu cách.")
    if len(url) > URL_MAX:
        raise CameraError(f"Địa chỉ dài quá {URL_MAX} ký tự.")
    authority = url.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0]
    if "@" in authority:
        raise CameraError("Đừng ghi tên đăng nhập hay mật khẩu vào địa chỉ; điền chúng vào ô bên dưới.")
    if not authority:
        raise CameraError("Địa chỉ không có tên máy hoặc IP.")
    return url


def clean_input(*, kind: str, location: str, video_url: str, audio_url: str, username: str, password: str,
                has_audio: bool) -> CameraInput:
    if kind not in KINDS:
        raise CameraError("Loại camera không hợp lệ.")
    try:
        place = places.clean_location(location)
    except places.LocationError as exc:
        raise CameraError(str(exc)) from exc
    video = _clean_url(video_url, schemes=("rtsp://", "http://", "https://"), what="Địa chỉ")
    audio = _clean_url(audio_url, schemes=("http://", "https://", "rtsp://"), what="Địa chỉ âm thanh") if audio_url.strip() else None
    return CameraInput(kind, place, video, audio, username.strip(), password, bool(has_audio))


def with_credentials(url: str, username: str, password: str) -> str:
    """The address with `user:password@` for ffprobe and ffmpeg, both percent-encoded; as it is without a user."""
    if not username:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{urllib.parse.quote(username, safe='')}:{urllib.parse.quote(password, safe='')}@{rest}"


def _toml(value) -> str:
    """A TOML value: JSON strings are TOML basic strings; bools and numbers as TOML writes them."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value)


def render_config(camera_id: str, cam: CameraInput, *, token: str | None, ca_fingerprint: str, brain_url: str,
                  tz: str) -> str:
    brain = {"url": brain_url, "ca_fingerprint": ca_fingerprint} | ({"pair_token": token} if token else {})
    capture = {
        "source": "stream", "stream_url": cam.video_url, "stream_username": cam.username,
        "stream_password": cam.password, "audio_enabled": cam.has_audio, "audio_gain_db": 0.0,
        "video_fps": VIDEO_FPS, "video_bitrate_bps": VIDEO_BITRATE_BPS,
    } | ({"audio_url": cam.audio_url} if cam.audio_url else {})
    sections = {
        "brain": brain, "capture": capture, "schedule": {"timezone": tz}, "led": {"source": "none"},
        "paths": {"data_dir": f"{CONTAINER_ROOT}/{camera_id}/data"},
    }
    lines = ["# Written by the brain (P3c-2) for the cameras container. Holds a password: mode 600, never commit."]
    for name, table in sections.items():
        lines.append(f"\n[{name}]")
        lines.extend(f"{key} = {_toml(value)}" for key, value in table.items())
    return "\n".join(lines) + "\n"


def write_config(cameras_dir: Path, camera_id: str, text: str) -> None:
    """Atomically: a mode-600 temp file, fsync, rename, fsync of the directory."""
    directory = cameras_dir / camera_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = directory / f".{CONFIG_FILE}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, directory / CONFIG_FILE)
    media.fsync_path(directory)


def _stored_password(settings: Settings, camera_id: str) -> str:
    path = settings.cameras_dir / camera_id / CONFIG_FILE
    try:
        return tomllib.loads(path.read_text())["capture"].get("stream_password", "")
    except (OSError, tomllib.TOMLDecodeError, KeyError):
        return ""


def _input_from_row(row: dict, password: str) -> CameraInput:
    return CameraInput(row["kind"], row["location"], row["video_url"], row["audio_url"], row["username"], password,
                       row["has_audio"])


async def _token(conn, camera_id: str) -> str:
    cur = await conn.execute(
        "INSERT INTO pair_token (token, expires_at, camera_id) VALUES (%s, now() + %s, %s) RETURNING token",
        (devices._new_token(), CAMERA_TOKEN_TTL, camera_id),
    )
    return (await cur.fetchone())["token"]


def _fingerprint(settings: Settings) -> str:
    return pki.fingerprint(pki.ca_cert_pem(settings.pki_dir))


async def add_camera(pool: AsyncConnectionPool, settings: Settings, cam: CameraInput) -> str:
    camera_id = devices.new_id()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO network_camera (id, kind, location, video_url, audio_url, username, has_audio)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (camera_id, cam.kind, cam.location, cam.video_url, cam.audio_url, cam.username, cam.has_audio),
        )
        token = await _token(conn, camera_id)
        # Written before the commit: a failed write rolls the camera and its token back.
        write_config(settings.cameras_dir, camera_id, render_config(
            camera_id, cam, token=token, ca_fingerprint=_fingerprint(settings), brain_url=settings.camera_brain_url,
            tz=settings.tz))
    return camera_id


async def repair_camera(pool: AsyncConnectionPool, settings: Settings, camera_id: str) -> bool:
    """A new 24 h token for a camera that has not paired yet; the file keeps its password; the supervisor restarts
    the agent because the file changed."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT * FROM network_camera WHERE id = %s AND removed_at IS NULL AND device_id IS NULL FOR UPDATE",
            (camera_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        token = await _token(conn, camera_id)
        write_config(settings.cameras_dir, camera_id, render_config(
            camera_id, _input_from_row(row, _stored_password(settings, camera_id)), token=token,
            ca_fingerprint=_fingerprint(settings), brain_url=settings.camera_brain_url, tz=settings.tz))
    return True


async def remove_camera(pool: AsyncConnectionPool, settings: Settings, camera_id: str) -> bool:
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "UPDATE network_camera SET removed_at = now() WHERE id = %s AND removed_at IS NULL RETURNING *",
            (camera_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        directory = settings.cameras_dir / camera_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (directory / DISABLED_FILE).touch()  # first: the supervisor stops the agent within one scan
        write_config(settings.cameras_dir, camera_id, render_config(
            camera_id, _input_from_row(row, ""), token=None, ca_fingerprint=_fingerprint(settings),
            brain_url=settings.camera_brain_url, tz=settings.tz))
        if row["device_id"]:
            await conn.execute("UPDATE device SET revoked = TRUE WHERE id = %s", (row["device_id"],))
    return True


async def list_cameras(pool: AsyncConnectionPool) -> list[dict]:
    """Cameras not removed, oldest first, with their device's last heartbeat (no password anywhere)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT c.id, c.kind, c.location, c.video_url, c.audio_url, c.username, c.has_audio, c.device_id,"
            " c.created_at, d.last_heartbeat, d.last_heartbeat_at FROM network_camera c"
            " LEFT JOIN device d ON d.id = c.device_id WHERE c.removed_at IS NULL ORDER BY c.created_at, c.id"
        )
        return await cur.fetchall()
```

(`devices._new_token` exists; if `media.fsync_path` is named differently, use the fsync helper `media.py` has.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: PASS, every test.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain brain/tests
git commit -m "feat(brain): network cameras get a mode-600 agent config, a 24 h pair token and their place on pairing (P3c-2, D30, D36)"
```

---

### Task 7: "Thử kết nối" — ffprobe a stream

**Files:**
- Create: `brain/src/loopi_brain/camera_probe.py`
- Test: `brain/tests/test_camera_probe.py`

**Interfaces:**
- Consumes: `cameras.with_credentials` (Task 6).
- Produces: `camera_probe.PROBE_TIMEOUT_S = 10`, frozen dataclass
  `ProbeResult(ok: bool, message: str, codec: str | None = None, width: int | None = None, height: int | None = None,
  fps: float | None = None, has_audio: bool | None = None)`,
  `async camera_probe.probe(url: str, username: str, password: str, *, timeout: float = PROBE_TIMEOUT_S) -> ProbeResult`.

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_camera_probe.py`:

```python
import json

import pytest

from loopi_brain import camera_probe

STREAMS = {"streams": [
    {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "25/1"},
    {"codec_type": "audio", "codec_name": "aac"},
]}


def fake_ffprobe(tmp_path, monkeypatch, *, stdout="", stderr="", code=0, sleep=None):
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    script = fake / "ffprobe"
    body = f"exec sleep {sleep}\n" if sleep else (
        f"echo \"$@\" > {tmp_path}/argv\nprintf '%s' '{stdout}'\nprintf '%s' '{stderr}' >&2\nexit {code}\n")
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")


async def test_a_stream_is_described(tmp_path, monkeypatch):
    fake_ffprobe(tmp_path, monkeypatch, stdout=json.dumps(STREAMS))
    result = await camera_probe.probe("rtsp://192.0.2.10/cam", "admin", "S3cr3t")
    assert result == camera_probe.ProbeResult(True, "Kết nối được: h264 1920×1080, 25 khung hình/giây, có âm thanh.",
                                              "h264", 1920, 1080, 25.0, True)
    argv = (tmp_path / "argv").read_text()
    assert "rtsp://admin:S3cr3t@192.0.2.10/cam" in argv and "-rtsp_transport tcp" in argv


@pytest.mark.parametrize(("stderr", "message"), [
    ("[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized (rtsp://admin:S3cr3t@192.0.2.10/cam)",
     "Camera từ chối tên đăng nhập hoặc mật khẩu."),
    ("Server returned 404 Not Found", "Không tìm thấy luồng ở địa chỉ này."),
    ("Connection to tcp://192.0.2.10:554 failed: Connection refused", "Camera từ chối kết nối (kiểm tra cổng)."),
    ("Connection timed out", "Không kết nối được tới camera."),
    ("weird", "Không đọc được luồng từ camera."),
])
async def test_ffprobe_errors_never_echo_the_address(tmp_path, monkeypatch, caplog, stderr, message):
    fake_ffprobe(tmp_path, monkeypatch, stderr=stderr, code=1)
    result = await camera_probe.probe("rtsp://192.0.2.10/cam", "admin", "S3cr3t")
    assert result == camera_probe.ProbeResult(False, message)
    assert "S3cr3t" not in caplog.text and "192.0.2.10" not in caplog.text


async def test_a_camera_that_does_not_answer_in_time(tmp_path, monkeypatch):
    fake_ffprobe(tmp_path, monkeypatch, sleep=30)
    result = await camera_probe.probe("http://192.0.2.10/stream", "", "", timeout=0.3)
    assert result == camera_probe.ProbeResult(False, "Camera không trả lời trong 10 giây.")


async def test_a_stream_without_a_picture(tmp_path, monkeypatch):
    fake_ffprobe(tmp_path, monkeypatch, stdout=json.dumps({"streams": [{"codec_type": "audio", "codec_name": "pcm_s16le"}]}))
    result = await camera_probe.probe("http://192.0.2.10/audio", "", "")
    assert result == camera_probe.ProbeResult(False, "Luồng này không có hình.", has_audio=True)


async def test_mjpeg_over_http_has_no_rtsp_flags(tmp_path, monkeypatch):
    fake_ffprobe(tmp_path, monkeypatch, stdout=json.dumps({"streams": [
        {"codec_type": "video", "codec_name": "mjpeg", "width": 800, "height": 600, "avg_frame_rate": "0/0",
         "r_frame_rate": "10/1"}]}))
    result = await camera_probe.probe("http://192.0.2.20:81/stream", "", "")
    assert (result.ok, result.fps, result.has_audio) == (True, 10.0, False)
    assert "-rtsp_transport" not in (tmp_path / "argv").read_text()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_camera_probe.py -q`
Expected: FAIL — no module.

- [ ] **Step 3: Implement**

`brain/src/loopi_brain/camera_probe.py`:

```python
"""'Thử kết nối' (D36): ffprobe a camera's stream for at most PROBE_TIMEOUT_S seconds and say what it carries. ffprobe's
error output quotes the address with the password, so it is never logged or shown: it is reduced to a fixed message."""
from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass

from .cameras import with_credentials

PROBE_TIMEOUT_S = 10
TIMEOUT_US = str(PROBE_TIMEOUT_S * 1_000_000)


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    message: str
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool | None = None


def _args(url: str) -> list[str]:
    reading = ["-rtsp_transport", "tcp", "-timeout", TIMEOUT_US] if url.startswith("rtsp://") else ["-rw_timeout", TIMEOUT_US]
    return ["-v", "error", *reading, "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate", "-of", "json", url]


def _rate(text: str | None) -> float | None:
    try:
        num, den = (text or "").split("/")
        return float(num) / float(den) if float(den) else None
    except ValueError:
        return None


def _failure(stderr: str) -> str:
    text = stderr.lower()
    if any(k in text for k in ("401", "403", "unauthorized", "authorization failed")):
        return "Camera từ chối tên đăng nhập hoặc mật khẩu."
    if "404" in text or "not found" in text:
        return "Không tìm thấy luồng ở địa chỉ này."
    if "connection refused" in text:
        return "Camera từ chối kết nối (kiểm tra cổng)."
    if any(k in text for k in ("timed out", "timeout", "no route to host", "unreachable")):
        return "Không kết nối được tới camera."
    return "Không đọc được luồng từ camera."


async def probe(url: str, username: str, password: str, *, timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", *_args(with_credentials(url, username, password)),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return ProbeResult(False, "Camera không trả lời trong 10 giây.")
    if proc.returncode != 0:
        return ProbeResult(False, _failure(stderr.decode(errors="replace")))
    try:
        streams = json.loads(stdout or b"{}").get("streams", [])
    except json.JSONDecodeError:
        return ProbeResult(False, "Không đọc được luồng từ camera.")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return ProbeResult(False, "Luồng này không có hình.", has_audio=has_audio)
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    sound = "có âm thanh" if has_audio else "không có âm thanh"
    shown_fps = f"{fps:g} khung hình/giây" if fps else "không rõ số khung hình/giây"
    message = f"Kết nối được: {video.get('codec_name')} {video.get('width')}×{video.get('height')}, {shown_fps}, {sound}."
    return ProbeResult(True, message, video.get("codec_name"), video.get("width"), video.get("height"), fps, has_audio)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest tests/test_camera_probe.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain/camera_probe.py brain/tests/test_camera_probe.py
git commit -m "feat(brain): Thử kết nối probes a camera stream in 10 s and never echoes its address (P3c-2, D36)"
```

---

### Task 8: The Thiết bị page: add, probe, re-pair and remove cameras; the capacity estimate

**Files:**
- Create: `brain/src/loopi_brain/capacity.py`, `brain/src/loopi_brain/web/camera_routes.py`
- Create: templates `_camera_form.html`, `_camera_probe.html`, `_camera_message.html`
- Modify: `brain/src/loopi_brain/app.py` (include `camera_routes.router`), `web/routes.py` (`devices_page` context),
  `templates/devices.html`
- Test: `brain/tests/test_capacity.py`, `brain/tests/test_web_cameras.py`

**Interfaces:**
- Consumes: Tasks 6 and 7.
- Produces: `capacity.NOMINAL_BYTES_PER_DAY = 4_400_000_000`, `capacity.WARN_RATIO = 0.85`, frozen dataclass
  `Capacity(days, bytes_per_day, total_bytes, free_bytes, new_camera_bytes, ratio, warn_if_added)`,
  `async capacity.estimate(pool, settings) -> Capacity`, `capacity.gb(n: int) -> str`; routes `GET /cameras/form`,
  `POST /cameras/probe`, `POST /cameras`, `POST /cameras/{id}/repair`, `POST /cameras/{id}/remove`.

- [ ] **Step 1: Write the failing tests**

`brain/tests/test_capacity.py`:

```python
import datetime as dt

from helpers import insert_item, paired_device
from loopi_brain import cameras, capacity, devices


async def _heartbeat(pool, device_id):
    async with pool.connection() as conn:
        await conn.execute("UPDATE device SET last_heartbeat_at = now() WHERE id = %s", (device_id,))


async def test_measured_devices_and_cameras_still_waiting_count(pool, pki_settings, monkeypatch):
    monkeypatch.setattr(capacity, "_free_bytes", lambda settings: 200 * 10**9)
    pi = await paired_device(pool, pki_settings)
    await _heartbeat(pool, pi["device_id"])
    now = dt.datetime.now(dt.UTC)
    await insert_item(pool, pi["device_id"], kind="video", started_at=now - dt.timedelta(days=2))  # older than 24 h
    item = await insert_item(pool, pi["device_id"], kind="video", started_at=now - dt.timedelta(hours=1))
    async with pool.connection() as conn:
        await conn.execute("UPDATE item SET size_bytes = %s WHERE id = %s", (5 * 10**9, item))
    await cameras.add_camera(pool, pki_settings, cameras.clean_input(
        kind="ip", location="Bếp", video_url="rtsp://192.0.2.10/cam", audio_url="", username="", password="", has_audio=True))
    est = await capacity.estimate(pool, pki_settings)
    assert est.bytes_per_day == 5 * 10**9 + capacity.NOMINAL_BYTES_PER_DAY
    assert est.days == 7 and est.total_bytes == 7 * est.bytes_per_day
    assert est.new_camera_bytes == 7 * capacity.NOMINAL_BYTES_PER_DAY
    assert est.warn_if_added == (est.total_bytes + est.new_camera_bytes > 0.85 * 200 * 10**9)


async def test_an_unknown_free_space_never_warns(pool, pki_settings, monkeypatch):
    monkeypatch.setattr(capacity, "_free_bytes", lambda settings: None)
    est = await capacity.estimate(pool, pki_settings)
    assert (est.free_bytes, est.ratio, est.warn_if_added) == (None, None, False)


def test_gigabytes_are_shown_with_one_decimal():
    assert capacity.gb(31_000_000_000) == "31,0 GB" and capacity.gb(0) == "0,0 GB"
```

`brain/tests/test_web_cameras.py`:

```python
import json
import re

from helpers import fetch_one
from loopi_brain import camera_probe

HX = {"HX-Request": "true"}
FORM = dict(kind="ip", location="Phòng khách", video_url="rtsp://192.0.2.10:554/cam/realmonitor?channel=1&subtype=0",
            audio_url="", username="admin", password="S3cr3t", has_audio="on")


async def test_the_devices_page_offers_the_camera_form_and_the_estimate(client):
    page = (await client.get("/devices")).text
    assert "Thêm camera mạng" in page and 'hx-post="/cameras"' in page and 'hx-post="/cameras/probe"' in page
    assert "Dung lượng ước tính cho" in page
    assert "Camera IP (RTSP)" in page and "rtsp://&lt;IP&gt;:554/cam/realmonitor?channel=1&amp;subtype=0" in page


async def test_switching_the_kind_fills_its_template(client):
    form = (await client.get("/cameras/form", params={"kind": "esp32"})).text
    assert 'value="http://&lt;IP&gt;:81/stream"' in form and "Firmware Loopi" in form
    assert 'name="has_audio"' in form and "checked" not in form.split('name="has_audio"')[1].split(">")[0]


async def test_adding_a_camera(client, pool, pki_settings):
    assert (await client.post("/cameras", data=FORM)).status_code == 403
    r = await client.post("/cameras", data=FORM, headers=HX)
    assert r.status_code == 200 and "Đã thêm camera Phòng khách" in r.text and r.headers.get("HX-Refresh") == "true"
    row = await fetch_one(pool, "SELECT id, location FROM network_camera")
    assert row["location"] == "Phòng khách"
    assert (pki_settings.cameras_dir / row["id"] / "loopi.toml").exists()
    page = (await client.get("/devices")).text
    assert "Camera chờ ghép cặp" in page and "S3cr3t" not in page


async def test_a_refused_camera_keeps_the_form_without_the_password(client, pool):
    r = await client.post("/cameras", data=FORM | {"video_url": "rtsp://<IP>/cam"}, headers=HX)
    assert r.status_code == 200 and "Thay &lt;IP&gt; bằng địa chỉ của camera." in r.text
    assert "S3cr3t" not in r.text and 'value="Phòng khách"' in r.text
    assert await fetch_one(pool, "SELECT count(*) AS n FROM network_camera") == {"n": 0}


async def test_probing_shows_what_the_stream_carries(client, monkeypatch):
    async def fake_probe(url, username, password, **_):
        assert (url, username, password) == (FORM["video_url"], "admin", "S3cr3t")
        return camera_probe.ProbeResult(True, "Kết nối được: h264 1920×1080, 25 khung hình/giây, có âm thanh.")
    monkeypatch.setattr(camera_probe, "probe", fake_probe)
    r = await client.post("/cameras/probe", data=FORM, headers=HX)
    assert r.status_code == 200 and "Kết nối được: h264 1920×1080" in r.text


async def test_repairing_and_removing_a_waiting_camera(client, pool, pki_settings):
    await client.post("/cameras", data=FORM, headers=HX)
    camera_id = (await fetch_one(pool, "SELECT id FROM network_camera"))["id"]
    r = await client.post(f"/cameras/{camera_id}/repair", headers=HX)
    assert r.status_code == 200 and "Đã tạo token ghép cặp mới" in r.text
    r = await client.post(f"/cameras/{camera_id}/remove", headers=HX)
    assert r.status_code == 200 and "Đã gỡ camera Phòng khách" in r.text
    assert (pki_settings.cameras_dir / camera_id / "disabled").exists()
    assert (await client.post(f"/cameras/{camera_id}/remove", headers=HX)).status_code == 404


async def test_the_form_warns_when_the_disk_would_fill(client, monkeypatch):
    from loopi_brain import capacity
    monkeypatch.setattr(capacity, "_free_bytes", lambda settings: 10 * 10**9)
    page = (await client.get("/devices")).text
    assert "vượt 85 % chỗ trống" in page
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_capacity.py tests/test_web_cameras.py -q`
Expected: FAIL — no modules, no routes.

- [ ] **Step 3: Implement**

`brain/src/loopi_brain/capacity.py`:

```python
"""How much disk the next days of recording take (P3c design §4.1, §5): every active device's bytes of the last 24 h
(a camera that has not recorded a day yet counts at the nominal 4.4 GB/day) times the kept video days, against the
media disk's free space; the camera form warns when one more camera would pass 85 % of it."""
from __future__ import annotations

import shutil
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from . import store, volume
from .config import Settings

NOMINAL_BYTES_PER_DAY = 4_400_000_000  # one network camera: 720p, 2 fps, about 400 kbps, plus sound
WARN_RATIO = 0.85


@dataclass(frozen=True)
class Capacity:
    days: int
    bytes_per_day: int
    total_bytes: int
    free_bytes: int | None
    new_camera_bytes: int
    ratio: float | None
    warn_if_added: bool


def _free_bytes(settings: Settings) -> int | None:
    if not volume.ready(settings.media_root, settings.media_require_marker):
        return None
    try:
        return shutil.disk_usage(settings.media_root).free
    except OSError:
        return None


def gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB".replace(".", ",")


async def estimate(pool: AsyncConnectionPool, settings: Settings) -> Capacity:
    days = (await store.get_retention(pool)).video_days
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT coalesce(sum(i.size_bytes), 0)::bigint AS bytes FROM item i JOIN device d ON d.id = i.device_id"
            " WHERE NOT d.revoked AND d.last_heartbeat_at > now() - interval '7 days'"
            " AND i.started_at > now() - interval '24 hours'"
        )
        measured = (await cur.fetchone())["bytes"]
        cur = await conn.execute(
            "SELECT count(*) AS n FROM network_camera c LEFT JOIN device d ON d.id = c.device_id"
            " WHERE c.removed_at IS NULL AND (c.device_id IS NULL OR NOT EXISTS ("
            "   SELECT 1 FROM item i WHERE i.device_id = c.device_id AND i.started_at <= now() - interval '24 hours'))"
        )
        young = (await cur.fetchone())["n"]
    # A camera with less than a day of recordings is counted at the nominal rate instead of its partial day.
    if young:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT coalesce(sum(i.size_bytes), 0)::bigint AS bytes FROM item i JOIN network_camera c ON c.device_id = i.device_id"
                " WHERE c.removed_at IS NULL AND i.started_at > now() - interval '24 hours' AND NOT EXISTS ("
                "   SELECT 1 FROM item o WHERE o.device_id = c.device_id AND o.started_at <= now() - interval '24 hours')"
            )
            measured -= (await cur.fetchone())["bytes"]
    per_day = measured + young * NOMINAL_BYTES_PER_DAY
    total = per_day * days
    free = _free_bytes(settings)
    new_camera = NOMINAL_BYTES_PER_DAY * days
    ratio = total / free if free else None
    warn = free is not None and total + new_camera > WARN_RATIO * free
    return Capacity(days, per_day, total, free, new_camera, ratio, warn)
```

`brain/src/loopi_brain/web/camera_routes.py`:

```python
"""Network cameras on the Thiết bị page (P3c design §4.1, §4.5, D36)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .. import camera_probe, cameras, capacity
from .routes import TEMPLATES, require_htmx

router = APIRouter()


async def form_context(request: Request, kind: str = "ip", values: dict | None = None, error: str | None = None) -> dict:
    kind = kind if kind in cameras.KINDS else "ip"
    meta = cameras.KINDS[kind]
    est = await capacity.estimate(request.app.state.pool, request.app.state.settings)
    defaults = {"kind": kind, "location": "", "video_url": meta.video_template, "audio_url": "",
                "username": meta.username, "has_audio": meta.has_audio}
    return {"kinds": cameras.KINDS, "meta": meta, "values": defaults | (values or {}), "error": error,
            "capacity": est, "gb": capacity.gb}


def _fields(kind, location, video_url, audio_url, username, has_audio) -> dict:
    return {"kind": kind, "location": location, "video_url": video_url, "audio_url": audio_url, "username": username,
            "has_audio": has_audio == "on"}


@router.get("/cameras/form", response_class=HTMLResponse)
async def camera_form(request: Request, kind: str = Query("ip")):
    return TEMPLATES.TemplateResponse(request, "_camera_form.html", await form_context(request, kind))


@router.post("/cameras/probe", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def probe(request: Request, video_url: str = Form(""), audio_url: str = Form(""), username: str = Form(""),
                password: str = Form("")):
    results = [await camera_probe.probe(video_url.strip(), username.strip(), password)]
    if audio_url.strip():
        sound = await camera_probe.probe(audio_url.strip(), username.strip(), password)
        message = "Âm thanh: có." if sound.has_audio else "Âm thanh: " + (sound.message if not sound.ok else "không có.")
        results.append(camera_probe.ProbeResult(sound.has_audio is True, message))
    return TEMPLATES.TemplateResponse(request, "_camera_probe.html", {"results": results})


@router.post("/cameras", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def add(request: Request, kind: str = Form("ip"), location: str = Form(""), video_url: str = Form(""),
              audio_url: str = Form(""), username: str = Form(""), password: str = Form(""), has_audio: str = Form("")):
    values = _fields(kind, location, video_url, audio_url, username, has_audio)
    try:
        cam = cameras.clean_input(**values, password=password)
    except cameras.CameraError as exc:
        return TEMPLATES.TemplateResponse(request, "_camera_form.html", await form_context(request, kind, values, str(exc)))
    await cameras.add_camera(request.app.state.pool, request.app.state.settings, cam)
    message = f"Đã thêm camera {cam.location}. Camera bắt đầu ghi trong khoảng 2 phút sau khi ghép cặp."
    return TEMPLATES.TemplateResponse(request, "_camera_message.html", {"message": message},
                                      headers={"HX-Refresh": "true"})


@router.post("/cameras/{camera_id}/repair", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def repair(request: Request, camera_id: str):
    if not await cameras.repair_camera(request.app.state.pool, request.app.state.settings, camera_id):
        raise HTTPException(status_code=404, detail="unknown or already paired camera")
    return TEMPLATES.TemplateResponse(request, "_camera_message.html",
                                      {"message": "Đã tạo token ghép cặp mới; camera thử ghép cặp lại trong vài giây."})


@router.post("/cameras/{camera_id}/remove", response_class=HTMLResponse, dependencies=[Depends(require_htmx)])
async def remove(request: Request, camera_id: str):
    pool = request.app.state.pool
    listed = {c["id"]: c for c in await cameras.list_cameras(pool)}
    if camera_id not in listed or not await cameras.remove_camera(pool, request.app.state.settings, camera_id):
        raise HTTPException(status_code=404, detail="unknown camera")
    message = f"Đã gỡ camera {listed[camera_id]['location']}. Dữ liệu đã ghi vẫn còn cho tới khi hết hạn hoặc bị Quên."
    return TEMPLATES.TemplateResponse(request, "_camera_message.html", {"message": message},
                                      headers={"HX-Refresh": "true"})
```

`app.py`: `from .web import camera_routes` and `app.include_router(camera_routes.router)` next to the OTA router.

`web/routes.py` `devices_page`: add to the context `**await camera_routes.form_context(request)` (import inside the
function to avoid an import cycle) and `"cameras_by_device": {c["device_id"]: c for c in cams if c["device_id"]}`,
`"waiting_cameras": [c for c in cams if not c["device_id"]]` with `cams = await cameras.list_cameras(pool)`.

`templates/_camera_form.html`:

```html
<form id="camera-form" hx-post="/cameras" hx-target="#camera-form" hx-swap="outerHTML">
  <p class="muted">Dung lượng ước tính cho {{ capacity.days }} ngày: {{ gb(capacity.total_bytes) }}
    {% if capacity.free_bytes is not none %}/ còn trống {{ gb(capacity.free_bytes) }}
    ({{ "%d"|format((capacity.ratio or 0) * 100) }} %){% else %}(chưa rõ chỗ trống){% endif %}.</p>
  {% if capacity.warn_if_added %}<p class="err">Thêm một camera sẽ cần khoảng {{ gb(capacity.new_camera_bytes) }} cho
    {{ capacity.days }} ngày, vượt 85 % chỗ trống còn lại.</p>{% endif %}
  <label>Loại camera
    <select name="kind" hx-get="/cameras/form" hx-target="#camera-form" hx-swap="outerHTML">
      {% for key, k in kinds.items() %}<option value="{{ key }}" {% if key == values.kind %}selected{% endif %}>{{ k.label }}</option>{% endfor %}
    </select></label>
  <p class="muted">{{ meta.hint }}</p>
  <label>Vị trí <input name="location" value="{{ values.location }}" maxlength="60" required></label>
  <label>Địa chỉ luồng hình <input name="video_url" value="{{ values.video_url }}" maxlength="1000" required size="60"></label>
  <label>Địa chỉ âm thanh riêng (nếu có) <input name="audio_url" value="{{ values.audio_url }}" maxlength="1000" size="60"></label>
  <label>Tên đăng nhập <input name="username" value="{{ values.username }}" autocomplete="off"></label>
  <label>Mật khẩu <input name="password" type="password" autocomplete="new-password"></label>
  <label><input type="checkbox" name="has_audio" {% if values.has_audio %}checked{% endif %}> Ghi âm từ camera này</label>
  <button type="button" hx-post="/cameras/probe" hx-include="#camera-form" hx-target="#camera-probe">Thử kết nối</button>
  <button>Lưu camera</button>
  <div id="camera-probe"></div>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
</form>
```

`templates/_camera_probe.html`:

```html
{% for r in results %}<p class="{{ 'online' if r.ok else 'err' }}">{{ r.message }}</p>{% endfor %}
```

`templates/_camera_message.html`:

```html
<p>{{ message }}</p>
```

`templates/devices.html`:
- under the pair-token block add
  ```html
  <section class="card" id="cameras">
    <h2>Thêm camera mạng</h2>
    {% include "_camera_form.html" %}
  </section>
  {% if waiting_cameras %}
  <section class="card" id="waiting-cameras">
    <h2>Camera chờ ghép cặp</h2>
    {% for c in waiting_cameras %}
    <div class="card" id="camera-{{ c.id }}">
      <strong>{{ c.location }}</strong> <span class="muted">{{ kinds[c.kind].label }} · {{ c.video_url }}</span>
      <button hx-post="/cameras/{{ c.id }}/repair" hx-target="#camera-{{ c.id }}">Ghép cặp lại</button>
      <button hx-post="/cameras/{{ c.id }}/remove" hx-target="#camera-{{ c.id }}" hx-confirm="Gỡ camera {{ c.location }}?">Gỡ camera</button>
    </div>
    {% endfor %}
  </section>
  {% endif %}
  ```
- inside each device card, after the location form, when `cameras_by_device.get(d.id)` is a camera `c`:
  `<p class="muted">Camera mạng: {{ kinds[c.kind].label }} · {{ c.video_url }}{% if not c.has_audio %} · không ghi âm{% endif %}</p>`
  and, next to "Thu hồi", `<button hx-post="/cameras/{{ c.id }}/remove" hx-target="#result-{{ d.id }}" hx-confirm="Gỡ camera {{ d.location }}?">Gỡ camera</button>`;
  hide the "Bật audio"/"Tắt audio" buttons when `c` and `not c.has_audio`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: PASS, every test (the OTA page tests still find their update form: Task 3 of P3c-1 anchored them on
`/update"`).

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain brain/tests
git commit -m "feat(brain): the Thiết bị page adds, probes, re-pairs and removes network cameras with a 7-day estimate (P3c-2, D36)"
```

---

### Task 9: "Đang ghi: N camera" on every page

**Files:**
- Modify: `brain/src/loopi_brain/devices.py` (`recording_count`), `brain/src/loopi_brain/web/routes.py` (`GET /status/recording`),
  `templates/base.html`; Create: `templates/_recording_now.html`
- Test: `brain/tests/test_recording_now.py`

**Interfaces:**
- Produces: `async devices.recording_count(pool) -> int` (not revoked, heartbeat in the last 45 s, `recording_video` true),
  route `GET /status/recording`.

- [ ] **Step 1: Write the failing tests**

```python
from psycopg.types.json import Jsonb

from helpers import paired_device


async def _beat(pool, device_id, *, recording, seconds_ago=0):
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE device SET last_heartbeat = %s, last_heartbeat_at = now() - make_interval(secs => %s) WHERE id = %s",
            (Jsonb({"recording_video": recording}), seconds_ago, device_id),
        )


async def test_every_page_asks_for_the_recording_line(client):
    page = (await client.get("/settings")).text
    assert 'hx-get="/status/recording"' in page and 'hx-trigger="load, every 15s"' in page


async def test_the_line_counts_recording_cameras_heard_from_lately(client, pool, pki_settings):
    a, b, c = (await paired_device(pool, pki_settings) for _ in range(3))
    await _beat(pool, a["device_id"], recording=True)
    await _beat(pool, b["device_id"], recording=False)
    await _beat(pool, c["device_id"], recording=True, seconds_ago=120)  # silent for two minutes
    r = await client.get("/status/recording")
    assert r.status_code == 200 and '<a href="/devices">Đang ghi: 1 camera</a>' in r.text
```

(Python's generator-in-tuple form above does not await; write the three `paired_device` calls on separate lines.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_recording_now.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`devices.py`:

```python
async def recording_count(pool: AsyncConnectionPool) -> int:
    """Devices recording video now (D31: the web is the sign for a network camera): not revoked, heard from within
    ONLINE_SECONDS, and their last heartbeat says recording_video."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM device WHERE NOT revoked AND last_heartbeat_at > now() - interval '45 seconds'"
            " AND (last_heartbeat->>'recording_video')::boolean"
        )
        return (await cur.fetchone())["n"]
```

`web/routes.py`:

```python
@router.get("/status/recording", response_class=HTMLResponse)
async def recording_now(request: Request):
    """The header line on every page (htmx, every 15 s)."""
    count = await devices.recording_count(request.app.state.pool)
    return TEMPLATES.TemplateResponse(request, "_recording_now.html", {"count": count})
```

`templates/_recording_now.html`: `<a href="/devices">Đang ghi: {{ count }} camera</a>`

`templates/base.html`: inside `<header>`, after `</nav>`:
`<span id="recording-now" hx-get="/status/recording" hx-trigger="load, every 15s"></span>`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain brain/tests/test_recording_now.py
git commit -m "feat(brain): every page shows how many cameras record now (P3c-2, D31)"
```

---

### Task 10: The P3c-1 leftovers (M2, M7, M8, wording)

**Files:**
- Modify: `brain/src/loopi_brain/places.py` (`active_cameras`), `web/search_routes.py`, `chat.py` (M2);
  `web/routes.py` `save_location`, `templates/_location.html`, `templates/devices.html` (M7);
  `web/day_routes.py` (M8); `web/settings_routes.py:82`, `templates/pipeline.html`, `templates/_vlog_card.html`,
  `templates/settings.html` (wording)
- Test: `brain/tests/test_p3c1_leftovers.py`; update the tests that pin the old wording

**Interfaces:**
- Produces: `async places.active_cameras(pool) -> int` (not revoked, heartbeat in the last 7 days);
  `chat.source_lines(hits, zone, named: bool | None = None)`.

- [ ] **Step 1: Write the failing tests**

```python
import datetime as dt

from helpers import insert_moment, paired_device
from loopi_brain import chat, places, search

HX = {"HX-Request": "true"}
NINE = dt.datetime(2026, 10, 14, 0, 0, tzinfo=dt.UTC)


async def _active(pool, *device_ids):
    async with pool.connection() as conn:
        for d in device_ids:
            await conn.execute("UPDATE device SET last_heartbeat_at = now() WHERE id = %s", (d,))


async def test_with_two_active_cameras_every_result_names_its_place(pool, pki_settings):
    a, b = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await places.set_location(pool, a["device_id"], "Bếp")
    await _active(pool, a["device_id"], b["device_id"])
    assert await places.active_cameras(pool) == 2
    await insert_moment(pool, a["device_id"], kind="speech", ts=NINE, text="nấu cơm", lang="vi")
    hits = (await search.search_moments(pool, None, "nấu cơm")).hits
    assert chat.source_lines(hits, pki_settings.zone, named=True) == ["[1] 2026-10-14 09:00 at Bếp, speech (vi): nấu cơm"]


async def test_a_bad_name_for_an_unknown_device_is_a_404(client):
    r = await client.post("/devices/NOPE/location", data={"location": " "}, headers=HX)
    assert r.status_code == 404


async def test_a_saved_name_also_updates_the_card_title(client, pool, pki_settings):
    dev = await paired_device(pool, pki_settings)
    r = await client.post(f"/devices/{dev['device_id']}/location", data={"location": "Bếp"}, headers=HX)
    assert f'<strong id="device-title-{dev["device_id"]}" hx-swap-oob="true">Bếp</strong>' in r.text
    page = (await client.get("/devices")).text
    assert f'<strong id="device-title-{dev["device_id"]}">Bếp</strong>' in page


async def test_a_camera_filter_keeps_the_day_s_data_and_names_the_camera(client, pool, pki_settings):
    a, b = await paired_device(pool, pki_settings), await paired_device(pool, pki_settings)
    await places.set_location(pool, b["device_id"], "Bếp")
    await insert_moment(pool, a["device_id"], kind="speech", ts=NINE, text="Một", lang="vi")
    text = (await client.get("/", params={"day": "2026-10-14", "device": b["device_id"]})).text
    assert "Không có dữ liệu cho ngày này." not in text and "Tạo lại recap" in text
    assert "<strong>Bếp</strong>" in text
```

Update the tests that pin "video thô" text in the four places below to the new wording.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd brain && uv run pytest tests/test_p3c1_leftovers.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

- `places.py`:
  ```python
  async def active_cameras(pool: AsyncConnectionPool) -> int:
      """Devices that are recording places now: not revoked and heard from in the last 7 days (old pairings left
      behind do not count)."""
      async with pool.connection() as conn:
          cur = await conn.execute(
              "SELECT count(*) AS n FROM device WHERE NOT revoked AND last_heartbeat_at > now() - interval '7 days'"
          )
          return (await cur.fetchone())["n"]
  ```
- M2 — `web/search_routes.py`: the Search page and the chat `sources` event use
  `named = places.several(h.device_id for h in hits) or await places.active_cameras(pool) >= 2`;
  `chat.source_lines(hits, zone, named=None)` uses `named` when given, else `places.several(...)`, and `chat_events`
  passes the same rule.
- M7 — `web/routes.py` `save_location`: first `if not await places.is_active_device(pool, device_id): raise
  HTTPException(404, "unknown device")` (add `places.is_active_device(pool, device_id) -> bool`: a non-revoked row),
  then validate; `templates/devices.html` title becomes `<strong id="device-title-{{ d.id }}">{{ d.location }}</strong>`;
  `templates/_location.html` adds, when `saved`,
  `<strong id="device-title-{{ d.id }}" hx-swap-oob="true">{{ d.location }}</strong>` after the form.
- M8 — `web/day_routes.py` `day_view`: `has_data` comes from
  `SELECT EXISTS (SELECT 1 FROM moment WHERE ts_start >= %s AND ts_start < %s) AS any` (the whole day, all cameras);
  when `device` is set and not in `day_cams`, append `{"id": device, "location": <its location>}` (from `device`).
- Wording: `web/settings_routes.py:82` "Đã lưu: giữ video thô …" → "Đã lưu: giữ video …";
  `templates/pipeline.html:33` "giữ video thô N ngày" → "giữ video N ngày"; `templates/_vlog_card.html:9`
  "video thô đã hết hạn" → "video đã hết hạn"; `templates/settings.html:44` the muted line now reads
  "Video (MP4) và âm thanh cũ hơn số ngày này bị xoá lúc 03:00; keyframe, transcript, chương, recap và vlog giữ vĩnh viễn."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd brain && uv run pytest --cov=loopi_brain -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brain/src/loopi_brain brain/tests
git commit -m "fix(brain): P3c-1 leftovers — labels with two active cameras, the card title and 404 on rename, the filtered day (P3c-2)"
```

---

### Task 11: Docs — the rulings in the specs, the operator guide

**Files:**
- Modify: `docs/superpowers/specs/2026-10-19-p3c-multicam-design.md`, `docs/superpowers/specs/2026-10-14-loopi-design.md`
  (its D31 row), `deploy/README.md`

- [ ] **Step 1: The P3c design**

- §1 D31 and §3 bullet `led.kind = "none"` → `[led] source = "none"` (R1), in both specs' D31 rows.
- §2 bullet "Agent của camera mạng nói chuyện với brain đúng như Pi: qua Caddy, mTLS, cùng URL brain trong LAN" →
  "… qua Caddy, mTLS, ở `https://proxy:8443` trong mạng compose (agent kiểm CA của brain, không kiểm tên máy) (R2)".
- §2 bullet "Giờ: container dùng đồng hồ của PC …" → "Giờ: container gắn thư mục `/run/systemd/timesync` của PC (chỉ
  đọc); camera chỉ ghi khi đồng hồ PC đã đồng bộ, như Pi (R3)".
- §3: "Camera được thêm với 'không có âm thanh' …" gains "(brain ghi `capture.audio_enabled = false`; giá trị này là
  trần: lệnh bật audio từ xa không mở lại mic, R4)"; add a bullet "Camera có âm thanh dùng hai kết nối (hình, tiếng);
  app chỉ nhận một kết nối thì mất tiếng (`mic_missing`) (R5)"; "`audio_gain_db` mặc định 0 cho `stream`" →
  "brain ghi `audio_gain_db = 0` cho `stream` (R7)".
- §4.1: "Lưu → tạo token ghép cặp → viết `cameras/<id>/loopi.toml`" gains "(token sống 24 giờ; nút 'Ghép cặp lại'
  tạo token mới, R6)"; "Gỡ camera: ghi file `disabled`, thu hồi thiết bị" gains "; file cấu hình được viết lại không
  còn mật khẩu (R9)".
- The "Trạng thái" line gains "; P3c-2 có plan ngày 2026-10-20".

- [ ] **Step 2: The operator guide**

`deploy/README.md`, a new section after "Pause and resume":

```markdown
## Network cameras (P3c-2)

- Thiết bị → "Thêm camera mạng": pick the type (IP camera, phone app, ESP32, other), type the place and the stream
  address (replace `<IP>`), the user name and password, then "Thử kết nối" (ffprobe, at most 10 s) and "Lưu camera".
  The brain writes `<LOOPI_DATA_DIR>/cameras/<id>/loopi.toml` (mode 600, holds the password; never commit or print it)
  and the `cameras` container starts an agent for it within 5 s; it pairs, then records 720p at 2 fps (about 4.4 GB a
  day with sound).
- The camera shows up as a device; pause, sound on/off and the place name work as for the Pi. The header of every page
  says how many cameras record now.
- A camera that has not paired within 24 h (cameras container down): "Ghép cặp lại". "Gỡ camera" stops its agent,
  removes the password from its file and revokes it; what it recorded stays until retention or "Quên".
- Logs: `docker compose -p deploy logs cameras` (each agent line starts with `[<camera id>]`; stream errors are
  named, never quoted, so no address or password appears).
- The container mounts only `<data>/cameras` and the host's `/run/systemd/timesync` (cameras record only while the PC's
  clock is synced).
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs deploy/README.md
git commit -m "docs: P3c-2 rulings in the specs and the network camera guide (P3c-2)"
```

---

### Task 12: Deploy and accept (controller, with the owner)

Done by the controller; the owner's hardware steps are marked **Owner**.

- [ ] **Step 1: Ask the owner, then deploy**

Tell the owner what deploying adds (a `cameras` container, migration 012; nothing is deleted), back up the database
(`pg_dump … > ~/loopi-backups/p3c2-<date>.dump`), fast-forward `feat/phase2` in `/home/soul/loopi-p2` to the branch,
`docker compose -p deploy up -d --build` (this builds the agent for x86_64: several minutes), then
`COMPOSE_PROJECT_NAME=deploy ./smoke.sh`. Check `docker compose -p deploy logs cameras` shows the supervisor idling
with no camera.

- [ ] **Step 2: Owner — the cameras**

**Owner:** on the Imou camera's network, note its IP; on each old iPhone install an app that serves RTSP (or MJPEG),
start it, keep it charging, turn on Guided Access; then on Thiết bị add each camera (type, place, address, user name
`admin` and the safety code for the Imou), press "Thử kết nối", then "Lưu camera". The controller never sees or prints
the safety code.

- [ ] **Step 3: Acceptance (P3c design §8)**

- §8.1: each camera pairs and its first segment is on the timeline, labelled with its place, within 2 minutes.
- §8.3: "Tạm dừng" on one camera: it stops within 15 s (its heartbeat says paused, its uploads stop) and the header
  count drops by one.
- §8.4 (24 h): storage per day ≤ about 22 GB with the cameras the owner added; caption/transcript latency ≤ 15 min
  between 08:00 and 22:00 (Xử lý page); no agent restarting in a loop (`docker compose -p deploy logs cameras` shows
  no repeated "agent exited").
- §8.7 when an Android phone or an ESP32 is added: "Thử kết nối" gives the right numbers, keyframes and captions
  appear, the camera is in search and on the timeline.
- Record every number in the ledger. Items §8.2 (one transcript per sentence) and §8.5 (vlog with two cameras) belong
  to P3c-3.
