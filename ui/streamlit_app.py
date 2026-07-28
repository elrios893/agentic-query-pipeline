import streamlit as st
import subprocess
import re
import sys
import os
import time
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / '.env')

from ui.db import init_db, guardar_item, listar_items, obtener_item
from ui.prompt_logger import registrar_prompt, actualizar_feedback

ORQUESTADOR = BASE_DIR / 'src' / 'orquestador.py'

init_db()

st.set_page_config(
    page_title='Creytex — Consultor de Ventas',
    page_icon=':material/bar_chart:',
    layout='wide',
)

SUGERENCIAS = {
    ':material/assessment: Ventas por departamento': 'ventas por departamento',
    ':material/bar_chart: Graficame top 5 departamentos': 'graficame top 5 departamentos por ventas',
    ':material/description: Genera un informe de ventas': 'genera un informe de ventas completo',
    ':material/sell: Talla mas vendida': 'cual es la talla que mas se vende',
}

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
st.session_state.setdefault('messages', [])
st.session_state.setdefault('vista', 'chat')
st.session_state.setdefault('item_id', None)
st.session_state.setdefault('carpeta', 'informes')
st.session_state.setdefault('ultimo_prompt_id', None)
st.session_state.setdefault('feedback_dado', False)

# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------
def cb_ver_item(item_id: int):
    st.session_state.vista = 'item'
    st.session_state.item_id = item_id

def cb_volver_chat():
    st.session_state.vista = 'chat'
    st.session_state.item_id = None

# ---------------------------------------------------------------------------
# Sidebar — historial
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown('### :material/folder: Historial')

    carpeta = st.segmented_control(
        'Carpeta',
        options=['informes', 'graficos'],
        format_func=lambda x: ':material/description: Informes' if x == 'informes' else ':material/bar_chart: Graficos',
        default=st.session_state.carpeta,
        key='carpeta_ctrl',
        label_visibility='collapsed',
    )

    carpeta = carpeta or 'informes'
    st.session_state.carpeta = carpeta
    tipo_db = 'informe' if carpeta == 'informes' else 'grafico'
    items = listar_items(tipo_db)

    if not items:
        st.caption('(vacio)')
    else:
        for item in items:
            fecha = item['created_at'][:10]
            with st.container(border=False):
                st.caption(fecha)
                if st.button(item['titulo'], key=f'btn_{item["id"]}', use_container_width=True):
                    cb_ver_item(item['id'])
                    st.rerun()

# ---------------------------------------------------------------------------
# Vista item (informe o grafico)
# ---------------------------------------------------------------------------
if st.session_state.vista == 'item' and st.session_state.item_id:
    item = obtener_item(st.session_state.item_id)
    if item:
        ruta_completa = BASE_DIR / item['ruta']
        col_back, col_dl = st.columns([6, 1])
        with col_back:
            st.button(':material/arrow_back: Volver al chat', on_click=cb_volver_chat)

        if item['tipo'] == 'informe':
            ruta_docx = ruta_completa.with_suffix('.docx')
            if not ruta_docx.exists():
                stem = ruta_completa.stem
                ruta_docx = ruta_completa.parent / f'Informe_{stem[len("informe_"):]}.docx'
            with col_dl:
                if ruta_docx.exists():
                    with open(ruta_docx, 'rb') as f:
                        st.download_button(
                            label=':material/download: Word',
                            data=f.read(),
                            file_name=ruta_docx.name,
                            mime='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                        )
            st.divider()
            if ruta_completa.exists():
                md_text = ruta_completa.read_text(encoding='utf-8')
                img_pattern = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
                partes = img_pattern.split(md_text)
                idx = 0
                buffer_md = []
                while idx < len(partes):
                    if idx % 3 == 0:
                        buffer_md.append(partes[idx])
                    elif idx % 3 == 1:
                        bloque = ''.join(buffer_md).strip()
                        if bloque:
                            st.markdown(bloque)
                        buffer_md = []
                        alt = partes[idx]
                        ruta_img_str = partes[idx + 1] if idx + 1 < len(partes) else ''
                        ruta_img = BASE_DIR / ruta_img_str.replace('\\', '/')
                        if ruta_img.exists():
                            st.image(str(ruta_img), caption=alt or None)
                        else:
                            st.caption(f'[Imagen no disponible: {ruta_img_str}]')
                        idx += 1
                    idx += 1
                bloque = ''.join(buffer_md).strip()
                if bloque:
                    st.markdown(bloque)
            else:
                st.warning(f'Archivo no encontrado: {item["ruta"]}')

        elif item['tipo'] == 'grafico':
            st.divider()
            if ruta_completa.exists():
                st.image(str(ruta_completa))
                with open(ruta_completa, 'rb') as f:
                    st.download_button(
                        label=':material/download: Descargar imagen',
                        data=f.read(),
                        file_name=ruta_completa.name,
                        mime='image/png',
                    )
            else:
                st.warning(f'Imagen no encontrada: {item["ruta"]}')
    else:
        st.warning('Item no encontrado.')
        cb_volver_chat()
    st.stop()

# ---------------------------------------------------------------------------
# Vista chat
# ---------------------------------------------------------------------------
def _detectar_tipo(pregunta: str, es_informe: bool) -> str:
    if es_informe:
        return 'informe'
    patrones = re.compile(r'(grafic\w*|chart|visualiza\w*|barras|torta|linea|tendencia)', re.IGNORECASE)
    return 'grafico' if patrones.search(pregunta) else 'consulta'


def _extraer_sql_queries(stdout: str) -> list[dict]:
    """
    Extrae las consultas SQL ejecutadas del stdout del orquestador.
    El orquestador emite lineas con el patron:
        SQL_LOG::<nombre>::<sql en una sola linea>
    Devuelve lista de {"nombre": str, "sql": str}.
    """
    queries = []
    for linea in stdout.splitlines():
        if linea.startswith('SQL_LOG::'):
            partes = linea.split('::', 2)
            if len(partes) == 3:
                nombre = partes[1].strip()
                sql = partes[2].strip()
                queries.append({'nombre': nombre, 'sql': sql})
    return queries


def ejecutar_pregunta(pregunta: str) -> tuple[str, list[str], bool, bool]:
    t0 = time.time()
    try:
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUTF8'] = '1'
        resultado = subprocess.run(
            ['python', '-X', 'utf8', str(ORQUESTADOR), pregunta],
            capture_output=True, text=True, timeout=600,
            encoding='utf-8', errors='replace', env=env,
        )
        salida = resultado.stdout

        if resultado.returncode != 0 and not salida.strip():
            error = resultado.stderr
            registrar_prompt(pregunta, 'consulta', time.time() - t0, False)
            return f'Error: {error or f"Proceso termino con codigo {resultado.returncode}"}', [], False, False

        guardados = 0
        archivos = []
        es_informe = bool(re.search(r'====+\s*INFORME\s*====+', salida))

        for m in re.finditer(r'Markdown guardado:\s*(.+)', salida):
            ruta = m.group(1).strip()
            r = Path(ruta)
            try:
                ruta_rel = r.relative_to(BASE_DIR).as_posix()
                guardar_item('informe', r.stem, ruta_rel, pregunta)
                archivos.append(ruta_rel)
                guardados += 1
            except ValueError:
                pass

        for m in re.finditer(r'OK:\s*(\S+\.png)', salida):
            ruta_raw = m.group(1).strip()
            ruta_limpia = ruta_raw.replace('\\', '/')
            g = BASE_DIR / ruta_limpia
            if g.exists():
                titulo = g.stem.replace('chart_', '', 1)
                guardar_item('grafico', titulo, ruta_limpia, pregunta)
                archivos.append(ruta_limpia)
                guardados += 1

        match_resp = re.search(r'====+\s*RESPUESTA\s*====+\s*(.*)', salida, re.DOTALL)

        if es_informe:
            respuesta = '_Informe generado y guardado en el historial. Abrelo desde la carpeta **Informes** en el panel izquierdo._'
            imagenes = []
        else:
            contenido = match_resp.group(1).strip() if match_resp else salida.strip()
            respuesta = contenido
            imagenes = re.findall(r'!\[.*?\]\((.*?)\)', respuesta)
            for ruta_img in imagenes:
                ruta_img_norm = ruta_img.replace('\\', '/')
                g = BASE_DIR / ruta_img_norm
                if g.exists():
                    titulo = g.stem.replace('chart_', '', 1)
                    guardar_item('grafico', titulo, ruta_img_norm, pregunta)
                    archivos.append(ruta_img_norm)
                    guardados += 1

        proveedor = os.environ.get('LLM_PROVIDER', '')
        modelo = os.environ.get(f'{proveedor.upper()}_MODEL', '')
        tipo = _detectar_tipo(pregunta, es_informe)
        sql_queries = _extraer_sql_queries(salida)
        prompt_id = registrar_prompt(pregunta, tipo, time.time() - t0, True, archivos, modelo, proveedor, sql_queries)
        st.session_state.ultimo_prompt_id = prompt_id
        st.session_state.feedback_dado = False

        return respuesta, imagenes, bool(guardados), es_informe

    except subprocess.TimeoutExpired:
        registrar_prompt(pregunta, 'consulta', time.time() - t0, False)
        return 'La consulta tardo demasiado (mas de 10 minutos). Intenta con una pregunta mas simple.', [], False, False
    except Exception as e:
        registrar_prompt(pregunta, 'consulta', time.time() - t0, False)
        return f'Error inesperado: {str(e)}', [], False, False


def _widget_feedback(prompt_id: str):
    """Widget de feedback debajo del último mensaje del asistente."""
    st.divider()
    st.caption(':material/rate_review: ¿Fue util esta respuesta?')

    # Inicializar estado del tipo elegido
    key_tipo = f'fb_tipo_{prompt_id}'
    st.session_state.setdefault(key_tipo, None)

    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        if st.button(':material/thumb_up: Buena', key=f'fb_bueno_{prompt_id}'):
            st.session_state[key_tipo] = 'bueno'
    with col2:
        if st.button(':material/thumbs_up_down: Regular', key=f'fb_regular_{prompt_id}'):
            st.session_state[key_tipo] = 'regular'
    with col3:
        if st.button(':material/thumb_down: Mala', key=f'fb_malo_{prompt_id}'):
            st.session_state[key_tipo] = 'malo'

    tipo_elegido = st.session_state.get(key_tipo)
    if tipo_elegido:
        msg = st.text_input(
            'Comentario opcional:',
            key=f'fb_msg_{prompt_id}',
            placeholder='¿Qué estuvo bien o qué faltó?',
            label_visibility='collapsed',
        )
        if st.button(':material/send: Enviar feedback', key=f'fb_enviar_{prompt_id}'):
            actualizar_feedback(prompt_id, tipo_elegido, msg)
            st.session_state.feedback_dado = True
            st.toast('Gracias por tu feedback', icon=None)
            st.rerun()


# ---------------------------------------------------------------------------
# Sugerencias iniciales
# ---------------------------------------------------------------------------
if not st.session_state.messages:
    st.title('Consultor Inteligente de Ventas')
    st.markdown('Pregunta sobre ventas en lenguaje natural.')
    seleccion = st.pills(
        'Sugerencias:',
        list(SUGERENCIAS.keys()),
        label_visibility='collapsed',
    )
    if seleccion:
        prompt = SUGERENCIAS[seleccion]
        st.session_state.messages.append({'role': 'user', 'content': prompt})
        
        with st.chat_message('user'):
            st.markdown(prompt)

        with st.chat_message('assistant'):
            with st.spinner('Analizando...'):
                respuesta, imagenes, hay_nuevos, es_informe = ejecutar_pregunta(prompt)
            st.markdown(respuesta)
            for ruta in imagenes:
                ruta_abs = BASE_DIR / ruta
                if ruta_abs.exists():
                    st.image(str(ruta_abs))
                else:
                    st.caption(f'[Grafico no encontrado: {ruta}]')

        st.session_state.messages.append({'role': 'assistant', 'content': respuesta, 'imagenes': imagenes})
        if hay_nuevos:
            carpeta_nombre = 'Informes' if es_informe else 'Graficos'
            st.toast(f'Guardado en {carpeta_nombre}', icon=None)
        st.rerun()
        
# ---------------------------------------------------------------------------
# Historial de mensajes
# ---------------------------------------------------------------------------
for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg['role']):
        st.markdown(msg['content'])
        for ruta in msg.get('imagenes', []):
            ruta_abs = BASE_DIR / ruta
            if ruta_abs.exists():
                st.image(str(ruta_abs))
            else:
                st.caption(f'[Grafico no encontrado: {ruta}]')

        # Feedback solo en el último mensaje del asistente, si no se ha dado aún
        es_ultimo = (i == len(st.session_state.messages) - 1)
        if (msg['role'] == 'assistant'
                and es_ultimo
                and st.session_state.ultimo_prompt_id
                and not st.session_state.feedback_dado):
            _widget_feedback(st.session_state.ultimo_prompt_id)

# ---------------------------------------------------------------------------
# Input del chat
# ---------------------------------------------------------------------------
if prompt := st.chat_input('Escribe tu pregunta sobre ventas...', submit_mode='disable'):
    st.session_state.messages.append({'role': 'user', 'content': prompt})
    with st.chat_message('user'):
        st.markdown(prompt)

    with st.chat_message('assistant'):
        with st.spinner('Analizando...'):
            respuesta, imagenes, hay_nuevos, es_informe = ejecutar_pregunta(prompt)
        st.markdown(respuesta)
        for ruta in imagenes:
            ruta_abs = BASE_DIR / ruta
            if ruta_abs.exists():
                st.image(str(ruta_abs))
            else:
                st.caption(f'[Grafico no encontrado: {ruta}]')

    st.session_state.messages.append({'role': 'assistant', 'content': respuesta, 'imagenes': imagenes})
    if hay_nuevos:
        carpeta_nombre = 'Informes' if es_informe else 'Graficos'
        st.toast(f'Guardado en {carpeta_nombre}', icon=None)
    st.rerun()
