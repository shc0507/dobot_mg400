"""Collision check of straight arm moves against a small bench world.

`fixtures/collision_world.json` is a part of the bench twin's collision world: the arm
with the suction tool and the wrist camera, the table, the tray table and the pole of the
tray camera. The tray plate top is at flange Z 22.0 for the cup tip.
"""

import json
from pathlib import Path

import pytest

from orchestrator import collision

WORLD = Path(__file__).parent / "fixtures/collision_world.json"
OVER_TRAY = [-69.3, -237.9, 163.7, -31.3]


def at(z):
    return [*OVER_TRAY[:2], z, OVER_TRAY[3]]


def check(start, target, **options):
    return collision.check(start, target, path=WORLD, **options)


def test_the_move_into_the_camera_pole_is_refused():
    # 2026-11-24: this move touched a screw on the pole about 58 mm along the line.
    problem = check(OVER_TRAY, [-137.2, -245.1, 163.7, -31.3])
    assert problem["obstacle"] == "pole tube 1" and problem["group"] == "fixtures"
    assert problem["clearance_mm"] == 0 and problem["start_clearance_mm"] is None
    assert 40 < problem["along_mm"] < 60
    assert problem["pose"][2:] == [163.7, -31.3]


def test_the_move_over_the_tray_is_clear():
    joints = [-99.56, -12.92, -15.55, 72.3]
    assert check([-39, -236, 163.8, -31.3], [41, -236, 163.8, -31.3], joints=joints) is None
    assert check([-39, -236, 163.8, -31.3], [41, -236, 163.8, -31.3]) is None


def test_a_move_must_keep_the_margin():
    assert check(OVER_TRAY, at(22.0 + 40)) is None
    problem = check(OVER_TRAY, at(22.0 + 5))
    assert problem["obstacle"] == "tray plate" and problem["arm_part"] == "suction cup pad"
    assert problem["clearance_mm"] == pytest.approx(5.0, abs=0.2)
    assert problem["along_mm"] == pytest.approx(163.7 - 27.0, abs=0.1)
    assert check(OVER_TRAY, at(22.0 + 5), margin=3.0) is None
    assert check(OVER_TRAY, at(22.0))["clearance_mm"] == 0


def test_inside_the_margin_a_move_must_not_go_closer():
    near = at(22.0 + 3)
    assert check(near, at(100.0)) is None
    assert check(near, [-29.3, -237.9, 25.0, -31.3]) is None
    problem = check(near, at(22.0 + 1))
    assert problem["obstacle"] == "tray plate"
    assert problem["start_clearance_mm"] == pytest.approx(3.0, abs=0.2)
    assert problem["clearance_mm"] == pytest.approx(1.0, abs=0.2)
    # A near obstacle does not hide the other obstacles.
    assert check(near, [-137.2, -245.1, 25.0, -31.3])["obstacle"] == "pole tube 1"


def test_a_move_that_starts_in_contact_is_checked_against_the_other_obstacles():
    touching = at(21.5)
    assert check(touching, at(100.0)) is None
    assert check(touching, [-137.2, -245.1, 21.5, -31.3])["group"] == "fixtures"


def stage_world(tmp_path):
    """The fixture world with a block on a stage_top carriage, 89 mm beyond the move end."""
    world = json.loads(WORLD.read_text())
    world["nodes"].append(
        {
            "name": "carriage",
            "parent": None,
            "rest": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "joint": "stage_top",
            "type": "prismatic",
            "axis": [1, 0, 0],
            "coef": None,
        }
    )
    lo, hi = (130.0, -260.0, 100.0), (170.0, -220.0, 200.0)
    corners = [
        [(lo, hi)[i][0], (lo, hi)[j][1], (lo, hi)[k][2]]
        for i in (0, 1)
        for j in (0, 1)
        for k in (0, 1)
    ]
    world["shapes"].append(
        {
            "object": "block",
            "entity": "stage",
            "node": "carriage",
            "body": "env",
            "verts": corners,
            "faces": collision.BOX_FACES,
        }
    )
    path = tmp_path / "world.json"
    path.write_text(json.dumps(world))
    return path


STAGE_MOVE = ([-39, -236, 163.8, -31.3], [41, -236, 163.8, -31.3])


def test_a_stage_joint_moves_its_obstacles(tmp_path):
    path = stage_world(tmp_path)
    for value in (0.0, 45.0):
        assert collision.check(*STAGE_MOVE, path=path, stage={"stage_top": value}) is None
    problem = collision.check(*STAGE_MOVE, path=path, stage={"stage_top": -70.0, "insert": None})
    assert problem["obstacle"] == "block" and problem["group"] == "stage"


def test_an_unknown_stage_joint_sweeps_its_obstacles_over_the_travel(tmp_path):
    """The block reaches x 60 at the travel end -70 mm, so the move is refused."""
    path = stage_world(tmp_path)
    for stage in (None, {}, {"stage_top": None}, {"stage_top": "x"}, {"stage_top": float("nan")}):
        problem = collision.check(*STAGE_MOVE, path=path, stage=stage)
        assert problem["obstacle"] == "block" and problem["clearance_mm"] == 0
    world = collision.World(path, {})
    (block,) = [shape for shape, _ in world.env if shape["object"] == "block"]
    xs = [vertex[0] for vertex in block["verts"]]
    assert block["node"] is None and (min(xs), max(xs)) == (60.0, 215.0)


def test_the_check_says_when_it_cannot_run(tmp_path):
    with pytest.raises(collision.Unavailable, match="Cannot read the collision world"):
        collision.check(OVER_TRAY, at(100.0), path=tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(collision.Unavailable, match="Cannot read the collision world"):
        collision.check(OVER_TRAY, at(100.0), path=tmp_path / "bad.json")
    with pytest.raises(collision.Unavailable, match="no joint solution"):
        check(OVER_TRAY, [600.0, 0.0, 163.7, 0.0])
    with pytest.raises(collision.Unavailable, match="not finite"):
        check(OVER_TRAY, [float("nan"), 0.0, 163.7, 0.0])


def test_a_move_that_starts_inside_a_swept_box_is_refused(tmp_path):
    path = stage_world(tmp_path)
    start = [60.0, -236.0, 163.8, -31.3]  # inside the box of the block over the travel
    for target in ([60.0, -236.0, 200.0, -31.3], [-39.0, -236.0, 163.8, -31.3]):
        problem = collision.check(start, target, path=path, stage={})
        assert problem["obstacle"] == "block" and problem["swept"] is True
        assert problem["along_mm"] == 0 and problem["start_clearance_mm"] == 0
    assert (
        collision.check(start, [60.0, -236.0, 200.0, -31.3], path=path, stage={"stage_top": 45.0})
        is None
    )
