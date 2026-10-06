"""AgentCore Platform v1.0"""

# RET-C2-667 - OutputFormatNode
# Domain node 5 (terminal): compose the final formatted answer - the risk
# level header, the grounded rationale, the Cited Provisions list, the
# Corrective Actions list, and the standing compliance advisory disclaimer.
# The disclaimer is part of THIS node's domain output contract, not of the
# outer post_process slot (post_process only gates, it does not compose).
#
# Wired by the inner graph (DomainWorkflowGraph). get_output() of the inner
# graph surfaces formatted_answer + citations + risk_level +
# corrective_actions + status to the outer merge_output().
# Returns only changed state keys (partial dict).

from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json

# Standing compliance advisory line - appended to EVERY answer this template
# emits. Deliberately hedged: this is a risk ASSESSMENT, not a legal
# determination of compliance or non-compliance.
_ADVISORY_DISCLAIMER = (
    "This assessment is generated from the seeded 景品表示法 (Act against "
    "Unjustifiable Premiums and Misleading Representations) knowledge base "
    "for informational purposes only and does not constitute legal advice. "
    "Confirm with your legal or compliance counsel before publishing or "
    "amending any price-comparison claim."
)

_RISK_LEVEL_LABELS: Dict[str, str] = {
    "insufficient_data": "INSUFFICIENT DATA",
    "low_risk": "LOW RISK",
    "medium_risk": "MEDIUM RISK",
    "high_risk": "HIGH RISK",
}


class OutputFormatNode(FunctionNode):
    """Compose the final answer: risk header + rationale + citations + actions + disclaimer.

    Input state keys:
        grounded_answer:    rationale body with [n] citation markers
        citations:          JSON list [{ref, id, title, source}]
        risk_level:          compliance-risk classification
        corrective_actions: JSON list[str]

    Output state keys (partial dict):
        formatted_answer: final rendered answer string
        status:           AgentStatus.SUCCESS.value (a plain string - never
                          write the bare enum into State)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        grounded_answer = state.get("grounded_answer") or ("No compliance rationale is available for this request.")
        citations: List[Dict[str, Any]] = from_json(state.get("citations"), []) or []
        corrective_actions: List[str] = from_json(state.get("corrective_actions"), []) or []
        risk_level = state.get("risk_level") or "insufficient_data"
        risk_label = _RISK_LEVEL_LABELS.get(risk_level, risk_level.upper())

        lines: List[str] = []
        lines.append("# EC Pricing Compliance Risk Assessment")
        lines.append("")
        lines.append(f"**Risk Level:** {risk_label}")
        lines.append("")
        lines.append(grounded_answer)
        lines.append("")
        lines.append("## Cited Provisions")
        if citations:
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                ref = citation.get("ref", "?")
                title = str(citation.get("title", "")).strip()
                source = str(citation.get("source", "")).strip()
                suffix = f" ({source})" if source else ""
                lines.append(f"- [{ref}] {title}{suffix}")
        else:
            lines.append("- none (no knowledge-base provision cleared the relevance threshold)")
        lines.append("")
        lines.append("## Corrective Actions")
        if corrective_actions:
            for action in corrective_actions:
                lines.append(f"- {action}")
        else:
            lines.append("- none")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"*{_ADVISORY_DISCLAIMER}*")

        formatted_answer = "\n".join(lines)

        # Audit: the final compliance-risk answer was composed with the
        # advisory line attached. Counts and labels only, never the answer text.
        emit_trace_event(
            "output_format_complete",
            {
                "answer_chars": len(formatted_answer),
                "citation_count": len(citations),
                "corrective_action_count": len(corrective_actions),
                "risk_level": risk_level,
            },
            state,
        )

        return {
            "formatted_answer": formatted_answer,
            "status": AgentStatus.SUCCESS.value,
        }
