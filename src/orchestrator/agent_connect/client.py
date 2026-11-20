"""Read-only scripted clients with independent verified contract caches."""

import copy
import json
import math
import re
import time
import urllib.request
from collections import deque

from .contract import PREFIX, compatibility, differences, digest

ORIGIN = "http://127.0.0.1:8790"
DISCOVERY = PREFIX + "/discovery"
STATE = PREFIX + "/v1/state"
BUNDLE_PATH = re.compile(re.escape(PREFIX) + r"/contracts/[a-f0-9]{64}\.json")


def validate_state(state, bundle):
    """Check the known script inputs. Unknown quality values stay unavailable."""
    for key in ("seq", "sampled_at", "age_s"):
        value = state.get(key)
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid state.{key}.")
    if type(state["seq"]) is not int:
        raise ValueError("The state sequence must be an integer.")
    for group, value_key in (("stage", "position"), ("temperature", "value")):
        row = state.get(group)
        if not isinstance(row, dict):
            raise ValueError(f"Missing state.{group}.")
        if value_key not in row:
            raise ValueError(f"Missing {group}.{value_key}.")
        value = row[value_key]
        if value is not None and (type(value) not in {int, float} or not math.isfinite(value)):
            raise ValueError(f"Invalid {group}.{value_key}.")
        expected = bundle["state_semantics"][f"{group}.{value_key}"]["unit"]
        if row.get("unit") != expected:
            raise ValueError(f"The {group} unit does not match the verified contract.")
        if row.get("quality") not in {"good", "stale", "unknown"}:
            raise ValueError(f"Unknown {group} quality. The reading is unavailable.")
    if state.get("quality") not in {"good", "stale", "unknown"}:
        raise ValueError("Unknown source quality. The reading is unavailable.")
    stage = state["stage"]
    if (
        not isinstance(stage.get("instance"), str)
        or type(stage.get("reference_revision")) is not int
    ):
        raise ValueError("The stage instance or reference revision is unavailable.")
    if not isinstance(state.get("samples"), list):
        raise ValueError("The sample fixture list is unavailable.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Mock redirects are disabled.")


class LocalTransport:
    """Use the fixed mock server only. Ignore proxy environment variables."""

    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __call__(self, path, data=None):
        allowed = path in {DISCOVERY, STATE} or BUNDLE_PATH.fullmatch(path)
        if data is None and not allowed:
            raise ValueError("Only mock discovery, contract and state reads are permitted.")
        if data is not None and path != "/mock/control":
            raise ValueError("Only mock controls are permitted.")
        if data is not None and self(DISCOVERY).get("mode") != "MOCK":
            raise ValueError("The Console is not in MOCK mode.")
        body = None if data is None else json.dumps(data).encode()
        request = urllib.request.Request(
            ORIGIN + path, data=body, headers={"Content-Type": "application/json"}
        )
        with self.opener.open(request, timeout=2) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Mock response exceeds the local limit.")
        return json.loads(raw)


class ScriptedClient:
    def __init__(self, name, generation, transport):
        self.name, self.generation, self.transport = name, generation, transport
        self.connected = True
        self.cache = {}
        self.discovery = None
        self.bundle = None
        self.state = None
        self.raw_state = None
        self.cached_id = None
        self.changes = []
        self.messages = deque(maxlen=60)
        self.status = {"status": "connecting", "problems": []}
        self.received_at = None
        self.error = None

    def _get(self, path):
        value = self.transport(path)
        self.messages.append({"method": "GET", "path": path, "response": copy.deepcopy(value)})
        return value

    def refresh(self):
        discovery = self._get(DISCOVERY)
        if discovery.get("mode") != "MOCK" or discovery.get("discovery_version") != 1:
            raise ValueError("Expected the Phase 0 MOCK discovery protocol.")
        self.discovery = discovery
        identity = discovery.get("contract_id", "")
        expected_path = PREFIX + "/contracts/" + identity.removeprefix("sha256:") + ".json"
        if (
            not BUNDLE_PATH.fullmatch(expected_path)
            or discovery.get("contract_url") != expected_path
        ):
            raise ValueError("The contract URL is outside the mock bundle paths.")
        if discovery.get("endpoints", {}).get("state") != STATE:
            raise ValueError("The state URL is outside the mock state path.")
        if identity != self.cached_id:
            bundle = self.cache.get((ORIGIN, identity))
            if bundle is None:
                bundle = self._get(expected_path)
                if digest(bundle) != identity:
                    raise ValueError("Contract digest mismatch. The bundle was not cached.")
            latest = self._get(DISCOVERY)
            if latest != discovery:
                raise ValueError("The release changed during download. Rediscovery is required.")
            if bundle.get("mode") != "MOCK":
                raise ValueError("Expected a MOCK contract bundle.")
            self.status = compatibility(bundle, self.generation)
            self.changes = differences(self.bundle or {}, bundle)
            self.cache[(ORIGIN, identity)] = copy.deepcopy(bundle)
            self.bundle, self.cached_id = bundle, identity
        return discovery

    def poll(self):
        if not self.connected:
            return self.report()
        try:
            self.refresh()
            state = self._get(STATE)
            if state.get("mode") != "MOCK":
                raise ValueError("The state is not MOCK data.")
            if state.get("contract_id") != self.cached_id:
                raise ValueError("The state contract changed. Rediscovery is required.")
            if state.get("boot_id") != self.discovery["boot_id"]:
                raise ValueError("The mock Console restarted. Rediscovery is required.")
            self.raw_state = state
            if self.status["status"] not in {"compatible", "supported subset"}:
                self.state = None
                self.error = None
                return self.report()
            validate_state(state, self.bundle)
            self.state = state
            self.received_at = time.time()
            self.error = None
            self.status = compatibility(self.bundle, self.generation)
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.error = str(error)
            self.status = {"status": "blocked", "problems": [str(error)]}
        return self.report()

    def report(self):
        return copy.deepcopy(
            {
                "name": self.name,
                "generation": self.generation,
                "connected": self.connected,
                "compatibility": self.status,
                "discovery": self.discovery,
                "cached_contract_id": self.cached_id,
                "changes": self.changes,
                "state": self.state,
                "raw_state": self.raw_state,
                "received_at": self.received_at,
                "error": self.error,
                "normal_commands": False,
            }
        )
