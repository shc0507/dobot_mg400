"""Check fingertip geometry without robot clients or host configuration."""

import math
import random
from dataclasses import asdict

import pytest

from orchestrator.tool import Pose, ToolOffset, fingertip_to_flange, flange_to_fingertip


@pytest.mark.parametrize(
    "angle,xy", [(0, (13, 24)), (90, (6, 23)), (180, (7, 16)), (270, (14, 17)), (-90, (14, 17))]
)
def test_offset_rotates_with_flange_and_positive_dz_lowers_fingertip(angle, xy):
    result = flange_to_fingertip(Pose(10, 20, 30, angle), ToolOffset(3, 4, 5))
    assert (result.x_mm, result.y_mm) == pytest.approx(xy)
    assert result.z_mm == 25
    assert result.r_deg == angle
    assert fingertip_to_flange(result, ToolOffset(3, 4, 5)) == Pose(10, 20, 30, angle)


def test_inverse_round_trips_arbitrary_offsets_and_keeps_unwrapped_rotation():
    randomizer = random.Random(307)
    for _ in range(100):
        pose = Pose(
            *(randomizer.uniform(-500, 500) for _ in range(3)), randomizer.uniform(-1440, 1440)
        )
        offset = ToolOffset(*(randomizer.uniform(-50, 50) for _ in range(3)))
        for forward, inverse in (
            (flange_to_fingertip, fingertip_to_flange),
            (fingertip_to_flange, flange_to_fingertip),
        ):
            converted = forward(pose, offset)
            assert converted.r_deg == pose.r_deg
            assert asdict(inverse(converted, offset)) == pytest.approx(asdict(pose))


@pytest.mark.parametrize(
    "invalid", [True, False, None, "1", math.nan, math.inf, -math.inf, 10**1000]
)
def test_pose_and_offset_reject_nonfinite_or_non_numeric_fields(invalid):
    for index in range(4):
        values = [0, 0, 0, 0]
        values[index] = invalid
        with pytest.raises(ValueError):
            Pose(*values)
    for index in range(3):
        values = [0, 0, 0]
        values[index] = invalid
        with pytest.raises(ValueError):
            ToolOffset(*values)


@pytest.mark.parametrize("function", [flange_to_fingertip, fingertip_to_flange])
def test_computed_overflow_is_refused(function):
    with pytest.raises(ValueError):
        function(
            Pose(1e308, 0, 0, 0),
            ToolOffset(1e308 if function is flange_to_fingertip else -1e308, 0, 0),
        )
    with pytest.raises(ValueError):
        function(Pose(0, 0, 0, 45), ToolOffset(1.7e308, 1.7e308, 0))


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"offset_mm": []},
        {"offset_mm": [1, 2]},
        {"offset_mm": [1, 2, 3, 4]},
        {"offset_mm": [1, 2, True]},
        {"offset_mm": [1, 2, 3], "extra": 4},
    ],
)
def test_tool_configuration_requires_explicit_three_measured_values(data):
    with pytest.raises(ValueError):
        ToolOffset.from_config(data)


def test_explicit_measured_zero_offset_is_valid_but_never_implicit():
    offset = ToolOffset.from_config({"offset_mm": [0, 0, 0]})
    assert flange_to_fingertip(Pose(230, 0, 20, 720), offset) == Pose(230, 0, 20, 720)
