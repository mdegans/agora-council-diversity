"""Send replay requests through the Anthropic Message Batches API.

First action only: one call per sample, no tool results sent back. Responses
are saved whole and hashed; only metadata is ever printed.

Cache priming: requests that share a prefix inside one batch don't prime each
other, so sample 1 of every seat-item goes in batch 1, and the remaining
samples go in batch 2 once batch 1 has ended, to read its 1h cache.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from .adapter import sha256

API = "https://api.anthropic.com/v1/messages/batches"
API_VERSION = "2023-06-01"  # what the Council's client (misanthropic) sends

POSITION_TOOLS = {"position", "respond", "vote", "rank"}
READ_TOOLS = {"get_content", "read_proposal", "ask_clerk", "witness_brief"}
POSITION_SUBMISSIONS = {"position", "respond", "vote"}


def classify_first_action(message: dict) -> dict:
    """What the seat did first, from block types, tool names and the
    structured submission's discriminator. Never returns text."""
    blocks = message.get("content", [])
    types = [b.get("type") for b in blocks]
    tools = [b.get("name") for b in blocks if b.get("type") == "tool_use"]
    stop = message.get("stop_reason")
    submission = None
    if stop == "refusal":
        action = "refusal"
    elif stop == "max_tokens":
        # Cut off: a tool call or submission begun here may be incomplete
        # (an empty position input was seen on 2026-11-04), so it is never
        # counted as an action.
        action = "truncated"
    elif tools:
        name = tools[0]
        action = "position" if name in POSITION_TOOLS else "read" if name in READ_TOOLS else "other"
    else:
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError:
            parsed = None
        submission = parsed.get("submission") if isinstance(parsed, dict) else None
        if submission in POSITION_SUBMISSIONS:
            action = "position"
        else:
            action = "other"
    return {
        "first_action": action,
        "tool": tools[0] if tools else None,
        "submission": submission,
        "block_types": types,
    }


def usage_cost(model: str, usage: dict, batch: bool = True) -> float:
    """Actual USD for one response, from its usage block and the price card."""
    from .pricing import BATCH_DISCOUNT, card

    inp = usage.get("input_tokens", 0)
    read = usage.get("cache_read_input_tokens", 0)
    write = usage.get("cache_creation_input_tokens", 0)
    split = usage.get("cache_creation") or {}
    w1h = split.get("ephemeral_1h_input_tokens", write if not split else 0)
    w5m = split.get("ephemeral_5m_input_tokens", 0)
    c = card(model, inp + read + write)
    usd = (
        inp * c["input"]
        + read * c["read"]
        + w1h * c["write_1h"]
        + w5m * c["input"] * 1.25
        + usage.get("output_tokens", 0) * c["output"]
    ) / 1e6
    return usd * (BATCH_DISCOUNT if batch else 1.0)


class BatchClient:
    def __init__(self, key: str, api: str = API, timeout: float = 120):
        self._headers = {
            "x-api-key": key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        self._api = api
        self._http = httpx.Client(timeout=timeout)

    def create(self, requests: list[dict]) -> dict:
        r = self._http.post(self._api, headers=self._headers, json={"requests": requests})
        r.raise_for_status()
        return r.json()

    def retrieve(self, batch_id: str) -> dict:
        r = self._http.get(f"{self._api}/{batch_id}", headers=self._headers)
        r.raise_for_status()
        return r.json()

    def results(self, results_url: str) -> Iterator[bytes]:
        with self._http.stream("GET", results_url, headers=self._headers) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line.strip():
                    yield line.encode()

    def wait(self, batch_id: str, first: float = 30, cap: float = 300) -> dict:
        delay = first
        while True:
            b = self.retrieve(batch_id)
            counts = b.get("request_counts")
            print(json.dumps({"batch": batch_id, "status": b["processing_status"], **counts}))
            if b["processing_status"] == "ended":
                return b
            time.sleep(delay)
            delay = min(cap, delay * 1.5)


def read_key(path: Path) -> str:
    """Read the API key into memory. Never printed, never put in env or argv."""
    key = path.read_text().strip()
    if not key:
        raise RuntimeError(f"{path} is empty")
    return key


def record_result(line: bytes, sample_path: Path) -> dict[str, Any]:
    """Save one batch result line. Never overwrites: a succeeded result goes to
    sample-NN.response.json, anything else to sample-NN.<type>.json."""
    result = json.loads(line)
    kind = result["result"]["type"]
    target = sample_path if kind == "succeeded" else sample_path.with_suffix(f".{kind}.json")
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(line)
    meta: dict[str, Any] = {
        "custom_id": result["custom_id"],
        "result_type": kind,
        "response_file": str(target),
        "response_sha256": sha256(line),
    }
    if kind == "succeeded":
        msg = result["result"]["message"]
        details = msg.get("stop_details") or {}
        meta |= {
            "message_id": msg.get("id"),
            "model": msg.get("model"),
            "stop_reason": msg.get("stop_reason"),
            "refusal_category": details.get("category"),
            "usage": msg.get("usage"),
            **classify_first_action(msg),
        }
    else:
        err = result["result"].get("error") or {}
        meta["error_type"] = (err.get("error") or err).get("type")
    return meta
