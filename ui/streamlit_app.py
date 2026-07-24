import streamlit as st
import subprocess
import re
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ORQUESTADOR = BASE_DIR / 'src' / 'orquestador.py'

st.set_page_config(
    page_title='Creytex — Consultor de Ventas',
    page_icon='📊',
    layout='wide',
)

st.title('📊 Creytex — Consultor Inteligente de Ventas')
st.markdown('Pregunta sobre ventas en lenguaje natural. Ej: *"ventas por departamento"*, *"graficame top 5 departamentos"*, *"genera un informe"*.')

if 'historial' not in st.session_state:
    st.session_state.historial = []

def ejecutar_pregunta(pregunta: str) -> tuple[str, list[str]]:
    try:
        resultado = subprocess.run(
            ['python', str(ORQUESTADOR), pregunta],
            capture_output=True, text=True, timeout=300, encoding='utf-8',
        )
        salida = resultado.stdout
        error = resultado.stderr

        if resultado.returncode != 0 and not salida.strip():
            return f'Error: {error or "El proceso termino con codigo " + str(resultado.returncode)}', []

        # Extraer seccion RESPUESTA o INFORME
        match = re.search(r'====+\s*(?:RESPUESTA|INFORME)\s*====+\s*(.*)', salida, re.DOTALL)
        if match:
            respuesta = match.group(1).strip()
        else:
            respuesta = salida.strip()

        # Extraer rutas de imagenes markdown
        imagenes = re.findall(r'!\[.*?\]\((.*?)\)', respuesta)

        return respuesta, imagenes

    except subprocess.TimeoutExpired:
        return 'La consulta tardo demasiado (mas de 5 minutos). Intenta con una pregunta mas simple.', []
    except Exception as e:
        return f'Error inesperado: {str(e)}', []

# Input
col1, col2 = st.columns([5, 1])
with col1:
    pregunta = st.chat_input('Escribe tu pregunta sobre ventas...')
with col2:
    st.markdown('###  ')

if pregunta:
    st.session_state.historial.append({'rol': 'usuario', 'contenido': pregunta})

    with st.spinner('Analizando...'):
        respuesta, imagenes = ejecutar_pregunta(pregunta)

    st.session_state.historial.append({'rol': 'asistente', 'contenido': respuesta, 'imagenes': imagenes})

# Mostrar historial
for msg in st.session_state.historial:
    if msg['rol'] == 'usuario':
        st.chat_message('user').markdown(msg['contenido'])
    else:
        with st.chat_message('assistant'):
            st.markdown(msg['contenido'])
            for ruta in msg.get('imagenes', []):
                ruta_abs = BASE_DIR / ruta
                if ruta_abs.exists():
                    st.image(str(ruta_abs), use_container_width=True)
                else:
                    st.caption(f'[Grafico no encontrado: {ruta}]')

if not st.session_state.historial:
    st.info('💡 Ejemplos:\n- "ventas por departamento"\n- "graficame top 5 departamentos"\n- "genera un informe de ventas completo"\n- "1 de julio"\n- "talla mas vendida"')
