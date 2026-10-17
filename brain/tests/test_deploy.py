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
    assert worker["volumes"] == ["${LOOPI_DATA_DIR:-/srv/loopi}:/srv/loopi", PROVIDERS_MOUNT]
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


def test_the_readme_says_how_to_connect_claude_code():
    readme = (DEPLOY / "README.md").read_text()
    assert "## MCP for Claude Code (P3a)" in readme
    assert "NODE_EXTRA_CA_CERTS" in readme and "claude mcp add --transport http loopi" in readme
    assert "loopi-brain mcp-token create" in readme
