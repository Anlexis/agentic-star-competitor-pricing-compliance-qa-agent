"""AgentCore Platform v1.0"""

# RET-C2-667 - Outer graph (AgentBaseGraph; two-layer nested architecture).
#
# EC Pricing Compliance Validation Agent.
#
# Architecture:
#
#   Outer backbone (fixed - do NOT override add_edges()):
#     START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
#                                             |  (RETRY, max 3)
#                                             -> pre_process
#
#   The `main` slot is a GraphNode subclass (PricingComplianceGraphNode) that
#   delegates the pricing-compliance workflow to DomainWorkflowGraph (inner
#   graph: input_validate -> retrieve -> rerank_filter -> generate_answer ->
#   output_format). Domain complexity lives entirely inside the inner graph;
#   the outer backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 <- outer graph (this file)
#   src/graph/domain_workflow_graph.py <- inner graph (multi-step topology)
#   src/graph/context_bridge.py        <- caller-context hand-off between them
#
# Class-name contract:
#   graph.py class:           ECPricingComplianceValidationAgent (this file)
#   config/agent.yaml class:  "src.graph.graph.ECPricingComplianceValidationAgent"
#   src/api/server.py import: from src.graph.graph import ECPricingComplianceValidationAgent
#
# Rules enforced:
#   - ECPricingComplianceValidationAgent inherits AgentBaseGraph (framework base
#     class - direct inheritance)
#   - super().register_nodes() called first (fills initialize + finalize)
#   - PricingComplianceGraphNode assigned to self._nodes["main"]
#   - _parent_config() forwards the runtime tuning blocks (never {})
#   - merge_output() returns only changed keys
#   - get_output() EXTENDS super().get_output() - structured product; never
#     replaces the base envelope; SUCCESS-path only; scalar whitelist only
#   - add_edges() NOT overridden on the outer graph
#   - No platform SDK imports

from pathlib import Path
from typing import Any, ClassVar, Dict

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import stash_caller_context
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

# Runtime parameters live in config/config.yaml. The manifest (config/agent.yaml)
# is the static registry entry and carries no tuning blocks, so reading tuning
# from it would return nothing and every declared value would quietly become a
# module default. Path: src/graph/graph.py -> parents[2] = repo root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Fallbacks mirror config/config.yaml so _parent_config() never forwards an
# empty block even if the file is unreadable in an exotic deployment layout.
_FALLBACK_RETRIEVAL: Dict[str, Any] = {
    "top_k": 4,
    "score_threshold": 0.25,
    "kb_path": "config/kb/pricing_compliance_kb.json",
}
_FALLBACK_LLM: Dict[str, Any] = {
    "temperature": 0.0,
    "max_tokens": 1500,
}


def load_runtime_config() -> Dict[str, Any]:
    """Read config/config.yaml - the runtime parameter file.

    The registry passes this file's contents to the graph constructor in a
    platform deployment. The standalone entry point loads it here so a
    standalone run honours the same declared values instead of falling back to
    framework defaults.
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


class PricingComplianceGraphNode(GraphNode):
    """GraphNode assigned to the `main` slot of the outer agent.

    Wraps DomainWorkflowGraph (the inner pricing-compliance pipeline). Called
    by the backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()  - instantiate DomainWorkflowGraph with the forwarded
                        runtime config (_parent_config())
      extract_input() - pull the validated request out of outer state, and hand
                        the accepted claim context across the graph boundary
      merge_output()  - map sub_result fields into the outer state delta
      error_strategy  - "propagate": re-raise inner errors as SubgraphError
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (fail fast).
    # "handle": call on_subgraph_error() instead - for graceful degradation.
    error_strategy: ClassVar[str] = "propagate"

    # False: interrupts are handled inside the inner graph only.
    propagate_hitl: ClassVar[bool] = False

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the runtime `retrieval` + `llm` blocks to the inner graph.

        The inner graph republishes the `retrieval` block into inner state
        (DomainWorkflowGraph._extra_initial_state()), so RetrieveNode and
        RerankFilterNode read the declared top_k / score_threshold instead of
        module defaults. The `llm` block is forwarded verbatim for the
        documented synthesis upgrade (unused by the deterministic pipeline).
        """
        runtime = load_runtime_config()
        retrieval = runtime.get("retrieval")
        if not isinstance(retrieval, dict) or not retrieval:
            retrieval = dict(_FALLBACK_RETRIEVAL)
        llm = runtime.get("llm")
        if not isinstance(llm, dict) or not llm:
            llm = dict(_FALLBACK_LLM)
        return {"configurable": {"retrieval": retrieval, "llm": llm}}

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner domain workflow graph.

        Imported inside the method to avoid a circular import at module load.
        The inner graph receives the runtime-derived config via its constructor;
        its domain NODES still take no constructor arguments and read config
        per-call from seeded state.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Return the string input for inner_graph.invoke(), and bridge the context.

        The framework's GraphNode passes only a string into the subgraph - it
        does not forward input_context - so the accepted claim context is
        stashed here and picked up by the inner graph's initial-state hook. See
        src/graph/context_bridge.py for why that hand-off needs a ContextVar.
        """
        stash_caller_context(
            {
                "claim_context": state.get("claim_context"),
                "input_context": state.get("input_context") or {},
            }
        )
        request: str = state.get("validated_input") or state.get("user_input", "")
        return request

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map the inner graph's sub_result into the outer state delta.

        Returns ONLY changed keys - never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  -> "formatted_answer", "citations",
                                       "risk_level", "corrective_actions", "status"
          This merge_output() reads -> the same names

        compliance_answer: the rendered assessment.
        result: the post_process slot reads state.get("result"), so the
          rendered assessment is mapped there as well - otherwise the final
          output surfaced by PostProcessNode (and its output gate) is empty.
        citations / corrective_actions: forwarded so PostProcessNode can scan
          the underlying structured payload recursively, not only the rendered
          text, and derive its whitelisted scalar summary. Neither is surfaced
          by get_output().
        risk_level: compliance-risk classification, read by
          ECPricingComplianceValidationAgent.get_output().
        status: terminal status value from the inner graph run.
        """
        return {
            # Outer reason wins: a reason settled before the inner run is the real
            # one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "compliance_answer": sub_result.get("formatted_answer"),
            "result": sub_result.get("formatted_answer"),
            "citations": sub_result.get("citations"),
            "corrective_actions": sub_result.get("corrective_actions"),
            "risk_level": sub_result.get("risk_level"),
            "status": sub_result.get("status"),
        }


class ECPricingComplianceValidationAgent(AgentBaseGraph):
    """Outer graph for RET-C2-667.

    Inherits AgentBaseGraph directly (framework base class). Domain logic is
    encapsulated in PricingComplianceGraphNode (main slot), which delegates to
    DomainWorkflowGraph.

    Backbone (fixed):
        START -> initialize -> pre_process -> main -> post_process -> finalize -> END

    register_nodes() is the ONLY backbone override:
      - super().register_nodes() fills initialize and finalize
      - pre_process:  PreProcessNode (caller contract)
      - main:         PricingComplianceGraphNode (delegates to the inner graph)
      - post_process: PostProcessNode (output gate, recursive scan)

    add_edges() is NOT overridden - backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the platform registry."""
        return "ECPricingComplianceValidationAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first - it injects the
        framework's default initialize node (schema_version, session_id,
        trust_level) and finalize node (response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = PricingComplianceGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """EXTEND the base envelope with the whitelisted compliance-risk summary.

        The product is a risk ASSESSMENT, not rendered text alone, so this
        override adds the risk classification and vetted scalar counts on top of
        AgentBaseGraph.get_output()'s envelope (`output` / `status` /
        `trace_id` / `correlation_id` / `node_history`) - it never replaces it.

        Two invariants keep this fail-closed (see docs/02_design.md
        "Structured Output"):
          1. SUCCESS-only: any non-SUCCESS terminal status - including a
             blocked output, which PostProcessNode reports as ERROR - returns
             the base envelope UNCHANGED. No structured key is ever added on a
             blocked or errored run.
          2. Scalars only, never the raw containers: rule_citation_count /
             corrective_action_count / primary_rule_reference are the only
             fields added, all vetted scalars PostProcessNode derived AFTER its
             recursive output scan passed clean. The raw citations /
             corrective_actions payloads are never surfaced here - the full
             text reaches the caller through `output` (the rendered, already
             scanned assessment).
        """
        base: Dict[str, Any] = super().get_output(state)
        # A run that completed WITHOUT carrying out the request holds the
        # sentence saying what to correct, not a product: none of the
        # structured fields below were produced, so none is released.
        if state.get("error_code"):
            return base
        if state.get("status") != AgentStatus.SUCCESS.value:
            return base
        return {
            **base,
            "compliance_risk_level": state.get("risk_level"),
            "rule_citation_count": state.get("rule_citation_count"),
            "corrective_action_count": state.get("corrective_action_count"),
            "primary_rule_reference": state.get("primary_rule_reference"),
        }


# Back-compat alias - the manifest declares the dotted path to the agent class
# and src/api/server.py imports it directly. Keep both names pointing at it.
Graph = ECPricingComplianceValidationAgent

__all__ = [
    "ECPricingComplianceValidationAgent",
    "Graph",
    "PricingComplianceGraphNode",
    "load_runtime_config",
]
