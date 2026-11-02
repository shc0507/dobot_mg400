"""One request through the gateway; start no model or instrument service."""

import argparse
import os

from .config import Settings
from .smolagents_example import create_agent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="A read request or a recipe proposal for human review.")
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env(os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        with create_agent(settings) as agent:
            print(agent.run(args.request))
    except Exception:
        # Provider and HTTP exceptions can include private headers and input text.
        raise SystemExit(
            "Client unavailable. A proposal might exist. Do not retry a proposal; "
            "have a person inspect proposals and current runs on the dashboard."
        ) from None


if __name__ == "__main__":
    main()
