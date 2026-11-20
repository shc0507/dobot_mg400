"""Deterministic Phase 0 checks through serialized mock HTTP responses."""

from .client import ScriptedClient
from .mock import MockConsole, create_mock_app


def run_scenarios():
    ticks = [100.0]
    source = MockConsole(clock=lambda: ticks[0], wall=lambda: 1700000000 + ticks[0])
    http = create_mock_app(source).test_client()

    def transport(path):
        response = http.get(path)
        if response.status_code != 200:
            raise ValueError(f"Mock HTTP {response.status_code}")
        return response.get_json()

    clients = [ScriptedClient("A", 1, transport), ScriptedClient("B", 2, transport)]
    results = []

    def check(name, expected, actual):
        results.append(
            {"name": name, "expected": expected, "actual": actual, "passed": actual == expected}
        )

    def control(action, value=None):
        response = http.post("/mock/control", json={"action": action, "value": value})
        return response.get_json()

    first = clients[0].poll()
    check("Old client / baseline", "compatible", first["compatibility"]["status"])
    check(
        "New client / older release",
        "supported subset",
        clients[1].poll()["compatibility"]["status"],
    )
    ticks[0] += 1
    current = clients[0].poll()["state"]
    check(
        "Source values change",
        True,
        current["stage"]["position"] != first["state"]["stage"]["position"],
    )
    control("pause")
    sampled_at = current["sampled_at"]
    ticks[0] += 4
    stale = clients[0].poll()["state"]
    check("Paused sample becomes stale", "stale", stale["quality"])
    check("Polling preserves source time", sampled_at, stale["sampled_at"])
    baseline_id = clients[0].cached_id
    control("fixture", "compatible")
    check("Old client / optional field", "compatible", clients[0].poll()["compatibility"]["status"])
    check("New client / new release", "compatible", clients[1].poll()["compatibility"]["status"])
    compatible_id = clients[0].cached_id
    control("fixture", "incompatible")
    check(
        "Changed units need a client update",
        "client update required",
        clients[0].poll()["compatibility"]["status"],
    )
    incompatible_id = clients[0].cached_id
    control("fixture", "unsupported-dialect")
    check(
        "Unknown dialect is blocked",
        "client update required",
        clients[1].poll()["compatibility"]["status"],
    )
    control("fixture", "broken-digest")
    # This client has no cached copy of the baseline bundle.
    broken = ScriptedClient("digest-check", 1, transport)
    check("Broken digest is blocked", "blocked", broken.poll()["compatibility"]["status"])
    control("fixture", "baseline")
    before = clients[0].poll()["state"]
    clients[0].connected = False
    control("restart")
    check("Disconnected client retains its last frame", before, clients[0].poll()["state"])
    clients[0].connected = True
    after = clients[0].poll()["state"]
    check("Reconnect discovers restart", True, before["boot_id"] != after["boot_id"])
    revision = after["stage"]["reference_revision"]
    control("reference-reset")
    check(
        "Reference reset changes revision",
        revision + 1,
        clients[0].poll()["state"]["stage"]["reference_revision"],
    )
    control("reset")
    reset = clients[0].poll()["state"]
    check(
        "Reset restores starting values",
        [0.0, 22.0, 2],
        [reset["stage"]["position"], reset["temperature"]["value"], len(reset["samples"])],
    )
    return {
        "mode": "MOCK",
        "scope": "Phase 0 only: no hardware or command dispatch",
        "fixture_versions": {"A": 1, "B": 2},
        "contract_ids": {
            "baseline": baseline_id,
            "compatible": compatible_id,
            "incompatible": incompatible_id,
        },
        "passed": all(row["passed"] for row in results),
        "results": results,
        "messages": {client.name: list(client.messages) for client in [*clients, broken]},
    }
