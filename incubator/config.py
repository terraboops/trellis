from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Paths
    project_root: Path = Path(__file__).resolve().parent.parent
    blackboard_dir: Path = Path(__file__).resolve().parent.parent / "blackboard" / "ideas"
    workspace_dir: Path = Path(__file__).resolve().parent.parent / "workspace"
    registry_path: Path = Path(__file__).resolve().parent.parent / "registry.yaml"

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Budget defaults (USD)
    max_budget_ideation: float = 0.50
    max_budget_implementation: float = 10.00
    max_budget_validation: float = 1.00
    max_budget_release: float = 3.00
    max_budget_watcher: float = 0.10

    # Model tiers
    model_tier_high: str = "claude-sonnet-4-6"
    model_tier_low: str = "claude-haiku-4-5"

    # Watcher cadences
    watcher_competitive_cron: str = "0 */6 * * *"
    watcher_research_cron: str = "0 8 * * *"

    # Worker pool
    pool_size: int = 3
    cycle_time_minutes: int = 30

    # Web
    web_host: str = "0.0.0.0"
    web_port: int = 8000


def get_settings() -> Settings:
    return Settings()
