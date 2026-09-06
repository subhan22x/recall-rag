# Recall RAG

Recall RAG is a portfolio implementation of a **grounded operations assistant** for automotive-parts and recall intelligence. It uses public NHTSA recall data alongside clearly labelled synthetic distributor data to demonstrate how an AI assistant can answer operational questions without being given unrestricted access to documents or a database.

The project deliberately separates four concerns:

1. **RAG for unstructured evidence** — retrieve the specific recall notices and guidance that support an answer.
2. **dbt/ELT for structured business facts** — clean and model recall, demand, inventory, and transfer data before it reaches analytics.
3. **Governed text-to-SQL** — turn a constrained business question into validated, read-only SQL against approved dbt marts.
4. **MCP tools and an LLM** — let the model choose from a small set of read-only capabilities, then compose an answer from their structured results.

The intended operational question is concrete:

> “Which critical brake recalls affect parts Dallas may run out of, what does NHTSA say the safety consequence is, and can Memphis cover the shortage without falling below safety stock?”

## Implemented scope

This is a working local implementation, not a production deployment. It keeps the operational surface intentionally small and inspectable.

| Area | What runs locally |
| --- | --- |
| Ingestion | NHTSA vehicle-recall API refresh plus a versioned official NHTSA PDF stored locally with checksum, metadata, and page anchors |
| RAG | 500-word chunks with 75-word overlap, PostgreSQL FTS, pgvector, independent candidate sets, reciprocal-rank fusion, and per-document result caps |
| Embeddings | OpenAI `text-embedding-3-small` when `OPENAI_API_KEY` is set; clearly labelled deterministic development embeddings otherwise |
| Chat | LangGraph routing over retrieval, constrained analytics, and a read-only status path; OpenAI Responses structured output when a key is configured, conservative fallback otherwise |
| Analytics | dbt Core build with 22 models and 8 data tests across recall intelligence, part demand, stockout risk, and transfer candidates |
| Text-to-SQL | YAML semantic layer → typed intent → compiler-generated, read-only SQL → SQL AST validation → bounded results |
| MCP | A real FastMCP server over stdio and local Streamable HTTP, exposing only `search_recall_documents`, `query_recall_analytics`, and `get_data_status` |
| Evals | Six retrieval fixtures, five analytics fixtures, adversarial semantic-parser checks, and a protocol-level MCP stress suite with twenty-five checks |

The public recall evidence is real; distributor inventory, sales, warehouse, and route records are deliberately marked as synthetic demo data.

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

## MCP verification

With the API running, this exercises the local Streamable HTTP server as an external MCP client. It verifies tool discovery, read-only schemas, retrieval, analytical correctness, limits, refusals, and data status:

```bash
.venv/bin/python backend/scripts/mcp_stress_test.py
```

## Scope boundaries

This project intentionally does **not** include n8n, multi-agent orchestration, application user authentication, arbitrary SQL execution, write-capable inventory tools, approval workflows, or deployment infrastructure. The local MCP HTTP surface is loopback-only by default (or bearer-protected when explicitly configured). The goal is a focused, inspectable demonstration of grounded retrieval, governed analytical access, and evaluations.
