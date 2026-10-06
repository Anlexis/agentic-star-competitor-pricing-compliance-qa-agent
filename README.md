# Competitor Pricing Compliance Q&A Agent

AI agent for validating e-commerce competitor pricing claims against the Premiums Act, built with Agentic Star.

> **Category**: Cat 2 (retail-domain compliance-validation retrieval pipeline)
> **Industry**: Retail
> **Template ID**: RET-C2-667

## Overview

Checks a retail e-commerce price-comparison claim against a curated knowledge base of
Japanese pricing-representation rules (景品表示法 — the Act against Unjustifiable Premiums
and Misleading Representations) and returns a compliance-risk assessment.

You supply the claim yourself — for example *"we advertise this product 30% below a
competitor's listed price"* — and the agent retrieves the provisions that bear on it,
classifies the risk, cites the rules it relied on, and lists the corrective actions the
knowledge base prescribes for that risk level. It performs no web crawling and no
competitor-price lookup: the caller provides the price data and the seeded knowledge base
is the only retrieval source, so the assessment is reproducible and auditable.

The assessment is informational. It is not legal advice and does not determine compliance.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. The agent resolves its secrets provider and compiles its graph at start-up; if the platform
is unreachable or the framework version does not match, start-up fails there rather than the agent
coming up in a partially working state. This is intentional — a half-running agent is worse than
one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test contract.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
