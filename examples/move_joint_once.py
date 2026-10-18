"""MG400 joint/Cartesian motion functions; Python standard library only.

Importing this module does not connect or move. Running it demonstrates J4 +5 degrees.
"""

import json
import math
import re
import socket
import time

ROBOT_IP = "192.168.1.6"
LOCAL_IP = "192.168.1.10"
DELTA_DEG = (0.0, 0.0, 0.0, 5.0)  # J1, J2, J3, J4: change from current angles

# 阅读提示：CHECK 框为校验/防护，可先跳过看运动主线；通信框包含必要的收发逻辑。


# ┌─ CHECK：通信封装（超时、断连、长度、应答匹配；只发送一次）──────────
def command(text, port=29999, timeout=2.0):
    """Send once, verify the complete reply; never retry a movement command."""
    with socket.create_connection(
        (ROBOT_IP, port), timeout=timeout, source_address=(LOCAL_IP, 0)
    ) as sock:
        deadline = time.monotonic() + timeout
        sock.sendall(text.encode("ascii"))
        packet = bytearray()
        while b";" not in packet:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(text)
            sock.settimeout(remaining)
            part = sock.recv(4096 - len(packet))
            if not part:
                raise ConnectionError("Controller disconnected")
            packet.extend(part)
            if len(packet) >= 4096:
                raise ValueError("Oversized controller reply")
    reply = packet.decode("ascii").strip()
    match = re.fullmatch(r"(-?\d+),\{(.*)\},(.*);", reply, re.DOTALL)
    if not match or re.sub(r"\s+", "", match[3]) != re.sub(r"\s+", "", text):
        raise RuntimeError(f"Unexpected reply: {reply}")
    if int(match[1]) != 0:
        raise RuntimeError(reply)
    return match[2].strip()


# └────────────────────────────────────────────────────────────────


def _read_position(query):
    # ┌─ CHECK：关节反馈允许 4/6 项，空间位姿仍为 4 项；均须有限 ─────
    values = tuple(float(x) for x in command(query).split(","))
    allowed_lengths = (4, 6) if query == "GetAngle()" else (4,)
    if len(values) not in allowed_lengths or not all(map(math.isfinite, values)):
        raise ValueError(f"Invalid position values from {query}: {values}")
    # └────────────────────────────────────────────────────────────
    return values[:4]  # MG400 的六项关节回复中，前四项对应 J1–J4。


def get_joints():
    """Read actual J1, J2, J3, J4 in degrees."""
    return _read_position("GetAngle()")


def get_pose():
    """Read actual X, Y, Z (mm), R (degrees) in User=0, Tool=0."""
    return _read_position("GetPose(0,0)")


def move_joints(j1, j2, j3, j4):
    """Move to absolute J1-J4 (degrees); return measured angles after arrival.

    Joint interpolation does not require the TCP to follow a straight line.
    """
    return _move((j1, j2, j3, j4), get_joints, "JointMovJ", "SpeedJ=10,AccJ=10")


def move_linear(x_mm, y_mm, z_mm, r_deg):
    """Move TCP along a line to absolute User=0/Tool=0 XYZ (mm), R (degrees).

    R is TCP yaw in this coordinate system, not the J4 joint angle.
    Return the measured pose after arrival.
    """
    return _move((x_mm, y_mm, z_mm, r_deg), get_pose, "MovL", "User=0,Tool=0,SpeedL=1,AccL=1")


def _move(target, read_position, motion, options):
    # ┌─ CHECK：执行前校验（目标数值、空闲状态、控制器报警）──────────
    if any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
        for x in target
    ):
        raise ValueError("Target must contain four finite numbers")
    mode = int(command("RobotMode()"))
    if mode not in (4, 5):  # 4: disabled; 5: enabled and idle
        raise RuntimeError(f"Robot must be idle; current mode: {mode}")
    errors = json.loads(command("GetErrorID()"))
    if not isinstance(errors, list) or not errors or any(item != [] for item in errors):
        raise RuntimeError(f"Resolve controller alarms first: {errors}")
    # └────────────────────────────────────────────────────────────
    start = read_position()
    print(f"{motion} start: {start}; target: {target}", flush=True)

    try:
        if mode == 4:
            command("EnableRobot()")  # Uses the controller's configured payload
        # ┌─ CHECK：限时等待使能并进入空闲状态 ──────────────────────
        deadline = time.monotonic() + 5
        while int(command("RobotMode()")) != 5:
            if time.monotonic() >= deadline:
                raise TimeoutError("Robot did not become enabled and idle")
            time.sleep(0.1)
        # └────────────────────────────────────────────────────────
        command("SpeedFactor(10)")  # Global speed ratio: 10%, not 10 degrees/s
        # ┌─ CHECK：准备期间起点未漂移（0.2° 或 0.2 mm / 0.2°）──────
        if any(abs(a - b) > 0.2 for a, b in zip(read_position(), start, strict=True)):
            raise RuntimeError("Starting position changed; inspect before trying again")
        # └────────────────────────────────────────────────────────
        command("CP(0)")  # No blending into a following movement
        values = ",".join(f"{q:.6f}" for q in target)
        command(f"{motion}({values},{options})", port=30003)
        # ┌─ CHECK：等待完成并回读确认到位（含模式和误差检查）────────
        command("Sync()", port=30003, timeout=30.0)  # Wait for the motion queue
        actual = read_position()  # A command ACK alone is not proof of arrival
        if int(command("RobotMode()")) != 5 or any(
            abs(a - b) > 0.1 for a, b in zip(actual, target, strict=True)
        ):
            raise RuntimeError(f"Arrival not confirmed: {actual}")
        # Tolerance: 0.1 degree for joints; 0.1 mm / 0.1 degree for TCP pose.
        # └────────────────────────────────────────────────────────
        print(f"{motion} arrived: {actual}; robot remains enabled.")
        return actual
    # ┌─ CHECK：异常兜底（含 Ctrl+C）；请求停止，不重试运动 ──────────
    except BaseException:
        try:
            command("ResetRobot()")  # Controlled stop request, not physical emergency stop
        except Exception as error:
            print(f"STOP NOT CONFIRMED — use the physical emergency stop: {error}")
        raise
    # └────────────────────────────────────────────────────────────


def main():
    # ┌─ CHECK：演示增量格式及每轴不超过 5° 的限制 ──────────────────
    if len(DELTA_DEG) != 4 or not all(math.isfinite(x) for x in DELTA_DEG):
        raise ValueError("DELTA_DEG must contain four finite numbers")
    if any(abs(x) > 5 for x in DELTA_DEG):
        raise ValueError("This small-motion example limits each change to 5 degrees")
    # └────────────────────────────────────────────────────────────
    start = get_joints()
    move_joints(*(a + d for a, d in zip(start, DELTA_DEG, strict=True)))


# ┌─ CHECK：入口保护，导入模块时不执行运动示例 ───────────────────────
if __name__ == "__main__":
    main()
# └────────────────────────────────────────────────────────────────
