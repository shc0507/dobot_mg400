"""Microscope contract routes and explicit operator focus-reference requests.

The service owns both lease classifications and every device operation. Creating
this app does not start the service, connect devices, or infer operator authority
from a client identifier.
"""

import threading
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from labkit.auth import bearer_matches, host_allowed, same_origin
from labkit.errors import ServiceError
from labkit.flask import contract_blueprint
from labkit.lease import validate_client


def create_app(service, *, token=None, base_path="/"):
    """Expose the shared contract plus the separate, confirmed operator action."""
    blueprint = contract_blueprint(service, token=token, base_path=base_path, lease=service.lease)
    prefix = base_path.rstrip("/")
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.register_blueprint(blueprint)
    # The CLI has six workers. Bound long-lived responses to leave workers for
    # state and control requests; individual frame reads also remain bounded.
    stream_slots = threading.BoundedSemaphore(4)
    assets = Path(__file__).parent / "static"

    @app.before_request
    def access():
        if not host_allowed(request.headers, token):
            raise ServiceError(403, "Use a loopback Host without forwarded headers.")
        public = request.method in ("GET", "HEAD") and request.endpoint in (
            "panel",
            "panel_app",
            "panel_css",
        )
        if (
            not public
            and token
            and not bearer_matches(request.headers.get("Authorization", ""), token)
        ):
            raise ServiceError(401, "Invalid service access token.")
        if not same_origin(request.headers):
            raise ServiceError(403, "The request origin is not allowed.")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if request.mimetype != "application/json":
                raise ServiceError(415, "Expected Content-Type: application/json.")
            validate_client(request.headers.get("X-MG400-Client"))

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'self'"
        )
        return response

    @app.errorhandler(ServiceError)
    def service_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(ValueError)
    def invalid_request(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
        return value

    def state():
        return {**service.snapshot(), "lease": service.lease.snapshot()}

    @app.get(prefix + "/")
    def panel():
        return render_template("index.html", base_path=prefix)

    @app.get(prefix + "/static/app.js")
    def panel_app():
        return send_file(assets / "app.js", mimetype="application/javascript")

    @app.get(prefix + "/static/styles.css")
    def panel_css():
        return send_file(assets / "styles.css", mimetype="text/css")

    @app.get(prefix + "/api/panel-info")
    def panel_info():
        return jsonify(service.panel_info())

    def camera_arguments(camera):
        if camera not in ("microscope", "side"):
            raise ValueError("Unknown microscope camera")
        if set(request.args) - {"after_id"}:
            raise ValueError("Unexpected camera query parameter")
        if len(request.args.getlist("after_id")) > 1:
            raise ValueError("Only one frame cursor is allowed")
        value = request.args.get("after_id")
        if value is None:
            return None
        if not value.isascii() or not value.isdecimal() or len(value) > 19:
            raise ValueError("after_id must be a nonnegative integer")
        number = int(value)
        if number >= 2**63:
            raise ValueError("after_id exceeds the frame cursor limit")
        return number

    @app.get(prefix + "/api/camera/<camera>/frame.jpg")
    def camera_frame(camera):
        after_id = camera_arguments(camera)
        if request.method == "HEAD":
            return Response(mimetype="image/jpeg")
        frame = service.read_frame(camera, after_id=after_id, timeout=0.5)
        if frame is None:
            return Response(status=204)
        return Response(
            frame.jpeg,
            mimetype="image/jpeg",
            headers={
                "X-Frame-Id": str(frame.frame_id),
                "X-Captured-At": str(frame.captured_at),
            },
        )

    @app.get(prefix + "/api/camera/<camera>/mjpeg")
    def camera_stream(camera):
        after_id = camera_arguments(camera)
        if request.method == "HEAD":
            return Response(content_type="multipart/x-mixed-replace; boundary=frame")
        if not stream_slots.acquire(blocking=False):
            raise ServiceError(503, "Preview streams are busy; close another preview")
        closed = threading.Event()
        release_lock = threading.Lock()

        def release():
            with release_lock:
                if not closed.is_set():
                    closed.set()
                    stream_slots.release()

        try:
            first = service.read_frame(camera, after_id=after_id, timeout=0.5)
            if first is None:
                raise ServiceError(503, "No microscope frame is available")
        except BaseException:
            release()
            raise

        def frames():
            frame = first
            try:
                while not closed.is_set():
                    if frame is not None:
                        yield (
                            b"--frame\r\nContent-Type: image/jpeg\r\n"
                            + f"Content-Length: {len(frame.jpeg)}\r\n".encode("ascii")
                            + f"X-Frame-Id: {frame.frame_id}\r\n".encode("ascii")
                            + f"X-Captured-At: {frame.captured_at}\r\n\r\n".encode("ascii")
                            + frame.jpeg
                            + b"\r\n"
                        )
                        cursor = frame.frame_id
                    if closed.wait(0.1):
                        break
                    frame = service.read_frame(camera, after_id=cursor, timeout=0.5)
            except (ServiceError, RuntimeError):
                # Headers are already sent. End this view; never retry devices.
                return
            finally:
                release()

        try:
            response = Response(frames(), content_type="multipart/x-mixed-replace; boundary=frame")
            # A response can close before its generator ever runs.
            response.call_on_close(release)
        except BaseException:
            release()
            raise
        return response

    @app.post(prefix + "/api/manual/<operation>")
    def manual_lease(operation):
        if body():
            raise ValueError("Manual lease requests take an empty object.")
        client = request.headers["X-MG400-Client"]
        if operation == "heartbeat":
            value = service.manual_heartbeat(client)
        elif operation == "renew":
            value = service.manual_heartbeat(client, renew_only=True)
        elif operation == "release":
            value = service.manual_release(client)
        else:
            raise ValueError("Unknown manual lease operation.")
        return jsonify(lease=value)

    @app.post(prefix + "/api/manual/set_focus_zero")
    def set_focus_zero():
        args = body()
        if set(args) != {"confirm"} or args["confirm"] is not True:
            raise ValueError("Focus zero requires confirm=true from the operator.")
        command_id = service.manual_command(
            "set_focus_zero", args, request.headers["X-MG400-Client"]
        )
        return jsonify(command_id=command_id, state=state()), 202

    return app
