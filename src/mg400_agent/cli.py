import argparse
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MG400 joint control in MuJoCo")
    parser.add_argument("--backend", choices=["mujoco"], default="mujoco")
    parser.add_argument("--viewer", action="store_true", help="Show the MuJoCo joint session")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("state", help="Show the fresh simulation scene")
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

    try:
        if args.command in {"ask", "chat"}:
            if args.api_mode not in {"responses", "chat_completions"}:
                raise ValueError("OPENAI_API_MODE must be responses or chat_completions")
            if not args.model:
                raise ValueError("Set OPENAI_MODEL or pass --model to select your API model")
            custom_chat = args.api_mode == "chat_completions" and os.environ.get("OPENAI_BASE_URL")
            if not os.environ.get("OPENAI_API_KEY") and not custom_chat:
                raise ValueError("Set OPENAI_API_KEY")
        from .mujoco_session import run_session

        return run_session(args, argv)
    except KeyboardInterrupt:
        print("Interrupted; session stopped.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
