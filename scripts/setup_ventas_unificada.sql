-- =============================================================================
-- setup_ventas_unificada.sql
-- Infraestructura de la vista materializada ventas_unificada en prueba_analisis.
--
-- Ejecutar UNA SOLA VEZ como superusuario en prueba_analisis.
-- Requiere que el servidor Creytex_Segmentacion_V1 sea accesible desde
-- el mismo host PostgreSQL (localhost:5432).
--
-- Orden de ejecución:
--   1. Instalar extensión postgres_fdw
--   2. Crear servidor foráneo apuntando a Creytex_Segmentacion_V1
--   3. Crear usuario mapping
--   4. Crear foreign table grupo_norm_fdw
--   5. Crear vista materializada ventas_unificada
--   6. Crear índices para consultas frecuentes
-- =============================================================================

-- ---------------------------------------------------------------------------
-- PASO 1: Extensión postgres_fdw
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

-- ---------------------------------------------------------------------------
-- PASO 2: Servidor foráneo apuntando a Creytex_Segmentacion_V1
-- Ajustar host/port si la BD está en otro servidor físico.
-- ---------------------------------------------------------------------------
CREATE SERVER IF NOT EXISTS seg_server
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (
        host 'localhost',
        port '5432',
        dbname 'Creytex_Segmentacion_V1'
    );

-- ---------------------------------------------------------------------------
-- PASO 3: User mapping — el usuario postgres de prueba_analisis
-- accede a Creytex_Segmentacion_V1 con sus propias credenciales.
-- Cambiar password si difiere.
-- ---------------------------------------------------------------------------
CREATE USER MAPPING IF NOT EXISTS FOR postgres
    SERVER seg_server
    OPTIONS (user 'postgres', password 'postgres');

-- ---------------------------------------------------------------------------
-- PASO 4: Foreign table — solo las columnas que necesitamos del snapshot
-- ---------------------------------------------------------------------------
CREATE FOREIGN TABLE IF NOT EXISTS grupo_norm_fdw (
    referencia_base TEXT,
    categoria       TEXT,
    linea           TEXT,
    perfil_prenda   TEXT,
    estado          TEXT,
    precio_unitario NUMERIC,
    loaded_at       TIMESTAMPTZ
)
SERVER seg_server
OPTIONS (
    schema_name 'public',
    table_name  'referencias_snapshot_actual'
);

-- ---------------------------------------------------------------------------
-- PASO 5: Vista materializada ventas_unificada
--
-- Une ventas_2025 y ventas_2026 (UNION ALL) y enriquece con GRUPO normalizado.
-- La columna "GRUPO_NORM" reemplaza a "GRUPO" para análisis:
--   - Si la referencia existe en el snapshot → usa categoria del snapshot
--   - Si no existe → fallback al GRUPO original de la tabla de ventas
--
-- NOTA: se usa referencia_base sin DISTINCT porque una referencia base
-- puede tener múltiples colores/SKUs pero SIEMPRE la misma categoria.
-- El GROUP BY en la subconsulta garantiza un único valor por referencia_base.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS ventas_unificada AS
WITH snapshot AS (
    -- Deduplica: una referencia_base → una categoria (ya verificado sin conflictos)
    SELECT DISTINCT ON (referencia_base)
        referencia_base,
        categoria,
        linea          AS linea_snap,
        perfil_prenda  AS perfil_snap,
        estado         AS estado_snap,
        precio_unitario
    FROM grupo_norm_fdw
    WHERE referencia_base IS NOT NULL
    ORDER BY referencia_base, loaded_at DESC
),
ventas_raw AS (
    SELECT *, 2025 AS "anio_tabla" FROM ventas_2025
    UNION ALL
    SELECT *, 2026 AS "anio_tabla" FROM ventas_2026
)
SELECT
    v.*,
    -- GRUPO normalizado: snapshot si existe, original como fallback
    COALESCE(s.categoria, TRIM(v."GRUPO"))          AS "GRUPO_NORM",
    -- Línea del snapshot como referencia alternativa (puede diferir del campo LINEA)
    COALESCE(s.linea_snap, TRIM(v."LINEA"))         AS "LINEA_NORM",
    -- Flag para identificar si la referencia tuvo normalización
    CASE WHEN s.referencia_base IS NOT NULL THEN TRUE ELSE FALSE END AS "TIENE_NORM"
FROM ventas_raw v
LEFT JOIN snapshot s
    ON TRIM(v."REFERENCIA") = s.referencia_base
WITH DATA;

-- ---------------------------------------------------------------------------
-- PASO 6: Índices para las consultas más frecuentes
-- ---------------------------------------------------------------------------

-- Filtro principal: movimiento (VENTAS POS, CAMBIOS DE MERCANCIA ACLIENTE)
CREATE INDEX IF NOT EXISTS idx_vu_movimiento
    ON ventas_unificada (TRIM("DESC_MOVIMIENTO"));

-- Filtro por año (columna ya existente en las tablas origen)
CREATE INDEX IF NOT EXISTS idx_vu_anio
    ON ventas_unificada ("Año");

-- Filtro por departamento
CREATE INDEX IF NOT EXISTS idx_vu_departamento
    ON ventas_unificada (UPPER(TRIM("DEPARTAMENTO")));

-- Filtro/agrupación por GRUPO_NORM (el más importante para análisis)
CREATE INDEX IF NOT EXISTS idx_vu_grupo_norm
    ON ventas_unificada ("GRUPO_NORM");

-- Filtro por LINEA
CREATE INDEX IF NOT EXISTS idx_vu_linea
    ON ventas_unificada (TRIM("LINEA"));

-- Filtro por REFERENCIA
CREATE INDEX IF NOT EXISTS idx_vu_referencia
    ON ventas_unificada (TRIM("REFERENCIA"));

-- Índice por fecha: omitido — TO_DATE no es IMMUTABLE en PostgreSQL.
-- Los filtros temporales se benefician del índice idx_vu_mov_anio y de la
-- columna "Año" que ya está indexada. Si se necesita índice de fecha,
-- agregar una columna generada IMMUTABLE en las tablas origen.

-- Índice compuesto para el caso de uso más frecuente:
-- WHERE DESC_MOVIMIENTO = 'VENTAS POS' AND "Año" = 2026
CREATE INDEX IF NOT EXISTS idx_vu_mov_anio
    ON ventas_unificada (TRIM("DESC_MOVIMIENTO"), "Año");

-- ---------------------------------------------------------------------------
-- VERIFICACIÓN FINAL
-- ---------------------------------------------------------------------------
SELECT
    'ventas_unificada' AS vista,
    COUNT(*)           AS total_filas,
    COUNT(CASE WHEN "TIENE_NORM" THEN 1 END) AS filas_normalizadas,
    COUNT(CASE WHEN NOT "TIENE_NORM" THEN 1 END) AS filas_sin_norm,
    ROUND(COUNT(CASE WHEN "TIENE_NORM" THEN 1 END)::numeric / COUNT(*) * 100, 1) AS pct_cobertura
FROM ventas_unificada;
