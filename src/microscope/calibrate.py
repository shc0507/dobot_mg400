"""Pinned calibration worker source checkpoint; not yet adapted or executable."""

def remember_objective_reference(self):
    """Save the current raw camera view as the selected objective reference."""
    with self.autofocus_camera_lock:
        camera = self.autofocus_camera
        camera_ready = camera is not None and camera.is_open
    if not camera_ready:
        self.pending_objective_reference_save = True
        self.objective_calibration_status_var.set(
            "Opening camera to remember the current view..."
        )
        self.show_camera_preview(open_camera=True)
        return

    try:
        frame = camera.get_frame(wait=True, timeout=2.0, raw=True, copy=True)
        if frame is None:
            raise RuntimeError("No camera frame is available")
        path_text = self.objective_calibration_vars[
            "reference_path"
        ].get().strip()
        if not path_text:
            raise ValueError("Choose a reference image file.")
        path = Path(path_text).expanduser()
        if not path.suffix:
            path = path.with_suffix(".png")
            self.objective_calibration_vars["reference_path"].set(str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded_ok, encoded = cv2.imencode(path.suffix, frame)
        if not encoded_ok:
            raise RuntimeError(
                f"Unsupported reference image format: {path.suffix}"
            )
        encoded.tofile(str(path))
        resolved = path.resolve()
        self.objective_calibration_status_var.set(
            f"Reference saved: {resolved}"
        )
        self._append_log(f"Objective reference saved: {resolved}")
    except Exception as exc:
        self.objective_calibration_status_var.set(
            "Could not save objective reference"
        )
        messagebox.showerror("Save objective reference failed", str(exc))

def _light_objective_calibration_worker(
    self,
    settings,
    focus_settings,
    cancel_event,
):
    camera = None
    terminal_event = None
    try:
        reference_bytes = np.fromfile(
            settings["reference_path"], dtype=np.uint8
        )
        reference_frame = cv2.imdecode(reference_bytes, cv2.IMREAD_COLOR)
        if reference_frame is None:
            raise RuntimeError(
                f"Could not read reference image: "
                f"{settings['reference_path']}"
            )
        camera_config = (
            settings["camera_index"],
            settings["camera_width"],
            settings["camera_height"],
        )
        with self.autofocus_camera_lock:
            existing_camera = self.autofocus_camera
            reuse_camera = (
                existing_camera is not None
                and existing_camera.is_open
                and self.autofocus_camera_config == camera_config
            )
        if reuse_camera:
            camera = existing_camera
        else:
            self._close_autofocus_camera()
            camera = MicroscopeCamera(
                camera_index=settings["camera_index"],
                resolution=(
                    settings["camera_width"], settings["camera_height"]
                ),
                stabilization=False,
            )
            camera.open()
            with self.autofocus_camera_lock:
                self.autofocus_camera = camera
                self.autofocus_camera_config = camera_config
        self.autofocus_events.put(("light_camera_ready", None))

        calibrator = LightObjectiveCalibrator(
            camera,
            self.controller,
            reference_frame=reference_frame,
            reference_magnification=settings["reference_magnification"],
            current_magnification=settings["current_magnification"],
            speed=settings["speed"],
            move_settle_seconds=settings["move_settle"],
            minimum_score=settings["minimum_score"],
            minimum_uniqueness=settings["minimum_uniqueness"],
            minimum_overlap=settings["minimum_overlap"],
            center_tolerance_px=settings["center_tolerance"],
            region_fraction=settings["region_fraction"],
            neighbor_support_fraction=(
                settings["neighbor_support_fraction"]
            ),
            release_threshold_px=settings["release_threshold"],
            neighbor_consistency_px=settings["release_neighbor_px"],
            release_min_confidence=settings["release_min_confidence"],
            release_max_iterations=settings["release_max_iterations"],
            verification_repeats=settings["release_verify_repeats"],
            progress=lambda item: self.autofocus_events.put(
                ("light_progress", item)
            ),
            focus_progress=lambda item: self.autofocus_events.put(
                ("light_focus_progress", item)
            ),
            before_move=lambda: self._wait_before_objective_move(
                cancel_event
            ),
            cancel_event=cancel_event,
        )
        result = calibrator.run(
            half_range_full_steps=settings["half_range"],
            coarse_microstep=settings["coarse_microstep"],
            fine_microstep=settings["fine_microstep"],
            fine_step_pulses=settings["fine_step_pulses"],
            focus_half_range=focus_settings["local_half_range"],
            focus_step=focus_settings["local_step"],
            focus_fine_radius=focus_settings["local_fine_radius"],
            focus_min_step=focus_settings["local_min_step"],
            focus_speed=focus_settings["speed"],
            focus_settle_seconds=focus_settings["precision_settle"],
            focus_frames=focus_settings["precision_frames"],
            focus_frame_interval=focus_settings["frame_interval"],
            focus_roi_fraction=focus_settings["roi"],
            focus_backlash_steps=focus_settings["backlash"],
            lock_exposure=settings["lock_exposure"],
        )
        output = save_light_objective_measurements(
            settings["output"], calibrator.measurements
        ).resolve()
        terminal_event = ("light_complete", (result, output))
    except (LightObjectiveCalibrationCancelled,
            ObjectiveCalibrationCancelled):
        terminal_event = ("light_cancelled", None)
    except Exception as exc:
        terminal_event = ("light_error", str(exc))
    finally:
        if self.controller.connected:
            try:
                self.controller.objective_stop()
                self.controller.objective_disable()
                self.controller.focus_stop()
                self.controller.focus_disable()
            except Exception:
                pass
        with self.autofocus_camera_lock:
            camera_is_shared = self.autofocus_camera is camera
        if camera is not None and not camera_is_shared:
            try:
                camera.close()
            except Exception:
                pass
        if terminal_event is not None:
            self.autofocus_events.put(terminal_event)

def _objective_calibration_worker(self, settings, cancel_event):
    camera = None
    terminal_event = None
    try:
        reference_bytes = np.fromfile(
            settings["reference_path"], dtype=np.uint8
        )
        reference_frame = cv2.imdecode(reference_bytes, cv2.IMREAD_COLOR)
        if reference_frame is None:
            raise RuntimeError(
                f"Could not read reference image: {settings['reference_path']}"
            )
        camera_config = (
            settings["camera_index"],
            settings["camera_width"],
            settings["camera_height"],
        )
        with self.autofocus_camera_lock:
            existing_camera = self.autofocus_camera
            reuse_camera = (
                existing_camera is not None
                and existing_camera.is_open
                and self.autofocus_camera_config == camera_config
            )
        if reuse_camera:
            camera = existing_camera
        else:
            self._close_autofocus_camera()
            camera = MicroscopeCamera(
                camera_index=settings["camera_index"],
                resolution=(
                    settings["camera_width"], settings["camera_height"]
                ),
                stabilization=False,
            )
            camera.open()
            with self.autofocus_camera_lock:
                self.autofocus_camera = camera
                self.autofocus_camera_config = camera_config
        self.autofocus_events.put(("objective_camera_ready", None))

        calibrator = ObjectiveCalibrator(
            camera,
            self.controller,
            reference_frame=reference_frame,
            reference_threshold=settings["reference_threshold"],
            speed=settings["speed"],
            move_settle_seconds=settings["move_settle"],
            precision_settle_seconds=settings["precision_settle"],
            enable_settle_seconds=settings["enable_settle"],
            release_settle_seconds=settings["release_settle"],
            roi_fraction=settings["roi"],
            min_confidence=settings["min_confidence"],
            min_overlap=settings["min_overlap"],
            max_valid_displacement=settings["max_valid_shift"],
            peak_threshold=settings["peak_threshold"],
            stable_threshold=settings["stable_threshold"],
            neighbor_consistency_tolerance_px=(
                settings["neighbor_consistency_px"]
            ),
            atlas_merge_distance_px=settings["atlas_merge_px"],
            post_peak_quiet_distance_px=(
                settings["post_peak_quiet_px"]
            ),
            cancel_event=cancel_event,
            progress=lambda item: self.autofocus_events.put(
                ("objective_progress", item)
            ),
            position_progress=lambda item: self.autofocus_events.put(
                ("objective_position", item)
            ),
            before_move=lambda: self._wait_before_objective_move(
                cancel_event
            ),
        )
        result = calibrator.run(
            half_range=settings["half_range"],
            coarse_step=settings["coarse_step"],
            stagger_offset=settings["stagger_offset"],
            refine_radius=settings["refine_radius"],
            fine_step=settings["fine_step"],
            verification_repeats=settings["verify_repeats"],
            lock_exposure=settings["lock_exposure"],
        )
        output = save_objective_measurements(
            settings["output"], calibrator.measurements
        ).resolve()
        terminal_event = ("objective_complete", (result, output))
    except ObjectiveCalibrationCancelled:
        terminal_event = ("objective_cancelled", None)
    except Exception as exc:
        terminal_event = ("objective_error", str(exc))
    finally:
        # Keep this final hardware guard even though ObjectiveCalibrator
        # also disables in its finally block.
        if self.controller.connected:
            try:
                self.controller.objective_stop()
            except Exception:
                pass
            try:
                self.controller.objective_disable()
            except Exception:
                pass
        with self.autofocus_camera_lock:
            camera_is_shared = self.autofocus_camera is camera
        if camera is not None and not camera_is_shared:
            try:
                camera.close()
            except Exception:
                pass
        if terminal_event is not None:
            self.autofocus_events.put(terminal_event)
