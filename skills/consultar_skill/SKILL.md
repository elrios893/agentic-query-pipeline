---
name: consultar-skill
description: >
  Skill para formular y ejecutar consultas SQL en PostgreSQL sobre la base de datos CreytexToSQL (tabla ventas).
  Proporciona reglas, restricciones y el contexto completo del esquema para que un agente genere queries precisas.
license: MIT
compatibility: opencode
metadata:
  database: CreytexToSQL
  tabla_principal: ventas
  total_filas: 263449
  motor: PostgreSQL
---

# Skill: Consultas PostgreSQL — CreytexToSQL

##  Objetivo

Ejecutar consultas SQL **solo de lectura (READ-ONLY)** sobre la tabla `ventas` en la base de datos PostgreSQL `CreytexToSQL`. Este skill está diseñado para que un agente IA pueda generar queries precisas sin errores de sintaxis ni lógica.

## ⛔ Restricción estricta: SOLO LECTURA

**Bajo ninguna circunstancia** el agente debe ejecutar sentencias que modifiquen la base de datos. Esto incluye, pero no se limita a:

| Operación | Prohibido |
|-----------|-----------|
| `INSERT` | ❌ Insertar filas |
| `UPDATE` | ❌ Modificar datos existentes |
| `DELETE` | ❌ Eliminar filas |
| `DROP` | ❌ Borrar tablas u objetos |
| `TRUNCATE` | ❌ Vaciar tablas |
| `ALTER` | ❌ Modificar estructura |
| `CREATE` | ❌ Crear tablas, índices u objetos |
| `GRANT` / `REVOKE` | ❌ Cambiar permisos |

El agente **solamente** puede ejecutar sentencias `SELECT` (y eventualmente `EXPLAIN ANALYZE` para diagnóstico, sin modificación).

Si el usuario pide explícitamente una modificación, el agente debe **rechazar cortésmente** indicando que este skill es de solo lectura y sugerir usar otra herramienta.

##  Conexión a la base de datos

| Parámetro | Valor |
|-----------|-------|
| Motor | PostgreSQL |
| Host | localhost |
| Puerto | 5432 |
| Base de datos | `CreytexToSQL` |
| Usuario | `postgres` |
| Contraseña | `root` |
| Tabla | `ventas` |

Usa `psycopg2` para conectarte desde Python:

```python
import psycopg2
conn = psycopg2.connect(
    host='localhost',
    port=5432,
    dbname='CreytexToSQL',
    user='postgres',
    password='root'
)
cur = conn.cursor()
```

##  Reglas obligatorias para generar consultas

### 1. Limitar segun el contexto

Toda consulta `SELECT` debe incluir `LIMIT N` (salvo que sea una agregacion con pocos grupos o que el usuario pida explicitamente todos los registros).

| Contexto | LIMIT sugerido |
|----------|---------------|
| Consulta puntual (top 5, un dia, busqueda especifica) | `LIMIT 20` |
| Periodo completo (un mes, varios meses, tendencias) | `LIMIT 1000` |
| GROUP BY con muchas categorias (tallas, tiendas, SKUs) | `LIMIT 200` |
| COUNT(*) o agregaciones con pocos grupos | Sin LIMIT |

✅ Correcto: `SELECT * FROM ventas LIMIT 20;`
✅ Correcto: `SELECT "DEPARTAMENTO", SUM("CANTIDAD") FROM ventas GROUP BY "DEPARTAMENTO";` (agregacion con pocos grupos)
❌ Incorrecto: `SELECT * FROM ventas;` (sin LIMIT, devuelve ~265K filas)

### 2. Columnas con espacios requieren comillas dobles

Los nombres de columna que contienen espacios o caracteres especiales deben encerrarse con `"` (comillas dobles).

✅ Correcto: `SELECT "PVP LISTA", "VENTA $ PVP LISTA" FROM ventas LIMIT 5;`
❌ Incorrecto: `SELECT PVP LISTA, VENTA $ PVP LISTA FROM ventas LIMIT 5;`

### 3. Columna "Año" usa comillas dobles

La columna `Año` tiene la letra `ñ`, por lo que siempre debe ir entre comillas dobles.

✅ Correcto: `SELECT "Año", Mes FROM ventas LIMIT 5;`
❌ Incorrecto: `SELECT Año, Mes FROM ventas LIMIT 5;`

### 4. Usar `COUNT(*)` para conteos

Para contar registros usa `COUNT(*)`. Si necesitas valores únicos usa `COUNT(DISTINCT columna)`.

✅ Correcto: `SELECT COUNT(*) FROM ventas;`
✅ Correcto: `SELECT COUNT(DISTINCT "DESC_MOVIMIENTO") FROM ventas;`

### 5. Fechas: FECHA_MVTO está como TEXT en D/M/YYYY sin ceros

La columna `FECHA_MVTO` es de tipo `text` en formato `D/M/YYYY` **sin ceros a la izquierda** (ej: `1/7/2026`, `10/3/2026`). Usa `TO_DATE()` con el modificador `FM` que ignora ceros opcionales:

```sql
-- Filtrar por rango de fechas
SELECT * FROM ventas
WHERE TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY') BETWEEN '2026-01-01' AND '2026-01-15'
LIMIT 20;

-- Extraer mes o año
SELECT EXTRACT(MONTH FROM TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY')) AS mes, COUNT(*)
FROM ventas
GROUP BY mes;
```

⚠️ **CRITICO**: 
- `"FECHA_MVTO"::DATE` FALLA — el texto está en D/M/YYYY, no en YYYY-MM-DD.
- `TO_DATE("FECHA_MVTO", 'DD/MM/YYYY')` FALLA con fechas de un dígito como `1/7/2026` — produce resultados incorrectos (año 2024 en lugar de 2026).
- **CORRECTO**: `TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY')`

### 6. Filtrar por tipo de movimiento

Usa `DESC_MOVIMIENTO` para filtrar por tipo de operación. Valores posibles:

| Valor | Significado |
|-------|-------------|
| `VENTAS POS` | Ventas en punto de venta |
| `CAMBIOS DE MERCANCIA ACLIENTE` | Cambios o devoluciones de clientes |
| `DEVOLUCIÓN AL PROVEEDOR` | Devoluciones a proveedor |

✅ Correcto: `SELECT * FROM ventas WHERE "DESC_MOVIMIENTO" = 'VENTAS POS' LIMIT 20;`

### 7. SIGNO indica dirección del inventario

| Valor | Significado |
|-------|-------------|
| `-` | Sale de inventario (venta, consumo) |
| `+` | Entra a inventario (recibo, cambio) |

### 8. Precios en COP (pesos colombianos)

Las columnas de precio están en pesos colombianos sin decimales. Usa `AVG()`, `SUM()`, etc. con precaución de no dividir por cantidad si es unitario.

### 9. Textos normalizados a mayusculas

`DEPARTAMENTO`, `CIUDAD`, `DESC_DEPENDENCIA`, `RAZON_SOCIAL`, `CLIMA`, `ZONA`, `ZONA_EX`, `DESC_ITEM` pueden tener casing inconsistente. Siempre usar `UPPER(TRIM(columna))`:

✅ `UPPER(TRIM("DEPARTAMENTO")) AS "DEPARTAMENTO"`
❌ `TRIM("DEPARTAMENTO") AS "Departamento"`

### 10. JOINs con otras tablas

Si en el futuro existen otras tablas, usa `LLAVE_DEP2`, `COD_SIESA`, `LLAVE_NAVAL`, `REFERENCIA` o `PLU` como posibles llaves de unión. Pregunta al usuario antes de asumir una relación.

### 10. Valores nulos

Las columnas `PVP HIST`, `PVP HIST LISTA`, `VENTA $ PVP HIST LISTA`, `FCH_ACT_PORTAFOLIO`, `FCH_ACT_SKU` y `LLAVE_DEP` pueden ser completamente nulas. No las uses sin verificar antes.

##  Esquema completo de la tabla `ventas`

| Columna | Tipo PostgreSQL | Descripción |
|---------|----------------|-------------|
| `ORIGEN` | TEXT | Nombre del archivo origen |
| `COD_DEPENDENCIA` | DOUBLE PRECISION | Código de dependencia |
| `DEP_DESTINO` | DOUBLE PRECISION | Dependencia destino |
| `DESC_DEP_DESTINO` | TEXT | Descripción dependencia destino |
| `PLU` | DOUBLE PRECISION | Identificador interno del SKU |
| `EAN` | DOUBLE PRECISION | Código de barras del SKU |
| `FECHA_MVTO` | TEXT | Fecha del movimiento (formato dd/mm/aaaa) |
| `DESC_MOVIMIENTO` | TEXT | Tipo de movimiento |
| `SIGNO` | TEXT | Signo del movimiento (+/-) |
| `CANTIDAD` | BIGINT | Unidades movidas |
| `FECHA_PROD` | TEXT | Fecha de producción |
| `REPROCESO_VTAS` | TEXT | Reproceso de ventas |
| `DEPENDENCIA` | TEXT | Macro cliente: AGAVAL, EXITO, JUMBO, LA MONTAÑA, NADELCO, OLIMPICA |
| `COD_BODEGA` | DOUBLE PRECISION | Código de bodega |
| `RAZON_SOCIAL` | TEXT | Razón social del cliente |
| `TIPO_DEPENDENCIA` | TEXT | Tipo de dependencia |
| `GTIN_ALMACEN` | DOUBLE PRECISION | Código de ubicación de tienda |
| `COD_SIESA` | DOUBLE PRECISION | Código de tienda en ERP |
| `DESC_DEPENDENCIA` | TEXT | Nombre de la tienda |
| `CLIMA` | TEXT | CALIDO, FRIO o TEMPLADO |
| `DEPARTAMENTO` | TEXT | Departamento geográfico |
| `CIUDAD` | TEXT | Ciudad |
| `ZONA` | TEXT | Zona geográfica |
| `ZONA_EX` | TEXT | Zona Éxito (solo este cliente) |
| `LLAVE_DEP2` | DOUBLE PRECISION | Concatena bodega + ubicación dependencia |
| `ESTADO_TIENDA` | TEXT | Estado de la tienda (Activa/Inactiva) |
| `LLAVE_DEP` | TEXT | Llave de dependencia (100% nula) |
| `REFERENCIA` | TEXT | Referencia interna de la prenda |
| `DESC_ITEM` | TEXT | Descripción de la prenda |
| `COD_COLOR` | DOUBLE PRECISION | Código numérico del color |
| `COLOR` | TEXT | Nombre del color |
| `TALLA` | TEXT | Talla de la prenda |
| `LINEA_GEN` | TEXT | Género: A (Bebes), J (Junior), M (Hombres), U (Unisex), W (Mujer) |
| `LINEA_DETLL` | TEXT | Categoría: A (Bebes), B (Beachwear), E (Exterior), J (Junior), L (Leasurewear), P (Performance) |
| `ESTILO_ITEM` | TEXT | Macrocategoria: 01 (Top), 02 (Camiseta), 04 (Blusa), 05 (Camisa), 07 (Chaqueta), 08 (Buzo), 09 (Vestido), 10 (Enterizo), 14 (Pantalones), 17 (Jogger), 20 (Falda), 22 (Conjunto), 24 (Gorra), 27 (Bolso) |
| `GRUPO` | TEXT | Estilo específico dentro de la macrocategoria (manga corta/larga, larga/corta) |
| `LINEA` | TEXT | Línea de la prenda |
| `MARCA` | TEXT | 0002 (Baby Planet), 0012 (Bata), 0018 (Amazon Mint), 8888 (Na), B (Belife) |
| `TIPO_DE_NEGOCIO` | TEXT | Origen de venta: 0001 (Marca propia), 0003 (Paquete completo nacional), 0004 (Paquete completo exportacion) |
| `CUENTO` | TEXT | Colección de la prenda |
| `TIPO_PORTAFOLIO_MOD` | TEXT | Tipo de portafolio (Línea) |
| `FCH_ACT_PORTAFOLIO` | TEXT | Fecha activación portafolio (casi siempre nula) |
| `ESTADO_SKU_MOD` | TEXT | Estado del SKU (Activo/Inactivo) |
| `FCH_ACT_SKU` | TEXT | Fecha cambio estado SKU (casi siempre nula) |
| `PERFIL_PRENDA` | TEXT | Perfil de la prenda (Inferior/Superior) |
| `CAMBIO_PORTAFOLIO?` | DOUBLE PRECISION | Indicador de cambio de portafolio |
| `PVP` | DOUBLE PRECISION | Precio venta al consumidor final |
| `PVP LISTA` | DOUBLE PRECISION | Precio venta al cliente macro |
| `PVP HIST` | DOUBLE PRECISION | PVP histórico (siempre nulo) |
| `PVP HIST LISTA` | DOUBLE PRECISION | PVP lista histórico (siempre nulo) |
| `VENTA $ PVP LISTA` | DOUBLE PRECISION | Venta en $ a precio de lista |
| `VENTA $ PVP HIST LISTA` | DOUBLE PRECISION | Venta histórica (siempre nulo) |
| `DESC_GRUPO` | TEXT | Descripción del grupo |
| `MODELO` | TEXT | Modelo (Linea = colección permanente, Moda = temporada) |
| `LINEA_MY` | TEXT | Línea MY |
| `LLAVE_NAVAL` | TEXT | Concatena COD_BODEGA + DEPENDENCIA + LINEA_MY |
| `ESTADO_LINEA` | TEXT | Estado de la prenda en la tienda (Activa/Inactiva) |
| `Año` | BIGINT | Año del movimiento |
| `Mes` | BIGINT | Mes del movimiento |
| `TIPO_PORTAFOLIO_MOD_2` | TEXT | Tipo de portafolio (Linea, Moda) |

##  Notas importantes del negocio

- El cliente entrega datos **hasta la columna REPROCESO_VTAS**. Las columnas restantes son combinaciones internas.
- **Jerarquía de producto**: `LINEA` → `LINEA_DETLL` (performance/exterior/junior) → `ESTILO_ITEM` (macrocategoria: camisa, falda, pantaloneta) → `GRUPO` (estilo específico: manga corta, larga). Elegir el nivel según la granularidad que pida el usuario.
- `FCH_ACT_PORTAFOLIO` registra el momento en que una prenda pasa de colección a línea. Aparece una única vez y cambia `ESTADO_SKU_MOD` a "Activo".
- `PVP HIST` y `PVP HIST LISTA` **no deben usarse** (siempre nulos).
- `MODELO` puede ser `Linea` (prendas que gustaron y se venden siempre) o `Moda` (prendas de temporada).
- **CRÍTICO — Columna de precio para ventas**: Para calcular valor de ventas SIEMPRE usa `"CANTIDAD" * "PVP"`. `PVP` es el precio que paga el consumidor final. NO uses `"PVP LISTA"` para ventas de tiendas individuales.
- **`PVP LISTA` solo para clientes MACRO**: Usa `"PVP LISTA"` únicamente cuando la pregunta sea sobre cadenas o macroclientes (ej: "ventas totales a Éxito"), no para tiendas individuales.

##  Ejemplos de consultas útiles

```sql
-- Top 10 tiendas con más ventas
SELECT "DESC_DEPENDENCIA", SUM("CANTIDAD") AS total_unidades
FROM ventas
WHERE "DESC_MOVIMIENTO" = 'VENTAS POS'
GROUP BY "DESC_DEPENDENCIA"
ORDER BY total_unidades DESC
LIMIT 10;

-- Ventas por departamento
SELECT "DEPARTAMENTO", COUNT(*) AS transacciones, SUM("CANTIDAD") AS unidades
FROM ventas
WHERE "DESC_MOVIMIENTO" = 'VENTAS POS'
GROUP BY "DEPARTAMENTO"
ORDER BY unidades DESC;

-- Distribución de tallas vendidas
SELECT "TALLA", COUNT(*) AS cantidad
FROM ventas
WHERE "DESC_MOVIMIENTO" = 'VENTAS POS'
GROUP BY "TALLA"
ORDER BY cantidad DESC;

-- Ingresos totales por tienda (PVP * CANTIDAD)
SELECT "DESC_DEPENDENCIA",
       SUM("CANTIDAD" * "PVP") AS ingresos_totales
FROM ventas
WHERE "DESC_MOVIMIENTO" = 'VENTAS POS'
GROUP BY "DESC_DEPENDENCIA"
ORDER BY ingresos_totales DESC
LIMIT 10;

-- Productos más movidos (entradas + salidas)
SELECT "DESC_ITEM", "TALLA", "COLOR", SUM("CANTIDAD") AS total_movido
FROM ventas
GROUP BY "DESC_ITEM", "TALLA", "COLOR"
ORDER BY total_movido DESC
LIMIT 10;
```

##  Errores comunes que debes evitar

| Error | Explicación |
|-------|-------------|
| `SELECT * FROM ventas` sin `LIMIT` | Puede devolver 263K filas y colapsar la conexión |
| Usar `Año` sin comillas | PostgreSQL interpretará `A` como alias y `ño` como error de sintaxis |
| `FECHA_MVTO = '2026-01-01'` sin castear | Falla porque es TEXT, no DATE. Usa `TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY') = '2026-01-01'` |
| `"FECHA_MVTO"::DATE` | FALLA — el formato es D/M/YYYY sin ceros. Usa `TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY')` |
| `TO_DATE("FECHA_MVTO", 'DD/MM/YYYY')` | FALLA con fechas de un digito (1/7/2026 → año 2024 erroneo). Siempre usar `FMDD/FMMM/YYYY` |
| `ORDER BY Ventas DESC` (alias con mayuscula sin comillas) | PostgreSQL dobla `ventas` a minusculas y no encuentra el alias. Usa `ORDER BY "Ventas" DESC` |
| `GROUP BY 1` con columnas con espacios | Falla porque los alias con espacios no se resuelven. Usa nombres completos |
| Olvidar el `;` al final | PostgreSQL no ejecutará la consulta |
