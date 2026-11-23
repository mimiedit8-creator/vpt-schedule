from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    database_path: str = Field(default="data/bot.sqlite3", alias="DATABASE_PATH")
    vpt_students_url: str = Field(default="https://volpt.ru/студентам", alias="VPT_STUDENTS_URL")
    schedule_url: str = Field(default="", alias="SCHEDULE_URL")
    schedule_group: str = Field(default="РКЛ-3", alias="SCHEDULE_GROUP")
    schedule_refresh_minutes: int = Field(default=15, alias="SCHEDULE_REFRESH_MINUTES")
    parcelle_api_key: str = Field(default="", alias="PARCELLE_API_KEY")
    parcelle_api_url: str = Field(default="https://api.parcelle.ru", alias="PARCELLE_API_URL")
    tracking_refresh_minutes: int = Field(default=60, alias="TRACKING_REFRESH_MINUTES")
    admin_ids: str = Field(default="", alias="ADMIN_IDS")

    @property
    def admins(self) -> set[int]:
        result = set()
        for item in self.admin_ids.split(","):
            item = item.strip()
            if item.isdigit():
                result.add(int(item))
        return result

settings = Settings()
