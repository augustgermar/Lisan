from __future__ import annotations

import json
from typing import Any

from .base import PromptAgent
from ..tools.epistemic import review_claim_against_evidence, review_pattern_against_records


class SkepticAgent(PromptAgent):
    name = "skeptic"
    prompt_file = "skeptic_v1"
    output_schema_name = "skeptic_output"

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        review = self._deterministic_review(user_input, significance=significance, **kwargs)
        issues = review["issues"]
        payload = {
            "approved": review["approved"],
            "approved_for_dreamer": review.get("approved_for_dreamer", False),
            "issues": issues,
            "risk": review["risk"],
            "recommended_action": review["recommended_action"],
            "priority_questions": review["priority_questions"],
            "observed_facts": review["observed_facts"],
            "interpretations": review["interpretations"],
            "alternative_hypotheses": review["alternative_hypotheses"],
            "evidence_needed": review["evidence_needed"],
            "claim_updates": review["claim_updates"],
            "confidence_adjustments": review["confidence_adjustments"],
            "reasoning_errors": review["reasoning_errors"],
            "reviewed_record_id": review["reviewed_record_id"],
            "reviewed_record_type": review["reviewed_record_type"],
            "pattern_status": review.get("status"),
            "counterexample_search": review.get("counterexample_search"),
            "summary": review["summary"],
        }
        return json.dumps(payload, indent=2, ensure_ascii=True)

    def _deterministic_review(self, user_input: str, significance: str = "medium", **kwargs: Any) -> dict[str, Any]:
        payload = self._try_parse_json(user_input)
        reviewed_record_id = ""
        reviewed_record_type = ""
        claim_text = user_input
        evidence_items: list[dict[str, Any]] = []

        if isinstance(payload, dict):
            writer = payload.get("writer") if isinstance(payload.get("writer"), dict) else payload
            if isinstance(writer, dict):
                if str(writer.get("frontmatter", {}).get("type") or payload.get("frontmatter", {}).get("type")) == "pattern":
                    pattern = writer.get("frontmatter", {}) if isinstance(writer.get("frontmatter"), dict) else {}
                    if not pattern:
                        pattern = payload.get("frontmatter", {}) if isinstance(payload.get("frontmatter"), dict) else {}
                    hypothesis = str(pattern.get("hypothesis") or writer.get("summary") or payload.get("summary") or "")
                    support = list(pattern.get("supporting_records") or [])
                    counterexamples = list(pattern.get("counterexamples") or [])
                    review = review_pattern_against_records(hypothesis, support, counterexamples)
                    approved = bool(review.get("approved", False))
                    approved_for_dreamer = bool(review.get("approved_for_dreamer", False))
                    confidence_adjustments = [{"target": rid, "delta": 0.05, "reason": "supporting pattern record"} for rid in review["supporting_records"]]
                    if review["counterexamples"] and not any("none" in str(item).lower() for item in review["counterexamples"]):
                        confidence_adjustments.append({"target": str(pattern.get("id") or ""), "delta": -0.1, "reason": "counterexample exists"})
                    issues = [] if approved else [{"type": "pattern_risk", "message": "Pattern hypothesis needs more longitudinal support before promotion."}]
                    for code in review["reasoning_errors"]:
                        issues.append({"type": code, "message": f"Potential reasoning error: {code}."})
                    if not bool(review.get("counterexample_search", {}).get("performed", False)):
                        issues.append({"type": "counterexample_search", "message": "Counterexample search was not recorded."})
                    return {
                        "approved": approved,
                        "approved_for_dreamer": approved_for_dreamer,
                        "issues": issues,
                        "risk": "low" if approved else ("medium" if review["confidence"] >= 0.35 else "high"),
                        "recommended_action": "approve" if approved else ("revise" if review["confidence"] >= 0.35 else "hold"),
                        "priority_questions": review["evidence_needed"][:5],
                        "observed_facts": [],
                        "interpretations": [hypothesis],
                        "alternative_hypotheses": review["alternative_explanations"],
                        "evidence_needed": review["evidence_needed"],
                        "claim_updates": [],
                        "confidence_adjustments": confidence_adjustments,
                        "reasoning_errors": review["reasoning_errors"],
                        "reviewed_record_id": str(pattern.get("id") or ""),
                        "reviewed_record_type": "pattern",
                        "pattern_status": review["status"],
                        "counterexample_search": review["counterexample_search"],
                        "summary": "Deterministic Skeptic review of pattern hypothesis",
                    }
                reviewed_record_id = str(
                    writer.get("frontmatter", {}).get("id")
                    or payload.get("frontmatter", {}).get("id")
                    or ""
                )
                reviewed_record_type = str(
                    writer.get("frontmatter", {}).get("type")
                    or payload.get("frontmatter", {}).get("type")
                    or "draft"
                )
                evidence_items = list(writer.get("evidence_to_create") or [])
                claims = list(writer.get("claims_to_create") or [])
                if claims:
                    first_claim = claims[0] if isinstance(claims[0], dict) else {}
                    claim_text = str(first_claim.get("claim_text") or first_claim.get("summary") or json.dumps(first_claim, ensure_ascii=True))
                else:
                    claim_text = str(writer.get("summary") or payload.get("summary") or json.dumps(writer, indent=2, ensure_ascii=True))
                if not evidence_items and isinstance(payload.get("evidence_to_create"), list):
                    evidence_items = list(payload.get("evidence_to_create") or [])

        review = review_claim_against_evidence(claim_text, evidence_items=evidence_items)
        issues: list[dict[str, str]] = []
        lowered = claim_text.lower()
        if "maybe" in lowered or "probably" in lowered:
            issues.append({"type": "uncertainty", "message": "The draft uses uncertainty without scoping it to evidence."})
        if "i think" in lowered or "i feel" in lowered:
            issues.append({"type": "interpretation", "message": "Separate report from interpretation."})
        if "placeholder" in lowered:
            issues.append({"type": "placeholder", "message": "Replace placeholder text with concrete evidence or leave it unresolved."})
        if any(token in lowered for token in ("legal", "medical", "financial", "custody", "fraud")):
            issues.append({"type": "high_risk", "message": "High-risk material needs careful verification and privacy review."})
        for code in review["reasoning_errors"]:
            issues.append({"type": code, "message": f"Potential reasoning error: {code}."})
        if not evidence_items:
            issues.append({"type": "missing_evidence", "message": "No external evidence was supplied, so the claim should remain tentative."})

        confidence = float(review["confidence"])
        if significance == "high":
            confidence = min(confidence, 0.75)
        approved = review["recommended_action"] == "approve" and not any(issue["type"] in {"high_risk", "missing_evidence"} for issue in issues)
        confidence_adjustments = []
        for cid in review["supporting_evidence"]:
            confidence_adjustments.append({"target": cid, "delta": 0.1, "reason": "supporting evidence"})
        for cid in review["contradicting_evidence"]:
            confidence_adjustments.append({"target": cid, "delta": -0.2, "reason": "contradicting evidence"})

        return {
            "approved": approved,
            "issues": issues,
            "risk": "high" if any(issue["type"] == "high_risk" for issue in issues) else ("medium" if issues or confidence < 0.65 else "low"),
            "recommended_action": review["recommended_action"],
            "priority_questions": review["evidence_needed"][:5],
            "observed_facts": review["observed_facts"],
            "interpretations": review["interpretations"],
            "alternative_hypotheses": review["alternative_hypotheses"],
            "evidence_needed": review["evidence_needed"],
            "claim_updates": [
                {
                    "claim_text": claim_text[:200],
                    "status": review["status"],
                    "confidence": round(confidence, 3),
                    "note": "Deterministic Skeptic fallback review.",
                }
            ],
            "confidence_adjustments": confidence_adjustments,
            "reasoning_errors": review["reasoning_errors"],
            "reviewed_record_id": reviewed_record_id,
            "reviewed_record_type": reviewed_record_type,
            "summary": "Deterministic Skeptic review",
        }

    def _try_parse_json(self, text: str) -> Any | None:
        try:
            return json.loads(text)
        except Exception:
            return None
