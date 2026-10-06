# RET-C2-667 - Unit Tests: PreProcessNode (the caller-contract boundary)
#
# Invocation contract: every test invokes the node via node(state) -
# BaseNode.__call__ runs the trust gate, then the input gate, then execute(),
# then the output gate - never a bare node.execute(state). PreProcessNode
# requires VERIFIED_EXTERNAL, so its behavioural tests build the state at that
# level (the ANONYMOUS rejection lives in test_trust_gate.py).
#
# Layering note: the framework's own input gate masks user_input /
# validated_input to [MASKED] before execute() runs - e-mails, identifier digit
# groups, and Title-Case name bigrams. Positive-path payloads below are
# therefore lowercase, identifier-free claim text; the deliberate-PII tests
# assert the raw identifier is gone and [MASKED] is present.
#
# What this node adds on top of that gate is the caller contract itself, and
# the PII mask over the CONTEXT channel, which the framework gate does not
# cover. Those are the tests that matter here.
#
# Mirrors docs/03_test_spec.md Sec 2.1 (PRE-01..PRE-10).
# Deterministic - no model call, no network. framework.* / src.* imports only.

from unittest.mock import MagicMock

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

import src.nodes.pre_process_node
from src.nodes.pre_process_node import PreProcessNode


def _assert_declined(result):
    """A rejection the caller can correct: the run COMPLETES carrying the reason.

    Both halves matter. The status says the calling surface's turn was not
    ended, and the reason code says the request was nonetheless not carried out
    — asserting only the status would pass on a run that quietly answered.

    The refusals that still terminate keep asserting AgentStatus.ERROR; the two
    are deliberately not merged into one predicate.
    """
    assert result["status"] == AgentStatus.SUCCESS.value, result
    assert result.get("error_code"), result



# Lowercase, PII-free claim text on purpose (no Title-Case bigram, no @, no
# digit run) so the framework's PII mask leaves the payload untouched.
_VALID_CLAIM = (
    "we are advertising a limited-time discount against a rival's listed "
    "price and want to confirm the comparison claim is compliant"
)


def _make_state(user_input=_VALID_CLAIM, **extra) -> dict:
    state = {
        "user_input": user_input,
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestPreProcessSuccess:
    def test_pre_01_valid_claim_accepted(self):
        result = PreProcessNode()(_make_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the plain status string, never the enum.
        assert type(result["status"]) is str  # noqa: E721 - AgentStatus subclasses str, so isinstance() would pass for
        # the enum too and this guard exists precisely to catch the enum.
        assert result["validated_input"] == _VALID_CLAIM

    def test_enriched_context_carries_channel_and_source(self):
        result = PreProcessNode()(_make_state(input_context={"channel": "web"}))
        assert result["enriched_context"]["channel"] == "web"
        assert result["enriched_context"]["source"] == "ECPricingComplianceValidationAgent"

    def test_missing_channel_defaults_to_unknown(self):
        result = PreProcessNode()(_make_state())
        assert result["enriched_context"]["channel"] == "unknown"


class TestPreProcessRejection:
    def test_pre_02_empty_input_is_error(self):
        result = PreProcessNode()(_make_state(user_input=""))
        _assert_declined(result)
        assert result["error_log"]
        assert any("empty" in str(e) for e in result["error_log"])
        # No validated_input is produced on the reject path.
        assert "validated_input" not in result

    def test_whitespace_only_is_error(self):
        result = PreProcessNode()(_make_state(user_input="   \n\t "))
        _assert_declined(result)
        assert any("empty" in str(e) for e in result["error_log"])

    def test_pre_03_missing_user_input_is_error(self):
        state = _make_state()
        del state["user_input"]
        result = PreProcessNode()(state)
        _assert_declined(result)

    def test_non_string_input_is_error(self):
        # A dict payload has no .strip() — execute() raises, __call__'s outer
        # except converts it to a status=ERROR result (never propagates raw).
        result = PreProcessNode()(_make_state(user_input={"malicious": "dict"}))
        _assert_declined(result)
        assert result["error_log"]


class TestPreProcessPIIScreen:
    """PRE-04: the framework input gate masks user_input before execute() runs."""

    def test_title_case_bigram_masked(self):
        # "Premiums Act" is a real Title-Case bigram from this template's own
        # domain vocabulary (docs/02_design.md) and matches the framework's
        # `name` PII pattern (two consecutive Title-Case words).
        raw = "under the Premiums Act guidelines, is a 30% discount claim compliant?"
        result = PreProcessNode()(_make_state(user_input=raw))
        vi = result["validated_input"]
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Premiums Act" not in vi
        assert "[MASKED]" in vi

    def test_email_masked_by_framework_s2_gate(self):
        raw = "escalate to compliance.desk@example.com about this claim today"
        result = PreProcessNode()(_make_state(user_input=raw))
        vi = result["validated_input"]
        assert "compliance.desk@example.com" not in vi
        assert "[MASKED]" in vi

    def test_grouped_digits_masked(self):
        # A 4-4-4 digit run (My Number / credit-card-shaped) is masked by the
        # framework's default PII patterns.
        raw = "reference number 1234 5678 9012 needs a compliance check"
        result = PreProcessNode()(_make_state(user_input=raw))
        vi = result["validated_input"]
        assert "1234 5678 9012" not in vi
        assert "[MASKED]" in vi


class TestPreProcessAudit:
    def test_pre_08_domain_audit_payload(self, monkeypatch):
        """Audit: the accepted request emits pricing_query_accepted; the
        assertion targets call.args[1] — the event payload — never the whole
        call repr."""
        spy = MagicMock()
        monkeypatch.setattr(src.nodes.pre_process_node, "emit_trace_event", spy)
        PreProcessNode()(_make_state())
        events = [call.args[0] for call in spy.call_args_list]
        assert "pricing_query_accepted" in events
        payload = spy.call_args_list[events.index("pricing_query_accepted")].args[1]
        assert payload["input_chars"] == len(_VALID_CLAIM)

    def test_rejection_emits_pricing_query_rejected(self, monkeypatch):
        spy = MagicMock()
        monkeypatch.setattr(src.nodes.pre_process_node, "emit_trace_event", spy)
        PreProcessNode()(_make_state(user_input=""))
        events = [call.args[0] for call in spy.call_args_list]
        assert "pricing_query_rejected" in events
        payload = spy.call_args_list[events.index("pricing_query_rejected")].args[1]
        assert payload["reason"] == "empty_input"
