from __future__ import annotations

import json
import re
import uuid
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from .analytics import execute_semantic_query, fallback_semantic_query
from .config import Settings
from .rag import hybrid_search
from .schemas import AnalyticsEvidence, AssistantResponse, Citation, RetrievalTrace, SemanticQuery
from .semantic import public_semantic_layer


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: Literal["documents", "analytics", "combined", "status", "general", "clarify"]


class AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer_markdown: str = Field(min_length=1)
    # OpenAI-compatible strict JSON Schema providers require every declared
    # property to be required. The prompt therefore requires empty arrays when
    # no citations or warnings apply.
    citation_ids: list[str]
    warnings: list[str]


class AgentState(TypedDict, total=False):
    question: str
    route: str
    citations: list[Citation]
    analytics: AnalyticsEvidence | None
    retrieval_trace: RetrievalTrace | None
    warnings: list[str]
    tool_trace: list[str]
    response: AssistantResponse


class ModelAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            default_headers={"HTTP-Referer": "http://localhost:5173", "X-OpenRouter-Title": "Recall RAG"} if settings.openai_base_url else None,
        ) if settings.model_enabled else None

    def structured(self, prompt: str, model: type[BaseModel]) -> BaseModel | None:
        if not self.client:
            return None
        try:
            response = self.client.responses.create(
                model=self.settings.openai_model,
                input=prompt,
                text={"format": {"type": "json_schema", "name": model.__name__.lower(), "strict": True, "schema": model.model_json_schema()}},
            )
            return model.model_validate_json(response.output_text)
        except Exception:
            # Never turn a provider/configuration problem into an ungrounded answer.
            return None


class RecallRagAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = ModelAdapter(settings)
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("route", self._route)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("analytics", self._analytics)
        graph.add_node("status", self._status)
        graph.add_node("compose", self._compose)
        graph.add_edge(START, "route")
        graph.add_conditional_edges("route", lambda state: state["route"], {
            "documents": "retrieve", "analytics": "analytics", "combined": "analytics", "status": "status", "general": "compose", "clarify": "compose",
        })
        graph.add_edge("retrieve", "compose")
        graph.add_conditional_edges("analytics", lambda state: "retrieve" if state["route"] == "combined" else "compose", {"retrieve": "retrieve", "compose": "compose"})
        graph.add_edge("status", "compose")
        graph.add_edge("compose", END)
        return graph.compile()

    def _fallback_route(self, question: str) -> str:
        lowered = question.lower()
        # Distinguish an imperative action (which this demo must decline) from
        # an analytical question such as “can Memphis transfer inventory?”
        # The latter is exactly what the read-only transfer-candidates mart is
        # intended to answer.
        write_patterns = (
            r"^\s*move\s+inventory\b",
            r"^\s*transfer\s+inventory\s+from\b",
            r"^\s*(?:please\s+)?create\s+(?:an\s+)?transfer\b",
            r"^\s*update\s+inventory\b",
            r"^\s*change\s+inventory\b",
        )
        if any(re.search(pattern, lowered) for pattern in write_patterns):
            return "clarify"
        if any(term in lowered for term in ("current", "fresh", "updated", "status")):
            return "status"
        document_terms = ("what does", "consequence", "remedy", "notice", "say", "nhtsa", "campaign")
        # A city name by itself is not an operational question. Keep location
        # terms out of the routing trigger so “weather in Dallas” falls through
        # to the permitted general-answer behavior instead of analytics.
        analytics_terms = ("how many", "stockout", "shortage", "transfer", "demand", "inventory", "warehouse", "cover")
        docs = any(term in lowered for term in document_terms)
        analytics = any(term in lowered for term in analytics_terms)
        if docs and analytics:
            return "combined"
        if analytics:
            return "analytics"
        if docs:
            return "documents"
        return "general"

    def _route(self, state: AgentState) -> dict[str, Any]:
        prompt = (
            "Classify the Recall RAG user question into exactly one route. documents = wording from recall/guidance; "
            "analytics = numerical structured-data question; combined = both; status = freshness/source status; "
            "general = ordinary question that does not need Recall RAG data; clarify = request needs a write action. "
            "Do not classify ordinary questions as clarify.\nQuestion: " + state["question"]
        )
        decision = self.model.structured(prompt, RouteDecision)
        deterministic_route = self._fallback_route(state["question"])
        # Known domain intents must never be downgraded to a generic answer
        # just because a probabilistic classifier misses a familiar phrase.
        # The model may still classify genuinely general questions, but recall,
        # warehouse, and write-request routes remain deterministic.
        route = deterministic_route if deterministic_route != "general" else (decision.route if decision else "general")
        return {"route": route, "citations": [], "analytics": None, "retrieval_trace": None, "warnings": [], "tool_trace": []}

    def _retrieve(self, state: AgentState) -> dict[str, Any]:
        query = state["question"]
        filters: dict[str, str] | None = None
        # A mixed operational question should not make retrieval infer a link
        # between generic stockout language and a recall notice. Analytics is
        # computed first; then its highest-priority campaign is used as a
        # deterministic document filter for the public-source portion.
        analytics = state.get("analytics")
        if state["route"] == "combined" and analytics and analytics.rows:
            campaign_id = next((str(row["campaign_id"]) for row in analytics.rows if row.get("campaign_id")), None)
            if campaign_id:
                query = f"What safety consequence and remedy does NHTSA report for campaign {campaign_id}?"
                filters = {"campaign_id": campaign_id}
        result = hybrid_search(self.settings, query, filters=filters)
        warnings = list(state["warnings"])
        if result["no_result"]:
            warnings.append("No indexed public source directly verified this part of the question.")
        return {"citations": result["citations"], "retrieval_trace": RetrievalTrace.model_validate(result["configuration"]), "warnings": warnings, "tool_trace": [*state["tool_trace"], "search_recall_documents"]}

    def _semantic_from_model(self, question: str) -> SemanticQuery:
        layer = public_semantic_layer()
        prompt = (
            "Translate the question into a SemanticQuery. Use only the attached semantic layer. "
            "Never invent metrics, dimensions, models, or SQL. Always include model, metrics, dimensions, filters, order_by, and limit. "
            "Each filter must include field, operator, and value; each order item must include field and direction.\nSemantic layer:\n"
            + json.dumps(layer) + "\nQuestion:\n" + question
        )
        response = self.model.structured(prompt, SemanticQuery)
        return response if isinstance(response, SemanticQuery) else fallback_semantic_query(question)

    def _analytics(self, state: AgentState) -> dict[str, Any]:
        warnings = list(state["warnings"])
        try:
            deterministic = fallback_semantic_query(state["question"])
        except ValueError as error:
            warnings.append(str(error))
            return {"analytics": None, "warnings": warnings, "tool_trace": state["tool_trace"]}
        try:
            candidate = self._semantic_from_model(state["question"])
            # The deterministic question mapper sets the intended mart. A
            # structured model intent may refine that shape, but it cannot
            # change the business surface selected for the question.
            if candidate.model != deterministic.model:
                raise ValueError("Model selected a different semantic mart")
            evidence = execute_semantic_query(self.settings, candidate)
            if not evidence.rows:
                evidence = execute_semantic_query(self.settings, deterministic)
                warnings.append("The model intent returned no rows; a deterministic governed query was used.")
        except ValueError:
            # A model-generated intent is never allowed to escape the semantic
            # contract. Fall back to the deterministic, question-aware intent
            # instead of sending arbitrary SQL or failing the entire answer.
            evidence = execute_semantic_query(self.settings, deterministic)
            warnings.append("The model intent was outside the semantic contract; a deterministic governed query was used.")
        except Exception as error:
            evidence = None
            warnings.append(f"Analytics request could not be safely completed: {error}")
        return {"analytics": evidence, "warnings": warnings, "tool_trace": [*state["tool_trace"], "query_recall_analytics"]}

    def _status(self, state: AgentState) -> dict[str, Any]:
        return {"warnings": ["Use the data-status panel for source freshness. This assistant does not refresh sources during chat."], "tool_trace": [*state["tool_trace"], "get_data_status"]}

    def _fallback_answer(self, state: AgentState) -> AnswerDraft:
        if state["route"] == "clarify":
            return AnswerDraft(answer_markdown="Recall RAG is read-only. I can show a transfer candidate, but I cannot move inventory or change source systems.", citation_ids=[], warnings=[])
        if state["route"] == "status":
            return AnswerDraft(answer_markdown="The data-status tool reports source freshness and the latest evaluation results. No source refresh is performed from a chat request.", citation_ids=[], warnings=[])
        if state["route"] == "general":
            return AnswerDraft(answer_markdown="I did not find anything relevant in the connected Recall RAG sources. General-answer mode needs a configured model to answer this question.", citation_ids=[], warnings=[])
        pieces: list[str] = []
        citations = state.get("citations") or []
        if citations:
            first = citations[0]
            pieces.append(f"The strongest matching public recall evidence is **{first.title}** [{first.citation_id}].")
        if state.get("analytics"):
            evidence = state["analytics"]
            assert evidence is not None
            pieces.append(f"The governed analytics query returned **{len(evidence.rows)} row(s)** from synthetic distributor data.")
        if not pieces:
            pieces.append("I could not verify a grounded answer from the available sources and governed marts.")
        return AnswerDraft(answer_markdown="\n\n".join(pieces), citation_ids=[citation.citation_id for citation in citations], warnings=[])

    def _compose(self, state: AgentState) -> dict[str, Any]:
        citations = state.get("citations") or []
        evidence = state.get("analytics")
        source_payload = [{"citation_id": item.citation_id, "title": item.title, "excerpt": item.excerpt} for item in citations]
        analytics_payload = evidence.model_dump() if evidence else None
        system_evidence = bool(citations or evidence)
        prompt = (
            "Answer the Recall RAG question using the supplied evidence when it exists. Cite public-source claims only with the supplied citation IDs. "
            "Explicitly call structured operational results synthetic distributor data. "
            + (
                "There is no relevant Recall RAG evidence for this question. You may answer from general knowledge, but begin with: "
                "'I did not find anything relevant in the connected Recall RAG sources, but generally:'. Do not invent citations. Do not provide current, future, price, legal, medical, weather, or other time-sensitive facts without a connected source; instead say that Recall RAG cannot verify them. "
                if not system_evidence else
                "Do not use outside facts for claims that should be supported by the connected Recall RAG system. "
            )
            + "When analytics and public evidence are both supplied, keep them separate: analytics rows are synthetic distributor data, while a public citation only verifies the campaign named in that citation. Do not generalize one cited campaign's consequence to other campaigns. For analytics, report only values and dimensions present in the returned rows; do not invent campaign names, dates, causes, recommendations, or operational facts that are not in the evidence. "
            + "Format answer_markdown as readable Markdown: bold important entities, counts, percentages, dates, urgency labels, and recommendations with **double asterisks**. Do not print citation IDs, chunk IDs, or [source: ...] markers in the answer; return citations only through citation_ids. Always include citation_ids and warnings arrays, even when each is empty.\n"
            f"Question: {state['question']}\nPublic evidence: {json.dumps(source_payload)}\nAnalytics evidence: {json.dumps(analytics_payload, default=str)}"
        )
        # A parser refusal or clarification is already the authoritative
        # answer. Do not let the language model turn it into generic advice
        # or invent an answer without a tool result.
        if not citations and evidence is None and state["route"] in {"analytics", "combined"} and state.get("warnings"):
            draft = AnswerDraft(answer_markdown=state["warnings"][-1], citation_ids=[], warnings=[])
        else:
            draft = self.model.structured(prompt, AnswerDraft) or self._fallback_answer(state)
        allowed = {citation.citation_id for citation in citations}
        if any(citation_id not in allowed for citation_id in draft.citation_ids):
            draft = self._fallback_answer(state)
        response = AssistantResponse(
            answer_markdown=draft.answer_markdown,
            citations=[citation for citation in citations if citation.citation_id in draft.citation_ids],
            analytics_evidence=evidence,
            retrieval_trace=state.get("retrieval_trace"),
            warnings=[*state.get("warnings", []), *draft.warnings],
            tool_trace=state.get("tool_trace", []), request_id=str(uuid.uuid4()),
            mode="model" if self.settings.model_enabled else "development_fallback",
        )
        return {"response": response}

    def run(self, question: str) -> AssistantResponse:
        state = self.graph.invoke({"question": question})
        return state["response"]
