"""FastAPI application factory; mounting this app does not open USB on import."""

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

from .service import CameraService

STATIC = Path(__file__).parent / "static"


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
        # Reject ambiguous headers before access checks or camera methods run.
        if len(headers.getlist("host")) != 1 or host_port(headers.get("host", ""), 80) is None:
            response = JSONResponse({"detail": "Invalid Host header"}, status_code=400)
        elif not host_allowed(headers, token):
            response = JSONResponse(
                {"detail": "A token is required outside loopback"}, status_code=400
            )
        elif not same_origin(headers) or (
            headers.get("origin") and urlsplit(headers["origin"]).netloc != headers.get("host")
        ):
            response = JSONResponse(
                {"detail": "Camera control requires the same origin"}, status_code=403
            )
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
                response = JSONResponse(
                    {"detail": "A valid bearer token is required"},
                    status_code=401,
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

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
