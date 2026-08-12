"""
tools/buscar_web.py
Búsqueda web simple vía Tavily API, para que el agente conversacional pueda
consultar información externa (tendencias de mercado, contexto de sector, etc.)
que no existe en la sesión ni en la base de datos.

Uso:
    from tools.buscar_web import buscar_web
    resultados = buscar_web("tendencias moda deportiva femenina Colombia 2026")
"""
import os
import requests

TAVILY_URL = 'https://api.tavily.com/search'

# Dominios de baja autoridad para análisis de mercado: redes sociales y video
# corto. Tavily los indexa y a veces rankean bien por keywords, pero su
# contenido no tiene respaldo editorial ni datos verificables — se excluyen
# de la búsqueda para priorizar prensa, firmas de investigación y medios
# especializados.
DOMINIOS_EXCLUIDOS = [
    'instagram.com', 'tiktok.com', 'youtube.com', 'youtu.be',
    'facebook.com', 'pinterest.com', 'x.com', 'twitter.com', 'threads.net',
]

# Resultados con score de relevancia de Tavily por debajo de esto se
# descartan aunque hayan venido en la respuesta — suelen ser ruido.
SCORE_MINIMO = 0.35


def buscar_web(query: str, max_resultados: int = 5) -> list[dict]:
    """
    Ejecuta una búsqueda web vía Tavily y devuelve resultados resumidos,
    filtrando fuentes de baja autoridad (redes sociales/video) y resultados
    con score de relevancia bajo.

    Args:
        query: texto de búsqueda.
        max_resultados: máximo de resultados a devolver (default 5).

    Returns:
        Lista de dicts {'titulo', 'contenido', 'url', 'score'}. Lista vacía
        si no hay API key configurada, la búsqueda falla o no hay resultados
        útiles tras el filtro.
    """
    api_key = os.getenv('TAVILY_KEY', '')
    if not api_key:
        print('[buscar_web] TAVILY_KEY no configurada — omitiendo búsqueda.')
        return []
    if not query.strip():
        print('[buscar_web] query vacía — omitiendo búsqueda.')
        return []

    payload = {
        'api_key': api_key,
        'query': query,
        'search_depth': 'advanced',
        'max_results': max(max_resultados * 2, 8),  # pedir de más: luego se filtra
        'exclude_domains': DOMINIOS_EXCLUIDOS,
    }
    print(f'[buscar_web] POST {TAVILY_URL} — query={query!r} search_depth=advanced excluye={DOMINIOS_EXCLUIDOS}')

    try:
        resp = requests.post(TAVILY_URL, json=payload, timeout=20)
        print(f'[buscar_web] status_code={resp.status_code}')
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as e:
        print(f'[buscar_web] HTTPError: {e} — body: {resp.text[:500]!r}')
        return []
    except Exception as e:
        print(f'[buscar_web] Error en búsqueda: {e!r}')
        return []

    crudos = data.get('results', [])
    print(f'[buscar_web] respuesta_answer={data.get("answer", "")!r}')
    print(f'[buscar_web] {len(crudos)} resultado(s) crudo(s) de Tavily (antes de filtrar)')

    resultados = []
    for r in crudos:
        titulo    = r.get('title', '')
        url       = r.get('url', '') or ''
        score     = r.get('score', 0) or 0
        contenido = (r.get('content', '') or '')[:1500]

        descartado_score = score < SCORE_MINIMO
        print(
            f'  [buscar_web] crudo: titulo={titulo!r} url={url} score={score} '
            f'contenido_len={len(r.get("content") or "")}'
            f'{" -> DESCARTADO (score bajo)" if descartado_score else ""}'
        )
        if descartado_score:
            continue

        resultados.append({
            'titulo':    titulo,
            'contenido': contenido,
            'url':       url,
            'score':     score,
        })
        if len(resultados) >= max_resultados:
            break

    print(f'[buscar_web] {len(resultados)} resultado(s) útil(es) tras filtro de dominios/score')
    return resultados
