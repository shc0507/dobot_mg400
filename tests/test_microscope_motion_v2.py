"""Run the v2 sketch offline with timer interrupts and fake GPIO."""

import shutil
import subprocess
from pathlib import Path

import pytest

from microscope.sim import MicroscopeSimulator

SKETCH = Path(__file__).resolve().parents[1] / "firmware/microscope-motion-v2"


@pytest.fixture(scope="module")
def firmware_v2(tmp_path_factory):
    compiler = shutil.which("g++")
    assert compiler, "The offline firmware checks require g++"
    directory = tmp_path_factory.mktemp("microscope-motion-v2")
    binaries = {}
    for name, flags in {
        "default": [],
        "home": ["-DFOCUS_HOME_PIN=5", "-DFOCUS_HOME_DIRECTION=1", "-DFOCUS_HOME_MAX_STEPS=10"],
    }.items():
        target = directory / name
        subprocess.run(
            [
                compiler,
                "-std=c++11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(SKETCH / "tests"),
                *flags,
                str(SKETCH / "tests/native.cpp"),
                "-o",
                str(target),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        binaries[name] = target
    return binaries


def run(firmware_v2, commands, variant="default"):
    return subprocess.run(
        [str(firmware_v2[variant])],
        input="\n".join(commands) + "\n",
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    ).stdout.splitlines()[5:]


def test_v2_starts_asleep_and_wakes_without_pulses(firmware_v2):
    lines = run(firmware_v2, ["VERSION", "@pins", "OBJ ENABLE", "@time", "@pins", "@edges"])
    assert lines == [
        "VERSION microscope-axes 2.0.0",
        "PINS OBJ_EN=1 OBJ_SLEEP=0 FOCUS_EN=1 FOCUS_SLEEP=0",
        "OK OBJ ENABLED",
        "TIME 1000",
        "PINS OBJ_EN=0 OBJ_SLEEP=1 FOCUS_EN=1 FOCUS_SLEEP=0",
        "EDGES OBJ=0 FOCUS=0",
    ]


def test_v2_interrupts_finish_both_motors_while_foreground_is_busy(firmware_v2):
    lines = run(
        firmware_v2,
        [
            "OBJ ENABLE",
            "FOCUS ENABLE",
            "OBJ MOVE 100",
            "FOCUS MOVE -50",
            "@blocked 1000000",
            "@edges",
            "STATUS",
        ],
    )
    assert "DONE OBJ POS 100" in lines and "DONE FOCUS POS -50" in lines
    assert "EDGES OBJ=100 FOCUS=50" in lines
    assert (
        "OBJ: enabled=1 moving=0 speed=400 remaining=0 pos=100 sleeping=0 acceleration=2000"
        in lines
    )


def test_v2_ramps_up_and_down_without_losing_pulses(firmware_v2):
    lines = run(
        firmware_v2,
        [
            "OBJ ENABLE",
            "OBJ SPEED 1000",
            "OBJ MOVE 1000",
            "@advance 2500000",
            "@intervals OBJ",
            "@edges",
        ],
    )
    intervals = list(
        map(int, next(line for line in lines if line.startswith("INTERVALS")).split()[1:])
    )
    assert len(intervals) == 999
    assert intervals[0] > intervals[150] > min(intervals)
    assert intervals[-1] > intervals[-100] > min(intervals)
    assert min(intervals) >= 1000
    assert "EDGES OBJ=1000 FOCUS=0" in lines


def test_v2_status_snapshot_stays_consistent_during_uart_delay(firmware_v2):
    lines = run(
        firmware_v2,
        [
            "OBJ ENABLE",
            "FOCUS ENABLE",
            "OBJ MOVE 100",
            "FOCUS MOVE -100",
            "@uart 1",
            "STATUS",
            "@uart 0",
            "@advance 1000000",
            "@edges",
        ],
    )
    assert (
        "OBJ: enabled=1 moving=1 speed=400 remaining=100 pos=0 sleeping=0 acceleration=2000"
        in lines
    )
    assert (
        "FOCUS: enabled=1 moving=1 speed=800 remaining=100 pos=0 sleeping=0 acceleration=2000"
        in lines
    )
    assert "EDGES OBJ=100 FOCUS=100" in lines


def test_v2_maximum_rate_keeps_low_time_between_pulses(firmware_v2):
    lines = run(
        firmware_v2,
        [
            "OBJ ENABLE",
            "OBJ SPEED 10000",
            "OBJ MOVE 60000",
            "@advance 12000000",
            "@intervals OBJ",
            "@edges",
        ],
    )
    intervals = list(
        map(int, next(line for line in lines if line.startswith("INTERVALS")).split()[1:])
    )
    assert len(intervals) == 59999 and min(intervals) == 100
    assert "EDGES OBJ=60000 FOCUS=0" in lines


def test_v2_stop_cancels_pending_pulses_and_never_restarts(firmware_v2):
    lines = run(
        firmware_v2,
        [
            "OBJ ENABLE",
            "FOCUS ENABLE",
            "OBJ MOVE 1000",
            "FOCUS MOVE 1000",
            "@advance 100000",
            "STOP ALL",
            "@edges",
            "@advance 2000000",
            "@edges",
        ],
    )
    edges = [line for line in lines if line.startswith("EDGES")]
    assert edges[0] == edges[1] and edges[0] != "EDGES OBJ=0 FOCUS=0"
    assert not any(line.startswith("DONE") for line in lines)


def test_v2_idle_polling_does_not_prevent_release_or_sleep(firmware_v2):
    commands = ["FOCUS ENABLE", "FOCUS ZERO", "FOCUS LIMIT -10 10", "OBJ ENABLE"]
    for _ in range(12):
        commands += ["@advance 10000000", "STATUS"]
    commands += ["@pins", "FOCUS ENABLE", "@pins", "STATUS", "@edges"]
    lines = run(firmware_v2, commands)
    assert (
        "FOCUS: enabled=0 moving=0 speed=800 remaining=0 pos=0 sleeping=0 acceleration=2000"
        in lines
    )
    assert (
        "FOCUS: enabled=0 moving=0 speed=800 remaining=0 pos=0 sleeping=1 acceleration=2000"
        in lines
    )
    assert "PINS OBJ_EN=1 OBJ_SLEEP=0 FOCUS_EN=1 FOCUS_SLEEP=0" in lines
    assert "PINS OBJ_EN=1 OBJ_SLEEP=0 FOCUS_EN=0 FOCUS_SLEEP=1" in lines
    assert lines[-2:] == [
        "FOCUS_REF: zeroed=0 limited=0 min=0 max=0 homing=0",
        "EDGES OBJ=0 FOCUS=0",
    ]


def test_v2_long_move_cannot_sleep(firmware_v2):
    lines = run(
        firmware_v2,
        [
            "FOCUS ENABLE",
            "FOCUS SPEED 1",
            "FOCUS MOVE 150",
            "@advance 121000000",
            "STATUS",
            "@edges",
        ],
    )
    assert (
        "FOCUS: enabled=1 moving=1 speed=1 remaining=29 pos=121 sleeping=0 acceleration=2000"
        in lines
    )
    assert "EDGES OBJ=0 FOCUS=121" in lines


def test_v2_reference_limits_busy_and_overflow_remain_enforced(firmware_v2):
    lines = run(
        firmware_v2,
        [
            "FOCUS ENABLE",
            "FOCUS ZERO",
            "FOCUS LIMIT -10 10",
            "FOCUS MOVE 11",
            "FOCUS MOVE 10",
            "MICRO 4",
            "FOCUS ZERO",
            "@advance 200000",
            "FOCUS DISABLE",
            "FOCUS ENABLE",
            "@position 2147483647",
            "FOCUS MOVE 1",
            "@edges",
        ],
    )
    assert "ERR FOCUS LIMIT" in lines
    assert "ERR MICRO WHILE_MOVING" in lines and "ERR FOCUS BUSY" in lines
    assert "ERR MOVE POSITION_RANGE" in lines
    assert "EDGES OBJ=0 FOCUS=10" in lines


def test_v2_home_switch_pauses_timer_during_debounce(firmware_v2):
    lines = run(
        firmware_v2,
        [
            "FOCUS ENABLE",
            "FOCUS HOME",
            "@advance 15000",
            "@switch 1",
            "@blocked 15000",
            "@edges",
            "@advance 25000",
            "@edges",
            "STATUS",
        ],
        "home",
    )
    edges = [line for line in lines if line.startswith("EDGES")]
    assert edges == ["EDGES OBJ=0 FOCUS=1"] * 2
    assert "DONE FOCUS HOME" in lines
    assert "FOCUS_REF: zeroed=1 limited=0 min=0 max=0 homing=0" in lines


def test_v2_simulator_matches_native_move_power_and_reference(firmware_v2):
    model = MicroscopeSimulator(firmware_version=2)
    commands = [
        "VERSION",
        "FOCUS ENABLE",
        "FOCUS ZERO",
        "FOCUS LIMIT -100 100",
        "OBJ ENABLE",
        "FOCUS MOVE -15",
        "OBJ MOVE 20",
        "@advance 500000",
        "STATUS",
        "FOCUS DISABLE",
        "@advance 60000000",
        "STATUS",
        "@advance 60000000",
        "@advance 1000",
        "STATUS",
    ]
    expected = []
    for command in commands:
        if command.startswith("@advance "):
            expected.extend(model.advance(int(command.split()[1]) / 1_000_000))
        else:
            expected.extend(model.command(command))
    assert run(firmware_v2, commands) == expected
