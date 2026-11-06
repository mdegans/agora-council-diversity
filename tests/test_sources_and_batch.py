import json
from pathlib import Path

import pytest

from council_diversity.adapter import ONE_HOUR, adapt, canonical_bytes, sha256
from council_diversity.batch import classify_first_action, record_result
from council_diversity.pricing import estimate_item
from council_diversity.sources import SEATS, ArchiveRow, SourceError, load_request, resolve

ROOT = Path(__file__).resolve().parent.parent


def _req(tag: str) -> dict:
    return {
        "model": "claude-opus-4-6",
        "messages": [{"role": "user", "content": [{"type": "text", "text": tag}]}],
        "max_tokens": 16384,
        "system": [{"type": "text", "text": "s", "cache_control": dict(ONE_HOUR)}],
        "tool_choice": {"type": "auto"},
        "tools": [],
        "thinking": {"type": "adaptive"},
    }


def _row(gov, role, item, ts, digest) -> ArchiveRow:
    return ArchiveRow(gov, role, item, "m", ts, digest, 1, 1, 10, 20, 0, 5)


def _setup(tmp_path: Path, with_dump: bool):
    """Each seat: a recusal question, then Round 1. Export holds the recusal."""
    inputs, dumps, index = tmp_path / "inputs", tmp_path / "dumps", []
    item = "item-uuid"
    for i, seat in enumerate(SEATS):
        rec, r1 = canonical_bytes(_req(f"recusal {seat}")), canonical_bytes(_req(f"r1 {seat}"))
        index += [
            _row("GOV-2026-0099", seat, item, f"2026-10-17T18:0{i}:00Z", sha256(rec)),
            _row("GOV-2026-0099", seat, item, f"2026-10-17T18:1{i}:00Z", sha256(r1)),
        ]
        (inputs / "GOV-2026-0099").mkdir(parents=True, exist_ok=True)
        export = {"role": seat, "digest": sha256(rec), "prompt": json.loads(rec)}
        (inputs / "GOV-2026-0099" / f"{seat}.json").write_text(json.dumps(export))
        if with_dump:
            d = dumps / item
            d.mkdir(parents=True, exist_ok=True)
            for ts, phase, raw in [("180000", "recusal", rec), ("181000", "round1", r1)]:
                name = f"20261017T{ts}.000Z.{seat}.{phase}.{sha256(raw)[:12]}.abcd1234.json"
                (d / name).write_bytes(raw)
            # a later Round 1 turn (after a tool call) must not be chosen
            later = canonical_bytes(_req(f"r1 turn 2 {seat}"))
            (d / f"20261017T182000.000Z.{seat}.round1.{sha256(later)[:12]}.ef.json").write_bytes(
                later
            )
    return inputs, dumps, index


def test_dump_round1_is_chosen_over_the_recusal_export(tmp_path):
    inputs, dumps, index = _setup(tmp_path, with_dump=True)
    sources = resolve(inputs, dumps, index)
    assert len(sources) == 4
    for s in sources:
        assert s.kind == "dump" and ".round1." in s.path and "181000" in s.path
        assert s.export_is_round1 is False
        assert "not Round 1" in s.notes
        body = load_request(s, inputs, dumps)
        assert body["messages"][0]["content"][0]["text"].startswith("r1 ")


def test_ambiguous_archive_without_dump_is_refused(tmp_path):
    inputs, dumps, index = _setup(tmp_path, with_dump=False)
    with pytest.raises(SourceError, match="can't tell"):
        resolve(inputs, dumps, index)


def test_changed_dump_is_detected(tmp_path):
    inputs, dumps, index = _setup(tmp_path, with_dump=True)
    s = resolve(inputs, dumps, index)[0]
    (dumps / s.path).write_bytes(canonical_bytes(_req("tampered")))
    with pytest.raises(SourceError, match="hash changed"):
        load_request(s, inputs, dumps)


# --- first action ------------------------------------------------------------


def test_classify_first_action():
    read = {
        "content": [{"type": "thinking"}, {"type": "tool_use", "name": "get_content"}],
        "stop_reason": "tool_use",
    }
    assert classify_first_action(read)["first_action"] == "read"
    pos = {"content": [{"type": "tool_use", "name": "position"}], "stop_reason": "tool_use"}
    assert classify_first_action(pos)["first_action"] == "position"
    fmt = {
        "content": [
            {"type": "thinking"},
            {"type": "text", "text": json.dumps({"submission": "position"})},
        ],
        "stop_reason": "end_turn",
    }
    c = classify_first_action(fmt)
    assert c["first_action"] == "position" and c["submission"] == "position"
    assert classify_first_action({"content": [], "stop_reason": "refusal"})["first_action"] == (
        "refusal"
    )


def test_record_result_never_overwrites(tmp_path):
    line = json.dumps(
        {
            "custom_id": "haiku55-GOV-2026-0006-artist-s01",
            "result": {
                "type": "succeeded",
                "message": {
                    "id": "msg_1",
                    "model": "claude-haiku-5-5",
                    "stop_reason": "refusal",
                    "stop_details": {"type": "refusal", "category": "reasoning_extraction"},
                    "content": [],
                    "usage": {"input_tokens": 1},
                },
            },
        }
    ).encode()
    p = tmp_path / "sample-01.response.json"
    meta = record_result(line, p)
    assert meta["refusal_category"] == "reasoning_extraction"
    assert meta["first_action"] == "refusal"
    assert meta["response_sha256"] == sha256(p.read_bytes())
    with pytest.raises(FileExistsError):
        record_result(line, p)


def test_errored_result_goes_elsewhere(tmp_path):
    line = json.dumps(
        {
            "custom_id": "x",
            "result": {
                "type": "errored",
                "error": {"type": "error", "error": {"type": "overloaded_error"}},
            },
        }
    )
    p = tmp_path / "sample-01.response.json"
    meta = record_result(line.encode(), p)
    assert not p.exists() and meta["error_type"] == "overloaded_error"


# --- pricing -------------------------------------------------------------------


def test_opus_estimate_by_hand():
    # 40k prompt, 30k cached prefix, 2 samples, 4k out, batch half price
    e = estimate_item("claude-opus-4-6", 40_000, 100, 100, 75, 2)
    first = 30_000 * 10 + 10_000 * 5 + 4000 * 25
    later = 30_000 * 0.5 + 10_000 * 5 + 4000 * 25
    assert e.usd == pytest.approx((first + later) / 1e6 / 2)


def test_haiku_tier_switches_above_100k():
    low = estimate_item("claude-haiku-5-5", 70_000, 1, 1, 1, 1)  # 91k after factor
    high = estimate_item("claude-haiku-5-5", 80_000, 1, 1, 1, 1)  # 104k
    assert low.tier == "base" and high.tier == ">100k"


# --- the real inputs, when present (never printed) -----------------------------

SOURCES = ROOT / "data" / "round1-sources.json"
DUMPS = Path("/home/claude-agora/Projects/agora/secrets/council-requests")
real = pytest.mark.skipif(
    not (SOURCES.exists() and (ROOT / "inputs").is_dir() and DUMPS.is_dir()),
    reason="inputs are not in this checkout",
)


@real
@pytest.mark.parametrize("model", ["claude-opus-4-6", "claude-haiku-5-5"])
def test_every_real_source_adapts(model):
    from council_diversity.sources import Source

    sources = [Source(**s) for s in json.loads(SOURCES.read_text())["sources"]]
    assert len(sources) == 52
    for s in sources:
        orig = load_request(s, ROOT / "inputs", DUMPS)
        a = adapt(orig, model)
        if a.fmt == "new" and model == "claude-opus-4-6" and s.kind == "dump":
            # the replay is the request the seat was sent, byte for byte
            assert sha256(a.payload) == s.archive_digest
        if s.kind == "dump":
            assert sha256(canonical_bytes(orig)) == s.archive_digest
