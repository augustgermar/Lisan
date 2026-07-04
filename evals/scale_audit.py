"""Audit a scaled vault for the failure modes that only appear at volume.

Run after a scale load. Reports:
- fragmentation: exact-duplicate names, cross-kind duplicates, name-subset
  near-duplicates (the "Wisteria Hollows" vs "...community garden" failure)
- junk: stub/empty entity narratives, suspicious single-token entities
- retrieval quality + latency: for a sample of real cast members, does
  assemble_context surface records actually about them, and how fast
- kind distribution and record-type counts
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

VAULT = Path("/Users/august/.lisan/vault")
DB = Path("/Users/august/.lisan/repo/lisan.sqlite")


def _entities() -> list[dict]:
    from lisan.frontmatter import load_markdown

    out = []
    for p in (VAULT / "entities").rglob("*.md"):
        try:
            doc = load_markdown(p)
            fm = doc.frontmatter
            body = re.sub(r"^#\s+.*$", "", doc.body, count=1, flags=re.M).strip()
            out.append({
                "path": p, "stem": p.stem,
                "name": str(fm.get("canonical_name") or "").strip(),
                "kind": str(fm.get("kind") or fm.get("subtype") or "").strip(),
                "words": len(body.split()),
                "summary": str(fm.get("summary") or ""),
            })
        except Exception:
            continue
    return out


def fragmentation(ents: list[dict]) -> dict:
    by_name_lower = defaultdict(list)
    for e in ents:
        if e["name"]:
            by_name_lower[e["name"].lower()].append(e)
    exact = {k: [e["kind"] for e in v] for k, v in by_name_lower.items() if len(v) > 1}
    cross_kind = {k: kinds for k, kinds in exact.items() if len(set(kinds)) > 1}

    names = sorted({e["name"] for e in ents if e["name"]})
    lows = [n.lower() for n in names]
    near = []
    for i, a in enumerate(lows):
        at = set(a.split())
        for b in lows[i + 1:]:
            bt = set(b.split())
            if a != b and (at < bt or bt < at) and (at & bt):
                near.append((a, b))
    return {
        "exact_duplicate_names": exact,
        "cross_kind_duplicates": cross_kind,
        "near_duplicate_pairs": near,
        "totals": {"exact": len(exact), "cross_kind": len(cross_kind), "near": len(near)},
    }


def junk(ents: list[dict]) -> dict:
    stubs = [e["name"] for e in ents if e["words"] <= 6]
    single_token = [e["name"] for e in ents
                    if e["name"] and len(e["name"].split()) == 1 and e["kind"] == "person"]
    articleish = [e["name"] for e in ents if e["name"].lower() in {"the", "a", "an", "my", "his", "her"}]
    return {
        "stub_narrative_count": len(stubs),
        "single_token_persons": single_token[:40],
        "article_entities": articleish,
    }


def retrieval_probe(ents: list[dict], sample: int = 8) -> list[dict]:
    from lisan.tools.retrieval import assemble_context

    people = [e for e in ents if e["kind"] == "person" and e["words"] > 8 and " " in e["name"]]
    rng = random.Random(3)
    picks = rng.sample(people, min(sample, len(people))) if people else []
    results = []
    for e in picks:
        first = e["name"].split()[0]
        started = time.time()
        try:
            ctx = assemble_context(f"tell me about {e['name']}", vault=VAULT, db_path=DB)
        except Exception as exc:
            results.append({"name": e["name"], "error": str(exc)})
            continue
        elapsed = round(time.time() - started, 2)
        # is the target's own record among the retrieved?
        hit = e["stem"] in ctx or e["name"] in ctx
        # how many OTHER cast names bleed into the top context (noise)
        results.append({
            "name": e["name"], "latency_s": elapsed,
            "self_present": hit, "context_chars": len(ctx),
        })
    return results


def main():
    ents = _entities()
    kinds = Counter(e["kind"] for e in ents)
    total_records = sum(1 for p in VAULT.rglob("*.md")
                        if not {"logs", "transcripts", "archive"} & set(p.parts))

    frag = fragmentation(ents)
    report = {
        "entity_count": len(ents),
        "total_md_records": total_records,
        "kind_distribution": dict(kinds.most_common()),
        "fragmentation": frag,
        "junk": junk(ents),
        "retrieval_probes": retrieval_probe(ents),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
