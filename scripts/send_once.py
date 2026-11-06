"""Send one archived Council request to the Anthropic Messages API, once.

Prints metadata only (stop reason, the tool the seat called, usage, any
refusal category), never the response text. Saves the raw response body and a
manifest beside the request. Exits 3 on a refusal, so a caller can stop at the
first one: a refused request is never retried.

Usage:
  uv run scripts/send_once.py <request.json> <out-dir> [--model MODEL]
      [--drop-temperature] [--key-file <path>] [--api-url URL]

With --api-url (e.g. a local blallama server) no key is needed.

With --model on a Claude model, `thinking` is set explicitly to adaptive with
`display: summarized` (a request without it runs adaptive thinking with the
text hidden on the 5-generation models, which still shapes the answer), any
sampling parameter is dropped (thinking rejects them), and a `reasoning`
property is removed from every tool and response-format schema, as the
adapter does (no field asks a model for its reasoning).
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import httpx

from council_diversity.adapter import (
    SAMPLING_KEYS,
    explicit_thinking,
    restore_property_order,
    strip_reasoning_properties,
)

API = "https://api.anthropic.com/v1/messages"


def apply_model(body: dict, model: str, stripped: list[str]) -> None:
    """Put `body` on `model`, with its thinking said rather than defaulted.

    On a Claude model: `thinking` adaptive with the summary shown, sampling
    parameters dropped, and `reasoning` removed from every schema (the names
    of the schemas it was removed from are appended to `stripped`). A
    non-Claude model (a local server) only has its name changed.
    """
    body["model"] = model
    thinking = explicit_thinking(model)
    if thinking is None:
        return
    body["thinking"] = thinking
    for key in SAMPLING_KEYS:
        body.pop(key, None)
    stripped.extend(strip_reasoning_properties(body))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("request", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--model")
    ap.add_argument("--drop-temperature", action="store_true")
    ap.add_argument("--max-tokens", type=int)
    ap.add_argument("--key-file", type=Path)
    ap.add_argument("--api-url", default=API)
    args = ap.parse_args()

    archived = json.loads(args.request.read_text())
    body = archived["prompt"]
    stripped: list[str] = []
    if args.model:
        apply_model(body, args.model, stripped)
    if args.max_tokens:
        body["max_tokens"] = args.max_tokens
    if args.drop_temperature:
        body.pop("temperature", None)
    restore_property_order(body)
    # Compact; the bytes hashed are the bytes sent. Keys are NOT sorted: under
    # strict tool use a decoder emits fields in `properties` order, so sorting
    # would put the decision before the rationale.
    payload = json.dumps(body, separators=(",", ":")).encode()

    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if args.key_file:
        headers["x-api-key"] = args.key_file.read_text().strip()
    started = time.time()
    resp = httpx.post(args.api_url, content=payload, headers=headers, timeout=3600)
    elapsed = time.time() - started

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.request.stem}.{body['model']}"
    (args.out_dir / f"{stem}.response.json").write_bytes(resp.content)

    is_json = resp.headers.get("content-type", "").startswith("application/json")
    result = resp.json() if is_json else {}
    tools_called = [b.get("name") for b in result.get("content", []) if b.get("type") == "tool_use"]
    details = result.get("stop_details") or {}
    manifest = {
        "request_file": args.request.name,
        "request_sha256": hashlib.sha256(payload).hexdigest(),
        "model": body["model"],
        "api_url": args.api_url,
        "temperature": body.get("temperature"),
        "thinking": body.get("thinking"),
        "reasoning_stripped_from": stripped,
        "max_tokens": body.get("max_tokens"),
        "http_status": resp.status_code,
        "request_id": resp.headers.get("request-id"),
        "response_sha256": hashlib.sha256(resp.content).hexdigest(),
        "stop_reason": result.get("stop_reason"),
        "refusal_category": details.get("category"),
        "tools_called": tools_called,
        "content_types": [b.get("type") for b in result.get("content", [])],
        "content_chars": [len(json.dumps(b)) for b in result.get("content", [])],
        "usage": result.get("usage"),
        "sent_at": started,
        "elapsed_s": round(elapsed, 2),
    }
    (args.out_dir / f"{stem}.manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest))

    if resp.status_code != 200:
        return 2
    if result.get("stop_reason") == "refusal":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
