"""Embeddings through Ollama, cached by the SHA-256 of each text.

Only hashes, counts and the server's version and model digest are ever
returned for logging; vectors are cached under out/ (gitignored).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import numpy as np

from .adapter import sha256

OLLAMA = "http://192.168.0.49:11434"
MODEL = "qwen3-embedding:8b"
NUM_CTX = 8192


def manifest(base: str = OLLAMA, model: str = MODEL) -> dict:
    with httpx.Client(timeout=30) as h:
        version = h.get(f"{base}/api/version").json().get("version")
        tags = h.get(f"{base}/api/tags").json().get("models", [])
    m = next((t for t in tags if t.get("name") == model), None)
    if m is None:
        raise RuntimeError(f"{model} not served at {base}")
    return {
        "endpoint": base,
        "ollama_version": version,
        "model": model,
        "model_digest": m.get("digest"),
        "quantization": (m.get("details") or {}).get("quantization_level"),
        "parameter_size": (m.get("details") or {}).get("parameter_size"),
        "num_ctx": NUM_CTX,
        "truncate": False,
        "instruction_prefix": None,
        "normalised": "L2, after the call",
    }


def embed_texts(
    texts: list[str], cache: Path, base: str = OLLAMA, model: str = MODEL, batch: int = 16
) -> dict[str, np.ndarray]:
    """Return {sha256(text): unit vector}. Texts already cached are not sent."""
    store: dict[str, np.ndarray] = {}
    if cache.exists():
        with np.load(cache) as z:
            store = {k: z[k] for k in z.files}
    todo = {}
    for t in texts:
        h = sha256(t.encode())
        if h not in store:
            todo[h] = t
    items = list(todo.items())
    with httpx.Client(timeout=600) as client:
        for i in range(0, len(items), batch):
            chunk = items[i : i + batch]
            r = client.post(
                f"{base}/api/embed",
                json={
                    "model": model,
                    "input": [t for _, t in chunk],
                    "truncate": False,
                    "options": {"num_ctx": NUM_CTX},
                },
            )
            r.raise_for_status()
            vecs = r.json()["embeddings"]
            assert len(vecs) == len(chunk)
            for (h, _), v in zip(chunk, vecs, strict=True):
                a = np.asarray(v, dtype=np.float32)
                store[h] = a / np.linalg.norm(a)
            cache.parent.mkdir(parents=True, exist_ok=True)
            np.savez(cache, **store)
            print(f"embedded {min(i + batch, len(items))}/{len(items)}", flush=True)
    return {sha256(t.encode()): store[sha256(t.encode())] for t in texts}
