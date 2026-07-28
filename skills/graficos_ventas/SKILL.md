---
name: graficos-ventas
description: >
  Decide si un resultado tabular merece un grafico y cual tipo usar,
  luego llama a la tool generar_grafico() para producirlo.
  Se activa con frases como "grafica esto", "hazme un grafico",
  "muestrame la tendencia", "comparacion visual", y en forma implicita
  cuando el informe de ventas incluye bloques de evolucion temporal
  o desgloses comparativos.
license: MIT
compatibility: opencode
activator:
  type: intent_match
  patterns:
    - "grafica"
    - "grafico"
    - "chart"
    - "tendencia"
    - "evolucion"
    - "comparacion.*visual"
    - "muestrame.*grafico"
    - "visualiza"
  auto_execute: false
metadata:
  herramienta_grafico: generar_grafico
  carpeta_salida: reports/charts/
---

# Skill: Graficos de Ventas — Creytex

## Filosofia de uso

Esta skill no genera el grafico por si misma. Su unica funcion es **decidir si
graficar y que tipo usar**, y luego llamar a la tool `generar_grafico()` con los
datos ya transformados al formato que ella espera.

**La pregunta guia:** "Al usuario le seria mas util ver esto como un numero
suerto o como una forma / barra / linea?" Si la respuesta es "forma / barra /
linea" → graficar. Si es "numero suelto" → no graficar.

---

## Cuando NO graficar (aunque los datos lo permitan)

| Situacion | Motivo | Accion |
|-----------|--------|--------|
| Un solo numero (ej. "total vendido ayer: $45M") | Ruido visual, no hay comparacion | Solo texto |
| Menos de 2 filas en el resultado | No hay con que comparar | Solo texto |
| El usuario dijo explicitamente "sin graficos", "solo el numero", "texto nada mas" | Preferencia explicita del usuario | Omitir grafico aunque el criterio lo sugiera |
| Datos con demasiadas categorias (>15) sin agrupar | Grafico ilegible | Agrupar o no graficar |
| El informe ya lleva 4 graficos | Satura el documento | Priorizar los mas relevantes |
| Error en la tool al generar | No detener el informe | Mencionar en texto: "no fue posible generar el grafico para X" y continuar |

---

## Mapeo: tipo de dato / pregunta → tipo de grafico

> **REGLA PRIORITARIA — eje X temporal:** Antes de evaluar cualquier otra regla,
> inspeccioná la columna que irá en el eje X. Si los valores son fechas, días,
> semanas o meses (ej: `2026-01-15`, `Ene`, `Semana 3`, `Lunes`) o si la columna
> se llama `Fecha`, `dia`, `fecha_mvto`, `semana`, `mes` → el tipo **siempre es
> `linea`**, sin excepción. Nunca usar `barras_verticales` ni `barras_horizontales`
> cuando el eje X representa tiempo.

| Cuando los datos muestran... | Ejemplo de pregunta | Tipo de grafico | Formato_y tipico |
|-----------------------------|---------------------|-----------------|------------------|
| Eje X con fechas, dias, semanas o meses — **cualquier intervalo temporal** | "ventas por dia en mayo", "tendencia semanal", "como van las ventas dia a dia" | `linea` | moneda o unidades |
| Comparacion entre categorias, ≤10 items | "top 5 departamentos por ventas" | `barras_horizontales` | moneda o unidades |
| Mismo conjunto de categorias, 2 periodos | "ventas este mes vs mes anterior por zona" | `barras_agrupadas` | moneda o unidades |
| Composicion/participacion, ≤5 categorias | "participacion por linea de producto" | `torta` | porcentaje |
| Composicion/participacion, >5 categorias | "participacion por departamento" | `barras_horizontales` | porcentaje |
| Distribucion simple de variable categorica (tallas, colores, referencias) | "unidades por talla (S/M/XL)" | `barras_verticales` | unidades |
| Ranking simple (1. Antioquia 2. Bogota...) | "cuales son las 3 zonas que mas vendieron" | `barras_horizontales` | moneda o unidades |

> **Regla de oro para barras:** cuando las etiquetas del eje X son largas
> (nombres de 10+ caracteres), preferir `barras_horizontales` para que se
> lean bien. Las verticales rotan el texto y son mas dificiles de escanear.

> **Señales de que el eje X es temporal** (usar `linea` obligatoriamente):
> - La columna SQL se llama `Fecha`, `dia`, `fecha`, `semana`, `mes`, `periodo`, `fecha_mvto` o similar
> - Los valores tienen formato de fecha: `2026-05-01`, `01/05/2026`, `May`, `Semana 18`
> - La pregunta contiene palabras como: "por día", "por semana", "por mes", "evolución", "tendencia", "día a día", "intervalo", "entre el ... y el ...", "durante", "a lo largo de"

---

## Como transformar los datos SQL antes de llamar a la tool

La consulta SQL devuelve filas con nombres de columna reales.
La tool `generar_grafico()` espera claves genericas `x` / `y` (y opcionalmente `serie`).

### Transformacion paso a paso

**Antes (resultado SQL):**

```json
[
  {"departamento": "ANTIOQUIA", "total_unidades": 33632},
  {"departamento": "BOGOTA",    "total_unidades": 16288},
  {"departamento": "ATLANTICO", "total_unidades": 8794}
]
```

**Despues (datos para la tool):**

```json
[
  {"x": "ANTIOQUIA", "y": 33632},
  {"x": "BOGOTA",    "y": 16288},
  {"x": "ATLANTICO", "y": 8794}
]
```

**Para barras_agrupadas (2 periodos):**

```json
[
  {"x": "ANTIOQUIA", "y": 33632, "serie": "Enero 2026"},
  {"x": "ANTIOQUIA", "y": 29500, "serie": "Febrero 2026"},
  {"x": "BOGOTA",    "y": 16288, "serie": "Enero 2026"},
  {"x": "BOGOTA",    "y": 15100, "serie": "Febrero 2026"}
]
```

### Reglas de transformacion

| Si la columna SQL se llama... | Mapear a... |
|------------------------------|-------------|
| `DEPARTAMENTO`, `CIUDAD`, `DESC_DEPENDENCIA`, `LINEA`, `MARCA`, `TALLA`, `MES`, `ZONA`, etc. | `x` |
| `TOTAL_UNIDADES`, `CANTIDAD`, `SUM(CANTIDAD)`, `UNIDADES` | `y` con `formato_y="unidades"` |
| `VALOR_TOTAL`, `VENTA`, `TOTAL_VENTA`, `SUM(CANTIDAD * PVP)`, `VENTA $` | `y` con `formato_y="moneda"` |
| `PARTICIPACION`, `PORCENTAJE`, `%`, `SHARE` | `y` con `formato_y="porcentaje"` |
| Cualquier columna que diferencie periodos (ej. `MES`, `SEMANA`, `PERIODO`) | `serie` (para barras_agrupadas) |

### Determinar formato_y segun el contexto

- **MONEDA**: si los valores representan dinero (ventas en COP, valor total, precios).
  Los valores numericos grandes (millones) se muestran con $ y separador de miles.
  Ej: `$45,000,000` en vez de `45000000`.
- **UNIDADES**: si representan conteo de articulos (cantidad de productos, numero de transacciones).
  Nunca llevan signo $. Ej: `33,632` en vez de `33632`.
- **PORCENTAJE**: si representan proporcion, participacion, o ya vienen calculados como %
  de un total. Llevan el simbolo % y un decimal. Ej: `33.6%`.

> **Regla de seguridad:** si tienes dudas sobre la columna, inspecciona los nombres
> de columna del resultado SQL. Si contiene "PVP", "VALOR", "VENTA $" o "$" → moneda.
> Si contiene "CANTIDAD", "UNIDAD" → unidades.
> Si contiene "%", "PORCENTAJE", "PARTICIPACION" → porcentaje.

---

## Integracion con el informe de ventas

Cuando esta skill se usa dentro del pipeline de `informe_ventas`:

1. El agente redactor incluye la llamada a `generar_grafico()` **despues** de
   presentar la tabla de datos del bloque correspondiente.
2. El grafico se inserta como imagen usando la ruta real que devuelve la tool
   `![Titulo](reports/charts/chart_nombre_timestamp.png)` justo debajo
   de la tabla, no antes.
3. El `timestamp` que se pasa a `generar_grafico()` debe ser el mismo del informe
   para que los archivos compartan sufijo y sea facil emparejarlos.
4. El texto alternativo de la imagen debe describir brevemente lo que muestra:
   `![Evolucion diaria de ventas - Enero 2026]`

### Ubicacion recomendada por bloque

| Bloque del informe | Donde insertar el grafico | Tipo sugerido |
|--------------------|---------------------------|---------------|
| A — Metricas clave | No graficar (son numeros individuales) | — |
| B — Resumen ejecutivo | Al final del bloque, como apoyo visual | barras_horizontales |
| C — Geografia (top departamentos) | Despues de la tabla de top departamentos | barras_horizontales |
| D — Producto (lineas, marcas) | Despues de la tabla del top producto | barras_horizontales |
| E — Tiendas (top dependencias) | Despues de la tabla de top tiendas | barras_horizontales |
| F — Clima (zonas climaticas) | Despues de la tabla de clima | barras_verticales |
| G — Evolucion temporal | Despues de la tabla de tendencia | linea |
| H — Tallas | Despues de la tabla de distribucion tallas | barras_verticales |
| I — Alertas / anomalias | Marcando con rojo en el grafico del bloque mas relevante | (segun el bloque) |
| J — Clientes MACRO | Despues de la tabla de macroclientes | barras_horizontales |

---

## Limite de graficos por informe

- **Maximo 4 graficos** por informe. Si hay mas candidatos, priorizar:
  1. Evolucion temporal (linea) — siempre es el mas informativo
  2. El desglose mas relevante para la pregunta del usuario
  3. El desglose con mayor impacto en ventas (top por valor)
- Si el informe es corto (2-3 bloques), maximo 2 graficos.
- Si la pregunta es comparativa ("comparar periodo A vs B"), los graficos tienen
  prioridad sobre las tablas.

---

## Llamada a la tool

```python
from tools.generar_grafico import generar_grafico

resultado = generar_grafico(
    datos=[{"x": "ANTIOQUIA", "y": 33632}, {"x": "BOGOTA", "y": 16288}],
    tipo="barras_horizontales",
    titulo="Unidades Vendidas por Departamento - Top 2",
    etiqueta_x="Departamento",
    etiqueta_y="Unidades Vendidas",
    formato_y="unidades",
    timestamp="20260723_142901",  # mismo timestamp del informe
)

if resultado["error"]:
    # No detener el informe, solo mencionarlo
    texto_alternativo = "No fue posible generar el grafico para este bloque."
else:
    ruta_imagen = resultado["path"]
    texto_alternativo = f"![Grafico]({ruta_imagen})"
```

---

## Manejo de errores

Si `generar_grafico()` devuelve `{"error": "..."}`:

| Error tipico | Causa | Que hacer |
|-------------|-------|-----------|
| `Datos vacios o invalidos` | El resultado SQL no tenia filas | Continuar sin grafico. Mencionar: "No hay datos suficientes para graficar." |
| `Tipo no soportado: ...` | Se paso un tipo que no existe | Revisar el mapeo y corregir el tipo. Usar solo: linea, barras_horizontales, barras_verticales, barras_agrupadas, torta. |
| `Demasiadas categorias (N) para grafico de torta` | Se intento torta con >5 items | Cambiar a barras_horizontales con los mismos datos. No reintentar torta. |
| `barras_agrupadas requiere clave "serie"` | Falta la columna de periodo en los datos | No se puede graficar agrupado. Usar barras_verticales o barras_horizontales simples con el periodo mas reciente. |

**Nunca detener el informe por un error de grafico.** El grafico es un adorno
analitico, no un componente critico del documento.

---

## Formato de salida final

Cuando generes un grafico dentro de un informe, el markdown debe verse asi:

```markdown
### Top 5 Departamentos por Unidades Vendidas

| Departamento | Unidades |
|-------------|----------|
| ANTIOQUIA | 33,632 |
| BOGOTA | 16,288 |
| ATLANTICO | 8,794 |
| BOLIVAR | 8,365 |
| SANTANDER | 6,611 |

![Top 5 Departamentos por Unidades Vendidas](reports/charts/chart_top_5_departamentos_20260723_142901.png)
```

El grafico va **despues** de la tabla, no antes. La tabla da los numeros exactos;
el grafico da el patron visual.

---

## Resumen rapido para el agente

1. Preguntar: ¿Esto se ve mejor como numero o como forma? → numero = texto, forma = grafico
2. Si grafico → elegir tipo segun la tabla de mapeo
3. Transformar columnas SQL → `x` / `y` / `serie`
4. Elegir `formato_y`: moneda / unidades / porcentaje
5. Llamar a `generar_grafico()` con los datos transformados
6. Si hay error → mencionarlo en texto y continuar (nunca detener)
7. En informes: maximo 4 graficos, insertar despues de la tabla correspondiente
