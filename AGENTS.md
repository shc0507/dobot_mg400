# AGENTS.md

This document provides permanent information/instructions for the convenience of the agents working on this project.

## The Workflow

- 操作 Chrome 时只控制需要的 page，不要再申请整个 Chrome 应用的权限。
- Olares is the lab host. It runs the lab services behind the Lab Console, which the proxy serves at `http://100.64.0.1:8080/` (it opens `/lab/`). [docs/deploy.md](docs/deploy.md) describes the services, the settings, and the `mg400ctl` command. Agents can control Olares through its web interface. Check the Chrome browser for tabs that are already open. If no tab is open, ask the user to open the Olares web interface and log in. The URL is: [https://desktop.qumusphusis.olares.com/](https://desktop.qumusphusis.olares.com/)
- VPN is necessary to access services on Olares (`100.64.0.1`). Turn it on with the LarePass application.
- There is a Dobot MG400 robot arm in the physical lab. The user will tell if it is connected or not.
- 排查真机报警或碰撞时，先读 [报警与历史日志](docs/hardware.md#报警与历史日志)。历史查询优先读取控制器 SMB 日志。
- When git-committing a task, also write a brief description of what was done.
- Coordinator and workers: a coordinator session reads [docs/agents/coordinator.md](docs/agents/coordinator.md) and keeps the board in `work/board.md`. A dispatched worker reads [docs/agents/worker.md](docs/agents/worker.md).

## Temporary screenshot cleanup

- This rule applies only when a task in this project creates temporary screenshots; it does not require screenshots for every Computer Use action.
- Track the generated temporary file paths or record the screenshot cache baseline before capture. After the task is complete and its result verified, directly delete only screenshots confirmed to belong to this task and no longer needed. Preserve the cache directory structure; moving files to Trash does not reclaim space.
- Preserve requested deliverables, project images, user files, conversation records, and screenshots needed by other active tasks. Keep files whose ownership is uncertain; never indiscriminately clear a shared cache.
- Verify cleanup and report the removed file count and size when material.

## Documenting

- Use instruction documents only for current procedures, interfaces, configuration requirements, and known limitations. This includes `AGENTS.md`, `README.md`, and instruction pages such as `docs/control-panel.md`.
- Do not add records of individual tasks or experiments to instruction documents. Do not add test dates, execution histories, troubleshooting attempts, or raw output from individual runs. Do not add test counts, measurements, operator confirmations, or final session states from individual runs. Do not add completion notes or links to individual run reports.
- "Useful for future reference," traceability, and task completion do not justify exceptions. Existing logs in an instruction document do not authorize more logs.
- When a verified change affects future use, update the relevant instructions in place. Extract the reusable procedure, interface, configuration requirement, or limitation. Omit the experiment narrative and evidence. Keep configuration values that future users must use to operate the system.
- Report task outcomes in the conversation. If evidence must persist, use an existing log directory or `work/`. Do not create a separate Markdown report unless the user requests one.
- Before saving an instruction document, check every added or changed paragraph. Each paragraph must describe current operation or guide future use. Remove all prohibited task records from your changes before completion. If a task changes no instructions, leave instruction documents unchanged.
- Keep documentation concise. Use clear headings, bullet points, and numbered lists where they help the reader.
- Use Simplified Technical English skill for writing in English.
- Use jianming-zhongwen skill for writing in Chinese.
- Human docs use Chinese `X.md` and English `X.en.md` beside it. Change both files in the same PR.
- This rule covers `README.md`, `docs/*.md`, `docs/adr/*.md`, and `firmware/*/README.md`. Agent docs stay English only.
- `AGENTS.md`, `docs/agents/**`, `.agents/**`, `GLOSSARY.md` (one bilingual table), and `tests/fixtures/**` are exempt.
- `scripts/check_doc_pairs.py` checks code blocks, inline code, numbers, link targets, and heading counts through pytest. Write numbers as digits in both languages.
- The checker permits missing pairs listed in `docs/agents/doc-pairs-pending.txt`. Until a pair exists, edit the Chinese file. Remove its entry when you add the pair.

## UX design

- Avoid unnecessary secondary labels and descriptions. Let the layout do the explaining.

## Reading the codebase

Read only the files that the task needs. The tests are larger than the code, so a full read costs many tokens.

- Find the files for a task with a search first. Then read only the files that the task changes or calls.
- Use the table below to find the tests and the doc for an area. Read and run only those tests. Before you commit, run `ruff check`, `ruff format --check` and the area tests. Do not run the full suite locally.
- Read a large test file by search, not from the top. List its tests with `rg -n "^def test_" <file>`, then read only the tests that you need.
- Read the shared test helpers (`tests/conftest.py`, `tests/helpers.py`, `tests/sim_mg400.py`, `tests/panel_ws.py`, `tests/stage_helpers.py`) only when a test that you read uses them.
- Do not read `uv.lock`, `firmware/`, `apps/layout-studio/` or `examples/` unless the task names them. Load a skill from `.agents/skills/` only when the task needs it.

| Area | Code | Tests | Doc |
| --- | --- | --- | --- |
| Arm motion, limits, protocol | `src/mg400/joint_control.py`, `joint_limits.py`, `controller_protocol.py`, `feedback.py`, `controller_alarms.py` | `test_joint_control.py`, `test_joint_limits.py`, `test_controller_protocol.py`, `test_feedback.py`, `test_controller_alarms.py` | [hardware.md](docs/hardware.md), [development.md](docs/development.md#真机控制参考) |
| Keyboard jog | `src/mg400/keyboard_jog.py`, `web_keyboard.py` | `test_keyboard_jog.py`, `test_keyboard_movejog.py`, `test_web_keyboard*.py` | [control-panel.md](docs/control-panel.md#控制机械臂) |
| Web panel service | `apps/control_panel/`, `src/mg400/web_control.py`, `websocket.py`, `controller_ownership.py`, `robot_config.py` | `test_control_panel*.py`, `test_web_control.py`, `test_websocket.py`, `test_robot_service_e2e.py`, `test_controller_ownership.py`, `test_robot_config.py` | [control-panel.md](docs/control-panel.md), [development.md](docs/development.md#机器人服务接口) |
| Robot service Python client | `src/mg400/client.py` | `test_robot_client.py` | [development.md](docs/development.md#python-客户端) |
| Vacuum suction | `src/mg400/suction.py`, `joint_control.py` (`vacuum`), `web_control.py` | `test_suction.py` | [development.md](docs/development.md#真空吸盘) |
| Gripper | `src/mg400/web_gripper.py`, `gripper_feedback.py`, `scripts/gripper_feedback.py` | `test_web_gripper.py`, `test_gripper_feedback.py`, `test_gripper_serial_feedback.py` | [development.md](docs/development.md#usb-夹爪协议) |
| Terminal | `src/mg400/terminal_control.py` | `test_terminal_control.py` | [terminal-control.md](docs/terminal-control.md) |
| Mode switch | `src/mg400/control_mode.py` | `test_control_mode.py` | [hardware.md](docs/hardware.md#从服务器切换-tcpip-模式) |
| Shared service contract and access | `src/labkit/auth.py`, `lease.py`, `events.py`, `describe.py`, `client.py`, `flask.py` | `test_labkit_*.py`, `contract_helpers.py`, `test_control_panel*.py`, `test_stage_app.py` | [contract ADR](docs/adr/0001-instrument-service-contract.en.md), [development.md](docs/development.md) |
| Stage panel | `src/stage_controller/` | `test_stage_*.py` | [stage-panel.md](docs/stage-panel.md) |
| Camera configuration and preview | `src/cameras/config.py`, `scripts/camera_preview.py` | `test_deploy_install.py`, `test_camera_preview.py` | [olares.md](docs/olares.md#摄像头) |
| Deployment | `deploy/`, `config/systemd/`, `config/proxy/`, `config/udev/` | `test_deploy_*.py`, `test_systemd_units.py`, `test_udev_rules.py` | [deploy.md](docs/deploy.md), [olares.md](docs/olares.md) |
| QR reader | `src/vision/qr.py`, `qr_micro.py` | `test_vision_qr*.py` | Module docstrings |
| Simulator | `src/mg400/sim/` | `test_mg400_sim.py`, `sim_mg400.py`, `test_web_keyboard_integration.py`, `test_robot_service_e2e.py` | [development.md](docs/development.md#离线机器人模拟器) |
| Vision localizer | `src/vision/localizer.py` | `test_vision_localizer.py` | Module docstrings |
| Detector evaluation | `scripts/vision_eval.py` | `test_vision_eval.py` | [vision.md](docs/vision.md#检测模型评估) |
| Vision service | `src/vision/service.py`, `frames.py`, `__main__.py` | `test_vision_service.py`, `contract_helpers.py` | [vision.md](docs/vision.md) |
| Camera-to-robot calibration | `src/vision/calibration.py`, `src/orchestrator/skills/calibrate.py`, `src/orchestrator/rehearsal.py` | `test_vision_calibration.py` | [vision.md](docs/vision.md) |
| Calibration drift check | `src/vision/calibration.py`, `service.py`, `src/orchestrator/skills/vision.py` | `test_vision_drift.py` | [vision.md](docs/vision.md) |
| Tray geometry and slot cross-checks | `src/vision/tray.py`, `config/trays/` | `test_vision_tray.py` | [vision.md](docs/vision.md) |
| Thermal camera | `src/thermal_cam/` | `test_thermal_*.py` | [thermal.md](docs/thermal.md) |
| Toupcam capture and recording | `src/toupcam_camera/camera.py`, `capture.py` | `test_toupcam_camera.py` | Module docstrings; keep vendor `toupcam.py` unchanged |
| Furnace and gas drivers | `src/furnace/controller.py`, `config.py` | `test_furnace_controller.py` | Module docstrings, [furnace lifecycle ADR](docs/adr/0005-furnace-lifecycle.en.md) |
| Furnace web service | `src/furnace/app.py`, `__main__.py` | `test_furnace_app.py` | [furnace.md](docs/furnace.md) |
| Microscope recordings and furnace metadata | `src/furnace/recordings.py`, `video.py`, `src/microscope/service.py`, `src/orchestrator/skills/recording.py` | `test_furnace_recordings.py`, `test_furnace_video.py`, `test_furnace_local_run_sync.py`, `test_microscope_panel.py`, `test_orchestrator_recording.py` | [microscope.md](docs/microscope.md), [furnace.md](docs/furnace.md) |
| Furnace run lifecycle and recovery | `src/furnace/service.py`, `state.py`, `sim.py` | `test_furnace_service.py`, `test_furnace_sim.py` | Module docstrings, [furnace lifecycle ADR](docs/adr/0005-furnace-lifecycle.en.md) |
| Recipe orchestration and rehearsal | `src/orchestrator/`, `recipes/`, `deploy/mg400ctl` | `test_orchestrator_*.py`, `test_recipe_tray_transfer.py`, `test_deploy_mg400ctl.py` | [orchestrator.md](docs/orchestrator.md) |
| Lab Console | `src/orchestrator/web/`, `src/orchestrator/app.py`, `scripts/console_font.py` | `test_console*.py`, `test_console*.mjs`, `test_dashboard.py`, `test_dashboard_passport.py` | [dashboard.md](docs/dashboard.md) |
| MCP gateway and approval policy | `src/gateway/`, `src/orchestrator/approvals.py`, `src/orchestrator/store.py` | `test_gateway*.py`, `test_approvals.py` | [agent-gateway.md](docs/agent-gateway.md) |
| Microscope motor firmware and simulator | `firmware/microscope-axes/`, `src/microscope/sim.py` | `test_microscope_protocol.py` | [Firmware guide](firmware/microscope-axes/README.md) |
| Microscope algorithms, service and panel, motors, and camera input | `src/microscope/`, `src/cameras/` | `test_microscope_calibrate.py`, `test_microscope_calibration_policy.py`, `test_microscope_focus_completion.py`, `test_microscope_motors.py`, `test_microscope_io.py`, `test_microscope_service.py`, `test_microscope_panel.py` | [microscope.md](docs/microscope.md) |

Run the tests of one area, for example: `uv run pytest -q tests/test_joint_control.py tests/test_joint_limits.py` or `node --test tests/test_console.mjs`.

CI runs ruff and all `tests/*.mjs` on every run. On a pull request, CI runs only the pytest files that `scripts/select_tests.py` selects from the changed files. The script runs the full suite when a change touches shared code or a path that it does not map: `src/labkit/`, shared test helpers, `tests/fixtures/`, `pyproject.toml`, `uv.lock`, `.github/`, or the script itself. A push to `main` runs the full suite. When you add an area or move code, update `AREAS` in `scripts/select_tests.py` and the table above. To see the selection for a branch, run `git diff --name-only origin/main... | uv run python scripts/select_tests.py`.

## Test stage

- The lab is a test stage. The owner needs every device to run and to be tested freely.
- Keep changes minimal. Do not add settings, tokens, access checks, task cards, plan documents or review processes. Use fixed defaults in code.
- Keep only these protections:
  - Stop and STOP ALL work at any time, need no control and are never blocked.
  - One controller per device at a time, through the lease.
  - Furnace: the maximum temperature and ramp caps, heat off and gas off.
  - Robot: the speed cap, and no move while the controller reports an alarm.
  - Microscope: the focus limits, and full turret switches only at the clearance position.
  - Stage: soft limits, when they are configured.
- Fix hardware problems in a local session on the lab machine: change, deploy to Olares, test on the devices, then push.
- Files in `docs/agents/` are for agents. Write them in English only.

## Agent skills

Local skills are in `.agents/skills/<skill-name>/SKILL.md`, relative to the repository root.

### Issue tracker

Issues and specs use GitHub Issues. Before issue operations, read [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md).

### Triage labels

Before triage, read the label mapping in [docs/agents/triage-labels.md](docs/agents/triage-labels.md).

### Domain docs

Use one root glossary and one ADR directory. Before exploring the codebase, read [docs/agents/domain.md](docs/agents/domain.md).
