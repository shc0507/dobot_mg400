"""Thermal contract and frame API. Mounting this app does not open USB on import."""

import json
import math
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from labkit.errors import ServiceError
from labkit.lease import validate_client

from .roi import select_region
from .service import CameraService


def normalize_base_path(value):
    """Return a literal URL prefix, with the root path represented by an empty string."""
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*/?", value)
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Invalid thermal base path: use absolute URL path segments.")
    return value.rstrip("/")


def create_app(*, demo=False, auto_connect=True, service=None, base_path="/"):
    prefix = normalize_base_path(base_path)
    controller = service or CameraService(demo=demo)

    @asynccontextmanager
    async def lifespan(app):
        if auto_connect:
            controller.connect()
        try:
            yield
        finally:
            await run_in_threadpool(controller.disconnect)

    app = FastAPI(
        title="Thermal Master P1",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.camera = controller

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def latest():
        try:
            return controller.latest()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get(prefix + "/api/frame")
    def frame():
        """One coherent frame; matrices are [y][x], temperatures always Celsius."""
        return JSONResponse(latest().payload())

    add_contract_routes(app, controller, prefix=prefix)
    return app


def add_contract_routes(app, controller, *, prefix=""):
    """Adapt the thermal contract: reads, a no-op stop, connect and the shutter."""

    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        return JSONResponse({"error": error.message}, status_code=error.status)

    async def body(request):
        headers = request.headers
        if (
            len(headers.getlist("content-type")) != 1
            or headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise ServiceError(415, "Expected Content-Type: application/json.")
        if len(headers.getlist("x-mg400-client")) != 1:
            raise ServiceError(400, "Expected exactly one X-MG400-Client header.")
        try:
            validate_client(headers.get("x-mg400-client"))
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from exc
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 4096:
                raise ServiceError(400, "Contract request exceeds 4096 bytes.")
            chunks.append(chunk)

        def invalid_constant(value):
            raise ValueError("JSON numbers must be finite.")

        try:
            data = json.loads(b"".join(chunks), parse_constant=invalid_constant)
        except ValueError as exc:
            raise ServiceError(400, "Expected a valid JSON object.") from exc
        if not isinstance(data, dict):
            raise ServiceError(400, "Expected a JSON object.")
        return data

    @app.get(prefix + "/api/describe")
    def discovery():
        return controller.describe()

    @app.get(prefix + "/api/state")
    def state():
        return controller.snapshot()

    @app.get(prefix + "/api/events")
    def events(request: Request):
        values = request.query_params.getlist("since")
        raw = values[0] if values else "0"
        if len(values) > 1 or not raw.isascii() or not raw.isdecimal():
            raise ServiceError(400, "Event sequence must be a nonnegative integer.")
        try:
            return controller.events.since(int(raw))
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from exc

    @app.post(prefix + "/api/heartbeat")
    async def heartbeat(request: Request):
        await body(request)
        return {"lease": controller.lease.acquire(request.headers["x-mg400-client"])}

    @app.post(prefix + "/api/renew")
    async def renew(request: Request):
        await body(request)
        return {"lease": controller.lease.renew(request.headers["x-mg400-client"])}

    @app.post(prefix + "/api/release")
    async def release(request: Request):
        await body(request)
        return {"lease": controller.lease.release(request.headers["x-mg400-client"])}

    @app.post(prefix + "/api/stop")
    async def stop(request: Request):
        wait_s = (await body(request)).get("wait_s", 0)
        if (
            isinstance(wait_s, bool)
            or not isinstance(wait_s, (int, float))
            or not 0 <= wait_s <= 15
            or not math.isfinite(wait_s)
        ):
            raise ServiceError(400, "wait_s must be a finite number from 0 to 15.")
        outcome = controller.stop(request.headers["x-mg400-client"], wait_s)
        return {"stop": outcome, "state": controller.snapshot()}

    @app.post(prefix + "/api/command")
    async def contract_command(request: Request):
        data = await body(request)
        action = data.pop("action", None)
        ident = await run_in_threadpool(
            controller.contract_command, action, data, request.headers["x-mg400-client"]
        )
        return JSONResponse({"command_id": ident, "state": controller.snapshot()}, status_code=202)

    @app.get(prefix + "/api/roi")
    def roi(request: Request):
        try:
            name, region = select_region(request.query_params.multi_items(), controller.regions)
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from exc
        try:
            return controller.region_reading(region, name=name)
        except RuntimeError as exc:
            raise ServiceError(503, str(exc)) from exc
