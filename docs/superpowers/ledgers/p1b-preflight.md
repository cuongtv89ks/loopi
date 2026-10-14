# P1b agent v0 preflight scan

Plan: docs/superpowers/plans/2026-10-14-p1b-agent-v0.md (5460 lines). Line numbers below refer to the plan.

Method: every code block was extracted into a scratch crate (`scratch/agent`, final state) and into 12 per-task snapshots (`scratch/inc/t1..t12`, built one at a time in a single dir). Host: macOS, cargo 1.99.0, rustc 1.99.0, libopus 1.6.1.

Results:
- Final state: `cargo test` passes (77 lib tests, 1 e2e). `cargo build --release` passes (4.07 MB binary).
- Per-task state: `cargo test --lib` passes at the end of every task. Lib test counts per task: 13, 22, 26, 31, 43, 48, 54, 61, 67, 74, 74, 77. The e2e passes from Task 11.
- Flake checks: lib suite 0/40 failures, 0/30 under 10 busy loops. e2e 0/8, 0/8 under load.
- Coverage on host: 92.4 % lines, 91.4 % regions (`cargo llvm-cov --ignore-filename-regex main.rs`).
- Clippy: 1 warning (`bool_assert_comparison`, line 1445) and 1 dead_code warning (`WATCHDOG_PING` on non-Linux, line 5265).
- Linux-only code (alsa, gpiocdev, sd-notify, pre_exec) cannot be compiled on this host. Its API usage was checked against the crate sources in ~/.cargo/registry (alsa 0.9.1, gpiocdev 0.7.3, sd-notify 0.4.5).

## A. Task-pair rows (shared file or interface)

| # | Tasks | Producer vs consumer (exact names) | Finding |
|---|---|---|---|
| A1 | 1,2 | `fsutil::write_atomic(&Path,&[u8],u32)` vs `queue.rs` calls `write_atomic(&self.meta_path(..), &json, FILE_MODE)` | OK |
| A2 | 1,3 | `write_atomic` vs `control::save_state` (`write_atomic(path,&json,0o600)`) | OK |
| A3 | 1,6 | `write_atomic`, `config::{Config, normalize_fingerprint}` vs `pairing.rs` | OK |
| A4 | 1,6 | `Config`/`BrainConfig`/`CaptureConfig`/... struct literals in pairing tests vs `config.rs` field lists (all 6 fields) | OK |
| A5 | 1,9 | `CaptureConfig {video_width, video_height, video_fps, video_bitrate_bps, segment_seconds, camera_cmd}` vs `VideoOptions::from_config` | OK |
| A6 | 1,11 | `ScheduleConfig`, `Schedule::from_config(&..)->Result<_,String>`, `PathsConfig.{data_dir,sync_flag}` vs `run.rs` (`map_err(anyhow::Error::msg)`) | OK |
| A7 | 1,12 | `import_boot_config`, `Config::load`, `WPA_CONF_FILE` vs `main.rs provision` | OK. The wiring is wrong, see D2 (nothing calls import at boot). |
| A8 | 1,7 | Cargo.toml shared: Task 7 adds a dev-dep `tokio` with `test-util`, `rt`, `macros` on top of the normal `tokio` | OK. Cargo unifies the features; compiled. |
| A9 | 2,3 | `testing.rs` shared: Task 2 `FixedDisk`/`CountingDisk` (uses `crate::disk::DiskUsage`), Task 3 appends `use chrono..`, `ManualClock`, `SyncedClock` | OK. Imports are not duplicated. |
| A10 | 2,4 | `testing.rs`: Task 4 appends `use std::sync::Arc;` and `RecordingLed` (uses the `Mutex`/`io` already imported by Task 2) | OK |
| A11 | 2,7 | `testing.rs`: Task 7 appends `FakeTransport` (`use std::path::Path` next to Task 2's `PathBuf`) | OK |
| A12 | 2,10 | `testing.rs`: Task 10 appends `ToneSource` (uses `ManualClock` from Task 3, `Arc` from Task 4) | OK |
| A13 | 2,7 | `Queue::{oldest, remove, quarantine, is_empty, len}`, `enqueue_bytes(b,kind,start,end)` vs `uploader.rs` and its tests | OK |
| A14 | 2,8 | `Queue::len()->io::Result<usize>` vs `heartbeat_once` (`unwrap_or(0) as u64`) | OK on types. The swallowed error is D6. |
| A15 | 2,9 | `Queue::enqueue_file(src,kind,start,end,Some(fps))->io::Result<ItemMeta>` vs `SegmentCollector::collect` | OK |
| A16 | 2,10 | `enqueue_file(&part, ItemKind::Audio, started, ended, None)`: `.part` is renamed to `<id>.opus`, same filesystem under `queue/incoming` | OK |
| A17 | 2,11 | `Queue::open->(Queue,u32)`, `disk::{enforce, DiskUsage, DiskReport}`, `FixedDisk` vs `run.rs`/e2e | OK |
| A18 | 2,12 | `disk::{DiskUsage, StatvfsUsage}` vs `selftest.rs` | OK. The hard-coded 85.0 at line 5232 duplicates `DROP_ABOVE_PCT` (D9). |
| A19 | 3,4 | `AgentStatus` and `ControlState` fields vs `led_pattern` | OK |
| A20 | 3,8 | `AgentStatus.dropped_since_heartbeat: u32`, `ControlState` vs `build_request` and the drop subtraction | OK |
| A21 | 3,8 | `control::{Command, parse_remote_command(&str,&Value)->Result<Command,String>}` vs `translate` | OK |
| A22 | 3,9 | `Clock::now`, `StatusTx::send_modify`, `status.camera_failures`/`last_error`/`recording_video` vs `video.rs` | OK |
| A23 | 3,10 | `Clock`, `ManualClock::advance(chrono::Duration)`, `status.{recording_audio, mic_missing}` vs `audio.rs` | OK |
| A24 | 3,11 | `recording_gates(&ControlState,&AgentStatus)->Gates`, `load_state`, `save_state`, `Command::TogglePause` vs `run.rs` | OK |
| A25 | 3,12 | `SystemClock::new(PathBuf)` vs `main.rs` | OK |
| A26 | 4,9 | `led::{led_pattern, LedPattern, CAMERA_FAILURE_LIMIT}` in the video test (`FastBlink` after 5 failures) | OK. Passes; see E7 for a theoretical race. |
| A27 | 4,11 | `led::{led_pattern, level_at, LedDriver}`, `RecordingLed.states`, `button::Press` vs `led_loop`/`handle_press`/e2e | OK |
| A28 | 4,12 | `GpioLed::open(&str,u32)`, `NoopLed`, `blink_while(&mut dyn LedDriver, fut)`, `spawn_button_thread(String,u32,mpsc::Sender<Press>)->anyhow::Result<()>` vs `main.rs` | OK. gpiocdev API checked in the registry source. |
| A29 | 5,6 | `transport::pair(&Client,&str,&PairRequest)->Result<PairResponse,TransportError>`, `wire::{HwInfo,PairRequest,PairResponse}`, `tls::tests::test_pki` (pub(crate)) | OK |
| A30 | 5,7 | `Transport::put_item(&ItemMeta,&Path)`, `PutOutcome`, `TransportError` vs `upload_once` and `FakeTransport` | OK |
| A31 | 5,8 | `Transport::heartbeat(&HeartbeatRequest)->Vec<RemoteCommand>`, `wire::contract::assert_matches` (pub(crate) under cfg(test)), `wire::tests::sample_heartbeat` | OK |
| A32 | 5,11 | `HttpTransport::new(Client,&str)` vs e2e (`reqwest::Client::new()` over http) | OK |
| A33 | 5,12 | `tls::{pairing_config, mtls_config}`, `build_client(ClientConfig)` vs `main.rs` | OK. In the Task 12 `agent_main`, `let tls = tls::mtls_config(..)` shadows module `tls` after its last use; it compiles. |
| A34 | 6,12 | `ensure_paired(&Config,&Path,HwInfo,&Client)`, `Identity`, `PairError::{NoToken,CaMismatch{..},BadCa}` vs `pair_until_done` | OK. 401 handling is D10. |
| A35 | 7,11 | `run_uploader(Arc<Queue>,Arc<dyn Transport>,StatusTx,watch::Receiver<bool>,Duration)` | OK |
| A36 | 8,11 | `HeartbeatDeps {transport,queue,status,control,commands,probe,started: std Instant,interval}`, `run_heartbeat(deps, shutdown)` | OK |
| A37 | 8,12 | `sysinfo::{hw_info, LinuxProbe, NoProbe}`, `SysProbe` | OK |
| A38 | 9,11 | `video::{VideoOptions::from_config, recover_incoming(&Path,&Queue,Duration,u32), run_video(..)}` | OK |
| A39 | 9,10 | `recorder/mod.rs` shared: Task 9 creates it with `pub mod video;`, Task 10 replaces it with 3 mods | OK |
| A40 | 10,11 | `audio::{AudioOptions, AudioSourceFactory, recover_incoming(&Path,&Queue,Duration), run_audio_blocking(..)}` | OK |
| A41 | 10,12 | `alsa_source::factory(String)` (Linux), `ogg_opus::FRAME_SAMPLES` vs `selftest::mic_check` and `main::audio_factory` | OK. The non-Linux stubs are present. |
| A42 | 11,12 | `run::{run, Deps, Timings}` field list: `Deps` literal in `main.rs` has all 9 fields (`config` moved last) | OK |
| A43 | all | `lib.rs` module list: the final list is config, fsutil, schedule, disk, item, queue, testing, clock, control, status, button, led, tls, transport, wire, pairing, uploader, heartbeat, sysinfo, recorder, run, selftest | OK |

## B. Per-task self-consistency

| # | Task | Finding |
|---|---|---|
| B1 | 1 | Tests, code and imports compile and pass (13 tests). **Not OK**: Step 8 expected output (lines 725-726) is wrong, see D1. |
| B2 | 2 | OK. 22 tests pass. |
| B3 | 3 | OK. 26 tests pass. |
| B4 | 4 | **Not OK**: Step 2 command `cargo test --lib led button` (line 1726) is invalid, see D1b. The code is fine and 31 tests pass. |
| B5 | 5 | OK. 43 tests pass. `cargo tree -d -i rustls` prints a `nothing to print` warning (prints nothing else); there is exactly one rustls. |
| B6 | 6 | OK. 48 tests pass. |
| B7 | 7 | OK. 54 tests pass. Needs the dev-dep `tokio` `test-util` from Step 1. |
| B8 | 8 | **Not OK**: Step 2 command `cargo test --lib sysinfo heartbeat` (line 3222) is invalid. Otherwise 61 tests pass. |
| B9 | 9 | OK. 67 tests pass. The fixture is `chmod +x`-ed at Step 1. |
| B10 | 10 | OK. 74 tests pass. The `thiserror` attribute `{FRAME_SAMPLES}` const interpolation compiles. |
| B11 | 11 | OK. 74 lib tests and the e2e (about 3.6 s) pass. |
| B12 | 12 | OK. 77 lib tests pass. The smoke commands behave as stated (`Imported`, secrets scrubbed, `unknown command "bogus"`, exit=1). The release binary is 4.07 MB, not "5-10 MB" (line 5436, informational). |

## C. Cross-plan interop (agent vs brain/YAML)

| # | Item | Finding |
|---|---|---|
| C1 | PUT `/v1/items/{id}`, method and path (`format!("{}/v1/items/{}")`, base trimmed) vs YAML/`device_routes.put_item` | OK |
| C2 | Headers `X-Loopi-Kind/Start/End/Sha256/Agent-Version`; Content-Type `video/h264`, `audio/ogg`, `application/json`; the brain checks content-type per kind (`items.parse_header`) | OK |
| C3 | Timestamps are `to_rfc3339_opts(Millis, true)` → `2026-10-14T01:00:00.000Z`; the brain uses `datetime.fromisoformat` (Python >= 3.11, `dt.UTC` is already used) and requires a tz | OK |
| C4 | sha256 is `hex::encode` lowercase 64 chars; the brain regex `[0-9a-f]{64}` | OK |
| C5 | `X-Loopi-Video-Fps` is sent only for video with `Some(fps)`, integer 1..30. The config validation (1..=30) matches. The brain defaults to 2 if absent. | OK |
| C6 | Segment length: the brain rejects `end < start` and > 1 h with 422 forever. The agent guards the clock jump with `segment_start`. First segment start is `clock.now()` at spawn. Marker has start == end. | OK |
| C7 | Status mapping 201/200/409/401/426/507/422/413 vs `map_put_status` | OK. Caveat: the `400..=499` catch-all quarantines retryable 408/425/429 (D8). |
| C8 | Marker body `{}`, a JSON object under 64 KiB with no NUL | OK |
| C9 | Max item size: the brain limit is 64 MiB (`max_item_bytes`) and Caddy 80 MB. A 60 s segment at 800 kbps is about 6 MB. | OK |
| C10 | Heartbeat JSON field names/types vs `Heartbeat` (pydantic `extra=forbid`): all 16 fields present; `uptime_s`/`queue_depth` u64, `camera_failures`/`dropped_items` u32 are strict ints >= 0; bools are bools; `Option` → `null` is accepted by `float|None`/`StrictInt|None`/`str|None` | OK |
| C11 | `last_error` truncated to 500 **chars** (`chars().take(500)`), the brain's `max_length` also counts chars; `disk_used_pct` clamped 0..100; `agent_version` "0.1.0" ≤ 32 | OK |
| C12 | `acked_command_ids`: required, ULID pattern. The brain generates ULIDs. **Not OK**: no 100-id cap (D5). | See D5 |
| C13 | HeartbeatResponse `{commands:[{id,type,payload}]}`; `RemoteCommand.payload` has `serde(default)`. `type` ∈ pause/resume/set_config; `set_config` keys: the agent accepts only `audio_enabled` (the brain's `SET_CONFIG_KEYS` should be a subset; unknown ones are acked and reported) | OK |
| C14 | The brain returns **all** unacked commands on every heartbeat. The agent acks on the next request, so a command can be applied twice only if an ack is lost. The commands are idempotent. | OK |
| C15 | `dropped_items` is added by the brain on every heartbeat (`dropped_total += n`). The agent subtracts `reported` only after a successful response, so a lost response double-counts; negligible. | OK |
| C16 | Pair request `{token,csr_pem,hw_info{model,serial,agent_version}}`, no extra fields; token ≤ 32; `model/serial` clipped to 100; `csr_pem` ≤ 8192 (a P-256 CSR is about 400 B) | OK |
| C17 | Pair response `{device_id,cert_pem,ca_cert_pem,ca_fingerprint}` and status 201 (`StatusCode::CREATED`) | OK |
| C18 | The CSR from rcgen (P-256, CN=loopi-device) satisfies `pki.sign_csr` (valid signature, `SECP256R1`; subject ignored, CN set to the device id). The key is PKCS#8 PEM, accepted by rustls. | OK |
| C19 | `ca_fingerprint`: the brain uses sha256 of the DER, lowercase hex. The agent's `sha256_of_first_cert` also uses sha256 of the first cert's DER, lowercase hex, and `normalize_fingerprint` accepts colons/uppercase for the configured value. | OK |
| C20 | mTLS: the server cert chain is leaf + CA (`init_pki`), EKU serverAuth, SAN from `--host/--ip`. The agent trusts only the stored CA; TLS 1.3 only (Caddy supports it); ChaCha first (Go picks ChaCha when the client prefers it). `brain.url` host must be in the SAN, which is a deploy concern. | OK |
| C21 | The client cert (CN=device_id, EKU clientAuth) is verified by Caddy `verify_if_given`; the fingerprint is forwarded and the brain lowercases it. `/v1/pair` is called without a client cert (`with_no_client_auth`). | OK |
| C22 | HTTP/1.1 only: reqwest has no `http2` feature, a streamed body plus an explicit Content-Length. Caddy `reverse_proxy` fine. | OK |
| C23 | `X-Loopi-Agent-Version: 0.1.0` vs `supported_agent_major = 0`; the 426 path exists on pair/put/heartbeat. The agent leaves the item queued on 426 for put (`Blocked`). | OK |
| C24 | The contract tests validate only PairRequest, Heartbeat and HeartbeatResponse against the YAML. The PUT headers/PairResponse are not schema-checked (only wiremock). Spec §13 says "test client by schema". | OK (coverage note only) |

## D. Findings: contradictions with Global Constraints/spec, command/doc errors, defects

| # | Plan lines | Finding | Severity |
|---|---|---|---|
| D1 | 725-726, 2483-2484, 5434-5436 | `cargo tree -i aws-lc-rs` / `cargo tree -d -i rustls`: on cargo 1.99.0 the "not present" result is `warning: nothing to print.` (exit 0), not `error: package ID specification ... did not match`. An implementer may think the check failed. aws-lc-rs is in fact absent: ring only, verified. | Low |
| D1b | 1726, 3222 | `cargo test --lib led button` and `cargo test --lib sysinfo heartbeat` fail: `error: unexpected argument 'button' found` (cargo takes one TESTNAME). | Medium (the red steps would error for the wrong reason) |
| D2 | 5267-5281, 5391-5392 | Spec §6.6: "on first boot the agent copies the file to /data/config". Nothing in the plan calls `import_boot_config` on boot. `run` just fails at `Config::load("/data/config/loopi.toml")` if the file is absent; only the manual `provision` subcommand imports. | Medium |
| D3 | 4900-4909 | Spec §7.2/§12 says >95 % must send a `disk_full` event, and each drop an event. The plan sets only `disk_stopped` (LED) and `dropped_items`. Nothing reaches the brain for `disk_full`: the protocol has no event field and `last_error` is not set. | Low-Medium |
| D4 | 3912-3914 | Privacy invariant ("LED Solid whenever any recorder runs"): `recording_video = true` is set after `spawn_camera`, whereas audio raises its flag before opening the mic (4535). There is a window with the camera process running and the flag false. | Low-Medium |
| D5 | 3386-3397 (+ brain `devices.py` pending query) | The brain has no cap on pending commands and returns all of them. The agent acks every one in the next request. >100 ids → the brain returns 422 on every heartbeat, and the failed request's acks are retained, so heartbeats never recover. | Low |
| D6 | 3389 | `deps.queue.len().unwrap_or(0)` swallows a queue I/O error and reports depth 0 to the brain. | Low |
| D7 | 3399-3401 | When several commands are invalid, only `errors.last()` is reported; the other errors are dropped. | Low |
| D8 | 2410 | `400..=499 => Rejected` quarantines retryable statuses (408, 425, 429, e.g. from Caddy/rate limiting). Data is moved out of the queue permanently. | Low |
| D9 | 3813-3822 vs 3838-3846; 630-641 vs 639-641; 5232 | Near-verbatim duplication: the empty-file/enqueue loop in `collect` and `recover_incoming`; `is_hex64` re-implements the hex check inside `normalize_fingerprint`; `selftest` hard-codes `85.0` instead of `DROP_ABOVE_PCT`. | Low |
| D10 | 5375-5383 | `pair_until_done` treats a 401 (token used/expired) as transient and retries every 30 s forever. It should use the 120 s "needs a fix" branch like `NoToken`. | Low |
| D11 | 1132, 1578, 1801-1806, 5395 | Swallowed or unwrapped fallible calls in non-test code: `metas()` silently skips unreadable sidecars (`if let Ok`); `load_state` falls back silently on non-NotFound read errors; `blink_while` ignores `led.set` errors without logging (unlike `led_loop`); `.expect("SIGTERM handler")` on signal registration in `agent_main`. | Low |
| D12 | 3820, 2429-2441 | Hashing a segment (`sha256_file`) and `read_dir` scans run synchronously on the current_thread runtime. On ARMv6 this can stall heartbeat/LED ticks for hundreds of ms per segment. | Info |

No test in the plan asserts nothing, and none uses `unwrap` on fallible I/O outside `#[cfg(test)]`/`testing.rs`.

## E. Environment (macOS host)

| # | Check | Result |
|---|---|---|
| E1 | `cargo --version` | cargo 1.99.0, rustc 1.99.0. `rust-version = "1.80"` is satisfied. |
| E2 | `pkg-config --modversion opus` | 1.6.1. `opus 0.3.1` builds through `audiopus_sys 0.2.2` against it. |
| E3 | Crates and versions on crates.io | All resolve: reqwest 0.12.28, rustls 0.23.45, rcgen 0.13.2, ogg 0.9.2, opus 0.3.1, alsa 0.9.1, gpiocdev 0.7.3, sd-notify 0.4.5, jsonschema 0.26.2, wiremock 0.6.5, serde_yaml 0.9.34 (deprecated but available), chrono-tz 0.10, thiserror 2. |
| E4 | Linux-only deps cfg-gated | `alsa`, `gpiocdev` and `sd-notify` are in the `target.'cfg(target_os="linux")'` table; all uses are `#[cfg(target_os="linux")]` with non-Linux stubs (`mic_check`, `open_led`, `open_button`, `audio_factory`, `die_with_parent`). macOS `cargo build`/`test` succeed. `WATCHDOG_PING` is an unused const on macOS (warning only). |
| E5 | ring-only TLS | `cargo tree -i aws-lc-rs` finds nothing (`reqwest` `rustls-tls-manual-roots` is on ring). A single rustls 0.23.45. `jsonschema` default features do not pull in aws-lc-rs. |
| E6 | Coverage tooling | `cargo-llvm-cov 0.9.1` works: 92.4 % lines (≥ 80 %). `selftest.rs` 59 %, `sysinfo.rs` 64 %, `tls.rs` 69 % per file, which the total absorbs. |
| E7 | Flakiness | 0/40 and 0/30 (loaded) lib runs; e2e 0/8 and 0/8 loaded. `crashing_camera_backs_off_and_counts_failures` (lines 3661-3665) asserts `!recording_video` at a random instant while the supervisor respawns every 50 ms; it could flake in theory, 0/70 observed. |
| E8 | Host disk | The scratch builds filled the host disk once (ENOSPC) and were cleaned; the scratch dir keeps only sources. This is not a plan issue. |

Row count: 43 pair rows + 12 self rows + 24 interop rows + 12 findings rows + 8 environment rows = 99.
