#!/usr/bin/env python3
"""
Orquestador: pipeline de consulta de datos en lenguaje natural.
Flujo:
  - Pregunta normal → generador_consultas → validador → consultar_db → redactor_respuesta
  - Intencion de informe (detectada por keywords) → mismo pipeline + skill informe_ventas → generar_docx
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
from groq import Groq

load_dotenv()
client = Groq(api_key=os.getenv('GROQ_API_KEY'))
MODELO = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')

BASE_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = BASE_DIR / 'agents'
TOOLS_DIR = BASE_DIR / 'tools'
SKILLS_DIR = BASE_DIR / 'skills'
REPORTS_DIR = BASE_DIR / 'reports'
MAX_ITERACIONES = 3

PATRONES_INFORME = re.compile(
    r'\b(informe|reporte|report|documento|word|docx|'
    r'resumen.*mensual|balance.*mes|'
    r'prepara.*informe|genera.*informe|crea.*documento)\b',
    re.IGNORECASE
)

def es_intencion_informe(texto: str) -> bool:
    return bool(PATRONES_INFORME.search(texto))

def leer_instrucciones(archivo: str) -> str:
    ruta = AGENTS_DIR / archivo
    return ruta.read_text(encoding='utf-8')

def leer_skill(nombre: str) -> str:
    ruta = SKILLS_DIR / nombre / 'SKILL.md'
    return ruta.read_text(encoding='utf-8')

def limpiar_texto(texto: str) -> str:
    return ''.join(c for c in texto if unicodedata.category(c) != 'So').strip()

def llamar_llm(system_prompt: str, user_prompt: str, temperatura: float = 0.1) -> str:
    respuesta = client.chat.completions.create(
        model=MODELO,
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        temperature=temperatura,
    )
    return limpiar_texto(respuesta.choices[0].message.content.strip())

def extraer_sql(texto: str) -> str:
    bloques = re.findall(r'```sql\s*(.*?)\s*```', texto, re.DOTALL | re.IGNORECASE)
    if bloques:
        return bloques[0].strip()
    lineas = texto.strip().split('\n')
    sql_lines = [l for l in lineas if l.strip().upper().startswith(('SELECT', 'WITH', 'EXPLAIN'))]
    if sql_lines:
        return '\n'.join(sql_lines)
    return texto.strip()

def ejecutar_consulta(sql: str, limite: int = 20) -> dict:
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

def generar_informe(pregunta: str):
    print('\n' + '=' * 60)
    print('INTENCION DETECTADA: GENERAR INFORME DE VENTAS')
    print('=' * 60)

    instrucciones_gen = leer_instrucciones('generador_consultas.md')
    instrucciones_val = leer_instrucciones('validador.md')
    skill_informe     = leer_skill('informe_ventas')

    extraer_system_gen = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_gen, re.DOTALL)
    extraer_system_val = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_val, re.DOTALL)

    reglas_extra_gen = """

### Reglas adicionales obligatorias
1. Siempre usa comillas dobles en TODOS los nombres de columna.
2. Usa TRIM() en columnas de texto: TRIM("SIGNO"), TRIM("DEPARTAMENTO"), TRIM("DESC_MOVIMIENTO").
3. "SIGNO" puede ser null, '-' con espacios, o '+' con espacios.
4. FECHA_MVTO es TEXT DD/MM/AAAA. Usa TO_DATE("FECHA_MVTO", 'DD/MM/YYYY'). NO uses ::DATE.
5. Para valor de ventas usa "CANTIDAD" * "PVP". NUNCA uses "PVP LISTA" para tiendas individuales.
6. "PVP LISTA" SOLO se usa si la consulta es sobre clientes MACRO (cadenas), no tiendas.
7. Si un alias tiene mayusculas (ej: "Ventas"), ponle comillas dobles en ORDER BY y GROUP BY: ORDER BY "Ventas" DESC.
8. **Textos siempre en mayusculas**: DEPARTAMENTO, CIUDAD, DESC_DEPENDENCIA, RAZON_SOCIAL, CLIMA, ZONA, ZONA_EX, DESC_ITEM deben usar UPPER(TRIM(...)) en SELECT. Ej: UPPER(TRIM("DEPARTAMENTO")) AS "DEPARTAMENTO".
"""
    reglas_extra_val = """

### Reglas adicionales obligatorias
1. Verifica que TODOS los nombres de columna esten entre comillas dobles.
2. Verifica uso de TRIM() en filtros de texto.
3. No aceptes LIMIT en COUNT(*) o agregaciones simples.
4. Rechaza "FECHA_MVTO"::DATE. Debe ser TO_DATE().
5. Revisa que use "CANTIDAD" * "PVP" para valor de ventas, no "PVP LISTA" (a menos que sea consulta macro).
6. Revisa que los alias con mayusculas usen comillas dobles en ORDER BY/GROUP BY. Sin comillas PostgreSQL los dobla a minusculas y no encuentra el alias.
7. Revisa que DEPARTAMENTO, CIUDAD, DESC_DEPENDENCIA, RAZON_SOCIAL, CLIMA, ZONA, ZONA_EX, DESC_ITEM usen UPPER(TRIM(...)). Si aparecen sin UPPER, RECHAZAR.
"""

    system_gen = (extraer_system_gen.group(1).strip() if extraer_system_gen else instrucciones_gen) + reglas_extra_gen
    system_val = (extraer_system_val.group(1).strip() if extraer_system_val else instrucciones_val) + reglas_extra_val

    # ------------------------------------------------------------------
    # FASE 1: el LLM decide que consultas necesita segun la peticion
    # ------------------------------------------------------------------
    prompt_planificacion = f"""El usuario quiere generar un informe con la siguiente peticion:
"{pregunta}"

Tienes disponible la siguiente skill de informes que define los bloques disponibles:
{skill_informe}

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
    # FASE 2: validar y ejecutar cada consulta del plan
    # ------------------------------------------------------------------
    resultados = {}
    for item in plan['consultas']:
        nombre = item.get('nombre', 'consulta')
        sql_raw = item.get('sql', '').strip()
        if not sql_raw:
            continue

        print(f'\n  Validando: {nombre}...')
        sql_validado = sql_raw
        for intento in range(MAX_ITERACIONES):
            validacion = llamar_llm(system_val, sql_validado, temperatura=0.0)
            if 'VALIDA' in validacion.upper():
                print(f'  Validacion APROBADA.')
                break
            else:
                print(f'  Validacion RECHAZADA (intento {intento + 1}). Corrigiendo...')
                if intento < MAX_ITERACIONES - 1:
                    correccion = llamar_llm(
                        system_gen,
                        f"La consulta fue rechazada:\n{validacion}\n\nCorrige la SQL:\n{sql_validado}",
                        temperatura=0.2,
                    )
                    sql_validado = extraer_sql(correccion)
                else:
                    print(f'  Se agotaron los intentos para: {nombre}')
                    sql_validado = None
                    break

        if sql_validado:
            print(f'  Ejecutando: {nombre}...')
            resultado = ejecutar_consulta(sql_validado, limite=500)
            if resultado.get('success'):
                resultados[nombre] = resultado
                print(f'  OK: {resultado["total_filas"]} fila(s)')
            else:
                print(f'  Error: {resultado.get("error")}')

    if not resultados:
        print('No se obtuvieron datos. Abortando informe.')
        return

    # ------------------------------------------------------------------
    # FASE 3: redactar el informe con los bloques seleccionados
    # ------------------------------------------------------------------
    print(f'\n[{MODELO}] Redactando informe...')
    prompt_redaccion = f"""El usuario solicito:
"{pregunta}"

Usa la siguiente skill para redactar el informe en Markdown:
{skill_informe}

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
    md_informe = llamar_llm(skill_informe, prompt_redaccion, temperatura=0.3)

    # ------------------------------------------------------------------
    # FASE 4: guardar markdown y convertir a DOCX
    # ------------------------------------------------------------------
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '_', pregunta.lower())[:40].strip('_')
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
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

    system_gen = (extraer_system_gen.group(1).strip() if extraer_system_gen else instrucciones_gen) + """

### Reglas adicionales obligatorias
1. Siempre usa comillas dobles en TODOS los nombres de columna.
2. Usa TRIM() en columnas de texto: TRIM("SIGNO"), TRIM("DEPARTAMENTO"), TRIM("DESC_MOVIMIENTO").
3. "SIGNO" puede ser null, '-' con espacios, o '+' con espacios.
4. FECHA_MVTO es TEXT DD/MM/AAAA. Usa TO_DATE("FECHA_MVTO", 'DD/MM/YYYY'). NO uses ::DATE.
5. Para valor de ventas usa "CANTIDAD" * "PVP". NUNCA uses "PVP LISTA" para tiendas individuales.
6. "PVP LISTA" SOLO se usa si la consulta es sobre clientes MACRO (cadenas), no tiendas.
7. Si un alias tiene mayusculas (ej: "Ventas"), ponle comillas dobles en ORDER BY y GROUP BY: ORDER BY "Ventas" DESC.
8. Textos siempre en mayusculas: DEPARTAMENTO, CIUDAD, DESC_DEPENDENCIA, RAZON_SOCIAL, CLIMA, ZONA, ZONA_EX, DESC_ITEM deben usar UPPER(TRIM(...)).
"""
    system_val = (extraer_system_val.group(1).strip() if extraer_system_val else instrucciones_val) + """

### Reglas adicionales obligatorias
1. Verifica que TODOS los nombres de columna esten entre comillas dobles.
2. Verifica uso de TRIM() en filtros de texto.
3. No aceptes LIMIT en COUNT(*) o agregaciones.
4. Rechaza "FECHA_MVTO"::DATE. Debe ser TO_DATE().
5. Revisa que use "CANTIDAD" * "PVP" para valor de ventas, no "PVP LISTA" (a menos que sea consulta macro).
6. Revisa que los alias con mayusculas usen comillas dobles en ORDER BY/GROUP BY.
7. Revisa que DEPARTAMENTO, CIUDAD, DESC_DEPENDENCIA, RAZON_SOCIAL, CLIMA, ZONA, ZONA_EX, DESC_ITEM usen UPPER(TRIM(...)). Si aparecen sin UPPER, RECHAZAR.
"""
    system_red = extraer_system_red.group(1).strip() if extraer_system_red else instrucciones_red

    sql_final = generar_sql_y_validar(pregunta, system_gen, system_val)

    print('Ejecutando consulta en PostgreSQL...')
    resultado = ejecutar_consulta(sql_final)

    if not resultado.get('success'):
        print(f'\nError al ejecutar: {resultado.get("error")}')
        sys.exit(1)

    print(f'Resultado: {resultado["total_filas"]} fila(s) obtenidas.\n')

    print(f'[{MODELO}] Redactando respuesta...')
    respuesta_final = llamar_llm(
        system_red,
        json.dumps(resultado, ensure_ascii=False, indent=2),
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
