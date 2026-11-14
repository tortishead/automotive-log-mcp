# Rules: detector rules

YAML rules change what the server finds without code:

| Kind | Top-level key | Used by | Effect |
| --- | --- | --- | --- |
| detector rule | `rule:` (one) or `rules:` (a list) | `detect`, the hit counts in `overview` | known failure patterns, cited by severity |

Connector definitions (`connector:`) live in the same directories; see
[connectors.md](connectors.md).

## Where rules live

- The default detector rules ship in the package:
  [src/automotive_log_mcp/rules/detect/](../src/automotive_log_mcp/rules/detect/).
- Your rules go into directories listed under `rule_dirs` in `config.yaml` (paths relative to the
  config file). Every `*.yaml` / `*.yml` file below a directory is read, in sorted order; the
  top-level key decides what a file is.
- A detector rule named like an earlier one (the package, or an earlier directory) replaces it:
  later directories win (D8). The same name twice within one directory is an error.
- An invalid file (unknown key, invalid regex, missing field) stops the server and the CLI at
  startup with the file name and the reason. A directory under `rule_dirs` that does not exist is
  an error.
- `automotive-log-mcp rules` lists the detector rules in effect, where each comes from and what it
  replaces; `rules --show <name>` prints a rule's file.
- Changing a detector rule needs no reindex: `detect` reads the records each time.

## Detector rules

```yaml
# One rule per file (or `rules: [ {rule: ...}, {rule: ...} ]`).
rule: hvac_bus_timeout            # name: [a-z][a-z0-9_]*, at most 48 characters
version: "1"                      # optional, for your own bookkeeping
severity: medium                  # high | medium | low (order of the detect output)
description: HVAC bus write timed out
match: 'HVAC bus write failed .*\(timeout\)'   # Python regex, case-sensitive
level: E                          # optional: minimum level V D I W E F
tags: [DefaultVehicleHal, 'Hvac*']             # optional: tag globs, case-sensitive
note: the abort right after it is the VHAL restarting; read the 10 s before it
```

| Key | Meaning |
| --- | --- |
| `rule` | name; the key for replacing a rule and for `detect(rules=[...])` |
| `severity` | `high`, `medium` or `low` |
| `description` | one line, printed with the hits |
| `match` | regex, searched (not anchored) in the message and, separately, in the tag |
| `level` | minimum level |
| `tags` | tag globs (a string or a list); a record must have one of them |
| `any` | instead of `match`/`level`/`tags`: a list of alternatives, each with its own `match`/`level`/`tags`; a record matching several counts once |
| `followed_by` | a second event: `match`/`level`/`tags` or `any`, and `within` (`500ms`, `30s`, `2m`, `1h`); a hit is a first event with a second event within that time after it. With `absent: true` a hit is a first event **without** one (e.g. "boot not completed") |
| `group_by` | `pid`: one output line per process; with `followed_by` the second event must have the same pid |
| `note` | what to read next, printed with the hits |
| `version` | free text |

A condition needs at least one of `match`, `level`, `tags`.

How a rule runs:

- `detect(case, rules?, filter?, examples)` runs every rule (or the named ones) over the records
  that pass the filter. Literal parts of `match` prefilter in SQL; each candidate is then tested
  in Python. Hits are collapsed by message template (by pid with `group_by: pid`); each line
  cites its first hit, with the count and the last one.
- **Redaction:** patterns are redacted like search queries (D5): a raw MAC or VIN written in a
  pattern matches its token in the logs.
- **Clocks:** a `followed_by` second event is looked for on the same clock as the first (the wall
  clock as written, or the monotonic clock of the same source), and
  beyond the filter's `to`. With `absent: true`, a first event too close to the end of the case
  for the window to close is marked as such.
- **Limits:** a rule with a `match` scans at most `caps.regex_scan_rows` candidate records (as a
  regex search does); for rules with `followed_by` or `group_by` at most 20,000 first events are
  evaluated. Beyond either the result says it is partial. A rule with only `level`/`tags` is
  counted in SQL and is exact.

Test a rule on a case without an agent:

```bash
automotive-log-mcp rules                                   # is it loaded, from where, what does it replace
automotive-log-mcp detect T-1 --rule hvac_bus_timeout --examples 5
automotive-log-mcp detect T-1 --rule hvac_bus_timeout --from "03-14 08:12:00" --to "+1m"
```

For a rule in a repository, add a constructed log with one line that must hit and near misses that
must not, as `tests/test_p1_tools.py` does for the package rules.

### The default rules

| Rule | Severity | Matches |
| --- | --- | --- |
| `java_crash` | high | `AndroidRuntime: FATAL EXCEPTION`, Java crash artifacts |
| `native_crash` | high | `libc: Fatal signal`, the debuggerd `*** ***` banner, tombstones |
| `anr` | high | `ActivityManager: ANR in`, ANR artifacts |
| `watchdog` | high | `Watchdog: Blocked in handler/monitor`, `WATCHDOG KILLING SYSTEM PROCESS` |
| `boot_not_completed` | high | `boot_progress_start` not followed within 3 min by `boot_progress_enable_screen`, `sf_stop_bootanim` or boot completed |
| `lmk` | medium | `lowmemorykiller`/`lmkd: Kill '…'`, kernel `Out of memory: Killed process` |
| `binder_failure` | medium | `!!! FAILED BINDER TRANSACTION !!!`, `DeadObjectException`, kernel `binder: … transaction failed` |
| `vhal_error` | medium | E/F from VHAL tags (`*VehicleHal*`, `*Vhal*`, …), `status=NOT_AVAILABLE` (and `TRY_AGAIN`, `INTERNAL_ERROR`, `ACCESS_DENIED`, `INVALID_ARG`) at W or above |
| `service_restart` | medium | `init: Service '…' (pid N) exited/killed/received signal`, `Process … has died`, `Scheduling restart of crashed service` |
| `selinux_denial` | low | `audit(…): avc: denied`, one line per pid |

To change one, copy its file into a rule directory and edit it there (same `rule:` name); to turn
one off, replace it with a rule that cannot match in your logs, or leave it out of
`detect(rules=[...])`.
