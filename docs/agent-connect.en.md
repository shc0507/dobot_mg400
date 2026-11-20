# Agent Connect

Agent Connect shows service observations from the Console's shared device cache. The tab does not acquire control or send device commands. It shows unavailable for missing process IDs.

## Local portal

Enable the VPN in LarePass. Run the portal from the repository root:

```powershell
uv run python scripts/agent_portal.py
```

The command opens Agent Portal at `http://127.0.0.1:8792/`. Olares data stream starts with 1 read-only connection to `http://100.64.0.1:8082`. The connection shows Live · Read only. If the VPN or source is unavailable, the portal shows the connection error. It does not substitute simulated data.

The local server relays the read-only Olares WebSocket as server-sent events (SSE). It subscribes to `devices`, `samples` and `operations`. Updates per second selects 1, 2 or 4 Hz. The default is 2 Hz. Disconnect stops the reader. Reconnect starts it again.

The page shows device readings, Active samples, Operations and the last 100 events under Recent events. Full state JSON shows every field in the received state. The browser checks the immutable data agreement and its digest before it decodes readings. Unsupported definitions or a digest mismatch stop decoding. Raw JSON remains available.

Use `--no-browser` to start without opening a tab. Press Ctrl+C to stop the servers. The local servers bind only to loopback. HTTP uses ports 8790 and 8792. The mock WebSocket uses port 8793.

Close another Console preview before starting the portal. Both use port 8790.

Use the appearance control to select dark or light mode. The browser saves the preference locally and restores it when the page opens again.

## Optional simulation tests

Enable simulation tests is off by default. Enable it to stop the live reader and open the test section. Disable it to close the test connections and resume the live reader. The tests make no background reads while disabled. Only the selected test tab runs.

The optional tests use the Simulated Console at `http://127.0.0.1:8790/lab/`. Open it from the link in the test section. The launcher does not open it automatically. The test source shows `MOCK`. Test controls change only local simulation data and never contact Olares.

The mock adapter uses a fixed loopback address and ignores proxy variables. It rejects redirects and non-mock discovery responses.

### Data format tests

Select Data format tests for the compatibility fixtures under `/lab/api/agent-fixtures`.

When updates run, the Simulated Console supplies changing stage positions and temperatures. It also contains 2 chip examples. Earlier agent version (A) and Newer agent version (B) are scripted tests. Each keeps a separate copy of the data agreement: the message format, fields and units. The newer script supports the added optional field.

1. Open the Simulated Console. Inspect discovery, received readings and the verified data agreement in Data format tests.
2. Select Pause updates. A reading older than 3 seconds becomes Outdated reading. Polling preserves its original sample time.
3. Select Resume updates to receive new samples. Simulate old readings pauses updates and immediately makes the current sample outdated.
4. Choose a test setup. Compare the current and saved agreement digests, changed definitions and each script's compatibility result.
5. Disconnect a script. Reconnect it after Simulate Console restart to inspect the new process ID.
6. Run the basic scenarios. Export received messages, agreement files and expected/actual results as JSON.

| Test setup | Check |
| --- | --- |
| Original format (1.0) | Use the original definitions. |
| Add optional field (1.1) | Accept a compatible addition. |
| Change position units (2.0) | Detect changed units. |
| Unrecognized schema format | Reject an unsupported schema dialect. |
| Damaged agreement file | Reject a file whose digest does not match. |

Change coordinate reference increments the stage reference revision and preserves its position. Simulate Console restart changes the Console and stage process IDs, resets the sequence and increments the reference revision. It preserves readings and chip records. Reset demo restores the original format, starting readings and chip records, then resumes updates. It also resets the sequence, reference revision and source history and creates new process IDs.

The scenario runner uses a separate disposable source. It does not reset the interactive source. The export retains bounded message history. Restarting the portal clears its in-memory records.

### Samples and stream tests

Samples & stream uses `/lab/api/agent`. Connection A and Connection B are independent browser WebSocket connections. Each verifies its agreement digest and retains its own replay cursor. Choose device, chip or operation groups and a rate of 1, 2 or 4 Hz. The default is 2 Hz. Enter exact chip IDs on separate lines, or select chips with work in progress. Apply the selection to both connections.

Use Read slowly on either connection while the other continues. Disconnect and reconnect a connection to resume from its cursor. Simulate restart changes the process identity. Expire saved events removes replay history. The connection reports missing history and reads a fresh snapshot. Agreement changes trigger discovery. An unfamiliar frame digest also triggers discovery if the change event was missed. Unsupported definitions stop decoded readings.

Browse saved chips reads a paged list. If the sample revision changes between pages, start again with Browse saved chips. Manual reports and Mark location unknown change only the mock sample store. The evidence buttons simulate pending picks, inferred placement, camera continuity, old frames and conflicting identity. Simulated furnace entry uses a test mapping. Losing its reference preserves the stage holder and makes entry unknown.

## Sample evidence

Phase 2 stores ordered `chip_events` and `sample_current` rows in the existing `runs.db`. Event insertion, current state and revision updates share a transaction. The existing run measurements in `samples` remain unchanged. Reads do not create events or current rows. Current rows can be rebuilt from chip events.

Chip IDs remain exact. Historical run, step, measurement, artifact and event records identify chips but do not establish their current location. An imported chip starts with an unknown location. Its latest run and step refer only to started work for that chip. A newer unstarted run or another chip's work does not replace them.

The evidence basis is `observed`, `reported`, `inferred` or `unknown`. Acceptance does not prove arrival. A handoff can make the holder unknown while preserving the last known location and pending destination. A confirmed rejection before motion preserves the previous holder. Old frames cannot restore an outdated location. Conflicts make the disputed claim unknown. Source observation time stays separate from record update time.

The holder and furnace entry are separate claims. Losing an entry reference preserves an established stage holder. No live furnace mapping is configured, so `inside_furnace` remains `null` on Olares. A mock mapping does not establish a live location.

The chip passport reads `/api/chip/location?id=...` under the Console base path. It shows current evidence beside existing history. Report location and Mark unknown remain disabled until Phase 4 adds operator control. The local mock form is the only manual-report interface in these phases.

## Read API and replay

Phase 3 publishes these read routes under `/lab/api/agent`:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/discovery` | Read process identity, current agreement and endpoint paths. |
| GET | `/contracts/{digest}.json` | Read an immutable JSON Schema 2020-12 bundle. |
| GET | `/v1/state` | Read a selected current snapshot. |
| GET | `/v1/samples` | Read a bounded sample page. |
| WebSocket | `/v1/stream` | Receive current state and ordered events. Sending commands is not supported. |

State and stream options are `groups`, `rate_hz`, `selection` and repeated `chip_id` fields. Groups are `devices`, `samples` and `operations`. Select `active` or `ids`. Active chips have a pending operation or started work in a running or paused run. Explicit selection accepts at most 50 IDs, each at most 256 characters. Use repeated `chip_id` fields for exact IDs containing commas. The alternate `chip_ids` field splits IDs at commas.

Sample pages also accept `selection=all`, `limit`, `offset` and `expected_revision`. The maximum page size is 100. Preserve the returned revision in `expected_revision` for subsequent pages. A revision mismatch requires pagination to restart. Read `count`, `total`, `complete` and `next_offset`. Completeness applies only to the selected chips.

The shared producer assembles frames at 4 Hz. Subscribers receive full frames at 1, 2 or 4 Hz. Device measurements can update more slowly. Source time and age remain separate from frame time. Subscribers read the shared device cache and do not add hardware polling. State, replay, subscriber counts and message sizes have fixed bounds. A slow reader keeps the newest unsent state rather than accumulating old frames.

One shared reader reads thermal summaries at 1 Hz. It reads the existing latest vision result once when the job changes. It starts no capture jobs. All subscribers reuse these summaries without separate hardware polling.

Older vision results have `unknown` quality for the present scene. Capture time remains `null` when the source omits it.

Reconnect with both `boot_id` and `event_seq`. The stream sends a current snapshot before retained events. Events retain their original `contract_id`, so replay does not change the current agreement. A `resync` message reports `boot_changed`, `cursor_ahead` or `history_evicted`. Read a fresh snapshot after a gap. The shared sampled history remains separate from repeated broadcasts.

## Current boundary

Phase 0 supports discovery, immutable test agreement files, HTTP snapshots and local compatibility checks. These files use JSON Schema 2020-12. Their digest uses the JCS subset with ASCII keys and safe integer values. Runtime readings do not change the digest.

The changed-unit test setup changes stage units. Scripts report that they require an update and leave decoded cards unavailable. Raw JSON remains visible. Unrecognized schema formats and damaged agreement files also block interpretation.

Phase 1 observations appear in the Console tab when the services publish them. The tab reads cached process IDs, stage references and selected observation fields. It adds no device polling.

Phases 2 and 3 add sample evidence and read-only streaming. Agent commands, connection approval and control grants remain disabled. On Olares, these read routes belong to the Console service on port 8082. The local portal reads that service through the VPN. Optional simulation tests remain local and do not establish physical-device behavior.
