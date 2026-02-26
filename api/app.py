"""
API Gateway — FastAPI con REST + WebSocket.

Endpoints:
- POST /api/tasks              → ejecutar tarea
- POST /api/messages/incoming  → webhook para mensajería
- GET  /api/plugins            → listar plugins
- POST /api/plugins/{name}/reload → hot-reload un plugin
- GET  /api/scheduler/jobs     → listar jobs
- POST /api/scheduler/jobs     → crear job
- WS   /ws                     → WebSocket bidireccional
- GET  /health                 → health check
"""
from __future__ import annotations

import asyncio
import sys
import json
from contextlib import asynccontextmanager

# Windows necesita ProactorEventLoop para subprocesos (Playwright)
# Esto aplica también al proceso hijo que crea uvicorn --reload
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config.settings import get_settings
from core.event_bus import Event, event_bus
from core.llm_router import AnthropicProvider, OpenAIProvider, OllamaProvider, llm_router
from core.orchestrator import Orchestrator
from core.plugin_base import PluginRegistry

# Módulos built-in
from modules.browser_module import BrowserModule
from modules.http_module import HTTPModule
from modules.scheduler_module import SchedulerModule
from modules.messaging_bridge import MessagingBridge
from modules.memory_module import MemoryModule
from modules.multi_tenancy import MultiTenancyModule
from modules.telegram_bridge import TelegramBridge

logger = structlog.get_logger()

# ── Estado global ────────────────────────────────────────────────

registry = PluginRegistry(bus=event_bus)
ws_connections: dict[str, WebSocket] = {}


# ── Lifecycle ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup y shutdown del agente."""
    settings = get_settings()

    # 1. Configurar LLM providers
    if settings.anthropic_api_key:
        llm_router.add_provider(
            AnthropicProvider(
                api_key=settings.anthropic_api_key,
                default_model=settings.default_llm_model,
            ),
            default=settings.default_llm_provider == "anthropic",
        )
    if settings.openai_api_key:
        llm_router.add_provider(
            OpenAIProvider(api_key=settings.openai_api_key),
            default=settings.default_llm_provider == "openai",
        )

    # Ollama (modelos locales)
    if settings.ollama_base_url:
        llm_router.add_provider(
            OllamaProvider(
                base_url=settings.ollama_base_url,
                default_model=settings.ollama_model,
            ),
            default=settings.default_llm_provider == "ollama",
        )

    config = settings.model_dump()

    # 2. Registrar módulos built-in
    await registry.register(MemoryModule, config)
    await registry.register(MultiTenancyModule, config)
    await registry.register(BrowserModule, config)
    await registry.register(HTTPModule, config)
    await registry.register(SchedulerModule, config)
    await registry.register(MessagingBridge, config)
    await registry.register(TelegramBridge, config)
    await registry.register(Orchestrator, config)

    # 2.5 Agregar middleware de rate limiting
    tenancy: MultiTenancyModule = registry.get("multi_tenancy")
    if tenancy:
        event_bus.add_middleware(tenancy.usage_middleware)

    # 3. Descubrir plugins custom
    await registry.discover(settings.plugins_dir, config)

    logger.info(
        "agent.started",
        plugins=len(registry.plugins),
        llm_providers=list(llm_router._providers.keys()),
    )

    yield

    # Shutdown
    await registry.shutdown()
    logger.info("agent.stopped")


# ── App ──────────────────────────────────────────────────────────

app = FastAPI(
    title="NexusAgent",
    description="Modular AI Agent Framework",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configurar según tu app
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Schemas ──────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    instruction: str
    channel: str = "api"


class MessageRequest(BaseModel):
    id: str | None = None
    type: str = "text"
    content: str
    sender: str
    channel: str = "default"
    metadata: dict[str, Any] = {}
    attachments: list[dict] = []


class JobRequest(BaseModel):
    id: str
    name: str
    trigger_type: str  # "cron" | "interval" | "date"
    trigger_args: dict
    event_name: str
    event_data: dict = {}


# ── Endpoints ────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "plugins": len(registry.plugins),
        "events_registered": len(event_bus.registered_events),
    }


@app.post("/api/tasks")
async def execute_task(req: TaskRequest):
    """Ejecuta una tarea en lenguaje natural."""
    results = await event_bus.emit(Event(
        name="task.execute",
        data={"instruction": req.instruction, "channel": req.channel},
        source="api",
    ))

    if results:
        result = results[0]
        if result:
            return {
                "success": result.success,
                "response": result.response,
                "steps_completed": result.steps_completed,
                "error": result.error,
            }

    return {"success": False, "error": "No orchestrator available"}


@app.post("/api/messages/incoming")
async def incoming_message(req: MessageRequest):
    """Webhook para recibir mensajes de tu app de mensajería."""
    messaging: MessagingBridge = registry.get("messaging")
    if not messaging:
        raise HTTPException(500, "Messaging module not loaded")

    message = await messaging.receive_message(req.model_dump())
    return {"status": "received", "message_id": message.id}


@app.get("/api/plugins")
async def list_plugins():
    """Lista todos los plugins cargados."""
    return {"plugins": registry.list_plugins()}


@app.post("/api/plugins/{name}/reload")
async def reload_plugin(name: str):
    """Hot-reload de un plugin (desarrollo)."""
    await registry.reload(name)
    return {"status": "reloaded", "plugin": name}


@app.get("/api/scheduler/jobs")
async def list_jobs():
    """Lista todos los jobs programados."""
    results = await event_bus.emit("scheduler.list_jobs")
    jobs = results[0] if results else []
    return {"jobs": jobs}


@app.post("/api/scheduler/jobs")
async def create_job(req: JobRequest):
    """Crea un job programado."""
    results = await event_bus.emit(Event(
        name="scheduler.add_job",
        data=req.model_dump(),
        source="api",
    ))
    if results and results[0]:
        job = results[0]
        return {"status": "created", "job_id": job.id, "next_run": str(job.next_run)}
    raise HTTPException(500, "Failed to create job")


@app.get("/api/events/history")
async def event_history(event_name: str | None = None, limit: int = 50):
    """Historial de eventos recientes."""
    events = event_bus.get_history(event_name=event_name, limit=limit)
    return {
        "events": [
            {
                "id": e.id,
                "name": e.name,
                "source": e.source,
                "timestamp": e.timestamp.isoformat(),
                "data_keys": list(e.data.keys()),
            }
            for e in events
        ]
    }


# ── Tenant & Auth Endpoints ──────────────────────────────────────

class TenantRequest(BaseModel):
    name: str
    plan: str = "free"


@app.post("/api/tenants")
async def create_tenant(req: TenantRequest):
    """Crea un nuevo tenant (cuenta SaaS)."""
    results = await event_bus.emit(Event(
        name="tenant.create",
        data={"name": req.name, "plan": req.plan},
        source="api",
    ))
    if results and results[0]:
        return results[0]
    raise HTTPException(500, "Failed to create tenant")


@app.get("/api/tenants/{tenant_id}")
async def get_tenant(tenant_id: str):
    """Obtiene info de un tenant."""
    results = await event_bus.emit(Event(
        name="tenant.get",
        data={"tenant_id": tenant_id},
        source="api",
    ))
    if results and results[0]:
        return results[0]
    raise HTTPException(404, "Tenant not found")


@app.get("/api/tenants/{tenant_id}/usage")
async def get_usage(tenant_id: str):
    """Obtiene el uso actual de un tenant."""
    results = await event_bus.emit(Event(
        name="usage.get",
        data={"tenant_id": tenant_id},
        source="api",
    ))
    return results[0] if results and results[0] else {}


# ── Memory Endpoints ─────────────────────────────────────────────

@app.get("/api/memory/{tenant_id}/{user_id}")
async def get_user_context(tenant_id: str, user_id: str):
    """Obtiene el contexto completo de un usuario (perfil + memorias)."""
    results = await event_bus.emit(Event(
        name="memory.build_context",
        data={"user_id": user_id, "tenant_id": tenant_id},
        source="api",
    ))
    return results[0] if results and results[0] else {}


@app.get("/api/memory/{tenant_id}/{user_id}/recall")
async def recall_memories(tenant_id: str, user_id: str, category: str | None = None, query: str | None = None):
    """Busca memorias de un usuario."""
    results = await event_bus.emit(Event(
        name="memory.recall",
        data={
            "user_id": user_id,
            "tenant_id": tenant_id,
            "category": category,
            "query": query or "",
        },
        source="api",
    ))
    return {"memories": results[0] if results and results[0] else []}


@app.get("/api/memory/stats/{tenant_id}")
async def memory_stats(tenant_id: str):
    """Estadísticas de memoria de un tenant."""
    results = await event_bus.emit(Event(
        name="memory.stats",
        data={"tenant_id": tenant_id},
        source="api",
    ))
    return results[0] if results and results[0] else {}


# ── WebSocket ────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    WebSocket bidireccional.

    Tu app de mensajería puede conectarse aquí para comunicación
    en tiempo real en lugar de usar webhooks.

    Protocolo:
    → Client envía: {"type": "message", "data": {"content": "...", "channel": "..."}}
    ← Server responde: {"type": "response", "data": {"content": "...", ...}}
    ← Server pushea: {"type": "event", "data": {"event": "...", ...}}
    """
    await ws.accept()
    client_id = id(ws)
    ws_connections[str(client_id)] = ws

    logger.info("ws.connected", client_id=client_id)

    # Suscribir para forwarding de eventos
    async def forward_event(event: Event):
        try:
            await ws.send_json({
                "type": "event",
                "data": {
                    "event": event.name,
                    "source": event.source,
                    "data": event.data,
                    "timestamp": event.timestamp.isoformat(),
                },
            })
        except Exception:
            pass

    event_bus.subscribe("messaging.outgoing", forward_event)

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)

            msg_type = data.get("type", "")

            if msg_type == "message":
                # Procesar como mensaje entrante
                messaging: MessagingBridge = registry.get("messaging")
                if messaging:
                    await messaging.receive_message(data.get("data", {}))

            elif msg_type == "task":
                # Ejecutar tarea directa
                results = await event_bus.emit(Event(
                    name="task.execute",
                    data=data.get("data", {}),
                    source="websocket",
                ))
                if results and results[0]:
                    await ws.send_json({
                        "type": "response",
                        "data": {
                            "success": results[0].success,
                            "response": results[0].response,
                        },
                    })

            elif msg_type == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info("ws.disconnected", client_id=client_id)
    finally:
        ws_connections.pop(str(client_id), None)
        event_bus.unsubscribe("messaging.outgoing", forward_event)


# ── Init packages ────────────────────────────────────────────────

# Ensure modules is importable
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
