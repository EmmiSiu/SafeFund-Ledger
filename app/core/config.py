from decimal import Decimal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    project_name: str = "SafeFund Ledger"
    debug: bool = False
    environment: str = "development"
    secret_key: str = "changeme"
    database_url: str = "sqlite:///./data/safefund.db"
    reports_dir: str = "./data/reports"
    log_file: str = "./data/app.log"
    default_interest_internal: Decimal = Decimal("0.05")
    default_interest_external: Decimal = Decimal("0.08")
    total_quincenas: int = 24

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()