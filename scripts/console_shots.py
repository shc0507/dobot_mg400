"""Capture Lab Console screenshots with Playwright in one call.

Examples (run from the repository root):

    # All 4 pages, EN and 中文, 1440x900 and 1280x800, from the live console.
    uv run python scripts/console_shots.py shoot --out /tmp/shots

    # Motion only, from a local demo server that the script starts and stops.
    uv run python scripts/console_shots.py shoot --demo --pages motion --out /tmp/shots

    # A state: a JS file that runs in the page after load (DOM changes only).
    uv run python scripts/console_shots.py shoot --pages motion --state held=held.js \
        --out /tmp/shots

    # Before/after composites (before on top) from 2 URLs or 2 directories of shots.
    uv run python scripts/console_shots.py compare http://100.64.0.1:8080/lab/ --demo --out /tmp/cmp
    uv run python scripts/console_shots.py compare /tmp/before /tmp/after --out /tmp/cmp

File names are ``<page>-<lang>-<width>x<height>[-<state>].png``. Composites are
``cmp-<same name>``. The browser sends only GET requests: the script aborts all
other HTTP methods and removes WebSocket, so a shot never commands a device.
The script uses the Node Playwright package (``npm root -g``) and its Chromium.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://100.64.0.1:8080/lab/"
PAGES = {"overview": "overview", "motion": "motion", "cameras": "cameras", "process": "process"}
LANGS = ("en", "zh")
VIEWPORTS = ("1440x900", "1280x800")
REPO = Path(__file__).resolve().parent.parent

DRIVER = r"""
const { chromium } = require("playwright");
const fs = require("fs");
const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const wait = ms => new Promise(r => setTimeout(r, ms));

async function shoot(browser, shot) {
  const ctx = await browser.newContext({ viewport: { width: shot.width, height: shot.height } });
  await ctx.addInitScript(lang => {
    try { localStorage.setItem("labconsole.lang", lang); } catch {}
    window.WebSocket = undefined;  // The control socket carries only commands.
  }, shot.lang);
  await ctx.route("**/*", route =>
    route.request().method() === "GET" ? route.continue() : route.abort());
  const page = await ctx.newPage();
  await page.goto(job.base + "#" + shot.hash);
  await wait(job.wait);
  try {
    if (shot.script) { await page.evaluate(shot.script); await wait(400); }
    const target = shot.clip ? page.locator(shot.clip).first() : page;
    const full = shot.clip ? {} : { fullPage: job.fullPage };
    await target.screenshot({ path: shot.out, timeout: 10000, ...full });
    console.log(shot.out);
  } catch (error) {
    failed++;
    console.error("skip " + shot.out + ": " + String(error.message || error).split("\n")[0]);
  }
  await ctx.close();
}
let failed = 0;

async function compose(browser, item) {
  const page = await browser.newPage({ viewport: { width: 400, height: 300 } });
  const src = file => "data:image/png;base64," + fs.readFileSync(file).toString("base64");
  const img = file => `<img src="${src(file)}" style="display:block">`;
  await page.setContent(`<body style="margin:0;background:#888">${img(item.top)}` +
    `<div style="height:8px"></div>${img(item.bottom)}</body>`);
  await page.screenshot({ path: item.out, fullPage: true });
  await page.close();
}

(async () => {
  const browser = await chromium.launch();
  try {
    const queue = [...job.shots];  // 4 pages at a time: most of a shot is the wait after load.
    const worker = async () => { while (queue.length) await shoot(browser, queue.shift()); };
    await Promise.all([worker(), worker(), worker(), worker()]);
    for (const item of job.composites) { await compose(browser, item); console.log(item.out); }
  } finally { await browser.close(); }
  if (failed) process.exit(2);
})().catch(error => { console.error(error); process.exit(1); });
"""


def parse_viewport(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"Viewport must be WIDTHxHEIGHT, not {text!r}.")
    return int(match[1]), int(match[2])


def parse_state(text: str) -> tuple[str, str]:
    name, sep, path = text.partition("=")
    if not sep or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise argparse.ArgumentTypeError("State must be NAME=FILE.js.")
    return name, Path(path).read_text(encoding="utf-8")


def shot_name(page: str, lang: str, viewport: tuple[int, int], state: str = "") -> str:
    name = f"{page}-{lang}-{viewport[0]}x{viewport[1]}"
    return f"{name}-{state}.png" if state else f"{name}.png"


def plan_shots(pages, langs, viewports, states, out: Path, clip: str | None = None) -> list[dict]:
    """One shot per page, language, viewport and state. The state "" is the page as loaded."""
    shots = []
    for page in pages:
        for lang in langs:
            for viewport in viewports:
                for state, script in [("", None), *states]:
                    shots.append(
                        {
                            "hash": PAGES[page],
                            "lang": lang,
                            "width": viewport[0],
                            "height": viewport[1],
                            "script": script,
                            "clip": clip,
                            "out": str(out / shot_name(page, lang, viewport, state)),
                        }
                    )
    return shots


def plan_composites(before: Path, after: Path, out: Path) -> list[dict]:
    """Pair the PNGs with the same name in 2 directories: before on top."""
    names = sorted({p.name for p in before.glob("*.png")} & {p.name for p in after.glob("*.png")})
    return [
        {"top": str(before / n), "bottom": str(after / n), "out": str(out / ("cmp-" + n))}
        for n in names
    ]


def node_path() -> str:
    npm = shutil.which("npm")
    if not npm:
        sys.exit("npm is not on PATH. Install Node and `npm install -g playwright`.")
    return subprocess.run(
        [npm, "root", "-g"], capture_output=True, text=True, check=True
    ).stdout.strip()


SKIPPED = 2  # The driver exit code when some shots failed and the others were saved.


def run_driver(
    base: str, shots: list[dict], composites: list[dict], wait_ms: int, full_page: bool
) -> int:
    """Run the Node driver once. Exit on a driver failure; return SKIPPED when some shots failed."""
    with tempfile.TemporaryDirectory() as tmp:
        driver, job = Path(tmp, "driver.cjs"), Path(tmp, "job.json")
        driver.write_text(DRIVER, encoding="utf-8")
        job.write_text(
            json.dumps(
                {
                    "base": base,
                    "shots": shots,
                    "composites": composites,
                    "wait": wait_ms,
                    "fullPage": full_page,
                }
            ),
            encoding="utf-8",
        )
        env = {**os.environ, "NODE_PATH": node_path()}
        code = subprocess.run(["node", str(driver), str(job)], env=env).returncode
    if code not in (0, SKIPPED):
        sys.exit(f"The Playwright driver failed (exit {code}).")
    return code


class DemoServer:
    """`orchestrator serve --demo` on a free local port, stopped on exit."""

    def __enter__(self) -> str:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "orchestrator",
                "serve",
                "--demo",
                "--recipes-dir",
                str(REPO / "recipes"),
                "--port",
                str(port),
                "--base-path",
                "/lab/",
            ],
            cwd=REPO,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}/lab/"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.log.seek(0)
                sys.exit(
                    "The demo server stopped:\n" + self.log.read().decode(errors="replace")[-2000:]
                )
            try:
                urllib.request.urlopen(url, timeout=2).close()
                return url
            except OSError:
                time.sleep(0.5)
        self.__exit__()
        sys.exit("The demo server did not start in 60 s.")

    def __exit__(self, *exc) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


def shoot_into(args, base: str | None, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    shots = plan_shots(args.pages, args.langs, args.viewports, args.state, out, args.clip)
    if base is None:
        with DemoServer() as url:
            return run_driver(url, shots, [], args.wait, args.full_page)
    return run_driver(base, shots, [], args.wait, args.full_page)


def csv(choices):
    def parse(text: str) -> list[str]:
        items = [item.strip() for item in text.split(",") if item.strip()]
        bad = [item for item in items if item not in choices]
        if bad or not items:
            raise argparse.ArgumentTypeError(f"Choose from {', '.join(choices)}.")
        return items

    return parse


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", type=Path, required=True, help="Output directory")
    common.add_argument("--pages", type=csv(tuple(PAGES)), default=list(PAGES))
    common.add_argument("--langs", type=csv(LANGS), default=list(LANGS))
    common.add_argument(
        "--viewports",
        type=lambda t: [parse_viewport(v) for v in t.split(",")],
        default=[parse_viewport(v) for v in VIEWPORTS],
    )
    common.add_argument(
        "--state",
        type=parse_state,
        action="append",
        default=[],
        help="NAME=FILE.js: a script that runs in the page after load; adds 1 shot per state",
    )
    common.add_argument("--clip", help="CSS selector: capture only this element")
    common.add_argument("--full-page", action="store_true", help="Capture the full scroll height")
    common.add_argument("--wait", type=int, default=3000, help="Milliseconds to wait after load")

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    shoot = sub.add_parser("shoot", parents=[common], help="Capture pages")
    shoot.add_argument("--url", default=DEFAULT_URL)
    shoot.add_argument(
        "--demo", action="store_true", help="Start a local demo server instead of --url"
    )
    compare = sub.add_parser(
        "compare", parents=[common], help="Before/after composites, before on top"
    )
    compare.add_argument("before", help="URL or directory of shots")
    compare.add_argument("after", nargs="?", help="URL or directory of shots; omit with --demo")
    compare.add_argument(
        "--demo", action="store_true", help="Use a local demo server as the after side"
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "shoot":
        sys.exit(shoot_into(args, None if args.demo else args.url, args.out))
    if (args.after is None) != args.demo:
        sys.exit("Give AFTER or --demo, not both.")
    code, sides = 0, []
    for label, source in (("before", args.before), ("after", args.after)):
        if source is not None and Path(source).is_dir():
            sides.append(Path(source))
        else:
            target = args.out / label
            code = max(code, shoot_into(args, source, target))
            sides.append(target)
    composites = plan_composites(sides[0], sides[1], args.out)
    if not composites:
        sys.exit("No PNG names match between the 2 sides.")
    run_driver("about:blank", [], composites, 0, False)
    sys.exit(code)


if __name__ == "__main__":
    main()
