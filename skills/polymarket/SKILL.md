# polymarket

Query Polymarket prediction markets via its public Gamma/CLOB APIs. Read-only,
no credentials, no trading — prices, order books, and market metadata only.

The bundled `polymarket_client.py` (ported from the Hermes agent skill,
stdlib only) does the actual work; `tool.py` maps the `action` parameter
onto its subcommands and runs it in a subprocess.

Typical flow: `search` or `trending` first (returns slugs and token ids),
then `market`/`event` for details or `price`/`book` for live numbers.
