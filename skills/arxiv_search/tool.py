from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

NS = {"a": "http://www.w3.org/2005/Atom"}
OPENSEARCH_TOTAL = "{http://a9.com/-/spec/opensearch/1.1/}totalResults"
API = "https://export.arxiv.org/api/query"
SORT_MAP = {"relevance": "relevance", "date": "submittedDate", "updated": "lastUpdatedDate"}


def build_query_params(args: dict[str, Any]) -> dict[str, str] | str:
    query = str(args.get("query") or "").strip()
    author = str(args.get("author") or "").strip()
    category = str(args.get("category") or "").strip()
    ids = str(args.get("ids") or "").strip()
    max_results = min(max(int(args.get("max_results") or 5), 1), 20)
    sort = str(args.get("sort") or "relevance")

    params: dict[str, str] = {"max_results": str(max_results)}
    if ids:
        params["id_list"] = ids
    else:
        parts = []
        if query:
            parts.append(f"all:{query}")
        if author:
            parts.append(f'au:"{author}"')
        if category:
            parts.append(f"cat:{category}")
        if not parts:
            return "Error: provide a query, author, category, or ids"
        params["search_query"] = " AND ".join(parts)
        params["sortBy"] = SORT_MAP.get(sort, "relevance")
        params["sortOrder"] = "descending"
    return params


def parse_feed(data: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(data)
    papers: list[dict[str, Any]] = []
    for entry in root.findall("a:entry", NS):
        def text(tag: str) -> str:
            node = entry.find(tag, NS)
            return (node.text or "").strip() if node is not None else ""

        raw_id = text("a:id")
        full_id = raw_id.split("/abs/")[-1] if "/abs/" in raw_id else raw_id
        base_id = full_id.rsplit("v", 1)[0] if "v" in full_id.split("/")[-1] else full_id
        summary = " ".join(text("a:summary").split())
        papers.append(
            {
                "id": full_id,
                "title": " ".join(text("a:title").split()),
                "authors": [
                    (a.find("a:name", NS).text or "").strip()
                    for a in entry.findall("a:author", NS)
                    if a.find("a:name", NS) is not None
                ],
                "published": text("a:published")[:10],
                "updated": text("a:updated")[:10],
                "categories": [c.get("term", "") for c in entry.findall("a:category", NS)],
                "abstract": summary[:600] + ("…" if len(summary) > 600 else ""),
                "url": f"https://arxiv.org/abs/{base_id}",
                "pdf": f"https://arxiv.org/pdf/{base_id}",
            }
        )
    return papers


def run(args: dict[str, Any], vault: Path, config: dict[str, Any]) -> str:
    params = build_query_params(args)
    if isinstance(params, str):
        return params
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "LisanAgent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        papers = parse_feed(data)
    except urllib.error.URLError as exc:
        return f"Error: could not reach arXiv: {getattr(exc, 'reason', exc)}"
    except ET.ParseError as exc:
        return f"Error: arXiv returned unparseable XML: {exc}"
    if not papers:
        return "No papers found."
    return json.dumps(papers, indent=2, ensure_ascii=False)
