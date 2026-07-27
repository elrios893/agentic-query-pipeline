import streamlit as st
import subprocess
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from ui.db import init_db, guardar_item, listar_items, obtener_item

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
# Inicializar session state
# ---------------------------------------------------------------------------
st.session_state.setdefault('messages', [])
st.session_state.setdefault('vista', 'chat')       # 'chat' | 'item'
st.session_state.setdefault('item_id', None)
st.session_state.setdefault('carpeta', 'informes')

# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------
def cb_ver_item(item_id: int):
    st.session_state.vista = 'item'
    st.session_state.item_id = item_id

def cb_volver_chat():
    st.session_state.vista = 'chat'
    st.session_state.item_id = None

def cb_cambiar_carpeta():
    # Llamado por on_change del segmented_control; simplemente deja que el
    # script rerrunn y lea el nuevo valor del widget.
    pass

# ---------------------------------------------------------------------------
# Sidebar
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
        on_change=cb_cambiar_carpeta,
    )

    # segmented_control puede devolver None si el usuario deselecciona
    carpeta = carpeta or 'informes'
    st.session_state.carpeta = carpeta

    items = listar_items(carpeta)

    if not items:
        st.caption('(vacio)')
    else:
        for item in items:
            fecha = item['created_at'][:10]
            label = f'{item["titulo"]}'
            sublabel = f'{fecha}'
            with st.container(border=False):
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.caption(sublabel)
                    if st.button(label, key=f'btn_{item["id"]}', use_container_width=True):
                        cb_ver_item(item['id'])
                        st.rerun()

# ---------------------------------------------------------------------------
# Vista principal
# ---------------------------------------------------------------------------
if st.session_state.vista == 'item' and st.session_state.item_id:
    item = obtener_item(st.session_state.item_id)
    if item:
        ruta_completa = BASE_DIR / item['ruta']
        st.button(':material/arrow_back: Volver al chat', on_click=cb_volver_chat)
        st.divider()
        if item['tipo'] == 'informe':
            if ruta_completa.exists():
                st.markdown(ruta_completa.read_text(encoding='utf-8'))
            else:
                st.warning(f'Archivo no encontrado: {item["ruta"]}')
        elif item['tipo'] == 'grafico':
            if ruta_completa.exists():
                st.image(str(ruta_completa))
            else:
                st.warning(f'Imagen no encontrada: {item["ruta"]}')
    else:
        st.warning('Item no encontrado.')
        cb_volver_chat()
    st.stop()

# ---------------------------------------------------------------------------
# Vista chat
# ---------------------------------------------------------------------------
def ejecutar_pregunta(pregunta: str) -> tuple[str, list[str], bool]:
    try:
        resultado = subprocess.run(
            ['python', str(ORQUESTADOR), pregunta],
            capture_output=True, text=True, timeout=300,
            encoding='utf-8', errors='replace',
        )
        salida = resultado.stdout

        if resultado.returncode != 0 and not salida.strip():
            error = resultado.stderr
            return f'Error: {error or f"Proceso termino con codigo {resultado.returncode}"}', [], False

        guardados = 0

        # Detectar informes
        for m in re.finditer(r'Markdown guardado:\s*(.+)', salida):
            ruta = m.group(1).strip()
            r = Path(ruta)
            try:
                ruta_rel = r.relative_to(BASE_DIR).as_posix()
                guardar_item('informe', r.stem, ruta_rel, pregunta)
                guardados += 1
            except ValueError:
                pass

        # Detectar graficos por linea OK:
        for m in re.finditer(r'OK:\s*(.+)', salida):
            ruta_raw = m.group(1).strip()
            ruta_limpia = ruta_raw.replace('\\', '/')
            g = BASE_DIR / ruta_limpia
            if g.exists():
                titulo = g.stem.replace('chart_', '', 1)
                guardar_item('grafico', titulo, ruta_limpia, pregunta)
                guardados += 1

        # Extraer respuesta
        match = re.search(r'====+\s*(?:RESPUESTA|INFORME)\s*====+\s*(.*)', salida, re.DOTALL)
        respuesta = match.group(1).strip() if match else salida.strip()
        imagenes = re.findall(r'!\[.*?\]\((.*?)\)', respuesta)

        # Detectar graficos embebidos en la respuesta markdown
        for ruta_img in imagenes:
            ruta_img_norm = ruta_img.replace('\\', '/')
            g = BASE_DIR / ruta_img_norm
            if g.exists():
                titulo = g.stem.replace('chart_', '', 1)
                guardar_item('grafico', titulo, ruta_img_norm, pregunta)
                guardados += 1

        return respuesta, imagenes, bool(guardados)

    except subprocess.TimeoutExpired:
        return 'La consulta tardo demasiado (mas de 5 minutos). Intenta con una pregunta mas simple.', [], False
    except Exception as e:
        return f'Error inesperado: {str(e)}', [], False


# Sugerencias iniciales
if not st.session_state.messages:
    st.title('Consultor Inteligente de Ventas')
    st.markdown('Pregunta sobre ventas en lenguaje natural.')
    seleccion = st.pills(
        'Sugerencias:',
        list(SUGERENCIAS.keys()),
        label_visibility='collapsed',
    )
    if seleccion:
        st.session_state.messages.append({'role': 'user', 'content': SUGERENCIAS[seleccion]})
        st.rerun()

# Historial de mensajes
for msg in st.session_state.messages:
    with st.chat_message(msg['role']):
        st.markdown(msg['content'])
        for ruta in msg.get('imagenes', []):
            ruta_abs = BASE_DIR / ruta
            if ruta_abs.exists():
                st.image(str(ruta_abs))
            else:
                st.caption(f'[Grafico no encontrado: {ruta}]')

# Input del chat
if prompt := st.chat_input('Escribe tu pregunta sobre ventas...', submit_mode='disable'):
    st.session_state.messages.append({'role': 'user', 'content': prompt})
    with st.chat_message('user'):
        st.markdown(prompt)

    with st.chat_message('assistant'):
        with st.spinner('Analizando...'):
            respuesta, imagenes, hay_nuevos = ejecutar_pregunta(prompt)
        st.markdown(respuesta)
        for ruta in imagenes:
            ruta_abs = BASE_DIR / ruta
            if ruta_abs.exists():
                st.image(str(ruta_abs))
            else:
                st.caption(f'[Grafico no encontrado: {ruta}]')

    st.session_state.messages.append({'role': 'assistant', 'content': respuesta, 'imagenes': imagenes})
    if hay_nuevos:
        st.toast('Elemento(s) guardado(s) en el historial :material/save:', icon=None)
        st.rerun()
