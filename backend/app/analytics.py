from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import re
from typing import Any

from psycopg import sql
from sqlglot import exp, parse_one

from .config import Settings
from .db import connection
from .schemas import AnalyticsEvidence, OrderBy, SemanticFilter, SemanticQuery
from .semantic import load_semantic_layer


@dataclass
class CompiledQuery:
    sql_text: str
    params: list[Any]
    semantic_terms: list[str]
    relation: str


def _model_spec(query: SemanticQuery) -> tuple[dict[str, Any], dict[str, Any]]:
    layer = load_semantic_layer()
    spec = layer["models"].get(query.model)
    if not spec:
        raise ValueError(f"Unsupported semantic model: {query.model}")
    return layer, spec


def _valid_field(field: str, spec: dict[str, Any]) -> bool:
    return field in spec.get("metrics", {}) or field in spec.get("dimensions", [])


def compile_query(query: SemanticQuery, settings: Settings) -> CompiledQuery:
    layer, spec = _model_spec(query)
    allowed_models = layer.get("policies", {}).get("allowed_models", [])
    if query.model not in allowed_models:
        raise ValueError("Semantic model is not in the allowed-model policy")
    metrics = spec.get("metrics", {})
    dimensions = spec.get("dimensions", [])
    if any(metric not in metrics for metric in query.metrics):
        raise ValueError("Requested metric is not defined for this model")
    if any(dimension not in dimensions for dimension in query.dimensions):
        raise ValueError("Requested dimension is not defined for this model")
    if any(not _valid_field(item.field, spec) for item in query.filters):
        raise ValueError("Filter field is not defined for this model")
    if any(not _valid_field(item.field, spec) for item in query.order_by):
        raise ValueError("Order-by field is not defined for this model")

    relation = spec["relation"]
    # All SQL fragments below come from the checked-in YAML or Pydantic-enforced
    # identifiers. User/model values remain bound parameters.
    fields = [sql.SQL(metrics[metric]).as_string() + sql.SQL(" AS ").as_string() + sql.Identifier(metric).as_string() for metric in query.metrics]
    fields.extend(sql.Identifier(dimension).as_string() for dimension in query.dimensions)
    select_sql = ", ".join(fields)
    group_sql = f" GROUP BY {', '.join(sql.Identifier(dimension).as_string() for dimension in query.dimensions)}" if query.dimensions else ""
    clauses: list[str] = []
    params: list[Any] = []
    for item in query.filters:
        identifier = sql.Identifier(item.field).as_string()
        if item.operator == "in":
            if not isinstance(item.value, list) or not item.value:
                raise ValueError("An IN filter must include at least one value")
            if item.field == "campaign_id":
                clauses.append("replace(campaign_id, '-', '') = ANY(%s)")
                params.append([str(value).replace("-", "") for value in item.value])
            else:
                clauses.append(f"{identifier} = ANY(%s)")
                params.append(item.value)
        else:
            if item.field == "campaign_id" and item.operator == "=":
                clauses.append("replace(campaign_id, '-', '') = replace(%s, '-', '')")
                params.append(item.value)
            else:
                clauses.append(f"{identifier} {item.operator} %s")
                params.append(item.value)
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    order_sql = ""
    if query.order_by:
        parts = [f"{sql.Identifier(item.field).as_string()} {item.direction.upper()}" for item in query.order_by]
        order_sql = " ORDER BY " + ", ".join(parts)
    limit = min(query.limit, settings.sql_max_rows)
    compiled = f"SELECT {select_sql} FROM {relation}{where_sql}{group_sql}{order_sql} LIMIT {limit}"
    validate_compiled_sql(compiled, relation)
    return CompiledQuery(compiled, params, [*query.metrics, *query.dimensions], relation)


def validate_compiled_sql(sql_text: str, relation: str) -> None:
    if ";" in sql_text or "--" in sql_text or "/*" in sql_text:
        raise ValueError("Unsafe SQL syntax")
    expression = parse_one(sql_text, dialect="postgres")
    if not isinstance(expression, exp.Select):
        raise ValueError("Only one SELECT statement is permitted")
    tables = list(expression.find_all(exp.Table))
    if len(tables) != 1:
        raise ValueError("Exactly one approved mart relation is permitted")
    table = tables[0]
    actual_relation = f"{table.db}.{table.name}" if table.db else table.name
    if actual_relation != relation:
        raise ValueError("Query relation is outside the semantic layer")


class UnsupportedAnalyticsQuestion(ValueError):
    """Raised when a question cannot be mapped to a governed mart."""


class ClarificationRequired(ValueError):
    """Raised when a write-free answer needs an explicit business direction."""


WAREHOUSES = {
    "dallas": "DFW-01",
    "dfw": "DFW-01",
    "houston": "HOU-01",
    "hou": "HOU-01",
    "austin": "AUS-01",
    "aus": "AUS-01",
    "san antonio": "SAT-01",
    "sat": "SAT-01",
    "memphis": "MEM-02",
    "mem": "MEM-02",
    "phoenix": "PHX-01",
    "phx": "PHX-01",
}

MANUFACTURERS = {
    "tesla": ["Tesla, Inc."],
    "ford": ["Ford Motor Company"],
    "honda": ["Honda (American Honda Motor Co.)"],
    "toyota": ["Toyota Motor Engineering", "Toyota Motor Engineering & Manufacturing"],
    "gm": ["General Motors LLC", "General Motors, LLC"],
    "general motors": ["General Motors LLC", "General Motors, LLC"],
}


_LOCATION_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(label) for label in sorted(WAREHOUSES, key=len, reverse=True)) + r")(?![a-z0-9])",
    re.IGNORECASE,
)


def _warehouse_from_question(lowered: str) -> str | None:
    mentions = _warehouse_mentions(lowered)
    if mentions:
        return mentions[0][1]
    match = re.search(r"\b(?:DFW|HOU|AUS|SAT|MEM|PHX)-?0[12]\b", lowered, re.IGNORECASE)
    return match.group(0).upper() if match else None


def _warehouse_mentions(lowered: str) -> list[tuple[str, str]]:
    return [(match.group(0).lower(), WAREHOUSES[match.group(0).lower()]) for match in _LOCATION_PATTERN.finditer(lowered)]


def _transfer_route(lowered: str) -> tuple[str, str] | None:
    """Extract source and destination from common natural-language forms."""

    locations = _warehouse_mentions(lowered)
    route_patterns = (
        r"\bfrom\s+(?P<source>[a-z ]+?)\s+(?:to|into|toward)\s+(?P<destination>[a-z ]+?)(?=[\s,.;:!?)]|$)",
        r"(?P<source>[a-z ]+?)\s+cover(?:s)?\s+(?:a\s+)?(?:the\s+)?(?:shortage|shortages)?\s*(?:in|at|for|of)\s+(?P<destination>[a-z ]+?)(?=[\s,.;:!?)]|$)",
        r"(?P<source>[a-z ]+?)\s+cover(?:s)?\s+(?P<destination>[a-z ]+?)(?:\s+shortage|\s+shortages)(?=[\s,.;:!?)]|$)",
    )
    for pattern in route_patterns:
        match = re.search(pattern, lowered)
        if not match:
            continue
        source_text = match.group("source").strip()
        destination_text = match.group("destination").strip()
        source_mentions = _warehouse_mentions(source_text)
        destination_mentions = _warehouse_mentions(destination_text)
        source = source_mentions[0][1] if source_mentions else None
        destination = destination_mentions[0][1] if destination_mentions else None
        if source and destination and source != destination:
            return source, destination
    if len(locations) >= 2 and any(term in lowered for term in ("cover", "transfer", "move", "rebalanc")):
        # If two locations are named without a direction marker, refuse to
        # guess. Silent reversal is materially worse than a short clarification.
        raise ClarificationRequired("Please specify the transfer direction, for example 'from Memphis to Dallas'.")
    return None


def _manufacturer_filter(lowered: str) -> SemanticFilter | None:
    matched: list[str] = []
    for label, known_values in MANUFACTURERS.items():
        if re.search(rf"\b{re.escape(label)}\b", lowered):
            for value in known_values:
                if value not in matched:
                    matched.append(value)
    return SemanticFilter(field="manufacturer", operator="in", value=matched) if matched else None


def _unknown_named_manufacturer(lowered: str) -> str | None:
    """Find an explicitly named manufacturer so it is not silently dropped."""
    match = re.search(
        r"\b(?:does|did)\s+(?P<name>[a-z][a-z0-9 .&'()-]{1,80}?)\s+(?:have|report|list|recall|campaign)",
        lowered,
    )
    return " ".join(match.group("name").split()) if match else None


def _campaign_from_question(question: str) -> str | None:
    match = re.search(r"\b\d{2}V-?\d{3,}\b", question, re.IGNORECASE)
    return match.group(0).upper() if match else None


def _explicit_year(question: str) -> int | None:
    match = re.search(r"\b(?:19|20)\d{2}\b", question)
    return int(match.group(0)) if match else None


def _urgency_filter(lowered: str) -> list[SemanticFilter]:
    return [SemanticFilter(field="urgency_tier", operator="=", value="critical")] if "critical" in lowered else []


def fallback_semantic_query(question: str) -> SemanticQuery:
    """Map common business questions to a small, explicit semantic contract.

    This is deliberately deterministic: an LLM can choose the analytics tool,
    but it never supplies raw SQL or identifiers.  The mapper only emits terms
    approved in ``semantic_layer.yml`` and refuses questions outside the marts.
    """

    lowered = question.lower().strip()
    if len(lowered) < 3:
        raise UnsupportedAnalyticsQuestion("Please ask a specific question about recall, demand, stockout, or transfer data.")

    warehouse = _warehouse_from_question(lowered)
    campaign_id = _campaign_from_question(question)
    explicit_year = _explicit_year(question)
    transfer_terms = ("transfer", "move inventory", "rebalanc", "cover a shortage", "cover dallas")
    stockout_terms = ("stockout", "shortage", "run out", "at risk", "days of cover", "available inventory", "available units")
    demand_terms = ("demand", "customer", "compatible", "parts needed", "part demand", "exposure")
    recall_terms = ("recall", "campaign", "manufacturer", "make", "model year", "component", "advisory", "do not drive", "park outside")

    is_transfer_question = any(term in lowered for term in transfer_terms) or ("cover" in lowered and "shortage" in lowered)
    if is_transfer_question:
        route = _transfer_route(lowered)
        if route:
            source, destination = route
        elif len(_warehouse_mentions(lowered)) == 1:
            # “Can we cover Dallas?” has no source and must not pick one based
            # on dictionary order.
            raise ClarificationRequired("Please name both warehouses and the direction, for example 'from Memphis to Dallas'.")
        else:
            source, destination = "MEM-02", warehouse or "DFW-01"
        filters = [
            SemanticFilter(field="destination_warehouse_id", operator="=", value=destination),
            SemanticFilter(field="source_warehouse_id", operator="=", value=source),
        ]
        if "critical" in lowered:
            filters.extend(_urgency_filter(lowered))
        return SemanticQuery(
            model="transfer_candidates",
            metrics=["recommended_transfer_units", "candidate_transfers"],
            dimensions=["campaign_id", "sku", "destination_warehouse_id", "source_warehouse_id", "urgency_tier"],
            filters=filters,
            order_by=[OrderBy(field="recommended_transfer_units", direction="desc")],
            limit=25,
        )

    if any(term in lowered for term in stockout_terms):
        filters = _urgency_filter(lowered)
        locations = [value for _, value in _warehouse_mentions(lowered)]
        if len(locations) > 1:
            filters.insert(0, SemanticFilter(field="warehouse_id", operator="in", value=list(dict.fromkeys(locations))))
        elif warehouse:
            filters.insert(0, SemanticFilter(field="warehouse_id", operator="=", value=warehouse))
        dimensions = ["campaign_id", "warehouse_id", "sku", "product_category", "urgency_tier"]
        if any(term in lowered for term in ("total", "overall", "how many", "number of", "count of")) and "compar" not in lowered:
            dimensions = []
        elif "compar" in lowered and len(locations) > 1:
            dimensions = ["warehouse_id"]
        return SemanticQuery(
            model="stockout_risk",
            metrics=["shortage_units", "at_risk_skus", "available_units"],
            dimensions=dimensions,
            filters=filters,
            order_by=[OrderBy(field="shortage_units", direction="desc")],
            limit=25,
        )

    if any(term in lowered for term in demand_terms):
        filters = _urgency_filter(lowered)
        locations = [value for _, value in _warehouse_mentions(lowered)]
        if len(locations) > 1:
            filters.insert(0, SemanticFilter(field="warehouse_id", operator="in", value=list(dict.fromkeys(locations))))
        elif warehouse:
            filters.insert(0, SemanticFilter(field="warehouse_id", operator="=", value=warehouse))
        if campaign_id:
            filters.append(SemanticFilter(field="campaign_id", operator="=", value=campaign_id))
        return SemanticQuery(
            model="recall_part_demand",
            metrics=["projected_recall_demand_units", "exposed_customer_vehicles"],
            dimensions=["campaign_id", "warehouse_id", "sku", "product_category", "urgency_tier"],
            filters=filters,
            order_by=[OrderBy(field="projected_recall_demand_units", direction="desc")],
            limit=25,
        )

    if any(term in lowered for term in recall_terms) or campaign_id:
        filters = _urgency_filter(lowered)
        if campaign_id:
            filters.append(SemanticFilter(field="campaign_id", operator="=", value=campaign_id))
        model_year_filter = explicit_year is not None and (
            "model year" in lowered or "model-year" in lowered or "vehicles" in lowered
        )
        if model_year_filter:
            filters.append(SemanticFilter(field="model_year", operator="=", value=explicit_year))
            dimensions = [] if any(term in lowered for term in ("how many", "number of", "count of", "total")) else ["model_year"]
        elif explicit_year and any(term in lowered for term in ("reported", "reported in", "during", "in ")):
            filters.extend([
                SemanticFilter(field="report_date", operator=">=", value=f"{explicit_year}-01-01"),
                SemanticFilter(field="report_date", operator="<=", value=f"{explicit_year}-12-31"),
            ])
            dimensions = ["manufacturer"] if any(term in lowered for term in ("manufacturer", "company", "make")) else []
        elif "this year" in lowered or "current year" in lowered:
            year = datetime.now(UTC).year
            filters.extend([
                SemanticFilter(field="report_date", operator=">=", value=f"{year}-01-01"),
                SemanticFilter(field="report_date", operator="<=", value=datetime.now(UTC).date().isoformat()),
            ])
            dimensions = []
        elif "model year" in lowered:
            dimensions = ["model_year"]
        elif any(term in lowered for term in ("manufacturer", "company", "make")):
            dimensions = ["manufacturer"]
        elif "component" in lowered or "brake" in lowered or "wiper" in lowered:
            dimensions = ["component"]
        elif campaign_id:
            dimensions = ["campaign_id", "manufacturer", "component", "urgency_tier"]
        else:
            dimensions = ["manufacturer", "component", "urgency_tier"]
        manufacturer_filter = _manufacturer_filter(lowered)
        unknown_manufacturer = _unknown_named_manufacturer(lowered)
        if unknown_manufacturer and not manufacturer_filter:
            raise UnsupportedAnalyticsQuestion(
                f"I couldn't match '{unknown_manufacturer}' to a manufacturer in the approved recall data."
            )
        if manufacturer_filter:
            filters.append(manufacturer_filter)
            # A named manufacturer question should return the named entity,
            # not a global ranking where it might be absent from the limit.
            if any(phrase in lowered for phrase in ("how many", "number of", "count of", "compar")):
                dimensions = ["manufacturer"]
        return SemanticQuery(
            model="recall_intelligence",
            metrics=["recall_campaigns", "critical_campaigns"],
            dimensions=dimensions,
            filters=filters,
            order_by=[OrderBy(field="recall_campaigns", direction="desc")],
            limit=25,
        )

    raise UnsupportedAnalyticsQuestion(
        "This tool only answers governed questions about recall campaigns, parts demand, stockout risk, or transfer candidates."
    )


def execute_semantic_query(settings: Settings, query: SemanticQuery) -> AnalyticsEvidence:
    compiled = compile_query(query, settings)
    with connection(settings, settings.analytics_database_url) as conn:
        # The transaction-level guard prevents writes even when a caller somehow
        # bypasses the compiler. Production can point this function at a separate
        # read-only DATABASE URL.
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '3000ms'")
        rows = [_json_safe_row(dict(row)) for row in conn.execute(compiled.sql_text, compiled.params).fetchall()]
        freshness = conn.execute(
            "SELECT max(source_refreshed_at) AS refreshed_at FROM " + compiled.relation
        ).fetchone()["refreshed_at"]
    return AnalyticsEvidence(
        query_id=str(uuid.uuid4()), columns=list(rows[0].keys()) if rows else [], rows=rows,
        compiled_sql=compiled.sql_text, semantic_terms=compiled.semantic_terms,
        data_freshness=freshness.isoformat() if freshness else None,
    )


def _json_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert database numerics/dates into compact JSON-native values."""

    normalized: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            normalized[key] = int(value) if value == value.to_integral_value() else round(float(value), 2)
        elif hasattr(value, "isoformat"):
            normalized[key] = value.isoformat()
        else:
            normalized[key] = value
    return normalized
