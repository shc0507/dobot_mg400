import argparse
import logging
import os
import signal

from waitress import create_server

from labkit import ports

from .app import create_app, normalize_base_path
from .controller import MotorController


def listen_urls(server):
    """Return the URL of each socket that waitress bound."""
    listening = getattr(server, "effective_listen", None) or [
        (server.effective_host, server.effective_port)
    ]
    return [
        f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
        for host, port in listening
    ]


def report(text):
    """Print a line. A hung-up terminal or a closed pipe must not interrupt the shutdown."""
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Stage motor panel. Options override the MG400_STAGE_* environment variables."
    )
    parser.add_argument(
        "--demo", action="store_true", help="Simulate motors without serial hardware"
    )
    parser.add_argument("--bind", default=ports.HOST, help="HTTP address (default: 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=ports.PORTS["stage"], help="HTTP port (default: %(default)s)"
    )
    parser.add_argument(
        "--serial",
        help="Offer only this Arduino device, for example /dev/serial/by-id/... "
        "(default: MG400_STAGE_SERIAL, or every serial port)",
    )
    parser.add_argument(
        "--config",
        help="Stage geometry TOML (default: MG400_STAGE_CONFIG or /var/lib/mg400/stage/stage.toml)",
    )
    parser.add_argument(
        "--base-path",
        type=normalize_base_path,
        default="/",
        help="URL prefix (default: /)",
    )
    args = parser.parse_args(argv)
    bind, port = args.bind, args.port
    if not 1 <= port <= 65535:
        parser.error("--port must be between 1 and 65535")
    # An empty environment value counts as unset.
    if args.serial is not None:
        serial = args.serial or None
    else:
        serial = os.environ.get("MG400_STAGE_SERIAL") or None
    logging.basicConfig(level=logging.INFO)
    # Do not open the port here: opening it resets the Arduino.
    config = (
        args.config if args.config is not None else os.environ.get("MG400_STAGE_CONFIG") or None
    )
    controller = MotorController(demo=args.demo, serial=serial, config=config)
    server = create_server(
        create_app(controller, base_path=args.base_path),
        host=bind,
        port=port,
        threads=6,
        clear_untrusted_proxy_headers=False,
        # Refuse a larger body before reading it, as MAX_CONTENT_LENGTH does in the app.
        # waitress refuses a body of this size or more, and keeps a smaller one in memory.
        max_request_body_size=4097,
    )
    for url in listen_urls(server):
        print(
            f"Stage Controller: {url}{args.base_path if args.base_path != '/' else ''}", flush=True
        )
    if args.demo:
        print("Demo mode: no serial hardware will be opened.", flush=True)
    elif serial:
        print(f"Serial port: {serial}", flush=True)
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in signals}

    def interrupted(signum, frame):
        # Repeated signals must not interrupt the motor stop.
        for number in signals:
            signal.signal(number, signal.SIG_IGN)
        raise KeyboardInterrupt

    controller.start()
    try:
        # Ctrl+C, systemctl stop and a closed SSH session must all stop the motors.
        # waitress returns from run() on KeyboardInterrupt.
        for signum in signals:
            if previous[signum] != signal.SIG_IGN:
                signal.signal(signum, interrupted)
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        for signum in signals:
            signal.signal(signum, signal.SIG_IGN)
        try:
            newest = max((event["id"] for event in controller.snapshot()["events"]), default=0)
            controller.close()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        # Report how the stop went, for example in the service journal.
        for event in reversed(controller.snapshot()["events"]):
            if event["id"] > newest:
                report(event["message"])


if __name__ == "__main__":
    main()
