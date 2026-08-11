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
    anio = datetime.now().year
    return f"""

### Contexto temporal
- Hoy es {datetime.now().strftime('%d/%m/%Y')}.
- TABLA PRINCIPAL: ventas_unificada — vista materializada que une ventas_2025 y ventas_2026 con GRUPO normalizado.
  - Filtrar por año con WHERE "Año" = {anio} (2026) o WHERE "Año" = 2025.
  - Columna "GRUPO_NORM": GRUPO estandarizado desde la tabla de segmentación. USAR SIEMPRE en lugar de "GRUPO" cuando la tabla sea ventas_unificada.
- ventas_2025 y ventas_2026 solo usar si se necesita el dato crudo sin normalizar.
- NUNCA uses FROM ventas sin sufijo ni _unificada.
- Cuando el usuario mencione un dia o mes sin especificar año, SIEMPRE usa {anio} con ventas_unificada.

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
11. Comparaciones de periodos temporales (ej: enero vs febrero, mes1 vs mes2): NUNCA uses FULL OUTER JOIN, LEFT JOIN, o RIGHT JOIN. Usa UNA SOLA tabla con multiples CASE WHEN para cada periodo. Ejemplo correcto: SELECT SUM(CASE WHEN fecha BETWEEN ene THEN valor ELSE 0 END) AS Enero, SUM(CASE WHEN fecha BETWEEN feb THEN valor ELSE 0 END) AS Febrero FROM tabla WHERE fecha BETWEEN ene_inicio AND feb_fin GROUP BY ...
12. REFERENCIA siempre acompañada de GRUPO: Cuando la consulta incluya la columna "REFERENCIA" en el SELECT, DEBE incluir también UPPER(TRIM("GRUPO")) AS "GRUPO". Esto es obligatorio para que el usuario pueda contextualizar cada referencia dentro de su grupo de producto.
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
7. Revisa que DEPARTAMENTO, CIUDAD, DESC_DEPENDENCIA, RAZON_SOCIAL, CLIMA, ZONA, ZONA_EX, DESC_ITEM usen UPPER(TRIM(...)). Si aparecen sin UPPER en el SELECT, NO rechazar — es opcional.
8. Verifica que el año en los literales de fecha sea {anio} o 2025. Si ves 2024 u otro año en una fecha literal, RECHAZAR.
9. LINEA — excepcion critica: la columna LINEA tiene casing mixto. TRIM("LINEA") = '11 - Dama Deportivo' es CORRECTO. UPPER(TRIM("LINEA")) en un filtro WHERE es un ERROR — RECHAZAR si aparece. NUNCA rechazar una query por usar TRIM("LINEA") sin UPPER.
10. SIGNO — prohibido: NUNCA debe aparecer TRIM("SIGNO") ni ningun filtro sobre "SIGNO" en la query. Si aparece → RECHAZAR e indicar que se elimine. DESC_MOVIMIENTO = 'VENTAS POS' ya delimita las ventas sin necesidad de SIGNO.
11. LIMIT y window functions: si la consulta usa RANK(), ROW_NUMBER() o DENSE_RANK() con un filtro WHERE sobre el ranking (ej: WHERE ranking = 1), el resultado esta acotado por el numero de grupos del PARTITION BY — NO exigir LIMIT. Lineas de producto son ~10, departamentos ~33, tallas ~10: estos GROUP BY nunca necesitan LIMIT.
12. Alias internos de subconsultas: alias en minusculas sin caracteres especiales (ranking, rn, row_num, subconsulta) NO requieren comillas dobles. WHERE ranking = 1 es correcto. NUNCA rechazar por ausencia de comillas en alias de window functions o nombres de subquery.
13. NO rechazar dos veces por el mismo problema. Si el generador ya corrigio un error en el intento anterior, aprobarlo aunque queden imperfecciones menores de estilo.
14. CAST para ROUND: si la consulta usa ROUND() sobre una operacion aritmetica (division, multiplicacion), DEBE incluir CAST(...AS numeric). Si ves ROUND((... * 100) / ..., N) sin CAST → RECHAZAR. La forma correcta es ROUND(CAST((... * 100) / ... AS numeric), N).
15. Deteccion de Cartesian product en comparaciones de periodos: si la consulta contiene FULL OUTER JOIN, LEFT JOIN, o RIGHT JOIN combinado con EXTRACT(DAY FROM ...) = EXTRACT(DAY FROM ...) u otro EXTRACT en la condicion ON → RECHAZAR. Feedback: "Para comparar periodos (enero vs febrero), NO uses JOINs. Usa UNA tabla con multiples CASE WHEN para cada periodo: SUM(CASE WHEN fecha BETWEEN ene THEN valor ELSE 0 END) AS Enero, SUM(CASE WHEN fecha BETWEEN feb THEN valor ELSE 0 END) AS Febrero".
16. LIMIT obligatorio en subqueries con "día/fecha de mayor venta": si la consulta detecta un patrón como COALESCE(...SELECT...GROUP BY fecha...LIMIT 1), DEBE tener LIMIT N en la query principal. Sin LIMIT → RECHAZAR. Mensaje: "Falta LIMIT en la query principal. Subqueries que buscan 'día de mayor venta' requieren LIMIT obligatorio para evitar procesar todas las filas. Agregar LIMIT 10 (o el número que pidió el usuario) antes del punto y coma final".
17. Tabla ventas_unificada: es una tabla válida. Aceptar consultas que usen FROM ventas_unificada. Si usa "GRUPO" en lugar de "GRUPO_NORM" sobre ventas_unificada, ADVERTIR en el feedback pero NO rechazar."""

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

def es_intencion_informe(texto: str) -> bool:
    return bool(PATRONES_INFORME.search(texto))

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
    
    # Limitar filas
    total_filas = len(rows)
    rows_mostrados = rows[:limite]
    
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
            if isinstance(val, (int, float)):
                # Si es número grande, agregar separador de miles
                if isinstance(val, int) and val > 999:
                    val_str = f"{val:,}"
                elif isinstance(val, float):
                    val_str = f"{val:,.2f}"
                else:
                    val_str = str(val)
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
        return {'success': False, 'error': str(e)}


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
            sys.exit(1)

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

### Mapeo obligatorio bloque → columna SQL
Para cada bloque, DEBES usar estas columnas exactas al generar la SQL:
- Bloque I (Referencia): SELECT TRIM("REFERENCIA"), TRIM("LINEA"), TRIM("GRUPO") con GROUP BY TRIM("REFERENCIA"), TRIM("LINEA"), TRIM("GRUPO") — incluir LINEA y GRUPO en lugar de DESC_ITEM
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
                (extraer_system_gen.group(1).strip() if extraer_system_gen else instrucciones_gen) + reglas_extra_gen,
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
            max_tokens=5,
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

def pre_computar_metricas(resultado: dict) -> dict:
    """
    Calcula métricas derivadas sobre el resultado SQL antes de enviarlo al analista.
    El analista recibe hechos ya calculados, no solo números crudos.

    Retorna dict con:
      - totales: suma de cada columna numérica
      - min_max: valor mínimo y máximo con etiqueta de categoría
      - top3_concentracion_pct: % que acumulan las 3 primeras filas
      - concentracion_por_categoria: % de cada categoría sobre el total (si hay col de texto)
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

    # Identificar columnas numéricas y categóricas
    primera = dict(zip(cols, rows[0]))
    cols_num = [c for c, v in primera.items() if isinstance(v, (int, float))]
    col_cat  = next((c for c, v in primera.items() if isinstance(v, str)), None)

    if not cols_num:
        return {}

    metricas = {}

    # — Construir vectores de valores por columna —
    vectores = {}
    for col in cols_num:
        vals = []
        for row in rows:
            rd = dict(zip(cols, row))
            v  = rd.get(col)
            vals.append(float(v) if isinstance(v, (int, float)) else 0.0)
        vectores[col] = vals

    for col, valores in vectores.items():
        total = sum(valores)
        n     = len(valores)

        # Totales
        metricas.setdefault('totales', {})[col] = round(total, 2)

        # Min / Max con etiqueta
        idx_max = valores.index(max(valores))
        idx_min = valores.index(min(valores))
        label_max = str(dict(zip(cols, rows[idx_max])).get(col_cat, idx_max)) if col_cat else str(idx_max)
        label_min = str(dict(zip(cols, rows[idx_min])).get(col_cat, idx_min)) if col_cat else str(idx_min)
        metricas.setdefault('min_max', {})[col] = {
            'max': {'valor': round(max(valores), 2), 'en': label_max},
            'min': {'valor': round(min(valores), 2), 'en': label_min},
        }

        # Concentración top-3
        if total > 0 and n >= 3:
            top3_suma = sum(sorted(valores, reverse=True)[:3])
            metricas.setdefault('top3_concentracion_pct', {})[col] = round(top3_suma / total * 100, 1)

        # Concentración por categoría (% de cada fila sobre el total)
        if col_cat and total > 0:
            dist = []
            for i, v in enumerate(valores):
                label = str(dict(zip(cols, rows[i])).get(col_cat, i))
                dist.append({
                    'categoria': label,
                    'valor':     round(v, 2),
                    'pct':       round(v / total * 100, 1),
                })
            # Solo incluir si hay variación significativa (no todos iguales)
            pcts = [d['pct'] for d in dist]
            if max(pcts) - min(pcts) > 2:
                metricas.setdefault('concentracion_por_categoria', {})[col] = sorted(
                    dist, key=lambda x: x['valor'], reverse=True
                )[:10]  # top 10 para no saturar el contexto

        # Variaciones % entre filas consecutivas
        variaciones_pct_vals = []
        if n >= 2:
            variaciones = []
            for i in range(1, n):
                ant = valores[i - 1]
                act = valores[i]
                pct = round((act - ant) / abs(ant) * 100, 1) if ant != 0 else None
                label = str(dict(zip(cols, rows[i])).get(col_cat, i)) if col_cat else str(i)
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
                except statistics.StatisticsError:
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
                        label = str(dict(zip(cols, rows[i])).get(col_cat, i)) if col_cat else str(i)
                        outs.append({
                            'en':               label,
                            'valor':            round(v, 2),
                            'desviaciones_std': round((v - media) / std, 1),
                            'direccion':        'alto' if v > media else 'bajo',
                        })
                if outs:
                    metricas.setdefault('outliers', {})[col] = outs
            except statistics.StatisticsError:
                pass

        # Gaps: posiciones con valor 0
        gaps = []
        for i, v in enumerate(valores):
            if v == 0:
                label = str(dict(zip(cols, rows[i])).get(col_cat, i)) if col_cat else str(i)
                gaps.append(label)
        if gaps:
            metricas.setdefault('gaps_valor_cero', {})[col] = gaps

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


def agente_analista(
    pregunta: str,
    resultado_inicial: dict,
    sql_inicial: str,
    system_gen: str,
    system_val: str,
) -> dict:
    """
    Ejecuta el análisis profundo sobre los datos.
    Puede pedir hasta MAX_RONDAS_ANALISTA consultas adicionales al generador.

    Retorna el JSON estructurado final del analista:
    {
      "estado": "completo",
      "patrones": [...],
      "anomalias": [...],
      "hipotesis": [...],
      "datos_usados": [...],
      "conclusion": "...",
      "preguntas_sugeridas": [...]
    }
    """
    system_analista = leer_instrucciones('analista.md')
    # Extraer solo la sección de instrucciones del system prompt
    m = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*)', system_analista, re.DOTALL)
    system_analista = m.group(1).strip() if m else system_analista

    # Acumular todos los datos a través de las rondas
    datos_acumulados = [
        {
            'ronda':       0,
            'descripcion': 'Consulta inicial del usuario',
            'sql':         sql_inicial,
            'resultado':   resultado_inicial,
        }
    ]

    for ronda in range(MAX_RONDAS_ANALISTA + 1):
        # Pre-computar métricas sobre el resultado más reciente
        resultado_reciente = datos_acumulados[-1]['resultado']
        metricas = pre_computar_metricas(resultado_reciente)

        # Construir prompt para el analista
        prompt = f"""Pregunta original del usuario: "{pregunta}"

=== DATOS DISPONIBLES ===

"""
        for d in datos_acumulados:
            prompt += f"--- Ronda {d['ronda']}: {d['descripcion']} ---\n"
            prompt += f"SQL usado:\n{d['sql']}\n\n"
            prompt += f"Resultado ({d['resultado'].get('total_filas', 0)} filas):\n"
            prompt += json.dumps({
                'columns': d['resultado'].get('columns', []),
                'rows':    d['resultado'].get('rows', [])[:50],  # máx 50 filas al LLM
            }, ensure_ascii=False, indent=2)
            prompt += '\n\n'

        prompt += f"""=== MÉTRICAS PRE-COMPUTADAS (última ronda) ===
{json.dumps(metricas, ensure_ascii=False, indent=2)}

=== INSTRUCCIÓN ===
"""
        if ronda >= MAX_RONDAS_ANALISTA:
            prompt += 'Has alcanzado el límite de rondas. Produce el análisis completo con los datos que tienes. Responde con estado "completo".'
        else:
            prompt += (
                f'Ronda {ronda + 1} de {MAX_RONDAS_ANALISTA} posibles. '
                'Analiza los datos. Si necesitas más información, responde con estado "necesita_datos". '
                'Si tienes suficiente para un análisis completo, responde con estado "completo".'
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
                'patrones':  [],
                'anomalias': [],
                'hipotesis': [],
                'datos_usados': [{'descripcion': d['descripcion'], 'filas': d['resultado'].get('total_filas', 0), 'columnas': d['resultado'].get('columns', [])} for d in datos_acumulados],
                'conclusion': respuesta_raw,
                'preguntas_sugeridas': [],
                'sql_queries': _sql_queries_desde_acumulados(datos_acumulados),
            }

        estado = analisis.get('estado', 'completo')

        if estado == 'completo' or ronda >= MAX_RONDAS_ANALISTA:
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

        # estado == 'necesita_datos' — pedir consulta complementaria
        solicitud = analisis.get('consulta_adicional', {})
        pregunta_adicional = solicitud.get('pregunta', '')
        contexto_adicional = solicitud.get('contexto', '')
        razon              = analisis.get('razon', '')

        if not pregunta_adicional:
            print('  [analista] Pidió datos adicionales pero sin pregunta. Terminando.')
            analisis['estado'] = 'completo'
            analisis['sql_queries'] = _sql_queries_desde_acumulados(datos_acumulados)
            return analisis

        print(f'  [analista] Solicita datos adicionales (ronda {ronda + 1}): {pregunta_adicional}')
        print(f'             Razón: {razon}')

        # Generar SQL para la consulta complementaria con contexto enriquecido
        prompt_gen_adicional = (
            f'{pregunta_adicional}\n\n'
            f'Contexto adicional para esta consulta: {contexto_adicional}\n'
            f'Columnas que se esperan en el resultado: {json.dumps(solicitud.get("columnas_esperadas", []))}'
        )
        sql_adicional = generar_sql_y_validar(prompt_gen_adicional, system_gen, system_val)

        print(f'  Ejecutando consulta complementaria...')
        resultado_adicional = ejecutar_consulta(sql_adicional)

        if not resultado_adicional.get('success'):
            print(f'  [analista] Error en consulta complementaria: {resultado_adicional.get("error")} — terminando análisis con datos actuales.')
            analisis['estado'] = 'completo'
            if 'datos_usados' not in analisis:
                analisis['datos_usados'] = []
            analisis['sql_queries'] = _sql_queries_desde_acumulados(datos_acumulados)
            return analisis

        print(f'  Complementaria OK: {resultado_adicional.get("total_filas", 0)} filas obtenidas.')
        datos_acumulados.append({
            'ronda':       ronda + 1,
            'descripcion': pregunta_adicional,
            'sql':         sql_adicional,
            'resultado':   resultado_adicional,
        })

    # Nunca debería llegar aquí, pero por seguridad:
    return {
        'estado': 'completo', 'conclusion': 'Análisis completado.', 'patrones': [],
        'anomalias': [], 'hipotesis': [], 'datos_usados': [], 'preguntas_sugeridas': [],
        'sql_queries': _sql_queries_desde_acumulados(datos_acumulados),
    }


def procesar_consulta(pregunta: str, contexto_refinamiento: dict | None = None):
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
    instrucciones_gen = leer_instrucciones('generador_consultas.md')
    instrucciones_val = leer_instrucciones('validador.md')
    instrucciones_red = leer_instrucciones('redactor_respuesta.md')

    extraer_system_gen = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_gen, re.DOTALL)
    extraer_system_val = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_val, re.DOTALL)
    extraer_system_red = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_red, re.DOTALL)

    system_gen = (extraer_system_gen.group(1).strip() if extraer_system_gen else instrucciones_gen) + _reglas_gen()
    system_val = (extraer_system_val.group(1).strip() if extraer_system_val else instrucciones_val) + _reglas_val()
    system_red = extraer_system_red.group(1).strip() if extraer_system_red else instrucciones_red

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
    resultado = ejecutar_consulta(sql_final)

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
    # Truncar a 50 filas para el LLM y auto-generar Excel si >100 filas
    # ------------------------------------------------------------------
    filas_completas = resultado.get('rows', [])
    ruta_excel = ''
    excel_auto = False
    if total_filas > 100 and filas_completas:
        print(f'[{MODELO}] Mas de 100 filas ({total_filas}). Generando Excel con todos los datos...')
        ruta_excel = exportar_excel_desde_resultado(resultado, pregunta)
        resultado['rows'] = filas_completas[:50]
        resultado['total_filas'] = total_filas
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
    # Formatear como tabla si es una solicitud de tabla
    # ------------------------------------------------------------------
    tabla_markdown = ''
    if es_intencion_tabla(pregunta) and resultado.get('rows'):
        tabla_markdown = formatear_resultado_como_tabla(resultado)

    # ------------------------------------------------------------------
    # Exportar a Excel si el usuario lo pide explicitamente
    # (solo si no se generó ya por threshold)
    # ------------------------------------------------------------------
    if not excel_auto and es_intencion_excel(pregunta) and resultado.get('rows'):
        print(f'[{MODELO}] Exportando a Excel...')
        ruta_excel = exportar_excel_desde_resultado(resultado, pregunta)

    # ------------------------------------------------------------------
    # Agente analista (si aplica)
    # ------------------------------------------------------------------
    analisis_profundo = None
    activar_analista = forzar_analisis or es_intencion_analisis(pregunta, resultado)

    if activar_analista:
        modo = '/analisis' if forzar_analisis else (
            'LLM' if CLASSIFY_INFERENCE == 'YES' else 'regex'
        )
        print(f'\n[ANALISTA] Activado ({modo}). Iniciando análisis profundo...')
        print('=' * 60)
        analisis_profundo = agente_analista(
            pregunta=pregunta,
            resultado_inicial=resultado,
            sql_inicial=sql_final,
            system_gen=system_gen,
            system_val=system_val,
        )
        print('=' * 60)
        print('[ANALISTA] Análisis completado.\n')

    # ------------------------------------------------------------------
    # Redactar respuesta final
    # ------------------------------------------------------------------
    print(f'[{MODELO}] Redactando respuesta...')
    # Se incluye el SQL realmente ejecutado (no la pregunta del usuario) para
    # que el redactor pueda abrir la respuesta indicando objeto y período
    # reales analizados — ver regla 2 de redactor_respuesta.md.
    prompt_red = (
        f'### Consulta SQL ejecutada\n```sql\n{sql_final}\n```\n\n'
        f'### Resultado\n{json.dumps(resultado, ensure_ascii=False, indent=2)}'
    )

    if analisis_profundo:
        prompt_red += f'\n\n### Análisis Profundo del Agente Analista\n'
        prompt_red += json.dumps(analisis_profundo, ensure_ascii=False, indent=2)
        prompt_red += (
            '\n\nEl análisis profundo ya está hecho. Tu tarea es redactarlo de forma clara y natural. '
            'Estructura la respuesta así:\n'
            '1. Resumen de los datos (tabla si aplica)\n'
            '2. Patrones y anomalías encontrados\n'
            '3. Hipótesis y causas probables\n'
            '4. Conclusión\n'
            '5. Preguntas sugeridas para profundizar (si las hay)\n'
            'No repitas los números crudos del JSON — intégralos en texto fluido.'
        )

    if excel_auto:
        prompt_red += f'\n\n### Nota de datos completos\nLa consulta devolvio {total_filas} filas en total. Se muestran las primeras 50 para la respuesta. El archivo completo se ha exportado a Excel: {ruta_excel}\nIncluir en la respuesta: "Mostrando las primeras 50 de {total_filas} filas. El listado completo se exportó a Excel en: {ruta_excel}"'
    if tabla_markdown:
        prompt_red += '\n\n### Tabla de Resultados\n' + tabla_markdown + '\n\nPor favor, incluir esta tabla en la respuesta.'
    if imagenes_chat:
        prompt_red += '\n\n### Grafico generado\n' + '\n'.join(imagenes_chat) + '\n\nSi hay un grafico, incluirlo en la respuesta como imagen markdown.'
    if ruta_excel and not excel_auto:
        prompt_red += f'\n\n### Archivo Excel generado\nEl archivo Excel se ha guardado en: {ruta_excel}\nInformar al usuario que puede descargarlo desde esa ruta.'

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

    if es_intencion_informe(pregunta):
        generar_informe(pregunta)
    else:
        procesar_consulta(pregunta)

if __name__ == '__main__':
    main()
