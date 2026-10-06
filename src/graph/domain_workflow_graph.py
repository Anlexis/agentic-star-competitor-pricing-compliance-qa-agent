"""AgentCore Platform v1.0"""

# RET-C2-667 - DomainWorkflowGraph (inner graph)
#
# The inner graph of the two-layer nested architecture. It carries the whole
# pricing-compliance validation workflow:
#
#   START -> input_validate -> retrieve -> rerank_filter
#         -> generate_answer -> output_format -> END
#
# Called by PricingComplianceGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   - Inherits BaseGraph (fully custom topology - no forced backbone)
#   - Implements all 7 BaseGraph abstract methods
#   - register_nodes() does NOT call super() (abstract in BaseGraph)
#   - register_nodes() instantiates every domain node with NO ctor args
#   - Does NOT register initialize / finalize (outer backbone concerns)
#   - get_output() designed together with PricingComplianceGraphNode.merge_output()
#   - No platform SDK imports
#   - Not placed under src/subagents/

from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import take_caller_context
from src.nodes.generate_answer_node import GenerateAnswerNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.nodes.rerank_filter_node import RerankFilterNode
from src.nodes.retrieve_node import RetrieveNode
from src.schemas.state import State, to_json


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for RET-C2-667.

    Inherits BaseGraph directly for a fully custom node topology. Called by
    PricingComplianceGraphNode.get_subgraph() in graph.py, which passes the
    runtime-derived config into the constructor.

    Pipeline (linear):
        START
          -> input_validate  (InputValidateNode)  - compose query + filters
          -> retrieve        (RetrieveNode)       - score the seeded corpus
          -> rerank_filter   (RerankFilterNode)   - boost / threshold / top_k cut
          -> generate_answer (GenerateAnswerNode) - rationale + risk + actions
          -> output_format   (OutputFormatNode)   - final format + advisory note
          -> END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns - not registered here.
    """

    # -- Identity --------------------------------------------------------------

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "ret_c2_667_pricing_compliance_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # -- Config validation -----------------------------------------------------

    def _validate_config(self) -> None:
        """Validate inner graph config before compilation.

        The forwarded `retrieval` block (top_k / score_threshold / kb_path) is
        read per-call by the domain nodes, which bound every value they read, so
        an absent or partial block is non-fatal rather than a compile error.
        """
        pass

    # -- Initial state (runtime config + the bridged caller context) ------------

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the inner state with runtime tuning AND the caller context.

        Two things have to cross the graph boundary and neither arrives on its
        own:

        `retrieval` - PricingComplianceGraphNode._parent_config() forwards the
        runtime block under config["configurable"]; republishing it here is what
        makes it reachable by the domain nodes, which read state rather than a
        per-call config argument. It travels as a JSON string, since structured
        state fields are stored serialised.

        `claim_context` - the framework's GraphNode passes only a string into a
        subgraph, so the caller's accepted fields would otherwise stop at the
        boundary. The outer node stashes them on the way in and they are picked
        up here (src/graph/context_bridge.py).
        """
        retrieval = (self.config or {}).get("configurable", {}).get("retrieval") or {}
        bridged = take_caller_context()
        return {
            "retrieval_config": to_json(retrieval),
            "claim_context": bridged.get("claim_context"),
            "input_context": bridged.get("input_context") or {},
        }

    # -- Node registration -----------------------------------------------------

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call - BaseGraph.register_nodes() is abstract. Do NOT
        register initialize or finalize; those are outer backbone concerns.

        Every node is instantiated with NO constructor arguments: domain node
        classes take no __init__, and config flows in via the state seeding
        above rather than a per-call execute() parameter. Every key registered
        here is referenced in add_edges().
        """
        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["retrieve"] = RetrieveNode()
        self._nodes["rerank_filter"] = RerankFilterNode()
        self._nodes["generate_answer"] = GenerateAnswerNode()
        self._nodes["output_format"] = OutputFormatNode()

    # -- Edge wiring -----------------------------------------------------------

    def add_edges(self) -> None:
        """Wire the linear pricing-compliance topology.

        Each step passes its partial-dict output into the shared State. The
        topology is intentionally linear - there is no conditional branching
        between domain nodes, so add_conditional_edges() is not used and route()
        below exists only to satisfy the abstract base contract.
        """
        self._sg.add_edge(START, "input_validate")
        self._sg.add_edge("input_validate", "retrieve")
        self._sg.add_edge("retrieve", "rerank_filter")
        self._sg.add_edge("rerank_filter", "generate_answer")
        self._sg.add_edge("generate_answer", "output_format")
        self._sg.add_edge("output_format", END)

    # -- Routing ---------------------------------------------------------------

    def route(self, state: State) -> str:
        """Conditional routing - required by the abstract base contract.

        Annotated with this graph's OWN State, not the framework base state:
        LangGraph reads a path callable's annotation as its input schema and
        projects away every field the annotation does not declare, so a callable
        annotated with the base state would be handed a state with all the
        domain fields missing. This topology does not call add_conditional_edges,
        but the annotation is correct here so that adding a branch later does
        not silently route on absent fields.

        Returns END on error so an unexpected call cannot re-enter a processing
        node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_format"

    # -- Output shape ----------------------------------------------------------

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        Received by PricingComplianceGraphNode.merge_output() in graph.py as its
        `sub_result` argument. Both methods are designed together so the field
        names cannot drift apart:

            Inner get_output()  emits: "formatted_answer", "citations",
                                        "risk_level", "corrective_actions",
                                        "status", ...
            Outer merge_output() reads: the same names

        The remaining fields (intake_notes, trace_id, correlation_id,
        node_history) are surfaced for observability and are available to a
        future outer-merge extension without an inner-graph change.
        """
        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "formatted_answer": state.get("formatted_answer"),
            "citations": state.get("citations"),
            "risk_level": state.get("risk_level"),
            "corrective_actions": state.get("corrective_actions"),
            "status": state.get("status"),
            "intake_notes": state.get("intake_notes"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
