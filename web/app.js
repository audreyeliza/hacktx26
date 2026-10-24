// BreathWatch dashboard — minimal live view. No build step; Chart.js is vendored.
"use strict";

const $ = (id) => document.getElementById(id);
let windowS = 30;

// ---------------------------------------------------------------- charts
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function waveChart(canvasId, color) {
  return new Chart($(canvasId), {
    type: "line",
    data: { datasets: [{ data: [], borderColor: color, borderWidth: 1.2, pointRadius: 0, spanGaps: false }] },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false, parsing: false, normalized: true,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: {
        x: { type: "linear", ticks: { display: false }, grid: { display: false } },
        y: { ticks: { maxTicksLimit: 3, font: { size: 9 } } },
      },
    },
  });
}

const waves = {
  effort: waveChart("wave-effort", cssVar("--wave-effort")),
  airflow: waveChart("wave-airflow", cssVar("--wave-airflow")),
  pulse: waveChart("wave-pulse", cssVar("--wave-pulse")),
};

const trend = new Chart($("trend"), {
  type: "line",
  data: { datasets: [
    { label: "rr_fused", data: [], borderColor: cssVar("--wave-effort"), borderWidth: 1.5, pointRadius: 0, spanGaps: false },
  ] },
  options: {
    animation: false, responsive: true, maintainAspectRatio: false, parsing: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { type: "linear", title: { display: true, text: "minutes" }, ticks: { maxTicksLimit: 7 } },
      y: { suggestedMin: 0, suggestedMax: 20, title: { display: true, text: "breaths/min" } },
    },
  },
});

function clearWaves() {
  for (const c of Object.values(waves)) c.data.datasets[0].data = [];
}

function onWave(msg) {
  const dt = msg.step / msg.fs;
  const t0 = msg.start / msg.fs;
  const tEnd = msg.end / msg.fs;
  for (const [name, chart] of Object.entries(waves)) {
    const data = chart.data.datasets[0].data;
    const values = msg[name];
    for (let i = 0; i < values.length; i++) {
      data.push({ x: t0 + i * dt, y: values[i] === null ? NaN : values[i] });
    }
    const cutoff = tEnd - windowS;
    let drop = 0;
    while (drop < data.length && data[drop].x < cutoff) drop++;
    if (drop) data.splice(0, drop);
    chart.options.scales.x.min = cutoff;
    chart.options.scales.x.max = tEnd;
    chart.update("none");
  }
}

// ---------------------------------------------------------------- vitals
function fmt(v, digits = 0) {
  return v === null || v === undefined ? "—" : Number(v).toFixed(digits);
}

function onVitals(v) {
  $("rr").textContent = fmt(v.rr_fused);
  const parts = [];
  if (v.rr_conf !== null && v.rr_conf !== undefined) parts.push(`conf ${fmt(v.rr_conf, 2)}`);
  if (v.flags && v.flags.length) parts.push(v.flags.join(", "));
  const src = ["belt", "accel", "airflow"]
    .map((s) => `${s} ${fmt(v["rr_" + s])}`).join(" · ");
  parts.push(src);
  if (v.rr_prelim !== null && v.rr_prelim !== undefined) parts.push(`30 s: ${fmt(v.rr_prelim)}`);
  $("rr-detail").textContent = parts.join(" | ");
  $("hr").textContent = fmt(v.hr);
  if (v.posture) $("posture").textContent = v.posture;
  if (v.quality) $("quality").textContent = v.quality;
}

function setTrend(points) {
  // points: [[epoch_s, rr_fused|null], ...]; x shown as minutes relative to the newest point
  const data = trend.data.datasets[0].data;
  data.length = 0;
  if (!points.length) { trend.update("none"); return; }
  const tLast = points[points.length - 1][0];
  for (const [t, rr] of points) data.push({ x: (t - tLast) / 60, y: rr === null ? NaN : rr });
  trend.update("none");
}

function onStatus(s) {
  const ing = s.ingest;
  const st = ing.stats;
  const link = ing.link_ok ? "receiving" : "no data";
  const ips = (s.network && s.network.ips) || [];
  const net = ips.length
    ? ` · open from a phone: ${ips.map((ip) => `http://${ip}:${s.network.http_port}`).join(" or ")}` +
      ` (Pico SERVER_IP = the address on the Pico's network)`
    : "";
  $("status").textContent =
    `${ing.source} (${ing.source_status}) · ${link} · packets ${st.packets} · gaps ${st.gaps}` +
    ` (${st.missing_packets} missing) · invalid ${st.invalid}` +
    (st.device ? ` · ${st.device}` : "") + net;
  if (s.vitals) onVitals(s.vitals);
  if (s.trend) setTrend(s.trend);
}

// ---------------------------------------------------------------- websocket
let ws;
let retryMs = 500;

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/live`);
  $("conn").dataset.state = "connecting";
  ws.onopen = () => { $("conn").textContent = "live"; $("conn").dataset.state = "open"; retryMs = 500; };
  ws.onclose = () => {
    $("conn").textContent = "reconnecting…";
    $("conn").dataset.state = "closed";
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 5000);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    switch (msg.type) {
      case "hello":
        windowS = msg.window_s;
        fillScenarios(msg.scenarios, msg.state.ingest.source);
        clearWaves();
        onStatus(msg.state);
        break;
      case "reset": clearWaves(); setTrend([]); break;
      case "wave": onWave(msg); break;
      case "status": onStatus(msg); break;
      case "vitals": onVitals(msg.vitals); if (msg.trend) setTrend(msg.trend); break;
    }
  };
}

// ---------------------------------------------------------------- controls
async function fillScenarios(scenarios, current) {
  const sel = $("source-select");
  const options = ["udp", "serial:auto"];
  try {
    const ports = await (await fetch("/api/serial/ports")).json();
    for (const p of ports) options.push(`serial:${p.device}`);
  } catch (e) { /* ignore */ }
  for (const s of scenarios) options.push(`simulator:${s}`);
  sel.innerHTML = "";
  for (const o of options) sel.add(new Option(o, o, false, o === current));
}

$("source-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = {
    source: $("source-select").value,
    speed: parseFloat($("source-speed").value) || 1,
    loop: $("source-loop").checked,
  };
  const r = await fetch("/api/source", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!r.ok) $("status").textContent = `error: ${(await r.json()).detail}`;
});

$("reset-btn").addEventListener("click", () => fetch("/api/demo/reset", { method: "POST" }));

connect();
