"""Turn an archived Council Round 1 request into its replay request.

Two formats exist in the record:

- **Old** (GOV-2026-0006 to 0012, before 2026-10-07): `temperature` set,
  forced tool use (`tool_choice` `any`), no `thinking`, and every tool asks
  for a `reasoning` field. Agora has since stopped asking any model for its
  reasoning in a field (Steward, 2026-10-07; Anthropic's `reasoning_extraction`
  safeguard declines exactly that), so a replay can't send these as they were.
  The adapter brings them to the current Council shape: `reasoning` removed,
  sampling defaults, `tool_choice` auto, adaptive thinking with a summarized
  display, effort `high`, `max_tokens` 16384.
- **New** (GOV-2026-0013 on): only `model` changes, and for Claude Haiku 5.5
  `thinking.display` is set to `summarized` (its default is `omitted`; Opus
  4.6's default is already `summarized`).

Nothing here reads or returns prompt text. The structural diff reports paths
and value shapes, not strings.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}
REPLAY_MAX_TOKENS = 16384
# A property name that asks a model for its reasoning, under any name
# (CLAUDE.md, "No field asks a model for its reasoning").
_FORBIDDEN_PROP = re.compile(r"reason|rationale|think|thought|scratch|chain_of|^trace$", re.I)
SAMPLING_KEYS = ("temperature", "top_p", "top_k")


class AdapterError(ValueError):
    pass


def canonical_bytes(body: dict) -> bytes:
    """The bytes we hash and send: compact, key order as built, UTF-8 as is.

    Agora's council writes its requests the same way, so a dump that is
    parsed and re-serialised here reproduces its original bytes.
    """
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --- property order -------------------------------------------------------


def restore_property_order(node: Any) -> int:
    """Put every schema's `properties` back in declaration order.

    Agora's archive stores prompts as Postgres jsonb, which does not keep
    object key order. `required` is an array, so it does, and it lists the
    fields in the order they were declared. Under strict tool use and
    structured outputs that order is the order the model writes the fields in
    (statement before decision), so it must survive the round trip. Returns
    how many `properties` objects changed order.
    """
    changed = 0
    if isinstance(node, dict):
        props, req = node.get("properties"), node.get("required")
        if isinstance(props, dict) and isinstance(req, list):
            ordered = {k: props[k] for k in req if k in props}
            ordered.update({k: v for k, v in props.items() if k not in ordered})
            if list(ordered) != list(props):
                changed += 1
            node["properties"] = ordered
        for v in node.values():
            changed += restore_property_order(v)
    elif isinstance(node, list):
        for v in node:
            changed += restore_property_order(v)
    return changed


# --- format detection -----------------------------------------------------


def _tool_props(req: dict) -> list[tuple[str, list[str]]]:
    return [
        (t.get("name", "?"), list((t.get("input_schema") or {}).get("properties", {})))
        for t in req.get("tools", [])
    ]


def detect_format(req: dict) -> str:
    """'old' or 'new'. Raises on a request that is a mixture of both."""
    forced = (req.get("tool_choice") or {}).get("type") in ("any", "tool")
    signs = {
        "temperature": "temperature" in req,
        "forced_tool_choice": forced,
        "no_thinking": "thinking" not in req,
        "reasoning_property": any("reasoning" in props for _, props in _tool_props(req)),
    }
    if all(signs.values()):
        return "old"
    if not any(signs.values()):
        return "new"
    raise AdapterError(f"request is neither old nor new format: {signs}")


# --- schema checks --------------------------------------------------------


def _schemas(req: dict) -> list[tuple[str, Any]]:
    out = [(f"tools[{t.get('name')}]", t.get("input_schema")) for t in req.get("tools", [])]
    fmt = (req.get("output_config") or {}).get("format")
    if fmt:
        out.append(("output_config.format", fmt.get("schema")))
    return out


def _walk_props(node: Any, path: str, out: list[str]) -> None:
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            out.extend(f"{path}/properties/{k}" for k in props)
        for k, v in node.items():
            _walk_props(v, f"{path}/{k}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_props(v, f"{path}[{i}]", out)


def property_paths(req: dict) -> list[str]:
    out: list[str] = []
    for name, schema in _schemas(req):
        _walk_props(schema, name, out)
    return out


def forbidden_properties(req: dict) -> list[str]:
    """Paths of any property, in any tool or output schema, that asks for reasoning."""
    return [p for p in property_paths(req) if _FORBIDDEN_PROP.search(p.rsplit("/", 1)[1])]


def ref_paths(req: dict) -> list[str]:
    """Paths of any `$ref` or `$defs` in a tool or output schema."""
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("$ref", "$defs", "definitions"):
                    found.append(f"{path}/{k}")
                walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    for name, schema in _schemas(req):
        walk(schema, name)
    return found


def cache_breakpoints(req: dict) -> list[tuple[str, dict]]:
    """Every cache_control in render order (tools, system, messages)."""
    out = []
    for i, t in enumerate(req.get("tools", [])):
        if "cache_control" in t:
            out.append((f"tools[{i}]", t["cache_control"]))
    system = req.get("system")
    if isinstance(system, list):
        for i, b in enumerate(system):
            if "cache_control" in b:
                out.append((f"system[{i}]", b["cache_control"]))
    for mi, m in enumerate(req.get("messages", [])):
        content = m.get("content")
        if isinstance(content, list):
            for bi, b in enumerate(content):
                if isinstance(b, dict) and "cache_control" in b:
                    out.append((f"messages[{mi}].content[{bi}]", b["cache_control"]))
    return out


# --- the adapter ----------------------------------------------------------


@dataclass
class Adapted:
    body: dict
    fmt: str
    notes: list[str] = field(default_factory=list)

    @property
    def payload(self) -> bytes:
        return canonical_bytes(self.body)


def is_haiku_55(model: str) -> bool:
    return model.startswith("claude-haiku-5-5")


def adapt(original: dict, model: str) -> Adapted:
    """Build the replay request for `model` from an original Round 1 request.

    `original` must already have its property order restored if it came from
    the jsonb archive. It is not modified.
    """
    req = copy.deepcopy(original)
    fmt = detect_format(req)
    notes: list[str] = []
    req.pop("stream", None)  # a transport flag; the Batches API takes none

    if fmt == "old":
        for t in req.get("tools", []):
            schema = t.get("input_schema") or {}
            props = schema.get("properties", {})
            if "reasoning" in props:
                del props["reasoning"]
            if "reasoning" in schema.get("required", []):
                schema["required"] = [r for r in schema["required"] if r != "reasoning"]
        for k in SAMPLING_KEYS:
            req.pop(k, None)
        # disable_parallel_tool_use is documented to work with `auto` (at most
        # one call), so it is kept: the seat still takes one action per turn.
        tc = {"type": "auto"}
        if (original.get("tool_choice") or {}).get("disable_parallel_tool_use"):
            tc["disable_parallel_tool_use"] = True
        req["tool_choice"] = tc
        req["thinking"] = {"type": "adaptive", "display": "summarized"}
        oc = dict(req.get("output_config") or {})
        oc["effort"] = "high"
        req["output_config"] = oc
        req["max_tokens"] = REPLAY_MAX_TOKENS
        notes.append("old format: reasoning removed, sampling defaults, auto, adaptive/high")

    req["model"] = model
    if fmt == "new" and is_haiku_55(model):
        req["thinking"] = {**req["thinking"], "display": "summarized"}

    system = req.get("system")
    if not isinstance(system, list) or not system:
        raise AdapterError("system must be a non-empty list of blocks")
    if system[-1].get("cache_control") != ONE_HOUR:
        system[-1]["cache_control"] = dict(ONE_HOUR)
        notes.append("last system block cache_control set to 1h")

    check(req)
    return Adapted(body=req, fmt=fmt, notes=notes)


def check(req: dict) -> None:
    """Invariants every replay request must meet before it may be sent."""
    problems = []
    if bad := forbidden_properties(req):
        problems.append(f"reasoning-like properties: {bad}")
    if refs := ref_paths(req):
        problems.append(f"$ref/$defs in schema: {refs}")
    if (req.get("tool_choice") or {}).get("type") in ("any", "tool"):
        problems.append("forced tool_choice")
    if is_haiku_55(req.get("model", "")):
        if present := [k for k in SAMPLING_KEYS if k in req]:
            problems.append(f"sampling parameters on Haiku 5.5: {present}")
        if (req.get("thinking") or {}).get("display") != "summarized":
            problems.append("Haiku 5.5 without thinking.display summarized")
    if "stream" in req:
        problems.append("stream set")
    bps = cache_breakpoints(req)
    if len(bps) > 4:
        problems.append(f"{len(bps)} cache breakpoints (max 4)")
    if (req.get("system") or [{}])[-1].get("cache_control") != ONE_HOUR:
        problems.append("last system block is not cached for 1h")
    if problems:
        raise AdapterError("; ".join(problems))


# --- structural diff ------------------------------------------------------


def _shape(v: Any) -> Any:
    """A value's shape, safe to log: scalars as is, text as length and hash."""
    if isinstance(v, str):
        if len(v) <= 40 and "\n" not in v:
            return v
        return f"<str len={len(v)} sha256={sha256(v.encode())[:12]}>"
    if isinstance(v, dict):
        return f"<object keys={len(v)}>"
    if isinstance(v, list):
        return f"<array len={len(v)}>"
    return v


def structural_diff(before: Any, after: Any, path: str = "") -> list[dict]:
    """Paths that differ between two requests, with value shapes only.

    Reports key-order changes in `properties` objects, since under strict
    decoding that order is the order fields are written in.
    """
    out: list[dict] = []
    if type(before) is not type(after):
        out.append(
            {"op": "changed", "path": path, "before": _shape(before), "after": _shape(after)}
        )
    elif isinstance(before, dict):
        for k in before:
            if k not in after:
                out.append({"op": "removed", "path": f"{path}/{k}", "before": _shape(before[k])})
        for k in after:
            if k not in before:
                out.append({"op": "added", "path": f"{path}/{k}", "after": _shape(after[k])})
            else:
                out.extend(structural_diff(before[k], after[k], f"{path}/{k}"))
        common_b = [k for k in before if k in after]
        common_a = [k for k in after if k in before]
        if path.endswith("/properties") and common_b != common_a:
            out.append({"op": "reordered", "path": path, "before": common_b, "after": common_a})
    elif isinstance(before, list):
        if len(before) != len(after):
            out.append(
                {"op": "changed", "path": path, "before": _shape(before), "after": _shape(after)}
            )
        else:
            for i, (b, a) in enumerate(zip(before, after, strict=True)):
                out.extend(structural_diff(b, a, f"{path}[{i}]"))
    elif before != after:
        out.append(
            {"op": "changed", "path": path, "before": _shape(before), "after": _shape(after)}
        )
    return out


# --- token accounting helpers --------------------------------------------


def prefix_chars(req: dict) -> tuple[int, int]:
    """(chars up to and including the last cache breakpoint, total chars).

    Render order is tools, system, messages. Used to split a recorded token
    count into its cached prefix and the rest, for cost estimates.
    """
    pieces: list[tuple[int, bool]] = []
    for t in req.get("tools", []):
        pieces.append((len(canonical_bytes(t)), "cache_control" in t))
    system = req.get("system")
    if isinstance(system, list):
        for b in system:
            pieces.append((len(canonical_bytes(b)), "cache_control" in b))
    for m in req.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                pieces.append(
                    (len(canonical_bytes(b)), isinstance(b, dict) and "cache_control" in b)
                )
        else:
            pieces.append((len(canonical_bytes(content)), False))
    total = sum(n for n, _ in pieces)
    last = max((i for i, (_, bp) in enumerate(pieces) if bp), default=-1)
    return sum(n for n, _ in pieces[: last + 1]), total
