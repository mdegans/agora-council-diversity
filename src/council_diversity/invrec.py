"""Invented-record count (OBL-2026-0059), step 1: case files and candidates.

Mirrors DATA-2026-0001's hand count (candidate-interview-2026-10/analysis/):
one case file per position, the rubric verbatim, mechanical candidates
(ids not in the inputs, the quote locator, citations) and a tally per group.

What a case file carries is built from an **allowlist**: text, tool calls
(with only the input fields named in ``TOOL_INPUT_KEYS``) and tool results.
Every other content block and every other input field is dropped without
being read, so the models' thinking, and the free-text field the old Council
tools carried beside a position, never reach a case file. Nothing here prints
prompt or response text; the CLI reports counts.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import secrets
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SEATS = ("artist", "philosopher", "lawyer", "engineer")
SEAT_NAME = {s: f"The {s.capitalize()}" for s in SEATS}

# Kept from a tool call's input. Any other field is dropped unread.
TOOL_INPUT_KEYS = (
    "position",
    "questions",
    "vote",
    "comment_numbers",
    "comment_ids",
    "id",
    "detail",
    "number",
    "round",
)
# Kept from a structured (text JSON) answer.
ANSWER_KEYS = ("position", "questions", "vote")
KEPT_BLOCKS = ("text", "tool_use", "tool_result")

# The sittings' wire DATA sets (directory inside each set). GOV-2026-0018 has
# none: its Round 1 was one request per seat, published in this repo
# (inputs/GOV-2026-0018/<seat>.json), and its responses are the Council's
# request dumps.
SITTING_SETS = {
    "GOV-2026-0006": ("DATA-2026-0017", "council-sitting-gov-2026-0006"),
    "GOV-2026-0007": ("DATA-2026-0018", "council-sitting-gov-2026-0007"),
    "GOV-2026-0008": ("DATA-2026-0019", "council-sitting-gov-2026-0008"),
    "GOV-2026-0009": ("DATA-2026-0020", "council-sitting-gov-2026-0009"),
    "GOV-2026-0010": ("DATA-2026-0021", "council-sitting-gov-2026-0010"),
    "GOV-2026-0011": ("DATA-2026-0022", "council-sitting-gov-2026-0011"),
    "GOV-2026-0012": ("DATA-2026-0023", "council-sitting-gov-2026-0012"),
    "GOV-2026-0013": ("DATA-2026-0024", "council-sitting-gov-2026-0013"),
    "GOV-2026-0014": ("DATA-2026-0008", "council-sitting-gov-2026-0014"),
    "GOV-2026-0015": ("DATA-2026-0009", "council-sitting-gov-2026-0015"),
    "GOV-2026-0016": ("DATA-2026-0010", "council-sitting-gov-2026-0016"),
    "GOV-2026-0017": ("DATA-2026-0011", "council-sitting-gov-2026-0017"),
}
GOV18 = "GOV-2026-0018"
GOV18_MEETING = "b5e374b0-c7e2-404e-95e7-2dd2ee110d13"
ITEMS = tuple(sorted(SITTING_SETS)) + (GOV18,)

# Replay runs: label in the key -> out/ directory. "recorded" is the third source.
RUNS = {
    "haiku55": "replay-claude-haiku-5-5",
    "opus46a": "replay-claude-opus-4-6-adapted",
}
SOURCE_MODEL = {
    "haiku55": "claude-haiku-5-5",
    "opus46a": "claude-opus-4-6",
    "recorded": "claude-opus-4-6",
}


# --- loading -----------------------------------------------------------------


@dataclass
class Call:
    who: str
    request: dict  # the Messages API body
    response: dict | None  # the response message, if recorded
    path: str


def _archive_calls(d: Path) -> list[Call]:
    calls = []
    for p in sorted((d / "archive").glob("*.json")):
        if p.name.endswith(".response.json"):
            continue
        row = json.loads(p.read_bytes())
        if not isinstance(row.get("prompt"), dict):
            continue
        rp = p.with_name(p.name[:-5] + ".response.json")
        resp = None
        if rp.exists():
            resp = json.loads(rp.read_bytes())
        elif isinstance(row.get("response"), str):
            try:
                resp = json.loads(row["response"])
            except json.JSONDecodeError:
                resp = None
        elif isinstance(row.get("response"), dict):
            resp = row["response"]
        calls.append(Call(row.get("role", ""), row["prompt"], resp, p.name))
    return calls


_INDEX_ROW = re.compile(r"^\| `([^`]+\.request\.json)` \| ([A-Za-z]+) \|")


def _wire_calls(d: Path) -> list[Call]:
    who = {}
    for line in (d / "index.md").read_text().splitlines():
        m = _INDEX_ROW.match(line)
        if m:
            who[m.group(1)] = m.group(2).lower()
    calls = []
    for p in sorted((d / "wire").glob("*.request.json")):
        rp = p.with_name(p.name.replace(".request.json", ".response.json"))
        resp = json.loads(rp.read_bytes()) if rp.exists() else None
        calls.append(Call(who.get(p.name, ""), json.loads(p.read_bytes()), resp, p.name))
    return calls


def sitting_calls(data_root: Path, item: str) -> list[Call]:
    ds, sub = SITTING_SETS[item]
    d = data_root / ds / sub
    return _wire_calls(d) if (d / "wire").is_dir() else _archive_calls(d)


def gov18_calls(repo: Path, dumps_dir: Path) -> list[Call]:
    calls = []
    for seat in SEATS:
        req = json.loads((repo / "inputs" / GOV18 / f"{seat}.json").read_bytes())["prompt"]
        dumps = sorted((dumps_dir / GOV18_MEETING).glob(f"*.{seat}.round1.*.response.json"))
        if len(dumps) != 1:
            raise ValueError(f"{GOV18} {seat}: expected one Round 1 response, found {len(dumps)}")
        calls.append(Call(seat, req, json.loads(dumps[0].read_bytes()), dumps[0].name))
    return calls


# --- allowlisted views ---------------------------------------------------------


def _text_json(text: str) -> dict | None:
    try:
        v = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def is_position(block: dict) -> bool:
    if block.get("type") == "tool_use" and block.get("name") == "position":
        return True
    if block.get("type") == "text":
        v = _text_json(block.get("text", ""))
        return bool(v) and v.get("submission") == "position"
    return False


def answer_of(block: dict) -> dict:
    """Position, questions and vote from a position block, nothing else."""
    if block.get("type") == "tool_use":
        src = block.get("input") or {}
    else:
        src = _text_json(block.get("text", "")) or {}
    return {k: src[k] for k in ANSWER_KEYS if k in src}


def kept_blocks(content: Any) -> list[dict]:
    """The allowlisted view of a message's content."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for b in content or []:
        t = b.get("type")
        if t == "text":
            out.append({"type": "text", "text": b.get("text", "")})
        elif t == "tool_use":
            inp = b.get("input") or {}
            out.append(
                {
                    "type": "tool_use",
                    "name": b.get("name"),
                    "input": {k: inp[k] for k in TOOL_INPUT_KEYS if k in inp},
                }
            )
        elif t == "tool_result":
            c = b.get("content")
            if isinstance(c, str):
                texts = [c]
            else:
                texts = [x.get("text", "") for x in (c or []) if x.get("type") == "text"]
            out.append({"type": "tool_result", "texts": texts})
    return out


def dropped_input_fields(content: Any) -> int:
    """How many tool-input fields the allowlist drops (a count, never a name)."""
    n = 0
    if isinstance(content, list):
        for b in content:
            if b.get("type") == "tool_use":
                n += sum(1 for k in (b.get("input") or {}) if k not in TOOL_INPUT_KEYS)
    return n


@dataclass
class Loop:
    """A seat's Round 1: the request it answered from, and its answer."""

    system: list[str]
    tools: list[tuple[str, str]]  # (name, description)
    messages: list[dict]  # allowlisted, up to but excluding the answer turn
    answer: dict
    path: str
    calls_in_loop: int = 0


def _request_view(req: dict) -> tuple[list[str], list[tuple[str, str]]]:
    system = req.get("system") or []
    if isinstance(system, str):
        system = [system]
    else:
        system = [b.get("text", "") for b in system if b.get("type") == "text"]
    tools = [(t.get("name", ""), t.get("description", "")) for t in req.get("tools") or []]
    return system, tools


def recorded_loop(calls: list[Call], seat: str) -> Loop:
    """The first conversation of ``seat`` in which it submits a position, cut there."""
    seat_calls = [c for c in calls if c.who == seat]
    for c in seat_calls:
        conv = list(c.request.get("messages") or [])
        if c.response is not None:
            conv.append({"role": "assistant", "content": c.response.get("content") or []})
        for i, m in enumerate(conv):
            if m.get("role") != "assistant" or isinstance(m.get("content"), str):
                continue
            pos = [b for b in m["content"] if is_position(b)]
            if not pos:
                continue
            system, tools = _request_view(c.request)
            first = conv[0]
            n_calls = sum(1 for x in seat_calls if x.request.get("messages", [None])[0] == first)
            return Loop(
                system=system,
                tools=tools,
                messages=[
                    {"role": x["role"], "content": kept_blocks(x["content"])} for x in conv[:i]
                ],
                answer=answer_of(pos[0]),
                path=c.path,
                calls_in_loop=n_calls,
            )
    raise ValueError(f"no position found for {seat}")


@dataclass
class ReplaySample:
    run: str
    item: str
    seat: str
    sample: str  # s01..s03
    rerun: bool
    request: dict
    answer: dict


def latest_samples(manifest: dict) -> dict[str, dict]:
    """One entry per sample: a re-run replaces the sample it re-ran (as concerns.py)."""
    out: dict[str, dict] = {}
    for cid, meta in manifest["samples"].items():
        base = meta.get("rerun_of") or cid
        if meta.get("rerun_of") or base not in out:
            out[base] = meta
    return out


def replay_samples(repo: Path, run: str) -> tuple[list[ReplaySample], Counter]:
    """Positions of a run, by the same rule as concerns.replay_positions."""
    rdir = repo / "out" / RUNS[run]
    manifest = json.loads((rdir / "manifest.json").read_bytes())
    out, status = [], Counter()
    for base, meta in sorted(latest_samples(manifest).items()):
        stem = base.split("-", 1)[1]
        item, rest = stem[:13], stem[14:]
        seat, n = rest.rsplit("-s", 1)
        status[meta.get("first_action")] += 1
        if meta.get("first_action") != "position":
            continue
        resp_path = rdir / item / seat / Path(meta["response_file"]).name
        msg = json.loads(resp_path.read_bytes())["result"]["message"]
        pos = [b for b in msg.get("content", []) if is_position(b)]
        if not pos:
            status["position-unparsed"] += 1
            continue
        rerun = bool(meta.get("rerun_of"))
        req_name = "request.rerun-32k.json" if rerun else "request.json"
        req = json.loads((rdir / item / seat / req_name).read_bytes())
        out.append(ReplaySample(run, item, seat, f"s{int(n):02d}", rerun, req, answer_of(pos[0])))
    return out, status


# --- rendering -----------------------------------------------------------------


def sha12(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


_VERSION = re.compile(r"\*\*Version ([0-9][0-9.]*)")


def classify_system_block(text: str) -> tuple[str, str]:
    """(kind, version label) of a system block: constitution, protocol or council."""
    head = text.lstrip()[:200]
    if head.startswith("# The Constitution"):
        kind = "constitution"
    elif head.startswith("# The Governance Protocol"):
        kind = "governance-protocol"
    else:
        kind = "council-protocol"
    m = _VERSION.search(text[:2000])
    ver = f"v{m.group(1)}" if m and kind != "council-protocol" else ""
    return kind, ver


@dataclass
class Common:
    """Shared texts, written once under common/ and referenced from case files."""

    files: dict[str, str] = field(default_factory=dict)  # relative path -> text
    by_hash: dict[str, str] = field(default_factory=dict)  # sha12 -> relative path
    versions: dict[tuple[str, str], set[str]] = field(default_factory=dict)

    def add(self, kind: str, ver: str, text: str, stem: str | None = None) -> str:
        h = sha12(text)
        if h in self.by_hash:
            return self.by_hash[h]
        seen = self.versions.setdefault((kind, ver), set())
        seen.add(h)
        name = stem or (f"{kind}-{ver}" if ver else f"{kind}-{h}")
        if ver and len(seen) > 1:
            name = f"{name}-{h}"
        rel = f"common/{name}.md"
        self.files[rel] = text
        self.by_hash[h] = rel
        return rel


def item_blocks(requests: list[dict]) -> set[str]:
    """Hashes of first-user-message blocks identical across every seat's request."""
    sets = []
    for r in requests:
        first = (r.get("messages") or [{}])[0]
        sets.append(
            {sha12(b["text"]) for b in kept_blocks(first.get("content")) if b["type"] == "text"}
        )
    return set.intersection(*sets) if sets else set()


def _fmt_input(inp: dict) -> str:
    lines = []
    for k, v in inp.items():
        if isinstance(v, str):
            lines.append(f"- {k}: {v}")
        else:
            lines.append(f"- {k}: {json.dumps(v, ensure_ascii=False)}")
    return "\n".join(lines) if lines else "- (no fields)"


def render_answer(ans: dict) -> str:
    out = ["## The answer", "", "### Position", "", (ans.get("position") or "").strip(), ""]
    qs = ans.get("questions") or []
    out += ["### Questions", ""]
    if not qs:
        out += ["(none)", ""]
    for i, q in enumerate(qs, 1):
        if isinstance(q, dict):
            to = q.get("for")
            text = q.get("question") or ""
            out.append(f"{i}. (to {to}) {text}" if to else f"{i}. {text}")
        else:
            out.append(f"{i}. {q}")
    if qs:
        out.append("")
    out += ["### Vote", "", str(ans.get("vote") if ans.get("vote") is not None else "(none)"), ""]
    return "\n".join(out)


def answer_text(ans: dict) -> str:
    """Plain text of the answer, for the candidate checks."""
    parts = [ans.get("position") or ""]
    for q in ans.get("questions") or []:
        parts.append((q.get("question") or "") if isinstance(q, dict) else str(q))
    return "\n\n".join(parts)


@dataclass
class Case:
    case_id: str
    source: str  # haiku55 | opus46a | recorded
    item: str
    seat: str
    sample: str  # s01.. or rec
    rerun: bool
    refs: list[str]  # common files the inputs reference
    constitution: str  # common path of the Constitution in force
    inputs_inline: str  # text of the inputs that is not in common/
    answer: dict
    body: str = ""


def _render_first_user(
    blocks: list[dict], shared: set[str], item_ref: str
) -> tuple[str, list[str]]:
    out, refs, run = [], [], []

    def flush():
        if run:
            out.append(
                f"*[{item_ref}, blocks {run[0]}–{run[-1]}: the item as every seat saw it]*"
                if len(run) > 1
                else f"*[{item_ref}, block {run[0]}]*"
            )
            out.append("")
            run.clear()

    k = 0
    for b in blocks:
        if b["type"] != "text":
            continue
        k += 1
        if sha12(b["text"]) in shared:
            run.append(k)
            if item_ref not in refs:
                refs.append(item_ref)
        else:
            flush()
            out += [b["text"], ""]
    flush()
    return "\n".join(out), refs


def render_turn(m: dict, n: int) -> str:
    out = []
    for b in m["content"]:
        if b["type"] == "text":
            out += [b["text"], ""]
        elif b["type"] == "tool_use":
            out += [f"**Tool call: `{b['name']}`**", "", _fmt_input(b["input"]), ""]
        elif b["type"] == "tool_result":
            out += ["**Tool result:**", ""]
            for t in b["texts"]:
                out += [t, ""]
    who = "The seat" if m["role"] == "assistant" else "Program"
    return f"### Turn {n}: {who}\n\n" + "\n".join(out)


def system_refs(system: list[str], common: Common) -> tuple[list[str], str]:
    refs, cons = [], ""
    for t in system:
        kind, ver = classify_system_block(t)
        rel = common.add(kind, ver, t)
        refs.append(rel)
        if kind == "constitution":
            cons = rel
    return refs, cons


def tools_ref(tools: list[tuple[str, str]], common: Common) -> str:
    text = "# Tools the seat was offered (names and descriptions)\n\n" + "\n\n".join(
        f"## `{n}`\n\n{d}" for n, d in tools
    )
    return common.add("tools", "", text + "\n")


def item_ref(item: str, first_blocks: list[dict], shared: set[str], common: Common) -> str:
    texts = [b["text"] for b in first_blocks if b["type"] == "text"]
    body = [f"# {item}: the item as the seats saw it", ""]
    body.append(
        "The blocks of the first user message that were identical for all four seats, "
        "numbered by their place in that message. A case file names the blocks it used.\n"
    )
    for k, t in enumerate(texts, 1):
        if sha12(t) in shared:
            body += [f"## Block {k}", "", t, ""]
    return common.add("items", "", "\n".join(body), stem=f"items/{item}")


def build_case_body(
    case: Case, system_rels: list[str], tools_rel: str, first_user: str, loop_rest: str
) -> str:
    refs = "\n".join(f"- `{r}`" for r in system_rels)
    head = [
        f"# Case {case.case_id}",
        "",
        f"Item: {case.item}. Seat: {SEAT_NAME[case.seat]}. "
        f"Constitution in force: `{case.constitution}`.",
        "",
        "## What the seat saw",
        "",
        "### System prompt (in this order; shared texts are in common/)",
        "",
        refs,
        "",
        f"### Tools offered: `{tools_rel}`",
        "",
        "### Turn 1: Program (the first user message)",
        "",
        first_user,
    ]
    if loop_rest:
        head += [loop_rest]
    return "\n".join(head) + "\n" + render_answer(case.answer)


# --- candidates -------------------------------------------------------------------

_ID = re.compile(r"\b(?:GOV|APP|REC|AMD|OBL|DATA|ANC)-\d{4}-\d{1,5}\b")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX8 = re.compile(r"(?<![0-9a-fA-F-])[0-9a-f]{8}(?![0-9a-fA-F-])")


def _norm_id(s: str) -> str:
    p, y, n = s.split("-")
    return f"{p}-{y}-{int(n):04d}"


def id_candidates(answer: str, inputs: str) -> list[tuple[str, int]]:
    """B: ids in the answer that are not in the inputs (first offset of each)."""
    norm_inputs = {_norm_id(m.group(0)) for m in _ID.finditer(inputs)}
    low = inputs.lower()
    out, seen = [], set()
    for m in _ID.finditer(answer):
        s = m.group(0)
        if s in seen:
            continue
        seen.add(s)
        if s not in inputs and _norm_id(s) not in norm_inputs:
            out.append((s, m.start()))
    for rx in (_UUID, _HEX8):
        for m in rx.finditer(answer):
            s = m.group(0)
            if s in seen or not re.search(r"\d", s) or not re.search(r"[a-f]", s.lower()):
                # an all-letter or all-digit 8-run is a word or a number, not an id
                continue
            seen.add(s)
            if s.lower() not in low:
                out.append((s, m.start()))
    return out


# The quote locator: norm() and frags() and the quote/blockquote extraction are
# DATA-2026-0001's analysis/tools/qcheck.py, unchanged (see tests). Only the
# corpus differs: here it is the case's inputs and the common texts it uses.


def qnorm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("—", "-").replace("–", "-").replace("—", "-").replace("§", "s ")
    s = re.sub(r"[*_`>#]", "", s)
    s = re.sub(r"\\", "", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9\'\- ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def qfrags(q: str) -> list[str]:
    parts = re.split(r"\.\.\.|…|\[\.\.\.\]|\[…\]", q)
    return [p for p in parts if len(qnorm(p).split()) >= 3]


def quotes_in(body: str) -> list[tuple[str, str]]:
    quotes = []
    for m in re.finditer(r'["“]([^"“”\n]{12,600})["”]', body):
        quotes.append(("q", m.group(1)))
    bq: list[str] = []
    for line in body.split("\n") + [""]:
        if line.startswith(">"):
            bq.append(line.lstrip("> ").strip())
        elif bq:
            quotes.append(("bq", " ".join(bq)))
            bq = []
    return quotes


def quote_candidates(answer: str, corpus_norm: str) -> list[dict]:
    """C: quotes whose fragments are not in the corpus, with the nearest text."""
    out = []
    for kind, q in quotes_in(answer):
        fs = qfrags(q)
        if not fs:
            continue
        bad = [f for f in fs if qnorm(f) not in corpus_norm]
        if not bad:
            continue
        nb = qnorm(bad[0])
        words = nb.split()
        best = ""
        key = " ".join(words[:4])
        idx = corpus_norm.find(key)
        if idx < 0:
            key = " ".join(words[-4:])
            idx = corpus_norm.find(key)
            if idx >= 0:
                idx = max(0, idx - len(nb))
        if idx >= 0:
            best = corpus_norm[idx : idx + len(nb) + 40]
        out.append({"kind": kind, "quote": q[:400], "nearest": best[:300]})
    return out


def quotes_checked(answer: str) -> int:
    return sum(1 for _, q in quotes_in(answer) if qfrags(q))


# Citations of the Constitution (E): existence only.
_ROMAN = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
}
_ROMAN_INV = {v: k for k, v in _ROMAN.items()}
_ART = r"\b(?:Articles?|Arts?\.)\s*(?P<art>[IVX]+\b|\d+\b)"
_SEC = r"(?:(?:\s*,?\s*(?:§§?|Section|Sec\.|s\.)\s*|\.)(?P<sec>\d+(?:\.\d+)?))?"
_ITEM = r"(?:\s*,?\s*(?:item|Item)\s+(?P<item>\d+))?"
_CITE = re.compile(_ART + _SEC + _ITEM)
_BARE_SEC = re.compile(r"(?<![\w.])(?:§\s*|Section\s+)(?P<sec>\d+(?:\.\d+)?)")


@dataclass
class Structure:
    """The Constitution's numbering: articles, their sections, and numbered items."""

    items: dict[int, int]  # article -> numbered items directly under it
    sections: dict[int, dict[int, int]]  # article -> section -> numbered items


def constitution_structure(text: str) -> Structure:
    items: dict[int, int] = {}
    sections: dict[int, dict[int, int]] = {}
    art = sec = None
    for line in text.splitlines():
        m = re.match(r"^## Article ([IVX]+)\b", line)
        if m:
            art, sec = _ROMAN.get(m.group(1)), None
            if art:
                items[art] = 0
                sections[art] = {}
            continue
        if line.startswith("## "):
            art = sec = None
            continue
        m = re.match(r"^### Section (\d+)\b", line)
        if m and art:
            sec = int(m.group(1))
            sections[art][sec] = 0
            continue
        m = re.match(r"^(\d+)\. ", line)
        if m and art:
            n = int(m.group(1))
            if sec is None:
                items[art] = max(items[art], n)
            else:
                sections[art][sec] = max(sections[art][sec], n)
    return Structure(items, sections)


def protocol_sections(text: str) -> set[str]:
    out = set()
    for line in text.splitlines():
        m = re.match(r"^#{2,4} (\d+(?:\.\d+)*)\.?\s", line)
        if m:
            out.add(m.group(1))
    return out


def _check(st: Structure, art: int | None, sec: str | None, item: str | None) -> str:
    if art not in st.items:
        return "no such article"
    secs = st.sections[art]
    if sec is None and item is None:
        return "exists"
    if sec is not None:
        s, _, i = sec.partition(".")
        s_n = int(s)
        i_n = int(i) if i else (int(item) if item else None)
        if not secs:
            # An article without sections numbers its items: "§ 6" read as item 6.
            if i:
                return "no such section in that article (it has no sections)"
            return (
                "exists (read as item; the article has no sections)"
                if s_n <= st.items[art]
                else "no such section or item in that article"
            )
        if s_n not in secs:
            return "no such section in that article"
        if i_n is None:
            return "exists"
        return "exists" if i_n <= secs[s_n] else "no such item in that section"
    n = int(item)
    if n <= st.items[art]:
        return "exists"
    return "no such item in that article"


def citation_candidates(answer: str, st: Structure, proto: set[str]) -> list[dict]:
    """E: every Constitution article/section/item citation, and whether it exists."""
    out = []
    spans = []
    for m in _CITE.finditer(answer):
        a = m.group("art")
        n = int(a) if a.isdigit() else _ROMAN.get(a)
        sec, item = m.group("sec"), m.group("item")
        label = f"Art. {_ROMAN_INV.get(n, a) if n else a}"
        if sec:
            label += f" § {sec}"
        if item:
            label += f" item {item}"
        out.append(
            {"cite": m.group(0), "as": label, "status": _check(st, n, sec, item), "at": m.start()}
        )
        spans.append((m.start(), m.end()))
    for m in _BARE_SEC.finditer(answer):
        if any(s <= m.start() < e for s, e in spans):
            continue
        sec = m.group("sec")
        s, _, i = sec.partition(".")
        in_cons = any(
            int(s) in st.sections[a] and (not i or int(i) <= st.sections[a][int(s)])
            for a in st.sections
        )
        in_proto = sec in proto
        if in_cons and in_proto:
            status = (
                "exists (no article named; matches a Constitution section and a Protocol section)"
            )
        elif in_proto:
            status = "exists (no article named; a Protocol section)"
        elif in_cons:
            status = "exists (no article named; matches a section of some article)"
        else:
            status = "no such section (no article named; none in the Constitution or Protocol)"
        out.append({"cite": m.group(0), "as": f"§ {sec}", "status": status, "at": m.start()})
    out.sort(key=lambda x: x["at"])
    return out


def context_of(text: str, at: int, width: int = 80) -> str:
    a = max(0, at - width)
    b = min(len(text), at + width)
    return re.sub(r"\s+", " ", text[a:b]).strip()


def render_candidates(
    case_id: str, answer: str, ids: list, quotes: list, n_quotes: int, cites: list, refs: list[str]
) -> str:
    out = [
        f"# Candidates: case {case_id}",
        "",
        "Mechanical candidates only, not counts (as DATA-2026-0001's counters used "
        "qcheck.py and an id grep). Every count is judged by hand against the rubric. "
        "Inputs searched: the case file's inputs and " + ", ".join(f"`{r}`" for r in refs) + ".",
        "",
        f"## B: ids in the answer not found in the inputs ({len(ids)})",
        "",
    ]
    out += [f"- `{s}` … {context_of(answer, at)}" for s, at in ids] or ["(none)"]
    out += [
        "",
        f"## C: quotes and blockquotes not found verbatim ({len(quotes)} of {n_quotes} checked)",
        "",
    ]
    for q in quotes:
        out += [f"- [{q['kind']}] MISS: {q['quote']}", f"      nearest: {q['nearest']}"]
    if not quotes:
        out.append("(none)")
    missing = [c for c in cites if c["status"].startswith("no such")]
    out += [
        "",
        f"## E: citations ({len(cites)}; {len(missing)} not in the Constitution in force)",
        "",
        "Existence only. Whether a provision says what the answer says it does is "
        "the counter's call.",
        "",
    ]
    for c in cites:
        flag = "**MISSING**" if c["status"].startswith("no such") else "ok"
        out.append(
            f"- {flag} `{c['cite']}` ({c['as']}: {c['status']}) … {context_of(answer, c['at'])}"
        )
    if not cites:
        out.append("(none)")
    return "\n".join(out) + "\n"


# --- ids and groups -----------------------------------------------------------------


def new_case_id(rng: secrets.SystemRandom, taken: set[str]) -> str:
    while True:
        cid = "c" + "".join(rng.choice("0123456789abcdefghjkmnpqrstvwxyz") for _ in range(6))
        if cid not in taken:
            taken.add(cid)
            return cid


def write_key(path: Path, cases: list[Case]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "source", "model", "item", "seat", "sample", "rerun"])
        for c in sorted(cases, key=lambda c: c.case_id):
            w.writerow(
                [c.case_id, c.source, SOURCE_MODEL[c.source], c.item, c.seat, c.sample, c.rerun]
            )


def read_key(path: Path) -> dict[str, dict]:
    with path.open() as f:
        return {r["case_id"]: r for r in csv.DictReader(f)}


# --- candidates from a case file (the build and the tests use this) --------------

ANSWER_HEADING = "\n## The answer\n"
_REF = re.compile(r"(common/[A-Za-z0-9_./-]+\.md)")


def split_case(body: str) -> tuple[str, str]:
    inputs, answer = body.split(ANSWER_HEADING, 1)
    return inputs, answer


def refs_in(text: str) -> list[str]:
    return list(dict.fromkeys(_REF.findall(text)))


def case_candidates(case_id: str, body: str, read) -> tuple[str, dict[str, int]]:
    """The candidates file for one case, from the case file and the common texts.

    ``read(rel)`` returns a common file's text. The answer checked is the case
    file's answer section, as DATA-2026-0001 ran qcheck on the answer files.
    """
    inputs, answer = split_case(body)
    refs = refs_in(inputs)
    corpus = inputs + "\n" + "\n".join(read(r) for r in refs)
    cons = next(r for r in refs if r.startswith("common/constitution-"))
    proto = "\n".join(read(r) for r in refs if r.startswith("common/governance-protocol-"))
    b = id_candidates(answer, corpus)
    cq = quote_candidates(answer, qnorm(corpus))
    nq = quotes_checked(answer)
    e = citation_candidates(answer, constitution_structure(read(cons)), protocol_sections(proto))
    text = render_candidates(case_id, answer, b, cq, nq, e, refs)
    counts = {
        "B": len(b),
        "C": len(cq),
        "C-checked": nq,
        "E-all": len(e),
        "E-missing": sum(1 for x in e if x["status"].startswith("no such")),
    }
    return text, counts
