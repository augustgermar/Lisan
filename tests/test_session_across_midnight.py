from __future__ import annotations

from datetime import datetime

from lisan.tools.memory_pipeline import _conversation_turn_count
from lisan.tools.narrative_state import conversation_session
from lisan.tools.retrieval_graph import _is_fresh_conversation, _recent_conversation_turns
from lisan.tools.transcripts import append_transcript


def _say(vault, when, speaker, text, conv="c"):
    append_transcript(vault=vault, conversation_id=conv, speaker=speaker, text=text,
                      timestamp=datetime.fromisoformat(when))


def test_a_conversation_continues_across_midnight(tmp_path):
    _say(tmp_path, "2026-10-28T23:50", "USER", "still up?")
    _say(tmp_path, "2026-10-28T23:51", "LISAN", "yes")
    _say(tmp_path, "2026-10-29T00:05", "USER", "and now?")
    assert [t["text"] for t in _recent_conversation_turns(tmp_path, "c")] == ["still up?", "yes", "and now?"]
    assert not _is_fresh_conversation(tmp_path, "c")
    assert _conversation_turn_count(tmp_path, "c") == 2


def test_a_long_silence_starts_a_new_sitting(tmp_path):
    _say(tmp_path, "2026-10-28T09:00", "USER", "morning")
    _say(tmp_path, "2026-10-28T09:01", "LISAN", "hi")
    _say(tmp_path, "2026-10-28T20:00", "USER", "evening")
    assert [t["text"] for t in conversation_session(tmp_path, "c")] == ["evening"]
    assert _is_fresh_conversation(tmp_path, "c")
