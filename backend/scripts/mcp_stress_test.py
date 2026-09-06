"""End-to-end contract and usefulness checks for the Recall RAG MCP server.

Run with the FastAPI service already running:

    .venv/bin/python backend/scripts/mcp_stress_test.py

The runner intentionally uses the Streamable HTTP MCP client rather than
importing server functions. Every invocation therefore tests discovery,
structured output, server-side limits, and persistent audit logging.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any, Callable

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MCP_URL = "http://127.0.0.1:8010/mcp"


@dataclass(frozen=True)
class Case:
    name: str
    tool: str
    arguments: dict[str, Any]
    check: Callable[[dict[str, Any]], tuple[bool, str]]


def _success(payload: dict[str, Any]) -> tuple[bool, str]:
    return payload.get("status") == "success", f"status={payload.get('status')}"


def _document_contains(fragment: str) -> Callable[[dict[str, Any]], tuple[bool, str]]:
    def check(payload: dict[str, Any]) -> tuple[bool, str]:
        citations = payload.get("citations") or []
        haystack = " ".join(f"{item.get('title', '')} {item.get('excerpt', '')}" for item in citations).lower()
        passed = payload.get("status") == "success" and fragment.lower() in haystack
        return passed, f"{len(citations)} citation(s); expected '{fragment}'"

    return check


def _no_result(payload: dict[str, Any]) -> tuple[bool, str]:
    return payload.get("status") == "no_result" and not payload.get("citations"), f"status={payload.get('status')}"


def _bounded_documents(payload: dict[str, Any]) -> tuple[bool, str]:
    citations = payload.get("citations") or []
    return payload.get("status") == "success" and 1 <= len(citations) <= 8, f"returned={len(citations)}"


def _analytics_top(field: str, value: Any) -> Callable[[dict[str, Any]], tuple[bool, str]]:
    def check(payload: dict[str, Any]) -> tuple[bool, str]:
        evidence = payload.get("evidence") or {}
        rows = evidence.get("rows") or []
        top = rows[0] if rows else {}
        passed = payload.get("status") == "success" and top.get(field) == value and evidence.get("compiled_sql", "").startswith("SELECT ")
        return passed, f"top {field}={top.get(field)!r}; rows={len(rows)}"

    return check


def _honda_total(payload: dict[str, Any]) -> tuple[bool, str]:
    evidence = payload.get("evidence") or {}
    rows = evidence.get("rows") or []
    passed = payload.get("status") == "success" and len(rows) == 1 and rows[0].get("manufacturer") == "Honda (American Honda Motor Co.)" and rows[0].get("recall_campaigns") == 5
    return passed, f"rows={len(rows)}; Honda campaigns={rows[0].get('recall_campaigns') if rows else None}"


def _campaign_total(payload: dict[str, Any]) -> tuple[bool, str]:
    evidence = payload.get("evidence") or {}
    rows = evidence.get("rows") or []
    passed = payload.get("status") == "success" and len(rows) == 1 and rows[0].get("campaign_id") == "24V-118" and rows[0].get("recall_campaigns") == 1
    return passed, f"campaign={rows[0].get('campaign_id') if rows else None}; rows={len(rows)}"


def _filter_contains(field: str, value: Any) -> Callable[[dict[str, Any]], tuple[bool, str]]:
    def check(payload: dict[str, Any]) -> tuple[bool, str]:
        intent = payload.get("interpreted_intent") or {}
        filters = intent.get("filters") or []
        passed = payload.get("status") == "success" and any(item.get("field") == field and item.get("value") == value for item in filters)
        return passed, f"filters={filters}"

    return check


def _multi_manufacturer(payload: dict[str, Any]) -> tuple[bool, str]:
    evidence = payload.get("evidence") or {}
    rows = evidence.get("rows") or []
    names = {row.get("manufacturer") for row in rows}
    passed = payload.get("status") == "success" and names == {"Tesla, Inc.", "Ford Motor Company"} and (evidence.get("compiled_sql") or "").find("ANY") >= 0
    return passed, f"manufacturers={sorted(names)}; rows={len(rows)}"


def _warehouse_unfiltered(payload: dict[str, Any]) -> tuple[bool, str]:
    intent = payload.get("interpreted_intent") or {}
    filters = intent.get("filters") or []
    passed = payload.get("status") == "success" and not any(item.get("field") == "warehouse_id" for item in filters)
    return passed, f"warehouse filters={[item for item in filters if item.get('field') == 'warehouse_id']}"


def _warehouse_comparison(payload: dict[str, Any]) -> tuple[bool, str]:
    evidence = payload.get("evidence") or {}
    rows = evidence.get("rows") or []
    warehouses = {row.get("warehouse_id") for row in rows}
    passed = payload.get("status") == "success" and warehouses == {"DFW-01", "HOU-01"} and all("warehouse_id" in row for row in rows)
    return passed, f"warehouses={sorted(warehouses)}; rows={len(rows)}"


def _structured_query(payload: dict[str, Any]) -> tuple[bool, str]:
    evidence = payload.get("evidence") or {}
    intent = payload.get("interpreted_intent") or {}
    passed = payload.get("status") == "success" and intent.get("model") == "recall_intelligence" and bool(evidence.get("rows")) and evidence.get("compiled_sql", "").startswith("SELECT ")
    return passed, f"model={intent.get('model')}; rows={len(evidence.get('rows') or [])}"


def _clarification(payload: dict[str, Any]) -> tuple[bool, str]:
    return payload.get("status") == "clarification_required", f"status={payload.get('status')}; message={payload.get('message')}"


def _stockout_dallas(payload: dict[str, Any]) -> tuple[bool, str]:
    evidence = payload.get("evidence") or {}
    rows = evidence.get("rows") or []
    passed = bool(rows) and all(row.get("warehouse_id") == "DFW-01" and row.get("urgency_tier") == "critical" for row in rows)
    return payload.get("status") == "success" and passed, f"rows={len(rows)}; all DFW critical={passed}"


def _memphis_to_dallas(payload: dict[str, Any]) -> tuple[bool, str]:
    evidence = payload.get("evidence") or {}
    rows = evidence.get("rows") or []
    passed = bool(rows) and all(
        row.get("source_warehouse_id") == "MEM-02"
        and row.get("destination_warehouse_id") == "DFW-01"
        and float(row.get("recommended_transfer_units") or 0) >= 0
        for row in rows
    )
    return payload.get("status") == "success" and passed, f"rows={len(rows)}; all MEM→DFW={passed}"


def _demand_is_ordered(payload: dict[str, Any]) -> tuple[bool, str]:
    evidence = payload.get("evidence") or {}
    rows = evidence.get("rows") or []
    values = [row.get("projected_recall_demand_units", 0) for row in rows]
    passed = bool(rows) and values == sorted(values, reverse=True)
    return payload.get("status") == "success" and passed, f"rows={len(rows)}; sorted desc={passed}"


def _refused(payload: dict[str, Any]) -> tuple[bool, str]:
    return payload.get("status") == "refused", f"status={payload.get('status')}; message={payload.get('message')}"


def _status_counts(payload: dict[str, Any]) -> tuple[bool, str]:
    sources = ((payload.get("data") or {}).get("sources") or {})
    nhtsa = sources.get("nhtsa_recalls") or {}
    documents = sources.get("document_index") or {}
    passed = payload.get("status") == "success" and (nhtsa.get("record_count") or 0) >= 40 and (documents.get("record_count") or 0) >= 40
    return passed, f"nhtsa={nhtsa.get('record_count')}; documents={documents.get('record_count')}"


CASES = [
    Case("public remedy guidance", "search_recall_documents", {"query": "What does NHTSA say about remedies for a safety defect?"}, _document_contains("remedy")),
    Case("park-outside advisory", "search_recall_documents", {"query": "Which recall says to park vehicles outside?"}, _document_contains("23V-441")),
    Case("normalized campaign ID", "search_recall_documents", {"query": "What safety consequence is listed for campaign 24V118?"}, _document_contains("24V-118")),
    Case("component retrieval", "search_recall_documents", {"query": "Which Ford recall concerns windshield wiper linkages?"}, _document_contains("22V250000")),
    Case("hard negative", "search_recall_documents", {"query": "Which campaign mentions a xylophonic krypton actuator?"}, _no_result),
    Case("document limit clamp", "search_recall_documents", {"query": "What does NHTSA say about remedies?", "limit": 99}, _bounded_documents),
    Case("manufacturer ranking", "query_recall_analytics", {"question": "Which manufacturer has the most recall campaigns?"}, _analytics_top("manufacturer", "Tesla, Inc.")),
    Case("named manufacturer total", "query_recall_analytics", {"question": "How many recall campaigns does Honda have?"}, _honda_total),
    Case("dashed campaign analytics", "query_recall_analytics", {"question": "How many recalls are in campaign 24V-118?"}, _campaign_total),
    Case("model-year ranking", "query_recall_analytics", {"question": "Which model year has the most recalls?"}, _analytics_top("model_year", 2022)),
    Case("explicit model year filter", "query_recall_analytics", {"question": "How many recalls affected 2020 model year vehicles?"}, _filter_contains("model_year", 2020)),
    Case("explicit report year filter", "query_recall_analytics", {"question": "How many campaigns were reported in 2024?"}, _filter_contains("report_date", "2024-01-01")),
    Case("multi-manufacturer filter", "query_recall_analytics", {"question": "Compare Tesla and Ford recall campaigns."}, _multi_manufacturer),
    Case("multi-warehouse comparison", "query_recall_analytics", {"question": "Compare stockout risk between Dallas and Houston."}, _warehouse_comparison),
    Case("warehouse word is not Houston", "query_recall_analytics", {"question": "What is demand across all warehouses?"}, _warehouse_unfiltered),
    Case("Dallas critical stockout", "query_recall_analytics", {"question": "What critical parts are at risk of stockout in Dallas?"}, _stockout_dallas),
    Case("Memphis transfer candidates", "query_recall_analytics", {"question": "Can Memphis cover Dallas shortages?"}, _memphis_to_dallas),
    Case("recall part demand", "query_recall_analytics", {"question": "Which parts have the highest projected demand?"}, _demand_is_ordered),
    Case("out-of-scope refusal", "query_recall_analytics", {"question": "What will the weather be tomorrow in Dallas?"}, _refused),
    Case("ambiguous transfer clarification", "query_recall_analytics", {"question": "Can we cover the Dallas shortage?"}, _clarification),
    Case("ambiguous source clarification", "query_recall_analytics", {"question": "Which warehouses can cover Memphis shortages?"}, _clarification),
    Case("unknown manufacturer refusal", "query_recall_analytics", {"question": "How many recall campaigns does Zzyzx Motors have?"}, _refused),
    Case(
        "structured semantic query",
        "query_recall_analytics",
        {
            "question": "Return the governed recall ranking.",
            "semantic_query": {
                "model": "recall_intelligence",
                "metrics": ["recall_campaigns"],
                "dimensions": ["manufacturer"],
                "filters": [],
                "order_by": [{"field": "recall_campaigns", "direction": "desc"}],
                "limit": 5,
            },
        },
        _structured_query,
    ),
    Case("status visibility", "get_data_status", {"scope": "all"}, _status_counts),
]


async def run() -> int:
    async with streamable_http_client(MCP_URL) as streams:
        read, write, *_ = streams
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            advertised = {tool.name: tool.inputSchema for tool in tools.tools}
            required = {"search_recall_documents", "query_recall_analytics", "get_data_status"}
            discovery_ok = required == set(advertised) and "sql" not in advertised["query_recall_analytics"].get("properties", {})
            print(f"DISCOVERY {'PASS' if discovery_ok else 'FAIL'} · {', '.join(sorted(advertised))}")
            passed = int(discovery_ok)

            for case in CASES:
                result = await session.call_tool(case.tool, case.arguments)
                payload = result.structuredContent or {}
                ok, detail = case.check(payload)
                passed += int(ok)
                print(f"{'PASS' if ok else 'FAIL'} · {case.name}: {detail}")
                if not ok:
                    print(json.dumps(payload, indent=2, default=str)[:1600])

    total = len(CASES) + 1
    print(f"RESULT {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
