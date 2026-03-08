"""
MCP Module — Conecta el agente con servidores MCP (Model Context Protocol).

Permite al agente usar herramientas externas como:
- Playwright MCP (navegación avanzada, scraping, interacción con páginas)
- Filesystem MCP
- Cualquier servidor MCP compatible

Eventos:
- mcp.call_tool    → Ejecutar una herramienta MCP
- mcp.list_tools   → Listar herramientas disponibles de todos los servidores

Configuración en .env:
- MCP_SERVERS = [{"name": "playwright", "command": "npx", "args": ["@playwright/mcp@latest"]}]

El módulo:
1. Inicia servidores MCP configurados al arrancar
2. Descubre herramientas automáticamente
3. Expone cada herramienta como evento en el bus (mcp.{server}.{tool})
4. También expone mcp.call_tool genérico
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import structlog

from core.event_bus import Event
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()

# Intentar importar MCP SDK
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    logger.warning("mcp.sdk_not_installed", hint="pip install mcp")


class MCPServerConnection:
    """Representa una conexión activa a un servidor MCP."""

    def __init__(self, name: str, params: StdioServerParameters):
        self.name = name
        self.params = params
        self.session: ClientSession | None = None
        self.tools: list[dict] = []
        self._context_manager = None
        self._session_cm = None
        self._read = None
        self._write = None

    async def connect(self, timeout: int = 120):
        """Establece conexión con el servidor MCP.

        timeout: segundos máximos para la conexión inicial (npx puede descargar paquetes).
        """
        if not MCP_AVAILABLE:
            raise RuntimeError("MCP SDK no instalado. Ejecutá: pip install mcp")

        logger.info("mcp.connecting", server=self.name, timeout=timeout)

        try:
            self._context_manager = stdio_client(self.params)
            self._read, self._write = await asyncio.wait_for(
                self._context_manager.__aenter__(), timeout=timeout
            )

            self._session_cm = ClientSession(self._read, self._write)
            self.session = await asyncio.wait_for(
                self._session_cm.__aenter__(), timeout=30
            )

            await asyncio.wait_for(self.session.initialize(), timeout=30)

            # Descubrir herramientas
            tools_result = await asyncio.wait_for(
                self.session.list_tools(), timeout=15
            )
            self.tools = []
            for tool in tools_result.tools:
                self.tools.append({
                    "name": tool.name,
                    "description": getattr(tool, "description", "") or "",
                    "input_schema": getattr(tool, "inputSchema", {}) or {},
                })

            logger.info(
                "mcp.server_connected",
                server=self.name,
                tools=[t["name"] for t in self.tools],
            )
        except asyncio.TimeoutError:
            logger.error("mcp.connect_timeout", server=self.name, timeout=timeout)
            await self.disconnect()
            raise RuntimeError(f"Timeout conectando a MCP '{self.name}' ({timeout}s)")
        except Exception as exc:
            logger.error("mcp.connect_error", server=self.name, error=str(exc))
            await self.disconnect()
            raise

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Ejecuta una herramienta en el servidor MCP."""
        if not self.session:
            raise RuntimeError(f"Servidor MCP '{self.name}' no conectado")

        result = await self.session.call_tool(tool_name, arguments=arguments)

        # Extraer contenido del resultado
        contents = []
        for block in result.content:
            if hasattr(block, "text"):
                contents.append(block.text)
            elif hasattr(block, "data"):
                contents.append(f"[binary data: {len(block.data)} bytes]")
            else:
                contents.append(str(block))

        return {
            "success": True,
            "tool": tool_name,
            "server": self.name,
            "result": "\n".join(contents) if contents else "(sin resultado)",
            "is_error": getattr(result, "isError", False),
        }

    async def disconnect(self):
        """Cierra la conexión con el servidor MCP."""
        try:
            if self._session_cm:
                await self._session_cm.__aexit__(None, None, None)
            if self._context_manager:
                await self._context_manager.__aexit__(None, None, None)
        except Exception as exc:
            logger.debug("mcp.disconnect_error", server=self.name, error=str(exc))
        finally:
            self.session = None
            logger.info("mcp.server_disconnected", server=self.name)


class MCPModule(PluginBase):
    """Módulo que conecta el agente con servidores MCP externos."""

    name = "mcp"
    version = "1.0.0"
    description = "Model Context Protocol integration for external tools"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._servers: dict[str, MCPServerConnection] = {}
        self._tool_registry: dict[str, tuple[str, str]] = {}  # tool_name -> (server_name, mcp_tool_name)
        self._pending_configs: list[dict] = []  # Configs que fallaron al conectar (retry lazy)

    async def on_load(self):
        """Conecta a los servidores MCP configurados."""
        if not MCP_AVAILABLE:
            logger.warning("mcp.disabled", reason="SDK no instalado")
            return

        # Cargar configuración de servidores MCP
        servers_config = self._load_mcp_config()
        if not servers_config:
            logger.info("mcp.no_servers_configured")
            return

        for server_cfg in servers_config:
            name = server_cfg.get("name", "unknown")
            try:
                await self._connect_server(server_cfg)
            except Exception as exc:
                logger.error("mcp.server_connect_error", server=name, error=str(exc))
                self._pending_configs.append(server_cfg)

    async def _retry_pending(self):
        """Reintenta conectar servidores que fallaron al inicio."""
        if not self._pending_configs:
            return
        still_pending = []
        for cfg in self._pending_configs:
            name = cfg.get("name", "unknown")
            try:
                await self._connect_server(cfg)
                logger.info("mcp.retry_connected", server=name)
            except Exception as exc:
                logger.debug("mcp.retry_failed", server=name, error=str(exc))
                still_pending.append(cfg)
        self._pending_configs = still_pending

    async def on_unload(self):
        """Desconecta todos los servidores MCP."""
        for name, server in list(self._servers.items()):
            await server.disconnect()
        self._servers.clear()
        self._tool_registry.clear()

    def _load_mcp_config(self) -> list[dict]:
        """Carga la configuración de servidores MCP."""
        # Desde .env (JSON string)
        mcp_env = os.environ.get("MCP_SERVERS", self.config.get("mcp_servers", ""))
        if mcp_env:
            try:
                return json.loads(mcp_env) if isinstance(mcp_env, str) else mcp_env
            except json.JSONDecodeError:
                logger.warning("mcp.invalid_config", raw=mcp_env[:100])

        # Desde archivo de configuración
        from pathlib import Path
        config_path = Path(__file__).parent.parent / "data" / "mcp_servers.json"
        if config_path.exists():
            try:
                return json.loads(config_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("mcp.config_file_error", path=str(config_path), error=str(exc))

        return []

    async def _connect_server(self, config: dict):
        """Conecta a un servidor MCP individual."""
        name = config.get("name", "unknown")
        command = config.get("command", "")
        args = config.get("args", [])
        env_vars = config.get("env", {})

        if not command:
            logger.warning("mcp.no_command", server=name)
            return

        # Construir environment
        env = {**os.environ, **env_vars}

        params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
        )

        server = MCPServerConnection(name, params)
        await server.connect()
        self._servers[name] = server

        # Registrar herramientas en el registry
        for tool in server.tools:
            event_key = f"mcp.{name}.{tool['name']}"
            self._tool_registry[event_key] = (name, tool["name"])

    # ── Event handlers ──────────────────────────────────────────────

    @hook("mcp.call_tool")
    async def handle_call_tool(self, event: Event) -> dict[str, Any]:
        """Ejecuta una herramienta MCP.

        Data: {"server": "playwright", "tool": "browser_navigate", "arguments": {...}}
        O:    {"tool": "browser_navigate", "arguments": {...}}  (busca en todos los servers)
        """
        # Reintentar servidores pendientes antes de buscar
        await self._retry_pending()

        server_name = event.data.get("server", "")
        tool_name = event.data.get("tool", "")
        arguments = event.data.get("arguments", {})

        if not tool_name:
            return {"success": False, "error": "Nombre de herramienta vacío"}

        # Buscar el servidor correcto
        target_server = None
        if server_name and server_name in self._servers:
            target_server = self._servers[server_name]
        else:
            # Buscar la herramienta en todos los servidores
            for srv in self._servers.values():
                if any(t["name"] == tool_name for t in srv.tools):
                    target_server = srv
                    break

        if not target_server:
            available = self._get_all_tools_summary()
            return {
                "success": False,
                "error": f"Herramienta '{tool_name}' no encontrada",
                "available_tools": available,
            }

        try:
            result = await target_server.call_tool(tool_name, arguments)
            logger.info("mcp.tool_called", server=target_server.name, tool=tool_name)
            return result
        except Exception as exc:
            logger.error("mcp.tool_error", server=target_server.name, tool=tool_name, error=str(exc))
            return {"success": False, "error": str(exc), "tool": tool_name}

    @hook("mcp.list_tools")
    async def handle_list_tools(self, event: Event) -> dict[str, Any]:
        """Lista todas las herramientas MCP disponibles."""
        await self._retry_pending()
        all_tools = {}
        for name, server in self._servers.items():
            all_tools[name] = server.tools

        return {
            "success": True,
            "servers": list(self._servers.keys()),
            "tools": all_tools,
            "total_tools": sum(len(t) for t in all_tools.values()),
        }

    def _get_all_tools_summary(self) -> list[str]:
        """Retorna un resumen de todas las herramientas disponibles."""
        tools = []
        for name, server in self._servers.items():
            for tool in server.tools:
                tools.append(f"{name}.{tool['name']}: {tool['description'][:80]}")
        return tools
