# AGENTS.md

This document provides permanent information/instructions for the convenience of the agents working on this project.

## The Workflow

- 操作 Chrome 时只控制需要的 page，不要再申请整个 Chrome 应用的权限。
- Isaac Sim is located on Olares. It is a cloud platform for computing and simulation. It is used to run Isaac Sim, and the agents can control it through a web interface. Check the Chrome browser for already opened tabs. If there are no tabs open, ask the user to open Olares web interface and log in. The URL is: [https://desktop.qumusphusis.olares.com/](https://desktop.qumusphusis.olares.com/)
- The GUI can be accessed locally with Isaac Sim WebRTC Streaming Client application. The server is 100.64.0.1, ports are 49100 (signal) and 47998 (stream). Note that VPN is required to access the server on Olares in this way. It can be turned on through LarePass application.
- There is a Dobot MG400 robot arm in the physical lab. The user will tell if it is connected or not.
- 排查真机报警或碰撞时，先读 [报警与历史日志](docs/isaacsim.md#报警与历史日志)。历史查询优先读取控制器 SMB 日志。
- When git-committing a task, also write a brief description of what was done.

## Local streaming client automation

- Before reading client state, capturing images, or sending input, read [the client procedure](docs/agents/isaac-client.md). Use `scripts/isaac_client.mjs` with Node.js 22+.
- Target `/Applications/Isaac Sim WebRTC Streaming Client.app`. A mounted installer contains another app with the same bundle ID. The user authorized the temporary loopback inspector for this project. Client operations share a lock.
- Preserve the current macOS focus and system pointer. You can operate a focused client. Do not ask the user to switch applications just to satisfy a background requirement. Capture and input require a visible, unminimized client. Do not automatically activate, raise, restore, or restart the client.
- Send clicks, keys, and text only through the CLI. Do not use `app.click`, OS `pressKey` or `paste`, Quartz input, or `scripts/isaac_client_move.swift`.
- Capture images contain only the remote video. The CLI does not capture the macOS screen. The image excludes client overlays. The CLI rejects clicks on targets covered by client overlays.
- Use the latest capture and its token for each batch. Keep the window geometry unchanged. Batch only actions with known targets and transitions. Inspect the returned image to verify the remote result before reporting success. Use menus or tabs for calibration, never robot action buttons.
- If the CLI returns `CLEANUP_UNCONFIRMED`, stop. Report the error. The client session blocks later calls while this state remains. Do not automatically retry or clear this state.

## Temporary screenshot cleanup

- This rule applies only when a task in this project creates temporary screenshots; it does not require screenshots for every Computer Use action.
- Track the generated temporary file paths or record the screenshot cache baseline before capture. After the task is complete and its result verified, directly delete only screenshots confirmed to belong to this task and no longer needed. Preserve the cache directory structure; moving files to Trash does not reclaim space.
- Preserve requested deliverables, project images, user files, conversation records, and screenshots needed by other active tasks. Keep files whose ownership is uncertain; never indiscriminately clear a shared cache.
- Verify cleanup and report the removed file count and size when material.

## Documenting

- Use instruction documents only for current procedures, interfaces, configuration requirements, and known limitations. This includes `AGENTS.md`, `README.md`, and instruction pages such as `docs/isaacsim.md`.
- Do not add records of individual tasks or experiments to instruction documents. Do not add test dates, execution histories, troubleshooting attempts, or raw output from individual runs. Do not add test counts, measurements, operator confirmations, or final session states from individual runs. Do not add completion notes or links to individual run reports.
- "Useful for future reference," traceability, and task completion do not justify exceptions. Existing logs in an instruction document do not authorize more logs.
- When a verified change affects future use, update the relevant instructions in place. Extract the reusable procedure, interface, configuration requirement, or limitation. Omit the experiment narrative and evidence. Keep configuration values that future users must use to operate the system.
- Report task outcomes in the conversation. If evidence must persist, use an existing log directory or `work/`. Do not create a separate Markdown report unless the user requests one.
- Before saving an instruction document, check every added or changed paragraph. Each paragraph must describe current operation or guide future use. Remove all prohibited task records from your changes before completion. If a task changes no instructions, leave instruction documents unchanged.
- Keep documentation concise. Use clear headings, bullet points, and numbered lists where they help the reader.
- Use Simplified Technical English skill for writing in English.
- Use jianming-zhongwen skill for writing in Chinese.

## UX design

- Avoid unnecessary secondary labels and descriptions. Let the layout do the explaining.

## Agent skills

Local skills are in `.agents/skills/<skill-name>/SKILL.md`, relative to the repository root.

### Issue tracker

Issues and specs use GitHub Issues. Before issue operations, read [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md).

### Triage labels

Before triage, read the label mapping in [docs/agents/triage-labels.md](docs/agents/triage-labels.md).

### Domain docs

Use one root glossary and one ADR directory. Before exploring the codebase, read [docs/agents/domain.md](docs/agents/domain.md).
