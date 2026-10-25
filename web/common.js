import { api, openSocket, SCENARIOS } from "/static/mock.js";

export { api, openSocket, ApiError, USE_MOCK, SCENARIOS, RECORD_LABELS } from "/static/mock.js";

/** Product name. Rename here only. */
export const PRODUCT_NAME = "BreathWatch";

export const TIER_NAME = ["Stable", "Watch", "Notify", "Urgent"];

const ROLE_LABEL = { nurse: "Nurse", physician: "Physician", admin: "Admin" };

export function $(id) { return document.getElementById(id); }

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

export function fmt(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(digits);
}

export function fmtTime(t) {
  if (t === null || t === undefined) return "—";
  return new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function fmtWhen(t) {
  if (t === null || t === undefined) return "—";
  return new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function tierName(tier) { return TIER_NAME[tier] || "Stable"; }

export function applyProductName() {
  for (const el of document.querySelectorAll("[data-product]")) el.textContent = PRODUCT_NAME;
  const titled = document.body?.dataset.title;
  if (titled) document.title = `${titled} · ${PRODUCT_NAME}`;
}

export function qualityLabel(q) {
  return { clean: "Clean", motion: "Motion", sensor_off: "Sensor off", unknown: "Unknown" }[q] || "—";
}

export function postureLabel(p) {
  return { supine: "Supine", left: "Left side", right: "Right side", upright: "Upright", unknown: "Unknown" }[p] || "—";
}

export function eventLabel(type) {
  return { obstructive: "Obstructive", central: "Central", mixed: "Mixed", hypopnea: "Hypopnea" }[type] || type || "—";
}

export function scenarioLabel(name) {
  return name ? String(name).replaceAll("_", " ") : "—";
}

export function riskItems(rf) {
  if (!rf) return [];
  const items = [];
  if (rf.opioids) items.push("Opioids");
  if (rf.supplemental_o2) items.push("Supplemental O₂");
  if (rf.osa === "suspected") items.push("OSA suspected");
  if (rf.osa === "known") items.push("OSA known");
  if (rf.age_over_65) items.push("Age over 65");
  return items;
}

export function canEdit(user) { return user.role === "nurse" || user.role === "admin"; }

export function patientIdFromLocation() {
  const match = location.pathname.match(/^\/patients\/([^/]+)\/?$/);
  if (!match || match[1] === "new") return null;
  return decodeURIComponent(match[1]);
}

export function safeNext(raw) {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\") || raw.startsWith("/login")) return "/";
  return raw;
}

const PUBLIC_DEMO_TEXT = "Public demo — simulated data, research prototype";

export function mountPublicDemo(text) {
  let el = document.querySelector(".public-demo-banner");
  if (!el) {
    el = document.createElement("div");
    el.className = "public-demo-banner";
    el.setAttribute("role", "note");
    document.body.prepend(el);
  }
  el.removeAttribute("style");
  const message = (text || el.textContent || PUBLIC_DEMO_TEXT).trim() || PUBLIC_DEMO_TEXT;
  el.textContent = message;
  document.body.classList.add("has-public-demo");
  const fields = document.getElementById("password-fields");
  if (fields) fields.hidden = true;
}

export async function applyPublicDemo() {
  let report = null;
  try {
    report = await api("/api/public-config", { redirectOn401: false });
  } catch { /* an older server reports the mode by injecting the banner */ }
  if (report && report.public_demo === true) {
    mountPublicDemo(report.banner);
    return;
  }
  const injected = document.querySelector(".public-demo-banner");
  if (injected && injected.textContent.trim()) mountPublicDemo(injected.textContent);
}

export async function boot() {
  const me = await api("/api/auth/me");
  const user = me.user || me;
  applyProductName();
  renderChrome(user);
  document.body.dataset.role = user.role || "";
  await applyPublicDemo();
  return user;
}

export function renderChrome(user) {
  const el = $("chrome");
  if (!el) return;
  const path = location.pathname;
  const links = [
    { href: "/", label: "Ward", show: true, on: path === "/" },
    { href: "/patients/new", label: "Add patient", show: canEdit(user), on: path === "/patients/new" },
    { href: "/admin/device", label: "Device & data", show: user.role === "admin", on: path.startsWith("/admin") },
  ];
  const role = ROLE_LABEL[user.role] || user.role;
  el.innerHTML = `
    <a class="brand" href="/">${esc(PRODUCT_NAME)}</a>
    <nav class="nav" aria-label="Sections">
      ${links.filter((l) => l.show).map((l) => `
        <a href="${l.href}" ${l.on ? 'aria-current="page"' : ""}>${l.label}</a>
      `).join("")}
    </nav>
    <div class="session">
      <span class="session__who">${esc(user.display_name)} <span class="session__role">(${esc(role)})</span></span>
      <button type="button" class="icon-btn" data-signout aria-label="Sign out" title="Sign out">⏏</button>
    </div>`;
  el.querySelector("[data-signout]").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST", redirectOn401: false });
    location.href = "/login";
  });
}

export function liveSocket(path, handlers) {
  let current = null;
  let stopped = false;
  let retry = 500;
  const start = () => {
    if (stopped) return;
    current = openSocket(path, {
      onMessage: handlers.onMessage,
      onClose(code) {
        if (stopped) return;
        if (code === 4401) {
          location.replace("/login?next=" + encodeURIComponent(location.pathname + location.search));
          return;
        }
        handlers.onTerminal?.(code);
        if (code === 4403 || code === 4404 || code === 1000) return;
        setTimeout(start, retry);
        retry = Math.min(retry * 2, 5000);
      },
    });
  };
  start();
  return { close() { stopped = true; current?.close(); } };
}

export function showError(el, err) {
  if (!el) return;
  if (!err) { el.hidden = true; el.textContent = ""; return; }
  el.hidden = false;
  el.textContent = err.detail || err.message || "Something went wrong";
}

export function admissionFields(patient, liveHolder, opts = {}) {
  const rf = patient?.risk_factors || { opioids: false, supplemental_o2: false, osa: "none", age_over_65: false };
  const device = patient?.device || { kind: "simulator", scenario: "normal", speed: 1, loop: true };
  const blocked = Boolean(opts.liveDisabled) || Boolean(liveHolder && (!patient || liveHolder.id !== patient.id));
  const liveOn = device.kind === "live" && !blocked;
  const scenario = device.scenario || "normal";
  const options = SCENARIOS.map((name) => `
    <option value="${esc(name)}" ${name === scenario ? "selected" : ""}>${esc(scenarioLabel(name))}</option>
  `).join("");
  const taken = new Set((opts.takenBeds || []).map(String));
  if (patient?.bed) taken.delete(String(patient.bed));
  const beds = ["1", "2", "3", "4", "5", "6"];
  for (const bed of opts.takenBeds || []) {
    const label = String(bed);
    if (!beds.includes(label)) beds.push(label);
  }
  const open = beds.filter((bed) => !taken.has(bed));
  const bedOptions = open.length
    ? open.map((bed) => `
    <option value="${esc(bed)}" ${String(patient?.bed || "") === bed ? "selected" : ""}>Bed ${esc(bed)}</option>
  `).join("")
    : `<option value="" selected>No open beds</option>`;
  const hint = opts.liveDisabled
    ? "Live wearable is off in the public demo."
    : blocked
      ? `The live wearable is already assigned to bed ${liveHolder.bed}, ${liveHolder.name}.`
      : "";
  const speed = device.speed ?? 1;
  const loopOn = device.loop !== false;
  const deviceId = device.device_id || "patch-01";
  const advanced = opts.admin ? `
    <details class="advanced">
      <summary>Advanced</summary>
      <label>Device id <input name="device_id" value="${esc(deviceId)}" ${blocked ? "disabled" : ""}></label>
      <div class="fields">
        <label>Speed <input name="speed" type="number" min="0" step="0.5" value="${esc(speed)}"></label>
        <label class="check"><input type="checkbox" name="loop" ${loopOn ? "checked" : ""}> Loop</label>
      </div>
    </details>` : `
    <input type="hidden" name="device_id" value="${esc(deviceId)}">
    <input type="hidden" name="speed" value="${esc(speed)}">
    <input type="hidden" name="loop" value="${loopOn ? "on" : ""}">`;
  return `
    <label>Name <input name="name" required maxlength="80" value="${esc(patient?.name || "")}"></label>
    <label>Bed <select name="bed" required>${bedOptions}</select></label>
    <label>Notes
      <textarea name="notes" rows="2" maxlength="280">${esc(patient?.notes || "")}</textarea>
    </label>
    <fieldset>
      <legend>Risk factors</legend>
      <p class="fine">Context only — never changes alarm thresholds.</p>
      <div class="chip-row">
        <label class="toggle"><input type="checkbox" name="opioids" ${rf.opioids ? "checked" : ""}> Opioids</label>
        <label class="toggle"><input type="checkbox" name="supplemental_o2" ${rf.supplemental_o2 ? "checked" : ""}> Supplemental O₂</label>
        <label class="toggle"><input type="checkbox" name="age_over_65" ${rf.age_over_65 ? "checked" : ""}> Age over 65</label>
      </div>
      <label>OSA
        <select name="osa">
          <option value="none" ${rf.osa === "none" ? "selected" : ""}>None</option>
          <option value="suspected" ${rf.osa === "suspected" ? "selected" : ""}>Suspected</option>
          <option value="known" ${rf.osa === "known" ? "selected" : ""}>Known</option>
        </select>
      </label>
    </fieldset>
    <fieldset>
      <legend>Monitoring</legend>
      <label class="choice"><input type="radio" name="kind" value="live" ${liveOn ? "checked" : ""} ${blocked ? "disabled" : ""}> Live wearable</label>
      <label class="choice"><input type="radio" name="kind" value="simulator" ${liveOn ? "" : "checked"}> Demo simulation</label>
      <label class="sim-only">Scenario <select name="scenario">${options}</select></label>
      ${hint ? `<p class="fine">${esc(hint)}</p>` : ""}
    </fieldset>
    ${advanced}`;
}

export function readAdmission(form) {
  const data = new FormData(form);
  const kind = data.get("kind");
  const device = kind === "live"
    ? { kind: "live", device_id: String(data.get("device_id") || "").trim() }
    : {
      kind: "simulator",
      scenario: String(data.get("scenario") || "normal"),
      speed: Number(data.get("speed")),
      loop: data.get("loop") === "on",
    };
  return {
    name: String(data.get("name") || "").trim(),
    bed: String(data.get("bed") || "").trim(),
    notes: String(data.get("notes") || "").trim(),
    risk_factors: {
      opioids: data.get("opioids") === "on",
      supplemental_o2: data.get("supplemental_o2") === "on",
      osa: String(data.get("osa") || "none"),
      age_over_65: data.get("age_over_65") === "on",
    },
    device,
  };
}

export function riskHtml(rf) {
  const items = riskItems(rf);
  const chips = items.length
    ? items.map((item) => `<span class="chip">${esc(item)}</span>`).join("")
    : `<span class="chip">No risk factors listed</span>`;
  return `${chips}<p class="fine">Context only. Risk factors never change alarm thresholds.</p>`;
}
