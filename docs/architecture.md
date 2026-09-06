# Architecture

## Purpose

Recall RAG is a single-assistant system for an automotive aftermarket distributor. Its job is to answer questions spanning two different kinds of evidence:

- **Unstructured sources:** NHTSA recall notices and safety guidance, where wording, context, and source citations matter.
- **Structured sources:** recall exposure, parts fitment, sales, inventory, purchase orders, warehouse routes, and safety-stock rules, where the answer needs deterministic calculations.

The assistant does not receive an entire folder or unrestricted database credentials. It receives a narrow set of tool results and must cite the evidence it uses.

```text
                 Public NHTSA API / official documents
                              |
                  ingest, version, parse, chunk, embed
                              |
               PostgreSQL: document metadata + pgvector + FTS
                              |
                         MCP search tool
                              |
User question -> LangGraph route -> LLM -> grounded response -> citation panel
                              |
                       MCP analytics tool
                              |
           semantic query -> validated compiler -> dbt marts -> PostgreSQL
                              ^
                 NHTSA structured feed + synthetic distributor data
                              |
                            ELT + dbt
```

## System responsibilities

| Layer | Responsibility | Must not do |
| --- | --- | --- |
| Ingestion | Fetch sources, retain source/version metadata, normalize structured rows | Silently overwrite the original source or invent freshness timestamps |
| RAG | Retrieve page-aware source passages using hybrid search | Calculate business metrics or synthesize unsupported facts |
| dbt/ELT | Turn raw structured data into tested analytical marts | Let the model define business logic at query time |
| Semantic layer | Name approved metrics, dimensions, joins, and their meaning | Become an executable arbitrary-SQL prompt |
| Text-to-SQL compiler | Convert a validated semantic intent into one read-only SQL statement | Execute model-written SQL verbatim |
| MCP server | Publish a small typed and logged access surface | Expose a database connection, filesystem, or write methods |
| LLM | Select the relevant read-only tool(s) and explain their results | Decide source-of-truth calculations or fabricate citations |
| Evaluation runner | Measure retrieval and analytical correctness after relevant changes | Treat a successful HTTP response as evidence of quality |

## Target technology choices

| Concern | Proposed technology | Reason |
| --- | --- | --- |
| API and orchestration | Python, FastAPI, LangGraph | One typed backend language; LangGraph makes the small routing flow explicit and testable. |
| Answer model | Configured LLM through the provider SDK | The model is used only for classification, tool selection, and response writing. Provider and model name remain environment configuration, not business logic. |
| Database | PostgreSQL with pgvector | Holds raw/modeled facts, document metadata, vectors, and full-text search in one inspectable store. |
| RAG embeddings | A production embedding provider configured through environment variables | Replaces the current hash-vector placeholder with semantic representations and a documented model/version. |
| Transformations | dbt Core + PostgreSQL adapter | SQL models, lineage, documentation, tests, and reproducible marts. |
| Tool protocol | Official MCP Python SDK | Typed input/output schemas give the agent a controlled, inspectable boundary. |
| PDF extraction/citation anchors | PyMuPDF | Extracts page text and retains page/bounding-box coordinates for the viewer. |
| SQL safety | Pydantic and sqlglot | Validate the semantic request before compilation and the compiled SQL before execution. |
| Frontend | Existing React + TypeScript + Vite UI | Already provides a useful three-panel assistant and node-canvas starting point. |

Exact package versions belong in a lockfile when implementation begins. Secrets, model keys, and database URLs belong only in environment variables.

## Request flow

1. The user asks a question in the chat panel.
2. A small LangGraph route classifies it as `documents`, `analytics`, `combined`, or `clarify`.
3. The assistant invokes only the allowed MCP tool(s): `search_recall_documents`, `query_recall_analytics`, and/or `get_data_status`.
4. RAG returns ranked chunks with source, version, page, excerpt, and retrieval metadata. Analytics returns validated query data, the canonical metric/dimension names, compiled SQL, freshness, and a row limit.
5. The LLM writes a concise answer using only those tool outputs. A response validator rejects citation markers that do not map to a returned source ID.
6. The frontend renders answer text in the center pane and source cards in the right pane. Selecting a PDF citation opens the exact page and highlights the cited bounding box; non-PDF documents show the recorded excerpt and source metadata.
7. The interaction log stores non-sensitive observability fields: question class, tool names, retrieval configuration, model/version identifiers, citation IDs, latency, and outcome. It does not store credentials or execute writes.

## Data boundaries and labels

The demo has two data classes that must remain visibly distinct in the UI and documentation:

| Class | Source | Label |
| --- | --- | --- |
| Public safety evidence | NHTSA recalls API and official NHTSA documents | `public source` |
| Internal-like operational data | Generated, fictional inventory/fitment/sales/route records | `synthetic distributor data` |

The latter exists only to demonstrate transformations and governed analytics. It must never be represented as FMP, real customer, supplier, warehouse, or sales data.

## Explicit non-goals

- No n8n or generic workflow-automation product.
- No multi-agent design; one assistant has a bounded route and three read-only tools.
- No write actions, inventory transfers, ERP updates, approvals, or background decision execution.
- No general chat over the local filesystem.
- No agent-generated arbitrary SQL.
- No claim that a synthetic demand estimate is a real forecast or safety recommendation.
