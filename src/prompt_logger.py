"""
prompt_logger.py
Registra cada interaccion del usuario en un archivo JSON diario dentro de prompts/.

Punto unico de registro: src/server.py llama a registrar_prompt() una sola vez
por request de /chat, sin importar si vino de Streamlit o de Telegram. Ambas
interfaces mandan su origen en el campo "origen" del request y ese valor se
persiste como "prompt_source" — asi todo queda en el mismo archivo diario con
trazabilidad de fuente.

Estructura de cada entrada:
{
    "id": "uuid4",
    "timestamp": "2026-07-27T10:30:00",
    "pregunta": "ventas por departamento",
    "tipo": "consulta" | "informe" | "grafico" | "sobre_datos" | "conversacional" | "error",
    "prompt_source": "streamlit" | "telegram",
    "user": "default",
    "turno": 3,
    "modelo_llm": "llama-3.3-70b-versatile",
    "proveedor_llm": "groq",
    "duracion_seg": 4.2,
    "exito": true,
    "archivos_generados": ["reports/charts/chart_xxx.png"],
    "sql_queries": [
        {"nombre": "consulta_principal", "sql": "SELECT ..."},
        {"nombre": "analista_ronda_1: por que cayeron las ventas de caballero", "sql": "SELECT ..."}
    ],
    "origen_sql": "plantilla" | "plantilla_ref" | "generador" | "",
    "feedback": "bueno" | "malo" | "regular" | "",
    "feedback_msg": "La respuesta fue precisa y clara"
}

sql_queries acumula TODAS las consultas SQL ejecutadas para producir la
respuesta, no solo la primera: si el agente analista pidio rondas adicionales
de datos, o un informe ejecuto una consulta por bloque, cada una queda como
una entrada separada con su "nombre" describiendo de donde salio.

Concurrencia
------------
Todas las operaciones de lectura-modificacion-escritura sobre el archivo JSON
estan protegidas con un FileLock (filelock). Esto garantiza que dos usuarios
concurrentes (dos sesiones Streamlit o dos procesos) no se pisen:

    - registrar_prompt: adquiere lock → lee → append → escribe → libera lock
    - actualizar_feedback: adquiere lock → lee → modifica → escribe → libera lock

El archivo de lock es <nombre_json>.lock (ej: prompts_20260728.json.lock) y se
crea automaticamente junto al JSON. No contiene datos — es solo un semaforo.
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

from filelock import FileLock, Timeout

PROMPTS_DIR = Path(__file__).resolve().parent.parent / 'prompts'
# Tiempo maximo de espera para adquirir el lock (segundos).
# Si tras LOCK_TIMEOUT segundos no se puede adquirir, se lanza Timeout.
LOCK_TIMEOUT = 10


def _archivo_hoy() -> Path:
    nombre = datetime.now().strftime('prompts_%Y%m%d.json')
    return PROMPTS_DIR / nombre


def _lock_hoy() -> FileLock:
    """Devuelve el FileLock asociado al archivo JSON de hoy."""
    return FileLock(str(_archivo_hoy()) + '.lock', timeout=LOCK_TIMEOUT)


def _cargar_sin_lock(ruta: Path) -> list:
    """Lee el JSON del archivo. Llamar SOLO dentro de un bloque con lock adquirido."""
    if ruta.exists():
        try:
            return json.loads(ruta.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _guardar_sin_lock(ruta: Path, entradas: list):
    """Escribe el JSON atomicamente (write a temp + rename). Llamar SOLO con lock."""
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    # Escritura atomica: escribir en .tmp y luego renombrar para evitar
    # archivos truncados si el proceso muere a mitad de escritura.
    tmp = ruta.with_suffix('.tmp')
    tmp.write_text(
        json.dumps(entradas, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    tmp.replace(ruta)


def registrar_prompt(
    pregunta: str,
    tipo: str,
    duracion_seg: float,
    exito: bool,
    archivos_generados: list[str] | None = None,
    modelo_llm: str = '',
    proveedor_llm: str = '',
    sql_queries: list[dict] | None = None,
    prompt_source: str = '',
    origen_sql: str = '',
    user: str = 'default',
    turno: int = 0,
) -> str:
    """
    Registra una nueva interaccion. Devuelve el id generado
    para poder actualizar el feedback despues.

    Parametros
    ----------
    sql_queries : list[dict] | None
        Lista de TODAS las consultas SQL ejecutadas para producir la
        respuesta (consulta principal + rondas del agente analista +
        consultas por bloque de un informe + consultas complementarias
        de SOBRE_DATOS). Cada dict debe tener al menos {"nombre": str, "sql": str}.
    prompt_source : str
        De donde vino la interaccion: "streamlit" | "telegram".
    user : str
        Identidad del usuario que disparo la interaccion. Por ahora sin una
        fuente real de identidad (Streamlit/Telegram no autentican usuarios
        individuales todavia), queda en "default" — el campo ya existe en el
        log para cuando se conecte esa fuente, sin tener que migrar entradas
        viejas.
    turno : int
        Numero de turno dentro de la sesion (ver Sesion.turno_actual en
        session_store.py) — permite cruzar una entrada del log con el
        historial de esa sesion.
    origen_sql : str
        De donde salio el SQL de la consulta principal (ver src/plantillas.py
        y orquestador.py::procesar_consulta): "plantilla" (ejecutado directo,
        sin generador ni validador) | "plantilla_ref" (plantilla usada como
        ejemplo few-shot, el generador igual escribio el SQL) | "generador"
        (flujo normal) | "" (ruta sin SQL, ej. conversacional). Sirve para
        medir la tasa de acierto de las plantillas y comparar su feedback
        contra el de las consultas generadas normalmente.
    """
    entrada = {
        'id': str(uuid.uuid4()),
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'pregunta': pregunta,
        'tipo': tipo,
        'prompt_source': prompt_source,
        'user': user,
        'turno': turno,
        'modelo_llm': modelo_llm,
        'proveedor_llm': proveedor_llm,
        'duracion_seg': round(duracion_seg, 2),
        'exito': exito,
        'archivos_generados': archivos_generados or [],
        'sql_queries': sql_queries or [],
        'origen_sql': origen_sql,
        'feedback': '',
        'feedback_msg': '',
    }
    ruta = _archivo_hoy()
    try:
        with _lock_hoy():
            entradas = _cargar_sin_lock(ruta)
            entradas.append(entrada)
            _guardar_sin_lock(ruta, entradas)
    except Timeout:
        # Si no se puede adquirir el lock, loguear de todas formas en memoria
        # (se perdera la persistencia pero no bloqueara al usuario).
        import sys
        print(f'[prompt_logger] WARNING: no se pudo adquirir lock en {LOCK_TIMEOUT}s. Entrada {entrada["id"]} no persistida.', file=sys.stderr)
    return entrada['id']


def actualizar_feedback(
    prompt_id: str,
    feedback: str,
    feedback_msg: str = '',
    sql_queries: list[dict] | None = None,
):
    """
    Actualiza el feedback de una entrada existente por su id.
    feedback: 'bueno' | 'regular' | 'malo'

    Si se provee sql_queries y la entrada aun tiene lista vacia,
    tambien actualiza ese campo (util cuando el SQL se conoce despues
    del registro inicial).
    """
    ruta = _archivo_hoy()
    try:
        with _lock_hoy():
            entradas = _cargar_sin_lock(ruta)
            for entrada in entradas:
                if entrada.get('id') == prompt_id:
                    entrada['feedback'] = feedback
                    entrada['feedback_msg'] = feedback_msg.strip()
                    if sql_queries and not entrada.get('sql_queries'):
                        entrada['sql_queries'] = sql_queries
                    break
            _guardar_sin_lock(ruta, entradas)
    except Timeout:
        import sys
        print(f'[prompt_logger] WARNING: no se pudo adquirir lock para actualizar feedback de {prompt_id}.', file=sys.stderr)


def listar_prompts_hoy() -> list:
    ruta = _archivo_hoy()
    try:
        with _lock_hoy():
            return _cargar_sin_lock(ruta)
    except Timeout:
        return []
