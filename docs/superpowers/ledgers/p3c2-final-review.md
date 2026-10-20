# Final whole-branch review — P3c-2 network cameras (85994cf..825b4fa, feat/p3c2)

Reviewer: opus, read-only. I reviewed the diff in four passes: agent (config, stream/stream_audio, video, supervisor,
main/run wiring, tests), deploy (compose, Dockerfile, .dockerignore, Caddyfile interplay, pause/resume scripts), brain
(migration, cameras/probe/capacity, routes, templates, P3c-1 leftovers, tests), and docs (specs, README). I also read
the ledger and re-checked the controller's rulings.

Checks I ran to settle specific doubts (nothing in the checkout was changed):
- I rendered two camera configs with the brain's own `cameras.render_config`: one with a password holding a quote,
  a backslash, an emoji, `ế` and DEL, and one scrubbed (removed) config. I fed both to the real
  `agent/target/debug/loopi-agent run --config …`. Both parsed and validated; the agent went on to pairing, so the
  `_toml` deviation and the key-by-key seam work. The files were fake, in the scratchpad, and are deleted.
- On the host: `systemd-timesyncd` has `RuntimeDirectory=systemd/timesync` with `RuntimeDirectoryPreserve=no`.
  `unattended-upgrades` is enabled (Ubuntu 24.04). There is no `/etc/docker/daemon.json`, so Docker logs never rotate.
- The live data dir is owned by uid 1000, so the api's `cameras_dir.mkdir` at startup will succeed.

### Strengths
- **Secrets are handled carefully at every layer the plan named.** `Secret` hides its value in Debug output, and so
  does `StreamInput`. TOML errors never quote the line, even for type errors on the password line. Pi configs
  serialise none of the new keys, which protects an A/B rollback. `scrub_secrets`/`has_secret` know the stream
  password. The video ffmpeg's stderr is classified and never logged, and the sound ffmpeg's stderr goes to
  `/dev/null` (a test pins this). The brain's `CameraInput.__repr__` hides the password, the password is never in the
  DB or a page, the probe gives only fixed messages, and the form never shows the password again.
- **Privacy lifecycle is right.** `NullLed` is allowed only with `stream`. PR_SET_PDEATHSIG covers both spawning
  paths (tokio on the main thread, std on the sound thread). `init: true` reaps orphans. The R4 ceiling applies both
  at load (`load_state`) and on remote commands, and a whole-agent safety test covers it. Pause still persists and
  fails closed for camera agents (state.json under their own data dir).
- **Remove is ordered and undoable.** Statements run first. Then `disabled` is written, so the agent stops within one
  scan, then the scrubbed config. If the transaction fails, the `disabled` file is unlinked only when this call
  created it. Tokens nobody used are closed, and `register_device` never links a removed camera. Config writes are
  atomic (temp file, fsync, rename, fsync of the directory).
- **The supervisor is simple and correct for its job.** ULID-only directories, stamps (mtime and length), backoff,
  restart on config change, SIGTERM to every agent before the serial waits, and `kill_on_drop` as a backstop.
- **The tests are honest.** A real-ffmpeg MJPEG→H.264 segment test runs with a paced server. Whole-agent stream e2e
  tests validate the contract schema and check that no ffmpeg is left behind. The brain tests cover rollback
  behaviour, unreadable passwords on repair, and probe cancellation.
- **The contract is untouched.** No change to `v1.openapi.yaml`, wire.rs, heartbeat.rs or the api routes. Video fps
  goes up as `X-Loopi-Video-Fps: 2`, inside the contract's 1..30.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)

**I1. The cameras container sits on the default compose network, so it can reach `api:8000` directly and get past
both of Caddy's protections.** Location: `deploy/docker-compose.yml:136-151` (no `networks:`). The assumption it
breaks is written at `deploy/Caddyfile:7-11`.
- *What's wrong:* the api trusts `X-Loopi-Client-Fingerprint` from any caller (`brain/src/loopi_brain/api/device_auth.py`).
  The web UI has no auth of its own; basic auth lives only in Caddy (binding spec §10: "Web UI … được bảo vệ bằng HTTP
  basic auth"). Until now, every container on the default network ran trusted code. The new container is the first
  that parses attacker-controllable bytes straight from the LAN: ffmpeg demuxing RTSP or MJPEG from cheap IP cameras
  and phone apps. Its image is built once and never rebuilt with Debian's ffmpeg security updates.
- *Failure scenario:* a compromised camera (or a hostile device on the LAN posing as one) exploits an ffmpeg demuxer
  bug. The attacker, with uid 1000 in the cameras container, can then:
  - GET `http://api:8000/` with no password: every recording, transcript and recap of the home;
  - POST `/devices/…` or "Quên" routes (only an `HX-Request` header is needed);
  - impersonate any device on `/v1/*` with a forged fingerprint header (the fingerprints are on the unauthenticated
    web page);
  - reach `db:5432` and `models:11434`.

  The plan clearly meant least privilege ("never the pki keys"), but the network was left open.
- *Fix:* give the cameras container its own network that it shares only with `proxy`. For example:
  `networks: { cameras: {} }`, `cameras: networks: [cameras]`, `proxy: networks: [default, cameras]`. Keep it a normal
  bridge, not `internal`, because the agents must reach the LAN. Pin it in `test_deploy.py` (cameras is only on
  `cameras`; api, db, worker and models are not on it). The agents keep using `https://proxy:8443` unchanged (R2).

**I2. The `/run/systemd/timesync` bind mount goes stale the first time systemd-timesyncd restarts. Every network camera
then stops recording, silently, until the cameras container is recreated.** Location: `deploy/docker-compose.yml:144`
together with `agent/src/clock.rs:26-28` (the agent checks `sync_flag.exists()` on every tick, and `run.rs:399-400`
closes the gates).
- *What's wrong:* on this host, timesyncd runs with `RuntimeDirectoryPreserve=no`, so systemd deletes
  `/run/systemd/timesync` when the service stops and creates a new directory (a new inode) when it starts. A Docker
  bind mount pins the old inode. After any restart, the container sees an empty, deleted directory forever:
  `synchronized` never appears again, `clock_synced` stays false, and video and sound gates stay closed. Restarting
  an agent does not help; only a new container mount does.
- *Failure scenario:* `unattended-upgrades` (enabled here) installs a systemd security update. The package's postinst
  restarts timesyncd, and all cameras stop recording. The owner sees "Đang ghi: 0 camera" and cards saying "Đang ghi
  hình: không" with no reason: the card does not show `clock_synced`, and `last_error` stays empty. The same happens
  after a manual `systemctl restart systemd-timesyncd` or `timedatectl set-ntp false/true`. This is the "camera that
  silently never records" failure, and on Ubuntu it is likely within weeks.
- *Fix (cheapest, keeps R3 as ruled):* add a host drop-in at
  `/etc/systemd/system/systemd-timesyncd.service.d/loopi.conf` with `[Service]` and `RuntimeDirectoryPreserve=yes`,
  then `daemon-reload`. This is an owner-approved host step in Task 12 and needs its own README line (a reinstalled PC
  needs it too).
- *Also:* show "đồng hồ chưa đồng bộ" on any device card whose heartbeat has `clock_synced: false`. The brain already
  receives the field, and this makes the failure visible for the Pi as well.
- *Robust long-term fix (changes how R3 works, so ask the owner):* the agent reads the kernel's NTP status with
  `libc::adjtimex` (modes 0 only reads; it works unprivileged in a container; STA_UNSYNC clear means synced). Then no
  mount is needed.

#### Minor (Nice to Have)

**M1. The camera password is in ffmpeg's argv for the camera's whole life, and in ffprobe's for ≤ 10 s. Any host user
can read it with `ps aux`, `docker top` or `/proc/<pid>/cmdline`, not only processes in the container.** Location:
`agent/src/recorder/stream.rs:81`, `brain/src/loopi_brain/camera_probe.py:55`.
- The ledger's Task 7 note ("api container only") understates this, and the agent side is not noted at all. Spec §6
  says the password lives "chỉ trong loopi.toml".
- For RTSP, ffmpeg has no way to take credentials outside the URL, so this is inherent. The fix is to record it as a
  known limitation in spec §6 and the README (single-user PC; consider `hidepid=2` on the host's /proc).

**M2. Docker logs never rotate, and the cameras service adds the chattiest logger.** Location:
`deploy/docker-compose.yml:136-151`.
- A camera with sound turned on whose stream has none logs "microphone unavailable" every ~10 s (about 1.5 MB a day
  per camera, for good). Pairing and stream failures add more.
- The host has no `daemon.json` limit.
- *Fix:* `logging: { driver: local }` (it rotates by default) or json-file `max-size`/`max-file` on `cameras`, and
  ideally on every service. Pin it in `test_deploy.py`.

**M3. The "Điện thoại" type's defaults guarantee a sound error.** Location: `brain/src/loopi_brain/cameras.py:47`.
- Its URL template `http://<IP>:8080/video` is MJPEG, which never carries sound, yet "Ghi âm" is on by default.
- Result: the card permanently shows "mic: mất" and "lỗi gần nhất: microphone: the camera's sound stream ended" for a
  healthy camera. A second connection to the phone is opened and dropped every ~10-15 s.
- *Fix:* pre-fill or hint the IP Webcam sound address (`http://<IP>:8080/audio.wav`) as `audio_url`, or uncheck "Ghi
  âm" when an http(s) video URL has no `audio_url` or when "Thử kết nối" reports no sound.

**M4. The Cài đặt page now says old files "bị xoá lúc 03:00" even while retention is off.** Location:
`brain/src/loopi_brain/web/templates/settings.html:44`.
- The line above it says "đang tắt (chưa xoá gì)", so the page contradicts itself.
- *Fix:* keep "Khi bật," at the start of the sentence, or render the sentence only when retention is on.

**M5. "WAV hoặc PCM 16 kHz mono" over HTTP works only for WAV.** Location: `agent/src/recorder/stream_audio.rs:17-21`,
spec §3.
- Headerless PCM cannot be probed: ffmpeg would need input `-f s16le -ar 16000 -ac 1`. No real-ffmpeg sound test
  exists.
- *Fix:* record in the spec/ledger that the P3c-4 firmware must serve WAV, or add the input format options for a
  raw-PCM `audio_url`.

**M6. A waiting camera says nothing about why it waits or when its token dies.** Location:
`brain/src/loopi_brain/web/templates/devices.html:13-24`.
- After 24 h (or with the cameras container down) it can never pair, and the card looks the same as one minute after
  adding.
- *Fix:* show "token hết hạn lúc HH:MM" or "token đã hết hạn — bấm Ghép cặp lại" (a subquery on `pair_token` in
  `list_cameras`).

### Parked findings you would escalate (from the ledger), if any
- **Task 1 minor 4, credentials in the query string: keep it Minor, but widen the fix to the brain.** The parked text
  covers only the agent's check. The brain also accepts and stores such a URL, then shows it on every card
  (`devices.html:38`, `{{ c.video_url }}`). Cameras whose manuals give URLs like `…/videostream.cgi?user=admin&pwd=…`
  or XMEye's `rtsp://ip/user=admin&password=…&channel=1` would then put the password in the DB and on a page, which
  the plan's Secrets constraint forbids. Cheap fix: `clean_url` refuses `pass=`, `pwd=` and `password=` (any case)
  outside the authority, pointing the owner to the password field, or the card masks the query.
- **Task 4, forwarders stop on a non-UTF-8 line: not escalated, but the consequence is worse than "lines lost".** A
  forwarder that stops reading lets the agent's stdout/stderr pipe fill. The agent's single runtime thread then blocks
  in a log write: no heartbeats, no pause, no collection, while ffmpeg keeps recording. The trigger is practically
  unreachable, because the agent writes only Rust strings. Still worth the `read_until` + lossy fix in the final wave.
- Everything else parked stays Minor as parked. I re-checked the lock-order deadlock (Postgres aborts one side; both
  outcomes are benign), the serial N × 15 s stop (only hung agents add up), `_failure`'s 40x matching (a camera code
  containing 401/403/404 is also possible, but the message is wrong, never leaked), and the young-camera and
  retention-off capacity points.
- **The rulings are sound.** R1, R2, R4, R6, R7, R8 and R9, plus the controller's Task 3, 5, 6, 7, 8, 9 and 11
  rulings, hold. I verified the `_toml` deviation through the real agent binary. R3's intent is sound, but its
  mechanism has the stale-mount flaw (I2). R5 is an accepted limitation (note: an app that pre-empts the older client
  instead of refusing a second one would break video every 10 s; "Tắt audio" on the card is the workaround).

### Declined to judge
- CPU of decoding the Imou main stream (`subtype=0`, possibly 2K H.265) and re-encoding it per camera: the spec picked
  the URL, and §5 says to measure at acceptance.
- No `cpus`/`mem_limit` on the cameras container: the spec is silent, and it is measured at §8.4.
- The capacity line compares a 7-day total with free space, which counts days already on disk twice: the spec's own
  wording (§4.1, D36), and it errs toward warning early.
- English `last_error` words ("the camera refused the user name or password") on a Vietnamese page: a plan decision,
  consistent with the agent's other messages.
- "Đang ghi" can lag up to ~45 s after a pause (15 s heartbeat + 15 s poll), and over-reports while ffmpeg is still
  trying to connect: the heartbeat cadence is protocol v1's, and over-reporting is the safe side of D31.
- Caddy serving the server certificate for SNI `proxy` (R2): Caddy picks a manually loaded certificate by tag when no
  name matches. This is confirmed when the first camera pairs in Task 12, not judgeable from code alone.
- The RTSP path is not exercised against a real RTSP server: deferred to Task 12 acceptance by the plan.
- Images are never rebuilt for security updates: operational and pre-existing for every service (it is part of I1's
  rationale only).
- `/cameras/probe` lets the owner make the api container connect to any URL: behind basic auth, owner-only.
- A removed camera's last queued items never upload, and its directory and queue stay (R9, ruled).
- A camera device revoked through the hidden `/devices/{id}/revoke` route keeps recording locally: parked as out of
  scope, and the UI hides the button.
- Switching camera type resets the typed fields, and a refused form must have its password typed again: a UX choice
  that never echoes the password.

### Recommendations
1. Before deploying: fix I1 (one compose network plus a test), and do I2's host drop-in as an owner-approved Task 12
   step plus a README line. Add the `clock_synced` hint on the card in the same small change.
2. Folding M2 (log rotation) and M4 (one word) into the same commit costs almost nothing.
3. Task 12 extras: check the first pairing over SNI `proxy` (R2), and confirm that `docker compose -p deploy exec
   cameras ls /run/systemd/timesync` shows `synchronized`. After the drop-in, confirm that a
   `systemctl restart systemd-timesyncd` leaves the cameras recording.
4. Record M1 and M5 as documented limitations (spec §6, §3, README) rather than code changes.
5. Final fix wave: the parked items the ledger already lists, plus M3 and M6 and the query-string credential refusal.

### Assessment
**Ready to deploy?** With fixes.

**Reasoning:** The branch is careful and well tested where the plan pointed: secrets, privacy lifecycle, remove and
re-pair, and the contract. Two deploy-level problems need fixing first. Any compromised camera stream can reach the
unauthenticated api and every recording (I1), and the first systemd update will silently stop every network camera
(I2). Both are small, local fixes.
