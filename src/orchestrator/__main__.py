"""Run recipes from the terminal or serve the orchestrator HTTP contract."""

import argparse
import logging
import os
import signal
import sys

SERVER_OPTIONS = {
    # Each microscope stream holds one worker.
    "threads": 16,
    "clear_untrusted_proxy_headers": False,
    "max_request_body_size": 65537,
}
# The console control socket (orchestrator.web.control_socket). Waitress cannot upgrade a
# connection, so the socket has its own loopback port. The proxy routes
# /lab/api/console/socket here (ORCH_SOCKET_PORT in deploy/install.py).
SOCKET_PORT = 8774


def serve(
    argv,
    *,
    environ=None,
    service_factory=None,
    server_factory=None,
    socket_factory=None,
    exec_function=None,
):
    """Start the service after validation; terminate only after accepted work drains."""
    from .app import (
        CATALOG_DEFAULT,
        OrchestratorService,
        create_app,
        demo_environment,
        demo_isolated,
    )
    from .clients import SERVICE_URLS

    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="python -m orchestrator serve")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument("--base-path", default="/")
    parser.add_argument("--recipes-dir", default=CATALOG_DEFAULT)
    parser.add_argument("--runs-dir", help="Private run store; unavailable in demo mode")
    parser.add_argument("--demo", action="store_true", help="Use only owned isolated demo services")
    args = parser.parse_args(argv)
    if not args.bind or args.bind != args.bind.strip():
        parser.error("bind must be a nonempty address without surrounding whitespace")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
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
            settings=(
                dict(env)
                if args.demo
                # The fixed service URLs win over the host environment.
                else {**{key: value for key, value in env.items() if value}, **SERVICE_URLS}
            ),
            recipes_dir=args.recipes_dir,
            runs_dir=args.runs_dir,
            demo=args.demo,
        )
        app = create_app(service, base_path=args.base_path)
    except (ValueError, OSError) as error:
        if service is not None:
            service.close()
        parser.error(str(error))

    if server_factory is None:
        from waitress import create_server

        from .web.control_socket import ControlSocket

        server_factory = create_server
        # A test server brings its own socket factory, or none.
        socket_factory = socket_factory or ControlSocket
    logging.basicConfig(level=logging.INFO)
    server = None
    control = None
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
            **SERVER_OPTIONS,
        )
        if socket_factory is not None:
            try:
                control = socket_factory(
                    app, host=args.bind, port=SOCKET_PORT, base_path=args.base_path
                )
            except OSError as error:
                # Pages then send every control request over HTTP.
                logging.getLogger(__name__).warning("No console control socket: %s", error)
        service.start()
        if control is not None:
            control.start()
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
                if control is not None:
                    control.close()
            finally:
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
