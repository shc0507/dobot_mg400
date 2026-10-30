"""FastAPI application factory; mounting this app does not open USB on import."""

import json
import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from labkit.auth import bearer_matches, host_allowed, host_port, same_origin, valid_token_for_bind
from labkit.errors import ServiceError
from labkit.lease import validate_client

from .roi import select_region
from .service import CameraService

STATIC = Path(__file__).parent / "static"
CONTRACT_PATHS = {
    f"/api/{name}"
    for name in ("describe", "state", "events", "heartbeat", "release", "stop", "command", "roi")
}


class GainRequest(BaseModel):
    gain: Literal["high"]


def create_app(*, demo=False, auto_connect=True, service=None, token=None):
    if not valid_token_for_bind(token, "127.0.0.1"):
        raise ValueError("A configured token must contain at least 24 printable ASCII characters")
    controller = service or CameraService(demo=demo)

    @asynccontextmanager
    async def lifespan(app):
        if auto_connect:
            controller.connect()
        try:
            yield
        finally:
            await run_in_threadpool(controller.disconnect)

    app = FastAPI(title="Thermal Master P1", version="0.1.0", lifespan=lifespan)
    app.state.camera = controller

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        headers = request.headers
        is_contract = request.url.path in CONTRACT_PATHS

        def denied(message, status_code, **kwargs):
            key = "error" if is_contract else "detail"
            return JSONResponse({key: message}, status_code=status_code, **kwargs)

        # Reject ambiguous headers before access checks or camera methods run.
        if len(headers.getlist("host")) != 1 or host_port(headers.get("host", ""), 80) is None:
            response = denied("Invalid Host header", 400)
        elif not host_allowed(headers, token):
            response = denied("A token is required outside loopback", 403 if is_contract else 400)
        elif not same_origin(headers) or (
            headers.get("origin") and urlsplit(headers["origin"]).netloc != headers.get("host")
        ):
            response = denied("Camera control requires the same origin", 403)
        else:
            public = request.method in {"GET", "HEAD"} and (
                request.url.path == "/" or request.url.path.startswith("/static/")
            )
            if (
                token
                and not public
                and (
                    len(headers.getlist("authorization")) != 1
                    or not bearer_matches(headers.get("authorization", ""), token)
                )
            ):
                response = denied(
                    "A valid bearer token is required",
                    401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            else:
                response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status():
        return controller.status()

    def latest():
        try:
            return controller.latest()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/frame")
    def frame():
        """One coherent frame; matrices are [y][x], temperatures always Celsius."""
        return JSONResponse(latest().payload())

    @app.get("/api/snapshot.npz")
    def snapshot():
        current = latest()
        return Response(
            current.npz(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="p1-{current.sequence}.npz"'},
        )

    @app.post("/api/connect")
    def connect():
        return controller.connect()

    @app.post("/api/disconnect")
    def disconnect():
        try:
            return controller.disconnect()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    def command(name, argument=None):
        try:
            return controller.command(name, argument)
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/shutter")
    def shutter():
        """Trigger the camera's non-uniformity correction shutter."""
        return command("shutter")

    @app.post("/api/gain")
    def gain(body: GainRequest):
        return command("gain", body.gain)

    add_contract_routes(app, controller)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def add_contract_routes(app, controller):
    """Adapt the read-only thermal contract without changing legacy controls."""

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

    @app.get("/api/describe")
    def discovery():
        return controller.describe()

    @app.get("/api/state")
    def state():
        return controller.snapshot()

    @app.get("/api/events")
    def events(request: Request):
        values = request.query_params.getlist("since")
        raw = values[0] if values else "0"
        if len(values) > 1 or not raw.isascii() or not raw.isdecimal():
            raise ServiceError(400, "Event sequence must be a nonnegative integer.")
        try:
            return controller.events.since(int(raw))
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from exc

    @app.post("/api/heartbeat")
    async def heartbeat(request: Request):
        await body(request)
        return {"lease": controller.lease.acquire(request.headers["x-mg400-client"])}

    @app.post("/api/release")
    async def release(request: Request):
        await body(request)
        return {"lease": controller.lease.release(request.headers["x-mg400-client"])}

    @app.post("/api/stop")
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

    @app.post("/api/command")
    async def contract_command(request: Request):
        await body(request)
        raise ServiceError(400, "The thermal contract has no commands.")

    @app.get("/api/roi")
    def roi(request: Request):
        try:
            name, region = select_region(request.query_params.multi_items(), controller.regions)
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from exc
        try:
            return controller.region_reading(region, name=name)
        except RuntimeError as exc:
            raise ServiceError(503, str(exc)) from exc
