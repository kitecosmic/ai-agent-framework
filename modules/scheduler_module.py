"""
Scheduler Module — Tareas programadas (cron jobs, intervals, one-shot).

Usa APScheduler para gestionar jobs persistentes con soporte para:
- Cron expressions
- Intervalos
- Ejecución diferida (one-shot)
- Persistencia en base de datos
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger

from core.event_bus import Event, event_bus
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()

JOBS_FILE = Path(__file__).resolve().parent.parent / "data" / "scheduled_jobs.json"


@dataclass
class JobInfo:
    id: str
    name: str
    trigger_type: str  # "cron" | "interval" | "date"
    trigger_args: dict
    event_name: str
    event_data: dict
    enabled: bool = True
    next_run: datetime | None = None


class SchedulerModule(PluginBase):
    """
    Módulo de tareas programadas.

    Cuando un job se dispara, emite un evento en el bus que
    otros módulos pueden escuchar y procesar.

    Eventos:
    - scheduler.job_added    → job creado
    - scheduler.job_fired    → job ejecutado
    - scheduler.job_removed  → job eliminado
    - scheduler.add_job      → solicitud para agregar job
    """

    name = "scheduler"
    version = "1.0.0"
    description = "Task scheduling with cron, interval, and one-shot triggers"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._scheduler: AsyncIOScheduler | None = None
        self._jobs: dict[str, JobInfo] = {}

    async def on_load(self):
        self._scheduler = AsyncIOScheduler(timezone="America/Argentina/Buenos_Aires")
        self._scheduler.start()
        logger.info("scheduler.started")
        # Restaurar jobs persistidos
        await self._load_persisted_jobs()

    async def on_unload(self):
        if self._scheduler:
            self._scheduler.shutdown(wait=False)

    # ── Persistence ───────────────────────────────────────────────

    def _save_jobs(self):
        """Guarda los jobs actuales a disco (excluye jobs de plugins como price_check)."""
        persistable = {}
        for job_id, info in self._jobs.items():
            # Solo persistir jobs que vinieron del orchestrator (task.execute)
            if info.event_name == "task.execute":
                persistable[job_id] = {
                    "id": info.id,
                    "name": info.name,
                    "trigger_type": info.trigger_type,
                    "trigger_args": info.trigger_args,
                    "event_name": info.event_name,
                    "event_data": info.event_data,
                    "enabled": info.enabled,
                }
        try:
            JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            JOBS_FILE.write_text(json.dumps(persistable, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.debug("scheduler.jobs_saved", count=len(persistable))
        except Exception as exc:
            logger.error("scheduler.save_error", error=str(exc))

    async def _load_persisted_jobs(self):
        """Restaura jobs desde disco al iniciar."""
        if not JOBS_FILE.exists():
            return
        try:
            data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
            for job_id, job_data in data.items():
                try:
                    await self.add_job(
                        job_id=job_data["id"],
                        name=job_data["name"],
                        trigger_type=job_data["trigger_type"],
                        trigger_args=job_data["trigger_args"],
                        event_name=job_data["event_name"],
                        event_data=job_data.get("event_data", {}),
                    )
                    logger.info("scheduler.job_restored", job_id=job_id, name=job_data["name"])
                except Exception as exc:
                    logger.warning("scheduler.job_restore_failed", job_id=job_id, error=str(exc))
            logger.info("scheduler.jobs_loaded", count=len(data))
        except Exception as exc:
            logger.error("scheduler.load_error", error=str(exc))

    # ── Event Handlers ───────────────────────────────────────────

    @hook("scheduler.add_job")
    async def handle_add_job(self, event: Event) -> JobInfo:
        """Agrega un job vía event bus."""
        return await self.add_job(
            job_id=event.data.get("id", ""),
            name=event.data.get("name", "Unnamed Job"),
            trigger_type=event.data["trigger_type"],
            trigger_args=event.data.get("trigger_args", {}),
            event_name=event.data["event_name"],
            event_data=event.data.get("event_data", {}),
        )

    @hook("scheduler.remove_job")
    async def handle_remove_job(self, event: Event):
        """Remueve un job vía event bus."""
        await self.remove_job(event.data["id"])

    @hook("scheduler.list_jobs")
    async def handle_list_jobs(self, event: Event) -> list[dict]:
        """Lista todos los jobs."""
        return self.list_jobs()

    # ── Core Methods ─────────────────────────────────────────────

    async def add_job(
        self,
        job_id: str,
        name: str,
        trigger_type: str,
        trigger_args: dict[str, Any],
        event_name: str,
        event_data: dict[str, Any] | None = None,
    ) -> JobInfo:
        """
        Agrega un job al scheduler.

        Ejemplos:
            # Cron: todos los días a las 9am
            await scheduler.add_job(
                job_id="daily_report",
                name="Daily Report",
                trigger_type="cron",
                trigger_args={"hour": 9, "minute": 0},
                event_name="report.generate",
                event_data={"type": "daily"},
            )

            # Interval: cada 5 minutos
            await scheduler.add_job(
                job_id="health_check",
                name="Health Check",
                trigger_type="interval",
                trigger_args={"minutes": 5},
                event_name="health.check",
            )

            # One-shot: en una fecha específica
            await scheduler.add_job(
                job_id="reminder",
                name="Meeting Reminder",
                trigger_type="date",
                trigger_args={"run_date": "2026-03-01T10:00:00"},
                event_name="notification.send",
                event_data={"message": "Meeting in 30 minutes"},
            )
        """
        event_data = event_data or {}

        # Crear trigger
        if trigger_type == "cron":
            trigger = CronTrigger(**trigger_args)
        elif trigger_type == "interval":
            trigger = IntervalTrigger(**trigger_args)
        elif trigger_type == "date":
            trigger = DateTrigger(**trigger_args)
        else:
            raise ValueError(f"Unknown trigger type: {trigger_type}")

        # Función que se ejecuta cuando el job dispara
        async def fire_job():
            logger.info("scheduler.job_fired", job_id=job_id, event_name=event_name)
            await self.bus.emit(Event(
                name=event_name,
                data={**event_data, "_job_id": job_id, "_job_name": name},
                source="scheduler",
            ))
            await self.bus.emit(Event(
                name="scheduler.job_fired",
                data={"job_id": job_id, "event_name": event_name},
                source="scheduler",
            ))

        # Registrar en APScheduler
        self._scheduler.add_job(
            fire_job,
            trigger=trigger,
            id=job_id,
            name=name,
            replace_existing=True,
            misfire_grace_time=60,  # Si se pierde la ventana por >60s, no re-ejecutar
            coalesce=True,          # Si hay múltiples misfires, ejecutar solo una vez
        )

        # Guardar metadata
        job_info = JobInfo(
            id=job_id,
            name=name,
            trigger_type=trigger_type,
            trigger_args=trigger_args,
            event_name=event_name,
            event_data=event_data,
        )

        # Obtener next_run
        ap_job = self._scheduler.get_job(job_id)
        if ap_job and ap_job.next_run_time:
            job_info.next_run = ap_job.next_run_time

        self._jobs[job_id] = job_info

        await self.bus.emit(Event(
            name="scheduler.job_added",
            data={"job_id": job_id, "name": name, "trigger_type": trigger_type},
            source="scheduler",
        ))

        logger.info("scheduler.job_added", job_id=job_id, trigger=trigger_type)
        self._save_jobs()
        return job_info

    async def remove_job(self, job_id: str):
        """Remueve un job del scheduler."""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass
        self._jobs.pop(job_id, None)
        self._save_jobs()

        await self.bus.emit(Event(
            name="scheduler.job_removed",
            data={"job_id": job_id},
            source="scheduler",
        ))

    async def pause_job(self, job_id: str):
        """Pausa un job."""
        self._scheduler.pause_job(job_id)
        if job_id in self._jobs:
            self._jobs[job_id].enabled = False

    async def resume_job(self, job_id: str):
        """Reanuda un job pausado."""
        self._scheduler.resume_job(job_id)
        if job_id in self._jobs:
            self._jobs[job_id].enabled = True

    def list_jobs(self) -> list[dict]:
        """Lista todos los jobs registrados."""
        result = []
        for job_id, info in self._jobs.items():
            ap_job = self._scheduler.get_job(job_id)
            result.append({
                "id": info.id,
                "name": info.name,
                "trigger_type": info.trigger_type,
                "trigger_args": info.trigger_args,
                "event_name": info.event_name,
                "enabled": info.enabled,
                "next_run": str(ap_job.next_run_time) if ap_job and ap_job.next_run_time else None,
            })
        return result
