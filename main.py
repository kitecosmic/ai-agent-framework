"""
NexusAgent — Entry point.

Usage:
    python -m core.main
    # o
    python main.py
"""
import sys
import asyncio
import uvicorn
from config.settings import get_settings

# Windows necesita ProactorEventLoop para subprocesos (Playwright)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def main():
    settings = get_settings()
    # En Windows, reload=True rompe Playwright (el reloader sobreescribe ProactorEventLoop)
    # Hot-reload de plugins sigue funcionando via el endpoint POST /api/plugins/{name}/reload
    use_reload = settings.plugins_hot_reload and sys.platform != "win32"
    uvicorn.run(
        "api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=use_reload,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
