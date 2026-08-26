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
    environment: str
    scraperapi_key: str
    # Supabase auth (replaces the old PWA_SHARED_SECRET model) -- hard-
    # required like database_url, not optional-with-default, since the app
    # cannot authenticate anyone without them. See app/auth.py.
    supabase_url: str
    supabase_service_key: str
    # Comma-separated allowlist of the handful of real users -- mirrors
    # sentimentfx-backend's ADMIN_EMAILS pattern. Empty (not unset) is a
    # valid, deliberate "deny everyone" state rather than a crash, so a
    # misconfigured deploy fails closed instead of taking down /health too
    # -- see auth.py's _allowlisted_emails().
    allowed_user_emails: str
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
        environment=os.environ.get("ENVIRONMENT", "development"),
        scraperapi_key=os.environ.get("SCRAPERAPI_KEY", ""),
        supabase_url=os.environ["SUPABASE_URL"],
        supabase_service_key=os.environ["SUPABASE_SERVICE_KEY"],
        allowed_user_emails=os.environ.get("ALLOWED_USER_EMAILS", ""),
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
