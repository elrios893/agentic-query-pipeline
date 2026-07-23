# Sales Data Pipeline — Natural Language Query System

Extract, analyze, and report on retail sales data from TXT files using a natural-language interface powered by LLMs.

## How it works

```
┌──────────────┐     ┌──────────────────────┐     ┌───────────────┐
│  TXT Source   │────>│   PostgreSQL DB      │────>│   LLM Oracle  │
│ (tab-sep CSV) │     │  (ingesta_postgres)  │     │  (Groq API)   │
└──────────────┘     └──────────────────────┘     └───────┬───────┘
                                                           │
                    ┌──────────────────────────────────────┤
                    │                                      │
                    ▼                                      ▼
         ┌──────────────────┐                  ┌──────────────────┐
         │ Normal Query     │                  │ Report (.docx)   │
         │ (stdout answer)  │                  │ (reports/ dir)   │
         └──────────────────┘                  └──────────────────┘
```

## Query flow

```
User question
     │
     ▼
┌─────────────────────┐
│ generador_consultas │─── LLM translates question → SQL
└─────────┬───────────┘
          │ SQL
          ▼
┌─────────────────────┐
│     validador       │─── LLM reviews SQL (max 3 attempts)
└─────────┬───────────┘
          │ approved SQL
          ▼
┌─────────────────────┐
│  consultar_db tool  │─── Executes SELECT on PostgreSQL
└─────────┬───────────┘
          │ JSON result
          ▼
┌─────────────────────┐
│ redactor_respuesta  │─── LLM formats answer in natural language
└─────────┬───────────┘
          │
          ▼
     User sees answer
```

If the question contains keywords like "report", "document", "word", the orchestrator
auto-detects a report intent and runs an extended pipeline:

1. LLM plans the required SQL queries based on the user's request and available report blocks
2. Each query goes through validation and execution
3. LLM assembles the results into a Markdown report using only relevant blocks
4. The report is converted to `.docx` and saved in `reports/`

## Architecture

```
├── src/
│   ├── orquestador.py         ← Main pipeline (LLM chain orchestrator)
│   └── ingesta_postgres.py    ← Load TXT data into PostgreSQL
│
├── agents/
│   ├── generador_consultas.md ← System prompt: NL → SQL translation
│   ├── validador.md           ← System prompt: SQL review & validation
│   └── redactor_respuesta.md  ← System prompt: JSON → natural language
│
├── tools/
│   ├── consultar_db.py        ← Read-only SQL executor (JSON output)
│   └── generar_docx.py        ← Markdown → .docx converter
│
├── skills/
│   ├── consultar_skill/       ← PostgreSQL query skill (schema, rules)
│   └── informe_ventas/       ← Sales report skill (modular blocks)
│
├── data_samples/              ← TXT source files (gitignored)
├── reports/                   ← Generated .docx reports (gitignored)
├── foragents/                 ← Agent context files (gitignored)
├── .env.example               ← Template for required env vars
└── .env                       ← API keys & DB credentials (gitignored)
```

## Quick start

```bash
# 1. Install dependencies
pip install pandas psycopg2-binary python-dotenv groq

# 2. Configure .env (copy from .env.example)
cp .env.example .env
#    Then edit .env with your credentials

# 3. Load data into PostgreSQL
python src/ingesta_postgres.py          # incremental
python src/ingesta_postgres.py --full-sync  # full reload

# 4. Ask a question
python src/orquestador.py "how many units sold in January?"

# 5. Generate a report
python src/orquestador.py "generate a sales report by department"
```

## Key design decisions

- **No conversation memory** — each call is stateless; the LLM gets full context every time
- **Server-side hash dedup** — `md5(COALESCE(col::text, '') || '|' || ...)` computed in PostgreSQL for consistent row identity
- **Modular report blocks** — the report skill provides independent building blocks (metrics, geography, product, stores, alerts) that the LLM selects from based on the user request
- **Read-only enforcement** — SQL executor rejects any non-SELECT statement at the keyword level
