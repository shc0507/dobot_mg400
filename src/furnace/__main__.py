"""Run one furnace service. Signal shutdown stops heat and gas."""

import argparse
import logging
import os
import signal

from labkit.auth import valid_token_for_bind

from .app import create_app, make_service
from .config import FurnaceConfig


def main(argv=None, *, environ=None, service_factory=make_service, server_factory=None):
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Furnace service and operator page")
    parser.add_argument(
        "--demo", action="store_true", help="Use isolated simulated furnace and gas devices"
    )
    parser.add_argument("--bind", default=env.get("MG400_FURNACE_BIND") or "127.0.0.1")
    parser.add_argument("--port", type=int, default=env.get("MG400_FURNACE_PORT") or "8769")
    parser.add_argument("--base-path", default=env.get("MG400_FURNACE_BASE_PATH") or "/")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    token = env.get("MG400_FURNACE_TOKEN") or None
    if not valid_token_for_bind(token, args.bind):
        parser.error(
            "Set MG400_FURNACE_TOKEN to at least 24 printable ASCII characters, or bind to loopback"
        )
    service = None
    try:
        if args.demo:
            service = service_factory(demo=True)
        else:
            config = FurnaceConfig(
                port=env.get("MG400_FURNACE_SERIAL") or None,
                flow_port=env.get("MG400_MFC_SERIAL") or None,
                slave_address=env.get("MG400_FURNACE_SLAVE") or 1,
                max_c=env.get("MG400_FURNACE_MAX_C") or 1000,
                max_ramp=env.get("MG400_FURNACE_MAX_RAMP") or 100,
            )
            if not config.port:
                parser.error("Set MG400_FURNACE_SERIAL to the configured serial device")
            service = service_factory(
                config=config,
                gas_map=env.get("MG400_FURNACE_GAS_MAP") or {},
                idle_gas=env.get("MG400_FURNACE_IDLE_GAS") or {},
                orphan_grace_min=env.get("MG400_FURNACE_ORPHAN_GRACE_MIN") or 15,
            )
        app = create_app(service, token=token, base_path=args.base_path)
    except (ValueError, OSError) as error:
        # No device started yet. In demo mode only an owned temporary directory
        # exists; cleanup still goes through the public service close method.
        if service is not None:
            service.close()
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
            max_request_body_size=65537,
        )
        service.start()
        server.run()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        parser.exit(2, f"Furnace startup or serving failed: {error}\n")
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
