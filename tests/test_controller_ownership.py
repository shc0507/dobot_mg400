"""Check connection ownership with real OS locks and no robot connections."""

import os
import subprocess
import sys

import pytest

from mg400_agent.controller_ownership import ControllerOwnership


def test_another_panel_cannot_acquire_until_the_owner_releases(tmp_path):
    isaac = ControllerOwnership("192.168.1.6", directory=tmp_path)
    web = ControllerOwnership("192.168.1.6", directory=tmp_path)
    assert not isaac.held
    isaac.acquire()
    try:
        isaac.acquire()
        assert isaac.held
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            web.acquire()
        assert not web.held
        web.release()
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            web.acquire()
    finally:
        isaac.release()
    assert not isaac.held
    web.acquire()
    assert web.held
    web.release()
    web.release()


def test_a_separate_process_cannot_acquire_an_owned_robot(tmp_path):
    owner = ControllerOwnership("192.168.1.6", directory=tmp_path)
    owner.acquire()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "\n".join(
                    [
                        "import sys",
                        "from mg400_agent.controller_ownership import ControllerOwnership",
                        "lease = ControllerOwnership('192.168.1.6', directory=sys.argv[1])",
                        "try:",
                        "    lease.acquire()",
                        "except RuntimeError as error:",
                        "    print(error)",
                        "else:",
                        "    lease.release()",
                        "    raise AssertionError('A second process acquired the robot')",
                    ]
                ),
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Robot controlled by another panel" in result.stdout
    finally:
        owner.release()


def test_process_exit_releases_ownership_without_a_stale_file_lock(tmp_path):
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "\n".join(
                [
                    "import sys",
                    "from mg400_agent.controller_ownership import ControllerOwnership",
                    "lease = ControllerOwnership('192.168.1.6', directory=sys.argv[1])",
                    "lease.acquire()",
                    "print('owned', flush=True)",
                    "sys.stdin.readline()",
                ]
            ),
            str(tmp_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    contender = ControllerOwnership("192.168.1.6", directory=tmp_path)
    try:
        assert child.stdout.readline().strip() == "owned"
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            contender.acquire()
        child.terminate()
        child.wait(timeout=5)
        contender.acquire()
        assert contender.held
    finally:
        contender.release()
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_losing_an_owner_object_does_not_clear_an_unreleased_safety_lock(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "import gc, sys",
                    "from mg400_agent.controller_ownership import ControllerOwnership",
                    "lease = ControllerOwnership('192.168.1.6', directory=sys.argv[1])",
                    "lease.acquire()",
                    "del lease",
                    "gc.collect()",
                    "try:",
                    "    ControllerOwnership('192.168.1.6', directory=sys.argv[1]).acquire()",
                    "except RuntimeError as error:",
                    "    print(error)",
                    "else:",
                    "    raise AssertionError('Lost object silently released ownership')",
                ]
            ),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Robot controlled by another panel" in result.stdout


def test_release_does_not_split_the_lock_file_between_waiting_panels(tmp_path):
    owners = [ControllerOwnership("192.168.1.6", directory=tmp_path) for _ in range(3)]
    owners[0].acquire()
    try:
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            owners[1].acquire()
        owners[0].release()
        owners[1].acquire()
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            owners[2].acquire()
    finally:
        for owner in owners:
            owner.release()


def test_shared_directory_environment_is_used_by_independent_instances(monkeypatch, tmp_path):
    monkeypatch.setenv("MG400_CONTROLLER_LOCK_DIR", str(tmp_path))
    owner = ControllerOwnership("ROBOT.TEST.")
    contender = ControllerOwnership("robot.test")
    other_robot = ControllerOwnership("192.168.1.7")
    owner.acquire()
    try:
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            contender.acquire()
        other_robot.acquire()
        assert other_robot.held
    finally:
        owner.release()
        other_robot.release()


def test_unwritable_lock_directory_prevents_ownership(monkeypatch, tmp_path):
    def deny_open(*args):
        raise PermissionError("Lock directory is not writable")

    monkeypatch.setattr(os, "open", deny_open)
    owner = ControllerOwnership("robot.test", directory=tmp_path)
    with pytest.raises(RuntimeError, match="Cannot acquire robot connection ownership"):
        owner.acquire()
    assert not owner.held


def test_relative_directory_is_rejected_before_connection(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MG400_CONTROLLER_LOCK_DIR", "different-in-each-working-directory")
    owner = ControllerOwnership("robot.test")
    with pytest.raises(RuntimeError, match="absolute"):
        owner.acquire()
    assert not owner.held
