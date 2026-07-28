"""
prompt_logger.py
Registra cada interaccion del usuario en un archivo JSON diario dentro de prompts/.

Estructura de cada entrada:
{
    "id": "uuid4",
    "timestamp": "2026-07-27T10:30:00",
    "pregunta": "ventas por departamento",
    "tipo": "consulta" | "informe" | "grafico",
    "modelo_llm": "llama-3.3-70b-versatile",
    "proveedor_llm": "groq",
    "duracion_seg": 4.2,
    "exito": true,
    "archivos_generados": ["reports/charts/chart_xxx.png"],
    "sql_queries": [
        {"nombre": "ventas_departamento", "sql": "SELECT ..."}
    ],
    "feedback": "bueno" | "malo" | "regular" | "",
    "feedback_msg": "La respuesta fue precisa y clara"
}

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
) -> str:
    """
    Registra una nueva interaccion. Devuelve el id generado
    para poder actualizar el feedback despues.

    Parametros
    ----------
    sql_queries : list[dict] | None
        Lista de consultas SQL ejecutadas durante la interaccion.
        Cada dict debe tener al menos {"nombre": str, "sql": str}.
        Ej: [{"nombre": "ventas_depto", "sql": "SELECT ..."}]
    """
    entrada = {
        'id': str(uuid.uuid4()),
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'pregunta': pregunta,
        'tipo': tipo,
        'modelo_llm': modelo_llm,
        'proveedor_llm': proveedor_llm,
        'duracion_seg': round(duracion_seg, 2),
        'exito': exito,
        'archivos_generados': archivos_generados or [],
        'sql_queries': sql_queries or [],
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
