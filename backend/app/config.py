from functools import lru_cache
from pathlib import Path
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")


class Settings:
    # ── AliExpress ──────────────────────────────
    ALIEXPRESS_APP_KEY    = os.getenv("ALIEXPRESS_APP_KEY", "")
    ALIEXPRESS_APP_SECRET = os.getenv("ALIEXPRESS_APP_SECRET", "")
    ALIEXPRESS_API_URL    = os.getenv("ALIEXPRESS_API_URL", "https://api-sg.aliexpress.com/sync")

    # ── Shopify ─────────────────────────────────
    SHOPIFY_STORE         = os.getenv("SHOPIFY_STORE", "")          # e.g. my-store (without .myshopify.com)
    SHOPIFY_CLIENT_ID     = os.getenv("SHOPIFY_CLIENT_ID", "")      # from Dev Dashboard → App → Settings
    SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")  # from Dev Dashboard → App → Settings
    SHOPIFY_API_VERSION   = os.getenv("SHOPIFY_API_VERSION", "2025-01")

    # ── AliExpress OAuth ────────────────────────
    REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8001/callback")

    # ── Database ────────────────────────────────
    MYSQL_URL = os.getenv("MYSQL_URL", "mysql+pymysql://root:password@localhost/aliexpress_shopify")
    # config.py — add this line inside the Settings class
    DB_DUMP_SECRET = os.getenv("DB_DUMP_SECRET", "")   # set a strong random value in .env


@lru_cache
def get_settings() -> Settings:
    return Settings()