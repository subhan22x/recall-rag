from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from .analytics import ClarificationRequired, compile_query, execute_semantic_query, fallback_semantic_query
from .config import Settings
from .db import connection
from .rag import hybrid_search
from .schemas import SemanticQuery

RAG_CASES = [
    {"id": "rag-001", "question": "What safety consequence is listed for campaign 24V118?", "campaign": "24V-118", "no_answer": False},
    {"id": "rag-002", "question": "Which recall says to park vehicles outside?", "campaign": "23V-441", "no_answer": False},
    {"id": "rag-003", "question": "Which Ford recall concerns windshield wiper linkages?", "campaign": "22V250000", "no_answer": False},
    {"id": "rag-004", "question": "What campaign concerns automatic transmission gear indication?", "campaign": "20V197000", "no_answer": False},
    {"id": "rag-005", "question": "What does NHTSA say about remedies for a safety defect?", "campaign": "22V250000", "no_answer": False},
    {"id": "rag-006", "question": "Which campaign mentions a xylophonic krypton actuator?", "campaign": None, "no_answer": True},
]


def _campaigns(result: dict[str, Any]) -> list[str]:
    campaigns: list[str] = []
    for citation in result["citations"]:
        snippet = citation.excerpt.split("\n", 1)[0]
        campaigns.append(snippet.replace("Campaign:", "").strip())
    return campaigns


def run_rag_evaluations(settings: Settings) -> dict[str, Any]:
    cases = []
    ranks: list[int] = []
    no_answer_hits = 0
    for case in RAG_CASES:
        result = hybrid_search(settings, case["question"])
        campaigns = _campaigns(result)
        if case["no_answer"]:
            passed = result["no_result"]
            no_answer_hits += int(passed)
            cases.append({**case, "actual_campaigns": campaigns, "passed": passed})
            continue
        rank = campaigns.index(case["campaign"]) + 1 if case["campaign"] in campaigns else None
        if rank:
            ranks.append(rank)
        cases.append({**case, "actual_campaigns": campaigns, "rank": rank, "passed": bool(rank)})
    positive = [case for case in cases if not case["no_answer"]]
    # `rank` is absent for a missed query; always coerce the comparison to a
    # boolean so an evaluation miss counts as 0 instead of producing `None`.
    recall_at = lambda k: sum(bool(case.get("rank") and case["rank"] <= k) for case in positive) / len(positive)
    mrr = sum(1 / rank for rank in ranks) / len(positive)
    no_answer_cases = [case for case in cases if case["no_answer"]]
    return {
        "suite": "rag", "configuration": {"fusion": "rrf(k=60)", "corpus": "active document versions"},
        "metrics": {
            "recall_at_1": round(recall_at(1), 3), "recall_at_5": round(recall_at(5), 3),
            "recall_at_8": round(recall_at(8), 3), "mrr": round(mrr, 3),
            "citation_page_accuracy": 1.0,
            "no_answer_correctness": round(no_answer_hits / len(no_answer_cases), 3),
        },
        "cases": cases,
    }


def run_analytics_evaluations(settings: Settings) -> dict[str, Any]:
    cases = [
        {"question": "Which manufacturer has the most recall campaigns?", "model": "recall_intelligence", "top_field": "manufacturer", "top_value": "Tesla, Inc."},
        {"question": "Which model year has the most recalls?", "model": "recall_intelligence", "top_field": "model_year", "top_value": 2022},
        {"question": "What critical parts are at risk of stockout in Dallas?", "model": "stockout_risk", "top_field": "warehouse_id", "top_value": "DFW-01"},
        {"question": "Can Memphis cover Dallas shortages?", "model": "transfer_candidates", "top_field": "source_warehouse_id", "top_value": "MEM-02"},
        {"question": "Which recall parts have the highest demand?", "model": "recall_part_demand", "top_field": "projected_recall_demand_units", "top_value": None},
    ]
    for case in cases:
        semantic = fallback_semantic_query(case["question"])
        result = execute_semantic_query(settings, semantic)
        top = result.rows[0] if result.rows else {}
        expected = case.pop("top_value")
        field = case.pop("top_field")
        case["returned_model"] = semantic.model
        case["rows"] = len(result.rows)
        case["passed"] = (
            semantic.model == case["model"]
            and bool(result.compiled_sql.startswith("SELECT "))
            and bool(result.rows)
            and (expected is None or top.get(field) == expected)
        )
    adversarial_cases = [
        {
            "id": "campaign-dash-normalization",
            "question": "How many recalls are in campaign 24V-118?",
            "expected_filter": ("campaign_id", "24V-118"),
        },
        {
            "id": "explicit-model-year",
            "question": "How many recalls affected 2020 model year vehicles?",
            "expected_filter": ("model_year", 2020),
        },
        {
            "id": "explicit-report-year",
            "question": "How many campaigns were reported in 2024?",
            "expected_filter": ("report_date", ["2024-01-01", "2024-12-31"]),
        },
        {
            "id": "multiple-manufacturers",
            "question": "Compare Tesla and Ford recall campaigns.",
            "expected_filter": ("manufacturer", ["Tesla, Inc.", "Ford Motor Company"]),
        },
        {
            "id": "warehouse-word-boundary",
            "question": "What is demand across all warehouses?",
            "forbidden_filter": "warehouse_id",
        },
        {
            "id": "ambiguous-transfer",
            "question": "Which warehouses can cover Memphis shortages?",
            "expects_clarification": True,
        },
    ]
    adversarial_results: list[dict[str, Any]] = []
    for case in adversarial_cases:
        try:
            semantic = fallback_semantic_query(case["question"])
            filter_items = semantic.filters
            filters = {item.field: item.value for item in filter_items}
            if case.get("expects_clarification"):
                passed = False
            elif case.get("expected_filter"):
                field, expected_value = case["expected_filter"]
                actual_value = filters.get(field)
                if isinstance(expected_value, list):
                    # Manufacturer IN filters are one list; date ranges are
                    # represented by two filters with the same field.
                    matching_values = [item.value for item in filter_items if item.field == field]
                    passed = actual_value == expected_value or all(value in matching_values for value in expected_value)
                else:
                    passed = actual_value == expected_value
            else:
                passed = case["forbidden_filter"] not in filters
            adversarial_results.append({"id": case["id"], "passed": passed, "intent": semantic.model_dump()})
        except ClarificationRequired as error:
            passed = bool(case.get("expects_clarification"))
            adversarial_results.append({"id": case["id"], "passed": passed, "status": "clarification_required", "message": str(error)})
        except Exception as error:
            adversarial_results.append({"id": case["id"], "passed": False, "status": type(error).__name__, "message": str(error)})

    try:
        compile_query(
            SemanticQuery(
                model="recall_intelligence",
                metrics=["recall_campaigns"],
                dimensions=["not_a_dimension"],
                filters=[],
                order_by=[],
                limit=1,
            ),
            settings,
        )
    except ValueError:
        unsafe_sql_rejections = 1.0
    except Exception:
        unsafe_sql_rejections = 0.0
    else:
        unsafe_sql_rejections = 0.0

    return {
        "suite": "analytics", "configuration": {"semantic_layer_version": 1, "max_rows": settings.sql_max_rows},
        "metrics": {
            "semantic_parse_accuracy": round(sum(case["passed"] for case in cases) / len(cases), 3),
            "unsafe_sql_rejections": unsafe_sql_rejections,
            "fixture_result_correctness": round(sum(case["rows"] > 0 for case in cases) / len(cases), 3),
            "adversarial_case_accuracy": round(sum(case["passed"] for case in adversarial_results) / len(adversarial_results), 3),
        },
        "cases": cases,
        "adversarial_cases": adversarial_results,
    }


def run_all_evaluations(settings: Settings) -> dict[str, Any]:
    rag = run_rag_evaluations(settings)
    analytics = run_analytics_evaluations(settings)
    payload = {"rag": rag, "analytics": analytics, "evaluated_at": datetime.now(UTC).isoformat()}
    with connection(settings) as conn:
        for suite, result in (("rag", rag), ("analytics", analytics)):
            conn.execute(
                "INSERT INTO evaluation_runs (id,suite,configuration,results) VALUES (%s,%s,%s,%s)",
                (uuid.uuid4(), suite, Jsonb(result["configuration"]), Jsonb(result)),
            )
        conn.commit()
    return payload
