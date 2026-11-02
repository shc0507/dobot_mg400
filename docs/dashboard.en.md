# Lab dashboard

[中文](dashboard.md)

The dashboard shows service status, the current run, its steps, and links to camera pages.
The existing optional `orch` service serves the page. See [deployment](deploy.en.md) and [recipe service configuration](orchestrator.en.md#http-service).
With the default configuration, open `http://127.0.0.1:8772/`.
If you configure `MG400_ORCH_BASE_PATH`, use that prefix. The optional proxy uses `/lab/`.

## Access and status

Enter `MG400_ORCH_TOKEN` in **Access token**, then select **Connect**.
The page keeps this token only in memory. Reloading the page or selecting **Forget token** clears it.
Do not put this token in page URLs. Downstream service tokens stay in the orchestrator process.

The page shell and its fixed styles and script do not require a bearer token. Host and Origin checks still apply.
Status and control APIs require the orchestrator token. Mutation requests also require a JSON object and a valid client identity.
Use the configured access path. See the [single-port proxy](olares.en.md#optional-single-port-proxy) for remote access.

Each service card shows connected, busy, locked, fault, lease holder, and demo status.
The cards cover the robot, stage, thermal camera, vision service, furnace, and microscope.
Missing values remain unknown. Unavailable or stale data does not prove that a device is healthy or stopped.
Server status older than 3 seconds is stale. The browser also marks its display stale when polling fails.
The service permits only 1 background state read per instrument at a time.

In demo mode, dashboard reads never start an instrument demo.
A card becomes available only after a requested run or recovery creates that owned demo.
Production endpoints and public camera links are not used in demo mode.

## Camera links

Configure camera page URLs explicitly in the service environment:

```sh
MG400_DASH_CAMERA_URL=
MG400_DASH_THERMAL_URL=
MG400_DASH_MICROSCOPE_URL=
```

The defaults are blank. A blank setting leaves its camera tile unavailable.
Use an HTTP(S) page URL that dashboard users may receive. The browser opens that page directly.
Do not include URL credentials, instrument API bearer tokens, query parameters, fragments, spaces, control characters, or backslashes.
The dashboard does not derive these URLs from private instrument endpoints or forward instrument credentials.
A camera page can require a secret path. Only a deliberately configured link is returned through the authenticated dashboard API.
The dashboard does not discover secret paths.
The camera page keeps its own access requirements.

## Stop controls

Keep the physical emergency stop reachable. A web request or a displayed confirmation is not a physical safety interlock.
These stop controls require authentication but do not require **Take control** or a control lease.
They remain separate from run controls:

| Button | Effect |
| --- | --- |
| **Stop all motion** | Sends independent stop requests to the robot, stage, and microscope in parallel. It also holds the active run and cancels recovery. Holding a run can stop its acquired heat, gas, and water outputs. |
| **Heat off** | Requires confirmation inside the page. It cancels the furnace sequence and stops heat and all furnace-controlled gas and water. It also holds the active run and cancels recovery. |
| **Gas off** | Requires confirmation inside the page. It independently requests gas-only stops for channels `0`, `1`, and `2`, without holding the run. An active sequence or recipe can apply gas again. Furnace fault handling can also stop other outputs. |

Read every target result. A target can be pending, confirmed, unconfirmed, unavailable, or failed.
For motion and heat stops, initial confirmations are provisional while the run hold and accepted work drain.
New control work remains blocked until that work drains and the service completes its final stop attempts.
An operation that finishes can still contain unconfirmed or failed targets.
Do not infer that all motion stopped from one successful target or an accepted request.
If a result is unknown, check the device and follow its recovery procedure before continuing.

## Run control and recovery

The **Current run** panel shows the run and its recorded steps.
Select **Take control** before **Hold run**, **Resume run**, or **Abort run**.
The page renews its existing lease only. After renewal fails, it requires another explicit **Take control** action.
Releasing or losing control holds an active run and cancels recovery.

**Hold run** stops progress and starts the stop and drain procedure.
**Abort run** ends the run after cleanup. It does not clear retained zone claims.
Neither action proves that an unknown device outcome is safe.

Before resuming a held run, read the recorded step status:

| Step status | Required action |
| --- | --- |
| Pending, never started | Do not perform this step manually. Acknowledge that it will execute after resume checks pass. |
| Started | Verify its physical outcome. Complete the step manually if necessary, then record the resolution. Resume skips this old step and does not replay it. |
| Unknown | Do not resume. Resolve the missing state first. |

Enter a nonempty **Resolution note** and select the acknowledgement for the displayed step.
If this run retains zones, verify physical clearance and select the separate clearance declaration.
Idle status alone does not establish physical clearance.
The service must still finish accepted work, confirm stops, and reconcile ownership and device health.
A refusal keeps the run held.

Use the [orchestrator recovery procedure](orchestrator.en.md#http-service) for terminal runs and retained zones.
The dashboard does not automatically resume a saved run, replay a step, reacquire an old run's leases, or clear unknown occupancy.

## Agent proposals

The **Agent proposals** section shows the requester, reason, expiry, full recipe definition, and effective parameters.
Treat this text as data. A requester cannot approve its own proposal.
Use the operator credential. The gateway credential has no dashboard decision authority.

1. Review every step and parameter.
2. Select **Take control**.
3. Confirm that you are in the lab and can reach a stop control throughout the run.
4. Select **Approve and run**, or select **Reject** without acquiring control.
5. Read the decision and current run status.

Approval starts through the same admission and lease checks as a manual run.
Keep this page connected and keep control. Lease expiry or release retains the existing run hold behavior.
A stale page cannot approve. Approval requires the displayed definition to match the current catalog and effective host limits.
The page sends each decision once and does not retry an uncertain result.
If dispatch fails, that approval stays consumed. Check the run before creating another proposal.
Proposal records disappear from the page after restart. Their audit remains saved, and no pending authority resumes automatically.
See the [gateway policy](agent-gateway.en.md#approval-policy) for supported steps and limits.

## Chip passport

Select **Chip passport** on the dashboard, or open `/chip` under the configured service prefix.
Enter the orchestrator token, then search with the chip's exact QR text.
Search preserves case, spaces, slashes, and punctuation. It does not trim or normalize the identifier.
For a direct link, use `/chip?id=` followed by the URL-encoded identifier.
The ordinary `/chip/<id>` route also opens the passport shell.
Do not include the access token in either URL.

The fixed passport shell and assets keep the dashboard's Host and Origin checks.
All history, search-data, and artifact requests require the bearer token.
The page keeps the token only in memory. It does not acquire a control lease or contact instruments.
The passport shows saved records only. An empty result does not prove that no physical work occurred.

Run summaries include runs associated with the exact chip identifier.
Within each run, steps, samples, events, and artifacts include only records explicitly associated with that chip.
Other chips' records are not included through a shared run.
The page loads 25 run summaries at a time. Each opened run loads 100 records per category at a time.
Use **Load more** to continue each list. The first page is not the complete history.

The passport shows recorded transfers, step parameters and results, samples, and events.
Its furnace PV chart uses explicit finite `pv_c` or `furnace.pv_c` values with recorded timestamps.
It does not infer temperatures from missing fields or create thermal readings that were not saved.
Missing chart data remains unavailable while other recorded data remains visible.
The server redacts private fields, known credentials, and configured private endpoint URLs in displayed records.
Invalid JSON payloads and payloads larger than 256 KiB show an unavailable marker.

Artifact requests use an opaque record ID and the exact chip identifier. They do not accept a filesystem path.
Only checked private regular files can be read. Symlinks, hard links, unsafe permissions, and changed paths or files cause refusal.
The read limit is 16 MiB per artifact. A refused read does not delete or rewrite the saved record.
The page previews PNG, JPEG, GIF, and WebP files identified by their bytes.
It fetches these files with authorization and uses temporary browser blob URLs for previews.
Other file types are available only as downloads. The browser never receives the stored filesystem path.
