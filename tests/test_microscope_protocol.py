"""Exercise the actual sketch through fake GPIO, then compare its wire grammar."""

import shutil
import subprocess
from pathlib import Path

import pytest

from microscope.sim import MicroscopeSimulator

ROOT = Path(__file__).resolve().parents[1]
SKETCH = ROOT / "firmware/microscope-axes"


@pytest.fixture(scope="module")
def firmware(tmp_path_factory):
    compiler = shutil.which("g++")
    assert compiler, "The offline firmware checks require g++"
    directory = tmp_path_factory.mktemp("microscope-firmware")
    variants = {
        "default": [],
        "home": ["-DFOCUS_HOME_PIN=7", "-DFOCUS_HOME_DIRECTION=1", "-DFOCUS_HOME_MAX_STEPS=10"],
        "negative": [
            "-DFOCUS_HOME_PIN=7",
            "-DFOCUS_HOME_DIRECTION=-1",
            "-DFOCUS_HOME_MAX_STEPS=10",
        ],
        "timeout": [
            "-DFOCUS_HOME_PIN=7",
            "-DFOCUS_HOME_DIRECTION=1",
            "-DFOCUS_HOME_MAX_STEPS=100",
            "-DFOCUS_HOME_TIMEOUT_MS=50",
        ],
        "unknown": ["-DFOCUS_HOME_PIN=7"],
    }
    binaries = {}
    for name, flags in variants.items():
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


def run_both(firmware, commands, variant="default"):
    options = {
        "default": {},
        "home": dict(home_switch=True, home_direction=1, home_max_steps=10),
        "negative": dict(home_switch=True, home_direction=-1, home_max_steps=10),
        "timeout": dict(home_switch=True, home_direction=1, home_max_steps=100, home_timeout_ms=50),
        "unknown": dict(home_switch=True),
    }
    model = MicroscopeSimulator(**options[variant])
    expected = model.reset()
    for command in commands:
        if command.startswith("@advance "):
            expected.extend(model.advance(int(command.split()[1]) / 1_000_000))
        elif command.startswith("@switch "):
            model.set_home_switch(command.endswith("1"))
        elif command.startswith("@position "):
            model.focus.position = int(command.split()[1])
        elif command.startswith("@hex "):
            expected.extend(model.feed(bytes.fromhex(command[5:])))
        else:
            expected.extend(model.command(command))
    actual = subprocess.run(
        [str(firmware[variant])],
        input="\n".join(commands) + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.splitlines()
    assert actual == expected
    return model, actual[5:]


def test_powerup_and_version(firmware):
    model, lines = run_both(firmware, ["VERSION", "STATUS", "HELP"])
    assert lines[0] == "VERSION microscope-axes 1.0.0"
    assert lines[1:5] == [
        "MICRO=1",
        "OBJ: enabled=0 moving=0 speed=400 remaining=0 pos=0",
        "FOCUS: enabled=0 moving=0 speed=800 remaining=0 pos=0",
        "FOCUS_REF: zeroed=0 limited=0 min=0 max=0 homing=0",
    ]
    assert not model.focus.enabled and not model.objective.enabled


@pytest.mark.parametrize("name", ["OBJ", "FOCUS"])
def test_legacy_motion_completion_and_disable(firmware, name):
    model, lines = run_both(
        firmware,
        [
            f"{name} MOVE 2",
            f"{name} ENABLE",
            f"{name} SPEED 1000",
            f"{name} MOVE -3",
            "@advance 5000",
            f"{name} MOVE 0",
            f"{name} DISABLE",
        ],
    )
    assert lines == [
        f"ERR {name} DISABLED",
        f"OK {name} ENABLED",
        f"OK {name} SPEED 1000",
        f"OK {name} MOVE -3",
        f"DONE {name} POS -3",
        f"OK {name} MOVE 0",
        f"OK {name} DISABLED",
    ]
    assert not model.focus.moving and not model.objective.moving


@pytest.mark.parametrize("divisor", [1, 2, 4, 8, 16])
def test_global_microstep_values(firmware, divisor):
    _, lines = run_both(firmware, [f"micro {divisor}"])
    assert lines == [f"OK MICRO {divisor}"]


@pytest.mark.parametrize("target", ["OBJ", "FOCUS", "ALL"])
def test_stop_cancels_without_completion_or_restart(firmware, target):
    model, lines = run_both(
        firmware,
        [
            "OBJ ENABLE",
            "FOCUS ENABLE",
            "OBJ MOVE 200",
            "FOCUS MOVE 200",
            "@advance 10000",
            f"STOP {target}",
            f"STOP {target}",
            "@advance 1000000",
        ],
    )
    assert lines.count(f"OK STOP {target}") == 2
    if target in ("FOCUS", "ALL"):
        assert 0 < model.focus.position < 200
        assert "DONE FOCUS POS 200" not in lines
    if target in ("OBJ", "ALL"):
        assert 0 < model.objective.position < 200
        assert "DONE OBJ POS 200" not in lines


def test_busy_rejects_conflicting_commands(firmware):
    _, lines = run_both(
        firmware,
        [
            "FOCUS ENABLE",
            "FOCUS MOVE 10",
            "FOCUS MOVE 2",
            "FOCUS ZERO",
            "FOCUS LIMIT -20 20",
            "FOCUS HOME",
            "MICRO 4",
            "STOP ALL",
        ],
    )
    assert lines[2:7] == ["ERR FOCUS BUSY"] * 4 + ["ERR MICRO WHILE_MOVING"]


def test_zero_and_limits_guard_entire_target_before_pulses(firmware):
    model, lines = run_both(
        firmware,
        [
            "FOCUS ZERO",
            "FOCUS ENABLE",
            "FOCUS LIMIT -3 4",
            "FOCUS ZERO",
            "FOCUS LIMIT -3 4",
            "FOCUS MOVE 5",
            "FOCUS MOVE 4",
            "@advance 10000",
            "FOCUS MOVE -8",
            "FOCUS MOVE -7",
            "@advance 10000",
            "FOCUS LIMIT 0 4",
        ],
    )
    assert lines == [
        "ERR FOCUS DISABLED",
        "OK FOCUS ENABLED",
        "ERR FOCUS NOT_ZEROED",
        "OK FOCUS ZERO",
        "OK FOCUS LIMIT -3 4",
        "ERR FOCUS LIMIT",
        "OK FOCUS MOVE 4",
        "DONE FOCUS POS 4",
        "ERR FOCUS LIMIT",
        "OK FOCUS MOVE -7",
        "DONE FOCUS POS -3",
        "ERR FOCUS LIMIT POSITION",
    ]
    assert model.focus.position == -3 and model.minimum == -3 and model.maximum == 4


@pytest.mark.parametrize("invalidate", ["FOCUS DISABLE", "MICRO 2", "FOCUS ZERO"])
def test_reference_invalidation_and_repeat_zero(firmware, invalidate):
    model, _ = run_both(
        firmware, ["FOCUS ENABLE", "FOCUS ZERO", "FOCUS LIMIT -5 5", invalidate, "STATUS"]
    )
    assert not model.limited
    assert model.zeroed is (invalidate == "FOCUS ZERO")


def test_same_microstep_preserves_reference(firmware):
    model, _ = run_both(
        firmware, ["FOCUS ENABLE", "FOCUS ZERO", "FOCUS LIMIT -5 5", "MICRO 1", "STATUS"]
    )
    assert model.zeroed and model.limited
    model.reset()
    assert not model.zeroed and not model.limited and not model.focus.enabled


@pytest.mark.parametrize(
    "command,error",
    [
        ("FOCUS MOVE", "ERR MOVE STEPS"),
        ("FOCUS MOVE 1.5", "ERR MOVE STEPS"),
        ("FOCUS MOVE 12tail", "ERR MOVE STEPS"),
        ("FOCUS MOVE 2147483648", "ERR MOVE STEPS"),
        ("FOCUS MOVE -2147483649", "ERR MOVE STEPS"),
        ("FOCUS MOVE 999999999999999999999", "ERR MOVE STEPS"),
        ("FOCUS MOVE 5 extra", "ERR EXTRA_ARGUMENTS"),
        ("FOCUS SPEED", "ERR SPEED VALUE"),
        ("FOCUS SPEED 0", "ERR SPEED RANGE 1-10000"),
        ("FOCUS SPEED -1", "ERR SPEED RANGE 1-10000"),
        ("FOCUS SPEED 10001", "ERR SPEED RANGE 1-10000"),
        ("FOCUS SPEED 1e3", "ERR SPEED RANGE 1-10000"),
        ("MICRO", "ERR MICRO VALUE"),
        ("MICRO 4evil", "ERR MICRO VALID=1,2,4,8,16"),
        ("MICRO 3", "ERR MICRO VALID=1,2,4,8,16"),
        ("STOP", "ERR STOP TARGET"),
        ("STOP OTHER", "ERR STOP TARGET"),
        ("FOCUS", "ERR MISSING_ACTION"),
        ("UNKNOWN", "ERR UNKNOWN_COMMAND"),
        ("FOCUS UNKNOWN", "ERR UNKNOWN_ACTION"),
        ("OBJ ZERO", "ERR UNKNOWN_ACTION"),
        ("FOCUS LIMIT 3 2", "ERR FOCUS LIMIT RANGE"),
        ("FOCUS LIMIT -4 bad", "ERR FOCUS LIMIT RANGE"),
        ("FOCUS LIMIT", "ERR FOCUS LIMIT RANGE"),
        ("FOCUS LIMIT 1", "ERR FOCUS LIMIT RANGE"),
        ("FOCUS LIMIT -2147483649 4", "ERR FOCUS LIMIT RANGE"),
        ("FOCUS ENABLE extra", "ERR EXTRA_ARGUMENTS"),
    ],
)
def test_malformed_commands_do_not_move(firmware, command, error):
    model, lines = run_both(firmware, ["FOCUS ENABLE", command, "@advance 10000"])
    assert lines[-1] == error
    assert model.focus.position == 0 and not model.focus.moving


@pytest.mark.parametrize("position,move", [(2147483647, 1), (-2147483648, -1)])
def test_position_addition_cannot_overflow(firmware, position, move):
    model, lines = run_both(
        firmware, ["FOCUS ENABLE", f"@position {position}", f"FOCUS MOVE {move}"]
    )
    assert lines[-1] == "ERR MOVE POSITION_RANGE"
    assert model.focus.position == position


def test_long_min_move_is_defined_and_stoppable(firmware):
    model, lines = run_both(
        firmware, ["FOCUS ENABLE", "FOCUS MOVE -2147483648", "@advance 2000", "STOP ALL"]
    )
    assert lines == ["OK FOCUS ENABLED", "OK FOCUS MOVE -2147483648", "OK STOP ALL"]
    assert model.focus.position == -1


@pytest.mark.parametrize(
    "payload,error",
    [
        (b"x" * 96 + b"FOCUS MOVE 20\n", "ERR LINE_TOO_LONG"),
        (b"FOCUS MOVE 20\x00\n", "ERR LINE_CHARACTER"),
        (b"\xffFOCUS MOVE 20\n", "ERR LINE_CHARACTER"),
        (b"FOCUS\x01 MOVE 20\n", "ERR LINE_CHARACTER"),
    ],
)
def test_invalid_serial_line_discards_suffix_then_recovers(firmware, payload, error):
    model, lines = run_both(
        firmware, ["FOCUS ENABLE", "@hex " + payload.hex(), "FOCUS MOVE 1", "@advance 2000"]
    )
    assert lines == ["OK FOCUS ENABLED", error, "OK FOCUS MOVE 1", "DONE FOCUS POS 1"]
    assert model.focus.position == 1


def test_fragmented_case_insensitive_serial_and_crlf(firmware):
    _, lines = run_both(firmware, ["@hex " + b"foCuS\tEn".hex(), "@hex " + b"Able\r\n".hex()])
    assert lines == ["OK FOCUS ENABLED"]


@pytest.mark.parametrize("variant,error", [("default", "NO_SWITCH"), ("unknown", "NOT_CONFIGURED")])
def test_home_refuses_missing_hardware_facts(firmware, variant, error):
    model, lines = run_both(firmware, ["FOCUS ENABLE", "FOCUS HOME"], variant)
    assert lines == ["OK FOCUS ENABLED", f"ERR FOCUS HOME {error}"]
    assert not model.focus.moving and model.focus.position == 0


@pytest.mark.parametrize("variant", ["home", "negative"])
def test_home_requires_new_debounced_edge_and_restores_speed(firmware, variant):
    model, lines = run_both(
        firmware,
        [
            "FOCUS ENABLE",
            "@switch 1",
            "FOCUS HOME",
            "@switch 0",
            "FOCUS HOME",
            "@advance 11000",
            "@switch 1",
            "@advance 19000",
            "@switch 0",
            "@advance 1000",
            "@switch 1",
            "@advance 21000",
            "STATUS",
        ],
        variant,
    )
    assert lines[1] == "ERR FOCUS HOME SWITCH_ACTIVE"
    assert lines.count("DONE FOCUS HOME") == 1
    assert model.zeroed and not model.limited and not model.homing
    assert model.focus.position == 0 and model.focus.speed == 800


@pytest.mark.parametrize("variant,error", [("home", "TRAVEL"), ("timeout", "TIMEOUT")])
def test_home_bounds_travel_and_time(firmware, variant, error):
    model, lines = run_both(
        firmware, ["FOCUS ENABLE", "FOCUS HOME", "@advance 200000", "STATUS"], variant
    )
    assert f"ERR FOCUS HOME {error}" in lines
    assert not model.zeroed and not model.limited and not model.focus.moving
    assert model.focus.speed == 800


@pytest.mark.parametrize("cancel", ["STOP FOCUS", "STOP ALL", "FOCUS DISABLE"])
def test_home_cancellation_does_not_declare_zero_or_complete_later(firmware, cancel):
    model, lines = run_both(
        firmware,
        ["FOCUS ENABLE", "FOCUS HOME", "@advance 10000", cancel, "@switch 1", "@advance 50000"],
        "home",
    )
    assert not any(line.startswith("DONE") for line in lines)
    assert not model.homing and not model.zeroed and not model.focus.moving
    assert model.focus.speed == 800


def test_home_cannot_be_sped_up_or_reconfigured(firmware):
    _, lines = run_both(
        firmware,
        ["FOCUS ENABLE", "FOCUS HOME", "FOCUS SPEED 9000", "MICRO 4", "FOCUS ZERO", "STOP ALL"],
        "home",
    )
    assert lines[2:5] == ["ERR FOCUS BUSY", "ERR MICRO WHILE_MOVING", "ERR FOCUS BUSY"]


@pytest.mark.parametrize("pin", [0, 1, 2, 3, 4, 8, 9, 10, 11, 12, 13, 20])
def test_home_pin_conflicts_fail_compilation(tmp_path, pin):
    probe = tmp_path / "probe.cpp"
    probe.write_text('#include "Arduino.h"\n#include "FocusConfig.h"\n')
    result = subprocess.run(
        [
            "g++",
            "-std=c++11",
            "-fsyntax-only",
            "-I",
            str(SKETCH / "tests"),
            "-I",
            str(SKETCH),
            f"-DFOCUS_HOME_PIN={pin}",
            str(probe),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and "static assertion failed" in result.stderr
