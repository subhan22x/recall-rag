# Recall RAG

Recall RAG is an AI assistant for automotive-parts and vehicle-recall questions. It combines public NHTSA recall information with operational data and gives users answers they can inspect instead of giving the model unrestricted access to documents or a database.

The system has four main parts:

1. **Document search** — find the recall notices and guidance that support an answer.
2. **dbt data models** — clean and combine recall, demand, inventory, and transfer data.
3. **Text-to-SQL** — turn a business question into validated, read-only SQL against approved models.
4. **MCP tools** — expose those capabilities through a small set of read-only tools for an LLM or external agent.

The intended operational question is concrete:

> “Which critical brake recalls affect parts Dallas may run out of, what does NHTSA say the safety consequence is, and can Memphis cover the shortage without falling below safety stock?”

## What is implemented

This is a working local application. The main pieces are:

| Area | What runs locally |
| --- | --- |
| Ingestion | NHTSA recall API refresh and versioned official NHTSA PDFs with page metadata |
| Retrieval | 500-token chunks, PostgreSQL full-text search, pgvector, hybrid ranking, and citation limits |
| Embeddings | OpenAI `text-embedding-3-small`, with a deterministic development fallback |
| Chat | LangGraph routing across document search, analytics, and source-status checks |
| Analytics | dbt Core models and tests for recall intelligence, part demand, stockout risk, and transfer candidates |
| Text-to-SQL | YAML semantic layer, typed intent, compiled read-only SQL, AST validation, and bounded results |
| MCP | FastMCP over stdio and local Streamable HTTP with three read-only tools |
| Evaluations | Retrieval, analytics, semantic-parser, and MCP protocol checks |

## MCP integration

MCP (Model Context Protocol) is the interface an external AI client uses to call Recall RAG. Instead of giving Claude, Cursor, or Codex a database connection or access to the document directory, the MCP server exposes three specific operations with typed inputs and bounded outputs.

### Available tools

- `search_recall_documents` searches the indexed NHTSA documents. It accepts a natural-language question, an optional campaign ID, and a small result limit. It returns ranked passages with source titles, pages, excerpts, and citation IDs.
- `query_recall_analytics` answers questions about the dbt data models. It accepts a business question such as “Which warehouse has the most shortage units?” and returns rows, the semantic terms used, the compiled SQL, and freshness information. It never accepts raw SQL from the client.
- `get_data_status` reports whether the document index and analytics models are current, along with source counts, model versions, and the latest evaluation status.

### How a request works

1. An external client connects to the MCP server and discovers the three tool schemas.
2. The client chooses a tool based on the user’s question.
3. The server validates the inputs, applies limits, and runs either hybrid document retrieval or the governed analytics compiler.
4. The server returns a small structured result. For document searches, every passage includes the information needed to cite the source.
5. The client writes the final answer using those results. It never receives unrestricted database credentials, filesystem paths, or the full document corpus.

A question that needs both kinds of evidence can call both search and analytics. For example, the document tool can provide NHTSA’s stated safety consequence while the analytics tool calculates which warehouse has a shortage. The answer can then keep those two sources separate instead of treating a generated statement as a fact.

### Safety and limits

The MCP server is read-only. It cannot update inventory, trigger a refresh, execute arbitrary SQL, or access local files. Analytics queries are compiled from an approved YAML semantic layer, restricted to known metrics, dimensions, filters, and dbt models, then validated before execution. Retrieval results and analytics rows are capped. Tool calls record only safe operational metadata such as client, tool, status, latency, and result count.

The server supports stdio for desktop clients and local Streamable HTTP for integration tests. The MCP Access screen shows the available tools, connection status, recent calls, and client configuration examples. The protocol-level test suite checks tool discovery, schemas, retrieval results, SQL restrictions, row limits, refusal behavior, and freshness reporting.

## Documentation

- [Architecture and system boundaries](docs/architecture.md)
- [RAG ingestion, retrieval, citations, and benchmarks](docs/rag-pipeline.md)
- [ELT, dbt models, semantic layer, and text-to-SQL](docs/elt-and-analytics.md)
- [Agent and MCP tool contract](docs/mcp-agent.md)
- [Repository guide: current code versus planned modules](docs/codebase-guide.md)
- [Implementation roadmap and acceptance criteria](docs/implementation-roadmap.md)

## Run locally

Start the isolated pgvector database (port `5433` avoids interfering with a conventional local PostgreSQL server), install dependencies, and launch both services:

```bash
docker compose up -d postgres
python -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
npm install
npm run dev
```

In another terminal:

```bash
.venv/bin/python backend/server.py
```

The frontend is available at `http://localhost:5173`; the API listens at `http://127.0.0.1:8010`.

To enable the model and real embeddings, create `backend/.env` from `backend/.env.example` and set `OPENAI_API_KEY`. The fallback mode remains useful for inspecting the deterministic retrieval, dbt, semantic compiler, and interface without a key.

## API surface

- `GET /api/health` — source, dbt, and model-mode status
- `POST /api/chat` — routed grounded answer, citations, compiled analytics evidence, and tool trace
- `POST /api/refresh` — refresh NHTSA data, rebuild dbt marts, and reindex documents
- `GET /api/pipelines` — workflow canvas nodes
- `GET /api/semantic-layer` and `POST /api/analytics/query` — public semantic contract and governed analytics results
- `GET /api/evaluations` — retrieval and analytics benchmark scores
- `GET /api/documents/{version_id}/file` — stored PDF used by the evidence viewer
- `GET /api/mcp/tools` — catalogue of the three actual MCP tools
- `GET /api/mcp/status` and `GET /api/mcp/calls` — live local MCP status, client setup details, and sanitized tool-call audit


