#!/usr/bin/env python3
"""
Tool: consultar_db
Ejecuta consultas SQL de SOLO LECTURA sobre la base CreytexToSQL.
Uso: python consultar_db.py "SELECT * FROM ventas LIMIT 5;"
Retorna resultados en formato JSON por stdout.
"""
import sys
import os
import json
import psycopg2
import re
import decimal
import datetime
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    'host': os.environ['DB_HOST'],
    'port': int(os.environ['DB_PORT']),
    'dbname': os.environ['DB_NAME'],
    'user': os.environ['DB_USER'],
    'password': os.environ['DB_PASSWORD'],
}

READ_ONLY_KEYWORDS = [
    'select', 'explain', 'analyze', 'with', 'show', 'values',
]

PROHIBITED_KEYWORDS = [
    'insert', 'update', 'delete', 'drop', 'truncate', 'alter',
    'create', 'replace', 'grant', 'revoke', 'copy', 'import',
]

def is_read_only(query: str) -> bool:
    # Eliminar comentarios SQL de línea (-- ...) y de bloque (/* ... */)
    cleaned = re.sub(r'--[^\n]*', '', query)
    cleaned = re.sub(r'/\*.*?\*/', '', cleaned, flags=re.DOTALL)
    # Eliminar strings entre comillas simples (literales de texto)
    cleaned = re.sub(r"'[^']*'", '', cleaned)
    # Eliminar identificadores entre comillas dobles (nombres de columna/tabla)
    cleaned = re.sub(r'"[^"]*"', '', cleaned)
    # Eliminar backticks
    cleaned = re.sub(r'`[^`]*`', '', cleaned)

    first_words = re.findall(r'\b\w+\b', cleaned)
    if not first_words:
        return False
    first_keyword = first_words[0].lower()
    if first_keyword in PROHIBITED_KEYWORDS:
        return False
    if first_keyword not in READ_ONLY_KEYWORDS:
        return False
    for kw in PROHIBITED_KEYWORDS:
        if re.search(r'\b' + kw + r'\b', cleaned, re.IGNORECASE):
            return False
    return True

def _serializar_celda(cell):
    """Preserva tipos numéricos nativos (int/float/bool) para que el resto
    del pipeline (métricas, formateo de tablas) pueda operar sobre ellos en
    vez de recibir todo como string. Decimal -> float; fechas/otros -> str."""
    if cell is None or isinstance(cell, (bool, int, float)):
        return cell
    if isinstance(cell, decimal.Decimal):
        return float(cell)
    if isinstance(cell, datetime.datetime):
        # DATE_TRUNC('week'/'month'/'day', ...) devuelve timestamp con hora
        # 00:00:00 (a veces con zona horaria) aunque el dato real es una
        # fecha simple — mostrar la hora/offset ahi es ruido, no informacion
        # ("2026-07-27T00:00:00-05:00" en vez de "2026-07-27").
        if cell.time() == datetime.time(0, 0):
            return cell.date().isoformat()
        return cell.isoformat()
    if isinstance(cell, datetime.date):
        return cell.isoformat()
    return str(cell)


def execute_query(query: str, limit: int = None) -> dict:
    if not is_read_only(query):
        return {
            'success': False,
            'error': 'Operación no permitida. Solo consultas SELECT de lectura.',
            'error_fuente': 'guardia',
        }

    if limit and 'limit' not in query.lower():
        query = query.rstrip('; ') + f' LIMIT {limit};'

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    cur = conn.cursor()

    try:
        cur.execute(query)

        if cur.description:
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            result = {
                'success': True,
                'columns': columns,
                'rows': [[_serializar_celda(cell) for cell in row] for row in rows],
                'total_filas': len(rows),
            }
        else:
            result = {
                'success': True,
                'message': 'Consulta ejecutada sin resultados de filas.',
            }

        return result
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'error_fuente': 'postgres',
        }
    finally:
        cur.close()
        conn.close()

if __name__ == '__main__':
    query = os.environ.get('SQL_QUERY')
    limit = 1000

    if query:
        if len(sys.argv) > 1 and sys.argv[1].isdigit():
            limit = int(sys.argv[1])
    else:
        if len(sys.argv) < 2:
            print(json.dumps({
                'success': False,
                'error': 'Debes proporcionar una consulta SQL como argumento o en variable SQL_QUERY.',
            }))
            sys.exit(1)
        query = sys.argv[1]
        if len(sys.argv) > 2 and sys.argv[2].isdigit():
            limit = int(sys.argv[2])

    result = execute_query(query, limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
