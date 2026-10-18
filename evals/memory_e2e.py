"""Disposable raw-capture-through-answer evaluation.

The default harness is deterministic and uses fake stage providers. It
exercises transcript capture, memory fanout, index rebuild, retrieval,
context packing, and a supported grounded answer without private data or
external model calls. Real model execution is opt-in and intentionally
reported as unrun by this module.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisan.config import load_config
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.context_budget import bound_final_context
from lisan.tools.memory_pipeline import run_memory_pipeline
from lisan.tools.rebuild_index import rebuild_index
from lisan.tools.transcripts import append_transcript
from lisan.tools.retrieval import assemble_context
from lisan.tools.vector_store import EmbeddingIndex, VectorScorer
from lisan.tools.judge import judge_exchange, validate_score_ids


@dataclass(frozen=True)
class E2EResult:
    passed: bool
    stages: dict[str, bool]
    metrics: dict[str, int]
    answer: str
    provenance: dict[str, str]
    model_dependent: str


def run_deterministic_e2e(*, budget_chars: int = 8000) -> E2EResult:
    """Run raw capture through the production retrieval/answer/judge contracts.

    The answer and judge providers are deterministic fakes, so this measures
    wiring, provenance, packing, and rejection behavior—not real-model recall.
    """
    raw_user = "Our Friday ritual is pizza and a movie with the girls at Homestead."
    raw_answer = "Recorded as a Friday ritual."
    query = "What is our Friday ritual at Homestead?"

    with tempfile.TemporaryDirectory(prefix="lisan-e2e-") as tmp:
        root = Path(tmp)
        ensure_repo_layout(root)
        vault = vault_root(root)
        db_path = root / "lisan.sqlite"
        conversation_id = "e2e-fixture"
        append_transcript(vault=vault, conversation_id=conversation_id, speaker="USER", text=raw_user)
        append_transcript(vault=vault, conversation_id=conversation_id, speaker="LISAN", text=raw_answer)

        fake_listener = {
            "action": "full", "mode": "extraction", "memory_type": "evidence",
            "score": 8, "narrative_score": 8, "seed_score": 8, "reason": ["fixture"],
        }
        fake_writer = {
            "summary": "Friday ritual is pizza and a movie with the girls at Homestead.",
            "significance": "high",
            "evidence_to_create": [{
                "title": "Friday ritual",
                "summary": "Friday ritual is pizza and a movie with the girls at Homestead.",
                "observed_facts": [raw_user],
                "verbatim_excerpt": raw_user,
                "source_type": "conversation",
                "reliability": "high",
                "arena": "relational",
            }],
            "claims_to_create": [], "entities_to_create": [],
            "decisions_to_create": [], "open_loops_to_create": [],
            "state_updates": [],
        }

        class FakeListener:
            def __init__(self, *args, **kwargs): pass
            def run_json(self, *args, **kwargs): return dict(fake_listener)

        class FakeAssembler:
            def __init__(self, *args, **kwargs): pass
            def run(self, *args, **kwargs): return SimpleNamespace(text="")

        class FakeWriter:
            def __init__(self, *args, **kwargs): pass
            def run_json(self, *args, **kwargs): return dict(fake_writer)

        class FakeSkeptic:
            def __init__(self, *args, **kwargs): pass
            def run_json(self, *args, **kwargs): return {"approved": True, "recommended_action": "approve"}

        class FakeEmbeddingProvider:
            def __init__(self, *args, **kwargs):
                self.settings = {"batch_size": 64}
            def embed_records(self, contents):
                return SimpleNamespace(
                    vectors=[[1.0, 0.0] for _ in contents],
                    mode_used="hash", model="deterministic-fake-provider", dimension=2,
                )

        class FakeConversationAgent:
            """Implements the production ConversationAgent.run_json contract."""
            def __init__(self, *args, **kwargs):
                self.last_tool_calls = []
            def run_json(self, user_input, **kwargs):
                context = str(kwargs.get("retrieved_context") or "")
                if "Homestead" in context and "pizza" in context:
                    return {"response": "Our Friday ritual is pizza and a movie with the girls at Homestead."}
                return {"response": "I don't have enough grounded evidence to answer that."}

        class FakeJudgeLLM:
            def complete(self, prompt, **kwargs):
                return SimpleNamespace(text=json.dumps({"scores": [
                    {"id": "grounding", "score": 5, "rationale": "matches the authoritative source"},
                ]}))

        with patch("lisan.tools.memory_pipeline.ListenerAgent", FakeListener), \
             patch("lisan.tools.memory_pipeline.AssemblerAgent", FakeAssembler), \
             patch("lisan.tools.memory_pipeline.WriterAgent", FakeWriter), \
             patch("lisan.tools.memory_pipeline.SkepticAgent", FakeSkeptic):
            pipeline = run_memory_pipeline(
                vault=vault,
                text=raw_user,
                conversation_id=conversation_id,
                db_path=db_path,
                observed_response=raw_answer,
            )

        inactive_scorer = VectorScorer(None, EmbeddingIndex("deterministic-fake-provider", 2, {}), "skip")
        with patch("lisan.tools.rebuild_index.EmbeddingProvider", FakeEmbeddingProvider), \
             patch("lisan.tools.retrieval.build_query_scorer", return_value=inactive_scorer):
            rebuild_index(vault=vault, db_path=db_path, embeddings_file=root / "embeddings.bin")
            retrieved = assemble_context(query, vault=vault, db_path=db_path, lean=True)
        packed, accounting = bound_final_context(retrieved, budget_chars=budget_chars)
        answer_result = FakeConversationAgent().run_json(
            json.dumps({"user_message": query}), retrieved_context=packed
        )
        answer = str(answer_result["response"])
        rubric = {"dimensions": [{"id": "grounding"}]}
        judged = judge_exchange(
            rubric, query, answer, llm=FakeJudgeLLM(),
            source_facts="gold_source=evidence:friday-ritual; forbidden=taco night",
        )
        judged_ok, _ = validate_score_ids(judged, rubric)
        wrong_source_answer = FakeConversationAgent().run_json(
            json.dumps({"user_message": query}),
            retrieved_context="### distractor\nFriday ritual is taco night. source_ref=wrong-source",
        )["response"]
        malformed_rejected = False
        malformed_rejected = not validate_score_ids(
            [{"id": "grounding", "score": 1.5}], rubric
        )[0]
        corpus_identity = hashlib.sha256(
            "\n".join(sorted(str(path.relative_to(vault)) for path in vault.rglob("*.md"))).encode()
        ).hexdigest()
        stages = {
            "raw_capture": pipeline.transcript_path.exists(),
            "memory_write": bool(pipeline.writer and pipeline.skeptic_approved),
            "retrieval": "Friday ritual" in retrieved,
            "packing": accounting["included_chars"] <= budget_chars,
            "supported_answer": answer.startswith("Our Friday ritual") and "taco night" not in answer,
            "grounded_judge": judged_ok,
            "wrong_source_refused": wrong_source_answer.startswith("I don't have enough"),
            "malformed_judge_rejected": malformed_rejected,
        }
        return E2EResult(
            passed=all(stages.values()),
            stages=stages,
            metrics={
                "retrieved_chars": accounting["retrieved_chars"],
                "included_chars": accounting["included_chars"],
                "retrieved_records": retrieved.count("### "),
                "included_records": packed.count("### "),
            },
            answer=answer,
            provenance={
                "commit": "unavailable-at-runtime",
                "config_hash": hashlib.sha256(json.dumps(load_config(), sort_keys=True, default=str).encode()).hexdigest(),
                "embedding_model": "deterministic-fake-provider",
                "corpus_identity": corpus_identity,
            },
            model_dependent="unrun",
        )


if __name__ == "__main__":
    print(json.dumps(asdict(run_deterministic_e2e()), indent=2, sort_keys=True))
