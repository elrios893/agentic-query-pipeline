---
name: informe-ventas
description: >
  Genera informes de ventas para Creytex en formato .docx.
  Se activa con frases como "genera un informe", "reporte de ventas",
  "prepara un documento", "informe del periodo", "word", "docx".
  Usa la herramienta generar_docx para convertir el contenido a Word.
license: MIT
compatibility: opencode
activator:
  type: intent_match
  patterns:
    - "informe"
    - "reporte"
    - "report"
    - "documento.*ventas"
    - "resumen.*mensual"
    - "balance.*mes"
    - "word"
    - "docx"
    - "doc"
    - "prepara.*informe"
    - "genera.*informe"
    - "crea.*documento"
  auto_execute: true
  output_dir: reports/
metadata:
  cliente: Creytex
  tabla_origen: ventas_2025, ventas_2026
  herramienta_conversion: generar_docx
  carpeta_salida: reports/
---

# Skill: Informe de Ventas — Creytex

## Filosofia de uso

Esta skill NO es una plantilla fija. Es una **caja de herramientas**: bloques disponibles
que el agente selecciona, combina y ordena segun lo que el usuario pidio.

**Regla principal:** leer la intencion del usuario primero. Luego elegir solo los bloques
que respondan a esa intencion. Un informe puede tener 2 bloques o 9 — lo que el contexto
requiera, ni mas ni menos.

El orden de los bloques sigue el principio **macro → micro**: primero lo mas agregado
(resumen, geografico), luego lo mas granular (tienda, producto, talla).

---

## Reglas de negocio (siempre aplican)

1. Fuente de ventas: `TRIM("DESC_MOVIMIENTO") = 'VENTAS POS'`. El campo `SIGNO` NO es obligatorio filtrar.
2. **Tabla principal: `ventas_unificada`** — vista materializada que une `ventas_2025` y `ventas_2026`. Tiene la columna `"GRUPO_NORM"` (GRUPO normalizado). Filtrar por año con `WHERE "Año" = N`. Usar esta tabla por defecto en todos los bloques.
3. `ventas_2025` y `ventas_2026` solo usar si el informe explícitamente necesita el dato crudo sin normalizar.
4. **`"GRUPO_NORM"`**: columna normalizada de categoría de producto en `ventas_unificada`. SIEMPRE usar `"GRUPO_NORM"` en lugar de `"GRUPO"` cuando la tabla sea `ventas_unificada`.
5. Devoluciones de cliente: `TRIM("DESC_MOVIMIENTO") = 'CAMBIOS DE MERCANCIA ACLIENTE'` — único movimiento que representa devolución real del consumidor final (signo `+`, entrada al almacén). `'DEVOLUCION AL PROVEEDOR'` es distinto: es devolución hacia el proveedor, no del cliente.
6. Valor de venta: `"CANTIDAD" * "PVP"`. Nunca `"PVP LISTA"` para tiendas individuales.
4. `"PVP LISTA"` solo cuando la pregunta sea sobre macroclientes/cadenas.
5. Fechas: `TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY')`. Nunca `::DATE` ni `'DD/MM/YYYY'` sin FM.
6. Siempre `TRIM()` en `"SIGNO"`, `"DESC_MOVIMIENTO"`, `"DEPARTAMENTO"`, etc.
7. **Jerarquía de producto**: `LINEA` → `LINEA_DETLL` → `ESTILO_ITEM` → `GRUPO`. Usar el nivel que corresponda.
8. No usar: `PVP HIST`, `PVP HIST LISTA`, `VENTA $ PVP HIST LISTA`, `FCH_ACT_PORTAFOLIO`, `FCH_ACT_SKU`, `LLAVE_DEP`.
9. Nunca inventar cifras. Si no hay dato: escribir "Sin informacion".

---

## REGLAS DE FORMATO MARKDOWN — OBLIGATORIAS

**CRITICO:** El documento final se convierte a Word (.docx). El parser espera Markdown estandar.
Seguir estas reglas sin excepcion, independientemente del modelo LLM que este generando:

### Titulos (OBLIGATORIO)
- El titulo principal del documento: `# Titulo` — UN solo numeral, sin espacios extra.
- Secciones de primer nivel: `## Nombre de seccion` — DOS numerales.
- Subsecciones: `### Nombre` — TRES numerales.
- **PROHIBIDO** usar cuatro o mas numerales (`####`, `#####`).
- **PROHIBIDO** escribir el titulo de seccion como texto plano o con negrita en lugar de `##`.

Ejemplos correctos:
```
# Informe de Ventas — Mayo 2026
## 1. Resumen Ejecutivo
## 2. Geografico
### 2.1 Participacion por Departamento
## 3. Dependencias
```

Ejemplos INCORRECTOS (no usar):
```
### Resumen Ejecutivo       ← mal: tres numerales para seccion principal
**Resumen Ejecutivo**       ← mal: negrita en lugar de titulo
#### Departamentos          ← mal: cuatro numerales
Resumen Ejecutivo           ← mal: texto plano
```

### Tablas
- Siempre con encabezado y separador `|---|`.
- Alinear columnas de numeros a la derecha: `| ---: |`.

### Numeros
| Tipo | Formato | Ejemplo |
|------|---------|---------|
| Unidades | Entero con separador de miles | 8,420 |
| Valor COP | $ + separador de miles | $126,300,000 |
| Variacion | Signo + o - con 1 decimal | +10.1% / -22.0% |
| Porcentaje | 1 decimal + % | 42.0% |
| Fechas | DD/MM/YYYY | 15/01/2026 |

---

## Orden de bloques (macro → micro)

Usar solo los bloques relevantes para la intencion del usuario.
El orden sugerido es orientativo — no es obligatorio incluir todos:

```
A  → Encabezado / portada          (siempre)
B  → Resumen ejecutivo             (informes generales / gerencia)
C  → Metricas principales          (junto con B; incluir LINEA y REFERENCIA top aqui)
D  → Geografico: Departamento/Zona (cuando aplique)
E  → Dependencias (cadenas)        (cuando aplique)
F  → Tiendas                       (cuando aplique)
G  → Linea de producto             (cuando aplique)
H  → Producto (DESC_ITEM)          (cuando aplique)
I  → Referencia                    (cuando aplique)
J  → Talla                         (cuando aplique)
K  → Evolucion en el tiempo        (cuando aplique)
L  → Estado de tiendas/portafolio  (cuando aplique)
M  → Alertas y hallazgos           (al final, informes ejecutivos)
N  → Anexo de datos completos      (cuando el usuario pide detalle completo)
P  → Devoluciones y cambios        (cuando el usuario pide analisis de devoluciones o cambios)
Q  → Categorias y mix de producto  (cuando el usuario pide analisis por categoria/grupo)
R  → Distribucion de precios       (cuando el usuario pide analisis de precios o rangos)
```

---

## Bloques disponibles

---

### BLOQUE A — Encabezado del documento

**Cuando usar:** siempre.

```
# [Titulo descriptivo segun lo que pidio el usuario]
**Periodo:** [rango de fechas]
**Generado:** [fecha de hoy]
Creytex
```

---

### BLOQUE B — Resumen ejecutivo

**Cuando usar:** informes generales o para gerencia.

**Contenido:** 4 a 6 bullets con los hallazgos mas importantes.
Cada bullet debe ser una conclusion, no un dato crudo.

- Volumen total: unidades vendidas + valor COP.
- Elemento destacado positivo (zona, tienda, referencia con mejor desempeno).
- Elemento que requiere atencion (caida, inactividad, baja rotacion).
- Dato relevante adicional segun contexto.

---

### BLOQUE C — Metricas principales

**Cuando usar:** junto con el resumen ejecutivo en informes generales.

**Formato:** tabla de KPIs. Incluir aqui tambien la linea top y la referencia top del periodo.

| Metrica | Valor | Nota |
|---------|-------|------|
| Unidades vendidas totales | 8,420 | |
| Valor total de ventas | $126,300,000 | |
| Linea mas vendida | [LINEA] | [unidades o valor] |
| Referencia top | [REFERENCIA] | [unidades o valor] |
| Talla mas vendida | [TALLA] | [unidades] |

**Datos necesarios:** consultas de agregacion totales + top por LINEA, REFERENCIA, TALLA.

---

### BLOQUE D — Geografico: Departamento / Zona

**Cuando usar:** analisis por zona, departamento, ciudad o region.

**Subsecciones sugeridas:**

#### D.1 Ventas por departamento
Tabla ordenada mayor a menor:

| Departamento | Unidades | Valor COP | % del total |
|---|---:|---:|---:|

Insertar grafico `barras_horizontales` despues de la tabla.

#### D.2 Participacion por departamento
Insertar grafico de `torta` (solo si hay 5 o menos departamentos con participacion relevante;
si son mas, usar barras horizontales con % del total en la tabla).

#### D.3 Desglose por ciudad
**Siempre incluir** cuando el informe se filtra por un departamento especifico
(ej: "informe de Antioquia", "ventas en Valle del Cauca").
Si el informe es nacional/general, incluir solo las top 10 ciudades por valor.

| Ciudad | Departamento | Unidades | Valor COP |
|---|---|---:|---:|

**Datos necesarios:** `GROUP BY UPPER(TRIM("CIUDAD")), UPPER(TRIM("DEPARTAMENTO"))`.
Si hay filtro por departamento: agregar `WHERE UPPER(TRIM("DEPARTAMENTO")) = 'NOMBRE'`.

#### D.4 Desglose por zona (ZONA)
Solo si el usuario pide analisis por zona o si el informe es detallado.

| Zona | Unidades | Valor COP |
|---|---:|---:|

**Datos necesarios:** `GROUP BY UPPER(TRIM("DEPARTAMENTO"))`, `UPPER(TRIM("ZONA"))`, `UPPER(TRIM("CIUDAD"))`.

---

### BLOQUE E — Dependencias (cadenas)

**Cuando usar:** cuando el informe incluye analisis de cadenas/macro-clientes.

**Formato:** dos tablas lado a lado en el documento — top 5 mejores y top 5 peores.

**Top 5 cadenas con mayor venta:**

| Dependencia | Unidades | Valor COP |
|---|---:|---:|

**Top 5 cadenas con menor venta:**

| Dependencia | Unidades | Valor COP |
|---|---:|---:|

**Datos necesarios:** `GROUP BY UPPER(TRIM("DEPENDENCIA"))` ordenado DESC y ASC.

---

### BLOQUE F — Tiendas

**Cuando usar:** cuando el usuario pide ver resultados por punto de venta o tienda.
Mostrar las tiendas mas relevantes para el tipo de informe solicitado.

**Formato:**

| Tienda (DESC_DEPENDENCIA) | Dependencia | Ciudad | Unidades | Valor COP |
|---|---|---|---:|---:|

Indicar si alguna tienda aparece inactiva (`ESTADO_TIENDA`).

**Datos necesarios:** `GROUP BY UPPER(TRIM("DESC_DEPENDENCIA"))`.

---

### BLOQUE G — Linea de producto

**Cuando usar:** cuando el informe incluye analisis de lineas de producto (columna `LINEA`).

**Formato:**

| Linea | Unidades | Valor COP | % del total |
|---|---:|---:|---:|

Insertar grafico `barras_verticales` o `barras_horizontales` segun cantidad de lineas.

**Datos necesarios:** `GROUP BY UPPER(TRIM("LINEA"))`.

---

### BLOQUE H — Producto (DESC_ITEM)

**Cuando usar:** cuando el usuario pide analisis por producto o descripcion de item.

**Formato:**

| Descripcion (DESC_ITEM) | Referencia | Unidades | Valor COP |
|---|---|---:|---:|

Identificar siempre el producto de mayor rotacion y el de menor.

**Datos necesarios:** `GROUP BY UPPER(TRIM("DESC_ITEM")), UPPER(TRIM("REFERENCIA"))`.

---

### BLOQUE I — Referencia

**Cuando usar:** cuando el usuario pide analisis por referencia, o en informes detallados.

**Formato:**

| Referencia | Línea | Grupo | Unidades | Valor COP |
|---|---|---|---:|---:|

#### I.1 Venta de la referencia TOP por zona

Tomar la referencia con mayor venta total y mostrar su distribucion geografica:

| Zona | Referencia | Línea | Grupo | Unidades | Valor COP |
|---|---|---|---|---:|---:|

**Datos necesarios:** `GROUP BY TRIM("REFERENCIA"), TRIM("LINEA_NORM"), TRIM("GRUPO_NORM")` (usar las columnas normalizadas de `ventas_unificada`, nunca `LINEA`/`GRUPO` crudas) + subconsulta por zona para la top.

---

### BLOQUE J — Talla

**Cuando usar:** cuando el usuario pide analisis por talla, o en informes de producto completos.

El analisis de tallas tiene sentido porque Creytex vende en tallas S, M y XL.
Muestra la distribucion de ventas por talla para identificar cual talla domina
y si hay desbalance que sugiera ajustar el mix de produccion.

**Formato:**

| Talla | Unidades | % del total | Valor COP |
|---|---:|---:|---:|

Insertar grafico `barras_verticales` con distribucion de tallas.

Agregar conclusion: si una talla tiene menos del 15% de participacion, puede indicar
desbalance de inventario o baja demanda para ese segmento.

**Datos necesarios:** `GROUP BY UPPER(TRIM("TALLA"))`.

---

### BLOQUE K — Evolucion en el tiempo

**Cuando usar:** cuando el usuario pide tendencia, evolucion, comparacion por semana/mes/dia.

**Formato:** tabla cronologica.

| Fecha / Semana / Mes | Unidades | Valor COP | Variacion vs anterior |
|---|---:|---:|---:|

Insertar grafico `linea` con tendencia temporal.

**Regla de granularidad — CRITICA:** la tabla y el grafico DEBEN usar exactamente los mismos datos con la misma granularidad. No agregar "(Muestra)" ni subconjuntos distintos en la tabla versus el grafico.
- Si el periodo tiene ≤ 20 dias → mostrar todos los dias en la tabla Y en el grafico.
- Si el periodo tiene > 20 dias → agrupar por semana (o cada 15 dias) tanto en la tabla como en el grafico. Indicar en el titulo del bloque la granularidad usada, ej: "Evolucion Semanal".
- Nunca mostrar en la tabla una granularidad distinta a la del grafico. Ambos deben ser coherentes.

**Datos necesarios:** `GROUP BY TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY')` o por `"Mes"`.

---

### BLOQUE L — Estado de tiendas / portafolio

**Cuando usar:** cuando el usuario pide saber cuantas tiendas estan activas o el estado del portafolio.

- Tiendas activas con venta en el periodo.
- Tiendas sin venta (posiblemente inactivas).
- Referencias con `ESTADO_SKU_MOD = 'Inactivo'` o `ESTADO_LINEA` inactivo.

---

### BLOQUE M — Alertas y hallazgos

**Cuando usar:** informes para toma de decisiones. Siempre al final del informe.

Lista numerada. Solo incluir alertas respaldadas por datos:

1. Zona o tienda con caida > 15% vs periodo anterior.
2. Referencia con rotacion muy baja (menos del 50% del promedio de su grupo).
3. Tiendas inactivas que tenian venta en el periodo previo.
4. Talla con baja rotacion implicita.
5. Cualquier anomalia que los datos confirmen.

Si no hay alertas con datos: "No se detectaron alertas en el periodo."

---

### BLOQUE O — Tablas Interactivas

**Cuando usar:** cuando el usuario pide explícitamente "tabla", "compara", "ranking", o solicita ver datos en formato tabular para análisis detallado.

**Formato:** tabla markdown con alineación de números a la derecha y formato de moneda.

**Tres subtipos de tabla:**

#### O.1 Tabla simple (ranking o desglose)
Mostrar una dimensión con métricas ordenadas. Máximo 20 filas; si hay más, indicar que hay más resultados.

| Departamento | Unidades | Valor COP |
|---|---:|---:|
| ANTIOQUIA | 8,420 | $126,300,000 |
| BOGOTA | 5,200 | $78,000,000 |

**Datos necesarios:** `GROUP BY columna, ORDER BY metrica DESC`

#### O.2 Tabla comparativa (períodos)
Dos o más períodos lado a lado (ej: enero | febrero). Incluir columna de variación %.

| Día | Ventas Enero | Ventas Febrero | Variación |
|---:|---:|---:|---:|
| 1 | $100,000 | $120,000 | +20.0% |
| 2 | $95,000 | $110,000 | +15.8% |

**Datos necesarios:** `CASE WHEN para cada período, GROUP BY día`

#### O.3 Tabla de ranking
Con número de posición (1, 2, 3...) e incluir % del total como contexto.

| Ranking | Referencia | Unidades | Valor COP | % Total |
|---:|---|---:|---:|---:|
| 1 | REF-001 | 2,450 | $36,750,000 | 12.5% |
| 2 | REF-002 | 1,890 | $28,350,000 | 9.6% |

**Formato Markdown obligatorio:**
- Alinear números a la derecha: `| ---: |`
- Unidades con separador de miles: `8,420`
- Moneda con $ y separador de miles: `$126,300,000`
- Porcentajes: `12.5%` (1 decimal)
- Fechas: `15/01/2026`

**Expansión de tabla:**
- Si resultado tiene ≤ 20 filas → mostrar todas en la tabla
- Si resultado tiene > 20 filas → mostrar top 20 y agregar nota: "(Mostrando top 20 de X resultados)"

---

### BLOQUE N — Anexo de datos completos

**Cuando usar:** cuando el usuario pide ver el listado completo.

Tabla completa sin limitar filas, al final del documento.
Indicar: "Datos extraidos de tablas `ventas_2025` / `ventas_2026` — CreytexToSQL."

---

### BLOQUE P — Devoluciones y cambios

**Cuando usar:** cuando este el bloque I, devoluciones, cambios, retornos, o cuando el bloque M detecta una tasa de cambios inusualmente alta en algún segmento.

**Movimiento fuente:** `TRIM("DESC_MOVIMIENTO") = 'CAMBIOS DE MERCANCIA ACLIENTE'` — único movimiento que representa devolución real del consumidor final (signo `+`, entrada al almacén). NO usar `DEVOLUCION AL PROVEEDOR`.

**Tasa de devolución:** `cambios / ventas_pos * 100`. Umbral de alerta: tasa > 5% en un grupo o referencia merece mención explícita.

#### P.1 Resumen global de cambios

| Métrica | Valor |
|---|---:|
| Total cambios (unidades) | |
| Total ventas POS (unidades) | |
| Tasa global de cambios | |

**Datos necesarios:**
```sql
SELECT
    SUM(CASE WHEN TRIM("DESC_MOVIMIENTO") = 'VENTAS POS' THEN "CANTIDAD" ELSE 0 END) AS ventas,
    SUM(CASE WHEN TRIM("DESC_MOVIMIENTO") = 'CAMBIOS DE MERCANCIA ACLIENTE' THEN ABS("CANTIDAD") ELSE 0 END) AS cambios
FROM ventas_2026
WHERE TRIM("DESC_MOVIMIENTO") IN ('VENTAS POS', 'CAMBIOS DE MERCANCIA ACLIENTE');
```

#### P.2 Tasa de cambios por GRUPO

Ordenar por tasa descendente. Alertar si tasa > 5%.

| Grupo | Ventas | Cambios | Tasa % | Alerta |
|---|---:|---:|---:|---|

**Datos necesarios:**
```sql
SELECT
    TRIM("GRUPO") AS grupo,
    SUM(CASE WHEN TRIM("DESC_MOVIMIENTO") = 'VENTAS POS' THEN "CANTIDAD" ELSE 0 END) AS ventas,
    SUM(CASE WHEN TRIM("DESC_MOVIMIENTO") = 'CAMBIOS DE MERCANCIA ACLIENTE' THEN ABS("CANTIDAD") ELSE 0 END) AS cambios
FROM ventas_2026
WHERE TRIM("DESC_MOVIMIENTO") IN ('VENTAS POS', 'CAMBIOS DE MERCANCIA ACLIENTE')
GROUP BY 1
HAVING SUM(CASE WHEN TRIM("DESC_MOVIMIENTO") = 'CAMBIOS DE MERCANCIA ACLIENTE' THEN ABS("CANTIDAD") ELSE 0 END) > 0
ORDER BY cambios DESC;
```

#### P.3 Tasa de cambios por COLOR

Identifica colores con alta tasa — puede indicar diferencia entre foto/realidad del producto.

| Color | Ventas | Cambios | Tasa % |
|---|---:|---:|---:|

**Datos necesarios:** misma query de P.2 cambiando `GROUP BY TRIM("GRUPO")` por `GROUP BY TRIM("COLOR")`.

#### P.4 Tasa de cambios por TALLA

Tallas extremas (XS, XXL) o mal representadas en inventario suelen tener más cambios.

| Talla | Ventas | Cambios | Tasa % |
|---|---:|---:|---:|

**Datos necesarios:** `GROUP BY TRIM("TALLA")`.

#### P.5 Tasa de cambios por rango de precio

Prendas más caras tienen más fricción de compra y potencialmente más cambios.

| Rango PVP | Ventas | Cambios | Tasa % |
|---|---:|---:|---:|

**Datos necesarios:**
```sql
SELECT
    CASE WHEN "PVP" < 50000 THEN '<50k'
         WHEN "PVP" < 100000 THEN '50k-100k'
         WHEN "PVP" < 150000 THEN '100k-150k'
         ELSE '>150k' END AS rango_precio,
    SUM(CASE WHEN TRIM("DESC_MOVIMIENTO") = 'VENTAS POS' THEN "CANTIDAD" ELSE 0 END) AS ventas,
    SUM(CASE WHEN TRIM("DESC_MOVIMIENTO") = 'CAMBIOS DE MERCANCIA ACLIENTE' THEN ABS("CANTIDAD") ELSE 0 END) AS cambios
FROM ventas_2026
WHERE TRIM("DESC_MOVIMIENTO") IN ('VENTAS POS', 'CAMBIOS DE MERCANCIA ACLIENTE')
GROUP BY 1 ORDER BY MIN("PVP");
```

#### P.6 Tiendas con mayor tasa de cambios

Tasa alta en una tienda puede revelar problemas de asesoría al cliente o de calidad del stock local.

| Tienda | Departamento | Ventas | Cambios | Tasa % |
|---|---|---:|---:|---:|

**Datos necesarios:** `GROUP BY TRIM("DESC_DEPENDENCIA"), TRIM("DEPARTAMENTO")`.

**Grafico sugerido:** `barras_horizontales` con tasa % por grupo (bloque P.2), top 10.

---

### BLOQUE Q — Categorías y mix de producto

**Cuando usar:** cuando el usuario pide análisis por categoría, grupo de producto, o composición del portafolio. También relevante en informes generales para mostrar qué categorías impulsan las ventas.

**Jerarquía de producto disponible:**
- `LINEA` — nivel alto (ej: `10 - Dama Exterior`, `11 - Dama Deportivo`, `13 - Hombre Deportivo`)
- `GRUPO` — nivel de tipo de prenda dentro de la línea (ej: `02 - Camiseta manga corta`, `40 - Pantalones`)
- `PERFIL_PRENDA` — clasificación física (Superior, Inferior, Conjunto, Enterizo)
- `ESTILO_ITEM` — estilo específico (Camiseta, Pantalones, Blusa, Vestido...)

#### Q.1 Mix por LINEA

Participación de cada línea en el total del período.

| Línea | Unidades | Valor COP | % del total |
|---|---:|---:|---:|

Insertar gráfico `barras_horizontales` o `torta` (si ≤5 líneas con participación relevante).

**Datos necesarios:** `GROUP BY TRIM("LINEA") ORDER BY ventas DESC`.

#### Q.2 Mix por GRUPO dentro de cada LINEA

Para cada línea relevante, mostrar sus grupos con participación.

| Línea | Grupo | Unidades | % dentro de la línea |
|---|---|---:|---:|

**Datos necesarios:**
```sql
SELECT
    TRIM("LINEA") AS linea,
    TRIM("GRUPO") AS grupo,
    SUM("CANTIDAD") AS unidades,
    ROUND(CAST(SUM("CANTIDAD") * 100.0 /
        NULLIF(SUM(SUM("CANTIDAD")) OVER (PARTITION BY TRIM("LINEA")), 0) AS numeric), 1) AS pct_linea
FROM ventas_2026
WHERE TRIM("DESC_MOVIMIENTO") = 'VENTAS POS'
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
```

#### Q.3 Concentración de categorías

Si los 3 grupos más vendidos concentran más del 70% del total, la línea es vulnerable a quiebres de stock de esos grupos específicos.

| Top 3 grupos | Unidades acumuladas | % del total |
|---|---:|---:|

#### Q.4 Grupos con crecimiento y grupos en declive (requiere dos períodos)

Solo incluir si hay datos de período anterior disponibles.

| Grupo | Período anterior | Período actual | Variación % |
|---|---:|---:|---:|

**Datos necesarios:** query con CASE WHEN para ambos períodos, `GROUP BY TRIM("GRUPO")`.

**Grafico sugerido:** `barras_agrupadas` para Q.4 (comparación de períodos), `barras_horizontales` para Q.1.

---

### BLOQUE R — Distribución de precios

**Cuando usar:** cuando el usuario pide análisis de precios, rangos de precio, o en informes ejecutivos donde el mix de precio es relevante para entender causas de variaciones en valor de ventas.

**Contexto de datos:** PVP va de $12,900 a $169,990. El 80% de las ventas ocurre entre $50k y $150k. `PVP HIST` no está disponible. `PVP LISTA` es el precio de macroclientes — no usar para análisis de precio de tienda.

#### R.1 Distribución de ventas por rango de precio

| Rango PVP | Unidades | % unidades | Valor COP | % valor |
|---|---:|---:|---:|---:|
| < $50,000 | | | | |
| $50,000 – $100,000 | | | | |
| $100,000 – $150,000 | | | | |
| > $150,000 | | | | |

Insertar gráfico `barras_verticales` con distribución de unidades por rango.

**Datos necesarios:**
```sql
SELECT
    CASE WHEN "PVP" < 50000  THEN '< 50k'
         WHEN "PVP" < 100000 THEN '50k-100k'
         WHEN "PVP" < 150000 THEN '100k-150k'
         ELSE '> 150k' END AS rango_precio,
    SUM("CANTIDAD") AS unidades,
    ROUND(CAST(SUM("CANTIDAD") * 100.0 / NULLIF(SUM(SUM("CANTIDAD")) OVER (), 0) AS numeric), 1) AS pct_uds,
    SUM("CANTIDAD" * "PVP") AS valor_cop
FROM ventas_2026
WHERE TRIM("DESC_MOVIMIENTO") = 'VENTAS POS'
GROUP BY 1 ORDER BY MIN("PVP");
```

#### R.2 Precio promedio ponderado por LINEA

Detecta si una línea sube o baja de precio promedio entre períodos — síntoma de cambios de mix o de descuentos.

| Línea | Unidades | PVP promedio ponderado | Valor COP |
|---|---:|---:|---:|

**Datos necesarios:**
```sql
SELECT
    TRIM("LINEA") AS linea,
    SUM("CANTIDAD") AS unidades,
    ROUND(CAST(SUM("CANTIDAD" * "PVP") / NULLIF(SUM("CANTIDAD"), 0) AS numeric), 0) AS pvp_promedio_ponderado,
    SUM("CANTIDAD" * "PVP") AS valor_cop
FROM ventas_2026
WHERE TRIM("DESC_MOVIMIENTO") = 'VENTAS POS'
GROUP BY 1 ORDER BY 4 DESC;
```

#### R.3 Relación precio-devolución

¿Las prendas más caras tienen más cambios? Cruza rangos de precio con tasa de cambios.

| Rango PVP | Ventas | Cambios | Tasa % |
|---|---:|---:|---:|

**Datos necesarios:** misma estructura que P.5 — incluir aquí si ya existe el bloque P, o calcular nuevamente.

#### R.4 Evolución del precio promedio ponderado en el tiempo (requiere período)

Si en el período analizado el precio promedio ponderado cae, puede ser síntoma de descuentos, mayor proporción de prendas baratas, o cambio de mix de producto.

| Mes / Semana | Unidades | PVP promedio ponderado | Variación vs anterior |
|---|---:|---:|---:|

**Datos necesarios:** `GROUP BY "Mes"` o `GROUP BY TO_DATE("FECHA_MVTO", 'FMDD/FMMM/YYYY')` con cálculo de pvp ponderado.

**Grafico sugerido:** `barras_verticales` para R.1 (distribución), `linea` para R.4 (evolución del precio promedio).

---

## Graficos de apoyo

Para los bloques D, E, F, G, H, I, J, K el pipeline puede generar graficos automaticamente.

### Reglas de insercion

1. El grafico se genera antes de redactar el informe.
2. Insertar la imagen markdown exactamente como viene en los datos:
   `![Titulo](ruta/real/del/archivo.png)` — no inventar ni modificar la ruta.
3. Si la generacion falla: continuar sin imagen y escribir "No fue posible generar el grafico."

### Grafico sugerido por bloque

| Bloque | Grafico sugerido | Ubicacion |
|--------|-----------------|-----------|
| D — Departamentos | `barras_horizontales` | Despues de tabla |
| D — Participacion | `torta` (<=5 items) | Despues de tabla |
| E — Dependencias | `barras_horizontales` | Despues de tabla top 5 |
| F — Tiendas | `barras_horizontales` | Despues de tabla |
| G — Linea | `barras_verticales` o `barras_horizontales` | Despues de tabla |
| J — Talla | `barras_verticales` | Despues de tabla |
| K — Evolucion | `linea` | Despues de tabla |
| P — Devoluciones | `barras_horizontales` (tasa % por grupo) | Despues de tabla P.2 |
| Q — Categorias | `barras_horizontales` (mix linea) o `barras_agrupadas` (comparacion) | Despues de tabla Q.1 o Q.4 |
| R — Precios | `barras_verticales` (distribucion) o `linea` (evolucion precio promedio) | Despues de tabla R.1 o R.4 |

---

## Ejemplo de lectura de intencion

| El usuario pide... | Bloques a usar |
|--------------------|---------------|
| "informe completo de ventas" | A + B + C + D + E + F + G + H + I + J + K + M + P + Q + R |
| "informe de ventas de enero" | A + B + C + D + K + M |
| "reporte por tienda" | A + F + opcionalmente M |
| "como van las ventas por talla" | A + J |
| "resumen ejecutivo para gerencia" | A + B + C + M |
| "evolucion semana a semana" | A + K |
| "cuantas tiendas estan activas" | A + L |
| "analisis de referencias" | A + I (con subseccion I.1) |
| "informe detallado con todo" | A + B + C + D + E + F + G + H + I + J + K + L + M + N |
| "analisis de devoluciones" | A + P + opcionalmente M |
| "cuantos cambios hubo en caballero" | A + P (filtrado por LINEA) |
| "analisis por categoria o grupo" | A + Q + opcionalmente G |
| "como se distribuyen las categorias" | A + Q |
| "analisis de precios" | A + R |
| "como va el precio promedio" | A + R.2 + R.4 |
| "informe completo con devoluciones y precios" | A + B + C + D + G + J + K + P + Q + R + M |
