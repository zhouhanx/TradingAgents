// V2-AUTH-LOGIN: two-step sign-in, forced first password change, account password change.
(() => {
  const $ = (id) => document.getElementById(id);
  const meta = (name) => document.querySelector(`meta[name="${name}"]`)?.getAttribute("content") || "";
  const MODE = meta("tradingagents-login-mode") || "login";
  const TOKEN = meta("tradingagents-token");
  const clone = (id) => document.getElementById(id).content.cloneNode(true);

  document.querySelectorAll("[data-brand]").forEach((el) => {
    el.append(clone("brand"));
    if (el.hasAttribute("data-beacon")) el.append(clone("beacon"));
  });
  document.querySelectorAll("[data-eye]").forEach((button) => {
    button.append(clone("eye-icon"));
    button.addEventListener("click", () => {
      const input = button.previousElementSibling;
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      button.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
      button.title = show ? "隐藏密码" : "显示密码";
    });
  });

  function showError(id, message) {
    const el = $(id);
    el.replaceChildren(clone("err-icon"), document.createTextNode(message));
    el.hidden = !message;
  }
  function clearError(id) { $(id).hidden = true; }
  function show(step) {
    for (const id of ["step-email", "step-password", "step-change", "step-init"]) $(id).hidden = id !== step;
  }

  async function post(path, body) {
    const headers = {"Content-Type": "application/json"};
    if (TOKEN) headers["X-TradingAgents-Token"] = TOKEN;
    const response = await fetch(path, {method: "POST", headers, body: JSON.stringify(body), credentials: "same-origin"});
    const data = await response.json().catch(() => ({}));
    return {status: response.status, ok: response.ok, data};
  }

  let email = "";
  $("email-form").addEventListener("submit", (event) => {
    event.preventDefault();
    email = $("email").value.trim();
    if (!email) { showError("email-error", "此字段不能为空"); $("email").focus(); return; }
    clearError("email-error");
    $("email-echo").value = email;
    $("password").value = "";
    clearError("password-error");
    show("step-password");
    $("password").focus();
  });
  $("back-to-email").addEventListener("click", (event) => {
    event.preventDefault();
    clearError("password-error");
    show("step-email");
    $("email").focus();
  });
  $("password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const password = $("password").value;
    if (!password) { showError("password-error", "此字段不能为空"); return; }
    $("password-submit").disabled = true;
    try {
      const result = await post("/api/account/login", {email, password});
      if (result.ok) { window.location.assign(result.data.must_change ? "/login" : "/"); return; }
      showError("password-error", result.data.error || "邮箱或密码错误");
    } catch {
      showError("password-error", "无法连接控制台，请稍后再试。");
    } finally {
      $("password").value = "";
      $("password-submit").disabled = false;
    }
  });

  $("change-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const oldPassword = $("old-password").value;
    const next = $("new-password").value;
    if (MODE === "change" && !oldPassword) { showError("change-error", "请输入旧密码"); return; }
    if (!next) { showError("change-error", "此字段不能为空"); return; }
    if (next.length < 8) { showError("change-error", "新密码至少 8 位。"); return; }
    if (next !== $("confirm-password").value) { showError("change-error", "两次输入的密码不一致"); return; }
    $("change-submit").disabled = true;
    try {
      const body = {new_password: next};
      if (MODE === "change") body.old_password = oldPassword;
      const result = await post("/api/account/password", body);
      if (result.ok) { window.location.assign("/"); return; }
      showError("change-error", result.data.error || "修改失败。");
    } catch {
      showError("change-error", "无法连接控制台，请稍后再试。");
    } finally {
      $("change-submit").disabled = false;
    }
  });
  $("change-logout").addEventListener("click", async (event) => {
    event.preventDefault();
    await post("/api/account/logout", {}).catch(() => null);
    window.location.assign("/login");
  });

  if (MODE === "init") show("step-init");
  else if (MODE === "force" || MODE === "change") {
    if (MODE === "change") {
      $("change-title").textContent = "修改密码";
      $("change-sub").textContent = "输入旧密码和新密码";
      $("old-row").hidden = false;
      $("change-submit").textContent = "保存";
      $("change-cancel").hidden = false;
      $("change-logout").hidden = true;
    }
    show("step-change");
  } else show("step-email");
})();
