# Loopi

A local-first personal "second brain", inspired by Looki L1. A **Raspberry Pi Zero W** with the kit CSI
camera and a USB microphone records low-fps video and audio all day and ships it to the **brain**, a
CPU-only Linux PC on your LAN. The brain stores everything, shows a per-day timeline, and (from Phase 2)
runs captioning, speech-to-text (Vietnamese / Korean / English), daily recaps, search and an MCP server.
No cloud by default; raw media never leaves your network.

```
 Pi Zero W (agent/, os/)                         Linux PC "brain" (brain/, deploy/)
 ┌──────────────────────────┐   HTTPS + mTLS     ┌──────────────────────────────────┐
 │ rpicam-vid  H.264 2 fps  │ ─ PUT /v1/items ─▶ │ Caddy (TLS, client certs)        │
 │ USB mic     Opus 16 kHz  │ ─ POST heartbeat ▶ │ FastAPI ingest + web timeline    │
 │ crash-safe on-disk queue │ ◀─ commands ────── │ Postgres 17 · ffmpeg remux       │
 │ privacy LED + button     │                    │ private CA (pairing)             │
 └──────────────────────────┘                    └──────────────────────────────────┘
```

## Status (2026-10-14)

| Part | Plan | State |
|---|---|---|
| Device↔brain contract `docs/protocol/v1.openapi.yaml` | P1a | done |
| Brain: pairing CA, ingest, remux, heartbeat, Vietnamese web UI, Docker/Caddy deploy | P1a | done, reviewed, 133 tests, 98 % coverage, smoke test passes |
| Agent `loopi-agent` (Rust) | P1b | done, 190 lib + 5 integration tests — **Linux-only code not yet compiled; one carried durability fix** (see `docs/HANDOFF.md` §3) |
| Buildroot OS image + Phase 1 acceptance on hardware | P1c | not started — to be built on Linux arm64 |

All Phase 1 work is on branch `feat/phase1`. Start with **[docs/HANDOFF.md](docs/HANDOFF.md)**.

## Repository layout

```
docs/superpowers/specs/2026-10-14-loopi-design.md   system design (Vietnamese) — the binding spec
docs/superpowers/plans/                             P1a / P1b / P1c implementation plans
docs/superpowers/ledgers/                           execution ledgers: every ruling + deferred finding
docs/protocol/v1.openapi.yaml                       device↔brain contract (single source of truth)
docs/HANDOFF.md                                     current state, decisions, how to continue
brain/                                              Python 3.13 FastAPI service (uv)
deploy/                                             docker compose: Postgres + api + Caddy mTLS, smoke test
agent/                                              Rust device agent (run | provision | selftest)
os/                                                 Buildroot external tree (P1c, not created yet)
```

## Brain

Requirements: Docker with Compose v2, `uv`, `ffmpeg` (for tests).

```bash
cd brain
uv sync                      # Python 3.13 is pinned in .python-version
uv run pytest --cov=loopi_brain   # needs Docker for testcontainers (pgvector/pgvector:pg17)
```

Deploy on the brain PC: follow [deploy/README.md](deploy/README.md) (`.env`, `init-pki`, `docker compose up -d --build`,
`./smoke.sh`, then pair a device from the web UI → "Thiết bị" → "Tạo token ghép cặp").

## Agent

Host prerequisites — Linux: `sudo apt install build-essential pkg-config libopus-dev libasound2-dev`;
macOS: `brew install opus pkg-config`. Rust stable ≥ 1.80 (`rustup`), `cargo install cargo-llvm-cov` for coverage.

```bash
cd agent
cargo test                                         # lib + e2e (uses tests/fixtures/fake-rpicam-vid.sh)
cargo llvm-cov --ignore-filename-regex 'main.rs'   # must stay ≥ 80 %
cargo build --release
cargo tree -i aws-lc-rs                            # must find nothing: rustls uses the ring provider only
```

The device binary is cross-compiled by Buildroot for `arm-unknown-linux-gnueabihf` (ARMv6) in plan P1c.

## Privacy invariants

- The red LED is **solid whenever any recorder runs**; no setting can turn it off.
- Recorders die with the agent (`PR_SET_PDEATHSIG` + systemd `KillMode=control-group`).
- Pause (button long-press or web) is persisted and survives reboot; a corrupt state file fails closed (paused).
- Audio recording can be disabled per device; raw media stays on the LAN.
