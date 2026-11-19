"""Run the real Stage contract in process with simulated encoder hardware."""

from labkit.client import ServiceClient
from labkit.errors import ServiceError
from stage_controller.app import create_app
from stage_controller.controller import MotorController

from ..clients import ServiceAdapter


class _LocalClient(ServiceClient):
    def __init__(self, app, identity):
        super().__init__("http://preview.invalid/contract/", identity)
        self.app = app

    def _request(self, path, data=None):
        with self.app.test_client() as client:
            response = client.open(
                "/contract/" + path,
                method="GET" if data is None else "POST",
                json=data,
                headers={"X-MG400-Client": self.client_id},
            )
            result = response.get_json()
        if response.status_code >= 400:
            raise ServiceError(response.status_code, result.get("error", "Preview request failed."))
        return result


class _LocalAdapter(ServiceAdapter):
    def __init__(self, app, identity):
        self.identity, self.timeout = identity, 2
        self._lease_seconds = 0
        self._http = _LocalClient(app, identity)


class StagePreview:
    """Use the production command, lease, and telemetry paths without sockets."""

    def __init__(self):
        self.controller = MotorController(demo=True)
        self.controller.connect("DEMO")
        self.controller.start()
        self.app = create_app(self.controller)

    def client(self, name, identity="console-preview"):
        return _LocalAdapter(self.app, identity) if name == "stage" else None

    def state(self):
        return self.client("stage").state()

    def close(self):
        self.controller.close()
