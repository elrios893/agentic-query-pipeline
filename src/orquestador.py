#!/usr/bin/env python3
"""
Orquestador: pipeline de consulta de datos en lenguaje natural.
Soporta multiples proveedores LLM configurados via .env (LLM_PROVIDER).
"""
import os
import re
import json
import math
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

# ---------------------------------------------------------------------------
# Cliente de clasificación de intención — siempre Groq (llama-3.1-8b-instant)
# independiente del proveedor principal configurado en LLM_PROVIDER
# ---------------------------------------------------------------------------
CLASSIFY_INFERENCE   = os.getenv('CLASSIFY_INFERENCE', 'NO').strip().upper()
GROQ_MODEL_INFERENCE = os.getenv('GROQ_MODEL_INFERENCE', 'llama-3.1-8b-instant')

# Importar Groq si no se importó ya (puede que el provider principal sea cerebras/gemini)
try:
    from groq import Groq as _GroqClass
    _client_inference = _GroqClass(api_key=os.getenv('GROQ_API_KEY'))
except Exception:
    _client_inference = None

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
    hoy      = datetime.now()
    anio     = hoy.year
    anio_ant = anio - 1
    hoy_str         = hoy.strftime('%d/%m/%Y')
    rango_fin_actual   = hoy.strftime('%Y-%m-%d')
    rango_fin_anterior = f'{anio_ant}-{hoy.month:02d}-{hoy.day:02d}'
    return f"""

### Contexto temporal
- Hoy es {hoy_str}. El año {anio} está EN CURSO (no ha terminado).
- TABLA PRINCIPAL: ventas_unificada — vista materializada que une ventas_2025 y ventas_2026 con GRUPO normalizado.
  - Filtrar por año con WHERE "Año" = {anio} ({anio}) o WHERE "Año" = {anio_ant}.
  - Columna "GRUPO_NORM": GRUPO estandarizado desde la tabla de segmentación. USAR SIEMPRE en lugar de "GRUPO" cuando la tabla sea ventas_unificada.
- ventas_2025 y ventas_2026 solo usar si se necesita el dato crudo sin normalizar.
- NUNCA uses FROM ventas sin sufijo ni _unificada.
- Cuando el usuario mencione un dia o mes sin especificar año, SIEMPRE usa {anio} con ventas_unificada.

### Filtro de tiempo por defecto — CRÍTICO
- Si el usuario NO especifica año, un intervalo o un período de tiempo concreto (ej: "¿cuántas unidades hemos vendido de la referencia X?", "top productos por departamento", "ventas totales de la línea Y"), SIEMPRE filtra por LO QUE VA DEL AÑO {anio} (year-to-date). NUNCA devuelvas el año {anio} completo sin acotar, ni ambos años sin filtro:
  ```sql
  WHERE ... AND "Año" = {anio} AND TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY') BETWEEN '{anio}-01-01' AND '{rango_fin_actual}'
  ```
- Excepciones al default "lo que va del año" (usa el rango o año que corresponda en su lugar):
  - El usuario pide explícitamente comparar {anio} vs {anio_ant} (u otros períodos) → sigue el patrón de "Comparaciones año en curso vs año histórico" más abajo (también usa YTD en ambos lados, así que el resultado es consistente con este default).
  - El usuario pide explícitamente el total/histórico completo de un año ya terminado (ej. "todo el {anio_ant}", "el año pasado completo") → usa ese año completo sin recortar por fecha.
  - El usuario da un período o rango explícito (ej. "en marzo", "del 1 al 15 de julio", "el trimestre pasado", "todo el {anio}" sabiendo que está en curso) → usa exactamente ese período/rango en vez del YTD.

### Comparaciones año en curso vs año histórico — CRÍTICO
Comparar el año {anio} (en curso, solo tiene datos hasta {hoy_str}) contra el año {anio_ant} COMPLETO es una comparación FALSA: un lado tiene ~{hoy.timetuple().tm_yday} días de datos y el otro 365. Cualquier "caída" o "crecimiento" calculado así es artificial, no real.
- Si el usuario pide comparar "este año"/"{anio}" contra "{anio_ant}"/"el año pasado" SIN pedir explícitamente el total anual completo de {anio_ant}, usa el MISMO rango de fechas en ambos años (mismo día del año):
  - {anio}: TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY') BETWEEN '{anio}-01-01' AND '{rango_fin_actual}'
  - {anio_ant}: TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY') BETWEEN '{anio_ant}-01-01' AND '{rango_fin_anterior}'
  Patrón obligatorio (CASE WHEN, nunca JOIN — ver regla 11):
  ```sql
  SUM(CASE WHEN "Año"={anio} AND TO_DATE("FECHA_MVTO",'FMDD/FMMM/YYYY') BETWEEN '{anio}-01-01' AND '{rango_fin_actual}' THEN "CANTIDAD" ELSE 0 END) AS "Unidades_{anio}",
  SUM(CASE WHEN "Año"={anio_ant} AND TO_DATE("FECHA_MVTO",'FMDD/FMMM/YYYY') BETWEEN '{anio_ant}-01-01' AND '{rango_fin_anterior}' THEN "CANTIDAD" ELSE 0 END) AS "Unidades_{anio_ant}"
  ```
- Excepción — SÍ usar el año completo sin restringir rango cuando:
  - El usuario pide explícitamente el total/resumen anual completo de {anio_ant} (año ya terminado, sin ambigüedad de comparación).
  - Ambos años comparados ya terminaron (ninguno es {anio}).
  - El usuario pide explícitamente "todo el año {anio}" sabiendo que está en curso (ej. una proyección o total parcial declarado como tal).
- Si aplicaste el rango equivalente, nombra las columnas de forma que quede claro (ej. "Unidades_{anio}_a_la_fecha" en vez de solo "Unidades_{anio}") para que el redactor pueda explicarlo.

### Reglas adicionales obligatorias
1. Siempre usa comillas dobles en TODOS los nombres de columna.
2. Usa TRIM() en columnas de texto: TRIM("DEPARTAMENTO"), TRIM("DESC_MOVIMIENTO"), TRIM("LINEA"), etc.
3. FECHA_MVTO es TEXT en formato D/M/YYYY sin ceros (ej: 1/7/2026). USA TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY'). NUNCA uses ::DATE ni TO_DATE con 'DD/MM/YYYY'.
4. Para valor de ventas usa "CANTIDAD" * "PVP". NUNCA uses "PVP LISTA" para tiendas individuales.
5. "PVP LISTA" SOLO se usa si la consulta es sobre clientes MACRO (cadenas), no tiendas.
6. Si un alias tiene mayusculas (ej: "Ventas"), ponle comillas dobles en ORDER BY y GROUP BY: ORDER BY "Ventas" DESC.
7. Textos siempre en mayusculas en SELECT: DEPARTAMENTO, CIUDAD, DESC_DEPENDENCIA, RAZON_SOCIAL, CLIMA, ZONA, ZONA_EX, DESC_ITEM deben usar UPPER(TRIM(...)).
8. EXCEPCION: columna LINEA tiene casing mixto. Usar solo TRIM("LINEA") = '11 - Dama Deportivo'. NUNCA UPPER(TRIM("LINEA")).
9. NUNCA uses TRIM("SIGNO") ni ningun filtro sobre "SIGNO". DESC_MOVIMIENTO = 'VENTAS POS' ya delimita las ventas.
10. CAST para ROUND en porcentajes y decimales: PostgreSQL requiere CAST(...AS numeric) antes de ROUND(). Si calculas porcentajes o decimales, usa siempre ROUND(CAST((SUM(...) * 100.0) / NULLIF(...) AS numeric), 2). NUNCA uses ROUND() sin CAST en operaciones aritmeticas.
11. Comparaciones de periodos temporales (ej: enero vs febrero, mes1 vs mes2, o {anio} vs {anio_ant}): NUNCA uses FULL OUTER JOIN, LEFT JOIN, o RIGHT JOIN. Usa UNA SOLA tabla con multiples CASE WHEN para cada periodo. Ejemplo correcto: SELECT SUM(CASE WHEN fecha BETWEEN ene THEN valor ELSE 0 END) AS Enero, SUM(CASE WHEN fecha BETWEEN feb THEN valor ELSE 0 END) AS Febrero FROM tabla WHERE fecha BETWEEN ene_inicio AND feb_fin GROUP BY ...
12. REFERENCIA siempre acompañada de GRUPO: Cuando la consulta incluya la columna "REFERENCIA" en el SELECT, DEBE incluir también el grupo de producto. Si la tabla es ventas_unificada, usa TRIM("GRUPO_NORM") AS "GRUPO" (NUNCA la columna "GRUPO" cruda de esa vista). Si la tabla es ventas_2025 o ventas_2026 directamente, usa UPPER(TRIM("GRUPO")) AS "GRUPO". Esto es obligatorio para que el usuario pueda contextualizar cada referencia dentro de su grupo de producto.
"""

def _reglas_val() -> str:
    from datetime import datetime
    hoy      = datetime.now()
    anio     = hoy.year
    anio_ant = anio - 1
    return f"""

### Fecha real del sistema (autoritativa — ignora cualquier otra fecha mencionada arriba)
- Hoy es {hoy.strftime('%d/%m/%Y')}. El año {anio} está en curso.
- Una fecha literal de {anio} anterior o igual a hoy NUNCA es "una fecha futura" — no rechaces por eso.

### Reglas adicionales obligatorias
1. Verifica que TODOS los nombres de columna esten entre comillas dobles.
2. Verifica uso de TRIM() en filtros de texto.
3. No aceptes LIMIT en COUNT(*) o agregaciones simples.
4. Rechaza TO_DATE con formato 'DD/MM/YYYY' o ::DATE. Solo acepta TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY').
5. Revisa que use "CANTIDAD" * "PVP" para valor de ventas, no "PVP LISTA" (a menos que sea consulta macro).
6. Revisa que los alias con mayusculas usen comillas dobles en ORDER BY/GROUP BY.
7. Revisa que DEPARTAMENTO, CIUDAD, DESC_DEPENDENCIA, RAZON_SOCIAL, CLIMA, ZONA, ZONA_EX, DESC_ITEM usen UPPER(TRIM(...)). Si aparecen sin UPPER en el SELECT, NO rechazar — es opcional.
8. Verifica que el año en los literales de fecha sea {anio} o {anio_ant}. Si ves un año anterior a {anio_ant} en una fecha literal, RECHAZAR. Fechas de {anio} hasta hoy ({hoy.strftime('%d/%m/%Y')}) son válidas, NUNCA "futuras".
18. Comparación {anio} vs {anio_ant} sin rango equivalente: si la consulta compara el año {anio} completo (sin filtro de fecha que lo acote a lo transcurrido) contra el año {anio_ant} completo, y por el nombre de las columnas/alias no es evidente que el usuario pidió el total anual completo a propósito, ADVIERTE en el feedback que la comparación puede ser desigual (año en curso vs año completo) — pero NO rechaces solo por esto, es una decisión de negocio del generador, no un error de sintaxis.
9. LINEA — excepcion critica: la columna LINEA tiene casing mixto. TRIM("LINEA") = '11 - Dama Deportivo' es CORRECTO. UPPER(TRIM("LINEA")) en un filtro WHERE es un ERROR — RECHAZAR si aparece. NUNCA rechazar una query por usar TRIM("LINEA") sin UPPER.
10. SIGNO — prohibido: NUNCA debe aparecer TRIM("SIGNO") ni ningun filtro sobre "SIGNO" en la query. Si aparece → RECHAZAR e indicar que se elimine. DESC_MOVIMIENTO = 'VENTAS POS' ya delimita las ventas sin necesidad de SIGNO.
11. LIMIT y window functions: si la consulta usa RANK(), ROW_NUMBER() o DENSE_RANK() con un filtro WHERE sobre el ranking (ej: WHERE ranking = 1), el resultado esta acotado por el numero de grupos del PARTITION BY — NO exigir LIMIT. Lineas de producto son ~10, departamentos ~33, tallas ~10: estos GROUP BY nunca necesitan LIMIT.
12. Alias internos de subconsultas: alias en minusculas sin caracteres especiales (ranking, rn, row_num, subconsulta) NO requieren comillas dobles. WHERE ranking = 1 es correcto. NUNCA rechazar por ausencia de comillas en alias de window functions o nombres de subquery.
13. NO rechazar dos veces por el mismo problema. Si el generador ya corrigio un error en el intento anterior, aprobarlo aunque queden imperfecciones menores de estilo.
14. CAST para ROUND: si la consulta usa ROUND() sobre una operacion aritmetica (division, multiplicacion), DEBE incluir CAST(...AS numeric). Si ves ROUND((... * 100) / ..., N) sin CAST → RECHAZAR. La forma correcta es ROUND(CAST((... * 100) / ... AS numeric), N).
15. Deteccion de Cartesian product en comparaciones de periodos: si la consulta contiene FULL OUTER JOIN, LEFT JOIN, o RIGHT JOIN combinado con EXTRACT(DAY FROM ...) = EXTRACT(DAY FROM ...) u otro EXTRACT en la condicion ON → RECHAZAR. Feedback: "Para comparar periodos (enero vs febrero), NO uses JOINs. Usa UNA tabla con multiples CASE WHEN para cada periodo: SUM(CASE WHEN fecha BETWEEN ene THEN valor ELSE 0 END) AS Enero, SUM(CASE WHEN fecha BETWEEN feb THEN valor ELSE 0 END) AS Febrero".
16. LIMIT obligatorio en subqueries con "día/fecha de mayor venta": si la consulta detecta un patrón como COALESCE(...SELECT...GROUP BY fecha...LIMIT 1), DEBE tener LIMIT N en la query principal. Sin LIMIT → RECHAZAR. Mensaje: "Falta LIMIT en la query principal. Subqueries que buscan 'día de mayor venta' requieren LIMIT obligatorio para evitar procesar todas las filas. Agregar LIMIT 10 (o el número que pidió el usuario) antes del punto y coma final".
17. Tabla ventas_unificada: es una tabla válida. Aceptar consultas que usen FROM ventas_unificada. Si usa "GRUPO" en lugar de "GRUPO_NORM" sobre ventas_unificada, ADVERTIR en el feedback pero NO rechazar.
19. Filtro de tiempo por defecto sin acotar: si la consulta filtra WHERE "Año" = {anio} (año en curso) SIN restringir también por fecha (TO_DATE("FECHA_MVTO", ...) BETWEEN/<= hasta una fecha cercana a hoy), y no hay evidencia en los alias/columnas de que el usuario pidió explícitamente "todo el año", una proyección, o una comparación entre años ya terminados, ADVIERTE en el feedback que falta acotar a "lo que va del año" (year-to-date) — no rechaces solo por esto, es un recordatorio para el generador, no un error de sintaxis."""

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

PATRONES_EXCEL = re.compile(
    r'(excel|\.xlsx?|exporta\w*\s*(?:a\s*)?excel|'
    r'descargar\s*(?:como|en|a)?\s*excel|'
    r'hoja\s*de\s*calculo|p.s[a-z]*\s*(?:lo|me|a)\s*excel)',
    re.IGNORECASE
)

PATRONES_BUSQUEDA_WEB = re.compile(
    r'(tendencias?\s+(de\s+|del\s+)?mercado|mercado\s+actual|'
    r'qu[eé]\s+(dice|hay en)\s+(el\s+)?internet|busca\w*\s+en\s+internet|'
    r'investiga\w*\s+en\s+internet|competencia|competidor\w*|'
    r'noticias?\s+(de|sobre)|est[aá]\s+de\s+moda|en\s+tendencia|'
    r'seg[uú]n\s+internet|b[uú]scalo\s+en\s+(google|internet|la\s+web)|'
    r'busca\w*\s+en\s+(google|la\s+web))',
    re.IGNORECASE
)

def es_intencion_informe(texto: str) -> bool:
    return bool(PATRONES_INFORME.search(texto))

def es_intencion_busqueda_web(texto: str) -> bool:
    return bool(PATRONES_BUSQUEDA_WEB.search(texto))

def es_intencion_grafico(texto: str) -> bool:
    return bool(PATRONES_GRAFICO.search(texto))

def es_intencion_excel(texto: str) -> bool:
    return bool(PATRONES_EXCEL.search(texto))

# ---------------------------------------------------------------------------
# Mapeo oficial: Bloque Informe → Tipo(s) de Gráfico Esperado(s)
# ---------------------------------------------------------------------------
MAPEO_BLOQUES_GRAFICOS = {
    'A': None,  # Encabezado: sin gráfico
    'B': None,  # Resumen ejecutivo: sin gráfico (bullets)
    'C': 'barras_agrupadas',  # Métricas/variación vs período anterior
    'D': ['barras_horizontales', 'torta'],  # Geográfico: ranking o participación
    'E': 'barras_horizontales',  # Dependencias: ranking
    'F': 'barras_horizontales',  # Tiendas: ranking
    'G': ['barras_verticales', 'barras_horizontales'],  # Línea de producto
    'H': 'barras_verticales',  # Producto (DESC_ITEM)
    'I': 'barras_horizontales',  # Referencias: ranking top
    'J': 'barras_verticales',  # Tallas: distribución
    'K': 'linea',  # Evolución temporal: siempre línea
    'L': 'barras_verticales',  # Tiendas activas
    'M': None,  # Alertas: sin gráfico
    'N': None,  # Anexo de datos: sin gráfico
    'O': None,  # Tablas interactivas: sin gráfico PNG
    'P': 'barras_horizontales',  # Devoluciones: tasa % por grupo (top 10)
    'Q': ['barras_horizontales', 'barras_agrupadas'],  # Categorías: mix línea o comparación períodos
    'R': ['barras_verticales', 'linea'],  # Precios: distribución rangos o evolución precio promedio
}

def leer_instrucciones(archivo: str) -> str:
    ruta = AGENTS_DIR / archivo
    return ruta.read_text(encoding='utf-8')


def _secciones_nivel2(texto: str) -> dict[str, str]:
    """Divide un archivo .md de agents/ en sus secciones de nivel '## ',
    ignorando encabezados que aparezcan dentro de bloques de código (```) —
    varias secciones incluyen ejemplos de salida que contienen '## ' como
    texto literal, no como encabezado real."""
    secciones: dict[str, str] = {}
    nombre_actual = None
    buffer: list[str] = []
    en_fence = False
    for linea in texto.split('\n'):
        if linea.strip().startswith('```'):
            en_fence = not en_fence
            if nombre_actual is not None:
                buffer.append(linea)
            continue
        if not en_fence and linea.startswith('## '):
            if nombre_actual is not None:
                secciones[nombre_actual] = '\n'.join(buffer).strip()
            nombre_actual = linea[3:].strip()
            buffer = []
            continue
        if nombre_actual is not None:
            buffer.append(linea)
    if nombre_actual is not None:
        secciones[nombre_actual] = '\n'.join(buffer).strip()
    return secciones


def cargar_system_prompt(archivo: str, secciones: list[str]) -> str:
    """Carga agents/<archivo> y concatena las secciones '## <nombre>' pedidas,
    en el orden dado. Reemplaza el patrón anterior (regex que capturaba solo
    hasta la primera '## ' siguiente) que dejaba fuera del system prompt
    secciones enteras como 'Modo análisis profundo' o 'Manejo de Gráficos'."""
    disponibles = _secciones_nivel2(leer_instrucciones(archivo))
    partes = []
    for nombre in secciones:
        contenido = disponibles.get(nombre)
        if contenido is None:
            print(f'  [WARN] Sección "{nombre}" no encontrada en agents/{archivo}')
            continue
        partes.append(contenido)
    return '\n\n'.join(partes)

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

def cargar_secciones_skill(nombre: str, secciones: list[str]) -> str:
    """Como cargar_system_prompt() pero para skills/<nombre>/SKILL.md: concatena
    solo las secciones '## <nombre>' pedidas en vez del archivo completo.
    Evita repetir contenido que ya se envia por separado (ej. la seccion
    'Bloques disponibles', que generar_informe() ya adjunta via
    extraer_seccion_skill) y contenido de otra fase (ej. 'Filosofia de uso'
    u 'Orden de bloques', que son guia de planificacion, no de redaccion)."""
    disponibles = _secciones_nivel2(leer_skill_sin_yaml(nombre))
    partes = []
    for s in secciones:
        contenido = disponibles.get(s)
        if contenido is None:
            print(f'  [WARN] Sección "{s}" no encontrada en skills/{nombre}/SKILL.md')
            continue
        partes.append(contenido)
    return '\n\n'.join(partes)

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

EXCEL_SHEETS_DIR = BASE_DIR / 'excel_sheets'

def exportar_excel_desde_resultado(resultado: dict, pregunta: str) -> str:
    """
    Exporta el resultado de una consulta a un archivo .xlsx.
    Retorna la ruta del archivo o string vacio si falla.
    """
    cols = resultado.get('columns', [])
    rows = resultado.get('rows', [])
    if not cols or not rows:
        return ''

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    slug = re.sub(r'[^a-z0-9]+', '_', pregunta.lower())[:40].strip('_')
    if not slug:
        slug = 'datos'
    nombre = f'{slug}_{ts}.xlsx'
    ruta = str(EXCEL_SHEETS_DIR / nombre)

    import json as _json
    payload = _json.dumps({
        'headers': cols,
        'rows': rows,
        'output_path': ruta,
        'sheet_name': 'Resultados',
        'title': pregunta[:60],
    })

    script = TOOLS_DIR / 'generar_excel.py'
    env = os.environ.copy()
    env['EXCEL_DATA'] = payload
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        proc = subprocess.run(
            ['python', str(script)],
            capture_output=True, text=True, env=env, timeout=60, encoding='utf-8'
        )
        out = _json.loads(proc.stdout)
        if out.get('success'):
            print(f'Excel generado: {ruta}')
            return ruta
        else:
            print(f'Error al generar Excel: {out.get("error")}')
            return ''
    except Exception as e:
        print(f'Error al generar Excel: {e}')
        return ''


def quiere_excel(pregunta: str, resultado: dict) -> bool:
    """Criterio unico de 'esta consulta necesita exportarse a Excel':
    lo pidio explicitamente en el texto, o el resultado supera las 100
    filas (umbral de auto-exportacion). Usado tanto por procesar_consulta
    (consulta unica) como por _procesar_multi_consulta en server.py (para
    decidir que sub-preguntas entran al Excel combinado de varias hojas)."""
    if not resultado or not resultado.get('rows'):
        return False
    total_filas = resultado.get('total_filas', 0)
    return total_filas > 100 or es_intencion_excel(pregunta)


_PATRON_VERBO_FORMATO = re.compile(
    r'^(genera(r|me)?|crea(r|me)?|dame|quiero|haz(me)?|exporta(r|me)?|'
    r'descarga(r|me)?|mu[eé]stra(me)?)\s+(un|una|el|la|los|las)?\s*'
    r'(excel|\.xlsx?|hoja\s*de\s*calculo|gr[aá]fic\w*|tabla)\w*\s*(de|del|con|sobre)?\s*',
    re.IGNORECASE,
)


def _titulo_hoja(pregunta: str) -> str:
    """Quita el verbo+formato ('genera un excel de...') de una sub-pregunta
    para usar solo el tema como nombre/título de hoja — sin esto, el nombre
    de hoja queda como 'genera un excel de las ventas p...' en vez de algo
    legible como 'Ventas por tienda en Antioquia'."""
    limpio = _PATRON_VERBO_FORMATO.sub('', pregunta.strip()).strip()
    limpio = limpio or pregunta.strip()
    return limpio[0].upper() + limpio[1:] if limpio else limpio


def exportar_excel_multi_hoja(hojas: list[dict], nombre_base: str) -> dict:
    """
    Exporta VARIOS resultados a un solo archivo .xlsx con una hoja por
    elemento, en vez de un archivo por elemento — usado cuando una
    sub-consulta (ver server.py::_procesar_multi_consulta) detecta que 2+
    de sus sub-preguntas quieren Excel.

    Args:
        hojas: lista de {'pregunta': str, 'resultado': dict} — 'resultado'
            es el dict de ejecutar_consulta (columns/rows).
        nombre_base: texto para derivar el nombre del archivo (slug).

    Retorna: {'ruta': str, 'nombres_hojas': list[str]} — 'ruta' vacio si falla.
    """
    hojas_payload = []
    for h in hojas:
        cols = h['resultado'].get('columns', [])
        rows = h['resultado'].get('rows', [])
        if not cols or not rows:
            continue
        titulo = _titulo_hoja(h['pregunta'])
        hojas_payload.append({
            'headers':    cols,
            'rows':       rows,
            'sheet_name': titulo[:31],
            'title':      titulo[:60],
        })
    if not hojas_payload:
        return {'ruta': '', 'nombres_hojas': []}

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    slug = re.sub(r'[^a-z0-9]+', '_', nombre_base.lower())[:40].strip('_') or 'datos'
    ruta = str(EXCEL_SHEETS_DIR / f'{slug}_{ts}.xlsx')

    import json as _json
    payload = _json.dumps({'hojas': hojas_payload, 'output_path': ruta})

    script = TOOLS_DIR / 'generar_excel.py'
    env = os.environ.copy()
    env['EXCEL_DATA'] = payload
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        proc = subprocess.run(
            ['python', str(script)],
            capture_output=True, text=True, env=env, timeout=60, encoding='utf-8'
        )
        out = _json.loads(proc.stdout)
        if out.get('success'):
            print(f'Excel combinado generado ({len(out["hojas"])} hoja(s)): {ruta}')
            return {'ruta': ruta, 'nombres_hojas': out['hojas']}
        print(f'Error al generar Excel combinado: {out.get("error")}')
        return {'ruta': '', 'nombres_hojas': []}
    except Exception as e:
        print(f'Error al generar Excel combinado: {e}')
        return {'ruta': '', 'nombres_hojas': []}


def limpiar_texto(texto: str) -> str:
    return ''.join(c for c in texto if unicodedata.category(c) != 'So').strip()

def extraer_seccion_skill(nombre_skill: str, titulo_seccion: str) -> str:
    """
    Extrae una sección específica de un skill para ahorrar tokens.
    
    Ej: extraer_seccion_skill('graficos_ventas', 'Mapeo:')
    → extrae desde '## Mapeo: tipo de dato / pregunta → tipo de grafico'
       hasta la próxima sección '## ...'
    
    Args:
        nombre_skill: nombre del skill (ej: 'graficos_ventas')
        titulo_seccion: patrón de búsqueda (ej: 'Mapeo:', 'Como transformar', 'Cuando NO')
    
    Returns:
        Texto de la sección encontrada, o skill completo si no encuentra
    """
    contenido = leer_skill_sin_yaml(nombre_skill)
    
    # Buscar la sección por patrón (case-insensitive)
    lineas = contenido.split('\n')
    inicio = None
    fin = None
    
    # Encontrar línea donde empieza la sección
    for i, linea in enumerate(lineas):
        if titulo_seccion.lower() in linea.lower() and linea.startswith('##'):
            inicio = i
            break
    
    if inicio is None:
        # Si no encuentra, retornar el skill completo
        return contenido
    
    # Encontrar la siguiente sección (## ) después del inicio
    for i in range(inicio + 1, len(lineas)):
        if lineas[i].startswith('## ') and lineas[i] != lineas[inicio]:
            fin = i
            break
    
    if fin is None:
        fin = len(lineas)
    
    # Retornar la sección (del inicio al fin)
    seccion = '\n'.join(lineas[inicio:fin]).strip()
    return seccion

def es_intencion_tabla(texto: str) -> bool:
    """Detecta si el usuario pide explícitamente una tabla."""
    patrones = re.compile(
        r'\b(tabla|compara|ranking|desglose|top\s+\d+|listar|lista|detalle)\b',
        re.IGNORECASE
    )
    return bool(patrones.search(texto))

def detectar_subquery_dia_mayor_venta(sql: str) -> bool:
    """
    Detecta si la consulta tiene subqueries para buscar 'día de mayor venta'.
    Patrón: COALESCE(...SELECT...GROUP BY...LIMIT 1...) o similar
    """
    # Verificar que tenga COALESCE y SELECT (pueden estar separados por newlines/espacios)
    tiene_coalesce = bool(re.search(r"COALESCE", sql, re.IGNORECASE))
    tiene_select = bool(re.search(r"SELECT", sql, re.IGNORECASE))
    
    if tiene_coalesce and tiene_select:
        # Verificar que tenga GROUP BY + LIMIT 1 dentro (patrón de subquery)
        patron_group_limit = r"GROUP\s+BY.*?LIMIT\s+1"
        tiene_group_limit = bool(re.search(patron_group_limit, sql, re.IGNORECASE | re.DOTALL))
        return tiene_group_limit
    
    return False

def asegurar_limit_en_subquery(sql: str, valor_default: int = 10) -> tuple[str, bool]:
    """
    Si la consulta detecta subquery de "día de mayor venta" SIN LIMIT en la query principal,
    agrega LIMIT valor_default automáticamente.
    
    Returns:
        (sql_corregida, fue_modificada)
    """
    if not detectar_subquery_dia_mayor_venta(sql):
        return sql, False
    
    # Verificar si ya tiene LIMIT
    if re.search(r'LIMIT\s+\d+\s*[;]?\s*$', sql, re.IGNORECASE):
        # Ya tiene LIMIT, no modificar
        return sql, False
    
    # Agregar LIMIT si falta
    sql_sin_newline = sql.rstrip()
    if sql_sin_newline.endswith(';'):
        sql_con_limit = sql_sin_newline[:-1] + f' LIMIT {valor_default};'
    else:
        sql_con_limit = sql_sin_newline + f' LIMIT {valor_default};'
    
    return sql_con_limit, True

def _muestra_representativa(resultado: dict, limite: int) -> list:
    """
    Selecciona qué filas mostrar cuando el resultado excede 'limite'.

    Un corte posicional (rows[:limite]) sobre un resultado con 2+ dimensiones
    categóricas (ej. ZONA x CATEGORIA) sesga hacia las combinaciones de mayor
    volumen y puede dejar zonas enteras sin representación — no porque esa
    zona no importe, sino porque sus categorías individuales no entran en el
    top-N global. Si se detecta una columna de baja cardinalidad, se reparte
    el cupo entre TODOS sus valores para que ninguno quede en cero.
    """
    cols = resultado.get('columns', [])
    rows = resultado.get('rows', [])
    if len(rows) <= limite:
        return rows

    primera = dict(zip(cols, rows[0]))
    cols_cat = [c for c, v in primera.items() if isinstance(v, str)]

    idx_estrato = None
    mejor_cardinalidad = None
    for c in cols_cat:
        idx = cols.index(c)
        n = len({row[idx] for row in rows})
        # Candidato válido: 2-30 grupos (evita columnas casi constantes y
        # columnas de alta cardinalidad como REFERENCIA/DESC_ITEM). Entre
        # varias columnas válidas, se prefiere la de menor cardinalidad.
        if 2 <= n <= 30 and (mejor_cardinalidad is None or n < mejor_cardinalidad):
            idx_estrato, mejor_cardinalidad = idx, n

    if idx_estrato is None:
        return rows[:limite]

    grupos: dict = {}
    for row in rows:
        grupos.setdefault(row[idx_estrato], []).append(row)

    cupo_por_grupo = max(1, limite // len(grupos))
    muestra = []
    for filas_grupo in grupos.values():
        muestra.extend(filas_grupo[:cupo_por_grupo])

    # Si algún grupo era más chico que su cupo, sobra presupuesto: rellenar
    # con las siguientes filas de los grupos grandes hasta llegar al límite.
    if len(muestra) < limite:
        usados = {id(row) for row in muestra}
        restantes = [row for row in rows if id(row) not in usados]
        muestra.extend(restantes[:limite - len(muestra)])

    return muestra[:limite]


def formatear_resultado_como_tabla(resultado: dict, limite: int = 20) -> str:
    """
    Convierte un resultado SQL en tabla markdown formateada.

    Args:
        resultado: dict con 'columns' y 'rows'
        limite: máximo de filas a mostrar (default 20)

    Returns:
        Tabla markdown formateada o empty string si no hay datos
    """
    cols = resultado.get('columns', [])
    rows = resultado.get('rows', [])

    if not cols or not rows:
        return ''

    # Limitar filas (muestra representativa si hay varias dimensiones)
    total_filas = len(rows)
    rows_mostrados = _muestra_representativa(resultado, limite)
    
    # Encabezado
    lineas = []
    lineas.append('| ' + ' | '.join(str(c) for c in cols) + ' |')
    
    # Separador (detectar si columna es numérica)
    separadores = []
    for col in cols:
        # Si el nombre contiene palabras de número, alinear a derecha
        if any(x in col.lower() for x in ['unidades', 'valor', 'cantidad', 'total', '%', 'dia', 'ranking']):
            separadores.append('---:')
        else:
            separadores.append('---')
    lineas.append('| ' + ' | '.join(separadores) + ' |')
    
    # Filas
    for row in rows_mostrados:
        fila_formateada = []
        for i, val in enumerate(row):
            # Formatear valores
            if isinstance(val, bool):
                val_str = str(val)
            elif isinstance(val, int):
                val_str = f"{val:,}" if val > 999 else str(val)
            elif isinstance(val, float):
                # SUM(...)/ROUND(CAST(...AS numeric)) llega como float aunque
                # represente un conteo entero (ej. unidades) — sin decimales
                # espurios si el valor no tiene parte fraccionaria.
                if val == int(val):
                    val_str = f"{int(val):,}"
                else:
                    val_str = f"{val:,.2f}"
            else:
                val_str = str(val) if val is not None else ''
            fila_formateada.append(val_str)
        lineas.append('| ' + ' | '.join(fila_formateada) + ' |')
    
    tabla = '\n'.join(lineas)
    
    # Agregar nota si hay más filas
    if total_filas > limite:
        tabla += f'\n\n*(Mostrando {limite} de {total_filas} resultados)*'
    
    return tabla

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

def formatear_sql_para_impresion(sql: str) -> str:
    """
    Formatea SQL para impresión: agrega saltos de línea en puntos lógicos.
    Mejora legibilidad en consola.
    """
    # Keywords que deben ir en nueva línea (si no lo están ya)
    keywords = [
        'SELECT', 'FROM', 'WHERE', 'GROUP BY', 'HAVING', 'ORDER BY',
        'LIMIT', 'JOIN', 'LEFT JOIN', 'RIGHT JOIN', 'FULL OUTER JOIN',
        'INNER JOIN', 'CROSS JOIN', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
        'UNION', 'UNION ALL', 'EXCEPT', 'INTERSECT',
    ]
    
    sql_formateado = sql
    
    # Agregar salto de línea ANTES de cada keyword (si no lo tiene)
    for kw in keywords:
        # Patrón: cualquier carácter ANTES del keyword, pero no otro palabra
        patron = r'([^ \n])(\s+)(' + re.escape(kw) + r')(\s+)'
        reemplazo = r'\1\n\3 '
        sql_formateado = re.sub(patron, reemplazo, sql_formateado, flags=re.IGNORECASE)
    
    # Agregar salto de línea DESPUÉS de comas en SELECT (pero mantener indentación)
    sql_formateado = re.sub(r',(\s+)', ',\n  ', sql_formateado)
    
    # Limpiar múltiples saltos de línea
    sql_formateado = re.sub(r'\n\s*\n+', '\n', sql_formateado)
    
    return sql_formateado.strip()

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
        return {'success': False, 'error': str(e), 'error_fuente': 'proceso'}


def generar_sql_y_validar(pregunta: str, system_gen: str, system_val: str) -> str:
    print(f'[{MODELO}] Generando consulta...')
    consulta_generada = llamar_llm(system_gen, pregunta)
    
    # LIMPIEZA: remover ruido de logs que el LLM puede incluir
    consulta_generada = consulta_generada.replace('SQL_LOG_END', '').replace('SQL_LOG::', '')
    
    consulta_limpia = extraer_sql(consulta_generada)
    
    # Corregir LIMIT faltante en subqueries de "día de mayor venta"
    consulta_limpia, fue_corregida = asegurar_limit_en_subquery(consulta_limpia, valor_default=10)
    if fue_corregida:
        print('ℹ LIMIT agregado automáticamente en subquery de "día de mayor venta"')
    
    # Formatear SQL para impresión (saltos de línea)
    consulta_formateada = formatear_sql_para_impresion(consulta_limpia)
    
    print(f'Consulta generada:\n{consulta_formateada}\n')
    print(f'SQL_LOG::consulta_principal::{consulta_limpia}')
    print('SQL_LOG_END')

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
            
            # LIMPIEZA: remover ruido
            consulta_generada = consulta_generada.replace('SQL_LOG_END', '').replace('SQL_LOG::', '')
            
            consulta_limpia = extraer_sql(consulta_generada)
            
            # Aplicar corrección de LIMIT nuevamente
            consulta_limpia, fue_corregida = asegurar_limit_en_subquery(consulta_limpia, valor_default=10)
            if fue_corregida:
                print('ℹ LIMIT agregado automáticamente en subquery de "día de mayor venta"')
            
            # Formatear para impresión
            consulta_formateada = formatear_sql_para_impresion(consulta_limpia)
            print(f'Nueva consulta:\n{consulta_formateada}\n')
        else:
            print('Se agotaron los intentos de validacion.')
            # RuntimeError, no sys.exit(1): esta función corre dentro del
            # proceso del servidor FastAPI (server.py llama a procesar_consulta
            # directamente, sin subprocess). sys.exit() lanza SystemExit, que
            # hereda de BaseException — el except Exception de /chat en
            # server.py NO lo captura, así que el error nunca se registraba en
            # prompts/ y el usuario no recibía ninguna respuesta. main() (modo
            # CLI, abajo) captura este RuntimeError y sí sigue terminando el
            # proceso con exit code 1, igual que antes.
            raise RuntimeError(
                f'No se pudo generar una consulta SQL válida para: "{pregunta}". '
                f'Último rechazo del validador: {validacion}'
            )

def generar_graficos_informe(resultados, pregunta, plan, timestamp):
    """
    Genera graficos para un informe completo usando MAPEO_BLOQUES_GRAFICOS.
    
    Retorna dict: {
        'bloque_A': ['![titulo](ruta)'],
        'bloque_K': ['![titulo](ruta)'],
        ...
    }
    
    OPTIMIZACION: Envía solo la sección "Mapeo" de la skill graficos_ventas
    en lugar del archivo completo (~80% menos tokens).
    """
    # OPTIMIZACION: Extraer solo la sección "Mapeo" en lugar de skill completo
    skill_graficos = extraer_seccion_skill('graficos_ventas', 'Mapeo:')

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

    # Construir información del mapeo oficial
    mapeo_info = "### Mapeo oficial de bloques → tipos de gráficos y consultas esperadas\n"
    bloques_activos = plan.get('bloques', [])
    
    # Construir descripción de cada bloque con su propósito y tipo de gráfico
    bloque_descripciones = {
        'A': 'Encabezado - NO requiere gráfico',
        'B': 'Resumen ejecutivo - NO requiere gráfico',
        'C': 'Métricas principales / Comparación períodos - Espera: barras_agrupadas',
        'D': 'Análisis geográfico (departamentos/ciudades) - Espera: barras_horizontales o torta',
        'E': 'Dependencias/Cadenas - Espera: barras_horizontales',
        'F': 'Tiendas - Espera: barras_horizontales',
        'G': 'Línea de producto - Espera: barras_verticales o barras_horizontales',
        'H': 'Producto/Descripción - Espera: barras_verticales',
        'I': 'Referencias - Espera: barras_horizontales',
        'J': 'Tallas - Espera: barras_verticales',
        'K': 'Evolución temporal - Espera: linea',
        'L': 'Estado de tiendas - Espera: barras_verticales',
        'M': 'Alertas - NO requiere gráfico',
        'N': 'Anexo de datos - NO requiere gráfico',
    }
    
    for bloque in bloques_activos:
        desc = bloque_descripciones.get(bloque, 'Desconocido')
        mapeo_info += f"- Bloque {bloque}: {desc}\n"

    prompt = f"""Resumen de datos disponibles (columnas exactas):
{json.dumps(resumen_columnas, ensure_ascii=False, indent=2)}

Bloques del informe: {bloques_activos}

{mapeo_info}

Pregunta del usuario: "{pregunta}"

INSTRUCCIONES CRÍTICAS:
1. Genera SOLO gráficos para consultas cuyo contenido CORRESPONDA al bloque destino
2. NO generes gráficos que muestren datos FUERA DE CONTEXTO del bloque
   - Ej: NO pongas gráfico de LINEA en bloque D (Geográfico)
   - Ej: NO pongas gráfico de DEPARTAMENTOS en bloque I (Referencias)
3. Vincula explícitamente cada gráfico con su consulta y bloque correcto
4. Si una consulta no casa con ningún bloque, NO generes gráfico para ella
5. Si la información ya está completamente mostrada en tabla, considera si el gráfico aporta valor

Decide qué gráficos generar (máximo 1 por bloque, máximo 4 en total).
Responde SOLO con un JSON válido (incluso si es lista vacía).

REGLA CRÍTICA - SELECCIÓN DE TIPO DE GRÁFICO:

**1. Comparación de Períodos Múltiples (MÁXIMA PRIORIDAD):**
   - Si la SQL contiene "CASE WHEN" con BETWEEN para múltiples períodos (enero, febrero, etc.)
   - O la pregunta dice "compara", "versus", "vs", "diferencia entre"
   - ENTONCES: tipo = "barras_agrupadas" (NUNCA "linea" aunque eje X sea temporal)
   - Las barras_agrupadas muestran lado-a-lado (ej: enero/febrero para cada día)

**2. Eje X Temporal (SIN comparación de períodos):**
   - Si columna_x tiene fechas, días, semanas o meses (Fecha, dia, fecha_mvto, etc.)
   - Y NO es comparación de múltiples períodos
   - ENTONCES: tipo = "linea"

**3. Ranking o Distribución Categórica:**
   - Si eje X son categorías (departamentos, tiendas, tallas, referencias)
   - ENTONCES: tipo = "barras_horizontales" (ranking) o "barras_verticales" (distribución)

REGLA PRIORITARIA - MAPEO DE BLOQUES:
Antes de generar un gráfico, verifica que:
1. El bloque_destino está en {bloques_activos}
2. El tipo de gráfico coincide con el esperado para ese bloque
3. Si el mapeo dice "NO requiere gráfico", NO generes uno para ese bloque
4. La consulta_origen tiene sentido para el bloque_destino

Tipos de gráficos disponibles:
{skill_graficos}

Formato esperado:
[
  {{
    "nombre": "identificador_unico",
    "tipo": "barras_horizontales",
    "titulo": "Titulo que cuente la historia (ej: 'Antioquia lidera con 34.6K unidades')",
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

- "bloque_destino": letra del bloque donde irá insertada la imagen (debe estar en {bloques_activos})
- "columna_serie": nombre de columna para series (barras_agrupadas) o null
- "formato_y": "moneda" | "unidades" | "porcentaje"
- Si no hay gráficos que valgan la pena, responde: []
"""

    print(f'  Enviando prompt con {len(resumen_columnas)} consultas disponibles...')
    respuesta = llamar_llm(skill_graficos, prompt, temperatura=0.2)
    print(f'  Respuesta del LLM (primeros 300 chars): {respuesta[:300]}')

    bloque_json = re.search(r'\[.*\]', respuesta, re.DOTALL)
    if not bloque_json:
        print('  ⚠ No se pudo parsear JSON de gráficos.')
        return {}

    try:
        specs = json.loads(bloque_json.group(0))
    except json.JSONDecodeError as e:
        print(f'  ⚠ Error al decodificar JSON de gráficos: {e}')
        return {}

    if not isinstance(specs, list):
        print('  ⚠ JSON no es una lista.')
        return {}
    
    if not specs:
        print('  ℹ No se generarán gráficos (lista vacía).')
        return {}

    import sys as _sys
    _sys.path.insert(0, str(BASE_DIR))
    from tools.generar_grafico import generar_grafico

    charts_dir = BASE_DIR / 'reports' / 'charts'
    imagenes_por_bloque = {}
    
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
            print(f'  ⚠ Saltando "{nombre}": consulta "{consulta_origen}" no encontrada.')
            continue

        if bloque_destino not in bloques_activos:
            print(f'  ⚠ Saltando "{nombre}": bloque {bloque_destino} no está en el informe.')
            continue
        
        # VALIDACIÓN CRÍTICA: verificar que el tipo de gráfico sea válido para el bloque
        tipos_permitidos = MAPEO_BLOQUES_GRAFICOS.get(bloque_destino)
        if tipos_permitidos is None:
            print(f'  ⚠ Saltando "{nombre}": Bloque {bloque_destino} NO requiere gráfico.')
            continue
        
        # Si tipos_permitidos es una lista, verificar que tipo esté en la lista
        if isinstance(tipos_permitidos, list):
            if tipo not in tipos_permitidos:
                print(f'  ⚠ Saltando "{nombre}": tipo "{tipo}" NO permitido para bloque {bloque_destino}. Permitidos: {tipos_permitidos}')
                continue
        else:
            # Si es un string, debe coincidir exactamente
            if tipo != tipos_permitidos:
                print(f'  ⚠ Saltando "{nombre}": tipo "{tipo}" NO coincide con tipo esperado "{tipos_permitidos}" para bloque {bloque_destino}.')
                continue

        data_cols = resultados[consulta_origen].get('columns', [])
        data_rows = resultados[consulta_origen].get('rows', [])
        if not data_cols or not data_rows:
            print(f'  ⚠ Saltando "{nombre}": datos vacíos.')
            continue

        # Buscar columna_x y columna_y ignorando mayúsculas/minúsculas
        col_x_real = _match_col(col_x, data_cols)
        col_y_real = _match_col(col_y, data_cols)

        datos_graf = []
        
        # CASO ESPECIAL: barras_agrupadas transforma datos
        if tipo == 'barras_agrupadas' and col_serie:
            # Para barras agrupadas, necesitamos crear 2 puntos de datos por fila:
            # Uno para col_y (serie 1) y otro para col_serie (serie 2)
            col_s_real = _match_col(col_serie, data_cols)
            for row in data_rows:
                row_dict = dict(zip(data_cols, row))
                x_val = str(row_dict.get(col_x_real or col_x, ''))
                y_val = float(row_dict.get(col_y_real or col_y, 0) or 0)
                serie_val = float(row_dict.get(col_s_real or col_serie, 0) or 0)
                
                # Punto 1: col_y con su nombre como serie
                datos_graf.append({
                    'x': x_val,
                    'y': y_val,
                    'serie': col_y_real or col_y  # Nombre de la columna como label de serie
                })
                # Punto 2: col_serie con su nombre como serie
                datos_graf.append({
                    'x': x_val,
                    'y': serie_val,
                    'serie': col_s_real or col_serie  # Nombre de la columna como label de serie
                })
        else:
            # CASO NORMAL: una fila = un punto
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
            print(f'  ⚠ Saltando "{nombre}": datos transformados vacíos.')
            continue

        print(f'  ✓ Generando Bloque {bloque_destino}: {titulo} ({len(datos_graf)} pts)...')
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
            
            # Almacenar por bloque
            if bloque_destino not in imagenes_por_bloque:
                imagenes_por_bloque[bloque_destino] = []
            imagenes_por_bloque[bloque_destino].append(img_md)
            print(f'      ✓ OK: {ruta_rel}')
        else:
            print(f'      ✗ Error: {r["error"]}')

    if imagenes_por_bloque:
        print(f'\n✓ {sum(len(v) for v in imagenes_por_bloque.values())} gráfico(s) generado(s) para {len(imagenes_por_bloque)} bloque(s)')
    
    return imagenes_por_bloque


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
    
    OPTIMIZACION: Envía solo la sección "Mapeo" de la skill graficos_ventas
    en lugar del archivo completo (~80% menos tokens).
    """
    cols = resultado.get('columns', [])
    rows = resultado.get('rows', [])
    if not cols or not rows or len(rows) < 2:
        return []

    # OPTIMIZACION: Extraer solo la sección "Mapeo" en lugar de skill completo
    skill_graficos = extraer_seccion_skill('graficos_ventas', 'Mapeo:')

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

REGLA CRÍTICA DE TIPO Y SERIE:
- Si la columna_x contiene fechas, días, semanas, meses o "Dia" (temporal):
  Y hay múltiples columnas numéricas con sufijos de año (ej: Valor_2025, Valor_2026):
  ENTONCES tipo = "linea" y columna_serie = null
  (El código transformará automáticamente las columnas numéricas en series para graficar líneas superpuestas)
  
- Si la pregunta contiene palabras como "compara", "versus", "vs", "diferencia"
  Y los datos tienen múltiples columnas numéricas (ej: Ventas_Enero, Ventas_Febrero)
  Y la columna_x NO es temporal:
  ENTONCES tipo = "barras_agrupadas" y columna_serie = nombre de la serie
  Esto crea barras lado-a-lado para comparar valores entre series.
  
- Si columna_x son categorías (departamentos, tiendas, tallas, referencias):
  ENTONCES tipo = "barras_horizontales" o "barras_verticales" y columna_serie = null

Disponible la siguiente guia de tipos de graficos:
{skill_graficos}

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
- "columna_serie": nombre de otra columna numérica (para barras_agrupadas) o null
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
        col_serie = spec.get('columna_serie')

        data_cols = cols
        data_rows = rows

        col_x_real = _match_col(col_x, data_cols)
        col_y_real = _match_col(col_y, data_cols)

        datos_graf = []
        
        # CASO ESPECIAL 1: linea con múltiples columnas numéricas (tendencia comparativa)
        # Detecta si hay columnas con sufijos de año (ej: Valor_2025, Valor_2026)
        if tipo == 'linea':
            # Crear diccionarios de filas para acceso fácil
            filas_dict = [dict(zip(data_cols, row)) for row in data_rows]
            
            # Extraer todas las columnas numéricas (excepto la columna X)
            columnas_numericas = []
            for col in data_cols:
                if col == col_x_real or col == col_x:
                    continue
                # Verificar si la columna tiene valores numéricos
                es_numerica = all(
                    isinstance(fila_dict.get(col), (int, float)) or
                    (isinstance(fila_dict.get(col), str) and 
                     (str(fila_dict.get(col)).replace('.', '').replace(',', '').isdigit() or str(fila_dict.get(col)) == ''))
                    for fila_dict in filas_dict
                )
                if es_numerica:
                    columnas_numericas.append(col)
            
            # Si hay múltiples columnas numéricas (comparación), usar cada una como serie
            if len(columnas_numericas) > 1:
                for fila_dict in filas_dict:
                    x_val = str(fila_dict.get(col_x_real or col_x, ''))
                    
                    for col_num in columnas_numericas:
                        y_val = float(fila_dict.get(col_num, 0) or 0)
                        datos_graf.append({
                            'x': x_val,
                            'y': y_val,
                            'serie': col_num  # Nombre de la columna como leyenda
                        })
            else:
                # Una sola columna numérica — comportamiento normal
                for fila_dict in filas_dict:
                    datos_graf.append({
                        'x': str(fila_dict.get(col_x_real or col_x, '')),
                        'y': float(fila_dict.get(col_y_real or col_y, 0) or 0),
                    })
        
        # CASO ESPECIAL 2: barras_agrupadas transforma datos
        elif tipo == 'barras_agrupadas' and col_serie:
            # Para barras agrupadas, necesitamos crear 2 puntos de datos por fila:
            # Uno para col_y (serie 1) y otro para col_serie (serie 2)
            col_s_real = _match_col(col_serie, data_cols)
            for row in data_rows:
                row_dict = dict(zip(data_cols, row))
                x_val = str(row_dict.get(col_x_real or col_x, ''))
                y_val = float(row_dict.get(col_y_real or col_y, 0) or 0)
                serie_val = float(row_dict.get(col_s_real or col_serie, 0) or 0)
                
                # Punto 1: col_y con su nombre como serie
                datos_graf.append({
                    'x': x_val,
                    'y': y_val,
                    'serie': col_y_real or col_y  # Nombre de la columna como label de serie
                })
                # Punto 2: col_serie con su nombre como serie
                datos_graf.append({
                    'x': x_val,
                    'y': serie_val,
                    'serie': col_s_real or col_serie  # Nombre de la columna como label de serie
                })
        else:
            # CASO NORMAL: una fila = un punto
            for row in data_rows:
                row_dict = dict(zip(data_cols, row))
                item = {
                    'x': str(row_dict.get(col_x_real or col_x, '')),
                    'y': float(row_dict.get(col_y_real or col_y, 0) or 0),
                }
                # Agregar serie si está especificada (para barras_agrupadas)
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

    skill_informe_planning  = leer_skill_bloques_resumen('informe_ventas')
    # OPTIMIZACION: antes se cargaba el skill completo (~3.8K palabras) como
    # system prompt de FASE 4, duplicando la seccion 'Bloques disponibles'
    # que ya se envia por separado via bloques_instructions (linea ~1500) y
    # arrastrando secciones de la fase de planificacion (Filosofia de uso,
    # Orden de bloques, Ejemplo de lectura de intencion) que no aportan a la
    # redaccion. Ahora solo se cargan las dos secciones que si son
    # necesarias para redactar: convenciones de datos y reglas de formato.
    skill_informe_redaccion = cargar_secciones_skill('informe_ventas', [
        'Reglas de negocio (siempre aplican)',
        'REGLAS DE FORMATO MARKDOWN — OBLIGATORIAS',
    ])

    system_gen = cargar_system_prompt('generador_consultas.md', ['Instrucciones (system prompt)']) + _reglas_gen()
    system_val = cargar_system_prompt('validador.md', ['Instrucciones (system prompt)']) + _reglas_val()

    # ------------------------------------------------------------------
    # FASE 1: el LLM decide que consultas necesita segun la peticion
    # ------------------------------------------------------------------
    prompt_planificacion = f"""El usuario quiere generar un informe con la siguiente peticion:
"{pregunta}"

Tienes disponible la siguiente skill de informes que define los bloques disponibles:
{skill_informe_planning}

### Mapeo obligatorio bloque → columna SQL
Para cada bloque, DEBES usar estas columnas exactas al generar la SQL:
- Bloque I (Referencia): SELECT TRIM("REFERENCIA"), TRIM("LINEA_NORM") AS "LINEA", TRIM("GRUPO_NORM") AS "GRUPO" con GROUP BY TRIM("REFERENCIA"), TRIM("LINEA_NORM"), TRIM("GRUPO_NORM") — incluir LINEA y GRUPO en lugar de DESC_ITEM (usar las columnas normalizadas de ventas_unificada, nunca LINEA/GRUPO crudas)
- Bloque J (Talla): GROUP BY TRIM("TALLA") WHERE TRIM("TALLA") ~ '^(XS|S|M|L|XL|XXL|[0-9]{1,2}|[0-9]{1,2}[WLT])$'
- Bloque G (Linea): GROUP BY TRIM("LINEA") — columna exacta: "LINEA"
- Bloque H (Producto): GROUP BY TRIM("DESC_ITEM") — columna exacta: "DESC_ITEM"
- Bloque D (Geografico): GROUP BY UPPER(TRIM("DEPARTAMENTO")) para nivel depto; GROUP BY UPPER(TRIM("CIUDAD")), UPPER(TRIM("DEPARTAMENTO")) para nivel ciudad. Si el informe es de un departamento especifico, filtrar por ese departamento en la consulta de ciudad.
- Bloque E (Dependencias): GROUP BY UPPER(TRIM("DEPENDENCIA")) — columna: "DEPENDENCIA"
- Bloque F (Tiendas): GROUP BY UPPER(TRIM("DESC_DEPENDENCIA")) — columna: "DESC_DEPENDENCIA"
- Bloque K (Evolucion): GROUP BY TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY')

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

- "bloques": lista de letras de bloques que usaras.
- "consultas": una entrada por cada consulta SQL necesaria.
- Si el informe es general/completo, incluir consultas para TODOS los bloques relevantes incluyendo I y J.
- Responde SOLO con el JSON, sin texto adicional ni bloques de codigo.
"""

    print(f'[{MODELO}] Planificando consultas segun la peticion...')
    respuesta_plan = llamar_llm(system_gen, prompt_planificacion, temperatura=0.1)

    # Extraer JSON de la respuesta — usar decoder que encuentra el primer objeto valido
    plan = None
    try:
        decoder = json.JSONDecoder()
        idx = respuesta_plan.find('{')
        if idx != -1:
            plan, _ = decoder.raw_decode(respuesta_plan, idx)
    except (json.JSONDecodeError, ValueError):
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
                           FROM ventas_2026
                           WHERE TRIM("DESC_MOVIMIENTO") = 'VENTAS POS'"""},
            ]
        }

    print(f'Bloques seleccionados: {plan.get("bloques", [])}')
    print(f'Consultas a ejecutar: {len(plan["consultas"])}')
    for q in plan['consultas']:
        print(f'  - {q.get("nombre")}')

    # ------------------------------------------------------------------
    # FASE 2: ejecutar cada consulta del plan (sin validador — el LLM
    # genera SQL correcta y PostgreSQL reporta errores reales si los hay)
    # ------------------------------------------------------------------
    resultados = {}
    sql_queries_ejecutadas = []  # trazabilidad: una entrada por bloque efectivamente ejecutado
    for item in plan['consultas']:
        nombre = item.get('nombre', 'consulta')
        sql_raw = item.get('sql', '').strip()
        if not sql_raw:
            continue

        sql_limpia = extraer_sql(sql_raw) if '```' in sql_raw else sql_raw
        if not sql_limpia.rstrip().endswith(';'):
            sql_limpia += ';'

        print(f'  Ejecutando: {nombre}...')
        print(f'SQL_LOG::{nombre}::{sql_limpia}')
        print('SQL_LOG_END')
        resultado = ejecutar_consulta(sql_limpia, limite=500)
        if resultado.get('success'):
            resultados[nombre] = resultado
            sql_queries_ejecutadas.append({'nombre': f'informe_bloque_{nombre}', 'sql': sql_limpia})
            print(f'  OK: {resultado["total_filas"]} fila(s)')
        else:
            # Un error real de PostgreSQL — intentar corregir UNA vez con el LLM
            error_pg = resultado.get('error', '')
            print(f'  Error PostgreSQL: {error_pg}. Intentando correccion...')
            correccion = llamar_llm(
                system_gen,
                f'La siguiente SQL produjo un error en PostgreSQL:\n\nSQL:\n{sql_limpia}\n\nError:\n{error_pg}\n\nCorrige la SQL para la consulta "{nombre}". Responde solo con el SQL corregido.',
                temperatura=0.1,
            )
            sql_corregida = extraer_sql(correccion)
            resultado2 = ejecutar_consulta(sql_corregida, limite=500)
            if resultado2.get('success'):
                resultados[nombre] = resultado2
                sql_queries_ejecutadas.append({'nombre': f'informe_bloque_{nombre}_corregida', 'sql': sql_corregida})
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
    
    # OPTIMIZACION: delay antes de FASE 4 para respetar rate limits
    import time
    print('  ⏳ Esperando antes de redacción (respetar rate limits)...')
    time.sleep(2)
    
    # Formatear resultados como tablas si es necesario
    tablas_markdown = {}
    for nombre, res in resultados.items():
        if res.get('rows'):
            tabla = formatear_resultado_como_tabla(res, limite=50)
            if tabla:
                tablas_markdown[nombre] = tabla
    
    # OPTIMIZACION: Extraer solo instrucciones de bloques en lugar de skill completo
    # Esto reduce tokens de ~10K a ~2K
    bloques_instructions = extraer_seccion_skill('informe_ventas', 'BLOQUE')
    
    prompt_redaccion = f"""El usuario solicito:
"{pregunta}"

Genera un informe profesional en Markdown usando SOLO los bloques seleccionados.

Bloques a incluir: {plan.get('bloques', [])}

Referencia de estructura de bloques:
{bloques_instructions}

Datos obtenidos (resumido):
- Consultas ejecutadas: {', '.join(resultados.keys())}
- Fecha: {datetime.now().strftime('%d/%m/%Y')}

REGLAS CRÍTICAS DE REDACCIÓN:

1. **NUNCA incluyas nombres de bloques en el texto.** No escribas "BLOQUE C" o "BLOQUE D".
   - Los títulos deben ser descriptivos: "Métricas Principales", "Análisis Geográfico", etc.
   - Solo incluye el título en formato ## (Markdown H2)

2. **FORMATO DE TABLAS - BLOQUE C (Métricas Principales)**
   - OBLIGATORIO usar tabla con 3 columnas: Métrica | Valor | Nota
   - NUNCA separes las métricas en párrafos
   - Incluye: Unidades totales, Valor total, Línea top, Referencia top, Talla top
   - Formato numérico: separadores de miles, COP donde aplique
   - EJEMPLO CORRECTO:
   ```
   ## Métricas Principales
   
   | Métrica | Valor | Nota |
   |---------|-------|------|
   | Unidades vendidas totales | 8,420 | |
   | Valor total de ventas | $126,300,000 | |
   | Línea más vendida | Dama Exterior | 2,150 unidades |
   | Referencia top | [Ref.] | [cantidad] |
   | Talla más vendida | [Talla] | [cantidad] |
   ```

3. **Estructura general:**
   - Construye SOLO los bloques seleccionados en orden lógico
   - Adapta títulos y contenido a la pregunta exacta del usuario
   - Si un bloque no tiene datos, omítelo o indica "Sin información"
   - Sé conciso: máximo 2-3 párrafos por sección (excepto tablas)

4. **Números y formato:**
   - Separador de miles: 54,616 o $54,616 COP
   - Porcentajes: 1 decimal (23.5%)
   - Unidades: sin  mbolo $ (8,420 unidades)
"""
    
    if tablas_markdown:
        prompt_redaccion += f"""

### Tablas de Resultados
Inserta estas tablas markdown DEBAJO del título de cada bloque:
{json.dumps(tablas_markdown, ensure_ascii=False, indent=2)}
"""
    if imagenes_por_bloque:
        prompt_redaccion += f"""

### GRAFICOS GENERADOS — INSTRUCCIONES DE INSERCIÓN
Estos gráficos DEBEN ser insertados en los bloques correspondientes.
Formato: inserta la imagen markdown INMEDIATAMENTE DESPUÉS del título del bloque 
y de cualquier tabla de datos, pero ANTES del texto explicativo.

Gráficos por bloque (cada bloque tiene 0 o más gráficos):
{json.dumps(imagenes_por_bloque, ensure_ascii=False, indent=2)}

REGLA DE INSERCIÓN:
1. Si el bloque tiene un gráfico asignado, inclúyelo con el formato: ![Titulo](ruta)
2. Posición: DESPUÉS de la tabla de datos (si la hay), ANTES del texto
3. SIEMPRE incluye un párrafo explicativo después del gráfico que describa QUÉ muestra
4. Si la inserción de imagen falla, menciona: "No fue posible mostrar el gráfico para [bloque]"

Ejemplo correcto para Bloque D:
```
## BLOQUE D — Análisis Geográfico

| Departamento | Unidades |
|---|---:|
| ANTIOQUIA | 34,615 |

![Departamentos lideran con Antioquia](reports/charts/..png)

El gráfico anterior muestra que Antioquia domina...
```
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

    # Retornar dict estructurado para el servidor
    return {
        'tipo':            'informe',
        'respuesta':       md_informe,
        'ruta_md':         str(ruta_md),
        'ruta_docx':       str(ruta_docx),
        'bloques':         plan.get('bloques', []),
        'periodo':         '',   # el servidor puede extraerlo del md si necesita
        'resultado_sql':   None, # informes no exponen un único df
        'sql_usada':       None,
        'sql_queries':     sql_queries_ejecutadas,  # una entrada por bloque ejecutado
        'imagenes':        [],
        'ruta_excel':      '',
        'analisis':        None,
    }

# ---------------------------------------------------------------------------
# Clasificador de intención analítica
# ---------------------------------------------------------------------------

PATRON_ANALISIS = re.compile(
    r'\b(analiz[a-z]*|explica[a-z]*|por\s+qu[eé]|a\s+qu[eé]\s+se\s+debe|'
    r'raz[oó]n|causa[s]?|investiga[a-z]*|profundiz[a-z]*|qu[eé]\s+pas[oó]|'
    r'qu[eé]\s+ocurri[oó]|c[oó]mo\s+es\s+posible|interpreta[a-z]*|'
    r'qu[eé]\s+factores|qu[eé]\s+lo\s+explica|diagnostica[a-z]*)\b',
    re.IGNORECASE,
)

def _es_comando_analisis(pregunta: str) -> tuple[bool, str]:
    """
    Detecta si la pregunta empieza con /analisis.
    Retorna (es_comando, pregunta_limpia).
    """
    limpia = pregunta.strip()
    if re.match(r'^/analisis\b', limpia, re.IGNORECASE):
        prompt_limpio = re.sub(r'^/analisis\s*', '', limpia, flags=re.IGNORECASE).strip()
        return True, prompt_limpio
    return False, limpia


def _clasificar_intencion_regex(pregunta: str) -> bool:
    """Detecta intención analítica via regex."""
    return bool(PATRON_ANALISIS.search(pregunta))


def _clasificar_intencion_llm(pregunta: str) -> bool:
    """
    Detecta intención analítica usando llama-3.1-8b-instant via Groq.
    Retorna True si la pregunta requiere análisis profundo.
    """
    if _client_inference is None:
        print('  [clasificador] cliente Groq no disponible, usando regex como fallback')
        return _clasificar_intencion_regex(pregunta)

    system = (
        'Eres un clasificador binario. Determina si la pregunta del usuario '
        'requiere análisis profundo de datos (buscar causas, relaciones, patrones, '
        'explicaciones del por qué) o si es solo una consulta de datos simple '
        '(un número, un listado, un conteo).\n'
        'Responde ÚNICAMENTE con una sola palabra: SI o NO.\n'
        'SI = requiere análisis profundo.\n'
        'NO = consulta simple de datos.'
    )
    try:
        resp = _client_inference.chat.completions.create(
            model=GROQ_MODEL_INFERENCE,
            messages=[
                {'role': 'system', 'content': system},
                {'role': 'user',   'content': pregunta},
            ],
            temperature=0.0,
            max_tokens=300,
            reasoning_effort='low',
        )
        resultado = resp.choices[0].message.content.strip().upper()
        print(f'  [clasificador LLM] respuesta: {resultado}')
        return resultado.startswith('SI') or resultado == 'SÍ'
    except Exception as e:
        print(f'  [clasificador LLM] error: {e} — usando regex como fallback')
        return _clasificar_intencion_regex(pregunta)


def es_intencion_analisis(pregunta: str, resultado: dict | None = None) -> bool:
    """
    Decide si activar el agente analista.
    - CLASSIFY_INFERENCE=YES → usa llama-3.1-8b (confía sin filtro de tamaño)
    - CLASSIFY_INFERENCE=NO  → usa regex + filtro de tamaño (>5 filas, >1 col numérica)
    """
    if CLASSIFY_INFERENCE == 'YES':
        return _clasificar_intencion_llm(pregunta)
    else:
        if not _clasificar_intencion_regex(pregunta):
            return False
        # Filtro de tamaño: solo activar si hay suficientes datos
        if resultado:
            filas = resultado.get('total_filas', 0)
            cols  = resultado.get('columns', [])
            # Contar columnas numéricas
            rows = resultado.get('rows', [])
            cols_numericas = 0
            if rows:
                primera_fila = dict(zip(cols, rows[0]))
                cols_numericas = sum(
                    1 for v in primera_fila.values()
                    if isinstance(v, (int, float))
                )
            if filas <= 5 or cols_numericas <= 1:
                print(f'  [clasificador regex] intención detectada pero datos insuficientes ({filas} filas, {cols_numericas} cols numéricas) — omitiendo analista')
                return False
        return True


# ---------------------------------------------------------------------------
# Pre-cómputo de métricas (Python puro, sin LLM)
# ---------------------------------------------------------------------------

def _a_float_seguro(v):
    """Convierte a float si v es numérico o un string numérico. Excluye bool
    (columnas booleanas como TIENE_NORM no son métricas agregables) y NaN
    (pandas lo usa para representar nulos en columnas float — sin este guard,
    un NaN llega hasta statistics.stdev() y lo hace fallar con
    AttributeError en vez de un error manejable)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and math.isnan(v):
            return None
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().replace(',', ''))
        except ValueError:
            return None
    return None


def pre_computar_metricas(resultado: dict) -> dict:
    """
    Calcula métricas derivadas sobre el resultado SQL antes de enviarlo al analista.
    El analista recibe hechos ya calculados, no solo números crudos.

    Retorna dict con:
      - totales: suma de cada columna numérica
      - min_max: valor mínimo y máximo con etiqueta compuesta de fila
      - top3_concentracion_pct: % que acumulan las 3 primeras filas
      - concentracion_por_dimension: % agregado por CADA columna categórica de
        cardinalidad razonable (no solo la primera) — ej. en un resultado
        zona x categoría, da la distribución completa por zona Y por
        categoría por separado, aunque el resultado tenga cientos de filas.
      - variaciones_pct: cambio % entre filas consecutivas
      - tendencia_variaciones: 'creciente' | 'decreciente' | 'volatil' | 'estable'
      - outliers: filas con valor > media ± 2*std
      - gaps_valor_cero: categorías con valor 0
      - ratios_cruzados: PVP promedio ponderado si hay columnas CANTIDAD y VALOR
    """
    import statistics

    cols = resultado.get('columns', [])
    rows = resultado.get('rows', [])

    if not cols or not rows:
        return {}

    # Identificar columnas numéricas y categóricas. consultar_db.py ya preserva
    # tipos nativos (int/float), pero se mantiene un fallback de coerción por
    # si otra ruta vuelve a stringificar los valores.
    primera = dict(zip(cols, rows[0]))
    cols_num       = [c for c, v in primera.items() if _a_float_seguro(v) is not None]
    cols_cat_todas = [c for c, v in primera.items() if isinstance(v, str)]

    if not cols_num:
        return {}

    def _label_fila(row) -> str:
        """Etiqueta compuesta con TODAS las columnas de texto de la fila
        (ej. 'ANTIOQUIA / TEMPLADO / 02 - Camiseta manga corta'), no solo la
        primera — para que min/max/outliers/gaps sigan siendo identificables
        cuando el resultado tiene varias dimensiones categóricas."""
        if not cols_cat_todas:
            return ''
        rd = dict(zip(cols, row))
        partes = [str(rd.get(c, '')).strip() for c in cols_cat_todas]
        return ' / '.join(p for p in partes if p)

    metricas = {}

    # — Construir vectores de valores por columna —
    vectores = {}
    for col in cols_num:
        vals = []
        for row in rows:
            rd = dict(zip(cols, row))
            v  = rd.get(col)
            f  = _a_float_seguro(v)
            vals.append(f if f is not None else 0.0)
        vectores[col] = vals

    for col, valores in vectores.items():
        total = sum(valores)
        n     = len(valores)

        # Totales
        metricas.setdefault('totales', {})[col] = round(total, 2)

        # Min / Max con etiqueta
        idx_max = valores.index(max(valores))
        idx_min = valores.index(min(valores))
        label_max = _label_fila(rows[idx_max]) or str(idx_max)
        label_min = _label_fila(rows[idx_min]) or str(idx_min)
        metricas.setdefault('min_max', {})[col] = {
            'max': {'valor': round(max(valores), 2), 'en': label_max},
            'min': {'valor': round(min(valores), 2), 'en': label_min},
        }

        # Concentración top-3
        if total > 0 and n >= 3:
            top3_suma = sum(sorted(valores, reverse=True)[:3])
            metricas.setdefault('top3_concentracion_pct', {})[col] = round(top3_suma / total * 100, 1)

        # Concentración por CADA dimensión categórica (agregando por esa
        # dimensión, no por fila cruda) — en un resultado zona x categoría
        # esto da la distribución completa por zona Y por categoría por
        # separado, aunque el resultado tenga cientos de filas y ninguna
        # combinación individual represente bien a su zona o su categoría.
        if total > 0:
            for col_dim in cols_cat_todas:
                agregados: dict[str, float] = {}
                for i, v in enumerate(valores):
                    rd = dict(zip(cols, rows[i]))
                    clave = str(rd.get(col_dim, '')).strip() or 'N/A'
                    agregados[clave] = agregados.get(clave, 0.0) + v
                n_grupos = len(agregados)
                # Ni casi-constante (1 grupo) ni de alta cardinalidad (ej.
                # REFERENCIA con cientos de valores) — entre 2 y 60 grupos.
                if n_grupos < 2 or n_grupos > 60:
                    continue
                dist = [
                    {'categoria': k, 'valor': round(val, 2), 'pct': round(val / total * 100, 1)}
                    for k, val in agregados.items()
                ]
                pcts = [d['pct'] for d in dist]
                if max(pcts) - min(pcts) <= 2:
                    continue  # sin variación significativa entre grupos
                metricas.setdefault('concentracion_por_dimension', {}).setdefault(col, {})[col_dim] = sorted(
                    dist, key=lambda x: x['valor'], reverse=True
                )[:15]

        # Variaciones % entre filas consecutivas. Solo tiene sentido como
        # "tendencia" si hay a lo sumo 1 dimensión categórica — con 2+ (ej.
        # zona x categoría), la fila i y la i+1 suelen ser combinaciones sin
        # relación real entre sí, y una "tendencia decreciente" ahí sería una
        # lectura falsa del orden en que las devolvió el SQL.
        variaciones_pct_vals = []
        if n >= 2 and len(cols_cat_todas) <= 1:
            variaciones = []
            for i in range(1, n):
                ant = valores[i - 1]
                act = valores[i]
                pct = round((act - ant) / abs(ant) * 100, 1) if ant != 0 else None
                label = _label_fila(rows[i]) or str(i)
                variaciones.append({'en': label, 'variacion_pct': pct})
                if pct is not None:
                    variaciones_pct_vals.append(pct)
            metricas.setdefault('variaciones_pct', {})[col] = variaciones

        # Tendencia de las variaciones
        if len(variaciones_pct_vals) >= 3:
            positivas = sum(1 for v in variaciones_pct_vals if v > 0)
            negativas = sum(1 for v in variaciones_pct_vals if v < 0)
            total_var = len(variaciones_pct_vals)
            if positivas / total_var >= 0.75:
                tendencia = 'creciente'
            elif negativas / total_var >= 0.75:
                tendencia = 'decreciente'
            else:
                # Medir volatilidad: std de las variaciones
                try:
                    std_var = statistics.stdev(variaciones_pct_vals)
                    tendencia = 'volatil' if std_var > 20 else 'estable'
                except (statistics.StatisticsError, AttributeError, ArithmeticError):
                    # statistics.stdev() puede fallar con AttributeError ante
                    # ciertos valores float extremos (bug conocido de CPython
                    # en su camino de cálculo exacto vía Fraction) — esto es
                    # una métrica derivada, no debe tumbar el pipeline.
                    tendencia = 'estable'
            metricas.setdefault('tendencia_variaciones', {})[col] = tendencia

        # Outliers: filas con valor > media ± 2*std
        if n >= 4:
            try:
                media = statistics.mean(valores)
                std   = statistics.stdev(valores)
                outs  = []
                for i, v in enumerate(valores):
                    if std > 0 and abs(v - media) > 2 * std:
                        label = _label_fila(rows[i]) or str(i)
                        outs.append({
                            'en':               label,
                            'valor':            round(v, 2),
                            'desviaciones_std': round((v - media) / std, 1),
                            'direccion':        'alto' if v > media else 'bajo',
                        })
                if outs:
                    metricas.setdefault('outliers', {})[col] = outs
            except (statistics.StatisticsError, AttributeError, ArithmeticError):
                pass

        # Gaps: posiciones con valor 0
        gaps = []
        for i, v in enumerate(valores):
            if v == 0:
                label = _label_fila(rows[i]) or str(i)
                gaps.append(label)
        if gaps:
            metricas.setdefault('gaps_valor_cero', {})[col] = gaps[:20]  # cap para no saturar

    # — Ratios cruzados entre columnas numéricas —
    # PVP promedio ponderado: si hay columna de CANTIDAD y columna de VALOR
    cols_num_lower = {c.lower(): c for c in cols_num}
    col_cantidad = next(
        (v for k, v in cols_num_lower.items()
         if any(x in k for x in ('cantidad', 'unidades', 'uds'))),
        None
    )
    col_valor = next(
        (v for k, v in cols_num_lower.items()
         if any(x in k for x in ('valor', 'venta', 'cop', 'ingreso', 'total'))),
        None
    )
    if col_cantidad and col_valor and col_cantidad != col_valor:
        suma_cant  = sum(vectores[col_cantidad])
        suma_valor = sum(vectores[col_valor])
        if suma_cant > 0:
            pvp_prom = round(suma_valor / suma_cant, 0)
            metricas['pvp_promedio_ponderado'] = {
                'valor':        pvp_prom,
                'interpretacion': (
                    'precio promedio por unidad vendida — '
                    'caída indica descuentos o mix hacia referencias más baratas; '
                    'alza indica referencias premium o reducción de descuentos'
                ),
            }

    # Ratio de dos columnas numéricas si hay exactamente 2 y tienen nombres sugerentes
    # ej: cambios/ventas = tasa devolución
    if len(cols_num) == 2:
        c1, c2 = cols_num
        c1l, c2l = c1.lower(), c2.lower()
        es_devolucion = (
            any(x in c1l for x in ('cambio', 'devol', 'retorno')) or
            any(x in c2l for x in ('cambio', 'devol', 'retorno'))
        )
        es_venta = (
            any(x in c1l for x in ('venta', 'pos', 'unid')) or
            any(x in c2l for x in ('venta', 'pos', 'unid'))
        )
        if es_devolucion and es_venta:
            total_c1 = sum(vectores[c1])
            total_c2 = sum(vectores[c2])
            denominador = total_c2 if 'venta' in c2l or 'pos' in c2l else total_c1
            numerador   = total_c1 if denominador == total_c2 else total_c2
            if denominador > 0:
                tasa = round(numerador / denominador * 100, 2)
                metricas['tasa_devolucion_global'] = {
                    'valor_pct': tasa,
                    'alerta':    tasa > 5.0,
                    'referencia': 'umbral de alerta: >5%. Tasa global historica: ~2.5%',
                }

    return metricas


# ---------------------------------------------------------------------------
# Agente analista con loop de consultas complementarias
# ---------------------------------------------------------------------------

MAX_RONDAS_ANALISTA = 3


def _sql_queries_desde_acumulados(datos_acumulados: list[dict]) -> list[dict]:
    """Traduce datos_acumulados (rondas del analista) a la lista plana
    {"nombre", "sql"} que espera prompt_logger, para trazabilidad completa
    de todas las consultas que el analista disparo (no solo la inicial)."""
    queries = []
    for d in datos_acumulados:
        if d['ronda'] == 0:
            nombre = 'consulta_principal'
        else:
            nombre = f"analista_ronda_{d['ronda']}: {d['descripcion']}"
        queries.append({'nombre': nombre, 'sql': d['sql']})
    return queries


def _fusionar_hallazgos(acumulado: dict, parcial: dict) -> None:
    """Acumula patrones/anomalias/hipotesis de un 'analisis_parcial' de una
    ronda intermedia, para que no se pierdan si el analista no los repite en
    la ronda final."""
    for clave in ('patrones', 'anomalias', 'hipotesis'):
        nuevos = parcial.get(clave) or []
        acumulado.setdefault(clave, []).extend(nuevos)


def _ejecutar_consulta_con_reintento(sql: str, system_gen: str) -> tuple[dict, str]:
    """Ejecuta una SQL; si Postgres devuelve un error real de ejecución,
    reintenta UNA vez reinyectando el mensaje de error al generador (mismo
    patrón que generar_informe usa para sus bloques). No reintenta ante
    otro tipo de fallo (guardia de solo-lectura, timeout/crash del
    subprocess) — en esos casos no hay nada en la SQL que "corregir" y
    reintentar solo desperdicia el único intento disponible. El llamador
    decide si descarta la consulta cuando el resultado final sigue fallando."""
    resultado = ejecutar_consulta(sql)
    if resultado.get('success') or resultado.get('error_fuente') != 'postgres':
        return resultado, sql

    error_pg = resultado.get('error', '')
    print(f'  Error de Postgres: {error_pg} — reintentando con corrección...')
    correccion = llamar_llm(
        system_gen,
        f'La siguiente SQL produjo un error en PostgreSQL:\n\nSQL:\n{sql}\n\n'
        f'Error:\n{error_pg}\n\nCorrige la SQL. Responde solo con el SQL corregido.',
        temperatura=0.1,
    )
    sql_corregida = extraer_sql(correccion)
    resultado_corregido = ejecutar_consulta(sql_corregida)
    if resultado_corregido.get('success'):
        return resultado_corregido, sql_corregida
    return resultado_corregido, sql_corregida


def agente_analista(
    pregunta: str,
    resultado_inicial: dict,
    sql_inicial: str,
    system_gen: str,
    system_val: str,
    metricas_iniciales: dict = None,
) -> dict:
    """
    Ejecuta el análisis profundo sobre los datos.
    Puede pedir hasta MAX_RONDAS_ANALISTA rondas de consultas adicionales al
    generador (hasta 3 consultas por ronda, ejecutadas secuencialmente).

    Retorna el JSON estructurado final del analista:
    {
      "estado": "completo",
      "patrones": [{"afirmacion","evidencia","ronda","confianza"}, ...],
      "anomalias": [...],
      "hipotesis": [...],
      "datos_usados": [...],
      "conclusion": "...",
      "preguntas_sugeridas": [...]
    }
    """
    system_analista = cargar_system_prompt('analista.md', ['Instrucciones (system prompt)'])

    # Acumular todos los datos a través de las rondas. Cada entrada guarda su
    # propia 'metricas' (pre-computada una sola vez, no en cada iteración).
    datos_acumulados = [
        {
            'ronda':       0,
            'descripcion': 'Consulta inicial del usuario',
            'sql':         sql_inicial,
            'resultado':   resultado_inicial,
            # Si el llamador ya calculó las métricas sobre este mismo
            # resultado (ej. procesar_consulta las necesita también para el
            # digest del redactor), se reutilizan en vez de recalcular.
            'metricas':    metricas_iniciales if metricas_iniciales is not None else pre_computar_metricas(resultado_inicial),
        }
    ]
    # Hallazgos de 'analisis_parcial' en rondas intermedias que el analista
    # ya derivó — se reinyectan para que no se pierdan ni se re-deriven.
    hallazgos_acumulados: dict = {}

    for ronda in range(MAX_RONDAS_ANALISTA + 1):
        # Construir prompt para el analista. Contexto lineal, no cuadrático:
        # solo la ronda más reciente lleva filas crudas; las anteriores solo
        # su descripción + SQL + métricas ya calculadas.
        prompt = f"""Pregunta original del usuario: "{pregunta}"

=== DATOS DISPONIBLES ===

"""
        for d in datos_acumulados:
            es_reciente = d['ronda'] == ronda
            prompt += f"--- Ronda {d['ronda']}: {d['descripcion']} ---\n"
            prompt += f"SQL usado:\n{d['sql']}\n\n"
            if es_reciente:
                prompt += f"Resultado ({d['resultado'].get('total_filas', 0)} filas):\n"
                prompt += json.dumps({
                    'columns': d['resultado'].get('columns', []),
                    'rows':    d['resultado'].get('rows', [])[:50],  # máx 50 filas al LLM
                }, ensure_ascii=False, indent=2)
                prompt += '\n\nMétricas pre-computadas de esta ronda:\n'
                prompt += json.dumps(d['metricas'], ensure_ascii=False, indent=2)
            else:
                prompt += f"Resultado: {d['resultado'].get('total_filas', 0)} filas (no repetidas aquí — usa las métricas).\n"
                prompt += 'Métricas pre-computadas:\n'
                prompt += json.dumps(d['metricas'], ensure_ascii=False, indent=2)
            prompt += '\n\n'

        if any(hallazgos_acumulados.values()):
            prompt += '=== HALLAZGOS YA DERIVADOS EN RONDAS ANTERIORES (no los repitas) ===\n'
            prompt += json.dumps(hallazgos_acumulados, ensure_ascii=False, indent=2)
            prompt += '\n\n'

        prompt += '=== INSTRUCCIÓN ===\n'
        if ronda >= MAX_RONDAS_ANALISTA:
            prompt += 'Has alcanzado el límite de rondas. Produce el análisis completo con los datos que tienes. Responde con estado "completo".'
        else:
            prompt += (
                f'Ronda {ronda + 1} de {MAX_RONDAS_ANALISTA} posibles. '
                'Analiza los datos. Si necesitas más información, responde con estado "necesita_datos" '
                'y hasta 3 "consultas_adicionales". Si tienes suficiente para un análisis completo, '
                'responde con estado "completo".'
            )

        print(f'  [{MODELO}] Analista — ronda {ronda + 1}...')
        respuesta_raw = llamar_llm(system_analista, prompt, temperatura=0.5)

        # Extraer JSON de la respuesta
        analisis = None
        bloque = re.search(r'\{.*\}', respuesta_raw, re.DOTALL)
        if bloque:
            try:
                analisis = json.loads(bloque.group(0))
            except json.JSONDecodeError:
                pass

        if not analisis:
            print('  [analista] No se pudo parsear JSON. Usando respuesta cruda.')
            return {
                'estado':    'completo',
                'patrones':  hallazgos_acumulados.get('patrones', []),
                'anomalias': hallazgos_acumulados.get('anomalias', []),
                'hipotesis': hallazgos_acumulados.get('hipotesis', []),
                'datos_usados': [{'descripcion': d['descripcion'], 'filas': d['resultado'].get('total_filas', 0), 'columnas': d['resultado'].get('columns', [])} for d in datos_acumulados],
                'conclusion': respuesta_raw,
                'preguntas_sugeridas': [],
                'sql_queries': _sql_queries_desde_acumulados(datos_acumulados),
            }

        estado = analisis.get('estado', 'completo')

        if estado == 'completo' or ronda >= MAX_RONDAS_ANALISTA:
            # Fusionar hallazgos de rondas intermedias que no se hayan repetido
            for clave in ('patrones', 'anomalias', 'hipotesis'):
                previos = hallazgos_acumulados.get(clave, [])
                if previos:
                    analisis[clave] = previos + (analisis.get(clave) or [])
            # Enriquecer con lista de datos usados si no viene incluida
            if 'datos_usados' not in analisis or not analisis['datos_usados']:
                analisis['datos_usados'] = [
                    {
                        'descripcion': d['descripcion'],
                        'filas':       d['resultado'].get('total_filas', 0),
                        'columnas':    d['resultado'].get('columns', []),
                    }
                    for d in datos_acumulados
                ]
            analisis['sql_queries'] = _sql_queries_desde_acumulados(datos_acumulados)
            print(f'  [analista] Análisis completo en ronda {ronda + 1}.')
            return analisis

        # estado == 'necesita_datos' — acumular hallazgos parciales y pedir
        # hasta 3 consultas complementarias, ejecutadas secuencialmente.
        _fusionar_hallazgos(hallazgos_acumulados, analisis.get('analisis_parcial', {}))

        solicitudes = analisis.get('consultas_adicionales') or []
        razon = analisis.get('razon', '')

        if not solicitudes:
            print('  [analista] Pidió datos adicionales pero sin consultas_adicionales. Terminando.')
            analisis['estado'] = 'completo'
            for clave in ('patrones', 'anomalias', 'hipotesis'):
                previos = hallazgos_acumulados.get(clave, [])
                if previos:
                    analisis[clave] = previos + (analisis.get(clave) or [])
            analisis['sql_queries'] = _sql_queries_desde_acumulados(datos_acumulados)
            return analisis

        print(f'  [analista] Solicita {len(solicitudes)} consulta(s) adicional(es) (ronda {ronda + 1}).')
        print(f'             Razón: {razon}')

        for solicitud in solicitudes[:3]:
            pregunta_adicional = solicitud.get('pregunta', '')
            if not pregunta_adicional:
                continue
            contexto_adicional = solicitud.get('contexto', '')

            print(f'    - {pregunta_adicional}')
            prompt_gen_adicional = (
                f'{pregunta_adicional}\n\n'
                f'Contexto adicional para esta consulta: {contexto_adicional}\n'
                f'Columnas que se esperan en el resultado: {json.dumps(solicitud.get("columnas_esperadas", []))}'
            )
            sql_adicional = generar_sql_y_validar(prompt_gen_adicional, system_gen, system_val)
            resultado_adicional, sql_adicional = _ejecutar_consulta_con_reintento(sql_adicional, system_gen)

            if not resultado_adicional.get('success'):
                print(f'    Descartada tras reintento: {resultado_adicional.get("error")}')
                continue

            print(f'    OK: {resultado_adicional.get("total_filas", 0)} filas obtenidas.')
            datos_acumulados.append({
                'ronda':       ronda + 1,
                'descripcion': pregunta_adicional,
                'sql':         sql_adicional,
                'resultado':   resultado_adicional,
                'metricas':    pre_computar_metricas(resultado_adicional),
            })

    # Nunca debería llegar aquí, pero por seguridad:
    return {
        'estado': 'completo', 'conclusion': 'Análisis completado.',
        'patrones':  hallazgos_acumulados.get('patrones', []),
        'anomalias': hallazgos_acumulados.get('anomalias', []),
        'hipotesis': hallazgos_acumulados.get('hipotesis', []),
        'datos_usados': [], 'preguntas_sugeridas': [],
        'sql_queries': _sql_queries_desde_acumulados(datos_acumulados),
    }


def procesar_consulta(
    pregunta: str,
    contexto_refinamiento: dict | None = None,
    permitir_excel_individual: bool = True,
    contexto_web: str = '',
):
    """
    Pipeline principal de consulta.

    Args:
        pregunta: pregunta del usuario en lenguaje natural
        contexto_refinamiento: dict opcional con contexto de sesión para REFINAMIENTO.
            {
              'sql_anterior':      str,   SQL de la consulta previa
              'columnas_previas':  list,  columnas del resultado previo
              'periodo_previo':    str,   período detectado en la consulta previa
              'filtros_previos':   list,  filtros activos de la consulta previa
              'descripcion_df':    str,   descripción del df anterior
            }
        permitir_excel_individual: si False, esta consulta NUNCA exporta su
            propio Excel (ni por umbral de filas ni por intención explícita)
            aunque la pregunta lo pida — usado por server.py::_procesar_multi_consulta,
            que combina el Excel de varias sub-preguntas en un solo archivo de
            varias hojas en vez de dejar que cada una genere el suyo.
        contexto_web: bloque de texto ya formateado con resultados de búsqueda
            web (o '' si no aplica), calculado por server.py ANTES de llamar
            a esta función — procesar_consulta no decide si buscar en internet,
            solo inyecta el resultado en el prompt del redactor si se lo pasan.
            El redactor ya sabe interpretar esta sección (ver regla 7 de
            redactor_respuesta.md, cargada siempre, no solo en modo conversacional).

    Retorna:
        {
          'tipo':         'consulta',
          'respuesta':    str,           texto final para el usuario
          'resultado_sql': dict,         {columns, rows, total_filas}
          'sql_usada':    str,
          'imagenes':     list[str],     rutas markdown de imágenes
          'ruta_excel':   str,
          'analisis':     dict | None,
        }
    """
    system_gen = cargar_system_prompt('generador_consultas.md', ['Instrucciones (system prompt)']) + _reglas_gen()
    system_val = cargar_system_prompt('validador.md', ['Instrucciones (system prompt)']) + _reglas_val()
    # system_red se compone más abajo, una vez se sabe si hubo análisis
    # profundo y/o gráfico — cada modo necesita secciones distintas del .md.

    # ------------------------------------------------------------------
    # Inyectar contexto de refinamiento en el generador
    # ------------------------------------------------------------------
    if contexto_refinamiento:
        ctx = contexto_refinamiento
        contexto_str = f"""
### Contexto de la consulta anterior (REFINAMIENTO)
El usuario está refinando o extendiendo una consulta previa. Usa este contexto:
- SQL anterior: {ctx.get('sql_anterior', '')}
- Columnas del resultado previo: {ctx.get('columnas_previas', [])}
- Período detectado: {ctx.get('periodo_previo', '')}
- Filtros activos: {ctx.get('filtros_previos', [])}
- Descripción: {ctx.get('descripcion_df', '')}

Genera una nueva SQL que incorpore el refinamiento solicitado por el usuario,
manteniendo los filtros base del contexto anterior a menos que el usuario
explícitamente pida cambiarlos.
"""
        system_gen = system_gen + contexto_str

    # ------------------------------------------------------------------
    # Detectar comando /analisis y limpiar el prompt
    # ------------------------------------------------------------------
    forzar_analisis, pregunta_limpia = _es_comando_analisis(pregunta)
    if forzar_analisis:
        print('\n[MODO] Análisis profundo activado via /analisis')
        pregunta = pregunta_limpia

    sql_final = generar_sql_y_validar(pregunta, system_gen, system_val)

    print('Ejecutando consulta en PostgreSQL...')
    # Si la SQL pasó el validador pero Postgres igual devuelve un error real
    # de ejecución (columna inexistente, tipo incompatible, etc.), se
    # reintenta UNA vez reinyectando el error al generador antes de rendirse
    # — el usuario nunca ve el primer error si la corrección funciona.
    # sql_final queda actualizada a la versión que realmente se ejecutó,
    # para que el resto del pipeline (redactor, trazabilidad) sea consistente.
    resultado, sql_final = _ejecutar_consulta_con_reintento(sql_final, system_gen)

    if not resultado.get('success'):
        print(f'\nError al ejecutar: {resultado.get("error")}')
        return {
            'tipo':          'error',
            'respuesta':     f'Error al ejecutar la consulta: {resultado.get("error")}',
            'resultado_sql': None,
            'sql_usada':     sql_final,
            'imagenes':      [],
            'ruta_excel':    '',
            'analisis':      None,
        }

    total_filas = resultado.get('total_filas', 0)
    print(f'Resultado: {total_filas} fila(s) obtenidas.\n')

    # ------------------------------------------------------------------
    # Auto-generar Excel si >100 filas. 'resultado' se mantiene íntegro
    # (no se trunca aquí) para que el analista, los gráficos y el DataFrame
    # de sesión (server.py) trabajen sobre el dataset completo — el
    # truncado a 50 filas se aplica solo a la vista que ve el redactor.
    # ------------------------------------------------------------------
    ruta_excel = ''
    excel_auto = False
    if permitir_excel_individual and total_filas > 100 and resultado.get('rows'):
        print(f'[{MODELO}] Mas de 100 filas ({total_filas}). Generando Excel con todos los datos...')
        ruta_excel = exportar_excel_desde_resultado(resultado, pregunta)
        excel_auto = bool(ruta_excel)

    # ------------------------------------------------------------------
    # Generar grafico si tiene sentido
    # ------------------------------------------------------------------
    imagenes_chat = []
    if es_intencion_grafico(pregunta) and resultado.get('columns'):
        timestamp_graf = datetime.now().strftime('%Y%m%d_%H%M%S')
        print(f'[{MODELO}] Evaluando grafico para la consulta...')
        imagenes_chat = generar_graficos_consulta(resultado, pregunta, timestamp_graf)

    # ------------------------------------------------------------------
    # Formatear como tabla si es una solicitud de tabla, o si el resultado
    # ya es tabular por su forma (varias filas, o una sola fila con varias
    # columnas/metricas) -- una tabla es mas legible y estetica que texto
    # plano separado por comas, independientemente de si el usuario usó
    # una palabra como "tabla"/"top N" al preguntar.
    # ------------------------------------------------------------------
    tabla_markdown = ''
    es_tabular = len(resultado.get('rows', [])) > 1 or len(resultado.get('columns', [])) > 2
    if (es_intencion_tabla(pregunta) or es_tabular) and resultado.get('rows'):
        tabla_markdown = formatear_resultado_como_tabla(resultado)

    # ------------------------------------------------------------------
    # Exportar a Excel si el usuario lo pide explicitamente
    # (solo si no se generó ya por threshold)
    # ------------------------------------------------------------------
    if permitir_excel_individual and not excel_auto and es_intencion_excel(pregunta) and resultado.get('rows'):
        print(f'[{MODELO}] Exportando a Excel...')
        ruta_excel = exportar_excel_desde_resultado(resultado, pregunta)

    # ------------------------------------------------------------------
    # Agente analista (si aplica)
    # ------------------------------------------------------------------
    analisis_profundo = None
    activar_analista = forzar_analisis or es_intencion_analisis(pregunta, resultado)
    # Se calcula una sola vez y se reutiliza abajo para digest_para_redactor
    # si tambien aplica (evita recalcular pre_computar_metricas dos veces
    # sobre el mismo 'resultado' cuando el analista corrio Y total_filas>100).
    metricas_resultado = None

    if activar_analista:
        modo = '/analisis' if forzar_analisis else (
            'LLM' if CLASSIFY_INFERENCE == 'YES' else 'regex'
        )
        print(f'\n[ANALISTA] Activado ({modo}). Iniciando análisis profundo...')
        print('=' * 60)
        metricas_resultado = pre_computar_metricas(resultado)
        analisis_profundo = agente_analista(
            pregunta=pregunta,
            resultado_inicial=resultado,
            sql_inicial=sql_final,
            system_gen=system_gen,
            system_val=system_val,
            metricas_iniciales=metricas_resultado,
        )
        print('=' * 60)
        print('[ANALISTA] Análisis completado.\n')

    # ------------------------------------------------------------------
    # Redactar respuesta final
    # ------------------------------------------------------------------
    # El system prompt del redactor se compone según el modo: la sección
    # 'Modo análisis profundo' del .md ya trae su propia estructura de 6
    # bloques — no se duplica aquí.
    secciones_red = ['Instrucciones (system prompt)']
    if imagenes_chat:
        secciones_red.append('Manejo de Gráficos en Respuestas')
    if analisis_profundo:
        secciones_red.append('Modo análisis profundo')
    system_red = cargar_system_prompt('redactor_respuesta.md', secciones_red)

    print(f'[{MODELO}] Redactando respuesta...')
    # Se incluye la pregunta original para que el redactor sepa qué se pidió,
    # pero el objeto/período que abre la respuesta se sigue derivando del SQL
    # realmente ejecutado (no de la pregunta) — ver regla 2 de
    # redactor_respuesta.md: el generador pudo interpretar "este mes" o una
    # tienda/línea distinto a lo que el usuario nombró.
    # 'resultado' se mantiene íntegro para el resto del pipeline (analista,
    # gráficos, DataFrame de sesión); aquí se trunca solo la vista que ve
    # el redactor, que ya no necesita más de 50 filas para redactar.
    # La muestra es representativa (no un corte posicional) — ver
    # _muestra_representativa. Si el resultado tiene 2+ dimensiones
    # categóricas, además se adjunta el desglose completo agregado por cada
    # dimensión (concentracion_por_dimension) para que ningún grupo quede
    # sin representar aunque no haya entrado en la muestra de filas.
    resultado_para_redactor = resultado
    digest_para_redactor = None
    if total_filas > 100:
        resultado_para_redactor = {**resultado, 'rows': _muestra_representativa(resultado, 50)}
        metricas_regulares = metricas_resultado if metricas_resultado is not None else pre_computar_metricas(resultado)
        digest_para_redactor = metricas_regulares.get('concentracion_por_dimension')
    titulo_resultado = (
        f'### Resultado (muestra representativa de {len(resultado_para_redactor["rows"])} de {total_filas} filas)'
        if total_filas > 100 else '### Resultado'
    )
    prompt_red = (
        f'### Pregunta original del usuario\n"{pregunta}"\n\n'
        f'### Consulta SQL ejecutada\n```sql\n{sql_final}\n```\n\n'
        f'{titulo_resultado}\n{json.dumps(resultado_para_redactor, ensure_ascii=False, indent=2)}'
    )
    if digest_para_redactor:
        prompt_red += (
            f'\n\n### Desglose completo por dimensión (sobre las {total_filas} filas reales, no la muestra)\n'
            f'{json.dumps(digest_para_redactor, ensure_ascii=False, indent=2)}\n\n'
            'Usa este desglose para que tu resumen represente a TODOS los grupos '
            '(ej. todas las zonas), no solo a los que aparecen en la muestra de filas de arriba.'
        )

    if analisis_profundo:
        # sql_queries es solo para trazabilidad (prompt_logger) — no aporta
        # nada al redactor y son ~500 tokens de SQL crudo que no debe ver.
        analisis_para_redactor = {k: v for k, v in analisis_profundo.items() if k != 'sql_queries'}
        prompt_red += f'\n\n### Análisis Profundo del Agente Analista\n'
        prompt_red += json.dumps(analisis_para_redactor, ensure_ascii=False, indent=2)
        prompt_red += (
            '\n\nEl análisis profundo ya está hecho. Redáctalo siguiendo la estructura de '
            '"Modo análisis profundo" de tus instrucciones, citando las cifras del campo '
            '"evidencia" de cada patrón/anomalía/hipótesis integradas en prosa fluida.'
        )

    if excel_auto:
        prompt_red += f'\n\n### Nota de datos completos\nLa consulta devolvio {total_filas} filas en total. Arriba se muestra una muestra representativa (no necesariamente las primeras) para la respuesta. El archivo completo se ha exportado a Excel: {ruta_excel}\nIncluir en la respuesta: "Mostrando una muestra representativa de {total_filas} filas totales. El listado completo se exportó a Excel en: {ruta_excel}"'
    if tabla_markdown:
        prompt_red += (
            '\n\n### Tabla de Resultados\n' + tabla_markdown +
            '\n\nIncluye esta tabla en la respuesta TAL CUAL — es la única forma en que deben '
            'presentarse estas filas. NO repitas los mismos datos como lista numerada ni como '
            'párrafo antes o después de la tabla (ni "1. Éxito Antioquia — 1,200 unidades..."): '
            'eso duplica la información y desperdicia espacio. Antes de la tabla, en vez de una '
            'frase seca de "aquí están los datos", cuenta brevemente la historia que cuentan '
            'esas cifras (quién lidera, qué tan grande es la brecha con el resto, qué tan '
            'concentrado o disperso está el resultado) en 1-2 frases que le den sentido a lo '
            'que el usuario va a leer, sin adelantar cada fila una por una. Después de la '
            'tabla, como máximo 1-2 líneas de insight adicional (qué destaca), nunca una '
            'relectura fila por fila de lo que ya está en la tabla.'
        )
    if imagenes_chat:
        prompt_red += '\n\n### Grafico generado\n' + '\n'.join(imagenes_chat) + '\n\nSi hay un grafico, incluirlo en la respuesta como imagen markdown.'
    if ruta_excel and not excel_auto:
        prompt_red += f'\n\n### Archivo Excel generado\nEl archivo Excel se ha guardado en: {ruta_excel}\nInformar al usuario que puede descargarlo desde esa ruta.'
    if contexto_web:
        prompt_red += contexto_web

    respuesta_final = llamar_llm(
        system_red,
        prompt_red,
        temperatura=0.3,
    )

    print('\n' + '=' * 60)
    print('RESPUESTA')
    print('=' * 60)
    print(respuesta_final)

    # Trazabilidad completa: consulta principal + (si hubo agente analista)
    # todas las rondas complementarias que este disparo.
    sql_queries = [{'nombre': 'consulta_principal', 'sql': sql_final}]
    if analisis_profundo and analisis_profundo.get('sql_queries'):
        # La ronda 0 del analista es la misma consulta_principal — no duplicar.
        sql_queries.extend(
            q for q in analisis_profundo['sql_queries'] if q['nombre'] != 'consulta_principal'
        )

    return {
        'tipo':          'consulta',
        'respuesta':     respuesta_final,
        'resultado_sql': resultado,
        'sql_usada':     sql_final,
        'sql_queries':   sql_queries,
        'imagenes':      imagenes_chat,
        'ruta_excel':    ruta_excel,
        'analisis':      analisis_profundo,
    }


def main():
    if len(sys.argv) < 2:
        print('Uso: python orquestador.py "tu pregunta en lenguaje natural"')
        print('     python orquestador.py "/analisis <pregunta>"  — análisis profundo')
        sys.exit(1)

    pregunta = sys.argv[1]

    try:
        if es_intencion_informe(pregunta):
            generar_informe(pregunta)
        else:
            procesar_consulta(pregunta)
    except RuntimeError as e:
        print(f'\nError: {e}')
        sys.exit(1)

if __name__ == '__main__':
    main()
