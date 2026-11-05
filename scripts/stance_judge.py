"""Blinded stance judge over best-match concern pairs (Claude Haiku 5.5).

  uv run scripts/stance_judge.py build            # pairs -> out/judge, token count, estimate
  uv run scripts/stance_judge.py send --max-usd 5 # priming batch of 1, then the rest
  uv run scripts/stance_judge.py analyze          # labels vs embedding similarity

Pairs: each concern and its best match (the plan's paragraph split), for the
comparisons in PAIRS. Prints counts, hashes and numbers only.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import httpx
import numpy as np

from council_diversity.adapter import sha256
from council_diversity.batch import API_VERSION, BatchClient, read_key, record_result, usage_cost
from council_diversity.judge import (
    DISCLAIMER,
    LABELS,
    MODEL,
    assign_sides,
    blind,
    build_request,
    canonical_bytes,
    check_request,
    pair_id,
    parse_label,
    prefix_blocks,
)
from council_diversity.novelty import RESAMPLES, SEED, auroc

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "out" / "analysis"
JOUT = ROOT / "out" / "judge"
DATA = ROOT / "data" / "judge"
AGORA = Path("/home/claude-agora/Projects/agora")
KEY_FILE = AGORA / "secrets" / "anthropic_api_key"
DOCS = {"constitution": AGORA / "constitution.md", "protocol": AGORA / "governance-protocol.md"}
PAIRS = (
    "haiku55_vs_rec_same",
    "opus46a_vs_rec_same",
    "haiku55_vs_haiku55",
    "opus46a_vs_opus46a",
)
SIDE_SEED = 20261105
ASSUMED_OUTPUT = 1500


def concern_texts() -> dict[tuple[str, int], str]:
    out = {}
    for line in (WORK / "concerns-para.jsonl").open():
        d = json.loads(line)
        for i, c in enumerate(d["concerns"]):
            out[(d["key"], i)] = c
    return out


def select_pairs() -> list[dict]:
    texts = concern_texts()
    seen: dict[tuple[str, str], dict] = {}
    with (ROOT / "data" / "divergence" / "best-matches-para.csv").open() as f:
        for r in csv.DictReader(f):
            comp = r["comparison"]
            if comp not in PAIRS:
                continue
            if comp.endswith(("_vs_haiku55", "_vs_opus46a")) and not (r["a"] < r["b"]):
                continue  # same-model pairs: one direction only
            a = texts[(r["a"], int(r["a_concern"]))]
            b = texts[(r["b"], int(r["b_concern"]))]
            ah, bh = sha256(a.encode()), sha256(b.encode())
            k = (ah, bh)
            if k in seen:
                seen[k]["comparisons"].append(comp)
                continue
            seen[k] = {
                "id": pair_id(ah, bh),
                "comparisons": [comp],
                "decision": r["decision"],
                "seat": r["seat"],
                "a": r["a"],
                "a_concern": int(r["a_concern"]),
                "b": r["b"],
                "b_concern": int(r["b_concern"]),
                "a_sha256": ah,
                "b_sha256": bh,
                "sim": float(r["sim"]),
                "novel_pair": r["novel_pair"] == "True",
            }
    return list(seen.values())


def count_tokens(key: str, req: dict) -> int:
    body = {k: v for k, v in req.items() if k != "max_tokens"}
    r = httpx.post(
        "https://api.anthropic.com/v1/messages/count_tokens",
        headers={
            "x-api-key": key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
        content=canonical_bytes(body),
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["input_tokens"]


def estimate(prefix_tok: int, pair_tok: list[int], out_tok: float) -> float:
    """USD through the batch API: request 1 writes the 1h cache, the rest read it."""
    first = {
        "input_tokens": pair_tok[0],
        "cache_creation_input_tokens": prefix_tok,
        "cache_creation": {"ephemeral_1h_input_tokens": prefix_tok},
        "output_tokens": out_tok,
    }
    usd = usage_cost(MODEL, first)
    for t in pair_tok[1:]:
        usd += usage_cost(
            MODEL,
            {"input_tokens": t, "cache_read_input_tokens": prefix_tok, "output_tokens": out_tok},
        )
    return usd


def cmd_build(args: argparse.Namespace) -> int:
    pairs = assign_sides(select_pairs(), SIDE_SEED)
    texts = concern_texts()
    docs_text = {k: p.read_text() for k, p in DOCS.items()}
    system, docs = prefix_blocks(docs_text["constitution"], docs_text["protocol"])
    JOUT.mkdir(parents=True, exist_ok=True)
    reqs = []
    for p in pairs:
        a = blind(texts[(p["a"], p["a_concern"])])
        b = blind(texts[(p["b"], p["b_concern"])])
        if p["swapped"]:
            a, b = b, a
        req = build_request(system, docs, a, b)
        check_request(req)
        p["request_sha256"] = sha256(canonical_bytes(req))
        p["pair_chars"] = len(a) + len(b)
        reqs.append(req)
    with (JOUT / "requests.jsonl").open("wb") as f:
        for p, r in zip(pairs, reqs, strict=True):
            f.write(
                json.dumps({"custom_id": p["id"], "params": r}, ensure_ascii=False).encode() + b"\n"
            )
    (JOUT / "pairs.json").write_text(json.dumps(pairs, indent=0) + "\n")

    key = read_key(args.key_file)
    full = count_tokens(key, reqs[0])
    # the prefix alone: same request with an empty-ish pair
    stub = build_request(system, docs, "x", "y")
    stub_tok = count_tokens(key, stub)
    stub_pair_chars = 2
    # Pair tokens scale with characters; calibrate on request 0.
    per_char = (full - stub_tok) / max(1, pairs[0]["pair_chars"] - stub_pair_chars)
    prefix_tok = stub_tok - 20  # the stub's own pair text is ~20 tokens
    pair_tok = [round(20 + per_char * p["pair_chars"]) for p in pairs]
    est = {
        "pairs": len(pairs),
        "by_comparison": Counter(c for p in pairs for c in p["comparisons"]),
        "prefix_tokens": prefix_tok,
        "request0_tokens": full,
        "pair_tokens_mean": float(np.mean(pair_tok)),
        "assumed_output_tokens": ASSUMED_OUTPUT,
        "estimated_usd_batch": round(estimate(prefix_tok, pair_tok, ASSUMED_OUTPUT), 3),
        "estimated_usd_batch_at_3000_out": round(estimate(prefix_tok, pair_tok, 3000), 3),
        "requests_sha256_file": sha256((JOUT / "requests.jsonl").read_bytes()),
    }
    (JOUT / "estimate.json").write_text(json.dumps(est, indent=1) + "\n")
    print(json.dumps(est, indent=1))
    return 0


def _load_manifest() -> dict:
    p = JOUT / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {"batches": [], "results": {}}


def _save_manifest(m: dict) -> None:
    (JOUT / "manifest.json").write_text(json.dumps(m, indent=1) + "\n")


def _submit(client: BatchClient, m: dict, phase: str, lines: list[dict]) -> dict:
    """Create a batch for `phase` unless one exists already (resume)."""
    existing = [b for b in m["batches"] if b["phase"] == phase]
    if existing:
        return existing[0]
    created = client.create(lines)
    batch = {
        "phase": phase,
        "id": created["id"],
        "n": len(lines),
        "fetched": False,
        "created_at": created.get("created_at"),
    }
    m["batches"].append(batch)
    _save_manifest(m)
    print(f"submitted {phase} {batch['id']} n={len(lines)}", flush=True)
    return batch


def _fetch(client: BatchClient, m: dict, batch: dict) -> None:
    if batch.get("fetched"):
        return
    phase = batch["phase"]
    ended = client.wait(batch["id"])
    for line in client.results(ended["results_url"]):
        cid = json.loads(line)["custom_id"]
        if cid in m["results"]:
            continue
        meta = record_result(line, JOUT / "results" / f"{cid}.json")
        msg = json.loads(line)["result"].get("message") or {}
        meta["label"] = parse_label(msg) if msg else None
        if meta.get("usage"):
            meta["usd_actual"] = usage_cost(MODEL, meta["usage"])
        for k in ("first_action", "tool", "submission", "vote", "block_types"):
            meta.pop(k, None)
        m["results"][cid] = meta | {"phase": phase}
    batch["fetched"] = True
    batch["ended_at"] = ended.get("ended_at")
    _save_manifest(m)


def _run(client: BatchClient, m: dict, phase: str, lines: list[dict]) -> None:
    _fetch(client, m, _submit(client, m, phase, lines))


# The batch API takes at most 256 MB per batch; every request carries the
# two documents (~95 KB), so the rest goes out in chunks, all submitted
# before any is awaited so that each reads the primed 1h cache.
CHUNK = 900


def cmd_send(args: argparse.Namespace) -> int:
    est = json.loads((JOUT / "estimate.json").read_text())
    lines = [json.loads(x) for x in (JOUT / "requests.jsonl").open()]
    if est["estimated_usd_batch_at_3000_out"] > args.max_usd:
        print(f"estimate over cap ${args.max_usd}; not sending")
        return 4
    m = _load_manifest()
    client = BatchClient(read_key(args.key_file))
    if not any(b["phase"] == "prime" for b in m["batches"]) or not all(
        b.get("fetched") for b in m["batches"] if b["phase"] == "prime"
    ):
        _run(client, m, "prime", lines[:1])
    r0 = m["results"][lines[0]["custom_id"]]
    u = r0.get("usage") or {}
    print(json.dumps({"prime_usage": u, "prime_usd": r0.get("usd_actual")}))
    # Re-check the cap with the primed request's real output size.
    out_tok = max(ASSUMED_OUTPUT, (u.get("output_tokens") or 0) * 1.5)
    proj = r0.get("usd_actual", 0) + (len(lines) - 1) * usage_cost(
        MODEL,
        {
            "input_tokens": u.get("input_tokens", 0),
            "cache_read_input_tokens": (
                u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
            ),
            "output_tokens": out_tok,
        },
    )
    print(json.dumps({"projected_total_usd": round(proj, 3)}))
    if proj > args.max_usd:
        print("projection over cap; stopping after the priming batch")
        return 4
    rest = [x for x in lines[1:] if x["custom_id"] not in m["results"]]
    chunks = [rest[i : i + CHUNK] for i in range(0, len(rest), CHUNK)]
    batches = [_submit(client, m, f"rest-{k + 1}", c) for k, c in enumerate(chunks)]
    for b in batches:
        _fetch(client, m, b)
    spent = sum(r.get("usd_actual", 0) for r in m["results"].values())
    print(
        json.dumps(
            {
                "results": len(m["results"]),
                "usd_actual": round(spent, 4),
                "labels": Counter(r.get("label") for r in m["results"].values()),
            }
        )
    )
    return 0


def _boot_auroc(sims: np.ndarray, pos: np.ndarray, groups: np.ndarray) -> tuple[float, float]:
    """95% interval, resampling items (decisions) with replacement."""
    rng = np.random.default_rng(SEED)
    keys = np.unique(groups)
    by = {k: np.flatnonzero(groups == k) for k in keys}
    vals = []
    for _ in range(min(RESAMPLES, 2000)):
        pick = rng.choice(keys, size=len(keys), replace=True)
        idx = np.concatenate([by[k] for k in pick])
        vals.append(auroc(sims[idx], pos[idx]))
    lo, hi = np.nanpercentile(vals, [2.5, 97.5])
    return float(lo), float(hi)


def cmd_analyze(_: argparse.Namespace) -> int:
    pairs = json.loads((JOUT / "pairs.json").read_text())
    m = _load_manifest()
    DATA.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in pairs:
        r = m["results"].get(p["id"]) or {}
        rows.append(
            {
                "id": p["id"],
                "comparisons": "+".join(p["comparisons"]),
                "decision": p["decision"],
                "seat": p["seat"],
                "a": p["a"],
                "a_concern": p["a_concern"],
                "b": p["b"],
                "b_concern": p["b_concern"],
                "a_sha256": p["a_sha256"],
                "b_sha256": p["b_sha256"],
                "request_sha256": p["request_sha256"],
                "swapped": p["swapped"],
                "sim": round(p["sim"], 6),
                "novel_pair": p["novel_pair"],
                "label": r.get("label"),
                "stop_reason": r.get("stop_reason"),
                "result_type": r.get("result_type"),
                "response_sha256": r.get("response_sha256"),
            }
        )
    with (DATA / "labels.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    labelled = [r for r in rows if r["label"] in LABELS]
    summary: dict = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "disclaimer": DISCLAIMER,
        "model": MODEL,
        "pairs": len(rows),
        "labelled": len(labelled),
        "not_labelled": Counter(
            f"{r['result_type']}/{r['stop_reason']}" for r in rows if r["label"] not in LABELS
        ),
        "by_comparison": {},
    }
    by = defaultdict(list)
    for r in labelled:
        for c in r["comparisons"].split("+"):
            by[c].append(r)
    for c, rs in sorted(by.items()):
        lab = Counter(r["label"] for r in rs)
        sims = np.array([r["sim"] for r in rs])
        agree = np.array([r["label"] == "agree" for r in rs])
        groups = np.array([r["decision"] for r in rs])
        novel = np.array([r["novel_pair"] for r in rs])
        a = auroc(sims, agree)
        lo, hi = _boot_auroc(sims, agree, groups)
        summary["by_comparison"][c] = {
            "n": len(rs),
            "labels": dict(lab),
            "shares": {k: lab[k] / len(rs) for k in LABELS},
            "auroc_sim_vs_agree": a,
            "auroc_lo95_items": lo,
            "auroc_hi95_items": hi,
            "embedding_novel": int(novel.sum()),
            "novel_and_not_agree": int((novel & ~agree).sum()),
            "not_novel_and_not_agree": int((~novel & ~agree).sum()),
            "contradict_share_among_novel": (
                sum(1 for r in rs if r["novel_pair"] and r["label"] == "contradict")
                / max(1, int(novel.sum()))
            ),
            "contradict_share_among_not_novel": (
                sum(1 for r in rs if not r["novel_pair"] and r["label"] == "contradict")
                / max(1, int((~novel).sum()))
            ),
        }
    sims = np.array([r["sim"] for r in labelled])
    agree = np.array([r["label"] == "agree" for r in labelled])
    groups = np.array([r["decision"] for r in labelled])
    lo, hi = _boot_auroc(sims, agree, groups)
    summary["all"] = {"auroc_sim_vs_agree": auroc(sims, agree), "lo95": lo, "hi95": hi}
    usage = Counter()
    for r in m["results"].values():
        for k, v in (r.get("usage") or {}).items():
            if isinstance(v, int):
                usage[k] += v
    summary["usage_totals"] = dict(usage)
    summary["usd_actual"] = round(sum(r.get("usd_actual", 0) for r in m["results"].values()), 4)
    summary["batches"] = [
        {k: b.get(k) for k in ("phase", "id", "n", "created_at", "ended_at")} for b in m["batches"]
    ]
    (DATA / "judge-summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("cmd", choices=["build", "send", "analyze"])
    ap.add_argument("--key-file", type=Path, default=KEY_FILE)
    ap.add_argument("--max-usd", type=float, default=5.0)
    a = ap.parse_args()
    return {"build": cmd_build, "send": cmd_send, "analyze": cmd_analyze}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
