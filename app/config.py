"""Env vars + config.yaml, loaded lazily so importing this module never
requires secrets to be set (decision-engine tests must run standalone)."""
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


@dataclass(frozen=True)
class Settings:
    database_url: str
    keepa_api_key: str
    discord_webhook_url: str
    pwa_shared_secret: str
    environment: str
    scraperapi_key: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        keepa_api_key=os.environ.get("KEEPA_API_KEY", ""),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        pwa_shared_secret=os.environ.get("PWA_SHARED_SECRET", ""),
        environment=os.environ.get("ENVIRONMENT", "development"),
        scraperapi_key=os.environ.get("SCRAPERAPI_KEY", ""),
    )


@lru_cache
def get_config() -> dict:
    path = Path(os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)
