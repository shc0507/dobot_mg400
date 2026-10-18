"""Run one explicitly authorized physical J4 +2 degree and return trial."""

import argparse
import json
import os
import sys

from mg400_agent.agent import run_agent
from mg400_agent.hardware_trial import JointTrial

TASK = "读取真机状态，然后让 J4 从本次记录的起始角度增加 2°，确认到位后回到该起点。"


class LoggedTrial:
    def __init__(self, trial):
        self.trial = trial

    def tool_schemas(self):
        return self.trial.tool_schemas()

    def execute(self, name, arguments):
        print(json.dumps({"tool": name, "arguments": arguments}, ensure_ascii=False), flush=True)
        result = self.trial.execute(name, arguments)
        print(json.dumps({"result": result}, ensure_ascii=False), flush=True)
        return result

    def stop(self):
        result = self.trial.stop()
        print(json.dumps({"stop": result}, ensure_ascii=False), flush=True)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--source-host")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL"))
    parser.add_argument(
        "--execute-j4-trial",
        action="store_true",
        required=True,
        help="Authorize the physical +2 degree and return sequence; keep the workspace clear.",
    )
    args = parser.parse_args(argv)
    if not args.model:
        parser.error("Set OPENAI_MODEL or pass --model")
    try:
        with JointTrial(args.host, args.source_host) as trial:
            print(json.dumps({"initial": trial.state()}, ensure_ascii=False), flush=True)
            answer = run_agent(
                TASK,
                LoggedTrial(trial),
                model=args.model,
                api_mode=os.environ.get("OPENAI_API_MODE", "chat_completions"),
            )
            final = trial.state()
            print(json.dumps({"agent_reply": answer, "verified_state": final}, ensure_ascii=False))
            if final["phase"] != "complete":
                raise RuntimeError("Agent ended before both physical actions were verified")
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(f"Trial ended: {error or 'interrupted'}. No motion will be retried.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
