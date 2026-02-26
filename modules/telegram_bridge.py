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
import re
from typing import Any

import structlog
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from core.event_bus import Event, event_bus
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()


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

    async def on_load(self):
        self._token = self.config.get("telegram_bot_token", "")
        if not self._token:
            logger.warning("telegram.no_token", hint="Set TELEGRAM_BOT_TOKEN in .env")
            return

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
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        # Iniciar polling en background
        self._polling_task = asyncio.create_task(self._run_polling())

        logger.info("telegram.loaded", bot_token=f"...{self._token[-6:]}")

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
        await update.message.reply_text(
            "📋 *Comandos disponibles:*\n\n"
            "/start — Mensaje de bienvenida\n"
            "/help — Esta ayuda\n"
            "/status — Estado del agente\n"
            "/jobs — Ver tareas programadas\n"
            "/removejob <id> — Eliminar tarea programada\n\n"
            "📝 *Ejemplos de uso:*\n\n"
            '• "Busca información sobre Python 3.12"\n'
            '• "¿Cuál es el clima en Buenos Aires?"\n'
            '• "Monitorea el precio de este producto: [URL]"\n'
            '• "Haz un request GET a https://api.example.com/data"\n\n'
            "Simplemente escribe tu consulta en lenguaje natural.",
            parse_mode="Markdown",
        )

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
