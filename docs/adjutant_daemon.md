# Running the Adjutant daemon (macOS launchd)

The daemon cycles every `adjutant.interval_minutes` (config.json,
default 15): fswatch pass, then poll -> gate -> execute -> report. It
takes a lockfile (`.adjutant.lock`, pid inside) in the vault root — two
daemons on one vault is an error, and a stale lock from a dead pid is
reclaimed automatically.

Remember the two keys: `adjutant.enabled: true` in config.json AND an
adopted intent.md (no 1970 sentinel dates). Without both, the daemon
runs dry — verdicts logged, nothing executed — and any halt is
edge-pinged to the owner once via Telegram and shown in
`lisan adjutant status`.

## launchd plist

Save as `~/Library/LaunchAgents/com.lisan.adjutant.plist`, adjusting
the three paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.lisan.adjutant</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/.lisan/venv/bin/python</string>
    <string>-m</string>
    <string>lisan</string>
    <string>adjutant</string>
    <string>daemon</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>LISAN_VAULT</key>
    <string>/Users/YOU/.local/share/Lisan/vault</string>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/lisan-adjutant.out.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/lisan-adjutant.err.log</string>
</dict>
</plist>
```

**`PATH` is not optional.** launchd hands a service
`/usr/bin:/bin:/usr/sbin:/sbin` unless the plist says otherwise, and the
executor binary (`codex`) normally lives in `/usr/local/bin`. Without the
`PATH` entry above the daemon runs, polls, gates, and logs verdicts
perfectly — and could never execute a task, because the binary it would
call is invisible to it. This install ran that way from 2026-07-24 to
2026-08-14; it cost nothing only because cycles were dry. The daemon now
prints its executor state at startup, so the next copy of this plist says
so on its first line rather than on the day someone sets
`adjutant.enabled: true`.

Load / unload:

```bash
launchctl load  ~/Library/LaunchAgents/com.lisan.adjutant.plist
launchctl unload ~/Library/LaunchAgents/com.lisan.adjutant.plist
```

KeepAlive restarts the daemon if it dies; the lockfile's stale-pid
reclaim makes that restart safe. If you also run the Telegram service
(`lisan telegram install-service`), both can share the vault — the
daemon locks only against another *daemon*.

## Schedules and the jobs table

`schedule` records are the definition; indexing one (re)materializes an
`adjutant.cycle` alarm job at its `next_run` (one per schedule, keyed by
coalesce group). The job only triggers a cycle — the records decide what
is due, the gate decides what may act, and the cycle advancing
`next_run` re-materializes the next alarm. Cadence never lives in the
database; delete the record and the alarm dies with the next index.
