# Provider Diagnostics

`python3 -m lisan provider check` runs a preflight against the selected provider before you start a long chat session.

## What it checks

- provider name and selected model
- provider binary or API availability
- required session or cache directories
- whether those directories are writable
- a minimal completion request
- elapsed time for the minimal request
- structured errors and suggested fixes

## Codex session path

For the local Codex provider, the diagnostics focus on the session directory under `~/.codex/sessions`.

If the provider supports a configurable home or session root, Lisan can use an isolated provider home for experiments. For normal use, the default is to use the user's shared authenticated Codex home.

This is intentional:

- vault/state isolation protects user memory and traces
- provider auth isolation is separate and optional
  - if Codex auth is only present in the shared home, use the shared home for authentication

### Typical remediation

```bash
mkdir -p "$HOME/.codex/sessions"
chmod 700 "$HOME/.codex" "$HOME/.codex/sessions"
chown -R "$(id -un)":"$(id -gn)" "$HOME/.codex"
```

## Exit states

- `ok`: the provider and session path are usable
- `warning`: completion worked, but there are non-fatal issues worth fixing
- `failed`: the provider is not usable and chat sessions should be blocked until the issue is fixed

## Auth vs permissions

If diagnostics report `provider_auth_failure`, the problem is authentication, not directory permissions.

Typical signs:

- HTTP 401
- `Missing bearer or basic authentication in header`
- `Unauthorized`

Suggested fixes for auth failure:

- rerun the provider check with shared authentication
- authenticate Codex in the isolated provider home if you intentionally want isolation
- use an isolated provider home if you intentionally want isolation

## Why this exists

Without a preflight, a local provider failure can look like an application regression. That is misleading. The preflight lets the app fail fast and report infrastructure problems separately from chat or retrieval failures.
