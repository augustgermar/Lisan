# Phase A follow-up report

Date: 2026-08-16

## Decision

The open loop `open_loop.adjutant-and-telegram-remain-running-stale-code`
remains unresolved. Linux user-systemd stale-service detection would not close
it: the current `_stale_code_services()` implementation returns immediately
with `[]` on non-Darwin platforms. Linux service status is checked elsewhere,
but Linux stale-code age detection is not implemented.

The Linux user-systemd work is therefore classified as a separate portability
improvement proposal. It does not satisfy the loop's falsifier, which requires
a controlled service restart followed by evidence that both services remain
operational and are no longer stale. No restart, service change, proposal
approval, or live-code change was performed.

## Existing draft and repository state

The existing Phase A draft/work order was preserved at
`docs/self_repair_workorder.md`; it remains marked `REVISED 2026-08-15, NOT
SCHEDULED`. The checkout already contained user changes before this report:

```text
 M docs/self_repair_workorder.md
 M docs/ship2_enable_workorder.md
 M docs/ship2_person_enrichment.md
 M lisan/tools/skills_cli.py
 M tests/test_skills_bundled.py
?? docs/lisan_growth_spec.md
```

The Phase A implementation refuses dirty live checkouts, so no new generated
proposal was created from this state. Only this report artifact was added.

## Exact provenance

- Base commit inspected: `f801113b00fd9a5f6dfe7978fec277054efd3a68`
- Base commit subject: `phase A add self-repair proposal pipeline`
- HEAD implementation patch hash (SHA-256 of `git show --format= --binary HEAD`):
  `21e8b7d87b0ce492af5ea591fe8e5a9e39850efe3d926c83f0765cef00a60fdb`
- Follow-up proposal patch hash: `N/A` — no follow-up patch was generated,
  applied, or approved.

## Test reconciliation

Reported result: `1,218 passed / 25 skipped`.

Current command and result:

```text
python3 -m pytest -q
1236 passed, 7 skipped, 1 warning, 9 subtests passed in 15.38s
```

The suite still collects 1,243 tests. Relative to the reported result, the
current run has 18 more passes and 18 fewer skips; there is no current test
failure. The warning is the existing `lisan.paths.data_root()` test-process
redirection warning in `tests/test_paths.py::DataRootContainmentTests::test_a_test_process_never_resolves_the_live_install`.

## Targeted probe

Command:

```text
python3 -c 'from unittest.mock import patch; from lisan.tools import self_model; p=patch("platform.system", return_value="Linux"); p.start(); print(self_model._stale_code_services()); p.stop()'
```

Result:

```text
[]
```

This is the targeted negative result: on Linux the current stale-service
detector reports no services, including when the inspected service state is
the stale adjutant/Telegram condition. On the actual Darwin host, the same
detector currently reports `['adjutant', 'telegram']`, consistent with the
active loop evidence.

## File list

Files in the inspected Phase A implementation commit:

```text
config.example.json
lisan/tools/action_policy.py
lisan/tools/deviations.py
lisan/tools/job_policy.py
lisan/tools/jobs.py
lisan/tools/self_repair.py
tests/test_action_policy.py
tests/test_deviations.py
tests/test_self_repair.py
```

Report artifact added by this follow-up:

```text
docs/phase_a_followup_report.md
```

## Notification and confirmation

No Telegram message was sent because no Telegram-send capability is available
in this execution context. The repository defines confirmation machinery, but
no confirmation was issued for this read-only follow-up report; therefore no
confirmation ID can be issued.
