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
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from .db import connect as _db_connect

from ..utils import today_iso
from .log import get_logger, log_error

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "days": 7,               # review window
    "sample_size": 10,       # judged exchanges per run
    "judge_provider": None,  # default: judge.py's own (openrouter/gpt-4o)
    "judge_model": None,
    "min_dimension_mean": 3.5,   # below this (with evidence) → suggestion
    "regression_drop": 0.5,      # overall-mean drop vs last run → suggestion
    "interval_hours": 7 * 24,    # scheduled weekly
}

_HISTORY_REL = "reports/self-eval-history.jsonl"
_TRIVIAL_WORDS = 4  # user turns at or below this are acks, not evidence


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
            from ..frontmatter import load_markdown

            for p in folder.rglob("*.md"):
                try:
                    if str(load_markdown(p).frontmatter.get("created") or "") >= cutoff:
                        created[kind] += 1
                except Exception:
                    continue
    health["records_created"] = created
    return health


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
    model = str(cfg.get("judge_model") or DEFAULT_JUDGE_MODEL)
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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
