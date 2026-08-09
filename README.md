# Consultor Inteligente de Ventas

Sistema de análisis de datos de ventas en lenguaje natural. Traduce preguntas en español a SQL, ejecuta consultas sobre PostgreSQL, genera gráficos y produce informes profesionales en Word (.docx). Un servidor FastAPI central concentra toda la lógica y mantiene el estado de sesión; Streamlit (interfaz web) y un bot de Telegram son dos clientes intercambiables que hablan con ese servidor por HTTP.

---

## Características principales

- **Consultas en lenguaje natural** → SQL → respuesta formateada
- **Servidor central con memoria de sesión** — DataFrames y contexto conversacional en memoria por `session_id`, con enrutamiento automático entre consulta nueva, refinamiento, pregunta sobre datos ya obtenidos y charla conversacional
- **Agente analista** — modo de análisis profundo que puede pedir rondas adicionales de datos por su cuenta antes de concluir
- **Dos interfaces, un solo backend**: Streamlit (web) y Telegram, ambas clientes HTTP de `src/server.py`
- **Gráficos automáticos** (barras, línea, torta) con tamaño dinámico según cantidad de datos
- **Informes completos en .docx** con estructura macro → micro: resumen ejecutivo, geográfico, tiendas, producto, referencia, talla, alertas
- **Interfaz web** con historial de informes y gráficos, descarga de Word, feedback por respuesta
- **Ingesta incremental** de datos TXT con deduplicación MD5 en PostgreSQL
- **Logging centralizado y trazable** — cada interacción, sin importar la interfaz de origen, queda en un único log diario con todas las consultas SQL que participaron en la respuesta
- **Múltiples proveedores LLM**: Groq, Cerebras, Gemini (configurable en `.env`)

---

## Arquitectura

```
                     ┌───────────────────┐
                     │  ui/streamlit_app │  (web)
                     └─────────┬─────────┘
                               │ HTTP POST /chat
                               │ {session_id, pregunta, origen:"streamlit"}
                               ▼
┌──────────────────┐   ┌───────────────────┐
│ telegram_bot/     │──▶│   src/server.py    │  FastAPI — único backend
│ (bot de Telegram) │   │  (SessionStore en  │
└──────────────────┘   │   memoria + rutas) │
   HTTP POST /chat      └─────────┬─────────┘
   {origen:"telegram"}            │
                                   ▼
                     ┌───────────────────────────┐
                     │  src/enrutador_sesion.py   │── clasifica en 1 de 4 rutas
                     └─────────────┬─────────────┘
                                   │
        ┌──────────────┬──────────┴──────────┬────────────────┐
        ▼              ▼                     ▼                ▼
 NUEVA_CONSULTA   REFINAMIENTO           SOBRE_DATOS      CONVERSACIONAL
 (SQL nueva)      (SQL con contexto      (responde con    (LLM + historial,
        │          de sesión)            Pandas sobre un      sin BD)
        │              │                 df en memoria;
        └──────┬───────┘                 si no alcanza,
               ▼                         hace 1 consulta
      src/orquestador.py                 SQL complementaria)
   generar SQL → validar → ejecutar
   → (opcional) agente_analista
   → redactar respuesta / informe .docx
               │
               ▼
      src/prompt_logger.py  ── único punto de registro (llamado desde server.py)
      prompts/prompts_YYYYMMDD.json  (pregunta, tipo, prompt_source, sql_queries[], feedback)
```

Streamlit y Telegram **no contienen lógica de negocio**: arman el request HTTP, muestran la respuesta y descargan archivos. Toda decisión de ruteo, ejecución SQL, generación de gráficos/informes y registro de logs vive en el servidor, así ambas interfaces quedan sincronizadas automáticamente.

### Pipeline de una consulta nueva (NUEVA_CONSULTA / REFINAMIENTO)

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
│ redactor_respuesta  │   o    │  agente_analista      │── si se pide /analisis
│ (respuesta natural) │        │  (ver sección propia) │
└─────────────────────┘        └──────────────────────┘
```

El resultado (DataFrame) queda guardado en la sesión (`session_store.py`) para que turnos siguientes puedan referirse a él sin volver a consultar la base de datos (rutas REFINAMIENTO y SOBRE_DATOS).

### Pipeline de informe (palabras clave: informe, reporte, documento, word)

1. LLM planifica las consultas SQL necesarias según los bloques del informe
2. Las consultas se ejecutan directamente en PostgreSQL (sin validador para evitar falsos positivos) — cada una queda registrada individualmente para trazabilidad
3. Se generan gráficos automáticamente por bloque
4. LLM redacta el informe en Markdown usando los bloques relevantes
5. El Markdown se convierte a `.docx` y se guarda en `reports/`

---

## Agente analista

Cuando el usuario pide análisis profundo (comando `/analisis`, o el enrutador detecta intención de análisis), `agente_analista()` en `src/orquestador.py` entra en un ciclo iterativo en vez de responder con una sola pasada:

1. Recibe el resultado de la consulta inicial (SQL + filas) y métricas pre-computadas.
2. El LLM analista decide si con esos datos alcanza (`estado: "completo"`) o si necesita una consulta complementaria (`estado: "necesita_datos"`), indicando qué pregunta adicional hacer.
3. Si pide más datos, el sistema reutiliza `generador_consultas` + `validador` para generar y ejecutar esa consulta extra, la agrega al acumulado, y vuelve al paso 2.
4. El ciclo se repite hasta `estado: "completo"` o hasta `MAX_RONDAS_ANALISTA = 3` rondas, lo que ocurra primero.

Ejemplo: *"analiza por qué cayeron las ventas de caballero"* → el analista pide la consulta inicial de ventas por línea, detecta la caída en "caballero", y por su cuenta solicita una segunda consulta desglosada por tienda/zona para identificar dónde se concentra la caída, antes de concluir.

**Trazabilidad:** cada ronda queda registrada como una entrada independiente en `sql_queries` (`consulta_principal`, `analista_ronda_1: <descripción>`, `analista_ronda_2: ...`), generada por `_sql_queries_desde_acumulados()` y propagada hasta el log final — así se puede reconstruir exactamente qué preguntas se hizo el propio agente para llegar a su conclusión.

---

## Endpoints del servidor (`src/server.py`)

| Método | Ruta | Descripción |
|--------|------|-------------|
| `POST` | `/chat` | Endpoint principal. Recibe `{session_id, pregunta, origen}`, enruta el mensaje, ejecuta el pipeline correspondiente y devuelve `{respuesta, ruta, tipo, imagenes, ruta_excel, ruta_docx, df_creado, log_id, turno, duracion_seg}`. Registra el log centralizado antes de responder. |
| `POST` | `/reset/{session_id}` | Elimina la sesión (historial + DataFrames en memoria) del `SessionStore`. Usado por el comando `/reset` de Telegram y por Streamlit al iniciar un chat nuevo. |
| `GET` | `/health` | Estado del servidor y cantidad de sesiones activas. |
| `GET` | `/sessions` | Debug: resumen de todas las sesiones activas (turno actual, historial, DataFrames activos por sesión). |

Tanto Streamlit como Telegram mandan su origen en el campo `origen` (`"streamlit"` | `"telegram"`), que el servidor persiste como `prompt_source` en el log.

Nota: los archivos generados (`.docx`, `.xlsx`, gráficos `.png`) se referencian por ruta relativa en la respuesta; Streamlit y Telegram corren en la misma máquina que el servidor y los leen directo del filesystem compartido (`reports/`, `excel_sheets/`) — no hay un endpoint de descarga HTTP.

---

## Estructura del proyecto

```
├── src/
│   ├── server.py               ← Servidor FastAPI persistente — único backend (habla con orquestador, expone /chat)
│   ├── session_store.py       ← Estado de sesión en memoria: historial (últimos 3 turnos), DataFrames activos, TTL
│   ├── enrutador_sesion.py    ← Clasifica cada mensaje en NUEVA_CONSULTA | REFINAMIENTO | SOBRE_DATOS | CONVERSACIONAL
│   ├── orquestador.py         ← Orquestador principal (cadena de agentes, agente_analista, informes)
│   ├── prompt_logger.py       ← Logger de prompts + feedback en JSON (registro centralizado, llamado desde server.py)
│   ├── ingesta_postgres.py    ← Carga TXT → PostgreSQL con dedup MD5 (ventas)
│   └── ingestar_items.py      ← Carga full-sync del maestro de items (CSV → PostgreSQL)
│
├── agents/
│   ├── generador_consultas.md ← Prompt: NL → SQL
│   ├── validador.md           ← Prompt: revisión y validación de SQL
│   ├── redactor_respuesta.md  ← Prompt: JSON → lenguaje natural
│   └── analista.md            ← Prompt: análisis profundo iterativo (agente analista)
│
├── tools/
│   ├── consultar_db.py        ← Ejecutor SQL solo lectura (salida JSON)
│   ├── generar_docx.py        ← Markdown → .docx con formato profesional
│   ├── generar_excel.py       ← Datos tabulares → .xlsx con formato profesional
│   ├── generar_grafico.py     ← Gráficos PNG con matplotlib (tamaño dinámico)
│   └── tool_pandas.py         ← Operaciones Pandas sobre DataFrames en sesión (ruta SOBRE_DATOS)
│
├── skills/
│   ├── consultar_skill/       ← Skill de consulta (esquema, reglas SQL)
│   ├── excel_skill/           ← Skill de exportación a Excel
│   ├── informe_ventas/        ← Skill de informes (bloques A–N, macro→micro)
│   └── graficos_ventas/       ← Skill de decisión de gráficos
│
├── telegram_bot/               ← Bot de Telegram — cliente HTTP de src/server.py
│   ├── main.py                 ← Punto de entrada (polling)
│   ├── handlers.py             ← Manejo de mensajes y comandos (incluye /reset)
│   ├── api_client.py           ← Cliente HTTP hacia /chat, /reset/{id}, /health, /sessions
│   └── config.py               ← Token y SERVER_URL
│
├── ui/
│   ├── streamlit_app.py       ← Interfaz web — cliente HTTP de src/server.py
│   └── db.py                  ← SQLite: historial de informes y gráficos
│
├── prompts/                   ← Logs diarios de uso y feedback, un archivo por día
│                                  compartido por Streamlit y Telegram (gitignored)
├── reports/                   ← Informes .docx y gráficos .png (gitignored)
├── excel_sheets/              ← Archivos .xlsx exportados (gitignored)
├── data_samples/              ← Archivos TXT/CSV fuente (gitignored)
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

# Telegram (solo si se va a correr el bot)
TELEGRA_BOT_TOKEN = tu_token_de_botfather

# URL del servidor FastAPI, usada por Streamlit y por el bot de Telegram
SERVER_URL = http://localhost:8000
```

### 3. Cargar datos en PostgreSQL

```bash
# Ventas — primera carga o carga incremental (solo filas nuevas)
python src/ingesta_postgres.py

# Ventas — recarga completa desde cero
python src/ingesta_postgres.py --full-sync

# Maestro de items (full-sync, tabla pequeña de referencia)
python src/ingestar_items.py
```

### 4. Levantar el servidor FastAPI

Todo el sistema pasa por este proceso — hay que dejarlo corriendo antes de abrir Streamlit o el bot de Telegram:

```bash
uvicorn src.server:app --host 0.0.0.0 --port 8000
```

Verificar que responde: `http://localhost:8000/health`

### 5. Conectar una o ambas interfaces

**Streamlit (web):**

```bash
streamlit run ui/streamlit_app.py
```

Abrir en el navegador: `http://localhost:8501`

**Telegram (opcional):**

```bash
python telegram_bot/main.py
```

Ambas interfaces son clientes independientes del mismo servidor — se puede correr solo una, o las dos al mismo tiempo compartiendo sesiones separadas y el mismo log de prompts.

### 6. (Opcional) Consulta por línea de comandos, sin servidor

```bash
python src/orquestador.py "ventas por departamento en enero"
python src/orquestador.py "genera un informe de ventas completo"
```

Este modo llama al orquestador directamente (sin `session_store`, sin ruteo de sesión ni registro en `prompts/`) — útil para pruebas puntuales.

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

Todas las interacciones, sin importar si vienen de Streamlit o Telegram, pasan por un único punto de registro: `registrar_prompt()` en `src/prompt_logger.py`, llamado una sola vez desde `src/server.py` al final de cada `/chat`. El resultado se acumula en `prompts/prompts_YYYYMMDD.json`:

```json
{
  "id": "uuid4",
  "timestamp": "2026-07-27T10:30:00",
  "pregunta": "analiza por qué cayeron las ventas de caballero",
  "tipo": "consulta",
  "prompt_source": "telegram",
  "modelo_llm": "llama-3.3-70b-versatile",
  "proveedor_llm": "groq",
  "duracion_seg": 6.8,
  "exito": true,
  "archivos_generados": [],
  "sql_queries": [
    { "nombre": "consulta_principal", "sql": "SELECT ... FROM ventas_unificada ..." },
    { "nombre": "analista_ronda_1: ventas de caballero por tienda", "sql": "SELECT ... GROUP BY tienda ..." }
  ],
  "feedback": "bueno",
  "feedback_msg": "Respuesta precisa y rápida"
}
```

Campos clave:

- **`prompt_source`** — de dónde vino la interacción (`"streamlit"` | `"telegram"`), tomado del campo `origen` que cada cliente manda en el request a `/chat`. Es lo que permite distinguir uso por interfaz en el mismo archivo.
- **`sql_queries`** — lista de **todas** las consultas SQL que participaron en la respuesta, no solo la primera. Se arma en el servidor combinando lo que devuelve cada camino del orquestador:
  - `procesar_consulta()` → la consulta principal, más las rondas del agente analista si se activó `/analisis`.
  - `generar_informe()` → una entrada `informe_bloque_<nombre>` por cada bloque del informe (o `..._corregida` si hubo que corregir el SQL generado).
  - `_manejar_sobre_datos()` en `server.py` → vacía si la respuesta salió de Pandas sobre datos ya en memoria, o `sobre_datos_complementaria` si tuvo que hacer una consulta extra a la BD.
- **`feedback` / `feedback_msg`** — se completan después, vía `actualizar_feedback()`, cuando el usuario califica la respuesta desde la UI. Si la entrada original quedó con `sql_queries` vacío, `actualizar_feedback` también puede rellenarlo si se le provee.

Un archivo por día. Los archivos JSON están en `.gitignore` — son datos de usuarios, no se versionan. Las escrituras están protegidas con `FileLock` para que sesiones concurrentes (dos usuarios de Streamlit, o Streamlit + Telegram al mismo tiempo) no se pisen al leer-modificar-escribir el JSON.

---

## Decisiones de diseño

- **Servidor central con estado de sesión en memoria** — `src/server.py` mantiene un `SessionStore` (histórico de los últimos 3 turnos por sesión + DataFrames activos con TTL de 2 horas). Streamlit y Telegram son clientes HTTP sin lógica propia; el modo `python src/orquestador.py <pregunta>` por línea de comandos es la única vía verdaderamente stateless, y no pasa por el servidor ni por el log de prompts.
- **Registro centralizado en un solo punto** — `registrar_prompt()` se llama una única vez, desde `server.py`, después de ejecutar cualquiera de las rutas (`NUEVA_CONSULTA`, `REFINAMIENTO`, `SOBRE_DATOS`, `CONVERSACIONAL`) o tras un error. Así ninguna interfaz puede quedar logueando distinto o duplicando entradas.
- **Trazabilidad completa de SQL** — cada función que puede disparar más de una consulta (`agente_analista`, `generar_informe`, la consulta complementaria de `SOBRE_DATOS`) devuelve su propia lista de `sql_queries`, que se combinan en `server.py` antes de loguear. Ninguna consulta se descarta silenciosamente.
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
fastapi>=0.111
uvicorn>=0.29
requests>=2.31
python-telegram-bot>=20.0
filelock>=3.32
```

Python 3.12+ recomendado.
