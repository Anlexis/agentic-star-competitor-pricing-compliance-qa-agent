"""AgentCore Platform v1.0"""

# RET-C2-667 - GenerateAnswerNode
# Domain node 4: assemble the grounded compliance rationale, the risk
# classification, and the corrective-action guidance from the ranked
# provisions.
#
# The pipeline is DETERMINISTIC (no model call): the rationale is assembled
# from the ranked provisions only - a lead sentence plus one cited point per
# provision, each carrying a numbered citation marker [n].
#
# Grounding invariant: the rationale BODY is built exclusively from the ranked
# provisions. The caller's own claim appears in the lead sentence only, and
# only after the caller contract has rendered it inert - whitespace collapsed
# and the characters that carry structure in the report removed - so a claim
# cannot open a heading, forge a "[1]" citation marker, or introduce a second
# risk verdict into the assessment.
#
# risk_level and corrective_actions are a small deterministic ladder over
# (top match strength, caller discount_pct); see _assess_risk_level(). The
# synthesis upgrade seam is documented in docs/02_design.md and
# config/prompts/answer_synthesis_prompt.md: a later node swaps the assembly
# for a model call over the same input and emits the same state contract.
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

import math
from typing import Any, ClassVar, Dict, List, Optional

from framework.schemas.agent_status import AgentStatus
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json

# Rationale used when no KB provision cleared the relevance threshold.
_NO_COVERAGE_ANSWER = (
    "The compliance knowledge base does not contain sufficient coverage to "
    "assess this claim. Rephrase the claim with more specific pricing or "
    "comparison terms, or escalate to the compliance team for a manual "
    "review."
)

# Cited excerpt length per provision inside the rationale body.
_POINT_EXCERPT_CHARS = 240

# Risk ladder thresholds (deterministic - see docs/02_design.md).
# A top-ranked provision at or above this score is treated as a strong,
# on-point match. Set just above RerankFilterNode's own score_threshold
# default (0.25) rather than at it, deliberately, so risk escalation
# requires a meaningfully closer match than the bare RAG inclusion floor -
# kept as an independent constant so the two nodes' tuning can diverge
# without coupling.
_STRONG_MATCH_SCORE = 0.3
# A discount claim at or above this percentage, combined with a strong KB
# match, is escalated to high_risk (the two known highest-volume 有利誤認
# scenarios in the seeded KB: dual/reference pricing and comparative
# advertising both turn on double-digit discount claims).
_HIGH_RISK_DISCOUNT_PCT = 20.0

# Deterministic corrective-action guidance, keyed by risk_level. Returned as
# a NEW list on every call - never a reference into this module-level dict's
# own list objects, which a caller could then mutate for every later run.
_CORRECTIVE_ACTIONS: Dict[str, List[str]] = {
    "insufficient_data": [
        "Escalate to the compliance team for manual review - no matching "
        "景品表示法 provision was found in the seeded knowledge base for "
        "this claim.",
    ],
    "high_risk": [
        "Do not publish the comparison claim until the compared price is "
        "substantiated with dated, verifiable selling-price records.",
        "Add an explicit comparison-basis disclosure (comparison date and "
        "source) directly next to the discount claim.",
        "Route the claim to the compliance/legal team for pre-publication " "sign-off before it goes live.",
    ],
    "medium_risk": [
        "Retain the underlying price and comparison evidence on file before " "publishing the claim.",
        "Add a comparison-basis disclosure near the discount claim so the " "reference point is clear to the consumer.",
    ],
    "low_risk": [
        "No immediate corrective action required; keep standard "
        "substantiation records on file in case of a future request.",
    ],
}


def _first_sentences(text: str, limit: int) -> str:
    """Trim an excerpt at a sentence boundary where possible, else hard-cap."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    period = cut.rfind(". ")
    if period > limit // 2:
        return cut[: period + 1]
    return cut.rstrip() + "..."


def _assess_risk_level(ranked: List[Dict[str, Any]], discount_pct: Optional[float]) -> str:
    """Deterministic risk ladder - see the module docstring for the rationale.

    insufficient_data: no KB provision cleared the relevance threshold.
    high_risk:          a strong on-point match AND a double-digit-or-more
                         discount claim.
    medium_risk:        a strong on-point match, discount magnitude unknown
                         or below the high-risk threshold.
    low_risk:            only weak/tangential matches.
    """
    if not ranked:
        return "insufficient_data"
    top = ranked[0]
    try:
        top_score = float(top.get("score", 0.0)) if isinstance(top, dict) else 0.0
    except (TypeError, ValueError):
        top_score = 0.0
    strong_match = top_score >= _STRONG_MATCH_SCORE
    if strong_match and discount_pct is not None and discount_pct >= _HIGH_RISK_DISCOUNT_PCT:
        return "high_risk"
    if strong_match:
        return "medium_risk"
    return "low_risk"


class GenerateAnswerNode(FunctionNode):
    """Rule-based grounded rationale + risk classification + corrective actions.

    Input state keys:
        ranked_rules:     JSON list of surviving provisions (from RerankFilterNode)
        compliance_query: normalised query (for the lead sentence)
        claim_filters:    JSON dict - reads discount_pct for the risk ladder

    Output state keys (partial dict):
        grounded_answer:    rationale body with [n] citation markers
        citations:          JSON list [{ref, id, title, source}]
        risk_level:          "insufficient_data" | "low_risk" | "medium_risk" | "high_risk"
        corrective_actions: JSON list[str]
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        ranked: List[Dict[str, Any]] = from_json(state.get("ranked_rules"), []) or []
        query = state.get("compliance_query") or ""
        filters = from_json(state.get("claim_filters"), {}) or {}
        # The caller contract already rejected non-finite and out-of-range
        # discounts, so anything that reaches here is a real number. Re-check
        # rather than assume: NaN compares False against every threshold, so an
        # unchecked one would silently DOWNGRADE the risk verdict, and this node
        # is also reachable directly with a hand-built state.
        discount_pct = filters.get("discount_pct")
        if (
            isinstance(discount_pct, bool)
            or not isinstance(discount_pct, (int, float))
            or not math.isfinite(float(discount_pct))
        ):
            discount_pct = None

        citations: List[Dict[str, Any]] = []

        if not ranked:
            grounded_answer = _NO_COVERAGE_ANSWER
        else:
            lines: List[str] = []
            if query:
                # `query` was rendered inert by the caller contract before it
                # reached state, so echoing it here cannot inject structure.
                lines.append(
                    f"Based on the seeded pricing-compliance knowledge base, the "
                    f'following provisions bear on the claim: "{query}"'
                )
            else:
                lines.append(
                    "Based on the seeded pricing-compliance knowledge base, the " "most relevant provisions are:"
                )
            lines.append("")
            for ref, doc in enumerate(ranked, start=1):
                if not isinstance(doc, dict):
                    continue
                title = str(doc.get("title", "")).strip()
                excerpt = _first_sentences(str(doc.get("excerpt", "")), _POINT_EXCERPT_CHARS)
                lines.append(f"[{ref}] {title}: {excerpt}")
                citations.append(
                    {
                        "ref": ref,
                        "id": str(doc.get("id", "")),
                        "title": title,
                        "source": str(doc.get("source", "")),
                    }
                )
            grounded_answer = "\n".join(lines)

        risk_level = _assess_risk_level(ranked, discount_pct)
        # A local copy of the module-level guidance list - never hand back
        # (or let a caller mutate) the module default itself.
        corrective_actions = list(_CORRECTIVE_ACTIONS.get(risk_level, []))

        # Audit: compliance rationale + risk assessment assembled. Payload
        # carries counts and labels only - never the claim text or the
        # rationale body itself, which also keeps the audit line small.
        emit_trace_event(
            "generate_answer_complete",
            {
                "citation_count": len(citations),
                "answer_chars": len(grounded_answer),
                "no_coverage": not ranked,
                "risk_level": risk_level,
                "corrective_action_count": len(corrective_actions),
            },
            state,
        )

        return {
            "grounded_answer": grounded_answer,
            "citations": to_json(citations),
            "risk_level": risk_level,
            "corrective_actions": to_json(corrective_actions),
        }
