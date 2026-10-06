"""AgentCore Platform v1.0"""

# State must be a flat TypedDict - never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization; Pydantic objects cause silent
# corruption. Extend AgentState with agent-specific fields only. Do NOT add
# credentials, secrets, or Pydantic models.
#
# Msgpack safety: structured fields (dict / list[dict]) are stored as JSON
# STRINGS, not bare Python containers. Producers serialize with to_json() on
# write; consumers deserialize with from_json() on read.
#
# RET-C2-667 - EC Pricing Compliance Validation Agent.
# Two-layer nested graph: outer backbone (AgentBaseGraph) + inner domain
# workflow (BaseGraph). The fields below cover both layers.
#
# Structured-output note: rule_citation_count / corrective_action_count /
# primary_rule_reference are the ONLY structured fields
# ECPricingComplianceValidationAgent.get_output() surfaces beyond the base
# envelope - all three are whitelisted SCALARS derived by PostProcessNode AFTER
# its recursive output scan passes clean (see src/nodes/post_process_node.py and
# docs/02_design.md "Structured Output"). The raw citations /
# corrective_actions containers are never surfaced at that boundary.
#
# PII note: this template validates the caller's OWN published pricing claim
# (business data, not a customer identifier) against a seeded compliance
# knowledge base. No customer PII is ever read or persisted; the framework's
# default PII scan runs on user_input / validated_input, and PreProcessNode
# applies the same mask to the free text arriving on the context channel, which
# that scan does not cover.

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a dict/list State field to a JSON string (msgpack safety).

    None passes through unchanged so an 'unset' field stays distinguishable
    from an empty container.
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON-string State field back to its dict/list.

    None / empty / malformed input -> the supplied ``default`` so a missing or
    corrupt field is non-fatal for the consuming node.
    """
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for RET-C2-667.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    Domain fields are NotRequired so the TypedDict is valid at graph
    initialisation, before any node has written a value.
    """

    # ------------------------------------------------------------------
    # Outer layer - set by PreProcessNode / PricingComplianceGraphNode.merge_output
    # / PostProcessNode
    # ------------------------------------------------------------------

    # Non-empty-checked request payload produced by PreProcessNode.
    validated_input: NotRequired[str]

    # JSON STRING (to_json) of the caller fields PreProcessNode ACCEPTED after
    # the caller contract ran. Deserialised shape:
    # {"claim": str, "product": str | None, "competitor": str | None,
    #  "category": str | None, "top_k": int | None, "discount_pct": float | None}.
    # This is the only caller data the pipeline reads; it crosses into the inner
    # graph through src/graph/context_bridge.py, because the framework's
    # GraphNode passes only a string into a subgraph.
    claim_context: NotRequired[Optional[str]]

    # Final compliance-risk answer, mapped from the inner graph's
    # formatted_answer output via merge_output.
    compliance_answer: NotRequired[str]

    # Compliance-risk classification: "insufficient_data" | "low_risk" |
    # "medium_risk" | "high_risk". Written by the inner GenerateAnswerNode,
    # mapped to the outer layer by merge_output(); read directly by
    # ECPricingComplianceValidationAgent.get_output() (SUCCESS path only).
    risk_level: NotRequired[str]

    # Whitelisted SCALAR summary fields - written by PostProcessNode ONLY on
    # the clean path (after its output scan passed). On a blocked run they are
    # explicitly CLEARED rather than left stale, which is what makes
    # get_output()'s SUCCESS-only check fail-closed in practice.
    rule_citation_count: NotRequired[int]
    corrective_action_count: NotRequired[int]
    primary_rule_reference: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Inner layer - domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # InputValidateNode outputs
    # Normalised free-text price-comparison query (whitespace-collapsed,
    # length-capped).
    compliance_query: NotRequired[str]

    # JSON STRING (to_json) of the filter set derived from claim_context.
    # Deserialised dict shape: {"category": str | None, "top_k": int | None,
    # "discount_pct": float | None}.
    # Consumers (RetrieveNode, RerankFilterNode, GenerateAnswerNode) read it
    # back via from_json().
    claim_filters: NotRequired[Optional[str]]

    # Runtime `retrieval` block forwarded by PricingComplianceGraphNode.
    # _parent_config() -> DomainWorkflowGraph._extra_initial_state().
    # JSON STRING (to_json) of {"top_k": int, "score_threshold": float,
    # "kb_path": str}. Consumers (RetrieveNode, RerankFilterNode) read it
    # back via from_json().
    retrieval_config: NotRequired[Optional[str]]

    # RetrieveNode output
    # JSON STRING (to_json) of scored KB provision candidates. Deserialised
    # shape: list[dict], each entry {"id": str, "title": str, "category": str,
    # "source": str, "score": float, "excerpt": str}.
    # Consumers (RerankFilterNode) read it back via from_json().
    retrieved_rules: NotRequired[Optional[str]]

    # RerankFilterNode output
    # JSON STRING (to_json) of reranked + threshold-filtered provisions,
    # capped at top_k. Same entry shape as retrieved_rules.
    # Consumers (GenerateAnswerNode) read it back via from_json().
    ranked_rules: NotRequired[Optional[str]]

    # GenerateAnswerNode outputs
    # Rule-assembled compliance rationale body with numbered citation markers.
    grounded_answer: NotRequired[str]

    # JSON STRING (to_json) of citations. Deserialised shape: list[dict],
    # each entry {"ref": int, "id": str, "title": str, "source": str}.
    # Consumers (OutputFormatNode, PostProcessNode) read it back via
    # from_json().
    citations: NotRequired[Optional[str]]

    # JSON STRING (to_json) of corrective-action guidance. Deserialised
    # shape: list[str]. Consumers (OutputFormatNode, PostProcessNode) read it
    # back via from_json().
    corrective_actions: NotRequired[Optional[str]]

    # OutputFormatNode output
    # Final formatted answer (rationale + citations + corrective actions +
    # advisory disclaimer). Written by OutputFormatNode; surfaced to the
    # outer graph via get_output() -> merge_output().
    formatted_answer: NotRequired[str]

    # Validation / parse notes accumulated during intake (no caller values).
    # JSON STRING (to_json) of list[str].
    intake_notes: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Tracing / audit - framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
    error_code: Optional[str]
    # node_history inherited from AgentState; listed here for clarity
    # node_history: Optional[List[str]]
