"""Runtime settings, read from LOOPI_* environment variables."""
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOOPI_")

    database_url: str
    media_root: Path = Path("/srv/loopi/media")
    pki_dir: Path = Path("/srv/loopi/pki")
    tz: str = "Asia/Seoul"
    min_free_bytes: int = 2 * 1024**3
    max_item_bytes: int = 64 * 1024**2
    pair_token_ttl_minutes: int = 30
    supported_agent_major: int = 0
    default_video_fps: int = 2
    # Perception pipeline (P2a)
    ollama_url: str = "http://models:11434"
    ollama_keep_alive: str = "30m"
    providers_file: Path | None = None
    model_dir: Path = Path("/srv/loopi/models")
    worker_concurrency: int = 2
    dispatch_max_queued: int = 20  # backlog jobs per task kept waiting at once (fresh uploads are not capped)
    keyframe_dark_luma: float = 24.0
    keyframe_motion_max: float = 8.0
    keyframe_hash_threshold: int = 12
    keyframe_min_gap_s: float = 30.0
    asr_languages: str = "vi,ko,en"
    whisper_cpu_threads: int = 4

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def asr_language_list(self) -> list[str]:
        return [code.strip() for code in self.asr_languages.split(",") if code.strip()]
