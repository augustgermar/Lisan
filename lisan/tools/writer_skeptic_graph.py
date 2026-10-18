from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..agents import SkepticAgent, WriterAgent
from .deixis import tokenize_principal_obj


class WriterSkepticState(TypedDict, total=False):
    source_text: str
    writer_task: str
    writer: dict[str, Any]
    skeptic: dict[str, Any]
    revision_count: int


def run_writer_skeptic_graph(
    *,
    vault: Path,
    text: str,
    writer_task: str,
    writer_kwargs: dict[str, Any],
    skeptic_kwargs: dict[str, Any],
    max_revisions: int = 2,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Run the narrow Writer <-> Skeptic editorial loop.

    LangGraph owns only transient workflow state. Lisan's vault remains the
    canonical durable store, and downstream fanout remains outside this graph.

    Skeptic feedback is editorial context, never evidence. On revision passes
    Writer always receives the original source text as INPUT plus the previous
    draft and Skeptic review as labeled context.
    """

    def writer_node(state: WriterSkepticState) -> dict[str, Any]:
        prior_writer = state.get("writer")
        prior_skeptic = state.get("skeptic")
        revision_count = int(state.get("revision_count", 0))
        kwargs = dict(writer_kwargs)

        if prior_writer is not None and prior_skeptic is not None:
            revision_count += 1
            kwargs.update(
                {
                    "previous_writer_draft": json.dumps(prior_writer, indent=2, ensure_ascii=True),
                    "skeptic_feedback": json.dumps(prior_skeptic, indent=2, ensure_ascii=True),
                    "revision_instructions": (
                        "Revise the draft while remaining grounded in the original INPUT. "
                        "Skeptic feedback is editorial guidance, not evidence and not a source "
                        "of new facts. Do not add a fact merely because the Skeptic suggested it."
                    ),
                }
            )

        writer = WriterAgent(vault=vault).run_json(
            state["source_text"],
            task=state["writer_task"],
            **kwargs,
        )
        writer = tokenize_principal_obj(writer, vault)
        return {"writer": writer, "revision_count": revision_count}

    def skeptic_node(state: WriterSkepticState) -> dict[str, Any]:
        skeptic = SkepticAgent(vault=vault).run_json(
            json.dumps(state["writer"], indent=2, ensure_ascii=True),
            **skeptic_kwargs,
        )
        return {"skeptic": skeptic}

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
        }
    )
    return dict(result["writer"]), dict(result["skeptic"]), int(result.get("revision_count", 0))
