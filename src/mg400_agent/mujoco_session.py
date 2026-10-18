"""Joint agent sessions with a responsive viewer and main-thread tool execution."""

import json
import os
import sys
from contextlib import nullcontext
from queue import Empty, Queue
from threading import Thread

from .agent import run_agent
from .mujoco_replay import launch_macos_viewer
from .mujoco_skills import MuJoCoJointExecutor


def wait_with_viewer(callback, viewer):
    """Wait for input or an API response, never running robot tools in the worker.

    Closing the window discards a pending response. A daemon worker may finish
    its bounded HTTP request, but cannot execute any tool after cancellation.
    Physics is paused during this wait; only the viewer is synchronized.
    """
    if not viewer.is_running():
        raise KeyboardInterrupt
    result = Queue(maxsize=1)

    def work():
        try:
            result.put((True, callback()))
        except BaseException as error:
            result.put((False, error))

    Thread(target=work, daemon=True).start()
    while viewer.is_running():
        viewer.sync()
        try:
            ok, value = result.get(timeout=1 / 30)
        except Empty:
            continue
        if not viewer.is_running():
            raise KeyboardInterrupt
        if ok:
            return value
        raise value
    raise KeyboardInterrupt


def _print_state(executor):
    print(
        json.dumps({"backend": "mujoco", "state": executor.state()}, ensure_ascii=False, indent=2),
        flush=True,
    )


def _run_commands(args, executor, viewer):
    wait = None if viewer is None else lambda callback: wait_with_viewer(callback, viewer)
    if args.command == "demo":
        for name, arguments in [
            ("move_joint", {"joint": "J1", "angle_deg": 20, "relative": False}),
            ("home", {}),
        ]:
            result = executor.execute(name, arguments)
            print(json.dumps({"tool": name, **result}, ensure_ascii=False), flush=True)
            if not result["ok"]:
                raise RuntimeError(result["error"])
    elif args.command == "ask":
        print(
            run_agent(
                args.task,
                executor,
                model=args.model,
                request_runner=wait,
                api_mode=args.api_mode,
            ),
            flush=True,
        )
    elif args.command == "chat":
        print(
            "MuJoCo 关节对话已启动。初始目标 J1–J4 = [0, 0, 0, 0] 度。\n"
            "例如：把第一轴转到 20 度；第三轴增加 10 度；回到初始姿态。\n"
            "等待输入/模型时暂停物理。输入 quit / exit / 退出，或关闭窗口结束。",
            flush=True,
        )
        history = []
        while not executor.stopped:
            try:
                task = input("你 > ") if wait is None else wait(lambda: input("你 > "))
            except EOFError:
                break
            if task.strip().lower() in {"quit", "exit", "退出"}:
                break
            if not task.strip():
                continue
            print(
                run_agent(
                    task,
                    executor,
                    model=args.model,
                    history=history,
                    request_runner=wait,
                    api_mode=args.api_mode,
                ),
                flush=True,
            )
            _print_state(executor)
    _print_state(executor)
    return 0


def run_session(args, argv: list[str]) -> int:
    if args.viewer and sys.platform == "darwin" and not os.environ.get("MJPYTHON_BIN"):
        launch_macos_viewer(argv, module="mg400_agent")
    executor = MuJoCoJointExecutor()
    try:
        context = nullcontext(None)
        if args.viewer:
            import mujoco.viewer

            context = mujoco.viewer.launch_passive(
                executor.model.model,
                executor.model.data,
                show_left_ui=False,
                show_right_ui=False,
            )
        with context as viewer:
            executor.viewer = viewer
            if viewer is not None:
                with viewer.lock():
                    viewer.cam.type = executor.model.mujoco.mjtCamera.mjCAMERA_FIXED
                    viewer.cam.fixedcamid = executor.model.model.camera("overview").id
                viewer.sync()
            try:
                return _run_commands(args, executor, viewer)
            except Exception:
                if viewer is not None and not viewer.is_running():
                    raise KeyboardInterrupt from None
                raise
    finally:
        executor.stop()
