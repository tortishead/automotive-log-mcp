# Rules: detector rules and marker rules

Two kinds of YAML rules change what the server finds without code:

| Kind | Top-level key | Used by | Effect |
| --- | --- | --- | --- |
| detector rule | `rule:` (one) or `rules:` (a list) | `detect`, the hit counts in `overview` | known failure patterns, cited by severity |
| marker rule | `marker:` (one) or `markers:` (a list) | clock alignment (`timeline`, `align`, `context(scope='all')`) | pairs events logged on two clocks |

Connector definitions (`connector:`) live in the same directories; see
[connectors.md](connectors.md).

## Where rules live

- The default detector rules ship in the package:
  [src/automotive_log_mcp/rules/detect/](../src/automotive_log_mcp/rules/detect/). There are no
  default marker rules.
- Your rules go into directories listed under `rule_dirs` in `config.yaml` (paths relative to the
  config file). Every `*.yaml` / `*.yml` file below a directory is read, in sorted order; the
  top-level key decides what a file is.
- A detector rule named like an earlier one (the package, or an earlier directory) replaces it:
  later directories win (D8). The same name twice within one directory is an error. A marker rule
  named like an earlier one replaces it too.
- An invalid file (unknown key, invalid regex, missing field) stops the server and the CLI at
  startup with the file name and the reason. A directory under `rule_dirs` that does not exist is
  an error.
- `automotive-log-mcp rules` lists the detector rules in effect, where each comes from and what it
  replaces; `rules --show <name>` prints a rule's file.
- Changing a detector rule needs no reindex: `detect` reads the records each time. Changing a
  marker rule recomputes the alignment of each case the next time it is used.

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
  clock as written, or the monotonic clock of the same source; no case-clock alignment), and
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

## Marker rules

Same lines in two sources (dmesg and `logcat -b kernel`), UTC times in kernel lines and the
bugreport's boot time align Android logs automatically. A company log on another clock (an ECU or
MCU trace, a second head unit) usually shares no lines with them, but both sides log some of the
same events: a request and its reception, a CAN signal sent and received, a sequence number. A
marker rule names such an event on each side.

```yaml
marker: vhal_set_property         # name: [a-z][a-z0-9_]*, at most 48 characters
description: VHAL SET request logged by the head unit and received by the MCU
a:
  match: 'setProperty prop=(?P<prop>0x[0-9a-f]+) seq=(?P<seq>\d+)'
  tags: [VehicleHal*]             # optional: tag globs
b:
  match: 'SET prop=(?P<prop>0x[0-9a-f]+) seq=(?P<seq>\d+)'
  sources: [mcu_trace]            # optional: globs on source name or format
delay: 2ms                        # optional: b is logged this long after a (default 0; may be negative)
tolerance: 200ms                  # optional: offsets of one stretch agree within this (default 500ms)
```

| Key | Meaning |
| --- | --- |
| `marker` | name; a later rule with the same name replaces an earlier one |
| `a`, `b` | one side each: `match` (required regex, searched in the message), `tags`, `sources` |
| `delay` | how long after `a` the event `b` is logged (`ns`, `us`, `ms`, `s`, `m`, `h`) |
| `tolerance` | how far the offsets of one stretch may spread |
| `description`, `version` | free text |

How the pairs become an alignment:

- Both regexes need the **same named groups**; their values are the key that makes two events
  the same event. A key seen twice on one side is not used (it is ambiguous). Pairs are only made
  between two different clocks.
- At least two pairs are needed (three when there are six or more candidates), and at least half
  of the pairs must agree within `tolerance`; a new stretch (piece) starts where the offset
  changes and the next pair confirms it (a clock that stopped during suspend, or was set). With
  five or more pairs spanning a minute or more, a drift is fitted.
- The result is `paired` when the offsets agree within 10 ms over at least three pairs, else
  `marker`; `align(case)` prints the rule's name and how many events of how many candidates the
  mapping rests on. The error is never smaller than the timestamps' resolution (1 ms for logcat).
- Marker rules are checked against manual settings and other anchors as described in
  [connectors.md](connectors.md#clock-alignment-m5): the best path from the reference clock wins.
- **Redaction:** marker patterns run over the redacted messages and, unlike detector patterns,
  are not redacted themselves. Write them against the text as the tools print it; a key that is a
  redacted value (a serial number) still pairs, because the same value gives the same token on
  both sides.

Example: [examples/rules/markers_vhal_set.yaml](../examples/rules/markers_vhal_set.yaml), with the
MCU trace connector in [examples/connectors/](../examples/connectors/). After adding a marker
rule, check it with `automotive-log-mcp align <case>`; a source still "not placed" means no key
paired.
