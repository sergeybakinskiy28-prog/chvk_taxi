from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    TELEGRAM_BOT_TOKEN: str
    DRIVER_CHAT_ID: int
    ADMIN_CHAT_ID: int
    DATABASE_URL: str
    SECRET_KEY: str
    API_BASE_URL: str = "http://api:8000"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
