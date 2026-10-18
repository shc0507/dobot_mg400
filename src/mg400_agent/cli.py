import argparse
import json
import logging
import os
import sys

from .backends import SimArm, SimGripper
from .config import Config
from .hardware import connect_mg400
from .skills import SkillExecutor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MG400 + electric gripper agent scaffold")
    parser.add_argument("--config", default="config/demo.toml")
    parser.add_argument("--backend", choices=["sim", "mujoco", "mg400"], default="sim")
    parser.add_argument("--viewer", action="store_true", help="Show the MuJoCo joint session")
    parser.add_argument("--host", help="Reserved for the future hardware adapter")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("state", help="Show the fresh simulation scene")
    demo = commands.add_parser("demo", help="Run pick/place or MuJoCo joint/home without an LLM")
    demo.add_argument("--object", default="red_block", dest="object_id")
    demo.add_argument("--destination", default="box_a")
    ask = commands.add_parser("ask", help="Use an OpenAI model to call named skills")
    ask.add_argument("task")
    ask.add_argument("--model", default=os.environ.get("OPENAI_MODEL"))
    chat = commands.add_parser("chat", help="Keep a MuJoCo natural-language joint session open")
    chat.add_argument("--model", default=os.environ.get("OPENAI_MODEL"))
    for command in (ask, chat):
        command.add_argument(
            "--api-mode",
            choices=["responses", "chat_completions"],
            default=os.environ.get("OPENAI_API_MODE", "responses"),
            help="API protocol (or OPENAI_API_MODE); use chat_completions for the lab Qwen server",
        )
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    executor = None
    try:
        if args.viewer and args.backend != "mujoco":
            raise ValueError("--viewer requires --backend mujoco")
        if args.command == "chat" and args.backend != "mujoco":
            raise ValueError("chat requires --backend mujoco")
        if args.command in {"ask", "chat"}:
            if args.api_mode not in {"responses", "chat_completions"}:
                raise ValueError("OPENAI_API_MODE must be responses or chat_completions")
            if not args.model:
                raise ValueError("Set OPENAI_MODEL or pass --model to select your API model")
            custom_chat = args.api_mode == "chat_completions" and os.environ.get("OPENAI_BASE_URL")
            if not os.environ.get("OPENAI_API_KEY") and not custom_chat:
                raise ValueError("Set OPENAI_API_KEY; the demo command does not need a key")
        if args.backend == "mujoco":
            if args.command == "demo" and (
                args.object_id != "red_block" or args.destination != "box_a"
            ):
                raise ValueError("MuJoCo demo supports joint/home motion, not object pick/place")
            from .mujoco_session import run_session

            return run_session(args, argv)
        config = Config.load(args.config)
        if args.backend == "mg400":
            if not args.host:
                raise ValueError("--backend mg400 requires --host (adapter not implemented yet)")
            arm, gripper = connect_mg400(args.host)
        else:
            arm, gripper = SimArm(config.home), SimGripper()
        executor = SkillExecutor(config, arm, gripper)
        if args.command == "demo":
            # Validate the complete requested task before the first action.
            if args.object_id not in config.objects or args.destination not in config.locations:
                raise ValueError("Unknown object or destination; inspect the state command first")
            for name, arguments in [
                ("pick", {"object_id": args.object_id}),
                ("place", {"location_id": args.destination}),
            ]:
                result = executor.execute(name, arguments)
                print(json.dumps({"tool": name, **result}, ensure_ascii=False))
                if not result["ok"]:
                    return 1
        elif args.command == "ask":
            from .agent import run_agent

            print(run_agent(args.task, executor, model=args.model, api_mode=args.api_mode))
        print(
            json.dumps(
                {"backend": args.backend, "state": executor.state()}, ensure_ascii=False, indent=2
            )
        )
        return 0
    except KeyboardInterrupt:
        if executor is not None:
            executor.stop()
        print("Interrupted; session stopped.", file=sys.stderr)
        return 130
    except Exception as error:
        if executor is not None:
            executor.stop()
        print(f"Error: {error}", file=sys.stderr)
        return 1
