import argparse
import logging
import os
import re
import signal

from waitress import create_server

from labkit.auth import valid_token, valid_token_for_bind

from .app import create_app
from .controller import GRIPPER_CONVERTER, MotorController, is_gripper_converter


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
    parser.add_argument("--bind", help="HTTP address (default: MG400_STAGE_BIND or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="HTTP port (default: MG400_STAGE_PORT or 8767)")
    parser.add_argument(
        "--serial",
        help="Offer only this Arduino device, for example /dev/serial/by-id/... "
        "(default: MG400_STAGE_SERIAL, or every serial port except the gripper converter)",
    )
    args = parser.parse_args(argv)
    # An empty environment value counts as unset.
    bind = args.bind if args.bind is not None else os.environ.get("MG400_STAGE_BIND") or "127.0.0.1"
    if args.port is not None:
        port = args.port
        if not 1 <= port <= 65535:
            parser.error("--port must be between 1 and 65535")
    else:
        value = os.environ.get("MG400_STAGE_PORT") or "8767"
        port = int(value) if re.fullmatch("[0-9]{1,5}", value) else 0
        if not 1 <= port <= 65535:
            parser.error("MG400_STAGE_PORT must be a whole number between 1 and 65535")
    if args.serial is not None:
        serial, source = args.serial or None, "--serial"
    else:
        serial, source = os.environ.get("MG400_STAGE_SERIAL") or None, "MG400_STAGE_SERIAL"
    if serial and not args.demo and is_gripper_converter(serial):
        parser.error(
            f"{source} points to the gripper USB converter ({GRIPPER_CONVERTER[2]}). "
            "Set it to the Arduino serial device"
        )
    token = os.environ.get("MG400_STAGE_TOKEN")
    if token and not valid_token(token):
        parser.error(
            "MG400_STAGE_TOKEN must contain only printable ASCII characters "
            "and must not begin or end with a space"
        )
    # With an access token, the app accepts any host name, also on a loopback bind.
    if not valid_token_for_bind(token, bind):
        parser.error(
            "Set MG400_STAGE_TOKEN to at least 24 characters, "
            "or leave it unset and bind to localhost"
        )
    logging.basicConfig(level=logging.INFO)
    # Do not open the port here: opening it resets the Arduino.
    controller = MotorController(demo=args.demo, serial=serial)
    server = create_server(
        create_app(controller, token or None),
        host=bind,
        port=port,
        threads=6,
        # Pass proxy headers through unused: without a token, the app refuses proxied requests.
        clear_untrusted_proxy_headers=False,
        # Refuse a larger body before reading it, as MAX_CONTENT_LENGTH does in the app.
        # waitress refuses a body of this size or more, and keeps a smaller one in memory.
        max_request_body_size=4097,
    )
    for url in listen_urls(server):
        print(f"Stage Controller: {url}", flush=True)
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
