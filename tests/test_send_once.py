import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "send_once.py"


def load():
    spec = importlib.util.spec_from_file_location("send_once", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def old_body() -> dict:
    return {
        "model": "claude-opus-4-6",
        "temperature": 0.5,
        "tool_choice": {"type": "auto"},
        "tools": [
            {
                "name": "submit_vote",
                "input_schema": {
                    "type": "object",
                    "properties": {"reasoning": {"type": "string"}, "vote": {"type": "string"}},
                    "required": ["reasoning", "vote"],
                },
            }
        ],
        "messages": [],
    }


def test_a_claude_model_gets_explicit_thinking_and_no_reasoning_field():
    body = old_body()
    stripped: list[str] = []
    load().apply_model(body, "claude-haiku-5-5", stripped)
    assert body["model"] == "claude-haiku-5-5"
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "temperature" not in body
    schema = body["tools"][0]["input_schema"]
    assert list(schema["properties"]) == ["vote"]
    assert schema["required"] == ["vote"]
    assert stripped == ["tools[submit_vote]"]


def test_a_budgeted_or_hidden_thinking_is_replaced():
    body = old_body()
    body["thinking"] = {"type": "adaptive"}
    load().apply_model(body, "claude-opus-5-5", [])
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_a_local_model_only_changes_its_name():
    body = old_body()
    stripped: list[str] = []
    load().apply_model(body, "qwen3.8-35b-a3b", stripped)
    assert body["model"] == "qwen3.8-35b-a3b"
    assert "thinking" not in body
    assert body["temperature"] == 0.5
    assert "reasoning" in body["tools"][0]["input_schema"]["properties"]
    assert stripped == []
