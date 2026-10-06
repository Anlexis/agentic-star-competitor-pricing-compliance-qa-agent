# RET-C2-667 - Unit Tests: GenerateAnswerNode (inner domain node 4)
#
# Invocation contract: node(state) via BaseNode.__call__ with an ANONYMOUS
# caller. The grounded answer and citations are DOMAIN fields, not input-gate
# scan targets, so Title-Case corpus titles inside them are safe to assert on.
#
# Mirrors docs/03_test_spec.md Sec 2.5 (GEN-01..GEN-06). Beyond the generic
# retrieval pipeline, this node ALSO derives a deterministic risk_level +
# corrective_actions ladder (docs/02_design.md); those are first-class
# assertions here, not carried elsewhere.
#
# Deterministic — rule-assembled from ranked_rules + claim_filters.discount_pct
# only (grounded by construction; no LLM, no network).
# framework.* / src.* imports only.

from framework.schemas.trust_level import TrustLevel

from src.nodes.generate_answer_node import GenerateAnswerNode
from src.schemas.state import from_json, to_json


def _ranked(*entries):
    return to_json(list(entries))


def _doc(doc_id, title, excerpt, score=0.9, source="seeded kb"):
    return {
        "id": doc_id,
        "title": title,
        "category": "discount_pricing",
        "source": source,
        "score": score,
        "excerpt": excerpt,
    }


def _make_state(ranked_rules, query="is this claim compliant", discount_pct=None, **extra) -> dict:
    state = {
        "ranked_rules": ranked_rules,
        "compliance_query": query,
        "claim_filters": to_json({"category": None, "top_k": None, "discount_pct": discount_pct}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestGroundedAnswer:
    def test_gen_01_answer_carries_numbered_citation_markers(self):
        ranked = _ranked(
            _doc("kb-a", "Title A", "excerpt a."),
            _doc("kb-b", "Title B", "excerpt b."),
        )
        result = GenerateAnswerNode()(_make_state(ranked))
        answer = result["grounded_answer"]
        assert "[1] Title A: excerpt a." in answer
        assert "[2] Title B: excerpt b." in answer

    def test_gen_02_lead_sentence_quotes_the_claim(self):
        ranked = _ranked(_doc("kb-a", "Title A", "excerpt."))
        result = GenerateAnswerNode()(_make_state(ranked, query="is this claim compliant"))
        assert 'the claim: "is this claim compliant"' in result["grounded_answer"]

    def test_gen_03_citations_mirror_ranked_order(self):
        ranked = _ranked(
            _doc("kb-a", "Title A", "a.", source="Source A"),
            _doc("kb-b", "Title B", "b."),
        )
        citations = from_json(GenerateAnswerNode()(_make_state(ranked))["citations"])
        assert [c["ref"] for c in citations] == [1, 2]
        assert [c["id"] for c in citations] == ["kb-a", "kb-b"]
        assert citations[0]["source"] == "Source A"

    def test_citations_is_json_string(self):
        # the JSON-string state contract: list-shaped State fields travel as JSON strings.
        ranked = _ranked(_doc("kb-a", "Title A", "a."))
        result = GenerateAnswerNode()(_make_state(ranked))
        assert isinstance(result["citations"], str)

    def test_gen_04_answer_is_grounded_in_ranked_passages_only(self):
        ranked = _ranked(_doc("kb-a", "Title A", "excerpt only from this passage."))
        answer = GenerateAnswerNode()(_make_state(ranked))["grounded_answer"]
        assert "excerpt only from this passage." in answer
        assert "[2]" not in answer


class TestNoCoverage:
    def test_gen_05_empty_ranked_set_yields_no_coverage_answer(self):
        result = GenerateAnswerNode()(_make_state(_ranked()))
        assert "does not contain sufficient coverage" in result["grounded_answer"]
        assert from_json(result["citations"]) == []
        assert result["risk_level"] == "insufficient_data"

    def test_missing_ranked_field_is_treated_as_no_coverage(self):
        state = _make_state(None)
        del state["ranked_rules"]
        result = GenerateAnswerNode()(state)
        assert "does not contain sufficient coverage" in result["grounded_answer"]
        assert result["risk_level"] == "insufficient_data"


class TestRiskLadder:
    """docs/02_design.md risk ladder: top-match strength x caller discount_pct."""

    _STRONG = _ranked(_doc("kb-a", "Title A", "a.", score=0.9), _doc("kb-b", "Title B", "b.", score=0.35))
    _WEAK = _ranked(_doc("kb-c", "Title C", "c.", score=0.1))

    def test_gen_06_strong_match_plus_high_discount_is_high_risk(self):
        result = GenerateAnswerNode()(_make_state(self._STRONG, discount_pct=30))
        assert result["risk_level"] == "high_risk"

    def test_strong_match_below_discount_threshold_is_medium_risk(self):
        result = GenerateAnswerNode()(_make_state(self._STRONG, discount_pct=5))
        assert result["risk_level"] == "medium_risk"

    def test_strong_match_no_discount_info_is_medium_risk(self):
        result = GenerateAnswerNode()(_make_state(self._STRONG, discount_pct=None))
        assert result["risk_level"] == "medium_risk"

    def test_weak_match_is_low_risk_even_with_high_discount(self):
        result = GenerateAnswerNode()(_make_state(self._WEAK, discount_pct=90))
        assert result["risk_level"] == "low_risk"


class TestCorrectiveActions:
    def test_corrective_actions_keyed_by_risk_level(self):
        ranked = _ranked(_doc("kb-a", "Title A", "a.", score=0.9))
        result = GenerateAnswerNode()(_make_state(ranked, discount_pct=30))
        actions = from_json(result["corrective_actions"])
        assert actions, "high_risk must carry corrective-action guidance"
        assert result["risk_level"] == "high_risk"

    def test_corrective_actions_is_json_string_new_list_each_call(self):
        ranked = _ranked(_doc("kb-a", "Title A", "a.", score=0.9))
        r1 = GenerateAnswerNode()(_make_state(ranked, discount_pct=30))
        r2 = GenerateAnswerNode()(_make_state(ranked, discount_pct=30))
        assert isinstance(r1["corrective_actions"], str)
        a1 = from_json(r1["corrective_actions"])
        a2 = from_json(r2["corrective_actions"])
        assert a1 == a2
        a1.append("mutated locally — must not leak into the module default")
        assert from_json(r2["corrective_actions"]) != a1

    def test_no_coverage_corrective_action_escalates_to_compliance_team(self):
        result = GenerateAnswerNode()(_make_state(_ranked()))
        actions = from_json(result["corrective_actions"])
        assert len(actions) == 1
        assert "Escalate to the compliance team" in actions[0]
