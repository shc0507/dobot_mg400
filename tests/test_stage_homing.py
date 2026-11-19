"""Legacy geometry files remain readable; they do not drive encoder moves."""

import copy
import math

import pytest

from stage_controller.config import Axis, load_geometry, parse_geometry
from stage_controller.protocol import MOTOR_IDS


def test_quantization_rounds_bounds_inward_and_rejects_unrepresentable_target():
    axis = Axis(10, 2, 0.11, 0.29, {})
    assert axis.bounds(2) == (2, 2)
    assert axis.target(0.2, 2) == 2
    with pytest.raises(ValueError, match="Rounded"):
        axis.target(0.11, 2)
    with pytest.raises(ValueError, match="no representable"):
        Axis(1, 1, 0.1, 0.2, {}).bounds(1)
    with pytest.raises(ValueError, match="pulse range"):
        Axis(1e300, 1, 0, 10, {}).bounds(16)


def config_data():
    return {
        "motors": {
            motor: dict(
                steps_per_mm=10,
                microstep=1,
                min_mm=0,
                max_mm=100,
                positions={"load": 1, "furnace": 20} if motor == "insert" else {},
            )
            for motor in MOTOR_IDS
        }
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("steps_per_mm", True),
        ("steps_per_mm", 0),
        ("steps_per_mm", math.inf),
        ("microstep", True),
        ("microstep", 3),
        ("min_mm", 101),
        ("max_mm", math.nan),
    ],
)
def test_bad_config_is_rejected(key, value):
    data = config_data()
    data["motors"]["bottom"][key] = value
    with pytest.raises(ValueError):
        parse_geometry(data)


def test_configuration_names_pose_shape_and_bounds():
    data = config_data()
    data["poses"] = {
        "furnace": dict(
            order=["insert", "bottom", "top"], positions=dict(bottom=1, top=2, insert=20)
        )
    }
    assert parse_geometry(data).poses["furnace"][0] == ("insert", 20)
    for edit in (
        lambda value: value["poses"]["furnace"].update(order=["insert", "bottom", "bottom"]),
        lambda value: value["motors"]["insert"]["positions"].update(furnace=101),
        lambda value: value["motors"]["insert"]["positions"].update({"../bad": 1}),
        lambda value: value.update(unexpected=True),
    ):
        modified = copy.deepcopy(data)
        edit(modified)
        with pytest.raises(ValueError):
            parse_geometry(modified)


def test_config_file_size_is_checked(tmp_path):
    path = tmp_path / "stage.toml"
    path.write_bytes(b" " * 1_048_577)
    with pytest.raises(ValueError, match="1 MiB"):
        load_geometry(path)
