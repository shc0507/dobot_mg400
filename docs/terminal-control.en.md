[中文](terminal-control.md)

# Interactive terminal

[Return to the project home page](../README.en.md)

The interactive terminal directly controls the MG400 and the vacuum suction cup.
The interface continuously shows feedback. Enter commands at the bottom.
It does not depend on an HTTP service.
The terminal cannot detect key release and does not support keyboard jogging.

## Start the terminal

Run the terminal on a host that can connect to the robot.
The program uses `curses` from the Python 3.12+ standard library.
It supports local terminals on macOS and Linux, and SSH sessions with a PTY.
A PTY is an interactive terminal allocated by SSH. Request it with `ssh -t`.

Before use, close other robot control programs.
The terminal shares the robot connection lock with the robot service.
All processes must use the same robot address.
They must also share the lock directory `/var/lib/mg400/locks`. `mg400ctl terminal` sets it.
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
| `disconnect` | Request a controlled stop and release control. Keep feedback until explicit disable confirmation |
| `enable` | Enable the robot |
| `disable` | Disable the robot |
| `move X Y Z R` | Move to absolute coordinates |
| `home` | Move J1–J4 to 0° |
| `vacuum suck`, `vacuum release`, `vacuum off` | Suck, blow briefly to release the part, or turn the suction cup off. The robot must be idle |
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

## Control the suction cup

`status` and the interface show 2 feedback lines:

- `Flange (mm/deg)`: the flange pose X, Y, Z and R.
- `Vacuum`: the measured levels of `suck`, `blow` and `held`. Without feedback, the line shows `--`.

`stop` and disable end the blow pulse on DO1. They keep suction on DO2.
See [Vacuum suction cup](development.en.md#vacuum-suction-cup) for the outputs.

## Stop and exit

Enter `stop` or press Ctrl+C to request a controlled robot stop.
The terminal stays open after Ctrl+C.
After stop confirmation, enter `disable`, then `enable` to resume motion.
A controlled stop cannot replace the physical emergency stop at the site.
If the stop or command outcome cannot be confirmed, use the physical emergency stop at the site.

Enter `quit` or press Ctrl+D to request a controlled stop and disconnection.
The terminal exits only after it confirms disconnection and no tasks remain in progress.
If the stop is confirmed while the robot remains enabled, the terminal keeps feedback connected and retains the connection lock.
It stays open and asks you to enter `disable`, then `quit`.
The terminal does not send disable automatically.

After explicit disable, the service requires fresh disabled feedback, confirmed rest, and completion of pending tasks before it disconnects.
While these tasks finish, the terminal continues to wait.
If you press Ctrl+C during this wait, the new stop can require another explicit disable.
When the terminal asks for it, enter `disable`, then `quit`.
For a normal exit, enter `disable`, then `quit`.
If feedback is lost or the stop cannot be confirmed, the terminal stays open and keeps the connection lock.
Use the physical emergency stop when the terminal reports an unconfirmed stop.

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

The terminal runs as UID 1000. It reads `/etc/mg400/mg400.env`.
It shows `DISCONNECTED` after startup. It connects to the robot only when you enter `connect`.
