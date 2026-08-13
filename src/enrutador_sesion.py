"""
src/enrutador_sesion.py
Clasifica cada mensaje del usuario en una de 4 rutas,
considerando el historial de sesión y los DataFrames activos.

Rutas:
  NUEVA_CONSULTA   — pregunta independiente, requiere SQL a BD
  REFINAMIENTO     — variación de la consulta anterior, requiere SQL con contexto
  SOBRE_DATOS      — pregunta sobre resultados ya en memoria, puede responder con Pandas
  CONVERSACIONAL   — respuesta directa sin BD ni Pandas

Usa llama-3.1-8b-instant vía Groq (mismo cliente que el clasificador de analista).
"""
import json
import os
import re
from dotenv import load_dotenv

load_dotenv()

try:
    from tools.tool_pandas import catalogo_para_llm as _catalogo_tool_pandas
except Exception:
    _catalogo_tool_pandas = None

# ---------------------------------------------------------------------------
# Cliente Groq para clasificación (modelo ligero, separado del LLM principal)
# ---------------------------------------------------------------------------
try:
    from groq import Groq as _GroqClass
    _client_router = _GroqClass(api_key=os.getenv('GROQ_API_KEY'))
    MODELO_ROUTER   = os.getenv('GROQ_MODEL_INFERENCE', 'llama-3.1-8b-instant')
except Exception:
    _client_router = None
    MODELO_ROUTER   = None

# ---------------------------------------------------------------------------
# Rutas posibles
# ---------------------------------------------------------------------------
RUTAS = ('NUEVA_CONSULTA', 'REFINAMIENTO', 'SOBRE_DATOS', 'CONVERSACIONAL')

SYSTEM_ENRUTADOR = """Eres un clasificador de intención conversacional para un sistema de análisis de ventas retail.

Tu única tarea es clasificar el mensaje del usuario en UNA de estas 4 rutas:

NUEVA_CONSULTA
  El mensaje pide datos completamente nuevos, sin relación directa con los resultados anteriores.
  Ejemplos: "dame las ventas de marzo", "top 10 tiendas de Bogotá", "ventas por línea en enero".

REFINAMIENTO
  El mensaje modifica, filtra o extiende la consulta anterior.
  Señales: "y en Bogotá?", "solo de Antioquia", "lo mismo pero por tienda", "ordénalo por valor",
           "ahora muéstrame por semana", "filtra solo caballero".

SOBRE_DATOS
  El mensaje pregunta algo que puede responderse con los datos ya obtenidos,
  sin necesitar una nueva consulta a la base de datos.
  Señales: "cuánto representa X del total?", "cuál es la diferencia entre A y B?",
           "qué porcentaje es eso?", "cuánto suman los 3 primeros?", "compara estos dos".

CONVERSACIONAL
  El mensaje no requiere datos ni cálculos. Es una pregunta conceptual, explicación,
  o comentario sobre los resultados en lenguaje natural.
  Señales: "por qué crees que pasó eso?", "qué significa ese número?",
           "es normal esa caída?", "gracias", "puedes explicarme mejor?".

REGLAS ESTRICTAS:
- Si hay duda entre REFINAMIENTO y NUEVA_CONSULTA: elige REFINAMIENTO si el mensaje
  usa palabras como "y", "también", "ahora", "lo mismo", "solo", "filtra", "pero".
- Si hay duda entre SOBRE_DATOS y REFINAMIENTO: elige SOBRE_DATOS si la pregunta
  puede responderse matemáticamente con los datos que ya están en memoria.
- Si no hay historial previo (primer mensaje): SIEMPRE es NUEVA_CONSULTA.
- Responde ÚNICAMENTE con un JSON válido. Sin texto adicional.

Formato de respuesta:
{
  "ruta": "NUEVA_CONSULTA",
  "confianza": "alta",
  "razon": "El usuario pide datos nuevos sobre marzo, sin referencia a resultados anteriores.",
  "df_relevante": null,
  "operacion_sugerida": null,
  "parametros_sugeridos": null
}

Si la ruta es SOBRE_DATOS, incluir también:
  "df_relevante": "df_1"  (nombre del df que contiene los datos necesarios)
  "operacion_sugerida": el nombre EXACTO de una operación de la lista de abajo — nunca inventes un nombre
  "parametros_sugeridos": los parámetros de esa operación, con los nombres de columna reales del df_relevante

Si la ruta es REFINAMIENTO, incluir:
  "contexto_sql": breve descripción de qué ajuste necesita el SQL anterior
"""

if _catalogo_tool_pandas is not None:
    SYSTEM_ENRUTADOR += '\n\n' + _catalogo_tool_pandas()


def clasificar(
    pregunta: str,
    contexto_sesion: dict,
) -> dict:
    """
    Clasifica el mensaje del usuario usando el LLM ligero.

    Args:
        pregunta: mensaje actual del usuario
        contexto_sesion: dict con 'historial' y 'dataframes_activos' del SessionStore

    Returns:
        {
          'ruta': str,
          'confianza': str,
          'razon': str,
          'df_relevante': str | None,
          'operacion_sugerida': str | None,
          'parametros_sugeridos': dict | None,
          'contexto_sql': str | None,
        }
    """
    hay_historial = bool(contexto_sesion.get('historial'))

    # Sin historial → siempre NUEVA_CONSULTA, sin llamar al LLM
    if not hay_historial:
        return {
            'ruta':                  'NUEVA_CONSULTA',
            'confianza':             'alta',
            'razon':                 'Primer mensaje de la sesión.',
            'df_relevante':          None,
            'operacion_sugerida':    None,
            'parametros_sugeridos':  None,
            'contexto_sql':          None,
        }

    # Si el cliente no está disponible → fallback a regex simple
    if _client_router is None:
        return _clasificar_regex(pregunta, contexto_sesion)

    # Construir prompt con el contexto de la sesión
    prompt = _construir_prompt(pregunta, contexto_sesion)

    try:
        resp = _client_router.chat.completions.create(
            model=MODELO_ROUTER,
            messages=[
                {'role': 'system', 'content': SYSTEM_ENRUTADOR},
                {'role': 'user',   'content': prompt},
            ],
            temperature=0.0,
            max_tokens=300,
        )
        texto = resp.choices[0].message.content.strip()
        resultado = _parsear_respuesta(texto)
        print(f'  [enrutador] {resultado["ruta"]} (confianza: {resultado["confianza"]}) — {resultado["razon"][:80]}')
        return resultado

    except Exception as e:
        print(f'  [enrutador] Error LLM: {e} — usando regex fallback')
        return _clasificar_regex(pregunta, contexto_sesion)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _construir_prompt(pregunta: str, contexto: dict) -> str:
    historial = contexto.get('historial', [])
    dfs       = contexto.get('dataframes_activos', [])
    turno     = contexto.get('turno_actual', 0)

    partes = [f'Turno actual: {turno}']
    partes.append(f'Mensaje del usuario: "{pregunta}"')
    partes.append('')

    if historial:
        partes.append('--- Historial de la sesión (últimos turnos) ---')
        for t in historial:
            partes.append(
                f'Turno {t["turno"]}: [{t["ruta"]}] "{t["pregunta"]}" → {t["resumen"]}'
            )
        partes.append('')

    if dfs:
        partes.append('--- DataFrames en memoria ---')
        for df in dfs:
            cols = list(df['columnas'].keys())
            partes.append(
                f'{df["nombre"]}: "{df["descripcion"]}" | '
                f'{df["total_filas"]} filas | columnas: {cols}'
            )
        partes.append('')

    partes.append('Clasifica el mensaje del usuario. Responde solo con el JSON.')
    return '\n'.join(partes)


def _parsear_respuesta(texto: str) -> dict:
    """Extrae el JSON de la respuesta del LLM."""
    # Intentar parsear directamente
    try:
        data = json.loads(texto)
    except json.JSONDecodeError:
        # Buscar bloque JSON
        bloque = re.search(r'\{.*\}', texto, re.DOTALL)
        if not bloque:
            return _fallback_ruta('NUEVA_CONSULTA', 'No se pudo parsear respuesta del enrutador.')
        try:
            data = json.loads(bloque.group(0))
        except json.JSONDecodeError:
            return _fallback_ruta('NUEVA_CONSULTA', 'JSON malformado en respuesta del enrutador.')

    ruta = data.get('ruta', 'NUEVA_CONSULTA').upper().strip()
    if ruta not in RUTAS:
        ruta = 'NUEVA_CONSULTA'

    return {
        'ruta':                  ruta,
        'confianza':             data.get('confianza', 'media'),
        'razon':                 data.get('razon', ''),
        'df_relevante':          data.get('df_relevante'),
        'operacion_sugerida':    data.get('operacion_sugerida'),
        'parametros_sugeridos':  data.get('parametros_sugeridos'),
        'contexto_sql':          data.get('contexto_sql'),
    }


def _clasificar_regex(pregunta: str, contexto: dict) -> dict:
    """
    Fallback de clasificación por regex cuando el LLM no está disponible.
    Conservador: prefiere NUEVA_CONSULTA ante la duda.
    """
    p = pregunta.lower().strip()

    # Señales de SOBRE_DATOS
    sobre_datos = re.search(
        r'\b(cuanto|cuánto|porcentaje|representa|diferencia|compara|suma|total de|'
        r'cuál es el %|qué %|dividido|proporcion|proporción)\b',
        p,
    )
    # Señales de REFINAMIENTO
    refinamiento = re.search(
        r'\b(y en|solo de|filtra|ahora|lo mismo|ordena|pero|también|tambien|'
        r'desglos|muéstrame|muestrame|por tienda|por ciudad|por semana)\b',
        p,
    )
    # Señales de CONVERSACIONAL
    conversacional = re.search(
        r'\b(por qué|porque|qué significa|explicame|explícame|es normal|'
        r'gracias|genial|entiendo|tiene sentido)\b',
        p,
    )

    hay_dfs = bool(contexto.get('dataframes_activos'))

    if conversacional:
        ruta = 'CONVERSACIONAL'
    elif sobre_datos and hay_dfs:
        ruta = 'SOBRE_DATOS'
    elif refinamiento and contexto.get('historial'):
        ruta = 'REFINAMIENTO'
    else:
        ruta = 'NUEVA_CONSULTA'

    return _fallback_ruta(ruta, f'Clasificación por regex (LLM no disponible).')


def _fallback_ruta(ruta: str, razon: str) -> dict:
    return {
        'ruta':                  ruta,
        'confianza':             'baja',
        'razon':                 razon,
        'df_relevante':          None,
        'operacion_sugerida':    None,
        'parametros_sugeridos':  None,
        'contexto_sql':          None,
    }
