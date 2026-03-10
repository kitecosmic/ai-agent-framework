"""
Telegram Bridge — Integración bidireccional con Telegram Bot API.

Usa polling (no requiere URL pública/ngrok) para desarrollo local.
Los mensajes entrantes se procesan via el orchestrator (LLM),
y las respuestas se envían de vuelta al chat de Telegram.

Coexiste con el MessagingBridge existente (webhooks) que se usará
para la app de mensajería propia.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import structlog
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from core.event_bus import Event, event_bus
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()

# ── /config wizard: pasos y validaciones ────────────────────────────
CONFIG_STEPS = [
    {
        "key": "ANTHROPIC_API_KEY",
        "label": "Anthropic API Key",
        "hint": "Empieza con sk-ant-...",
        "validate": lambda v: v.startswith("sk-ant-") and len(v) > 20,
        "mask": True,
    },
    {
        "key": "OPENAI_API_KEY",
        "label": "OpenAI API Key",
        "hint": "Empieza con sk-...",
        "validate": lambda v: v.startswith("sk-") and len(v) > 10,
        "mask": True,
    },
    {
        "key": "MINIMAX_API_KEY",
        "label": "MiniMax API Key",
        "hint": "Para usuarios con Coding Plan",
        "validate": lambda v: len(v) > 5,
        "mask": True,
    },
    {
        "key": "RAPIBASE_URL",
        "label": "RapiBase URL",
        "hint": "Ej: https://tuapp.rapibase.io",
        "validate": lambda v: v.startswith("http"),
        "mask": False,
    },
    {
        "key": "RAPIBASE_SERVICE_KEY",
        "label": "RapiBase Service Key",
        "hint": "Acceso total sin JWT (para el agente/backend)",
        "validate": lambda v: len(v) > 3,
        "mask": True,
    },
    {
        "key": "RAPIBASE_ANON_KEY",
        "label": "RapiBase Anon Key",
        "hint": "Acceso público (requiere JWT del usuario)",
        "validate": lambda v: len(v) > 3,
        "mask": True,
    },
    {
        "key": "DEFAULT_LLM_PROVIDER",
        "label": "Provider de IA por defecto",
        "hint": "ollama | anthropic | openai | minimax",
        "validate": lambda v: v in ("ollama", "anthropic", "openai", "minimax"),
        "mask": False,
    },
    {
        "key": "OLLAMA_MODEL",
        "label": "Modelo Ollama por defecto",
        "hint": "Ej: qwen3-coder:latest, deepseek-r1:14b",
        "validate": lambda v: len(v) > 2,
        "mask": False,
    },
]


def _update_env_value(key: str, value: str) -> bool:
    """Actualiza o agrega un valor en .env de forma segura."""
    env_path = Path(".env")
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return True

    content = env_path.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    updated = False
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}="):
            new_lines.append(f"{key}={value}\n")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"\n{key}={value}\n")

    env_path.write_text("".join(new_lines), encoding="utf-8")

    # Invalidar cache de settings
    try:
        from config.settings import get_settings
        get_settings.cache_clear()
    except Exception:
        pass

    return True


def _mask_value(value: str) -> str:
    """Enmascara un valor sensible para mostrar al usuario."""
    if len(value) <= 8:
        return "****"
    return value[:4] + "..." + value[-4:]


class TelegramBridge(PluginBase):
    """
    Bridge bidireccional para Telegram.

    Flujo:
    1. Usuario envía mensaje en Telegram
    2. TelegramBridge recibe via polling
    3. Emite evento messaging.incoming → Orchestrator lo procesa con LLM
    4. Orchestrator emite messaging.send con la respuesta
    5. TelegramBridge envía la respuesta al chat de Telegram

    Comandos:
    /start  → Mensaje de bienvenida
    /help   → Lista de capacidades
    /status → Estado del agente
    """

    name = "telegram"
    version = "1.0.0"
    description = "Telegram Bot integration via polling"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._app: Application | None = None
        self._token: str = ""
        self._polling_task: asyncio.Task | None = None
        self._chat_contexts: dict[int, list[dict]] = {}  # chat_id -> conversation
        self._admin_ids: list[int] = []
        self._config_sessions: dict[int, dict] = {}  # chat_id -> {step_key, step_index}

    async def on_load(self):
        self._token = self.config.get("telegram_bot_token", "")
        if not self._token:
            logger.warning("telegram.no_token", hint="Set TELEGRAM_BOT_TOKEN in .env")
            return

        # Cargar admin IDs
        from config.settings import get_settings
        s = get_settings()
        self._admin_ids = s.get_admin_chat_ids()
        if not self._admin_ids:
            logger.warning("telegram.no_admin_ids", hint="Agregá ADMIN_CHAT_IDS en .env para usar /config")

        # Construir la app de Telegram
        self._app = (
            Application.builder()
            .token(self._token)
            .build()
        )

        # Registrar handlers de Telegram
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("jobs", self._cmd_jobs))
        self._app.add_handler(CommandHandler("removejob", self._cmd_removejob))
        self._app.add_handler(CommandHandler("config", self._cmd_config))
        self._app.add_handler(CommandHandler("trust", self._cmd_trust))
        self._app.add_handler(CommandHandler("model", self._cmd_model))
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        # Voz y audio
        self._app.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self._handle_voice)
        )
        # Fotos e imágenes
        self._app.add_handler(
            MessageHandler(filters.PHOTO, self._handle_photo)
        )

        # Iniciar polling en background
        self._polling_task = asyncio.create_task(self._run_polling())

        logger.info("telegram.loaded", bot_token=f"...{self._token[-6:]}", admins=self._admin_ids)

    async def on_unload(self):
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        if self._app:
            try:
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                pass

    async def _run_polling(self):
        """Inicia el polling de Telegram en background."""
        try:
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )
            logger.info("telegram.polling_started")
            # Mantener corriendo hasta cancelación
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("telegram.polling_stopped")
            raise
        except Exception as exc:
            logger.error("telegram.polling_error", error=str(exc))

    # ── Telegram Command Handlers ────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start — bienvenida."""
        await update.message.reply_text(
            "🤖 ¡Hola! Soy *NexusAgent*, tu asistente de IA.\n\n"
            "Puedo ayudarte con:\n"
            "• Responder preguntas usando IA\n"
            "• Navegar y extraer datos de la web\n"
            "• Hacer requests HTTP a APIs\n"
            "• Programar tareas automáticas\n"
            "• Monitorear precios de productos\n\n"
            "Simplemente escríbeme lo que necesites.\n"
            "Usa /help para más info.",
            parse_mode="Markdown",
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /help — capacidades."""
        chat_id = update.effective_chat.id
        is_admin = self._is_admin(chat_id)

        lines = [
            "📋 *Comandos disponibles:*\n",
            "/start — Mensaje de bienvenida",
            "/help — Esta ayuda",
            "/status — Estado del agente",
            "/model — Ver modelos de IA configurados",
            "/jobs — Ver tareas programadas",
            "/removejob <id> — Eliminar tarea programada",
        ]

        if is_admin:
            lines += [
                "",
                "🔧 *Comandos de administrador:*",
                "/config — Configurar API keys y opciones (sin tocar la terminal)",
                "/trust on|off — Activar/desactivar auto-aprobación de comandos del sistema",
            ]

        lines += [
            "",
            "💡 *Capacidades:*",
            "• Responde preguntas y razona con IA",
            "• Navega y extrae datos de la web",
            "• Entiende fotos e imágenes",
            "• Transcribe mensajes de voz a texto",
            "• Crea y gestiona bases de datos con RapiBase",
            "• Programa tareas automáticas",
            "• Ejecuta comandos del sistema",
            "• Crea subdominios en Cloudflare",
            "",
            "_Podés agregar nuevas habilidades creando plugins Python en la carpeta plugins/_",
        ]

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /status — estado del agente."""
        from core.plugin_base import PluginRegistry
        plugins_count = len(self.bus._handlers)
        await update.message.reply_text(
            f"✅ *Estado del Agente*\n\n"
            f"• Bot: Online\n"
            f"• Eventos registrados: {plugins_count}\n"
            f"• Chat ID: `{update.effective_chat.id}`",
            parse_mode="Markdown",
        )

    async def _cmd_jobs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /jobs — lista tareas programadas."""
        try:
            results = await event_bus.emit(Event(
                name="scheduler.list_jobs",
                data={},
                source="telegram",
            ))
            jobs = results[0] if results and results[0] else []
        except Exception as exc:
            logger.error("telegram.jobs_error", error=str(exc))
            jobs = []

        if not jobs:
            await update.message.reply_text("📋 No hay tareas programadas.")
            return

        lines = ["📋 *Tareas programadas:*\n"]
        for j in jobs:
            status = "✅" if j.get("enabled", True) else "⏸️"
            next_run = j.get("next_run", "—")
            lines.append(
                f"{status} *{j['name']}*\n"
                f"   ID: `{j['id']}`\n"
                f"   Tipo: {j['trigger_type']}\n"
                f"   Próxima: {next_run}\n"
            )
        lines.append("\nPara eliminar: /removejob <id>")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_removejob(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /removejob <id> — elimina una tarea programada."""
        if not context.args:
            await update.message.reply_text(
                "⚠️ Uso: /removejob <id>\n\nUsa /jobs para ver los IDs disponibles."
            )
            return

        job_id = context.args[0]
        try:
            await event_bus.emit(Event(
                name="scheduler.remove_job",
                data={"id": job_id},
                source="telegram",
            ))
            await update.message.reply_text(f"🗑️ Tarea `{job_id}` eliminada.", parse_mode="Markdown")
            logger.info("telegram.job_removed", job_id=job_id)
        except Exception as exc:
            await update.message.reply_text(f"❌ Error eliminando tarea: {str(exc)}")
            logger.error("telegram.removejob_error", job_id=job_id, error=str(exc))

    # ── /config wizard (solo admins) ─────────────────────────────

    def _is_admin(self, chat_id: int) -> bool:
        return chat_id in self._admin_ids

    async def _cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Wizard de configuración seguro — solo para admins."""
        chat_id = update.effective_chat.id

        if not self._is_admin(chat_id):
            await update.message.reply_text(
                "🔒 Este comando es solo para administradores.\n\n"
                "Para habilitarte, tu chat ID debe estar en `ADMIN_CHAT_IDS` del `.env`.\n"
                f"Tu chat ID es: `{chat_id}`",
                parse_mode="Markdown",
            )
            return

        # Mostrar menú de configuración con botones
        keyboard = []
        for i, step in enumerate(CONFIG_STEPS):
            keyboard.append([InlineKeyboardButton(
                f"⚙️ {step['label']}", callback_data=f"cfg:{i}"
            )])
        keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="cfg:cancel")])

        await update.message.reply_text(
            "🛠️ *Configuración del Agente*\n\n"
            "Seleccioná qué querés configurar:\n"
            "_(Los valores se guardan en `.env` automáticamente)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los botones del wizard de configuración."""
        query = update.callback_query
        chat_id = query.from_user.id
        await query.answer()

        if not query.data.startswith("cfg:"):
            return

        data = query.data[4:]

        if data == "cancel":
            self._config_sessions.pop(chat_id, None)
            await query.edit_message_text("❌ Configuración cancelada.")
            return

        try:
            step_idx = int(data)
        except ValueError:
            return

        if step_idx < 0 or step_idx >= len(CONFIG_STEPS):
            return

        step = CONFIG_STEPS[step_idx]
        self._config_sessions[chat_id] = {"step_idx": step_idx, "step": step}

        await query.edit_message_text(
            f"⚙️ *{step['label']}*\n\n"
            f"_{step['hint']}_\n\n"
            f"Enviá el nuevo valor (o /cancelconfig para cancelar):",
            parse_mode="Markdown",
        )

    async def _cmd_config_value(self, chat_id: int, text: str) -> bool:
        """
        Procesa el valor enviado durante el wizard /config.
        Retorna True si estaba en sesión de config, False si no.
        """
        if chat_id not in self._config_sessions:
            return False

        session = self._config_sessions.pop(chat_id)
        step = session["step"]

        # Validar formato del valor
        value = text.strip()
        if step["validate"] and not step["validate"](value):
            await self._send_long_message(
                chat_id,
                f"❌ Valor inválido para *{step['label']}*.\n"
                f"Formato esperado: {step['hint']}\n\n"
                f"Usá /config para intentarlo de nuevo.",
            )
            return True

        # Guardar en .env
        try:
            _update_env_value(step["key"], value)
            display = _mask_value(value) if step["mask"] else value
            await self._send_long_message(
                chat_id,
                f"✅ *{step['label']}* guardado: `{display}`\n\n"
                f"Usá /config para configurar otro valor.\n"
                f"_El agente usará el nuevo valor en el próximo reinicio._",
            )
            logger.info("telegram.config_saved", key=step["key"], admin=chat_id)
        except Exception as exc:
            await self._send_long_message(chat_id, f"❌ Error guardando: {exc}")

        return True

    # ── /trust command ───────────────────────────────────────────

    async def _cmd_trust(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Activa/desactiva el modo de confianza para comandos del sistema.
        Con trust ON, el agente ejecuta rm y comandos peligrosos sin pedir confirmación.
        Con trust OFF (default), siempre pide confirmación.
        """
        chat_id = update.effective_chat.id
        if not self._is_admin(chat_id):
            await update.message.reply_text("🔒 Solo para administradores.")
            return

        channel = f"telegram:{chat_id}"
        args = context.args or []
        mode = args[0].lower() if args else None

        if mode == "off":
            enable = False
        elif mode == "on":
            enable = True
        else:
            # Toggle
            results = await event_bus.emit(Event(
                name="system.set_trust",
                data={"channel": channel, "enable": True},
                source="telegram",
            ))
            # Verificar estado actual via toggle inverso — simplemente toggle
            # (leer del orchestrator es complejo, así que usamos el estado del mensaje)
            await update.message.reply_text(
                "⚠️ *Modo Trust — Comandos del sistema*\n\n"
                "Usá:\n"
                "• `/trust on` — El agente ejecuta comandos `rm` y operaciones peligrosas *sin pedir confirmación*\n"
                "• `/trust off` — Vuelve a pedir confirmación (recomendado)\n\n"
                "Estado actual: ver con `/status`",
                parse_mode="Markdown",
            )
            return

        results = await event_bus.emit(Event(
            name="system.set_trust",
            data={"channel": channel, "enable": enable},
            source="telegram",
        ))

        status = "activado" if enable else "desactivado"
        icon = "🔓" if enable else "🔒"
        warning = "\n\n⚠️ *CUIDADO*: El agente ejecutará comandos destructivos sin pedir confirmación." if enable else ""

        await update.message.reply_text(
            f"{icon} *Trust mode {status}*{warning}",
            parse_mode="Markdown",
        )
        logger.info("telegram.trust_changed", chat_id=chat_id, enabled=enable)

    # ── /model command ───────────────────────────────────────────

    async def _cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Muestra los modelos configurados y cómo cambiarlos."""
        from config.settings import get_settings
        s = get_settings()

        lines = [
            "🤖 *Modelos configurados*\n",
            f"• **Default**: `{s.ollama_model}` (provider: {s.default_llm_provider})",
            f"• **Visión**: `{s.ollama_vision_model}`",
            f"• **Código**: `{s.ollama_coding_model}`",
            f"• **Razonamiento**: `{s.ollama_reasoning_model}`",
            f"• **OCR**: `{s.ollama_ocr_model}`",
            f"• **Rápido**: `{s.ollama_fast_model}`",
            "",
            "_El agente elige automáticamente según la tarea._",
            "Para cambiar: usá /config o edita `.env` directamente.",
        ]

        if self._is_admin(update.effective_chat.id):
            keyboard = [[InlineKeyboardButton("⚙️ Cambiar modelos", callback_data="cfg:6")]]
            await update.message.reply_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # ── Message Handler ──────────────────────────────────────────

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Procesa mensajes de texto del usuario."""
        chat_id = update.effective_chat.id
        user = update.effective_user
        text = update.message.text

        logger.info(
            "telegram.message_received",
            chat_id=chat_id,
            user=user.username or user.first_name,
            text_length=len(text),
        )

        # Verificar si estamos en el wizard /config (tiene prioridad)
        if chat_id in self._config_sessions:
            await self._cmd_config_value(chat_id, text)
            return

        # Indicar que estamos procesando
        await update.message.chat.send_action("typing")

        try:
            # Ejecutar tarea via el orchestrator
            results = await event_bus.emit(Event(
                name="task.execute",
                data={
                    "instruction": text,
                    "channel": f"telegram:{chat_id}",
                },
                source="telegram",
            ))

            if results and results[0]:
                result = results[0]
                response_text = result.response if result.success else f"❌ Error: {result.error}"
            else:
                response_text = "⚠️ No pude procesar tu mensaje. Intenta de nuevo."

        except Exception as exc:
            logger.error("telegram.process_error", error=str(exc), chat_id=chat_id)
            response_text = f"❌ Error interno: {str(exc)[:200]}"

        # Enviar respuesta — dividir si es muy larga (Telegram max 4096 chars)
        await self._send_long_message(update.message.chat_id, response_text)

    @staticmethod
    def _to_telegram_html(text: str) -> str:
        """Convierte texto con formato markdown ligero a HTML de Telegram."""
        # 1. Escapar caracteres HTML especiales (antes de agregar tags)
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")

        # 2. Convertir **negrita** a <b>negrita</b>
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

        # 3. Convertir _cursiva_ a <i>cursiva</i> (solo _ simple, no __)
        text = re.sub(r"(?<!_)_([^_]+?)_(?!_)", r"<i>\1</i>", text)

        # 4. Convertir `código` a <code>código</code>
        text = re.sub(r"`([^`]+?)`", r"<code>\1</code>", text)

        return text

    async def _send_long_message(self, chat_id: int, text: str, max_length: int = 4000):
        """Envía un mensaje largo dividiéndolo en chunks si es necesario."""
        if not self._app:
            return

        # Convertir formato markdown a HTML de Telegram
        html_text = self._to_telegram_html(text)

        if len(html_text) <= max_length:
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=html_text, parse_mode="HTML")
            except Exception:
                # Fallback sin HTML si hay error de parseo
                await self._app.bot.send_message(chat_id=chat_id, text=text)
            return

        # Dividir en chunks respetando líneas
        chunks = []
        current = ""
        for line in html_text.split("\n"):
            if len(current) + len(line) + 1 > max_length:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)

        for chunk in chunks:
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")
            except Exception:
                await self._app.bot.send_message(chat_id=chat_id, text=chunk)
            await asyncio.sleep(0.3)  # Rate limiting

    # ── Voice Handler ─────────────────────────────────────────────

    async def _handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Procesa mensajes de voz: transcribe y procesa como texto."""
        chat_id = update.effective_chat.id
        user = update.effective_user

        await update.message.chat.send_action("typing")

        voice = update.message.voice or update.message.audio
        if not voice:
            await update.message.reply_text("⚠️ No pude procesar el audio.")
            return

        logger.info("telegram.voice_received", chat_id=chat_id, duration=getattr(voice, "duration", 0))

        try:
            # Descargar archivo de voz
            voice_file = await context.bot.get_file(voice.file_id)
            import io
            buf = io.BytesIO()
            await voice_file.download_to_memory(buf)
            audio_bytes = buf.getvalue()

            file_name = getattr(voice, "file_name", None) or "audio.ogg"

            # Transcribir via AudioModule
            results = await event_bus.emit(Event(
                name="audio.transcribe",
                data={
                    "audio_bytes": audio_bytes,
                    "file_name": file_name,
                    "language": "es",
                },
                source="telegram",
            ))

            transcription_result = results[0] if results else None

            if not transcription_result or not transcription_result.get("success"):
                error = transcription_result.get("error", "desconocido") if transcription_result else "sin respuesta"
                await update.message.reply_text(f"❌ Error transcribiendo audio: {error}")
                return

            text = transcription_result["text"].strip()
            if not text:
                await update.message.reply_text("⚠️ No pude entender el audio. Intenta de nuevo.")
                return

            logger.info("telegram.voice_transcribed", chat_id=chat_id, text_len=len(text))

            # Indicar al usuario qué se entendió
            await update.message.reply_text(f'🎙️ Transcripción: "{text}"', parse_mode="HTML")

            # Procesar el texto transcripto como si fuera un mensaje normal
            task_results = await event_bus.emit(Event(
                name="task.execute",
                data={
                    "instruction": text,
                    "channel": f"telegram:{chat_id}",
                },
                source="telegram",
            ))

            if task_results and task_results[0]:
                result = task_results[0]
                response_text = result.response if result.success else f"❌ Error: {result.error}"
            else:
                response_text = "⚠️ No pude procesar tu mensaje. Intenta de nuevo."

            await self._send_long_message(chat_id, response_text)

        except Exception as exc:
            logger.error("telegram.voice_error", error=str(exc), chat_id=chat_id)
            await update.message.reply_text(f"❌ Error procesando audio: {str(exc)[:200]}")

    # ── Photo Handler ─────────────────────────────────────────────

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Procesa fotos e imágenes enviadas por el usuario."""
        chat_id = update.effective_chat.id
        user = update.effective_user
        caption = update.message.caption or ""

        await update.message.chat.send_action("typing")

        logger.info("telegram.photo_received", chat_id=chat_id, has_caption=bool(caption))

        try:
            # Obtener la foto en mayor resolución
            photo = update.message.photo[-1]
            photo_file = await context.bot.get_file(photo.file_id)

            import io
            import base64
            buf = io.BytesIO()
            await photo_file.download_to_memory(buf)
            image_bytes = buf.getvalue()
            image_b64 = base64.b64encode(image_bytes).decode("utf-8")

            # Emitir evento para que el orchestrator procese la imagen
            instruction = caption if caption else "Describe y analiza esta imagen en detalle."

            task_results = await event_bus.emit(Event(
                name="task.execute",
                data={
                    "instruction": instruction,
                    "channel": f"telegram:{chat_id}",
                    "image_b64": image_b64,
                    "image_mime": "image/jpeg",
                },
                source="telegram",
            ))

            if task_results and task_results[0]:
                result = task_results[0]
                response_text = result.response if result.success else f"❌ Error: {result.error}"
            else:
                response_text = "⚠️ No pude analizar la imagen. Intenta de nuevo."

            await self._send_long_message(chat_id, response_text)

        except Exception as exc:
            logger.error("telegram.photo_error", error=str(exc), chat_id=chat_id)
            await update.message.reply_text(f"❌ Error procesando imagen: {str(exc)[:200]}")

    # ── Event Handler: enviar mensajes a Telegram desde otros módulos ─

    @hook("telegram.send")
    async def handle_send(self, event_obj: Event):
        """Permite a otros módulos enviar mensajes a Telegram."""
        if not self._app:
            logger.warning("telegram.not_initialized")
            return

        chat_id = event_obj.data.get("chat_id")
        text = event_obj.data.get("content", event_obj.data.get("text", ""))

        if not chat_id or not text:
            logger.warning("telegram.send_missing_data", data=event_obj.data)
            return

        await self._send_long_message(int(chat_id), text)
        logger.info("telegram.message_sent", chat_id=chat_id)
