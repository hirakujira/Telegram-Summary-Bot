from __future__ import annotations

import os
from dataclasses import dataclass

from app.reasoning import normalize_reasoning_effort


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    openai_api_key: str
    owner_telegram_user_id: int
    default_timezone: str = "UTC+8"
    default_cron_expr: str = "0 9 * * *"
    default_model: str = "gpt-5.6-luna"
    default_api_style: str = "auto"
    default_reasoning_effort: str = "default"
    sqlite_path: str = "/app/data/bot.db"
    max_messages_per_summary: int = 300
    min_messages_to_summary: int = 8
    max_summary_gap_hours: int = 24
    openai_max_output_tokens: int = 1800



def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required env var: {name}")
    return value



def load_settings() -> Settings:
    return Settings(
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        openai_api_key=_require_env("OPENAI_API_KEY"),
        owner_telegram_user_id=int(_require_env("OWNER_TELEGRAM_USER_ID")),
        default_timezone=os.getenv("DEFAULT_TIMEZONE", "UTC+8"),
        default_cron_expr=os.getenv("DEFAULT_CRON_EXPR", "0 9 * * *"),
        default_model=os.getenv("DEFAULT_MODEL", "gpt-5.6-luna"),
        default_api_style=os.getenv("DEFAULT_API_STYLE", "auto"),
        default_reasoning_effort=normalize_reasoning_effort(
            os.getenv("DEFAULT_REASONING_EFFORT", "default")
        ),
        sqlite_path=os.getenv("SQLITE_PATH", "/app/data/bot.db"),
        max_messages_per_summary=int(os.getenv("MAX_MESSAGES_PER_SUMMARY", "300")),
        min_messages_to_summary=int(os.getenv("MIN_MESSAGES_TO_SUMMARY", "8")),
        max_summary_gap_hours=int(os.getenv("MAX_SUMMARY_GAP_HOURS", "24")),
        openai_max_output_tokens=int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "1800")),
    )
