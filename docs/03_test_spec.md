# Test Specification — RET-C2-667

**Template ID:** RET-C2-667
**Template Name:** ECPricingComplianceValidationAgent
**Category:** Cat 2 (nested retrieval pipeline)

This document is the test contract: what each shipped test file proves, and
why. It describes the tests that ship in this repository —
`tests/unit/`, `tests/proof_of_boundary/` and `tests/integration/`.

## 1. Scope & invocation conventions

- The caller-data contract (`src/services/caller_contract.py`) — numbers,
  labels, and the injection screen, probed in BOTH directions.
- Per-node unit tests for the 5 inner domain nodes + the 2 outer gate nodes.
- Manifest/runtime-config consistency (`config/agent.yaml` and
  `config/config.yaml` against the code that reads them) and corpus integrity.
- Retrieval quality (golden queries over `config/kb/pricing_compliance_kb.json`).
- Inner-graph (`DomainWorkflowGraph`) and outer-graph
  (`ECPricingComplianceValidationAgent`) composition / integration.
- Boundary tests: import isolation, State msgpack safety, invoke order (PB-6),
  interrupt propagation (PB-7, conditional), server boot.
- End-to-end tests through the real ASGI `/invoke` entry with bearer auth.

**Invocation contract.** Every per-node test invokes the node via `node(state)`
— through `BaseNode.__call__`, which runs the trust gate, then the input gate,
then `execute()`, then the output gate — never a bare `node.execute(state)`.
The state builder sets `caller_trust_level` to
`TrustLevel.VERIFIED_EXTERNAL.value` for the two outer gate slots
(PreProcessNode / PostProcessNode — the manifest's declared caller level) and
`TrustLevel.ANONYMOUS.value` for the five inner domain nodes.

Nodes take no `config` parameter — `execute(self, state)` is the only
signature. Config knobs (`retrieval.top_k` / `score_threshold` / `kb_path`) are
exercised by seeding `state["retrieval_config"]`, the JSON string
`DomainWorkflowGraph._extra_initial_state()` republishes at runtime.

**The one exception, deliberately.** The caller-contract rules are also tested
at the module level (`test_caller_contract.py`), with no node and no framework
wrapper in front. That is the point: a refusal that only happens when the
framework's input gate is active is not a guarantee the template owns.
Assertions stay behavioural — the rule raises, the run carries the reason,
nothing is carried forward — never a gate's wording.

**Two rejection outcomes, asserted separately.** A rejection the caller can
correct ends the run by COMPLETING: `status=SUCCESS` plus a reason code in
`error_code`, so the calling surface's turn is not ended and the request can be
corrected and resent. A refusal the caller cannot correct still TERMINATES with
`status=ERROR`. The node-level suites carry a `_assert_declined()` helper for
the first kind — it asserts BOTH halves, because asserting the status alone
would also pass on a run that quietly answered — while the terminating refusals
keep asserting `AgentStatus.ERROR` directly. The two predicates are deliberately
not merged. Which outcome a row expects is stated per row below.

**PII masking expectations.** The framework input gate masks
`user_input`/`validated_input`/`llm_response` (e-mail, phone and other
identifier digit groups, Title-Case name bigrams) to `[MASKED]` before
`execute()` runs. Positive-path payloads are therefore lowercase,
identifier-free claim phrasing; deliberate-PII tests assert the raw identifier
is gone and `[MASKED]` is present. `PreProcessNode` applies the same mask to
free text on the CONTEXT channel, which the framework gate does not cover.
Domain fields (`grounded_answer`, `formatted_answer`, `retrieved_rules`,
`citations`, …) are not input-gate scan targets.

**Audit muting.** `shared.*` is never sys.modules-stubbed (the framework
imports `shared.security` at load time). The domain audit emitter is muted via
an autouse fixture patching `src.nodes.<mod>.emit_trace_event`; the audit
assertion tests re-patch the same attribute with a spy and assert on
`call.args[1]` (the event payload).

## 2. Unit Test Cases

### 2.1 PreProcessNode (the caller-contract boundary) — `test_pre_process_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| PRE-01 | Valid claim | lowercase, identifier-free claim text | `status=SUCCESS`, `validated_input` + `claim_context` set, `enriched_context` carries channel/source |
| PRE-02 | Empty input | `""` / whitespace | DECLINED — `status=SUCCESS` carrying `error_code`, `error_log` names "empty", no `validated_input`. `test_trust_gate.py` pins the code itself to `EMPTY_INPUT` |
| PRE-03 | Missing / non-string input | `user_input` absent; dict payload | DECLINED — `status=SUCCESS` carrying `error_code`, `error_log` non-empty |
| PRE-04 | PII screen (framework gate) | Title-Case bigram / e-mail / digit group | raw identifier absent from `validated_input`; `[MASKED]` present |
| PRE-08 | Audit | valid claim / empty claim | `pricing_query_accepted` (`input_chars`) / `pricing_query_rejected` (`reason=empty_input`) |
| PRE-09 | Caller-contract refusal | out-of-range / non-finite / hostile field on either channel | asserted where the rule lives and where the caller sees it rather than a third time at this node: the rule in `test_caller_contract.py` (§2.10), the node-level outcome in `test_input_validate_node.py` (§2.2 — DECLINED, field named, value never echoed), and through `/invoke` in E2E-07 / E2E-10 (a correctable value completes) and E2E-09 (injection content terminates) |
| PRE-10 | Context-channel PII | free text carrying an identifier in `input_context` | masked in `claim_context` — the framework gate does not cover this channel |

### 2.2 InputValidateNode (inner node 1) — `test_input_validate_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| VAL-01 | Plain text | free-text claim | whole string becomes `compliance_query`; filters all `None` |
| VAL-02 | Whitespace | ragged spacing/newlines | collapsed to single spaces |
| VAL-03 | JSON envelope | `{claim, category, top_k, discount_pct, product, competitor}` | all parsed; `query` alias accepted; `product`/`competitor` appended to the query; `category` lower-cased/stripped |
| VAL-04 | Malformed JSON | `{`-prefixed non-JSON | treated as plain-text claim (callers do send prose containing braces) |
| VAL-05 | `top_k` out of range | 99 | DECLINED — `status=SUCCESS` carrying `error_code`, `top_k` named in `error_log` |
| VAL-06 | `top_k` non-numeric | `"many"` | DECLINED — `status=SUCCESS` carrying `error_code` |
| VAL-07 | `discount_pct` guard | `-5` / `500` / `NaN` | DECLINED, never clamped — a clamp turns a nonsense input into a plausible answer the caller never sees. Declining rather than terminating changes only how the refusal is reported: no query is composed and no filters are derived either way |
| VAL-08 | Oversize query | > 2000 chars | capped at 2000 |
| VAL-09 | Empty request | `""` | `compliance_query=""` + "empty request" note (non-fatal) |
| VAL-10 | Bridged context preferred | `claim_context` present | composed from the accepted context, not re-parsed from the request string |
| — | No bridged context | `claim_context` absent, `NaN` `discount_pct` | the same contract runs here — a node that is only safe when its caller behaved is not safe — and reaches the same DECLINED outcome |
| — | Rejection hygiene | any refused value | DECLINED, and the value never appears in `error_log` |
| — | JSON-string state contract | any | `claim_filters` is a JSON string, never a bare dict |

### 2.3 RetrieveNode (inner node 2) — `test_retrieve_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| RET-01 | Happy path | discount-comparison claim | top-1 candidate is `keihyo-nijuu-kakaku-01` (dual price display) |
| RET-02 | Ordering | discount-comparison claim | scores strictly sorted desc; all > 0 |
| RET-03 | Entry shape | any hit | keys `{id,title,category,source,score,excerpt}`; excerpt <= 400 chars |
| RET-04 | Category filter | `claim_filters.category="comparative_advertising"` | only that category; top-1 `keihyo-hikaku-koukoku-01` |
| RET-05 | Empty query | `""` | no candidates |
| RET-06 | State `kb_path` override (unreadable) | `retrieval_config.kb_path` bogus | `[]` + "not readable" note |
| RET-07 | State `retrieval_config` drives `top_k` | seeded via state | node reads it (no execute(state, config) carve-out — C2 retired) |
| RET-08 | Notes accumulation | prior `intake_notes` | appended, never clobbered |

### 2.4 RerankFilterNode (inner node 3) — `test_rerank_filter_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| RRF-01 | Relevance floor | scores 0.9 / 0.1 | 0.1 dropped (default 0.25 floor) |
| RRF-02 | State score_threshold | `retrieval_config.score_threshold=0.5` | 0.3 dropped |
| RRF-03 | State top_k | `retrieval_config.top_k=1` | one survivor, highest score |
| RRF-04 | Category boost | matching category | +0.1, re-ranked ahead |
| RRF-05 | Boost cap | 0.95 + boost | capped at 1.0 |
| RRF-06 | Caller top_k | stricter (1) wins; looser (10) does not widen past the state top_k | enforced |
| RRF-07 | Garbage entries | non-dict / uncoercible score | skipped / coerced to 0.0 and dropped |
| RRF-08 | Tie-break | equal scores | deterministic id-ascending order |

### 2.5 GenerateAnswerNode (inner node 4) — `test_generate_answer_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| GEN-01 | Citation markers | 2 ranked provisions | `[1]`/`[2]` markers with titles |
| GEN-02 | Lead sentence | query present | claim text quoted in the lead — already rendered inert by the caller contract, so it cannot introduce structure |
| GEN-03 | Citations list | ranked provisions | refs 1..n mirror ranked order; id/title/source carried |
| GEN-04 | Groundedness | single provision | answer body traces to the ranked provision only |
| GEN-05 | No coverage | empty/missing `ranked_rules` | escalation answer; `citations=[]`; `risk_level=insufficient_data` |
| GEN-06 | Risk ladder | strong match (score >= 0.3) x `discount_pct` | `>= 20%` -> `high_risk`; `< 20%` / absent -> `medium_risk`; weak match -> `low_risk` regardless of discount |
| — | Corrective actions | keyed by `risk_level` | a new list per call, never a shared module-list reference a caller could mutate; `insufficient_data` escalates to the compliance team |

### 2.6 OutputFormatNode (inner node 5, terminal) — `test_output_format_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| FMT-01 | Full compose | risk level + body + citations + actions | header + risk-level line + body + `## Cited Provisions` + `## Corrective Actions` rows + advisory disclaimer; `status=SUCCESS` |
| FMT-02 | Blank source | citation without source | no `()` suffix |
| FMT-03 | Disclaimer | every input | 景品表示法 advisory disclaimer rides with every answer, including empty body |
| FMT-04 | No citations | empty list | explicit "- none (no knowledge-base provision cleared the relevance threshold)" line |
| FMT-05 | No corrective actions | empty list | explicit "- none" line |
| FMT-06 | Missing body | no `grounded_answer` | fallback text; `status=SUCCESS` |

### 2.7 PostProcessNode (the output boundary, RECURSIVE + CONTAINING) — `test_post_process_node.py`

| ID | Case | Input | Expected |
|----|------|-------|----------|
| POST-01 | Clean output | normal compliance answer | `formatted_output=result`, `status=SUCCESS` |
| POST-02 | Empty result | `""` | forwarded as-is, `status=SUCCESS` (non-fatal) |
| — | Scalar summary | clean `citations`/`corrective_actions` | `rule_citation_count` / `corrective_action_count` / `primary_rule_reference` derived on the clean path only |
| POST-03..06 | Credential leak (flat `result` string) | `sk-` API key / `password=` assignment / JWT (built at runtime) / bearer token | `formatted_output` + `result` replaced with the sanitised stub, `status=ERROR`, raw secret absent from both |
| **POST-07** | **Credential leak NESTED inside `citations`/`corrective_actions`** | a `citations[i]["source"]` or `corrective_actions[i]` string carries a credential-shaped value while the flat `result` text is clean | **BLOCKED** — a top-level-string-only scan would miss this entirely |
| — | Negative control | clean nested `citations`/`corrective_actions` | reaches SUCCESS — without this control, a gate that blocked everything nested would pass POST-07 too |
| **POST-08** | **Containment** | a violation with released text in `compliance_answer` / `formatted_answer` / `grounded_answer` / `citations` / `corrective_actions` / `risk_level` | every one of those fields is **cleared**. Refusing is not containing: `AgentBaseGraph.get_output()` falls back to `state["result"]` even on an error run, so a gate that merely raises still ships the un-gated answer inside the error envelope |
| **POST-09** | **The gate is at least as wide as the framework's** | credential forms the platform detector matches but the domain patterns do not (`sk_live_…`, `AKIA…`, `postgresql://…`) | caught HERE, where they can be contained — the framework would otherwise RAISE on them after `execute()` returned, leaving state un-gated. The test verifies its own probes are genuinely framework-only |
| — | Unexpected failure | a value that explodes during finalisation | still a contained ERROR with the output-bearing fields cleared — an escaping exception leaves state untouched, which is the uncontained case |

### 2.8 Manifest / runtime-config consistency — `test_config_manifest.py`

Configuration is split: `config/agent.yaml` is the static registry entry,
`config/config.yaml` carries the runtime parameters. A reader left pointing at
the wrong file does not fail — it returns nothing and every declared value
quietly becomes a module default, which is why these are pinned.

| ID | Case | Expected |
|----|------|----------|
| CFG-01 | Flat manifest | every registry key at ROOT level; no nested `agent:` block; `id = RET-C2-667`; `enabled: true` |
| CFG-02 | Class-name contract | manifest `class` = `src.graph.graph.ECPricingComplianceValidationAgent`; `name` = the agent's `name` property |
| CFG-03 | Classification | Cat 2 / RET / namespace `ret` / RAGAgent |
| CFG-04 | Trust level | manifest `VERIFIED_EXTERNAL` == PreProcessNode & PostProcessNode `required_trust_level` |
| CFG-05 | `max_retry` | from `config/config.yaml`; int, `0 <= v < 10` (framework ceiling) |
| CFG-05b | Timeout key | `timeout_s` present, `timeout_seconds` absent — the retired spelling nothing reads |
| CFG-06 | Retrieval block | `top_k`/`score_threshold` mirror node module defaults; `kb_path` exists |
| CFG-07 | `_parent_config()` | forwards the runtime retrieval + llm blocks; never `{}` |
| CFG-08 | Declared requirements follow the code | `generation_mode: deterministic`; `requires.secrets` and `requires.extras` both `[]` — declaring either would make the agent fail to compile where it is not provisioned |
| — | Runtime loader | `load_runtime_config()` reads the live file (`max_retry` / `timeout_s` match) |
| — | hitl waiver | `config/config.yaml` does not enable hitl |
| — | Corpus integrity | JSON list of 10 entries; unique ids; required keys per entry; no `url`/`api_endpoint` field (no live lookup) |
| — | Category alphabet | every corpus category matches the inert alphabet the caller contract accepts, or a caller could never filter by it |

### 2.10 The caller-data contract — `test_caller_contract.py`

The module every caller-supplied value passes through, so it carries the
security tests that matter most.

**REFUSED in this table means the module RAISES** — these tests exercise the
rule, not a run. The module reports the two kinds of refusal by exception type:
`ScreeningRefused` for injection content and `CallerDataError` for a value the
caller can correct. `ScreeningRefused` subclasses `CallerDataError`, so a
`pytest.raises(CallerDataError)` here covers both, and a handler that does not
know about the subtype keeps the stricter behaviour rather than letting a
refusal escape. Which of the two run outcomes in §1 the caller then sees is
decided by the calling node from that type, and is asserted there (§2.2) and end
to end (§5) — not here.

| ID | Case | Expected |
|----|------|----------|
| CC-01 | Non-finite matrix, per field | `"NaN"` / `"Infinity"` / `"-Infinity"` / raw `float("nan")` / `float("inf")` / `float("-inf")` REFUSED for every numeric field, error names the field |
| CC-02 | Booleans are not numbers | `True` refused — `isinstance(True, int)` is True in Python, so it would otherwise arrive as `1.0` |
| CC-03 | Out of range | refused, never clamped |
| CC-04 | In range | accepted, including numeric strings |
| CC-05 | Whole numbers | `top_k` rejects `2.5` |
| CC-06 | Error hygiene | the message names the field and never the value |
| CC-07 | Real product codes | `EarbudsPro`, `sku_48210`, `SKF-6205` accepted |
| CC-08 | Labels that could carry structure | whitespace, markup, brackets, over-length REFUSED |
| CC-09 | Category alphabet | `[a-z0-9_]{1,32}`, case-folded |
| CC-10 | Free-text rendering | the claim is rendered inert — no newlines, no emphasis, no forged `[1]` marker, length capped |
| CC-11 | Attack matrix | control tokens (`<\|…\|>`, `[INST]`, `<<SYS>>`, role tags, `### System`), directive phrases, URL-encoded, fullwidth, zero-width-spliced and tag-spliced forms all refused |
| — | Both screening layers earn their place | the token form is invisible to a strip-only scan and the tag-spliced form is invisible to a raw scan — each is asserted explicitly |
| CC-12 | Escape evasion | a `\u`-escaped control token is refused after parsing |
| — | Hostile field NAME | refused, and the error reports a POSITION, not the caller's field name |
| — | Nested walk + control | a directive nested two levels deep is refused; the same shape with clean leaves passes |
| — | Fail-CLOSED direction | no text in this template's own corpus, and none of 8 real compliance questions, trips the screen |
| — | Composition | the shipped `deploy/invoke_payload.json` is accepted; the context channel overrides the string envelope; one hostile field refuses the whole payload; structural caps enforced |

### 2.9 Retrieval quality (golden queries) — `test_retrieval_quality.py`

| ID | Case | Expected |
|----|------|----------|
| QUAL-01 | 9 golden domain queries | expected KB entry is top-1 (covers 9 of 10 seeded entries across 7 of 8 categories) |
| QUAL-02 | Relevance floor | every survivor after rerank >= 0.25 |
| QUAL-03 | Citation integrity | every retrieved id exists in the seeded KB |
| QUAL-04 | Category filter precision | `comparative_advertising` filter -> only that category, top-1 `keihyo-hikaku-koukoku-01` |
| QUAL-05 | No coverage | out-of-domain query -> zero survivors |

## 3. Integration / Composition

### 3.1 Inner graph — `test_domain_workflow_graph.py`

| ID | Case | Expected |
|----|------|----------|
| INT-01 | Composition | inherits `BaseGraph`; registers exactly the 5 domain nodes; no initialize/finalize |
| INT-02 | Initial-state seeding | `_extra_initial_state()` republishes the retrieval block as the JSON-string `retrieval_config`, AND seeds the bridged `claim_context` + `input_context` — the two things that would otherwise not cross the graph boundary |
| INT-03 | Output shape | `get_output()` emits `formatted_answer`/`citations`/`risk_level`/`corrective_actions`/`status`/… (the merge contract); `route()` -> END on error |
| INT-04 | Inner e2e | full inner `invoke()` -> SUCCESS; formatted answer + disclaimer + `keihyo-nijuu-kakaku-01` citation; inner `node_history` = the 5 domain nodes in linear order; no-coverage query still terminates SUCCESS |

### 3.2 Outer graph + e2e — `test_graph_composition.py`

| ID | Case | Expected |
|----|------|----------|
| INT-05 | Outer composition | inherits `AgentBaseGraph` directly; `Graph` alias; `add_edges()` NOT overridden |
| INT-06 | Backbone slots | compile() fills all 5; pre/main/post are PreProcessNode / PricingComplianceGraphNode / PostProcessNode |
| INT-07 | `get_subgraph()` | returns `DomainWorkflowGraph` carrying the forwarded retrieval config |
| INT-08 | `extract_input()` | prefers `validated_input`, falls back to `user_input`, and stashes the accepted claim context for the inner graph |
| INT-09 | `merge_output()` | inner `formatted_answer` -> outer `compliance_answer` AND `result`; `citations`/`corrective_actions`/`risk_level`/`status` mapped; `error_code` carried across the boundary — on an answered run neither side set one, so it crosses empty; changed keys only |
| INT-10 | Runtime-file fallback | `_parent_config()` never `{}` even with an unreadable `config/config.yaml` |
| INT-11 | e2e happy path | VERIFIED_EXTERNAL invoke -> SUCCESS; `output` = gated formatted answer; structured `compliance_risk_level`/`rule_citation_count`/`corrective_action_count` present; PostProcessNode traversed |
| INT-12 | e2e trust denial | ANONYMOUS invoke -> ERROR; empty `output`; no structured field added; PostProcessNode NOT traversed |
| — | JSON-string helpers | `to_json`/`from_json` round-trip; None/malformed handling |

## 4. Proof-of-Boundary

| ID | Case | Expected |
|----|------|----------|
| PB-IMPORT | `test_import_isolation.py` | no direct platform-SDK import anywhere under `src/` |
| PB-STATE | `test_state_safety.py` | `State` has no credential-named fields and no `BaseModel` / `InvocationContext` annotations |
| PB-6 | `test_pb_invoke_order.py` | full `Graph().invoke()` with `InvocationContext(caller_trust_level=VERIFIED_EXTERNAL)` (never `for_internal()`) over the payload byte-equal to `deploy/invoke_payload.json`'s `input` -> SUCCESS with outer `node_history` exactly `[InitializeNode, PreProcessNode, PricingComplianceGraphNode, PostProcessNode, FinalizeNode]`; structured compliance-risk summary present |
| PB-7 | `test_pb7_hitl_interrupt_propagation.py` | **Auto-waived — non-HITL** (`config/config.yaml` has no `hitl.enabled: true`); the conditional skip-stub is retained, with its config reader pointed at the runtime file where the flag actually lives |
| PB-BOOT | `test_server_boot.py` | `import src.api.server` does not raise; module-level agent is this template's class, compiled; fresh ctor -> `compile()` fills the 5 backbone slots; `/health` reports the agent |

> **Mandatory boundary tests:** PB-IMPORT, PB-STATE, PB-6 and PB-BOOT. PB-7
> applies only to templates with a human-in-the-loop step — this template has
> none, so PB-7 is **auto-waived** and its skip must not block the gate.

## 5. End-to-end through the real ASGI entry — `tests/integration/test_e2e_invoke.py`

Driven over the real FastAPI app's ASGI interface with bearer auth, through the
real compiled agent and the full nested pipeline. No socket bind, no network.

| ID | Case | Expected |
|----|------|----------|
| — | Auth boundary | a missing or wrong bearer token gets 401 |
| E2E-01 | Real work on the public path | a real claim returns a long, cited, disclaimed assessment with a non-zero citation count |
| E2E-02 | The baseline is distinguishable | an out-of-domain query returns `insufficient_data` with zero citations — proving the happy path is not a stub that answers everything the same way |
| **E2E-03** | **The caller-context bridge** | `discount_pct` sent on `input_context` moves the verdict from `medium_risk` to `high_risk`. The framework's `GraphNode` does not forward `input_context`, so without the bridge this field never reaches the inner graph — and node-level tests would not notice |
| E2E-04 | Category filter over the bridge | narrows retrieval versus the unfiltered run |
| E2E-05 | `top_k` over the bridge | caps the citation count |
| E2E-06 | Every risk path | `insufficient_data` / `low_risk` / `medium_risk` / `high_risk` each reachable end to end |
| E2E-07 | Out-of-contract fields | DECLINED through `/invoke` — `status=success` and `output` is one of the four fixed sentences, so the caller can correct the field and resend on the same conversation; and the run still releases nothing: no assessment, no `compliance_risk_level`, no `primary_rule_reference` |
| E2E-08 | Raw non-finite literals | bare `NaN` / `Infinity` / `-Infinity` in the request body are refused, not absorbed — DECLINED, `output` is a fixed sentence, no structured product |
| E2E-09 | Injection on BOTH channels | control tokens and directive phrases TERMINATE (`status=error`) whether sent as the request string or on the context channel — refused content is not a value to correct, and reporting it as one would read as an invitation to reword the claim until it gets through |
| E2E-10 | Rejection hygiene | the refused value never appears anywhere in the response — asserted over the whole serialised envelope of a DECLINED run, so the fixed sentence is verified to carry no trace of what was rejected |
| — | Fail-CLOSED control | an ordinary question containing the same words ("consumers ignore the reference price") still succeeds |
| E2E-11 | Declared config reaches the inner graph | the citation count equals the `top_k` declared in `config/config.yaml`, over a query that matches more provisions than that |
| **E2E-12** | **Containment** | a blocked assessment returns an error envelope carrying no released text, no secret, no traceback, no source paths, and no structured product |
| E2E-13 | Released output is clean | the successful assessment carries no credential material |

## 6. Simulator scope

This template performs **no web crawling and no competitor-price lookup** — the
caller supplies the price-comparison data in the request payload, and retrieval
is exclusively over the seeded, self-contained
`config/kb/pricing_compliance_kb.json`. There is no external HTTP client and no
transport-injection seam: every test in this suite runs fully offline against
the local corpus and in-process nodes.

## 7. Test execution summary

- Runner: the real framework wheel, `python -m pytest tests/`
- Total tests: 267 passed, 2 skipped (the interrupt-propagation stubs — auto-waived)
- Failures: 0
- Determinism: no model call, no network; retrieval and risk assembly are rule-based
