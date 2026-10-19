# SDD ledger — plan: docs/superpowers/plans/2026-10-18-p4a-prod-ota.md

Spec: docs/superpowers/specs/2026-10-18-p4a-prod-ota-design.md (D26–D29), binding spec 2026-10-14-loopi-design.md.
Branch feat/p4 in /home/soul/loopi-p4 = feat/phase2 (b2c9607) + merge of feat/phase1 (30aa8c3); spec 2030b17, 3c6d78b;
plan 0dd0637. Owner: "okie hay tu dong chay het p4a giup toi" (2026-10-18) — run all of P4a, incl. Tasks 11–12
(builds, live brain deploy, device migration and acceptance). Pushing still needs a yes.

## Pre-flight scan (2026-10-18)

| Pair / task | Produces vs consumes | Found |
|---|---|---|
| T1↔T2 | T1 devices.BUNDLE_RE/VERSION_RE/SHA256_RE/MAX_BUNDLE_BYTES → T2 bundles.py; both edit api/device_routes.py | consistent |
| T1↔T3 | T1 Heartbeat.ota model + validate_command('update') → T3 record_report(body.ota.model_dump()), request_update | consistent |
| T2↔T3 | bundles.find/STEM_RE/description_path, helpers.write_bundle, conftest ota_dir → T3 | consistent |
| T2/T3↔T4 | bundles.scan/find, ota.expire_stale/request_update/recent_updates/delete_bundle, ota.BUSY/NOT_FOUND → T4 | consistent; write_bundle(notes_truncated=True) works through **override |
| T1↔T5 | contract OtaReport/Heartbeat fields/UpdateCommand → agent contract tests; variant↔bundle-name rule on both sides | consistent |
| T5↔T6 | Command::Update, AgentStatus.ota, HeartbeatDeps.with_delivered, Deps.os, refuse_update → T6 rewires the command arm | consistent |
| T6↔T7 | BundleFetcher (T6 system.rs) implemented for HttpTransport (T7); run::OtaSystem, OtaTimings | consistent |
| T6↔T10 | pending.json format written by migrate-to-ab.sh; T6 test `reads_what_the_migration_script_writes` pins it | consistent |
| T8↔T9b | loopi-boot install-files required files (zImage, dtb, cmdline.txt, loopi-config.txt) = files T9b post-build stages; system.conf keyring path = T9b install path; guard timer enabled by T9b | consistent |
| T8↔T10 | migrate runs the new rootfs's loopi-boot install-files B | consistent |
| T9a↔T9b | both edit post-build.sh (T9a `$dev` used by T9b), container-build.sh image branch, check-image.sh (T9a variant guards, T9b moves boot checks to a/) | consistent in sequence |
| T9a/T9b↔T10/T11 | os/out names .img/.squashfs (9a), .raucb/.json (9b) → publish, migrate, Task 11 | consistent |
| T1 | tests vs code: variant-mismatch message contains "variant"; patterns equal YAML strings | agrees |
| T2 | reason substrings in tests match BadBundle messages; check order (notes before file) | agrees |
| T3 | tests vs ota.py; fetch_all exists in helpers (test_volume uses it) | agrees |
| T4 | tests vs routes/templates | agrees |
| T5 | `a_delivered_heartbeat_is_counted` and the run-loop test are given as assertions + prose (harness specifics live in the existing tests) | see Ruling P1 |
| T6 | e2e test given as prose; test `a_second_update_while_one_runs` fixed before commit 0dd0637 | see Ruling P1 |
| T7 | tests vs fetch impl | agrees |
| T8 | test harness vs loopi-boot | agrees |
| T9a/T9b | no host-run tests (Buildroot); verified by check-image.sh in Task 11 | see Ruling P2 |
| T10 | publish test vs script | agrees; migrate-to-ab is device-only (bash -n here, run in Task 12) |
| Global | implementer tasks never run os/build.sh/docker compose/smoke.sh; T9b Step 7 runs rauc-keys.sh only against a temp dir | consistent |

Ruling P1: Tasks 5/6 specify some tests as exact assertions plus the harness to copy, not literal code — the existing
heartbeat/e2e harnesses are long and the implementer reads them anyway — costs: a weak test slips through; the task
review checks the tests exist and assert what the plan names.
Ruling P2: OS tasks 9a/9b are verified statically by implementers and for real by check-image.sh in Task 11's builds;
build failures there go back through a fix dispatch on the owning task's files — costs: late discovery of build errors
(hours of Buildroot), bounded by incremental rebuilds.
Ruling P3: RAUC built without its D-Bus service (spec §5 says rauc.service must start after /data mounts): with no
service the ordering concern cannot arise; the agent and the guard call the CLI — costs: if RAUC's CLI needs the
service for install in 1.15, Task 11 enables BR2_PACKAGE_RAUC_DBUS plus a rauc.service drop-in RequiresMountsFor=/data.
Ruling P4: `mountprefix=/run/rauc` added (not in spec §5): the default /mnt/rauc is on the read-only squashfs root —
costs: none expected.
Ruling P5: os/build.sh refuses any image build without the keys (spec §9 says the build refuses to make a bundle): every
image must embed the CA keyring or it can never accept an update — costs: CI or a fresh clone cannot build images
without running rauc-keys.sh first.
Ruling P6: migrate-to-ab.sh moves /data/dev/loopi-agent aside (not in spec §10): a deployed pre-OTA dev agent would run
instead of the rootfs agent on the dev image and never confirm the trial — costs: the owner's deployed dev agent is
renamed to loopi-agent.pre-ab, recoverable.
Ruling P7: publish-bundle.sh uses notes_base only when its commit is an ancestor of the new build (spec §8.1 says the
newest published bundle): notes across unrelated branches would be wrong — costs: such a bundle shows the last 20
device commits instead.
Ruling P8: the broken-kernel acceptance bundle uses root=/dev/mmcblk0p4 (ext4) instead of a missing partition (spec §13
"root= trỏ sai phân vùng"): with `rootwait` a missing partition hangs forever before any watchdog, an existing ext4
one panics at once — costs: none.

## Tasks
Task 1: dispatched (implementer sonnet, BASE 0dd0637)
Task 1: implementer DONE (b6d7695; brain 1089 passed + 1 skipped, 99 %; agent cargo test ok); review dispatched (sonnet)
Ruling: implementer commits carry the implementing model in Co-Authored-By (Sonnet 5.5) instead of the plan's Opus 5.5 line — accurate attribution of who wrote the commit; the Claude-Session line is unchanged — costs: a cosmetic mismatch with the global constraint.
Task 1: minor (deferred): server accepts explicit null for os_version/os_variant/boot_slot/ota/command_id while the contract only allows absent (agent never sends null: skip_serializing_if)
Task 1: minor (deferred): OtaState / os_variant / boot_slot enums not pinned to the YAML by a test
Task 1: minor (deferred): no test for a version that differs from the bundle name (only variant mismatch tested)
Task 1: minor (deferred): pytest.raises(Exception) on the contract side (existing pattern) instead of jsonschema.ValidationError
Task 1: complete (commits 0dd0637..b6d7695, review clean)
Task 2: dispatched (implementer sonnet, BASE b6d7695)
Task 2: implementer DONE_WITH_CONCERNS (ee5477e; 1117 passed + 1 skipped, 99 %). Plan defect fixed by implementer: the parametrized case {"version": ...} hit write_bundle's own version parameter; replaced by a test that rewrites the description's version.
Task 2: ⚠️ resolved — deploy needs no compose change: LOOPI_DATA_DIR is mounted at /srv/loopi, so the default ota_dir /srv/loopi/ota is <data dir>/ota (Task 11 creates it).
Task 2: minor (deferred): "..%2Fpasswd" endpoint case is rejected by routing before find (unit test is the real check)
Task 2: minor (deferred): find() swallows BadBundle/OSError without a log; scan() warns on every page render for an unusable file
Task 2: minor (deferred): TOCTOU between find() stat and FileResponse (500 if the file vanishes); symlinks in ota_dir followed
Task 2: minor (deferred): note commit only length-truncated, not COMMIT_RE-checked (brief-mandated); test/name nits (bundles.Path, no-op mkdir, Z replace)
Task 2: minor (deferred): pyproject allows Starlette < 0.39 (no Range) — lockfile pins 1.8.0
Task 2: complete (commits b6d7695..ee5477e, review clean)
Task 3: dispatched (implementer sonnet, BASE ee5477e)
Task 3: implementer DONE (91c93a9; 1146 passed + 1 skipped, 99 %; test_db.py migration list updated — outside the brief, needed)
Task 3: review — Needs fixes: Important #1 (plan-mandated): expire_stale never runs on the heartbeat path, so an update the brain gave up on is still delivered to a device that returns after > 30 min (Review Focus item 1).
Ruling: amend the plan — the heartbeat route records the device's OTA report first, then calls ota.expire_stale, then record_heartbeat (delivery); add a /v1/heartbeat-level test with an aged, unswept update asserting commands == [] — spec D27 says the brain cancels unsent commands after 30 min, the plan only swept on page loads — costs: one cheap UPDATE per heartbeat.
Task 3: minor (deferred): record_report can hit UniqueViolation (500 on one heartbeat) when reviving races a concurrent request_update; request_update's except UniqueViolation untested (FOR UPDATE serialises)
Task 3: minor (deferred): revive guard revives any finished row on a non-terminal report, not only brain timeouts (reordered duplicate could flip succeeded back)
Task 3: minor (deferred): recent_updates SELECT * leaks the row_number column n
Task 3: minor (deferred): delete_bundle check-then-unlink race with request_update; device-running check skipped for unusable bundles
Note (not a task): the P3b morning check (bh7yw8926, 08:00 10-19) was killed by Claude Code under memory pressure (1.9 GiB available, swap 4.8/6 GiB; models 6.5 GiB, worker 1 GiB, a testcontainers Postgres). Not restarted (harness rule); check the 10-18 vlog by hand instead.
Task 3: fix round 1/5 implementer DONE_WITH_CONCERNS (7ad4a61; covering files 77 + 44 passed; full suite interrupted by host memory pressure — next task's full run covers it); scoped re-review dispatched (sonnet)
Task 3: fix round 1/5 (1 addressed, 0 open; commits 91c93a9..7ad4a61)
Task 3: minor (deferred): expire_stale UPDATE runs on every heartbeat (cheap, partial-index scan); an ack for a command expired in the same heartbeat matches 0 rows (harmless)
Task 3: complete (commits ee5477e..7ad4a61, review clean after 1 fix round)
Task 4: dispatched (implementer sonnet, BASE 7ad4a61)
Task 4: implementer DONE (3287d9f; full suite 1166 passed + 1 skipped, 99 %; test_web_ota 17 passed). NEXT: review-package 7ad4a61..3287d9f and dispatch the Task 4 reviewer (not yet done: usage limit reached).
Task 4: review — Needs fixes: Important #1 (plan-mandated) hx-confirm on the form is inherited by the notes <select hx-get>, so previewing notes asks "Cài bản đã chọn?"; Important #2 (plan-mandated) hx-target="#bundle-<stem>" is not a valid CSS selector for stems with dots (versions), so Xoá never fires.
Ruling: fix #1 with hx-disinherit="hx-confirm" on the update form (+ a test that the select carries hx-get and the form hx-disinherit); fix #2 with hx-target="closest .card" on both delete buttons (+ a test that no hx-target in the page uses a #bundle- id selector) — htmx 2.0.4 resolves hx-confirm through ancestors and has no CSS.escape — costs: none.
Task 4: minor (deferred): revoked-device layout untested; busy-state test weak (no idle/enabled negative); refused delete of an unusable file loses its file name/reason; page not refreshed after an update request; select disabled while busy
Task 4: fix round 1/5 implementer DONE (6f0bf7d; 1169 passed + 1 skipped, 99 %); scoped re-review dispatched
Task 4: fix round 1/5 (2 addressed, 0 open; commits 3287d9f..6f0bf7d)
Task 4: minor (deferred): htmx targets #update-/#notes-/#result-<device id> rely on device ids (ULIDs) having no dots; no browser-level test of htmx behaviour
Task 4: complete (commits 7ad4a61..6f0bf7d, review clean after 1 fix round)
Task 5: dispatched (implementer sonnet, BASE 6f0bf7d)
Task 5: implementer DONE (66e9cbe; 227 lib + 2 e2e + 4 safety, 95.4 % lines; no aws-lc-rs). Carry to Task 6: OtaReport version/command_id must never make a heartbeat fail the contract; status.ota cleared via delivered.
Task 5: minor (deferred → carried into Task 6): no test pins that Command::Update never changes pause/audio (control.rs Update => self)
Task 5: minor (deferred): parse_release / parse_cmdline_slot take the first match (shell takes the last); quoted values dropped — Task 9a writes one unquoted line each
Task 5: minor (deferred): command_id not ULID-checked inside OtaReport (only flows from parse_update / migration null); refuse_update lacks a doc comment
Task 5: complete (commits 6f0bf7d..66e9cbe, review clean)
Ruling: Task 6 also (a) adds a control test that Update leaves ControlState unchanged and an e2e assertion that pause survives an update command, (b) makes build_request drop an OTA report whose version is not a valid OsVersion (log a warning) so a hand-written pending.json can never make every heartbeat 422 — Review Focus item 3 extended to the OTA report — costs: a malformed report is silently absent from the heartbeat (logged).
Task 6: implementer DONE_WITH_CONCERNS (febb126; lib 251 + e2e 4 + safety 4, 95.6 %; fixed a shutdown hang in run_ota while waiting for the trial's heartbeat; corrected run() test; concern: an agent restart between rauc install and the reboot is reported as rolled_back)
Task 6: review (opus) — Needs fixes: Important #1 (plan-mandated) a second update during download/install is queued in the mpsc channel and run later (or silently lost on reboot) instead of ignored with a last_error note; the test passes only because FakeRebooter returns.
Ruling: run_ota exposes a busy flag (Arc<AtomicBool>, true from taking a request until it finishes or the reboot is requested, and during on_boot's Confirm wait); run.rs refuses an update with the last_error note while busy and only then try_sends; test with a fetcher that blocks until released, asserting the second request is never fetched and last_error mentions IN_PROGRESS — design §6.1 — costs: none.
Task 6: minor (deferred): an agent restart between saving Rebooting and the real reboot (≤ 45 s window) is reported rolled_back and marks the good slot bad; cheap guard = boot_id in pending.json (serde default) or a /run marker — needs a ruling on the /data format; safe and recoverable as is
Task 6: minor (deferred): no shutdown check between hashing, rename and rauc install (SIGTERM during hashing still starts the install, killed at TimeoutStopSec)
Task 6: minor (deferred): a failed save of the Rebooting state is only logged and the trial reboot still happens (trial then reads Installing → interrupted, never confirms, guard rolls back)
Task 6: minor (deferred): orphan .part / bundle files not cleaned on rename failure or corrupt pending.json (clear_downloads only on Interrupted); closed channel reported as "in progress"
Task 6: fix round 1/5 implementer DONE (3e9b2f2; lib 254 + e2e 5 + safety 4, 95.7 %); scoped re-review dispatched
Note (P3b acceptance): evening 1/3 OK — vlog 2026-10-18 rendered by itself at 2026-10-19 01:33 KST (90 s, ollama:qwen3:8b, no failed/todo vlog jobs). Remaining: 10-19, 10-20; then the owner watches one.
Task 6: fix round 1/5 (1 addressed, 0 open; commits febb126..3e9b2f2)
Task 6: minor (deferred): busy flag stays claimed if the task exits at shutdown with an unread request (harmless); Full arm leaves the flag claimed (unreachable); a vacuous initial wait in one unit test; the "ignored" note lingers in last_error; an update sent during a failed update's ~30 s delivery window is acked and lost
Task 6: complete (commits 66e9cbe..3e9b2f2, review clean after 1 fix round)
Task 7: dispatched (implementer sonnet, BASE 3e9b2f2)
Task 7: implementer DONE (5a41b18; 263 lib + 9 integration, 95.3 %; release build ok)
Task 7: minor (deferred): mid-body network cut path untested; no flush on early error; Content-Range not validated on 206 (a wrong-offset 206 fails the update via checksum instead of restarting); no early Content-Length check; Timeouts::upload used for downloads; no test for a 206 resume that overruns size
Task 7: complete (commits 3e9b2f2..5a41b18, review clean)
Task 8: dispatched (implementer sonnet, BASE 5a41b18)
Ruling: from 2026-10-19 (account change) commits carry only the Co-Authored-By trailer; the Claude-Session line came from the previous session attribution, not from the owner — global-constraints.md updated — costs: none.
Task 8: implementer DONE (8f48953; test-loopi-boot.sh 46/46 under dash, sh and busybox sh; test's SHELL_UNDER_TEST unquoted to allow "busybox sh")
Task 8: review (opus) — Needs fixes: Important #1 (plan-mandated) during an unconfirmed trial loopi-boot lets RAUC overwrite the fallback slot (set-state <primary> bad / install-files <primary> only refuse the booted slot) — reproduced: an untried image becomes config.txt's slot, or a half-written fallback bricks the device.
Ruling: fix #1 — set-state <S> bad and install-files <S> die when current is non-empty, current != primary and S == primary (empty current stays allowed for the image build and the migration). Also fix, as D26 boot-safety requirements rather than polish: #2 never rename a tryboot.txt.new (or boot-state.new) whose content failed to render — check prefix_of(tryboot.txt.new) == slot dir (and that sed succeeded) before mv; #4 install-files requires overlays/README (firmware loads ${os_prefix}overlays/ only when README exists; cleanup_legacy removes the shared /boot/overlays); #6 the guard service orders After=boot.mount-equivalent only (no Requires=) so a failed /boot mount does not skip the rollback. Task 6 needs no change: with #1 a new update during an unconfirmed trial fails at RAUC's set-state bad and is reported failed; the guard reboots to the good slot — costs: none.
Task 8: minor (deferred): cleanup_legacy failure reported as a failed confirm; set-primary does not check the slot's zImage; no flock against loopi-usbmode; test gaps (confirm with tryboot naming the other slot, missing flag file, no rauc-post-install test, temp dirs not removed); unvalidated $i in rauc-post-install eval; temp dir left in /run on mount failure; commit 8f48953 carries a Claude-Session trailer
Task 8: fix round 1/5 implementer DONE (f1cb4c9; test-loopi-boot 80/80 under dash, sh, busybox sh); carry to Task 9b: stage overlays/README; scoped re-review dispatched (opus)
Task 8: fix round 1/5 (4 addressed, 0 open; commits 8f48953..f1cb4c9)
Task 8: minor (deferred): an existing-but-unreadable config.txt is read as primary = A (prefix_of hides sed's error) — only a FAT I/O error can cause it on the device (root); render-failure test SKIPs as root; set-primary/get-state call $(primary) inside [ ] so a missing config.txt does not fail them; set-primary <primary> during a trial deletes tryboot.txt
Task 8: complete (commits 5a41b18..f1cb4c9, review clean after 1 fix round)
Task 9a: dispatched (implementer sonnet, BASE f1cb4c9)
Task 9a: implementer DONE (d5a700a; static checks; version dry-run 2026.10.19-d5a700a; concerns: prod config.txt still has dwc2 until 9b; tmpfiles /data/dev in common overlay)
Task 9a: review — Needs fixes: I1 (plan gap) etc/tmpfiles.d/loopi.conf (declares /data/dev, /data/dev/ssh, /data/dev/dropbear) stays in the common overlay; I2 (plan-mandated) the prod check "agent wrapper never runs /data/dev" greps etc/loopi-dev again and never looks at run-agent.
Ruling: I1 — git mv the tmpfiles file to rootfs-overlay-dev and add it to the prod "no dev marker…" regex; I2 — replace the vacuous prod check with a common check that pins run-agent's guard line (`if [ -f /etc/loopi-dev ] && [ -x /data/dev/loopi-agent ]; then`), so prod's behaviour follows from the absent marker (D28 makes /data/dev reachable on prod after a dev→prod update) — spec §6.3/§6.5 — costs: none.
Task 9a: minor (deferred): check-image.sh aborts on an unset LOOPI_OS_VERSION late (set -u) and variant detection fails open; build.sh empty "${key_mount[@]}" needs bash ≥ 4.4; container-build VARIANT unset by hand; linux-* glob also matches linux-headers; rpicam-hello/still/jpeg demo apps likely in prod (spec §6.5 lists rpicam-hello as dev-only); prod keeps a UART root console (console=serial0, enable_uart=1, no root password) — spec-level, for the owner; 9b must drop BR2_PACKAGE_RPI_FIRMWARE_CONFIG_FILE from loopi.fragment only if it overrides it per variant
Task 9a: fix round 1/5 implementer DONE (64f2583); scoped re-review dispatched
Task 9a: fix round 1/5 (2 addressed, 0 open; commits d5a700a..64f2583)
Task 9a: minor (deferred): the run-agent guard check pins one exact line and would not catch a second unguarded exec added elsewhere
Task 9a: complete (commits f1cb4c9..64f2583, review clean after 1 fix round)
Task 9b: dispatched (implementer sonnet, BASE 64f2583); carried: overlays/README staging + check
Task 9b: implementer DONE (c187ac1; static + host dry runs of post-build/post-image with stubs; rauc-keys.sh on a temp dir; test-loopi-boot still green)
Task 9b: review (opus) — Needs fixes: Important #1 (plan-mandated) "RAUC CLI only" is not enforced: loopi.fragment never unsets BR2_PACKAGE_RAUC_DBUS; a default-on D-Bus build would ship rauc.service and rauc-mark-good.service (self-confirming trials → no rollback).
Ruling: fix #1 with `# BR2_PACKAGE_RAUC_DBUS is not set` in loopi.fragment plus a check-image guard (no rauc.service, rauc-mark-good.service or de.pengutronix.rauc files in the rootfs); also fix, as OTA-or-reflash requirements: #2 pin CONFIG_SQUASHFS_ZLIB=y and CONFIG_CRYPTO_SHA256=y in linux.fragment and check the built kernel .config for BLK_DEV_LOOP, BLK_DEV_DM, DM_VERITY, SQUASHFS_ZLIB; #5 check the staged usr/share/loopi/boot/cmdline.txt keeps root=@ROOT@ and rauc.slot=@SLOT@ — an image that cannot mount a verity bundle or whose slot B boots the old rootfs can only be fixed by re-flashing — costs: none.
Task 9b: minor (deferred): JSON check weaker than the brain validator; bundle manifest not checked after signing (rauc info output); two negated mtools checks pass when mtools cannot read the image; README overstates that config.txt is only written by rename (loopi-usbmode edits it); confirm in Task 11 that overlays load from a/ (0-byte README)
Task 9b: fix round 1/5 implementer DONE (a74bec4); scoped re-review dispatched
Task 9b: fix round 1/5 (3 addressed, 0 open; commits c187ac1..a74bec4)
Task 9b: minor (deferred): kernel-config check's diagnostic is swallowed by check() and prints a double slash; strict about stale linux-<old> dirs (fails safe)
Task 9b: complete (commits 64f2583..a74bec4, review clean after 1 fix round)
Task 10: dispatched (implementer sonnet, BASE a74bec4)
Task 11 (controller, started early): Step 1 keys created 2026-10-19 with os/scripts/rauc-keys.sh in ~/.config/loopi/rauc (dir 700, files 600; CA CN "Loopi OTA CA", valid to 2046-10-14) — owner to back it up. Prod build started from a clean detached worktree /home/soul/loopi-build at a74bec4 (Task 10 changes only host scripts; the image content is the same), log scratchpad/build-prod-a74bec4.log.
Ruling: build prod early from a separate clean worktree so the 1–3 h toolchain build overlaps Tasks 10–final review; rebuild (incremental) if the final fix wave touches agent/os — costs: one extra incremental prod build.
Task 10: implementer DONE_WITH_CONCERNS (c53b32c; test-publish-bundle 23/23; brain bundles.scan accepted the published descriptions; migrate-to-ab.sh mocked end-to-end with a fake ssh; 5 fixes to the brief's script incl. reboot race via boot_id)
Task 10: review (opus) — Needs fixes: Important #1 (plan-mandated) publish-bundle.sh `cp … && mv …` lists ignore a failed copy under set -e and still print "published"; Important #2 migrate-to-ab.sh leaves /data/dev/loopi-agent set aside when a later step fails (pending write, boot_id read, refused reboot, trial-not-started) while saying "nothing changed".
Ruling: fix both as the reviewer proposes (die on cp/mv failure; read old_boot and write pending.json before the rename, make the rename the last step before the reboot, move the agent back and remove pending.json on any failure after the rename and on the "trial did not start" path, correct messages). Also fix, because the controller runs this script on the owner's only device next: Minor #3 the backup is written to a temp file and renamed (a failed re-run must not empty the previous backup) and refuses a backup dir inside a git work tree, and checks config/ too; Minor #4 refuse a description whose variant is not dev and a version that fails the OsVersion pattern; Minor #5 wait at least the guard's 10-minute window (+2 min) for confirmation and tell the owner to power-cycle if the device never returns — costs: none.
Task 10: minor (deferred): no SSH keepalive (a drop mid-dd hangs until TCP gives up); silent set -e exits without "device unchanged" messages; mv overwrites an existing .pre-ab; the bundle's own commit not checked as 40-hex in publish; D1 filter and non-ancestor fallback untested; no blkid check that p3 is empty
Task 10: fix round 1/5 implementer DONE_WITH_CONCERNS (28b6766; publish 47/47; new test-migrate-to-ab.sh 94/94 with a fake ssh); scoped re-review dispatched (opus)
Task 10: fix round 1/5 (5 addressed, 0 open; commits c53b32c..28b6766)
Task 10: minor (deferred): publish signal window after the description mv deletes the bundle; rollback's remote rm status masked by sync; printed manual restore command can run on a confirmed slot B and overwrite an agent; "did not return" wording when every poll failed; Ctrl-C during the reboot ssh runs rollback; git work-tree check fails open; empty /data/ota left after rollback
Task 10: complete (commits a74bec4..28b6766, review clean after 1 fix round)
Tasks 1-10: complete. Final whole-branch review dispatched (opus, 0dd0637..28b6766).
Final review (opus): With fixes — no Critical; Important 1 (plan defect: spec §12 lists rauc-post-install among the host-tested boot scripts; it can succeed having installed nothing). Minors 1–10. Deferred-minor triage: none must be fixed before the device steps; four sit on the device path (Task 8 post-install test, set-primary file check, cleanup_legacy-as-failure; Task 10 manual restore command).
Correction (from the final review): the Task 6 minor about a restart before the trial reboot marks the newly installed, untried slot bad — not "the good slot".
Ruling: ONE final fix wave = Important 1 + Minors 1, 2, 3, 4, 5, 6, 7, 8; Minor 9 (rpicam demo apps in prod) left for the owner (spec §6.5 names rpicam-hello dev-only) and Minor 10 (update sent during outcome delivery is acked but not run; brain row locked 30 min) parked — the two-heartbeat delivery window is ~30 s — costs: an owner retry in that window.
Final fix wave: dispatched (sonnet, BASE 28b6766; findings file final-fix-findings.md)
Task 11: prod build at a74bec4 ran 14:59–16:23 KST (1 h 24 min incl. toolchain); image + bundle built, `rauc info --keyring` ok, every check OK except "rauc has no D-Bus service": the rootfs has usr/share/dbus-1/interfaces/de.pengutronix.rauc.Installer.xml (interface description RAUC installs without its service), no rauc.service / rauc-mark-good.service. Root cause: the check's regex was broader than "a service". Added to the final fix wave: narrow it to the units, the system-services activation file and the bus policy. Outputs not copied (check-image failed first); prod is rebuilt from the post-fix commit anyway.
Final fix wave: DONE (84f93ab, 7b6316c, 1fa538a, e9d590a, e0045ee; boot 113 + post-install 35 + publish 47 + migrate 113 ok; brain 1175 passed, 99 %); scoped re-review dispatched (opus)
Task 11: dev build started from e0045ee in /home/soul/loopi-p4 (incremental /br/output; log scratchpad/build-dev-e0045ee.log)
Task 11: dev build e0045ee OK 16:25–16:29 KST (kernel rebuilt with DM_VERITY/SQUASHFS_ZLIB, check-image 114 OK, 0 FAIL); outputs os/out/loopi-dev-2026.10.19-e0045ee.{img 407 MB, raucb 62.3 MB, squashfs 68.7 MB, json}. Prod build from e0045ee started in /home/soul/loopi-build (log build-prod-e0045ee.log).
Task 11: prod build e0045ee OK 16:29:52–16:30:15 (incremental; check-image 95 OK, 0 FAIL; the narrowed D-Bus check passes); outputs /home/soul/loopi-build/os/out/loopi-prod-2026.10.19-e0045ee.{img, raucb 61.4 MB, squashfs 67.7 MB, json}
Final fix wave re-review (opus): all 9 items ADDRESSED, no new Critical/Important. Minor: guard logs "slot  was not confirmed" with an empty slot. Out of scope: (1) RAUC 1.15 call order post-install before set-primary is now load-bearing — confirm in the device log at the first OTA (a reversed order fails safely: nothing activated); (2) the new image checks ran for real in the e0045ee builds (pass); (3) POST /devices/{id}/update still accepts a device without boot_slot (UI-only lock).
Final review: clean after one fix wave (commits 28b6766..e0045ee). Workspace kept until Tasks 11–12 finish.
Task 11 Step 4 (16:32 KST): pg_dump → ~/loopi-backups/p3b-2026-10-19-1632.dump (16.8 MB); ROLLBACK=b2c9607; feat/phase2 fast-forwarded to feat/p4 e0045ee; /home/soul/loopi-data/ota created; up -d --build healthy; 008_ota.sql applied 07:32 UTC; smoke.sh PASSED; /devices, /versions, /healthz 200 (inside the api); live device heartbeat 11 s old (old agent still accepted).
Task 11 Step 5: published loopi-dev-2026.10.19-e0045ee (20 notes, no base). Task 11: complete.
Task 12 Step 1 attempt 1 (16:33–16:47 KST): migrate-to-ab.sh wrote p3 + b/ + tryboot.txt; the trial boot of slot B came up (rauc.slot=B, heartbeats 200 from 07:37:08 to 07:46:38 UTC) but was never confirmed; the guard rebooted at 10 min and the device came back on the old system; the script removed pending.json — automatic rollback demonstrated on the real device.
Debug (systematic): /data/rauc never created → mark-good never ran. Second manual trial (16:48 KST): the agent journal has no "system" or "trial boot" lines; /usr/bin/loopi-agent on slot B has 0 OTA strings. Root cause: Buildroot's local-site loopi-agent package is rsynced only once; /br/output's agent stamps date from 2026-10-15, so dev images built since carry the P1 agent. /br/output-prod was fresh (rsynced at a74bec4) → the prod agent has the OTA code (1 match).
Ruling: fix container-build.sh's image branch to run `make loopi-agent-rebuild` before the image build (the `agent` target already does), and add a check-image check that the shipped agent supports OTA (marker string, same pattern as the D19 gain check); dispatch on the Task 9a files, scoped re-review, rebuild dev; remove the published dev bundle e0045ee (it carries the P1 agent; never installed) — the image check had no way to see a stale agent — costs: each image build recompiles the agent (cargo incremental in the package build dir).
Task 12 fix: 381bc70 (container-build rebuilds loopi-agent in image builds; check-image 'the shipped agent supports OTA updates'); scoped re-review dispatched; dev rebuild started
Task 12 fix verified by a real build: dev 381bc70 built 16:52–16:53 KST, loopi-agent re-rsynced (07:52 UTC) and rebuilt, check-image 115 OK / 0 FAIL incl. 'the shipped agent supports OTA updates'; outputs os/out/loopi-dev-2026.10.19-381bc70.{img,raucb 62.3 MB,squashfs,json}
Task 12 fix 381bc70: scoped re-review — ADDRESSED (Buildroot 2026.02 pkg-generic.mk confirms -rebuild removes .stamp_rsynced for local-site packages; check runs on both variants; real artefacts carry the marker), no new Critical/Important. Minor: stale D19 comment mentions loopi-agent-dirclean.
Task 12 Step 1 attempt 2 started 16:59 KST with loopi-dev-2026.10.19-381bc70 (published, 20 notes); device back on the old system after the 2nd guard rollback (16:59:27), pending.json removed
Task 12 Step 1 PASS (16:59–17:08 KST, attempt 2): migrate-to-ab.sh confirmed slot B (2026.10.19-381bc70). Device: root=/dev/mmcblk0p3 rauc.slot=B, config.txt os_prefix=b/, boot-state "B good", rauc status B booted good / A inactive bad; agent journal "trial boot: confirming…" then "update finished state=Succeeded". Brain: heartbeat os_version 2026.10.19-381bc70, dev, slot B, ota succeeded, recording video+audio, items arriving. Legacy root kernel files still in /boot until slot A is installed.
