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

Esta skill NO es una plantilla fija. Es una **caja de herramientas**: un conjunto de bloques
disponibles que el agente selecciona, combina y ordena segun lo que el usuario pidio.

**Regla principal:** leer la intencion del usuario primero. Luego elegir solo los bloques
que respondan a esa intencion. Un informe puede tener 2 bloques o 8 — lo que el contexto
requiera, ni mas ni menos.

---

## Reglas de negocio (siempre aplican, en cualquier bloque)

1. Fuente de ventas: `TRIM("DESC_MOVIMIENTO") = 'VENTAS POS'` y `TRIM("SIGNO") = '-'`.
2. DESC_MOVIMIENTO solo tiene 3 valores válidos: `'VENTAS POS'`, `'CAMBIOS DE MERCANCIA ACLIENTE'`, `'DEVOLUCIÓN AL PROVEEDOR'`.
3. Valor de venta: `"CANTIDAD" * "PVP"`. Nunca `"PVP LISTA"` para tiendas individuales.
4. `"PVP LISTA"` solo cuando la pregunta sea sobre macroclientes/cadenas (ej: Exito como cadena).
5. Fechas: `TO_DATE("FECHA_MVTO", 'DD/MM/YYYY')`. Nunca `::DATE`.
6. Texto con espacios: siempre `TRIM()` en `"SIGNO"`, `"DESC_MOVIMIENTO"`, `"DEPARTAMENTO"`, etc.
7. **Jerarquía de producto**: `LINEA` → `LINEA_DETLL` (performance/exterior/junior) → `ESTILO_ITEM` (camisa, falda, pantaloneta) → `GRUPO` (manga corta/larga). Usar el nivel que corresponda segun la granularidad que pida el usuario.
8. No usar: `PVP HIST`, `PVP HIST LISTA`, `VENTA $ PVP HIST LISTA`, `FCH_ACT_PORTAFOLIO`, `FCH_ACT_SKU`, `LLAVE_DEP`.
9. Nunca inventar cifras. Si no hay dato: escribir "Sin informacion".

---

## Como elegir bloques

Antes de construir el informe, identificar:

- **Que quiere ver el usuario** — ventas globales, por zona, por producto, por tienda, alertas, etc.
- **Con que granularidad** — total, por mes, por semana, por departamento, por talla.
- **Para quien es** — gerencia (resumen ejecutivo + alertas), operaciones (detalle de tiendas), producto (SKUs y tallas).

Luego seleccionar de la lista de bloques disponibles. No es necesario usar todos.

---

## Bloques disponibles

Cada bloque describe: que muestra, que datos necesita y como formatearlo.

---

### BLOQUE A — Encabezado del documento

**Cuando usar:** siempre, es la portada de cualquier informe.

**Contenido:**
```
# [Titulo del informe segun lo que pidio el usuario]
**Periodo:** [rango de fechas o descripcion del periodo]
**Generado:** [fecha de hoy]
Creytex
```

El titulo debe reflejar exactamente lo que el usuario pidio, no un titulo generico.
Ejemplos:
- "Informe de Ventas por Departamento — Enero 2026"
- "Reporte de Tallas mas Vendidas — Q1 2026"
- "Resumen Ejecutivo de Ventas — Semana 3"

---

### BLOQUE B — Resumen ejecutivo

**Cuando usar:** cuando el usuario pide un resumen, informe general, o documento para gerencia.
No usar si el usuario pidio algo muy especifico (ej: "tabla de tallas vendidas").

**Contenido:** 4 a 6 bullets con los hallazgos mas importantes del periodo.
Cada bullet debe ser una conclusion, no un dato crudo.

Estructura sugerida de bullets:
- Volumen total: unidades vendidas + valor COP.
- Elemento destacado positivo (zona, talla, tienda con mejor desempeno).
- Elemento que requiere atencion (caida, inactividad, baja rotacion).
- Comparacion con periodo anterior si el dato esta disponible.
- Dato relevante segun el contexto (tasa de cambios, SKU nuevo, etc.).

**Datos necesarios:** resultado de consultas de totales y comparativos.

---

### BLOQUE C — Metricas principales

**Cuando usar:** cuando se necesita mostrar KPIs del periodo de forma tabular.
Util en informes generales y ejecutivos.

**Formato:** tabla con las metricas mas relevantes segun lo pedido.

| Metrica | Valor | Contexto adicional |
|---------|-------|--------------------|
| [nombre] | [cifra formateada] | [unidad, comparacion o nota] |

Metricas posibles (usar solo las relevantes):
- Unidades vendidas totales
- Valor total de ventas (COP)
- Numero de tiendas con venta en el periodo
- Ticket promedio por transaccion
- Talla / grupo / zona mas vendida
- Tasa de cambios/devoluciones (%)

**Datos necesarios:** consultas de agregacion sobre `ventas`.

---

### BLOQUE D — Desglose geografico

**Cuando usar:** cuando el usuario pide analisis por zona, departamento, ciudad o region.

**Formato:** tabla ordenada de mayor a menor por la metrica principal.

| Departamento / Ciudad | Unidades | Valor COP | % del total |
|-----------------------|----------|-----------|-------------|

Agregar al final:
- Top 3 zonas con mejor desempeno.
- Zonas con caida significativa (si hay comparativo disponible).

**Datos necesarios:** `GROUP BY TRIM("DEPARTAMENTO")` o `TRIM("CIUDAD")`.

---

### BLOQUE E — Desglose por tienda

**Cuando usar:** cuando el usuario pide ver resultados por punto de venta, tienda o dependencia.

**Formato:** tabla ordenada por valor o unidades.

| Tienda (DESC_DEPENDENCIA) | Ciudad | Unidades | Valor COP |
|---------------------------|--------|----------|-----------|

Agregar nota si alguna tienda aparece inactiva (`ESTADO_TIENDA`) o con `ESTADO_LINEA` inactivo
para alguna referencia.

**Datos necesarios:** `GROUP BY TRIM("DESC_DEPENDENCIA")`.

---

### BLOQUE F — Desglose por producto

**Cuando usar:** cuando el usuario pide analisis por talla, color, referencia, grupo o SKU.

**Formato segun granularidad pedida:**

Por talla:
| Talla | Unidades | % del total | Valor COP |
|-------|----------|-------------|-----------|

Por grupo/referencia:
| Grupo | Referencia | Unidades | Valor COP |
|-------|-----------|----------|-----------|

Por SKU (PLU + talla + color):
| PLU | Descripcion | Talla | Color | Unidades |
|-----|-------------|-------|-------|----------|

Identificar siempre:
- El SKU / talla / grupo de mayor rotacion.
- El de menor rotacion si el contexto lo requiere.

**Datos necesarios:** `GROUP BY "TALLA"`, `"GRUPO"`, `"PLU"`, `"COLOR"` segun lo pedido.

---

### BLOQUE G — Evolucion en el tiempo

**Cuando usar:** cuando el usuario pide tendencia, evolucion, comparacion por semana/mes/dia,
o quiere ver si las ventas subieron o bajaron.

**Formato:** tabla cronologica o descripcion de tendencia.

| Fecha / Semana / Mes | Unidades | Valor COP | Variacion vs anterior |
|----------------------|----------|-----------|----------------------|

Si hay pocos puntos, puede ser una lista en lugar de tabla.

**Datos necesarios:** `GROUP BY TO_DATE("FECHA_MVTO", 'DD/MM/YYYY')` o por `"Mes"`, `"Año"`.

---

### BLOQUE H — Estado de tiendas / portafolio

**Cuando usar:** cuando el usuario pide saber cuantas tiendas estan activas, cuales se
desactivaron, o el estado del portafolio de prendas.

**Contenido:**
- Tiendas activas con venta en el periodo.
- Tiendas sin venta (posiblemente inactivas).
- Referencias con `ESTADO_SKU_MOD = 'Inactivo'` o `ESTADO_LINEA` inactivo.
- Cambios de portafolio (`FCH_ACT_PORTAFOLIO` no nulo = paso de moda a linea).

**Datos necesarios:** consultas sobre `ESTADO_TIENDA`, `ESTADO_LINEA`, `ESTADO_SKU_MOD`.

---

### BLOQUE I — Alertas y hallazgos

**Cuando usar:** cuando el informe es para toma de decisiones o el usuario pide destacar
problemas, riesgos o anomalias. Tambien util al final de cualquier informe ejecutivo.

**Formato:** lista numerada. Solo incluir alertas que tengan datos que las respalden.

Tipos de alerta posibles:
1. Zona o tienda con caida > 15% vs periodo anterior.
2. SKU con rotacion muy baja (menos del 50% del promedio de su grupo).
3. Tiendas inactivas que tenian venta en el periodo previo.
4. Talla con sobrestock implicito (baja rotacion + alta disponibilidad historica).
5. Cualquier anomalia que el analisis de los datos revele.

Si no hay alertas con datos que las respalden: escribir "No se detectaron alertas en el periodo."

**Importante:** no inventar alertas. Solo las que los datos confirmen.

---

### BLOQUE J — Anexo de datos completos

**Cuando usar:** cuando el informe necesita respaldo detallado, o el usuario pide ver
el listado completo (todas las tiendas, todos los SKUs, etc.).

**Formato:** tabla completa sin limitar filas, al final del documento.

Indicar la fuente: "Datos extraidos de tabla `ventas` — CreytexToSQL."

---

## Formato de numeros (aplica a todos los bloques)

| Tipo | Formato | Ejemplo |
|------|---------|---------|
| Unidades | Entero con separador de miles | 8,420 |
| Valor COP | $ + separador de miles | $126,300,000 |
| Variacion | Signo + o - con 1 decimal | +10.1% / -22.0% |
| Porcentaje de composicion | 1 decimal + % | 42.0% |
| Fechas | DD/MM/YYYY | 15/01/2026 |

---

## Graficos de apoyo (skill `graficos_ventas`)

Para los bloques **D**, **F** y **G**, el pipeline genera automaticamente graficos
a partir de los datos obtenidos usando la skill `graficos_ventas`. No es necesario
que el agente los solicite explicitamente.

### Reglas de insercion

1. El grafico se genera **antes** de redactar el informe, usando los mismos datos.
2. El agente redactor debe insertar la imagen markdown **tal como viene en los datos**
   `![Titulo](ruta/real/del/archivo.png)` — la ruta exacta la proporciona el pipeline en la
   seccion "Graficos generados" del prompt. No inventar ni modificar la ruta.
3. El `timestamp` del grafico es el mismo del informe para que compartan sufijo.
4. Si la generacion del grafico falla, el agente debe continuar el informe sin la
   imagen y mencionarlo: "No fue posible generar el grafico para este bloque."

### Ubicacion sugerida por bloque

| Bloque | Contenido del bloque | Grafico sugerido | Donde insertarlo |
|--------|---------------------|------------------|------------------|
| D | Desglose geografico | `barras_horizontales` con top departamentos | Despues de la tabla de top departamentos |
| F | Desglose por producto | `barras_verticales` con distribucion de tallas o grupo | Despues de la tabla de producto |
| G | Evolucion temporal | `linea` con tendencia diaria/semanal/mensual | Despues de la tabla de evolucion |
| B | Resumen ejecutivo | `barras_horizontales` (si hay datos comparativos) | Al final del bloque, antes del siguiente |
| E | Desglose por tienda | `barras_horizontales` con top tiendas | Despues de la tabla de tiendas |

### Cuando NO forzar grafico

- Bloque A (portada): nunca lleva grafico.
- Bloque C (metricas): son numeros individuales, no graficar.
- Bloque H (estado): datos binarios/estados, no graficar.
- Bloque I (alertas): las alertas son texto, no graficar.
- Bloque J (anexo): datos completos, no graficar.

---

## Conversion a DOCX

- Entregar el Markdown completo a la herramienta `generar_docx`.
- El archivo se guarda en `reports/` con nombre descriptivo: `Informe_[tema]_[periodo].docx`.
- Las tablas deben tener encabezados. Las alertas en **negrita**.

---

## Ejemplo de lectura de intencion

| El usuario pide... | Bloques a usar |
|--------------------|---------------|
| "informe completo de ventas de enero" | A + B + C + D + F + I |
| "reporte de ventas por tienda" | A + E + opcionalmente I |
| "como van las ventas por talla" | A + F (por talla) |
| "resumen ejecutivo para gerencia" | A + B + C + I |
| "evolucion de ventas semana a semana" | A + G |
| "cuantas tiendas estan activas" | A + H |
| "informe detallado con todo" | A + B + C + D + E + F + G + H + I + J |
