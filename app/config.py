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
    # SP-API (Phase 2, dormant) -- all default "" like scraperapi_key; see
    # app/spapi_client.py's is_configured(). No Pro-seller account exists
    # yet (see app/pricing/fees.py's module docstring), so these are unset
    # in every real deployment today -- ready the moment that changes.
    spapi_client_id: str
    spapi_client_secret: str
    spapi_refresh_token: str
    spapi_seller_id: str
    spapi_marketplace_id: str
    # PWA CORS origin (Phase 2 /scan) -- empty means CORS is not enabled at
    # all (see app/main.py), not "allow everything".
    pwa_origin: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        keepa_api_key=os.environ.get("KEEPA_API_KEY", ""),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        pwa_shared_secret=os.environ.get("PWA_SHARED_SECRET", ""),
        environment=os.environ.get("ENVIRONMENT", "development"),
        scraperapi_key=os.environ.get("SCRAPERAPI_KEY", ""),
        spapi_client_id=os.environ.get("SPAPI_CLIENT_ID", ""),
        spapi_client_secret=os.environ.get("SPAPI_CLIENT_SECRET", ""),
        spapi_refresh_token=os.environ.get("SPAPI_REFRESH_TOKEN", ""),
        spapi_seller_id=os.environ.get("SPAPI_SELLER_ID", ""),
        spapi_marketplace_id=os.environ.get("SPAPI_MARKETPLACE_ID", ""),
        pwa_origin=os.environ.get("PWA_ORIGIN", ""),
    )


@lru_cache
def get_config() -> dict:
    path = Path(os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)
