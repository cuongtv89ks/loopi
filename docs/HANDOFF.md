# Loopi — handoff (2026-10-14)

Context memory for whoever continues this project — the owner or a Claude Code session on the new
Linux arm64 machine. Read this, then `CLAUDE.md`, then the plan you are about to execute.

## 1. Where things stand

| Plan | File | State | Commits on `feat/phase1` |
|---|---|---|---|
| Spec v1 | `docs/superpowers/specs/2026-10-14-loopi-design.md` | approved; amended D16 (kit CSI camera + USB OTG mic) and D17 (USB gadget bring-up) | on `main` and `feat/phase1` |
| P1a — protocol + brain | `docs/superpowers/plans/2026-10-14-p1a-protocol-brain-ingest.md` | **complete**: 9/9 tasks + final whole-branch review + fix wave; 133 tests, 98 % coverage; smoke test passes on the real compose stack | `25c89c4..18da73c` |
| P1b — agent v0 (Rust) | `docs/superpowers/plans/2026-10-14-p1b-agent-v0.md` | **complete**: 12/12 tasks, each reviewed, final review + fix wave; 190 lib + 1 e2e + 4 safety tests (≈ 96 % coverage before the wave), release binary 4.1 MB, no aws-lc-rs. One carried fix (§3), final review: §6 | `18da73c..4765370` |
| P1c — Buildroot image + hardware acceptance | `docs/superpowers/plans/2026-10-14-p1c-buildroot-dev-image.md` | **not started** — moved to Linux arm64 | — |

`main` holds only the spec and plans. Nothing has been merged into `main` yet: merge `feat/phase1` once P1c
acceptance passes (or earlier if you prefer — it is your call).

Execution ledgers (every ruling, every deferred finding, pre-flight scans) are committed under
`docs/superpowers/ledgers/`. When resuming a plan with subagent-driven development, copy its ledger to
`.superpowers/sdd/<plan-basename>/progress.md` (that directory is git-ignored scratch).

## 2. Setting up the Linux arm64 machine

```bash
git clone https://github.com/cuongtv89ks/loopi.git && cd loopi && git checkout feat/phase1
sudo apt install -y build-essential pkg-config libopus-dev libasound2-dev ffmpeg git curl
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh     # Rust stable
rustup component add llvm-tools-preview && cargo install --locked cargo-llvm-cov
curl -LsSf https://astral.sh/uv/install.sh | sh                      # uv (Python 3.13 is fetched on demand)
# Docker Engine + compose plugin (docs.docker.com/engine/install/debian or ubuntu), user in the docker group
```

Then verify both sides before touching P1c:

```bash
cd brain && uv sync && uv run pytest --cov=loopi_brain     # expect 133 passed, ≥ 98 %
cd ../agent && cargo test                                  # FIRST build of the Linux-only code — see §3
cargo llvm-cov --ignore-filename-regex 'main.rs'           # expect ≈ 96 %
cargo build --release && cargo tree -i aws-lc-rs           # the tree must find nothing
```

On Linux, testcontainers talks to `/var/run/docker.sock` directly; the macOS-only env file used during
development (`.superpowers/sdd/env/env.sh`: colima socket, Ryuk disabled) is not needed.

## 3. First thing to do on Linux: one carried fix, then compile the Linux-only agent code

**Carried durability fix (ruling B31, do this before any hardware test).** `agent/src/queue.rs` `Queue::recover`
now continues after a directory-entry error, but Rust's `ReadDir` stops listing at the first error, so payloads or
sidecars that were never listed can be deleted as "orphans" (`queue.rs` ~239-245 with ~279-290). Fix: remember
that the listing was incomplete and, in that case, skip every orphan/sidecar deletion and report those files as
left in place; add a test (e.g. a fake directory iterator that errors midway). Found by the final re-review;
the process allowed no second fix wave, so it is carried here.

Everything in the agent was developed and tested on macOS. Code behind `cfg(target_os = "linux")` has
**never been compiled**:

- `agent/src/led.rs` `GpioLed` and `agent/src/button.rs` button thread — gpiocdev 0.7
- `agent/src/recorder/audio.rs` `alsa_source` — alsa 0.9 (incl. `get_rate`/`get_channels` read-back)
- `agent/src/main.rs` sd-notify 0.4 (`Ready`, `Watchdog`, `Status`) and `agent/src/recorder/video.rs` `prctl(PR_SET_PDEATHSIG)`

Expect a few API-level compile errors there; fix them with tests green. `cargo test` on Linux also runs the
selftest unit test against the real default ALSA device (it records one second) — harmless.

## 4. Next: P1c (Buildroot image) on arm64

- The plan builds inside a Debian container with Buildroot's **internal** toolchain, so it works natively on
  arm64 (no x86_64 prebuilt toolchain needed). First build: 1–3 h, ~20–30 GB in the `loopi-buildroot` Docker volume.
- P1c was revised for **USB gadget bring-up** (spec D17): the dev image boots with the OTG port as a USB gadget
  (ACM console + CDC-ECM network, Pi at `10.55.0.1`); `loopi-usbmode host` switches the port to host mode for
  the microphone. No serial cable needed. Task order: 1 base image + USB console · 2 read-only root · 3 agent +
  WiFi + provisioning · 4 SSH · 5 camera + mic · 6 acceptance.
- Carried into P1c from P1a/P1b reviews:
  - **R22**: the brain remuxes raw H.264 with `-framerate <fps>` assuming no VUI timing in the stream. In the P1c
    end-to-end step, check that a remuxed 60 s segment plays ≈ 60 s in the browser. If not, switch the remux to
    the `setts` bitstream filter.
  - The agent's Buildroot package must build with `--locked` (Cargo.lock is committed) and the ring-only TLS stack.
  - systemd unit: `Type=notify`, `WatchdogSec=30` (the agent pings only while its loops make progress, ≤ 20 s),
    `KillMode=control-group`; READY is sent at start on purpose (pairing may wait indefinitely for a token).
  - `loopi-agent provision` must run before `loopi-agent run` (`loopi-provision.service`); `run` fails with
    "not provisioned" otherwise.
- Hardware steps only you can do (the plan pauses for them): flash the SD card, connect the Pi's `USB` port to
  the computer with a data cable, plug/unplug camera and mic, power pulls, the 24 h soak.
- From the P1b final review — check these while building the image:
  - Cross-compile **only** with Buildroot's ARMv6 toolchain. Debian's `gcc-arm-linux-gnueabihf` targets ARMv7,
    and ring's / libopus' C and assembly would die with SIGILL on the Pi Zero.
  - Consider `KillMode=mixed` instead of `control-group`: with control-group systemd SIGTERMs rpicam-vid directly
    and its last segment is not flushed; mixed still kills the whole group at the end. (Deviates from the spec
    text — your call.)
  - Make `loopi-provision.service` a `Wants=` (not `Requires=`) of the agent so a bad boot file cannot block it.
  - Grant the agent access to the gpiochip, `/dev/snd`, `/dev/video*`, `/dev/media*`, `/dev/dma_heap`; pass
    `--verbose 0` to rpicam-vid and set `LIBCAMERA_LOG_LEVELS=*:WARN`.
  - On hardware: `kill -9` the agent must turn the LED off — if the kernel keeps GPIO outputs on release, add an
    `ExecStopPost` that drives the LED low. Measure RSS (< 20 MB), CPU, reactor stalls during sha256/fsync of
    6 MB segments, and upload throughput vs the upload timeout. `brain.url` should match one of the brain's SANs.

## 5. Decisions taken during execution (summary)

56 rulings were made while executing P1a (R1–R26 + setup) and P1b (B1–B28); each line in the ledgers says what
was decided, why, and what it costs if wrong. The ones that change behaviour you might not expect:

**Protocol / brain**
- Media path is `raw/<device_id>/YYYY/MM/DD/` (UTC date); device pause/audio state is read from the last
  heartbeat, not stored in columns (spec §9.4 updated).
- Contract declares every limit the server enforces: item ≤ 64 MiB (`x-loopi-max-item-bytes`), marker ≤ 64 KiB,
  End ≥ Start and End − Start ≤ 3600 s, ≤ 100 commands per heartbeat response and ≤ 100 acks per request,
  `last_error` ≤ 500 chars, extra heartbeat fields rejected. Commands are a `oneOf`: pause/resume (`{}`) and
  `set_config {audio_enabled}`.
- `/v1/pair` also enforces the agent major version (426). PKI certs carry SKI/AKI/KeyUsage (strict TLS clients).
- Uploads are fsynced (file + directory) before the 201; on a commit failure the file is kept, never unlinked
  outside the advisory lock. Video remux runs in the background with timeout/kill, re-queued at startup.
- The API container runs as `LOOPI_UID:LOOPI_GID`; images are pinned to exact versions; Caddy mounts only the
  server cert/key and CA cert; JSON endpoints have a 64 KB body limit; `X-Frame-Options: DENY`.

**Agent**
- Config rejects `video_bitrate_bps × segment_seconds ÷ 8 > 64 MiB`. An open WiFi network is imported on first boot.
- Payloads are fsynced before the sidecar (commit point); corrupt sidecars move the payload to `quarantine/`
  instead of deleting it; only payloads with no sidecar at all are dropped (and counted).
- A corrupt `state.json` fails **closed** (paused). Commands are acked only after reaching the control loop.
- HTTP client: no redirects, https only, full error chains in `last_error`; 408/425/429/3xx/5xx are retried.
- Pairing verifies the CA fingerprint **during the TLS handshake** when `ca_fingerprint` is configured, before
  the one-time token is sent; device certs (clientAuth) cannot impersonate the brain. Secrets are redacted in Debug.
- Every loop treats a dropped watch sender as shutdown (no hot loops). Video and audio segments are clamped to
  ≤ 3600 s. Audio finalisation/fsync runs on a finisher thread so capture never pauses; the ALSA rate is verified.
- The LED write is retried until it succeeds; the disk is checked before recording first starts; the systemd
  watchdog is fed by real loop progress; `selftest` exits non-zero on any failed check.

## 6. Open items

- P1b final whole-branch review ("with fixes": 1 critical, 4 important) was answered by one fix wave (ruling
  B29): LED unavailable → recording disabled; per-entry startup recovery; visible quarantine with retry for
  400/404/405 and a circuit breaker; per-request timeouts (heartbeat 10 s, uploads sized to the file); mTLS
  verifies the brain against the stored CA without a hostname check. Details and the re-review verdict are at
  the end of `docs/superpowers/ledgers/p1b-progress.md`.
- ~40 deferred minor findings across both plans are listed in the ledgers (`minor (deferred)` lines) with the
  final reviewers' triage; none blocks Phase 1 acceptance.
- Incident during P1a: a pre-flight subagent ran `docker container prune -f` on the dev Mac (removed any
  stopped containers there). Every later subagent was forbidden from touching resources it did not create.

## 7. How to resume with Claude Code

```
Read CLAUDE.md and docs/HANDOFF.md. Do the carried durability fix in §3 (TDD), then compile and test the agent
on this Linux machine and fix any Linux-only compile errors. Then execute docs/superpowers/plans/2026-10-14-p1c-buildroot-dev-image.md with
superpowers:subagent-driven-development, stopping only for hardware steps and approvals.
```
