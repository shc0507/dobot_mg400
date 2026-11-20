"""Open the live Olares reader, with optional local simulation tests."""

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestrator.agent_connect.mock import create_mock_app  # noqa: E402
from orchestrator.agent_connect.portal import create_portal_app  # noqa: E402
from orchestrator.web.control_socket import ControlSocket  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    # The portal reads the fixed Olares test site. Simulations keep their own source.
    app = create_mock_app(streaming=True)
    mock = make_server("127.0.0.1", 8790, app, threaded=True)
    stream = ControlSocket(app, host="127.0.0.1", port=8793, base_path="/lab")
    try:
        portal = make_server("127.0.0.1", 8792, create_portal_app(), threaded=True)
    except BaseException:
        mock.server_close()
        raise
    worker = threading.Thread(target=mock.serve_forever, daemon=True)
    worker.start()
    app.config["AGENT_SIMULATION"].hub.start()
    stream.start()
    print(
        "Agent Portal http://127.0.0.1:8792/ | Olares 8082 read-only | Optional local simulations"
    )
    if not args.no_browser:
        webbrowser.open("http://127.0.0.1:8792/")
    try:
        portal.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stream.close()
        app.config["AGENT_SIMULATION"].close()
        portal.server_close()
        mock.shutdown()
        mock.server_close()
        worker.join(3)


if __name__ == "__main__":
    main()
