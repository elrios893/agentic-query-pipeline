#!/usr/bin/env python3
"""
Ingesta de la maestra de items (data_samples/items/MAESTRA DE ITEMS.csv) a PostgreSQL.
Tabla destino: items.

Es una tabla maestra pequeña: cada corrida hace full-sync (DROP + CREATE + INSERT),
reflejando siempre el estado actual del CSV. No usa deduplicacion incremental
como src/ingesta_postgres.py (pensado para archivos de ventas mucho mas grandes).

Uso:
    python src/ingestar_items.py
"""
import sys
import os
import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
import numpy as np
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

DB_CONFIG = {
    'host'    : os.environ['DB_HOST'],
    'port'    : int(os.environ['DB_PORT']),
    'dbname'  : os.environ['DB_NAME'],
    'user'    : os.environ['DB_USER'],
    'password': os.environ['DB_PASSWORD'],
}

RUTA_CSV   = BASE_DIR / 'data_samples' / 'items' / 'MAESTRA DE ITEMS.csv'
TABLA      = 'items'
SEPARADOR  = ';'
ENCODING   = 'utf-8-sig'  # el CSV trae BOM UTF-8

DTYPE_MAP = {
    'int64'         : 'BIGINT',
    'float64'       : 'DOUBLE PRECISION',
    'object'        : 'TEXT',
    'bool'          : 'BOOLEAN',
    'datetime64[ns]': 'TIMESTAMP',
}

def map_dtype(dtype):
    return DTYPE_MAP.get(str(dtype), 'TEXT')

def limpiar_nombre_columna(col):
    """Nombres de columna quedan tal cual (se citan con sql.Identifier),
    solo se recorta espacios en los extremos."""
    return col.strip()

def build_create_table_sql(df, table_name):
    cols = []
    for col_name, dtype in df.dtypes.items():
        cols.append(sql.Composed([
            sql.Identifier(col_name), sql.SQL(' '), sql.SQL(map_dtype(dtype))
        ]))
    return sql.SQL('CREATE TABLE {} ({});').format(
        sql.Identifier(table_name),
        sql.SQL(', ').join(cols),
    )

def main():
    if not RUTA_CSV.exists():
        print(f'ERROR: Archivo no encontrado: {RUTA_CSV}')
        sys.exit(1)

    t0 = datetime.now()
    print(f'[{t0:%H:%M:%S}] Leyendo {RUTA_CSV}...')

    df = pd.read_csv(
        str(RUTA_CSV),
        sep=SEPARADOR,
        encoding=ENCODING,
        on_bad_lines='skip',
        engine='python',
    )
    df.columns = [limpiar_nombre_columna(c) for c in df.columns]

    columns = list(df.columns)
    total   = len(df)
    print(f'[{datetime.now():%H:%M:%S}] Filas: {total:,}  Columnas: {len(columns)}')

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print(f'Recreando tabla {TABLA} (full-sync)...')
        cur.execute(sql.SQL('DROP TABLE IF EXISTS {} CASCADE;').format(sql.Identifier(TABLA)))
        cur.execute(build_create_table_sql(df, TABLA))
        conn.commit()
        print('Tabla creada.')

        col_ids   = [sql.Identifier(c) for c in columns]
        insert_sql = sql.SQL('INSERT INTO {} ({}) VALUES %s').format(
            sql.Identifier(TABLA),
            sql.SQL(', ').join(col_ids),
        ).as_string(cur)

        chunksize = 5000
        inserted_total = 0
        for start in range(0, total, chunksize):
            chunk = df.iloc[start:start + chunksize]
            rows = chunk.where(pd.notna(chunk), None).values.tolist()
            rows = [
                [None if isinstance(v, (float, np.floating)) and np.isnan(v) else v
                 for v in row]
                for row in rows
            ]
            execute_values(cur, insert_sql, rows, page_size=chunksize)
            inserted_total += len(rows)
            conn.commit()
            print(f'  {min(start + chunksize, total):>{len(str(total))},}/{total:,} insertadas')

        tf = datetime.now()
        print(f'\n[{tf:%H:%M:%S}] Completado en {tf - t0}')
        print(f'  Filas insertadas en {TABLA}: {inserted_total:,}')

    except Exception as e:
        conn.rollback()
        print(f'\nERROR: {e}')
        raise
    finally:
        cur.close()
        conn.close()

if __name__ == '__main__':
    main()
