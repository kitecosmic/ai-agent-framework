"""
System Module — Permite al agente ejecutar comandos del sistema, crear/editar archivos,
e instalar paquetes.

Eventos:
- system.exec        → Ejecutar un comando shell
- system.file_read   → Leer un archivo
- system.file_write  → Crear o escribir un archivo
- system.file_list   → Listar archivos en un directorio
- system.pip_install → Instalar un paquete Python

SEGURIDAD:
- Comandos peligrosos (rm -rf /, etc.) están bloqueados
- Paths están restringidos a un directorio raíz configurable
- Timeout en ejecución de comandos
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

import structlog

from core.event_bus import Event
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()

# Comandos completamente prohibidos (patrones regex)
BLOCKED_COMMANDS = [
    r'\brm\s+(-rf?|--recursive)\s+/',  # rm -rf /
    r'\brm\s+(-rf?|--recursive)\s+~',  # rm -rf ~
    r'\bmkfs\b',                         # format disk
    r'\bdd\s+.*of=/dev/',               # dd to device
    r'\b:()\s*\{',                       # fork bomb
    r'\bchmod\s+777\s+/',               # chmod 777 /
    r'\bshutdown\b',                     # shutdown
    r'\breboot\b',                       # reboot
    r'\binit\s+0\b',                     # init 0
    r'\bkill\s+-9\s+1\b',              # kill init
    r'\bsystemctl\s+(stop|disable)\s+(sshd|ssh|networking)', # kill ssh
    r'>\s*/dev/sd',                      # write to disk device
    r'\bpasswd\b',                       # change password
    r'\buserdel\b',                      # delete user
    r'\bgroupdel\b',                     # delete group
]

# Comandos que requieren confirmación extra (se loguean con warning)
WARN_COMMANDS = [
    r'\bapt\s+(remove|purge)',
    r'\bpip\s+uninstall',
    r'\bsystemctl\s+(stop|restart)',
    r'\brm\s+',
    r'\bchown\b',
    r'\bchmod\b',
]

# Extensiones de archivos que no se pueden escribir (binarios, etc.)
BLOCKED_EXTENSIONS = {'.exe', '.bin', '.so', '.dll', '.iso', '.img'}


class SystemModule(PluginBase):
    """Módulo para ejecutar comandos del sistema y manipular archivos."""

    name = "system"
    version = "1.0.0"
    description = "Shell execution, file CRUD, and package management"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Directorio raíz permitido (por defecto: home del usuario)
        self._allowed_root = Path(
            os.path.expanduser(self.config.get("system_allowed_root", "~"))
        ).resolve()
        self._command_timeout = int(self.config.get("system_command_timeout", 30))
        self._max_output = int(self.config.get("system_max_output", 10000))

    async def on_load(self):
        logger.info(
            "system.loaded",
            allowed_root=str(self._allowed_root),
            timeout=self._command_timeout,
        )

    # ── Shell execution ──────────────────────────────────────────────

    @hook("system.exec")
    async def exec_command(self, event: Event) -> dict[str, Any]:
        """Ejecuta un comando shell y retorna stdout/stderr."""
        command = event.data.get("command", "")
        cwd = os.path.expanduser(event.data.get("cwd", str(self._allowed_root)))
        timeout = event.data.get("timeout", self._command_timeout)

        if not command.strip():
            return {"success": False, "error": "Comando vacío"}

        # Validar seguridad
        security_check = self._check_command_safety(command)
        if not security_check["allowed"]:
            logger.warning("system.blocked_command", command=command, reason=security_check["reason"])
            return {"success": False, "error": f"Comando bloqueado: {security_check['reason']}"}

        if security_check.get("warn"):
            logger.warning("system.warn_command", command=command, reason=security_check["warn"])

        # Validar cwd dentro del directorio permitido
        cwd_path = Path(cwd).resolve()
        if not self._is_path_allowed(cwd_path):
            return {"success": False, "error": f"Directorio no permitido: {cwd}"}

        logger.info("system.exec", command=command[:100], cwd=cwd)

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd_path),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )

            stdout_str = stdout.decode("utf-8", errors="replace")[:self._max_output]
            stderr_str = stderr.decode("utf-8", errors="replace")[:self._max_output]

            result = {
                "success": process.returncode == 0,
                "exit_code": process.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "command": command,
            }

            logger.info(
                "system.exec_done",
                exit_code=process.returncode,
                stdout_len=len(stdout_str),
                stderr_len=len(stderr_str),
            )
            return result

        except asyncio.TimeoutError:
            return {"success": False, "error": f"Comando excedió el timeout de {timeout}s", "command": command}
        except Exception as exc:
            return {"success": False, "error": str(exc), "command": command}

    # ── File operations ──────────────────────────────────────────────

    @hook("system.file_read")
    async def file_read(self, event: Event) -> dict[str, Any]:
        """Lee el contenido de un archivo."""
        file_path = event.data.get("path", "")
        if not file_path:
            return {"success": False, "error": "Path vacío"}

        path = Path(os.path.expanduser(file_path)).resolve()
        if not self._is_path_allowed(path):
            return {"success": False, "error": f"Path no permitido: {file_path}"}

        if not path.exists():
            return {"success": False, "error": f"Archivo no existe: {file_path}"}

        if not path.is_file():
            return {"success": False, "error": f"No es un archivo: {file_path}"}

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            # Limitar tamaño de lectura
            if len(content) > self._max_output:
                content = content[:self._max_output] + f"\n... (truncado, {len(content)} chars total)"

            return {
                "success": True,
                "path": str(path),
                "content": content,
                "size": path.stat().st_size,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc), "path": str(path)}

    @hook("system.file_write")
    async def file_write(self, event: Event) -> dict[str, Any]:
        """Crea o escribe un archivo."""
        file_path = event.data.get("path", "")
        content = event.data.get("content", "")
        append = event.data.get("append", False)

        if not file_path:
            return {"success": False, "error": "Path vacío"}

        path = Path(os.path.expanduser(file_path)).resolve()
        if not self._is_path_allowed(path):
            return {"success": False, "error": f"Path no permitido: {file_path}"}

        # Bloquear escritura de binarios
        if path.suffix.lower() in BLOCKED_EXTENSIONS:
            return {"success": False, "error": f"Extensión bloqueada: {path.suffix}"}

        try:
            # Crear directorio padre si no existe
            path.parent.mkdir(parents=True, exist_ok=True)

            mode = "a" if append else "w"
            path.write_text(content, encoding="utf-8") if not append else \
                path.open(mode, encoding="utf-8").write(content)

            logger.info("system.file_write", path=str(path), size=len(content), append=append)
            return {
                "success": True,
                "path": str(path),
                "size": len(content),
                "action": "append" if append else "write",
            }
        except Exception as exc:
            return {"success": False, "error": str(exc), "path": str(path)}

    @hook("system.file_list")
    async def file_list(self, event: Event) -> dict[str, Any]:
        """Lista archivos en un directorio."""
        dir_path = event.data.get("path", str(self._allowed_root))
        pattern = event.data.get("pattern", "*")
        recursive = event.data.get("recursive", False)
        max_items = event.data.get("max_items", 100)

        path = Path(os.path.expanduser(dir_path)).resolve()
        if not self._is_path_allowed(path):
            return {"success": False, "error": f"Path no permitido: {dir_path}"}

        if not path.exists():
            return {"success": False, "error": f"Directorio no existe: {dir_path}"}

        try:
            items = []
            glob_fn = path.rglob if recursive else path.glob
            for i, item in enumerate(sorted(glob_fn(pattern))):
                if i >= max_items:
                    break
                items.append({
                    "name": item.name,
                    "path": str(item),
                    "type": "dir" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None,
                })

            return {
                "success": True,
                "path": str(path),
                "items": items,
                "count": len(items),
                "truncated": len(items) >= max_items,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc), "path": str(path)}

    @hook("system.file_delete")
    async def file_delete(self, event: Event) -> dict[str, Any]:
        """
        Elimina un archivo o directorio.

        REQUIERE confirmed=True para ejecutar — el orchestrator se encarga
        de pedir confirmación explícita al usuario antes de pasar este flag.
        Esto protege contra prompt injection: la confirmación viene del usuario real,
        no del LLM.

        Data: {"path": "/ruta/al/archivo", "confirmed": true}
        """
        file_path = event.data.get("path", "")
        confirmed = event.data.get("confirmed", False)

        if not file_path:
            return {"success": False, "error": "Path vacío"}

        path = Path(os.path.expanduser(file_path)).resolve()

        if not self._is_path_allowed(path):
            return {"success": False, "error": f"Path no permitido: {file_path}"}

        if not path.exists():
            return {"success": False, "error": f"No existe: {file_path}"}

        if not confirmed:
            # Retornar info para que el orchestrator muestre la confirmación al usuario
            return {
                "success": False,
                "requires_confirmation": True,
                "path": str(path),
                "is_dir": path.is_dir(),
                "error": "Requiere confirmación del usuario",
            }

        try:
            if path.is_file():
                path.unlink()
                action = "file_deleted"
            elif path.is_dir():
                import shutil as _shutil
                _shutil.rmtree(str(path))
                action = "dir_deleted"
            else:
                return {"success": False, "error": f"No es archivo ni directorio: {file_path}"}

            logger.warning("system.file_delete", path=str(path), action=action)
            return {"success": True, "path": str(path), "action": action}

        except Exception as exc:
            return {"success": False, "error": str(exc), "path": str(path)}

    # ── Package management ───────────────────────────────────────────

    @hook("system.pip_install")
    async def pip_install(self, event: Event) -> dict[str, Any]:
        """Instala un paquete Python con pip."""
        package = event.data.get("package", "")
        if not package.strip():
            return {"success": False, "error": "Nombre de paquete vacío"}

        # Sanitizar: solo permitir nombre de paquete + versión
        if not re.match(r'^[a-zA-Z0-9_\-\.\[\]]+([<>=!]+[a-zA-Z0-9_\-\.]+)?$', package):
            return {"success": False, "error": f"Nombre de paquete inválido: {package}"}

        command = f"{sys.executable} -m pip install {package}"
        logger.info("system.pip_install", package=package)

        # Reusar exec_command
        return await self.exec_command(Event(
            name="system.exec",
            data={"command": command, "timeout": 120},
            source="system",
        ))

    # ── Security helpers ─────────────────────────────────────────────

    def _check_command_safety(self, command: str) -> dict:
        """Verifica si un comando es seguro de ejecutar."""
        cmd_lower = command.lower().strip()

        # Bloquear comandos peligrosos
        for pattern in BLOCKED_COMMANDS:
            if re.search(pattern, cmd_lower):
                return {"allowed": False, "reason": f"Comando peligroso detectado (pattern: {pattern})"}

        # Bloquear encadenamiento con comandos peligrosos
        # Separar por ; | && || y verificar cada parte
        parts = re.split(r'[;|&]+', cmd_lower)
        for part in parts:
            part = part.strip()
            for pattern in BLOCKED_COMMANDS:
                if re.search(pattern, part):
                    return {"allowed": False, "reason": f"Sub-comando peligroso: {part[:50]}"}

        # Advertir sobre comandos sensibles
        warn = None
        for pattern in WARN_COMMANDS:
            if re.search(pattern, cmd_lower):
                warn = f"Comando sensible: {command[:50]}"
                break

        return {"allowed": True, "warn": warn}

    def _is_path_allowed(self, path: Path) -> bool:
        """Verifica que un path esté dentro del directorio permitido."""
        try:
            path.resolve().relative_to(self._allowed_root)
            return True
        except ValueError:
            return False
