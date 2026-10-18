import math
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mg400_agent.mirror import (
    JOINT_NAMES,
    UPDATE_SCRIPT,
    CoppeliaMirror,
    MirrorBindingLost,
    joint_angles,
    main,
)


def simulator():
    aliases = {100 + i: "mg400_" + name for i, name in enumerate(JOINT_NAMES)}
    uids = {handle: handle + 1000 for handle in [10, 200, *aliases]}
    return SimpleNamespace(
        object_uids=uids,
        simulation_stopped=0,
        sceneobject_joint=1,
        jointmode_kinematic=2,
        scripttype_customization=6,
        getSimulationState=Mock(return_value=0),
        getObject=Mock(return_value=10),
        getObjectUid=Mock(side_effect=uids.__getitem__),
        getObjectFromUid=Mock(
            side_effect=lambda uid, options: next((h for h, u in uids.items() if u == uid), -1)
        ),
        getObjectsInTree=Mock(return_value=list(reversed(aliases))),
        getObjectAlias=Mock(side_effect=aliases.__getitem__),
        getJointMode=Mock(return_value=[2, 0]),
        createScript=Mock(return_value=200),
        initScript=Mock(),
        callScriptFunction=Mock(
            side_effect=lambda name, script, *args: args[-1] if name.startswith("apply") else None
        ),
        removeObjects=Mock(),
    )


def test_resolves_names_independent_of_tree_order_and_updates_in_one_call():
    sim = simulator()
    mirror = CoppeliaMirror(sim, "/other/MG400")
    sim.getObject.assert_called_once_with("/other/MG400")
    assert mirror.joints == list(range(100, 108))
    error = mirror.update((90, 30, 70, -45))
    assert error == 0
    sim.callScriptFunction.assert_called_once_with(
        mirror.update_function,
        200,
        1200,
        1010,
        list(range(1100, 1108)),
        list(joint_angles((90, 30, 70, -45))),
    )
    mirror.close()
    sim.callScriptFunction.assert_called_with(mirror.close_function, 200, 1200)
    sim.removeObjects.assert_not_called()


def test_refuses_running_simulation_before_creating_a_script():
    sim = simulator()
    sim.getSimulationState.return_value = 1
    with pytest.raises(RuntimeError, match="Stop CoppeliaSim"):
        CoppeliaMirror(sim)
    sim.getObject.assert_not_called()
    sim.createScript.assert_not_called()


def test_refuses_nonkinematic_joint_before_creating_a_script():
    sim = simulator()
    sim.getJointMode.side_effect = [[2, 0], [3, 0]]
    with pytest.raises(ValueError, match="kinematic"):
        CoppeliaMirror(sim)
    sim.createScript.assert_not_called()


def test_refuses_missing_model_before_creating_a_script():
    sim = simulator()
    sim.getObject.side_effect = RuntimeError("Model does not exist")
    with pytest.raises(RuntimeError, match="Model does not exist"):
        CoppeliaMirror(sim)
    sim.createScript.assert_not_called()


def test_refuses_missing_joint_before_creating_a_script():
    sim = simulator()
    sim.getObjectsInTree.return_value.remove(107)
    with pytest.raises(ValueError, match="mg400_j5"):
        CoppeliaMirror(sim)
    sim.createScript.assert_not_called()


def test_refuses_duplicate_joint_alias_before_creating_a_script():
    sim = simulator()
    sim.getObjectsInTree.return_value.append(107)
    with pytest.raises(ValueError, match="Duplicate model joint"):
        CoppeliaMirror(sim)
    sim.createScript.assert_not_called()


def test_script_initialization_failure_attempts_owned_cleanup():
    sim = simulator()
    original = RuntimeError("Cannot initialize feedback script")
    sim.initScript.side_effect = original
    with pytest.raises(RuntimeError) as caught:
        CoppeliaMirror(sim)
    assert caught.value is original
    sim.removeObjects.assert_not_called()
    cleanup = sim.callScriptFunction.call_args
    assert cleanup.args[0].startswith("closeFeedback_")
    assert cleanup.args[1:] == (200, 1200)


def test_failed_initialization_cleanup_never_masks_original_error():
    sim = simulator()
    original = RuntimeError("Cannot initialize feedback script")
    sim.initScript.side_effect = original
    sim.callScriptFunction.side_effect = RuntimeError("Script is not initialized")
    with pytest.raises(RuntimeError) as caught:
        CoppeliaMirror(sim)
    assert caught.value is original
    sim.removeObjects.assert_not_called()


def test_invalid_input_does_not_change_model_pose():
    sim = simulator()
    mirror = CoppeliaMirror(sim)
    with pytest.raises(ValueError, match="finite"):
        mirror.update((1, 2, float("nan"), 4))
    sim.callScriptFunction.assert_not_called()


@pytest.mark.parametrize("readback", [[], [0] * 7, [0] * 9, [float("nan")] * 8])
def test_rejects_invalid_model_readback(readback):
    sim = simulator()
    mirror = CoppeliaMirror(sim)
    sim.callScriptFunction.side_effect = None
    sim.callScriptFunction.return_value = readback
    with pytest.raises(RuntimeError, match="Invalid joint readback"):
        mirror.update((0, 0, 0, 0))


def test_reports_joint_clamping_or_readback_mismatch():
    sim = simulator()
    mirror = CoppeliaMirror(sim)
    sim.callScriptFunction.side_effect = lambda name, script, *args: [
        *args[-1][:7],
        args[-1][7] + 0.01,
    ]
    with pytest.raises(RuntimeError, match="readback mismatch"):
        mirror.update((90, 30, 70, -45))


def test_accepts_equivalent_wrapped_joint_angles():
    sim = simulator()
    mirror = CoppeliaMirror(sim)
    sim.callScriptFunction.side_effect = lambda name, script, *args: [
        value + (i - 4) * 2 * math.pi for i, value in enumerate(args[-1])
    ]
    assert mirror.update((90, 30, 70, -45)) < 1e-12


def test_missing_optional_dependency_has_actionable_cli_error(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "coppeliasim_zmqremoteapi_client", None)
    assert main(["--host", "robot.test"]) == 1
    assert "uv sync --extra coppeliasim" in capsys.readouterr().err


@pytest.mark.parametrize("replacement_uid", [None, 9999])
def test_missing_or_reused_helper_is_not_called_or_deleted(replacement_uid):
    sim = simulator()
    mirror = CoppeliaMirror(sim)
    del sim.object_uids[200]
    if replacement_uid is not None:
        sim.object_uids[200] = replacement_uid
    with pytest.raises(MirrorBindingLost):
        mirror.update((0, 0, 0, 0))
    mirror.close()
    sim.callScriptFunction.assert_not_called()
    sim.removeObjects.assert_not_called()
    assert sim.createScript.call_count == 1


@pytest.mark.parametrize(
    "message",
    [
        "in sim.callScriptFunction: script does not exist.",
        "MG400_MIRROR_BINDING_LOST: model replaced",
        "MG400_MIRROR_BINDING_LOST: joint replaced",
    ],
)
def test_explicit_binding_loss_requests_outer_reconnect_without_local_retry(message):
    sim = simulator()
    mirror = CoppeliaMirror(sim)
    sim.callScriptFunction.side_effect = RuntimeError(message)
    with pytest.raises(MirrorBindingLost, match=message):
        mirror.update((0, 0, 0, 0))
    assert sim.callScriptFunction.call_count == 1
    assert sim.createScript.call_count == 1


@pytest.mark.parametrize(
    "error",
    [TimeoutError("Timeout"), RuntimeError("Stop simulation"), RuntimeError("Invalid joint mode")],
)
def test_unrelated_errors_are_not_reclassified_as_binding_loss(error):
    sim = simulator()
    mirror = CoppeliaMirror(sim)
    sim.callScriptFunction.side_effect = error
    with pytest.raises(type(error)) as caught:
        mirror.update((0, 0, 0, 0))
    assert caught.value is error


def test_each_binding_has_unique_update_and_cleanup_functions():
    first = CoppeliaMirror(simulator())
    second = CoppeliaMirror(simulator())
    assert first.update_function != second.update_function
    assert first.close_function != second.close_function


def test_close_is_idempotent_and_uses_owned_function_only():
    sim = simulator()
    mirror = CoppeliaMirror(sim)
    mirror.close()
    mirror.close()
    sim.callScriptFunction.assert_called_once_with(mirror.close_function, 200, 1200)
    sim.removeObjects.assert_not_called()


def run_mock_lua(body):
    executable = shutil.which("luajit") or shutil.which("lua")
    if executable is None:
        pytest.skip("Lua interpreter unavailable for offline helper tests")
    setup = """
local handlesByUid = {[1010]=10,[1200]=200}
local uidByHandle = {[10]=1010,[200]=1200}
for i=100,107 do handlesByUid[i+1000]=i; uidByHandle[i]=i+1000 end
local writes, removed = 0, 0
local positions = {}
local fakeSim = {simulation_stopped=0,jointmode_kinematic=2}
function fakeSim.getSimulationState() return 0 end
function fakeSim.getObject() return 200 end
function fakeSim.getObjectUid(handle) return uidByHandle[handle] end
function fakeSim.getObjectFromUid(uid) return handlesByUid[uid] or -1 end
function fakeSim.getJointMode() return 2 end
function fakeSim.setJointPosition(joint, angle) writes=writes+1; positions[joint]=angle end
function fakeSim.getJointPosition(joint) return positions[joint] end
function fakeSim.removeObjects(handles, delayed)
    assert(handles[1]==200 and delayed==true); removed=removed+1
end
package.preload['sim'] = function() return fakeSim end
local jointUids = {1100,1101,1102,1103,1104,1105,1106,1107}
local angles = {0.1,0.2,0.2,0.1,-0.2,-0.3,0.3,0.4}
"""
    code = setup + UPDATE_SCRIPT.replace("TOKEN", "test") + "\nsysCall_init()\n" + body
    result = subprocess.run([executable, "-"], input=code, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "change, error",
    [
        ("handlesByUid[1010]=nil", "MG400_MIRROR_BINDING_LOST"),
        ("handlesByUid[1107]=nil", "MG400_MIRROR_BINDING_LOST"),
        ("uidByHandle[200]=9999", "MG400_MIRROR_BINDING_LOST"),
        ("fakeSim.getSimulationState=function() return 1 end", "Stop simulation"),
        ("fakeSim.getJointMode=function(j) return j==107 and 3 or 2 end", "kinematic"),
    ],
)
def test_lua_validates_entire_binding_before_any_joint_write(change, error):
    run_mock_lua(
        change
        + f"""
local ok, err = pcall(applyFeedback_test,1200,1010,jointUids,angles)
assert(not ok and string.find(err, '{error}', 1, true))
assert(writes==0)
"""
    )


def test_lua_updates_all_joints_and_cleans_up_only_own_uid():
    run_mock_lua("""
local actual = applyFeedback_test(1200,1010,jointUids,angles)
assert(writes==8)
for i=1,8 do assert(actual[i]==angles[i]) end
closeFeedback_test(9999)
assert(removed==0)
closeFeedback_test(1200)
assert(removed==1)
""")
