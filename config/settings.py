"""
NexusAgent — Configuración centralizada con validación.
"""
from __future__ import annotations

from pathlib import Path
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── API ──────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_secret_key: str = "change-me"

    # ── Database ─────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus_agent"

    # ── Redis ────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── LLM ──────────────────────────────────────────────
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "deepseek-r1:14b"
    default_llm_provider: str = "ollama"
    default_llm_model: str = "deepseek-r1:14b"

    # ── Browser ──────────────────────────────────────────
    browser_headless: bool = True
    browser_timeout: int = 30_000
    browser_max_concurrent: int = 5

    # ── Messaging Bridge ─────────────────────────────────
    messaging_webhook_url: str = ""
    messaging_webhook_secret: str = ""
    messaging_ws_enabled: bool = True

    # ── Telegram ──────────────────────────────────────────
    telegram_bot_token: str = ""

    # ── Plugins ──────────────────────────────────────────
    plugins_dir: str = "plugins"
    plugins_hot_reload: bool = True

    # ── Logging ──────────────────────────────────────────
    log_level: str = "INFO"
    log_format: str = "json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
