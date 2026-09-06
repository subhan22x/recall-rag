# RAG Pipeline: Evidence, Citations, and Retrieval Quality

## What RAG is responsible for

RAG handles questions where the answer depends on the wording of a safety notice or a policy document, for example: “What safety consequence did NHTSA state for campaign 24V-118?” It retrieves passages; it does not calculate inventory coverage, demand, or transfer quantities.

The target corpus contains:

- NHTSA recall records fetched from the public API (structured narrative fields plus recall metadata).
- A small, curated set of official NHTSA PDF guidance used to demonstrate page-level citations.

Every answer based on a document must be traceable to a stored source version and passage.

## Source refresh and versioning

### Refresh rules

1. Define each source in `sources.yml`: source ID, URL/API query, type, refresh cadence, parser, and allowed domain.
2. On a refresh, record the fetch time, HTTP status, `ETag`/`Last-Modified` when available, content checksum, and source URL.
3. If the content checksum has not changed, record the successful check and do not re-chunk or re-embed.
4. If it changed, create a new immutable document version. Retain the previous version so citations in historical conversations remain resolvable.
5. Parse and index only the new version. Mark the old version inactive for default retrieval but preserve it for audit/history.

For API records, the stable source key is the NHTSA campaign plus the vehicle context used by the query. For PDFs, the source key is the canonical document URL and a SHA-256 checksum of the downloaded bytes.

### Target document tables

```text
documents(
  id, source_key, source_type, canonical_url, title, mime_type,
  active_version_id, created_at
)
document_versions(
  id, document_id, checksum, source_updated_at, fetched_at,
  etag, last_modified, storage_path, parser_version, is_active
)
document_pages(
  id, document_version_id, page_number, page_text, width, height
)
document_chunks(
  id, document_version_id, page_id, chunk_index, text, token_count,
  char_start, char_end, bbox_json, embedding, embedding_model,
  content_hash, metadata_json
)
```

`bbox_json` stores the page coordinates of the cited span when a parser can supply them. It is optional for API records and non-PDF sources.

## Parsing and chunking

### PDF documents

Use PyMuPDF to extract text separately for each page. Preserve page number, page dimensions, text blocks, and span coordinates before chunking. A citation to a PDF can then open the same page and highlight the actual source span rather than merely asserting a page number.

### Recall API records

Build a normalized narrative from stable fields, keeping field labels in the text so retrieval and answer composition preserve their meaning:

```text
Campaign: 24V-118
Component: Service Brakes, Hydraulic
Summary: ...
Consequence: ...
Remedy: ...
Park outside: false
Do not drive: true
```

The normalized record is a source document with a single logical page. Field-level metadata is retained so answers can cite `consequence` or `remedy` precisely.

### Chunk policy

- Target **500 tokens** per chunk with **75-token overlap**.
- Never join text across PDF page boundaries.
- Prefer sentence or paragraph boundaries. Use hard token splitting only for an overlong paragraph.
- Include document title, campaign ID, component, and page number as chunk metadata, not repeated unbounded boilerplate.
- Serialize table rows as `header: value` pairs. Do not embed an entire table as a visually flattened paragraph.
- Deduplicate chunks by `content_hash`; re-embedding runs only when text or the embedding-model version changes.

Each chunk must remain understandable on its own. The UI will display its `text`, `page_number`, `bbox_json`, source title, source URL, and version/checksum metadata.

## Embedding and indexing

The target implementation replaces the current 32-dimension deterministic hash vectors with a real embedding model. The embedding model name and dimensions are stored with every chunk so a later model migration can be reindexed deliberately rather than silently mixing vectors.

PostgreSQL holds both retrieval indexes:

- `embedding vector(...)` with an HNSW cosine-distance index for semantic similarity.
- `search_tsv tsvector` generated from chunk text and selected metadata for lexical search.

The lexical index is especially important for campaign IDs, part numbers, regulatory phrases, and exact model names. Embeddings are especially useful when a user paraphrases a source passage.

## Hybrid retrieval

Given a question:

1. Normalize recognized filters: campaign ID, manufacturer, component, document type, date, and advisory terms. Filters narrow candidate documents but do not replace retrieval.
2. Run lexical search and vector search independently, each returning the top 40 candidates.
3. Fuse those ranked lists with reciprocal-rank fusion:

   ```text
   RRF score(chunk) = 1 / (60 + lexical_rank)
                    + 1 / (60 + vector_rank)
   ```

4. Apply an exact campaign-ID boost when the question contains a valid campaign identifier.
5. Limit duplicate context to at most two chunks per source document, then return the top eight chunks.
6. Send only those chunk texts and citation IDs to the answer model.

This means the model never searches the corpus itself. It receives a small evidence bundle, not the entire source collection.

### No-answer behavior

If no result clears the configured retrieval threshold, or the sources conflict, the assistant says it could not verify the claim from the indexed material. It can offer a narrower query or show the closest sources, but it must not fill the gap with general model knowledge.

## Citation contract

Every retrieved citation includes:

```json
{
  "citation_id": "src_24v118_p1_c0",
  "document_id": "nhtsa-recall-24v118",
  "document_version_id": "...",
  "title": "Recall 24V-118 · Brake hose",
  "source_url": "https://...",
  "page_number": 1,
  "excerpt": "...",
  "bbox": [{"x0": 42.1, "y0": 312.4, "x1": 480.8, "y1": 334.2}],
  "retrieval_score": 0.82
}
```

The answer is stored as text plus citation IDs. The server validates every marker before returning it. The frontend must never infer a citation from answer text alone.

- **PDF:** right pane loads the stored PDF in a same-origin viewer or approved PDF renderer, selects `page_number`, and draws highlight rectangles from `bbox`.
- **Non-PDF/API:** right pane shows title, version, URL, field/page label, and saved excerpt. It does not pretend there is a PDF page to open.

## Retrieval benchmark suite

The benchmark file is committed as reviewed JSONL, not generated by the model during a run. Start with roughly 60 cases across:

| Case type | What it proves |
| --- | --- |
| Exact campaign IDs | IDs and technical identifiers work lexically. |
| Exact phrases | The source wording can be found. |
| Paraphrases | Semantic retrieval works beyond keyword matching. |
| Metadata/filter questions | Manufacturer, component, date, and document type filters are applied correctly. |
| Multi-source questions | The system returns the necessary source set, not one attractive fragment. |
| Hard negatives / no-answer | It does not cite unrelated material just to answer. |

Each case specifies question, expected document/version/page/chunk IDs, acceptable alternatives, and whether a no-answer is expected. Example:

```json
{"id":"rag-014","question":"Which campaign involves a brake hose rupture?","expected_chunk_ids":["src_24v118_p1_c0"],"expected_pages":[1],"expect_no_answer":false}
```

Run four retrieval configurations on the same suite: FTS only, vector only, hybrid, and hybrid plus metadata filters. Store the configuration, corpus version, embedding model, timestamp, per-case results, and aggregate result together.

Initial target gates:

| Metric | Target |
| --- | --- |
| Recall@1 | >= 0.65 |
| Recall@5 | >= 0.90 |
| Recall@8 | >= 0.95 |
| MRR | >= 0.80 |
| nDCG@5 | >= 0.80 |
| Precision@5 | >= 0.60 |
| Citation page accuracy | 1.00 |
| No-answer correctness | >= 0.90 |

The targets are regression gates for this small portfolio corpus, not universal claims about RAG quality. A change that misses a gate should retain the previous retrieval configuration until the misses are explained.
