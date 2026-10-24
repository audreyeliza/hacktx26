import { api, applyPublicDemo, esc, safeNext, showError } from "/static/common.js";

const ROLE = {
  nurse: "Ward, alerts, and admissions",
  physician: "Ward, alerts, and summaries",
  admin: "All of that, plus device and data",
};
const ROLE_NAME = { nurse: "Nurse", physician: "Physician", admin: "Admin" };

const params = new URLSearchParams(location.search);
const next = safeNext(params.get("next"));

try {
  const me = await api("/api/auth/me", { redirectOn401: false });
  if (me.user) location.replace(next);
} catch { /* signed out */ }
await applyPublicDemo();

const accounts = await api("/api/auth/demo-accounts", { redirectOn401: false });
const demo = document.getElementById("demo");
for (const acct of accounts) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "demo-btn";
  btn.innerHTML = `<span class="eyebrow">Demo sign-in</span><span>${esc(ROLE_NAME[acct.role] || acct.role)}</span><small>${esc(acct.display_name)} · ${esc(ROLE[acct.role] || "")}</small>`;
  btn.addEventListener("click", () => enter({ role: acct.role }, "/api/auth/demo"));
  demo.appendChild(btn);
}

document.getElementById("login-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const data = new FormData(ev.target);
  enter({ username: data.get("username"), password: data.get("password") }, "/api/auth/login");
});

async function enter(body, path) {
  const err = document.getElementById("login-error");
  showError(err, null);
  try {
    await api(path, { method: "POST", body, redirectOn401: false });
    location.replace(next);
  } catch (e) {
    showError(err, e);
  }
}
