"""
RapiBase Module — Integración con RapiBase (github.com/kitecosmic/rapibase).

RapiBase es un Backend-as-a-Service que provee:
- REST API CRUD sobre tablas de base de datos
- Autenticación (signup, signin, magic links, reset de contraseña)
- Storage S3-compatible (MinIO)

## Claves de API
- Service Key (RAPIBASE_SERVICE_KEY): acceso total sin JWT — para el agente (backend)
- Anon Key (RAPIBASE_ANON_KEY): acceso público, requiere JWT — para apps del usuario

El agente usa el Service Key por defecto para todas sus operaciones.

## Endpoints
- REST:    /api/v1/rest/{table}
- Auth:    /api/v1/auth/*
- Storage: /api/v1/storage/{bucket}/*

Configurar en .env:
  RAPIBASE_URL=http://tu-servidor:8080
  RAPIBASE_SERVICE_KEY=tu-service-key
  RAPIBASE_ANON_KEY=tu-anon-key  (opcional)

Eventos disponibles:
━━━ DATOS (REST API) ━━━
- rapibase.select       → GET registros de una tabla (con filtros y paginación)
- rapibase.insert       → POST insertar un registro
- rapibase.update       → PUT actualizar un registro por ID
- rapibase.delete       → DELETE eliminar un registro por ID

━━━ AUTH ━━━
- rapibase.auth_signup          → Registrar nuevo usuario
- rapibase.auth_signin          → Iniciar sesión
- rapibase.auth_magic_link      → Enviar magic link por email
- rapibase.auth_forgot_password → Enviar email de reset de contraseña
- rapibase.auth_reset_password  → Aplicar nueva contraseña con token

━━━ STORAGE ━━━
- rapibase.storage_list    → Listar archivos en un bucket
- rapibase.storage_delete  → Eliminar un archivo
- rapibase.storage_search  → Buscar archivos por metadata
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import structlog

from core.event_bus import Event
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()


class RapibaseModule(PluginBase):
    """Módulo de integración con RapiBase."""

    name = "rapibase"
    version = "2.0.0"
    description = "RapiBase BaaS — REST CRUD, Auth, and Storage integration"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._base_url: str = ""
        self._service_key: str = ""
        self._anon_key: str = ""
        self._client: httpx.AsyncClient | None = None
        self._enabled: bool = False

    async def on_load(self):
        self._base_url = self.config.get("rapibase_url", "").rstrip("/")
        # Service key tiene prioridad; fallback a api_key por compatibilidad
        self._service_key = (
            self.config.get("rapibase_service_key", "")
            or self.config.get("rapibase_api_key", "")
        )
        self._anon_key = self.config.get("rapibase_anon_key", "")

        if not self._base_url or not self._service_key:
            logger.info(
                "rapibase.disabled",
                reason="RAPIBASE_URL o RAPIBASE_SERVICE_KEY no configurados",
            )
            return

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=30.0,
        )
        self._enabled = True
        logger.info("rapibase.loaded", url=self._base_url)

    async def on_unload(self):
        if self._client:
            await self._client.aclose()

    # ── Helpers ─────────────────────────────────────────────────────

    def _service_headers(self, extra: dict | None = None) -> dict[str, str]:
        """Headers con Service Key — acceso total, no requiere JWT."""
        h = {"apikey": self._service_key, "Content-Type": "application/json"}
        if extra:
            h.update(extra)
        return h

    def _anon_headers(self, token: str | None = None) -> dict[str, str]:
        """Headers con Anon Key + JWT de usuario."""
        h = {"apikey": self._anon_key or self._service_key}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _not_configured(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": (
                "RapiBase no configurado. Agregá RAPIBASE_URL y RAPIBASE_SERVICE_KEY en .env\n"
                "O usá /config en Telegram para configurarlo."
            ),
        }

    async def _req(
        self,
        method: str,
        path: str,
        headers: dict | None = None,
        json_data: Any = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        """Ejecuta una request HTTP a RapiBase con manejo de errores."""
        if not self._client or not self._enabled:
            return self._not_configured()

        try:
            response = await self._client.request(
                method=method.upper(),
                url=path,
                headers=headers or self._service_headers(),
                json=json_data,
                params=params,
            )

            if response.status_code in (200, 201, 204):
                if response.status_code == 204 or not response.content:
                    return {"success": True, "data": None}
                try:
                    return {"success": True, "data": response.json()}
                except Exception:
                    return {"success": True, "data": response.text}

            # Error response
            try:
                err_body = response.json()
                err_msg = err_body.get("error") or err_body.get("message") or str(err_body)
            except Exception:
                err_msg = response.text[:300]

            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {err_msg}",
                "status": response.status_code,
            }

        except httpx.ConnectError:
            return {
                "success": False,
                "error": f"No se pudo conectar a RapiBase en {self._base_url}. ¿Está corriendo?",
            }
        except Exception as exc:
            logger.error("rapibase.request_error", path=path, error=str(exc))
            return {"success": False, "error": str(exc)}

    # ── REST API (CRUD) ──────────────────────────────────────────────

    @hook("rapibase.select")
    async def handle_select(self, event: Event) -> dict[str, Any]:
        """
        Consulta registros de una tabla.

        Data: {
            "table": "products",
            "filter": "status:eq:active",  # opcional — campo:op:valor (eq, ne, gt, lt, gte, lte, like)
            "page": 1,                      # opcional (default 1)
            "page_size": 50,               # opcional (default 50)
            "order_by": "created_at",      # opcional
            "order_dir": "desc"            # opcional ("asc" | "desc")
        }

        Operadores de filtro: eq (=), ne (!=), gt (>), lt (<), gte (>=), lte (<=), like (LIKE)
        Ejemplo: "price:gte:100" — productos con precio >= 100
        """
        table = event.data.get("table", "")
        if not table:
            return {"success": False, "error": "El campo 'table' es requerido"}

        params: dict[str, Any] = {}
        if event.data.get("filter"):
            params["filter"] = event.data["filter"]
        if event.data.get("page"):
            params["page"] = event.data["page"]
        if event.data.get("page_size"):
            params["page_size"] = event.data["page_size"]
        if event.data.get("order_by"):
            params["order_by"] = event.data["order_by"]
            params["order_dir"] = event.data.get("order_dir", "asc")

        result = await self._req("GET", f"/api/v1/rest/{table}", params=params or None)
        logger.info("rapibase.select", table=table, success=result.get("success"))
        return result

    @hook("rapibase.insert")
    async def handle_insert(self, event: Event) -> dict[str, Any]:
        """
        Inserta un nuevo registro en una tabla.

        Data: {
            "table": "products",
            "data": {"name": "Laptop", "price": 999.99, "stock": 10}
        }
        """
        table = event.data.get("table", "")
        data = event.data.get("data", {})
        if not table:
            return {"success": False, "error": "El campo 'table' es requerido"}
        if not data:
            return {"success": False, "error": "El campo 'data' es requerido"}

        result = await self._req("POST", f"/api/v1/rest/{table}", json_data=data)
        logger.info("rapibase.insert", table=table, success=result.get("success"))
        return result

    @hook("rapibase.update")
    async def handle_update(self, event: Event) -> dict[str, Any]:
        """
        Actualiza un registro por su ID.

        Data: {
            "table": "products",
            "id": "uuid-del-registro",
            "data": {"price": 799.99}
        }
        """
        table = event.data.get("table", "")
        record_id = event.data.get("id", "")
        data = event.data.get("data", {})
        if not table or not record_id:
            return {"success": False, "error": "Los campos 'table' e 'id' son requeridos"}
        if not data:
            return {"success": False, "error": "El campo 'data' es requerido"}

        result = await self._req("PUT", f"/api/v1/rest/{table}/{record_id}", json_data=data)
        logger.info("rapibase.update", table=table, id=record_id, success=result.get("success"))
        return result

    @hook("rapibase.delete")
    async def handle_delete(self, event: Event) -> dict[str, Any]:
        """
        Elimina un registro por su ID.

        Data: {
            "table": "products",
            "id": "uuid-del-registro"
        }
        """
        table = event.data.get("table", "")
        record_id = event.data.get("id", "")
        if not table or not record_id:
            return {"success": False, "error": "Los campos 'table' e 'id' son requeridos"}

        result = await self._req("DELETE", f"/api/v1/rest/{table}/{record_id}")
        logger.info("rapibase.delete", table=table, id=record_id, success=result.get("success"))
        return result

    # ── Auth endpoints ───────────────────────────────────────────────

    @hook("rapibase.auth_signup")
    async def handle_auth_signup(self, event: Event) -> dict[str, Any]:
        """
        Registra un nuevo usuario.

        Data: {
            "email": "user@example.com",
            "password": "securepass",
            "full_name": "John Doe"  # opcional
        }
        """
        data = {
            "email": event.data.get("email", ""),
            "password": event.data.get("password", ""),
        }
        if event.data.get("full_name"):
            data["full_name"] = event.data["full_name"]

        if not data["email"] or not data["password"]:
            return {"success": False, "error": "email y password son requeridos"}

        result = await self._req(
            "POST", "/api/v1/auth/signup",
            headers={"apikey": self._anon_key or self._service_key, "Content-Type": "application/json"},
            json_data=data,
        )
        logger.info("rapibase.auth_signup", email=data["email"], success=result.get("success"))
        return result

    @hook("rapibase.auth_signin")
    async def handle_auth_signin(self, event: Event) -> dict[str, Any]:
        """
        Inicia sesión con email y contraseña.

        Data: {"email": "user@example.com", "password": "securepass"}
        Retorna: {token, refresh_token, expires_in, user}
        """
        data = {
            "email": event.data.get("email", ""),
            "password": event.data.get("password", ""),
        }
        if not data["email"] or not data["password"]:
            return {"success": False, "error": "email y password son requeridos"}

        result = await self._req(
            "POST", "/api/v1/auth/signin",
            headers={"apikey": self._anon_key or self._service_key, "Content-Type": "application/json"},
            json_data=data,
        )
        logger.info("rapibase.auth_signin", email=data["email"], success=result.get("success"))
        return result

    @hook("rapibase.auth_magic_link")
    async def handle_auth_magic_link(self, event: Event) -> dict[str, Any]:
        """
        Envía un magic link por email para autenticación sin contraseña.

        Data: {
            "email": "user@example.com",
            "redirect_url": "https://miapp.com/callback"  # opcional
        }
        """
        data: dict[str, Any] = {"email": event.data.get("email", "")}
        if not data["email"]:
            return {"success": False, "error": "email es requerido"}
        if event.data.get("redirect_url"):
            data["redirect_url"] = event.data["redirect_url"]

        result = await self._req(
            "POST", "/api/v1/auth/magiclink",
            headers={"apikey": self._anon_key or self._service_key, "Content-Type": "application/json"},
            json_data=data,
        )
        logger.info("rapibase.auth_magic_link", email=data["email"], success=result.get("success"))
        return result

    @hook("rapibase.auth_forgot_password")
    async def handle_auth_forgot_password(self, event: Event) -> dict[str, Any]:
        """
        Envía email de reset de contraseña.

        Data: {
            "email": "user@example.com",
            "redirect_url": "https://miapp.com/reset"  # opcional
        }
        """
        data: dict[str, Any] = {"email": event.data.get("email", "")}
        if not data["email"]:
            return {"success": False, "error": "email es requerido"}
        if event.data.get("redirect_url"):
            data["redirect_url"] = event.data["redirect_url"]

        result = await self._req(
            "POST", "/api/v1/auth/forgot-password",
            headers={"apikey": self._anon_key or self._service_key, "Content-Type": "application/json"},
            json_data=data,
        )
        return result

    @hook("rapibase.auth_reset_password")
    async def handle_auth_reset_password(self, event: Event) -> dict[str, Any]:
        """
        Aplica nueva contraseña con token de reset.

        Data: {"token": "abc123...", "new_password": "newpass"}
        """
        data = {
            "token": event.data.get("token", ""),
            "new_password": event.data.get("new_password", ""),
        }
        if not data["token"] or not data["new_password"]:
            return {"success": False, "error": "token y new_password son requeridos"}

        return await self._req(
            "POST", "/api/v1/auth/reset-password",
            headers={"apikey": self._anon_key or self._service_key, "Content-Type": "application/json"},
            json_data=data,
        )

    # ── Storage API ──────────────────────────────────────────────────

    @hook("rapibase.storage_list")
    async def handle_storage_list(self, event: Event) -> dict[str, Any]:
        """
        Lista archivos en un bucket.

        Data: {
            "bucket": "images",
            "prefix": "avatars/"  # opcional — filtra por carpeta
        }
        """
        bucket = event.data.get("bucket", "")
        if not bucket:
            return {"success": False, "error": "El campo 'bucket' es requerido"}

        params = {}
        if event.data.get("prefix"):
            params["prefix"] = event.data["prefix"]

        result = await self._req(
            "GET", f"/api/v1/storage/{bucket}/list",
            params=params or None,
        )
        logger.info("rapibase.storage_list", bucket=bucket, success=result.get("success"))
        return result

    @hook("rapibase.storage_delete")
    async def handle_storage_delete(self, event: Event) -> dict[str, Any]:
        """
        Elimina un archivo del storage.

        Data: {"bucket": "images", "path": "avatars/user.png"}
        """
        bucket = event.data.get("bucket", "")
        file_path = event.data.get("path", "")
        if not bucket or not file_path:
            return {"success": False, "error": "Los campos 'bucket' y 'path' son requeridos"}

        result = await self._req("DELETE", f"/api/v1/storage/{bucket}/{file_path}")
        logger.info("rapibase.storage_delete", bucket=bucket, path=file_path, success=result.get("success"))
        return result

    @hook("rapibase.storage_search")
    async def handle_storage_search(self, event: Event) -> dict[str, Any]:
        """
        Busca archivos por metadata.

        Data: {"bucket": "images", "key": "product_id", "value": "123"}
        """
        bucket = event.data.get("bucket", "")
        key = event.data.get("key", "")
        value = event.data.get("value", "")
        if not bucket or not key:
            return {"success": False, "error": "Los campos 'bucket' y 'key' son requeridos"}

        result = await self._req(
            "GET", f"/api/v1/storage/{bucket}/search",
            params={"key": key, "value": value},
        )
        return result
