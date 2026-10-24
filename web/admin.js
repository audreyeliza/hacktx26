import { $, api, boot, esc, RECORD_LABELS, SCENARIOS, showError, USE_MOCK } from "/static/common.js";

const user = await boot();
if (user.role !== "admin") {
  $("forbidden").hidden = false;
  $("admin-root").hidden = true;
} else {
  await start();
}

async function start() {
  let device;
  try {
    device = await api("/api/admin/device");
  } catch (err) {
    if (err.status === 403) {
      $("forbidden").hidden = false;
      $("admin-root").hidden = true;
      return;
    }
    throw err;
  }
  const ports = await api("/api/serial/ports");
  const select = $("source-select");
  const options = ["udp", "serial:auto"];
  for (const port of ports) options.push(`serial:${port.device}`);
  for (const name of SCENARIOS) options.push(`simulator:${name}`);
  if (device.source && !options.includes(device.source)) options.unshift(device.source);
  for (const opt of options) select.add(new Option(opt, opt));
  select.value = device.source;

  $("ports").innerHTML = ports.map((p) => `
    <li><b>${esc(p.device)}</b> — ${esc(p.description || "")}${p.is_pico ? " · Pico" : ""}</li>
  `).join("") || `<li>No ports reported.</li>`;

  const labels = $("labels");
  for (const label of RECORD_LABELS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "quiet";
    btn.textContent = label.replaceAll("_", " ");
    btn.dataset.label = label;
    btn.addEventListener("click", () => startRecording(label));
    labels.appendChild(btn);
  }
  $("rec-stop").addEventListener("click", stopRecording);
  $("source-form").addEventListener("submit", switchSource);
  $("reset-form").addEventListener("submit", resetDemo);
  paint(device);
  setInterval(async () => {
    try { paint(await api("/api/admin/device")); } catch { /* keep the last view */ }
  }, 1000);
}

function paint(device) {
  const link = device.link_ok ? "Receiving" : "No packets";
  $("source-line").textContent = `${device.source} · ${link}. ${device.source_status}`;
  const stats = device.stats || {};
  const cells = [
    ["Packets", stats.packets ?? 0],
    ["Gaps", stats.gaps ?? 0],
    ["Missing", stats.missing_packets ?? 0],
    ["Invalid", stats.invalid ?? 0],
    ["Unassigned", device.unassigned_packets ?? 0],
    ["Device", stats.device || "—"],
  ];
  $("stats").innerHTML = cells.map(([k, v]) => `<div><span class="fine">${esc(k)}</span><b>${esc(v)}</b></div>`).join("");
  const assigned = device.assigned_patient_id
    ? `Assigned to <a href="/patients/${esc(device.assigned_patient_id)}">${esc(device.assigned_patient_id)}</a>`
    : "No admitted patient is assigned to the live wearable.";
  $("assigned").innerHTML = assigned;
  const ips = (device.network && device.network.ips) || [];
  const port = device.network ? device.network.http_port : "";
  const udp = device.network ? device.network.udp_port : "";
  $("net").textContent = ips.length
    ? `Phones: ${ips.map((ip) => `http://${ip}:${port}`).join("  ·  ")}. Pico SERVER_IP is the address on the Pico's network. UDP ${udp}.`
    : "No LAN address reported.";
  $("net-note").textContent = USE_MOCK ? "Mock network — the live server reports this laptop's own addresses." : "";
  const select = $("source-select");
  const editing = $("source-form").contains(document.activeElement);
  if (!editing && device.source) select.value = device.source;
  if (device.recording) {
    $("rec-status").textContent = `Recording “${device.recording.label}” · ${device.recording.samples} samples`;
    $("rec-stop").hidden = false;
  } else {
    $("rec-status").textContent = "Not recording.";
    $("rec-stop").hidden = true;
  }
  for (const btn of $("labels").querySelectorAll("button")) {
    btn.setAttribute("aria-pressed", String(Boolean(device.recording && device.recording.label === btn.dataset.label)));
  }
}

async function switchSource(ev) {
  ev.preventDefault();
  showError($("source-error"), null);
  try {
    paint(await api("/api/source", {
      method: "POST",
      body: {
        source: $("source-select").value,
        speed: Number($("source-speed").value),
        loop: $("source-loop").checked,
      },
    }));
  } catch (err) {
    showError($("source-error"), err);
  }
}

async function startRecording(label) {
  showError($("rec-error"), null);
  try {
    const res = await api("/api/record/start", { method: "POST", body: { label } });
    if (res.recording) paint(await api("/api/admin/device"));
  } catch (err) {
    showError($("rec-error"), err);
  }
}

async function stopRecording() {
  showError($("rec-error"), null);
  try {
    const res = await api("/api/record/stop", { method: "POST" });
    $("rec-status").textContent = res.file ? `Saved ${res.file}` : "Not recording.";
    $("rec-stop").hidden = true;
  } catch (err) {
    showError($("rec-error"), err);
  }
}

async function resetDemo(ev) {
  ev.preventDefault();
  const reseed = ev.target.reseed.checked;
  const message = reseed
    ? "Reset streams and restore the six demo beds? Added patients will be removed."
    : "Clear alerts, events, and rate history for every patient?";
  if (!confirm(message)) return;
  showError($("reset-note"), null);
  try {
    const ward = await api("/api/demo/reset", { method: "POST", body: { reseed_ward: reseed } });
    $("reset-note").hidden = false;
    $("reset-note").textContent = `Ward reset. ${ward.patients.length} patients admitted.`;
  } catch (err) {
    showError($("reset-note"), err);
  }
}
