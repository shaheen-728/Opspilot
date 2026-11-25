from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", validate_assignment=True)
    provider: Literal["offline", "gemini"] = "offline"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    state_dir: Path = Path("var")
    checkpoint_url: SecretStr | None = None
    api_key: SecretStr | None = None
    max_tool_calls: int = Field(12, ge=4, le=30)
    model_timeout_seconds: int = Field(30, ge=1, le=120)
    incident_timeout_seconds: int = Field(120, ge=10, le=600)
    max_tokens_per_incident: int = Field(40000, ge=1000, le=200000)
    docurag_url: str | None = None
    input_usd_per_million: float | None = Field(None, ge=0)
    output_usd_per_million: float | None = Field(None, ge=0)
    langfuse_enabled: bool = False
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str = "https://cloud.langfuse.com"

    def prepare(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.provider == "gemini" and not self.gemini_api_key:
            raise ValueError("Set GEMINI_API_KEY before selecting PROVIDER=gemini")
