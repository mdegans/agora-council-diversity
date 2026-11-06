"""Blinded stance judge: Claude Haiku 5.5 labels pairs of concerns.

Each request is one pair. The common prefix comes first and is cached for an
hour: the instructions (system), then the Constitution and the Governance
Protocol as documents, with the 1h breakpoint on the last of them. Only the
pair varies, at the end.

The API rules from Agora's CLAUDE.md, checked by `check_request`:
- the output schema asks for a label only, no reasoning field under any name;
- adaptive thinking with display "summarized";
- no temperature (or other sampling key), no tools and so no forced tool_choice;
- strict structured output (output_config.format json_schema) with no $ref,
  $defs or pattern, and additionalProperties false.

Blinding: no model names, ids or seat names in the request; whitespace and
markdown formatting normalised away; which concern is A and which is B is
randomised per pair, as is the order of pairs in the batch.
"""

from __future__ import annotations

import json
import random
import re

from .adapter import ONE_HOUR, canonical_bytes, sha256

MODEL = "claude-haiku-5-5"
LABELS = ("agree", "contradict", "unrelated_or_new")
MAX_TOKENS = 8000

DISCLAIMER = (
    "The stance judge is Claude Haiku 5.5, an Anthropic model of the same family as both "
    "models it compares (Claude Opus 4.6 and Claude Haiku 5.5); it may share their blind "
    "spots, and it is one of the compared models. A judge from another maker (Qwen, run "
    "locally) is planned for February 2027 to compare against. The judge saw each pair "
    "blinded (no model names, ids or seat names, formatting normalised, sides and order "
    "randomised) and returned labels only."
)

SYSTEM = (
    "You compare two short statements taken from written positions on a governance "
    "question for an online community of AI agents. The community's Constitution and "
    "Governance Protocol are attached for context. For each pair, decide how statement B "
    "relates to statement A:\n"
    "- agree: they make the same point, or compatible points on the same concern;\n"
    "- contradict: they address the same concern and take incompatible stances on it;\n"
    "- unrelated_or_new: they address different concerns, so B raises something A does "
    "not (or the reverse).\n"
    "Judge the substance, not the wording, length or style. Reply with the label only."
)

SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": list(LABELS)}},
    "required": ["label"],
    "additionalProperties": False,
}

_MODEL_NAMES = re.compile(
    r"\b(claude|anthropic|opus|haiku|sonnet|fable|qwen|gpt|gemini|llama)"
    r"(\s*[-\d.]+)?\b",
    re.I,
)
_SEATS = re.compile(r"\b(the\s+)?(artist|philosopher|lawyer|engineer)('s)?\b", re.I)
_IDS = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_MD = [
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),  # headers
    (re.compile(r"^\s*([-*+]|\d+[.)])\s+", re.M), ""),  # list markers
    (re.compile(r"(\*\*|__|\*|`)"), ""),  # emphasis, code ticks
    (re.compile(r"^\s*>\s?", re.M), ""),  # quotes markers
]


def blind(text: str) -> str:
    t = text
    for rx, rep in _MD:
        t = rx.sub(rep, t)
    t = _IDS.sub("[id]", t)
    t = _MODEL_NAMES.sub("[model]", t)
    t = _SEATS.sub(lambda m: ("the " if m.group(1) else "") + "[seat]", t)
    return re.sub(r"\s+", " ", t).strip()


def prefix_blocks(constitution: str, protocol: str) -> tuple[list[dict], list[dict]]:
    system = [{"type": "text", "text": SYSTEM}]
    docs = [
        {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": constitution},
            "title": "The Agora Constitution",
        },
        {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": protocol},
            "title": "The Agora Governance Protocol",
            "cache_control": dict(ONE_HOUR),
        },
    ]
    return system, docs


def build_request(system: list[dict], docs: list[dict], a: str, b: str) -> dict:
    pair = f"Statement A:\n{a}\n\nStatement B:\n{b}"
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": [*docs, {"type": "text", "text": pair}]}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }


_FORBIDDEN = re.compile(r"reason|rationale|explan|think|thought|scratch|chain_of|trace", re.I)


def check_request(req: dict) -> None:
    for k in ("temperature", "top_p", "top_k", "tools", "tool_choice"):
        assert k not in req, k
    assert req["thinking"] == {"type": "adaptive", "display": "summarized"}
    schema = req["output_config"]["format"]["schema"]
    s = json.dumps(schema)
    for bad in ('"$ref"', '"$defs"', '"definitions"', '"pattern"'):
        assert bad not in s, bad
    assert schema.get("additionalProperties") is False
    assert list(schema["properties"]) == ["label"]
    assert not any(_FORBIDDEN.search(p) for p in schema["properties"])
    bps = [b for b in req["messages"][0]["content"] if "cache_control" in b]
    assert len(bps) == 1 and bps[0]["cache_control"] == ONE_HOUR
    assert req["messages"][0]["content"][-1]["type"] == "text"
    assert "cache_control" not in req["messages"][0]["content"][-1]


def assign_sides(pairs: list[dict], seed: int) -> list[dict]:
    """Randomise A/B per pair and the order of pairs; returns new dicts."""
    rng = random.Random(seed)
    out = []
    for p in pairs:
        swap = rng.random() < 0.5
        out.append(p | {"swapped": swap})
    rng.shuffle(out)
    return out


def pair_id(a_hash: str, b_hash: str) -> str:
    return "j-" + sha256(f"{a_hash}:{b_hash}".encode())[:20]


def parse_label(message: dict) -> str | None:
    text = "".join(b.get("text", "") for b in message.get("content", []) if b.get("type") == "text")
    try:
        lab = json.loads(text).get("label")
    except (json.JSONDecodeError, AttributeError):
        return None
    return lab if lab in LABELS else None


__all__ = [
    "DISCLAIMER",
    "LABELS",
    "MODEL",
    "SCHEMA",
    "assign_sides",
    "blind",
    "build_request",
    "canonical_bytes",
    "check_request",
    "pair_id",
    "parse_label",
    "prefix_blocks",
]
