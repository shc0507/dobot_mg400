"""Reviewed operating context and supported Console actions for the local agent."""

import copy
import hashlib
from pathlib import Path

from labkit.errors import ServiceError

from .actions import descriptors

DOCS = "https://github.com/shc0507/dobot_mg400/blob/main/docs/agent-connect/"
REVIEWED_DOCUMENTS = {
    "project-knowledge.md": "564833ec58dbc8e29eac36aa8d7c6cdd24f302891494072ea5a96ac7255b4451",
    "communication-contract.md": "8045e18a5066b696d1ab318b3387c5cf1c34c0b1108c801702e90d26c2f4492c",
}


class ConsoleAdaptation:
    """Load human-reviewed guides once; runtime checks never rewrite the connector."""

    def __init__(self, root=None):
        root = Path(root) if root is not None else Path(__file__).resolve().parents[3]
        self.knowledge = ""
        self.problem = ""
        self.documents = {}
        self.actions = {item["id"]: item for item in descriptors()}
        for name, expected in REVIEWED_DOCUMENTS.items():
            path = "docs/agent-connect/" + name
            try:
                text = (root / path).read_text(encoding="utf-8")
                digest = hashlib.sha256(text.encode()).hexdigest()
            except (OSError, UnicodeError):
                text, digest = "", None
            self.documents[name] = {"path": path, "digest": digest}
            if digest != expected:
                self.problem = (
                    "The integration guides changed or are missing. Ask your coding agent to "
                    "read both guides, update this adapter, and test it before sending commands."
                )
            if name == "project-knowledge.md":
                self.knowledge = text

    def status(self):
        return {
            "status": "update_required" if self.problem else "ready",
            "message": self.problem
            or "Reviewed project rules loaded. Live formats are checked before each instruction.",
            "project_knowledge": dict(self.documents["project-knowledge.md"]),
            "communication_contract": dict(self.documents["communication-contract.md"]),
            "documents": [
                {"title": "Adaptation guide", "url": DOCS + "README.md"},
                {"title": "Project knowledge", "url": DOCS + "project-knowledge.md"},
                {"title": "Communication contract", "url": DOCS + "communication-contract.md"},
            ],
        }

    def context(self, catalog):
        if self.problem:
            raise ServiceError(409, self.problem)
        for action in catalog["actions"]:
            known = self.actions.get(action["id"])
            if known is None or any(
                action.get(key) != known.get(key)
                for key in ("revision", "device", "params", "defaults")
            ):
                raise ServiceError(
                    409,
                    "The Console action definitions changed. Ask your coding agent to update "
                    "the local adapter using the integration guides. No command was sent.",
                )
        return self.knowledge

    def public(self):
        return copy.deepcopy(self.status())
