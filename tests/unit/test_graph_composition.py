# RET-C2-667 - Unit Tests: nested graph composition (outer + end-to-end)
#
# Drives the REAL outer agent (ECPricingComplianceValidationAgent / Graph)
# end-to-end via AgentBaseGraph.invoke(). The context is
# InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL) - the
# manifest's declared caller level; for_internal() is NEVER used, because it
# would over-privilege the run and hide trust-gate regressions.
#
# Mirrors docs/03_test_spec.md Sec 3 (INT-05..INT-12).
# Deterministic - no model call, no network. framework.* / src.* imports only.

import pathlib

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

import src.graph.graph
from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.graph.graph import (
    ECPricingComplianceValidationAgent,
    Graph,
    PricingComplianceGraphNode,
)
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json, to_json

_DISCOUNT_CLAIM_QUERY = "discount comparison advertising claim against a competitor"


def _run(user_input: str, trust: TrustLevel = TrustLevel.VERIFIED_EXTERNAL) -> dict:
    ctx = InvocationContext(caller_trust_level=trust, caller_id="unit-suite")
    return Graph().invoke(user_input, ctx=ctx)


class TestOuterGraphConstruction:
    def test_int_05_inherits_agent_base_graph_directly(self):
        assert issubclass(ECPricingComplianceValidationAgent, AgentBaseGraph)

    def test_int_05_graph_alias(self):
        assert Graph is ECPricingComplianceValidationAgent

    def test_state_schema_is_state(self):
        assert ECPricingComplianceValidationAgent().state_schema is State

    def test_int_06_compile_fills_all_backbone_slots(self):
        agent = ECPricingComplianceValidationAgent()
        agent.compile()
        for slot in ("initialize", "pre_process", "main", "post_process", "finalize"):
            assert agent._nodes.get(slot) is not None, f"backbone slot not filled: {slot}"
        assert isinstance(agent._nodes["pre_process"], PreProcessNode)
        assert isinstance(agent._nodes["main"], PricingComplianceGraphNode)
        assert isinstance(agent._nodes["post_process"], PostProcessNode)

    def test_add_edges_is_not_overridden(self):
        # Backbone wiring belongs to the framework — the template must not
        # redefine it.
        assert "add_edges" not in ECPricingComplianceValidationAgent.__dict__


class TestMainSlotGraphNode:
    def test_int_07_get_subgraph_returns_the_inner_graph(self):
        subgraph = PricingComplianceGraphNode().get_subgraph()
        assert isinstance(subgraph, DomainWorkflowGraph)
        assert subgraph.config["configurable"]["retrieval"], "inner config must carry the retrieval block"

    def test_int_08_extract_input_prefers_validated_input(self):
        node = PricingComplianceGraphNode()
        assert node.extract_input({"validated_input": "VI", "user_input": "UI"}) == "VI"
        assert node.extract_input({"user_input": "UI"}) == "UI"

    def test_int_09_merge_output_maps_the_inner_contract(self):
        node = PricingComplianceGraphNode()
        citations = to_json([{"ref": 1, "id": "kb-a", "title": "t", "source": "s"}])
        actions = to_json(["a1"])
        delta = node.merge_output(
            {},
            {
                "formatted_answer": "ANSWER",
                "citations": citations,
                "corrective_actions": actions,
                "risk_level": "high_risk",
                "status": AgentStatus.SUCCESS.value,
            },
        )
        # The inner formatted_answer surfaces as BOTH compliance_answer and
        # result (PostProcessNode's output gate reads state["result"]).
        assert delta == {
            # The boundary now also carries the degraded-completion marker; on
            # an answered run neither side set one, so it crosses empty.
            "error_code": "",
            "compliance_answer": "ANSWER",
            "result": "ANSWER",
            "citations": citations,
            "corrective_actions": actions,
            "risk_level": "high_risk",
            "status": AgentStatus.SUCCESS.value,
        }

    def test_error_strategy_is_propagate_and_hitl_is_contained(self):
        assert PricingComplianceGraphNode.error_strategy == "propagate"
        assert PricingComplianceGraphNode.propagate_hitl is False

    def test_int_10_parent_config_never_empty_without_runtime_file(self, monkeypatch):
        # Even with an unreadable runtime file the forwarded config carries the
        # fallback retrieval/llm blocks - never {}.
        monkeypatch.setattr(src.graph.graph, "_RUNTIME_CONFIG_PATH", pathlib.Path("/nonexistent/config.yaml"))
        cfg = PricingComplianceGraphNode()._parent_config()
        assert cfg["configurable"]["retrieval"]["kb_path"] == "config/kb/pricing_compliance_kb.json"
        assert cfg["configurable"]["llm"]


class TestEndToEndInvoke:
    """Full agent run: outer backbone + inner domain workflow, no LLM."""

    def test_int_11_invoke_returns_success(self):
        result = _run(_DISCOUNT_CLAIM_QUERY)
        assert (
            result.get("status") == AgentStatus.SUCCESS.value
        ), f"Expected success, got {result.get('status')}. result={result!r}"

    def test_int_11_output_is_the_gated_formatted_answer(self):
        output = _run(_DISCOUNT_CLAIM_QUERY).get("output")
        assert isinstance(output, str) and output.strip()
        assert output.startswith("# EC Pricing Compliance Risk Assessment")
        assert "[1]" in output
        assert "does not constitute legal advice" in output

    def test_int_11_structured_output_extends_the_base_envelope(self):
        result = _run(_DISCOUNT_CLAIM_QUERY)
        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("compliance_risk_level") in (
            "insufficient_data",
            "low_risk",
            "medium_risk",
            "high_risk",
        )
        assert isinstance(result.get("rule_citation_count"), int)
        assert isinstance(result.get("corrective_action_count"), int)

    def test_int_11_e2e_traverses_the_post_process_gate(self):
        history = _run(_DISCOUNT_CLAIM_QUERY).get("node_history", [])
        for cls_name in ("PreProcessNode", "PricingComplianceGraphNode", "PostProcessNode"):
            assert cls_name in history, f"node_history missing {cls_name}: {history}"

    def test_no_coverage_query_still_terminates_success(self):
        result = _run("quantum telepathy sandwich recipes")
        assert result.get("status") == AgentStatus.SUCCESS.value
        assert "does not contain sufficient coverage" in result.get("output", "")

    def test_int_12_anonymous_caller_is_denied_at_the_outer_boundary(self):
        """Trust gate at graph level: an ANONYMOUS invoke is refused by the
        VERIFIED_EXTERNAL pre_process slot. The error state carries through
        the main slot (its incoming status is already ERROR, so __call__'s
        generic post-input-gate check short-circuits execute() and the inner
        graph never runs) and routes past post_process to finalize - no domain
        answer is ever produced, and no structured field is added."""
        result = _run(_DISCOUNT_CLAIM_QUERY, trust=TrustLevel.ANONYMOUS)
        assert result.get("status") == AgentStatus.ERROR.value
        assert not result.get("output")
        assert "compliance_risk_level" not in result
        history = result.get("node_history", [])
        assert "PostProcessNode" not in history
        assert history[:2] == ["InitializeNode", "PreProcessNode"]


class TestStateRoundTrip:
    """the JSON-string state contract helpers: producers to_json() on write, consumers from_json()."""

    def test_to_from_json_list_round_trip(self):
        original = [{"id": "keihyo-nijuu-kakaku-01", "score": 0.58, "title": "dual pricing"}]
        assert from_json(to_json(original)) == original

    def test_to_from_json_dict_round_trip(self):
        original = {"category": "discount_pricing", "top_k": 3}
        assert from_json(to_json(original)) == original

    def test_to_json_none_passes_through(self):
        assert to_json(None) is None

    def test_from_json_malformed_returns_default(self):
        assert from_json("{not valid json", default=[]) == []
        assert from_json(None, default={}) == {}
        assert from_json("", default=[]) == []
