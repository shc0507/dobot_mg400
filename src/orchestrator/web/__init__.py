"""Dashboard routes; instrument credentials stay in the service process."""

from flask import Blueprint, jsonify, render_template, request, send_from_directory

from labkit.lease import validate_client


def dashboard_blueprint(service, prefix):
    blueprint = Blueprint("dashboard", __name__, template_folder="templates", url_prefix=prefix)

    @blueprint.get("/")
    def index():
        return render_template("dashboard.html", base_path=prefix)

    @blueprint.get("/dashboard-static/dashboard.js")
    def javascript():
        return send_from_directory(blueprint.root_path + "/static", "dashboard.js")

    @blueprint.get("/dashboard-static/dashboard.css")
    def stylesheet():
        return send_from_directory(blueprint.root_path + "/static", "dashboard.css")

    @blueprint.get("/api/dashboard/state")
    def state():
        service.dashboard.refresh()
        return jsonify(service.dashboard.snapshot())

    @blueprint.get("/api/services")
    def services():
        service.dashboard.refresh()
        report = service.dashboard.snapshot()
        return jsonify(services=report["services"], cameras=report["cameras"])

    def safety(action):
        data = request.get_json()
        expected = {"confirmed": True} if action in {"heat", "gas"} else {}
        if data != expected or action in {"heat", "gas"} and data.get("confirmed") is not True:
            raise ValueError(
                "Confirm the stated effects inside the page."
                if expected
                else "Expected an empty object."
            )
        client = validate_client(request.headers.get("X-MG400-Client"))
        if action == "hold":
            with service.lease.control(client):
                operation, fresh = service.dashboard.reserve(action)
        else:
            operation, fresh = service.dashboard.reserve(action)
        if fresh:
            service.dashboard.dispatch(operation)
        key = "command_id" if action == "hold" else "operation_id"
        return jsonify({key: operation["id"]}), 202

    @blueprint.post("/api/dashboard/motion")
    def motion():
        return safety("motion")

    @blueprint.post("/api/dashboard/heat")
    def heat():
        return safety("heat")

    @blueprint.post("/api/dashboard/gas")
    def gas():
        return safety("gas")

    @blueprint.post("/api/dashboard/hold")
    def hold():
        return safety("hold")

    return blueprint
