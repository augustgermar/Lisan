from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lisan.tools.skills_cli import bundled_skills_root


def _load(module_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def google():
    return _load(
        bundled_skills_root() / "_google_common" / "lisan_google.py", "t_lisan_google"
    )


@pytest.fixture(scope="module")
def gsetup(google):
    # setup.py imports lisan_google from its own directory via sys.path.
    sys.path.insert(0, str(bundled_skills_root() / "_google_common"))
    try:
        return _load(bundled_skills_root() / "_google_common" / "setup.py", "t_gsetup")
    finally:
        sys.path.pop(0)


# ── Google OAuth pieces ──────────────────────────────────────────────────────


def test_extract_code_accepts_full_redirect_url(gsetup) -> None:
    url = "http://localhost:1/?code=4/0AbCdEf&scope=https://mail.google"
    assert gsetup.extract_code(url) == "4/0AbCdEf"
    assert gsetup.extract_code("  '4/0AbCdEf'  ") == "4/0AbCdEf"
    assert gsetup.extract_code("code=4/0AbCdEf&scope=x") == "4/0AbCdEf"


def test_extract_code_rejects_url_without_code(gsetup) -> None:
    # setup.py imports lisan_google under its own module instance, so the
    # exception class must come from gsetup's namespace, not the google fixture.
    with pytest.raises(gsetup.GoogleAuthError):
        gsetup.extract_code("http://localhost:1/?error=access_denied")


def test_token_expiry_logic(google) -> None:
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert google.token_expired({"expiry": past}) is True
    assert google.token_expired({"expiry": future}) is False
    assert google.token_expired({}) is True
    assert google.token_expired({"expiry": "garbage"}) is True
    # Z-suffixed timestamps (what setup.py writes) parse too
    z_future = future.replace("+00:00", "Z")
    assert google.token_expired({"expiry": z_future}) is False


def test_credentials_dir_resolution(google, monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("LISAN_GOOGLE_CREDENTIALS_DIR", raising=False)
    config = {"skills": {"google": {"credentials_dir": str(tmp_path / "cfg")}}}
    assert google.credentials_dir(config) == tmp_path / "cfg"
    monkeypatch.setenv("LISAN_GOOGLE_CREDENTIALS_DIR", str(tmp_path / "env"))
    assert google.credentials_dir(config) == tmp_path / "env"
    monkeypatch.delenv("LISAN_GOOGLE_CREDENTIALS_DIR")
    assert "credentials/google" in str(google.credentials_dir(None)).replace("\\", "/")


def test_save_token_is_owner_only(google, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LISAN_GOOGLE_CREDENTIALS_DIR", str(tmp_path))
    path = google.save_token({"token": "x"})
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_gmail_body_extraction_walks_nested_multiparts(google) -> None:
    import base64

    def b64(text: str) -> str:
        return base64.urlsafe_b64encode(text.encode()).decode()

    msg = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "multipart/related",
                    "body": {},
                    "parts": [
                        {"mimeType": "text/html", "body": {"data": b64("<b>html</b>")}},
                        {"mimeType": "text/plain", "body": {"data": b64("plain wins")}},
                    ],
                }
            ],
        }
    }
    assert google.extract_body(msg) == "plain wins"
    html_only = {
        "payload": {
            "parts": [{"mimeType": "text/html", "body": {"data": b64("<b>html</b>")}}]
        }
    }
    assert google.extract_body(html_only) == "<b>html</b>"


# ── imsg helpers ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def imsg():
    return _load(
        bundled_skills_root() / "_imsg_common" / "lisan_imsg.py", "t_lisan_imsg"
    )


def test_parse_ndjson_skips_garbage(imsg) -> None:
    raw = '{"id": 1}\nnot json\n\n{"id": 2}\n[1,2]\n'
    assert [r["id"] for r in imsg.parse_ndjson(raw)] == [1, 2]


def test_trim_message_keeps_essentials(imsg) -> None:
    msg = {
        "id": 5,
        "chat_id": 9,
        "chat_identifier": "+1555",
        "chat_name": "",
        "sender": "+1555",
        "is_from_me": False,
        "text": "hello",
        "created_at": "2026-01-01T00:00:00Z",
        "reply_to_text": "original",
        "attachments": [{"a": 1}, {"b": 2}],
        "guid": "SHOULD-NOT-SURVIVE",
    }
    trimmed = imsg.trim_message(msg)
    assert trimmed["chat"] == "+1555"
    assert trimmed["in_reply_to"] == "original"
    assert trimmed["attachments"] == 2
    assert "guid" not in trimmed


def test_run_imsg_reports_missing_binary(imsg, monkeypatch) -> None:
    monkeypatch.delenv("LISAN_IMSG_BIN", raising=False)
    monkeypatch.setattr(imsg.shutil, "which", lambda _: None)
    ok, message = imsg.run_imsg(["chats"], {})
    assert ok is False
    assert "brew install" in message


# ── maps / polymarket arg building ───────────────────────────────────────────


@pytest.fixture(scope="module")
def maps_tool():
    return _load(bundled_skills_root() / "maps" / "tool.py", "t_maps_tool")


def test_maps_arg_building(maps_tool) -> None:
    assert maps_tool.build_cli_args({"action": "search", "query": "Chico CA"}) == [
        "search",
        "Chico CA",
    ]
    nearby = maps_tool.build_cli_args(
        {"action": "nearby", "near": "Chico CA", "category": "cafe", "radius": 800}
    )
    assert nearby[:3] == ["nearby", "--near", "Chico CA"]
    assert "--radius" in nearby and "800" in nearby
    distance = maps_tool.build_cli_args(
        {"action": "distance", "origin": "Chico", "destination": "Sacramento"}
    )
    assert distance == ["distance", "Chico", "--to", "Sacramento", "--mode", "driving"]
    assert "Error" in maps_tool.build_cli_args({"action": "nearby"})
    assert "Error" in maps_tool.build_cli_args({"action": "bogus"})
    assert "Error" in maps_tool.build_cli_args({"action": "reverse"})


@pytest.fixture(scope="module")
def polymarket_tool():
    return _load(bundled_skills_root() / "polymarket" / "tool.py", "t_poly_tool")


def test_polymarket_arg_building(polymarket_tool) -> None:
    assert polymarket_tool.build_cli_args({"action": "search", "query": "btc"}) == [
        "search",
        "btc",
    ]
    assert polymarket_tool.build_cli_args({"action": "trending", "limit": 99}) == [
        "trending",
        "--limit",
        "25",
    ]
    assert "Error" in polymarket_tool.build_cli_args({"action": "market"})
    assert "Error" in polymarket_tool.build_cli_args({"action": "hack"})


# ── arxiv ────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def arxiv_tool():
    return _load(bundled_skills_root() / "arxiv_search" / "tool.py", "t_arxiv_tool")


def test_arxiv_query_building(arxiv_tool) -> None:
    params = arxiv_tool.build_query_params(
        {"query": "GRPO", "category": "cs.AI", "max_results": 3}
    )
    assert params["search_query"] == "all:GRPO AND cat:cs.AI"
    assert params["max_results"] == "3"
    by_id = arxiv_tool.build_query_params({"ids": "2402.03300"})
    assert by_id["id_list"] == "2402.03300"
    assert "Error" in arxiv_tool.build_query_params({})


def test_arxiv_feed_parsing(arxiv_tool) -> None:
    feed = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>http://arxiv.org/abs/2402.03300v3</id>
        <title>Test  Paper
        Title</title>
        <published>2024-02-05T00:00:00Z</published>
        <updated>2024-04-27T00:00:00Z</updated>
        <summary>An abstract.</summary>
        <author><name>Ada Lovelace</name></author>
        <category term="cs.AI"/>
      </entry>
    </feed>"""
    papers = arxiv_tool.parse_feed(feed.encode())
    assert len(papers) == 1
    paper = papers[0]
    assert paper["title"] == "Test Paper Title"
    assert paper["id"] == "2402.03300v3"
    assert paper["url"] == "https://arxiv.org/abs/2402.03300"
    assert paper["authors"] == ["Ada Lovelace"]


# ── youtube ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def yt_tool():
    return _load(
        bundled_skills_root() / "youtube_transcript" / "tool.py", "t_yt_tool"
    )


def test_youtube_video_id_extraction(yt_tool) -> None:
    vid = "dQw4w9WgXcQ"
    for url in (
        f"https://www.youtube.com/watch?v={vid}",
        f"https://youtu.be/{vid}?t=10",
        f"https://www.youtube.com/shorts/{vid}",
        f"https://www.youtube.com/embed/{vid}",
        vid,
    ):
        assert yt_tool.extract_video_id(url) == vid


def test_youtube_rejects_junk_input(yt_tool, tmp_path) -> None:
    result = yt_tool.run({"url": "https://example.com/nothing"}, tmp_path, {})
    assert result.startswith("Error:")


def test_youtube_timestamp_format(yt_tool) -> None:
    assert yt_tool.format_timestamp(65) == "1:05"
    assert yt_tool.format_timestamp(3700) == "1:01:40"


def test_youtube_caption_parsing_handles_xml_and_json3(yt_tool) -> None:
    xml = (
        '<?xml version="1.0" encoding="utf-8" ?><timedtext format="3">'
        '<body><p t="1360" d="2000">hello <s>world</s></p>'
        '<p t="4000" d="1000"></p>'
        '<p t="5000" d="1500">again</p></body></timedtext>'
    )
    segments = yt_tool._parse_caption_payload(xml)
    assert [s["text"] for s in segments] == ["hello world", "again"]
    assert segments[0]["start"] == pytest.approx(1.36)

    json3 = json.dumps(
        {
            "events": [
                {"tStartMs": 0, "dDurationMs": 2500, "segs": [{"utf8": "hi\nthere"}]},
                {"tStartMs": 9000, "segs": []},
            ]
        }
    )
    segments = yt_tool._parse_caption_payload(json3)
    assert segments == [{"text": "hi there", "start": 0.0, "duration": 2.5}]

    assert isinstance(yt_tool._parse_caption_payload(""), str)
    assert isinstance(yt_tool._parse_caption_payload("<broken"), str)
    assert isinstance(yt_tool._parse_caption_payload("not json or xml"), str)


# ── obsidian ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def obsidian_vault(tmp_path, monkeypatch):
    vault = tmp_path / "ObsidianVault"
    (vault / "Projects").mkdir(parents=True)
    (vault / ".obsidian").mkdir()
    (vault / "Projects" / "Lisan.md").write_text(
        "# Lisan\nA local-first memory vault.\n", encoding="utf-8"
    )
    (vault / "Groceries.md").write_text("- milk\n- eggs\n", encoding="utf-8")
    (vault / ".obsidian" / "hidden.md").write_text("memory\n", encoding="utf-8")
    monkeypatch.setenv("LISAN_OBSIDIAN_VAULT", str(vault))
    return vault


@pytest.fixture(scope="module")
def obs_search():
    return _load(
        bundled_skills_root() / "obsidian_search" / "tool.py", "t_obs_search"
    )


@pytest.fixture(scope="module")
def obs_read():
    return _load(bundled_skills_root() / "obsidian_read" / "tool.py", "t_obs_read")


def test_obsidian_search_finds_content_and_titles(
    obs_search, obsidian_vault, tmp_path
) -> None:
    result = json.loads(obs_search.run({"query": "memory"}, tmp_path, {}))
    paths = [r["path"] for r in result["results"]]
    assert paths == ["Projects/Lisan.md"]  # .obsidian dir excluded
    by_title = json.loads(obs_search.run({"query": "groceries"}, tmp_path, {}))
    assert by_title["results"][0]["path"] == "Groceries.md"


def test_obsidian_read_and_suffix_convenience(obs_read, obsidian_vault, tmp_path) -> None:
    result = obs_read.run({"path": "Projects/Lisan"}, tmp_path, {})
    assert "local-first memory vault" in result


def test_obsidian_read_blocks_traversal(obs_read, obsidian_vault, tmp_path) -> None:
    outside = obsidian_vault.parent / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    result = obs_read.run({"path": "../secret.md"}, tmp_path, {})
    assert result.startswith("Error:")
    assert "secret" not in result.split("Error:")[1] or "does not exist" in result


def test_obsidian_missing_vault_is_readable_error(obs_search, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LISAN_OBSIDIAN_VAULT", str(tmp_path / "nope"))
    result = obs_search.run({"query": "x"}, tmp_path, {})
    assert result.startswith("Error:")
    assert "vault" in result.lower()
