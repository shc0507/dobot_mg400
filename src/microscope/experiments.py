"""Single-pass microscope benchmarks for an explicitly supervised terminal session.

Importing this module opens no devices. Algorithms receive only the supervised
session's guarded motor and exclusively owned cameras.
"""

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict

import cv2

from .aruco_switch import OBJECTIVE_ORDER, ArucoObjectiveSwitcher, SwitchSettings
from .autofocus import AutoFocuser, save_measurements
from .paths import directory, private_open, run_path


def bounded_target(ids, source, direction):
    """Experimental order is a bounded line, never a circular turret route."""
    if source not in ids or direction not in (-1, 0, 1):
        raise ValueError(f"Invalid objective source/direction: {source}, {direction}")
    index = ids.index(source) + direction
    if not 0 <= index < len(ids):
        raise ValueError(f"Objective boundary: cannot move {direction:+d} from ID {source}")
    return ids[index]


def preparation_direction(ids, current, target):
    if current not in ids or target not in ids:
        raise ValueError(f"Objective outside experiment order: {current} -> {target}")
    return (ids.index(target) > ids.index(current)) - (ids.index(target) < ids.index(current))


def run_trials(
    kind,
    session,
    *,
    focus_settings,
    focus_microstep=1,
    switch_settings=None,
    ids=OBJECTIVE_ORDER,
    focus_start=None,
    output=None,
    progress=print,
):
    """Preserve the pinned trial order and offsets within a supervised session.

    focus_start is an explicit MICRO 4 coordinate, not a replacement zero. The
    session converts it exactly to the active divisor and checks measured limits.
    """
    from .calibrate import validated_focus_settings

    ids = validate_order(ids)
    if kind not in ("focus", "switch", "combined"):
        raise ValueError("Unknown microscope experiment")
    if type(focus_microstep) is not int or focus_microstep not in (1, 2, 4, 8, 16):
        raise ValueError("Focus microstep must be 1, 2, 4, 8 or 16")
    settings = validated_focus_settings(focus_settings) if kind != "switch" else None
    if settings is not None and type(focus_start) is not int:
        raise ValueError("Focus experiments require an explicit MICRO 4 starting coordinate")
    if kind == "focus" and settings["half_range"] < 2000:
        raise ValueError("Autofocus half range must be at least 2000 steps")
    if kind != "focus" and switch_settings is None:
        switch_settings = SwitchSettings()
    session.check()
    root = directory(
        run_path(
            output if output is not None else f"microscope/experiments/{kind}_{time.time_ns()}"
        ),
        exclusive=True,
    )
    with private_open(root / "settings.json", "x") as stream:
        json.dump(
            dict(
                kind=kind,
                objective_order=ids,
                focus=settings,
                focus_microstep=focus_microstep,
                focus_start_micro4=focus_start,
                switch=asdict(switch_settings) if switch_settings else None,
            ),
            stream,
            indent=2,
        )
    microscope, side, motor = session.camera, session.side, session.motor
    cancel, micro = session.cancel_event, focus_microstep
    started = time.monotonic()
    current = None
    rows = []

    def check():
        session.check()

    def command(text, prefix, timeout=10):
        check()
        return motor.send_and_wait(text, prefix, timeout=timeout, cancel_event=cancel)

    def report(**row):
        rows.append(row)
        keys = sorted(set().union(*(r.keys() for r in rows)))
        with private_open(root / "summary.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
        progress(str(row))

    def focus(local, label):
        command(f"MICRO {micro}", f"OK MICRO {micro}")
        if kind == "combined":
            session.move_focus_to(focus_start)
        f = AutoFocuser(
            microscope,
            motor,
            speed=settings["speed"],
            settle_seconds=settings["settle"],
            frames_per_position=settings["frames"],
            precision_settle_seconds=settings["precision_settle"],
            precision_frames_per_position=settings["precision_frames"],
            frame_interval=settings["frame_interval"],
            roi_fraction=settings["roi"],
            backlash_steps=settings["backlash"],
            cancel_event=cancel,
        )
        t = time.monotonic()
        try:
            if local:
                best = f.run_local(
                    half_range=settings["local_half_range"],
                    step=settings["local_step"],
                    fine_radius=settings["local_fine_radius"],
                    min_step=settings["local_min_step"],
                    lock_exposure=settings["lock_exposure"],
                )
            else:
                best = f.run(
                    half_range=settings["half_range"],
                    coarse_step=settings["coarse_step"],
                    fine_step=settings["fine_step"],
                    ultrafine_step=settings["ultrafine_step"],
                    lock_exposure=settings["lock_exposure"],
                )
        finally:
            save_measurements(root / f"{label}_curve.csv", f.measurements)
        check()
        packet = microscope.get_frame_packet(wait=True, raw=True, copy=True)
        check()
        if packet is None:
            raise RuntimeError("No final microscope frame is available")
        ok, encoded = cv2.imencode(".png", packet[2])
        if not ok:
            raise RuntimeError("Experiment image could not be encoded")
        with private_open(root / f"{label}.png", "xb") as stream:
            stream.write(encoded.tobytes())
        return f, best, time.monotonic() - t

    def sw(source, direction, label):
        nonlocal current
        check()
        expected = bounded_target(ids, source, direction) if source is not None else None
        if source is None and direction:
            raise ValueError("Identify current objective before moving")
        obj = ArucoObjectiveSwitcher(
            side,
            motor,
            settings=switch_settings,
            cancel_event=cancel,
            before_move=session.move_to_clearance,
        )
        t = time.monotonic()
        result = (
            obj.run(direction, root / f"{label}.csv", source_id=source)
            if direction
            else obj.calibrate_current(root / f"{label}.csv", marker_id=source)
        )
        check()
        current = result.target_id
        if current not in ids or (expected is not None and current != expected):
            raise RuntimeError(f"Unexpected objective ID {current}; expected {expected}")
        return result, time.monotonic() - t

    def goto(target):
        nonlocal current
        if current is None:
            result, _ = sw(None, 0, "initial_identify")
            current = result.target_id
        while current != target:
            source = current
            direction = preparation_direction(ids, current, target)
            _, elapsed = sw(current, direction, f"prepare_{len(list(root.glob('*.csv')))}")
            report(
                stage="preparation",
                source=source,
                target=current,
                seconds=elapsed,
                status="completed",
            )
        sw(target, 0, f"reference_{len(list(root.glob('*.csv')))}")

    try:
        if kind == "focus":
            command(f"MICRO {micro}", f"OK MICRO {micro}")
            session.move_focus_to(focus_start)
            # Stable reference is established once. Every trial returns to it
            # using the measured final relative position before adding offset.
            f, best, elapsed = focus(True, "reference")
            report(stage="reference", score=best.score, seconds=elapsed)
            for offset in (-2000, -1000, 1000, 2000):
                check()
                command(f"MICRO {micro}", f"OK MICRO {micro}")
                motor.focus_speed(settings["speed"])
                motor.focus_enable()
                f.position = 0
                f._move_to(offset, precision=True)
                f, best, elapsed = focus(False, f"offset_{offset}")
                report(
                    stage="autofocus",
                    offset=offset,
                    final_error_steps=offset + best.position,
                    score=best.score,
                    seconds=elapsed,
                    status="completed",
                )
                f._move_to(-offset, precision=True)
        elif kind == "switch":
            for a, b in list(zip(ids, ids[1:])) + list(zip(ids[:0:-1], ids[-2::-1])):
                direction = 1 if ids.index(b) > ids.index(a) else -1
                for offset in (0, -50, 50, -100, 100):
                    goto(a)
                    if offset:
                        command("MICRO 1", "OK MICRO 1")
                        command(f"OBJ SPEED {switch_settings.speed}", "OK OBJ SPEED")
                        command("OBJ ENABLE", "OK OBJ ENABLED")
                        if cancel.wait(switch_settings.enable_settle_seconds):
                            check()
                        command(
                            f"OBJ MOVE {offset}",
                            "DONE OBJ POS ",
                            max(10, abs(offset) / switch_settings.speed + 5),
                        )
                        command("OBJ DISABLE", "OK OBJ DISABLED")
                        if cancel.wait(switch_settings.release_settle_seconds):
                            check()
                    result, elapsed = sw(a, direction, f"{a}_to_{b}_offset_{offset}")
                    report(
                        stage="switch",
                        source=a,
                        target=b,
                        offset_full_steps=offset,
                        dx_px=result.dx_px,
                        dy_px=result.dy_px,
                        seconds=elapsed,
                        status="completed",
                    )
        else:
            goto(ids[0])
            sequence_start = time.monotonic()
            for i, identity in enumerate(ids):
                switching = 0
                if i:
                    _, switching = sw(ids[i - 1], 1, f"combined_switch_{identity}")
                _, best, elapsed = focus(True, f"combined_focus_{identity}")
                report(
                    stage="combined",
                    target=identity,
                    switch_seconds=switching,
                    focus_seconds=elapsed,
                    sequence_seconds=time.monotonic() - sequence_start,
                    score=best.score,
                    status="completed",
                )
        check()
        elapsed = time.monotonic() - started
        progress(f"Completed; elapsed {elapsed:.2f}s; {root}")
        return {"directory": root, "rows": rows, "elapsed_seconds": elapsed}
    except BaseException as exc:
        first_failure = getattr(session, "failure", None)
        cancelled = (
            isinstance(exc, KeyboardInterrupt)
            or getattr(session, "operator_stopped", False)
            or (cancel.is_set() and first_failure is None)
        )
        try:
            session.abort(exc)
        except Exception as cleanup:
            progress(f"Cleanup was not confirmed: {cleanup}")
        report(
            stage="terminal",
            status="stopped" if cancelled else "failed",
            error=first_failure or str(exc),
            seconds=time.monotonic() - started,
        )
        raise


def validate_order(ids):
    ids = tuple(ids)
    if (
        len(ids) < 2
        or len(set(ids)) != len(ids)
        or any(type(identity) is not int or identity not in OBJECTIVE_ORDER for identity in ids)
    ):
        raise ValueError(f"Use distinct configured objective IDs: {OBJECTIVE_ORDER}")
    if any(
        (OBJECTIVE_ORDER.index(b) - OBJECTIVE_ORDER.index(a)) % len(OBJECTIVE_ORDER) != 1
        for a, b in zip(ids, ids[1:])
    ):
        raise ValueError("Order must follow adjacent positive-direction turret positions")
    return ids


def main(argv=None):
    from .calibrate import load_settings, supervised_session, validated_focus_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("focus", "switch", "combined"))
    parser.add_argument("--settings", help="TOML file with focus and switch settings")
    parser.add_argument("--ids", default=",".join(map(str, OBJECTIVE_ORDER)))
    parser.add_argument("--focus-microstep", type=int, choices=(1, 2, 4, 8, 16), default=1)
    parser.add_argument("--focus-start", type=int, help="Measured focus start in MICRO 4 pulses")
    parser.add_argument("--output", help="New directory beneath MG400_ORCH_RUNS_DIR")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.settings)
        if set(settings) - {"focus", "switch"}:
            raise ValueError("Experiment settings contain an unknown section")
        focus = validated_focus_settings(settings.get("focus")) if args.kind != "switch" else None
        switch = SwitchSettings(**settings.get("switch", {})) if args.kind != "focus" else None
        ids = validate_order(tuple(int(value.strip()) for value in args.ids.split(",")))
        if args.kind != "switch" and args.focus_start is None:
            raise ValueError("--focus-start is required for focus and combined trials")
        if args.kind == "focus" and focus["half_range"] < 2000:
            raise ValueError("Autofocus half range must be at least 2000 steps")
        if args.output is not None:
            run_path(args.output)
        resolution = (focus["camera_width"], focus["camera_height"]) if focus else (2592, 1944)
        with supervised_session(need_side=args.kind != "focus", resolution=resolution) as session:
            run_trials(
                args.kind,
                session,
                focus_settings=focus,
                focus_microstep=args.focus_microstep,
                switch_settings=switch,
                ids=ids,
                focus_start=args.focus_start,
                output=args.output,
            )
    except KeyboardInterrupt:
        print("Microscope experiment stopped", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Microscope experiment failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
