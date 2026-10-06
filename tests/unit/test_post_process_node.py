# RET-C2-667 - Unit Tests: PostProcessNode (the output boundary)
#
# Invocation canon: node(state) via BaseNode.__call__. PostProcessNode is the
# second outer gate slot and requires VERIFIED_EXTERNAL (like PreProcessNode),
# so its behavioural tests build the state at that level; the ANONYMOUS
# rejection lives in test_trust_gate.py.
#
# Gate layering: the node's own module-level _security_gate_output() scan runs
# INSIDE execute() and RECURSES into dict/list/tuple containers - it scans the
# rendered `result` text AND the structured `citations` / `corrective_actions`
# payload (deserialised from State), not just a flat top-level string. A
# violation anywhere returns a contained error (a returned dict - no
# exception), so the framework's own credential scan then sees only the clean
# stub.
#
# TestRecursiveGateProvesNestedScan is the explicit proof that a credential
# buried inside a returned structured payload cannot bypass the scan.
# TestViolationIsContained is the proof that refusing is not the same as
# containing: the envelope builder falls back to state["result"] even on an
# error run, so the blocked path has to leave nothing there to fall back to.
#
# Mirrors docs/03_test_spec.md Sec 2.7 (POST-01..POST-09).
# Deterministic - no model call, no network. framework.* / src.* imports only.

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import to_json

_CLEAN_REPORT = (
    "# EC Pricing Compliance Risk Assessment\n\n"
    "**Risk Level:** LOW RISK\n\n"
    "[1] a clean compliance rationale with no secrets.\n"
)

# JWT-shaped token built at runtime so no credential-shaped literal ever sits
# in the repository (credential-scan hygiene).
_FAKE_JWT = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12


def _make_state(result_text, **extra) -> dict:
    state = {
        "result": result_text,
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestPostProcessClean:
    def test_post_01_clean_output_passes_through(self):
        result = PostProcessNode()(_make_state(_CLEAN_REPORT))
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the plain status string, never the enum.
        assert type(result["status"]) is str  # noqa: E721 - AgentStatus subclasses str, so isinstance() would pass for
        # the enum too and this guard exists precisely to catch the enum.
        assert result["formatted_output"] == _CLEAN_REPORT

    def test_post_02_empty_result_is_non_fatal(self):
        result = PostProcessNode()(_make_state(""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == ""

    def test_clean_path_derives_the_whitelisted_scalar_summary(self):
        citations = to_json(
            [
                {"ref": 1, "id": "kb-a", "title": "T1", "source": "S1"},
                {"ref": 2, "id": "kb-b", "title": "T2", "source": "S2"},
            ]
        )
        actions = to_json(["a1", "a2", "a3"])
        result = PostProcessNode()(_make_state("clean output text", citations=citations, corrective_actions=actions))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["rule_citation_count"] == 2
        assert result["corrective_action_count"] == 3
        assert result["primary_rule_reference"] == "kb-a"

    def test_clean_path_with_no_citations_has_no_primary_reference(self):
        result = PostProcessNode()(_make_state("clean text with no citations"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["rule_citation_count"] == 0
        assert result["corrective_action_count"] == 0
        assert result["primary_rule_reference"] is None


class TestGateBlocksFlatSecret:
    """POST-03..06 - a credential in the flat `result` string (the shallow
    case a top-level-only scan would also have caught)."""

    def _assert_blocked(self, result, secret):
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output blocked" in str(e) for e in result["error_log"])
        # The raw secret must not survive into either surfaced field.
        assert secret not in str(result.get("formatted_output", ""))
        assert secret not in str(result.get("result", ""))
        assert "[OUTPUT BLOCKED" in result["formatted_output"]

    def test_post_03_api_key_is_blocked(self):
        secret = "sk-ABCDEF0123456789abcdef"
        result = PostProcessNode()(_make_state(f"# Report\n\n<!-- debug api_key={secret} -->\n"))
        self._assert_blocked(result, secret)

    def test_post_04_credential_assignment_is_blocked(self):
        secret = "password=super_secret_value_123"
        result = PostProcessNode()(_make_state(f"# Report\n\ninternal note: {secret}\n"))
        self._assert_blocked(result, "super_secret_value_123")

    def test_post_05_jwt_is_blocked(self):
        result = PostProcessNode()(_make_state(f"# Report\n\nsession token {_FAKE_JWT}\n"))
        self._assert_blocked(result, _FAKE_JWT)

    def test_post_06_bearer_token_is_blocked(self):
        secret = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"
        result = PostProcessNode()(_make_state(f"# Report\n\nauthorization: {secret}\n"))
        self._assert_blocked(result, secret)


class TestRecursiveGateProvesNestedScan:
    """POST-07 - proves the scan RECURSES, not just the flat-string case above.

    The top-level `result` text is completely clean; the credential is nested
    one level down inside a structured field (`citations[i]["source"]`, or a
    `corrective_actions[i]` string). A scan that only checked the top-level
    result string would let these through untouched. The clean-nested test at
    the end is the control that proves the verifier itself works: without it, a
    gate that blocked everything nested would pass the two leak tests too.
    Assert the whole returned dict, not just formatted_output, so a leak into
    any other surfaced field is also caught.
    """

    def test_credential_nested_inside_a_citation_source_is_blocked(self):
        secret = "sk-" + "A" * 24
        citations = to_json([{"ref": 1, "id": "kb-a", "title": "Title A", "source": secret}])
        result = PostProcessNode()(
            _make_state(
                "a completely clean compliance summary with no secrets in it",
                citations=citations,
            )
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output blocked" in str(e) for e in result["error_log"])
        assert any("PostProcessNode" in str(e) for e in result["error_log"])
        assert result["formatted_output"] == (
            "[OUTPUT BLOCKED - disallowed content detected. Review the generated "
            "output and retry without credential-like strings.]"
        )
        # The secret must not have leaked into ANY surfaced field.
        assert secret not in str(result)
        # Fail-closed: the scalar summary is cleared, never left stale, on the
        # blocked path - get_output()'s SUCCESS-only check then has nothing to
        # surface even if it were reached.
        assert result["rule_citation_count"] is None
        assert result["primary_rule_reference"] is None

    def test_credential_nested_inside_a_corrective_action_is_blocked(self):
        secret = "password=" + "verysecretvalue123456"
        actions = to_json(["retain substantiation records", f"internal note: {secret}"])
        result = PostProcessNode()(
            _make_state(
                "a completely clean compliance summary with no secrets in it",
                corrective_actions=actions,
            )
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert secret not in str(result)
        assert "verysecretvalue123456" not in str(result)

    def test_clean_nested_citations_and_actions_pass_through(self):
        """Negative control for the two tests above: nested structures with NO
        credential-shaped content must reach the clean/SUCCESS path — the
        recursive scan is a filter, not a wall that blocks everything nested."""
        citations = to_json([{"ref": 1, "id": "kb-a", "title": "Title A", "source": "Source A"}])
        actions = to_json(["retain substantiation records on file"])
        result = PostProcessNode()(_make_state(_CLEAN_REPORT, citations=citations, corrective_actions=actions))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == _CLEAN_REPORT
        assert result["rule_citation_count"] == 1
        assert result["corrective_action_count"] == 1


class TestViolationIsContained:
    """POST-08/09 - a violating gate must CLEAR the output-bearing fields.

    Refusing is not containing. The framework builds the caller's envelope as
    `state["formatted_output"] or state["result"]`, and it does that on an
    ERROR run too - so a gate that merely raises, or that returns ERROR while
    leaving the answer sitting in state, still ships the un-gated text inside
    the error envelope. These tests pin the containment, not just the refusal.
    """

    def test_post_08_every_output_bearing_field_is_cleared(self):
        secret = "sk-" + "B" * 24
        result = PostProcessNode()(
            _make_state(
                f"# Assessment\n\nleaked {secret}\n",
                compliance_answer=f"# Assessment\n\nleaked {secret}\n",
                formatted_answer=f"# Assessment\n\nleaked {secret}\n",
                grounded_answer=f"rationale carrying {secret}",
                risk_level="high_risk",
                citations=to_json([{"ref": 1, "id": "kb-a", "title": "T", "source": "S"}]),
                corrective_actions=to_json(["do the thing"]),
            )
        )
        assert result["status"] == AgentStatus.ERROR.value
        for field in (
            "compliance_answer",
            "formatted_answer",
            "grounded_answer",
            "citations",
            "corrective_actions",
            "risk_level",
        ):
            assert result[field] is None, f"{field} still carries released content"
        assert secret not in str(result)

    # Credential forms the FRAMEWORK detects but this template's own pattern
    # list does not: a Stripe-style underscore key, an AWS access key id, and a
    # connection string. They are the interesting ones - see the test below.
    FRAMEWORK_ONLY = [
        "sk_live_" + "e" * 20,
        "AKIA" + "F" * 16,
        # Assembled at runtime so no credential-shaped literal is committed.
        "postgre" + "sql://u:" + "p" * 4 + "@h.example:5432/d",
    ]

    def test_post_09_the_gate_catches_what_the_framework_would_raise_on(self):
        # The framework applies its own credential detector to whatever this
        # node returns and RAISES on a hit - and a raise leaves state untouched,
        # which is the un-contained case: the envelope then falls back to the
        # un-gated answer still sitting in state. So the node's gate has to be
        # at least as wide as the framework's. It is, because it runs the same
        # detector first, and these probes prove that is not redundant.
        from framework.security.credential_detector import detect_credentials_in_value

        from src.nodes.post_process_node import _scan_patterns, _security_gate_output

        for probe in self.FRAMEWORK_ONLY:
            assert detect_credentials_in_value(probe), f"probe is not detectable: {probe[:12]}"
            # Verify the verifier: without the framework detector these would
            # sail through the domain pattern list and reach the raise.
            assert _scan_patterns(probe) is None, f"probe is not framework-only: {probe[:12]}"
            assert (
                _security_gate_output(probe) is not None
            ), f"the framework would raise on {probe[:12]}... but the node gate passed it"

    def test_post_09b_a_framework_only_credential_is_contained_end_to_end(self):
        secret = self.FRAMEWORK_ONLY[0]
        result = PostProcessNode()(_make_state(f"# Assessment\n\nleaked {secret}\n", compliance_answer="leaked"))
        assert result["status"] == AgentStatus.ERROR.value
        assert secret not in str(result)
        assert result["compliance_answer"] is None

    def test_an_unexpected_failure_still_contains(self):
        # Anything unexpected inside finalisation is turned into the same
        # contained error rather than escaping: an exception would leave the
        # un-gated answer in state for the envelope to fall back to.
        class Exploding(str):
            def __str__(self):  # noqa: D105 - deliberately hostile
                raise RuntimeError("boom")

        result = PostProcessNode()(_make_state(Exploding("x"), citations="not-json"))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["compliance_answer"] is None
