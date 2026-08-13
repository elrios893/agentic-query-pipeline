"""
tools/tool_pandas.py
Ejecutor de operaciones Pandas predefinidas sobre DataFrames en memoria.
El LLM nunca ve los datos directamente — solo llama operaciones por nombre
y recibe el resultado calculado con precisión exacta.

Uso:
    from tools.tool_pandas import ejecutar_operacion
    resultado = ejecutar_operacion(df, operacion, parametros)
"""
import math
import pandas as pd
from typing import Any


# ---------------------------------------------------------------------------
# Catálogo de operaciones disponibles
# El enrutador elige una de estas por nombre.
# ---------------------------------------------------------------------------

OPERACIONES_DISPONIBLES = {
    'suma_total':           'Suma todos los valores de una columna numérica',
    'suma_por_grupo':       'Agrupa por una columna categórica y suma una numérica',
    'porcentaje_de_total':  'Calcula qué % representa un valor/fila del total de la columna',
    'top_n':                'Devuelve las N filas con mayor valor en una columna',
    'bottom_n':             'Devuelve las N filas con menor valor en una columna',
    'filtrar_y_sumar':      'Filtra filas donde col_filtro == valor y suma col_valor',
    'filtrar_y_contar':     'Filtra filas donde col_filtro == valor y cuenta',
    'variacion_pct':        'Calcula variación % entre dos valores: (nuevo-anterior)/anterior*100',
    'contar_por_grupo':     'Cuenta registros por cada valor de una columna categórica',
    'promedio_ponderado':   'Promedio ponderado: sum(col_valor*col_peso) / sum(col_peso)',
    'comparar_dos_valores': 'Extrae y compara las filas de dos valores específicos de una columna',
    'distribucion_pct':     'Calcula la distribución porcentual de cada grupo sobre el total',
    'suma_condicional':     'Suma col_valor solo donde col_filtro está entre una lista de valores',
    'rango':                'Devuelve min, max, promedio y total de una columna numérica',
    'correlacion_simple':   'Calcula si dos columnas numéricas se mueven en la misma dirección',
}


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

def ejecutar_operacion(
    df: pd.DataFrame,
    operacion: str,
    parametros: dict,
) -> dict:
    """
    Ejecuta una operación predefinida sobre un DataFrame.

    Parámetros comunes:
        col_valor   : columna numérica sobre la que operar
        col_grupo   : columna categórica para agrupar
        col_filtro  : columna para filtrar
        valor       : valor de filtro (string o número)
        n           : número de filas para top/bottom
        valor_nuevo : valor actual para variación %
        valor_anterior: valor base para variación %

    Retorna:
        {
          'exito': True/False,
          'operacion': str,
          'resultado': Any,    ← el valor calculado (número, lista, dict)
          'descripcion': str,  ← texto legible del resultado
          'error': str | None,
        }
    """
    operacion = operacion.strip().lower()

    if operacion not in OPERACIONES_DISPONIBLES:
        return _error(f'Operación desconocida: "{operacion}". Disponibles: {list(OPERACIONES_DISPONIBLES.keys())}')

    if df is None or df.empty:
        return _error('El DataFrame está vacío o no disponible.')

    try:
        if operacion == 'suma_total':
            return _suma_total(df, parametros)

        elif operacion == 'suma_por_grupo':
            return _suma_por_grupo(df, parametros)

        elif operacion == 'porcentaje_de_total':
            return _porcentaje_de_total(df, parametros)

        elif operacion == 'top_n':
            return _top_n(df, parametros)

        elif operacion == 'bottom_n':
            return _bottom_n(df, parametros)

        elif operacion == 'filtrar_y_sumar':
            return _filtrar_y_sumar(df, parametros)

        elif operacion == 'filtrar_y_contar':
            return _filtrar_y_contar(df, parametros)

        elif operacion == 'variacion_pct':
            return _variacion_pct(parametros)

        elif operacion == 'contar_por_grupo':
            return _contar_por_grupo(df, parametros)

        elif operacion == 'promedio_ponderado':
            return _promedio_ponderado(df, parametros)

        elif operacion == 'comparar_dos_valores':
            return _comparar_dos_valores(df, parametros)

        elif operacion == 'distribucion_pct':
            return _distribucion_pct(df, parametros)

        elif operacion == 'suma_condicional':
            return _suma_condicional(df, parametros)

        elif operacion == 'rango':
            return _rango(df, parametros)

        elif operacion == 'correlacion_simple':
            return _correlacion_simple(df, parametros)

    except Exception as e:
        return _error(f'Error ejecutando "{operacion}": {str(e)}')


# ---------------------------------------------------------------------------
# Implementaciones
# ---------------------------------------------------------------------------

def _suma_total(df: pd.DataFrame, p: dict) -> dict:
    col = _req(p, 'col_valor')
    _validar_col(df, col)
    total = float(df[col].sum())
    return _ok(
        'suma_total',
        total,
        f'Total de {col}: {_fmt(total)}',
    )


def _suma_por_grupo(df: pd.DataFrame, p: dict) -> dict:
    col_grupo = _req(p, 'col_grupo')
    col_valor = _req(p, 'col_valor')
    _validar_col(df, col_grupo)
    _validar_col(df, col_valor)
    resultado = (
        df.groupby(col_grupo)[col_valor]
        .sum()
        .sort_values(ascending=False)
        .reset_index()
    )
    rows = resultado.to_dict('records')
    desc = f'{col_valor} por {col_grupo}: ' + ', '.join(
        f'{r[col_grupo]}={_fmt(r[col_valor])}' for r in rows[:5]
    )
    if len(rows) > 5:
        desc += f' ... ({len(rows)} grupos en total)'
    return _ok('suma_por_grupo', rows, desc)


def _mascara_valor(serie: pd.Series, valor) -> pd.Series:
    """
    Máscara booleana para filtrar una columna categórica por 'valor'.
    Coincidencia exacta primero (case-sensitive); si no hay ninguna fila,
    cae a case-insensitive y luego a "contiene" — las categorías del
    negocio suelen llevar un prefijo numérico (ej. "02 - Camiseta manga
    corta") que el LLM que sugiere el filtro no siempre reproduce.
    """
    serie_str = serie.astype(str).str.strip()
    valor_str = str(valor).strip()

    exacta = serie_str == valor_str
    if exacta.any():
        return exacta

    insensible = serie_str.str.casefold() == valor_str.casefold()
    if insensible.any():
        return insensible

    return serie_str.str.casefold().str.contains(valor_str.casefold(), regex=False, na=False)


def _porcentaje_de_total(df: pd.DataFrame, p: dict) -> dict:
    col_filtro = _req(p, 'col_filtro')
    valor      = _req(p, 'valor')
    col_valor  = _req(p, 'col_valor')
    _validar_col(df, col_filtro)
    _validar_col(df, col_valor)

    total     = float(df[col_valor].sum())
    subtotal  = float(df[_mascara_valor(df[col_filtro], valor)][col_valor].sum())

    if total == 0:
        return _error('Total es 0, no se puede calcular porcentaje.')

    pct = round(subtotal / total * 100, 2)
    return _ok(
        'porcentaje_de_total',
        pct,
        f'{valor} representa el {pct}% del total de {col_valor} ({_fmt(subtotal)} de {_fmt(total)})',
    )


def _top_n(df: pd.DataFrame, p: dict) -> dict:
    col_valor = _req(p, 'col_valor')
    n         = int(p.get('n', 5))
    _validar_col(df, col_valor)
    resultado = df.nlargest(n, col_valor)
    rows = resultado.to_dict('records')
    cols_str = df.select_dtypes(include='object').columns.tolist()
    label_col = cols_str[0] if cols_str else col_valor
    desc = f'Top {n} por {col_valor}: ' + ', '.join(
        f'{r.get(label_col, i+1)}={_fmt(r[col_valor])}' for i, r in enumerate(rows)
    )
    return _ok('top_n', rows, desc)


def _bottom_n(df: pd.DataFrame, p: dict) -> dict:
    col_valor = _req(p, 'col_valor')
    n         = int(p.get('n', 5))
    _validar_col(df, col_valor)
    resultado = df.nsmallest(n, col_valor)
    rows = resultado.to_dict('records')
    cols_str = df.select_dtypes(include='object').columns.tolist()
    label_col = cols_str[0] if cols_str else col_valor
    desc = f'Bottom {n} por {col_valor}: ' + ', '.join(
        f'{r.get(label_col, i+1)}={_fmt(r[col_valor])}' for i, r in enumerate(rows)
    )
    return _ok('bottom_n', rows, desc)


def _filtrar_y_sumar(df: pd.DataFrame, p: dict) -> dict:
    col_filtro = _req(p, 'col_filtro')
    valor      = _req(p, 'valor')
    col_valor  = _req(p, 'col_valor')
    _validar_col(df, col_filtro)
    _validar_col(df, col_valor)
    filtrado = df[_mascara_valor(df[col_filtro], valor)]
    total    = float(filtrado[col_valor].sum())
    return _ok(
        'filtrar_y_sumar',
        total,
        f'Suma de {col_valor} donde {col_filtro}="{valor}": {_fmt(total)} ({len(filtrado)} filas)',
    )


def _filtrar_y_contar(df: pd.DataFrame, p: dict) -> dict:
    col_filtro = _req(p, 'col_filtro')
    valor      = _req(p, 'valor')
    _validar_col(df, col_filtro)
    filtrado = df[_mascara_valor(df[col_filtro], valor)]
    n = len(filtrado)
    return _ok(
        'filtrar_y_contar',
        n,
        f'Filas donde {col_filtro}="{valor}": {n}',
    )


def _variacion_pct(p: dict) -> dict:
    nuevo    = float(_req(p, 'valor_nuevo'))
    anterior = float(_req(p, 'valor_anterior'))
    if anterior == 0:
        return _error('El valor anterior es 0, no se puede calcular variación %.')
    pct = round((nuevo - anterior) / abs(anterior) * 100, 2)
    signo = '+' if pct >= 0 else ''
    return _ok(
        'variacion_pct',
        pct,
        f'Variación: {signo}{pct}% (de {_fmt(anterior)} a {_fmt(nuevo)})',
    )


def _contar_por_grupo(df: pd.DataFrame, p: dict) -> dict:
    col_grupo = _req(p, 'col_grupo')
    _validar_col(df, col_grupo)
    resultado = (
        df.groupby(col_grupo)
        .size()
        .sort_values(ascending=False)
        .reset_index(name='conteo')
    )
    rows = resultado.to_dict('records')
    desc = f'Conteo por {col_grupo}: ' + ', '.join(
        f'{r[col_grupo]}={r["conteo"]}' for r in rows[:5]
    )
    return _ok('contar_por_grupo', rows, desc)


def _promedio_ponderado(df: pd.DataFrame, p: dict) -> dict:
    col_valor = _req(p, 'col_valor')
    col_peso  = _req(p, 'col_peso')
    _validar_col(df, col_valor)
    _validar_col(df, col_peso)
    suma_pesos = float(df[col_peso].sum())
    if suma_pesos == 0:
        return _error('La suma de pesos es 0.')
    prom = float((df[col_valor] * df[col_peso]).sum() / suma_pesos)
    return _ok(
        'promedio_ponderado',
        round(prom, 2),
        f'Promedio ponderado de {col_valor} (peso={col_peso}): {_fmt(prom)}',
    )


def _comparar_dos_valores(df: pd.DataFrame, p: dict) -> dict:
    col_filtro = _req(p, 'col_filtro')
    valor_a    = _req(p, 'valor_a')
    valor_b    = _req(p, 'valor_b')
    col_valor  = _req(p, 'col_valor')
    _validar_col(df, col_filtro)
    _validar_col(df, col_valor)

    fila_a = df[_mascara_valor(df[col_filtro], valor_a)]
    fila_b = df[_mascara_valor(df[col_filtro], valor_b)]

    total_a = float(fila_a[col_valor].sum()) if not fila_a.empty else 0.0
    total_b = float(fila_b[col_valor].sum()) if not fila_b.empty else 0.0
    diferencia = total_a - total_b
    pct = round(diferencia / abs(total_b) * 100, 2) if total_b != 0 else None

    resultado = {
        valor_a:      round(total_a, 2),
        valor_b:      round(total_b, 2),
        'diferencia': round(diferencia, 2),
        'variacion_pct': pct,
    }
    desc = (
        f'{valor_a}={_fmt(total_a)} vs {valor_b}={_fmt(total_b)}, '
        f'diferencia={_fmt(diferencia)}'
        + (f' ({pct:+.1f}%)' if pct is not None else '')
    )
    return _ok('comparar_dos_valores', resultado, desc)


def _distribucion_pct(df: pd.DataFrame, p: dict) -> dict:
    col_grupo = _req(p, 'col_grupo')
    col_valor = _req(p, 'col_valor')
    _validar_col(df, col_grupo)
    _validar_col(df, col_valor)

    agrupado = df.groupby(col_grupo)[col_valor].sum()
    total    = agrupado.sum()
    if total == 0:
        return _error('Total es 0.')

    dist = (agrupado / total * 100).round(2).sort_values(ascending=False)
    rows = [{'grupo': k, 'pct': v} for k, v in dist.items()]
    desc = 'Distribución %: ' + ', '.join(f'{r["grupo"]}={r["pct"]}%' for r in rows[:5])
    if len(rows) > 5:
        desc += f' ... ({len(rows)} grupos)'
    return _ok('distribucion_pct', rows, desc)


def _suma_condicional(df: pd.DataFrame, p: dict) -> dict:
    col_filtro = _req(p, 'col_filtro')
    valores    = _req(p, 'valores')  # lista de valores
    col_valor  = _req(p, 'col_valor')
    _validar_col(df, col_filtro)
    _validar_col(df, col_valor)

    if not isinstance(valores, list):
        valores = [valores]

    mascara = pd.Series(False, index=df.index)
    for v in valores:
        mascara |= _mascara_valor(df[col_filtro], v)
    filtrado = df[mascara]
    total    = float(filtrado[col_valor].sum())
    return _ok(
        'suma_condicional',
        total,
        f'Suma de {col_valor} para {col_filtro} en {valores}: {_fmt(total)} ({len(filtrado)} filas)',
    )


def _rango(df: pd.DataFrame, p: dict) -> dict:
    col_valor = _req(p, 'col_valor')
    _validar_col(df, col_valor)
    serie = df[col_valor].dropna()
    resultado = {
        'min':     round(float(serie.min()), 2),
        'max':     round(float(serie.max()), 2),
        'promedio': round(float(serie.mean()), 2),
        'total':   round(float(serie.sum()), 2),
        'n':       int(serie.count()),
    }
    return _ok(
        'rango',
        resultado,
        f'{col_valor}: min={_fmt(resultado["min"])}, max={_fmt(resultado["max"])}, '
        f'promedio={_fmt(resultado["promedio"])}, total={_fmt(resultado["total"])}',
    )


def _correlacion_simple(df: pd.DataFrame, p: dict) -> dict:
    col_a = _req(p, 'col_a')
    col_b = _req(p, 'col_b')
    _validar_col(df, col_a)
    _validar_col(df, col_b)
    corr = round(float(df[col_a].corr(df[col_b])), 3)
    if math.isnan(corr):
        return _error('No se pudo calcular correlación (datos insuficientes o sin varianza).')
    if corr > 0.7:
        interpretacion = 'correlación positiva fuerte'
    elif corr > 0.3:
        interpretacion = 'correlación positiva moderada'
    elif corr > -0.3:
        interpretacion = 'sin correlación clara'
    elif corr > -0.7:
        interpretacion = 'correlación negativa moderada'
    else:
        interpretacion = 'correlación negativa fuerte'
    return _ok(
        'correlacion_simple',
        corr,
        f'Correlación entre {col_a} y {col_b}: {corr} ({interpretacion})',
    )


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _req(p: dict, key: str) -> Any:
    if key not in p:
        raise ValueError(f'Parámetro requerido faltante: "{key}"')
    return p[key]


def _validar_col(df: pd.DataFrame, col: str):
    if col not in df.columns:
        cols_disponibles = list(df.columns)
        raise ValueError(f'Columna "{col}" no existe. Columnas disponibles: {cols_disponibles}')


def _fmt(valor: Any) -> str:
    """Formatea números para la descripción legible."""
    try:
        v = float(valor)
        if v >= 1_000_000:
            return f'${v:,.0f}'
        if v >= 1_000:
            return f'{v:,.0f}'
        if v == int(v):
            return str(int(v))
        return f'{v:,.2f}'
    except (TypeError, ValueError):
        return str(valor)


def _ok(operacion: str, resultado: Any, descripcion: str) -> dict:
    return {
        'exito':       True,
        'operacion':   operacion,
        'resultado':   resultado,
        'descripcion': descripcion,
        'error':       None,
    }


def _error(mensaje: str) -> dict:
    return {
        'exito':       False,
        'operacion':   None,
        'resultado':   None,
        'descripcion': None,
        'error':       mensaje,
    }


# ---------------------------------------------------------------------------
# Catálogo de operaciones (para que el enrutador lo inyecte en el prompt)
# ---------------------------------------------------------------------------

def catalogo_para_llm() -> str:
    """Retorna el catálogo de operaciones en formato legible para el LLM."""
    lineas = ['Operaciones disponibles en tool_pandas:']
    for nombre, desc in OPERACIONES_DISPONIBLES.items():
        lineas.append(f'  - {nombre}: {desc}')
    lineas.append('')
    lineas.append('Parámetros por operación:')
    lineas.append('  suma_total:           {col_valor}')
    lineas.append('  suma_por_grupo:       {col_grupo, col_valor}')
    lineas.append('  porcentaje_de_total:  {col_filtro, valor, col_valor}')
    lineas.append('  top_n:                {col_valor, n?}')
    lineas.append('  bottom_n:             {col_valor, n?}')
    lineas.append('  filtrar_y_sumar:      {col_filtro, valor, col_valor}')
    lineas.append('  filtrar_y_contar:     {col_filtro, valor}')
    lineas.append('  variacion_pct:        {valor_nuevo, valor_anterior}')
    lineas.append('  contar_por_grupo:     {col_grupo}')
    lineas.append('  promedio_ponderado:   {col_valor, col_peso}')
    lineas.append('  comparar_dos_valores: {col_filtro, valor_a, valor_b, col_valor}')
    lineas.append('  distribucion_pct:     {col_grupo, col_valor}')
    lineas.append('  suma_condicional:     {col_filtro, valores: [...], col_valor}')
    lineas.append('  rango:                {col_valor}')
    lineas.append('  correlacion_simple:   {col_a, col_b}')
    return '\n'.join(lineas)
