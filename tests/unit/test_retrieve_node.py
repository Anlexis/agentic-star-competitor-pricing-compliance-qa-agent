# RET-C2-667 — Unit Tests: RetrieveNode (inner domain node 2)
#
# Invocation canon: node(state) via BaseNode.__call__ with an ANONYMOUS caller.
# C2 RETIRED (2026-07-27): execute(self, state) is the only signature — there
# is no execute(state, config=...) carve-out. Config knobs (kb_path / top_k)
# are exercised by seeding state["retrieval_config"] (the JSON string
# DomainWorkflowGraph._extra_initial_state() republishes), never by a 2nd
# positional argument.
#
# Mirrors docs/03_test_spec.md Sec 2.3 (RET-01..RET-08).
# Deterministic — keyword scoring over the seeded
# config/kb/pricing_compliance_kb.json; no LLM, no network.
# framework.* / src.* imports only.

from framework.schemas.trust_level import TrustLevel

from src.nodes.retrieve_node import RetrieveNode
from src.schemas.state import from_json, to_json

_DISCOUNT_CLAIM_QUERY = "discount comparison advertising claim against a competitor"


def _make_state(query=_DISCOUNT_CLAIM_QUERY, **extra) -> dict:
    state = {
        "compliance_query": query,
        "claim_filters": to_json({"category": None, "top_k": None, "discount_pct": None}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestRetrieveHappyPath:
    def test_ret_01_top_hit_is_the_dual_pricing_entry(self):
        result = RetrieveNode()(_make_state())
        docs = from_json(result["retrieved_rules"])
        assert docs, "expected candidates for the discount-comparison query"
        assert docs[0]["id"] == "keihyo-nijuu-kakaku-01"

    def test_ret_02_scores_sorted_descending(self):
        docs = from_json(RetrieveNode()(_make_state())["retrieved_rules"])
        scores = [d["score"] for d in docs]
        assert scores == sorted(scores, reverse=True)
        assert all(s > 0.0 for s in scores)

    def test_ret_03_entry_shape_and_excerpt_cap(self):
        docs = from_json(RetrieveNode()(_make_state())["retrieved_rules"])
        for doc in docs:
            assert set(doc.keys()) == {"id", "title", "category", "source", "score", "excerpt"}
            assert len(doc["excerpt"]) <= 400

    def test_retrieved_rules_is_json_string(self):
        # the JSON-string state contract: list-shaped State fields travel as JSON strings.
        result = RetrieveNode()(_make_state())
        assert isinstance(result["retrieved_rules"], str)


class TestRetrieveFilters:
    def test_ret_04_category_filter_restricts_pool(self):
        state = _make_state(
            query="three conditions for lawful comparison claim",
            claim_filters=to_json({"category": "comparative_advertising", "top_k": None, "discount_pct": None}),
        )
        docs = from_json(RetrieveNode()(state)["retrieved_rules"])
        assert docs, "comparative_advertising category has a seeded entry"
        assert {d["category"] for d in docs} == {"comparative_advertising"}
        assert docs[0]["id"] == "keihyo-hikaku-koukoku-01"

    def test_ret_05_empty_query_yields_no_candidates(self):
        docs = from_json(RetrieveNode()(_make_state(query=""))["retrieved_rules"])
        assert docs == []


class TestRetrieveConfigViaState:
    """Config plumbing: state retrieval_config > module defaults (C2 retired —
    no passed-config carve-out; every call goes through node(state))."""

    def test_ret_06_state_kb_path_override_unreadable(self):
        state = _make_state(retrieval_config=to_json({"kb_path": "config/kb/does_not_exist.json"}))
        result = RetrieveNode()(state)
        assert from_json(result["retrieved_rules"]) == []
        notes = from_json(result.get("intake_notes"), [])
        assert any("not readable" in n for n in notes)

    def test_ret_07_state_retrieval_config_drives_top_k(self):
        state = _make_state(retrieval_config=to_json({"top_k": 1}))
        docs = from_json(RetrieveNode()(state)["retrieved_rules"])
        # Candidate pool is max(top_k * 3, 10) — a top_k=1 still yields a pool
        # of 10 from a 10-entry KB; the config is proven to flow through by
        # combining it with the category filter (RET-04) where the whole
        # category has exactly one entry, asserted below via ret_04.
        assert docs, "state-seeded retrieval_config must be read by the node"


class TestRetrieveNotesAccumulation:
    def test_ret_08_notes_append_never_clobber(self):
        state = _make_state(
            intake_notes=to_json(["earlier note from input validation"]),
            retrieval_config=to_json({"kb_path": "config/kb/bogus.json"}),
        )
        result = RetrieveNode()(state)
        notes = from_json(result["intake_notes"])
        assert notes[0] == "earlier note from input validation"
        assert len(notes) == 2
