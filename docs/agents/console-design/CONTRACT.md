# Lab Console contract (DSH-03)

This is the contract for the workspace tasks DSH-04 (Cameras), DSH-05 (Motion) and DSH-06 (Furnace + microscope). The shell, the relay and the Overview belong to DSH-03. Work in your own files. Do not edit the shell files. If the shell needs a change, say so in your PR body and keep the change small.

## 1. Entry and access

- The orchestrator service serves the console at `<base>/`. On Olares, `<base>` is `/lab`, and the proxy redirects `/` to `/lab/`.
- The DSH-01 dashboard moved to `<base>/dashboard/`. The passport pages stay at `<base>/chip/…`.
- The page, `<base>/console-static/<name>.js|css` and the dashboard shells are public. Every API route needs `Authorization: Bearer <MG400_ORCH_TOKEN>` when that token is set. The page asks the operator for this token and keeps it in memory only.
- Service tokens (`MG400_ORCH_<SERVICE>_TOKEN`) stay in the console process. No response contains them. The console sends no CORS headers.
- Every POST needs `Content-Type: application/json`, a JSON object body and `X-MG400-Client: <page id>`. The shell makes a random page id per page load (`console-page-<hex>`). Console control is bound to this page id.

## 2. Devices

The devices are the service names in `orchestrator.clients.SERVICES`: `robot`, `stage`, `thermal`, `vision`, `furnace`, `microscope`. The status strip also shows `cameras` (the camera preview link) and `runs` (the orchestrator).

## 3. HTTP API

All paths start with `<base>/api/console/`. Errors are `{"error": "<text>"}` with a 4xx or 5xx status.

| Method and path | Body | Response |
| --- | --- | --- |
| `GET state` | none | Frame state, see 3.1. Send `X-MG400-Client` to get `mine` flags. |
| `GET events` | none | `{"events": [Event…], "errors": {"<device>": "<text>"}}`, newest first, at most 500. |
| `GET describe/<device>` | none | `{"device", "commands": {"<action>": {"params": <JSON Schema>, "any_client": bool}}}` from the service `api/describe`, without lease actions. |
| `POST control/<device>/take` | `{}` | `200 {"control": Control}`. `409` if any other client holds the lease. `502` if the outcome is unknown. |
| `POST control/<device>/release` | `{}` | `{"device", "result": "released" \| "unconfirmed" \| "not-held"}`. `409` if another page holds it. |
| `POST heartbeat` | `{}` | `{"control": [Control…]}`. Renews this page's leases once each. |
| `POST release-page` | `{}` | `{"results": {"<device>": "released" \| "unconfirmed"}}` for this page. |
| `POST release-all` | `{}` | The same for every console lease. Other holders are not touched. |
| `POST command/<device>` | `{"action": "<name>", "params": {…}}` | `202 {"outcome": "accepted", "command_id"}`, `409 {"outcome": "refused", "error"}` or `502 {"outcome": "unknown", "error"}`. |
| `POST stop/<device>` | `{}` | `{"device", "outcome": "confirmed" \| "pending" \| "unconfirmed" \| "unknown"}` |

The DSH-01 routes stay: `GET <base>/api/dashboard/state`, `POST <base>/api/dashboard/motion` (`{}`), `heat` and `gas` (`{"confirmed": true}`), and the run routes in [orchestrator.md](../../orchestrator.md).

### 3.1 State

`GET state` returns the DSH-01 dashboard snapshot (`services`, `cameras`, `orchestrator`, `run`, `operations`, `approvals`) and these keys:

```json
{
  "console": {"commit": "c6d5f14", "demo": false},
  "control": [{"device": "stage", "holder": "console-control-…", "mine": true, "state": "held", "message": null, "held_s": 12.5}],
  "readiness": [{"id": "gas-map", "state": "blocked", "detail": "…", "workspace": "process"}],
  "workspaces": [{"id": "overview", "title": {"en": "Overview", "zh": "总览"}}]
}
```

- A service card has `id`, `phase` (`unconfigured`, `loading`, `ready`, `stale`, `unavailable`), `connected`, `busy`, `locked`, `fault`, `lease_holder` and `demo`. A card is `stale` 3 s after its last read.
- `readiness[].state` is `ready`, `todo` or `blocked`. The ids are `tool-offset`, `camera-calibration`, `stage-reference`, `focus-zero`, `gas-map` and `cutoff`.
- An Event is `{"service", "seq", "ts_unix", "level", "message"}`. `service` is a device name or `runs`.

### 3.2 Relay rules

- The relay forwards only an action that the service lists in `api/describe`. It never forwards `start_sync`, `stop_sync`, `heartbeat`, `renew` or `release`.
- A command needs this page's control, unless the service marks it `any_client`. Then the relay uses a fresh identity and no lease.
- The service validates `params` against its schema. The relay checks only that `params` is an object.
- The relay sends a command once. A transport failure, a timeout or a 5xx is `unknown`. Show it to the operator. Do not retry it.
- Stop needs no control, never waits for a lease and is sent once.

### 3.3 Lease rules

- Take control creates a fresh service identity and calls the service `api/heartbeat` once. The service refuses it if another client holds the lease. The console never takes a lease over.
- If the take outcome is unknown, the console releases that fresh identity once.
- Each page heartbeat renews each of the page's leases once (`api/renew`). The shell sends a heartbeat every 500 ms while the page holds control.
- If no heartbeat arrives for 3 s, the console releases the lease. On `pagehide` the shell sends `release-page` with `keepalive`.
- If a renewal fails, the session ends as `lost`. The console does not release or reacquire it.
- The robot client keeps its own WebSocket heartbeat while it is connected. The console ends that connection after 3 s without a browser heartbeat.

## 4. Browser API for workspaces

The shell loads `shell.js` first and then one script per workspace, all with `defer`. Register on load:

```js
window.LabConsole.register({
  id: "motion",
  i18n: { en: { "jog.title": "Jog" }, zh: { "jog.title": "点动" } },
  mount(root, api) { /* Build the workspace in root once, on the first visit. */ },
  show() {}, hide() {},  // Optional: called when the tab is shown or hidden.
});
```

The `api` object:

| Member | Use |
| --- | --- |
| `api.relay(device, action, params)` | Promise of `{outcome, command_id?, error?}`. Shows a notice for `unknown`. |
| `api.stop(device)` | Promise of the stop result. Needs no control. |
| `api.stopAll()` | The DSH-01 stop fan-out (robot, stage, microscope). |
| `api.take(device)`, `api.release(device)`, `api.releaseAll()` | Control. `take` rejects with the service message on refusal. |
| `api.owns(device)`, `api.controls()` | Whether this page holds a device; the control list. |
| `api.describe(device)` | Promise of the described commands. |
| `api.state()`, `api.fresh()`, `api.onState(fn)` | The latest state, whether it is less than 3 s old, and a listener (returns an unsubscribe function). The shell polls every 1 s. |
| `api.t(key, vars)`, `api.lang()`, `api.onLanguage(fn)` | Strings from your `i18n`, then the shell. `{name}` fills `vars`. The language is `en` or `zh`. Re-render in `onLanguage`. |
| `api.notify(text, level)` | The page notice. `level` is `info`, `warn` or `error`. |
| `api.request(path, data, options)` | A call to your own routes at `<base>/api/console/ws/<id>/<path>`. With `data` it is a POST. |
| `api.consoleRequest(path, data)` | A call to any console API path that starts with `/api/`. |
| `api.el(tag, props, ...children)` | Make an element. `text` sets text content; `on<event>` adds a listener. Never use `innerHTML` with data. |
| `api.deviceName(id)` | The localized device name. |

## 5. Files

| Path | Owner |
| --- | --- |
| `src/orchestrator/web/console.py`, `console_routes.py` | DSH-03 (relay, leases, events, readiness) |
| `src/orchestrator/web/templates/console.html`, `static/console/shell.*`, `overview.*` | DSH-03 |
| `src/orchestrator/web/workspaces/__init__.py`, `overview.py` | DSH-03 |
| `src/orchestrator/web/workspaces/<id>.py`, `static/console/<id>.js`, `<id>.css` | The workspace task. `motion`: DSH-05, `cameras`: DSH-04, `process`: DSH-06. |
| `tests/test_console_<id>.py`, `tests/test_console_<id>.mjs` | The workspace task. |

Replace the placeholder files for your workspace. Prefix every CSS rule with `#ws-<id>`. Use the shell classes (`lc-card`, `lc-button`, `lc-danger`, `lc-row`, `lc-muted`, `lc-layout`, `lc-col-main`, `lc-col-side`) and colors from `shell.css`. Follow the boards in this folder.

## 6. Server routes for a workspace

`workspaces/<id>.py` defines `WORKSPACE`. The console finds it without a shared list.

```python
from flask import jsonify

from . import Workspace


def register(blueprint, service):
    # Mounted at <base>/api/console/ws/motion. The operator token is checked for you.
    @blueprint.get("/stations")
    def stations():
        return jsonify(stations=[])


WORKSPACE = Workspace(
    id="motion", order=20, title={"en": "Motion", "zh": "运动"}, register=register
)
```

- Use `service.console` for device access: `service.console.command(device, page, action, params)`, `service.console.stop(device)`, `service.console.describe(device)`. Read the page id from the `X-MG400-Client` header.
- Do not add a device command or a second lease path. Do not send a service token to the browser.
- A relay for streams (DSH-04) adds its own routes here and bounds its streams.

## 7. Tests

- Python: copy the `rig` fixture and the `Device` fake from `tests/test_console.py`. They use `client_factory`, a fake clock and no bound ports. `rig.client` is a Flask test client with `/lab` as the base.
- Node: `tests/test_console.mjs` loads `shell.js` with `require` and a small fake DOM. Load your workspace script after it with `globalThis.window = globalThis` and a stub `LabConsole`, or test your pure functions.
- Run `uv run pytest -q tests/test_console*.py` and `node --test tests/test_console*.mjs`.

## 8. Run offline

```sh
MG400_ORCH_BASE_PATH=/lab uv run python -m orchestrator serve --demo --recipes-dir ./recipes --port 8790
```

Open `http://127.0.0.1:8790/lab/`. In demo mode, Take control starts that device's owned demo service (robot, stage, thermal, furnace or vision) on the first use. The microscope has no owned demo here. Without `MG400_ORCH_TOKEN` the console accepts only a loopback Host.

## 9. Camera tiles and the stream relay (DSH-04)

### 9.1 Browser: `window.LabCameras.tile(view, options)`

`static/console/cameras.js` loads on every console page. It defines `window.LabCameras` when it runs, before `DOMContentLoaded`. Call it in `mount` or later, not at script load: `motion.js` loads before `cameras.js`.

```js
const node = window.LabCameras
  ? window.LabCameras.tile("camera2", { api, large: true, onSelect(view) {}, onExpand(view) {} })
  : placeholderTile();  // Your own OFFLINE tile.
root.append(node);
```

- `view` is `camera1` to `camera4`, `thermal` or `microscope`. Another value throws.
- `options.api` is your workspace `api`. Pass it. `large` makes the image area 16:10. `onSelect` runs on a click or Enter on the image. `onExpand` replaces the default Expand action (full screen).
- The tile is a `<figure class="lcam-tile">` with the name and role, the LIVE, STALE, OFFLINE or FROZEN badge with the frame age, the mode, and Snapshot and Expand buttons. Camera 4 shows the chip detections and the thermal tile shows the minimum, maximum and center markers and the colour bar when the overlays are on.
- The tile streams only while it is visible: connected, not in a hidden workspace, and the browser tab is visible. It checks once a second. It stops the stream when it is hidden or removed, so the relay ends. Remove the node to drop the tile.
- Tile styles are in `cameras.css` with the `lcam-` prefix and no `#ws-cameras` prefix, so tiles work in every workspace. Size the tile with your own container.

### 9.2 Server routes

All paths start with `<base>/api/console/ws/cameras/`. They need the operator token, except the two ticket routes.

| Method and path | Response |
| --- | --- |
| `GET status` | `{"views": [{"id", "configured", "state": "live" \| "stale" \| "offline", "age_s", "streams", "error", "mode"}], "limits", "ticket_ttl_s"}` |
| `POST ticket` `{"view", "kind": "stream" \| "snapshot"}` | `{"url", "expires_s"}`. Only for `camera1` to `camera4` and `microscope`. |
| `GET stream/<view>?ticket=…` | The MJPEG stream (`multipart/x-mixed-replace`). `503` at the stream bound. `502` if the source fails. |
| `GET snapshot/<view>?ticket=…` | One JPEG as an attachment. |
| `GET thermal/frame` | The thermal frame: `sequence`, `timestamp`, `stats`, `temperatures_c` ([y][x] in °C) and `age_s`. `?full=1` adds `raw`, `ir` and `raw_unit`. |
| `GET thermal/regions` | `{"regions": {"<name>": {"x0", "y0", "x1", "y1"}}}` in sensor pixels. |
| `GET detections/4` | `{"available", "image_size", "detections": [{"label", "confidence", "xyxy"}]}` from the vision service. |

- **Tickets.** An `<img>` cannot send the token. The page sends `POST ticket` with the token and puts the returned `url` in `img.src`. A ticket is random, used once, bound to one view and one kind, and expires after 20 s. Any use spends it. A ticket route accepts no other query parameter. The console never puts the token or the camera key in a URL.
- **Bounds.** At most 6 relayed streams in total, 2 per view, and 2 for the microscope (its service allows 4; 2 stay for the microscope page). The orchestrator serves with 16 worker threads, so open streams leave workers for the API.
- **Relay end.** When the viewer closes the connection, the console closes the upstream connection and frees the slot. When the upstream ends or fails, the stream ends and the tile asks for a new ticket after a back-off of 1 s to 15 s.
- **States.** A view is `live` when its last frame is at most 2 s old, `stale` when it is older, and `offline` when no stream is open for 5 s, the source failed, or no frame came. The thermal state comes from the frame timestamp.
- **Sources.** camera1 to camera4 come from the camera preview at `MG400_ORCH_CAMERA_URL` with the key in `MG400_CAMERA_KEY_FILE`. The thermal, vision and microscope views use the `MG400_ORCH_<SERVICE>_URL` and `_TOKEN` settings (the demo services in demo mode). Tests attach fake sources as `service.camera_sources`.
- **Thermal commands.** The region editor sends `set_region` (`{"name": "crucible", "x0", "y0", "x1", "y1"}`) and the shutter button sends `shutter` (`{}`) through `api.relay("thermal", …)`, each after an in-page confirmation. The buttons are disabled until the thermal `api/describe` lists the command.
