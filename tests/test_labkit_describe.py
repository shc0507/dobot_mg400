import ast
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from labkit.describe import command, describe, parameter


def metadata(**overrides):
    args = dict(
        service="demo",
        version="1.0",
        demo=True,
        zones=["tray"],
        on_lease_expiry="stop motion",
        commands={"move": command()},
    )
    args.update(overrides)
    return describe(**args)


def test_schema_and_description_are_json_values_with_independent_copies():
    params = parameter("object", properties={"x_mm": parameter("number", minimum=0, maximum=50)})
    spec = command(params)
    result = metadata(commands={"move": spec, "stop": command(any_client=True)})
    assert json.loads(json.dumps(result)) == result
    assert result["contract"] == 1
    assert result["lease_seconds"] == 3
    assert result["commands"]["stop"]["any_client"] is True
    params["properties"].clear()
    spec["params"]["properties"].clear()
    assert result["commands"]["move"]["params"]["properties"]["x_mm"]["maximum"] == 50


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"params": None, "any_client": False},
        {"params": {"type": "array"}, "any_client": False},
        {"params": {"type": "object"}, "any_client": "false"},
    ],
)
def test_incomplete_or_invalid_command_description_is_rejected(spec):
    with pytest.raises(ValueError):
        metadata(commands={"move": spec})


def test_parameter_type_cannot_be_replaced():
    with pytest.raises(ValueError):
        parameter("integer", type="unknown")
    with pytest.raises(ValueError):
        parameter("unknown")


@pytest.mark.parametrize(
    "override", [{"demo": 1}, {"lease_seconds": float("nan")}, {"version": ""}, {"zones": "tray"}]
)
def test_invalid_description_fields(override):
    with pytest.raises(ValueError):
        metadata(**override)


def test_modules_other_than_flask_only_import_stdlib_or_labkit():
    import labkit

    directory = Path(labkit.__file__).parent
    for path in directory.glob("*.py"):
        if path.stem == "flask":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module.split(".")[0]]
            else:
                continue
            assert set(names) <= sys.stdlib_module_names | {"labkit"}, path


def test_imports_start_no_thread_open_no_device_and_write_no_file(tmp_path):
    source = Path(importlib.import_module("labkit").__file__).parents[1]
    script = """
import sys
import flask, urllib.request, threading, socket, builtins
sys.dont_write_bytecode = True
def forbidden(*args, **kwargs):
    raise AssertionError("Import attempted external work")
threading.Thread.start = forbidden
socket.socket = forbidden
builtins.open = forbidden
for name in ("lease", "events", "describe", "client", "flask", "errors"):
    __import__("labkit." + name)
"""
    env = dict(os.environ, PYTHONPATH=str(source))
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
