import { api, applyProductName, applyPublicDemo, esc, safeNext, showError } from "/static/common.js";

const ROLE = {
  nurse: "Sees the ward and alerts.",
  physician: "Sees summaries and trends.",
  admin: "Sees device and data.",
};
const ROLE_NAME = { nurse: "Nurse", physician: "Physician", admin: "Admin" };

applyProductName();

const params = new URLSearchParams(location.search);
const next = safeNext(params.get("next"));
const err = document.getElementById("login-error");
const passwordLink = document.querySelector("#password-fields a");
if (passwordLink && params.get("next")) {
  passwordLink.href = "/login?next=" + encodeURIComponent(params.get("next"));
}

let signedIn = false;
try {
  const me = await api("/api/auth/me", { redirectOn401: false });
  signedIn = Boolean(me && me.user);
} catch { /* signed out */ }

if (signedIn) {
  location.replace(next);
} else {
  await showWelcome();
}

async function showWelcome() {
  await applyPublicDemo();
  const demo = document.getElementById("demo");
  try {
    const accounts = await api("/api/auth/demo-accounts", { redirectOn401: false });
    for (const acct of accounts) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "demo-btn";
      const name = ROLE_NAME[acct.role] || acct.role;
      btn.innerHTML = `<span>${esc(name)}</span><small>${esc(ROLE[acct.role] || "")}</small>`;
      btn.addEventListener("click", () => enter({ role: acct.role }));
      demo.appendChild(btn);
    }
  } catch (e) {
    showError(err, e);
  }
}

async function enter(body) {
  showError(err, null);
  try {
    await api("/api/auth/demo", { method: "POST", body, redirectOn401: false });
    location.replace(next);
  } catch (e) {
    showError(err, e);
  }
}
