import {
  $, api, boot, esc, eventLabel, fmt, liveSocket, postureLabel, tierName,
} from "/static/common.js";

await boot();

const TIERS = [3, 2, 1, 0];
let filter = null;
let latest = null;
const previousRr = new Map();

const tiers = $("tiers");
tiers.innerHTML = TIERS.map((tier) => `
  <button type="button" class="tier-chip" data-tier="${tier}" aria-pressed="false">
    <span class="tier-chip__dot" aria-hidden="true"></span>
    <span>${tierName(tier)}</span>
    <b>0</b>
  </button>`).join("");
tiers.addEventListener("click", (ev) => {
  const btn = ev.target.closest(".tier-chip");
  if (!btn) return;
  const tier = Number(btn.dataset.tier);
  filter = filter === tier ? null : tier;
  for (const chip of tiers.querySelectorAll(".tier-chip")) {
    chip.setAttribute("aria-pressed", String(filter !== null && Number(chip.dataset.tier) === filter));
  }
  if (latest) paint(latest);
});

function signalOf(p) {
  if (p.device_kind === "live" && p.link_ok === false && p.rr == null) {
    return { kind: "off", rate: "No reading", hint: "Check sensor" };
  }
  if (p.quality === "sensor_off") return { kind: "off", rate: "No reading", hint: "Sensor off" };
  if (p.rr == null || p.quality == null || p.quality === "unknown") {
    return { kind: "starting", rate: "Starting up", hint: "First reading in about 15 s" };
  }
  if (p.quality === "motion" || (p.rr_conf != null && p.rr_conf < 0.6)) {
    return { kind: "low", rate: fmt(p.rr, 0), hint: "Low confidence" };
  }
  return { kind: "good", rate: fmt(p.rr, 0), hint: "" };
}

function trendArrow(id, rr) {
  const prev = previousRr.get(id);
  if (rr != null) previousRr.set(id, rr);
  if (prev == null || rr == null) return "";
  if (rr > prev + 0.4) return "↑";
  if (rr < prev - 0.4) return "↓";
  return "";
}

function situation(p, signal) {
  if (p.tier_reason) return p.tier_reason;
  if (signal.kind === "off") return "No reading — check sensor";
  if (signal.kind === "starting") return "Starting up — first reading in about 15 s";
  if (p.last_event) {
    return `${eventLabel(p.last_event.type)}, ${Math.round(p.last_event.duration_s)} s`;
  }
  return "No active alert.";
}

function paint(payload) {
  latest = payload;
  for (const tier of TIERS) {
    const node = tiers.querySelector(`[data-tier="${tier}"] b`);
    if (node) node.textContent = String(payload.counts[String(tier)] ?? 0);
  }
  const shift = payload.shift;
  $("shift-label").textContent = shift && shift.label ? shift.label : "This shift";
  const counts = $("shift-counts");
  const rates = $("shift-rates");
  if (shift && shift.smart && shift.naive) {
    counts.textContent = `${shift.smart.alarms} vs ${shift.naive.alarms} this shift`;
    const showRates = shift.rates_available === true
      && shift.smart.per_hour != null && shift.naive.per_hour != null;
    rates.hidden = !showRates;
    rates.textContent = showRates
      ? `${fmt(shift.smart.per_hour, 1)} vs ${fmt(shift.naive.per_hour, 1)} per hour`
      : "";
  } else {
    counts.textContent = "—";
    rates.hidden = true;
    rates.textContent = "";
  }

  const shown = filter == null ? payload.patients : payload.patients.filter((p) => p.tier === filter);
  const n = payload.patients.length;
  $("lede").textContent = `${n} ${n === 1 ? "patient" : "patients"}`;

  const board = $("board");
  const head = `<div class="census-head" aria-hidden="true"><span>Bed</span><span>Patient</span><span>Breathing</span><span>Heart</span><span>Position</span><span>Status</span></div>`;
  if (!shown.length) {
    board.innerHTML = head + `<p class="empty census-empty">${filter == null ? "No patients in this list." : `No ${tierName(filter).toLowerCase()} patients.`}</p>`;
    return;
  }
  board.innerHTML = head + shown.map((p) => row(p)).join("");
}

function row(p) {
  const signal = signalOf(p);
  const arrow = trendArrow(p.id, p.rr);
  const live = p.device_kind === "live";
  const tag = live ? `<span class="tag tag-live">Live sensor</span>` : `<span class="tag">Simulated</span>`;
  const warn = signal.kind === "good" ? "" : `<span class="sig-warn" title="${esc(signal.hint || signal.kind)}">!</span>`;
  const rate = signal.kind === "good" || signal.kind === "low"
    ? `<b>${esc(signal.rate)}</b> <small>/min</small> ${arrow ? `<span class="trend-arrow">${arrow}</span>` : ""} ${warn}`
    : `<b class="rate-note">${esc(signal.rate)}</b> ${warn}`;
  const heart = p.hr == null ? "—" : fmt(p.hr, 0);
  const ack = p.unacked_alerts ? `${p.unacked_alerts} to ack.` : "";
  return `
    <a class="census" data-tier="${p.tier}" href="/patients/${encodeURIComponent(p.id)}" aria-label="Bed ${esc(p.bed)}, ${esc(p.name)}, ${esc(tierName(p.tier))}">
      <span class="census__bed"><span>Bed</span><b>${esc(p.bed)}</b></span>
      <span class="census__who">
        <span class="census__name">${esc(p.name)} ${tag}</span>
        <span class="census__reason">${esc(situation(p, signal))}</span>
      </span>
      <span class="census__stat census__rr">${rate}</span>
      <span class="census__stat census__hr"><b>${esc(heart)}</b></span>
      <span class="census__posture">${esc(postureLabel(p.posture))}</span>
      <span class="census__tier"><span>${esc(tierName(p.tier))}</span><small>${esc(ack)}</small></span>
    </a>`;
}

try {
  paint(await api("/api/patients"));
} catch (err) {
  $("board").innerHTML = `<p class="notice">${esc(err.detail || err.message)}</p>`;
}

liveSocket("/ws/ward", {
  onMessage(msg) {
    if (msg.type === "ward") paint(msg);
    if (msg.type === "alert") api("/api/patients").then(paint).catch(() => {});
  },
});
