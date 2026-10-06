# RET-C2-667 - Unit Tests: manifest / config consistency
#
# Configuration is split in two, and the split is load-bearing:
#   config/agent.yaml  - the static registry entry (flat; identity, class path,
#                        trust level, compile-time requirements)
#   config/config.yaml - runtime parameters (max_retry / timeout_s, and the
#                        retrieval + llm tuning blocks the graph forwards)
#
# These tests pin manifest <-> code consistency so a drift fails fast in CI, and
# they pin the runtime file specifically because a reader left pointing at the
# old location does not fail - it silently returns nothing and every declared
# value quietly becomes a module default.
#
# Mirrors docs/03_test_spec.md Sec 2.8 (CFG-01..CFG-08). Deterministic.

import json
import pathlib

import yaml

from framework.schemas.trust_level import TrustLevel

from src.graph.graph import (
    ECPricingComplianceValidationAgent,
    PricingComplianceGraphNode,
    load_runtime_config,
)
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MANIFEST = yaml.safe_load((_ROOT / "config" / "agent.yaml").read_text(encoding="utf-8"))
_RUNTIME = yaml.safe_load((_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))


class TestManifestIdentity:
    def test_cfg_01_manifest_is_flat_and_identifies_the_template(self):
        # Every key the registry reads sits at ROOT level - a nested `agent:`
        # block is the retired shape and would not be read at all.
        assert "agent" not in _MANIFEST
        assert _MANIFEST["id"] == "RET-C2-667"
        assert _MANIFEST["enabled"] is True

    def test_cfg_02_declared_class_is_the_graph_class(self):
        # Class-name contract: manifest class path == graph.py class == server import.
        assert _MANIFEST["class"] == ("src.graph.graph." + ECPricingComplianceValidationAgent.__name__)
        assert _MANIFEST["name"] == ECPricingComplianceValidationAgent().name

    def test_cfg_03_category_and_industry(self):
        assert _MANIFEST["category"] == "Cat 2"
        assert _MANIFEST["industry"] == "RET"
        assert _MANIFEST["namespace"] == "ret"
        assert _MANIFEST["base_type"] == "RAGAgent"

    def test_cfg_08_declares_no_unprovisioned_requirement(self):
        # The pipeline calls no model and requires no secret. Declaring either
        # would make the agent fail to compile in a deployment that has not
        # provisioned it, so the declaration must follow the code.
        assert _MANIFEST["generation_mode"] == "deterministic"
        assert _MANIFEST["requires"]["secrets"] == []
        assert _MANIFEST["requires"]["extras"] == []


class TestManifestSecurity:
    def test_cfg_04_required_trust_level_matches_outer_gate_nodes(self):
        declared = TrustLevel(_MANIFEST["required_trust_level"])
        assert declared is TrustLevel.VERIFIED_EXTERNAL
        assert PreProcessNode.required_trust_level is declared
        assert PostProcessNode.required_trust_level is declared

    def test_cfg_05_max_retry_within_framework_ceiling(self):
        max_retry = _RUNTIME["max_retry"]
        assert isinstance(max_retry, int)
        assert 0 <= max_retry < 10  # framework retry ceiling

    def test_cfg_05b_timeout_uses_the_key_the_framework_validates(self):
        # The runtime key is timeout_s; the retired manifest spelled it
        # timeout_seconds, which nothing reads.
        assert "timeout_s" in _RUNTIME
        assert "timeout_seconds" not in _RUNTIME
        assert isinstance(_RUNTIME["timeout_s"], int)

    def test_hitl_is_not_enabled(self):
        # Interrupt-propagation waiver: this template declares no human step.
        assert (_RUNTIME.get("hitl") or {}).get("enabled", False) is False


class TestRuntimeTuningReachesTheNodes:
    def test_cfg_06_retrieval_block_matches_node_defaults(self):
        # Node module defaults mirror the runtime file - a drift silently
        # changes tuning for anyone reading the file to learn the values.
        retrieval = _RUNTIME["retrieval"]
        from src.nodes.retrieve_node import _DEFAULT_RETRIEVAL as retrieve_defaults
        from src.nodes.rerank_filter_node import _DEFAULT_RETRIEVAL as rerank_defaults

        assert retrieval["top_k"] == retrieve_defaults["top_k"] == rerank_defaults["top_k"]
        assert (
            retrieval["score_threshold"] == retrieve_defaults["score_threshold"] == rerank_defaults["score_threshold"]
        )
        assert retrieval["kb_path"] == retrieve_defaults["kb_path"]
        assert (_ROOT / retrieval["kb_path"]).is_file()

    def test_cfg_07_parent_config_forwards_the_runtime_blocks(self):
        cfg = PricingComplianceGraphNode()._parent_config()
        assert cfg["configurable"]["retrieval"] == _RUNTIME["retrieval"]
        assert cfg["configurable"]["llm"] == _RUNTIME["llm"]
        assert cfg["configurable"]["retrieval"], "_parent_config() must never forward an empty retrieval block"

    def test_runtime_loader_reads_the_live_file(self):
        # The loader is what the standalone entry point passes to the graph
        # constructor; if it read the wrong file the declared max_retry would
        # never reach the framework.
        loaded = load_runtime_config()
        assert loaded["max_retry"] == _RUNTIME["max_retry"]
        assert loaded["timeout_s"] == _RUNTIME["timeout_s"]


class TestSeededKnowledgeBase:
    def _entries(self):
        return json.loads((_ROOT / _RUNTIME["retrieval"]["kb_path"]).read_text(encoding="utf-8"))

    def test_kb_is_a_well_formed_entry_list(self):
        entries = self._entries()
        assert isinstance(entries, list)
        assert len(entries) >= 5, "seeded knowledge base must carry a usable corpus"
        for entry in entries:
            assert set(entry.keys()) == {"id", "title", "category", "source", "tags", "content"}
            assert entry["id"] and entry["title"] and entry["content"]

    def test_kb_ids_are_unique(self):
        ids = [e["id"] for e in self._entries()]
        assert len(ids) == len(set(ids))

    def test_kb_has_no_live_lookup_fields(self):
        # Stated constraint (docs/02_design.md): no web crawling and no
        # competitor-price lookup - the seeded corpus is the only retrieval
        # source, so it must not carry a URL / endpoint field that would imply
        # a live fetch.
        for entry in self._entries():
            assert "url" not in entry and "api_endpoint" not in entry

    def test_kb_categories_match_the_accepted_category_alphabet(self):
        # A caller filters by category, and the caller contract locks that field
        # to an inert identifier. If the corpus used categories outside that
        # alphabet the filter could never match them.
        from src.services.caller_contract import inert_category

        for entry in self._entries():
            assert inert_category(entry["category"], field="category") == entry["category"]
