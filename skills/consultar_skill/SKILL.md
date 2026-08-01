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

### 3. Columnas "Año" y "Mes" usan comillas dobles

Las columnas `"Año"` (con ñ) y `"Mes"` son precalculadas en la tabla (tipo BIGINT) para optimizar queries temporales. Ambas usan comillas dobles en PostgreSQL.

✅ Correcto: `SELECT "Año", "Mes" FROM ventas LIMIT 5;`
✅ Correcto: `SELECT * FROM ventas WHERE "Mes" = 5 AND "Año" = 2026;`
❌ Incorrecto: `SELECT Año, Mes FROM ventas LIMIT 5;` (sin comillas, da error de sintaxis)

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

##  Esquema de la tabla `ventas`

> **Fuente de verdad única:** el esquema completo (columnas, tipos, valores válidos por campo)
> está documentado en `agents/generador_consultas.md`, sección **"Esquema de la tabla `ventas`"**.
> No se duplica aquí para evitar desincronización. Consultarlo antes de generar cualquier query.
>
> Columnas clave a recordar:
> - `LINEA`: casing mixto exacto — **NO usar `UPPER()`**. Valores: `"10 - Dama Exterior"`, `"11 - Dama Deportivo"`, `"12 - Hombre Exterior"`, `"13 - Hombre Deportivo"`, `"14 - Junior Femenino"`, `"15 - Junior Masculino"`, `"16 - Bebita"`, `"17 - Bebito"`, `"19 - Primis Bebito"`, `"20 - Primis Bebita"`.
> - `FECHA_MVTO`: TEXT formato `D/M/YYYY`. Usar siempre `TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY')`.
> - `PVP HIST`, `PVP HIST LISTA`, `VENTA $ PVP HIST LISTA`, `FCH_ACT_PORTAFOLIO`, `FCH_ACT_SKU`, `LLAVE_DEP`: siempre nulas, no usar.
> - Valor de venta: `"CANTIDAD" * "PVP"`. Nunca `"PVP LISTA"` salvo macroclientes.

##  Notas importantes del negocio

> Las notas de negocio completas están en `agents/generador_consultas.md`, sección **"Notas de negocio"**.
> Regla crítica de precio: usar `"CANTIDAD" * "PVP"` para ventas. `"PVP LISTA"` solo para macroclientes.

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
| Utilizar `UPPER` al tratar la columna `LINEA`  | Esta columna no posee sus valores en mayuscula |
