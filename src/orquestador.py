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
- La tabla ventas_2026 SOLO contiene datos del año {anio}.
- La tabla ventas_2025 SOLO contiene datos del año 2025.
- Ambas tablas tienen el mismo esquema de columnas.
- Si el usuario pregunta por 2025 usa ventas_2025. Si no especifica año, usa ventas_2026.
- NUNCA uses FROM ventas (sin sufijo de año), esa tabla no existe.
- Cuando el usuario mencione un dia o mes sin especificar año, SIEMPRE usa {anio}.
- NUNCA uses ningun otro año.

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
8. Verifica que el año en los literales de fecha sea {anio}. Si ves 2024, 2025 u otro año en una fecha literal, RECHAZAR.
9. LINEA — excepcion critica: la columna LINEA tiene casing mixto. TRIM("LINEA") = '11 - Dama Deportivo' es CORRECTO. UPPER(TRIM("LINEA")) en un filtro WHERE es un ERROR — RECHAZAR si aparece. NUNCA rechazar una query por usar TRIM("LINEA") sin UPPER.
10. SIGNO — prohibido: NUNCA debe aparecer TRIM("SIGNO") ni ningun filtro sobre "SIGNO" en la query. Si aparece → RECHAZAR e indicar que se elimine. DESC_MOVIMIENTO = 'VENTAS POS' ya delimita las ventas sin necesidad de SIGNO.
11. LIMIT y window functions: si la consulta usa RANK(), ROW_NUMBER() o DENSE_RANK() con un filtro WHERE sobre el ranking (ej: WHERE ranking = 1), el resultado esta acotado por el numero de grupos del PARTITION BY — NO exigir LIMIT. Lineas de producto son ~10, departamentos ~33, tallas ~10: estos GROUP BY nunca necesitan LIMIT.
12. Alias internos de subconsultas: alias en minusculas sin caracteres especiales (ranking, rn, row_num, subconsulta) NO requieren comillas dobles. WHERE ranking = 1 es correcto. NUNCA rechazar por ausencia de comillas en alias de window functions o nombres de subquery.
13. NO rechazar dos veces por el mismo problema. Si el generador ya corrigio un error en el intento anterior, aprobarlo aunque queden imperfecciones menores de estilo.
14. CAST para ROUND: si la consulta usa ROUND() sobre una operacion aritmetica (division, multiplicacion), DEBE incluir CAST(...AS numeric). Si ves ROUND((... * 100) / ..., N) sin CAST → RECHAZAR. La forma correcta es ROUND(CAST((... * 100) / ... AS numeric), N).
15. Deteccion de Cartesian product en comparaciones de periodos: si la consulta contiene FULL OUTER JOIN, LEFT JOIN, o RIGHT JOIN combinado con EXTRACT(DAY FROM ...) = EXTRACT(DAY FROM ...) u otro EXTRACT en la condicion ON → RECHAZAR. Feedback: "Para comparar periodos (enero vs febrero), NO uses JOINs. Usa UNA tabla con multiples CASE WHEN para cada periodo: SUM(CASE WHEN fecha BETWEEN ene THEN valor ELSE 0 END) AS Enero, SUM(CASE WHEN fecha BETWEEN feb THEN valor ELSE 0 END) AS Febrero".
16. LIMIT obligatorio en subqueries con "día/fecha de mayor venta": si la consulta detecta un patrón como COALESCE(...SELECT...GROUP BY fecha...LIMIT 1), DEBE tener LIMIT N en la query principal. Sin LIMIT → RECHAZAR. Mensaje: "Falta LIMIT en la query principal. Subqueries que buscan 'día de mayor venta' requieren LIMIT obligatorio para evitar procesar todas las filas. Agregar LIMIT 10 (o el número que pidió el usuario) antes del punto y coma final"."""

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
    'A': None,  # Resumen ejecutivo: sin gráfico (números directos)
    'B': None,  # KPIs principales: sin gráfico (solo números)
    'C': 'barras_agrupadas',  # Variación vs mes anterior: comparación de períodos
    'D': ['barras_horizontales', 'torta'],  # Geográfico (departamentos/ciudades): ranking o participación
    'E': 'barras_horizontales',  # Dependencias: ranking
    'F': 'barras_horizontales',  # Tiendas: ranking
    'G': ['barras_verticales', 'barras_horizontales'],  # Línea de producto: distribución o ranking
    'H': 'barras_verticales',  # Producto (referencias): distribución por cantidad
    'I': 'barras_horizontales',  # Análisis de referencias: ranking top
    'J': 'barras_verticales',  # Tallas: distribución (XS, S, M, L, XL, XXL)
    'K': 'linea',  # Evolución temporal: SIEMPRE línea (fechas, días, semanas)
    'L': 'barras_verticales',  # Tiendas activas: barras simples
    'M': None,  # Alertas: sin gráfico
    'N': None,  # Anexo de datos completos: tabla de datos, sin gráfico
    'O': None,  # Tablas interactivas: ya son markdown tables, sin gráfico PNG
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
- Si la pregunta contiene palabras como "compara", "versus", "vs", "diferencia"
  Y los datos tienen múltiples columnas numéricas (ej: Ventas_Enero, Ventas_Febrero):
  ENTONCES tipo = "barras_agrupadas" y columna_serie = nombre de la serie (ej: "Ventas_Enero")
  Esto crea barras lado-a-lado para comparar valores entre series.
  
- Si la columna_x contiene fechas, días, semanas o meses (nombre: Fecha, dia, fecha_mvto, etc.)
  Y NO es comparación de múltiples series:
  ENTONCES tipo = "linea" y columna_serie = null
  
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

    print(f'[{MODELO}] Redactando respuesta...')
    prompt_red = json.dumps(resultado, ensure_ascii=False, indent=2)
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
