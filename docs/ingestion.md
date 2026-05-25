# Ingestion

Lisan's ingestion flow is artifact-first:

1. A local file becomes an `artifact` record.
2. Parsed text becomes extracted text plus artifact-linked evidence and claim candidates.
3. The extracted statements are still hypotheses until the rest of the memory system reviews them.

This keeps file content auditable without treating it as confirmed truth.

## Supported Files

The first ingestion pass supports:

- `.md`
- `.txt`
- `.json`
- `.csv`
- `.pdf` when a lightweight parser is available
- images as artifact records only, without text extraction yet

## Default Exclusions

Files are skipped by default when they look unsafe or irrelevant:

- `.env`
- `id_rsa` and similar private key names
- `.key`, `.pem`, `.p12`, `.pfx`, `.keystore`
- password stores
- `.git`
- `node_modules`
- `__pycache__`
- virtualenv directories
- files over the configured size limit
- sealed content detected by the heuristic classifier

If a file is skipped, Lisan records the skip in the ingestion manifest and explains why.

## Sensitivity Classification

Lisan applies a simple heuristic classifier before parsing:

- financial terms -> `high`
- legal, divorce, custody, and similar terms -> `restricted`
- health or diagnosis terms -> `restricted`
- credentials or secrets -> `sealed`
- work and internal infrastructure terms -> `high`
- ordinary notes -> `medium` or `low`

Sealed content is not ingested by default.

## Artifact vs Evidence

Artifacts are source records. They say:

- this file existed
- where it came from
- when it was seen
- what kind of source it is

Evidence records say:

- what the artifact appears to state
- what facts were observed in the artifact
- how reliable the source is

Claim records say:

- what interpretation or hypothesis was derived
- how confident the system is
- what supports or contradicts it

The system should keep these layers separate.

## Commands

Scan a directory or file:

```bash
python3 -m lisan ingest scan <path>
```

Preview what would happen without writing anything:

```bash
python3 -m lisan ingest scan <path> --dry-run
```

Preview as JSON for tooling or a future UI:

```bash
python3 -m lisan ingest scan <path> --dry-run --json
```

Preview, then ask before writing:

```bash
python3 -m lisan ingest scan <path> --review
```

Show ingestion status:

```bash
python3 -m lisan ingest status
```

Run queued ingest jobs:

```bash
python3 -m lisan ingest run
```

Show a specific artifact:

```bash
python3 -m lisan ingest show <artifact_id>
```

Audit ingestion health:

```bash
python3 -m lisan ingest audit
```

List ingestion batches:

```bash
python3 -m lisan ingest batches
```

Show a specific batch:

```bash
python3 -m lisan ingest batch show <batch_id>
```

Audit a batch in detail:

```bash
python3 -m lisan ingest batch audit <batch_id>
```

Quarantine a bad batch:

```bash
python3 -m lisan ingest batch quarantine <batch_id> --reason "bad import"
```

## Safe Workflow

Use this flow for a new folder or vault export:

1. Run `python3 -m lisan ingest scan <path> --dry-run --json` first.
2. Inspect the planned actions, skip reasons, sensitivity levels, and proposed batch summary.
3. If the preview looks right, run `python3 -m lisan ingest scan <path> --review` and confirm the write.
4. A real scan creates a durable ingestion batch. Record the batch ID and inspect it with `python3 -m lisan ingest batch show <batch_id>`.
5. Inspect `python3 -m lisan ingest status`, `python3 -m lisan ingest audit`, and `python3 -m lisan ingest batches`.
6. Run `python3 -m lisan ingest run` to process queued parsing and extraction jobs.
7. Audit the batch with `python3 -m lisan ingest batch audit <batch_id>`.
8. If the batch looks wrong or unsafe, quarantine it with `python3 -m lisan ingest batch quarantine <batch_id> --reason "..."`.
9. Rebuild the index if needed, then search or assemble context.

For an Obsidian vault or project directory, start with:

```bash
python3 -m lisan ingest scan /path/to/vault --dry-run --json
```

That gives a machine-readable plan without touching the vault, the manifest, or the job queue.

Dry-run output now also includes a proposed batch summary. No batch record is persisted unless you run a real scan.

If you want to proceed after inspecting the preview:

```bash
python3 -m lisan ingest scan /path/to/vault --review
```

The queue is durable, so large scans do not need to complete in the foreground.

## Planned Actions

Dry-run output groups files by the action Lisan would take:

- `new` or `changed` files would create or refresh artifact records.
- `unchanged` files reuse the existing manifest entry.
- `unsupported` files usually become artifact-only records without parsing.
- `skipped` files are excluded by policy, size, hidden-file rules, or sensitivity rules.
- `duplicate_hash` flags content that already appears elsewhere in the manifest.

The preview also shows whether Lisan would enqueue parse jobs and evidence-extraction jobs.

## Batches

Every real ingest scan creates an ingestion batch. The batch is the durable audit unit for that run:

- source root
- scan mode
- options used
- created artifacts
- manifest changes
- queued jobs
- skipped files
- failures

The batch record makes it easier to inspect a bad import without deleting anything.

### Quarantine

Quarantining a batch:

- marks the batch as quarantined
- marks artifact records from that batch as quarantined
- cancels queued ingest jobs from that batch
- excludes quarantined artifacts and their derived memory by default during retrieval

Quarantine does not delete files or memory records. It only blocks them from normal use.

## Notes

- Artifact ingestion is idempotent by content hash.
- If a file changes, Lisan creates a new artifact record for the new hash.
- Evidence and claims derived from a file retain the artifact link for provenance.
- Nothing extracted from a file becomes a confirmed fact automatically.
