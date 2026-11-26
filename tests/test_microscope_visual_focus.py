"""Rough 5x approach and sharpness-based lens progression, without hardware."""

import copy
from dataclasses import replace

import pytest
from test_microscope_service import demo_rig as _demo_rig
from test_microscope_service import dispatch, service_post, wait_command

from microscope.autofocus import NoFocusPeak
from microscope.turret_calibration import capture, new_focus_profile, validate_profile

demo_rig = _demo_rig


def teach_profile(rig):
    profile = new_focus_profile("X and initial 5x", "demo")
    rig.service._side.open()
    for objective in range(4):
        rig.port.sim.objective.position = objective * 740
        rig.port.focus_origin = rig.port.sim.focus.position = 0
        profile["captures"][str(objective)] = capture(
            rig.service._side.get_frame_packet()[2], objective
        )
    profile["initial_5x_capture"] = copy.deepcopy(profile["captures"]["0"])
    rig.service._side.close()
    rig.port.sim.objective.position = 0
    return validate_profile(profile)


def install(rig, half_range=62):
    profile = teach_profile(rig)
    rig.service._turret_store.save(profile)
    rig.service._turret_store.select(profile["profile_id"])
    service_post(rig, "heartbeat")
    rig.service._five_x_half_range = half_range
    rig.service._ten_x_half_range = 300
    rig.service._twenty_x_half_range = 300
    rig.service._fifty_x_half_range = 300
    return profile


def assert_ok(state):
    assert state["last_command"]["result"] == "ok", state["last_command"]


def test_near_initial_5x_needs_no_probe_or_focus_steps(demo_rig):
    rig = demo_rig()
    install(rig)
    before = len(rig.port.commands)
    state = dispatch(rig, "rough_focus_5x")
    assert_ok(state)
    assert state["last_result"]["rough_position"] == "already_near"
    assert not any(c.startswith("FOCUS MOVE") for c in rig.port.commands[before:])
    assert not rig.port.sim.focus.enabled


def test_individual_autofocus_and_adjacent_selection(demo_rig):
    rig = demo_rig()
    install(rig)
    state = dispatch(rig, "autofocus", mode="full")
    assert_ok(state)
    assert state["autofocus_run"]["verified"]
    assert state["autofocus_run"]["completed"] == [0]
    assert not rig.port.sim.focus.enabled
    before = len(rig.port.commands)
    state = dispatch(rig, "select_objective", id=1)
    assert_ok(state)
    assert state["objective_id"] == 1
    assert not state["autofocus_run"]["verified"]
    assert not any(c.startswith("FOCUS MOVE") for c in rig.port.commands[before:])
    state = dispatch(rig, "autofocus", mode="local")
    assert_ok(state)
    assert state["autofocus_run"]["completed"] == [1]


@pytest.mark.parametrize("source,target", [(0, 1), (0, 3), (3, 0), (2, 0), (1, 3), (2, 2)])
def test_direct_selection_allows_any_lens_without_autofocus(demo_rig, monkeypatch, source, target):
    rig = demo_rig()
    install(rig)
    rig.port.sim.objective.position = source * 740

    def no_autofocus(*args, **kwargs):
        raise AssertionError("Direct selection must not inspect sample sharpness or autofocus")

    monkeypatch.setattr(rig.service, "_sharpness", no_autofocus)
    monkeypatch.setattr(rig.service, "_autofocus", no_autofocus)
    before = len(rig.port.commands)
    state = dispatch(rig, "select_objective", id=target)
    assert_ok(state)
    assert state["objective_id"] == target
    assert len(state["last_result"]["switches"]) == abs(target - source)
    assert state["last_result"]["autofocus_run"] is False
    assert not state["autofocus_run"]["verified"]
    assert not any(c.startswith("FOCUS MOVE") for c in rig.port.commands[before:])
    assert not rig.port.sim.focus.enabled


def test_non5x_automatic_sequence_is_refused_before_power(demo_rig):
    rig = demo_rig()
    install(rig)
    rig.port.sim.objective.position = 740
    before = len(rig.port.commands)
    state = dispatch(rig, "autofocus_sequence")
    assert state["last_command"]["result"] == "failed"
    assert not any(" MOVE " in c or c.endswith(" ENABLE") for c in rig.port.commands[before:])


def test_stop_interrupts_direct_selection_before_another_lens_turn(demo_rig, monkeypatch):
    rig = demo_rig()
    install(rig)
    factory = rig.service._switcher_factory
    turns = []

    def switcher(*args, **kwargs):
        value = factory(*args, **kwargs)
        run = value.run

        def stop_after_turn(*args, **kwargs):
            result = run(*args, **kwargs)
            turns.append(result.target_id)
            rig.service.stop("human-stop", 0)
            return result

        value.run = stop_after_turn
        return value

    monkeypatch.setattr(rig.service, "_switcher_factory", switcher)
    state = dispatch(rig, "select_objective", id=3)
    assert state["last_command"]["result"] == "stopped"
    assert turns == [1]
    assert not rig.port.sim.focus.enabled
    assert not rig.port.sim.objective.enabled


def test_sequence_advances_only_after_each_verified_focus(demo_rig, monkeypatch):
    rig = demo_rig()
    install(rig)
    ranges = {0: 62, 1: 80, 2: 100, 3: 120}
    settings = {
        "five_x_half_range_pulses": 62,
        "ten_x_half_range_pulses": 80,
        "twenty_x_half_range_pulses": 100,
        "fifty_x_half_range_pulses": 120,
    }
    assert service_post(rig, "autofocus-settings", settings).status_code == 200
    factory = rig.service._switcher_factory
    paths = []
    autofocus_factory = rig.service._autofocus_factory
    scans = []

    def tracked_autofocus(*args, **kwargs):
        focuser = autofocus_factory(*args, **kwargs)
        run = focuser.run

        def tracked_scan(**options):
            objective = rig.service._objective_id
            scans.append((objective, options["half_range"]))
            assert options["half_range"] == ranges[objective]
            assert (
                rig.service.snapshot()["autofocus_run"]["search_half_range_pulses"]
                == ranges[objective]
            )
            if objective == 0:
                refused = service_post(rig, "autofocus-settings", {"ten_x_half_range_pulses": 999})
                assert refused.status_code == 409
                # Simulate a future internal update after dispatch. The job owns all 4 values.
                rig.service._ten_x_half_range = 999
                rig.service._twenty_x_half_range = 999
                rig.service._fifty_x_half_range = 999
            return run(**options)

        focuser.run = tracked_scan
        return focuser

    def tracked_switcher(*args, **kwargs):
        switcher = factory(*args, **kwargs)
        run = switcher.run

        def tracked_run(direction, path, **options):
            assert path not in paths
            paths.append(path)
            return run(direction, path, **options)

        switcher.run = tracked_run
        return switcher

    monkeypatch.setattr(rig.service, "_switcher_factory", tracked_switcher)
    monkeypatch.setattr(rig.service, "_autofocus_factory", tracked_autofocus)
    before = len(rig.port.commands)
    response = service_post(rig, "command", {"action": "autofocus_sequence"})
    state = wait_command(rig, response.json["command_id"], timeout=25)
    assert_ok(state)
    assert state["autofocus_run"]["completed"] == [0, 1, 2, 3]
    assert state["autofocus_run"]["verified"]
    assert state["objective_id"] == 3
    assert scans == list(ranges.items())
    assert state["autofocus_run"]["search_half_range_pulses"] == 120
    assert paths == [
        f"microscope/{response.json['command_id']}-switch-{index}.csv" for index in range(3)
    ]
    commands = rig.port.commands[before:]
    assert commands.count("FOCUS ZERO") == 1
    assert (
        sum(abs(int(c.split()[-1])) for c in commands if c.startswith("FOCUS MOVE"))
        == state["visual_focus"]["travel_used_pulses"]
    )
    assert state["visual_focus"]["travel_limit_pulses"] == 16 * sum(ranges.values()) + 20000


@pytest.mark.parametrize("objective,half_range", [(0, 62), (1, 80), (2, 100), (3, 120)])
def test_individual_scan_uses_frozen_range_for_actual_camera_lens(
    demo_rig, monkeypatch, objective, half_range
):
    rig = demo_rig()
    install(rig)
    fields = ("five_x", "ten_x", "twenty_x", "fifty_x")
    settings = {
        name + "_half_range_pulses": value for name, value in zip(fields, (62, 80, 100, 120))
    }
    assert service_post(rig, "autofocus-settings", settings).status_code == 200
    rig.port.sim.objective.position = objective * 740
    rig.service._objective_id = (objective + 1) % 4  # Deliberately outdated cached identity.
    original = rig.service._prepare_visual

    def after_dispatch(job):
        assert job.autofocus_half_ranges[objective] == half_range
        setattr(rig.service, "_" + fields[objective] + "_half_range", 999)
        return original(job)

    monkeypatch.setattr(rig.service, "_prepare_visual", after_dispatch)
    state = dispatch(rig, "autofocus", mode="local" if objective % 2 else "full")
    assert_ok(state)
    assert state["objective_id"] == objective
    assert state["autofocus_run"]["search_half_range_pulses"] == half_range
    assert state["visual_focus"]["travel_limit_pulses"] == 16 * half_range + (
        20000 if objective == 0 else 0
    )


def test_failed_af_never_rotates_turret(demo_rig, monkeypatch):
    rig = demo_rig()
    install(rig)

    def failure(*_args):
        raise NoFocusPeak("No verified peak")

    monkeypatch.setattr(rig.service, "_autofocus", failure)
    before = len(rig.port.commands)
    state = dispatch(rig, "autofocus_sequence")
    assert state["last_command"]["result"] == "failed"
    assert state["autofocus_run"]["completed"] == []
    assert not any(c.startswith("OBJ MOVE") for c in rig.port.commands[before:])
    assert not rig.port.sim.focus.enabled


def test_wide_5x_range_is_not_clipped_by_manual_cap(demo_rig, monkeypatch):
    rig = demo_rig()
    install(rig, half_range=5000)
    assert service_post(rig, "focus-travel-limit", {"pulses": 1000}).status_code == 200
    from microscope.autofocus import FocusMeasurement, focus_score

    class Focuser:
        def __init__(self, camera, motor, **_kwargs):
            self.camera, self.motor = camera, motor

        def run(self, **kwargs):
            assert kwargs["half_range"] == 5000
            self.motor.focus_move(12)
            import time

            time.sleep(0.1)
            return FocusMeasurement(
                12, focus_score(self.camera.get_frame_packet(raw=True)[2]), "final"
            )

    monkeypatch.setattr(rig.service, "_autofocus_factory", Focuser)
    state = dispatch(rig, "autofocus", mode="full")
    assert_ok(state)
    assert state["visual_focus"]["travel_limit_pulses"] == 81000
    assert state["focus_travel_limit_pulses"] == 1000


def test_direct_selection_does_not_require_previous_focus_to_remain_sharp(demo_rig):
    rig = demo_rig()
    install(rig)
    assert_ok(dispatch(rig, "autofocus", mode="local"))
    rig.port.focus_origin += 80
    before = len(rig.port.commands)
    state = dispatch(rig, "select_objective", id=1)
    assert_ok(state)
    assert state["objective_id"] == 1
    assert not any(c.startswith("FOCUS MOVE") for c in rig.port.commands[before:])


def test_direct_selection_does_not_require_sharpness_after_focus_wake(demo_rig, monkeypatch):
    rig = demo_rig()
    install(rig)
    assert_ok(dispatch(rig, "autofocus", mode="local"))
    enable = rig.service._motor.focus_enable

    def shift(**kwargs):
        result = enable(**kwargs)
        rig.port.focus_origin += 80
        return result

    monkeypatch.setattr(rig.service._motor, "focus_enable", shift)
    before = len(rig.port.commands)
    state = dispatch(rig, "select_objective", id=1)
    assert_ok(state)
    assert state["objective_id"] == 1
    assert not any(c.startswith("FOCUS MOVE") for c in rig.port.commands[before:])


def test_side_close_failure_still_finishes_job_and_disables_focus(demo_rig, monkeypatch):
    rig = demo_rig()
    install(rig)
    close = rig.service._side.close

    def failed_close():
        close()
        raise RuntimeError("Reader close failed")

    with monkeypatch.context() as patch:
        patch.setattr(rig.service._side, "close", failed_close)
        state = dispatch(rig, "rough_focus_5x")
    assert state["last_command"]["result"] == "failed"
    assert rig.service._job is None and not rig.port.sim.focus.enabled


@pytest.mark.parametrize("unverified", ["missing", "disconnected", "fault", "session", "epoch"])
def test_unverified_controller_status_does_not_claim_motor_power(demo_rig, unverified):
    rig = demo_rig()
    status = rig.service._motor.read_status()
    rig.service._status = replace(
        status,
        objective=replace(status.objective, enabled=False, sleeping=True),
        focus=replace(status.focus, enabled=False, sleeping=True),
    )
    assert rig.service.snapshot()["motor_power"]["focus"] == {"enabled": False, "sleeping": True}
    if unverified == "missing":
        rig.service._status = None
    elif unverified == "disconnected":
        rig.port.is_open = False
    elif unverified == "fault":
        rig.service._motor._fail("Serial read failed")
    elif unverified == "session":
        rig.service._status = replace(rig.service._status, session=status.session - 1)
    else:
        rig.service._status = replace(
            rig.service._status, reference_epoch=status.reference_epoch - 1
        )
    assert rig.service.snapshot()["motor_power"] == {
        "objective": {"enabled": None, "sleeping": None},
        "focus": {"enabled": None, "sleeping": None},
    }
    if unverified == "disconnected":
        rig.port.is_open = True
