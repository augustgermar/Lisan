from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config, save_default_config
from .paths import ensure_repo_layout, repo_root, vault_root
from .providers.base import LisanLLM, ProviderError
from .tools.assembler import assemble_context
from .tools.health_report import generate_health_report
from .tools.heuristic_gate import score_text
from .tools.manifest_gen import generate_manifests
from .tools.rebuild_index import rebuild_index
from .tools.validator import format_report, validate_vault


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lisan")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create the default vault layout and config")

    validate = subparsers.add_parser("validate", help="Validate vault files")
    validate.add_argument("--vault", type=Path, default=vault_root())

    manifest = subparsers.add_parser("manifest", help="Generate derived manifests")
    manifest.add_argument("--vault", type=Path, default=vault_root())
    manifest.add_argument("--no-write", action="store_true")

    rebuild = subparsers.add_parser("rebuild-index", help="Rebuild SQLite and embeddings")
    rebuild.add_argument("--vault", type=Path, default=vault_root())

    health = subparsers.add_parser("health", help="Write a health report")
    health.add_argument("--vault", type=Path, default=vault_root())

    stale = subparsers.add_parser("stale", help="List stale state files")
    stale.add_argument("--vault", type=Path, default=vault_root())

    loops = subparsers.add_parser("loops", help="List active open loops")
    loops.add_argument("--vault", type=Path, default=vault_root())

    assemble = subparsers.add_parser("assemble", help="Assemble context for a query")
    assemble.add_argument("query", nargs="+")
    assemble.add_argument("--arena", default=None)
    assemble.add_argument("--vault", type=Path, default=vault_root())

    heuristic = subparsers.add_parser("heuristic", help="Score text for memory processing")
    heuristic.add_argument("text", nargs="+")

    call = subparsers.add_parser("complete", help="Send a prompt through the provider router")
    call.add_argument("prompt", nargs="+")
    call.add_argument("--agent", default="writer")
    call.add_argument("--significance", default="medium")
    call.add_argument("--provider", default=None)
    call.add_argument("--model", default=None)

    sync = subparsers.add_parser("sync", help="Generate manifests, validate, rebuild index")
    sync.add_argument("--vault", type=Path, default=vault_root())

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        ensure_repo_layout()
        if not (repo_root() / "config.yaml").exists():
            save_default_config()
        print("Lisan workspace initialized.")
        return 0

    if args.command == "validate":
        report = validate_vault(args.vault)
        print(format_report(report))
        return 0 if report.ok else 1

    if args.command == "manifest":
        manifests = generate_manifests(args.vault, write=not args.no_write)
        print("\n".join(sorted(manifests)))
        return 0

    if args.command == "rebuild-index":
        counts = rebuild_index(args.vault)
        print(json.dumps(counts, indent=2))
        return 0

    if args.command == "health":
        report = generate_health_report(args.vault)
        out = args.vault / "reports" / "health-latest.md"
        out.write_text(report, encoding="utf-8")
        print(out)
        return 0

    if args.command == "stale":
        from datetime import date

        from .frontmatter import load_markdown

        for path in sorted((args.vault / "state").glob("*.md")):
            try:
                doc = load_markdown(path)
            except Exception:
                continue
            ttl = int(doc.frontmatter.get("ttl_days", 0) or 0)
            updated = doc.frontmatter.get("updated")
            if not ttl or not updated:
                continue
            try:
                age = (date.today() - date.fromisoformat(str(updated))).days
            except ValueError:
                continue
            if age > ttl:
                print(f"{path.name}: age={age} ttl={ttl}")
        return 0

    if args.command == "loops":
        for path in sorted((args.vault / "open_loops").glob("*.md")):
            print(path.name)
        return 0

    if args.command == "assemble":
        query = " ".join(args.query)
        print(assemble_context(query, arena=args.arena, vault=args.vault))
        return 0

    if args.command == "heuristic":
        result = score_text(" ".join(args.text))
        print(json.dumps(result.as_dict(), indent=2))
        return 0

    if args.command == "complete":
        config = load_config()
        llm = LisanLLM(config=config)
        try:
            response = llm.complete(
                " ".join(args.prompt),
                agent=args.agent,
                significance=args.significance,
                provider=args.provider,
                model=args.model,
            )
        except ProviderError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(response.text)
        return 0

    if args.command == "sync":
        generate_manifests(args.vault, write=True)
        report = validate_vault(args.vault)
        counts = rebuild_index(args.vault)
        print(format_report(report))
        print(json.dumps(counts, indent=2))
        return 0 if report.ok else 1

    parser.print_help()
    return 1

