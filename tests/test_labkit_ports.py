from labkit import ports
from orchestrator.clients import SERVICE_URLS


def test_every_port_is_distinct_and_on_loopback():
    assert len(set(ports.PORTS.values())) == len(ports.PORTS)
    assert ports.url("orch", "lab/") == "http://127.0.0.1:8772/lab/"


def test_orchestrator_service_urls_come_from_the_table():
    assert SERVICE_URLS["MG400_ORCH_ROBOT_URL"] == "http://127.0.0.1:8766/"
    assert SERVICE_URLS["MG400_ORCH_STAGE_URL"] == "http://127.0.0.1:8767/"
    assert len(SERVICE_URLS) == 6
