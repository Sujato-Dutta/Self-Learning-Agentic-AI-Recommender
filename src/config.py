from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "SmartReco"
    app_base_url: str = "http://localhost:8000"
    secret_key: SecretStr = SecretStr("development-only-change-me-please")
    database_url: str = "sqlite:///./smartreco.db"
    supabase_url: str | None = None
    supabase_service_role_key: SecretStr | None = None

    mesh_api_key: SecretStr | None = None
    # Cost-control gate. A configured key is necessary for production readiness,
    # but no chat or embedding request is allowed until this is explicitly enabled.
    mesh_calls_enabled: bool = False
    mesh_base_url: str = "https://api.meshapi.ai/v1"
    mesh_model: str = "openai/gpt-5.6-luna"
    mesh_embedding_model: str = "openai/text-embedding-3-small"

    pinecone_api_key: SecretStr | None = None
    pinecone_index_name: str = "smartreco-products"
    pinecone_namespace: str = "development"

    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "smartreco"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_workspace_id: str | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "hello@smartreco.local"
    smtp_use_tls: bool = True

    session_cookie_secure: bool = False
    recommendation_cooldown_minutes: int = Field(15, ge=1, le=1440)
    recommendation_ttl_minutes: int = Field(360, ge=5, le=10080)
    recommendation_hourly_limit: int = Field(4, ge=1, le=50)
    recommendation_daily_limit: int = Field(12, ge=1, le=200)
    event_batch_max_size: int = Field(50, ge=1, le=100)
    outbox_max_retries: int = Field(5, ge=1, le=20)
    scheduler_enabled: bool = True
    scheduler_metrics_port: int = Field(9101, ge=1024, le=65535)

    @field_validator("mesh_base_url")
    @classmethod
    def require_mesh_gateway(cls, value: str) -> str:
        if value.rstrip("/") != "https://api.meshapi.ai/v1":
            raise ValueError("MESH_BASE_URL must use the mandatory Mesh API gateway")
        return value.rstrip("/")

    @field_validator("mesh_model")
    @classmethod
    def require_mesh_luna(cls, value: str) -> str:
        model = value.strip()
        if model != "openai/gpt-5.6-luna":
            raise ValueError("MESH_MODEL must be openai/gpt-5.6-luna")
        return model

    def validate_production(self) -> None:
        if self.app_env != "production":
            return
        missing: list[str] = []
        if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            missing.append("DATABASE_URL (Supabase PostgreSQL)")
        if not self.mesh_api_key:
            missing.append("MESH_API_KEY")
        if not self.mesh_calls_enabled:
            missing.append("MESH_CALLS_ENABLED=true")
        if not self.pinecone_api_key:
            missing.append("PINECONE_API_KEY")
        if len(self.secret_key.get_secret_value()) < 32:
            missing.append("SECRET_KEY (32+ characters)")
        if missing:
            raise RuntimeError("Missing production configuration: " + ", ".join(missing))


@lru_cache
def get_settings() -> Settings:
    return Settings()
