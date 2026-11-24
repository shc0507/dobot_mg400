[中文](README.md)

# MG400 control

Hardware control project for the Dobot MG400 with a vacuum suction cup.
Control the hardware through an interactive terminal or the Lab Console.
The Lab Console is the only operator web page. It also controls the 3-motor stage, the cameras, the furnace, and the microscope.

## Connect an external agent

Start with the [external agent integration pack](docs/agent-connect/README.md). A human prompts the coding agent to read the project knowledge and communication contract, then adapt its own code before operation. The agent-facing files are in English.

Copy this prompt into your coding agent with access to this repository and your external agent's repository:

```text
Adapt my external agent to this Lab Console in the external agent's own repository.
Read docs/agent-connect/project-knowledge.md first, then docs/agent-connect/communication-contract.md in the Console repository.
Use the first file for operating context and the second for the required communication behavior.
Include the relevant project rules in my agent's runtime instructions.
Read linked source files only when an implementation detail is unclear. Ask me for missing experimental goals or physical context.
Implement state reads, compatibility checks, pairing, operator control, commands, result tracking and independent Stop as documented.
Keep the Console authoritative. Do not change its code to fit my agent.
Complete adaptation and simulated checks before operation. This task does not authorize equipment commands or live Stop tests.
At runtime, check definitions and read current values. If the interface is unsupported, report it for human-directed adaptation instead of rewriting code during operation.
Report the changed files, supported interfaces, checks and remaining gaps. Never put credentials in source files or model prompts.
```

## Choose an interface

Choose an operation guide for your task:

| Goal | Guide and description |
| --- | --- |
| Control the robot, suction cup, and stage, view the cameras, and operate the furnace and microscope in a web page | [Lab Console](docs/dashboard.en.md#lab-console). Open `http://100.64.0.1:8080/` |
| Read about robot and suction cup control, stop, and recovery, and configure the robot service | [Robot service](docs/control-panel.en.md) |
| Read about stage control and stop, and configure the stage service | [Stage](docs/stage-panel.en.md) |

Before hardware operation, read the prerequisites and stop instructions in your selected guide.

## Deployment, troubleshooting, and development

| Task | Document |
| --- | --- |
| Install, start, or update services, or migrate to a new host | [Host deployment](docs/deploy.en.md) |
| Configure Olares networking, get code, see the USB device layout, and troubleshoot camera faults | [Olares configuration and maintenance](docs/olares.en.md) |
| Query current robot alarms or historical logs | [Alarms and historical logs](docs/hardware.en.md#alarms-and-historical-logs) |
| Change code, run checks, and read implementation details | [Development reference](docs/development.en.md) |
| Read about the hardware TCP protocol and action completion checks | [Hardware control interface](docs/hardware.en.md) |

## License

This repository is licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0), the same license as the vision model and `ultralytics`. Vendored third-party files keep their own terms: `src/thermal_cam/_vendor/` is Apache-2.0, and `src/toupcam_camera/toupcam.py` comes from the ToupTek SDK.
