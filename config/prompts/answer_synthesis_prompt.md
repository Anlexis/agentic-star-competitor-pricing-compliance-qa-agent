# Answer Synthesis Prompt — RET-C2-667 (v2 LLM upgrade seam)

> **v1 does NOT use this prompt at runtime.** v1 of `GenerateAnswerNode` is
> deterministic (rule-based grounded assembly + a fixed risk ladder over
> `ranked_rules`); no node reads this file. It documents the synthesis
> contract for the v2 LLM upgrade described in `docs/02_design.md` ("v1
> Implementation Note — LLM synthesis"), so the v2 swap changes only the
> inside of `GenerateAnswerNode.execute()`.

## Contract (v2 GenerateAnswerNode)

- **Input:** the same `ranked_rules` JSON (id / title / category / source /
  score / excerpt), `compliance_query`, and `claim_filters` (incl.
  `discount_pct`) the v1 node reads.
- **Output:** the same state contract — `grounded_answer` (str, with
  numbered `[n]` citation markers), `citations` (JSON list of
  `{ref, id, title, source}`), `risk_level` (one of `insufficient_data` /
  `low_risk` / `medium_risk` / `high_risk`), and `corrective_actions` (JSON
  list of guidance strings).
- **Grounding rule:** every factual statement in the rationale must be
  traceable to one of the supplied provisions via a `[n]` marker; content
  not present in the provisions must not be asserted.
- **No-coverage rule:** when no provision supports the claim, say so and
  recommend refining the claim or escalating to the compliance team — never
  answer from parametric knowledge, and set `risk_level = "insufficient_data"`.
- **Risk-level rule:** `risk_level` must be derived ONLY from the supplied
  `ranked_rules` match strength and the caller-supplied `discount_pct` — never
  from an LLM's own judgement of "is this compliant," which this template
  deliberately does not attempt (see the advisory disclaimer in
  `OutputFormatNode`: this is a risk assessment, not a legal determination).
- **Tone:** neutral, compliance-appropriate, no individualized legal
  conclusions (the advisory disclaimer is appended downstream by
  `OutputFormatNode`).

## Prompt template

```
You assess retail-EC price-comparison and discount claims strictly from the
compliance knowledge-base provisions provided below.

Claim:
{compliance_query}

Discount percentage claimed (if any): {discount_pct}

Provisions (each with a reference number):
{ranked_rules}

Rules:
1. Use ONLY the provisions above. If they do not bear on the claim, say the
   knowledge base has insufficient coverage and stop.
2. Mark every factual statement with the [n] reference of its provision.
3. Classify risk_level as one of insufficient_data / low_risk / medium_risk /
   high_risk based on provision relevance and the discount percentage —
   never assert the claim IS or IS NOT compliant.
4. Propose 1-3 concrete corrective_actions appropriate to the risk_level.
5. Keep the rationale under 300 words.
```

## Manifest coupling

The `llm` block in `config/agent.yaml` (`temperature`, `max_tokens`) is
already forwarded to the inner graph via
`PricingComplianceGraphNode._parent_config()` under
`config["configurable"]["llm"]`; the v2 node reads it from there.
