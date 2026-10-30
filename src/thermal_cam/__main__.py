"""Command-line server, USB diagnosis, and real-camera acquisition smoke test."""

import argparse
import errno
import json
from pathlib import Path
import socket
import sys
import time

from .camera import P1Camera, diagnose
from .service import ThermalFrame


def probe(frames: int, output: Path | None):
    camera = P1Camera()
    started = time.monotonic()
    try:
        device = camera.open()
        current = None
        for sequence in range(1, frames + 1):
            ir, raw = camera.read()
            current = ThermalFrame.from_arrays(ir, raw, sequence)
        result = {"ok": True, "source": "camera", "device": device,
                  "frames": frames, "elapsed_s": round(time.monotonic() - started, 2),
                  "last_frame": current.metadata(), "usb_stats": camera.stats()}
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(current.npz())
            result["snapshot"] = str(output.resolve())
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "source": "camera", "error": f"{type(exc).__name__}: {exc}",
                          "diagnostics": diagnose()}, indent=2))
        return 1
    finally:
        camera.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--diagnose", action="store_true", help="Report USB backend/device availability")
    modes.add_argument("--probe", action="store_true", help="Capture real P1 frames and report temperatures")
    modes.add_argument("--demo", action="store_true", help="Explicit simulated camera for GUI development")
    parser.add_argument("--frames", type=int, default=30, help="Number of real frames to capture with --probe")
    parser.add_argument("--output", type=Path, help="Save the last probe frame as a lossless .npz file")
    parser.add_argument("--no-connect", action="store_true", help="Wait for Connect in the GUI")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.frames <= 10000:
        parser.error("--frames must be between 1 and 10000")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.diagnose:
        print(json.dumps(diagnose(), indent=2))
        return 0
    if args.probe:
        return probe(args.frames, args.output)
    host = "127.0.0.1"
    url = f"http://{host}:{args.port}"
    # Reserve the port before the app's lifespan can acquire the USB camera.
    # Keep this exact socket through startup: a check followed by a second bind
    # would leave a race with another instance launching at the same time.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            if sys.platform == "win32":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, args.port))
            listener.listen(128)
            listener.setblocking(False)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
                print(f"Port {args.port} is already in use. No new camera connection was opened.", file=sys.stderr)
            else:
                print(f"Cannot listen at {url}: {exc}. No new camera connection was opened.", file=sys.stderr)
            print(
                f"If the P1 dashboard is already running, open {url}.\n"
                "To restart it, press Ctrl+C in its original terminal, then run this command again.\n"
                "If another application uses this port, choose a free port with --port PORT.\n"
                "Only one running process can connect to the P1 camera at a time.",
                file=sys.stderr,
            )
            return 1

        import uvicorn
        from .app import create_app
        print(f"Thermal Master P1 {'[SIMULATED DEMO]' if args.demo else '[REAL CAMERA]'}: {url}")
        app = create_app(demo=args.demo, auto_connect=not args.no_connect)
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=args.port, workers=1))
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            # Uvicorn re-raises Ctrl+C after its graceful lifespan shutdown.
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
