"""Application configuration module."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized configuration loaded from environment variables or .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="resilient-triage", description="Service application name")
    debug: bool = Field(default=False, description="Enable debug logging")
    host: str = Field(default="0.0.0.0", description="API bind host")
    port: int = Field(default=8000, description="API bind port")

    # Redis & Semantic Caching
    redis_url: str = Field(default="redis://localhost:6379/0", description="Redis connection URL")
    cache_similarity_threshold: float = Field(
        default=0.90,
        ge=0.0,
        le=1.0,
        description="Cosine similarity threshold for semantic cache HIT (>= 0.90)",
    )
    cache_ttl_seconds: int = Field(default=3600, ge=60, description="Semantic cache TTL in seconds")

    # Telemetry Sources
    statuspage_url: str = Field(
        default="https://www.githubstatus.com/api/v2/summary.json",
        description="Atlassian Statuspage summary endpoint",
    )

    # Resilience & Circuit Breaker
    circuit_breaker_fail_max: int = Field(
        default=3,
        ge=1,
        description="Consecutive failures before tripping breaker open",
    )
    circuit_breaker_reset_timeout: int = Field(
        default=30,
        ge=1,
        description="Seconds to stay OPEN before HALF_OPEN probe",
    )
    retry_max_attempts: int = Field(default=3, ge=1, description="Max tenacity retry attempts")
    retry_min_wait_seconds: float = Field(default=0.1, ge=0.0, description="Tenacity min wait")
    retry_max_wait_seconds: float = Field(default=1.0, ge=0.0, description="Tenacity max wait")

    # Self-Repair Loop
    max_self_repair_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Max retries allowed for Pydantic LLM self-repair loop",
    )


settings = Settings()
