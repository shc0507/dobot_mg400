# AGENTS.md

This document provides permanent information/instructions for the convenience of the agents working on this project.

## The Workflow

- 操作 Chrome 时只控制需要的 page，不要再申请整个 Chrome 应用的权限。
- Olares is a cloud host. It runs the web control panel, the stage motor panel, the USB gripper collector, and the camera preview. [docs/deploy.md](docs/deploy.md) describes the services, the settings, and the `mg400ctl` command. Agents can control Olares through its web interface. Check the Chrome browser for tabs that are already open. If no tab is open, ask the user to open the Olares web interface and log in. The URL is: [https://desktop.qumusphusis.olares.com/](https://desktop.qumusphusis.olares.com/)
- VPN is necessary to access services on Olares (`100.64.0.1`). Turn it on with the LarePass application.
- There is a Dobot MG400 robot arm in the physical lab. The user will tell if it is connected or not.
- 排查真机报警或碰撞时，先读 [报警与历史日志](docs/hardware.md#报警与历史日志)。历史查询优先读取控制器 SMB 日志。
- When git-committing a task, also write a brief description of what was done.

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
- The checker permits missing pairs listed in `docs/agents/migration/doc-pairs-pending.txt`. Until a pair exists, edit the Chinese file. Remove its entry when you add the pair.

## UX design

- Avoid unnecessary secondary labels and descriptions. Let the layout do the explaining.

## Reading the codebase

Read only the files that the task needs. The tests are larger than the code, so a full read costs many tokens.

- Find the files for a task with a search first. Then read only the files that the task changes or calls.
- Use the table below to find the tests and the doc for an area. While you work, read and run only those tests. Run the full checks in [docs/development.md](docs/development.md#开发检查) once before you commit.
- Read a large test file by search, not from the top. List its tests with `rg -n "^def test_" <file>`, then read only the tests that you need.
- Read the shared test helpers (`tests/conftest.py`, `tests/helpers.py`, `tests/sim_mg400.py`, `tests/panel_ws.py`, `tests/stage_helpers.py`) only when a test that you read uses them.
- Do not read `uv.lock`, `firmware/`, `apps/layout-studio/` or `examples/` unless the task names them. Load a skill from `.agents/skills/` only when the task needs it.

| Area | Code | Tests | Doc |
| --- | --- | --- | --- |
| Arm motion, limits, protocol | `src/mg400/joint_control.py`, `joint_limits.py`, `controller_protocol.py`, `feedback.py`, `controller_alarms.py` | `test_joint_control.py`, `test_joint_limits.py`, `test_controller_protocol.py`, `test_feedback.py`, `test_controller_alarms.py` | [hardware.md](docs/hardware.md), [development.md](docs/development.md#真机控制参考) |
| Keyboard jog | `src/mg400/keyboard_jog.py`, `web_keyboard.py` | `test_keyboard_jog.py`, `test_keyboard_movejog.py`, `test_web_keyboard*.py` | [control-panel.md](docs/control-panel.md#键盘点动) |
| Web panel service | `apps/control_panel/`, `src/mg400/web_control.py`, `websocket.py`, `controller_ownership.py`, `robot_config.py` | `test_control_panel*.py`, `test_control_panel_keys.mjs`, `test_web_control.py`, `test_websocket.py`, `test_robot_service_e2e.py`, `test_controller_ownership.py`, `test_robot_config.py` | [control-panel.md](docs/control-panel.md), [development.md](docs/development.md#机器人服务接口) |
| Robot service Python client | `src/mg400/client.py` | `test_robot_client.py` | [development.md](docs/development.md#python-客户端) |
| Gripper | `src/mg400/web_gripper.py`, `gripper_feedback.py`, `scripts/gripper_feedback.py` | `test_web_gripper.py`, `test_gripper_feedback.py`, `test_gripper_serial_feedback.py` | [development.md](docs/development.md#usb-夹爪协议) |
| Terminal | `src/mg400/terminal_control.py` | `test_terminal_control.py` | [terminal-control.md](docs/terminal-control.md) |
| Mode switch | `src/mg400/control_mode.py` | `test_control_mode.py` | [hardware.md](docs/hardware.md#从服务器切换-tcpip-模式) |
| Shared service contract and access | `src/labkit/auth.py`, `lease.py`, `events.py`, `describe.py`, `client.py`, `flask.py` | `test_labkit_*.py`, `contract_helpers.py`, `test_control_panel*.py`, `test_stage_app.py` | [contract ADR](docs/adr/0001-instrument-service-contract.en.md), [development.md](docs/development.md) |
| Stage panel | `src/stage_controller/` | `test_stage_*.py` | [stage-panel.md](docs/stage-panel.md) |
| Camera preview | `scripts/camera_preview.py`, `camera_preview.js` | `test_camera_preview.py`, `test_camera_preview.mjs` | [olares.md](docs/olares.md#摄像头) |
| Deployment | `deploy/`, `config/systemd/` | `test_deploy_*.py`, `test_systemd_units.py` | [deploy.md](docs/deploy.md), [olares.md](docs/olares.md) |
| QR reader | `src/vision/qr.py`, `qr_micro.py` | `test_vision_qr*.py` | Module docstrings |
| Simulator | `src/mg400/sim/` | `test_mg400_sim.py`, `sim_mg400.py`, `test_web_keyboard_integration.py`, `test_robot_service_e2e.py` | [development.md](docs/development.md#离线机器人模拟器) |
| Vision localizer | `src/vision/localizer.py` | `test_vision_localizer.py` | Module docstrings |
| Vision service | `src/vision/service.py`, `frames.py`, `__main__.py` | `test_vision_service.py`, `contract_helpers.py` | [vision.md](docs/vision.md) |
| Thermal camera | `src/thermal_cam/` | `test_thermal_*.py` | [thermal.md](docs/thermal.md) |
| Migration plan | `docs/agents/migration/tasks.toml`, `tasks.md` | `test_migration_plan.py` | [migration README](docs/agents/migration/README.md) |

Run the tests of one area, for example: `uv run pytest -q tests/test_joint_control.py tests/test_joint_limits.py` or `node --test tests/test_camera_preview.mjs`.

## Lab integration migration

- The plan that moves the WuLab instrument code into this repo is in [docs/agents/migration/](docs/agents/migration/README.md). Read its README before you start a migration task. `tasks.md` has one card per task, and `tasks.toml` is the task graph.
- Files in `docs/agents/` are for agents. Write them in English only.

## Agent skills

Local skills are in `.agents/skills/<skill-name>/SKILL.md`, relative to the repository root.

### Issue tracker

Issues and specs use GitHub Issues. Before issue operations, read [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md).

### Triage labels

Before triage, read the label mapping in [docs/agents/triage-labels.md](docs/agents/triage-labels.md).

### Domain docs

Use one root glossary and one ADR directory. Before exploring the codebase, read [docs/agents/domain.md](docs/agents/domain.md).
