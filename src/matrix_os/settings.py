from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    stocks_api_key: str
    slack_user_id: str
    slack_token: str
    local_tz: str = "America/Los_Angeles"
    weather_api_key: str
    lat: float
    lon: float


settings = Settings()
