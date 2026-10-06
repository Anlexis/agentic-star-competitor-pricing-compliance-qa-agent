# RET-C2-667 — Unit Tests: OutputFormatNode (inner domain node 5, terminal)
#
# Invocation canon: node(state) via BaseNode.__call__ with an ANONYMOUS caller.
# formatted_answer is a DOMAIN field (not an input-gate scan target); the standing
# compliance advisory disclaimer is part of THIS node's output contract (not
# post_process — post_process only gates, per docs/02_design.md).
#
# Mirrors docs/03_test_spec.md Sec 2.6 (FMT-01..FMT-06).
# Deterministic — no LLM, no network. framework.* / src.* imports only.

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.output_format_node import OutputFormatNode
from src.schemas.state import to_json

_DISCLAIMER_FRAGMENT = "does not constitute legal advice"


def _make_state(grounded_answer, citations, risk_level, corrective_actions, **extra) -> dict:
    state = {
        "grounded_answer": grounded_answer,
        "citations": citations,
        "risk_level": risk_level,
        "corrective_actions": corrective_actions,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestFormattedAnswer:
    def test_fmt_01_composes_header_body_citations_actions_disclaimer(self):
        citations = to_json([{"ref": 1, "id": "kb-a", "title": "Title A", "source": "Source A"}])
        actions = to_json(["do X", "do Y"])
        result = OutputFormatNode()(_make_state("[1] body text.", citations, "high_risk", actions))
        answer = result["formatted_answer"]
        assert answer.startswith("# EC Pricing Compliance Risk Assessment")
        assert "**Risk Level:** HIGH RISK" in answer
        assert "[1] body text." in answer
        assert "## Cited Provisions" in answer
        assert "- [1] Title A (Source A)" in answer
        assert "## Corrective Actions" in answer
        assert "- do X" in answer
        assert "- do Y" in answer
        assert _DISCLAIMER_FRAGMENT in answer
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the plain status string, never the enum.
        assert type(result["status"]) is str  # noqa: E721 - AgentStatus subclasses str, so isinstance() would pass for
        # the enum too and this guard exists precisely to catch the enum.

    def test_fmt_02_source_suffix_omitted_when_blank(self):
        citations = to_json([{"ref": 1, "id": "kb-a", "title": "Title A", "source": ""}])
        answer = OutputFormatNode()(_make_state("body.", citations, "low_risk", to_json([])))["formatted_answer"]
        assert "- [1] Title A\n" in answer + "\n"
        assert "()" not in answer

    def test_fmt_03_disclaimer_present_on_every_answer(self):
        for grounded in ("a body.", ""):
            answer = OutputFormatNode()(_make_state(grounded, to_json([]), "insufficient_data", to_json([])))[
                "formatted_answer"
            ]
            assert _DISCLAIMER_FRAGMENT in answer

    def test_risk_level_label_mapping(self):
        for level, label in (
            ("insufficient_data", "INSUFFICIENT DATA"),
            ("low_risk", "LOW RISK"),
            ("medium_risk", "MEDIUM RISK"),
            ("high_risk", "HIGH RISK"),
        ):
            answer = OutputFormatNode()(_make_state("body.", to_json([]), level, to_json([])))["formatted_answer"]
            assert f"**Risk Level:** {label}" in answer


class TestDegradedInputs:
    def test_fmt_04_no_citations_renders_explicit_none_line(self):
        answer = OutputFormatNode()(_make_state("no coverage body.", to_json([]), "insufficient_data", to_json([])))[
            "formatted_answer"
        ]
        assert "- none (no knowledge-base provision cleared the relevance threshold)" in answer

    def test_fmt_05_no_corrective_actions_renders_explicit_none_line(self):
        answer = OutputFormatNode()(_make_state("body.", to_json([]), "low_risk", to_json([])))["formatted_answer"]
        assert "## Corrective Actions\n- none" in answer

    def test_fmt_06_missing_grounded_answer_uses_fallback_text(self):
        state = _make_state("", to_json([]), "insufficient_data", to_json([]))
        del state["grounded_answer"]
        result = OutputFormatNode()(state)
        assert "No compliance rationale is available for this request." in result["formatted_answer"]
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_missing_risk_level_defaults_to_insufficient_data(self):
        state = _make_state("body.", to_json([]), "insufficient_data", to_json([]))
        del state["risk_level"]
        answer = OutputFormatNode()(state)["formatted_answer"]
        assert "**Risk Level:** INSUFFICIENT DATA" in answer
