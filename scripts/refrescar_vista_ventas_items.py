#!/usr/bin/env python3
"""
scripts/refrescar_vista_ventas_items.py
Refresca la vista materializada ventas_unificada en prueba_analisis.
La vista usa la tabla 'items' (maestra de items) como fuente de GRUPO normalizado.

Uso:
    python scripts/refrescar_vista_ventas_items.py
    python scripts/refrescar_vista_ventas_items.py --dry-run   (solo verificar, sin refrescar)

Retorna exit code 0 si OK, 1 si error.
"""
import os
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

# Forzar UTF-8 en stdout para evitar errores en consola Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ---------------------------------------------------------------------------
# Setup de paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / '.env')

try:
    import psycopg2
except ImportError:
    print('[ERROR] psycopg2 no instalado. Ejecutar: pip install psycopg2-binary')
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
VISTA = 'ventas_unificada'

CONN_PARAMS = dict(
    host=os.getenv('DB_HOST', 'localhost'),
    port=int(os.getenv('DB_PORT', 5432)),
    dbname=os.getenv('DB_NAME', 'prueba_analisis'),
    user=os.getenv('DB_USER', 'postgres'),
    password=os.getenv('DB_PASSWORD', 'postgres'),
)


# ---------------------------------------------------------------------------
# Lógica principal
# ---------------------------------------------------------------------------

def refrescar(dry_run: bool = False) -> bool:
    """
    Ejecuta REFRESH MATERIALIZED VIEW.

    Returns:
        True si éxito, False si error.
    """
    sql_refresh = f'REFRESH MATERIALIZED VIEW {VISTA};'
    sql_count   = f'SELECT COUNT(*) FROM {VISTA};'
    sql_cobertura = f"""
        SELECT
            COUNT(*) AS total,
            COUNT(CASE WHEN "TIENE_NORM" THEN 1 END) AS normalizadas,
            ROUND(
                COUNT(CASE WHEN "TIENE_NORM" THEN 1 END)::numeric
                / NULLIF(COUNT(*), 0) * 100, 1
            ) AS pct_cobertura
        FROM {VISTA};
    """

    ts_inicio = datetime.now()
    print(f'[{ts_inicio:%Y-%m-%d %H:%M:%S}] Iniciando refresco de {VISTA}...')
    print(f'  Fuente de normalizacion: tabla items (maestra de items)')
    if dry_run:
        print(f'  Modo: DRY-RUN -- solo se verifica la conexion, no se refresca.')

    try:
        conn = psycopg2.connect(**CONN_PARAMS)
        conn.autocommit = True
        cur = conn.cursor()

        # Verificar que la vista existe
        cur.execute(
            "SELECT 1 FROM pg_matviews WHERE matviewname = %s;",
            (VISTA,)
        )
        if not cur.fetchone():
            print(f'[ERROR] La vista materializada "{VISTA}" no existe en {CONN_PARAMS["dbname"]}.')
            print('        Ejecutar primero: scripts/setup_ventas_unificada.sql')
            cur.close()
            conn.close()
            return False

        print(f'  Conexión OK → {CONN_PARAMS["dbname"]} en {CONN_PARAMS["host"]}:{CONN_PARAMS["port"]}')

        if dry_run:
            # Solo contar filas actuales sin refrescar
            cur.execute(sql_count)
            total_actual = cur.fetchone()[0]
            print(f'  Filas actuales en la vista: {total_actual:,}')
            print('  [DRY-RUN] Sin cambios realizados.')
            cur.close()
            conn.close()
            return True

        t0 = time.time()
        print(f'  Ejecutando: {sql_refresh.strip()}')
        cur.execute(sql_refresh)
        duracion = round(time.time() - t0, 1)

        # Estadísticas post-refresco
        cur.execute(sql_cobertura)
        row = cur.fetchone()
        total        = int(row[0]) if row else 0
        normalizadas = int(row[1]) if row else 0
        pct          = float(row[2]) if row and row[2] else 0.0

        ts_fin = datetime.now()
        print(f'[{ts_fin:%Y-%m-%d %H:%M:%S}] Refresco completado en {duracion}s.')
        print(f'  Filas totales:       {total:,}')
        print(f'  Filas normalizadas:  {normalizadas:,} ({pct}%)')
        print(f'  Sin normalizar:      {total - normalizadas:,} (usan GRUPO original)')

        cur.close()
        conn.close()
        return True

    except psycopg2.OperationalError as e:
        print(f'[ERROR] No se pudo conectar a la base de datos: {e}')
        return False
    except psycopg2.Error as e:
        print(f'[ERROR] Error de PostgreSQL: {e}')
        return False
    except Exception as e:
        print(f'[ERROR] Error inesperado: {e}')
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Refresca la vista materializada ventas_unificada.'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Solo verificar conexion y mostrar estado actual sin refrescar.',
    )
    args = parser.parse_args()

    ok = refrescar(dry_run=args.dry_run)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
