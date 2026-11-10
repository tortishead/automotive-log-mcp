# DLT

automotive-log-mcp does not read DLT (AUTOSAR Diagnostic Log and Trace) files itself: the DLT
connector through `dlt-core` (plan, D9 and D12) was left out of M5, which built the multi-source
parts (clock alignment, `timeline`, the `command` connector) without it. A DLT decoder you
already have can be plugged in as a `command` connector that writes JSON lines (ECU, APID and CTID
as `attrs`, ECU ticks as `mono`), see [connectors.md](connectors.md); its sources then share the
case clock with the Android logs. Without such a connector there are two ways to work on a defect
whose evidence is partly in DLT.

## 1. dlt-mcp-server in DLT Viewer, next to this server

dlt-mcp-server is a DLT Viewer plugin that serves the trace open in DLT Viewer over MCP: a
summary of the file (message count, size, time range, the APID/CTID contexts in it) and queries
over the messages. It comes from a separate project with its own documentation and release;
install and configure it as that project describes. The steps below are on the client side only,
and the plugin's tool names and options are its own (not covered by D11 of this project).

1. Open the DLT file (and the FIBEX file, for non-verbose traces) in DLT Viewer with the plugin
   loaded, as the plugin's documentation says.
2. Add the plugin's MCP server to the same client as automotive-log-mcp, as a second entry
   next to `automotive-log` in `.vscode/mcp.json` (VS Code) or `.mcp.json` (Claude Code), with
   the command, URL or port the plugin's documentation gives. The agent then sees both tool sets.
3. Tell the agent which tools hold which logs, e.g. in the symptom for `defect_triage`: "Android
   logs are in case T-4711 (automotive-log tools); the DLT trace of ECU1 is open in DLT Viewer
   (dlt tools)."

What to expect:

- **Citations:** this server's `log:`/`art:` citations cover the Android side only.
  `check_citations` checks those and ignores the DLT side; ask the agent to cite DLT messages by
  their DLT Viewer index (and ECU, APID, CTID) and check those by hand in DLT Viewer.
- **Time:** the two sides are not aligned. DLT storage time, ECU timestamps (ticks since ECU
  start) and the Android wall clock can be seconds or more apart; cross-source alignment is the
  clock model, which needs the DLT side indexed by this server. Have the agent pair events by content (a request and its answer), not by time
  alone, and say when an order between the two sides is a guess.
- **Redaction:** the DLT side is whatever the plugin returns. This server's redaction (D5) does
  not apply to it; check the plugin's behaviour before using an agent on traces with personal data
  (VINs, positions).

## 2. A text export, indexed by this server

For a one-off case, export the trace from DLT Viewer as text ("Export" → ASCII, the messages
you need, decoded with the FIBEX file if non-verbose) and index the export with a YAML connector.
Then all tools (search, context, count, detect, check_citations) and redaction apply to it, and
its lines get `log:` citations.

1. Copy [examples/rules/dlt_viewer_ascii.yaml](../examples/rules/dlt_viewer_ascii.yaml) into a
   directory listed under `rule_dirs` in `config.yaml` (or list `examples/rules` itself). Compare
   one exported line with the regex at the top of the file and adjust it: the columns depend on
   the DLT Viewer version and its export settings. The connector maps the context id (CTID) to
   `tag` and the DLT log level to `level`, and keeps ECU, APID, ticks and message type as
   attributes (`filter={'attrs': {'ecu': 'ECU1', 'apid': 'HVAC'}}`).
2. Index the export, forcing the connector if the file name does not end in `_dlt.txt`:

   ```bat
   py -m automotive_log_mcp index T-4711 C:\defects\T-4711\ecu1_dlt.txt --format dlt_viewer_ascii
   ```

   ```bash
   python -m automotive_log_mcp index T-4711 ~/defects/T-4711/ecu1_dlt.txt --format dlt_viewer_ascii
   ```

Limits of the export route: the storage time printed by DLT Viewer is the wall-clock time of the
logger, not of the head unit (it is `assumed` on the case clock until an anchor places it); ECU
ticks are kept as an attribute but not used as a clock; DLT file transfers, control messages and non-verbose payloads without
FIBEX are not decoded; a multi-GB trace exported as text is slow to index (about 30–40k lines/s,
see the M1 status). Marker rules or `align` anchors place the export on the case clock
([connectors.md](connectors.md#clock-alignment-m5)).
