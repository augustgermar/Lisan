"""Fitting a growing vault into a fixed provider window.

Every provider enforces a hard input ceiling — codex rejects anything over
1,048,576 characters with ``input_too_large``. The vault has no such ceiling:
entity biographies grow by design, so any bundle that says "every entity, in
full" is a prompt that works until the day it doesn't. The dreamer's compress
bundle crossed the line in late July 2026 and every run failed for the next
seventeen days.

The instinct is to truncate. That instinct is wrong here, and principle 4
says why: never lose data in the name of tidiness. Dropping half the entities
would produce a dreamer that runs clean and reasons from a vault it cannot
see — a worse failure than the crash, because nothing would report it.

So the primary mechanism is **partition, not truncation**. Records are packed
into as many chunks as the budget requires; the caller runs the model once per
chunk and merges. Everything is read. Truncation exists only as a backstop for
a single record too large to fit any chunk alone, and it is always announced
in-band, with the byte count and the path to the full record.

Packing is deterministic — sorted input, greedy fill — so the same vault
produces the same chunks, which is what makes a failure reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# The codex ceiling, observed live: {"max_chars": 1048576}. Configurable
# because it is a property of the provider, not of this code.
DEFAULT_PROVIDER_INPUT_CHARS = 1_048_576
# Headroom for the prompt template, the schema instruction, and the chunk
# banner — the bundle is never the whole prompt.
DEFAULT_RESERVE_CHARS = 131_072
# Deliberately generous: the largest entity in a real vault is ~61k, so
# nothing truncates today. This is a backstop against a pathological record,
# not a routine budget.
DEFAULT_PER_RECORD_CHARS = 100_000
# A ceiling on how many model calls one bundle may become.
DEFAULT_MAX_CHUNKS = 12


@dataclass(frozen=True)
class Record:
    """One vault record as it appears in a bundle."""

    label: str
    text: str

    def rendered(self) -> str:
        # An empty label is a record that is its own heading (a small
        # pre-rendered bundle wrapped as one indivisible unit).
        return f"{self.label}\n{self.text}\n" if self.label else f"{self.text}\n"

    def size(self) -> int:
        return len(self.rendered())


@dataclass(frozen=True)
class Section:
    """A titled group of records. The title repeats in every chunk that
    carries any of its records, so a chunk is readable on its own."""

    title: str
    records: tuple[Record, ...] = ()
    empty_note: str = ""

    def rendered(self) -> str:
        parts = [self.title, ""] if self.title else []
        if self.records:
            parts.extend(record.rendered() for record in self.records)
        elif self.empty_note:
            parts.extend([self.empty_note, ""])
        return "\n".join(parts)


@dataclass
class ChunkPlan:
    chunks: list[str] = field(default_factory=list)
    truncated: list[tuple[str, int, int]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    record_count: int = 0
    budget_chars: int = 0

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def notes(self) -> list[str]:
        """Human-readable account of anything this plan did not carry whole.
        Empty when every record was passed through intact."""
        lines: list[str] = []
        for label, kept, original in self.truncated:
            lines.append(f"{label}: truncated to {kept:,} of {original:,} chars")
        if self.dropped:
            lines.append(
                f"{len(self.dropped)} record(s) dropped after the {DEFAULT_MAX_CHUNKS}-chunk "
                f"ceiling: {', '.join(self.dropped[:10])}"
                + (" ..." if len(self.dropped) > 10 else "")
            )
        return lines


def budget_from_config(config: dict[str, Any] | None) -> dict[str, int]:
    """Resolve the packing budget from a config's ``context`` block."""
    block = ((config or {}).get("context") or {})

    def _int(key: str, default: int) -> int:
        try:
            value = int(block.get(key, default) or default)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    provider_chars = _int("provider_input_chars", DEFAULT_PROVIDER_INPUT_CHARS)
    reserve = _int("reserve_chars", DEFAULT_RESERVE_CHARS)
    # A reserve at or above the ceiling would leave no budget at all; fall
    # back to a proportional reserve rather than packing into nothing.
    if reserve >= provider_chars:
        reserve = provider_chars // 8
    return {
        "budget_chars": provider_chars - reserve,
        "per_record_chars": _int("per_record_chars", DEFAULT_PER_RECORD_CHARS),
        "max_chunks": _int("max_chunks", DEFAULT_MAX_CHUNKS),
    }


def render(sections: Iterable[Section]) -> str:
    """The whole bundle as one string — what a chunk-free caller wants, and
    what the report archives regardless of how many chunks were sent."""
    body = "\n".join(section.rendered() for section in sections)
    return body.rstrip() + "\n"


def _truncate(record: Record, limit: int) -> tuple[Record, tuple[str, int, int] | None]:
    original = record.size()
    if original <= limit:
        return record, None
    # Announce the cut in-band. A model that can see it was handed a partial
    # record can say so; a model handed a silent stump cannot.
    marker = (
        f"\n\n[... truncated by Lisan: this record is {original:,} chars, "
        f"over the {limit:,}-char per-record budget. Read the full record at "
        f"{record.label.lstrip('# ').strip()} ...]\n"
    )
    keep = max(0, limit - len(record.label) - len(marker) - 2)
    trimmed = Record(record.label, record.text[:keep] + marker)
    return trimmed, (record.label, trimmed.size(), original)


def plan_chunks(
    sections: Iterable[Section],
    *,
    budget_chars: int,
    per_record_chars: int = DEFAULT_PER_RECORD_CHARS,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> ChunkPlan:
    """Pack sections into the fewest chunks that each fit ``budget_chars``.

    Greedy and order-preserving: records stay in the order given, so a chunk
    is a contiguous slice of the bundle and the same vault always yields the
    same partition.
    """
    sections = list(sections)
    plan = ChunkPlan(budget_chars=budget_chars)
    plan.record_count = sum(len(section.records) for section in sections)

    # Nothing to pack: one chunk carrying whatever headers exist. An empty
    # bundle is a real answer ("no candidates"), not an error.
    whole = render(sections)
    if len(whole) <= budget_chars:
        plan.chunks = [whole]
        return plan

    # Leave room for the section header that may precede any record, so a
    # single max-size record can never overflow the chunk it starts.
    header_room = max((len(section.title) for section in sections), default=0) + 8
    per_record_chars = max(1, min(per_record_chars, budget_chars - header_room))
    current: list[str] = []
    current_len = 0
    open_section: str | None = None

    def _add(piece: str) -> None:
        """Track the "\n" that join() will insert, or the budget is a lie by
        one character per piece — enough to push a full chunk over."""
        nonlocal current_len
        current_len += len(piece) + (1 if current else 0)
        current.append(piece)

    def _would_fit(piece: str) -> bool:
        return current_len + len(piece) + (1 if current else 0) <= budget_chars

    def flush() -> None:
        nonlocal current, current_len, open_section
        if current:
            plan.chunks.append("\n".join(current).rstrip() + "\n")
        current = []
        current_len = 0
        open_section = None

    for section in sections:
        if not section.records:
            block = section.rendered()
            if not _would_fit(block) and current:
                flush()
            _add(block)
            open_section = None
            continue

        for record in section.records:
            record, cut = _truncate(record, per_record_chars)
            if cut:
                plan.truncated.append(cut)
            header = "" if open_section == section.title else f"{section.title}\n\n"
            piece = header + record.rendered()
            if not _would_fit(piece) and current:
                if len(plan.chunks) + 1 >= max_chunks:
                    # Out of chunks. Everything from here on is named in the
                    # plan rather than vanishing.
                    plan.dropped.append(record.label)
                    continue
                flush()
                piece = f"{section.title}\n\n" + record.rendered()
            _add(piece)
            open_section = section.title

    flush()
    return plan
