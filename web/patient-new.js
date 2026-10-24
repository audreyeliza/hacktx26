import { $, admissionFields, api, boot, canEdit, readAdmission, showError } from "/static/common.js";

const user = await boot();
const form = $("new-form");
const ward = await api("/api/patients?status=admitted");
const live = ward.patients.find((p) => p.device_kind === "live") || null;
form.innerHTML = admissionFields(null, live) + `
  <div class="form-actions">
    <button class="primary" type="submit" id="save">Admit patient</button>
    <a class="quiet" href="/">Cancel</a>
  </div>`;

if (!canEdit(user)) {
  $("role-note").hidden = false;
  $("save").disabled = true;
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  showError($("form-error"), null);
  $("save").disabled = true;
  try {
    const patient = await api("/api/patients", { method: "POST", body: readAdmission(form) });
    location.href = `/patients/${encodeURIComponent(patient.id)}`;
  } catch (err) {
    showError($("form-error"), err);
    $("save").disabled = !canEdit(user);
  }
});
