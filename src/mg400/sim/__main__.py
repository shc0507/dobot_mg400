"""Run the MG400 simulator on numeric loopback addresses only."""

import argparse
import signal
import threading

from .runtime import Simulator


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.2", help="Numeric loopback address")
    parser.add_argument("--gripper-feedback", help="New feedback file for the simulated gripper")
    args = parser.parse_args(argv)
    stop = threading.Event()
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {}
    try:
        simulator = Simulator(args.host, gripper_feedback=args.gripper_feedback)
        with simulator:
            for signum in signals:
                previous[signum] = signal.signal(signum, lambda signum, frame: stop.set())
            print(f"MG400 simulator: {args.host} ports 29999, 30003, 30004", flush=True)
            stop.wait()
    except (OSError, ValueError) as error:
        parser.exit(2, f"MG400 simulator: {error}\n")
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
