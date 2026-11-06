"""Invented-record count (OBL-2026-0059), step 1: case files, candidates, groups.

  uv run scripts/invented_records.py build --data-root DIR [--dumps-dir DIR]
  uv run scripts/invented_records.py qcheck CASE_ID [...]

`--data-root` holds checkouts of the sittings' wire DATA sets, one directory
per set named as the set (DATA-2026-0008 ... 0011, 0017 ... 0024), e.g.
`git clone ~/agora-data/DATA-2026-0017.git DIR/DATA-2026-0017`. GOV-2026-0018
has no DATA set: its Round 1 requests are this repo's inputs/GOV-2026-0018/,
its responses the Council's request dumps (`--dumps-dir`).

Writes analysis/invented-records/ (see its README). Prints counts only, never
prompt or response text. Case ids are random once and then kept: an existing
key.csv is reused, so a rebuild writes the same files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from council_diversity import invrec as I

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "analysis" / "invented-records"
DEFAULT_DUMPS = Path.home() / "Projects" / "agora" / "secrets" / "council-requests"
N_GROUPS = 9
CHARS_PER_TOKEN = 3.5  # a rough figure for English prose and Markdown
# A group, every file in it read whole, stays under this, leaving a counter's
# context room for its tool calls and its tally.
MAX_GROUP_TOKENS = 250_000
CAND_LEN: dict[str, int] = {}  # case id -> characters of its candidates file


def _git_head(d: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def collect(data_root: Path, dumps_dir: Path):
    """Every position, as (source, item, seat, sample, rerun, request view, loop, answer)."""
    rows = []
    status = {}
    for run in I.RUNS:
        samples, st = I.replay_samples(ROOT, run)
        status[run] = dict(st)
        for s in samples:
            rows.append(("replay", s))
    recorded = []
    sources = {}
    for item in I.ITEMS:
        if item == I.GOV18:
            calls = I.gov18_calls(ROOT, dumps_dir)
            sources[item] = {
                "from": "inputs/GOV-2026-0018 (requests) + the Council's request dumps (responses)",
                "response_sha256": {
                    c.who: _sha((dumps_dir / I.GOV18_MEETING / c.path).read_bytes()) for c in calls
                },
            }
        else:
            ds, sub = I.SITTING_SETS[item]
            calls = I.sitting_calls(data_root, item)
            sources[item] = {"data_set": ds, "dir": sub, "commit": _git_head(data_root / ds)}
        files = {}
        for seat in I.SEATS:
            loop = I.recorded_loop(calls, seat)
            recorded.append((item, seat, loop))
            files[seat] = loop.path
        sources[item]["round1_files"] = files
    return rows, recorded, status, sources


def load_or_new_ids(keys: list[tuple]) -> dict[tuple, str]:
    kp = OUT / "key.csv"
    have = {}
    if kp.exists():
        for cid, r in I.read_key(kp).items():
            have[(r["source"], r["item"], r["seat"], r["sample"])] = cid
    rng = secrets.SystemRandom()
    taken = set(have.values())
    out = {}
    for k in keys:
        out[k] = have.get(k) or I.new_case_id(rng, taken)
    return out


def cmd_build(a: argparse.Namespace) -> int:
    data_root, dumps = Path(a.data_root).expanduser(), Path(a.dumps_dir).expanduser()
    replays, recorded, status, sources = collect(data_root, dumps)
    common = I.Common()

    # The item text: first-user-message blocks identical for all four seats.
    shared: dict[str, set[str]] = {}
    item_rel: dict[str, str] = {}
    for item in I.ITEMS:
        reqs = []
        for seat in I.SEATS:
            p = ROOT / "out" / I.RUNS["haiku55"] / item / seat / "request.json"
            reqs.append(json.loads(p.read_bytes()))
        shared[item] = I.item_blocks(reqs)
        first = I.kept_blocks(reqs[0]["messages"][0]["content"])
        item_rel[item] = I.item_ref(item, first, shared[item], common)

    keys = [(s.run, s.item, s.seat, s.sample) for _, s in replays] + [
        ("recorded", item, seat, "rec") for item, seat, _ in recorded
    ]
    ids = load_or_new_ids(keys)
    cases: list[I.Case] = []

    def make(source, item, seat, sample, rerun, system, tools, first_msg, rest, answer):
        sys_rels, cons = I.system_refs(system, common)
        tools_rel = I.tools_ref(tools, common)
        first_user, refs = I._render_first_user(
            I.kept_blocks(first_msg["content"]), shared[item], item_rel[item]
        )
        rest_text = "\n".join(I.render_turn(m, n) for n, m in enumerate(rest, 2))
        c = I.Case(
            case_id=ids[(source, item, seat, sample)],
            source=source,
            item=item,
            seat=seat,
            sample=sample,
            rerun=rerun,
            refs=sys_rels + [tools_rel] + refs,
            constitution=cons,
            inputs_inline=first_user + "\n" + rest_text,
            answer=answer,
        )
        c.body = I.build_case_body(c, sys_rels, tools_rel, first_user, rest_text)
        cases.append(c)

    for _, s in replays:
        if s.rerun:
            orig = json.loads(
                (ROOT / "out" / I.RUNS[s.run] / s.item / s.seat / "request.json").read_bytes()
            )
            for k in ("system", "messages", "tools"):
                if orig.get(k) != s.request.get(k):
                    raise SystemExit(f"rerun request differs from the original in {k}")
        system, tools = I._request_view(s.request)
        make(
            s.run,
            s.item,
            s.seat,
            s.sample,
            s.rerun,
            system,
            tools,
            s.request["messages"][0],
            [],
            s.answer,
        )
    for item, seat, loop in recorded:
        first = {"role": "user", "content": loop.messages[0]["content"]}
        # loop.messages is already allowlisted; re-wrap so kept_blocks is idempotent
        make(
            "recorded",
            item,
            seat,
            "rec",
            False,
            loop.system,
            loop.tools,
            _unkept(first),
            loop.messages[1:],
            loop.answer,
        )

    # --- write -------------------------------------------------------------
    (OUT / "cases").mkdir(parents=True, exist_ok=True)
    (OUT / "candidates").mkdir(parents=True, exist_ok=True)
    for old in list((OUT / "cases").glob("*.md")) + list((OUT / "candidates").glob("*.md")):
        old.unlink()
    for rel, text in common.files.items():
        p = OUT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    for c in cases:
        (OUT / "cases" / f"{c.case_id}.md").write_text(c.body)
    I.write_key(OUT / "key.csv", cases)

    # --- candidates -----------------------------------------------------------
    cand_counts = Counter()
    for c in cases:
        cand, counts = I.case_candidates(c.case_id, c.body, common.files.__getitem__)
        (OUT / "candidates" / f"{c.case_id}.md").write_text(cand)
        CAND_LEN[c.case_id] = len(cand)
        for k, v in counts.items():
            cand_counts[(c.source, k)] += v

    groups = make_groups(cases, common)
    write_groups(groups, common)
    write_tallies(groups)

    manifest = {
        "sources": sources,
        "replay_status": status,
        "cases": dict(Counter(c.source for c in cases)),
        "common": {r: _sha(t.encode()) for r, t in sorted(common.files.items())},
        "case_sha256": {
            c.case_id: _sha(c.body.encode()) for c in sorted(cases, key=lambda c: c.case_id)
        },
        "candidate_counts": {f"{s}:{k}": v for (s, k), v in sorted(cand_counts.items())},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")

    print("cases by source:", dict(Counter(c.source for c in cases)))
    print("replay first actions (latest per sample):", status)
    print("common files:", len(common.files))
    for (s, k), v in sorted(cand_counts.items()):
        print(f"  candidates {s:9s} {k:10s} {v}")
    for g in groups:
        print(
            f"  G{g['n']}: {len(g['cases'])} cases, items {','.join(i[-2:] for i in g['items'])},"
            f" sources {dict(g['sources'])}, {g['chars']:,} chars ~{g['tokens']:,} tok"
            f" (cases {g['case_chars']:,}, common {g['common_chars']:,})"
        )
    return 0


def _unkept(m: dict) -> dict:
    """An allowlisted message back in API shape (text blocks only are needed here)."""
    content = []
    for b in m["content"]:
        if b["type"] == "text":
            content.append({"type": "text", "text": b["text"]})
    return {"role": m["role"], "content": content}


# --- groups -------------------------------------------------------------------


def make_groups(cases: list[I.Case], common: I.Common) -> list[dict]:
    """Groups of whole (item, seat) bundles in item order, ~24 cases each, size-capped.

    A bundle is every case of one seat on one item: its Haiku 5.5 samples, its
    Opus 4.6 adapted samples and the recorded position, as DATA-2026-0001 kept
    both models' samples of one case in one group. Keeping items together keeps
    the shared texts a counter must read (the item, the Constitution and
    Protocol in force) to as few as possible.
    """
    bundles: dict[tuple, list[I.Case]] = defaultdict(list)
    for c in cases:
        bundles[(c.item, c.seat)].append(c)
    order = sorted(bundles, key=lambda k: (k[0], I.SEATS.index(k[1])))
    target = len(cases) / N_GROUPS
    # Cut the item-ordered bundles into contiguous groups (dynamic programming):
    # as few groups as the size cap allows, each near `target` cases, and as
    # few shared texts per group as possible. A counter must be able to hold a
    # group's cases and the shared texts they cite (MAX_GROUP_TOKENS).
    n = len(order)
    best: list[tuple[float, int]] = [(0.0, -1)] + [(float("inf"), -1)] * n
    for i in range(1, n + 1):
        for j in range(i - 1, -1, -1):
            g = _size(order[j:i], bundles, common)
            if g["tokens"] > MAX_GROUP_TOKENS and i - j > 1:
                break
            cost = best[j][0] + 2000 + 100 * (len(g["cases"]) - target) ** 2 + g["tokens"] / 100
            if cost < best[i][0]:
                best[i] = (cost, j)
    cuts, i = [], n
    while i > 0:
        j = best[i][1]
        cuts.append((j, i))
        i = j
    groups = [order[j:i] for j, i in reversed(cuts)]
    sized = [_size(ks, bundles, common) for ks in groups]
    out = []
    for n, g in enumerate(sized, 1):
        g["n"] = n
        out.append(g)
    return out


def _size(ks: list[tuple], bundles: dict, common: I.Common) -> dict:
    cs = sorted(
        (c for k in ks for c in bundles[k]),
        key=lambda c: (c.item, I.SEATS.index(c.seat), c.case_id),
    )
    refs = sorted({r for c in cs for r in c.refs})
    case_chars = sum(len(c.body) + CAND_LEN[c.case_id] for c in cs)
    common_chars = sum(len(common.files[r]) for r in refs)
    chars = case_chars + common_chars
    return {
        "cases": cs,
        "items": sorted({c.item for c in cs}),
        "sources": Counter(c.source for c in cs),
        "refs": refs,
        "case_chars": case_chars,
        "common_chars": common_chars,
        "chars": chars,
        "tokens": round(chars / CHARS_PER_TOKEN),
    }


def write_groups(groups: list[dict], common: I.Common) -> None:
    lines = [
        "# Groups",
        "",
        f"{sum(len(g['cases']) for g in groups)} cases in {len(groups)} groups. A group is whole "
        "(item, seat) bundles: every case of one seat on one item stays together, all "
        "sources mixed, as DATA-2026-0001 kept both models' samples of a case in one "
        "group. Within a group, cases are listed by item and seat, then by case id "
        "(random), so the order says nothing about the source.",
        "",
        f"Sizes: characters of the group's case files and candidate files, plus the "
        f"common files they reference (each counted once); tokens at about "
        f"{CHARS_PER_TOKEN} characters a token. The item files are most of it: read "
        "the answers and the case-specific inputs in full, and search the item "
        "files, the Constitution and the Protocol (grep, `qcheck`) rather than "
        "reading them whole when they don't fit.",
        "",
        "| group | cases | items | case + candidate chars | common chars | total chars | ~tokens |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for g in groups:
        lines.append(
            f"| G{g['n']} | {len(g['cases'])} | {', '.join(i[-4:] for i in g['items'])} | "
            f"{g['case_chars']:,} | {g['common_chars']:,} | {g['chars']:,} | {g['tokens']:,} |"
        )
    lines.append("")
    for g in groups:
        lines += [f"## G{g['n']}", "", "Common files to have at hand:", ""]
        lines += [f"- `{r}` ({len(common.files[r]):,} chars)" for r in g["refs"]]
        lines += ["", "Cases:", ""]
        cur = None
        for c in g["cases"]:
            if (c.item, c.seat) != cur:
                cur = (c.item, c.seat)
                lines.append(f"- {c.item} {c.seat}:")
            lines.append(f"  - `{c.case_id}`")
        lines.append("")
    (OUT / "groups.md").write_text("\n".join(lines))


def write_tallies(groups: list[dict]) -> None:
    for old in OUT.glob("halluc-tally-G*.md"):
        old.unlink()
    for g in groups:
        items = ", ".join(g["items"])
        lines = [
            f"# G{g['n']} tally (items {items})",
            "Columns: case | item | seat | A B C Cmin D E | items (counted; [bl] = borderline, "
            "not counted)",
            "",
        ]
        for c in g["cases"]:
            lines.append(f"{c.case_id} | {c.item[-4:]} | {c.seat} | _ _ _ _ _ _ | ")
        lines += [
            "",
            f"## Totals (G{g['n']})  A B C(incl Cmin) Cmin D E ; samples ; >=1 excl Cmin ; "
            ">=1 incl Cmin",
            f"all : _ _ _ _ _ _ ; {len(g['cases'])} ; _ ; _",
            "",
        ]
        (OUT / f"halluc-tally-G{g['n']}.md").write_text("\n".join(lines))


# --- qcheck ----------------------------------------------------------------------


def cmd_qcheck(a: argparse.Namespace) -> int:
    """The quote locator on one or more case files, as DATA-2026-0001's qcheck.py prints."""
    for cid in a.case_ids:
        inputs, answer = I.split_case((OUT / "cases" / f"{cid}.md").read_text())
        corpus = inputs + "\n" + "\n".join((OUT / r).read_text() for r in I.refs_in(inputs))
        print("=====", f"{cid}.md")
        for q in I.quote_candidates(answer, I.qnorm(corpus)):
            print(f"answer [{q['kind']}] MISS: {q['quote']}\n      nearest: {q['nearest']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--data-root", required=True)
    b.add_argument("--dumps-dir", default=str(DEFAULT_DUMPS))
    b.set_defaults(func=cmd_build)
    q = sub.add_parser("qcheck")
    q.add_argument("case_ids", nargs="+")
    q.set_defaults(func=cmd_qcheck)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
