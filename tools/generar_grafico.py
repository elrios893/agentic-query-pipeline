#!/usr/bin/env python3
"""
Tool: generar_grafico
Genera graficos PNG a partir de datos tabulares usando matplotlib.
No consulta la base de datos — solo recibe datos ya obtenidos.

Tipos: linea | barras_horizontales | barras_verticales | barras_agrupadas | torta
"""
import os
import hashlib
from datetime import datetime
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import numpy as np

# --- Paleta corporativa ---
COLOR_PRINCIPAL = '#2F5496'
COLOR_GRIS      = '#A6A6A6'
COLOR_ALERTA    = '#C00000'
COLOR_FONDO     = '#FAFAFA'
COLOR_TEXTO     = '#333333'
COLOR_SPINE     = '#D0D0D0'

DPI = 150
TAMANO_TORTA    = (6, 6)

# Espacio fijo reservado para titulo, ejes y margenes (en pulgadas)
MARGEN_ALTO  = 1.8   # titulo + xlabel + padding top/bottom
MARGEN_ANCHO = 2.0   # ylabel + yticklabels + padding left/right

# Espaciado por item: cuántas pulgadas ocupa cada categoría en su eje
ESPACIADO_MIN = 0.30
ESPACIADO_MAX = 0.80
ANCHO_MIN     = 5.0
ANCHO_MAX     = 20.0
ALTO_MIN      = 3.5
ALTO_MAX      = 24.0


def _calcular_tamano(n_items: int, horizontal: bool) -> tuple[float, float]:
    """
    Tamaño dinámico basado en la fórmula:
        espaciado = dim_util / (n_items + 1)
    Despejado:
        dim_util = espaciado * (n_items + 1)
        dim_total = dim_util + margen_fijo

    - horizontal=True  → eje de categorías es Y → dim variable = ALTO
    - horizontal=False → eje de categorías es X → dim variable = ANCHO
    Ambos ejes usan la misma fórmula, solo cambia qué dimensión varía.
    """
    espaciado = max(ESPACIADO_MIN, min(ESPACIADO_MAX, 3.0 / (n_items + 1) + ESPACIADO_MIN))
    dim_util  = espaciado * (n_items + 1)

    if horizontal:
        alto  = max(ALTO_MIN,  min(ALTO_MAX,  dim_util + MARGEN_ALTO))
        ancho = 9.0
        return ancho, alto
    else:
        ancho = max(ANCHO_MIN, min(ANCHO_MAX, dim_util + MARGEN_ANCHO))
        alto  = 4.5
        return ancho, alto


def aplicar_estilo_creytex(fig, ax, formatter=None, horizontal=False):
    """
    Aplica el estilo visual corporativo Creytex.
    - formatter: FuncFormatter ya configurado con el formato_y correcto.
    - horizontal: True para barras horizontales (los valores estan en el eje X).
    """
    ax.set_facecolor(COLOR_FONDO)
    fig.patch.set_facecolor('white')
    if horizontal:
        # Valores en eje X → grilla vertical, formatter en xaxis
        ax.grid(True, axis='x', alpha=0.3, linestyle='--', color=COLOR_GRIS)
        ax.grid(False, axis='y')
        if formatter:
            ax.xaxis.set_major_formatter(formatter)
    else:
        # Valores en eje Y → grilla horizontal, formatter en yaxis
        ax.grid(True, axis='y', alpha=0.3, linestyle='--', color=COLOR_GRIS)
        ax.grid(False, axis='x')
        if formatter:
            ax.yaxis.set_major_formatter(formatter)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color(COLOR_SPINE)
    ax.spines['bottom'].set_color(COLOR_SPINE)
    ax.tick_params(colors=COLOR_TEXTO, labelsize=9)


def _formatear_y(valor, _pos, fmt='moneda'):
    if fmt == 'moneda':
        return f'${valor:,.0f}'
    elif fmt == 'unidades':
        return f'{valor:,.0f}'
    elif fmt == 'porcentaje':
        return f'{valor:.1f}%'
    return f'{valor:,.0f}'


def _generar_nombre(titulo, timestamp=None):
    if timestamp:
        ts = timestamp
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    slug = re.sub(r'[^a-z0-9]+', '_', titulo.lower())[:40].strip('_')
    return f'chart_{slug}_{ts}.png'


def generar_grafico(
    datos,
    tipo,
    titulo,
    etiqueta_x='',
    etiqueta_y='',
    output_path='reports/charts/',
    formato_y='moneda',
    timestamp=None,
):
    """
    Genera un grafico PNG a partir de datos tabulares.

    Parametros
    ----------
    datos : list[dict]
        Cada dict representa una fila con claves 'x', 'y' (y opcional 'serie').
        Ej: [{"x": "ANTIOQUIA", "y": 33632}, {"x": "BOGOTA", "y": 16288}]
    tipo : str
        linea | barras_horizontales | barras_verticales | barras_agrupadas | torta
    titulo : str
    etiqueta_x, etiqueta_y : str
    output_path : str
        Directorio donde se guarda el PNG.
    formato_y : str
        moneda | unidades | porcentaje
    timestamp : str or None
        Para vincular al informe. Formato YYYYMMDD_HHMMSS.

    Retorna
    -------
    dict {"path": ..., "width_px": ..., "height_px": ..., "error": None|str}
    """
    import re as _re
    globals()['re'] = _re

    # --- Validaciones ---
    if not datos or not isinstance(datos, list) or len(datos) == 0:
        return {'path': None, 'width_px': 0, 'height_px': 0, 'error': 'Datos vacios o invalidos.'}

    tipo = tipo.lower().strip()
    tipos_validos = {'linea', 'barras_horizontales', 'barras_verticales', 'barras_agrupadas', 'torta'}
    if tipo not in tipos_validos:
        return {'path': None, 'width_px': 0, 'height_px': 0, 'error': f'Tipo no soportado: {tipo}. Usar: {", ".join(sorted(tipos_validos))}'}

    # --- Pie: rechazar si >5 categorias ---
    if tipo == 'torta' and len(datos) > 5:
        return {
            'path': None, 'width_px': 0, 'height_px': 0,
            'error': f'Demasiadas categorias ({len(datos)}) para grafico de torta. Usar "barras_horizontales" en su lugar.'
        }

    # --- Determinar tamaño dinámico según tipo y número de ítems ---
    n = len(datos)
    if tipo == 'torta':
        size = TAMANO_TORTA
    elif tipo == 'barras_horizontales':
        size = _calcular_tamano(n, horizontal=True)
    elif tipo in ('barras_verticales', 'linea'):
        size = _calcular_tamano(n, horizontal=False)
    elif tipo == 'barras_agrupadas':
        # Contar grupos únicos en X
        n_grupos = len({str(d.get('x', '')) for d in datos})
        size = _calcular_tamano(n_grupos, horizontal=False)
    else:
        size = _calcular_tamano(n, horizontal=False)
    fig, ax = plt.subplots(figsize=size, dpi=DPI)

    # --- Procesar datos ---
    x_vals  = [str(d.get('x', '')) for d in datos]
    y_vals  = [float(d.get('y', 0)) for d in datos]
    series  = [d.get('serie', None) for d in datos]
    n       = len(datos)

    # Tamaño de fuente adaptivo para etiquetas del eje de categorías
    tick_fs = max(6, min(9, int(9 - n * 0.08)))

    # --- Formateador Y con el formato solicitado ---
    fmt_y = formato_y
    def formatter(val, _pos):
        return _formatear_y(val, _pos, fmt=fmt_y)
    fmt_func = FuncFormatter(formatter)

    try:
        if tipo == 'linea':
            ax.plot(x_vals, y_vals, color=COLOR_PRINCIPAL, linewidth=2, marker='o', markersize=5)
            ax.fill_between(range(len(x_vals)), y_vals, alpha=0.08, color=COLOR_PRINCIPAL)
            ax.set_xticks(range(len(x_vals)))
            ax.set_xticklabels(x_vals, rotation=30, ha='right', fontsize=tick_fs)
            aplicar_estilo_creytex(fig, ax, formatter=fmt_func, horizontal=False)
            ax.set_title(titulo, fontweight='bold', fontsize=13, color=COLOR_TEXTO, pad=12)
            ax.set_xlabel(etiqueta_x, fontsize=10, color=COLOR_TEXTO, labelpad=8)
            ax.set_ylabel(etiqueta_y, fontsize=10, color=COLOR_TEXTO, labelpad=8)

        elif tipo == 'barras_horizontales':
            indices = list(range(len(x_vals)))
            colores = [COLOR_ALERTA if _es_alerta(v, y_vals) else COLOR_PRINCIPAL for v in y_vals]
            bar_h = max(0.3, min(0.75, 0.65 * 5 / max(n, 5)))
            ax.barh(indices, y_vals, color=colores, height=bar_h, edgecolor='white', linewidth=0.3)
            ax.set_yticks(indices)
            ax.set_yticklabels(x_vals, fontsize=tick_fs)
            ax.invert_yaxis()
            aplicar_estilo_creytex(fig, ax, formatter=fmt_func, horizontal=True)
            ax.set_title(titulo, fontweight='bold', fontsize=13, color=COLOR_TEXTO, pad=12)
            ax.set_xlabel(etiqueta_y or etiqueta_x, fontsize=10, color=COLOR_TEXTO, labelpad=8)
            for i, v in enumerate(y_vals):
                ax.text(v + abs(max(y_vals)) * 0.01, i, formatter(v, None),
                        va='center', fontsize=max(6, tick_fs - 1), color=COLOR_TEXTO)

        elif tipo == 'barras_verticales':
            indices = np.arange(len(x_vals))
            colores = [COLOR_ALERTA if _es_alerta(v, y_vals) else COLOR_PRINCIPAL for v in y_vals]
            bar_w = max(0.2, min(0.7, 0.6 * 5 / max(n, 5)))
            ax.bar(indices, y_vals, color=colores, width=bar_w, edgecolor='white', linewidth=0.3)
            ax.set_xticks(indices)
            ax.set_xticklabels(x_vals, rotation=35, ha='right', fontsize=tick_fs)
            aplicar_estilo_creytex(fig, ax, formatter=fmt_func, horizontal=False)
            ax.set_title(titulo, fontweight='bold', fontsize=13, color=COLOR_TEXTO, pad=12)
            ax.set_xlabel(etiqueta_x, fontsize=10, color=COLOR_TEXTO, labelpad=8)
            ax.set_ylabel(etiqueta_y, fontsize=10, color=COLOR_TEXTO, labelpad=8)

        elif tipo == 'barras_agrupadas':
            if not any(d.get('serie') for d in datos):
                plt.close(fig)
                return {'path': None, 'width_px': 0, 'height_px': 0, 'error': 'barras_agrupadas requiere clave "serie" en cada dict de datos.'}
            _graficar_barras_agrupadas(ax, datos, formatter, tick_fs)
            aplicar_estilo_creytex(fig, ax, formatter=fmt_func, horizontal=False)
            ax.set_title(titulo, fontweight='bold', fontsize=13, color=COLOR_TEXTO, pad=12)
            ax.set_xlabel(etiqueta_x, fontsize=10, color=COLOR_TEXTO, labelpad=8)
            ax.set_ylabel(etiqueta_y, fontsize=10, color=COLOR_TEXTO, labelpad=8)

        elif tipo == 'torta':
            explode = [0.05] + [0] * (len(x_vals) - 1)
            colores_torta = [COLOR_PRINCIPAL, COLOR_GRIS, '#5B9BD5', '#ED7D31', '#70AD47'][:len(x_vals)]
            wedges, texts, autotexts = ax.pie(
                y_vals, labels=x_vals, autopct='%1.1f%%', startangle=90,
                colors=colores_torta, explode=explode, pctdistance=0.75,
                wedgeprops={'edgecolor': 'white', 'linewidth': 1},
            )
            for t in texts:
                t.set_fontsize(9)
                t.set_color(COLOR_TEXTO)
            for t in autotexts:
                t.set_fontsize(8)
                t.set_color('white')
                t.set_fontweight('bold')
            ax.set_title(titulo, fontweight='bold', fontsize=13, color=COLOR_TEXTO, pad=16)

        # --- Guardar ---
        out_dir = Path(output_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        nombre = _generar_nombre(titulo, timestamp)
        ruta = out_dir / nombre
        fig.tight_layout()
        fig.savefig(ruta, dpi=DPI, bbox_inches='tight', facecolor='white')
        ancho_px, alto_px = fig.get_size_inches() * DPI
        plt.close(fig)
        return {
            'path': str(ruta),
            'width_px': int(ancho_px),
            'height_px': int(alto_px),
            'error': None,
        }

    except Exception as e:
        plt.close(fig)
        return {'path': None, 'width_px': 0, 'height_px': 0, 'error': str(e)}


def _es_alerta(valor, todos):
    """Marca como alerta el valor mas bajo (para destacar cual necesita atencion)."""
    if len(todos) < 3:
        return False
    return valor == min(todos)


def _graficar_barras_agrupadas(ax, datos, formatter, tick_fs=8):
    from collections import OrderedDict
    grupos = OrderedDict()
    etiquetas_series = []
    for d in datos:
        x = str(d.get('x', ''))
        s = str(d.get('serie', ''))
        v = float(d.get('y', 0))
        if x not in grupos:
            grupos[x] = {}
        grupos[x][s] = v
        if s not in etiquetas_series:
            etiquetas_series.append(s)

    n_series = len(etiquetas_series)
    n_grupos = len(grupos)
    x = np.arange(n_grupos)
    ancho = 0.7 / n_series
    colores_serie = [COLOR_PRINCIPAL, COLOR_GRIS, '#5B9BD5', '#ED7D31', '#70AD47']

    for i, serie_nombre in enumerate(etiquetas_series):
        valores = [grupos[g].get(serie_nombre, 0) for g in grupos]
        offset = (i - (n_series - 1) / 2) * ancho
        bars = ax.bar(x + offset, valores, ancho * 0.9,
                      label=serie_nombre, color=colores_serie[i % len(colores_serie)],
                      edgecolor='white', linewidth=0.3)

    ax.set_xticks(x)
    ax.set_xticklabels(list(grupos.keys()), rotation=30, ha='right', fontsize=tick_fs)
    ax.legend(fontsize=8, framealpha=0.9, edgecolor=COLOR_SPINE)


# ===================================================================
# Ejemplos de uso (ejecutar directamente para validar visualmente)
# ===================================================================
if __name__ == '__main__':
    import json
    import re

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    # --- 1. Barras verticales ---
    print('Ejemplo 1: Barras verticales')
    r1 = generar_grafico(
        datos=[
            {'x': 'ANTIOQUIA',     'y': 33632},
            {'x': 'BOGOTA',        'y': 16288},
            {'x': 'ATLANTICO',     'y': 8794},
            {'x': 'BOLIVAR',       'y': 8365},
            {'x': 'SANTANDER',     'y': 6611},
        ],
        tipo='barras_verticales',
        titulo='Unidades Vendidas por Departamento',
        etiqueta_x='Departamento',
        etiqueta_y='Unidades',
        formato_y='unidades',
        timestamp=ts,
    )
    print(json.dumps(r1, indent=2))

    # --- 2. Barras horizontales ---
    print('\nEjemplo 2: Barras horizontales')
    r2 = generar_grafico(
        datos=[
            {'x': 'ANTIOQUIA',     'y': 2647590911},
            {'x': 'BOGOTA',        'y': 1284493146},
            {'x': 'ATLANTICO',     'y': 632738932},
            {'x': 'BOLIVAR',       'y': 629362725},
            {'x': 'SANTANDER',     'y': 515300064},
        ],
        tipo='barras_horizontales',
        titulo='Valor de Ventas por Departamento (COP)',
        etiqueta_y='Departamento',
        formato_y='moneda',
        timestamp=ts,
    )
    print(json.dumps(r2, indent=2))

    # --- 3. Linea ---
    print('\nEjemplo 3: Linea')
    r3 = generar_grafico(
        datos=[
            {'x': 'Ene', 'y': 12000},
            {'x': 'Feb', 'y': 14500},
            {'x': 'Mar', 'y': 13200},
            {'x': 'Abr', 'y': 15800},
            {'x': 'May', 'y': 14200},
            {'x': 'Jun', 'y': 16500},
        ],
        tipo='linea',
        titulo='Tendencia Mensual de Unidades Vendidas',
        etiqueta_x='Mes',
        etiqueta_y='Unidades',
        formato_y='unidades',
        timestamp=ts,
    )
    print(json.dumps(r3, indent=2))

    # --- 4. Torta ---
    print('\nEjemplo 4: Torta (participacion)')
    r4 = generar_grafico(
        datos=[
            {'x': 'ANTIOQUIA', 'y': 33.6},
            {'x': 'BOGOTA',    'y': 16.3},
            {'x': 'ATLANTICO', 'y': 8.8},
            {'x': 'BOLIVAR',   'y': 8.4},
            {'x': 'SANTANDER', 'y': 6.6},
        ],
        tipo='torta',
        titulo='Participacion por Departamento (%)',
        formato_y='porcentaje',
        timestamp=ts,
    )
    print(json.dumps(r4, indent=2))

    # --- 5. Barras agrupadas ---
    print('\nEjemplo 5: Barras agrupadas')
    r5 = generar_grafico(
        datos=[
            {'x': 'ANTIOQUIA', 'y': 33632, 'serie': 'Semana Actual'},
            {'x': 'ANTIOQUIA', 'y': 29500, 'serie': 'Semana Anterior'},
            {'x': 'BOGOTA',    'y': 16288, 'serie': 'Semana Actual'},
            {'x': 'BOGOTA',    'y': 15100, 'serie': 'Semana Anterior'},
            {'x': 'ATLANTICO', 'y': 8794,  'serie': 'Semana Actual'},
            {'x': 'ATLANTICO', 'y': 9200,  'serie': 'Semana Anterior'},
        ],
        tipo='barras_agrupadas',
        titulo='Comparacion Semanal por Departamento',
        etiqueta_x='Departamento',
        etiqueta_y='Unidades',
        formato_y='unidades',
        timestamp=ts,
    )
    print(json.dumps(r5, indent=2))

    print(f'\nTodos los graficos guardados en reports/charts/')
