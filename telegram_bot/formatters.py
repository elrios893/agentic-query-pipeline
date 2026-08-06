"""
telegram_bot/formatters.py
Formateadores para convertir respuestas a formato legible en Telegram.

Conversión Markdown estándar → formato Telegram (MarkdownV1 compatible):
  ## Título        →  *TÍTULO*
  **negrita**      →  *negrita*
  *cursiva*        →  _cursiva_
  | tabla |        →  bloque de código monoespaciado (```...```)
  ![img](ruta)     →  (eliminado — la imagen se envía por separado)
  `código`         →  `código`  (igual, Telegram lo soporta)
"""
import re
from typing import List, Dict, Any, Optional
from telegram_bot.config import EMOJIS, MAX_MESSAGE_LENGTH


# ---------------------------------------------------------------------------
# Conversor principal: Markdown estándar → texto para Telegram
# ---------------------------------------------------------------------------

def _convertir_tabla_pipe(bloque_tabla: str) -> str:
    """
    Convierte una tabla Markdown de pipes a una tabla ASCII monoespaciada
    envuelta en bloque de código (``` ```) para Telegram.

    Entrada:
        | Col A | Col B |
        |-------|-------|
        | val1  | val2  |

    Salida (dentro de triple backtick):
        Col A  │ Col B
        ───────┼───────
        val1   │ val2
    """
    lineas = [l.rstrip() for l in bloque_tabla.strip().splitlines() if l.strip()]
    if not lineas:
        return ''

    # Parsear filas: quitar | iniciales/finales y separar celdas
    filas = []
    separador_idx = None
    for i, linea in enumerate(lineas):
        if re.match(r'^\s*\|?\s*[-:]+[-| :]*$', linea):
            separador_idx = i
            continue
        celdas = [c.strip() for c in re.split(r'\|', linea.strip('| \t'))]
        filas.append(celdas)

    if not filas:
        return ''

    # Normalizar número de columnas
    n_cols = max(len(f) for f in filas)
    filas = [f + [''] * (n_cols - len(f)) for f in filas]

    # Calcular anchos de columna
    anchos = [max(len(filas[r][c]) for r in range(len(filas))) for c in range(n_cols)]

    def fila_a_texto(celdas):
        return ' │ '.join(str(celdas[c]).ljust(anchos[c]) for c in range(n_cols))

    lineas_resultado = []
    encabezado_emitido = False
    for i, fila in enumerate(filas):
        lineas_resultado.append(fila_a_texto(fila))
        # Después de la primera fila (encabezado), poner separador
        if not encabezado_emitido and len(filas) > 1:
            sep = '─' * (sum(anchos) + 3 * (n_cols - 1))
            lineas_resultado.append(sep)
            encabezado_emitido = True

    tabla_ascii = '\n'.join(lineas_resultado)
    return f'```\n{tabla_ascii}\n```'


def md_a_telegram(texto: str) -> str:
    """
    Convierte Markdown estándar al subconjunto de formato que Telegram
    renderiza correctamente con ParseMode.MARKDOWN (v1).

    Reglas aplicadas (en orden):
    1. Bloques de código cercados (``` ... ```) → se conservan tal cual
    2. Tablas pipe (| col | col |) → tabla ASCII en bloque ```
    3. ![imagen](ruta) → eliminado (las imágenes se envían por separado)
    4. ## / ### Encabezados → *TEXTO EN MAYÚSCULAS*
    5. **negrita** → *negrita*
    6. __negrita__ → *negrita*
    7. *cursiva* (sin par de asteriscos) → _cursiva_
    8. _cursiva_ → se conserva (ya es formato Telegram)
    9. `código inline` → se conserva
    10. Líneas separadoras --- o === → ───────────────────
    """
    if not texto:
        return texto

    # --- 1. Proteger bloques de código existentes (no tocarlos) ---
    bloques_codigo = {}
    contador = [0]

    def guardar_bloque(m):
        token = f'\x00BLOQUE{contador[0]}\x00'
        bloques_codigo[token] = m.group(0)
        contador[0] += 1
        return token

    texto = re.sub(r'```[\s\S]*?```', guardar_bloque, texto)

    # --- 2. Convertir tablas pipe ---
    def reemplazar_tabla(m):
        return _convertir_tabla_pipe(m.group(0))

    # Detecta bloque de líneas que contengan pipes (tabla markdown)
    # Acepta líneas con o sin newline al final (última línea del texto)
    texto = re.sub(
        r'(?m)^(?:\|[^\n]+\|?\s*\n)*\|[^\n]+\|?\s*$',
        reemplazar_tabla,
        texto,
    )

    # --- 3. Eliminar imágenes markdown (![alt](url)) ---
    texto = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', texto)

    # --- 4. Encabezados ## y ### → *TÍTULO* ---
    def reemplazar_encabezado(m):
        nivel = len(m.group(1))   # número de #
        titulo = m.group(2).strip()
        # Limpiar negritas dentro del título antes de convertir
        titulo = re.sub(r'\*\*(.+?)\*\*', r'\1', titulo)
        titulo = re.sub(r'__(.+?)__', r'\1', titulo)
        if nivel <= 3:
            return f'\n*{titulo.upper()}*'
        else:
            return f'\n*{titulo}*'

    texto = re.sub(r'^(#{1,6})\s+(.+)$', reemplazar_encabezado, texto, flags=re.MULTILINE)

    # --- 5. **negrita** y __negrita__ → marcador temporal para proteger de regex de cursiva ---
    MARCA = '\x01'
    texto = re.sub(r'\*\*(.+?)\*\*', lambda m: f'{MARCA}{m.group(1)}{MARCA}', texto, flags=re.DOTALL)
    texto = re.sub(r'__(.+?)__',     lambda m: f'{MARCA}{m.group(1)}{MARCA}', texto, flags=re.DOTALL)

    # --- 6. *cursiva* suelta → _cursiva_ (solo cuando no es negrita ni lista) ---
    texto = re.sub(r'(?<![*\n])\*(?!\*)([^*\n]+?)\*(?!\*)', r'_\1_', texto)

    # --- 7. Restaurar marcadores de negrita → *texto* ---
    texto = texto.replace(MARCA, '*')

    # --- 7. Líneas separadoras (--- o ===) → línea de guiones Unicode ---
    texto = re.sub(r'^[-=]{3,}\s*$', '─' * 30, texto, flags=re.MULTILINE)

    # --- 8. Restaurar bloques de código protegidos ---
    for token, bloque in bloques_codigo.items():
        texto = texto.replace(token, bloque)

    # --- 9. Limpiar líneas en blanco excesivas (máx 2 consecutivas) ---
    texto = re.sub(r'\n{3,}', '\n\n', texto)

    return texto.strip()


# ---------------------------------------------------------------------------
# Clase principal de formateo
# ---------------------------------------------------------------------------

class MessageFormatter:
    """Formatea mensajes para Telegram"""

    @staticmethod
    def convertir_para_telegram(texto: str) -> str:
        """Convierte Markdown estándar al formato legible en Telegram."""
        return md_a_telegram(texto)

    @staticmethod
    def dividir_mensaje_largo(mensaje: str, max_length: int = MAX_MESSAGE_LENGTH) -> List[str]:
        """
        Divide un mensaje largo en partes respetando el límite de Telegram.
        Evita cortar dentro de bloques de código (```) para no romper el formato.
        """
        if len(mensaje) <= max_length:
            return [mensaje]

        mensajes = []
        mensaje_actual = ''
        en_bloque_codigo = False

        for linea in mensaje.splitlines(keepends=True):
            # Rastrear si estamos dentro de un bloque ```
            if linea.strip().startswith('```'):
                en_bloque_codigo = not en_bloque_codigo

            # Si añadir esta línea supera el límite y no estamos en bloque de código
            if len(mensaje_actual) + len(linea) > max_length and not en_bloque_codigo:
                if mensaje_actual.strip():
                    mensajes.append(mensaje_actual.strip())
                mensaje_actual = linea
            else:
                mensaje_actual += linea

        if mensaje_actual.strip():
            mensajes.append(mensaje_actual.strip())

        return mensajes if mensajes else [mensaje]

    # ------------------------------------------------------------------
    # Métodos de apoyo (para uso interno o futuro)
    # ------------------------------------------------------------------

    @staticmethod
    def formato_titulo(titulo: str, emoji: str = '📊') -> str:
        return f"{emoji} *{titulo}*"

    @staticmethod
    def formato_tabla_simple(datos: List[Dict[str, Any]]) -> str:
        """Convierte lista de dicts a tabla ASCII monoespaciada (en bloque ```)."""
        if not datos:
            return 'Sin datos'
        if hasattr(datos, 'to_dict'):
            datos = datos.to_dict('records')

        columnas = list(datos[0].keys())
        anchos = {
            col: max(len(str(col)), max(len(str(f.get(col, ''))) for f in datos))
            for col in columnas
        }
        # Limitar ancho por columna
        anchos = {col: min(v, 25) for col, v in anchos.items()}

        def fila(cells):
            return ' │ '.join(str(cells.get(c, '')).ljust(anchos[c])[:anchos[c]] for c in columnas)

        sep = '─' * (sum(anchos.values()) + 3 * (len(columnas) - 1))
        lineas = [fila({c: c for c in columnas}), sep]
        for row in datos[:20]:
            lineas.append(fila(row))
        if len(datos) > 20:
            lineas.append(f'... y {len(datos) - 20} registros más')

        return '```\n' + '\n'.join(lineas) + '\n```'

    @staticmethod
    def formato_archivo(nombre: str, tamanio: int = 0, tipo: str = 'documento') -> str:
        emoji = {'excel': '📊', 'imagen': '🖼️', 'pdf': '📄'}.get(tipo, EMOJIS['file'])
        if tamanio > 1024 * 1024:
            tam_str = f"{tamanio / (1024*1024):.1f} MB"
        else:
            tam_str = f"{tamanio / 1024:.1f} KB"
        return f"{emoji} {nombre}\n_Tamaño: {tam_str}_"
