# Memory retrieval guarantees

Lexical and semantic indexing use the same bounded source representation.
Entity source-log entries remain searchable after narrative compaction and
archival, while the canonical Markdown and append-only archive remain the
source of truth. The representation is bounded for index/provider safety; a
rendered result identifies the record and includes a bounded source excerpt.

Retrieval applies quarantine, operational-lane, and archive visibility before
the final candidate limit. Direct evidence is retained through graph
expansion, and graph-derived records are deduplicated without allowing a
direct record to consume expansion capacity.

Temporal fields are intentionally separate:

- observed_at or an explicit event date describes when evidence was observed.
- created describes record creation and is only a compatibility fallback.
- updated describes editing, not a new observation.
- valid_until describes validity when explicitly supported.
- review_after is a work/review deadline, never proof that a fact ended.

Missing event dates render as unknown; the system does not substitute the
current date. Historical/as-of interpretation remains explicit when records
are unresolved or contradictory.

Conversation retrieval is bounded against a conservative retrieved-context
budget before the final prompt is assembled. This reserves practical space
for instructions, tools, conversation history, and model output, but it is not
yet a hard accounting layer over the complete provider prompt or later tool
outputs. When the retrieval bound is reached, the accounting records retrieved
and included sizes and the omission reason; the canonical record link remains
available.

Evaluation judges must receive authoritative source facts when available and
must return exactly one score object for each rubric dimension. Empty or
malformed results are recorded as unjudged, never as a successful evaluation.

Known scope limits: embeddings remain one vector per record rather than
passage-level vectors, and retrieval has no explicit as-of selector yet. The
temporal fields prevent false freshness and preserve ambiguity, but they do
not by themselves implement historical selection. The repository also does
not yet contain the requested isolated raw-conversation capture-to-answer
end-to-end harness.
