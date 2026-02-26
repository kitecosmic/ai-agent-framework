"""
Plugin: Agregador de Noticias Tech via RSS.

Busca noticias en feeds RSS de fuentes confiables sin necesidad de browser.
Usa httpx directo (los feeds RSS son XML plano, no necesitan JS).

Eventos:
- news.search → busca noticias por query (filtra por relevancia)
- news.latest → devuelve las últimas noticias sin filtro
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog

from core.event_bus import Event
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()

# ── Fuentes RSS configurables ────────────────────────────────────
# Cada fuente tiene: nombre, URL del feed, idioma, categoría
RSS_FEEDS = [
    # Español — tech general
    ("Xataka", "https://www.xataka.com/feedburner.xml", "es", "tech"),
    ("Genbeta", "https://www.genbeta.com/feedburner.xml", "es", "tech"),
    ("Hipertextual", "https://hipertextual.com/feed", "es", "tech"),
    ("WWWhat's New", "https://wwwhatsnew.com/feed/", "es", "tech,startups"),
    # LATAM — startups / negocios / innovación
    ("Contxto", "https://contxto.com/en/feed/", "en", "startups,latam"),
    ("FayerWayer", "https://www.fayerwayer.com/feed/", "es", "tech,latam"),
    ("Cointelegraph ES", "https://es.cointelegraph.com/rss", "es", "crypto,tech"),
    # Inglés — tech top-tier
    ("TechCrunch", "https://techcrunch.com/feed/", "en", "tech,startups"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab", "en", "tech"),
    ("The Verge", "https://www.theverge.com/rss/index.xml", "en", "tech"),
    ("Wired", "https://www.wired.com/feed/rss", "en", "tech"),
    # Google News RSS (búsqueda dinámica)
    ("Google News", "https://news.google.com/rss/search?q={query}&hl=es-419&gl=AR&ceid=AR:es-419", "es", "dynamic"),
]

# Timeout y límites
FEED_TIMEOUT = 10.0
MAX_ARTICLES_PER_FEED = 5
MAX_TOTAL_ARTICLES = 20


# Timezone de Argentina para referencia de "hoy"
TZ_AR = timezone(timedelta(hours=-3))


@dataclass
class NewsArticle:
    title: str
    url: str
    snippet: str
    source: str
    published: str
    language: str
    parsed_date: datetime | None = None


class NewsAggregatorPlugin(PluginBase):
    name = "news_aggregator"
    version = "1.0.0"
    description = "Agregador de noticias tech via RSS feeds (sin browser)"
    dependencies = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None

    async def on_load(self):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(FEED_TIMEOUT),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"},
        )
        logger.info("news_aggregator.loaded", feeds_count=len(RSS_FEEDS))

    async def on_unload(self):
        if self._client:
            await self._client.aclose()

    @hook("news.search")
    async def search_news(self, event: Event) -> dict[str, Any]:
        """
        Busca noticias relevantes a un query.
        Datos: {"query": "inteligencia artificial startups"}
        Retorna: {"articles": [...], "summary_text": "..."}
        """
        query = event.data.get("query", "tecnología startups innovación")
        max_results = event.data.get("max_results", MAX_TOTAL_ARTICLES)
        lang_filter = event.data.get("language", "")  # "es", "en", o "" para ambos

        logger.info("news.search_started", query=query, max_results=max_results)

        articles: list[NewsArticle] = []

        # 1. Google News RSS con el query específico
        gn_articles = await self._fetch_google_news_rss(query)
        articles.extend(gn_articles)
        logger.debug("news.google_rss_results", count=len(gn_articles))

        # 2. Feeds estáticos (siempre tienen contenido fresco)
        static_articles = await self._fetch_static_feeds(lang_filter)
        articles.extend(static_articles)
        logger.debug("news.static_feeds_results", count=len(static_articles))

        # 3. Ordenar por fecha (más recientes primero)
        articles = self._sort_by_date(articles)

        # 4. Filtrar por relevancia al query (con boost por fecha)
        if query:
            articles = self._filter_by_relevance(articles, query)

        # 5. Deduplicar por título similar
        articles = self._deduplicate(articles)

        # 6. Limitar
        articles = articles[:max_results]

        # 6. Formatear como texto para el LLM
        summary_text = self._format_articles(articles, query)

        logger.info("news.search_complete", query=query, total_articles=len(articles))

        return {
            "articles": [
                {
                    "title": a.title,
                    "url": a.url,
                    "snippet": a.snippet,
                    "source": a.source,
                    "published": a.published,
                }
                for a in articles
            ],
            "summary_text": summary_text,
            "articles_count": len(articles),
        }

    @hook("news.latest")
    async def latest_news(self, event: Event) -> dict[str, Any]:
        """Devuelve las últimas noticias sin filtro de query."""
        lang_filter = event.data.get("language", "")
        max_results = event.data.get("max_results", 15)

        articles = await self._fetch_static_feeds(lang_filter)
        articles = self._deduplicate(articles)[:max_results]
        summary_text = self._format_articles(articles, "últimas noticias tecnología")

        return {
            "articles_count": len(articles),
            "summary_text": summary_text,
        }

    # ── Fetchers ──────────────────────────────────────────────────

    async def _fetch_google_news_rss(self, query: str) -> list[NewsArticle]:
        """Fetch Google News RSS para un query específico."""
        import urllib.parse
        url = f"https://news.google.com/rss/search?q={urllib.parse.quote_plus(query)}&hl=es-419&gl=AR&ceid=AR:es-419"

        return await self._parse_rss_feed("Google News", url, "es")

    async def _fetch_static_feeds(self, lang_filter: str = "") -> list[NewsArticle]:
        """Fetch todos los feeds estáticos en paralelo."""
        import asyncio

        feed_names = []
        tasks = []
        for name, url, lang, category in RSS_FEEDS:
            if category == "dynamic":
                continue  # Google News se maneja aparte
            if lang_filter and lang != lang_filter:
                continue
            feed_names.append(name)
            tasks.append(self._parse_rss_feed(name, url, lang))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_articles = []
        for i, result in enumerate(results):
            fname = feed_names[i] if i < len(feed_names) else "unknown"
            if isinstance(result, Exception):
                logger.warning("news.feed_fetch_error", feed=fname, error=str(result))
            elif isinstance(result, list):
                all_articles.extend(result)

        return all_articles

    async def _parse_rss_feed(self, source: str, url: str, lang: str) -> list[NewsArticle]:
        """Parsea un feed RSS y devuelve artículos."""
        articles = []
        try:
            resp = await self._client.get(url)
            if resp.status_code != 200:
                logger.debug("news.feed_http_error", feed=source, status=resp.status_code)
                return []

            content = resp.text
            # Algunos feeds devuelven con BOM o encoding raro
            content = content.lstrip("\ufeff")

            try:
                root = ET.fromstring(content)
            except ET.ParseError as e:
                logger.debug("news.feed_parse_error", feed=source, error=str(e)[:100])
                return []

            # RSS 2.0: <channel><item>
            items = list(root.iter("item"))

            # Atom: <entry>
            if not items:
                atom_ns = "{http://www.w3.org/2005/Atom}"
                items = list(root.iter(f"{atom_ns}entry"))

            for item in items[:MAX_ARTICLES_PER_FEED]:
                article = self._parse_item(item, source, lang)
                if article:
                    articles.append(article)

            logger.debug("news.feed_parsed", feed=source, articles=len(articles))

        except httpx.TimeoutException:
            logger.debug("news.feed_timeout", feed=source)
        except Exception as exc:
            logger.debug("news.feed_error", feed=source, error=str(exc)[:100])

        return articles

    @staticmethod
    def _parse_date(date_str: str) -> datetime | None:
        """Parsea fechas RSS/Atom a datetime con timezone."""
        if not date_str:
            return None
        date_str = date_str.strip()
        # Formatos comunes en RSS/Atom
        formats = [
            "%a, %d %b %Y %H:%M:%S %z",     # RFC 822: Mon, 25 Feb 2026 14:30:00 +0000
            "%a, %d %b %Y %H:%M:%S %Z",     # Mon, 25 Feb 2026 14:30:00 GMT
            "%Y-%m-%dT%H:%M:%S%z",           # ISO 8601: 2026-02-25T14:30:00+00:00
            "%Y-%m-%dT%H:%M:%SZ",            # ISO 8601 UTC: 2026-02-25T14:30:00Z
            "%Y-%m-%d %H:%M:%S",             # 2026-02-25 14:30:00
            "%Y-%m-%d",                       # 2026-02-25
        ]
        # Normalizar "GMT" → "+0000" para strptime
        normalized = date_str.replace("GMT", "+0000").replace("UTC", "+0000")
        for fmt in formats:
            try:
                dt = datetime.strptime(normalized, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
        # Último intento: solo extraer la fecha YYYY-MM-DD si está presente
        m = re.search(r"(\d{4}-\d{2}-\d{2})", date_str)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return None

    @staticmethod
    def _find_el(item: ET.Element, *tags: str) -> ET.Element | None:
        """Busca el primer elemento que exista (evita bug de `or` con XML elements)."""
        for tag in tags:
            el = item.find(tag)
            if el is not None:
                return el
        return None

    @classmethod
    def _parse_item(cls, item: ET.Element, source: str, lang: str) -> NewsArticle | None:
        """Parsea un <item> RSS o <entry> Atom."""
        atom_ns = "{http://www.w3.org/2005/Atom}"

        # Título
        title_el = cls._find_el(item, "title", f"{atom_ns}title")
        if title_el is None or not title_el.text:
            return None
        title = title_el.text.strip()

        # URL
        link_el = cls._find_el(item, "link", f"{atom_ns}link")
        url = ""
        if link_el is not None:
            url = link_el.text.strip() if link_el.text else link_el.get("href", "")

        # Snippet / descripción
        desc_el = cls._find_el(item, "description", f"{atom_ns}summary", f"{atom_ns}content")
        snippet = ""
        if desc_el is not None and desc_el.text:
            snippet = re.sub(r"<[^>]+>", "", desc_el.text).strip()[:300]

        # Fecha de publicación
        pub_el = cls._find_el(item, "pubDate", f"{atom_ns}published", f"{atom_ns}updated")
        published = pub_el.text.strip() if pub_el is not None and pub_el.text else ""

        # Source override (Google News incluye <source>)
        source_el = item.find("source")
        if source_el is not None and source_el.text:
            source = source_el.text.strip()

        if not title or len(title) < 10:
            return None

        parsed_date = cls._parse_date(published)

        return NewsArticle(
            title=title,
            url=url,
            snippet=snippet,
            source=source,
            published=published,
            language=lang,
            parsed_date=parsed_date,
        )

    # ── Filtering & Formatting ────────────────────────────────────

    @staticmethod
    def _sort_by_date(articles: list[NewsArticle]) -> list[NewsArticle]:
        """Ordena artículos por fecha, más recientes primero."""
        epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
        return sorted(
            articles,
            key=lambda a: a.parsed_date if a.parsed_date else epoch,
            reverse=True,
        )

    @staticmethod
    def _filter_by_relevance(articles: list[NewsArticle], query: str) -> list[NewsArticle]:
        """Filtra artículos por relevancia al query. Score basado en keywords + fecha."""
        keywords = [kw.lower().strip() for kw in query.split() if len(kw) > 2]
        if not keywords:
            return articles

        now = datetime.now(timezone.utc)
        scored = []
        for article in articles:
            text = f"{article.title} {article.snippet}".lower()
            score = sum(1 for kw in keywords if kw in text)

            # Boost por fecha reciente
            if article.parsed_date:
                age_hours = (now - article.parsed_date).total_seconds() / 3600
                if age_hours < 24:       # Hoy → +3 puntos
                    score += 3
                elif age_hours < 48:     # Ayer → +2 puntos
                    score += 2
                elif age_hours < 168:    # Esta semana → +1 punto
                    score += 1

            if score > 0:
                scored.append((score, article))

        # Ordenar por score descendente
        scored.sort(key=lambda x: x[0], reverse=True)

        # Si hay suficientes resultados relevantes, devolver solo esos
        relevant = [a for s, a in scored if s >= 1]
        if len(relevant) >= 5:
            return relevant

        # Si no hay suficientes, incluir todos
        return [a for _, a in scored] + [a for a in articles if a not in [x[1] for x in scored]]

    @staticmethod
    def _deduplicate(articles: list[NewsArticle]) -> list[NewsArticle]:
        """Elimina artículos con títulos muy similares."""
        seen_titles: list[str] = []
        unique = []

        for article in articles:
            title_lower = article.title.lower().strip()
            # Verificar si ya hay un título muy similar
            is_dup = False
            for seen in seen_titles:
                # Si comparten más del 60% de las palabras, es duplicado
                words_a = set(title_lower.split())
                words_b = set(seen.split())
                if words_a and words_b:
                    overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
                    if overlap > 0.6:
                        is_dup = True
                        break
            if not is_dup:
                seen_titles.append(title_lower)
                unique.append(article)

        return unique

    @staticmethod
    def _format_articles(articles: list[NewsArticle], query: str) -> str:
        """Formatea artículos como texto para que el LLM resuma."""
        today = datetime.now(TZ_AR)
        today_str = today.strftime("%d/%m/%Y")
        day_names = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        today_name = day_names[today.weekday()]

        if not articles:
            return f"Fecha de hoy: {today_name} {today_str}\nNo se encontraron noticias recientes sobre: {query}"

        parts = [f"📅 Fecha de hoy: {today_name} {today_str}"]
        parts.append(f"**Noticias sobre: {query}**")
        parts.append(f"Total: {len(articles)} artículos de múltiples fuentes\n")

        for i, a in enumerate(articles, 1):
            source_tag = f"[{a.source}]" if a.source else ""
            # Mostrar fecha legible
            if a.parsed_date:
                age_hours = (datetime.now(timezone.utc) - a.parsed_date).total_seconds() / 3600
                if age_hours < 24:
                    pub_tag = " (HOY)"
                elif age_hours < 48:
                    pub_tag = " (AYER)"
                else:
                    pub_tag = f" ({a.parsed_date.strftime('%d/%m/%Y')})"
            elif a.published:
                pub_tag = f" ({a.published[:16]})"
            else:
                pub_tag = ""
            parts.append(
                f"{i}. {source_tag} **{a.title}**{pub_tag}\n"
                f"   {a.snippet[:200]}\n"
                f"   URL: {a.url}\n"
            )

        return "\n".join(parts)
