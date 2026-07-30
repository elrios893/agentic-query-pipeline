---
name: excel-sheets
description: >
  Exporta resultados tabulares a archivos .xlsx con formato profesional
  (headers con fondo oscuro, texto blanco bold, filas alternadas, autoajuste).
  Se activa cuando el usuario pide "excel", "xlsx", "exportar a excel",
  "descargar como excel", "tabla en excel", "hoja de calculo".
license: MIT
compatibility: opencode
activator:
  type: intent_match
  patterns:
    - "excel"
    - "xlsx"
    - "exportar.*excel"
    - "descargar.*excel"
    - "tabla.*excel"
    - "hoja.*calculo"
    - "sheet"
    - "export.*xls"
    - "pasa.*excel"
  auto_execute: false
  output_dir: excel_sheets/
metadata:
  herramienta_excel: generar_excel
  carpeta_salida: excel_sheets/
---

# Skill: Exportar a Excel — Creytex

## Objetivo

Exportar resultados de consultas SQL o cualquier dato tabular a un archivo `.xlsx`
con formato profesional usando la tool `generar_excel`.

## Cuando activar esta skill

Activar **siempre** que el usuario mencione explícitamente:

| Frase clave | Ejemplo |
|-------------|---------|
| "excel" | "pásame esto a excel" |
| "xlsx" | "dame el archivo en xlsx" |
| "exportar" | "exporta esta tabla" |
| "descargar como excel" | "descarga los datos en excel" |
| "hoja de cálculo" | "necesito una hoja de cálculo con estos números" |

## Formato de datos esperado por la tool

La tool `generar_excel()` recibe un JSON con esta estructura:

```json
{
  "headers": ["Departamento", "Unidades", "Valor COP"],
  "rows": [
    ["ANTIOQUIA", 33632, 126300000],
    ["BOGOTA", 16288, 78450000]
  ],
  "sheet_name": "Ventas por Depto",
  "title": "Reporte de Ventas — Julio 2026"
}
```

### Campos

| Campo | Tipo | Requerido | Descripción |
|-------|------|-----------|-------------|
| `headers` | `list[str]` | Sí | Nombres de las columnas |
| `rows` | `list[list]` | Sí | Filas de datos (cada fila es un array del mismo largo que headers) |
| `sheet_name` | `str` | No (default `"Datos"`) | Nombre de la pestaña en el Excel |
| `title` | `str` | No | Título centrado sobre la tabla (fila fusionada) |
| `output_path` | `str` | Sí | Ruta completa de salida del .xlsx |
| `column_widths` | `list[int]` | No | Anchos de columna personalizados (si no se pasa, autoajusta) |

## Formato de salida de la tool

```json
{
  "success": true,
  "archivo": "C:\\...\\excel_sheets\\ventas_depto_20260729_143000.xlsx",
  "hojas": ["Ventas por Depto"],
  "filas": 10,
  "columnas": 5
}
```

## Ruta de salida

Los archivos se guardan en `excel_sheets/` en la raíz del proyecto.
Usar la convención de nombre:

```
excel_sheets/{slug_descripitvo}_{timestamp}.xlsx
```

Donde `slug` es una versión corta del título o descripción (sin espacios, max 50 chars)
y `timestamp` es `YYYYMMDD_HHMMSS`.

## Reglas de transformación SQL → Excel

| Si el resultado SQL tiene columnas... | Mapear a headers de Excel |
|---------------------------------------|---------------------------|
| Nombres de columna originales | Usarlos directamente como headers |
| `COUNT(*)`, `SUM(col)` | Renombrar con nombres legibles: "Total", "Unidades", "Valor" |

Los tipos de dato se infieren automáticamente:
- `int` → formato `#,##0`
- `float` → formato `#,##0.00`
- `str` → texto alineado a la izquierda

## Formato visual del Excel generado

| Elemento | Estilo |
|----------|--------|
| **Título** (si se especifica) | Fila fusionada, fondo azul oscuro `#2F5496`, texto blanco bold 14pt |
| **Headers** | Fondo `#1F4E79`, texto blanco bold 11pt, centrado |
| **Filas pares** | Fondo azul claro `#D6E4F0` |
| **Filas impares** | Fondo blanco |
| **Números** | Alineación derecha, separador de miles |
| **Texto** | Alineación izquierda |
| **Bordes** | Líneas finas color gris `#B0B0B0` en toda la tabla |
| **Ancho de columna** | Autoajuste (mín 4 caracteres, máx 40) o personalizado |

## Llamada a la tool

```python
import subprocess, json, os

excel_script = Path('tools/generar_excel.py')
env = os.environ.copy()
env['EXCEL_DATA'] = json.dumps({
    "headers": ["Departamento", "Unidades", "Valor COP"],
    "rows": [["ANTIOQUIA", 33632, 126300000]],
    "title": "Ventas por Departamento",
    "sheet_name": "Ventas",
    "output_path": "excel_sheets/ventas_depto_20260729.xlsx"
})
resultado = subprocess.run(
    ['python', str(excel_script)],
    capture_output=True, text=True, env=env, timeout=60, encoding='utf-8'
)
data = json.loads(resultado.stdout)
if data['success']:
    ruta = data['archivo']
```

## Manejo de errores

| Error | Causa | Acción |
|-------|-------|--------|
| `EXCEL_DATA no encontrada` | No se pasó la variable de entorno | Asegurar que env EXCEL_DATA está seteada antes de llamar |
| `JSON invalido` | El JSON de entrada tiene errores de sintaxis | Revisar comillas, comas, estructura |
| `Faltan headers o rows` | El JSON no tiene los campos obligatorios | Verificar que ambos arrays existen y no están vacíos |
| Archivo no se puede guardar | Ruta inválida o permisos | Usar una ruta válida dentro de `excel_sheets/` |

## Límites

- **Sin límite** de filas (Excel soporta ~1M filas, pero considerar rendimiento)
- Máximo 20 columnas recomendado para legibilidad
- Si hay más de 1000 filas, sugerir al usuario que el archivo puede ser grande
