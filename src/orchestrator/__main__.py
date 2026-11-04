"""Run recipes from the terminal or serve the orchestrator HTTP contract."""

import argparse
import logging
import os
import signal
import sys

from labkit.auth import valid_token_for_bind


def serve(argv, *, environ=None, service_factory=None, server_factory=None, exec_function=None):
    """Start the service after validation; terminate only after accepted work drains."""
    from .app import (
        CATALOG_DEFAULT,
        OrchestratorService,
        create_app,
        demo_environment,
        demo_isolated,
    )

    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="python -m orchestrator serve")
    parser.add_argument("--bind", default=env.get("MG400_ORCH_BIND") or "127.0.0.1")
    parser.add_argument("--port", type=int, default=env.get("MG400_ORCH_PORT") or "8772")
    parser.add_argument("--base-path", default=env.get("MG400_ORCH_BASE_PATH") or "/")
    parser.add_argument(
        "--recipes-dir", default=env.get("MG400_ORCH_RECIPES_DIR") or CATALOG_DEFAULT
    )
    parser.add_argument("--runs-dir", help="Private run store; unavailable in demo mode")
    parser.add_argument("--demo", action="store_true", help="Use only owned isolated demo services")
    args = parser.parse_args(argv)
    if not args.bind or args.bind != args.bind.strip():
        parser.error("bind must be a nonempty address without surrounding whitespace")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    token = env.get("MG400_ORCH_TOKEN") or None
    if not valid_token_for_bind(token, args.bind):
        parser.error(
            "Set MG400_ORCH_TOKEN to at least 24 printable ASCII characters, or bind to loopback"
        )
    if args.demo and args.runs_dir is not None:
        parser.error("--demo owns a temporary run store; do not supply --runs-dir")
    if args.demo and not demo_isolated(env):
        # No service, store, server or thread exists yet. A fresh interpreter also
        # removes effects of ambient Python settings. The service independently
        # checks the complete environment; a marker alone never grants access.
        execute = os.execve if exec_function is None else exec_function
        try:
            execute(
                sys.executable,
                [sys.executable, "-I", "-m", "orchestrator", "serve", *argv],
                demo_environment(env),
            )
        except OSError as error:
            parser.error(f"Cannot start the isolated demo process: {error}")
        raise RuntimeError("Demo process replacement unexpectedly returned.")

    service = None
    try:
        factory = OrchestratorService if service_factory is None else service_factory
        service = factory(
            settings=dict(env),
            recipes_dir=args.recipes_dir,
            runs_dir=args.runs_dir,
            demo=args.demo,
        )
        app = create_app(service, token=token, base_path=args.base_path)
    except (ValueError, OSError) as error:
        if service is not None:
            service.close()
        parser.error(str(error))

    if server_factory is None:
        from waitress import create_server

        server_factory = create_server
    logging.basicConfig(level=logging.INFO)
    server = None
    serving = False
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {number: signal.getsignal(number) for number in signals}

    def interrupted(signum, frame):
        for number in signals:
            signal.signal(number, signal.SIG_IGN)
        raise KeyboardInterrupt

    try:
        for number in signals:
            if previous[number] != signal.SIG_IGN:
                signal.signal(number, interrupted)
        server = server_factory(
            app,
            host=args.bind,
            port=args.port,
            # Camera relays hold one worker each (at most 6); keep 10 for the API.
            threads=16,
            clear_untrusted_proxy_headers=False,
            max_request_body_size=65537,
        )
        service.start()
        serving = True
        server.run()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        parser.exit(1 if serving else 2, f"Orchestrator startup or serving failed: {error}\n")
    finally:
        for number in signals:
            signal.signal(number, signal.SIG_IGN)
        try:
            try:
                service.close()
            finally:
                if server is not None:
                    server.close()
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
    return 0


def main(
    argv=None,
    *,
    out=None,
    read=None,
    environ=None,
    service_factory=None,
    server_factory=None,
    exec_function=None,
):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["serve"]:
        return serve(
            args[1:],
            environ=environ,
            service_factory=service_factory,
            server_factory=server_factory,
            exec_function=exec_function,
        )
    from .cli import main as terminal_main

    return terminal_main(args, out=out, read=read)


if __name__ == "__main__":
    raise SystemExit(main())
