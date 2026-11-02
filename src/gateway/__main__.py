"""Start the authenticated MCP gateway for reads and human-reviewed proposals."""

import argparse
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path

from labkit.auth import loopback_name
from orchestrator.store import DEFAULT_RUNS, RunStore

from .app import create_app, validate_access
from .backend import Backend, configured_endpoints


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default=os.environ.get("MG400_GATEWAY_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.environ.get("MG400_GATEWAY_PORT", "8773"))
    parser.add_argument("--demo", action="store_true")
    parser.add_argument(
        "--runs-dir", default=os.environ.get("MG400_GATEWAY_RUNS_DIR", str(DEFAULT_RUNS))
    )
    parser.add_argument(
        "--recipes-dir", default=os.environ.get("MG400_GATEWAY_RECIPES_DIR", "/opt/mg400/recipes")
    )
    args = parser.parse_args(argv)
    try:
        if not loopback_name(args.bind) or not 1 <= args.port <= 65535:
            raise ValueError("Gateway bind must be loopback and port must be from 1 to 65535.")
        token = os.environ.get("MG400_GATEWAY_TOKEN", "")
        caller = os.environ.get("MG400_GATEWAY_CALLER", "gateway-agent")
        validate_access(token, caller)
        with ExitStack() as stack:
            if args.demo:
                # Demo reads only a private empty store and the bundled recipe catalog.
                directory = Path(
                    stack.enter_context(tempfile.TemporaryDirectory(prefix="mg400-gateway-demo-"))
                )
                runs = directory / "runs"
                recipes = Path(__file__).resolve().parents[2] / "recipes"
                endpoints = {}
                proposal = None
            else:
                runs, recipes = args.runs_dir, args.recipes_dir
                endpoints = configured_endpoints(os.environ)
                proposal_url = os.environ.get("MG400_GATEWAY_ORCH_URL", "")
                proposal = (proposal_url, token, caller) if proposal_url else None
            store = stack.enter_context(RunStore(runs))
            backend = Backend(
                store, recipes, endpoints=endpoints, proposal=proposal, demo=args.demo
            )
            app = create_app(
                backend,
                token=token,
                caller=caller,
                bind=args.bind,
            )
            import uvicorn

            uvicorn.run(app, host=args.bind, port=args.port, proxy_headers=False, access_log=False)
    except (ValueError, OSError) as error:
        parser.exit(2, f"Gateway configuration failed: {error}\n")


if __name__ == "__main__":
    main()
