from __future__ import annotations

import json
import re
from typing import Any

from ..prompts import load_prompt
from ..tools.heuristic_gate import score_text
from ..utils import approx_word_count
from .base import PromptAgent


def _truncate_summary(text: str, cap: int) -> str:
    """Return *text* truncated to at most *cap* chars, respecting word and
    sentence boundaries when possible (Finding #7).

    Prefers the last sentence boundary inside the window. Falls back to the
    last word boundary with an ellipsis. Never returns a mid-word slice.
    """
    if len(text) <= cap:
        return text
    window = text[:cap]
    # Sentence boundaries — search from the right, take the closest meaningful one.
    sentence_end = max(
        window.rfind(". "),
        window.rfind("? "),
        window.rfind("! "),
        window.rfind(".\n"),
        window.rfind("?\n"),
        window.rfind("!\n"),
    )
    # Only accept a sentence boundary if it leaves a non-trivial summary
    # behind — at least 40% of the cap (so a tiny first sentence doesn't
    # short-circuit a long summary, but a meaningful first sentence wins).
    if sentence_end >= max(12, (cap * 2) // 5):
        return text[: sentence_end + 1].rstrip()
    # Word boundary fallback. rsplit guarantees no mid-word break.
    trimmed = window.rsplit(" ", 1)[0].rstrip(".,;:!?-—")
    if not trimmed:
        # Pathological input with no whitespace; hard slice as last resort.
        return window
    return trimmed + "…"


_TASK_PROMPT_FILES = {
    # v0.1.9: the legacy single-shot episode prompt is kept as a fallback
    # alias for callers that haven't migrated to the split.
    "episode":           "writer_episode_v1",
    "episode_core":      "writer_episode_core_v1",
    "episode_artifacts": "writer_episode_artifacts_v1",
    "episode_full_turn": "writer_full_turn_v1",
    "decision":  "writer_decision_v1",
    "open_loop": "writer_open_loop_v1",
    "state":     "writer_state_v1",
    "entity":         "writer_entity_v1",
    "entity_story":   "writer_entity_story_v1",
    "knowledge":      "writer_knowledge_v1",
    "questions":      "writer_questions_v1",
}


class WriterAgent(PromptAgent):
    name = "writer"
    prompt_file = "writer_episode_v1"
    output_schema_name = "writer_output"

    def prompt(self) -> str:
        if self.prompt_file == "writer_full_turn_v1":
            base = load_prompt("writer_episode_v1")
            addon = load_prompt("writer_full_turn_v1")
            from ..tools.deixis import render_deixis

            return render_deixis(base + "\n\n" + addon, self.prompt_audience, self.vault)
        return super().prompt()

    def run_json(self, user_input: str, **kwargs: Any) -> Any:
        task = str(kwargs.get("task") or "episode")
        self.prompt_file = _TASK_PROMPT_FILES.get(task, "writer_episode_v1")
        return super().run_json(user_input, **kwargs)

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        task = str(kwargs.get("task") or "episode")
        summary = self._summary_from_input(user_input)
        payload = {
            "record_type": task,
            "summary": summary,
            "significance": significance,
            "frontmatter": {
                "summary": summary,
                "significance": significance,
                "confidence": "low",
                "confidence_basis": "Deterministic fallback writer",
                "review_after": kwargs.get("review_after") or "",
                "links": kwargs.get("links") or [],
            },
            "sections": self._sections(task, user_input),
            "questions": self._questions(user_input),
            "significance_rationale": self._significance_rationale(user_input, significance),
            "entities_to_create": self._extract_entity_stubs(user_input),
            "state_updates": [],
            "open_loops_to_create": [],
            "decisions_to_create": [],
        }
        return json.dumps(payload, indent=2, ensure_ascii=True)

    def parse_output(self, text: str) -> Any | None:
        parsed = super().parse_output(text)
        if isinstance(parsed, dict):
            return parsed
        return None

    # Finding #7: the working summary returned by the fallback writer is what
    # the skeptic and the interlocutor see. The draft frontmatter's `summary`
    # field has its own 120-char convention enforced at write time
    # (memory_pipeline._write_draft uses str(...)[:120]). Giving the working
    # summary more room (240 chars) lets a full first sentence survive so
    # downstream agents have a coherent thought to react to.
    _WORKING_SUMMARY_CAP = 240

    def _summary_from_input(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return "Draft memory"
        first = lines[0]
        return _truncate_summary(first, self._WORKING_SUMMARY_CAP)

    def _sections(self, task: str, text: str) -> dict[str, str]:
        if task == "questions":
            questions = self._questions(text)
            return {"questions": "\n".join(f"- {q}" for q in questions)}
        if task == "state":
            return {"current_state": text.strip() or "No state summary provided."}
        if task == "decision":
            return {"decision": text.strip() or "No decision text provided."}
        if task == "open_loop":
            return {"open_loop": text.strip() or "No open loop text provided."}
        if task == "knowledge":
            return {"knowledge": text.strip() or "No knowledge statement provided."}
        if task == "entity":
            return {"identity": text.strip() or "No identity details provided."}
        return {
            "event_timeline": text.strip() or "No event timeline provided.",
            "documented_evidence": "No evidence recorded yet.",
            "user_reported_context": "No additional context recorded yet.",
            "interpretations": "No interpretations recorded yet.",
            "operational_consequences": "No consequences recorded yet.",
            "open_questions": "No open questions recorded yet.",
        }

    def _questions(self, text: str) -> list[str]:
        heuristics = score_text(text, vault=self.vault)
        questions: list[str] = []
        if "decision phrase" in heuristics.reasons:
            questions.append("What alternative options were considered?")
        if "high-stakes term" in heuristics.reasons:
            questions.append("What factual details need verification before recording this?")
        if "possible named entity" in heuristics.reasons:
            questions.append("Which person or entity is this referring to?")
        if approx_word_count(text) > 60:
            questions.append("What is the simplest summary that still preserves the durable point?")
        if not questions:
            questions.append("What detail would most change the meaning of this memory?")
        return questions[:5]

    def _extract_entity_stubs(self, text: str) -> list[dict[str, str]]:
        """Deterministic fallback: extract capitalized proper nouns as entity stubs.

        Finding #4: the previous version emitted any capitalized word as
        ``subtype: "person"`` with a generic placeholder summary. The new
        logic:

        1. Pulls the primer-known cast first — first-name-only mentions of
           known people produce stubs immediately.
        2. Skips a much broader stopword list (days, interrogatives, adverbs,
           tools). Months are *not* in the general stopword set; users can be
           named after months (e.g. "August"). They are blocked only when
           absent from the primer cast.
        3. Requires multi-word capitalization shape for person stubs when the
           candidate is not in the primer cast.
        4. Emits ``kind`` plus a summary derived from the sentence containing
           the name, so downstream extraction gets a useful seed instead of a
           placeholder.
        """
        from ..tools.primer_index import known_names as _primer_known_names
        from ..tools.stopwords import (
            SENTENCE_INITIAL_OR_TOOL_STOPWORDS,
            MONTH_STOPWORDS,
            DAY_STOPWORDS,
        )

        primer_cast = _primer_known_names(self.vault)

        _PLACE_SUFFIXES = ("ranch", "farm", "park", "lake", "valley", "beach",
                           "street", "avenue", "road", "way", "drive", "blvd",
                           "mountain", "river", "forest")
        stubs: list[dict[str, str]] = []
        seen: set[str] = set()

        def _sentence_for_name(name: str) -> str:
            pattern = re.compile(rf"[^.?!]*\b{re.escape(name)}\b[^.?!]*[.?!]?", re.I)
            match = pattern.search(text)
            if match:
                snippet = match.group(0).strip()
            else:
                snippet = text.strip()
            return _truncate_summary(snippet or f"{name} mentioned in conversation.", 160)

        for match in re.finditer(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+)?\b", text):
            name = match.group(0)
            if name in seen:
                continue
            seen.add(name)

            # Possessives ("Nates" when "Nate" already seen).
            if name.endswith("s") and name[:-1] in seen:
                continue

            primer_hit = name in primer_cast

            # Stopword check — primer hits are exempt.
            if not primer_hit:
                if name in SENTENCE_INITIAL_OR_TOOL_STOPWORDS:
                    continue
                if name in DAY_STOPWORDS:
                    continue
                if name in MONTH_STOPWORDS:
                    # Month-shaped names need primer support to be considered.
                    continue
                # Single-word capitalized terms that aren't in the primer cast
                # are usually adverbs or sentence-starters; skip them. Place
                # names with a clear suffix get a free pass below.
                if " " not in name:
                    lower = name.lower()
                    if not any(lower.endswith(s) for s in _PLACE_SUFFIXES):
                        continue

            lower = name.lower()
            if any(lower.endswith(s) for s in _PLACE_SUFFIXES) or any(s in lower for s in _PLACE_SUFFIXES):
                kind = "place"
            else:
                kind = "person"
            stubs.append(
                {
                    "name": name,
                    "kind": kind,
                    "summary": _sentence_for_name(name),
                    "confidence_basis": f"Extracted from the sentence mentioning {name}.",
                }
            )
        return stubs[:10]

    def _significance_rationale(self, text: str, significance: str) -> str:
        if significance == "high":
            return "Marked high significance because the input contains durable, review-worthy content."
        if approx_word_count(text) > 80:
            return "Marked medium significance because the input is substantive and may recur."
        return "Marked low significance because the input appears routine."
