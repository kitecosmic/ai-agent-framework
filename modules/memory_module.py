"""
Memory Module — Sistema de memoria persistente multi-capa.

Tres capas de memoria:

1. SHORT-TERM (Conversación)
   - Contexto de la conversación actual
   - Se mantiene en Redis para velocidad
   - TTL configurable (default 24h)

2. LONG-TERM (Usuario + Agente)
   - Datos del usuario: nombre, preferencias, historial de interacciones
   - Datos del agente: configuración aprendida, patrones detectados
   - Persistido en PostgreSQL

3. SEMANTIC (Conocimiento)
   - Embeddings de conversaciones y datos recopilados
   - Búsqueda por similitud para recordar cosas relevantes
   - Almacenado en PostgreSQL con pgvector (o vector store externo)

Cada usuario (tenant) tiene su propio espacio de memoria aislado.
"""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import structlog

from core.event_bus import Event, event_bus
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════════════

@dataclass
class UserProfile:
    """Perfil persistente de un usuario."""
    user_id: str
    tenant_id: str = "default"
    display_name: str = ""
    language: str = "es"
    timezone: str = "America/Argentina/Buenos_Aires"
    preferences: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    interaction_count: int = 0
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "language": self.language,
            "timezone": self.timezone,
            "preferences": self.preferences,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "interaction_count": self.interaction_count,
            "last_seen": self.last_seen.isoformat(),
        }


@dataclass
class MemoryEntry:
    """Una entrada individual de memoria."""
    id: str = field(default_factory=lambda: uuid4().hex[:16])
    user_id: str = ""
    tenant_id: str = "default"
    category: str = "general"        # "preference", "fact", "task", "interaction", etc.
    key: str = ""                     # Identificador legible
    content: str = ""                 # Contenido principal
    metadata: dict[str, Any] = field(default_factory=dict)
    importance: float = 0.5           # 0.0 - 1.0 (para ranking)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    embedding: list[float] | None = None  # Para búsqueda semántica

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "category": self.category,
            "key": self.key,
            "content": self.content,
            "metadata": self.metadata,
            "importance": self.importance,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass
class ConversationTurn:
    """Un turno de conversación (mensaje + respuesta)."""
    role: str               # "user" | "assistant"
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════
# Storage Backends (in-memory por ahora, swappeable a Redis/PG)
# ═══════════════════════════════════════════════════════════════════

class InMemoryStore:
    """
    Storage backend en memoria.
    En producción, reemplazar con RedisStore / PostgresStore.
    La interfaz es la misma, así que el swap es transparente.
    """

    def __init__(self):
        self._data: dict[str, Any] = {}

    async def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry and isinstance(entry, dict) and "expires_at" in entry:
            if entry["expires_at"] and datetime.fromisoformat(entry["expires_at"]) < datetime.now(timezone.utc):
                del self._data[key]
                return None
        return entry

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None):
        if isinstance(value, dict) and ttl_seconds:
            value["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        self._data[key] = value

    async def delete(self, key: str):
        self._data.pop(key, None)

    async def search(self, prefix: str) -> list[tuple[str, Any]]:
        return [(k, v) for k, v in self._data.items() if k.startswith(prefix)]

    async def keys(self, pattern: str = "*") -> list[str]:
        if pattern == "*":
            return list(self._data.keys())
        prefix = pattern.rstrip("*")
        return [k for k in self._data.keys() if k.startswith(prefix)]


# ═══════════════════════════════════════════════════════════════════
# Memory Module (Plugin)
# ═══════════════════════════════════════════════════════════════════

class MemoryModule(PluginBase):
    """
    Sistema de memoria completo para el agente.

    Eventos que escucha:
    - memory.store            → guardar un recuerdo
    - memory.recall           → buscar recuerdos
    - memory.user.get         → obtener perfil de usuario
    - memory.user.update      → actualizar perfil de usuario
    - memory.conversation.get → obtener historial de conversación
    - memory.analyze          → analizar conversación y extraer memorias

    Eventos que emite:
    - memory.stored           → recuerdo guardado
    - memory.user.updated     → perfil actualizado
    - memory.preference.learned → nueva preferencia detectada
    """

    name = "memory"
    version = "1.0.0"
    description = "Multi-layer memory system with user profiles and semantic search"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Stores separados por capa
        self._conversations: InMemoryStore = InMemoryStore()   # short-term
        self._profiles: InMemoryStore = InMemoryStore()        # user profiles
        self._memories: InMemoryStore = InMemoryStore()         # long-term facts
        self._agent_data: InMemoryStore = InMemoryStore()       # agent's own data

    async def on_load(self):
        logger.info("memory.loaded")

    # ─── Conversation (Short-Term) ───────────────────────────────

    @hook("memory.conversation.append")
    async def append_to_conversation(self, event: Event):
        """Agrega un turno a la conversación."""
        user_id = event.data.get("user_id", "unknown")
        tenant_id = event.data.get("tenant_id", "default")
        channel = event.data.get("channel", "default")
        role = event.data.get("role", "user")
        content = event.data.get("content", "")

        key = f"conv:{tenant_id}:{user_id}:{channel}"
        history = await self._conversations.get(key) or {"turns": []}

        history["turns"].append({
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": event.data.get("metadata", {}),
        })

        # Limitar a últimos 100 turnos
        if len(history["turns"]) > 100:
            history["turns"] = history["turns"][-100:]

        # TTL de 24 horas
        await self._conversations.set(key, history, ttl_seconds=86400)

    @hook("memory.conversation.get")
    async def get_conversation(self, event: Event) -> list[dict]:
        """Obtiene el historial de conversación."""
        user_id = event.data.get("user_id", "unknown")
        tenant_id = event.data.get("tenant_id", "default")
        channel = event.data.get("channel", "default")
        limit = event.data.get("limit", 50)

        key = f"conv:{tenant_id}:{user_id}:{channel}"
        history = await self._conversations.get(key) or {"turns": []}
        return history["turns"][-limit:]

    @hook("memory.conversation.clear")
    async def clear_conversation(self, event: Event):
        """Limpia la conversación de un canal."""
        user_id = event.data.get("user_id", "unknown")
        tenant_id = event.data.get("tenant_id", "default")
        channel = event.data.get("channel", "default")
        key = f"conv:{tenant_id}:{user_id}:{channel}"
        await self._conversations.delete(key)

    # ─── User Profiles ───────────────────────────────────────────

    @hook("memory.user.get")
    async def get_user_profile(self, event: Event) -> dict | None:
        """Obtiene o crea un perfil de usuario."""
        user_id = event.data.get("user_id", "")
        tenant_id = event.data.get("tenant_id", "default")

        key = f"user:{tenant_id}:{user_id}"
        profile = await self._profiles.get(key)

        if not profile:
            # Crear perfil nuevo
            profile = UserProfile(user_id=user_id, tenant_id=tenant_id).to_dict()
            await self._profiles.set(key, profile)

        # Actualizar last_seen
        profile["last_seen"] = datetime.now(timezone.utc).isoformat()
        profile["interaction_count"] = profile.get("interaction_count", 0) + 1
        await self._profiles.set(key, profile)

        return profile

    @hook("memory.user.update")
    async def update_user_profile(self, event: Event) -> dict:
        """Actualiza campos del perfil de un usuario."""
        user_id = event.data.get("user_id", "")
        tenant_id = event.data.get("tenant_id", "default")
        updates = event.data.get("updates", {})

        key = f"user:{tenant_id}:{user_id}"
        profile = await self._profiles.get(key)
        if not profile:
            profile = UserProfile(user_id=user_id, tenant_id=tenant_id).to_dict()

        # Aplicar updates
        for field_name, value in updates.items():
            if field_name in profile:
                if field_name == "preferences" and isinstance(value, dict):
                    profile["preferences"].update(value)
                elif field_name == "metadata" and isinstance(value, dict):
                    profile["metadata"].update(value)
                else:
                    profile[field_name] = value

        profile["updated_at"] = datetime.now(timezone.utc).isoformat()
        await self._profiles.set(key, profile)

        await self.bus.emit(Event(
            name="memory.user.updated",
            data={"user_id": user_id, "tenant_id": tenant_id, "updates": updates},
            source="memory",
        ))

        return profile

    @hook("memory.user.set_preference")
    async def set_user_preference(self, event: Event):
        """Shortcut para guardar una preferencia del usuario."""
        user_id = event.data.get("user_id", "")
        tenant_id = event.data.get("tenant_id", "default")
        pref_key = event.data.get("key", "")
        pref_value = event.data.get("value")

        await self.update_user_profile(Event(
            name="memory.user.update",
            data={
                "user_id": user_id,
                "tenant_id": tenant_id,
                "updates": {"preferences": {pref_key: pref_value}},
            },
        ))

        logger.info("memory.preference_set", user_id=user_id, key=pref_key)

    # ─── Long-Term Memory ────────────────────────────────────────

    @hook("memory.store")
    async def store_memory(self, event: Event) -> MemoryEntry:
        """
        Guarda un recuerdo en la memoria de largo plazo.

        Categorías sugeridas:
        - "preference"   → "Al usuario le gusta recibir noticias a las 8am"
        - "fact"         → "El usuario trabaja en marketing"
        - "task"         → "Configuré un monitor de precios para iPhone"
        - "interaction"  → "El usuario preguntó por restaurantes en Rosario"
        - "learned"      → "El dólar blue suele subir los viernes"
        """
        entry = MemoryEntry(
            user_id=event.data.get("user_id", ""),
            tenant_id=event.data.get("tenant_id", "default"),
            category=event.data.get("category", "general"),
            key=event.data.get("key", ""),
            content=event.data.get("content", ""),
            metadata=event.data.get("metadata", {}),
            importance=event.data.get("importance", 0.5),
        )

        # TTL opcional
        if "ttl_hours" in event.data:
            entry.expires_at = datetime.now(timezone.utc) + timedelta(hours=event.data["ttl_hours"])

        store_key = f"mem:{entry.tenant_id}:{entry.user_id}:{entry.id}"
        await self._memories.set(
            store_key,
            entry.to_dict(),
            ttl_seconds=int(event.data.get("ttl_hours", 0) * 3600) or None,
        )

        # También indexar por categoría para búsqueda rápida
        index_key = f"idx:{entry.tenant_id}:{entry.user_id}:{entry.category}"
        index = await self._memories.get(index_key) or {"ids": []}
        index["ids"].append(entry.id)
        await self._memories.set(index_key, index)

        await self.bus.emit(Event(
            name="memory.stored",
            data=entry.to_dict(),
            source="memory",
        ))

        logger.info("memory.stored", category=entry.category, key=entry.key)
        return entry

    @hook("memory.recall")
    async def recall_memories(self, event: Event) -> list[dict]:
        """
        Busca recuerdos por usuario y/o categoría.

        Parámetros:
        - user_id: filtrar por usuario
        - tenant_id: filtrar por tenant
        - category: filtrar por categoría
        - query: búsqueda por texto (substring match por ahora)
        - limit: máximo de resultados
        """
        user_id = event.data.get("user_id", "")
        tenant_id = event.data.get("tenant_id", "default")
        category = event.data.get("category")
        query = event.data.get("query", "").lower()
        limit = event.data.get("limit", 20)

        prefix = f"mem:{tenant_id}:{user_id}:"
        all_entries = await self._memories.search(prefix)

        results = []
        for _key, entry_data in all_entries:
            if not isinstance(entry_data, dict) or "content" not in entry_data:
                continue

            # Filtrar por categoría
            if category and entry_data.get("category") != category:
                continue

            # Filtrar por query (búsqueda simple)
            if query and query not in entry_data.get("content", "").lower() and query not in entry_data.get("key", "").lower():
                continue

            results.append(entry_data)

        # Ordenar por importancia
        results.sort(key=lambda x: x.get("importance", 0), reverse=True)

        return results[:limit]

    @hook("memory.forget")
    async def forget_memory(self, event: Event):
        """Elimina un recuerdo específico."""
        memory_id = event.data.get("id", "")
        tenant_id = event.data.get("tenant_id", "default")
        user_id = event.data.get("user_id", "")
        key = f"mem:{tenant_id}:{user_id}:{memory_id}"
        await self._memories.delete(key)

    # ─── Agent Data (el agente recuerda cosas sobre sí mismo) ────

    @hook("memory.agent.store")
    async def store_agent_data(self, event: Event):
        """
        El agente puede guardar datos propios.
        Ej: configuraciones aprendidas, estadísticas, etc.
        """
        tenant_id = event.data.get("tenant_id", "default")
        data_key = event.data.get("key", "")
        value = event.data.get("value")
        key = f"agent:{tenant_id}:{data_key}"
        await self._agent_data.set(key, {"value": value, "updated_at": datetime.now(timezone.utc).isoformat()})

    @hook("memory.agent.get")
    async def get_agent_data(self, event: Event) -> Any | None:
        """Recupera datos del agente."""
        tenant_id = event.data.get("tenant_id", "default")
        data_key = event.data.get("key", "")
        key = f"agent:{tenant_id}:{data_key}"
        entry = await self._agent_data.get(key)
        return entry.get("value") if entry else None

    # ─── Análisis automático de conversaciones ───────────────────

    @hook("memory.analyze")
    async def analyze_and_extract(self, event: Event) -> list[dict]:
        """
        Analiza una conversación y extrae memorias automáticamente.

        Esto se puede llamar al final de cada conversación para que
        el agente "aprenda" del usuario automáticamente.

        Usa el LLM para identificar:
        - Preferencias del usuario
        - Datos personales compartidos
        - Tareas recurrentes
        - Patrones de comportamiento
        """
        conversation = event.data.get("conversation", [])
        user_id = event.data.get("user_id", "")
        tenant_id = event.data.get("tenant_id", "default")

        if not conversation or len(conversation) < 2:
            return []

        # Construir prompt para el LLM
        conv_text = "\n".join(
            f"{'Usuario' if t.get('role') == 'user' else 'Agente'}: {t.get('content', '')}"
            for t in conversation[-20:]  # Últimos 20 turnos
        )

        analysis_prompt = f"""Analiza esta conversación y extrae información importante para recordar.

CONVERSACIÓN:
{conv_text}

Responde SOLO con un JSON array. Cada elemento debe tener:
- "category": "preference" | "fact" | "task" | "pattern"
- "key": identificador corto
- "content": descripción del recuerdo
- "importance": 0.0-1.0

Ejemplos:
[
  {{"category": "preference", "key": "horario_noticias", "content": "Prefiere recibir noticias a las 8am", "importance": 0.8}},
  {{"category": "fact", "key": "trabajo", "content": "Trabaja como diseñador freelance", "importance": 0.7}}
]

Si no hay nada relevante para recordar, responde: []"""

        # Emitir al LLM via el orchestrator o directamente
        # Por ahora retornamos placeholder — en producción esto llamaría al LLM
        logger.info("memory.analyze_requested", user_id=user_id, turns=len(conversation))

        return []

    # ─── Context builder para el Orchestrator ────────────────────

    @hook("memory.build_context")
    async def build_context(self, event: Event) -> dict:
        """
        Construye el contexto completo para una interacción.
        El Orchestrator llama esto antes de procesar cada mensaje.

        Retorna un dict con toda la info relevante del usuario
        para inyectar en el system prompt del LLM.
        """
        user_id = event.data.get("user_id", "")
        tenant_id = event.data.get("tenant_id", "default")
        channel = event.data.get("channel", "default")

        # 1. Perfil del usuario
        profile_results = await self.get_user_profile(Event(
            name="memory.user.get",
            data={"user_id": user_id, "tenant_id": tenant_id},
        ))

        # 2. Historial de conversación reciente
        conversation = await self.get_conversation(Event(
            name="memory.conversation.get",
            data={"user_id": user_id, "tenant_id": tenant_id, "channel": channel, "limit": 20},
        ))

        # 3. Memorias relevantes (las más importantes)
        memories = await self.recall_memories(Event(
            name="memory.recall",
            data={"user_id": user_id, "tenant_id": tenant_id, "limit": 10},
        ))

        # 4. Preferencias
        preferences = profile_results.get("preferences", {}) if profile_results else {}

        context = {
            "user_profile": profile_results,
            "conversation_history": conversation,
            "memories": memories,
            "preferences": preferences,
            "context_summary": self._build_summary(profile_results, memories),
        }

        return context

    def _build_summary(self, profile: dict | None, memories: list[dict]) -> str:
        """Construye un resumen en texto para inyectar en el prompt."""
        parts = []

        if profile:
            name = profile.get("display_name", "")
            if name:
                parts.append(f"Nombre del usuario: {name}")
            lang = profile.get("language", "")
            if lang:
                parts.append(f"Idioma preferido: {lang}")
            tz = profile.get("timezone", "")
            if tz:
                parts.append(f"Zona horaria: {tz}")

            prefs = profile.get("preferences", {})
            if prefs:
                pref_lines = [f"  - {k}: {v}" for k, v in prefs.items()]
                parts.append("Preferencias:\n" + "\n".join(pref_lines))

        if memories:
            mem_lines = [f"  - [{m.get('category', '?')}] {m.get('content', '')}" for m in memories[:10]]
            parts.append("Recuerdos relevantes:\n" + "\n".join(mem_lines))

        return "\n".join(parts) if parts else "(sin contexto previo)"

    # ─── Stats ───────────────────────────────────────────────────

    @hook("memory.stats")
    async def get_stats(self, event: Event) -> dict:
        """Estadísticas del sistema de memoria."""
        tenant_id = event.data.get("tenant_id", "default")

        profile_keys = await self._profiles.keys(f"user:{tenant_id}:*")
        memory_keys = await self._memories.keys(f"mem:{tenant_id}:*")
        conv_keys = await self._conversations.keys(f"conv:{tenant_id}:*")

        return {
            "tenant_id": tenant_id,
            "total_users": len(profile_keys),
            "total_memories": len(memory_keys),
            "active_conversations": len(conv_keys),
        }
