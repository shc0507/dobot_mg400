[中文](control-panel.md)

# Robot service

[Return to the project home page](../README.en.md)

The robot service (`apps/control_panel/server.py`) controls the MG400 and the vacuum suction cup.
It uses the Python 3.12+ standard library and provides an HTTP interface and a WebSocket connection.
Operators control the robot in the Motion workspace of the [Lab Console](dashboard.en.md).
The recipe runner, command-line tools, and the Python client use the same interface.

## Before use

The service uses real-robot control mode by default.
Starting the service does not connect to the robot or send motion commands.
The service connects to the robot only after a client takes control (`start_sync`).
Before use, close the interactive terminal and other control programs.
Connections can interrupt each other when multiple programs query the controller.

The robot service and interactive terminal use a file lock to limit real-robot connections to one program per robot.
Read-only mode also uses this lock.
See [Panel connection lock](deploy.en.md#panel-connection-lock) for the lock directory and permission requirements.
This lock coordinates only this project's programs. It cannot prevent DobotStudio or another host from connecting.

## Control the robot

Keep the LarePass VPN connected and open the Lab Console (`http://100.64.0.1:8080/`).
On the robot card of the Motion workspace, use this sequence:

1. Click `Take control` and wait for measured coordinates.
2. Click `Enable` to enable the robot.
3. Select a movement method:
   - Keyboard jog: turn on `Keyboard jog`, then hold a key to move. See [Lab Console](dashboard.en.md) for the keys and the speed.
   - Jog buttons: open `Jog buttons`, select the step 0.5, 2, or 10, then click a direction button.
   - Move to a pose: click `Use actual`, change X/Y/Z (mm) and R (degrees), set the speed, then click `Execute`.
   - Home: click `Home…` and confirm. J1–J4 move to 0°.
4. Click `Stop arm` to stop the robot at any time.
5. When you finish, click `Release`.

Each jog adds 1 step on one axis to the measured pose. The step unit is mm for X/Y/Z and degrees for R.
Coordinates use the base frame and flange origin (`User=0,Tool=0`).
The console checks that the fingertip target is in the workspace. Then it sends 1 `execute` command and does not retry.
See [Lab Console](dashboard.en.md) for the workspace bounds and tool offset.
The service then checks the target with the controller's inverse kinematics and the existing joint range checks.

Clients can supply an integer `speed` with the `execute` command. The console speed input sets this value.
The host setting `MG400_ROBOT_MAX_SPEED_L` limits this value. The service rejects values above the cap with 400.
The movement uses `SpeedL=<speed>,AccL=<min(speed, acceleration cap)>`.
The acceleration cap is `MG400_ROBOT_MAX_ACC_L`.
Without `speed`, the command remains `SpeedL=1,AccL=1`, even with higher caps.

Both settings default to 1 and accept integers from 1–100. Empty settings also use 1.
Set them in `/etc/mg400/mg400.env`. Restart the service after a change.
Invalid settings prevent startup.

The `home` command moves J1–J4 to 0° with `SpeedJ=5,AccJ=5`.
The speed caps do not change `home` or the global `SpeedFactor`.
An operator must validate speeds above 1 on hardware before unattended transfers use them.

## Control the suction cup

The Motion workspace of the [Lab Console](dashboard.en.md) has **Suck** and **Drop** (`release`). In keyboard jog, Z sucks and X drops. A client uses the `vacuum` command, for example the [Python client](development.en.md#python-client):

```python
client.run("vacuum", operation="suck")
```

`operation` is `suck`, `release` or `off`. The robot must be idle.
The `vacuum` field of the state object gives the measured DO levels. See [Vacuum suction cup](development.en.md#vacuum-suction-cup).
Stop and disable end the blow. They keep the suck output.

## Stop and recover

`STOP ALL MOTION` at the top of the console sends a stop to all devices. It does not need control.
It requests a controlled stop. It is not a physical emergency stop.
If a stop or command result cannot be confirmed, use the physical emergency stop on site.

The console `Release` sends `disable` first and waits up to 12 seconds. Then it sends `stop_sync`.
If the console page sends no heartbeat within 3 seconds, the console releases control in the same sequence.
The service disconnects feedback and unlocks control only after it confirms disabled mode 4 and rest.
If the robot was already disabled and idle, disabled mode 4 is sufficient. You can move a disabled arm by hand, and a `Disable` or `Release` does not lock control.
Stale feedback cannot confirm a stop. If the service reports an unconfirmed stop, use the physical emergency stop on site.
If the service recorded no unconfirmed stop during a brief feedback gap, fresh feedback lets it confirm a new explicit `disable` request.

If the feedback reader fails, the stop stays unconfirmed and the connection lock stays held.
The feedback reader does not start again. `Recover` and `Clear alarm` both need fresh feedback.
In this case, check the robot state, then run `sudo mg400ctl restart web`.

For clients that connect directly to the service, such as `RobotClient` and the interactive terminal, `stop_sync` does not disable the robot.
If the robot is enabled, `stop_sync` stops it and releases control, but keeps feedback connected and control locked.
The client can send `start_sync` again to cancel the pending disconnect. Then it sends `disable`, then `stop_sync`.
If the robot is already disabled, `stop_sync` confirms disable, then disconnects.
When a `RobotClient` `with` block ends, it sends `stop_sync` if it holds control. A client created with `keep_control=True` does not send it.
After a confirmed stop, send an explicit `disable` to complete the disconnect without restarting the service.

Only one client holds control at a time.
If the holder does not renew within 3 seconds, the service requests a robot stop and locks control.
The console page sends a heartbeat every 0.5 seconds. The console renews control on each heartbeat.
If the page closes or sends no heartbeat within 3 seconds, the console releases control.

When you press Ctrl+C in the service terminal or stop the systemd service, the service first requests a stop and waits for confirmation.
It then exits.
Repeated signals do not interrupt stop confirmation during this wait.

If the process is forcibly terminated, crashes, loses power, or encounters a network fault, the stop command may not arrive.
The robot may continue to the endpoint of the trajectory already sent.

After a confirmed stop, you can disable and re-enable the robot.
Taking control again does not unlock an unknown command result.
If stopping cannot be confirmed during disconnection, the service stops normal coordinate queries.
It retains feedback, the connection lock, and the warning. Another program cannot connect to the robot.
After the 3-second lease expires, another client can take control, but it cannot move the robot before you click `Recover`. Check the robot, then click `Recover`.
Do not delete the lock file to release ownership.
After deletion, 2 processes can each hold their own lock.

### Clear alarms

During a controller alarm, `Mode` shows 9 and the controller rejects disable and stop commands.
An alarm stops robot motion.
The service locks because the stop is unconfirmed. It lists alarm numbers, descriptions, and corrective actions.
The descriptions come from Dobot's alarm tables.

Use this recovery sequence:

1. Remove the cause according to the alarm's corrective action.
2. Click `Recover` on the robot card.
3. Wait for the service to report that the alarm is cleared and confirm that the robot is disabled and stationary.

While the service is locked, the button reads `Recover`. Otherwise, it reads `Clear alarm`. An alarm locks the service, so the button reads `Recover` during an alarm.
Both buttons send `clear_alarm`.
`clear_alarm` sends only `ClearError()`. It does not send `Continue()`.
Motion paused before the alarm does not resume.
After the alarm clears, the service disables the robot through a new control instance and confirms that it is stationary.
Control unlocks only after both are confirmed.

If the cause remains, the alarm immediately returns and control stays locked.
Joint limit alarms, such as 69 and 72, can clear only after the robot returns within its limits.
Use DobotStudio Pro or the teach pendant to jog it back.
After jogging back, check the controller mode as described in [Switch to TCP/IP mode from the server](hardware.en.md#switch-to-tcpip-mode-from-the-server).

When collision detection triggers, the alarm list shows `-2` (Collision detected).
If the alarm clears outside the console, for example with the robot's Collision Resume button, check the robot state, then click the button (`Recover` or `Clear alarm`).
The service disables the robot through a new control instance and confirms that it is stationary, then unlocks control.

An unconfirmed stop without an alarm shows a warning, and the button reads `Recover`.
Check the robot state, then click `Recover`. The service disables the robot through a new control instance, confirms that it is stationary, and clears the warning.
A service restart is not necessary, except after a feedback reader failure.
While a stop is unconfirmed, the service message shows the recovery procedure. A later successful stop does not replace this message.

### Controller not in TCP/IP mode

In `tp` (online mode), the controller returns `-1` for all commands on port `29999`.
The `30004` feedback continues to work.
After you connect, the first coordinate query `GetPose(0,0)` fails. The service requests a stop and locks control.
The controller also rejects the stop and disable commands.
A `-1` reply does not confirm a stop. Control stays locked.

When the controller returns `-1`, the service reads the feedback.
If the robot is disabled and stationary without an alarm, the service message tells you that the controller is probably not in TCP/IP mode.
In all other conditions, the service message shows the original error.
In that case, use the physical emergency stop as described in [Stop and recover](#stop-and-recover).

The mode switch script must get the [panel connection lock](deploy.en.md#panel-connection-lock). Thus, stop the robot service first.
Use this recovery sequence:

1. Make sure that the robot is stationary.
2. Run `sudo mg400ctl mode` and read the controller mode.
3. Run `sudo mg400ctl stop web`.
4. Run `sudo mg400ctl mode --switch-to-tcp`.
5. Run `sudo mg400ctl start web`, then click `Take control` in the console.

For the switch conditions, see [Switch to TCP/IP mode from the server](hardware.en.md#switch-to-tcpip-mode-from-the-server).

## Read-only feedback

Use this command for read-only mode:

```sh
python3 apps/control_panel/server.py --mode read-only --robot-host 192.168.1.6
```

Only the `start_sync` command connects to TCP `30004`. `stop_sync` stops reading.
This mode provides only measured joints, controller mode, and alarm flags.
It provides no coordinate feedback and accepts no motion commands.

## Service configuration

The service is `mg400-web-panel.service`, installed by [Host deployment](deploy.en.md). Its `mg400ctl` name is `web`.
Start or check the service on the host:

```sh
sudo mg400ctl start web
sudo mg400ctl status
```

For a local start, enter the project directory on a host that can reach the robot and run:

```sh
MG400_CONTROLLER_LOCK_DIR=/var/lib/mg400/locks python3 apps/control_panel/server.py
```

`MG400_CONTROLLER_LOCK_DIR` sets the connection lock directory. If it is unset, the directory is `/tmp/mg400-controller-locks`.
If other programs use `/var/lib/mg400/locks`, the local service must set the same directory. Otherwise it cannot exclude them.

If you omit `--mode`, the service uses `robot` mode.
The robot address defaults to `MG400_ROBOT_HOST`, or to `config/robot.toml` if that variable is unset. Use `--robot-host` to specify it.

During service shutdown, the service can take about 60 seconds to confirm that the robot stopped.
The service's `TimeoutStopSec` is 90 seconds, which is longer than this interval.

By default, the service listens on `127.0.0.1:8766`.
`MG400_PANEL_BIND` and `MG400_PANEL_PORT` override the default. `--bind` and `--port` override the environment variables.
Empty values use the default. If the port is not an integer from 1–65535, the service does not start.
The Lab Console and the recipe service connect to this service through `MG400_ORCH_ROBOT_URL` (default `http://127.0.0.1:8766/`). If you change the address or port, also change this setting. See [Recipe orchestrator](orchestrator.en.md#service-settings).
The service checks no token, `Host`, or `Origin`. The VPN is the access boundary.

Set the path prefix with `--base-path`, for example `--base-path /mg400-panel/`. All interface routes use this prefix.
If a reverse proxy is in front of the service, the proxy must meet these requirements:

- Forward `api/ws` with HTTP/1.1, including the `Upgrade` and `Connection` request headers.
- Set the read timeout above 5 seconds. The service pushes state at least once per second and sends a ping every 5 seconds.

See [Robot service interface](development.en.md#robot-service-interface) for the interface reference.

After updating code, run the installer as described in [Host deployment](deploy.en.md#installation-and-updates), then run `mg400ctl restart web`.

### Service logs

The service writes these events to standard error:

- Each change in the service message
- Control locking and unlocking
- Unconfirmed stop or disable results, and clearance of those states
- Controller alarm numbers and alarm clearance
- Changes to mode and alarm flags in robot feedback

The service also records the result of each command.
It does not log heartbeats or state pushes.
A separate thread writes the logs. Blocking standard error does not delay a stop.

Read the logs on the Olares host:

```sh
sudo mg400ctl logs web -n 200
```

When control is locked, find the first `Stop or disable unconfirmed.` in the logs.
The next robot message line gives the reason the stop or disable failed.
