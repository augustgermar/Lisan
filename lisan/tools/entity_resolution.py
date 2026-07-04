"""Entity resolution helpers for the memory pipeline.

Everything about deciding *which* entity a mention refers to and how a new
one enters the vault: the person/organization structural gates, kind
subtyping, name/root/token matching, surname-conflict blocking, nickname and
disambiguator assignment, stub creation, and relationship edges.

The settled policy this module implements: aggressive split, never
speculative merge — when identity is uncertain, create a distinct entity
with a disambiguator; merging is a deliberate, high-confidence operation.
Structural gates block bad classifications but never promote them, and the
user's own language for a person always beats a system-coined handle.
"""
from __future__ import annotations

import re
from dataclasses import field
from pathlib import Path
from typing import Any
from ..frontmatter import load_markdown, write_markdown
from ..utils import slugify, today_iso
from ..utils import listify
from .deixis import has_unresolved_token
from .record_fanout import basis_or_default, index_created_record
from .record_factory import CreatedRecord, new_entity
from .reference_resolution import normalize_text, resolution_action


def _create_entity_stubs(
    vault: Path,
    writer: dict[str, Any],
    draft_rel: str,
    source_text: str,
    frequent_names: frozenset[str] | None = None,
    index_conn: Any | None = None,
) -> list[Path]:
    """Materialize entity stubs proposed by the writer.

    Returns paths for all entities processed (new or existing) so callers
    can enqueue story-rewrite jobs for entities that received new material.
    """
    from .primer_index import known_names as _primer_known_names
    from .primer_index import roster as _roster
    from .entity_kind import assign_kind
    from .stopwords import MONTH_STOPWORDS

    entities = writer.get("entities_to_create") or []
    if not entities:
        return []
    index = _load_entity_index(vault)
    primer_cast = _primer_known_names(vault)
    # Acceptance allowlist = primer cast + roster (known entities of ANY kind) +
    # frequently-mentioned names. Seeding the roster here also kills the
    # duplicate-invention problem at the source (spec §4 Layer 1).
    roster_names: set[str] = set()
    for _entry in _roster(vault):
        roster_names.add(_entry.name)
        roster_names.update(_entry.aliases)
    allowlist = primer_cast | (frequent_names or frozenset()) | frozenset(roster_names)
    seen_in_pass: set[str] = set()
    entities_touched_set: set[Path] = set()
    for entry in entities:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        summary = str(entry.get("summary") or "").strip()
        if not name:
            continue
        # {{principal}}/{{self}} are deixis ROLES, not entities. A writer
        # that emits the role token (or its bare slug) as an entity name must
        # never materialize a record for it — that way lies a bogus
        # entities/people/principal.md describing the user as a third party.
        # Drop any candidate whose name carries a role token or bare role slug.
        if has_unresolved_token(name) or name.strip().lower() in {"principal", "self", "user"}:
            continue
        # The principal's own name is the same role wearing its literal form:
        # the user is represented by the primer, never as a third-party entity.
        if name.strip().lower() in _principal_alias_set(vault):
            continue
        normalized = name.lower()
        if normalized in seen_in_pass:
            continue
        seen_in_pass.add(normalized)

        # Kind (P3): roster -> structural -> model's explicit choice -> thing.
        # NEVER defaults to person — that was the Atlas/Houston bug. The result
        # is stored as both `kind` and `subtype` (see new_entity) and scopes
        # dedup so a person "Atlas" and a project "Atlas" never merge.
        subtype = assign_kind(
            name,
            vault,
            model_kind=str(entry.get("kind") or entry.get("subtype") or "").strip(),
            summary=summary,
            source_text=source_text,
        )
        if not subtype:
            continue

        # Kind stickiness: if an entity with this exact name already exists,
        # inherit ITS kind. Otherwise a single mislabeled turn (a garden called
        # a "person" once) spawns a second-kind duplicate, whose shared tokens
        # then poison the index as "ambiguous" and fragment every later mention
        # into yet more duplicates. Kind is a property of the entity, decided
        # once — not re-litigated every time it comes up.
        existing_kind = _existing_entity_kind(index, name)
        if existing_kind and existing_kind != subtype:
            subtype = existing_kind

        pronoun_reject = {"she", "he", "they", "her", "him", "them", "it", "we", "i", "me", "us"}
        if normalized in pronoun_reject:
            continue
        if not _looks_like_entity(name, subtype, allowlist, source_text):
            # Month names are legitimate human names in this vault, but a
            # single-token month still needs some primer anchor before we
            # materialize it as a person stub. That keeps "August" usable when
            # the primer already establishes a live cast, while leaving other
            # bare single words rejected.
            if not (
                subtype == "person"
                and len(name.split()) == 1
                and name in MONTH_STOPWORDS
                and primer_cast
            ):
                continue

        raw_aliases = entry.get("aliases") or []
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        aliases = [str(alias).strip() for alias in raw_aliases if str(alias).strip()]
        user_handle = _scan_user_stated_handle(name, source_text, {alias.lower() for alias in aliases})
        if not user_handle:
            user_handle = next((alias for alias in aliases if alias.lower() != name.lower()), None)
        if user_handle and user_handle.lower() not in {alias.lower() for alias in aliases}:
            aliases.append(user_handle)

        existing = _match_existing_entity(vault, name, subtype, index, allowlist, source_text, summary=summary)
        if existing is not None:
            _append_entity_alias(existing, name)
            for alias in aliases:
                _append_entity_alias(existing, alias)
            if user_handle:
                _assign_entity_nickname(existing, user_handle)
            index_created_record(vault, CreatedRecord(path=existing, created=True), index_conn)
            entities_touched_set.add(existing)
            # Refresh the in-memory index so the next sibling in the same pass
            # also resolves to this canonical entity. Full-name key only —
            # surname tokens stay subject to the strict-token rule.
            index.setdefault(name.lower(),
                             {"path": existing, "kind": "full", "canonical": name})
            continue
        try:
            same_first_records = []
            nickname = None
            if subtype == "person":
                same_first_records = _same_first_name_records(vault, name, subtype, index)
                if same_first_records:
                    assigned_nicknames = _ensure_nicknames_for_collision(vault, same_first_records, source_text=source_text)
                    existing_handles = {
                        str(value).strip().lower()
                        for _, fm, _ in same_first_records
                        for value in _entity_identity_names(fm)
                    }
                    existing_handles.update(str(nickname).strip().lower() for nickname in assigned_nicknames.values())
                    nickname = _entity_nickname(
                        name,
                        summary=summary,
                        source_text=source_text,
                        existing_handles=existing_handles,
                    )
                if not nickname and user_handle:
                    nickname = user_handle
            created = new_entity(
                vault=vault,
                name=name,
                subtype=subtype,
                summary=summary or f"{name} mentioned in conversation.",
                confidence="low",
                confidence_basis=basis_or_default(entry, "Auto-extracted from conversation"),
                aliases=aliases,
                nickname=nickname,
                disambiguation=_entity_disambiguator_from_candidates(vault, name, subtype, index, summary, source_text),
            )
            index_created_record(vault, created, index_conn)
            entities_touched_set.add(created.path)
            if nickname:
                index.setdefault(nickname.lower(), {"path": created.path, "kind": "full", "canonical": name})
                for token in nickname.split():
                    tkey = token.lower()
                    existing_entry = index.get(tkey)
                    if existing_entry is None:
                        index[tkey] = {"path": created.path, "kind": "token", "canonical": name}
                    elif existing_entry.get("path") != created.path and existing_entry.get("kind") == "token":
                        existing_entry["kind"] = "ambiguous"
        except FileExistsError:
            continue
        # When seeding the index after a creation, register only
        # the full canonical name as a "full" hit and each token as "token".
        # If a second entity later tries to claim the same token, the index
        # marks it ambiguous and the strict matcher refuses cross-merges.
        index.setdefault(name.lower(),
                         {"path": created.path, "kind": "full", "canonical": name})
        for token in name.split():
            tkey = token.lower()
            existing_entry = index.get(tkey)
            if existing_entry is None:
                index[tkey] = {"path": created.path, "kind": "token", "canonical": name}
            elif existing_entry.get("path") != created.path and existing_entry.get("kind") == "token":
                existing_entry["kind"] = "ambiguous"
    return list(entities_touched_set)

def _create_relationship_edges(
    vault: Path,
    writer: dict[str, Any],
    db_path: Path | None = None,
    index_conn: Any | None = None,
) -> None:
    """Write entity-to-entity relationship edges from writer relationships_to_create."""
    import sqlite3 as _sqlite3
    from ..paths import sqlite_path
    relationships = list(writer.get("relationships_to_create") or [])
    if not relationships:
        return
    _db = db_path or sqlite_path()
    if index_conn is None and not _db.exists():
        return
    conn = index_conn or _sqlite3.connect(_db)
    try:
        for rel in relationships:
            if not isinstance(rel, dict):
                continue
            entity_a = str(rel.get("entity_a") or "").strip()
            entity_b = str(rel.get("entity_b") or "").strip()
            rel_type = str(rel.get("relationship_type") or "related_to").strip()
            if not entity_a or not entity_b:
                continue
            # Resolve names to entity IDs via the alias table.
            row_a = conn.execute(
                "SELECT entity_id FROM entity_aliases WHERE alias = ? LIMIT 1",
                (entity_a,),
            ).fetchone()
            row_b = conn.execute(
                "SELECT entity_id FROM entity_aliases WHERE alias = ? LIMIT 1",
                (entity_b,),
            ).fetchone()
            if not row_a or not row_b:
                continue
            id_a, id_b = row_a[0], row_b[0]
            existing = conn.execute(
                "SELECT 1 FROM links WHERE source_id=? AND target_id=? AND relationship_type=?",
                (id_a, id_b, rel_type),
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                    (id_a, id_b, rel_type),
                )
        if index_conn is None:
            conn.commit()
    except _sqlite3.Error:
        pass
    finally:
        if index_conn is None:
            conn.close()

def _normalize_entity_subtype(
    *,
    name: str,
    subtype: str,
    summary: str,
    source_text: str,
    primer_cast: frozenset[str],
) -> str | None:
    """Coerce writer-emitted subtype labels into one of the supported buckets."""
    allowed = frozenset({"person", "place", "thing", "project", "organization"})
    subtype = (subtype or "person").strip().lower()
    if subtype in allowed:
        if subtype == "person" and _looks_like_organization(name, summary, source_text, primer_cast):
            return "organization"
        return subtype
    if _looks_like_organization(name, summary, source_text, primer_cast):
        return "organization"
    return "person"

def _looks_like_organization(
    name: str,
    summary: str,
    source_text: str,
    primer_cast: frozenset[str],
) -> bool:
    """Heuristic for company / org-like entities that should not be typed as people."""
    combined = " ".join(part for part in (name, summary, source_text) if part).lower()
    org_markers = (
        "company",
        "employer",
        "organization",
        "organisation",
        "corporation",
        "corporate",
        "startup",
        "firm",
        "business",
        "vendor",
        "contractor",
        "department",
        "division",
        "group",
        "team",
        "studio",
        "labs",
        "systems",
        "solutions",
        "holdings",
        "ventures",
        "partners",
        "works at",
        "work for",
        "employed at",
    )
    if any(marker in combined for marker in org_markers):
        if len(name.split()) >= 2:
            return True
    lower_name = name.lower()
    if any(lower_name.endswith(suffix) for suffix in (" inc", " llc", " ltd", " corp", " company", " group", " labs", " systems", " studio", " ventures", " holdings", " partners")):
        return True
    return False

_PERSON_TITLES: frozenset[str] = frozenset({
    "dr", "mr", "mrs", "ms", "miss", "prof", "rev",
    "sgt", "cpl", "cpt", "capt", "lt", "col", "gen", "adm",
})

_NEVER_PERSON_TOKENS: frozenset[str] = frozenset({
    # Determiners, pronouns, conjunctions, prepositions
    "The", "A", "An", "It", "He", "She", "They", "We", "You",
    "His", "Her", "Their", "Our", "My", "Me", "Mine", "I",
    "No", "Yes", "Ok", "Okay", "So", "But", "And", "Or",
    "In", "On", "At", "Of", "For", "With", "From", "By", "Up", "Out",
    # Interrogatives and sentence-initial adverbs
    "What", "Why", "How", "When", "Where", "Who", "Whom", "Whose", "Which",
    "Then", "Now", "Today", "Tomorrow", "Yesterday",
    "Strategically", "Honestly", "Frankly", "Maybe", "Perhaps", "Probably",
    "Anyway", "Actually", "Eventually", "Finally", "Basically", "Apparently",
    "Hopefully", "Obviously", "Clearly", "Suddenly", "Recently",
    # Productivity tools / platforms (clearly never persons)
    "Slack", "Zoom", "GitHub", "Gmail", "Notion", "Jira", "Linear",
    "Google", "Microsoft", "Apple", "Discord", "Figma", "Trello",
    "Asana", "Confluence", "Outlook", "Teams", "Dropbox", "OneDrive",
    "Excel", "Word", "PowerPoint", "Sheets", "Docs", "Calendar",
    "YouTube", "Twitter", "Reddit", "Facebook", "Instagram",
    "ChatGPT", "Claude", "OpenAI", "Anthropic",
    # Dating / social apps — person sense is implausible even with social context
    "Bumble", "Hinge", "Tinder", "OkCupid",
})

_RELATIONSHIP_WORDS: frozenset[str] = frozenset({
    # Family
    "son", "daughter", "dad", "mom", "mother", "father", "brother", "sister",
    "husband", "wife", "partner", "uncle", "aunt", "grandpa", "grandma",
    "grandfather", "grandmother", "grandson", "granddaughter", "nephew",
    "niece", "cousin", "stepmom", "stepdad", "stepson", "stepdaughter",
    "fiance", "fiancee", "ex",
    # Casual / informal
    "buddy", "pal", "bro", "bestie", "homie", "mate",
    "date",  # "my date Friday" — date as a person, not a calendar day
    "guy", "dude", "crush",
    "barber", "stylist", "trainer", "instructor", "tutor",
    "landlord", "tenant",
    "babysitter", "nanny",
    "vet",  # "my vet Dr. March"
    # Professional / social
    "colleague", "coworker", "boss", "manager", "supervisor", "therapist",
    "lawyer", "attorney", "accountant", "mentor", "coach", "advisor",
    "friend", "neighbor", "roommate", "classmate", "teammate",
    "boyfriend", "girlfriend", "doctor",
})

_EVENT_PHRASE = re.compile(
    r"^(?:dinner|lunch|brunch|breakfast|drinks|coffee|happy\s+hour|meeting|check-?in|"
    r"appointment|session|practice|rehearsal|game|party|gathering|cookout|barbecue|bbq)"
    r"\s+"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"tonight|tomorrow|today|morning|afternoon|evening|night|weekly|daily)",
    re.IGNORECASE,
)

_EVENT_PHRASE_TIME_FIRST = re.compile(
    r"^(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"tonight|tomorrow|today|morning|afternoon|evening|night|weekly|daily)"
    r"\s+"
    r"(?:check-?in|meeting|appointment|session|practice|rehearsal|game|party|gathering|"
    r"cookout|barbecue|bbq|dinner|lunch|brunch|breakfast|drinks|coffee|happy\s+hour)",
    re.IGNORECASE,
)

_PLACE_PHRASE = re.compile(
    r"^(?:north|south|east|west|upper|lower|old|new|downtown|midtown|uptown|central|"
    r"lake|river|park|mount|fort|port|bay|st\.?|saint)\s+\w+",
    re.IGNORECASE,
)

def _has_person_role_context(name: str, source_text: str) -> bool:
    """Return True when the source text places *name* in a clear person context.

    Detects four pattern families:
      - possessive-role-name: "my/his/her/their [role] [Name]"
      - name-role appositive:  "[Name], my/his/her [role]" or "[Name] is my [role]"
      - name-as-agent: "[Name] texted/called/messaged/emailed me"
      - social-action:  "I/we went out with [Name]", "I met/saw [Name]",
                        "dinner/lunch/drinks/coffee with [Name]"
    """
    if not source_text:
        return False
    lowered = source_text.lower()
    name_lower = name.lower()
    n = re.escape(name_lower)
    role_group = "(?:" + "|".join(re.escape(w) for w in _RELATIONSHIP_WORDS) + ")"

    possessive = r"(?:my|his|her|their|our)\s+(?:\w+\s+)?" + role_group + r"\s+" + n
    appositive = n + r"(?:,?\s+(?:my|his|her|their)\s+" + role_group + r"|\s+is\s+(?:my|his|her|their)\s+" + role_group + r")"
    # "Her name is Barbara", "my name is Barbara", "this is Barbara"
    intro_named = (
        r"(?:my|his|her|their|our)\s+name\s+is\s+" + n
        + r"|(?:this|that)\s+is\s+" + n
        + r"|(?:met\s+(?:someone\s+)?named|someone\s+named)\s+" + n
        + r"|(?:named|called|known\s+as|goes\s+by)\s+" + n
    )
    # "[Name] texted/called/messaged me" — name acting as a communicating person
    name_acts = n + r"\s+(?:texted|called|messaged|emailed|reached\s+out|pinged|wrote|rang)"
    # "I/we texted/called/met/saw [Name]"
    i_act_name = r"(?:i|we)\s+(?:texted|called|messaged|emailed|met|saw|visited|asked|told)\s+(?:\w+\s+){0,3}" + n
    # "went (out) with [Name]", "dinner/lunch/drinks/coffee with [Name]"
    social_with = (
        r"(?:went\s+(?:out\s+)?with"
        r"|(?:had\s+)?(?:dinner|lunch|drinks|coffee|brunch)\s+with"
        r"|a\s+date\s+with"
        r"|talking\s+to|talked\s+to|speaking\s+with|spoke\s+with"
        r")\s+(?:\w+\s+){0,4}" + n
    )
    return bool(
        re.search(possessive, lowered)
        or re.search(appositive, lowered)
        or re.search(intro_named, lowered)
        or re.search(name_acts, lowered)
        or re.search(i_act_name, lowered)
        or re.search(social_with, lowered)
    )

def _principal_alias_set(vault: Path) -> frozenset[str]:
    try:
        from .primer_index import principal_aliases

        return frozenset(a.lower() for a in principal_aliases(vault))
    except Exception:
        return frozenset()


def _appears_only_inside_paths(name: str, source_text: str) -> bool:
    """True when every occurrence of *name* is embedded in filesystem-path
    tokens — "Mobile Documents" inside an iCloud path is a path segment, not
    somebody the user mentioned. Shape rule: an occurrence is path-embedded
    when every whitespace-delimited token it overlaps contains a slash
    (macOS paths carry single spaces, so a two-word segment straddles two
    slash-bearing tokens)."""
    if not source_text or not name:
        return False
    hits = [m.span() for m in re.finditer(re.escape(name), source_text)]
    if not hits:
        return False
    tokens = [(m.start(), m.end(), ("/" in m.group() or "\\" in m.group()))
              for m in re.finditer(r"\S+", source_text)]

    def in_path(h0: int, h1: int) -> bool:
        overlapping = [slashy for s, e, slashy in tokens if s < h1 and e > h0]
        return bool(overlapping) and all(overlapping)

    return all(in_path(h0, h1) for h0, h1 in hits)


def _looks_like_entity(name: str, subtype: str, primer_cast: frozenset[str], source_text: str = "") -> bool:
    """Validate that *name* is plausibly an entity of *subtype*.

    Rules (in priority order):
    1. Primer/roster-known names: always accepted (highest authority).
    2. Title-prefixed names ("Dr. Kwan", "Ms. Reyes"): always persons.
    3. Single-token persons:
       a. Hard reject if in _NEVER_PERSON_TOKENS (function words, platform names).
       b. Otherwise, accept only when _has_person_role_context fires — this lets
          day names, month names, seasons, and other "name-that-is-also-a-word"
          tokens resolve as persons when structural context supports it
          ("my friend Tuesday", "I went out with January", "my colleague August").
    4. Multi-token persons: reject if any token is a function word/platform; require
       all tokens to be proper-noun shaped (uppercase-initial). Day and month names
       are allowed as name components ("Tuesday Smith", "August Chen").
    5. Non-person subtypes: light-touch validation only.
    """
    if name.strip().lower() not in primer_cast and _appears_only_inside_paths(name, source_text):
        return False

    from .stopwords import SENTENCE_INITIAL_OR_TOOL_STOPWORDS

    if not name:
        return False

    if name in primer_cast:
        return True

    tokens = name.split()
    if not tokens:
        return False

    if subtype == "person":
        # Title-prefixed names ("Dr. Kwan", "Ms. Reyes") are always persons.
        first_token_bare = tokens[0].rstrip(".").lower()
        if first_token_bare in _PERSON_TITLES and len(tokens) >= 2:
            return True

        if len(tokens) < 2:
            # Hard reject: function words and platform names that can never be
            # person names. Days, months, seasons, and common-word names are NOT
            # in this set — they are context-gated below so that persons named
            # Tuesday, January, August, Mercury, Summer, etc. can still resolve.
            if name in _NEVER_PERSON_TOKENS:
                return False
            return _has_person_role_context(name, source_text)

        # Multi-token names: reject if any token is a function word or platform;
        # allow day/month names as valid name components ("Tuesday Smith").
        for tok in tokens:
            if tok in _NEVER_PERSON_TOKENS:
                return False
        if not all(t[:1].isupper() and len(t) > 1 for t in tokens):
            return False
        combined = " ".join(tokens)
        if _EVENT_PHRASE.match(combined) or _EVENT_PHRASE_TIME_FIRST.match(combined) or _PLACE_PHRASE.match(combined):
            return False
        return True

    # Non-person subtypes: light-touch validation.
    if name in SENTENCE_INITIAL_OR_TOOL_STOPWORDS and name not in primer_cast:
        return False
    return True

def _load_entity_index(vault: Path) -> dict[str, dict[str, Any]]:
    """Map names and tokens to entity records, keeping them distinguishable.

    "Full canonical name" and "individual token" lookups must stay
    distinguishable — a surname-only token hit is not a full-name hit.
    Each entry is marked ``"full"``,
    ``"token"``, or ``"ambiguous"`` (when two entities both claim the same
    token), and ``_match_existing_entity`` reads those flags to decide whether
    a merge is safe.
    """
    index: dict[str, dict[str, Any]] = {}
    entities_root = vault / "entities"
    if not entities_root.exists():
        return index
    for path in entities_root.rglob("*.md"):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        canonical = str(doc.frontmatter.get("canonical_name") or "").strip()
        nickname = str(doc.frontmatter.get("nickname") or "").strip()
        aliases = doc.frontmatter.get("aliases") or []
        names = [canonical, nickname] + [str(a) for a in aliases if isinstance(a, str)]
        for name in names:
            if not name:
                continue
            key = name.lower()
            existing = index.get(key)
            if existing is None:
                index[key] = {"path": path, "kind": "full", "canonical": canonical or name}
            elif existing.get("path") != path:
                existing["kind"] = "ambiguous"
            for token in name.split():
                tkey = token.lower()
                existing = index.get(tkey)
                if existing is None:
                    index[tkey] = {"path": path, "kind": "token", "canonical": canonical or name}
                elif existing.get("path") != path and existing.get("kind") == "token":
                    # Two distinct entities want the same surname token. Mark
                    # the entry ambiguous so single-token merges are refused.
                    existing["kind"] = "ambiguous"
    return index

def _entity_resolution_candidates(
    vault: Path,
    name: str,
    subtype: str,
    index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    from .reference_resolution import candidate_keys

    tokens = {token.lower() for token in name.split() if token}
    if not tokens:
        return []
    candidates: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for entry in index.values():
        path = entry.get("path")
        if not isinstance(path, Path) or path in seen_paths:
            continue
        if _entity_subtype(path) != subtype:
            continue
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        payload = dict(doc.frontmatter)
        payload["path"] = path
        payload["body"] = doc.body
        candidate_tokens = candidate_keys(payload)
        if candidate_tokens.intersection(tokens) or normalize_text(payload.get("canonical_name") or "") == normalize_text(name):
            candidates.append(payload)
            seen_paths.add(path)
    if candidates:
        return candidates
    for path in (vault / "entities").rglob("*.md"):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("type") or "") != "entity":
            continue
        if str(doc.frontmatter.get("subtype") or "") != subtype:
            continue
        payload = dict(doc.frontmatter)
        payload["path"] = path
        payload["body"] = doc.body
        candidate_tokens = candidate_keys(payload)
        if candidate_tokens.intersection(tokens) or normalize_text(payload.get("canonical_name") or "") == normalize_text(name):
            candidates.append(payload)
    return candidates

def _entity_disambiguator(name: str, summary: str, source_text: str) -> str | None:
    tokens = []
    combined = " ".join(part for part in (summary, source_text) if part).strip().lower()
    if not combined:
        return None
    exclude = {token.lower() for token in name.split() if token}
    for token in re.findall(r"[a-z0-9][a-z0-9_-]+", combined):
        if len(token) <= 3 or token in exclude:
            continue
        if token in {"this", "that", "with", "from", "into", "over", "under", "about", "after", "before", "kept", "named"}:
            continue
        tokens.append(token)
    return tokens[0] if tokens else None

_NICKNAME_HINTS: list[tuple[str, str]] = [
    ("guitar", "Guitar"),
    ("studio", "Studio"),
    ("music", "Music"),
    ("accountant", "Accountant"),
    ("budget", "Budget"),
    ("tax", "Tax"),
    ("lunch", "Lunch"),
    ("office", "Office"),
    ("gym", "Gym"),
    ("meeting", "Meeting"),
    ("project", "Project"),
    ("family", "Family"),
    ("work", "Work"),
    ("coffee", "Coffee"),
    ("school", "School"),
    ("clinic", "Clinic"),
    ("therapy", "Therapy"),
]

_NICKNAME_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "for", "from",
    "had", "has", "have", "he", "her", "his", "i", "in", "into", "is", "it", "its",
    "me", "my", "of", "on", "or", "our", "she", "that", "the", "their", "them", "there",
    "they", "this", "to", "was", "we", "with", "you", "your", "who", "what", "when",
    "where", "why", "how", "record", "records", "handle", "handles", "working", "works",
    "said", "says", "say", "doing", "do", "did", "done", "directly", "named",
    # D1a: deixis role tokens — strip {{principal}} → "principal" etc. from roots
    "principal", "self", "user",
}

def _pascalize_token(token: str) -> str:
    parts = [part for part in re.split(r"[-_ ]+", str(token).strip()) if part]
    return "".join(part[:1].upper() + part[1:].lower() for part in parts)

def _entity_first_token(name: str) -> str:
    token = str(name or "").strip().split()[0] if str(name or "").strip() else ""
    return token.lower()

def _entity_name_roots(*values: str) -> list[str]:
    combined = " ".join(str(value or "") for value in values).strip().lower()
    roots: list[str] = []
    seen: set[str] = set()
    for needle, label in _NICKNAME_HINTS:
        if needle in combined and label not in seen:
            seen.add(label)
            roots.append(label)
    for token in re.findall(r"[a-z0-9][a-z0-9_-]+", combined):
        if len(token) <= 3 or token in _NICKNAME_STOPWORDS:
            continue
        root = _pascalize_token(token)
        if root and root not in seen:
            seen.add(root)
            roots.append(root)
    if not roots:
        roots.extend(["Context", "Signal", "Thread", "Marker"])
    return roots

_USER_HANDLE_PREFIXES: list[re.Pattern[str]] = [
    # "I call her …", "we've been calling him …"
    re.compile(r"(?i)(?:i|we)(?:'ve)?\s+(?:been\s+)?call(?:ed|ing)?\s+(?:her|him|them|it)\s+"),
    # "goes by …"
    re.compile(r"(?i)goes\s+by\s+"),
    # "(her/his/their/my) nickname is …"
    re.compile(r"(?i)(?:her|his|their|my)?\s*nickname\s+(?:is|was)\s+"),
    # "aka …"
    re.compile(r"(?i)\baka\b\s+"),
    # "also known as …"
    re.compile(r"(?i)also\s+known\s+as\s+"),
]

_CAPITALIZED_WORDS: re.Pattern[str] = re.compile(
    r"[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*){0,3}"
)

_HANDLE_WINDOW = 400  # chars: how close a stated handle must be to the person's first name

def _scan_user_stated_handle(
    name: str,
    source_text: str,
    existing_handles: set[str],
) -> str | None:
    """Return the first user-stated nickname for *name* found near it in *source_text*.

    Searches for Tier-1 explicit declaration patterns ("I call her X",
    "goes by X", "aka X") within _HANDLE_WINDOW characters of any occurrence
    of the person's first name. Returns the handle verbatim (as the user wrote
    it) if it's not already taken by another entity.
    """
    if not source_text:
        return None
    first = _entity_first_token(name)
    if not first:
        return None
    text_lower = source_text.lower()
    first_positions = [
        m.start()
        for m in re.finditer(r"\b" + re.escape(first) + r"\b", text_lower)
    ]
    if not first_positions:
        return None
    for prefix_pat in _USER_HANDLE_PREFIXES:
        for m in prefix_pat.finditer(source_text):
            # Extract capitalized-word run starting at the end of the trigger phrase.
            cap = _CAPITALIZED_WORDS.match(source_text, m.end())
            if not cap:
                continue
            raw = cap.group(0).strip()
            if not raw:
                continue
            if not any(abs(m.start() - pos) <= _HANDLE_WINDOW for pos in first_positions):
                continue
            if raw.lower() not in existing_handles:
                return raw
    return None

def _entity_nickname(
    name: str,
    *,
    summary: str = "",
    source_text: str = "",
    existing_handles: set[str] | None = None,
) -> str | None:
    first = _pascalize_token(_entity_first_token(name))
    if not first:
        return None
    handles = {str(item).strip().lower() for item in (existing_handles or set()) if str(item).strip()}
    # D1b Tier 1: user-stated handle wins over any system-coined nickname.
    user_handle = _scan_user_stated_handle(name, source_text, handles)
    if user_handle:
        return user_handle
    for root in _entity_name_roots(summary, source_text):
        nickname = f"{root}{first}"
        if nickname.lower() not in handles:
            return nickname
    return None

def _entity_identity_names(fm: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for field in ("canonical_name", "nickname", "disambiguation"):
        value = str(fm.get(field) or "").strip()
        if value:
            values.append(value)
    values.extend(str(alias).strip() for alias in listify(fm.get("aliases")))
    return [value for value in values if value]

def _same_first_name_records(
    vault: Path,
    name: str,
    subtype: str,
    index: dict[str, dict[str, Any]],
) -> list[tuple[Path, dict[str, Any], str]]:
    first = _entity_first_token(name)
    if not first:
        return []
    records: list[tuple[Path, dict[str, Any], str]] = []
    seen: set[Path] = set()
    for entry in index.values():
        path = entry.get("path")
        if not isinstance(path, Path) or path in seen:
            continue
        if _entity_subtype(path) != subtype:
            continue
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        fm = dict(doc.frontmatter)
        names = _entity_identity_names(fm)
        if not any(_entity_first_token(value) == first for value in names):
            continue
        seen.add(path)
        records.append((path, fm, doc.body))
    return records

def _assign_entity_nickname(
    path: Path,
    nickname: str,
) -> None:
    try:
        doc = load_markdown(path)
    except Exception:
        return
    fm = dict(doc.frontmatter)
    if str(fm.get("nickname") or "").strip() == nickname:
        return
    fm["nickname"] = nickname
    fm["updated"] = today_iso()
    write_markdown(path, fm, doc.body)

def _ensure_nicknames_for_collision(
    vault: Path,
    records: list[tuple[Path, dict[str, Any], str]],
    *,
    source_text: str = "",
) -> dict[Path, str]:
    assigned: dict[Path, str] = {}
    existing_handles: set[str] = set()
    for _, fm, _ in records:
        existing_handles.update(str(value).strip().lower() for value in _entity_identity_names(fm))
    for path, fm, body in sorted(records, key=lambda item: str(item[0])):
        if str(fm.get("nickname") or "").strip():
            existing_handles.add(str(fm.get("nickname") or "").strip().lower())
            continue
        nickname = _entity_nickname(
            str(fm.get("canonical_name") or fm.get("id") or path.stem),
            summary=str(fm.get("summary") or body or ""),
            source_text=source_text or body,
            existing_handles=existing_handles,
        )
        if not nickname:
            continue
        _assign_entity_nickname(path, nickname)
        assigned[path] = nickname
        existing_handles.add(nickname.lower())
    return assigned

def _entity_disambiguator_from_candidates(
    vault: Path,
    name: str,
    subtype: str,
    index: dict[str, dict[str, Any]],
    summary: str,
    source_text: str,
) -> str | None:
    candidates = _entity_resolution_candidates(vault, name, subtype, index)
    if not candidates:
        return None
    return _entity_disambiguator(name, summary, source_text)

def _existing_entity_kind(index: dict[str, Any], name: str) -> str:
    """The kind of an already-indexed entity with this exact canonical name,
    or '' if none exists."""
    entry = index.get(name.lower())
    if entry and entry.get("kind") == "full":
        path = entry.get("path")
        if isinstance(path, Path):
            try:
                return _entity_subtype(path)
            except Exception:
                return ""
    return ""



def _match_existing_entity(
    vault: Path,
    name: str,
    subtype: str,
    index: dict[str, dict[str, Any]],
    primer_cast: frozenset[str] | None = None,
    source_text: str = "",
    *,
    summary: str = "",
) -> Path | None:
    """Find an entity that this proposed name should fold into, if any.

    Rules:
    - Full-name match (case-insensitive) → merge if subtype matches.
    - Single-word proposal → merge only if the token is unambiguous.
    - Multi-word proposal → require >= 2 shared tokens with the same target,
      or a single shared token whose entry resolves to the same primer-cast
      canonical as the proposal.
    """
    primer_cast = primer_cast or frozenset()

    direct = index.get(name.lower())
    if direct and direct.get("kind") == "full" and _entity_subtype(direct["path"]) == subtype:
        return direct["path"]
    if direct and direct.get("kind") == "full":
        direct_path = direct.get("path")
        if isinstance(direct_path, Path) and _entity_subtype(direct_path) == "person":
            return direct_path

    tokens = [t.lower() for t in name.split() if t]
    if not tokens:
        return None

    if len(tokens) == 1:
        # Single-word proposal can absorb into an existing multi-word entity
        # only when exactly one entity claims that token.
        entry = index.get(tokens[0])
        if entry is not None and entry.get("kind") in ("token", "full") and entry.get("kind") != "ambiguous" and _entity_subtype(entry["path"]) == subtype:
            return entry["path"]
        if entry is not None and entry.get("kind") in ("token", "full") and entry.get("kind") != "ambiguous":
            entry_path = entry.get("path")
            if isinstance(entry_path, Path) and _entity_subtype(entry_path) == "person":
                return entry_path
        candidates = _entity_resolution_candidates(vault, name, subtype, index)
        if not candidates:
            return None
        neighborhood = " ".join(part for part in (summary, source_text, name) if part).strip()
        from . import memory_pipeline as _memory_pipeline

        result = _memory_pipeline.resolve_reference(neighborhood, candidates)
        if resolution_action(result.confidence, load_bearing=True) == "bind" and result.candidate is not None:
            path = result.candidate.get("path")
            if isinstance(path, Path):
                return path
        return None

    for entry in index.values():
        path = entry.get("path")
        if not isinstance(path, Path):
            continue
        if _entity_subtype(path) != subtype:
            continue
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        candidate = dict(doc.frontmatter)
        candidate["path"] = path
        candidate["body"] = doc.body
        if _candidate_has_surname_conflict(name, candidate):
            return None

    # Multi-word proposal: tally per-target token hits.
    hits: dict[Path, int] = {}
    for tok in tokens:
        entry = index.get(tok)
        if not entry or entry.get("kind") == "ambiguous":
            continue
        if _entity_subtype(entry["path"]) != subtype:
            continue
        hits[entry["path"]] = hits.get(entry["path"], 0) + 1
    # Require >= 2 token overlap for a multi-word merge.
    for path, n in hits.items():
        if n >= 2:
            return path
    candidates = _entity_resolution_candidates(vault, name, subtype, index)
    if candidates:
        neighborhood = " ".join(part for part in (summary, source_text, name) if part).strip()
        from . import memory_pipeline as _memory_pipeline

        result = _memory_pipeline.resolve_reference(neighborhood, candidates)
        if resolution_action(result.confidence, load_bearing=True) == "bind" and result.candidate is not None:
            path = result.candidate.get("path")
            if isinstance(path, Path):
                return path
    if subtype != "person":
        # Canonical-person safeguard: if a human with this exact slug already
        # exists anywhere in the vault, prefer that record over a context-leaked
        # non-person subtype. This prevents people introduced in event turns
        # from spawning shadow records under entities/events/.
        slug = slugify(name)
        for path in (vault / "entities").rglob(f"{slug}.md"):
            if _entity_subtype(path) == "person":
                return path
    # Optional primer-cast tiebreaker: if there's exactly one single-token
    # hit *and* the proposed name is in the primer cast and the existing
    # entity's canonical is also in the primer cast under a different name,
    # refuse the merge (they are distinct primer-cast members).
    if hits and primer_cast and name in primer_cast:
        # Two primer-cast members with a shared surname: never merge.
        return None
    return None

def _entity_subtype(path: Path) -> str:
    try:
        return str(load_markdown(path).frontmatter.get("subtype") or "")
    except Exception:
        return ""

def _entity_name_tokens(name: str) -> list[str]:
    return [token.lower() for token in str(name or "").split() if token]

def _candidate_has_surname_conflict(name: str, candidate: dict[str, Any]) -> bool:
    proposal_tokens = _entity_name_tokens(name)
    if len(proposal_tokens) < 2:
        return False
    proposal_surname = proposal_tokens[-1]
    identity_haystack = " ".join(
        str(candidate.get(field) or "").strip()
        for field in ("canonical_name", "nickname", "disambiguation", "summary")
    ).lower()
    identity_names = _entity_identity_names(candidate)
    candidate_surnames = {
        tokens[-1]
        for tokens in (_entity_name_tokens(value) for value in identity_names)
        if len(tokens) >= 2
    }
    if proposal_surname in candidate_surnames:
        return False
    if candidate_surnames:
        return True
    return proposal_surname not in identity_haystack

def _append_entity_alias(path: Path, alias: str) -> None:
    """Add `alias` to the entity record at `path` if it's not already present."""
    try:
        doc = load_markdown(path)
    except Exception:
        return
    fm = dict(doc.frontmatter)
    canonical = str(fm.get("canonical_name") or "").strip()
    if not alias.strip() or alias.strip() == canonical:
        return
    aliases = list(fm.get("aliases") or [])
    if alias in aliases:
        return
    aliases.append(alias)
    fm["aliases"] = aliases
    fm["updated"] = today_iso()
    write_markdown(path, fm, doc.body)
