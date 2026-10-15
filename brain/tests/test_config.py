from pathlib import Path

from loopi_brain.config import Settings


def test_pipeline_defaults():
    s = Settings(database_url="postgresql://x")
    assert s.ollama_url == "http://models:11434"
    assert s.ollama_keep_alive == "30m"
    assert s.providers_file is None
    assert s.model_dir == Path("/srv/loopi/models")
    assert s.worker_concurrency == 2 and s.dispatch_max_queued == 20
    assert (s.keyframe_dark_luma, s.keyframe_motion_max, s.keyframe_hash_threshold, s.keyframe_min_gap_s) == (
        24.0, 8.0, 12, 30.0,
    )
    assert s.asr_language_list == ["vi", "ko", "en"]
    assert s.whisper_cpu_threads == 4


def test_pipeline_settings_from_env(monkeypatch):
    monkeypatch.setenv("LOOPI_DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("LOOPI_ASR_LANGUAGES", " ko , en ,")
    monkeypatch.setenv("LOOPI_KEYFRAME_MIN_GAP_S", "45")
    monkeypatch.setenv("LOOPI_PROVIDERS_FILE", "/etc/loopi/providers.yaml")
    s = Settings()
    assert s.asr_language_list == ["ko", "en"]
    assert s.keyframe_min_gap_s == 45.0
    assert s.providers_file == Path("/etc/loopi/providers.yaml")
