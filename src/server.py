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
from datetime import datetime
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
from src.session_store import store, Turno, MAX_BUSQUEDAS_WEB_SESION
from src.enrutador_sesion import clasificar
from src.orquestador import (
    procesar_consulta,
    generar_informe,
    es_intencion_informe,
    es_intencion_busqueda_web,
    llamar_llm,
    cargar_system_prompt,
    _reglas_gen,
    _es_comando_analisis,
    quiere_excel,
    exportar_excel_multi_hoja,
    analizar_relacion_subconsultas,
)
from src.prompt_logger import registrar_prompt, actualizar_feedback
from src.plantillas import (
    candidatas as _candidatas_plantillas,
    render as _render_plantilla,
    PlantillaRenderError,
)
from tools.tool_pandas import ejecutar_operacion, catalogo_para_llm
from tools.buscar_web import buscar_web

# ---------------------------------------------------------------------------
# Interruptor de la biblioteca de plantillas SQL (ver src/plantillas.py).
# En '0' el pipeline se comporta exactamente como antes de esta funcionalidad
# — útil para revertir rápido o para el A/B de verificación.
# ---------------------------------------------------------------------------
PLANTILLAS_ENABLED = os.getenv('PLANTILLAS_ENABLED', '1').strip() != '0'

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
    # El comando /analisis se detecta ANTES del enrutador: el enrutador es
    # un clasificador LLM ligero que no conoce esta sintaxis y, con historial
    # de sesión, puede leer "/analisis por qué..." como una pregunta
    # conceptual y mandarla a CONVERSACIONAL — donde el comando nunca se
    # ejecuta como análisis profundo.
    # Prefiltro léxico local (sin LLM, ver src/plantillas.py) — solo cuando
    # hay candidatas se le agrega el bloque de plantillas al prompt del
    # enrutador y al schema; en el resto de turnos (la mayoría) el enrutador
    # es idéntico a como era antes de esta funcionalidad, costo 0.
    cands_plantillas = _candidatas_plantillas(pregunta) if PLANTILLAS_ENABLED else []

    if _es_comando_analisis(pregunta)[0]:
        clasificacion = {
            'ruta': 'NUEVA_CONSULTA', 'confianza': 'alta',
            'razon': 'Comando /analisis explícito.',
            'df_relevante': None, 'operacion_sugerida': None,
            'parametros_sugeridos': None, 'contexto_sql': None,
            'sub_preguntas': None,
            'plantilla_sugerida': None, 'plantilla_parametros': None,
            'plantilla_confianza': 'ninguna',
        }
    else:
        clasificacion = clasificar(pregunta, contexto, plantillas_candidatas=cands_plantillas)
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
    origen_sql    = ''   # 'plantilla' | 'plantilla_ref' | 'generador' | '' (rutas sin SQL)

    try:
        if ruta in ('NUEVA_CONSULTA', 'REFINAMIENTO'):
            # ----------------------------------------------------------------
            # Llamar al orquestador directamente (función Python, no subprocess)
            # ----------------------------------------------------------------
            contexto_ref = None
            if ruta == 'REFINAMIENTO':
                contexto_ref = _construir_contexto_refinamiento(session_id, clasificacion)

            sub_preguntas = clasificacion.get('sub_preguntas')
            if not sub_preguntas and es_intencion_informe(pregunta):
                # El informe (.docx) tiene su propio flujo de redacción que
                # todavía no consume contexto_web — no tiene sentido gastar
                # una búsqueda del cupo de la sesión para un resultado que se
                # va a descartar. Ver generar_informe si se quiere sumarlo.
                resultado = generar_informe(pregunta)
                tipo      = 'informe'
                ruta_docx = resultado.get('ruta_docx', '')
            else:
                # Se resuelve UNA sola vez para todo el turno (respeta el
                # límite por sesión) y se reparte según la rama — ver
                # _procesar_multi_consulta para cómo se distribuye entre
                # sub-preguntas.
                bloque_web = _resolver_busqueda_web(
                    pregunta, contexto, session_id, clasificacion.get('necesita_busqueda_web', False)
                )
                if sub_preguntas:
                    # El enrutador detectó varias consultas independientes en
                    # un mismo mensaje (ej. "una tabla de X y otra de Y") —
                    # cada una corre por el pipeline normal de una sola
                    # consulta en vez de forzarlas todas en una sola tabla.
                    # Ver _procesar_multi_consulta.
                    resultado = _procesar_multi_consulta(
                        sub_preguntas, contexto_ref, session_id, pregunta, contexto_web=bloque_web,
                        relacion_tipo=clasificacion.get('relacion_tipo', 'ninguna'),
                        relacion_descripcion=clasificacion.get('relacion_descripcion', ''),
                    )
                    tipo      = 'consulta'
                else:
                    # Plantillas SQL pre-armadas (ver src/plantillas.py) solo
                    # aplican a NUEVA_CONSULTA — REFINAMIENTO depende del SQL
                    # anterior en sesión, que una plantilla fija no conoce.
                    sql_prearmada, plantillas_ref, post_proceso = (
                        _resolver_plantilla(clasificacion, cands_plantillas)
                        if ruta == 'NUEVA_CONSULTA' else ('', None, None)
                    )
                    resultado = procesar_consulta(
                        pregunta, contexto_refinamiento=contexto_ref, contexto_web=bloque_web,
                        sql_prearmada=sql_prearmada, plantillas_referencia=plantillas_ref,
                        post_proceso=post_proceso,
                    )
                    tipo      = resultado.get('tipo', 'consulta')

            respuesta_txt = resultado.get('respuesta', '')
            imagenes      = resultado.get('imagenes', [])
            ruta_excel    = resultado.get('ruta_excel', '')
            resultado_sql = resultado.get('resultado_sql')
            sql_usada     = resultado.get('sql_usada', '')
            sql_queries   = resultado.get('sql_queries', [])
            origen_sql    = resultado.get('origen_sql', '')

            if sub_preguntas:
                df_creado_nom = ', '.join(resultado.get('dfs_creados', [])) or None
            else:
                df_creado_nom = _crear_df_si_aplica(session_id, pregunta, resultado_sql, sql_usada)

        elif ruta == 'SOBRE_DATOS':
            # ----------------------------------------------------------------
            # Responder usando Pandas sobre datos en memoria
            # ----------------------------------------------------------------
            respuesta_txt, tipo, sql_queries = _manejar_sobre_datos(
                session_id, pregunta, clasificacion, contexto
            )

        elif ruta == 'CONVERSACIONAL':
            # ----------------------------------------------------------------
            # Respuesta directa con el LLM usando historial + digest del df
            # activo. Puede escalar a una consulta SQL nueva si le hace falta
            # un dato que no está en sesión (ver _manejar_conversacional).
            # ----------------------------------------------------------------
            bloque_web = _resolver_busqueda_web(
                pregunta, contexto, session_id, clasificacion.get('necesita_busqueda_web', False)
            )
            resultado_conv = _manejar_conversacional(pregunta, contexto, session_id, bloque_web)
            if resultado_conv['escalado']:
                resultado     = resultado_conv['resultado']
                tipo          = resultado.get('tipo', 'consulta')
                respuesta_txt = resultado.get('respuesta', '')
                imagenes      = resultado.get('imagenes', [])
                ruta_excel    = resultado.get('ruta_excel', '')
                resultado_sql = resultado.get('resultado_sql')
                sql_usada     = resultado.get('sql_usada', '')
                sql_queries   = resultado.get('sql_queries', [])
                origen_sql    = resultado.get('origen_sql', '')
                df_creado_nom = _crear_df_si_aplica(session_id, pregunta, resultado_sql, sql_usada)
            else:
                respuesta_txt = resultado_conv['respuesta']
                tipo = 'conversacional'
    except Exception as e:
        duracion_error = round(time.time() - t_inicio, 2)
        print(f'[SERVER] ERROR: {e}')
        log_id_error = registrar_prompt(
            pregunta=pregunta,
            tipo='error',
            duracion_seg=duracion_error,
            exito=False,
            prompt_source=req.origen,
        )
        # Antes: raise HTTPException(500, detail=str(e)) — el usuario veía el
        # texto crudo de la excepción (mensajes de Postgres, tracebacks, etc).
        # Ahora se devuelve una respuesta normal con tipo='error': Streamlit
        # la renderiza igual que cualquier otra respuesta y Telegram entra por
        # el camino de éxito (resultado.get('success', True) es True al no
        # venir la clave 'success' en absoluto). El detalle técnico completo
        # sigue impreso en consola y en prompts/ — solo cambia lo que ve el
        # usuario. Se retorna aquí mismo, sin pasar por agregar_turno, para
        # que un turno fallido no quede en el historial de sesión.
        return ChatResponse(
            respuesta=(
                'Ocurrió un error al procesar tu consulta. Intenta reformular '
                'la pregunta o vuelve a intentarlo en un momento.'
            ),
            ruta=ruta,
            tipo='error',
            log_id=log_id_error,
            turno=sesion.turno_actual,
            duracion_seg=duracion_error,
        )

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
        origen_sql=origen_sql,
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
    # Intentar convertir columnas numéricas. Si una columna viene enteramente
    # NULL (ej. GRUPO_NORM sin dato para esa referencia), pd.to_numeric no
    # falla — convierte None a NaN igual que con cualquier otra columna
    # numérica, disfrazando de "numérica" una columna que en realidad es de
    # texto sin dato. Eso rompe .idxmax()/.idxmin() más adelante (ver
    # SessionStore._calcular_metricas), así que se deja sin convertir.
    for col in df.columns:
        if df[col].isna().all():
            continue
        try:
            df[col] = pd.to_numeric(df[col])
        except (ValueError, TypeError):
            pass
    return df


def _crear_df_si_aplica(
    session_id: str,
    pregunta: str,
    resultado_sql: dict | None,
    sql_usada: str,
) -> str | None:
    """Crea un DataFrame en sesión si resultado_sql es válido. Compartido por
    NUEVA_CONSULTA/REFINAMIENTO y por CONVERSACIONAL cuando escala a SQL."""
    if not (resultado_sql and resultado_sql.get('success') and resultado_sql.get('rows')):
        return None
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
    return df_creado_nom


def _resolver_plantilla(clasificacion: dict, candidatas_locales: list[dict]) -> tuple[str, list[dict] | None, dict | None]:
    """
    Decide qué hacer con el veredicto de plantillas del enrutador (ver
    src/plantillas.py y el bloque 'plantilla_*' de clasificar()).

    Devuelve (sql_prearmada, plantillas_referencia, post_proceso) para pasar
    directo a procesar_consulta():
      - Nivel A (confianza 'alta' + render() exitoso): sql_prearmada no vacío,
        procesar_consulta salta generador y validador por completo.
        post_proceso viene de la plantilla si es 'multi-bloque' (ver
        plantillas/resumen_ventas.json), None si no.
      - Nivel B (confianza 'media', o 'alta' con render() fallido):
        plantillas_referencia = [la plantilla sugerida], se inyecta como
        ejemplo few-shot pero el generador y el validador siguen corriendo.
      - Nivel C (confianza 'ninguna', o sin candidatas): ('', None, None) —
        sin cambios respecto al flujo de siempre.

    Conservador por diseño: cualquier duda (plantilla no encontrada en las
    candidatas locales, render() que falla) cae a Nivel B o C, nunca fuerza
    Nivel A — una plantilla mal aplicada devuelve números incorrectos en
    silencio, que es peor que gastar los tokens del flujo normal.
    """
    plantilla_id = clasificacion.get('plantilla_sugerida')
    confianza    = clasificacion.get('plantilla_confianza', 'ninguna')
    if not plantilla_id or confianza == 'ninguna':
        return '', None, None

    plantilla = next((p for p in candidatas_locales if p['id'] == plantilla_id), None)
    if plantilla is None:
        # El enrutador sugirió un id que no está en las candidatas que le
        # ofrecimos — no debería pasar (_parsear_respuesta ya valida contra
        # esa misma lista), pero por si acaso no se ejecuta nada a ciegas.
        return '', None, None

    if confianza == 'alta':
        try:
            sql = _render_plantilla(plantilla, clasificacion.get('plantilla_parametros') or {})
            print(f'[SERVER] Plantilla "{plantilla_id}" — Nivel A (ejecución directa)')
            return sql, None, plantilla.get('post_proceso')
        except PlantillaRenderError as e:
            print(f'[SERVER] Plantilla "{plantilla_id}" confianza alta pero render() falló ({e}) — cae a Nivel B')
            # cae a Nivel B abajo

    print(f'[SERVER] Plantilla "{plantilla_id}" — Nivel B (referencia para el generador)')
    return '', [plantilla], None


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


def _procesar_multi_consulta(
    sub_preguntas: list[str],
    contexto_ref: dict | None,
    session_id: str,
    pregunta_original: str = '',
    contexto_web: str = '',
    relacion_tipo: str = 'ninguna',
    relacion_descripcion: str = '',
) -> dict:
    """
    Ejecuta cada sub-pregunta detectada por el enrutador (clasificacion['sub_preguntas'])
    como una consulta independiente, una por una, por el pipeline normal de una
    sola consulta (procesar_consulta) — en vez de forzar varios resultados en
    una sola tabla. Cada sub-pregunta genera su propio DataFrame de sesión.

    Trazabilidad: cada entrada de sql_queries queda prefijada con el número de
    sub-consulta y su texto (ej. "sub_1/2 [top 10 productos]: consulta_principal"),
    así el log persistido (prompts_YYYYMMDD.json) distingue de qué parte del
    mensaje original salió cada SQL. La consola imprime lo mismo en tiempo real
    con separadores, porque procesar_consulta ya hace print() de cada SQL que
    genera pero SIEMPRE bajo la misma etiqueta genérica "consulta_principal" —
    sin este encabezado, dos sub-consultas seguidas se ven indistinguibles en
    el log de la terminal.

    Excel: cada procesar_consulta() corre con permitir_excel_individual=False
    — si cada sub-pregunta generara su propio archivo, solo el primero
    sobrevivía (el resto quedaba huérfano en disco, sin referencia en la
    respuesta). En su lugar, al final se decide con el mismo criterio de
    siempre (quiere_excel: >100 filas o "excel" explícito en el texto) cuáles
    sub-preguntas quieren Excel, y se genera UN solo archivo con una hoja por
    cada una (exportar_excel_multi_hoja).

    Limitación conocida: resultado_sql/sql_usada al final del dict quedan como
    los de la ÚLTIMA sub-pregunta (no hay un "resultado principal" cuando hay
    varios) — solo importan para el flujo de REFINAMIENTO de un turno futuro
    que no especifique a cuál de los N resultados se refiere; no afecta la
    respuesta que ve el usuario en este turno, que sí incluye las N tablas.

    contexto_web (si no es '') es el bloque ya resuelto por
    _resolver_busqueda_web para el mensaje completo — se adjunta solo a la(s)
    sub-pregunta(s) cuyo texto matchea es_intencion_busqueda_web (ej. la parte
    de "competencia" en "ventas de camisetas y compáralas con la competencia"
    partida en dos sub-preguntas); si ninguna sub-pregunta matchea por texto
    propio pero el mensaje original sí necesitaba web, se adjunta a la última
    (evita repetir el bloque en cada tabla cuando claramente aplica a una sola).

    relacion_tipo/relacion_descripcion vienen del enrutador (clasificacion['relacion_tipo']) —
    una hipótesis de que una sub-pregunta puede explicar/compararse/depender de otra (ver
    DETECCIÓN DE RELACIÓN ENTRE SUB-PREGUNTAS en enrutador_sesion.py). Si no es 'ninguna', al
    final —con TODOS los resultados ya calculados— se llama a analizar_relacion_subconsultas
    (OpenRouter) para contrastar la hipótesis contra las cifras reales y, si se sostiene, se
    agrega como una sección extra al final de la respuesta. Es best-effort: si falla o no hay
    cliente configurado, se omite sin afectar el resto de la respuesta.
    """
    respuestas       = []
    sub_resultados_texto = []  # [{'pregunta', 'respuesta'}] para analizar_relacion_subconsultas
    imagenes         = []
    sql_queries      = []
    dfs_creados      = []
    candidatos_excel = []  # [{'pregunta': sub, 'resultado': resultado_sql}]
    resultado_sql    = None
    sql_usada        = None
    n = len(sub_preguntas)

    print(f'[SERVER] Enrutador detectó {n} sub-consultas en el mismo mensaje:')
    for i, sub in enumerate(sub_preguntas, start=1):
        print(f'  {i}. {sub}')

    matches_web = [es_intencion_busqueda_web(s) for s in sub_preguntas]
    ninguna_matchea_propia = bool(contexto_web) and not any(matches_web)

    for i, sub in enumerate(sub_preguntas, start=1):
        print(f'\n{"-"*60}')
        print(f'[SERVER] Sub-consulta {i}/{n}: "{sub}"')
        print(f'{"-"*60}')

        web_sub = contexto_web if (matches_web[i - 1] or (ninguna_matchea_propia and i == n)) else ''
        resultado = procesar_consulta(
            sub, contexto_refinamiento=contexto_ref, permitir_excel_individual=False,
            contexto_web=web_sub,
        )
        resultado_sql_sub = resultado.get('resultado_sql')

        filas = (resultado_sql_sub or {}).get('total_filas', 0)
        print(f'[SERVER] Sub-consulta {i}/{n} completada — {filas} fila(s).')

        respuesta_sub = resultado.get('respuesta', '')
        respuestas.append(f'### {i}. {sub}\n\n{respuesta_sub}')
        sub_resultados_texto.append({'pregunta': sub, 'respuesta': respuesta_sub})
        imagenes.extend(resultado.get('imagenes', []))

        if quiere_excel(sub, resultado_sql_sub or {}):
            candidatos_excel.append({'pregunta': sub, 'resultado': resultado_sql_sub})

        for q in resultado.get('sql_queries', []):
            sql_queries.append({
                'nombre': f'sub_{i}/{n} [{sub}]: {q["nombre"]}',
                'sql':    q['sql'],
            })

        df_nom = _crear_df_si_aplica(
            session_id, sub, resultado_sql_sub, resultado.get('sql_usada', ''),
        )
        if df_nom:
            dfs_creados.append(df_nom)

        resultado_sql = resultado_sql_sub
        sql_usada     = resultado.get('sql_usada')

    respuesta_final = '\n\n---\n\n'.join(respuestas)

    print(f'[SERVER] relacion_tipo recibido del enrutador: {relacion_tipo}'
          + (f' — {relacion_descripcion}' if relacion_descripcion else ''))
    if relacion_tipo != 'ninguna':
        texto_relacion = analizar_relacion_subconsultas(
            pregunta_original, relacion_tipo, relacion_descripcion, sub_resultados_texto,
        )
        if texto_relacion:
            respuesta_final += f'\n\n---\n\n**Relación entre los resultados:** {texto_relacion}'
            print(f'[SERVER] Análisis relacional agregado ({len(texto_relacion)} caracteres).')
        else:
            print('[SERVER] Análisis relacional omitido (sin cliente/error/no confirmable).')

    ruta_excel = ''
    if candidatos_excel:
        print(f'[SERVER] {len(candidatos_excel)}/{n} sub-consulta(s) piden Excel — '
              f'generando archivo combinado de {len(candidatos_excel)} hoja(s)...')
        salida = exportar_excel_multi_hoja(candidatos_excel, pregunta_original or 'consulta')
        ruta_excel = salida['ruta']
        if ruta_excel:
            hojas_txt = ', '.join(salida['nombres_hojas'])
            respuesta_final += (
                f'\n\n---\n\n**Excel generado** ({len(salida["nombres_hojas"])} hoja(s): '
                f'{hojas_txt}): {ruta_excel}'
            )

    print(f'\n[SERVER] Multi-consulta completada: {n} sub-consultas, '
          f'{len(dfs_creados)} DataFrame(s) creado(s), {len(sql_queries)} SQL registrada(s), '
          f'excel={"si" if ruta_excel else "no"}.')

    return {
        'respuesta':     respuesta_final,
        'imagenes':      imagenes,
        'ruta_excel':    ruta_excel,
        'sql_queries':   sql_queries,
        'dfs_creados':   dfs_creados,
        'resultado_sql': resultado_sql,
        'sql_usada':     sql_usada,
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
    from src.orquestador import _reglas_gen, _reglas_val

    system_gen = cargar_system_prompt('generador_consultas.md', ['Instrucciones (system prompt)']) + _reglas_gen()
    system_val = cargar_system_prompt('validador.md', ['Instrucciones (system prompt)']) + _reglas_val()

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
    system_red = cargar_system_prompt('redactor_respuesta.md', ['Instrucciones (system prompt)'])

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


_MARCADOR_CALCULAR = '[[CALCULAR]]'
_MARCADOR_ESCALAR  = '[[ESCALAR_A_CONSULTA]]'


def _resolver_busqueda_web(
    pregunta: str, contexto: dict, session_id: str, necesita_web_router: bool,
) -> str:
    """
    Decide si corresponde salir a internet — combinando la señal por regex
    (es_intencion_busqueda_web, frases explícitas tipo "según internet") con
    la señal del enrutador LLM (clasificacion['necesita_busqueda_web'], que
    puede detectar intención web aunque la ruta principal sea NUEVA_CONSULTA
    o REFINAMIENTO, ej: "ventas de camisetas y compáralas con la competencia").

    Aplica el límite MAX_BUSQUEDAS_WEB_SESION antes de llamar a Tavily (cada
    llamada es una petición paga) y devuelve el bloque de texto ya formateado
    para inyectar en el prompt del redactor ('' si no aplica ninguna búsqueda).
    """
    necesita_web_regex = es_intencion_busqueda_web(pregunta)
    necesita_web = necesita_web_regex or necesita_web_router
    print(
        f'[BUSQUEDA_WEB] regex={"SI" if necesita_web_regex else "NO"} '
        f'enrutador={"SI" if necesita_web_router else "NO"} → '
        f'{"SI" if necesita_web else "NO"} — pregunta: "{pregunta}"'
    )
    if not necesita_web:
        return ''

    if not store.puede_buscar_web(session_id):
        print(
            f'[BUSQUEDA_WEB] límite de {MAX_BUSQUEDAS_WEB_SESION} búsquedas '
            f'alcanzado para session={session_id[:8]}... — se omite la búsqueda.'
        )
        return (
            '\n\nBúsqueda web: se alcanzó el límite de búsquedas externas '
            'permitidas para esta sesión. Responde solo con los datos internos '
            'disponibles y acláraselo brevemente al usuario si es relevante.\n'
        )

    query_web = _generar_query_busqueda_web(pregunta, contexto)
    print(f'[BUSQUEDA_WEB] query generada: "{query_web}"')
    resultados_web = buscar_web(query_web)
    store.registrar_busqueda_web(session_id)
    print(f'[BUSQUEDA_WEB] {len(resultados_web)} resultado(s)')
    for i, r in enumerate(resultados_web):
        print(f'  [{i}] {r.get("titulo", "")[:80]} — {r.get("url", "")}')
        print(f'      contenido[:200]: {r.get("contenido", "")[:200]!r}')

    if resultados_web:
        return (
            f'\n\nResultados de búsqueda web (fuentes externas, query: "{query_web}"):\n'
            f'{json.dumps(resultados_web, ensure_ascii=False)}\n'
        )
    return f'\n\nBúsqueda web (query: "{query_web}"): no se encontraron resultados.\n'


def _manejar_conversacional(pregunta: str, contexto: dict, session_id: str, bloque_web: str = '') -> dict:
    """
    Responde preguntas conversacionales usando el historial + un digest
    estadístico sobre TODAS las filas del df activo (no solo metadata).

    Si el redactor determina que le falta un cálculo exacto sobre el df, o
    directamente datos que no están en sesión, responde con un marcador en
    vez de pedirle al usuario que repregunte:
      - [[CALCULAR]] {"operacion": "...", "parametros": {...}}  → se ejecuta
        con tool_pandas sobre el df completo y se re-redacta con el resultado.
      - [[ESCALAR_A_CONSULTA]] <pregunta reformulada>  → se dispara el
        pipeline de consulta SQL completo (procesar_consulta).

    Retorna:
      {'escalado': False, 'respuesta': str}
      {'escalado': True, 'resultado': dict}   # dict de procesar_consulta
    """
    system_red = cargar_system_prompt(
        'redactor_respuesta.md',
        ['Instrucciones (system prompt)', 'Modo conversacional (sin consulta SQL nueva)'],
    )
    system_red += '\n\n' + catalogo_para_llm()

    historial_str = json.dumps(contexto.get('historial', []), ensure_ascii=False)
    dfs_str       = json.dumps(contexto.get('dataframes_activos', []), ensure_ascii=False)

    digest_str  = ''
    df_reciente = None
    dfs_meta = store.listar_dfs_activos(session_id)
    if dfs_meta:
        df_reciente = max(dfs_meta, key=lambda m: m.turno_creado)
        digest = store.calcular_digest(session_id, df_reciente.nombre)
        if digest:
            digest_str = (
                f'\n\nDigest estadístico completo de "{df_reciente.nombre}" '
                f'(sobre las {df_reciente.total_filas} filas reales, no una muestra):\n'
                f'{json.dumps(digest, ensure_ascii=False, indent=2)}\n'
            )

    prompt = (
        f'El usuario dice: "{pregunta}"\n\n'
        f'Contexto de la sesión (últimos turnos):\n{historial_str}\n\n'
        f'Datos disponibles en memoria (metadata):\n{dfs_str}\n'
        f'{digest_str}'
        f'{bloque_web}\n'
        f'Responde siguiendo las reglas del modo conversacional.'
    )
    respuesta_llm = llamar_llm(system_red, prompt, temperatura=0.4).strip()

    if respuesta_llm.startswith(_MARCADOR_ESCALAR):
        pregunta_efectiva = respuesta_llm[len(_MARCADOR_ESCALAR):].strip() or pregunta
        print(f'[CONVERSACIONAL] Escalando a consulta SQL: "{pregunta_efectiva}"')
        resultado = procesar_consulta(pregunta_efectiva)
        return {'escalado': True, 'resultado': resultado}

    if respuesta_llm.startswith(_MARCADOR_CALCULAR) and df_reciente:
        cuerpo = respuesta_llm[len(_MARCADOR_CALCULAR):].strip()
        try:
            peticion = json.loads(cuerpo)
        except json.JSONDecodeError:
            peticion = None
        entrada_df = store.obtener_df(session_id, df_reciente.nombre) if peticion else None
        if entrada_df:
            _, df_real = entrada_df
            print(f'[CONVERSACIONAL] Calculando: {peticion.get("operacion")} {peticion.get("parametros")}')
            resultado_calc = ejecutar_operacion(
                df_real, peticion.get('operacion', ''), peticion.get('parametros', {}) or {}
            )
            if resultado_calc.get('exito'):
                prompt_final = (
                    f'El usuario dice: "{pregunta}"\n\n'
                    f'Pediste calcular "{peticion.get("operacion")}" sobre "{df_reciente.nombre}" '
                    f'y el resultado exacto es:\n{resultado_calc.get("descripcion")}\n\n'
                    f'Redacta la respuesta final usando este resultado exacto, siguiendo las '
                    f'reglas del modo conversacional. No uses marcadores esta vez.'
                )
                return {'escalado': False, 'respuesta': llamar_llm(system_red, prompt_final, temperatura=0.3)}
            print(f'[CONVERSACIONAL] Cálculo falló: {resultado_calc.get("error")} — escalando a consulta SQL')
            resultado = procesar_consulta(pregunta)
            return {'escalado': True, 'resultado': resultado}

    return {'escalado': False, 'respuesta': respuesta_llm}


def _generar_query_busqueda_web(pregunta: str, contexto: dict) -> str:
    """
    Convierte la pregunta conversacional en una query de búsqueda web específica,
    usando el LLM para incorporar términos concretos del contexto de la sesión
    (categorías/referencias/líneas mencionadas, cifras top, período) — en vez de
    pasar la pregunta del usuario tal cual a Tavily, que suele ser genérica
    ("¿esto es congruente con las tendencias de LatAm?") y no menciona lo que
    se está discutiendo (ej: qué categorías, qué período).
    """
    historial = contexto.get('historial', [])[-3:]
    dfs = contexto.get('dataframes_activos', [])

    lineas_contexto = []
    for t in historial:
        lineas_contexto.append(f'- Usuario preguntó: "{t.get("pregunta", "")}" → Resultado: {t.get("resumen", "")}')
    for df in dfs:
        metricas = df.get('metricas', {})
        metricas_str = ', '.join(f'{k}={v}' for k, v in metricas.items()) if metricas else ''
        lineas_contexto.append(
            f'- Datos en memoria ({df.get("nombre", "")}): {df.get("descripcion", "")}'
            + (f' | métricas: {metricas_str}' if metricas_str else '')
        )
    contexto_str = '\n'.join(lineas_contexto) if lineas_contexto else '(sin contexto previo en la sesión)'

    print(f'[CONVERSACIONAL] contexto para query_web:\n{contexto_str}')

    anio_actual = datetime.now().year

    system = (
        'Conviertes la pregunta de un usuario en UNA sola query de búsqueda web, concreta y específica, '
        'para el sector TEXTIL Y DE MODA (la empresa es Creytex, fabricante/proveedor de prendas de vestir '
        'para el retail colombiano — Almacenes Éxito). La query SIEMPRE debe quedar ubicada en ese sector: '
        'agrega términos como "moda", "textil", "confección", "prendas de vestir" o "retail de moda" según '
        'aplique — nunca generes una query genérica de economía/mercado que podría devolver resultados de '
        'cualquier industria. '
        'Ubica también la región: usa Colombia por defecto (el negocio opera ahí) salvo que la pregunta o el '
        'contexto mencionen explícitamente otro país o mercado (ej. "mercado chino" → producción/exportación '
        'textil de China e impacto en importaciones a Colombia, no economía china en general). '
        f'Ancla la query al año {anio_actual} (o "{anio_actual}-{str(anio_actual + 1)[2:]}" si se trata de una '
        'temporada/colección) para priorizar información reciente — la búsqueda ya filtra resultados del '
        'último año, pero incluir el año en el texto ayuda a que el resultado sea sobre el presente, no una '
        'noticia vieja de otro año que solo coincide en palabras clave. '
        'Usa el contexto interno entregado (categorías, líneas de producto, referencias, cifras, período, país/región) '
        'para reemplazar referencias vagas como "esto", "las tendencias internas" o "eso" por los términos reales — '
        'ej. si el contexto menciona "camisetas manga corta" o "línea Dama Deportivo", inclúyelo tal cual en vez '
        'de generalizarlo a "ropa". '
        'NUNCA incluyas cifras de ventas internas ni el nombre de la empresa en la query — la query es para buscar '
        'información pública externa (tendencias de mercado, sector, competencia), no datos propios. '
        'Responde SOLO con la query de búsqueda en texto plano, sin comillas ni explicación, máximo 20 palabras.'
    )
    prompt = (
        f'Pregunta del usuario: "{pregunta}"\n\n'
        f'Año actual: {anio_actual}\n\n'
        f'Contexto interno de la sesión (úsalo solo para identificar QUÉ términos concretos buscar, '
        f'no los incluyas como datos en la query):\n{contexto_str}\n\n'
        f'Genera la query de búsqueda web.'
    )

    try:
        query = llamar_llm(system, prompt, temperatura=0.2).strip().strip('"').strip("'")
        if query:
            return query
    except Exception as e:
        print(f'[CONVERSACIONAL] Error generando query_web con LLM: {e!r} — usando fallback simple')

    return _construir_query_busqueda_web_fallback(pregunta, contexto)


def _construir_query_busqueda_web_fallback(pregunta: str, contexto: dict) -> str:
    """
    Fallback sin LLM: concatena la pregunta con el turno y df más recientes.
    Se agrega "sector textil moda Colombia <año actual>" al final para que,
    aun sin el LLM afinando la query, Tavily no devuelva resultados de una
    industria distinta al negocio ni de un año viejo que solo coincide en
    palabras clave (ver _generar_query_busqueda_web para el criterio completo;
    el filtro duro de recencia va en tools/buscar_web.py vía time_range).
    """
    partes = [pregunta]
    historial = contexto.get('historial', [])
    if historial:
        partes.append(historial[-1].get('resumen', '') or historial[-1].get('pregunta', ''))
    dfs = contexto.get('dataframes_activos', [])
    if dfs:
        partes.append(dfs[-1].get('descripcion', ''))
    partes.append(f'sector textil moda Colombia {datetime.now().year}')
    return ' '.join(p for p in partes if p).strip()


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
