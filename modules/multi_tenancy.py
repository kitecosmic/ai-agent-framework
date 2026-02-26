"""
Multi-Tenancy Module — Aislamiento de datos y auth para SaaS.

Conceptos:
- Tenant: una organización/empresa/cuenta que usa tu SaaS
- User: un usuario dentro de un tenant
- Plan: nivel de servicio (free, pro, enterprise)

Cada tenant tiene:
- Sus propios datos aislados (memorias, conversaciones, jobs)
- Su propia configuración de LLM (pueden traer su propia API key)
- Límites según su plan (mensajes/día, jobs, browsers concurrentes)
- Sus propios plugins habilitados

Flujo de autenticación:
1. Tu app de mensajería envía un JWT o API key con cada request
2. Este módulo valida y extrae tenant_id + user_id
3. Inyecta el contexto en cada evento del bus
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any

import structlog

from core.event_bus import Event, event_bus
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════
# Plans & Limits
# ═══════════════════════════════════════════════════════════════════

class PlanTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


@dataclass
class PlanLimits:
    messages_per_day: int = 50
    scheduled_jobs: int = 3
    browser_sessions_per_day: int = 10
    http_requests_per_day: int = 100
    memory_entries: int = 500
    custom_plugins: int = 0
    llm_tokens_per_day: int = 50_000
    websocket_connections: int = 1
    file_storage_mb: int = 100


PLAN_LIMITS: dict[PlanTier, PlanLimits] = {
    PlanTier.FREE: PlanLimits(
        messages_per_day=50,
        scheduled_jobs=3,
        browser_sessions_per_day=10,
        http_requests_per_day=100,
        memory_entries=500,
        custom_plugins=0,
        llm_tokens_per_day=50_000,
        websocket_connections=1,
        file_storage_mb=100,
    ),
    PlanTier.PRO: PlanLimits(
        messages_per_day=1_000,
        scheduled_jobs=25,
        browser_sessions_per_day=200,
        http_requests_per_day=5_000,
        memory_entries=10_000,
        custom_plugins=10,
        llm_tokens_per_day=500_000,
        websocket_connections=5,
        file_storage_mb=2_000,
    ),
    PlanTier.ENTERPRISE: PlanLimits(
        messages_per_day=999_999,
        scheduled_jobs=999,
        browser_sessions_per_day=999_999,
        http_requests_per_day=999_999,
        memory_entries=999_999,
        custom_plugins=999,
        llm_tokens_per_day=9_999_999,
        websocket_connections=50,
        file_storage_mb=50_000,
    ),
}


# ═══════════════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Tenant:
    """Una cuenta/organización en el SaaS."""
    id: str
    name: str
    plan: PlanTier = PlanTier.FREE
    api_key: str = field(default_factory=lambda: f"nxa_{secrets.token_urlsafe(32)}")
    webhook_secret: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True
    settings: dict[str, Any] = field(default_factory=dict)
    # Pueden traer sus propias API keys de LLM
    custom_llm_keys: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "plan": self.plan.value,
            "api_key": self.api_key,
            "created_at": self.created_at.isoformat(),
            "is_active": self.is_active,
            "settings": self.settings,
            "limits": self.get_limits().__dict__,
        }

    def get_limits(self) -> PlanLimits:
        return PLAN_LIMITS.get(self.plan, PLAN_LIMITS[PlanTier.FREE])


@dataclass
class UsageCounter:
    """Contador de uso diario por tenant."""
    tenant_id: str
    date: str  # "2026-02-24"
    messages: int = 0
    browser_sessions: int = 0
    http_requests: int = 0
    llm_tokens: int = 0
    scheduled_jobs_created: int = 0

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "date": self.date,
            "messages": self.messages,
            "browser_sessions": self.browser_sessions,
            "http_requests": self.http_requests,
            "llm_tokens": self.llm_tokens,
        }


# ═══════════════════════════════════════════════════════════════════
# Multi-Tenancy Module
# ═══════════════════════════════════════════════════════════════════

class MultiTenancyModule(PluginBase):
    """
    Gestión de tenants, autenticación y rate limiting para SaaS.

    Eventos:
    - auth.validate           → validar un request entrante
    - tenant.create           → crear un tenant nuevo
    - tenant.get              → obtener info de un tenant
    - tenant.update           → actualizar tenant
    - usage.check             → verificar si una acción está dentro del límite
    - usage.increment         → incrementar un contador
    - usage.get               → obtener uso actual
    """

    name = "multi_tenancy"
    version = "1.0.0"
    description = "Multi-tenant authentication, authorization, and usage limits"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tenants: dict[str, Tenant] = {}
        self._api_key_index: dict[str, str] = {}  # api_key -> tenant_id
        self._usage: dict[str, UsageCounter] = {}  # "tenant_id:date" -> counter

    async def on_load(self):
        # Crear tenant default para desarrollo
        default_tenant = Tenant(
            id="default",
            name="Development",
            plan=PlanTier.ENTERPRISE,  # Sin límites en dev
        )
        self._tenants["default"] = default_tenant
        self._api_key_index[default_tenant.api_key] = "default"
        logger.info("multi_tenancy.loaded", default_api_key=default_tenant.api_key)

    # ─── Authentication ──────────────────────────────────────────

    @hook("auth.validate")
    async def validate_request(self, event: Event) -> dict | None:
        """
        Valida un request y retorna el contexto del tenant/user.

        Acepta:
        - api_key: header "Authorization: Bearer nxa_..."
        - webhook_signature: para validar webhooks
        """
        api_key = event.data.get("api_key", "")
        webhook_signature = event.data.get("webhook_signature", "")
        webhook_body = event.data.get("webhook_body", "")

        # Auth por API key
        if api_key:
            tenant_id = self._api_key_index.get(api_key)
            if not tenant_id or tenant_id not in self._tenants:
                logger.warning("auth.invalid_key")
                return None

            tenant = self._tenants[tenant_id]
            if not tenant.is_active:
                logger.warning("auth.inactive_tenant", tenant_id=tenant_id)
                return None

            return {
                "tenant_id": tenant.id,
                "tenant_name": tenant.name,
                "plan": tenant.plan.value,
                "limits": tenant.get_limits().__dict__,
                "authenticated": True,
            }

        # Auth por webhook signature
        if webhook_signature and webhook_body:
            for tenant in self._tenants.values():
                expected = hmac.new(
                    tenant.webhook_secret.encode(),
                    webhook_body.encode() if isinstance(webhook_body, str) else webhook_body,
                    hashlib.sha256,
                ).hexdigest()
                if hmac.compare_digest(expected, webhook_signature):
                    return {
                        "tenant_id": tenant.id,
                        "tenant_name": tenant.name,
                        "plan": tenant.plan.value,
                        "limits": tenant.get_limits().__dict__,
                        "authenticated": True,
                    }

        return None

    # ─── Tenant Management ───────────────────────────────────────

    @hook("tenant.create")
    async def create_tenant(self, event: Event) -> dict:
        """Crea un nuevo tenant."""
        tenant_id = event.data.get("id", secrets.token_urlsafe(8))
        name = event.data.get("name", "Unnamed")
        plan = PlanTier(event.data.get("plan", "free"))

        tenant = Tenant(id=tenant_id, name=name, plan=plan)
        self._tenants[tenant_id] = tenant
        self._api_key_index[tenant.api_key] = tenant_id

        logger.info("tenant.created", tenant_id=tenant_id, plan=plan.value)
        return tenant.to_dict()

    @hook("tenant.get")
    async def get_tenant(self, event: Event) -> dict | None:
        """Obtiene info de un tenant."""
        tenant_id = event.data.get("tenant_id", "")
        tenant = self._tenants.get(tenant_id)
        return tenant.to_dict() if tenant else None

    @hook("tenant.list")
    async def list_tenants(self, event: Event) -> list[dict]:
        """Lista todos los tenants."""
        return [t.to_dict() for t in self._tenants.values()]

    # ─── Usage & Rate Limiting ───────────────────────────────────

    def _get_usage_key(self, tenant_id: str) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"{tenant_id}:{today}"

    def _get_counter(self, tenant_id: str) -> UsageCounter:
        key = self._get_usage_key(tenant_id)
        if key not in self._usage:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self._usage[key] = UsageCounter(tenant_id=tenant_id, date=today)
        return self._usage[key]

    @hook("usage.check")
    async def check_usage(self, event: Event) -> dict:
        """
        Verifica si una acción está dentro de los límites.

        Retorna {"allowed": bool, "remaining": int, "limit": int}
        """
        tenant_id = event.data.get("tenant_id", "default")
        action = event.data.get("action", "")  # "messages", "browser_sessions", etc.

        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return {"allowed": False, "reason": "tenant_not_found"}

        limits = tenant.get_limits()
        counter = self._get_counter(tenant_id)

        limit_map = {
            "messages": (counter.messages, limits.messages_per_day),
            "browser_sessions": (counter.browser_sessions, limits.browser_sessions_per_day),
            "http_requests": (counter.http_requests, limits.http_requests_per_day),
            "llm_tokens": (counter.llm_tokens, limits.llm_tokens_per_day),
        }

        if action not in limit_map:
            return {"allowed": True, "remaining": -1, "limit": -1}

        current, limit = limit_map[action]
        allowed = current < limit

        return {
            "allowed": allowed,
            "remaining": max(0, limit - current),
            "limit": limit,
            "current": current,
        }

    @hook("usage.increment")
    async def increment_usage(self, event: Event):
        """Incrementa un contador de uso."""
        tenant_id = event.data.get("tenant_id", "default")
        action = event.data.get("action", "")
        amount = event.data.get("amount", 1)

        counter = self._get_counter(tenant_id)

        if action == "messages":
            counter.messages += amount
        elif action == "browser_sessions":
            counter.browser_sessions += amount
        elif action == "http_requests":
            counter.http_requests += amount
        elif action == "llm_tokens":
            counter.llm_tokens += amount

    @hook("usage.get")
    async def get_usage(self, event: Event) -> dict:
        """Obtiene el uso actual de un tenant."""
        tenant_id = event.data.get("tenant_id", "default")
        counter = self._get_counter(tenant_id)
        tenant = self._tenants.get(tenant_id)
        limits = tenant.get_limits().__dict__ if tenant else {}

        return {
            "usage": counter.to_dict(),
            "limits": limits,
        }

    # ─── Middleware para el Event Bus ─────────────────────────────

    async def usage_middleware(self, event: Event) -> Event | None:
        """
        Middleware que se puede agregar al event bus para
        verificar límites automáticamente antes de cada acción.

        Uso:
            event_bus.add_middleware(tenancy_module.usage_middleware)
        """
        # Mapear eventos a acciones de uso
        action_map = {
            "browser.navigate": "browser_sessions",
            "browser.extract": "browser_sessions",
            "http.request": "http_requests",
            "messaging.incoming": "messages",
        }

        action = action_map.get(event.name)
        if not action:
            return event  # No aplica límite

        tenant_id = event.data.get("tenant_id", event.metadata.get("tenant_id", "default"))

        check = await self.check_usage(Event(
            name="usage.check",
            data={"tenant_id": tenant_id, "action": action},
        ))

        if not check.get("allowed", True):
            logger.warning(
                "usage.limit_exceeded",
                tenant_id=tenant_id,
                action=action,
                limit=check.get("limit"),
            )
            # Notificar al usuario
            await self.bus.emit(Event(
                name="messaging.send",
                data={
                    "content": f"⚠️ Límite diario alcanzado para {action}. Upgradeá tu plan para más capacidad.",
                    "channel": event.data.get("channel", "system"),
                },
                source="multi_tenancy",
            ))
            return None  # Cancelar el evento

        # Incrementar uso
        await self.increment_usage(Event(
            name="usage.increment",
            data={"tenant_id": tenant_id, "action": action},
        ))

        return event
