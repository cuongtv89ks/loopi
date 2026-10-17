# P2a pipeline — measurements on real recordings (2026-10)

Plan: `docs/superpowers/plans/2026-10-15-p2a-perception-pipeline.md`, Task 11. Numbers only: no caption or
transcript text is quoted here.

## Setup

| | |
|---|---|
| PC | Intel Core i5-1135G7 (4 cores / 8 threads), 16 GB RAM (15.4 GiB usable), 6 GiB swap, CPU only |
| Brain | `feat/phase2` @ `54e22f3` (P2a + audio level Part B), compose project `deploy` |
| Captions | Ollama 0.35.1, `minicpm-v4.6:1b` |
| Embeddings | Ollama, `qwen3-embedding:0.6b` |
| ASR | faster-whisper 1.2.1 (CTranslate2 4.8.3), `large-v3-turbo` int8, 4 CPU threads, Silero VAD |
| Device | Pi Zero W, images smoke7 until 2026-10-16 08:41, then smoke8 (audio gain +20 dB, D19) |
| Window | 24 h steady state: 2026-10-16 04:28 → 2026-10-17 04:28 KST, starting when the backlog had drained |

## Backlog

Deploying P2a at 2026-10-15 23:51 KST queued every P1 recording: 1 802 video and 1 910 audio items, about 31 h of
recording. They drained by 2026-10-16 04:28, after **4 h 37 min**, with 0 failed jobs and 0 worker restarts. The
smoke7 device kept uploading the whole time.

## Keyframes

| Local day | Video hours | Keyframes |
|---|---|---|
| 2026-10-14 (partial) | 3.5 | 2 |
| 2026-10-15 | 24.0 | 107 |
| 2026-10-16 | 23.9 | 131 |
| 24 h window | 23.7 | 131 |

A full day gives 107–131 keyframes. That is inside the 100–300 budget, at its low end. The defaults were not tuned
(Task 11 Step 7 skipped). Nights in a dark, static room give almost none: 0 keyframes in the first 4.3 h of
2026-10-17.

## Captions

| | Runs | Avg s / keyframe | p95 s |
|---|---|---|---|
| Backlog (shared with ASR) | 109 | 26.6 | 41.1 |
| 24 h window | 131 | 16.7 | 28.8 |

There were 0 caption errors and 0 dead letters. The steady-state average is slightly above the spec's 5–15 s
estimate (§9.6). The 0.61 h of caption run time per day is still inside the "< 1 h" total.

## ASR

| | Audio items | Real-time factor (run s / audio s) |
|---|---|---|
| Backlog | 1 910 | 0.101 |
| 24 h window | 1 426 | 0.067 |

Transcribe runs took 1.60 h of wall time in the window, inside §9.6's 0.5–3 h. There were 0 dead letters.

Segments stored, all time:

| Language | Segments | Minutes |
|---|---|---|
| en | 884 | 53.5 |
| vi | 478 | 59.5 |
| ko | 108 | 7.8 |

Most `en` segments are Whisper hallucinations on normalised room noise. They are short stock phrases with no_speech_prob
0.00, because VAD had already cut the region (see Open issues).

## Latency, 08:00–22:00

Live data only: items uploaded within 5 min of recording, recorded in the 24 h window. Target ≤ 15 min.

| Path | n | Avg min | p95 min |
|---|---|---|---|
| video → caption | 127 | 0.6 | 1.0 |
| audio → transcript | 831 | 0.3 | 1.7 |

The target is met by a wide margin. P2a needs no job priorities for live uploads over the backlog. Items recorded
before the deploy waited in the backlog for hours, so they are not latency samples.

## CPU and memory

cgroup `cpu.stat` `usage_usec` of each container, sampled at both ends of the window. Neither container restarted.

| Container | CPU s in 24 h | CPU-h / day | Job run time in the window |
|---|---|---|---|
| worker (ASR, VAD, normalisation, keyframe decoding) | 25 082 | 6.97 | transcribe 1.60 h, analyze_video 0.19 h |
| models (Ollama: captions, embeddings) | 8 756 | 2.43 | caption 0.61 h, embed 0.04 h |

§9.6 budgets one job at a time ("< 1 h for captions", "0.5–3 h for ASR"), and both are met in run time. In
CPU-seconds the totals are larger, because Whisper uses 4 threads and Ollama uses every core while it runs. That is
9.4 CPU-h/day, about 5 % of the PC's 8 threads × 24 h.

Memory, from cgroup `memory.peak` (it includes page cache). Both peaks were reached during the backlog and did not
grow in the window:

| Container | Peak |
|---|---|
| worker | 4.50 GB |
| models | 7.95 GB (includes page cache from pulling 10 GB of models at deploy; P2a loads no LLM) |

At the end of the window the host had 12.4 GiB available. Swap held 4.6 GiB, 2.3 GiB of it idle pages of the worker's
main process.

## Samples

None in the repository. Captions and transcripts are the family's private data.

## Open issues

- **English hallucinations on room noise** (for P2c): stock phrases on VAD-cut regions. P2c adds switchable filter
  rules, off by default; its golden set decides which rules to turn on.
- **Recovery after a worker kill was not timed.** After a SIGKILL or OOM, the dead worker's jobs wait up to 10 min for
  `retry_stalled_jobs`. That counts against the ≤ 15 min target. P2c acceptance measures it.
- **Worker swap** (for the P2b deploy): 2.3 GiB of the worker's idle pages sit in swap. The P2b memory test stops if
  swap grows by more than 1 GB over this baseline.
- **Caption time** averages 16.7 s against §9.6's 5–15 s. That is acceptable at about 130 keyframes a day. Revisit if
  the budget grows.
