"""A function-local import of a module-level name is a landmine, not a nicety.

`lisan complete` and `lisan provider check` both died with
``UnboundLocalError: cannot access local variable 'load_config'`` while three
*other* branches of the same ``main()`` re-imported ``load_config`` locally.
Python binds a name for the whole function body, so a redundant import at the
bottom of ``main()`` unbinds every read above it. Nothing in the suite noticed,
because no test dispatched the two affected commands.

Two tests, deliberately: the behavioral pair proves those commands dispatch,
and the structural gate makes the whole defect class impossible — the next
redundant import anywhere in ``main()`` fails here rather than in the owner's
terminal, whichever branch it lands in.
"""

from __future__ import annotations

import ast
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.cli import main
from lisan.tools.provider_diagnostics import ProviderDiagnosticResult


CLI_SOURCE = Path(__file__).resolve().parents[1] / "lisan" / "cli.py"


def _module_level_import_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _local_import_names(func: ast.FunctionDef) -> dict[str, int]:
    """Names bound by an import *inside* ``func``, mapped to their line."""
    found: dict[str, int] = {}
    for node in ast.walk(func):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if node is func:
                continue
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                found.setdefault(bound, node.lineno)
    return found


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in {CLI_SOURCE}")


class CliShadowedImportGateTests(unittest.TestCase):
    """The structural gate: no local import may shadow a module-level import."""

    def test_main_has_no_local_import_shadowing_a_module_level_name(self) -> None:
        tree = ast.parse(CLI_SOURCE.read_text(encoding="utf-8"))
        module_names = _module_level_import_names(tree)
        local_names = _local_import_names(_find_function(tree, "main"))

        shadowed = sorted(
            (name, line) for name, line in local_names.items() if name in module_names
        )
        self.assertEqual(
            shadowed,
            [],
            "main() re-imports name(s) already imported at module level; every read "
            "of them earlier in main() will raise UnboundLocalError at runtime: "
            + ", ".join(f"{name} (cli.py:{line})" for name, line in shadowed),
        )


class CliAffectedCommandsDispatchTests(unittest.TestCase):
    """The behavioral pair: the two commands the bug actually killed."""

    def test_provider_check_reaches_its_handler(self) -> None:
        diagnostic = ProviderDiagnosticResult(provider="rotato", model="stub-model", status="ok")
        stdout = io.StringIO()
        with patch("lisan.cli.diagnose_provider", return_value=diagnostic) as diagnose:
            with contextlib.redirect_stdout(stdout):
                code = main(["provider", "check"])

        self.assertEqual(code, 0)
        diagnose.assert_called_once()
        self.assertIn("rotato", stdout.getvalue())

    def test_complete_reaches_its_handler(self) -> None:
        class _Response:
            text = "stub completion"

        llm = type("_LLM", (), {"complete": lambda self, *a, **k: _Response()})()
        stdout = io.StringIO()
        with patch("lisan.cli.LisanLLM", return_value=llm):
            with contextlib.redirect_stdout(stdout):
                code = main(["complete", "hello"])

        self.assertEqual(code, 0)
        self.assertIn("stub completion", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
