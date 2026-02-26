"""
Plugin System — Auto-discovery, lifecycle management, hot-reload.

Cada plugin hereda de PluginBase y usa decoradores @hook para
registrar handlers en el event bus automáticamente.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import structlog

from core.event_bus import EventBus, event_bus

logger = structlog.get_logger()


# ── Hook Decorator ───────────────────────────────────────────────

def hook(event_name: str, priority: int = 0):
    """
    Decorador que marca un método como handler de un evento.

    @hook("browser.page_loaded")
    async def on_page(self, event):
        ...
    """
    def decorator(fn: Callable) -> Callable:
        if not hasattr(fn, "_hooks"):
            fn._hooks = []
        fn._hooks.append({"event": event_name, "priority": priority})
        return fn
    return decorator


# ── Plugin Base ──────────────────────────────────────────────────

class PluginBase(ABC):
    """
    Clase base para todos los plugins/módulos del agente.

    Cada plugin tiene:
    - Metadata (name, version, description, dependencies)
    - Lifecycle hooks (on_load, on_unload)
    - Acceso al event_bus para emitir y suscribirse a eventos
    - Config inyectado automáticamente
    """

    name: str = "unnamed_plugin"
    version: str = "0.1.0"
    description: str = ""
    dependencies: list[str] = []

    def __init__(self, bus: EventBus | None = None, config: dict[str, Any] | None = None):
        self.bus = bus or event_bus
        self.config = config or {}
        self._registered_handlers: list[tuple[str, Callable]] = []

    async def on_load(self):
        """Hook llamado cuando el plugin se carga. Override para inicializar recursos."""
        pass

    async def on_unload(self):
        """Hook llamado antes de descargar. Override para cleanup."""
        pass

    def register_hooks(self):
        """Registra automáticamente todos los métodos decorados con @hook."""
        for attr_name in dir(self):
            attr = getattr(self, attr_name, None)
            if attr and hasattr(attr, "_hooks"):
                for hook_info in attr._hooks:
                    self.bus.subscribe(
                        hook_info["event"],
                        attr,
                        priority=hook_info["priority"],
                    )
                    self._registered_handlers.append((hook_info["event"], attr))
                    logger.debug(
                        "plugin.hook_registered",
                        plugin=self.name,
                        event_name=hook_info["event"],
                        method=attr_name,
                    )

    def unregister_hooks(self):
        """Remueve todos los handlers registrados."""
        for event_name, handler in self._registered_handlers:
            self.bus.unsubscribe(event_name, handler)
        self._registered_handlers.clear()

    def __repr__(self):
        return f"<Plugin {self.name} v{self.version}>"


# ── Plugin Registry ──────────────────────────────────────────────

@dataclass
class PluginInfo:
    """Metadata de un plugin registrado."""
    name: str
    version: str
    description: str
    instance: PluginBase
    module_path: str
    enabled: bool = True
    load_order: int = 0


class PluginRegistry:
    """
    Registro central de plugins con auto-discovery y gestión de ciclo de vida.

    Características:
    - Auto-descubrimiento de plugins en un directorio
    - Resolución de dependencias
    - Hot-reload (para desarrollo)
    - Enable/disable individual
    """

    def __init__(self, bus: EventBus | None = None):
        self.bus = bus or event_bus
        self._plugins: dict[str, PluginInfo] = {}
        self._load_order_counter = 0

    @property
    def plugins(self) -> dict[str, PluginInfo]:
        return self._plugins.copy()

    async def discover(self, plugins_dir: str | Path, config: dict[str, Any] | None = None):
        """
        Descubre y carga plugins desde un directorio.
        Busca archivos .py con clases que hereden de PluginBase.
        """
        plugins_path = Path(plugins_dir)
        if not plugins_path.exists():
            logger.warning("plugin.dir_not_found", path=str(plugins_path))
            plugins_path.mkdir(parents=True, exist_ok=True)
            return

        for file_path in sorted(plugins_path.glob("*.py")):
            if file_path.name.startswith("_"):
                continue
            try:
                await self._load_from_file(file_path, config or {})
            except Exception as exc:
                logger.error(
                    "plugin.load_error",
                    file=str(file_path),
                    error=str(exc),
                )

        # También buscar en subdirectorios (packages)
        for dir_path in sorted(plugins_path.iterdir()):
            init_file = dir_path / "__init__.py"
            if dir_path.is_dir() and init_file.exists():
                try:
                    await self._load_from_file(init_file, config or {}, package_name=dir_path.name)
                except Exception as exc:
                    logger.error(
                        "plugin.load_error",
                        file=str(init_file),
                        error=str(exc),
                    )

        # Resolver dependencias y ordenar
        await self._resolve_dependencies()

        logger.info(
            "plugin.discovery_complete",
            total=len(self._plugins),
            plugins=list(self._plugins.keys()),
        )

    async def _load_from_file(
        self, file_path: Path, config: dict, package_name: str | None = None
    ):
        """Carga un plugin desde un archivo Python."""
        module_name = package_name or file_path.stem
        spec = importlib.util.spec_from_file_location(
            f"plugins.{module_name}", str(file_path)
        )
        if spec is None or spec.loader is None:
            return

        module = importlib.util.module_from_spec(spec)
        sys.modules[f"plugins.{module_name}"] = module
        spec.loader.exec_module(module)

        # Encontrar clases que hereden de PluginBase
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, PluginBase) and obj is not PluginBase:
                await self.register(obj, config, str(file_path))

    async def register(
        self,
        plugin_class: type[PluginBase],
        config: dict[str, Any] | None = None,
        module_path: str = "",
    ):
        """Registra e inicializa un plugin."""
        instance = plugin_class(bus=self.bus, config=config or {})

        if instance.name in self._plugins:
            logger.warning("plugin.duplicate", name=instance.name)
            return

        # Registrar hooks en el event bus
        instance.register_hooks()

        # Ejecutar lifecycle hook
        await instance.on_load()

        self._load_order_counter += 1
        self._plugins[instance.name] = PluginInfo(
            name=instance.name,
            version=instance.version,
            description=instance.description,
            instance=instance,
            module_path=module_path,
            load_order=self._load_order_counter,
        )

        logger.info(
            "plugin.registered",
            name=instance.name,
            version=instance.version,
        )

    async def unregister(self, name: str):
        """Descarga y remueve un plugin."""
        if name not in self._plugins:
            return

        info = self._plugins[name]
        info.instance.unregister_hooks()
        await info.instance.on_unload()
        del self._plugins[name]

        logger.info("plugin.unregistered", name=name)

    async def reload(self, name: str):
        """Hot-reload de un plugin (útil en desarrollo)."""
        if name not in self._plugins:
            return

        info = self._plugins[name]
        module_path = info.module_path
        config = info.instance.config

        await self.unregister(name)

        # Re-importar el módulo
        module_name = f"plugins.{name}"
        if module_name in sys.modules:
            del sys.modules[module_name]

        await self._load_from_file(Path(module_path), config)
        logger.info("plugin.reloaded", name=name)

    async def _resolve_dependencies(self):
        """Verifica que las dependencias de cada plugin estén satisfechas."""
        for name, info in self._plugins.items():
            for dep in info.instance.dependencies:
                if dep not in self._plugins:
                    logger.warning(
                        "plugin.missing_dependency",
                        plugin=name,
                        dependency=dep,
                    )

    async def shutdown(self):
        """Descarga todos los plugins en orden inverso."""
        for name in reversed(list(self._plugins.keys())):
            await self.unregister(name)

    def get(self, name: str) -> PluginBase | None:
        """Obtiene la instancia de un plugin por nombre."""
        info = self._plugins.get(name)
        return info.instance if info else None

    def list_plugins(self) -> list[dict]:
        """Lista información de todos los plugins."""
        return [
            {
                "name": info.name,
                "version": info.version,
                "description": info.description,
                "enabled": info.enabled,
                "load_order": info.load_order,
            }
            for info in self._plugins.values()
        ]
