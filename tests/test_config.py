"""Tests for pool configuration settings."""

from incubator.config import Settings


def test_pool_defaults():
    """Pool settings have sensible defaults."""
    s = Settings(
        anthropic_api_key="test",
        telegram_bot_token="test",
        telegram_chat_id="test",
    )
    assert s.pool_size == 3
    assert s.cycle_time_minutes == 30


def test_pool_settings_from_env(monkeypatch):
    """Pool settings can be overridden via env vars."""
    monkeypatch.setenv("POOL_SIZE", "5")
    monkeypatch.setenv("CYCLE_TIME_MINUTES", "15")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test")
    s = Settings()
    assert s.pool_size == 5
    assert s.cycle_time_minutes == 15
