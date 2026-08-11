-- =============================================================================
-- setup_ventas_unificada.sql
-- Infraestructura de la vista materializada ventas_unificada en prueba_analisis.
--
-- Ejecutar UNA SOLA VEZ como superusuario en prueba_analisis.
-- La tabla 'items' debe estar ya creada (via scripts/ingestar_items.py).
--
-- Orden de ejecución:
--   1. Crear tabla items (via src/ingestar_items.py o ingesta manual del CSV)
--   2. Crear vista materializada ventas_unificada
--   3. Crear índices
-- =============================================================================

-- ---------------------------------------------------------------------------
-- PASO 1: Vista materializada ventas_unificada
--
-- Une ventas_2025 y ventas_2026 (UNION ALL) y normaliza GRUPO usando la
-- tabla 'items' (maestra de items) como fuente de verdad.
--
-- GRUPO_NORM: GRUPO normalizado desde items. Una referencia → un GRUPO único.
-- LINEA_NORM: LINEA normalizada desde items.
-- TIENE_NORM: TRUE si la referencia tiene match en items.
--
-- Cobertura: 100% (items cubre todas las referencias de ventas_2025 y ventas_2026).
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS ventas_unificada AS
WITH items_norm AS (
    -- Una fila por REFERENCIA con GRUPO normalizado de la maestra de items.
    SELECT DISTINCT ON (TRIM("REFERENCIA"))
        TRIM("REFERENCIA")    AS referencia,
        TRIM("GRUPO")         AS grupo_norm,
        TRIM("LINEA")         AS linea_norm,
        TRIM("DESC_ITEM")     AS desc_item_norm,
        TRIM("PERFIL_PRENDA") AS perfil_norm
    FROM items
    WHERE TRIM("REFERENCIA") IS NOT NULL
      AND TRIM("GRUPO") IS NOT NULL
    ORDER BY TRIM("REFERENCIA"), TRIM("GRUPO")
),
ventas_raw AS (
    SELECT * FROM ventas_2025
    UNION ALL
    SELECT * FROM ventas_2026
)
SELECT
    v.*,
    COALESCE(i.grupo_norm,  TRIM(v."GRUPO"))  AS "GRUPO_NORM",
    COALESCE(i.linea_norm,  TRIM(v."LINEA"))  AS "LINEA_NORM",
    CASE WHEN i.referencia IS NOT NULL THEN TRUE ELSE FALSE END AS "TIENE_NORM"
FROM ventas_raw v
LEFT JOIN items_norm i ON TRIM(v."REFERENCIA") = i.referencia
WITH DATA;

-- ---------------------------------------------------------------------------
-- PASO 2: Índices para las consultas más frecuentes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_vu_movimiento
    ON ventas_unificada (TRIM("DESC_MOVIMIENTO"));

CREATE INDEX IF NOT EXISTS idx_vu_anio
    ON ventas_unificada ("Año");

CREATE INDEX IF NOT EXISTS idx_vu_departamento
    ON ventas_unificada (UPPER(TRIM("DEPARTAMENTO")));

CREATE INDEX IF NOT EXISTS idx_vu_grupo_norm
    ON ventas_unificada ("GRUPO_NORM");

CREATE INDEX IF NOT EXISTS idx_vu_linea
    ON ventas_unificada (TRIM("LINEA"));

CREATE INDEX IF NOT EXISTS idx_vu_referencia
    ON ventas_unificada (TRIM("REFERENCIA"));

CREATE INDEX IF NOT EXISTS idx_vu_mov_anio
    ON ventas_unificada (TRIM("DESC_MOVIMIENTO"), "Año");

-- row_hash es unico en toda la vista (heredado de ventas_2025/ventas_2026).
-- Requerido para poder usar REFRESH MATERIALIZED VIEW CONCURRENTLY, que no
-- bloquea lecturas mientras refresca (importante para refrescos automaticos
-- que puedan caer en horario de uso activo).
CREATE UNIQUE INDEX IF NOT EXISTS idx_vu_row_hash
    ON ventas_unificada (row_hash);

-- ---------------------------------------------------------------------------
-- VERIFICACIÓN FINAL
-- ---------------------------------------------------------------------------
SELECT
    'ventas_unificada' AS vista,
    COUNT(*) AS total_filas,
    COUNT(CASE WHEN "TIENE_NORM" THEN 1 END) AS normalizadas,
    ROUND(COUNT(CASE WHEN "TIENE_NORM" THEN 1 END)::numeric / COUNT(*) * 100, 1) AS pct_cobertura
FROM ventas_unificada;
