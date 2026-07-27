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
  tabla_origen: ventas
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
2. DESC_MOVIMIENTO solo tiene 3 valores válidos: `'VENTAS POS'`, `'CAMBIOS DE MERCANCIA ACLIENTE'`, `'DEVOLUCIÓN AL PROVEEDOR'`.
3. Valor de venta: `"CANTIDAD" * "PVP"`. Nunca `"PVP LISTA"` para tiendas individuales.
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
| Tiendas con venta en el periodo | 45 | |
| Ticket promedio por transaccion | $15,000 | |
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

#### D.3 Desglose por zona (ZONA)
Solo si el usuario pide analisis por zona o si el informe es detallado.

| Zona | Unidades | Valor COP |
|---|---:|---:|

**Datos necesarios:** `GROUP BY UPPER(TRIM("DEPARTAMENTO"))`, `UPPER(TRIM("ZONA"))`.

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

| Referencia | Descripcion | Unidades | Valor COP |
|---|---|---:|---:|

#### I.1 Venta de la referencia TOP por zona

Tomar la referencia con mayor venta total y mostrar su distribucion geografica:

| Zona | Referencia | Unidades | Valor COP |
|---|---|---:|---:|

**Datos necesarios:** `GROUP BY UPPER(TRIM("REFERENCIA"))` + subconsulta por zona para la top.

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

### BLOQUE N — Anexo de datos completos

**Cuando usar:** cuando el usuario pide ver el listado completo.

Tabla completa sin limitar filas, al final del documento.
Indicar: "Datos extraidos de tabla `ventas` — CreytexToSQL."

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

---

## Ejemplo de lectura de intencion

| El usuario pide... | Bloques a usar |
|--------------------|---------------|
| "informe completo de ventas" | A + B + C + D + E + F + G + I + J + M |
| "informe de ventas de enero" | A + B + C + D + K + M |
| "reporte por tienda" | A + F + opcionalmente M |
| "como van las ventas por talla" | A + J |
| "resumen ejecutivo para gerencia" | A + B + C + M |
| "evolucion semana a semana" | A + K |
| "cuantas tiendas estan activas" | A + L |
| "analisis de referencias" | A + I (con subseccion I.1) |
| "informe detallado con todo" | A + B + C + D + E + F + G + H + I + J + K + L + M + N |
