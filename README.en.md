[中文](README.md)

# MG400 control

Hardware control project for the Dobot MG400 and PGEA electric gripper.
Control the hardware through an interactive terminal or a web page.
The project also includes a web panel for a stage with 3 motors.

## Choose an interface

Choose an operation guide for your task:

| Goal | Guide and description |
| --- | --- |
| View state and enter control commands in a terminal | [Interactive terminal](docs/terminal-control.en.md). Run `uv run mg400-robot` |
| Control the robot and gripper through a web page or keyboard, or view state in read-only mode | [Web panel](docs/control-panel.en.md) |
| Control the stage's 3 motors through a web page or keyboard | [Stage panel](docs/stage-panel.en.md) |

Before hardware operation, read the prerequisites and stop instructions in your selected guide.

## Deployment, troubleshooting, and development

| Task | Document |
| --- | --- |
| Install, start, or update services, or migrate to a new host | [Host deployment](docs/deploy.en.md) |
| Configure Olares networking, get code, and troubleshoot USB gripper and camera faults | [Olares configuration and maintenance](docs/olares.en.md) |
| Query current robot alarms or historical logs | [Alarms and historical logs](docs/hardware.en.md#alarms-and-historical-logs) |
| Change code, run checks, and read implementation details | [Development reference](docs/development.en.md) |
| Read about the hardware TCP protocol and action completion checks | [Hardware control interface](docs/hardware.en.md) |
