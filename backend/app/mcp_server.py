from __future__ import annotations

from contextlib import asynccontextmanager
import time
from typing import Annotated, Literal

from pydantic import Field

from mcp.server.fastmcp import Context, FastMCP

from .analytics import ClarificationRequired, execute_semantic_query, fallback_semantic_query
from .config import get_settings
from .mcp_audit import client_name_from_context, record_tool_call, request_id_from_context
from .mcp_models import AnalyticsQueryOutput, DataStatusOutput, DocumentSearchOutput, McpCitation, McpMetadata
from .rag import hybrid_search
from .schemas import RetrievalTrace, SemanticQuery
from .status import data_status


MCP_INSTRUCTIONS = """
Recall RAG provides read-only access to public NHTSA recall evidence and approved
analytics marts. Use search_recall_documents for source wording and citations,
query_recall_analytics for counts, shortages, demand, and transfer candidates,
and get_data_status for freshness. Never imply that this server changes source
systems. Operational inventory and demand values are synthetic demo data.
""".strip()


@asynccontextmanager
async def mcp_lifespan(_: FastMCP):
    # MCP startup must not bootstrap schemas, create roles, or run migrations.
    # The API lifecycle/migration job owns that work; these tools only read
    # indexed data and emit best-effort telemetry rows.
    settings = get_settings()
    yield {"settings": settings}


mcp = FastMCP(
    "Recall RAG governed data tools",
    instructions=MCP_INSTRUCTIONS,
    host="127.0.0.1",
    port=8011,
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
    lifespan=mcp_lifespan,
)


def _data_version(status: dict) -> str | None:
    values = [
        source.get("refreshed_at")
        for source in status.get("sources", {}).values()
        if source.get("refreshed_at") is not None
    ]
    if not values:
        return None
    return max(value.isoformat() if hasattr(value, "isoformat") else str(value) for value in values)


@mcp.tool(
    title="Search recall documents",
    description=(
        "Search indexed public NHTSA recall notices and guidance using hybrid keyword and vector retrieval. "
        "Use for source wording, consequences, remedies, notices, and policies. Returns bounded, citable passages."
    ),
    structured_output=True,
)
def search_recall_documents(
    query: Annotated[str, Field(min_length=3, max_length=2000)],
    campaign_id: Annotated[str | None, Field(min_length=1, max_length=120)] = None,
    component: Annotated[str | None, Field(min_length=1, max_length=240)] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 5,
    ctx: Context | None = None,
) -> DocumentSearchOutput:
    settings = get_settings()
    started = time.perf_counter()
    request_id = request_id_from_context(ctx)
    client_name = client_name_from_context(ctx)
    safe_limit = min(limit, 8)
    try:
        result = hybrid_search(
            settings,
            query,
            filters={key: value for key, value in {"campaign_id": campaign_id, "component": component}.items() if value},
            limit=safe_limit,
        )
        citations = [McpCitation.from_citation(citation) for citation in result["citations"]]
        status: Literal["success", "no_result"] = "success" if citations else "no_result"
        duration_ms = round((time.perf_counter() - started) * 1000)
        current_status = data_status(settings, "documents")
        output = DocumentSearchOutput(
            status=status,
            citations=citations,
            retrieval=RetrievalTrace.model_validate(result["configuration"]),
            message=None if citations else "No indexed public source directly matched the request.",
            metadata=McpMetadata(
                request_id=request_id,
                duration_ms=duration_ms,
                data_version=_data_version(current_status),
            ),
        )
        record_tool_call(
            settings,
            request_id=request_id,
            client_name=client_name,
            tool_name="search_recall_documents",
            status=status,
            result_summary=f"{len(citations)} cited passage{'s' if len(citations) != 1 else ''}",
            result_count=len(citations),
            duration_ms=duration_ms,
        )
        return output
    except Exception as error:
        duration_ms = round((time.perf_counter() - started) * 1000)
        record_tool_call(
            settings,
            request_id=request_id,
            client_name=client_name,
            tool_name="search_recall_documents",
            status="error",
            result_summary="Document search failed",
            result_count=0,
            duration_ms=duration_ms,
            error_code=type(error).__name__,
        )
        return DocumentSearchOutput(
            status="error",
            message="The document index could not complete this search.",
            metadata=McpMetadata(request_id=request_id, duration_ms=duration_ms),
        )


@mcp.tool(
    title="Query recall analytics",
    description=(
        "Answer a numerical or operational question using approved semantic definitions and tested dbt marts. "
        "Use for recall counts, demand, stockout risk, and transfer candidates. Pass a natural-language question or an approved semantic query object; raw SQL is never accepted."
    ),
    structured_output=True,
)
def query_recall_analytics(
    question: Annotated[str | None, Field(min_length=3, max_length=2000)] = None,
    max_rows: Annotated[int, Field(ge=1, le=100)] = 25,
    semantic_query: SemanticQuery | None = None,
    ctx: Context | None = None,
) -> AnalyticsQueryOutput:
    settings = get_settings()
    started = time.perf_counter()
    request_id = request_id_from_context(ctx)
    client_name = client_name_from_context(ctx)
    try:
        if semantic_query is None and not question:
            raise ClarificationRequired("Provide a natural-language question or an approved semantic query.")
        intent = semantic_query or fallback_semantic_query(question or "")
        intent.limit = min(max_rows, min(settings.sql_max_rows, 100))
        evidence = execute_semantic_query(settings, intent)
        duration_ms = round((time.perf_counter() - started) * 1000)
        current_status = data_status(settings, "analytics")
        result_status: Literal["success", "no_result"] = "success" if evidence.rows else "no_result"
        output = AnalyticsQueryOutput(
            status=result_status,
            evidence=evidence,
            interpreted_intent=intent.model_dump(),
            message=None if evidence.rows else "The approved query ran successfully but matched no rows.",
            metadata=McpMetadata(
                request_id=request_id,
                duration_ms=duration_ms,
                data_version=_data_version(current_status),
            ),
        )
        record_tool_call(
            settings,
            request_id=request_id,
            client_name=client_name,
            tool_name="query_recall_analytics",
            status=result_status,
            result_summary=(
                f"{len(evidence.rows)} row{'s' if len(evidence.rows) != 1 else ''} returned"
                if evidence.rows else "No rows matched the approved query"
            ),
            result_count=len(evidence.rows),
            duration_ms=duration_ms,
        )
        return output
    except ClarificationRequired as error:
        duration_ms = round((time.perf_counter() - started) * 1000)
        record_tool_call(
            settings,
            request_id=request_id,
            client_name=client_name,
            tool_name="query_recall_analytics",
            status="clarification_required",
            result_summary="Question needs clarification",
            result_count=0,
            duration_ms=duration_ms,
            error_code="clarification_required",
        )
        return AnalyticsQueryOutput(
            status="clarification_required",
            message=str(error),
            metadata=McpMetadata(request_id=request_id, duration_ms=duration_ms),
        )
    except ValueError as error:
        duration_ms = round((time.perf_counter() - started) * 1000)
        record_tool_call(
            settings,
            request_id=request_id,
            client_name=client_name,
            tool_name="query_recall_analytics",
            status="refused",
            result_summary="Question outside approved analytics data",
            result_count=0,
            duration_ms=duration_ms,
            error_code="unsupported_semantic_request",
        )
        return AnalyticsQueryOutput(
            status="refused",
            message=str(error),
            metadata=McpMetadata(request_id=request_id, duration_ms=duration_ms),
        )
    except Exception as error:
        duration_ms = round((time.perf_counter() - started) * 1000)
        record_tool_call(
            settings,
            request_id=request_id,
            client_name=client_name,
            tool_name="query_recall_analytics",
            status="error",
            result_summary="Analytics query failed",
            result_count=0,
            duration_ms=duration_ms,
            error_code=type(error).__name__,
        )
        return AnalyticsQueryOutput(
            status="error",
            message="The governed analytics query could not be completed.",
            metadata=McpMetadata(request_id=request_id, duration_ms=duration_ms),
        )


@mcp.tool(
    title="Get data status",
    description=(
        "Report document-index and analytics freshness, record counts, model mode, and the latest evaluation time. "
        "This tool observes status and never refreshes or mutates data."
    ),
    structured_output=True,
)
def get_data_status(
    scope: Literal["all", "documents", "analytics"] = "all",
    ctx: Context | None = None,
) -> DataStatusOutput:
    settings = get_settings()
    started = time.perf_counter()
    request_id = request_id_from_context(ctx)
    client_name = client_name_from_context(ctx)
    try:
        status_data = data_status(settings, scope)
        duration_ms = round((time.perf_counter() - started) * 1000)
        source_count = len(status_data.get("sources", {}))
        output = DataStatusOutput(
            status="success",
            data=status_data,
            metadata=McpMetadata(
                request_id=request_id,
                duration_ms=duration_ms,
                data_version=_data_version(status_data),
            ),
        )
        record_tool_call(
            settings,
            request_id=request_id,
            client_name=client_name,
            tool_name="get_data_status",
            status="success",
            result_summary=f"{source_count} source status record{'s' if source_count != 1 else ''}",
            result_count=source_count,
            duration_ms=duration_ms,
        )
        return output
    except Exception as error:
        duration_ms = round((time.perf_counter() - started) * 1000)
        record_tool_call(
            settings,
            request_id=request_id,
            client_name=client_name,
            tool_name="get_data_status",
            status="error",
            result_summary="Status lookup failed",
            result_count=0,
            duration_ms=duration_ms,
            error_code=type(error).__name__,
        )
        return DataStatusOutput(
            status="error",
            data={},
            metadata=McpMetadata(request_id=request_id, duration_ms=duration_ms),
        )


def run_stdio() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_stdio()
