"""
Plugin: Clima via Open-Meteo API.

Consulta clima actual y pronóstico usando Open-Meteo (gratis, sin API key).
Incluye geocoding para resolver nombres de ciudades a coordenadas.

Eventos:
- weather.current  → clima actual de una ciudad
- weather.forecast → pronóstico de 3-7 días
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from core.event_bus import Event
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()

# ── Configuración ──────────────────────────────────────────────────

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 10.0

# WMO Weather interpretation codes → emoji + descripción en español
WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("☀️", "Despejado"),
    1: ("🌤️", "Mayormente despejado"),
    2: ("⛅", "Parcialmente nublado"),
    3: ("☁️", "Nublado"),
    45: ("🌫️", "Niebla"),
    48: ("🌫️", "Niebla con escarcha"),
    51: ("🌦️", "Llovizna ligera"),
    53: ("🌦️", "Llovizna moderada"),
    55: ("🌧️", "Llovizna intensa"),
    56: ("🌧️", "Llovizna helada ligera"),
    57: ("🌧️", "Llovizna helada intensa"),
    61: ("🌧️", "Lluvia ligera"),
    63: ("🌧️", "Lluvia moderada"),
    65: ("🌧️", "Lluvia intensa"),
    66: ("🌧️", "Lluvia helada ligera"),
    67: ("🌧️", "Lluvia helada intensa"),
    71: ("🌨️", "Nevada ligera"),
    73: ("🌨️", "Nevada moderada"),
    75: ("❄️", "Nevada intensa"),
    77: ("❄️", "Granizo fino"),
    80: ("🌦️", "Chubascos ligeros"),
    81: ("🌧️", "Chubascos moderados"),
    82: ("⛈️", "Chubascos intensos"),
    85: ("🌨️", "Chubascos de nieve ligeros"),
    86: ("🌨️", "Chubascos de nieve intensos"),
    95: ("⛈️", "Tormenta eléctrica"),
    96: ("⛈️", "Tormenta con granizo ligero"),
    99: ("⛈️", "Tormenta con granizo intenso"),
}

# Ciudad por defecto (perfil del usuario)
DEFAULT_CITY = "Rosario, Argentina"
DEFAULT_TIMEZONE = "America/Argentina/Buenos_Aires"


@dataclass
class GeoLocation:
    name: str
    admin: str  # provincia/estado
    country: str
    latitude: float
    longitude: float

    @property
    def display_name(self) -> str:
        parts = [self.name]
        if self.admin:
            parts.append(self.admin)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)


class WeatherPlugin(PluginBase):
    name = "weather"
    version = "1.0.0"
    description = "Clima actual y pronóstico via Open-Meteo (gratis, sin API key)"
    dependencies = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._geo_cache: dict[str, GeoLocation] = {}

    async def on_load(self):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT),
            follow_redirects=True,
        )
        logger.info("weather.loaded")

    async def on_unload(self):
        if self._client:
            await self._client.aclose()

    # ── Eventos ────────────────────────────────────────────────────

    @hook("weather.current")
    async def get_current(self, event: Event) -> dict[str, Any]:
        """
        Clima actual de una ciudad.
        Datos: {"city": "Rosario"} o {"city": "Buenos Aires, Argentina"}
        """
        city = event.data.get("city", DEFAULT_CITY)
        logger.info("weather.current_request", city=city)

        geo = await self._geocode(city)
        if not geo:
            return {"error": f"No encontré la ciudad: {city}"}

        try:
            resp = await self._client.get(FORECAST_URL, params={
                "latitude": geo.latitude,
                "longitude": geo.longitude,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,precipitation",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
                "timezone": DEFAULT_TIMEZONE,
                "forecast_days": 1,
            })
            data = resp.json()
        except Exception as exc:
            logger.error("weather.api_error", error=str(exc))
            return {"error": f"Error consultando clima: {str(exc)[:100]}"}

        current = data["current"]
        daily = data["daily"]
        code = current.get("weather_code", 0)
        emoji, desc = WMO_CODES.get(code, ("🌡️", f"Código {code}"))

        summary = (
            f"{emoji} **Clima en {geo.display_name}**\n\n"
            f"🌡️ **Temperatura:** {current['temperature_2m']}°C (sensación {current['apparent_temperature']}°C)\n"
            f"📋 **Condición:** {desc}\n"
            f"💧 **Humedad:** {current['relative_humidity_2m']}%\n"
            f"💨 **Viento:** {current['wind_speed_10m']} km/h\n"
        )

        if daily["time"]:
            summary += (
                f"\n📊 **Hoy:** {daily['temperature_2m_min'][0]}°C - {daily['temperature_2m_max'][0]}°C\n"
                f"🌧️ **Prob. lluvia:** {daily['precipitation_probability_max'][0]}%\n"
                f"🌅 **Amanecer:** {self._format_time(daily['sunrise'][0])}\n"
                f"🌇 **Atardecer:** {self._format_time(daily['sunset'][0])}"
            )

        logger.info("weather.current_ok", city=geo.display_name, temp=current["temperature_2m"])
        return {
            "summary_text": summary,
            "city": geo.display_name,
            "temperature": current["temperature_2m"],
            "feels_like": current["apparent_temperature"],
            "condition": desc,
            "humidity": current["relative_humidity_2m"],
            "wind_speed": current["wind_speed_10m"],
        }

    @hook("weather.forecast")
    async def get_forecast(self, event: Event) -> dict[str, Any]:
        """
        Pronóstico extendido (3-7 días).
        Datos: {"city": "Rosario", "days": 5}
        """
        city = event.data.get("city", DEFAULT_CITY)
        days = min(event.data.get("days", 3), 7)
        logger.info("weather.forecast_request", city=city, days=days)

        geo = await self._geocode(city)
        if not geo:
            return {"error": f"No encontré la ciudad: {city}"}

        try:
            resp = await self._client.get(FORECAST_URL, params={
                "latitude": geo.latitude,
                "longitude": geo.longitude,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
                "timezone": DEFAULT_TIMEZONE,
                "forecast_days": days,
            })
            data = resp.json()
        except Exception as exc:
            logger.error("weather.forecast_api_error", error=str(exc))
            return {"error": f"Error consultando pronóstico: {str(exc)[:100]}"}

        daily = data["daily"]
        lines = [f"📅 **Pronóstico {days} días — {geo.display_name}**\n"]

        day_names_es = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
        forecast_days = []

        for i in range(len(daily["time"])):
            code = daily["weather_code"][i]
            emoji, desc = WMO_CODES.get(code, ("🌡️", f"Código {code}"))

            # Nombre del día
            from datetime import datetime
            dt = datetime.strptime(daily["time"][i], "%Y-%m-%d")
            day_name = day_names_es[dt.weekday()]
            date_str = f"{dt.day}/{dt.month}"

            t_min = daily["temperature_2m_min"][i]
            t_max = daily["temperature_2m_max"][i]
            rain = daily["precipitation_probability_max"][i]
            wind = daily["wind_speed_10m_max"][i]

            line = f"{emoji} **{day_name} {date_str}:** {t_min}°C - {t_max}°C"
            if rain and rain > 0:
                line += f" | 🌧️ {rain}%"
            if wind and wind > 30:
                line += f" | 💨 {wind}km/h"
            line += f" — {desc}"
            lines.append(line)

            forecast_days.append({
                "date": daily["time"][i],
                "day": day_name,
                "temp_min": t_min,
                "temp_max": t_max,
                "condition": desc,
                "rain_prob": rain,
            })

        summary = "\n".join(lines)
        logger.info("weather.forecast_ok", city=geo.display_name, days=days)
        return {
            "summary_text": summary,
            "city": geo.display_name,
            "days": forecast_days,
        }

    # ── Geocoding ──────────────────────────────────────────────────

    async def _geocode(self, city: str) -> GeoLocation | None:
        """Resuelve nombre de ciudad a coordenadas. Cachea resultados."""
        cache_key = city.lower().strip()
        if cache_key in self._geo_cache:
            return self._geo_cache[cache_key]

        try:
            resp = await self._client.get(GEOCODING_URL, params={
                "name": city,
                "count": 5,
                "language": "es",
            })
            data = resp.json()
            results = data.get("results", [])

            if not results:
                logger.warning("weather.geocode_not_found", city=city)
                return None

            # Preferir resultados de Argentina/LATAM si hay ambigüedad
            latam_countries = {"Argentina", "Chile", "Uruguay", "Brasil", "Colombia", "México", "Perú", "Ecuador", "Paraguay", "Bolivia", "Venezuela"}
            best = results[0]
            for r in results:
                if r.get("country") in latam_countries:
                    best = r
                    break

            geo = GeoLocation(
                name=best["name"],
                admin=best.get("admin1", ""),
                country=best.get("country", ""),
                latitude=best["latitude"],
                longitude=best["longitude"],
            )
            self._geo_cache[cache_key] = geo
            logger.debug("weather.geocoded", city=city, resolved=geo.display_name, lat=geo.latitude, lon=geo.longitude)
            return geo

        except Exception as exc:
            logger.error("weather.geocode_error", city=city, error=str(exc))
            return None

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _format_time(iso_time: str) -> str:
        """Formatea '2026-02-25T07:15' → '07:15'."""
        if "T" in iso_time:
            return iso_time.split("T")[1][:5]
        return iso_time
