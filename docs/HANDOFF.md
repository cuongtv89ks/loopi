# Loopi — handoff (2026-10-17)

Context memory for whoever continues this project, whether that is the owner or a Claude Code session on the brain PC
(Linux). Read this file first, then `CLAUDE.md`, then the plan you are about to execute.

## 1. Where things stand

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

All plan paths are under `docs/superpowers/`. Execution ledgers are under `docs/superpowers/ledgers/`. They record every
ruling with its cost if wrong, every deferred finding, and the pre-flight scans. The P1c ledger is still only in the
git-ignored `/home/soul/loopi/.superpowers/sdd/2026-10-14-p1c-buildroot-dev-image/`. A sanitized committed copy waits
for the owner's yes.

## 2. Phase 1 (device + ingest): done

- Pi Zero W, Rust agent (rustls with ring only; `aws-lc-rs` must never enter the tree), Buildroot dev image. The
  device runs image **smoke8**, which is smoke7 plus audio gain +20 dB with a −1 dBFS limiter (D19). The image is at
  `/home/soul/loopi/os/out/loopi-dev-smoke8.img`.
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
- retention, which **ships off** (D20: 7/7 days when enabled);
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
- **Retention.** Nothing is older than 7 days yet. The disk has 228 GB free at ≈ 6.4 GB of video a day, ≈ 35 days of
  room. The owner said "not yet". Ask again once the golden set is labelled, about two weeks after 2026-10-17.

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
+20 dB and brain normalisation. D20: raw audio kept 7 days like video, retention off until the owner enables it.
D21: the golden set stays out of git (`/home/soul/loopi-data/eval` + DB) and the owner labels it on the "Đánh giá"
page. D22: §14's recall@5 is hit@5, and the 30 % rule uses CER for Korean and WER for vi/en. D23: one local text
model (`qwen3:8b`). D21/D22 are on `feat/p2c`; D23 is on `feat/p2b`/`feat/phase2`.

## 5. Branches

- `main`: P1, at 7021705.
- `feat/phase1`: P1 + audio level D19 (agent and OS).
- `feat/phase2`: P2a + P2b, the live branch.
- `feat/p2b` / `feat/p2c`: the P2b and P2c work branches (worktrees `/home/soul/loopi-p2b`, `/home/soul/loopi-p2c`).
- `feat/p3` / `feat/p3a`: P3 (design, P3a MCP; P3b vlog next) in the worktree `/home/soul/loopi-p3`.

All are pushed to GitHub. Pushing is outward-facing, so ask the owner first.

## 6. Next

1. **P3b (vlog):** write its plan from the P3 design §4, then execute (SDD).
2. **P2c Tasks 12–14** (P2c is live since 2026-10-17 09:56 KST: migration 005, the Đánh giá page):
   - the owner labels about 22 clips, 30 keyframes and 25 questions;
   - a baseline run, twice, for the noise floor;
   - tuning backed by numbers and the owner's yes;
   - a final run with no overrides;
   - a fresh 24 h CPU window;
   - the acceptance report, numbers only.
3. **P4.**

## 7. How to resume with Claude Code

```
Read CLAUDE.md and docs/HANDOFF.md, then docs/superpowers/ledgers/p2c-progress.md. Continue P2c at the first task
without a "complete" line (Tasks 11–14 are controller-executed; each needs the owner's yes where the plan says so).
```
