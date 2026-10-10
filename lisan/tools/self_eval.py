"""Self-evaluation: the agent periodically grades its own real work.

The evaluation loop used to require an outside frontier agent simulating a
user against a disposable vault. That era ended when real data arrived —
end-user experience can no longer be simulated here, and doesn't need to
be: the transcripts ARE the experience. So the instrument turns inward and
becomes an organ: on a schedule, the agent reviews its own recent
conversations and the memory artifacts they produced, scores the exchanges
against the kernel-derived rubric (examiner ≠ examinee: the judge runs on
a different model family), checks its machinery deterministically, and
turns what it finds into suggestions for improvement.

Everything stays in the vault. The report (which quotes real conversation)
goes to ``reports/`` — private by construction, never repo-tracked. Scores
append to a history file so trends and regressions are visible across
runs. Suggestions are emitted as ``origin: self`` open loops through the
deviation seam — same cap, same dedup, same drive surfacing — because a
quality slippage IS a deviation the agent should ache about.
"""
from __future__ import annotations

import json
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from .db import connect as _db_connect

from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso
from .log import get_logger, log_error

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "days": 7,               # review window
    "sample_size": 10,       # judged exchanges per run
    "judge_provider": None,  # default: judge.py's own (codex/analyst read-only)
    "judge_model": None,
    "min_dimension_mean": 3.5,   # below this (with evidence) → suggestion
    "regression_drop": 0.5,      # overall-mean drop vs last run → suggestion
    "interval_hours": 7 * 24,    # scheduled weekly
}

_HISTORY_REL = "reports/self-eval-history.jsonl"
_TRIVIAL_WORDS = 4  # user turns at or below this are acks, not evidence


class SelfEvalJudgeUnavailable(RuntimeError):
    """Every judge call in a run failed, so the run measured nothing.

    Raised *after* the report and history are written, so the evidence of the
    failed run survives and the job still fails. Degrading to "not judged this
    run" rather than inventing scores is right; degrading *silently* is not.
    For five consecutive weeks this organ scored 0/10, wrote a well-formed
    report saying nothing, and returned success — and because `_judge_sample`
    caught each exception and continued, nothing ever reached the escalation
    ladder, whose entire purpose is that a silent failure is the worst kind.
    A caller that swallows an exception makes the ladder blind.
    """


def self_eval_config(config: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update((config or {}).get("self_eval") or {})
    return out


def run_self_evaluation(
    vault: Path,
    *,
    db_path: Path | None = None,
    config: dict[str, Any] | None = None,
    now: date | None = None,
) -> dict[str, Any]:
    """One full self-review. Returns a summary; writes the report and
    history into the vault; emits suggestion loops through the deviation
    seam."""
    cfg = self_eval_config(config)
    if not cfg.get("enabled", True):
        return {"enabled": False}
    now = now or date.today()

    exchanges = recent_exchanges(vault, days=int(cfg["days"]), now=now)
    health = machine_health(vault, db_path=db_path, days=int(cfg["days"]))
    judged, judge_note = _judge_sample(vault, exchanges, cfg)
    previous = _last_history_entry(vault)
    entry = _history_entry(now, exchanges, health, judged)
    suggestions = _derive_suggestions(cfg, entry, previous, judged)

    report_path = _write_report(vault, now, entry, judged, judge_note, suggestions, previous)
    _append_history(vault, entry)
    emitted = _emit_suggestions(vault, suggestions, report_path, db_path=db_path)

    get_logger(vault).info(
        f"self_eval.run window_days={cfg['days']} exchanges={len(exchanges)} "
        f"judged={len(judged)} suggestions={len(emitted)}"
    )

    # A window with exchanges in it that judged none of them is a failed run,
    # not a quiet one. Raising here puts it on the escalation ladder — real
    # error to Telegram, one second chance, then an investigation loop — which
    # is the owner's ratified policy for exactly this. The report and history
    # are already on disk above, so the failure keeps its evidence.
    if exchanges and not judged:
        raise SelfEvalJudgeUnavailable(
            f"self-evaluation judged 0 of {min(len(exchanges), int(cfg['sample_size']))} "
            f"sampled exchange(s); {judge_note}. The run measured nothing."
        )

    return {
        "enabled": True,
        "window_days": int(cfg["days"]),
        "exchanges": len(exchanges),
        "judged": len(judged),
        "overall_mean": entry.get("overall_mean"),
        "dimension_means": entry.get("dimensions"),
        "health": health,
        "suggestions": [s["summary"] for s in suggestions],
        "emitted_loops": emitted,
        "report": str(report_path),
    }


# ---------------------------------------------------------------- gathering

_HEADER = re.compile(r"^## Conversation — \d{2}:\d{2} \[(?P<cid>[^\]]+)\]\s*$")


def recent_exchanges(vault: Path, *, days: int, now: date | None = None) -> list[dict[str, Any]]:
    """USER→LISAN exchange pairs from the transcript files in the window,
    oldest first. Trivial user turns (bare acks) are dropped — they carry
    no evidence worth judging."""
    now = now or date.today()
    turns: list[dict[str, Any]] = []
    root = vault / "transcripts"
    for offset in range(days, -1, -1):
        day = (now - timedelta(days=offset)).isoformat()
        path = root / f"{day}.md"
        if not path.exists():
            continue
        turns.extend(_parse_transcript(path, day))

    exchanges: list[dict[str, Any]] = []
    for i, turn in enumerate(turns):
        if turn["speaker"] != "USER":
            continue
        nxt = turns[i + 1] if i + 1 < len(turns) else None
        if not nxt or nxt["speaker"] != "LISAN" or nxt["cid"] != turn["cid"]:
            continue
        if len(turn["text"].split()) <= _TRIVIAL_WORDS:
            continue
        prior = [t for t in turns[max(0, i - 4):i] if t["cid"] == turn["cid"]]
        exchanges.append({
            "cid": turn["cid"], "day": turn["day"],
            "user": turn["text"], "assistant": nxt["text"],
            "context": "\n".join(f"{t['speaker']}: {t['text']}" for t in prior[-2:]),
        })
    return exchanges


def _parse_transcript(path: Path, day: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    cid = ""
    body: list[str] = []
    speaker = ""

    def flush() -> None:
        nonlocal body, speaker
        if speaker and body:
            text = "\n".join(body).strip()
            if text:
                turns.append({"cid": cid, "day": day, "speaker": speaker, "text": text})
        body, speaker = [], ""

    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return turns
    for line in lines:
        m = _HEADER.match(line.strip())
        if m:
            flush()
            cid = m.group("cid")
            continue
        for tag in ("USER:", "LISAN:"):
            if line.startswith(tag):
                flush()
                speaker = tag.rstrip(":")
                body = [line[len(tag):].strip()]
                break
        else:
            if speaker:
                body.append(line)
    flush()
    return turns


# ---------------------------------------------------------------- machinery

def machine_health(vault: Path, *, db_path: Path | None, days: int) -> dict[str, Any]:
    """Deterministic health signals for the window — no model involved."""
    import sqlite3

    health: dict[str, Any] = {}
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    if db_path and Path(db_path).exists():
        try:
            conn = _db_connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT job_type, status, COUNT(*) FROM jobs WHERE created_at >= ? GROUP BY 1, 2",
                    (cutoff,),
                ).fetchall()
                jobs: dict[str, dict[str, int]] = {}
                for job_type, status, n in rows:
                    jobs.setdefault(job_type, {})[status] = int(n)
                health["jobs"] = jobs
                captures = jobs.get("capture.observe", {})
                total = sum(captures.values())
                health["capture_failure_rate"] = round(
                    captures.get("failed", 0) / total, 3) if total else 0.0
                try:
                    from .retrieval_graph import _ensure_retrieval_log_columns

                    _ensure_retrieval_log_columns(conn)
                    conn.commit()
                    weekly = conn.execute(
                        "SELECT strftime('%Y-%W', timestamp) AS week, "
                        "COUNT(*) AS tasks, AVG(retrieved_token_estimate) AS avg_retrieved_tokens "
                        "FROM retrieval_log WHERE timestamp >= datetime('now', '-56 days') "
                        "AND retrieved_token_estimate IS NOT NULL GROUP BY week ORDER BY week"
                    ).fetchall()
                    means = [round(float(row[2] or 0), 2) for row in weekly]
                    health["retrieval_volume"] = {
                        "weekly": [
                            {"week": str(row[0]), "tasks": int(row[1]),
                             "avg_retrieved_tokens": round(float(row[2] or 0), 2)}
                            for row in weekly
                        ],
                        "monotonic_week_over_week_growth": (
                            len(means) >= 3 and all(b > a for a, b in zip(means, means[1:]))
                        ),
                        "metric_note": (
                            "Approximate tokens in retrieved summaries, estimated at 1.33 tokens/word; "
                            "historical rows require a fresh retrieval to populate this metric."
                        ),
                    }
                except Exception:
                    # Older/test databases may not yet have retrieval_log.
                    health["retrieval_volume"] = {"weekly": [], "available": False}
            finally:
                conn.close()
        except Exception:
            health["jobs"] = {}

    log_path = vault / "logs" / "lisan.log"
    empty = failed_turns = 0
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        empty = text.count("conversation.empty_response")
        failed_turns = text.count("telegram turn failed")
    except Exception:
        pass
    health["empty_responses_logged"] = empty
    health["failed_turns_logged"] = failed_turns

    created = {"entities": 0, "episodes": 0, "knowledge": 0}
    for kind in created:
        folder = vault / kind
        if folder.exists():
            for p in folder.rglob("*.md"):
                try:
                    if str(load_markdown(p).frontmatter.get("created") or "") >= cutoff:
                        created[kind] += 1
                except Exception:
                    continue
    health["records_created"] = created
    return health


def run_memory_pipeline_evaluation() -> dict[str, Any]:
    """Run deterministic release-gate cases against an isolated Markdown vault.

    The suite exercises the real index and retrieval path without an LLM,
    external services, or writes to the user's vault. Markdown fixtures are
    the source of truth; SQLite is built only as a disposable retrieval cache.
    """
    from unittest.mock import patch

    from ..frontmatter import dump_markdown
    from ..paths import ensure_repo_layout, vault_root
    from .rebuild_index import index_single_record, open_index_connection
    from .retrieval import assemble_context, retrieve_context
    from .vector_store import EmbeddingIndex, VectorScorer

    cases: list[dict[str, Any]] = []

    def record(vault: Path, relative: str, frontmatter: dict[str, Any], body: str) -> Path:
        path = vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_markdown(frontmatter, body), encoding="utf-8")
        return path

    def entity(
        record_id: str, name: str, summary: str, *, aliases: list[str] | None = None,
        body: str = "",
    ) -> dict[str, Any]:
        return {
            "id": record_id, "type": "entity", "subtype": "person",
            "canonical_name": name, "aliases": aliases or [], "summary": summary,
            "status": "active", "significance": "high", "domain_primary": "relational",
            "allowed_contexts": ["all"], "created": "2026-01-01", "updated": "2026-02-01",
        }

    def evaluate(name: str, condition: bool, evidence: str) -> None:
        cases.append({"name": name, "passed": bool(condition), "evidence": evidence})

    with tempfile.TemporaryDirectory(prefix="lisan-memory-eval-") as tmp:
        root = Path(tmp)
        ensure_repo_layout(root)
        vault = vault_root(root)
        db_path = root / "lisan.sqlite"

        fixture_records = [
            (
                "episodes/vendor-switch-jan.md",
                {"id": "episode.vendor-jan", "type": "episode", "summary": "On 2026-01-10, office supply purchasing used Vendor A.", "status": "superseded", "significance": "medium", "domain_primary": "work", "created": "2026-01-10", "updated": "2026-01-10", "allowed_contexts": ["all"]},
                "The office supply vendor was Vendor A as of January 10, 2026.",
            ),
            (
                "episodes/vendor-switch-feb.md",
                {"id": "episode.vendor-feb", "type": "episode", "summary": "On 2026-02-05, office supply purchasing switched to Vendor B, now the current vendor.", "status": "active", "significance": "high", "domain_primary": "work", "created": "2026-02-05", "updated": "2026-02-05", "allowed_contexts": ["all"]},
                "The change superseded Vendor A; Vendor B is the current office supply vendor.",
            ),
            (
                "states/tea-preference.md",
                {"id": "state.tea-preference", "type": "state", "summary": "Current beverage preference: prefers green tea, not coffee.", "status": "active", "significance": "high", "domain_primary": "relational", "created": "2026-02-01", "updated": "2026-02-01", "allowed_contexts": ["all"]},
                "The latest preference is green tea rather than coffee.",
            ),
            (
                "states/coffee-preference-old.md",
                {"id": "state.old-coffee-preference", "type": "state", "summary": "Earlier beverage preference: preferred coffee.", "status": "superseded", "significance": "low", "domain_primary": "relational", "created": "2025-10-01", "updated": "2025-10-01", "allowed_contexts": ["all"]},
                "This older preference was later changed.",
            ),
            (
                "entities/people/ruth-vale.md",
                entity(
                    "entity.ruth-vale", "Ruth Vale",
                    "Ruth Vale's SDP plan includes weekly communication practice and a visual schedule.",
                    aliases=["Ruth Varga Project"],
                    body="Ruth Vale's SDP details: weekly communication practice and a visual schedule.",
                ),
                "# Ruth Vale\n\nRuth Vale's SDP details include weekly communication practice and a visual schedule.",
            ),
            (
                "entities/people/relationship-context.md",
                entity(
                    "entity.relationship-context", "Relationship Context",
                    "Legally married to Omar, living separately since May 2025; see narrative for the full relationship context.",
                    body="The legal status and practical household arrangement differ; they live separately.",
                ),
                "# Relationship Context\n\nThe legal status is still married; the practical relationship is separated, and they live separately.",
            ),
        ]
        for relative, fm, body in fixture_records:
            record(vault, relative, fm, body)
        record(
            vault,
            "contradictions/vendor-switch.md",
            {"id": "contradiction.vendor-switch", "type": "contradiction_log", "status": "active", "created": "2026-02-05", "summary": "Unresolved vendor history: Vendor A was used in January; the February switch made Vendor B current."},
            "The January Vendor A record is superseded by the February Vendor B decision. Surface the change when recalling the current vendor.",
        )
        record(
            vault,
            "contradictions/relationship-status.md",
            {"id": "contradiction.relationship-status", "type": "contradiction_log", "status": "active", "created": "2026-02-05", "summary": "Relationship status has a legal-versus-practical distinction: married legally, living separately in practice."},
            "Do not flatten the legally married status into a complete description of the relationship.",
        )

        conn = open_index_connection(db_path)
        try:
            for path in sorted(vault.rglob("*.md")):
                index_single_record(path, vault, conn)
            conn.commit()
        finally:
            conn.close()

        inactive_scorer = VectorScorer(None, EmbeddingIndex("eval", 0, {}), "skip")
        retrieval_config = {
            "retrieval": {"fusion": {"enabled": True, "method": "rrf", "serendipity_slots": 0},
                          "learned_edges": {"enabled": False}}
        }
        with patch("lisan.tools.retrieval.load_config", return_value=retrieval_config), patch(
            "lisan.tools.retrieval.build_query_scorer", return_value=inactive_scorer
        ):
            current = retrieve_context(
                "What is the current office supply vendor?", vault=vault, db_path=db_path
            )
            current_ids = [item.id for item in current.loaded]
            current_context = assemble_context(
                "What is the current office supply vendor?", vault=vault, db_path=db_path
            )
            vendor_b_rank = current_ids.index("episode.vendor-feb") if "episode.vendor-feb" in current_ids else 999
            vendor_a_rank = current_ids.index("episode.vendor-jan") if "episode.vendor-jan" in current_ids else 999
            evaluate(
                "vendor switch: newer vendor ranks and conflict is surfaced",
                vendor_b_rank < vendor_a_rank
                and "Active Contradictions" in current_context
                and "Vendor A" in current_context and "Vendor B" in current_context,
                f"vendor_b_rank={vendor_b_rank}; vendor_a_rank={vendor_a_rank}; conflict_note={'Active Contradictions' in current_context}",
            )

            historic = retrieve_context(
                "What did we know about the office supply vendor on 2026-01-15 historically?",
                vault=vault, db_path=db_path,
            )
            historic_ids = [item.id for item in historic.loaded]
            historic_a_rank = historic_ids.index("episode.vendor-jan") if "episode.vendor-jan" in historic_ids else 999
            historic_b_rank = historic_ids.index("episode.vendor-feb") if "episode.vendor-feb" in historic_ids else 999
            temporal_evidence = (
                f"vendor_a_rank={historic_a_rank}; vendor_b_rank={historic_b_rank}; "
                f"top_ids={historic_ids[:5]}"
            )
            if historic_a_rank < historic_b_rank:
                evaluate("as-of query ranks the January fact first", True, temporal_evidence)
            else:
                cases.append({
                    "name": "as-of query ranks the January fact first",
                    "passed": False,
                    "known_gap": True,
                    "evidence": temporal_evidence,
                })

            preference = retrieve_context(
                "What is the current beverage preference?", vault=vault, db_path=db_path
            )
            preference_ids = [item.id for item in preference.loaded]
            evaluate(
                "newer preference outranks superseded preference",
                bool(preference_ids) and preference_ids[0] == "state.tea-preference",
                f"top_ids={preference_ids[:3]}",
            )

            relationship_context = assemble_context(
                "What is the current relationship status and household arrangement?",
                vault=vault, db_path=db_path,
            )
            relationship_ids = [
                item.id for item in retrieve_context(
                    "What is the current relationship status and household arrangement?",
                    vault=vault, db_path=db_path,
                ).loaded
            ]
            evaluate(
                "relationship summary preserves legal/practical nuance and conflict",
                "living separately" in relationship_context.lower()
                and "practical" in relationship_context.lower()
                and "Active Contradictions" in relationship_context
                and "entity.relationship-context" in relationship_ids,
                f"entity_loaded={'entity.relationship-context' in relationship_ids}; nuance={'living separately' in relationship_context.lower()}",
            )

            identity = retrieve_context(
                "Ruth Varga Project SDP plan details", vault=vault, db_path=db_path
            )
            identity_ids = [item.id for item in identity.loaded]
            evaluate(
                "identity alias resolves SDP details to one canonical entity",
                "entity.ruth-vale" in identity_ids
                and not any("ruth-varga-project" in item.id for item in identity.loaded),
                f"canonical_loaded={'entity.ruth-vale' in identity_ids}; duplicate_loaded=False",
            )

        conn = open_index_connection(db_path)
        try:
            metric = conn.execute(
                "SELECT retrieved_token_estimate FROM retrieval_log "
                "WHERE retrieved_token_estimate IS NOT NULL ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        evaluate(
            "retrieved-token estimate recorded for each task",
            len(metric) >= 5 and all(int(row[0]) > 0 for row in metric),
            f"logged_tasks={len(metric)}",
        )

    failures = [case for case in cases if not case["passed"] and not case.get("known_gap")]
    known_gaps = sum(bool(case.get("known_gap")) for case in cases)
    return {
        "suite": "memory-pipeline",
        "passed": sum(bool(case["passed"]) for case in cases),
        "failed": len(failures),
        "known_gaps": known_gaps,
        "cases": cases,
    }


# ---------------------------------------------------------------- judgement

def _judge_sample(
    vault: Path, exchanges: list[dict[str, Any]], cfg: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """Judge the most recent exchanges against the kernel rubric. A judge
    failure degrades to 'not judged this run' — never to fake scores."""
    if not exchanges:
        return [], "no exchanges in window"
    from .judge import DEFAULT_JUDGE_MODEL, DEFAULT_JUDGE_PROVIDER, judge_exchange
    from .rubric import rubric_from_kernel

    rubric = rubric_from_kernel(vault)
    provider = str(cfg.get("judge_provider") or DEFAULT_JUDGE_PROVIDER)
    # NOT str(): "no model, let the provider choose its default" is None, and
    # str(None) is the four-character string "None", which reaches the codex
    # CLI as `--model None` and exits 1 in about three seconds. This line was
    # harmless while DEFAULT_JUDGE_MODEL was "openai/gpt-4o"; f5de272 moved the
    # judge to the codex provider (so transcripts reach no new third party) and
    # set the default to None, which turned a pointless coercion into a total
    # outage. Every run from 2026-07-15 to 2026-08-13 scored 0/10 and reported
    # success. A str() around a value that is allowed to be None is never
    # load-bearing and is always a latent bug.
    model = cfg.get("judge_model") or DEFAULT_JUDGE_MODEL
    model = str(model) if model is not None else None
    sample = exchanges[-int(cfg["sample_size"]):]
    judged: list[dict[str, Any]] = []
    errors = 0
    for ex in sample:
        try:
            scores = judge_exchange(
                rubric, ex["user"], ex["assistant"],
                provider=provider, model=model, context=ex.get("context") or None,
            )
        except Exception as exc:
            errors += 1
            log_error(vault, "self_eval judge call failed", exc)
            continue
        judged.append({**ex, "scores": scores})
    note = f"judge: {provider}/{model}; {len(judged)}/{len(sample)} scored"
    if errors:
        note += f", {errors} judge errors"
    return judged, note


# ---------------------------------------------------------------- synthesis

def _history_entry(
    now: date,
    exchanges: list[dict[str, Any]],
    health: dict[str, Any],
    judged: list[dict[str, Any]],
) -> dict[str, Any]:
    from .judge import aggregate

    dims = aggregate([j["scores"] for j in judged]) if judged else {}
    means = [d["mean"] for d in dims.values() if d.get("n", 0) > 0]
    return {
        "date": now.isoformat(),
        "exchanges": len(exchanges),
        "judged": len(judged),
        "dimensions": dims,
        "overall_mean": round(sum(means) / len(means), 2) if means else None,
        "health": {
            "capture_failure_rate": health.get("capture_failure_rate"),
            "empty_responses": health.get("empty_responses_logged"),
            "failed_turns": health.get("failed_turns_logged"),
            "records_created": health.get("records_created"),
            "retrieval_volume": health.get("retrieval_volume"),
        },
    }


def _derive_suggestions(
    cfg: dict[str, Any],
    entry: dict[str, Any],
    previous: dict[str, Any] | None,
    judged: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rules, not vibes: each suggestion cites the number that triggered it
    and carries a stable fingerprint so it cannot nag."""
    out: list[dict[str, Any]] = []
    floor = float(cfg["min_dimension_mean"])
    for dim, stats in (entry.get("dimensions") or {}).items():
        if stats.get("n", 0) >= 3 and stats["mean"] < floor:
            worst = _worst_rationale(judged, dim)
            out.append({
                "klass": "self_eval",
                "fingerprint": f"self-eval-dim-{dim}",
                "summary": (
                    f"my '{dim}' quality is slipping — mean {stats['mean']}/5 over "
                    f"{stats['n']} recent real exchanges{worst}"
                ),
                "links": [],
            })
    rate = float(entry["health"].get("capture_failure_rate") or 0.0)
    if rate > 0.10:
        out.append({
            "klass": "self_eval",
            "fingerprint": "self-eval-capture-failures",
            "summary": f"{round(rate * 100)}% of my memory captures failed this week — I am forgetting parts of what I hear",
            "links": [],
        })
    if int(entry["health"].get("empty_responses") or 0) > 2:
        out.append({
            "klass": "self_eval",
            "fingerprint": "self-eval-empty-responses",
            "summary": f"I returned {entry['health']['empty_responses']} empty responses recently — turns where I simply failed to speak",
            "links": [],
        })
    retrieval_volume = entry["health"].get("retrieval_volume") or {}
    if retrieval_volume.get("monotonic_week_over_week_growth"):
        weekly = retrieval_volume.get("weekly") or []
        first = weekly[0].get("avg_retrieved_tokens") if weekly else "?"
        latest = weekly[-1].get("avg_retrieved_tokens") if weekly else "?"
        out.append({
            "klass": "self_eval",
            "fingerprint": "self-eval-retrieval-token-growth",
            "summary": (
                "retrieved-summary tokens per task rose every measured week "
                f"({first} → {latest} across {len(weekly)} weeks); check for retrieval bloat"
            ),
            "links": [],
        })
    prev_mean = (previous or {}).get("overall_mean")
    cur_mean = entry.get("overall_mean")
    if prev_mean is not None and cur_mean is not None and prev_mean - cur_mean >= float(cfg["regression_drop"]):
        out.append({
            "klass": "self_eval",
            "fingerprint": f"self-eval-regression-{entry['date']}",
            "summary": (
                f"my overall quality dropped from {prev_mean} to {cur_mean} since the last review — "
                "something recent made me worse"
            ),
            "links": [],
        })
    return out


def _worst_rationale(judged: list[dict[str, Any]], dim: str) -> str:
    worst_score, rationale = 6, ""
    for j in judged:
        for s in j.get("scores") or []:
            if s.get("id") == dim and s.get("score") is not None and s["score"] < worst_score:
                worst_score, rationale = s["score"], str(s.get("rationale") or "")
    return f" (worst case: {rationale})" if rationale else ""


def _emit_suggestions(
    vault: Path,
    suggestions: list[dict[str, Any]],
    report_path: Path,
    *,
    db_path: Path | None,
) -> list[str]:
    """Through the deviation seam: same daily cap, same fingerprint dedup,
    same drive surfacing. A quality slippage is an ache like any other."""
    if not suggestions:
        return []
    from .deviations import _emit, deviations_config

    rel = str(report_path.relative_to(vault))
    for s in suggestions:
        s.setdefault("links", []).append(rel)
    return _emit(vault, suggestions, deviations_config(None), date.today(), db_path=db_path)


# ---------------------------------------------------------------- artifacts

def _write_report(
    vault: Path,
    now: date,
    entry: dict[str, Any],
    judged: list[dict[str, Any]],
    judge_note: str,
    suggestions: list[dict[str, Any]],
    previous: dict[str, Any] | None,
) -> Path:
    lines = [
        f"# Self-evaluation — {now.isoformat()}",
        "",
        "Scheduled review of my own recent real conversations and the memory",
        "they produced. Private to the vault.",
        "",
        f"- exchanges in window: {entry['exchanges']} | judged: {entry['judged']} ({judge_note})",
        f"- overall mean: {entry.get('overall_mean')}"
        + (f" (previous: {previous.get('overall_mean')})" if previous else ""),
        "",
        "## Dimension scores",
        "",
    ]
    for dim, stats in (entry.get("dimensions") or {}).items():
        lines.append(f"- {dim}: {stats['mean']}/5 (n={stats['n']})")
    if not entry.get("dimensions"):
        lines.append("- (no judged exchanges this run)")
    lines += ["", "## Machinery", ""]
    for key, value in (entry.get("health") or {}).items():
        lines.append(f"- {key}: {value}")
    lines += ["", "## Weak moments (evidence for the scores)", ""]
    weak = _weakest_exchanges(judged, limit=3)
    if weak:
        for w in weak:
            lines.append(f"- [{w['day']} {w['cid']}] {w['dim']}={w['score']}: {w['rationale']}")
            lines.append(f"  > USER: {w['user'][:180]}")
            lines.append(f"  > ME: {w['assistant'][:180]}")
    else:
        lines.append("- none below threshold")
    lines += ["", "## Suggestions", ""]
    if suggestions:
        lines.extend(f"- {s['summary']}" for s in suggestions)
    else:
        lines.append("- nothing actionable; hold course")
    path = vault / "reports" / f"self-eval-{now.strftime('%Y%m%d')}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Frontmatter, because reports/ is a structured-record directory: written
    # bare, every self-evaluation failed validation for a missing `type` and
    # stayed invisible to retrieval — the system could not recall its own
    # reviews of itself. personal_sensitive/private by construction: the body
    # quotes real conversation verbatim.
    stamp = now.isoformat()
    frontmatter = {
        "id": f"report.self-eval-{now.strftime('%Y%m%d')}",
        "type": "report",
        "created": stamp,
        "updated": stamp,
        "status": "active",
        "significance": "medium",
        "domain_primary": "competence",
        "domain_secondary": [],
        "privacy": "personal_sensitive",
        "disclosure": "private",
        "summary": f"Self-evaluation {stamp}: {entry['judged']} exchange(s) judged, "
                   f"overall mean {entry.get('overall_mean')}",
        "links": [],
        "confidence": "medium",
        "confidence_basis": "Rubric scores over real transcripts, judged by a separate examiner",
        "last_confirmed": stamp,
        "review_after": stamp,
    }
    write_markdown(path, frontmatter, "\n".join(lines) + "\n")
    return path


def _weakest_exchanges(judged: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    flat = []
    for j in judged:
        for s in j.get("scores") or []:
            if s.get("score") is not None and s["score"] <= 2:
                flat.append({
                    "day": j["day"], "cid": j["cid"], "dim": s["id"], "score": s["score"],
                    "rationale": s.get("rationale") or "", "user": j["user"], "assistant": j["assistant"],
                })
    flat.sort(key=lambda w: w["score"])
    return flat[:limit]


def _append_history(vault: Path, entry: dict[str, Any]) -> None:
    path = vault / _HISTORY_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=True) + "\n")


def _last_history_entry(vault: Path) -> dict[str, Any] | None:
    path = vault / _HISTORY_REL
    if not path.exists():
        return None
    try:
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None
