"""Bounded Writer <-> Skeptic editorial loop.

LangGraph owns only transient workflow state. Lisan's vault remains the
canonical durable store, and downstream fanout, memory writes, and indexing
remain outside this graph.

Provenance guarantee (structural, not prompt-based): the Skeptic's free-form
fields -- observed_facts, interpretations, alternative_hypotheses,
evidence_needed prose, claim_updates notes, confidence_adjustments -- NEVER
cross into Writer input. Only a closed vocabulary of issue categories,
JSON-path targets into the current draft, permitted editorial operations,
and short editorial notes pass through. A prompt instruction alone cannot
enforce this; the allowlist below does.

Exhaustion: when the revision cap is reached with a final "revise", the
disposition is "revision_exhausted" -- a distinct terminal state, never a
silent fall-through to "store with caveats". The storage policy for that
disposition is decided by the caller (memory_pipeline withholds
evidence/claim/state fanout and stores the draft for review).
"""
from __future__ import annotations

import json
import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict

from ..agents import SkepticAgent, WriterAgent
from .deixis import tokenize_principal_obj
from .tracing import record_inline_step


# ---------------------------------------------------------------------------
# Structured revision feedback: the provenance boundary.
# ---------------------------------------------------------------------------

# Closed vocabulary of issue categories. Anything the Skeptic emits outside
# this set is normalized to "general_editorial".
ISSUE_CATEGORIES = frozenset({
    "unsupported_claim",
    "unmarked_inference",
    "unscoped_uncertainty",
    "placeholder",
    "high_risk",
    "low_evidence",
    "contradiction",
    "missing_counterexample_search",
    "general_editorial",
})

# Skeptic issue types (from the deterministic fallback and the skeptic
# prompt schema) mapped onto the closed categories above.
_SKEPTIC_TYPE_MAP = {
    "interpretation": "unmarked_inference",
    "uncertainty": "unscoped_uncertainty",
    "placeholder": "placeholder",
    "high_risk": "high_risk",
    "low_evidence": "low_evidence",
    "pattern_risk": "unsupported_claim",
    "counterexample_search": "missing_counterexample_search",
}

# Permitted editorial operations per category. "hold_for_review" never edits;
# it routes the draft to human review instead.
CATEGORY_OPERATIONS = {
    "unsupported_claim": ("remove", "ground_in_input", "mark_inference"),
    "unmarked_inference": ("mark_inference",),
    "unscoped_uncertainty": ("scope_uncertainty", "remove"),
    "placeholder": ("remove", "mark_unresolved"),
    "high_risk": ("hold_for_review",),
    "low_evidence": ("lower_confidence", "mark_unresolved"),
    "contradiction": ("resolve", "flag_unresolved"),
    "missing_counterexample_search": ("mark_unresolved",),
    "general_editorial": ("clarify",),
}

# Skeptic output fields that may carry factual prose. None of these may
# appear in Writer input -- they are the provenance-laundering vector.
_FACTUAL_SKEPTIC_FIELDS = frozenset({
    "observed_facts",
    "interpretations",
    "alternative_hypotheses",
    "evidence_needed",
    "claim_updates",
    "confidence_adjustments",
    "priority_questions",
})


def normalize_issue_category(raw_type: Any) -> str:
    """Map a Skeptic issue type onto the closed category vocabulary."""
    key = str(raw_type or "").strip().lower()
    if key in ISSUE_CATEGORIES:
        return key
    return _SKEPTIC_TYPE_MAP.get(key, "general_editorial")


def default_operation(category: str) -> str:
    """The first permitted operation for a category (deterministic)."""
    return CATEGORY_OPERATIONS[category][0]


def structure_skeptic_feedback(
    skeptic: dict[str, Any] | None,
    revision: int,
) -> dict[str, Any]:
    """Build the typed revision-feedback payload for Writer.

    Only issue categories, draft targets, permitted operations, and short
    editorial notes cross this boundary. All factual prose fields from the
    Skeptic output are dropped structurally -- Writer can never receive
    them, regardless of what the revision prompt says.
    """
    issues: list[dict[str, Any]] = []
    for issue in (skeptic or {}).get("issues") or []:
        if not isinstance(issue, dict):
            continue
        category = normalize_issue_category(issue.get("type"))
        target = str(issue.get("target") or issue.get("path") or "$")
        issues.append(
            {
                "category": category,
                "target": target,
                "operation": default_operation(category),
                "note": str(issue.get("message") or "")[:300],
            }
        )
    return {"feedback_version": 1, "revision": revision, "issues": issues}


def feedback_contains_factual_prose(feedback: dict[str, Any]) -> bool:
    """Test helper: True if any denylisted factual field leaked through."""
    text = json.dumps(feedback, ensure_ascii=True)
    # The structured payload must never carry these keys at all.
    return any(f'"{field}"' in text for field in _FACTUAL_SKEPTIC_FIELDS)


# ---------------------------------------------------------------------------
# Disposition and audit.
# ---------------------------------------------------------------------------

DISPOSITIONS = frozenset({"approved", "revision_exhausted", "held", "revise"})


def final_disposition(
    skeptic: dict[str, Any] | None,
    revision_count: int,
    max_revisions: int,
) -> str:
    """Resolve the graph's terminal disposition.

    "revise" at the revision cap becomes "revision_exhausted" -- a distinct
    terminal state, never a silent fall-through to "store with caveats".
    Unknown or empty actions fail closed to "held".
    """
    action = str(((skeptic or {}).get("recommended_action")) or "").lower()
    if action == "approve":
        return "approved"
    if action in {"hold", "needs_revision"}:
        return "held"
    if action == "revise":
        if revision_count >= max_revisions:
            return "revision_exhausted"
        return "revise"
    return "held"


def build_audit(
    skeptic: dict[str, Any] | None,
    revision_count: int,
    max_revisions: int,
    disposition: str,
    issue_categories: list[str] | None = None,
) -> dict[str, Any]:
    """Audit metadata for the draft frontmatter and downstream inspection.

    issue_categories accumulates what the Skeptic flagged across ALL
    reviews in the loop (not just the final one), so an approved-after-
    revision draft still records what had to be fixed.
    """
    if issue_categories is None:
        issue_categories = sorted(
            {
                normalize_issue_category(issue.get("type"))
                for issue in ((skeptic or {}).get("issues") or [])
                if isinstance(issue, dict)
            }
        )
    return {
        "revision_count": int(revision_count),
        "max_revisions": int(max_revisions),
        "disposition": disposition,
        "issue_categories": sorted(set(issue_categories)),
        "approved_immediately": disposition == "approved" and int(revision_count) == 0,
    }


def categories_of(skeptic: dict[str, Any] | None) -> list[str]:
    """Normalized issue categories from one Skeptic review."""
    return sorted(
        {
            normalize_issue_category(issue.get("type"))
            for issue in ((skeptic or {}).get("issues") or [])
            if isinstance(issue, dict)
        }
    )


# ---------------------------------------------------------------------------
# Graph state and nodes.
# ---------------------------------------------------------------------------


class WriterSkepticState(TypedDict, total=False):
    source_text: str
    writer_task: str
    writer: dict[str, Any]
    skeptic: dict[str, Any]
    revision_count: int
    issue_categories: Annotated[list[str], operator.add]


def _writer_kwargs_for_revision(
    base_kwargs: dict[str, Any],
    prior_writer: dict[str, Any],
    prior_skeptic: dict[str, Any],
    revision: int,
) -> dict[str, Any]:
    """Writer kwargs for a revision pass: structured feedback only.

    The previous draft and the ORIGINAL source text travel along; the
    Skeptic's factual prose does not. The revision instructions restate the
    provenance rule, but the structural allowlist above is the guarantee.
    """
    kwargs = dict(base_kwargs)
    kwargs.update(
        {
            "previous_writer_draft": json.dumps(prior_writer, indent=2, ensure_ascii=True),
            "revision_feedback": json.dumps(
                structure_skeptic_feedback(prior_skeptic, revision),
                indent=2,
                ensure_ascii=True,
            ),
            "revision_instructions": (
                "Revise the draft while remaining grounded in the original INPUT. "
                "The revision_feedback lists typed editorial issues "
                "(category/target/operation). It contains no new facts: issue "
                "notes are editorial guidance, not evidence and not a source "
                "of new facts. Do not add a fact merely because feedback "
                "mentions it. Apply each operation at its target; if a target "
                "cannot be located, leave the material unchanged."
            ),
        }
    )
    return kwargs


def _run_bounded_loop_fallback(
    *,
    vault: Path,
    text: str,
    writer_task: str,
    writer_kwargs: dict[str, Any],
    skeptic_kwargs: dict[str, Any],
    max_revisions: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Deterministic fallback when LangGraph is not installed.

    Mirrors the graph's contract exactly (writer, skeptic, audit) so the
    optional dependency never changes downstream behavior.
    """
    writer = WriterAgent(vault=vault).run_json(
        text, task=writer_task, **writer_kwargs,
    )
    writer = tokenize_principal_obj(writer, vault)
    revision_count = 0
    skeptic = SkepticAgent(vault=vault).run_json(
        json.dumps(writer, indent=2, ensure_ascii=True), **skeptic_kwargs,
    )
    seen_categories = set(categories_of(skeptic))
    while (
        str(skeptic.get("recommended_action") or "").lower() == "revise"
        and revision_count < max_revisions
    ):
        revision_count += 1
        record_inline_step("memory_pipeline.writer")
        writer = WriterAgent(vault=vault).run_json(
            text,
            task=writer_task,
            **_writer_kwargs_for_revision(writer_kwargs, writer, skeptic, revision_count),
        )
        writer = tokenize_principal_obj(writer, vault)
        record_inline_step("memory_pipeline.skeptic")
        skeptic = SkepticAgent(vault=vault).run_json(
            json.dumps(writer, indent=2, ensure_ascii=True), **skeptic_kwargs,
        )
        seen_categories.update(categories_of(skeptic))
    disposition = final_disposition(skeptic, revision_count, max_revisions)
    audit = build_audit(skeptic, revision_count, max_revisions, disposition,
                        sorted(seen_categories))
    return dict(writer), dict(skeptic), audit


def run_writer_skeptic_graph(
    *,
    vault: Path,
    text: str,
    writer_task: str,
    writer_kwargs: dict[str, Any],
    skeptic_kwargs: dict[str, Any],
    max_revisions: int = 2,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Run the narrow Writer <-> Skeptic editorial loop.

    LangGraph owns only transient workflow state. Lisan's vault remains the
    canonical durable store, and downstream fanout remains outside this graph.

    Returns (writer, skeptic, audit) where audit carries revision_count,
    disposition, issue_categories, and approved_immediately. When LangGraph
    is not installed, a deterministic fallback runs the identical bounded
    loop so the optional dependency never changes the contract.
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return _run_bounded_loop_fallback(
            vault=vault,
            text=text,
            writer_task=writer_task,
            writer_kwargs=writer_kwargs,
            skeptic_kwargs=skeptic_kwargs,
            max_revisions=max_revisions,
        )

    def writer_node(state: WriterSkepticState) -> dict[str, Any]:
        record_inline_step("memory_pipeline.writer")
        prior_writer = state.get("writer")
        prior_skeptic = state.get("skeptic")
        revision_count = int(state.get("revision_count", 0))
        kwargs = dict(writer_kwargs)

        if prior_writer is not None and prior_skeptic is not None:
            revision_count += 1
            kwargs = _writer_kwargs_for_revision(
                writer_kwargs, prior_writer, prior_skeptic, revision_count,
            )

        writer = WriterAgent(vault=vault).run_json(
            state["source_text"],
            task=state["writer_task"],
            **kwargs,
        )
        writer = tokenize_principal_obj(writer, vault)
        return {"writer": writer, "revision_count": revision_count}

    def skeptic_node(state: WriterSkepticState) -> dict[str, Any]:
        record_inline_step("memory_pipeline.skeptic")
        skeptic = SkepticAgent(vault=vault).run_json(
            json.dumps(state["writer"], indent=2, ensure_ascii=True),
            **skeptic_kwargs,
        )
        return {"skeptic": skeptic, "issue_categories": categories_of(skeptic)}

    def route_after_skeptic(state: WriterSkepticState) -> str:
        action = str((state.get("skeptic") or {}).get("recommended_action") or "").lower()
        if action == "revise" and int(state.get("revision_count", 0)) < max_revisions:
            return "writer"
        return END

    graph = StateGraph(WriterSkepticState)
    graph.add_node("writer", writer_node)
    graph.add_node("skeptic", skeptic_node)
    graph.add_edge(START, "writer")
    graph.add_edge("writer", "skeptic")
    graph.add_conditional_edges("skeptic", route_after_skeptic, {"writer": "writer", END: END})
    app = graph.compile()

    result = app.invoke(
        {
            "source_text": text,
            "writer_task": writer_task,
            "revision_count": 0,
            "issue_categories": [],
        }
    )
    writer = dict(result["writer"])
    skeptic = dict(result["skeptic"])
    revision_count = int(result.get("revision_count", 0))
    disposition = final_disposition(skeptic, revision_count, max_revisions)
    audit = build_audit(
        skeptic, revision_count, max_revisions, disposition,
        list(result.get("issue_categories") or []),
    )
    return writer, skeptic, audit
