"""Voice extraction pass + ratification ceremony (Phase 2 WO-2).

Codify, don't author: the kernel voice is distilled from the agent's own
accumulated transcript history and ratified into `primer/identity-core.md`
via the WO-1 ceremony path. One code path, two invocation times — a fresh
install runs it when the evidence threshold is met; a mature instance runs
the same ceremony late, over its real history.

Three stages:

1. **Deterministic collection.** Agent turns are parsed from the daily
   transcripts, grouped by conversation, with surface statistics (length
   distribution, question rate, formatting habits) computed without a model.
2. **Model-assisted distillation, evidence-gated.** The model proposes
   candidate invariants; every candidate must cite verbatim quotes that
   resolve to real turns — at least ``min_evidence`` quotes across at least
   ``min_conversations_per_invariant`` distinct conversations — or it is
   rejected deterministically. No evidence, no invariant.
3. **Deterministic stability + provenance.** Recurrence across dates and
   conversations, and a ``factory`` / ``earned`` tag per invariant: a
   candidate whose wording traces to the authored prompt voice is factory;
   one traceable only to interaction is earned. These tags are the data for
   the deferred seeded-vs-earned question.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..paths import vault_root
from .kernel import ceremony, kernel_path, stamp_kernel_hash
from .transcripts import _BLOCK_RE

# Ceremony eligibility defaults (config: identity.ceremony)
MIN_INVARIANTS = 5
MIN_CONVERSATIONS = 3
# Per-invariant evidence gate
MIN_EVIDENCE = 3
MIN_CONVERSATIONS_PER_INVARIANT = 2
MIN_QUOTE_CHARS = 12

_CATEGORIES = ("register", "move", "prohibition", "temperament")


@dataclass(slots=True)
class Turn:
    date: str
    time: str
    conversation: str  # conversation id, or "day:<date>" when the block has none
    text: str


@dataclass(slots=True)
class Candidate:
    statement: str
    category: str
    evidence: list[dict[str, str]] = field(default_factory=list)
    conversations: set[str] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)
    provenance: str = "earned"
    rejected_reason: str = ""


# ── Stage 1: deterministic collection ────────────────────────────────────────


def collect_agent_turns(vault: Path) -> list[Turn]:
    turns: list[Turn] = []
    root = vault / "transcripts"
    if not root.exists():
        return turns
    for path in sorted(root.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        date = path.stem
        for match in _BLOCK_RE.finditer("\n" + text):
            body = match.group("body").strip()
            if not body.startswith("LISAN:"):
                continue
            conv = (match.group("conv") or "").strip() or f"day:{date}"
            reply = body[len("LISAN:"):].strip()
            if reply:
                turns.append(Turn(date=date, time=match.group("time"), conversation=conv, text=reply))
    return turns


def _sentences(text: str) -> int:
    return max(1, len([s for s in re.split(r"[.!?]+(?:\s|$)", text) if s.strip()]))


def surface_stats(turns: list[Turn]) -> dict[str, Any]:
    if not turns:
        return {"turns": 0, "conversations": 0}
    sentence_counts = sorted(_sentences(t.text) for t in turns)

    def pct(p: float) -> int:
        return sentence_counts[min(len(sentence_counts) - 1, int(p * len(sentence_counts)))]

    return {
        "turns": len(turns),
        "conversations": len({t.conversation for t in turns}),
        "dates": sorted({t.date for t in turns}),
        "sentences_median": pct(0.50),
        "sentences_p25": pct(0.25),
        "sentences_p75": pct(0.75),
        "sentences_p90": pct(0.90),
        "question_rate": round(sum(1 for t in turns if "?" in t.text) / len(turns), 3),
        "bullet_rate": round(sum(1 for t in turns if re.search(r"^\s*[-*] ", t.text, re.M)) / len(turns), 3),
        "exclamation_rate": round(sum(1 for t in turns if "!" in t.text) / len(turns), 3),
    }


def sample_for_model(turns: list[Turn], cap: int = 150) -> list[Turn]:
    """Recency-weighted sample that still spans the whole window: the
    earliest quarter of the cap comes from the start of history, the rest
    from the most recent turns."""
    if len(turns) <= cap:
        return list(turns)
    head = turns[: cap // 4]
    tail = turns[-(cap - len(head)):]
    return head + tail


# ── Stage 2: evidence gate ───────────────────────────────────────────────────


def _normalize(text: str) -> str:
    return " ".join(str(text).split()).lower()


def validate_candidates(
    raw_candidates: list[dict[str, Any]],
    turns: list[Turn],
    *,
    min_evidence: int = MIN_EVIDENCE,
    min_conversations: int = MIN_CONVERSATIONS_PER_INVARIANT,
) -> tuple[list[Candidate], list[Candidate]]:
    """Deterministic gate: every candidate keeps only quotes that resolve to
    a real agent turn, and must retain >= min_evidence quotes across >=
    min_conversations distinct conversations. No evidence, no invariant."""
    normalized_turns = [(t, _normalize(t.text)) for t in turns]
    valid: list[Candidate] = []
    rejected: list[Candidate] = []
    for raw in raw_candidates or []:
        statement = str(raw.get("statement") or "").strip()
        category = str(raw.get("category") or "").strip().lower()
        cand = Candidate(statement=statement, category=category)
        if not statement or category not in _CATEGORIES:
            cand.rejected_reason = "malformed (empty statement or unknown category)"
            rejected.append(cand)
            continue
        for ev in raw.get("evidence") or []:
            quote = _normalize((ev or {}).get("quote") or "")
            if len(quote) < MIN_QUOTE_CHARS:
                continue
            for turn, norm in normalized_turns:
                if quote in norm:
                    cand.evidence.append(
                        {"conversation": turn.conversation, "date": turn.date, "quote": str(ev.get("quote")).strip()}
                    )
                    cand.conversations.add(turn.conversation)
                    cand.dates.add(turn.date)
                    break
        if len(cand.evidence) < min_evidence:
            cand.rejected_reason = f"insufficient resolved evidence ({len(cand.evidence)}/{min_evidence})"
            rejected.append(cand)
        elif len(cand.conversations) < min_conversations:
            cand.rejected_reason = f"evidence spans {len(cand.conversations)} conversation(s); needs {min_conversations}"
            rejected.append(cand)
        else:
            valid.append(cand)
    return valid, rejected


# ── Stage 3: provenance ──────────────────────────────────────────────────────


def _ngrams(text: str, n: int = 4) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z']+", _normalize(text))
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def tag_provenance(candidates: list[Candidate], prompt_text: str) -> None:
    """``factory`` when the invariant's wording overlaps the authored prompt
    (4-gram overlap — a deterministic approximation, documented as such);
    ``earned`` when it traces only to interaction."""
    prompt_grams = _ngrams(prompt_text)
    for cand in candidates:
        cand.provenance = "factory" if (_ngrams(cand.statement) & prompt_grams) else "earned"


# ── The extraction pass ──────────────────────────────────────────────────────


def run_extraction(
    vault: Path | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    out: Path | None = None,
    min_invariants: int | None = None,
    min_conversations: int | None = None,
    max_turns: int = 150,
    agent: Any | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the full pass and write the ratification artifact. Read-only with
    respect to everything except the artifact under ``reports/``."""
    from ..config import load_config

    vault = vault or vault_root()
    config = config or load_config()
    ceremony_cfg = (config.get("identity") or {}).get("ceremony") or {}
    min_invariants = min_invariants or int(ceremony_cfg.get("min_invariants", MIN_INVARIANTS))
    min_conversations = min_conversations or int(ceremony_cfg.get("min_conversations", MIN_CONVERSATIONS))

    turns = collect_agent_turns(vault)
    stats = surface_stats(turns)

    if agent is None:
        from ..agents.voice_extractor import VoiceExtractorAgent

        agent = VoiceExtractorAgent(vault=vault, config=config)
    sample = sample_for_model(turns, cap=max_turns)
    payload = {
        "surface_stats": stats,
        "agent_turns": [
            {"conversation": t.conversation, "date": t.date, "time": t.time, "text": t.text} for t in sample
        ],
    }
    output = agent.run_json(
        json.dumps(payload, indent=2, ensure_ascii=True),
        significance="high",
        provider=provider,
        model=model,
    )
    raw_candidates = output.get("candidates") if isinstance(output, dict) else None
    valid, rejected = validate_candidates(list(raw_candidates or []), turns)

    try:
        from ..prompts import load_prompt

        prompt_text = load_prompt("conversation_v1")
    except Exception:
        prompt_text = ""
    tag_provenance(valid, prompt_text)

    eligible = len(valid) >= min_invariants and len({c for cand in valid for c in cand.conversations}) >= min_conversations
    result = {
        "stats": stats,
        "eligible": eligible,
        "thresholds": {"min_invariants": min_invariants, "min_conversations": min_conversations},
        "candidates": [_candidate_dict(c) for c in valid],
        "rejected": [_candidate_dict(c) for c in rejected],
    }
    result["artifact"] = str(_write_artifact(vault, result, out=out))
    return result


def _candidate_dict(c: Candidate) -> dict[str, Any]:
    d: dict[str, Any] = {
        "statement": c.statement,
        "category": c.category,
        "provenance": c.provenance,
        "evidence": c.evidence,
        "conversations": sorted(c.conversations),
        "dates": sorted(c.dates),
    }
    if c.rejected_reason:
        d["rejected_reason"] = c.rejected_reason
    return d


def _write_artifact(vault: Path, result: dict[str, Any], out: Path | None = None) -> Path:
    now = datetime.now().astimezone()
    stamp = now.strftime("%Y%m%d%H%M%S")
    day = now.date().isoformat()
    path = out or (vault / "reports" / f"voice-extraction-{stamp}.md")
    stats = result["stats"]
    frontmatter = {
        "id": f"report.voice-extraction.{stamp}",
        "type": "report",
        "created": day,
        "updated": day,
        "status": "active",
        "significance": "high",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "private",
        "summary": "Voice extraction pass — ratification artifact",
        "links": [],
        "confidence": "medium",
        "confidence_basis": "Deterministic evidence gate over transcript history",
        "last_confirmed": day,
        "review_after": day,
        "voice_extraction": {
            "stats": stats,
            "eligible": result["eligible"],
            "thresholds": result["thresholds"],
            "candidates": result["candidates"],
            "rejected": result["rejected"],
        },
    }
    lines = ["# Voice extraction — ratification artifact", ""]
    lines.append(
        f"{stats.get('turns', 0)} agent turns across {stats.get('conversations', 0)} conversations; "
        f"typical reply {stats.get('sentences_p25', '?')}-{stats.get('sentences_p75', '?')} sentences "
        f"(median {stats.get('sentences_median', '?')})."
    )
    lines.append("")
    lines.append(f"**Ceremony eligible: {'yes' if result['eligible'] else 'no'}** "
                 f"(needs {result['thresholds']['min_invariants']} invariants across "
                 f"{result['thresholds']['min_conversations']} conversations)")
    lines.append("")
    lines.append("## Candidate invariants")
    lines.append("")
    for cand in result["candidates"]:
        lines.append(f"- **{cand['statement']}** ({cand['category']}, {cand['provenance']}; "
                     f"{len(cand['evidence'])} quotes / {len(cand['conversations'])} conversations)")
        for ev in cand["evidence"][:3]:
            lines.append(f"  - {ev['date']} `{ev['conversation']}`: \"{ev['quote']}\"")
    if result["rejected"]:
        lines.append("")
        lines.append("## Rejected (deterministic gate)")
        lines.append("")
        for cand in result["rejected"]:
            lines.append(f"- {cand['statement'] or '(malformed)'} — {cand['rejected_reason']}")
    lines.append("")
    lines.append("To ratify: `lisan self ratify --from <this file> --provisional` "
                 "(edit this file first to prune candidates you reject).")
    write_markdown(path, frontmatter, "\n".join(lines))
    return path


# ── The ratification ceremony ────────────────────────────────────────────────

_VOICE_OR_PROVENANCE_RE = re.compile(
    r"^##\s+Voice(?:\s+Provenance)?\s*$.*?(?=^##\s|\Z)", re.MULTILINE | re.DOTALL
)


def ratify_voice(
    vault: Path | None = None,
    *,
    artifact_path: Path,
    provisional: bool = True,
    ratifier: str | None = None,
) -> Path:
    """Write the ratified voice block into the kernel via the ceremony path.

    Register policy (owner decision 2026-07-04): temperament is ratified as
    extracted; verbosity is bounded at the observed median band. Provenance
    goes into a separate ``## Voice Provenance`` section so it never enters
    the conversation prompt.
    """
    vault = vault or vault_root()
    doc = load_markdown(Path(artifact_path))
    data = (doc.frontmatter.get("voice_extraction") or {}) if isinstance(doc.frontmatter, dict) else {}
    candidates = [c for c in (data.get("candidates") or []) if str(c.get("statement") or "").strip()]
    if not candidates:
        raise ValueError(f"No valid candidates in artifact {artifact_path}; nothing to ratify.")
    stats = data.get("stats") or {}

    by_category: dict[str, list[dict[str, Any]]] = {}
    for cand in candidates:
        by_category.setdefault(str(cand.get("category") or "register"), []).append(cand)

    voice_lines = ["## Voice", ""]
    for category in _CATEGORIES:
        for cand in by_category.get(category, []):
            voice_lines.append(f"- {cand['statement']}")
    voice_lines.append("")
    p25, p75 = stats.get("sentences_p25"), stats.get("sentences_p75")
    if p25 and p75:
        voice_lines.append(
            f"- A typical reply runs {p25}-{p75} sentences; expand only when the content demands it, "
            "never for ceremony."
        )
    voice_block = "\n".join(voice_lines).rstrip() + "\n"

    who = ratifier or ("agent-provisional — pending owner review" if provisional else "owner")
    dates = stats.get("dates") or []
    window = f"{dates[0]}..{dates[-1]}" if dates else "unknown"
    earned = sum(1 for c in candidates if c.get("provenance") == "earned")
    provenance_block = "\n".join(
        [
            "## Voice Provenance",
            "",
            f"- ratified: {datetime.now().astimezone().date().isoformat()} by {who}",
            f"- accumulation window: {window} — {stats.get('turns', '?')} agent turns "
            f"across {stats.get('conversations', '?')} conversations",
            "- formed under: prompts/conversation_v1.md authored voice",
            f"- invariants: {earned} earned / {len(candidates) - earned} factory-traceable",
            f"- artifact: {Path(artifact_path).name}",
        ]
    ) + "\n"

    path = kernel_path(vault)
    text = path.read_text(encoding="utf-8")
    new_section = voice_block + "\n" + provenance_block
    if _VOICE_OR_PROVENANCE_RE.search(text):
        text = _VOICE_OR_PROVENANCE_RE.sub("", text).rstrip() + "\n\n" + new_section
    else:
        text = text.rstrip() + "\n\n" + new_section
    with ceremony():
        path.write_text(text, encoding="utf-8")
        stamp_kernel_hash(vault)
    return path
