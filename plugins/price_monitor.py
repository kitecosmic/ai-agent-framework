"""
Ejemplo: Plugin de monitoreo de precios.

Demuestra cómo crear un plugin custom que:
1. Usa el browser module para scraping
2. Programa checks periódicos con el scheduler
3. Notifica via messaging cuando hay cambios

Coloca este archivo en plugins/ y se auto-descubre.
"""
from core.plugin_base import PluginBase, hook
from core.event_bus import Event


class PriceMonitorPlugin(PluginBase):
    name = "price_monitor"
    version = "1.0.0"
    description = "Monitors product prices and alerts on changes"
    dependencies = ["browser", "scheduler", "messaging"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tracked_prices: dict[str, float] = {}  # url -> last_price

    async def on_load(self):
        """Al cargar, configura un job de chequeo cada 30 minutos."""
        await self.bus.emit(Event(
            name="scheduler.add_job",
            data={
                "id": "price_check",
                "name": "Price Check",
                "trigger_type": "interval",
                "trigger_args": {"minutes": 30},
                "event_name": "price_monitor.check",
                "event_data": {},
            },
            source=self.name,
        ))

    @hook("price_monitor.check")
    async def check_prices(self, event: Event):
        """Chequea todos los precios trackeados."""
        for url, last_price in self._tracked_prices.items():
            results = await self.bus.emit(Event(
                name="browser.extract",
                data={
                    "url": url,
                    "selectors": {"price": ".price::text"},
                },
                source=self.name,
            ))

            if results and results[0]:
                result = results[0]
                prices = result.extracted_data.get("price", [])
                if prices:
                    try:
                        current = float(prices[0].replace("$", "").replace(",", "").strip())
                        if last_price > 0 and current != last_price:
                            change = ((current - last_price) / last_price) * 100
                            await self.bus.emit(Event(
                                name="messaging.send",
                                data={
                                    "content": f"💰 Cambio de precio en {url}: ${last_price:.2f} → ${current:.2f} ({change:+.1f}%)",
                                    "channel": "alerts",
                                },
                                source=self.name,
                            ))
                        self._tracked_prices[url] = current
                    except ValueError:
                        pass

    @hook("price_monitor.track")
    async def track_url(self, event: Event):
        """Agrega una URL para trackear su precio."""
        url = event.data.get("url", "")
        if url:
            self._tracked_prices[url] = 0
            return {"status": "tracking", "url": url}

    @hook("price_monitor.untrack")
    async def untrack_url(self, event: Event):
        """Remueve una URL del tracking."""
        url = event.data.get("url", "")
        self._tracked_prices.pop(url, None)
        return {"status": "untracked", "url": url}

    @hook("price_monitor.list")
    async def list_tracked(self, event: Event):
        """Lista URLs trackeadas."""
        return {
            "tracked": [
                {"url": url, "last_price": price}
                for url, price in self._tracked_prices.items()
            ]
        }
