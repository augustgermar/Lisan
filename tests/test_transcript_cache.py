from __future__ import annotations

from lisan.tools import narrative_state
from lisan.tools.transcripts import append_transcript


def test_history_reads_each_unchanged_file_once(tmp_path, monkeypatch):
    append_transcript(vault=tmp_path, conversation_id="c1", speaker="USER", text="hello there")
    reads = []
    real = narrative_state.Path.read_text

    def counting(self, *a, **k):
        reads.append(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(narrative_state.Path, "read_text", counting)
    first = narrative_state.conversation_history(tmp_path, "c1")
    second = narrative_state.conversation_history(tmp_path, "c1")
    assert first == second and first[-1]["text"] == "hello there"
    assert len(reads) <= 1


def test_an_appended_turn_is_seen_immediately(tmp_path):
    append_transcript(vault=tmp_path, conversation_id="c2", speaker="USER", text="one")
    narrative_state.conversation_history(tmp_path, "c2")
    append_transcript(vault=tmp_path, conversation_id="c2", speaker="LISAN", text="two")
    texts = [t["text"] for t in narrative_state.conversation_history(tmp_path, "c2")]
    assert texts[-2:] == ["one", "two"]


def test_callers_cannot_mutate_the_cache(tmp_path):
    append_transcript(vault=tmp_path, conversation_id="c3", speaker="USER", text="keep")
    narrative_state.conversation_history(tmp_path, "c3")[-1]["text"] = "mutated"
    assert narrative_state.conversation_history(tmp_path, "c3")[-1]["text"] == "keep"
