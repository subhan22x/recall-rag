from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=2, max_length=2000)


class BBox(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x0: float
    y0: float
    x1: float
    y1: float


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    citation_id: str
    document_id: str
    document_version_id: str
    title: str
    source_url: str
    mime_type: str = "application/json"
    page_number: int | None = None
    excerpt: str
    bbox: list[BBox] = Field(default_factory=list)
    retrieval_score: float
    source_type: Literal["public_source", "synthetic_distributor_data"] = "public_source"


class AnalyticsEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query_id: str
    columns: list[str]
    rows: list[dict[str, Any]]
    compiled_sql: str
    semantic_terms: list[str]
    data_freshness: str | None = None
    source_label: Literal["synthetic_distributor_data"] = "synthetic_distributor_data"


class RetrievalTrace(BaseModel):
    lexical_candidates: int
    vector_candidates: int
    embedding_model: str


class AssistantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer_markdown: str
    citations: list[Citation] = Field(default_factory=list)
    analytics_evidence: AnalyticsEvidence | None = None
    retrieval_trace: RetrievalTrace | None = None
    warnings: list[str] = Field(default_factory=list)
    tool_trace: list[str] = Field(default_factory=list)
    request_id: str
    mode: Literal["model", "development_fallback"]


class SemanticFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    operator: Literal["=", "in", ">", ">=", "<", "<="]
    value: str | int | float | list[str] | list[int] | list[float]


class OrderBy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    direction: Literal["asc", "desc"]


class SemanticQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    metrics: list[str] = Field(min_length=1, max_length=4)
    dimensions: list[str] = Field(max_length=6)
    filters: list[SemanticFilter] = Field(max_length=5)
    order_by: list[OrderBy] = Field(max_length=2)
    limit: int = Field(ge=1, le=100)


class DataStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    status: Literal["healthy", "stale", "unavailable"]
    refreshed_at: datetime | None = None
    record_count: int | None = None
    detail: str | None = None
