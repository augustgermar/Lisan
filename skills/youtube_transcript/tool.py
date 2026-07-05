from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# YouTube's innertube player endpoint. The ANDROID client context returns
# caption tracks without needing an API key or consent cookies. This is an
# unofficial surface and can change; fetch_captions degrades to a readable
# error (and the youtube-transcript-api package is used first if installed).
PLAYER_URL = "https://www.youtube.com/youtubei/v1/player"
CLIENT_CONTEXT = {
    "context": {
        "client": {
            "clientName": "ANDROID",
            "clientVersion": "20.10.38",
            "androidSdkVersion": 30,
        }
    }
}
USER_AGENT = "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip"


def extract_video_id(url_or_id: str) -> str:
    value = url_or_id.strip()
    for pattern in (
        r"(?:v=|youtu\.be/|shorts/|embed/|live/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ):
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return value


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _segments_via_library(video_id: str, language: str) -> list[dict[str, Any]] | None:
    """Prefer youtube-transcript-api when it happens to be installed."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None
    try:
        api = YouTubeTranscriptApi()
        result = api.fetch(video_id, languages=[language, "en"])
        return [
            {"text": seg.text, "start": seg.start, "duration": seg.duration}
            for seg in result
        ]
    except Exception:
        return None  # fall through to the stdlib path


def _segments_via_innertube(video_id: str, language: str) -> list[dict[str, Any]] | str:
    body = dict(CLIENT_CONTEXT)
    body["videoId"] = video_id
    req = urllib.request.Request(
        f"{PLAYER_URL}?prettyPrint=false",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            player = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        return f"could not reach YouTube: {getattr(exc, 'reason', exc)}"

    status = player.get("playabilityStatus", {})
    if status.get("status") not in (None, "OK"):
        reason = status.get("reason") or status.get("status")
        return f"video is not playable ({reason})"

    tracks = (
        player.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks", [])
    )
    if not tracks:
        return "this video has no captions/transcript"

    def score(track: dict[str, Any]) -> tuple[int, int]:
        lang_match = 1 if str(track.get("languageCode", "")).startswith(language) else 0
        manual = 0 if track.get("kind") == "asr" else 1  # prefer human captions
        return (lang_match, manual)

    track = max(tracks, key=score)
    base_url = str(track.get("baseUrl") or "")
    if not base_url:
        return "caption track has no URL"
    req = urllib.request.Request(base_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        return f"could not fetch caption track: {getattr(exc, 'reason', exc)}"

    segments = _parse_caption_payload(raw)
    if isinstance(segments, str):
        return segments
    if not segments:
        return "caption track was empty"
    return segments


def _parse_caption_payload(raw: str) -> list[dict[str, Any]] | str:
    """The timedtext endpoint answers in XML (`<timedtext format="3">`) for
    the ANDROID client; other clients get json3. Handle both."""
    raw = raw.strip()
    if not raw:
        return "caption track was empty"
    segments: list[dict[str, Any]] = []
    if raw.startswith("<"):
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            return f"could not parse caption XML: {exc}"
        # timedtext format=3 uses <p t="ms" d="ms">; legacy uses
        # <text start="s" dur="s">. Nested <s> word nodes need itertext().
        for node in root.iter():
            if node.tag not in ("p", "text"):
                continue
            text = " ".join("".join(node.itertext()).split())
            if not text:
                continue
            if node.tag == "p":
                start = float(node.get("t", 0)) / 1000.0
                duration = float(node.get("d", 0)) / 1000.0
            else:
                start = float(node.get("start", 0))
                duration = float(node.get("dur", 0))
            segments.append({"text": text, "start": start, "duration": duration})
        return segments
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"unrecognized caption payload: {exc}"
    for event in payload.get("events", []):
        text = "".join(seg.get("utf8", "") for seg in event.get("segs", []) or [])
        text = text.replace("\n", " ").strip()
        if not text:
            continue
        segments.append(
            {
                "text": text,
                "start": float(event.get("tStartMs", 0)) / 1000.0,
                "duration": float(event.get("dDurationMs", 0)) / 1000.0,
            }
        )
    return segments


def run(args: dict[str, Any], vault: Path, config: dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "Error: url is required"
    video_id = extract_video_id(url)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
        return f"Error: could not extract a video id from {url!r}"
    language = str(args.get("language") or "en").strip() or "en"
    max_chars = max(int(args.get("max_chars") or 12000), 500)
    want_timestamps = bool(args.get("timestamps"))

    segments = _segments_via_library(video_id, language)
    if segments is None:
        segments = _segments_via_innertube(video_id, language)
    if isinstance(segments, str):
        return f"Error: {segments}"

    if want_timestamps:
        text = "\n".join(
            f"{format_timestamp(seg['start'])} {seg['text']}" for seg in segments
        )
    else:
        text = " ".join(seg["text"] for seg in segments)
    duration = format_timestamp(segments[-1]["start"] + segments[-1]["duration"])
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n… [transcript truncated at {max_chars} characters]"
    return (
        f"Video {video_id} — {len(segments)} caption segments, ~{duration} long\n\n{text}"
    )
