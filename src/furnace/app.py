@app.get("/")
# Render the single-page camera and furnace control interface.
def index():
    return render_template("index.html")


@app.get("/api/status")
# Return one combined snapshot of camera and furnace state.
def status():
    return jsonify({
        "camera": camera_manager.status(),
        "furnace": furnace_service.status(),
    })


@app.post("/api/furnace/connect")
# Connect the furnace service using the fixed port from furnace.py.
def connect_furnace():
    try:
        furnace_service.connect(port=PORT)
        return jsonify({"ok": True, "status": furnace_service.status()})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.post("/api/furnace/apply")
# Validate manual controls and send a target temperature and ramp rate.
def apply_furnace():
    try:
        data = request.get_json(silent=True) or {}
        result = furnace_service.apply_setpoint(
            data.get("temperature_c"), data.get("ramp_rate_c_per_min")
        )
        return jsonify({"ok": True, "status": result})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/furnace/stop")
# Stop furnace output outside a running sequence.
def stop_furnace():
    try:
        return jsonify({"ok": True, "status": furnace_service.stop_furnace()})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.post("/api/flow/connect")
def connect_flow_controller():
    try:
        return jsonify({
            "ok": True,
            "status": furnace_service.connect_flow_controller(port=FLOW_PORT),
        })
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.post("/api/flow/gas/valve")
def set_gas_valve():
    try:
        data = request.get_json(silent=True) or {}
        opened = data.get("open")
        if not isinstance(opened, bool):
            raise ValueError("open must be true or false")
        return jsonify({
            "ok": True,
            "status": furnace_service.set_gas_valve(
                data.get("channel_index"), opened
            ),
        })
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.post("/api/flow/gas/set")
def set_gas_flow():
    try:
        data = request.get_json(silent=True) or {}
        return jsonify({
            "ok": True,
            "status": furnace_service.set_gas_flow(
                data.get("channel_index"),
                data.get("flow_percent"),
            ),
        })
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.post("/api/flow/gas/stop")
def stop_gas_flow():
    try:
        data = request.get_json(silent=True) or {}
        return jsonify({
            "ok": True,
            "status": furnace_service.stop_gas_flow(data.get("channel_index")),
        })
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.post("/api/flow/stop-all")
def stop_all_flow():
    try:
        return jsonify({"ok": True, "status": furnace_service.stop_all_flow()})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.put("/api/sequence")
# Validate and store an edited sequence, including edits made while running.
def update_sequence():
    try:
        data = request.get_json(silent=True) or {}
        return jsonify({"ok": True, "status": furnace_service.set_sequence(data.get("steps"))})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/sequence/start")
# Optionally start sequence-owned recording, then start the furnace program.
def start_sequence():
    global _sequence_owns_recording
    data = request.get_json(silent=True) or {}
    record_camera = bool(data.get("record_camera", False))
    settings = normalize_recording_settings(data)
    recording_mode = settings["mode"]
    capture_interval_seconds = settings["capture_interval_seconds"]
    output_fps = settings["output_fps"]
    minimum_free_gb = settings["minimum_free_gb"]
    started_recording = False
    try:
        if record_camera:
            if camera_manager.camera.is_recording:
                return jsonify({"ok": False, "message": "Camera is already recording"}), 409
            path, run_directory = create_run(
                data.get("run_name"), "temperature_sequence",
                {"sequence_steps": data.get("steps", [])},
            )
            start_camera_recording(
                path,
                mode=recording_mode,
                capture_interval_seconds=capture_interval_seconds,
                output_fps=output_fps,
                minimum_free_gb=minimum_free_gb,
            )
            started_recording = True
            with _sequence_recording_lock:
                _sequence_owns_recording = True
        result = furnace_service.start_sequence(data.get("steps"))
        return jsonify({"ok": True, "status": result})
    except Exception as exc:
        if started_recording and camera_manager.camera.is_recording:
            camera_manager.camera.stop_recording()
        with _sequence_recording_lock:
            _sequence_owns_recording = False
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/sequence/stop")
# Stop the active sequence and its associated recording, if any.
def stop_sequence():
    try:
        return jsonify({"ok": True, "status": furnace_service.stop_sequence()})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500

