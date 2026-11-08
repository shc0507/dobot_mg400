# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Primary:** the lab owner and the lab members. They operate the devices by hand and test them, usually from a laptop next to the devices.
- **Long-term:** the same people monitor runs, cameras and the furnace remotely over the VPN. Remote monitoring is a goal, not the current main use.
- The users know the lab and its devices. Some lab members know the devices less well than the owner.

## Product Purpose

WuLab is a lab automation stack on one host (Olares). The **WuLab Console** is the only operator web page. It controls and shows the Dobot MG400 arm and suction cup, the 3-axis stage, camera1 to camera4, the thermal camera, the furnace and gas, and the microscope (Toupcam).

- **Current stage:** a test bench. The owner brings each device up and tests it freely.
- **Next stage:** automated experiments. The robot moves a chip from a tray into the crucible, the stage carries the crucible into the furnace, the furnace heats it under gas, and the microscope images the chip through the furnace window. Each run records its steps, chip identities, samples and artifacts.
- **Success:** an operator can see the state of every device at once, take control of 1 device, operate it, and stop all motion at any time without doubt about the result.

## Positioning

- One console for all lab devices, built on 1 shared service contract (`api/describe`, leases, events). It is not a set of separate vendor panels.
- Control is explicit and per device: 1 controller per device through a lease, and the console never takes a lease from another holder.
- Stops need no control and are never blocked. The console reports each stop target result separately and never infers that all motion stopped.
- Each chip has a passport that shows only the records saved for that exact chip.

## Operating Context

- Access is over the VPN at `http://100.64.0.1:8080/` (the console is at `/lab/`). The VPN is the access boundary. The console checks no token.
- The physical emergency stop must stay within reach during hardware operation. A web button is not a safety interlock.
- Workspaces: **Overview** (run, events, safety, agent proposals), **Motion** (robot arm, cameras, stage, keyboard jog), **Cameras** (6 views), **Furnace + microscope**.
- Demo mode runs the console offline with simulated devices. Agents use rehearsal only.
- Other pages: the chip passport (`/chip`). Instrument services also have their own panels.
- An MCP agent gateway can read state and propose catalog recipes for human approval. It cannot approve or start runs or command devices. The console shows only the count of waiting proposals.

## Capabilities and Constraints

- **Language:** every UI string is in English and 中文. The browser keeps the selected language. Both languages must fit the same layout.
- **Screen:** laptop is the real-use screen. The Motion workspace stacks at phone width, but phones and tablets are not a target.
- **Test stage rules:** keep changes minimal. Do not add settings, tokens, access checks, task cards or review processes. Use fixed defaults in code.
- **Protections to keep:**
  - Stop and STOP ALL work at any time, need no control and are never blocked.
  - 1 controller per device at a time, through the lease.
  - Furnace: the maximum temperature and ramp caps, heat off and gas off.
  - Robot: the speed cap, and no move while the controller reports an alarm.
  - Microscope: the focus limits, and full turret switches only at the clearance position.
  - Stage: soft limits, when they are configured.
- **Outcomes:** a command is sent 1 time. An unknown outcome shows as unknown and is not retried. Status older than 3 seconds shows as stale.
- **Implementation:** server-rendered Flask pages with plain JavaScript and CSS (`src/orchestrator/web/`). Workspace CSS is prefixed `#ws-<id>` and uses the shell classes and colors from `shell.css`. See `docs/agents/console-design/CONTRACT.md`.
- **Not built yet:** console controls to hold, abort or recover a run, and to approve agent proposals.
- **Terminology:** use the terms in `GLOSSARY.md` (for example chip, not wafer; lease; station; clearance position).

## Brand Commitments

- Name: **WuLab Console** (`console.name`). The header shows a line-drawn robot-arm mark and the deployed commit.
- UI copy: avoid unnecessary secondary labels and descriptions. Let the layout do the explaining.
- English copy follows Simplified Technical English. Chinese copy follows 简明中文.
- Safety labels stay literal and visible, for example **STOP ALL MOTION**, **Stop heat and gas** and **Stop all gas**.

## Evidence on Hand

- Console design: `DESIGN.md` and the shipped pages in `src/orchestrator/web/static/console/`.
- Operator docs: `docs/dashboard.en.md` (console and passport), `docs/orchestrator.en.md` (recipes and runs).
- Recipes: `recipes/tray-transfer.toml`, `recipes/example-rehearsal.toml`.
- Real experiment results, images or chip records for display are not in the repository. Do not invent them.

## Product Principles

1. **Safety reads first.** Stop controls and device state are always visible and never depend on control or a lease.
2. **Show the truth, including uncertainty.** Show pending, unconfirmed, stale and unknown as they are. Never imply success that a service did not confirm.
3. **Explicit control.** The operator always sees which devices this page controls, and takes control on purpose.
4. **Minimal at the test stage.** Prefer fixed defaults and few controls over settings and process.
5. **Ready for remote use.** The state of a device must be clear without a view of the bench, so that remote monitoring works later.
