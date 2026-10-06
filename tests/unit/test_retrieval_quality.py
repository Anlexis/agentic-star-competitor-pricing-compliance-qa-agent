# RET-C2-667 — Unit Tests: retrieval quality (golden queries)
#
# Invocation canon: node(state) via BaseNode.__call__ with an ANONYMOUS caller.
# Deterministic keyword-overlap scoring over the seeded
# config/kb/pricing_compliance_kb.json (10 entries, 8 categories). Each golden
# query below was verified against the live scorer, not guessed — retrieval is
# store-agnostic keyword weighting (title 1.0 > tags 0.8 > content 0.5).
#
# Mirrors docs/03_test_spec.md Sec 2.9 (QUAL-01..QUAL-07).
# Deterministic — no LLM, no network. framework.* / src.* imports only.

from framework.schemas.trust_level import TrustLevel

from src.nodes.rerank_filter_node import RerankFilterNode
from src.nodes.retrieve_node import RetrieveNode
from src.schemas.state import from_json, to_json

# query -> expected top-1 KB id. Covers 9 of the 10 seeded entries across 7 of
# the 8 categories (keihyo-005-2-01 / advantageous_misrepresentation is
# exercised as a top-3 hit, not top-1, by the "quality" and the module-level
# discount-comparison queries elsewhere in this suite).
_GOLDEN_QUERIES = {
    "discount comparison advertising claim against a competitor": "keihyo-nijuu-kakaku-01",
    "three conditions for lawful comparison claim against a named competitor": "keihyo-hikaku-koukoku-01",
    "is claiming our product quality is far superior to a competitor's without evidence a violation": "keihyo-005-1-01",
    "what evidence must we retain to substantiate a discount claim if challenged by the agency": "keihyo-substantiation-01",
    "can we keep re-running the same limited-time sale offer again and again": "keihyo-genteihyouji-01",
    "what corrective order and surcharge penalty applies for a confirmed violation": "keihyo-sochi-kachoukin-01",
    "what internal pre-publication review process should we set up before publishing price claims": "keihyo-jishu-shinsa-01",
    "where should the reference price be displayed on our online store product page": "keihyo-web-ec-01",
    "can we ask the consumer affairs agency for informal advice before publishing a claim": "keihyo-soudan-madoguchi-01",
}

_ALL_KB_IDS = {
    "keihyo-005-2-01",
    "keihyo-nijuu-kakaku-01",
    "keihyo-hikaku-koukoku-01",
    "keihyo-005-1-01",
    "keihyo-substantiation-01",
    "keihyo-genteihyouji-01",
    "keihyo-sochi-kachoukin-01",
    "keihyo-jishu-shinsa-01",
    "keihyo-web-ec-01",
    "keihyo-soudan-madoguchi-01",
}


def _retrieve_state(query, **extra):
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


class TestGoldenQueryTopHit:
    def test_qual_01_each_golden_query_top_hit_matches(self):
        for query, expected_id in _GOLDEN_QUERIES.items():
            docs = from_json(RetrieveNode()(_retrieve_state(query))["retrieved_rules"])
            assert docs, f"no candidates for query: {query!r}"
            assert (
                docs[0]["id"] == expected_id
            ), f"query {query!r}: expected top-1 {expected_id!r}, got {docs[0]['id']!r}"


class TestRelevanceFloorAndIntegrity:
    def test_qual_02_every_survivor_clears_the_relevance_floor(self):
        for query in _GOLDEN_QUERIES:
            retrieved = RetrieveNode()(_retrieve_state(query))
            ranked = from_json(
                RerankFilterNode()(
                    {
                        "retrieved_rules": retrieved["retrieved_rules"],
                        "claim_filters": to_json({"category": None, "top_k": None, "discount_pct": None}),
                        "caller_trust_level": TrustLevel.ANONYMOUS.value,
                        "node_history": [],
                        "error_log": [],
                        "session_id": "unit-session",
                        "execution_time": {},
                    }
                )["ranked_rules"]
            )
            assert all(c["score"] >= 0.25 for c in ranked), f"survivor below relevance floor for {query!r}"

    def test_qual_03_citation_ids_exist_in_the_seeded_kb(self):
        for query in _GOLDEN_QUERIES:
            docs = from_json(RetrieveNode()(_retrieve_state(query))["retrieved_rules"])
            for doc in docs:
                assert doc["id"] in _ALL_KB_IDS, f"unknown KB id surfaced: {doc['id']!r}"


class TestCategoryFilterPrecision:
    def test_qual_04_comparative_advertising_filter_is_precise(self):
        state = _retrieve_state(
            "three conditions for lawful comparison claim",
            claim_filters=to_json({"category": "comparative_advertising", "top_k": None, "discount_pct": None}),
        )
        docs = from_json(RetrieveNode()(state)["retrieved_rules"])
        assert docs and {d["category"] for d in docs} == {"comparative_advertising"}
        assert docs[0]["id"] == "keihyo-hikaku-koukoku-01"


class TestNoCoverage:
    def test_qual_05_out_of_domain_query_yields_zero_survivors(self):
        docs = from_json(RetrieveNode()(_retrieve_state("quantum telepathy sandwich recipes"))["retrieved_rules"])
        assert docs == []
