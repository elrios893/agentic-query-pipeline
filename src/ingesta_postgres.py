#!/usr/bin/env python3
"""
Ingesta incremental de datos de ventas a PostgreSQL.
Usa hash MD5 (server-side via PostgreSQL md5()) como clave de deduplicacion.

Modos:
  python ingesta_postgres.py --2026           # ingesta incremental de 2026
  python ingesta_postgres.py --2025           # ingesta incremental de 2025
  python ingesta_postgres.py --2026 --full-sync  # recrea tabla ventas_2026 desde cero
  python ingesta_postgres.py --2025 --full-sync  # recrea tabla ventas_2025 desde cero

Comportamiento incremental:
  - Por cada chunk del TXT: carga en temp table SIN constraint UNIQUE,
    calcula hash server-side, luego INSERT INTO ventas_YYYY ... ON CONFLICT DO NOTHING.
  - La temp table se crea y destruye dentro de cada iteracion (sin ON COMMIT).
  - Duplicados internos del TXT son absorbidos por ON CONFLICT en la tabla real.
  - Segunda ejecucion con el mismo TXT: 0 insertadas, todo saltado.
"""
import sys
import os
import time
import shutil
import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
import numpy as np
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

DB_CONFIG = {
    'host'    : os.environ['DB_HOST'],
    'port'    : int(os.environ['DB_PORT']),
    'dbname'  : os.environ['DB_NAME'],
    'user'    : os.environ['DB_USER'],
    'password': os.environ['DB_PASSWORD'],
}

ENCODING     = 'latin-1'
SEPARADOR    = '\t'

DTYPE_MAP = {
    'int64'         : 'BIGINT',
    'float64'       : 'DOUBLE PRECISION',
    'object'        : 'TEXT',
    'bool'          : 'BOOLEAN',
    'datetime64[ns]': 'TIMESTAMP',
}

def map_dtype(dtype):
    return DTYPE_MAP.get(str(dtype), 'TEXT')

def build_create_table_sql(df, table_name, col_hash='row_hash'):
    """DDL de la tabla destino con columna row_hash UNIQUE al final."""
    cols = []
    for col_name, dtype in df.dtypes.items():
        cols.append(sql.Composed([
            sql.Identifier(col_name), sql.SQL(' '), sql.SQL(map_dtype(dtype))
        ]))
    cols.append(sql.Composed([sql.Identifier(col_hash), sql.SQL(' TEXT UNIQUE')]))
    return sql.SQL('CREATE TABLE IF NOT EXISTS {} ({});').format(
        sql.Identifier(table_name),
        sql.SQL(', ').join(cols),
    )

def build_create_temp_sql(columns, col_hash='row_hash'):
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
    cols.append(sql.Composed([sql.Identifier(col_hash), sql.SQL(' TEXT')]))
    return sql.SQL('CREATE TEMP TABLE {} ({});').format(
        sql.Identifier('ventas_tmp'),
        sql.SQL(', ').join(cols),
    )

def build_hash_expr(columns):
    """Expresion SQL que calcula md5 sobre la concatenacion de todas las columnas."""
    parts = [sql.SQL("COALESCE({}::text, '')").format(sql.Identifier(c)) for c in columns]
    return sql.SQL(" || '|' || ").join(parts)

def ensure_hash_column(cur, conn, tabla_destino, columns, col_hash='row_hash'):
    """Si la tabla existe pero no tiene row_hash, lo agrega y backfillea."""
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (tabla_destino, col_hash),
    )
    if cur.fetchone():
        return  # ya existe

    print('  Agregando columna row_hash...')
    cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN {} TEXT;").format(
        sql.Identifier(tabla_destino), sql.Identifier(col_hash)))
    conn.commit()

    print('  Calculando hashes (server-side MD5)...')
    cur.execute(sql.SQL("UPDATE {} SET {} = md5({})").format(
        sql.Identifier(tabla_destino),
        sql.Identifier(col_hash),
        build_hash_expr(columns),
    ))
    conn.commit()
    print(f'  Hashes generados: {cur.rowcount:,}')

    print('  Eliminando duplicados exactos...')
    cur.execute(f"""
        DELETE FROM {tabla_destino} WHERE ctid IN (
            SELECT ctid FROM (
                SELECT ctid,
                       ROW_NUMBER() OVER (PARTITION BY {col_hash} ORDER BY ctid) AS rn
                FROM {tabla_destino}
            ) t WHERE rn > 1
        )
    """)
    conn.commit()
    print(f'  Duplicados eliminados: {cur.rowcount:,}')

    print('  Creando constraint UNIQUE en row_hash...')
    cur.execute(sql.SQL(
        "ALTER TABLE {} ADD CONSTRAINT uq_{}_row_hash UNIQUE ({});"
    ).format(
        sql.Identifier(tabla_destino),
        sql.SQL(tabla_destino),
        sql.Identifier(col_hash),
    ))
    conn.commit()
    print('  Constraint UNIQUE creada.')

def resolver_ruta_local(ano: str) -> Path:
    """
    Ruta local de trabajo: {DATA_FILE_PATH_LOCAL o 'data_samples'}/{ano}/BSPlanaVentas{ano}.txt

    La lectura/parseo del CSV SIEMPRE ocurre sobre esta ruta local, nunca
    directo sobre la red — evita depender de la estabilidad/permisos de la
    red durante el parseo (que es la parte mas pesada, con engine='python').
    """
    base_local = os.getenv('DATA_FILE_PATH_LOCAL') or 'data_samples'
    return Path(base_local) / ano / f'BSPlanaVentas{ano}.txt'


def sincronizar_desde_red(destino: Path, max_intentos: int = 3, espera_seg: int = 5) -> bool:
    """
    Si DATA_FILE_PATH esta seteada (ruta COMPLETA al archivo de red), lo
    copia hacia `destino` con reintentos (por si el job que lo genera a
    diario lo tiene abierto justo en ese momento). Devuelve True si
    sincronizo desde red, False si DATA_FILE_PATH no esta definida (se usa
    el archivo local tal cual esta, sin tocar la red).
    """
    ruta_red_str = os.getenv('DATA_FILE_PATH')
    if not ruta_red_str:
        return False

    ruta_red = Path(ruta_red_str)
    destino.parent.mkdir(parents=True, exist_ok=True)

    for intento in range(1, max_intentos + 1):
        try:
            shutil.copy2(str(ruta_red), str(destino))
            print(f'  Sincronizado desde red: {ruta_red} -> {destino}')
            return True
        except FileNotFoundError:
            # Ruta de red mal escrita / no accesible: no tiene sentido reintentar.
            raise
        except OSError as e:
            if intento == max_intentos:
                raise RuntimeError(
                    f'No se pudo copiar {ruta_red} tras {max_intentos} intentos: {e}\n'
                    f'Si es una ruta de red, verifica que sea UNC (\\\\servidor\\carpeta\\archivo.txt) '
                    f'en vez de una unidad mapeada (M:\\...), y que la terminal no este '
                    f'corriendo como Administrador (las unidades mapeadas no son visibles '
                    f'para procesos elevados).'
                ) from e
            print(f'  Intento {intento}/{max_intentos} fallo ({e}). Reintentando en {espera_seg}s...')
            time.sleep(espera_seg)


def leer_archivo_con_reintentos(ruta_archivo: Path, max_intentos: int = 3, espera_seg: int = 5) -> pd.DataFrame:
    """
    Lee el TXT fuente con deteccion de encoding por BOM, reintentando si el
    archivo esta bloqueado momentaneamente (ej: el job que lo genera a
    diario lo tiene abierto justo cuando corre la ingesta).

    Solo reintenta PermissionError/OSError (bloqueo/acceso). Un
    FileNotFoundError por ruta mal escrita no se reintenta, falla directo.
    """
    for intento in range(1, max_intentos + 1):
        try:
            with open(str(ruta_archivo), 'rb') as f:
                bom = f.read(2)
            if bom == b'\xff\xfe':
                encoding_real = 'utf-16-le'
                print(f'  Encoding detectado: UTF-16 LE (BOM encontrado)')
            elif bom == b'\xfe\xff':
                encoding_real = 'utf-16-be'
                print(f'  Encoding detectado: UTF-16 BE (BOM encontrado)')
            else:
                encoding_real = ENCODING
                print(f'  Encoding detectado: {ENCODING} (sin BOM)')

            return pd.read_csv(
                str(ruta_archivo),
                sep=SEPARADOR,
                encoding=encoding_real,
                on_bad_lines='skip',
                engine='python',
            )
        except FileNotFoundError:
            # Ruta mal escrita / carpeta inexistente: no tiene sentido reintentar.
            raise
        except OSError as e:
            if intento == max_intentos:
                raise RuntimeError(
                    f'No se pudo leer {ruta_archivo} tras {max_intentos} intentos: {e}\n'
                    f'Si es una ruta de red, verifica que sea UNC (\\\\servidor\\carpeta) '
                    f'en vez de una unidad mapeada (M:\\...), y que la terminal no este '
                    f'corriendo como Administrador (las unidades mapeadas no son visibles '
                    f'para procesos elevados).'
                ) from e
            print(f'  Intento {intento}/{max_intentos} fallo ({e}). Reintentando en {espera_seg}s...')
            time.sleep(espera_seg)


def main():
    # ------------------------------------------------------------------
    # Procesar argumentos: determinar año y modo
    # ------------------------------------------------------------------
    full_sync = '--full-sync' in sys.argv
    
    # Buscar año (--2026 o --2025)
    ano = None
    for arg in sys.argv[1:]:
        if arg.startswith('--') and arg[2:].isdigit():
            ano = arg[2:]
            break
    
    if not ano:
        print('ERROR: Especifica un año con --2026 o --2025')
        print('Uso: python ingesta_postgres.py --2026')
        print('     python ingesta_postgres.py --2025')
        sys.exit(1)
    
    if ano not in ['2025', '2026']:
        print(f'ERROR: Año no válido: {ano}. Usa --2025 o --2026')
        sys.exit(1)
    
    # Ruta local de trabajo (siempre se lee/parsea de aca, nunca directo de red)
    ruta_archivo = resolver_ruta_local(ano)

    # Nombre de tabla destino
    tabla_destino = f'ventas_{ano}'
    chunksize = 5000
    col_hash = 'row_hash'

    # Si DATA_FILE_PATH esta seteada, sincronizar (copiar) desde la red antes
    # de leer — con reintentos si el archivo esta bloqueado momentaneamente
    t0 = datetime.now()
    try:
        leido_de_red = sincronizar_desde_red(ruta_archivo)
    except FileNotFoundError:
        print(f'ERROR: Archivo de red no encontrado: {os.getenv("DATA_FILE_PATH")}')
        sys.exit(1)
    except RuntimeError as e:
        print(f'ERROR: {e}')
        sys.exit(1)

    origen = 'DATA_FILE_PATH (sincronizado desde red)' if leido_de_red else 'DATA_FILE_PATH_LOCAL (o default)'
    print(f'  Origen del archivo: {origen}')
    print(f'[{t0:%H:%M:%S}] Leyendo {ruta_archivo}...')

    try:
        df = leer_archivo_con_reintentos(ruta_archivo)
    except FileNotFoundError:
        print(f'ERROR: Archivo no encontrado: {ruta_archivo}')
        print('Verifica DATA_FILE_PATH / DATA_FILE_PATH_LOCAL en .env, o que exista '
              f'la carpeta data_samples/{ano}/ y el archivo BSPlanaVentas{ano}.txt')
        sys.exit(1)
    except RuntimeError as e:
        print(f'ERROR: {e}')
        sys.exit(1)

    # Limpiar BOM residual del nombre de la primera columna (puede ocurrir con UTF-16)
    df.columns = [c.lstrip('\ufeff').lstrip('\xff\xfe') for c in df.columns]
    
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
            print(f'Modo --full-sync: eliminando tabla {tabla_destino}...')
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE;").format(
                sql.Identifier(tabla_destino)))
            conn.commit()

        cur.execute(
            "SELECT EXISTS(SELECT FROM information_schema.tables WHERE table_name=%s)",
            (tabla_destino,),
        )
        if not cur.fetchone()[0]:
            print(f'Creando tabla {tabla_destino}...')
            cur.execute(build_create_table_sql(df, tabla_destino, col_hash))
            conn.commit()
            print('Tabla creada.')
        else:
            print(f'Tabla {tabla_destino} encontrada.')
            ensure_hash_column(cur, conn, tabla_destino, columns, col_hash)

        # ------------------------------------------------------------------
        # 2. Ingesta incremental chunk a chunk
        # ------------------------------------------------------------------
        col_ids  = [sql.Identifier(c) for c in columns]
        hash_expr = build_hash_expr(columns)
        inserted_total = 0
        skipped_total  = 0

        for start in range(0, total, chunksize):
            chunk = df.iloc[start:start + chunksize]

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
            cur.execute(build_create_temp_sql(col_dtypes, col_hash))

            # Insertar datos en temp (sin hash)
            execute_values(
                cur,
                sql.SQL("INSERT INTO ventas_tmp ({}) VALUES %s").format(
                    sql.SQL(', ').join(col_ids)
                ).as_string(cur),
                rows,
                page_size=chunksize,
            )

            # Calcular hash server-side en la temp table
            cur.execute(sql.SQL("UPDATE ventas_tmp SET {} = md5({})").format(
                sql.Identifier(col_hash), hash_expr))

            # Mover a tabla real; ON CONFLICT salta duplicados
            cur.execute(sql.SQL("""
                WITH moved AS (
                    INSERT INTO {} SELECT * FROM ventas_tmp
                    ON CONFLICT ({}) DO NOTHING
                    RETURNING 1
                )
                SELECT COUNT(*) FROM moved
            """).format(
                sql.Identifier(tabla_destino),
                sql.Identifier(col_hash),
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
        cur.execute(f"SELECT COUNT(*) FROM {tabla_destino}")
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
