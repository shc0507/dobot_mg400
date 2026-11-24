"""Render the Agent Connect test gateway and port 8082 proxy route."""

import re
from pathlib import PurePosixPath

MARKER = "# Agent Connect test emergency gateway: fixed Stop and status routes."
BLOCK = """\t# Agent Connect test emergency gateway: fixed Stop and status routes.
\thandle_path /lab/api/agent/emergency/* {
\t\timport instrument 127.0.0.2:8874
\t}
"""
LEGACY_BLOCK = BLOCK.replace(
    MARKER, "# Agent Connect test emergency gateway: physical dispatch disabled."
)


def proxy_config(text):
    if not re.search(r"(?m)^http://[^\n{]*:8082\s*\{", text):
        raise ValueError("This gateway applies only to the port 8082 test proxy.")
    if text.count(LEGACY_BLOCK) == 1:
        text = text.replace(LEGACY_BLOCK, BLOCK)
    if MARKER in text:
        if text.count(BLOCK) != 1:
            raise ValueError("Review the existing test emergency gateway route.")
        return text
    if "/lab/api/agent/emergency/" in text:
        raise ValueError("Review the existing emergency route before updating it.")
    anchor = "\thandle /lab/* {"
    if text.count(anchor) != 1:
        raise ValueError("The test proxy has no unique Console route.")
    return text.replace(anchor, BLOCK + anchor)


def service_unit(checkout, *, physical=False):
    path = str(PurePosixPath(checkout))
    if not path.startswith("/") or re.search(r"[\s\"'\\%]", path):
        raise ValueError("Use an absolute checkout path without shell or unit escapes.")
    arguments = " --physical" if physical else ""
    return f"""[Unit]
Description=Agent Connect test emergency Stop and status gateway
After=network.target

[Service]
Type=simple
User=1000
Group=1000
WorkingDirectory={path}
ExecStart={path}/.venv/bin/python -m orchestrator.agent_connect.fallback{arguments}
Restart=on-failure
RestartSec=3
UMask=0077

[Install]
WantedBy=multi-user.target
"""
