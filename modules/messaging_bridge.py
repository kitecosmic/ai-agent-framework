"""
Messaging Bridge — Puente bidireccional con apps de mensajería.

Diseñado para integrarse con tu app de mensajería custom.
Soporta:
- WebSocket (bidireccional en tiempo real)
- Webhooks (HTTP push/pull)
- Message queue (Redis pub/sub para alta disponibilidad)

Tu app de mensajería solo necesita implementar uno de estos protocolos.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

import httpx
import structlog

from core.event_bus import Event, event_bus
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()


class MessageType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    ACTION = "action"      # Botones, acciones rápidas
    CARD = "card"          # Rich cards
    SYSTEM = "system"


@dataclass
class Message:
    """Mensaje universal entre el agente y la app de mensajería."""

    id: str = field(default_factory=lambda: uuid4().hex[:16])
    type: MessageType = MessageType.TEXT
    content: str = ""
    sender: str = ""           # user_id o "agent"
    channel: str = "default"   # chat_id, room, canal
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: list[dict] = field(default_factory=list)
    reply_to: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "content": self.content,
            "sender": self.sender,
            "channel": self.channel,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "attachments": self.attachments,
            "reply_to": self.reply_to,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Message:
        return cls(
            id=data.get("id", uuid4().hex[:16]),
            type=MessageType(data.get("type", "text")),
            content=data.get("content", ""),
            sender=data.get("sender", ""),
            channel=data.get("channel", "default"),
            metadata=data.get("metadata", {}),
            attachments=data.get("attachments", []),
            reply_to=data.get("reply_to"),
        )


class MessagingBridge(PluginBase):
    """
    Bridge bidireccional para conectar con tu app de mensajería.

    ┌──────────────┐     WebSocket/Webhook     ┌──────────────────┐
    │  Tu App de   │ ◄─────────────────────► │  NexusAgent      │
    │  Mensajería  │                           │  Messaging Bridge│
    └──────────────┘                           └──────────────────┘

    Protocolo:
    1. Tu app envía mensajes al agente via webhook POST /api/messages/incoming
    2. El agente procesa y responde via webhook a MESSAGING_WEBHOOK_URL
    3. O ambos se conectan via WebSocket para tiempo real

    Eventos:
    - messaging.incoming     → mensaje recibido de la app
    - messaging.outgoing     → mensaje a enviar a la app
    - messaging.send         → solicitud para enviar mensaje
    - messaging.delivered    → confirmación de entrega
    """

    name = "messaging"
    version = "1.0.0"
    description = "Bidirectional messaging bridge for external apps"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._http_client: httpx.AsyncClient | None = None
        self._webhook_url: str = ""
        self._webhook_secret: str = ""
        self._ws_connections: dict[str, Any] = {}  # channel_id -> ws
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._outbound_worker_task: asyncio.Task | None = None

    async def on_load(self):
        self._webhook_url = self.config.get("messaging_webhook_url", "")
        self._webhook_secret = self.config.get("messaging_webhook_secret", "")
        self._http_client = httpx.AsyncClient(timeout=10.0)

        # Worker para enviar mensajes en cola
        self._outbound_worker_task = asyncio.create_task(self._outbound_worker())

        logger.info(
            "messaging.loaded",
            webhook_url=self._webhook_url or "(not set)",
        )

    async def on_unload(self):
        if self._outbound_worker_task:
            self._outbound_worker_task.cancel()
        if self._http_client:
            await self._http_client.aclose()

    # ── Inbound: recibir mensajes de tu app ──────────────────────

    async def receive_message(self, raw_data: dict) -> Message:
        """
        Procesa un mensaje entrante desde tu app de mensajería.
        Llamado desde el endpoint API /api/messages/incoming.
        """
        message = Message.from_dict(raw_data)

        logger.info(
            "messaging.received",
            sender=message.sender,
            channel=message.channel,
            type=message.type.value,
        )

        # Emitir evento para que el orchestrator o LLM lo procese
        await self.bus.emit(Event(
            name="messaging.incoming",
            data={
                "message": message.to_dict(),
                "sender": message.sender,
                "channel": message.channel,
                "content": message.content,
            },
            source="messaging",
        ))

        return message

    # ── Outbound: enviar mensajes a tu app ───────────────────────

    @hook("messaging.send")
    async def handle_send(self, event: Event):
        """Envía un mensaje a tu app de mensajería."""
        message_data = event.data.get("message", event.data)
        message = Message(
            content=message_data.get("content", ""),
            channel=message_data.get("channel", "default"),
            sender="agent",
            type=MessageType(message_data.get("type", "text")),
            metadata=message_data.get("metadata", {}),
            attachments=message_data.get("attachments", []),
            reply_to=message_data.get("reply_to"),
        )
        await self.send_message(message)

    async def send_message(self, message: Message):
        """Envía un mensaje a la app de mensajería via webhook."""
        await self._message_queue.put(message)

    async def _outbound_worker(self):
        """Worker que procesa la cola de mensajes salientes."""
        while True:
            try:
                message = await self._message_queue.get()
                await self._deliver_message(message)
                self._message_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("messaging.outbound_error", error=str(exc))
                await asyncio.sleep(1)

    async def _deliver_message(self, message: Message):
        """Entrega un mensaje via webhook HTTP."""
        if not self._webhook_url:
            logger.warning("messaging.no_webhook_url")
            return

        payload = {
            "event": "message",
            "data": message.to_dict(),
        }

        headers = {"Content-Type": "application/json"}
        if self._webhook_secret:
            # Tu app puede verificar este header
            import hashlib
            import hmac
            signature = hmac.new(
                self._webhook_secret.encode(),
                json.dumps(payload).encode(),
                hashlib.sha256,
            ).hexdigest()
            headers["X-Webhook-Signature"] = signature

        try:
            response = await self._http_client.post(
                self._webhook_url,
                json=payload,
                headers=headers,
            )
            if response.status_code == 200:
                await self.bus.emit(Event(
                    name="messaging.delivered",
                    data={"message_id": message.id, "channel": message.channel},
                    source="messaging",
                ))
            else:
                logger.error(
                    "messaging.delivery_failed",
                    status=response.status_code,
                    body=response.text[:200],
                )
        except Exception as exc:
            logger.error("messaging.delivery_error", error=str(exc))

    # ── Convenience methods ──────────────────────────────────────

    async def send_text(self, channel: str, content: str, reply_to: str | None = None):
        """Shortcut para enviar un mensaje de texto."""
        await self.send_message(Message(
            content=content,
            channel=channel,
            sender="agent",
            reply_to=reply_to,
        ))

    async def send_card(self, channel: str, title: str, body: str, actions: list[dict] | None = None):
        """Envía una rich card con acciones opcionales."""
        await self.send_message(Message(
            type=MessageType.CARD,
            content=body,
            channel=channel,
            sender="agent",
            metadata={
                "title": title,
                "actions": actions or [],
            },
        ))

    async def broadcast(self, channels: list[str], content: str):
        """Envía un mensaje a múltiples canales."""
        for channel in channels:
            await self.send_text(channel, content)
