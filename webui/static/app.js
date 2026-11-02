const state = {
  page: "analyze",
  catalog: null,
  auth: null,
  config: {},
  step: 1,
  form: {
    ticker: "",
    date: "",
    output_language: "Chinese",
    analysts: [],
    research_depth: 1,
    llm_provider: "",
    quick_think_provider: "",
    deep_think_provider: "",
    quick_think_llm: "",
    deep_think_llm: "",
    openai_reasoning_effort: "",
    save: true,
    html: true,
    show: true,
    checkpoint: null,
    background: true,
  },
};

// The wizard's starting values before the catalog and saved config are applied.
// A successful launch rebuilds the form from these (WEBUI-006).
const FORM_TEMPLATE = JSON.stringify(state.form);



function displaySentence(key) {
  return (state.catalog && state.catalog.display_sentences && state.catalog.display_sentences[key]) || "";
}

function logSentence(key) {
  return (state.catalog && state.catalog.log_sentences && state.catalog.log_sentences[key]) || "";
}

function envRow(field) {
  return (state.catalog?.config_env || []).find((item) => item.field === field) || null;
}

function envName(field) {
  return envRow(field)?.cli || "";
}

function envLabel(field) {
  const row = envRow(field);
  return row ? `${row.zh} ${row.cli}` : "";
}

function fillTemplate(template, values) {
  let text = template || "";
  for (const [key, value] of Object.entries(values || {})) text = text.split(`{${key}}`).join(value ?? "");
  return text;
}

function placeholder(key) {
  return (state.catalog && state.catalog.placeholders && state.catalog.placeholders[key]) || "";
}

function pickerPrompt(key) {
  return (state.catalog && state.catalog.picker_prompts && state.catalog.picker_prompts[key]) || "";
}

function tierQuestion(tier) {
  const provider = state.form[`${tier}_think_provider`];
  const prompts = (state.catalog && state.catalog.model_prompts) || {};
  const titled = prompts[provider] && prompts[provider][tier];
  return titled || pickerPrompt(tier === "deep" ? "deep_engine" : "quick_engine");
}





function selectionDepthLines() {
  return (state.depthOverrides || []).filter((item) => String(item).includes("Research depth from environment"));
}

function runDepthOverrideLines() {
  return (state.depthOverrides || []).filter((item) => String(item).includes("does not apply"));
}

function depthEnvLine() {
  return selectionDepthLines()[0] || "";
}

function depthRunOverrideHtml() {
  const lines = runDepthOverrideLines();
  if (!lines.length) return "";
  return `<p class="notice" id="depth-run-when">${escapeHtml(state.catalog?.depth_run_when || "")}</p><pre class="log-view" id="depth-run-override">${escapeHtml(lines.join("\n"))}</pre>`;
}

function bindDepth() {
  const host = document.querySelector("#depths");
  if (!host) return;
  const skipped = Object.prototype.hasOwnProperty.call(state.envSkipped || {}, "research_depth");
  const current = String(state.form.research_depth ?? "");
  const matched = (state.catalog.depths || []).some((item) => String(item.value) === current);
  fillChoices("#depths", state.catalog.depths, current, (value) => {
    if (skipped) {
      state.form.research_depth = state.envSkipped.research_depth;
      bindDepth();
      return;
    }
    state.form.research_depth = Number(value);
  }, false, {
    allowEmpty: skipped && !matched,
    emptyLabel: depthEnvLine() || "命令行已用环境变量跳过研究深度，不预选菜单第一项。",
  });
}

function bindLanguages() {
  const host = document.querySelector("#languages");
  if (!host) return;
  fillChoices("#languages", state.catalog.languages, state.form.output_language, (value) => {
    if (state.outputLanguageFromEnv) {
      state.form.output_language = state.envOutputLanguage;
      bindLanguages();
      return;
    }
    state.form.output_language = value;
    const input = document.querySelector("#custom-language");
    if (input) input.hidden = value !== "custom";
    if (value === "custom" && !input?.value.trim()) noteEmptyCustomLanguage();
  });
}

function slotSentence(sentence) {
  return `命令行把当时的值填进这句，花括号不是原样输出：${sentence || ""}`;
}

const pageEl = document.querySelector("#page");
const portal = document.querySelector("#portal");

/* WEBUI-005 ledger: a table wider than its box scrolls inside .table-wrap and says so,
   so a narrow screen never silently cuts off columns. */
function markScrollableTables(root = document) {
  root.querySelectorAll(".table-wrap").forEach((wrap) => {
    const scrollable = wrap.scrollWidth > wrap.clientWidth + 1;
    wrap.classList.toggle("is-scrollable", scrollable);
    if (scrollable) {
      wrap.setAttribute("tabindex", "0");
      wrap.setAttribute("role", "region");
      if (!wrap.hasAttribute("aria-label")) wrap.setAttribute("aria-label", "表格，可左右滚动查看全部列");
      if (!wrap.previousElementSibling?.classList.contains("scroll-hint")) {
        wrap.insertAdjacentHTML("beforebegin", `<p class="scroll-hint" aria-hidden="true">表格较宽，左右滑动查看全部列 →</p>`);
      }
    } else if (wrap.previousElementSibling?.classList.contains("scroll-hint")) {
      wrap.previousElementSibling.remove();
    }
  });
}
let scrollCheckQueued = false;
function queueScrollableCheck() {
  if (scrollCheckQueued) return;
  scrollCheckQueued = true;
  (window.requestAnimationFrame || ((fn) => window.setTimeout(fn, 16)))(() => { scrollCheckQueued = false; markScrollableTables(); });
}
if (typeof MutationObserver === "function") {
  new MutationObserver((records) => {
    if (records.some((record) => [...record.addedNodes].some((node) => node.nodeType === 1 && !node.classList?.contains("scroll-hint")))) queueScrollableCheck();
  }).observe(document.body, {childList: true, subtree: true});
}
window.addEventListener("resize", queueScrollableCheck);
document.addEventListener("toggle", queueScrollableCheck, true);

// SEC-001: the server puts a per-process session token in the page; every API call sends it.
const SESSION_TOKEN = document.querySelector('meta[name="tradingagents-token"]')?.getAttribute("content") || "";

// W6: a restarted console mints a new session token. Notice it before the user acts, not after.
const SESSION_REFUSED = "缺少或错误的控制台会话令牌";
let sessionProbe = null;

function showSessionBanner() {
  if (state.sessionInvalid) return;
  state.sessionInvalid = true;
  const host = document.querySelector("#session-banner-host");
  if (!host) return;
  host.innerHTML = `<div class="session-banner" id="session-banner" role="alert"><div><strong>控制台已经重启，这个页面的会话失效了</strong><p>刷新页面后再继续。刷新前的操作不会提交。</p></div><button class="button button--primary" type="button" id="session-reload">刷新页面</button></div>`;
  document.querySelector("#session-reload").addEventListener("click", () => window.location.reload());
}

function probeSession() {
  if (state.sessionInvalid || sessionProbe) return sessionProbe;
  sessionProbe = api("/api/session").catch(() => null).finally(() => { sessionProbe = null; });
  return sessionProbe;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
      "X-TradingAgents-Token": SESSION_TOKEN,
    },
  });
  const data = await response.json().catch(() => ({ error: "响应不是 JSON。" }));
  if (!response.ok) {
    if (response.status === 403 && String(data.error || "").includes(SESSION_REFUSED)) showSessionBanner();
    throw new Error(data.error || "请求失败。");
  }
  return data;
}

// M2: downloads ask the server for an attachment and keep the report's own file name.
// reports/<run>/complete_report.md downloads as <run>_complete_report.md (same rule as the server).
function reportFileName(path) {
  const parts = String(path || "").split(/[\\/]/).filter(Boolean);
  const base = parts.pop() || "report";
  const parent = parts.pop();
  return parent && parent !== "reports" ? `${parent}_${base}` : base;
}

// W1 (SEC-002): report links carry the console's per-process link key, not the API token.
const LINK_KEY = document.querySelector('meta[name="tradingagents-link-key"]')?.getAttribute("content") || "";

function reportFileHref(path, download = false) {
  return `/api/reports/file?path=${encodeURIComponent(path)}${download ? "&download=1" : ""}&k=${encodeURIComponent(LINK_KEY)}`;
}

function reportDownloadHref(path) {
  return reportFileHref(path, true);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function commandMap(items) {
  return `<ul class="command-map">${items.map((item) =>
    `<li><code>${escapeHtml(item.cli)}</code> ${escapeHtml(item.zh)}</li>`).join("")}</ul>`;
}

function flagTable(items) {
  const rows = items || [];
  if (!rows.length) return "";
  return `<div class="table-wrap"><table class="data-table"><thead><tr><th>中文</th><th>命令行</th><th>命令行说明</th></tr></thead><tbody>${rows.map((item) =>
    `<tr><td>${escapeHtml(item.zh)}</td><td><code>${escapeHtml(item.cli)}</code></td><td class="flag-help">${[item.doc, item.help].filter(Boolean).map((line) => escapeHtml(line)).join("\n")}</td></tr>`).join("")}</tbody></table></div>`;
}

function cliDoc(text) {
  if (!text) return "";
  return `<pre class="log-view">${escapeHtml(text)}</pre>`;
}

function heading(kicker, title, description, actions = "") {
  return `<header class="page-heading">
    <div>
      <h1>${escapeHtml(title)}</h1>
      <p class="page-description">${escapeHtml(description)}</p>
    </div>
    <div class="page-actions page-actions-group">${actions}</div>
  </header>`;
}

function showPage(name, {runDetail = null, framework = null} = {}) {
  closeLayer();
  closeMenus();
  state.page = name;
  if (name === "analyze") {
    // WEBUI-008 (PM): 启动分析 opens on the framework cards (#start), every time,
    // even with one framework; a card opens its wizard (#start/<id>).
    state.analyzeFramework = frameworkIdOf(framework);
    clearStatusRoute();
  } else if (name === "runs") {
    clearStartRoute();
    // WEBUI-007 item 1: 查看状态 opens on the run list; only a #status/<id> address
    // (a refresh, the browser's back/forward) or a click on a run opens its detail.
    state.runDetailOpen = Boolean(runIdOf(runDetail));
    if (state.runDetailOpen) state.selectedRunId = runIdOf(runDetail);
    setStatusRoute(state.runDetailOpen ? state.selectedRunId : null);
  } else {
    clearStatusRoute();
    clearStartRoute();
  }
  pageEl.dataset.page = name;
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("nav-item--active", button.dataset.page === name);
  });
  const renderers = { analyze: renderAnalyze, runs: renderRuns, reports: renderReports, backtest: renderBacktest, accounts: renderAccounts, models: renderModels };
  if (!renderers[name]) {
    state.page = "analyze";
    pageEl.dataset.page = "analyze";
    state.analyzeFramework = null;
  }
  // W7: a page opened before the catalog arrives shows a loading state; init renders it once ready.
  if (!state.catalog) {
    pageEl.innerHTML = `<section class="panel page-loading" id="page-loading" role="status"><p>正在加载…</p></section>`;
    return;
  }
  probeSession();
  (renderers[state.page] || renderAnalyze)();
}


function welcomeBox() {
  const welcome = state.catalog && state.catalog.welcome;
  if (!welcome) {
    return `<section class="panel"><h2>欢迎 <span>${escapeHtml(displaySentence("welcome_title"))}</span></h2><p>${escapeHtml(displaySentence("welcome_cli"))}</p></section>`;
  }
  return `<section class="panel"><h2>欢迎 <span>${escapeHtml(welcome.title)}</span></h2><p class="notice">${escapeHtml(welcome.subtitle)}</p>${welcome.ascii ? `<pre class="log-view">${escapeHtml(welcome.ascii)}</pre>` : ""}<p>${escapeHtml(welcome.body)}</p><p><strong>${escapeHtml(welcome.workflow_label)}</strong> ${escapeHtml(welcome.workflow)}</p><p>命令行原句：${escapeHtml(welcome.credit)}</p></section>`;
}

function updatesSection() {
  const lines = (state.updates && state.updates.lines) || [];
  const note = (state.updates && state.updates.note) || "还没有更新缓存。";
  const body = lines.length ? lines.join("\n") : note;
  return `<details class="disclosure account-details" id="updates-section"><summary>版本与更新</summary>
    <p class="form-hint">${escapeHtml(displaySentence("whats_new"))}</p>
    <pre class="log-view" id="updates-note">${escapeHtml(body)}</pre>
    <div class="inline-actions"><button class="button button--secondary button--small" type="button" id="check-updates">检查更新</button></div>
  </details>`;
}

function bindUpdates() {
  document.querySelector("#check-updates")?.addEventListener("click", async () => {
    const note = document.querySelector("#updates-note");
    note.textContent = "正在检查更新。这会询问 GitHub 和公告服务，不发送身份信息。";
    try {
      const data = await api("/api/updates/refresh", { method: "POST", body: "{}" });
      state.updates = data;
      note.textContent = [displaySentence("whats_new"), ...(data.lines || []), data.note || "", data.reason || ""].filter(Boolean).join("\n");
    } catch (error) {
      note.textContent = error.message;
    }
  });
}

const WIZARD_STEPS = [["分析对象", "标的和日期"], ["研究范围", "维度和深度"], ["模型选择", "快模型和深模型"], ["确认启动", "检查后启动"]];
const WIZARD_TITLES = ["要分析哪个标的", "选择研究范围", "选择本次使用的模型", "检查一下，然后启动"];
const WIZARD_HINTS = ["", "选择分析维度与研究深度。", "可沿用上次选择，或更换模型服务。", "启动后会自动打开查看状态，可以随时离开这个页面。"];
const ASSET_ZH = {stock: "股票", crypto: "加密货币", commodity: "贵金属/商品期货"};

function todayLocal() {
  const today = new Date();
  return `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
}

function recentChips() {
  const items = (state.recent || []).filter((item) => item && item.ticker);
  if (!items.length) return "";
  return `<div class="chips" id="recent-tickers"><span class="form-hint">最近分析过：</span>${items.map((item) =>
    `<button class="chip" type="button" data-recent="${escapeHtml(item.ticker)}">${escapeHtml(item.ticker)}</button>`).join("")}</div>`;
}

function assetSegHtml() {
  const current = state.form.asset_type;
  return Object.entries(ASSET_ZH).map(([value, zh]) =>
    `<span class="seg-item${current === value ? " on" : ""}" data-asset="${value}"${current === value ? ' aria-current="true"' : ""}>${zh}</span>`).join("");
}

function assetHint() {
  if (!state.form.asset_type) return "填写代码后自动识别，例如 BTC-USD 是加密货币。";
  if (state.form.asset_type === "crypto") return "按代码识别为加密货币，不做基本面分析。";
  if (state.form.asset_type === "commodity") return "按代码识别为贵金属/商品期货（如 XAUUSD→GC=F），不做公司基本面分析。";
  return "按代码识别为股票。";
}

function paintAssetSeg() {
  const host = document.querySelector("#asset-seg");
  if (host) host.innerHTML = assetSegHtml();
  const hint = document.querySelector("#asset-hint");
  if (hint) hint.textContent = assetHint();
}

function languageMode() {
  if (state.languageOther) return "other";
  const value = state.form.output_language || "Chinese";
  return value === "Chinese" || value === "English" ? value : "other";
}

function languageSegHtml() {
  const mode = languageMode();
  const locked = state.outputLanguageFromEnv ? " disabled" : "";
  return [["Chinese", "中文"], ["English", "English"], ["other", "其他语言"]].map(([value, label]) =>
    `<button class="seg-item${mode === value ? " on" : ""}" type="button" data-language="${value}" aria-pressed="${mode === value}"${locked}>${label}</button>`).join("");
}

function paintLanguage() {
  const seg = document.querySelector("#language-seg");
  if (seg) seg.innerHTML = languageSegHtml();
  const other = document.querySelector("#language-other");
  if (other) other.hidden = languageMode() !== "other";
  seg?.querySelectorAll("[data-language]").forEach((button) => button.addEventListener("click", () => {
    if (state.outputLanguageFromEnv) return;
    const value = button.dataset.language;
    state.languageOther = value === "other";
    if (value !== "other") state.form.output_language = value;
    paintLanguage();
    bindLanguages();
    const custom = document.querySelector("#custom-language");
    if (custom) custom.hidden = state.form.output_language !== "custom" || languageMode() !== "other";
  }));
}

// ---- WEBUI-008: 启动分析 is a page of analysis-framework cards (GET /api/frameworks);
// a card opens that framework's wizard. Everything on a card comes from the backend.
const START_ROUTE = "#start";

function frameworkIdOf(value) {
  const id = String(value || "").trim().toLowerCase();
  return /^[a-z0-9_-]{1,64}$/.test(id) ? id : null;
}

function startRouteFramework(hash = window.location.hash) {
  const match = /^#start\/(.+)$/.exec(String(hash || ""));
  if (!match) return null;
  try { return frameworkIdOf(decodeURIComponent(match[1])); } catch { return null; }
}

function setStartRoute(id, {push = false} = {}) {
  const target = id ? `${START_ROUTE}/${encodeURIComponent(id)}` : START_ROUTE;
  if (target === window.location.hash) return;
  try {
    window.history[push ? "pushState" : "replaceState"]({framework: id || null}, "", statusUrl(target));
  } catch { /* no history: the page still works, only without the address */ }
}

function clearStartRoute() {
  if (!String(window.location.hash || "").startsWith(START_ROUTE)) return;
  try { window.history.replaceState(null, "", statusUrl("")); } catch { /* see setStartRoute */ }
}

function registeredFramework(id) {
  return (state.frameworks || []).find((item) => item.id === id) || null;
}

function lastUsedText(stamp) {
  const match = /^\d{4}-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(String(stamp || ""));
  return match ? `${match[1]}/${match[2]} ${match[3]}:${match[4]}` : "";
}

function frameworkCard(item) {
  const used = lastUsedText(item.last_used);
  const icon = /^\/icons\/[A-Za-z0-9_.-]+\.(svg|png)$/.test(String(item.icon || "")) ? item.icon : "";
  return `<article class="fw" role="link" tabindex="0" data-framework="${escapeHtml(item.id)}" aria-label="${escapeHtml(item.name)}">
    <div class="fw-t">${icon ? `<img class="fw-logo" src="${escapeHtml(icon)}" width="28" height="28" alt="">` : ""}<span class="fw-name">${escapeHtml(item.name)}</span></div>
    <p class="fw-d">${escapeHtml(item.description || "")}</p>
    ${(item.tags || []).length ? `<div class="fw-m">${item.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}</div>` : ""}
    <div class="fw-f">${used ? `<span class="fw-k">上次使用 ${escapeHtml(used)}</span>` : "<span></span>"}<button class="button button--primary" type="button" data-start-framework="${escapeHtml(item.id)}">开始</button></div>
  </article>`;
}

function hubHtml() {
  const head = heading("", "启动分析", "选一个分析工具开始，结果只用于研究参考。");
  if (state.frameworksError && !(state.frameworks || []).length) {
    return `${head}<div class="error-banner" id="frameworks-error" role="alert">${escapeHtml(state.frameworksError)} <button class="button button--secondary button--small" type="button" id="frameworks-retry">重试</button></div>`;
  }
  if (!state.frameworks) return `${head}<p class="notice" id="frameworks-loading" role="status">正在读取分析工具…</p>`;
  return `${head}<div class="fw-grid" id="fw-grid">${state.frameworks.map(frameworkCard).join("")}</div>`;
}

let frameworksLoad = null;
function loadFrameworks() {
  if (frameworksLoad) return frameworksLoad;
  frameworksLoad = api("/api/frameworks").then((data) => {
    state.frameworks = Array.isArray(data.frameworks) ? data.frameworks : [];
    state.frameworksError = "";
  }).catch((error) => {
    state.frameworksError = `没能读取分析工具列表：${error.message}`;
  }).finally(() => { frameworksLoad = null; });
  return frameworksLoad;
}

function openFramework(id) {
  if (!registeredFramework(id)) return;
  state.analyzeFramework = id;
  setStartRoute(id, {push: true});
  renderAnalyze();
  document.querySelector("#ticker")?.focus();
}

function bindHub() {
  pageEl.querySelectorAll(".fw[data-framework]").forEach((card) => {
    const open = () => openFramework(card.dataset.framework);
    card.addEventListener("click", open);
    card.addEventListener("keydown", (event) => {
      if (event.target !== card || (event.key !== "Enter" && event.key !== " ")) return;
      event.preventDefault();
      open();
    });
  });
  document.querySelector("#frameworks-retry")?.addEventListener("click", () => {
    state.frameworksError = "";
    renderAnalyze();
  });
}

function renderHub({refresh = true} = {}) {
  setStartRoute(null);
  pageEl.innerHTML = hubHtml();
  bindHub();
  if (!refresh) return;
  // last_used changes with every launch: read the cards again each time the page opens.
  loadFrameworks().then(() => {
    if (state.page === "analyze" && !state.analyzeFramework) renderHub({refresh: false});
  });
}

function wizardHeading(framework) {
  return `<header class="page-heading">
    <div>
      <button type="button" class="crumb" id="back-to-frameworks">‹ 全部分析工具</button>
      <h1>${escapeHtml(framework.name)}</h1>
      <p class="page-description">四步设置好就能启动，结果只用于研究参考。</p>
    </div>
    <div class="page-actions page-actions-group"></div>
  </header>`;
}

function backToFrameworks() {
  if (window.history.state?.framework && startRouteFramework()) {
    window.history.back();  // the wizard was opened from the cards: popstate repaints them
    return;
  }
  state.analyzeFramework = null;
  renderAnalyze();
}

function renderAnalyze() {
  const framework = state.analyzeFramework ? registeredFramework(state.analyzeFramework) : null;
  if (!framework) {
    // No framework, or one the backend does not list (an old link): the cards.
    state.analyzeFramework = null;
    renderHub();
    return;
  }
  setStartRoute(framework.id);
  if (!state.form.date) state.form.date = todayLocal();
  pageEl.innerHTML = `
    ${wizardHeading(framework)}
    <div class="wiz">
    <nav class="steps" aria-label="分析设置步骤">${WIZARD_STEPS.map(([title, sub], index) => `<button class="st" type="button" data-step="${index + 1}"><span class="n">${index + 1}</span><span class="st-copy">${title}<small>${sub}</small></span></button>`).join("")}</nav>
    <section class="wizard-panel wiz-form">
      <div class="wizard-heading"><p class="kick" id="step-progress"></p><h2 class="ft" id="step-title" tabindex="-1"></h2><p class="sub" id="step-cli"></p></div>
      <form id="analysis-form" class="wiz-fields" novalidate>
        <section data-step-panel="1">
          <div class="field"><label id="ticker-label" for="ticker">标的代码</label><input id="ticker" name="ticker" value="${escapeHtml(state.form.ticker)}" placeholder="例如 NVDA、AAPL 或 BTC-USD" autocomplete="off">${recentChips()}</div>
          <div class="row2">
            <div class="field"><span class="field-label" id="asset-label">资产类型</span><div class="seg seg--readonly" id="asset-seg" role="group" aria-labelledby="asset-label">${assetSegHtml()}</div><p class="form-hint" id="asset-hint">${escapeHtml(assetHint())}</p></div>
            <div class="field"><label id="date-label" for="date">分析日期</label><input id="date" name="date" type="date" value="${escapeHtml(state.form.date)}" max="${todayLocal()}"><p class="form-hint">默认今天，选过去的日期就是复盘当天。</p></div>
          </div>
          <div class="field"><span class="field-label" id="language-label">报告语言</span><div class="seg" id="language-seg" role="group" aria-labelledby="language-label"></div>
            <div id="language-other" hidden><div class="choice-row" id="languages"></div><input id="custom-language" hidden placeholder="输入报告语言" aria-label="自定义报告语言"></div>
            ${state.outputLanguageFromEnv ? '<p class="form-hint">报告语言由本机配置固定。</p>' : ""}</div>
          <div class="field" id="holding-field"><span class="field-label" id="held-label">是否持仓</span><div class="seg" id="held-seg" role="group" aria-labelledby="held-label"></div><div id="holding-box"></div></div>
        </section>
        <section data-step-panel="2">
          <div class="field"><label id="analyst-label">分析维度</label><div class="choice-row" id="analysts"></div><p class="form-hint">可选择市场、新闻、情绪和基本面等维度。</p></div>
          <div class="field"><label>研究深度</label><div class="choice-row" id="depths"></div><p class="form-hint">深度越高，辩论轮次越多，耗时和模型用量也会增加。</p></div>
        </section>
        <section data-step-panel="3">
          <div class="field"><label>模型服务</label><div id="main-providers"></div><input id="main-provider" type="hidden" value="${escapeHtml(state.form.llm_provider)}"><p class="form-hint">订阅服务需先在「账户」中登录；API 服务需先配置密钥。</p></div>
          <div class="field"><label id="region-label">服务地区</label><div class="choice-row" id="regions"></div></div>
          <div class="field"><label id="backend-url-label" for="backend-url">服务地址</label><input id="backend-url" value="${escapeHtml(backendUrlValue())}" placeholder="例如 http://localhost:11434" ${backendUrlLocked() ? "readonly" : ""}></div>
          <p class="form-hint" id="tier-role-note">快模型负责资料分析与辩论，深模型负责综合判断。可使用同一服务中的不同模型。</p>
          <div class="model-settings-grid">
            ${["quick", "deep"].map((tier) => `<section class="panel"><h3>${tier === "quick" ? "快模型" : "深模型"}</h3><div class="field"><label>供应商</label><div id="${tier}-providers"></div><input id="${tier}-provider" type="hidden" value="${escapeHtml(state.form[`${tier}_think_provider`])}"></div><div class="field"><label>模型</label><div class="choice-row" id="${tier}-models"></div></div><details><summary>手动填写模型 ID</summary><input id="${tier}-model" value="${escapeHtml(state.form[`${tier}_think_llm`])}" aria-label="${tier === "quick" ? "快模型" : "深模型"} ID"></details></section>`).join("")}
          </div>
          <details class="disclosure"><summary>调整思考强度</summary><div class="field"><label id="effort-label">思考强度</label><div id="efforts"></div></div></details>
        </section>
        <section data-step-panel="4"><div id="analysis-summary" class="review"></div></section>
        <p class="notice" id="form-note" role="status" aria-live="polite"></p>
        <div class="foot"><button class="button button--secondary" type="button" id="step-back">上一步</button><div class="r"><button class="button button--secondary" type="button" id="check-analysis" hidden>检查配置</button><button class="button button--primary" type="button" id="step-next">下一步</button><button class="button button--primary" type="button" id="run-analysis" hidden>启动分析</button></div></div>
      </form>
    </section>
    </div>`;
  document.querySelector("#back-to-frameworks")?.addEventListener("click", backToFrameworks);
  paintLanguage();
  bindLanguages();
  paintHolding();
  if (state.outputLanguageFromEnv) document.querySelector("#languages button")?.setAttribute("disabled", "");
  const customLanguage = document.querySelector("#custom-language");
  if (customLanguage) {
    customLanguage.value = state.form.custom_language || "";
    customLanguage.hidden = state.form.output_language !== "custom";
    customLanguage.addEventListener("input", () => {
      state.form.custom_language = customLanguage.value;
      if (state.form.output_language === "custom" && !customLanguage.value.trim()) noteEmptyCustomLanguage();
    });
  }
  renderAnalystChoices(state.catalog.analysts);
  const ticker = document.querySelector("#ticker");
  ticker.addEventListener("change", refreshAnalystsForTicker);
  ticker.addEventListener("input", () => {
    state.form.ticker = ticker.value;
    if (state.form.asset_type) {
      state.form.asset_type = "";
      paintAssetSeg();
    }
  });
  document.querySelectorAll("[data-recent]").forEach((chip) => chip.addEventListener("click", () => {
    ticker.value = chip.dataset.recent;
    state.form.ticker = chip.dataset.recent;
    refreshAnalystsForTicker();
  }));
  bindDepth();
  if (envFieldLocked("research_depth")) document.querySelector("#depths button").disabled = true;
  bindTier("quick");
  bindTier("deep");
  refreshModelPrompts();
  bindMainProvider();
  refreshRegions();
  refreshBackendUrlLabel();
  refreshEffort();
  document.querySelectorAll("[data-step]").forEach((button) => button.addEventListener("click", () => Number(button.dataset.step) <= state.step ? showStep(Number(button.dataset.step)) : advanceAnalysisStep(state.step + 1)));
  document.querySelector("#step-back").addEventListener("click", () => showStep(Math.max(1, state.step - 1)));
  document.querySelector("#step-next").addEventListener("click", () => advanceAnalysisStep());
  document.querySelector("#run-analysis").addEventListener("click", submitAnalysis);
  document.querySelector("#check-analysis").addEventListener("click", async () => {
    const note = document.querySelector("#form-note");
    try {
      const result = await api("/api/runs/validate", { method: "POST", body: JSON.stringify(readForm()) });
      note.textContent = "配置检查通过，可以启动分析。";
      paintReview();
      if (result.ignored?.length) note.textContent += " 部分设置由本机配置决定。";
    } catch (error) {
      note.textContent = error.message;
    }
  });
  document.querySelector("#analysis-form").addEventListener("submit", (event) => { event.preventDefault(); advanceAnalysisStep(); });
  for (const [id, key] of [["date", "date"], ["quick-model", "quick_think_llm"], ["deep-model", "deep_think_llm"], ["backend-url", "backend_url"]]) {
    document.querySelector(`#${id}`)?.addEventListener("input", (event) => { state.form[key] = event.target.value; });
  }
  showStep(Math.min(state.step || 1, 4));
  loadSelectedTierModels();
  if (state.form.ticker && !state.form.asset_type) refreshAnalystsForTicker();
}

// A native file input shows the browser's own English words. Keep it hidden and label it in Chinese.
function filePicker(id, label) {
  return `<div class="file-picker"><input id="${id}" class="file-picker-input" type="file" accept="application/json,.json" tabindex="-1" aria-hidden="true"><button class="button button--secondary button--small" type="button" data-file-for="${id}" aria-label="${escapeHtml(label)}">选择 JSON 文件</button><span class="file-picker-name" data-file-name="${id}">未选择文件</span></div>`;
}

function bindPortfolioFile(fileId, textId, noteId) {
  const input = document.querySelector(fileId);
  const area = document.querySelector(textId);
  if (!input || !area) return;
  const name = document.querySelector(`[data-file-name="${input.id}"]`);
  document.querySelector(`[data-file-for="${input.id}"]`)?.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    const note = document.querySelector(noteId || "#form-note") || document.querySelector("#backtest-result");
    if (!file) return;
    if (name) name.textContent = file.name;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const parsed = JSON.parse(String(reader.result || ""));
        area.value = JSON.stringify(parsed, null, 2);
        area.dispatchEvent(new Event("input", {bubbles: true}));
        if (note) note.textContent = `已读取 ${file.name}。`;
      } catch (error) {
        if (note) note.textContent = `${file.name} 不是可用的 JSON：${error.message}`;
      }
    };
    reader.onerror = () => {
      if (note) note.textContent = `无法读取 ${file.name}。`;
    };
    reader.readAsText(file);
  });
}

function renderAnalystChoices(items) {
  const host = document.querySelector("#analysts");
  if (!host) return;
  mountMultiSelect(host, items, state.form.analysts, (value, pressed) => {
    const set = new Set(state.form.analysts);
    pressed ? set.add(value) : set.delete(value);
    state.form.analysts = [...set];
    refreshStepNote();
  }, {
    requireOne: true,
    emptyLabel: "未选择",
    hint: "选择需要的分析维度，至少保留一项。",
  });
  if (envFieldLocked("analysts")) host.querySelector("button").disabled = true;
}

async function refreshAnalystsForTicker() {
  const ticker = visibleText(document.querySelector("#ticker")?.value);
  if (!ticker) return;
  try {
    const data = await api(`/api/asset?ticker=${encodeURIComponent(ticker)}`);
    state.form.asset_type = data.asset_type;
    paintAssetSeg();
    state.form.analysts = state.form.analysts.filter((item) => data.analysts.includes(item));
    const items = state.catalog.analysts.filter((item) => data.analysts.includes(item.value));
    renderAnalystChoices(items);
    const note = document.querySelector("#form-note");
    if (note) {
      note.textContent = data.fundamentals_hidden
        ? "已识别为加密资产，不使用基本面分析。"
        : "";
    }
    return true;
  } catch (error) {
    const note = document.querySelector("#form-note");
    if (note) note.textContent = error.message;
    return false;
  }
}


function checkPreface(selections) {
  const tickerDefault = Boolean(selections && selections.ticker_from_default);
  const dateDefault = Boolean(selections && selections.date_from_default);
  const parts = ["已核对，尚未启动模型。"];
  if (!tickerDefault && !dateDefault) {
    parts.push("下面是命令行在收到同样的 --ticker、--date、--analysts 时打印的句子。");
    return parts.join("");
  }
  if (tickerDefault) {
    parts.push(`标的留空。命令行提问框原句是 ${questionDefault(1)}。空输入变成 ${placeholder("ticker")}，不打印 ${state.catalog?.confirmation_sentences?.ticker_flag_prefix || ""}。`);
  }
  if (dateDefault) {
    parts.push(`日期留空。命令行提问框原句是 ${questionDefault(2)}。空输入使用这一天，不打印 ${state.catalog?.confirmation_sentences?.date_flag_prefix || ""}。`);
  }
  return parts.join("");
}

function questionDefault(step) {
  return (state.catalog && state.catalog.question_defaults && state.catalog.question_defaults[String(step)]) || "";
}

function stepTitle(step) {
  const item = (state.catalog?.steps || []).find((entry) => entry.step === step);
  return (item && item.title) || "";
}


function envSkipLine(marker) {
  return (state.environmentLines || []).find((item) => String(item).includes(marker)) || "";
}

function skippedPromptNote(marker, title) {
  const line = envSkipLine(marker);
  return line
    ? `命令行已用环境变量跳过这一问。${line}`
    : `命令行已用环境变量跳过这一问，不把 ${title} 标题当成已打印。`;
}

function providerQuestionsSkipped() {
  return Object.prototype.hasOwnProperty.call(state.envSkipped || {}, "llm_provider");
}

function languageAskNote() {
  if (!state.outputLanguageFromEnv) return `命令行提问：${state.catalog.setup_prompts.language}。`;
  return skippedPromptNote("Output language from environment", "Step 3");
}

function depthAskNote() {
  if (!Object.prototype.hasOwnProperty.call(state.envSkipped || {}, "research_depth")) {
    return `命令行提问：${state.catalog.research_depth_prompt}。这是辩论轮数，不是思考强度。`;
  }
  return skippedPromptNote("Research depth from environment", "Step 5");
}

function providerAskNote() {
  if (providerQuestionsSkipped()) return skippedPromptNote("LLM provider from environment", "Step 6");
  return `命令行提问：${state.catalog.provider_prompt}。命令行菜单项是 ${state.catalog.subscription_choice}，只有选中它才问 ${state.catalog.subscription_prompt}。此页把订阅直接放在供应商里，不另开那一步。`;
}

function currentField(id) {
  return document.querySelector(id)?.value.trim() || "";
}

function flagInsteadOfTitle(prefix, title) {
  return `已填写。命令行打印 ${prefix} 加当时的值，不打印 ${title}。`;
}

function calledProviders(extra) {
  const main = state.form.llm_provider;
  const quick = state.form.quick_think_provider || main;
  const deep = state.form.deep_think_provider || main;
  return [extra, quick, deep].filter(Boolean);
}

function calledTierProviders() {
  const main = String(state.form.llm_provider || "").toLowerCase();
  const quick = String(state.form.quick_think_provider || main).toLowerCase();
  const deep = String(state.form.deep_think_provider || main).toLowerCase();
  return [quick, deep].filter(Boolean);
}

function mainEffortKey() {
  const provider = String(state.form.llm_provider || "").toLowerCase();
  if (provider === "chatgpt" || provider === "openai") return "openai";
  if (provider === "google") return "google";
  if (provider === "anthropic") return "anthropic";
  return "";
}

function effortBoxLine(key, name, box) {
  const variant = box || state.catalog?.step_eight?.[key] || {};
  if (!variant.title) return name || key;
  const field = {openai: "openai_reasoning_effort", google: "google_thinking_level", anthropic: "anthropic_effort"}[key];
  const prompt = box ? box.prompt : variant.prompt;
  const printed = `${variant.title}。${variant.body || ""} ${prompt || ""}`.trim();
  if (field && effortSkipped(field)) {
    return `${name}。环境变量已占用这一问，命令行不会再打印 ${variant.title}。页面不预选第一项。`;
  }
  if (mainEffortKey() === key) return `${name}。命令行标题：${printed}`;
  return `${name}。命令行只在主供应商属于这一档时打印 ${variant.title}。当前主供应商不是这一档，不把这句当成已打印。页面仍可设置，调用到这一档时会用到这个值。`;
}

function effortFamilies(extra) {
  const providers = calledProviders(extra);
  const families = [];
  if (providers.some((item) => item === "chatgpt" || item === "openai")) families.push(["openai", "OpenAI / ChatGPT", "openai_reasoning_effort"]);
  if (providers.includes("google")) families.push(["google", "Gemini", "google_thinking_level"]);
  if (providers.includes("anthropic")) families.push(["anthropic", "Claude", "anthropic_effort"]);
  return families;
}

function reasoningFamily() {
  const first = effortFamilies()[0];
  return first ? first[0] : "";
}

function tickerFieldNote() { return "标的代码"; }
function dateFieldNote() { return "分析日期"; }
function analystFieldNote() { return "分析维度"; }

function refreshStepNote() {
  if (state.page === "analyze" && state.step === 4) paintReview();
}

function providerLabel(key) {
  return (state.catalog.providers || []).find((item) => item.key === key)?.zh || key || "未选择";
}

function reportSummaryText() {
  if (state.form.save === false) return state.form.show === false ? "不保存" : "不保存，完成后在状态页展开全文";
  const parts = [state.form.html === false ? "保存 Markdown" : "保存 Markdown 和 HTML"];
  if (state.form.save_path) parts.push(`位置 ${state.form.save_path}`);
  if (state.form.show !== false) parts.push("完成后展开全文");
  return parts.join("，");
}

// Which holdings a start sends (PM rule, WEBUI-006): an imported holdings JSON
// wins over step 1's 是否持仓 — the JSON (cash and every ticker) is sent unchanged,
// step 1's answer is kept but not used, shown read-only, and never blocks step 1.
// 清除导入 hands control back to step 1 with whatever was there. This is the one
// place the rule lives; flip it here (and in the tests) if PM ever decides otherwise.
function holdingsSource() {
  return String(state.portfolioText || "").trim() ? "json" : "step1";
}

function importedHoldingsText() {
  let book = null;
  try { book = parsePortfolio(state.portfolioText); } catch (error) { book = null; }
  const parts = ["来自导入 JSON"];
  if (book && typeof book === "object") {
    parts.push(`${Array.isArray(book.positions) ? book.positions.length : 0} 个持仓`);
    if (book.cash !== undefined && book.cash !== null) parts.push(`现金 ${[book.cash, book.currency].filter((v) => v !== undefined && v !== null && v !== "").join(" ")}`);
  }
  return parts.join(" · ");
}

function holdingSummaryText() {
  if (holdingsSource() === "json") return importedHoldingsText();
  const holding = state.holding || {};
  if (!holding.held) return "未持仓";
  const quantity = String(holding.quantity || "").trim();
  return ["已持仓", `成本 ${holding.average_price || "未填"}`, quantity || "数量未填"].join(" · ");
}

// Step 1 「是否持仓」 (WEBUI-006 item 4, mock2 ?s=w1h): one holding of the analysed
// ticker. 持仓价格 is required once 已持仓 is on; 持有数量 is optional.
const HOLDING_HINT = "用于组合经理给出加仓或减仓建议。持仓价格按买入成本填写。";

const HOLDING_IMPORTED = "已从 JSON 导入持仓，第 1 步填的不再使用";

function holdingImportedHtml() {
  return `<div class="hold-import" id="holding-imported" role="status"><span>${HOLDING_IMPORTED}</span><button class="button button--secondary button--small" type="button" id="clear-holding-import">清除导入</button></div>`;
}

function holdingBoxHtml(holding, readOnly = false) {
  const lock = readOnly ? " disabled" : "";
  const imported = readOnly ? holdingImportedHtml() : "";
  if (!holding.held) return `${imported}${readOnly ? "" : '<p class="form-hint" id="holding-hint">已持仓时填写成本，组合经理会给出加仓或减仓建议。</p>'}`;
  return `${imported}<div class="hold-box${readOnly ? " hold-box--readonly" : ""}"><div class="row2">
    <div class="field"><label for="holding-price">持仓价格<span class="req" aria-hidden="true">*</span></label><input id="holding-price" inputmode="decimal"${readOnly ? "" : ' required aria-required="true"'} value="${escapeHtml(holding.average_price || "")}" placeholder="例如 62000"${lock}></div>
    <div class="field"><label for="holding-quantity">持有数量（可选）</label><input id="holding-quantity" inputmode="decimal" value="${escapeHtml(holding.quantity || "")}" placeholder="例如 0.5"${lock}></div>
  </div>${readOnly ? "" : `<p class="form-hint" id="holding-hint">${HOLDING_HINT}</p>`}</div>`;
}

function paintHolding() {
  const seg = document.querySelector("#held-seg");
  const box = document.querySelector("#holding-box");
  if (!seg || !box) return;
  const holding = state.holding || (state.holding = {});
  const readOnly = holdingsSource() === "json";
  seg.classList.toggle("seg--disabled", readOnly);
  seg.innerHTML = [[false, "未持仓"], [true, "已持仓"]].map(([value, label]) =>
    `<button class="seg-item${Boolean(holding.held) === value ? " on" : ""}" type="button" data-held="${value}" aria-pressed="${Boolean(holding.held) === value}"${readOnly ? " disabled" : ""}>${label}</button>`).join("");
  box.innerHTML = holdingBoxHtml(holding, readOnly);
  document.querySelector("#clear-holding-import")?.addEventListener("click", () => {
    state.portfolioText = "";
    paintHolding();
    document.querySelector("#held-seg [data-held]")?.focus();
  });
  if (readOnly) return;
  seg.querySelectorAll("[data-held]").forEach((button) => button.addEventListener("click", () => {
    holding.held = button.dataset.held === "true";
    paintHolding();
    if (holding.held) document.querySelector("#holding-price")?.focus();
  }));
  for (const [id, key] of [["#holding-price", "average_price"], ["#holding-quantity", "quantity"]]) {
    document.querySelector(id)?.addEventListener("input", (event) => { holding[key] = event.target.value.trim(); });
  }
}

function holdingError() {
  if (holdingsSource() === "json") return "";  // the imported JSON is used; step 1 never blocks
  const holding = state.holding || {};
  if (!holding.held) return "";
  const price = String(holding.average_price || "").trim();
  const value = Number(price);
  if (!price || !Number.isFinite(value) || value <= 0) return "已持仓时请填写持仓价格，需要是大于 0 的数字。";
  const quantity = String(holding.quantity || "").trim();
  if (quantity && (!Number.isFinite(Number(quantity)) || Number(quantity) === 0)) return "持有数量需要是不为 0 的数字，空头可以填负数。";
  return "";
}

function reviewSection(title, rows, action) {
  return `<section class="sec"><div class="sh"><span>${escapeHtml(title)}</span>${action}</div><dl class="kv">${rows.map(([label, value]) =>
    `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join("")}</dl></section>`;
}

function analysisSummary() {
  const language = currentOutputLanguage();
  const languageLabel = (state.catalog.languages || []).find((item) => item.value === language)?.zh || language;
  const ticker = document.querySelector("#ticker")?.value.trim() || state.form.ticker || "未填写";
  const asset = ASSET_ZH[state.form.asset_type];
  const depth = (state.catalog.depths || []).find((item) => String(item.value) === String(state.form.research_depth))?.zh || state.form.research_depth;
  const edit = (step) => `<button class="link-button" type="button" data-edit-step="${step}">修改</button>`;
  return [
    reviewSection("分析对象", [["标的", asset ? `${ticker} · ${asset}` : ticker], ["日期", document.querySelector("#date")?.value || state.form.date || "今天"], ["语言", languageLabel], ["持仓", holdingSummaryText()]], edit(1)),
    reviewSection("研究范围", [["分析维度", (state.catalog.analysts || []).filter((item) => state.form.analysts.includes(item.value)).map((item) => item.zh).join("、") || "未选择"], ["研究深度", String(depth ?? "")]], edit(2)),
    reviewSection("模型", [["快模型", `${providerLabel(state.form.quick_think_provider)} · ${document.querySelector("#quick-model")?.value || state.form.quick_think_llm || "未选择"}`], ["深模型", `${providerLabel(state.form.deep_think_provider)} · ${document.querySelector("#deep-model")?.value || state.form.deep_think_llm || "未选择"}`]], edit(3)),
    reviewSection("保存与续跑", [["报告", reportSummaryText()], ["断点续跑", state.form.checkpoint === false ? "关，失败后只能从头开始" : "开，失败后可从出错的角色继续"]],
      `<button class="link-button" type="button" id="open-save-drawer">调整</button>`),
  ].join("");
}

function paintReview() {
  const host = document.querySelector("#analysis-summary");
  if (!host) return;
  host.innerHTML = analysisSummary();
  host.querySelectorAll("[data-edit-step]").forEach((button) => button.addEventListener("click", () => {
    showStep(Number(button.dataset.editStep));
    document.querySelector("#step-title")?.focus();
  }));
  host.querySelector("#open-save-drawer")?.addEventListener("click", (event) => openSaveDrawer(event.currentTarget));
}

function toggleRow(key, label, on) {
  return `<button class="sw" type="button" role="switch" aria-checked="${on ? "true" : "false"}" data-toggle="${key}"><span class="tog${on ? "" : " off"}" aria-hidden="true"></span><span>${escapeHtml(label)}</span></button>`;
}

const SAVE_MORE_FIELDS = [
  ["drawer-results-dir", "results_dir", "报告和回测写在这里。只能写在 ~/.tradingagents 下。"],
  ["drawer-cache-dir", "data_cache_dir", "断点库在它的 checkpoints 下。只能写在 ~/.tradingagents 下。"],
  ["drawer-memory-path", "memory_log_path", "记忆日志写在这里。只能写在 ~/.tradingagents 下。"],
  ["drawer-memory-max", "memory_log_max_entries", null],
  ["drawer-holding-days", "holding_period_days", null],
  ["drawer-call-budget", "llm_call_budget", null],
];

function openSaveDrawer(trigger) {
  const flags = {
    save: state.form.save !== false,
    html: state.form.html !== false,
    show: state.form.show !== false,
    checkpoint: state.form.checkpoint !== false,
    clear_checkpoints: state.form.clear_checkpoints === true,
  };
  const timeoutRow = configKeyRows().find((item) => item.field === "timeout") || {};
  openLayer(`<div class="provider-drawer-layer" role="presentation"><aside class="provider-drawer save-drawer" role="dialog" aria-modal="true" aria-labelledby="save-drawer-title">
    <div class="provider-drawer-header"><strong id="save-drawer-title">保存与续跑</strong><button class="provider-drawer-close" id="close-save-drawer" type="button" aria-label="关闭">×</button></div>
    <div class="provider-drawer-body save-drawer-body">
      <p class="dg">报告</p>
      ${toggleRow("save", "保存报告", flags.save)}
      ${toggleRow("html", "同时生成 HTML", flags.html)}
      ${toggleRow("show", "完成后展开完整报告", flags.show)}
      <div class="field"><label for="save-path">保存位置</label><input id="save-path" value="${escapeHtml(state.form.save_path || "")}" placeholder="${escapeHtml(`${state.form.results_dir || "~/.tradingagents/logs"}（默认）`)}"><p class="form-hint">留空保存到默认位置。自定义位置需在 reports 下。</p></div>
      <p class="dg">断点续跑</p>
      ${toggleRow("checkpoint", "失败后从出错的角色继续", flags.checkpoint)}
      <p class="form-hint">已完成的角色不会再调用模型。</p>
      <p class="dg">模型调用</p>
      <div class="row2">
        <div class="field"><label for="drawer-timeout">单次超时（秒）</label><input id="drawer-timeout" inputmode="numeric" value="${escapeHtml(state.form.timeout || "")}" placeholder="${escapeHtml(String(timeoutRow.default ?? 300))}"></div>
        <div class="field"><label for="drawer-retries">失败重试（次）</label><input id="drawer-retries" inputmode="numeric" value="${escapeHtml(envFieldValue("llm_max_retries"))}" placeholder="3" ${envFieldLocked("llm_max_retries") ? "readonly" : ""}></div>
      </div>
      <p class="form-hint">只在超时、断网和服务端出错时重试，不会因为登录失效反复重试。</p>
      <details class="disclosure drawer-details" id="portfolio-json-details"${String(state.portfolioText || "").trim() ? " open" : ""}><summary>导入持仓 JSON（含现金或多个持仓）</summary>
        <p class="form-hint">填写后以 JSON 为准，第 1 步填写的持仓不再使用。</p>
        ${filePicker("portfolio-file", "选择持仓 JSON 文件")}
        <div class="field"><label for="portfolio">持仓 JSON</label><textarea id="portfolio" placeholder="${escapeHtml(placeholder("portfolio"))}">${escapeHtml(state.portfolioText || "")}</textarea></div>
      </details>
      <details class="disclosure drawer-details" id="checkpoint-manage"><summary>管理已保存的断点</summary>
        ${toggleRow("clear_checkpoints", "启动前清除断点", flags.clear_checkpoints)}
        <div class="inline-actions"><button class="button button--secondary button--small" type="button" id="list-checkpoints">查看断点</button><button class="button button--danger button--small" type="button" id="clear-checkpoints-now">立即清除断点</button></div>
        <div class="inline-confirm" id="clear-checkpoints-confirm" hidden><span>会删除本机全部断点，正在运行的分析不受影响。</span><button class="button button--danger button--small" type="button" id="clear-checkpoints-yes">确认清除</button><button class="button button--secondary button--small" type="button" id="clear-checkpoints-no">取消</button></div>
        <pre class="log-view" id="checkpoint-list">点击「查看断点」检查可恢复的记录。</pre>
      </details>
      <details class="disclosure drawer-details" id="save-more"><summary>更多保存设置</summary>
        <div class="form-grid">${SAVE_MORE_FIELDS.map(([id, field, note]) => settingInput(id, field, note ? {note, wide: true, value: envFieldValue(field)} : {wide: true, value: envFieldValue(field)})).join("")}</div>
      </details>
      <p class="notice" id="save-drawer-note" role="status" aria-live="polite"></p>
    </div>
    <div class="provider-drawer-footer"><button class="button button--secondary" type="button" id="save-drawer-cancel">取消</button><button class="button button--primary" type="button" id="save-drawer-save">保存</button></div>
  </aside></div>`, {trigger, focus: "#close-save-drawer"});
  document.querySelector("#close-save-drawer").addEventListener("click", closeLayer);
  document.querySelector("#save-drawer-cancel").addEventListener("click", closeLayer);
  document.querySelectorAll(".save-drawer [data-toggle]").forEach((button) => button.addEventListener("click", () => {
    const on = button.getAttribute("aria-checked") !== "true";
    button.setAttribute("aria-checked", String(on));
    button.querySelector(".tog").classList.toggle("off", !on);
  }));
  bindPortfolioFile("#portfolio-file", "#portfolio", "#save-drawer-note");
  document.querySelector("#list-checkpoints").addEventListener("click", async () => {
    const view = document.querySelector("#checkpoint-list");
    try {
      const data = await api("/api/checkpoints");
      const rows = (data.files || []).map((item) => `${item.ticker}  ${item.bytes} bytes`);
      view.textContent = [data.directory ? `目录 ${data.directory}` : "", data.note, ...(rows.length ? rows : ["没有断点文件。"])].filter(Boolean).join("\n");
    } catch (error) {
      view.textContent = error.message;
    }
  });
  const confirmBox = document.querySelector("#clear-checkpoints-confirm");
  document.querySelector("#clear-checkpoints-now").addEventListener("click", () => {
    confirmBox.hidden = false;
    document.querySelector("#clear-checkpoints-no").focus();
  });
  document.querySelector("#clear-checkpoints-no").addEventListener("click", () => {
    confirmBox.hidden = true;
    document.querySelector("#clear-checkpoints-now").focus();
  });
  document.querySelector("#clear-checkpoints-yes").addEventListener("click", async () => {
    const view = document.querySelector("#checkpoint-list");
    confirmBox.hidden = true;
    try {
      const data = await api("/api/checkpoints/clear", { method: "POST", body: "{}" });
      view.textContent = data.zh || `已清除 ${data.cleared} 个断点。`;
    } catch (error) {
      view.textContent = error.message;
    }
  });
  document.querySelector("#save-drawer-save").addEventListener("click", saveDrawer);
}

function positiveNumber(raw, message) {
  const text = String(raw || "").trim();
  if (!text) return null;
  const value = Number(text);
  if (!Number.isFinite(value) || value <= 0) throw new Error(message);
  return text;
}

function readSaveDrawer() {
  const toggles = Object.fromEntries([...document.querySelectorAll(".save-drawer [data-toggle]")].map((button) =>
    [button.dataset.toggle, button.getAttribute("aria-checked") === "true"]));
  const timeout = positiveNumber(document.querySelector("#drawer-timeout").value, "单次超时需要是大于 0 的秒数。");
  const retriesInput = document.querySelector("#drawer-retries");
  const retries = retriesInput.value.trim();
  if (retries && !/^\d+$/.test(retries)) throw new Error("失败重试需要是 0 或正整数。");
  const portfolioText = document.querySelector("#portfolio").value;
  parsePortfolio(portfolioText);
  const config = {timeout, llm_max_retries: retriesInput.readOnly ? undefined : (retries || null)};
  for (const [id, field] of SAVE_MORE_FIELDS) config[field] = inputValue(id);
  return {
    toggles,
    save_path: document.querySelector("#save-path").value.trim() || null,
    portfolioText,
    config,
  };
}

async function saveDrawer() {
  const note = document.querySelector("#save-drawer-note");
  let draft;
  try {
    draft = readSaveDrawer();
  } catch (error) {
    note.className = "error-banner";
    note.textContent = error.message;
    return;
  }
  const saved = await saveConfigPart(draft.config, note, document.querySelector("#save-drawer-save"), "已保存。");
  if (!saved) return;
  Object.assign(state.form, draft.toggles, {save_path: draft.save_path});
  state.portfolioText = draft.portfolioText;
  closeLayer();
  paintHolding();
  paintReview();
  document.querySelector("#open-save-drawer")?.focus();
}

function buildPortfolio(ticker) {
  if (holdingsSource() === "json") return parsePortfolio(state.portfolioText);
  const holding = state.holding || {};
  if (!holding.held) return null;
  const problem = holdingError();
  if (problem) throw new Error(problem);
  const position = {ticker, average_price: Number(holding.average_price)};
  if (String(holding.quantity || "").trim()) position.quantity = Number(holding.quantity);
  return {positions: [position]};
}

function analysisStepError() {
  if (state.step === 1) {
    const ticker = visibleText(document.querySelector("#ticker").value);
    const date = document.querySelector("#date").value;
    if (!ticker) return "请填写要分析的标的代码。";
    if (!date) return "请选择分析日期。";
    if (date > todayLocal()) return "分析日期不能晚于今天。";
    if (state.form.output_language === "custom" && !document.querySelector("#custom-language").value.trim()) return "请填写自定义报告语言。";
    const holding = holdingError();
    if (holding) return holding;
  }
  if (state.step === 2 && !state.form.analysts.length) return "请至少选择一个分析维度。";
  if (state.step === 3) {
    if (!["quick", "deep"].every((tier) => document.querySelector(`#${tier}-provider`).value && document.querySelector(`#${tier}-model`).value.trim())) return "请为快模型和深模型分别选择供应商和模型。";
  }
  return "";
}

async function advanceAnalysisStep(target = state.step + 1) {
  const note = document.querySelector("#form-note");
  const error = analysisStepError();
  if (error) { note.textContent = error; return; }
  if (state.step === 1 && await refreshAnalystsForTicker() === false) return;
  showStep(Math.min(4, target));
  document.querySelector("#step-title")?.focus();
}

function showStep(step) {
  closeMenus();
  state.step = Math.max(1, Math.min(4, step));
  document.querySelector(".wizard-panel").dataset.activeStep = state.step;
  document.querySelectorAll("[data-step-panel]").forEach((panel) => { panel.hidden = Number(panel.dataset.stepPanel) !== state.step; });
  document.querySelectorAll("[data-step]").forEach((button) => {
    const index = Number(button.dataset.step);
    const active = index === state.step;
    const done = index < state.step;
    button.classList.toggle("on", active);
    button.classList.toggle("done", done);
    button.querySelector(".n").textContent = done ? "✓" : String(index);
    button.disabled = index > state.step + 1;
    if (active) button.setAttribute("aria-current", "step"); else button.removeAttribute("aria-current");
  });
  document.querySelector("#step-progress").textContent = `第 ${state.step} 步，共 4 步`;
  document.querySelector("#step-title").textContent = WIZARD_TITLES[state.step - 1];
  document.querySelector("#step-cli").textContent = WIZARD_HINTS[state.step - 1];
  document.querySelector("#step-back").hidden = state.step === 1;
  document.querySelector("#step-next").hidden = state.step === 4;
  document.querySelector("#run-analysis").hidden = state.step !== 4;
  document.querySelector("#check-analysis").hidden = state.step !== 4;
  document.querySelector("#form-note").textContent = "";
  if (state.step === 4) paintReview();
}

function providerText(item) {
  return `<span><strong>${escapeHtml(item.zh || item.key || "")}</strong></span>`;
}

function providerCards(tier) {
  const current = state.form[`${tier}_think_provider`];
  const preferred = ["chatgpt", "grok", "deepseek", "openai", "google", "anthropic"];
  const providers = [...(state.catalog.providers || [])].sort((a, b) => {
    const left = preferred.indexOf(a.key);
    const right = preferred.indexOf(b.key);
    return (left === -1 ? 99 : left) - (right === -1 ? 99 : right);
  });
  return providers.map((item) => `<button class="provider-card" type="button" aria-pressed="${item.key === current}" data-tier="${tier}" data-provider="${escapeHtml(item.key)}">${providerMark(item)}${providerText(item)}</button>`).join("");
}

function providerMark(item) {
  if (item && item.icon) return `<img src="${escapeHtml(item.icon)}" alt="">`;
  return `<svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><rect x="5" y="7" width="18" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.75"/><path d="M9 17l3.2-4 2.4 2.4L19 11" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

function providerOptions(current = state.form.llm_provider) {
  const options = (state.catalog.providers || []).map((item) => ({value: item.key, zh: item.zh || item.key}));
  if (current && !options.some((item) => item.value === current)) {
    const family = regionFamily(current);
    const region = state.catalog.regions?.[family]?.find((item) => item.value === current);
    const name = options.find((item) => item.value === family)?.zh || current;
    options.push({value: current, zh: region ? `${name} · ${region.zh}` : current});
  }
  return options;
}

function bindMainProvider() {
  const host = document.querySelector("#main-providers");
  if (!host) return;
  mountSelect(host, providerOptions(), state.form.llm_provider, (value) => {
      if (Object.prototype.hasOwnProperty.call(state.envSkipped || {}, "llm_provider")) {
        bindMainProvider();
        return;
      }
      const previous = state.form.llm_provider;
      const next = value;
      state.form.llm_provider = next;
      const main = document.querySelector("#main-provider");
      if (main) main.value = next;
      if (!regionFamily(next) && !backendUrlLocked()) {
        state.form.backend_url = next === "ollama" || next === "openai_compatible" ? state.form.backend_url : null;
        const url = document.querySelector("#backend-url");
        if (url && next !== "ollama" && next !== "openai_compatible") url.value = "";
      }
      const refresh = [];
      if (previous !== next) {
        for (const tier of ["quick", "deep"]) {
          if (tierProviderSkipped(tier)) continue;
          if (state.form[`${tier}_think_provider`] !== previous) continue;
          state.form[`${tier}_think_provider`] = next;
          const hidden = document.querySelector(`#${tier}-provider`);
          if (hidden) hidden.value = next;
          refresh.push(tier);
        }
      }
      bindMainProvider();
      refreshRegions();
      refreshBackendUrlLabel();
      refreshEffort();
      for (const tier of refresh) chooseProvider(tier, next, {forceRefresh: true});
      showProviderNote(next);
  }, {allowEmpty: true, emptyLabel: "选择模型服务"});
  if (envFieldLocked("llm_provider")) host.querySelector("button").disabled = true;
}

function regionFamily(provider) {
  const regions = state.catalog?.regions || {};
  if (regions[provider]) return provider;
  for (const [family, items] of Object.entries(regions)) {
    if (items.some((item) => item.value === provider)) return family;
  }
  return null;
}

function refreshRegions() {
  const host = document.querySelector("#regions");
  const field = host?.closest(".field");
  if (!host || !field) return;
  const family = regionFamily(state.form.llm_provider);
  const items = family ? state.catalog.regions[family] : [];
  field.hidden = items.length === 0;
  host.innerHTML = "";
  const label = document.querySelector("#region-label");
  if (label) {
    label.textContent = "服务地区";
  }
  if (!items.length) return;
  fillChoices("#regions", items, state.form.llm_provider, (value) => applyRegion(value, items));
}

function applyRegion(value, items) {
  const previous = state.form.llm_provider;
  const familyValues = new Set(items.map((item) => item.value));
  const picked = items.find((item) => item.value === value);
  state.form.llm_provider = value;
  const main = document.querySelector("#main-provider");
  if (main) main.value = value;
  if (!backendUrlLocked()) {
    state.form.backend_url = picked?.url || null;
    const url = document.querySelector("#backend-url");
    if (url) url.value = picked?.url || "";
  }
  const refresh = [];
  for (const tier of ["quick", "deep"]) {
    if (tierProviderSkipped(tier)) continue;
    const current = state.form[`${tier}_think_provider`];
    if (current === previous || familyValues.has(current)) {
      if (current !== value) refresh.push(tier);
      state.form[`${tier}_think_provider`] = value;
      const hidden = document.querySelector(`#${tier}-provider`);
      if (hidden) hidden.value = value;
    }
  }
  bindMainProvider();
  refreshBackendUrlLabel();
  refreshEffort();
  refreshModelPrompts();
  for (const tier of refresh) chooseProvider(tier, value, {forceRefresh: true});
  const note = document.querySelector("#form-note");
  if (note) {
    note.textContent = "服务地区已更新。";
  }
}

function hostPressed() {
  const selected = state.form.llm_provider;
  const selectedFamily = regionFamily(selected);
  document.querySelectorAll("#main-providers .provider-card").forEach((card) => {
    const key = card.dataset.provider;
    const pressed = key === selected || (selectedFamily && regionFamily(key) === selectedFamily);
    card.setAttribute("aria-pressed", pressed ? "true" : "false");
  });
}

function modelQuestionsSkipped() {
  return tierModelSkipped("quick") || tierModelSkipped("deep");
}

function modelAskNote(tier) {
  if (modelQuestionsSkipped()) return "命令行已用环境变量跳过这一问。";
  return `命令行提问：${tierQuestion(tier)}。`;
}

function refreshModelPrompts() {
  const note = document.querySelector("#tier-prompts");
  if (note) note.textContent = "";
}

function bindTier(tier) {
  const host = document.querySelector(`#${tier}-providers`);
  if (!host) return;
  mountSelect(host, providerOptions(state.form[`${tier}_think_provider`]), state.form[`${tier}_think_provider`], (value) => chooseProvider(tier, value), {allowEmpty: true, emptyLabel: "选择供应商"});
  if (tierProviderSkipped(tier)) host.querySelector("button").disabled = true;
}

function tierPrompt(provider, tier) {
  const prompts = (state.catalog && state.catalog.model_prompts) || {};
  return (prompts[provider] && prompts[provider][tier]) || "";
}

function loadSelectedTierModels() {
  for (const tier of ["quick", "deep"]) {
    const provider = state.form[`${tier}_think_provider`];
    if (provider && document.querySelector(`#${tier}-models`)) chooseProvider(tier, provider);
  }
  lockSkippedModelInputs();
}




function envFieldLocked(key) {
  return Object.prototype.hasOwnProperty.call(state.envSkipped || {}, key);
}

function envFieldValue(key) {
  if (!envFieldLocked(key)) return state.form[key] || "";
  const value = state.envSkipped[key];
  return value == null ? "" : value;
}

function envFieldNote(key, fallback) {
  if (!envFieldLocked(key)) return fallback;
  return `环境变量 ${envName(key)} 已设置，页面不能改。`;
}

function envSkippedHas(key) {
  return Object.prototype.hasOwnProperty.call(state.envSkipped || {}, key);
}

function backendUrlLocked() {
  return envSkippedHas("backend_url");
}

function backendUrlValue() {
  if (backendUrlLocked()) return state.envSkipped.backend_url || "";
  return state.form.backend_url || "";
}


function backendUrlAskNote() {
  return backendUrlLocked() ? "服务地址（由本机配置固定）" : "服务地址";
}

function refreshBackendUrlLabel() {
  const label = document.querySelector("#backend-url-label");
  if (label) {
    label.textContent = backendUrlAskNote();
    label.closest(".field").hidden = !backendUrlLocked() && !["openai_compatible", "ollama"].includes(state.form.llm_provider);
  }
}

function backendUrlNote() {
  if (!backendUrlLocked()) return "";
  const line = (state.environmentLines || []).find((item) => String(item).startsWith("✓ Backend URL:"));
  return line ? `命令行已占用这一项，页面不能改。${line} ` : `环境变量 ${envName("backend_url")} 已设置，页面不能改。`;
}

function tierUrlLocked(tier) {
  return envSkippedHas(`${tier}_think_backend_url`);
}

function tierUrlValue(tier) {
  const key = `${tier}_think_backend_url`;
  if (tierUrlLocked(tier)) return state.envSkipped[key] || "";
  return state.form[key] || "";
}

function tierUrlNote(tier) {
  if (tierUrlLocked(tier)) return "环境变量已设置，页面不能改。";
  const provider = String(state.form[`${tier}_think_provider`] || state.form.llm_provider || "").toLowerCase();
  if (provider === "ollama") return "Ollama 的地址由选择后的端点行决定。命令行不把这个环境变量当成提问。";
  if (provider === "openai_compatible") return "兼容端点的提问在供应商步骤。这里是分层环境变量，命令行不会再问一次。";
  return "命令行没有这一问。留空沿用供应商默认。";
}

function tierSkippedNote(tier) {
  if (!tierProviderSkipped(tier)) return "";
  const value = state.envSkipped[`${tier}_think_provider`] || "";
  return `环境变量已设置，页面不能改，当前是 ${value}。`;
}

function tierProviderSkipped(tier) {
  return Object.prototype.hasOwnProperty.call(state.envSkipped || {}, `${tier}_think_provider`);
}

function tierModelSkipped(tier) {
  return Object.prototype.hasOwnProperty.call(state.envSkipped || {}, `${tier}_think_llm`);
}

function lockSkippedModelInputs() {
  for (const tier of ["quick", "deep"]) {
    const input = document.querySelector(`#${tier}-model`);
    if (!input || !tierModelSkipped(tier)) continue;
    const value = state.envSkipped[`${tier}_think_llm`];
    input.readOnly = true;
    input.value = value == null ? "" : value;
    state.form[`${tier}_think_llm`] = value == null ? "" : value;
  }
}

async function chooseProvider(tier, provider, options = {}) {
  if (tierProviderSkipped(tier)) {
    state.form[`${tier}_think_provider`] = state.envSkipped[`${tier}_think_provider`];
    bindTier(tier);
    lockSkippedModelInputs();
    provider = state.form[`${tier}_think_provider`];
  }
  const previous = state.form[`${tier}_think_provider`];
  const changed = Boolean(options.forceRefresh) || previous !== provider;
  state.form[`${tier}_think_provider`] = provider;
  document.querySelector(`#${tier}-provider`).value = provider;
  bindTier(tier);
  document.querySelectorAll(`#${tier}-providers .provider-card`).forEach((card) => {
    card.setAttribute("aria-pressed", card.dataset.provider === provider ? "true" : "false");
  });
  const list = document.querySelector(`#${tier}-models`);
  list.textContent = "正在读取可用模型…";
  try {
    const data = await api(`/api/models?provider=${encodeURIComponent(provider)}&mode=${tier}`);
    if (!list.isConnected || state.form[`${tier}_think_provider`] !== provider) return;
    if (changed && !tierModelSkipped(tier)) {
      const current = state.form[`${tier}_think_llm`] || "";
      const matched = (data.models || []).some((item) => String(item.id) === String(current));
      if (!matched) {
        state.form[`${tier}_think_llm`] = "";
        const input = document.querySelector(`#${tier}-model`);
        if (input) input.value = "";
      }
    }
    renderModelChoices(list, data, tier);
  } catch (error) {
    if (!list.isConnected) return;
    list.textContent = error.message;
  }
  refreshEffort();
  refreshModelPrompts();
  showProviderNote(provider);
}

async function showProviderNote(provider) {
  const note = document.querySelector("#form-note");
  if (!note || provider !== "ollama") return;
  const url = document.querySelector("#backend-url")?.value.trim();
  try {
    const data = await api(`/api/ollama${url ? `?url=${encodeURIComponent(url)}` : ""}`);
    note.style.whiteSpace = "pre-wrap";
    note.textContent = ["Ollama。", ...(data.cli || data.notes || [])].join("\n");
  } catch (error) {
    note.textContent = error.message;
  }
}

function refreshEffort() {
  const label = document.querySelector("#effort-label");
  const host = document.querySelector("#efforts");
  if (!label || !host || !state.catalog) return;
  const families = effortFamilies();
  if (!families.length) {
    label.textContent = "所选模型使用默认思考方式，无需额外设置。";
    host.innerHTML = "";
    return;
  }
  label.textContent = "思考强度（提高强度可能增加耗时与用量）";
  host.innerHTML = families.map(([key, title]) => {
    const box = state.catalog.step_eight[key] || {};
    return `<div class="field field--wide"><span>${escapeHtml(title + (effortSkipped(families.find((item) => item[0] === key)?.[2]) ? "（由本机配置固定）" : ""))}</span><div class="choice-row" id="effort-${key}"></div></div>`;
  }).join("");
  for (const [key, , field] of families) bindEffort(`#effort-${key}`, key, field);
}

function effortSkipped(field) {
  return Object.prototype.hasOwnProperty.call(state.envSkipped || {}, field);
}

function bindEffort(selector, catalogKey, field) {
  const skipped = effortSkipped(field);
  const current = state.form[field];
  fillChoices(selector, state.catalog.reasoning[catalogKey], current, (value) => {
    if (skipped) {
      state.form[field] = state.envSkipped[field] == null ? "" : state.envSkipped[field];
      refreshEffort();
      showModelEffort();
      return;
    }
    state.form[field] = value;
  }, false, {
    allowEmpty: skipped && (current == null || current === ""),
    emptyLabel: "命令行跳过了这一问，不预选第一项。",
  });
}

let openMenu = null;

function closeMenus() {
  if (!openMenu) return;
  document.removeEventListener("keydown", openMenu.onKey);
  document.removeEventListener("mousedown", openMenu.onDown);
  openMenu.menu.remove();
  openMenu.trigger.setAttribute("aria-expanded", "false");
  openMenu.trigger.classList.remove("sidebar-create-button--open");
  openMenu = null;
}

function placeMenu(menu, trigger) {
  const rect = trigger.getBoundingClientRect();
  const width = Math.min(Math.max(rect.width, 180), window.innerWidth - 16);
  menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
  menu.style.width = `${width}px`;
  menu.style.top = `${rect.bottom + 4}px`;
  const box = menu.getBoundingClientRect();
  if (box.bottom > window.innerHeight - 8) {
    menu.style.top = `${Math.max(8, rect.top - box.height - 4)}px`;
  }
}

function bindMenu(menu, trigger, onKey) {
  const onDown = (event) => {
    if (!menu.contains(event.target) && event.target !== trigger && !trigger.contains(event.target)) closeMenus();
  };
  document.addEventListener("keydown", onKey);
  document.addEventListener("mousedown", onDown);
  openMenu = {menu, trigger, onKey, onDown};
  trigger.setAttribute("aria-expanded", "true");
  document.body.append(menu);
  placeMenu(menu, trigger);
}

function mountSelect(host, items, current, onPick, options = {}) {
  let currentValue = current;
  const matched = items.find((item) => String(item.value) === String(current));
  const selected = matched || (options.allowEmpty ? null : items[0]);
  host.innerHTML = "";
  host.classList.add("custom-select");
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "custom-select-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  const label = document.createElement("span");
  label.className = "custom-select-value";
  label.textContent = selected ? selected.zh : (options.emptyLabel || "请选择");
  trigger.innerHTML = "";
  trigger.append(label);
  trigger.insertAdjacentHTML("beforeend", '<svg class="custom-select-chevron" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>');
  host.append(trigger);
  if (options.navigation) {
    const hint = document.createElement("p");
    hint.className = "form-hint";
    hint.textContent = `命令行提示：${pickerPrompt("menu_navigation")}`;
    host.append(hint);
  }
  const open = () => {
    closeMenus();
    const menu = document.createElement("div");
    menu.className = "custom-select-menu";
    menu.setAttribute("role", "listbox");
    let index = Math.max(0, items.findIndex((item) => String(item.value) === String(currentValue)));
    const paint = () => {
      menu.innerHTML = items.map((item, i) => {
        const chosen = String(item.value) === String(currentValue);
        return `<button type="button" class="custom-select-option${i === index ? " custom-select-option--highlighted" : ""}${chosen ? " custom-select-option--selected" : ""}" role="option" aria-selected="${chosen}" data-index="${i}"><span class="custom-select-value">${escapeHtml(item.zh)}</span>${item.cli ? `<span class="custom-select-option-detail">${escapeHtml(item.cli)}</span>` : ""}${chosen ? '<span class="custom-select-check">✓</span>' : ""}</button>`;
      }).join("");
      menu.querySelector(`[data-index="${index}"]`)?.scrollIntoView({block: "nearest"});
    };
    const choose = (item) => {
      currentValue = item.value;
      label.textContent = item.zh;
      onPick(item.value, true);
      closeMenus();
      trigger.focus();
    };
    paint();
    menu.addEventListener("click", (event) => {
      const button = event.target.closest("[data-index]");
      if (button) choose(items[Number(button.dataset.index)]);
    });
    bindMenu(menu, trigger, (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeMenus();
        trigger.focus();
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        index = (index + 1) % items.length;
        paint();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        index = (index + items.length - 1) % items.length;
        paint();
      } else if (event.key === "Enter") {
        event.preventDefault();
        choose(items[index]);
      }
    });
    menu.querySelector(`[data-index="${index}"]`)?.focus();
  };
  trigger.addEventListener("click", () => {
    if (openMenu?.trigger === trigger) closeMenus();
    else open();
  });
  trigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      open();
    }
  });
}

function mountMultiSelect(host, items, current, onPick, options = {}) {
  const selected = new Set((Array.isArray(current) ? current : String(current || "").split(",")).map((item) => String(item).trim()).filter(Boolean));
  host.innerHTML = "";
  host.classList.add("custom-select");
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "custom-select-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  const label = document.createElement("span");
  label.className = "custom-select-value";
  const summary = () => {
    const names = items.filter((item) => selected.has(String(item.value))).map((item) => item.zh);
    return names.length ? names.join("、") : (options.emptyLabel || "未选择");
  };
  label.textContent = summary();
  trigger.append(label);
  trigger.insertAdjacentHTML("beforeend", '<svg class="custom-select-chevron" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>');
  host.append(trigger);
  const setOne = (item, pressed) => {
    const key = String(item.value);
    if (pressed) selected.add(key);
    else selected.delete(key);
    onPick(item.value, pressed);
  };
  const open = () => {
    closeMenus();
    const menu = document.createElement("div");
    menu.className = "custom-select-menu";
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-multiselectable", "true");
    let index = Math.max(0, items.findIndex((item) => selected.has(String(item.value))));
    const paint = () => {
      menu.innerHTML = items.map((item, i) => {
        const chosen = selected.has(String(item.value));
        return `<button type="button" class="custom-select-option${i === index ? " custom-select-option--highlighted" : ""}${chosen ? " custom-select-option--selected" : ""}" role="option" aria-selected="${chosen}" data-index="${i}"><span class="custom-select-check custom-select-check--lead">${chosen ? "✓" : ""}</span><span class="custom-select-value">${escapeHtml(item.zh)}</span>${item.cli ? `<span class="custom-select-option-detail">${escapeHtml(item.cli)}</span>` : ""}</button>`;
      }).join("") + (options.hint ? `<p class="form-hint">${escapeHtml(options.hint)}</p>` : "");
      label.textContent = summary();
      menu.querySelector(`[data-index="${index}"]`)?.scrollIntoView({block: "nearest"});
    };
    const toggle = (i) => {
      const item = items[i];
      setOne(item, !selected.has(String(item.value)));
      paint();
    };
    const toggleAll = () => {
      const allOn = items.every((item) => selected.has(String(item.value)));
      items.forEach((item) => setOne(item, !allOn));
      paint();
    };
    paint();
    menu.addEventListener("click", (event) => {
      const button = event.target.closest("[data-index]");
      if (!button) return;
      index = Number(button.dataset.index);
      toggle(index);
    });
    bindMenu(menu, trigger, (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeMenus();
        trigger.focus();
      } else if (event.key === "Enter") {
        event.preventDefault();
        if (options.requireOne && selected.size === 0) {
          const sentence = state.catalog?.guard_sentences?.analyst_required || "";
          label.textContent = `至少选择一名分析师。对应命令行：${sentence} 页面没有取消这一问，不会退出。`;
          return;
        }
        closeMenus();
        trigger.focus();
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        index = (index + 1) % items.length;
        paint();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        index = (index + items.length - 1) % items.length;
        paint();
      } else if (event.key === " " || event.key === "Spacebar") {
        event.preventDefault();
        toggle(index);
      } else if (event.key === "a" || event.key === "A") {
        event.preventDefault();
        toggleAll();
      }
    });
  };
  trigger.addEventListener("click", () => {
    if (openMenu?.trigger === trigger) closeMenus();
    else open();
  });
  trigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      open();
    }
  });
}

function fillChoices(selector, items, current, onPick, multi = false, options = {}) {
  const host = document.querySelector(selector);
  if (!host) return;
  if (!multi) {
    mountSelect(host, items, current, onPick, options);
    return;
  }
  mountMultiSelect(host, items, current, onPick, options);
}


function riskRoundLocked() {
  return Object.prototype.hasOwnProperty.call(state.envSkipped || {}, "max_risk_discuss_rounds");
}

function riskRoundValue() {
  if (riskRoundLocked()) return state.envSkipped.max_risk_discuss_rounds ?? "";
  return state.form.max_risk_discuss_rounds || "";
}

function riskRoundNote() {
  if (!riskRoundLocked()) return "留空则和研究深度相同。";
  const runLine = runDepthOverrideLines().find((item) => String(item).includes("max_risk_discuss_rounds")) || "";
  if (runLine) return `${state.catalog?.depth_run_when || ""} ${runLine}`;
  const skipped = depthEnvLine();
  if (skipped) return `两个轮数都来自环境变量，研究深度提问被跳过。页面不能改这一项。命令行在研究深度提问处打印：${skipped}`;
  return "环境变量已占用风险轮数，页面不能改。这不是研究深度跳过句。";
}

function applyEnvSkipped(target) {
  const skipped = state.envSkipped || {};
  for (const [key, value] of Object.entries(skipped)) target[key] = value == null ? "" : value;
  return target;
}

function readForm() {
  return applyEnvSkipped({
    ...state.form,
    ticker: document.querySelector("#ticker").value.trim(),
    date: document.querySelector("#date").value.trim(),
    quick_think_provider: document.querySelector("#quick-provider").value.trim(),
    quick_think_llm: document.querySelector("#quick-model").value.trim(),
    deep_think_provider: document.querySelector("#deep-provider").value.trim(),
    deep_think_llm: document.querySelector("#deep-model").value.trim(),
    llm_provider: document.querySelector("#main-provider")?.value.trim() || state.form.llm_provider,
    backend_url: document.querySelector("#backend-url")?.value.trim() || null,
    portfolio: buildPortfolio(document.querySelector("#ticker").value.trim().toUpperCase()),
    // W50: the console needs the cost only for holdings from the step-1 form;
    // an imported JSON is read as before (a position may give only its size).
    portfolio_source: holdingsSource() === "json" ? "json" : "form",
    save_path: state.form.save_path || null,
    output_language: currentOutputLanguage(),
    // WEBUI-008: the framework whose wizard this is (the backend refuses an unknown one).
    framework: state.analyzeFramework || undefined,
  });
}

function customLanguageInvalid() {
  return state.catalog?.setup_prompts?.custom_language_invalid || "";
}

function noteEmptyCustomLanguage() {
  const note = document.querySelector("#form-note");
  if (!note) return;
  note.textContent = "请填写自定义报告语言。";
}

function currentOutputLanguage() {
  if (state.form.output_language !== "custom") return state.form.output_language || "Chinese";
  const text = document.querySelector("#custom-language")?.value.trim() || String(state.form.custom_language || "").trim();
  if (!text) {
    noteEmptyCustomLanguage();
    return "custom";
  }
  return text;
}

function parsePortfolio(raw) {
  const text = String(raw || "").trim();
  if (!text) return null;
  let book;
  try {
    book = JSON.parse(text);
  } catch (error) {
    throw new Error("持仓内容不是有效的 JSON，请检查文件或粘贴内容。");
  }
  const problems = portfolioProblems(book);
  if (problems.length) throw new Error(`持仓内容不可用：${problems.slice(0, 5).join("；")}。`);
  return book;
}

// W49: the same rules the console applies to an imported holdings JSON, in
// Chinese, so a bad book is caught in the drawer instead of at start.
function plainNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function portfolioProblems(book) {
  if (!book || typeof book !== "object" || Array.isArray(book)) return ['持仓需要是一个 JSON 对象（可含 cash、currency 和 positions）'];
  const problems = [];
  if (book.cash != null && !(plainNumber(book.cash) && book.cash >= 0)) problems.push("现金（cash）需要是不小于 0 的数字");
  if (book.currency != null && typeof book.currency !== "string") problems.push("币种（currency）需要是文字");
  if (book.positions == null) return problems;
  if (!Array.isArray(book.positions)) return [...problems, "持仓列表（positions）需要是数组"];
  book.positions.forEach((item, index) => {
    const where = `第 ${index + 1} 个持仓`;
    if (!item || typeof item !== "object" || Array.isArray(item)) { problems.push(`${where}需要是一个 JSON 对象`); return; }
    if (typeof item.ticker !== "string" || !visibleText(item.ticker)) problems.push(`${where}的代码（ticker）需要是文字`);
    if (item.quantity != null && !(plainNumber(item.quantity) && item.quantity !== 0)) problems.push(`${where}的数量（quantity）需要是不为 0 的数字（负数表示做空）`);
    if (item.average_price != null && !(plainNumber(item.average_price) && item.average_price > 0)) problems.push(`${where}的成本价（average_price）需要是大于 0 的数字`);
    if (item.quantity == null && item.average_price == null) problems.push(`${where}需要填写数量或成本价`);
  });
  return problems;
}

function rememberNotice(text) {
  const line = String(text || "").trim();
  if (!line) return;
  state.notices = [line, ...(state.notices || []).filter((item) => item !== line)].slice(0, 4);
}

// W58: the sentence's 「下一步：…」 starts its own line.
// W83: zero-width / format characters (U+200B, BOM, bidi marks) do not count as
// text, so a ticker made only of them is "empty". trim() keeps them.
// W122: tag characters (U+E0001...) and musical format characters (U+1D173...) too,
// the same set the backend refuses.
function visibleText(value) {
  return String(value ?? "").replace(/[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\u{e0000}-\u{e007f}\u{1d173}-\u{1d17a}\u{e0100}-\u{e01ef}]/gu, "").trim();
}

function multilineHtml(text) {
  return escapeHtml(text).replace(/\n/g, "<br>");
}

// W82: the refusal only stands while the run still counts as running; once it has
// ended (stopped from the API or another tab) the stale banner goes away.
const STOP_REFUSAL_LIVE = new Set(["queued", "starting", "running", "stopping"]);

function stopRefusedHtml(run) {
  const kept = state.stopRefused;
  if (!kept || !run || kept.id !== runIdOf(run.id)) return "";
  if (run.status && !STOP_REFUSAL_LIVE.has(run.status)) {
    state.stopRefused = null;
    return "";
  }
  return `<p class="error-banner" role="alert" id="stop-refused">${multilineHtml(kept.text)}</p>`;
}

function noticesHtml() {
  return (state.notices || []).map((text) => `<p class="notice" data-kept-notice="true">${escapeHtml(text)}</p>`).join("");
}

async function submitAnalysis() {
  if (state.submittingAnalysis) return;
  const button = document.querySelector("#run-analysis");
  state.submittingAnalysis = true;
  if (button) { button.disabled = true; button.textContent = "正在启动…"; }
  let payload;
  let record;
  try {
    payload = readForm();
    state.form = payload;
    record = await api("/api/runs", { method: "POST", body: JSON.stringify(payload) });
  } catch (error) {
    state.submittingAnalysis = false;
    if (button?.isConnected) { button.disabled = false; button.textContent = "启动分析"; }
    // A failed launch keeps everything the user filled in and says why — in the
    // dialog only; the page carries no second copy of the result (WEBUI-006).
    showLaunchFailed(error.message, payload, button);
    return;
  }
  state.submittingAnalysis = false;
  await resetAnalysisForm();
  showLaunchStarted(record || {}, payload);
  refreshRunBadge();
}

// After a successful launch the wizard starts over at step 1 with the values a
// freshly opened page would show. Nothing typed for the last run is kept.
async function resetAnalysisForm() {
  let config = state.bootConfig || {};
  try {
    config = await api("/api/config");
  } catch (error) {
    // Keep the values read when the page opened.
  }
  state.form = JSON.parse(FORM_TEMPLATE);
  applyFormDefaults(state.catalog, config);
  state.form.date = todayLocal();
  state.form.ticker = "";
  state.step = 1;
  state.holding = {};
  state.portfolioText = "";
  state.languageOther = false;
  if (state.page === "analyze") renderAnalyze();
}

function launchDialog(kind, title, bodyHtml, actionsHtml, focus, trigger) {
  openLayer(`<div class="confirm-dialog-layer" role="presentation"><div class="confirm-dialog launch-dialog launch-dialog--${kind}" id="launch-dialog" role="${kind === "ok" ? "dialog" : "alertdialog"}" aria-modal="true" aria-labelledby="launch-title" aria-describedby="launch-body">
    <div class="launch-mark" aria-hidden="true">${kind === "ok" ? "✓" : "!"}</div>
    <h2 id="launch-title">${escapeHtml(title)}</h2>
    <div id="launch-body">${bodyHtml}</div>
    <div class="confirm-dialog-actions launch-actions">${actionsHtml}</div>
  </div></div>`, {focus, trigger});
}

function launchMeta(id, rows) {
  return `<dl class="launch-meta" id="${id}">${rows.filter(([, value]) => value).map(([label, value]) => `<div><dt>${label}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl>`;
}

function launchTickerLabel(ticker, assetType) {
  const asset = ASSET_ZH[assetType];
  return asset ? `${ticker} · ${asset}` : ticker;
}

function showLaunchStarted(record, payload) {
  const ticker = String(record.ticker || payload?.ticker || "").toUpperCase();
  const assetType = record.selections?.asset_type || payload?.asset_type;
  const ignored = (record.ignored || []).length ? `<p class="launch-hint">部分设置由本机配置决定。</p>` : "";
  // RUN-004: when every run slot is busy the run waits in the queue instead.
  const queued = record.status === "queued";
  const ahead = Number(record.queue_ahead) || 0;
  const lead = !queued ? "任务已在后台运行，可以继续启动下一个。"
    : ahead > 0 ? `前面还有 ${ahead} 个任务在排队，轮到时自动开始。` : "前面没有排队的任务，有任务结束后自动开始。";
  launchDialog("ok", queued ? "已加入队列" : "已启动分析",
    `<p id="launch-lead">${escapeHtml(lead)}</p>${launchMeta("launch-run-info", [["标的", launchTickerLabel(ticker, assetType)], ["日期", record.date || payload?.date || ""], ["任务", record.id || ""]])}${ignored}`,
    `<button class="button button--secondary" type="button" id="launch-another">再启动一个</button><button class="button button--primary" type="button" id="launch-open-runs">查看状态</button>`,
    "#launch-open-runs", document.querySelector("#ticker"));
  document.querySelector("#launch-another").addEventListener("click", () => {
    closeLayer();
    document.querySelector("#ticker")?.focus();
  });
  document.querySelector("#launch-open-runs").addEventListener("click", () => {
    closeLayer();
    showPage("runs");
  });
}

// A refusal that a fresh sign-in or key fixes gets a shortcut to the 账户 page.
const SIGN_IN_PROBLEM = /登录|未登录|重新登录|密钥|API key|api_key|凭据/i;

const QUEUE_FULL = "队列已满";

function showQueueFull(reason, trigger) {
  const detail = reason.replace(/^队列已满[：:]\s*/, "");
  launchDialog("fail", QUEUE_FULL,
    `<p class="launch-error" id="launch-error">${escapeHtml(detail)}</p><p class="launch-hint">你填写的设置都保留着。</p>`,
    `<button class="button button--secondary" type="button" id="launch-close">返回修改</button><button class="button button--primary" type="button" id="launch-open-runs">查看状态</button>`,
    "#launch-close", trigger?.isConnected ? trigger : document.querySelector("#run-analysis"));
  document.querySelector("#launch-close").addEventListener("click", closeLayer);
  document.querySelector("#launch-open-runs").addEventListener("click", () => {
    closeLayer();
    showPage("runs");
  });
}

function showLaunchFailed(message, payload, trigger) {
  if (String(message || "").trim().startsWith(QUEUE_FULL)) {
    showQueueFull(String(message).trim(), trigger);
    return;
  }
  const form = payload || state.form;
  const ticker = String(form.ticker || "").toUpperCase();
  const quick = form.quick_think_llm ? `${providerLabel(form.quick_think_provider)} · ${form.quick_think_llm}` : "";
  const reason = String(message || "").trim() || "控制台没有返回原因。";
  const toAccounts = SIGN_IN_PROBLEM.test(reason)
    ? `<button class="button button--primary" type="button" id="launch-open-accounts">去账户页</button>` : "";
  launchDialog("fail", "启动失败",
    `<p class="launch-error" id="launch-error">${escapeHtml(reason)}</p>${launchMeta("launch-fail-info", [["标的", [ticker, form.date].filter(Boolean).join(" · ")], ["模型", quick]])}<p class="launch-hint">你填写的设置都保留着，修改后可以直接再次启动。</p>`,
    `<button class="button ${toAccounts ? "button--secondary" : "button--primary"}" type="button" id="launch-close">返回修改</button>${toAccounts}`,
    "#launch-close", trigger?.isConnected ? trigger : document.querySelector("#run-analysis"));
  document.querySelector("#launch-close").addEventListener("click", closeLayer);
  document.querySelector("#launch-open-accounts")?.addEventListener("click", () => {
    closeLayer();
    showPage("accounts");
  });
}

let runPoll = 0;
let renderingRuns = false;
let renderRunsAgain = false;

function watchRunning(run, runs) {
  window.clearInterval(runPoll);
  runPoll = 0;
  const waiting = (runs || []).some((item) => ["starting", "running", "stopping", "queued"].includes(item.status));
  if (!waiting && (!run || run.status !== "running")) return;
  runPoll = window.setInterval(() => {
    if (state.page !== "runs") {
      window.clearInterval(runPoll);
      runPoll = 0;
      return;
    }
    renderRuns();
  }, 4000);
}


function runStatusLabel(status) {
  return ({
    starting: "启动中",
    running: "运行中",
    stopping: "停止中",
    completed: "已完成",
    finished: "已结束",
    failed: "失败",
    stopped: "已停止",
    queued: "排队中",
    cancelled: "已取消",
    idle: "空闲",
  })[status] || status || "未知";
}

function runStatusClass(status) {
  if (["finished", "completed"].includes(status)) return "status-badge--success";
  if (["failed"].includes(status)) return "status-badge--error";
  if (["running", "starting", "stopping"].includes(status)) return "status-badge--info";
  return "status-badge--muted";
}

function runTicker(run) {
  return run?.ticker || run?.selections?.ticker || "";
}

function runDate(run) {
  return run?.date || run?.selections?.analysis_date || run?.selections?.date || "";
}

function statusLine(run) {
  if (!run) return "暂无正在记录的任务。";
  const label = runStatusLabel(run.status);
  if (run.status === "failed" && run.failure && run.detail) return `当前任务：${label}。${run.detail}`;
  if (run.error) return `当前任务：${label}。${run.error}`;
  if (run.status === "failed" && run.detail) return `当前任务：${label}。${run.detail}`;
  return `当前任务：${label}。`;
}

function taskSummary(run, progress) {
  if (!run) {
    return `<div class="empty-state" id="current-task"><div><h2>还没有分析任务</h2><p>启动一次分析后，这里会显示标的、进度和结果。</p><button class="button button--primary" type="button" id="start-analysis-from-runs">启动分析</button></div></div>`;
  }
  const rows = progress?.agents || [];
  const latest = progress?.backtest?.latest;
  const completed = rows.filter((row) => row.status === "completed").length;
  const active = rows.filter((row) => row.status === "in_progress").length;
  const progressText = latest
    ? `${latest.done}/${latest.total} 个格子完成`
    : rows.length
    ? `${completed}/${rows.length} 个角色完成${active ? `，${active} 个进行中` : ""}`
    : "等待进度";
  const kind = run.kind === "backtest" ? "回测" : "分析";
  return `<section class="panel" id="current-task">
    <h2>当前任务</h2>
    <dl class="summary-list"><div><dt>状态</dt><dd><span class="status-badge ${runStatusClass(run.status)}">${escapeHtml(runStatusLabel(run.status))}</span></dd></div><div><dt>任务</dt><dd>${escapeHtml(kind)}</dd></div><div><dt>标的</dt><dd>${escapeHtml(runTicker(run) || "—")}</dd></div><div><dt>分析日期</dt><dd>${escapeHtml(runDate(run) || "—")}</dd></div><div><dt>进度</dt><dd>${escapeHtml(progressText)}</dd></div></dl>
    ${run.status === "failed" && run.failure && run.detail ? `<p class="error-banner" id="run-failure">${escapeHtml(run.detail)}</p>` : run.error ? `<p class="error-banner">${escapeHtml(run.error)}</p>` : ""}
  </section>`;
}

function nextStepPanel(run, result, progress) {
  if (!run) {
    return `<section class="panel" id="next-step"><h2>下一步</h2><p class="notice">启动分析后，这里会提供刷新、停止和查看结果的操作。</p></section>`;
  }
  if (["starting", "running", "stopping"].includes(run.status)) {
    return `<section class="panel" id="next-step"><h2>下一步</h2><p class="notice">任务仍在运行。稍后刷新状态，或在确认后停止任务。</p></section>`;
  }
  if (result?.present) {
    return `<section class="panel" id="next-step"><h2>下一步</h2><p class="notice">结果已经写出。查看上方结果，或打开报告列表。</p><p class="inline-actions"><button class="button button--secondary" type="button" id="open-reports-from-run">打开报告列表</button></p></section>`;
  }
  if (run.status === "failed" && run.checkpoint?.saved) {
    return `<section class="panel" id="next-step"><h2>下一步</h2><p class="notice">任务没有生成结果，但已保存断点。点「重新启动」会从失败的角色继续，已完成的角色不再调用模型。</p></section>`;
  }
  if (run.status === "failed") {
    return `<section class="panel" id="next-step"><h2>下一步</h2><p class="notice">任务没有生成结果。展开技术详情查看日志，修正配置后可以重新启动。</p></section>`;
  }
  return `<section class="panel" id="next-step"><h2>下一步</h2><p class="notice">任务已结束，暂时没有可显示的结果。</p></section>`;
}

function alreadyRunningNote() {
  const rows = (state.catalog?.launcher_conditional || []).filter((item) =>
    item.key === "already_running_lock" || item.key === "already_running_socket");
  if (!rows.length) return "";
  return `<details class="disclosure" id="already-running"><summary>查看并发启动说明</summary><section class="panel"><h2>已在运行</h2>${rows.map((item) => `<p class="notice">${escapeHtml(item.zh)}。${escapeHtml(item.when)}</p><pre class="log-view" data-already="${escapeHtml(item.key)}">${escapeHtml(item.cli)}</pre>`).join("")}</section></details>`;
}

function statusArgvLine(line) {
  if (!line) {
    return `<p class="notice" id="status-argv-note">当前任务没有可显示的启动参数。</p>`;
  }
  return `<p class="notice" id="status-argv-note">任务启动参数</p><pre class="log-view" id="status-argv">${escapeHtml(line)}</pre>`;
}


function reportTitleKind(kind) {
  if (kind === "panel") return "面板标题 panel";
  if (kind === "stream") return "流式 ### 标题 stream";
  return kind || "";
}

function reportSectionMap() {
  const rows = (state.catalog && state.catalog.live_report_titles) || [];
  return `<ul class="command-map">${rows.map((item) => `<li><span>${escapeHtml(item.zh)} · ${escapeHtml(reportTitleKind(item.kind))}</span><code>${escapeHtml(item.cli)}</code></li>`).join("")}</ul>`;
}

function liveTitlePanel() {
  return `<details class="disclosure"><summary>查看报告分段说明</summary><section class="panel"><h2>实时报告分段</h2><p class="notice">分段标题和文件名来自实际运行记录。</p>${reportSectionMap()}</section></details>`;
}

function currentReportPanel(run, progress) {
  const report = progress && progress.report;
  const waiting = run && (run.status === "running" || run.status === "starting")
    ? `<p class="notice">分析过程中，最新报告内容会显示在这里。</p>`
    : "";
  if (!report && !waiting) return "";
  return `<details class="disclosure"><summary>查看当前分析内容</summary>${waiting}${report ? `<pre class="log-view">${escapeHtml(report)}</pre>` : ""}</details>`;
}

function runLogTailPanel(run) {
  return `<details id="run-log-details"><summary>查看运行日志</summary><div class="field"><label for="log-lines">显示日志行数</label><input id="log-lines" value="40"></div><p class="notice" id="log-path">${escapeHtml(logPathNotice(null))}</p><pre class="log-view" id="run-log">${escapeHtml(run?.detail || "展开后读取日志。")}</pre></details>`;
}

function runTechnicalDetails(run, data) {
  if (!run) return "";
  const progress = data?.progress || {};
  const runMeta = [
    run.id ? `任务 ID：${run.id}` : "",
    run.pid ? `进程：${run.pid}` : "",
    run.started_at ? `开始时间：${run.started_at}` : "",
  ].filter(Boolean);
  const details = [
    statusArgvLine(data?.argv_line),
    run.detail ? `<p class="notice">${escapeHtml(run.detail)}</p>` : "",
    progress.stats ? statsNote(progress) : "",
    progress.wall_time ? wallNote(progress) : "",
    progressTechnicalDetails(progress),
    messageTable(progress),
    toolTable(progress),
    runMeta.length ? `<p class="notice">${escapeHtml(runMeta.join(" · "))}</p>` : "",
    runLogTailPanel(run),
    alreadyRunningNote(),
  ].filter(Boolean).join("");
  if (!details) return "";
  return `<details class="disclosure" id="run-technical-details"><summary>查看技术详情、消息与日志</summary>${details}</details>`;
}

function showToast(text) {
  const host = document.querySelector("#toast-host");
  if (!host || !text) return;
  host.innerHTML = `<div class="toast" role="status"><span class="ck" aria-hidden="true">✓</span><span>${escapeHtml(text)}</span></div>`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { host.innerHTML = ""; }, 4000);
}

const ROLE_PILL = {
  ok: ["ok", "完成"],
  run: ["run", "进行中"],
  bad: ["err", "失败"],
  wait: ["wait", "等待"],
  part: ["wait", "未完成"],
};

function runActive(run) {
  return Boolean(run && ["starting", "running", "stopping"].includes(run.status));
}

function teamGroups(rows) {
  const groups = [];
  for (const row of rows) {
    let group = groups.find((item) => item.team === row.team);
    if (!group) {
      group = {team: row.team, team_zh: row.team_zh, roles: []};
      groups.push(group);
    }
    group.roles.push(row);
  }
  return groups;
}

function teamState(roles, active) {
  if (roles.some((row) => row.status === "error")) return "bad";
  if (roles.every((row) => row.status === "completed")) return "ok";
  if (roles.some((row) => row.status === "in_progress")) return "run";
  if (roles.some((row) => row.status === "completed")) return active ? "run" : "part";
  return "wait";
}

function roleDot(status) {
  return ({completed: "ok", in_progress: "run", error: "err"})[status] || "wait";
}

function roleTime(row) {
  if (row.status === "completed") return row.elapsed || row.finished || "";
  if (row.status === "in_progress") return "进行中";
  if (row.status === "error") return "出错";
  return "";
}

function failureSummary(run) {
  return run?.failure?.summary || run?.failure?.message || run?.detail || "";
}

function teamNow(group, stateKey, run) {
  if (stateKey === "run") {
    const working = group.roles.filter((row) => row.status === "in_progress");
    if (!working.length) return `<div class="now"><b>正在做：</b>等待本组下一个角色开始。</div>`;
    return `<div class="now"><b>正在做：</b>${working.map((row) => `${escapeHtml(row.zh)}${row.started ? `（${escapeHtml(row.started)} 开始）` : ""}`).join("、")}正在工作。</div>`;
  }
  if (stateKey === "bad") {
    const failed = group.roles.find((row) => row.status === "error");
    const saved = run?.checkpoint?.saved;
    const reason = failureSummary(run) || `${failed?.zh || "这个角色"}出错`;
    return `<div class="now"><b>停在这里：</b>${escapeHtml(reason)}${saved ? "。已完成的角色已存进断点，可以从这里继续。" : "。"}</div>`;
  }
  return "";
}

function pipelineHtml(rows, run) {
  const active = runActive(run);
  return `<div class="pipe" id="team-pipeline">${teamGroups(rows).map((group) => {
    const key = teamState(group.roles, active);
    const [pill, label] = ROLE_PILL[key];
    const cls = key === "run" ? " cur" : key === "bad" ? " bad" : key === "wait" ? " dim" : "";
    return `<section class="team${cls}" data-team="${escapeHtml(group.team)}" aria-label="${escapeHtml(group.team_zh)}：${label}"><div class="tn"><span>${escapeHtml(group.team_zh)}</span><span class="pill ${pill}">${label}</span></div>${group.roles.map((row) =>
      `<div class="role" data-agent="${escapeHtml(row.agent)}"><span class="dot ${roleDot(row.status)}" aria-hidden="true"></span><span>${escapeHtml(row.zh)}</span><span class="t">${escapeHtml(roleTime(row))}</span><span class="visually-hidden">${escapeHtml(row.status_zh)}</span></div>`).join("")}${teamNow(group, key, run)}</section>`;
  }).join("")}</div>`;
}

function timelineText(event) {
  return ({in_progress: "开始工作", completed: "完成", error: "出错，任务在这里停止"})[event.status] || event.status_zh || "";
}

function feedHtml(timeline) {
  if (!timeline?.length) return `<div class="feed" id="run-feed"><p class="ev ev--empty">还没有动态。角色开始工作后会出现在这里。</p></div>`;
  return `<div class="feed" id="run-feed">${timeline.map((event) =>
    `<div class="ev${event.status === "error" ? " ev--err" : ""}"><span class="tm">${escapeHtml(event.time || "—")}</span><span class="who">${escapeHtml(event.zh)}</span><span>${escapeHtml(timelineText(event))}</span></div>`).join("")}</div>`;
}

function elapsedText(run) {
  const start = Date.parse(run?.started_at || "");
  if (!Number.isFinite(start)) return "—";
  const endRaw = runActive(run) ? Date.now() : Date.parse(run.finished_at || "");
  const end = Number.isFinite(endRaw) ? endRaw : Date.now();
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分 ${rest} 秒`;
}

function statusPill(run) {
  const key = ({running: "run", starting: "run", stopping: "run", failed: "err", finished: "ok", completed: "ok"})[run.status] || "wait";
  return `<span class="pill ${key}">${escapeHtml(runStatusLabel(run.status))}</span>`;
}

// The models this run was started with, from its own selections (quick/deep,
// falling back to the single provider/model of an older record).
function runModelLines(selections) {
  const line = (tier, provider, model) => model ? `${tier} ${provider ? `${providerLabel(provider)} · ` : ""}${model}` : "";
  return [
    line("快", selections.quick_think_provider || selections.llm_provider, selections.quick_think_llm),
    line("深", selections.deep_think_provider || selections.llm_provider, selections.deep_think_llm),
  ].filter(Boolean);
}

function runSummaryCards(run, rows) {
  const total = rows.length;
  const done = rows.filter((row) => row.status === "completed").length;
  const percent = total ? Math.round((done / total) * 100) : 0;
  const failed = run.status === "failed";
  const selections = run.selections || {};
  const modelLines = runModelLines(selections);
  return `<div class="run-summary" id="current-task">
    <div class="stat-card"><div class="k">状态</div><div class="v">${statusPill(run)}</div></div>
    <div class="stat-card"><div class="k">角色进度</div><div class="v" id="role-progress">${done} / ${total}</div><div class="bar${failed ? " bar--err" : ""}" role="progressbar" aria-label="角色进度" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${done}"><i style="width:${percent}%"></i></div></div>
    <div class="stat-card"><div class="k">已用时间</div><div class="v" id="run-elapsed">${escapeHtml(elapsedText(run))}</div></div>
    <div class="stat-card"><div class="k">模型</div><div class="v v--small" id="run-models">${modelLines.length ? modelLines.map((line) => `<span class="model-line">${escapeHtml(line)}</span>`).join("") : "—"}</div></div>
  </div>`;
}

function failureAlert(run, rows) {
  if (run.status !== "failed") return "";
  const failedRow = rows.find((row) => row.status === "error");
  const done = rows.filter((row) => row.status === "completed").length;
  const where = failedRow ? `分析在${failedRow.team_zh}这一步停止了` : "分析没有完成";
  const who = failedRow?.zh || run.failure?.role_zh;
  const reason = failureSummary(run);
  const saved = Boolean(run.checkpoint?.saved);
  const canRestart = Boolean(run.selections);
  const restart = !canRestart ? "" : saved
    ? `<button class="button button--primary" type="button" id="resume-run">从断点重新启动</button>`
    : `<button class="button button--secondary" type="button" id="resume-run" disabled aria-describedby="resume-note">从断点重新启动</button><button class="button button--primary" type="button" id="restart-run">重新启动</button>`;
  return `<div class="alert" id="run-failure" role="alert">
    <div class="alert-copy"><b>任务失败：${escapeHtml(where)}</b><div class="why">已完成 ${done} 个角色${who ? `，${escapeHtml(who)}出错后进程退出` : ""}，没有写出报告。${reason ? `原因：<code>${escapeHtml(reason)}</code>` : ""}</div></div>
    <div class="alert-actions"><div class="alert-buttons"><button class="button button--secondary" type="button" id="show-error-log">查看错误日志</button>${restart}</div>${canRestart && !saved ? `<span class="alert-note" id="resume-note">本次未保存断点，只能重新启动</span>` : ""}</div>
  </div>`;
}

function runMetaLine(run) {
  const parts = [run.id ? `任务 ID：${run.id}` : "", run.pid ? `进程：${run.pid}` : "", run.started_at ? `开始时间：${run.started_at}` : ""].filter(Boolean);
  return parts.length ? `<p class="notice">${escapeHtml(parts.join(" · "))}</p>` : "";
}

function statusSubline(run) {
  const selections = run.selections || {};
  const depth = (state.catalog?.depths || []).find((item) => String(item.value) === String(selections.research_depth))?.zh;
  return [runTicker(run), runDate(run) ? `分析日期 ${runDate(run)}` : "", depth ? `研究深度 ${depth}` : ""].filter(Boolean).join(" · ");
}

function clockOf(stamp) {
  return String(stamp || "").slice(11, 16);
}

function depthZh(run) {
  const selections = run.selections || {};
  return (state.catalog?.depths || []).find((item) => String(item.value) === String(selections.research_depth))?.zh || "";
}

// 任务详情 header for one run of the list (RUN-004, Proto -v4).
function detailHead(run, actions) {
  const when = run.status === "queued" ? (run.queued_at ? `${clockOf(run.queued_at)} 提交` : "")
    : (run.started_at ? `开始于 ${clockOf(run.started_at)}` : "");
  const sub = [run.framework_name || "", depthZh(run) ? `研究深度 ${depthZh(run)}` : "", when].filter(Boolean).join(" · ");
  return `<div class="dhd" id="run-detail-head"><div><div class="ttl">${escapeHtml([runTicker(run), runDate(run)].filter(Boolean).join(" · "))}</div>${sub ? `<p class="sub">${escapeHtml(sub)}</p>` : ""}</div><div class="act">${actions}</div></div>`;
}

function queuedDetailHtml(run, data) {
  const rows = data.progress?.agents || [];
  const position = Number(run.position) || 1;
  return `${detailHead(run, `<button class="button button--danger-outline" type="button" id="cancel-queued" data-run-id="${escapeHtml(run.id)}">取消排队</button>`)}
    <div class="alert alert--quiet" id="queue-note"><div class="alert-copy"><b>排队第 ${position} 位，前面有任务结束后自动开始。</b></div></div>
    ${rows.length ? `<section class="status-block"><h2 class="section-title">团队进度</h2>${pipelineHtml(rows, run)}</section>` : ""}`;
}

function analysisStatusHtml(run, data, options = {}) {
  if (options.detail && run.status === "queued") return queuedDetailHtml(run, data);
  const progress = data.progress || {};
  const rows = progress.agents || [];
  const active = runActive(run);
  const actions = `${active ? `<button class="button button--danger-outline" type="button" id="stop-run">停止任务</button>` : ""}${!active && run.status !== "failed" && run.selections ? `<button class="button button--secondary" type="button" id="restart-run">重新启动</button>` : ""}<button class="button button--secondary" type="button" id="open-run-report">打开报告</button>`;
  const failureMessage = run.failure?.message ? `<p class="notice">错误信息（已隐去密钥和账号）</p><pre class="log-view" id="failure-message">${escapeHtml(run.failure.message)}</pre>` : "";
  return `
    ${options.detail ? detailHead(run, actions) : `${noticesHtml()}
    ${heading("status", "查看状态", statusSubline(run), actions)}`}
    ${failureAlert(run, rows)}
    ${runSummaryCards(run, rows)}
    ${rows.length ? `<section class="status-block"><h2 class="section-title">团队进度</h2>${pipelineHtml(rows, run)}</section>` : `<p class="notice">等待进度。</p>`}
    <section class="status-block"><h2 class="section-title">最近动态</h2>${feedHtml(progress.timeline)}</section>
    ${data.result?.present ? resultPanel(data.result) : ""}
    ${completionNote(progress)}
    <details class="disclosure" id="run-technical-details"><summary>技术信息与完整日志</summary>
      ${failureMessage}
      ${statusArgvLine(data.argv_line)}
      ${run.detail ? `<p class="notice">${escapeHtml(run.detail)}</p>` : ""}
      ${progress.stats ? statsNote(progress) : ""}
      ${progress.wall_time ? wallNote(progress) : ""}
      ${currentReportPanel(run, progress)}
      ${progressTechnicalDetails(progress)}
      ${messageTable(progress)}
      ${toolTable(progress)}
      ${runMetaLine(run)}
      ${runLogTailPanel(run)}
      ${alreadyRunningNote()}
    </details>`;
}

// Stop / restart / resume / cancel always name the run: the console refuses a
// request without an id (400, nothing done), so a run without one is never posted.
const RUN_ID_MISSING = "这个任务没有 id，无法操作。请刷新状态后再试。";

function runIdOf(value) {
  return typeof value === "string" && value.trim() ? value : "";
}

async function restartRun(resume, button, runId) {
  const id = runIdOf(runId);
  if (!id) {
    pageEl.insertAdjacentHTML("afterbegin", `<p class="error-banner" role="alert">${escapeHtml(RUN_ID_MISSING)}</p>`);
    return;
  }
  if (button) { button.disabled = true; button.textContent = resume ? "正在从断点启动…" : "正在重新启动…"; }
  try {
    const target = {id};
    state.stopRefused = null;   // W76: a new action replaces the old refusal
    const result = await api("/api/runs/restart", { method: "POST", body: JSON.stringify(resume ? {...target, resume: true} : target) });
    if (result?.id) {
      state.selectedRunId = result.id;
      if (state.runDetailOpen) setStatusRoute(result.id);
    }
    showToast(resume ? "已从断点重新启动，已完成的角色不会再调用模型" : (result.zh || "已重新启动任务"));
    await renderRuns();
  } catch (error) {
    if (button?.isConnected) { button.disabled = false; button.textContent = resume ? "从断点重新启动" : "重新启动"; }
    pageEl.insertAdjacentHTML("afterbegin", `<p class="error-banner" role="alert">${escapeHtml(error.message)}</p>`);
  }
}

// ---- RUN-004: several runs at once (Proto mock.html -v4: multi / multifail / multidetail / queued)

const RUN_FILTERS = [["all", "全部"], ["running", "运行中"], ["queued", "排队中"], ["failed", "失败"], ["finished", "已完成"]];
const LIVE_RUN = ["starting", "running", "stopping"];

// 查看状态 is always the list + 任务详情, whatever the number of runs (PM ruling, WEBUI-006 item 3).
function runListItems(run, data, runs) {
  if (runs.length || !run) return runs;
  // A reply without the list (an older console) still shows its one run as a row.
  const agents = data.progress?.agents || [];
  return [{id: run.id || "current", kind: run.kind, ticker: runTicker(run), date: runDate(run), status: run.status,
    done: agents.filter((row) => row.status === "completed").length, total: agents.length,
    team_zh: "", started_at: run.started_at, queued_at: run.queued_at, framework_name: run.framework_name}];
}

function runListCounts(data, runs) {
  if (data.counts) return data;
  const counts = {};
  RUN_FILTERS.forEach(([key]) => { counts[key] = runs.filter((item) => runFilterMatch(item, key)).length; });
  return {...data, counts};
}

function emptyRunsHtml(data) {
  const limits = data.limits || {};
  const rule = limits.max_running ? `最多同时运行 ${limits.max_running} 个，其余自动排队。` : "";
  const body = `<div class="runs-empty" id="runs-empty"><b>还没有分析任务</b><p>启动分析后，任务会出现在这里。${rule}</p><button class="button button--primary" type="button" id="start-analysis-from-runs">启动分析</button></div>`;
  if (narrowRuns()) return `<div class="cards" id="run-list" role="list">${body}</div>`;
  return `<div class="tl" id="run-list" role="list"><div class="tr th" aria-hidden="true"><span>标的</span><span>工具</span><span>状态</span><span>进度</span><span>当前团队</span><span>开始时间</span><span></span></div>${body}</div>`;
}

function narrowRuns() {
  return Boolean(window.matchMedia && window.matchMedia("(max-width: 600px)").matches);
}

function runFilterMatch(item, filter) {
  if (filter === "running") return LIVE_RUN.includes(item.status);
  if (filter === "finished") return ["finished", "completed"].includes(item.status);
  if (filter === "all") return true;
  return item.status === filter;
}

function runsHeadline(data) {
  const counts = data.counts || {};
  const limits = data.limits || {};
  const caps = limits.max_running ? `（最多同时运行 ${limits.max_running} 个，最多排队 ${limits.max_queued} 个）` : "";
  return `${counts.running || 0} 个运行中 · ${counts.queued || 0} 个排队中${caps}`;
}

// W43: the list holds the newest runs only; say so when some were left out.
function runsCapNote(data) {
  const listing = data.listing || {};
  if (!listing.truncated) return "";
  return `<p class="notice runs-cap" id="runs-cap">只显示最近 ${Number(listing.shown) || 0} 个任务（共 ${Number(listing.total) || 0} 个）；上方计数包含全部任务。</p>`;
}

function runFilterTabs(data) {
  const counts = data.counts || {};
  const current = state.runFilter || "all";
  return `<div class="ftabs" role="tablist" aria-label="按状态筛选" id="run-filters">${RUN_FILTERS.map(([key, label]) =>
    `<button type="button" class="ftab${key === current ? " on" : ""}" role="tab" aria-selected="${key === current}" data-filter="${key}">${label}<b>${Number(counts[key]) || 0}</b></button>`).join("")}</div>`;
}

function runBar(item) {
  const total = Number(item.total) || 0;
  const done = Number(item.done) || 0;
  const tone = item.status === "failed" ? " bar--err" : ["finished", "completed"].includes(item.status) ? " bar--ok" : item.status === "queued" ? " bar--wait" : "";
  return `<div class="pg"><div class="bar${tone}" role="progressbar" aria-label="角色进度" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${done}"><i style="width:${total ? Math.round((done / total) * 100) : 0}%"></i></div><span>${done}/${total}</span></div>`;
}

function runListPill(item) {
  const label = ({finished: "已完成", completed: "已完成"})[item.status] || runStatusLabel(item.status);
  const key = ({running: "run", starting: "run", stopping: "run", failed: "err", finished: "ok", completed: "ok"})[item.status] || "wait";
  return `<span class="pill ${key}">${escapeHtml(label)}</span>`;
}

function runWhen(item) {
  return item.status === "queued" ? `${clockOf(item.queued_at)} 提交` : clockOf(item.started_at) || "—";
}

function cancelButton(item) {
  return `<button type="button" class="qx" data-cancel-run="${escapeHtml(item.id)}" aria-label="取消排队 ${escapeHtml(item.ticker || "")}">取消</button>`;
}

function runTable(items, selected) {
  return `<div class="tl" id="run-list" role="list"><div class="tr th" aria-hidden="true"><span>标的</span><span>工具</span><span>状态</span><span>进度</span><span>当前团队</span><span>开始时间</span><span></span></div>${items.map((item) =>
    `<div class="tr${item.id === selected ? " on" : ""}" role="listitem button" tabindex="0" data-run-id="${escapeHtml(item.id)}" data-status="${escapeHtml(item.status)}"${item.id === selected ? ' aria-current="true"' : ""}><div class="tk">${escapeHtml(item.ticker || "—")}<small>分析日期 ${escapeHtml(item.date || "—")}</small></div><span class="fwc"${frameworkTitle(item)}>${escapeHtml(item.framework_name || "—")}</span><span>${runListPill(item)}</span>${runBar(item)}<span${item.status === "queued" ? ' class="mu"' : ""}>${escapeHtml(item.team_zh || "—")}</span><span class="mu">${escapeHtml(runWhen(item))}</span>${item.status === "queued" ? cancelButton(item) : '<span class="chev" aria-hidden="true">›</span>'}</div>`).join("") || '<p class="notice tl-empty">这个分类下没有任务。</p>'}</div>`;
}

function runCards(items, selected) {
  return `<div class="cards" id="run-list" role="list">${items.map((item) =>
    `<div class="tc${item.id === selected ? " on" : ""}" role="listitem button" tabindex="0" data-run-id="${escapeHtml(item.id)}" data-status="${escapeHtml(item.status)}"><div class="a"><span class="tk">${escapeHtml(item.ticker || "—")}</span><span>${runListPill(item)}${item.status === "queued" ? cancelButton(item) : ""}</span></div><div class="b">${escapeHtml([item.framework_name, item.date, item.team_zh, `开始 ${runWhen(item)}`].filter(Boolean).join(" · "))}</div>${runBar(item)}</div>`).join("") || '<p class="notice">这个分类下没有任务。</p>'}</div>`;
}

function runDetailHtml(run, data) {
  if (run.kind === "backtest") {
    const active = ["starting", "running", "stopping"].includes(run.status);
    const actions = `${run.selections ? `<button class="button button--secondary" type="button" id="restart-run">重新启动</button>` : ""}${active ? `<button class="button button--danger" type="button" id="stop-run">停止任务</button>` : ""}`;
    return `${detailHead(run, actions)}
    ${taskSummary(run, data.progress)}
    ${run.error || ["failed", "stopped"].includes(run.status) ? `<p class="notice" id="run-status-line">${escapeHtml(statusLine(run, data.status_sentence))}</p>` : ""}
    ${progressTable(data.progress)}
    ${backtestProgress(data.progress)}
    ${currentReportPanel(run, data.progress)}
    ${resultPanel(data.result)}
    ${completionNote(data.progress)}
    ${nextStepPanel(run, data.result, data.progress)}
    ${runTechnicalDetails(run, data)}`;
  }
  return analysisStatusHtml(run, data, {detail: true});
}

// WEBUI-007 item 1 (PM): 查看状态 is only the run list, on a desktop and on a phone,
// whatever the number of runs. Clicking a run opens its detail (#status/<id>) with
// 「‹ 全部任务」 top left; the row last opened stays highlighted when you come back.
// W67: a cut 「未知工具（…）」 cell carries the whole framework id on hover.
function frameworkTitle(item) {
  const name = String(item.framework_name || "");
  if (!name) return "";
  const full = name.startsWith("未知工具") && item.framework ? `未知工具（${item.framework}）` : name;
  return ` title="${escapeHtml(full)}"`;
}

function runListPage(run, data, runs) {
  const items = runs.filter((item) => runFilterMatch(item, state.runFilter || "all"));
  const head = heading("status", "查看状态", runsHeadline(data), '<button class="button button--secondary" type="button" id="refresh-run">刷新</button>');
  if (!run) return `${noticesHtml()}${head}${runFilterTabs(data)}${emptyRunsHtml(data)}`;
  if (state.runDetailOpen) {
    return `${noticesHtml()}${stopRefusedHtml(run)}<button type="button" class="back" id="runs-back">‹ 全部任务</button><div id="run-detail">${runDetailHtml(run, data)}</div>`;
  }
  const lastOpened = state.selectedRunId || "";
  return `${noticesHtml()}${head}${runFilterTabs(data)}${runsCapNote(data)}${narrowRuns() ? runCards(items, lastOpened) : runTable(items, lastOpened)}`;
}

// The run detail has its own address so a refresh stays on it and back returns to the list.
const STATUS_ROUTE = "#status";
const RUN_NOT_FOUND = "没有找到这个任务，可能已被删除。已回到任务列表。";

function statusRouteId(hash = window.location.hash) {
  const match = /^#status\/(.+)$/.exec(String(hash || ""));
  if (!match) return null;
  try { return runIdOf(decodeURIComponent(match[1])) || null; } catch { return null; }
}

function statusUrl(hash) {
  return `${window.location.pathname}${window.location.search}${hash}`;
}

function setStatusRoute(id, {push = false} = {}) {
  const target = id ? `${STATUS_ROUTE}/${encodeURIComponent(id)}` : STATUS_ROUTE;
  if (window.location.hash === target) return;
  try {
    window.history[push ? "pushState" : "replaceState"]({runDetail: id || null}, "", statusUrl(target));
  } catch { /* no history: the page still works, only without the address */ }
}

function clearStatusRoute() {
  if (!String(window.location.hash || "").startsWith(STATUS_ROUTE)) return;
  try { window.history.replaceState(null, "", statusUrl("")); } catch { /* see setStatusRoute */ }
}

function openRunDetail(id) {
  const runId = runIdOf(id);
  if (!runId) return;
  state.selectedRunId = runId;
  state.runDetailOpen = true;
  state.focusAfterRuns = {back: true};
  setStatusRoute(runId, {push: true});
  renderRuns();
}

function closeRunDetail() {
  const from = state.selectedRunId;
  state.runDetailOpen = false;
  state.focusAfterRuns = from ? {row: from} : null;
  // The detail was opened from the list: going back pops that entry, so the
  // browser's back button and 「‹ 全部任务」 never leave a stray detail entry behind.
  if (window.history.state?.runDetail && statusRouteId()) {
    window.history.back();
    return;
  }
  setStatusRoute(null);
  renderRuns();
}

window.addEventListener("popstate", () => {
  if (!state.catalog) return;
  const hash = String(window.location.hash || "");
  if (hash === START_ROUTE || hash.startsWith(`${START_ROUTE}/`)) {
    const framework = startRouteFramework();
    if (state.page === "analyze" && (state.analyzeFramework || null) === framework) return;
    if (state.page === "analyze") {
      state.analyzeFramework = framework;
      renderAnalyze();
    } else {
      showPage("analyze", {framework});
    }
    return;
  }
  const id = statusRouteId();
  if (id) {
    if (state.page === "runs" && state.runDetailOpen && state.selectedRunId === id) return;
    showPage("runs", {runDetail: id});
  } else if (window.location.hash === STATUS_ROUTE) {
    if (state.page === "runs" && !state.runDetailOpen) { renderRuns(); return; }
    const from = state.selectedRunId;
    showPage("runs");
    if (from && !state.focusAfterRuns) state.focusAfterRuns = {row: from};
  }
});

async function cancelQueuedRun(runId, button) {
  const id = runIdOf(runId);
  if (!id) {
    rememberNotice(RUN_ID_MISSING);
    await renderRuns();
    return;
  }
  if (button) button.disabled = true;
  try {
    state.stopRefused = null;   // W76: a new action replaces the old refusal
    const result = await api("/api/runs/cancel", { method: "POST", body: JSON.stringify({id}) });
    rememberNotice(result.zh || result.detail || "已取消排队。");
  } catch (error) {
    rememberNotice(error.message);
  }
  await renderRuns();
}

function bindRunList() {
  document.querySelectorAll("#run-filters [data-filter]").forEach((tab) => tab.addEventListener("click", () => {
    state.runFilter = tab.dataset.filter;
    renderRuns();
  }));
  document.querySelectorAll("#run-list [data-run-id]").forEach((row) => {
    const open = () => openRunDetail(row.dataset.runId);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => {
      if (event.target !== row || !["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      open();
    });
  });
  document.querySelectorAll("[data-cancel-run]").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    cancelQueuedRun(button.dataset.cancelRun, button);
  }));
  document.querySelector("#cancel-queued")?.addEventListener("click", (event) => cancelQueuedRun(event.currentTarget.dataset.runId, event.currentTarget));
  document.querySelector("#runs-back")?.addEventListener("click", closeRunDetail);
}

function updateRunBadge(count) {
  const nav = document.querySelector('.nav-item[data-page="runs"]');
  if (!nav) return;
  let badge = nav.querySelector(".nav-badge");
  const value = Number(count) || 0;
  if (!value) {
    badge?.remove();
    return;
  }
  if (!badge) {
    badge = document.createElement("span");
    badge.className = "nav-badge";
    badge.id = "runs-badge";
    nav.append(badge);
  }
  badge.textContent = String(value);
  badge.setAttribute("aria-label", `${value} 个运行中`);
}

function refreshRunBadge() {
  api("/api/runs").then((data) => updateRunBadge(data?.counts?.running)).catch(() => {});
}

async function renderRuns() {
  // A click (open a run, ‹ 全部任务, 重新启动) while a poll is in flight is not
  // dropped: the poll's reply is discarded and the page is drawn again afterwards.
  if (renderingRuns) { renderRunsAgain = true; return; }
  renderingRuns = true;
  renderRunsAgain = false;
  try {
  const data = await api(state.selectedRunId ? `/api/runs?id=${encodeURIComponent(state.selectedRunId)}` : "/api/runs");
  if (state.page !== "runs" || renderRunsAgain) return;
  if (state.runDetailOpen && (!data.run || ("selected" in data && data.selected !== state.selectedRunId))) {
    // #status/<id> of a run that is gone: show the list, not some other run's detail.
    state.runDetailOpen = false;
    state.selectedRunId = null;
    setStatusRoute(null);
    rememberNotice(RUN_NOT_FOUND);
  }
  const run = data.run;
  const runs = runListItems(run, data, Array.isArray(data.runs) ? data.runs : []);
  const listData = runListCounts(data, runs);
  updateRunBadge(listData.counts?.running);
  const sameTask = run?.id && state.renderedRunId === run.id;
  const openDetails = sameTask ? [...pageEl.querySelectorAll("details[id][open]")].map((item) => item.id) : [];
  const logInput = sameTask ? document.querySelector("#log-lines") : null;
  const logLength = logInput?.value;
  const logFocus = logInput && document.activeElement === logInput
    ? {start: logInput.selectionStart, end: logInput.selectionEnd} : null;
  pageEl.innerHTML = runListPage(run, listData, runs);
  state.renderedRunId = run?.id || null;
  document.querySelector("#show-saved-outline")?.addEventListener("click", () => showOutline(document.querySelector("#show-saved-outline").dataset.outline, document.querySelector("#run-outline")));
  document.querySelector("#start-analysis-from-runs")?.addEventListener("click", () => showPage("analyze"));
  document.querySelector("#open-reports-from-run")?.addEventListener("click", () => showPage("reports"));
  document.querySelector("#load-message-log")?.addEventListener("click", async (event) => {
    const view = document.querySelector("#message-tool-log");
    try {
      const data = await api(`/api/message-log?path=${encodeURIComponent(event.currentTarget.dataset.log)}`);
      if (view) view.textContent = (data.lines || []).join("\n") || `${logSentence("message_tool_log")} 是空的。`;
    } catch (error) {
      if (view) view.textContent = error.message;
    }
  });
  const loadLog = () => {
    const lines = document.querySelector("#log-lines")?.value || "40";
    const view = document.querySelector("#run-log");
    const path = document.querySelector("#log-path");
    api(`/api/logs?lines=${encodeURIComponent(lines)}${run?.id ? `&id=${encodeURIComponent(run.id)}` : ""}`).then((log) => {
      if (!view?.isConnected) return;
      if (path) path.textContent = logPathNotice(log);
      if (view) view.textContent = (log.lines || []).join("\n") || run.detail || "日志还是空的。";
    }).catch((error) => {
      if (!view?.isConnected) return;
      if (path) path.textContent = error.message;
      if (view) view.textContent = error.message;
    });
  };
  if (run) {
    const logDetails = document.querySelector("#run-log-details");
    logDetails?.addEventListener("toggle", () => {
      if (logDetails.open && logDetails.isConnected) loadLog();
    });
    document.querySelector("#log-lines")?.addEventListener("change", loadLog);
  }
  const updatedLogInput = document.querySelector("#log-lines");
  if (updatedLogInput && logLength != null) updatedLogInput.value = logLength;
  openDetails.forEach((id) => {
    const details = document.getElementById(id);
    if (details?.tagName === "DETAILS") details.open = true;
  });
  if (updatedLogInput && logFocus) {
    updatedLogInput.focus();
    updatedLogInput.setSelectionRange(logFocus.start, logFocus.end);
  }
  document.querySelector("#refresh-run")?.addEventListener("click", renderRuns);
  document.querySelector("#restart-run")?.addEventListener("click", (event) => restartRun(false, event.currentTarget, run?.id));
  document.querySelector("#resume-run")?.addEventListener("click", (event) => restartRun(true, event.currentTarget, run?.id));
  document.querySelector("#open-run-report")?.addEventListener("click", () => {
    state.reportFocus = data.result?.html || null;
    showPage("reports");
  });
  document.querySelector("#show-error-log")?.addEventListener("click", () => {
    const details = document.querySelector("#run-technical-details");
    const log = document.querySelector("#run-log-details");
    if (details) details.open = true;
    if (log && !log.open) log.open = true;
    (document.querySelector("#failure-message") || log)?.scrollIntoView({block: "start", behavior: "smooth"});
  });
  document.querySelector("#stop-run")?.addEventListener("click", (event) => confirmAction("停止当前分析？", "会停止当前任务，已产生的结果是否保留取决于任务进度。", async () => {
    const id = runIdOf(run?.id);
    if (!id) {
      rememberNotice(RUN_ID_MISSING);
      await renderRuns();
      return;
    }
    let result;
    try {
      result = await api("/api/runs/stop", { method: "POST", body: JSON.stringify({id}) });
    } catch (error) {
      // W58: a refused stop (e.g. the process start time is unreadable) shows the
      // console's sentence, which ends with the next step, instead of failing silently.
      // W76: kept in state, so the 4-second refresh redraws it instead of wiping it;
      // it stays until the next action on a run or another run is opened.
      state.stopRefused = {id, text: error.message};
      await renderRuns();
      document.querySelector("#stop-refused")?.scrollIntoView?.({block: "nearest"});
      return;
    }
    state.stopRefused = null;
    rememberNotice(result.zh || result.detail || "任务已停止。");
    await renderRuns();
  }, event.currentTarget));
  bindRunList();
  // Keyboard focus follows the move: onto 「‹ 全部任务」 in the detail, back onto the row on return.
  const focus = state.focusAfterRuns;
  state.focusAfterRuns = null;
  const focusTarget = focus?.back ? document.querySelector("#runs-back")
    : focus?.row ? [...document.querySelectorAll("#run-list [data-run-id]")].find((row) => row.dataset.runId === focus.row) : null;
  focusTarget?.focus?.();
  watchRunning(run, runs);
  } catch (error) {
    if (state.page !== "runs") return;
    pageEl.innerHTML = `${heading("", "查看状态", "任务状态暂时无法读取。请重试。", '<button class="button button--secondary" type="button" id="refresh-run">重新加载</button>')}<p class="error-banner" role="alert">${escapeHtml(error.message)}</p>`;
    document.querySelector("#refresh-run").addEventListener("click", renderRuns);
  } finally {
    renderingRuns = false;
    if (renderRunsAgain && state.page === "runs") renderRuns();
  }
}

function completionNote(progress) {
  if (!progress || !progress.complete) return "";
  return `<p class="notice">分析已完成。</p>`;
}

function reviewNote(rating, sentence) {
  if (!sentence && rating !== "REVIEW") return "";
  return `<p class="notice">需要复核：未能从最终决定读出可交易评级。请查看决定内容后自行判断，或重新运行分析。</p>`;
}

function shownReport(result) {
  const shown = result && result.shown_report;
  if (!shown || !Array.isArray(shown.sections)) return "";
  const rule = shown.rule || displaySentence("complete_report");
  const headings = state.catalog?.report_sections || [];
  const agents = state.catalog?.show_agents || [];
  const headingZh = (heading) => (headings.find((item) => item.cli === heading) || {}).zh || heading;
  const agentZh = (agent) => (agents.find((item) => item.agent === agent) || {}).zh || agent;
  const blocks = shown.sections.map((section) => {
    const items = (section.items || []).map((item) => `<h3>${escapeHtml(agentZh(item.agent))}</h3><pre class="log-view">${escapeHtml(item.text || "")}</pre>`).join("");
    return `<section class="panel"><h2>${escapeHtml(headingZh(section.heading))}</h2>${items}</section>`;
  }).join("");
  const empty = shown.sections.length ? "" : `<p class="notice">这次没有可显示的报告分段。</p>`;
  return `<details class="disclosure"><summary>查看完整分析报告</summary><section class="panel"><h2>完整报告</h2>${empty}${blocks}</section></details>`;
}

function resultPanel(result) {
  if (!result || !result.present) {
    return `<section class="panel" id="run-result"><h2>结果</h2><p class="notice">${escapeHtml(result?.note || "结果将在任务完成后显示。")}</p></section>`;
  }
  if (result.kind === "backtest") {
    const text = result.text || "";
    return `<section class="panel" id="run-result"><h2>回测结果</h2>${text ? `<pre class="log-view">${escapeHtml(text)}</pre>` : `<p class="notice">结果文件为空。</p>`}</section>`;
  }
  const decision = result.decision || "—";
  const rating = result.rating || "—";
  const logName = logSentence("message_tool_log");
  const toolLog = result.message_tool_log
    ? `<details class="disclosure"><summary>查看消息与工具日志</summary><p class="notice">日志文件：${escapeHtml(result.message_tool_log)}</p><button class="button button--small button--secondary" type="button" id="load-message-log" data-log="${escapeHtml(result.message_tool_log)}">读取日志</button><pre class="log-view" id="message-tool-log"></pre></details>`
    : "";
  const reportDownload = result.report
    ? `<a class="button button--secondary" href="${reportDownloadHref(result.report)}" download="${escapeHtml(reportFileName(result.report))}">下载报告</a>`
    : "";
  const html = result.html
    ? `<p class="inline-actions"><a class="button button--secondary" id="open-html" href="${reportFileHref(result.html)}" target="_blank" rel="noopener">打开 HTML</a><a class="button button--secondary" href="${reportDownloadHref(result.html)}" download="${escapeHtml(reportFileName(result.html))}">下载 HTML</a>${reportDownload}<button class="button button--secondary" type="button" id="show-saved-outline" data-outline="${escapeHtml(result.html)}">查看报告内容</button></p><div id="run-outline"></div>`
    : `<p class="inline-actions">${reportDownload}</p><p class="notice">没有可打开的 HTML 报告。</p>`;
  const reportMeta = result.report || result.saved_to
    ? `<details class="disclosure"><summary>查看文件信息</summary><p class="notice">${result.report ? `报告文件：${escapeHtml(result.report)}` : ""}${result.saved_to ? `<br>保存位置：${escapeHtml(result.saved_to)}` : ""}</p></details>`
    : "";
  return `<section class="panel" id="run-result"><h2>分析结果</h2>${reviewNote(result.rating, result.review)}<div class="metric-grid"><div class="metric-card"><span class="metric-label">决定</span><p class="metric-detail">${escapeHtml(decision)}</p></div><div class="metric-card"><span class="metric-label">评级</span><strong>${escapeHtml(rating)}</strong></div></div>${saveNotice(result)}${html}${shownReport(result)}${toolLog}${reportMeta}</section>`;
}

function saveNotice(result) {
  if (result.save_error) {
    return `<p class="error-banner">保存报告失败：${escapeHtml(result.save_error)}</p>`;
  }
  if (result.saved === false) {
    return `<p class="notice">本次未保存报告。</p>`;
  }
  const lines = Array.isArray(result.save_lines) && result.save_lines.length
    ? result.save_lines
    : fallbackSaveLines(result);
  if (!lines.length) return "";
  return `<p class="notice">报告已保存。</p><details class="disclosure"><summary>查看保存信息</summary><pre class="log-view">${escapeHtml(lines.join("\n"))}</pre></details>`;
}

function fallbackSaveLines(result) {
  const prompts = state.catalog?.report_prompts || {};
  const lines = [];
  if (result.saved_to) lines.push(`${prompts.saved_to} ${result.saved_to}`);
  if (result.report) lines.push(`${prompts.complete_report} ${String(result.report).split("/").pop()}`);
  if (result.html) lines.push(`${prompts.html_report} ${String(result.html).split("/").pop()}`);
  return lines;
}

function messageTypeRows() {
  return (state.catalog && state.catalog.message_types) || [];
}

function messageTypeLabel(type) {
  const row = messageTypeRows().find((item) => item.cli === type);
  return row ? row.zh : type;
}

function messageTypeLine() {
  return messageTypeRows().map((item) => `${item.zh} ${item.cli}`).join("、");
}

function catalogColumns(key) {
  return (displaySentence(key) || "").split(" / ").filter(Boolean);
}

function messageHeaders() {
  const zh = ["时间", "类型", "内容"];
  return catalogColumns("message_columns").map((cli, index) => `<th>${escapeHtml(zh[index] || "")} ${escapeHtml(cli)}</th>`).join("");
}

function messageTable(progress) {
  const rows = progress?.panel || progress?.messages || [];
  if (!rows.length) return "";
  return `<section class="panel"><h2>运行消息</h2><div class="table-wrap"><table class="data-table"><thead><tr>${messageHeaders()}</tr></thead><tbody>
    ${rows.map((row) => `<tr><td>${escapeHtml(row.time || "")}</td><td><strong>${escapeHtml(messageTypeLabel(row.type))}</strong> <span>${escapeHtml(row.type)}</span></td><td>${escapeHtml(row.content)}</td></tr>`).join("")}
  </tbody></table></div></section>`;
}

function wallNote(progress) {
  const pending = state.catalog?.wall_time_pending || "";
  if (!progress?.wall_time) return `<p class="notice">分析师耗时：${escapeHtml(pending || "暂未完成计时")}</p>`;
  return `<p class="notice">分析师耗时：${escapeHtml(progress.wall_time)}</p>`;
}

function statsNote(progress) {
  if (!progress?.stats) return `<p class="notice">暂无运行统计。</p>`;
  return `<p class="notice">运行统计：${escapeHtml(progress.stats)}</p>`;
}

function backtestProgress(progress) {
  const item = progress?.backtest;
  if (!item) return "";
  const latest = item.latest;
  const summary = (item.summary || []).map((line) => `<li>${escapeHtml(line)}</li>`).join("");
  if (!latest) return summary ? `<details class="disclosure"><summary>查看回测摘要</summary><ul class="command-map">${summary}</ul></details>` : "";
  return `<section class="panel"><h2>回测进度</h2><p class="notice"><strong>${escapeHtml(latest.done)}</strong> / ${escapeHtml(latest.total)} 个格子完成 · ${escapeHtml(latest.ticker)} · ${escapeHtml(latest.date)}</p>${summary ? `<details class="disclosure"><summary>查看回测摘要</summary><ul class="command-map">${summary}</ul></details>` : ""}</section>`;
}

function toolRowContent(row) {
  return `${row?.name || ""}: ${row?.args || ""}`;
}

function toolTable(progress) {
  const rows = progress?.tools || [];
  if (!rows.length) return "";
  return `<section class="panel"><h2>工具调用</h2><div class="table-wrap"><table class="data-table"><thead><tr>${messageHeaders()}</tr></thead><tbody>
    ${rows.map((row) => `<tr><td>${escapeHtml(row.time || "")}</td><td><strong>${escapeHtml(messageTypeLabel("Tool"))}</strong> <span>Tool</span></td><td>${escapeHtml(toolRowContent(row))}</td></tr>`).join("")}
  </tbody></table></div></section>`;
}

function progressTeamMap() {
  const rows = (state.catalog && state.catalog.progress_teams) || [];
  return `<ul class="command-map">${rows.map((row) => `<li><span>${escapeHtml(row.team_zh)} · ${escapeHtml(row.zh)}</span><code>${escapeHtml(row.team)} / ${escapeHtml(row.agent)}</code></li>`).join("")}</ul>`;
}

function progressStatusLine() {
  return (state.catalog?.progress_statuses || []).map((item) => `${item.zh} ${item.cli}`).join("、");
}

function progressHeaders() {
  return ["团队", "角色", "状态"].map((label) => `<th>${escapeHtml(label)}</th>`).join("");
}

function progressTechnicalDetails(progress) {
  if (!progress?.agents?.length) return "";
  return `<details class="disclosure"><summary>查看进度来源</summary><p class="notice">详细列：${escapeHtml(displaySentence("progress_columns"))}。状态：${escapeHtml(progressStatusLine())}。</p>${progressTeamMap()}</details>`;
}

function progressTable(progress) {
  const rows = progress?.agents || [];
  if (!rows.length) return "";
  return `<section class="panel"><h2>进度</h2><div class="table-wrap"><table class="data-table"><thead><tr>${progressHeaders()}</tr></thead><tbody>
    ${rows.map((row) => `<tr><td><strong>${escapeHtml(row.team_zh)}</strong></td><td>${escapeHtml(row.zh)}</td><td><span class="status-badge ${runStatusClass(row.status === "completed" ? "completed" : row.status === "error" ? "failed" : row.status === "in_progress" ? "running" : "idle")}">${escapeHtml(row.status_zh)}</span></td></tr>`).join("")}
  </tbody></table></div></section>`;
}

function showAgentMap() {
  const rows = (state.catalog && state.catalog.show_agents) || [];
  if (!rows.length) return "";
  return `<details class="disclosure"><summary>查看报告角色映射</summary><section class="panel"><h2>完整报告角色</h2><ul class="command-map">${rows.map((row) => `<li><span>${escapeHtml(row.heading_zh)} · ${escapeHtml(row.zh)}</span><code>${escapeHtml(row.heading)} / ${escapeHtml(row.agent)} / ${escapeHtml(row.file)}</code></li>`).join("")}</ul></section></details>`;
}

function logPathNotice(log) {
  if (!log) return "正在读取。读到路径后显示命令行 logs 打印的路径句，不预写。";
  const parts = [];
  if (log.cli) parts.push(`对应命令行：${log.cli}`);
  if (log.error) parts.push(log.error);
  if (!log.cli && !log.error) parts.push("这次控制台任务还没有日志路径。不编造命令行的路径句。");
  return parts.join("\n");
}

function logTailPanel() {
  return `<details class="disclosure" id="report-log-details"><summary>查看运行日志</summary><section class="panel"><h2>日志</h2><div class="field"><label for="report-log-lines">显示日志行数</label><input id="report-log-lines" value="40"></div><p class="notice" id="report-log-path">${escapeHtml(logPathNotice(null))}</p><pre class="log-view" id="report-log">展开后读取日志。</pre>${logsHelpNote()}</section></details>`;
}

function bindLogTail() {
  const load = () => {
    const lines = document.querySelector("#report-log-lines")?.value || "40";
    api(`/api/logs?lines=${encodeURIComponent(lines)}`).then((log) => {
      const view = document.querySelector("#report-log");
      const path = document.querySelector("#report-log-path");
      if (path) path.textContent = logPathNotice(log);
      if (view) view.textContent = (log.lines || []).join("\n") || "日志还是空的。";
    }).catch((error) => {
      const path = document.querySelector("#report-log-path");
      const view = document.querySelector("#report-log");
      if (path) path.textContent = error.message;
      if (view) view.textContent = error.message;
    });
  };
  const details = document.querySelector("#report-log-details");
  details?.addEventListener("toggle", () => {
    if (details.open) load();
  });
  document.querySelector("#report-log-lines")?.addEventListener("change", load);
  if (!details) load();
}

const RATING_ZH = {
  BUY: ["买入", "buy"],
  OVERWEIGHT: ["增持", "buy"],
  HOLD: ["持有", "hold"],
  UNDERWEIGHT: ["减持", "sell"],
  SELL: ["卖出", "sell"],
  REVIEW: ["需复核", "wait"],
};

function ratingInfo(rating) {
  const found = RATING_ZH[String(rating || "").trim().toUpperCase()];
  if (found) return {zh: found[0], tone: found[1]};
  return rating ? {zh: String(rating), tone: "wait"} : {zh: "无评级", tone: "wait"};
}

function ratingPill(rating) {
  const info = ratingInfo(rating);
  return `<span class="pill ${info.tone}" title="${escapeHtml(rating || "")}">${escapeHtml(info.zh)}</span>`;
}

function reportDate(row) {
  if (row.date) return row.date;
  const stamp = /_(\d{4})(\d{2})(\d{2})_\d{6}$/.exec(row.name || "");
  return stamp ? `${stamp[1]}-${stamp[2]}-${stamp[3]}` : "";
}

function reportDepth(row) {
  if (row.debate_rounds == null) return "";
  const depth = (state.catalog?.depths || []).find((item) => Number(item.value) === Number(row.debate_rounds));
  return depth ? `${depth.zh}深度` : `${row.debate_rounds} 轮辩论`;
}

function reportListRow(row, selected) {
  const sub = [reportDate(row), reportDepth(row)].filter(Boolean).join(" · ");
  return `<li><button class="ri${selected ? " on" : ""}" type="button" data-report="${escapeHtml(row.html)}" aria-current="${selected ? "true" : "false"}">
    <span class="a"><span>${escapeHtml(row.ticker || row.name)}</span>${ratingPill(row.rating)}</span>
    <span class="b">${escapeHtml(sub || row.name)}</span></button></li>`;
}

/* A small, safe markdown renderer: the text is escaped first, then only headings,
   lists, tables, bold, italics and inline code are turned into tags. */
function inlineFormat(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
}

// W34: the folder relative links in the report being shown resolve against.
let markdownLinkBase = "";

function joinReportPath(base, relative) {
  const absolute = relative.startsWith("/") || /^[A-Za-z]:[\\/]/.test(relative);
  const parts = (absolute ? relative : `${base}/${relative}`).split(/[\\/]/);
  const out = [];
  for (const part of parts) {
    if (part === "..") out.pop();
    else if (part !== "." && part !== "") out.push(part);
  }
  return `${/^[A-Za-z]:$/.test(out[0] || "") ? "" : "/"}${out.join("/")}`;
}

/* W34: a markdown link in a report. http(s) opens outside the console with no referrer;
   a file link goes through /api/reports/file (which only serves files under the results
   directory); any other scheme (javascript:, data:, ...) stays plain text. */
function markdownLink(label, url) {
  const text = inlineFormat(label);
  if (/^https?:\/\//i.test(url)) {
    return `<a class="md-link md-link--external" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer nofollow">${text}</a>`;
  }
  let path = url;
  if (/^file:/i.test(url)) path = decodeURIComponent(url.replace(/^file:(\/\/[^/]*)?/i, ""));
  else if (/^[a-z][a-z0-9+.-]*:/i.test(url) && !/^[A-Za-z]:[\\/]/.test(url)) return text;
  if (path.startsWith("#") || !path.trim()) return text;
  path = path.split("#")[0].split("?")[0];
  return `<a class="md-link md-link--file" href="${reportFileHref(joinReportPath(markdownLinkBase, path))}" target="_blank" rel="noopener noreferrer">${text}</a>`;
}

function inlineMarkdown(text) {
  const value = String(text ?? "");
  const pattern = /\[([^\]\n]+)\]\(([^()\s]+)\)/g;
  let html = "";
  let last = 0;
  for (const match of value.matchAll(pattern)) {
    html += inlineFormat(value.slice(last, match.index)) + markdownLink(match[1], match[2]);
    last = match.index + match[0].length;
  }
  return html + inlineFormat(value.slice(last));
}

function markdownTable(lines) {
  const cells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  const [head, , ...body] = lines;
  return `<div class="table-wrap" tabindex="0"><table class="data-table md-table"><thead><tr>${cells(head).map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>${body.map((line) => `<tr>${cells(line).map((cell) => `<td>${inlineMarkdown(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function renderMarkdown(text) {
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let paragraph = [];
  let list = null;
  const flush = () => {
    if (paragraph.length) out.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
    paragraph = [];
    if (list) out.push(`<${list.tag}>${list.items.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</${list.tag}>`);
    list = null;
  };
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|?\s*:?-{2,}/.test(lines[index + 1] || "")) {
      flush();
      const block = [line];
      while (index + 1 < lines.length && /^\s*\|.*\|\s*$/.test(lines[index + 1])) block.push(lines[++index]);
      out.push(markdownTable(block));
    } else if (heading) {
      flush();
      const level = Math.min(6, heading[1].length + 2);
      out.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
    } else if (bullet || numbered) {
      const tag = bullet ? "ul" : "ol";
      if (paragraph.length || (list && list.tag !== tag)) flush();
      if (!list) list = {tag, items: []};
      list.items.push((bullet || numbered)[1]);
    } else if (!line.trim()) {
      flush();
    } else if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      flush();
      out.push("<hr>");
    } else {
      if (list) flush();
      paragraph.push(line.trim());
    }
  }
  flush();
  return out.join("");
}

function decisionSummary(text) {
  const plain = String(text || "").split(/\n\s*\n/).map((block) => block.replace(/^#+\s.*$/gm, "").replace(/[*_`>#|]/g, "").replace(/\s+/g, " ").trim()).find((block) => block.length > 20) || "";
  return plain.length > 220 ? `${plain.slice(0, 218)}…` : plain;
}

function reportTabs(outline) {
  const byFolder = Object.fromEntries((outline.sections || []).map((section) => [section.folder, section]));
  const tabs = [];
  const decision = byFolder["5_portfolio"];
  if (decision) tabs.push({key: "decision", label: "综合结论", items: decision.items, single: true});
  for (const item of byFolder["1_analysts"]?.items || []) {
    tabs.push({key: `analyst-${item.file}`, label: String(item.zh || item.agent).replace(/分析师$/, "") || item.zh, items: [item], single: true});
  }
  if (byFolder["2_research"]) tabs.push({key: "research", label: "研究辩论", items: byFolder["2_research"].items});
  if (byFolder["3_trading"]) tabs.push({key: "trading", label: "交易计划", items: byFolder["3_trading"].items, single: true});
  if (byFolder["4_risk"]) tabs.push({key: "risk", label: "风险评估", items: byFolder["4_risk"].items});
  return tabs;
}

function tabBody(tab) {
  return tab.items.map((item) => `${tab.single ? "" : `<h3 class="md-role">${escapeHtml(item.zh || item.agent)}</h3>`}${renderMarkdown(item.text)}`).join("");
}

function reportDetailHtml(row, outline) {
  markdownLinkBase = String(row?.html || "").replace(/[\\/][^\\/]*$/, "");
  const tabs = reportTabs(outline);
  state.reportTab = tabs.some((tab) => tab.key === state.reportTab) ? state.reportTab : tabs[0]?.key;
  const decisionText = (outline.sections || []).find((section) => section.folder === "5_portfolio")?.items?.[0]?.text || "";
  const rating = outline.rating || row.rating;
  const info = ratingInfo(rating);
  const finished = row.generated ? `完成于 ${String(row.generated).slice(11, 16)}` : "";
  const models = [row.quick_model ? `快 ${row.quick_model}` : "", row.deep_model ? `深 ${row.deep_model}` : ""].filter(Boolean).join(" · ");
  const sub = [finished, reportDepth(row), models].filter(Boolean).join(" · ");
  const downloads = `${row.markdown ? `<a class="button button--secondary" href="${reportDownloadHref(row.markdown)}" download="${escapeHtml(reportFileName(row.markdown))}">下载 Markdown</a>` : ""}<a class="button button--secondary" href="${reportDownloadHref(row.html)}" download="${escapeHtml(reportFileName(row.html))}">下载 HTML</a>`;
  return `<div class="rc-head"><div class="rc-title"><h2 id="report-title">${escapeHtml(row.ticker || row.name)} · ${escapeHtml(reportDate(row) || "—")}</h2>${sub ? `<p class="page-description">${escapeHtml(sub)}</p>` : ""}</div><div class="rc-actions">${downloads}</div></div>
    ${reviewNote(rating)}
    <div class="decision"><div class="decision-rating"><div class="k">组合经理结论</div><div class="big big--${info.tone}">${escapeHtml(info.zh)}</div></div><div class="decision-text">${escapeHtml(decisionSummary(decisionText) || "这份报告没有组合经理的结论段落。")}</div></div>
    ${tabs.length ? `<div class="tabs" role="tablist" aria-label="报告分段">${tabs.map((tab) => `<button class="tab${tab.key === state.reportTab ? " on" : ""}" type="button" role="tab" id="tab-${escapeHtml(tab.key)}" data-tab="${escapeHtml(tab.key)}" aria-selected="${tab.key === state.reportTab}" aria-controls="report-body" tabindex="${tab.key === state.reportTab ? "0" : "-1"}">${escapeHtml(tab.label)}</button>`).join("")}</div>
    <div class="report-body md" id="report-body" role="tabpanel" aria-labelledby="tab-${escapeHtml(state.reportTab)}" tabindex="0">${tabBody(tabs.find((tab) => tab.key === state.reportTab))}</div>` : `<p class="notice">这份报告没有可显示的分段。</p>`}`;
}

async function showReport(path) {
  const host = document.querySelector("#report-detail");
  const row = (state.reportRows || []).find((item) => item.html === path);
  if (!host || !row) return;
  state.reportFocus = path;
  document.querySelectorAll("[data-report]").forEach((button) => {
    const on = button.dataset.report === path;
    button.classList.toggle("on", on);
    button.setAttribute("aria-current", String(on));
  });
  host.innerHTML = `<p class="notice">正在读取报告…</p>`;
  try {
    const outline = await api(`/api/reports/outline?path=${encodeURIComponent(path)}`);
    if (state.page !== "reports" || state.reportFocus !== path) return;
    const paint = () => {
      host.innerHTML = reportDetailHtml(row, outline);
      const tabButtons = [...host.querySelectorAll("[data-tab]")];
      tabButtons.forEach((button, index) => {
        button.addEventListener("click", () => { state.reportTab = button.dataset.tab; paint(); [...host.querySelectorAll("[data-tab]")].find((tab) => tab.dataset.tab === state.reportTab)?.focus(); });
        button.addEventListener("keydown", (event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === "Home" ? 0 : event.key === "End" ? tabButtons.length - 1
            : (index + (event.key === "ArrowRight" ? 1 : -1) + tabButtons.length) % tabButtons.length;
          tabButtons[next].click();
        });
      });
    };
    paint();
    const meta = document.querySelector("#report-meta-lines");
    if (meta) meta.textContent = (outline.header || []).join("\n") || "这份报告没有元信息。";
  } catch (error) {
    host.innerHTML = `<p class="error-banner">${escapeHtml(error.message)}</p>`;
  }
}

function paintReportList() {
  const list = document.querySelector("#report-list");
  if (!list) return;
  const query = (document.querySelector("#report-filter")?.value || "").trim().toUpperCase();
  const rows = (state.reportRows || []).filter((row) => !query || String(row.ticker || row.name).toUpperCase().includes(query));
  list.innerHTML = rows.length ? rows.map((row) => reportListRow(row, row.html === state.reportFocus)).join("") : `<li class="ri-empty">没有匹配「${escapeHtml(query)}」的报告。</li>`;
  list.querySelectorAll("[data-report]").forEach((button) => button.addEventListener("click", () => showReport(button.dataset.report)));
}

async function renderReports() {
  try {
    const data = await api("/api/reports");
    if (state.page !== "reports") return;
    const rows = data.reports || [];
    state.reportRows = rows;
    if (!rows.some((row) => row.html === state.reportFocus)) state.reportFocus = rows[0]?.html || null;
    pageEl.innerHTML = `
      ${heading("reports", "报告", "每次分析的结论和全文，可以直接下载。")}
      ${rows.length ? `<div class="rep" id="saved-reports">
        <div class="rl"><div class="rlh"><label class="visually-hidden" for="report-filter">按标的筛选</label><input id="report-filter" type="search" placeholder="按标的筛选" autocomplete="off"></div><ul class="report-list" id="report-list" aria-label="已保存报告"></ul></div>
        <div class="rc"><div id="report-detail" aria-live="polite"></div>
          <details class="disclosure" id="report-technical-details"><summary>运行日志与技术信息</summary>
            <p class="notice">结果目录：${escapeHtml(data.directory || `沿用 ${envName("results_dir")}`)}</p>
            <p class="inline-actions"><a class="button button--small button--secondary" id="open-report-html" href="#" target="_blank" rel="noopener">在新窗口打开 HTML</a></p>
            <p class="notice">报告元信息</p><pre class="log-view" id="report-meta-lines">读取报告后显示。</pre>
            ${liveSectionTable(data.live || [])}
            ${logTailPanel()}
            ${commandMap([{cli: "--show", zh: "显示完整报告"}, {cli: "complete_report.html", zh: "打开已保存的 HTML"}])}${flagTable(state.catalog?.report_sections || [])}${showAgentMap()}${liveTitlePanel()}
          </details>
        </div>
      </div>` : `<div class="empty-state" id="saved-reports"><div><h2>还没有汇总报告</h2><p>完成一次分析并保存后，报告会出现在这里。</p><button class="button button--primary" type="button" id="start-analysis-from-reports">启动分析</button></div></div>
      ${liveSectionTable(data.live || [])}
      ${logTailPanel()}`}`;
    document.querySelector("#report-filter")?.addEventListener("input", paintReportList);
    document.querySelectorAll("[data-live]").forEach((button) => button.addEventListener("click", () => showLiveSection(button.dataset.live)));
    document.querySelector("#start-analysis-from-reports")?.addEventListener("click", () => showPage("analyze"));
    const openHtml = document.querySelector("#open-report-html");
    openHtml?.addEventListener("click", (event) => {
      if (!state.reportFocus) { event.preventDefault(); return; }
      openHtml.href = reportFileHref(state.reportFocus);
    });
    bindLogTail();
    paintReportList();
    if (state.reportFocus) showReport(state.reportFocus);
  } catch (error) {
    if (state.page !== "reports") return;
    pageEl.innerHTML = `${heading("reports", "报告", "每次分析的结论和全文，可以直接下载。")}<p class="error-banner">${escapeHtml(error.message)}</p>`;
  }
}

function liveTitleLabel(title) {
  const rows = (state.catalog && state.catalog.live_report_titles) || [];
  const found = rows.find((item) => item.cli === title);
  return escapeHtml(found ? found.zh : title);
}

function liveSectionTable(rows) {
  if (!rows.length) return "";
  return `<details class="disclosure" id="live-sections"><summary>查看分析过程中的分段（${escapeHtml(rows.length)}）</summary><section class="panel"><h2>命令行分段文件</h2><p class="notice">这些文件可能在任务结束后仍留在磁盘上；有文件不等于分析正在进行。</p><div class="table-wrap"><table class="data-table"><thead><tr><th>标的</th><th>日期</th><th>文件</th><th>标题</th></tr></thead><tbody>
    ${rows.map((row) => `<tr><td><strong>${escapeHtml(row.ticker)}</strong></td><td>${escapeHtml(row.date)}</td><td><button class="button button--small button--secondary" type="button" data-live="${escapeHtml(row.path)}">${escapeHtml(row.file)}</button></td><td>${liveTitleLabel(row.title)}</td></tr>`).join("")}
  </tbody></table></div><pre class="log-view" id="live-section"></pre></section></details>`;
}

async function showLiveSection(path) {
  const host = document.querySelector("#live-section");
  if (!host) return;
  host.textContent = "正在读取命令行分段文件。";
  try {
    const data = await api(`/api/live-sections?path=${encodeURIComponent(path)}`);
    host.textContent = data.text || "文件是空的。";
  } catch (error) {
    host.textContent = error.message;
  }
}

async function showOutline(path, host) {
  host = host || document.querySelector("#report-outline") || document.querySelector("#run-outline");
  if (!host) return;
  host.textContent = "正在读取已保存的报告。";
  try {
    const data = await api(`/api/reports/outline?path=${encodeURIComponent(path)}`);
    if (!data.sections.length) {
      host.innerHTML = `<p class="notice">这份报告没有可显示的分段。</p>`;
      return;
    }
    const header = (data.header || []).length
      ? `<details class="disclosure"><summary>查看报告元信息</summary><ul class="command-map">${data.header.map((line) => `<li><code>${escapeHtml(line)}</code></li>`).join("")}</ul></details>`
      : "";
    host.innerHTML = `${reviewNote(data.rating)}<p class="notice">${escapeHtml(data.name)}${data.rating ? ` · 评级 ${escapeHtml(data.rating)}` : ""}</p>${header}` + data.sections.map((section) => `
      <section class="panel"><h2>${escapeHtml(section.zh)}</h2>
      ${section.items.map((item) => `<h3>${escapeHtml(item.zh)}</h3><pre class="log-view">${escapeHtml(item.text)}</pre>`).join("")}
      </section>`).join("");
  } catch (error) {
    host.innerHTML = `<p class="error-banner">${escapeHtml(error.message)}</p>`;
  }
}

function ensureBacktest() {
  if (!state.backtest) {
    state.backtest = {
      tickers: "",
      start: "",
      end: "",
      every: Number(placeholder("every")),
      asset_type: "stock",
      analysts: (state.catalog.analysts || []).map((item) => item.value),
      run_id: "",
      portfolio: "",
    };
  }
  return state.backtest;
}

function backtestAnalystChoices() {
  const asset = ensureBacktest().asset_type;
  return (state.catalog.analysts || []).filter((item) => (asset !== "crypto" && asset !== "commodity") || item.value !== "fundamentals");
}

function backtestPayload() {
  const book = ensureBacktest();
  book.tickers = document.querySelector("#bt-tickers").value;
  book.start = document.querySelector("#bt-start").value;
  book.end = document.querySelector("#bt-end").value;
  book.every = Number(document.querySelector("#bt-every").value);
  book.run_id = document.querySelector("#bt-run")?.value.trim() || "";
  book.portfolio = document.querySelector("#bt-portfolio")?.value || "";
  if (!book.tickers.split(",").some((ticker) => ticker.trim())) throw new Error("请至少填写一个标的代码。");
  if (!book.start || !book.end) throw new Error("请选择回测的开始日期和结束日期。");
  if (book.start > book.end) throw new Error("结束日期不能早于开始日期。");
  if (!Number.isInteger(book.every) || book.every < 1) throw new Error("分析间隔需填写大于 0 的整数天数。");
  if (!book.analysts.length) throw new Error("请至少选择一个分析维度。");
  syncBacktestModels();
  return {
    ...state.form,
    tickers: book.tickers,
    start: book.start,
    end: book.end,
    every: book.every,
    asset_type: book.asset_type,
    analysts: book.analysts,
    portfolio: parsePortfolio(book.portfolio),
    run_id: book.run_id || null,
    quick_think_provider: state.form.quick_think_provider,
    quick_think_llm: state.form.quick_think_llm,
    deep_think_provider: state.form.deep_think_provider,
    deep_think_llm: state.form.deep_think_llm,
  };
}

function syncBacktestModels() {
  const read = (id, fallback) => document.querySelector(id)?.value.trim() || fallback;
  state.form.quick_think_provider = read("#quick-provider", state.form.quick_think_provider);
  state.form.quick_think_llm = read("#quick-model", state.form.quick_think_llm);
  state.form.deep_think_provider = read("#deep-provider", state.form.deep_think_provider);
  state.form.deep_think_llm = read("#deep-model", state.form.deep_think_llm);
}


function backtestAnalystNote(asset) {
  if (asset === "crypto") {
    return `加密资产没有基本面分析师。命令行：${state.catalog?.confirmation_sentences?.unavailable_crypto || ""}。`;
  }
  if (asset === "commodity") {
    return "贵金属/商品期货没有基本面分析师（没有公司财报）。";
  }
  return "当前不是 crypto。命令行不会打印加密资产不可用那句。";
}


function backtestMissingNote() {
  const note = state.catalog?.backtest_missing_note || "";
  const missing = state.catalog?.backtest_missing || {};
  const blocks = ["tickers", "start", "end"].map((key) => {
    const sentence = missing[key];
    return sentence ? `<pre class="log-view">${escapeHtml(sentence)}</pre>` : "";
  }).join("");
  return `<section class="panel" id="backtest-missing"><h2>缺少必填参数 <span>backtest</span></h2><p class="notice">${escapeHtml(note)}</p>${blocks}</section>`;
}

function renderBacktest() {
  const book = ensureBacktest();
  const analysts = backtestAnalystChoices();
  pageEl.innerHTML = `
    ${heading("", "回测", "在历史日期上重复分析，评估过往判断。结果不包含成交、滑点和手续费。")}
    <section class="panel"><h2>设置回测范围</h2>
    <form class="form-grid" id="backtest-form">
      <div class="field field--wide"><label for="bt-tickers">标的代码</label><input id="bt-tickers" value="${escapeHtml(book.tickers)}" placeholder="例如 NVDA,AAPL"><p class="form-hint">多个标的用英文逗号分隔。</p></div>
      <div class="field"><label for="bt-start">开始日期</label><input id="bt-start" type="date" value="${escapeHtml(book.start)}"></div>
      <div class="field"><label for="bt-end">结束日期</label><input id="bt-end" type="date" value="${escapeHtml(book.end)}"></div>
      <div class="field"><label>资产类型</label><div class="choice-row" id="bt-asset"></div></div>
      <div class="field"><label for="bt-every">每隔多少天分析一次</label><input id="bt-every" type="number" min="1" step="1" value="${escapeHtml(book.every)}"><p class="form-hint">间隔越小，分析次数越多，耗时和模型用量越高。</p></div>
      <details class="disclosure field--wide"><summary>分析维度与模型</summary>
        <div class="field"><label>分析维度</label><div class="choice-row" id="bt-analysts"></div><p class="form-hint">${book.asset_type === "crypto" ? "加密资产不使用基本面分析。" : book.asset_type === "commodity" ? "贵金属/商品期货不使用基本面分析。" : "默认使用全部分析维度。"}</p></div>
        <div class="model-settings-grid">${["quick", "deep"].map((tier) => `<section class="panel"><h3>${tier === "quick" ? "快模型" : "深模型"}</h3><div class="field"><label>供应商</label><div id="${tier}-providers"></div><input id="${tier}-provider" type="hidden" value="${escapeHtml(state.form[`${tier}_think_provider`] || "")}"></div><div class="field"><label>模型</label><div class="choice-row" id="${tier}-models"></div></div><details><summary>手动填写模型 ID</summary><input id="${tier}-model" value="${escapeHtml(state.form[`${tier}_think_llm`] || "")}" aria-label="${tier === "quick" ? "快模型" : "深模型"} ID"></details></section>`).join("")}</div>
        <div class="field" id="model-effort"></div>
      </details>
      <p class="notice field--wide" id="backtest-model-summary">当前模型：${escapeHtml(state.form.quick_think_llm || "快模型未选择")} / ${escapeHtml(state.form.deep_think_llm || "深模型未选择")}。可展开上方设置更改。</p>
      <details class="disclosure field--wide"><summary>持仓与继续上次回测</summary>
        <div class="field"><label for="bt-portfolio">持仓（可选）</label>${filePicker("bt-portfolio-file", "选择回测持仓 JSON 文件")}<textarea id="bt-portfolio" placeholder="${escapeHtml(placeholder("portfolio"))}">${escapeHtml(book.portfolio)}</textarea><p class="form-hint">持仓在整个回测期间保持不变。</p></div>
        <div class="field"><label for="bt-run">已有回测编号（可选）</label><input id="bt-run" value="${escapeHtml(book.run_id)}" placeholder="留空创建新回测"><p class="form-hint">也可以从下方历史记录中选择。</p></div>
      </details>
    </form>
    <div class="form-actions"><button class="button button--secondary" type="button" id="check-backtest">预估分析次数</button><button class="button button--primary" type="button" id="start-backtest">启动回测</button></div>
    <p class="notice" role="status" aria-live="polite" id="backtest-result">先预估分析次数，再决定是否启动。回测不排队：运行名额已满或有任务排队时不会启动。</p></section>
    <section class="panel"><h2>历史回测</h2><div id="sweep-list"></div></section>`;
  bindTier("quick");
  bindTier("deep");
  showModelEffort();
  loadSelectedTierModels();
  fillChoices("#bt-asset", [{value: "stock", zh: "股票"}, {value: "crypto", zh: "加密资产"}, {value: "commodity", zh: "贵金属/商品期货"}], book.asset_type, (value) => {
    book.tickers = document.querySelector("#bt-tickers").value;
    book.start = document.querySelector("#bt-start").value;
    book.end = document.querySelector("#bt-end").value;
    book.every = document.querySelector("#bt-every").value;
    book.run_id = document.querySelector("#bt-run").value;
    book.portfolio = document.querySelector("#bt-portfolio").value;
    syncBacktestModels();
    book.asset_type = value;
    if (value === "crypto" || value === "commodity") book.analysts = book.analysts.filter((item) => item !== "fundamentals");
    renderBacktest();
  });
  fillChoices("#bt-analysts", analysts, book.analysts, (value, pressed) => {
    const set = new Set(book.analysts);
    pressed ? set.add(value) : set.delete(value);
    book.analysts = [...set];
  }, true);
  document.querySelector("#start-backtest").addEventListener("click", async () => {
    try {
      const payload = backtestPayload();
      const result = await api("/api/backtests", { method: "POST", body: JSON.stringify(payload) });
      rememberNotice("回测已启动，可在这里跟踪进度。");
      showPage("runs");
    } catch (error) {
      // W44: a refused backtest shows the console's own reason (slots full or a
      // queue waiting; backtests never queue), styled as an error, not a status.
      const out = document.querySelector("#backtest-result");
      out.className = "error-banner";
      out.setAttribute("role", "alert");
      out.textContent = error.message;
      out.scrollIntoView?.({block: "nearest", behavior: "smooth"});   // W44: in view, not below the fold
    }
  });
  bindPortfolioFile("#bt-portfolio-file", "#bt-portfolio");
  document.querySelector("#check-backtest").addEventListener("click", async () => {
    try {
      const payload = backtestPayload();
      const result = await api("/api/backtests/validate", { method: "POST", body: JSON.stringify(payload) });
      const empty = result.cells ? "尚未启动；确认后点击「启动回测」。" : "所选范围没有可分析的日期，请调整日期。";
      const out = document.querySelector("#backtest-result");
      out.className = "notice";
      out.setAttribute("role", "status");
      out.textContent = `共 ${result.cells} 次分析。${empty}`;
    } catch (error) {
      document.querySelector("#backtest-result").textContent = error.message;
    }
  });
  loadSweeps();
}

function sweepSelectionText(row) {
  return [(row && (row.summary || row.error)) || "这次回测还没有可评分的记录。", "已选择这次回测，点击「启动回测」可继续。"].join("\n");
}

async function loadSweeps() {
  const host = document.querySelector("#sweep-list");
  if (!host) return;
  try {
    const data = await api("/api/backtests");
    const rows = data.sweeps || [];
    if (!rows.length) {
      host.innerHTML = `<div class="empty-state"><div><h2>还没有回测记录</h2><p>设置标的与日期后，启动第一次回测。</p></div></div>`;
      return;
    }
    host.innerHTML = `<div class="table-wrap"><table class="data-table"><thead><tr><th>回测编号</th><th>记录状态</th></tr></thead><tbody>
      ${rows.map((row) => `<tr><td><button class="button button--small button--secondary" type="button" data-sweep="${escapeHtml(row.id)}">${escapeHtml(row.id)}</button></td><td>${row.has_log ? "有日志" : "还没有记忆日志"}</td></tr>`).join("")}
    </tbody></table></div><pre class="log-view" id="sweep-summary">选择一条记录查看摘要，或继续这次回测。</pre>`;
    host.querySelectorAll("[data-sweep]").forEach((button) => button.addEventListener("click", () => {
      const row = rows.find((item) => item.id === button.dataset.sweep);
      const input = document.querySelector("#bt-run");
      if (input) input.value = row.id;
      ensureBacktest().run_id = row.id;
      const summary = document.querySelector("#sweep-summary");
      if (summary) summary.textContent = sweepSelectionText(row);
    }));
  } catch (error) {
    host.innerHTML = `<p class="error-banner">${escapeHtml(error.message)}</p>`;
  }
}


function keyStatusText(keys) {
  const rows = keys || [];
  const configured = rows.filter((item) => item.configured).length;
  if (!rows.length) return "暂无 API Key 配置项。";
  if (!configured) return "尚未配置 API Key。使用订阅账户时无需填写。";
  return `已有 ${configured} 个供应商配置了 API Key，尚未验证服务可用性。`;
}

function accountFailureText(message) {
  const text = String(message || "").trim();
  if (!text) return "操作未完成。请检查网络或本机登录材料后重试。";
  if (/重试|检查|稍后/.test(text)) return text;
  return `${text}${/[。.!！?？]$/.test(text) ? "" : "。"}请检查网络或本机登录材料后重试。`;
}

function initialKeyProvider(keys) {
  return (keys || []).find((item) => !item.configured) || (keys || [])[0] || null;
}


function loginSentenceList() {
  const rows = state.catalog.login_rows || [];
  return `<ul class="command-map">${rows.map((item) => {
    const cli = item.slot ? slotSentence(item.cli) : item.cli;
    return `<li><span>${escapeHtml(item.zh)}</span><code>${escapeHtml(cli)}</code></li>`;
  }).join("")}</ul>`;
}


function planErrorNote() {
  const note = state.catalog?.plan_note || "";
  const errors = state.catalog?.plan_errors || {};
  const blocks = ["login_missing", "logout_missing", "login_nope", "logout_nope", "auth_status_nope"].map((key) => {
    const sentence = errors[key];
    return sentence ? `<pre class="log-view" data-plan-error="${escapeHtml(key)}">${escapeHtml(sentence)}</pre>` : "";
  }).join("");
  if (!note && !blocks) return "";
  return `<section class="panel" id="plan-errors"><h2>订阅参数 <span>plan</span></h2><p class="notice">${escapeHtml(note)}</p>${blocks}</section>`;
}

function authPlanFilters() {
  const current = state.authPlan || "";
  const options = [
    { plan: "", zh: "全部" },
    { plan: "chatgpt", zh: "ChatGPT" },
    { plan: "grok", zh: "Grok" },
  ];
  return `<div class="field field--wide"><label id="auth-plan-label">按订阅查看账户</label><div class="choice-row" id="auth-plan">${options.map((item) => `<button type="button" data-auth-plan="${escapeHtml(item.plan)}" aria-pressed="${item.plan === current}">${escapeHtml(item.zh)}</button>`).join("")}</div></div>`;
}

function pasteLoginNote(login) {
  const notes = state.catalog.login_paste_notes || {};
  const prompt = state.catalog.login_paste || "";
  if (login && login.status === "waiting" && login.provider === "grok") return notes.grok || "";
  if (login && login.status === "waiting" && login.provider === "chatgpt") return `${notes.chatgpt || ""} ${prompt}`;
  return `${notes.idle || ""} ${prompt}`;
}

function authProviderMeta(provider) {
  return (state.catalog.providers || []).find((item) => item.key === provider) || {
    key: provider,
    zh: provider,
    cli: provider,
  };
}

function authProviderName(provider) {
  return authProviderMeta(provider).zh || provider;
}

function setAuthCardFeedback(provider, message, error = false) {
  state.authCardMessages ||= {};
  const displayMessage = error ? accountFailureText(message) : message;
  state.authCardMessages[provider] = {message: displayMessage, error};
  const feedback = document.querySelector(`[data-auth-feedback="${provider}"]`);
  if (!feedback) return;
  feedback.textContent = displayMessage;
  feedback.classList.toggle("account-provider-feedback--error", error);
}

// One wording for account state on every page: the topbar, the account cards and the wizard.
function accountStateLabel(item) {
  if (!item) return "未登录";
  if (item.read_error) return "状态读取失败";
  return item.signed_in ? "已登录" : "未登录";
}

function identityText(accounts) {
  const signed = (accounts || []).filter((item) => item.signed_in && !item.read_error);
  if (!signed.length) return {name: "未登录", title: "还没有登录任何订阅"};
  return {
    name: signed.map((item) => authProviderName(item.provider)).join(" · "),
    title: signed.map((item) => `${authProviderName(item.provider)}：${item.account}`).join("\n"),
  };
}

function setIdentity(accounts) {
  const host = document.querySelector("#identity-name");
  if (!host) return;
  const text = identityText(accounts);
  host.textContent = text.name;
  host.title = text.title;
}

function accountProviderCard(item) {
  const meta = authProviderMeta(item.provider);
  const signed = Boolean(item.signed_in);
  const readError = Boolean(item.read_error);
  const feedback = state.authCardMessages?.[item.provider] || {
    message: readError ? "无法读取本机登录状态，请刷新状态或重新登录。" : signed ? "已有本机登录记录。" : "",
    error: readError,
  };
  return `<article class="provider-card account-provider-card">
    ${providerMark(meta)}
    <div class="account-provider-body">
      <div class="account-provider-heading">
        <div class="account-provider-title">
          <strong>${escapeHtml(meta.zh || item.zh || item.provider)}</strong>
        </div>
        <span class="status-badge ${readError ? "status-badge--error" : signed ? "status-badge--success" : "status-badge--muted"}">${accountStateLabel(item)}</span>
      </div>
      <p class="account-provider-account"><strong>${signed && !readError ? "当前账户" : "账户状态"}</strong> ${escapeHtml(signed && !readError ? item.account : accountStateLabel(item))}</p>
      <div class="inline-actions account-provider-actions">
        <button class="button button--small button--primary" type="button" data-login="${escapeHtml(item.provider)}">登录</button>
        <button class="button button--small button--secondary" type="button" data-logout="${escapeHtml(item.provider)}" ${signed ? "" : "disabled"}>退出</button>
      </div>
      <p class="account-provider-feedback ${feedback.error ? "account-provider-feedback--error" : ""}" data-auth-feedback="${escapeHtml(item.provider)}" role="status" aria-live="polite">${escapeHtml(feedback.message)}</p>
      <details class="disclosure account-provider-details">
        <summary>查看登录详情</summary>
        <p class="account-provider-meta">${escapeHtml(meta.cli || item.cli || item.provider)}</p>
        ${item.plan_use ? `<p class="account-provider-meta">${escapeHtml(item.plan_use)}</p>` : ""}
        <p class="account-provider-meta">登录状态只读取本机材料，页面打开时不会联网校验。</p>
      </details>
    </div>
  </article>`;
}

async function renderAccounts() {
  if (!state.catalog) {
    pageEl.innerHTML = `<section class="panel page-loading" id="page-loading" role="status"><p>正在加载账户…</p></section>`;
    return;
  }
  const plan = state.authPlan || "";
  const query = plan ? `?plan=${encodeURIComponent(plan)}` : "";
  let auth;
  let config;
  let login;
  let identity;
  try {
    auth = await api(`/api/auth/status${query}`);
    config = await api("/api/config");
    login = await api("/api/auth/login");
    identity = plan ? await api("/api/auth/status") : auth;
  } catch (error) {
    if (state.page !== "accounts") return;
    pageEl.innerHTML = `
      ${heading("auth-status", "账户", "账户状态暂时无法读取。请检查服务是否运行后重试。")}
      <section class="panel" id="account-load-error">
        <h2>账户状态读取失败</h2>
        <p class="error-banner">${escapeHtml(accountFailureText(error.message))}</p>
        <button class="button button--secondary" type="button" id="retry-auth">重新加载</button>
      </section>`;
    document.querySelector("#retry-auth").addEventListener("click", renderAccounts);
    return;
  }
  const chatgptWaiting = login && login.status === "waiting" && login.provider === "chatgpt";
  if (state.page !== "accounts") return;
  state.auth = auth;
  const initialKey = initialKeyProvider(config.keys);
  const credentialRows = [...(config.data_env || []), ...(config.cloud_env || [])];
  const configuredCredentials = credentialRows.filter((item) => item.configured).length;
  const credentialSummary = credentialRows.length
    ? `可选凭据已配置 ${configuredCredentials} 项。默认数据来源无需在此填写。`
    : "数据来源凭据：暂无可配置项。";
  setIdentity(identity.accounts);
  pageEl.innerHTML = `
    ${heading("account", "账户", "管理订阅登录和 API Key。本页显示本机配置状态，密钥不会回显。",
      `<button class="button button--secondary" type="button" id="refresh-auth">刷新状态</button>`)}
    ${authPlanFilters()}
    <section class="subscription-section" aria-labelledby="subscription-heading">
      <div class="subsection-heading">
        <div>
          <h2 id="subscription-heading">订阅账户</h2>
          <p>使用订阅账户时，无需额外填写该服务的 API Key。</p>
        </div>
      </div>
      <div class="provider-grid account-provider-grid" id="auth-accounts">
        ${auth.accounts.map(accountProviderCard).join("")}
      </div>
    </section>
    <section class="panel account-keys" aria-labelledby="key-heading">
      <div class="subsection-heading">
        <div>
          <h2 id="key-heading">API Key</h2>
          <p id="key-status" class="notice">${keyStatusText(config.keys)}</p>
        </div>
      </div>
      <form class="form-grid" id="key-form">
        <div class="field"><label>选择供应商</label><div id="key-providers" aria-label="选择 API Key 供应商"></div><input id="key-provider" type="hidden" value="${escapeHtml(initialKey?.provider || "")}"></div>
        <div class="field"><label for="key-value" id="key-label">API Key（保存后不会回显）</label><input id="key-value" type="password" autocomplete="off" placeholder="只写入本机配置，不回显"></div>
        <div class="form-actions field--wide"><button class="button button--primary" type="submit">保存 API Key</button></div>
      </form>
      <details class="disclosure account-details" id="key-details">
        <summary>查看密钥配置项</summary>
        <p class="notice">页面不会展示密钥内容。以下仅用于确认供应商与保存项。</p>
        <ul class="command-map">${(config.keys || []).map((item) => `<li><span>${escapeHtml(authProviderName(item.provider))}</span><code>${escapeHtml(item.env_var || "")}</code><span>${item.configured ? "已配置" : "未配置"}</span></li>`).join("")}</ul>
      </details>
    </section>
    <section class="panel account-data-credentials" aria-labelledby="data-credentials-heading">
      <div class="subsection-heading">
        <div>
          <h2 id="data-credentials-heading">可选数据服务</h2>
          <p class="notice">${escapeHtml(credentialSummary)}</p>
        </div>
      </div>
      <details class="disclosure account-details" id="data-env-details">
        <summary>配置数据来源凭据</summary>
        <form class="form-grid" id="data-env-form">
          <p class="notice field--wide">这些可选凭据只写入本机配置，内容不会回显。</p>
          ${envFields(state.catalog?.data_env, config.data_env)}
          ${envFields(state.catalog?.cloud_env, config.cloud_env)}
          <div class="form-actions field--wide"><button class="button button--primary" type="submit">保存数据凭据</button></div>
          <p class="notice field--wide" id="data-env-feedback" aria-live="polite">尚未保存。密钥和联系标识都不会回显。</p>
        </form>
      </details>
    </section>
    ${dataSourceSection()}
    ${updatesSection()}
    <details class="disclosure account-details" id="login-help"${chatgptWaiting ? " open" : ""}>
      <summary>登录辅助</summary>
      <p class="notice">如果登录窗口没有自动完成，可在这里查看提示并提交地址。</p>
      <pre class="log-view" id="login-log">${escapeHtml((login.messages || []).join("\n") || "登录提示会显示在这里。")}</pre>
      <form class="form-grid" id="paste-form">
        <div class="field field--wide"><label for="paste-url" id="paste-label">${escapeHtml(pasteLoginNote(login))}</label><input id="paste-url" placeholder="${escapeHtml(placeholder("login_paste"))}" ${chatgptWaiting ? "" : "disabled"}></div>
        <div class="form-actions field--wide"><button class="button button--secondary" type="submit" ${chatgptWaiting ? "" : "disabled"}>提交粘贴地址</button></div>
      </form>
    </details>
    <details class="disclosure account-details" id="account-cli-details">
      <summary>查看诊断与命令行说明</summary>
      <p class="notice" id="auth-invocation">当前筛选：${escapeHtml(auth.cli || "auth-status")}。状态只读取本机登录材料，页面打开时不会联网校验。</p>
      ${commandMap([{ cli: "login", zh: "登录订阅" }, { cli: "logout", zh: "退出登录" }, { cli: "auth-status", zh: "登录状态" }, ...(state.catalog.data_env || []).map((item) => ({cli: item.cli, zh: item.zh})), ...(state.catalog.cloud_env || []).map((item) => ({cli: item.cli, zh: item.zh}))])}
      ${flagTable(state.catalog?.subscription_commands || [])}
      ${loginSentenceList()}
      ${planErrorNote()}
    </details>`;
  document.querySelector("#paste-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/auth/login/paste", { method: "POST", body: JSON.stringify({ url: document.querySelector("#paste-url").value }) });
      document.querySelector("#paste-url").value = "";
    } catch (error) {
      pageEl.insertAdjacentHTML("afterbegin", `<p class="error-banner">${escapeHtml(error.message)}</p>`);
    }
  });
  document.querySelector("#refresh-auth").addEventListener("click", renderAccounts);
  document.querySelectorAll("[data-auth-plan]").forEach((button) => button.addEventListener("click", () => {
    state.authPlan = button.dataset.authPlan || "";
    renderAccounts();
  }));
  document.querySelectorAll("[data-login]").forEach((button) => button.addEventListener("click", () => beginLogin(button.dataset.login)));
  document.querySelectorAll("[data-logout]").forEach((button) => button.addEventListener("click", () => confirmAction("退出这个订阅？", "本机凭据会被清除。是否撤销服务端授权，以接口返回为准。", () => logout(button.dataset.logout), button)));
  const keyHost = document.querySelector("#key-providers");
  const known = new Map((state.catalog.providers || []).map((item) => [item.key, item]));
  const selectedKey = initialKey?.provider || "";
  const keyItems = (config.keys || []).map((item) => {
    const meta = known.get(item.provider) || {key: item.provider, zh: item.provider};
    return {value: item.provider, zh: `${meta.zh || item.provider} · ${item.configured ? "已配置" : "未配置"}`};
  });
  if (keyItems.length) {
    mountSelect(keyHost, keyItems, selectedKey, (value) => {
      document.querySelector("#key-provider").value = value;
      const label = document.querySelector("#key-label");
      const meta = known.get(value) || {zh: value};
      if (label) label.textContent = `填写 ${meta.zh || value} 的 API Key。保存后不会回显。`;
    });
  } else {
    keyHost.textContent = "暂无可配置的 API Key。";
  }
  document.querySelector("#key-form").addEventListener("submit", saveKey);
  document.querySelector("#data-env-form").addEventListener("submit", saveDataEnv);
  bindDataSources();
  bindUpdates();
}

function envFields(items, statusRows) {
  return (items || []).map((item) => {
    const status = (statusRows || []).find((row) => row.env === item.env);
    const configured = status && status.configured ? "已配置。" : "未配置。";
    const inputType = item.masked ? "password" : "text";
    const placeholder = status && status.configured ? "已配置。留空不修改，页面不回显。" : "写入本机 .env，页面不回显。";
    return `<div class="field field--wide"><label for="env-${escapeHtml(item.env)}">${escapeHtml(item.zh)} ${escapeHtml(item.cli)}。${escapeHtml(item.note)} ${configured}</label><input id="env-${escapeHtml(item.env)}" type="${inputType}" autocomplete="off" placeholder="${placeholder}"></div>`;
  }).join("");
}

async function saveDataEnv(event) {
  event.preventDefault();
  const feedback = document.querySelector("#data-env-feedback");
  const button = event.currentTarget.querySelector("[type=submit]");
  const fields = [...document.querySelectorAll("#data-env-form input")].filter((input) => input.value.trim());
  if (!fields.length) {
    if (feedback) feedback.textContent = "没有填写新值。留空不会清除已有配置。";
    return;
  }
  button.disabled = true;
  button.textContent = "正在保存";
  const saved = [];
  try {
    for (const input of fields) {
      const env = input.id.slice(4);
      const result = await api("/api/data-env", { method: "POST", body: JSON.stringify({ env, value: input.value }) });
      input.value = "";
      saved.push(result.env_var);
    }
    await renderAccounts();
    const detail = saved.length ? `<details class="disclosure account-details"><summary>查看保存详情</summary><p class="notice">${escapeHtml(saved.join("、"))}</p></details>` : "";
    pageEl.insertAdjacentHTML("afterbegin", `<p class="notice">已保存 ${saved.length} 项数据来源凭据。内容不会回显。</p>${detail}`);
  } catch (error) {
    const savedNote = saved.length ? `已保存 ${saved.join("、")}。` : "";
    await renderAccounts();
    const detail = savedNote ? `<details class="disclosure account-details"><summary>查看已保存项目</summary><p class="notice">${escapeHtml(savedNote)}</p></details>` : "";
    pageEl.insertAdjacentHTML("afterbegin", `<p class="error-banner">${escapeHtml(savedNote + accountFailureText(error.message))}</p>${detail}`);
  }
}

function updateLoginInputs(login) {
  const enabled = login?.provider === "chatgpt" && login.status === "waiting";
  const input = document.querySelector("#paste-url");
  const submit = document.querySelector("#paste-form [type=submit]");
  if (input) input.disabled = !enabled;
  if (submit) submit.disabled = !enabled;
  const label = document.querySelector("#paste-label");
  if (label) label.textContent = enabled ? "如果浏览器未自动完成登录，请粘贴授权完成后的回调地址。" : "ChatGPT 登录等待授权时，可在这里提交回调地址。";
}

async function beginLogin(provider) {
  const label = authProviderName(provider);
  const button = document.querySelector(`[data-login="${provider}"]`);
  const originalLabel = button?.textContent || "登录";
  if (button) {
    button.disabled = true;
    button.textContent = "正在启动";
  }
  setAuthCardFeedback(provider, `正在启动 ${label} 登录…`);
  const log = document.querySelector("#login-log");
  if (log) log.textContent = `正在启动 ${label} 登录…`;
  let data;
  try {
    data = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ provider }) });
  } catch (error) {
    if (button) {
      button.disabled = false;
      button.textContent = originalLabel;
    }
    setAuthCardFeedback(provider, error.message, true);
    if (log) log.textContent = error.message;
    return;
  }
  if (button) button.textContent = "等待授权";
  setAuthCardFeedback(provider, `${label} 登录已启动，请完成浏览器授权。`);
  const help = document.querySelector("#login-help");
  if (help) help.open = true;
  updateLoginInputs({...data, provider});
  if (log) log.textContent = "正在等待登录提示…";
  const timer = setInterval(async () => {
    let current;
    try {
      current = await api("/api/auth/login");
    } catch (error) {
      clearInterval(timer);
      setAuthCardFeedback(provider, error.message, true);
      if (button) {
        button.disabled = false;
        button.textContent = originalLabel;
      }
      return;
    }
    const currentLog = document.querySelector("#login-log");
    if (currentLog) {
      currentLog.textContent = [current.zh, current.cli ? `对应命令行：${current.cli}` : "", current.url, ...(current.messages || []), current.error].filter(Boolean).join("\n");
    }
    if (current.status === "waiting") {
      updateLoginInputs(current);
      setAuthCardFeedback(provider, `${label} 正在等待浏览器授权…`);
      return;
    }
    if (current.status !== "waiting") {
      clearInterval(timer);
      if (state.page !== "accounts") return;
      const cli = current.cli ? `对应命令行：${current.cli}` : "";
      const sentence = [current.zh, cli].filter(Boolean).join(" ");
      await renderAccounts();
      setAuthCardFeedback(provider, current.error || sentence || `${label} 登录流程已结束。`, Boolean(current.error));
      if (current.error) {
        pageEl.insertAdjacentHTML("afterbegin", `<p class="error-banner">${escapeHtml(accountFailureText(current.error))}</p>`);
      } else if (sentence) {
        const detail = current.cli ? `<details class="disclosure account-details"><summary>查看登录详情</summary><p class="notice">${escapeHtml(sentence)}</p></details>` : "";
        pageEl.insertAdjacentHTML("afterbegin", `<p class="notice">${escapeHtml(current.zh || `${label} 登录已完成。`)}</p>${detail}`);
      }
    }
  }, 1000);
  const currentLog = document.querySelector("#login-log");
  if (data.url && currentLog) currentLog.textContent = data.url;
}

async function logout(provider) {
  const label = authProviderName(provider);
  const button = document.querySelector(`[data-logout="${provider}"]`);
  if (button) {
    button.disabled = true;
    button.textContent = "正在退出";
  }
  setAuthCardFeedback(provider, `正在退出 ${label}…`);
  let result;
  try {
    result = await api("/api/auth/logout", { method: "POST", body: JSON.stringify({ provider }) });
  } catch (error) {
    if (button) {
      button.disabled = false;
      button.textContent = "退出";
    }
    setAuthCardFeedback(provider, error.message, true);
    pageEl.insertAdjacentHTML("afterbegin", `<p class="error-banner">${escapeHtml(accountFailureText(error.message))}</p>`);
    return;
  }
  if (state.page !== "accounts") return;
  await renderAccounts();
  setAuthCardFeedback(provider, result.zh || `${label} 已退出。`);
  const detail = result.cli ? `<details class="disclosure account-details"><summary>查看退出详情</summary><p class="notice">${escapeHtml(result.zh)} 对应命令行：${escapeHtml(result.cli)}</p></details>` : "";
  pageEl.insertAdjacentHTML("afterbegin", `<p class="notice">${escapeHtml(result.zh || `${label} 已退出。`)}</p>${detail}`);
}

async function saveKey(event) {
  event.preventDefault();
  const provider = document.querySelector("#key-provider").value.trim();
  const key = document.querySelector("#key-value").value;
  try {
    const result = await api("/api/keys", { method: "POST", body: JSON.stringify({ provider, key }) });
    const verb = result.saved ? "已保存" : "已跳过，没有写入";
    const sentence = `${verb} ${authProviderName(provider)} 的 API Key。密钥不会回显。`;
    const detail = result.cli ? `<details class="disclosure account-details"><summary>查看保存详情</summary><p class="notice">${escapeHtml(result.cli)}</p></details>` : "";
    await renderAccounts();
    pageEl.insertAdjacentHTML("afterbegin", `<p class="notice">${escapeHtml(sentence)}</p>${detail}`);
  } catch (error) {
    pageEl.insertAdjacentHTML("afterbegin", `<p class="error-banner">${escapeHtml(accountFailureText(error.message))}</p>`);
  }
}

function renderModels() {
  const providers = state.catalog.providers || [];
  pageEl.innerHTML = `
    ${heading("login", "模型", "两层可以分别选择已登录的订阅。快模型不是数据源，Grok 不会代替 yfinance 或 SEC。Grok 没有独立思考强度。")}
    <p class="notice">这里列出命令行供应商表里的全部供应商，不只订阅。下面的思考强度写入同一次分析，和启动分析「模型选择」一步是同一组字段。</p>
    <div class="provider-grid">${providers.map((item) =>
      `<button class="provider-card" type="button" data-provider="${escapeHtml(item.key)}">${providerMark(item)}${providerText(item)}</button>`).join("")}</div>
    <div id="model-effort"></div>
    <div id="model-list"></div>
    ${modelAdvancedSection()}`;
  showModelEffort();
  bindModelAdvanced();
  document.querySelectorAll("[data-provider]").forEach((button) => button.addEventListener("click", () => loadModels(button.dataset.provider)));
}

function showModelEffort(extra) {
  const host = document.querySelector("#model-effort");
  if (!host || !state.catalog) return;
  const families = effortFamilies(extra);
  if (!families.length) {
    host.innerHTML = `<p class="notice">所选模型使用默认思考方式，无需额外设置。</p>`;
    return;
  }
  host.innerHTML = families.map(([key, title]) => {
    const box = state.catalog.step_eight[key];
    return `<div class="field field--wide"><span>${escapeHtml(title + (effortSkipped(families.find((item) => item[0] === key)?.[2]) ? "（由本机配置固定）" : ""))}</span><div class="choice-row" id="model-effort-${key}"></div></div>`;
  }).join("");
  for (const [key, , field] of families) {
    bindEffort(`#model-effort-${key}`, key, field);
  }
}


async function loadModels(provider) {
  const host = document.querySelector("#model-list");
  host.textContent = provider === "azure"
    ? `Azure 没有模型列表。命令行提问：${pickerPrompt("azure_quick")} 和 ${pickerPrompt("azure_deep")}。空值校验：${pickerPrompt("azure_required")}`
    : `正在读取快模型和深模型。列表失败时仍可手填，对应 ${pickerPrompt("custom_choice")}。`;
  try {
    const [quick, deep] = await Promise.all(["quick", "deep"].map((mode) => api(`/api/models?provider=${encodeURIComponent(provider)}&mode=${mode}`)));
    host.innerHTML = [quick, deep].map((data) => modelTable(provider, data)).join("") + customModelRow(provider);
    if (provider === "ollama") {
      const url = state.form.backend_url ? `?url=${encodeURIComponent(state.form.backend_url)}` : "";
      const hint = await api(`/api/ollama${url}`);
      host.insertAdjacentHTML("afterbegin", `<pre class="log-view">${escapeHtml((hint.cli || []).join("\n"))}</pre>`);
    }
    bindModelUses(host, provider);
    showModelEffort(provider);
  } catch (error) {
    host.innerHTML = `<p class="error-banner">${escapeHtml(error.message)}</p>` + customModelRow(provider);
    bindModelUses(host, provider);
  }
}

function modelTable(provider, data) {
  const warning = data.cli ? `<p class="notice">${escapeHtml(data.detail || "模型列表没有读到。")} 对应命令行：${escapeHtml(data.cli)}</p>` : "";
  const rows = (data.models || []).map((item) => `<tr><td><strong>${escapeHtml(item.zh)}</strong></td><td><code>${escapeHtml(item.id)}</code></td><td><button class="button button--small button--secondary" type="button" data-use="${escapeHtml(data.mode)}" data-id="${escapeHtml(item.id)}">用于这一层</button></td></tr>`).join("");
  const empty = data.detail || `没有可选模型。可在下方手填 ${pickerPrompt("custom_choice")}。`;
  return `<section class="panel">${warning}<h2>${escapeHtml(data.title || data.mode)}</h2>${rows ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>模型</th><th>ID</th><th>用于</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="notice">${escapeHtml(empty)}</p>`}</section>`;
}

function customModelRow(provider) {
  if (provider === "azure") {
    return `<div class="field field--wide"><label for="custom-model-id">Azure 部署名。快模型命令行提问：${escapeHtml(pickerPrompt("azure_quick"))}。深模型：${escapeHtml(pickerPrompt("azure_deep"))}。空值校验：${escapeHtml(pickerPrompt("azure_required"))}</label><input id="custom-model-id" placeholder="${escapeHtml(pickerPrompt("azure_required"))}" aria-label="${escapeHtml(pickerPrompt("azure_quick"))}"><div class="choice-row"><button class="button button--secondary" type="button" data-use="quick" data-custom="true">用作快模型</button><button class="button button--secondary" type="button" data-use="deep" data-custom="true">用作深模型</button></div></div>`;
  }
  if (provider === "openrouter") {
    return `<div class="field field--wide"><label for="custom-model-id">OpenRouter 模型 ID。命令行提问：${escapeHtml(pickerPrompt("openrouter_id"))}。空值校验：${escapeHtml(pickerPrompt("model_id_required"))}</label><input id="custom-model-id" placeholder="${escapeHtml(placeholder("openrouter_id"))}" aria-label="${escapeHtml(pickerPrompt("openrouter_id"))}"><div class="choice-row"><button class="button button--secondary" type="button" data-use="quick" data-custom="true">用作快模型</button><button class="button button--secondary" type="button" data-use="deep" data-custom="true">用作深模型</button></div></div>`;
  }
  return `<div class="field field--wide"><label for="custom-model-id">${escapeHtml(pickerPrompt("custom_choice"))}。命令行提问：${escapeHtml(pickerPrompt("model_id"))}。空值校验：${escapeHtml(pickerPrompt("model_id_required"))}。列表失败或选择自定义时手填。</label><input id="custom-model-id" placeholder="${escapeHtml(pickerPrompt("model_id"))}"><div class="choice-row"><button class="button button--secondary" type="button" data-use="quick" data-custom="true">用作快模型</button><button class="button button--secondary" type="button" data-use="deep" data-custom="true">用作深模型</button></div></div>`;
}

function modelIdRequired(provider) {
  return provider === "azure" ? pickerPrompt("azure_required") : pickerPrompt("model_id_required");
}

function bindModelUses(host, provider) {
  host.querySelectorAll("[data-use]").forEach((button) => button.addEventListener("click", () => {
    const id = button.dataset.custom ? document.querySelector("#custom-model-id")?.value.trim() : button.dataset.id;
    if (!id) {
      let note = host.querySelector("#model-id-note");
      if (!note) {
        note = document.createElement("p");
        note.id = "model-id-note";
        note.className = "error-banner";
        host.append(note);
      }
      note.textContent = `模型 ID 为空。对应命令行：${modelIdRequired(provider)} 页面没有这一问的取消。`;
      return;
    }
    host.querySelector("#model-id-note")?.remove();
    const tier = button.dataset.use;
    state.form[`${tier}_think_provider`] = provider;
    state.form[`${tier}_think_llm`] = id;
    button.textContent = "已选";
  }));
}

function applyTierModelPrompt(tier, data) {
  const input = document.querySelector(`#${tier}-model`);
  if (!input || !data) return;
  if (data.provider === "azure") {
    input.setAttribute("aria-label", data.title || pickerPrompt(tier === "quick" ? "azure_quick" : "azure_deep"));
    input.placeholder = data.hint || pickerPrompt("azure_required");
    return;
  }
  if (data.custom_prompt) {
    input.setAttribute("aria-label", data.custom_prompt);
    input.placeholder = data.hint || pickerPrompt("model_id_required");
    return;
  }
  input.setAttribute("aria-label", tier === "quick" ? "快模型 ID，可手填自定义 ID" : "深模型 ID，可手填自定义 ID");
  input.placeholder = "";
}

function modelSelectState(tier, models) {
  const current = state.form[`${tier}_think_llm`] || "";
  const matched = (models || []).some((item) => String(item.id) === String(current));
  return {
    current: matched ? current : "",
    options: {
      allowEmpty: !matched,
      emptyLabel: current ? `当前模型：${current}` : "请选择模型",
    },
  };
}

function renderModelChoices(list, data, tier) {
  applyTierModelPrompt(tier, data);
  const warning = data.detail || "";
  if (!data.models.length) {
    const empty = data.provider === "azure"
      ? ["请输入 Azure 部署名称。", data.detail].filter(Boolean).join("\n")
      : [warning, "没有可选模型，请展开下方设置手动填写模型 ID。"].filter(Boolean).join("\n");
    list.textContent = empty;
    return;
  }
  const picked = modelSelectState(tier, data.models);
  const items = data.models.map((item) => ({value: item.id, zh: item.zh || item.id, cli: item.id}));
  const choose = (value) => {
    if (tierModelSkipped(tier)) {
      lockSkippedModelInputs();
      renderModelChoices(list, data, tier);
      return;
    }
    state.form[`${tier}_think_llm`] = value;
    const input = document.querySelector(`#${tier}-model`);
    if (input) input.value = value;
    };
  mountSelect(list, items, picked.current, choose, picked.options);
  if (tierModelSkipped(tier)) list.querySelector("button").disabled = true;
}

function configKeyRows() {
  return state.catalog?.config_keys || [];
}

function queryText() {
  const value = state.form.global_news_queries;
  return Array.isArray(value) ? value.join("\n") : (value || "");
}

function chatgptRefusalNote() {
  const called = calledTierProviders();
  if (!called.some((item) => item === "chatgpt")) return "当前快模型和深模型都不是 ChatGPT 订阅，这两项会照常发送。";
  return `当前有一层使用 ChatGPT 订阅：订阅不接受 temperature 和 max_tokens，填写了也不会发送。日志会写 ${state.catalog.confirmation_sentences.refuse_temperature} 和 ${state.catalog.confirmation_sentences.refuse_max_tokens}。`;
}

// W59: LangChain drops temperature (unless 1) for gpt-5 models other than the
// chat ones, unless reasoning effort is "none". Say so where temperature is set.
function gpt5TemperatureModels() {
  if (String(state.form.openai_reasoning_effort || "").toLowerCase() === "none") return [];
  const models = [state.form.quick_think_llm, state.form.deep_think_llm].map((item) => String(item || "").trim());
  return [...new Set(models.filter((item) => item.toLowerCase().startsWith("gpt-5") && !item.toLowerCase().includes("chat")))];
}

function gpt5TemperatureNote() {
  const models = gpt5TemperatureModels();
  if (!models.length) return "";
  return `所选模型 ${models.join("、")} 属于 gpt-5 系列推理模型（非 chat 版）：temperature 只接受 1，填写其他值会被直接去掉，不会生效。`;
}

// WEBUI-005: one labelled input for a setting the CLI reads from the environment or the run config.
function settingInput(id, field, options = {}) {
  const row = envRow(field) || configKeyRows().find((item) => item.field === field) || {};
  const locked = options.locked ?? envFieldLocked(field);
  const value = options.value ?? envFieldValue(field);
  const note = locked ? envFieldNote(field, "") : (options.note ?? row.note ?? "");
  const label = options.label || row.zh || field;
  return `<div class="field${options.wide ? " field--wide" : ""}"><label for="${id}">${escapeHtml(label)} <code class="field-code">${escapeHtml(row.cli || field)}</code></label><input id="${id}" value="${escapeHtml(value ?? "")}" placeholder="${escapeHtml(options.placeholder || row.placeholder || "留空沿用默认")}" ${locked ? "readonly" : ""}>${note ? `<p class="form-hint">${escapeHtml(note)}</p>` : ""}</div>`;
}

function inputValue(id) {
  const input = document.querySelector(`#${id}`);
  if (!input || input.readOnly) return undefined;
  return input.value.trim() || null;
}

// Only the keys the form actually owns are sent; a field the environment locks is left out.
function cleanConfigPayload(payload) {
  const clean = {};
  for (const [key, value] of Object.entries(payload)) {
    if (value === undefined || envFieldLocked(key)) continue;
    clean[key] = value;
  }
  return clean;
}

async function saveConfigPart(payload, feedback, button, doneText) {
  const clean = cleanConfigPayload(payload);
  if (button) { button.disabled = true; button.dataset.label = button.textContent; button.textContent = "正在保存"; }
  if (feedback) { feedback.className = "notice field--wide"; feedback.textContent = "正在写入本机控制台配置。"; }
  try {
    await api("/api/config", { method: "POST", body: JSON.stringify(clean) });
    Object.assign(state.form, clean);
    if (Array.isArray(clean.global_news_queries) && !clean.global_news_queries.length) state.form.global_news_queries = null;
    if (feedback) feedback.textContent = doneText;
    return true;
  } catch (error) {
    if (feedback) { feedback.className = "error-banner field--wide"; feedback.textContent = error.message; }
    return false;
  } finally {
    if (button) { button.disabled = false; button.textContent = button.dataset.label || "保存"; }
  }
}

// WEBUI-005: the old 运行参数 page is split. Model knobs live here, data sources live in 账户.
function modelAdvancedSection() {
  return `<details class="disclosure" id="model-advanced"><summary>高级设置</summary>
    <p class="form-hint">保存在本机控制台配置里，下一次分析生效。留空沿用默认。</p>
    <form id="model-advanced-form" class="form-grid">
      ${settingInput("set-temperature", "temperature", {note: ""})}
      ${settingInput("set-tokens", "max_tokens", {note: ""})}
      <p class="notice field--wide" id="model-refusal-note">${escapeHtml(chatgptRefusalNote())}</p>${gpt5TemperatureNote() ? `<p class="notice field--wide" id="gpt5-temperature-note">${escapeHtml(gpt5TemperatureNote())}</p>` : ""}
      ${settingInput("set-tools", "max_tool_rounds", {note: "每个分析师最多调用工具的轮数。"})}
      ${settingInput("set-risk-rounds", "max_risk_discuss_rounds", {value: riskRoundValue(), locked: riskRoundLocked(), note: riskRoundNote(), placeholder: "留空"})}
      ${settingInput("set-quick-url", "quick_think_backend_url", {value: tierUrlValue("quick"), locked: tierUrlLocked("quick"), note: tierUrlNote("quick"), placeholder: "留空沿用供应商默认", wide: true})}
      ${settingInput("set-deep-url", "deep_think_backend_url", {value: tierUrlValue("deep"), locked: tierUrlLocked("deep"), note: tierUrlNote("deep"), placeholder: "留空沿用供应商默认", wide: true})}
      <div class="form-actions field--wide"><button class="button button--primary" type="submit">保存高级设置</button></div>
      <p class="notice field--wide" id="model-advanced-feedback" aria-live="polite"></p>
    </form></details>`;
}

function readModelAdvanced() {
  return {
    temperature: inputValue("set-temperature"),
    max_tokens: inputValue("set-tokens"),
    max_tool_rounds: inputValue("set-tools"),
    max_risk_discuss_rounds: inputValue("set-risk-rounds"),
    quick_think_backend_url: inputValue("set-quick-url"),
    deep_think_backend_url: inputValue("set-deep-url"),
  };
}

function bindModelAdvanced() {
  const form = document.querySelector("#model-advanced-form");
  if (!form) return;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    saveConfigPart(readModelAdvanced(), document.querySelector("#model-advanced-feedback"), form.querySelector("[type=submit]"),
      "已保存。下一次启动分析会带上这些值。");
  });
}

const DATA_KEY_FIELDS = ["news_article_limit", "global_news_article_limit", "global_news_lookback_days"];

function dataSourceSection() {
  const keyRows = configKeyRows().filter((item) => DATA_KEY_FIELDS.includes(item.field));
  return `<section class="panel account-data-sources" aria-labelledby="data-sources-heading">
      <div class="subsection-heading"><div><h2 id="data-sources-heading">数据来源</h2>
      <p class="notice">行情、新闻和基本面从哪里取。留空沿用默认来源，保存在本机控制台配置里。</p></div></div>
      <details class="disclosure account-details" id="data-sources-details"><summary>调整数据来源</summary>
      <form id="data-sources-form" class="form-grid">
        ${vendorFields()}
        <details class="disclosure field--wide" id="tool-vendor-details"><summary>按工具单独指定（优先于上面的类别）</summary>${toolFields()}</details>
        ${keyRows.map((item) => settingInput(`set-key-${item.field}`, item.field, {value: state.form[item.field] || "", locked: false})).join("")}
        <div class="field field--wide"><label for="set-key-global_news_queries">宏观新闻查询 <code class="field-code">global_news_queries</code></label><textarea id="set-key-global_news_queries" placeholder="一行一条。留空沿用内置查询。">${escapeHtml(queryText())}</textarea></div>
        ${settingInput("set-benchmark", "benchmark_ticker", {note: "回测对比用的基准。", wide: true})}
        <div class="form-actions field--wide"><button class="button button--primary" type="submit">保存数据来源</button></div>
        <p class="notice field--wide" id="data-sources-feedback" aria-live="polite"></p>
      </form></details>
    </section>`;
}

function readDataSources() {
  const payload = {
    data_vendors: readVendorFields(),
    tool_vendors: readToolFields(),
    benchmark_ticker: inputValue("set-benchmark"),
  };
  for (const field of DATA_KEY_FIELDS) payload[field] = inputValue(`set-key-${field}`);
  const queries = document.querySelector("#set-key-global_news_queries");
  if (queries) payload.global_news_queries = queries.value.split("\n").map((line) => line.trim()).filter(Boolean);
  return payload;
}

function bindDataSources() {
  const form = document.querySelector("#data-sources-form");
  if (!form) return;
  bindVendorButtons();
  bindToolButtons();
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    saveConfigPart(readDataSources(), document.querySelector("#data-sources-feedback"), form.querySelector("[type=submit]"),
      "已保存。下一次分析会使用这些数据来源。");
  });
}

function logsHelpNote() {
  const keys = ["logs_help", "logs_lines_integer", "logs_usage"];
  const rows = ((state.catalog && state.catalog.launcher_conditional) || []).filter((item) => keys.includes(item.key));
  if (!rows.length) return "";
  return rows.map((row) => `<p class="notice">${escapeHtml(row.zh)}。${escapeHtml(row.when)}</p><pre class="log-view" data-logs="${escapeHtml(row.key)}">${escapeHtml(row.cli)}</pre>`).join("");
}

function vendorFields() {
  const saved = state.form.data_vendors || {};
  return (state.catalog.vendors || []).map((item) => {
    const value = saved[item.category] || item.default || "";
    return `<div class="field field--wide"><label for="vendor-${escapeHtml(item.category)}">${escapeHtml(item.zh)} data_vendors.${escapeHtml(item.category)}${item.optional ? "，可选增强" : ""}</label><div id="vendor-select-${escapeHtml(item.category)}"></div><input id="vendor-${escapeHtml(item.category)}" type="hidden" value="${escapeHtml(value)}"></div>`;
  }).join("");
}

function readVendorFields() {
  const vendors = {};
  for (const item of state.catalog.vendors || []) {
    const input = document.querySelector(`#vendor-${item.category}`);
    if (input && input.value.trim()) vendors[item.category] = input.value.trim();
  }
  return vendors;
}

function bindVendorButtons() {
  for (const item of state.catalog.vendors || []) {
    const input = document.querySelector(`#vendor-${item.category}`);
    const host = document.querySelector(`#vendor-select-${item.category}`);
    if (!input || !host) continue;
    const selected = input.value.split(",").map((part) => part.trim()).filter((part) => part && part !== "default");
    mountMultiSelect(host, item.vendors.map((vendor) => ({value: vendor, zh: vendor})), selected, (value, pressed) => {
      const current = input.value.split(",").map((part) => part.trim()).filter((part) => part && part !== "default");
      const next = pressed ? [...current.filter((part) => part !== value), value] : current.filter((part) => part !== value);
      input.value = next.join(",");
    }, {hint: "空格切换，a 全选或取消全选，Enter 完成。命令行没有供应商菜单，这里改的是 data_vendors。"});
  }
}

function toolFields() {
  const saved = state.form.tool_vendors || {};
  return (state.catalog.tools || []).map((item) => {
    const value = saved[item.method] || "";
    return `<div class="field field--wide"><label for="tool-${escapeHtml(item.method)}">${escapeHtml(item.zh)} tool_vendors.${escapeHtml(item.method)}。留空沿用类别链。</label><div id="tool-select-${escapeHtml(item.method)}"></div><input id="tool-${escapeHtml(item.method)}" type="hidden" value="${escapeHtml(value)}"></div>`;
  }).join("");
}

function readToolFields() {
  const tools = {};
  for (const item of state.catalog.tools || []) {
    const input = document.querySelector(`#tool-${item.method}`);
    if (input && input.value.trim()) tools[item.method] = input.value.trim();
  }
  return Object.keys(tools).length ? tools : null;
}

function bindToolButtons() {
  for (const item of state.catalog.tools || []) {
    const input = document.querySelector(`#tool-${item.method}`);
    const host = document.querySelector(`#tool-select-${item.method}`);
    if (!input || !host) continue;
    const selected = input.value.split(",").map((part) => part.trim()).filter((part) => part && part !== "default");
    mountMultiSelect(host, item.vendors.map((vendor) => ({value: vendor, zh: vendor})), selected, (value, pressed) => {
      const current = input.value.split(",").map((part) => part.trim()).filter((part) => part && part !== "default");
      const next = pressed ? [...current.filter((part) => part !== value), value] : current.filter((part) => part !== value);
      input.value = next.join(",");
    }, {hint: "空格切换。留空沿用类别链。tool_vendors 优先于 data_vendors。"});
  }
}

let layerState = null;


function focusableIn(root) {
  return [...root.querySelectorAll("button, a[href], input, textarea, [tabindex]:not([tabindex='-1'])")]
    .filter((el) => !el.disabled && el.getAttribute("aria-hidden") !== "true");
}

function closeLayer() {
  if (!layerState) return;
  document.removeEventListener("keydown", layerState.onKey);
  layerState.onClose?.();
  portal.innerHTML = "";
  document.body.classList.remove("layer-open");
  const trigger = layerState.trigger;
  layerState = null;
  if (trigger && typeof trigger.focus === "function") trigger.focus();
}

function openLayer(html, options = {}) {
  closeLayer();
  const trigger = options.trigger || document.activeElement;
  portal.innerHTML = html;
  document.body.classList.add("layer-open");
  const layer = portal.firstElementChild;
  const dialog = layer.querySelector("[role='dialog']") || layer;
  const onKey = (event) => {
    if (!layerState) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeLayer();
      return;
    }
    if (event.key !== "Tab") return;
    const items = focusableIn(dialog);
    if (!items.length) {
      event.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !dialog.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  };
  layerState = {onKey, trigger, onClose: options.onClose};
  document.addEventListener("keydown", onKey);
  layer.addEventListener("mousedown", (event) => {
    if (event.target === layer) closeLayer();
  });
  const start = options.focus ? dialog.querySelector(options.focus) : focusableIn(dialog)[0];
  if (start) start.focus();
}

function confirmAction(title, body, onYes, trigger) {
  openLayer(`<div class="confirm-dialog-layer" role="presentation"><div class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-body">
    <h2 id="confirm-title">${escapeHtml(title)}</h2><p id="confirm-body">${escapeHtml(body)}</p>
    <div class="confirm-dialog-actions"><button class="button button--secondary" type="button" id="confirm-cancel">取消</button><button class="button button--danger" type="button" id="confirm-yes">确认</button></div>
  </div></div>`, {focus: "#confirm-cancel", trigger});
  document.querySelector("#confirm-cancel").addEventListener("click", () => closeLayer());
  document.querySelector("#confirm-yes").addEventListener("click", async () => {
    closeLayer();
    await onYes();
  });
}

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));
document.querySelector("#create-analysis").addEventListener("click", () => {
  const trigger = document.querySelector("#create-analysis");
  if (openMenu?.trigger === trigger) {
    closeMenus();
    trigger.focus();
    return;
  }
  closeMenus();
  const menu = document.createElement("div");
  menu.id = "create-menu";
  menu.className = "sidebar-create-menu";
  menu.setAttribute("role", "menu");
  menu.innerHTML = `<button class="sidebar-create-menu-item" type="button" role="menuitem" data-page="analyze">新建分析<span>start</span></button><button class="sidebar-create-menu-item sidebar-create-menu-item--separated" type="button" role="menuitem" data-page="backtest">新建回测<span>backtest</span></button>`;
  let index = 0;
  const items = () => [...menu.querySelectorAll("[role='menuitem']")];
  const paint = () => items().forEach((item, i) => item.classList.toggle("custom-select-option--highlighted", i === index));
  menu.addEventListener("click", (event) => {
    const item = event.target.closest("[data-page]");
    if (!item) return;
    closeMenus();
    showPage(item.dataset.page);
    trigger.focus();
  });
  bindMenu(menu, trigger, (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenus();
      trigger.focus();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      index = (index + 1) % items().length;
      paint();
      items()[index].focus();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      index = (index + items().length - 1) % items().length;
      paint();
      items()[index].focus();
    } else if (event.key === "Enter") {
      event.preventDefault();
      items()[index].click();
    }
  });
  trigger.classList.add("sidebar-create-button--open");
  paint();
  items()[0].focus();
});
document.querySelector("#collapse-sidebar").addEventListener("click", () => {
  document.querySelector("#app-shell").classList.toggle("app-shell--collapsed");
});
if (window.matchMedia("(max-width: 700px)").matches) {
  document.querySelector("#app-shell").classList.add("app-shell--collapsed");
}
function closeSearch() {
  document.querySelector("#command-search-menu")?.remove();
}

function renderSearch(query) {
  closeSearch();
  const needle = query.trim().toLowerCase();
  document.querySelectorAll(".nav-item").forEach((button) => {
    const text = `${button.textContent} ${button.dataset.cli || ""}`.toLowerCase();
    button.hidden = Boolean(needle) && !text.includes(needle);
  });
  if (!needle) return;
  const matches = ((state.catalog && state.catalog.search) || []).filter((item) => {
    const haystack = `${item.zh} ${item.cli} ${item.summary || ""}`.toLowerCase();
    return haystack.includes(needle);
  }).slice(0, 12);
  const menu = document.createElement("div");
  menu.id = "command-search-menu";
  menu.className = "sidebar-create-menu";
  menu.setAttribute("role", "listbox");
  menu.innerHTML = matches.length
    ? matches.map((item) => `<button type="button" class="sidebar-create-menu-item" role="option" data-page="${escapeHtml(item.page)}">${escapeHtml(item.zh)}<span>${escapeHtml(item.cli)}</span></button>`).join("")
    : `<p class="form-hint">没有匹配的命令。不会编造结果。</p>`;
  document.body.append(menu);
  placeMenu(menu, document.querySelector("#command-search"));
  menu.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => {
    showPage(button.dataset.page);
    closeSearch();
  }));
}

document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && state.catalog) probeSession(); });
window.addEventListener("focus", () => { if (state.catalog) probeSession(); });
document.querySelector("#command-search").addEventListener("input", (event) => renderSearch(event.target.value));
document.querySelector("#command-search").addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeSearch();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
  const target = event.target;
  if (target && target.closest && target.closest("input, textarea, [contenteditable='true']")) return;
  if (openMenu || layerState) return;
  event.preventDefault();
  const search = document.querySelector("#command-search");
  if (!search) return;
  search.focus();
  search.select();
});

// Fill state.form the way a freshly opened page does: the catalog defaults,
// then the remembered choices and saved config, then the environment locks.
function applyFormDefaults(catalog, config) {
  const defaults = (catalog && catalog.form_defaults) || {};
  for (const [key, value] of Object.entries(defaults)) {
    if (value != null && value !== "") state.form[key] = value;
  }
  const saved = config.saved || {};
  const prefs = (config.prefs && config.prefs.values) || {};
  state.prefsNote = config.prefs && config.prefs.note;
  state.depthOverrides = config.depth_overrides || [];
  state.environmentLines = config.environment_lines || [];
  state.envOutputLanguage = config.env_output_language || "";
  state.outputLanguageFromEnv = Boolean(state.envOutputLanguage);
  state.envSkipped = config.env_skipped || {};
  for (const key of ["output_language", "analysts", "research_depth", "llm_provider", "quick_think_provider", "deep_think_provider", "quick_think_llm", "deep_think_llm", "backend_url", "openai_reasoning_effort", "google_thinking_level", "anthropic_effort"]) {
    if (prefs[key] != null && prefs[key] !== "" && !(Array.isArray(prefs[key]) && !prefs[key].length)) state.form[key] = prefs[key];
  }
  for (const key of ["temperature", "max_tokens", "llm_max_retries", "max_tool_rounds", "benchmark_ticker", "quick_think_backend_url", "deep_think_backend_url", "output_language", "openai_reasoning_effort", "google_thinking_level", "anthropic_effort", "data_vendors", "tool_vendors", "memory_log_path", "max_risk_discuss_rounds", "data_cache_dir", "results_dir", "news_article_limit", "global_news_article_limit", "global_news_lookback_days", "holding_period_days", "memory_log_max_entries", "global_news_queries", "timeout", "llm_call_budget"]) {
    if (saved[key]) state.form[key] = saved[key];
  }
  if (state.outputLanguageFromEnv) state.form.output_language = state.envOutputLanguage;
  applyEnvSkipped(state.form);
  state.recent = config.recent || [];
  state.bootConfig = config;
}

Promise.all([api("/api/catalog"), api("/api/auth/status"), api("/api/config"), api("/api/updates"), loadFrameworks()]).then(([catalog, auth, config, updates]) => {
  state.catalog = catalog;
  state.auth = auth;
  state.updates = updates;
  applyFormDefaults(catalog, config);
  setIdentity(auth.accounts);
  const routeId = statusRouteId();
  if (routeId) showPage("runs", {runDetail: routeId});
  else if (window.location.hash === STATUS_ROUTE) showPage("runs");
  else if (state.page && state.page !== "analyze") showPage(state.page);
  else showPage("analyze", {framework: startRouteFramework()});  // #start/<id> survives a refresh
}).catch((error) => {
  pageEl.innerHTML = `<p class="error-banner">${escapeHtml(error.message)}</p>`;
});
