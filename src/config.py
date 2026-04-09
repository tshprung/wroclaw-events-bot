from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str | None
    telegram_channel_id: str | None
    admin_telegram_id: str | None
    db_path: str
    timezone: str
    dry_run: bool


def load_settings() -> Settings:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    channel = os.environ.get("TELEGRAM_CHANNEL_ID")
    admin = os.environ.get("ADMIN_TELEGRAM_ID")
    db_path = os.environ.get("DB_PATH", "./data/events.db")
    tz = os.environ.get("TIMEZONE", "Europe/Warsaw")
    dry = os.environ.get("DRY_RUN", "0").strip() == "1"
    return Settings(
        telegram_bot_token=tok,
        telegram_channel_id=channel,
        admin_telegram_id=admin,
        db_path=db_path,
        timezone=tz,
        dry_run=dry,
    )

