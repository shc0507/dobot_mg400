"""Thermal camera service entry point."""

import argparse
import errno
import socket
import sys

from labkit import ports

from . import roi


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--demo", action="store_true", help="Explicit simulated camera for GUI development"
    )
    parser.add_argument("--no-connect", action="store_true", help="Wait for the connect command")
    parser.add_argument("--bind", default=ports.HOST)
    parser.add_argument("--port", type=int, default=ports.PORTS["thermal"])
    parser.add_argument("--base-path", default="/")
    args = parser.parse_args()
    from .app import normalize_base_path

    try:
        prefix = normalize_base_path(args.base_path)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not args.bind or args.bind != args.bind.strip():
        parser.error("--bind must be a nonempty address without surrounding whitespace")
    try:
        # The service reads this file at startup; a bad file must stop before bind.
        roi.load_regions(roi.DEFAULT_ROI_PATH)
    except ValueError as exc:
        parser.error(str(exc))
    host = args.bind
    url_host = f"[{host}]" if ":" in host else host
    url = f"http://{url_host}:{args.port}{prefix}"
    # Reserve the port before the app's lifespan can acquire the USB camera.
    # Keep this exact socket through startup: a check followed by a second bind
    # would leave a race with another instance launching at the same time.
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, args.port))
            listener.listen(128)
            listener.setblocking(False)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                print(
                    f"Port {args.port} is already in use. No new camera connection was opened.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Cannot listen at {url}: {exc}. No new camera connection was opened.",
                    file=sys.stderr,
                )
            print(
                f"If the P1 dashboard is already running, open {url}.\n"
                "To restart it, press Ctrl+C in its original terminal, "
                "then run this command again.\n"
                "If another application uses this port, choose a free port with --port PORT.\n"
                "Only one running process can connect to the P1 camera at a time.",
                file=sys.stderr,
            )
            return 1

        import uvicorn

        from .app import create_app

        print(f"Thermal Master P1 {'[SIMULATED DEMO]' if args.demo else '[REAL CAMERA]'}: {url}")
        app = create_app(demo=args.demo, auto_connect=not args.no_connect, base_path=prefix or "/")
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=args.port, workers=1))
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            # Uvicorn re-raises Ctrl+C after its graceful lifespan shutdown.
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
