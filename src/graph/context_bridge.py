"""AgentCore Platform v1.0"""

# RET-C2-667 - caller-context bridge between the outer and inner graph.
#
# Why this module exists
# ----------------------
# The caller passes structured claim data to `invoke()` through the framework's
# `input_context` parameter, and the framework seeds it into the OUTER graph's
# initial state. The outer `main` slot is a GraphNode that runs the inner
# DomainWorkflowGraph, and GraphNode.execute() calls
# `subgraph.invoke(user_input, session_id=..., ctx=...)` - it does not pass
# `input_context` through. The inner graph therefore starts with an EMPTY
# input_context and every inner node that reads caller data sees nothing.
#
# This bridge closes that gap without touching the framework: the outer
# GraphNode stashes the caller context in a ContextVar on its way into the
# subgraph, and the inner graph's `_extra_initial_state()` hook seeds the
# stashed value into the inner initial state.
#
# A ContextVar (not a module global) is deliberate: it is per-context, so
# concurrent invocations in the same worker process cannot read each other's
# caller data.

from contextvars import ContextVar, Token
from typing import Any, Dict, Optional

# Holds the caller context for the duration of one outer -> inner hand-off.
# Default None means "nothing stashed"; consumers then fall back to whatever
# the inner graph was seeded with by the framework (an empty mapping).
_CALLER_CONTEXT: ContextVar[Optional[Dict[str, Any]]] = ContextVar("ret_c2_667_caller_context", default=None)


def stash_caller_context(context: Any) -> "Token[Optional[Dict[str, Any]]]":
    """Stash the caller context for the inner graph. Returns the reset token.

    Non-mapping values are normalised to an empty mapping so a malformed
    caller payload can never reach the inner graph as an unexpected type.
    A shallow copy is stored so a later mutation of the outer state cannot
    change what the inner graph already read.
    """
    if not isinstance(context, dict):
        return _CALLER_CONTEXT.set({})
    return _CALLER_CONTEXT.set(dict(context))


def take_caller_context() -> Dict[str, Any]:
    """Return the stashed caller context (empty mapping when nothing was stashed)."""
    stashed = _CALLER_CONTEXT.get()
    if not isinstance(stashed, dict):
        return {}
    return dict(stashed)


def reset_caller_context(token: "Token[Optional[Dict[str, Any]]]") -> None:
    """Restore the previous value - always call this once the hand-off is done."""
    _CALLER_CONTEXT.reset(token)
