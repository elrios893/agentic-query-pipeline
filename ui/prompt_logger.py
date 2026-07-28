"""
prompt_logger.py
Registra cada interacción del usuario en un archivo JSON diario dentro de prompts/.
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
    "feedback": "bueno" | "malo" | "regular" | "",
    "feedback_msg": "La respuesta fue precisa y clara"
}
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / 'prompts'


def _archivo_hoy() -> Path:
    nombre = datetime.now().strftime('prompts_%Y%m%d.json')
    return PROMPTS_DIR / nombre


def _cargar_log() -> list:
    ruta = _archivo_hoy()
    if ruta.exists():
        try:
            return json.loads(ruta.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _guardar_log(entradas: list):
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    _archivo_hoy().write_text(
        json.dumps(entradas, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def registrar_prompt(
    pregunta: str,
    tipo: str,
    duracion_seg: float,
    exito: bool,
    archivos_generados: list[str] | None = None,
    modelo_llm: str = '',
    proveedor_llm: str = '',
) -> str:
    """
    Registra una nueva interacción. Devuelve el id generado
    para poder actualizar el feedback después.
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
        'feedback': '',
        'feedback_msg': '',
    }
    entradas = _cargar_log()
    entradas.append(entrada)
    _guardar_log(entradas)
    return entrada['id']


def actualizar_feedback(prompt_id: str, feedback: str, feedback_msg: str = ''):
    """
    Actualiza el feedback de una entrada existente por su id.
    feedback: 'bueno' | 'regular' | 'malo'
    """
    entradas = _cargar_log()
    for entrada in entradas:
        if entrada.get('id') == prompt_id:
            entrada['feedback'] = feedback
            entrada['feedback_msg'] = feedback_msg.strip()
            break
    _guardar_log(entradas)


def listar_prompts_hoy() -> list:
    return _cargar_log()
