"""Run the vision contract service; model loading starts only through main()."""

import argparse
import logging
import os
import signal

from labkit.auth import valid_token_for_bind

from .frames import DemoSource, Go2RTCSource
from .service import DEFAULT_WEIGHTS, FIXTURES, VisionService, create_app


def main(argv=None, *, environ=None, service_factory=VisionService, server_factory=None):
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(
        description="Vision service. Options override MG400_VISION_* settings."
    )
    parser.add_argument(
        "--demo", action="store_true", help="Use offline fixtures without torch or cameras"
    )
    parser.add_argument("--bind", default=env.get("MG400_VISION_BIND") or "127.0.0.1")
    parser.add_argument("--port", default=env.get("MG400_VISION_PORT") or "8771", type=int)
    parser.add_argument("--base-path", default=env.get("MG400_VISION_BASE_PATH") or "/")
    parser.add_argument("--weights", default=env.get("MG400_VISION_WEIGHTS") or DEFAULT_WEIGHTS)
    parser.add_argument("--device", default=env.get("MG400_VISION_DEVICE") or None)
    parser.add_argument(
        "--gpu-fraction", default=env.get("MG400_VISION_GPU_FRACTION") or "0.15", type=float
    )
    parser.add_argument(
        "--frame-timeout", default=env.get("MG400_VISION_FRAME_TIMEOUT") or "2", type=float
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    token = env.get("MG400_VISION_TOKEN") or None
    if not valid_token_for_bind(token, args.bind):
        parser.error(
            "Set MG400_VISION_TOKEN to at least 24 printable ASCII characters, "
            "or leave it unset and bind to loopback"
        )
    try:
        source = DemoSource(FIXTURES) if args.demo else Go2RTCSource(timeout=args.frame_timeout)
        service = service_factory(
            demo=args.demo,
            frames=source,
            weights=args.weights,
            device=args.device,
            gpu_fraction=args.gpu_fraction,
        )
        app = create_app(service, token=token, base_path=args.base_path)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if server_factory is None:
        from waitress import create_server

        server_factory = create_server
    logging.basicConfig(level=logging.INFO)
    server = None
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
            threads=6,
            clear_untrusted_proxy_headers=False,
            max_request_body_size=4097,
        )
        service.start()
        server.run()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        parser.exit(2, f"Vision startup or serving failed: {error}\n")
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


if __name__ == "__main__":
    raise SystemExit(main())
