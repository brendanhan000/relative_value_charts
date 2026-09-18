"""Runtime configuration, loaded from .env / environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Schwab data comes from the central schwab_hub, which owns all credentials.
    hub_url: str = os.getenv("SCHWAB_HUB_URL", "http://127.0.0.1:8765")

    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = _int("PORT", 8182)

    quote_poll_seconds: int = _int("QUOTE_POLL_SECONDS", 5)
    history_cache_seconds: int = _int("HISTORY_CACHE_SECONDS", 30)

    # Screener pacing, set from measured behaviour rather than the published number.
    # Against a live account, 517 symbols at concurrency 16 completed in 6.3s with
    # zero errors; concurrency 24 tripped Schwab's edge protection (HTTP 429 then a
    # sustained 403 Access Denied that took minutes to clear). These defaults sit at
    # half the proven-clean concurrency, and the limiter backs off on its own if
    # Schwab ever pushes back. Raise them knowing where the cliff is.
    screen_requests_per_minute: int = _int("SCREEN_REQUESTS_PER_MINUTE", 1200)
    screen_concurrency: int = _int("SCREEN_CONCURRENCY", 8)

    # RVC_DEMO=1 serves synthetic bars so the UI runs without Schwab credentials.
    demo: bool = os.getenv("RVC_DEMO", "").lower() in {"1", "true", "yes"}


SETTINGS = Settings()
