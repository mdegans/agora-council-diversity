import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from council_diversity.adapter import canonical_bytes, sha256
from council_diversity.batch import classify_first_action, usage_cost

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "replay_round1.py"


@pytest.fixture
def rr(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("replay_round1", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod


def test_max_tokens_is_truncated_even_with_a_tool_call():
    # seen 2026-11-04: a position call cut off with an empty input
    msg = {
        "content": [{"type": "thinking"}, {"type": "tool_use", "name": "position", "input": {}}],
        "stop_reason": "max_tokens",
    }
    c = classify_first_action(msg)
    assert c["first_action"] == "truncated" and c["tool"] == "position"


def test_usage_cost_by_hand():
    u = {
        "input_tokens": 50_000,
        "cache_read_input_tokens": 20_000,
        "cache_creation_input_tokens": 10_000,
        "cache_creation": {"ephemeral_1h_input_tokens": 10_000, "ephemeral_5m_input_tokens": 0},
        "output_tokens": 4000,
    }
    # 80k prompt: Haiku base card, batch half price
    want = (50_000 * 0.10 + 20_000 * 0.01 + 10_000 * 0.20 + 4000 * 0.50) / 1e6 / 2
    assert usage_cost("claude-haiku-5-5", u) == pytest.approx(want)
    u["input_tokens"] = 80_000  # 110k prompt: the higher card
    want = (80_000 * 0.50 + 20_000 * 0.05 + 10_000 * 1.00 + 4000 * 2.50) / 1e6 / 2
    assert usage_cost("claude-haiku-5-5", u) == pytest.approx(want)


def _layout(mod, tmp_path):
    run_dir = tmp_path / "out" / "replay-claude-haiku-5-5"
    body = {"model": "claude-haiku-5-5", "max_tokens": 16384, "messages": []}
    payload = canonical_bytes(body)
    items, samples = [], {}
    for seat, stop, action in [
        ("artist", "max_tokens", "truncated"),
        ("lawyer", "end_turn", "position"),
        ("engineer", "max_tokens", "truncated"),
    ]:
        d = run_dir / "GOV-2026-0008" / seat
        d.mkdir(parents=True)
        (d / "request.json").write_bytes(payload)
        (d / "sample-01.response.json").write_bytes(b"original")
        items.append(
            {
                "decision": "GOV-2026-0008",
                "seat": seat,
                "request_file": str((d / "request.json").relative_to(tmp_path)),
                "request_sha256": sha256(payload),
            }
        )
        samples[f"haiku55-GOV-2026-0008-{seat}-s01"] = {
            "stop_reason": stop,
            "first_action": action,
            "model": "claude-haiku-5-5",
            "usage": {"input_tokens": 1000, "output_tokens": 16384},
        }
    mod.save_manifest(run_dir, {"batches": [], "samples": samples})
    return run_dir, items


def _args(**kw):
    base = dict(
        rerun=["first_action=truncated"],
        ids=None,
        max_tokens=32768,
        tag=None,
        dry_run=True,
        max_usd=None,
        key_file=Path("/nonexistent"),
    )
    return argparse.Namespace(**base | kw)


def test_rerun_dry_run_writes_request_and_diff_only(rr, tmp_path):
    run_dir, items = _layout(rr, tmp_path)
    plan = rr.PLANS["haiku55"]
    assert rr.cmd_rerun(_args(), plan, run_dir, items) == 0
    d = run_dir / "GOV-2026-0008" / "artist"
    body = json.loads((d / "request.rerun-32k.json").read_bytes())
    assert body["max_tokens"] == 32768
    diff = json.loads((d / "request.rerun-32k.diff.json").read_text())
    assert [x["path"] for x in diff] == ["/max_tokens"]
    assert (d / "sample-01.response.json").read_bytes() == b"original"
    assert not (run_dir / "GOV-2026-0008" / "lawyer" / "request.rerun-32k.json").exists()
    assert not list(run_dir.rglob("*.rerun-32k.response.json"))


def test_ids_restrict_and_must_match(rr, tmp_path):
    run_dir, _ = _layout(rr, tmp_path)
    m = rr.load_manifest(run_dir)
    assert rr.select_samples(m, ["first_action=truncated"], []) == [
        "haiku55-GOV-2026-0008-artist-s01",
        "haiku55-GOV-2026-0008-engineer-s01",
    ]
    assert rr.select_samples(m, [], ["haiku55-GOV-2026-0008-artist-s01"]) == [
        "haiku55-GOV-2026-0008-artist-s01"
    ]
    with pytest.raises(SystemExit):
        rr.select_samples(m, ["first_action=truncated"], ["haiku55-GOV-2026-0008-lawyer-s01"])


def test_rerun_send_keeps_originals_and_records_change(rr, tmp_path, monkeypatch):
    run_dir, items = _layout(rr, tmp_path)
    sent = {}

    class FakeClient:
        def __init__(self, key):
            pass

        def create(self, requests):
            sent["requests"] = requests
            return {"id": "msgbatch_fake", "created_at": "t"}

        def wait(self, batch_id):
            return {"results_url": "u", "ended_at": "t2"}

        def results(self, url):
            for r in sent["requests"]:
                yield json.dumps(
                    {
                        "custom_id": r["custom_id"],
                        "result": {
                            "type": "succeeded",
                            "message": {
                                "id": "m",
                                "model": "claude-haiku-5-5",
                                "stop_reason": "end_turn",
                                "content": [{"type": "tool_use", "name": "position", "input": {}}],
                                "usage": {"input_tokens": 1000, "output_tokens": 20000},
                            },
                        },
                    }
                ).encode()

    monkeypatch.setattr(rr, "BatchClient", FakeClient)
    monkeypatch.setattr(rr, "read_key", lambda p: "k")
    assert (
        rr.cmd_rerun(
            _args(dry_run=False, ids="haiku55-GOV-2026-0008-artist-s01"),
            rr.PLANS["haiku55"],
            run_dir,
            items,
        )
        == 0
    )
    assert [r["custom_id"] for r in sent["requests"]] == ["haiku55-GOV-2026-0008-artist-s01-rr32k"]
    assert sent["requests"][0]["params"]["max_tokens"] == 32768
    d = run_dir / "GOV-2026-0008" / "artist"
    assert (d / "sample-01.response.json").read_bytes() == b"original"
    assert (d / "sample-01.rerun-32k.response.json").exists()
    m = rr.load_manifest(run_dir)
    meta = m["samples"]["haiku55-GOV-2026-0008-artist-s01-rr32k"]
    assert meta["rerun_of"] == "haiku55-GOV-2026-0008-artist-s01"
    assert meta["change"] == {"max_tokens": [16384, 32768]}
    assert meta["first_action"] == "position" and meta["usd_actual"] > 0
    assert m["samples"]["haiku55-GOV-2026-0008-artist-s01"]["first_action"] == "truncated"
    # a second run sends nothing: never overwrite
    sent.clear()
    rr.cmd_rerun(
        _args(dry_run=False, ids="haiku55-GOV-2026-0008-artist-s01"),
        rr.PLANS["haiku55"],
        run_dir,
        items,
    )
    assert "requests" not in sent
