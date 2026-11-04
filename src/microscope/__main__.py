"""Run the microscope contract service with explicit startup and safe shutdown."""

import argparse
import logging
import os
import signal

from .app import create_app


def main(
    argv=None, *, environ=None, service_factory=None, config_factory=None, server_factory=None
):
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Microscope contract service")
    parser.add_argument("--demo", action="store_true", help="Use simulated motors and cameras")
    parser.add_argument("--bind", default=env.get("MG400_MICROSCOPE_BIND") or "127.0.0.1")
    parser.add_argument("--port", type=int, default=env.get("MG400_MICROSCOPE_PORT") or "8770")
    parser.add_argument("--base-path", default=env.get("MG400_MICROSCOPE_BASE_PATH") or "/")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if service_factory is None or config_factory is None:
        from .service import MicroscopeConfig, MicroscopeService

        service_factory = service_factory or MicroscopeService
        config_factory = config_factory or MicroscopeConfig
    service = None
    try:
        config = config_factory.from_env(env)
        service = service_factory(config, demo=args.demo)
        app = create_app(service, base_path=args.base_path)
    except (ValueError, OSError) as error:
        if service is not None:
            try:
                service.close()
            except Exception as close_error:
                parser.error(f"{error}; service cleanup failed: {close_error}")
        parser.error(str(error))
    if server_factory is None:
        from waitress import create_server

        server_factory = create_server
    logging.basicConfig(level=logging.INFO)
    server = None
    failure = None
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
        failure = f"Microscope startup or serving failed: {error}"
    finally:
        for number in signals:
            signal.signal(number, signal.SIG_IGN)
        try:
            try:
                service.close()
            except Exception as error:
                failure = f"{failure + '; ' if failure else ''}Microscope cleanup failed: {error}"
            finally:
                if server is not None:
                    try:
                        server.close()
                    except Exception as error:
                        failure = (
                            f"{failure + '; ' if failure else ''}Server cleanup failed: {error}"
                        )
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
    if failure:
        parser.exit(2, failure + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
