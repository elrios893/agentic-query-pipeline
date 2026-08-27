#!/usr/bin/env python3
"""
plantillas.py
Biblioteca de consultas SQL pre-armadas y ya validadas, para preguntas que se
repiten. Objetivo: cuando una pregunta coincide con una plantilla, saltar el
generador y el validador (ver generar_sql_y_validar en orquestador.py) — que
juntos mandan ~70K caracteres de prompt en cada llamada — y ejecutar
directamente un SQL conocido-correcto. Cuando el match es parcial, la
plantilla se ofrece como ejemplo resuelto al generador en vez de ejecutarse
directo (ver Nivel B en procesar_consulta).

Cada plantilla es un archivo JSON independiente en plantillas/ (no un
catálogo único) para que un proceso externo (n8n) pueda agregar plantillas
nuevas escribiendo un archivo, sin tocar código ni pelear con locks.

Formato de cada archivo, ver plantillas/*.json para ejemplos reales:
{
  "id": "top_referencias_por_zona",
  "descripcion": "...",
  "disparadores": ["frase 1", "frase 2", ...],
  "parametros": {
      "fecha_inicio": {"tipo": "fecha", "requerido": false, "default": "@inicio_ytd"},
      "n":            {"tipo": "entero", "requerido": false, "default": 5}
  },
  "sql": "SELECT ... {{fecha_inicio}} ... {{n}} ...",
  "validada_en": "2026-08-27"
}

Regla de correctitud (no de estilo): el SQL guardado NUNCA lleva fechas
literales. Los defaults usan tokens @hoy / @inicio_ytd / @inicio_mes,
resueltos en render() con la MISMA convención de fechas que usa
_reglas_gen() en orquestador.py (year-to-date, etc.) — una plantilla con
'2026-08-01' incrustado queda silenciosamente incorrecta al mes siguiente.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

PLANTILLAS_DIR = Path(__file__).resolve().parent.parent / 'plantillas'

# Puntaje mínimo de solapamiento (sobre 1.0) para que una plantilla se
# considere candidata. Por debajo de esto, candidatas() devuelve [] — es
# preferible no ofrecer nada a ofrecer una plantilla que no aplica: la
# decisión final de ejecutarla directo (Nivel A) o solo como ejemplo
# (Nivel B) la toma igual el LLM del enrutador con su propio criterio.
UMBRAL_MINIMO = 0.30

# Palabras sin valor discriminante para el solapamiento — de lo contrario
# preguntas genéricas ("cuáles son las ventas de...") matchean cualquier
# plantilla solo por compartir estas palabras.
_STOPWORDS = {
    'de', 'la', 'el', 'los', 'las', 'un', 'una', 'y', 'o', 'en', 'a', 'por',
    'para', 'con', 'del', 'al', 'que', 'se', 'su', 'sus', 'es', 'son',
    'me', 'mi', 'dame', 'dime', 'quiero', 'necesito', 'cual', 'cuales',
    'cuanto', 'cuantos', 'cuanta', 'cuantas', 'como',
}

_cache: dict | None = None
_cache_firma: tuple | None = None


def _normalizar(texto: str) -> str:
    """minúsculas, sin tildes, sin puntuación, espacios colapsados."""
    texto = texto.lower().strip()
    texto = unicodedata.normalize('NFKD', texto)
    texto = ''.join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r'[^a-z0-9\s]', ' ', texto)
    texto = re.sub(r'\s+', ' ', texto).strip()
    return texto


def _tokens(texto: str) -> set[str]:
    return {t for t in _normalizar(texto).split(' ') if t and t not in _STOPWORDS}


def _firma_dir() -> tuple:
    """(nombre, mtime) de cada archivo — para invalidar el caché si algo
    cambió en plantillas/ (ej. n8n escribió una plantilla nueva) sin tener
    que releer disco en cada llamada."""
    if not PLANTILLAS_DIR.exists():
        return ()
    return tuple(sorted(
        (p.name, p.stat().st_mtime) for p in PLANTILLAS_DIR.glob('*.json')
    ))


def cargar_plantillas(forzar_recarga: bool = False) -> list[dict]:
    """Lee y cachea plantillas/*.json. Cada dict incluye además
    '_tokens_disparo' (set precomputado) para que candidatas() no tenga que
    renormalizar en cada llamada."""
    global _cache, _cache_firma
    firma = _firma_dir()
    if not forzar_recarga and _cache is not None and firma == _cache_firma:
        return _cache

    plantillas = []
    for archivo in sorted(PLANTILLAS_DIR.glob('*.json')) if PLANTILLAS_DIR.exists() else []:
        try:
            datos = json.loads(archivo.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as e:
            print(f'[plantillas] WARNING: no se pudo leer {archivo.name}: {e}')
            continue
        faltantes = {'id', 'descripcion', 'disparadores', 'parametros', 'sql'} - datos.keys()
        if faltantes:
            print(f'[plantillas] WARNING: {archivo.name} le faltan campos {faltantes}, se ignora')
            continue
        # Un set de tokens POR disparador (no fusionados) — el score de una
        # plantilla es el máximo entre sus frases-disparador, no un promedio
        # diluido por la descripción larga ni por frases-disparador que no
        # aplican a esta pregunta particular. La descripción es solo texto
        # para mostrarle al LLM en Nivel B, nunca entra al matching.
        datos['_tokens_por_disparador'] = [
            _tokens(frase) for frase in datos['disparadores']
        ]
        plantillas.append(datos)

    _cache = plantillas
    _cache_firma = firma
    return plantillas


def _score_frase(tokens_pregunta: set, tokens_frase: set) -> float:
    """Máximo entre Jaccard y el coeficiente de solapamiento (intersección
    sobre el menor de los dos sets). Jaccard solo penaliza demasiado cuando
    la pregunta es mucho más larga que la frase-disparador (normal: la
    pregunta real trae palabras que la frase corta no tiene) — el
    coeficiente de solapamiento compensa eso sin dejar de exigir que la
    frase-disparador esté casi completa dentro de la pregunta."""
    if not tokens_frase:
        return 0.0
    interseccion = len(tokens_pregunta & tokens_frase)
    if interseccion == 0:
        return 0.0
    jaccard = interseccion / len(tokens_pregunta | tokens_frase)
    solapamiento = interseccion / min(len(tokens_pregunta), len(tokens_frase))
    return max(jaccard, solapamiento)


def candidatas(pregunta: str, k: int = 3) -> list[dict]:
    """Solapamiento de tokens entre la pregunta y las frases-disparador de
    cada plantilla (la descripción no entra al matching, ver
    cargar_plantillas). Devuelve hasta k plantillas por encima de
    UMBRAL_MINIMO, ordenadas por score descendente, cada una con su 'score'
    agregado (el mejor entre sus frases-disparador). [] si ninguna alcanza
    el umbral (caso normal — la mayoría de preguntas no tienen plantilla)."""
    tokens_pregunta = _tokens(pregunta)
    if not tokens_pregunta:
        return []

    resultados = []
    for plantilla in cargar_plantillas():
        mejor_score = max(
            (_score_frase(tokens_pregunta, tf) for tf in plantilla['_tokens_por_disparador']),
            default=0.0,
        )
        if mejor_score >= UMBRAL_MINIMO:
            sin_internos = {k2: v for k2, v in plantilla.items() if k2 != '_tokens_por_disparador'}
            resultados.append({**sin_internos, 'score': round(mejor_score, 3)})

    resultados.sort(key=lambda p: p['score'], reverse=True)
    return resultados[:k]


def _resolver_token_fecha(token: str) -> str:
    """Convierte @hoy / @inicio_ytd / @inicio_mes a 'YYYY-MM-DD' usando la
    fecha real del sistema — misma convención que _reglas_gen() en
    orquestador.py (year-to-date por defecto, etc.), para que una plantilla
    y una consulta generada por el LLM ese mismo día usen el mismo rango."""
    hoy = datetime.now()
    if token == '@hoy':
        return hoy.strftime('%Y-%m-%d')
    if token == '@inicio_ytd' or token == '@inicio_anio':
        return f'{hoy.year}-01-01'
    if token == '@inicio_mes':
        return f'{hoy.year}-{hoy.month:02d}-01'
    raise ValueError(f"Token de fecha desconocido: '{token}'")


class PlantillaRenderError(Exception):
    """El render() falló: parámetro requerido faltante o valor inválido para
    su tipo. Quien llama debe tratar esto como 'esta plantilla no aplica
    directo' y caer a Nivel B (referencia few-shot), nunca a Nivel A."""


def _validar_fecha(valor: str) -> str:
    if isinstance(valor, str) and valor.startswith('@'):
        valor = _resolver_token_fecha(valor)
    try:
        datetime.strptime(str(valor), '%Y-%m-%d')
    except (ValueError, TypeError):
        raise PlantillaRenderError(f"Fecha inválida: '{valor}' (se espera YYYY-MM-DD)")
    return f"'{valor}'"


def _validar_entero(valor) -> str:
    try:
        return str(int(valor))
    except (ValueError, TypeError):
        raise PlantillaRenderError(f"Valor entero inválido: '{valor}'")


def _validar_texto(valor: str) -> str:
    if valor is None:
        raise PlantillaRenderError('Valor de texto requerido no puede ser None')
    escapado = str(valor).replace("'", "''")
    return f"'{escapado}'"


_VALIDADORES = {
    'fecha': _validar_fecha,
    'entero': _validar_entero,
    'texto': _validar_texto,
}


def render(plantilla: dict, parametros: dict | None = None) -> str:
    """Sustituye {{param}} en plantilla['sql'] con valores validados por
    tipo. NUNCA concatena texto crudo del usuario — cada valor pasa por su
    validador de tipo antes de entrar al SQL. Lanza PlantillaRenderError si
    falta un parámetro requerido o un valor no valida; quien llama debe
    capturar esto y caer a Nivel B, no a Nivel A (ver orquestador.py)."""
    parametros = parametros or {}
    sql = plantilla['sql']

    for nombre, spec in plantilla['parametros'].items():
        tipo = spec.get('tipo', 'texto')
        validador = _VALIDADORES.get(tipo)
        if validador is None:
            raise PlantillaRenderError(f"Tipo de parámetro desconocido: '{tipo}' (param '{nombre}')")

        if nombre in parametros and parametros[nombre] is not None:
            valor_crudo = parametros[nombre]
        elif 'default' in spec:
            valor_crudo = spec['default']
        elif spec.get('requerido'):
            raise PlantillaRenderError(f"Falta el parámetro requerido '{nombre}'")
        else:
            valor_crudo = None

        if valor_crudo is None:
            raise PlantillaRenderError(f"El parámetro '{nombre}' no tiene valor ni default")

        sql = sql.replace('{{' + nombre + '}}', validador(valor_crudo))

    restantes = re.findall(r'\{\{(\w+)\}\}', sql)
    if restantes:
        raise PlantillaRenderError(f"La plantilla dejó placeholders sin resolver: {restantes}")

    return sql


def dividir_resultado(resultado: dict, plantilla: dict) -> list[dict] | None:
    """
    Plantillas 'multi-bloque' (ej. resumen_ventas.json) ejecutan UNA sola
    consulta pesada que trae varios cortes distintos (zona, línea, talla,
    etc.) etiquetados por una columna común, en vez de una consulta por
    corte — un solo escaneo de la tabla de hechos en vez de N.

    Si plantilla['post_proceso'] declara {'tipo': 'split_por_columna',
    'columna': ..., 'etiquetas': {...}}, separa 'resultado' (un único
    columns/rows con esa columna de etiqueta) en una lista de bloques
    {'nombre', 'columns', 'rows', 'total_filas'} — uno por valor distinto
    de esa columna, sin ella (ya no hace falta, cada bloque es homogéneo).
    Devuelve None si la plantilla no declara post_proceso (caso normal, no
    afecta a ninguna plantilla existente).

    El orden de los bloques sigue el de 'etiquetas' en el JSON (curado a
    mano), no el orden en que las filas salieron del SQL — cualquier valor
    de columna que no esté en 'etiquetas' se agrega al final, sin perderlo.

    Cada entrada de 'etiquetas' puede ser un string simple (solo el nombre
    del bloque) o un dict {'nombre': ..., 'columna': ...} — 'columna'
    renombra, solo en ese bloque, la columna que declara
    post_proceso['columna_valor'] (opcional: el nombre genérico que tiene en
    el SQL, ej. "Etiqueta"). Sirve para que cada tabla muestre un encabezado
    con sentido ("Zona", "Producto"...) en vez del nombre genérico que tiene
    que usar el SQL para poder unir los 5 cortes con UNION ALL.
    """
    post = plantilla.get('post_proceso')
    if not post or post.get('tipo') != 'split_por_columna':
        return None

    columna = post['columna']
    etiquetas = post.get('etiquetas', {})
    columna_valor = post.get('columna_valor')
    columns = resultado.get('columns', [])
    if columna not in columns:
        return None

    idx_col = columns.index(columna)
    columnas_resto = [c for c in columns if c != columna]
    indices_resto = [i for i, c in enumerate(columns) if c != columna]
    idx_valor_en_resto = columnas_resto.index(columna_valor) if columna_valor in columnas_resto else None

    filas_por_valor: dict = {}
    for row in resultado.get('rows', []):
        clave = row[idx_col]
        filas_por_valor.setdefault(clave, []).append([row[i] for i in indices_resto])

    claves_ordenadas = [k for k in etiquetas if k in filas_por_valor]
    claves_ordenadas += [k for k in filas_por_valor if k not in etiquetas]

    bloques = []
    for clave in claves_ordenadas:
        spec = etiquetas.get(clave, clave)
        if isinstance(spec, dict):
            nombre, columna_renombrada = spec.get('nombre', str(clave)), spec.get('columna')
        else:
            nombre, columna_renombrada = spec, None

        cols_bloque = list(columnas_resto)
        if columna_renombrada and idx_valor_en_resto is not None:
            cols_bloque[idx_valor_en_resto] = columna_renombrada

        bloques.append({
            'nombre': nombre,
            'columns': cols_bloque,
            'rows': filas_por_valor[clave],
            'total_filas': len(filas_por_valor[clave]),
        })
    return bloques
