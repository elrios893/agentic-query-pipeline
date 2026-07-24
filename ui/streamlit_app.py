import streamlit as st
import subprocess
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ORQUESTADOR = BASE_DIR / 'src' / 'orquestador.py'

st.set_page_config(
    page_title='Creytex — Consultor de Ventas',
    page_icon=':material/bar_chart:',
)

SUGERENCIAS = {
    ':material/assessment: Ventas por departamento': 'ventas por departamento',
    ':material/bar_chart: Graficame top 5 departamentos': 'graficame top 5 departamentos por ventas',
    ':material/description: Genera un informe de ventas': 'genera un informe de ventas completo',
    ':material/sell: Talla mas vendida': 'cual es la talla que mas se vende',
}

if 'messages' not in st.session_state:
    st.session_state.messages = []

def ejecutar_pregunta(pregunta: str) -> tuple[str, list[str]]:
    try:
        resultado = subprocess.run(
            ['python', str(ORQUESTADOR), pregunta],
            capture_output=True, text=True, timeout=300, encoding='utf-8',
        )
        salida = resultado.stdout
        error = resultado.stderr

        if resultado.returncode != 0 and not salida.strip():
            return f'Error: {error or f"El proceso termino con codigo {resultado.returncode}"}', []

        match = re.search(r'====+\s*(?:RESPUESTA|INFORME)\s*====+\s*(.*)', salida, re.DOTALL)
        respuesta = match.group(1).strip() if match else salida.strip()
        imagenes = re.findall(r'!\[.*?\]\((.*?)\)', respuesta)
        return respuesta, imagenes

    except subprocess.TimeoutExpired:
        return 'La consulta tardo demasiado (mas de 5 minutos). Intenta con una pregunta mas simple.', []
    except Exception as e:
        return f'Error inesperado: {str(e)}', []

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
        prompt = SUGERENCIAS[seleccion]
        st.session_state.messages.append({'role': 'user', 'content': prompt})
        st.rerun()

# Historial
for msg in st.session_state.messages:
    with st.chat_message(msg['role']):
        st.markdown(msg['content'])
        for ruta in msg.get('imagenes', []):
            ruta_abs = BASE_DIR / ruta
            if ruta_abs.exists():
                st.image(str(ruta_abs))
            else:
                st.caption(f'[Grafico no encontrado: {ruta}]')

# Input
if prompt := st.chat_input('Escribe tu pregunta sobre ventas...', submit_mode='disable'):
    st.session_state.messages.append({'role': 'user', 'content': prompt})
    with st.chat_message('user'):
        st.markdown(prompt)

    with st.chat_message('assistant'):
        with st.spinner('Analizando...'):
            respuesta, imagenes = ejecutar_pregunta(prompt)
        st.markdown(respuesta)
        for ruta in imagenes:
            ruta_abs = BASE_DIR / ruta
            if ruta_abs.exists():
                st.image(str(ruta_abs))
            else:
                st.caption(f'[Grafico no encontrado: {ruta}]')

    st.session_state.messages.append({'role': 'assistant', 'content': respuesta, 'imagenes': imagenes})
