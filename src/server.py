"""
src/server.py
Servidor FastAPI persistente que reemplaza el modelo subprocess.
Mantiene sesiones, DataFrames y historial en memoria entre mensajes.

Endpoints:
  POST /chat              — procesa un mensaje del usuario
  POST /reset/{session_id} — reinicia una sesión manualmente
  POST /feedback          — registra feedback de una respuesta (por log_id)
  GET  /health            — estado del servidor
  GET  /sessions          — resumen de sesiones activas (debug)
"""
import json
import sys
import os
import re
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Setup de paths — igual que en orquestador.py
# ---------------------------------------------------------------------------
BASE_DIR  = Path(__file__).resolve().parent.parent
SRC_DIR   = BASE_DIR / 'src'
TOOLS_DIR = BASE_DIR / 'tools'

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# ---------------------------------------------------------------------------
# Imports internos
# ---------------------------------------------------------------------------
from src.session_store import store, Turno
from src.enrutador_sesion import clasificar
from src.orquestador import (
    procesar_consulta,
    generar_informe,
    es_intencion_informe,
    llamar_llm,
    leer_instrucciones,
    _reglas_gen,
)
from src.prompt_logger import registrar_prompt, actualizar_feedback
from tools.tool_pandas import ejecutar_operacion, catalogo_para_llm

# ---------------------------------------------------------------------------
# App FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(
    title='Agentic Query Pipeline — Servidor',
    description='Pipeline de análisis de ventas Creytex con memoria de sesión',
    version='2.0.0',
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

# ---------------------------------------------------------------------------
# Modelos de request/response
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    session_id: str
    pregunta:   str
    origen:     str = 'streamlit'  # 'streamlit' | 'telegram' — trazabilidad de la fuente

class FeedbackRequest(BaseModel):
    log_id:       str
    feedback:     str   # 'bueno' | 'regular' | 'malo'
    feedback_msg: str = ''

class ChatResponse(BaseModel):
    respuesta:       str
    ruta:            str           # NUEVA_CONSULTA | REFINAMIENTO | SOBRE_DATOS | CONVERSACIONAL
    tipo:            str           # consulta | informe | sobre_datos | conversacional | error
    imagenes:        list[str] = []
    ruta_excel:      str = ''
    ruta_docx:       str = ''
    df_creado:       Optional[str] = None   # nombre del df generado en este turno
    turno:           int = 0
    duracion_seg:    float = 0.0
    log_id:          str = ''      # id de la entrada en prompts/prompts_YYYYMMDD.json

# ---------------------------------------------------------------------------
# Endpoint principal
# ---------------------------------------------------------------------------

@app.post('/chat', response_model=ChatResponse)
def chat(req: ChatRequest):
    t_inicio = time.time()
    session_id = req.session_id.strip()
    pregunta   = req.pregunta.strip()

    if not session_id or not pregunta:
        raise HTTPException(status_code=400, detail='session_id y pregunta son requeridos.')

    # Asegurar que la sesión existe y obtener contexto
    sesion   = store.obtener_o_crear(session_id)
    contexto = store.contexto_para_llm(session_id)

    print(f'\n{"="*60}')
    print(f'[SERVER] session={session_id[:8]}... turno={sesion.turno_actual+1}')
    print(f'[SERVER] pregunta: {pregunta}')
    print(f'{"="*60}')

    # ------------------------------------------------------------------
    # 1. Enrutar el mensaje
    # ------------------------------------------------------------------
    clasificacion = clasificar(pregunta, contexto)
    ruta = clasificacion['ruta']
    print(f'[SERVER] ruta: {ruta}')

    # ------------------------------------------------------------------
    # 2. Ejecutar según la ruta
    # ------------------------------------------------------------------
    respuesta_txt = ''
    tipo          = 'consulta'
    imagenes      = []
    ruta_excel    = ''
    ruta_docx     = ''
    df_creado_nom = None
    resultado_sql = None
    sql_usada     = None
    sql_queries   = []   # trazabilidad completa: TODAS las consultas SQL de este turno

    try:
        if ruta in ('NUEVA_CONSULTA', 'REFINAMIENTO'):
            # ----------------------------------------------------------------
            # Llamar al orquestador directamente (función Python, no subprocess)
            # ----------------------------------------------------------------
            contexto_ref = None
            if ruta == 'REFINAMIENTO':
                contexto_ref = _construir_contexto_refinamiento(session_id, clasificacion)

            if es_intencion_informe(pregunta):
                resultado = generar_informe(pregunta)
                tipo      = 'informe'
                ruta_docx = resultado.get('ruta_docx', '')
            else:
                resultado = procesar_consulta(pregunta, contexto_refinamiento=contexto_ref)
                tipo      = resultado.get('tipo', 'consulta')

            respuesta_txt = resultado.get('respuesta', '')
            imagenes      = resultado.get('imagenes', [])
            ruta_excel    = resultado.get('ruta_excel', '')
            resultado_sql = resultado.get('resultado_sql')
            sql_usada     = resultado.get('sql_usada', '')
            sql_queries   = resultado.get('sql_queries', [])

            # Crear DataFrame en sesión si hay resultado SQL válido
            if resultado_sql and resultado_sql.get('success') and resultado_sql.get('rows'):
                df_creado_nom = store.siguiente_nombre_df(session_id)
                df = _resultado_a_df(resultado_sql)
                descripcion = _inferir_descripcion(pregunta, resultado_sql)
                store.crear_df(
                    session_id=session_id,
                    nombre=df_creado_nom,
                    df=df,
                    descripcion=descripcion,
                    sql_original=sql_usada or '',
                )
                print(f'[SERVER] DataFrame creado: {df_creado_nom} ({len(df)} filas)')

        elif ruta == 'SOBRE_DATOS':
            # ----------------------------------------------------------------
            # Responder usando Pandas sobre datos en memoria
            # ----------------------------------------------------------------
            respuesta_txt, tipo, sql_queries = _manejar_sobre_datos(
                session_id, pregunta, clasificacion, contexto
            )

        elif ruta == 'CONVERSACIONAL':
            # ----------------------------------------------------------------
            # Respuesta directa con el LLM usando el historial como contexto
            # ----------------------------------------------------------------
            respuesta_txt = _manejar_conversacional(pregunta, contexto)
            tipo = 'conversacional'
    except Exception as e:
        duracion_error = round(time.time() - t_inicio, 2)
        print(f'[SERVER] ERROR: {e}')
        registrar_prompt(
            pregunta=pregunta,
            tipo='error',
            duracion_seg=duracion_error,
            exito=False,
            prompt_source=req.origen,
        )
        raise HTTPException(status_code=500, detail=str(e))

    # ------------------------------------------------------------------
    # 3. Guardar turno en historial
    # ------------------------------------------------------------------
    resumen = _resumir_respuesta(respuesta_txt)
    periodo = _detectar_periodo(pregunta + ' ' + (sql_usada or ''))

    turno = Turno(
        turno=sesion.turno_actual + 1,
        pregunta=pregunta,
        ruta=ruta,
        df_creado=df_creado_nom,
        df_usado=clasificacion.get('df_relevante'),
        resumen_respuesta=resumen,
        periodo_detectado=periodo,
        filtros_activos=_extraer_filtros(sql_usada or ''),
    )
    store.agregar_turno(session_id, turno)

    duracion = round(time.time() - t_inicio, 2)
    print(f'[SERVER] Completado en {duracion}s — ruta={ruta} tipo={tipo}')

    # ------------------------------------------------------------------
    # 4. Registro centralizado del prompt — único punto para Streamlit y Telegram
    # ------------------------------------------------------------------
    proveedor_llm = os.environ.get('LLM_PROVIDER', '')
    modelo_llm    = os.environ.get(f'{proveedor_llm.upper()}_MODEL', '') if proveedor_llm else ''
    archivos_generados = list(imagenes)
    if ruta_excel:
        archivos_generados.append(ruta_excel)
    if ruta_docx:
        archivos_generados.append(ruta_docx)

    log_id = registrar_prompt(
        pregunta=pregunta,
        tipo=tipo,
        duracion_seg=duracion,
        exito=True,
        archivos_generados=archivos_generados,
        modelo_llm=modelo_llm,
        proveedor_llm=proveedor_llm,
        sql_queries=sql_queries,
        prompt_source=req.origen,
    )

    return ChatResponse(
        respuesta=respuesta_txt,
        ruta=ruta,
        tipo=tipo,
        imagenes=imagenes,
        ruta_excel=ruta_excel,
        ruta_docx=ruta_docx,
        df_creado=df_creado_nom,
        log_id=log_id,
        turno=sesion.turno_actual,
        duracion_seg=duracion,
    )


# ---------------------------------------------------------------------------
# Endpoint de reset de sesión
# ---------------------------------------------------------------------------

@app.post('/reset/{session_id}')
def reset_sesion(session_id: str):
    store.eliminar_sesion(session_id)
    return {'ok': True, 'session_id': session_id, 'mensaje': 'Sesión eliminada.'}


# ---------------------------------------------------------------------------
# Endpoint de feedback
# ---------------------------------------------------------------------------
# Deliberadamente independiente de /chat: solo toca prompts/prompts_*.json vía
# actualizar_feedback(). No pasa por store/agregar_turno, así que el feedback
# nunca entra al historial de sesión ni al contexto que se manda al LLM.

@app.post('/feedback')
def feedback(req: FeedbackRequest):
    if req.feedback not in ('bueno', 'regular', 'malo'):
        raise HTTPException(status_code=400, detail="feedback debe ser 'bueno', 'regular' o 'malo'")
    actualizar_feedback(req.log_id, req.feedback, req.feedback_msg)
    return {'ok': True, 'log_id': req.log_id}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get('/health')
def health():
    return {
        'status':           'ok',
        'sesiones_activas': store.total_sesiones_activas(),
        'version':          '2.0.0',
    }


# ---------------------------------------------------------------------------
# Debug — sesiones activas
# ---------------------------------------------------------------------------

@app.get('/sessions')
def listar_sesiones():
    resumen = []
    for sid, ses in store._sesiones.items():
        dfs_activos = [
            {'nombre': meta.nombre, 'descripcion': meta.descripcion, 'filas': meta.total_filas}
            for meta, _ in ses.dataframes.values()
            if meta.estado == 'activo'
        ]
        resumen.append({
            'session_id':   sid[:8] + '...',
            'turno_actual': ses.turno_actual,
            'historial_n':  len(ses.historial),
            'dfs_activos':  dfs_activos,
        })
    return resumen


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _resultado_a_df(resultado_sql: dict) -> pd.DataFrame:
    """Convierte el dict {columns, rows} del orquestador a un DataFrame pandas."""
    cols = resultado_sql.get('columns', [])
    rows = resultado_sql.get('rows', [])
    df   = pd.DataFrame(rows, columns=cols)
    # Intentar convertir columnas numéricas
    for col in df.columns:
        try:
            df[col] = pd.to_numeric(df[col])
        except (ValueError, TypeError):
            pass
    return df


def _construir_contexto_refinamiento(session_id: str, clasificacion: dict) -> dict | None:
    """
    Construye el dict de contexto para REFINAMIENTO a partir del último df activo.
    """
    dfs = store.listar_dfs_activos(session_id)
    if not dfs:
        return None

    # Usar el df más reciente
    df_meta = max(dfs, key=lambda m: m.turno_creado)

    return {
        'sql_anterior':     df_meta.sql_original,
        'columnas_previas': list(df_meta.columnas.keys()),
        'periodo_previo':   '',
        'filtros_previos':  _extraer_filtros(df_meta.sql_original),
        'descripcion_df':   df_meta.descripcion,
    }


def _manejar_sobre_datos(
    session_id: str,
    pregunta: str,
    clasificacion: dict,
    contexto: dict,
) -> tuple[str, str, list[dict]]:
    """
    Maneja la ruta SOBRE_DATOS.
    1. Intenta ejecutar la operación Pandas sugerida por el enrutador.
    2. Si falla o es insuficiente, hace una consulta adicional a la BD.
    3. Redacta la respuesta con el LLM usando el resultado exacto.

    Devuelve (respuesta, tipo, sql_queries) — sql_queries queda vacía si la
    respuesta salió de Pandas (sin ir a la BD), para trazabilidad.
    """
    df_nombre         = clasificacion.get('df_relevante')
    operacion         = clasificacion.get('operacion_sugerida')
    parametros        = clasificacion.get('parametros_sugeridos') or {}

    resultado_pandas  = None
    descripcion_calc  = ''

    # Intentar ejecutar la operación Pandas
    if df_nombre and operacion:
        entrada = store.obtener_df(session_id, df_nombre)
        if entrada:
            meta, df = entrada
            res_op = ejecutar_operacion(df, operacion, parametros)
            if res_op['exito']:
                resultado_pandas = res_op['resultado']
                descripcion_calc = res_op['descripcion']
                print(f'  [SOBRE_DATOS] Pandas OK: {descripcion_calc}')
            else:
                print(f'  [SOBRE_DATOS] Pandas falló: {res_op["error"]}')

    # Si Pandas no pudo → consulta adicional a BD (igual que agente_analista)
    if resultado_pandas is None:
        print('  [SOBRE_DATOS] Sin resultado Pandas — intentando consulta adicional a BD...')
        resultado_adicional, sql_adicional = _consulta_adicional_sobre_datos(
            pregunta, session_id, contexto
        )
        if resultado_adicional:
            respuesta = _redactar_sobre_datos(
                pregunta, contexto,
                resultado_pandas=None,
                descripcion_calc='',
                resultado_bd=resultado_adicional,
            )
            sql_queries = [{'nombre': 'sobre_datos_complementaria', 'sql': sql_adicional}]
            return respuesta, 'sobre_datos_bd', sql_queries
        else:
            return (
                'No pude encontrar los datos necesarios para responder esta pregunta con la información disponible. '
                '¿Podrías reformularla o hacer una nueva consulta?',
                'error',
                [],
            )

    # Redactar respuesta con el resultado exacto de Pandas
    respuesta = _redactar_sobre_datos(
        pregunta, contexto,
        resultado_pandas=resultado_pandas,
        descripcion_calc=descripcion_calc,
        resultado_bd=None,
    )
    return respuesta, 'sobre_datos', []


def _consulta_adicional_sobre_datos(
    pregunta: str,
    session_id: str,
    contexto: dict,
) -> tuple[dict | None, str]:
    """
    Hace una sola consulta adicional a la BD para cubrir lo que Pandas no pudo.
    Reutiliza el generador del orquestador con contexto de la sesión.

    Devuelve (resultado, sql) — sql viene vacío si no se llegó a generar/ejecutar.
    """
    from src.orquestador import generar_sql_y_validar, ejecutar_consulta
    from src.orquestador import leer_instrucciones, _reglas_gen, _reglas_val

    instrucciones_gen = leer_instrucciones('generador_consultas.md')
    instrucciones_val = leer_instrucciones('validador.md')
    m_gen = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_gen, re.DOTALL)
    m_val = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_val, re.DOTALL)
    system_gen = (m_gen.group(1).strip() if m_gen else instrucciones_gen) + _reglas_gen()
    system_val = (m_val.group(1).strip() if m_val else instrucciones_val) + _reglas_val()

    # Enriquecer el prompt con el contexto de sesión disponible
    dfs = store.listar_dfs_activos(session_id)
    ctx_str = ''
    if dfs:
        df_reciente = max(dfs, key=lambda m: m.turno_creado)
        ctx_str = (
            f'\n\nContexto: el usuario ya tiene datos de "{df_reciente.descripcion}". '
            f'Columnas disponibles: {list(df_reciente.columnas.keys())}. '
            f'Esta consulta adicional debe complementar esos datos.'
        )

    sql = ''
    try:
        sql = generar_sql_y_validar(pregunta + ctx_str, system_gen, system_val)
        resultado = ejecutar_consulta(sql)
        if resultado.get('success') and resultado.get('rows'):
            return resultado, sql
    except Exception as e:
        print(f'  [SOBRE_DATOS] Error en consulta adicional: {e}')

    return None, sql


def _redactar_sobre_datos(
    pregunta: str,
    contexto: dict,
    resultado_pandas,
    descripcion_calc: str,
    resultado_bd: dict | None,
) -> str:
    """Redacta la respuesta conversacional para SOBRE_DATOS usando el LLM."""
    instrucciones_red = leer_instrucciones('redactor_respuesta.md')
    m = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_red, re.DOTALL)
    system_red = m.group(1).strip() if m else instrucciones_red

    historial_str = json.dumps(contexto.get('historial', []), ensure_ascii=False)

    if resultado_pandas is not None:
        prompt = (
            f'El usuario pregunta: "{pregunta}"\n\n'
            f'Resultado calculado con precisión exacta:\n{descripcion_calc}\n\n'
            f'Valor exacto: {resultado_pandas}\n\n'
            f'Contexto de la sesión (últimos turnos): {historial_str}\n\n'
            f'Redacta una respuesta clara y natural usando este resultado exacto. '
            f'No hagas aproximaciones ni estimaciones — usa el número exacto.'
        )
    else:
        prompt = (
            f'El usuario pregunta: "{pregunta}"\n\n'
            f'Datos de la base de datos:\n'
            f'{json.dumps(resultado_bd, ensure_ascii=False, indent=2)[:2000]}\n\n'
            f'Contexto de sesión: {historial_str}\n\n'
            f'Redacta una respuesta clara y natural.'
        )

    return llamar_llm(system_red, prompt, temperatura=0.2)


def _manejar_conversacional(pregunta: str, contexto: dict) -> str:
    """Responde preguntas conversacionales usando el historial como contexto."""
    instrucciones_red = leer_instrucciones('redactor_respuesta.md')
    m = re.search(r'## Instrucciones \(system prompt\)\s*\n(.*?)(?=\n## |\Z)', instrucciones_red, re.DOTALL)
    system_red = m.group(1).strip() if m else instrucciones_red

    historial_str = json.dumps(contexto.get('historial', []), ensure_ascii=False)
    dfs_str       = json.dumps(contexto.get('dataframes_activos', []), ensure_ascii=False)

    prompt = (
        f'El usuario dice: "{pregunta}"\n\n'
        f'Contexto de la sesión (últimos turnos):\n{historial_str}\n\n'
        f'Datos disponibles en memoria:\n{dfs_str}\n\n'
        f'Responde de forma conversacional y natural. '
        f'Si la pregunta pide una explicación o interpretación, usa el contexto de la sesión. '
        f'No inventes datos que no estén en el contexto.'
    )
    return llamar_llm(system_red, prompt, temperatura=0.4)


def _resumir_respuesta(respuesta: str, max_chars: int = 120) -> str:
    """Extrae un resumen de 1-2 líneas de la respuesta para el historial."""
    if not respuesta:
        return ''
    # Tomar primera oración o primeras N chars
    primera_linea = respuesta.split('\n')[0].strip()
    if len(primera_linea) <= max_chars:
        return primera_linea
    return primera_linea[:max_chars].rsplit(' ', 1)[0] + '...'


def _detectar_periodo(texto: str) -> str:
    """Extrae mención de período del texto (año, mes, semana)."""
    meses = {
        'enero': '01', 'febrero': '02', 'marzo': '03', 'abril': '04',
        'mayo': '05', 'junio': '06', 'julio': '07', 'agosto': '08',
        'septiembre': '09', 'octubre': '10', 'noviembre': '11', 'diciembre': '12',
    }
    texto_l = texto.lower()
    for mes, num in meses.items():
        if mes in texto_l:
            anio_match = re.search(r'\b(202[0-9])\b', texto_l)
            anio = anio_match.group(1) if anio_match else '2026'
            return f'{mes} {anio}'
    anio_match = re.search(r'\b(202[0-9])\b', texto_l)
    if anio_match:
        return anio_match.group(1)
    return ''


def _extraer_filtros(sql: str) -> list[str]:
    """Extrae condiciones WHERE del SQL como lista de strings legibles."""
    if not sql:
        return []
    # Buscar bloque WHERE
    m = re.search(r'WHERE\s+(.*?)(?:GROUP BY|ORDER BY|LIMIT|HAVING|$)', sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    where_clause = m.group(1).strip()
    # Dividir por AND/OR y limpiar
    condiciones = re.split(r'\bAND\b|\bOR\b', where_clause, flags=re.IGNORECASE)
    return [c.strip() for c in condiciones if c.strip()][:5]  # Máx 5 filtros


def _inferir_descripcion(pregunta: str, resultado_sql: dict) -> str:
    """Genera una descripción breve del DataFrame basada en la pregunta y columnas."""
    cols = resultado_sql.get('columns', [])
    filas = resultado_sql.get('total_filas', 0)
    pregunta_corta = pregunta[:60].rstrip()
    return f'{pregunta_corta} ({filas} filas, cols: {", ".join(cols[:4])}{"..." if len(cols)>4 else ""})'
