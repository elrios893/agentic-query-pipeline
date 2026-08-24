# Stack tecnológico, desarrollo local y por qué no LangChain (aún)

Este documento complementa al `README.md` (que detalla endpoints, formato de logs,
bloques de informe, etc.) con cuatro cosas puntuales: el stack completo, cómo
levantar el entorno en local, el flujo de una consulta explicado paso a paso, y
la justificación de no usar LangChain por ahora.

---

## 1. Stack tecnológico

| Capa | Tecnología | Dónde vive |
|---|---|---|
| API / orquestación | Python 3.12 + **FastAPI**, **Uvicorn**, **Pydantic** | `src/server.py` |
| Interfaz web | **Streamlit** | `ui/streamlit_app.py` |
| Interfaz chat | **python-telegram-bot** (v20+, async) | `telegram_bot/` |
| Base de datos | **PostgreSQL** vía `psycopg2-binary` — SQL crudo, sin ORM | `tools/consultar_db.py` |
| LLM | `groq`, `openai` (usado como cliente API-compatible para Cerebras), `google-genai` — proveedor activo por `.env` | `src/orquestador.py` |
| Generación de salidas | `openpyxl` (Excel), `python-docx` (Word), `matplotlib` (gráficos PNG) | `tools/generar_excel.py`, `tools/generar_docx.py`, `tools/generar_grafico.py` |
| Datos en memoria | `pandas`, `numpy` | `tools/tool_pandas.py`, `src/session_store.py` |
| Persistencia de logs/sesión | JSON en disco (`prompts/`) + `FileLock`, `SQLite` (historial de UI en `ui/db.py`) | — |
| Utilidades | `python-dotenv`, `requests` | transversal |

No hay ORM, no hay framework de orquestación de agentes (LangChain, LlamaIndex,
CrewAI, etc.) y no hay vectorstore — todo el "agente" es funciones Python
explícitas en `src/orquestador.py`, ver sección 4.

El proveedor de LLM se elige con una sola variable de entorno
(`LLM_PROVIDER=groq|cerebras|gemini`), pero el enrutador de intención
(`src/enrutador_sesion.py`) siempre usa Groq (`openai/gpt-oss-20b` por defecto),
independiente del proveedor principal configurado.

---

## 2. Cómo montar el desarrollo en local

Versión resumida — la versión completa (incluyendo ingesta de datos, exportación
a Excel y estructura de bloques de informe) está en `README.md`.

### 2.1 Dependencias

```bash
pip install -r requirements.txt
```

### 2.2 Variables de entorno

```bash
cp .env.example .env
```

Editar `.env` con credenciales reales. Mínimo para correr consultas:

```env
LLM_PROVIDER = groq
GROQ_API_KEY = tu_api_key
GROQ_MODEL = openai/gpt-oss-120b
GROQ_MODEL_INFERENCE = openai/gpt-oss-20b

DB_HOST = localhost
DB_PORT = 5432
DB_NAME = tu_base
DB_USER = postgres
DB_PASSWORD = tu_password

SERVER_URL = http://localhost:8000
```

Si vas a correr el bot de Telegram, agrega `TELEGRA_BOT_TOKEN` (el nombre de la
variable está así, sin la "M", tanto en `.env.example` como en
`telegram_bot/config.py` — mantenerlo consistente si se toca).

### 2.3 Cargar datos en PostgreSQL

```bash
python src/ingesta_postgres.py --2026            # incremental (dedup MD5)
python src/ingesta_postgres.py --2026 --full-sync # recarga completa
python src/ingestar_items.py                      # maestro de items
```

### 2.4 Levantar el backend

Todo pasa por este proceso — debe quedar corriendo antes de abrir cualquier
interfaz:

```bash
uvicorn src.server:app --host 0.0.0.0 --port 8000
```

Verificar: `http://localhost:8000/health`

### 2.5 Conectar una o ambas interfaces

```bash
streamlit run ui/streamlit_app.py       # web, http://localhost:8501
python telegram_bot/main.py             # bot (opcional)
```

Ambas son clientes HTTP independientes del mismo servidor: se puede correr solo
una, o las dos a la vez, compartiendo el mismo log de prompts pero con sesiones
separadas.

### 2.6 Modo sin servidor (debug puntual)

```bash
python src/orquestador.py "ventas por departamento en enero"
```

Llama al orquestador directo, sin `session_store`, sin ruteo, sin quedar
registrado en `prompts/`. Útil para probar un cambio en un prompt sin levantar
todo el stack.

---

## 3. Flujo de una consulta, paso a paso

```
Streamlit / Telegram
        │  POST /chat  {session_id, pregunta, origen}
        ▼
src/server.py (FastAPI) ── único backend
        │
        │  1. Recupera/crea sesión en session_store.py
        │     (historial últimos 3 turnos, DataFrames activos, TTL 10 min)
        ▼
src/enrutador_sesion.py :: clasificar()
        │  LLM ligero (Groq) con JSON Schema estructurado.
        │  Un regex filtra saludos antes de gastar la llamada.
        │  Clasifica en 1 de 4 rutas (y detecta sub_preguntas si el
        │  mensaje pide varias tablas distintas en un mismo turno).
        │
   ┌────┴─────────┬──────────────┬─────────────────┐
   ▼               ▼              ▼                 ▼
NUEVA_CONSULTA  REFINAMIENTO   SOBRE_DATOS      CONVERSACIONAL
   │               │              │                 │
   │  (misma ruta) │        tools/tool_pandas.py   LLM + historial,
   │               │        sobre un DataFrame      sin BD. Puede
   │               │        ya en memoria           escalar a SQL
   ▼               ▼        (sin tocar Postgres)    si le falta un dato
src/orquestador.py :: procesar_consulta()
        │
        │  1. generar_sql_y_validar()
        │     agents/generador_consultas.md → LLM genera SQL
        │     agents/validador.md           → LLM valida (máx 3 intentos)
        │
        │  2. ejecutar_consulta()
        │     tools/consultar_db.py (subprocess) → psycopg2 → PostgreSQL
        │     reintento único si Postgres da error real de ejecución
        │
        │  3. Post-proceso condicional:
        │     - tools/generar_excel.py  si >100 filas o se pide explícito
        │     - tools/generar_grafico.py si hay intención de gráfico
        │     - agente_analista()        si se pide /analisis o se detecta
        │                                 intención de análisis profundo
        │                                 (rondas extra de SQL + LLM)
        │
        ▼
   agents/redactor_respuesta.md → LLM redacta la respuesta final en español
        │
        ▼
src/server.py
        │  2. Guarda el turno en session_store.py
        │  3. registrar_prompt() → prompts/prompts_YYYYMMDD.json
        │     (único punto de log, protegido con FileLock)
        ▼
   ChatResponse → Streamlit / Telegram (solo renderizan)
```

Puntos clave del diseño:

- **Streamlit y Telegram no tienen lógica de negocio.** Arman el request HTTP y
  pintan la respuesta. Toda decisión vive en el servidor, así ambas interfaces
  quedan sincronizadas sin esfuerzo.
- **El pipeline SQL es lineal y explícito**: generar → validar → ejecutar →
  (opcional analista) → redactar. No hay un grafo de decisiones dinámico — el
  siguiente paso siempre se sabe de antemano.
- **Reintentos con backoff exponencial** (`llamar_llm()`, 10s/20s/40s) cubren
  rate limits y timeouts del proveedor LLM sin intervención manual.
- Detalle completo del pipeline de informes (`.docx`) y del agente analista
  está en `README.md`, secciones "Pipeline de informe" y "Agente analista".

---

## 4. Por qué no usar LangChain (aún)

### ¿Para qué sirve LangChain?

Es una capa de abstracción sobre LLMs que estandariza piezas que se repiten en
casi todo proyecto de IA: interfaz uniforme entre providers, memoria
conversacional, retrieval sobre vectorstores, parsing de outputs estructurados,
y agentes que deciden dinámicamente qué tool invocar. Su valor aparece cuando
hay *muchas* piezas intercambiables (varios LLMs, varios vectorstores, varias
tools) y no se quiere reescribir el pegamento cada vez.

### Qué mejoraría si lo adoptáramos hoy

- **Retry/backoff**: `llamar_llm()` lo hace a mano; LangChain lo trae de fábrica.
- **Parsing estructurado**: el JSON Schema manual de `enrutador_sesion.py`
  (~60 líneas) se acortaría con un output parser.
- **Observabilidad**: con LangSmith (el companion de LangChain) se obtienen
  trazas de cada llamada, tokens y latencia sin instrumentar nada a mano — esto
  es una mejora real que hoy no tenemos.

### Qué NO mejoraría

- **El pipeline SQL generar→validar→ejecutar**: ya es lineal y explícito.
  Envolverlo en un `Chain`/`AgentExecutor` lo hace más indirecto, no más
  correcto — el valor de LangChain es para grafos *no lineales* de decisiones,
  y este pipeline ya sabe siempre cuál es el siguiente paso.
- **Multi-provider**: el `if/elif` de 15 líneas en `orquestador.py` hace lo
  mismo que la abstracción de providers de LangChain, para solo 3 proveedores
  fijos.
- **RAG**: no existe hoy — no hay retrieval semántico ni vectorstore, que es el
  caso de uso estrella de LangChain.

### Qué empeoraría

- **Debuggability**: hoy un error se seguimos línea por línea en
  `orquestador.py`. Con `AgentExecutor`/`Chain` el stacktrace pasa por varias
  capas de LangChain antes de llegar al código propio.
- **Superficie de dependencias**: LangChain tiene historial de romper
  compatibilidad entre versiones menores — riesgo innecesario para un pipeline
  que ya funciona.
- **Costo de reescritura sin beneficio proporcional**: tocaría el corazón del
  sistema (`orquestador.py`, `enrutador_sesion.py`, `session_store.py`) para
  terminar con el mismo comportamiento, ahora dependiente de un framework
  externo.

### Cuándo sí tendría sentido

- **RAG real**: si en algún momento se necesita buscar en documentos no
  estructurados (políticas, manuales, PDFs) en vez de solo SQL sobre Postgres.
- **Decisiones no lineales**: si el flujo deja de ser un enrutador de 4 rutas
  fijas y pasa a un LLM decidiendo dinámicamente entre 6-8 tools por turno.
- **Explosión de providers**: si se necesita swap dinámico de modelo por
  usuario/tenant/costo en tiempo real, más allá de los 3 providers fijos de hoy.

### Si llega ese momento: todo el framework o gradual

Siempre gradual. LangChain está empaquetado para eso — `langchain-core`,
integraciones de provider, `langgraph` y `langsmith` son paquetes separados.
Orden natural si aplica:

1. **`langsmith`** primero — observabilidad, cero cambio de arquitectura.
2. Si llega RAG, **un solo vectorstore integration** sobre el pipeline actual,
   sin tocar el resto.
3. Solo si el enrutador de 4 rutas se queda corto, considerar **`langgraph`**
   para esa pieza puntual — no para todo el orquestador.

Adoptar el framework completo de una sola vez, sin un dolor concreto que
resolver, es el tipo de reescritura riesgosa que este proyecto no necesita hoy.
