# RET-C2-667 — Unit Tests: RerankFilterNode (inner domain node 3)
#
# Invocation canon: node(state) via BaseNode.__call__ with an ANONYMOUS caller.
# C2 RETIRED (2026-07-27): execute(self, state) is the only signature — score
# threshold / top_k overrides are exercised by seeding
# state["retrieval_config"], never by a 2nd positional `config` argument.
#
# Mirrors docs/03_test_spec.md Sec 2.4 (RRF-01..RRF-08).
# Deterministic — no LLM, no network. framework.* / src.* imports only.

from framework.schemas.trust_level import TrustLevel

from src.nodes.rerank_filter_node import RerankFilterNode
from src.schemas.state import from_json, to_json


def _doc(doc_id, score, category="discount_pricing"):
    return {
        "id": doc_id,
        "title": f"entry {doc_id}",
        "category": category,
        "source": "seeded kb",
        "score": score,
        "excerpt": "excerpt text",
    }


def _make_state(candidates, **extra) -> dict:
    state = {
        "retrieved_rules": to_json(candidates),
        "claim_filters": to_json({"category": None, "top_k": None, "discount_pct": None}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestThresholdAndCap:
    def test_rrf_01_default_threshold_drops_weak_candidates(self):
        result = RerankFilterNode()(_make_state([_doc("a", 0.9), _doc("b", 0.1)]))
        kept = from_json(result["ranked_rules"])
        assert [d["id"] for d in kept] == ["a"]  # 0.1 < default 0.25 floor

    def test_rrf_02_state_score_threshold_override(self):
        state = _make_state(
            [_doc("a", 0.9), _doc("b", 0.3)],
            retrieval_config=to_json({"score_threshold": 0.5}),
        )
        kept = from_json(RerankFilterNode()(state)["ranked_rules"])
        assert [d["id"] for d in kept] == ["a"]

    def test_rrf_03_state_top_k_override(self):
        state = _make_state(
            [_doc("a", 0.9), _doc("b", 0.8), _doc("c", 0.7)],
            retrieval_config=to_json({"top_k": 1}),
        )
        kept = from_json(RerankFilterNode()(state)["ranked_rules"])
        assert [d["id"] for d in kept] == ["a"]

    def test_ranked_rules_is_json_string(self):
        # the JSON-string state contract: list-shaped State fields travel as JSON strings.
        result = RerankFilterNode()(_make_state([_doc("a", 0.9)]))
        assert isinstance(result["ranked_rules"], str)


class TestCategoryBoost:
    def test_rrf_04_matching_category_is_boosted_and_reranked(self):
        state = _make_state(
            [_doc("a", 0.30, category="operations"), _doc("b", 0.25, category="discount_pricing")],
            claim_filters=to_json({"category": "discount_pricing", "top_k": None, "discount_pct": None}),
        )
        kept = from_json(RerankFilterNode()(state)["ranked_rules"])
        assert [d["id"] for d in kept] == ["b", "a"]
        assert kept[0]["score"] == 0.35  # 0.25 + 0.1 category boost

    def test_rrf_05_boost_is_capped_at_one(self):
        state = _make_state(
            [_doc("a", 0.95, category="discount_pricing")],
            claim_filters=to_json({"category": "discount_pricing", "top_k": None, "discount_pct": None}),
        )
        kept = from_json(RerankFilterNode()(state)["ranked_rules"])
        assert kept[0]["score"] == 1.0


class TestCallerTopK:
    def test_rrf_06_stricter_caller_top_k_wins(self):
        state = _make_state(
            [_doc("a", 0.9), _doc("b", 0.8), _doc("c", 0.7)],
            claim_filters=to_json({"category": None, "top_k": 1, "discount_pct": None}),
        )
        kept = from_json(RerankFilterNode()(state)["ranked_rules"])
        assert [d["id"] for d in kept] == ["a"]

    def test_rrf_06_looser_caller_top_k_does_not_widen(self):
        state = _make_state(
            [_doc("a", 0.9), _doc("b", 0.8), _doc("c", 0.7)],
            claim_filters=to_json({"category": None, "top_k": 10, "discount_pct": None}),
            retrieval_config=to_json({"top_k": 2, "score_threshold": 0.25}),
        )
        kept = from_json(RerankFilterNode()(state)["ranked_rules"])
        assert [d["id"] for d in kept] == ["a", "b"]


class TestRobustness:
    def test_rrf_07_garbage_candidates_are_skipped_or_dropped(self):
        candidates = [
            "not-a-dict",
            {"id": "bad", "title": "b", "category": "x", "source": "s", "score": "NaN?", "excerpt": "e"},
            _doc("a", 0.9),
        ]
        kept = from_json(RerankFilterNode()(_make_state(candidates))["ranked_rules"])
        # The string entry is skipped; the uncoercible score becomes 0.0 and
        # falls below the relevance floor.
        assert [d["id"] for d in kept] == ["a"]

    def test_rrf_08_deterministic_tie_break_by_id(self):
        kept = from_json(RerankFilterNode()(_make_state([_doc("b", 0.5), _doc("a", 0.5)]))["ranked_rules"])
        assert [d["id"] for d in kept] == ["a", "b"]
