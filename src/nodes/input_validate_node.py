"""AgentCore Platform v1.0"""

# RET-C2-667 - InputValidateNode
# Domain node 1: turn the accepted claim context into the retrieval query and
# the filter set the rest of the pipeline reads.
#
# The caller contract itself is enforced at the entry boundary
# (PreProcessNode), and the accepted result is handed to this graph through the
# caller-context bridge. This node re-runs the same contract when it cannot see
# that result - a node that is only safe when its caller behaved is not safe.
# Both paths call the one implementation in src/services/caller_contract.py, so
# there is no second, weaker copy of the rules to drift.
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json
from src.services.caller_contract import (
    CallerDataError,
    ScreeningRefused,
    build_claim_context,
    compose_query,
)
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress


class InputValidateNode(FunctionNode):
    """Compose the retrieval query and filters from the accepted claim context.

    Input state keys:
        claim_context:  JSON dict of caller fields already accepted upstream
        validated_input | user_input: the request payload (fallback source)
        input_context:  structured caller channel (fallback source)

    Output state keys (partial dict):
        compliance_query: retrieval query composed from the accepted fields
        claim_filters:    JSON dict {"category", "top_k", "discount_pct"}
        intake_notes:     JSON list[str] when the claim carried nothing to match
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        notes: List[str] = []
        claim_context = from_json(state.get("claim_context"), None)

        if not isinstance(claim_context, dict):
            # No accepted context reached this graph - validate from scratch
            # against the same contract rather than trusting the raw payload.
            raw = state.get("validated_input") or state.get("user_input", "")
            try:
                claim_context = build_claim_context(raw, state.get("input_context", {}))
            except ScreeningRefused as exc:
                # Terminal — refused content, not a correctable value. Caught
                # ahead of CallerDataError below, and separated by TYPE so the
                # distinction survives any rewording of either message.
                emit_trace_event(
                    "input_validate_rejected",
                    {"reason": "screening"},
                    state,
                )
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"InputValidateNode: {exc}"],
                }
            except CallerDataError as exc:
                emit_trace_event(
                    "input_validate_rejected",
                    {"reason": "caller_contract"},
                    state,
                )
                emit_progress(INPUT_REJECTED)
                return {
                    "status": AgentStatus.SUCCESS.value,
                    "error_code": "INVALID_REQUEST",
                    "error_log": [f"InputValidateNode: {exc}"],
                }

        query = compose_query(claim_context)
        if not query:
            notes.append("InputValidateNode: empty request - no claim to validate.")

        filters: Dict[str, Any] = {
            "category": claim_context.get("category"),
            "top_k": claim_context.get("top_k"),
            "discount_pct": claim_context.get("discount_pct"),
        }

        # Audit: claim parsed and normalised. Counts and presence flags only.
        emit_trace_event(
            "input_validate_complete",
            {
                "query_chars": len(query),
                "has_category_filter": filters["category"] is not None,
                "has_top_k_override": filters["top_k"] is not None,
                "has_discount_pct": filters["discount_pct"] is not None,
            },
            state,
        )

        out: Dict[str, Any] = {
            "compliance_query": query,
            "claim_filters": to_json(filters),
        }
        if notes:
            out["intake_notes"] = to_json(notes)
        return out
