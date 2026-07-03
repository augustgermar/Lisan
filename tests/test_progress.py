from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
