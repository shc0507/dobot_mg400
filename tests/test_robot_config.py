import socket
import tomllib

import pytest
from helpers import block_network

from mg400.robot_config import load_robot_host


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    block_network(monkeypatch, "create_connection", message="Network prohibited")
    monkeypatch.delenv("MG400_ROBOT_HOST", raising=False)


def test_reads_requested_file_without_network(tmp_path):
    config = tmp_path / "robot.toml"
    config.write_text('[robot]\nhost = "robot.test"\n')
    assert load_robot_host(config) == "robot.test"
    socket.create_connection.assert_not_called()


def test_missing_file_does_not_fall_back_to_another_robot(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_robot_host(tmp_path / "missing.toml")


def test_malformed_toml_is_rejected(tmp_path):
    config = tmp_path / "robot.toml"
    config.write_text("[robot\n")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_robot_host(config)


@pytest.mark.parametrize("contents", ("", "[robot]\n", 'robot = "192.168.1.6"\n'))
def test_missing_host_is_rejected(tmp_path, contents):
    config = tmp_path / "robot.toml"
    config.write_text(contents)
    with pytest.raises(ValueError, match=r"must contain \[robot\]\.host"):
        load_robot_host(config)


@pytest.mark.parametrize("host", ('""', '" "', '" robot.test"', '"robot name"', "6", "true", "[]"))
def test_invalid_host_is_rejected(tmp_path, host):
    config = tmp_path / "robot.toml"
    config.write_text(f"[robot]\nhost = {host}\n")
    with pytest.raises(ValueError, match="nonempty string without whitespace"):
        load_robot_host(config)


def test_environment_address_wins_without_reading_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MG400_ROBOT_HOST", "robot.env")
    assert load_robot_host(tmp_path / "missing.toml") == "robot.env"


def test_empty_environment_address_reads_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MG400_ROBOT_HOST", "")
    config = tmp_path / "robot.toml"
    config.write_text('[robot]\nhost = "robot.test"\n')
    assert load_robot_host(config) == "robot.test"


def test_environment_address_with_whitespace_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("MG400_ROBOT_HOST", "robot name")
    with pytest.raises(ValueError, match="nonempty string without whitespace"):
        load_robot_host(tmp_path / "missing.toml")
