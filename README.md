# 🤖 NexusAgent — Modular AI Agent Framework

## Arquitectura

```
┌─────────────────────────────────────────────────────────┐
│                      API Gateway                        │
│                  (FastAPI + WebSocket)                   │
├─────────────────────────────────────────────────────────┤
│                    Agent Orchestrator                    │
│              (Event Bus + Task Router)                   │
├──────────┬──────────┬──────────┬──────────┬─────────────┤
│ Browser  │  HTTP    │  Cron    │ Messaging│  Plugin N   │
│ Module   │ Requests │ Scheduler│ Bridge   │  (custom)   │
│(Playwright)│ Module │ Module   │ Module   │             │
├──────────┴──────────┴──────────┴──────────┴─────────────┤
│                    Plugin Registry                       │
│            (Auto-discovery + Hot-reload)                 │
├─────────────────────────────────────────────────────────┤
│              Shared Services Layer                       │
│     (Redis Queue · PostgreSQL · Vector Store)            │
└─────────────────────────────────────────────────────────┘
```

## Stack Tecnológico
- **Runtime**: Python 3.12+
- **API**: FastAPI + WebSocket
- **Browser Automation**: Playwright (async)
- **Task Queue**: Celery + Redis
- **Scheduler**: APScheduler
- **Database**: PostgreSQL + SQLAlchemy
- **LLM**: Anthropic Claude / OpenAI (pluggable)
- **Messaging Bridge**: WebSocket + Webhooks (listo para tu app)

## Quick Start

```bash
# 1. Instalar dependencias
pip install -r requirements.txt
playwright install chromium

# 2. Configurar variables de entorno
cp .env.example .env

# 3. Levantar servicios
docker-compose up -d redis postgres

# 4. Ejecutar el agente
python -m core.main
```

## Crear un Plugin Custom

```python
from core.plugin_base import PluginBase, hook

class MiPlugin(PluginBase):
    name = "mi_plugin"
    version = "1.0.0"

    @hook("on_message")
    async def handle_message(self, event):
        return {"response": "Hola desde mi plugin!"}

    @hook("on_task")
    async def run_task(self, task):
        # Tu lógica aquí
        pass
```

Coloca el archivo en `plugins/` y se auto-descubre al iniciar.
