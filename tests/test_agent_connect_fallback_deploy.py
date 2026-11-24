"""Keep the disabled emergency gateway separate from the main proxy and devices."""

import pytest

from orchestrator.agent_connect.fallback_deploy import proxy_config, service_unit


def test_test_proxy_route_precedes_console_and_is_idempotent():
    original = "http://:8082 {\n\thandle /lab/* {\n\t\treverse_proxy localhost:8872\n\t}\n}\n"
    result = proxy_config(original)
    assert result.index("handle_path /lab/api/agent/emergency/*") < result.index("handle /lab/*")
    assert "127.0.0.2:8874" in result
    assert proxy_config(result) == result
    assert result.endswith(original[original.index("\thandle /lab/*") :])


@pytest.mark.parametrize(
    "original",
    [
        "http://:8080 {\n\thandle /lab/* {\n}\n}",
        "http://:8082 {\n}",
        "http://:8082 {\n\thandle /lab/* {\n}\n\thandle /lab/* {\n}\n}",
        "http://:8082 {\n\thandle /lab/api/agent/emergency/* {\n}\n\thandle /lab/* {\n}\n}",
    ],
)
def test_refuse_other_proxy_or_conflicting_route(original):
    with pytest.raises(ValueError):
        proxy_config(original)


def test_unit_starts_disabled_gateway_without_device_environment():
    result = service_unit("/opt/mg400-twin-test")
    assert "-m orchestrator.agent_connect.fallback\n" in result
    assert "--simulation" not in result
    assert "EnvironmentFile" not in result
    assert "User=1000" in result


@pytest.mark.parametrize("path", ["relative", "/opt/space path", "/opt/%i", "/opt/line\nbreak"])
def test_unit_refuses_unsafe_path(path):
    with pytest.raises(ValueError):
        service_unit(path)
