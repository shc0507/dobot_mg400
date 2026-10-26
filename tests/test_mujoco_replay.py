import json
import os
import socket
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from mg400_agent.mujoco_replay import (
    Keyframe,
    launch_macos_viewer,
    load_trajectory,
    main,
    replay,
    trajectory_samples,
)


def write_trajectory(tmp_path, data):
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def trajectory_path(tmp_path):
    return write_trajectory(
        tmp_path,
        [
            {"time_s": 0, "joints_deg": [0, 0, 0, 0]},
            {"time_s": 1.2, "joints_deg": [0, 0, 0, 15]},
        ],
    )


@pytest.mark.parametrize("options", [[], ["--viewer"], ["--dynamics"], ["--dynamics", "--viewer"]])
def test_missing_trajectory_is_rejected_before_model_or_viewer(monkeypatch, capsys, options):
    def forbidden(*args, **kwargs):
        pytest.fail("A missing trajectory must fail before model or viewer startup")

    monkeypatch.setattr("mg400_agent.mujoco_replay.load_trajectory", forbidden)
    monkeypatch.setattr("mg400_agent.mujoco_replay.KinematicModel", forbidden)
    monkeypatch.setattr("mg400_agent.mujoco_replay.launch_macos_viewer", forbidden)
    monkeypatch.setitem(
        sys.modules,
        "mg400_agent.mujoco_dynamics",
        SimpleNamespace(
            DynamicModel=forbidden, run_dynamics=forbidden, validate_trajectory=forbidden
        ),
    )

    with pytest.raises(SystemExit) as caught:
        main(options)

    assert caught.value.code == 2
    assert "required: --trajectory" in capsys.readouterr().err


@pytest.mark.parametrize(
    "data",
    [
        {},
        [],
        [{"time_s": 0, "joints_deg": [0, 0, 0, 0]}],
        [{"time_s": 1, "joints_deg": [0, 0, 0, 0]}] * 2,
        [{"time_s": 0, "joints_deg": [0, 0, 0, 0]}] * 2,
        [{"time_s": 0, "joints_deg": [0, 0, 0]}] * 2,
        [{"time_s": 0, "joints_deg": [0, 0, 0, 0], "typo": 1}] * 2,
        [{"time_s": 0, "joints_deg": [True, 0, 0, 0]}] * 2,
        [{"time_s": 0, "joints_deg": [float("nan"), 0, 0, 0]}] * 2,
        [{"time_s": float("inf"), "joints_deg": [0, 0, 0, 0]}] * 2,
        [None, None],
    ],
)
def test_rejects_malformed_trajectory_before_creating_model(tmp_path, monkeypatch, capsys, data):
    def unexpected_model():
        pytest.fail("Invalid trajectory must be rejected before loading MuJoCo")

    monkeypatch.setattr("mg400_agent.mujoco_replay.KinematicModel", unexpected_model)
    assert main(["--trajectory", str(write_trajectory(tmp_path, data))]) == 1
    assert "failed" in capsys.readouterr().err


def test_interpolates_uneven_keyframes_and_keeps_final_sample(tmp_path):
    path = write_trajectory(
        tmp_path,
        [
            {"time_s": 0, "joints_deg": [0, 0, 0, 0]},
            {"time_s": 0.5, "joints_deg": [10, 20, 30, 40]},
            {"time_s": 1.1, "joints_deg": [16, 26, 36, 46]},
        ],
    )
    frames = load_trajectory(path)
    samples = list(trajectory_samples(frames, rate=4))
    assert [frame.time_s for frame in samples] == pytest.approx([0, 0.25, 0.5, 0.75, 1, 1.1])
    assert samples[1].joints_deg == pytest.approx((5, 10, 15, 20))
    assert samples[3].joints_deg == pytest.approx((12.5, 22.5, 32.5, 42.5))
    assert samples[-1] == Keyframe(1.1, (16, 26, 36, 46))


def test_decimal_duration_does_not_duplicate_final_frame():
    frames = [Keyframe(0, (0, 0, 0, 0)), Keyframe(0.07, (1, 2, 3, 4))]
    samples = list(trajectory_samples(frames, rate=100))
    assert len(samples) == 8
    assert samples[-1] == frames[-1]
    assert all(a.time_s < b.time_s for a, b in zip(samples, samples[1:]))


def test_window_close_during_final_wait_cancels_promptly(monkeypatch):
    now = 0
    poses = []

    def sleep(seconds):
        nonlocal now
        assert seconds <= 0.02
        now += seconds

    monkeypatch.setattr("mg400_agent.mujoco_replay.time.monotonic", lambda: now)
    monkeypatch.setattr("mg400_agent.mujoco_replay.time.sleep", sleep)
    model = SimpleNamespace(data=SimpleNamespace(time=0), set_joints=poses.append, state=lambda: {})
    viewer = SimpleNamespace(is_running=lambda: now < 0.1, lock=nullcontext, sync=lambda: None)
    result = replay(model, [Keyframe(0, (0, 0, 0, 0)), Keyframe(1000, (1, 2, 3, 4))], 0.001, viewer)
    assert result["completed"] is False
    assert result["samples"] == 1
    assert result["trajectory_time_s"] == 0
    assert poses == [(0, 0, 0, 0)]
    assert now < 0.13


@pytest.mark.parametrize("rate", ["0", "-1", "nan", "inf"])
def test_cli_rejects_invalid_rate(trajectory_path, capsys, rate):
    with pytest.raises(SystemExit) as caught:
        main(["--trajectory", str(trajectory_path), "--rate", rate])
    assert caught.value.code == 2
    assert "Rate must be positive and finite" in capsys.readouterr().err


def test_optional_dependency_error_explains_install(trajectory_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "mujoco", None)
    assert main(["--trajectory", str(trajectory_path)]) == 1
    assert "uv sync --extra mujoco" in capsys.readouterr().err


@pytest.mark.parametrize("module", ["mg400_agent.mujoco_replay", "mg400_agent"])
def test_macos_launcher_uses_real_python_libdir_without_global_environment_changes(
    tmp_path, monkeypatch, module
):
    launcher = tmp_path / "mjpython"
    launcher.touch()
    monkeypatch.setenv("DYLD_FALLBACK_LIBRARY_PATH", "/existing")
    monkeypatch.setattr("mg400_agent.mujoco_replay.sysconfig.get_path", lambda key: str(tmp_path))
    monkeypatch.setattr("mg400_agent.mujoco_replay.sysconfig.get_config_var", lambda key: "/uv/lib")
    calls = []
    monkeypatch.setattr("mg400_agent.mujoco_replay.os.execve", lambda *args: calls.append(args))
    launch_macos_viewer(["--viewer", "--trajectory", "path with spaces.json"], module=module)
    executable, arguments, environment = calls[0]
    assert executable == sys.executable
    assert arguments == [
        sys.executable,
        str(launcher),
        "-m",
        module,
        "--viewer",
        "--trajectory",
        "path with spaces.json",
    ]
    assert environment["DYLD_FALLBACK_LIBRARY_PATH"] == "/uv/lib:/existing"
    assert os.environ["DYLD_FALLBACK_LIBRARY_PATH"] == "/existing"


def test_headless_replay_uses_user_trajectory_without_network_viewer_or_physics(
    trajectory_path, monkeypatch, capsys
):
    mujoco = pytest.importorskip("mujoco")

    def forbidden(*args, **kwargs):
        pytest.fail("Offline kinematic replay must not connect, launch a viewer, or step physics")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(mujoco, "mj_step", forbidden)
    monkeypatch.setitem(sys.modules, "mujoco.viewer", None)
    assert main(["--trajectory", str(trajectory_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "offline_kinematic_playback"
    assert result["completed"] is True
    assert result["samples"] == 37
    assert result["trajectory_time_s"] == 1.2
    assert result["joints_deg"] == [0, 0, 0, 15]
    assert result["flange_yaw_deg"] == pytest.approx(15)
    assert result["flange_in_base_mm"] == pytest.approx(
        [278.4487429828484, -0.0001243781102, 353.251164344716]
    )
