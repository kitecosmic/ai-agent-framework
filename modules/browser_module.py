"""
Browser Module — Navegación web con Playwright (async).

Features:
- Pool de browsers para concurrencia
- Screenshots, PDF export
- Interceptación de network requests
- Extracción de datos estructurados
- Stealth mode para evitar detección
"""
from __future__ import annotations

import asyncio
import base64
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from core.event_bus import Event, event_bus
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()


@dataclass
class BrowseResult:
    """Resultado de una operación de navegación."""
    url: str
    status: int
    title: str = ""
    content: str = ""
    html: str = ""
    screenshot: bytes | None = None
    extracted_data: dict[str, Any] = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    error: str | None = None


class BrowserPool:
    """
    Pool de browsers Playwright para manejar concurrencia.
    Reutiliza contextos y limita instancias simultáneas.
    """

    def __init__(self, max_concurrent: int = 5, headless: bool = True, timeout: int = 30_000):
        self._max_concurrent = max_concurrent
        self._headless = headless
        self._timeout = timeout
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        logger.info("browser_pool.started", headless=self._headless, max_concurrent=self._max_concurrent)

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("browser_pool.stopped")

    @asynccontextmanager
    async def get_page(self, stealth: bool = True):
        """Context manager que provee una página del pool."""
        import random

        # Rotar user-agents para reducir detección
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        ]
        # Variar resolución ligeramente
        widths = [1920, 1536, 1440, 1366]
        heights = [1080, 864, 900, 768]
        idx = random.randint(0, len(widths) - 1)

        async with self._semaphore:
            context = await self._browser.new_context(
                viewport={"width": widths[idx], "height": heights[idx]},
                user_agent=random.choice(user_agents),
                java_script_enabled=True,
                locale="es-AR",
                timezone_id="America/Argentina/Buenos_Aires",
                extra_http_headers={
                    "Accept-Language": "es-AR,es;q=0.9,en;q=0.5",
                    "Sec-CH-UA-Platform": '"Windows"',
                },
            )

            if stealth:
                await self._apply_stealth(context)

            page = await context.new_page()
            page.set_default_timeout(self._timeout)

            try:
                yield page
            finally:
                await page.close()
                await context.close()

    async def _apply_stealth(self, context: BrowserContext):
        """Aplica técnicas stealth para evitar detección de bot."""
        await context.add_init_script("""
            // Override navigator.webdriver
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // Override chrome runtime
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };

            // Override permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) =>
                parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters);

            // Override plugins (simular plugins reales)
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const plugins = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin' },
                    ];
                    plugins.length = 3;
                    return plugins;
                },
            });

            // Override languages (español Argentina)
            Object.defineProperty(navigator, 'languages', {
                get: () => ['es-AR', 'es', 'en'],
            });

            // Override platform
            Object.defineProperty(navigator, 'platform', {
                get: () => 'Win32',
            });

            // Override hardwareConcurrency
            Object.defineProperty(navigator, 'hardwareConcurrency', {
                get: () => 8,
            });

            // Override deviceMemory
            Object.defineProperty(navigator, 'deviceMemory', {
                get: () => 8,
            });

            // Evitar detección por WebGL renderer
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter.call(this, parameter);
            };
        """)


class BrowserModule(PluginBase):
    """
    Módulo de navegación web — plugin principal del browser.

    Eventos que emite:
    - browser.navigated      → después de navegar a una URL
    - browser.data_extracted → después de extraer datos
    - browser.screenshot     → después de tomar screenshot
    - browser.error          → en caso de error

    Eventos que escucha:
    - browser.navigate       → solicitud de navegación
    - browser.extract        → solicitud de extracción
    - browser.screenshot     → solicitud de screenshot
    """

    name = "browser"
    version = "1.0.0"
    description = "Web browsing and data extraction via Playwright"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pool: BrowserPool | None = None

    async def on_load(self):
        self.pool = BrowserPool(
            max_concurrent=self.config.get("browser_max_concurrent", 5),
            headless=self.config.get("browser_headless", True),
            timeout=self.config.get("browser_timeout", 30_000),
        )
        try:
            await self.pool.start()
            logger.info("browser_module.loaded")
        except Exception as exc:
            logger.warning("browser_module.start_failed", error=str(exc),
                           hint="Browser features disabled. Install Playwright browsers with: playwright install chromium")
            self.pool = None

    async def on_unload(self):
        if self.pool:
            await self.pool.stop()

    # ── Event Handlers ───────────────────────────────────────────

    def _check_pool(self, url: str) -> BrowseResult | None:
        """Retorna un BrowseResult de error si el pool no está disponible."""
        if not self.pool:
            return BrowseResult(url=url, status=0, error="Browser module not available. Run: playwright install chromium")
        return None

    @hook("browser.navigate")
    async def handle_navigate(self, event: Event) -> BrowseResult:
        """Navega a una URL y retorna el resultado."""
        url = event.data.get("url", "")
        if err := self._check_pool(url):
            return err
        wait_for = event.data.get("wait_for", "networkidle")
        # Sanitizar: Playwright solo acepta estos valores
        valid_wait = ("load", "domcontentloaded", "networkidle", "commit")
        if wait_for not in valid_wait:
            wait_for = "networkidle"
        extract_links = event.data.get("extract_links", False)

        return await self.navigate(url, wait_for=wait_for, extract_links=extract_links)

    @hook("browser.extract")
    async def handle_extract(self, event: Event) -> BrowseResult:
        """Navega y extrae datos con selectores CSS."""
        url = event.data.get("url", "")
        if not url:
            return BrowseResult(url="", status=0, error="URL is required for browser.extract")
        if err := self._check_pool(url):
            return err
        selectors = event.data.get("selectors", {})
        return await self.extract_data(url, selectors)

    @hook("browser.take_screenshot")
    async def handle_screenshot(self, event: Event) -> BrowseResult:
        """Toma un screenshot de una URL."""
        url = event.data.get("url", "")
        if err := self._check_pool(url):
            return err
        full_page = event.data.get("full_page", True)
        return await self.screenshot(url, full_page=full_page)

    @hook("browser.search")
    async def handle_search(self, event: Event) -> BrowseResult:
        """Búsqueda web inteligente via Google con Playwright."""
        query = event.data.get("query", "")
        if not query:
            return BrowseResult(url="", status=0, error="Query is required for browser.search")
        if err := self._check_pool("google.com"):
            return err
        return await self.web_search(query)

    async def web_search(self, query: str, max_results: int = 8) -> BrowseResult:
        """
        Búsqueda web multi-engine con fallback.
        Intenta Google primero, luego Bing, luego DuckDuckGo HTML.
        """
        # Limpiar query: eliminar fechas (dd/mm/yyyy, yyyy-mm-dd) que ensucian resultados
        import re as _re
        query = _re.sub(r'\b\d{1,2}/\d{1,2}/\d{4}\b', '', query)
        query = _re.sub(r'\b\d{4}-\d{2}-\d{2}\b', '', query)
        query = _re.sub(r'\s{2,}', ' ', query).strip()

        # Orden de motores: DuckDuckGo → Google → Bing
        # (noticias/tendencias se manejan por el plugin news_aggregator con news.search)
        engines = [
            ("duckduckgo", self._search_duckduckgo),
            ("google", self._search_google),
            ("bing", self._search_bing),
        ]

        for engine_name, engine_fn in engines:
            try:
                result = await engine_fn(query, max_results)
                search_results = result.extracted_data.get("search_results", [])
                if search_results or result.extracted_data.get("featured_snippet"):
                    # Verificar diversidad de resultados (no todos del mismo dominio basura)
                    if search_results and self._results_are_garbage(search_results):
                        logger.warning(
                            "browser.search_garbage_results",
                            engine=engine_name,
                            query=query,
                            reason="all results from same irrelevant domain",
                        )
                        continue  # Probar siguiente motor

                    logger.info(
                        "browser.search_complete",
                        engine=engine_name,
                        query=query,
                        results_count=len(search_results),
                        has_featured=bool(result.extracted_data.get("featured_snippet")),
                    )
                    # Visitar primer resultado para contenido detallado
                    if search_results:
                        result = await self._enrich_first_result(result, search_results)
                    return result
                else:
                    logger.warning(
                        "browser.search_no_results",
                        engine=engine_name,
                        query=query,
                        page_title=result.title,
                    )
            except Exception as exc:
                logger.warning(
                    "browser.search_engine_failed",
                    engine=engine_name,
                    query=query,
                    error=str(exc),
                )

        # Si todos los motores fallan, intentar Wikipedia directamente
        logger.info("browser.search_fallback_wikipedia", query=query)
        return await self._search_wikipedia(query)

    def _results_are_garbage(self, search_results: list) -> bool:
        """Detecta si los resultados son basura (todos del mismo dominio irrelevante)."""
        if len(search_results) < 3:
            return False
        # Extraer dominios
        domains = []
        for r in search_results:
            url = r.get("url", "")
            try:
                domain = urllib.parse.urlparse(url).netloc.replace("www.", "")
                if domain:
                    domains.append(domain)
            except Exception:
                pass
        if not domains:
            return False
        # Si >60% de resultados son del mismo dominio, probablemente es basura
        from collections import Counter
        most_common_domain, count = Counter(domains).most_common(1)[0]
        ratio = count / len(domains)
        if ratio > 0.6:
            logger.warning("browser.garbage_detection", domain=most_common_domain, ratio=f"{ratio:.0%}")
            return True
        return False

    async def _handle_google_consent(self, page) -> bool:
        """Detecta y maneja la página de consentimiento de cookies de Google."""
        try:
            # Detectar consent form por varios indicadores
            consent_selectors = [
                'form[action*="consent"]',
                'button#L2AGLb',           # "Acepto" button
                '[aria-label*="Accept"]',
                '[aria-label*="Aceptar"]',
                'button:has-text("Accept all")',
                'button:has-text("Aceptar todo")',
                'button:has-text("Rechazar todo")',  # También vale rechazar
            ]
            for sel in consent_selectors:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        logger.debug("browser.google_consent_detected", selector=sel)
                        await btn.click()
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                        return True
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("browser.consent_check_error", error=str(exc))
        return False

    async def _search_google(self, query: str, max_results: int) -> BrowseResult:
        """Búsqueda via Google."""
        search_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}&hl=es&gl=ar"

        async with self.pool.get_page() as page:
            response = await page.goto(search_url, wait_until="domcontentloaded")
            page_title = await page.title()
            logger.debug("browser.google_loaded", title=page_title, status=response.status if response else 0)

            # Manejar consent page
            if await self._handle_google_consent(page):
                logger.debug("browser.google_consent_accepted")
                # Reintentar la búsqueda después de aceptar cookies
                response = await page.goto(search_url, wait_until="domcontentloaded")
                page_title = await page.title()
                logger.debug("browser.google_reloaded", title=page_title)

            # Esperar resultados
            try:
                await page.wait_for_selector("#search, #rso, .g", timeout=8_000)
            except Exception:
                # Log lo que hay en la página para debug
                body_text = await page.inner_text("body")
                logger.debug(
                    "browser.google_no_search_div",
                    title=page_title,
                    body_preview=body_text[:500],
                )

            # Extraer resultados con selectores robustos
            results = await page.evaluate("""
                () => {
                    const items = [];
                    
                    // Múltiples estrategias de extracción
                    // Estrategia 1: div.g estándar
                    document.querySelectorAll('div.g').forEach(el => {
                        const link = el.querySelector('a[href^="http"]');
                        const title = el.querySelector('h3');
                        const snippet = el.querySelector(
                            '.VwiC3b, [data-sncf], [style*="-webkit-line-clamp"], .lEBKkf, span.st'
                        );
                        if (title && link) {
                            items.push({
                                title: title.innerText,
                                url: link.href,
                                snippet: snippet ? snippet.innerText : ''
                            });
                        }
                    });
                    
                    // Estrategia 2: si no funcionó, buscar h3 + ancestor con link
                    if (items.length === 0) {
                        document.querySelectorAll('h3').forEach(h3 => {
                            const parent = h3.closest('a') || h3.parentElement?.querySelector('a');
                            if (parent && parent.href && parent.href.startsWith('http') 
                                && !parent.href.includes('google.com')) {
                                const container = h3.closest('[data-sokoban-container], [data-hveid], .g, [jscontroller]');
                                const snippet = container ? 
                                    container.querySelector('[data-sncf], .VwiC3b, span[style]') : null;
                                items.push({
                                    title: h3.innerText,
                                    url: parent.href,
                                    snippet: snippet ? snippet.innerText : ''
                                });
                            }
                        });
                    }

                    // Estrategia 3: cualquier link externo con texto sustancial
                    if (items.length === 0) {
                        document.querySelectorAll('a[href^="http"]').forEach(a => {
                            if (!a.href.includes('google.com') && 
                                !a.href.includes('accounts.google') &&
                                !a.href.includes('support.google') &&
                                a.innerText.trim().length > 10) {
                                items.push({
                                    title: a.innerText.trim().substring(0, 200),
                                    url: a.href,
                                    snippet: ''
                                });
                            }
                        });
                    }
                    
                    // Featured snippet / knowledge panel
                    const featured = document.querySelector(
                        '[data-attrid="description"], .hgKElc, .kno-rdesc, .IZ6rdc, .kp-header'
                    );
                    
                    // Texto visible completo para debug
                    const bodyText = document.body.innerText.substring(0, 2000);
                    
                    return { 
                        items: items.slice(0, 8), 
                        featured: featured ? featured.innerText : '',
                        bodyPreview: bodyText
                    };
                }
            """)

            search_results = results.get("items", [])
            featured = results.get("featured", "")

            logger.debug(
                "browser.google_extracted",
                results_count=len(search_results),
                has_featured=bool(featured),
                body_preview=results.get("bodyPreview", "")[:300],
            )

            return self._build_search_result(
                search_url, response, query, search_results, featured, "Google"
            )

    @staticmethod
    def _resolve_bing_url(url: str) -> str:
        """Resuelve URLs de tracking de Bing (bing.com/ck/a?...) a la URL real."""
        if "bing.com/ck/a" not in url:
            return url
        try:
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            # El parámetro 'u' contiene la URL real codificada en base64 con prefijo 'a1'
            if "u" in params:
                encoded = params["u"][0]
                # Quitar prefijo 'a1' que Bing agrega
                if encoded.startswith("a1"):
                    encoded = encoded[2:]
                # Agregar padding base64 si falta
                padding = 4 - len(encoded) % 4
                if padding != 4:
                    encoded += "=" * padding
                decoded = base64.b64decode(encoded).decode("utf-8")
                if decoded.startswith("http"):
                    return decoded
        except Exception:
            pass
        return url

    async def _search_bing(self, query: str, max_results: int) -> BrowseResult:
        """Búsqueda via Bing (fallback)."""
        search_url = f"https://www.bing.com/search?q={urllib.parse.quote_plus(query)}&setlang=es"

        async with self.pool.get_page() as page:
            response = await page.goto(search_url, wait_until="domcontentloaded")
            page_title = await page.title()
            logger.debug("browser.bing_loaded", title=page_title)

            try:
                await page.wait_for_selector("#b_results, .b_algo", timeout=8_000)
            except Exception:
                pass

            results = await page.evaluate("""
                () => {
                    const items = [];
                    
                    // Resultados de Bing
                    document.querySelectorAll('.b_algo, li.b_algo').forEach(el => {
                        const link = el.querySelector('a[href^="http"]');
                        const title = el.querySelector('h2, h2 a');
                        const snippet = el.querySelector('.b_caption p, .b_lineclamp2, .b_lineclamp3');
                        if (title && link) {
                            items.push({
                                title: title.innerText,
                                url: link.href,
                                snippet: snippet ? snippet.innerText : ''
                            });
                        }
                    });
                    
                    const featured = document.querySelector('.b_ans .b_vPanel, .b_entityTP');
                    return { 
                        items: items.slice(0, 8), 
                        featured: featured ? featured.innerText.substring(0, 1000) : '' 
                    };
                }
            """)

            search_results = results.get("items", [])
            featured = results.get("featured", "")

            # Resolver URLs de tracking de Bing a URLs reales
            for r in search_results:
                original = r.get("url", "")
                resolved = self._resolve_bing_url(original)
                if resolved != original:
                    logger.debug("browser.bing_url_resolved", original=original[:80], resolved=resolved[:80])
                r["url"] = resolved

            logger.debug("browser.bing_extracted", results_count=len(search_results))

            return self._build_search_result(
                search_url, response, query, search_results, featured, "Bing"
            )

    async def _search_duckduckgo(self, query: str, max_results: int) -> BrowseResult:
        """Búsqueda via DuckDuckGo HTML (último fallback antes de Wikipedia)."""
        search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}"

        async with self.pool.get_page() as page:
            response = await page.goto(search_url, wait_until="domcontentloaded")
            page_title = await page.title()
            logger.debug("browser.ddg_loaded", title=page_title)

            # Esperar a que aparezcan resultados
            try:
                await page.wait_for_selector(".result, .web-result, .results .result__a, .links_main", timeout=5_000)
            except Exception:
                pass

            results = await page.evaluate("""
                () => {
                    const items = [];

                    // Estrategia 1: selectores clásicos DDG HTML
                    document.querySelectorAll('.result, .web-result, .links_main .result').forEach(el => {
                        const link = el.querySelector('a.result__a, a.result__url, a[href^="http"]');
                        const snippet = el.querySelector('.result__snippet, a.result__snippet, .result__body');
                        if (link && link.innerText.trim()) {
                            let url = link.href || '';
                            // DDG tracking URLs: //duckduckgo.com/l/?uddg=ENCODED_URL
                            if (url.includes('duckduckgo.com/l/')) {
                                try {
                                    const u = new URL(url);
                                    const real = u.searchParams.get('uddg');
                                    if (real) url = decodeURIComponent(real);
                                } catch(e) {}
                            }
                            items.push({
                                title: link.innerText.trim(),
                                url: url,
                                snippet: snippet ? snippet.innerText.trim() : ''
                            });
                        }
                    });

                    // Estrategia 2: si no hay resultados, buscar cualquier link sustancial
                    if (items.length === 0) {
                        document.querySelectorAll('a[href^="http"]').forEach(a => {
                            const text = a.innerText.trim();
                            const href = a.href;
                            if (text.length > 15 && !href.includes('duckduckgo.com') && !href.includes('javascript:')) {
                                items.push({ title: text, url: href, snippet: '' });
                            }
                        });
                    }

                    // Debug: capturar HTML si no hay resultados
                    const bodyPreview = items.length === 0 ? document.body.innerText.substring(0, 500) : '';
                    return { items: items.slice(0, 8), featured: '', bodyPreview: bodyPreview };
                }
            """)

            search_results = results.get("items", [])
            if not search_results:
                logger.debug("browser.ddg_no_results_debug", body_preview=results.get("bodyPreview", "")[:300])
            logger.debug("browser.ddg_extracted", results_count=len(search_results))

            return self._build_search_result(
                search_url, response, query, search_results, "", "DuckDuckGo"
            )

    async def _search_wikipedia(self, query: str) -> BrowseResult:
        """Búsqueda directa en Wikipedia como último recurso."""
        wiki_url = f"https://es.wikipedia.org/wiki/{urllib.parse.quote(query.replace(' ', '_'))}"

        try:
            async with self.pool.get_page() as page:
                response = await page.goto(wiki_url, wait_until="domcontentloaded")
                page_title = await page.title()

                # Si redirige a búsqueda de Wikipedia, extraer primer resultado
                if "buscar" in page_title.lower() or response.status == 404:
                    search_wiki = f"https://es.wikipedia.org/w/index.php?search={urllib.parse.quote_plus(query)}"
                    await page.goto(search_wiki, wait_until="domcontentloaded")
                    first_link = await page.query_selector(".mw-search-results a")
                    if first_link:
                        await first_link.click()
                        await page.wait_for_load_state("domcontentloaded")

                title = await page.title()
                content = await page.inner_text("#mw-content-text")
                content = content[:5_000] if content else ""

                logger.info("browser.wikipedia_result", title=title, content_length=len(content))

                return BrowseResult(
                    url=page.url,
                    status=response.status if response else 0,
                    title=title,
                    content=f"**Fuente: Wikipedia**\n\n{content}",
                    extracted_data={
                        "search_results": [{"title": title, "url": page.url, "snippet": content[:300]}],
                        "featured_snippet": content[:500],
                        "source": "wikipedia",
                    },
                )
        except Exception as exc:
            logger.error("browser.wikipedia_error", query=query, error=str(exc))
            return BrowseResult(
                url=wiki_url, status=0,
                error=f"No pude buscar en ningún motor. Último error: {str(exc)}"
            )

    async def _enrich_first_result(self, result: BrowseResult, search_results: list) -> BrowseResult:
        """Visita el primer resultado de búsqueda para obtener contenido detallado."""
        # Buscar la primera URL válida (no tracking, no redirect)
        first_url = ""
        for sr in search_results:
            url = sr.get("url", "")
            if url and not any(skip in url for skip in (
                "bing.com/ck/", "google.com/url", "duckduckgo.com/l/",
                "youtube.com", "facebook.com", "twitter.com", "instagram.com",
            )):
                first_url = url
                break

        if not first_url:
            logger.debug("browser.search_enrich_skipped", reason="no valid URL found in results")
            return result

        logger.debug("browser.search_enriching", url=first_url)

        try:
            async with self.pool.get_page() as page:
                resp = await page.goto(first_url, wait_until="domcontentloaded", timeout=15_000)
                final_url = page.url  # URL real después de redirects
                detailed = await page.inner_text("body")
                detailed = detailed[:5_000]

                if len(detailed.strip()) < 100:
                    logger.debug("browser.search_enrich_too_short", url=final_url, length=len(detailed))
                    return result

                result.content += f"\n\n**Contenido detallado de {final_url}:**\n{detailed}"
                result.extracted_data["first_result_url"] = final_url
                result.extracted_data["detailed_content_length"] = len(detailed)
                logger.info("browser.search_enriched", url=final_url, content_length=len(detailed))
        except Exception as exc:
            logger.warning("browser.search_enrich_failed", url=first_url, error=str(exc))

        return result

    def _build_search_result(
        self, search_url, response, query, search_results, featured, engine
    ) -> BrowseResult:
        """Construye un BrowseResult formateado a partir de resultados de búsqueda."""
        content_parts = []
        if featured:
            content_parts.append(f"**Resumen destacado ({engine}):**\n{featured}\n")

        content_parts.append(f"**Resultados de búsqueda ({engine}) para: {query}**\n")
        for i, r in enumerate(search_results, 1):
            content_parts.append(
                f"{i}. {r.get('title', 'Sin título')}\n"
                f"   URL: {r.get('url', '')}\n"
                f"   {r.get('snippet', '')}\n"
            )

        return BrowseResult(
            url=search_url,
            status=response.status if response else 0,
            title=f"Búsqueda: {query}",
            content="\n".join(content_parts),
            extracted_data={
                "search_results": search_results,
                "featured_snippet": featured,
                "engine": engine,
            },
        )

    # ── Core Methods ─────────────────────────────────────────────

    async def navigate(
        self,
        url: str,
        wait_for: str = "networkidle",
        extract_links: bool = False,
    ) -> BrowseResult:
        """Navega a una URL y extrae contenido básico."""
        try:
            async with self.pool.get_page() as page:
                response = await page.goto(url, wait_until=wait_for)

                title = await page.title()
                content = await page.inner_text("body")
                html = await page.content()

                links = []
                if extract_links:
                    link_elements = await page.query_selector_all("a[href]")
                    for link in link_elements:
                        href = await link.get_attribute("href")
                        if href:
                            links.append(href)

                result = BrowseResult(
                    url=url,
                    status=response.status if response else 0,
                    title=title,
                    content=content[:50_000],  # Limitar tamaño
                    html=html[:100_000],
                    links=links,
                )

                await self.bus.emit(Event(
                    name="browser.navigated",
                    data={"url": url, "title": title, "status": result.status},
                    source="browser",
                ))

                return result

        except Exception as exc:
            error_msg = str(exc)
            logger.error("browser.navigate_error", url=url, error=error_msg)
            await self.bus.emit(Event(
                name="browser.error",
                data={"url": url, "error": error_msg},
                source="browser",
            ))
            return BrowseResult(url=url, status=0, error=error_msg)

    async def extract_data(self, url: str, selectors: dict[str, str]) -> BrowseResult:
        """
        Navega a una URL y extrae datos usando selectores CSS.

        Ejemplo:
            selectors = {
                "title": "h1",
                "prices": ".price::text",
                "images": "img::attr(src)",
            }
        """
        try:
            async with self.pool.get_page() as page:
                response = await page.goto(url, wait_until="networkidle")

                extracted = {}
                for key, selector in selectors.items():
                    # Soporte para pseudo-selectores
                    if "::text" in selector:
                        sel = selector.replace("::text", "")
                        elements = await page.query_selector_all(sel)
                        extracted[key] = [await el.inner_text() for el in elements]
                    elif "::attr(" in selector:
                        sel, attr = selector.split("::attr(")
                        attr = attr.rstrip(")")
                        elements = await page.query_selector_all(sel)
                        extracted[key] = [await el.get_attribute(attr) for el in elements]
                    else:
                        elements = await page.query_selector_all(selector)
                        extracted[key] = [await el.inner_text() for el in elements]

                result = BrowseResult(
                    url=url,
                    status=response.status if response else 0,
                    title=await page.title(),
                    extracted_data=extracted,
                )

                await self.bus.emit(Event(
                    name="browser.data_extracted",
                    data={"url": url, "selectors": list(selectors.keys()), "results": extracted},
                    source="browser",
                ))

                return result

        except Exception as exc:
            logger.error("browser.extract_error", url=url, error=str(exc))
            return BrowseResult(url=url, status=0, error=str(exc))

    async def screenshot(self, url: str, full_page: bool = True) -> BrowseResult:
        """Toma un screenshot de una página."""
        try:
            async with self.pool.get_page() as page:
                response = await page.goto(url, wait_until="networkidle")
                screenshot_bytes = await page.screenshot(full_page=full_page)

                return BrowseResult(
                    url=url,
                    status=response.status if response else 0,
                    title=await page.title(),
                    screenshot=screenshot_bytes,
                )
        except Exception as exc:
            return BrowseResult(url=url, status=0, error=str(exc))

    async def run_script(self, url: str, script: str) -> Any:
        """Ejecuta JavaScript arbitrario en una página."""
        async with self.pool.get_page() as page:
            await page.goto(url, wait_until="networkidle")
            return await page.evaluate(script)

    async def fill_form(self, url: str, fields: dict[str, str], submit_selector: str | None = None) -> BrowseResult:
        """Llena un formulario y opcionalmente lo envía."""
        async with self.pool.get_page() as page:
            await page.goto(url, wait_until="networkidle")

            for selector, value in fields.items():
                await page.fill(selector, value)

            if submit_selector:
                await page.click(submit_selector)
                await page.wait_for_load_state("networkidle")

            return BrowseResult(
                url=page.url,
                status=200,
                title=await page.title(),
                content=await page.inner_text("body"),
            )

    async def intercept_requests(
        self,
        url: str,
        patterns: list[str] | None = None,
    ) -> list[dict]:
        """
        Navega e intercepta requests de red que matcheen los patrones.
        Útil para capturar APIs internas.
        """
        captured: list[dict] = []

        async with self.pool.get_page() as page:
            async def on_response(response):
                req_url = response.url
                if patterns is None or any(p in req_url for p in patterns):
                    try:
                        body = await response.text()
                    except Exception:
                        body = ""
                    captured.append({
                        "url": req_url,
                        "status": response.status,
                        "method": response.request.method,
                        "body": body[:10_000],
                    })

            page.on("response", on_response)
            await page.goto(url, wait_until="networkidle")

        return captured
