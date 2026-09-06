# Implementation Roadmap

This roadmap is deliberately limited to the agreed project scope: live RAG over public documents, dbt/ELT for structured data, governed text-to-SQL, three read-only MCP tools, and evaluations. It does not add n8n, a multi-agent system, write workflows, or deployment work. The local HTTP MCP surface is loopback-only by default, with an optional bearer token for an explicitly configured remote deployment.

## Milestone 0 — Establish the target contract

**Build**

- Add a Python dependency/lock strategy and environment template without committing secrets.
- Introduce PostgreSQL with pgvector for the target implementation; keep the existing SQLite demo separate or retire it once parity exists.
- Add typed Pydantic domain schemas for `Citation`, `SemanticQuery`, `AnalyticsResult`, `DataStatus`, and final `AssistantResponse`.
- Create the checked-in source registry, semantic YAML contract, synthetic-data README, and reviewed JSONL evaluation fixtures.

**Done when**

- The project starts with documented environment variables and no embedded credentials.
- The semantic contract loads from YAML and fails validation on invalid metric/dimension references.
- All synthetic data is visibly marked synthetic and its generation is reproducible.

## Milestone 1 — Build the NHTSA document and structured-data ingestion paths

**Build**

- Ingest NHTSA recall API records into raw structured tables with source timestamps and stable keys.
- Download/parse a small curated official PDF set with checksum, version, page text, and page-coordinate metadata.
- Implement idempotent refresh: changed sources create a new version; unchanged sources do not trigger reprocessing.
- Load the synthetic distributor data into raw tables with run identifiers and freshness metadata.

**Done when**

- Two runs without source changes do not duplicate rows/chunks.
- A changed PDF or recall record creates a new version while preserving the old one.
- The status endpoint reports last successful fetch, record counts, and stale/healthy state for each source.

## Milestone 2 — Implement deterministic RAG

**Build**

- Page-aware parsing and 500-token/75-token-overlap chunking.
- Real embeddings with stored provider model/version metadata.
- Postgres full-text and pgvector indexes.
- Independent lexical and vector retrieval, filter normalization, RRF fusion, campaign-ID boost, and per-document diversity cap.
- Citation payloads that contain source/version/page/excerpt/anchor fields.

**Done when**

- Search returns only active source versions by default and supports campaign/component/document filters.
- A PDF citation resolves to a page and stored highlight coordinates.
- Hybrid retrieval is demonstrably run as two retrievals plus RRF, not a single mixed score disguised as hybrid search.
- No-result retrieval returns an explicit no-answer state.

## Milestone 3 — Build the three dbt ELT pipelines

**Build**

- Pipeline 1: `mart_recall_intelligence`.
- Pipeline 2: `mart_recall_part_demand` using the versioned component-to-product crosswalk and documented scenario formula.
- Pipeline 3: `mart_stockout_risk` and `mart_transfer_candidates` using inventory, demand, routes, and safety stock.
- Source freshness, documentation, relationships, accepted-value, uniqueness, and calculation-constraint dbt tests.

**Done when**

- `dbt build` completes on a fresh fixture database.
- Every mart contains its documented grain, source freshness, and calculation operands.
- A transfer candidate cannot exceed destination shortage or source surplus and cannot breach source safety stock.
- The UI can obtain lineage/run status from dbt artifacts or a backend adapter.

## Milestone 4 — Govern text-to-SQL with the semantic layer

**Build**

- Parse the YAML semantic layer into typed metric/dimension/join definitions.
- Have the model produce a typed semantic intent, not SQL.
- Validate intent with Pydantic; compile parameterized SQL deterministically.
- Parse compiled SQL with sqlglot and reject non-`SELECT`, unapproved relation/column, multi-statement, comment, and unsafe limit cases.
- Execute as a read-only database role with a timeout and a bounded result set.

**Done when**

- A supported question returns data, compiled SQL, semantic mappings, and freshness.
- Unsupported metric names or ambiguous questions return a structured clarification response.
- Prompt-injected raw SQL and destructive SQL strings are rejected before reaching the database.

## Milestone 5 — Add the real MCP server and the model-backed assistant

**Build**

- Publish exactly `search_recall_documents`, `query_recall_analytics`, and `get_data_status` through the MCP Python SDK.
- Create the small LangGraph route for documents, analytics, combined, status, and clarify requests.
- Add the answer model with strict response schema and citation ID validation.
- Record a safe request/tool trace with source/model/index versions.

**Done when**

- The model can only request the three read-only tools.
- A combined question uses both retrieved document evidence and analytics evidence, without conflating them.
- Unsupported claims or citation IDs fail validation and do not reach the frontend.
- “Move inventory” is declined because no write tool exists.

## Milestone 6 — Connect the UI to evidence and pipeline metadata

**Build**

- Replace current seeded chat results with the typed assistant response.
- Build document, data, and activity evidence tabs.
- Add PDF-page viewer/highlight and non-PDF excerpt rendering.
- Populate the pipeline canvas and inspector from dbt lineage/recent run metadata; preserve drag positions only as UI layout preferences.
- Display source freshness, data labels, evaluation configuration, metrics, failures, and no-answer/clarification states.

**Done when**

- Clicking a citation opens the correct source/page or evidence excerpt.
- The right panel explains whether evidence came from a public document or synthetic distributor data.
- The canvas does not pretend visual node movement edits a dbt model.

## Milestone 7 — Run and preserve evaluations

**Build**

- Retrieval benchmark runner across FTS-only, vector-only, hybrid, and hybrid+filters.
- Analytics runner for semantic parsing, compiler safety, and fixture-result correctness.
- Save runs with suite version, corpus/data version, retrieval/model configuration, per-case failures, aggregate metrics, and timestamp.

**Done when**

- The RAG suite records Recall@k, MRR, nDCG, precision, citation-page accuracy, and no-answer correctness.
- The analytics suite confirms no raw-table access, no unsafe SQL, and correct result fixtures.
- UI surfaces the last run and its failures; it does not hide a failed benchmark behind an overall green status.

## Final portfolio demonstration

The finished demo should reproduce one coherent investigation end to end:

1. Refresh public recall data and show source freshness.
2. Ask which critical brake recalls create Dallas stockout risk.
3. Show the matched NHTSA passage with an exact citation.
4. Show the governed mart query, semantic mapping, SQL, and synthetic-data label.
5. Show whether Memphis has a safe transfer candidate, with transparent calculations.
6. Open the pipeline page to inspect dbt lineage, test/run state, and evaluation outcome.

That demonstration proves the requested concepts without pretending to be an ERP, production demand forecaster, or fully autonomous agent.
