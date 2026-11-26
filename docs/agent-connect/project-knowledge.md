# Project knowledge for external agents

## Read before integration

The human must explicitly instruct the coding agent to read this guide, then [communication-contract.md](communication-contract.md).
The agent must adapt its own client code and runtime before execution.
The human supplies the project objective, physical targets, and intended experiment.

During adaptation, put the relevant operating rules into the external agent's runtime instructions.
A separate runtime model does not inherit the coding conversation. Supply current state separately from this operating context.

Check adaptation with simulated adapters and live read-only checks.
Physical tests are not part of client adaptation.
During the current supervised trial, Mark chooses and sends the first physical commands through the local portal.

## System and device roles

The project coordinates chip handling, positioning, heating, imaging, and records.
Olares hosts services that own instrument connections and enforce native behavior.
The Lab Console combines workspaces, state, commands, and run records.
The main Console uses port 8080. Agent Connect development uses the test Console on port 8082 through LarePass VPN.

External agents run as separate clients on another PC or Olares.
Their runtimes read published state and request supported actions through Agent Connect.
Agent Portal provides a reference interface.
GUI behavior does not define the communication contract.

- **Robot:** The Dobot MG400 moves chips with vacuum suction. Motion, suction, and evidence of chip possession are different claims.
- **Stage:** Encoder-controlled motors move Top (X), Bottom (Y), and Insert. Insert carries the crucible toward or away from the furnace.
- **Cameras and vision:** Cameras supply bench images. Vision detects chips, reads identifiers, and supports calibrated observations. Detection alone establishes neither identity nor robot targets.
- **Thermal camera:** Thermal images and named regions provide temperature observations. These readings differ from the furnace controller temperature.
- **Microscope and Toupcam:** Focus and turret motors position optics above the furnace window. Toupcam supplies microscope images and recordings through the microscope service.
- **Furnace and gas:** The furnace service controls heating, ramp rates, sequences, and fixed gas channels. Gas connection and measured flow can remain unavailable independently of furnace temperature.

## Chips, records, and evidence

A chip is the sample under study.
A tray contains addressed slots for chips. A crucible holds a chip and travels on the stage.
A recipe defines ordered operations. A run records recipe execution, steps, chip identities, measurements, events, and artifacts.
Artifacts include images, recordings, and other outputs.
Project recipe support does not imply Agent Connect recipe access.

Preserve exact chip identifiers, including case, spaces, and punctuation.
The chip passport shows saved history and current evidence.
History does not establish current location.
Empty history does not prove that no physical work occurred.

Current evidence distinguishes `observed`, `reported`, `inferred`, and `unknown` claims.
Command acceptance does not prove arrival. A handoff can leave the holder unknown while retaining the last known location.
Conflicting evidence makes the disputed claim unknown. Old images cannot establish a current location.
Observation, record update, and Console frame times differ.
Keep missing readings unknown, including disconnected gas controllers and unverified flow.

Holder and furnace entry are separate claims.
Furnace entry defaults to unknown (`inside_furnace: null`).
Entry evidence requires a valid mapping, reference, alignment, and stage holder.
Simulated mappings do not establish live locations. Keep simulated results separate from physical readings.

## Coordinates and calibration

Robot X/Y/Z use millimetres. Robot R uses degrees.
Camera pixels, robot flange poses, tool-tip poses, and stage coordinates require their own references and calibration.
A screen position or image center is not automatically a physical target.

Stage positions use measured shaft rotations. X means Top, Y means Bottom, and Insert is a separate axis.
Absolute motion requires the axis calibration, valid bounds, and fresh healthy encoder feedback.
After a board restart or zero reset, establish calibration again before absolute motion.
A saved profile does not restore the axis reference.
Do not convert pulse or millimetre values to rotations without a measured mapping.

Sample Transfer uses encoder coordinates and saved profiles:

- **Extract Sample** moves only Insert to `0`. **Insert Sample** moves only Insert to its current firmware upper limit. Neither action needs a saved profile. Both require calibrated Insert feedback and valid bounds.
- **Move Out Stage** and **Move to Microscope** use saved X/Y targets. Each moves Bottom, then Top, and leaves Insert unchanged. These actions do not require Insert calibration or extraction.
- **Move to position** uses a saved named position. It moves Bottom, then Top, then Insert, without an extra extraction move. Only 1 axis moves at a time.

Profile selection causes no motion. Sample Transfer implements neither chip recognition nor robot transfer.
Position Finder (legacy) uses separate image calibration and sends no motion commands.
Its measurements do not drive Motion or Sample Transfer.
The 3D Twin shows device state without commands. Its stage geometry has no calibrated mapping from encoder rotations.

## Microscope objectives and focus

A human creates the microscope calibration in the Console's **Calibration** subsection.
The profile stores each lens's marker X alignment and 1 separate initial 5x image.
The human manually focuses 5x on the sample before recording this initial image. Both motors stay released during calibration.
Only the initial 5x marker Y guides coarse height positioning. Other lenses have no saved Y target.
If 5x is already near its recorded height, the service skips coarse movement and starts the requested autofocus scan.
Autofocus uses microscope-image sharpness, not marker Y movement.
Keep the camera mount, view, and marker mounting consistent with the profile.
The human must establish that the chosen switches at the current focus height clear the furnace. Image sharpness does not measure physical clearance.

Agents can select a saved profile with `turret.select`.
Selection causes no movement. Agents cannot create, capture, edit, upload, or delete turret profiles.
An old profile requires the new 5x initial record before this workflow. Existing profile files are preserved.
Do not send `microscope.set_focus_zero` or `microscope.home_focus`; these are not Agent Connect actions.

Use `microscope.select_objective` to select any lens in any order, without prior autofocus or a fresh sharpness check.
Selection aligns saved X at the current focus height. It commands no focus movement or automatic autofocus.
The operator is responsible for physical clearance and selection order. Run `microscope.autofocus` separately when needed.
For a new sample, the optional `microscope.autofocus_sequence` starts with 5x and runs 5x, 10x, 20x, then 50x.
It advances only after verified autofocus. Failure, cancellation, or lost control prevents further steps.
The sequence requires 5x at the start. If another lens is seated, follow the available separate controls or request human setup.
The service verifies final sharpness and permits at most 1 bounded refinement per autofocus scan.
A verified sharpness peak does not identify which sample surface or feature produced that peak.

`microscope.rough_focus_5x` moves only the seated 5x lens near its initial image height. It does not establish verified focus.
The former `microscope.focus_to_saved_height` and `microscope.focus_to_clearance` actions are unavailable.
`microscope.focus_move` accepts signed quarter-step pulse counts. These are not stage rotations or millimetres.
The existing objective nudge, camera, recording, and Stop actions remain available through the reviewed catalog.

A human or an authorized agent sets the manual/coarse travel budget and separate autofocus half-ranges for 5x, 10x, 20x and 50x in Operate.
Defaults are 20000 quarter-step pulses for manual/coarse travel, a 5000-pulse half-range for 5x, 10x and 20x, and a 3000-pulse half-range for 50x. Saved settings override these defaults.
A 5000-pulse half-range searches up to 5000 pulses in each direction from the scan center.
Each lens uses its own saved range in both separate autofocus and the combined sequence. Use `microscope.autofocus` with `{"mode":"local"}` to match the single Console Autofocus button. The native service retains full mode for older clients; Agent Connect does not expose it.
Autofocus has a separate cumulative travel budget derived from its search range. A combined sequence shares 1 budget across all its steps.
The status panel separates Search range from Total movement budget. Unused movement allowance does not mean the scan missed part of its search range.
These limits count commanded pulses. They are not physical distances or mechanical end stops.
Agents can read `focus_travel_limit_pulses` and `autofocus_settings`. Use `microscope.set_focus_travel_limit` or `microscope.set_autofocus_settings` to change these operating settings while the microscope is idle. These actions save settings without moving equipment. Calibration editing remains human-only.
If a command exhausts its budget, report the result. Do not repeat commands to bypass the limit.

Focus releases after an operation. Firmware v2 also disables idle focus after 30 seconds and sleeps each idle driver after 120 seconds.
Read requests do not prevent sleep. The controller wakes the required motor automatically.
After release, sleep, or reset, pulse counts do not establish current physical height.
The next operation uses fresh images and its own relative pulse reference. Objective selection does not require a previous autofocus result.
Read `autofocus_run` for the current lens, scan phase, score, verified result, completed lenses, and failure reason.
Its `search_half_range_pulses` is the frozen range for the active or last scan. Saved settings apply to the next command.
An accepted command or a stationary motor does not establish successful focus.
Missing markers, a changed camera view, or incomplete calibration can block movement.
Ask the human to correct these conditions. Do not bypass the profile or manufacture a zero reference.

## Capabilities and control

Read the current command catalog before selecting actions.
A Console button or service feature does not imply agent access.
The catalog provides availability reasons, parameters, units, and limits.
Use supported native handlers. Agent Connect provides no arbitrary shell, device-command bypass, URL dispatch, or recipe dispatch.

Connection starts an observer session. Normal commands require an explicit operator grant.
Connecting, reconnecting, and granting control start no equipment.
Only 1 agent operator controls the 8082 Console at a time.

This authority does not lock 8080 or direct device interfaces.
Mark coordinates access so nobody operates equipment through those interfaces during agent control.
Device leases permit only 1 controller per device.

Preserve robot speed and alarm checks, configured stage limits, microscope focus travel budgets, and turret clearance requirements.
The 8082 Console enables furnace commands within native temperature and ramp caps and fixed gas channels.
Command completion does not imply thermal sequence completion.
Stop and STOP ALL remain available without an operator grant or device control.
Preserve cancellation handling. Do not automatically repeat a physical command after an unknown result.

The trial uses warning-only handling for agent heartbeat loss.
The Console revokes the grant, cancels further agent work, releases its device leases, and shows the sender in a warning.
The furnace retains its native sequence, recovery, and orphan behavior.
Connection loss adds no automatic furnace shutdown or new cooling or gas policy. Explicit furnace Stop remains available.
