# Consultor Inteligente de Ventas

Sistema de análisis de datos de ventas en lenguaje natural. Traduce preguntas en español a SQL, ejecuta consultas sobre PostgreSQL, genera gráficos y produce informes profesionales en Word (.docx), todo desde una interfaz web Streamlit.

---

## Características principales

- **Consultas en lenguaje natural** → SQL → respuesta formateada
- **Gráficos automáticos** (barras, línea, torta) con tamaño dinámico según cantidad de datos
- **Informes completos en .docx** con estructura macro → micro: resumen ejecutivo, geográfico, tiendas, producto, referencia, talla, alertas
- **Interfaz web** con historial de informes y gráficos, descarga de Word, feedback por respuesta
- **Ingesta incremental** de datos TXT con deduplicación MD5 en PostgreSQL
- **Múltiples proveedores LLM**: Groq, Cerebras, Gemini (configurable en `.env`)

---

## Flujo del sistema

```
Pregunta del usuario
        │
        ▼
┌─────────────────────┐
│ generador_consultas │── LLM traduce pregunta → SQL
└──────────┬──────────┘
           │ SQL
           ▼
┌─────────────────────┐
│     validador       │── LLM revisa SQL (máx 3 intentos)
└──────────┬──────────┘
           │ SQL aprobado
           ▼
┌─────────────────────┐
│   consultar_db      │── Ejecuta SELECT en PostgreSQL
└──────────┬──────────┘
           │ JSON con resultados
           ▼
┌─────────────────────┐        ┌──────────────────────┐
│ redactor_respuesta  │   o    │  Pipeline de informe  │
│ (respuesta natural) │        │  (Markdown → .docx)   │
└─────────────────────┘        └──────────────────────┘
```

### Pipeline de informe (palabras clave: informe, reporte, documento, word)

1. LLM planifica las consultas SQL necesarias según los bloques del informe
2. Las consultas se ejecutan directamente en PostgreSQL (sin validador para evitar falsos positivos)
3. Se generan gráficos automáticamente por bloque
4. LLM redacta el informe en Markdown usando los bloques relevantes
5. El Markdown se convierte a `.docx` y se guarda en `reports/`

---

## Estructura del proyecto

```
├── src/
│   ├── orquestador.py         ← Orquestador principal (cadena de agentes)
│   └── ingesta_postgres.py    ← Carga TXT → PostgreSQL con dedup MD5
│
├── agents/
│   ├── generador_consultas.md ← Prompt: NL → SQL
│   ├── validador.md           ← Prompt: revisión y validación de SQL
│   └── redactor_respuesta.md  ← Prompt: JSON → lenguaje natural
│
├── tools/
│   ├── consultar_db.py        ← Ejecutor SQL solo lectura (salida JSON)
│   ├── generar_docx.py        ← Markdown → .docx con formato profesional
│   ├── generar_excel.py       ← Datos tabulares → .xlsx con formato profesional
│   └── generar_grafico.py     ← Gráficos PNG con matplotlib (tamaño dinámico)
│
├── skills/
│   ├── consultar_skill/       ← Skill de consulta (esquema, reglas SQL)
│   ├── excel_skill/           ← Skill de exportación a Excel
│   ├── informe_ventas/        ← Skill de informes (bloques A–N, macro→micro)
│   └── graficos_ventas/       ← Skill de decisión de gráficos
│
├── ui/
│   ├── streamlit_app.py       ← Interfaz web principal
│   ├── db.py                  ← SQLite: historial de informes y gráficos
│   └── prompt_logger.py       ← Logger de prompts + feedback en JSON
│
├── prompts/                   ← Logs diarios de uso y feedback (gitignored)
├── reports/                   ← Informes .docx y gráficos .png (gitignored)
├── excel_sheets/              ← Archivos .xlsx exportados (gitignored)
├── data_samples/              ← Archivos TXT fuente (gitignored)
├── requirements.txt
├── .env.example               ← Template de variables de entorno
└── .env                       ← Credenciales (gitignored)
```

---

## Inicio rápido

### 1. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 2. Configurar `.env`

```bash
cp .env.example .env
# Editar .env con las credenciales reales
```

Variables requeridas:

```env
# Proveedor LLM activo: groq | cerebras | gemini
LLM_PROVIDER = groq

# Groq
GROQ_API_KEY = tu_api_key
GROQ_MODEL = llama-3.3-70b-versatile

# Cerebras (opcional)
CEREBRAS_KEY = tu_key
CEREBRAS_MODEL = gemma-4-31b

# Gemini (opcional)
GEMINI_API_KEY = tu_key
GEMINI_MODEL = gemini-2.0-flash

# PostgreSQL
DB_HOST = localhost
DB_PORT = 5432
DB_NAME = your_db_name
DB_USER = postgres
DB_PASSWORD = tu_password

# Ruta al archivo TXT fuente (puede ser ruta de red UNC)
DATA_FILE_PATH = ...\your_txt_file.txt
```

### 3. Cargar datos en PostgreSQL

```bash
# Primera carga o carga incremental (solo filas nuevas)
python src/ingesta_postgres.py

# Recarga completa desde cero
python src/ingesta_postgres.py --full-sync
```

### 4. Correr la interfaz web

```bash
streamlit run ui/streamlit_app.py
```

Abrir en el navegador: `http://localhost:8501`

### 5. (Opcional) Consulta por línea de comandos

```bash
python src/orquestador.py "ventas por departamento en enero"
python src/orquestador.py "genera un informe de ventas completo"
```

---

## Interfaz web

La UI tiene dos áreas principales:

**Panel lateral — Historial**
- Pestaña **Informes**: lista todos los informes generados. Al hacer clic muestra el informe con imágenes y un botón de descarga en Word.
- Pestaña **Gráficos**: lista todos los gráficos generados. Al hacer clic muestra la imagen con opción de descarga.

**Área principal — Chat**
- Campo de preguntas en lenguaje natural
- Sugerencias rápidas con pills
- Respuestas con texto, tablas y gráficos inline
- Widget de **feedback** al final de cada respuesta (Buena / Regular / Mala + comentario opcional)

---

## Ingesta de datos

El script `ingesta_postgres.py` soporta dos modos:

| Modo | Comando | Comportamiento |
|------|---------|----------------|
| Incremental | `python src/ingesta_postgres.py` | Solo inserta filas nuevas (dedup MD5) |
| Full sync | `python src/ingesta_postgres.py --full-sync` | Recrea la tabla desde cero |

La deduplicación calcula `md5(col1 \|\| '\|' \|\| col2 \|\| ...)` en PostgreSQL server-side. Una segunda ejecución con el mismo archivo inserta 0 filas.

El archivo fuente se configura con la variable `DATA_FILE_PATH` en `.env`, que acepta rutas locales o rutas UNC de red (`\\servidor\carpeta\archivo.txt`).

---

## Exportación a Excel

El sistema puede exportar cualquier resultado tabular a un archivo `.xlsx` con formato profesional usando la tool `tools/generar_excel.py`.

### Activación

El usuario solicita la exportación mencionando palabras clave como "excel", "xlsx", "exportar", "hoja de cálculo", "descargar como excel".

### Formato del Excel generado

| Elemento | Estilo |
|----------|--------|
| **Título** (opcional) | Fila fusionada, fondo azul oscuro, texto blanco bold 14pt |
| **Headers** | Fondo `#1F4E79`, texto blanco bold, centrado |
| **Filas alternadas** | Pares con fondo azul claro `#D6E4F0` |
| **Números** | Separador de miles (`#,##0`), alineación derecha |
| **Ancho de columna** | Autoajuste (máx 40 caracteres) o personalizado |
| **Bordes** | Líneas finas color gris en toda la tabla |

Los archivos se guardan en `excel_sheets/` con nombre descriptivo + timestamp.

---

El motor de gráficos (`tools/generar_grafico.py`) soporta 5 tipos:

| Tipo | Uso recomendado |
|------|----------------|
| `barras_horizontales` | Rankings, comparativos con etiquetas largas |
| `barras_verticales` | Distribuciones con pocas categorías |
| `barras_agrupadas` | Comparación de series por categoría |
| `linea` | Tendencias temporales |
| `torta` | Participación (máx 5 categorías) |

El tamaño de la figura se calcula dinámicamente:
```
espaciado = 3.0 / (n+1) + 0.30   [pulgadas por ítem]
dim_util  = espaciado × (n+1)
dim_total = dim_util + margen_fijo
```
Esto garantiza que gráficos con 3 ítems sean compactos y con 20+ ítems sean legibles.

---

## Bloques de informe

Los informes siguen una estructura macro → micro. El LLM selecciona los bloques relevantes según la petición:

| Bloque | Contenido |
|--------|-----------|
| A | Encabezado / portada |
| B | Resumen ejecutivo |
| C | Métricas principales (incluye línea top y referencia top) |
| D | Geográfico: departamento → ciudad → zona |
| E | Dependencias (top 5 mejor y top 5 peor) |
| F | Tiendas (DESC_DEPENDENCIA) |
| G | Línea de producto |
| H | Producto (DESC_ITEM) |
| I | Referencia (+ venta de ref top por zona) |
| J | Talla |
| K | Evolución en el tiempo |
| L | Estado de tiendas / portafolio |
| M | Alertas y hallazgos |
| N | Anexo de datos completos |

---

## Feedback y logs de uso

Cada interacción se registra en `prompts/prompts_YYYYMMDD.json`:

```json
{
  "id": "uuid4",
  "timestamp": "2026-07-27T10:30:00",
  "pregunta": "ventas por departamento",
  "tipo": "consulta",
  "modelo_llm": "llama-3.3-70b-versatile",
  "proveedor_llm": "groq",
  "duracion_seg": 4.2,
  "exito": true,
  "archivos_generados": [],
  "feedback": "bueno",
  "feedback_msg": "Respuesta precisa y rápida"
}
```

Un archivo por día. Los archivos JSON están en `.gitignore` — son datos de usuarios, no se versionan.

---

## Decisiones de diseño

- **Sin memoria de conversación** — cada llamada es stateless; el LLM recibe el contexto completo en cada turno
- **Dedup server-side** — el hash MD5 se calcula en PostgreSQL para consistencia garantizada
- **Validador solo en consultas interactivas** — en el pipeline de informe las queries se ejecutan directo en PostgreSQL para evitar falsos positivos del validador LLM
- **Solo lectura** — `consultar_db.py` rechaza cualquier sentencia que no sea SELECT a nivel de keyword antes de llegar a la DB
- **Tamaño dinámico de gráficos** — la figura se calcula en función del número de ítems, no es un tamaño fijo
- **Retry con backoff exponencial** — `llamar_llm()` reintenta 3 veces (10s / 20s / 40s) ante rate limits o timeouts del proveedor LLM

---

## Requisitos

```
psycopg2-binary>=2.9
python-dotenv>=1.0
python-docx>=1.1
openpyxl>=3.1
matplotlib>=3.8
numpy>=1.26
pandas>=2.1
groq>=0.12
openai>=1.50
google-genai>=1.0
streamlit>=1.40
```

Python 3.12+ recomendado.
