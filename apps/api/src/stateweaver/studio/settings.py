from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class RetryPolicy(BaseModel):
    max_retries: int = Field(default=3, ge=0)
    base_delay_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    max_delay_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_bounds(self) -> "RetryPolicy":
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be at least base_delay_seconds")
        return self

    def delay(self, prior_retries: int, retry_after_seconds: float = 0) -> float:
        return max(
            retry_after_seconds,
            min(self.max_delay_seconds, self.base_delay_seconds * (1 << min(prior_retries, 30))),
        )


class StudioSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTVIDEO_STUDIO_",
        extra="ignore",
        env_nested_delimiter="__",
        env_file=Path(__file__).resolve().parents[5] / ".env",
    )

    database_url: str | URL = "sqlite:///./var/studio/agentvideo.db"
    cas_root: Path = Path("var/studio/cas")
    work_root: Path = Path("var/studio/work")
    media_scratch_root: Path | None = None
    provider_registry_path: Path | None = None
    provider_ledger_path: Path = Path(__file__).resolve().parents[5] / "var/api-resources.db"
    role_runtime: Literal["agentvideo-v2"] = "agentvideo-v2"
    host: str = "127.0.0.1"
    port: int = 8186
    tick_seconds: float = Field(default=1, gt=0)
    lease_seconds: float = Field(default=120, gt=10)
    shutdown_drain_seconds: float = Field(default=600, ge=0)
    text_retry: RetryPolicy = Field(
        default_factory=lambda: RetryPolicy(base_delay_seconds=10, max_delay_seconds=60)
    )
    image_retry: RetryPolicy = Field(default_factory=RetryPolicy)
    video_retry: RetryPolicy = Field(default_factory=RetryPolicy)
    production_query_outage_seconds: float = Field(default=1800, gt=0)
    routing_calibration_path: Path | None = None
    actor_silence_seconds: float = Field(default=600, gt=0)
    actor_state_poll_seconds: float = Field(default=0.5, gt=0, le=2)
    actor_recovery_limit: int = Field(default=3, ge=0)
    actor_recovery_delay_seconds: float = Field(default=10, ge=0)
    query_error_limit: int = Field(default=3, ge=1)
    query_recovery_poll_seconds: float = Field(default=60, gt=0)
    roles_skill_root: Path = Path(__file__).resolve().parents[5] / "skills/studio"
