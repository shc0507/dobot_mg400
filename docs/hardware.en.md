[中文](hardware.md)

# Hardware control interface

[Return to the home page to choose an interface](../README.en.md)

This page describes the TCP protocol and feedback checks for hardware control.
Existing hardware functions connect directly through TCP. They do not provide a general pick-and-place interface.
For routine operation, use the relevant guide:

- [Web panel](control-panel.en.md): hardware control through a standalone web page.
- [Interactive terminal](terminal-control.en.md): terminal commands for the robot and gripper.

The web panel and interactive terminal share the robot address configuration.
See `MG400_ROBOT_HOST` in [Configuration file](deploy.en.md#configuration-file).

## Direct TCP interface

- [MG400 / M1Pro 4-axis Python SDK](https://github.com/Dobot-Arm/TCP-IP-4Axis-Python)
- [SDK source](https://github.com/Dobot-Arm/TCP-IP-4Axis-Python/blob/main/dobot_api.py)
- [4-axis TCP/IP protocol repository](https://github.com/Dobot-Arm/TCP-IP-Protocol-4AXis)
- [4-axis protocol, English version dated 2024-04-19](https://github.com/Dobot-Arm/TCP-IP-Protocol-4AXis/blob/master/TCP_IP%20Remote%20Control%20Interface%20Guide%20%284axis%29_20240419_en.pdf)

The Python README above specifies controller V1.5.6.0+.
For troubleshooting, trying V1.6.0.0+ is recommended.
The official manual for the installed firmware is the final reference.
Before control, enter TCP/IP secondary development mode.
Select it in DobotStudio Pro or use the script below.
Do not mix in the 6-axis robot SDK.

### Switch to TCP/IP mode from the server

[`scripts/mg400_control_mode.py`](../scripts/mg400_control_mode.py) is read-only by default.
It reads the mode through SMB (network file sharing) and the operating state through HTTP.
`configured_mode` is the saved configuration. It cannot by itself prove that the mode is active.
`tp` means online mode. `tcp` means TCP/IP mode.

The server must reach the robot's TCP ports `445`, `22000`, and `29999`.
On a host installed through [Host deployment](deploy.en.md), read the mode as root:

```sh
mg400ctl mode
```

`mg400ctl` runs the script as UID 1000 and shares the panel's [connection lock](deploy.en.md#panel-connection-lock).
The script uses `pysmb` in `/opt/mg400/.venv`. No separate dependency installation is required.
When run directly as root, the script refuses to acquire the connection lock and does not connect to the controller.

Before switching, disable the robot and stop running programs.
Close other control clients and release the [panel connection lock](deploy.en.md#panel-connection-lock).
Then run `mg400ctl mode --switch-to-tcp`.
The script reads `MG400_ROBOT_HOST`. If it is unset, the script reads `config/robot.toml`.
Switching accepts only an original mode of `tp` or `tcp`.
It checks that the robot is disabled, programs are stopped, and no alarms are active.
The script first backs up the configuration, then writes `/settings/function/remoteControl.json` in the `project` share.
It then sends the same configuration by POST to `/settings/function/remoteControl` on port `22000`.
The target fields are `{"mode":"tcp","name":""}`. Other fields keep their original values.
This order comes from the [DobotLink source](https://github.com/Dobot-Arm/DobotLink/blob/80c6ad78010721648ad671cc47320604d303f1d9/Plugins/IndustrialRobotPlugin/Device.cpp#L3239).

Success requires HTTP `status: true`, matching configuration readback, and a successful `RobotMode()` response of `4`.
The script does not enable the robot, clear alarms, or send motion commands.
`mg400ctl` stores backups in `/var/lib/mg400/work/control-mode/`.
If an error occurs after writing, the controller state may already have changed.
The script reports the backup path. It does not automatically repeat the switch request or roll back.
Check the state and error before deciding whether to run it again.
This procedure depends on interfaces used by the official client. After a firmware upgrade, check compatibility again.

### Official SDK interface

| Operation | Official 4-axis interface / description |
| --- | --- |
| Open a control connection | `DobotApiDashboard(ip, 29999)` |
| Open a motion connection | `DobotApiMove(ip, 30003)` |
| Feedback connection | `DobotApi(ip, 30004)`. Split the binary frames correctly |
| Read the pose | `dashboard.GetPose()`. XYZ in millimeters, R in degrees |
| Linear motion | `motion.MovL(x, y, z, r, *params)` |
| Wait for the queue to complete | `motion.Sync()`. Also check fresh pose data and controller state |
| Controlled-stop mapping reference | `dashboard.ResetRobot()`. The manual describes this as stopping and clearing the planning queue |
| Electric gripper | Depends on the gripper's specific I/O or communication protocol |

The protocol supports `GetPose(User=0,Tool=0)`, but the Python wrapper method `GetPose()` above accepts no arguments.
You must explicitly configure or validate the current User, Tool, and TCP.
Do not assume that returned data uses the same coordinate frame as the scene.
Official `MovL` optional arguments are strings, such as `"User=0"`. They are not arbitrary Python keyword arguments.

## Completion checks and failure handling

A success code in the TCP response `ErrorID,{values},Command(...);` only means that the controller accepted the command.
An acknowledgement for queued motion does not mean that the robot reached its destination.
After sending a motion command, parse the success response and wait for the queue to complete.
Then check fresh pose data and state. Handle alarms and timeouts.
The official example transport code does not by itself guarantee reliable completion.

After a timeout or disconnection, the action may already be partly executed.
Stop the session and check the physical state again. Do not automatically repeat motion commands or clear alarms.
`SpeedFactor` is a percentage relative to the controller setting. It is not an absolute speed in mm/s.
A software stop command is not equivalent to an independent hardware emergency stop.
A real system must implement speed limits, travel limits, emergency stops, and fault monitoring at the device or controller level.

After opening the electric gripper, you must confirm completion. Treat unconfirmed completion as failure.
After closing the gripper, read the feedback that indicates an object is gripped.
Completion of closing alone does not prove that the gripper holds an object.
Without grip detection, you must add external detection or explicitly keep the state unverified.

## Alarms and historical logs

`mode=9, error=True` only indicates a fault state. It contains no specific alarm code.
`GetErrorID()` only reads current alarms. It cannot reconstruct the initial fault.
`control_status.json` is overwritten repeatedly and cannot serve as a historical record.

To find the initial fault, investigate in this order:

1. Open a terminal on a host that can reach the controller, such as the Olares host.
2. Read controller logs through SMB (network file sharing).
3. Check collision, pause, and emergency-stop records in log order.

The controller address is in `config/robot.toml`. It is currently `192.168.1.6`.
SMB uses TCP `445`, the `project` share, and the `/logs` directory.
This path is independent of dashboard queries. You do not need to close the web panel.
During read-only investigation, do not clear alarms, enable the robot, or send motion commands.

| Path in the controller share | Purpose |
| --- | --- |
| `/logs/channel_0` | Controller events, alarm states, and command return values |
| `/logs/algs/algs_info` | Motion algorithm events, including timestamped collision detection records |
| `/logs/gcontrol/log`, `/logs/codesys/log` | Additional control-program records to check |
| Files with suffixes such as `.1` and `.2` in the same directory | Rotated logs: older logs saved in separate files |

Read the first 2 files first.
If they do not cover enough time, select older logs from the directory listing.
Use file modification times only to filter files. Use log contents for event times.

The commands below connect to the share anonymously, list files, and save copies of 2 logs.
They use `/opt/mg400/.venv` from [Host deployment](deploy.en.md), which already contains `pysmb`.

```sh
export MG400_LOG_DIR=$(mktemp -d /tmp/mg400-logs.XXXXXX)
/opt/mg400/.venv/bin/python - <<'PY'
import os
from pathlib import Path
from smb.SMBConnection import SMBConnection

robot_ip = "192.168.1.6"  # 与 config/robot.toml 保持一致
output = Path(os.environ["MG400_LOG_DIR"])
connection = SMBConnection(
    "", "", "mg400logreader", "MG400",
    use_ntlm_v2=True, is_direct_tcp=True,
)
try:
    assert connection.connect(robot_ip, 445, timeout=8)
    for directory in ("/logs", "/logs/algs"):
        for entry in connection.listPath("project", directory, timeout=15):
            if not entry.isDirectory:
                print(directory, entry.filename, entry.file_size, entry.last_write_time)
    for source in ("/logs/channel_0", "/logs/algs/algs_info"):
        destination = output / source.strip("/").replace("/", "__")
        with destination.open("wb") as stream:
            connection.retrieveFile("project", source, stream, timeout=20)
        print(destination)
finally:
    connection.close()
PY
grep -rn -C 2 -E 'collision!|collisionStatus|碰撞检测|alarm ID:' "$MG400_LOG_DIR"
grep -rn -A 14 'func = geterrorid' "$MG400_LOG_DIR"
```

Change the `source` list according to the directory listing to read older or additional logs.
If anonymous authentication fails, you can retry with username `Guest` and an empty password.
After the query, remove only the temporary directory created for this query.
Save evidence that you must keep in `work/` under the data directory. Do not put it in operating documents.

Keep these distinctions when interpreting the logs:

- `collision!`, `collisionStatus`, and "碰撞检测 Pause" are used to check collision detection and pauses.
  These records do not mean that you obtained a numeric alarm code.
- Alarm `85` means that the hardware emergency-stop button was pressed or a software emergency stop was triggered.
  A later `85` cannot replace the initial event.
- The controller clock and panel log clock may differ.
  Keep the original timestamps. First compare order and intervals within the same controller.
  Before aligning different logs, check time zones and clock offsets.

To query current alarm codes, disconnect the web panel and interactive terminal first.
Then connect separately to `29999` and call `GetErrorID()`.
Panel polling and external queries may disconnect each other's connections.

Path reference: [MG400 log export instructions](https://techshare.co.jp/faq/dobot/mg400/support.html).
Alarm code reference: [Dobot 4-axis alarm table](https://raw.githubusercontent.com/Dobot-Arm/TCP-IP-4Axis-Python/main/files/alarm_controller.json).

## Hardware validation

Validate in this order:

1. Check firmware, robot IP, gripper model, power supply, and I/O or Modbus communication parameters. Use read-only tests for pose and alarms.
2. Set the tool TCP and payload. Use the teach pendant to record the actual start, pick, and place points and path.
3. Validate individual actions gradually at low speed in a controlled workspace. Then validate a fixed pick-and-place sequence.
4. Before connecting an agent, add path reachability checks, collision checks, and device protection independent of the model.

## Optional ROS 2 integration

For ROS 2, see [HarvestX/MG400_ROS2](https://github.com/HarvestX/MG400_ROS2).
The interface descriptions here use the `humble` branch commit
[`1a8c7344be44c52030f7216c0f387873ed91fd11`](https://github.com/HarvestX/MG400_ROS2/tree/1a8c7344be44c52030f7216c0f387873ed91fd11).

The upstream [README](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/README.md)
lists Ubuntu 22 + ROS 2 Humble and MG400 firmware 1.5.6.0.
The repository provides a robot model, RViz display, driver nodes, motion actions, and control services.
`display.launch.py` can display the model without hardware. This is not physical simulation or complete obstacle-avoidance planning.

This project currently has no ROS 2 backend.
The names below use the default namespace from `main.launch.py`: `mg400`.
If you change the namespace, change the interface names accordingly.

| Operation | HarvestX interface / integration requirement |
| --- | --- |
| Linear motion | `/mg400/mov_l`, type `mg400_msgs/action/MovL`. Wait for the final action state and check `result` |
| Read the pose | Get fresh pose data from `/mg400/joint_states`, TF, connection state, and alarm state. Check the relationship between the flange and gripper TCP |
| Controlled stop | Evaluate the `/mg400/reset_robot` service and check stop feedback. Sending action cancel alone is insufficient |
| Electric gripper output | `/mg400/tool_do_execute`. Use only when tool digital I/O actually controls the gripper |
| Electric gripper input | `/mg400/tool_di`. Interpret input signals according to the gripper manual. A successful read does not mean a successful grip |

Integration requirements and limitations:

1. Unit conversion: this project uses mm / deg. `MovL` uses meters and quaternions in `PoseStamped`.
   The driver takes yaw in radians, then converts it to device protocol units.
   The adapter must convert lengths and angles and specify the correct TF frame.
   Source: [Motion command implementation](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_interface/src/commander/motion_commander.cpp).
2. Cancellation and stop: in this version, `MovL::handle_cancel` only returns acceptance. Stopping is still TODO.
   The timeout / abort branches do not actively send a stop command either. These branches cannot establish that the robot stopped.
   Its execution path also has a branch that returns after a send exception without ending the action.
   The client must have its own deadline and fault handling.
   Source: [MovL implementation](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_plugin/src/motion_api/mov_l.cpp).
3. Recheck arrival accuracy: this version of `MovL` uses pose error and ENABLE state to determine completion.
   The position threshold is 5 mm per axis. The angle threshold is 0.0174 rad. Grasping tasks must use their own acceptance accuracy.
   The same source has a fixed execution deadline of 10 seconds.
   Do not assume that a client deadline automatically reaches the server.
4. Gripper adaptation: the repository has tool I/O services but no grip confirmation for arbitrary electric grippers.
   If the gripper uses Modbus or another communication protocol, you must implement that protocol. You cannot directly reuse digital output.
   Sources: [ToolDOExecute](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_plugin/src/dashboard_api/tool_do_execute.cpp),
   [ToolDI](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_plugin/src/dashboard_api/tool_di.cpp).

Before implementing a ROS 2 adapter, decide the deployment environment.
This project requires Python 3.12+.
`rclpy` must be compatible with the Python environment of the installed ROS distribution.
Do not assume that the current macOS venv can be used directly.
First validate the driver in the upstream-supported Ubuntu / Humble environment.
Then decide whether the agent runs in the same process or communicates through a separate bridge process.
