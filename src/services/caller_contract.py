"""AgentCore Platform v1.0"""

# RET-C2-667 - the caller-data contract.
#
# One module owns every rule that applies to data the CALLER supplies, so the
# nodes that read caller data cannot each invent their own (weaker) version:
#
#   1. numbers  - parsed finite and in range, or REJECTED (never clamped past a
#                 non-finite value, never silently dropped)
#   2. labels   - caller strings that end up rendered in the report are locked
#                 to an inert identifier alphabet
#   3. screening- caller text is screened for prompt-injection content before
#                 anything downstream treats it as a question
#
# Why numbers must be finite
# --------------------------
# float("NaN") and float("Infinity") both parse, and JSON accepts the bare
# tokens NaN / Infinity in a request body. Every comparison against NaN is
# False, so a NaN discount would sail through a `>= threshold` risk check and
# silently DOWNGRADE the assessment - a fail-open on precisely the decision
# this agent exists to make. Numbers are therefore validated, not coerced.
#
# Why the screen runs on three forms of the same string
# ----------------------------------------------------
# Stripping markup is not refusal: removing a `<|...|>` control span turns a
# recognisable token attack into ordinary-looking prose that the next reader
# happily follows, and re-assembles a directive that was split by inline tags
# ("ig<b>nore all previous instructions"). So each string is screened raw,
# normalised (URL-decoding, compatibility folding, zero-width removal), AND
# markup-stripped - tokens are caught before the strip removes them, spliced
# directives after it re-assembles them.

from __future__ import annotations

import math
import re
import unicodedata
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 1. Numbers - finite and bounded, or rejected
# --------------------------------------------------------------------------


class CallerDataError(ValueError):
    """A caller field failed the contract. The message names the FIELD only.

    The offending value is deliberately never interpolated: an error string
    travels into logs and, on some deployments, back to the caller, and echoing
    rejected input there is how a rejected payload gets a second delivery route.
    """


class ScreeningRefused(CallerDataError):
    """Caller text carried prompt-injection content and was refused.

    A SEPARATE TYPE, not a separate message. Every other failure in this module
    describes a value the caller can correct and resend; this one does not, and
    the two must be distinguishable by something stronger than the wording of a
    message — wording gets reworded, and the distinction would be lost silently
    the next time one of these strings is edited.

    Kept as a subclass so every existing ``except CallerDataError`` still
    catches it: a handler that does not know about this type keeps its old,
    stricter behaviour rather than letting the refusal escape.
    """


def finite_in_range(
    value: Any,
    *,
    field: str,
    minimum: float,
    maximum: float,
) -> float:
    """Return ``value`` as a finite float within [minimum, maximum], or raise.

    Rejects, in order: booleans (``isinstance(True, int)`` is True in Python, so
    ``True`` would otherwise arrive as 1.0), non-numeric types and strings,
    NaN / +Infinity / -Infinity in either literal or parsed form, and anything
    outside the declared range. Fails CLOSED - there is no clamp path, because a
    clamp turns a nonsense input into a plausible answer.
    """
    if isinstance(value, bool):
        raise CallerDataError(f"{field}: must be a number, not a boolean")
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value.strip())
        except (TypeError, ValueError):
            raise CallerDataError(f"{field}: must be a number") from None
    else:
        raise CallerDataError(f"{field}: must be a number")
    if not math.isfinite(parsed):
        raise CallerDataError(f"{field}: must be a finite number")
    if parsed < minimum or parsed > maximum:
        raise CallerDataError(f"{field}: must be between {minimum} and {maximum}")
    return parsed


def finite_int_in_range(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    """Integer form of :func:`finite_in_range` - whole numbers only."""
    parsed = finite_in_range(value, field=field, minimum=float(minimum), maximum=float(maximum))
    if parsed != int(parsed):
        raise CallerDataError(f"{field}: must be a whole number")
    return int(parsed)


# --------------------------------------------------------------------------
# 2. Labels - inert alphabets for anything that renders
# --------------------------------------------------------------------------

# Category keys select a knowledge-base partition; they are never free text.
_CATEGORY_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Product / competitor labels are rendered into the assessment, so they are held
# to an inert alphabet: letters, digits, and the three separators real product
# codes use. No whitespace (a directive sentence needs it), no markup
# characters, no brackets - so a label cannot forge a heading, a citation
# marker, or a second risk verdict inside the rendered report.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

MAX_LABEL_CHARS = 32
MAX_CLAIM_CHARS = 2000


def inert_category(value: Any, *, field: str) -> str:
    """Validate a knowledge-base category key against the inert alphabet."""
    if not isinstance(value, str) or not _CATEGORY_RE.match(value.strip().lower()):
        raise CallerDataError(f"{field}: must match [a-z0-9_] and be 1-{MAX_LABEL_CHARS} characters")
    return value.strip().lower()


def inert_label(value: Any, *, field: str) -> str:
    """Validate a caller label that will be rendered into the assessment."""
    if not isinstance(value, str) or not _LABEL_RE.match(value.strip()):
        raise CallerDataError(f"{field}: must match [A-Za-z0-9._-] and be 1-{MAX_LABEL_CHARS} characters")
    return value.strip()


# Characters that carry structure in the rendered report. The claim is the one
# caller string that is free text by nature (it is the question being asked), so
# it is not rejected for containing them - it is rendered with them removed, and
# it is screened separately below. Removing them means a claim cannot open a
# heading, emphasise itself, forge a "[1]" citation marker, or introduce a
# second "Risk Level:" line.
_STRUCTURAL_CHARS_RE = re.compile(r"[#*_`~|<>\[\]{}\\]+")
_WHITESPACE_RE = re.compile(r"\s+")


def inert_text(value: str, *, limit: int = MAX_CLAIM_CHARS) -> str:
    """Render-safe form of a free-text caller string.

    Collapses all whitespace to single spaces (so no injected line structure
    survives), removes the characters that carry structure in the report, and
    caps the length. This is a RENDERING transform, never a substitute for the
    screen below.
    """
    collapsed = _WHITESPACE_RE.sub(" ", _STRUCTURAL_CHARS_RE.sub(" ", value))
    collapsed = _WHITESPACE_RE.sub(" ", collapsed).strip()
    return collapsed[:limit]


# --------------------------------------------------------------------------
# 3. Screening - injection content, as classes rather than one-off phrases
# --------------------------------------------------------------------------

_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\ufeff\u00ad]")
_MARKUP_TAG_RE = re.compile(r"<[^<>]{0,64}>")
_CONTROL_SPAN_RE = re.compile(r"<\|[^|]{0,64}\|>")

# Chat-template control tokens, screened as a CLASS. A screen built from
# directive phrases alone misses these entirely, and they are the more dangerous
# form: they do not ask a model to change behaviour, they forge the frame in
# which the model decides what its instructions are. None of them occur in
# retail pricing prose.
_CONTROL_TOKEN_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("chat_control_span", re.compile(r"<\||\|>")),
    ("chat_role_tag", re.compile(r"\[/?\s*(?:INST|SYS)\s*\]", re.IGNORECASE)),
    ("chat_role_tag", re.compile(r"<<\s*/?\s*SYS\s*>>", re.IGNORECASE)),
    ("chat_role_tag", re.compile(r"<\s*/?\s*(?:system|user|assistant)\s*>", re.IGNORECASE)),
    ("chat_role_heading", re.compile(r"###\s*(?:system|instruction)\b", re.IGNORECASE)),
]

# Directive phrasing. Every pattern is anchored on both ends so it cannot fire
# on the domain's own vocabulary - the corpus this agent works over is full of
# sentences like "reference price within the preceding eight weeks" and
# "do not publish the claim until it is substantiated", and a loose pattern here
# refuses real compliance work, which is the more damaging failure direction.
_DIRECTIVE_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+|your\s+)*"
            r"(?:previous|above|prior|earlier|preceding|system)\s+"
            r"(?:instruction|prompt|rule|context|direction|message)s?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget)\s+(?:all|any|every)\s+"
            r"(?:the\s+)?(?:rule|instruction|restriction|guideline|guardrail)s?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction_override",
        re.compile(
            r"\boverride\s+(?:your|the|all)\s+(?:instruction|rule|restriction|safety)\w*\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_override",
        re.compile(
            r"\bact\s+as\s+(?:a|an)\s+(?:different|new|unrestricted|unfiltered|evil|"
            r"jailbroken|dan|god|admin|root|superuser|hacker)\b",
            re.IGNORECASE,
        ),
    ),
    ("role_override", re.compile(r"\bjailbreak(?:ing|ed)?\b", re.IGNORECASE)),
    (
        "prompt_disclosure",
        re.compile(
            r"\b(?:reveal|print|show|repeat|output|display)\s+(?:me\s+)?"
            r"(?:your|the)\s+(?:full\s+|entire\s+|original\s+)?"
            r"(?:system\s+)?(?:prompt|instruction|rule)s?\b",
            re.IGNORECASE,
        ),
    ),
    # UNION SELECT is the one SQL form worth screening: bare verbs and bare
    # punctuation appear constantly in ordinary text and in this corpus.
    ("sql_injection", re.compile(r"\bunion\s+(?:all\s+)?select\b", re.IGNORECASE)),
]


def _normalize(text: str) -> str:
    """Undo the cheap obfuscations before screening."""
    decoded = urllib.parse.unquote(text)
    folded = unicodedata.normalize("NFKC", decoded)
    return _ZERO_WIDTH_RE.sub("", folded)


def _strip_markup(text: str) -> str:
    """The form a downstream reader sees after markup is removed.

    Screening this form as well is what catches a directive that was split by
    inline tags: "ig<b>nore all previous instructions" is inert to a raw scan
    and reads as a plain directive once the tags are gone.
    """
    without_spans = _CONTROL_SPAN_RE.sub(" ", text)
    without_tags = _MARKUP_TAG_RE.sub("", without_spans)
    return _WHITESPACE_RE.sub(" ", without_tags)


def screen_text(text: str) -> Optional[str]:
    """Return the violation class for hostile text, or None when it is clean.

    Each of the three forms of the string is screened with every pattern:
    control tokens have to be caught before the strip removes them, and spliced
    directives only become visible once it has.
    """
    if not isinstance(text, str) or not text:
        return None
    normalised = _normalize(text)
    forms = (text, normalised, _strip_markup(normalised))
    for form in forms:
        for name, pattern in _CONTROL_TOKEN_PATTERNS:
            if pattern.search(form):
                return name
        for name, pattern in _DIRECTIVE_PATTERNS:
            if pattern.search(form):
                return name
    return None


# A caller-supplied key is reported by POSITION, never by name: a hostile field
# name is caller-controlled text, and naming it in an error message is the same
# echo the value rules forbid.
def _mask_key(index: int) -> str:
    return f"field#{index}"


def screen_payload(payload: Any, *, path: str = "input_context") -> Optional[str]:
    """Depth-first screen of a parsed caller payload - KEYS INCLUDED.

    Returns ``"<path>: <violation class>"`` for the first hostile element, or
    None. Scanning after parsing (rather than the raw request text) is what
    makes JSON \\u escapes useless as an evasion: by this point the escape has
    become the character it encodes. Keys are screened because a payload can
    carry its directive in a field NAME just as easily as in a value.
    """
    if isinstance(payload, str):
        violation = screen_text(payload)
        return f"{path}: {violation}" if violation else None
    if isinstance(payload, dict):
        for index, (key, value) in enumerate(payload.items()):
            if isinstance(key, str):
                violation = screen_text(key)
                if violation:
                    return f"{path}.{_mask_key(index)}: {violation}"
            child = f"{path}.{key}" if _LABEL_RE.match(str(key)) else f"{path}.{_mask_key(index)}"
            found = screen_payload(value, path=child)
            if found:
                return found
        return None
    if isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            found = screen_payload(item, path=f"{path}[{index}]")
            if found:
                return found
        return None
    return None


def screen_context_text(context: Dict[str, Any]) -> Optional[str]:
    """Screen a caller context mapping - the entry-point convenience wrapper."""
    return screen_payload(context, path="input_context")


# --------------------------------------------------------------------------
# 4. Composition - the whole caller contract in one place
# --------------------------------------------------------------------------

# Structural caps on the context channel. The transport adapter caps the whole
# body; these cap the SHAPE, so a body that is small enough to arrive still
# cannot carry an unbounded number of fields or an unbounded single string.
MAX_CONTEXT_ENTRIES = 32
MAX_CONTEXT_STRING_CHARS = 4000

# Retrieval overrides the caller may set. Bounds are the node's own operating
# range, not a guess: below 1 there is nothing to retrieve, above 20 the ranked
# list stops being a citation list.
TOP_K_MIN, TOP_K_MAX = 1, 20
DISCOUNT_PCT_MIN, DISCOUNT_PCT_MAX = 0.0, 100.0


def _check_shape(context: Dict[str, Any]) -> None:
    """Reject an over-wide or over-long context before anything reads it."""
    if len(context) > MAX_CONTEXT_ENTRIES:
        raise CallerDataError(f"input_context: at most {MAX_CONTEXT_ENTRIES} fields are accepted")
    for index, value in enumerate(context.values()):
        if isinstance(value, str) and len(value) > MAX_CONTEXT_STRING_CHARS:
            raise CallerDataError(
                f"input_context.{_mask_key(index)}: at most " f"{MAX_CONTEXT_STRING_CHARS} characters are accepted"
            )


def parse_claim_envelope(user_input: str) -> Dict[str, Any]:
    """Read the JSON claim envelope out of the request string.

    The string entry point accepts either plain text (the whole string is the
    claim) or a JSON object carrying the same fields the context channel takes.
    A string that opens like JSON but does not parse is treated as plain text
    rather than rejected - callers do send prose containing braces.
    """
    import json

    text = user_input.strip() if isinstance(user_input, str) else ""
    if not text.startswith("{"):
        return {"claim": text}
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {"claim": text}
    if not isinstance(payload, dict):
        return {"claim": text}
    return payload


def build_claim_context(user_input: str, input_context: Any) -> Dict[str, Any]:
    """Validate every caller field and return the accepted claim context.

    Raises :class:`CallerDataError` - naming the field, never the value - on the
    first field that fails. There is no partial-acceptance path: a payload that
    carries one hostile or malformed field is refused whole, because the fields
    are read together and a half-validated claim is not a claim.

    Screening happens on the PARSED payloads. A caller can hide a control token
    from a scan of the raw request body as a ``\\u003c`` escape; by the time the
    payload is a Python object the escape has become the character it encodes,
    and the same scan sees it plainly.
    """
    context: Dict[str, Any] = input_context if isinstance(input_context, dict) else {}
    _check_shape(context)

    envelope = parse_claim_envelope(user_input)
    _check_shape(envelope)

    # Screen every caller-reachable surface: the request string as sent, the
    # envelope parsed out of it, and the context mapping - keys included.
    raw_violation = screen_text(user_input if isinstance(user_input, str) else "")
    if raw_violation:
        raise ScreeningRefused(f"user_input: rejected ({raw_violation})")
    for source, label in ((envelope, "user_input"), (context, "input_context")):
        violation = screen_payload(source, path=label)
        if violation:
            raise ScreeningRefused(f"rejected ({violation})")

    # The context channel wins field by field; the envelope is the fallback, so
    # an existing string-only caller keeps working unchanged.
    merged: Dict[str, Any] = dict(envelope)
    merged.update(context)

    claim_raw = merged.get("claim")
    if claim_raw is None:
        claim_raw = merged.get("query")
    if claim_raw is not None and not isinstance(claim_raw, str):
        raise CallerDataError("claim: must be text")

    accepted: Dict[str, Any] = {
        "claim": inert_text(claim_raw or "", limit=MAX_CLAIM_CHARS),
    }
    if merged.get("product") is not None:
        accepted["product"] = inert_label(merged["product"], field="product")
    if merged.get("competitor") is not None:
        accepted["competitor"] = inert_label(merged["competitor"], field="competitor")
    if merged.get("category") is not None:
        accepted["category"] = inert_category(merged["category"], field="category")
    if merged.get("top_k") is not None:
        accepted["top_k"] = finite_int_in_range(merged["top_k"], field="top_k", minimum=TOP_K_MIN, maximum=TOP_K_MAX)
    if merged.get("discount_pct") is not None:
        accepted["discount_pct"] = finite_in_range(
            merged["discount_pct"],
            field="discount_pct",
            minimum=DISCOUNT_PCT_MIN,
            maximum=DISCOUNT_PCT_MAX,
        )
    return accepted


def compose_query(claim_context: Dict[str, Any]) -> str:
    """Build the retrieval query from the accepted claim context.

    Labels are appended as validated identifiers so they contribute retrieval
    signal; every part is already inert by the time it gets here.
    """
    parts: List[str] = []
    claim = claim_context.get("claim") or ""
    if claim:
        parts.append(claim)
    product = claim_context.get("product")
    if product:
        parts.append(f"(product: {product})")
    competitor = claim_context.get("competitor")
    if competitor:
        parts.append(f"(competitor: {competitor})")
    return inert_text(" ".join(parts), limit=MAX_CLAIM_CHARS)
