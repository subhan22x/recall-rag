from __future__ import annotations

from datetime import datetime
import logging
from typing import Any
import uuid

from mcp.server.fastmcp import Context

from .config import Settings
from .db import connection


logger = logging.getLogger(__name__)


def client_name_from_context(ctx: Context | None) -> str:
    if ctx is None:
        return "MCP client"
    try:
        client_info = ctx.session.client_params.clientInfo
        if client_info and client_info.name:
            return client_info.name[:120]
    except (AttributeError, ValueError):
        pass
    return (ctx.client_id or "MCP client")[:120]


def request_id_from_context(ctx: Context | None) -> str:
    if ctx is None:
        return str(uuid.uuid4())
    try:
        return ctx.request_id
    except ValueError:
        return str(uuid.uuid4())


def record_tool_call(
    settings: Settings,
    *,
    request_id: str,
    client_name: str,
    tool_name: str,
    status: str,
    result_summary: str,
    result_count: int,
    duration_ms: int,
    error_code: str | None = None,
) -> None:
    """Persist sanitized tool telemetry; prompts, credentials, and result bodies are omitted."""
    # Telemetry must never turn a useful tool response into an error. The MCP
    # contract is about read-only data access; this optional INSERT is only
    # local observability and can be disabled in a minimal deployment.
    try:
        with connection(settings) as conn:
            conn.execute(
                """INSERT INTO mcp_tool_calls
                (id, request_id, client_name, tool_name, status, result_summary,
                 result_count, duration_ms, error_code)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    uuid.uuid4(), request_id, client_name, tool_name, status,
                    result_summary[:240], max(result_count, 0), max(duration_ms, 0), error_code,
                ),
            )
            conn.commit()
    except Exception:
        logger.exception("Could not persist MCP telemetry for %s", tool_name)


def recent_tool_calls(settings: Settings, limit: int = 20) -> list[dict[str, Any]]:
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT request_id, client_name, tool_name, status, result_summary,
                      result_count, duration_ms, error_code, created_at
               FROM mcp_tool_calls
               ORDER BY created_at DESC
               LIMIT %s""",
            (min(max(limit, 1), 100),),
        ).fetchall()
    return [dict(row) for row in rows]


def last_client_use(settings: Settings) -> dict[str, datetime]:
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT client_name, max(created_at) AS last_used_at
               FROM mcp_tool_calls
               GROUP BY client_name"""
        ).fetchall()
    return {row["client_name"]: row["last_used_at"] for row in rows}
