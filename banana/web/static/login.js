"use strict";
/* Sign-in page. The same form doubles as first-account setup while the server reports setup_open. */

(async function () {
  const $ = (id) => document.getElementById(id);
  let mode = "login";

  try {
    const state = await (await fetch("/api/auth/state")).json();
    if (state.username || state.login_required === false) { location.replace("/"); return; }
    if (state.setup_open) {
      mode = "setup";
      $("login-title").textContent = "Create the first account";
      $("login-intro").hidden = false;
      $("login-password").autocomplete = "new-password";
      $("btn-login").textContent = "Create account";
    }
  } catch { /* server unreachable: the submit below will report it */ }

  $("login-username").focus();

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const error = $("login-error");
    error.hidden = true;
    $("btn-login").disabled = true;
    try {
      const res = await fetch(mode === "setup" ? "/api/auth/setup" : "/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: $("login-username").value, password: $("login-password").value }),
      });
      if (res.ok) { location.replace("/"); return; }
      let detail = res.statusText;
      try { detail = (await res.json()).detail ?? detail; } catch { /* not JSON */ }
      error.textContent = typeof detail === "string" ? detail : "Sign-in failed.";
      error.hidden = false;
    } catch {
      error.textContent = "Can't reach the server.";
      error.hidden = false;
    } finally {
      $("btn-login").disabled = false;
    }
  });
})();
