# RET-C2-667 - Unit Tests: InputValidateNode (inner domain node 1)
#
# Invocation canon: node(state) via BaseNode.__call__ with an ANONYMOUS caller -
# execute(self, state) is the only signature.
#
# The caller contract itself is exercised in test_caller_contract.py; this file
# pins how THIS node applies it: the accepted context is used when the bridge
# delivered one, and the same rules are re-run from scratch when it did not.
#
# Mirrors docs/03_test_spec.md Sec 2.2 (VAL-01..VAL-10).
# Deterministic - no model call, no network. framework.* / src.* imports only.

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.input_validate_node import InputValidateNode
from src.schemas.state import from_json, to_json


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




def _make_state(validated_input="", **extra) -> dict:
    state = {
        "validated_input": validated_input,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestPlainTextAndWhitespace:
    def test_val_01_plain_text_becomes_the_whole_query(self):
        raw = "we compare against a rival's price openly"
        result = InputValidateNode()(_make_state(raw))
        assert result["compliance_query"] == raw
        filters = from_json(result["claim_filters"])
        assert filters == {"category": None, "top_k": None, "discount_pct": None}

    def test_val_02_ragged_whitespace_collapsed(self):
        result = InputValidateNode()(_make_state("is   this\n\tclaim   compliant?"))
        assert result["compliance_query"] == "is this claim compliant?"


class TestJsonEnvelope:
    def test_val_03_json_envelope_fields_all_parsed(self):
        envelope = to_json(
            {
                "claim": "is this discount compliant",
                "category": "Discount_Pricing",
                "top_k": 3,
                "discount_pct": 25,
                "product": "Widget",
                "competitor": "Acme",
            }
        )
        result = InputValidateNode()(_make_state(envelope))
        assert result["compliance_query"] == ("is this discount compliant (product: Widget) (competitor: Acme)")
        filters = from_json(result["claim_filters"])
        assert filters == {"category": "discount_pricing", "top_k": 3, "discount_pct": 25.0}

    def test_query_alias_accepted(self):
        envelope = to_json({"query": "alias claim text"})
        result = InputValidateNode()(_make_state(envelope))
        assert result["compliance_query"] == "alias claim text"

    def test_val_04_malformed_json_treated_as_plain_text(self):
        # Callers do send prose containing braces; that is not an attack and
        # must not be refused.
        raw = "{not valid json at all"
        result = InputValidateNode()(_make_state(raw))
        assert result["compliance_query"] == "not valid json at all"


class TestNumericGuardsFailClosed:
    # Out-of-contract numbers are REFUSED, not clamped. A clamp turns a nonsense
    # input into a plausible answer, and the caller never learns the number the
    # assessment actually used.

    def test_val_05_top_k_out_of_range_is_refused(self):
        result = InputValidateNode()(_make_state(to_json({"claim": "c", "top_k": 99})))
        _assert_declined(result)
        assert any("top_k" in str(e) for e in result["error_log"])

    def test_val_06_top_k_non_numeric_is_refused(self):
        result = InputValidateNode()(_make_state(to_json({"claim": "c", "top_k": "many"})))
        _assert_declined(result)

    def test_discount_pct_negative_is_refused(self):
        result = InputValidateNode()(_make_state(to_json({"claim": "c", "discount_pct": -5})))
        _assert_declined(result)

    def test_discount_pct_over_100_is_refused(self):
        result = InputValidateNode()(_make_state(to_json({"claim": "c", "discount_pct": 500})))
        _assert_declined(result)

    def test_rejection_never_echoes_the_rejected_value(self):
        secret_ish = 987654321
        result = InputValidateNode()(_make_state(to_json({"claim": "c", "discount_pct": secret_ish})))
        _assert_declined(result)
        assert all(str(secret_ish) not in str(e) for e in result["error_log"])


class TestSizeAndEmptiness:
    def test_val_08_oversize_query_is_capped(self):
        result = InputValidateNode()(_make_state("a" * 2500))
        assert len(result["compliance_query"]) == 2000

    def test_val_09_empty_request_is_non_fatal(self):
        result = InputValidateNode()(_make_state(""))
        assert result["compliance_query"] == ""
        notes = from_json(result["intake_notes"])
        assert any("empty request" in n for n in notes)


class TestBridgedContextIsPreferred:
    def test_val_10_accepted_context_from_the_bridge_is_used(self):
        # When the entry boundary already validated the caller fields, this node
        # composes from that result rather than re-parsing the request string.
        state = _make_state(
            "ignored raw payload",
            claim_context=to_json({"claim": "bridged claim", "product": "Widget", "top_k": 2}),
        )
        result = InputValidateNode()(state)
        assert result["compliance_query"] == "bridged claim (product: Widget)"
        assert from_json(result["claim_filters"])["top_k"] == 2

    def test_node_is_safe_when_called_with_no_bridged_context(self):
        # A node that is only safe when its caller behaved is not safe: with no
        # accepted context present the same contract runs here.
        result = InputValidateNode()(_make_state(to_json({"claim": "c", "discount_pct": float("nan")})))
        _assert_declined(result)


class TestStructuredStateFieldsAreJsonStrings:
    def test_claim_filters_is_a_json_string_never_a_bare_dict(self):
        result = InputValidateNode()(_make_state("plain claim"))
        assert isinstance(result["claim_filters"], str)
        assert isinstance(from_json(result["claim_filters"]), dict)
