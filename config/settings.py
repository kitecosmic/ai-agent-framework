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

    # ── LLM Principal ────────────────────────────────────
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "deepseek-r1:14b"
    default_llm_provider: str = "ollama"
    default_llm_model: str = "deepseek-r1:14b"

    # ── Multi-Model Routing (modelos especializados) ──────
    # El agente elige automáticamente el mejor modelo según la tarea
    model_routing_enabled: bool = True
    ollama_vision_model: str = "qwen3-vl:32b"       # Fotos, imágenes, video
    ollama_coding_model: str = "qwen3-coder:latest"  # Código, debugging, programación
    ollama_reasoning_model: str = "deepseek-r1:14b"  # Análisis profundo, razonamiento
    ollama_ocr_model: str = "glm-ocr:latest"         # Extraer texto de imágenes
    ollama_fast_model: str = "phi4-mini:latest"       # Respuestas rápidas y simples

    # ── MiniMax ──────────────────────────────────────────
    minimax_api_key: str = ""
    minimax_model: str = "MiniMax-M1-m-2.5"

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

    # ── System Module ─────────────────────────────────────
    system_allowed_root: str = "~"
    system_command_timeout: int = 30
    system_max_output: int = 10000

    # ── Admin & Seguridad ─────────────────────────────────
    # Chat IDs de Telegram con acceso de administrador (comma-separated)
    # Admins pueden usar /config para configurar el agente sin tocar la terminal
    admin_chat_ids: str = ""

    # ── RapiBase (github.com/kitecosmic/rapibase) ────────
    rapibase_url: str = ""
    # Service Key: acceso total sin JWT (para el agente / backend)
    rapibase_service_key: str = ""
    # Anon Key: acceso público (requiere JWT del usuario)
    rapibase_anon_key: str = ""
    # Alias retrocompatible — se usa como service key si está configurado
    rapibase_api_key: str = ""

    # ── Audio / Transcripción de voz ─────────────────────
    # Providers: "faster_whisper" (local, recomendado) | "openai" | "ollama"
    audio_transcription_provider: str = "faster_whisper"
    # Modelos faster-whisper: tiny, base, small, medium, large-v3, turbo (recomendado)
    faster_whisper_model: str = "turbo"
    faster_whisper_device: str = "auto"  # "auto" | "cpu" | "cuda"
    ollama_whisper_model: str = "whisper"  # fallback si usás ollama provider

    # ── MCP ────────────────────────────────────────────────
    mcp_servers: str = ""  # JSON array of server configs

    # ── Plugins ──────────────────────────────────────────
    plugins_dir: str = "plugins"
    plugins_hot_reload: bool = True

    # ── Logging ──────────────────────────────────────────
    log_level: str = "INFO"
    log_format: str = "json"

    def get_admin_chat_ids(self) -> list[int]:
        """Retorna lista de chat IDs de administradores."""
        if not self.admin_chat_ids:
            return []
        try:
            return [int(x.strip()) for x in self.admin_chat_ids.split(",") if x.strip()]
        except ValueError:
            return []


@lru_cache
def get_settings() -> Settings:
    return Settings()
