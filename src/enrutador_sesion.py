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
  "sub_preguntas": null,
  "necesita_busqueda_web": false,
  "relacion_tipo": "ninguna",
  "relacion_descripcion": null
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
  - Si el mensaje asigna un FORMATO de salida (excel, gráfico, tabla, exportar)
    a cada parte, CONSERVA esa palabra en la sub-pregunta reescrita que le
    corresponde — no la omitas al resumir. Ej: "genera dos excels, uno de las
    ventas por tienda en Antioquia y otro en Bogotá" → sub_preguntas: ["genera
    un excel de las ventas por tienda en Antioquia", "genera un excel de las
    ventas por tienda en Bogotá"] (NO "ventas por tienda en Antioquia" a secas
    — eso pierde la intención de exportar y el archivo nunca se genera).
  - Si detectas sub_preguntas, "ruta" sigue siendo NUEVA_CONSULTA o REFINAMIENTO
    según corresponda (sub_preguntas es independiente de la ruta).
  - NO uses sub_preguntas para una sola pregunta con múltiples cláusulas que SÍ
    caben en una tabla (ej: "ventas por tienda y por línea" puede ser ambiguo,
    pero "top 5 tiendas de Bogotá" con un solo filtro NO se separa).
  - Si solo hay una intención, "sub_preguntas" es null.

DETECCIÓN DE BÚSQUEDA WEB (necesita_busqueda_web):
  La base de datos SOLO tiene ventas internas de Creytex. Si el mensaje pide o
  implica información que NO puede existir ahí — tendencias de mercado,
  competencia, precios de otras marcas, noticias del sector, moda/temporadas
  externas — marca "necesita_busqueda_web": true. Es INDEPENDIENTE de "ruta":
  un mensaje puede pedir datos internos (NUEVA_CONSULTA/REFINAMIENTO) Y
  requerir información externa al mismo tiempo.
  Ejemplo: "dame las ventas de camisetas manga corta y compáralas con la
  competencia" → ruta: NUEVA_CONSULTA (las ventas SÍ están en la BD),
  necesita_busqueda_web: true (la competencia NO está en la BD).
  Si el mensaje no menciona ni implica nada externo al negocio interno,
  "necesita_busqueda_web" es false.

DETECCIÓN DE RELACIÓN ENTRE SUB-PREGUNTAS (relacion_tipo, relacion_descripcion):
  Solo aplica cuando "sub_preguntas" tiene 2 o más elementos (si sub_preguntas
  es null, relacion_tipo es SIEMPRE "ninguna"). El pipeline ejecuta cada
  sub-pregunta como una consulta SQL 100% independiente — no comparten
  contexto entre sí. Tu tarea es detectar si, ADEMÁS de estar en el mismo
  mensaje, hay una hipótesis real de que una sub-pregunta pueda explicar,
  compararse o depender de otra — para que el sistema pueda sintetizar esa
  conexión después de calcular los resultados.
  - "causal": el resultado de una sub-pregunta podría ser causa o explicación
    del resultado de otra. Ej: "ventas de la tienda X en agosto y su
    inventario en agosto" → la caída de ventas podría explicarse por el
    inventario bajo.
  - "comparativa": el usuario pide explícitamente comparar o contrastar los
    resultados entre sí. Ej: "ventas de camisetas en Bogotá vs en Medellín".
  - "secuencial": una sub-pregunta depende del resultado de otra para tener
    sentido completo, o hay un orden temporal/lógico entre ellas. Ej: "top 3
    tiendas del mes pasado y cómo les fue este mes".
  - "ninguna": son pedidos empaquetados en un mismo mensaje SIN relación real
    entre sí (el caso más común — el usuario solo aprovechó para pedir dos
    cosas a la vez). Ej: "ventas de camisetas en Bogotá y el top 5 de tiendas
    en Antioquia" → sin relación.
  - "relacion_descripcion": SOLO si relacion_tipo no es "ninguna", una frase
    breve (máx. 20 palabras) con la hipótesis concreta — quién podría explicar
    a quién y por qué. Si relacion_tipo es "ninguna", va null.
  IMPORTANTE: tú NO ves los datos reales, solo el texto de la pregunta — esto
  es una HIPÓTESIS a verificar después contra las cifras, no una conclusión.
  Ante la duda entre un tipo y "ninguna", prefiere "ninguna" — falsos
  positivos generan análisis forzados que no aportan nada.
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
            'sub_preguntas', 'necesita_busqueda_web',
            'relacion_tipo', 'relacion_descripcion',
        ],
        'properties': {
            'ruta':      {'type': 'string', 'enum': list(RUTAS)},
            'confianza': {'type': 'string', 'enum': ['alta', 'media', 'baja']},
            'razon':     {'type': 'string'},
            'df_relevante': {'type': ['string', 'null']},
            'necesita_busqueda_web': {'type': 'boolean'},
            'relacion_tipo': {
                'type': 'string',
                'enum': ['causal', 'comparativa', 'secuencial', 'ninguna'],
            },
            'relacion_descripcion': {'type': ['string', 'null']},
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


def _construir_schema_con_plantillas(ids_candidatas: list[str]) -> dict:
    """Copia de _JSON_SCHEMA_ENRUTADOR extendida con 3 campos de plantillas.
    Se construye por request, SOLO cuando hay candidatas (nunca a nivel de
    módulo) — así el schema base (y su costo) no cambia para la inmensa
    mayoría de turnos que no matchean ninguna plantilla.

    "plantilla_sugerida" restringe su enum a las candidatas YA filtradas de
    ESTE turno (máx. unas pocas, ver src/plantillas.py candidatas()), no a
    toda la biblioteca — evita que el modelo devuelva un id inventado sin
    reintroducir el problema que se quería evitar (un enum que crece con
    cada plantilla nueva y se manda en todas las llamadas)."""
    import copy
    schema = copy.deepcopy(_JSON_SCHEMA_ENRUTADOR)
    props = schema['schema']['properties']
    props['plantilla_sugerida'] = {
        'type': ['string', 'null'],
        'enum': list(ids_candidatas) + [None],
    }
    props['plantilla_parametros'] = {'type': ['object', 'null']}
    props['plantilla_confianza'] = {
        'type': 'string',
        'enum': ['alta', 'media', 'ninguna'],
    }
    schema['schema']['required'] += [
        'plantilla_sugerida', 'plantilla_parametros', 'plantilla_confianza',
    ]
    return schema


def clasificar(
    pregunta: str,
    contexto_sesion: dict,
    plantillas_candidatas: list | None = None,
) -> dict:
    """
    Clasifica el mensaje del usuario usando el LLM ligero.

    Args:
        pregunta: mensaje actual del usuario
        contexto_sesion: dict con 'historial' y 'dataframes_activos' del SessionStore
        plantillas_candidatas: salida de src/plantillas.py::candidatas(pregunta) ya
            calculada por server.py, o None/[] si no hubo match léxico — en ese
            caso la llamada es idéntica a como era antes de esta funcionalidad.

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
          'necesita_busqueda_web': bool,
          'relacion_tipo': str,           # 'causal' | 'comparativa' | 'secuencial' | 'ninguna'
          'relacion_descripcion': str,     # '' si relacion_tipo es 'ninguna'
          'plantilla_sugerida': str | None,     # id de plantilla, o None
          'plantilla_parametros': dict | None,  # valores extraídos del mensaje para esa plantilla
          'plantilla_confianza': str,           # 'alta' | 'media' | 'ninguna'
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
            'necesita_busqueda_web': False,
            'relacion_tipo':         'ninguna',
            'relacion_descripcion':  '',
            'plantilla_sugerida':    None,
            'plantilla_parametros':  None,
            'plantilla_confianza':   'ninguna',
        }

    # Si el cliente no está disponible → fallback a regex simple
    if _client_router is None:
        return _clasificar_regex(pregunta, contexto_sesion)

    # Construir prompt con el contexto de la sesión
    prompt = _construir_prompt(pregunta, contexto_sesion, plantillas_candidatas)

    # El schema extendido (campos de plantillas) solo se construye — y solo
    # se manda — cuando hay candidatas reales para este turno. Sin
    # candidatas, la llamada es byte-por-byte la de siempre.
    ids_candidatas = [c['id'] for c in plantillas_candidatas] if plantillas_candidatas else []
    json_schema = _construir_schema_con_plantillas(ids_candidatas) if ids_candidatas else _JSON_SCHEMA_ENRUTADOR

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
                'json_schema': json_schema,
            },
        )
        texto = resp.choices[0].message.content.strip()
        resultado = _parsear_respuesta(texto, ids_candidatas)
        print(f'  [enrutador] {resultado["ruta"]} (confianza: {resultado["confianza"]}) — {resultado["razon"]}')
        if resultado['necesita_busqueda_web']:
            print(f'  [enrutador] necesita_busqueda_web=True')
        # Se imprime siempre que aplica (2+ sub_preguntas), incluyendo "ninguna"
        # — trazabilidad de que la clasificación corrió, no solo cuando detecta
        # algo (a diferencia de necesita_busqueda_web, que solo importa en True).
        if resultado['sub_preguntas'] and len(resultado['sub_preguntas']) >= 2:
            desc = f' — {resultado["relacion_descripcion"]}' if resultado['relacion_descripcion'] else ''
            print(f'  [enrutador] relacion_tipo={resultado["relacion_tipo"]}{desc}')
        if resultado['plantilla_sugerida']:
            print(f'  [enrutador] plantilla_sugerida={resultado["plantilla_sugerida"]} (confianza: {resultado["plantilla_confianza"]})')
        return resultado

    except Exception as e:
        print(f'  [enrutador] Error LLM: {e} — usando regex fallback')
        return _clasificar_regex(pregunta, contexto_sesion)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _construir_prompt(pregunta: str, contexto: dict, plantillas_candidatas: list | None = None) -> str:
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

    # Bloque de plantillas SQL pre-armadas — solo aparece cuando un prefiltro
    # léxico local (ver src/plantillas.py) ya encontró candidatas para esta
    # pregunta puntual, así que en la mayoría de los turnos este bloque no
    # existe y el prompt es idéntico al de siempre (0 tokens extra). Es
    # autocontenido (explica los 3 campos nuevos aquí mismo) para no tener
    # que agregar nada a SYSTEM_ENRUTADOR, que se manda en TODAS las
    # llamadas — ver nota de presupuesto en orquestador.py/_reglas_gen.
    if plantillas_candidatas:
        partes.append('--- Plantillas SQL pre-armadas y ya validadas para esta pregunta ---')
        partes.append(
            'Si alguna de estas plantillas responde EXACTAMENTE lo que pide el '
            'usuario, indícalo con "plantilla_sugerida" (el id exacto) y '
            '"plantilla_parametros" (los valores que puedas extraer del mensaje '
            'para sus parámetros — nombres de tienda, fechas, N, etc.; usa null '
            'para los que no se mencionan, la plantilla trae sus propios '
            'defaults). "plantilla_confianza": "alta" SOLO si la plantilla '
            'cubre la pregunta completa sin adaptarla; "media" si aplica en '
            'espíritu pero con matices (otra dimensión, otro filtro no '
            'soportado); "ninguna" si ninguna aplica realmente — ante la duda, '
            'preferir "media" y nunca forzar "alta".'
        )
        for c in plantillas_candidatas:
            params = ', '.join(c.get('parametros', {}).keys())
            partes.append(f'  - id="{c["id"]}": {c["descripcion"]} | parámetros: {params}')
        partes.append('')

    partes.append('Clasifica el mensaje del usuario. Responde solo con el JSON.')
    return '\n'.join(partes)


def _parsear_respuesta(texto: str, ids_candidatas: list[str] | None = None) -> dict:
    """Extrae el JSON de la respuesta del LLM.

    ids_candidatas: ids de plantillas realmente ofrecidas en este turno (ver
    clasificar). Si el modelo devuelve un plantilla_sugerida que no está en
    esta lista (alucinado, o quedó de un schema viejo), se descarta a None —
    nunca se confía en un id que Python no ofreció explícitamente."""
    ids_candidatas = ids_candidatas or []
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

    sub_preguntas = data.get('sub_preguntas') or None
    relacion_tipo = (data.get('relacion_tipo') or 'ninguna').strip().lower()
    if relacion_tipo not in ('causal', 'comparativa', 'secuencial', 'ninguna'):
        relacion_tipo = 'ninguna'
    # Solo tiene sentido con 2+ sub_preguntas — si el modelo lo marcó igual
    # sobre una sola pregunta (o ninguna), se descarta como ruido.
    if not sub_preguntas or len(sub_preguntas) < 2:
        relacion_tipo = 'ninguna'

    plantilla_sugerida = data.get('plantilla_sugerida')
    if plantilla_sugerida not in ids_candidatas:
        plantilla_sugerida = None
    plantilla_confianza = (data.get('plantilla_confianza') or 'ninguna').strip().lower()
    if plantilla_confianza not in ('alta', 'media', 'ninguna') or not plantilla_sugerida:
        plantilla_confianza = 'ninguna'

    return {
        'ruta':                  ruta,
        'confianza':             data.get('confianza', 'media'),
        'razon':                 data.get('razon', ''),
        'df_relevante':          data.get('df_relevante'),
        'operacion_sugerida':    data.get('operacion_sugerida'),
        'parametros_sugeridos':  data.get('parametros_sugeridos'),
        'contexto_sql':          data.get('contexto_sql'),
        'sub_preguntas':         sub_preguntas,
        'necesita_busqueda_web': bool(data.get('necesita_busqueda_web', False)),
        'relacion_tipo':         relacion_tipo,
        'relacion_descripcion':  (data.get('relacion_descripcion') or '') if relacion_tipo != 'ninguna' else '',
        'plantilla_sugerida':    plantilla_sugerida,
        'plantilla_parametros':  data.get('plantilla_parametros') if plantilla_sugerida else None,
        'plantilla_confianza':   plantilla_confianza,
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
    # necesita_busqueda_web queda en False: estos caminos (regex/parseo fallido)
    # no tienen LLM disponible para emitir la señal; server.py sigue
    # detectando intención web por su propia regex (es_intencion_busqueda_web),
    # así que la capacidad no se pierde, solo la señal extra del enrutador.
    return {
        'ruta':                  ruta,
        'confianza':             'baja',
        'razon':                 razon,
        'df_relevante':          None,
        'operacion_sugerida':    None,
        'parametros_sugeridos':  None,
        'contexto_sql':          None,
        'sub_preguntas':         None,
        'necesita_busqueda_web': False,
        'relacion_tipo':         'ninguna',
        'relacion_descripcion':  '',
        'plantilla_sugerida':    None,
        'plantilla_parametros':  None,
        'plantilla_confianza':   'ninguna',
    }
