[中文](control-panel.md)

# Web panel

[Return to the project home page](../README.en.md)

The web panel directly controls the MG400 and PGEA gripper.
It uses the Python 3.12+ standard library and provides real-robot control and read-only feedback.
The page receives live state and sends commands and key input through a WebSocket connection.
Real-robot control mode supports keyboard jogging.

## Before use

The service uses real-robot control mode by default.
Starting the service and opening the page do not connect to the robot or send motion commands.
Before use, close the interactive terminal and other control programs.
Connections can interrupt each other when multiple programs query the controller.

The web panel and interactive terminal use a file lock to limit real-robot connections to one program per robot.
The web panel's read-only mode also uses this lock.
See [Panel connection lock](deploy.en.md#panel-connection-lock) for the lock directory and permission requirements.
This lock coordinates only this project's panels. It cannot prevent DobotStudio or another host from connecting.

## Open the panel

### Use the service deployed on Olares

Keep the LarePass VPN connected.
The VPN access rules must allow TCP `8766`.
Open the [Olares web panel](http://100.64.0.1:8766/).

If an authentication dialog appears, enter the access token and click `Connect`.
Run `sudo mg400ctl token web` on the host to read the access token.
The local backup is `work/.web-panel-token`. This file is not tracked in Git.
If the service is not running, see [Service configuration](#service-configuration).

### Start locally

On a host that can reach the robot, enter the project directory and run:

```sh
python3 apps/control_panel/server.py
```

Open the [local web panel](http://127.0.0.1:8766/) on that host.
If you omit `--mode`, the service uses `robot` mode.
The robot address defaults to `MG400_ROBOT_HOST`, or to `config/robot.toml` if that variable is unset. Use `--robot-host` to specify it.

## Control the robot

Use this sequence:

1. Click `Connect & take control` and wait for measured coordinates.
2. Click `Enable robot` to enable the robot.
3. After the robot is enabled, click `Use actual`.
4. Change X/Y/Z (mm) and R (degrees) in `Target`.
5. Click `Execute targets` and wait for confirmation that the robot reached the target.

`Use actual` copies the measured coordinates to the target.
Coordinates use the base frame and flange origin (`User=0,Tool=0`).
The sliders adjust values near the measured position. Their ranges are not the robot's physical limits.
Before execution, the program checks the target with the controller's inverse kinematics and the existing joint range checks.

`Home` moves J1–J4 to 0°.
Linear motion uses `SpeedL=1,AccL=1`. Homing uses `SpeedJ=5,AccJ=5`.

## Control the gripper

The gripper uses a USB collector on the same host.
The collector must use `--commands` and must share the web service's monotonic clock.
[Host deployment](deploy.en.md) installs the collector service. The web service reads its feedback file through `--gripper-feedback`.
For a local start, specify the feedback file as follows:

```sh
python3 apps/control_panel/server.py --mode robot \
  --gripper-feedback /var/lib/mg400/work/gripper_feedback.json
```

The web process must be able to read the path supplied through `--gripper-feedback`.
When you click `Connect & take control`, the web service requests that the collector service start.
The collector starts in a read-only state. The web service does not initialize the gripper automatically.
If the request fails, the gripper status shows the reason.
The robot can still operate independently when gripper feedback is unavailable.

Do not initialize the gripper while it holds an object.
Initialization opens the fingers to find the travel endpoint.
Use this sequence:

1. If the gripper is not initialized, click `Initialize gripper`.
2. Change the `Gripper` opening. 0 is closed and 1000 is fully open.
3. Click `Execute targets`.

The service executes the gripper target only after confirming that the robot reached its target.
`Stop arm` and disabling the robot do not retract gripper commands already sent.

## Keyboard jogging

Keyboard jogging is available only in real-robot control mode. See [Jogging rules](#jogging-rules) for the motion rules.
During keyboard jogging, a person on site must be able to operate the physical emergency stop.
W/S, A/D, R/F, and Q/E use continuous jogging: motion continues while the key is held, with no trajectory endpoint.

Use this sequence:

1. Click `Connect & take control` and wait for measured coordinates.
2. Click `Enable robot` and wait until the robot is enabled.
3. Enter an integer from 1–100 in `Speed %`. The default is 1.
4. Click `Keyboard: OFF`.
5. Wait for the button to show `Keyboard: ON N%`, where N is the selected speed.
6. Hold a key to jog. Release the key to request a stop.

During the check, the button shows `Keyboard: CHECKING…`.
The service sets the selected `SpeedFactor` and checks the speed feedback.
The check sends no motion commands.

`SpeedFactor` is a global ratio that also affects `Home` and `Execute targets`.
The service does not restore the previous ratio when keyboard control turns off.
The `Speed %` tooltip shows the controller's current ratio.
Before changing speed, turn off keyboard control.
After changing `Speed %`, click `Keyboard: OFF` to apply the new speed.

| Key | Action |
| --- | --- |
| W / S | Extend / retract along the robot's heading |
| A / D | Rotate J1 left / right |
| R / F | Raise / lower the flange |
| Q / E | Rotate J4 in the positive / negative direction |
| Z / X | Increase / decrease the gripper opening, by 25 / 1000 per step |
| Esc | Disable the robot, only while keyboard control is on or being checked |

Esc has no effect after keyboard control turns off.
Click `Disable robot` to disable it in this state.

W/S rewrites the controller's User 8 as necessary. Turning off keyboard control does not restore it.
Do not use User 8 for other calibration or programs.

Z/X runs only while the robot is idle. Each hold lasts at most 5 seconds.
If the gripper is uninitialized or its feedback is unavailable, the service reports only a gripper error.
The robot keys remain available.

While a key is held, the page resends the current key through the WebSocket connection every 100 ms.
If the service receives no update within 0.4 seconds, it stops the current jog and turns off keyboard control.
Network latency variation near 0.4 seconds can interrupt jogging for this reason.

When the service detects that the page connection closed, it immediately turns off keyboard control.
During a network outage, it may not detect the closed connection. The 0.4-second key timeout stops jogging.

Keyboard control turns off when the page loses focus, becomes hidden, opens a context menu, starts a drag, or starts editing an input.
It also turns off when Shift, Ctrl, Alt, or Meta is pressed.
The service also turns it off when you click `Home`, `Execute targets`, or `Initialize gripper`.
The service turns it off if the robot leaves its enabled idle state or feedback fails or reports an alarm.
It also turns it off if joints exceed their ranges or the gripper locks.
Click `Keyboard: OFF` again before resuming jogging.

During robot jogging, Esc, `Stop arm`, `Disable robot`, and `Disconnect` first wait for the jog stop check to finish.
This wait can take about 12 seconds.
If the check fails or times out, the service still sends the stop or disable command.
If the robot does not stop immediately, use the physical emergency stop.

If communication or state becomes abnormal during robot jogging, the service stops and locks control.
Once the stop is confirmed, control unlocks after disabling is confirmed.

If the result of a Z/X command is unknown, the service locks the gripper and turns off keyboard control.
This does not stop the robot.

Disabling does not unlock control if the gripper is locked or the stop is unconfirmed.
Check the robot and gripper states, then restart the web service.
For an unconfirmed stop caused by a controller alarm, follow [Clear alarms](#clear-alarms).
A gripper command timeout does not retract a target already sent or guarantee that the gripper stops moving.
Restarting the collector process does not clear the command lock.
Check the gripper state first. Repair the fault before handling the lock. See [USB gripper protocol](development.en.md#usb-gripper-protocol) for its format.

### Jogging rules

Pressing W/S, A/D, R/F, or Q/E sends one `MoveJog`. Holding a key does not add trajectories.
R/F uses `CoordType=0,User=0,Tool=0`.
W/S uses the X axis of `CoordType=0,User=8,Tool=0`.
After W/S is pressed, the program first checks that User 0 is unchanged.
As necessary, it sets User 8 to the base origin, rotated by the current J1 angle, then reads it back for verification.
It does not change the global User selection or other coordinate frames.
If configuration verification fails, or J1 changes by more than 0.1° from the starting point, the program stops and locks control.
A/D changes the flange heading as J1 changes. It is not linear motion along base Y.
Q/E changes the flange heading while J1 stays fixed.

Releasing a key sends `MoveJog()`, then checks that the robot is stationary.
Feedback after a successful response must span at least 50 ms and contain at least 3 frames.
The interval between adjacent packet arrivals must not exceed about 16.7 ms.
Each joint must change by no more than 0.025° from the start of the window.
50 ms is the feedback confirmation window, not the total delay from key release to a stop.

Continuous jogging checks feedback, alarms, and the speed ratio throughout motion.
At the 1.05° margin boundary of a joint target limit, the program requests a stop.
The stopping distance is 1.05° plus 0.08° per 1% speed. For example, it is 4.25° at 40%.
The program also requests a stop if a joint angle or J3-J2 is within the stopping distance of a limit and still approaching it.
Higher speed causes a longer travel distance after the stop command is sent.
The stopping distance does not limit continuous jogging away from or along a limit.
If the starting point is in the 1.05° margin band, the program first uses a finite trajectory to return to the permitted range.
This software check cannot replace controller limits or the physical emergency stop.

For finite trajectories, each W/S or R/F segment is at most 50 mm. Each A/D or Q/E segment is at most 10°.
Each segment checks measured joints, the target, and path limits.
If no feasible target is found, the travel distance is halved repeatedly before sending. The shortest attempt is 0.1 mm.
If all targets are rejected, the current hold ends. Release the key before retrying or changing direction.
Target rejection does not lock control.
After leaving the margin band, continuous jogging in the same direction resumes automatically if the key remains held.
Robot key holds have no total time limit or total travel limit.

Holding multiple direction keys requests a stop.
The program continues motion only when one direction key remains held and that key press has not yet executed.
A key press that already executed must be released and pressed again.

## Stop and recover

When you finish normally, click `Disable robot`, then `Disconnect`.
`Stop arm` requests a controlled stop. It is not a physical emergency stop.
If a stop or command result cannot be confirmed, use the physical emergency stop on site.

Only one page holds control at a time.
The page sends a heartbeat through the WebSocket connection every 0.5 seconds.
If the page closes or sends no heartbeat within 3 seconds, the service requests a robot stop and locks control.
The page reconnects automatically after the service or proxy closes the connection.
If the page reconnects within 3 seconds, it retains control, with keyboard control off.
If the network outage exceeds 3 seconds, the service stops the robot and locks control.

When the connection drops, the page disables controls that depend on it.
During the outage, the page reads the state over HTTP once per second.
If no state is received, the page keeps the last received state.
`Stop arm` and `Disable robot` are sent simultaneously through the WebSocket connection and a separate HTTP request.
These 2 buttons and Esc remain available during a connection outage.

When you press Ctrl+C in the service terminal or stop the systemd service, the service first requests a stop and waits for confirmation.
It then exits.
Repeated signals do not interrupt stop confirmation during this wait.

If the process is forcibly terminated, crashes, loses power, or encounters a network fault, the stop command may not arrive.
The robot may continue to the endpoint of a finite trajectory.
Continuous jogging has no endpoint.
The robot may continue moving until a controller limit or collision.

After a confirmed stop, you can disable and re-enable the robot.
Reconnecting the page does not unlock an unknown command result.
If stopping cannot be confirmed during disconnection, the panel stops normal coordinate queries.
It retains feedback, the connection lock, and the safety warning. Another panel cannot take control.
Do not delete the lock file to release ownership.
After deletion, 2 processes can each hold their own lock.

### Clear alarms

During a controller alarm, `Mode` shows 9 and the controller rejects disable and stop commands.
An alarm stops robot motion.
The panel locks because the stop is unconfirmed. It lists alarm numbers, descriptions, and corrective actions.
The descriptions come from Dobot's alarm tables.

Use this recovery sequence:

1. Remove the cause according to the alarm's corrective action.
2. Click `Clear alarm`.
3. Wait for the panel to report that the alarm is cleared and confirm that the robot is disabled and stationary.

`Clear alarm` sends only `ClearError()`. It does not send `Continue()`.
Motion paused before the alarm does not resume.
After the alarm clears, the service disables the robot through a new control instance and confirms that it is stationary.
Control unlocks only after both are confirmed.

If the cause remains, the alarm immediately returns and the panel stays locked.
Joint limit alarms, such as 69 and 72, can clear only after the robot returns within its limits.
Use DobotStudio Pro or the teach pendant to jog it back.
After jogging back, check the controller mode as described in [Switch to TCP/IP mode from the server](hardware.en.md#switch-to-tcpip-mode-from-the-server).

When collision detection triggers, the alarm list shows `-2` (Collision detected).
If the alarm clears outside the panel, for example with the robot's Collision Resume button, the button shows `Recover`.
Check the robot state, then click `Recover`.
The service disables the robot through a new control instance and confirms that it is stationary, then unlocks control.
`Recover` works only for an unconfirmed stop that occurred during an alarm.

If the command order is unknown, control stays locked after the alarm clears.
An unconfirmed stop that occurred without an alarm also stays locked.
Check the robot state, then restart the web service.
Before restarting, read the reason for the unconfirmed result in the [Service logs](#service-logs).
Control also stays locked if you click `Stop arm` during recovery and the stop is unconfirmed.
While a stop is unconfirmed, the status bar shows the recovery procedure. A later successful stop does not replace this message.

## Read-only feedback

Use this command for read-only mode:

```sh
python3 apps/control_panel/server.py --mode read-only --robot-host 192.168.1.6
```

Only `Start sync` connects to TCP `30004`. `Stop sync` stops reading.
This mode shows only measured joints, controller mode, and alarm flags.
It provides no coordinate or gripper feedback.

## Service configuration

The service is `mg400-web-panel.service`, installed by [Host deployment](deploy.en.md). Its `mg400ctl` name is `web`.
Start or check the service on the host:

```sh
sudo mg400ctl start web
sudo mg400ctl status
```

During service shutdown, the panel can take about 60 seconds to confirm that the robot stopped.
The service's `TimeoutStopSec` is 90 seconds, which is longer than this interval.

The service reads its listen address and port from `MG400_PANEL_BIND` and `MG400_PANEL_PORT`, defaulting to `127.0.0.1:8766`.
`--bind` and `--port` override these values.
To listen on a non-loopback address, set `MG400_PANEL_TOKEN` to at least 24 characters.
The access token must contain only printable ASCII characters and must not start or end with a space.
The page uses the same token to access the API.
Without an access token, open `127.0.0.1`, `localhost`, or `[::1]` in the browser.
The service rejects other host names.

If a reverse proxy retains a path prefix, add `--base-path /mg400-panel/`.
Both the page and API use this prefix.
The reverse proxy must meet these requirements:

- Forward `api/ws` with HTTP/1.1, including the `Upgrade` and `Connection` request headers.
- Preserve the browser's `Host` value, including its port, or pass it in `X-Forwarded-Host`. In nginx, use `$http_host`, not `$host`.
- Set the read timeout above 5 seconds. The service pushes state at least once per second and sends a ping every 5 seconds.
- Set `MG400_PANEL_TOKEN` even when the service listens on a loopback address.

See [Robot service interface](development.en.md#robot-service-interface) for the interface reference.

After updating code, run the installer as described in [Host deployment](deploy.en.md#installation-and-updates), then run `mg400ctl restart web`.
After restarting, reload any panel pages that are already open.
If the page and service message format versions differ, the page asks you to reload and stops reconnecting.
An old page with the same version shows no prompt.

### Service logs

The service writes these events to standard error:

- Each change in the status bar message
- Control locking and unlocking
- Unconfirmed stop or disable results, and clearance of those states
- Controller alarm numbers and alarm clearance
- Changes to mode and alarm flags in robot feedback
- The reason keyboard control turns off
- Gripper locking and unlocking

The service also records the result of each page command.
It does not log key input, heartbeats, or state pushes.
A separate thread writes the logs. Blocking standard error does not delay a stop.

Read the logs on the Olares host:

```sh
sudo mg400ctl logs web -n 200
```

When the panel is locked, find the first `Stop or disable unconfirmed.` in the logs.
The following `Message:` line gives the reason the stop or disable failed.
