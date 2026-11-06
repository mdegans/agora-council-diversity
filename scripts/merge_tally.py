"""Invented-record count (OBL-2026-0059), step 3: merge the counters' tallies.

  uv run scripts/merge_tally.py analysis/invented-records

Reads every `halluc-tally-G*.md` in the folder, joins each case to its source
through `key.csv` (the only step allowed to read the key), and prints the
merged tables in DATA-2026-0001's format: totals by source and by format
(old = GOV-2026-0006 to 0012, current = 0013 to 0018), then per item.

Intervals are 95% percentile bootstraps resampling whole items (10,000
resamples, seed 20261106), as the novelty tables resample items.

`--echo-sensitivity` also prints the totals with G3's shared call S1 counted
as D (one per case that carries it), instead of [bl].

Written by Claude (Opus 5.5) for agora-council-diversity, 2026-11-06.
"""

from __future__ import annotations

import argparse
import csv
import random
import re
from collections import defaultdict
from pathlib import Path

LINE = re.compile(
    r"^(c[0-9a-z]{6})\s*\|\s*(\d{4})\s*\|\s*([a-z]+)\s*\|\s*"
    r"(\d+) (\d+) (\d+) (\d+) (\d+) (\d+)\s*\|(.*)$"
)
NAMES = {
    "haiku55": "Haiku 5.5 replay",
    "opus46a": "Opus 4.6 adapted replay",
    "recorded": "Opus 4.6 recorded",
}
OLD = {f"GOV-2026-{n:04d}" for n in range(6, 13)}
REPS = 10_000
SEED = 20261106


def load(folder: Path, echo: bool) -> tuple[dict, dict]:
    key = {r["case_id"]: r for r in csv.DictReader(open(folder / "key.csv"))}
    rows = {}
    for f in sorted(folder.glob("halluc-tally-G*.md")):
        group = f.stem.rsplit("-", 1)[1]
        for line in f.read_text().splitlines():
            m = LINE.match(line)
            if not m:
                continue
            a, b, c, cmin, d, e = map(int, m.groups()[3:9])
            if echo and group == "G3" and "[bl] S1" in m.group(10):
                d += 1
            rows[m.group(1)] = dict(group=group, A=a, B=b, C=c, Cmin=cmin, D=d, E=e)
    missing = sorted(set(key) - set(rows))
    unknown = sorted(set(rows) - set(key))
    if missing or unknown:
        raise SystemExit(f"missing {missing}; not in key {unknown}")
    return key, rows


def items_excl(r: dict) -> int:
    return r["A"] + r["B"] + (r["C"] - r["Cmin"]) + r["D"] + r["E"]


def stats(rows: dict, cids: list[str]) -> dict:
    t = defaultdict(int)
    for c in cids:
        r = rows[c]
        for k in ("A", "B", "C", "Cmin", "D", "E"):
            t[k] += r[k]
        x = items_excl(r)
        t["items_x"] += x
        t["hit_x"] += x > 0
        t["hit_i"] += x + r["Cmin"] > 0
    t["n"] = len(cids)
    return t


def boot(key: dict, rows: dict, cids: list[str], metric) -> tuple[float, float]:
    by_item = defaultdict(list)
    for c in cids:
        by_item[key[c]["item"]].append(c)
    items = sorted(by_item)
    rng = random.Random(SEED)
    vals = sorted(
        metric([c for it in (rng.choice(items) for _ in items) for c in by_item[it]])
        for _ in range(REPS)
    )
    return vals[int(0.025 * REPS)], vals[int(0.975 * REPS) - 1]


def totals(key: dict, rows: dict) -> None:
    def share(cids):
        return stats(rows, cids)["hit_x"] / len(cids)

    def rate(cids):
        return stats(rows, cids)["items_x"] / len(cids)

    print(
        "| source | items | n | >=1 item excl C-minor [95%] | >=1 incl C-minor"
        " | A | B | C (minor) | D | E | items excl C-minor per position [95%] |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    formats = [
        ("all", lambda it: True),
        ("old (0006-0012)", lambda it: it in OLD),
        ("current (0013-0018)", lambda it: it not in OLD),
    ]
    for src, name in NAMES.items():
        for fmt, sel in formats:
            cids = [c for c in rows if key[c]["source"] == src and sel(key[c]["item"])]
            if not cids or (
                fmt != "all" and len(cids) == len([c for c in rows if key[c]["source"] == src])
            ):
                continue
            t = stats(rows, cids)
            n = t["n"]
            lo, hi = boot(key, rows, cids, share)
            rlo, rhi = boot(key, rows, cids, rate)
            print(
                f"| {name} | {fmt} | {n} | {t['hit_x']} ({t['hit_x'] / n:.0%})"
                f" [{lo:.0%}, {hi:.0%}] | {t['hit_i']} | {t['A']} | {t['B']}"
                f" | {t['C']} ({t['Cmin']}) | {t['D']} | {t['E']}"
                f" | {t['items_x'] / n:.2f} [{rlo:.2f}, {rhi:.2f}] |"
            )


def per_item(key: dict, rows: dict) -> None:
    print("| item | " + " | ".join(NAMES.values()) + " |")
    print("|---|---|---|---|")
    for item in sorted({key[c]["item"] for c in rows}):
        cells = []
        for src in NAMES:
            cids = [c for c in rows if key[c]["source"] == src and key[c]["item"] == item]
            if not cids:
                cells.append("—")
                continue
            t = stats(rows, cids)
            cells.append(f"{t['hit_x']}/{t['n']} ({t['items_x']})")
        print(f"| {item} | " + " | ".join(cells) + " |")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", type=Path)
    ap.add_argument("--echo-sensitivity", action="store_true")
    args = ap.parse_args()
    key, rows = load(args.folder, echo=False)
    print(f"cases tallied: {len(rows)} of {len(key)}\n")
    totals(key, rows)
    print("\nPer item: positions with >=1 item excl C-minor / positions (items excl C-minor)\n")
    per_item(key, rows)
    if args.echo_sensitivity:
        key, rows = load(args.folder, echo=True)
        print("\nWith G3-S1 counted as D:\n")
        totals(key, rows)


if __name__ == "__main__":
    main()
