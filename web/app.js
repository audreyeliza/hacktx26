import {
  $, api, boot, esc, eventLabel, fmt, fmtTime, liveSocket, postureLabel, qualityLabel, scenarioLabel, tierName,
} from "/static/common.js";

const GLOSS = ["No active alert", "Review on rounds", "Needs attention", "Bedside response"];

await boot();

const tiers = $("tiers");
tiers.innerHTML = [0, 1, 2, 3].map((tier) => `
  <div class="tier-count" data-tier="${tier}" data-count="${tier}">
    <span class="tier-count__name">${tierName(tier)}</span>
    <b>0</b>
    <span class="fine">${GLOSS[tier]}</span>
  </div>`).join("");

function paint(payload) {
  for (const tier of [0, 1, 2, 3]) {
    const node = tiers.querySelector(`[data-count="${tier}"] b`);
    if (node) node.textContent = String(payload.counts[String(tier)] ?? 0);
  }
  const board = $("board");
  const ids = payload.patients.map((p) => p.id).join(",");
  if (board.dataset.ids !== ids) {
    board.dataset.ids = ids;
    const head = `<div class="census-head" aria-hidden="true"><span>Bed</span><span>Patient</span><span>Resp.</span><span>Heart</span><span>Posture</span><span>Signal</span><span>Tier</span></div>`;
    const rows = payload.patients.map(() => `
      <a class="census" href="/">
        <span class="census__bed"><small>Bed</small><b data-f="bed"></b></span>
        <span class="census__who">
          <span class="census__name" data-f="name"></span>
          <span class="census__meta" data-f="meta"></span>
          <span class="census__reason" data-f="reason"></span>
        </span>
        <span class="census__stat census__rr"><b data-f="rr"></b> <small>/min</small><small data-f="conf"></small></span>
        <span class="census__stat census__hr"><b data-f="hr"></b> <small>bpm</small></span>
        <span class="census__posture" data-f="posture"></span>
        <span class="census__quality" data-f="quality"></span>
        <span class="census__tier"><span data-f="tier"></span><small data-f="unacked"></small></span>
      </a>`).join("");
    board.innerHTML = payload.patients.length ? head + rows : `<p class="empty" style="padding:16px">No patients in this list.</p>`;
  }
  const rows = board.querySelectorAll(".census");
  payload.patients.forEach((p, i) => fill(rows[i], p));
  const n = payload.patients.length;
  $("lede").textContent = `${n} admitted ${n === 1 ? "patient" : "patients"}, highest alert tier first.`;
  $("generated").textContent = payload.generated_t ? `Updated ${fmtTime(payload.generated_t)}` : "";
}

function fill(row, p) {
  if (!row) return;
  row.dataset.tier = String(p.tier);
  row.href = `/patients/${encodeURIComponent(p.id)}`;
  row.setAttribute("aria-label", `Bed ${p.bed}, ${p.name}, ${tierName(p.tier)}, breathing ${p.rr ?? "unavailable"} per minute`);
  text(row, "bed", p.bed);
  text(row, "name", p.name);
  const assign = p.device_kind === "live"
    ? (p.link_ok ? "Live wearable · link up" : "Live wearable · link lost")
    : `Simulated · ${scenarioLabel(p.scenario)}`;
  const last = p.last_event ? `${eventLabel(p.last_event.type)} · ${Math.round(p.last_event.duration_s)} s` : "No events";
  text(row, "meta", `${assign} · ${last}`);
  const reason = row.querySelector('[data-f="reason"]');
  reason.hidden = !p.tier_reason;
  reason.textContent = p.tier_reason || "";
  text(row, "rr", fmt(p.rr, 1));
  const conf = row.querySelector('[data-f="conf"]');
  conf.textContent = p.rr_conf == null ? "" : ` conf ${fmt(p.rr_conf, 2)}`;
  text(row, "hr", fmt(p.hr, 0));
  text(row, "posture", postureLabel(p.posture));
  const q = row.querySelector('[data-f="quality"]');
  q.textContent = qualityLabel(p.quality);
  q.className = `census__quality q-${p.quality || "unknown"}`;
  text(row, "tier", tierName(p.tier));
  text(row, "unacked", p.unacked_alerts ? `${p.unacked_alerts} unacknowledged` : "");
}

function text(row, field, value) {
  const node = row.querySelector(`[data-f="${field}"]`);
  if (node) node.textContent = value;
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
