import {
  $, admissionFields, api, boot, canEdit, esc, eventLabel, fmt, fmtTime, fmtWhen,
  liveSocket, patientIdFromLocation, postureLabel, qualityLabel, readAdmission, riskHtml,
  scenarioLabel, showError, tierName, USE_MOCK,
} from "/static/common.js";

const id = patientIdFromLocation();

function missing() {
  $("missing").hidden = false;
  $("detail-root").hidden = true;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function waveChart(canvasId, color) {
  return new Chart($(canvasId), {
    type: "line",
    data: { datasets: [{ data: [], borderColor: color, borderWidth: 1.4, pointRadius: 0, spanGaps: false, tension: 0.15 }] },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false, parsing: false, normalized: true,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: {
        x: { type: "linear", ticks: { display: false }, grid: { display: false }, border: { display: false } },
        y: { ticks: { maxTicksLimit: 3, font: { size: 10 } }, grid: { color: "rgba(28,36,33,0.06)" } },
      },
    },
  });
}

const waves = {
  effort: waveChart("wave-effort", cssVar("--effort")),
  airflow: waveChart("wave-airflow", cssVar("--airflow")),
  pulse: waveChart("wave-pulse", cssVar("--pulse")),
};
let windowS = 30;
let trendWindowS = 3600;
let trendPoints = [];
let baselineRr = null;
const trend = new Chart($("trend"), {
  type: "line",
  data: { datasets: [
    { label: "Breathing rate", data: [], borderColor: cssVar("--teal"), borderWidth: 1.6, pointRadius: 0, spanGaps: false },
    { label: "Baseline", data: [], borderColor: cssVar("--muted"), borderDash: [4, 4], borderWidth: 1, pointRadius: 0 },
  ] },
  options: {
    animation: false, responsive: true, maintainAspectRatio: false, parsing: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { type: "linear", title: { display: true, text: "minutes ago" }, ticks: { maxTicksLimit: 6 } },
      y: { suggestedMin: 0, suggestedMax: 20, title: { display: true, text: "breaths/min" } },
    },
  },
});

function clearWaves() {
  for (const chart of Object.values(waves)) chart.data.datasets[0].data = [];
  for (const chart of Object.values(waves)) chart.update("none");
}

function onWave(msg) {
  const dt = msg.step / msg.fs;
  const t0 = msg.start / msg.fs;
  const tEnd = msg.end / msg.fs;
  let any = false;
  for (const [name, chart] of Object.entries(waves)) {
    const data = chart.data.datasets[0].data;
    const values = msg[name] || [];
    for (let i = 0; i < values.length; i++) {
      const y = values[i];
      if (y !== null) any = true;
      data.push({ x: t0 + i * dt, y: y === null ? NaN : y });
    }
    const cutoff = tEnd - windowS;
    let drop = 0;
    while (drop < data.length && data[drop].x < cutoff) drop++;
    if (drop) data.splice(0, drop);
    chart.options.scales.x.min = cutoff;
    chart.options.scales.x.max = tEnd;
    chart.update("none");
  }
  if (any) $("link-note").hidden = true;
}

function drawTrend() {
  const line = trend.data.datasets[0].data;
  const base = trend.data.datasets[1].data;
  line.length = 0;
  base.length = 0;
  if (trendPoints.length) {
    const tLast = trendPoints[trendPoints.length - 1][0];
    trendPoints = trendPoints.filter(([t]) => t >= tLast - trendWindowS);
    for (const [t, rr] of trendPoints) line.push({ x: (t - tLast) / 60, y: rr === null ? NaN : rr });
    if (baselineRr !== null && trendPoints.length) {
      const x0 = (trendPoints[0][0] - tLast) / 60;
      base.push({ x: x0, y: baselineRr }, { x: 0, y: baselineRr });
    }
  }
  trend.update("none");
}

let alertRows = [];
let alertSig = "";
let currentTier = 0;
let discharged = false;
let editBuilt = false;

async function openPatient(patientId, currentUser) {
  let state;
  try {
    state = await api(`/api/patients/${patientId}/state`);
  } catch (err) {
    if (err.status === 404) { missing(); return; }
    throw err;
  }
  const [alerts, events] = await Promise.all([
    api(`/api/patients/${patientId}/alerts`, { query: { status: "unacked" } }),
    api(`/api/patients/${patientId}/events`),
  ]);
  applyState(state);
  setAlerts(alerts.alerts);
  setEvents(events.events);
  if (!editBuilt) {
    editBuilt = true;
    await buildAdmission(state.patient, currentUser);
  }
  $("cal-btn").addEventListener("click", calibrateSupine);
  $("sum-explain").addEventListener("click", () => runSummary("explain"));
  $("sum-overnight").addEventListener("click", () => runSummary("overnight"));
  $("sum-handoff").addEventListener("click", () => runSummary("handoff"));
  $("alerts").addEventListener("submit", onAck);

  liveSocket(`/ws/patients/${encodeURIComponent(patientId)}`, {
    onMessage(msg) { onSocket(msg, patientId); },
    onTerminal(code) {
      if (code === 4404) missing();
    },
  });
}

function onSocket(msg, patientId) {
  if (msg.type === "hello") {
    windowS = msg.window_s || windowS;
    trendWindowS = msg.trend_window_s || trendWindowS;
    applyState(msg.state);
    trendPoints = (msg.trend || []).slice();
    drawTrend();
  } else if (msg.type === "reset") {
    clearWaves();
    trendPoints = [];
    drawTrend();
  } else if (msg.type === "wave") onWave(msg);
  else if (msg.type === "vitals") {
    applyVitals(msg.vitals);
    if (msg.trend) trendPoints.push(...msg.trend);
    drawTrend();
  } else if (msg.type === "event") {
    api(`/api/patients/${patientId}/events`).then((res) => setEvents(res.events)).catch(() => {});
  } else if (msg.type === "alert") {
    refreshClinical(patientId);
  } else if (msg.type === "status") {
    applyDevice(msg.device);
    applyMonitors(msg.monitors);
    if (msg.tier !== currentTier) refreshClinical(patientId);
    else setTier(msg.tier, $("pt-reason").textContent || null);
  } else if (msg.type === "discharged") markDischarged(msg.patient);
}

async function refreshClinical(patientId) {
  const [state, alerts, events] = await Promise.all([
    api(`/api/patients/${patientId}/state`),
    api(`/api/patients/${patientId}/alerts`, { query: { status: "unacked" } }),
    api(`/api/patients/${patientId}/events`),
  ]);
  applyState(state);
  setAlerts(alerts.alerts);
  setEvents(events.events);
}

function applyState(state) {
  const p = state.patient;
  document.title = `${p.name} · BreathWatch`;
  $("pt-bed").textContent = `Bed ${p.bed}`;
  $("pt-name").textContent = p.name;
  $("pt-risk").innerHTML = riskHtml(p.risk_factors);
  $("pt-notes").textContent = p.notes || "No admission note.";
  applyDevice(state.device);
  setTier(state.tier, state.tier_reason);
  applyVitals(state.vitals);
  baselineRr = state.baseline ? state.baseline.rr : null;
  $("baseline-note").textContent = state.baseline
    ? `Dashed line is this patient's baseline, ${fmt(state.baseline.rr, 1)}/min.`
    : "No baseline yet — not enough clean data.";
  applyMonitors(state.monitors);
  if (p.status === "discharged") markDischarged(p);
}

function applyDevice(device) {
  if (!device) return;
  const label = device.kind === "live"
    ? `Live · ${device.device_id} · ${device.link_ok ? "link up" : "link lost"}`
    : `Simulated · ${scenarioLabel(device.scenario)}`;
  $("pt-assign").textContent = `${label}. ${device.source_status || ""}`;
  $("link-note").hidden = !(device.kind === "live" && !device.link_ok);
  $("calibrate").hidden = device.kind !== "live";
}

function setTier(tier, reason) {
  currentTier = tier;
  const name = tierName(tier);
  $("pt-tier").textContent = name;
  $("pt-tier").dataset.tier = String(tier);
  $("tile").dataset.tier = String(tier);
  const text = reason && reason !== "null" ? reason : "";
  if (reason !== undefined) $("pt-reason").textContent = text || (tier === 0 ? "No active alert." : "");
}

function applyVitals(v) {
  if (!v) {
    $("rr").textContent = "—";
    $("hr").textContent = "—";
    $("posture").textContent = "—";
    $("quality").textContent = "—";
    $("rr-detail").textContent = "Waiting for a reading.";
    return;
  }
  $("rr").textContent = fmt(v.rr_fused, 1);
  $("hr").textContent = fmt(v.hr, 0);
  $("posture").textContent = postureLabel(v.posture);
  const q = $("quality");
  q.textContent = qualityLabel(v.quality);
  q.className = `badge q-${v.quality || "unknown"}`;
  const parts = [];
  if (v.rr_conf != null) parts.push(`confidence ${fmt(v.rr_conf, 2)}`);
  if (v.flags && v.flags.length) parts.push(v.flags.join(", "));
  parts.push(["belt", "accel", "airflow"].map((s) => `${s} ${fmt(v["rr_" + s], 1)}`).join(" · "));
  if (v.rr_prelim != null) parts.push(`30 s ${fmt(v.rr_prelim, 1)}`);
  $("rr-detail").textContent = parts.join("  ·  ");
}

function applyMonitors(m) {
  if (!m) return;
  $("m-smart").textContent = String(m.smart.alarms);
  $("m-naive").textContent = String(m.naive.alarms);
  $("m-smart-rate").textContent = `${fmt(m.smart.per_hour, 1)} / hour`;
  $("m-naive-rate").textContent = `${fmt(m.naive.per_hour, 1)} / hour`;
  $("m-since").textContent = `Since ${fmtWhen(m.since_t)}. The naive monitor uses fixed thresholds and does not check signal quality.`;
}

function setAlerts(list) {
  alertRows = list.slice().sort((a, b) => b.raised_t - a.raised_t);
  const sig = alertRows.map((a) => `${a.id}:${a.status}:${a.tier}:${a.escalated}:${a.reason}:${a.ack_note || ""}`).join("|");
  $("sum-explain").disabled = alertRows.length === 0;
  if (sig === alertSig) return;
  alertSig = sig;
  const root = $("alerts");
  if (!alertRows.length) {
    root.innerHTML = `<p class="empty">No unacknowledged alerts.</p>`;
    return;
  }
  root.innerHTML = alertRows.map((a) => {
    const waiting = a.status === "awaiting_ack";
    const canAck = a.status === "active" || waiting;
    return `
    <article class="alert" data-tier="${a.tier}" data-status="${esc(a.status)}">
      <div class="alert__top">
        <span class="pill">${esc(tierName(a.tier))}</span>
        <span class="fine">${esc(fmtWhen(a.raised_t))}</span>
        <span class="fine${waiting ? " awaiting" : ""}">${waiting ? "Resolved, awaiting acknowledgment" : "Active"}</span>
      </div>
      <p>${esc(a.reason)}</p>
      ${a.tier === 3 && a.escalated ? `<p class="escalated">Escalated to charge nurse</p>` : ""}
      ${canAck ? `<form class="ack"><input name="note" placeholder="Note (optional)" aria-label="Acknowledgement note"><button class="primary" type="submit" data-id="${a.id}">Acknowledge</button></form>` : ""}
    </article>`;
  }).join("");
}

function setEvents(list) {
  const root = $("events");
  if (!list.length) {
    root.innerHTML = `<li class="empty">No breathing events.</li>`;
    return;
  }
  root.innerHTML = list.map((ev) => `
    <li>
      <span class="ev ev-${esc(ev.type)}">${esc(eventLabel(ev.type))}</span>
      <span>${esc(Math.round(ev.duration_s))} s</span>
      <span>${esc(postureLabel(ev.posture))}</span>
      <span class="fine">${esc(fmtTime(ev.start_t))}–${esc(fmtTime(ev.end_t))}</span>
      <span class="fine">airflow ${esc(ev.airflow_pct)}% · effort ${esc(ev.effort_pct)}% · conf ${esc(fmt(ev.confidence, 2))}</span>
    </li>`).join("");
}

async function onAck(ev) {
  const form = ev.target;
  if (!(form instanceof HTMLFormElement)) return;
  ev.preventDefault();
  const btn = form.querySelector("button");
  btn.disabled = true;
  try {
    await api(`/api/patients/${id}/alerts/${btn.dataset.id}/ack`, {
      method: "POST",
      body: { note: new FormData(form).get("note") || "" },
    });
    alertSig = "";
    await refreshClinical(id);
  } catch (err) {
    btn.disabled = false;
    showError($("edit-error"), err);
  }
}

async function runSummary(kind) {
  showError($("ai-error"), null);
  $("ai-status").textContent = "Writing…";
  for (const btn of document.querySelectorAll(".ai-actions button")) btn.disabled = true;
  try {
    let path = `/api/patients/${id}/summary/${kind}`;
    if (kind === "explain") {
      if (!alertRows.length) return;
      path = `/api/patients/${id}/summary/explain/${alertRows[0].id}`;
    }
    const summary = await api(path, { method: "POST" });
    renderSummary(summary);
    if (kind === "explain") $("chart-img").hidden = true;
    else await showChart();
  } catch (err) {
    if (err.status === 503) {
      showError($("ai-error"), {
        detail: "The summary could not be written. The language model is unreachable, and there is no saved copy yet.",
      });
    } else {
      showError($("ai-error"), err);
    }
  } finally {
    $("ai-status").textContent = "";
    $("sum-overnight").disabled = false;
    $("sum-handoff").disabled = false;
    $("sum-explain").disabled = alertRows.length === 0;
  }
}

function renderSummary(summary) {
  // Handoff (SBAR): the four cards plus the chart and discrepancies lines only — the raw text
  // repeats the same content, so it is not shown. Other summaries show their text.
  const s = summary.sbar;
  let body;
  if (s) {
    const disc = Array.isArray(s.discrepancies) ? (s.discrepancies.join("; ") || "none") : null;
    body = `
    <div class="sbar">
      ${["situation", "background", "assessment", "recommendation"].map((key) => `
        <div><h3>${key}</h3><p>${esc(s[key] || "")}</p></div>`).join("")}
    </div>
    ${s.chart_observation ? `<p class="summary-text"><strong>Chart:</strong> ${esc(s.chart_observation)}</p>` : ""}
    ${disc !== null ? `<p class="summary-text"><strong>Chart vs data discrepancies:</strong> ${esc(disc)}</p>` : ""}`;
  } else {
    body = `<p class="summary-text">${esc(summary.text || "")}</p>`;
  }
  $("summary").innerHTML = `
    ${summary.cached ? `<p class="chip">Saved copy — the language model was unreachable</p>` : ""}
    ${body}
    <p class="model-line">${esc(modelLine(summary))}</p>`;
}

function modelLine(summary) {
  const model = summary.model ? `Answered by ${summary.model}` : "Answered by Gemini";
  const saved = summary.cached ? " · saved copy" : "";
  return `${model}${saved} · ${fmtWhen(summary.created_t)}`;
}

async function calibrateSupine() {
  const btn = $("cal-btn");
  showError($("cal-error"), null);
  btn.disabled = true;
  $("cal-note").textContent = "Recording gravity while you lie flat…";
  try {
    const res = await api("/api/calibrate/supine", { method: "POST", body: {} });
    $("cal-note").textContent = calibrateNote(res);
  } catch (err) {
    $("cal-note").textContent = "Lie flat for a few seconds. Posture is then measured from that position.";
    showError($("cal-error"), err);
  } finally {
    btn.disabled = false;
  }
}

function calibrateNote(res) {
  const g = res && (res.gravity || res.vector);
  if (Array.isArray(g) && g.length >= 3) {
    const nums = g.slice(0, 3).map((n) => Number(n).toFixed(2)).join(", ");
    return `Supine recorded (${nums} g). Posture is now measured from this position.`;
  }
  if (res && typeof res.detail === "string") return res.detail;
  return "Supine recorded. Posture is now measured from this position.";
}

async function showChart() {
  const img = $("chart-img");
  if (USE_MOCK) {
    const res = await api(`/api/patients/${id}/summary/chart.png`);
    img.src = res.data_url;
  } else {
    img.src = `/api/patients/${id}/summary/chart.png?t=${Date.now()}`;
  }
  img.hidden = false;
}

async function buildAdmission(patient, currentUser) {
  if (!canEdit(currentUser)) {
    $("edit").hidden = true;
    $("discharge-box").hidden = true;
    $("role-note").hidden = false;
    return;
  }
  const ward = await api("/api/patients?status=admitted");
  const live = ward.patients.find((p) => p.device_kind === "live") || null;
  const form = $("edit-form");
  form.innerHTML = admissionFields(patient, live) + `<div class="form-actions"><button class="primary" type="submit">Save admission</button></div>`;
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    showError($("edit-error"), null);
    try {
      await api(`/api/patients/${id}`, { method: "PATCH", body: readAdmission(form) });
      location.reload();
    } catch (err) {
      showError($("edit-error"), err);
    }
  });
  $("discharge-box").innerHTML = `
    <div class="form-actions"><button type="button" class="quiet" id="discharge-btn">Discharge</button></div>
    <div id="discharge-confirm" hidden>
      <p>Discharge this patient? Monitoring stops. Stored history is kept.</p>
      <div class="form-actions">
        <button type="button" class="primary" id="discharge-yes">Discharge</button>
        <button type="button" class="quiet" id="discharge-no">Cancel</button>
      </div>
    </div>`;
  $("discharge-btn").addEventListener("click", () => { $("discharge-confirm").hidden = false; });
  $("discharge-no").addEventListener("click", () => { $("discharge-confirm").hidden = true; });
  $("discharge-yes").addEventListener("click", async () => {
    try {
      const updated = await api(`/api/patients/${id}/discharge`, { method: "POST", body: {} });
      markDischarged(updated);
    } catch (err) {
      showError($("edit-error"), err);
    }
  });
}

function markDischarged(patient) {
  if (discharged) return;
  discharged = true;
  $("pt-status").hidden = false;
  $("pt-status").textContent = `${patient?.name || "This patient"} is discharged and off the ward list. History is kept.`;
  $("edit").hidden = true;
  $("discharge-box").hidden = true;
  $("link-note").hidden = false;
  $("link-note").textContent = "Monitoring stopped.";
}

const user = await boot();
if (!id) missing();
else await openPatient(id, user);
