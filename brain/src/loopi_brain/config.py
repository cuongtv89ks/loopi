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

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)
