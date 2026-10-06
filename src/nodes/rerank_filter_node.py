"""AgentCore Platform v1.0"""

# RET-C2-667 - RerankFilterNode
# Domain node 3: rerank the retrieval candidates and enforce the relevance
# floor. Deterministic: a small category-match boost on top of the retrieval
# score, drop everything below `score_threshold`, cap the survivors at
# `top_k`.
#
# Config: domain nodes take no execute() config parameter - this node reads the
# forwarded tuning from State (retrieval_config, republished by
# DomainWorkflowGraph._extra_initial_state()), falling back to module defaults
# that mirror config/config.yaml. A caller-supplied top_k override
# (claim_filters) wins when it is stricter.
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

from typing import Any, ClassVar, Dict, List

from framework.schemas.agent_status import AgentStatus
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json
from src.services.caller_contract import (
    TOP_K_MAX,
    TOP_K_MIN,
    CallerDataError,
    finite_in_range,
    finite_int_in_range,
)

# Defaults mirror the `retrieval` block in config/config.yaml.
_DEFAULT_RETRIEVAL: Dict[str, Any] = {
    "top_k": 4,
    "score_threshold": 0.25,
}

# Boost applied when a candidate's category matches the caller's filter.
_CATEGORY_BOOST = 0.1


def _resolve_retrieval_config(state: AgentState) -> Dict[str, Any]:
    """Effective retrieval config: state-seeded retrieval_config > module defaults."""
    effective = dict(_DEFAULT_RETRIEVAL)  # local copy - never mutate the module default
    from_state = from_json(state.get("retrieval_config"), None)
    if isinstance(from_state, dict):
        effective.update(from_state)
    return effective


class RerankFilterNode(FunctionNode):
    """Rerank candidates, apply the score threshold, cap at top_k.

    Input state keys:
        retrieved_rules:  JSON list of scored candidates (from RetrieveNode)
        claim_filters:    JSON dict with optional category / top_k override
        retrieval_config: forwarded manifest retrieval block (JSON)

    Output state keys (partial dict):
        ranked_rules: JSON list of surviving provisions (score desc, <= top_k)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        candidates: List[Dict[str, Any]] = from_json(state.get("retrieved_rules"), []) or []
        filters = from_json(state.get("claim_filters"), {}) or {}
        retrieval_cfg = _resolve_retrieval_config(state)

        # Both tuning values go through the finite+bounded parser. A non-finite
        # score_threshold is the dangerous one: every comparison against NaN is
        # False, so `score >= threshold` would drop EVERY provision and the
        # assessment would report insufficient coverage for a claim the corpus
        # actually covers. Falling back to the declared default is the only safe
        # reading of a nonsense value.
        try:
            top_k = finite_int_in_range(
                retrieval_cfg.get("top_k", _DEFAULT_RETRIEVAL["top_k"]),
                field="retrieval.top_k",
                minimum=TOP_K_MIN,
                maximum=TOP_K_MAX,
            )
        except CallerDataError:
            top_k = int(_DEFAULT_RETRIEVAL["top_k"])
        # A stricter caller override (already validated by the caller contract)
        # wins.
        caller_top_k = filters.get("top_k")
        if isinstance(caller_top_k, int) and not isinstance(caller_top_k, bool) and 1 <= caller_top_k < top_k:
            top_k = caller_top_k

        try:
            score_threshold = finite_in_range(
                retrieval_cfg.get("score_threshold", _DEFAULT_RETRIEVAL["score_threshold"]),
                field="retrieval.score_threshold",
                minimum=0.0,
                maximum=1.0,
            )
        except CallerDataError:
            score_threshold = float(_DEFAULT_RETRIEVAL["score_threshold"])

        category = filters.get("category")

        reranked: List[Dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            entry = dict(candidate)  # local copy - inputs stay immutable
            try:
                score = float(entry.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            if category and str(entry.get("category", "")).lower() == str(category).lower():
                score = min(1.0, score + _CATEGORY_BOOST)
            entry["score"] = round(score, 4)
            reranked.append(entry)

        # Deterministic ordering: score desc, then id asc for stable ties.
        reranked.sort(key=lambda c: (-c.get("score", 0.0), str(c.get("id", ""))))

        kept = [c for c in reranked if c.get("score", 0.0) >= score_threshold][:top_k]
        dropped = len(reranked) - len(kept)

        # Audit: rerank + relevance floor applied.
        emit_trace_event(
            "rerank_filter_complete",
            {
                "kept": len(kept),
                "dropped": dropped,
                "score_threshold": score_threshold,
                "top_k": top_k,
            },
            state,
        )

        return {"ranked_rules": to_json(kept)}
