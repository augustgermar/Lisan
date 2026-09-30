from __future__ import annotations

import json
import unittest
from pathlib import Path

from lisan.tools.chat import _ProgressRenderer
from lisan.tools.tracing import (
    record_inline_step,
    record_jobs_queued,
    record_llm_call,
    record_retrieval_result,
    record_tool_use,
    reset_progress_listener,
    set_progress_listener,
    start_turn_trace,
    reset_current_turn_trace,
)


class ProgressListenerTests(unittest.TestCase):
    def setUp(self):
        self.events: list[dict] = []
        self.token = set_progress_listener(self.events.append)

    def tearDown(self):
        reset_progress_listener(self.token)

    def test_record_functions_emit_events(self):
        record_inline_step("memory_pipeline.assembler")
        record_retrieval_result(12, 4)
        record_llm_call(
            call_name="interlocutor", provider="codex", model="gpt-5.4",
            prompt="p", output="o", elapsed_ms=8300, success=True,
        )
        record_tool_use("read_file", {"path": "/tmp/x"})
        record_jobs_queued(2)
        kinds = [event["kind"] for event in self.events]
        self.assertEqual(kinds, ["step", "retrieval", "llm_call", "tool", "jobs_queued"])

    def test_zero_jobs_queued_is_silent(self):
        record_jobs_queued(0)
        self.assertEqual(self.events, [])

    def test_listener_errors_never_propagate(self):
        token = set_progress_listener(lambda event: 1 / 0)
        try:
            record_inline_step("anything")  # must not raise
        finally:
            reset_progress_listener(token)

    def test_tool_use_lands_in_trace_steps(self):
        trace, trace_token = start_turn_trace("t1", "hi", "memory", False)
        try:
            record_tool_use("search_memory", {"query": "cats"})
            self.assertIn("tool.search_memory", trace.inline_steps)
        finally:
            reset_current_turn_trace(trace_token)


class ProgressRendererTests(unittest.TestCase):
    def setUp(self):
        self.lines: list[str] = []
        self.renderer = _ProgressRenderer("Vee", out=self.lines.append)

    def test_known_step_is_humanized_and_header_prints_once(self):
        self.renderer({"kind": "step", "step": "memory_pipeline.assembler"})
        self.renderer({"kind": "step", "step": "memory_pipeline.writer"})
        joined = "\n".join(self.lines)
        self.assertEqual(joined.count("thinking…"), 1)
        self.assertIn("recalling related memories", joined)
        self.assertIn("extracting what to remember", joined)

    def test_noise_steps_are_skipped(self):
        self.renderer({"kind": "step", "step": "memory_pipeline.start"})
        self.renderer({"kind": "step", "step": "memory_pipeline.transcript"})
        self.assertEqual(self.lines, [])

    def test_unknown_step_passes_through(self):
        self.renderer({"kind": "step", "step": "custom.stage"})
        self.assertIn("custom.stage", "\n".join(self.lines))

    def test_llm_call_lines(self):
        self.renderer({
            "kind": "llm_call", "call_name": "writer", "provider": "codex",
            "model": "gpt-5.4", "elapsed_ms": 21400, "success": True,
        })
        self.renderer({
            "kind": "llm_call", "call_name": "writer", "provider": "codex",
            "model": "", "elapsed_ms": 500, "success": False, "error_type": "ProviderError",
        })
        joined = "\n".join(self.lines)
        self.assertIn("writer done (codex/gpt-5.4, 21.4s)", joined)
        self.assertIn("✗ writer failed (codex, 0.5s, ProviderError)", joined)

    def test_retrieval_and_tool_and_jobs_lines(self):
        self.renderer({"kind": "retrieval", "records": 12, "graph": 4})
        self.renderer({"kind": "retrieval", "records": 3, "graph": 0})
        self.renderer({"kind": "tool", "tool": "read_file", "args_preview": '{"path": "/tmp/x"}'})
        self.renderer({"kind": "jobs_queued", "count": 2})
        joined = "\n".join(self.lines)
        self.assertIn("recalled 12 record(s) (+4 via graph)", joined)
        self.assertIn("recalled 3 record(s)", joined)
        self.assertIn('tool: read_file {"path": "/tmp/x"}', joined)
        self.assertIn("queued 2 background job(s)", joined)

    def _feed(self, renderer, *events):
        from lisan.providers.codex import _record_codex_json_progress

        token = set_progress_listener(renderer)
        try:
            for event in events:
                _record_codex_json_progress(
                    json.dumps(event), agent="codex", model="gpt-5",
                    working_directory=Path("/tmp"), elapsed_ms=1800,
                )
        finally:
            reset_progress_listener(token)

    def test_codex_command_is_unwrapped_and_status_shown(self):
        self._feed(
            self.renderer,
            {"type": "item.started", "item": {"id": "1", "type": "command_execution", "command": "/bin/bash -lc 'ls /etc'"}},
            {"type": "item.completed", "item": {"id": "1", "type": "command_execution", "command": "/bin/bash -lc 'ls /etc'",
                                                "exit_code": 0, "aggregated_output": "a\nb\nc\n"}},
        )
        joined = "\n".join(self.lines)
        self.assertIn("│ $ ls /etc", joined)
        self.assertIn("✓ exit 0 · 3 line(s)", joined)
        self.assertNotIn("│   c", joined)  # compact mode hides successful output

    def test_codex_verbose_shows_output_tail(self):
        lines: list[str] = []
        renderer = _ProgressRenderer("Vee", out=lines.append, verbose=True)
        output = "\n".join(f"line {n}" for n in range(20)) + "\nPython 3.13.5"
        self._feed(renderer, {"type": "item.completed", "item": {
            "type": "command_execution", "command": "bash -lc pytest", "exit_code": 0, "aggregated_output": output}})
        joined = "\n".join(lines)
        self.assertIn("Python 3.13.5", joined)
        self.assertIn("… 13 earlier line(s)", joined)
        self.assertNotIn("line 12\n", joined)

    def test_codex_failed_command_shows_tail_in_compact_mode(self):
        self._feed(self.renderer, {"type": "item.completed", "item": {
            "type": "command_execution", "command": "false", "exit_code": 2, "aggregated_output": "ok\nboom: no such file"}})
        joined = "\n".join(self.lines)
        self.assertIn("boom: no such file", joined)
        self.assertIn("✗ exit 2", joined)

    def test_codex_secrets_are_masked_not_hidden(self):
        from lisan.providers.codex import _display_command, _mask_secrets

        self.assertEqual(_display_command("curl --header password=secret"), "curl --header password=****")
        self.assertEqual(_display_command("mysql --password hunter2 db"), "mysql --password **** db")
        self.assertEqual(_mask_secrets("Authorization: Bearer abcdef123456"), "Authorization: Bearer ****")
        self.assertEqual(_mask_secrets("export API_KEY=abc123"), "export API_KEY=****")
        self.assertEqual(_mask_secrets('{"token": "abc123"}'), '{"token": ****}')
        self.assertEqual(_mask_secrets("https://bob:pw@host/x"), "https://bob:****@host/x")
        self.assertEqual(_mask_secrets("jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.sig_part"), "jwt ****")
        self.assertIn("****", _mask_secrets("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"))
        self.assertNotIn("MIIE", _mask_secrets("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"))
        # Mentions of sensitive words, dotted names, and versions stay visible.
        for text in ("rg -n token lisan/", "lisan.providers.codex", "Python 3.13.5", "10.0.0.1"):
            self.assertEqual(_mask_secrets(text), text)

    def test_codex_reasoning_and_messages(self):
        reasoning = {"type": "item.completed", "item": {"type": "reasoning", "text": "**Inspecting config**\n\nI will read the file next."}}
        message = {"type": "item.completed", "item": {"type": "agent_message", "text": "Done.\nChanged two files."}}
        structured = {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}}
        self._feed(self.renderer, reasoning, message, structured)
        compact = "\n".join(self.lines)
        self.assertIn("✻ Inspecting config", compact)
        self.assertNotIn("read the file next", compact)
        self.assertIn("» Done. …", compact)
        self.assertIn("replied with structured output", compact)

        lines: list[str] = []
        self._feed(_ProgressRenderer("Vee", out=lines.append, verbose=True), reasoning, message)
        verbose = "\n".join(lines)
        self.assertIn("I will read the file next.", verbose)
        self.assertIn("Changed two files.", verbose)

    def test_codex_turn_lifecycle_and_errors(self):
        self._feed(
            self.renderer,
            {"type": "item.completed", "item": {"type": "file_change", "status": "completed",
                                                "changes": [{"path": "/tmp/a.py", "kind": "update"}]}},
            {"type": "error", "message": "Your workspace is out of credits."},
            {"type": "turn.failed", "error": {"message": "out of credits"}},
            {"type": "turn.completed", "usage": {"input_tokens": 12345, "output_tokens": 800}},
        )
        self.renderer({"kind": "codex", "event": "heartbeat", "elapsed_ms": 45000, "detail": "still running: pytest"})
        joined = "\n".join(self.lines)
        self.assertIn("✎ update /tmp/a.py", joined)
        self.assertIn("✗ Your workspace is out of credits.", joined)
        self.assertIn("✗ Codex turn failed: out of credits", joined)
        self.assertIn("done · 1.8s · 12.3k in / 800 out", joined)
        self.assertIn("… still working · 45s · still running: pytest", joined)


if __name__ == "__main__":
    unittest.main()
