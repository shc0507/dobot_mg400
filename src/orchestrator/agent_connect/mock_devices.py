"""Attach the mock Console to the independent, loopback-only stage simulator."""

import ipaddress
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from labkit.errors import ServiceError
from labkit.events import EventLog
from orchestrator.clients import ServiceAdapter
from orchestrator.devices import Devices
from orchestrator.web.console import Console
from orchestrator.web.turret_preview import TurretPreview

STAGE_URL = "http://127.0.0.1:8794/stage/contract/"


class MockDevices:
    def __init__(self, stage_url=STAGE_URL):
        address = urlsplit(stage_url)
        if (
            address.scheme != "http"
            or not ipaddress.ip_address(address.hostname or "").is_loopback
            or address.path != "/stage/contract/"
            or address.username is not None
            or address.query
            or address.fragment
        ):
            raise ValueError("Use the local simulated stage contract URL.")
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-portal-profiles-")

        def client(name, identity="agent-simulation"):
            return ServiceAdapter(stage_url, identity, timeout=2) if name == "stage" else None

        def unavailable(name):
            raise ServiceError(409, f"The {name} simulator is unavailable.")

        service = SimpleNamespace(
            demo=True,
            temporary=self.temporary,
            settings={},
            events=EventLog(),
            dashboard=SimpleNamespace(text=str, refresh=lambda: None),
            dashboard_configured=lambda name: name == "stage",
            console_client=client,
            dashboard_client=client,
            console_start_demo=unavailable,
            turret_preview=TurretPreview(Path(self.temporary.name) / "turret"),
        )
        service.console = Console(service)
        service.devices = Devices(service, on_events=service.console.pull_events)
        self.service = service

    def close(self):
        transfer = getattr(self.service, "sample_transfer", None)
        if transfer is not None:
            transfer.close()
        self.service.devices.close()
        self.service.console.close()
        self.temporary.cleanup()
