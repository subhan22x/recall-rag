# Codebase Guide

## Current repository map

```text
.
├── src/
│   ├── App.tsx                 # Current React UI: assistant + pipeline canvas
│   ├── main.tsx                # React bootstrap
│   └── styles.css              # Existing visual system
├── backend/
│   ├── app/
│   │   ├── main.py             # FastAPI routes and lifecycle bootstrap
│   │   ├── agent.py            # LangGraph router and response composer
│   │   ├── analytics.py        # Typed semantic intent → safe SQL compiler
│   │   ├── db.py               # PostgreSQL / pgvector schema and connections
│   │   ├── documents.py        # Versioned PDF fetch, parse, chunks, anchors
│   │   ├── evals.py            # Retrieval and analytics benchmark runners
│   │   ├── ingest.py           # NHTSA vehicle-recall API refresh
│   │   ├── mcp_server.py       # FastMCP read-only tool server
│   │   ├── rag.py              # Embeddings, FTS/vector search, RRF
│   │   └── seed.py             # Explicitly synthetic distributor fixtures
│   ├── dbt/                    # dbt project: sources, stages, marts, tests
│   ├── semantic_layer.yml      # Enforced business contract
│   ├── sources.yml             # Curated official PDF registry
│   └── server.py               # Uvicorn launcher
├── README.md
└── docs/                       # Architecture and implementation contract
```

## What the code does

### Frontend: `src/App.tsx`

- Renders the three-pane assistant view, dynamic citations/data evidence/activity, and a draggable presentation-only workflow canvas.
- Calls the FastAPI service for health, chat, refresh, evaluations, pipeline metadata, and semantic information.
- Embeds an official stored PDF in the evidence pane at the cited page; API-record citations use a rendered excerpt. The backend retains PDF word bounding boxes for a later pdf.js overlay, but this compact implementation does not draw that overlay yet.
- Falls back to clearly marked static/demo content only if the local API is offline.

### Backend: `backend/app/`

- Bootstraps PostgreSQL with the `vector` extension and a separate read-only analytics role.
- Refreshes a small NHTSA recall set, versions an official NHTSA PDF by checksum, parses every PDF page, and stores chunk/page/word-coordinate relationships.
- Runs independent PostgreSQL lexical and pgvector candidate retrieval, fuses result ranks with RRF, boosts exact campaign IDs, and caps results per document.
- Builds 22 dbt models and eight dbt data tests. The API only reads approved marts.
- Loads `semantic_layer.yml`; it accepts a typed semantic intent, compiles parameterized `SELECT` SQL, parses the SQL AST, and executes it in a read-only transaction with a row limit and timeout.
- Routes document, analytics, combined, and status questions through a compact LangGraph. With `OPENAI_API_KEY`, the model creates JSON-schema-constrained intents and answer drafts using Responses API; otherwise a labelled fallback supports local inspection.
- Exposes the same domain services through three actual FastMCP tools, not a REST simulation.

## Module responsibilities

```text
backend/
├── app/
│   ├── main.py                 # FastAPI app and HTTP routes
│   ├── config.py               # Typed environment configuration
│   ├── db.py                   # Postgres connection/session helpers
│   ├── schemas.py              # Pydantic HTTP/domain contracts
│   ├── ingest/
│   │   ├── nhtsa.py            # Structured API ingestion
│   │   ├── documents.py        # Fetch/version/checksum/PDF parsing
│   │   └── refresh.py          # Incremental refresh coordinator
│   ├── rag/
│   │   ├── chunking.py         # Page-aware chunk policy
│   │   ├── embeddings.py       # Provider adapter and model versioning
│   │   ├── retrieval.py        # FTS/vector/RRF and filter application
│   │   └── citations.py        # Citation payload and anchor validation
│   ├── analytics/
│   │   ├── semantic_layer.py   # Load/validate YAML contract
│   │   ├── semantic_query.py   # Typed query schema
│   │   ├── compiler.py         # Deterministic SQL compiler
│   │   └── validator.py        # sqlglot safety checks and execution guard
│   ├── agent/
│   │   ├── graph.py            # Small LangGraph route
│   │   ├── prompts.py          # Versioned prompt text
│   │   └── response.py         # Structured response/citation verification
│   ├── mcp/
│   │   └── server.py           # Three read-only MCP tools
│   └── evals/
│       ├── rag_runner.py       # Retrieval metrics
│       └── analytics_runner.py # Semantic/compiler/result checks
├── dbt/
│   ├── models/
│   ├── macros/
│   └── tests/
├── data/
│   ├── synthetic/              # Explicitly labelled generated operational inputs
│   └── evals/                  # Reviewed JSONL test suites
└── semantic_layer.yml
```

The modules are organized around verifiable boundaries: data ingestion, retrieval, analytics compilation, agent orchestration, tool access, and evaluations can be tested independently.

## HTTP API contract

| Route | Responsibility |
| --- | --- |
| `POST /api/chat` | Invoke the graph and return structured answer, citations, analytics evidence, warnings, and tool trace. |
| `GET /api/documents/{version_id}/pages/{page}` | Return approved page text/anchor metadata for the right pane. |
| `GET /api/documents/{version_id}/file` | Serve an approved stored PDF/document for the viewer, not an arbitrary path. |
| `GET /api/pipelines` | Return dbt manifest-derived lineage and recent run metadata for the node canvas. |
| `GET /api/semantic-layer` | Return the parsed, active semantic contract. |
| `GET /api/evaluations` | Return latest saved benchmark results and configuration. |
| `GET /api/data-status` | Return per-source freshness and model/index versions. |

The MCP server is a separate protocol endpoint/process as appropriate for the SDK. HTTP routes should call the same domain services, not reimplement tool logic.

## Frontend implementation notes

- `/api/chat` feeds the evidence and activity panels directly.
- The right pane shows an iframe for an official PDF, currently positioned to the cited page. A visible excerpt identifies the precise retrieved passage; backend `bbox_json` is available for a future PDF canvas highlight.
- The data tab displays only the generated SQL, semantic terms, and rows sent by the governed analytics service.
- Canvas drag state changes only client presentation. It cannot edit a dbt model, tool definition, or database workflow.
- Evaluation cards use the latest API-calculated retrieval and analytics metrics.

## Code-quality rules for the implementation

- Add unit tests around chunk boundaries, RRF ordering, semantic validation, SQL rejection, transfer constraints, and citation validation.
- Keep model/provider code behind an interface; do not mix prompts into route handlers.
- Use typed schemas at every HTTP/MCP boundary.
- Log request IDs and safe operational metadata; never log secrets or full private source documents.
- Migrate schema changes deliberately and keep dbt tests executable locally.
- Treat warnings, no-answer responses, and stale data as first-class UI states rather than errors hidden in logs.
