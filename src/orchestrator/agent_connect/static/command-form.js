(function (root) {
  "use strict";
  const copy = value => JSON.parse(JSON.stringify(value));
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value || {}, key);
  function schemaFor(schema, value) {
    if (!schema?.oneOf) return schema || {};
    return schema.oneOf.find(option => Object.keys(value || {}).every(key => own(option.properties, key)) && (option.required || []).every(key => own(value, key))) || schema.oneOf[0];
  }
  function validate(value, schema, path = "Parameters") {
    if (schema?.oneOf) {
      const matches = schema.oneOf.filter(option => !validate(value, option, path));
      return matches.length === 1 ? "" : `${path}: choose one supported coordinate group.`;
    }
    const type = schema?.type;
    if (type === "object") {
      if (!value || Array.isArray(value) || typeof value !== "object") return `${path}: use a JSON object.`;
      for (const key of schema.required || []) if (!own(value, key)) return `${path}.${key} is required.`;
      for (const [key, item] of Object.entries(value)) {
        if (!own(schema.properties, key)) return `${path}.${key} is not a supported field.`;
        const error = validate(item, schema.properties[key], `${path}.${key}`); if (error) return error;
      }
    } else if (type === "array") {
      if (!Array.isArray(value)) return `${path}: use a JSON array.`;
      if (value.length < (schema.minItems || 0) || value.length > (schema.maxItems ?? Infinity)) return `${path}: check the number of entries.`;
      for (const [index, item] of value.entries()) { const error = validate(item, schema.items, `${path}[${index}]`); if (error) return error; }
    } else if (type === "number" || type === "integer") {
      if (typeof value !== "number" || !Number.isFinite(value) || (type === "integer" && !Number.isInteger(value))) return `${path}: enter a finite ${type}.`;
      if (schema.minimum !== undefined && value < schema.minimum || schema.maximum !== undefined && value > schema.maximum) return `${path}: use a value within the displayed limits.`;
      if (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum || schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum) return `${path}: use a value within the displayed limits.`;
    } else if (typeof value !== type) return `${path}: use a ${type}.`;
    if (schema?.enum && !schema.enum.some(item => item === value)) return `${path}: choose a listed value.`;
    if (type === "string") {
      if (value.length < (schema.minLength || 0) || value.length > (schema.maxLength ?? Infinity)) return `${path}: check the text length.`;
      if (schema.pattern && !new RegExp(schema.pattern).test(value)) return `${path}: check the required format.`;
    }
    return "";
  }
  function labelFor(action, path) {
    const key = path.split(".").pop();
    const robot = action?.device === "robot";
    if (action?.device === "furnace") {
      const labels = { temperature_c: "Temperature (°C)", end_gas_until_c: "End gas temperature (°C)", ramp_rate_c_per_min: "Ramp (°C/min)", flow_percent: "Gas flow (%)", channel_index: "Gas channel (0–2)", open: "Open valve", hold_minutes: "Hold (minutes)", steps: "Sequence steps (JSON)", "0": "Gas channel 0 (%)", "1": "Gas channel 1 (%)", "2": "Gas channel 2 (%)" };
      if (labels[key]) return labels[key];
    }
    if (key === "id" && action?.id === "microscope.select_objective") return "Objective ID";
    if (key === "step" && action?.id === "robot.jog") return "Step (mm for X/Y/Z; degrees for R)";
    if (key === "axis") return "Axis";
    if (key === "speed") return robot ? "Speed (%)" : "Speed (rotations/s)";
    if (["position", "rotations"].includes(key)) return key === "position" ? "Position (rotations)" : "Travel (rotations)";
    if (["x", "y", "z", "r", "insert"].includes(key)) return `${key === "insert" ? "Insert" : key.toUpperCase()} (${robot ? key === "r" ? "degrees" : "mm" : "rotations"})`;
    return ({ motor: "Axis", id: "Saved profile", objective_id: "Objective ID", target: "Destination", name: "Name", direction: "Direction", profile: "Profile JSON", microstep: "Microstepping", new: "Start a new draft", enabled: "Enabled", recording_id: "Recording ID", run_name: "Recording name", capture_interval_seconds: "Capture interval (seconds)", output_fps: "Output frames/s", steps: "Steps", step: "Step", mode: "Mode", port: "Port", operation: "Operation", path: "Save path" })[key] || key.replaceAll("_", " ");
  }
  function optionLabel(value) {
    return ({ bottom: "Bottom (Y)", top: "Top (X)", insert: "Insert", cw: "Decrease (cw)", ccw: "Increase (ccw)", out_stage: "Move Out Stage", microscope: "Move to Microscope", true: "Yes", false: "No" })[String(value)] || String(value);
  }
  function stagePositionView(stage, motor, position, elapsed = 0) {
    const axis = stage?.axes?.find(axis => axis.id === motor);
    const finite = value => typeof value === "number" && Number.isFinite(value);
    const fresh = Boolean(stage?.available === true && stage?.fresh === true && stage?.phase === "ready" && stage?.connected === true && finite(stage.age_s) && stage.age_s >= 0 && stage.age_s + elapsed <= 3);
    const bounds = Boolean(axis && finite(axis.lower_limit_rotations) && finite(axis.upper_limit_rotations) && axis.lower_limit_rotations <= axis.upper_limit_rotations);
    const error = !fresh ? stage?.reason || "Fresh stage limits are unavailable. Refresh the device state." : !axis ? "Choose an axis with current stage data." : axis.telemetry_fresh !== true ? axis.reason || "The axis feedback is outdated or unavailable." : !bounds ? axis.reason || "Calibrated limits are unavailable for this axis." : axis.available !== true || axis.positioning_ready !== true ? axis.reason || "Absolute positioning is unavailable for this axis." : finite(position) && (position < axis.lower_limit_rotations || position > axis.upper_limit_rotations) ? `Position must be between ${axis.lower_limit_rotations} and ${axis.upper_limit_rotations} rotations.` : "";
    return { axis, fresh, bounds, error, current: axis?.telemetry_fresh === true && finite(axis.position_rotations) ? axis.position_rotations : null };
  }
  class ParameterModel {
    constructor(action = null) { this.setAction(action); }
    setAction(action) { this.action = action; this.value = copy(action?.device === "furnace" ? {} : action?.defaults || {}); this.text = JSON.stringify(this.value, null, 2); this.parseError = ""; this.stage = null; this.stageAt = null; }
    setConstraints(stage, receivedAt = Date.now()) { this.stage = stage || null; this.stageAt = receivedAt; }
    get stageView() { return stagePositionView(this.stage, this.value?.motor, this.value?.position, this.stageAt == null ? Infinity : Math.max(0, Date.now() - this.stageAt) / 1000); }
    setJSON(text) {
      this.text = text;
      try { this.value = JSON.parse(text); this.parseError = ""; }
      catch { this.parseError = "Parameters must contain valid JSON."; }
      return this.error;
    }
    set(path, value) {
      if (!this.value || Array.isArray(this.value) || typeof this.value !== "object") this.value = {};
      let target = this.value; const keys = path.split(".");
      for (const key of keys.slice(0, -1)) { if (!target[key] || Array.isArray(target[key]) || typeof target[key] !== "object") target[key] = {}; target = target[key]; }
      if (value === undefined) delete target[keys.at(-1)]; else target[keys.at(-1)] = value;
      this.parseError = ""; this.text = JSON.stringify(this.value, null, 2);
    }
    get error() { return this.parseError || (this.action ? validate(this.value, this.action.params) || (this.action.id === "stage.position" ? this.stageView.error : "") : "Choose an available action."); }
    getValue() { if (this.error) throw new Error(this.error); return copy(this.value); }
  }
  class ParameterForm {
    constructor(container, jsonInput, changed = () => {}) {
      this.container = container; this.jsonInput = jsonInput; this.changed = changed; this.model = new ParameterModel(); this.choices = {}; this.generation = 0;
      jsonInput.addEventListener("input", () => { this.model.setJSON(jsonInput.value); this.render(); this.changed(); });
    }
    setAction(action, choices = {}) { this.generation++; this.choices = choices; this.model.setAction(action); this.jsonInput.value = this.model.text; this.render(); this.changed(); }
    setChoices(choices = {}) {
      if (JSON.stringify(choices) === JSON.stringify(this.choices) && !this.choicesPending) return;
      this.choices = choices;
      if (this.model.action?.id === "stage.position") this.refreshStageContext();
      else if (this.container.contains?.(document.activeElement)) this.choicesPending = true;
      else { this.choicesPending = false; this.render(); }
    }
    setConstraints(stage, receivedAt) { this.model.setConstraints(stage, receivedAt); this.refreshStageContext(); }
    refreshStageContext() {
      if (this.model.action?.id !== "stage.position") return;
      const view = this.model.stageView;
      if (this.positionInput) {
        if (view.fresh && view.bounds) { this.positionInput.min = String(view.axis.lower_limit_rotations); this.positionInput.max = String(view.axis.upper_limit_rotations); }
        else { this.positionInput.removeAttribute("min"); this.positionInput.removeAttribute("max"); }
      }
      if (this.stageNote) this.stageNote.textContent = [
        view.fresh && view.current !== null ? `Current: ${view.current} rotations` : "Current position: unavailable",
        view.fresh && view.bounds ? `Calibrated limits: ${view.axis.lower_limit_rotations} to ${view.axis.upper_limit_rotations} rotations` : "Calibrated limits: unavailable",
        view.error,
      ].filter(Boolean).join(" · ");
      for (const [id, option] of this.stageOptions || []) {
        const candidate = stagePositionView(this.model.stage, id, undefined, this.model.stageAt == null ? Infinity : Math.max(0, Date.now() - this.model.stageAt) / 1000);
        option.textContent = optionLabel(id) + (candidate.error ? " · Unavailable" : "");
        option.title = candidate.error;
      }
    }
    update(path, value, redraw = false) {
      this.model.set(path, value); this.jsonInput.value = this.model.text;
      if (redraw) this.render(); else this.refreshStageContext(); this.changed();
    }
    getValue() { return this.model.getValue(); }
    node(tag, text, className) { const item = document.createElement(tag); if (text) item.textContent = text; if (className) item.className = className; return item; }
    field(schema, path, value, required) {
      const row = this.node("div", "", "command-field");
      const stagePosition = this.model.action?.id === "stage.position";
      if (schema.oneOf) {
        const select = this.node("select"); select.id = "parameter-" + path; select.dataset.parameter = path + ".group";
        const current = schemaFor(schema, value);
        schema.oneOf.forEach((option, index) => { const item = this.node("option", Object.keys(option.properties || {}).map(key => key.toUpperCase()).join(" / ")); item.value = String(index); select.append(item); });
        select.value = String(schema.oneOf.indexOf(current));
        const label = this.node("label", labelFor(this.model.action, path)); label.htmlFor = select.id;
        select.onchange = () => { const next = schema.oneOf[Number(select.value)]; this.update(path, Object.fromEntries(Object.keys(next.properties || {}).map(key => [key, 0])), true); };
        row.append(label, select, this.object(current, path, value)); return row;
      }
      if (schema.type === "object") return this.object(schema, path, value);
      const label = this.node("label", (schema.title || labelFor(this.model.action, path)) + (required ? " *" : " (optional)"));
      const dynamic = this.choices[path], values = Array.isArray(dynamic) ? dynamic : schema.enum?.map(value => ({ value, label: optionLabel(value) }));
      let input;
      if (schema.type === "array") {
        input = this.node("textarea"); input.rows = 6; input.spellcheck = false;
        input.value = value === undefined ? "" : JSON.stringify(value, null, 2);
        input.oninput = () => {
          try { const parsed = input.value.trim() ? JSON.parse(input.value) : undefined; this.update(path, parsed); }
          catch { this.update(path, input.value); this.model.parseError = `${labelFor(this.model.action, path)}: enter valid JSON.`; this.changed(); }
        };
      } else if (values || schema.type === "boolean") {
        input = this.node("select");
        const options = values || [{ value: true, label: "Yes" }, { value: false, label: "No" }];
        const empty = this.node("option", values?.length === 0 ? "No saved choices" : required ? "Choose…" : "Use default"); empty.value = ""; input.append(empty);
        options.forEach((item, index) => { const option = this.node("option", item.label); option.value = String(index); input.append(option); if (stagePosition && path === "motor") this.stageOptions.set(item.value, option); });
        const index = options.findIndex(item => item.value === value);
        if (value !== undefined && index < 0) { const missing = this.node("option", "Current JSON value: " + String(value)); missing.value = "current"; input.append(missing); input.value = "current"; }
        else input.value = index < 0 ? "" : String(index);
        input.onchange = () => { if (input.value !== "current") this.update(path, input.value === "" ? undefined : options[Number(input.value)].value); };
      } else if (schema.type === "number" || schema.type === "integer") {
        input = this.node("input"); input.type = "number"; input.step = schema.type === "integer" ? "1" : "any";
        if (schema.minimum !== undefined && !(stagePosition && path === "position")) input.min = schema.minimum;
        if (schema.maximum !== undefined && !(stagePosition && path === "position")) input.max = schema.maximum;
        input.value = typeof value === "number" ? value : "";
        input.oninput = () => this.update(path, input.value === "" ? undefined : Number(input.value));
      } else {
        input = this.node(path === "profile" ? "textarea" : "input"); if (input.tagName === "INPUT") input.type = "text"; else input.rows = 4;
        if (schema.maxLength !== undefined) input.maxLength = schema.maxLength;
        if (schema.pattern) input.pattern = schema.pattern;
        input.value = typeof value === "string" ? value : "";
        input.oninput = () => this.update(path, input.value === "" ? undefined : input.value);
      }
      input.id = "parameter-" + path; input.dataset.parameter = path; input.required = required; label.htmlFor = input.id;
      row.append(label, input);
      if (stagePosition && path === "position") {
        this.positionInput = input;
        this.stageNote = this.node("small", "", "section-note"); this.stageNote.id = "stage-position-context"; this.stageNote.setAttribute("role", "status"); row.append(this.stageNote);
        return row;
      }
      if (path === "profile") {
        const file = this.node("input"); file.type = "file"; file.accept = ".json,application/json"; file.setAttribute("aria-label", "Choose a calibration profile JSON file");
        file.onchange = async () => { const selected = file.files?.[0], generation = this.generation; if (!selected) return; if (selected.size > (schema.maxLength || 2097152)) { this.model.parseError = "The profile file exceeds the displayed size limit."; this.changed(); return; } try { const value = await selected.text(); if (generation === this.generation) this.update(path, value, true); } catch { if (generation === this.generation) { this.model.parseError = "The profile file could not be read."; this.changed(); } } };
        row.append(file);
      }
      const limits = [schema.description, schema.minimum === undefined ? null : `Minimum: ${schema.minimum}`, schema.maximum === undefined ? null : `Maximum: ${schema.maximum}`, schema.maxLength === undefined ? null : `Maximum text length: ${schema.maxLength}`].filter(Boolean);
      if (limits.length) row.append(this.node("small", limits.join(" · "), "section-note"));
      return row;
    }
    object(schema, path = "", value = {}) {
      const group = this.node("div", "", "command-fields");
      for (const [key, definition] of Object.entries(schema.properties || {})) group.append(this.field(definition, path ? `${path}.${key}` : key, value?.[key], (schema.required || []).includes(key)));
      return group;
    }
    render() {
      const action = this.model.action;
      this.positionInput = null; this.stageNote = null; this.stageOptions = new Map();
      this.container.replaceChildren();
      if (!action) { this.container.append(this.node("p", "No available commands for this source.", "section-note")); return; }
      if (!Object.keys(action.params?.properties || {}).length) this.container.append(this.node("p", "No parameters needed. This action uses the current Console settings.", "section-note"));
      else this.container.append(this.object(action.params, "", this.model.value));
      this.refreshStageContext();
    }
  }
  const exports = { schemaFor, validate, labelFor, optionLabel, stagePositionView, ParameterModel, ParameterForm };
  if (typeof module !== "undefined") module.exports = exports;
  root.AgentCommandForm = exports;
})(typeof window === "undefined" ? globalThis : window);
