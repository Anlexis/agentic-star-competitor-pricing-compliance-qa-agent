# RET-C2-667 - Unit Tests: DomainWorkflowGraph (the inner graph)
#
# Inner-graph composition plus a full inner invoke() over the seeded corpus.
# The inner graph runs the 5 domain nodes (all ANONYMOUS) - the outer trust
# boundary is the backbone's concern and is covered in
# test_graph_composition.py and the boundary suite.
#
# Mirrors docs/03_test_spec.md Sec 3 (INT-01..INT-04).
# Deterministic — no LLM, no network. framework.* / src.* imports only.

from langgraph.graph import END

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.graph.graph import PricingComplianceGraphNode
from src.nodes.generate_answer_node import GenerateAnswerNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.nodes.rerank_filter_node import RerankFilterNode
from src.nodes.retrieve_node import RetrieveNode
from src.schemas.state import State, from_json

_DISCOUNT_CLAIM_QUERY = "discount comparison advertising claim against a competitor"


class TestInnerGraphConstruction:
    def test_int_01_inherits_base_graph(self):
        assert issubclass(DomainWorkflowGraph, BaseGraph)

    def test_int_01_registers_the_five_domain_nodes(self):
        inner = DomainWorkflowGraph()
        inner.register_nodes()
        assert set(inner._nodes.keys()) == {
            "input_validate",
            "retrieve",
            "rerank_filter",
            "generate_answer",
            "output_format",
        }
        assert isinstance(inner._nodes["input_validate"], InputValidateNode)
        assert isinstance(inner._nodes["retrieve"], RetrieveNode)
        assert isinstance(inner._nodes["rerank_filter"], RerankFilterNode)
        assert isinstance(inner._nodes["generate_answer"], GenerateAnswerNode)
        assert isinstance(inner._nodes["output_format"], OutputFormatNode)

    def test_inner_graph_name_and_schema(self):
        inner = DomainWorkflowGraph()
        assert inner.name == "ret_c2_667_pricing_compliance_workflow"
        assert inner.state_schema is State

    def test_initialize_finalize_are_not_registered(self):
        # Outer backbone concerns must not leak into the inner topology.
        inner = DomainWorkflowGraph()
        inner.register_nodes()
        assert "initialize" not in inner._nodes
        assert "finalize" not in inner._nodes


class TestConfigForwarding:
    def test_int_02_extra_initial_state_republishes_retrieval_block(self):
        inner = DomainWorkflowGraph(config={"configurable": {"retrieval": {"top_k": 2}}})
        extra = inner._extra_initial_state()
        # Two things cross the boundary here: the runtime tuning block, and the
        # caller context the framework's GraphNode does not forward.
        assert set(extra.keys()) == {"retrieval_config", "claim_context", "input_context"}
        assert isinstance(extra["retrieval_config"], str)  # stored as a JSON string
        assert from_json(extra["retrieval_config"]) == {"top_k": 2}

    def test_extra_initial_state_with_no_config_is_empty_block(self):
        assert from_json(DomainWorkflowGraph()._extra_initial_state()["retrieval_config"]) == {}


class TestOutputShape:
    def test_int_03_get_output_shapes_the_merge_contract(self):
        inner = DomainWorkflowGraph()
        out = inner.get_output(
            {
                "formatted_answer": "ANSWER",
                "citations": "[]",
                "risk_level": "medium_risk",
                "corrective_actions": "[]",
                "status": AgentStatus.SUCCESS.value,
                "node_history": ["InputValidateNode"],
            }
        )
        assert out["formatted_answer"] == "ANSWER"
        assert out["citations"] == "[]"
        assert out["risk_level"] == "medium_risk"
        assert out["corrective_actions"] == "[]"
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["node_history"] == ["InputValidateNode"]

    def test_route_returns_end_on_error(self):
        inner = DomainWorkflowGraph()
        assert inner.route({"status": AgentStatus.ERROR.value}) == END
        assert inner.route({"status": AgentStatus.SUCCESS.value}) == "output_format"


class TestInnerEndToEnd:
    def _invoke(self, payload: str) -> dict:
        # Same construction path the outer GraphNode uses: manifest-derived
        # config via _parent_config(); domain nodes take NO ctor args.
        inner = DomainWorkflowGraph(config=PricingComplianceGraphNode()._parent_config())
        return inner.invoke(payload, session_id="inner-e2e")

    def test_int_04_full_inner_run_produces_the_formatted_answer(self):
        result = self._invoke(_DISCOUNT_CLAIM_QUERY)
        assert result["status"] == AgentStatus.SUCCESS.value
        answer = result["formatted_answer"]
        assert answer.startswith("# EC Pricing Compliance Risk Assessment")
        assert "[1]" in answer
        assert "does not constitute legal advice" in answer
        citations = from_json(result["citations"])
        assert citations and citations[0]["id"] == "keihyo-nijuu-kakaku-01"

    def test_int_04_inner_node_history_is_the_linear_topology(self):
        history = self._invoke(_DISCOUNT_CLAIM_QUERY)["node_history"]
        assert history == [
            "InputValidateNode",
            "RetrieveNode",
            "RerankFilterNode",
            "GenerateAnswerNode",
            "OutputFormatNode",
        ]

    def test_no_coverage_query_still_terminates_success(self):
        result = self._invoke("quantum telepathy sandwich recipes")
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "does not contain sufficient coverage" in result["formatted_answer"]
        assert result["risk_level"] == "insufficient_data"
