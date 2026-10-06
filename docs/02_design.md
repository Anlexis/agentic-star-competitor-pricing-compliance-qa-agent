# Template Design Specification — RET-C2-667

**Template ID:** RET-C2-667
**Template Name:** ECPricingComplianceValidationAgent
**Category:** Cat 2 (multi-step domain workflow — retrieval pattern)
**Industry:** RET

## Position in the framework architecture

| Aspect | Value |
|---|---|
| Agent class | `ECPricingComplianceValidationAgent` (alias `Graph`) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Inner graph base | `BaseGraph` — `DomainWorkflowGraph` |
| Pattern | Two-layer nested architecture (outer fixed 5-node backbone + `GraphNode` in the `main` slot wrapping an inner `BaseGraph` domain workflow) |

**Three-layer separation:**

- State: flat `TypedDict` composition (no Pydantic — msgpack incompatible);
  structured fields stored as JSON strings via `to_json()` / `from_json()`
- Node: `FunctionNode` subclasses overriding `execute(self, state) -> dict` only
  — no extra parameters
- Graph: composition (`register_nodes()` for node substitution); the outer
  `add_edges()` is NOT overridden

## Purpose

Retail-EC competitor pricing compliance validation Q&A agent. The user supplies
price-comparison data they intend to publish (e.g. "we list Product X 30% below
Competitor A"); the agent retrieves the matching 景品表示法 (Act against
Unjustifiable Premiums and Misleading Representations) provisions from a seeded
compliance knowledge base, validates the claim against them, and returns a
compliance-risk assessment with rule citations and corrective-action guidance.
There is NO live web crawling and NO competitor-price lookup — the caller
provides the price data; retrieval is KB-based only. v1 is fully deterministic
(keyword retrieval + rule-based risk assembly; no live LLM call — see the v1
Implementation Note below).

## Architecture Overview

### Outer backbone (AgentBaseGraph)

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                     ↓ (retry, max 3)
                                   pre_process
```

| Slot | Class | Responsibility | required_trust_level | Gate |
|------|-------|----------------|----------------------|--------|
| initialize | InitializeNode (framework default) | session_id, trust_level, schema_version | — (framework) | — |
| pre_process | `PreProcessNode` | Owns the CALLER CONTRACT: validates every caller field from both channels, screens for injection content, masks PII on the context channel → `validated_input` + `claim_context`. A value the caller can correct ends the run by COMPLETING with an `error_code`; injection content terminates (see "How a rejection is reported") | `TrustLevel.VERIFIED_EXTERNAL` | trust + input |
| main | `PricingComplianceGraphNode` (`GraphNode`) | delegates to inner `DomainWorkflowGraph`; maps inner `formatted_answer` → outer `result` + the vetted risk fields. Skips the inner graph entirely when a reason code is already set — a declined request has no validated input to act on | — (GraphNode delegation) | — (delegates) |
| post_process | `PostProcessNode` | Output gate — module-level `_security_gate_output()` recursively scans `result` **and** the structured citation/corrective-action payload for credential-shaped content → on a hit, ERROR **and every output-bearing field cleared**; on the clean path derives the whitelisted SCALAR summary fields (see "Security gates" below). When a reason code is set it formats that code's fixed sentence as the caller-facing body instead of gating an answer that was never produced | `TrustLevel.VERIFIED_EXTERNAL` | output |
| finalize | FinalizeNode (framework default) | response_metadata, total_time_ms | — (framework) | — |

### Inner graph (DomainWorkflowGraph — BaseGraph, linear)

```
START → input_validate → retrieve → rerank_filter → generate_answer → output_format → END
```

All five inner domain nodes declare `required_trust_level = TrustLevel.ANONYMOUS`
(the external trust gate lives on the outer backbone gate nodes; a stricter inner
level would deny a real VERIFIED_EXTERNAL invoke at runtime).

The inner topology is linear — `add_conditional_edges()` is not used. `route()`
is implemented to satisfy the abstract base contract and is annotated with this
graph's own `State`: LangGraph reads a path callable's annotation as its input
schema and projects away every field the annotation does not declare, so a
callable annotated with the framework base state would receive a state with all
the domain fields missing. Annotating it correctly now means adding a branch
later cannot silently route on absent fields.

| Node | Responsibility | required_trust_level | Input State | Output State |
|------|----------------|----------------------|-------------|---------------|
| `InputValidateNode` | Compose the retrieval query and filter set from the accepted claim context; re-run the caller contract from scratch when no accepted context reached this graph | `TrustLevel.ANONYMOUS` | `claim_context`, `validated_input` \| `user_input` | `compliance_query`, `claim_filters`, `intake_notes` |
| `RetrieveNode` | Deterministic keyword retrieval over the seeded KB (`config/kb/pricing_compliance_kb.json`): tokenise query, score title/tags/content overlap, apply category filter | `TrustLevel.ANONYMOUS` | `compliance_query`, `claim_filters`, `retrieval_config` | `retrieved_rules`, `intake_notes` |
| `RerankFilterNode` | Rerank candidates (category-match boost), drop entries below `score_threshold`, cap at `top_k` | `TrustLevel.ANONYMOUS` | `retrieved_rules`, `claim_filters`, `retrieval_config` | `ranked_rules` |
| `GenerateAnswerNode` | Rule-based grounded rationale from the ranked provisions only, with numbered citation markers; deterministic `risk_level` classification (`insufficient_data` / `low_risk` / `medium_risk` / `high_risk`) from the top match strength + the caller's `discount_pct`; deterministic `corrective_actions` list keyed off `risk_level` (v1 deterministic — LLM synthesis seam documented below) | `TrustLevel.ANONYMOUS` | `ranked_rules`, `compliance_query`, `claim_filters` | `grounded_answer`, `citations`, `risk_level`, `corrective_actions` |
| `OutputFormatNode` | Compose the final answer: risk-level header + rationale + Cited Provisions list + Corrective Actions list + the standing compliance advisory disclaimer (disclaimer is part of this node, NOT post_process) | `TrustLevel.ANONYMOUS` | `grounded_answer`, `citations`, `risk_level`, `corrective_actions` | `formatted_answer`, `status` |

### Data Flow

```
user_input
  → PreProcessNode (caller contract)              → validated_input + claim_context
  → PricingComplianceGraphNode.extract_input      → inner DomainWorkflowGraph.invoke(validated_input)
        → input_validate                          → compliance_query / claim_filters
        → retrieve                                → retrieved_rules
        → rerank_filter                           → ranked_rules
        → generate_answer                         → grounded_answer / citations / risk_level / corrective_actions
        → output_format                           → formatted_answer (+ advisory disclaimer)
     get_output() → {formatted_answer, citations, corrective_actions, risk_level, status, ...}
  → PricingComplianceGraphNode.merge_output        → result = formatted_answer, compliance_answer, citations,
                                                      corrective_actions, risk_level
  → PostProcessNode (output gate, recursive)       → formatted_output (gated) + vetted scalar summary
  → ECPricingComplianceValidationAgent.get_output() → base envelope + whitelisted structured scalars
                                                      (answered runs only)
```

A request declined on a value the caller can correct leaves this flow at the
node that declined it: `error_code` is set, every node after that point passes
it through without doing work, `PostProcessNode` renders the matching sentence
as the caller-facing body, and `get_output()` returns the base envelope with no
structured scalars. See "How a rejection is reported" below.

### The caller-data contract

Caller data arrives on two channels and both are validated by the same code
(`src/services/caller_contract.py`), called from `PreProcessNode`:

| Channel | Shape |
|---|---|
| `input_context` (framework parameter) | `{"claim", "product", "competitor", "category", "top_k", "discount_pct"}` |
| `input` (request string) | plain claim text, or the same fields as a JSON envelope |

The context channel wins field by field, so an existing string-only caller keeps
working unchanged. Rules, all fail-CLOSED — a payload with one bad field is
refused whole, and the reason names the field but never the value:

- **Numbers** (`top_k` 1–20, `discount_pct` 0–100) go through a finite+bounded
  parser. Booleans, non-numerics, and NaN / ±Infinity are refused rather than
  clamped. NaN is the one that matters: every comparison against it is False, so
  an absorbed NaN discount would silently downgrade the risk verdict this agent
  exists to produce, and JSON accepts the bare token in a request body.
- **Labels** that render into the assessment (`product`, `competitor`) are locked
  to an inert alphabet (`[A-Za-z0-9._-]`, 1–32 chars). No whitespace and no
  markup characters, so a label cannot forge a heading, a citation marker, or a
  second risk verdict inside the rendered report.
- **`category`** is a corpus partition key, locked to `[a-z0-9_]{1,32}`.
- **The claim** is free text by nature — it is the question being asked — so it
  is rendered inert rather than refused: whitespace collapsed, structural
  characters removed, length capped.
- **Injection screening** runs on the parsed payload, depth-first, keys
  included. Scanning after parsing is what makes JSON `\u` escapes useless as an
  evasion. Each string is screened in three forms — raw, normalised
  (URL-decoded, compatibility-folded, zero-width stripped), and markup-stripped
  — because stripping markup is not refusal: it turns a `<|…|>` control token
  into ordinary prose, and it re-assembles a directive split by inline tags.
  Chat-template control tokens (`<|…|>`, `[INST]`, `<<SYS>>`, role tags) are
  screened as a CLASS, not as a list of phrases.
- **PII** on the context channel is masked with the platform detector.
  The framework's own mask covers `user_input` / `validated_input` only.

The screen is also probed in the fail-CLOSED direction against this template's
own corpus and real compliance questions, because a screen that refuses genuine
work is the more damaging failure.

### How a rejection is reported

A rejection is not one outcome. The two kinds are told apart by what the caller
can do about them, and they are separated in code by exception TYPE
(`ScreeningRefused`, a subclass of `CallerDataError`) rather than by the wording
of a message — wording gets reworded, and a distinction carried only by a string
is lost silently the next time one of those strings is edited.

| Rejection | Terminal status | Marker | What reaches the caller |
|---|---|---|---|
| Empty, whitespace-only, or non-string request | `SUCCESS` | `error_code: "EMPTY_INPUT"` | a sentence naming what to send |
| A caller field that fails the contract — out of range, non-finite, boolean, non-inert label or category, over-wide or over-long payload (`CallerDataError`) | `SUCCESS` | `error_code: "INVALID_REQUEST"` | a sentence saying a value was not accepted and to check it against the documented format |
| Injection content on either channel (`ScreeningRefused`) | `ERROR` | — | the run terminates |
| Credential-shaped content at the output gate | `ERROR` | — | the sanitised stub, with every output-bearing field cleared |
| Trust-gate denial, a high-confidence injection finding in the framework's own input gate, a broken upstream invariant, or a failure during output finalisation | `ERROR` | — | the run terminates |

**Why a rejection the caller can fix COMPLETES.** Terminating ends the calling
surface's turn: the envelope carries an error status and no body, the reason is
reachable only from the audit trail, and the same conversation cannot be
continued with a corrected value — one mistyped field costs the whole exchange
and the caller is never told which field it was. Completing with a
reason code keeps the turn open: the caller reads a plain sentence saying what to
correct and sends the request again. **What is refused does not change.** No
retrieval runs, no assessment is assembled, the same `pricing_query_rejected` /
`input_validate_rejected` audit event is emitted with the same reason, and no
structured product is released. Only the reporting channel moved.

**Why the other refusals still TERMINATE.** Instruction-override content is not a
value to correct. Reporting it as one would read as an invitation to reword the
claim until it gets through, and there is no corrected form of it to send. The
same holds for a credential reaching the output gate and for a broken invariant:
none of them is something the request can fix, so the run ends with `ERROR` and
the containment rules under "Security gates" apply in full.

**The marker travels; it never becomes the product.** Once `error_code` is set,
every node after it returns it untouched and does no work of its own:
`PricingComplianceGraphNode.execute()` skips the inner graph, and `RetrieveNode`
/ `RerankFilterNode` / `GenerateAnswerNode` / `OutputFormatNode` short-circuit.
Without that, a run already declined would keep going and a later node would
overwrite the specific reason with a vaguer one. The marker also has to LEAVE
the subgraph, so `DomainWorkflowGraph.get_output()` emits it — a reason settled
inside the inner graph is otherwise invisible to the outer one — and
`merge_output()` prefers the outer value when both sides carry one, because a
reason settled before the inner run is the real one. `PostProcessNode` then maps
the code to its fixed sentence and writes that to `formatted_output` / `result`.

**The code is not surfaced; the sentence is.** `get_output()` returns the base
envelope unchanged whenever a marker is set, so `error_code` never appears in the
envelope and the structured compliance-risk fields — none of which were produced
— are withheld. The reason reaches the caller through the message text alone.
The sentences (`src/services/failure_message.py`) name WHAT to correct and
nothing else: none echoes the rejected value, names an internal field path, or
quotes a gate message. Those stay in `error_log`, the internal audit channel.

### Configuration split

Configuration lives in two files and the split is load-bearing:

| File | Contents | Read by |
|---|---|---|
| `config/agent.yaml` | the static registry entry: identity, dotted class path, trust level, compile-time `requires` | the registry, at discovery |
| `config/config.yaml` | runtime parameters: `max_retry`, `timeout_s`, and the `retrieval` + `llm` tuning blocks | the graph, at construction |

`PricingComplianceGraphNode._parent_config()` reads the `retrieval` + `llm`
blocks from `config/config.yaml` and forwards them under
`config["configurable"]` (never `{}`):

```
{"configurable": {"retrieval": {top_k, score_threshold, kb_path}, "llm": {...}}}
```

`get_subgraph()` passes that into `DomainWorkflowGraph(config=...)`; the inner
graph republishes the `retrieval` block into the inner initial state as the
JSON-string field `retrieval_config` (via `_extra_initial_state()`), so the
declared `top_k` / `score_threshold` are live at runtime. `RetrieveNode` and
`RerankFilterNode` read that state field, falling back to module defaults that
mirror the runtime file. Both read it through the same finite+bounded parser as
caller data: a non-finite `score_threshold` would compare False against every
score and drop the entire corpus.

The static manifest carries no tuning blocks, so a reader still pointed at it
would not fail — it would return nothing, and every declared value would quietly
become a module default. `test_config_manifest.py` pins the readers against the
live file for that reason, and an end-to-end test proves a declared value
reaches the inner graph.

### The caller-context bridge

The framework's `GraphNode.execute()` calls
`subgraph.invoke(user_input, session_id=..., ctx=...)`. It does **not** forward
`input_context`. Without a bridge, every field a caller sends on the context
channel stops at the outer graph and the inner pipeline silently runs on
defaults — with green unit tests, because at node level nothing is wrong.

`src/graph/context_bridge.py` closes that: the outer `extract_input()` stashes
the accepted claim context in a `ContextVar` on its way into the subgraph, and
the inner `_extra_initial_state()` seeds it into the inner initial state. A
`ContextVar` rather than a module global, so concurrent invocations in one
worker process cannot read each other's caller data. It is proven end to end —
a `discount_pct` sent on the context channel visibly changes the risk verdict —
because proving it at node level would prove nothing.

### State Definition

| Field | Type | Purpose | Layer |
|-------|------|---------|-------|
| `validated_input` | `NotRequired[str]` | non-empty-checked request payload (PreProcessNode) | outer |
| `claim_context` | `NotRequired[Optional[str]]` (JSON) | the caller fields ACCEPTED by the caller contract; the only caller data the pipeline reads, bridged into the inner graph | outer + inner |
| `compliance_answer` | `NotRequired[str]` | final answer, mapped from inner `formatted_answer` | outer |
| `risk_level` | `NotRequired[str]` | compliance-risk classification, mapped from the inner graph | outer + inner |
| `rule_citation_count` | `NotRequired[int]` | vetted SCALAR — count of cited provisions (PostProcessNode, SUCCESS-path only) | outer |
| `corrective_action_count` | `NotRequired[int]` | vetted SCALAR — count of corrective actions (PostProcessNode, SUCCESS-path only) | outer |
| `primary_rule_reference` | `NotRequired[Optional[str]]` | vetted SCALAR — id of the top-ranked cited provision (PostProcessNode, SUCCESS-path only) | outer |
| `compliance_query` | `NotRequired[str]` | normalised price-comparison query text | inner |
| `claim_filters` | `NotRequired[Optional[str]]` (JSON) | filter set derived from the accepted context: `{"category", "top_k", "discount_pct"}` | inner |
| `retrieval_config` | `NotRequired[Optional[str]]` (JSON) | forwarded runtime `retrieval` block | inner |
| `retrieved_rules` | `NotRequired[Optional[str]]` (JSON) | scored KB provision candidates | inner |
| `ranked_rules` | `NotRequired[Optional[str]]` (JSON) | reranked + threshold-filtered provisions | inner |
| `grounded_answer` | `NotRequired[str]` | rule-assembled compliance rationale body | inner |
| `citations` | `NotRequired[Optional[str]]` (JSON) | `[{ref, id, title, source}]` | inner |
| `corrective_actions` | `NotRequired[Optional[str]]` (JSON) | `list[str]` corrective-action guidance | inner |
| `formatted_answer` | `NotRequired[str]` | final answer + citations + corrective actions + disclaimer | inner |
| `intake_notes` | `NotRequired[Optional[str]]` (JSON) | validation / parse notes (never a caller value) | inner |
| `error_code` | `Optional[str]` | reason code for a run that COMPLETES without carrying out the request (`EMPTY_INPUT` / `INVALID_REQUEST`); read by every downstream node as the signal to do nothing, and by `get_output()` as the signal to release no structured product | outer + inner |
| `trace_id` / `correlation_id` | `Optional[str]` | framework-managed tracing | both |

**State Constraints (mandatory):**
- Flat `TypedDict` only (primitives + JSON-serialisable types).
- Structured fields (dict / list[dict]) stored as JSON STRINGS via `to_json()` /
  `from_json()` — used consistently by every producer AND consumer (msgpack
  safety: a bare dict or list in a checkpointed field corrupts silently).
- Domain fields are `NotRequired[...]` (valid TypedDict before any node writes).
- `formatted_output` is NOT re-declared (backbone field stays framework-owned).
- No JWT, API keys, credentials, or raw personal identifiers in State.
- `InvocationContext` via `config["configurable"]` only (never in State).
- No Pydantic models / dataclasses / arbitrary Python objects.

## Security gates

- **Trust gate:** every node declares `required_trust_level` (see the tables
  above). `PreProcessNode` and `PostProcessNode` require VERIFIED_EXTERNAL,
  matching the manifest; the inner domain nodes run at ANONYMOUS because they
  are reachable only behind those slots. The standalone server elevates
  authenticated bearer callers to VERIFIED_EXTERNAL (`INVOKE_AUTH_TOKEN`).
- **Input gate:** the framework's `FunctionNode` default masks PII in
  `user_input` / `validated_input` and screens those fields for injection
  content. **The template does not rely on that alone.** That gate does not see
  the context channel, and a deployment can run without it — a refusal that only
  happens when something upstream is configured a particular way is a fail-open
  by another name. `PreProcessNode` therefore enforces the caller contract in
  the node that reads the data (see "The caller-data contract" above), and the
  contract module carries its own test file exercised directly — no node and no
  framework gate in front — so what is proven is the rule itself rather than a
  wrapper's behaviour.
- **Output gate (recursive, and CONTAINING):** `PostProcessNode` calls the
  module-level `_security_gate_output()` scan from `execute()`. Its signature is
  `_security_gate_output(content: Any) -> Optional[str]` and it RECURSES into
  nested `dict` / `list` / `tuple` values — every string leaf of the rendered
  answer AND of the structured `citations` / `corrective_actions` payload is
  scanned before any value can reach the caller. It runs the platform's own
  credential detector first, then the domain patterns (API keys, JWTs, bearer
  tokens, credential assignments).

  Running the platform detector here is not redundant. The framework applies the
  same detector to whatever a node returns and RAISES on a hit — and a raise
  leaves the un-gated answer sitting in state, where
  `AgentBaseGraph.get_output()` picks it up as `state["result"]` **even on an
  error run**. Refusing is not containing. So on a violation this node returns
  ERROR *and clears every output-bearing state field* (`result`,
  `formatted_output`, `compliance_answer`, `formatted_answer`,
  `grounded_answer`, `citations`, `corrective_actions`, `risk_level`, and the
  three scalar summary fields), leaving the error envelope nothing to fall back
  to. For the same reason `execute()` never lets an exception escape: an
  exception leaves state untouched, which is exactly the uncontained case.

  No `_extra_security_gate_input` / `_extra_security_gate_output` instance
  methods are defined on any node.
- **Audit logging:** every node's `execute()` emits at least one
  domain-specific `emit_trace_event("<event>", {small payload with no caller
  values}, state)` on its reachable path. Nodes do NOT emit `node_start` /
  `node_complete` / `node_error` — `BaseNode.__call__()` emits those. Domain
  event names:
  - `pricing_query_accepted` (pre_process, success) / `pricing_query_rejected`
    (pre_process, `reason` = `empty_input` | `caller_contract` | `screening`)
  - `input_validate_complete` / `input_validate_rejected` (`reason` =
    `caller_contract` | `screening`)
  - `retrieve_complete`
  - `rerank_filter_complete`
  - `generate_answer_complete`
  - `output_format_complete`
  - `compliance_answer_emitted` (post_process, success) / `compliance_answer_blocked` (post_process, output-gate violation) / `post_process_degraded` (post_process, a run completing on a reason code — the payload carries the code, never caller content)

## Output invariants

The output boundary enforces the invariants this template actually states,
enumerated here so each one has a test:

1. **No credential-shaped content in ANY representation** — rendered text and
   the structured payload underneath it, scanned recursively, with the platform
   detector included so nothing the framework would raise on gets past.
2. **A violation releases nothing** — ERROR, every output-bearing field cleared,
   and an error envelope carrying no released text, no traceback, no source
   paths.
3. **Structured product only on a run that produced one, scalars only** — a
   SUCCESS status is not sufficient on its own, because a request declined on a
   correctable value also completes with SUCCESS. The gate is the absence of a
   reason code AND a SUCCESS status; see "Structured output" below.
4. **Every answer carries the advisory line** — see "Advisory disclaimer".
5. **The rationale body is grounded by construction** — it is assembled from the
   ranked provisions alone. The caller's claim appears only in the lead
   sentence, and only after the caller contract has rendered it inert.

**Monetary precision grid: not applicable to this template.** A rounding grid
over rendered monetary aggregates is the right invariant for a template that
computes and prints money. This one does not: it renders a risk label, cited
provisions, and corrective-action guidance, and the only numbers in the corpus
are statutory article references (`第5条第2項`). Applying a currency-snapping
grammar here would corrupt those references rather than protect anything, so the
invariants above are enforced instead.

## Structured Output — `get_output()` override

This template's proposal calls for a compliance-risk assessment product (risk
level + citation/action counts), not just rendered text, so
`ECPricingComplianceValidationAgent.get_output()` is overridden to EXTEND
`super().get_output()` (the `AgentBaseGraph` envelope: `output`, `status`,
`trace_id`, `correlation_id`, `node_history` — never replaced, so downstream
callers keep every field they already rely on):

```python
def get_output(self, state) -> dict:
    base = super().get_output(state)
    if state.get("error_code"):
        return base  # declined — the sentence saying what to correct, not a product
    if state.get("status") != AgentStatus.SUCCESS.value:
        return base  # fail-closed — no structured keys off the SUCCESS path
    return {
        **base,
        "compliance_risk_level": state.get("risk_level"),
        "rule_citation_count": state.get("rule_citation_count"),
        "corrective_action_count": state.get("corrective_action_count"),
        "primary_rule_reference": state.get("primary_rule_reference"),
    }
```

Two invariants keep this safe:
1. **Answered runs only:** an output-gate violation or any other non-SUCCESS
   status returns the base envelope only, so a blocked or errored run can never
   leak a structured field. A SUCCESS status alone is not the gate, because a
   request declined on a correctable value also completes with SUCCESS — that
   run holds the sentence saying what to correct, and none of the structured
   fields was ever produced. The reason-code check therefore comes first, and
   both checks are fail-closed: anything other than an answered run returns the
   base envelope untouched.
2. **Scalars only, never the raw containers:** `get_output()` never surfaces
   `citations` or `corrective_actions` themselves (nested dict/list payloads)
   — only the whitelisted vetted SCALAR fields `PostProcessNode` derived
   AFTER the recursive output scan passed clean. The full citation/action text
   still reaches the caller through `output` (the rendered `formatted_answer`
   string, itself scanned), just not as a second, unscanned structured
   surface.

## Advisory Disclaimer

Every answer carries a standing compliance advisory line (informational only,
not legal advice, verify with legal/compliance counsel before publishing or
amending a price-comparison claim). It is appended by `OutputFormatNode` as
part of the domain output contract — NOT injected by `post_process`
(post_process only gates).

## v1 Implementation Note — LLM synthesis

v1 of this template is **deterministic end-to-end**: retrieval is keyword
scoring over the seeded KB, and `GenerateAnswerNode` assembles the grounded
rationale, `risk_level`, and `corrective_actions` rule-based from the ranked
provisions and the caller-supplied `discount_pct` only. There is NO live LLM
call and no LLM client dependency in v1 — the `llm` manifest block is
forwarded through `_parent_config()` for forward-compatibility but is not
consumed by any v1 node, and no `system_prompt` is read at runtime. The LLM
synthesis upgrade seam is documented in
`config/prompts/answer_synthesis_prompt.md`: a v2 `GenerateAnswerNode` swaps
the rule-based assembly for an LLM call that synthesises over the same
`ranked_rules` input and emits the same `grounded_answer` / `citations` /
`risk_level` / `corrective_actions` state contract, so no other node changes.

## Composition Pattern

- **Pattern:** `GraphNode` (subgraph) in the outer `main` slot.
- **Composition target:** `DomainWorkflowGraph` (inner `BaseGraph`).
- **Error propagation strategy:** `propagate` (inner errors re-raised as `SubgraphError`).
- Inner domain nodes run at `TrustLevel.ANONYMOUS`; outer pre/post_process run
  at `TrustLevel.VERIFIED_EXTERNAL`.

## Import Isolation Confirmation
- [x] Template does not import the platform SDK directly.
- [x] Import targets: `framework/` and `shared/` only.
- [x] The base position names a framework base class, never a derived agent type.

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Framework base class | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | Fixed multi-step retrieval/validation workflow, no autonomous loop |
| Composition pattern | A single main-slot node | GraphNode → inner BaseGraph | **GraphNode → inner BaseGraph** | A 5-step domain workflow exceeds a single `main` node; nesting keeps the outer backbone untouched |
| Risk/answer synthesis | Rule-based assembly | Model call | **Rule-based (v1)** | Deterministic assembly is testable and auditable for a compliance-adjacent output; v2 swaps a model in at the documented seam |
| KB storage | External vector store | Seeded JSON KB | **Seeded JSON KB (v1)** | Self-contained, deterministic CI, no live web crawling / competitor-price lookup (per proposal); the retrieval contract (`retrieved_rules` JSON) is store-agnostic for a later vector-store upgrade |
| Structured output | Rendered text only | `get_output()` override, scalar whitelist | **`get_output()` override** | The product is a compliance-risk assessment, not rendered text alone; the override extends (never replaces) the base envelope and surfaces only vetted SUCCESS-path scalars — never the raw citation/action containers |
