"""
HTTP Module — Cliente HTTP async para APIs y requests genéricos.

Features:
- Rate limiting
- Retry con backoff exponencial
- Response caching
- Request/Response logging
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from core.event_bus import Event, event_bus
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()


@dataclass
class HTTPResult:
    url: str
    method: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    elapsed_ms: float = 0
    error: str | None = None
    from_cache: bool = False


class HTTPModule(PluginBase):
    """
    Módulo para HTTP requests con retry, caching y rate limiting.

    Eventos:
    - http.request  → solicitud de request
    - http.response → respuesta recibida
    - http.error    → error en request
    """

    name = "http"
    version = "1.0.0"
    description = "Async HTTP client with retry, caching, and rate limiting"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, HTTPResult]] = {}
        self._cache_ttl: int = 300  # 5 minutos default
        self._rate_limit_delay: float = 0.1  # 100ms entre requests
        self._last_request_time: float = 0

    async def on_load(self):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    async def on_unload(self):
        if self._client:
            await self._client.aclose()

    @hook("http.request")
    async def handle_request(self, event: Event) -> HTTPResult:
        """Handler para requests vía event bus."""
        url = event.data.get("url", "")
        method = event.data.get("method", "GET")
        if not url:
            return HTTPResult(url="", method=method, status=0, error="URL is required")
        logger.info("http.requesting", method=method, url=url)
        return await self.request(
            method=method,
            url=url,
            headers=event.data.get("headers"),
            json_data=event.data.get("json"),
            params=event.data.get("params"),
            use_cache=event.data.get("cache", False),
            retries=event.data.get("retries", 1),
        )

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        json_data: Any = None,
        data: Any = None,
        params: dict[str, str] | None = None,
        use_cache: bool = False,
        retries: int = 3,
        backoff_factor: float = 1.0,
    ) -> HTTPResult:
        """Ejecuta un HTTP request con retry y caching."""

        # Check cache
        if use_cache and method.upper() == "GET":
            cache_key = self._cache_key(method, url, params)
            cached = self._get_cached(cache_key)
            if cached:
                return cached

        # Rate limiting
        await self._rate_limit()

        # Retry loop
        last_error = None
        for attempt in range(retries + 1):
            try:
                start = time.monotonic()
                response = await self._client.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    json=json_data,
                    data=data,
                    params=params,
                )
                elapsed = (time.monotonic() - start) * 1000

                # Parse body
                body: Any
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    body = response.json()
                else:
                    body = response.text

                result = HTTPResult(
                    url=str(response.url),
                    method=method.upper(),
                    status=response.status_code,
                    headers=dict(response.headers),
                    body=body,
                    elapsed_ms=elapsed,
                )

                # Cache si corresponde
                if use_cache and method.upper() == "GET" and response.status_code == 200:
                    self._set_cached(self._cache_key(method, url, params), result)

                # Emitir evento
                await self.bus.emit(Event(
                    name="http.response",
                    data={
                        "url": url,
                        "method": method,
                        "status": result.status,
                        "elapsed_ms": elapsed,
                    },
                    source="http",
                ))

                return result

            except Exception as exc:
                last_error = str(exc)
                if attempt < retries:
                    wait_time = backoff_factor * (2 ** attempt)
                    logger.warning(
                        "http.retry",
                        url=url,
                        attempt=attempt + 1,
                        wait=wait_time,
                        error=last_error,
                    )
                    await asyncio.sleep(wait_time)

        # Todos los intentos fallaron
        error_result = HTTPResult(
            url=url, method=method.upper(), status=0, error=last_error
        )
        await self.bus.emit(Event(
            name="http.error",
            data={"url": url, "error": last_error},
            source="http",
        ))
        return error_result

    # Shortcuts
    async def get(self, url: str, **kwargs) -> HTTPResult:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, json_data: Any = None, **kwargs) -> HTTPResult:
        return await self.request("POST", url, json_data=json_data, **kwargs)

    async def put(self, url: str, json_data: Any = None, **kwargs) -> HTTPResult:
        return await self.request("PUT", url, json_data=json_data, **kwargs)

    async def delete(self, url: str, **kwargs) -> HTTPResult:
        return await self.request("DELETE", url, **kwargs)

    # ── Cache helpers ────────────────────────────────────────────

    def _cache_key(self, method: str, url: str, params: dict | None) -> str:
        raw = f"{method}:{url}:{json.dumps(params or {}, sort_keys=True)}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _get_cached(self, key: str) -> HTTPResult | None:
        if key in self._cache:
            ts, result = self._cache[key]
            if time.time() - ts < self._cache_ttl:
                result.from_cache = True
                return result
            del self._cache[key]
        return None

    def _set_cached(self, key: str, result: HTTPResult):
        self._cache[key] = (time.time(), result)

    # ── Rate limiting ────────────────────────────────────────────

    async def _rate_limit(self):
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._rate_limit_delay:
            await asyncio.sleep(self._rate_limit_delay - elapsed)
        self._last_request_time = time.time()
