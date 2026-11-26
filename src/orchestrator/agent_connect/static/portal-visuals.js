(function (root) {
  "use strict";
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const number = (value, unit = "") => finite(value) ? `${Number(value.toFixed(3))}${unit ? " " + unit : ""}` : "Unknown";
  const text = value => value === null || value === undefined ? "Unknown" : typeof value === "boolean" ? value ? "Yes" : "No" : String(value);
  const labels = { stage: "Stage", robot: "Robot arm", furnace: "Furnace", thermal: "Thermal camera", microscope: "Microscope", vision: "Vision" };
  function stageAxes(device, elapsed = 0) {
    const axes = device?.values?.axes || [];
    return [ ["top", "X"], ["bottom", "Y"], ["insert", "Insert"] ].map(([id, label]) => {
      const source = axes.find(axis => axis.id === id) || {};
      const native = device?.cached_state?.values?.motors?.find(axis => axis.id === id) || {};
      const lowerSet = source.lower_limit_set ?? native.lower_limit_set, upperSet = source.upper_limit_set ?? native.upper_limit_set;
      const min = lowerSet === false ? null : source.lower_limit_rotations, max = upperSet === false ? null : source.upper_limit_rotations, value = source.position_rotations;
      const calibration = source.calibration ?? native.calibration, fault = source.fault ?? native.fault;
      const faulted = Boolean(fault && fault !== "none");
      const unit = source.unit, rotationUnit = unit === "rotation";
      const bounds = rotationUnit && finite(min) && finite(max) && max > min;
      const recent = age => finite(age) && age >= 0 && age + elapsed <= 3;
      const fresh = device?.quality === "valid" && device?.values?.connected === true && recent(device.age_s) && recent(source.telemetry_age_s) && recent(source.encoder_age_s);
      const verified = source.quality === "valid" && source.reference_valid === true;
      const unverified = source.quality === "unknown" && source.reference_valid == null && source.motion_ready === true;
      const inside = bounds && finite(value) && value >= min && value <= max;
      const position = fresh && !faulted && (verified || unverified) && inside ? (value - min) / (max - min) : null;
      const quality = !Object.keys(source).length ? "Not received" : !rotationUnit ? "Unsupported unit" : faulted ? `Fault: ${typeof fault === "object" ? fault.message || fault.code || "Reported" : fault}` : source.reference_valid === false || source.quality === "uncalibrated" ? "Not calibrated" : !fresh || !(verified || unverified) ? "Outdated or unknown" : !finite(value) ? "Position unknown" : !bounds ? "Limits unavailable" : !inside ? "Outside limits" : unverified ? "Reference unverified" : "Current";
      const rangeStatus = lowerSet === false && upperSet === false ? "Limits not set" : lowerSet === false ? "Minimum not set" : upperSet === false ? "Maximum not set" : !bounds ? "Limits unavailable" : "";
      const calibrationStatus = ({ none: "Not calibrated", lower: "Calibrating: minimum", upper: "Calibrating: maximum", center: "Calibrating: center", done: "Calibration complete", cancelled: "Calibration cancelled", failed: "Calibration failed" })[calibration];
      const status = [...new Set([!fresh ? "Outdated reading" : "", rangeStatus, calibrationStatus, quality === "Current" || quality === rangeStatus || quality === "Limits unavailable" && rangeStatus ? "" : quality].filter(Boolean))].join(" · ") || "Current";
      return { id, label, value, unit, min, max, bounds, fresh, unverified, position, quality, status };
    });
  }
  function observationQuality(observation, elapsed = 0) {
    if (!observation || observation.available !== true) return "Unavailable";
    if (observation.quality !== "valid") return observation.quality === "stale" ? "Outdated" : "Unverified";
    return finite(observation.cache_age_s) && observation.cache_age_s + elapsed <= 3 ? "Current" : "Outdated";
  }
  function motorPower(value) {
    if (value?.sleeping === true) return "Asleep";
    const enabled = value?.enabled === true ? "Enabled" : value?.enabled === false ? "Released" : "Unknown";
    return value?.sleeping === false ? enabled : enabled === "Unknown" ? "Unknown" : enabled + " · Sleep not reported";
  }
  function observedTime(value) {
    if (!finite(value)) return "Not received";
    const date = new Date(value * 1000);
    return finite(date.getTime()) ? date.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC") : "Unknown";
  }
  function summary(name, device = {}, elapsed = 0) {
    const value = device.values || {}, cached = device.cached_state?.values || {}, observation = device.observations || {};
    if (name === "robot") {
      const pose = value.pose_mm_deg;
      return [["X", number(Array.isArray(pose) ? pose[0] : pose?.x, "mm")], ["Y", number(Array.isArray(pose) ? pose[1] : pose?.y, "mm")], ["Z", number(Array.isArray(pose) ? pose[2] : pose?.z, "mm")], ["R", number(Array.isArray(pose) ? pose[3] : pose?.r, "°")], ["Enabled", text(value.enabled)], ["Suction", text(value.vacuum?.suck)]];
    }
    if (name === "furnace") return [["Measured", number(value.pv_c, "°C")], ["Setpoint", number(value.sp_c, "°C")], ["Ramp", number(value.ramp_c_per_min, "°C/min")], ["Run state", text(value.run_state)], ["Sequence", text(value.sequence?.phase)], ["Gas controller", cached.flow?.connected === true ? "Connected" : cached.flow?.connected === false ? "Disconnected" : "Unknown"]];
    if (name === "thermal") return [["Mean", number(observation.stats?.mean_celsius, "°C")], ["Minimum", number(observation.stats?.min?.celsius, "°C")], ["Maximum", number(observation.stats?.max?.celsius, "°C")], ["Center", number(observation.stats?.center?.celsius, "°C")], ["Frame", text(observation.frame_sequence)], ["Statistics", observationQuality(observation, elapsed)]];
    if (name === "microscope") {
      const profile = value.turret_profile, visual = value.visual_focus, power = value.motor_power, focus = value.autofocus_run, settings = value.autofocus_settings;
      const readiness = profile?.ready === true ? "Ready" : profile?.needs_height_calibration === true ? "Needs 5x initial reference" : profile?.ready === false ? "Not ready" : "Unknown";
      return [["Objective", text(value.objective_id)], ["Turret profile", text(profile?.name || profile?.id || (profile?.id === null ? "Not selected" : null))], ["Profile status", readiness],
        ["Visual phase", typeof visual?.phase === "string" ? visual.phase.replaceAll("_", " ") : "Unknown"], ["Last observed Y", number(visual?.y_px, "px")], ["Target Y", number(visual?.target_y_px, "px")], ["Last visual observation", observedTime(visual?.observed_at)],
        ["Autofocus phase", typeof focus?.phase === "string" ? focus.phase.replaceAll("_", " ") : "Unknown"], ["Focus verified", text(focus?.verified)], ["Focus score", number(focus?.score)], ["Focus result", text(focus?.error)],
        ["Active search half-range", number(focus?.search_half_range_pulses, "pulses")], ["Total movement budget", number(visual?.travel_limit_pulses, "pulses")], ["Movement used", number(visual?.travel_used_pulses, "pulses")],
        ["5x search half-range", number(settings?.five_x_half_range_pulses, "pulses")], ["10x search half-range", number(settings?.ten_x_half_range_pulses, "pulses")], ["20x search half-range", number(settings?.twenty_x_half_range_pulses, "pulses")], ["50x search half-range", number(settings?.fifty_x_half_range_pulses, "pulses")],
        ["Turret power", motorPower(power?.objective)], ["Focus power", motorPower(power?.focus)], ["Relative focus count", number(value.focus_position_pulses, "pulses")], ["Manual/coarse travel budget", number(value.focus_travel_limit_pulses, "pulses")], ["Busy", text(value.busy)], ["Recording", text(value.recording?.status)], ["Frames", number(value.recording?.frame_count)]];
    }
    if (name === "vision") return [["Model loaded", text(value.model_loaded)], ["Camera", text(observation.camera ?? value.last_result?.camera)], ["Result", text(observation.status)], ["Observations", observationQuality(observation, elapsed)], ["Items", number(observation.count)], ["Coordinates", text(observation.coordinate_system)]];
    return [];
  }
  function node(tag, content, className) { const item = document.createElement(tag); if (content !== undefined) item.textContent = content; if (className) item.className = className; return item; }
  function svgNode(tag, attributes) { const item = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const [key, value] of Object.entries(attributes)) item.setAttribute(key, value); return item; }
  function metrics(items) {
    const result = node("dl", undefined, "visual-metrics");
    for (const [label, value] of items) { const entry = node("div"); entry.append(node("dt", label), node("dd", value)); result.append(entry); }
    return result;
  }
  function stageTable(axes) {
    const wrapper = node("div", undefined, "stage-axis-table"), table = node("table"), head = node("tr"), body = node("tbody");
    for (const label of ["Axis", "Current (rot)", "Min (rot)", "Max (rot)", "Status"]) { const cell = node("th", label); cell.setAttribute("scope", "col"); head.append(cell); }
    const header = node("thead"); header.append(head); table.append(header, body);
    for (const axis of axes) {
      const row = node("tr"), name = node("th", axis.label); name.setAttribute("scope", "row"); row.append(name);
      for (const value of [axis.value, axis.min, axis.max]) row.append(node("td", axis.unit === "rotation" ? number(value) : "Unknown"));
      row.append(node("td", axis.status)); body.append(row);
    }
    wrapper.append(table); return wrapper;
  }
  function stageDiagram(axes) {
    const root = node("div", undefined, "stage-visual"), plot = svgNode("svg", { viewBox: "0 0 280 180", role: "img", "aria-label": "Stage X and Y in received rotation limits. Dashed markers have an unverified reference. No physical geometry is inferred." });
    plot.append(svgNode("rect", { x: 36, y: 12, width: 218, height: 130, rx: 3, class: "stage-range" }));
    const [x, y, insert] = axes;
    for (const [px, py, label, anchor] of [[145, 173, "X · rotations", "middle"], [36, 157, x.bounds ? number(x.min) : "?", "start"], [254, 157, x.bounds ? number(x.max) : "?", "end"], [29, 23, y.bounds ? number(y.max) : "?", "end"], [29, 141, y.bounds ? number(y.min) : "?", "end"]]) { const labelNode = svgNode("text", { x: px, y: py, "text-anchor": anchor }); labelNode.textContent = label; plot.append(labelNode); }
    const axisLabel = svgNode("text", { x: 14, y: 83, transform: "rotate(-90 14 83)", "text-anchor": "middle" }); axisLabel.textContent = "Y"; plot.append(axisLabel);
    if (x.position !== null && y.position !== null) {
      const cx = 36 + 218 * x.position, cy = 142 - 130 * y.position;
      plot.append(svgNode("line", { x1: cx, x2: cx, y1: 12, y2: 142, class: "stage-guide" }), svgNode("line", { x1: 36, x2: 254, y1: cy, y2: cy, class: "stage-guide" }), svgNode("circle", { cx, cy, r: 5, class: x.unverified || y.unverified ? "stage-point stage-point-unverified" : "stage-point" }));
    } else {
      const missing = svgNode("text", { x: 145, y: 78, "text-anchor": "middle" });
      const missingLimits = [x, y].filter(axis => !axis.bounds).map(axis => axis.label);
      missing.textContent = missingLimits.length && [x, y].every(axis => axis.unit === "rotation" && finite(axis.value)) ? `${missingLimits.join("/")} limits unavailable` : "XY position unavailable";
      plot.append(missing);
    }
    const rail = node("div", undefined, "stage-insert"), track = node("div", undefined, "axis-track");
    if (insert.position !== null) { const marker = node("span", undefined, insert.unverified ? "axis-marker axis-marker-unverified" : "axis-marker"); marker.style.left = `${insert.position * 100}%`; marker.title = insert.quality; track.append(marker); }
    rail.append(node("span", "Insert · rotations", "axis-label"), track, node("div", insert.bounds ? `${number(insert.min)} — ${number(insert.max)}` : "Calibrated limits unavailable", "axis-bounds"));
    root.append(plot, rail, metrics(axes.map(axis => [axis.label, axis.unit === "rotation" ? number(axis.value, "rot") : "Unknown"])), stageTable(axes), node("p", axes.filter(axis => axis.quality !== "Current").map(axis => `${axis.label}: ${axis.quality}`).join(" · ") || "Received position within calibrated limits", "visual-note")); return root;
  }
  function deviceVisual(name, device, elapsed = 0) {
    const result = node("div", undefined, "device-visual");
    if (name === "stage") return stageDiagram(stageAxes(device, elapsed));
    result.append(metrics(summary(name, device, elapsed)));
    const observation = device?.observations, cached = device?.cached_state?.values || {};
    if (name === "microscope") result.append(node("p", "Marker Y is the last visual observation, not continuous tracking. Image coordinates use the 3840 px reference width. Focus counts are relative to the current command; the travel budget is not a physical end stop.", "visual-note"));
    if (name === "furnace") {
      const channels = cached.flow?.gas_channels;
      for (const channel of Array.isArray(channels) ? channels.slice(0, 3) : []) {
        const connected = cached.flow?.connected === true && channel.available !== false;
        result.append(node("p", `${channel.gas_name || "Gas " + text(channel.channel_index)} · ${number(channel.actual_percent, "%")} actual / ${number(channel.target_percent, "%")} target${connected ? "" : " · Unverified readings"}${channel.setpoint_verified === true ? "" : " · Target unverified"}`, "gas-reading"));
      }
      if (!Array.isArray(channels) || !channels.length) result.append(node("p", "Gas readings not received", "visual-note"));
    }
    if (name === "vision") {
      const list = node("div", undefined, "observation-list");
      for (const item of (observation?.items || []).slice(0, 8)) {
        const label = item.text ?? item.label ?? item.id ?? item.kind ?? "Observation";
        const position = Array.isArray(item.center_xy_mm) ? item.center_xy_mm.map(v => number(v)).join(", ") + " mm" : Array.isArray(item.center_px) ? item.center_px.map(v => number(v)).join(", ") + " px" : "Position unknown";
        list.append(node("p", `${label} · ${position}`, "observation-chip"));
      }
      if (!list.children.length) list.append(node("p", observation?.available === true && observation.count === 0 ? "No observations in this result" : "No observations received", "visual-note"));
      if ((observation?.items?.length || 0) > 8) list.append(node("p", "More observations in the inspector", "visual-note"));
      result.append(list);
    }
    return result;
  }
  function targetPreview(action, params, stage, elapsed = 0) {
    if (action?.id !== "stage.position") return null;
    const axis = stage?.axes?.find(axis => axis.id === params?.motor);
    const fresh = stage?.fresh === true && stage?.available === true && stage?.connected === true && finite(stage.age_s) && stage.age_s + elapsed <= 3 && axis?.telemetry_fresh === true;
    const min = axis?.lower_limit_rotations, max = axis?.upper_limit_rotations, target = params?.position;
    const bounds = fresh && finite(min) && finite(max) && max > min;
    return { current: fresh ? axis?.position_rotations : null, target, min, max, position: bounds && finite(target) && target >= min && target <= max ? (target - min) / (max - min) : null, currentPosition: bounds && finite(axis.position_rotations) && axis.position_rotations >= min && axis.position_rotations <= max ? (axis.position_rotations - min) / (max - min) : null, label: ({top:"X",bottom:"Y",insert:"Insert"})[params?.motor] || "Axis" };
  }
  function renderTarget(container, preview) {
    container.hidden = !preview; if (!preview) return;
    const track = node("div", undefined, "axis-track");
    for (const [position, className] of [[preview.currentPosition, "axis-marker"], [preview.position, "axis-marker target-marker"]]) if (position !== null) { const marker = node("span", undefined, className); marker.style.left = `${position * 100}%`; track.append(marker); }
    container.replaceChildren(node("p", `${preview.label} · ${number(preview.current, "rot")} current → ${number(preview.target, "rot")} target`, "target-caption"), track, node("p", preview.position === null ? "Target is not plotted: current calibrated bounds are unavailable or the target is outside them." : `${number(preview.min)} — ${number(preview.max)} rotations · Outline marker: selected target`, "visual-note"));
  }
  const exported = { finite, number, labels, stageAxes, observationQuality, summary, deviceVisual, targetPreview, renderTarget };
  if (typeof module !== "undefined") module.exports = exported;
  root.AgentPortalVisuals = exported;
})(typeof window === "undefined" ? globalThis : window);
