"""Check connection ownership with real OS locks and no robot connections."""

import gc
import os
import subprocess
import sys

import pytest

from mg400_agent import controller_ownership
from mg400_agent.controller_ownership import ControllerOwnership


def test_another_panel_cannot_acquire_until_the_owner_releases(tmp_path):
    owner = ControllerOwnership("192.168.1.6", directory=tmp_path)
    web = ControllerOwnership("192.168.1.6", directory=tmp_path)
    third = ControllerOwnership("192.168.1.6", directory=tmp_path)
    assert not owner.held
    owner.acquire()
    try:
        owner.acquire()
        assert owner.held
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            web.acquire()
        assert not web.held
        web.release()
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            web.acquire()
        owner.release()
        assert not owner.held
        web.acquire()
        assert web.held
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            third.acquire()
    finally:
        owner.release()
        web.release()
        web.release()
        third.release()


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
    lease = ControllerOwnership("192.168.1.6", directory=tmp_path)
    lease.acquire()
    del lease
    gc.collect()
    with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
        ControllerOwnership("192.168.1.6", directory=tmp_path).acquire()


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


def test_root_refuses_a_lock_directory_that_another_user_owns(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        os.chown(tmp_path, 1000, 1000)
    monkeypatch.setattr(controller_ownership.os, "geteuid", lambda: 0)
    lease = ControllerOwnership("192.168.1.6", directory=tmp_path)
    with pytest.raises(RuntimeError, match=f"Run as UID {tmp_path.stat().st_uid}"):
        lease.acquire()
    assert not lease.held
    assert list(tmp_path.iterdir()) == []
