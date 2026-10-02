"""A monotonic validation gate: legacy errors may disappear, never grow."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .validator import ValidationReport, validate_vault


def error_fingerprints(report: ValidationReport, vault: Path) -> Counter[str]:
    fingerprints: Counter[str] = Counter()
    for issue in report.issues:
        if issue.severity != "error":
            continue
        try:
            location = issue.path.relative_to(vault).as_posix()
        except ValueError:
            location = str(issue.path)
        fingerprints[f"{location}\t{issue.message}"] += 1
    return fingerprints


def write_baseline(path: Path, report: ValidationReport, vault: Path) -> None:
    counts = error_fingerprints(report, vault)
    payload = {
        "format": 1,
        "vault": str(vault.resolve()),
        "errors": [
            {"fingerprint": fingerprint, "count": count}
            for fingerprint, count in sorted(counts.items())
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def read_baseline(path: Path) -> Counter[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != 1 or not isinstance(payload.get("errors"), list):
        raise ValueError(f"Unsupported validation baseline: {path}")
    return Counter(
        {
            str(item["fingerprint"]): int(item["count"])
            for item in payload["errors"]
        }
    )


@dataclass(slots=True)
class GateResult:
    report: ValidationReport
    new_errors: Counter[str]
    resolved_errors: Counter[str]

    @property
    def ok(self) -> bool:
        return not self.new_errors


def compare_to_baseline(vault: Path, baseline: Path, *, db_path: Path | None = None) -> GateResult:
    report = validate_vault(vault, db_path=db_path)
    current = error_fingerprints(report, vault)
    expected = read_baseline(baseline)
    return GateResult(
        report=report,
        new_errors=current - expected,
        resolved_errors=expected - current,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail if a vault has validation errors absent from its baseline")
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args(argv)
    report = validate_vault(args.vault, db_path=args.db_path)
    if args.write_baseline:
        write_baseline(args.baseline, report, args.vault)
        print(f"Wrote {sum(error_fingerprints(report, args.vault).values())} error fingerprints to {args.baseline}")
        return 0
    result = compare_to_baseline(args.vault, args.baseline, db_path=args.db_path)
    if result.ok:
        print(
            f"Validation gate passed: 0 new errors; "
            f"{sum(result.resolved_errors.values())} legacy errors resolved."
        )
        return 0
    print(f"Validation gate failed: {sum(result.new_errors.values())} new error(s)")
    for fingerprint, count in sorted(result.new_errors.items()):
        print(f"- {count}x {fingerprint}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
