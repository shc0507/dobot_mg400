[中文](README.md)

# MG400 control

Hardware control project for the Dobot MG400 with a vacuum suction cup.
Control the hardware through an interactive terminal or the Lab Console.
The Lab Console is the only operator web page. It also controls the 3-motor stage, the cameras, the furnace, and the microscope.

## Choose an interface

Choose an operation guide for your task:

| Goal | Guide and description |
| --- | --- |
| View state and enter control commands in a terminal | [Interactive terminal](docs/terminal-control.en.md). Run `uv run mg400-robot` |
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
