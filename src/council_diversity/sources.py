"""Find each seat's first Round 1 request for each Council decision.

Two sources:

- **Exact dumps.** From 2026-10-17 the Council writes every request it sends
  to `<secrets>/council-requests/<agenda-item-id>/<ts>.<role>.<phase>.<sha12>.<x>.json`.
  A dump's SHA-256 is the archive's `digest` for the same request (checked
  here for every file used), and its bytes are the wire body less
  `"stream":false`.
- **The archive export** (`inputs/GOV-*/<role>.json`, from Postgres jsonb):
  same content, object key order lost; `properties` order is restored from
  `required`.

The archive export script takes a seat's *first* single-turn prompt on an
item. From 2026-10-17 a sitting may open with the recusal question, so for
those items the export is the recusal request, not Round 1. A dump's file
name says which phase it is. Where there is no dump, the archive must hold
exactly one single-turn prompt per seat for the item, or the item is refused
as ambiguous.

Reads bytes and structure only; no prompt text is returned or logged.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .adapter import canonical_bytes, restore_property_order, sha256

SEATS = ("artist", "philosopher", "lawyer", "engineer")
_DUMP = re.compile(
    r"^(?P<ts>\d{8}T\d{6}\.\d{3}Z)\.(?P<role>[a-z]+)\.(?P<phase>[a-z0-9_]+)\."
    r"(?P<sha12>[0-9a-f]{12})\.(?P<x>[0-9a-f]+)\.json$"
)


@dataclass
class ArchiveRow:
    decision: str
    role: str
    agenda_item: str
    meeting: str
    created_at: str
    digest: str
    n_messages: int
    n_blocks: int
    input_tokens: int
    cache_read: int
    cache_creation: int
    output_tokens: int

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read + self.cache_creation


def load_index(path: Path) -> list[ArchiveRow]:
    rows = []
    with path.open() as f:
        for r in csv.DictReader(f, delimiter="\t"):
            rows.append(
                ArchiveRow(
                    **{
                        k: (int(v) if k.startswith(("n_", "input", "cache", "output")) else v)
                        for k, v in r.items()
                    }
                )
            )
    return rows


@dataclass
class Source:
    decision: str
    seat: str
    kind: str  # "dump" or "archive"
    path: str  # dump: relative to the dumps dir; archive: relative to inputs dir
    sha256: str  # of the source file's bytes
    request_sha256: str  # of the request as we will treat it (see notes)
    archive_digest: str  # the archive row this request is
    archive_created_at: str
    agenda_item: str
    meeting: str
    phase: str
    prompt_tokens_opus46: int  # recorded usage of the original send
    export_digest: str  # what inputs/GOV-*/<seat>.json holds
    export_is_round1: bool
    properties_reordered: int
    notes: str

    def to_json(self) -> dict:
        return asdict(self)


class SourceError(RuntimeError):
    pass


def _dump_round1(dumps_dir: Path, agenda_item: str, seat: str) -> Path | None:
    d = dumps_dir / agenda_item
    if not d.is_dir():
        return None
    found = []
    for p in d.iterdir():
        m = _DUMP.match(p.name)
        if m and m["role"] == seat and m["phase"] == "round1":
            found.append((m["ts"], p))
    return min(found)[1] if found else None


def resolve(inputs_dir: Path, dumps_dir: Path, index: list[ArchiveRow]) -> list[Source]:
    """One Source per (decision, seat) in the index."""
    out = []
    decisions = sorted({r.decision for r in index})
    for gov in decisions:
        for seat in SEATS:
            rows = sorted(
                (r for r in index if r.decision == gov and r.role == seat),
                key=lambda r: r.created_at,
            )
            if not rows:
                raise SourceError(f"{gov} {seat}: no single-turn archive rows")
            export_path = inputs_dir / gov / f"{seat}.json"
            export = json.loads(export_path.read_text())
            export_digest = export["digest"]
            by_digest = {r.digest: r for r in rows}
            dump = _dump_round1(dumps_dir, rows[0].agenda_item, seat)
            if dump is not None:
                raw = dump.read_bytes()
                digest = sha256(raw)
                row = by_digest.get(digest)
                if row is None:
                    raise SourceError(f"{gov} {seat}: dump {dump.name} is not in the archive")
                if canonical_bytes(json.loads(raw)) != raw:
                    raise SourceError(f"{gov} {seat}: dump does not round-trip byte for byte")
                if len(json.loads(raw).get("messages", [])) != 1:
                    raise SourceError(f"{gov} {seat}: first round1 dump is not single-turn")
                notes = []
                if export_digest == digest:
                    same = export["prompt"] == json.loads(raw)
                    notes.append(f"archive export is this request; content equal: {same}")
                    if not same:
                        raise SourceError(f"{gov} {seat}: export and dump differ in content")
                else:
                    pos = [r.digest for r in rows].index(export_digest)
                    notes.append(
                        f"archive export is single-turn prompt #{pos + 1} of {len(rows)} "
                        f"(not Round 1); Round 1 is #{rows.index(row) + 1}"
                    )
                out.append(
                    Source(
                        decision=gov,
                        seat=seat,
                        kind="dump",
                        path=str(dump.relative_to(dumps_dir)),
                        sha256=digest,
                        request_sha256=digest,
                        archive_digest=row.digest,
                        archive_created_at=row.created_at,
                        agenda_item=row.agenda_item,
                        meeting=row.meeting,
                        phase="round1",
                        prompt_tokens_opus46=row.prompt_tokens,
                        export_digest=export_digest,
                        export_is_round1=export_digest == digest,
                        properties_reordered=0,
                        notes="; ".join(notes),
                    )
                )
                continue
            if len(rows) != 1:
                raise SourceError(
                    f"{gov} {seat}: {len(rows)} single-turn prompts and no dump; "
                    "can't tell which is Round 1"
                )
            row = rows[0]
            if export_digest != row.digest:
                raise SourceError(f"{gov} {seat}: export digest is not the archive row's")
            prompt = export["prompt"]
            n = restore_property_order(prompt)
            out.append(
                Source(
                    decision=gov,
                    seat=seat,
                    kind="archive",
                    path=str(export_path.relative_to(inputs_dir)),
                    sha256=sha256(export_path.read_bytes()),
                    request_sha256=sha256(canonical_bytes(prompt)),
                    archive_digest=row.digest,
                    archive_created_at=row.created_at,
                    agenda_item=row.agenda_item,
                    meeting=row.meeting,
                    phase="round1 (only single-turn prompt for the seat)",
                    prompt_tokens_opus46=row.prompt_tokens,
                    export_digest=export_digest,
                    export_is_round1=True,
                    properties_reordered=n,
                    notes="jsonb archive; properties order restored from required",
                )
            )
    return out


def load_request(src: Source, inputs_dir: Path, dumps_dir: Path) -> dict:
    """The original request a Source names, verified against its hash."""
    if src.kind == "dump":
        raw = (dumps_dir / src.path).read_bytes()
        if sha256(raw) != src.sha256:
            raise SourceError(f"{src.path}: hash changed")
        return json.loads(raw)
    p = inputs_dir / src.path
    if sha256(p.read_bytes()) != src.sha256:
        raise SourceError(f"{src.path}: hash changed")
    prompt = json.loads(p.read_text())["prompt"]
    restore_property_order(prompt)
    if sha256(canonical_bytes(prompt)) != src.request_sha256:
        raise SourceError(f"{src.path}: restored request hash changed")
    return prompt
