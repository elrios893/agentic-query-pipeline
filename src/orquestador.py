#!/usr/bin/env python3
"""
Orquestador: pipeline de consulta de datos en lenguaje natural.
Soporta multiples proveedores LLM configurados via .env (LLM_PROVIDER).
"""
import os
import re
import json
import subprocess
import sys
import unicodedata
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# Forzar stdout/stderr en UTF-8 independientemente del locale de Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

load_dotenv()

# ---------------------------------------------------------------------------
# Provider abstraction — configurado via .env: LLM_PROVIDER=groq|cerebras|gemini
# ---------------------------------------------------------------------------
PROVIDER = os.getenv('LLM_PROVIDER', 'groq').lower()

if PROVIDER == 'groq':
    from groq import Groq
    _client = Groq(api_key=os.getenv('GROQ_API_KEY'))
    MODELO  = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')

elif PROVIDER == 'cerebras':
    from openai import OpenAI
    _client = OpenAI(
        api_key=os.getenv('CEREBRAS_KEY'),
        base_url='https://api.cerebras.ai/v1',
    )
    MODELO  = os.getenv('CEREBRAS_MODEL', 'gemma-4-31b')

elif PROVIDER == 'gemini':
    from google import genai
    _client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
    MODELO  = os.getenv('GEMINI_MODEL', 'gemini-2.0-flash')

else:
    raise ValueError(f'LLM_PROVIDER desconocido: {PROVIDER}. Usa: groq, cerebras, gemini')

BASE_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = BASE_DIR / 'agents'
TOOLS_DIR = BASE_DIR / 'tools'
SKILLS_DIR = BASE_DIR / 'skills'
REPORTS_DIR = BASE_DIR / 'reports'
MAX_ITERACIONES = 3

# ---------------------------------------------------------------------------
# Reglas adicionales inyectadas en el system prompt del generador y validador.
# Definidas una sola vez aqui para evitar duplicacion.
# ---------------------------------------------------------------------------
def _reglas_gen() -> str:
    from datetime import datetime
    anio = datetime.now().year
    return f"""

### Contexto temporal
- Hoy es {datetime.now().strftime('%d/%m/%Y')}.
- La tabla ventas SOLO contiene datos del año {anio}.
- Cuando el usuario mencione un dia o mes sin especificar año, SIEMPRE usa {anio}.
- NUNCA uses ningun otro año.

### Reglas adicionales obligatorias
1. Siempre usa comillas dobles en TODOS los nombres de columna.
2. Usa TRIM() en columnas de texto: TRIM("SIGNO"), TRIM("DEPARTAMENTO"), TRIM("DESC_MOVIMIENTO").
3. "SIGNO" puede ser null, '-' con espacios, o '+' con espacios.
4. FECHA_MVTO es TEXT en formato D/M/YYYY sin ceros (ej: 1/7/2026). USA TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY'). NUNCA uses ::DATE ni TO_DATE con 'DD/MM/YYYY'.
5. Para valor de ventas usa "CANTIDAD" * "PVP". NUNCA uses "PVP LISTA" para tiendas individuales.
6. "PVP LISTA" SOLO se usa si la consulta es sobre clientes MACRO (cadenas), no tiendas.
7. Si un alias tiene mayusculas (ej: "Ventas"), ponle comillas dobles en ORDER BY y GROUP BY: ORDER BY "Ventas" DESC.
8. Textos siempre en mayusculas: DEPARTAMENTO, CIUDAD, DESC_DEPENDENCIA, RAZON_SOCIAL, CLIMA, ZONA, ZONA_EX, DESC_ITEM deben usar UPPER(TRIM(...)) en SELECT.
"""

def _reglas_val() -> str:
    from datetime import datetime
    anio = datetime.now().year
    return f"""

### Reglas adicionales obligatorias
1. Verifica que TODOS los nombres de columna esten entre comillas dobles.
2. Verifica uso de TRIM() en filtros de texto.
3. No aceptes LIMIT en COUNT(*) o agregaciones simples.
4. Rechaza TO_DATE con formato 'DD/MM/YYYY' o ::DATE. Solo acepta TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY').
5. Revisa que use "CANTIDAD" * "PVP" para valor de ventas, no "PVP LISTA" (a menos que sea consulta macro).
6. Revisa que los alias con mayusculas usen comillas dobles en ORDER BY/GROUP BY.
7. Revisa que DEPARTAMENTO, CIUDAD, DESC_DEPENDENCIA, RAZON_SOCIAL, CLIMA, ZONA, ZONA_EX, DESC_ITEM usen UPPER(TRIM(...)). Si aparecen sin UPPER, RECHAZAR.
8. Verifica que el año en los literales de fecha sea {anio}. Si ves 2024, 2025 u otro año en una fecha literal, RECHAZAR.
"""

PATRONES_INFORME = re.compile(
    r'\b(informe|reporte|report|documento|word|docx|'
    r'resumen.*mensual|balance.*mes|'
    r'prepara.*informe|genera.*informe|crea.*documento)\b',
    re.IGNORECASE
)

PATRONES_GRAFICO = re.compile(
    r'(grafic\w*|grafica\w*|chart|plotea\w*|visualiza\w*|'
    r'\bbarras\b|\btorta\b|\blinea\b|\btendencia\b|\bdistribucion\b)',
    re.IGNORECASE
)

def es_intencion_informe(texto: str) -> bool:
    return bool(PATRONES_INFORME.search(texto))

def es_intencion_grafico(texto: str) -> bool:
    return bool(PATRONES_GRAFICO.search(texto))

def leer_instrucciones(archivo: str) -> str:
    ruta = AGENTS_DIR / archivo
    return ruta.read_text(encoding='utf-8')

def leer_skill(nombre: str) -> str:
    ruta = SKILLS_DIR / nombre / 'SKILL.md'
    return ruta.read_text(encoding='utf-8')

def leer_skill_sin_yaml(nombre: str) -> str:
    """Lee un skill eliminando el bloque YAML front matter para ahorrar tokens."""
    contenido = leer_skill(nombre)
    if contenido.startswith('---'):
        partes = contenido.split('---', 2)
        if len(partes) >= 3:
            return partes[2].strip()
    return contenido

def leer_skill_bloques_resumen(nombre: str) -> str:
    """Extrae solo los encabezados de bloque del skill de informes para el planning.
    Ahorra ~2,500 tokens en la llamada de planificacion."""
    contenido = leer_skill_sin_yaml(nombre)
    lineas = contenido.split('\n')
    resumen = []
    capturando = False
    for linea in lineas:
        # Incluir reglas de negocio y encabezados de bloque, omitir el detalle
        if linea.startswith('## Reglas de negocio') or linea.startswith('## Como elegir'):
            capturando = True
        if linea.startswith('### BLOQUE'):
            capturando = True
        if capturando:
            resumen.append(linea)
            # Despues del encabezado del bloque, tomar solo las 2 primeras lineas de contenido
            if linea.startswith('### BLOQUE') and len(resumen) > 1:
                capturando = False  # reset para tomar solo el header
    # Estrategia mas simple y robusta: extraer bloques por patron
    bloques = []
    patron = re.compile(r'(### BLOQUE [A-Z] —.*?)(?=### BLOQUE [A-Z] —|\Z)', re.DOTALL)
    for m in patron.finditer(contenido):
        bloque_texto = m.group(1)
        lineas_bloque = [l for l in bloque_texto.strip().split('\n') if l.strip()]
        # Header + "Cuando usar" solamente
        header = lineas_bloque[0] if lineas_bloque else ''
        cuando = next((l for l in lineas_bloque if 'Cuando usar' in l or l.startswith('**Cuando')), '')
        desc = next((l for l in lineas_bloque[1:4] if l.strip() and not l.startswith('**')), '')
        bloques.append(f"{header}\n{cuando}\n{desc}".strip())
    reglas = []
    in_reglas = False
    for linea in lineas:
        if '## Reglas de negocio' in linea:
            in_reglas = True
        if in_reglas:
            reglas.append(linea)
        if in_reglas and linea.startswith('## ') and '## Reglas de negocio' not in linea:
            break
    return '\n'.join(reglas) + '\n\n## Bloques disponibles\n\n' + '\n\n'.join(bloques)

def limpiar_texto(texto: str) -> str:
    return ''.join(c for c in texto if unicodedata.category(c) != 'So').strip()

def llamar_llm(system_prompt: str, user_prompt: str, temperatura: float = 0.1) -> str:
    import time
    max_reintentos = 4
    espera = 10  # segundos base entre reintentos
    for intento in range(max_reintentos):
        try:
            if PROVIDER == 'gemini':
                response = _client.models.generate_content(
                    model=MODELO,
                    contents=user_prompt,
                    config={
                        'system_instruction': system_prompt,
                        'temperature': temperatura,
                    },
                )
                return limpiar_texto(response.text.strip())
            else:
                respuesta = _client.chat.completions.create(
                    model=MODELO,
                    messages=[
                        {'role': 'system', 'content': system_prompt},
                        {'role': 'user', 'content': user_prompt},
                    ],
                    temperature=temperatura,
                )
                return limpiar_texto(respuesta.choices[0].message.content.strip())
        except Exception as e:
            msg = str(e).lower()
            es_rate_limit = any(x in msg for x in ('rate limit', '429', 'too many', 'timeout', 'timed out', 'connection'))
            if es_rate_limit and intento < max_reintentos - 1:
                pausa = espera * (2 ** intento)  # backoff exponencial: 10s, 20s, 40s
                print(f'  [LLM] Rate limit / timeout (intento {intento+1}). Reintentando en {pausa}s...')
                time.sleep(pausa)
            else:
                raise

def extraer_sql(texto: str) -> str:
    bloques = re.findall(r'```sql\s*(.*?)\s*```', texto, re.DOTALL | re.IGNORECASE)
    if bloques:
        sql = bloques[0].strip()
    else:
        lineas = texto.strip().split('\n')
        sql_lines = [l for l in lineas if l.strip().upper().startswith(('SELECT', 'WITH', 'EXPLAIN'))]
        if sql_lines:
            sql = '\n'.join(sql_lines)
        else:
            sql = texto.strip()
    if not sql.endswith(';'):
        sql += ';'
    return sql

def ejecutar_consulta(sql: str, limite: int = 1000) -> dict:
    script = TOOLS_DIR / 'consultar_db.py'
    env = os.environ.copy()
    env['SQL_QUERY'] = sql
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        resultado = subprocess.run(
            ['python', str(script), str(limite)],
            capture_output=True, text=True, env=env, timeout=120, encoding='utf-8'
        )
        return json.loads(resultado.stdout)
    except Exception as e:
        return {'success': False, 'error': str(e)}


def generar_sql_y_validar(pregunta: str, system_gen: str, system_val: str) -> str:
    print(f'[{MODELO}] Generando consulta...')
    consulta_generada = llamar_llm(system_gen, pregunta)
    consulta_limpia = extraer_sql(consulta_generada)
    print(f'Consulta generada:\n{consulta_limpia}\n')

    for intento in range(MAX_ITERACIONES):
        print(f'[{MODELO}] Validando (intento {intento + 1})...')
        validacion = llamar_llm(system_val, consulta_limpia, temperatura=0.0)

        if 'VALIDA' in validacion.upper():
            print('Validacion: APROBADA\n')
            return consulta_limpia

        print('Validacion: RECHAZADA')
        print(f'Feedback: {validacion}\n')
        if intento < MAX_ITERACIONES - 1:
            feedback_msg = f"""La consulta anterior fue rechazada por el validador con estos errores:

{validacion}

Por favor, genera una nueva version corregida de la consulta SQL.

Pregunta original del usuario: {pregunta}"""
            consulta_generada = llamar_llm(system_gen, feedback_msg, temperatura=0.2)
            consulta_limpia = extraer_sql(consulta_generada)
            print(f'Nueva consulta:\n{consulta_limpia}\n')
        else:
            print('Se agotaron los intentos de validacion.')
            sys.exit(1)

def generar_graficos_informe(resultados, pregunta, plan, timestamp):
    skill_graficos = leer_skill_sin_yaml('graficos_ventas')

    # Construir resumen de columnas disponibles por consulta
    resumen_columnas = {}
    for nombre_consulta, res in resultados.items():
        cols = res.get('columns', [])
        filas = res.get('rows', [])
        if cols and filas:
            resumen_columnas[nombre_consulta] = {
                'filas': len(filas),
                'columnas': cols,
            }

    prompt = f"""Resumen de datos disponibles (columnas exactas):
{json.dumps(resumen_columnas, ensure_ascii=False, indent=2)}

Pregunta del usuario: "{pregunta}"
Bloques seleccionados: {plan.get('bloques', [])}

Decide que graficos generar (maximo 4). Responde SOLO con un JSON valido.

Formato:
[
  {{
    "nombre": "identificador_unico",
    "tipo": "barras_horizontales",
    "titulo": "Titulo del grafico",
    "etiqueta_x": "Departamento",
    "etiqueta_y": "Unidades Vendidas",
    "formato_y": "unidades",
    "consulta_origen": "nombre_exacto_de_la_consulta",
    "columna_x": "DEPARTAMENTO",
    "columna_y": "TOTAL_UNIDADES",
    "columna_serie": null,
    "bloque_destino": "D"
  }}
]

- "consulta_origen" debe coincidir con una clave del resumen de datos.
- "columna_x" y "columna_y" deben ser nombres exactos de columna visibles en "columnas".
- "columna_serie": nombre de columna para series (barras_agrupadas) o null.
- "formato_y": "moneda" | "unidades" | "porcentaje".
- Si no hay graficos que valgan la pena, responde: []
"""

    print(f'  Enviando prompt con {len(resumen_columnas)} consultas disponibles...')
    respuesta = llamar_llm(skill_graficos, prompt, temperatura=0.2)
    print(f'  Respuesta del LLM (primeros 300 chars): {respuesta[:300]}')

    bloque_json = re.search(r'\[.*\]', respuesta, re.DOTALL)
    if not bloque_json:
        print('  No se pudo parsear JSON de graficos.')
        return {}

    try:
        specs = json.loads(bloque_json.group(0))
    except json.JSONDecodeError:
        print('  Error al decodificar JSON de graficos.')
        return {}

    if not isinstance(specs, list) or not specs:
        print('  No se generaran graficos (lista vacia).')
        return {}

    import sys as _sys
    _sys.path.insert(0, str(BASE_DIR))
    from tools.generar_grafico import generar_grafico

    charts_dir = BASE_DIR / 'reports' / 'charts'
    imagenes = {}
    for spec in specs[:4]:
        nombre = spec.get('nombre', 'grafico')
        tipo = spec.get('tipo', 'barras_horizontales')
        titulo = spec.get('titulo', '')
        etiqueta_x = spec.get('etiqueta_x', '')
        etiqueta_y = spec.get('etiqueta_y', '')
        formato_y = spec.get('formato_y', 'unidades')
        consulta_origen = spec.get('consulta_origen', '')
        col_x = spec.get('columna_x', '')
        col_y = spec.get('columna_y', '')
        col_serie = spec.get('columna_serie')
        bloque_destino = spec.get('bloque_destino', '')

        if consulta_origen not in resultados:
            print(f'  Saltando "{nombre}": consulta "{consulta_origen}" no encontrada.')
            continue

        data_cols = resultados[consulta_origen].get('columns', [])
        data_rows = resultados[consulta_origen].get('rows', [])
        if not data_cols or not data_rows:
            print(f'  Saltando "{nombre}": datos vacios.')
            continue

        # Buscar columna_x y columna_y ignorando mayusculas/minusculas
        col_x_real = _match_col(col_x, data_cols)
        col_y_real = _match_col(col_y, data_cols)

        datos_graf = []
        for row in data_rows:
            row_dict = dict(zip(data_cols, row))
            item = {
                'x': str(row_dict.get(col_x_real or col_x, '')),
                'y': float(row_dict.get(col_y_real or col_y, 0) or 0),
            }
            if col_serie:
                col_s_real = _match_col(col_serie, data_cols)
                val_serie = row_dict.get(col_s_real or col_serie)
                if val_serie:
                    item['serie'] = str(val_serie)
            datos_graf.append(item)

        if not datos_graf:
            continue

        print(f'  Generando: {titulo} ({len(datos_graf)} pts)...')
        r = generar_grafico(
            datos=datos_graf,
            tipo=tipo,
            titulo=titulo,
            etiqueta_x=etiqueta_x,
            etiqueta_y=etiqueta_y,
            formato_y=formato_y,
            output_path=str(charts_dir),
            timestamp=timestamp,
        )

        if not r['error']:
            # Ruta relativa al proyecto para que funcione en markdown
            ruta_rel = os.path.relpath(r['path'], BASE_DIR)
            img_md = f"![{titulo}]({ruta_rel})"
            imagenes.setdefault(bloque_destino, []).append(img_md)
            print(f'    OK: {ruta_rel}')
        else:
            print(f'    Error: {r["error"]}')

    return imagenes


def _match_col(col_name, real_cols):
    """Busca columna en real_cols ignorando mayusculas/minusculas y guiones bajos."""
    if not col_name:
        return None
    col_lower = col_name.lower().replace('_', '').replace(' ', '')
    for rc in real_cols:
        if rc.lower().replace('_', '').replace(' ', '') == col_lower:
            return rc
    return None


def generar_graficos_consulta(resultado: dict, pregunta: str, timestamp: str) -> list:
    """
    Genera graficos para una consulta simple (no informe).
    Retorna lista de strings markdown de imagenes generadas.
    """
    cols = resultado.get('columns', [])
    rows = resultado.get('rows', [])
    if not cols or not rows or len(rows) < 2:
        return []

    skill_graficos = leer_skill_sin_yaml('graficos_ventas')

    resumen = {
        'consulta': {
            'filas': len(rows),
            'columnas': cols,
        }
    }

    prompt = f"""Resumen de datos disponibles (columnas exactas):
{json.dumps(resumen, ensure_ascii=False, indent=2)}

Pregunta del usuario: "{pregunta}"

Decide si tiene sentido generar un grafico con estos datos.
- Si los datos tienen al menos 2 filas con valores numericos y una columna categorica
  que pueda servir como eje X, elige el tipo de grafico apropiado.
- Si no tiene sentido graficar (datos muy pocos, solo una fila, todo texto),
  responde: []
- Si tiene sentido, genera hasta 1 grafico.

Responde SOLO con un JSON valido con este formato:
[
  {{
    "nombre": "identificador_unico",
    "tipo": "barras_horizontales",
    "titulo": "Titulo del grafico",
    "etiqueta_x": "Departamento",
    "etiqueta_y": "Unidades Vendidas",
    "formato_y": "unidades",
    "consulta_origen": "consulta",
    "columna_x": "columna_categorica",
    "columna_y": "columna_numerica",
    "columna_serie": null
  }}
]

- "columna_x" y "columna_y" deben ser nombres exactos de columna visibles en "columnas".
- "formato_y": "moneda" | "unidades" | "porcentaje".
- Si no hay graficos que valgan la pena, responde: []
"""

    print(f'  Evaluando grafico para consulta...')
    respuesta = llamar_llm(skill_graficos, prompt, temperatura=0.2)

    bloque_json = re.search(r'\[.*\]', respuesta, re.DOTALL)
    if not bloque_json:
        return []

    try:
        specs = json.loads(bloque_json.group(0))
    except json.JSONDecodeError:
        return []

    if not isinstance(specs, list) or not specs:
        return []

    import sys as _sys
    _sys.path.insert(0, str(BASE_DIR))
    from tools.generar_grafico import generar_grafico

    charts_dir = BASE_DIR / 'reports' / 'charts'
    imagenes_md = []
    for spec in specs[:1]:
        nombre = spec.get('nombre', 'grafico')
        tipo = spec.get('tipo', 'barras_horizontales')
        titulo = spec.get('titulo', '')
        etiqueta_x = spec.get('etiqueta_x', '')
        etiqueta_y = spec.get('etiqueta_y', '')
        formato_y = spec.get('formato_y', 'unidades')
        col_x = spec.get('columna_x', '')
        col_y = spec.get('columna_y', '')

        data_cols = cols
        data_rows = rows

        col_x_real = _match_col(col_x, data_cols)
        col_y_real = _match_col(col_y, data_cols)

        datos_graf = []
        for row in data_rows:
            row_dict = dict(zip(data_cols, row))
            y_val = row_dict.get(col_y_real or col_y, 0)
            if y_val is None:
                y_val = 0
            datos_graf.append({
                'x': str(row_dict.get(col_x_real or col_x, '')),
                'y': float(y_val),
            })

        if not datos_graf:
            continue

        print(f'  Generando: {titulo} ({len(datos_graf)} pts)...')
        r = generar_grafico(
            datos=datos_graf,
            tipo=tipo,
            titulo=titulo,
            etiqueta_x=etiqueta_x,
            etiqueta_y=etiqueta_y,
            formato_y=formato_y,
            output_path=str(charts_dir),
            timestamp=timestamp,
        )

        if not r['error']:
            ruta_rel = os.path.relpath(r['path'], BASE_DIR)
            imagenes_md.append(f"![{titulo}]({ruta_rel})")
            print(f'    OK: {ruta_rel}')
        else:
            print(f'    Error: {r["error"]}')

    return imagenes_md


def generar_informe(pregunta: str):
    print('\n' + '=' * 60)
    print('INTENCION DETECTADA: GENERAR INFORME DE VENTAS')
    print('=' * 60)

    instrucciones_gen = leer_instrucciones('generador_consultas.md')
    instrucciones_val = leer_instrucciones('validador.md')
    skill_informe_planning  = leer_skill_bloques_resumen('informe_ventas')
    skill_informe_redaccion = leer_skill_sin_yaml('informe_ventas')

    extraer_system_gen = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_gen, re.DOTALL)
    extraer_system_val = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_val, re.DOTALL)

    reglas_extra_gen = _reglas_gen()
    reglas_extra_val = _reglas_val()

    system_gen = (extraer_system_gen.group(1).strip() if extraer_system_gen else instrucciones_gen) + reglas_extra_gen
    system_val = (extraer_system_val.group(1).strip() if extraer_system_val else instrucciones_val) + reglas_extra_val

    # ------------------------------------------------------------------
    # FASE 1: el LLM decide que consultas necesita segun la peticion
    # ------------------------------------------------------------------
    prompt_planificacion = f"""El usuario quiere generar un informe con la siguiente peticion:
"{pregunta}"

Tienes disponible la siguiente skill de informes que define los bloques disponibles:
{skill_informe_planning}

Tu tarea es determinar que datos necesitas consultar en PostgreSQL para construir
ese informe. Genera UNA consulta SQL por cada bloque de datos que necesites.

Responde con un JSON con esta estructura exacta:
{{
  "bloques": ["A", "C", "D"],
  "consultas": [
    {{"nombre": "nombre_descriptivo", "sql": "SELECT ..."}},
    {{"nombre": "otro_nombre", "sql": "SELECT ..."}}
  ]
}}

- "bloques": lista de letras de bloques de la skill que usaras en el informe.
- "consultas": una entrada por cada consulta SQL necesaria para alimentar esos bloques.
- Genera solo las consultas que necesitas para responder la peticion del usuario.
- No incluyas consultas de datos que no vayas a usar en el informe.
- Responde SOLO con el JSON, sin texto adicional.
"""

    print(f'[{MODELO}] Planificando consultas segun la peticion...')
    respuesta_plan = llamar_llm(system_gen, prompt_planificacion, temperatura=0.1)

    # Extraer JSON de la respuesta
    plan = None
    bloques_json = re.search(r'\{.*\}', respuesta_plan, re.DOTALL)
    if bloques_json:
        try:
            plan = json.loads(bloques_json.group(0))
        except json.JSONDecodeError:
            pass

    if not plan or 'consultas' not in plan or not plan['consultas']:
        print('No se pudo parsear el plan. Usando consultas de respaldo...')
        plan = {
            'bloques': ['A', 'B', 'C'],
            'consultas': [
                {'nombre': 'ventas_totales',
                 'sql': """SELECT COUNT(*) AS transacciones,
                                  SUM("CANTIDAD") AS unidades,
                                  SUM("CANTIDAD" * "PVP") AS valor_cop
                           FROM ventas
                           WHERE TRIM("DESC_MOVIMIENTO") = 'VENTAS POS'
                             AND TRIM("SIGNO") = '-'"""},
            ]
        }

    print(f'Bloques seleccionados: {plan.get("bloques", [])}')
    print(f'Consultas a ejecutar: {len(plan["consultas"])}')

    # ------------------------------------------------------------------
    # FASE 2: ejecutar cada consulta del plan (sin validador — el LLM
    # genera SQL correcta y PostgreSQL reporta errores reales si los hay)
    # ------------------------------------------------------------------
    resultados = {}
    for item in plan['consultas']:
        nombre = item.get('nombre', 'consulta')
        sql_raw = item.get('sql', '').strip()
        if not sql_raw:
            continue

        sql_limpia = extraer_sql(sql_raw) if '```' in sql_raw else sql_raw
        if not sql_limpia.rstrip().endswith(';'):
            sql_limpia += ';'

        print(f'  Ejecutando: {nombre}...')
        resultado = ejecutar_consulta(sql_limpia, limite=500)
        if resultado.get('success'):
            resultados[nombre] = resultado
            print(f'  OK: {resultado["total_filas"]} fila(s)')
        else:
            # Un error real de PostgreSQL — intentar corregir UNA vez con el LLM
            error_pg = resultado.get('error', '')
            print(f'  Error PostgreSQL: {error_pg}. Intentando correccion...')
            correccion = llamar_llm(
                (extraer_system_gen.group(1).strip() if extraer_system_gen else instrucciones_gen) + reglas_extra_gen,
                f'La siguiente SQL produjo un error en PostgreSQL:\n\nSQL:\n{sql_limpia}\n\nError:\n{error_pg}\n\nCorrige la SQL para la consulta "{nombre}". Responde solo con el SQL corregido.',
                temperatura=0.1,
            )
            sql_corregida = extraer_sql(correccion)
            resultado2 = ejecutar_consulta(sql_corregida, limite=500)
            if resultado2.get('success'):
                resultados[nombre] = resultado2
                print(f'  OK (corregida): {resultado2["total_filas"]} fila(s)')
            else:
                print(f'  Omitiendo {nombre}: {resultado2.get("error")}')

    if not resultados:
        print('No se obtuvieron datos. Abortando informe.')
        return

    # ------------------------------------------------------------------
    # FASE 3: generar graficos a partir de los datos obtenidos
    # ------------------------------------------------------------------
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    print(f'\n[{MODELO}] Generando graficos...')
    imagenes_por_bloque = generar_graficos_informe(resultados, pregunta, plan, timestamp)

    # ------------------------------------------------------------------
    # FASE 4: redactar el informe con los bloques seleccionados
    # ------------------------------------------------------------------
    print(f'\n[{MODELO}] Redactando informe...')
    prompt_redaccion = f"""El usuario solicito:
"{pregunta}"

Usa la siguiente skill para redactar el informe en Markdown:
{skill_informe_redaccion}

Bloques seleccionados para este informe: {plan.get('bloques', [])}

Datos obtenidos de la base de datos:
{json.dumps(resultados, ensure_ascii=False, indent=2)}

Fecha de generacion: {datetime.now().strftime('%d/%m/%Y')}

Instrucciones:
- Construye el informe usando SOLO los bloques seleccionados, en el orden logico.
- Adapta el titulo y el contenido a lo que el usuario pidio exactamente.
- Usa unicamente los datos proporcionados. No inventes cifras.
- Formatea numeros con separador de miles y moneda COP donde corresponda.
- Si un bloque necesita un dato que no esta disponible, omite ese bloque o indica "Sin informacion".
"""
    if imagenes_por_bloque:
        prompt_redaccion += f"""
### Graficos generados
Inserta estos graficos como imagenes markdown DESPUES de la tabla de datos
del bloque correspondiente (nunca antes). Cada bloque indica que graficos
lleva.

Graficos por bloque:
{json.dumps(imagenes_por_bloque, ensure_ascii=False, indent=2)}
"""
    md_informe = llamar_llm(skill_informe_redaccion, prompt_redaccion, temperatura=0.3)

    # ------------------------------------------------------------------
    # FASE 5: guardar markdown y convertir a DOCX
    # ------------------------------------------------------------------
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '_', pregunta.lower())[:40].strip('_')
    ruta_md   = REPORTS_DIR / f'informe_{slug}_{timestamp}.md'
    ruta_docx = REPORTS_DIR / f'Informe_{slug}_{timestamp}.docx'

    ruta_md.write_text(md_informe, encoding='utf-8')
    print(f'Markdown guardado: {ruta_md}')

    print(f'[{MODELO}] Convirtiendo a DOCX...')
    docx_script = TOOLS_DIR / 'generar_docx.py'
    env = os.environ.copy()
    env['MD_CONTENT'] = md_informe
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        subprocess.run(
            ['python', str(docx_script), str(ruta_docx)],
            capture_output=True, text=True, env=env, timeout=30, encoding='utf-8'
        )
        print(f'DOCX generado: {ruta_docx}')
    except Exception as e:
        print(f'Error al generar DOCX: {e}')

    print('\n' + '=' * 60)
    print('INFORME')
    print('=' * 60)
    print(md_informe)

def procesar_consulta(pregunta: str):
    instrucciones_gen = leer_instrucciones('generador_consultas.md')
    instrucciones_val = leer_instrucciones('validador.md')
    instrucciones_red = leer_instrucciones('redactor_respuesta.md')

    extraer_system_gen = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_gen, re.DOTALL)
    extraer_system_val = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_val, re.DOTALL)
    extraer_system_red = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_red, re.DOTALL)

    system_gen = (extraer_system_gen.group(1).strip() if extraer_system_gen else instrucciones_gen) + _reglas_gen()
    system_val = (extraer_system_val.group(1).strip() if extraer_system_val else instrucciones_val) + _reglas_val()
    system_red = extraer_system_red.group(1).strip() if extraer_system_red else instrucciones_red

    sql_final = generar_sql_y_validar(pregunta, system_gen, system_val)

    print('Ejecutando consulta en PostgreSQL...')
    resultado = ejecutar_consulta(sql_final)

    if not resultado.get('success'):
        print(f'\nError al ejecutar: {resultado.get("error")}')
        sys.exit(1)

    print(f'Resultado: {resultado["total_filas"]} fila(s) obtenidas.\n')

    # ------------------------------------------------------------------
    # Generar grafico si tiene sentido
    # ------------------------------------------------------------------
    imagenes_chat = []
    if es_intencion_grafico(pregunta) and resultado.get('columns'):
        timestamp_graf = datetime.now().strftime('%Y%m%d_%H%M%S')
        print(f'[{MODELO}] Evaluando grafico para la consulta...')
        imagenes_chat = generar_graficos_consulta(resultado, pregunta, timestamp_graf)

    print(f'[{MODELO}] Redactando respuesta...')
    prompt_red = json.dumps(resultado, ensure_ascii=False, indent=2)
    if imagenes_chat:
        prompt_red += '\n\n### Grafico generado\n' + '\n'.join(imagenes_chat) + '\n\nSi hay un grafico, incluirlo en la respuesta como imagen markdown.'
    respuesta_final = llamar_llm(
        system_red,
        prompt_red,
        temperatura=0.3,
    )

    print('\n' + '=' * 60)
    print('RESPUESTA')
    print('=' * 60)
    print(respuesta_final)

def main():
    if len(sys.argv) < 2:
        print('Uso: python orquestador.py "tu pregunta en lenguaje natural"')
        sys.exit(1)

    pregunta = sys.argv[1]

    if es_intencion_informe(pregunta):
        generar_informe(pregunta)
    else:
        procesar_consulta(pregunta)

if __name__ == '__main__':
    main()
