import asyncio
import threading
from dataclasses import dataclass, field

import pytest

from mg400_agent.keyboard_control import KeyboardController


@dataclass
class Motion:
    key: str
    released: threading.Event
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    error: Exception | None = None


class Backend:
    def __init__(self):
        self.motions = []
        self.tasks = []
        self.release_count = 0
        self.allowed = True
        self.controller = KeyboardController(
            self.run_hold,
            self.release_hold,
            lambda: self.allowed,
            self.tasks.append,
        )

    async def run_hold(self, key, released):
        motion = Motion(key, released)
        self.motions.append(motion)
        await motion.stopped.wait()
        if motion.error is not None:
            raise motion.error

    def release_hold(self):
        self.release_count += 1

    async def finish(self, index=-1):
        task = self.tasks[index]
        self.motions[index].stopped.set()
        await task
        await asyncio.sleep(0)


def test_construction_and_disabled_input_do_not_start_motion():
    async def scenario():
        backend = Backend()
        control = backend.controller
        assert control.held_keys == set()
        assert control.active_key is None
        assert control.released is None
        assert control.task is None
        control.key_event("W", True)
        await asyncio.sleep(0)
        assert backend.tasks == []
        assert backend.motions == []

    asyncio.run(scenario())


def test_direction_change_waits_for_stop_confirmation():
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        await asyncio.sleep(0)
        first = backend.motions[0]
        control.key_event("W", False)
        control.key_event("S", True)
        await asyncio.sleep(0)
        assert first.released.is_set()
        assert backend.release_count > 0
        assert [motion.key for motion in backend.motions] == ["W"]
        assert not backend.tasks[0].done()
        await backend.finish(0)
        assert [motion.key for motion in backend.motions] == ["W", "S"]
        assert control.active_key == "S"
        assert control.task is backend.tasks[1]
        assert control.released is backend.motions[1].released
        control.key_event("S", False)
        await backend.finish(1)

    asyncio.run(scenario())


def test_release_and_repress_keeps_a_fresh_press_while_stopping():
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        await asyncio.sleep(0)
        control.key_event("W", False)
        control.key_event("W", True)
        control.key_event("W", True, repeat=True)
        await asyncio.sleep(0)
        assert len(backend.motions) == 1
        await backend.finish(0)
        assert [motion.key for motion in backend.motions] == ["W", "W"]
        assert not backend.motions[1].released.is_set()
        control.key_event("W", False)
        await backend.finish(1)

    asyncio.run(scenario())


def test_released_pending_input_does_not_start_after_stop():
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        await asyncio.sleep(0)
        control.key_event("W", False)
        control.key_event("S", True)
        control.key_event("S", False)
        await backend.finish(0)
        assert [motion.key for motion in backend.motions] == ["W"]
        assert control.held_keys == set()
        assert control.active_key is None
        assert control.task is None or control.task.done()

    asyncio.run(scenario())


def test_only_the_remaining_fresh_key_starts_after_stop():
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        await asyncio.sleep(0)
        control.key_event("W", False)
        control.key_event("S", True)
        control.key_event("S", False)
        control.key_event("A", True)
        await backend.finish(0)
        assert [motion.key for motion in backend.motions] == ["W", "A"]
        control.key_event("A", False)
        await backend.finish(1)

    asyncio.run(scenario())


def test_two_keys_stop_motion_and_a_fresh_remaining_key_can_start():
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        await asyncio.sleep(0)
        control.key_event("S", True)
        assert control.held_keys == {"W", "S"}
        assert backend.motions[0].released.is_set()
        await backend.finish(0)
        assert len(backend.motions) == 1
        control.key_event("W", False)
        await asyncio.sleep(0)
        assert [motion.key for motion in backend.motions] == ["W", "S"]
        control.key_event("S", False)
        await backend.finish(1)

    asyncio.run(scenario())


def test_releasing_a_second_key_does_not_restart_a_consumed_key():
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        await asyncio.sleep(0)
        control.key_event("S", True)
        control.key_event("S", False)
        await backend.finish(0)
        assert control.held_keys == {"W"}
        assert len(backend.motions) == 1
        control.key_event("W", True, repeat=True)
        await asyncio.sleep(0)
        assert len(backend.motions) == 1
        control.key_event("W", False)
        control.key_event("W", True)
        await asyncio.sleep(0)
        assert len(backend.motions) == 2
        control.key_event("W", False)
        await backend.finish(1)

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["W", "Z"])
def test_completed_motion_requires_release_before_another_start(key):
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event(key, True)
        await asyncio.sleep(0)
        await backend.finish(0)
        control.key_event(key, True, repeat=True)
        control.key_event(key, True)
        await asyncio.sleep(0)
        assert len(backend.motions) == 1
        assert control.task is None or control.task.done()
        control.key_event(key, False)
        control.key_event(key, True)
        await asyncio.sleep(0)
        assert len(backend.motions) == 2
        control.key_event(key, False)
        await backend.finish(1)

    asyncio.run(scenario())


def test_disable_clears_held_and_pending_input():
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        await asyncio.sleep(0)
        control.key_event("W", False)
        control.key_event("S", True)
        control.set_enabled(False)
        assert control.held_keys == set()
        assert backend.motions[0].released.is_set()
        await backend.finish(0)
        control.set_enabled(True)
        await asyncio.sleep(0)
        assert len(backend.motions) == 1
        control.key_event("S", True)
        await asyncio.sleep(0)
        assert [motion.key for motion in backend.motions] == ["W", "S"]
        control.key_event("S", False)
        await backend.finish(1)

    asyncio.run(scenario())


def test_revocation_from_another_thread_stops_and_blocks_pending_input():
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        await asyncio.sleep(0)
        thread = threading.Thread(target=control.revoke)
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert backend.motions[0].released.is_set()
        control.key_event("W", False)
        control.key_event("S", True)
        await backend.finish(0)
        assert [motion.key for motion in backend.motions] == ["W"]
        assert control.task is None or control.task.done()

    asyncio.run(scenario())


def test_permission_is_checked_again_before_pending_input_starts():
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        await asyncio.sleep(0)
        control.key_event("W", False)
        control.key_event("S", True)
        backend.allowed = False
        await backend.finish(0)
        assert [motion.key for motion in backend.motions] == ["W"]
        assert control.task is None or control.task.done()

    asyncio.run(scenario())


def test_revocation_before_task_execution_prevents_backend_start():
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        thread = threading.Thread(target=control.revoke)
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert control.released.is_set()
        await backend.tasks[0]
        await asyncio.sleep(0)
        assert backend.motions == []
        control.key_event("S", True)
        await asyncio.sleep(0)
        assert len(backend.tasks) == 1

    asyncio.run(scenario())


def test_failed_motion_does_not_start_pending_input():
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        await asyncio.sleep(0)
        control.key_event("W", False)
        control.key_event("S", True)
        backend.motions[0].error = RuntimeError("Stop was not confirmed")
        with pytest.raises(RuntimeError, match="Stop was not confirmed"):
            await backend.finish(0)
        await asyncio.sleep(0)
        assert [motion.key for motion in backend.motions] == ["W"]
        assert control.task is None or control.task.done()

    asyncio.run(scenario())


def test_key_event_before_failure_cleanup_does_not_start_pending_input():
    class FailingBackend(Backend):
        async def run_hold(self, key, released):
            try:
                await super().run_hold(key, released)
            except RuntimeError:
                asyncio.get_running_loop().call_soon(self.controller.key_event, "D", False)
                raise

    async def scenario():
        backend = FailingBackend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        await asyncio.sleep(0)
        control.key_event("W", False)
        control.key_event("S", True)
        backend.motions[0].error = RuntimeError("Stop was not confirmed")
        with pytest.raises(RuntimeError, match="Stop was not confirmed"):
            await backend.finish(0)
        await asyncio.sleep(0)
        assert [motion.key for motion in backend.motions] == ["W"]
        assert len(backend.tasks) == 1

    asyncio.run(scenario())


def test_reenable_after_failure_allows_a_fresh_press():
    async def scenario():
        backend = Backend()
        control = backend.controller
        control.set_enabled(True)
        control.key_event("W", True)
        await asyncio.sleep(0)
        control.key_event("W", False)
        control.key_event("S", True)
        backend.motions[0].error = RuntimeError("Stop was not confirmed")
        with pytest.raises(RuntimeError, match="Stop was not confirmed"):
            await backend.finish(0)
        await asyncio.sleep(0)
        control.set_enabled(True)
        await asyncio.sleep(0)
        assert [motion.key for motion in backend.motions] == ["W"]
        control.key_event("S", True)
        await asyncio.sleep(0)
        assert [motion.key for motion in backend.motions] == ["W", "S"]
        assert not backend.motions[1].released.is_set()
        control.key_event("S", False)
        await backend.finish(1)

    asyncio.run(scenario())
