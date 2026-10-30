"""Run validated recipes and report retained safety outcomes."""

import argparse
import json
import os
import signal
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from labkit.errors import ServiceError

from .clients import ServiceAdapter, from_settings
from .recipe import load_recipe, positive
from .rehearsal import Rehearsal
from .runner import Runner
from .skills import default_registry
from .skills.robot import TRANSFER_SETTINGS
from .stations import DEFAULT_STATIONS, DEFAULT_TOOL, StationStore, Workspace, load_tool, teach
from .store import DEFAULT_RUNS, RunStore

TERMINAL = {"completed", "aborted"}


@contextmanager
def _demo_environment():
    """Remove ambient device, credential, proxy and Python settings for demos."""
    saved = dict(os.environ)
    allowed = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "TZ"} | TRANSFER_SETTINGS
    try:
        os.environ.clear()
        os.environ.update({key: value for key, value in saved.items() if key in allowed})
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@contextmanager
def _interrupts():
    """Treat process termination as an abort request, including at an input prompt."""
    previous = {}

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _report(runner, out):
    # Do not reconstruct this from the database: persistence itself can fail.
    print(json.dumps(runner.snapshot(), ensure_ascii=False, allow_nan=False), file=out, flush=True)


def _drain(runner):
    """Keep resources alive after abort until old work and stop operations finish."""
    while True:
        try:
            runner.abort()
            if runner.status in TERMINAL:
                return
            runner.wait(0.25)
            if runner.status not in TERMINAL:
                time.sleep(0.05)
        except (TimeoutError, KeyboardInterrupt, EOFError):
            continue


def _drive(runner, out, read):
    try:
        runner.start()
        _report(runner, out)
        while True:
            try:
                status = runner.wait(0.25)
            except TimeoutError:
                continue
            if status in TERMINAL:
                _report(runner, out)
                return int(status != "completed")
            _report(runner, out)
            answer = read(
                "Run held. Type 'manually-completed' to record manual completion, or 'abort': "
            ).strip()
            if answer == "abort":
                _drain(runner)
                _report(runner, out)
                return 1
            if answer != "manually-completed":
                print("No action taken. The failed step will not be retried.", file=out)
                continue
            note = read("Manual completion note (required): ").strip()
            if not note:
                print("A nonempty manual completion note is required.", file=out)
                continue
            try:
                runner.resume(manually_completed=True, note=note)
            except (RuntimeError, ValueError) as error:
                print(f"Run remains held: {error}", file=out, flush=True)
    except BaseException as error:
        # Even broken output and repeated terminal interrupts must not close the
        # store or demo contexts beneath an in-flight worker.
        _drain(runner)
        try:
            _report(runner, out)
            print(f"Run aborted: {type(error).__name__}: {error}", file=out, flush=True)
        except (OSError, KeyboardInterrupt, EOFError):
            pass
        if not isinstance(error, (Exception, KeyboardInterrupt, EOFError)):
            raise
        return 130 if isinstance(error, KeyboardInterrupt) else 1


def _overrides(values):
    result = {}
    for value in values:
        name, separator, raw = value.partition("=")
        if not separator or not name or name in result:
            raise ValueError("Each parameter needs a unique NAME=JSON value.")
        result[name] = json.loads(raw)
    return result


def _station(args, settings, out):
    workspace = Workspace.from_settings(settings)
    path = args.stations_file or settings.get("MG400_ORCH_STATIONS_FILE") or DEFAULT_STATIONS
    store = StationStore(workspace, path)
    if args.station_action == "save":
        offset = load_tool(args.tool_file or settings.get("MG400_ORCH_TOOL_FILE") or DEFAULT_TOOL)
        url = settings.get("MG400_ORCH_ROBOT_URL")
        if not url:
            raise ValueError("Set MG400_ORCH_ROBOT_URL before teaching a station.")
        timeout = positive(float(settings.get("MG400_ORCH_TIMEOUT_S", "2")), "request timeout")
        # This adapter has no robot WebSocket and sends only the two GETs in teach().
        robot = ServiceAdapter(
            url,
            settings.get("MG400_ORCH_ROBOT_TOKEN", ""),
            "teach-" + uuid.uuid4().hex,
            timeout=timeout,
        )
        result = teach(store, args.name, robot, offset, approach_mm=args.approach, zone=args.zone)
    elif args.station_action == "rm":
        store.remove(args.name)
        result = {"removed": args.name}
    else:
        result = store.load()
        if args.station_action == "show":
            if args.name not in result:
                raise ValueError(f"Unknown station: {args.name}")
            result = result[args.name]
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), file=out)
    return 0


def main(argv=None, *, out=None, read=None):
    out, read = sys.stdout if out is None else out, input if read is None else read
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "rehearse"):
        sub = commands.add_parser(command, allow_abbrev=False)
        sub.add_argument("recipe", type=Path)
        sub.add_argument("--param", action="append", default=[], metavar="NAME=JSON")
        sub.add_argument("--runs-dir", type=Path)
        if command == "rehearse":
            sub.add_argument("--time-scale", type=float, default=0.1)
    history = commands.add_parser("runs", allow_abbrev=False)
    history.add_argument("run_id", nargs="?")
    history.add_argument("--runs-dir", type=Path)
    station = commands.add_parser("station", allow_abbrev=False)
    station_actions = station.add_subparsers(dest="station_action", required=True)
    for action in ("save", "list", "show", "rm"):
        sub = station_actions.add_parser(action, allow_abbrev=False)
        if action != "list":
            sub.add_argument("name")
        sub.add_argument("--stations-file", type=Path)
        if action == "save":
            sub.add_argument("--approach", type=float, required=True)
            sub.add_argument("--zone", default="")
            sub.add_argument("--tool-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "station":
            return _station(args, os.environ, out)
        directory = args.runs_dir or Path(os.environ.get("MG400_ORCH_RUNS_DIR", DEFAULT_RUNS))
        if args.command == "runs":
            with RunStore(directory, readonly=True) as store:
                result = store.get(args.run_id) if args.run_id else store.runs()
                print(json.dumps(result, ensure_ascii=False, allow_nan=False), file=out)
            return 0
        registry = default_registry()
        recipe = load_recipe(args.recipe, registry, overrides=_overrides(args.param))
        scale = positive(args.time_scale, "time scale") if args.command == "rehearse" else 1
        identity = "orch-" + uuid.uuid4().hex
        # Recipe and option validation precede even store creation or demo startup.
        with _interrupts():
            if args.command == "rehearse":
                with (
                    _demo_environment(),
                    tempfile.TemporaryDirectory(prefix="mg400-rehearsal-") as temporary,
                ):
                    with (
                        RunStore(directory) as store,
                        Rehearsal(temporary, recipe.services, identity=identity) as demo,
                    ):
                        return _drive(
                            Runner(recipe, registry, demo.clients, store, time_scale=scale),
                            out,
                            read,
                        )
            services = from_settings(os.environ, identity=identity)
            with RunStore(directory) as store:
                return _drive(Runner(recipe, registry, services, store), out, read)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, ServiceError) as error:
        print(f"Cannot {args.command}: {error}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        return 130
