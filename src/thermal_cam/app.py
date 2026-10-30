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
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .service import CameraService

STATIC = Path(__file__).parent / "static"


class GainRequest(BaseModel):
    gain: Literal["high"]


def create_app(*, demo=False, auto_connect=True, service=None):
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
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        # Disallow browser-origin camera control from unrelated websites.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Camera control requires the same origin"}, status_code=403)
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
        return Response(current.npz(), media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="p1-{current.sequence}.npz"'})

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
