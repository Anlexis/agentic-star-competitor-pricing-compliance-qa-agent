"""AgentCore Platform v1.0"""

# RET-C2-667 - PreProcessNode
# Outer backbone gate slot: this is the node that owns the CALLER CONTRACT.
# Every field the caller can set - through the request string or through the
# structured context channel - is validated here, before any of it reaches
# retrieval or the assessment.
#
# The template owns this guarantee rather than delegating it to the framework's
# input gate. That gate screens the request string where it is active; it does
# not see the context channel, and a deployment can run without it. A refusal
# that only happens when something upstream is configured a particular way is a
# fail-OPEN by another name, so the check lives in the node that reads the data.
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus.<X>.value strings for status assignments
#  - Read input_context via state.get("input_context", {}) - read-only
#  - Never import from mediator/, api/, or other agents

from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.pii_detector import detect_pii
from framework.security.pii_masking import mask_pii
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import to_json
from src.services.caller_contract import CallerDataError, ScreeningRefused, build_claim_context
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED
from src.services.progress import emit_progress

# Free-text caller fields that must be PII-masked. The framework masks the
# request string; nothing masks the context channel, so any free text arriving
# there is masked here with the same detector before it is stored or rendered.
_PII_MASKED_FIELDS = ("claim",)


def _mask_free_text(claim_context: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the platform PII mask to the free-text fields of the claim context."""
    masked = dict(claim_context)
    for field in _PII_MASKED_FIELDS:
        value = masked.get(field)
        if not isinstance(value, str) or not value:
            continue
        findings = detect_pii(value)
        if findings:
            masked[field] = mask_pii(value, findings)
    return masked


class PreProcessNode(FunctionNode):
    """Validate the caller's price-comparison claim before the pipeline runs."""

    # Explicit by design, not inherited implicitly. Outer backbone gate slot -
    # matches the required_trust_level declared in the agent manifest.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not isinstance(user_input, str) or not user_input.strip():
            # Audit: the request was rejected before any rule retrieval ran.
            # Payload carries a reason code and no caller content.
            emit_trace_event(
                "pricing_query_rejected",
                {"reason": "empty_input"},
                state,
            )
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        try:
            claim_context = build_claim_context(user_input, input_context)
        except ScreeningRefused as exc:
            # Terminal. Screening raises a distinct TYPE rather than a distinct
            # message, so this stays a refusal however the wording is later
            # edited. Refused content is not a value to correct, and reporting
            # it like one would read as an invitation to reword the claim until
            # it gets through.
            emit_trace_event(
                "pricing_query_rejected",
                {"reason": "screening"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: {exc}"],
            }
        except CallerDataError as exc:
            # Fail CLOSED. The message names the field that failed; the value
            # that failed is never written to the log or handed back. The run
            # COMPLETES carrying the reason: the caller can correct the field
            # and send the request again on the same conversation.
            emit_trace_event(
                "pricing_query_rejected",
                {"reason": "caller_contract"},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"PreProcessNode: {exc}"],
            }

        claim_context = _mask_free_text(claim_context)
        validated_input = user_input.strip()

        # Audit: a price-comparison claim was accepted for validation. Payload
        # carries lengths and presence flags only - never the claim text.
        emit_trace_event(
            "pricing_query_accepted",
            {
                "input_chars": len(validated_input),
                "claim_chars": len(claim_context.get("claim", "")),
                "has_category_filter": "category" in claim_context,
                "has_top_k_override": "top_k" in claim_context,
                "has_discount_pct": "discount_pct" in claim_context,
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "claim_context": to_json(claim_context),
            "enriched_context": {
                "source": "ECPricingComplianceValidationAgent",
                "channel": str(input_context.get("channel", "unknown"))[:32]
                if isinstance(input_context, dict)
                else "unknown",
            },
            "status": AgentStatus.SUCCESS.value,
        }
