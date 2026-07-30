"""Where a memory came from — trust class, and source identity.

Two different questions live here, and conflating them is how the gap this
module closes was created.

**How much authority does this memory carry?** That is the *origin class*, and
it must be bound at the write boundary from the authenticated channel, never
inferred from content. Before this module, the closest thing Lisan had was
``reliability``, which arrived from the writer's own output
(``record_fanout.py``) — a language model reading possibly-adversarial content
and grading its own trustworthiness. The 2026 work on memory poisoning shows
that both content-derived and lineage-derived trust are *malleable*: an
adversary can transform the protected signal without forging a credential,
through self-summarization, trusted-tool echo, or manufactured corroboration.
Authority has to follow origin, and origin has to be recorded by the code that
receives the content.

**Are two memories independent?** That is the *lineage key*, and it is a
different question with a different answer. Two ``external`` records from two
different documents corroborate each other. Two ``external`` records from the
same PDF do not — they are one source counted twice. Measured on a production
vault 2026-07-30: 339 of 340 knowledge records carry a ``source_document``, and
a single document had produced **38 chunks**. Against an evidence gate that
asked only ``len(support) >= 2``, that one document could found a pattern about
a person nineteen times over, by itself, and the result would read as
well-cited.

So: class decides *how much* a memory may do; lineage decides *whether two
memories are really two*.

A note on what "immutable" means here, because overclaiming it would be worse
than not having it. These are plain markdown files and the owner can edit any
field — that is the point of the architecture and it is not going to change.
Non-malleability is enforced against the *pipeline*: no model sets an origin, a
derived record cannot be more trusted than its least-trusted source, and the
validator fails a record that violates that. The owner remains sovereign; an
adversary who can only supply content does not.
"""
from __future__ import annotations

from typing import Any, Iterable

# ---------------------------------------------------------------------------
# The trust lattice

ORIGIN_OWNER = "owner"        # the principal said it, wrote it, or approved it
ORIGIN_AGENT = "agent"        # the system's own reasoning, or its own execution
ORIGIN_TOOL = "tool"          # output of a tool the system chose to invoke
ORIGIN_EXTERNAL = "external"  # third-party content the system merely received

#: Most to least trusted. Derived records inherit the **least** trusted of
#: their sources, which is what makes the binding non-malleable under
#: paraphrase: summarizing untrusted text cannot launder it into agent-authored
#: knowledge, because the summary's origin is computed, not written.
_TRUST: dict[str, int] = {
    ORIGIN_OWNER: 3,
    ORIGIN_AGENT: 2,
    ORIGIN_TOOL: 1,
    ORIGIN_EXTERNAL: 0,
}

ORIGINS = frozenset(_TRUST)

#: Unknown provenance is treated as untrusted, not as absent. A memory whose
#: origin cannot be established is exactly the memory that should not be able
#: to authorize anything, so the default fails closed.
DEFAULT_ORIGIN = ORIGIN_EXTERNAL

#: The capture front door labels turns by speaker; this is the one place that
#: mapping lives. ``SYSTEM`` and ``ADJUTANT`` are the system acting or
#: reporting on itself, which is agent-authored — not owner-authored, however
#: much the text may read like an instruction.
SPEAKER_ORIGINS: dict[str, str] = {
    "USER": ORIGIN_OWNER,
    "OWNER": ORIGIN_OWNER,
    "LISAN": ORIGIN_AGENT,
    "ADJUTANT": ORIGIN_AGENT,
    "SYSTEM": ORIGIN_AGENT,
}


def trust(origin: Any) -> int:
    """Trust level of an origin class; unknown values are least trusted."""
    return _TRUST.get(normalize_origin(origin), _TRUST[DEFAULT_ORIGIN])


def normalize_origin(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    return text if text in ORIGINS else ""


def origin_for_speaker(speaker: Any) -> str:
    """Origin class implied by a capture turn's speaker.

    Anything unrecognised is ``external``: a speaker label the code does not
    know is content arriving from somewhere it does not control.
    """
    if not isinstance(speaker, str):
        return DEFAULT_ORIGIN
    return SPEAKER_ORIGINS.get(speaker.strip().upper(), DEFAULT_ORIGIN)


def least_trusted(origins: Iterable[Any]) -> str:
    """The least trusted origin in a set — what a derived record inherits.

    An empty set yields :data:`DEFAULT_ORIGIN`, because a record derived from
    nothing traceable is not thereby trustworthy.
    """
    known = [normalize_origin(o) for o in origins]
    known = [o for o in known if o]
    if not known:
        return DEFAULT_ORIGIN
    return min(known, key=trust)


def record_origin(frontmatter: dict[str, Any]) -> str:
    """The origin class declared on a record, or ``""`` if it declares none.

    Deliberately does *not* default. A caller deciding authority should use
    :func:`effective_origin`, which fails closed; a caller reporting on the
    corpus wants to know the difference between "external" and "never labelled",
    because every record written before this field existed is the latter.
    """
    return normalize_origin(frontmatter.get("origin"))


def effective_origin(frontmatter: dict[str, Any]) -> str:
    """Origin for authority decisions: an unlabelled record is untrusted."""
    return record_origin(frontmatter) or DEFAULT_ORIGIN


def may_authorize(origin: Any) -> bool:
    """Whether a memory of this origin may ground a consequential action.

    Owner- and agent-authored memory may; tool output and external content may
    inform, never authorize. This is the elevation rule, and the corroboration
    path in :func:`independent_lineages` is how untrusted material earns more.
    """
    return trust(origin) >= _TRUST[ORIGIN_AGENT]


# ---------------------------------------------------------------------------
# Lineage — source identity, which is a different question from trust

#: Fields that name the *thing a record came from*, most specific first. A
#: document chunked into 38 records shares one ``source_document``, so grouping
#: on this collapses the chunks back into the single source they always were.
_LINEAGE_FIELDS = (
    "artifact_ref",      # the ingested artifact record
    "source_document",   # the document a chunk was cut from
    "source_path",       # the file on disk
    "source_uri",        # where it was fetched from
    "batch_id",          # the ingestion run
)


def lineage_key(frontmatter: dict[str, Any]) -> str:
    """A stable identity for where this record ultimately came from.

    Falls back to the record's own id: a record with no traceable source is its
    own lineage, which is the conservative reading — it neither corroborates
    others nor is corroborated by them.
    """
    for field_name in _LINEAGE_FIELDS:
        value = frontmatter.get(field_name)
        if isinstance(value, str) and value.strip():
            return f"{field_name}:{value.strip().lower()}"
    record_id = frontmatter.get("id")
    if isinstance(record_id, str) and record_id.strip():
        return f"id:{record_id.strip()}"
    return ""


def independent_lineages(frontmatters: Iterable[dict[str, Any]]) -> set[str]:
    """Distinct sources among these records.

    This is the number an evidence gate should count. ``len(records) >= 2`` asks
    whether someone wrote two things down; ``len(independent_lineages) >= 2``
    asks whether two sources agree, which is the question the gate was written
    to answer.
    """
    keys = {lineage_key(fm) for fm in frontmatters}
    keys.discard("")
    return keys
