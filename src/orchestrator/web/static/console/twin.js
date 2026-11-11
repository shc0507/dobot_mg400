/* Twin workspace: a 3D model of the bench that follows the devices.
 * The model (GET api/console/ws/twin/model, the twin-model asset) carries entities (userData.entity) and joints (userData.joint, joint_type,
 * joint_axis in Blender axes, joint_coef for the robot links). GET api/console/ws/twin/state returns the joint
 * values and signals; the page moves the joints and colours the entities. three.js r128 loads from jsDelivr. */
(function () {
  "use strict";
  const LabConsole = (typeof window !== "undefined" && window.LabConsole) || null;
  if (!LabConsole) return;

  const POLL_MS = 250;
  const CDN = "https://cdn.jsdelivr.net/npm/three@0.128.0/";
  const SCRIPTS = ["build/three.min.js", "examples/js/controls/OrbitControls.js", "examples/js/loaders/GLTFLoader.js",
    "examples/js/loaders/DRACOLoader.js"];
  const ENTITY_SIGNALS = { robot: "robot", stage: "stage", insert: "stage", furnace: "furnace" };

  const i18n = {
    en: {
      "twin.title": "Bench twin", "twin.loading": "Loading the model…", "twin.noModel": "The model could not be loaded: {why}",
      "twin.robot": "Robot", "twin.stage": "Stage", "twin.microscope": "Microscope", "twin.furnace": "Furnace",
      "twin.unavailable": "No data", "twin.stale": "stale", "twin.alarm": "ALARM", "twin.moving": "moving",
      "twin.enabled": "enabled", "twin.disabled": "disabled", "twin.uncalibrated": "count scale unknown (microstep changed)",
      "twin.objective": "objective {id}", "twin.noObjective": "objective unknown", "twin.focusUnknown": "focus not modelled yet",
      "twin.gasOn": "gas on", "twin.gasOff": "gas off", "twin.running": "running", "twin.stopped": "stopped",
      "twin.legend": "Model at the devices' reported state. See-through: no fresh data. Red: alarm. Blue: moving. Glow: furnace heat.",
    },
    zh: {
      "twin.title": "实时台面", "twin.loading": "正在加载台面…", "twin.noModel": "无法加载台面：{why}",
      "twin.robot": "机械臂", "twin.stage": "位移台", "twin.microscope": "显微镜", "twin.furnace": "加热炉",
      "twin.unavailable": "无数据", "twin.stale": "数据过期", "twin.alarm": "报警", "twin.moving": "运动中",
      "twin.enabled": "已使能", "twin.disabled": "未使能", "twin.uncalibrated": "计数对应未知（细分模式已改变）",
      "twin.objective": "物镜 {id}", "twin.noObjective": "物镜未知", "twin.focusUnknown": "调焦尚未建模",
      "twin.gasOn": "气阀开启", "twin.gasOff": "气阀关闭", "twin.running": "运行中", "twin.stopped": "已停止",
      "twin.legend": "台面显示设备报告的状态。灰色部件：无新数据。红色：报警。变色：运动中。加热炉按炉温变色。",
    },
  };

  // ---- pure logic (tested in tests/test_console_twin.mjs) ----
  /** Blender axis (x, y, z) -> glTF axis (x, z, -y). */
  function gltfAxis(a) { return [a[0], a[2], -a[1] || 0]; }
  /** Angle (deg) or travel (mm) of one joint node from the joint values; robot links mix J1..J4. */
  function nodeValue(user, values) {
    if (user.joint === "robot") return (user.joint_coef || []).reduce((s, c, k) => s + c * (values["J" + (k + 1)] ?? 0), 0);
    return values[user.joint] ?? 0;
  }
  /** Furnace glow: 0 below 50 C, 1 at 1000 C; colour from dark red to yellow-orange. */
  function furnaceGlow(c) {
    if (typeof c !== "number" || !(c > 50)) return { k: 0, rgb: [0, 0, 0] };
    const k = Math.min(1, (c - 50) / 950);
    return { k, rgb: [1, 0.12 + 0.55 * k, 0.02 + 0.15 * k] };
  }
  /** Look of one entity: "ghost" (no fresh data), "alarm", "moving" or "normal". */
  function entityMode(key, signals, devices) {
    const s = signals || {}, d = devices || {};
    if (ENTITY_SIGNALS[key] === "robot") return s.robot_alarm ? "alarm" : s.robot_stale ? "ghost" : s.robot_moving ? "moving" : "normal";
    if (ENTITY_SIGNALS[key] === "stage") return s.stage_stale || !s.stage_calibrated ? "ghost" : s.stage_moving ? "moving" : "normal";
    if (key === "furnace") return d.furnace && d.furnace.available ? "normal" : "ghost";
    if (key === "microscope") return d.microscope && d.microscope.available ? "normal" : "ghost";
    return "normal";
  }
  /** Text rows of the device list. */
  function rows(body, t) {
    const d = body?.devices || {}, j = body?.joints || {}, s = body?.signals || {};
    const num = (v, unit, digits = 1) => (typeof v === "number" ? v.toFixed(digits) + " " + unit : "–");
    const out = [];
    const r = d.robot || {};
    out.push({ key: "robot", title: t("twin.robot"), state: !r.available ? t("twin.unavailable") : s.robot_alarm ? t("twin.alarm") : s.robot_stale ? t("twin.stale") : r.enabled ? t("twin.enabled") : t("twin.disabled"),
      level: s.robot_alarm ? "bad" : s.robot_stale ? "off" : "ok",
      detail: ["J1", "J2", "J3", "J4"].map(k => `${k} ${num(j[k], "°")}`).join(" · ") });
    const st = d.stage || {};
    out.push({ key: "stage", title: t("twin.stage"), state: !st.available ? t("twin.unavailable") : !s.stage_calibrated ? t("twin.uncalibrated") : s.stage_moving ? t("twin.moving") : s.stage_stale ? t("twin.stale") : "",
      level: !st.available || s.stage_stale || !s.stage_calibrated ? "off" : s.stage_moving ? "info" : "ok",
      detail: `Y ${num(j.stage_bottom, "mm")} · X ${num(j.stage_top, "mm")} · insert ${num(j.insert, "mm")}` });
    const m = d.microscope || {};
    out.push({ key: "microscope", title: t("twin.microscope"), state: m.available ? "" : t("twin.unavailable"), level: m.available ? "ok" : "off",
      detail: (typeof m.objective_id === "number" ? t("twin.objective", { id: m.objective_id }) : t("twin.noObjective")) + " · " + t("twin.focusUnknown") });
    const f = d.furnace || {};
    out.push({ key: "furnace", title: t("twin.furnace"), state: f.available ? (s.furnace_running ? t("twin.running") : t("twin.stopped")) : t("twin.unavailable"),
      level: !f.available ? "off" : s.furnace_hot ? "warn" : "ok",
      detail: `${num(s.furnace_c, "°C")} · ${s.gas_on ? t("twin.gasOn") : t("twin.gasOff")}` });
    return out;
  }

  // ---- page ----
  const view = { api: null, root: null, n: null, timer: null, raf: 0, three: null, body: null, visible: false };

  function loadScripts() {
    if (view.scripts) return view.scripts;
    view.scripts = SCRIPTS.reduce((p, file) => p.then(() => new Promise((resolve, reject) => {
      if (file.endsWith("three.min.js") && window.THREE) return resolve();
      const tag = document.createElement("script");
      tag.src = CDN + file; tag.onload = resolve; tag.onerror = () => reject(new Error("cannot load " + file));
      document.head.appendChild(tag);
    })), Promise.resolve());
    return view.scripts;
  }

  function build() {
    const { el, t } = view.api;
    // The 3D stage (canvas and tags) is built once and kept across language changes; the rest is rebuilt.
    const old = view.n;
    const n = {};
    n.status = el("p", { class: "lc-muted", text: view.three?.joints.length ? "" : t("twin.loading") });
    n.list = el("div", { class: "tw-list" });
    n.canvas = old ? old.canvas : el("canvas", { class: "tw-canvas", "aria-label": t("twin.title") });
    n.tags = old ? old.tags : el("div", { class: "tw-tags" });
    n.stage = old ? old.stage : el("div", { class: "tw-stage" }, n.canvas, n.tags);
    view.root.replaceChildren(el("section", { class: "lc-card tw-card" },
      el("div", { class: "lc-card-head" }, el("h2", { text: t("twin.title") }), n.status),
      el("div", { class: "tw-body" }, n.stage, el("div", { class: "tw-side" }, n.list, el("p", { class: "lc-muted", text: t("twin.legend") })))));
    view.n = n;
    view.rowNodes = null;
  }

  function renderRows() {
    const { el, t } = view.api;
    const list = rows(view.body, t);
    if (!view.rowNodes) {
      view.rowNodes = list.map(() => {
        const r = { title: el("span", { class: "lc-strong" }), state: el("span", { class: "tw-state" }), detail: el("span", { class: "lc-muted tw-detail" }) };
        r.node = el("div", { class: "tw-row" }, el("div", { class: "tw-row-head" }, r.title, r.state), r.detail);
        view.n.list.appendChild(r.node);
        return r;
      });
    }
    list.forEach((row, i) => {
      const r = view.rowNodes[i];
      r.title.textContent = row.title; r.state.textContent = row.state; r.detail.textContent = row.detail;
      r.state.className = "tw-state tw-" + row.level;
    });
  }

  function setupThree() {
    const THREE = window.THREE;
    const canvas = view.n.canvas;
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputEncoding = THREE.sRGBEncoding;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0f1418);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 0.9));
    const sun = new THREE.DirectionalLight(0xffffff, 0.9); sun.position.set(-1.2, 2.4, -1.6); scene.add(sun);
    const camera = new THREE.PerspectiveCamera(36, 1, 0.02, 60);
    const P = (x, y, z) => new THREE.Vector3(x / 1000, z / 1000, -y / 1000);     // bench mm (Z up) -> glTF m (Y up)
    camera.position.copy(P(-620, -1250, 820));
    const controls = new THREE.OrbitControls(camera, canvas);
    controls.target.copy(P(140, 260, 170)); controls.enableDamping = true;
    view.three = { THREE, renderer, scene, camera, controls, P, joints: [], entities: {}, furnace: [] };
    const resize = () => {
      const w = view.n.stage.clientWidth, h = view.n.stage.clientHeight;
      if (w && h) { renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); }
    };
    new ResizeObserver(resize).observe(view.n.stage); resize();
  }

  function loadModel() {
    const { THREE } = view.three;
    const loader = new THREE.GLTFLoader();
    const draco = new THREE.DRACOLoader(); draco.setDecoderPath(CDN + "examples/js/libs/draco/");
    loader.setDRACOLoader(draco);
    loader.load(view.api.base + "/api/console/ws/twin/model", gltf => {
      const tw = view.three;
      tw.scene.add(gltf.scene);
      gltf.scene.traverse(o => {
        const u = o.userData || {};
        if (u.joint) tw.joints.push({ o, u, p: o.position.clone(), q: o.quaternion.clone(), a: new THREE.Vector3(...gltfAxis(u.joint_axis)).normalize() });
        if (u.entity) tw.entities[u.entity] = o;
      });
      // Own material copies per signalled entity, so a colour change does not touch other entities.
      tw.looks = {};
      for (const key of Object.keys(ENTITY_SIGNALS).concat(["microscope"])) {
        const root = tw.entities[key]; if (!root) continue;
        const mats = [];
        root.traverse(o => {
          if (!o.isMesh || (o !== root && o.userData.entity && o.userData.entity !== key)) return;
          let owner = o; while (owner && !owner.userData.entity) owner = owner.parent;
          if (owner !== root) return;
          o.material = o.material.clone();
          mats.push({ m: o.material, opacity: o.material.opacity, transparent: o.material.transparent, emissive: o.material.emissive.clone() });
        });
        tw.looks[key] = { mats, mode: "normal" };
      }
      view.n.status.textContent = "";
      apply();
    }, undefined, error => { view.n.status.textContent = view.api.t("twin.noModel", { why: String(error?.message || error) }); });
  }

  function setLook(key, mode, glow) {
    const look = view.three.looks?.[key]; if (!look) return;
    const { THREE } = view.three;
    for (const item of look.mats) {
      const ghost = mode === "ghost";
      item.m.transparent = ghost || item.transparent;
      item.m.opacity = ghost ? Math.min(item.opacity, 0.3) : item.opacity;
      item.m.depthWrite = !ghost;
      const e = mode === "alarm" ? new THREE.Color(0.55, 0.02, 0.02) : mode === "moving" ? new THREE.Color(0.05, 0.18, 0.45)
        : glow && glow.k > 0 ? new THREE.Color(...glow.rgb).multiplyScalar(0.25 + 0.75 * glow.k) : item.emissive;
      item.m.emissive.copy(e);
    }
    look.mode = mode;
  }

  function apply() {
    const tw = view.three; if (!tw || !tw.joints.length || !view.body) return;
    const values = view.body.joints || {};
    for (const j of tw.joints) {
      const v = nodeValue(j.u, values);
      if (j.u.joint_type === "revolute") j.o.quaternion.copy(j.q).multiply(new tw.THREE.Quaternion().setFromAxisAngle(j.a, v * Math.PI / 180));
      else j.o.position.copy(j.p).add(j.a.clone().applyQuaternion(j.q).multiplyScalar(v / 1000));
    }
    const s = view.body.signals || {};
    for (const key of Object.keys(tw.looks || {})) {
      setLook(key, entityMode(key, s, view.body.devices), key === "furnace" ? furnaceGlow(s.furnace_c) : null);
    }
  }

  function tags() {
    const tw = view.three, n = view.n; if (!tw || !view.body) return;
    const s = view.body.signals || {};
    const want = [];
    if (tw.entities.furnace && typeof s.furnace_c === "number") want.push({ key: "furnace", text: s.furnace_c.toFixed(0) + " °C", level: s.furnace_hot ? "warn" : "ok" });
    if (tw.entities.robot && s.robot_alarm) want.push({ key: "robot", text: view.api.t("twin.alarm"), level: "bad" });
    if (!view.tagNodes) view.tagNodes = {};
    for (const key of Object.keys(view.tagNodes)) if (!want.some(w => w.key === key)) { view.tagNodes[key].remove(); delete view.tagNodes[key]; }
    const box = new tw.THREE.Box3(), v = new tw.THREE.Vector3();
    for (const w of want) {
      let node = view.tagNodes[w.key];
      if (!node) { node = view.api.el("span", { class: "tw-tag" }); n.tags.appendChild(node); view.tagNodes[w.key] = node; }
      node.textContent = w.text; node.className = "tw-tag tw-" + w.level;
      box.setFromObject(tw.entities[w.key]); box.getCenter(v); v.y = box.max.y + 0.03;
      v.project(tw.camera);
      node.hidden = v.z > 1;
      node.style.left = ((v.x + 1) / 2 * n.stage.clientWidth) + "px";
      node.style.top = ((1 - v.y) / 2 * n.stage.clientHeight) + "px";
    }
  }

  function loop() {
    if (!view.visible) { view.raf = 0; return; }
    const tw = view.three;
    tw.controls.update(); tw.renderer.render(tw.scene, tw.camera); tags();
    view.raf = requestAnimationFrame(loop);
  }

  async function refresh() {
    try { view.body = await view.api.request("state"); }
    catch { view.body = { devices: {}, joints: {}, signals: { robot_stale: true, stage_stale: true } }; }
    renderRows(); apply();
  }

  function start() {
    view.visible = true;
    if (!view.timer) { refresh(); view.timer = setInterval(refresh, POLL_MS); }
    if (view.three && !view.raf) view.raf = requestAnimationFrame(loop);
  }
  function stop() {
    view.visible = false;
    if (view.timer) { clearInterval(view.timer); view.timer = null; }
  }

  LabConsole.register({
    id: "twin",
    i18n,
    logic: { gltfAxis, nodeValue, furnaceGlow, entityMode, rows },
    mount(root, api) {
      view.root = root; view.api = api;
      build();
      api.onLanguage(() => { build(); renderRows(); });
      start();
      loadScripts().then(() => { setupThree(); loadModel(); if (view.visible && !view.raf) view.raf = requestAnimationFrame(loop); })
        .catch(error => { view.n.status.textContent = api.t("twin.noModel", { why: String(error.message) }); });
    },
    show: start,
    hide: stop,
  });
})();
