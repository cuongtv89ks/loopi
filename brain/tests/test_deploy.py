import re
import shutil
import subprocess
from pathlib import Path

import yaml

from loopi_brain import acceptance

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
COMPOSE = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]
PROVIDERS_MOUNT = "./providers.yaml:/etc/loopi/providers.yaml:ro"
MEDIA_MOUNT = "${LOOPI_MEDIA_DIR:-${LOOPI_DATA_DIR:-/srv/loopi}/media}:/srv/loopi/media"


def test_worker_runs_the_pipeline_with_the_api_image_and_data():
    api, worker = SERVICES["api"], SERVICES["worker"]
    assert worker["build"] == api["build"] == "../brain"
    assert worker["command"] == ["loopi-brain", "worker"]
    assert worker["user"] == api["user"]
    env = worker["environment"]
    assert env["LOOPI_DATABASE_URL"] == api["environment"]["LOOPI_DATABASE_URL"]
    assert env["LOOPI_MEDIA_ROOT"] == "/srv/loopi/media"
    assert env["LOOPI_OLLAMA_URL"] == "http://models:11434"
    assert env["LOOPI_PROVIDERS_FILE"] == "/etc/loopi/providers.yaml"
    assert env["LOOPI_MODEL_DIR"] == "/srv/loopi/models" and env["HF_HOME"] == "/srv/loopi/models/hf"
    assert env["LOOPI_ASR_MAX_GAIN_DB"] == "${LOOPI_ASR_MAX_GAIN_DB:-30}"  # the README advertises it
    assert worker["volumes"] == ["${LOOPI_DATA_DIR:-/srv/loopi}:/srv/loopi", MEDIA_MOUNT, PROVIDERS_MOUNT]
    assert worker["healthcheck"]["test"] == ["CMD", "loopi-brain", "worker-health"]
    assert worker["healthcheck"]["interval"] == "30s" and worker["stop_grace_period"] == "2m"
    assert env["OMP_NUM_THREADS"] == env["OPENBLAS_NUM_THREADS"] == "1"
    assert worker["depends_on"] == {"db": {"condition": "service_healthy"}, "api": {"condition": "service_healthy"}}
    assert "ports" not in worker


def test_api_reads_the_same_providers_file_and_model_server():
    api = SERVICES["api"]
    assert api["environment"]["LOOPI_OLLAMA_URL"] == "http://models:11434"
    assert api["environment"]["LOOPI_PROVIDERS_FILE"] == "/etc/loopi/providers.yaml"
    assert PROVIDERS_MOUNT in api["volumes"]


def test_models_service_is_a_pinned_private_ollama():
    models = SERVICES["models"]
    assert models["image"] == "ollama/ollama:0.35.1"
    assert models["environment"] == {"OLLAMA_KEEP_ALIVE": "30m", "OLLAMA_MAX_LOADED_MODELS": "2",
                                     "OLLAMA_NUM_PARALLEL": "1"}
    assert models["volumes"] == ["ollama:/root/.ollama"] and "ollama" in COMPOSE["volumes"]
    assert models["healthcheck"]["test"] == ["CMD", "ollama", "list"]
    assert "ports" not in models  # reachable only inside the compose network


def test_every_image_is_pinned():
    for name, service in SERVICES.items():
        if "image" in service:
            tag = service["image"].rsplit(":", 1)
            assert len(tag) == 2 and tag[1] not in {"latest", ""}, name


def test_smoke_script_is_valid_bash():
    subprocess.run(["bash", "-n", str(DEPLOY / "smoke.sh")], check=True)


def test_smoke_script_removes_what_it_created_on_every_exit():
    script = (DEPLOY / "smoke.sh").read_text()
    delete = "DELETE FROM moment WHERE device_id = '$DEVICE_ID'; DELETE FROM item WHERE device_id = '$DEVICE_ID';"
    check = '[[ "$DEVICE_ID" =~ ^[0-9A-HJKMNP-TV-Z]{26}$ ]] || return 0'
    remove = 'rm -rf "/srv/loopi/media/raw/$DEVICE_ID" "/srv/loopi/media/view/$DEVICE_ID"'
    assert delete in script and remove in script and '"/srv/loopi/media/keyframes/$DEVICE_ID"' in script
    # The id is validated before anything is deleted, and the device is revoked last.
    assert script.index(check) < script.index(delete) < script.index(remove) < script.index("SET revoked = TRUE")
    # The exit trap is armed before the device exists, and the device id is known before the first upload.
    first_upload = script.index('expect 201 "${MTLS[@]}"')
    assert script.index("trap on_exit EXIT") < script.index("/v1/pair") < script.index("DEVICE_ID=\"$(") < first_upload
    assert 'SMOKE_ANALYSIS_TIMEOUT="${SMOKE_ANALYSIS_TIMEOUT:-90}"' in script
    # Deletion and revocation are separate steps of the exit trap: the device is revoked whatever happened to the
    # DELETE or the rm (the api container may be the very thing that is down), by id when the id is a valid ULID,
    # otherwise by the certificate fingerprint (valid fingerprints only).
    remove_body = script.split("remove_smoke_data() {")[1].split("\n}\n")[0]
    revoke_body = script.split("revoke_smoke_device() {")[1].split("\n}\n")[0]
    on_exit_body = script.split("on_exit() {")[1].split("\n}\n")[0]
    assert "revoked" not in remove_body
    assert "id = '$DEVICE_ID'" in revoke_body and "cert_fingerprint = '$FP'" in revoke_body
    assert '[[ "$DEVICE_ID" =~ ^[0-9A-HJKMNP-TV-Z]{26}$ ]]' in revoke_body
    assert '[[ "$FP" =~ ^[0-9a-f]{64}$ ]]' in revoke_body
    assert on_exit_body.index("remove_smoke_data ||") < on_exit_body.index("revoke_smoke_device ||")
    assert "&&" not in on_exit_body and "-n " not in on_exit_body
    # The pipeline check: a static test picture (an animated one is rejected as motion blur by the keyframe
    # selection and yields no keyframe), and the wait ends on either final analysis state.
    assert "smptebars=size=320x240:rate=2" in script and "testsrc" not in script
    assert '[[ "$STATE" == "done" || "$STATE" == "skipped" ]] && break' in script


def test_the_worker_has_a_third_slot_for_one_text_model_job():
    assert SERVICES["worker"]["environment"]["LOOPI_WORKER_CONCURRENCY"] == "3"
    assert "mem_limit" not in SERVICES["models"]  # a kill would stop a caption mid-job; memory is checked in Task 14


def test_the_anthropic_key_is_optional_and_comes_only_from_the_env_file():
    for name in ("api", "worker"):
        assert SERVICES[name]["environment"]["ANTHROPIC_API_KEY"] == "${ANTHROPIC_API_KEY:-}"
    example = (DEPLOY / ".env.example").read_text()
    assert "\nANTHROPIC_API_KEY=\n" in example  # empty: 100 % local unless the owner adds a key
    providers = yaml.safe_load((DEPLOY / "providers.yaml").read_text())
    for task, route in providers["tasks"].items():  # 100 % local by default: no route, fallbacks included
        while route:
            assert route["provider"] != "anthropic", f"{task} routes to anthropic; keep the examples commented out"
            route = route.get("fallback")


def test_the_golden_set_lives_in_the_data_dir_for_the_page_and_the_runner():
    # Spec D21: the api writes the owner's golden copies, the worker's evaluation runs read them; both under the
    # mounted data dir (not in git, not in an image), at the same path.
    for name in ("api", "worker"):
        service = SERVICES[name]
        assert service["environment"]["LOOPI_EVAL_DIR"] == "/srv/loopi/eval"
        assert "${LOOPI_DATA_DIR:-/srv/loopi}:/srv/loopi" in service["volumes"]
    # The rules and the VAD threshold are not passed through: an empty variable would override a default the
    # golden set's numbers chose (spec §13), so they change only in the brain's code.
    assert not {"LOOPI_ASR_RULES", "LOOPI_ASR_VAD_THRESHOLD"} & set(SERVICES["worker"]["environment"])
    readme = (DEPLOY / "README.md").read_text()
    assert "## Evaluation and acceptance (P2c)" in readme and "loopi-brain eval run" in readme
    assert "loopi-brain acceptance" in readme and "for s in api worker models db; do" in readme
    assert "loopi-brain eval compare" in readme and "loopi-brain eval rules-impact" in readme


FAKE_DOCKER = """\
#!/bin/bash
# Stands in for docker: the cgroup files of a container and its start time, nothing else. Like `docker compose exec
# -T`, an exec reads its standard input to the end: a loop that does not close it swallows the rest of its script.
case "$1 $2 $3 $4 $5" in
  "compose -p deploy exec -T")
    service="$6"
    cat >/dev/null
    case "$7 $8" in
      "cat /sys/fs/cgroup/cpu.stat") printf 'usage_usec %s\\nuser_usec 1\\nsystem_usec 2\\n' "$((FAKE_USAGE + ${#service}))" ;;
      "cat /sys/fs/cgroup/memory.peak") echo "$((1000 * ${#service}))" ;;
      *) exit 64 ;;
    esac ;;
  "inspect -f {{.State.StartedAt}} deploy-"*"-1 ") echo 2026-10-15T14:51:34.148257075Z ;;
  *) exit 64 ;;
esac
"""
FAKE_DATE = """\
#!/bin/bash
case "$*" in
  "-u +%FT%TZ") echo "$FAKE_NOW" ;;
  "+%F") echo "${FAKE_NOW%%T*}" ;;
  *) exit 64 ;;
esac
"""


def test_the_readme_cpu_loop_writes_the_lines_that_acceptance_reads(tmp_path):
    # Task 14 feeds the file this loop writes to `loopi-brain acceptance`: run the README's own block, twice, 24 h
    # apart, with a stand-in for docker (PATH holds only the stand-ins, so no real docker is reachable).
    readme = (DEPLOY / "README.md").read_text()
    section = readme.split("## Evaluation and acceptance (P2c)", 1)[1]
    block = re.search(r"```bash\n(.*?)```", section, re.DOTALL).group(1)
    bin_dir, home = tmp_path / "bin", tmp_path / "home"
    bin_dir.mkdir()
    home.mkdir()  # no ~/loopi-backups yet: the block makes it, or its first `>>` fails
    for name, body in (("docker", FAKE_DOCKER), ("date", FAKE_DATE)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    for tool in ("grep", "mkdir", "cat"):  # the only real programs the block and the stand-ins use
        (bin_dir / tool).symlink_to(shutil.which(tool))
    for now, usage in (("2026-10-16T09:00:00Z", 1_000_000), ("2026-10-17T09:05:00Z", 87_400_000)):
        env = {"PATH": str(bin_dir), "HOME": str(home), "FAKE_NOW": now, "FAKE_USAGE": str(usage)}
        if now.startswith("2026-10-17"):
            env["window"] = "2026-10-16"  # the second sample goes to the file of the day the window started
        # The block is the script on bash's standard input (an ssh session, a pasted block): every `docker compose
        # exec` must be told `</dev/null`, or the first one swallows the rest of the block.
        done = subprocess.run([shutil.which("bash"), "-s"], input=block, env=env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    files = sorted(path.name for path in (home / "loopi-backups").iterdir())
    assert files == ["p2-cpu-samples-2026-10-16.txt"]  # one dated file for the whole window
    text = (home / "loopi-backups" / files[0]).read_text()
    samples, peaks = acceptance.parse_cpu(text)
    assert len(text.splitlines()) == 16 == len(samples) + 8  # every line is one acceptance understands
    assert sorted({s.service for s in samples}) == ["api", "db", "models", "worker"]
    assert peaks == {"api": 3000, "worker": 6000, "models": 6000, "db": 2000}
    cpu = acceptance.cpu_per_day(samples)
    assert not any(row["restarted"] for row in cpu.values())
    assert round(cpu["models"]["hours"], 2) == 24.08
    assert round(cpu["models"]["cpu_s"], 3) == 86.4


def test_mcp_goes_to_the_api_without_basic_auth_and_without_a_client_fingerprint():
    caddy = (DEPLOY / "Caddyfile").read_text()
    assert "\thandle /mcp* {" in caddy
    block = caddy.split("\thandle /mcp* {", 1)[1].split("\n\t}\n", 1)[0]
    assert "reverse_proxy api:8000" in block and "header_up -X-Loopi-Client-Fingerprint" in block
    assert "max_size 64KB" in block and "basic_auth" not in block
    assert caddy.index("\thandle /mcp* {") < caddy.index("\thandle {\n")
    # Caddy matches the cleaned path but forwards the raw one: without the rewrite, /commitments/../mcp would skip
    # basic auth and reach a web handler, and /mcp/ would draw Starlette's http:// redirect.
    directives = [line.strip() for line in block.splitlines() if line.strip()]
    assert directives[0] == "rewrite * /mcp"  # the block's first directive: the api always sees exactly /mcp
    assert block.index("rewrite * /mcp") < block.index("reverse_proxy")


def test_the_readme_says_how_to_connect_claude_code():
    readme = (DEPLOY / "README.md").read_text()
    assert "## MCP for Claude Code (P3a)" in readme
    section = readme.split("## MCP for Claude Code (P3a)", 1)[1]
    assert "claude mcp add --scope user --transport http loopi https://localhost:8443/mcp" in section
    assert "export NODE_EXTRA_CA_CERTS=/srv/loopi/pki/ca.crt" in section  # the path itself, not an unexported variable
    assert "$LOOPI_DATA_DIR" not in section and "deploy/.env" in section
    assert "loopi-brain mcp-token create" in readme


def test_media_may_live_on_their_own_disk_and_are_written_only_while_it_is_mounted():
    for name in ("api", "worker"):
        service = SERVICES[name]
        # after the data dir: the media mount is laid over its media/ (unset, the same directory as before)
        assert service["volumes"][:2] == ["${LOOPI_DATA_DIR:-/srv/loopi}:/srv/loopi", MEDIA_MOUNT]
        assert service["environment"]["LOOPI_MEDIA_REQUIRE_MARKER"] == "${LOOPI_MEDIA_REQUIRE_MARKER:-false}"
    example = (DEPLOY / ".env.example").read_text()
    assert "# LOOPI_MEDIA_DIR=" in example and "# LOOPI_MEDIA_REQUIRE_MARKER=true" in example
    readme = (DEPLOY / "README.md").read_text()
    section = readme.split("## Media on their own disk", 1)[1].split("\n## ", 1)[0]
    assert "touch /mnt/loopi-media/.loopi-media" in section and "LOOPI_MEDIA_REQUIRE_MARKER=true" in section
    assert "rsync" in section and "--delete" in section  # the last pass also drops what a forget removed meanwhile
    assert "--exclude=/lost+found" in section  # mkfs's root-owned directory exists only on the new disk
    assert r"After=mnt-loopi\x2dmedia.mount" in section  # a nofail mount is not ordered before Docker by itself
    assert "docker compose restart api worker" in section  # up -d keeps a running container on the bare mount point


def test_the_cameras_service_runs_the_agents_with_only_their_directory():
    service = SERVICES["cameras"]
    assert service["build"] == {"context": "..", "dockerfile": "deploy/cameras.Dockerfile"}
    assert service["user"] == "${LOOPI_UID:-1000}:${LOOPI_GID:-1000}"
    assert service["init"] is True
    # The agents ask the kernel whether the clock is synced ([clock] source = "kernel"): no host directory but theirs.
    assert service["volumes"] == ["${LOOPI_DATA_DIR:-/srv/loopi}/cameras:/srv/loopi/cameras"]
    assert service["depends_on"] == {"api": {"condition": "service_healthy"}}
    assert service["restart"] == "unless-stopped"
    # loopi-cameras drains each agent for up to 15 s on SIGTERM; Docker's default 10 s would SIGKILL a slow drain
    assert service["stop_grace_period"] == "30s"
    assert "ports" not in service and "environment" not in service
    # The chattiest logger (a camera with sound but a silent stream logs every ~10 s): the local driver, rotated.
    assert service["logging"] == {"driver": "local", "options": {"max-size": "10m", "max-file": "3"}}


def test_the_cameras_container_shares_a_network_with_the_proxy_only():
    # ffmpeg parses bytes from the LAN in there: it must not reach the api (no basic auth behind Caddy), db or models.
    assert COMPOSE["networks"] == {"cameras": {}}
    assert SERVICES["cameras"]["networks"] == ["cameras"]
    assert SERVICES["proxy"]["networks"] == ["default", "cameras"]
    for name in ("api", "db", "worker", "models"):
        assert "cameras" not in SERVICES[name].get("networks", ["default"]), name
    assert set(SERVICES) == {"db", "api", "worker", "models", "proxy", "cameras"}


def test_the_brain_image_installs_ffmpeg_and_the_vlog_label_font():
    text = (DEPLOY.parent / "brain" / "Dockerfile").read_text()
    installed = re.search(r"apt-get install -y --no-install-recommends ([^&\\]+)", text).group(1).split()
    assert {"ffmpeg", "fonts-dejavu-core"} <= set(installed), installed  # DejaVu Sans draws the place labels (R13)


def test_the_cameras_image_builds_both_binaries_and_runs_as_the_data_owner():
    text = (DEPLOY / "cameras.Dockerfile").read_text()
    assert "cargo build --release --locked --bin loopi-agent --bin loopi-cameras" in text
    assert "ffmpeg" in text and "USER loopi" in text
    runtime = text.split("\nFROM ")[2]  # the stage after the build stage
    installed = re.search(r"apt-get install -y --no-install-recommends ([^&\\]+)", runtime).group(1).split()
    assert {"ffmpeg", "libasound2", "libopus0"} <= set(installed), installed  # the agent links ALSA and Opus
    assert 'CMD ["loopi-cameras", "--root", "/srv/loopi/cameras"]' in text
    ignore = (DEPLOY.parent / ".dockerignore").read_text().split()
    assert ignore[:2] == ["*", "!agent/"] and "agent/target/" in ignore


def _p3c3_note() -> str:
    readme = (DEPLOY / "README.md").read_text()
    section = readme.split("## Network cameras (P3c-2)", 1)[1].split("\n## ", 1)[0]
    return section.split("**Deploying and rolling back P3c-3:**", 1)[1].split("\n- ", 1)[0]


def test_the_readme_says_how_to_deploy_p3c3():
    note = _p3c3_note()
    assert "api and worker together" in note and "docker compose -p deploy up -d --build" in note
    assert "Tìm tiếng nói" in note  # retried from Xử lý after the deploy
    assert "docker compose -p deploy stop api worker" in note  # no new dispatcher defers a detection again meanwhile


async def test_the_readme_rollback_hands_every_pending_detection_back_to_the_old_dispatcher(pool, pki_settings,
                                                                                           jobs_open):
    import datetime as dt

    from helpers import fetch_all, insert_item, paired_device
    from loopi_brain.pipeline import dispatch, jobs

    (sql,) = re.findall(r'psql -U loopi -d loopi -c "([^"]+)"', _p3c3_note())
    pi = await paired_device(pool, pki_settings)
    start = dt.datetime(2026, 10, 21, 1, 0, tzinfo=dt.UTC)
    items = {}
    for k, status in enumerate(("todo", "failed", "succeeded", "doing")):  # an older worker cannot run any of them
        items[status] = await insert_item(pool, pi["device_id"], kind="audio", started_at=start + dt.timedelta(minutes=k))
        job = await dispatch.defer_item(pool, items[status])
        async with pool.connection() as conn:
            await conn.execute("UPDATE procrastinate_jobs SET status = %s WHERE id = %s", (status, job))
    whisper = await insert_item(pool, pi["device_id"], kind="audio", started_at=start + dt.timedelta(minutes=9))
    video = await insert_item(pool, pi["device_id"], kind="video", started_at=start)
    async with pool.connection() as conn:
        job = await jobs.enqueue(conn, jobs.TRANSCRIBE_AUDIO, queue="audio", lock="asr",
                                 queueing_lock=f"asr:{whisper}", item_id=whisper)  # the old image runs these
        await conn.execute("UPDATE item SET analysis_job_id = %s WHERE id = %s", (job, whisper))
    await dispatch.defer_item(pool, video)
    async with pool.connection() as conn:
        await conn.execute(sql)
    rows = {r["id"]: r["analysis_job_id"] for r in await fetch_all(pool, "SELECT id, analysis_job_id FROM item")}
    assert all(rows[i] is None for i in items.values())  # the old dispatcher defers its own audio job for them
    assert rows[whisper] is not None and rows[video] is not None
