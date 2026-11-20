# Agent Connect — Phase 0 and Phase 1 change reference

This file records the implementation, checks, deployment, and rollback locations for this change. No physical movement tests were requested or performed.

## Branch and proposal

- Branch: `feat/agent-connect-phase-0-1`.
- Local base: main commit `f5db7f9ea8c76244ba47878d0109fd01b9c8ccd5`.
- Approved design: [Agent Connect proposal v3](../pdf/Agent_Connect_Design_Proposal_v3.pdf).
- The original proposal and v2 remain unchanged.
- No feature commit or push was made. Implementation files remain in the local working tree.

## Phase 0: local mock connection

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts/agent_portal.py
```

- Mock Console: <http://127.0.0.1:8790/lab/>.
- Agent Test Portal: <http://127.0.0.1:8792/>.
- The launcher starts both servers. Close another preview that uses port 8790 first.
- Both servers use loopback. The portal has no live-device mode or arbitrary target URL.
- Clients A and B maintain separate contract caches. They are scripted test clients, not AI model integrations.
- The GUI shows changing mock stage and temperature values, 2 chip fixtures, raw state, contract differences, and message history.
- Controls simulate paused readings, reference changes, service restarts, client disconnection, and contract updates.
- Contract fixtures cover compatible additions, changed units, unsupported schema dialects, and a broken digest.
- A separate disposable source runs the test scenarios. JSON export contains expected and actual results and bounded message history.
- Each reading uses its own quality flag. An unknown reading is unavailable. A stale reading stays marked stale.

Code: `scripts/agent_portal.py` and `src/orchestrator/agent_connect/`.

## Phase 1: service observations

| Area | Change |
| --- | --- |
| Shared event log | Process instance, oldest retained sequence, replay-gap reason, and restart detection. The existing sequence-only query still works. |
| Robot | State and events use the same process instance. The custom HTTP event route accepts an instance-aware replay query. |
| Stage | Instance, reference revision, change reason, and per-axis reference validity. Optional paired expected-instance and expected-revision arguments reject an outdated motion request. The check runs again after fresh telemetry, under the serial lock. |
| Thermal | Instance and `GET /api/frame?summary=1`. The compact response contains statistics and configured region summaries, without temperature matrices. Capture time remains unknown when the camera cannot supply it. |
| Furnace and microscope | Process instance in state. Device reconnects preserve that instance. Service recreation changes it. |
| Vision | One final event for each completed job. Result lookup requires the service instance and job ID. Storage is bounded by 100 results, 600 seconds, and 8 MiB of serialized data. Missing results never use an unrelated last result. |
| Console history | Clients send the saved instance with their replay cursor. Stored events use service, instance, and sequence. The SQLite migration preserves legacy rows. The history view records restart and replay-gap notices. |
| Agent Connect tab | Read-only service diagnostics from the existing device cache. The tab adds no device polling or control commands. |

The stage revision does not change for ordinary motion or Stop. Reference resets, changed limits, calibration changes, and reconnects invalidate old references. A board reset that produces identical telemetry cannot be identified without a firmware reset indicator.

Main paths: `src/labkit/{events,flask,client}.py`, `apps/control_panel/server.py`, `src/mg400/web_control.py`, `src/stage_controller/{contract,controller}.py`, the 4 instrument service modules, and `src/orchestrator/{clients,store}.py`.

Console paths: `src/orchestrator/web/console.py`, `web/workspaces/agent_connect.py`, and `web/static/console/agent-connect.*`.

Paired instructions cover Agent Connect, event replay, stage references, thermal summaries, and vision results. `scripts/select_tests.py` and the `AGENTS.md` area table include the new module.

## Validation

- Focused Linux run across 51 test files: 2,523 passed, with 2 follow-ups. Both follow-ups passed after updating the replay assertion and using an absolute test import path.
- Exact shared-service candidate: 138 tests passed.
- Final robot HTTP route and robot control tests: 212 passed.
- Portal JavaScript checks: 4 passed.
- Local portal, preview, documentation, and repository-file checks passed. The local portal browser check passed without external requests.
- Ruff lint, format checks, document pairs, and diff checks passed.
- All 6 task screenshots were inspected and deleted, totaling 466,233 bytes.
- Linux tests used an isolated network namespace and simulated devices. The existing vision decoder package was copied into the test directory only.
- No full repository test suite, firmware upload, physical motion, heat command, gas command, or calibration command was run.

The first deployment attempt restored the previous source automatically. Its replay check found that the robot launcher bypassed the source override. The corrected override runs the launcher from the shared test copy. The robot replay route also received an instance-query fix.

## Olares deployment

Target: <http://100.64.0.1:8082/lab/#agent-connect>.

The owner checkout at `/opt/mg400` remains unchanged. The shared candidate preserves owner commit `a87b09e467217261217ee3a31b7f2f76667a325f`, including the newer robot stop changes, and adds this feature's service patch.

| Location | Purpose |
| --- | --- |
| `/opt/mg400-twin-test` | Console runtime source for port 8082. Existing environment and saved data remain in place. |
| `/opt/mg400-agent-connect-phase-0-1` | Separate shared-service source copy. |
| `/etc/systemd/system/<unit>.service.d/agent-connect-phase-0-1.conf` | Source override for the 6 device services. Robot also uses the launcher from the shared copy. |
| `/var/lib/mg400/twin-test/agent-connect-phase-0-1-backup` | Original test runtime files, deployment manifest, pre-deployment state, plan, and SQLite backup. |
| `/home/olares/agent-connect-phase-0-1` | Uploaded source, patches, isolated tests, deployment helper, and read-only verification results. |
| `work/agent-connect-release/` on this PC | Local deployment helper, browser check, evidence, and portal logs. |

Device units: `mg400-web-panel`, `mg400-stage-panel`, `mg400-thermal`, `mg400-furnace`, `mg400-microscope`, and `mg400-vision`.

The deployment checks that devices and both orchestrators are idle before stopping services. It restarts these 6 units and `mg400-twin-test`. It does not restart either shared camera capture service, either proxy, or the main orchestrator. The test Console runtime also includes changes from current main because its previous source predates the merged branch.

The override remains active until it is removed or replaced. Updating the owner's main checkout alone does not update these 6 overridden services. Review this override when merging the feature or returning the test services to main.

## Final live checks

Deployment completed on 2026-11-20 at 21:23:41 UTC. Both Console URLs returned HTTP 200. The owner checkout remained clean.

- All 6 state/event instance pairs matched. An old-instance query reported a restart on each service.
- A live thermal summary was 623 bytes and contained statistics without temperature matrices.
- A read-only label scan from camera 4 completed. Its result lookup returned the matching job, and its event appeared once. The scan returned 0 labels. This does not establish tray-marker recognition accuracy.
- The deployed tab passed desktop and mobile browser checks. Its assets match the local files.
- The local portal remains running. The final service states showed no active operation or fault. Robot and stage remained disconnected.

Local evidence: `work/agent-connect-release/olares-verification.tar.gz`, its extracted directory, `browser-result.json`, and `changed-files.json`.

## Rollback

Confirm that the devices and both Consoles are idle. Then run on Olares:

```sh
sudo python3 /home/olares/agent-connect-phase-0-1/deploy_phase01.py rollback
```

The helper restores the previous 8082 runtime files and manifest, removes this feature's service overrides, and restarts the affected services. It does not change the owner's checkout or camera configuration. It leaves the SQLite data in place because the migration remains readable by the previous code. The database backup is retained for separate recovery.

## Remaining scope

### Local portal UI follow-up

The local portal at <http://127.0.0.1:8792/> now uses plain labels and short explanations. “Mock source” is “Simulated Console”; Agent A and Agent B are “Earlier agent version” and “Newer agent version.” “Stale” is “Outdated reading,” with the 3-second threshold shown. The page explains that these clients are test scripts. Technical details start collapsed. A light/dark toggle saves the choice in the browser. The simulated Console page uses the same theme.

This UI update was checked locally at desktop and mobile sizes, including controls, compatibility cases, saved theme, and blocked browser storage. It does not change the message format or backend behavior. It was not deployed to Olares.

### Later phases

Phase 0 and Phase 1 do not enable live agent commands, connection approval, a whole-Console operator grant, or a digital-twin broadcast. Those features remain in later proposal phases. The portal currently receives mock data. The Olares tab receives real cached service observations.

Physical stage reference behavior still needs the owner's later hardware test. The robot and stage were disconnected during the idle deployment checks. The stage already reported a missing `/var/lib/mg400/stage/stage.toml` configuration before this change.
