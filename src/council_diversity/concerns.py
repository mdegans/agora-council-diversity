"""Positions on the record, split into concerns (PLAN.md, "Semantic measures").

What is compared is only what a seat puts on the record in Round 1: its
`position` and its `questions`. The recorded `rationale` is not compared:
under the old format it was the `reasoning` field, and since 2026-10-07 it is
the thinking summary, and the REC keeps thinking out of the comparison.

Splitting (PLAN.md step 1, mechanical, no model involved):
- the position is split into paragraphs at blank lines;
- a paragraph under 40 words is joined to the paragraph before it (the first
  one, having nothing before it, is joined to the one after);
- each question is a concern of its own.

Nothing here prints or returns text to a caller that logs it; scripts only
write the concerns to files under out/ and report counts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SEATS = ("artist", "philosopher", "lawyer", "engineer")
MIN_WORDS = 40
VOTES = ("yes", "no", "abstain", "defer")

RUNS = {
    "haiku55": "replay-claude-haiku-5-5",
    "opus46a": "replay-claude-opus-4-6-adapted",
    "opus46": "replay-claude-opus-4-6",
}
MODEL_OF_RUN = {
    "haiku55": "claude-haiku-5-5",
    "opus46a": "claude-opus-4-6",
    "opus46": "claude-opus-4-6",
    "recorded": "claude-opus-4-6",
}


@dataclass
class Position:
    source: str  # "recorded" or a run name
    decision: str
    seat: str
    sample: str  # "rec" or "s01".. ; the latest result for that sample
    vote: str | None
    ready_to_vote: bool | None
    concerns: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)  # "para" or "question"

    @property
    def key(self) -> str:
        return f"{self.source}/{self.decision}/{self.seat}/{self.sample}"


def _norm(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s).strip()


def split_paragraphs(text: str, min_words: int = MIN_WORDS) -> list[str]:
    paras = [_norm(p) for p in re.split(r"\n\s*\n", text or "")]
    paras = [p for p in paras if p]
    out: list[str] = []
    for p in paras:
        if out and len(p.split()) < min_words:
            out[-1] = out[-1] + "\n" + p
        else:
            out.append(p)
    # A short first paragraph has nothing before it: join it to the next.
    if len(out) > 1 and len(out[0].split()) < min_words:
        out[1] = out[0] + "\n" + out[1]
        out.pop(0)
    return out


# Sensitivity variant, not the plan's rule: 21 of 120 Claude Haiku 5.5
# positions have no line breaks at all, so the plan's rule makes each one a
# single concern of up to ~800 words. The "chunk" variant cuts any paragraph
# over CHUNK_MAX words at sentence ends into pieces of at least MIN_WORDS
# words, aiming at CHUNK_TARGET (the recorded median concern length).
CHUNK_MAX = 150
CHUNK_TARGET = 65
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'*\d])")


def chunk_paragraph(p: str) -> list[str]:
    if len(p.split()) <= CHUNK_MAX:
        return [p]
    out: list[str] = []
    cur: list[str] = []
    for sent in _SENT.split(p):
        cur.append(sent)
        if len(" ".join(cur).split()) >= CHUNK_TARGET:
            out.append(" ".join(cur))
            cur = []
    if cur:
        tail = " ".join(cur)
        if out and len(tail.split()) < MIN_WORDS:
            out[-1] = out[-1] + " " + tail
        else:
            out.append(tail)
    return out


def split_position(
    position: str, questions: list[str] | None, variant: str = "para"
) -> tuple[list[str], list[str]]:
    paras = split_paragraphs(position)
    if variant == "chunk":
        paras = [c for p in paras for c in chunk_paragraph(p)]
    elif variant != "para":
        raise ValueError(variant)
    # Since 2026-10-17 a question may be addressed to a seat ({for, question});
    # only the question is kept, so the addressee can't identify anything.
    qs = [q.get("question") if isinstance(q, dict) else q for q in (questions or [])]
    qs = [_norm(q) for q in qs if isinstance(q, str) and _norm(q)]
    return paras + qs, ["para"] * len(paras) + ["question"] * len(qs)


def recorded_positions(records_dir: Path, variant: str = "para") -> list[Position]:
    out = []
    for p in sorted(records_dir.glob("GOV-*.r1.json")):
        gov = p.name[:13]
        data = json.loads(p.read_text())["data"]
        r1 = next(r for r in data["rounds"] if r["number"] == 1)
        for x in r1["responses"]:
            c, k = split_position(x.get("position") or "", x.get("questions"), variant)
            out.append(
                Position(
                    "recorded", gov, x["role"], "rec", x.get("vote"), x.get("ready_to_vote"), c, k
                )
            )
    return out


def latest_samples(manifest: dict) -> dict[str, dict]:
    """One entry per sample: a re-run replaces the sample it re-ran."""
    out: dict[str, dict] = {}
    for cid, meta in manifest["samples"].items():
        base = meta.get("rerun_of") or cid
        if meta.get("rerun_of") or base not in out:
            out[base] = meta
    return out


def _submission(message: dict) -> dict | None:
    for b in message.get("content", []):
        if b.get("type") == "tool_use" and b.get("name") == "position":
            return b.get("input") or {}
    text = "".join(b.get("text", "") for b in message.get("content", []) if b.get("type") == "text")
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and parsed.get("submission") == "position":
        return parsed
    return None


def replay_positions(
    out_dir: Path, run: str, variant: str = "para"
) -> tuple[list[Position], list[dict]]:
    """Positions from a run, and a status row (enum fields only) per sample."""
    manifest = json.loads((out_dir / RUNS[run] / "manifest.json").read_text())
    positions, status = [], []
    for base, meta in sorted(latest_samples(manifest).items()):
        stem = base.split("-", 1)[1]
        gov, rest = stem[:13], stem[14:]
        seat, n = rest.rsplit("-s", 1)
        row = {
            "run": run,
            "decision": gov,
            "seat": seat,
            "sample": f"s{int(n):02d}",
            "first_action": meta.get("first_action"),
            "tool": meta.get("tool"),
            "vote": meta.get("vote"),
            "rerun": bool(meta.get("rerun_of")),
        }
        status.append(row)
        if meta.get("first_action") != "position":
            continue
        msg = json.loads(Path(meta["response_file"]).read_bytes())["result"]["message"]
        sub = _submission(msg)
        if sub is None:
            row["first_action"] = "position-unparsed"
            continue
        c, k = split_position(sub.get("position") or "", sub.get("questions"), variant)
        vote = sub.get("vote") if sub.get("vote") in VOTES else None
        positions.append(Position(run, gov, seat, row["sample"], vote, None, c, k))
    return positions, status
