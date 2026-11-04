"""Round 1 votes per decision and seat: the recorded vote beside each replay's.

Reads only enum fields: the recorded Round 1 `vote` from inputs/records and
each manifest's extracted `vote` / `first_action`. Each sample counts once,
by its latest result (a re-run replaces the sample it re-ran).

  uv run scripts/vote_table.py [GOV-2026-0006 ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEATS = ("artist", "philosopher", "lawyer", "engineer")
RUNS = {
    "haiku55": "replay-claude-haiku-5-5",
    "opus46a": "replay-claude-opus-4-6-adapted",
    "opus46": "replay-claude-opus-4-6",
}
SHOW = {"read": "R", "truncated": "T", "refusal": "X", "other": "O"}


def recorded(gov: str) -> dict[str, str]:
    p = ROOT / "inputs" / "records" / f"{gov}.r1.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())["data"]
    r1 = next(r for r in data["rounds"] if r["number"] == 1)
    return {x["role"]: x["vote"] for x in r1["responses"]}


def latest(manifest: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for cid, meta in manifest["samples"].items():
        base = meta.get("rerun_of") or cid
        if meta.get("rerun_of") or base not in out:
            out[base] = meta
    return out


def main() -> int:
    runs = {}
    for name, d in RUNS.items():
        p = ROOT / "out" / d / "manifest.json"
        if p.exists():
            runs[name] = latest(json.loads(p.read_text()))
    govs = sys.argv[1:] or sorted({cid.split("-", 1)[1][:13] for r in runs.values() for cid in r})
    print("R=read T=truncated X=refusal O=other")
    print(f"{'decision':<14}{'seat':<12}{'record':<8}" + "".join(f"{n:<22}" for n in runs))
    for gov in govs:
        rec = recorded(gov)
        for seat in SEATS:
            cells = []
            for samples in runs.values():
                mine = sorted((cid, m) for cid, m in samples.items() if f"-{gov}-{seat}-s" in cid)
                cells.append(
                    " ".join(m.get("vote") or SHOW.get(m.get("first_action"), "?") for _, m in mine)
                    or "-"
                )
            print(
                f"{gov:<14}{seat:<12}{rec.get(seat, '-'):<8}" + "".join(f"{c:<22}" for c in cells)
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
