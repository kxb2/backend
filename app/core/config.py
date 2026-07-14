from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_env: str = "dev"
    database_url: str
    cors_allowed_origins: str = "http://localhost:3000"

    r2_endpoint_url: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket_name: str
    r2_public_url: str

    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-5"

    openai_api_key: str
    openai_image_model: str = "gpt-image-1"

    gemini_api_key: str
    gemini_image_model: str = "gemini-3.1-flash-image"

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()