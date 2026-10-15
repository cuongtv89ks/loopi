import subprocess
from pathlib import Path

import yaml

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
