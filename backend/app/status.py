from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .config import Settings
from .db import connection


def _health(record_count: int | None, refreshed_at, *, max_age: timedelta = timedelta(days=2)) -> str:
    if not record_count or refreshed_at is None:
        return "unavailable"
    timestamp = refreshed_at if refreshed_at.tzinfo else refreshed_at.replace(tzinfo=UTC)
    return "stale" if datetime.now(UTC) - timestamp > max_age else "healthy"


def data_status(settings: Settings, scope: str = "all") -> dict:
    """Return read-only freshness and evaluation metadata for agents and the UI."""
    with connection(settings) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        recall = conn.execute(
            "SELECT count(*) AS count, max(source_refreshed_at) AS refreshed_at FROM raw_nhtsa_recalls"
        ).fetchone()
        documents = conn.execute(
            "SELECT count(*) AS count, max(fetched_at) AS refreshed_at FROM document_versions WHERE is_active"
        ).fetchone()
        chunks = conn.execute(
            """SELECT count(*) AS count
            FROM document_chunks chunk
            JOIN document_versions version ON version.id = chunk.document_version_id
            WHERE version.is_active"""
        ).fetchone()
        marts = conn.execute(
            "SELECT count(*) AS count, max(source_refreshed_at) AS refreshed_at FROM analytics.mart_stockout_risk"
        ).fetchone()
        eval_run = conn.execute(
            "SELECT created_at FROM evaluation_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    sources = {
        "nhtsa_recalls": {
            "status": _health(recall["count"], recall["refreshed_at"]),
            "record_count": recall["count"],
            "refreshed_at": recall["refreshed_at"],
        },
        "document_index": {
            "status": _health(documents["count"], documents["refreshed_at"]),
            "record_count": documents["count"],
            "chunk_count": chunks["count"],
            "refreshed_at": documents["refreshed_at"],
        },
        "synthetic_distributor_data": {
            "status": _health(marts["count"], marts["refreshed_at"]),
            "record_count": marts["count"],
            "refreshed_at": marts["refreshed_at"],
        },
    }
    selected_sources = sources if scope == "all" else {
        key: value
        for key, value in sources.items()
        if (scope == "documents" and key in {"nhtsa_recalls", "document_index"})
        or (scope == "analytics" and key == "synthetic_distributor_data")
    }
    return {
        "scope": scope,
        "model_mode": "model" if settings.model_enabled else "development_fallback",
        "sources": selected_sources,
        "latest_evaluation_at": eval_run["created_at"] if eval_run else None,
    }
