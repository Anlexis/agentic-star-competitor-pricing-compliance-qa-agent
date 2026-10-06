"""AgentCore Platform v1.0"""

# RET-C2-667 - PostProcessNode: the output boundary.
#
# This node calls the module-level `_security_gate_output()` scan from
# execute() itself. The scan RECURSES into nested dict / list / tuple values
# rather than looking only at a top-level string, so it sees the rendered
# assessment AND the structured citations / corrective_actions payload
# underneath it. It looks for disallowed content - API keys, JWTs, bearer
# tokens, raw credential assignments - using generic, domain-agnostic patterns
# plus the platform's own credential detector, so anything the framework would
# refuse is caught here first, where it can be CONTAINED rather than raised.
#
# The scan is unconditional: every path through execute() reaches it before any
# value can be returned, and no flag, argument or state field can suppress it.
#
# Containment, not just refusal
# -----------------------------
# The framework's envelope builder falls back to `state["result"]` when
# `formatted_output` is absent, and it does that on an ERROR run too. So a gate
# that merely RAISES on a violation still ships the un-gated answer: the node
# call returns an error status, the offending text is still sitting in state,
# and the envelope picks it up. Refusing is not containing. On a violation this
# node therefore returns ERROR *and* clears every state field that can carry
# released text, so the error envelope has nothing to fall back to.
#
# For the same reason execute() never lets an exception escape: an exception
# leaves state untouched, which is exactly the un-contained case. Anything
# unexpected is turned into the same contained ERROR result.
#
# A top-level-string-only scan is the specific defect that lets a
# credential-shaped string buried inside a returned structured payload through
# untouched. This gate closes that, and on the clean path it derives ONLY
# whitelisted vetted SCALAR summary fields (rule_citation_count /
# corrective_action_count / primary_rule_reference) for the agent's
# get_output() to surface - the raw citations / corrective_actions containers
# are never surfaced at that boundary.
#
# No _extra_security_gate_input/_output instance methods are defined here (the
# framework auto-wraps such hooks).
#
# Outer backbone gate slot: the manifest declares
# required_trust_level: "VERIFIED_EXTERNAL".

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials_in_value
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG
from src.schemas.state import from_json

logger = logging.getLogger(__name__)

# Disallowed content patterns. Each tuple: (name, compiled regex) - order
# matters (most specific first).
_DISALLOWED_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    # API key patterns: sk-..., pk-..., ak-...
    ("api_key", re.compile(r"\b(?:sk|pk|ak)-[A-Za-z0-9]{16,}", re.IGNORECASE)),
    # JWT: three base64url segments separated by dots
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # Bearer token in an authorization-like context
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/]{20,}", re.IGNORECASE)),
    # Credential assignment patterns
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|secret|api_key|token|access_key|private_key)\s*[:=]\s*\S{8,}",
            re.IGNORECASE,
        ),
    ),
]

_SANITISED_STUB = (
    "[OUTPUT BLOCKED - disallowed content detected. Review the generated "
    "output and retry without credential-like strings.]"
)

# Every state field that can carry released assessment text. On a violation all
# of them are cleared, so no fallback in the envelope builder can reach one.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "formatted_output",
    "compliance_answer",
    "formatted_answer",
    "grounded_answer",
    "citations",
    "corrective_actions",
    "risk_level",
    "rule_citation_count",
    "corrective_action_count",
    "primary_rule_reference",
)


def _security_gate_output(content: Any) -> Optional[str]:
    """Run the output content gate, RECURSING into nested containers.

    Returns the name of the first matched violation, or None if clean.

    Unlike a top-level-string-only scan, this walks into dict / list / tuple
    values, so a credential-shaped string buried inside a structured payload -
    one entry of `citations` or `corrective_actions`, say - cannot bypass it.

    The platform's own credential detector runs first over the whole structure.
    That matters for containment as much as for detection: the framework applies
    the same detector to whatever this node returns and RAISES on a hit, and a
    raise leaves the un-gated text in state. Catching those hits here means the
    violation is contained instead.
    """
    if content is None:
        return None
    platform_findings = detect_credentials_in_value(content)
    if platform_findings:
        finding_type = platform_findings[0].get("type")
        return str(finding_type) if finding_type else "credential"
    return _scan_patterns(content)


def _scan_patterns(content: Any) -> Optional[str]:
    """Recursive scan of the domain-specific disallowed patterns."""
    if content is None:
        return None
    if isinstance(content, str):
        for name, pattern in _DISALLOWED_PATTERNS:
            if pattern.search(content):
                return name
        return None
    if isinstance(content, dict):
        for value in content.values():
            violation = _scan_patterns(value)
            if violation:
                return violation
        return None
    if isinstance(content, (list, tuple)):
        for item in content:
            violation = _scan_patterns(item)
            if violation:
                return violation
        return None
    # Non-string scalars (int / float / bool / ...) carry no credential risk.
    return None


def _contained_error(violation: str) -> Dict[str, Any]:
    """The blocked-output result: an error AND an emptied output surface."""
    contained: Dict[str, Any] = {field: None for field in _OUTPUT_BEARING_FIELDS}
    contained["formatted_output"] = _SANITISED_STUB
    contained["result"] = _SANITISED_STUB
    contained["status"] = AgentStatus.ERROR.value
    contained["error_log"] = [f"PostProcessNode: output blocked - disallowed content detected ({violation})"]
    return contained


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Finalize the compliance-risk answer, behind the output gate."""

    # Explicit by design, not inherited implicitly. Outer backbone gate slot -
    # matches the required_trust_level declared in the agent manifest.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "formatted_output": message,
                "result": message,
            }
        try:
            return self._gate_and_finalize(state)
        except Exception as exc:  # containment: never leave state un-gated
            logger.error("PostProcessNode: output finalisation failed: %s", exc)
            emit_trace_event(
                "compliance_answer_blocked",
                {"violation": "finalisation_error"},
                state,
            )
            return _contained_error("finalisation_error")

    def _gate_and_finalize(self, state: Dict[str, Any]) -> Dict[str, Any]:
        result = state.get("result", "")
        scanned_text = "" if result is None else str(result)
        citations = from_json(state.get("citations"), []) or []
        corrective_actions = from_json(state.get("corrective_actions"), []) or []

        # The gate - unconditional and RECURSIVE. Every path through execute()
        # reaches this scan before a value can be returned; there is no
        # suppression flag. It covers the rendered text AND the structured
        # citations / corrective_actions payload, plus the fields the outer
        # merge wrote, so no representation of the answer is left unscanned.
        violation = _security_gate_output(
            {
                "text": scanned_text,
                "citations": citations,
                "corrective_actions": corrective_actions,
                "compliance_answer": state.get("compliance_answer"),
                "grounded_answer": state.get("grounded_answer"),
            }
        )
        if violation:
            logger.error("PostProcessNode: OUTPUT BLOCKED - violation type: %s", violation)
            # Audit: an output was blocked. Payload carries the violation type
            # only - never the offending text.
            emit_trace_event(
                "compliance_answer_blocked",
                {"violation": violation},
                state,
            )
            return _contained_error(violation)

        # Clean - derive the whitelisted SCALAR summary. Only these vetted
        # scalars reach the agent's get_output(); the raw citations /
        # corrective_actions containers are never surfaced at this boundary.
        # The full text is already in formatted_output, itself part of the scan.
        primary_ref: Optional[str] = None
        if citations and isinstance(citations[0], dict):
            top_id = citations[0].get("id")
            primary_ref = top_id if isinstance(top_id, str) and top_id else None

        # Audit: a finalized compliance-risk answer was emitted. Payload carries
        # counts and lengths only - never the answer text.
        emit_trace_event(
            "compliance_answer_emitted",
            {
                "output_chars": len(scanned_text),
                "citation_count": len(citations),
                "corrective_action_count": len(corrective_actions),
            },
            state,
        )

        return {
            "formatted_output": result,
            "status": AgentStatus.SUCCESS.value,
            "rule_citation_count": len(citations),
            "corrective_action_count": len(corrective_actions),
            "primary_rule_reference": primary_ref,
        }
