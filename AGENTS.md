# AGENTS.md

This document provides permanent information/instructions for the convenience of the agents working on this project.

## The Workflow

- 操作 Chrome 时只控制需要的 page，不要再申请整个 Chrome 应用的权限。
- Olares is a cloud host. It runs the web control panel, the USB gripper collector, and the camera preview. Agents can control Olares through its web interface. Check the Chrome browser for tabs that are already open. If no tab is open, ask the user to open the Olares web interface and log in. The URL is: [https://desktop.qumusphusis.olares.com/](https://desktop.qumusphusis.olares.com/)
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
