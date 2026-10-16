"""Runtime settings, read from LOOPI_* environment variables."""
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
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
    asr_max_gain_db: float = Field(default=30.0, ge=0.0, le=60.0)  # level gain before VAD/Whisper (spec D19)
    # Understanding (P2b): text models (chapters, recap, chat) leave memory a minute after answering (the host is
    # short of memory: P2a deferred M14), get a context window large enough for their bounded prompts, and may
    # take long on a CPU: a request waits up to llm_timeout_s seconds for the whole answer.
    ollama_text_keep_alive: str = "1m"
    llm_num_ctx: int = Field(default=8192, ge=2048, le=131072)
    llm_timeout_s: float = Field(default=900.0, ge=60.0, le=3600.0)
    # Search (spec §10.4): a moment is relevant when its embedding is this close to the question's (cosine
    # distance), or when its text matches the question's words this well (pg_trgm word similarity). P2c tunes both.
    search_max_distance: float = Field(default=0.6, gt=0.0, le=2.0)
    search_min_similarity: float = Field(default=0.3, gt=0.0, le=1.0)
    # Optional cloud provider for text tasks (spec §9.3): only used when this key is set AND providers.yaml routes a
    # text task to "anthropic". Read from ANTHROPIC_API_KEY (no LOOPI_ prefix); never logged or shown.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    anthropic_url: str = "https://api.anthropic.com"

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def _blank_key_is_no_key(cls, value):
        return None if value is None or (isinstance(value, str) and not value.strip()) else value

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def asr_language_list(self) -> list[str]:
        return [code.strip() for code in self.asr_languages.split(",") if code.strip()]
