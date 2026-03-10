"""
Agent Orchestrator — El cerebro que coordina todos los módulos.

Recibe instrucciones (en lenguaje natural o estructuradas),
planifica la ejecución, delega a los módulos correctos, y
compone la respuesta final.

Flujo típico:
1. Recibe mensaje/tarea
2. LLM analiza y crea plan de ejecución
3. Ejecuta pasos delegando a módulos via event bus
4. Compone respuesta con resultados
5. Responde al usuario/sistema
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from core.event_bus import Event, event_bus
from core.llm_router import LLMMessage, LLMRouter, llm_router
from core.plugin_base import PluginBase, hook

logger = structlog.get_logger()


# ── Onboarding ────────────────────────────────────────────────────────
# Campos obligatorios para onboarding. Cada entrada: (section, field, pregunta)
ONBOARDING_FIELDS = [
    ("Onboarding", "Nickname", "¿Cómo te gustaría que te llame? (apodo o nombre preferido)"),
    ("Onboarding", "Interests", "¿Qué temas te interesan? Ej: tecnología, IA, deportes, finanzas, música..."),
    ("Onboarding", "Career", "¿Qué carrera estudiás o a qué te dedicás profesionalmente?"),
    ("Onboarding", "News Interests", "¿Qué tipo de noticias te gustaría recibir? Ej: tech, IA, economía, deportes..."),
]

ONBOARDING_WELCOME = """¡Hola! Soy tu agente personal. Antes de empezar necesito conocerte mejor para darte una experiencia personalizada.

Solo voy a hacerte {total} preguntas rápidas.

{question}"""

ONBOARDING_COMPLETE = """¡Listo! Ya tengo todo lo que necesito para ayudarte de forma personalizada.

A partir de ahora funcionaré con todas mis capacidades. ¿En qué te puedo ayudar?"""


# ── Prompt base (las secciones dinámicas se inyectan en runtime) ──────

BASE_MODULES_PROMPT = """
## Módulos disponibles

### 1. news.search (NOTICIAS Y TENDENCIAS — usar para noticias, tendencias, resúmenes diarios)
Datos: {"query": "inteligencia artificial startups"}
- Busca en feeds RSS de fuentes confiables: Xataka, Genbeta, TechCrunch, The Verge, Google News, etc.
- Devuelve artículos REALES y ACTUALES (no datos de entrenamiento)
- USAR PARA: noticias, tendencias, resúmenes, "qué está pasando en tech", novedades
- QUERY: usa términos descriptivos del tema. Ejemplo: "inteligencia artificial startups innovación"

### 2. weather.current (CLIMA ACTUAL de una ciudad)
Datos: {"city": "Buenos Aires"}
- Devuelve temperatura, sensación térmica, humedad, viento, condición, amanecer/atardecer
- USAR PARA: "qué clima hace", "cómo está el tiempo", "temperatura en X"

### 3. weather.forecast (PRONÓSTICO extendido)
Datos: {"city": "Rosario", "days": 5}
- Pronóstico de 3 a 7 días con temperaturas, probabilidad de lluvia, condiciones
- USAR PARA: "pronóstico", "va a llover", "clima de la semana"

### 4. browser.search (BÚSQUEDAS WEB GENERALES — para info que NO sea noticias ni clima)
Datos: {"query": "texto de búsqueda"}
- Busca en Google/DuckDuckGo/Bing via browser real
- NO usar para noticias (usar news.search) ni clima (usar weather.current/forecast)
- QUERY: términos cortos y claros. NUNCA incluyas fechas (dd/mm/yyyy).

### 5. http.request (APIs directas con URL conocida)
Datos: {"method": "GET", "url": "...", "headers": {...}, "params": {...}}
APIs públicas SIN API key:
- IP INFO: https://ipinfo.io/json
- HORA: https://worldtimeapi.org/api/timezone/America/Argentina/Buenos_Aires

### 6. browser.navigate (ir a una URL específica conocida)
Datos: {"url": "https://..."}  (URL completa obligatoria)

### 7. browser.extract (scraping con selectores CSS)
Datos: {"url": "https://...", "selectors": {"titulo": "h1", "precio": ".price"}}

### 8. scheduler.add_job (tareas programadas)
Datos: {"id": "...", "name": "...", "trigger_type": "interval|cron|date", "trigger_args": {...}, "event_name": "task.execute", "event_data": {"instruction": "la tarea a ejecutar", "chat_id": CHAT_ID, "channel": "telegram:CHAT_ID"}}
IMPORTANTE: event_name SIEMPRE debe ser "task.execute" y event_data debe incluir "instruction" (qué hacer), "chat_id" (del usuario) y "channel".

### 9. system.exec (EJECUTAR COMANDOS del sistema)
Datos: {"command": "ls -la ~", "cwd": "~", "timeout": 30}
- Ejecuta comandos shell en el servidor
- Retorna stdout, stderr, exit_code
- Comandos peligrosos (rm -rf /, reboot, etc.) están BLOQUEADOS
- USAR PARA: instalar paquetes, verificar estado del sistema, ejecutar scripts
- IMPORTANTE: usá el Home directory del Contexto Actual para paths, NUNCA inventes /home/user/

### 10. system.file_read (LEER archivos)
Datos: {"path": "~/config.json"}
- Usá ~ o el home directory real del Contexto Actual

### 11. system.file_write (CREAR/ESCRIBIR archivos)
Datos: {"path": "~/script.py", "content": "print('hola')", "append": false}
- append=true agrega al final sin borrar
- SIEMPRE usá el home directory real o ~ como base, NUNCA /home/user/

### 12. system.file_list (LISTAR archivos en directorio)
Datos: {"path": "~", "pattern": "*.py", "recursive": false}

### 13. system.pip_install (INSTALAR paquetes Python)
Datos: {"package": "requests"}

### 13b. system.file_delete (ELIMINAR archivos o directorios — REQUIERE CONFIRMACIÓN)
Datos: {"path": "/ruta/al/archivo"}
- IMPORTANTE: el sistema pedirá confirmación explícita al usuario antes de ejecutar
- Nunca incluyas confirmed=true en el plan — el orchestrator lo maneja automáticamente
- USAR PARA: eliminar archivos, directorios, limpiar datos

### 14. mcp.call_tool (HERRAMIENTAS MCP externas — Playwright avanzado, Cloudflare, etc.)
Datos: {"server": "playwright", "tool": "browser_navigate", "arguments": {"url": "https://..."}}
- Conecta con servidores MCP configurados
- Playwright MCP permite: navegar, click, screenshot, llenar formularios, extraer datos
- Cloudflare MCP permite: crear subdominios, gestionar DNS, workers, páginas
- USAR PARA crear subdominios: {"server": "cloudflare", "tool": "...", "arguments": {...}}
- USAR CUANDO browser.search no es suficiente y necesitás interactuar con una página

### 15. mcp.list_tools (VER herramientas MCP disponibles)
Datos: {}

### 16. rapibase.select (CONSULTAR/LEER datos de RapiBase)
Datos: {"table": "nombre_tabla", "filter": "campo:op:valor", "page": 1, "page_size": 50, "order_by": "campo", "order_dir": "asc"}
- Todos los parámetros excepto "table" son opcionales
- filter sintaxis: "campo:op:valor" donde op es: eq, ne, gt, lt, gte, lte, like
- Ejemplos de filter: "status:eq:active" | "age:gte:18" | "name:like:Joel"
- USAR PARA: leer registros, buscar datos, listar entradas con o sin filtros

### 17. rapibase.insert (INSERTAR un registro nuevo)
Datos: {"table": "nombre_tabla", "data": {"campo": "valor", "campo2": "valor2"}}
- Crea un nuevo registro en la tabla
- USAR PARA: crear nuevos registros, agregar datos

### 18. rapibase.update (ACTUALIZAR un registro por ID)
Datos: {"table": "nombre_tabla", "id": "uuid-del-registro", "data": {"campo": "nuevo_valor"}}
- Actualiza el registro con el ID especificado
- USAR PARA: modificar datos existentes (necesitás el ID del registro)

### 19. rapibase.delete (ELIMINAR un registro por ID)
Datos: {"table": "nombre_tabla", "id": "uuid-del-registro"}
- Elimina el registro con el ID especificado
- USAR PARA: borrar un registro específico

### 20. rapibase.auth_signup (REGISTRAR un nuevo usuario)
Datos: {"email": "user@example.com", "password": "pass123", "metadata": {"name": "Joel"}}
- Registra un nuevo usuario en RapiBase Auth
- Usa Anon Key automáticamente (acceso público)
- USAR PARA: crear cuentas de usuario en la app

### 21. rapibase.auth_signin (INICIAR SESIÓN de usuario)
Datos: {"email": "user@example.com", "password": "pass123"}
- Autentica al usuario y retorna JWT token
- USAR PARA: login de usuarios, obtener token para operaciones autenticadas

### 22. rapibase.auth_magic_link (ENVIAR magic link por email)
Datos: {"email": "user@example.com"}
- Envía un link de acceso sin contraseña al email del usuario
- USAR PARA: autenticación passwordless

### 23. rapibase.auth_forgot_password (RECUPERAR contraseña)
Datos: {"email": "user@example.com"}
- Envía email de recuperación de contraseña
- USAR PARA: recuperar acceso cuando el usuario olvidó su contraseña

### 24. rapibase.auth_reset_password (RESETEAR contraseña con token)
Datos: {"token": "token-del-email", "new_password": "nueva_pass"}
- Cambia la contraseña usando el token del email de recuperación
- USAR PARA: completar el flujo de reset de contraseña

### 25. rapibase.storage_list (LISTAR archivos en Storage)
Datos: {"bucket": "nombre_bucket", "prefix": "carpeta/", "page": 1, "page_size": 50}
- Lista archivos en un bucket de storage (prefix opcional para filtrar carpeta)
- USAR PARA: ver archivos subidos, explorar storage

### 26. rapibase.storage_delete (ELIMINAR archivo de Storage)
Datos: {"bucket": "nombre_bucket", "path": "carpeta/archivo.jpg"}
- Elimina un archivo del storage
- USAR PARA: borrar archivos subidos

### 27. rapibase.storage_search (BUSCAR archivos en Storage)
Datos: {"bucket": "nombre_bucket", "query": "texto-a-buscar"}
- Busca archivos por nombre en un bucket
- USAR PARA: encontrar archivos específicos en el storage

## Reglas CRÍTICAS
1. Para NOTICIAS, TENDENCIAS, RESÚMENES de actualidad: USA news.search (feeds RSS confiables y actuales)
2. Para CLIMA, TEMPERATURA, PRONÓSTICO: USA weather.current o weather.forecast (NUNCA http.request a wttr.in)
3. Para buscar info GENERAL en la web: USA browser.search (NUNCA http.request a buscadores)
4. Para datos de APIs conocidas (hora, IP): USA http.request
5. Si el usuario dice "busca en la web", "buscá en internet" → USA browser.search
6. Para ir a una URL específica: USA browser.navigate
7. NUNCA uses messaging.send — la respuesta llega al usuario automáticamente
8. NUNCA le digas al usuario que busque él mismo — TÚ tienes las herramientas, ÚSALAS
9. Si NO necesitas módulos, pon steps: [] y responde directamente
10. Responde SOLO JSON válido, sin markdown, sin texto extra
11. NUNCA expongas errores técnicos internos al usuario (URLs, .env, API keys, configuración del servidor, nombres de módulos). Si un módulo no está disponible, respondé de forma amigable que esa función no está disponible ahora y ofrecé alternativas
12. Si detectas info personal nueva del usuario, incluye "profile_update"
13. Al programar tareas (scheduler.add_job), el campo "response" debe ser BREVE: solo confirma qué se programó, a qué hora y por qué canal. NO incluyas resúmenes anticipados ni datos inventados.
14. Para tareas del SISTEMA (instalar, crear archivos, ejecutar comandos): USA system.exec / system.file_write / system.pip_install
15. PLANIFICACIÓN REACTIVA: Después de cada step, evaluaré los resultados y te pediré decidir si necesitás más pasos. Planificá solo el primer paso necesario si no estás seguro del resultado.
16. Para BASES DE DATOS (consultar, insertar, actualizar, eliminar): USA rapibase.select/insert/update/delete — filter sintaxis: "campo:op:valor" (op: eq, ne, gt, lt, gte, lte, like)
    Para AUTH de usuarios en la app: USA rapibase.auth_signup/auth_signin/auth_magic_link/auth_forgot_password/auth_reset_password
    Para STORAGE de archivos: USA rapibase.storage_list/storage_delete/storage_search
    INSTALAR RapiBase: es un proyecto open-source (github.com/kitecosmic/rapibase). Para instalarlo en el servidor:
      1. git clone https://github.com/kitecosmic/rapibase.git ~/rapibase
      2. cd ~/rapibase && docker compose up -d (requiere Docker)
      3. Configurar RAPIBASE_URL=http://localhost:3500 y RAPIBASE_SERVICE_KEY en el .env del agente
    Si RapiBase no está configurado, informar al usuario cómo instalarlo con estos pasos
17. Para crear SUBDOMINIOS o gestionar DNS en Cloudflare: USA mcp.call_tool con server="cloudflare"
18. Para ELIMINAR archivos: USA system.file_delete (NO system.exec con rm) — el sistema pedirá confirmación al usuario automáticamente
19. NUNCA incluyas confirmed=true en system.file_delete — el orchestrator se encarga de la confirmación
20. Para BORRAR con rm (vía system.exec): el sistema también pedirá confirmación automática si no hay trust mode activo

## Formato OBLIGATORIO (un solo JSON)
{"thinking": "análisis", "steps": [{"event": "...", "data": {...}, "description": "..."}], "response": "mensaje al usuario"}

## Ejemplos

Búsqueda web:
{"thinking": "Busco info sobre Granadero Baigorria", "steps": [{"event": "browser.search", "data": {"query": "Granadero Baigorria Santa Fe Argentina"}, "description": "Buscar en Google"}], "response": "Buscando información..."}

Clima actual:
{"thinking": "Uso weather.current para el clima", "steps": [{"event": "weather.current", "data": {"city": "Rosario"}, "description": "Clima actual de Rosario"}], "response": "Consultando el clima..."}

Pronóstico:
{"thinking": "El usuario quiere saber si va a llover", "steps": [{"event": "weather.forecast", "data": {"city": "Buenos Aires", "days": 3}, "description": "Pronóstico 3 días Buenos Aires"}], "response": "Consultando el pronóstico..."}

Saludo:
{"thinking": "Es un saludo", "steps": [], "response": "¡Hola! ¿En qué te puedo ayudar?"}

Profile update:
{"thinking": "El usuario me dice su nombre", "steps": [], "response": "¡Encantado Joel!", "profile_update": {"field": "Name", "value": "Joel", "section": "Personal"}}

Ejecutar comando:
{"thinking": "El usuario quiere ver el espacio en disco", "steps": [{"event": "system.exec", "data": {"command": "df -h"}, "description": "Ver espacio en disco"}], "response": "Revisando..."}

Crear archivo:
{"thinking": "El usuario quiere crear un script", "steps": [{"event": "system.file_write", "data": {"path": "~/hello.py", "content": "print('Hola mundo!')"}, "description": "Crear script Python"}], "response": "Creando archivo..."}

Instalar paquete:
{"thinking": "Necesito instalar requests para hacer HTTP", "steps": [{"event": "system.pip_install", "data": {"package": "requests"}, "description": "Instalar requests"}], "response": "Instalando..."}

Tarea programada (el chat_id se obtiene del contexto):
{"thinking": "El usuario quiere un resumen diario a las 18hs", "steps": [{"event": "scheduler.add_job", "data": {"id": "daily_tech_trends", "name": "Tendencias tech diarias", "trigger_type": "cron", "trigger_args": {"hour": 18, "minute": 0}, "event_name": "task.execute", "event_data": {"instruction": "Usa news.search para buscar noticias recientes sobre inteligencia artificial, startups y tecnología. Haz un resumen en español con los puntos más importantes.", "chat_id": 1714121336, "channel": "telegram:1714121336"}}, "description": "Programar resumen diario a las 18hs"}], "response": "Listo, programé un resumen diario de tendencias tech para las 18:00 hs."}

RapiBase — consultar con filtro:
{"thinking": "El usuario quiere ver los usuarios activos", "steps": [{"event": "rapibase.select", "data": {"table": "users", "filter": "status:eq:active", "page": 1, "page_size": 20}, "description": "Listar usuarios activos"}], "response": "Consultando los usuarios activos..."}

RapiBase — consultar sin filtro:
{"thinking": "El usuario quiere ver todos los productos", "steps": [{"event": "rapibase.select", "data": {"table": "products"}, "description": "Listar todos los productos"}], "response": "Consultando productos..."}

RapiBase — insertar registro:
{"thinking": "El usuario quiere agregar un cliente", "steps": [{"event": "rapibase.insert", "data": {"table": "clients", "data": {"name": "Joel", "email": "joel@example.com", "plan": "pro"}}, "description": "Insertar nuevo cliente"}], "response": "Insertando el registro..."}

RapiBase — actualizar registro:
{"thinking": "El usuario quiere cambiar el plan del cliente con id abc123", "steps": [{"event": "rapibase.update", "data": {"table": "clients", "id": "abc123", "data": {"plan": "enterprise"}}, "description": "Actualizar plan del cliente"}], "response": "Actualizando el registro..."}

RapiBase — eliminar registro:
{"thinking": "El usuario quiere eliminar el producto con id xyz789", "steps": [{"event": "rapibase.delete", "data": {"table": "products", "id": "xyz789"}, "description": "Eliminar producto"}], "response": "Eliminando el registro..."}

RapiBase — registrar usuario:
{"thinking": "El usuario quiere crear una cuenta para alguien", "steps": [{"event": "rapibase.auth_signup", "data": {"email": "nuevo@example.com", "password": "pass123", "metadata": {"name": "Carlos"}}, "description": "Registrar nuevo usuario"}], "response": "Registrando el usuario..."}

RapiBase — listar archivos en storage:
{"thinking": "El usuario quiere ver los archivos subidos en el bucket avatars", "steps": [{"event": "rapibase.storage_list", "data": {"bucket": "avatars"}, "description": "Listar archivos en bucket avatars"}], "response": "Consultando el storage..."}
"""

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass
class TaskResult:
    """Resultado de una tarea orquestada."""
    success: bool
    steps_completed: int
    steps_total: int
    results: list[Any] = field(default_factory=list)
    response: str = ""
    error: str | None = None


class Orchestrator(PluginBase):
    """
    Orquestador central que usa LLM para planificar y ejecutar tareas.
    """

    name = "orchestrator"
    version = "1.0.0"
    description = "AI-powered task orchestrator"

    def __init__(self, *args, llm: LLMRouter | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.llm = llm or llm_router
        self._conversation_history: dict[str, list[LLMMessage]] = {}  # channel -> messages
        self._pending_profile_updates: dict[str, dict] = {}  # channel -> pending update
        self._onboarding_state: dict[str, str | None] = {}  # channel -> current field being asked
        self._pending_confirmations: dict[str, dict] = {}  # channel -> {step, description, type}
        self._trusted_channels: set[str] = set()  # channels con auto-approve activado

    # ── Onboarding helpers ────────────────────────────────────────

    def _get_missing_onboarding_fields(self) -> list[tuple[str, str, str]]:
        """Retorna lista de (section, field, question) con campos de onboarding vacíos."""
        content = self._load_profile("user_profile.md")
        missing = []
        for section, field, question in ONBOARDING_FIELDS:
            # Buscar el campo en la línea exacta (no cruzar líneas)
            import re
            # Buscar "**Field**: valor" en la misma línea
            # Usar [ \t]* para no cruzar líneas (no incluir \n en el match de espacio)
            pattern = rf"^\s*-\s*\*\*{re.escape(field)}\*\*:[ \t]*([^\r\n]*)"
            match = re.search(pattern, content, re.MULTILINE)
            # Campo faltante si: no existe la línea, o el valor tras ":" está vacío
            if not match or not match.group(1).strip():
                missing.append((section, field, question))
        return missing

    async def _handle_onboarding(
        self, instruction: str, channel: str
    ) -> TaskResult | None:
        """
        Maneja el flujo de onboarding.
        Retorna TaskResult si estamos en onboarding, None si ya está completo.
        """
        missing = self._get_missing_onboarding_fields()

        if not missing:
            # Onboarding completo — limpiar estado si había uno pendiente
            if channel in self._onboarding_state:
                del self._onboarding_state[channel]
            return None

        current_field = self._onboarding_state.get(channel)

        # Si estábamos esperando respuesta para un campo, guardarla
        if current_field and instruction.strip():
            answer = instruction.strip()
            # Encontrar el campo en ONBOARDING_FIELDS para saber la sección
            for section, field, _ in ONBOARDING_FIELDS:
                if field == current_field:
                    self._update_profile_field(section, field, answer)
                    logger.info("onboarding.field_saved", field=current_field, value=answer[:50])
                    break

            # Re-verificar campos faltantes después de guardar
            missing = self._get_missing_onboarding_fields()
            if not missing:
                # ¡Onboarding completo!
                del self._onboarding_state[channel]
                return TaskResult(
                    success=True,
                    steps_completed=0,
                    steps_total=0,
                    response=ONBOARDING_COMPLETE,
                )

        # Hay campos faltantes → preguntar el primero
        section, next_field, next_question = missing[0]
        self._onboarding_state[channel] = next_field

        total_remaining = len(missing)

        if current_field is None and total_remaining == len(ONBOARDING_FIELDS):
            # Primera vez — usar mensaje de bienvenida
            response = ONBOARDING_WELCOME.format(
                total=len(ONBOARDING_FIELDS),
                question=next_question,
            )
        else:
            remaining_str = f" (quedan {total_remaining})" if total_remaining > 1 else ""
            response = f"{next_question}{remaining_str}"

        return TaskResult(
            success=True,
            steps_completed=0,
            steps_total=0,
            response=response,
        )

    # ── Trust mode ───────────────────────────────────────────────

    @hook("system.set_trust")
    async def handle_set_trust(self, event: Event) -> dict:
        """Activa o desactiva el modo de confianza para un canal."""
        channel = event.data.get("channel", "")
        enable = event.data.get("enable", True)
        if enable:
            self._trusted_channels.add(channel)
        else:
            self._trusted_channels.discard(channel)
        return {"success": True, "trusted": enable, "channel": channel}

    # ── Multi-model routing ───────────────────────────────────────

    def _select_model_for_task(
        self, instruction: str, has_image: bool = False
    ) -> tuple[str | None, str | None]:
        """
        Selecciona el mejor modelo para la tarea.
        Retorna (provider, model) o (None, None) para usar el default.
        """
        if not self.config.get("model_routing_enabled", True):
            return (None, None)

        inst = instruction.lower()

        # Visión: imagen presente → modelo de visión dedicado
        if has_image:
            # Si pide extraer texto → OCR
            if any(kw in inst for kw in ["texto", "dice", "escrito", "leer", "lee", "extrae"]):
                m = self.config.get("ollama_ocr_model", "")
                if m:
                    return ("ollama", m)
            m = self.config.get("ollama_vision_model", "")
            if m:
                return ("ollama", m)

        # Código / programación
        # IMPORTANTE: usar keywords específicos para evitar falsos positivos
        # (ej: "api" matchea "rápido", "programa" matchea "programar tareas")
        code_kws_exact = [
            "python", "javascript", "typescript", "rust", "golang",
            "código", "codigo", "script", "función", "funcion",
            "algoritmo", "endpoint", "refactori", "deploy", "docker",
            "crea una app", "crea un script", "crea una api",
            "error en el código", "debugg", "debugue",
        ]
        # Keywords que necesitan ser palabras completas (word boundary)
        code_kws_word = [
            "api", "bug", "class", "clase", "def", "import", "java",
            "test", "testing", "programa",  # "programa" como sustantivo, no "programar"
        ]
        has_code_exact = any(kw in inst for kw in code_kws_exact)
        has_code_word = any(re.search(rf'\b{re.escape(kw)}\b', inst) for kw in code_kws_word)
        if has_code_exact or has_code_word:
            m = self.config.get("ollama_coding_model", "")
            if m:
                return ("ollama", m)

        # Razonamiento profundo
        think_kws = [
            "analiza en detalle", "analiza a fondo", "razona", "razonamiento",
            "explica en detalle", "explica a fondo", "piensa paso a paso",
            "step by step", "filosofía", "debate", "pros y contras",
            "ventajas y desventajas", "qué pasaría si", "hipotéticamente",
            "por qué exactamente", "explica el porqué", "profundiza",
        ]
        if any(kw in inst for kw in think_kws):
            m = self.config.get("ollama_reasoning_model", "")
            if m:
                return ("ollama", m)

        # Respuesta rápida: SOLO para mensajes muy cortos y simples (saludos, confirmaciones)
        # NO usar para preguntas reales, incluso si son cortas
        stripped = instruction.strip()
        simple_kws = ["hola", "hello", "hi", "gracias", "ok", "okey", "dale", "perfecto", "jaja", "buenas"]
        # Debe ser muy corto (<40 chars) Y empezar con keyword simple Y no contener signos de pregunta
        if (len(stripped) < 40
                and any(stripped.lower().startswith(kw) for kw in simple_kws)
                and "?" not in stripped
                and "¿" not in stripped):
            m = self.config.get("ollama_fast_model", "")
            if m:
                return ("ollama", m)

        return (None, None)

    # ── Profile & Context helpers ─────────────────────────────────

    def _load_profile(self, filename: str) -> str:
        """Carga un archivo .md de data/."""
        path = DATA_DIR / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def _save_user_profile(self, content: str):
        """Guarda el perfil de usuario actualizado."""
        path = DATA_DIR / "user_profile.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _update_profile_field(self, section: str, field_name: str, value: str) -> bool:
        """Actualiza un campo en user_profile.md. Si no existe, lo agrega a la sección."""
        path = DATA_DIR / "user_profile.md"
        if not path.exists():
            return False
        content = path.read_text(encoding="utf-8")
        lines = content.split("\n")
        new_lines = []
        in_section = False
        section_end_idx = -1
        updated = False

        for i, line in enumerate(lines):
            if line.startswith("## "):
                # Si estábamos en la sección correcta y no actualizamos, insertar antes de salir
                if in_section and not updated:
                    new_lines.append(f"- **{field_name}**: {value}")
                    updated = True
                in_section = line.strip().lower() == f"## {section.lower()}"
            if in_section and line.startswith(f"- **{field_name}**:"):
                new_lines.append(f"- **{field_name}**: {value}")
                updated = True
                continue
            new_lines.append(line)

        # Si la sección era la última y no se actualizó, agregar al final
        if in_section and not updated:
            new_lines.append(f"- **{field_name}**: {value}")
            updated = True

        # Si la sección no existe, crearla
        if not updated:
            new_lines.append(f"\n## {section}")
            new_lines.append(f"- **{field_name}**: {value}")
            updated = True

        if updated:
            path.write_text("\n".join(new_lines), encoding="utf-8")
        return updated

    def _build_image_message(self, text: str, image_b64: str, mime: str) -> str:
        """
        Construye un mensaje con imagen para el LLM.
        Para Anthropic/Claude: formato de content blocks serializado como JSON string.
        Para otros providers que no soporten vision, devuelve solo el texto.
        """
        import json
        # Serializar como JSON para que AnthropicProvider pueda detectarlo
        content_blocks = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": image_b64,
                },
            },
            {
                "type": "text",
                "text": text or "Describe y analiza esta imagen en detalle.",
            },
        ]
        return f"__IMAGE_CONTENT__:{json.dumps(content_blocks)}"

    def _build_system_prompt(self, channel: str = "default") -> str:
        """Construye el system prompt dinámicamente con contexto en tiempo real."""
        agent_profile = self._load_profile("agent_profile.md")
        user_profile = self._load_profile("user_profile.md")

        # Fecha/hora actual en la timezone del usuario
        try:
            tz = ZoneInfo("America/Argentina/Buenos_Aires")
        except Exception:
            tz = None
        now = datetime.now(tz)
        datetime_str = now.strftime("%A %d/%m/%Y %H:%M")
        date_iso = now.strftime("%Y-%m-%d")

        # Info del servidor
        import platform
        home_dir = str(Path.home())
        project_dir = str(Path(__file__).resolve().parent.parent)

        # Extraer chat_id del canal si es Telegram
        chat_id = ""
        if channel.startswith("telegram:"):
            chat_id = channel.split(":", 1)[1]

        parts = [
            "Eres NexusAgent, un agente AI. Responde SOLO con JSON válido, sin texto adicional.",
            f"\n## Contexto Actual\n- **Fecha y hora**: {datetime_str}\n- **Fecha ISO**: {date_iso}\n- **Timezone**: America/Argentina/Buenos_Aires (UTC-3)",
            f"- **Servidor**: {platform.system()} {platform.release()}",
            f"- **Home directory**: {home_dir} (USAR ESTE PATH para crear archivos del usuario, NUNCA /home/user/)",
            f"- **Proyecto**: {project_dir}",
        ]

        if chat_id:
            parts.append(f"- **Chat ID del usuario**: {chat_id} (usar en scheduler.add_job para enviar resultados)")

        if agent_profile:
            parts.append(f"\n## Perfil del Agente\n{agent_profile}")

        if user_profile:
            parts.append(f"\n## Perfil del Usuario\n{user_profile}")

        parts.append(BASE_MODULES_PROMPT)

        return "\n".join(parts)

    @hook("messaging.incoming")
    async def handle_incoming_message(self, event: Event):
        """Procesa mensajes entrantes de la app de mensajería."""
        content = event.data.get("content", "")
        channel = event.data.get("channel", "default")
        sender = event.data.get("sender", "unknown")

        if not content.strip():
            return

        logger.info("orchestrator.processing", sender=sender, channel=channel)

        result = await self.process_task(content, channel=channel)

        # Enviar respuesta de vuelta por messaging
        if result.response:
            await self.bus.emit(Event(
                name="messaging.send",
                data={
                    "content": result.response,
                    "channel": channel,
                    "reply_to": event.data.get("message", {}).get("id"),
                },
                source="orchestrator",
            ))

    @hook("task.execute")
    async def handle_task(self, event: Event) -> TaskResult:
        """Ejecuta una tarea directa. Si viene del scheduler, envía resultado por Telegram."""
        instruction = event.data.get("instruction", "")
        channel = event.data.get("channel", "system")
        chat_id = event.data.get("chat_id")

        logger.info(
            "orchestrator.handle_task",
            instruction=instruction[:80] if instruction else "(empty)",
            channel=channel,
            chat_id=chat_id,
            source=event.source,
        )

        image_b64 = event.data.get("image_b64")
        image_mime = event.data.get("image_mime", "image/jpeg")

        try:
            result = await self.process_task(
                instruction,
                channel=channel,
                image_b64=image_b64,
                image_mime=image_mime,
            )
        except Exception as exc:
            logger.error("orchestrator.handle_task_error", error=str(exc), chat_id=chat_id)
            result = TaskResult(
                success=False, steps_completed=0, steps_total=0,
                error=str(exc),
                response=f"Error procesando tarea programada: {str(exc)[:200]}",
            )

        # Si la tarea tiene chat_id (viene del scheduler), enviar resultado por Telegram
        if chat_id and result.response:
            logger.info("orchestrator.scheduled_task_sending", chat_id=chat_id, response_len=len(result.response))
            try:
                await self.bus.emit(Event(
                    name="telegram.send",
                    data={"chat_id": chat_id, "content": self._clean_response(result.response)},
                    source="orchestrator",
                ))
            except Exception as send_exc:
                logger.error("orchestrator.scheduled_task_send_error", error=str(send_exc), chat_id=chat_id)
        elif chat_id and not result.response:
            logger.warning("orchestrator.scheduled_task_no_response", chat_id=chat_id, success=result.success, error=result.error)

        return result

    async def process_task(
        self,
        instruction: str,
        channel: str = "default",
        image_b64: str | None = None,
        image_mime: str = "image/jpeg",
    ) -> TaskResult:
        """
        Proceso principal:
        1. Envía instrucción al LLM para planificar
        2. Ejecuta cada paso del plan
        3. Compone respuesta final
        """
        # ── Onboarding check (antes de cualquier procesamiento) ──
        onboarding_result = await self._handle_onboarding(instruction, channel)
        if onboarding_result is not None:
            return onboarding_result

        # Obtener o crear historial de conversación
        if channel not in self._conversation_history:
            self._conversation_history[channel] = []

        history = self._conversation_history[channel]

        # ── Confirmación pendiente (operaciones peligrosas) ──────────
        if channel in self._pending_confirmations:
            pending = self._pending_confirmations[channel]
            answer = instruction.strip().lower()
            confirm_words = {"confirmar", "confirm", "sí", "si", "yes", "ok", "dale", "procede", "hazlo"}
            cancel_words = {"no", "cancelar", "cancel", "nah", "nel", "no gracias"}

            if answer in confirm_words:
                self._pending_confirmations.pop(channel)
                step = pending["step"]
                # Marcar como confirmado
                if step.get("event") == "system.file_delete":
                    step["data"]["confirmed"] = True
                logger.warning(
                    "orchestrator.dangerous_op_confirmed",
                    channel=channel,
                    step=step.get("event"),
                    path=step.get("data", {}).get("path", step.get("data", {}).get("command", "?")),
                )
                step_result = await self._execute_step(step, 0, channel, instruction)
                ok = step_result["ok"]
                results = step_result["results"]
                errors = step_result["errors"]
                response = await self._summarize_for_user(instruction, results, errors, "")
                return TaskResult(
                    success=ok,
                    steps_completed=1 if ok else 0,
                    steps_total=1,
                    results=results,
                    response=response,
                )
            elif answer in cancel_words:
                self._pending_confirmations.pop(channel)
                msg = "❌ Operación cancelada."
                return TaskResult(success=True, steps_completed=0, steps_total=0, response=msg)
            else:
                # Otro mensaje → descartar pendiente y continuar normal
                self._pending_confirmations.pop(channel)

        # Verificar si el usuario confirma/rechaza un profile update pendiente
        if channel in self._pending_profile_updates:
            answer = instruction.strip().lower()
            if answer in ("sí", "si", "yes", "ok", "dale", "guardalo", "guárdalo", "confirmo"):
                pu = self._pending_profile_updates.pop(channel)
                updated = self._update_profile_field(pu["section"], pu["field"], pu["value"])
                if updated:
                    msg = f"✅ Guardé **{pu['field']}**: {pu['value']} en tu perfil."
                else:
                    msg = f"⚠️ No pude actualizar el campo {pu['field']}. Puede que la sección '{pu['section']}' no exista en el perfil."
                history.append(LLMMessage(role="user", content=instruction))
                history.append(LLMMessage(role="assistant", content=msg))
                return TaskResult(success=True, steps_completed=0, steps_total=0, response=msg)
            elif answer in ("no", "nah", "nel", "no gracias"):
                self._pending_profile_updates.pop(channel)
                msg = "👍 Entendido, no guardé nada."
                history.append(LLMMessage(role="user", content=instruction))
                history.append(LLMMessage(role="assistant", content=msg))
                return TaskResult(success=True, steps_completed=0, steps_total=0, response=msg)
            else:
                # El usuario dijo otra cosa → descartar el pending y procesar normalmente
                self._pending_profile_updates.pop(channel)

        # Construir mensajes para el LLM (prompt dinámico con perfiles + fecha/hora)
        system_prompt = self._build_system_prompt(channel=channel)

        # Construir el mensaje del usuario (con imagen si corresponde)
        if image_b64:
            user_content = self._build_image_message(instruction, image_b64, image_mime)
        else:
            user_content = instruction

        messages = [
            LLMMessage(role="system", content=system_prompt),
            *history[-20:],  # Últimos 20 mensajes de contexto
            LLMMessage(role="user", content=user_content),
        ]

        # ── Seleccionar modelo óptimo para la tarea ───────────────────
        routed_provider, routed_model = self._select_model_for_task(instruction, has_image=bool(image_b64))
        if routed_model:
            logger.info(
                "orchestrator.model_routed",
                model=routed_model,
                provider=routed_provider,
                instruction_preview=instruction[:60],
            )

        try:
            # 1. Planificar con LLM (con retry si devuelve vacío)
            llm_response = await self.llm.complete(
                messages,
                temperature=0.3,
                provider=routed_provider,
                model=routed_model,
            )
            plan = self._parse_plan(llm_response.content)

            if not plan and not llm_response.content.strip():
                logger.info("orchestrator.llm_empty_retry", attempt=2)
                llm_response = await self.llm.complete(
                    messages, temperature=0.5,
                    provider=routed_provider, model=routed_model,
                )
                plan = self._parse_plan(llm_response.content)

            clean = ""
            if not plan:
                # Limpiar respuesta: quitar <think> tags y JSON residual
                clean = llm_response.content.strip()
                logger.debug("orchestrator.no_plan_raw", raw_length=len(clean), raw_preview=clean[:200])
                clean = re.sub(r"<think>.*?</think>", "", clean, flags=re.DOTALL).strip()
                # Si parece JSON pero _parse_plan falló, intentar reparar y ejecutar
                if clean.startswith("{"):
                    obj = None
                    try:
                        obj = json.loads(clean)
                    except json.JSONDecodeError:
                        obj = self._try_repair_json(clean)
                    if isinstance(obj, dict) and obj.get("steps"):
                        # Recuperamos un plan válido — ejecutar en vez de descartar
                        logger.info("orchestrator.recovered_plan_from_no_plan", steps=len(obj["steps"]))
                        plan = obj
                    elif isinstance(obj, dict):
                        clean = obj.get("response", clean)

            # Si recuperamos un plan, no retornar aquí — caer al bloque de ejecución
            if not plan:
                logger.debug("orchestrator.no_plan_clean", clean_length=len(clean), clean_preview=clean[:200])

                # Si la respuesta quedó vacía (solo había <think> tags), ejecutar directamente como búsqueda
                if not clean and instruction.strip():
                    clean = await self._empty_response_fallback(instruction)

                if not clean:
                    clean = f"No pude procesar tu consulta correctamente. Intenta reformularla."

                # Detectar info personal en mensajes donde el LLM no generó JSON plan
                profile_msg = await self._detect_profile_info(instruction, channel)
                if profile_msg:
                    clean += profile_msg

                return TaskResult(
                    success=True,
                    steps_completed=0,
                    steps_total=0,
                    response=self._clean_response(clean),
                )

            thinking = plan.get("thinking", "")
            steps = plan.get("steps", [])
            response_text = plan.get("response", "")

            # Detectar si el LLM quiere actualizar el perfil del usuario
            profile_update = plan.get("profile_update")
            if profile_update:
                pu_field = profile_update.get("field", "")
                pu_value = profile_update.get("value", "")
                pu_section = profile_update.get("section", "Personal")
                self._pending_profile_updates[channel] = {
                    "field": pu_field,
                    "value": pu_value,
                    "section": pu_section,
                }
                response_text += (
                    f"\n\n💾 Detecté info nueva: **{pu_field}** = {pu_value}. "
                    f"¿Quieres que lo guarde en tu perfil? Responde 'sí' o 'no'."
                )
                logger.info("orchestrator.profile_update_pending", field=pu_field, value=pu_value)

            logger.info(
                "orchestrator.plan",
                steps=len(steps),
                thinking=thinking[:100],
            )

            # 2. Ejecutar pasos con planificación reactiva
            #    - Ejecuta los steps iniciales
            #    - Después de cada batch, el LLM evalúa resultados y decide si necesita más pasos
            #    - Máximo MAX_REACTIVE_ITERATIONS iteraciones para evitar loops infinitos
            MAX_REACTIVE_ITERATIONS = 8
            all_results = []
            all_errors = []
            total_steps = 0
            iteration = 0

            while steps and iteration < MAX_REACTIVE_ITERATIONS:
                iteration += 1
                batch_results = []
                batch_errors = []

                for i, step in enumerate(steps):
                    step_result = await self._execute_step(
                        step, i + total_steps, channel, instruction
                    )
                    # Operación peligrosa pausada — pedir confirmación al usuario
                    if step_result.get("confirmation_required"):
                        response_text += step_result.get("confirmation_msg", "")
                        # Interrumpir ejecución del plan hasta recibir confirmación
                        steps = []
                        break
                    if step_result["ok"]:
                        batch_results.extend(step_result["results"])
                    if step_result["errors"]:
                        batch_errors.extend(step_result["errors"])

                total_steps += len(steps)
                all_results.extend(batch_results)
                all_errors.extend(batch_errors)

                # Planificación reactiva: preguntar al LLM si necesita más pasos
                if iteration < MAX_REACTIVE_ITERATIONS:
                    next_steps = await self._reactive_replan(
                        instruction, steps, batch_results, batch_errors, system_prompt
                    )
                    if next_steps:
                        steps = next_steps
                        logger.info(
                            "orchestrator.reactive_replan",
                            iteration=iteration,
                            new_steps=len(next_steps),
                        )
                    else:
                        break  # LLM decidió que no necesita más pasos
                else:
                    logger.warning("orchestrator.max_iterations", iterations=iteration)
                    break

            # 3. Pedir al LLM que resuma los resultados (o errores)
            response_text = await self._summarize_for_user(
                instruction, all_results, all_errors, response_text
            )

            # Actualizar historial
            history.append(LLMMessage(role="user", content=instruction))
            history.append(LLMMessage(role="assistant", content=response_text))

            # Limitar historial
            if len(history) > 50:
                self._conversation_history[channel] = history[-50:]

            return TaskResult(
                success=True,
                steps_completed=total_steps,
                steps_total=total_steps,
                results=all_results,
                response=response_text,
            )

        except Exception as exc:
            logger.error("orchestrator.error", error=str(exc))
            # Dar mensaje amigable según el tipo de error, NUNCA exponer detalles técnicos
            err_str = str(exc).lower()
            if "insufficient_balance" in err_str or "insufficient balance" in err_str:
                friendly = (
                    "\u26a0\ufe0f El servicio de inteligencia artificial no tiene saldo disponible. "
                    "Contactá al administrador para recargar el balance."
                )
            elif "429" in err_str or "rate_limit" in err_str or "rate limit" in err_str:
                friendly = (
                    "\u23f3 El servicio está temporalmente saturado. "
                    "Esperá unos segundos e intentá de nuevo."
                )
            elif "401" in err_str or "unauthorized" in err_str or "invalid.*key" in err_str:
                friendly = (
                    "\u26a0\ufe0f Hay un problema de autenticación con el servicio de IA. "
                    "Contactá al administrador."
                )
            elif "timeout" in err_str or "timed out" in err_str:
                friendly = (
                    "\u23f3 La solicitud tardó demasiado. Intentá de nuevo o con una consulta más simple."
                )
            else:
                friendly = (
                    "\u274c Hubo un problema procesando tu solicitud. Intentá de nuevo en unos segundos."
                )
            return TaskResult(
                success=False,
                steps_completed=0,
                steps_total=0,
                error=str(exc),
                response=friendly,
            )

    # ── Ejecución de steps y planificación reactiva ─────────────────

    async def _execute_step(
        self, step: dict, step_index: int, channel: str, instruction: str
    ) -> dict:
        """Ejecuta un step individual y retorna resultados + errores."""
        event_name = step.get("event", "")
        event_data = step.get("data", {})
        description = step.get("description", "")

        # Forzar scheduler.add_job: event_name siempre task.execute + inyectar chat_id
        if event_name == "scheduler.add_job":
            event_data["event_name"] = "task.execute"
            ed = event_data.get("event_data", {})
            if not ed.get("instruction"):
                ed["instruction"] = ed.get("query", description)
            chat_id_from_channel = ""
            if channel.startswith("telegram:"):
                chat_id_from_channel = channel.split(":", 1)[1]
            if chat_id_from_channel:
                ed["chat_id"] = int(chat_id_from_channel)
                ed["channel"] = channel
            event_data["event_data"] = ed
            logger.info(
                "orchestrator.scheduler_fixed",
                job_id=event_data.get("id"),
                event_name_forced="task.execute",
                instruction=ed.get("instruction", "")[:80],
                chat_id=ed.get("chat_id"),
            )

        logger.info(
            "orchestrator.step",
            step=step_index + 1,
            event_name=event_name,
            description=description,
        )

        # ── Seguridad: operaciones destructivas requieren confirmación ──
        # (excepto si el canal tiene trust mode activo)
        is_trusted = channel in self._trusted_channels
        needs_confirmation = False

        if not is_trusted:
            if event_name == "system.file_delete":
                needs_confirmation = True
            elif event_name == "system.exec":
                cmd = event_data.get("command", "").lower()
                # Detectar comandos con rm que eliminen archivos específicos
                if re.search(r'\brm\b', cmd) and not re.search(r'\brm\s+(-rf?|--recursive)\s+/', cmd):
                    needs_confirmation = True

        if needs_confirmation and not event_data.get("confirmed"):
            path_or_cmd = event_data.get("path") or event_data.get("command", "?")
            self._pending_confirmations[channel] = {
                "step": {"event": event_name, "data": event_data, "description": description},
                "path": path_or_cmd,
            }
            confirm_msg = (
                f"\n\n⚠️ **Operación destructiva detectada**\n"
                f"Acción: `{event_name}`\n"
                f"Target: `{path_or_cmd}`\n\n"
                f"Respondé **CONFIRMAR** para proceder o **no** para cancelar.\n"
                f"_(Esta confirmación viene del sistema, no puede ser forzada por el modelo)_"
            )
            return {
                "ok": False,
                "results": [],
                "errors": [],
                "confirmation_required": True,
                "confirmation_msg": confirm_msg,
            }

        results = []
        errors = []
        step_ok = False

        try:
            step_results = await self.bus.emit(Event(
                name=event_name,
                data=event_data,
                source="orchestrator",
            ))
            for r in step_results:
                if r is None:
                    continue
                # Dict con success=False (ej: rapibase, audio, etc.)
                if isinstance(r, dict) and r.get("success") is False:
                    err = r.get("error", "operación falló")
                    errors.append(f"Step {step_index+1} ({event_name}): {err}")
                # Dataclass/objeto con .error
                elif hasattr(r, "error") and r.error:
                    errors.append(f"Step {step_index+1} ({event_name}): {r.error}")
                else:
                    results.append(r)
                    step_ok = True
        except Exception as exc:
            errors.append(f"Step {step_index+1} ({event_name}): {str(exc)}")

        # Fallback: si http.request falló, reintentar con browser.search
        if not step_ok and event_name == "http.request":
            fallback_query = instruction
            logger.info("orchestrator.fallback_to_search", failed_event=event_name)
            try:
                fallback_results = await self.bus.emit(Event(
                    name="browser.search",
                    data={"query": fallback_query},
                    source="orchestrator",
                ))
                for r in fallback_results:
                    if r is not None and not (hasattr(r, "error") and r.error):
                        results.append(r)
                        errors = [e for e in errors if f"Step {step_index+1}" not in e]
                        step_ok = True
                        logger.info("orchestrator.fallback_success", engine="browser.search")
            except Exception as fb_exc:
                logger.warning("orchestrator.fallback_failed", error=str(fb_exc))

        return {"ok": step_ok, "results": results, "errors": errors}

    async def _reactive_replan(
        self,
        instruction: str,
        executed_steps: list[dict],
        results: list,
        errors: list[str],
        system_prompt: str,
    ) -> list[dict] | None:
        """Después de ejecutar steps, pregunta al LLM si necesita más pasos.

        Retorna lista de nuevos steps, o None si el LLM decide que terminó.
        """
        # Si no hubo resultados ni errores, no re-planificar
        if not results and not errors:
            return None

        results_summary = self._summarize_results(results) if results else "(sin datos)"
        error_summary = "\n".join(errors) if errors else ""

        steps_desc = ", ".join(
            f"{s.get('event', '?')}: {s.get('description', '')}" for s in executed_steps
        )

        replan_prompt = (
            "Acabás de ejecutar estos pasos:\n"
            f"  {steps_desc}\n\n"
            f"Resultados obtenidos:\n{results_summary[:3000]}\n\n"
        )
        if error_summary:
            replan_prompt += f"Errores:\n{error_summary}\n\n"

        replan_prompt += (
            f"Instrucción original del usuario: {instruction}\n\n"
            "¿Necesitás ejecutar más pasos para cumplir la tarea? "
            "Respondé SOLO con JSON:\n"
            '- Si necesitás más pasos: {"next_steps": [{"event": "...", "data": {...}, "description": "..."}]}\n'
            '- Si ya tenés toda la info o el paso falló por configuración: {"done": true}\n'
            "REGLAS:\n"
            "- SOLO JSON, sin texto extra.\n"
            "- Si un módulo falló por falta de configuración (ej: RapiBase no configurado, API key faltante), "
            "respondé {\"done\": true}. NO intentes messaging.send ni pidas al usuario que configure .env.\n"
            "- NO uses messaging.send para notificar al usuario — la respuesta llega automáticamente."
        )

        try:
            replan_msgs = [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=replan_prompt),
            ]
            resp = await self.llm.complete(replan_msgs, temperature=0.2)
            clean = re.sub(r"<think>.*?</think>", "", resp.content, flags=re.DOTALL).strip()

            # Extraer JSON
            match = re.search(r'\{.*\}', clean, re.DOTALL)
            if not match:
                return None

            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                data = self._try_repair_json(match.group())
                if not data:
                    return None

            if data.get("done"):
                return None

            next_steps = data.get("next_steps", [])
            if next_steps and isinstance(next_steps, list):
                # Validar que cada step tiene al menos "event"
                valid = [s for s in next_steps if isinstance(s, dict) and s.get("event")]
                return valid if valid else None

            return None
        except Exception as exc:
            logger.debug("orchestrator.replan_error", error=str(exc))
            return None

    async def _summarize_for_user(
        self,
        instruction: str,
        results: list,
        errors: list[str],
        fallback_response: str,
    ) -> str:
        """Pide al LLM que resuma los resultados obtenidos para el usuario."""
        has_results = results and any(r is not None for r in results)
        has_errors = len(errors) > 0

        if not has_results and not has_errors:
            return fallback_response

        results_summary = self._summarize_results(results) if has_results else "(sin datos)"
        error_summary = "\n".join(errors) if has_errors else ""

        try:
            tz = ZoneInfo("America/Argentina/Buenos_Aires")
        except Exception:
            tz = None
        _now_ar = datetime.now(tz)
        _today_str = _now_ar.strftime("%A %d/%m/%Y").capitalize()

        context_parts = [f"📅 Fecha y hora actual: {_today_str} {_now_ar.strftime('%H:%M')} (Argentina)"]
        context_parts.append(f"Tarea original: {instruction}")
        if has_results:
            context_parts.append(f"Datos obtenidos:\n{results_summary}")
        if has_errors:
            context_parts.append(f"Errores en pasos:\n{error_summary}")

        summary_messages = [
            LLMMessage(role="system", content=(
                "Eres un asistente útil. Tu tarea es transformar los datos crudos obtenidos en una "
                "respuesta clara, completa y bien formateada para el usuario en Telegram. "
                "REGLAS ESTRICTAS: "
                "1. SIEMPRE responde en ESPAÑOL (español de Argentina). "
                "2. USA los datos reales que se obtuvieron, NO inventes información. "
                "3. Organiza la información de forma legible con secciones si es necesario. "
                "4. Si los datos incluyen contenido de una página web, extrae lo más relevante y preséntalo de forma útil. "
                "5. Si hubo errores, NO le digas al usuario que busque él mismo. "
                "   En su lugar, usa tu conocimiento para dar la mejor respuesta posible. "
                "6. FORMATO para Telegram: "
                "   - Usá **negritas** para títulos y datos clave. "
                "   - Usá emojis con moderación para separar secciones y hacer visual: 🌡️ ☀️ 🌧️ 📰 🚀 💡 📊 etc. "
                "   - NO uses ### headers, ---, tablas markdown ni HTML. "
                "   - NO pongas disclaimers ni aclaraciones legales. "
                "   - Sé directo y conversacional, como un amigo que te cuenta las noticias/datos. "
                "7. Sé conciso pero informativo — incluye datos específicos, cifras, fechas, nombres. "
                "8. NUNCA sugieras al usuario buscar en Google u otro buscador — vos sos su buscador. "
                "9. NUNCA respondas en inglés. El idioma es ESPAÑOL siempre. "
                "10. Si los datos ya vienen formateados con emojis y estructura (ej: datos de clima o noticias), "
                "    usá esa estructura como base, no la reescribas desde cero. "
                "11. PRIORIZAR artículos marcados como (HOY) o (AYER). Ignorar artículos de hace semanas/meses si "
                "    el usuario pidió noticias 'del día de hoy' o 'actuales'. "
                "12. NUNCA menciones errores técnicos internos al usuario (URLs de servicios, .env, API keys, "
                "    configuración del servidor, nombres de módulos internos). "
                "    Si un servicio falló, decí algo como 'esa funcionalidad no está disponible ahora' "
                "    y ofrecé una alternativa o solución creativa con lo que sí tengas."
            )),
            LLMMessage(role="user", content="\n\n".join(context_parts)),
        ]
        summary_response = await self.llm.complete(summary_messages, temperature=0.5)
        response_text = re.sub(
            r"<think>.*?</think>", "", summary_response.content, flags=re.DOTALL
        ).strip()
        return self._clean_response(response_text)

    # ── Fallback cuando el LLM devuelve vacío ──────────────────────

    @staticmethod
    def _extract_search_query(instruction: str) -> str:
        """Extrae keywords de búsqueda de una instrucción natural.

        'dime que noticias encuentras de "openclaw" es un software libre...'
        → 'openclaw software libre'
        """
        # 1. Extraer texto entre comillas (alta prioridad)
        quoted = re.findall(r'"([^"]+)"', instruction)

        # 2. Quitar filler words comunes en español
        filler = {
            'dame', 'dime', 'busca', 'buscar', 'encuentra', 'encontrar', 'quiero',
            'necesito', 'muestrame', 'muestra', 'sobre', 'acerca', 'noticias',
            'resumen', 'resumir', 'importante', 'importantes', 'trata', 'tratar',
            'evitar', 'evita', 'filtra', 'filtrar', 'contenido', 'cosas', 'demas',
            'deseado', 'para', 'como', 'cual', 'cuales', 'donde', 'cuando',
            'porque', 'pero', 'tambien', 'también', 'solo', 'sólo', 'algo',
            'nuevo', 'nueva', 'nuevos', 'nuevas', 'todo', 'toda', 'todos',
            'todas', 'mas', 'más', 'mejor', 'mejores', 'hoy', 'actual',
            'actuales', 'dia', 'día', 'mundo', 'mundial', 'nivel', 'favor',
            'puedes', 'podés', 'podrías', 'podrias', 'quisiera', 'que', 'del',
            'los', 'las', 'una', 'uno', 'unos', 'unas', 'con', 'sin', 'por',
            'este', 'esta', 'estos', 'estas', 'ese', 'esa', 'esos', 'esas',
        }

        words = re.findall(r'\w+', instruction.lower())
        keywords = [w for w in words if w not in filler and len(w) > 2]

        # 3. Priorizar quoted terms + keywords únicos
        result_parts = quoted + [kw for kw in keywords if kw.lower() not in ' '.join(quoted).lower()]
        query = ' '.join(result_parts[:8])  # Máximo 8 términos

        return query if query.strip() else instruction[:80]

    async def _empty_response_fallback(self, instruction: str) -> str:
        """Fallback cuando el LLM devuelve respuesta vacía.

        Estrategia cascada:
        1. Si parece query de noticias → news.search con keywords limpios
        2. Si news.search no encuentra nada relevante → browser.search
        3. Si nada funciona → mensaje de error amigable
        """
        query = self._extract_search_query(instruction)
        logger.info("orchestrator.empty_response_fallback", instruction=instruction[:80], extracted_query=query)

        # Detectar si es consulta de noticias
        news_kw = ['noticias', 'tendencia', 'novedades', 'actualidad', 'resumen',
                    'tecnología', 'startups', 'innovación', 'news', 'trends']
        is_news = any(kw in instruction.lower() for kw in news_kw)

        result_data = None

        # Paso 1: Si parece noticias, intentar news.search primero
        if is_news:
            try:
                logger.info("orchestrator.fallback_trying", step="news.search", query=query)
                search_results = await self.bus.emit(Event(
                    name="news.search",
                    data={"query": query},
                    source="orchestrator",
                ))
                for r in search_results:
                    if r is not None and isinstance(r, dict):
                        # Verificar que realmente encontró artículos relevantes
                        count = r.get("articles_count", 0)
                        if count > 0:
                            result_data = r
                            logger.info("orchestrator.fallback_news_ok", articles=count)
                            break
                        else:
                            logger.info("orchestrator.fallback_news_empty", articles=count)
            except Exception as exc:
                logger.warning("orchestrator.fallback_news_error", error=str(exc))

        # Paso 2: Si no hay resultados de noticias (o no era de noticias), usar browser.search
        if result_data is None:
            try:
                logger.info("orchestrator.fallback_trying", step="browser.search", query=query)
                search_results = await self.bus.emit(Event(
                    name="browser.search",
                    data={"query": query},
                    source="orchestrator",
                ))
                for r in search_results:
                    if r is not None and not (hasattr(r, "error") and r.error):
                        result_data = r
                        logger.info("orchestrator.fallback_browser_ok")
                        break
            except Exception as exc:
                logger.warning("orchestrator.fallback_browser_error", error=str(exc))

        # Paso 3: Resumir los resultados con LLM
        if result_data is not None:
            try:
                summary = self._summarize_results([result_data])
                summary_msgs = [
                    LLMMessage(role="system", content=(
                        "Eres un asistente útil. Transformá los datos en una respuesta para Telegram. "
                        "REGLAS: Respondé en español argentino. Usá **negritas** para datos clave. "
                        "Usá emojis con moderación. NO pongas disclaimers. Sé directo y conversacional. "
                        "Si los datos ya vienen formateados, usá esa estructura como base. "
                        "Si NO hay datos relevantes a la pregunta, decilo honestamente."
                    )),
                    LLMMessage(role="user", content=f"Tarea: {instruction}\n\nDatos:\n{summary}"),
                ]
                resp = await self.llm.complete(summary_msgs, temperature=0.5)
                clean = re.sub(r"<think>.*?</think>", "", resp.content, flags=re.DOTALL).strip()
                clean = self._clean_response(clean)
                logger.info("orchestrator.fallback_summary_ok", response_len=len(clean))
                return clean
            except Exception as exc:
                logger.warning("orchestrator.fallback_summary_error", error=str(exc))

        return ""

    async def _detect_profile_info(self, instruction: str, channel: str) -> str:
        """Detecta info personal en mensajes del usuario y genera profile_update.

        Se usa cuando el LLM no devolvió JSON (respondió texto plano)
        pero el usuario compartió datos personales (equipo, gustos, etc).
        Returns confirmation message to append, or empty string.
        """
        # Solo intentar si el mensaje es corto (info personal, no queries largos)
        if len(instruction) > 200:
            return ""
        try:
            extract_msgs = [
                LLMMessage(role="system", content=(
                    "Analiza el mensaje del usuario y extrae información personal si la hay. "
                    "Responde SOLO con JSON. Si NO hay info personal, responde: {}\n"
                    "Si hay info, responde con: "
                    '{"updates": [{"field": "NombreCampo", "value": "valor", "section": "Sección"}]}\n'
                    "Secciones válidas: Personal, Location, Preferences, Notes\n"
                    "Ejemplos de campos: Nationality, Football Team, Hobbies, Favorite Music, Birthday, etc.\n"
                    "SOLO JSON, sin texto extra."
                )),
                LLMMessage(role="user", content=instruction),
            ]
            resp = await self.llm.complete(extract_msgs, temperature=0.1)
            clean = re.sub(r"<think>.*?</think>", "", resp.content, flags=re.DOTALL).strip()

            # Extraer JSON
            match = re.search(r'\{.*\}', clean, re.DOTALL)
            if not match:
                return ""
            data = json.loads(match.group())
            updates = data.get("updates", [])
            if not updates:
                return ""

            parts = []
            for upd in updates:
                field = upd.get("field", "")
                value = upd.get("value", "")
                section = upd.get("section", "Personal")
                if field and value:
                    self._pending_profile_updates[channel] = {
                        "field": field,
                        "value": value,
                        "section": section,
                    }
                    parts.append(f"**{field}** = {value}")
                    logger.info("orchestrator.profile_detected_no_plan", field=field, value=value)
                    break  # Uno a la vez para confirmación

            if parts:
                return f"\n\n💾 Detecté info nueva: {', '.join(parts)}. ¿Querés que lo guarde en tu perfil? Respondé 'sí' o 'no'."
            return ""
        except Exception as exc:
            logger.debug("orchestrator.profile_detect_error", error=str(exc))
            return ""

    @staticmethod
    def _clean_response(text: str) -> str:
        """
        Limpia la respuesta del LLM antes de enviarla al usuario.
        - Elimina caracteres chinos (artefactos de DeepSeek-R1)
        - Convierte markdown pesado a formato legible en Telegram
        """
        # 1. Eliminar caracteres CJK (chinos/japoneses/coreanos) — artefactos de DeepSeek
        text = re.sub(r'[\u4e00-\u9fff\u3400-\u4dbf\u2e80-\u2eff\u3000-\u303f]+', '', text)

        # 2. Convertir headers markdown a texto con emoji
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)

        # 3. Eliminar líneas de separación markdown (---)
        text = re.sub(r'^-{3,}\s*$', '', text, flags=re.MULTILINE)

        # 4. Limpiar negritas markdown (**texto**) — Telegram soporta esto parcialmente
        # Dejamos las negritas simples pero eliminamos exceso
        # text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # descomentar si Telegram no soporta

        # 5. Limpiar líneas vacías excesivas (máximo 2 seguidas)
        text = re.sub(r'\n{3,}', '\n\n', text)

        # 6. Limpiar espacios residuales
        text = text.strip()

        return text

    @staticmethod
    def _try_repair_json(text: str) -> dict | None:
        """Intenta reparar JSON malformado de LLMs.

        Errores comunes:
        - steps array con "description" fuera del objeto step:
          [{"event":"x","data":{}}, "description": "y"]
          → [{"event":"x","data":{},"description":"y"}]
        - Trailing commas
        - Single quotes instead of double quotes
        """
        try:
            # Fix 1: trailing commas antes de } o ]
            fixed = re.sub(r',\s*([}\]])', r'\1', text)
            obj = json.loads(fixed)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        try:
            # Fix 2: "description" fuera del step object en steps array
            # Pattern: }, "description": "..."] → , "description": "..."}]
            fixed = re.sub(
                r'\}\s*,\s*"description"\s*:\s*"([^"]*?)"\s*\]',
                r', "description": "\1"}]',
                text,
            )
            # También manejar múltiples steps con el mismo problema
            fixed = re.sub(
                r'\}\s*,\s*"description"\s*:\s*"([^"]*?)"\s*,\s*\{',
                r', "description": "\1"}, {',
                fixed,
            )
            fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
            obj = json.loads(fixed)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        try:
            # Fix 3: single quotes → double quotes (last resort)
            fixed = text.replace("'", '"')
            fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
            obj = json.loads(fixed)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        return None

    def _parse_plan(self, content: str) -> dict | None:
        """
        Intenta parsear el JSON del plan del LLM.

        Maneja patrones de DeepSeek-R1:
        - <think>...</think> tags (se eliminan)
        - Múltiples bloques JSON (se mergean steps)
        - JSON dentro de bloques ```json ... ```
        - JSON puro
        """
        raw = content.strip()

        # 1. Eliminar tags <think>...</think> de DeepSeek-R1
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        # 2. Extraer JSON de bloques de código
        code_blocks = re.findall(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
        if code_blocks:
            raw = "\n".join(code_blocks)

        # 3. Encontrar todos los objetos JSON en el texto
        json_objects = []
        depth = 0
        start_idx = None
        for i, ch in enumerate(raw):
            if ch == "{":
                if depth == 0:
                    start_idx = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start_idx is not None:
                    candidate = raw[start_idx : i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            json_objects.append(obj)
                    except json.JSONDecodeError:
                        # Intentar reparar errores comunes de LLMs
                        repaired = self._try_repair_json(candidate)
                        if repaired and isinstance(repaired, dict):
                            json_objects.append(repaired)
                    start_idx = None

        if not json_objects:
            return None

        # 4. Si hay un solo JSON con "steps", usarlo directamente
        if len(json_objects) == 1 and "steps" in json_objects[0]:
            return json_objects[0]

        # 5. Si hay múltiples JSONs, mergear todos los steps en un solo plan
        merged_steps = []
        thinking = ""
        response = ""
        profile_update = None

        for obj in json_objects:
            if "steps" in obj:
                merged_steps.extend(obj.get("steps", []))
            if obj.get("thinking") and not thinking:
                thinking = obj["thinking"]
            if obj.get("response"):
                response = obj["response"]  # Quedarse con la última
            if obj.get("profile_update"):
                profile_update = obj["profile_update"]

        plan = {
            "thinking": thinking,
            "steps": merged_steps,
            "response": response,
        }
        if profile_update:
            plan["profile_update"] = profile_update

        return plan if merged_steps or response else None

    def _summarize_results(self, results: list[Any]) -> str:
        """Convierte resultados de módulos a texto legible."""
        summaries = []
        for r in results:
            if r is None:
                continue
            if hasattr(r, "__dataclass_fields__"):
                # Dataclass → dict legible
                # Para browser results, el 'content' puede ser grande — incluir más texto
                d = {}
                for k, v in r.__dict__.items():
                    if k.startswith("_"):
                        continue
                    sv = str(v)
                    # Campos de contenido grandes: permitir más texto
                    if k in ("content", "body"):
                        d[k] = sv[:5000]
                    elif k == "html":
                        continue  # Omitir HTML crudo, no es útil para el resumen
                    else:
                        d[k] = sv[:1000]
                summaries.append(json.dumps(d, indent=2, ensure_ascii=False))
            elif isinstance(r, dict):
                # Si tiene summary_text (ej: news.search), usar directamente
                if "summary_text" in r:
                    summaries.append(r["summary_text"][:5000])
                else:
                    summaries.append(json.dumps(r, indent=2, ensure_ascii=False)[:5000])
            else:
                summaries.append(str(r)[:3000])

        return "\n---\n".join(summaries) if summaries else "(sin resultados)"
