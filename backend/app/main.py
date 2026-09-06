from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hmac
from pathlib import Path
from typing import Literal
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
import fitz
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import Receive, Scope, Send

from .agent import RecallRagAgent
from .analytics import execute_semantic_query, fallback_semantic_query
from .config import Settings, get_settings
from .db import connection, init_database
from .dbt_runner import run_dbt_build
from .documents import sync_official_pdf_documents
from .evals import run_all_evaluations
from .ingest import refresh_nhtsa
from .mcp_audit import last_client_use, recent_tool_calls
from .mcp_server import mcp
from .rag import get_document_page, sync_recall_documents
from .schemas import ChatRequest, SemanticQuery
from .seed import seed_demo_data
from .semantic import public_semantic_layer
from .status import data_status

ROOT = Path(__file__).resolve().parents[2]
MCP_TOOL_CATALOG = [
    {
        "name": "search_recall_documents",
        "access": "read-only",
        "scope": "Documents only",
        "description": "Searches NHTSA documents and returns relevant, cited passages.",
    },
    {
        "name": "query_recall_analytics",
        "access": "read-only",
        "scope": "Approved marts only",
        "description": "Answers business questions using approved analytics data.",
    },
    {
        "name": "get_data_status",
        "access": "read-only",
        "scope": "Status only",
        "description": "Checks whether documents and analytics data are current.",
    },
]


def _mcp_client_configs() -> dict:
    python_path = str(ROOT / ".venv" / "bin" / "python")
    launcher_path = str(ROOT / "mcp_server.py")
    json_config = {
        "mcpServers": {
            "recall_rag": {
                "command": python_path,
                "args": [launcher_path],
            }
        }
    }
    codex_config = (
        "[mcp_servers.recall_rag]\n"
        f'command = "{python_path}"\n'
        f'args = ["{launcher_path}"]'
    )
    return {
        "codex": {
            "name": "Codex",
            "filename": "~/.codex/config.toml",
            "instructions": [
                "Open your Codex MCP configuration.",
                "Add the Recall RAG server block below.",
                "Restart Codex and ask it to list Recall RAG tools.",
            ],
            "configuration": codex_config,
        },
        "claude": {
            "name": "Claude",
            "filename": "claude_desktop_config.json",
            "instructions": [
                "Open Claude Desktop settings and edit its configuration.",
                "Add the Recall RAG server block below.",
                "Restart Claude Desktop and select a Recall RAG tool.",
            ],
            "configuration": json_config,
        },
        "cursor": {
            "name": "Cursor",
            "filename": ".cursor/mcp.json",
            "instructions": [
                "Open Cursor MCP settings for this workspace.",
                "Add the Recall RAG server block below.",
                "Reload Cursor and confirm the three tools appear.",
            ],
            "configuration": json_config,
        },
    }


def pipeline_nodes(settings: Settings) -> list[dict]:
    status = data_status(settings)
    return [
        {"id": "nhtsa", "title": "NHTSA recalls API", "kind": "source", "status": "fresh", "meta": f"{status['sources']['nhtsa_recalls']['record_count']} records"},
        {"id": "raw", "title": "raw_nhtsa_recalls", "kind": "raw", "status": "fresh", "meta": "PostgreSQL raw table"},
        {"id": "stg", "title": "stg_nhtsa_recalls", "kind": "staging", "status": "fresh", "meta": "dbt staging model"},
        {"id": "demand", "title": "mart_recall_part_demand", "kind": "mart", "status": "fresh", "meta": "dbt scenario estimate"},
        {"id": "risk", "title": "mart_stockout_risk", "kind": "mart", "status": "fresh", "meta": "dbt stockout risk"},
        {"id": "transfer", "title": "mart_transfer_candidates", "kind": "mart", "status": "fresh", "meta": "read-only candidates"},
        {"id": "search", "title": "search_recall_documents", "kind": "tool", "status": "fresh", "meta": "MCP · hybrid RAG"},
        {"id": "analytics", "title": "query_recall_analytics", "kind": "tool", "status": "fresh", "meta": "MCP · governed SQL"},
        {"id": "agent", "title": "Recall RAG assistant", "kind": "agent", "status": "fresh", "meta": status["model_mode"]},
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_database(settings)
    seed_demo_data(settings)
    dbt = run_dbt_build(settings)
    if not dbt["ok"]:
        raise RuntimeError(f"dbt build failed: {dbt['output']}")
    sync_recall_documents(settings)
    sync_official_pdf_documents(settings)
    app.state.settings = settings
    app.state.agent = RecallRagAgent(settings)
    app.state.dbt = dbt
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Recall RAG API", version="1.0.0", lifespan=lifespan)


class McpAccessMiddleware:
    """Keep the demo MCP endpoint local unless an explicit bearer is set."""

    def __init__(self, app, token: str | None):
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        client_host = client[0] if client else ""
        loopback = client_host in {"127.0.0.1", "::1", "localhost"}
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, supplied = authorization.partition(" ")
        supplied = supplied.strip() if scheme.lower() == "bearer" else ""
        authenticated = bool(self.token and supplied and hmac.compare_digest(supplied, self.token))
        allowed = authenticated if self.token else loopback
        if allowed:
            await self.app(scope, receive, send)
            return

        body = b'MCP endpoint requires a configured bearer token or a loopback connection.'
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


mcp_http_app = McpAccessMiddleware(mcp.streamable_http_app(), get_settings().mcp_http_token)
settings_for_cors = get_settings()
app.add_middleware(CORSMiddleware, allow_origins=settings_for_cors.cors_origins, allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Authorization"], allow_credentials=False)


@app.get("/api/health")
def health() -> dict:
    settings: Settings = app.state.settings
    return {"ok": True, "service": "recall-rag", "status": data_status(settings), "dbt": app.state.dbt, "time": datetime.now(UTC).isoformat()}


@app.get("/api/data-status")
def get_status(scope: Literal["all", "documents", "analytics"] = "all") -> dict:
    return data_status(app.state.settings, scope)


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict:
    return app.state.agent.run(request.question).model_dump()


@app.post("/api/refresh")
def refresh() -> dict:
    settings: Settings = app.state.settings
    ingestion = refresh_nhtsa(settings)
    dbt = run_dbt_build(settings)
    if not dbt["ok"]:
        raise HTTPException(status_code=500, detail="dbt build failed after refresh")
    index = sync_recall_documents(settings)
    documents = sync_official_pdf_documents(settings)
    app.state.dbt = dbt
    return {"ingestion": ingestion, "dbt": dbt, "index": index, "documents": documents}


@app.get("/api/pipelines")
def pipelines() -> dict:
    return {"nodes": pipeline_nodes(app.state.settings), "status": data_status(app.state.settings)}


@app.get("/api/semantic-layer")
def semantic_layer() -> dict:
    return public_semantic_layer()


@app.post("/api/analytics/query")
def analytics_query(query: SemanticQuery | None = None, question: str | None = None) -> dict:
    if query is None:
        if not question:
            raise HTTPException(status_code=422, detail="Provide a SemanticQuery or question")
        query = fallback_semantic_query(question)
    return execute_semantic_query(app.state.settings, query).model_dump()


@app.get("/api/evaluations")
def evaluations() -> dict:
    return run_all_evaluations(app.state.settings)


@app.get("/api/documents/{version_id}/pages/{page_number}")
def document_page(version_id: str, page_number: int) -> dict:
    page = get_document_page(app.state.settings, version_id, page_number)
    if not page:
        raise HTTPException(status_code=404, detail="Document page not found")
    return page


@app.get("/api/documents/{version_id}/file")
def document_file(version_id: str):
    with connection(app.state.settings) as conn:
        row = conn.execute(
            """SELECT version.storage_path, document.mime_type, document.title
            FROM document_versions version JOIN documents document ON document.id = version.document_id
            WHERE version.id = %s""",
            (version_id,),
        ).fetchone()
    if not row or not row["storage_path"]:
        raise HTTPException(status_code=404, detail="Stored document file not found")
    return FileResponse(row["storage_path"], media_type=row["mime_type"], filename=f"{row['title']}.pdf", content_disposition_type="inline")


@app.get("/api/documents/{version_id}/pages/{page_number}/image")
def document_page_image(version_id: str, page_number: int, citation_id: str | None = None):
    """Render one PDF page with the selected citation coordinates highlighted."""
    highlight_boxes: list[dict[str, float]] = []
    with connection(app.state.settings) as conn:
        row = conn.execute("SELECT storage_path FROM document_versions WHERE id = %s", (version_id,)).fetchone()
        if citation_id and citation_id.startswith("chunk_"):
            try:
                chunk_id = uuid.UUID(citation_id.removeprefix("chunk_"))
                chunk = conn.execute(
                    """SELECT chunk.bbox_json
                    FROM document_chunks chunk
                    LEFT JOIN document_pages page ON page.id = chunk.page_id
                    WHERE chunk.id = %s AND chunk.document_version_id = %s AND page.page_number = %s""",
                    (chunk_id, version_id, page_number),
                ).fetchone()
                if chunk:
                    highlight_boxes = chunk["bbox_json"] or []
            except ValueError:
                highlight_boxes = []
    if not row or not row["storage_path"]:
        raise HTTPException(status_code=404, detail="Stored document file not found")
    try:
        with fitz.open(row["storage_path"]) as pdf:
            if pdf.page_count < 1:
                raise HTTPException(status_code=404, detail="Document has no pages")
            # Some indexed citation metadata can outlive a source revision;
            # keep the preview usable by clamping to the stored PDF range.
            safe_page_number = min(max(page_number, 1), pdf.page_count)
            page = pdf.load_page(safe_page_number - 1)
            for box in highlight_boxes:
                rect = fitz.Rect(box["x0"], box["y0"], box["x1"], box["y1"])
                annotation = page.add_highlight_annot(rect)
                annotation.set_colors(stroke=(1, 0.78, 0.12))
                annotation.set_opacity(0.42)
                annotation.update()
            image = page.get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False).tobytes("png")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not render document page: {error}") from error
    return Response(content=image, media_type="image/png")


@app.get("/api/mcp/tools")
def mcp_tools() -> dict:
    return {"tools": MCP_TOOL_CATALOG}


@app.get("/api/mcp/status")
def mcp_status() -> dict:
    settings: Settings = app.state.settings
    client_usage = last_client_use(settings)
    normalized_usage: dict[str, datetime] = {}
    for raw_name, used_at in client_usage.items():
        lowered = raw_name.lower()
        for client_key in ("codex", "claude", "cursor"):
            if client_key in lowered:
                previous = normalized_usage.get(client_key)
                if previous is None or used_at > previous:
                    normalized_usage[client_key] = used_at
    return {
        "running": True,
        "access": "read-only",
        "transports": ["stdio", "streamable-http"],
        "http_endpoint": "http://127.0.0.1:8010/mcp",
        "http_access": "bearer token" if settings.mcp_http_token else "loopback only",
        "tool_count": len(MCP_TOOL_CATALOG),
        "tools": MCP_TOOL_CATALOG,
        "client_configs": _mcp_client_configs(),
        "client_last_used": normalized_usage,
    }


@app.get("/api/mcp/calls")
def mcp_calls(limit: int = 20) -> dict:
    return {"calls": recent_tool_calls(app.state.settings, limit)}


# Keep MCP mounted after the API routes so /api/* remains owned by FastAPI.
app.mount("/", mcp_http_app)
