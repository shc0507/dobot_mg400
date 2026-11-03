def bounded_target(ids, source, direction):
    """Experimental order is a bounded line, never a circular turret route."""
    if source not in ids or direction not in (-1, 0, 1):
        raise ValueError(f'Invalid objective source/direction: {source}, {direction}')
    index = ids.index(source) + direction
    if not 0 <= index < len(ids):
        raise ValueError(f'Objective boundary: cannot move {direction:+d} from ID {source}')
    return ids[index]


def preparation_direction(ids, current, target):
    if current not in ids or target not in ids:
        raise ValueError(f'Objective outside experiment order: {current} -> {target}')
    return (ids.index(target) > ids.index(current)) - (ids.index(target) < ids.index(current))


def _run(app, kind, ids, settings, micro, config, switch_settings, cancel, events):
    root = Path(__file__).resolve().parent / 'experiment_logs' / f'{kind}_{time.time_ns()}'
    root.mkdir(parents=True)
    (root/'settings.json').write_text(json.dumps(dict(kind=kind, objective_order=ids,
        focus=settings, focus_microstep=micro, side_camera=config,
        switch=vars(switch_settings) if switch_settings else None), indent=2))
    microscope = side = None
    started = time.monotonic()
    current = None
    rows = []
    def check():
        if cancel.is_set() or app.closing:
            raise RuntimeError('Experiment stopped')
    def command(text, prefix, timeout=10):
        check()
        return app.controller.send_and_wait(text, prefix, timeout=timeout, cancel_event=cancel)
    def report(**row):
        rows.append(row)
        keys = sorted(set().union(*(r.keys() for r in rows)))
        with (root/'summary.csv').open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader(); writer.writerows(rows)
        events.put(('progress', str(row)))
    def focus(local, label):
        command(f'MICRO {micro}', f'OK MICRO {micro}')
        f = AutoFocuser(microscope, app.controller, speed=settings['speed'],
            settle_seconds=settings['settle'], frames_per_position=settings['frames'],
            precision_settle_seconds=settings['precision_settle'],
            precision_frames_per_position=settings['precision_frames'],
            frame_interval=settings['frame_interval'], roi_fraction=settings['roi'],
            backlash_steps=settings['backlash'], cancel_event=cancel)
        t = time.monotonic()
        try:
            if local:
                best = f.run_local(half_range=settings['local_half_range'], step=settings['local_step'],
                    fine_radius=settings['local_fine_radius'], min_step=settings['local_min_step'],
                    lock_exposure=settings['lock_exposure'])
            else:
                best = f.run(half_range=settings['half_range'], coarse_step=settings['coarse_step'],
                    fine_step=settings['fine_step'], ultrafine_step=settings['ultrafine_step'],
                    lock_exposure=settings['lock_exposure'])
        finally:
            save_measurements(root/f'{label}_curve.csv', f.measurements)
        import cv2
        packet = microscope.get_frame_packet(wait=True, raw=True, copy=True)
        if packet:
            cv2.imwrite(str(root/f'{label}.png'), packet[2])
        return f, best, time.monotonic()-t
    def sw(source, direction, label):
        nonlocal current
        check()
        expected = bounded_target(ids, source, direction) if source is not None else None
        if source is None and direction:
            raise ValueError('Identify current objective before moving')
        obj = ArucoObjectiveSwitcher(side, app.controller, settings=switch_settings,
            cancel_event=cancel, before_move=lambda: app._wait_before_objective_move(cancel))
        t = time.monotonic()
        result = (obj.run(direction, root/f'{label}.csv', source_id=source) if direction else
                  obj.calibrate_current(root/f'{label}.csv', marker_id=source))
        current = result.target_id
        if current not in ids or (expected is not None and current != expected):
            raise RuntimeError(f'Unexpected objective ID {current}; expected {expected}')
        return result, time.monotonic()-t
    def goto(target):
        nonlocal current
        if current is None:
            result, _ = sw(None, 0, 'initial_identify')
            current = result.target_id
        while current != target:
            source = current
            direction = preparation_direction(ids, current, target)
            _, elapsed = sw(current, direction, f'prepare_{len(list(root.glob("*.csv")))}')
            report(stage='preparation',source=source,target=current,
                   seconds=elapsed,status='completed')
        sw(target, 0, f'reference_{len(list(root.glob("*.csv")))}')
    try:
        if settings:
            microscope = shared_camera(app, 'microscope',
                (settings['camera_index'], settings['camera_width'], settings['camera_height']))
        if config:
            side = shared_camera(app, 'side', config)
        if kind == 'focus':
            # Stable reference is established once. Every trial returns to it
            # using the measured final relative position before adding offset.
            f, best, elapsed = focus(True, 'reference')
            report(stage='reference', score=best.score, seconds=elapsed)
            for offset in (-2000, -1000, 1000, 2000):
                check()
                command(f'MICRO {micro}', f'OK MICRO {micro}')
                app.controller.focus_speed(settings['speed'])
                app.controller.focus_enable()
                f.position = 0
                f._move_to(offset, precision=True)
                f, best, elapsed = focus(False, f'offset_{offset}')
                report(stage='autofocus', offset=offset, final_error_steps=offset+best.position,
                       score=best.score, seconds=elapsed, status='completed')
                f._move_to(-offset, precision=True)
        elif kind == 'switch':
            for a,b in list(zip(ids, ids[1:])) + list(zip(ids[:0:-1], ids[-2::-1])):
                direction = 1 if ids.index(b)>ids.index(a) else -1
                for offset in (0,-50,50,-100,100):
                    if (a,b,offset) in getattr(app, 'skip_switch_trials', set()):
                        continue
                    goto(a)
                    if offset:
                        command('MICRO 1', 'OK MICRO 1')
                        command(f'OBJ SPEED {switch_settings.speed}', 'OK OBJ SPEED')
                        command('OBJ ENABLE', 'OK OBJ ENABLED')
                        if cancel.wait(switch_settings.enable_settle_seconds): check()
                        command(f'OBJ MOVE {offset}', 'DONE OBJ POS ',
                                max(10,abs(offset)/switch_settings.speed+5))
                        command('OBJ DISABLE', 'OK OBJ DISABLED')
                        if cancel.wait(switch_settings.release_settle_seconds): check()
                    result, elapsed = sw(a,direction,f'{a}_to_{b}_offset_{offset}')
                    report(stage='switch', source=a,target=b,offset_full_steps=offset,
                           dx_px=result.dx_px,dy_px=result.dy_px,seconds=elapsed,status='completed')
        else:
            goto(ids[0])
            sequence_start = time.monotonic()
            for i, identity in enumerate(ids):
                switching = 0
                if i:
                    _, switching = sw(ids[i-1],1,f'combined_switch_{identity}')
                _, best, elapsed = focus(True,f'combined_focus_{identity}')
                report(stage='combined',target=identity,switch_seconds=switching,
                       focus_seconds=elapsed,sequence_seconds=time.monotonic()-sequence_start,
                       score=best.score,status='completed')
        events.put(('progress', f'Completed; elapsed {time.monotonic()-started:.2f}s; {root}'))
    except Exception as exc:
        report(stage='terminal',status='stopped' if cancel.is_set() else 'failed',
               error=str(exc),seconds=time.monotonic()-started)
    finally:
        for text,prefix in [('STOP ALL','OK STOP ALL'),('OBJ DISABLE','OK OBJ DISABLED'),
                            ('FOCUS DISABLE','OK FOCUS DISABLED')]:
            try:
                app.controller.send_and_wait(text,prefix,timeout=3)
            except Exception as exc:
                events.put(('progress', f'Cleanup warning: {exc}'))
        # Cameras remain registered to the GUI. The Tk completion handler
        # closes a feed only if its preview was closed while the test ran.
        events.put(('done', f'Experiment ended. Results: {root}'))
