"""Separate mock Console process; its stage simulator survives this process."""

from werkzeug.serving import make_server

from orchestrator.web.control_socket import ControlSocket

from .mock import create_mock_app
from .mock_devices import MockDevices


def main():
    devices = MockDevices()
    app = create_mock_app(streaming=True, command_service=devices.service)
    server = make_server("127.0.0.1", 8790, app, threaded=True)
    stream = ControlSocket(app, host="127.0.0.1", port=8793, base_path="/lab")
    app.config["AGENT_SIMULATION"].hub.start()
    stream.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.extensions["agent_control"].close()
        stream.close()
        app.config["AGENT_SIMULATION"].close()
        server.server_close()
        devices.close()


if __name__ == "__main__":
    main()
