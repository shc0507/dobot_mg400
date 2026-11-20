# Agent Connect: Phase 2 and Phase 3 change reference

This file records the changes, checks and deployment locations for these phases. The branch is `feat/agent-connect-phase-0-1`. The approved design is [proposal v3](../pdf/Agent_Connect_Design_Proposal_v3.pdf). The earlier Phase 0 and Phase 1 work remains in the working tree. No commit or push was made.

## Phase 2: sample records

- Added ordered `chip_events`, current sample rows and a shared sample revision to the existing `runs.db`. Event insertion and current-state updates share a transaction. Existing run measurements remain unchanged.
- Added exact chip selection, bounded pages and revision checks between pages. Reads do not create evidence. Historical records can identify a chip without claiming its current location.
- Added the evidence bases `observed`, `reported`, `inferred` and `unknown`. Pending handoffs, rejected operations, conflicting observations and old observations have separate handling. Completion must match the pending operation.
- Kept the holder separate from furnace entry. An uncertain entry reference does not erase an established stage holder.
- Added chip location and evidence to the passport. The latest run and step come from started work for that exact chip.
- Added a shared manual-report handler and simulated report controls. Live report controls remain disabled until Phase 4 provides operator authority.

Main files: `src/orchestrator/agent_connect/samples.py`, `src/orchestrator/store.py`, `src/orchestrator/web/passport.py`, and the passport HTML, CSS and JavaScript.

## Phase 3: read stream

- Added discovery, immutable contract bundles, current-state reads, sample pages and a receive-only WebSocket under `/lab/api/agent`.
- Added selected `devices`, `samples` and `operations` groups. Connections select 1, 2 or 4 Hz; the default is 2 Hz.
- Added process IDs, contract digests, sequence numbers, source ages and explicit quality. Frame creation time does not replace source capture time.
- Added bounded event replay, independent subscribers, slow-reader handling, and explicit restart or missing-history messages. Replayed events keep their original contract digest.
- Added compact robot, stage, furnace, microscope, thermal and vision fields. Stage coordinates use rotations. Missing references and source times remain unknown.
- Added one shared thermal statistics reader at 1 Hz. It reads scalar summaries without image matrices. The vision reader fetches an existing result when the job changes; it does not start camera capture or inference. Expired results remain unavailable until another job or process appears.
- Added read-stream diagnostics to the Agent Connect tab. Device commands and control grants remain disabled.

Main files: `src/orchestrator/agent_connect/{protocol,state,stream,routes,runtime,observations}.py`, `src/orchestrator/app.py`, `src/orchestrator/web/{console,control_socket}.py`, the Agent Connect workspace and JavaScript, and `config/proxy/Caddyfile`.

## Local portal

Open <http://127.0.0.1:8792/>. The portal remains connected only to the simulated Console on this PC. It does not connect to live equipment.

The Samples & stream view has 2 independent test connections. It supports chip selection, rates, slow reading, reconnects, restart and replay-gap tests, and sample evidence scenarios. Data format tests retain the earlier compatibility fixtures. Dark and light appearances remain available.

The mock HTTP services use ports 8790 and 8792. The mock WebSocket uses port 8793. Start them with `python scripts/agent_portal.py` from the repository root. Current instructions are in [agent-connect.en.md](../../docs/agent-connect.en.md) and [agent-connect.md](../../docs/agent-connect.md).

## Test deployment

- Test page: <http://100.64.0.1:8082/lab/#agent-connect>.
- Discovery: <http://100.64.0.1:8082/lab/api/agent/discovery>.
- Updated 30 runtime/template files. A subsequent 1-file update corrected expired vision-result handling.
- Only `mg400-twin-test` and `mg400-twin-proxy` restarted. The shared device services, main orchestrator and main proxy retained their process IDs. Port 8080 remained available.
- Preserved the installed dependency files. The Console runtime dependencies match the local project; unrelated older dependency groups were not replaced. No dependency rebuild or font rebuild was needed.
- Backup: `/var/lib/mg400/twin-test/agent-connect-phase-2-3-backup`. It contains the original changed files, manifest, proxy configuration and SQLite backup. The deployment manifest remains `/var/lib/mg400/twin-test/deployment-manifest.json`.
- Remote source, test logs, update archives and deployment result: `/home/olares/agent-connect-phase-2-3/`.
- Local deployment helpers and verification evidence: `work/agent-connect-phase23/`.

Rollback must restore the recorded files and proxy under the deployment lock after checking the current manifest. Preserve any newer changes. Restore the SQLite backup only if its loss of later records is acceptable; the new tables are additive.

## Checks and limits

- Local focused checks: 119 Python tests passed before the final vision fix. The final affected checks passed: 18 Python tests. All 5 Agent Connect JavaScript checks passed. Ruff and diff checks passed.
- Isolated Linux checks: 709 passed and 3 skipped. The final vision checks passed: 16 passed and 2 skipped. The skipped checks require an optional JSON Schema validator that is absent on Olares. They passed locally.
- Browser checks covered independent connections, sample evidence, old and conflicting observations, unsupported formats, replay gaps, restart, slow readers, chip selection, passport display, themes and mobile layout. The live Agent Connect panel and local source/portal passed final browser checks without JavaScript errors.
- Live state, sample pages and stream messages passed JSON Schema validation on this PC. Measured frame intervals matched 1, 2 and 4 Hz. A mismatched process ID produced a restart/resynchronization message. Thermal statistics were available. The expired vision result was correctly unavailable.
- The test store currently has 0 chip records. No mock chip records were copied to Olares. Live furnace-entry mapping is not configured. Physical handoff evidence and agent command execution require the later integration phases.
- No physical movement, heating, gas, firmware or calibration tests were performed. The local portal remains simulated; connecting that portal to the live Console is planned for Phase 7.
- All 4 task screenshots were inspected and removed after verification, totaling 481387 bytes.
