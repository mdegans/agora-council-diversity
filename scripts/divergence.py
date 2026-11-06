"""Concern-level divergence (REC draft, step 5) and the vote/deferral table.

  uv run scripts/divergence.py concerns   # split positions -> out/analysis (text), data (hashes)
  uv run scripts/divergence.py embed      # Ollama embeddings, cached in out/analysis
  uv run scripts/divergence.py analyze    # floors, novelty, bootstrap -> data/divergence
  uv run scripts/divergence.py votes      # vote / deferral table -> data/divergence

Prints counts and numbers only, never prompt or response text.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from council_diversity.adapter import sha256
from council_diversity.concerns import (
    MODEL_OF_RUN,
    SEATS,
    Position,
    recorded_positions,
    replay_positions,
)
from council_diversity.embed import embed_texts
from council_diversity.embed import manifest as embed_manifest
from council_diversity.novelty import (
    FLOOR_PERCENTILE,
    RESAMPLES,
    SEED,
    best_match,
    bootstrap_clusters,
    bootstrap_diff,
    bootstrap_share,
    floor,
)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
WORK = OUT / "analysis"
DATA = ROOT / "data" / "divergence"
VARIANTS = ("para", "chunk")
REPLAY_RUNS = ("haiku55", "opus46a", "opus46")
OLD_FORMAT = {f"GOV-2026-{n:04d}" for n in range(6, 13)}


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_positions(variant: str) -> tuple[list[Position], list[dict]]:
    pos = recorded_positions(ROOT / "inputs" / "records", variant)
    status = []
    for run in REPLAY_RUNS:
        p, s = replay_positions(OUT, run, variant)
        pos += p
        status += s
    return pos, status


# --- concerns ---------------------------------------------------------------


def cmd_concerns(_: argparse.Namespace) -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    for v in VARIANTS:
        pos, _ = load_positions(v)
        with (WORK / f"concerns-{v}.jsonl").open("w") as f:
            for p in pos:
                f.write(json.dumps({"key": p.key, "concerns": p.concerns, "kinds": p.kinds}) + "\n")
        with (DATA / f"concerns-index-{v}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                ["source", "decision", "seat", "sample", "concern", "kind", "words", "sha256"]
            )
            for p in pos:
                for i, (c, k) in enumerate(zip(p.concerns, p.kinds, strict=True)):
                    w.writerow(
                        [
                            p.source,
                            p.decision,
                            p.seat,
                            p.sample,
                            i,
                            k,
                            len(c.split()),
                            sha256(c.encode()),
                        ]
                    )
        n = Counter(p.source for p in pos)
        c = Counter()
        for p in pos:
            c[p.source] += len(p.concerns)
        print(json.dumps({"variant": v, "positions": n, "concerns": c}))
    return 0


# --- embed ------------------------------------------------------------------


def cmd_embed(_: argparse.Namespace) -> int:
    texts: list[str] = []
    for v in VARIANTS:
        for line in (WORK / f"concerns-{v}.jsonl").open():
            texts += json.loads(line)["concerns"]
    uniq = sorted(set(texts))
    man = embed_manifest()
    man["started_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    embed_texts(uniq, WORK / "embeddings.npz")
    man["finished_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    after = embed_manifest()
    if after["model_digest"] != man["model_digest"]:
        raise RuntimeError("model digest changed during the run")
    man["texts_embedded"] = len(uniq)
    man["texts_sha256_of_sorted_hashes"] = sha256(
        "\n".join(sorted(sha256(t.encode()) for t in uniq)).encode()
    )
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "embedding-manifest.json").write_text(json.dumps(man, indent=1) + "\n")
    print(
        json.dumps(
            {k: man[k] for k in ("ollama_version", "model", "model_digest", "texts_embedded")}
        )
    )
    return 0


# --- analyze ----------------------------------------------------------------


def _vectors(variant: str) -> dict[str, np.ndarray]:
    with np.load(WORK / "embeddings.npz") as z:
        store = {k: z[k] for k in z.files}
    out = {}
    for line in (WORK / f"concerns-{variant}.jsonl").open():
        d = json.loads(line)
        out[d["key"]] = np.stack([store[sha256(c.encode())] for c in d["concerns"]])
    return out


def _model(source: str) -> str:
    return MODEL_OF_RUN[source]


def analyze_variant(variant: str) -> dict:
    pos, _ = load_positions(variant)
    vec = _vectors(variant)
    groups: dict[tuple[str, str, str], list[Position]] = defaultdict(list)
    for p in pos:
        groups[(p.source, p.decision, p.seat)].append(p)

    rows: list[dict] = []

    def compare(comp: str, a: Position, partners: list[Position], unit: str) -> None:
        if not partners:
            return
        B = np.concatenate([vec[q.key] for q in partners])
        owners = [(q, i) for q in partners for i in range(len(q.concerns))]
        sims, idx = best_match(vec[a.key], B)
        for i, (s, j) in enumerate(zip(sims, idx, strict=True)):
            q, qi = owners[j]
            rows.append(
                {
                    "comparison": comp,
                    "decision": a.decision,
                    "seat": a.seat,
                    "unit": unit,
                    "a": a.key,
                    "a_concern": i,
                    "a_kind": a.kinds[i],
                    "b": q.key,
                    "b_concern": qi,
                    "b_seat": q.seat,
                    "sim": float(s),
                    "a_model": _model(a.source),
                    "b_model": _model(q.source),
                }
            )

    decisions = sorted({p.decision for p in pos})
    for g in decisions:
        for seat in SEATS:
            rec = groups.get(("recorded", g, seat), [])
            others = [q for s in SEATS if s != seat for q in groups.get(("recorded", g, s), [])]
            for run in REPLAY_RUNS:
                mine = sorted(groups.get((run, g, seat), []), key=lambda p: p.sample)
                for a in mine:
                    compare(f"{run}_vs_rec_same", a, rec, a.key)
                    compare(f"{run}_vs_rec_others", a, others, a.key)
                    # the reverse: recorded concerns with no match in this answer
                    for r in rec:
                        compare(f"rec_vs_{run}_same", r, [a], a.key)
                    for b in mine:
                        if b is not a:
                            compare(f"{run}_vs_{run}", a, [b], a.key)
            # cross-model with the format held fixed (adapted requests, 0006..0012)
            for a in groups.get(("haiku55", g, seat), []):
                for b in groups.get(("opus46a", g, seat), []):
                    compare("haiku55_vs_opus46a_same", a, [b], a.key)
            for r in rec:
                compare("rec_vs_rec_others", r, others, r.key)

    # noise floors: same model, same run, same item and seat, different samples
    floor_src = {
        "claude-haiku-5-5": ["haiku55_vs_haiku55"],
        "claude-opus-4-6": ["opus46a_vs_opus46a", "opus46_vs_opus46"],
    }
    floors = {}
    for m, comps in floor_src.items():
        sims = [r["sim"] for r in rows if r["comparison"] in comps]
        floors[m] = {
            "p05": floor(sims),
            "n_matches": len(sims),
            "from": comps,
            "p50": float(np.percentile(sims, 50)) if sims else None,
        }
    f_h = floors["claude-haiku-5-5"]["p05"]
    f_o = floors["claude-opus-4-6"]["p05"]
    t_cross = max(f_h, f_o)
    for r in rows:
        t_pair = max(floors[r["a_model"]]["p05"], floors[r["b_model"]]["p05"])
        r["threshold_pair"] = t_pair
        r["novel_pair"] = r["sim"] < t_pair
        r["novel_cross"] = r["sim"] < t_cross

    def units(comp: str, flag: str, g: str | None = None, kind: str | None = None):
        u: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in rows:
            if r["comparison"] != comp or (g and r["decision"] != g):
                continue
            if kind and r["a_kind"] != kind:
                continue
            u[r["unit"]][0] += int(r[flag])
            u[r["unit"]][1] += 1
        return [tuple(v) for v in u.values()]

    comps = sorted({r["comparison"] for r in rows})
    per_item = []
    for comp in comps:
        for g in decisions:
            for flag in ("novel_cross", "novel_pair"):
                u = units(comp, flag, g)
                if not u:
                    continue
                pt, lo, hi = bootstrap_share(u)
                per_item.append(
                    {
                        "comparison": comp,
                        "decision": g,
                        "threshold": flag,
                        "units": len(u),
                        "concerns": sum(t for _, t in u),
                        "novel": sum(n for n, _ in u),
                        "share": pt,
                        "lo95": lo,
                        "hi95": hi,
                    }
                )
    overall = []
    for comp in comps:
        for flag in ("novel_cross", "novel_pair"):
            for subset, keep in (
                ("all", None),
                ("old_format_0006_0012", OLD_FORMAT),
                ("new_format_0013_on", "new"),
            ):
                clusters = {}
                for g in decisions:
                    if keep == "new" and g in OLD_FORMAT:
                        continue
                    if isinstance(keep, set) and g not in keep:
                        continue
                    u = units(comp, flag, g)
                    if u:
                        clusters[g] = u
                if not clusters:
                    continue
                pt, lo, hi = bootstrap_clusters(clusters)
                overall.append(
                    {
                        "comparison": comp,
                        "threshold": flag,
                        "items": subset,
                        "n_items": len(clusters),
                        "concerns": sum(t for u in clusters.values() for _, t in u),
                        "novel": sum(n for u in clusters.values() for n, _ in u),
                        "share": pt,
                        "lo95_over_items": lo,
                        "hi95_over_items": hi,
                    }
                )

    # (a) cross-model vs same-model, per item, same threshold (max of floors)
    contrasts = [
        ("haiku55_vs_rec_same", "haiku55_vs_haiku55", "novel_cross"),
        ("haiku55_vs_opus46a_same", "haiku55_vs_haiku55", "novel_cross"),
        ("haiku55_vs_opus46a_same", "opus46a_vs_opus46a", "novel_cross"),
        ("haiku55_vs_rec_others", "rec_vs_rec_others", "novel_cross"),
        # (b) same model, adapted prompt vs record, against Opus's own floor
        ("opus46a_vs_rec_same", "opus46a_vs_opus46a", "novel_pair"),
    ]
    diffs = []
    for a, b, flag in contrasts:
        for g in decisions:
            ua, ub = units(a, flag, g), units(b, flag, g)
            if not ua or not ub:
                continue
            pt, lo, hi = bootstrap_diff(ua, ub)
            diffs.append(
                {
                    "a": a,
                    "b": b,
                    "threshold": flag,
                    "decision": g,
                    "diff": pt,
                    "lo95": lo,
                    "hi95": hi,
                    "above_spread": lo > 0,
                    "below_spread": hi < 0,
                }
            )
    return {
        "rows": rows,
        "floors": floors,
        "t_cross": t_cross,
        "per_item": per_item,
        "overall": overall,
        "diffs": diffs,
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()})


def cmd_analyze(_: argparse.Namespace) -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "repo_commit": git_commit(),
        "embedding_manifest": json.loads((DATA / "embedding-manifest.json").read_text()),
        "floor_percentile": FLOOR_PERCENTILE,
        "bootstrap": {"resamples": RESAMPLES, "seed": SEED, "interval": "95% percentile"},
        "variants": {},
    }
    for v in VARIANTS:
        res = analyze_variant(v)
        _write_csv(DATA / f"best-matches-{v}.csv", res["rows"])
        _write_csv(DATA / f"novelty-per-item-{v}.csv", res["per_item"])
        _write_csv(DATA / f"novelty-overall-{v}.csv", res["overall"])
        _write_csv(DATA / f"novelty-contrasts-{v}.csv", res["diffs"])
        summary["variants"][v] = {
            "floors": res["floors"],
            "t_cross": res["t_cross"],
            "overall": [o for o in res["overall"] if o["items"] == "all"],
            "contrasts_items_above_spread": Counter(
                f"{d['a']} - {d['b']}" for d in res["diffs"] if d["above_spread"]
            ),
            "contrasts_items_below_spread": Counter(
                f"{d['a']} - {d['b']}" for d in res["diffs"] if d["below_spread"]
            ),
            "contrasts_items_total": Counter(f"{d['a']} - {d['b']}" for d in res["diffs"]),
        }
    (DATA / "novelty-summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary["variants"], indent=1))
    return 0


# --- votes ------------------------------------------------------------------


def cmd_votes(_: argparse.Namespace) -> int:
    pos, status = load_positions("para")
    rec = {(p.decision, p.seat): p for p in pos if p.source == "recorded"}
    rows = []
    for p in pos:
        if p.source != "recorded":
            continue
        rows.append(
            {
                "source": "recorded",
                "decision": p.decision,
                "seat": p.seat,
                "sample": "rec",
                "first_action": "position",
                "vote": p.vote,
                "ready_to_vote": p.ready_to_vote,
                "recorded_vote": p.vote,
                "relation": "self",
            }
        )
    vote_of = {(p.source, p.decision, p.seat, p.sample): p.vote for p in pos}
    for s in status:
        r = rec.get((s["decision"], s["seat"]))
        v = vote_of.get((s["run"], s["decision"], s["seat"], s["sample"]))
        rv = r.vote if r else None
        if s["first_action"] != "position" or v is None:
            rel = f"no position ({s['first_action']})"
        elif v == rv:
            rel = "same vote"
        elif v == "defer":
            rel = "divergent by deferring"
        elif rv == "defer":
            rel = "votes where record deferred"
        else:
            rel = "divergent by voting differently"
        rows.append(
            {
                "source": s["run"],
                "decision": s["decision"],
                "seat": s["seat"],
                "sample": s["sample"],
                "first_action": s["first_action"],
                "vote": v,
                "ready_to_vote": None,
                "recorded_vote": rv,
                "relation": rel,
            }
        )
    DATA.mkdir(parents=True, exist_ok=True)
    _write_csv(DATA / "votes.csv", rows)

    def table(subset: str, keep) -> dict:
        out = {}
        for src in ("recorded", *REPLAY_RUNS):
            rs = [r for r in rows if r["source"] == src and keep(r["decision"])]
            if not rs:
                continue
            out[src] = {
                "samples": len(rs),
                "positions": sum(1 for r in rs if r["vote"]),
                "votes": dict(Counter(r["vote"] for r in rs if r["vote"])),
                "not_ready_to_vote": (
                    sum(1 for r in rs if r["ready_to_vote"] is False) if src == "recorded" else None
                ),
                "relation_to_record": dict(Counter(r["relation"] for r in rs)),
            }
        return out

    summary = {
        "note": "Round 1. The Round 1 `position` submission has no ready_to_vote field "
        "(only `respond`, from Round 2, does), so readiness exists for the recorded "
        "responses only; in the replays `defer` is the Round 1 way to say not ready. "
        "One row per sample, latest result (a re-run replaces the sample it re-ran).",
        "all": table("all", lambda g: True),
        "old_format_0006_0012": table("old", lambda g: g in OLD_FORMAT),
        "new_format_0013_on": table("new", lambda g: g not in OLD_FORMAT),
    }
    (DATA / "votes-summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("cmd", choices=["concerns", "embed", "analyze", "votes"])
    a = ap.parse_args()
    return {
        "concerns": cmd_concerns,
        "embed": cmd_embed,
        "analyze": cmd_analyze,
        "votes": cmd_votes,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
