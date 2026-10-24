/**
 * Clinical app data (§19).
 *
 * USE_MOCK is the only switch:
 *   true  — this in-memory ward (sessionStorage stands in for the session cookie)
 *   false — same-origin HTTP and WebSockets described in §19
 *
 * The mock does not run the Python detector. Seeded findings are a scripted ward;
 * waveforms and rates move with the clock. Demo reset clears alerts, events and
 * rate history. Restoring the default ward puts the scripted six beds back.
 */
export const USE_MOCK = false;

export const SCENARIOS = [
  "normal", "motion", "opioid_decline", "obstructive", "central", "hypopnea", "sensor_off", "demo",
];

export const RECORD_LABELS = [
  "clean", "motion", "sensor_off", "slow_breathing", "breath_hold_effort", "breath_hold_no_effort",
];

const ACCOUNTS = [
  { username: "nurse", display_name: "Demo Nurse", role: "nurse", password: "demo" },
  { username: "doctor", display_name: "Demo Physician", role: "physician", password: "demo" },
  { username: "admin", display_name: "Demo Admin", role: "admin", password: "demo" },
];

const SOURCES = ["belt", "accel", "airflow"];
const DB_KEY = "bw_mock_v1";
const LIVE_ID = "patch-01";

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `Request failed (${status})`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail || this.message;
  }
}

// --------------------------------------------------------------------- switch

export async function api(path, opts = {}) {
  const { redirectOn401 = true, ...rest } = opts;
  try {
    return USE_MOCK ? mockApi(path, rest) : await realApi(path, rest);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401 && redirectOn401 && location.pathname !== "/login") {
      const next = location.pathname + location.search;
      location.replace("/login?next=" + encodeURIComponent(next));
      return new Promise(() => {});
    }
    throw err;
  }
}

export function openSocket(path, handlers = {}) {
  if (!USE_MOCK) return realSocket(path, handlers);
  return mockSocket(path, handlers);
}

async function realApi(path, { method = "GET", body, query } = {}) {
  let url = path;
  if (query) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
    }
    const qs = params.toString();
    if (qs) url += (url.includes("?") ? "&" : "?") + qs;
  }
  const res = await fetch(url, {
    method,
    credentials: "include",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { detail: text }; }
  }
  if (!res.ok) {
    let detail = data && data.detail;
    if (detail && typeof detail !== "string") detail = JSON.stringify(detail);
    throw new ApiError(res.status, detail || res.statusText);
  }
  return data;
}

function realSocket(path, handlers) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}${path}`);
  ws.onmessage = (ev) => {
    try { handlers.onMessage?.(JSON.parse(ev.data)); } catch { /* ignore malformed */ }
  };
  ws.onclose = (ev) => handlers.onClose?.(ev.code);
  return { close() { ws.close(); } };
}

// --------------------------------------------------------------------- store

let db;

function ensure() {
  if (!db) db = loadDb();
  return db;
}

function loadDb() {
  try {
    const raw = sessionStorage.getItem(DB_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && parsed.v === 1 && Array.isArray(parsed.patients)) return parsed;
    }
  } catch { /* private mode or bad JSON — start fresh */ }
  const fresh = createDb();
  persist(fresh);
  return fresh;
}

function persist(next = db) {
  const raw = JSON.stringify(next);
  try { sessionStorage.setItem(DB_KEY, raw); } catch { /* ignore quota */ }
}

function save() { persist(db); }

function now() { return Date.now() / 1000; }

function sec(t) { return Math.round(t * 10) / 10; }

function r1(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return null;
  return Math.round(n * 10) / 10;
}

function r2(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return null;
  return Math.round(n * 100) / 100;
}

function copy(value) { return JSON.parse(JSON.stringify(value)); }

function createDb() {
  const epoch = now();
  const admitted = epoch - 7200;
  const nurse = { username: "nurse", display_name: "Demo Nurse", role: "nurse" };
  const all = SOURCES.slice();

  const patients = [
    bed("p1", "Elena Voss", "1", admitted - 1500, {
      opioids: false, supplemental_o2: false, osa: "none", age_over_65: true,
    }, sim("normal"), "Post-op day 2, knee arthroplasty. Regional anesthesia.", story("normal", { rr: 15.2, hr: 72, posture: "supine", breathPhase: 0.4, sources: all })),
    bed("p2", "Marcus Chen", "2", admitted - 1200, {
      opioids: false, supplemental_o2: false, osa: "none", age_over_65: false,
    }, sim("motion"), "Restless since lunch. Belt position checked.", story("motion", { posture: "supine", breathPhase: 1.2 })),
    bed("p3", "Priya Nair", "3", admitted - 900, {
      opioids: true, supplemental_o2: false, osa: "suspected", age_over_65: false,
    }, sim("obstructive"), "Snores heavily. No sleep study on file.", story("obstructive", {
      rr: 13.4, hr: 78, posture: "left", period: 70, apnea: 14, offset: 8, breathPhase: 2.1,
      sources: ["belt"], flags: ["single_source"],
    })),
    bed("p4", "Jordan Avery", "4", admitted, {
      opioids: true, supplemental_o2: true, osa: "suspected", age_over_65: false,
    }, { kind: "live", device_id: LIVE_ID }, "Post-op day 1, PCA morphine", story("live", {
      rr: 14.8, hr: 70, posture: "supine", breathPhase: 0.6, sources: all,
    })),
    bed("p5", "Samuel Okonkwo", "5", admitted - 400, {
      opioids: true, supplemental_o2: false, osa: "none", age_over_65: true,
    }, sim("opioid_decline"), "Post-op day 0. Opioid PCA.", story("decline", {
      hr: 66, posture: "supine", breathPhase: 3.3, sources: all, baseline: 15.1,
    })),
    bed("p6", "Ruth Hale", "6", admitted - 200, {
      opioids: true, supplemental_o2: false, osa: "known", age_over_65: true,
    }, sim("central"), "OSA, uses CPAP at home. Not wearing it on the ward.", story("central", {
      rr: 10.6, hr: 64, posture: "supine", period: 85, apnea: 22, offset: 37, breathPhase: 4.2, sources: all, baseline: 13.6,
    })),
  ];

  const alerts = [
    alert(1, "p5", "rr_trend", 2, "Breathing rate dropped 35% from baseline 15/min over 25 min, signal confidence high", {
      status: "acknowledged", raised_t: epoch - 2400, updated_t: epoch - 1800,
      acknowledged_by: nurse, acknowledged_t: epoch - 1800, ack_note: "Reviewed on rounds",
    }),
    alert(2, "p3", "apnea", 1, "Apnea 14 s (obstructive, left), signal confidence moderate", {
      raised_t: epoch - 520, updated_t: epoch - 520,
    }),
    alert(3, "p5", "rr_low", 2, "Breathing rate 7/min for 75 s (baseline 15/min), signal confidence high", {
      raised_t: epoch - 400, updated_t: epoch - 400,
    }),
    alert(4, "p6", "apnea", 3, "Apnea 22 s (central, supine), signal confidence high", {
      raised_t: epoch - 200, updated_t: epoch - 140, escalated: true, escalated_t: epoch - 140,
    }),
  ];

  const events = [
    event(1, "p3", "obstructive", epoch - 1600, 12, "left", 8, 88, 0.8),
    event(2, "p3", "obstructive", epoch - 534, 14, "left", 6, 91, 0.84),
    event(3, "p5", "hypopnea", epoch - 980, 16, "supine", 42, 48, 0.77),
    event(4, "p6", "central", epoch - 1100, 18, "supine", 5, 12, 0.82),
    event(5, "p6", "central", epoch - 222.5, 22.5, "supine", 4, 9, 0.88),
  ];

  const monitors = {};
  for (const p of patients) {
    const naive = { p1: 1, p2: 36, p3: 7, p4: 4, p5: 16, p6: 9 }[p.id];
    const smart = { p1: 0, p2: 0, p3: 2, p4: 0, p5: 2, p6: 3 }[p.id];
    monitors[p.id] = { smart, naive, since_t: p.admitted_t };
  }

  return {
    v: 1,
    username: null,
    epoch,
    history_from: 0,
    nextPatient: 7,
    nextAlert: 5,
    nextEvent: 6,
    patients,
    alerts,
    events,
    eventKeys: {},
    monitors,
    source: { source: "udp", speed: 1, loop: false, started_t: epoch, restarts: 0 },
    recording: null,
  };
}

function bed(id, name, bedNo, admitted_t, risk, device, notes, storyObj) {
  return {
    id, name, bed: bedNo, risk_factors: risk, device, notes,
    status: "admitted", admitted_t, discharged_t: null, pipeline_t: 0, story: storyObj,
  };
}

function sim(scenario) {
  return { kind: "simulator", scenario, speed: 1, loop: true };
}

function story(mode, extra) {
  return Object.assign({
    mode, rr: 15, hr: 70, posture: "supine", breathPhase: 0,
    sources: SOURCES.slice(), flags: [], period: 0, apnea: 0, offset: 0, baseline: null,
  }, extra);
}

function alert(id, patient_id, kind, tier, reason, extra) {
  return Object.assign({
    id, patient_id, kind, tier, reason, status: "active",
    raised_t: 0, updated_t: 0, escalated: false, escalated_t: null,
    acknowledged_by: null, acknowledged_t: null, ack_note: null,
  }, extra);
}

function event(id, patient_id, type, end_t, duration_s, posture, airflow_pct, effort_pct, confidence) {
  return {
    id, patient_id, type, start_t: sec(end_t - duration_s), end_t: sec(end_t),
    duration_s, confidence, posture, airflow_pct, effort_pct,
  };
}

// ---------------------------------------------------------------- physiology

function storyOf(p) {
  const src = ensure().source.source;
  if (p.story.mode === "live" && src.startsWith("simulator:")) {
    const scenario = src.slice("simulator:".length);
    const built = storyForScenario(scenario, { phase: 0.6, offset: 11 });
    built.hr = p.story.hr;
    built.posture = p.story.posture;
    return built;
  }
  return p.story;
}

function storyForScenario(scenario, { phase = 0, offset = 0 } = {}) {
  const all = SOURCES.slice();
  const table = {
    normal: story("normal", { rr: 14.6, hr: 74, breathPhase: phase, sources: all }),
    motion: story("motion", { breathPhase: phase, sources: [], flags: ["no_clean_source"] }),
    opioid_decline: story("decline", { hr: 67, breathPhase: phase, sources: all, baseline: 15.1 }),
    obstructive: story("obstructive", {
      rr: 13.5, hr: 76, period: 72, apnea: 16, offset, breathPhase: phase, sources: all,
    }),
    central: story("central", {
      rr: 11, hr: 63, period: 80, apnea: 18, offset, breathPhase: phase, sources: all, baseline: 13.4,
    }),
    hypopnea: story("hypopnea", { rr: 14, hr: 71, posture: "right", breathPhase: phase, sources: all }),
    sensor_off: story("sensor_off", { breathPhase: phase, sources: [], flags: ["no_clean_source"] }),
    demo: story("normal", { rr: 15, hr: 72, breathPhase: phase, sources: all }),
  };
  return table[scenario] || table.normal;
}

function liveLinkOk() {
  const src = ensure().source.source;
  if (src.startsWith("simulator:")) return true;
  if (src === "serial:/dev/cu.usbmodem1101") return true;
  return false;
}

function declineRr(t) {
  const epoch = ensure().epoch;
  const start = epoch - 50 * 60;
  const end = epoch - 18 * 60;
  if (t <= start) return 15.6;
  if (t >= end) return 7.2 + 0.22 * Math.sin(t / 17);
  const u = (t - start) / (end - start);
  const s = u * u * (3 - 2 * u);
  return 15.6 + (7.15 - 15.6) * s;
}

function rrAt(p, t) {
  if (p.status !== "admitted") return null;
  const s = storyOf(p);
  if (s.mode === "motion" || s.mode === "sensor_off") return null;
  if (s.mode === "live" && !liveLinkOk()) {
    if (t > ensure().epoch - 8 * 60) return null;
    return 14.6 + 0.3 * Math.sin(t / 26);
  }
  if (s.mode === "decline") return declineRr(t);
  const base = s.rr || 15;
  return base + 0.35 * Math.sin(t / 22 + (s.breathPhase || 0));
}

function hrAt(p, t) {
  if (p.status !== "admitted") return null;
  const s = storyOf(p);
  if (s.mode === "motion" || s.mode === "sensor_off") return null;
  if (s.mode === "live" && !liveLinkOk()) return null;
  return s.hr + 1.1 * Math.sin(t / 15);
}

function historyFloor(p) {
  if (p.pipeline_t) return p.pipeline_t;
  if (ensure().history_from) return ensure().history_from;
  return 0;
}

function historyStart(p, t) {
  return Math.max(t - 3600, historyFloor(p));
}

function apneaState(s, t) {
  if (!s.period) return { inApnea: false, completed: -1, start: 0, end: 0 };
  const u = t - s.offset;
  const idx = Math.floor(u / s.period);
  const into = u - idx * s.period;
  const inApnea = into >= 0 && into < s.apnea;
  const completed = inApnea ? idx - 1 : idx;
  const start = s.offset + completed * s.period;
  return { inApnea, completed, start, end: start + s.apnea };
}

function noise(t, salt) {
  const x = Math.sin(t * 12.9898 + salt * 78.233) * 43758.5453;
  return x - Math.floor(x);
}

function breath(t, rr) {
  const ph = ((t * (rr / 60)) % 1 + 1) % 1;
  if (ph < 0.42) return Math.sin((ph / 0.42) * Math.PI);
  return -0.35 * Math.sin(((ph - 0.42) / 0.58) * Math.PI);
}

function adc(base, delta) {
  return Math.max(0, Math.min(65535, Math.round(base + delta)));
}

function ppg(t, hr) {
  const ph = ((t * (hr / 60)) % 1 + 1) % 1;
  const peak = Math.exp(-((ph - 0.16) ** 2) / 0.0032);
  const notch = 0.28 * Math.exp(-((ph - 0.4) ** 2) / 0.005);
  return adc(27400, (peak + notch) * 4600);
}

function sampleChannel(p, t, channel) {
  if (p.status !== "admitted") return null;
  const s = storyOf(p);
  if (s.mode === "live" && !liveLinkOk()) return null;
  const speed = p.device.kind === "simulator" ? (p.device.speed || 1) : (ensure().source.speed || 1);
  const bt = t * (speed || 1) + (s.breathPhase || 0);
  if (s.mode === "sensor_off") {
    if (channel === "effort") return 65520;
    if (channel === "airflow") return adc(40100, (noise(t, 2) - 0.5) * 30);
    return ppg(bt, s.hr || 70);
  }
  if (s.mode === "motion") {
    const burst = noise(Math.floor(t * 2), 1) > 0.45;
    if (burst && channel !== "pulse") return noise(t, channel === "effort" ? 2 : 3) > 0.5 ? 65535 : 280;
    if (channel === "pulse") return adc(28000, (noise(t, 5) - 0.5) * 8000);
    return adc(channel === "effort" ? 33000 : 41000, (noise(t, 6) - 0.5) * 9000);
  }
  const rr = rrAt(p, t) || s.rr || 14;
  const hr = hrAt(p, t) || s.hr || 70;
  const ap = apneaState(s, t);
  let amp = s.mode === "hypopnea" ? 620 : s.mode === "decline" ? 480 + rr * 42 : 1550;
  const wobble = (noise(t, channel === "airflow" ? 4 : 7) - 0.5) * 70;
  if (channel === "pulse") return ppg(bt, hr);
  if (ap.inApnea && channel === "airflow") return adc(40110, wobble * 0.15);
  if (ap.inApnea && channel === "effort" && s.mode === "central") return adc(32040, wobble * 0.2);
  if (channel === "airflow" && s.flags.includes("single_source") && !ap.inApnea) {
    return adc(39800, (noise(bt, 4) - 0.5) * 5200);
  }
  const wave = breath(channel === "airflow" ? bt - 0.4 : bt, rr);
  const base = channel === "airflow" ? 40220 : 31800;
  const scale = channel === "airflow" ? 0.82 : 1;
  return adc(base, wave * amp * scale + wobble);
}

function vitalsAt(p, t) {
  const s = storyOf(p);
  const rr = rrAt(p, t);
  const hr = hrAt(p, t);
  const flags = s.flags.slice();
  let quality = "clean";
  let qualityConf = 0.93;
  let motion = 0.006;
  if (s.mode === "motion") { quality = "motion"; qualityConf = 0.41; motion = 0.38; }
  else if (s.mode === "sensor_off") { quality = "sensor_off"; qualityConf = 0.9; motion = 0.002; }
  else if (s.mode === "live" && !liveLinkOk()) {
    quality = "unknown"; qualityConf = null; motion = null;
  } else if (s.mode === "decline") qualityConf = 0.92;
  if (s.mode === "motion" || s.mode === "sensor_off" || (s.mode === "live" && !liveLinkOk())) {
    if (!flags.includes("no_clean_source")) flags.push("no_clean_source");
  }
  else if (s.flags.includes("single_source")) qualityConf = 0.74;

  const sources = (rr === null) ? [] : s.sources.slice();
  let conf = rr === null ? null : (s.mode === "decline" ? 0.91 : s.mode === "central" ? 0.88 : 0.95);
  if (sources.length === 1) conf = Math.min(conf ?? 0.6, 0.6);

  const v = {
    t: sec(t),
    rr_fused: r1(rr),
    rr_conf: conf === null ? null : r2(conf),
    rr_sources: sources,
    flags,
    rr_belt: null, rr_accel: null, rr_airflow: null,
    conf_belt: null, conf_accel: null, conf_airflow: null,
    rr_prelim: rr === null ? null : r1(rr + 0.25 * Math.sin(t / 9)),
    hr: r1(hr),
    hr_conf: hr === null ? null : 0.9,
    quality, quality_conf: qualityConf === null ? null : r2(qualityConf),
    posture: quality === "unknown" ? "unknown" : s.posture,
    motion: motion === null ? null : r2(motion),
  };
  if (rr !== null && sources.includes("belt")) { v.rr_belt = r1(rr + 0.1); v.conf_belt = r2(Math.min(0.99, (conf || 0.6) + 0.02)); }
  if (rr !== null && sources.includes("accel")) { v.rr_accel = r1(rr - 0.15); v.conf_accel = r2((conf || 0.6) - 0.04); }
  if (rr !== null && sources.includes("airflow")) { v.rr_airflow = r1(rr + 0.05); v.conf_airflow = r2(conf); }
  return v;
}

function baselineFor(p, t) {
  if (p.status !== "admitted") return null;
  const s = storyOf(p);
  if (s.mode === "motion" || s.mode === "sensor_off") return null;
  if (s.mode === "live" && !liveLinkOk()) return null;
  const floor = historyFloor(p);
  if (floor && t - floor < 120) return null;
  const rr = s.baseline || (s.mode === "decline" ? 15.1 : (s.rr || 15));
  return { rr: r1(rr), since_t: sec(floor || p.admitted_t), n: 120 };
}

function trendSeries(p, t) {
  const start = historyStart(p, t);
  const points = [];
  for (let x = start; x <= t; x += 5) points.push([sec(x), r1(rrAt(p, x))]);
  if (!points.length || points[points.length - 1][0] < sec(t) - 1) points.push([sec(t), r1(rrAt(p, t))]);
  return points;
}

// --------------------------------------------------------------------- views

function userPublic(acct) {
  return { username: acct.username, display_name: acct.display_name, role: acct.role };
}

function currentUser() {
  const name = ensure().username;
  if (!name) return null;
  const acct = ACCOUNTS.find((a) => a.username === name);
  return acct ? userPublic(acct) : null;
}

function requireUser() {
  const user = currentUser();
  if (!user) throw new ApiError(401, "Sign in required");
  return user;
}

function requireEditor() {
  const user = requireUser();
  if (user.role !== "nurse" && user.role !== "admin") {
    throw new ApiError(403, "Admitting and editing patients needs a nurse or admin demo account");
  }
  return user;
}

function requireAdmin() {
  const user = requireUser();
  if (user.role !== "admin") {
    throw new ApiError(403, "Device and data controls are limited to the demo admin account");
  }
  return user;
}

function findPatient(id) {
  return ensure().patients.find((p) => p.id === id) || null;
}

function mustPatient(id) {
  const p = findPatient(id);
  if (!p) throw new ApiError(404, `No patient ${id}`);
  return p;
}

function publicDevice(d) {
  if (d.kind === "live") return { kind: "live", device_id: d.device_id };
  return { kind: "simulator", scenario: d.scenario, speed: d.speed ?? 1, loop: d.loop !== false };
}

function publicPatient(p) {
  return {
    id: p.id, name: p.name, bed: p.bed,
    risk_factors: Object.assign({}, p.risk_factors),
    device: publicDevice(p.device),
    notes: p.notes || "",
    status: p.status,
    admitted_t: p.admitted_t,
    discharged_t: p.discharged_t,
  };
}

function publicAlert(a) {
  return {
    id: a.id, patient_id: a.patient_id, kind: a.kind, tier: a.tier, reason: a.reason,
    status: a.status, raised_t: a.raised_t, updated_t: a.updated_t,
    escalated: a.escalated, escalated_t: a.escalated_t,
    acknowledged_by: a.acknowledged_by ? Object.assign({}, a.acknowledged_by) : null,
    acknowledged_t: a.acknowledged_t, ack_note: a.ack_note,
  };
}

function publicEvent(e) {
  return {
    id: e.id, patient_id: e.patient_id, type: e.type,
    start_t: e.start_t, end_t: e.end_t, duration_s: e.duration_s,
    confidence: e.confidence, posture: e.posture,
    airflow_pct: e.airflow_pct, effort_pct: e.effort_pct,
  };
}

function activeAlerts(id) {
  return ensure().alerts
    .filter((a) => a.patient_id === id && a.status === "active")
    .sort((a, b) => b.tier - a.tier || b.raised_t - a.raised_t);
}

function tierOf(id) {
  const list = activeAlerts(id);
  if (!list.length) return { tier: 0, tier_reason: null, unacked_alerts: 0 };
  return { tier: list[0].tier, tier_reason: list[0].reason, unacked_alerts: list.length };
}

function latestEvent(id) {
  const list = ensure().events.filter((e) => e.patient_id === id);
  if (!list.length) return null;
  list.sort((a, b) => b.end_t - a.end_t);
  const e = list[0];
  return { type: e.type, start_t: e.start_t, duration_s: e.duration_s };
}

function summaryOf(p, t) {
  const v = p.status === "admitted" ? vitalsAt(p, t) : null;
  const tier = tierOf(p.id);
  const dev = deviceStatus(p, t);
  return {
    id: p.id, name: p.name, bed: p.bed,
    device_kind: p.device.kind,
    scenario: p.device.kind === "simulator" ? p.device.scenario : null,
    link_ok: dev.link_ok,
    tier: tier.tier,
    tier_reason: tier.tier_reason,
    rr: v ? v.rr_fused : null,
    rr_conf: v ? v.rr_conf : null,
    hr: v ? v.hr : null,
    posture: v ? v.posture : "unknown",
    quality: v ? v.quality : "unknown",
    last_event: latestEvent(p.id),
    unacked_alerts: tier.unacked_alerts,
    risk_factors: Object.assign({}, p.risk_factors),
    updated_t: sec(t),
  };
}

function bedRank(bedNo) {
  const m = /^(\d+)(.*)$/.exec(String(bedNo).trim());
  if (!m) return [1e9, String(bedNo)];
  return [Number(m[1]), m[2]];
}

function wardPayload(status) {
  syncGenerated(false);
  const t = now();
  const want = status || "admitted";
  let list = ensure().patients.filter((p) => {
    if (want === "all") return true;
    if (want === "discharged") return p.status === "discharged";
    return p.status === "admitted";
  });
  const summaries = list.map((p) => summaryOf(p, t));
  summaries.sort((a, b) => {
    if (b.tier !== a.tier) return b.tier - a.tier;
    if (b.unacked_alerts !== a.unacked_alerts) return b.unacked_alerts - a.unacked_alerts;
    const [an, as] = bedRank(a.bed);
    const [bn, bs] = bedRank(b.bed);
    if (an !== bn) return an - bn;
    return as < bs ? -1 : as > bs ? 1 : 0;
  });
  const counts = { "0": 0, "1": 0, "2": 0, "3": 0 };
  for (const s of summaries) counts[String(s.tier)] += 1;
  return { patients: summaries, counts, generated_t: sec(t) };
}

function deviceStatus(p, t) {
  if (p.device.kind === "simulator") {
    const running = p.status === "admitted";
    const started = p.pipeline_t || p.admitted_t;
    return {
      kind: "simulator", device_id: null, scenario: p.device.scenario,
      link_ok: running,
      source: `simulator:${p.device.scenario}`,
      source_status: running
        ? `running ${p.device.scenario}, speed ${p.device.speed ?? 1}, ${p.device.loop !== false ? "loop" : "once"}`
        : "discharged",
      packets: running ? Math.max(0, Math.floor((t - started) * 2)) : 0,
      gaps: 0, missing_packets: 0,
      last_packet_t: running ? sec(t) : null,
    };
  }
  const link = p.status === "admitted" && liveLinkOk();
  const started = ensure().source.started_t;
  return {
    kind: "live", device_id: p.device.device_id, scenario: null,
    link_ok: link,
    source: ensure().source.source,
    source_status: sourceStatusText(),
    packets: link ? Math.max(0, Math.floor((t - started) * 2)) : 0,
    gaps: link ? 1 : 0,
    missing_packets: link ? 3 : 0,
    last_packet_t: link ? sec(t) : sec(ensure().epoch - 8 * 60),
  };
}

function sourceStatusText() {
  const src = ensure().source;
  if (src.source === "udp") return "listening on 0.0.0.0:5005 — no packets from patch-01";
  if (src.source === "serial:auto") return "waiting for a Pico (USB VID 2E8A)";
  if (src.source.startsWith("serial:")) {
    return liveLinkOk()
      ? `connected to ${src.source.slice(7)}`
      : `opened ${src.source.slice(7)} — no packets`;
  }
  if (src.source.startsWith("simulator:")) {
    const scenario = src.source.slice(10);
    return `running ${scenario}, speed ${src.speed}, ${src.loop ? "loop" : "once"}`;
  }
  return src.source;
}

function monitorsFor(p, t) {
  const base = ensure().monitors[p.id] || { smart: 0, naive: 0, since_t: p.admitted_t };
  let naive = base.naive;
  if (p.id === "p2" && p.status === "admitted") naive += Math.floor(Math.max(0, t - ensure().epoch) / 25);
  const hours = Math.max(1 / 60, (t - base.since_t) / 3600);
  return {
    smart: { alarms: base.smart, per_hour: r1(base.smart / hours) },
    naive: { alarms: naive, per_hour: r1(naive / hours) },
    since_t: base.since_t,
  };
}

function stateOf(p) {
  const t = now();
  syncGenerated(false);
  const tier = tierOf(p.id);
  const events = patientEvents(p.id).slice(0, 10);
  const active = activeAlerts(p.id).map(publicAlert);
  return {
    patient: publicPatient(p),
    device: deviceStatus(p, t),
    vitals: p.status === "admitted" ? vitalsAt(p, t) : null,
    tier: tier.tier,
    tier_reason: tier.tier_reason,
    baseline: baselineFor(p, t),
    active_alerts: active,
    recent_events: events,
    monitors: monitorsFor(p, t),
  };
}

function patientEvents(id) {
  return ensure().events
    .filter((e) => e.patient_id === id)
    .sort((a, b) => b.end_t - a.end_t)
    .map(publicEvent);
}

function patientAlerts(id, status) {
  const allowed = status === "unacked" ? ["active", "awaiting_ack"]
    : status === "all" || !status ? null
    : [status];
  return ensure().alerts
    .filter((a) => a.patient_id === id && (allowed === null || allowed.includes(a.status)))
    .sort((a, b) => b.raised_t - a.raised_t)
    .map(publicAlert);
}

function syncGenerated(emit) {
  const t = now();
  let changed = false;
  for (const p of ensure().patients) {
    if (p.status !== "admitted") continue;
    const s = storyOf(p);
    const ap = apneaState(s, t);
    if (ap.completed < 0) continue;
    const key = `${p.id}:${s.mode}:${ap.completed}`;
    if (ensure().eventKeys[key]) continue;
    const type = s.mode === "central" ? "central" : s.mode === "obstructive" ? "obstructive" : null;
    if (!type) continue;
    const ev = {
      id: ensure().nextEvent++,
      patient_id: p.id,
      type,
      start_t: sec(ap.start),
      end_t: sec(ap.end),
      duration_s: s.apnea,
      confidence: type === "central" ? 0.86 : 0.83,
      posture: s.posture,
      airflow_pct: type === "central" ? 5 : 7,
      effort_pct: type === "central" ? 11 : 90,
      _gen: true,
    };
    ensure().events.push(ev);
    ensure().eventKeys[key] = ev.id;
    changed = true;
    if (emit) emitPatient(p.id, { type: "event", event: publicEvent(ev) });
  }
  if (changed) {
    pruneEvents();
    save();
  }
  escalateDue(emit);
}

function pruneEvents() {
  for (const p of ensure().patients) {
    const gen = ensure().events.filter((e) => e.patient_id === p.id && e._gen);
    if (gen.length <= 6) continue;
    gen.sort((a, b) => a.end_t - b.end_t);
    const drop = new Set(gen.slice(0, gen.length - 6).map((e) => e.id));
    ensure().events = ensure().events.filter((e) => !drop.has(e.id));
  }
}

function escalateDue(emit) {
  const t = now();
  for (const a of ensure().alerts) {
    if (a.tier === 3 && a.status === "active" && !a.escalated && t - a.raised_t >= 60) {
      a.escalated = true;
      a.escalated_t = sec(a.raised_t + 60);
      a.updated_t = sec(t);
      save();
      const pub = publicAlert(a);
      if (emit) {
        emitPatient(a.patient_id, { type: "alert", alert: pub });
        emitWard({ type: "alert", patient_id: a.patient_id, alert: pub });
      }
    }
  }
}

// --------------------------------------------------------------------- mutate

function validateRisk(rf) {
  if (!rf || typeof rf !== "object") throw new ApiError(400, "Risk factors are required");
  for (const key of ["opioids", "supplemental_o2", "age_over_65"]) {
    if (typeof rf[key] !== "boolean") throw new ApiError(400, `Risk factor ${key} must be true or false`);
  }
  if (!["none", "suspected", "known"].includes(rf.osa)) throw new ApiError(400, "OSA must be none, suspected, or known");
  return {
    opioids: rf.opioids, supplemental_o2: rf.supplemental_o2, osa: rf.osa, age_over_65: rf.age_over_65,
  };
}

function validateDevice(device) {
  if (!device || (device.kind !== "live" && device.kind !== "simulator")) {
    throw new ApiError(400, "Device must be a live wearable or a simulator scenario");
  }
  if (device.kind === "live") {
    const id = String(device.device_id || "").trim();
    if (!id) throw new ApiError(400, "Live device needs a device id");
    return { kind: "live", device_id: id };
  }
  if (!SCENARIOS.includes(device.scenario)) throw new ApiError(400, "Unknown simulator scenario");
  const speed = device.speed === undefined ? 1 : Number(device.speed);
  if (!Number.isFinite(speed) || speed < 0) throw new ApiError(400, "Speed must be a number ≥ 0");
  return { kind: "simulator", scenario: device.scenario, speed, loop: device.loop !== false };
}

function assertBedFree(bedNo, exceptId) {
  const hit = ensure().patients.find((p) => p.status === "admitted" && p.id !== exceptId && p.bed === bedNo);
  if (hit) throw new ApiError(409, `Bed ${bedNo} is already in use by ${hit.name}`);
}

function assertDeviceFree(device, exceptId) {
  if (device.kind !== "live") return;
  const hit = ensure().patients.find((p) => p.status === "admitted" && p.id !== exceptId
    && p.device.kind === "live" && p.device.device_id === device.device_id);
  if (hit) throw new ApiError(409, `${device.device_id} is already assigned to ${hit.name} (bed ${hit.bed})`);
}

function applyCreate(body, exceptId) {
  const name = String(body.name || "").trim();
  const bedNo = String(body.bed || "").trim();
  if (!name) throw new ApiError(400, "Name is required");
  if (!bedNo) throw new ApiError(400, "Bed is required");
  const risk = validateRisk(body.risk_factors);
  const device = validateDevice(body.device);
  assertBedFree(bedNo, exceptId);
  assertDeviceFree(device, exceptId);
  const notes = body.notes === undefined ? "" : String(body.notes);
  return { name, bed: bedNo, risk_factors: risk, device, notes };
}

function createPatient(body) {
  requireEditor();
  const fields = applyCreate(body, null);
  const t = now();
  const id = `p${ensure().nextPatient++}`;
  const scenario = fields.device.kind === "simulator" ? fields.device.scenario : "live";
  const patient = {
    id, ...fields,
    status: "admitted",
    admitted_t: sec(t),
    discharged_t: null,
    pipeline_t: sec(t),
    story: fields.device.kind === "live"
      ? story("live", { rr: 14.8, hr: 72, sources: SOURCES.slice() })
      : storyForScenario(scenario, { phase: Math.random() * 6, offset: Math.floor(Math.random() * 45) }),
  };
  ensure().patients.push(patient);
  ensure().monitors[id] = { smart: 0, naive: 0, since_t: sec(t) };
  save();
  return { status: 201, body: publicPatient(patient) };
}

function patchPatient(id, body) {
  requireEditor();
  const p = mustPatient(id);
  if (p.status === "discharged") throw new ApiError(400, "Discharged patients cannot be edited");
  const next = {
    name: body.name !== undefined ? body.name : p.name,
    bed: body.bed !== undefined ? body.bed : p.bed,
    notes: body.notes !== undefined ? body.notes : p.notes,
    risk_factors: body.risk_factors !== undefined ? body.risk_factors : p.risk_factors,
    device: body.device !== undefined ? body.device : p.device,
  };
  const fields = applyCreate(next, id);
  const deviceChanged = JSON.stringify(publicDevice(fields.device)) !== JSON.stringify(publicDevice(p.device));
  Object.assign(p, fields);
  if (deviceChanged) {
    p.pipeline_t = sec(now());
    p.story = fields.device.kind === "live"
      ? story("live", { rr: 14.8, hr: p.story.hr || 70, posture: p.story.posture, sources: SOURCES.slice() })
      : storyForScenario(fields.device.scenario, { phase: Math.random() * 6, offset: Math.floor(Math.random() * 45) });
    restartPatientStream(id);
  }
  save();
  return publicPatient(p);
}

function dischargePatient(id) {
  requireEditor();
  const p = mustPatient(id);
  if (p.status !== "discharged") {
    const t = now();
    p.status = "discharged";
    p.discharged_t = sec(t);
    save();
    emitPatient(id, { type: "discharged", patient: publicPatient(p) });
    closePatientSockets(id, 1000);
  }
  return publicPatient(p);
}

function ackAlert(patientId, alertId, body) {
  const user = requireUser();
  mustPatient(patientId);
  const a = ensure().alerts.find((x) => x.id === Number(alertId) && x.patient_id === patientId);
  if (!a) throw new ApiError(404, "No alert with that id for this patient");
  if (a.status !== "acknowledged") {
    a.status = "acknowledged";
    a.acknowledged_by = user;
    a.acknowledged_t = sec(now());
    a.updated_t = a.acknowledged_t;
    a.ack_note = body && body.note ? String(body.note) : "";
    save();
    const pub = publicAlert(a);
    emitPatient(patientId, { type: "alert", alert: pub });
    emitWard({ type: "alert", patient_id: patientId, alert: pub });
    emitWardMessage();
  }
  return publicAlert(a);
}

function adminDevice() {
  requireAdmin();
  const t = now();
  const src = ensure().source;
  const link = liveLinkOk();
  const live = ensure().patients.find((p) => p.status === "admitted" && p.device.kind === "live");
  const packets = link ? Math.max(0, Math.floor((t - src.started_t) * 2)) : 0;
  const recording = ensure().recording
    ? {
      label: ensure().recording.label,
      started_t: ensure().recording.started_t,
      samples: Math.max(0, Math.floor((t - ensure().recording.started_t) * 50)),
    }
    : null;
  return {
    source: src.source,
    source_status: sourceStatusText(),
    link_ok: link,
    stats: {
      packets, invalid: 0, gaps: link ? 1 : 0, missing_packets: link ? 3 : 0,
      dropped_late: 0, restarts: src.restarts, nonjson_lines: src.source.startsWith("serial:") ? 4 : 0,
      last_arrival: link ? sec(t) : null,
      last_seq: link ? 1200 + packets : null,
      last_t0_ms: null,
      device: link ? LIVE_ID : null,
      addr: link && src.source === "udp" ? "10.42.0.24:5005" : null,
    },
    assigned_patient_id: live ? live.id : null,
    unassigned_packets: src.source === "udp" ? 2 + Math.floor(Math.max(0, t - src.started_t) / 12) : 0,
    network: { ips: ["172.20.10.4"], http_port: 8000, udp_port: 5005 },
    recording,
  };
}

function setSource(body) {
  requireAdmin();
  const spec = String(body.source || "");
  let ok = spec === "udp" || spec === "serial:auto" || spec.startsWith("serial:/");
  if (spec.startsWith("simulator:")) ok = SCENARIOS.includes(spec.slice(10));
  if (!ok) throw new ApiError(400, "Source must be udp, serial:<port|auto>, or simulator:<scenario>");
  const speed = body.speed === undefined ? ensure().source.speed : Number(body.speed);
  if (!Number.isFinite(speed) || speed < 0) throw new ApiError(400, "Speed must be a number ≥ 0");
  ensure().source = {
    source: spec,
    speed,
    loop: Boolean(body.loop),
    started_t: sec(now()),
    restarts: ensure().source.restarts + 1,
  };
  save();
  const live = ensure().patients.find((p) => p.device.kind === "live" && p.status === "admitted");
  if (live) restartPatientStream(live.id);
  return adminDevice();
}

function demoReset(body) {
  requireAdmin();
  const reseed = Boolean(body && body.reseed_ward);
  const user = ensure().username;
  if (reseed) {
    const fresh = createDb();
    fresh.username = user;
    db = fresh;
  } else {
    const t = sec(now());
    ensure().alerts = [];
    ensure().events = [];
    ensure().eventKeys = {};
    ensure().history_from = t;
    ensure().nextAlert = 1;
    ensure().nextEvent = 1;
    for (const p of ensure().patients) {
      p.pipeline_t = t;
      ensure().monitors[p.id] = { smart: 0, naive: 0, since_t: t };
    }
  }
  save();
  broadcastAfterReset();
  return wardPayload("admitted");
}

const SERIAL_PORTS = [
  { device: "/dev/cu.usbmodem1101", description: "Board in FS mode", vid: 11914, pid: 5, is_pico: true },
  { device: "/dev/cu.Bluetooth-Incoming-Port", description: "n/a", vid: null, pid: null, is_pico: false },
];

// --------------------------------------------------------------------- http

function mockApi(path, { method = "GET", body, query } = {}) {
  ensure();
  const url = new URL(path, "http://breathwatch.local");
  const q = Object.assign({}, query || {});
  for (const [key, value] of url.searchParams) q[key] = value;
  const result = mockRoute(method.toUpperCase(), url.pathname, body ?? {}, q);
  if (result.status === 204) return null;
  return copy(result.body);
}

function mockRoute(method, path, body, query) {
  if (method === "POST" && path === "/api/auth/login") {
    const acct = ACCOUNTS.find((a) => a.username === body.username && a.password === body.password);
    if (!acct) throw new ApiError(401, "Unknown username or password");
    ensure().username = acct.username;
    save();
    return { status: 200, body: { user: userPublic(acct) } };
  }
  if (method === "POST" && path === "/api/auth/demo") {
    const acct = ACCOUNTS.find((a) => a.role === body.role);
    if (!acct) throw new ApiError(400, "Unknown demo role");
    ensure().username = acct.username;
    save();
    return { status: 200, body: { user: userPublic(acct) } };
  }
  if (method === "POST" && path === "/api/auth/logout") {
    ensure().username = null;
    save();
    return { status: 204, body: null };
  }
  if (method === "GET" && path === "/api/auth/me") {
    const user = currentUser();
    if (!user) throw new ApiError(401, "Sign in required");
    return { status: 200, body: { user } };
  }
  if (method === "GET" && path === "/api/public-config") {
    return {
      status: 200,
      body: {
        public_demo: false,
        banner: null,
        password_login: true,
        live_device: true,
      },
    };
  }
  if (method === "POST" && path === "/api/calibrate/supine") {
    requireUser();
    return { status: 200, body: { gravity: [0.01, 0.02, 0.99], samples: 150 } };
  }
  if (method === "GET" && path === "/api/auth/demo-accounts") {
    return { status: 200, body: ACCOUNTS.map((a) => ({ role: a.role, display_name: a.display_name })) };
  }

  if (method === "GET" && path === "/api/patients") {
    requireUser();
    const status = query.status || "admitted";
    if (!["admitted", "discharged", "all"].includes(status)) throw new ApiError(400, "status must be admitted, discharged, or all");
    return { status: 200, body: wardPayload(status) };
  }
  if (method === "POST" && path === "/api/patients") return createPatient(body);

  const parts = path.split("/").filter(Boolean);
  if (parts[0] === "api" && parts[1] === "patients" && parts[2]) {
    const id = decodeURIComponent(parts[2]);
    if (parts.length === 3 && method === "GET") {
      requireUser();
      return { status: 200, body: publicPatient(mustPatient(id)) };
    }
    if (parts.length === 3 && method === "PATCH") return { status: 200, body: patchPatient(id, body) };
    if (parts.length === 4 && parts[3] === "discharge" && method === "POST") {
      return { status: 200, body: dischargePatient(id) };
    }
    if (parts.length === 4 && parts[3] === "state" && method === "GET") {
      requireUser();
      return { status: 200, body: stateOf(mustPatient(id)) };
    }
    if (parts.length === 4 && parts[3] === "vitals" && method === "GET") {
      requireUser();
      const p = mustPatient(id);
      const t = now();
      const since = query.since_t ? Number(query.since_t) : historyStart(p, t);
      const limit = query.limit ? Number(query.limit) : 720;
      const vitals = [];
      for (let x = Math.max(since, historyStart(p, t)); x <= t && vitals.length < limit; x += 5) vitals.push(vitalsAt(p, x));
      return { status: 200, body: { vitals } };
    }
    if (parts.length === 4 && parts[3] === "events" && method === "GET") {
      requireUser();
      mustPatient(id);
      let events = patientEvents(id);
      if (query.since_t) events = events.filter((e) => e.end_t >= Number(query.since_t));
      const limit = query.limit ? Number(query.limit) : 100;
      return { status: 200, body: { events: events.slice(0, limit) } };
    }
    if (parts.length === 4 && parts[3] === "alerts" && method === "GET") {
      requireUser();
      mustPatient(id);
      const status = query.status || "all";
      let alerts = patientAlerts(id, status);
      if (query.since_t) alerts = alerts.filter((a) => a.raised_t >= Number(query.since_t));
      const limit = query.limit ? Number(query.limit) : 100;
      return { status: 200, body: { alerts: alerts.slice(0, limit) } };
    }
    if (parts.length === 6 && parts[3] === "alerts" && parts[5] === "ack" && method === "POST") {
      return { status: 200, body: ackAlert(id, parts[4], body) };
    }
    if (parts.length === 6 && parts[3] === "summary" && parts[4] === "explain" && method === "POST") {
      requireUser();
      return { status: 200, body: explainSummary(mustPatient(id), parts[5]) };
    }
    if (parts.length === 5 && parts[3] === "summary" && parts[4] === "overnight" && method === "POST") {
      requireUser();
      return { status: 200, body: overnightSummary(mustPatient(id)) };
    }
    if (parts.length === 5 && parts[3] === "summary" && parts[4] === "handoff" && method === "POST") {
      requireUser();
      return { status: 200, body: handoffSummary(mustPatient(id)) };
    }
    if (parts.length === 5 && parts[3] === "summary" && parts[4] === "chart.png" && method === "GET") {
      requireUser();
      mustPatient(id);
      return { status: 200, body: { data_url: chartPng(id) } };
    }
  }

  if (path === "/api/admin/device" && method === "GET") return { status: 200, body: adminDevice() };
  if (path === "/api/source" && method === "POST") return { status: 200, body: setSource(body) };
  if (path === "/api/serial/ports" && method === "GET") {
    requireAdmin();
    return { status: 200, body: SERIAL_PORTS };
  }
  if (path === "/api/record/start" && method === "POST") {
    requireAdmin();
    if (!RECORD_LABELS.includes(body.label)) throw new ApiError(400, "Unknown recording label");
    if (ensure().recording) throw new ApiError(400, "A recording is already running");
    ensure().recording = { label: body.label, started_t: sec(now()) };
    save();
    return { status: 200, body: { recording: adminDevice().recording } };
  }
  if (path === "/api/record/stop" && method === "POST") {
    requireAdmin();
    if (!ensure().recording) throw new ApiError(400, "Not recording");
    const label = ensure().recording.label;
    ensure().recording = null;
    save();
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "");
    return { status: 200, body: { recording: null, file: `data/recordings/${stamp}-${label}.csv` } };
  }
  if (path === "/api/demo/reset" && method === "POST") return { status: 200, body: demoReset(body) };

  throw new ApiError(404, `No mock route ${method} ${path}`);
}

// --------------------------------------------------------------------- summaries

function fmtRate(v) { return v === null || v === undefined ? "no rate" : `${Number(v).toFixed(1)}/min`; }

function explainSummary(p, alertId) {
  const a = ensure().alerts.find((x) => x.id === Number(alertId) && x.patient_id === p.id);
  if (!a) throw new ApiError(404, "No alert with that id for this patient");
  const pub = publicAlert(a);
  const who = pub.acknowledged_by ? ` It was acknowledged by ${pub.acknowledged_by.display_name}.` : " It is still unacknowledged.";
  const esc = pub.escalated
    ? " It was marked escalated after staying unacknowledged for 60 seconds (charge nurse)."
    : "";
  const text = [
    `This note only explains an alert the rule engine already raised. The language model did not choose the tier.`,
    `Bed ${p.bed}, ${p.name}. ${pub.reason}. Tier ${pub.tier} (${pub.kind.replaceAll("_", " ")}).${who}${esc}`,
    `Signal context is whatever the monitor stored with that alert. Risk factors on the admission are context for the reader and do not change thresholds.`,
    `Research prototype — not a diagnosis, and not medication or dosing advice.`,
  ].join("\n\n");
  return summary("explain", p.id, pub.id, text, null, false);
}

function overnightSummary(p) {
  const t = now();
  const v = p.status === "admitted" ? vitalsAt(p, t) : null;
  const base = baselineFor(p, t);
  const text = [
    `Saved copy. The language model could not be reached, so this is the latest cached overnight note.`,
    `Bed ${p.bed}, ${p.name}. Latest breathing rate ${fmtRate(v && v.rr_fused)}` +
      `${base ? ` against a baseline of ${base.rr}/min` : ""}. Heart rate ${v && v.hr != null ? `${v.hr} bpm` : "unavailable"}. Posture ${v ? v.posture : "unknown"}. Signal ${v ? v.quality : "unknown"}.`,
    `The chart and this digest agree on the shape of the rate over the last hour. Nothing here was guessed past those figures.`,
    `Research prototype. This is not a diagnosis.`,
  ].join("\n\n");
  return summary("overnight", p.id, null, text, null, true);
}

function handoffSummary(p) {
  const t = now();
  const v = p.status === "admitted" ? vitalsAt(p, t) : null;
  const tier = tierOf(p.id);
  const base = baselineFor(p, t);
  const risks = [];
  if (p.risk_factors.opioids) risks.push("opioids");
  if (p.risk_factors.supplemental_o2) risks.push("supplemental oxygen");
  if (p.risk_factors.osa !== "none") risks.push(`OSA ${p.risk_factors.osa}`);
  if (p.risk_factors.age_over_65) risks.push("age over 65");
  const riskText = risks.length ? risks.join(", ") : "none listed";
  const situation = `Bed ${p.bed}, ${p.name}, tier ${tier.tier}. ${tier.tier_reason || "No active alert."} Breathing ${fmtRate(v && v.rr_fused)}, heart rate ${v && v.hr != null ? v.hr + " bpm" : "unavailable"}.`;
  const background = `Admitted ${new Date(p.admitted_t * 1000).toLocaleString()}. Context listed on the chart: ${riskText}. ${base ? `Baseline breathing rate ${base.rr}/min.` : "No baseline yet."} ${p.notes || ""}`.trim();
  const assessment = `The monitor reports ${v ? v.quality : "unknown"} signal` +
    `${v && v.flags && v.flags.length ? ` (${v.flags.join(", ")})` : ""}. ` +
    `These sentences describe rule output only. This is not a diagnosis.`;
  const recommendation = `A clinician should review the patient in person and keep monitoring. This research prototype does not recommend medication or dosing changes.`;
  const sbar = { situation, background, assessment, recommendation };
  const text = `Situation: ${situation}\n\nBackground: ${background}\n\nAssessment: ${assessment}\n\nRecommendation: ${recommendation}`;
  return summary("handoff", p.id, null, text, sbar, false);
}

function summary(kind, patientId, alertId, text, sbar, cached) {
  return {
    kind, patient_id: patientId, alert_id: alertId, text, sbar,
    model: "gemini-flash", cached, created_t: sec(now()),
  };
}

function chartPng(id) {
  const p = mustPatient(id);
  const t = now();
  const points = trendSeries(p, t);
  const base = baselineFor(p, t);
  const events = patientEvents(id).slice(0, 8);
  const canvas = document.createElement("canvas");
  canvas.width = 720;
  canvas.height = 280;
  const g = canvas.getContext("2d");
  g.fillStyle = "#f7f5ef";
  g.fillRect(0, 0, 720, 280);
  g.strokeStyle = "#d9d2c5";
  g.strokeRect(0.5, 0.5, 719, 279);
  const left = 48, right = 16, top = 28, bottom = 32;
  const w = 720 - left - right, h = 280 - top - bottom;
  const ys = points.map((pt) => pt[1]).filter((n) => n !== null);
  if (base) ys.push(base.rr);
  let min = 0, max = 20;
  if (ys.length) {
    min = Math.max(0, Math.min(...ys) - 2);
    max = Math.max(...ys) + 2;
  }
  const t0 = points.length ? points[0][0] : t - 3600;
  const t1 = points.length ? points[points.length - 1][0] : t;
  const xOf = (time) => left + ((time - t0) / Math.max(1, t1 - t0)) * w;
  const yOf = (rr) => top + (1 - (rr - min) / Math.max(0.1, max - min)) * h;
  g.strokeStyle = "#e4dfd4";
  g.beginPath();
  for (let rr = 0; rr <= 20; rr += 5) {
    const y = yOf(rr);
    g.moveTo(left, y); g.lineTo(720 - right, y);
    g.fillStyle = "#6d675c";
    g.font = "11px sans-serif";
    g.fillText(String(rr), 8, y + 4);
  }
  g.stroke();
  for (const ev of events) {
    const x = xOf(ev.start_t);
    g.strokeStyle = "#1d5c54";
    g.globalAlpha = 0.35;
    g.beginPath();
    g.moveTo(x, top); g.lineTo(x, top + h);
    g.stroke();
    g.globalAlpha = 1;
  }
  if (base) {
    g.strokeStyle = "#6d675c";
    g.setLineDash([4, 4]);
    g.beginPath();
    g.moveTo(left, yOf(base.rr)); g.lineTo(720 - right, yOf(base.rr));
    g.stroke();
    g.setLineDash([]);
  }
  g.strokeStyle = "#1d5c54";
  g.lineWidth = 1.6;
  g.beginPath();
  let drawing = false;
  for (const [time, rr] of points) {
    if (rr === null) { drawing = false; continue; }
    const x = xOf(time), y = yOf(rr);
    if (!drawing) { g.moveTo(x, y); drawing = true; } else g.lineTo(x, y);
  }
  g.stroke();
  g.fillStyle = "#1c2421";
  g.font = "13px serif";
  g.fillText(`Breathing rate · bed ${p.bed}`, left, 18);
  return canvas.toDataURL("image/png");
}

// --------------------------------------------------------------------- sockets

const sockets = { ward: new Set(), patients: new Map() };
const STREAM0 = Date.now() / 1000;

function emitWard(msg) {
  for (const entry of sockets.ward) if (!entry.closed) entry.handlers.onMessage?.(msg);
}

function emitWardMessage() {
  emitWard(Object.assign({ type: "ward" }, wardPayload("admitted")));
}

function emitPatient(id, msg) {
  const set = sockets.patients.get(id);
  if (!set) return;
  for (const entry of set) if (!entry.closed) entry.handlers.onMessage?.(msg);
}

function closePatientSockets(id, code) {
  const set = sockets.patients.get(id);
  if (!set) return;
  for (const entry of set) {
    entry.closed = true;
    clearInterval(entry.waveTimer);
    clearInterval(entry.slowTimer);
    entry.handlers.onClose?.(code);
  }
  sockets.patients.delete(id);
}

function restartPatientStream(id) {
  const p = findPatient(id);
  if (!p) return;
  emitPatient(id, { type: "reset" });
  emitPatient(id, {
    type: "hello",
    patient: publicPatient(p),
    state: stateOf(p),
    trend: trendSeries(p, now()),
    window_s: 30,
    trend_window_s: 3600,
  });
}

function broadcastAfterReset() {
  for (const [id, set] of [...sockets.patients]) {
    const exists = findPatient(id);
    if (!exists) {
      for (const entry of set) {
        entry.closed = true;
        clearInterval(entry.waveTimer);
        clearInterval(entry.slowTimer);
        entry.handlers.onClose?.(4404);
      }
      sockets.patients.delete(id);
    } else {
      restartPatientStream(id);
    }
  }
  emitWardMessage();
}

function mockSocket(path, handlers) {
  ensure();
  if (!currentUser()) {
    setTimeout(() => handlers.onClose?.(4401), 0);
    return { close() {} };
  }
  if (path === "/ws/ward") {
    const entry = { handlers, closed: false, timer: null };
    sockets.ward.add(entry);
    setTimeout(() => { if (!entry.closed) handlers.onMessage?.(Object.assign({ type: "ward" }, wardPayload("admitted"))); }, 0);
    entry.timer = setInterval(() => {
      if (!entry.closed) handlers.onMessage?.(Object.assign({ type: "ward" }, wardPayload("admitted")));
    }, 2000);
    return { close() { entry.closed = true; clearInterval(entry.timer); sockets.ward.delete(entry); } };
  }
  const match = /^\/ws\/patients\/([^/]+)$/.exec(path);
  if (!match) {
    setTimeout(() => handlers.onClose?.(1002), 0);
    return { close() {} };
  }
  const id = decodeURIComponent(match[1]);
  const patient = findPatient(id);
  if (!patient) {
    setTimeout(() => handlers.onClose?.(4404), 0);
    return { close() {} };
  }
  const entry = {
    handlers, closed: false,
    cursor: Math.floor((now() - STREAM0) * 25),
    waveTimer: null, slowTimer: null, ticks: 0,
  };
  if (!sockets.patients.has(id)) sockets.patients.set(id, new Set());
  sockets.patients.get(id).add(entry);
  setTimeout(() => {
    if (entry.closed) return;
    const p = findPatient(id);
    if (!p) return;
    handlers.onMessage?.({
      type: "hello",
      patient: publicPatient(p),
      state: stateOf(p),
      trend: trendSeries(p, now()),
      window_s: 30,
      trend_window_s: 3600,
    });
  }, 0);
  entry.waveTimer = setInterval(() => pushWave(id, entry), 100);
  entry.slowTimer = setInterval(() => pushSlow(id, entry), 1000);
  return {
    close() {
      entry.closed = true;
      clearInterval(entry.waveTimer);
      clearInterval(entry.slowTimer);
      const set = sockets.patients.get(id);
      if (set) set.delete(entry);
    },
  };
}

function pushWave(id, entry) {
  if (entry.closed) return;
  const p = findPatient(id);
  if (!p || p.status !== "admitted") return;
  const end = Math.floor((now() - STREAM0) * 25);
  if (end <= entry.cursor) return;
  let first = entry.cursor + 1;
  if (end - first > 24) first = end - 24;
  const n = end - first + 1;
  const effort = [], airflow = [], pulse = [];
  for (let k = 0; k < n; k++) {
    const raw = (first + k) * 2;
    const t = STREAM0 + raw / 50;
    effort.push(sampleChannel(p, t, "effort"));
    airflow.push(sampleChannel(p, t, "airflow"));
    pulse.push(sampleChannel(p, t, "pulse"));
  }
  entry.cursor = end;
  entry.handlers.onMessage?.({
    type: "wave", start: first * 2, step: 2, fs: 50, end: (end + 1) * 2,
    t_end: sec(STREAM0 + ((end + 1) * 2) / 50),
    effort, airflow, pulse,
  });
}

function pushSlow(id, entry) {
  if (entry.closed) return;
  const p = findPatient(id);
  if (!p) return;
  entry.ticks += 1;
  const t = now();
  if (p.status === "admitted") {
    entry.handlers.onMessage?.({
      type: "status", device: deviceStatus(p, t), tier: tierOf(p.id).tier, monitors: monitorsFor(p, t),
    });
    if (entry.ticks % 5 === 0) {
      syncGenerated(true);
      const v = vitalsAt(p, t);
      entry.handlers.onMessage?.({ type: "vitals", vitals: v, trend: [[sec(t), v.rr_fused]] });
    }
  }
}
