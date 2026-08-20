"""
src/enrutador_sesion.py
Clasifica cada mensaje del usuario en una de 4 rutas,
considerando el historial de sesión y los DataFrames activos.

Rutas:
  NUEVA_CONSULTA   — pregunta independiente, requiere SQL a BD
  REFINAMIENTO     — variación de la consulta anterior, requiere SQL con contexto
  SOBRE_DATOS      — pregunta sobre resultados ya en memoria, puede responder con Pandas
  CONVERSACIONAL   — respuesta directa sin BD ni Pandas

Usa openai/gpt-oss-20b vía Groq (mismo cliente que el clasificador de analista).
"""
import json
import os
import re
from dotenv import load_dotenv

load_dotenv()

try:
    from tools.tool_pandas import catalogo_para_llm as _catalogo_tool_pandas
    from tools.tool_pandas import OPERACIONES_DISPONIBLES as _OPERACIONES
except Exception:
    _catalogo_tool_pandas = None
    _OPERACIONES = {}

# ---------------------------------------------------------------------------
# Cliente Groq para clasificación (modelo ligero, separado del LLM principal)
# ---------------------------------------------------------------------------
try:
    from groq import Groq as _GroqClass
    _client_router = _GroqClass(api_key=os.getenv('GROQ_API_KEY'))
    MODELO_ROUTER   = os.getenv('GROQ_MODEL_INFERENCE', 'openai/gpt-oss-20b')
except Exception:
    _client_router = None
    MODELO_ROUTER   = None

# ---------------------------------------------------------------------------
# Rutas posibles
# ---------------------------------------------------------------------------
RUTAS = ('NUEVA_CONSULTA', 'REFINAMIENTO', 'SOBRE_DATOS', 'CONVERSACIONAL')

# ---------------------------------------------------------------------------
# Saludo / chit-chat sin intención de datos — se detecta por regex, sin
# gastar una llamada al LLM. Solo se usa como primer mensaje de la sesión
# (ver "Sin historial" en clasificar()): antes, cualquier primer mensaje se
# forzaba a NUEVA_CONSULTA sin mirar el contenido, y un saludo terminaba
# forzando al generador de SQL a inventar una consulta sobre "hola", lo que
# rompía el pipeline (el validador lo rechazaba y agotaba los reintentos).
# Anclado a todo el mensaje (con puntuación/espacios al final tolerados)
# para no atrapar mensajes reales que solo empiezan con un saludo
# ("hola, dame las ventas de marzo" NO matchea).
# ---------------------------------------------------------------------------
PATRON_SALUDO = re.compile(
    r'^\s*('
    r'hola+|buenas|buenos?\s+d[ií]as?|buenas\s+tardes|buenas\s+noches|'
    r'hey|hi|hello|qu[eé]\s+tal|c[oó]mo\s+(est[aá]s|vas|andas)|saludos|'
    r'oye|holi|'
    r'gracias|muchas\s+gracias|listo|vale|perfecto|entendido|'
    r'ad[ií]os|chao|hasta\s+luego|nos\s+vemos|'
    r'qui[eé]n\s+eres|qu[eé]\s+(eres|puedes\s+hacer)|c[oó]mo\s+funcionas'
    r')[\s!.,¡¿?]*$',
    re.IGNORECASE,
)

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
  "parametros_sugeridos": null,
  "sub_preguntas": null
}

Si la ruta es SOBRE_DATOS, incluir también:
  "df_relevante": "df_1"  (nombre del df que contiene los datos necesarios)
  "operacion_sugerida": el nombre EXACTO de una operación de la lista de abajo — nunca inventes un nombre
  "parametros_sugeridos": los parámetros de esa operación, con los nombres de columna reales del df_relevante

Si la ruta es REFINAMIENTO, incluir:
  "contexto_sql": breve descripción de qué ajuste necesita el SQL anterior

DETECCIÓN DE MÚLTIPLES CONSULTAS (sub_preguntas):
  El pipeline SQL genera UNA sola tabla por consulta. Si el mensaje pide, en un
  mismo turno, dos o más resultados que NO caben en una sola tabla porque tienen
  dimensiones, agrupaciones o filtros distintos entre sí (ej: "dame una tabla de
  las referencias con más ventas y otra tabla de las tiendas con más ventas en
  Antioquia"), NO intentes resolverlo en una sola consulta ni elijas solo una
  parte: descompón el mensaje en sub-preguntas independientes.
  - "sub_preguntas": lista de strings, cada uno una pregunta autocontenida y
    completa (reescrita para poder enviarse sola al generador SQL, sin depender
    de las demás) — incluye en cada una el filtro/contexto que le corresponda
    específicamente (ej: el filtro "en Antioquia" va SOLO en la sub-pregunta de
    tiendas, no en la de referencias).
  - Si detectas sub_preguntas, "ruta" sigue siendo NUEVA_CONSULTA o REFINAMIENTO
    según corresponda (sub_preguntas es independiente de la ruta).
  - NO uses sub_preguntas para una sola pregunta con múltiples cláusulas que SÍ
    caben en una tabla (ej: "ventas por tienda y por línea" puede ser ambiguo,
    pero "top 5 tiendas de Bogotá" con un solo filtro NO se separa).
  - Si solo hay una intención, "sub_preguntas" es null.
"""

if _catalogo_tool_pandas is not None:
    SYSTEM_ENRUTADOR += '\n\n' + _catalogo_tool_pandas()

# ---------------------------------------------------------------------------
# JSON Schema (structured outputs, modo strict) — le exige a Groq que la
# salida sea JSON válido con esta forma exacta, en vez de confiar solo en la
# instrucción del prompt. Con esto el bloque try/except de _parsear_respuesta
# queda como red de seguridad, no como el camino normal: antes, con
# gpt-oss-20b (modelo de razonamiento, canal de "thinking" en inglés) las
# preguntas de refinamiento —las más ambiguas de clasificar— a veces gastaban
# el presupuesto de max_tokens en el razonamiento interno y truncaban el JSON
# final, cayendo en "no se pudo parsear" y perdiendo el contexto del turno.
# Todos los campos van "required" (constraint de modo strict); los opcionales
# se marcan nullable con ["tipo", "null"] en vez de omitirlos.
# ---------------------------------------------------------------------------
_PARAM_STRING = {'type': ['string', 'null']}
_PARAM_STRING_O_NUM = {'type': ['string', 'number', 'null']}

_JSON_SCHEMA_ENRUTADOR = {
    'name': 'clasificacion_enrutador',
    # best-effort (no strict): con 'strict': True, Groq RECHAZA la respuesta
    # completa (error 400, sin reintento) si el modelo omite una clave nullable
    # en 'parametros_sugeridos' en vez de mandarla en null — se vio en pruebas
    # con SOBRE_DATOS y forzaba el fallback a regex, perdiendo la clasificación
    # real del LLM. best-effort sigue guiando la forma del JSON sin rechazar.
    'strict': False,
    'schema': {
        'type': 'object',
        'additionalProperties': False,
        'required': [
            'ruta', 'confianza', 'razon', 'df_relevante',
            'operacion_sugerida', 'parametros_sugeridos', 'contexto_sql',
            'sub_preguntas',
        ],
        'properties': {
            'ruta':      {'type': 'string', 'enum': list(RUTAS)},
            'confianza': {'type': 'string', 'enum': ['alta', 'media', 'baja']},
            'razon':     {'type': 'string'},
            'df_relevante': {'type': ['string', 'null']},
            'sub_preguntas': {
                'type': ['array', 'null'],
                'items': {'type': 'string'},
            },
            'operacion_sugerida': {
                'type': ['string', 'null'],
                'enum': list(_OPERACIONES.keys()) + [None],
            },
            'contexto_sql': {'type': ['string', 'null']},
            'parametros_sugeridos': {
                'type': ['object', 'null'],
                'additionalProperties': False,
                'required': [
                    'col_valor', 'col_grupo', 'col_filtro', 'valor', 'valores',
                    'n', 'valor_nuevo', 'valor_anterior', 'col_peso',
                    'valor_a', 'valor_b', 'col_a', 'col_b',
                ],
                'properties': {
                    'col_valor':      _PARAM_STRING,
                    'col_grupo':      _PARAM_STRING,
                    'col_filtro':     _PARAM_STRING,
                    'valor':          _PARAM_STRING_O_NUM,
                    'valores':        {
                        'type': ['array', 'null'],
                        'items': {'type': ['string', 'number']},
                    },
                    'n':              {'type': ['integer', 'null']},
                    'valor_nuevo':    {'type': ['number', 'null']},
                    'valor_anterior': {'type': ['number', 'null']},
                    'col_peso':       _PARAM_STRING,
                    'valor_a':        _PARAM_STRING_O_NUM,
                    'valor_b':        _PARAM_STRING_O_NUM,
                    'col_a':          _PARAM_STRING,
                    'col_b':          _PARAM_STRING,
                },
            },
        },
    },
}


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
          'sub_preguntas': list[str] | None,
        }
    """
    hay_historial = bool(contexto_sesion.get('historial'))

    # Sin historial y es un saludo/chit-chat (ver PATRON_SALUDO arriba) → va
    # directo a CONVERSACIONAL sin llamar al LLM. Un saludo real jamás trae
    # sub-preguntas (el patrón exige que TODO el mensaje sea el saludo, sin
    # texto adicional), así que este atajo es seguro.
    #
    # Nota: antes, cualquier OTRO primer mensaje (no saludo) también se
    # devolvía como NUEVA_CONSULTA fijo sin llamar al LLM — optimización que
    # dejó de ser segura al agregar sub_preguntas: el primer mensaje de una
    # sesión es justo donde más aparecen preguntas compuestas ("dame una
    # tabla de X y otra de Y"), y el atajo las dejaba pasar sin detectar.
    # Ahora ese caso cae al flujo normal de abajo, que sí llama al LLM.
    if not hay_historial and PATRON_SALUDO.match(pregunta.strip()):
        return {
            'ruta':                  'CONVERSACIONAL',
            'confianza':             'alta',
            'razon':                 'Saludo o mensaje sin intención de datos (regex, primer mensaje).',
            'df_relevante':          None,
            'operacion_sugerida':    None,
            'parametros_sugeridos':  None,
            'contexto_sql':          None,
            'sub_preguntas':         None,
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
            max_tokens=1200,
            reasoning_effort='low',
            response_format={
                'type': 'json_schema',
                'json_schema': _JSON_SCHEMA_ENRUTADOR,
            },
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
        'sub_preguntas':         data.get('sub_preguntas') or None,
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
        'sub_preguntas':         None,
    }
