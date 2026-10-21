# Loopi — handoff (2026-10-21)

Context memory for whoever continues this project, whether that is the owner or a Claude Code session on the brain PC
(Linux). Read this file first, then `CLAUDE.md`, then the plan you are about to execute.

## 1. Where things stand

**Running again since 2026-10-20 13:56 KST** (paused 2026-10-19 → 2026-10-20 at the owner's request). Pause and
resume with `/home/soul/loopi-p2/deploy/pause.sh` and `resume.sh` (`deploy/README.md`, "Pause and resume"). The 1 TB
HDD is not on the bus (it dropped off on 2026-10-18); unplug it if it is still connected.

| Plan | File | State | Branch / commits |
|---|---|---|---|
| Spec v1 | `docs/superpowers/specs/2026-10-14-loopi-design.md` | approved; amended by D16–D23 (see §4) | every branch carries it |
| P1a: protocol + brain ingest | `plans/2026-10-14-p1a-protocol-brain-ingest.md` | **complete** | `feat/phase1`, merged into `main` |
| P1b: agent v0 (Rust) | `plans/2026-10-14-p1b-agent-v0.md` | **complete** | `feat/phase1`, merged into `main` |
| P1c: Buildroot image + hardware acceptance | `plans/2026-10-14-p1c-buildroot-dev-image.md` | **complete, with waivers** (§2) | `feat/phase1` (`main` = 7021705) |
| Audio level (D19) | `plans/2026-10-15-audio-level.md` | **complete**: device +20 dB on `feat/phase1` (image smoke8), brain normalisation in P2a | `feat/phase1` 06059a0 |
| P2a: perception pipeline | `plans/2026-10-15-p2a-perception-pipeline.md` | **complete**; live since 2026-10-15 23:51 KST; measurements in `docs/measurements/2026-10-p2a-pipeline.md` | `feat/phase2` |
| P2b: understanding + UI | `plans/2026-10-16-p2b-understanding-ui.md` | **complete**; live since 2026-10-17 09:00 KST (§3) | `feat/p2b` → `feat/phase2` |
| P2c: evaluation + P2 acceptance | `plans/2026-10-16-p2c-evaluation-acceptance.md` | Tasks 1–10 complete and reviewed; Tasks 11–14 (deploy, the owner labels, runs, report) in progress | `feat/p2c` |
| P3 design (MCP + vlog) | `specs/2026-10-17-p3-mcp-vlog-design.md` | approved 2026-10-17; D24 (MCP pictures off by default), D25 (vlog: active hours only) | `feat/p3` |
| P3a: read-only MCP server | `plans/2026-10-17-p3a-mcp-server.md` | **complete**; live since 2026-10-17 16:47 KST at `/mcp`; Claude Code on the brain PC registered as `loopi` (`--scope user`, token in `~/.config/loopi/mcp-token`), the five tools called OK; pictures off (D24); the api access log no longer records query strings | `feat/p3a` → `feat/phase2` |
| P3b: daily vlog | `plans/2026-10-17-p3b-vlog.md` | **complete**; live since 2026-10-18 07:21 KST; the first run caught up 10-14 … 10-17. Acceptance (§14: a vlog made by itself every evening) is checked at 08:00 on 10-19, 10-20 and 10-21, then the owner watches one | `feat/p3b` → `feat/phase2` |
| Media on their own disk | — (bounded change, owner's yes 2026-10-18) | **on `feat/phase2`, not in use**: `LOOPI_MEDIA_DIR` + a required `.loopi-media` marker; with the marker missing the brain writes and forgets nothing. The 1 TB HDD dropped off the bus during the first copy (§3, Storage); media stay on the SSD | `feat/media-volume` e4861ce, bef6df5 → `feat/phase2` |
| P3c decisions (multi-camera) | `specs/2026-10-18-p3c-multicam-decisions.md` | the owner's first decisions; superseded by the P3c design below | `feat/p3b` 400e4b1 |
| P3c design (multi-camera) | `specs/2026-10-19-p3c-multicam-design.md` | approved 2026-10-19 (D30–D36): any LAN camera can be added in the brain (IP camera, iPhone/Android, ESP32 with camera and a separate audio URL, Loopi ESP32 firmware in P3c-4). Plans P3c-1 … P3c-4 are written **after P4** | `feat/p3c` 88ddc06 (worktree `/home/soul/loopi-p3`) |
| P3c-1: storage and display | `plans/2026-10-19-p3c1-storage-display.md` | **complete, live since 2026-10-20 16:22 KST**: one copy per segment (the raw H.264 goes once the MP4 and the keyframes exist), retention on 7/7 days, an editable location name per device, the merged timeline with a camera filter, places in search/chat/MCP and in the model text; acceptance 6 (stable SSD) is measured on the mornings of 10-21 and 10-22; ledger `ledgers/p3c1-progress.md` | `feat/p3c1` (worktree `/home/soul/loopi-p3c`); live through `feat/phase2` f5fd9a2 |
| P3c-2: network cameras | `plans/2026-10-20-p3c2-network-cameras.md` | **deployed 2026-10-20 22:22 KST, acceptance waits for the owner's cameras**: the agent's `stream` source (ffmpeg pulls RTSP/MJPEG; a separate WAV sound address), `[led] source = "none"`, `[clock] source = "kernel"` (adjtimex, latched once synced), `loopi-cameras` in the `cameras` container (own network with the proxy only, one mount `<data>/cameras`), the Thiết bị camera form (Thử kết nối, Ghép cặp lại, Gỡ camera), the 7-day capacity estimate, "Đang ghi: N camera" on every page, the P3c-1 leftovers M2/M7/M8; ledger `ledgers/p3c2-progress.md`, final review `ledgers/p3c2-final-review.md` | `feat/p3c2` (worktree `/home/soul/loopi-p3c2`); live through `feat/phase2` |
| P3c-3: multi-camera understanding | `plans/2026-10-21-p3c3-multicam-understanding.md` | **deployed 2026-10-21 06:22 KST**: one transcript per sentence across microphones (D33: `detect_speech` → `group_speech` (waits up to 3 min, minute by minute) → `transcribe_audio` on the chosen regions; migration 013 `speech_region`), chapter signatures with place names and a rename that refreshes the days still holding video, the vlog with one camera per 10-minute block, a run per camera and top-left place labels (D35). Acceptance §8.2/§8.5 waits for a second camera with sound; ledger `ledgers/p3c3-progress.md`, final review `ledgers/p3c3-final-review.md` | `feat/p3c3` (worktree `/home/soul/loopi-p3c3`); live through `feat/phase2` |
| P3c-3b: same-sound check | `plans/2026-10-21-p3c3b-same-sound.md` | **deployed 2026-10-21 08:2x KST** (the owner's I3 ruling): migration 014 `speech_region.envelope`, `speech.same_sound` in the decision; ledger `ledgers/p3c3b-progress.md`, final review `ledgers/p3c3b-final-review.md` | `feat/p3c3b` (worktree `/home/soul/loopi-p3c3`); live through `feat/phase2` |
| P4a: prod image + OTA | `specs/2026-10-18-p4a-prod-ota-design.md` (D26–D29), `plans/2026-10-18-p4a-prod-ota.md` | **complete**: all seven device acceptance steps (§13) passed on 2026-10-19 at 383581c; ledger `ledgers/p4a-progress.md` | `feat/p4` (worktree `/home/soul/loopi-p4`); the brain part is live through `feat/phase2` e0045ee |

All plan paths are under `docs/superpowers/`. Execution ledgers are under `docs/superpowers/ledgers/`. They record every
ruling with its cost if wrong, every deferred finding, and the pre-flight scans. The P1c ledger is still only in the
git-ignored `/home/soul/loopi/.superpowers/sdd/2026-10-14-p1c-buildroot-dev-image/`. A sanitized committed copy waits
for the owner's yes.

## 2. Phase 1 (device + ingest): done

- Pi Zero W, Rust agent (rustls with ring only; `aws-lc-rs` must never enter the tree), Buildroot dev image. Image
  **smoke8** (smoke7 plus audio gain +20 dB with a −1 dBFS limiter, D19) is the last single-slot image
  (`/home/soul/loopi/os/out/loopi-dev-smoke8.img`). Since 2026-10-19 the device uses the A/B layout of P4a (see §3, P4a).
- P1c acceptance: 24 h soak on smoke7 PASS (0 restarts, 0 agent warnings). The brain outage test passed. The privacy
  LED is the onboard ACT LED (D18).
- The owner waived the rest of the hardware tests (2026-10-16): SIGKILL/LED watch, 10 power pulls, reboot
  AlreadyImported, `loopi-usbmode` round trip, brcmfmac reload, the stable-DHCP re-check, selftest with the agent
  stopped, and camera unplugged at boot (RF5).
- Parked for a later agent pass (P1c final review, C42–C45):
  - an LED re-assert failure is not retried on the next frame;
  - selftest can darken the LED for ≤ 1 s;
  - the `[led]` path accepts any directory;
  - an unfilled `ca_fingerprint` gives no README pointer.

## 3. Phase 2 on the live stack

**Where it runs.** The live compose project `deploy` (data in `/home/soul/loopi-data`) runs **from the worktree
`/home/soul/loopi-p2/deploy`** on branch `feat/phase2`. It builds there and bind-mounts `Caddyfile` and `providers.yaml`
from there; `deploy/.env` is copied in, mode 600. **Never remove that worktree** (for example with
`finishing-a-development-branch`) before redeploying from another checkout on a branch that contains P2: `docker
compose -p deploy up -d --build`, then `COMPOSE_PROJECT_NAME=deploy ./smoke.sh`. Every compose command passes
`-p deploy`. Never run `down -v`, never prune. Database backups are kept in `~/loopi-backups/`.

**P2a (since 2026-10-15 23:51 KST).** Keyframes, captions (`minicpm-v4.6:1b`), ASR (faster-whisper large-v3-turbo
int8, Silero VAD, brain normalisation ≤ 30 dB with a −40 dBFS noise-floor cap), moments and embeddings
(`qwen3-embedding:0.6b`).
- The backlog of 31 h of recording drained in 4 h 37 min with 0 failed jobs.
- Over a 24 h window it produced 131 keyframes a day, inside the 100–300 budget.
- Live latency from 08:00 to 22:00: video→caption p95 1.0 min, audio→transcript p95 1.7 min.
- CPU was 9.4 CPU-h/day. Peaks: worker 4.5 GB, models 7.95 GB.
- Open finding for P2c: English Whisper hallucinations on room noise.

**P2b (since 2026-10-17 09:00 KST).** This adds:
- chapters (hourly at minute 5), the day recap (23:30) and commitments;
- hybrid search and chat with sources (streams through Caddy, checked in the browser);
- "Quên khoảng thời gian" and reprocess by day;
- retention (D20: 7/7 days; **on by default since D32**, P3c-1);
- the "Hiểu ngày" card on Xử lý.

Deploy notes:
- **Rollback.** First deploy 04:33, then a rollback at 05:33. The first recap loaded `qwen3:8b` (6.7 GB) while
  `qwen3:4b` (3.9 GB) was still in memory from the chapter summaries: 661 MB free, swap 100 %. The worker was
  stopped and both models were unloaded (no OOM kill), then the stack rolled back to P2a.
- **Fix: D23.** Chapter summaries, recaps and chat all use `qwen3:8b`. The owner chose this on 2026-10-17, commit
  6a46d1d. Redeployed at 09:00.
- **Memory.** With `qwen3:8b` and minicpm-v loaded, the models container holds ≈ 9.6 GiB and ≈ 1.5 GB is free while a
  text model runs. To save RAM, the owner's other project (erp-nemo-platform: 4 containers, 5 PM2 apps) was
  **stopped, not removed**. Restore it with
  `docker start nemo-base nemo-whisper nemo-minio nemo-control-db && pm2 start nemo-web nemo-provisioner
  nemo-telegram-bridge nemo-scheduler cf-tunnel-2`. Watch this margin. Further knobs, if needed:
  `OLLAMA_MAX_LOADED_MODELS=1` (text runs unload the vision model) or `LOOPI_LLM_NUM_CTX=4096` (smaller KV cache, and
  the recap prompt budget must shrink, test-first).
- **First results.** 52 chapters (10-14 … 10-17), recaps for 10-14/15/16 (each ≈ 5 min on `qwen3:8b`), 0 failed
  jobs. Latency while the recaps caught up: video→caption p95 2.7 min, audio→transcript p95 2.0 min.
- **Retention.** On since 2026-10-20 (D32, P3c-1); see Storage below.

**P3b (since 2026-10-18 07:21 KST).** The vlog planner runs every 15 minutes and builds yesterday's vlog after 23:30
(script on `qwen3:8b`, then the ffmpeg render). Backup before the deploy: `~/loopi-backups/p3a-2026-10-18-0720.dump`;
rollback commit 22f523c. Ledger: `docs/superpowers/ledgers/p3b-progress.md`.

**Storage (2026-10-18).**
- One Pi camera at 1080p/2 fps writes **≈ 15 GB a day**. Each segment is kept twice: raw H.264 ≈ 7.5 GB and its
  `-c copy` MP4 for playback ≈ 7.3 GB. Audio adds ≈ 240 MB.
- The SSD had 202 GB free (media 58 GB) on 10-18. **P3c-1 settled it (2026-10-20):** one copy per segment deleted
  7 019 raw H.264 files (39 GB; SSD free 143 → 181 GB) and retention is on: from 03:00 on 10-22 each night deletes the
  MP4 and audio of recordings older than 7 days (keyframes, transcripts, captions, chapters, recaps, vlogs stay).
  Audio older than 7 days can no longer be added to the golden set on Đánh giá.
- Four cameras (P3c) at this rate need ≈ 60 GB a day: 7 days of retention is ≈ 420 GB, more than the SSD holds. The
  P3c design must halve this (one copy per segment) or shorten retention.
- **The 1 TB HDD is not usable as is.** On 2026-10-18 the owner formatted it (ext4 `loopi-media`, fstab `nofail`,
  `/mnt/loopi-media`). During the first copy, after ≈ 52 GB of sustained writes at 16:05, it stopped answering: the
  NCQ writes timed out, four link resets failed, the kernel disabled it and ext4 went read-only. The SATA port runs
  `min_power_with_partial` link power management, and the drive is SMR; no SMART report was taken. The owner chose to
  stay on the SSD. The live `deploy/.env` has neither `LOOPI_MEDIA_DIR` nor the marker setting.
- The api and worker images were rebuilt at e4861ce, but their containers still run the d166a2a build. The next
  `up -d` picks the new images up with no change in behaviour (the defaults keep media under `LOOPI_DATA_DIR`).

**P4a: OTA (since 2026-10-19).** The device runs the **dev** image `2026.10.19-383581c` from slot A, both slots good.
Updates are started per device with "Cập nhật" on the Thiết bị page; the Phiên bản page lists the bundles with their
release notes and the device running each one, and deletes old ones (not a bundle a device runs or installs).
- Build in `/home/soul/loopi-p4`: `VARIANT=dev|prod os/build.sh image` (output `os/out/`, ≈ 1 min incremental). Publish
  with `os/scripts/publish-bundle.sh os/out/loopi-<variant>-<version>.json /home/soul/loopi-data/ota` (the brain sees
  it at once). A tree with uncommitted changes builds a `-dirty` version that publish refuses.
- **Signing keys:** `~/.config/loopi/rauc`. Never commit or print them. **Back them up offline**: without them no
  bundle the device accepts can be built, and the only way back is re-flashing the SD card. A copy of the device's
  `/data/keys` and `/data/config` from before the migration is in `~/loopi-backups/device-2026-10-19` (never commit).
- Prod has no SSH (port 22 refused). To debug a prod device, install the dev bundle through OTA (D28).
- The brain holds `loopi-dev-2026.10.19-383581c` and `loopi-prod-2026.10.19-383581c`.
- Measured: bundle ≈ 62 MB; download ≈ 1 min; install 10–30 s; click to "Đã cập nhật" ≈ 3 min; each reboot stops
  recording ≈ 40 s; a broken bundle comes back by itself (agent dead: ≈ 11 min via the guard; kernel panic: ≈ 1.5 min).
  Agent while downloading: RSS ≈ 11 MB, ≈ 35–40 % of the CPU.
- The device's LAN address comes from DHCP and moved once during the acceptance (the host key stayed the same).
- Three bugs only the real device showed were fixed during the acceptance (ledger, Task 12): RAUC 1.15 marks the slot
  primary before the post-install handler copies its boot files; systemd kills `systemctl reboot` together with the
  agent while the reboot it asked for runs; and the stale-agent build trap of a local Buildroot package.

**Rollback P2b → P2a.**

```bash
cd /home/soul/loopi-p2/deploy && docker compose -p deploy stop worker
docker compose -p deploy exec -T db psql -U loopi -d loopi -c "DELETE FROM procrastinate_jobs WHERE task_name IN
  ('plan_chapters', 'build_chapters', 'plan_recaps', 'write_recap', 'apply_retention');"
cd /home/soul/loopi-p2 && git checkout --detach 54e22f3 && cd deploy && docker compose -p deploy up -d --build
```

P2a ignores the P2b tables. To go back to P2b afterwards: `git -C /home/soul/loopi-p2 checkout feat/phase2` and
`up -d --build`.

## 4. Decisions taken after the first spec (D16–D23)

D16: kit CSI camera + USB OTG mic. D17: USB gadget bring-up. D18: the ACT LED is the privacy LED. D19: device gain
+20 dB and brain normalisation. D20: raw audio kept 7 days like video (retention off until the owner enables it; D32
made it on by default in P3c-1).
D21: the golden set stays out of git (`/home/soul/loopi-data/eval` + DB) and the owner labels it on the "Đánh giá"
page. D22: §14's recall@5 is hit@5, and the 30 % rule uses CER for Korean and WER for vi/en. D23: one local text
model (`qwen3:8b`). D21/D22 are on `feat/p2c`; D23 is on `feat/p2b`/`feat/phase2`.

Later: D24–D25 (P3: MCP pictures off by default, vlog of active hours only). D26–D29 (P4a, on `feat/p4`): A/B through
the firmware's tryboot with automatic rollback; an update is started by the owner per device and kept only after the
new system reaches the brain; dev and prod bundles share the signing key and install over each other; the Phiên bản
page with release notes. D30–D36 (P3c design, on `feat/p3c`). D37 (2026-10-19, on `feat/p4`): P4 ends with the prod
image and OTA; WiFi by QR with a captive portal and BLE presence move to the backlog.

## 5. Branches

- `main`: P1, at 7021705.
- `feat/phase1`: P1 + audio level D19 (agent and OS).
- `feat/phase2`: the live branch: P2a + P2b + P2c (Tasks 1–10) + P3a + P3b + the media-volume change.
- `feat/p2b` / `feat/p2c`: the P2b and P2c work branches (worktrees `/home/soul/loopi-p2b`, `/home/soul/loopi-p2c`).
- `feat/p3` / `feat/p3a` / `feat/p3b` / `feat/media-volume`: P3 work in the worktree `/home/soul/loopi-p3`.
- `feat/p3c`: the P3c design (worktree `/home/soul/loopi-p3`).
- `feat/p4`: P4a (worktree `/home/soul/loopi-p4`); `feat/phase2` is at its e0045ee, the rest are agent/OS/docs commits.
- `feat/p3c1`: P3c-1 (worktree `/home/soul/loopi-p3c`), merged into `main` (PR #3).
- `feat/p3c3b`: P3c-3b, the same-sound check (worktree `/home/soul/loopi-p3c3`); merged into `main`
  (fast-forward) and pushed on 2026-10-21; `feat/phase2` = `main`.
- `feat/p3c3`: P3c-3 (worktree `/home/soul/loopi-p3c3`), with main (the P3c-2 sign-in fix) merged in;
  `feat/phase2` was fast-forwarded to it for the deploy; merged into `main` (fast-forward) and pushed on 2026-10-21.
- `feat/p3c2`: P3c-2 (worktree `/home/soul/loopi-p3c2`); `feat/phase2` was fast-forwarded to it for the deploy;
  merged into `main` (fast-forward) and `main` pushed on 2026-10-20.

On 2026-10-19 every branch was merged into `main` (a fast-forward: each branch is an ancestor of `feat/p3c1`) and
`main`, `feat/phase2` and every work branch were pushed. `feat/phase2` (the live worktree's branch) was
fast-forwarded to `main` too; its `brain/` and `deploy/` were unchanged by that, apart from the pause/resume scripts.
From now on, pushing is still outward-facing: ask the owner first.

## 6. Next

1. **P3c-1 acceptance 6** (P3c design §8): on the mornings of 10-21 and 10-22 record `df -h /home/soul/loopi-data`,
   `du -sh` of `media/raw` and `media/view`, the GB added in the last 24 h, and after the 03:00 run of 10-22 (its first
   deletions: recordings of 10-14) that no video or audio file is older than 7 days. It passes when the SSD's free
   space holds steady from one morning to the next.
2. **For the owner (P3c-1):** name the Pi's location on Thiết bị (it is still "loopi-ve1y"); decide whether to revoke
   the old pairings `loopi-57s1`, `loopi-qh07` and `loopi-f9cn` (10-14 and 10-15 hold items of the old pairings, so
   those two Day pages show a camera filter with their names). Still open from P4a: the rpicam demo programs and the
   UART root console in prod, and an offline backup of the signing keys.
3. **P3c-2 acceptance with the owner's cameras** (P3c design §8; plan Task 12):
   - **Owner:** on Thiết bị → "Thêm camera mạng", add the Imou camera (type the IP and the safety code yourself;
     nobody else sees or prints it) and each old iPhone running an RTSP app (charging, Guided Access); press
     "Thử kết nối", then "Lưu camera".
   - Then check: §8.1 the camera pairs and its first segment is on the timeline with its place within 2 minutes (its
     heartbeat says `clock_synced: true`, which proves adjtimex inside the container); §8.3 "Tạm dừng" stops it
     within 15 s and the header count drops; §8.4 over 24 h: storage ≤ about 22 GB a day, caption/transcript
     latency ≤ 15 min between 08:00 and 22:00, no agent restarting in a loop (`docker compose -p deploy logs
     cameras`); §8.7 when an Android phone or an ESP32 is added. Record the numbers in `ledgers/p3c2-progress.md`.
   - **Owner:** the web password was printed once in a Claude Code session on 2026-10-20 (a shell slip, local
     only); to change it, put a new bcrypt hash in `LOOPI_WEB_PASSWORD_HASH` in `deploy/.env` (see `.env.example`),
     update `/home/soul/loopi-data/web-password.txt`, and recreate the proxy
     (`docker compose -p deploy up -d proxy`).
   - Backlog from the P3c-2 reviews (ledger): refuse a login in the URL path (XMEye style), show a waiting
     camera's token expiry, retention-off in the capacity estimate, the supervisor's serial stop waits.
   - The Imou still refuses `admin` with the passwords tried (2026-10-21 02:02, one masked attempt): check the
     device password / ONVIF setting in the Imou Life app, then one "Thử kết nối".
   - P3c-3 is live: when a camera with sound records next to the Pi, check §8.2 (one sentence between two
     microphones gives one transcript; the other region is `covered`) and, on a day with two active cameras, §8.5
     (the vlog shows both, each scene labelled). **I3 (the owner's ruling 2026-10-21) is live as P3c-3b**
     (`plans/2026-10-21-p3c3b-same-sound.md`, deployed 08:2x KST): a region is covered only by the same sound on
     another microphone (loudness envelopes, correlation ≥ 0.7); very short phrases (< ~3 s) can still be taken
     for the same sound — at §8.2 record per-length correlations and each camera's lag from the stored
     envelopes and tune with the owner. Ask the owner whether §8 gets an item for two simultaneous
     conversations (M3). Backlogs: the ledgers `p3c3-progress.md` and `p3c3b-progress.md`, last lines.
   - Then P3c-4 (Loopi ESP32 firmware), when the owner has the board.
4. **P3b acceptance:** the vlogs of 10-19 and 10-20 appear by themselves; the owner watches one.
5. **P2c Tasks 12–14** (P2c is live since 2026-10-17 09:56 KST: migration 005, the Đánh giá page):
   - the owner labels about 22 clips, 30 keyframes and 25 questions;
   - a baseline run, twice, for the noise floor;
   - tuning backed by numbers and the owner's yes;
   - a final run with no overrides;
   - a fresh 24 h CPU window;
   - the acceptance report, numbers only.

## 7. How to resume with Claude Code

```
Read CLAUDE.md and docs/HANDOFF.md. Continue with §6: the P3c-1 acceptance measurements, the P3c-2 and P3c-3
acceptance with the owner's cameras (ledgers `docs/superpowers/ledgers/p3c2-progress.md`, `p3c3-progress.md`),
the owner's I3 decision; then P3c-4.
```
