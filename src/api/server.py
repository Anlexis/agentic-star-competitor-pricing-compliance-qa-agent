"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only - no business logic here.
# For platform-level routing, the gateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any, Dict, Optional, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import ECPricingComplianceValidationAgent, load_runtime_config

app = FastAPI(title="Agent")

# The runtime parameters live in config/config.yaml. A platform deployment gets
# them from the registry; loading them here means a standalone run honours the
# same declared values (max_retry, timeout_s) instead of silently falling back
# to framework defaults.
agent = ECPricingComplianceValidationAgent(config=load_runtime_config())
agent.compile()
# Replace namespace/agent_name to match the agent's manifest values.
agent.provision_secrets(secrets_factory(namespace="ret", agent_name="ECPricingComplianceValidationAgent"))

# Size cap on the structured caller channel. The claim context is a handful of
# short fields; anything approaching this is not a pricing claim, and the cap
# keeps a large body from reaching the validator at all.
_MAX_INPUT_CONTEXT_BYTES = 256 * 1024


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured caller data. The claim and its labels can travel here instead
    # of being packed into `input` as a JSON string; every field is validated
    # against the caller contract before anything reads it.
    input_context: Optional[Dict[str, Any]] = Field(default=None)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary - a deployment-level caller
    # credential, not an agent secret, so ctx.secrets does not apply (no
    # InvocationContext exists before auth).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose - do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    input_context: Dict[str, Any] = req.input_context or {}
    if input_context:
        try:
            encoded_size = len(json.dumps(input_context).encode("utf-8"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="input_context is not serialisable.")
        if encoded_size > _MAX_INPUT_CONTEXT_BYTES:
            raise HTTPException(status_code=413, detail="input_context is too large.")

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        # The framework's invoke() is untyped, so its return annotation
        # resolves to Any; cast at this one boundary rather than loosening the
        # adapter's own contract.
        envelope: Dict[str, Any] = cast(Dict[str, Any], agent.invoke(req.input, ctx=ctx, input_context=input_context))
        return envelope


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "ECPricingComplianceValidationAgent"}
