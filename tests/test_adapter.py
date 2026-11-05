import copy
import json

import pytest

from council_diversity.adapter import (
    ONE_HOUR,
    AdapterError,
    adapt,
    canonical_bytes,
    check,
    detect_format,
    display_omitted_by_default,
    explicit_thinking,
    forbidden_properties,
    prefix_chars,
    ref_paths,
    restore_property_order,
    strip_reasoning_properties,
    structural_diff,
)


def _schema(props: dict, required: list) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def old_request() -> dict:
    """An old-format (pre 2026-10-07) Round 1 request, keys in jsonb order."""
    s = {"type": "string", "description": "x"}
    vote = {"type": "string", "enum": ["yes", "no", "abstain", "defer"]}
    return {
        "model": "claude-opus-4-6",
        "tools": [
            {
                "name": "position",
                "strict": True,
                "description": "d",
                # jsonb order: shortest key first, so not declaration order
                "input_schema": _schema(
                    {"vote": vote, "position": s, "questions": {"type": "array"}, "reasoning": s},
                    ["reasoning", "position", "questions", "vote"],
                ),
            },
            {
                "name": "ask_clerk",
                "strict": True,
                "description": "d",
                "input_schema": _schema(
                    {"reasoning": s, "comment_numbers": {"type": "array"}},
                    ["reasoning", "comment_numbers"],
                ),
            },
        ],
        "system": [
            {"type": "text", "text": "role"},
            {"type": "text", "text": "constitution", "cache_control": dict(ONE_HOUR)},
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "item", "cache_control": dict(ONE_HOUR)},
                    {"type": "text", "text": "rest"},
                ],
            }
        ],
        "max_tokens": 4096,
        "temperature": 0.5,
        "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
    }


def new_request() -> dict:
    """A new-format (from 2026-10-17) Round 1 request, in the order the Council writes."""
    fmt_schema = {
        "anyOf": [
            _schema(
                {
                    "submission": {"type": "string", "const": "position"},
                    "position": {"type": "string"},
                    "questions": {"type": "array", "items": {"type": "string"}},
                    "vote": {"type": "string", "enum": ["yes", "no", "abstain", "defer"]},
                },
                ["submission", "position", "questions", "vote"],
            )
        ]
    }
    return {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "item", "cache_control": dict(ONE_HOUR)},
                    {"type": "text", "text": "ünïcode rest"},
                ],
            }
        ],
        "max_tokens": 16384,
        "system": [
            {"type": "text", "text": "role"},
            {"type": "text", "text": "constitution", "cache_control": dict(ONE_HOUR)},
        ],
        "tool_choice": {"type": "auto"},
        "tools": [
            {
                "name": "read_proposal",
                "strict": True,
                "description": "d",
                "input_schema": _schema({"number": {"type": "integer"}}, ["number"]),
            },
            {
                "name": "get_content",
                "description": "d",
                "input_schema": _schema({"id": {"type": "string"}}, ["id"]),
            },
        ],
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": "high",
            "format": {"type": "json_schema", "schema": fmt_schema},
        },
    }


# --- property order --------------------------------------------------------


def test_restore_property_order_follows_required():
    req = old_request()
    n = restore_property_order(req)
    assert n == 1  # ask_clerk was already in order
    props = req["tools"][0]["input_schema"]["properties"]
    assert list(props) == ["reasoning", "position", "questions", "vote"]
    # idempotent
    assert restore_property_order(req) == 0


def test_restore_property_order_reaches_nested_anyof():
    schema = {"anyOf": [_schema({"b": {}, "a": {}}, ["a", "b"])]}
    restore_property_order(schema)
    assert list(schema["anyOf"][0]["properties"]) == ["a", "b"]


def test_restore_keeps_properties_not_in_required_at_the_end():
    s = _schema({"z": {}, "opt": {}, "a": {}}, ["a", "z"])
    restore_property_order(s)
    assert list(s["properties"]) == ["a", "z", "opt"]


# --- format detection --------------------------------------------------------


def test_detects_both_formats():
    assert detect_format(old_request()) == "old"
    assert detect_format(new_request()) == "new"


def test_mixed_format_is_refused():
    req = old_request()
    req["thinking"] = {"type": "adaptive"}
    with pytest.raises(AdapterError):
        detect_format(req)


# --- old format --------------------------------------------------------------


def test_old_format_adaptation():
    orig = old_request()
    restore_property_order(orig)
    before = copy.deepcopy(orig)
    a = adapt(orig, "claude-haiku-5-5")
    assert orig == before, "adapt must not modify its input"
    r = a.body
    assert a.fmt == "old"
    assert r["model"] == "claude-haiku-5-5"
    assert "temperature" not in r
    assert r["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert r["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert r["output_config"] == {"effort": "high"}
    assert r["max_tokens"] == 16384
    pos = r["tools"][0]
    assert pos["strict"] is True
    assert pos["input_schema"]["additionalProperties"] is False
    assert list(pos["input_schema"]["properties"]) == ["position", "questions", "vote"]
    assert pos["input_schema"]["required"] == ["position", "questions", "vote"]
    assert r["tools"][1]["input_schema"]["required"] == ["comment_numbers"]
    # everything else untouched
    assert r["system"] == orig["system"] and r["messages"] == orig["messages"]


def test_old_format_on_opus_46_also_gets_summarized_display():
    orig = old_request()
    restore_property_order(orig)
    r = adapt(orig, "claude-opus-4-6").body
    assert r["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_old_format_without_disable_parallel_does_not_invent_it():
    orig = old_request()
    orig["tool_choice"] = {"type": "any"}
    assert adapt(orig, "claude-haiku-5-5").body["tool_choice"] == {"type": "auto"}


# --- new format --------------------------------------------------------------


def test_new_format_opus_changes_nothing_but_model():
    orig = new_request()
    orig["model"] = "claude-opus-4-5"  # so the model change is visible
    a = adapt(orig, "claude-opus-4-6")
    diff = structural_diff(orig, a.body)
    assert [d["path"] for d in diff] == ["/model"]
    expected = canonical_bytes(orig).replace(b'"claude-opus-4-5"', b'"claude-opus-4-6"')
    assert a.payload == expected


def test_new_format_same_model_is_byte_identical():
    orig = new_request()
    a = adapt(orig, "claude-opus-4-6")
    assert a.payload == canonical_bytes(orig)
    assert structural_diff(orig, a.body) == []


def test_new_format_haiku_changes_model_and_display_only():
    orig = new_request()
    a = adapt(orig, "claude-haiku-5-5")
    paths = sorted(d["path"] for d in structural_diff(orig, a.body))
    assert paths == ["/model", "/thinking/display"]
    assert a.body["thinking"] == {"type": "adaptive", "display": "summarized"}


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-fable-5-1",
        "claude-opus-4-7",
        "claude-something-7",
    ],
)
def test_new_format_asks_for_the_summary_on_every_model_that_hides_it(model):
    orig = new_request()
    a = adapt(orig, model)
    paths = sorted(d["path"] for d in structural_diff(orig, a.body))
    assert paths == ["/model", "/thinking/display"]
    assert a.body["thinking"]["display"] == "summarized"


@pytest.mark.parametrize(
    ("model", "omitted"),
    [
        ("claude-haiku-5-5", True),
        ("claude-opus-5-5", True),
        ("claude-sonnet-5-5", True),
        ("claude-fable-5-1", True),
        ("claude-opus-4-7", True),
        ("claude-opus-4-8", True),
        ("claude-something-7", True),
        ("claude-opus-4-6", False),
        ("claude-sonnet-4-6", False),
        ("claude-haiku-4-5-20251001", False),
        ("claude-opus-4", False),
        ("qwen3.8-35b-a3b", False),
    ],
)
def test_display_default_by_model(model, omitted):
    assert display_omitted_by_default(model) is omitted


def test_a_hidden_display_is_refused_on_any_model_that_hides_it():
    req = new_request()
    req["model"] = "claude-opus-5-5"
    req["thinking"] = {"type": "adaptive"}
    with pytest.raises(AdapterError, match="display summarized"):
        check(req)


def test_strip_reasoning_covers_tools_and_the_response_format():
    req = old_request()
    req["output_config"] = {
        "format": {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {"reasoning": {"type": "string"}, "vote": {"type": "string"}},
                "required": ["reasoning", "vote"],
            },
        }
    }
    removed = strip_reasoning_properties(req)
    assert "output_config.format" in removed
    assert removed and all(name for name in removed)
    assert forbidden_properties(req) == []
    assert req["output_config"]["format"]["schema"]["required"] == ["vote"]


def test_explicit_thinking_is_adaptive_and_shown_on_claude_only():
    for model in ("claude-haiku-5-5", "claude-opus-4-6", "claude-opus-5-5"):
        assert explicit_thinking(model) == {"type": "adaptive", "display": "summarized"}
    assert explicit_thinking("qwen3.8-35b-a3b") is None


def test_stream_flag_is_dropped():
    orig = new_request()
    orig["stream"] = False
    assert "stream" not in adapt(orig, "claude-opus-4-6").body


# --- invariants --------------------------------------------------------------


@pytest.mark.parametrize("make", [old_request, new_request])
@pytest.mark.parametrize("model", ["claude-opus-4-6", "claude-haiku-5-5"])
def test_no_reasoning_like_property_survives(make, model):
    orig = make()
    restore_property_order(orig)
    r = adapt(orig, model).body
    assert forbidden_properties(r) == []
    assert ref_paths(r) == []
    blob = json.dumps([t["input_schema"] for t in r["tools"]] + [r.get("output_config")])
    for word in ("reasoning", "rationale", "thinking", "scratchpad"):
        assert f'"{word}"' not in blob


@pytest.mark.parametrize(
    "name", ["rationale", "thinking", "my_reasoning", "thoughts", "scratchpad"]
)
def test_reasoning_like_property_in_output_schema_is_refused(name):
    req = new_request()
    branch = req["output_config"]["format"]["schema"]["anyOf"][0]
    branch["properties"][name] = {"type": "string"}
    branch["required"].append(name)
    with pytest.raises(AdapterError, match="reasoning-like"):
        adapt(req, "claude-haiku-5-5")


def test_ref_is_refused():
    req = new_request()
    req["tools"][0]["input_schema"]["properties"]["number"] = {"$ref": "#/$defs/N"}
    req["tools"][0]["input_schema"]["$defs"] = {"N": {"type": "integer"}}
    assert len(ref_paths(req)) == 2
    with pytest.raises(AdapterError, match=r"\$ref"):
        adapt(req, "claude-opus-4-6")


def test_haiku_with_temperature_is_refused():
    req = new_request()
    req["model"] = "claude-haiku-5-5"
    req["thinking"]["display"] = "summarized"
    req["temperature"] = 0.5
    with pytest.raises(AdapterError, match="sampling"):
        check(req)


def test_last_system_block_gets_one_hour_cache():
    req = new_request()
    req["system"][-1]["cache_control"] = {"type": "ephemeral"}
    a = adapt(req, "claude-opus-4-6")
    assert a.body["system"][-1]["cache_control"] == ONE_HOUR
    assert a.notes


# --- diff and accounting -----------------------------------------------------


def test_diff_never_carries_long_text():
    a = {"system": [{"text": "x" * 500}], "properties": {"a": 1, "b": 2}}
    b = {"system": [{"text": "y" * 500}], "properties": {"b": 2, "a": 1}}
    diff = structural_diff(a, b)
    assert "xxxx" not in json.dumps(diff) and "yyyy" not in json.dumps(diff)
    assert {d["op"] for d in diff} == {"changed", "reordered"}


def test_prefix_chars_stops_at_last_breakpoint():
    req = new_request()
    pre, total = prefix_chars(req)
    tail = len(canonical_bytes(req["messages"][0]["content"][1]))
    assert pre == total - tail
