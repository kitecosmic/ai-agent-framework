"""
Event Bus — Sistema de eventos async para comunicación entre módulos.

Permite a los plugins suscribirse a eventos y emitir los suyos propios,
creando un sistema desacoplado y extensible.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine
from uuid import uuid4

import structlog

logger = structlog.get_logger()

# Type alias para handlers
EventHandler = Callable[["Event"], Coroutine[Any, Any, Any]]


@dataclass
class Event:
    """Unidad básica de comunicación entre módulos."""

    name: str
    data: dict[str, Any] = field(default_factory=dict)
    source: str = "system"
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def reply(self, data: dict[str, Any]) -> Event:
        """Crea un evento de respuesta vinculado a este."""
        return Event(
            name=f"{self.name}.reply",
            data=data,
            source=self.source,
            metadata={"reply_to": self.id},
        )


class EventBus:
    """
    Bus de eventos asíncrono con soporte para:
    - Suscripción por nombre exacto o wildcard (e.g., "browser.*")
    - Prioridades de handlers
    - Middleware chain
    - Historial de eventos recientes
    """

    def __init__(self, history_size: int = 1000):
        self._handlers: dict[str, list[tuple[int, EventHandler]]] = defaultdict(list)
        self._middlewares: list[Callable] = []
        self._history: list[Event] = []
        self._history_size = history_size
        self._lock = asyncio.Lock()

    def on(self, event_name: str, priority: int = 0) -> Callable:
        """Decorador para registrar un handler."""

        def decorator(fn: EventHandler) -> EventHandler:
            self._handlers[event_name].append((priority, fn))
            # Mantener ordenado por prioridad (mayor = primero)
            self._handlers[event_name].sort(key=lambda x: -x[0])
            return fn

        return decorator

    def subscribe(self, event_name: str, handler: EventHandler, priority: int = 0):
        """Registrar un handler programáticamente."""
        self._handlers[event_name].append((priority, handler))
        self._handlers[event_name].sort(key=lambda x: -x[0])

    def unsubscribe(self, event_name: str, handler: EventHandler):
        """Remover un handler."""
        self._handlers[event_name] = [
            (p, h) for p, h in self._handlers[event_name] if h is not handler
        ]

    def add_middleware(self, middleware: Callable):
        """Agrega middleware que procesa eventos antes de llegar a handlers."""
        self._middlewares.append(middleware)

    async def emit(self, event: Event | str, data: dict[str, Any] | None = None, source: str = "system") -> list[Any]:
        """
        Emite un evento y ejecuta todos los handlers suscritos.
        Retorna lista de resultados de cada handler.
        """
        if isinstance(event, str):
            event = Event(name=event, data=data or {}, source=source)

        # Aplicar middlewares
        for mw in self._middlewares:
            event = await mw(event) if asyncio.iscoroutinefunction(mw) else mw(event)
            if event is None:
                return []  # Middleware canceló el evento

        # Guardar en historial
        async with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_size:
                self._history = self._history[-self._history_size:]

        # Encontrar handlers que matchean
        matching_handlers = self._find_handlers(event.name)

        if not matching_handlers:
            logger.debug("event.no_handlers", event_name=event.name)
            return []

        # Ejecutar handlers
        results = []
        for _priority, handler in matching_handlers:
            try:
                result = await handler(event)
                results.append(result)
            except Exception as exc:
                logger.error(
                    "event.handler_error",
                    event_name=event.name,
                    handler=handler.__qualname__,
                    error=str(exc),
                )
                results.append(None)

        logger.info(
            "event.emitted",
            event_name=event.name,
            handlers=len(matching_handlers),
            source=event.source,
        )
        return results

    def _find_handlers(self, event_name: str) -> list[tuple[int, EventHandler]]:
        """Encuentra handlers por nombre exacto y wildcards."""
        handlers = list(self._handlers.get(event_name, []))

        # Wildcard matching: "browser.*" matchea "browser.navigate"
        parts = event_name.split(".")
        for i in range(len(parts)):
            wildcard = ".".join(parts[: i + 1]) + ".*"
            handlers.extend(self._handlers.get(wildcard, []))

        # Handler universal "*"
        handlers.extend(self._handlers.get("*", []))

        handlers.sort(key=lambda x: -x[0])
        return handlers

    def get_history(self, event_name: str | None = None, limit: int = 50) -> list[Event]:
        """Obtiene eventos recientes, opcionalmente filtrados."""
        events = self._history
        if event_name:
            events = [e for e in events if e.name == event_name]
        return events[-limit:]

    @property
    def registered_events(self) -> list[str]:
        return list(self._handlers.keys())


# Instancia global del event bus
event_bus = EventBus()
