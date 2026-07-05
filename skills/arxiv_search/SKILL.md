# arxiv_search

Search arXiv via its public Atom API (ported from the Hermes agent skill,
reimplemented stdlib-only with JSON output). No credentials, no API key.

Combine filters freely: `query` (all-fields keyword search), `author`,
`category` (e.g. `cs.AI`, `cs.CL`, `stat.ML`), or fetch specific papers by
`ids`. `sort` accepts `relevance` (default), `date` (newest submissions),
or `updated`.

Abstracts are truncated at 600 characters to keep tool results compact;
the `url`/`pdf` links point at the full paper.
