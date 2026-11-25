"""Open the live Olares reader, with optional local simulation tests."""

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestrator.agent_connect.portal import create_portal_app  # noqa: E402


def start_child(module, url, *arguments):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    child = subprocess.Popen(
        [sys.executable, "-m", module, *arguments],
        cwd=ROOT,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if child.poll() is not None:
                raise RuntimeError(f"{module} did not start. Close another local preview first.")
            try:
                with urllib.request.urlopen(url, timeout=0.2):
                    return child
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        raise RuntimeError(f"{module} did not become ready.")
    except BaseException:
        child.terminate()
        child.wait(timeout=5)
        raise


def stop_child(child):
    if child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    children = []
    app = None
    try:
        children.append(
            start_child(
                "orchestrator.agent_connect.fallback",
                "http://127.0.0.1:8794/status",
                "--simulation",
            )
        )
        children.append(
            start_child(
                "orchestrator.agent_connect.mock_server",
                "http://127.0.0.1:8790/lab/api/agent/control/contract",
            )
        )
        app = create_portal_app(persist_local=True)
        portal = make_server("127.0.0.1", 8792, app, threaded=True)
        app.extensions["agent_portal_control"].start_restore()
    except BaseException:
        if app is not None:
            app.extensions["agent_portal_chat"].close()
            app.extensions["agent_portal_control"].close()
        for child in reversed(children):
            stop_child(child)
        raise
    print(
        "Agent Portal http://127.0.0.1:8792/ | Olares 8082 commands after Console grant | "
        "Optional local simulations"
    )
    if not args.no_browser:
        webbrowser.open("http://127.0.0.1:8792/")
    try:
        portal.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.extensions["agent_portal_chat"].close()
        app.extensions["agent_portal_control"].close()
        portal.server_close()
        for child in reversed(children):
            stop_child(child)


if __name__ == "__main__":
    main()
