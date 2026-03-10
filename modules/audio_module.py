"""
Audio Module — Transcripción de audio a texto.

Providers soportados (en orden de preferencia recomendada):
1. faster_whisper  — Local, rápido, sin API key. Usa NVIDIA GPU con CUDA si disponible.
   Instalar: pip install faster-whisper
   Con soporte NVIDIA GPU: pip install faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12
   Modelos: tiny(75MB) | base(142MB) | small(461MB) | medium(1.5GB) | large-v3(3GB) | turbo(1.6GB, recomendado)
   El modelo se descarga automáticamente de HuggingFace en el primer uso.

2. openai          — OpenAI Whisper API (requiere OPENAI_API_KEY)
   Para usuarios sin GPU. Configurar OPENAI_API_KEY en .env.

3. ollama          — Solo si tu Ollama >= 0.7 tiene soporte de transcripción
   (actualmente NO hay modelo whisper disponible en el registry de Ollama)

Fallback automático:
  Si el provider configurado no está disponible, el módulo intenta automáticamente
  el siguiente en orden: faster_whisper → openai → ollama

Configurar en .env:
  AUDIO_TRANSCRIPTION_PROVIDER=faster_whisper   # faster_whisper | openai | ollama
  FASTER_WHISPER_MODEL=turbo                    # tiny/base/small/medium/large-v3/turbo
  FASTER_WHISPER_DEVICE=auto                    # auto/cpu/cuda
  OPENAI_API_KEY=sk-...                         # requerido para provider openai

Eventos:
- audio.transcribe → transcribe un archivo de audio a texto
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import structlog

from core.event_bus import Event
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()

# Thread pool para operaciones síncronas (faster-whisper no es async)
_executor = ThreadPoolExecutor(max_workers=2)


class AudioModule(PluginBase):
    """Módulo de transcripción de audio a texto."""

    name = "audio"
    version = "1.2.0"
    description = "Audio transcription: faster-whisper (local/GPU), OpenAI Whisper, or Ollama"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._provider: str = "faster_whisper"
        self._ollama_url: str = "http://localhost:11434"
        self._ollama_model: str = "whisper"
        self._openai_key: str = ""
        self._fw_model_name: str = "turbo"
        self._fw_device: str = "auto"
        self._fw_model = None  # faster-whisper model instance (lazy load)
        self._client: httpx.AsyncClient | None = None
        self._fw_available: bool = False
        self._openai_available: bool = False
        self._cuda_available: bool = False

    async def on_load(self):
        self._provider = self.config.get("audio_transcription_provider", "faster_whisper")
        self._ollama_url = self.config.get("ollama_base_url", "http://localhost:11434")
        self._ollama_model = self.config.get("ollama_whisper_model", "whisper")
        self._openai_key = self.config.get("openai_api_key", "")
        self._fw_model_name = self.config.get("faster_whisper_model", "turbo")
        self._fw_device = self.config.get("faster_whisper_device", "auto")
        self._client = httpx.AsyncClient(timeout=120.0)

        # Detectar disponibilidad de providers al inicio
        self._fw_available = self._check_faster_whisper()
        self._openai_available = bool(self._openai_key)
        self._cuda_available = self._check_cuda()

        logger.info(
            "audio.loaded",
            provider=self._provider,
            fw_available=self._fw_available,
            openai_available=self._openai_available,
            cuda=self._cuda_available,
            fw_model=self._fw_model_name if self._fw_available else "n/a",
        )

        # Advertir si el provider configurado no está disponible
        if self._provider == "faster_whisper" and not self._fw_available:
            if self._openai_available:
                logger.warning(
                    "audio.fw_not_installed_fallback_openai",
                    msg="faster-whisper no instalado, se usará OpenAI Whisper API como fallback",
                )
            else:
                logger.error(
                    "audio.no_transcription_available",
                    msg="faster-whisper no instalado y OPENAI_API_KEY no configurada. "
                        "Transcripción de audio NO funcionará. "
                        "Instalar: pip install faster-whisper  |  O configurar OPENAI_API_KEY en .env",
                )
        elif self._provider == "openai" and not self._openai_available:
            logger.error(
                "audio.openai_no_key",
                msg="Provider 'openai' configurado pero OPENAI_API_KEY no está en .env",
            )

    @staticmethod
    def _check_faster_whisper() -> bool:
        """Verifica si faster-whisper está instalado."""
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _check_cuda() -> bool:
        """Verifica si CUDA está disponible (GPU NVIDIA)."""
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            # Sin torch, faster-whisper puede detectar CUDA por su cuenta via ctranslate2
            try:
                import ctranslate2
                return "cuda" in ctranslate2.get_supported_compute_types("cuda")
            except Exception:
                return False

    async def on_unload(self):
        if self._client:
            await self._client.aclose()

    def _load_faster_whisper(self):
        """Carga el modelo faster-whisper (lazy, la primera vez descarga el modelo)."""
        if self._fw_model is not None:
            return self._fw_model
        if not self._fw_available:
            raise _FasterWhisperNotInstalled()

        from faster_whisper import WhisperModel
        device = self._fw_device
        if device == "auto":
            device = "cuda" if self._cuda_available else "cpu"
        compute = "float16" if device == "cuda" else "int8"
        logger.info("audio.loading_faster_whisper", model=self._fw_model_name, device=device, compute=compute)
        self._fw_model = WhisperModel(self._fw_model_name, device=device, compute_type=compute)
        logger.info("audio.faster_whisper_ready", model=self._fw_model_name, device=device)
        return self._fw_model

    def _resolve_provider_order(self) -> list[str]:
        """Devuelve la lista de providers a intentar, empezando por el configurado."""
        all_providers = ["faster_whisper", "openai", "ollama"]
        # El configurado va primero, luego los demás en orden
        order = [self._provider]
        for p in all_providers:
            if p not in order:
                order.append(p)
        return order

    def _can_use_provider(self, provider: str) -> bool:
        """Verifica si un provider está disponible para usar."""
        if provider == "faster_whisper":
            return self._fw_available
        if provider == "openai":
            return self._openai_available
        if provider == "ollama":
            return True  # Se intenta siempre, falla en runtime si no hay soporte
        return False

    @hook("audio.transcribe")
    async def handle_transcribe(self, event: Event) -> dict[str, Any]:
        """
        Transcribe un archivo de audio a texto.

        Data esperada:
        - file_path: str     → ruta al archivo de audio
        - audio_bytes: bytes → bytes del audio (alternativa a file_path)
        - file_name: str     → nombre del archivo (para determinar formato)
        - language: str      → idioma (opcional, None = auto-detect)
        """
        file_path = event.data.get("file_path", "")
        audio_bytes = event.data.get("audio_bytes")
        file_name = event.data.get("file_name", "audio.ogg")
        language = event.data.get("language")  # None = auto-detect

        tmp_path: str | None = None
        if audio_bytes and not file_path:
            suffix = Path(file_name).suffix or ".ogg"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            file_path = tmp_path

        if not file_path or not Path(file_path).exists():
            return {"success": False, "error": "Archivo de audio no encontrado"}

        try:
            text, used_provider = await self._transcribe_with_fallback(file_path, language)
            logger.info("audio.transcribed", chars=len(text), provider=used_provider)
            return {"success": True, "text": text, "provider": used_provider}

        except Exception as exc:
            logger.error("audio.transcribe_error", error=str(exc), provider=self._provider)
            return {"success": False, "error": str(exc)}
        finally:
            if tmp_path and Path(tmp_path).exists():
                Path(tmp_path).unlink(missing_ok=True)

    async def _transcribe_with_fallback(self, file_path: str, language: str | None) -> tuple[str, str]:
        """
        Intenta transcribir con el provider configurado.
        Si falla por no estar disponible, intenta el siguiente automáticamente.
        Retorna (texto, provider_usado).
        """
        provider_order = self._resolve_provider_order()
        last_error: Exception | None = None

        for provider in provider_order:
            if not self._can_use_provider(provider):
                continue
            try:
                text = await self._dispatch_transcribe(provider, file_path, language)
                if provider != self._provider:
                    logger.warning(
                        "audio.fallback_used",
                        configured=self._provider,
                        used=provider,
                    )
                return text, provider
            except _FasterWhisperNotInstalled:
                # No está instalado, seguir con el siguiente provider
                logger.warning("audio.fw_not_available", msg="faster-whisper no instalado, intentando siguiente provider")
                continue
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "audio.provider_failed",
                    provider=provider,
                    error=str(exc)[:200],
                )
                continue

        # Ningún provider funcionó
        raise RuntimeError(
            self._build_no_provider_error(last_error)
        )

    async def _dispatch_transcribe(self, provider: str, file_path: str, language: str | None) -> str:
        """Despacha la transcripción al provider indicado."""
        if provider == "openai":
            return await self._transcribe_openai(file_path, language)
        if provider == "ollama":
            return await self._transcribe_ollama(file_path, language)
        return await self._transcribe_faster_whisper(file_path, language)

    def _build_no_provider_error(self, last_error: Exception | None) -> str:
        """Construye un mensaje de error útil cuando ningún provider funciona."""
        parts = ["❌ No se pudo transcribir el audio. Opciones:"]
        if not self._fw_available:
            parts.append(
                "\n🖥️ Con GPU NVIDIA: pip install faster-whisper\n"
                "   (modelo se descarga automáticamente en el primer uso)"
            )
        if not self._openai_available:
            parts.append(
                "\n☁️ Sin GPU: Configurar OPENAI_API_KEY en .env y "
                "AUDIO_TRANSCRIPTION_PROVIDER=openai"
            )
        if last_error:
            parts.append(f"\n\nÚltimo error: {str(last_error)[:300]}")
        return "\n".join(parts)

    async def _transcribe_faster_whisper(self, file_path: str, language: str | None) -> str:
        """Transcribe localmente usando faster-whisper (sin API key, muy rápido)."""
        loop = asyncio.get_event_loop()

        def _run():
            model = self._load_faster_whisper()
            segments, info = model.transcribe(
                file_path,
                language=language,
                beam_size=5,
                vad_filter=True,          # Filtra silencios automáticamente
                vad_parameters={"min_silence_duration_ms": 500},
            )
            text = " ".join(seg.text.strip() for seg in segments)
            logger.info(
                "audio.fw_done",
                detected_lang=info.language,
                confidence=round(info.language_probability, 2),
            )
            return text

        return await loop.run_in_executor(_executor, _run)

    async def _transcribe_openai(self, file_path: str, language: str | None) -> str:
        """Transcribe usando OpenAI Whisper API."""
        with open(file_path, "rb") as f:
            audio_data = f.read()

        suffix = Path(file_path).suffix.lstrip(".") or "ogg"
        data = {"model": "whisper-1"}
        if language:
            data["language"] = language

        response = await self._client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {self._openai_key}"},
            files={"file": (Path(file_path).name, audio_data, _audio_content_type(suffix))},
            data=data,
        )
        response.raise_for_status()
        return response.json().get("text", "").strip()

    async def _transcribe_ollama(self, file_path: str, language: str | None) -> str:
        """Transcribe via Ollama /api/transcriptions (requiere Ollama >= 0.7 con soporte de audio)."""
        with open(file_path, "rb") as f:
            audio_data = f.read()

        suffix = Path(file_path).suffix.lstrip(".") or "ogg"
        data = {"model": self._ollama_model}
        if language:
            data["language"] = language

        response = await self._client.post(
            f"{self._ollama_url}/api/transcriptions",
            files={"file": (Path(file_path).name, audio_data, _audio_content_type(suffix))},
            data=data,
        )

        if response.status_code == 404:
            raise RuntimeError(
                "Ollama no soporta transcripción en esta versión, o el modelo no está disponible.\n"
                "Cambiá AUDIO_TRANSCRIPTION_PROVIDER=faster_whisper y ejecutá: pip install faster-whisper"
            )
        response.raise_for_status()
        return response.json().get("text", "").strip()


class _FasterWhisperNotInstalled(Exception):
    """Señal interna para indicar que faster-whisper no está instalado."""
    pass


def _audio_content_type(ext: str) -> str:
    mapping = {
        "ogg": "audio/ogg", "mp3": "audio/mpeg", "mp4": "audio/mp4",
        "m4a": "audio/mp4", "wav": "audio/wav", "webm": "audio/webm", "flac": "audio/flac",
    }
    return mapping.get(ext.lower(), "audio/ogg")
