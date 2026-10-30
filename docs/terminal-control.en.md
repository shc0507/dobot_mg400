[中文](terminal-control.md)

# Interactive terminal

[Return to the project home page](../README.en.md)

The interactive terminal directly controls the MG400 and PGEA gripper.
The interface continuously shows feedback. Enter commands at the bottom.
It does not depend on an HTTP service.
The terminal cannot detect key release and does not support keyboard jogging.

## Start the terminal

Run the terminal on a host that can connect to the robot.
The program uses `curses` from the Python 3.12+ standard library.
It supports local terminals on macOS and Linux, and SSH sessions with a PTY.
A PTY is an interactive terminal allocated by SSH. Request it with `ssh -t`.

Before use, close other robot control programs.
The terminal shares the robot connection lock with the web panel.
All processes must use the same robot address.
They must also share `MG400_CONTROLLER_LOCK_DIR`.
For directory and permission requirements, see [Panel connection lock](deploy.en.md#panel-connection-lock).
This lock cannot prevent DobotStudio or other hosts from connecting.

Enter the project directory and run:

```sh
uv run mg400-robot
```

You can also run it directly:

```sh
python3 scripts/mg400_robot.py
```

The robot address defaults to `MG400_ROBOT_HOST`. If unset, it comes from `config/robot.toml`.
Override the address with `--robot-host`:

```sh
uv run mg400-robot --robot-host 192.168.1.6
```

Starting the program does not connect, enable, or move the robot.
It does not initialize the gripper either.
Enter `connect`, then wait until the interface shows measured state.
Check that the motion area contains no people or obstacles. Then enter `enable`.

## Enter commands

After entering a command, press Enter to execute it.
Use the up and down arrow keys to recall previous commands.
Use the left and right arrow keys to move the input cursor.
Enter `help` for command descriptions. Enter `status` for the current state.

| Command | Behavior |
| --- | --- |
| `connect` | Connect to the robot and acquire the connection lock |
| `disconnect` | Request a controlled stop, then disconnect after confirmation |
| `enable` | Enable the robot |
| `disable` | Disable the robot |
| `move X Y Z R` | Move to absolute coordinates |
| `home` | Move J1–J4 to 0° |
| `grip N` | Set the gripper opening in the range 0–1000 |
| `grip-init` | Initialize the gripper. This opens the fingers |
| `stop` | Request a controlled robot stop |
| `status` | Show the current state |
| `help` | Show command descriptions |
| `quit` | Request a controlled stop, then exit after confirming disconnection |
| `quit --force` | Force exit. This cannot confirm that the robot stopped |

`move` uses the base coordinate frame and flange origin (`User=0,Tool=0`).
X/Y/Z use mm. R uses degrees.
Before execution, the program validates the target with the controller's inverse kinematics and existing joint-range checks.
Linear motion uses `SpeedL=1,AccL=1`.
`home` uses `SpeedJ=5,AccJ=5`.
`home` moves the robot. It is not a coordinate calibration command.

## Control the gripper

The gripper uses the existing USB collection service.
[Host deployment](deploy.en.md) installs this service.
The collection program must run with `--commands`.
The terminal and collection program must run on the same host.
Both must share a monotonic clock (a clock that measures time intervals).
Gripper commands do not support transmission between hosts.

Use `--gripper-feedback` to specify the feedback file.
This example uses the deployed host's path:

```sh
python3 scripts/mg400_robot.py \
  --gripper-feedback /var/lib/mg400/work/gripper_feedback.json
```

The terminal process must be able to read this path.
When `connect` connects to the robot, the terminal requests that the collection service start.
The terminal does not automatically initialize the gripper.
Without a gripper feedback file, you can still control the robot alone.

Before sending gripper commands, you must connect to the robot with `connect`.
The robot must be idle, and the controller mode must be 4 or 5.
These 2 modes mean disabled idle and enabled idle, respectively.

Do not enter `grip-init` while the gripper holds an object.
Initialization opens the fingers to find the travel endpoints.
Follow this sequence:

1. Check that the gripper holds no object.
2. If the gripper is not initialized, enter `grip-init`.
3. Enter `grip N`. Replace N with the target opening.

0 means closed. 1000 means fully open.
`grip` moves only the gripper, not the robot.
`stop` and disabling the robot do not retract gripper commands already sent.

## Stop and exit

Enter `stop` or press Ctrl+C to request a controlled robot stop.
The terminal stays open after Ctrl+C.
After stop confirmation, enter `disable`, then `enable` to resume motion.
A controlled stop cannot replace the physical emergency stop at the site.
If the stop or command outcome cannot be confirmed, use the physical emergency stop at the site.

Enter `quit` or press Ctrl+D to request a controlled stop and disconnection.
The terminal exits only after it confirms disconnection and no tasks remain in progress.
If it cannot confirm the stop, the terminal stays open and keeps the connection lock.
Exiting does not automatically disable the robot.
For a normal exit, enter `disable`, then `quit`.

Use `quit --force` only after a physical stop at the site.
A forced exit returns exit code 1 and releases the process lock.
It cannot confirm that the robot stopped.

If the process exits or the network fails, the stop command may not arrive.
The robot may continue to the end of the trajectory already sent.
Do not remove lock files to release ownership.
For lock handling, see [Panel connection lock](deploy.en.md#panel-connection-lock).

## Start on a deployed host

On a host installed through [Host deployment](deploy.en.md), run as root:

```sh
mg400ctl terminal
```

The terminal runs as UID 1000. It reads `/etc/mg400/mg400.env` and uses the deployed host's gripper feedback file.
It shows `DISCONNECTED` after startup. It connects to the robot only when you enter `connect`.
