from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import AnalyticsEvidence, Citation, RetrievalTrace


ToolStatus = Literal["success", "no_result", "clarification_required", "refused", "error"]


class McpMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    duration_ms: int = Field(ge=0)
    data_version: str | None = None


class McpCitation(BaseModel):
    """A compact citation fit for an external agent's context window.

    The assistant UI uses the richer internal Citation object to draw PDF
    highlights. MCP consumers only need a stable source pointer and passage,
    not page-coordinate arrays or internal document UUIDs.
    """

    model_config = ConfigDict(extra="forbid")
    citation_id: str
    title: str
    source_url: str
    page_number: int | None = None
    excerpt: str
    retrieval_score: float
    source_type: Literal["public_source", "synthetic_distributor_data"] = "public_source"

    @classmethod
    def from_citation(cls, citation: Citation) -> "McpCitation":
        return cls(
            citation_id=citation.citation_id,
            title=citation.title,
            source_url=citation.source_url,
            page_number=citation.page_number,
            excerpt=citation.excerpt,
            retrieval_score=citation.retrieval_score,
            source_type=citation.source_type,
        )


class DocumentSearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ToolStatus
    citations: list[McpCitation] = Field(default_factory=list)
    retrieval: RetrievalTrace | None = None
    message: str | None = None
    metadata: McpMetadata


class AnalyticsQueryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ToolStatus
    evidence: AnalyticsEvidence | None = None
    interpreted_intent: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    message: str | None = None
    metadata: McpMetadata


class DataStatusOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ToolStatus
    data: dict[str, Any]
    metadata: McpMetadata
