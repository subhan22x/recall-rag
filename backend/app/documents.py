from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml
from pgvector import Vector
from psycopg.types.json import Jsonb

from .config import Settings
from .db import connection
from .rag import Embedder

ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / "sources.yml"
DOCUMENT_STORAGE = ROOT / "data" / "documents"


def _load_sources() -> list[dict[str, Any]]:
    with SOURCES_PATH.open() as file:
        return list((yaml.safe_load(file) or {}).get("documents", []))


def _page_chunks(page: Any, target_tokens: int = 500, overlap_tokens: int = 75) -> list[tuple[str, int, int, int, list[dict[str, float]]]]:
    words = page.get_text("words", sort=True)
    if not words:
        return []
    chunks: list[tuple[str, int, int, int, list[dict[str, float]]]] = []
    start = 0
    while start < len(words):
        end = min(start + target_tokens, len(words))
        selected = words[start:end]
        text = " ".join(str(word[4]) for word in selected)
        bbox = [{"x0": float(word[0]), "y0": float(word[1]), "x1": float(word[2]), "y1": float(word[3])} for word in selected]
        chunks.append((text, 0, len(text), len(selected), bbox))
        if end == len(words):
            break
        start = max(end - overlap_tokens, start + 1)
    return chunks


def sync_official_pdf_documents(settings: Settings) -> dict[str, int | list[str]]:
    """Fetch/version official PDF sources and retain page-aware chunk anchors."""
    try:
        import fitz
    except ImportError:
        return {"indexed": 0, "unchanged": 0, "failed": ["PyMuPDF is not installed in this runtime"]}
    indexed = 0
    unchanged = 0
    failures: list[str] = []
    embedder = Embedder(settings)
    DOCUMENT_STORAGE.mkdir(parents=True, exist_ok=True)
    for source in _load_sources():
        try:
            response = httpx.get(source["url"], timeout=30, headers={"User-Agent": "Recall RAG/1.0"})
            response.raise_for_status()
            content = response.content
            checksum = hashlib.sha256(content).hexdigest()
            storage_path = DOCUMENT_STORAGE / f"{checksum}.pdf"
            if not storage_path.exists():
                storage_path.write_bytes(content)
            with connection(settings) as conn:
                document = conn.execute("SELECT * FROM documents WHERE source_key = %s", (source["source_key"],)).fetchone()
                if not document:
                    document_id = uuid.uuid4()
                    conn.execute(
                        "INSERT INTO documents (id,source_key,source_type,canonical_url,title,mime_type) VALUES (%s,%s,'public_source',%s,%s,%s)",
                        (document_id, source["source_key"], source["url"], source["title"], source["mime_type"]),
                    )
                else:
                    document_id = document["id"]
                existing = conn.execute("SELECT * FROM document_versions WHERE document_id = %s AND checksum = %s", (document_id, checksum)).fetchone()
                if existing:
                    conn.execute("UPDATE documents SET active_version_id = %s WHERE id = %s", (existing["id"], document_id))
                    conn.commit()
                    unchanged += 1
                    continue
                conn.execute("UPDATE document_versions SET is_active = false WHERE document_id = %s", (document_id,))
                version_id = uuid.uuid4()
                conn.execute(
                    """INSERT INTO document_versions (id,document_id,checksum,etag,last_modified,storage_path,parser_version,is_active)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,true)""",
                    (version_id, document_id, checksum, response.headers.get("etag"), response.headers.get("last-modified"), str(storage_path), source["parser"]),
                )
                pdf = fitz.open(stream=content, filetype="pdf")
                page_records: list[tuple[uuid.UUID, int, list[tuple[str, int, int, int, list[dict[str, float]]]]]] = []
                for page_number, page in enumerate(pdf, start=1):
                    page_id = uuid.uuid4()
                    text = page.get_text("text")
                    page_records.append((page_id, page_number, _page_chunks(page)))
                    conn.execute(
                        "INSERT INTO document_pages (id,document_version_id,page_number,page_text,width,height) VALUES (%s,%s,%s,%s,%s,%s)",
                        (page_id, version_id, page_number, text, page.rect.width, page.rect.height),
                    )
                chunks = [item for page_id, page_number, page_chunks in page_records for item in [(page_id, page_number, *chunk) for chunk in page_chunks]]
                vectors = embedder.embed([chunk[2] for chunk in chunks])
                for chunk_index, ((page_id, page_number, text, char_start, char_end, token_count, bbox), vector) in enumerate(zip(chunks, vectors, strict=True)):
                    conn.execute(
                        """INSERT INTO document_chunks
                        (id,document_version_id,page_id,chunk_index,text,token_count,char_start,char_end,bbox_json,embedding,embedding_model,content_hash,metadata_json)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (uuid.uuid4(), version_id, page_id, chunk_index, text, token_count, char_start, char_end, Jsonb(bbox), Vector(vector), embedder.model_name, hashlib.sha256(text.encode()).hexdigest(), Jsonb({"document_type": "guidance", "page_number": page_number})),
                    )
                conn.execute("UPDATE documents SET active_version_id = %s WHERE id = %s", (version_id, document_id))
                conn.commit()
                indexed += 1
        except Exception as error:
            failures.append(f"{source.get('source_key', 'unknown')}: {error}")
    return {"indexed": indexed, "unchanged": unchanged, "failures": failures}
