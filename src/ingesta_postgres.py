#!/usr/bin/env python3
"""
Ingesta incremental de BSPlanaVentas2026.txt a PostgreSQL.
Usa hash MD5 (server-side via PostgreSQL md5()) como clave de deduplicacion.

Modos:
  python ingesta_postgres.py               # incremental: solo inserta filas nuevas
  python ingesta_postgres.py --full-sync   # recrea la tabla desde cero

Comportamiento incremental:
  - Por cada chunk del TXT: carga en temp table SIN constraint UNIQUE,
    calcula hash server-side, luego INSERT INTO ventas ... ON CONFLICT DO NOTHING.
  - La temp table se crea y destruye dentro de cada iteracion (sin ON COMMIT).
  - Duplicados internos del TXT son absorbidos por ON CONFLICT en la tabla real.
  - Segunda ejecucion con el mismo TXT: 0 insertadas, todo saltado.
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

load_dotenv()

RUTA_ARCHIVO = r'data_samples\BSPlanaVentas2026.txt'
ENCODING     = 'latin-1'
SEPARADOR    = '\t'

DB_CONFIG = {
    'host'    : os.environ['DB_HOST'],
    'port'    : int(os.environ['DB_PORT']),
    'dbname'  : os.environ['DB_NAME'],
    'user'    : os.environ['DB_USER'],
    'password': os.environ['DB_PASSWORD'],
}

TABLA_DESTINO = 'ventas'
CHUNKSIZE     = 5000
COL_HASH      = 'row_hash'

DTYPE_MAP = {
    'int64'         : 'BIGINT',
    'float64'       : 'DOUBLE PRECISION',
    'object'        : 'TEXT',
    'bool'          : 'BOOLEAN',
    'datetime64[ns]': 'TIMESTAMP',
}

def map_dtype(dtype):
    return DTYPE_MAP.get(str(dtype), 'TEXT')

def build_create_table_sql(df, table_name):
    """DDL de la tabla destino con columna row_hash UNIQUE al final."""
    cols = []
    for col_name, dtype in df.dtypes.items():
        cols.append(sql.Composed([
            sql.Identifier(col_name), sql.SQL(' '), sql.SQL(map_dtype(dtype))
        ]))
    cols.append(sql.Composed([sql.Identifier(COL_HASH), sql.SQL(' TEXT UNIQUE')]))
    return sql.SQL('CREATE TABLE IF NOT EXISTS {} ({});').format(
        sql.Identifier(table_name),
        sql.SQL(', ').join(cols),
    )

def build_create_temp_sql(columns):
    """
    DDL de la temp table: mismas columnas de datos + row_hash TEXT (sin UNIQUE).
    Sin constraint para que filas duplicadas del mismo chunk no fallen aqui;
    el ON CONFLICT de la tabla real se encarga de la dedup final.
    """
    cols = []
    for col_name, dtype in columns:
        cols.append(sql.Composed([
            sql.Identifier(col_name), sql.SQL(' '), sql.SQL(map_dtype(dtype))
        ]))
    cols.append(sql.Composed([sql.Identifier(COL_HASH), sql.SQL(' TEXT')]))
    return sql.SQL('CREATE TEMP TABLE {} ({});').format(
        sql.Identifier('ventas_tmp'),
        sql.SQL(', ').join(cols),
    )

def build_hash_expr(columns):
    """Expresion SQL que calcula md5 sobre la concatenacion de todas las columnas."""
    parts = [sql.SQL("COALESCE({}::text, '')").format(sql.Identifier(c)) for c in columns]
    return sql.SQL(" || '|' || ").join(parts)

def ensure_hash_column(cur, conn, columns):
    """Si la tabla existe pero no tiene row_hash, lo agrega y backfillea."""
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (TABLA_DESTINO, COL_HASH),
    )
    if cur.fetchone():
        return  # ya existe

    print('  Agregando columna row_hash...')
    cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN {} TEXT;").format(
        sql.Identifier(TABLA_DESTINO), sql.Identifier(COL_HASH)))
    conn.commit()

    print('  Calculando hashes (server-side MD5)...')
    cur.execute(sql.SQL("UPDATE {} SET {} = md5({})").format(
        sql.Identifier(TABLA_DESTINO),
        sql.Identifier(COL_HASH),
        build_hash_expr(columns),
    ))
    conn.commit()
    print(f'  Hashes generados: {cur.rowcount:,}')

    print('  Eliminando duplicados exactos...')
    cur.execute(f"""
        DELETE FROM {TABLA_DESTINO} WHERE ctid IN (
            SELECT ctid FROM (
                SELECT ctid,
                       ROW_NUMBER() OVER (PARTITION BY {COL_HASH} ORDER BY ctid) AS rn
                FROM {TABLA_DESTINO}
            ) t WHERE rn > 1
        )
    """)
    conn.commit()
    print(f'  Duplicados eliminados: {cur.rowcount:,}')

    print('  Creando constraint UNIQUE en row_hash...')
    cur.execute(sql.SQL(
        "ALTER TABLE {} ADD CONSTRAINT uq_{}_row_hash UNIQUE ({});"
    ).format(
        sql.Identifier(TABLA_DESTINO),
        sql.SQL(TABLA_DESTINO),
        sql.Identifier(COL_HASH),
    ))
    conn.commit()
    print('  Constraint UNIQUE creada.')

def main():
    full_sync = '--full-sync' in sys.argv

    t0 = datetime.now()
    print(f'[{t0:%H:%M:%S}] Leyendo {RUTA_ARCHIVO}...')
    df = pd.read_csv(RUTA_ARCHIVO, sep=SEPARADOR, encoding=ENCODING, low_memory=False)
    columns     = list(df.columns)
    col_dtypes  = list(df.dtypes.items())   # [(col, dtype), ...]
    total       = len(df)
    print(f'[{datetime.now():%H:%M:%S}] Filas: {total:,}  Columnas: {len(columns)}')

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # ------------------------------------------------------------------
        # 1. Preparar tabla destino
        # ------------------------------------------------------------------
        if full_sync:
            print('Modo --full-sync: eliminando tabla...')
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE;").format(
                sql.Identifier(TABLA_DESTINO)))
            conn.commit()

        cur.execute(
            "SELECT EXISTS(SELECT FROM information_schema.tables WHERE table_name=%s)",
            (TABLA_DESTINO,),
        )
        if not cur.fetchone()[0]:
            print(f'Creando tabla {TABLA_DESTINO}...')
            cur.execute(build_create_table_sql(df, TABLA_DESTINO))
            conn.commit()
            print('Tabla creada.')
        else:
            print(f'Tabla {TABLA_DESTINO} encontrada.')
            ensure_hash_column(cur, conn, columns)

        # ------------------------------------------------------------------
        # 2. Ingesta incremental chunk a chunk
        # ------------------------------------------------------------------
        col_ids  = [sql.Identifier(c) for c in columns]
        hash_expr = build_hash_expr(columns)
        inserted_total = 0
        skipped_total  = 0

        for start in range(0, total, CHUNKSIZE):
            chunk = df.iloc[start:start + CHUNKSIZE]

            # Normalizar NaN → None (NULL en PostgreSQL)
            rows = chunk.where(pd.notna(chunk), None).values.tolist()
            rows = [
                [None if isinstance(v, (float, np.floating)) and np.isnan(v) else v
                 for v in row]
                for row in rows
            ]
            n = len(rows)

            # --- temp table nueva por iteracion (sin UNIQUE) ---
            cur.execute("DROP TABLE IF EXISTS ventas_tmp;")
            cur.execute(build_create_temp_sql(col_dtypes))

            # Insertar datos en temp (sin hash)
            execute_values(
                cur,
                sql.SQL("INSERT INTO ventas_tmp ({}) VALUES %s").format(
                    sql.SQL(', ').join(col_ids)
                ).as_string(cur),
                rows,
                page_size=CHUNKSIZE,
            )

            # Calcular hash server-side en la temp table
            cur.execute(sql.SQL("UPDATE ventas_tmp SET {} = md5({})").format(
                sql.Identifier(COL_HASH), hash_expr))

            # Mover a tabla real; ON CONFLICT salta duplicados
            cur.execute(sql.SQL("""
                WITH moved AS (
                    INSERT INTO {} SELECT * FROM ventas_tmp
                    ON CONFLICT ({}) DO NOTHING
                    RETURNING 1
                )
                SELECT COUNT(*) FROM moved
            """).format(
                sql.Identifier(TABLA_DESTINO),
                sql.Identifier(COL_HASH),
            ))
            inserted = cur.fetchone()[0]
            skipped  = n - inserted
            inserted_total += inserted
            skipped_total  += skipped

            cur.execute("DROP TABLE IF EXISTS ventas_tmp;")
            conn.commit()

            print(f'  {start + n:>{len(str(total))},}/{total:,} '
                  f'| +nuevas: {inserted:,}  ~saltadas: {skipped:,}')

        # ------------------------------------------------------------------
        # 3. Resumen
        # ------------------------------------------------------------------
        cur.execute(f"SELECT COUNT(*) FROM {TABLA_DESTINO}")
        total_db = cur.fetchone()[0]

        tf = datetime.now()
        print(f'\n[{tf:%H:%M:%S}] Completado en {tf - t0}')
        print(f'  Insertadas (nuevas) : {inserted_total:,}')
        print(f'  Saltadas (existian) : {skipped_total:,}')
        print(f'  Total filas en DB   : {total_db:,}')

    except Exception as e:
        conn.rollback()
        print(f'\nERROR: {e}')
        raise
    finally:
        cur.execute("DROP TABLE IF EXISTS ventas_tmp;")
        try:
            conn.commit()
        except Exception:
            pass
        cur.close()
        conn.close()

if __name__ == '__main__':
    main()
