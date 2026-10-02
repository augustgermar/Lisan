"""Build an evidence-gated manifest for the August/month deixis defect.

This module is intentionally dry-run only.  It can describe candidate edits,
but it contains no apply path: owner review is a hard boundary between finding
legacy damage and changing durable memory.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path


TOKEN = r"\{\{\s*principal\s*\}\}"
DATE_CANDIDATE = re.compile(
    rf"{TOKEN}\s+(?:(?P<day>3[01]|[12][0-9]|0?[1-9])(?P<ordinal>st|nd|rd|th)?(?!\d)"
    rf"(?P<year_part>\s*,?\s*(?P<year>(?:19|20)\d{{2}}))?|(?P<year_only>(?:19|20)\d{{2}}))",
    re.IGNORECASE,
)
MONTH_ONLY_CANDIDATE = re.compile(
    rf"(?P<prefix>(?:birthday(?:\s+is|\s+falls)?(?:\s+sometime)?\s+in|sometime\s+in)\s+){TOKEN}",
    re.IGNORECASE,
)
TEXT_SUFFIXES = {".md", ".json", ".jsonl", ".yaml", ".yml", ".txt"}
WORD = re.compile(r"[a-z0-9]{3,}")
STOP = {
    "the", "and", "for", "that", "this", "with", "from", "into", "was", "were", "has", "had",
    "user", "principal", "august", "2026", "2024", "2015", "2012", "record", "reported",
}


@dataclass(slots=True)
class RepairCandidate:
    path: str
    line: int
    column: int
    corrupted_value: str
    proposed_value: str | None
    occurrence_sha256_input: str
    evidence_source: str | None
    evidence_kind: str | None
    evidence_excerpt: str | None
    status: str


def build_manifest(vault: Path) -> list[RepairCandidate]:
    evidence_lines = _evidence_lines(vault)
    candidates: list[RepairCandidate] = []
    for path in _text_files(vault):
        text = path.read_text(encoding="utf-8", errors="replace")
        matches = list(DATE_CANDIDATE.finditer(text)) + list(MONTH_ONLY_CANDIDATE.finditer(text))
        matches.sort(key=lambda match: match.start())
        for match in matches:
            token_match = re.search(TOKEN, match.group(0), re.IGNORECASE)
            if token_match is None:
                continue
            start = match.start() + token_match.start()
            end = match.end()
            corrupted = text[start:end]
            corrected = re.sub(TOKEN, "August", corrupted, count=1, flags=re.IGNORECASE)
            context = text[max(0, start - 220):min(len(text), end + 220)]
            evidence = _find_evidence(
                vault=vault,
                source_path=path,
                corrected=corrected,
                context=context,
                evidence_lines=evidence_lines,
            )
            line = text.count("\n", 0, start) + 1
            line_start = text.rfind("\n", 0, start) + 1
            accepted_evidence = bool(evidence and evidence[1] in {
                "original transcript", "structured ISO timestamp",
            })
            status = "supported" if accepted_evidence else "unresolved"
            candidates.append(RepairCandidate(
                path=str(path.relative_to(vault)),
                line=line,
                column=start - line_start + 1,
                corrupted_value=corrupted,
                proposed_value=corrected if accepted_evidence else None,
                occurrence_sha256_input=f"{path.relative_to(vault)}:{start}:{corrupted}",
                evidence_source=evidence[0] if evidence else None,
                evidence_kind=evidence[1] if evidence else None,
                evidence_excerpt=evidence[2] if evidence else None,
                status=status,
            ))
    return candidates


def write_manifest(vault: Path, json_path: Path, markdown_path: Path) -> tuple[int, int, int]:
    candidates = build_manifest(vault)
    payload = {
        "version": 1,
        "mode": "dry-run-only",
        "vault": str(vault),
        "replacement_rule": "Only evidence-supported date/month uses of {{principal}} may become August.",
        "candidates": [asdict(candidate) for candidate in candidates],
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(_render_markdown(candidates, json_path), encoding="utf-8")
    return len(candidates), sum(c.status == "supported" for c in candidates), len({c.path for c in candidates})


def _text_files(vault: Path) -> list[Path]:
    return sorted(
        path for path in vault.rglob("*")
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES
    )


def _evidence_lines(vault: Path) -> list[tuple[Path, int, str, str]]:
    lines: list[tuple[Path, int, str, str]] = []
    for directory, kind in (("transcripts", "original transcript"), ("knowledge", "imported source")):
        root = vault / directory
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                if line.strip() and (kind == "original transcript" or "August" in line):
                    lines.append((path, number, line, kind))
    return lines


def _find_evidence(
    *,
    vault: Path,
    source_path: Path,
    corrected: str,
    context: str,
    evidence_lines: list[tuple[Path, int, str, str]],
) -> tuple[str, str, str] | None:
    corrected_norm = _normalize(corrected)
    corrected_words = set(WORD.findall(corrected_norm)) - STOP
    context_words = set(WORD.findall(_normalize(context))) - STOP
    date_parts = _date_parts(corrected)

    # Prefer an ISO timestamp in the affected record itself over a merely
    # coincident same-day sentence elsewhere. This is the strongest link for
    # generated entity narratives whose source-log entry carries the date.
    if date_parts:
        year, day = date_parts
        iso = f"{year}-08-{day:02d}"
        source_text = source_path.read_text(encoding="utf-8", errors="replace")
        if iso in source_text:
            line = source_text[:source_text.index(iso)].count("\n") + 1
            return str(source_path.relative_to(vault)) + f":{line}", "structured ISO timestamp", iso
    else:
        year_only = re.fullmatch(r"August\s+((?:19|20)\d{2})", corrected, re.IGNORECASE)
        if year_only:
            year = int(year_only.group(1))
            source_text = source_path.read_text(encoding="utf-8", errors="replace")
            iso_match = re.search(rf"\b{year}-08-(?:0[1-9]|[12][0-9]|3[01])\b", source_text)
            if iso_match:
                line = source_text[:iso_match.start()].count("\n") + 1
                return (
                    str(source_path.relative_to(vault)) + f":{line}",
                    "structured ISO timestamp",
                    iso_match.group(0),
                )

    ranked: list[tuple[int, Path, int, str, str]] = []
    for path, number, line, kind in evidence_lines:
        normalized = _normalize(line)
        line_words = set(WORD.findall(normalized)) - STOP
        overlap = context_words & line_words
        same_dated_transcript = False
        if date_parts and kind == "original transcript":
            year, day = date_parts
            same_dated_transcript = path.stem == f"{year}-08-{day:02d}"
        score = 0
        exact = bool(corrected_norm and re.search(
            rf"(?<![a-z0-9]){re.escape(corrected_norm)}(?![a-z0-9])",
            normalized,
        ))
        if exact:
            score += 120
        elif date_parts and _contains_date(normalized, *date_parts):
            score += 80
        elif corrected.lower() == "august" and "birthday" in normalized and "august" in normalized:
            score += 70
        elif same_dated_transcript and overlap:
            score += 70
        else:
            continue
        if date_parts and not same_dated_transcript and len(overlap) < 2 and not (exact and overlap):
            continue
        if corrected.lower() == "august" and "birthday" not in line.lower():
            continue
        score += min(25, len(overlap) * 3)
        if same_dated_transcript:
            score += 35
        filename_words = set(WORD.findall(path.stem.lower())) - STOP
        score += min(12, len(context_words & filename_words) * 3)
        score += 80 if kind == "original transcript" else 15
        ranked.append((score, path, number, line, kind))
    if ranked:
        _, path, number, line, kind = max(ranked, key=lambda item: (item[0], -len(item[3])))
        return str(path.relative_to(vault)) + f":{number}", kind, _clip(line)

    return None


def _date_parts(value: str) -> tuple[int, int] | None:
    match = re.search(
        r"\bAugust\s+(3[01]|[12]\d|0?[1-9])(?:st|nd|rd|th)?(?!\d)(?:\s*,?\s*((?:19|20)\d{2}))?",
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    return int(match.group(2) or 2026), int(match.group(1))


def _contains_date(normalized: str, year: int, day: int) -> bool:
    pattern = rf"\baugust\s+0?{day}(?:st|nd|rd|th)?\b"
    if not re.search(pattern, normalized):
        return False
    # A transcript filename supplies 2026 implicitly; literal years, when
    # present in source text, must agree.
    years = {int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", normalized)}
    return not years or year in years


def _normalize(value: str) -> str:
    value = value.lower()
    value = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", value)
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _clip(value: str, limit: int = 180) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _escape(value: str | None) -> str:
    if value is None:
        return "—"
    return value.replace("|", "\\|").replace("\n", " ")


def _render_markdown(candidates: list[RepairCandidate], json_path: Path) -> str:
    grouped: dict[tuple[str, str, str | None, str | None], list[RepairCandidate]] = defaultdict(list)
    for candidate in candidates:
        key = (
            candidate.path,
            candidate.corrupted_value,
            candidate.proposed_value,
            candidate.evidence_source,
        )
        grouped[key].append(candidate)
    supported = sum(candidate.status == "supported" for candidate in candidates)
    unresolved = len(candidates) - supported
    files = len({candidate.path for candidate in candidates})
    lines = [
        "# Deixis/date repair manifest — dry run",
        "",
        "> No live record has been changed. A missing proposal means the month could not be independently evidenced and must not be guessed.",
        "",
        "- mode: **dry-run-only**",
        f"- affected records: **{files}**",
        f"- candidate occurrences: **{len(candidates)}**",
        f"- evidence-supported: **{supported}**",
        f"- unresolved: **{unresolved}**",
        f"- machine-readable manifest: `{json_path}`",
        "",
        "| Record | Location | Count | Corrupted value | Proposed corrected value | Evidence source | Evidence kind |",
        "|---|---:|---:|---|---|---|---|",
    ]
    ordered = sorted(
        grouped.items(),
        key=lambda item: tuple("" if value is None else value for value in item[0]),
    )
    for (path, corrupted, proposed, evidence_source), members in ordered:
        locations = ", ".join(f"L{member.line}:C{member.column}" for member in members)
        evidence_kind = members[0].evidence_kind
        lines.append(
            f"| `{_escape(path)}` | {_escape(locations)} | {len(members)} | "
            f"`{_escape(corrupted)}` | `{_escape(proposed)}` | "
            f"`{_escape(evidence_source)}` | {_escape(evidence_kind)} |"
        )
    lines.extend((
        "",
        "## Rules used",
        "",
        "1. Candidate detection is limited to date-shaped role tokens and explicit birthday-month grammar.",
        "2. The proposed month is emitted only when supported by an original transcript, an imported literal source, or an exact same-record ISO date.",
        "3. The tool has no apply mode. Approval and a separate reversible migration are required.",
        "4. Archived records and drafts are included because they remain queryable or can seed later derived records.",
        "",
    ))
    return "\n".join(lines)
