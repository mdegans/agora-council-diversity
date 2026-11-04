"""Round 1 replays of Agora Council decisions, through the Message Batches API.

  uv run scripts/replay_round1.py map
      Resolve each decision's first Round 1 request per seat (exact dump where
      there is one, else the archive export) and write data/round1-sources.json.

  uv run scripts/replay_round1.py run --plan PLAN (--dry-run | --send)
      Build every replay request for the plan, write each one with a
      structural diff against its original under out/replay-<model>/, and
      print counts, sizes, hashes and an estimated cost. --send also submits
      them: sample 1 of every seat-item in one batch, then, once it has ended,
      the remaining samples in a second batch that reads its 1h cache.

Plans: opus46-noise (GOV-2026-0015/0016, 2 samples), haiku55 (every decision, 3
samples). Prints metadata only, never prompt or response text.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from council_diversity.adapter import (
    adapt,
    canonical_bytes,
    prefix_chars,
    sha256,
    structural_diff,
)
from council_diversity.batch import (
    BatchClient,
    classify_first_action,
    read_key,
    record_result,
    usage_cost,
)
from council_diversity.pricing import OUTPUT_TOKENS_PER_ANSWER, estimate_item
from council_diversity.sources import Source, load_index, load_request, resolve

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "inputs"
SOURCES = ROOT / "data" / "round1-sources.json"
OUT = ROOT / "out"
AGORA = Path("/home/claude-agora/Projects/agora")
DUMPS = AGORA / "secrets" / "council-requests"
KEY_FILE = AGORA / "secrets" / "anthropic_api_key"

PLANS = {
    "opus46-noise": {
        "model": "claude-opus-4-6",
        "decisions": ["GOV-2026-0015", "GOV-2026-0016"],
        "samples": 2,
        "max_usd": 3.0,
    },
    "haiku55": {"model": "claude-haiku-5-5", "decisions": None, "samples": 3, "max_usd": 10.0},
    # Opus 4.6 on the same adapted old-format requests as haiku55, to separate
    # the model from the format change (Steward, 2026-11-04).
    "opus46-adapted": {
        "model": "claude-opus-4-6",
        "decisions": [f"GOV-2026-{n:04d}" for n in range(6, 13)],
        "samples": 3,
        "max_usd": 12.0,
        "dir": "replay-claude-opus-4-6-adapted",
        "prefix": "opus46a",
    },
}
SHORT = {"claude-opus-4-6": "opus46", "claude-haiku-5-5": "haiku55"}


def plan_dir(plan: dict) -> Path:
    return OUT / plan.get("dir", f"replay-{plan['model']}")


def plan_prefix(plan: dict) -> str:
    return plan.get("prefix", SHORT[plan["model"]])


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


def cmd_map(args: argparse.Namespace) -> int:
    index = load_index(INPUTS / "archive-index.tsv")
    sources = resolve(INPUTS, args.dumps, index)
    SOURCES.parent.mkdir(exist_ok=True)
    doc = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "dumps_dir": str(args.dumps),
        "note": "Hashes and file names only. sha256 is of the source file's bytes; "
        "request_sha256 of the request as replayed from (dump bytes, or the archive "
        "prompt with properties order restored, compact JSON).",
        "sources": [s.to_json() for s in sources],
    }
    SOURCES.write_text(json.dumps(doc, indent=2) + "\n")
    for s in sources:
        print(
            f"{s.decision} {s.seat:<11} {s.kind:<7} {s.request_sha256[:12]} "
            f"export_is_round1={s.export_is_round1!s:<5} {Path(s.path).name}"
        )
    print(f"{len(sources)} seat-items -> {SOURCES.relative_to(ROOT)}")
    return 0


def _write_once(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise FileExistsError(f"{path} exists with different content; not overwriting")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build(plan: dict, dumps: Path) -> tuple[Path, list[dict]]:
    model = plan["model"]
    sources = [Source(**s) for s in json.loads(SOURCES.read_text())["sources"]]
    if plan["decisions"]:
        sources = [s for s in sources if s.decision in plan["decisions"]]
    run_dir = plan_dir(plan)
    items = []
    for s in sources:
        original = load_request(s, INPUTS, dumps)
        adapted = adapt(original, model)
        payload = adapted.payload
        diff = structural_diff(original, adapted.body)
        item_dir = run_dir / s.decision / s.seat
        _write_once(item_dir / "request.json", payload)
        _write_once(item_dir / "request.diff.json", (json.dumps(diff, indent=1) + "\n").encode())
        pre, total = prefix_chars(adapted.body)
        _, orig_total = prefix_chars(original)
        est = estimate_item(model, s.prompt_tokens_opus46, orig_total, total, pre, plan["samples"])
        items.append(
            {
                "decision": s.decision,
                "seat": s.seat,
                "source_kind": s.kind,
                "source_path": s.path,
                "source_sha256": s.sha256,
                "original_request_sha256": s.request_sha256,
                "format": adapted.fmt,
                "request_file": str((item_dir / "request.json").relative_to(ROOT)),
                "request_sha256": sha256(payload),
                "request_bytes": len(payload),
                "diff_paths": len(diff),
                "notes": adapted.notes,
                "estimate": est.__dict__,
                "body": adapted.body,
            }
        )
    return run_dir, items


def custom_id(prefix: str, decision: str, seat: str, n: int) -> str:
    return f"{prefix}-{decision}-{seat}-s{n:02d}"


def load_manifest(run_dir: Path) -> dict:
    p = run_dir / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {"batches": [], "samples": {}}


def save_manifest(run_dir: Path, m: dict) -> None:
    (run_dir / "manifest.json").write_text(json.dumps(m, indent=1) + "\n")


def run_batch(
    client: BatchClient,
    run_dir: Path,
    manifest: dict,
    phase: str,
    reqs: list[tuple],
    extra: dict[str, dict] | None = None,
) -> list[dict]:
    """Submit (or resume) one batch and record its results."""
    pending = [b for b in manifest["batches"] if b["phase"] == phase and not b.get("fetched")]
    if pending:
        batch = pending[0]
        print(f"resuming {phase} batch {batch['id']}")
    else:
        body = [{"custom_id": cid, "params": params} for cid, params, _ in reqs]
        created = client.create(body)
        batch = {
            "phase": phase,
            "id": created["id"],
            "created_at": created.get("created_at"),
            "custom_ids": [cid for cid, _, _ in reqs],
            "fetched": False,
        }
        manifest["batches"].append(batch)
        save_manifest(run_dir, manifest)
        print(f"submitted {phase} batch {batch['id']} with {len(reqs)} requests")
    ended = client.wait(batch["id"])
    paths = {cid: path for cid, _, path in reqs}
    metas = []
    for line in client.results(ended["results_url"]):
        cid = json.loads(line)["custom_id"]
        meta = record_result(line, paths[cid]) | {"batch_id": batch["id"], "phase": phase}
        meta |= (extra or {}).get(cid, {})
        if meta.get("usage"):
            meta["usd_actual"] = round(usage_cost(meta["model"], meta["usage"]), 6)
        manifest["samples"][cid] = meta
        metas.append(meta)
        print(
            json.dumps(
                {
                    k: meta.get(k)
                    for k in (
                        "custom_id",
                        "result_type",
                        "stop_reason",
                        "refusal_category",
                        "first_action",
                        "tool",
                        "submission",
                        "usage",
                    )
                }
            )
        )
    batch["fetched"] = True
    batch["ended_at"] = ended.get("ended_at")
    save_manifest(run_dir, manifest)
    return metas


def select_samples(manifest: dict, filters: list[str], ids: list[str]) -> list[str]:
    """Original (non-rerun) samples matching every KEY=VALUE filter, and in
    `ids` when given."""
    out = []
    for cid, meta in manifest["samples"].items():
        if meta.get("rerun_of"):
            continue
        if ids and cid not in ids:
            continue
        if all(str(meta.get(k)) == v for k, v in (f.split("=", 1) for f in filters)):
            out.append(cid)
    missing = [i for i in ids if i not in out]
    if missing:
        raise SystemExit(f"--ids not matching the filters or not in the manifest: {missing}")
    return sorted(out)


def cmd_rerun(args: argparse.Namespace, plan: dict, run_dir: Path, items: list[dict]) -> int:
    """Re-send chosen samples with one change (max_tokens), under a new name.

    The originals are kept: the re-run request is written beside the original
    as request.<tag>.json with its own structural diff, its responses go to
    sample-NN.<tag>.response.json, and the manifest records what changed.
    """
    model = plan["model"]
    tag = args.tag or f"rerun-{args.max_tokens // 1024}k"
    manifest = load_manifest(run_dir)
    ids = [i for i in (args.ids or "").split(",") if i]
    chosen = select_samples(manifest, args.rerun or [], ids)
    by_item = {(it["decision"], it["seat"]): it for it in items}
    reqs, extra, est = [], {}, 0.0
    for cid in chosen:
        orig = manifest["samples"][cid]
        stem = cid.removeprefix(f"{plan_prefix(plan)}-")
        decision, rest = stem[:13], stem[14:]
        seat, n = rest.rsplit("-s", 1)
        it = by_item[(decision, seat)]
        item_dir = run_dir / decision / seat
        base = json.loads((ROOT / it["request_file"]).read_bytes())
        assert sha256(canonical_bytes(base)) == it["request_sha256"]
        body = dict(base)
        body["max_tokens"] = args.max_tokens
        payload = canonical_bytes(body)
        diff = structural_diff(base, body)
        assert [d["path"] for d in diff] == ["/max_tokens"], diff
        _write_once(item_dir / f"request.{tag}.json", payload)
        _write_once(
            item_dir / f"request.{tag}.diff.json", (json.dumps(diff, indent=1) + "\n").encode()
        )
        new_cid = f"{cid}-{tag.replace('rerun-', 'rr')}"
        path = item_dir / f"sample-{int(n):02d}.{tag}.response.json"
        if path.exists() or new_cid in manifest["samples"]:
            continue
        reqs.append((new_cid, json.loads(payload), path))
        extra[new_cid] = {
            "rerun_of": cid,
            "rerun_tag": tag,
            "change": {"max_tokens": [base["max_tokens"], args.max_tokens]},
            "request_file": str((item_dir / f"request.{tag}.json").relative_to(ROOT)),
            "request_sha256": sha256(payload),
            "base_request_sha256": it["request_sha256"],
        }
        # Upper bound: same prompt usage as the original, full max_tokens out.
        u = dict(orig["usage"])
        u["output_tokens"] = args.max_tokens
        est += usage_cost(model, u)
        print(
            f"{new_cid:<52} was {orig.get('stop_reason')}/{orig.get('first_action')} "
            f"sha {sha256(payload)[:12]}"
        )
    print(
        json.dumps(
            {
                "tag": tag,
                "calls": len(reqs),
                "max_tokens": args.max_tokens,
                "estimated_usd_upper_bound": round(est, 4),
            }
        )
    )
    if args.dry_run or not reqs:
        print("dry run: nothing sent" if args.dry_run else "nothing to send")
        return 0
    cap = args.max_usd if args.max_usd is not None else 1.0
    if est > cap:
        print(f"estimate ${est:.2f} is over the cap ${cap:.2f}; not sending")
        return 4
    client = BatchClient(read_key(args.key_file))
    metas = run_batch(client, run_dir, manifest, tag, reqs, extra)
    print(
        json.dumps(
            {
                "tag": tag,
                "results": len(metas),
                "usd_actual": round(sum(m.get("usd_actual", 0) for m in metas), 4),
            }
        )
    )
    return 0


def cmd_reclassify(args: argparse.Namespace) -> int:
    """Recompute first_action for every saved response (files untouched).
    A changed classification keeps its previous value under first_action_prev."""
    run_dir = plan_dir(PLANS[args.plan])
    manifest = load_manifest(run_dir)
    changed = 0
    for cid, meta in manifest["samples"].items():
        if meta.get("result_type") != "succeeded":
            continue
        line = Path(meta["response_file"]).read_bytes()
        assert sha256(line) == meta["response_sha256"], cid
        c = classify_first_action(json.loads(line)["result"]["message"])
        if c["first_action"] != meta.get("first_action"):
            print(f"{cid}: {meta.get('first_action')} -> {c['first_action']}")
            meta["first_action_prev"] = meta.get("first_action")
            changed += 1
        meta |= c
    save_manifest(run_dir, manifest)
    print(f"{changed} reclassified")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    plan = dict(PLANS[args.plan])
    model = plan["model"]
    run_dir, items = build(plan, args.dumps)
    if args.rerun or args.ids:
        if not args.max_tokens:
            raise SystemExit("a re-run needs --max-tokens (the one change it makes)")
        return cmd_rerun(args, plan, run_dir, items)
    if args.max_usd is not None:
        plan["max_usd"] = args.max_usd

    total_usd = 0.0
    by_fmt: dict[str, int] = {}
    tiers: dict[str, int] = {}
    for it in items:
        e = it["estimate"]
        total_usd += e["usd"]
        by_fmt[it["format"]] = by_fmt.get(it["format"], 0) + 1
        tiers[e["tier"]] = tiers.get(e["tier"], 0) + 1
        print(
            f"{it['decision']} {it['seat']:<11} {it['format']:<3} {it['source_kind']:<7} "
            f"{it['request_bytes']:>8}B sha {it['request_sha256'][:12]} "
            f"~{e['prompt_tokens']:>7}tok (cached {e['prefix_tokens']:>7}) "
            f"{e['tier']:<5} ${e['usd']:.4f}  diff {it['diff_paths']} paths"
        )
    calls = len(items) * plan["samples"]
    summary = {
        "plan": args.plan,
        "model": model,
        "seat_items": len(items),
        "samples_per_item": plan["samples"],
        "calls": calls,
        "formats": by_fmt,
        "tiers": tiers,
        "request_bytes_total": sum(it["request_bytes"] for it in items),
        "assumed_output_tokens_per_answer": OUTPUT_TOKENS_PER_ANSWER,
        "estimated_usd_batch": round(total_usd, 2),
        "budget_cap_usd": plan["max_usd"],
    }
    print(json.dumps(summary, indent=1))

    manifest = load_manifest(run_dir)
    manifest["plan"] = summary | {"repo_commit": git_commit()}
    manifest["items"] = {
        f"{it['decision']}/{it['seat']}": {k: v for k, v in it.items() if k != "body"}
        for it in items
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    save_manifest(run_dir, manifest)

    if args.dry_run:
        print("dry run: nothing sent")
        return 0
    if total_usd > plan["max_usd"]:
        print(f"estimate ${total_usd:.2f} is over the cap ${plan['max_usd']:.2f}; not sending")
        return 4

    client = BatchClient(read_key(args.key_file))

    def todo(samples: range) -> list[tuple]:
        out = []
        for it in items:
            for n in samples:
                cid = custom_id(plan_prefix(plan), it["decision"], it["seat"], n)
                path = run_dir / it["decision"] / it["seat"] / f"sample-{n:02d}.response.json"
                if path.exists() or cid in manifest["samples"]:
                    continue
                # The bytes sent are the bytes hashed: re-parse the written file.
                params = json.loads((ROOT / it["request_file"]).read_bytes())
                assert sha256(canonical_bytes(params)) == it["request_sha256"]
                out.append((cid, params, path))
        return out

    first = todo(range(1, 2))
    if first:
        run_batch(client, run_dir, manifest, "phase1", first)
    refused = {
        cid.rsplit("-s", 1)[0]
        for cid, m in manifest["samples"].items()
        if m.get("stop_reason") == "refusal"
    }
    if refused and not args.continue_after_refusal:
        print(f"refusals in phase 1: {sorted(refused)}; stopping (refusals are not retried)")
        return 3
    rest = [
        r for r in todo(range(2, plan["samples"] + 1)) if r[0].rsplit("-s", 1)[0] not in refused
    ]
    if rest:
        run_batch(client, run_dir, manifest, "phase2", rest)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dumps", type=Path, default=DUMPS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("map")
    rc = sub.add_parser("reclassify")
    rc.add_argument("--plan", choices=sorted(PLANS), required=True)
    r = sub.add_parser("run")
    r.add_argument("--plan", choices=sorted(PLANS), required=True)
    mode = r.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--send", action="store_true")
    r.add_argument("--max-usd", type=float)
    r.add_argument("--key-file", type=Path, default=KEY_FILE)
    r.add_argument(
        "--continue-after-refusal",
        action="store_true",
        help="send phase 2 for the seat-items that were not refused",
    )
    r.add_argument(
        "--rerun",
        action="append",
        metavar="KEY=VALUE",
        help="re-send samples whose manifest entry matches (e.g. stop_reason=max_tokens)",
    )
    r.add_argument("--ids", help="comma-separated custom_ids to restrict a re-run to")
    r.add_argument("--max-tokens", type=int, help="the re-run's max_tokens")
    r.add_argument("--tag", help="name for the re-run (default rerun-<N>k)")
    args = ap.parse_args()
    if args.cmd == "map":
        return cmd_map(args)
    if args.cmd == "reclassify":
        return cmd_reclassify(args)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
