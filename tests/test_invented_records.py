"""The invented-record case files (analysis/invented-records/).

Checks that the allowlist keeps only text, tool calls' named fields and tool
results; that case files are blinded; that the units are the published ones;
and that the candidates rebuild byte for byte from the case files.

The recorded positions' sources (the sittings' DATA sets) are outside this
repo. Tests that read them run when INVREC_DATA_ROOT names a directory of
checkouts (see scripts/invented_records.py), and skip otherwise.
"""

import ast
import json
import os
import re
from collections import Counter
from pathlib import Path

import pytest

from council_diversity import invrec as I

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "analysis" / "invented-records"
KEY = I.read_key(OUT / "key.csv")
DATA_ROOT = os.environ.get("INVREC_DATA_ROOT")
DUMPS = Path.home() / "Projects" / "agora" / "secrets" / "council-requests"

MODEL_NAMES = re.compile(r"(?i)haiku|opus|sonnet|fable|qwen|claude-[a-z]|\b4\.6\b|\b5\.5\b")


def _case(cid: str) -> str:
    return (OUT / "cases" / f"{cid}.md").read_text()


def _hidden_strings(content, min_len: int = 40) -> list[str]:
    """Every string under a block or input field the allowlist drops."""
    out: list[str] = []

    def walk(v):
        if isinstance(v, str):
            if len(v) >= min_len:
                out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    if not isinstance(content, list):
        return out
    for b in content:
        if b.get("type") not in I.KEPT_BLOCKS:
            walk(b)
        elif b.get("type") == "tool_use":
            for k, v in (b.get("input") or {}).items():
                if k not in I.TOOL_INPUT_KEYS:
                    walk(v)
    return out


def _probes(s: str) -> list[str]:
    """Three 50-character slices of a hidden string, to look for in a case file."""
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < 60:
        return [s] if len(s) >= 40 else []
    mid = len(s) // 2
    return [s[:50], s[mid : mid + 50], s[-50:]]


def _visible(content) -> str:
    """The allowlisted text of a message's content, whitespace-normalized."""
    return re.sub(r"\s+", " ", json.dumps(I.kept_blocks(content), ensure_ascii=False)) + re.sub(
        r"\s+", " ", " ".join(_kept_strings(I.kept_blocks(content)))
    )


def _kept_strings(blocks: list[dict]) -> list[str]:
    out = []
    for b in blocks:
        if b["type"] == "text":
            out.append(b["text"])
        elif b["type"] == "tool_result":
            out += b["texts"]
        elif b["type"] == "tool_use":
            out += [v for v in b["input"].values() if isinstance(v, str)]
            out += [
                q.get("question", "") if isinstance(q, dict) else str(q)
                for q in b["input"].get("questions") or []
            ]
        if b["type"] == "text":
            v = I._text_json(b["text"])
            if v:
                out += [x for k, x in v.items() if k in I.ANSWER_KEYS and isinstance(x, str)]
                out += [str(q) for q in v.get("questions") or [] if isinstance(q, str)]
    return out


def _leaks(hidden: list[str], body: str, visible: str) -> bool:
    """True if a slice of dropped content is in the case file but in no kept text.

    Returns a bool only, so a failure never prints the dropped text. Thinking
    often quotes what the seat then wrote, so a slice that is also in the kept
    text (the seat's own visible words or its inputs) is not a leak.
    """
    for h in hidden:
        for p in _probes(h):
            if p in body and p not in visible:
                return True
    return False


# --- the allowlist, on synthetic input ---------------------------------------


def test_allowlist_drops_every_unlisted_block_and_field():
    secret = "SECRET-MARKER-" + "x" * 40
    content = [
        {"type": "x_hidden_block", "x_body": secret},
        {"type": "text", "text": "visible text"},
        {
            "type": "tool_use",
            "name": "position",
            "input": {"position": "p", "questions": ["q?"], "vote": "yes", "x_private": secret},
        },
        {
            "type": "tool_result",
            "content": [{"type": "text", "text": "result"}, {"type": "x", "x": secret}],
        },
    ]
    kept = I.kept_blocks(content)
    assert secret not in json.dumps(kept)
    assert [b["type"] for b in kept] == ["text", "tool_use", "tool_result"]
    assert set(kept[1]["input"]) == {"position", "questions", "vote"}
    assert I.answer_of(content[2]) == {"position": "p", "questions": ["q?"], "vote": "yes"}
    turn = I.render_turn({"role": "assistant", "content": kept}, 2)
    assert secret not in turn and "visible text" in turn


def test_structured_answer_keeps_only_position_questions_vote():
    secret = "SECRET-" + "y" * 40
    block = {
        "type": "text",
        "text": json.dumps(
            {
                "submission": "position",
                "position": "p",
                "questions": [],
                "vote": "no",
                "x_other": secret,
            }
        ),
    }
    assert I.is_position(block)
    ans = I.answer_of(block)
    assert set(ans) == {"position", "questions", "vote"}
    assert secret not in I.render_answer(ans)


# --- the real case files --------------------------------------------------------


def test_units_are_the_published_ones():
    by = Counter(r["source"] for r in KEY.values())
    assert by == {"haiku55": 120, "opus46a": 43, "recorded": 52}
    assert len(list((OUT / "cases").glob("*.md"))) == 215
    assert len(list((OUT / "candidates").glob("*.md"))) == 215
    # Same rule as concerns.replay_positions: the latest result per sample.
    for run, n in (("haiku55", 120), ("opus46a", 43)):
        samples, status = I.replay_samples(ROOT, run)
        assert len(samples) == n
        assert status["position"] == n
    # Every seat-item's recorded position, once.
    rec = Counter((r["item"], r["seat"]) for r in KEY.values() if r["source"] == "recorded")
    assert len(rec) == 52 and set(rec.values()) == {1}


def test_replay_cases_hold_nothing_the_allowlist_drops():
    for run in I.RUNS:
        rdir = ROOT / "out" / I.RUNS[run]
        manifest = json.loads((rdir / "manifest.json").read_bytes())
        by_key = {(r["source"], r["item"], r["seat"], r["sample"]): c for c, r in KEY.items()}
        for base, meta in I.latest_samples(manifest).items():
            if meta.get("first_action") != "position":
                continue
            stem = base.split("-", 1)[1]
            item, rest = stem[:13], stem[14:]
            seat, n = rest.rsplit("-s", 1)
            cid = by_key[(run, item, seat, f"s{int(n):02d}")]
            body = re.sub(r"\s+", " ", _case(cid))
            resp = json.loads((rdir / item / seat / Path(meta["response_file"]).name).read_bytes())
            hidden = _hidden_strings(resp["result"]["message"]["content"])
            for block in resp["result"]["message"]["content"]:
                if I.is_position(block) and block["type"] == "text":
                    parsed = json.loads(block["text"])
                    hidden += [
                        v
                        for k, v in parsed.items()
                        if k not in I.ANSWER_KEYS and isinstance(v, str) and len(v) >= 40
                    ]
            visible = _visible(resp["result"]["message"]["content"]) + _visible(
                json.loads((rdir / item / seat / "request.json").read_bytes())["messages"][0][
                    "content"
                ]
            )
            assert not _leaks(hidden, body, visible), f"{cid}: dropped content in the case file"


@pytest.mark.skipif(not DATA_ROOT, reason="INVREC_DATA_ROOT not set")
def test_recorded_cases_hold_nothing_the_allowlist_drops():
    by_key = {(r["item"], r["seat"]): c for c, r in KEY.items() if r["source"] == "recorded"}
    for item in I.ITEMS:
        calls = (
            I.gov18_calls(ROOT, DUMPS)
            if item == I.GOV18
            else I.sitting_calls(Path(DATA_ROOT), item)
        )
        for seat in I.SEATS:
            body = re.sub(r"\s+", " ", _case(by_key[(item, seat)]))
            hidden: list[str] = []
            visible = ""
            for c in calls:
                if c.who != seat:
                    continue
                contents = [m.get("content") for m in c.request.get("messages") or []]
                if c.response:
                    contents.append(c.response.get("content"))
                for content in contents:
                    hidden += _hidden_strings(content)
                    visible += _visible(content)
            assert not _leaks(hidden, body, visible), f"{item} {seat}: dropped content in case"


def test_case_files_are_blinded():
    for cid, row in KEY.items():
        body = _case(cid)
        inputs, answer = I.split_case(body)
        # The formatter's own lines (title, item, seat, references) name no model.
        head = inputs.split("### Turn 1:", 1)[0]
        assert not MODEL_NAMES.search(head), cid
        assert row["model"] not in head
        for f in ("key.csv", "replay-claude", "out/", "manifest.json", "DATA-2026-00"):
            assert f not in head
    # Candidates add only the answer's own text around their findings.
    for cid in KEY:
        cand = (OUT / "candidates" / f"{cid}.md").read_text()
        head = cand.split("## B:", 1)[0]
        assert not MODEL_NAMES.search(head)


def test_model_names_in_a_replay_case_are_the_seats_own_words_or_its_inputs():
    """A name may appear (the items discuss the Council's models); only where the source has it."""
    for run in I.RUNS:
        samples, _ = I.replay_samples(ROOT, run)
        by_key = {(r["source"], r["item"], r["seat"], r["sample"]): c for c, r in KEY.items()}
        for s in samples:
            body = _case(by_key[(run, s.item, s.seat, s.sample)])
            src = I.render_answer(s.answer) + json.dumps(
                s.request["messages"][0]["content"], ensure_ascii=False
            )
            assert len(MODEL_NAMES.findall(body)) <= len(MODEL_NAMES.findall(src))


def test_common_texts_are_identical_for_every_case_that_cites_them():
    manifest = json.loads((OUT / "manifest.json").read_text())
    for rel, digest in manifest["common"].items():
        import hashlib

        assert hashlib.sha256((OUT / rel).read_bytes()).hexdigest() == digest


def test_candidates_rebuild_from_the_case_files():
    def read(rel: str) -> str:
        return (OUT / rel).read_text()

    for cid in KEY:
        text, _ = I.case_candidates(cid, _case(cid), read)
        assert text == (OUT / "candidates" / f"{cid}.md").read_text(), cid


def test_groups_keep_seat_items_together_and_mix_sources():
    groups = (OUT / "groups.md").read_text()
    where: dict[str, str] = {}
    parts = re.split(r"^## (G\d+)$", groups, flags=re.M)
    for g, body in zip(parts[1::2], parts[2::2], strict=True):
        for cid in re.findall(r"`(c[0-9a-z]{6})`", body):
            where[cid] = g
    assert set(where) == set(KEY)
    bundles: dict[tuple, set[str]] = {}
    for cid, r in KEY.items():
        bundles.setdefault((r["item"], r["seat"]), set()).add(where[cid])
    assert all(len(v) == 1 for v in bundles.values())
    by_group: dict[str, set[str]] = {}
    for cid, g in where.items():
        by_group.setdefault(g, set()).add(KEY[cid]["source"])
    assert all(len(v) >= 2 for v in by_group.values())


# --- the quote locator is DATA-2026-0001's ---------------------------------------


def _original_qcheck_functions() -> dict:
    src = (OUT / "tools" / "qcheck.py").read_text()
    tree = ast.parse(src)
    defs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("norm", "frags")]
    ns: dict = {"re": re}
    exec(compile(ast.Module(body=defs, type_ignores=[]), "qcheck", "exec"), ns)
    return ns


def test_port_matches_the_original_norm_and_frags():
    ns = _original_qcheck_functions()
    samples = [
        "The “Council” — Art. V § 1… shall *not* `act` > now",
        "‘Quoted’ text – with \\ backslash … and [...] more words here",
        "Short",
        "a b c ... d e f … g h i",
    ]
    for s in samples:
        assert I.qnorm(s) == ns["norm"](s)
        assert I.qfrags(s) == ns["frags"](s)


def test_citations_exist_or_not():
    cons = (
        "## Article II — Rights\n\n1. **A**\n2. **B**\n\n## Article V — Content\n\n"
        "### Section 1: Prohibited\n\n1. x\n2. y\n3. z\n\n### Section 2: Permitted\n\n"
    )
    st = I.constitution_structure(cons)
    found = {
        c["as"]: c["status"]
        for c in I.citation_candidates(
            "Art. V § 1.3; Article V.1.4; Art. II § 2; Art. II §6; Article IX; Art. V, item 2",
            st,
            set(),
        )
    }
    assert found["Art. V § 1.3"] == "exists"
    assert found["Art. V § 1.4"].startswith("no such")
    assert found["Art. II § 2"].startswith("exists")
    assert found["Art. II § 6"].startswith("no such")
    assert found["Art. IX"] == "no such article"


def test_id_candidates():
    inputs = "see GOV-2026-0006 and proposal 6b67abe7"
    got = [s for s, _ in I.id_candidates("GOV-2026-6, GOV-2026-0099, 6b67abe7, deadbee5", inputs)]
    assert got == ["GOV-2026-0099", "deadbee5"]
