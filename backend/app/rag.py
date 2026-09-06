from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Iterable

from openai import OpenAI
from pgvector import Vector
from psycopg.types.json import Jsonb

from .config import Settings
from .db import connection
from .schemas import Citation

# NHTSA campaign ids appear in both forms in public material: 24V376000 and
# 24V-376. Normalize to the form stored in the corpus before filtering.
CAMPAIGN_PATTERN = re.compile(r"\b\d{2}V-?\d{3,}\b", re.IGNORECASE)


def _words(text: str) -> list[re.Match[str]]:
    return list(re.finditer(r"\S+", text))


def chunk_text(text: str, *, target_tokens: int = 500, overlap_tokens: int = 75) -> list[tuple[str, int, int, int]]:
    """Return page-bounded chunks using word tokens as a dependency-free token estimate."""
    tokens = _words(text)
    if not tokens:
        return []
    chunks: list[tuple[str, int, int, int]] = []
    start = 0
    index = 0
    while start < len(tokens):
        end = min(start + target_tokens, len(tokens))
        char_start = tokens[start].start()
        char_end = tokens[end - 1].end()
        chunks.append((text[char_start:char_end], char_start, char_end, end - start))
        if end == len(tokens):
            break
        start = max(end - overlap_tokens, start + 1)
        index += 1
    return chunks


class Embedder:
    """Uses OpenAI embeddings when configured; otherwise an explicit dev-only deterministic fallback."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            default_headers={"HTTP-Referer": "http://localhost:5173", "X-OpenRouter-Title": "Recall RAG"} if settings.openai_base_url else None,
        ) if settings.model_enabled else None

    @property
    def model_name(self) -> str:
        return self.settings.embedding_model if self.client else "development-hash-1536"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.client:
            response = self.client.embeddings.create(model=self.settings.embedding_model, input=texts)
            return [item.embedding for item in response.data]
        return [self._hash_embed(text) for text in texts]

    def _hash_embed(self, text: str) -> list[float]:
        vector = [0.0] * self.settings.embedding_dimensions
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % len(vector)
            vector[index] += 1.0 if digest[4] % 2 else -1.0
        length = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / length for value in vector]


def recall_narrative(row: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Campaign: {row['campaign_id']}",
            f"Component: {row['component']}",
            f"Summary: {row['summary']}",
            f"Consequence: {row['consequence']}",
            f"Remedy: {row['remedy']}",
            f"Park outside: {bool(row['park_outside'])}",
            f"Do not drive: {bool(row['do_not_drive'])}",
        ]
    )


def sync_recall_documents(settings: Settings) -> dict[str, int]:
    """Version and index recall narratives without re-embedding unchanged source content."""
    embedder = Embedder(settings)
    indexed = 0
    unchanged = 0
    with connection(settings) as conn:
        recalls = conn.execute("SELECT * FROM raw_nhtsa_recalls ORDER BY campaign_id").fetchall()
        for recall in recalls:
            source_key = recall["source_key"]
            text = recall_narrative(recall)
            checksum = hashlib.sha256(text.encode()).hexdigest()
            document = conn.execute("SELECT * FROM documents WHERE source_key = %s", (source_key,)).fetchone()
            if not document:
                document_id = uuid.uuid4()
                conn.execute(
                    """INSERT INTO documents (id,source_key,source_type,canonical_url,title,mime_type)
                    VALUES (%s,%s,'public_source',%s,%s,'application/json')""",
                    (document_id, source_key, recall["source_url"], f"Recall {recall['campaign_id']} · {recall['component']}"),
                )
            else:
                document_id = document["id"]
            version = conn.execute(
                "SELECT * FROM document_versions WHERE document_id = %s AND checksum = %s", (document_id, checksum)
            ).fetchone()
            if version:
                conn.execute("UPDATE documents SET active_version_id = %s WHERE id = %s", (version["id"], document_id))
                unchanged += 1
                continue

            conn.execute("UPDATE document_versions SET is_active = false WHERE document_id = %s", (document_id,))
            version_id = uuid.uuid4()
            page_id = uuid.uuid4()
            conn.execute(
                """INSERT INTO document_versions (id,document_id,checksum,source_updated_at,parser_version,is_active)
                VALUES (%s,%s,%s,%s,'recall-narrative-v1',true)""",
                (version_id, document_id, checksum, recall["source_refreshed_at"]),
            )
            conn.execute(
                "INSERT INTO document_pages (id,document_version_id,page_number,page_text) VALUES (%s,%s,1,%s)",
                (page_id, version_id, text),
            )
            chunks = chunk_text(text)
            vectors = embedder.embed([chunk[0] for chunk in chunks])
            for chunk_index, ((chunk, char_start, char_end, token_count), vector) in enumerate(zip(chunks, vectors, strict=True)):
                conn.execute(
                    """INSERT INTO document_chunks
                    (id,document_version_id,page_id,chunk_index,text,token_count,char_start,char_end,bbox_json,embedding,embedding_model,content_hash,metadata_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        uuid.uuid4(), version_id, page_id, chunk_index, chunk, token_count, char_start, char_end,
                        Jsonb([]), Vector(vector), embedder.model_name, hashlib.sha256(chunk.encode()).hexdigest(),
                        Jsonb({"campaign_id": recall["campaign_id"], "component": recall["component"]}),
                    ),
                )
            conn.execute("UPDATE documents SET active_version_id = %s WHERE id = %s", (version_id, document_id))
            indexed += 1
        conn.commit()
    return {"indexed": indexed, "unchanged": unchanged}


def _filters_sql(query: str, filters: dict[str, str] | None) -> tuple[str, list[Any], str | None]:
    filters = filters or {}
    clauses = ["version.is_active = true"]
    params: list[Any] = []
    campaign_id = filters.get("campaign_id") or next(iter(CAMPAIGN_PATTERN.findall(query)), None)
    if campaign_id:
        # The NHTSA feed contains both 24V118 and 24V-118.  Compare a
        # normalized representation but retain the stored value in citations.
        campaign_id = campaign_id.upper()
        clauses.append("replace(chunk.metadata_json ->> 'campaign_id', '-', '') = replace(%s, '-', '')")
        params.append(campaign_id)
    component = filters.get("component")
    if component:
        clauses.append("chunk.metadata_json ->> 'component' ILIKE %s")
        params.append(f"%{component}%")
    return " AND ".join(clauses), params, campaign_id if campaign_id else None


def _lexical_query(query: str) -> str:
    """Build a recall-oriented OR query for the keyword retrieval branch.

    Natural-language questions have filler words such as ``concerns`` that a
    source passage may not contain. Requiring every word silently empties the
    FTS candidate pool, so retain the useful words and ask PostgreSQL for a
    bounded union. Vector retrieval still supplies semantic matches; RRF ranks
    the two independent result sets together.
    """

    ignored = {
        "about", "and", "are", "can", "does", "for", "from", "has", "have", "how", "is", "its", "listed",
        "most", "nhtsa", "of", "on", "recall", "says", "that", "the", "this", "to", "what", "which", "with",
        "would", "you", "your", "vehicles", "vehicle", "concerns", "campaign",
    }
    terms = [term for term in re.findall(r"[a-z0-9]+", query.lower()) if len(term) > 2 and term not in ignored]
    # Preserve order, cap candidates, and use websearch's OR operator.
    unique = list(dict.fromkeys(terms))[:10]
    if "park" in query.lower() and "outside" in query.lower():
        unique.extend(["park", "outside"])
    if "do not drive" in query.lower():
        unique.extend(["drive"])
    return " OR ".join(dict.fromkeys(unique)) or query


def _rows_to_citations(rows: Iterable[dict[str, Any]], scores: dict[uuid.UUID, float]) -> list[Citation]:
    citations: list[Citation] = []
    for row in rows:
        metadata = row.get("metadata_json") or {}
        citations.append(
            Citation(
                citation_id=f"chunk_{row['chunk_id']}", document_id=str(row["document_id"]),
                document_version_id=str(row["document_version_id"]), title=row["title"], source_url=row["canonical_url"],
                mime_type=row["mime_type"], page_number=row["page_number"], excerpt=row["text"], bbox=row.get("bbox_json") or [],
                retrieval_score=round(scores.get(row["chunk_id"], 0.0), 4), source_type="public_source",
            )
        )
    return citations


def hybrid_search(settings: Settings, query: str, *, filters: dict[str, str] | None = None, limit: int | None = None) -> dict[str, Any]:
    """Independent FTS and vector retrieval fused with reciprocal-rank fusion."""
    top_k = limit or settings.retrieval_limit
    where_sql, filters_params, exact_campaign = _filters_sql(query, filters)
    lexical_query = _lexical_query(query)
    embedder = Embedder(settings)
    query_vector = Vector(embedder.embed([query])[0])
    common = """
      FROM document_chunks chunk
      JOIN document_versions version ON version.id = chunk.document_version_id
      JOIN documents document ON document.id = version.document_id
      LEFT JOIN document_pages page ON page.id = chunk.page_id
      WHERE """ + where_sql
    select_columns = """
      chunk.id AS chunk_id, chunk.document_version_id, chunk.text, chunk.bbox_json, chunk.metadata_json,
      document.id AS document_id, document.title, document.canonical_url, document.mime_type, page.page_number
    """
    with connection(settings) as conn:
        # Even if this function is ever reached with a broad application
        # connection, the retrieval transaction itself cannot mutate data.
        conn.execute("SET TRANSACTION READ ONLY")
        lexical = conn.execute(
            f"SELECT {select_columns}, ts_rank_cd(chunk.search_tsv, websearch_to_tsquery('english', %s)) AS score {common} "
            "AND chunk.search_tsv @@ websearch_to_tsquery('english', %s) ORDER BY score DESC LIMIT 40",
            [lexical_query, *filters_params, lexical_query],
        ).fetchall()
        vector = conn.execute(
            f"SELECT {select_columns}, 1 - (chunk.embedding <=> %s) AS score {common} ORDER BY chunk.embedding <=> %s LIMIT 40",
            [query_vector, *filters_params, query_vector],
        ).fetchall()

    fused: dict[uuid.UUID, float] = defaultdict(float)
    rows: dict[uuid.UUID, dict[str, Any]] = {}
    for rank, row in enumerate(lexical, start=1):
        fused[row["chunk_id"]] += 1 / (60 + rank)
        rows[row["chunk_id"]] = row
    for rank, row in enumerate(vector, start=1):
        fused[row["chunk_id"]] += 1 / (60 + rank)
        rows[row["chunk_id"]] = row
    lowered = query.lower()
    if exact_campaign:
        for chunk_id, row in rows.items():
            actual_campaign = (row.get("metadata_json") or {}).get("campaign_id", "").replace("-", "")
            if actual_campaign == exact_campaign.replace("-", ""):
                fused[chunk_id] += 0.04
    if "park" in lowered and "outside" in lowered:
        for chunk_id, row in rows.items():
            if "park outside: true" in row["text"].lower():
                fused[chunk_id] += 0.05
    if "do not drive" in lowered:
        for chunk_id, row in rows.items():
            if "do not drive: true" in row["text"].lower():
                fused[chunk_id] += 0.05
    # Give an exact phrase-rich source a modest lift over generic guidance.
    # RRF still combines keyword and embedding rankings; this only breaks ties
    # when a record contains several distinctive words from the user question.
    distinctive_terms = [
        term for term in re.findall(r"[a-z0-9]+", lowered)
        if len(term) > 3 and term not in {"about", "campaign", "does", "from", "listed", "nhtsa", "recall", "says", "that", "what", "which", "with"}
    ]
    for chunk_id, row in rows.items():
        source_terms = set(re.findall(r"[a-z0-9]+", row["text"].lower()))
        matches = len(set(distinctive_terms) & source_terms)
        fused[chunk_id] += min(matches, 5) * 0.008
    ordered = sorted(rows.values(), key=lambda row: fused[row["chunk_id"]], reverse=True)
    chosen: list[dict[str, Any]] = []
    per_document: dict[uuid.UUID, int] = defaultdict(int)
    for row in ordered:
        if per_document[row["document_id"]] >= 2:
            continue
        chosen.append(row)
        per_document[row["document_id"]] += 1
        if len(chosen) == top_k:
            break
    # A vector index always returns nearest neighbours. For a deliberate hard
    # negative, do not turn an unrelated nearest neighbour into fake evidence.
    generic_terms = {"about", "campaign", "does", "from", "mentions", "nhtsa", "recall", "says", "that", "the", "what", "which", "with"}
    query_terms = {term for term in re.findall(r"[a-z0-9]+", query.lower()) if len(term) > 2 and term not in generic_terms}
    lexical_overlap = max(
        (len(query_terms & set(re.findall(r"[a-z0-9]+", row["text"].lower()))) for row in chosen),
        default=0,
    )
    if not exact_campaign and lexical_overlap == 0:
        chosen = []
    return {
        "citations": _rows_to_citations(chosen, fused),
        "no_result": not chosen,
        "configuration": {
            "lexical_candidates": len(lexical), "vector_candidates": len(vector), "fusion": "rrf(k=60)",
            "embedding_model": embedder.model_name, "exact_campaign_filter": exact_campaign,
        },
    }


def get_document_page(settings: Settings, version_id: str, page_number: int) -> dict[str, Any] | None:
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT page.page_number,page.page_text,page.width,page.height,document.title,document.canonical_url
            FROM document_pages page
            JOIN document_versions version ON version.id = page.document_version_id
            JOIN documents document ON document.id = version.document_id
            WHERE page.document_version_id = %s AND page.page_number = %s""",
            (version_id, page_number),
        ).fetchone()
    return dict(row) if row else None
