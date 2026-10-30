# Task cards

One card per entry in `tasks.toml`. The first line under each heading repeats the owner, size, review level and dependencies from `tasks.toml`; `tests/test_migration_plan.py` checks that they agree. The write set of each task is only in `tasks.toml`.

- Procedures named `P-...` are in README section 8.
- "Ask first" items need an answer from the owner before the work starts.
- "Source" names the WuLab files to move, at the commits in README section 3.
- Every card's "Done when" includes green CI, even when it does not say so.

## Phase 0: foundation

### H-01 · Approve the plan and answer the open questions

Owner: human · Size: S · Review: human · Deps: none

Goal: accept this plan so that agents can start.

Steps:

1. Read `README.md` and skim this file.
2. Check the answers in README section 12. Answer Q-02, Q-05 and Q-06 on the map issue when they are known.
3. Change what you disagree with in a plan PR, or ask an agent to.
4. Merge the plan PR.

Done when: the plan is merged and every open question has an answer or an owner.

### GOV-01 · Create the tracking issues

Owner: agent · Size: S · Review: human · Deps: H-01

Goal: GitHub issues become the status board, with the dependency graph from `tasks.toml`.

Read: `docs/agents/issue-tracker.md`, `docs/agents/triage-labels.md`, `tasks.toml`.

Steps:

1. Create missing labels: `wayfinder:map`, `wayfinder:task`, `ready-for-agent`, `ready-for-human`, `needs-info`, and `mig:<track>` for every track in `tasks.toml`.
2. Create the map issue "Lab integration migration" with the sections Notes, Decisions so far (README section 2) and Fog (the open questions without an answer), and a link to this folder.
3. In `tasks.toml` order, create one issue per task. Title: `[<ID>] <title>`. Body: a link to the card, owner, size, review level, dependencies by id. Labels: `wayfinder:task`, `mig:<track>`, and `ready-for-agent` (owner `agent`) or `ready-for-human` (owner `human` or `agent+human`).
4. Add each issue as a sub-issue of the map.
5. Add a native blocked-by link for every dependency.
6. Put a table from task id to issue number in the map issue body.

Done when:

- The map has one sub-issue per task in `tasks.toml`. A comment on the map shows both counts.
- Every dependency has a blocked-by link. Spot-check five tasks and list them in the comment.

Notes: use a throwaway script outside the repo. Do not commit issue numbers. Cloud sessions may have no `gh`; then use the GitHub tools that the session has.

### FND-01 · Rename mg400_agent to mg400

Owner: agent · Size: M · Review: human · Deps: none

Goal: D3. The package name still says "agent" though the agent code is gone. Every later task imports the new name.

Read: `AGENTS.md`, `pyproject.toml`, and the output of `rg -l mg400_agent -g '!.agents/**'` (32 files and 100 hits at `c25ccc9`).

Steps:

1. `git mv src/mg400_agent src/mg400`.
2. Replace `mg400_agent` with `mg400` in every listed file: code, tests, `scripts/mg400_robot.py`, `scripts/mg400_control_mode.py`, `apps/control_panel/server.py`, `AGENTS.md`, `docs/development.md`. Do not touch `.agents/`.
3. `pyproject.toml`: `name = "mg400"`, `mg400-robot = "mg400.terminal_control:main"`, wheel `packages = ["src/mg400", "src/stage_controller"]`.
4. `uv lock`, then `uv sync`.

Done when:

- `rg mg400_agent -g '!.agents/**'` finds nothing.
- P-CHECKS pass.
- `uv run mg400-robot --help` and `python3 apps/control_panel/server.py --help` print their help.

Docs: the code table in `docs/development.md` (Chinese only; DOC-05 adds the pair) and the paths in the `AGENTS.md` area table.

Notes: the systemd units start scripts and console scripts by path, so they need no change. `deploy/mg400ctl` imports only `deploy/install.py`. The installer's `uv sync` puts the renamed package into the Olares `.venv`.

### FND-02 · Require Python 3.12

Owner: agent · Size: S · Review: agent · Deps: FND-01

Goal: Olares runs Python 3.12 and the moved thermal package needs it. One supported version keeps CI simple.

Read: the output of `rg -n "3\.11" -g '!uv.lock' -g '!.agents/**'`.

Steps:

1. `pyproject.toml`: `requires-python = ">=3.12"` and ruff `target-version = "py312"`.
2. `.github/workflows/ci.yml`: matrix `python: ["3.12"]`. Update the comment above it.
3. `deploy/install.py`: version check `(3, 12)` and its message. Update `tests/test_deploy_install.py`.
4. Docstrings in `apps/control_panel/server.py` and `scripts/mg400_robot.py`.
5. Docs: `development.md`, `control-panel.md`, `hardware.md`, `deploy.md`, `terminal-control.md` (Chinese only; the pairs come later).
6. `uv lock`. Run `uv run ruff check .` and fix new findings without changing behavior.

Done when: the `rg` command finds nothing; P-CHECKS pass.

### FND-03 · Block large and binary files

Owner: agent · Size: S · Review: agent · Deps: none

Goal: the WuLab repos carry DLLs, model weights and large images. A test keeps them out of Git.

Steps:

1. Add `tests/test_repo_files.py`. List tracked files with `git ls-files -z` from the repo root. Skip with a reason when Git or `.git` is missing.
2. Fail when a file is larger than 1,000,000 bytes, except `uv.lock`.
3. Fail on these extensions: `.dll .so .dylib .exe .bin .pt .pth .onnx .engine .npz .h5 .zip .stp`.
4. Fail when an image (`.png .jpg .jpeg .bmp .tif .tiff .webp`) is larger than 300,000 bytes.
5. The failure message names the file and where such files belong: the asset manifest (FND-08), host data under `/var/lib/mg400`, or a release.

Done when: the test passes; a temporary 2 MB tracked file makes it fail. Say so in the PR body and do not commit that file.

Notes: `uv.lock` grows when VIS-03 adds PyTorch. It is generated text, so it is exempt.

### DOC-01 · Bilingual docs rule and pair check

Owner: agent · Size: M · Review: human · Deps: FND-02

Goal: D9. Every human doc exists in Chinese and English, and CI catches drift between the two.

Steps:

1. Naming rule: the unsuffixed file `X.md` is Chinese; `X.en.md` beside it is English.
2. Scope: `README.md`, `docs/*.md`, `docs/adr/*.md`, `firmware/*/README.md`. Exempt: `AGENTS.md`, `docs/agents/**`, `.agents/**`, `GLOSSARY.md` (one bilingual table), `tests/fixtures/**`.
3. Add `scripts/check_doc_pairs.py` (stdlib only) and `tests/test_doc_pairs.py`, which runs it on the repo. For each file in scope:
   - Its pair must exist, unless the file is listed in `docs/agents/migration/doc-pairs-pending.txt` (one repo-relative path per line). A missing pending file counts as empty.
   - Compare the pair: number and order of fenced code blocks, and their lines after removing comments that start with `#` or `//`; the set of inline code spans; the set of ASCII numbers outside code; the set of link targets without `#fragments`, where `X.md` and `X.en.md` count as the same target; the number of headings at each level.
   - Print each mismatch with both file names and the differing items.
4. Seed `doc-pairs-pending.txt` with every in-scope file that lacks a pair today: `README.md`, the seven `docs/*.md` files, `firmware/three-motor-demo/README.md`.
5. `AGENTS.md`, section Documenting: add the rule. Change both files in the same PR. Write Chinese with jianming-zhongwen and English with asd-ste100. The pair check runs in pytest. Agent docs stay English only.
6. Unit tests with temporary files: one passing and one failing case per comparison rule.

Done when: P-CHECKS pass.

Notes: write numbers as digits in both languages. The check ignores code comments, so they may be translated or kept.

### GOV-02 · Write the ADRs and the glossary

Owner: agent · Size: M · Review: human · Deps: DOC-01

Goal: record D1 to D9 and the plan defaults where `docs/agents/domain.md` says decisions live.

Steps:

1. Create `docs/adr/` with these ADRs. Each is `NNNN-slug.md` (Chinese) plus `NNNN-slug.en.md` (English), has the parts Status (Accepted, date), Context, Decision and Consequences, and fits on one page.
   - 0001 Instrument service contract v1 (README 4.3).
   - 0002 Repository layout and moved code (D2, D3, README 4.1, P-MOVE).
   - 0003 One host: Olares (D1, README 4.2, the USB layout from OPS-01).
   - 0004 Large files and assets (D4, FND-08).
   - 0005 Furnace without a client, restart and shutdown (D5, FUR-03).
   - 0006 Vision runtime and GPU use (D6, VIS-03, VIS-05).
   - 0007 Bilingual docs (D9, DOC-01).
   - 0008 LLM agents behind approval gates (D8, AGT-01, AGT-02).
   - 0009 Web framework for new services (README 2.3).
2. `GLOSSARY.md`: one table with the columns Term, 术语, Definition, 定义. Terms: chip (the lab also says wafer, Q-01), crucible, furnace window, tray, slot, station, load position, pose, zone, lease, recipe, skill, run, rehearsal, service, contract, control plane, tool offset, calibration, asset, homing, soft limit, clearance position.

Done when: the pair check passes; every README 2.3 row names an ADR that exists.

Notes: the cards disappear in CLN-02, so each ADR must state its decision in full, not by reference to a card.

### DOC-02 · English README

Owner: agent · Size: S · Review: agent · Deps: DOC-03, DOC-04, DOC-05, DOC-06, DOC-07, DOC-08, DOC-09

Goal: the entry page in English, after the pages it links to exist.

Steps: P-TRANSLATE (below) for `README.md`.

Done when: the pair check passes for the file; every link in `README.en.md` resolves.

#### P-TRANSLATE

Used by DOC-02 to DOC-09:

1. Translate the Chinese file one to one into `X.en.md`: same headings in the same order, same tables, same code blocks, same numbers.
2. Links in the English file point to `.en.md` pages with English anchors. A target page may not exist yet; its DOC task creates it in the same wave.
3. Make `[English](X.en.md)` the first line of the Chinese file and `[中文](X.md)` the first line of the English file.
4. Remove the file from `doc-pairs-pending.txt`.
5. Write the English with the asd-ste100 skill.

### DOC-03 · English control-panel guide

Owner: agent · Size: M · Review: agent · Deps: DOC-01

Steps: P-TRANSLATE for `docs/control-panel.md`.

Done when: the pair check passes for the file.

### DOC-04 · English deployment guide

Owner: agent · Size: M · Review: agent · Deps: DOC-01

Steps: P-TRANSLATE for `docs/deploy.md`.

Done when: the pair check passes for the file.

### DOC-05 · English development reference

Owner: agent · Size: M · Review: agent · Deps: DOC-01

Steps: P-TRANSLATE for `docs/development.md`. Keep every JSON field name, status code, close code and command name exactly as in the Chinese file.

Done when: the pair check passes for the file.

### DOC-06 · English hardware reference

Owner: agent · Size: M · Review: agent · Deps: DOC-01

Steps: P-TRANSLATE for `docs/hardware.md`. Keep the code blocks; their comments may stay Chinese.

Done when: the pair check passes for the file.

### DOC-07 · English Olares guide

Owner: agent · Size: M · Review: agent · Deps: DOC-01

Steps: P-TRANSLATE for `docs/olares.md`. Keep host paths and commands exactly.

Done when: the pair check passes for the file.

### DOC-08 · English stage-panel guide

Owner: agent · Size: M · Review: agent · Deps: DOC-01

Steps: P-TRANSLATE for `docs/stage-panel.md`.

Done when: the pair check passes for the file.

### DOC-09 · English terminal guide

Owner: agent · Size: S · Review: agent · Deps: DOC-01

Steps: P-TRANSLATE for `docs/terminal-control.md`.

Done when: the pair check passes for the file.

### DOC-10 · Chinese firmware README

Owner: agent · Size: S · Review: agent · Deps: DOC-08

Goal: the stage firmware README follows the naming rule. It is English today.

Steps:

1. `git mv firmware/three-motor-demo/README.md firmware/three-motor-demo/README.en.md`.
2. Write the Chinese `README.md` with the jianming-zhongwen skill, one to one, with the language links on the first lines.
3. In `docs/stage-panel.md` and `docs/stage-panel.en.md`, drop the "（英文）" note on the firmware link and link each language to its pair.
4. Remove the entry from `doc-pairs-pending.txt`.

Done when: the pair check passes for the file.

### FND-04 · Shared access checks (labkit.auth)

Owner: agent · Size: M · Review: human · Deps: FND-02

Goal: one implementation of the token, Host, Origin and forwarded-header rules for every service.

Read: `apps/control_panel/server.py` (search for `Forwarded`, `Host`, `token`, `Origin`), `src/stage_controller/app.py` (`_host_port`, `loopback_name`, `loopback_host`, `protect_local_control`), `src/stage_controller/__main__.py` (the token rule at startup), and the auth tests from `rg -n "def test_.*(token|host|forward|origin)" tests/`.

Steps:

1. `src/labkit/__init__.py` and `src/labkit/auth.py`, stdlib only. Functions: token validation (printable ASCII, no leading or trailing space, at least 24 characters when binding a non-loopback address), loopback bind check, Host check, forwarded-header check, bearer check with `hmac.compare_digest`, Origin check.
2. The robot server and the stage app call these functions. Every status code and message stays the same.
3. Add `src/labkit` to the wheel packages.
4. Unit tests for each function. An `AGENTS.md` area row for labkit.

Done when: the existing auth tests pass unchanged; P-CHECKS pass.

Notes: the robot service is stdlib-only, so `labkit.auth` imports only the standard library.

### FND-05 · Service contract kit (labkit)

Owner: agent · Size: L · Review: agent · Deps: FND-04, GOV-02

Goal: the shared parts of contract v1, so each service needs little code.

Read: README 4.3, ADR-0001, `src/stage_controller/app.py` (error handling), `src/stage_controller/controller.py` (hold timing).

Steps:

1. `labkit/lease.py`: `Lease(seconds, clock)` with acquire, renew, release, holder, expired, and `poll()`, which calls an expiry callback once. No threads inside.
2. `labkit/events.py`: `EventLog(maxlen=1000)` with `add(level, message, **fields)` returning a sequence number, `since(seq)`, and the same line written through `logging`.
3. `labkit/describe.py`: command and parameter descriptions as JSON Schema dicts, and a `describe(...)` builder.
4. `labkit/flask.py`: `contract_blueprint(service, *, token, base_path, lease)` with the routes and errors from README 4.3. POST routes need `Content-Type: application/json` and `X-MG400-Client`. Only this module imports Flask.
5. `labkit/client.py`: `ServiceClient(base_url, token, client_id)` with describe, state, command, stop, heartbeat, release, events, and a heartbeat thread helper. It uses urllib and raises `ServiceError(status, message)`.
6. `tests/contract_helpers.py`: conformance checks that every service in demo mode must pass: describe fields, state fields, stop from any client, lease conflict 409, expiry callback, error bodies.
7. Tests for every module with a fake clock. Update the `AGENTS.md` labkit row.

Done when: P-CHECKS pass; a test shows that every labkit module except `labkit.flask` imports only the standard library.

### FND-06 · Contract endpoints on the robot service

Owner: agent · Size: M · Review: human · Deps: FND-05, DOC-03, DOC-05

Goal: the robot service speaks contract v1 beside its current protocol, so the orchestrator and the dashboard treat it like the other services.

Read: `apps/control_panel/server.py` (`do_GET`, `do_POST`), `src/mg400/web_control.py` (`RobotPanelState.snapshot`, `command`, the logging calls), `docs/development.md` section 机器人服务接口.

Steps:

1. `RobotPanelState` keeps an `EventLog` and adds an event wherever the panel already writes a journal line: state messages, locks, alarms, keyboard close reasons, gripper locks. Add no journal lines.
2. Add `last_command` to the state: id, action, client, result (`ok`, `failed`, `rejected`, `stopped`), message, finished_at. Fill it when a command ends.
3. `server.py`: `GET {base}api/describe` (contract 1, the commands of the docs table; `stop` and `disable` are any-client; `keyboard` is marked UI-only), `POST {base}api/stop` (any client; `wait_s` 0 to 15; reports confirmed, pending or unconfirmed), `GET {base}api/events?since=`.
4. The new POST route follows the existing POST rules: JSON, `X-MG400-Panel: 1`, token.
5. `api/state`, `api/command`, `api/heartbeat` and `api/ws` stay unchanged.
6. Tests: the access rules apply to the new routes; stop works without control; events filter by `since`; `last_command` for success, rejection and stop.

Done when: P-CHECKS pass; the existing tests pass unchanged.

Docs: `development.md` and `.en.md`, the service interface tables.

### FND-07 · Python client for the robot service

Owner: agent · Size: M · Review: human · Deps: FND-06

Goal: scripts, notebooks and the orchestrator control the robot through the service, with the lease and the checks the page has.

Read: `tests/panel_ws.py` (`WsClient`), `src/mg400/websocket.py`, the WebSocket section of `docs/development.md`, `tests/test_robot_service_e2e.py`.

Steps:

1. `src/mg400/client.py`: `RobotClient(url, token, client_id)` on the `mg400.websocket` frame helpers. Client frames are masked.
2. API: `connect`, `close`, `state` (the latest push), `command(action, **fields)` returning the reply, `wait(predicate, timeout)`, and `run(action, timeout=..., **fields)`, which waits for `busy == False` and checks `last_command`.
3. A heartbeat thread every 0.5 s while the client holds control. Reconnect within 3 s after a dropped socket. Stop the thread on close.
4. HTTP fallback for stop and disable when the socket is down.
5. Context manager: on exit with control, run `stop_sync` unless `keep_control=True`.
6. Tests with the real `PanelServer` and the simulated controller rig: wrong token (close 4401), protocol mismatch (4426), command and reply, `run` waits for completion, control survives a 5 s wait, closing the client lets the lease expire and the service stops the arm, stop over HTTP while the socket is down.

Done when: P-CHECKS pass.

Docs: `development.md` and `.en.md`, a short section "Python 客户端" / "Python client" with an example.

### FND-08 · Asset manifest and installer download

Owner: agent · Size: M · Review: human · Deps: FND-02, DOC-04

Goal: D4. Large files get one manifest, one download path and a hash check, like go2rtc today.

Read: `deploy/install.py` (`Host`, `--go2rtc`, the step order), `tests/test_deploy_install.py`, `deploy/mg400ctl`.

Steps:

1. `config/assets.toml`, one table per asset: `url`, `sha256`, `bytes`, `install` (absolute path), `mode`, `service`, `source`, `license`. First entry `vision-weights`: 23,393,117 bytes, SHA-256 `0b684c470191540b0cfc8802b0d13149b47d7be76b901ad1dd59882297407a07` (from Visual-Localization `models/download_manifest.json`), the URL of asset `chip-arm-yolo26s-seg-v1.pt` in release `vision-model-v1` of this repo, install path `/var/lib/mg400/models/chip-arm-yolo26s-seg-v1.pt`, license AGPL-3.0 per Roboflow.
2. `deploy/assets.py` (stdlib): load and validate the manifest; download to a temporary file in the target directory, check size and SHA-256, rename atomically, set owner and mode; install from a local path with the same checks; report status (present, missing, mismatch).
3. `install.py`: `--asset NAME=PATH` installs from a local file (the offline path, like `--go2rtc`); `--fetch-assets` downloads. FND-09 later limits both to enabled services. Dry run lists the steps. Refuse symbolic links like the other paths.
4. `mg400ctl assets` prints each asset's status.
5. Tests with a local HTTP server or a fake opener: size mismatch, hash mismatch, offline path, permissions, dry run.

Done when: P-CHECKS pass.

Docs: `deploy.md` and `.en.md`: the manifest, the download, the offline path through Olares Drive (link to the bundle procedure in `olares.md`).

### FND-09 · Service registry and labctl

Owner: agent · Size: M · Review: human · Deps: FND-08

Goal: adding a service becomes one registry entry instead of edits in five places.

Read: `deploy/mg400ctl` (`SERVICES`), `deploy/install.py`, `tests/test_systemd_units.py`, `tests/test_deploy_mg400ctl.py`.

Steps:

1. `deploy/services.toml`: one entry per service with `name` (the `mg400ctl` name), `units`, `optional`, `venv` (`.venv` or `.venv-vision`), `groups` (uv dependency groups), `token` (settings key or empty), `port`, `assets`.
2. `install.py` and `mg400ctl` load it. The `SERVICES` table in `mg400ctl` and the `SERVICES` tuple in `test_systemd_units.py` come from it.
3. New setting `MG400_OPTIONAL_SERVICES` (comma list, empty by default). The installer syncs groups, installs assets and udev rules only for the listed optional services. The existing services are not optional.
4. Install `/usr/local/bin/labctl` as a second link to `deploy/mg400ctl`. The help text names both.
5. Tests for the loader, unknown names, and a host without optional services (behavior unchanged).

Done when: P-CHECKS pass; the installer dry run on a test host prints the same steps as before for the existing services.

Docs: `deploy.md` and `.en.md`: the registry, the new setting, `labctl`.

### FND-10 · Contract and control lease on the stage service

Owner: agent · Size: M · Review: human · Deps: FND-05, DOC-05, DOC-08

Goal: the orchestrator can drive the stage, and the stage panel cannot move a motor while automation holds the stage.

Read: `src/stage_controller/app.py`, `controller.py`, `static/app.js`, the stage interface section of `docs/development.md`.

Steps:

1. Add the labkit contract blueprint beside the existing routes. Map commands to the controller: move, hold, keepalive, release_hold, stop_all, set_microstep.
2. While a client holds the lease, the existing motion routes return 409 "Stage is controlled by automation", except stop. The page shows a banner and disables the motion buttons.
3. Lease expiry stops all motors.
4. Write the controller's activity log entries to an `EventLog` too.
5. Tests with the `tests/stage_helpers.py` fakes and `tests/contract_helpers.py`.

Done when: P-CHECKS pass; the existing stage tests pass unchanged.

Docs: `stage-panel.md` and `.en.md` (the banner), `development.md` and `.en.md` (the stage interface).

### SIM-01 · Runnable MG400 simulator

Owner: agent · Size: L · Review: agent · Deps: FND-02

Goal: run the real robot service against a simulated controller for rehearsals and demos.

Read: `tests/sim_mg400.py` (`Controller`, `Connection`, `Feed`, `Collector`), `src/mg400/feedback.py` (frame format), and the commands the robot code sends: `rg -o '[A-Z][A-Za-z]+\(' src/mg400/joint_control.py src/mg400/keyboard_jog.py | sort -u`.

Steps:

1. `src/mg400/sim/`: the controller model (move the shared logic out of `tests/sim_mg400.py` so that tests and the simulator use one copy), TCP servers on ports 29999, 30003 and 30004 of a loopback address, feedback frames at the controller's period, and a gripper collector that writes the feedback file and executes command files.
2. `python -m mg400.sim --host 127.0.0.2 --gripper-feedback <file>`. Refuse non-loopback addresses.
3. `tests/sim_mg400.py` keeps working and imports the shared model.
4. Tests: start the simulator on a free loopback address, run `PanelServer` with `--robot-host` on it, then `start_sync`, `enable`, `execute`, `grip`, `stop_sync`.

Done when: P-CHECKS pass; the documented manual demo works.

Docs: `development.md` and `.en.md`: running the panel against the simulator.

## Phase 1: vision

### VIS-01 · Move the chip and arm localizer

Owner: agent · Size: M · Review: agent · Deps: FND-02, FND-03

Source: Visual-Localization `visual_localization/predict.py`, `visual_localization/test_coordinates.py`.

Goal: the YOLO chip and arm detector lives in `src/vision` and also returns chip outlines and angles.

Steps (P-MOVE plus):

1. `src/vision/localizer.py` from `predict.py`. Keep `Detection`, `load_image`, `map_detections`, `draw_detections`, `Localizer`, `annotate_image`.
2. Weights come from the `weights` argument or `MG400_VISION_WEIGHTS`. Remove the search in the module folder. A missing file gives a clear error. Never download.
3. Masks: when the model returns masks (`prediction.masks.xy`), add `polygon` (original-image pixels, mapped from the 640 × 640 stretch like the boxes), `angle_deg` and `size_px` from `cv2.minAreaRect`. Boxes stay as they are.
4. Replace the Windows font path with `ImageFont.load_default(size=...)`.
5. Set `YOLO_CONFIG_DIR` from `MG400_VISION_CACHE_DIR` (default `/var/lib/mg400/vision/yolo`).
6. Importing `vision` must not need torch or ultralytics. Keep the ultralytics import inside `Localizer`.
7. `pyproject.toml`: group `vision` with numpy, pillow, zxing-cpp and opencv-python-headless, in versions compatible with the source repos; add it to the default groups; add `src/vision` to the wheel packages; `uv lock`.
8. `test_coordinates.py` becomes `tests/test_vision_localizer.py`. Add tests for polygon mapping and angles with synthetic masks and no model.

Done when: P-CHECKS pass in an environment without torch.

Notes: `run_demo.py` and the download helpers stay behind (README section 3).

### VIS-02 · Move the QR reader

Owner: agent · Size: M · Review: agent · Deps: VIS-01

Source: Chemistry-Integration `Identify_Wafer/qr_detector.py`, `qr_micro_recovery.py`, `tests/test_qr_detector.py`, `tests/test_micro_recovery.py`, `tests/test_photo_detection.py`, and `sample_img/*.jpg` as crops.

Goal: QR and Micro QR detection with corner coordinates in `src/vision`.

Steps (P-MOVE plus):

1. `src/vision/qr.py` from `qr_detector.py` (`QRCode`, `read_image`, `detect_qr_codes`, `results_csv`, `DetectionCancelled`, the CLI as `python -m vision.qr`). `src/vision/qr_micro.py` from `qr_micro_recovery.py`.
2. Do not move `qr_gui.py`, `ui_theme.py` or `tests/test_qr_gui.py`.
3. Fixtures: run the detector on each sample photo at the source commit. Crop each decoded label with a margin of twice its size, at the original pixel density. Save PNG files in `tests/fixtures/qr/`: each at most 300 KB, together at most 1 MB. Name each file's source photo and crop box in the commit message.
4. Adapt `test_photo_detection.py` to the crops. Move the synthetic tests unchanged.

Done when: P-CHECKS pass; the crop tests decode the labels that the source README reports for the sample photos (four `Stamp`, `2D`, `2C`).

### VIS-03 · GPU runtime for vision

Owner: agent · Size: S · Review: human · Deps: VIS-01

Goal: a locked runtime for the detector on the RTX 5090, with one OpenCV package and no change to CI.

Steps:

1. Group `vision-gpu`: `ultralytics>=8.4,<9`, `torch>=2.7`, `torchvision>=0.22`. Take torch and torchvision from an explicit uv index `pytorch-cu128` at `https://download.pytorch.org/whl/cu128`.
2. One OpenCV: drop the `opencv-python` requirement of ultralytics with `[tool.uv] override-dependencies = ["opencv-python; sys_platform == 'never'"]`, so that `opencv-python-headless` from the `vision` group provides `cv2`. Check with `uv tree --group vision-gpu` that `opencv-python` is absent. If uv rejects the override, stop and report. The fallback is a separate uv project for vision.
3. Do not add `vision-gpu` to the default groups.
4. `uv lock`. Check that `uv sync --locked` with the default groups downloads no torch.

Done when: `uv lock --check` passes; P-CHECKS pass.

Notes: the RTX 5090 needs PyTorch 2.7 or later built for CUDA 12.8. Older builds fail with "no kernel image is available for execution on the device".

### VIS-04 · Vision service

Owner: agent · Size: L · Review: agent · Deps: VIS-02, FND-05

Goal: one service that finds chips, the arm and QR labels in live camera frames.

Steps (P-SERVICE plus):

1. `src/vision/frames.py`: a go2rtc source that reads `http://127.0.0.1:1984/api/frame.jpeg?src=cameraN` with a timeout and decodes it, and a demo source that reads images from `tests/fixtures/vision/`. go2rtc stays the only owner of the cameras.
2. `src/vision/service.py`: one inference at a time. Commands `detect` (camera) and `read_labels` (camera, optional region). State: model loaded, device, last result summary, calibrated cameras.
3. Read routes beside the contract: `GET api/detect?camera=N`, `GET api/labels?camera=N`, `GET api/annotated.jpg?camera=N`. Results carry pixels, plus robot millimetres when a calibration exists (CAL-01).
4. GPU: `MG400_VISION_DEVICE` (default `cuda` when available, else `cpu`) and `MG400_VISION_GPU_FRACTION` (default 0.15), applied with `torch.cuda.set_per_process_memory_fraction` before the model loads. Load the model and run one warm-up inference at start.
5. Demo mode: a fake localizer that returns detections from a fixture JSON file. No torch needed.
6. Default bind loopback, port 8771.
7. Tests in demo mode with `tests/contract_helpers.py`; QR on the VIS-02 fixtures.

Done when: P-CHECKS pass.

Docs: `docs/vision.md` and `.en.md`: what the service does, settings, routes, limits (pixel coordinates need CAL-01).

### VIS-05 · Deploy vision on Olares

Owner: agent · Size: M · Review: human · Deps: VIS-03, VIS-04, FND-09, H-13, H-14

Goal: the vision service runs on Olares with the GPU and the released weights.

Steps (P-DEPLOY plus):

1. When `vision` is in `MG400_OPTIONAL_SERVICES`, the installer creates `/opt/mg400/.venv-vision` with `UV_PROJECT_ENVIRONMENT=/opt/mg400/.venv-vision uv sync --locked --no-default-groups --group vision --group vision-gpu`, installs the `vision-weights` asset and creates `/var/lib/mg400/vision`.
2. Unit `mg400-vision.service` with `ExecStart=/opt/mg400/.venv-vision/bin/python -m vision`. Allow `.venv-vision` paths in `tests/test_systemd_units.py`.
3. `mg400ctl gpu` runs `nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv` as UID 1000.
4. Settings `MG400_VISION_BIND`, `_PORT`, `_TOKEN`, `_WEIGHTS`, `_DEVICE`, `_GPU_FRACTION`.
5. A registry entry with the asset and the venv.

Done when: P-CHECKS pass; the installer dry run shows the vision steps only when vision is enabled.

Docs: `deploy.md` and `.en.md`, `olares.md` and `.en.md`.

Notes: the owner runs the first install and start on Olares and checks `mg400ctl gpu`.

### VIS-06 · Detection overlay in the camera page

Owner: agent · Size: M · Review: agent · Deps: VIS-04, DOC-07

Goal: operators see what vision sees, on the existing camera page.

Read: `scripts/camera_preview.py` (the routes under the key path), `scripts/camera_preview.js`, `tests/test_camera_preview.py`, `tests/test_camera_preview.mjs`.

Steps:

1. `camera_preview.py`: a route `/<key>/camera<N>/detections.json` that asks the vision service on loopback (token from `MG400_VISION_TOKEN`) with a 2 s timeout, and returns `{"available": false}` on any error.
2. The preview unit must keep `MG400_VISION_TOKEN`. Adjust its `UnsetEnvironment` line and the unit test.
3. `camera_preview.js`: an overlay switch per camera. Draw boxes and outlines (chip `#18b85a`, arm `#ed8a24`, the colors of the source model) with labels. Poll once per second while the tile is visible. Keep the current pause rules.
4. Tests: the Python route with a fake vision server; the drawing and polling logic in the node tests.

Done when: P-CHECKS pass; the stream behaves the same when vision is down.

Docs: `olares.md` and `.en.md`, camera section.

## Phase 2: manipulation

### ROB-01 · Speed per command with a cap

Owner: agent · Size: M · Review: human · Deps: FND-07

Goal: unattended transfers need more than `SpeedL=1,AccL=1`, under a hard cap that only the host settings raise.

Read: `src/mg400/web_control.py` (`execute`, `_targets`), `src/mg400/joint_control.py` (`move_linear`), the command table in `docs/development.md`.

Steps:

1. `execute` accepts an optional integer `speed` and sends `SpeedL=<speed>,AccL=<min(speed, acceleration cap)>` with the MovL. Without `speed` the command is unchanged (`SpeedL=1,AccL=1`).
2. Caps from `MG400_ROBOT_MAX_SPEED_L` and `MG400_ROBOT_MAX_ACC_L` (default 1, maximum 100). A value above the cap returns 400.
3. Do not change `home`, the `SpeedFactor` handling or keyboard jog.
4. Tests for validation, the MovL text and the unchanged default.

Done when: P-CHECKS pass.

Docs: `control-panel.md` and `.en.md`, `development.md` and `.en.md`, `deploy/mg400.env.example`.

Notes: H-17 validates on hardware before MS-01 uses a speed above 1.

### ROB-02 · Grasp outcome in the robot state

Owner: agent · Size: M · Review: human · Deps: ROB-01

Goal: recipes branch on whether the gripper holds the object, without parsing messages.

Read: `src/mg400/web_gripper.py` (outcomes `arrived` and `contact`, the state register values), `src/mg400/gripper_feedback.py`, the USB gripper section of `docs/development.md`.

Steps:

1. Keep the outcomes `arrived` and `contact`. After `contact`, watch the gripper state. State 3 (object dropped, PGEA register `0x0201`) sets the outcome to `dropped` and adds an event.
2. State fields: `gripper.outcome` (null, `arrived`, `contact`, `dropped`) and `gripper.outcome_at`.
3. The page shows the outcome next to the gripper status.
4. Tests with the simulated collector.

Done when: P-CHECKS pass.

Docs: `control-panel.md` and `.en.md`, `development.md` and `.en.md`.

Notes: H-17 checks the three outcomes on hardware.

### ORC-01 · Orchestrator core with rehearsal

Owner: agent · Size: L · Review: human · Deps: FND-05, FND-07, SIM-01

Goal: one place that runs recipes across devices, holds safely on failure, records everything and rehearses without hardware.

Steps:

1. `src/orchestrator/recipe.py`: recipes are TOML files in `recipes/`. A recipe has `name`, `version`, `params` and `[[steps]]` with `skill`, its parameters, `timeout_s` and `on_fail` (`hold` by default, or `abort`). Check unknown skills, missing parameters and types before the run starts.
2. `src/orchestrator/skills/`: a registry from skill name to a function `(ctx, **params)`. `ctx` gives the service clients, the run store and the zone manager.
3. `src/orchestrator/runner.py`: runs the steps in order. On a failure or timeout with `hold`, it stops motion on every service it controls, marks the run `held` and waits for `resume` or `abort` from a person. It never retries motion.
4. `src/orchestrator/store.py`: SQLite at `/var/lib/mg400/runs/runs.db` (path from settings) with runs, steps, artifacts and samples. Every row about a sample has a `chip_id`. Run files go to `/var/lib/mg400/runs/<run-id>/`.
5. `src/orchestrator/clients.py`: the robot client (FND-07) and labkit clients for the other services, from `MG400_ORCH_*` settings. The orchestrator holds each service's lease during a run.
6. `src/orchestrator/rehearsal.py`: starts the services in demo mode on free loopback ports (the robot service with SIM-01) and runs a recipe against them. A time scale shortens simulated waits.
7. CLI: `labctl run <recipe>`, `labctl rehearse <recipe>`, `labctl runs`.
8. `recipes/example-rehearsal.toml` and a test that rehearses every recipe in `recipes/`.

Done when: P-CHECKS pass; in rehearsal, a failing step holds the run and stops the simulated arm.

Docs: `docs/orchestrator.md` and `.en.md`. An `AGENTS.md` area row.

### ORC-02 · Stations and tool offset

Owner: agent · Size: M · Review: human · Deps: ORC-01

Goal: named robot poses at the fingertip, taught once and used by every recipe.

Steps:

1. `/var/lib/mg400/tool.toml`: `offset_mm = [dx, dy, dz]`, measured in H-08.
2. `src/orchestrator/tool.py`: convert between flange and fingertip poses. Fingertip XY = flange XY + (dx, dy) rotated by R. Fingertip Z = flange Z − dz. Pure functions with round-trip tests.
3. `/var/lib/mg400/stations.toml`: per station `x_mm`, `y_mm`, `z_mm`, `r_deg` (fingertip), `approach_mm`, optional `via` (station names), `zone`, `saved_at`.
4. `src/orchestrator/stations.py`: load, validate (workspace limits from settings, `approach_mm` at least 10), save atomically with mode 0640.
5. CLI: `labctl station save NAME --approach MM [--zone Z]` reads the measured pose from the robot service state (someone, for example the panel, must hold control) and converts it with the tool offset. Also `list`, `show`, `rm`.
6. Tests with fake robot states.

Done when: P-CHECKS pass.

Docs: `orchestrator.md` and `.en.md`: teaching stations, the tool file.

Notes: the robot keeps `User=0,Tool=0`. The offset is applied in software, so no controller tool setting changes.

### ORC-03 · Transfer skill

Owner: agent · Size: M · Review: human · Deps: ORC-02, ROB-02

Goal: pick at one station and place at another, with every check the hardware allows.

Steps:

1. Skills `robot.pick(station, grip)`, `robot.place(station, release)`, `robot.transfer(from, to, grip, release)`.
2. Pick: enabled and idle; gripper initialized; rise to the safe height at the current XY; travel at the safe height through the via stations; open to the pre-grip width; descend; close; require outcome `contact`; rise; require that the outcome did not become `dropped`.
3. Place: travel at the safe height; descend; open; require outcome `arrived`; rise.
4. Every move uses `execute` with the capped speed and waits for completion through the client.
5. Take each station's zone before entering and release it after leaving (the ORC-04 interface; a no-op manager until ORC-04 lands).
6. Any unexpected state, fault or timeout holds the run.
7. Rehearsal tests with SIM-01, including an empty grasp and a drop.

Done when: P-CHECKS pass.

Docs: `orchestrator.md` and `.en.md`.

### ORC-04 · Zones and cross-device interlocks

Owner: agent · Size: M · Review: human · Deps: ORC-01, FND-10

Goal: two devices never move into the same space, and the conditions between devices hold.

Steps:

1. Zones from settings: `tray`, `crucible-load`, `furnace-tube`. Each has an owner device and entry conditions. Only the stage may hold `furnace-tube`. The robot never goes under the microscope.
2. `src/orchestrator/zones.py`: acquire and release with the holder's name. A second holder waits, then fails after a timeout.
3. Entry conditions are functions over service states. Examples: the robot enters `crucible-load` only when the stage is homed and the Insert axis is idle at `load` and held awake. The Insert axis moves only when the robot does not hold `crucible-load`.
4. The runner checks the conditions before each step that declares a zone and again after it.
5. Tests with fake states for each condition and for a lease loss during a step.

Done when: P-CHECKS pass.

Docs: `orchestrator.md` and `.en.md`: zones and their conditions.

### ORC-05 · Orchestrator service on Olares

Owner: agent · Size: M · Review: human · Deps: ORC-01, FND-09

Goal: recipes run in a service instead of a terminal, and the dashboard and the gateway get a backend.

Steps (P-SERVICE and P-DEPLOY plus):

1. `src/orchestrator/app.py`: the contract routes plus `GET api/recipes`, `POST api/runs` (recipe and params), `GET api/runs`, `GET api/runs/<id>`, `POST api/runs/<id>/resume`, `POST api/runs/<id>/abort`.
2. One run at a time, in a worker thread.
3. Unit `mg400-orchestrator.service`, port 8772, settings `MG400_ORCH_*` (service URLs and tokens, data paths).
4. On SIGTERM: hold the current run (stop motion), then exit. A run never ends without a recorded state.

Done when: P-CHECKS pass.

Docs: `orchestrator.md` and `.en.md`, `deploy.md` and `.en.md`.

### OPS-03 · Backup and host migration for new data

Owner: agent · Size: S · Review: human · Deps: FND-09, DOC-04

Goal: a host move keeps stations, calibrations, runs and furnace state.

Steps:

1. `mg400ctl backup FILE.tar` archives `/etc/mg400/mg400.env` and `/var/lib/mg400` without `locks/`, with owners and modes. It refuses to overwrite a file.
2. Docs: add the new data paths (README 4.2) to "迁移到新主机" and "Migrate to a new host", with the restore steps by hand.
3. Tests with a fake root.

Done when: P-CHECKS pass.

### VIS-07 · Camera modes and full-resolution stills

Owner: agent · Size: M · Review: human · Deps: FND-09, H-09, DOC-07

Goal: the tray camera gives enough pixels for Micro QR labels.

Ask first: the mode of each camera and the still method, from H-09.

Steps:

1. Setting `MG400_CAMERA_MODES`: per camera `WIDTHxHEIGHT@FPS`, default `640x480@30`. `render_go2rtc` uses it. Accept only the modes that H-09 lists.
2. Implement the still method that H-09 chose: a full-resolution stream for the tray camera, or a separate still capture. Add `still(camera)` to `src/vision/frames.py`.
3. Tests and docs.

Done when: P-CHECKS pass.

Notes: aim for about 4 pixels per module, about 70 pixels across a 17-module Micro QR label.

### CAL-01 · Camera-to-robot calibration

Owner: agent · Size: L · Review: human · Deps: ORC-02, VIS-04, H-08, H-09

Goal: pixel positions from a fixed camera become robot millimetres on the tray plane.

Steps:

1. `src/vision/calibration.py`: fit a homography from image points to robot XY with `cv2.findHomography` (RANSAC, threshold 2 px). Validate on held-out points and report the RMS error in millimetres. Store JSON in `/var/lib/mg400/calibration/cameras/`, keyed by the camera's `/dev/v4l/by-path` name: matrix, plane height, image size, points, RMS, time.
2. Refuse a calibration when the live image size differs from the stored one.
3. `src/orchestrator/skills/calibrate.py`: the robot carries the ArUco tag from H-08 to a 3 × 4 grid between two taught stations at the plane height. At each point: wait until still, take 5 frames, detect the tag center, take the median. Fit with 9 points, validate with 3. Accept when the RMS is at most 1.0 mm (setting).
4. The vision service loads the calibrations and adds `robot_mm` to detections.
5. Tests with synthetic points from a known homography, outliers included.

Done when: P-CHECKS pass; the routine runs in rehearsal.

Docs: `vision.md` and `.en.md`: when and how to recalibrate.

Notes: the homography is valid only for the plane at the calibration height. Plugging the camera into another USB port invalidates its file, because the key is the port path.

### CAL-02 · Tray model

Owner: agent · Size: M · Review: agent · Deps: CAL-01, VIS-02

Goal: slot positions from the tray's two corner QR codes, as a check on the taught stations and on detections.

Steps:

1. `config/trays/<name>.toml` from H-08: the text of each corner QR code, its position in the tray frame, and each slot's position and label area in millimetres.
2. `src/vision/tray.py`: find both corner codes, map their centers to robot XY with the calibration, fit a similarity transform, and return every slot in robot coordinates.
3. Cross-check each slot against its taught station and, when a chip is present, against the YOLO chip center. Report the residuals and fail above 2 mm (setting).
4. Tests with synthetic images.

Done when: P-CHECKS pass.

### CAL-03 · Calibration drift check

Owner: agent · Size: M · Review: human · Deps: CAL-01

Goal: stop vision-guided motion when a camera has moved.

Steps:

1. Project the fingertip position from the robot state into the image with the inverse homography.
2. Check that it falls inside the detected "Robotic Arm" box, with a margin from settings.
3. Skill `vision.check_calibration(camera)`. The transfer recipe calls it before each vision-guided move. A failure holds the run with "camera moved, recalibrate".
4. Tests with synthetic boxes.

Done when: P-CHECKS pass.

Notes: perspective makes this a coarse check. It catches a moved camera, not a small error.

### VIS-08 · Evaluate and retrain the detector

Owner: agent+human · Size: L · Review: human · Deps: VIS-05, VIS-07, H-22

Goal: know how well the detector works on the final cameras, and retrain it when needed.

Steps:

1. `scripts/vision_eval.py`: run the model on the labeled set from H-22 (outside Git). Report precision and recall per class at the deployed confidence, and the center errors in pixels and millimetres.
2. The owner sets the targets on the issue.
3. If a target fails: `src/vision/train.py` wraps ultralytics training on the GPU. The dataset and outputs stay outside Git.
4. New weights get a release `vision-model-vN` (the H-14 procedure) and an `assets.toml` change in a PR.

Done when: the evaluation report is on the issue; the manifest points to the new weights if the model changed.

### MS-01 · Milestone: QR-verified tray transfer

Owner: agent+human · Size: M · Review: human · Deps: ORC-03, ORC-04, ORC-05, CAL-02, CAL-03, VIS-05, H-17

Goal: D7. The first end-to-end automated task.

Steps:

1. `recipes/tray-transfer.toml` with the params `from_slot`, `to_slot`, `expect_label`.
2. Skills in `src/orchestrator/skills/vision.py`: `vision.find_chip(slot)` (present, center, angle) and `vision.verify_label(slot, expect)`.
3. Steps: check the calibration, verify the label at the source, find the chip, transfer with R from the chip angle, verify the label at the target.
4. Rehearsal tests for success and for each failure: no chip, wrong label, empty grasp, drop, moved camera. Each failure holds the run.
5. The operator runs it on hardware with P-HW.

Done when: three supervised transfers in a row succeed with the label verified at both ends (the owner may change the count); the results are on the issue.

Docs: `orchestrator.md` and `.en.md`: running it.

## Phase 3: hot side

### OPS-01 · USB device table and udev rules

Owner: agent · Size: M · Review: human · Deps: FND-09, H-03, DOC-07

Goal: about a dozen USB devices on one host get stable names, access rules and a layout that survives a hub reset.

Steps:

1. `config/udev/*.rules.in` templates with `@SERVICE_GROUP@`. The installer replaces it with the group name of GID 1000 (`getent group 1000`), installs the rules of enabled services as `/etc/udev/rules.d/70-mg400-<name>.rules` (root, 0644), then runs `udevadm control --reload-rules` and `udevadm trigger --subsystem-match=usb`.
2. `tests/test_udev_rules.py`: every template has a VID and PID, `MODE="0660"` and the group placeholder.
3. Installer tests for the dry run and the rendering.
4. Docs: a USB section in `olares.md` and `.en.md` with the device table from H-03 (device, VID:PID, stable path, hub and port, service) and the layout rules: the Toupcam camera on its own USB 3 controller, powered hubs by job, never reset a shared upstream hub.

Done when: P-CHECKS pass.

### THM-01 · Move the thermal camera package

Owner: agent · Size: M · Review: agent · Deps: FND-02, FND-04

Source: Chemistry-Integration `Thermal_Cam_Control/thermal_cam/**` (`app.py`, `camera.py`, `service.py`, `__main__.py`, `__init__.py`, `static/`, `_vendor/` with `LICENSE-p3-ir-camera`), `THIRD_PARTY_NOTICES.md`, `tests/*.py`.

Goal: the thermal camera driver, acquisition service and dashboard live in `src/thermal_cam`.

Steps (P-MOVE plus):

1. Copy the package unchanged in the first commit. Adapt it in the following commits of the same PR.
2. Keep `_vendor/LICENSE-p3-ir-camera`. Copy `THIRD_PARTY_NOTICES.md` to `src/thermal_cam/_vendor/NOTICE.md`. The Apache-2.0 license requires both.
3. Group `thermal` with fastapi, uvicorn, numpy, pyusb and libusb-package, in the default groups; httpx in the dev group; script `thermal-cam = "thermal_cam.__main__:main"`.
4. Default port 8768. Settings `MG400_THERMAL_BIND`, `_PORT`, `_TOKEN`. With a token, a bearer-check middleware built on `labkit.auth`. Without a token, keep the loopback-only behavior.
5. Keep the demo mode and the static dashboard.
6. Tests become `tests/test_thermal_*.py`.

Done when: P-CHECKS pass without the camera.

Docs: `docs/thermal.md` and `.en.md` from the source README: Linux setup instead of Zadig, and no hardware verification record.

### THM-02 · Thermal service on Olares

Owner: agent · Size: M · Review: human · Deps: THM-01, OPS-01, H-05

Goal: the thermal camera runs as a service on Olares.

Steps (P-DEPLOY plus):

1. `config/udev/70-mg400-thermal.rules.in` for USB `3474:45c2`.
2. Unit `mg400-thermal.service` with `ExecStart=/opt/mg400/.venv/bin/thermal-cam`. Reading the camera moves nothing, so the service connects at start as it does today.
3. A registry entry `thermal` (optional) and the settings in the env example.

Done when: P-CHECKS pass.

Docs: `thermal.md` and `.en.md`, `deploy.md` and `.en.md`.

Notes: H-19 validates on hardware.

### THM-03 · Thermal contract and region readings

Owner: agent · Size: M · Review: agent · Deps: THM-01, FND-05

Goal: the orchestrator reads "how hot is the crucible" through the contract.

Steps:

1. Contract routes (describe, state, stop as a no-op, events) in the FastAPI app. Write a small FastAPI adapter around the labkit lease and event classes; the Flask blueprint does not apply.
2. `/var/lib/mg400/thermal/rois.toml`: named regions in sensor coordinates (x is the column and y the row, as the source README defines them, not the rotated display).
3. `GET api/roi?name=` or with `x0,y0,x1,y1`: max, mean and min in °C, frame sequence and age. Return 503 when the frame is stale.
4. Tests with the demo camera.

Done when: P-CHECKS pass.

Docs: `thermal.md` and `.en.md`: regions, units, staleness, warm-up and shutter advice.

### FUR-01 · Move the furnace controller code

Owner: agent · Size: M · Review: human · Deps: FND-02, FND-03

Source: FurnaceCode `furnace.py` (the module helpers, `MFCFlowController`, `Furnace`) and their tests in `test_flow_controller.py`.

Goal: the AI-808 and MFC drivers live in `src/furnace`, configured by settings instead of COM-port constants.

Steps (P-MOVE plus):

1. `src/furnace/controller.py`: register constants, connect, disconnect, read and write helpers, `MFCFlowController`, `Furnace`. `FurnaceService` moves in FUR-03.
2. `src/furnace/config.py`: `MG400_FURNACE_SERIAL`, `MG400_FURNACE_SLAVE` (default 1), `MG400_MFC_SERIAL`, `MG400_FURNACE_MAX_C` (default 1000, never above 1000), `MG400_FURNACE_MAX_RAMP` (default 100, never above 100). No COM defaults.
3. Every function that takes a port uses it. Remove the module-global port changes (at `ebbf84b`, the `port` arguments of `FurnaceService.connect` and `connect_flow_controller` are ignored).
4. Group `furnace` with minimalmodbus and pyserial, in the default groups.
5. Move the controller tests to `tests/test_furnace_controller.py`.
6. Do not move `test_gas.py`.

Done when: P-CHECKS pass; no test opens a serial port.

Notes: the source README cites a Yudian protocol PDF that is not in the repo. H-10 puts it on the lab share.

### FUR-02 · Furnace and MFC simulator

Owner: agent · Size: L · Review: agent · Deps: FUR-01

Goal: a simulator good enough for tests, demo mode and rehearsals.

Steps:

1. `src/furnace/sim.py`: a fake serial object that minimalmodbus can use, and a fake for the MFC channel protocol. Derive the protocol from `MFCFlowController._exchange`, `_read_command`, `_write_command` and the existing test fakes.
2. AI-808 model: the registers that `controller.py` reads and writes (SV, PV, run state, STEP, PAF, PNO, program data), RUN, HOLD and STOP as `furnace.py` expects, and a first-order temperature model with the ramp limit.
3. MFC model: three channels (units 7, 8, 9) and the solenoids through the valve unit (4), with setpoints and readback.
4. A time-scale parameter and an injectable clock.
5. Tests that drive `Furnace` and `MFCFlowController` against the simulator.

Done when: P-CHECKS pass.

### FUR-03 · Furnace run logic: gas per step, client loss, restart

Owner: agent · Size: L · Review: human · Deps: FUR-02, GOV-02

Source: FurnaceCode `furnace.py` (`FurnaceService`) and its tests in `test_flow_controller.py`.

Goal: D5 and a restart rule, tested in simulation.

Steps:

1. `src/furnace/service.py` from `FurnaceService`. Keep the ramp, HOLD and hold-timer behavior.
2. Steps get an optional `gas` table (channel to percent). The sequence gets `end_gas` and `end_gas_until_c`, required when any step sets gas. Gas is applied at the start of each step. Readback is recorded; missing readback is "unverified", not an error.
3. Client loss (lease expiry): a sequence runs to its planned end, which already ends in STOP/COOLING, then applies `end_gas` until PV is below `end_gas_until_c`. An open-ended setpoint (`apply_setpoint`) starts a timer of `MG400_FURNACE_ORPHAN_GRACE_MIN` (default 15). When it ends, the service stops heating and applies `MG400_FURNACE_IDLE_GAS` (default all off). Commands other than stop wait until a client takes the lease.
4. `src/furnace/state.py` writes `/var/lib/mg400/furnace/state.json` atomically at each phase change and every 60 s during a hold: steps, index, phase, hold start (wall clock), elapsed time, gas plan, orphan time.
5. Startup: read the controller. RUN or HOLD with a matching state file (target within 2 °C): resume and add an event. RUN or HOLD without a matching file: stop heating, apply the idle gas, lock until a person acknowledges. STOP: idle.
6. Service stop (SIGTERM): keep the FurnaceCode behavior, which stops heating and gas. FUR-04 makes `mg400ctl` refuse to stop the furnace during a sequence without `--force`.
7. Apply the caps from `config.py` to every setpoint and ramp.
8. Tests with the simulator and a fake clock: a lease loss mid-hold completes the run; the grace stop; restart and resume; restart with a mismatch locks; caps; gas per step and the end gas.

Done when: P-CHECKS pass.

Notes: H-10 supplies the gas map and the defaults. Until then, gas commands refuse to run when `MG400_FURNACE_GAS_MAP` is unset.

### FUR-04 · Furnace service and page on Olares

Owner: agent · Size: L · Review: human · Deps: FUR-03, FND-05, FND-09

Source: FurnaceCode `app.py` (furnace, flow and sequence routes), `templates/index.html`, `static/app.js`, `static/styles.css`, `README.md`, `FURNACE_CLASSES.md`.

Goal: operators keep their furnace page, now on Olares, and the orchestrator gets the contract.

Steps (P-SERVICE and P-DEPLOY plus):

1. `src/furnace/app.py`: the contract blueprint and the existing page routes (`/`, `/api/status`, `/api/furnace/*`, `/api/flow/*`, `/api/sequence*`). The page routes call the same service object. The camera and recording routes come in FUR-05; hide those page parts until then.
2. Manual page commands work when no automation lease is held. Stop always works.
3. Unit `mg400-furnace.service` with `SupplementaryGroups=dialout`, port 8769, the settings from FUR-01 and FUR-03.
4. `mg400ctl` refuses `stop` or `restart` of `furnace` while a sequence runs, unless `--force`.
5. Tests in demo mode with `tests/contract_helpers.py`, and page route tests.

Done when: P-CHECKS pass.

Docs: `docs/furnace.md` and `.en.md` (operator guide from the source README and `FURNACE_CLASSES.md`, Linux only), `deploy.md` and `.en.md`.

Notes: H-20 validates on hardware before the first real heating.

### TPC-01 · One Toupcam camera package

Owner: agent · Size: M · Review: agent · Deps: FND-02, FND-03

Source: FurnaceCode `camera.py`, `toupcam_capture.py`, `python/toupcam.py`, `test_camera_recording.py`; Objective-Control `Objective_Control/camera.py` (the `MicroscopeCamera` part), `Objective_Control/toupcam_capture.py`.

Goal: one `MicroscopeCamera` with the features of both copies.

Steps (P-MOVE plus):

1. `src/toupcam_camera/camera.py`: one `MicroscopeCamera` with the FurnaceCode features (time-lapse recording, recording mode, interval and fps, segments, free-disk stop, stabilization) and the Objective-Control features (frame ids, capture times, `get_frame_packet`).
2. `src/toupcam_camera/capture.py`: `ToupcamCapture` from Objective-Control, which sets the fastest readout.
3. `src/toupcam_camera/toupcam.py`: the vendor wrapper, version 59.30594.20260120, unchanged except LF line endings.
4. Remove the `preview()` functions that use `cv2.imshow`.
5. Select a camera by index or serial number from settings. Open nothing at import.
6. Group `camera` with numpy and opencv-python-headless, in the default groups.
7. Tests: `test_camera_recording.py` adapted, and a fake SDK for frame packets and time-lapse.

Done when: P-CHECKS pass without the native library.

Notes: the wrapper loads `libtoupcam.so` from its own folder first, then from the system path. TPC-02 installs the library outside the repo and sets `LD_LIBRARY_PATH`. The `USBCamera` class moves in MIC-02.

### TPC-02 · Toupcam library and USB access on Olares

Owner: agent · Size: M · Review: human · Deps: TPC-01, FND-08, OPS-01, H-12

Goal: the services that use a Toupcam camera find the native library and may open the device.

Steps:

1. Installer option `--toupcam PATH` installs the library from H-12 as `/usr/local/lib/mg400/libtoupcam.so` (root, 0755), like `--go2rtc`.
2. `config/udev/70-mg400-toupcam.rules.in` with the VID and PID from H-06, or the vendor's rules from H-12.
3. The units that use the camera (furnace, microscope) set `Environment=LD_LIBRARY_PATH=/usr/local/lib/mg400`. Allow it in the unit tests.
4. Tests and docs.

Done when: P-CHECKS pass.

### STG-01 · Stage firmware: limit switches, homing, keep-awake

Owner: agent+human · Size: L · Review: human · Deps: DOC-10

Goal: the Insert axis carries the crucible into the furnace, and the stage moves the chip under the furnace window for the microscope (Q-03). The position of every axis must be known after every reset, and no axis may leave its safe travel.

Read: `firmware/three-motor-demo/*`, `src/stage_controller/protocol.py` (what the host parses).

Steps:

1. `MotorPins.h`: per motor `LimitMin` and `LimitMax` pins (defaults A0 and A1 for Insert, A2 and A3 for Bottom, A4 and A5 for Top; 255 means none). `INPUT_PULLUP`, active low, 5 ms debounce. Static checks against pin conflicts.
2. `MotorControl`: refuse motion toward an active limit. Stop at once, without a ramp, when the limit in the motion direction becomes active, in finite and hold modes.
3. `home` (selected motor): move toward the minimum limit at the homing speed, back off 200 steps, approach slowly, set position 0 and `homed=1`. Fail with `ERROR: home failed: <reason>` after a step limit or a timeout.
4. `soft <min> <max>` (selected motor, steps from home): accepted only when homed. Finite moves that end outside the range fail with `ERROR`. Hold mode stops at the edge of the range.
5. After a reset or power-up, `homed=0` and no soft range for all motors. A microstep change does the same. A new `home` clears the soft range of its motor.
6. `awake <ms>` (selected motor): keep the driver awake for up to 60000 ms after the last motion. Renewable. `awake 0` ends it.
7. Status lines append ` homed=<0|1> limits=<min><max> soft=<0|1> awake=<0|1>`. Every existing field keeps its order.
8. Every existing command and reply stays unchanged.
9. Update `README.md` and `README.en.md`: wiring, commands, status format.

Done when: the sketch compiles for the board in use (report the Arduino CLI or IDE command and its output).

Notes: the operator uploads and validates in H-07 and H-18. STG-02 keeps the panel working with the old firmware.

### STG-02 · Stage host: homing, named positions, keep-awake

Owner: agent · Size: L · Review: human · Deps: STG-01, FND-10

Goal: the stage service knows positions in millimetres and refuses automated moves before homing.

Steps:

1. `protocol.py`: parse `homed`, `limits`, `soft` and `awake`; commands `home`, `soft` and `awake`; `DemoTransport` simulates limit switches, homing and soft ranges.
2. `/var/lib/mg400/stage.toml`, with values from H-07:
   - Per motor: `steps_per_mm` at a given microstep, and the soft limits `min_mm` and `max_mm` from home.
   - Named positions of one axis in millimetres from home. Insert: `load` and `furnace`.
   - Named poses: a position for each of the three axes, reached one axis at a time in the listed order. The imaging poses put the chip under the furnace window. The pose `furnace` is the inserted position from which `crucible.unload` starts.
3. `controller.py`: `home(motor)`, `awake(motor, seconds)`, `goto(motor, name)`, `pose(name)`. After `home`, send `soft` with the soft limits of that motor. `goto` and `pose` need `homed` on every axis they move, refuse unknown names and targets outside the soft limits, and convert with the microstep in use. Without homing, automated moves (`goto`, `pose` and contract commands) fail with "position unknown, home first".
4. Routes `POST /api/motors/<m>/home`, `/awake`, `/goto`, `POST /api/poses/<name>`, and the matching contract commands.
5. Page: a homed badge per axis, a Home button, named position buttons for Insert, pose buttons.
6. With the old firmware, show homing as unavailable and keep manual control.
7. Tests and docs.

Done when: P-CHECKS pass.

Docs: `stage-panel.md` and `.en.md`.

### XDV-01 · Crucible load and unload skills

Owner: agent · Size: L · Review: human · Deps: ORC-03, ORC-04, STG-02, FUR-04, THM-03

Goal: move a chip between the tray and the crucible with every interlock between robot, stage, furnace and thermal camera.

Steps:

1. `crucible.load(from_slot)`: the furnace is in STOP/COOLING with PV below `load_max_c`; the stage is homed; Insert is at `load` and held awake; the robot takes zone `crucible-load`, transfers to station `crucible/load`, leaves and releases the zone; Insert moves to `furnace`.
2. `crucible.unload(to_slot)`: no sequence is running and the furnace is in STOP/COOLING; Insert moves to `load` and stays awake; the thermal region `crucible` reads below `unload_max_c` (wait with a timeout, then hold); the robot takes the zone, picks, leaves and places in the slot.
3. Limits come from settings and the recipe. No default allows a heat-related move on hardware.
4. Rehearsal tests for success and for each failed condition.

Done when: P-CHECKS pass.

Docs: `orchestrator.md` and `.en.md`.

### MS-02 · Milestone: furnace cycle with the crucible

Owner: agent+human · Size: M · Review: human · Deps: XDV-01, THM-02, FUR-04, MS-01, H-18, H-19, H-20, H-11

Goal: the full hot-side loop once, supervised.

Steps:

1. `recipes/furnace-cycle.toml`: verify the label, load, run a short low-temperature sequence (the owner sets the temperature and time; inert gas only), wait for completion, unload after the thermal check, verify the label.
2. Rehearsal tests for success, a furnace fault, a crucible that does not cool in time, and an unhomed stage.
3. The operator runs it on hardware with P-HW.

Done when: one supervised run succeeds and its record (PV curve, steps) is in the run store; the results are on the issue.

## Phase 4: microscope

### MIC-01 · Turret and focus firmware

Owner: agent+human · Size: L · Review: human · Deps: H-02

Goal: firmware source for the turret and focus motors in the repo, plus a host-side simulator.

Read: in Objective-Control, every command the Python code sends and every reply prefix it waits for (`rg -n "send_and_wait|_command\(|command\(" Objective_Control/*.py`), and lines 1 to 22 of `Dual_A4988_GUI.py` (the protocol docstring).

Steps:

1. If H-02 found the original sketch, commit it unchanged under `firmware/microscope-axes/` as the first commit. Otherwise write a new sketch with the structure of `three-motor-demo` (AccelStepper, bounded line buffer, non-blocking loop) and the wiring from H-02.
2. Keep the existing protocol byte for byte:

   | Command | Reply that the Python code waits for |
   | --- | --- |
   | `OBJ ENABLE` | `OK OBJ ENABLED` |
   | `OBJ DISABLE` | `OK OBJ DISABLED` |
   | `OBJ SPEED <n>` | starts with `OK OBJ SPEED` |
   | `OBJ MOVE <signed steps>` | `DONE OBJ POS <n>` when the move ends |
   | `FOCUS ENABLE`, `FOCUS DISABLE`, `FOCUS SPEED <n>`, `FOCUS MOVE <signed steps>` | `OK FOCUS ...` replies (`OK FOCUS DISABLED` is confirmed; check the others against the original sketch) |
   | `MICRO <1, 2, 4, 8 or 16>` | `OK MICRO <n>` |
   | `STOP OBJ`, `STOP FOCUS`, `STOP ALL` | `OK STOP OBJ`, `OK STOP FOCUS`, `OK STOP ALL` |
   | `STATUS` | status lines |
   | any error | a line that starts with `ERR` or `ERROR` |

3. Add without changing the rows above: `DONE FOCUS POS <n>` when a focus move ends (today the host waits a computed time), `FOCUS ZERO`, `FOCUS LIMIT <min> <max>` (moves outside the range are refused), `FOCUS HOME`, and `VERSION`, which replies `VERSION microscope-axes <x.y.z>`. `FOCUS HOME` moves away from the furnace window to an optional limit switch, sets zero there and replies `DONE FOCUS HOME`. Without a switch (pin 255 in the sketch) it replies with `ERR`. The objectives work just above the window, so the host needs a known focus zero before any automated move (MIC-03).
4. `src/microscope/sim.py`: the same grammar for tests and demo mode.
5. `tests/test_microscope_protocol.py`: the simulator answers every row of the table.
6. `firmware/microscope-axes/README.md` and `README.en.md`: wiring, commands, replies.

Done when: the sketch compiles (report the command and output); the protocol tests pass.

Notes: H-21 validates on hardware before any automated use.

### MIC-02 · Move the microscope algorithms

Owner: agent · Size: L · Review: agent · Deps: TPC-01, MIC-01

Source: Objective-Control `Objective_Control/autofocus.py`, `aruco_objective_locator.py`, `aruco_objective_switch.py`, `objective_calibration.py`, `light_objective_calibration.py`, `side_objective_capture.py`, `usb_camera_devices.py`, `camera.py` (the `USBCamera` part), lines 76 to 244 of `Dual_A4988_GUI.py` (`MotorController`), `test_aruco_calibration_policy.py`, `test_focus_completion.py`, `requirements.txt`, `README.md`.

Goal: objective switching, autofocus and calibration live in `src/microscope` and run headless on Linux.

Steps (P-MOVE plus):

1. `src/microscope/`: `autofocus.py`, `aruco_locator.py`, `aruco_switch.py`, `objective_calibration.py`, `light_calibration.py`, `side_capture.py`, `motors.py` (`MotorController`). `src/cameras/`: `usb.py` (the Linux part of `usb_camera_devices.py`) and `uvc.py` (`USBCamera`).
2. Imports: `toupcam_camera.MicroscopeCamera` instead of `camera`; package-relative imports elsewhere.
3. Paths: the calibration CSV files, `objective_views/`, `current_objective.txt`, `objective_reference.png` and `side_camera_images/` come from `MG400_MICROSCOPE_CALIBRATION_DIR` (default `/var/lib/mg400/calibration/microscope`). Experiment logs go to the run store folder.
4. Cameras: V4L2 only. The side camera is chosen by its `/dev/v4l/by-path` name (`MG400_MICROSCOPE_SIDE_CAMERA`). Remove the Windows enumeration and the DirectShow and MSMF paths.
5. `motors.py` asks `VERSION` at connect. With the new firmware, focus moves wait for `DONE FOCUS POS`. With the old one, keep the computed wait.
6. No Tk import anywhere.
7. Group `microscope` with numpy, opencv-python-headless and pyserial, in the default groups.
8. Tests moved, plus tests against the MIC-01 simulator.

Done when: P-CHECKS pass.

Docs: `docs/microscope.md` and `.en.md`, a first version from the source README.

### MIC-03 · Microscope service

Owner: agent · Size: L · Review: human · Deps: MIC-02, FND-05

Goal: the microscope speaks contract v1.

Steps (P-SERVICE plus):

1. Commands: `select_objective(id)` (first raise focus to the clearance position `MG400_MICROSCOPE_SWITCH_FOCUS`, then the ArUco switch with the side camera), `autofocus(mode)` (`full` or `local`, inside the focus limits), `focus_move(steps)` within the focus limits, `home_focus()`, `set_focus_zero()`, `snapshot()` (saved to the run store, or to a path the orchestrator gives), `stop`.
2. Focus zero: `home_focus()` sends `FOCUS HOME` when the focus axis has a switch. Without a switch, an operator moves focus to the clearance position and calls `set_focus_zero()` from the panel. Automation may not call it. Until zero is known, commands that move focus or the turret fail with "focus position unknown". A firmware reset clears zero. When zero is known, the service sends `FOCUS LIMIT` with `MG400_MICROSCOPE_FOCUS_LIMITS`.
3. State: objective id, focus position relative to zero, whether zero is known, camera states, busy, last result.
4. Stop and lease expiry: cancel the running task, send `STOP ALL`, disable both motors.
5. Demo mode: the MIC-01 simulator, a fake microscope camera, and synthetic side-camera frames with ArUco markers.
6. Tests in demo mode with `tests/contract_helpers.py`, including the clearance move before every turret turn and the refusals without zero.

Done when: P-CHECKS pass.

Docs: `microscope.md` and `.en.md`.

### MIC-04 · Microscope web panel

Owner: agent · Size: L · Review: agent · Deps: MIC-03

Goal: operators lose the Tk GUI when the microscope moves to Olares (D1). This page replaces its daily controls.

Steps:

1. A page served by the microscope service: live microscope view (MJPEG), side camera view, objective buttons (ids from the calibration data), autofocus buttons, focus jog within the limits, focus home or set zero (with a confirmation), snapshot, stop, status and events.
2. The same access rules as the other panels. English labels, like the robot and stage panels.
3. Python route tests and node tests for the page logic.

Done when: P-CHECKS pass.

Docs: `microscope.md` and `.en.md`.

### MIC-05 · Microscope calibration tools and data import

Owner: agent+human · Size: M · Review: agent · Deps: MIC-02, FND-09

Source: the calibration workers in `Dual_A4988_GUI.py` (`_objective_calibration_worker`, `_light_objective_calibration_worker`, `remember_objective_reference`) and the trials in `experiment_gui.py` (focus, switch, combined).

Goal: calibration and benchmark work without the Tk GUI, and the existing calibration data on Olares.

Steps:

1. `python -m microscope.calibrate objective|light|reference`: headless versions of the workers. They write to the calibration folder.
2. `python -m microscope.experiments focus|switch|combined`: the trial logic of `experiment_gui.py`, writing to the run store folder.
3. `mg400ctl calibration import microscope DIR` copies `objective_views/`, `side_camera_images/`, `objective_reference.png` and the CSV files into the calibration folder with owner 1000:1000 and mode 0640. It refuses symbolic links and lists what it copies.
4. Tests with a fake root and the simulator.

Done when: P-CHECKS pass; the operator imports the data from Objective-Control `3a2dad0` on Olares and comments on the issue.

Docs: `microscope.md` and `.en.md`.

### MIC-06 · Deploy the microscope on Olares

Owner: agent · Size: M · Review: human · Deps: MIC-03, TPC-02, FND-09, H-06

Goal: the microscope service runs on Olares with its devices.

Steps (P-DEPLOY plus):

1. Unit `mg400-microscope.service` with `SupplementaryGroups=dialout video`, `LD_LIBRARY_PATH` as in TPC-02, port 8770.
2. Settings `MG400_MICROSCOPE_SERIAL`, `_SIDE_CAMERA`, `_CAMERA_SERIAL`, `_CALIBRATION_DIR`, `_SWITCH_FOCUS`, `_FOCUS_LIMITS`, `_BIND`, `_PORT`, `_TOKEN`. The focus values come from H-06.
3. A registry entry `microscope` (optional).

Done when: P-CHECKS pass.

Docs: `microscope.md` and `.en.md`, `deploy.md` and `.en.md`.

### FUR-05 · Recordings with furnace metadata

Owner: agent · Size: M · Review: agent · Deps: FUR-04, MIC-04, TPC-02

Source: FurnaceCode `run_recordings.py`, `run_video_compiler.py`, the camera and recording routes of `app.py`, `test_run_recordings.py`, `test_run_video_compiler.py`, `test_local_run_sync.py`.

Goal: record video of the chip in the furnace, with the furnace readings on every frame.

Ask first: Q-02 (one Toupcam camera or two). Build only the case that the answer names.

Context: the microscope watches the chip through the furnace window, so the FurnaceCode camera is probably the microscope camera. A camera has one owner service. The owner of the camera records. The furnace service only supplies the readings.

Steps (P-MOVE plus):

1. `src/furnace/recordings.py` and `src/furnace/video.py`: the run recorder and the video compiler as libraries without camera code. Local runs go to `/var/lib/mg400/recordings/`. When `MG400_FURNACE_ARCHIVE_DIR` is set (a mounted share, Q-06), archive there instead of `Z:\Wu_Lab_Data`. Otherwise runs stay local.
2. A readings provider reads the furnace `api/state` at 1 Hz with `labkit.ServiceClient` and gives each frame the furnace fields of the source. The `.timestamps.csv` columns stay unchanged. When the furnace does not answer, the fields stay empty, an event is added, and the recording goes on.
3. One camera: the microscope service hosts recording. Add start and stop recording, "record with sequence" and the stabilization switch to its page and its contract commands.
4. Two cameras: the furnace service opens the furnace camera with `toupcam_camera` (`MG400_FURNACE_CAMERA_SERIAL`) and hosts the live feed, the stabilization switch and the same recording controls on the furnace page.
5. "Record with sequence": the provider sees a sequence start in the furnace state and starts recording. Recording stops when the sequence ends.
6. Runs that the orchestrator started also go into the run store under the chip ID.
7. Tests moved and adapted, with a fake furnace service and the fake camera.

Done when: P-CHECKS pass.

Docs: `furnace.md`, `microscope.md` and their `.en.md` pairs.

### MS-03 · Milestone: microscope image under the chip's ID

Owner: agent+human · Size: M · Review: human · Deps: MIC-06, MIC-05, ORC-05, XDV-01, H-18, H-21

Goal: the first automated image of a chip through the furnace window, with full provenance.

Steps:

1. `src/orchestrator/skills/microscope.py`: `microscope.select`, `microscope.autofocus`, `microscope.snapshot`. `src/orchestrator/skills/stage.py`: `stage.pose(name)`, which holds the zone `furnace-tube` while the stage moves to a named pose (STG-02).
2. `recipes/microscope-image.toml` with the params `chip_id`, `objective` and `pose`: verify the label, load the chip (`crucible.load`), move to the imaging pose, select the objective, autofocus, snapshot, move to the pose `furnace`, unload (`crucible.unload`), verify the label. The image goes to the run store under the chip ID.
3. The furnace stays in STOP for the whole run (room temperature). Imaging during heating is a later recipe, after the owner sets its limits.
4. Rehearsal tests for success, an unhomed stage, an unknown pose, an unknown focus zero and an autofocus failure.
5. The operator runs it with P-HW.

Done when: one supervised run at room temperature stores a focused image under the chip ID; the results are on the issue.

## Phase 5: people and agents

### DSH-01 · Lab dashboard

Owner: agent · Size: L · Review: human · Deps: ORC-05, FND-06, FND-10

Goal: one page for the whole lab.

Steps:

1. The orchestrator service serves it. The server reads each service's `api/state` with that service's token. The browser never receives service tokens.
2. One card per service: connected, busy, locked, fault, lease holder, demo flag.
3. Camera tiles that link to the camera page; the current run with its steps; hold, resume and abort.
4. Buttons: "Stop all motion" calls `api/stop` on the robot, the stage and the microscope at the same time and shows each result. "Heat off" and "Gas off" are separate buttons and need a confirmation inside the page.
5. The same access rules as the other panels. English labels.
6. Python tests with fake services; node tests for the page logic.

Done when: P-CHECKS pass.

Docs: `docs/dashboard.md` and `.en.md`.

### DSH-02 · Chip passport

Owner: agent · Size: M · Review: agent · Deps: DSH-01

Goal: everything about one chip on one page.

Steps:

1. `/chip/<id>`: every run, step, furnace PV curve, image, thermal reading and transfer for the chip ID, from the run store.
2. Search by QR text.
3. Tests and docs.

Done when: P-CHECKS pass.

### AGT-01 · MCP gateway with read-only tools

Owner: agent · Size: L · Review: human · Deps: ORC-05, GOV-02

Goal: D8, first half. LLM agents read the lab state through MCP and can do nothing else.

Steps:

1. `src/gateway/` with the Python MCP SDK, the Streamable HTTP transport, loopback port 8773 and a bearer token.
2. Read-only tools: `list_services`, `get_state(service)`, `list_recipes`, `describe_recipe`, `list_runs`, `get_run`, `get_chip(id)`.
3. Every call goes to the run store: caller, tool, arguments, time.
4. Unit `mg400-gateway.service`, a registry entry (optional), group `gateway`.
5. Tests with an in-process MCP client.

Done when: P-CHECKS pass; a test fixes the list of tools, and none of them changes a device.

Docs: `docs/agent-gateway.md` and `.en.md`.

### AGT-02 · Approval gates for actuation

Owner: agent · Size: L · Review: human · Deps: AGT-01, DSH-01, H-23

Goal: D8, second half. An agent may propose a recipe run, and a person approves it.

Steps:

1. `src/orchestrator/approvals.py`: pending approvals with recipe, params, requester, reason, creation time, decision and decider.
2. Gateway tools `propose_run(recipe, params, reason)` and `get_approval(id)`. No tool sends a device command.
3. Dashboard: an approvals list that shows the full recipe and params, with Approve and Reject. An approved run starts in the orchestrator like a manual run.
4. Check the H-23 limits (for example the maximum temperature) before a proposal is stored.
5. Tests for propose, approve, reject, expiry and limit violations.

Done when: P-CHECKS pass.

Docs: `agent-gateway.md` and `.en.md`, `dashboard.md` and `.en.md`.

### AGT-03 · Agent clients

Owner: agent · Size: M · Review: agent · Deps: AGT-02

Source: FurnaceCode `smolagents_agent.py` (`OPERATOR_GUIDANCE`, the LiteLLM settings `QUMUS_*`), as ideas only.

Goal: Claude, a local model on Olares and smolagents can use the gateway.

Steps:

1. Configuration examples for Claude, a local Qwen model on Olares, and smolagents, which can load MCP tools.
2. A prompt resource in the gateway with operator guidance, rewritten for recipe-level tools.
3. Smoke tests with fakes.

Done when: P-CHECKS pass.

Docs: `agent-gateway.md` and `.en.md`.

### OPS-02 · One reverse-proxy entry for all services

Owner: agent · Size: M · Review: human · Deps: FND-09, ORC-05

Goal: one VPN port and one address instead of one per service.

Steps:

1. A proxy (for example Caddy, installed like go2rtc with `--caddy PATH`) on the VPN address, with the paths `/robot/`, `/stage/`, `/camera/`, `/thermal/`, `/furnace/`, `/microscope/`, `/lab/`.
2. Meet the proxy rules in `docs/control-panel.md`: HTTP/1.1 for `api/ws` with Upgrade and Connection, the browser's Host with port or `X-Forwarded-Host`, a read timeout above 5 s, tokens set even on loopback.
3. Every service supports a base path. The robot panel has `--base-path`; labkit services take `base_path`.
4. Unit, registry entry, tests, docs.

Done when: P-CHECKS pass.

Notes: optional. Skip it while one VPN rule per port stays workable.

## Phase 6: research and cleanup

### OPT-01 · Image metric for grown material

Owner: agent+human · Size: L · Review: human · Deps: MS-03

Goal: one number per microscope image that says how good the growth is, for example coverage or layer count from optical contrast on SiO₂.

Steps:

1. The owner defines the metric and provides labeled examples.
2. `src/analysis/`: the metric, tested on the examples.
3. A skill that computes it for a snapshot and stores it in the run store.

Done when: the owner accepts the metric on the examples.

### OPT-02 · Recipe optimizer loop

Owner: agent+human · Size: L · Review: human · Deps: OPT-01, MS-02

Goal: the next furnace recipe comes from the results of the previous ones.

Steps:

1. The owner gives the parameter space: temperatures, ramps, holds and gas flows, within the furnace caps.
2. An optimizer, for example Bayesian optimization, proposes the next recipe from past metrics in the run store.
3. Proposals go through the AGT-02 approvals or a manual start by the owner.

Done when: the owner accepts a simulated loop over recorded data.

### OPT-03 · Unattended overnight runs

Owner: agent+human · Size: L · Review: human · Deps: OPT-02, DSH-01, H-24

Goal: several chips processed overnight under the H-24 conditions.

Steps:

1. Queue several chips and interleave furnace runs with microscope imaging.
2. Notifications on holds and faults, on a channel the owner chooses.
3. Enforce the H-24 conditions in code.

Done when: one overnight run completes under the H-24 conditions.

### CLN-01 · Mark the WuLab repos as moved

Owner: agent+human · Size: S · Review: human · Deps: VIS-02, THM-01, FUR-05, MIC-02, H-16

Goal: nobody keeps editing the old copies.

Steps:

1. With each author's consent (H-16), put a note at the top of each WuLab repo README: moved to shc0507/dobot_mg400, the new paths, the date. Or archive the repo.
2. An agent may draft these PRs. The authors merge them.

Done when: each WuLab repo shows the note or is archived.

### CLN-02 · Remove the migration plan

Owner: agent · Size: S · Review: human · Deps: MS-01, MS-02, MS-03, DSH-02, AGT-03, OPS-03, CLN-01

Goal: the repo keeps only current procedures once the migration ends.

Steps:

1. Move any open OPT task to its own map issue.
2. Delete `docs/agents/migration/` and `tests/test_migration_plan.py`.
3. Remove the migration section and row from `AGENTS.md`. Check that the docs and ADRs state every decision that still applies.

Done when: P-CHECKS pass.

## Human tasks

Hardware freeze (README section 2.2): device moves, wiring and mounting wait until the owner ends the freeze. This holds H-04 to H-07, H-09, mounting the tag in H-08 and installing a cutoff in H-11. The other steps and tasks go on.

### H-02 · Recover the turret and focus firmware

Owner: human · Size: S · Review: human · Deps: none

Steps:

1. On the microscope PC, find the Arduino sketch that speaks the OBJ/FOCUS protocol: the Arduino IDE sketchbook, Downloads, the project folder.
2. Photograph the wiring. Write down the board type and the STEP, DIR, EN and microstep pins of each driver.

Output on the issue: the sketch folder as a zip, or "not found", and the wiring table. Unblocks MIC-01.

### H-03 · USB inventory and hardware purchase

Owner: human · Size: M · Review: human · Deps: none

Steps:

1. List every device that moves to Olares: model, VID:PID (`lsusb`), serial number, USB speed, cable length needed, current host.
2. Plan the hub layout: the Toupcam camera on its own USB 3 controller, powered hubs by job (cameras; serial adapters and Arduinos).
3. Buy what is missing: powered hubs, and a PCIe USB 3 card if Olares has no free controller.

Output: the device table on the issue. Unblocks OPS-01, H-04, H-05, H-06, H-09.

### H-04 · Move the furnace and MFC links to Olares

Owner: human · Size: M · Review: human · Deps: H-03

Steps:

1. Find the link type of the furnace and MFC adapters (RS-485 or other) and the cable lengths to Olares (Q-04).
2. Move the USB adapters to Olares and run the cables. Keep the heater off during the move.
3. Write down the `/dev/serial/by-id/` names. Set `MG400_FURNACE_SERIAL` and `MG400_MFC_SERIAL`.
4. If recordings must go to the lab share, mount it on Olares (Q-06).

Unblocks H-20.

### H-05 · Move the thermal camera to Olares

Owner: human · Size: S · Review: human · Deps: H-03

Steps:

1. Connect it to Olares per the H-03 layout.
2. Mount it where it sees the crucible at the load position.
3. Write down its USB path.

Unblocks THM-02, H-19.

### H-06 · Move the microscope devices to Olares

Owner: human · Size: M · Review: human · Deps: H-03

Steps:

1. Move the Toupcam camera, the turret and focus Arduino and the side AC410 camera to Olares per the H-03 layout.
2. Write down the Toupcam VID and PID, the Arduino's `/dev/serial/by-id/` name and the side camera's `/dev/v4l/by-path/` name.
3. Do not move the side camera or the ArUco markers relative to the turret. The calibration data depends on their positions.
4. Keep the microscope aligned over the furnace window. Find the clearance position: a focus position at which the turret turns every objective clear of the window, with a margin. Mark it so that an operator can find it again, and write down its distance from the focus switch if one is fitted.
5. Find the focus limits: the focus range in which every objective stays clear of the window, with a margin, measured from the clearance position.

Unblocks MIC-06, H-21.

### H-07 · Stage limit switches and geometry

Owner: human · Size: M · Review: human · Deps: STG-01

Steps:

1. Install minimum and maximum limit switches on all three axes. Wire them to the pins in STG-01.
2. Upload the STG-01 firmware.
3. Home each axis. Measure the steps per mm at the microstep in use.
4. Measure the Insert positions `load` and `furnace` in mm from home.
5. With the furnace cold, measure the soft limits of each axis: the travel that keeps the crucible clear of the tube and the window.
6. Measure the poses: each imaging pose (the positions of all three axes that put the chip under the window, checked with a test chip in the microscope view) and the pose `furnace`. Write down the axis order that reaches each pose without a collision.

Output: the values for `/var/lib/mg400/stage.toml` on the issue. Unblocks H-18.

### H-08 · Robot workspace measurements

Owner: human · Size: M · Review: human · Deps: none

Steps:

1. Measure the gripper tool offset: dx, dy, dz in mm from the flange to the fingertip center.
2. Print and mount the ArUco calibration tag (dictionary 4x4_50). Write down its id, size and offset from the fingertip.
3. Choose the safe travel height and any via points around the furnace and the microscope.
4. Measure the tray: the text and position of its two corner QR codes, and the position of every slot.

Output on the issue. Unblocks CAL-01, CAL-02, H-17.

### H-09 · Final camera setup

Owner: human · Size: M · Review: human · Deps: H-03

Steps:

1. Decide which cameras stay and what each one watches: the tray, the crucible load position, the workspace.
2. Mount them rigidly. Use diffuse light over the tray.
3. Choose a mode per camera. Check that a Micro QR label gets about 70 pixels across.
4. Write down each camera's `/dev/v4l/by-path/` name and set `MG400_CAMERA_DEVICES`.

Output on the issue. Unblocks VIS-07, CAL-01, H-22.

### H-10 · Furnace process rules

Owner: human · Size: S · Review: human · Deps: none

Steps:

1. Name the gas on each MFC channel and the inert one (Q-05).
2. Set the end-of-run gas rule (for example: inert gas until PV is below 100 °C, then all off) and the idle gas after a stopped manual setpoint.
3. Set the caps for automation (maximum °C and °C/min) and the grace time for an orphaned manual setpoint.
4. Decide whether `mg400ctl stop furnace` may stop a running sequence.
5. Put the AI-808 communication protocol PDF on the lab share and put its path on the issue.

Output on the issue. Feeds ADR-0005, FUR-03 and H-20.

### H-11 · Independent over-temperature cutoff and e-stop review

Owner: human · Size: M · Review: human · Deps: none

Steps:

1. Confirm that an over-temperature cutoff works without any software. Install one if it is missing.
2. Check which e-stop stops which device: robot, stage motors, turret and focus motors, furnace heat.

Output on the issue. Unblocks H-20 and MS-02.

### H-12 · Toupcam Linux SDK

Owner: human · Size: S · Review: human · Deps: none

Steps:

1. Download the ToupTek SDK for Linux x64, version 59.30594.20260120, or update the wrapper and the library together.
2. Check that its license allows this use.
3. Keep `libtoupcam.so` and the vendor's udev rules outside Git. Give the library to the installer with `--toupcam`.

Unblocks TPC-02.

### H-13 · Olares GPU and disk check

Owner: human · Size: S · Review: human · Deps: none

Steps:

1. Run `nvidia-smi` as the service user (UID 1000). It must work.
2. Write down the GPU memory that other apps use.
3. Check that at least 15 GB of disk are free for the CUDA build of PyTorch.

Output on the issue. Unblocks VIS-05.

### H-14 · Publish the weights release

Owner: human · Size: S · Review: human · Deps: H-15

Steps:

1. The lab agreed to publish the weights (Q-08). Wait until H-15 has chosen the license.
2. Create the release `vision-model-v1` on shc0507/dobot_mg400 with the asset `chip-arm-yolo26s-seg-v1.pt`. It is `models/weights.pt` from Visual-Localization `cc329b3`: 23,393,117 bytes, SHA-256 `0b684c470191540b0cfc8802b0d13149b47d7be76b901ad1dd59882297407a07`.

Unblocks VIS-05.

### H-15 · Repository license

Owner: human · Size: S · Review: human · Deps: none

Steps:

1. Choose a license for this repo, with the AGPL-3.0 label of the model and of `ultralytics` in mind.
2. If the weights must stay private, change D4 to a private model store first.

Output on the issue; an agent adds the LICENSE file. Unblocks H-14.

### H-16 · Colleague consent for the move

Owner: human · Size: S · Review: human · Deps: none

Steps: ask the authors of the four WuLab repos to agree to the move and to the "moved" notes or archiving.

Output on the issue. Unblocks CLN-01.

### H-17 · Validate the robot changes on hardware

Owner: human · Size: M · Review: human · Deps: ROB-02, ORC-02, H-08

Steps (P-HW):

1. Speed cap: moves at speed 1, then at the planned speed.
2. Tool offset: touch a marked point with the fingertip from several R angles.
3. Gripper outcomes: an empty close, a grasp, a drop.
4. Stations with via points at low speed.

Output on the issue. Unblocks MS-01.

### H-18 · Validate the stage on hardware

Owner: human · Size: M · Review: human · Deps: STG-02, H-07

Steps (P-HW):

1. Homing on each axis; limit stops in both directions.
2. The named positions `load` and `furnace`, the soft limits, and each pose at low speed.
3. Keep-awake: the axis holds while the robot works at the crucible at low speed.

Output on the issue. Unblocks MS-02.

### H-19 · Validate the thermal camera on Olares

Owner: human · Size: S · Review: human · Deps: THM-02, H-05

Steps (P-HW):

1. Warm up for 5 minutes and run the shutter correction.
2. Compare a reading with a reference, for example a hot plate with a thermocouple.
3. Define the region `crucible` and check its reading.

Output on the issue. Unblocks MS-02.

### H-20 · Validate the furnace and gas on Olares

Owner: human · Size: M · Review: human · Deps: FUR-04, H-04, H-10, H-11

Steps (P-HW):

1. Connect and read PV and SV.
2. Run a short program without a sample at a low temperature, for example 100 °C, and stop it.
3. Inert gas at a low flow; then stop all gas.
4. Close the controlling page during a run and check the D5 behavior. Restart the service and check the startup rule.

Output on the issue. Unblocks MS-02.

### H-21 · Validate the microscope on Olares

Owner: human · Size: M · Review: human · Deps: MIC-06, MIC-04, MIC-05, H-06

Steps (P-HW):

1. With the furnace cold, check the focus zero (home or operator zero), the focus limits and the clearance position against the window.
2. Switch through every objective in both directions. The turret turns only at the clearance position.
3. Autofocus, full and local. Check that both stay inside the focus limits.
4. Take a snapshot from the panel.

Output on the issue. Unblocks MS-03.

### H-22 · Capture and label evaluation images

Owner: human · Size: M · Review: human · Deps: H-09, VIS-05

Steps:

1. Export the current labeled set from the Roboflow project behind the model `rotatedlabel/yolo-for-the-qunums2-0-right-han-1-yolo26s-seg-t1` (dataset version 1). Use a Roboflow account with access to that project (Q-07).
2. Capture at least 50 tray images with the final cameras, with varied chip positions and lighting.
3. Label the chips and the arm with the classes of the model. Anyone in the lab may label (Roboflow, CVAT or similar). Keep the set outside Git and put its location on the issue.

Unblocks VIS-08.

### H-23 · Agree the agent approval policy

Owner: human · Size: S · Review: human · Deps: AGT-01

Steps: decide which recipes an agent may propose, who approves them, and the limits (temperatures, speeds, gases). Update ADR-0008 in a PR.

Unblocks AGT-02.

### H-24 · Sign off the first unattended run

Owner: human · Size: S · Review: human · Deps: MS-02, MS-03, DSH-01

Steps: after MS-02 and MS-03, decide the conditions for unattended runs: who is on call, the maximum temperature, the allowed gases, the notifications.

Unblocks OPT-03.
