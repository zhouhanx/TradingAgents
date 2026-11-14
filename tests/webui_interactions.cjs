// DOM unit tests: fixtures only, no browser connection or live model/account actions.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {createRequire} = require('node:module');
const {execFileSync} = require('node:child_process');
const {JSDOM} = createRequire(resolve(__dirname, '../webui/package.json'))('jsdom');
const root = resolve(__dirname, '..');
const catalog = JSON.parse(execFileSync(process.env.TRADINGAGENTS_TEST_PYTHON || 'python3', [
  '-c', 'import json; from webui.server import Handler; print(json.dumps(Handler._catalog(None)))',
], {cwd: root, encoding: 'utf8'}));
const script = readFileSync(resolve(root, 'webui/static/app.js'), 'utf8');
const markup = readFileSync(resolve(root, 'webui/static/index.html'), 'utf8');
const settle = () => new Promise((done) => setImmediate(done));

function styleRules(window) {
  const flatten = (rules) => [...(rules || [])].flatMap((rule) => [
    ...(rule.selectorText ? [rule] : []),
    ...(rule.cssRules ? flatten(rule.cssRules) : []),
  ]);
  return [...window.document.styleSheets].flatMap((sheet) => flatten(sheet.cssRules));
}

function selectorMatches(rule, selector) {
  return rule.selectorText?.split(',').some((item) => item.trim() === selector);
}

function normalizeCssValue(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

// WEBUI-007 item 1: 查看状态 is only the run list; a run's detail opens from its row.
async function openRunFromList(ui, id) {
  const pass = async () => { for (let i = 0; i < 8; i++) await settle(); };
  await ui.click('.nav-item[data-page=runs]');
  await pass();
  await ui.click(id ? `#run-list [data-run-id="${id}"]` : '#run-list [data-run-id]');
  await pass();
  assert.ok(ui.doc.querySelector('#runs-back'), 'the run detail is open');
}

// jsdom delivers history traversal (back/forward → popstate) on a timer.
async function waitHistory() {
  await new Promise((done) => setTimeout(done, 30));
  for (let i = 0; i < 8; i++) await settle();
}

// WEBUI-008: 启动分析 opens on the framework cards; most tests start in the
// TradingAgents wizard (#start/tradingagents), the hub tests pass url 'http://localhost/'.
const FRAMEWORKS = {default: 'tradingagents', frameworks: [{id: 'tradingagents', name: 'TradingAgents',
  description: '多智能体交易研究。分析师、研究员、交易员、风控和组合经理分工协作，给出评级和交易计划。',
  icon: '/icons/logo.svg', tags: ['股票', '加密货币', '4 步设置']}]};
const WIZARD_URL = 'http://localhost/#start/tradingagents';

async function setup(overrides = {}, {html = markup, url = WIZARD_URL} = {}) {
  const dom = new JSDOM(html, {url, runScripts: 'outside-only'});
  const {window} = dom;
  for (const file of ['console.css', 'app.css']) {
    const style = window.document.createElement('style');
    style.textContent = readFileSync(resolve(root, 'webui/static', file), 'utf8');
    window.document.head.append(style);
  }
  window.HTMLElement.prototype.scrollIntoView = function () {};
  window.matchMedia = () => ({matches: false, addEventListener() {}, removeEventListener() {}});
  const calls = [];
  const auth = {accounts: [{provider: 'chatgpt', signed_in: false}, {provider: 'grok', signed_in: true, account: 'Fixture User'}]};
  const routes = {
    '/api/catalog': catalog,
    '/api/auth/status': auth,
    // A POST answers like the server ({saved: true, config}); the page only says 已保存 for saved === true.
    '/api/config': (options) => options.method === 'POST'
      ? {saved: true, config: {}}
      : {saved: {}, keys: [{provider: 'openai', configured: false, env_var: 'OPENAI_API_KEY'}], data_env: [], cloud_env: []},
    '/api/updates': {}, '/api/auth/login': {status: 'idle', messages: []},
    '/api/runs': {run: null, progress: {}, result: null},
    '/api/reports': {reports: []}, '/api/backtests': {sweeps: []},
    '/api/watchlist': {items: [], triggers: [], mode: 'observe', mode_label: '观察', rule_version: 'watch-1', note: ''},
    '/api/schedule/tick': {schedules: [], reminders: [], triggers: [], checks: [], mode_label: '观察', rule_version: 'sched-1'},
    '/api/cost': {runs: [], note: '', rule: ''},
    '/api/compare': {runs: [], note: ''},
    '/api/frameworks': FRAMEWORKS,
        ...overrides,
  };
  window.fetch = async (path, options = {}) => {
    calls.push({path, ...options});
    let value = routes[path] ?? routes[path.split('?')[0]];
    if (path.startsWith('/api/models?')) value = {models: [{id: 'fixture-model', zh: 'Fixture Model'}]};
    if (value === undefined && path.startsWith('/api/asset?')) value = {asset_type: 'stock', analysts: ['market', 'social', 'news', 'fundamentals']};
    if (typeof value === 'function') value = await value(options, path);
    if (value === undefined) throw new Error(`Unexpected fixture request: ${path}`);
    if (value && value.__status) return {ok: false, status: value.__status, json: async () => structuredClone(value.body)};
    return {ok: true, json: async () => structuredClone(value)};
  };
  window.eval(`${script}\nwindow.uiTest = {state, showPage, openRunDetail, closeRunDetail, readForm, renderAnalyze, renderRuns, renderAccounts, renderReports, submitAnalysis, resultPanel, renderMarkdown, openSaveDrawer, markScrollableTables, paintHolding, paintReview, nextStepPanel, restartRefusal, restartRefusalHtml, liveSectionTable, analystRequiredHtml, infoItemsHtml, customModelRow, infoAttachHtml, showToast, formNoteHtml, compareCard, infoStatusHtml, paintReplay, modelTable, renderModelChoices, api, watchWhen, watchQuoteContext, watchClock, watchStamp, watchTableOrder, watchTypedText, watchThresholdText, ruleValueError, rulePreview};`);
  // The watchlist fixtures are from 2026-11-13 evening (Beijing); date labels (W-WL1) compare with this day.
  window.eval("watchNowMs = () => Date.parse('2026-11-13T14:45:00Z')");
  for (let i = 0; i < 5; i++) await settle();
  const doc = window.document;
  const click = async (selector) => {
    const element = doc.querySelector(selector);
    assert.ok(element, `Missing ${selector}`);
    element.click();
    for (let i = 0; i < 4; i++) await settle();
  };
  const fill = (selector, value) => {
    const el = doc.querySelector(selector);
    el.value = value;
    el.dispatchEvent(new window.Event('input', {bubbles: true}));
  };
  const visibleText = () => {
    const copy = doc.querySelector('#page').cloneNode(true);
    copy.querySelectorAll('[hidden], details:not([open]) > :not(summary)').forEach((node) => node.remove());
    return copy.textContent;
  };
  return {dom, window, doc, calls, routes, click, fill, visibleText, ui: window.uiTest};
}

test('analysis guides through four stages, validates, retains input and submits only on final action', async () => {
  const ui = await setup({'/api/runs': (options) => options.method ? {id: 'fixture-run'} : {run: null, progress: {}}});
  try {
    const {doc, click, fill, calls} = ui;
    assert.equal(doc.querySelectorAll('[data-step]').length, 4);
    assert.equal(doc.querySelectorAll('[data-step-panel]:not([hidden])').length, 1);
    assert.equal(doc.querySelector('#run-analysis').hidden, true);
    assert.doesNotMatch(ui.visibleText(), /命令行|--ticker|花括号|CLI/);
    await click('#step-next');
    assert.match(doc.querySelector('#form-note').textContent, /填写/);
    assert.equal(ui.ui.state.step, 1);
    fill('#ticker', 'NVDA'); fill('#date', '2026-10-01');
    await click('#step-next');
    assert.equal(ui.ui.state.step, 2);
    ui.ui.state.form.analysts = ['market'];
    await click('#step-next');
    assert.equal(ui.ui.state.step, 3);
    fill('#quick-model', 'fixture-model'); fill('#deep-model', 'fixture-model');
    await click('#step-next');
    assert.equal(ui.ui.state.step, 4);
    assert.equal(doc.querySelector('#run-analysis').hidden, false);
    assert.match(doc.querySelector('#analysis-summary').textContent, /NVDA/);
    await click('#step-back'); await click('#step-back'); await click('#step-back');
    assert.equal(doc.querySelector('#ticker').value, 'NVDA');
    assert.equal(doc.querySelector('#quick-model').value, 'fixture-model');
    assert.equal(calls.filter((item) => item.path === '/api/runs' && item.method === 'POST').length, 0);
    await click('#step-next'); await click('#step-next'); await click('#step-next'); await click('#run-analysis');
    const submitted = calls.find((item) => item.path === '/api/runs' && item.method === 'POST');
    assert.equal(JSON.parse(submitted.body).ticker, 'NVDA');
    assert.ok(doc.querySelector('#launch-dialog'), 'a launch answers with the result dialog');
    assert.equal(ui.ui.state.page, 'analyze');
  } finally { ui.dom.window.close(); }
});

test('all pages use full-width surfaces while retaining fluid containers', async () => {
  const ui = await setup();
  try {
    const pages = ['analyze', 'runs', 'reports', 'kline', 'watchlist', 'schedule', 'cost', 'compare', 'backtest', 'accounts', 'models'];
    assert.deepEqual([...ui.doc.querySelectorAll('.nav-item[data-page]')].map((item) => item.dataset.page), pages);
    for (const gone of ['settings', 'memory', 'help']) assert.equal(ui.doc.querySelector(`[data-page=${gone}]`), null, `${gone} page is folded away`);
    for (const page of pages) {
      await ui.click(`.nav-item[data-page=${page}]`);
      const surface = ui.doc.querySelector('#page');
      const style = ui.window.getComputedStyle(surface);
      assert.equal(surface.dataset.page, page);
      assert.equal(style.maxWidth, 'none', `${page} content remains uncapped`);
      assert.equal(style.width, '100%', `${page} remains fluid on smaller screens`);
      assert.equal(style.marginLeft, '0px', `${page} surface has no left centering margin`);
      assert.equal(style.marginRight, '0px', `${page} surface has no right centering margin`);
    }
  } finally { ui.dom.window.close(); }
});

test('the wizard has a step rail, one panel at a time and a four-part review (WEBUI-005)', async () => {
  const ui = await setup();
  try {
    const {doc} = ui;
    assert.ok(doc.querySelector('.wiz .steps'), 'the step rail sits beside the form');
    assert.equal(doc.querySelectorAll('.steps .st').length, 4);
    assert.ok(doc.querySelector('.st.on[data-step="1"]'));
    assert.match(doc.querySelector('.kick').textContent, /第 1 步，共 4 步/);
    assert.equal(doc.querySelectorAll('input[type=file]:not(.visually-hidden)').length, 0, 'no unstyled native file input');
    ui.fill('#ticker', 'NVDA'); ui.fill('#date', '2026-10-01');
    await ui.click('#step-next');
    assert.ok(doc.querySelector('.st.done[data-step="1"]'), 'a finished step is marked done');
    assert.match(doc.querySelector('.kick').textContent, /第 2 步，共 4 步/);
    ui.ui.state.form.analysts = ['market'];
    await ui.click('#step-next');
    ui.fill('#quick-model', 'fixture-model'); ui.fill('#deep-model', 'fixture-model');
    await ui.click('#step-next');
    const sections = [...doc.querySelectorAll('#analysis-summary .sec')];
    assert.deepEqual(sections.map((sec) => sec.querySelector('.sh > span').textContent),
      ['分析对象', '研究范围', '模型', '保存与续跑']);
    assert.match(doc.querySelector('#analysis-summary').textContent, /NVDA/);
    assert.match(doc.querySelector('#analysis-summary').textContent, /断点续跑开，失败后可从出错的角色继续/);
    await ui.click('#analysis-summary [data-edit-step="1"]');
    assert.equal(ui.ui.state.step, 1);
    assert.equal(doc.querySelector('#ticker').value, 'NVDA');
  } finally { ui.dom.window.close(); }
});

test('small numeric controls and account form columns retain their intended widths', async () => {
  const ui = await setup({'/api/runs': {run: {id: 'fixture', status: 'finished'}, progress: {}}});
  try {
    await openRunFromList(ui);
    assert.equal(ui.window.getComputedStyle(ui.doc.querySelector('#log-lines')).width, '120px');
    await ui.click('.nav-item[data-page=backtest]');
    assert.equal(ui.window.getComputedStyle(ui.doc.querySelector('#bt-every')).width, '120px');
    await ui.click('.nav-item[data-page=accounts]');
    assert.equal(ui.doc.querySelector('#key-value').closest('.field').classList.contains('field--wide'), false);
    assert.equal(ui.window.getComputedStyle(ui.doc.querySelector('#paste-form')).maxWidth, '720px');
  } finally { ui.dom.window.close(); }
});

async function openDrawerAtReview(ui) {
  ui.fill('#ticker', 'NVDA'); ui.fill('#date', '2026-10-01');
  await ui.click('#step-next');
  ui.ui.state.form.analysts = ['market'];
  await ui.click('#step-next');
  ui.fill('#quick-model', 'fixture-model'); ui.fill('#deep-model', 'fixture-model');
  await ui.click('#step-next');
  const trigger = ui.doc.querySelector('#open-save-drawer');
  assert.ok(trigger, 'the review offers 调整 for 保存与续跑');
  trigger.focus();
  await ui.click('#open-save-drawer');
  assert.ok(ui.doc.querySelector('.save-drawer[role=dialog]'));
  return trigger;
}

test('the 保存与续跑 drawer is wide enough, discards on Escape and applies only after a successful save', async () => {
  let fail = false;
  const ui = await setup({'/api/config': (options) => {
    if (options.method && fail) throw new Error('配置写入失败');
    if (options.method === 'POST') return {saved: true, config: {}};
    return {saved: {}, keys: [], data_env: [], cloud_env: []};
  }});
  try {
    const css = readFileSync(resolve(root, 'webui/static/app.css'), 'utf8');
    assert.match(css, /\.save-drawer\s*\{[^}]*width:\s*min\(800px,\s*92vw\)/);
    assert.match(css, /@media \(max-width: 600px\)[\s\S]*\.save-drawer\s*\{[^}]*width:\s*100%/);
    const trigger = await openDrawerAtReview(ui);
    ui.fill('#save-path', 'reports/NVDA');
    ui.doc.dispatchEvent(new ui.window.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
    for (let i = 0; i < 4; i++) await settle();
    assert.equal(ui.doc.querySelector('.save-drawer'), null);
    assert.equal(ui.ui.readForm().save_path ?? null, null, 'Escape discards the draft');
    assert.equal(ui.doc.activeElement, trigger, 'focus returns to 调整');

    await ui.click('#open-save-drawer');
    ui.fill('#save-path', 'reports/NVDA');
    await ui.click('.save-drawer [data-toggle=checkpoint]');
    assert.equal(ui.doc.querySelector('[data-toggle=checkpoint]').getAttribute('aria-checked'), 'false');
    fail = true;
    await ui.click('#save-drawer-save');
    assert.match(ui.doc.querySelector('#save-drawer-note').textContent, /配置写入失败/);
    assert.ok(ui.doc.querySelector('.save-drawer'), 'a failed save keeps the drawer open');
    assert.equal(ui.ui.state.form.checkpoint !== false, true, 'nothing is applied when the save fails');
    fail = false;
    await ui.click('#save-drawer-save');
    assert.equal(ui.doc.querySelector('.save-drawer'), null);
    assert.equal(ui.ui.readForm().save_path, 'reports/NVDA');
    assert.equal(ui.ui.readForm().checkpoint, false);
    assert.match(ui.doc.querySelector('#analysis-summary').textContent, /关，失败后只能从头开始/);
    assert.equal(ui.doc.activeElement?.id, 'open-save-drawer', 'focus lands back on 调整 after the review repaints');
  } finally { ui.dom.window.close(); }
});

test('step 1 asks 是否持仓; 已持仓 needs a positive 持仓价格, 持有数量 is optional, and both reach the payload (WEBUI-006 item 4)', async () => {
  const ui = await setup({'/api/runs': (options) => options.method ? {id: 'fixture-run'} : {run: null, progress: {}}});
  try {
    const {doc} = ui;
    const held = () => doc.querySelector('#held-seg [aria-pressed="true"]')?.textContent;
    assert.equal(held(), '未持仓', 'a new analysis starts without a holding');
    assert.equal(doc.querySelector('#holding-price'), null);
    ui.fill('#ticker', 'BTC-USD'); ui.fill('#date', '2026-10-30');
    await ui.click('#held-seg [data-held="true"]');
    assert.equal(held(), '已持仓');
    assert.equal(doc.querySelector('#holding-price').required, true);
    assert.match(doc.querySelector('label[for=holding-price]').textContent, /持仓价格\*/);
    assert.match(doc.querySelector('label[for=holding-quantity]').textContent, /持有数量（可选）/);
    assert.equal(doc.querySelector('#holding-hint').textContent, '用于组合经理给出加仓或减仓建议。持仓价格按买入成本填写。');
    await ui.click('#step-next');
    assert.equal(ui.ui.state.step, 1, 'a missing 持仓价格 keeps step 1');
    assert.match(doc.querySelector('#form-note').textContent, /持仓价格/);
    for (const bad of ['0', '-5', 'abc']) {
      ui.fill('#holding-price', bad);
      await ui.click('#step-next');
      assert.equal(ui.ui.state.step, 1, `持仓价格 ${bad} is refused`);
    }
    ui.fill('#holding-price', '62000');
    ui.fill('#holding-quantity', '0');
    await ui.click('#step-next');
    assert.equal(ui.ui.state.step, 1, '持有数量 0 is refused');
    ui.fill('#holding-quantity', '');
    await ui.click('#step-next');
    assert.equal(ui.ui.state.step, 2, 'price alone is enough');
    assert.deepEqual(JSON.parse(JSON.stringify(ui.ui.readForm().portfolio)), {positions: [{ticker: 'BTC-USD', average_price: 62000}]});
    await ui.click('#step-back');
    assert.equal(doc.querySelector('#holding-price').value, '62000', 'going back keeps the holding');
    ui.fill('#holding-quantity', '0.5');
    await ui.click('#step-next');
    ui.ui.state.form.analysts = ['market'];
    await ui.click('#step-next');
    ui.fill('#quick-model', 'fixture-model'); ui.fill('#deep-model', 'fixture-model');
    await ui.click('#step-next');
    const sections = [...doc.querySelectorAll('#analysis-summary .sec')];
    assert.match(sections[0].textContent, /持仓已持仓 · 成本 62000 · 0\.5/, '分析对象 shows the holding');
    assert.doesNotMatch(sections[3].textContent, /持仓/, '保存与续跑 has no 持仓 row');
    await ui.click('#run-analysis');
    const submitted = JSON.parse(ui.calls.find((item) => item.path === '/api/runs' && item.method === 'POST').body);
    assert.deepEqual(submitted.portfolio, {positions: [{ticker: 'BTC-USD', average_price: 62000, quantity: 0.5}]});
    assert.equal(submitted.framework, 'tradingagents', 'the launch names the framework whose wizard it is (WEBUI-008)');
  } finally { ui.dom.window.close(); }
});

test('已持仓 with only 持仓价格, 未持仓, and a holdings JSON import (a position without average_price) each start with the right portfolio and source (WEBUI-006 item 4, W50)', async () => {
  const launched = async (prepare) => {
    const ui = await setup({'/api/runs': (options) => options.method ? {id: 'fixture-run', status: 'running'} : {run: null, progress: {}}});
    try {
      await prepare(ui);
      await ui.click('#run-analysis');
      await settleMany();
      const post = ui.calls.find((item) => item.path === '/api/runs' && item.method === 'POST');
      assert.ok(post, 'the run was started');
      assert.match(ui.doc.querySelector('#launch-title').textContent, /已启动分析/);
      return JSON.parse(post.body);
    } finally { ui.dom.window.close(); }
  };
  const costOnly = await launched(async (ui) => {
    ui.fill('#ticker', 'BTC-USD'); ui.fill('#date', '2026-10-30');
    await ui.click('#held-seg [data-held="true"]');
    ui.fill('#holding-price', '62000');
    await ui.click('#step-next');
    ui.ui.state.form.analysts = ['market'];
    await ui.click('#step-next');
    ui.fill('#quick-model', 'fixture-model'); ui.fill('#deep-model', 'fixture-model');
    await ui.click('#step-next');
    assert.match(ui.doc.querySelector('#analysis-summary .sec').textContent, /持仓已持仓 · 成本 62000 · 数量未填/);
  });
  assert.deepEqual(costOnly.portfolio, {positions: [{ticker: 'BTC-USD', average_price: 62000}]}, 'no quantity key when 持有数量 is empty');
  assert.equal(costOnly.portfolio_source, 'form', 'step 1 says so: the backend needs its cost');
  const none = await launched(fillToReview);
  assert.ok(!none.portfolio, '未持仓 sends no portfolio');
  // W50: an imported JSON is read as on b4617f1, so a position may give only its size
  const legacy = {cash: 10000, currency: 'USD', positions: [{ticker: 'NVDA', quantity: 5}, {ticker: 'AAPL', quantity: 2, average_price: 120}]};
  const imported = await launched(async (ui) => {
    await openDrawerAtReview(ui);
    ui.doc.querySelector('#portfolio-json-details').open = true;
    ui.fill('#portfolio', JSON.stringify(legacy));
    await ui.click('#save-drawer-save');
    await settleMany();
    assert.equal(ui.doc.querySelector('.save-drawer'), null, 'the drawer saved and closed');
    assert.match(ui.doc.querySelector('#analysis-summary .sec').textContent, /持仓来自导入 JSON · 2 个持仓 · 现金 10000 USD/);
  });
  assert.deepEqual(imported.portfolio, legacy, 'the legacy holdings JSON import is sent as is');
  assert.equal(imported.portfolio_source, 'json', 'the import says so: the backend reads it as before');
});

test('未持仓 sends no portfolio, and the drawer keeps only the JSON import (WEBUI-006 item 4)', async () => {
  const ui = await setup();
  try {
    ui.fill('#ticker', 'NVDA');
    await ui.click('#held-seg [data-held="true"]');
    ui.fill('#holding-price', '100');
    await ui.click('#held-seg [data-held="false"]');
    assert.equal(ui.ui.readForm().portfolio, null, 'switching back to 未持仓 drops the holding');
    assert.equal(ui.doc.querySelector('#holding-price'), null);
    await ui.click('#held-seg [data-held="true"]');
    assert.equal(ui.doc.querySelector('#holding-price').value, '100', 'the typed price comes back with 已持仓');
    await ui.click('#held-seg [data-held="false"]');
    await openDrawerAtReview(ui);
    const drawer = ui.doc.querySelector('.save-drawer');
    assert.equal(drawer.querySelector('#holding-quantity'), null);
    assert.equal(drawer.querySelector('#holding-price'), null);
    assert.ok(drawer.querySelector('#portfolio-json-details #portfolio'), 'the JSON import stays');
    assert.equal(drawer.querySelectorAll('[id^="holding"], [data-held]').length, 0, 'no holdings inputs, buttons or hints in the drawer');
    assert.deepEqual([...drawer.querySelectorAll('input, textarea')].filter((el) => el.closest('#portfolio-json-details')).map((el) => el.id), ['portfolio-file', 'portfolio'].filter((id) => drawer.querySelector('#' + id)));
    assert.doesNotMatch(drawer.textContent, /持仓（可选）|持仓价格|持有数量|已移到/);
    assert.match(ui.doc.querySelector('#analysis-summary .sec').textContent, /持仓未持仓/);
  } finally { ui.dom.window.close(); }
});

test('forward step links cannot bypass required fields or missing model selections', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-step="4"]');
    assert.equal(ui.ui.state.step, 1);
    ui.fill('#ticker', 'NVDA'); ui.fill('#date', '2026-10-01');
    await ui.click('#step-next');
    ui.ui.state.form.analysts = ['market'];
    await ui.click('#step-next');
    ui.fill('#quick-model', '');
    await ui.click('#step-next');
    assert.equal(ui.ui.state.step, 3);
    assert.match(ui.doc.querySelector('#form-note').textContent, /选择供应商和模型/);
  } finally { ui.dom.window.close(); }
});

test('fixed local configuration is visible, protected and still loads model choices', async () => {
  const ui = await setup({'/api/config': {saved: {}, env_skipped: {
    llm_provider: 'deepseek', quick_think_provider: 'deepseek', deep_think_provider: 'deepseek',
    quick_think_llm: 'fixed-model', deep_think_llm: 'fixed-model', research_depth: 2, analysts: ['market'],
  }}});
  try {
    assert.equal(ui.doc.querySelector('#main-providers button').disabled, true);
    assert.equal(ui.doc.querySelector('#quick-providers button').disabled, true);
    assert.equal(ui.doc.querySelector('#quick-model').readOnly, true);
    assert.equal(ui.doc.querySelector('#quick-model').value, 'fixed-model');
    assert.ok(ui.doc.querySelector('#quick-models button'));
    assert.equal(ui.ui.readForm().quick_think_llm, 'fixed-model');
  } finally { ui.dom.window.close(); }
});

test('start prevents duplicate submissions and recovers its action after a request failure', async () => {
  let reject;
  const ui = await setup({'/api/runs': () => new Promise((resolve, fail) => { reject = fail; })});
  try {
    const button = ui.doc.querySelector('#run-analysis');
    const first = ui.ui.submitAnalysis();
    const second = ui.ui.submitAnalysis();
    assert.equal(button.disabled, true);
    assert.equal(ui.calls.filter((item) => item.path === '/api/runs' && item.method === 'POST').length, 1);
    reject(new Error('模型服务暂时不可用'));
    await Promise.all([first, second]);
    assert.equal(button.disabled, false);
    assert.match(ui.doc.querySelector('#launch-error').textContent, /模型服务暂时不可用/);
    assert.equal(ui.doc.querySelector('#form-note').textContent, '', 'the reason is in the dialog only');
  } finally { ui.dom.window.close(); }
});

test('provider dropdown changes both inherited model services, and model selection writes the payload', async () => {
  const ui = await setup();
  try {
    await ui.click('#main-providers button');
    const options = [...ui.doc.querySelectorAll('[role=option]')];
    const provider = catalog.providers.find((item) => item.key === 'deepseek');
    const index = options.findIndex((node) => node.textContent.includes(provider.zh));
    assert.ok(index >= 0);
    await ui.click(`[role=option][data-index="${index}"]`);
    assert.equal(ui.doc.querySelector('#quick-provider').value, 'deepseek');
    assert.equal(ui.doc.querySelector('#deep-provider').value, 'deepseek');
    await ui.click('#quick-models button'); await ui.click('[role=option]');
    assert.equal(ui.doc.querySelector('#quick-model').value, 'fixture-model');
    assert.equal(ui.ui.readForm().quick_think_llm, 'fixture-model');
  } finally { ui.dom.window.close(); }
});

test('the review does not call an unrecognized depth shallow or an unrecognized asset a stock', async () => {
  const ui = await setup();
  try {
    ui.ui.state.form.research_depth = 9;
    ui.ui.state.form.asset_type = 'index';
    ui.ui.state.form.output_language = 'Klingon';
    ui.ui.state.form.ticker = 'AAA';
    ui.ui.showPage('analyze', {framework: 'tradingagents'});
    for (let i = 0; i < 4; i++) await settle();
    ui.ui.paintReview();
    const text = ui.doc.querySelector('#analysis-summary').textContent;
    assert.match(text, /研究深度未知，不把它写成浅层/);
    assert.match(text, /资产类型未知，不把它写成股票/);
    assert.match(text, /报告语言未知，不把它写成中文/);
    assert.doesNotMatch(text, /AAA · 股票|研究深度浅层|语言中文/);
  } finally { ui.dom.window.close(); }
});

test('the review still names a known depth, asset and language', async () => {
  const ui = await setup();
  try {
    ui.ui.state.form.research_depth = 1;
    ui.ui.state.form.asset_type = 'stock';
    ui.ui.state.form.output_language = 'Chinese';
    ui.ui.state.form.ticker = 'AAPL';
    ui.ui.showPage('analyze', {framework: 'tradingagents'});
    for (let i = 0; i < 4; i++) await settle();
    ui.ui.paintReview();
    const text = ui.doc.querySelector('#analysis-summary').textContent;
    assert.match(text, /AAPL · 股票/);
    assert.match(text, /研究深度浅层/);
    assert.match(text, /语言中文/);
    assert.doesNotMatch(text, /不把它写成浅层|不把它写成股票|不把它写成中文/);
  } finally { ui.dom.window.close(); }
});

test('an unrecognized research depth is not called shallow', async () => {
  const ui = await setup();
  try {
    ui.ui.state.form.research_depth = 9;
    ui.ui.showPage('analyze', {framework: 'tradingagents'});
    for (let i = 0; i < 4; i++) await settle();
    assert.equal(ui.doc.querySelector('#depths .custom-select-value').textContent, '研究深度未知');
    assert.match(ui.doc.querySelector('#depth-unknown').textContent, /不把它写成浅层/);
    assert.equal(ui.doc.querySelector('#depth-unknown').hidden, false);
  } finally { ui.dom.window.close(); }
});

test('a known research depth is still called by its name', async () => {
  const ui = await setup();
  try {
    ui.ui.state.form.research_depth = 1;
    ui.ui.showPage('analyze', {framework: 'tradingagents'});
    for (let i = 0; i < 4; i++) await settle();
    assert.equal(ui.doc.querySelector('#depths .custom-select-value').textContent, '浅层');
    assert.equal(ui.doc.querySelector('#depth-unknown').hidden, true);
  } finally { ui.dom.window.close(); }
});

test('an unrecognized report language is not called the first language', async () => {
  const ui = await setup();
  try {
    ui.ui.state.form.output_language = 'Klingon';
    ui.ui.showPage('analyze', {framework: 'tradingagents'});
    for (let i = 0; i < 4; i++) await settle();
    assert.equal(ui.doc.querySelector('#languages .custom-select-value').textContent, '状态未知');
    assert.notEqual(ui.doc.querySelector('#languages .custom-select-value').textContent, '中文');
  } finally { ui.dom.window.close(); }
});

test('an unrecognized backtest asset is not shown as stock', async () => {
  const ui = await setup();
  try {
    ui.ui.state.backtest = {tickers: 'AAA', start: '2026-11-01', end: '2026-11-12', every: 7, asset_type: 'index', analysts: ['market', 'fundamentals'], run_id: '', portfolio: ''};
    await ui.click('[data-page=backtest]');
    const shown = ui.doc.querySelector('#bt-asset .custom-select-value').textContent;
    const hint = ui.doc.querySelector('#bt-asset-hint').textContent;
    const analystHint = ui.doc.querySelector('#bt-analyst-hint').textContent;
    assert.equal(shown, '资产类型未知');
    assert.match(hint, /不把它写成股票/);
    assert.doesNotMatch(shown, /股票/);
    assert.doesNotMatch(hint, /默认使用全部分析维度|当前不是 crypto/);
    assert.match(analystHint, /不把它写成股票/);
    assert.match(analystHint, /不提供基本面分析师/);
    assert.doesNotMatch(analystHint, /默认使用全部分析维度/);
    assert.equal(ui.ui.state.backtest.analysts.includes('fundamentals'), false);
    await ui.click('#bt-analysts .custom-select-trigger');
    const options = [...ui.doc.querySelectorAll('.custom-select-menu [role=option]')].map((el) => el.textContent);
    assert.equal(options.some((text) => text.includes('基本面')), false);
    assert.ok(options.some((text) => text.includes('市场分析师')));
  } finally { ui.dom.window.close(); }
});

test('a stock backtest still shows stock', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=backtest]');
    assert.equal(ui.doc.querySelector('#bt-asset .custom-select-value').textContent, '股票');
    assert.equal(ui.doc.querySelector('#bt-asset-hint'), null);
    assert.match(ui.doc.querySelector('#bt-analyst-hint').textContent, /默认使用全部分析维度/);
    await ui.click('#bt-analysts .custom-select-trigger');
    const options = [...ui.doc.querySelectorAll('.custom-select-menu [role=option]')].map((el) => el.textContent);
    assert.equal(options.some((text) => text.includes('基本面分析师')), true);
  } finally { ui.dom.window.close(); }
});


test('a crypto backtest still does not offer fundamentals', async () => {
  const ui = await setup();
  try {
    ui.ui.state.backtest = {tickers: 'BTC-USD', start: '2026-11-01', end: '2026-11-12', every: 7, asset_type: 'crypto', analysts: ['market', 'fundamentals'], run_id: '', portfolio: ''};
    await ui.click('[data-page=backtest]');
    assert.equal(ui.ui.state.backtest.analysts.includes('fundamentals'), false);
    assert.match(ui.doc.querySelector('#bt-analyst-hint').textContent, /加密资产不使用基本面分析/);
    await ui.click('#bt-analysts .custom-select-trigger');
    const options = [...ui.doc.querySelectorAll('.custom-select-menu [role=option]')].map((el) => el.textContent);
    assert.equal(options.some((text) => text.includes('基本面')), false);
  } finally { ui.dom.window.close(); }
});

test('backtest defaults are concise, optional settings collapsed, estimate never starts a job', async () => {
  const ui = await setup({'/api/backtests/validate': {cells: 4, analysts: ['market']}});
  try {
    await ui.click('[data-page=backtest]');
    assert.doesNotMatch(ui.visibleText(), /命令行|--start|summarize|花括号/);
    assert.equal(ui.doc.querySelectorAll('#backtest-form details[open]').length, 0);
    await ui.click('#check-backtest');
    assert.match(ui.doc.querySelector('#backtest-result').textContent, /至少填写一个标的/);
    assert.equal(ui.calls.filter((item) => item.path === '/api/backtests/validate').length, 0);
    ui.fill('#bt-tickers', 'NVDA'); ui.fill('#bt-start', '2026-10-01'); ui.fill('#bt-end', '2026-10-08');
    await ui.click('#check-backtest');
    const estimate = ui.doc.querySelector('#backtest-result');
    assert.match(estimate.textContent, /共 4 次分析。尚未启动；确认后点击「启动回测」。/);
    assert.match(estimate.innerHTML, /共 4 次分析。<br class="notice-break">尚未启动；<br class="notice-break">确认后点击「启动回测」。/);
    assert.equal(ui.calls.filter((item) => item.path === '/api/backtests' && item.method === 'POST').length, 0);
  } finally { ui.dom.window.close(); }
});

test('status empty state and task errors are understandable; technical details start collapsed', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=runs]');
    assert.match(ui.visibleText(), /还没有任务/);
    ui.routes['/api/runs'] = {run: {status: 'failed', kind: 'analysis', selections: {ticker: 'NVDA', date: '2026-10-01'}, error: '需要登录'}, progress: {}, result: {present: false}};
    await ui.ui.renderRuns();
    assert.equal(ui.doc.querySelector('#run-detail'), null, 'the list comes first');
    await openRunFromList(ui);
    assert.match(ui.visibleText(), /NVDA|需要登录/);
    assert.equal(ui.doc.querySelector('#run-technical-details').open, false);
    assert.doesNotMatch(ui.visibleText(), /任务 ID|任务启动参数|并发启动/);
  } finally { ui.dom.window.close(); }
});

function failedRun({saved}) {
  return {
    run: {id: 'failed-fixture', status: 'failed', kind: 'analysis', selections: {ticker: 'NVDA', date: '2026-10-01'},
          failure: {role: 'Bull Researcher', kind: 'timeout', summary: 'TimeoutError: grok-4.7 300 秒未返回'},
          checkpoint: {enabled: true, saved}},
    progress: {agents: [
      {team: 'Analyst Team', team_zh: '分析师团队', agent: 'Market Analyst', zh: '市场分析师', status: 'completed', status_zh: '完成'},
      {team: 'Research Team', team_zh: '研究团队', agent: 'Bull Researcher', zh: '多头研究员', status: 'error', status_zh: '错误'},
      {team: 'Trading Team', team_zh: '交易团队', agent: 'Trader', zh: '交易员', status: 'pending', status_zh: '等待'},
    ], timeline: [{agent: 'Bull Researcher', zh: '多头研究员', status: 'error', time: '21:14:02'}]},
    result: {present: false},
  };
}

test('a failed run names where it stopped and offers resume only when a checkpoint was saved (RUN-002)', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=runs]');
    ui.routes['/api/runs'] = failedRun({saved: true});
    await openRunFromList(ui, 'failed-fixture');
    const alert = ui.doc.querySelector('#run-failure');
    assert.equal(alert.getAttribute('role'), 'alert');
    assert.match(alert.textContent, /研究团队这一步停止了/);
    assert.match(alert.textContent, /已完成 1 个角色，多头研究员出错后进程退出/);
    assert.match(alert.textContent, /TimeoutError: grok-4\.7 300 秒未返回/);
    assert.ok(ui.doc.querySelector('#show-error-log'));
    const resume = ui.doc.querySelector('#resume-run');
    assert.equal(resume.disabled, false);
    assert.ok(resume.classList.contains('button--primary'));
    assert.ok(ui.doc.querySelector('.team.bad[data-team="Research Team"]'));
    assert.match(ui.doc.querySelector('.team.bad').textContent, /停在这里/);
    const stoppedHere = ui.doc.querySelector('.team.bad .now');
    assert.equal(stoppedHere.textContent, '停在这里：TimeoutError: grok-4.7 300 秒未返回。已完成的角色已存进断点，可以从这里继续。');
    assert.match(stoppedHere.innerHTML, /停在这里：<\/b><br>TimeoutError: grok-4\.7 300 秒未返回。<br>已完成的角色已存进断点，<br>可以从这里继续。/);
    assert.ok(ui.doc.querySelector('.team.dim[data-team="Trading Team"]'), 'waiting teams are dimmed');
    assert.equal(ui.doc.querySelector('#stop-run'), null, 'no stop button once the task ended');
    assert.equal(ui.doc.querySelector('#run-technical-details').open, false);

    ui.routes['/api/runs/restart'] = {zh: '已重新启动'};
    ui.routes['/api/runs'] = {run: null, progress: {}};
    await ui.click('#resume-run');
    const posted = ui.calls.find((item) => item.path === '/api/runs/restart');
    assert.deepEqual(JSON.parse(posted.body), {id: 'failed-fixture', resume: true}, 'resume names the run on screen even when no row was clicked');
  } finally { ui.dom.window.close(); }
});

test('a failed unrecognized run is not called an unfinished analysis', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'kind-fail', status: 'failed', kind: 'index', ticker: 'AAA', date: '2026-11-12', selections: {ticker: 'AAA', date: '2026-11-12'}, failure: {summary: '超时'}},
    runs: [{id: 'kind-fail', status: 'failed', ticker: 'AAA', date: '2026-11-12'}],
    progress: {agents: [{team: 'Research Team', team_zh: '研究团队', agent: 'Trader', zh: '交易员', status: 'error', status_zh: '错误'}]},
    result: {present: false},
    counts: {all: 1, running: 0, queued: 0, failed: 1, finished: 0},
  }});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'kind-fail');
    const alert = ui.doc.querySelector('#run-failure').textContent;
    assert.match(alert, /任务在研究团队这一步停止了，不把它写成分析/);
    assert.doesNotMatch(alert, /分析在|分析没有完成/);
    ui.routes['/api/runs'] = {run: {id: 'known-fail', status: 'failed', kind: 'analysis', ticker: 'AAA', date: '2026-11-12', selections: {ticker: 'AAA'}}, runs: [{id: 'known-fail', status: 'failed', ticker: 'AAA', date: '2026-11-12'}], progress: {agents: []}, result: {present: false}};
    ui.ui.state.selectedRunId = 'known-fail';
    await ui.ui.renderRuns();
    assert.match(ui.doc.querySelector('#run-failure').textContent, /分析没有完成/);
    assert.doesNotMatch(ui.doc.querySelector('#run-failure').textContent, /不把它写成分析/);
  } finally { ui.dom.window.close(); }
});

test('the failure banner shows the run detail as text, never as markup (RUN-002 W22)', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=runs]');
    const detail = '交易员 · 调用失败：<img src=x onerror="window.__qa=1"><script>window.__qa=2</script><b>bold</b>';
    ui.routes['/api/runs'] = {
      run: {status: 'failed', kind: 'analysis', selections: {ticker: 'NVDA', date: '2026-10-01'}, detail,
            failure: {role: 'Trader', kind: 'other'}, checkpoint: {enabled: false, saved: null}},
      progress: {agents: []},
      result: {present: false},
    };
    await openRunFromList(ui);
    const banner = ui.doc.querySelector('#run-failure');
    assert.ok(banner, 'the failure banner is shown');
    const reason = banner.querySelector('code');
    assert.ok(reason, 'the detail is shown inside the banner reason');
    assert.equal(reason.textContent, detail);
    assert.equal(reason.children.length, 0);
    assert.equal(ui.doc.querySelector('#run-failure img, #run-failure script'), null);
    assert.equal(ui.dom.window.__qa, undefined);
  } finally { ui.dom.window.close(); }
});

test('without a saved checkpoint resume is disabled with a reason and 重新启动 is primary', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=runs]');
    ui.routes['/api/runs'] = failedRun({saved: false});
    await openRunFromList(ui, 'failed-fixture');
    const resume = ui.doc.querySelector('#resume-run');
    assert.equal(resume.disabled, true);
    assert.equal(resume.getAttribute('aria-describedby'), 'resume-note');
    assert.match(ui.doc.querySelector('#resume-note').textContent, /本次未保存断点，只能重新启动/);
    const restart = ui.doc.querySelector('#run-failure #restart-run');
    assert.ok(restart.classList.contains('button--primary'));
    ui.routes['/api/runs/restart'] = {zh: '已重新启动'};
    ui.routes['/api/runs'] = {run: null, progress: {}};
    await ui.click('#run-failure #restart-run');
    assert.deepEqual(JSON.parse(ui.calls.find((item) => item.path === '/api/runs/restart').body), {id: 'failed-fixture'}, 'restart names the run on screen');
  } finally { ui.dom.window.close(); }
});

test('a stopping run does not call an in-progress role still working', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'stop', status: 'stopping', kind: 'analysis', selections: {ticker: 'AAPL', date: '2026-11-11'}, started_at: '2026-11-11T10:00:00'},
    progress: {agents: [
      {team: 'Analyst Team', team_zh: '分析师团队', agent: 'Market Analyst', zh: '市场分析师', status: 'in_progress', status_zh: '进行中', started: '10:01'},
    ], timeline: []},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'stop');
    const pipe = ui.doc.querySelector('#team-pipeline').textContent;
    assert.match(pipe, /停止中/);
    assert.match(pipe, /不把还在日志里的角色写成仍在工作/);
    const note = ui.doc.querySelector('#team-pipeline .now');
    assert.equal(note.textContent, '停止中：不把还在日志里的角色写成仍在工作。');
    assert.match(note.innerHTML, /停止中：<\/b><br>不把还在日志里的角色<br>写成仍在工作。/);
    assert.doesNotMatch(pipe, /正在工作|正在做|进行中/);
    assert.equal(ui.doc.querySelector('#team-pipeline .pill').textContent, '停止中');
  } finally { ui.dom.window.close(); }
});

test('a running team card waiting for the next role does not split 角色', async () => {
  const done = {team: 'Analyst Team', team_zh: '分析师团队', agent: 'Market Analyst', zh: '市场分析师', status: 'completed', status_zh: '完成'};
  const pending = {team: 'Analyst Team', team_zh: '分析师团队', agent: 'News Analyst', zh: '新闻分析师', status: 'pending', status_zh: '等待'};
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'an-next', status: 'running', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA', analysts: ['market', 'news']}},
    runs: [{id: 'an-next', status: 'running', kind: 'analysis', ticker: 'NVDA'}],
    progress: {agents: [done, pending], timeline: []},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'an-next');
    const note = ui.doc.querySelector('#team-pipeline .now');
    assert.equal(note.textContent, '正在做：等待本组下一个角色开始。');
    assert.match(note.innerHTML, /正在做：<\/b><br>等待本组下一个角色开始。/);
    assert.doesNotMatch(note.textContent, /正在工作|仍在工作/);
  } finally { ui.dom.window.close(); }
});

test('a finished run does not call a leftover in-progress role still working', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'done', status: 'finished', kind: 'analysis', selections: {ticker: 'AAPL', date: '2026-11-11'}, started_at: '2026-11-11T10:00:00'},
    progress: {agents: [
      {team: 'Analyst Team', team_zh: '分析师团队', agent: 'Market Analyst', zh: '市场分析师', status: 'in_progress', status_zh: '进行中'},
    ], timeline: []},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'done');
    const pipe = ui.doc.querySelector('#team-pipeline').textContent;
    assert.match(pipe, /未写完/);
    assert.match(pipe, /不把这个角色写成正在工作/);
    const note = ui.doc.querySelector('#team-pipeline .now');
    assert.equal(note.textContent, '未写完：进程已经不在运行，不把这个角色写成正在工作。');
    assert.match(note.innerHTML, /未写完：<\/b><br>进程已经不在运行，<br>不把这个角色写成正在工作。/);
    assert.doesNotMatch(pipe, /正在做|进行中/);
    assert.equal(ui.doc.querySelector('#team-pipeline .pill').textContent, '未完成');
  } finally { ui.dom.window.close(); }
});

test('an ended run without a finish time is not timed until now', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'ended', status: 'failed', kind: 'analysis', selections: {ticker: '600519', date: '2026-11-11'}, started_at: '2026-11-10T10:00:00'},
    progress: {agents: [], timeline: []},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'ended');
    assert.equal(ui.doc.querySelector('#run-elapsed').textContent, '—');
    assert.match(ui.doc.querySelector('#missing-end').textContent, /没有结束时间，不按当前时间计算已用时间/);
    assert.match(ui.doc.querySelector('#page').textContent, /失败/);
    assert.doesNotMatch(ui.doc.querySelector('#page').textContent, /小时|分 \d+ 秒/);
  } finally { ui.dom.window.close(); }
});

test('a running task still counts elapsed time', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'live', status: 'running', kind: 'analysis', selections: {ticker: '600519', date: '2026-11-11'}, started_at: '2026-11-11T10:00:00'},
    progress: {agents: [], timeline: []},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'live');
    assert.match(ui.doc.querySelector('#run-elapsed').textContent, /小时|分/);
    assert.equal(ui.doc.querySelector('#missing-end'), null);
  } finally { ui.dom.window.close(); }
});

test('an unknown role status is not called in progress or complete', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'role-unknown', status: 'running', kind: 'analysis', selections: {ticker: '600519', date: '2026-11-11'}, started_at: '2026-11-11T10:00:00'},
    progress: {agents: [
      {team: 'Analyst Team', team_zh: '分析师团队', agent: 'Market Analyst', zh: '市场分析师', status: 'retrying', status_zh: '进行中'},
    ], timeline: [{time: '10:02', zh: '市场分析师', status: 'retrying', status_zh: '进行中'}]},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'role-unknown');
    const pipe = ui.doc.querySelector('#team-pipeline').textContent;
    assert.match(pipe, /状态未知/);
    assert.doesNotMatch(pipe, /retrying|进行中|完成/);
    assert.equal(ui.doc.querySelector('#team-pipeline .pill').textContent, '状态未知');
    assert.match(ui.doc.querySelector('#unknown-role-status').textContent, /不把它写成进行中或已完成/);
    assert.match(ui.doc.querySelector('#run-feed').textContent, /状态未知/);
    assert.doesNotMatch(ui.doc.querySelector('#run-feed').textContent, /retrying|进行中|开始工作/);
  } finally { ui.dom.window.close(); }
});

test('a cost role without an effort field is not called effortless', async () => {
  const ui = await setup({'/api/cost': {runs: [{
    id: 'c-effort', ticker: '600519', date: '2026-11-11',
    api_text: '没有 API 用量', match_text: '没有供应商用量，不估算。',
    roles: [
      {role: 'Market Analyst', zh: '市场分析师', tier: 'quick', provider: 'openai', model: 'gpt-4.1-mini', billing: 'api'},
      {role: 'Trader', zh: '交易员', tier: 'deep', provider: 'grok', model: 'grok-4', billing: 'subscription', effort_label: '无独立强度'},
    ],
  }], note: ''}});
  try {
    await ui.click('[data-page=cost]');
    for (let i = 0; i < 6; i++) await settle();
    const tiers = ui.doc.querySelector('.cost-tiers').textContent;
    assert.match(tiers, /强度未知，不写成无独立强度/);
    assert.match(tiers, /无独立强度/);
    assert.doesNotMatch(tiers, /gpt-4\.1-mini · 无独立强度/);
  } finally { ui.dom.window.close(); }
});

test('a cost row without a usage label is not called unused', async () => {
  const ui = await setup({'/api/cost': {runs: [{
    id: 'c-unknown', ticker: '600519', date: '2026-11-11',
    api_text: '没有供应商用量，不估算。', match_text: '没有供应商用量，不估算。',
    roles: [
      {role: 'Market Analyst', zh: '市场分析师', tier: 'quick', provider: 'openai', model: 'gpt-4.1-mini', billing: 'api', prompt_count: 10, completion_count: null},
      {role: 'Trader', zh: '交易员', tier: 'deep', provider: 'openai', model: 'gpt-4.1', billing: 'api', usage_label: '未调用', cost_text: '未调用'},
    ],
  }], note: ''}});
  try {
    await ui.click('[data-page=cost]');
    for (let i = 0; i < 6; i++) await settle();
    const card = ui.doc.querySelector('.cost-card').textContent;
    assert.match(card, /用量未知，不写成未调用/);
    assert.match(card, /未调用/);
    assert.doesNotMatch(ui.doc.querySelector('.cost-roles li').textContent, /^市场分析师未调用/);
  } finally { ui.dom.window.close(); }
});

test('the cost page names the price file and says when calls returned no usage', async () => {
  const ui = await setup({'/api/cost': {prices_path: '/home/me/.tradingagents/cost-prices.json', prices_models: 0, runs: [{
    id: 'c-usage', ticker: 'NVDA', date: '2026-11-13', usage_unknown: 2,
    api_text: '不显示 API 合计：没有供应商合计。', match_text: '没有供应商合计，不把调用合计当成已核对。',
    roles: [{role: 'Market Analyst', zh: '市场分析师', tier: 'quick', provider: 'deepseek', model: 'deepseek-chat', billing: 'api', usage_label: '用量未归集', cost_text: '价格未知'}],
  }], note: ''}});
  try {
    await ui.click('[data-page=cost]');
    for (let i = 0; i < 6; i++) await settle();
    const prices = ui.doc.querySelector('#cost-prices');
    assert.equal(prices.hidden, false);
    assert.match(prices.textContent, /没有读到单价，金额显示「价格未知」。单价文件：\/home\/me\/\.tradingagents\/cost-prices\.json/);
    assert.equal(ui.doc.querySelector('.cost-unknown').textContent, '有 2 次调用没有返回用量，合计不完整。');
  } finally { ui.dom.window.close(); }
  const priced = await setup({'/api/cost': {prices_path: '/p/cost-prices.json', prices_models: 3, runs: [], note: ''}});
  try {
    await priced.click('[data-page=cost]');
    for (let i = 0; i < 6; i++) await settle();
    assert.equal(priced.doc.querySelector('#cost-prices').textContent, '已读取 3 个模型的单价：/p/cost-prices.json');
  } finally { priced.dom.window.close(); }
  const none = await setup({'/api/cost': {runs: [], note: ''}});
  try {
    await none.click('[data-page=cost]');
    for (let i = 0; i < 6; i++) await settle();
    assert.equal(none.doc.querySelector('#cost-prices').hidden, true, 'an older backend without prices_path shows nothing');
  } finally { none.dom.window.close(); }
});

test('the cost page shows cache-priced roles and does not add mixed currencies', async () => {
  const role = (en, zh, extra) => ({role: en, zh, tier: 'quick', provider: 'deepseek', model: 'deepseek-chat', billing: 'api', ...extra});
  const ui = await setup({'/api/cost': {prices_path: '/p/cost-prices.json', prices_models: 3, prices_unreadable: false, runs: [
    {id: 'cny', ticker: '600519', date: '2026-11-14', prompt_count: 4000, completion_count: 400, api_text: '0.01455 CNY', match_text: '与供应商计数一致，容差 0。',
      roles: [role('Market Analyst', '市场分析师', {prompt_count: 2000, completion_count: 200, cache_read: 1500, cost_text: '0.00335 CNY', price_note: ''}),
              role('Portfolio Manager', '组合经理', {tier: 'deep', model: 'deepseek-reasoner', prompt_count: 2000, completion_count: 200, cache_read: 1500, cost_text: '0.0112 CNY', price_note: '缓存命中按输入单价计'})]},
    {id: 'mixed', ticker: 'NVDA', date: '2026-11-14', prompt_count: 4000, completion_count: 400, api_text: '币种不一致，不计入合计', match_text: '与供应商计数一致，容差 0。',
      roles: [role('Market Analyst', '市场分析师', {provider: 'openai', model: 'gpt-4.1-mini', prompt_count: 2000, completion_count: 200, cost_text: '0.00112 USD', price_note: ''}),
              role('Portfolio Manager', '组合经理', {tier: 'deep', model: 'deepseek-reasoner', prompt_count: 2000, completion_count: 200, cost_text: '0.0112 CNY', price_note: ''})]},
  ], note: ''}});
  try {
    await ui.click('[data-page=cost]');
    for (let i = 0; i < 6; i++) await settle();
    const [cny, mixed] = [...ui.doc.querySelectorAll('.cost-card')];
    assert.equal(cny.querySelector('.cost-api').textContent, 'API 0.01455 CNY');
    const notes = [...cny.querySelectorAll('.cost-note')].map((el) => el.textContent);
    assert.deepEqual(notes, ['缓存命中按输入单价计'], 'only the role without a cache price is marked');
    assert.match(cny.querySelectorAll('.cost-roles li')[1].textContent, /组合经理提示 2000 · 完成 2000\.0112 CNY缓存命中按输入单价计/);
    assert.equal(mixed.querySelector('.cost-api').textContent, '币种不一致，不计入合计');
    assert.match(mixed.textContent, /0\.00112 USD/);
    assert.match(mixed.textContent, /0\.0112 CNY/);
  } finally { ui.dom.window.close(); }
});

test('the cost page opens with an unreadable price file and says so with its path', async () => {
  const ui = await setup({'/api/cost': {prices_path: '/home/me/.tradingagents/cost-prices.json', prices_models: 0, prices_unreadable: true, runs: [{
    id: 'c-broken', ticker: 'AAPL', date: '2026-11-12', prompt_count: 100, completion_count: 10,
    api_text: '价格未知', match_text: '与供应商计数一致，容差 0。',
    roles: [{role: 'Market Analyst', zh: '市场分析师', tier: 'quick', provider: 'deepseek', model: 'deepseek-chat', billing: 'api', prompt_count: 100, completion_count: 10, cost_text: '价格未知'}],
  }], note: ''}});
  try {
    await ui.click('[data-page=cost]');
    for (let i = 0; i < 6; i++) await settle();
    const prices = ui.doc.querySelector('#cost-prices');
    assert.equal(prices.hidden, false);
    assert.equal(prices.textContent, '单价文件无法读取，金额显示「价格未知」。单价文件：/home/me/.tradingagents/cost-prices.json');
    assert.equal(prices.querySelector('.cost-path').textContent, '/home/me/.tradingagents/cost-prices.json');
    assert.equal(ui.doc.querySelector('#cost-error').hidden, true, 'the page opens normally');
    assert.match(ui.doc.querySelector('.cost-card').textContent, /市场分析师提示 100 · 完成 10价格未知/);
  } finally { ui.dom.window.close(); }
});

test('the cost page words the roles without a token row by why they have none', async () => {
  const role = (en, zh, extra) => ({role: en, zh, tier: 'quick', provider: 'deepseek', model: 'deepseek-chat', billing: 'api', prompt_count: null, completion_count: null, ...extra});
  const names = [['Market Analyst', '市场分析师'], ['Trader', '交易员'], ['Memory Log', '记忆日志']];
  const ui = await setup({'/api/cost': {runs: [
    {id: 'pending', ticker: 'NVDA', date: '2026-11-13', prompt_count: 300, completion_count: 30, api_text: '不显示 API 合计：没有供应商合计。', match_text: '没有供应商合计，不把调用合计当成已核对。',
      roles: names.map(([en, zh]) => role(en, zh, {usage_label: '用量未归集', cost_text: '用量未归集'}))},
    {id: 'none', ticker: 'AAPL', date: '2026-11-12', prompt_count: null, completion_count: null, api_text: '没有 API 用量', match_text: '没有供应商用量，不估算。',
      roles: names.map(([en, zh]) => role(en, zh, {usage_label: '未调用', cost_text: '未调用'}))},
    {id: 'one-idle', ticker: 'MSFT', date: '2026-11-11', prompt_count: 300, completion_count: 30, api_text: '0.0001 USD', match_text: '与供应商计数一致，容差 0。',
      roles: [role('Market Analyst', '市场分析师', {prompt_count: 200, completion_count: 20, cost_text: '0.0001 USD'}), role('Trader', '交易员', {prompt_count: 100, completion_count: 10, cost_text: '0.0000 USD'}), role('Memory Log', '记忆日志', {usage_label: '未调用', cost_text: '未调用'})]},
    {id: 'two-idle', ticker: 'TSLA', date: '2026-11-10', prompt_count: 200, completion_count: 20, api_text: '0.0001 USD', match_text: '与供应商计数一致，容差 0。',
      roles: [role('Market Analyst', '市场分析师', {prompt_count: 200, completion_count: 20, cost_text: '0.0001 USD'}), role('Trader', '交易员', {usage_label: '未调用', cost_text: '未调用'}), role('Memory Log', '记忆日志', {usage_label: '未调用', cost_text: '未调用'})]},
  ], note: ''}});
  try {
    await ui.click('[data-page=cost]');
    for (let i = 0; i < 6; i++) await settle();
    const rest = [...ui.doc.querySelectorAll('.cost-card')].map((card) => card.querySelector('.cost-rest').textContent);
    assert.deepEqual(rest, ['分角色用量：核对供应商合计后再显示（共 3 个角色）', '这次运行没有用量记录', '记忆日志未调用', '其余 2 个角色未调用']);
    assert.doesNotMatch(ui.doc.querySelector('#cost-list').textContent, /用量未归集/);
  } finally { ui.dom.window.close(); }
});

test('a cost row without billing is not called API', async () => {
  const ui = await setup({
    '/api/cost': {runs: [{id: 'c1', ticker: 'AAPL', date: '2026-11-11', api_text: '计费方式未知，不把它当成 API 用量。', match_text: '没有供应商用量，不估算。', roles: [
      {role: 'Market Analyst', zh: '市场分析师', tier: 'quick', provider: 'openai', model: 'gpt-4.1-mini', billing: null, prompt_count: 20, completion_count: 4, cost_text: '计费方式未知'},
    ]}], note: ''},
  });
  try {
    await ui.click('[data-page=cost]');
    for (let i = 0; i < 4; i++) await settle();
    const card = ui.doc.querySelector('.cost-card').textContent;
    assert.match(card, /计费方式未知/);
    assert.doesNotMatch(card, /API gpt| · API/);
  } finally { ui.dom.window.close(); }
});

test('an unrecognized cost tier is not called the quick model', async () => {
  const ui = await setup({'/api/cost': {runs: [{
    id: 'c-tier', ticker: 'AAA', date: '2026-11-12',
    api_text: '没有 API 用量', match_text: '没有供应商用量，不估算。',
    roles: [
      {role: 'Market Analyst', zh: '市场分析师', tier: 'index', provider: 'openai', model: 'unknown-model', billing: 'api'},
      {role: 'Research Manager', zh: '研究经理', tier: 'quick', provider: 'openai', model: 'quick-model', billing: 'subscription'},
      {role: 'Trader', zh: '交易员', tier: 'deep', provider: 'openai', model: 'deep-model', billing: 'api', effort_label: '无独立强度'},
      {role: 'News Analyst', zh: '新闻分析师', provider: 'openai', model: 'missing-model', billing: 'api'},
    ],
  }], note: ''}});
  try {
    await ui.click('[data-page=cost]');
    for (let i = 0; i < 6; i++) await settle();
    const part = (model) => [...ui.doc.querySelectorAll('.cost-tier')].map((el) => el.textContent).find((item) => item.includes(model)) || "";
    assert.match(part("unknown-model"), /^模型层级未知，不把它写成快模型 openai \/ unknown-model/);
    assert.match(part("missing-model"), /^模型层级未知，不把它写成快模型 openai \/ missing-model/);
    assert.match(part("quick-model"), /^快模型 openai \/ quick-model/);
    assert.match(part("deep-model"), /^深模型 openai \/ deep-model/);
    assert.doesNotMatch(part("unknown-model"), /^快模型 /);
    assert.doesNotMatch(part("missing-model"), /^快模型 /);
  } finally { ui.dom.window.close(); }
});

test('an unrecognized launch status is not called submitted', async () => {
  const ui = await setup({'/api/runs': (options) => options.method
    ? {id: 'run-retry', status: 'retrying', ticker: 'AAPL', date: '2026-11-11', selections: {asset_type: 'stock'}, ignored: []}
    : {run: null, progress: {}}});
  try {
    await fillToReview(ui);
    await ui.click('#run-analysis');
    await settleMany();
    const dialog = ui.doc.querySelector('#launch-dialog');
    assert.equal(ui.doc.querySelector('#launch-title').textContent, '状态未知');
    assert.match(ui.doc.querySelector('#launch-lead').textContent, /不把它写成已提交或已启动/);
    assert.doesNotMatch(dialog.textContent, /已提交任务|任务已提交|已启动分析|已加入队列/);
    assert.equal(dialog.classList.contains('launch-dialog--unknown'), true);
    assert.equal(dialog.classList.contains('launch-dialog--ok'), false);
    assert.doesNotMatch(dialog.querySelector('.launch-mark').textContent, /✓|!/);
    assert.match(ui.doc.querySelector('#launch-run-info').textContent, /run-retry/);
  } finally { ui.dom.window.close(); }
});

test('a starting launch is not called already running', async () => {
  const ui = await setup({'/api/runs': (options) => options.method ? {id: 'start-1', status: 'starting', ticker: 'AAPL'} : {run: null, progress: {}}});
  try {
    await ui.click('[data-page=analyze]');
    await ui.click('.fw[data-framework=tradingagents]');
    ui.fill('#ticker', 'AAPL');
    ui.fill('#date', '2026-11-11');
    await ui.click('#step-next');
    ui.ui.state.form.analysts = ['market'];
    await ui.click('#step-next');
    ui.fill('#quick-model', 'fixture-model');
    ui.fill('#deep-model', 'fixture-model');
    await ui.click('#step-next');
    await ui.click('#run-analysis');
    for (let i = 0; i < 6; i++) await settle();
    assert.equal(ui.doc.querySelector('#launch-title').textContent, '正在启动');
    assert.equal(ui.doc.querySelector('#launch-lead').textContent, '任务正在启动，还没有进入运行。可以继续准备下一个。');
    assert.equal(ui.doc.querySelector('#launch-lead').innerHTML, '任务正在启动，<br class="notice-break">还没有进入运行。<br class="notice-break">可以继续准备下一个。');
    assert.doesNotMatch(ui.doc.querySelector('#launch-dialog').textContent, /已在后台运行|已启动分析/);
    assert.equal(ui.doc.querySelector('#launch-dialog').classList.contains('launch-dialog--wait'), true);
    assert.doesNotMatch(ui.doc.querySelector('.launch-mark').textContent, /✓/);
  } finally { ui.dom.window.close(); }
});

test('a REVIEW result is not shown as a tradeable rating', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'rev', status: 'finished', kind: 'analysis', selections: {ticker: 'AAPL', date: '2026-11-11'}},
    progress: {agents: []},
    result: {present: true, rating: 'REVIEW', review: 'needs review', decision: '决定原文没有可交易评级。'},
  }});
  try {
    await openRunFromList(ui, 'rev');
    const panel = ui.doc.querySelector('#run-result');
    assert.equal(panel.querySelectorAll('.metric-label')[1].textContent, '复核');
    assert.equal(panel.querySelectorAll('.metric-card strong')[0].textContent, '需复核');
    assert.match(panel.textContent, /未能从最终决定读出可交易评级/);
    assert.equal(panel.textContent.includes('REVIEW'), false);
    assert.doesNotMatch(panel.querySelectorAll('.metric-card')[1].textContent, /评级/);
  } finally { ui.dom.window.close(); }
});

test('a stopping backtest progress row is not called in progress', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'bt-live', status: 'stopping', kind: 'backtest', ticker: 'AAPL', date: '2026-11-11', selections: {ticker: 'AAPL'}},
    progress: {agents: [
      {team: 'Analyst Team', team_zh: '分析师团队', agent: 'Market Analyst', zh: '市场分析师', status: 'in_progress', status_zh: '进行中'},
    ]},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'bt-live');
    const table = ui.doc.querySelector('#run-detail table');
    assert.match(table.textContent, /停止中/);
    assert.doesNotMatch(table.textContent, /进行中/);
    assert.match(ui.doc.querySelector('#current-task').textContent, /停止中/);
    assert.doesNotMatch(ui.doc.querySelector('#current-task').textContent, /进行中/);
    assert.equal(ui.doc.querySelector('#restart-run'), null);
  } finally { ui.dom.window.close(); }
});

test('a stopping backtest is not called still running, and a queued backtest is not finished', async () => {
  const stopping = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'bt-stop', status: 'stopping', kind: 'backtest', ticker: 'AAPL', date: '2026-11-11', selections: {ticker: 'AAPL'}},
    progress: {}, result: {present: false},
  }});
  try {
    await openRunFromList(stopping, 'bt-stop');
    const next = stopping.doc.querySelector('#next-step').textContent;
    assert.match(next, /任务正在停止，不把它写成运行中，也还不是已结束。稍后刷新状态。/);
    assert.match(stopping.doc.querySelector('#next-step .notice').innerHTML, /任务正在停止，<br class="notice-break">不把它写成运行中，<br class="notice-break">也还不是已结束。<br class="notice-break">稍后刷新状态。/);
    assert.doesNotMatch(next, /仍在运行/);
    assert.equal(stopping.doc.querySelector('#restart-run'), null, 'a stopping backtest is not offered as a restart');
    assert.ok(stopping.doc.querySelector('#stop-run'));
    assert.match(stopping.doc.querySelector('#run-result').textContent, /还没有结果/);
    assert.doesNotMatch(stopping.doc.querySelector('#run-result').textContent, /完成后显示/);
  } finally { stopping.dom.window.close(); }
  const queued = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'bt-queue', status: 'queued', kind: 'backtest', ticker: 'AAPL', date: '2026-11-11', position: 1},
    progress: {}, result: {present: false},
  }});
  try {
    await openRunFromList(queued, 'bt-queue');
    const next = queued.doc.querySelector('#next-step').textContent;
    assert.match(next, /任务还在排队，没有开始运行，也不是已结束。/);
    assert.match(queued.doc.querySelector('#next-step .notice').innerHTML, /任务还在排队，<br class="notice-break">没有开始运行，<br class="notice-break">也不是已结束。/);
    assert.doesNotMatch(next, /仍在运行|分析/);
    assert.equal(queued.doc.querySelector('#restart-run'), null);
    assert.ok(queued.doc.querySelector('#cancel-queued'));
  } finally { queued.dom.window.close(); }
});

test('the kline page description does not split 实际K线 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('.nav-item[data-page=kline]');
    const desc = ui.doc.querySelector('.page-description');
    assert.equal(desc.textContent, '日线请求近 1 年，短周期更短，图上区间以实际K线为准。股票与贵金属沿用分析数据源；加密货币优先 OKX，其次 Coinbase。北交所仅新浪。');
    assert.match(desc.innerHTML, /日线请求近 1 年，<br class="notice-break">短周期更短，<br class="notice-break">图上区间以实际K线为准。<br class="notice-break">股票与贵金属沿用分析数据源；<br class="notice-break">加密货币优先 OKX，<br class="notice-break">其次 Coinbase。<br class="notice-break">北交所仅新浪。/);
    assert.doesNotMatch(desc.textContent, /Yahoo|实时行情/);
  } finally { ui.dom.window.close(); }
});

test('the compare page description does not split 卡片 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('.nav-item[data-page=compare]');
    const desc = ui.doc.querySelector('.page-description');
    assert.equal(desc.textContent, '并排看运行的评级和证据。左右是同一次时只显示一张卡片。回放只用左侧保存的提示词版本。');
    assert.match(desc.innerHTML, /并排看运行的评级和证据。<br class="notice-break">左右是同一次时只显示一张卡片。<br class="notice-break">回放只用左侧保存的提示词版本。/);
    assert.doesNotMatch(desc.textContent, /当前提示词|两次不同结果/);
  } finally { ui.dom.window.close(); }
});

test('the model page description does not split 思考强度 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('.nav-item[data-page=models]');
    const desc = ui.doc.querySelector('.page-description');
    assert.equal(desc.textContent, '两层可以分别选择已登录的订阅。快模型不是数据源，Grok 不会代替 yfinance 或 SEC。Grok 没有独立思考强度。');
    assert.match(desc.innerHTML, /已登录的订阅。<br class="notice-break">快模型不是数据源，<br class="notice-break">Grok 不会代替 yfinance 或 SEC。<br class="notice-break">Grok 没有独立思考强度。/);
    assert.doesNotMatch(desc.textContent, /快模型负责取数|Grok 搜索/);
  } finally { ui.dom.window.close(); }
});

test('the model source note does not split 订阅 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('.nav-item[data-page=models]');
    const note = ui.doc.querySelector('#model-source-note');
    assert.equal(note.textContent, '这里列出命令行供应商表里的全部供应商，不只订阅。下面的思考强度写入同一次分析，和启动分析「模型选择」一步是同一组字段。');
    assert.match(note.innerHTML, /全部供应商，<br class="notice-break">不只订阅。<br class="notice-break">下面的思考强度写入同一次分析，<br class="notice-break">和启动分析「模型选择」一步是同一组字段。/);
    assert.doesNotMatch(note.textContent, /不只订 阅|快模型负责取数/);
  } finally { ui.dom.window.close(); }
});

test('the cost rule does not split 账单 on a narrow screen', async () => {
  const ui = await setup({'/api/cost': {runs: []}});
  try {
    await ui.click('[data-page=cost]');
    const rule = ui.doc.querySelector('#cost-rule');
    assert.equal(rule.textContent, '单价来自本机 cost-prices.json，不是供应商账单。429 记为限流，不是预算超限。没有结构化字段时记为未知。本机调用时限不是供应商预算。');
    assert.match(rule.innerHTML, /cost-prices\.json，<br class="notice-break">不是供应商账单。<br class="notice-break">429 记为限流，<br class="notice-break">不是预算超限。<br class="notice-break">没有结构化字段时记为未知。<br class="notice-break">本机调用时限不是供应商预算。/);
    assert.doesNotMatch(rule.textContent, /供应商账单已核对|预算超限。本机/);
    const desc = ui.doc.querySelector('.page-description');
    assert.equal(desc.textContent, '按这次运行冻结的角色模型统计。订阅和 API 分开，没有单价不估算。');
    assert.match(desc.innerHTML, /角色模型统计。<br class="notice-break">订阅和 API 分开，<br class="notice-break">没有单价不估算。/);
  } finally { ui.dom.window.close(); }
});

test('a running task shows the team pipeline, the current team, the feed and a stop button', async () => {
  const ui = await setup({'/api/logs': {lines: []}});
  try {
    ui.routes['/api/runs'] = {
      run: {id: 'running', status: 'running', kind: 'analysis', selections: {ticker: 'NVDA', date: '2026-10-01'}, started_at: '2026-10-30T21:00:00'},
      progress: {agents: [
        {team: 'Analyst Team', team_zh: '分析师团队', agent: 'Market Analyst', zh: '市场分析师', status: 'completed', status_zh: '完成', elapsed: '1 分 02 秒'},
        {team: 'Research Team', team_zh: '研究团队', agent: 'Bull Researcher', zh: '多头研究员', status: 'in_progress', status_zh: '进行中', started: '21:03'},
        {team: 'Trading Team', team_zh: '交易团队', agent: 'Trader', zh: '交易员', status: 'pending', status_zh: '等待'},
      ], timeline: [
        {agent: 'Bull Researcher', zh: '多头研究员', status: 'in_progress', time: '21:03:00'},
        {agent: 'Market Analyst', zh: '市场分析师', status: 'completed', time: '21:02:58'},
      ]},
    };
    await openRunFromList(ui, 'running');
    assert.ok(ui.doc.querySelector('#team-pipeline'));
    const current = ui.doc.querySelector('.team.cur');
    assert.equal(current.dataset.team, 'Research Team');
    assert.match(current.textContent, /正在做：多头研究员（21:03 开始）正在工作/);
    const working = current.querySelector('.now');
    assert.equal(working.textContent, '正在做：多头研究员（21:03 开始）正在工作。');
    assert.match(working.innerHTML, /正在做：<\/b><br>多头研究员<br>（21:03 开始）<br>正在工作。/);
    assert.ok(ui.doc.querySelector('.team.dim[data-team="Trading Team"]'));
    const feed = [...ui.doc.querySelectorAll('#run-feed .ev')].map((ev) => ev.textContent);
    assert.equal(feed.length, 2);
    assert.match(feed[0], /21:03:00.*多头研究员.*开始工作/);
    assert.ok(ui.doc.querySelector('#stop-run'));
    assert.equal(ui.doc.querySelector('#run-failure'), null);
    assert.equal(ui.doc.querySelector('#run-technical-details').open, false);
    assert.match(ui.doc.querySelector('#run-technical-details summary').textContent, /技术信息与完整日志/);
  } finally { ui.dom.window.close(); }
});

async function fillToReview(ui) {
  ui.fill('#ticker', 'btc-usd'); ui.fill('#date', '2026-10-30');
  await ui.click('#step-next');
  ui.ui.state.form.analysts = ['market'];
  await ui.click('#step-next');
  ui.fill('#quick-model', 'fixture-model'); ui.fill('#deep-model', 'fixture-model');
  await ui.click('#step-next');
  assert.equal(ui.ui.state.step, 4);
}

const settleMany = async (count = 8) => { for (let i = 0; i < count; i++) await settle(); };

test('a successful launch shows the real run id, resets the wizard to step 1 and offers 查看状态 / 再启动一个 (WEBUI-006)', async () => {
  const ui = await setup({'/api/runs': (options) => options.method
    ? {id: '20261031_141327_59ee0ec2', status: 'running', ticker: 'BTC-USD', date: '2026-10-30', started_at: '2026-10-31T14:13:27', selections: {asset_type: 'crypto'}, ignored: []}
    : {run: null, progress: {}}});
  try {
    const {doc} = ui;
    await fillToReview(ui);
    ui.ui.state.form.clear_checkpoints = true;
    ui.ui.state.portfolioText = '{"positions": []}';
    ui.ui.state.holding = {quantity: '1', average_price: '62000'};
    await ui.click('#run-analysis');
    await settleMany();
    const dialog = doc.querySelector('#launch-dialog');
    assert.ok(dialog, 'the result dialog opens');
    assert.equal(dialog.getAttribute('aria-modal'), 'true');
    assert.match(doc.querySelector('#launch-title').textContent, /已启动分析/);
    assert.equal(dialog.classList.contains('launch-dialog--ok'), true);
    assert.equal(dialog.querySelector('.launch-mark').textContent, '✓');
    const info = doc.querySelector('#launch-run-info').textContent;
    assert.match(info, /20261031_141327_59ee0ec2/, 'the run id is the one the API returned');
    assert.match(info, /BTC-USD · 加密货币/);
    assert.match(info, /2026-10-30/);
    assert.equal(doc.activeElement?.id, 'launch-open-runs', 'focus starts on 查看状态');
    // The wizard behind the dialog has started over with fresh values.
    assert.equal(ui.ui.state.step, 1);
    assert.equal(ui.ui.state.page, 'analyze');
    assert.equal(doc.querySelector('#ticker').value, '');
    assert.equal(doc.querySelector('[data-step-panel="1"]').hidden, false);
    assert.equal(ui.ui.state.form.ticker, '');
    assert.equal(ui.ui.state.form.clear_checkpoints, undefined, 'a one-off clear does not carry to the next run');
    assert.equal(ui.ui.state.portfolioText, '');
    assert.equal(Object.keys(ui.ui.state.holding).length, 0);
    assert.equal(doc.querySelector('#toast-host .toast'), null, 'no toast; the dialog replaces it');
    await ui.click('#launch-another');
    assert.equal(doc.querySelector('#launch-dialog'), null);
    assert.equal(doc.activeElement?.id, 'ticker', '再启动一个 puts the cursor in 标的代码');
  } finally { ui.dom.window.close(); }
});

test('查看状态 in the success dialog opens the status page (WEBUI-006)', async () => {
  const ui = await setup({'/api/runs': (options) => options.method ? {id: 'fixture-run', ticker: 'BTC-USD'} : {run: null, progress: {}}});
  try {
    await fillToReview(ui);
    await ui.click('#run-analysis');
    await settleMany();
    await ui.click('#launch-open-runs');
    await settleMany();
    assert.equal(ui.doc.querySelector('#launch-dialog'), null);
    assert.equal(ui.ui.state.page, 'runs');
  } finally { ui.dom.window.close(); }
});

test('a refused launch says why in a dialog and keeps every field as filled (WEBUI-006)', async () => {
  const reason = '供应商 openai 需要 OPENAI_API_KEY。请在账户页保存，不要把订阅令牌填到这里。';
  const ui = await setup({'/api/runs': (options) => options.method ? {__status: 400, body: {error: reason}} : {run: null, progress: {}}});
  try {
    const {doc} = ui;
    await fillToReview(ui);
    ui.ui.state.portfolioText = '{"positions": []}';
    await ui.click('#run-analysis');
    await settleMany();
    const dialog = doc.querySelector('#launch-dialog');
    assert.ok(dialog);
    assert.equal(dialog.getAttribute('role'), 'alertdialog');
    assert.match(doc.querySelector('#launch-title').textContent, /启动失败/);
    assert.equal(doc.querySelector('#launch-error').textContent, reason);
    assert.match(doc.querySelector('#launch-fail-info').textContent, /BTC-USD · 2026-10-30/);
    assert.match(dialog.textContent, /设置都保留着/);
    assert.equal(doc.querySelector('.launch-hint').textContent, '你填写的设置都保留着，修改后可以直接再次启动。');
    assert.equal(doc.querySelector('.launch-hint').innerHTML, '你填写的设置都保留着，<br class="notice-break">修改后可以直接再次启动。');
    assert.ok(doc.querySelector('#launch-open-accounts'), 'a key or sign-in problem offers 去账户页');
    assert.equal(ui.ui.state.step, 4, 'a failure does not reset the wizard');
    assert.equal(doc.querySelector('[data-step-panel="4"]').hidden, false);
    assert.equal(doc.querySelector('#ticker').value, 'btc-usd');
    assert.equal(doc.querySelector('#quick-model').value, 'fixture-model');
    assert.equal(ui.ui.state.portfolioText, '{"positions": []}');
    assert.equal(doc.querySelector('#run-analysis').disabled, false, 'the launch button is usable again');
    assert.equal(doc.querySelector('#form-note').textContent, '', 'the result is in the dialog only, not repeated on the page');
    await ui.click('#launch-close');
    assert.equal(doc.querySelector('#launch-dialog'), null);
    assert.equal(doc.activeElement?.id, 'run-analysis', '返回修改 returns to the launch button');
    assert.equal(ui.ui.state.page, 'analyze');
  } finally { ui.dom.window.close(); }
});

test('a refusal that is not about a sign-in only offers 返回修改, and Escape closes it (WEBUI-006)', async () => {
  const ui = await setup({'/api/runs': (options) => options.method ? {__status: 409, body: {error: '<img src=x id=boom>已有控制台分析在运行。'}} : {run: null, progress: {}}});
  try {
    await fillToReview(ui);
    await ui.click('#run-analysis');
    await settleMany();
    assert.equal(ui.doc.querySelector('#launch-open-accounts'), null);
    assert.equal(ui.doc.querySelector('#boom'), null, 'the reason is shown as text, never as markup');
    assert.match(ui.doc.querySelector('#launch-error').textContent, /<img src=x id=boom>已有控制台分析在运行。/);
    assert.ok(ui.doc.querySelector('#launch-close').classList.contains('button--primary'));
    ui.doc.dispatchEvent(new ui.window.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
    await settleMany(2);
    assert.equal(ui.doc.querySelector('#launch-dialog'), null);
    assert.equal(ui.ui.state.step, 4);
    assert.equal(ui.doc.querySelector('#ticker').value, 'btc-usd');
  } finally { ui.dom.window.close(); }
});

test('step 4 has no divider lines above 上一步 / 检查配置 / 启动分析 (WEBUI-006)', async () => {
  const ui = await setup();
  try {
    await fillToReview(ui);
    const foot = ui.window.getComputedStyle(ui.doc.querySelector('.foot'));
    const summary = ui.window.getComputedStyle(ui.doc.querySelector('#analysis-summary'));
    assert.ok(foot.borderTopStyle === 'none' || parseFloat(foot.borderTopWidth) === 0, 'the button row has no top line');
    assert.ok(summary.borderBottomStyle === 'none' || parseFloat(summary.borderBottomWidth) === 0, 'the review has no bottom line');
    const css = readFileSync(resolve(root, 'webui/static/app.css'), 'utf8');
    assert.doesNotMatch(css, /(^|\n)\.foot\s*\{[^}]*border-top:\s*1px/);
    assert.doesNotMatch(css, /#analysis-summary\s*\{[^}]*border-bottom:\s*1px/);
  } finally { ui.dom.window.close(); }
});

test('查看断点 / 立即清除断点 keep a 12px gap above the output box, and the drawer is 800px wide (WEBUI-006)', async () => {
  const ui = await setup();
  try {
    await fillToReview(ui);
    await ui.click('#open-save-drawer');
    const list = ui.window.getComputedStyle(ui.doc.querySelector('#checkpoint-list'));
    assert.equal(list.marginTop, '12px', 'the grey box sits 12px below the buttons');
    const confirm = ui.doc.querySelector('#clear-checkpoints-confirm');
    confirm.hidden = false;
    assert.equal(ui.window.getComputedStyle(confirm).marginTop, '12px', 'the confirm row also keeps its distance');
    const css = readFileSync(resolve(root, 'webui/static/app.css'), 'utf8');
    assert.match(css, /\.provider-drawer\.save-drawer\s*\{[^}]*width:\s*min\(800px,\s*92vw\)/);
    assert.match(css, /@media \(max-width: 600px\)\s*\{[^@]*\.provider-drawer\.save-drawer\s*\{\s*width:\s*100%;/, 'phones keep a full-width drawer');
  } finally { ui.dom.window.close(); }
});

test('checkpoints are on by default in the 保存与续跑 drawer (RUN-002)', async () => {
  const ui = await setup();
  try {
    assert.notEqual(ui.ui.readForm().checkpoint, false);
    ui.ui.openSaveDrawer(ui.doc.querySelector('#step-next'));
    for (let i = 0; i < 4; i++) await settle();
    const toggle = ui.doc.querySelector('.save-drawer [data-toggle=checkpoint]');
    assert.ok(toggle, 'the checkpoint switch is in the drawer');
    assert.equal(toggle.getAttribute('role'), 'switch');
    assert.equal(toggle.getAttribute('aria-checked'), 'true');
    assert.equal(ui.doc.querySelector('.save-drawer [data-toggle=clear_checkpoints]').getAttribute('aria-checked'), 'false');
  } finally { ui.dom.window.close(); }
});

test('status API failure has a visible error and a working retry', async () => {
  const ui = await setup({'/api/runs': () => { throw new Error('服务暂时不可用'); }});
  try {
    await ui.click('[data-page=runs]');
    assert.match(ui.doc.querySelector('[role=alert]').textContent, /服务暂时不可用/);
    ui.routes['/api/runs'] = {run: null, progress: {}};
    await ui.click('#refresh-run');
    assert.match(ui.visibleText(), /还没有任务/);
  } finally { ui.dom.window.close(); }
});

test('refreshing the same task preserves expanded diagnostics and the chosen log length', async () => {
  const ui = await setup({
    '/api/runs': {run: {id: 'running-fixture', status: 'running', selections: {ticker: 'NVDA'}}, progress: {}},
    '/api/logs': {lines: ['fixture log']},
  });
  try {
    await openRunFromList(ui, 'running-fixture');
    ui.doc.querySelector('#run-technical-details').open = true;
    ui.doc.querySelector('#run-log-details').open = true;
    ui.fill('#log-lines', '250');
    ui.doc.querySelector('#log-lines').focus();
    ui.doc.querySelector('#log-lines').setSelectionRange(2, 2);
    await ui.ui.renderRuns();
    assert.equal(ui.doc.querySelector('#run-technical-details').open, true);
    assert.equal(ui.doc.querySelector('#run-log-details').open, true);
    assert.equal(ui.doc.querySelector('#log-lines').value, '250');
    assert.equal(ui.doc.activeElement.id, 'log-lines');
    assert.equal(ui.doc.activeElement.selectionStart, 2);
    ui.routes['/api/runs'] = {run: {id: 'another-task', status: 'running'}, progress: {}};
    await ui.ui.renderRuns();
    assert.equal(ui.doc.querySelector('#run-technical-details').open, false);
    assert.equal(ui.doc.querySelector('#log-lines').value, '40');
    for (let i = 0; i < 4; i++) await settle();
  } finally { ui.dom.window.close(); }
});

for (const [page, path] of [['accounts', '/api/auth/status'], ['backtest', '/api/backtests']]) {
  test(`a late ${page} failure cannot replace the analysis guide after navigation`, async () => {
    const ui = await setup();
    let reject;
    try {
      ui.routes[path] = () => new Promise((resolve, fail) => { reject = fail; });
      await ui.click(`[data-page=${page}]`);
      await ui.click('[data-page=analyze]');
      assert.ok(ui.doc.querySelector('#fw-grid'), '启动分析 opens on the framework cards (WEBUI-008)');
      await ui.click('.fw[data-framework=tradingagents]');
      reject(new Error('late fixture failure'));
      for (let i = 0; i < 4; i++) await settle();
      assert.ok(ui.doc.querySelector('#analysis-form'));
      assert.doesNotMatch(ui.visibleText(), /late fixture failure/);
    } finally { ui.dom.window.close(); }
  });
}

const REPORT_DIR = '/tmp/results/reports';
const reportRows = [
  {name: 'NVDA_20261029_211200', html: `${REPORT_DIR}/NVDA_20261029_211200/complete_report.html`, markdown: `${REPORT_DIR}/NVDA_20261029_211200/complete_report.md`,
   ticker: 'NVDA', date: '2026-10-29', rating: 'Overweight', has_decision: true, generated: '2026-10-29 21:12:00', deep_model: 'gpt-6-sol', quick_model: 'grok-4.7', debate_rounds: 3},
  {name: 'AAPL_20261028_100000', html: `${REPORT_DIR}/AAPL_20261028_100000/complete_report.html`, markdown: null,
   ticker: 'AAPL', date: '2026-10-28', rating: 'Sell', has_decision: true, generated: '2026-10-28 10:00:00', debate_rounds: 1},
];
function outlineFor(path) {
  const ticker = path.includes('AAPL') ? 'AAPL' : 'NVDA';
  return {name: ticker, rating: ticker === 'AAPL' ? 'Sell' : 'Overweight', header: [`# Trading Analysis Report: ${ticker}`, '- Analysis date: 2026-10-29'], sections: [
    {heading: 'I. Analyst Team Reports', folder: '1_analysts', items: [
      {agent: 'Market Analyst', zh: '市场分析师', file: 'market.md', text: `## 技术面\n\n${ticker} 站上均线。\n\n| 指标 | 数值 |\n|---|---|\n| RSI | 61 |\n\n<script>alert(1)</script><img src=x onerror=alert(2)>`},
      {agent: 'News Analyst', zh: '新闻分析师', file: 'news.md', text: '- 新品发布\n- 监管消息'},
    ]},
    {heading: 'II. Research Team Decision', folder: '2_research', items: [
      {agent: 'Bull Researcher', zh: '多头研究员', file: 'bull.md', text: '看多理由'},
      {agent: 'Bear Researcher', zh: '空头研究员', file: 'bear.md', text: '看空理由'},
    ]},
    {heading: 'III. Trading Team Plan', folder: '3_trading', items: [{agent: 'Trader', zh: '交易员', file: 'trader.md', text: '分三批建仓'}]},
    {heading: 'IV. Risk Management Team Decision', folder: '4_risk', items: [{agent: 'Neutral Analyst', zh: '中性分析师', file: 'neutral.md', text: '按计划执行'}]},
    {heading: 'V. Portfolio Manager Decision', folder: '5_portfolio', items: [{agent: 'Portfolio Manager', zh: '投资组合经理', file: 'decision.md',
      text: `## 核心判断\n\n**Rating: Overweight**\n\n数据中心需求仍在上升，技术面站上 50 日均线，建议分批建仓，仓位不超过组合的 5%。`}]},
  ]};
}

async function openReports(overrides = {}) {
  const ui = await setup({'/api/reports': {reports: reportRows, live: [], directory: '/tmp/results'},
    '/api/reports/outline': () => outlineFor(ui.calls.at(-1).path), ...overrides});
  await ui.click('[data-page=reports]');
  for (let i = 0; i < 6; i++) await settle();
  return ui;
}

test('a report list row without a decision flag is not shown as a rating', async () => {
  const ui = await setup({'/api/reports': {reports: [
    {name: 'NVDA_known', html: '/tmp/results/reports/NVDA_known/complete_report.html', ticker: 'NVDA', date: '2026-10-29', rating: 'Overweight', has_decision: true},
    {name: '600519_unknown', html: '/tmp/results/reports/600519_unknown/complete_report.html', ticker: '600519', date: '2026-11-11', rating: 'Hold'},
  ], live: []}, '/api/reports/outline': {name: 'NVDA', rating: 'Overweight', header: [], sections: [
    {heading: 'V. Portfolio Manager Decision', folder: '5_portfolio', items: [{text: '**Rating: Overweight**\n\n保持增持。'}]},
  ]}});
  try {
    await ui.click('[data-page=reports]');
    for (let i = 0; i < 8; i++) await settle();
    const rows = [...ui.doc.querySelectorAll('#report-list .ri')];
    assert.equal(rows[1].querySelector('.pill').textContent, '不写成评级');
    assert.equal(rows[1].querySelector('.pill.hold, .pill.buy, .pill.sell'), null);
    assert.doesNotMatch(rows[1].textContent, /持有|Hold/);
    assert.match(rows[1].querySelector('.pill').title, /没有决定标记，不把它写成评级/);
  } finally { ui.dom.window.close(); }
});

test('a report header rating without a portfolio section is not called the manager conclusion', async () => {
  const ui = await openReports({
    '/api/reports/outline': () => ({name: 'AAPL', rating: 'Hold', header: ['- Rating: Hold'], sections: [
      {heading: 'I. Analyst Team Reports', folder: '1_analysts', items: [
        {agent: 'Market Analyst', zh: '市场分析师', file: 'market.md', text: '只有市场分析，没有组合经理段落。'},
      ]},
    ]}),
  });
  try {
    const decision = ui.doc.querySelector('.decision');
    assert.equal(decision.querySelector('.k').textContent, '没有组合经理结论');
    assert.match(decision.textContent, /报告头写着 Hold，不把它当成组合经理结论/);
    assert.match(decision.querySelector('.decision-text').innerHTML, /结论段落。<br class="notice-break">报告头写着 Hold，<br class="notice-break">不把它当成组合经理结论。/);
    assert.equal(decision.querySelector('.big').textContent, '—');
    assert.doesNotMatch(ui.doc.querySelector('.rc').textContent, /未能从最终决定/);
    const selected = ui.doc.querySelector('#report-list .ri.on');
    assert.equal(selected.querySelector('.pill').textContent, '无结论');
    assert.equal(selected.querySelector('.pill.hold, .pill.buy, .pill.sell'), null);
    assert.doesNotMatch(selected.textContent, /持有|增持/);
  } finally { ui.dom.window.close(); }
});

test('a REVIEW header without a portfolio section is not a failed final decision', async () => {
  const ui = await openReports({
    '/api/reports/outline': () => ({name: 'AAPL', rating: 'REVIEW', header: ['- Rating: REVIEW'], sections: [
      {heading: 'I. Analyst Team Reports', folder: '1_analysts', items: [
        {agent: 'Market Analyst', zh: '市场分析师', file: 'market.md', text: '只有市场分析，没有组合经理段落。'},
      ]},
    ]}),
  });
  try {
    assert.match(ui.doc.querySelector('.decision').textContent, /没有报告头评级|报告头写着 REVIEW/);
    assert.match(ui.doc.querySelector('.decision').textContent, /没有组合经理结论/);
    assert.doesNotMatch(ui.doc.querySelector('.rc').textContent, /未能从最终决定/);
    assert.equal(ui.doc.querySelector('#report-list .ri.on .pill').textContent, '无结论');
  } finally { ui.dom.window.close(); }
});

test('a saved outline without a portfolio section does not call the header rating a decision', async () => {
  const ui = await setup({
    '/api/runs': {run: {id: 'done', status: 'completed', kind: 'analysis', selections: {ticker: 'AAPL', date: '2026-11-11'}}, progress: {agents: []},
      result: {present: true, rating: 'Hold', decision: '—', html: '/tmp/results/reports/AAPL/complete_report.html'}},
    '/api/reports/outline': {name: 'AAPL', rating: 'Hold', header: ['- Rating: Hold'], sections: [
      {heading: 'I. Analyst Team Reports', zh: '分析师', folder: '1_analysts', items: [
        {agent: 'Market Analyst', zh: '市场分析师', file: 'market.md', text: '只有市场分析，没有组合经理段落。'},
      ]},
    ]},
  });
  try {
    await openRunFromList(ui, 'done');
    await ui.click('#show-saved-outline');
    for (let i = 0; i < 6; i++) await settle();
    const host = ui.doc.querySelector('#run-outline');
    assert.match(host.textContent, /报告头 Hold，没有组合经理结论/);
    assert.doesNotMatch(host.textContent, /未能从最终决定/);
    assert.doesNotMatch(host.querySelector('.notice').textContent, /评级 Hold/);
  } finally { ui.dom.window.close(); }
});

test('a schedule reminder does not call a header rating the conclusion', async () => {
  const ui = await setup({
    '/api/schedule/tick': {schedules: [{id: 's1', symbol: 'AAPL', asset_label: '股票', at: '09:30', status: 'active', status_label: '启用', due_note: '还没有到点触发。'}],
      reminders: [{body: 'AAPL 的分析已完成，报告头写着 Hold，没有组合经理结论。', status_label: '已记录'}],
      mode: 'observe', mode_label: '观察', rule_version: 'sched-1'},
  });
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    const text = ui.doc.querySelector('#sched-reminders').textContent;
    assert.match(text, /报告头写着 Hold，没有组合经理结论/);
    assert.doesNotMatch(text, /评级 Hold/);
  } finally { ui.dom.window.close(); }
});

test('a watch row without a saved decision does not say 决定', async () => {
  const ui = await setup({
    '/api/watchlist': {items: [{symbol: 'AAPL', asset_label: '股票', asset_type: 'stock', enabled: true, last_price: 190.5}],
      triggers: [], mode: 'observe', mode_label: '观察', rule_version: 'watch-1'},
  });
  try {
    await ui.click('[data-page=watchlist]');
    for (let i = 0; i < 6; i++) await settle();
    const text = ui.doc.querySelector('#watch-table').textContent;
    assert.match(text, /AAPL/);
    assert.match(text, /190\.50/);
    assert.doesNotMatch(text, /决定|持有/);
  } finally { ui.dom.window.close(); }
});

test('reports list every saved report with a rating pill, date and depth, and open the newest', async () => {
  const ui = await openReports();
  try {
    const rows = [...ui.doc.querySelectorAll('#report-list .ri')];
    assert.equal(rows.length, 2);
    assert.match(rows[0].textContent, /NVDA.*增持.*2026-10-29 · 中等深度/s);
    assert.ok(rows[0].querySelector('.pill.buy'));
    assert.ok(rows[1].querySelector('.pill.sell'));
    assert.match(rows[1].textContent, /浅层深度/);
    assert.ok(rows[0].classList.contains('on'));
    assert.equal(rows[0].getAttribute('aria-current'), 'true');
    assert.equal(ui.doc.querySelector('#report-title').textContent, 'NVDA · 2026-10-29');
    assert.match(ui.doc.querySelector('.rc-head').textContent, /完成于 21:12 · 中等深度 · 快 grok-4\.7 · 深 gpt-6-sol/);
    const decision = ui.doc.querySelector('.decision');
    assert.match(decision.textContent, /组合经理结论/);
    assert.equal(decision.querySelector('.big').textContent, '增持');
    assert.match(decision.textContent, /数据中心需求仍在上升/);
    assert.equal(ui.doc.querySelector('#report-technical-details').open, false);
    assert.match(ui.doc.querySelector('#report-technical-details summary').textContent, /运行日志与技术信息/);
    assert.equal(ui.calls.filter((item) => item.path.startsWith('/api/logs')).length, 0, 'logs load only when asked');
    assert.doesNotMatch(ui.visibleText(), /命令行|--lines|花括号/);
  } finally { ui.dom.window.close(); }
});

test('report tabs follow the outline and render markdown without running any HTML', async () => {
  const ui = await openReports();
  try {
    const tabs = [...ui.doc.querySelectorAll('[role=tab]')];
    assert.deepEqual(tabs.map((tab) => tab.textContent), ['综合结论', '市场', '新闻', '研究辩论', '交易计划', '风险评估']);
    assert.equal(tabs[0].getAttribute('aria-selected'), 'true');
    assert.match(ui.doc.querySelector('#report-body').textContent, /核心判断/);
    await ui.click('[data-tab="analyst-market.md"]');
    const body = ui.doc.querySelector('#report-body');
    assert.equal(body.querySelector('script, img'), null, 'raw HTML in a report is shown as text');
    assert.match(body.textContent, /<script>alert\(1\)<\/script>/);
    assert.ok(body.querySelector('h4'), 'markdown headings render');
    assert.ok(body.querySelector('.table-wrap > table'), 'markdown tables sit in a scrolling wrapper');
    const tab = ui.doc.querySelector('[data-tab="analyst-market.md"]');
    tab.dispatchEvent(new ui.window.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
    for (let i = 0; i < 4; i++) await settle();
    assert.equal(ui.doc.querySelector('[role=tab][aria-selected=true]').textContent, '新闻');
    assert.equal(ui.doc.activeElement?.dataset.tab, 'analyst-news.md', 'arrow keys move focus with the selected tab');
    assert.equal(ui.doc.querySelectorAll('#report-body li').length, 2);
    await ui.click('[data-tab=research]');
    assert.deepEqual([...ui.doc.querySelectorAll('#report-body h3.md-role')].map((node) => node.textContent), ['多头研究员', '空头研究员']);
  } finally { ui.dom.window.close(); }
});

test('the report filter narrows by ticker and another report can be selected', async () => {
  const ui = await openReports();
  try {
    ui.fill('#report-filter', 'aa');
    assert.deepEqual([...ui.doc.querySelectorAll('#report-list .ri')].map((row) => row.dataset.report.split('/').at(-2)), ['AAPL_20261028_100000']);
    await ui.click('#report-list .ri');
    for (let i = 0; i < 4; i++) await settle();
    assert.equal(ui.doc.querySelector('#report-title').textContent, 'AAPL · 2026-10-28');
    assert.equal(ui.doc.querySelector('.decision .big').textContent, '卖出');
    assert.equal(ui.doc.querySelector('.rc-head a[download]').textContent, '下载 HTML', 'no Markdown link without a Markdown file');
    ui.fill('#report-filter', 'zzz');
    assert.match(ui.doc.querySelector('#report-list').textContent, /没有匹配「ZZZ」的报告/);
  } finally { ui.dom.window.close(); }
});

test('the status page 打开报告 opens that report on the reports page', async () => {
  const ui = await openReports();
  try {
    ui.routes['/api/runs'] = {run: {id: 'done', status: 'finished', kind: 'analysis', selections: {ticker: 'AAPL'}}, progress: {agents: []},
      result: {present: true, html: reportRows[1].html}};
    await openRunFromList(ui, 'done');
    await ui.click('#open-run-report');
    for (let i = 0; i < 6; i++) await settle();
    assert.equal(ui.ui.state.page, 'reports');
    assert.equal(ui.doc.querySelector('#report-title').textContent, 'AAPL · 2026-10-28');
  } finally { ui.dom.window.close(); }
});

test('with no reports the page explains how to get one', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=reports]');
    assert.match(ui.visibleText(), /还没有汇总报告/);
    await ui.click('#start-analysis-from-reports');
    assert.equal(ui.ui.state.page, 'analyze');
  } finally { ui.dom.window.close(); }
});

test('accounts expose connection actions without credentials or diagnostic walls', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=accounts]');
    assert.match(ui.visibleText(), /Fixture User/);
    assert.equal(ui.doc.querySelector('#key-value').type, 'password');
    assert.equal(ui.doc.querySelector('#key-value').value, '');
    assert.equal(ui.doc.querySelector('[data-logout=chatgpt]').disabled, true);
    assert.equal(ui.doc.querySelector('#account-cli-details').open, false);
    assert.doesNotMatch(ui.visibleText(), /OPENAI_API_KEY|login_missing|花括号/);
  } finally { ui.dom.window.close(); }
});

test('a local account read error is shown as an error rather than a signed-out state', async () => {
  const ui = await setup({'/api/auth/status': {accounts: [{provider: 'chatgpt', signed_in: false, read_error: true, detail: 'FixtureReadError'}]}});
  try {
    await ui.click('[data-page=accounts]');
    assert.match(ui.visibleText(), /状态读取失败/);
    assert.match(ui.visibleText(), /刷新状态或重新登录/);
    assert.doesNotMatch(ui.visibleText(), /FixtureReadError/);
  } finally { ui.dom.window.close(); }
});

test('an unrecognized account sign-in is not called signed out', async () => {
  const ui = await setup({'/api/auth/status': {accounts: [
    {provider: 'chatgpt', signed_in: null, account: null},
    {provider: 'grok', signed_in: false},
  ]}});
  try {
    await ui.click('[data-page=accounts]');
    const cards = [...ui.doc.querySelectorAll('.account-provider-card')];
    const chatgpt = cards.find((card) => card.textContent.includes('ChatGPT') || card.querySelector('[data-login=chatgpt]'));
    const grok = cards.find((card) => card.querySelector('[data-login=grok]'));
    assert.ok(chatgpt, 'chatgpt card');
    assert.match(chatgpt.textContent, /登录状态未知，不把它写成未登录/);
    assert.doesNotMatch(chatgpt.querySelector('.status-badge').textContent, /未登录|已登录/);
    assert.equal(chatgpt.querySelector('[data-logout=chatgpt]').disabled, true);
    assert.match(ui.doc.querySelector('#identity-name').textContent, /登录状态未知/);
    assert.doesNotMatch(ui.doc.querySelector('#identity-name').textContent, /未登录/);
    assert.equal(grok.querySelector('.status-badge').textContent, '未登录');
    assert.doesNotMatch(grok.textContent, /不把它写成未登录/);
  } finally { ui.dom.window.close(); }
});

test('a known signed-in account is still called signed in', async () => {
  const ui = await setup({'/api/auth/status': {accounts: [
    {provider: 'chatgpt', signed_in: true, account: 'fixture-user'},
  ]}});
  try {
    await ui.click('[data-page=accounts]');
    const card = ui.doc.querySelector('.account-provider-card');
    assert.equal(card.querySelector('.status-badge').textContent, '已登录');
    assert.match(card.textContent, /fixture-user/);
    assert.equal(card.querySelector('[data-logout=chatgpt]').disabled, false);
    assert.doesNotMatch(card.textContent, /不把它写成未登录/);
    assert.match(ui.doc.querySelector('#identity-name').textContent, /模型服务/);
  } finally { ui.dom.window.close(); }
});

test('the plan note does not split 缺少 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=accounts]');
    const note = ui.doc.querySelector('#plan-note');
    assert.equal(note.textContent, '没传 plan 时，login 和 logout 各自打印缺少 plan 的句子。auth-status 省略 plan 时显示全部订阅，不打印缺少参数。空字符串是非法值，不是缺少参数。非法值会带上当时的原文，区分大小写。下面的 nope 只对 nope，不套用到别的值。auth-status nope 和 login nope 不是同一句。');
    assert.match(note.innerHTML, /没传 plan 时，<br class="notice-break">login 和 logout 各自打印缺少 plan 的<br class="notice-break">句子。<br class="notice-break">/);
    assert.match(note.innerHTML, /带上当时的原文，<br class="notice-break">/);
    assert.match(note.innerHTML, /不是<br class="notice-break">同一句。/);
    assert.doesNotMatch(note.innerHTML, /缺<br|当<br/);
    assert.doesNotMatch(note.textContent, /已经打印|参数已改/);
  } finally { ui.dom.window.close(); }
});

test('the account diagnostic note does not split 材料 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=accounts]');
    const note = ui.doc.querySelector('#auth-invocation');
    assert.equal(note.textContent, '当前筛选：auth-status。状态只读取本机登录材料，页面打开时不会联网校验。');
    assert.match(note.innerHTML, /auth-status。<br class="notice-break">状态只读取本机登录材料，<br class="notice-break">页面打开时不会联网校验。/);
    assert.doesNotMatch(note.innerHTML, /材<br/);
    assert.doesNotMatch(note.textContent, /已经联网|校验已完成/);
  } finally { ui.dom.window.close(); }
});

test('the already-running socket note does not split 文件锁 on a narrow screen', async () => {
  const run = {id: 'probe-idle', status: 'finished', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA', date: '2026-11-12'}};
  const ui = await setup({'/api/runs': {run, runs: [run], progress: {agents: []}, result: {present: false}}});
  try {
    await openRunFromList(ui, 'probe-idle');
    const note = ui.doc.querySelector('#already-socket-note');
    assert.equal(note.textContent, '控制套接字仍在监听。只有前台 start 或 restart 已经拿到文件锁，但控制套接字仍能连上时，才把这句打印到标准错误，退出码 1。句尾没有句号。拿不到文件锁时不套用这句。');
    assert.match(note.innerHTML, /控制套接字仍在监听。<br class="notice-break">只有前台 start 或 restart <br class="notice-break">已经拿到文件锁，<br class="notice-break">但控制套接字仍能连上时，<br class="notice-break">才把这句打印到标准错误，<br class="notice-break">退出码 1。<br class="notice-break">句尾没有句号。<br>拿不到文件锁时不套用这句。/);
    assert.doesNotMatch(note.innerHTML, /文<br|件<br/);
    assert.doesNotMatch(note.textContent, /已经释放|套接字已关闭/);
  } finally { ui.dom.window.close(); }
});

test('the already-running lock note does not split 这句 on a narrow screen', async () => {
  const run = {id: 'probe-idle', status: 'finished', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA', date: '2026-11-12'}};
  const ui = await setup({'/api/runs': {run, runs: [run], progress: {agents: []}, result: {present: false}}});
  try {
    await openRunFromList(ui, 'probe-idle');
    const note = ui.doc.querySelector('#already-lock-note');
    assert.equal(note.textContent, '文件锁已被占用。只有 start 或 restart 拿不到本机文件锁时才把这句打印到标准错误，退出码 1。句尾有句号。控制台已有进程时拒绝第二次启动，用的也是这句。套接字仍在监听时不套用这句。');
    assert.match(note.innerHTML, /文件锁已被占用。<br class="notice-break">只有 start 或 restart 拿不到本机文件锁时才把<br class="notice-break">这句打印到标准错误，<br class="notice-break">退出码 1。<br class="notice-break">句尾有句号。<br class="notice-break">控制台已有进程时拒绝第二次<br class="notice-break">启动，<br class="notice-break">用的也是这句。<br class="notice-break">套接字仍在监听时不套用这句。/);
    assert.doesNotMatch(note.innerHTML, /这<br/);
    assert.doesNotMatch(note.textContent, /已经启动|锁已释放/);
    const other = ui.doc.querySelector('#already-socket-note');
    assert.match(other.textContent, /控制套接字仍在监听/);
    assert.match(other.innerHTML, /restart <br class="notice-break">已经拿到文件锁，/, 'the socket note keeps 文件锁 whole, as its own test pins');
  } finally { ui.dom.window.close(); }
});

test('the logs usage note does not split 错误 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=reports]');
    const note = ui.doc.querySelector('#logs-usage-note');
    assert.equal(note.textContent, '日志多余参数或负数行数。没有 --help 或 -h 时，logs 还有无法识别的剩余参数，或 --lines 是负数，才把这句打印到标准错误，退出码 2。非整数不是这句。');
    assert.match(note.innerHTML, /日志多余参数或负数行数。<br class="notice-break">没有 --help 或 -h 时，<br class="notice-break">logs 还有无法识别的剩余参数，<br class="notice-break">或 --lines 是负数，<br class="notice-break">才把这句打印到标准错误，<br class="notice-break">退出码 2。<br class="notice-break">非整数不是这句。/);
    assert.doesNotMatch(note.innerHTML, /错<br/);
    assert.doesNotMatch(note.textContent, /已经打印|行数已改/);
  } finally { ui.dom.window.close(); }
});

test('the logs integer note does not split 打印 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=reports]');
    const note = ui.doc.querySelector('#logs-integer-note');
    assert.equal(note.textContent, '日志行数不是整数。只有 logs --lines 后面缺少值，或那个值不是整数时，才把这句打印到标准错误，退出码 2。这句不带上当时的值。负数不是这句。带 --help 或 -h 时不打印这句。');
    assert.match(note.innerHTML, /才把这句打印到标准错误，<br class="notice-break">/);
    assert.doesNotMatch(note.innerHTML, /打<br/);
  } finally { ui.dom.window.close(); }
});

test('the logs help note does not split 打印 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=reports]');
    const note = ui.doc.querySelector('#logs-help-note');
    assert.equal(note.textContent, '日志帮助。argv 里出现 --help 或 -h 就打印到标准输出，退出码 0，即使同时还有别的参数。没有这两个标志时，错误参数不打印这句。');
    assert.match(note.innerHTML, /日志帮助。<br class="notice-break">argv 里出现 --help 或 -h 就<br class="notice-break">打印到标准输出，<br class="notice-break">退出码 0，<br class="notice-break">即使同时还有别的参数。<br class="notice-break">没有这两个标志时，<br class="notice-break">错误参数不打印这句。/);
    assert.doesNotMatch(note.textContent, /已经打印|帮助已显示/);
    const other = ui.doc.querySelector('#logs-integer-note');
    assert.equal(other.textContent, '日志行数不是整数。只有 logs --lines 后面缺少值，或那个值不是整数时，才把这句打印到标准错误，退出码 2。这句不带上当时的值。负数不是这句。带 --help 或 -h 时不打印这句。');
    assert.match(other.innerHTML, /日志行数不是整数。<br class="notice-break">只有 logs --lines 后面缺少值，<br class="notice-break">或那个值不是整数时，<br class="notice-break">才把这句打印到标准错误，<br class="notice-break">退出码 2。<br class="notice-break">这句不带上当时的值。<br class="notice-break">负数不是这句。<br class="notice-break">带 --help 或 -h 时不打印这句。/);
    assert.doesNotMatch(other.textContent, /已经打印|行数已改/);
  } finally { ui.dom.window.close(); }
});

test('the log waiting note does not split 打印 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=reports]');
    const note = ui.doc.querySelector('#report-log-path');
    assert.equal(note.textContent, '正在读取。读到路径后显示命令行 logs 打印的路径句，不预写。');
    assert.match(note.innerHTML, /正在读取。<br class="notice-break">读到路径后显示命令行 logs <br class="notice-break">打印的路径句，不预写。/);
    assert.doesNotMatch(note.textContent, /已预写|日志路径是/);
  } finally { ui.dom.window.close(); }
});

test('the key details note does not split 供应商 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=accounts]');
    const details = ui.doc.querySelector('#key-details');
    details.open = true;
    const note = ui.doc.querySelector('#key-secret-note');
    assert.equal(note.textContent, '页面不会展示密钥内容。以下仅用于确认供应商与保存项。');
    assert.match(note.innerHTML, /密钥内容。<br class="notice-break">以下仅用于确认供应商与保存项。/);
    assert.doesNotMatch(note.textContent, /密钥已展示|sk-/);
  } finally { ui.dom.window.close(); }
});

test('the data source note does not split 来源 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=accounts]');
    const note = ui.doc.querySelector('#data-source-note');
    assert.equal(note.textContent, '行情、新闻和基本面从哪里取。留空沿用默认来源，保存在本机控制台配置里。');
    assert.match(note.innerHTML, /从哪里取。<br class="notice-break">留空沿用默认来源，<br class="notice-break">保存在本机控制台配置里。/);
    assert.doesNotMatch(note.textContent, /已更换来源|供应商账单/);
  } finally { ui.dom.window.close(); }
});

test('the credential summary does not split 填写 on a narrow screen', async () => {
  const ui = await setup({'/api/config': {saved: {}, keys: [{provider: 'openai', configured: false, env_var: 'OPENAI_API_KEY'}], data_env: [{env: 'FRED_API_KEY', configured: true}], cloud_env: []}});
  try {
    await ui.click('[data-page=accounts]');
    const note = ui.doc.querySelector('#credential-summary');
    assert.equal(note.textContent, '可选凭据已配置 1 项。默认数据来源无需在此填写。');
    assert.match(note.innerHTML, /已配置 1 项。<br class="notice-break">默认数据来源无需在此填写。/);
    assert.doesNotMatch(note.textContent, /不把它写成未配置|已在此填写/);
  } finally { ui.dom.window.close(); }
});

test('the local data credential note does not split 回显 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=accounts]');
    const details = ui.doc.querySelector('#data-env-details');
    details.open = true;
    const note = ui.doc.querySelector('#data-env-local-note');
    assert.equal(note.textContent, '这些可选凭据只写入本机配置，内容不会回显。');
    assert.match(note.innerHTML, /本机配置，<br class="notice-break">内容不会回显。/);
    assert.doesNotMatch(note.innerHTML, /回<br/);
    assert.doesNotMatch(note.textContent, /已经回显|密钥已展示/);
  } finally { ui.dom.window.close(); }
});

test('the SEC contact note does not split 留空 or 项目', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=accounts]');
    ui.doc.querySelector('#data-env-details').open = true;
    const label = ui.doc.querySelector('label[for="env-SEC_EDGAR_USER_AGENT"]');
    assert.equal(label.textContent, 'SEC 联系标识 SEC_EDGAR_USER_AGENT。读取财报时发给 SEC 的姓名和联系地址。不是密钥。留空则使用项目占位地址。 配置状态未知，不把它写成未配置。');
    assert.match(label.innerHTML, /不是密钥。<br>留空则使用项目占位地址。/);
    assert.doesNotMatch(label.innerHTML, /留<br|项<br/);
    assert.doesNotMatch(label.textContent, /已经留空|地址已改/);
  } finally { ui.dom.window.close(); }
});

test('the Azure endpoint note does not split 地址 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=accounts]');
    ui.doc.querySelector('#data-env-details').open = true;
    const label = ui.doc.querySelector('label[for="env-AZURE_OPENAI_ENDPOINT"]');
    assert.match(label.textContent, /例如 https:\/\/资源名.openai.azure.com\/。页面的推理地址也会传给 Azure。/);
    assert.match(label.innerHTML, /azure\.com\/。<br class="notice-break">页面的推理地址也会传给 Azure。/);
    assert.doesNotMatch(label.innerHTML, /地<br|址<br/);
    assert.doesNotMatch(label.textContent, /地址已改|已经传给/);
  } finally { ui.dom.window.close(); }
});

test('the Azure API version note does not split 配置 on a narrow screen', async () => {
  const ui = await setup({'/api/config': {saved: {}, keys: [], data_env: [], cloud_env: [{env: 'OPENAI_API_VERSION', configured: false}]}});
  try {
    await ui.click('[data-page=accounts]');
    ui.doc.querySelector('#data-env-details').open = true;
    const label = ui.doc.querySelector('label[for="env-OPENAI_API_VERSION"]');
    assert.equal(label.textContent, 'Azure 接口版本 OPENAI_API_VERSION。Azure OpenAI 需要，例如 2025-03-01-preview。 未配置。');
    assert.match(label.innerHTML, /preview。 <br class="notice-break">未配置。/);
    assert.doesNotMatch(label.innerHTML, /配<br|未<br/);
    assert.doesNotMatch(label.textContent, /已经配置|版本已改/);
  } finally { ui.dom.window.close(); }
});

test('the Bedrock region note does not split 客户端 or 未配置', async () => {
  const ui = await setup({'/api/config': {saved: {}, keys: [], data_env: [], cloud_env: [{env: 'AWS_REGION', configured: false}]}});
  try {
    await ui.click('[data-page=accounts]');
    ui.doc.querySelector('#data-env-details').open = true;
    const label = ui.doc.querySelector('label[for="env-AWS_REGION"]');
    assert.equal(label.textContent, 'Bedrock 优先区域 AWS_REGION。Bedrock 客户端先读这个变量。设置后会覆盖 AWS_DEFAULT_REGION。 未配置。');
    assert.match(label.innerHTML, /Bedrock <br class="notice-break">客户端先读这个变量。/);
    assert.match(label.innerHTML, /AWS_DEFAULT_REGION。 <br>未配置。/);
    assert.doesNotMatch(label.innerHTML, /客<br|端<br|未<br|配<br/);
    assert.doesNotMatch(label.textContent, /已经配置|区域已改/);
  } finally { ui.dom.window.close(); }
});

test('the Bedrock default region note does not split 配置 on a narrow screen', async () => {
  const ui = await setup({'/api/config': {saved: {}, keys: [], data_env: [], cloud_env: [{env: 'AWS_DEFAULT_REGION', configured: false}]}});
  try {
    await ui.click('[data-page=accounts]');
    ui.doc.querySelector('#data-env-details').open = true;
    const label = ui.doc.querySelector('label[for="env-AWS_DEFAULT_REGION"]');
    assert.equal(label.textContent, 'Bedrock 区域 AWS_DEFAULT_REGION。客户端先读 AWS_REGION，没有时才读这个变量。 未配置。');
    assert.match(label.innerHTML, /这个变量。 <br class="notice-break">未配置。/);
    assert.doesNotMatch(label.innerHTML, /配<br|未<br/);
    assert.doesNotMatch(label.textContent, /已经配置|区域已改/);
  } finally { ui.dom.window.close(); }
});

test('the Bedrock token note does not split 未配置 on a wide screen', async () => {
  const ui = await setup({'/api/config': {saved: {}, keys: [], data_env: [], cloud_env: [{env: 'AWS_BEARER_TOKEN_BEDROCK', configured: false}]}});
  try {
    await ui.click('[data-page=accounts]');
    ui.doc.querySelector('#data-env-details').open = true;
    const label = ui.doc.querySelector('label[for="env-AWS_BEARER_TOKEN_BEDROCK"]');
    assert.equal(label.textContent, 'Bedrock 令牌 AWS_BEARER_TOKEN_BEDROCK。Bedrock API 密钥。不回显。没有它时使用 AWS 凭证链。 未配置。');
    assert.match(label.innerHTML, /凭证链。 <br>未配置。/);
    assert.doesNotMatch(label.innerHTML, /未<br|配<br/);
    assert.doesNotMatch(label.textContent, /已经配置|令牌已改/);
  } finally { ui.dom.window.close(); }
});

test('a tool vendor note does not split 留空 or 沿用 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=accounts]');
    const label = ui.doc.querySelector('label[for="tool-get_stock_data"]');
    assert.ok(label, 'stock data tool label');
    assert.match(label.textContent, /tool_vendors.get_stock_data。留空沿用类别链。/);
    assert.match(label.innerHTML, /get_stock_data。<br class="notice-break">留空沿用类别链。/);
    assert.doesNotMatch(label.innerHTML, /留<br|沿<br/);
    assert.doesNotMatch(label.textContent, /已经沿用|类别已改/);
  } finally { ui.dom.window.close(); }
});

test('an unrecognized key configuration is not called unconfigured', async () => {
  const ui = await setup({'/api/config': {saved: {}, keys: [
    {provider: 'openai', configured: null, env_var: 'OPENAI_API_KEY'},
    {provider: 'anthropic', configured: false, env_var: 'ANTHROPIC_API_KEY'},
  ], data_env: [{env: 'FRED_API_KEY', configured: null}], cloud_env: []}});
  try {
    await ui.click('[data-page=accounts]');
    const status = ui.doc.querySelector('#key-status').textContent;
    assert.match(status, /配置状态未知，不把它写成未配置/);
    assert.doesNotMatch(status, /尚未配置 API Key/);
    const shown = ui.doc.querySelector('#key-providers .custom-select-value').textContent;
    assert.match(shown, /配置状态未知/);
    assert.doesNotMatch(shown, /未配置/);
    ui.doc.querySelector('#key-details').open = true;
    const row = [...ui.doc.querySelectorAll('#key-details li')].find((item) => item.textContent.includes('OPENAI_API_KEY'));
    assert.match(row.textContent, /配置状态未知/);
    assert.doesNotMatch(row.textContent, /未配置/);
    const known = [...ui.doc.querySelectorAll('#key-details li')].find((item) => item.textContent.includes('ANTHROPIC_API_KEY'));
    assert.match(known.textContent, /未配置/);
    assert.doesNotMatch(known.textContent, /不把它写成未配置/);
    ui.doc.querySelector('#data-env-details').open = true;
    const field = ui.doc.querySelector('#data-env-form label');
    if (field) {
      assert.match(field.textContent, /配置状态未知，不把它写成未配置/);
    }
    assert.match(ui.doc.querySelector('#credential-summary').textContent, /不把它写成未配置/);
  } finally { ui.dom.window.close(); }
});

test('a data credential save without a saved flag is not called saved', async () => {
  const ui = await setup({'/api/data-env': {env_var: 'FRED_API_KEY', zh: '已保存'}});
  try {
    await ui.click('[data-page=accounts]');
    ui.doc.querySelector('#data-env-details').open = true;
    ui.fill('#env-FRED_API_KEY', 'fred-fixture-not-real');
    await ui.click('#data-env-form button[type=submit]');
    await settleMany();
    const note = ui.doc.querySelector('#data-env-note');
    assert.ok(note);
    assert.match(note.textContent, /不把它写成已保存/);
    assert.doesNotMatch(note.textContent, /^已保存/);
    assert.doesNotMatch(ui.doc.body.textContent, /fred-fixture-not-real|undefined/);
  } finally { ui.dom.window.close(); }
});

test('a data credential save marked saved still says it was saved', async () => {
  const ui = await setup({'/api/data-env': {saved: true, env_var: 'FRED_API_KEY'}});
  try {
    await ui.click('[data-page=accounts]');
    ui.doc.querySelector('#data-env-details').open = true;
    ui.fill('#env-FRED_API_KEY', 'fred-fixture-not-real');
    await ui.click('#data-env-form button[type=submit]');
    await settleMany();
    assert.match(ui.doc.querySelector('#data-env-note').textContent, /^已保存 1 项/);
    assert.doesNotMatch(ui.doc.body.textContent, /fred-fixture-not-real|undefined/);
  } finally { ui.dom.window.close(); }
});

test('a watch trigger without a status is not called unchecked', async () => {
  const trigger = {id: 't1', symbol: '600519', kind: 'price', created_at: '2026-11-12T01:00:00Z', old_price: 9, new_price: 10};
  const ui = await setup({'/api/watchlist': {mode: 'observe', mode_label: '观察', rule_version: 'watch-1', note: '', items: [
    {symbol: '600519', asset_label: '股票', asset_type: 'stock', enabled: true, last_price: 10},
  ], triggers: [trigger], visible_triggers: [trigger]}});
  try {
    await ui.click('[data-page=watchlist]');
    for (let i = 0; i < 6; i++) await settle();
    const note = ui.doc.querySelector('#watch-trigger-note');
    const badge = ui.doc.querySelector('.watch-triggers .status-badge');
    assert.ok(note);
    assert.match(note.textContent, /不把它写成未检查/);
    assert.doesNotMatch(note.textContent, /^未检查/);
    assert.equal(badge.textContent, '状态未知');
    assert.doesNotMatch(ui.doc.querySelector('.watch-triggers').textContent, /未检查/);
  } finally { ui.dom.window.close(); }
});

test('an unrecognized watch trigger label is not called unchecked', async () => {
  const unchecked = {id: 't1', symbol: '600519', kind: 'price', status_label: 'retrying', checked: false, created_at: '2026-11-12T01:00:00Z', old_price: 9, new_price: 10};
  const checked = {id: 't2', symbol: 'AAPL', kind: 'price', status_label: 'later', checked: true, started: true, run_id: 'run-1', created_at: '2026-11-12T02:00:00Z', old_price: 1, new_price: 2};
  const ui = await setup({'/api/watchlist': {mode: 'observe', mode_label: '观察', rule_version: 'watch-1', note: '', items: [], triggers: [unchecked, checked], visible_triggers: [unchecked, checked]}});
  try {
    await ui.click('[data-page=watchlist]');
    for (let i = 0; i < 6; i++) await settle();
    const badges = [...ui.doc.querySelectorAll('.watch-triggers .status-badge')].map((node) => node.textContent);
    const list = ui.doc.querySelector('.watch-triggers').textContent;
    assert.deepEqual(badges, ['状态未知', '状态未知']);
    assert.match(ui.doc.querySelector('#watch-trigger-note').textContent, /不把它写成未检查/);
    assert.doesNotMatch(list, /未检查|已检查|retrying|later|已启动/);
  } finally { ui.dom.window.close(); }
});

test('a watch trigger marked unchecked still says unchecked', async () => {
  const trigger = {id: 't1', symbol: '600519', kind: 'price', status_label: '未检查', checked: false, created_at: '2026-11-12T01:00:00Z'};
  const ui = await setup({'/api/watchlist': {mode: 'observe', mode_label: '观察', rule_version: 'watch-1', note: '', items: [], triggers: [trigger], visible_triggers: [trigger]}});
  try {
    await ui.click('[data-page=watchlist]');
    for (let i = 0; i < 6; i++) await settle();
    assert.equal(ui.doc.querySelector('.watch-triggers .status-badge').textContent, '未检查');
    assert.equal(ui.doc.querySelector('#watch-trigger-note'), null);
  } finally { ui.dom.window.close(); }
});

test('a watchlist without a mode is not called observe', async () => {
  const ui = await setup({'/api/watchlist': {mode_label: '观察', rule_version: 'watch-1', note: '', items: [
    {symbol: '600519', asset_label: '股票', asset_type: 'stock', enabled: true, last_price: 10},
  ], triggers: [], visible_triggers: []}});
  try {
    await ui.click('[data-page=watchlist]');
    for (let i = 0; i < 6; i++) await settle();
    const rule = ui.doc.querySelector('#watch-rule').textContent;
    const note = ui.doc.querySelector('#watch-mode-note').textContent;
    assert.match(rule, /不把它写成观察模式/);
    assert.match(note, /不把它写成观察模式/);
    assert.doesNotMatch(rule, /观察模式不会自动启动/);
    assert.doesNotMatch(note, /观察模式不会自动启动/);
    assert.match(rule, /状态未知/);
  } finally { ui.dom.window.close(); }
});

test('an observe watchlist still says observe', async () => {
  const ui = await setup({'/api/watchlist': {mode: 'observe', mode_label: '观察', rule_version: 'watch-1', note: '', items: [], triggers: [], visible_triggers: []}});
  try {
    await ui.click('[data-page=watchlist]');
    for (let i = 0; i < 6; i++) await settle();
    assert.match(ui.doc.querySelector('#watch-rule').textContent, /未检查表示还没有规则检查/);
    assert.match(ui.doc.querySelector('#watch-mode-note').textContent, /观察模式不会自动启动分析/);
    assert.doesNotMatch(ui.doc.querySelector('#watch-rule').textContent, /不把它写成观察模式/);
  } finally { ui.dom.window.close(); }
});

test('an unknown watch mode is not called observe', async () => {
  const ui = await setup({'/api/watchlist': {mode: 'later', mode_label: 'later', rule_version: 'watch-1', note: '', items: [
    {symbol: '600519', asset_label: '股票', asset_type: 'stock', enabled: true, last_price: 10},
  ], triggers: [
    {id: 't1', symbol: '600519', kind: 'price', status_label: 'retrying', created_at: '2026-11-11T10:00:00Z', old_price: 9, new_price: 10},
  ], visible_triggers: [
    {id: 't1', symbol: '600519', kind: 'price', status_label: 'retrying', created_at: '2026-11-11T10:00:00Z', old_price: 9, new_price: 10},
  ]}});
  try {
    await ui.click('[data-page=watchlist]');
    for (let i = 0; i < 6; i++) await settle();
    const page = ui.doc.querySelector('#page').textContent;
    assert.match(page, /状态未知/);
    assert.match(page, /不把它写成观察模式/);
    assert.doesNotMatch(page, /later|retrying/);
    assert.doesNotMatch(ui.doc.querySelector('#watch-rule').textContent, /观察模式不会自动启动/);
  } finally { ui.dom.window.close(); }
});

test('a schedule without an asset label is not called a stock', async () => {
  const ui = await setup({'/api/schedule/tick': {mode: 'observe', mode_label: '观察', rule_version: 'sched-1', note: '', schedules: [
    {id: 'sch-1', symbol: 'EURUSD', at: '09:30', status: 'active', status_label: '观察中', due_note: '还没有到点触发。'},
  ], reminders: []}});
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    const meta = ui.doc.querySelector('.schedule-card .cell-secondary').textContent;
    assert.match(meta, /^资产类型未知，不把它写成股票 · 每天 09:30/);
    assert.doesNotMatch(meta, /^ ·|^股票/);
    assert.match(ui.doc.querySelector('.schedule-card').textContent, /观察中/);
  } finally { ui.dom.window.close(); }
});

test('a schedule with a stock label still says stock', async () => {
  const ui = await setup({'/api/schedule/tick': {mode: 'observe', mode_label: '观察', rule_version: 'sched-1', note: '', schedules: [
    {id: 'sch-1', symbol: '600519', at: '09:30', status: 'active', asset_label: '股票', status_label: '观察中', due_note: '还没有到点触发。'},
  ], reminders: []}});
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    const card = ui.doc.querySelector('.schedule-card').textContent;
    assert.match(card, /股票 · 每天 09:30 · 观察中/);
    assert.doesNotMatch(card, /不把它写成股票/);
  } finally { ui.dom.window.close(); }
});

test('an active schedule without a status label is not called observing', async () => {
  const ui = await setup({'/api/schedule/tick': {mode: '', rule_version: 'sched-1', note: '', schedules: [
    {id: 'sch-1', symbol: '600519', at: '09:30', status: 'active', asset_label: '股票'},
  ], reminders: []}});
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    const card = ui.doc.querySelector('.schedule-card').textContent;
    assert.match(card, /不把它写成观察中/);
    assert.match(card, /· 状态未知/);
    assert.doesNotMatch(card, /· 观察中/);
  } finally { ui.dom.window.close(); }
});

test('a schedule without a mode is not called observe', async () => {
  const ui = await setup({'/api/schedule/tick': {mode_label: '观察', rule_version: 'sched-1', note: '', schedules: [], reminders: []}});
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    const rule = ui.doc.querySelector('#sched-rule').textContent;
    const note = ui.doc.querySelector('#sched-mode-note').textContent;
    assert.match(rule, /不把它写成观察模式或已启用/);
    assert.match(note, /不把它写成观察模式或已启用/);
    assert.doesNotMatch(rule, /观察模式不会自动启动/);
    assert.doesNotMatch(note, /观察模式不会自动启动/);
    assert.match(rule, /状态未知/);
  } finally { ui.dom.window.close(); }
});

test('the schedule page description does not split 打开向导 on a narrow screen', async () => {
  const ui = await setup({'/api/schedule/tick': {mode: 'observe', mode_label: '观察', rule_version: 'sched-1', note: '', schedules: [], reminders: []}});
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    const note = ui.doc.querySelector('#sched-mode-note');
    assert.equal(note.textContent, '到点只记录触发。观察模式不会自动启动分析。去分析只打开向导。提醒先写入，再显示在本页。');
    assert.match(note.innerHTML, /到点只记录触发。<br class="notice-break">观察模式不会自动启动分析。<br class="notice-break">去分析只打开向导。<br class="notice-break">提醒先写入，<br class="notice-break">再显示在本页。/);
    assert.doesNotMatch(note.textContent, /已启动分析|自动启动。去分析/);
  } finally { ui.dom.window.close(); }
});

test('an observe schedule still says observe', async () => {
  const ui = await setup({'/api/schedule/tick': {mode: 'observe', mode_label: '观察', rule_version: 'sched-1', note: '', schedules: [], reminders: []}});
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    assert.match(ui.doc.querySelector('#sched-rule').textContent, /到点只记录触发，不会自动启动分析/);
    assert.match(ui.doc.querySelector('#sched-mode-note').textContent, /观察模式不会自动启动分析/);
    assert.doesNotMatch(ui.doc.querySelector('#sched-rule').textContent, /不把它写成观察模式/);
  } finally { ui.dom.window.close(); }
});

test('an unknown schedule mode is not called enabled or observe', async () => {
  const ui = await setup({'/api/schedule/tick': {mode: 'later', mode_label: 'later', rule_version: 'sched-1', note: '', schedules: [
    {id: 'sch-1', symbol: '600519', at: '09:30', status: 'active', asset_label: '股票', status_label: '状态未知', due_note: '还没有到点触发。'},
  ], reminders: [
    {id: 'r1', body: '600519 的提醒', status: 'retrying', status_label: 'retrying', system_problem: true},
  ]}});
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    const page = ui.doc.querySelector('#page').textContent;
    assert.match(page, /状态未知/);
    assert.match(page, /不把它写成观察模式或已启用/);
    assert.doesNotMatch(page, /later|retrying|仍会重试/);
    assert.doesNotMatch(ui.doc.querySelector('#sched-rule').textContent, /^已启用/);
    assert.match(ui.doc.querySelector('#sched-rule').textContent, /状态未知/);
  } finally { ui.dom.window.close(); }
});

test('an unknown run status is not shown as its raw token', async () => {
  const run = {id: 'run-1', kind: 'analysis', status: 'retrying', ticker: '600519', date: '2026-11-11', started_at: '2026-11-11T10:00:00', selections: {ticker: '600519', date: '2026-11-11'}};
  const payload = {runs: [{...run, done: 1, total: 11, team_zh: ''}], run, progress: {agents: [], timeline: []}, result: {present: false}, counts: {all: 1, running: 0, queued: 0, failed: 0, finished: 0}, limits: {max_running: 2, max_queued: 5}};
  const ui = await setup({'/api/runs': (options, path) => payload});
  try {
    await ui.click('[data-page=runs]');
    for (let i = 0; i < 8; i++) await settle();
    const row = ui.doc.querySelector('#run-list [data-run-id="run-1"]');
    assert.match(row.textContent, /状态未知/);
    assert.doesNotMatch(row.textContent, /retrying|运行中|已结束|已完成/);
    await ui.click('#run-list [data-run-id="run-1"]');
    for (let i = 0; i < 8; i++) await settle();
    const page = ui.doc.querySelector('#page').textContent;
    assert.match(page, /状态未知/);
    assert.match(page, /不把它写成已结束，也不写成运行中/);
    assert.equal(ui.doc.querySelector('#run-elapsed').textContent, '—');
    assert.doesNotMatch(page, /retrying|小时/);
  } finally { ui.dom.window.close(); }
});

test('a reminder without a decision flag is not shown as a structured rating', async () => {
  const body = 'AAPL 的分析已完成，记录里有 Buy，但不把它当成结构化评级。';
  const ui = await setup({'/api/schedule/tick': {mode: 'observe', mode_label: '观察', rule_version: 'sched-1', note: '', schedules: [], reminders: [
    {id: 'r1', body, status: 'sent', status_label: '已发送'},
  ]}});
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    const row = ui.doc.querySelector('#sched-reminders').textContent;
    assert.match(row, /记录里有 Buy，但不把它当成结构化评级/);
    assert.doesNotMatch(row, /评级 Buy|仍会重试/);
    assert.match(row, /已发送/);
  } finally { ui.dom.window.close(); }
});

test('an unknown schedule item status is not called observing', async () => {
  const ui = await setup({'/api/schedule/tick': {mode: 'observe', mode_label: '观察', rule_version: 'sched-1', note: '', schedules: [
    {id: 'sch-1', symbol: '600519', at: '09:30', status: 'mystery', asset_label: '股票', status_label: '观察中', due_note: '还没有到点触发。'},
  ], reminders: []}});
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    const card = ui.doc.querySelector('.schedule-card').textContent;
    assert.match(card, /状态未知/);
    assert.match(card, /定时状态未知，不把它写成观察中，也不写成可以暂停/);
    const note = [...ui.doc.querySelectorAll('.schedule-card .cell-secondary')].find((el) => el.textContent.includes('不把它写成观察中'));
    assert.match(note.innerHTML, /定时状态未知，<br class="notice-break">不把它写成观察中，<br class="notice-break">也不写成可以暂停。/);
    assert.doesNotMatch(card, /mystery|· 观察中|还没有到点触发|暂停去分析|继续去分析/);
    assert.equal(ui.doc.querySelector('[data-sched-pause]'), null);
  } finally { ui.dom.window.close(); }
});

test('an unknown schedule check is not called not started', async () => {
  const ui = await setup({'/api/schedule/tick': {mode: 'observe', mode_label: '观察', rule_version: 'sched-1', note: '', schedules: [
    {id: 'sch-1', symbol: '600519', at: '09:30', status: 'active', asset_label: '股票', status_label: '观察中',
      latest_due: {slot: '2026-11-12', at: '09:30'},
      latest_check: {result: 'mystery', started: true, run_id: 'run-9'}},
  ], reminders: []}});
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    const card = ui.doc.querySelector('.schedule-card').textContent;
    assert.match(card, /检查结果未知，不把它写成未启动或已启动。/);
    assert.doesNotMatch(card, /mystery|09:30 未启动|09:30 已启动/);
  } finally { ui.dom.window.close(); }
});

test('a login that is not waiting is not called already started', async () => {
  const ui = await setup({'/api/auth/login': (options) => options.method
    ? {provider: 'chatgpt', status: 'signed_in', account: 'fixture@example.com'}
    : {status: 'idle', messages: []}});
  try {
    await ui.click('[data-page=accounts]');
    await ui.click('[data-login=chatgpt]');
    const card = ui.doc.querySelector('[data-auth-feedback="chatgpt"]').textContent;
    assert.match(card, /登录返回已登录，不把它写成仍在启动/);
    assert.doesNotMatch(card, /登录已启动|等待授权/);
    assert.equal(ui.doc.querySelector('#paste-url').disabled, true);
  } finally { ui.dom.window.close(); }
});

test('a result without a saved flag is not called saved', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'saved-run', status: 'finished', kind: 'analysis', ticker: 'AAPL', date: '2026-11-11', selections: {ticker: 'AAPL'}, started_at: '2026-11-11T09:00:00', finished_at: '2026-11-11T09:20:00'},
    progress: {agents: []},
    result: {present: true, decision: '持有', rating: 'Hold', saved_to: '/tmp/reports', report: '/tmp/reports/complete_report.md'},
    runs: [{id: 'saved-run', status: 'finished', ticker: 'AAPL', date: '2026-11-11'}],
  }});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'saved-run');
    const note = ui.doc.querySelector('#save-notice');
    assert.ok(note);
    assert.match(note.textContent, /保存结果未知，不把它写成已保存/);
    assert.doesNotMatch(note.textContent, /^报告已保存/);
  } finally { ui.dom.window.close(); }
});

test('a result marked saved still says the report was saved', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'saved-run', status: 'finished', kind: 'analysis', ticker: 'AAPL', date: '2026-11-11', selections: {ticker: 'AAPL'}, started_at: '2026-11-11T09:00:00', finished_at: '2026-11-11T09:20:00'},
    progress: {agents: []},
    result: {present: true, decision: '持有', rating: 'Hold', saved: true, saved_to: '/tmp/reports'},
    runs: [{id: 'saved-run', status: 'finished', ticker: 'AAPL', date: '2026-11-11'}],
  }});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'saved-run');
    assert.match(ui.doc.querySelector('#save-notice').textContent, /^报告已保存/);
  } finally { ui.dom.window.close(); }
});

test('the account page description does not split 密钥 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('.nav-item[data-page=accounts]');
    const desc = ui.doc.querySelector('.page-description');
    assert.equal(desc.textContent, '管理订阅登录和 API Key。本页显示本机配置状态，密钥不会回显。');
    assert.match(desc.innerHTML, /管理订阅登录和 API Key。<br class="notice-break">本页显示本机配置状态，<br class="notice-break">密钥不会回显。/);
    assert.doesNotMatch(desc.textContent, /密钥已回显|已保存密钥/);
  } finally { ui.dom.window.close(); }
});

test('a key save without a saved flag is not called skipped', async () => {
  const ui = await setup({'/api/keys': {zh: '已保存', cli: 'Saved OPENAI_API_KEY to /tmp/.env'}});
  try {
    await ui.click('[data-page=accounts]');
    ui.fill('#key-value', 'sk-fixture-not-real');
    await ui.click('#key-form button[type=submit]');
    await settleMany();
    const note = ui.doc.querySelector('#key-save-note');
    assert.ok(note);
    assert.match(note.textContent, /不把它写成已保存或已跳过/);
    assert.doesNotMatch(note.textContent, /^已保存|^已跳过/);
    assert.doesNotMatch(ui.doc.body.textContent, /sk-fixture-not-real|undefined/);
    assert.equal(ui.calls.filter((item) => item.path === '/api/keys' && item.method === 'POST').length, 1);
  } finally { ui.dom.window.close(); }
});

test('a key save marked saved still says it was saved', async () => {
  const ui = await setup({'/api/keys': {saved: true, cli: 'Saved OPENAI_API_KEY to /tmp/.env'}});
  try {
    await ui.click('[data-page=accounts]');
    ui.fill('#key-value', 'sk-fixture-not-real');
    await ui.click('#key-form button[type=submit]');
    await settleMany();
    const note = ui.doc.querySelector('#key-save-note');
    assert.match(note.textContent, /^已保存/);
    assert.match(note.textContent, /密钥不会回显/);
    assert.doesNotMatch(note.textContent, /不把它写成已保存/);
    assert.doesNotMatch(ui.doc.body.textContent, /sk-fixture-not-real/);
  } finally { ui.dom.window.close(); }
});

test('a key save marked skipped still says nothing was written', async () => {
  const ui = await setup({'/api/keys': {saved: false, zh: '已跳过。没有写入密钥。'}});
  try {
    await ui.click('[data-page=accounts]');
    ui.fill('#key-value', 'sk-fixture-not-real');
    await ui.click('#key-form button[type=submit]');
    await settleMany();
    assert.match(ui.doc.querySelector('#key-save-note').textContent, /^已跳过，没有写入/);
  } finally { ui.dom.window.close(); }
});

test('a logout without an outcome is not called signed out', async () => {
  const ui = await setup({
    '/api/auth/status': {accounts: [{provider: 'chatgpt', signed_in: true, account: 'fixture@example.com'}, {provider: 'grok', signed_in: false}]},
    '/api/auth/logout': {zh: '已退出'},
  });
  try {
    await ui.click('[data-page=accounts]');
    assert.equal(ui.doc.querySelector('[data-logout=chatgpt]').disabled, false);
    await ui.click('[data-logout=chatgpt]');
    await ui.click('#confirm-yes');
    await settleMany();
    const note = ui.doc.querySelector('#logout-note');
    assert.ok(note);
    assert.match(note.textContent, /不把它写成已退出/);
    assert.doesNotMatch(note.textContent, /^已退出/);
    assert.match(ui.doc.querySelector('[data-auth-feedback="chatgpt"]').textContent, /不把它写成已退出/);
    assert.doesNotMatch(ui.doc.body.textContent, /undefined/);
    assert.equal(ui.calls.filter((item) => item.path === '/api/auth/logout' && item.method === 'POST').length, 1);
  } finally { ui.dom.window.close(); }
});

test('a revoked logout can still say signed out', async () => {
  const ui = await setup({
    '/api/auth/status': {accounts: [{provider: 'chatgpt', signed_in: true, account: 'fixture@example.com'}, {provider: 'grok', signed_in: false}]},
    '/api/auth/logout': {outcome: 'revoked', zh: '已退出 ChatGPT 订阅，并完成服务端撤销。', cli: 'Signed out of ChatGPT.'},
  });
  try {
    await ui.click('[data-page=accounts]');
    await ui.click('[data-logout=chatgpt]');
    await ui.click('#confirm-yes');
    await settleMany();
    const note = ui.doc.querySelector('#logout-note');
    assert.match(note.textContent, /^已退出/);
    assert.doesNotMatch(note.textContent, /不把它写成已退出/);
    const detail = ui.doc.querySelector('details.account-details');
    assert.match(detail.textContent, /Signed out of ChatGPT/);
    assert.doesNotMatch(detail.textContent, /undefined/);
  } finally { ui.dom.window.close(); }
});

test('a not-signed-in logout keeps the server sentence', async () => {
  const ui = await setup({
    '/api/auth/status': {accounts: [{provider: 'chatgpt', signed_in: true, account: 'fixture@example.com'}]},
    '/api/auth/logout': {outcome: null, zh: '本来就没有登录 ChatGPT 订阅。', cli: 'Not signed in to ChatGPT.'},
  });
  try {
    await ui.click('[data-page=accounts]');
    await ui.click('[data-logout=chatgpt]');
    await ui.click('#confirm-yes');
    await settleMany();
    const note = ui.doc.querySelector('#logout-note');
    assert.match(note.textContent, /本来就没有登录/);
    assert.doesNotMatch(note.textContent, /不把它写成已退出|^已退出/);
  } finally { ui.dom.window.close(); }
});

test('an unrecognized login poll is not called finished', async () => {
  let posts = 0;
  const ui = await setup({'/api/auth/login': (options) => {
    if (options.method) { posts += 1; return {provider: 'chatgpt', status: 'waiting'}; }
    if (posts) return {status: 'retrying', zh: '登录已完成'};
    return {status: 'idle', messages: []};
  }});
  try {
    await ui.click('[data-page=accounts]');
    await ui.click('[data-login=chatgpt]');
    await new Promise((resolve) => setTimeout(resolve, 1100));
    for (let i = 0; i < 6; i++) await settle();
    const note = ui.doc.querySelector('#login-end-note');
    assert.ok(note);
    assert.match(note.textContent, /登录状态未知，不把它写成已结束或已完成/);
    assert.doesNotMatch(note.textContent, /登录流程已结束|登录已完成/);
    assert.match(ui.doc.querySelector('[data-auth-feedback="chatgpt"]').textContent, /不把它写成已结束或已完成/);
  } finally { ui.dom.window.close(); }
});

test('a finished login without a signed-in sentence is not called complete', async () => {
  let posts = 0;
  const ui = await setup({'/api/auth/login': (options) => {
    if (options.method) { posts += 1; return {provider: 'chatgpt', status: 'waiting'}; }
    return {status: 'idle', cli: 'auth-status chatgpt', messages: []};
  }});
  try {
    await ui.click('[data-page=accounts]');
    await ui.click('[data-login=chatgpt]');
    assert.match(ui.doc.querySelector('[data-auth-feedback="chatgpt"]').textContent, /登录已启动/);
    await new Promise((resolve) => setTimeout(resolve, 1100));
    for (let i = 0; i < 6; i++) await settle();
    const note = ui.doc.querySelector('#login-end-note');
    assert.ok(note, 'the ended login is explained');
    assert.match(note.textContent, /登录流程已结束，不把它写成已完成/);
    assert.doesNotMatch(ui.doc.body.textContent, /登录已完成/);
    assert.equal(posts, 1);
  } finally { ui.dom.window.close(); }
});

test('starting ChatGPT login enables the fallback callback form without a page refresh', async () => {
  const ui = await setup({'/api/auth/login': (options) => options.method ? {provider: 'chatgpt', status: 'waiting'} : {status: 'idle', messages: []}});
  try {
    await ui.click('[data-page=accounts]');
    assert.equal(ui.doc.querySelector('#paste-url').disabled, true);
    await ui.click('[data-login=chatgpt]');
    assert.equal(ui.doc.querySelector('#login-help').open, true);
    assert.equal(ui.doc.querySelector('#paste-url').disabled, false);
    assert.equal(ui.doc.querySelector('#paste-form [type=submit]').disabled, false);
  } finally { ui.dom.window.close(); }
});

test('every API call carries the session token the server put in the page (SEC-001)', async () => {
  const html = markup.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n  <meta name="tradingagents-token" content="fixture-token-123">');
  const ui = await setup({'/api/runs': (options) => options.method ? {id: 'fixture-run'} : {run: null, progress: {}}}, {html});
  try {
    const {calls} = ui;
    assert.ok(calls.length > 0);
    for (const call of calls) {
      assert.equal(call.headers['X-TradingAgents-Token'], 'fixture-token-123', call.path);
      assert.equal(call.headers['Content-Type'], 'application/json', call.path);
    }
  } finally {
    ui.window.close();
  }
});

test('report downloads name their file and ask the server for an attachment (WEBUI-004 AC3)', async () => {
  const dir = '/tmp/results/reports/NVDA_2026-10-01';
  const ui = await setup({'/api/reports': {reports: [{name: 'NVDA_2026-10-01', html: `${dir}/complete_report.html`, markdown: `${dir}/complete_report.md`}], live: [], directory: '/tmp/results'},
    '/api/reports/outline': {name: 'NVDA', rating: null, header: [], sections: []}});
  try {
    ui.ui.showPage('reports');
    for (let i = 0; i < 6; i++) await settle();
    const links = [...ui.doc.querySelectorAll('a[download]')];
    const md = links.find((a) => a.textContent === '下载 Markdown');
    const html = links.find((a) => a.textContent === '下载 HTML');
    assert.ok(md && html, 'download links rendered');
    assert.equal(md.getAttribute('download'), 'NVDA_2026-10-01_complete_report.md');
    assert.equal(html.getAttribute('download'), 'NVDA_2026-10-01_complete_report.html');
    assert.match(md.getAttribute('href'), /complete_report\.md&download=1&k=/);
  } finally {
    ui.dom.window.close();
  }
});

test('a table wider than its box scrolls with a visible hint instead of being cut off (WEBUI-005 ledger)', async () => {
  const ui = await setup();
  try {
    const {window, doc} = ui;
    const css = readFileSync(resolve(root, 'webui/static/app.css'), 'utf8');
    assert.match(css, /\.table-wrap \{ overflow-x: auto;/);
    assert.doesNotMatch(css, /\.table-wrap\s*\{[^}]*overflow(-x)?:\s*hidden/);
    await ui.click('[data-page=backtest]');
    doc.querySelector('#page').insertAdjacentHTML('beforeend', '<div class="table-wrap" id="wide"><table class="data-table"><tr><td>a</td></tr></table></div><div class="table-wrap" id="narrow"><table><tr><td>b</td></tr></table></div>');
    const wide = doc.querySelector('#wide');
    Object.defineProperty(wide, 'scrollWidth', {value: 900, configurable: true});
    Object.defineProperty(wide, 'clientWidth', {value: 300});
    ui.ui.markScrollableTables();
    assert.ok(wide.classList.contains('is-scrollable'));
    assert.equal(wide.getAttribute('tabindex'), '0', 'keyboard users can scroll it');
    assert.equal(wide.getAttribute('role'), 'region');
    assert.match(wide.previousElementSibling.textContent, /左右滑动查看全部列/);
    assert.equal(doc.querySelector('#narrow').classList.contains('is-scrollable'), false);
    assert.equal(doc.querySelector('#narrow').previousElementSibling.id, 'wide', 'no hint on a table that fits');
    ui.ui.markScrollableTables();
    assert.equal(doc.querySelectorAll('.scroll-hint').length, 1, 'the hint is not duplicated');
    Object.defineProperty(wide, 'scrollWidth', {value: 300});
    ui.ui.markScrollableTables();
    assert.equal(doc.querySelectorAll('.scroll-hint').length, 0, 'the hint goes away once the table fits');
    void window;
  } finally { ui.dom.window.close(); }
});

test('a page opened before the catalog arrives shows loading, then the page itself (QA W7)', async () => {
  let release;
  const pending = new Promise((done) => { release = done; });
  const ui = await setup({'/api/catalog': () => pending.then(() => catalog)});
  const errors = [];
  ui.window.addEventListener('error', (event) => errors.push(event.message));
  try {
    assert.equal(ui.ui.state.catalog, null);
    await ui.click('.nav-item[data-page=accounts]');
    assert.ok(ui.doc.querySelector('#page-loading'), 'accounts shows a loading state while the catalog is pending');
    assert.doesNotMatch(ui.doc.querySelector('#page').textContent, /Cannot read|providers|undefined/);
    release();
    for (let i = 0; i < 12; i++) await settle();
    assert.equal(ui.ui.state.page, 'accounts');
    assert.equal(ui.doc.querySelector('#page-loading'), null, 'the loading state is replaced once the catalog is ready');
    assert.match(ui.doc.querySelector('#page').textContent, /ChatGPT/);
    assert.deepEqual(errors, []);
  } finally { ui.dom.window.close(); }
});

test('a restarted console is noticed before the user acts and asks for a refresh (QA W6)', async () => {
  let stale = false;
  const ui = await setup({'/api/session': () => stale
    ? {__status: 403, body: {error: '缺少或错误的控制台会话令牌。请刷新控制台页面后重试。'}}
    : {ok: true}});
  try {
    assert.equal(ui.doc.querySelector('#session-banner'), null, 'a valid session shows no banner');
    stale = true;
    ui.window.dispatchEvent(new ui.window.Event('focus'));
    for (let i = 0; i < 6; i++) await settle();
    const banner = ui.doc.querySelector('#session-banner');
    assert.ok(banner, 'returning to the tab probes the session and shows the banner');
    assert.equal(banner.getAttribute('role'), 'alert');
    assert.match(banner.textContent, /刷新页面/);
    assert.ok(ui.calls.some((item) => item.path === '/api/session'));
    const before = ui.calls.filter((item) => item.path === '/api/session').length;
    ui.window.dispatchEvent(new ui.window.Event('focus'));
    for (let i = 0; i < 4; i++) await settle();
    assert.equal(ui.calls.filter((item) => item.path === '/api/session').length, before, 'no more probes once the session is known stale');
    assert.equal(ui.doc.querySelectorAll('#session-banner').length, 1);
  } finally { ui.dom.window.close(); }
});


// ---- RUN-004 / WEBUI-006 item 3: several runs on 查看状态 (Proto mock.html -v4)

function multiRuns({selected = 'run-btc'} = {}) {
  const runs = [
    {id: 'run-btc', kind: 'analysis', status: 'running', ticker: 'BTC-USD', date: '2026-10-30', started_at: '2026-10-31T13:52:00', done: 6, total: 11, team_zh: '研究团队'},
    {id: 'run-nvda', kind: 'analysis', status: 'running', ticker: 'NVDA', date: '2026-10-30', started_at: '2026-10-31T14:01:00', done: 2, total: 11, team_zh: '分析师团队'},
    {id: 'run-eth', kind: 'analysis', status: 'queued', ticker: 'ETH-USD', date: '2026-10-30', queued_at: '2026-10-31T14:08:00', position: 1, done: 0, total: 11, team_zh: '等待开始'},
    {id: 'run-sol', kind: 'analysis', status: 'queued', ticker: 'SOL-USD', date: '2026-10-30', queued_at: '2026-10-31T14:09:00', position: 2, done: 0, total: 11, team_zh: '等待开始'},
    {id: 'run-tsla', kind: 'analysis', status: 'failed', ticker: 'TSLA', date: '2026-10-30', started_at: '2026-10-31T13:10:00', done: 3, total: 11, team_zh: '研究团队'},
    {id: 'run-aapl', kind: 'analysis', status: 'finished', ticker: 'AAPL', date: '2026-10-29', started_at: '2026-10-31T12:40:00', done: 11, total: 11, team_zh: '全部完成'},
  ];
  const chosen = runs.find((item) => item.id === selected) || runs[0];
  const agents = [
    {team: 'Analyst Team', team_zh: '分析师团队', agent: 'Market Analyst', zh: '市场分析师', status: chosen.status === 'queued' ? 'pending' : 'completed', status_zh: ''},
    {team: 'Research Team', team_zh: '研究团队', agent: 'Bull Researcher', zh: '多头研究员', status: chosen.status === 'queued' ? 'pending' : 'in_progress', status_zh: ''},
  ];
  return {
    runs, selected: chosen.id, limits: {max_running: 2, max_queued: 5},
    counts: {all: 6, running: 2, queued: 2, failed: 1, finished: 1},
    run: {...chosen, selections: {ticker: chosen.ticker, date: chosen.date, research_depth: 3}},
    progress: {agents, timeline: []}, result: {present: false},
  };
}

function multiSetup(extra = {}) {
  const posts = [];
  const routes = {
    '/api/runs': (options) => multiRuns(),
    '/api/runs/cancel': (options) => { posts.push(['cancel', JSON.parse(options.body)]); return {zh: '已取消排队。'}; },
    '/api/runs/stop': (options) => { posts.push(['stop', JSON.parse(options.body)]); return {status: 'stopped'}; },
    ...extra,
  };
  return setup(routes).then((ui) => {
    ui.posts = posts;
    ui.window.fetch = ((inner) => async (path, options = {}) => {
      const id = new URL(path, 'http://localhost/').searchParams.get('id');
      if (path.startsWith('/api/runs?id=')) {
        ui.calls.push({path, ...options});
        return {ok: true, json: async () => structuredClone(multiRuns({selected: id}))};
      }
      return inner(path, options);
    })(ui.window.fetch);
    return ui;
  });
}

test('the run list limit note breaks before the cap, not inside the number', async () => {
  const ui = await setup({'/api/runs': {run: null, runs: [], progress: {}, counts: {all: 0, running: 0, queued: 0, failed: 0, finished: 0}, limits: {max_running: 2, max_queued: 5}}});
  try {
    await ui.click('[data-page=runs]');
    const description = ui.doc.querySelector('.page-description');
    assert.equal(description.textContent, '0 个运行中 · 0 个排队中（最多同时运行 2 个，最多排队 5 个）');
    const html = description.innerHTML;
    assert.match(html, /排队中<br class="notice-break">（最多同时运行 2 个，<br class="notice-break">最多排队 5 个）/);
    assert.doesNotMatch(html, /排队<br/);
  } finally { ui.dom.window.close(); }
});

test('several runs show a filtered list with real counts and no detail until a row is clicked; the nav badge counts running (RUN-004, WEBUI-007)', async () => {
  const ui = await multiSetup();
  try {
    const {doc} = ui;
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    assert.equal(doc.querySelector('.page-description').textContent, '2 个运行中 · 2 个排队中（最多同时运行 2 个，最多排队 5 个）');
    assert.deepEqual([...doc.querySelectorAll('#run-filters .ftab')].map((tab) => tab.textContent), ['全部6', '运行中2', '排队中2', '失败1', '已结束1']);
    const rows = [...doc.querySelectorAll('#run-list .tr[data-run-id]')];
    assert.deepEqual(rows.map((row) => row.dataset.runId), ['run-btc', 'run-nvda', 'run-eth', 'run-sol', 'run-tsla', 'run-aapl']);
    assert.equal(rows.filter((row) => row.classList.contains('on')).length, 0, 'nothing is highlighted before a run was opened');
    assert.match(rows[2].textContent, /排队中/);
    assert.match(rows[2].textContent, /0\/11/);
    assert.match(rows[2].textContent, /14:08 提交/);
    assert.ok(rows[2].querySelector('.qx'), 'a queued row has the red 取消');
    assert.equal(rows[0].querySelector('.qx'), null);
    assert.match(rows[5].textContent, /已结束/);
    assert.doesNotMatch(rows[5].textContent, /已完成/);
    assert.equal(rows[5].querySelector('.pill').className.includes('ok'), false, 'a finished process is not a confirmed completion');
    assert.equal(doc.querySelector('#run-detail'), null, 'the list page shows no 任务详情 (WEBUI-007 item 1)');
    assert.equal(doc.querySelector('#run-detail-title'), null);
    assert.equal(doc.querySelector('#runs-back'), null);
    assert.equal(doc.querySelector('.nav-item[data-page=runs] .nav-badge').textContent, '2');
    await ui.click('#run-filters [data-filter="queued"]');
    await settleMany();
    assert.deepEqual([...doc.querySelectorAll('#run-list .tr[data-run-id]')].map((row) => row.dataset.runId), ['run-eth', 'run-sol']);
    await ui.click('#run-filters [data-filter="failed"]');
    await settleMany();
    assert.deepEqual([...doc.querySelectorAll('#run-list .tr[data-run-id]')].map((row) => row.dataset.runId), ['run-tsla']);
    await ui.click('#run-list [data-run-id="run-tsla"]');
    await settleMany();
    assert.ok(ui.calls.some((call) => call.path === '/api/runs?id=run-tsla'), 'choosing a row asks for that run');
    assert.match(doc.querySelector('#run-detail-head').textContent, /TSLA/);
    assert.equal(doc.querySelector('#run-list'), null, 'the detail replaces the list');
    await ui.click('#runs-back');
    await waitHistory();
    assert.deepEqual([...doc.querySelectorAll('#run-list .tr[data-run-id]')].map((row) => row.dataset.runId), ['run-tsla'], 'the filter is kept');
    assert.ok(doc.querySelector('#run-list [data-run-id="run-tsla"]').classList.contains('on'), 'the run just viewed stays highlighted');
  } finally { ui.dom.window.close(); }
});

test('a stop response without a status is not called stopped', async () => {
  const running = {id: 'run-live', status: 'running', kind: 'analysis', ticker: 'AAPL', date: '2026-11-11', selections: {ticker: 'AAPL'}, started_at: '2026-11-11T09:00:00'};
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {run: running, runs: [running], progress: {agents: []}, result: {present: false}, counts: {all: 1, running: 1, queued: 0, failed: 0, finished: 0}}, '/api/runs/stop': {id: 'run-live', zh: '任务已停止。'}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'run-live');
    await ui.click('#stop-run');
    await ui.click('#confirm-yes');
    await settleMany();
    const note = [...ui.doc.querySelectorAll('[data-kept-notice]')].map((item) => item.textContent).join('\n');
    assert.match(note, /停止结果状态未知，不把它写成已停止/);
    assert.doesNotMatch(note, /^任务已停止/);
  } finally { ui.dom.window.close(); }
});

test('a stopped backtest is not called a stopped analysis', async () => {
  const running = {id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}, started_at: '2026-11-12T09:00:00'};
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {run: running, runs: [running], progress: {agents: []}, result: {present: false}, counts: {all: 1, running: 1, queued: 0, failed: 0, finished: 0}}, '/api/runs/stop': {id: 'bt-live', status: 'stopped', kind: 'backtest', zh: '正在停止控制台分析。'}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'bt-live');
    await ui.click('#stop-run');
    assert.match(ui.doc.body.textContent, /停止当前回测/);
    await ui.click('#confirm-yes');
    await settleMany();
    const note = [...ui.doc.querySelectorAll('[data-kept-notice]')].map((item) => item.textContent).join('\n');
    assert.match(note, /回测已停止，不是正常完成。不把它写成分析/);
    assert.doesNotMatch(note, /控制台分析|正在停止控制台/);
  } finally { ui.dom.window.close(); }
});

test('a stopped response still says the task stopped', async () => {
  const running = {id: 'run-live', status: 'running', kind: 'analysis', ticker: 'AAPL', date: '2026-11-11', selections: {ticker: 'AAPL'}, started_at: '2026-11-11T09:00:00'};
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {run: running, runs: [running], progress: {agents: []}, result: {present: false}, counts: {all: 1, running: 1, queued: 0, failed: 0, finished: 0}}, '/api/runs/stop': {id: 'run-live', status: 'stopped', zh: '任务已停止。不是正常完成。'}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'run-live');
    await ui.click('#stop-run');
    await ui.click('#confirm-yes');
    await settleMany();
    const note = [...ui.doc.querySelectorAll('[data-kept-notice]')].map((item) => item.textContent).join('\n');
    assert.match(note, /^任务已停止/);
    assert.doesNotMatch(note, /状态未知/);
  } finally { ui.dom.window.close(); }
});

test('a cancel response without a status is not called cancelled', async () => {
  const queued = {id: 'run-q', status: 'queued', kind: 'analysis', ticker: 'AAPL', date: '2026-11-11', selections: {ticker: 'AAPL'}, position: 1};
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {run: queued, runs: [queued], progress: {agents: []}, result: {present: false}, counts: {all: 1, running: 0, queued: 1, failed: 0, finished: 0}}, '/api/runs/cancel': {id: 'run-q', zh: '已取消排队。'}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'run-q');
    await ui.click('#cancel-queued');
    await settleMany();
    const note = [...ui.doc.querySelectorAll('[data-kept-notice]')].map((item) => item.textContent).join('\n');
    assert.match(note, /取消结果状态未知，不把它写成已取消排队/);
    assert.doesNotMatch(note, /^已取消排队|这个任务没有启动/);
  } finally { ui.dom.window.close(); }
});

test('a cancelled queue response still says it was cancelled', async () => {
  const queued = {id: 'run-q', status: 'queued', kind: 'analysis', ticker: 'AAPL', date: '2026-11-11', selections: {ticker: 'AAPL'}, position: 1};
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {run: queued, runs: [queued], progress: {agents: []}, result: {present: false}, counts: {all: 1, running: 0, queued: 1, failed: 0, finished: 0}}, '/api/runs/cancel': {id: 'run-q', status: 'cancelled', zh: '已取消排队。这个任务没有启动。'}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'run-q');
    await ui.click('#cancel-queued');
    await settleMany();
    const note = [...ui.doc.querySelectorAll('[data-kept-notice]')].map((item) => item.textContent).join('\n');
    assert.match(note, /已取消排队/);
    assert.doesNotMatch(note, /状态未知/);
  } finally { ui.dom.window.close(); }
});

test('a queued run shows 排队第 N 位 with grey teams, and 取消排队 or the row 取消 cancels that run only (RUN-004)', async () => {
  const ui = await multiSetup();
  try {
    const {doc} = ui;
    await openRunFromList(ui, 'run-sol');
    assert.match(doc.querySelector('#run-detail-head').textContent, /SOL-USD · 2026-10-30/);
    assert.match(doc.querySelector('#run-detail-head').textContent, /14:09 提交/);
    assert.equal(doc.querySelector('#queue-note').textContent.trim(), '排队第 2 位，前面有任务结束后自动开始。');
    const teams = [...doc.querySelectorAll('#run-detail .team')];
    assert.ok(teams.length > 0);
    assert.ok(teams.every((team) => /等待/.test(team.textContent)), 'every team waits');
    assert.equal(doc.querySelector('#stop-run'), null, 'a queued run has nothing to stop');
    await ui.click('#cancel-queued');
    await settleMany();
    assert.deepEqual(ui.posts.at(-1), ['cancel', {id: 'run-sol'}]);
    assert.ok(doc.querySelector('#runs-back'), '取消排队 keeps you on that run');
    await ui.click('#runs-back');
    await waitHistory();
    const before = ui.calls.filter((call) => call.path === '/api/runs?id=run-eth').length;
    await ui.click('#run-list [data-run-id="run-eth"] .qx');
    await settleMany();
    assert.deepEqual(ui.posts.at(-1), ['cancel', {id: 'run-eth'}]);
    assert.equal(ui.calls.filter((call) => call.path === '/api/runs?id=run-eth').length, before, 'the row 取消 does not open the run');
    assert.equal(doc.querySelector('#runs-back'), null, 'the row 取消 stays on the list');
    assert.ok(ui.posts.every(([kind]) => kind === 'cancel'), 'cancelling stops nothing');
  } finally { ui.dom.window.close(); }
});

test('停止任务 and 查看日志 name the run on screen (RUN-004)', async () => {
  const ui = await multiSetup({'/api/logs': {lines: ['x'], path: '/tmp/x.log'}});
  try {
    await openRunFromList(ui, 'run-nvda');
    const details = ui.doc.querySelector('#run-log-details');
    details.open = true;
    details.dispatchEvent(new ui.window.Event('toggle'));
    await settleMany();
    const logCall = ui.calls.find((call) => call.path.startsWith('/api/logs?'));
    assert.ok(logCall, 'opening the log reads it');
    assert.match(logCall.path, /[?&]id=run-nvda/);
    await ui.click('#stop-run');
    await ui.click('#confirm-yes');
    await settleMany();
    assert.deepEqual(ui.posts.find(([kind]) => kind === 'stop'), ['stop', {id: 'run-nvda'}]);
  } finally { ui.dom.window.close(); }
});

test('on a phone the runs are cards; a card opens 任务详情 with 「‹ 全部任务」 back to the list (RUN-004)', async () => {
  const ui = await multiSetup();
  try {
    const {doc} = ui;
    ui.window.matchMedia = (query) => ({matches: /max-width: 600px/.test(query), addEventListener() {}, removeEventListener() {}});
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    assert.ok(doc.querySelector('.cards#run-list'));
    assert.equal(doc.querySelector('.tl'), null);
    assert.equal(doc.querySelector('#run-detail'), null, 'the phone list does not repeat the detail');
    assert.match(doc.querySelector('.tc[data-run-id="run-btc"] .b').textContent, /2026-10-30 · 研究团队 · 开始 13:52/);
    await ui.click('.tc[data-run-id="run-nvda"]');
    await settleMany();
    assert.equal(doc.querySelector('#runs-back').textContent, '‹ 全部任务');
    assert.match(doc.querySelector('#run-detail-head').textContent, /NVDA/);
    assert.equal(doc.querySelector('#run-list'), null);
    await ui.click('#runs-back');
    await waitHistory();
    assert.ok(doc.querySelector('.cards#run-list'));
  } finally { ui.dom.window.close(); }
});

test('one run is still only the list (one row) until it is clicked (WEBUI-007 item 1, PM)', async () => {
  const one = multiRuns();
  const ui = await setup({'/api/runs': {...one, runs: [one.runs[0]], counts: {all: 1, running: 1, queued: 0, failed: 0, finished: 0}}});
  try {
    const {doc} = ui;
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    const rows = [...doc.querySelectorAll('#run-list .tr[data-run-id]')];
    assert.equal(rows.length, 1, 'a single run is a one-row list, not the old single-task page');
    assert.equal(rows[0].dataset.runId, one.runs[0].id);
    assert.match(doc.querySelector('.page-heading .page-description').textContent, /^1 个运行中 · 0 个排队中/);
    assert.equal(doc.querySelector('#run-filters [data-filter=all] b').textContent, '1');
    assert.equal(doc.querySelector('#run-detail'), null, 'even a single run is not opened by itself');
    assert.equal(doc.querySelector('#stop-run'), null);
    await ui.click('#run-list [data-run-id]');
    await settleMany();
    assert.ok(doc.querySelector('#run-detail #run-detail-head'), 'the detail uses the v4 head');
    assert.ok(doc.querySelector('#run-detail #stop-run'));
    assert.equal(doc.querySelector('.nav-item[data-page=runs] .nav-badge').textContent, '1');
  } finally { ui.dom.window.close(); }
});

test('one run on a phone is one card that opens 任务详情 (RUN-004, PM ruling)', async () => {
  const one = multiRuns();
  const ui = await setup({'/api/runs': {...one, runs: [one.runs[0]], counts: {all: 1, running: 1, queued: 0, failed: 0, finished: 0}}});
  try {
    const {doc} = ui;
    ui.window.matchMedia = (query) => ({matches: /max-width: 600px/.test(query), addEventListener() {}, removeEventListener() {}});
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    assert.equal(doc.querySelectorAll('#run-list .tc').length, 1);
    await ui.click('#run-list .tc');
    await settleMany();
    assert.ok(doc.querySelector('#runs-back'));
    assert.ok(doc.querySelector('#run-detail #run-detail-head'));
  } finally { ui.dom.window.close(); }
});

test('no runs shows the list frame with zero counts and an empty state that starts an analysis (RUN-004, PM ruling)', async () => {
  const ui = await setup({'/api/runs': {run: null, progress: {}, runs: [], counts: {all: 0, running: 0, queued: 0, failed: 0, finished: 0}, limits: {max_running: 2, max_queued: 5}}});
  try {
    const {doc} = ui;
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    assert.ok(doc.querySelector('#run-filters'), 'the filter tabs are there');
    assert.deepEqual([...doc.querySelectorAll('#run-filters .ftab b')].map((b) => b.textContent), ['0', '0', '0', '0', '0']);
    assert.ok(doc.querySelector('#run-list.tl .tr.th'), 'the table frame keeps its header');
    assert.match(doc.querySelector('#runs-empty').textContent, /还没有任务/);
    assert.doesNotMatch(doc.querySelector('#runs-empty').textContent, /还没有分析任务/);
    assert.match(doc.querySelector('#runs-empty').textContent, /启动分析或回测后/);
    assert.match(doc.querySelector('#runs-empty').textContent, /最多同时运行 2 个，其余自动排队/);
    assert.equal(doc.querySelector('#run-detail'), null, 'no 任务详情 without a run');
    assert.equal(doc.querySelector('.nav-item[data-page=runs] .nav-badge'), null);
    await ui.click('#start-analysis-from-runs');
    assert.equal(ui.ui.state.page, 'analyze');
  } finally { ui.dom.window.close(); }
});

test('a queued start says 已加入队列 with the real number ahead (RUN-004, mock2 ?s=okq)', async () => {
  const ui = await setup({'/api/runs': (options) => options.method
    ? {id: 'run-q', status: 'queued', queue_ahead: 3, ticker: 'BTC-USD', date: '2026-10-30', selections: {asset_type: 'crypto'}, ignored: []}
    : {run: null, progress: {}, runs: [], counts: {running: 2}}});
  try {
    await fillToReview(ui);
    await ui.click('#run-analysis');
    await settleMany();
    assert.equal(ui.doc.querySelector('#launch-title').textContent, '已加入队列');
    assert.equal(ui.doc.querySelector('#launch-lead').textContent, '前面还有 3 个任务在排队，轮到时自动开始。');
    assert.equal(ui.doc.querySelector('#launch-dialog').classList.contains('launch-dialog--wait'), true);
    assert.doesNotMatch(ui.doc.querySelector('.launch-mark').textContent, /✓/);
    assert.match(ui.doc.querySelector('#launch-run-info').textContent, /run-q/);
    assert.ok(ui.doc.querySelector('#launch-another'));
    assert.ok(ui.doc.querySelector('#launch-open-runs'));
    assert.equal(ui.doc.querySelector('.nav-item[data-page=runs] .nav-badge').textContent, '2');
  } finally { ui.dom.window.close(); }
});

test('a full queue says 队列已满 with 返回修改 / 查看状态 and keeps the form (RUN-004, mock2 ?s=limit)', async () => {
  const reason = '队列已满：2 个任务运行中，5 个在排队（最多同时运行 2 个，最多排队 5 个）。等排队的任务开始后再启动。';
  const ui = await setup({'/api/runs': (options) => options.method ? {__status: 409, body: {error: reason}} : {run: null, progress: {}}});
  try {
    const {doc} = ui;
    await fillToReview(ui);
    await ui.click('#run-analysis');
    await settleMany();
    assert.equal(doc.querySelector('#launch-title').textContent, '队列已满');
    assert.match(doc.querySelector('#launch-error').textContent, /^2 个任务运行中，5 个在排队/);
    assert.match(doc.querySelector('#launch-error').innerHTML, /2 个任务运行中，<br class="notice-break">5 个在排队<br>（最多同时运行 2 个，<br class="notice-break">最多排队 5 个）。<br>等排队的任务开始后再启动。/);
    assert.match(doc.querySelector('#launch-dialog').textContent, /你填写的设置都保留着/);
    assert.deepEqual([...doc.querySelectorAll('#launch-dialog .button')].map((b) => b.textContent), ['返回修改', '查看状态']);
    assert.ok(doc.querySelector('#launch-open-runs').classList.contains('button--primary'));
    assert.doesNotMatch(doc.querySelector('#form-note').textContent, /未启动|队列已满|启动失败/, 'no inline note duplicates the dialog');
    assert.doesNotMatch(doc.querySelector('[data-step-panel="4"]').closest('form, .wizard-panel, main').textContent, /未启动：/);
    await ui.click('#launch-close');
    assert.equal(doc.querySelector('#launch-dialog'), null);
    assert.equal(ui.ui.state.step, 4);
    assert.equal(ui.doc.querySelector('#ticker').value, 'btc-usd', 'the form is kept');
    await ui.click('#run-analysis');
    await settleMany();
    await ui.click('#launch-open-runs');
    await settleMany();
    assert.equal(ui.ui.state.page, 'runs');
  } finally { ui.dom.window.close(); }
});

test('a full queue keeps 最多, 可以 and 一个 together on narrow and wide breaks', async () => {
  const reason = '队列已满：1 个任务运行中，1 个停止中，5 个在排队（最多同时运行 2 个，最多排队 5 个）。等排队的任务开始后再启动，也可以在查看状态里取消一个排队的任务。';
  const ui = await setup({'/api/runs': (options) => options.method ? {__status: 409, body: {error: reason}} : {run: null, progress: {}}});
  try {
    await fillToReview(ui);
    await ui.click('#run-analysis');
    await settleMany();
    const error = ui.doc.querySelector('#launch-error');
    assert.equal(error.textContent, '1 个任务运行中，1 个停止中，5 个在排队（最多同时运行 2 个，最多排队 5 个）。等排队的任务开始后再启动，也可以在查看状态里取消一个排队的任务。');
    assert.equal(error.innerHTML, '1 个任务运行中，<br class="notice-break">1 个停止中，<br class="notice-break">5 个在排队<br>（最多同时运行 2 个，<br class="notice-break">最多排队 5 个）。<br>等排队的任务开始后再启动，<br>也可以在查看状态里取消一个排队的任务。');
    assert.doesNotMatch(error.innerHTML, /也可以<br|取消一<br|最多同<br|查<br>看/);
  } finally { ui.dom.window.close(); }
});

test('the status page keeps polling while a run waits in the queue (RUN-004)', async () => {
  const ui = await multiSetup();
  try {
    let polls = 0;
    const realSet = ui.window.setInterval;
    ui.window.setInterval = (fn, ms) => { polls += 1; return realSet(() => {}, 1e9); };
    const data = multiRuns();
    data.runs = data.runs.map((item) => item.status === 'running' ? {...item, status: 'finished'} : item);
    data.counts.running = 0;
    data.run = {...data.run, status: 'finished'};
    ui.window.fetch = ((inner) => async (path, options = {}) => path.startsWith('/api/runs') && !options.method
      ? {ok: true, json: async () => structuredClone(data)} : inner(path, options))(ui.window.fetch);
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    assert.ok(polls > 0, 'a queued run keeps the page refreshing');
  } finally { ui.dom.window.close(); }
});

// --- WEBUI-006 PM holdings rule: an imported holdings JSON is used, step 1 is read-only ---
const HOLDING_BOOK = {cash: 10000, currency: 'USD', positions: [
  {ticker: 'NVDA', quantity: 5, average_price: 120}, {ticker: 'AAPL', quantity: 10, average_price: 150}]};

async function importHoldings(ui, book) {
  await ui.click('#open-save-drawer');
  ui.doc.querySelector('#portfolio-json-details').open = true;
  ui.fill('#portfolio', JSON.stringify(book));
  await ui.click('#save-drawer-save');
  await settleMany();
  assert.equal(ui.doc.querySelector('.save-drawer'), null, 'the drawer saved and closed');
}

async function heldToReview(ui) {
  ui.fill('#ticker', 'BTC-USD'); ui.fill('#date', '2026-10-30');
  await ui.click('#held-seg [data-held="true"]');
  ui.fill('#holding-price', '62000'); ui.fill('#holding-quantity', '0.5');
  await ui.click('#step-next');
  ui.ui.state.form.analysts = ['market'];
  await ui.click('#step-next');
  ui.fill('#quick-model', 'fixture-model'); ui.fill('#deep-model', 'fixture-model');
  await ui.click('#step-next');
  assert.equal(ui.ui.state.step, 4);
}

const reviewHolding = (ui) => ui.doc.querySelector('#analysis-summary .sec').textContent;

test('an imported holdings JSON replaces step 1: step 1 turns read-only, never blocks, and 清除导入 brings it back (WEBUI-006 PM rule)', async () => {
  const ui = await setup({'/api/runs': (options) => options.method ? {id: 'fixture-run', status: 'running'} : {run: null, progress: {}}});
  try {
    const {doc} = ui;
    await heldToReview(ui);
    assert.match(reviewHolding(ui), /持仓已持仓 · 成本 62000 · 0\.5/);
    await importHoldings(ui, HOLDING_BOOK);
    assert.match(reviewHolding(ui), /持仓来自导入 JSON · 2 个持仓 · 现金 10000 USD/, 'the confirm page names the JSON as the source');
    assert.doesNotMatch(reviewHolding(ui), /成本 62000/);
    assert.deepEqual(JSON.parse(JSON.stringify(ui.ui.readForm().portfolio)), HOLDING_BOOK, 'the JSON (cash and every ticker) is what would be sent');
    // Step 1 is read-only while the import is in use.
    await ui.click('[data-step="1"]');
    assert.equal(ui.ui.state.step, 1);
    assert.equal(doc.querySelector('#holding-imported').textContent.trim().startsWith('已从 JSON 导入持仓，第 1 步填的不再使用'), true);
    assert.equal(doc.querySelector('#holding-price').disabled, true);
    assert.equal(doc.querySelector('#holding-quantity').disabled, true);
    assert.equal(doc.querySelector('#holding-price').value, '62000', 'step 1 keeps what was typed, just not used');
    assert.ok([...doc.querySelectorAll('#held-seg [data-held]')].every((button) => button.disabled), '是否持仓 cannot be changed');
    await ui.click('#held-seg [data-held="false"]');
    assert.equal(ui.ui.state.holding.held, true, 'a click on the locked switch changes nothing');
    // 清除导入 gives step 1 back, editable, with what was there before.
    await ui.click('#clear-holding-import');
    assert.equal(ui.ui.state.portfolioText, '');
    assert.equal(doc.querySelector('#holding-imported'), null);
    assert.equal(doc.querySelector('#holding-price').disabled, false);
    assert.equal(doc.querySelector('#holding-price').value, '62000');
    assert.equal(doc.querySelector('#holding-quantity').value, '0.5');
    assert.ok([...doc.querySelectorAll('#held-seg [data-held]')].every((button) => !button.disabled));
    assert.deepEqual(JSON.parse(JSON.stringify(ui.ui.readForm().portfolio)), {positions: [{ticker: 'BTC-USD', average_price: 62000, quantity: 0.5}]});
    await ui.click('#held-seg [data-held="false"]');
    assert.equal(ui.ui.state.holding.held, false, 'after 清除导入 the switch works again');
    await ui.click('#held-seg [data-held="true"]');
    // Import again; a missing step-1 price must not block while the JSON is in use.
    ui.fill('#holding-price', '');
    await ui.click('#step-next');
    assert.equal(ui.ui.state.step, 1, 'without an import a missing price blocks step 1');
    ui.ui.state.portfolioText = JSON.stringify(HOLDING_BOOK);
    ui.ui.paintHolding();
    assert.ok(doc.querySelector('#holding-imported'));
    await ui.click('#step-next');
    assert.equal(ui.ui.state.step, 2, 'step 1 never blocks on a missing price while the JSON is used');
    await ui.click('#step-next');
    await ui.click('#step-next');
    assert.equal(ui.ui.state.step, 4);
    assert.match(reviewHolding(ui), /持仓来自导入 JSON · 2 个持仓 · 现金 10000 USD/);
    await ui.click('#run-analysis');
    await settleMany();
    const post = ui.calls.find((item) => item.path === '/api/runs' && item.method === 'POST');
    assert.ok(post, 'the run started');
    assert.deepEqual(JSON.parse(post.body).portfolio, HOLDING_BOOK, 'the imported JSON overrides step 1');
  } finally { ui.dom.window.close(); }
});

test('the confirm page names the holdings source: step 1, 未持仓, or the imported JSON (WEBUI-006 PM rule)', async () => {
  const ui = await setup();
  try {
    await fillToReview(ui);
    const shown = (holding, text) => {
      ui.ui.state.holding = holding; ui.ui.state.portfolioText = text || '';
      ui.ui.paintReview();
      return reviewHolding(ui).replace(/^.*?持仓/s, '持仓');
    };
    assert.match(shown({}), /持仓未持仓/);
    assert.match(shown({held: true, average_price: '62000', quantity: '0.5'}), /持仓已持仓 · 成本 62000 · 0\.5/);
    assert.match(shown({held: true, average_price: '62000'}), /持仓已持仓 · 成本 62000 · 数量未填/);
    assert.match(shown({held: true, average_price: '62000'}, JSON.stringify(HOLDING_BOOK)), /持仓来自导入 JSON · 2 个持仓 · 现金 10000 USD/);
    assert.match(shown({}, '{"positions": [{"ticker": "NVDA", "average_price": 120}]}'), /持仓来自导入 JSON · 1 个持仓(?! · 现金)/);
    assert.match(shown({}, '{"cash": 500, "positions": []}'), /持仓来自导入 JSON · 0 个持仓 · 现金 500/);
  } finally { ui.dom.window.close(); }
});

test('after a successful held start with an import, the reset clears the import and 是否持仓 is back to 未持仓 with no price (WEBUI-006)', async () => {
  const ui = await setup({'/api/runs': (options) => options.method ? {id: 'fixture-run', ticker: 'BTC-USD', status: 'running'} : {run: null, progress: {}}});
  try {
    const {doc} = ui;
    await heldToReview(ui);
    await ui.click('#run-analysis');
    await settleMany();
    assert.match(doc.querySelector('#launch-title').textContent, /已启动分析/);
    const held = () => doc.querySelector('#held-seg [aria-pressed="true"]')?.textContent;
    assert.equal(held(), '未持仓', '是否持仓 starts over at 未持仓');
    assert.equal(doc.querySelector('#holding-price'), null);
    await ui.click('#launch-another');
    await ui.click('#held-seg [data-held="true"]');
    assert.equal(doc.querySelector('#holding-price').value, '', 'the old price is gone');
    assert.equal(doc.querySelector('#holding-quantity').value, '');
    // The same after a start that used an imported JSON.
    await ui.click('#held-seg [data-held="false"]');
    await heldToReview(ui);
    await importHoldings(ui, HOLDING_BOOK);
    await ui.click('#run-analysis');
    await settleMany();
    assert.equal(ui.ui.state.portfolioText, '', 'the import is cleared too');
    assert.equal(doc.querySelector('#holding-imported'), null, 'step 1 is editable again');
    assert.equal(held(), '未持仓');
    assert.ok([...doc.querySelectorAll('#held-seg [data-held]')].every((button) => !button.disabled));
  } finally { ui.dom.window.close(); }
});

test('任务详情 shows the quick and deep models the run was started with (WEBUI-006, PM)', async () => {
  const ui = await setup({'/api/logs': {lines: []}});
  try {
    ui.routes['/api/runs'] = {
      run: {id: 'models', status: 'running', kind: 'analysis', started_at: '2026-10-30T21:00:00',
        selections: {ticker: 'NVDA', date: '2026-10-01', llm_provider: 'openai', quick_think_provider: 'openai', deep_think_provider: 'anthropic', quick_think_llm: 'gpt-quick-x', deep_think_llm: 'claude-deep-y'}},
      progress: {agents: []},
    };
    await openRunFromList(ui, 'models');
    const models = ui.doc.querySelector('#run-models');
    assert.ok(models);
    const lines = [...models.querySelectorAll('.model-line')].map((line) => line.textContent);
    assert.equal(lines.length, 2);
    assert.match(lines[0], /^快 .+ · gpt-quick-x$/);
    assert.match(lines[1], /^深 .+ · claude-deep-y$/);
    assert.notEqual(lines[0].replace(/ · .*/, ''), lines[1].replace(/ · .*/, '').replace('深', '快'), 'each tier names its own provider');
    assert.doesNotMatch(models.textContent, /—/);
  } finally { ui.dom.window.close(); }
});

test('the empty runs frame is a solid line at 1440 and on a phone (WEBUI-006, PM)', async () => {
  const ui = await setup();
  try {
    const rules = styleRules(ui.window);
    const borderOf = (selector) => rules.filter((rule) => selectorMatches(rule, selector))
      .map((rule) => normalizeCssValue(rule.style.border || rule.style.borderStyle)).filter(Boolean);
    const wide = borderOf('.tl');
    assert.ok(wide.length && wide.every((value) => /solid/.test(value)), `.tl frame: ${wide}`);
    const phone = borderOf('.cards .runs-empty');
    assert.ok(phone.length, 'the phone empty state has its own frame');
    assert.ok(phone.every((value) => /solid/.test(value) && !/dashed|dotted/.test(value)), `.cards .runs-empty frame: ${phone}`);
    assert.ok(!rules.some((rule) => /runs-empty/.test(rule.selectorText || '') && /dashed|dotted/.test(rule.style.cssText)), 'no dashed empty-state rule anywhere');
  } finally { ui.dom.window.close(); }
});

test('停止任务 only exists on an opened run and names that run, never an empty body (W36, PM, WEBUI-007)', async () => {
  const ui = await multiSetup();
  try {
    assert.equal(ui.ui.state.selectedRunId ?? null, null);
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    assert.equal(ui.doc.querySelector('#stop-run'), null, 'the list has no 停止任务');
    await openRunFromList(ui, 'run-btc');
    await ui.click('#stop-run');
    await ui.click('#confirm-yes');
    await settleMany();
    assert.deepEqual(ui.posts.find(([kind]) => kind === 'stop'), ['stop', {id: 'run-btc'}]);
  } finally { ui.dom.window.close(); }
});

test('a run without an id is never stopped, restarted, resumed or cancelled — nothing is posted and the reason is shown (W36, PM)', async () => {
  const ui = await setup({'/api/logs': {lines: []}});
  try {
    const posted = () => ui.calls.filter((call) => /^\/api\/runs\/(stop|restart|cancel)/.test(call.path));
    const reason = /这个任务没有 id，无法操作/;
    await ui.click('[data-page=runs]');
    for (const saved of [true, false]) {
      const failed = failedRun({saved});
      delete failed.run.id;
      ui.routes['/api/runs'] = failed;
      if (ui.doc.querySelector('#runs-back')) await ui.ui.renderRuns(); else await openRunFromList(ui);
      await ui.click(saved ? '#resume-run' : '#run-failure #restart-run');
      await settleMany();
      assert.equal(posted().length, 0, saved ? 'resume posted nothing' : 'restart posted nothing');
      assert.match(ui.doc.querySelector('.error-banner').textContent, reason);
    }
    ui.routes['/api/runs'] = {run: {status: 'running', kind: 'analysis', selections: {ticker: 'NVDA', date: '2026-10-01'}}, progress: {agents: []}, result: {present: false}};
    await ui.ui.renderRuns();
    await ui.click('#stop-run');
    await ui.click('#confirm-yes');
    await settleMany();
    assert.equal(posted().length, 0, 'stop posted nothing');
    assert.ok([...ui.doc.querySelectorAll('[data-kept-notice]')].some((note) => reason.test(note.textContent)));
    ui.ui.state.notices = [];
    ui.routes['/api/runs'] = {run: {status: 'queued', kind: 'analysis', queue_position: 1, selections: {ticker: 'NVDA', date: '2026-10-01'}}, progress: {agents: []}, result: {present: false}};
    await ui.ui.renderRuns();
    assert.ok(ui.doc.querySelector('#cancel-queued'));
    await ui.click('#cancel-queued');
    await settleMany();
    assert.equal(posted().length, 0, 'cancel posted nothing');
    assert.ok([...ui.doc.querySelectorAll('[data-kept-notice]')].some((note) => reason.test(note.textContent)));
  } finally { ui.dom.window.close(); }
});


// ---- WEBUI-007 item 1 (PM): 查看状态 is only the run list; a run opens at #status/<id>

function routedRuns(path) {
  const id = new URL(path, 'http://localhost/').searchParams.get('id');
  return multiRuns({selected: id || 'run-btc'});
}

test('查看状态 is only the list; a row opens its detail at #status/<id> with 「‹ 全部任务」 top left, which returns to the list (WEBUI-007 item 1)', async () => {
  const ui = await setup({'/api/runs': (options, path) => routedRuns(path), '/api/logs': {lines: []}});
  try {
    const {doc} = ui;
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    assert.equal(ui.window.location.hash, '#status');
    assert.equal(doc.querySelectorAll('#run-list [data-run-id]').length, 6);
    assert.equal(doc.querySelector('#run-detail'), null);
    await ui.click('#run-list [data-run-id="run-nvda"]');
    await settleMany();
    assert.equal(ui.window.location.hash, '#status/run-nvda');
    assert.ok(ui.calls.some((call) => call.path === '/api/runs?id=run-nvda'));
    const back = doc.querySelector('#runs-back');
    assert.equal(back.textContent, '‹ 全部任务');
    assert.equal(doc.querySelector('#page').firstElementChild, back, '「‹ 全部任务」 is the first thing on the page, top left');
    assert.equal(ui.window.getComputedStyle(back).alignSelf, 'flex-start', '「‹ 全部任务」 is a small control, not a full-width bar');
    assert.equal(doc.activeElement, back, 'focus moves to 「‹ 全部任务」');
    assert.match(doc.querySelector('#run-detail-head').textContent, /NVDA · 2026-10-30/);
    assert.ok(doc.querySelector('#run-detail #stop-run'), '停止任务 lives in the detail');
    assert.equal(doc.querySelector('#run-list'), null);
    assert.equal(doc.querySelector('.page-heading h1'), null, 'the detail does not repeat the list heading');
    await ui.click('#runs-back');
    await waitHistory();
    assert.equal(ui.window.location.hash, '#status');
    assert.equal(doc.querySelector('#run-detail'), null);
    const row = doc.querySelector('#run-list [data-run-id="run-nvda"]');
    assert.ok(row.classList.contains('on'));
    assert.equal(doc.activeElement, row, 'focus returns to the row');
    // Enter on a row opens it too.
    doc.querySelector('#run-list [data-run-id="run-aapl"]').dispatchEvent(new ui.window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    await settleMany();
    assert.equal(ui.window.location.hash, '#status/run-aapl');
    assert.match(doc.querySelector('#run-detail-head').textContent, /AAPL/);
  } finally { ui.dom.window.close(); }
});

test('the browser back and forward buttons move between the list and the detail; another page drops the address (WEBUI-007 item 1)', async () => {
  const ui = await setup({'/api/runs': (options, path) => routedRuns(path), '/api/logs': {lines: []}});
  try {
    const {doc} = ui;
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    await ui.click('#run-list [data-run-id="run-tsla"]');
    await settleMany();
    assert.equal(ui.window.location.hash, '#status/run-tsla');
    ui.window.history.back();
    await waitHistory();
    assert.equal(ui.window.location.hash, '#status');
    assert.ok(doc.querySelector('#run-list'), 'back shows the list');
    assert.equal(doc.querySelector('#run-detail'), null);
    ui.window.history.forward();
    await waitHistory();
    assert.equal(ui.window.location.hash, '#status/run-tsla');
    assert.match(doc.querySelector('#run-detail-head').textContent, /TSLA/, 'forward opens the same run again');
    await ui.click('.nav-item[data-page=reports]');
    await settleMany();
    assert.equal(ui.window.location.hash, '', 'leaving 查看状态 drops #status');
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    assert.equal(ui.window.location.hash, '#status');
    assert.equal(doc.querySelector('#run-detail'), null, 'the sidebar 查看状态 always opens the list');
    assert.ok(![...doc.querySelectorAll('[data-kept-notice]')].some((note) => /没有找到这个任务/.test(note.textContent)),
      'sidebar 查看状态 is the list itself, not an unknown-id fallback');
    // From an open detail, the same nav item must close it without a round-trip through another page.
    await ui.click('#run-list [data-run-id="run-tsla"]');
    await settleMany();
    assert.ok(doc.querySelector('#run-detail'));
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    assert.equal(doc.querySelector('#run-detail'), null, 'sidebar 查看状态 closes the open detail');
    assert.equal(ui.window.location.hash, '#status');
    assert.equal(ui.ui.state.selectedRunId, 'run-tsla', 'sidebar keeps selectedRunId for the last-opened highlight');
    assert.ok(doc.querySelector('#run-list [data-run-id="run-tsla"].on'), 'the row just viewed stays highlighted');
    assert.ok(![...doc.querySelectorAll('[data-kept-notice]')].some((note) => /没有找到这个任务/.test(note.textContent)));
  } finally { ui.dom.window.close(); }
});

test('a refresh on #status/<id> stays on that run; #status opens the list; an unknown id falls back to the list (WEBUI-007 item 1)', async () => {
  const routes = {'/api/runs': (options, path) => routedRuns(path), '/api/logs': {lines: []}};
  let ui = await setup(routes, {url: 'http://localhost/#status/run-eth'});
  try {
    await settleMany();
    assert.equal(ui.ui.state.page, 'runs');
    assert.match(ui.doc.querySelector('#run-detail-head').textContent, /ETH-USD/);
    assert.ok(ui.doc.querySelector('#cancel-queued'), 'a queued run keeps 取消排队 in its detail');
    await ui.click('#runs-back');
    await waitHistory();
    assert.equal(ui.window.location.hash, '#status', 'a detail opened from the address goes back to the list in place');
    assert.ok(ui.doc.querySelector('#run-list [data-run-id="run-eth"]'));
  } finally { ui.dom.window.close(); }
  ui = await setup(routes, {url: 'http://localhost/#status'});
  try {
    await settleMany();
    assert.equal(ui.ui.state.page, 'runs');
    assert.ok(ui.doc.querySelector('#run-list'));
    assert.equal(ui.doc.querySelector('#run-detail'), null);
  } finally { ui.dom.window.close(); }
  ui = await setup(routes, {url: 'http://localhost/#status/run-gone'});
  try {
    await settleMany();
    assert.equal(ui.ui.state.page, 'runs');
    assert.equal(ui.doc.querySelector('#run-detail'), null, 'an unknown id is not replaced by some other run');
    assert.ok(ui.doc.querySelector('#run-list'));
    assert.equal(ui.window.location.hash, '#status');
    assert.ok([...ui.doc.querySelectorAll('[data-kept-notice]')].some((note) => /没有找到这个任务/.test(note.textContent)));
  } finally { ui.dom.window.close(); }
});

test('polling keeps you on the open detail, and a click during a poll is not lost (WEBUI-007 item 1)', async () => {
  const ui = await setup({'/api/runs': (options, path) => routedRuns(path), '/api/logs': {lines: []}});
  try {
    const {doc} = ui;
    let poll = null;
    ui.window.setInterval = (fn) => { poll = fn; return 1; };
    ui.window.clearInterval = () => {};
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    assert.ok(poll, 'running runs are polled');
    await ui.click('#run-list [data-run-id="run-nvda"]');
    await settleMany();
    for (let i = 0; i < 3; i++) { poll(); await settleMany(); }
    assert.match(doc.querySelector('#run-detail-head').textContent, /NVDA/, 'the poll redraws the same run');
    assert.ok(doc.querySelector('#runs-back'));
    assert.equal(doc.querySelector('#run-list'), null, 'the poll does not jump back to the list');
    assert.equal(ui.window.location.hash, '#status/run-nvda');
    await ui.click('#runs-back');
    await waitHistory();
    poll();  // a poll starts on the list ...
    doc.querySelector('#run-list [data-run-id="run-tsla"]').click();  // ... and a run is opened before it answers
    await settleMany(12);
    assert.match(doc.querySelector('#run-detail-head').textContent, /TSLA/, 'the clicked run is shown, not the poll\'s answer');
    assert.equal(ui.window.location.hash, '#status/run-tsla');
  } finally { ui.dom.window.close(); }
});

test('重新启动 in a detail moves the address to the new run (WEBUI-007 item 1)', async () => {
  const ui = await setup({
    '/api/runs': (options, path) => routedRuns(path), '/api/logs': {lines: []},
    '/api/runs/restart': {id: 'run-aapl', zh: '已重新启动'},
  });
  try {
    await openRunFromList(ui, 'run-tsla');
    const restart = ui.doc.querySelector('#restart-run');
    assert.ok(restart, '重新启动 lives in the detail');
    await ui.click('#restart-run');
    await settleMany();
    assert.deepEqual(JSON.parse(ui.calls.find((call) => call.path === '/api/runs/restart').body), {id: 'run-tsla'});
    assert.equal(ui.window.location.hash, '#status/run-aapl');
    assert.match(ui.doc.querySelector('#run-detail-head').textContent, /AAPL/);
  } finally { ui.dom.window.close(); }
});

// ---- WEBUI-007 item 4: the sidebar badge digit is centred

test('the 查看状态 badge centres its digits both ways, for 1 and for 2 digits (WEBUI-007 item 4)', async () => {
  const one = multiRuns();
  const ui = await setup({'/api/runs': {...one, counts: {...one.counts, running: 1}}});
  try {
    const {doc, window} = ui;
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    const badge = doc.querySelector('.nav-item[data-page=runs] .nav-badge');
    assert.equal(badge.textContent, '1');
    const style = window.getComputedStyle(badge);
    assert.equal(style.display, 'inline-flex');
    assert.equal(style.alignItems, 'center');
    assert.equal(style.justifyContent, 'center');
    assert.equal(style.boxSizing, 'border-box');
    assert.equal(style.height, '18px');
    assert.equal(style.minWidth, '18px', 'one digit is a circle');
    assert.equal(style.borderRadius, '9px');
    assert.equal(style.lineHeight, '1', 'the line box is the glyph height, so flex centring centres the digit');
    assert.equal(style.fontVariantNumeric, 'tabular-nums', '1 gets the same advance as 8');
    assert.equal(style.paddingTop, '0px');
    assert.equal(style.paddingBottom, '0px');
    assert.equal(style.paddingLeft, style.paddingRight, 'equal side padding');
    const rules = styleRules(window).filter((rule) => selectorMatches(rule, '.nav-badge'));
    assert.ok(rules.every((rule) => !rule.style.lineHeight || rule.style.lineHeight === '1'), 'no later rule brings back line-height: 18px');
    ui.ui.state.page = 'runs';
    window.eval('updateRunBadge(12)');
    assert.equal(badge.textContent, '12', 'two digits widen the pill');
    assert.equal(window.getComputedStyle(badge).justifyContent, 'center');
  } finally { ui.dom.window.close(); }
});

// ---- WEBUI-007 item 5: the report reading view is 16px / 1.7

test('the report body is 16px with line-height 1.7 and headings stay above it in order; nothing else changes (WEBUI-007 item 5)', async () => {
  const ui = await openReports();
  try {
    const {doc, window} = ui;
    const body = doc.querySelector('#report-body');
    assert.ok(body.classList.contains('md'));
    const css = window.getComputedStyle(body);
    assert.equal(css.fontSize, '16px');
    assert.equal(css.lineHeight, '1.7');
    const px = (selector) => {
      const rule = styleRules(window).filter((item) => selectorMatches(item, selector)).at(-1);
      assert.ok(rule, `rule ${selector}`);
      return parseFloat(rule.style.fontSize);
    };
    const role = px('.report-body.md h3.md-role');
    const h3 = px('.report-body.md h3');
    const h4 = px('.report-body.md h4');
    const h5 = px('.report-body.md h5');
    assert.ok(role >= h3 && h3 > h4 && h4 > h5 && h5 > 16, `role ${role} ≥ # ${h3} > ## ${h4} > ### ${h5} > body 16`);
    await ui.click('[data-tab="analyst-market.md"]');
    await settleMany();
    const marketBody = doc.querySelector('#report-body');
    const sub = marketBody.querySelector('h4');
    assert.ok(sub, 'the fixture has a ## heading');
    assert.equal(window.getComputedStyle(sub).fontSize, `${h4}px`);
    assert.equal(window.getComputedStyle(marketBody.querySelector('p')).fontSize, '16px');
    assert.equal(window.getComputedStyle(marketBody).lineHeight, '1.7');
    await ui.click('[data-tab=research]');
    await settleMany();
    const roleHead = doc.querySelector('#report-body h3.md-role');
    assert.ok(roleHead, 'the fixture has role headings');
    assert.equal(window.getComputedStyle(roleHead).fontSize, `${role}px`);
    // Only the reading view: the generic .md rule and the rest of the page are as before.
    const generic = styleRules(window).find((item) => item.selectorText === '.md');
    assert.equal(generic.style.fontSize, '14px');
    assert.notEqual(window.getComputedStyle(doc.querySelector('#report-list')).fontSize, '16px');
    assert.notEqual(window.getComputedStyle(doc.querySelector('.tab')).fontSize, '16px', 'the section tabs keep their size');
  } finally { ui.dom.window.close(); }
});


// ---- WEBUI-007 item 3 (PM final on widths): one content padding on every page, no width cap

// The cascaded value of `prop` on `element` at a viewport `width`: top-level rules plus the
// @media rules whose (min|max-width) conditions hold, by specificity then source order.
function cascadeAt(window, element, prop, width) {
  const mediaHolds = (text) => String(text).split(',').some((query) => {
    const parts = query.split(/\band\b/).map((part) => part.trim()).filter(Boolean);
    return parts.every((part) => {
      const m = /^\((max|min)-width:\s*(\d+)px\)$/.exec(part);
      if (m) return m[1] === 'max' ? width <= Number(m[2]) : width >= Number(m[2]);
      return part === 'screen' || part === 'all';
    });
  });
  const specificity = (selector) => {
    const s = selector.replace(/::?[\w-]+\([^)]*\)/g, (x) => (x.startsWith('::') ? ' b' : ' .c'));
    return [(s.match(/#[\w-]+/g) || []).length, (s.match(/\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+/g) || []).length,
      (s.replace(/"[^"]*"/g, '').match(/(^|[\s>+~])[a-z][\w-]*/gi) || []).length];
  };
  let best = null;
  let order = 0;
  const walk = (rules) => {
    for (const rule of rules || []) {
      if (rule.media) { if (mediaHolds(rule.media.mediaText)) walk(rule.cssRules); continue; }
      if (!rule.selectorText) { if (rule.cssRules) walk(rule.cssRules); continue; }
      order++;
      const value = rule.style.getPropertyValue(prop);
      if (!value) continue;
      for (const selector of rule.selectorText.split(',').map((item) => item.trim())) {
        let matches = false;
        try { matches = element.matches(selector); } catch { matches = false; }
        if (!matches) continue;
        const key = [...specificity(selector), order];
        const i = best ? key.findIndex((v, n) => v !== best.key[n]) : 0;
        if (!best || (i >= 0 && key[i] > best.key[i])) best = {key, value};
      }
    }
  };
  for (const sheet of window.document.styleSheets) walk(sheet.cssRules);
  return best ? normalizeCssValue(best.value) : '';
}

test('start (cards and wizard), 查看状态 and 报告 share one content left edge at 1750 and 1440 beside the 256px sidebar (16px on a phone); nothing caps the width (WEBUI-007 item 3, WEBUI-008 PM)', async () => {
  const ui = await setup({'/api/runs': multiRuns(), '/api/logs': {lines: []},
    '/api/reports': {reports: reportRows, live: [], directory: '/tmp/results'},
    '/api/reports/outline': () => outlineFor(ui.calls.at(-1).path)});
  try {
    const {doc, window} = ui;
    const card = doc.querySelector('.workspace-card');
    const page = doc.querySelector('#page');
    const edges = {};
    for (const name of ['hub', 'analyze', 'runs', 'reports']) {
      if (name === 'analyze') await ui.click('.fw[data-framework=tradingagents]');
      else await ui.click(`.nav-item[data-page=${name === 'hub' ? 'analyze' : name}]`);
      await settleMany(10);
      if (name === 'hub') assert.ok(doc.querySelector('#fw-grid'), 'the nav opens the framework cards');
      const first = page.firstElementChild;
      assert.ok(first, `${name} rendered`);
      for (const width of [1750, 1440, 390]) {
        const box = ['padding-top', 'padding-right', 'padding-bottom', 'padding-left'].map((prop) => cascadeAt(window, card, prop, width));
        const shift = [page, first].map((el) => ['margin-left', 'padding-left'].map((prop) => cascadeAt(window, el, prop, width) || '0')).flat();
        (edges[width] ||= {})[name] = `${box.join(' ')} | ${shift.join(' ')}`;
        for (const el of [page, first]) {
          const cap = cascadeAt(window, el, 'max-width', width);
          assert.ok(!cap || cap === 'none' || cap === '100%', `${name} ${el.id || el.className} is not capped at ${width}: ${cap}`);
          assert.notEqual(cascadeAt(window, el, 'margin-left', width), 'auto', `${name} is not centred at ${width}`);
        }
      }
      if (name === 'analyze') {
        for (const width of [1750, 1440]) {
          assert.equal(cascadeAt(window, doc.querySelector('.wiz'), 'gap', width), '40px');
          assert.equal(cascadeAt(window, doc.querySelector('.steps'), 'width', width), '220px');
          assert.ok(['none', '0 0 auto'].includes(cascadeAt(window, doc.querySelector('.steps'), 'flex', width)), 'the step rail keeps 220px (flex: none)');
          assert.equal(cascadeAt(window, doc.querySelector('.wiz-form'), 'max-width', width), 'none', 'the form fills the area');
          assert.match(cascadeAt(window, doc.querySelector('.wiz-form'), 'flex', width), /^1( 1 0(%|px)?)?$/);
          assert.equal(cascadeAt(window, doc.querySelector('.foot .r'), 'margin-left', width), 'auto', 'the step buttons sit at the right edge of the form');
        }
        assert.equal(cascadeAt(window, doc.querySelector('.steps'), 'width', 390), '100%', 'the phone keeps its step strip');
      }
      if (name === 'runs') {
        for (const width of [1750, 1440]) {
          for (const el of [doc.querySelector('#run-list'), doc.querySelector('#run-filters')]) {
            const cap = cascadeAt(window, el, 'max-width', width);
            assert.ok(!cap || cap === 'none' || cap === '100%', `the run list fills the width: ${cap}`);
          }
        }
      }
      if (name === 'reports') {
        const rep = doc.querySelector('.rep');
        const body = doc.querySelector('#report-body');
        for (const width of [1750, 1440]) {
          const cap = cascadeAt(window, rep, 'max-width', width);
          assert.ok(!cap || cap === 'none', `the report container fills the width: ${cap}`);
          assert.equal(cascadeAt(window, body, 'max-width', width), '860px', 'report lines stop at 860px');
          assert.ok(!['auto'].includes(cascadeAt(window, body, 'margin-left', width)), 'the report text is left-aligned, not centred');
          assert.ok(!/auto/.test(cascadeAt(window, body, 'margin', width)), 'no margin: auto on the report text');
        }
      }
    }
    for (const width of [1750, 1440]) {
      assert.equal(new Set(Object.values(edges[width])).size, 1, `one left edge at ${width}: ${JSON.stringify(edges[width])}`);
      assert.match(edges[width].analyze, /^20px 32px 40px 32px \|/, `the start page padding at ${width}`);
      // WEBUI-008 (PM): the desktop sidebar is 256px with 12px sides, so the content
      // starts 256 + 32 = 288px from the window edge (it was 216 + 32 = 248).
      const side = doc.querySelector('.sidebar');
      assert.equal(cascadeAt(window, side, 'width', width), '256px');
      assert.match(cascadeAt(window, side, 'flex', width), /^0 0 256px$/);
      assert.equal(cascadeAt(window, side, 'padding-left', width), '12px');
      assert.equal(cascadeAt(window, side, 'padding-right', width), '12px');
      const edge = parseFloat(cascadeAt(window, side, 'width', width)) + parseFloat(cascadeAt(window, card, 'padding-left', width));
      assert.equal(edge, 288, `the content left edge at ${width}`);
    }
    // The narrow layout is unchanged: the 216px overlay sidebar with its 10px sides.
    const side = doc.querySelector('.sidebar');
    for (const width of [390, 700]) {
      assert.equal(cascadeAt(window, side, 'width', width), '216px', `narrow sidebar width at ${width}`);
      assert.equal(cascadeAt(window, side, 'padding-left', width), '10px');
      assert.equal(cascadeAt(window, side, 'position', width), 'absolute');
    }
    assert.equal(new Set(Object.values(edges[390])).size, 1, `one left edge on a phone: ${JSON.stringify(edges[390])}`);
    assert.match(edges[390].analyze, /^16px 16px 16px 16px \|/);
    // The real width cap that PM ruled out must not exist anywhere.
    assert.ok(styleRules(window).every((rule) => rule.style.maxWidth !== '1200px'), 'no 1200px cap');
  } finally { ui.dom.window.close(); }
});

// ---- WEBUI-007 item 2: the TradingAgents mark

test('the top bar shows the 22px TradingAgents mark and name, no Tauric line, and the tab uses the logo icons (WEBUI-007 item 2)', async () => {
  const ui = await setup();
  try {
    const {doc, window} = ui;
    const brand = doc.querySelector('.topbar .brand');
    assert.equal(brand.textContent.replace(/\s+/g, ' ').trim(), 'TradingAgents');
    assert.ok(!/Tauric/i.test(doc.querySelector('.topbar').innerHTML), 'no Tauric Research line or link');
    const mark = brand.querySelector('img.brand-mark');
    assert.equal(mark.getAttribute('src'), '/icons/logo.svg');
    assert.equal(mark.getAttribute('alt'), '', 'decorative: the name is spelled out next to it');
    assert.equal(window.getComputedStyle(mark).width, '22px');
    assert.equal(window.getComputedStyle(mark).height, '22px');
    for (const width of [1750, 1440, 390]) assert.equal(cascadeAt(window, mark, 'width', width), '22px', `the mark stays 22px at ${width}`);
    assert.equal(doc.querySelector('link[rel=icon][type="image/svg+xml"]').getAttribute('href'), '/icons/logo.svg');
    assert.deepEqual([...doc.querySelectorAll('link[rel=icon][type="image/png"]')].map((link) => [link.getAttribute('sizes'), link.getAttribute('href')]),
      [['32x32', '/icons/logo-32.png'], ['16x16', '/icons/logo-16.png']]);
    assert.equal(doc.querySelector('link[rel=apple-touch-icon]').getAttribute('href'), '/icons/logo-180.png');
    const svg = readFileSync(resolve(root, 'webui/static/icons/logo.svg'), 'utf8');
    assert.match(svg, /viewBox="0 0 24 24"/);
    assert.doesNotMatch(svg, /<script|on\w+=|href=/i, 'the logo is a plain drawing');
  } finally { ui.dom.window.close(); }
});

// ---- WEBUI-008 (PM, Proto mock8 v2 ?s=hub / ?s=w1b): framework cards before the wizard.
const HUB_URL = 'http://localhost/';
const twoFrameworks = () => ({default: 'tradingagents', frameworks: [
  {...FRAMEWORKS.frameworks[0], last_used: '2026-10-31T13:52:07'},
  {id: 'demo', name: 'Demo <b>Lab</b>', description: '只在测试里出现的框架。', icon: '/icons/logo.svg', tags: ['股票']},
]});

test('启动分析 opens on one card per backend framework, each from GET /api/frameworks (WEBUI-008)', async () => {
  const ui = await setup({'/api/frameworks': twoFrameworks()}, {url: HUB_URL});
  try {
    const {doc, window} = ui;
    assert.equal(ui.ui.state.page, 'analyze');
    assert.equal(window.location.hash, '#start', 'the cards live at #start');
    assert.equal(doc.querySelector('#page h1').textContent, '启动分析');
    assert.equal(doc.querySelector('#page .page-description').textContent, '选一个分析工具开始，结果只用于研究参考。');
    assert.equal(doc.querySelector('#analysis-form'), null, 'no wizard before a card is chosen');
    const cards = [...doc.querySelectorAll('#fw-grid > .fw')];
    assert.equal(cards.length, 2, 'one card per framework, no placeholders');
    assert.equal(doc.querySelectorAll('#fw-grid > *').length, 2);
    const [ta, demo] = cards;
    assert.equal(ta.querySelector('.fw-name').textContent, 'TradingAgents');
    assert.equal(ta.querySelector('.fw-logo').getAttribute('src'), '/icons/logo.svg');
    assert.match(ta.querySelector('.fw-d').textContent, /^多智能体交易研究。/);
    assert.deepEqual([...ta.querySelectorAll('.fw-m span')].map((tag) => tag.textContent), ['股票', '加密货币', '4 步设置']);
    assert.equal(ta.querySelector('.fw-k').textContent, '上次使用 10/31 13:52');
    assert.equal(ta.querySelector('.fw-f .button--primary').textContent, '开始');
    assert.equal(demo.querySelector('.fw-k'), null, 'no runs: no 上次使用 at all');
    assert.doesNotMatch(demo.textContent, /上次使用/);
    assert.equal(demo.querySelector('.fw-name').textContent, 'Demo <b>Lab</b>', 'names are text, not markup');
    assert.equal(demo.querySelector('.fw-name b'), null);
    assert.ok(ui.calls.some((call) => call.path === '/api/frameworks'));
  } finally { ui.dom.window.close(); }
});

test('the framework cards follow mock8 v2: 320px auto-fill grid, 16px gap, 8px card, one column on a phone (WEBUI-008)', async () => {
  const ui = await setup({}, {url: HUB_URL});
  try {
    const {doc, window} = ui;
    const grid = doc.querySelector('#fw-grid');
    const card = doc.querySelector('.fw');
    for (const width of [1750, 1440]) {
      assert.equal(cascadeAt(window, grid, 'grid-template-columns', width), 'repeat(auto-fill, minmax(320px, 1fr))');
      assert.equal(cascadeAt(window, grid, 'gap', width), '16px');
    }
    assert.equal(cascadeAt(window, grid, 'grid-template-columns', 390), '1fr', 'one column below 600px');
    assert.equal(cascadeAt(window, card, 'border-radius', 1440), '8px');
    assert.equal(cascadeAt(window, card, 'padding', 1440), '18px 20px');
    assert.match(cascadeAt(window, card, 'border', 1440), /^1px solid/);
    assert.equal(cascadeAt(window, card, 'cursor', 1440), 'pointer', 'the whole card is clickable');
    assert.equal(cascadeAt(window, doc.querySelector('.fw-name'), 'font-size', 1440) || cascadeAt(window, doc.querySelector('.fw-t'), 'font-size', 1440), '16px');
    assert.equal(cascadeAt(window, doc.querySelector('.fw-t'), 'font-weight', 1440), '700');
    assert.equal(cascadeAt(window, doc.querySelector('.fw-logo'), 'width', 1440), '28px');
    assert.equal(cascadeAt(window, doc.querySelector('.fw-d'), 'font-size', 1440), '13px');
    assert.equal(cascadeAt(window, doc.querySelector('.fw-d'), 'line-height', 1440), '1.6');
    const hover = styleRules(window).find((rule) => rule.selectorText && rule.selectorText.split(',').map((x) => x.trim()).includes('.fw:hover'));
    assert.ok(hover, 'a hover rule exists');
    assert.equal(hover.style.borderColor, 'var(--primary)', 'hover border is the primary colour');
  } finally { ui.dom.window.close(); }
});

test('a click anywhere on a card opens that framework at #start/<id> with 「‹ 全部分析工具」 back to the cards (WEBUI-008)', async () => {
  const ui = await setup({'/api/frameworks': twoFrameworks()}, {url: HUB_URL});
  try {
    const {doc, window} = ui;
    await ui.click('.fw[data-framework=tradingagents] .fw-d');
    assert.equal(window.location.hash, '#start/tradingagents');
    assert.equal(doc.querySelector('#page h1').textContent, 'TradingAgents', 'the title is the framework name');
    assert.equal(doc.querySelector('#page .page-description').textContent, '四步设置好就能启动，结果只用于研究参考。');
    const crumb = doc.querySelector('#back-to-frameworks');
    assert.equal(crumb.textContent, '‹ 全部分析工具');
    assert.ok(crumb.compareDocumentPosition(doc.querySelector('#page h1')) & window.Node.DOCUMENT_POSITION_FOLLOWING, 'the crumb sits above the title');
    assert.equal(cascadeAt(window, crumb, 'font-size', 1440), '13px');
    assert.equal(cascadeAt(window, crumb, 'color', 1440), 'var(--primary)');
    assert.equal(doc.querySelectorAll('[data-step]').length, 4, 'the four-step wizard is unchanged');
    assert.equal(ui.ui.readForm().framework, 'tradingagents');
    await ui.click('#back-to-frameworks');
    await waitHistory();
    assert.equal(window.location.hash, '#start');
    assert.ok(doc.querySelector('#fw-grid'));
    assert.equal(doc.querySelector('#analysis-form'), null);
    // The 开始 button opens the same wizard; a second framework opens its own.
    await ui.click('.fw[data-framework=demo] [data-start-framework]');
    assert.equal(window.location.hash, '#start/demo');
    assert.equal(doc.querySelector('#page h1').textContent, 'Demo <b>Lab</b>');
    assert.equal(ui.ui.readForm().framework, 'demo');
    // Keyboard: Enter on a focused card opens it.
    await ui.click('#back-to-frameworks');
    await waitHistory();
    doc.querySelector('.fw[data-framework=tradingagents]').dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    await settleMany();
    assert.equal(window.location.hash, '#start/tradingagents');
  } finally { ui.dom.window.close(); }
});

test('browser back from a wizard returns to the cards, forward reopens it (WEBUI-008)', async () => {
  const ui = await setup({}, {url: HUB_URL});
  try {
    const {doc, window} = ui;
    await ui.click('.fw[data-framework=tradingagents]');
    assert.ok(doc.querySelector('#analysis-form'));
    window.history.back();
    await waitHistory();
    assert.equal(window.location.hash, '#start');
    assert.ok(doc.querySelector('#fw-grid'));
    window.history.forward();
    await waitHistory();
    assert.equal(window.location.hash, '#start/tradingagents');
    assert.equal(doc.querySelector('#page h1').textContent, 'TradingAgents');
  } finally { ui.dom.window.close(); }
});

test('a refresh on #start/<id> stays in that wizard; an unknown framework falls back to #start (WEBUI-008)', async () => {
  let ui = await setup({}, {url: 'http://localhost/#start/tradingagents'});
  try {
    assert.equal(ui.window.location.hash, '#start/tradingagents');
    assert.equal(ui.doc.querySelector('#page h1').textContent, 'TradingAgents');
    assert.ok(ui.doc.querySelector('#back-to-frameworks'));
  } finally { ui.dom.window.close(); }
  for (const url of ['http://localhost/#start/nope', 'http://localhost/#start/%3Cb%3E', 'http://localhost/#start']) {
    ui = await setup({}, {url});
    try {
      assert.equal(ui.window.location.hash, '#start', `${url} lands on the cards`);
      assert.ok(ui.doc.querySelector('#fw-grid'));
      assert.equal(ui.doc.querySelector('#analysis-form'), null);
    } finally { ui.dom.window.close(); }
  }
});

test('sidebar 「+ 新建分析」 and 启动分析 always open the cards, even with one framework and from inside a wizard (WEBUI-008, PM)', async () => {
  const ui = await setup();
  try {
    const {doc, window} = ui;
    assert.equal(doc.querySelectorAll('.fw').length, 0, 'the fixture starts inside the wizard');
    await ui.click('#create-analysis');
    await ui.click('#create-menu [data-page=analyze]');
    assert.equal(window.location.hash, '#start');
    assert.equal(doc.querySelectorAll('#fw-grid .fw').length, 1, 'one framework still gets its card');
    await ui.click('.fw[data-framework=tradingagents]');
    await ui.click('.nav-item[data-page=analyze]');
    assert.equal(window.location.hash, '#start');
    assert.ok(doc.querySelector('#fw-grid'));
    for (const page of ['reports', 'accounts', 'backtest', 'runs']) {
      await ui.click('.nav-item[data-page=analyze]');
      assert.equal(window.location.hash, '#start');
      await ui.click(`.nav-item[data-page=${page}]`);
      assert.equal(window.location.hash.startsWith('#start'), false, `leaving 启动分析 for ${page} drops #start`);
    }
  } finally { ui.dom.window.close(); }
});

test('the cards re-read last_used each visit and a failed read says so with 重试 (WEBUI-008)', async () => {
  let fail = true;
  let seen = 0;
  const ui = await setup({'/api/frameworks': () => {
    seen++;
    if (fail) return {__status: 500, body: {error: '读取失败'}};
    return twoFrameworks();
  }}, {url: HUB_URL});
  try {
    const {doc} = ui;
    assert.match(doc.querySelector('#frameworks-error').textContent, /没能读取分析工具列表：读取失败/);
    assert.equal(doc.querySelector('.fw'), null, 'no made-up card');
    fail = false;
    await ui.click('#frameworks-retry');
    await settleMany();
    assert.equal(doc.querySelectorAll('.fw').length, 2);
    const before = seen;
    await ui.click('.nav-item[data-page=runs]');
    await ui.click('.nav-item[data-page=analyze]');
    await settleMany();
    assert.ok(seen > before, 'opening the cards asks the backend again');
  } finally { ui.dom.window.close(); }
});

test('查看状态 shows a 「工具」 column and the detail names the framework, both from the backend (WEBUI-008)', async () => {
  const named = (path) => {
    const data = routedRuns(path);
    data.runs = data.runs.map((item, i) => ({...item, framework: i === 1 ? 'demo' : 'tradingagents', framework_name: i === 1 ? 'Demo Lab' : 'TradingAgents'}));
    data.run = {...data.run, framework: 'tradingagents', framework_name: 'TradingAgents'};
    return data;
  };
  const ui = await setup({'/api/runs': (options, path) => named(path), '/api/logs': {lines: []}}, {url: 'http://localhost/#status'});
  try {
    const {doc, window} = ui;
    await settleMany();
    const head = [...doc.querySelectorAll('#run-list .tr.th span')].map((cell) => cell.textContent);
    assert.deepEqual(head.slice(0, 3), ['标的', '工具', '状态']);
    assert.equal(doc.querySelector('#run-list [data-run-id="run-btc"] .fwc').textContent, 'TradingAgents');
    assert.equal(doc.querySelector('#run-list [data-run-id="run-nvda"] .fwc').textContent, 'Demo Lab', 'the name is the backend one, not a constant');
    const cols = cascadeAt(window, doc.querySelector('#run-list .tr'), 'grid-template-columns', 1440).split(' ');
    assert.equal(cols.length, head.length, 'one grid track per header cell');
    await ui.click('#run-list [data-run-id="run-btc"]');
    await settleMany();
    // The first .sub is #run-kind (分析/回测); the framework line follows it.
    assert.match(doc.querySelector('#run-detail-head .sub:not(#run-kind)').textContent, /^TradingAgents · /, 'the detail subtitle starts with the framework name');
  } finally { ui.dom.window.close(); }
});

test('an empty 查看状态 keeps the 「工具」 header (WEBUI-008)', async () => {
  const ui = await setup({'/api/runs': {runs: [], counts: {}, run: null, progress: {}}}, {url: 'http://localhost/#status'});
  try {
    await settleMany();
    const head = [...ui.doc.querySelectorAll('#run-list .tr.th span')].map((cell) => cell.textContent);
    assert.ok(head.includes('工具'), JSON.stringify(head));
  } finally { ui.dom.window.close(); }
});

test('a run whose endpoint sent no usage shows 「Tokens: 未知」, not 「Tokens: --」 (W75)', async () => {
  const withStats = (path) => {
    const data = routedRuns(path);
    data.progress = {...(data.progress || {}), stats: 'Agents: 2/12 | LLM: 12 | Tools: 3 | Tokens: 未知 | Reports: 1/7 | ⏱ 01:05'};
    return data;
  };
  const ui = await setup({'/api/runs': (options, path) => withStats(path), '/api/logs': {lines: []}}, {url: 'http://localhost/#status'});
  try {
    const {doc} = ui;
    await settleMany();
    await ui.click('#run-list [data-run-id="run-btc"]');
    await settleMany();
    const text = doc.body.textContent;
    assert.ok(text.includes('运行统计：Agents: 2/12 | LLM: 12 | Tools: 3 | Tokens: 未知'), 'the footer line is shown as sent');
    assert.ok(!text.includes('Tokens: --'));
  } finally { ui.dom.window.close(); }
});

// ---- RUN-003c (B3)
test('W49: the drawer refuses a holdings JSON that breaks the rules, in Chinese, before saving', async () => {
  const ui = await setup();
  try {
    await openDrawerAtReview(ui);
    const before = ui.calls.filter((item) => item.path === '/api/config' && item.method).length;
    for (const [book, words] of [
      ['{"cash": -1}', /现金（cash）需要是不小于 0 的数字/],
      ['{"cash": true}', /现金（cash）/],
      ['{"positions": [{"ticker": "NVDA", "quantity": "many"}]}', /第 1 个持仓的数量（quantity）/],
      ['{"positions": [{"ticker": "NVDA"}]}', /第 1 个持仓需要填写数量或成本价/],
      ['[1]', /持仓需要是一个 JSON 对象/],
    ]) {
      ui.fill('#portfolio', book);
      await ui.click('#save-drawer-save');
      const note = ui.doc.querySelector('#save-drawer-note');
      assert.match(note.textContent, words);
      assert.doesNotMatch(note.textContent, /pydantic|validation error|Input should/);
      assert.equal(note.className, 'error-banner');
    }
    assert.equal(ui.calls.filter((item) => item.path === '/api/config' && item.method).length, before, 'nothing saved');
    ui.fill('#portfolio', '{"cash": 0, "positions": [{"ticker": "NVDA", "quantity": 2}]}');
    await ui.click('#save-drawer-save');
    assert.equal(ui.doc.querySelector('.save-drawer'), null, 'a valid book saves');
  } finally { ui.dom.window.close(); }
});

test('W43: a capped run list says how many runs it leaves out; counts stay whole', async () => {
  const data = {...multiRuns(), counts: {all: 43, running: 2, queued: 2, failed: 9, finished: 30},
    listing: {shown: 30, total: 43, limit: 30, truncated: true}};
  const ui = await setup({'/api/runs': data});
  try {
    await ui.click('[data-page=runs]');
    assert.match(ui.doc.querySelector('#runs-cap').textContent, /只显示最近 30 个任务（共 43 个）/);
    assert.match(ui.doc.querySelector('#runs-cap').innerHTML, /个）；<br class="notice-break">上方计数包含全部任务。/);
    assert.match(ui.doc.querySelector('#run-filters [data-filter=all]').textContent, /43/);
  } finally { ui.dom.window.close(); }
  const whole = await setup({'/api/runs': {...multiRuns(), listing: {shown: 6, total: 6, limit: 30, truncated: false}}});
  try {
    await whole.click('[data-page=runs]');
    assert.equal(whole.doc.querySelector('#runs-cap'), null);
  } finally { whole.dom.window.close(); }
});

test('W44: a refused backtest shows the console reason as an error, never 回测已启动', async () => {
  const reason = '运行名额已满或有任务在排队（正在运行 2 个，上限 2 个），回测没有启动。回测不排队：等有任务结束、队列清空后再启动。';
  const ui = await setup({'/api/backtests': (options) => (options.method === 'POST' ? {__status: 409, body: {error: reason}} : {sweeps: []})});
  try {
    await ui.click('[data-page=backtest]');
    assert.match(ui.doc.querySelector('#backtest-result').textContent, /回测不排队/);
    ui.fill('#bt-tickers', 'NVDA'); ui.fill('#bt-start', '2026-10-01'); ui.fill('#bt-end', '2026-10-08');
    await ui.click('#start-backtest');
    const out = ui.doc.querySelector('#backtest-result');
    assert.equal(out.textContent, reason);
    assert.equal(out.className, 'error-banner');
    assert.equal(out.getAttribute('role'), 'alert');
    assert.ok(!(ui.ui.state.notices || []).some((line) => line.includes('回测已启动')));
  } finally { ui.dom.window.close(); }
});

test('W59: the model page warns that gpt-5 reasoning models drop temperature; chat models and effort none do not', async () => {
  const ui = await setup();
  try {
    ui.ui.state.form.quick_think_llm = 'gpt-5.4';
    ui.ui.state.form.deep_think_llm = 'gpt-5-chat-latest';
    await ui.click('[data-page=models]');
    const note = ui.doc.querySelector('#gpt5-temperature-note');
    assert.ok(note);
    assert.match(note.textContent, /gpt-5\.4/);
    assert.doesNotMatch(note.textContent, /gpt-5-chat/);
    ui.ui.state.form.openai_reasoning_effort = 'none';
    await ui.click('[data-page=analyze]');
    await ui.click('[data-page=models]');
    assert.equal(ui.doc.querySelector('#gpt5-temperature-note'), null);
    ui.ui.state.form.openai_reasoning_effort = '';
    ui.ui.state.form.quick_think_llm = 'grok-4';
    ui.ui.state.form.deep_think_llm = 'grok-4';
    await ui.click('[data-page=analyze]');
    await ui.click('[data-page=models]');
    assert.equal(ui.doc.querySelector('#gpt5-temperature-note'), null);
  } finally { ui.dom.window.close(); }
});

test('W58: a refused stop shows the console sentence with its next step instead of failing silently', async () => {
  const sentence = '读不到这个任务进程的启动时间，无法确认它还是本任务的分析进程。下一步：稍后点「刷新」再试。';
  const ui = await multiSetup({'/api/runs/stop': {__status: 409, body: {error: sentence}}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'run-btc');
    await ui.click('#stop-run');
    await ui.click('#confirm-yes');
    const banner = ui.doc.querySelector('#stop-refused');
    assert.ok(banner);
    assert.equal(banner.textContent, sentence);
    assert.equal(banner.getAttribute('role'), 'alert');
  } finally { ui.dom.window.close(); }
});

// ---- RUN-003d (B3b)
test('W76/W58: a refused stop stays through the 4-second refresh, puts 「下一步」 on its own line, and goes on the next action', async () => {
  const sentence = '读不到这个任务进程的启动时间，无法确认它还是本任务的分析进程。\n下一步：稍后点「刷新」再试。';
  const ui = await multiSetup({'/api/runs/stop': {__status: 409, body: {error: sentence}}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'run-btc');
    await ui.click('#stop-run');
    await ui.click('#confirm-yes');
    let banner = ui.doc.querySelector('#stop-refused');
    assert.ok(banner);
    assert.equal(banner.getAttribute('role'), 'alert');
    assert.match(banner.innerHTML, /<br>下一步：/);
    assert.equal(banner.textContent, sentence.replace('\n', ''));
    for (let i = 0; i < 3; i++) { await ui.ui.renderRuns(); await settleMany(); }   // the auto-refresh
    banner = ui.doc.querySelector('#stop-refused');
    assert.ok(banner, 'the refusal survives a redraw');
    assert.equal(ui.doc.querySelectorAll('#stop-refused').length, 1, 'shown once, not stacked');
    await openRunFromList(ui, 'run-nvda');
    assert.equal(ui.doc.querySelector('#stop-refused'), null, 'another run does not carry it');
    await openRunFromList(ui, 'run-btc');
    assert.ok(ui.doc.querySelector('#stop-refused'), 'it still belongs to its own run');
  } finally { ui.dom.window.close(); }
});

test('W76: a markup-looking refusal is text, never markup', async () => {
  const ui = await multiSetup({'/api/runs/stop': {__status: 409, body: {error: '<img src=x onerror=alert(1)>\n下一步'}}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'run-btc');
    await ui.click('#stop-run');
    await ui.click('#confirm-yes');
    const banner = ui.doc.querySelector('#stop-refused');
    assert.equal(banner.querySelector('img'), null);
    assert.match(banner.textContent, /<img src=x/);
  } finally { ui.dom.window.close(); }
});

test('W44: a refused backtest scrolls its alert into view', async () => {
  const reason = '运行名额已满或有任务在排队（正在运行 2 个，上限 2 个），回测没有启动。';
  const ui = await setup({'/api/backtests': (options) => (options.method === 'POST' ? {__status: 409, body: {error: reason}} : {sweeps: []})});
  try {
    const scrolled = [];
    ui.window.HTMLElement.prototype.scrollIntoView = function () { scrolled.push(this.id); };
    await ui.click('[data-page=backtest]');
    ui.fill('#bt-tickers', 'NVDA'); ui.fill('#bt-start', '2026-10-01'); ui.fill('#bt-end', '2026-10-08');
    await ui.click('#start-backtest');
    assert.ok(scrolled.includes('backtest-result'), JSON.stringify(scrolled));
    assert.doesNotMatch(ui.doc.querySelector('#backtest-result').textContent, /already running/);
  } finally { ui.dom.window.close(); }
});

test('a config save without a saved flag is not called saved', async () => {
  const ui = await setup({'/api/config': (options) => options.method === 'POST'
    ? {zh: '已保存'}
    : {saved: {}, keys: [{provider: 'openai', configured: false, env_var: 'OPENAI_API_KEY'}], data_env: [], cloud_env: []}});
  try {
    await ui.click('[data-page=models]');
    ui.doc.querySelector('#model-advanced').open = true;
    await ui.click('#model-advanced-form button[type=submit]');
    await settleMany();
    const note = ui.doc.querySelector('#model-advanced-feedback');
    assert.match(note.textContent, /不把它写成已保存/);
    assert.doesNotMatch(note.textContent, /^已保存/);
    assert.doesNotMatch(ui.doc.body.textContent, /undefined/);
  } finally { ui.dom.window.close(); }
});

test('a config save marked saved still says it was saved', async () => {
  const ui = await setup({'/api/config': (options) => options.method === 'POST'
    ? {saved: true, config: {}}
    : {saved: {}, keys: [{provider: 'openai', configured: false, env_var: 'OPENAI_API_KEY'}], data_env: [], cloud_env: []}});
  try {
    await ui.click('[data-page=models]');
    ui.doc.querySelector('#model-advanced').open = true;
    await ui.click('#model-advanced-form button[type=submit]');
    await settleMany();
    assert.match(ui.doc.querySelector('#model-advanced-feedback').textContent, /^已保存/);
  } finally { ui.dom.window.close(); }
});

test('W59: the empty status line under 保存高级设置 draws nothing until there is text', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=models]');
    const box = ui.doc.querySelector('#model-advanced-feedback');
    assert.ok(box);
    assert.equal(box.textContent, '');
    assert.equal(ui.window.getComputedStyle(box).display, 'none');
    box.textContent = '已保存';
    assert.notEqual(ui.window.getComputedStyle(box).display, 'none');
  } finally { ui.dom.window.close(); }
});

test('W67: a cut 「未知工具（…）」 run-table cell carries the whole id in title=', async () => {
  const full = 'x'.repeat(90);
  const named = (path) => {
    const data = routedRuns(path);
    data.runs = data.runs.map((item, i) => ({...item, framework: i === 1 ? full : 'tradingagents',
      framework_name: i === 1 ? `未知工具（${full.slice(0, 64)}…）` : 'TradingAgents'}));
    return data;
  };
  const ui = await setup({'/api/runs': (options, path) => named(path), '/api/logs': {lines: []}}, {url: 'http://localhost/#status'});
  try {
    await settleMany();
    const cut = ui.doc.querySelector('#run-list [data-run-id="run-nvda"] .fwc');
    assert.equal(cut.getAttribute('title'), `未知工具（${full}）`);
    assert.equal(ui.doc.querySelector('#run-list [data-run-id="run-btc"] .fwc').getAttribute('title'), 'TradingAgents');
  } finally { ui.dom.window.close(); }
});

// ---- SEC-002 (B4)
const SEC_HTML = () => markup.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n  <meta name="tradingagents-token" content="api-token-SECRET">\n  <meta name="tradingagents-link-key" content="link-KEY-42">');

test('W1: every report link carries the link key, never the API token', async () => {
  const dir = '/tmp/results/reports/NVDA_2026-10-01';
  const ui = await setup({'/api/reports': {reports: [{name: 'NVDA_2026-10-01', html: `${dir}/complete_report.html`, markdown: `${dir}/complete_report.md`}], live: [], directory: '/tmp/results'},
    '/api/reports/outline': {name: 'NVDA', rating: null, header: [], sections: []}}, {html: SEC_HTML()});
  try {
    ui.ui.showPage('reports');
    for (let i = 0; i < 6; i++) await settle();
    const links = [...ui.doc.querySelectorAll('a[href*="/api/reports/file"]')];
    assert.ok(links.length >= 2, 'report links rendered');
    for (const link of links) {
      assert.match(link.getAttribute('href'), /&k=link-KEY-42$/);
      assert.doesNotMatch(link.getAttribute('href'), /api-token-SECRET/);
    }
  } finally { ui.dom.window.close(); }
});

test('W34: report links: http(s) opens outside with no referrer, file links go through /api/reports/file, other schemes stay text', async () => {
  const dir = '/tmp/results/reports/NVDA_2026-10-01';
  const text = [
    '# 结论',
    'See [官网](https://example.com/a?b=1&c="x") and [市场报告](1_analysts/market.md) and [上级](../other/x.md)',
    'Bad [点我](javascript:alert(1)) and [数据](data:text/html,hi) and [本地](file:///tmp/results/reports/a%20b.md) and [锚](#top)',
    'And [<b>粗</b>](https://example.org/)',
  ].join('\n');
  const ui = await setup({'/api/reports': {reports: [{name: 'NVDA_2026-10-01', html: `${dir}/complete_report.html`, markdown: `${dir}/complete_report.md`}], live: [], directory: '/tmp/results'},
    '/api/reports/outline': {name: 'NVDA', rating: null, header: [], sections: [{folder: '5_portfolio', items: [{file: 'decision.md', agent: 'PM', zh: '组合经理', text}]}]}}, {html: SEC_HTML()});
  try {
    ui.ui.showPage('reports');
    for (let i = 0; i < 8; i++) await settle();
    const body = ui.doc.querySelector('#report-body');
    assert.ok(body, 'report body rendered');
    const ext = [...body.querySelectorAll('a.md-link--external')];
    assert.equal(ext.length, 2);
    assert.equal(ext[0].getAttribute('href'), 'https://example.com/a?b=1&c="x"');
    assert.equal(ext[0].getAttribute('rel'), 'noopener noreferrer nofollow');
    assert.equal(ext[0].getAttribute('target'), '_blank');
    assert.equal(ext[1].innerHTML, '&lt;b&gt;粗&lt;/b&gt;', 'the label is escaped');
    const files = [...body.querySelectorAll('a.md-link--file')].map((a) => a.getAttribute('href'));
    assert.deepEqual(files, [
      `/api/reports/file?path=${encodeURIComponent(`${dir}/1_analysts/market.md`)}&k=link-KEY-42`,
      `/api/reports/file?path=${encodeURIComponent('/tmp/results/reports/other/x.md')}&k=link-KEY-42`,
      `/api/reports/file?path=${encodeURIComponent('/tmp/results/reports/a b.md')}&k=link-KEY-42`,
    ]);
    assert.equal(body.querySelectorAll('a').length, 5, 'javascript:, data: and #anchor are not links');
    assert.match(body.textContent, /点我/);
    for (const a of body.querySelectorAll('a')) assert.match(a.getAttribute('href'), /^(https:\/\/|\/api\/reports\/file\?)/);
    assert.doesNotMatch(body.innerHTML, /href="(javascript|data):/);
  } finally { ui.dom.window.close(); }
});

// ---- D1
test('W82: a stop refusal goes away once the run is no longer running (stopped outside the UI)', async () => {
  const sentence = '读不到这个任务进程的启动时间，无法确认它还是本任务的分析进程。\n下一步：稍后点「刷新」再试。';
  const ui = await multiSetup({'/api/runs/stop': {__status: 409, body: {error: sentence}}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'run-btc');
    await ui.click('#stop-run');
    await ui.click('#confirm-yes');
    assert.ok(ui.doc.querySelector('#stop-refused'));
    await ui.ui.renderRuns(); await settleMany();
    assert.ok(ui.doc.querySelector('#stop-refused'), 'still running: the refusal stays (W76)');
    ui.window.fetch = ((inner) => async (path, options = {}) => {
      const response = await inner(path, options);
      if (!String(path).startsWith('/api/runs')) return response;
      const data = await response.json();
      for (const row of data.runs || []) if (row.id === 'run-btc') row.status = 'stopped';
      if (data.run && data.run.id === 'run-btc') data.run.status = 'stopped';
      return {...response, ok: true, json: async () => data};
    })(ui.window.fetch);
    await ui.ui.renderRuns(); await settleMany();
    assert.equal(ui.doc.querySelector('#stop-refused'), null, 'the run ended: no stale 「任务仍算运行中」 banner');
  } finally { ui.dom.window.close(); }
});

test('W83: a ticker made only of zero-width characters is empty in the wizard', async () => {
  const ui = await setup({'/api/runs': (options) => options.method ? {id: 'fixture-run'} : {run: null, progress: {}}});
  try {
    ui.fill('#ticker', '\u200b\u200b'); ui.fill('#date', '2026-10-01');
    await ui.click('#step-next');
    assert.equal(ui.ui.state.step, 1);
    assert.match(ui.doc.querySelector('#form-note').textContent, /标的代码/);
    ui.fill('#ticker', '\ufeff');
    await ui.click('#step-next');
    assert.equal(ui.ui.state.step, 1);
    ui.fill('#ticker', 'NVDA');
    await ui.click('#step-next');
    assert.equal(ui.ui.state.step, 2);
  } finally { ui.dom.window.close(); }
});

test('W122: tag and musical format characters are invisible for ticker validation', async () => {
  const ui = await setup({'/api/runs': (options) => options.method ? {id: 'fixture-run'} : {run: null, progress: {}}});
  try {
    const invisible = '\u{e0001}\u{1d173}';
    ui.fill('#ticker', invisible); ui.fill('#date', '2026-10-01');
    await ui.click('#step-next');
    assert.equal(ui.ui.state.step, 1, 'only tag/format characters cannot advance');
    assert.match(ui.doc.querySelector('#form-note').textContent, /标的代码/);
    assert.equal(ui.calls.filter((item) => item.path === '/api/runs' && item.method === 'POST').length, 0);

    ui.fill('#ticker', `N${'\u{e0001}'}V${'\u{1d173}'}DA`);
    await ui.click('#step-next');
    assert.equal(ui.ui.state.step, 2, 'visible NVDA remains valid when format characters are interleaved');
  } finally { ui.dom.window.close(); }
});

// V2-KLINE-RT: the K 线行情 page polls the newest bar only while visible.
async function klineSetup(asset, pollSeconds, klineRoute, extraRoutes = {}) {
  const bars = [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}, {time: 1762041600, open: 10.5, high: 12, low: 10, close: 11, volume: 6}];
  const latest = {asset_type: asset, poll_seconds: pollSeconds, delayed: asset !== 'crypto', bar: {time: 1762041600, open: 10.5, high: 13, low: 10, close: 12.5, volume: 9}};
  const ui = await setup({
    '/api/kline': klineRoute || {symbol: 'X', canonical: 'X', asset_type: asset, tf: '1D', provider: asset === 'crypto' ? 'okx' : 'yahoo', provider_symbol: 'X', is_future: asset === 'commodity', bars},
    '/api/kline/latest': latest,
    ...extraRoutes,
  });
  const updates = [];
  const setDataCalls = [];
  const series = {setData: (p) => setDataCalls.push(p), update: (p) => updates.push(p), priceScale: () => ({applyOptions() {}})};
  ui.window.LightweightCharts = {createChart: () => ({addCandlestickSeries: () => series, addHistogramSeries: () => series,
    timeScale: () => ({fitContent() {}, subscribeVisibleLogicalRangeChange() {}, setVisibleLogicalRange() {}}), subscribeCrosshairMove() {}, remove() {}})};
  let visibility = 'visible';
  Object.defineProperty(ui.doc, 'visibilityState', {configurable: true, get: () => visibility});
  const timers = new Map(); let next = 1;
  ui.window.setTimeout = (fn, ms) => { timers.set(next, {fn, ms}); return next++; };
  ui.window.clearTimeout = (id) => { timers.delete(id); };
  const flush = async () => { for (let i = 0; i < 8; i++) await settle(); };
  const runTimers = async () => { const due = [...timers.entries()]; timers.clear(); for (const [, t] of due) t.fn(); await flush(); };
  const setVisible = async (value) => { visibility = value; ui.doc.dispatchEvent(new ui.window.Event('visibilitychange')); await flush(); };
  await ui.click('.nav-item[data-page=kline]'); await flush();
  const latestCalls = () => ui.calls.filter((c) => c.path.startsWith('/api/kline/latest')).length;
  return {...ui, updates, setDataCalls, timers, runTimers, setVisible, latestCalls};
}

test('K 线行情 stock/metal polls every 60 s as 知情 and pauses while the tab is hidden (V2-KLINE-RT)', async () => {
  const ui = await klineSetup('commodity', 60);
  try {
    assert.deepEqual([...ui.timers.values()].map((t) => t.ms), [0]);
    await ui.runTimers();
    assert.equal(ui.latestCalls(), 1);
    assert.equal(ui.updates.at(-2).close, 12.5, 'the forming bar replaces the last candle');
    assert.equal(ui.doc.querySelector('#kline-price').textContent, '12.50');
    assert.deepEqual([...ui.timers.values()].map((t) => t.ms), [60000]);
    assert.match(ui.doc.querySelector('#kline-live').textContent, /知情.*60 秒/);
    assert.match(ui.doc.querySelector('#kline-note').textContent, /知情.*延时行情.*不是逐笔/);
    assert.doesNotMatch(ui.doc.querySelector('#kline-note').textContent, /可能未更新/);
    assert.doesNotMatch(ui.doc.querySelector('#kline-live').textContent, /可能未更新/);
    await ui.setVisible('hidden');
    assert.equal(ui.timers.size, 0, 'hidden: no poll scheduled');
    assert.match(ui.doc.querySelector('#kline-live').textContent, /已暂停/);
    await ui.runTimers();
    assert.equal(ui.latestCalls(), 1);
    await ui.setVisible('visible');
    assert.deepEqual([...ui.timers.values()].map((t) => t.ms), [0], 'visible again: polls at once');
    await ui.runTimers();
    assert.equal(ui.latestCalls(), 2);
  } finally { ui.dom.window.close(); }
});

test('K 线可能未更新写在页脚和实时标记上，没有这条说明时不编造', async () => {
  const ui = await klineSetup('stock', 60, {
    symbol: '600519', canonical: '600519.SS', asset_type: 'stock', tf: '1D', provider: 'yahoo',
    provider_symbol: '600519.SS', freshness_note: '可能未更新', history_label: '近1年',
    bars: [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}, {time: 1762041600, open: 10.5, high: 12, low: 10, close: 11, volume: 6}],
  }, {
    '/api/kline/latest': {
      asset_type: 'stock', poll_seconds: 60, delayed: true, freshness_note: '可能未更新',
      market_state: 'open', provider: 'yahoo', provider_symbol: '600519.SS',
      bar: {time: 1762041600, open: 10.5, high: 13, low: 10, close: 12.5, volume: 9},
    },
  });
  try {
    const note = ui.doc.querySelector('#kline-note').textContent;
    const live = ui.doc.querySelector('#kline-live').textContent;
    assert.match(note, /可能未更新/);
    assert.match(live, /知情/);
    assert.match(live, /询问/);
    assert.match(live, /可能未更新/);
    assert.doesNotMatch(live, /秒更新/);
    assert.doesNotMatch(live, /实时/);
    assert.doesNotMatch(ui.doc.querySelector('#page').textContent, /Yahoo|yahoo/);
    await ui.runTimers();
    assert.match(ui.doc.querySelector('#kline-live').textContent, /可能未更新/);
    assert.doesNotMatch(ui.doc.querySelector('#kline-live').textContent, /Yahoo|yahoo/);
  } finally { ui.dom.window.close(); }
});

test('crypto Yahoo fallback is not called realtime', async () => {
  const ui = await klineSetup('crypto', 5, {
    symbol: 'BTC-USDT', canonical: 'BTC-USD', asset_type: 'crypto', tf: '1D', provider: 'yahoo',
    provider_symbol: 'BTC-USD', delayed: true, history_label: '近1年',
    bars: [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}, {time: 1762041600, open: 10.5, high: 12, low: 10, close: 11, volume: 6}],
  }, {
    '/api/kline/latest': {
      asset_type: 'crypto', poll_seconds: 5, delayed: true, provider: 'yahoo', provider_symbol: 'BTC-USD',
      bar: {time: 1762041600, open: 10.5, high: 13, low: 10, close: 12.5, volume: 9},
    },
  });
  try {
    const live = ui.doc.querySelector('#kline-live').textContent;
    assert.match(live, /备用行情，不是逐笔实时/);
    assert.doesNotMatch(live, /实时 ·|约 5 秒更新/);
    assert.doesNotMatch(ui.doc.querySelector('#page').textContent, /Yahoo|yahoo/);
    assert.match(ui.doc.querySelector('#kline-note').textContent, /不是逐笔实时/);
  } finally { ui.dom.window.close(); }
});

test('crypto without a delayed field is not called realtime', async () => {
  const ui = await klineSetup('crypto', 5, {
    symbol: 'BTC-USDT', canonical: 'BTC-USD', asset_type: 'crypto', tf: '1D', provider: 'okx',
    provider_symbol: 'BTC-USDT', history_label: '近1年',
    bars: [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}, {time: 1762041600, open: 10.5, high: 12, low: 10, close: 11, volume: 6}],
  }, {
    '/api/kline/latest': {
      asset_type: 'crypto', poll_seconds: 5, provider: 'okx', provider_symbol: 'BTC-USDT',
      bar: {time: 1762041600, open: 10.5, high: 13, low: 10, close: 12.5, volume: 9},
    },
  });
  try {
    const live = ui.doc.querySelector('#kline-live').textContent;
    const note = ui.doc.querySelector('#kline-note').textContent;
    assert.match(live, /延时未知，不写成实时/);
    assert.match(note, /延时未知，不写成实时/);
    assert.doesNotMatch(live, /实时 ·|约 5 秒更新/);
    assert.doesNotMatch(note, /最后一根约每 5 秒刷新/);
    await ui.runTimers();
    assert.match(ui.doc.querySelector('#kline-live').textContent, /延时未知，不写成实时/);
    assert.doesNotMatch(ui.doc.querySelector('#kline-live').textContent, /实时 ·/);
  } finally { ui.dom.window.close(); }
});

test('K 线行情 crypto polls about every 5 s and stops when leaving the page (V2-KLINE-RT)', async () => {
  const ui = await klineSetup('crypto', 5);
  try {
    await ui.runTimers();
    assert.deepEqual([...ui.timers.values()].map((t) => t.ms), [5000]);
    assert.match(ui.doc.querySelector('#kline-live').textContent, /实时.*5 秒/);
    assert.doesNotMatch(ui.doc.querySelector('#page').textContent, /tradingview/i);
    await ui.click('.nav-item[data-page=reports]');
    await ui.runTimers();
    assert.equal(ui.latestCalls(), 1, 'a timer firing after leaving the page does not poll');
    assert.equal(ui.timers.size, 0);
  } finally { ui.dom.window.close(); }
});

test('启动分析第 2 步可以导入本地 CSV/JSON，清除后不再带入', async () => {
  const ui = await setup({}, {url: WIZARD_URL});
  try {
    const doc = ui.doc;
    assert.ok(doc.querySelector('#local-import-file'), 'the import control is in the wizard');
    assert.match(doc.querySelector('#local-import-details').textContent, /不会联网抓取/);
    ui.ui.state.localImport = {filename: 'notes.csv', count: 2, items: [{title: 'a'}, {title: 'b'}]};
    ui.ui.state.form.analysts = ['market'];
    ui.ui.state.step = 2;
    assert.match(ui.window.analysisStepError(), /未选新闻分析师/);
    ui.ui.state.form.analysts = ['news'];
    assert.equal(ui.window.analysisStepError(), '');
    assert.deepEqual(ui.window.readForm().local_import, [{title: 'a'}, {title: 'b'}]);
    doc.querySelector('#local-import-clear').click();
    assert.equal(ui.ui.state.localImport, null);
    assert.equal(ui.window.readForm().local_import, undefined);
  } finally { ui.dom.window.close(); }
});

test('顶栏模型状态按可用宽度显示，不再固定裁成 160px', async () => {
  const ui = await setup();
  try {
    const rule = styleRules(ui.window).find((item) => selectorMatches(item, '.topbar-identity strong'));
    assert.equal(normalizeCssValue(rule.style.maxWidth), '100%');
    assert.equal(ui.window.klineErrorText('VendorUnavailableError: boom'), '行情源暂时连不上，请稍后再试。');
    assert.equal(ui.window.klineErrorText('Failed to fetch'), '行情源暂时连不上，请稍后再试。');
    assert.equal(ui.window.klineErrorText('行情源暂时连不上（BTC-USD），请稍后再试。'), '行情源暂时连不上（BTC-USD），请稍后再试。');
  } finally { ui.dom.window.close(); }
});

test('K 线轮询失败不把异常类名写进页面', async () => {
  const ui = await klineSetup('crypto', 5, null, {
    '/api/kline/latest': {__status: 502, body: {error: 'VendorUnavailableError: boom'}},
  });
  try {
    await ui.runTimers();
    const live = ui.doc.querySelector('#kline-live').textContent;
    assert.match(live, /更新失败/);
    assert.match(live, /行情源暂时连不上/);
    assert.doesNotMatch(live, /VendorUnavailableError|boom/);
  } finally { ui.dom.window.close(); }
});

test('K 线价格轴比成交量轴宽时，两边锁成同一宽度', async () => {
  const bars = [{time: 1761955200, open: 70000, high: 76000, low: 69000, close: 73125.85, volume: 5}];
  const ui = await setup({
    '/api/kline': {symbol: 'BTC-USDT', canonical: 'BTC-USD', asset_type: 'crypto', tf: '1D', provider: 'coinbase', provider_symbol: 'BTC-USD', is_future: false, bars},
    '/api/kline/latest': {asset_type: 'crypto', poll_seconds: 5, delayed: false, bar: bars[0]},
  });
  const applied = [];
  const widths = [84, 72];
  let n = 0;
  ui.window.LightweightCharts = {createChart: () => {
    const index = n++;
    const scale = {
      width: () => widths[index],
      applyOptions(opts) { applied.push({index, minimumWidth: opts.minimumWidth}); if (opts.minimumWidth) widths[index] = opts.minimumWidth; },
    };
    return {
      addCandlestickSeries: () => ({setData() {}, update() {}}),
      addHistogramSeries: () => ({setData() {}, update() {}}),
      timeScale: () => ({fitContent() {}, subscribeVisibleLogicalRangeChange() {}, setVisibleLogicalRange() {}, getVisibleLogicalRange: () => ({from: 0, to: 1})}),
      subscribeCrosshairMove() {},
      priceScale: () => scale,
      remove() {},
    };
  }};
  try {
    await ui.click('.nav-item[data-page=kline]');
    assert.deepEqual(applied.map((item) => item.minimumWidth), [84, 84]);
    assert.equal(widths[0], 84);
    assert.equal(widths[1], 84);
    applied.length = 0;
    ui.window.syncKlineScales();
    assert.equal(applied.length, 0, 'already equal widths are not rewritten');
  } finally { ui.dom.window.close(); }
});

test('K 线行情周线以完整日线重算成交量，重复轮询不累计 (AUDIT-FIX-05)', async () => {
  const daily = [
    {time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5},
    {time: 1762041600, open: 10.5, high: 12, low: 10, close: 11, volume: 6},
  ];
  const week = {time: daily[0].time, open: 10, high: 12, low: 9, close: 11, volume: 11};
  const replies = [
    {time: daily[1].time, open: 10.5, high: 13, low: 10, close: 12.5, volume: 9},
    {time: daily[1].time, open: 10.5, high: 13, low: 10, close: 12.5, volume: 9},
  ];
  const ui = await klineSetup('stock', 60, {
    symbol: 'X', canonical: 'X', asset_type: 'stock', tf: '1W', provider: 'yahoo', provider_symbol: 'X',
    is_future: false, bars: [week], daily_bars: daily,
  }, {'/api/kline/latest': () => ({asset_type: 'stock', poll_seconds: 60, delayed: true, bar: replies.shift() || replies[0]})});
  try {
    await ui.runTimers();
    const volumeUpdates = ui.updates.filter((item) => Object.prototype.hasOwnProperty.call(item, 'value'));
    assert.equal(volumeUpdates.at(-1).value, 14, '5 + forming-day replacement 9');
    await ui.runTimers();
    const repeated = ui.updates.filter((item) => Object.prototype.hasOwnProperty.call(item, 'value'));
    assert.equal(repeated.at(-1).value, 14, 'same forming bar is replaced, not added');
  } finally { ui.dom.window.close(); }
});

test('K 线行情跨日恢复请求完整日线并补齐缺失 bar (AUDIT-FIX-05)', async () => {
  const initial = [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}];
  const recovered = [
    ...initial,
    {time: 1762041600, open: 10.5, high: 12, low: 10, close: 11, volume: 6},
    {time: 1762128000, open: 11, high: 13, low: 10.5, close: 12, volume: 7},
    {time: 1762214400, open: 12, high: 14, low: 11, close: 13, volume: 8},
  ];
  let historyCalls = 0;
  const ui = await klineSetup('stock', 60, async (_options, path) => {
    if (path.includes('refresh=1')) {
      historyCalls += 1;
      return {symbol: 'X', canonical: 'X', asset_type: 'stock', tf: '1D', provider: 'yahoo', provider_symbol: 'X', is_future: false, bars: recovered, daily_bars: recovered};
    }
    return {symbol: 'X', canonical: 'X', asset_type: 'stock', tf: '1D', provider: 'yahoo', provider_symbol: 'X', is_future: false, bars: initial, daily_bars: initial};
  }, {'/api/kline/latest': {asset_type: 'stock', poll_seconds: 60, delayed: true, bar: recovered.at(-1)}});
  try {
    await ui.runTimers();
    assert.equal(historyCalls, 1, 'a multi-day gap refreshes complete history');
    const latestVolumeData = ui.setDataCalls.filter((items) => items.length === recovered.length && items[0].value !== undefined).at(-1);
    assert.deepEqual(Array.from(latestVolumeData, (item) => item.value), [5, 6, 7, 8]);
  } finally { ui.dom.window.close(); }
});

test('K 线月线 OHLCV 等于完整日线重采样，重复轮询不累计 (NEXT-KLINE-VERIFY)', async () => {
  const daily = [
    {time: 1761955200, open: 10, high: 12, low: 9, close: 11, volume: 5},
    {time: 1762041600, open: 11, high: 13, low: 10, close: 12, volume: 6},
  ];
  const month = {time: daily[0].time, open: 10, high: 13, low: 9, close: 12, volume: 11};
  const forming = {time: daily[1].time, open: 11, high: 14, low: 10, close: 13, volume: 8};
  const ui = await klineSetup('stock', 60, {
    symbol: 'X', canonical: 'X', asset_type: 'stock', tf: '1M', provider: 'yahoo', provider_symbol: 'X',
    is_future: false, bars: [month], daily_bars: daily,
  }, {'/api/kline/latest': () => ({asset_type: 'stock', poll_seconds: 60, delayed: true, bar: forming})});
  try {
    await ui.runTimers();
    await ui.runTimers();
    const volume = ui.updates.filter((item) => Object.prototype.hasOwnProperty.call(item, 'value')).at(-1);
    const candle = ui.updates.filter((item) => Object.prototype.hasOwnProperty.call(item, 'close')).at(-1);
    assert.equal(volume.value, 13, '5 + replaced forming day 8, not 5+6+8');
    assert.equal(candle.open, 10);
    assert.equal(candle.high, 14);
    assert.equal(candle.low, 9);
    assert.equal(candle.close, 13);
  } finally { ui.dom.window.close(); }
});

test('K 线暂停跨周后恢复会补齐，过时响应不回滚 (NEXT-KLINE-VERIFY)', async () => {
  const initial = [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}];
  const recovered = [
    ...initial,
    {time: 1762214400, open: 12, high: 14, low: 11, close: 13, volume: 8},
  ];
  let historyCalls = 0;
  let latest = {time: 1761868800, open: 9, high: 10, low: 8, close: 9.5, volume: 4};
  const ui = await klineSetup('stock', 60, async (_options, path) => {
    if (path.includes('refresh=1')) {
      historyCalls += 1;
      return {symbol: 'X', canonical: 'X', asset_type: 'stock', tf: '1W', provider: 'yahoo', provider_symbol: 'X', is_future: false, bars: recovered, daily_bars: recovered};
    }
    return {symbol: 'X', canonical: 'X', asset_type: 'stock', tf: '1W', provider: 'yahoo', provider_symbol: 'X', is_future: false, bars: initial, daily_bars: initial};
  }, {'/api/kline/latest': () => ({asset_type: 'stock', poll_seconds: 60, delayed: true, bar: latest})});
  try {
    await ui.runTimers();
    assert.equal(historyCalls, 0, 'an older bar is not a gap');
    const before = ui.updates.filter((item) => Object.prototype.hasOwnProperty.call(item, 'value')).at(-1)?.value;
    assert.equal(before, undefined, 'stale bar does not replace the drawn week');
    latest = {time: 1762214400, open: 12, high: 14, low: 11, close: 13, volume: 8};
    await ui.setVisible('hidden');
    await ui.setVisible('visible');
    await ui.runTimers();
    assert.equal(historyCalls, 1, 'a paused cross-week gap refreshes complete history');
    const volumes = ui.setDataCalls.filter((items) => items[0] && items[0].value !== undefined).at(-1);
    assert.deepEqual(Array.from(volumes, (item) => item.value), [5, 8]);
  } finally { ui.dom.window.close(); }
});

test('K 线新周期不并入上一周，断网后下一轮恢复 (NEXT-KLINE-VERIFY)', async () => {
  const sunday = 1762041600;
  const monday = sunday + 86400;
  const daily = [{time: sunday, open: 10, high: 11, low: 9, close: 10.5, volume: 6}];
  let fail = true;
  const ui = await klineSetup('stock', 60, {
    symbol: 'X', canonical: 'X', asset_type: 'stock', tf: '1W', provider: 'yahoo', provider_symbol: 'X',
    is_future: false, bars: [{time: sunday, open: 10, high: 11, low: 9, close: 10.5, volume: 6}], daily_bars: daily,
  }, {'/api/kline/latest': () => {
    if (fail) { fail = false; throw new Error('网络中断'); }
    return {asset_type: 'stock', poll_seconds: 60, delayed: true, bar: {time: monday, open: 11, high: 12, low: 10, close: 11.5, volume: 7}};
  }});
  try {
    await ui.runTimers();
    assert.match(ui.doc.querySelector('#kline-live').textContent, /更新失败/);
    await ui.runTimers();
    const candle = ui.updates.filter((item) => Object.prototype.hasOwnProperty.call(item, 'close')).at(-1);
    const volume = ui.updates.filter((item) => Object.prototype.hasOwnProperty.call(item, 'value')).at(-1);
    assert.equal(candle.time, monday);
    assert.equal(candle.volume, undefined);
    assert.equal(volume.value, 7, 'the new week is its own bar, not 6+7');
    assert.match(ui.doc.querySelector('#kline-live').textContent, /知情/);
  } finally { ui.dom.window.close(); }
});

test('the stop confirm does not split 保留 on a narrow screen', async () => {
  const ui = await setup();
  try {
    ui.window.confirmAction('停止当前分析？', '会停止当前任务，已产生的结果是否保留取决于任务进度。', () => {}, ui.doc.body);
    const body = ui.doc.querySelector('#confirm-body');
    assert.equal(body.textContent, '会停止当前任务，已产生的结果是否保留取决于任务进度。');
    assert.match(body.innerHTML, /任务，<br class="notice-break">已产生的结果是否保留<br class="notice-break">取决于任务进度。/);
    assert.doesNotMatch(body.innerHTML, /进<br/);
    assert.doesNotMatch(body.innerHTML, /保<br/);
    assert.match(body.innerHTML, /保留<br class="notice-break">取决于/);
    assert.doesNotMatch(body.textContent, /已经停止|结果已保留/);
  } finally { ui.dom.window.close(); }
});

test('the logout confirm does not split 授权 on a narrow screen', async () => {
  const ui = await setup();
  try {
    ui.window.confirmAction('退出这个订阅？', '本机凭据会被清除。是否撤销服务端授权，以接口返回为准。', () => {}, ui.doc.body);
    const body = ui.doc.querySelector('#confirm-body');
    assert.equal(body.textContent, '本机凭据会被清除。是否撤销服务端授权，以接口返回为准。');
    assert.match(body.innerHTML, /清除。<br class="notice-break">是否撤销服务端授权，<br class="notice-break">以接口返回为准。/);
    assert.doesNotMatch(body.innerHTML, /授<br|权<br/);
    assert.doesNotMatch(body.textContent, /已经清除|授权已撤销/);
  } finally { ui.dom.window.close(); }
});

test('the retry hint does not split 重试 on a narrow screen', async () => {
  const ui = await setup();
  try {
    ui.window.paintReview();
    ui.doc.querySelector('#open-save-drawer').click();
    const hint = ui.doc.querySelector('#retry-hint');
    assert.equal(hint.textContent, '只在超时、断网和服务端出错时重试，不会因为登录失效反复重试。');
    assert.match(hint.innerHTML, /重试，<br class="notice-break">不会因为登录失效反复重试。/);
    assert.doesNotMatch(hint.innerHTML, /重<br|试<br/);
    assert.doesNotMatch(hint.textContent, /已经重试|登录已失效/);
  } finally { ui.dom.window.close(); }
});

test('the launch review does not split 展开 on a narrow screen', async () => {
  const ui = await setup();
  try {
    ui.window.paintReview();
    const row = [...ui.doc.querySelectorAll('#analysis-summary .kv div, #analysis-summary dt')];
    const report = [...ui.doc.querySelectorAll('#analysis-summary dd')].find((item) => item.textContent.includes('展开全文'));
    assert.ok(report, 'report summary');
    assert.equal(report.textContent, '保存 Markdown 和 HTML，完成后展开全文');
    assert.match(report.innerHTML, /HTML，<br class="notice-break">完成后展开全文/);
    assert.doesNotMatch(report.innerHTML, /展<br|开<br/);
    assert.doesNotMatch(report.textContent, /已经展开|报告已改/);
  } finally { ui.dom.window.close(); }
});

test('the progress source note does not split 状态 on a narrow screen', async () => {
  const run = {id: 'probe-idle', status: 'finished', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA', date: '2026-11-12'}};
  const ui = await setup({'/api/runs': {run, runs: [run], progress: {agents: [{role: 'market', zh: '市场分析师', status: 'completed', team_zh: '分析师'}]}, result: {present: false}}});
  try {
    await openRunFromList(ui, 'probe-idle');
    const note = ui.doc.querySelector('#progress-source-note');
    assert.match(note.textContent, /^详细列：/);
    assert.match(note.textContent, /。状态：等待 pending、进行中 in_progress、完成 completed、错误 error。$/);
    assert.match(note.innerHTML, /。<br class="notice-break">状态：等待 pending、进行中 in_progress、完成 completed、错误 error。/);
    assert.doesNotMatch(note.innerHTML, /状<br/);
    assert.doesNotMatch(note.textContent, /已经完成计时|状态已改/);
  } finally { ui.dom.window.close(); }
});

test('the tier role note does not split 服务 on a narrow screen', async () => {
  const ui = await setup();
  try {
    const note = ui.doc.querySelector('#tier-role-note');
    assert.equal(note.textContent, '快模型负责资料分析与辩论，深模型负责综合判断。可使用同一服务中的不同模型。');
    assert.match(note.innerHTML, /辩论，<br class="notice-break">深模型负责综合判断。<br class="notice-break">可使用同一服务中的不同模型。/);
    assert.doesNotMatch(note.innerHTML, /服<br|务<br/);
    assert.doesNotMatch(note.textContent, /已经选择|模型已改/);
  } finally { ui.dom.window.close(); }
});

test('the local import hint does not split 内容 on a narrow screen', async () => {
  const ui = await setup();
  try {
    const hint = ui.doc.querySelector('#local-import-hint');
    assert.equal(hint.textContent, '只读取你自己导出的文件，不会联网抓取。新闻分析师会把内容当作证据，报告标明「本地导入」。');
    assert.match(hint.innerHTML, /文件，<br class="notice-break">不会联网抓取。<br class="notice-break">新闻分析师会把内容当作证据，<br class="notice-break">/);
    assert.doesNotMatch(hint.innerHTML, /内<br|容<br/);
    assert.doesNotMatch(hint.textContent, /已经导入|已经联网/);
  } finally { ui.dom.window.close(); }
});

test('the info crawl page note does not split 网站 on a narrow screen', async () => {
  const ui = await setup({
    '/api/frameworks': {...FRAMEWORKS, tools: [{id: 'info-crawl', name: '信息抓取', description: '官方来源', icon: '/icons/logo.svg', tags: ['公告'], tool: true}]},
    '/api/info-crawl/sources': {inject_default: false, existing: [], sources: []},
  }, {url: 'http://localhost/#start'});
  try {
    await ui.click('.fw[data-framework="info-crawl"]');
    for (let i = 0; i < 6; i++) await settle();
    const note = ui.doc.querySelector('#info-crawl-note');
    assert.equal(note.textContent, '只读官方公开接口和新闻稿 RSS，不登录、不抓社交网站。结果仅供个人研究参考，抓取条目保留 30 天后自动清理。');
    assert.match(note.innerHTML, /RSS，<br class="notice-break">不登录、不抓社交网站。<br class="notice-break">结果仅供个人研究参考，<br>抓取条目保留 30 天后自动清理。/);
    assert.doesNotMatch(note.innerHTML, /清<br/);
    assert.doesNotMatch(note.innerHTML, /网<br|站<br/);
    assert.doesNotMatch(note.textContent, /已经清理|已经登录/);
  } finally { ui.dom.window.close(); }
});

test('the model refusal note does not split 两项 on a narrow screen', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=models]');
    ui.doc.querySelector('#model-advanced').open = true;
    const note = ui.doc.querySelector('#model-refusal-note');
    assert.equal(note.textContent, '当前快模型和深模型都不是 ChatGPT 订阅，这两项会照常发送。');
    assert.match(note.innerHTML, /订阅，<br class="notice-break">这两项会照常发送。/);
    assert.doesNotMatch(note.innerHTML, /两<br|项<br/);
    assert.doesNotMatch(note.textContent, /已经发送|订阅已改/);
  } finally { ui.dom.window.close(); }
});

test('the info crawl card does not split 财政部 on a narrow screen', async () => {
  const description = '从官方公开来源（SEC 公告、交易所公告、央行与财政部新闻稿、GDELT 新闻索引）汇总资讯，可勾选「带入分析」。';
  const ui = await setup({
    '/api/frameworks': {...FRAMEWORKS, tools: [{id: 'info-crawl', name: '信息抓取', description, icon: '/icons/logo.svg', tags: ['公告'], tool: true}]},
  }, {url: 'http://localhost/#start'});
  try {
    const card = ui.doc.querySelector('.fw[data-framework="info-crawl"] .fw-d');
    assert.equal(card.textContent, description);
    assert.match(card.innerHTML, /公告、<br>交易所公告、<br>央行与财政部新闻稿、/);
    assert.doesNotMatch(card.innerHTML, /notice-break/);
    assert.doesNotMatch(card.innerHTML, /财<br|政<br/);
    assert.doesNotMatch(card.textContent, /已经汇总|来源已改/);
  } finally { ui.dom.window.close(); }
});

test('信息抓取: card on 启动分析, gray unavailable source, 不可用 status, 带入分析 default off (V2-INFO-CRAWL)', async () => {
  const sources = {inject_default: false, existing: [{id: 'yahoo', name: 'Yahoo', note: '条款灰区'}], sources: [
    {id: 'sec_edgar', name: 'SEC EDGAR 公告', kind: 'filing', assets: ['stock'], terms_url: 'https://www.sec.gov/os/accessing-edgar-data', description: '官方披露', optional: false, available: true, reason: ''},
    {id: 'gdelt', name: 'GDELT 全球新闻索引', kind: 'news', assets: ['stock'], terms_url: 'https://www.gdeltproject.org/about.html', description: '新闻', optional: false, available: true, reason: ''},
    {id: 'keyed', name: '需密钥来源', kind: 'news', assets: ['stock'], terms_url: '', description: '', optional: false, available: false, reason: '未配置 DEMO_KEY，该来源暂不可用'},
    {id: 'prnewswire', name: 'PR Newswire', kind: 'press', assets: ['stock'], terms_url: '', description: '个人使用', optional: true, personal_use: true, available: true, reason: ''},
  ]};
  const job = {id: 'ic-20261103-abcdef12', keyword: 'NVDA', asset_type: 'stock', hours: 72, status: 'partial',
    sources: {sec_edgar: {status: 'ok', count: 1, reason: ''}, gdelt: {status: 'unavailable', count: 0, reason: '请求过于频繁，已被来源限流（HTTP 429）'}}};
  let posted = null;
  let runBody = null;
  const ui = await setup({
    '/api/frameworks': {...FRAMEWORKS, tools: [{id: 'info-crawl', name: '信息抓取', description: '官方来源', icon: '/icons/logo.svg', tags: ['公告'], tool: true}]},
    '/api/info-crawl/sources': sources,
    '/api/info-crawl/jobs': (options) => { if (options.method === 'POST') { posted = JSON.parse(options.body); return {job: {...job, status: 'running'}}; } return {job}; },
    '/api/info-crawl/items': {job, items: [{id: 'aaaaaaaaaaaaaaaa', source: 'sec_edgar', source_name: 'SEC EDGAR 公告', kind: 'filing', title: 'NVIDIA 10-Q', url: 'https://www.sec.gov/x', published_at: '2026-11-01T20:05:00Z', summary: ''}, {id: 'bbbbbbbbbbbbbbbb', source: 'sec_edgar', source_name: 'SEC EDGAR 公告', kind: 'filing', title: 'NVIDIA 8-K', url: 'https://www.sec.gov/y', published_at: '2026-10-31T23:30:00Z', summary: ''}]},
    '/api/runs': (options) => { if (options.method) { runBody = JSON.parse(options.body); return {id: 'r1'}; } return {run: null, progress: {}}; },
  }, {url: 'http://localhost/#start'});
  try {
    const {doc, click, fill} = ui;
    assert.ok(doc.querySelector('.fw[data-framework="info-crawl"]'), 'second card');
    await click('.fw[data-framework="info-crawl"]');
    for (let i = 0; i < 6; i++) await settle();
    assert.match(ui.window.location.hash, /#start\/info-crawl/);
    const keyed = doc.querySelector('[data-source="keyed"] input');
    assert.equal(keyed.disabled, true);
    assert.match(doc.querySelector('[data-source="keyed"]').textContent, /未配置 DEMO_KEY/);
    assert.equal(doc.querySelector('[data-source="prnewswire"] input').checked, false, 'optional off by default');
    ui.ui.state.infoPollMs = 0;
    fill('#ic-keyword', 'NVDA');
    doc.querySelector('#ic-form').dispatchEvent(new ui.window.Event('submit', {cancelable: true}));
    for (let i = 0; i < 3; i++) { await new Promise((r) => setTimeout(r, 15)); await settleMany(); }
    assert.deepEqual(posted, {keyword: 'NVDA', asset_type: 'stock', hours: 72, sources: ['sec_edgar', 'gdelt']});
    assert.match(doc.querySelector('[data-source-status="gdelt"]').textContent, /不可用/);
    assert.doesNotMatch(doc.querySelector('#ic-status').textContent, /无数据/);
    assert.equal(doc.querySelectorAll('.ic-item').length, 2);
    assert.match(doc.querySelector('#ic-grp-opt').nextElementSibling.textContent, /仅限个人使用/);
    assert.match(doc.querySelector('.ic-item .ic-time').textContent, /^2026-11-01 20:05 UTC$/);
    assert.match(doc.querySelector('.ic-kind').textContent, /监管披露/);
    assert.match(ui.visibleText(), /保留 30 天/);
    assert.equal(doc.querySelector('#ic-inject'), null, 'no batch checkbox');
    const picks = [...doc.querySelectorAll('.ic-pick')];
    assert.deepEqual(picks.map((box) => box.checked), [false, false], '带入分析 default off per row');
    const go = doc.querySelector('#ic-to-analysis');
    assert.equal(go.disabled, true);
    picks[1].checked = true;
    picks[1].dispatchEvent(new ui.window.Event('change'));
    assert.equal(go.disabled, false);
    assert.match(go.textContent, /1 条/);
    await click('#ic-to-analysis');
    assert.ok(doc.querySelector('#info-attach'));
    assert.match(doc.querySelector('#info-attach').textContent, /1 条/);
    assert.equal(doc.querySelector('#ticker').value, 'NVDA');
    assert.deepEqual(JSON.parse(JSON.stringify(ui.window.withInfoAttach({ticker: 'NVDA'}))), {ticker: 'NVDA', info_crawl_job: job.id, info_inject: ['bbbbbbbbbbbbbbbb']});
    await click('#info-attach-clear');
    assert.deepEqual(JSON.parse(JSON.stringify(ui.window.withInfoAttach({ticker: 'NVDA'}))), {ticker: 'NVDA'});
    assert.equal(runBody, null);
  } finally { ui.window.close(); }
});

test('AUDIT-CHECK-02 / W214: no-news hint sits on its own line and follows the final analyst list', async () => {
  const ui = await setup();
  try {
    const {doc} = ui;
    ui.ui.state.infoAttach = {job: 'ic-20261103-abcdef12', keyword: 'NVDA', items: ['aaaaaaaaaaaaaaaa']};
    ui.ui.state.form.analysts = ['market'];
    const host = doc.createElement('div');
    doc.body.append(host);
    host.innerHTML = ui.window.uiTest.infoAttachHtml();
    const hint = () => doc.querySelector('#info-attach-nonews');
    assert.equal(hint().hidden, false);
    assert.equal(hint().textContent, '未选新闻分析师，附件不会被使用。', "Eric's wording with the period");
    assert.equal(hint().parentElement.id, 'info-attach');
    assert.match(doc.querySelector('#info-attach').innerHTML, /<br class="notice-break">报告会附来源引用。/, "Eric's notice-break kept");
    assert.doesNotMatch(doc.querySelector('#info-attach').textContent, /本次未带入/, 'no extra unused-count clause');
    ui.ui.state.form.analysts = ['market', 'news'];
    ui.window.refreshInfoAttachHint();
    assert.equal(hint().hidden, true);
    assert.equal(hint().textContent, '');
    ui.ui.state.envSkipped = {...(ui.ui.state.envSkipped || {}), analysts: ['market']};
    ui.window.refreshInfoAttachHint();
    assert.equal(hint().hidden, false, 'env override without news shows the hint');
    ui.ui.state.form.analysts = ['market'];
    ui.ui.state.envSkipped = {...ui.ui.state.envSkipped, analysts: ['news']};
    ui.window.refreshInfoAttachHint();
    assert.equal(hint().hidden, true, 'env override adding news hides it');
    delete ui.ui.state.envSkipped.analysts;
    // W218: the page never gates start on the hint; the attach still travels and the server decides.
    ui.ui.state.form.analysts = ['market'];
    assert.deepEqual(JSON.parse(JSON.stringify(ui.window.withInfoAttach({ticker: 'NVDA'}))).info_inject, ['aaaaaaaaaaaaaaaa']);
    const css = readFileSync(resolve(root, 'webui/static/app.css'), 'utf8');
    assert.match(css, /#info-attach-nonews \{ display: block; margin-top: 6px; \}/);
    assert.match(css, /#info-attach-nonews\[hidden\] \{ display: none !important; \}/);
  } finally { ui.window.close(); }
});

test('W218: 配置检查 sends attach like start; page never refuses for missing ticks', async () => {
  // Setup page validate uses withInfoAttach (same payload as start). Existence of
  // ticked rows is the server's job; without news the server must allow start.
  let validateBody = null;
  const ui = await setup({
    '/api/runs/validate': (options) => { validateBody = JSON.parse(options.body); return {ok: true, ignored: []}; },
  });
  try {
    const {doc, click} = ui;
    ui.ui.state.infoAttach = {job: 'ic-20261103-abcdef12', keyword: 'NVDA', items: ['aaaaaaaaaaaaaaaa']};
    ui.ui.state.form.analysts = ['market'];
    ui.window.showStep(4);
    for (let i = 0; i < 4; i++) await settle();
    assert.equal(doc.querySelector('#check-analysis').hidden, false);
    await click('#check-analysis');
    for (let i = 0; i < 4; i++) await settle();
    assert.ok(validateBody, 'validate was called');
    assert.equal(validateBody.info_crawl_job, 'ic-20261103-abcdef12');
    assert.deepEqual(validateBody.info_inject, ['aaaaaaaaaaaaaaaa']);
    assert.ok(!/已找不到|附件已失效/.test(String(doc.querySelector('#form-note')?.textContent || '')),
      'page does not invent a missing-items refusal');
    validateBody = null;
    ui.ui.state.envSkipped = {...(ui.ui.state.envSkipped || {}), analysts: ['market']};
    ui.ui.state.form.analysts = ['market', 'news'];
    assert.equal(ui.window.infoAttachUnused(), true);
    await click('#check-analysis');
    for (let i = 0; i < 4; i++) await settle();
    assert.deepEqual(validateBody.info_inject, ['aaaaaaaaaaaaaaaa']);
  } finally { ui.window.close(); }
});

test('K 线行情: Yahoo is never named in the source pill; period word follows the timeframe (W201, Eric rule)', async () => {
  const bars = [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}];
  let resp = {symbol: 'NVDA', canonical: 'NVDA', asset_type: 'stock', tf: '1D', provider: 'yahoo', provider_symbol: 'NVDA', source_label: null, fallback: false, adjusted: true, bars, daily_bars: bars};
  const ui = await klineSetup('stock', 60, async (options, path) => ({...resp, tf: (new URLSearchParams(path.split('?')[1] || '').get('tf') || resp.tf)}));
  try {
    const doc = ui.doc;
    const flush = async () => { for (let i = 0; i < 8; i++) await settle(); };
    await flush();
    assert.equal(doc.querySelector('#kline-source').textContent, '数据源：股票日线 NVDA');
    for (const [tf, word] of [['1W', '周线'], ['1M', '月线'], ['1D', '日线']]) {
      doc.querySelector(`#kline-tf [data-tf="${tf}"]`).click(); await flush();
      assert.equal(doc.querySelector('#kline-source').textContent, `数据源：股票${word} NVDA`, `pill follows ${tf}`);
    }
    // Even a stale server label must not put Yahoo on the pill.
    resp = {...resp, source_label: 'Yahoo'};
    doc.querySelector('#kline-tf [data-tf="1W"]').click(); await flush();
    doc.querySelector('#kline-tf [data-tf="1D"]').click(); await flush();
    assert.doesNotMatch(doc.querySelector('#kline-source').textContent, /Yahoo/);
    const input = doc.querySelector('#kline-symbol');
    resp = {...resp, symbol: 'XAUUSD', canonical: 'GC=F', asset_type: 'commodity', provider_symbol: 'GC=F', is_future: true, source_label: null};
    input.value = 'XAUUSD';
    doc.querySelector('#kline-form').dispatchEvent(new ui.window.Event('submit', {cancelable: true})); await flush();
    assert.equal(doc.querySelector('#kline-source').textContent, '数据源：期货日线 GC=F');
    assert.doesNotMatch(doc.querySelector('#kline-source').textContent, /Yahoo/);
  } finally { ui.dom.window.close(); }
});

test('an unrecognized info source is not called unavailable or required', async () => {
  const sources = {inject_default: false, existing: [], sources: [
    {id: 'mystery', name: '未知来源', kind: 'news', assets: ['stock'], terms_url: '', description: '不该显示成可用说明', optional: null, available: null, reason: '未配置 DEMO_KEY，该来源暂不可用'},
    {id: 'keyed', name: '需密钥来源', kind: 'news', assets: ['stock'], terms_url: '', description: '', optional: false, available: false, reason: '未配置 DEMO_KEY，该来源暂不可用'},
    {id: 'sec_edgar', name: 'SEC EDGAR 公告', kind: 'filing', assets: ['stock'], terms_url: '', description: '官方披露', optional: false, available: true, reason: ''},
  ]};
  const ui = await setup({
    '/api/frameworks': {...FRAMEWORKS, tools: [{id: 'info-crawl', name: '信息抓取', description: '官方来源', icon: '/icons/logo.svg', tags: ['公告'], tool: true}]},
    '/api/info-crawl/sources': sources,
  }, {url: 'http://localhost/#start'});
  try {
    await ui.click('.fw[data-framework="info-crawl"]');
    for (let i = 0; i < 6; i++) await settle();
    const mystery = ui.doc.querySelector('[data-source="mystery"]');
    const keyed = ui.doc.querySelector('[data-source="keyed"]');
    const edgar = ui.doc.querySelector('[data-source="sec_edgar"]');
    assert.match(mystery.textContent, /来源是否可用未知，不把它写成不可用/);
    assert.match(mystery.textContent, /是否必选未知/);
    assert.doesNotMatch(mystery.textContent, /首批必选|暂不可用|DEMO_KEY/);
    assert.equal(mystery.querySelector('input').disabled, false);
    assert.equal(mystery.classList.contains('ic-src--off'), false);
    assert.match(ui.doc.querySelector('#ic-grp-unknown').textContent, /不把它写成首批必选/);
    assert.equal(ui.doc.querySelector('#ic-grp-unknown').nextElementSibling.contains(mystery), true);
    assert.equal(ui.doc.querySelector('#ic-grp-req').nextElementSibling.contains(mystery), false);
    assert.equal(keyed.querySelector('input').disabled, true);
    assert.match(keyed.textContent, /未配置 DEMO_KEY，该来源暂不可用/);
    assert.match(keyed.textContent, /首批必选/);
    assert.match(edgar.textContent, /官方披露/);
    assert.match(edgar.textContent, /首批必选/);
    assert.equal(edgar.querySelector('input').checked, true);
  } finally { ui.dom.window.close(); }
});

test('a finished info crawl does not call a leftover source still fetching', async () => {
  const sources = {inject_default: false, existing: [], sources: [
    {id: 'sec_edgar', name: 'SEC EDGAR 公告', kind: 'filing', assets: ['stock'], terms_url: 'https://www.sec.gov/os/accessing-edgar-data', description: '官方披露', optional: false, available: true, reason: ''},
    {id: 'gdelt', name: 'GDELT 全球新闻索引', kind: 'news', assets: ['stock'], terms_url: 'https://www.gdeltproject.org/about.html', description: '新闻', optional: false, available: true, reason: ''},
  ]};
  const running = {id: 'ic-20261111-run00001', keyword: 'NVDA', asset_type: 'stock', hours: 72, status: 'running',
    sources: {sec_edgar: {status: 'pending', count: 0, reason: ''}, gdelt: {status: 'pending', count: 0, reason: ''}}};
  const interrupted = {id: 'ic-20261111-dead0001', keyword: 'NVDA', asset_type: 'stock', hours: 72, status: 'interrupted',
    reason: '抓取没有完成（服务重启或写盘失败），请重新抓取。',
    sources: {sec_edgar: {status: 'ok', count: 1, reason: ''}, gdelt: {status: 'pending', count: 0, reason: ''}}};
  let releaseGet;
  const ui = await setup({
    '/api/frameworks': {...FRAMEWORKS, tools: [{id: 'info-crawl', name: '信息抓取', description: '官方来源', icon: '/icons/logo.svg', tags: ['公告'], tool: true}]},
    '/api/info-crawl/sources': sources,
    '/api/info-crawl/jobs': (options) => {
      if (options.method === 'POST') return {job: running};
      return new Promise((resolve) => { releaseGet = () => resolve({job: interrupted}); });
    },
    '/api/info-crawl/items': {job: interrupted, items: []},
  }, {url: 'http://localhost/#start'});
  try {
    const {doc, click, fill} = ui;
    await click('.fw[data-framework="info-crawl"]');
    for (let i = 0; i < 6; i++) await settle();
    ui.ui.state.infoPollMs = 0;
    fill('#ic-keyword', 'NVDA');
    doc.querySelector('#ic-form').dispatchEvent(new ui.window.Event('submit', {cancelable: true}));
    await settle();
    assert.match(doc.querySelector('[data-source-status="gdelt"]').textContent, /抓取中/);
    assert.equal(doc.querySelector('#ic-start').textContent, '正在抓取…');
    assert.equal(doc.querySelector('#ic-empty'), null);
    for (let i = 0; i < 20 && typeof releaseGet !== 'function'; i++) {
      await new Promise((r) => setTimeout(r, 15));
      await settle();
    }
    assert.equal(typeof releaseGet, 'function');
    releaseGet();
    for (let i = 0; i < 3; i++) { await new Promise((r) => setTimeout(r, 15)); await settleMany(); }
    const gdelt = doc.querySelector('[data-source-status="gdelt"]').textContent;
    const edgar = doc.querySelector('[data-source-status="sec_edgar"]').textContent;
    assert.match(gdelt, /没有结果，不把它写成仍在抓取/);
    assert.match(edgar, /1 条/);
    assert.doesNotMatch(doc.querySelector('#ic-status').textContent, /抓取中/);
    assert.match(doc.querySelector('#ic-job-note').textContent, /抓取没有完成/);
    assert.doesNotMatch(doc.querySelector('#ic-job-note').textContent, /抓取中|不可用的来源/);
    assert.equal(doc.querySelector('#ic-empty'), null);
    assert.equal(doc.querySelector('#ic-start').textContent, '开始抓取');
    assert.equal(doc.querySelector('#ic-start').disabled, false);
  } finally { ui.window.close(); }
});

test('a failed info crawl without a reason does not call a pending source unavailable', async () => {
  const sources = {inject_default: false, existing: [], sources: [
    {id: 'sec_edgar', name: 'SEC EDGAR 公告', kind: 'filing', assets: ['stock'], terms_url: 'https://www.sec.gov/os/accessing-edgar-data', description: '官方披露', optional: false, available: true, reason: ''},
  ]};
  const failed = {id: 'ic-20261111-fail0001', keyword: 'NVDA', asset_type: 'stock', hours: 72, status: 'failed',
    sources: {sec_edgar: {status: 'pending', count: 0, reason: ''}}};
  const ui = await setup({
    '/api/frameworks': {...FRAMEWORKS, tools: [{id: 'info-crawl', name: '信息抓取', description: '官方来源', icon: '/icons/logo.svg', tags: ['公告'], tool: true}]},
    '/api/info-crawl/sources': sources,
    '/api/info-crawl/jobs': () => ({job: failed}),
    '/api/info-crawl/items': {job: failed, items: []},
  }, {url: 'http://localhost/#start'});
  try {
    const {doc, click, fill} = ui;
    await click('.fw[data-framework="info-crawl"]');
    for (let i = 0; i < 6; i++) await settle();
    ui.ui.state.infoPollMs = 0;
    fill('#ic-keyword', 'NVDA');
    doc.querySelector('#ic-form').dispatchEvent(new ui.window.Event('submit', {cancelable: true}));
    for (let i = 0; i < 3; i++) { await new Promise((r) => setTimeout(r, 15)); await settleMany(); }
    assert.match(doc.querySelector('[data-source-status="sec_edgar"]').textContent, /没有结果，不把它写成仍在抓取/);
    assert.doesNotMatch(doc.querySelector('#ic-status').textContent, /抓取中|不可用/);
    assert.match(doc.querySelector('#ic-empty').textContent, /没有结果，不把它写成仍在抓取/);
    assert.equal(doc.querySelector('#ic-job-note'), null);
    assert.equal(doc.querySelector('#ic-start').textContent, '开始抓取');
  } finally { ui.window.close(); }
});

test('a backtest that cannot start is not called a failed analysis', async () => {
  const old = '控制台没能启动分析。对应命令行：TradingAgents could not start the CLI: spawn failed';
  const ui = await setup({'/api/backtests': (options) => options.method === 'POST'
    ? {__status: 400, body: {error: old}}
    : {sweeps: []}});
  try {
    await ui.click('[data-page=backtest]');
    ui.fill('#bt-tickers', 'NVDA');
    ui.fill('#bt-start', '2026-10-01');
    ui.fill('#bt-end', '2026-10-08');
    await ui.click('#start-backtest');
    await settleMany();
    const out = ui.doc.querySelector('#backtest-result');
    assert.equal(out.className, 'error-banner');
    assert.match(out.textContent, /^控制台没能启动回测。对应命令行：TradingAgents could not start the CLI: spawn failed$/);
    assert.doesNotMatch(out.textContent, /没能启动分析/);
    assert.match(out.innerHTML, /控制台没能启动回测。<br class="notice-break">/);
    assert.equal(ui.ui.state.page, 'backtest');
  } finally { ui.dom.window.close(); }
});

test('a backtest response without a status is not called submitted', async () => {
  const ui = await setup({'/api/backtests': (options) => options.method === 'POST'
    ? {id: 'bt-unknown', zh: '回测已提交'}
    : {sweeps: []}, '/api/runs': {run: null, runs: [], progress: {}, counts: {all: 0, running: 0, queued: 0, failed: 0, finished: 0}}});
  try {
    await ui.click('[data-page=backtest]');
    ui.fill('#bt-tickers', 'NVDA');
    ui.fill('#bt-start', '2026-10-01');
    ui.fill('#bt-end', '2026-10-08');
    await ui.click('#start-backtest');
    await settleMany();
    const note = [...ui.doc.querySelectorAll('[data-kept-notice]')].map((item) => item.textContent).join('\n');
    assert.match(note, /不把它写成已提交或已启动/);
    assert.doesNotMatch(note, /回测已提交|已在后台运行|已加入队列/);
    assert.equal(ui.ui.state.page, 'runs');
  } finally { ui.dom.window.close(); }
});

test('a stopped, cancelled, or stopping backtest is not called an unknown status', async () => {
  const start = async (ui, status) => {
    ui.routes['/api/backtests'] = (options) => options.method === 'POST' ? {id: 'bt-known', status} : {sweeps: []};
    await ui.click('[data-page=backtest]');
    ui.fill('#bt-tickers', 'NVDA');
    ui.fill('#bt-start', '2026-10-01');
    ui.fill('#bt-end', '2026-10-08');
    await ui.click('#start-backtest');
    await settleMany();
    return ui.doc.querySelector('[data-kept-notice]')?.textContent || '';
  };
  const ui = await setup({'/api/backtests': {sweeps: []}, '/api/runs': {run: null, runs: [], progress: {}, counts: {all: 0, running: 0, queued: 0, failed: 0, finished: 0}}});
  try {
    const stopped = await start(ui, 'stopped');
    assert.match(stopped, /回测已停止，不是仍在启动/);
    assert.doesNotMatch(stopped, /状态未知|已在后台运行|回测已启动/);
    const cancelled = await start(ui, 'cancelled');
    assert.match(cancelled, /回测已取消，没有进入运行/);
    assert.doesNotMatch(cancelled, /状态未知/);
    const stopping = await start(ui, 'stopping');
    assert.match(stopping, /回测正在停止，不把它写成仍在启动，也还不是已结束/);
    assert.doesNotMatch(stopping, /状态未知|已在后台运行/);
    const mystery = await start(ui, 'mystery');
    assert.match(mystery, /回测状态未知，不把它写成已启动/);
    assert.doesNotMatch(mystery, /回测已停止|回测已取消|正在停止/);
  } finally { ui.dom.window.close(); }
});

test('a failed backtest is not told it can restart as an analysis', async () => {
  const failed = {
    run: {id: 'bt-fail', status: 'failed', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}, checkpoint: {saved: true}},
    runs: [{id: 'bt-fail', status: 'failed', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08'}],
    progress: {agents: []},
    result: {present: false},
    counts: {all: 1, running: 0, queued: 0, failed: 1, finished: 0},
  };
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': failed});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'bt-fail');
    const step = ui.doc.querySelector('#next-step').textContent;
    assert.match(step, /回测没有生成结果/);
    assert.match(step, /不能按分析重新启动/);
    assert.doesNotMatch(step, /可以重新启动|从失败的角色继续|不再调用模型/);
    assert.equal(ui.doc.querySelector('#restart-run'), null, 'a failed backtest header does not offer 重新启动');
    assert.equal(ui.doc.querySelector('#resume-run'), null, 'a saved backtest checkpoint is not an analysis resume');
    assert.doesNotMatch(ui.doc.querySelector('#run-detail-head').textContent, /重新启动/);
    const analysis = ui.doc.createElement('div');
    analysis.innerHTML = ui.window.uiTest.nextStepPanel({status: 'failed', kind: 'analysis', checkpoint: {saved: true}}, {present: false}, {});
    assert.match(analysis.textContent, /从失败的角色继续/);
    assert.equal(analysis.textContent.replace(/\s+/g, ""), "下一步任务没有生成结果，但已保存断点。点「重新启动」会从失败的角色继续，已完成的角色不再调用模型。");
    assert.match(analysis.querySelector(".notice").innerHTML, /任务没有生成结果，<br class="notice-break">但已保存断点。<br class="notice-break">点「重新启动」会从失败的角色继续，<br class="notice-break">已完成的角色不再调用模型。/);
    const other = ui.doc.createElement('div');
    other.innerHTML = ui.window.uiTest.nextStepPanel({status: 'failed', kind: 'index'}, {present: false}, {});
    assert.match(other.textContent, /不把它写成可以按分析重新启动/);
    assert.doesNotMatch(other.textContent, /可以重新启动|从失败的角色继续/);
  } finally { ui.dom.window.close(); }
});

test('restarting a backtest is not called an analysis restart', async () => {
  const finished = {
    run: {id: 'bt-fin', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}},
    runs: [{id: 'bt-fin', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08'}],
    progress: {agents: []},
    result: {present: false},
    counts: {all: 1, running: 0, queued: 0, failed: 0, finished: 1},
  };
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': finished, '/api/runs/restart': {__status: 400, body: {error: '没有可重新启动的分析参数。'}}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'bt-fin');
    assert.equal(ui.doc.querySelector('#restart-run'), null, 'a finished backtest header does not offer 重新启动');
    assert.doesNotMatch(ui.doc.querySelector('#run-detail-head').textContent, /重新启动/);
    assert.doesNotMatch(ui.doc.querySelector('#next-step').textContent, /可以重新启动|从失败的角色继续/);
    const refused = ui.window.uiTest.restartRefusal({kind: 'backtest'}, '没有可重新启动的分析参数。');
    assert.match(refused, /这是回测，不能按分析重新启动/);
    assert.match(refused, /没有启动/);
    assert.doesNotMatch(refused, /分析参数|不等于分析完成|已重新启动任务/);
    assert.match(ui.window.uiTest.restartRefusalHtml(refused), /这是回测，<br class="notice-break">/);
    const analysis = {
      run: {id: 'an-fail', status: 'failed', kind: 'analysis', selections: {ticker: 'NVDA', date: '2026-10-01'}, checkpoint: {saved: true}},
      runs: [{id: 'an-fail', status: 'failed', kind: 'analysis', ticker: 'NVDA'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.routes['/api/runs'] = analysis;
    ui.ui.state.selectedRunId = 'an-fail';
    await ui.ui.renderRuns();
    assert.ok(ui.doc.querySelector('#resume-run'), 'a failed analysis still offers 从断点重新启动');
    assert.match(ui.doc.querySelector('#resume-run').textContent, /从断点重新启动/);
    ui.routes['/api/runs'] = {
      run: {id: 'an-fin', status: 'finished', kind: 'analysis', selections: {ticker: 'NVDA', date: '2026-10-01'}},
      runs: [{id: 'an-fin', status: 'finished', kind: 'analysis', ticker: 'NVDA'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'an-fin';
    await ui.ui.renderRuns();
    assert.ok(ui.doc.querySelector('#restart-run'), 'a finished analysis still offers 重新启动');
    assert.equal(ui.doc.querySelector('#restart-run').textContent, '重新启动');
  } finally { ui.dom.window.close(); }
});

test('a stopped, cancelled, or completed backtest is not offered an analysis restart', async () => {
  const open = async (status) => {
    const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
      run: {id: 'bt-end', status, kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}},
      runs: [{id: 'bt-end', status, kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08'}],
      progress: {agents: []},
      result: {present: false},
    }});
    await openRunFromList(ui, 'bt-end');
    return ui;
  };
  for (const status of ['stopped', 'cancelled', 'completed']) {
    const ui = await open(status);
    try {
      assert.equal(ui.doc.querySelector('#restart-run'), null, `${status} backtest header does not offer 重新启动`);
      assert.equal(ui.doc.querySelector('#resume-run'), null);
      assert.doesNotMatch(ui.doc.querySelector('#run-detail-head').textContent, /重新启动/);
      assert.doesNotMatch(ui.doc.querySelector('#next-step').textContent, /可以重新启动|从失败的角色继续|按分析重新启动/);
      if (status === 'stopped') {
        const step = ui.doc.querySelector('#next-step');
        assert.match(step.textContent, /任务已停止，不是正常完成。暂时没有可显示的结果。/);
        assert.match(step.querySelector('.notice').innerHTML, /不是正常完成。<br class="notice-break">暂时没有可显示的结果。/);
      }
      if (status === 'completed') {
        const step = ui.doc.querySelector('#next-step');
        assert.match(step.textContent, /任务标记为已完成，但这里没有可显示的结果。/);
        assert.match(step.querySelector('.notice').innerHTML, /任务标记为已完成，<br class="notice-break">但这里没有可显示的结果。/);
        assert.doesNotMatch(step.textContent, /分析已完成|写成分析/);
      }
    } finally { ui.dom.window.close(); }
  }
  const analysis = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'an-stop', status: 'stopped', kind: 'analysis', ticker: 'NVDA', date: '2026-10-01', selections: {ticker: 'NVDA', date: '2026-10-01'}},
    runs: [{id: 'an-stop', status: 'stopped', kind: 'analysis', ticker: 'NVDA'}],
    progress: {agents: []},
    result: {present: false},
  }});
  try {
    await openRunFromList(analysis, 'an-stop');
    assert.equal(analysis.doc.querySelector('#restart-run').textContent, '重新启动');
    assert.equal(analysis.doc.querySelector('#next-step'), null, 'analysis detail does not use the backtest next-step panel');
  } finally { analysis.dom.window.close(); }
});

test('a missing result note breaks at periods on a narrow screen without changing the words', async () => {
  const note = '没有结果文件。进程结束并写出结果后才会出现。启动不等于完成。';
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'bt-empty', status: 'completed', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}},
    runs: [{id: 'bt-empty', status: 'completed', kind: 'backtest', ticker: 'NVDA'}],
    progress: {agents: []},
    result: {present: false, note},
  }});
  try {
    await openRunFromList(ui, 'bt-empty');
    const panel = ui.doc.querySelector('#run-result .notice');
    assert.equal(panel.textContent, note);
    assert.match(panel.innerHTML, /没有结果文件。<br class="notice-break">进程结束并写出结果后才会出现。<br class="notice-break">启动不等于完成。/);
    assert.doesNotMatch(panel.textContent, /分析完成|回测完成/);
    ui.routes['/api/runs'] = {
      run: {id: 'bt-other', status: 'completed', kind: 'backtest', ticker: 'NVDA', selections: {ticker: 'NVDA'}},
      runs: [{id: 'bt-other', status: 'completed', kind: 'backtest', ticker: 'NVDA'}],
      progress: {agents: []},
      result: {present: false, note: '新闻分析师调用超时'},
    };
    ui.ui.state.selectedRunId = 'bt-other';
    await ui.ui.renderRuns();
    const other = ui.doc.querySelector('#run-result .notice');
    assert.equal(other.textContent, '新闻分析师调用超时');
    assert.equal(other.querySelector('br'), null);
  } finally { ui.dom.window.close(); }
});

test('an unknown-status backtest is not offered an analysis restart', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'bt-mystery', status: 'mystery', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}},
    runs: [{id: 'bt-mystery', status: 'mystery', kind: 'backtest', ticker: 'NVDA'}],
    progress: {agents: []},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'bt-mystery');
    assert.equal(ui.doc.querySelector('#restart-run'), null);
    assert.doesNotMatch(ui.doc.querySelector('#run-detail-head').textContent, /重新启动/);
    const step = ui.doc.querySelector('#next-step');
    assert.match(step.textContent, /任务状态未知，不把它写成已结束，也不写成运行中。/);
    assert.match(step.querySelector('.notice').innerHTML, /任务状态未知，<br class="notice-break">不把它写成已结束，<br class="notice-break">也不写成运行中。/);
    assert.doesNotMatch(step.textContent, /可以重新启动|按分析重新启动/);
    const analysis = ui.doc.createElement('div');
    analysis.innerHTML = ui.window.uiTest.nextStepPanel({status: 'mystery', kind: 'analysis'}, {present: false}, {});
    assert.match(analysis.textContent, /任务状态未知，不把它写成已结束，也不写成运行中。/);
    ui.routes['/api/runs'] = {
      run: {id: 'an-mystery', status: 'mystery', kind: 'analysis', ticker: 'NVDA', date: '2026-10-01', selections: {ticker: 'NVDA', date: '2026-10-01'}},
      runs: [{id: 'an-mystery', status: 'mystery', kind: 'analysis', ticker: 'NVDA'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'an-mystery';
    await ui.ui.renderRuns();
    assert.equal(ui.doc.querySelector('#restart-run').textContent, '重新启动');
    assert.match(ui.doc.querySelector('#unknown-run-status').innerHTML, /任务状态未知，<br class="notice-break">不把它写成已结束，<br class="notice-break">也不写成运行中。/);
  } finally { ui.dom.window.close(); }
});

test('a failed analysis next step does not split 日志 or 重新启动', async () => {
  const ui = await setup();
  try {
    const saved = ui.doc.createElement("div");
    saved.innerHTML = ui.window.uiTest.nextStepPanel({status: "failed", kind: "analysis", checkpoint: {saved: true}}, {present: false}, {});
    assert.equal(saved.querySelector(".notice").textContent, "任务没有生成结果，但已保存断点。点「重新启动」会从失败的角色继续，已完成的角色不再调用模型。");
    const plain = ui.doc.createElement("div");
    plain.innerHTML = ui.window.uiTest.nextStepPanel({status: "failed", kind: "analysis"}, {present: false}, {});
    assert.equal(plain.querySelector(".notice").textContent, "任务没有生成结果。展开技术详情查看日志，修正配置后可以重新启动。");
    assert.match(plain.querySelector(".notice").innerHTML, /任务没有生成结果。<br class="notice-break">展开技术详情查看日志，<br class="notice-break">修正配置后可以重新启动。/);
    const empty = ui.doc.createElement("div");
    empty.innerHTML = ui.window.uiTest.nextStepPanel(null, {present: false}, {});
    assert.equal(empty.querySelector(".notice").textContent, "启动分析后，这里会提供刷新、停止和查看结果的操作。");
    assert.match(empty.querySelector(".notice").innerHTML, /启动分析后，<br class="notice-break">这里会提供刷新、停止和<br class="notice-break">查看结果的操作。/);
    const done = ui.doc.createElement("div");
    done.innerHTML = ui.window.uiTest.nextStepPanel({status: "finished", kind: "analysis"}, {present: true}, {});
    assert.equal(done.querySelector(".notice").textContent, "结果已经写出。查看上方结果，或打开报告列表。");
    assert.match(done.querySelector(".notice").innerHTML, /结果已经写出。<br class="notice-break">查看上方结果，或打开报告列表。/);
  } finally { ui.dom.window.close(); }
});

test('review, live files, and backtest intro do not split 评级, 文件, or 排队', async () => {
  const ui = await setup();
  try {
    const review = ui.doc.createElement('div');
    review.innerHTML = ui.window.uiTest.resultPanel({present: true, kind: 'analysis', rating: 'REVIEW', review: 'needs review', decision: '决定原文没有可交易评级。'});
    const note = review.querySelector('#review-note');
    assert.equal(note.textContent, '需要复核：未能从最终决定读出可交易评级。请查看决定内容后自行判断，或重新运行分析。');
    assert.equal(note.innerHTML, '需要复核：<br class="notice-break">未能从最终决定读出可交易评级。<br class="notice-break">请查看决定内容后自行判断，<br class="notice-break">或重新运行分析。');
    const live = ui.doc.createElement('div');
    live.innerHTML = ui.window.uiTest.liveSectionTable([{ticker: 'NVDA', date: '2026-11-12', path: 'a.md', file: 'a.md', title: 'market'}]);
    const fileNote = live.querySelector('#live-file-note');
    assert.equal(fileNote.textContent, '这些文件可能在任务结束后仍留在磁盘上；有文件不等于分析正在进行。');
    assert.equal(fileNote.innerHTML, '这些文件可能在任务结束后<br class="notice-break">仍留在磁盘上；<br class="notice-break">有文件不等于分析正在进行。');
    await ui.click('[data-page=backtest]');
    const intro = ui.doc.querySelector('#backtest-result');
    assert.equal(intro.textContent, '先预估分析次数，再决定是否启动。回测不排队：运行名额已满或有任务排队时不会启动。');
    assert.equal(intro.innerHTML, '先预估分析次数，<br class="notice-break">再决定是否启动。<br class="notice-break">回测不排队：<br class="notice-break">运行名额已满或有任务排队时不会启动。');
  } finally { ui.dom.window.close(); }
});

test('provider, model, and login notes do not split 密钥, 沿用, or 提示', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=models]');
    const model = ui.doc.querySelector('#model-advanced-hint');
    assert.equal(model.textContent, '保存在本机控制台配置里，下一次分析生效。留空沿用默认。');
    assert.equal(model.innerHTML, '保存在本机控制台配置里，<br class="notice-break">下一次分析生效。<br class="notice-break">留空沿用默认。');
    await ui.click('[data-page=accounts]');
    const login = ui.doc.querySelector('#login-help-note');
    assert.equal(login.textContent, '如果登录窗口没有自动完成，可在这里查看提示并提交地址。');
    assert.equal(login.innerHTML, '如果登录窗口没有自动完成，<br class="notice-break">可在这里查看提示并提交地址。');
    await ui.click('[data-page=analyze]');
    await ui.click('.fw[data-framework=tradingagents]');
    const provider = ui.doc.querySelector('#provider-login-hint');
    assert.equal(provider.textContent, '订阅服务需先在「账户」中登录；API 服务需先配置密钥。');
    assert.equal(provider.innerHTML, '订阅服务需先在「账户」中登录；<br class="notice-break">API 服务需先配置密钥。');
  } finally { ui.dom.window.close(); }
});

test('an idle backtest is not offered an analysis restart', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'bt-idle', status: 'idle', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}},
    runs: [{id: 'bt-idle', status: 'idle', kind: 'backtest', ticker: 'NVDA'}],
    progress: {agents: []},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'bt-idle');
    assert.equal(ui.doc.querySelector('#restart-run'), null);
    assert.doesNotMatch(ui.doc.querySelector('#run-detail-head').textContent, /重新启动/);
    const step = ui.doc.querySelector('#next-step');
    assert.match(step.textContent, /任务状态是空闲，暂时没有可显示的结果。不把它写成已结束。/);
    assert.doesNotMatch(step.textContent, /可以重新启动|按分析重新启动|写成分析/);
    ui.routes['/api/runs'] = {
      run: {id: 'an-idle', status: 'idle', kind: 'analysis', ticker: 'NVDA', date: '2026-10-01', selections: {ticker: 'NVDA', date: '2026-10-01'}},
      runs: [{id: 'an-idle', status: 'idle', kind: 'analysis', ticker: 'NVDA'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'an-idle';
    await ui.ui.renderRuns();
    assert.equal(ui.doc.querySelector('#restart-run').textContent, '重新启动');
  } finally { ui.dom.window.close(); }
});

test('an unrecorded team card does not split 角色', async () => {
  const pending = {team: 'Analyst Team', team_zh: '分析师团队', agent: 'Market Analyst', zh: '市场分析师', status: 'pending', status_zh: '等待'};
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'an-blank', status: 'failed', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}},
    runs: [{id: 'an-blank', status: 'failed', kind: 'analysis', ticker: 'NVDA'}],
    progress: {agents: [pending]},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'an-blank');
    const note = ui.doc.querySelector('.team .now');
    assert.equal(note.textContent, '未记录：日志里没有这组角色，不把它们写成等待。');
    assert.match(note.innerHTML, /未记录：<\/b><br>日志里没有这组角色，<br>不把它们写成等待。/);
    assert.doesNotMatch(note.textContent, /仍在等待|写成等待中/);
  } finally { ui.dom.window.close(); }
});

test('an idle or unknown backtest without role records is not given a completion count', async () => {
  const pending = {team: 'Analyst Team', team_zh: '分析师团队', agent: 'Market Analyst', zh: '市场分析师', status: 'pending', status_zh: '等待'};
  const open = async (status) => {
    const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
      run: {id: 'bt-blank', status, kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}},
      runs: [{id: 'bt-blank', status, kind: 'backtest', ticker: 'NVDA'}],
      progress: {agents: [pending, {...pending, agent: 'News Analyst', zh: '新闻分析师'}]},
      result: {present: false},
    }});
    await openRunFromList(ui, 'bt-blank');
    return ui;
  };
  for (const status of ['idle', 'mystery']) {
    const ui = await open(status);
    try {
      assert.match(ui.doc.querySelector('#task-progress').textContent, /没有角色进度，不写成已完成/);
      assert.doesNotMatch(ui.doc.querySelector('#task-progress').textContent, /个角色完成|0\/2/);
      assert.equal(ui.doc.querySelector('.progress-table .status-badge').textContent, '未记录');
      assert.doesNotMatch(ui.doc.querySelector('.progress-table').textContent, /等待/);
      assert.match(ui.doc.querySelector('#unrecorded-roles').textContent, /不把它们写成等待/);
    } finally { ui.dom.window.close(); }
  }
  const live = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'NVDA', selections: {ticker: 'NVDA'}},
    runs: [{id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'NVDA'}],
    progress: {agents: [pending]},
    result: {present: false},
  }});
  try {
    await openRunFromList(live, 'bt-live');
    assert.match(live.doc.querySelector('#task-progress').textContent, /0\/1 个角色完成/);
    assert.equal(live.doc.querySelector('.progress-table .status-badge').textContent, '等待');
  } finally { live.dom.window.close(); }
});

test('an idle backtest list does not show a completion count or call it ended', async () => {
  const idle = {
    id: 'bt-idle', status: 'idle', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08',
    done: 0, total: 12, team_zh: '准备中', blank: true, started_at: '2026-11-12T09:15:00',
  };
  const running = {
    id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'AAPL', date: '2026-11-12',
    done: 0, total: 12, team_zh: '准备中', started_at: '2026-11-12T09:10:00',
  };
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: idle, runs: [running, idle], progress: {agents: []}, result: {present: false},
    counts: {all: 2, running: 1, queued: 0, failed: 0, finished: 0},
  }});
  try {
    await ui.click('[data-page=runs]');
    const row = [...ui.doc.querySelectorAll('#run-list [data-run-id]')].find((item) => item.dataset.runId === 'bt-idle');
    assert.match(row.querySelector('.pg').textContent, /没有记录/);
    assert.doesNotMatch(row.querySelector('.pg').textContent, /0\/12/);
    assert.equal(ui.doc.querySelector('#unrecorded-list'), null);
    const live = [...ui.doc.querySelectorAll('#run-list [data-run-id]')].find((item) => item.dataset.runId === 'bt-live');
    assert.match(live.querySelector('.pg').textContent, /0\/12/);
    assert.doesNotMatch(live.querySelector('.pg').textContent, /没有记录/);
    ui.window.matchMedia = (query) => ({matches: String(query).includes('600'), addEventListener() {}, removeEventListener() {}});
    await ui.ui.renderRuns();
    const card = ui.doc.querySelector('#run-list .b');
    assert.match(card.innerHTML, /准备中 · <br class="notice-break">开始/);
    assert.doesNotMatch(card.innerHTML, /&nbsp;·/);
  } finally { ui.dom.window.close(); }
});

test('a running backtest next step does not split 停止任务', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}},
    runs: [{id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'NVDA'}],
    progress: {agents: []},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'bt-live');
    const step = ui.doc.querySelector('#next-step');
    assert.match(step.textContent, /任务仍在运行。稍后刷新状态，或在确认后停止任务。/);
    assert.match(step.querySelector('.notice').innerHTML, /任务仍在运行。<br class="notice-break">稍后刷新状态，<br class="notice-break">或在确认后停止任务。/);
    assert.doesNotMatch(step.textContent, /写成分析|已结束|准备中/);
    assert.equal(ui.doc.querySelector('#restart-run'), null);
    assert.ok(ui.doc.querySelector('#stop-run'));
  } finally { ui.dom.window.close(); }
});

test('a starting backtest next step does not split 刷新状态', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'bt-start', status: 'starting', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}},
    runs: [{id: 'bt-start', status: 'starting', kind: 'backtest', ticker: 'NVDA'}],
    progress: {agents: []},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'bt-start');
    const step = ui.doc.querySelector('#next-step');
    assert.match(step.textContent, /任务正在启动，还没有进入运行。稍后刷新状态，或在确认后停止任务。/);
    assert.match(step.querySelector('.notice').innerHTML, /任务正在启动，<br class="notice-break">还没有进入运行。<br class="notice-break">稍后刷新状态，<br class="notice-break">或在确认后停止任务。/);
    assert.doesNotMatch(step.textContent, /写成分析|已结束/);
    assert.equal(ui.doc.querySelector('#restart-run'), null);
    assert.ok(ui.doc.querySelector('#stop-run'));
  } finally { ui.dom.window.close(); }
});

test('the backtest model summary breaks before the change hint on a narrow screen', async () => {
  const ui = await setup({'/api/backtests': {sweeps: []}});
  try {
    await ui.click('[data-page=backtest]');
    const note = ui.doc.querySelector('#backtest-model-summary');
    assert.match(note.textContent, /当前模型：.+。可展开上方设置更改。/);
    assert.match(note.innerHTML, /。<br class="notice-break">可展开上方设置更改。/);
    assert.doesNotMatch(note.textContent, /分析完成|回测完成/);
  } finally { ui.dom.window.close(); }
});

test('the backtest page description does not split 成交 on a narrow screen', async () => {
  const ui = await setup({'/api/backtests': {sweeps: []}});
  try {
    await ui.click('[data-page=backtest]');
    const desc = ui.doc.querySelector('.page-description');
    assert.match(desc.textContent, /在历史日期上重复分析，评估过往判断。结果不包含成交、滑点和手续费。/);
    assert.match(desc.innerHTML, /在历史日期上重复分析，<br class="notice-break">评估过往判断。<br class="notice-break">结果不包含成交、<br class="notice-break">滑点和手续费。/);
    assert.doesNotMatch(desc.textContent, /分析完成|回测完成/);
  } finally { ui.dom.window.close(); }
});

test('an immediate backtest exit is not called an analysis completion', async () => {
  const ui = await setup({'/api/backtests': (options) => options.method === 'POST'
    ? {id: 'bt-fail', status: 'failed', kind: 'backtest', zh: '后台启动失败。进程启动不等于分析完成。'}
    : {sweeps: []}, '/api/runs': {run: null, runs: [], progress: {}, counts: {all: 0, running: 0, queued: 0, failed: 0, finished: 0}}});
  try {
    await ui.click('[data-page=backtest]');
    ui.fill('#bt-tickers', 'NVDA');
    ui.fill('#bt-start', '2026-10-01');
    ui.fill('#bt-end', '2026-10-08');
    await ui.click('#start-backtest');
    await settleMany();
    const note = [...ui.doc.querySelectorAll('[data-kept-notice]')].map((item) => item.textContent).join('\n');
    assert.match(note, /后台启动失败/);
    assert.match(note, /进程启动不等于回测完成/);
    assert.doesNotMatch(note, /不等于分析完成|写成分析完成/);
  } finally { ui.dom.window.close(); }
});

test('a started backtest is not called an analysis completion', async () => {
  const ui = await setup({'/api/backtests': (options) => options.method === 'POST'
    ? {id: 'bt-live', status: 'running', kind: 'backtest', zh: '已在后台启动。进程启动不等于分析完成。'}
    : {sweeps: []}, '/api/runs': {run: null, runs: [], progress: {}, counts: {all: 0, running: 0, queued: 0, failed: 0, finished: 0}}});
  try {
    await ui.click('[data-page=backtest]');
    ui.fill('#bt-tickers', 'NVDA');
    ui.fill('#bt-start', '2026-10-01');
    ui.fill('#bt-end', '2026-10-08');
    await ui.click('#start-backtest');
    await settleMany();
    const note = [...ui.doc.querySelectorAll('[data-kept-notice]')].map((item) => item.textContent).join('\n');
    assert.match(note, /回测已在后台运行/);
    assert.match(note, /进程启动不等于回测完成/);
    assert.doesNotMatch(note, /不等于分析完成|写成分析完成/);
  } finally { ui.dom.window.close(); }
});

test('a finished backtest response is not called still starting', async () => {
  const ui = await setup({'/api/backtests': (options) => options.method === 'POST'
    ? {id: 'bt-done', status: 'finished', ticker: 'NVDA'}
    : {sweeps: []}, '/api/runs': {run: null, runs: [], progress: {}}});
  try {
    await ui.click('[data-page=backtest]');
    ui.fill('#bt-tickers', 'NVDA');
    ui.fill('#bt-start', '2026-10-01');
    ui.fill('#bt-end', '2026-10-08');
    await ui.click('#start-backtest');
    await settleMany();
    const note = [...ui.doc.querySelectorAll('[data-kept-notice]')].map((item) => item.textContent).join('\n');
    assert.match(note, /回测已经结束，不把它写成仍在启动/);
    assert.doesNotMatch(note, /回测已启动|可在这里跟踪进度/);
    ui.routes['/api/backtests'] = (options) => options.method === 'POST'
      ? {id: 'bt-live', status: 'running'}
      : {sweeps: []};
    await ui.click('[data-page=backtest]');
    ui.fill('#bt-tickers', 'NVDA');
    ui.fill('#bt-start', '2026-10-01');
    ui.fill('#bt-end', '2026-10-08');
    await ui.click('#start-backtest');
    await settleMany();
    const running = [...ui.doc.querySelectorAll('[data-kept-notice]')].map((item) => item.textContent).join('\n');
    assert.match(running, /回测已在后台运行/);
    assert.match(running, /进程启动不等于回测完成/);
    assert.doesNotMatch(running, /回测已启动，可在这里跟踪进度/);
  } finally { ui.dom.window.close(); }
});

test('an unrecognized run kind is not called analysis', async () => {
  const unknown = {
    run: {id: 'kind-unknown', status: 'running', kind: 'index', ticker: 'AAA', date: '2026-11-12', selections: {ticker: 'AAA', date: '2026-11-12'}},
    runs: [{id: 'kind-unknown', status: 'running', kind: 'index', ticker: 'AAA', date: '2026-11-12', framework_name: 'TradingAgents'}],
    progress: {agents: [], complete: 'Analysis Complete!'},
    result: {present: true, kind: 'index', text: 'not an analysis'},
    counts: {all: 1, running: 1, queued: 0, failed: 0, finished: 0},
  };
  const ui = await setup({'/api/runs': unknown});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'kind-unknown');
    const kind = ui.doc.querySelector('#run-kind').textContent;
    assert.match(kind, /^任务类型未知，不把它写成分析/);
    assert.doesNotMatch(kind, /^分析$/);
    assert.match(ui.doc.querySelector('#completion-note').textContent, /任务类型未知，不把它写成分析已完成/);
    assert.doesNotMatch(ui.doc.querySelector('#completion-note').textContent, /^分析已完成/);
    assert.match(ui.doc.querySelector('#unknown-result-kind').textContent, /不把它写成分析结果/);
    ui.routes['/api/runs'] = {run: {id: 'known-analysis', status: 'running', kind: 'analysis', ticker: 'AAA', date: '2026-11-12', selections: {ticker: 'AAA'}}, runs: [{id: 'known-analysis', status: 'running', ticker: 'AAA', date: '2026-11-12'}], progress: {agents: []}, result: {present: false}};
    ui.ui.state.selectedRunId = 'known-analysis';
    await ui.ui.renderRuns();
    assert.equal(ui.doc.querySelector('#run-kind').textContent, '分析');
    ui.routes['/api/runs'] = {run: {id: 'known-backtest', status: 'running', kind: 'backtest', ticker: 'AAA', date: '2026-11-12', selections: {ticker: 'AAA'}}, runs: [{id: 'known-backtest', status: 'running', ticker: 'AAA', date: '2026-11-12'}], progress: {agents: []}, result: {present: false}};
    ui.ui.state.selectedRunId = 'known-backtest';
    await ui.ui.renderRuns();
    assert.equal(ui.doc.querySelector('#run-kind').textContent, '回测');
  } finally { ui.dom.window.close(); }
});

test('an unverified backtest stop is not called an analysis process', async () => {
  const old = '读不到这个任务进程的启动时间，无法确认它还是本任务的分析进程。为安全起见没有发送任何信号；任务仍算运行中，继续占用名额。\n下一步：稍后点「刷新」再试。';
  const running = {id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}, started_at: '2026-11-12T09:00:00'};
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {run: running, runs: [running], progress: {agents: []}, result: {present: false}, counts: {all: 1, running: 1, queued: 0, failed: 0, finished: 0}}, '/api/runs/stop': {__status: 409, body: {error: old}}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'bt-live');
    await ui.click('#stop-run');
    assert.match(ui.doc.body.textContent, /停止当前回测/);
    await ui.click('#confirm-yes');
    await settleMany();
    const banner = ui.doc.querySelector('#stop-refused');
    assert.ok(banner);
    assert.match(banner.textContent, /无法确认这是回测进程/);
    assert.match(banner.textContent, /没有发送任何信号/);
    assert.match(banner.textContent, /下一步：稍后点「刷新」再试/);
    assert.doesNotMatch(banner.textContent, /分析进程/);
    assert.match(banner.innerHTML, /<br>下一步：/);

    const otherRun = {id: 'idx-live', status: 'running', kind: 'index', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}};
    ui.routes['/api/runs'] = {run: otherRun, runs: [otherRun], progress: {agents: []}, result: {present: false}, counts: {all: 1, running: 1, queued: 0, failed: 0, finished: 0}};
    ui.ui.state.stopRefused = {id: 'idx-live', text: old};
    ui.ui.state.selectedRunId = 'idx-live';
    await ui.ui.renderRuns();
    const other = ui.doc.querySelector('#stop-refused').textContent;
    assert.match(other, /不把它写成分析进程/);
    assert.doesNotMatch(other, /这是回测进程|本任务的分析进程/);
  } finally { ui.dom.window.close(); }
});

test('an unverified analysis stop does not split 分析 or 信号', async () => {
  const sentence = '读不到这个任务进程的启动时间，无法确认它还是本任务的分析进程。为安全起见没有发送任何信号；任务仍算运行中，继续占用名额。\n下一步：稍后点「刷新」再试。';
  const running = {id: 'an-live', status: 'running', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}, started_at: '2026-11-12T09:00:00'};
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {run: running, runs: [running], progress: {agents: []}, result: {present: false}, counts: {all: 1, running: 1, queued: 0, failed: 0, finished: 0}}, '/api/runs/stop': {__status: 409, body: {error: sentence}}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'an-live');
    await ui.click('#stop-run');
    await ui.click('#confirm-yes');
    await settleMany();
    const banner = ui.doc.querySelector('#stop-refused');
    assert.ok(banner);
    assert.equal(banner.textContent, sentence.replace('\n', ''));
    assert.match(banner.innerHTML, /分析进程。<br class="notice-break">为安全起见没有发送任何信号；<br class="notice-break">/);
    assert.match(banner.innerHTML, /<br>下一步：稍后点「刷新」再试。/);
    assert.equal(banner.querySelector('img'), null);
  } finally { ui.dom.window.close(); }
});

test('a reused pid on a backtest is not called an analysis process', async () => {
  const old = '这个任务记录的进程号现在属于另一个程序（启动时间对不上），原来的分析进程已经不在，没有写出结果。控制台没有向那个程序发送任何信号。';
  const failed = {
    run: {id: 'bt-reused', status: 'failed', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}, detail: old, pid_reused: true},
    runs: [{id: 'bt-reused', status: 'failed', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08'}],
    progress: {agents: []},
    result: {present: false},
    counts: {all: 1, running: 0, queued: 0, failed: 1, finished: 0},
  };
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': failed});
  try {
    await openRunFromList(ui, 'bt-reused');
    const line = ui.doc.querySelector('#run-status-line').textContent;
    assert.match(line, /原来的回测进程已经不在/);
    assert.match(line, /没有向那个程序发送信号/);
    assert.doesNotMatch(line, /原来的分析进程|分析进程/);
    const note = ui.doc.querySelector('#run-detail-note').textContent;
    assert.match(note, /原来的回测进程已经不在/);
    assert.doesNotMatch(note, /原来的分析进程/);

    ui.routes['/api/runs'] = {
      run: {id: 'an-reused', status: 'failed', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}, detail: old, pid_reused: true},
      runs: [{id: 'an-reused', status: 'failed', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'an-reused';
    await ui.ui.renderRuns();
    const analysisNote = ui.doc.querySelector('#run-detail-note');
    assert.equal(analysisNote.textContent, old);
    assert.match(analysisNote.innerHTML, /程序<br class="notice-break">（启动时间对不上），<br class="notice-break">原来的分析进程已经不在，<br class="notice-break">没有写出结果。<br class="notice-break">控制台没有向那个程序发送任何信号。/);
    const reason = ui.doc.querySelector('#run-failure code');
    assert.equal(reason.textContent, old);
    assert.match(reason.innerHTML, /程序<br>（启动时间对不上），<br>原来的分析进程已经不在，/);
    assert.match(ui.doc.querySelector('#run-failure .why').innerHTML, /原因：<br><code>/);
    assert.doesNotMatch(reason.innerHTML, /notice-break/);
    assert.equal(ui.doc.querySelector('#run-failure img, #run-failure script'), null);

    ui.routes['/api/runs'] = {
      run: {id: 'idx-reused', status: 'failed', kind: 'index', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}, detail: old, pid_reused: true},
      runs: [{id: 'idx-reused', status: 'failed', kind: 'index', ticker: 'NVDA', date: '2026-11-12'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'idx-reused';
    await ui.ui.renderRuns();
    const other = ui.doc.querySelector('#run-detail-note').textContent;
    assert.match(other, /不把它写成分析进程/);
    assert.doesNotMatch(other, /原来的分析进程|原来的回测进程/);
  } finally { ui.dom.window.close(); }
});

test('a finished backtest with a result is not called an analysis exit', async () => {
  const old = '分析进程已退出，结果文件已写出。这只说明进程结束，不表示分析完成或评级已确认。';
  const finished = {
    run: {id: 'bt-wrote', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}, detail: old},
    runs: [{id: 'bt-wrote', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08'}],
    progress: {agents: []},
    result: {present: true, kind: 'backtest', text: '回测摘要'},
    counts: {all: 1, running: 0, queued: 0, failed: 0, finished: 1},
  };
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': finished});
  try {
    await openRunFromList(ui, 'bt-wrote');
    const note = ui.doc.querySelector('#run-detail-note').textContent;
    assert.match(note, /回测进程已退出，结果文件已写出。这只说明进程结束，不表示回测完成/);
    assert.doesNotMatch(note, /分析进程|不表示分析完成|评级已确认/);
    assert.doesNotMatch(ui.doc.querySelector('#run-log').textContent, /分析进程|不表示分析完成/);

    ui.routes['/api/runs'] = {
      run: {id: 'an-wrote', status: 'finished', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}, detail: old},
      runs: [{id: 'an-wrote', status: 'finished', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'an-wrote';
    await ui.ui.renderRuns();
    assert.match(ui.doc.querySelector('#run-detail-note').textContent, /分析进程已退出，结果文件已写出/);
    assert.match(ui.doc.querySelector('#run-detail-note').textContent, /不表示分析完成或评级已确认/);

    ui.routes['/api/runs'] = {
      run: {id: 'idx-wrote', status: 'finished', kind: 'index', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}, detail: old},
      runs: [{id: 'idx-wrote', status: 'finished', kind: 'index', ticker: 'NVDA', date: '2026-11-12'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'idx-wrote';
    await ui.ui.renderRuns();
    const other = ui.doc.querySelector('#run-detail-note').textContent;
    assert.match(other, /不把它写成分析完成/);
    assert.doesNotMatch(other, /分析进程|不表示分析完成|不表示回测完成/);
  } finally { ui.dom.window.close(); }
});

test('a failed backtest without a result is not called an exited analysis', async () => {
  const old = '分析进程已退出，但没有写出结果。进程退出不等于分析完成。';
  const failed = {
    run: {id: 'bt-dead', status: 'failed', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}, detail: old},
    runs: [{id: 'bt-dead', status: 'failed', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08'}],
    progress: {agents: []},
    result: {present: false},
    counts: {all: 1, running: 0, queued: 0, failed: 1, finished: 0},
  };
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': failed});
  try {
    await openRunFromList(ui, 'bt-dead');
    const line = ui.doc.querySelector('#run-status-line').textContent;
    assert.match(line, /回测进程已退出，但没有写出结果。进程退出不等于回测完成/);
    assert.doesNotMatch(line, /分析进程|不等于分析完成/);
    const note = ui.doc.querySelector('#run-detail-note').textContent;
    assert.match(note, /不等于回测完成/);
    assert.doesNotMatch(note, /分析进程|不等于分析完成/);
    assert.doesNotMatch(ui.doc.querySelector('#run-log').textContent, /分析进程|不等于分析完成/);

    ui.routes['/api/runs'] = {
      run: {id: 'an-dead', status: 'failed', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}, detail: old},
      runs: [{id: 'an-dead', status: 'failed', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'an-dead';
    await ui.ui.renderRuns();
    assert.match(ui.doc.querySelector('#run-detail-note').textContent, /分析进程已退出，但没有写出结果。进程退出不等于分析完成/);

    ui.routes['/api/runs'] = {
      run: {id: 'idx-dead', status: 'failed', kind: 'index', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}, detail: old},
      runs: [{id: 'idx-dead', status: 'failed', kind: 'index', ticker: 'NVDA', date: '2026-11-12'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'idx-dead';
    await ui.ui.renderRuns();
    const other = ui.doc.querySelector('#run-detail-note').textContent;
    assert.match(other, /不把它写成分析完成/);
    assert.doesNotMatch(other, /分析进程|不等于分析完成|不等于回测完成/);

    ui.routes['/api/runs'] = {
      run: {id: 'bt-why', status: 'failed', kind: 'backtest', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}, detail: '新闻分析师调用超时', failure: {summary: '新闻分析师调用超时'}},
      runs: [{id: 'bt-why', status: 'failed', kind: 'backtest', ticker: 'NVDA', date: '2026-11-12'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'bt-why';
    await ui.ui.renderRuns();
    assert.match(ui.doc.querySelector('#run-status-line').textContent, /新闻分析师调用超时/);
    assert.doesNotMatch(ui.doc.querySelector('#run-status-line').textContent, /不等于回测完成/);
  } finally { ui.dom.window.close(); }
});

test('a finished backtest without a result is not called an unfinished analysis', async () => {
  const finished = {
    run: {id: 'bt-fin', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA', date: '2026-11-12'}},
    runs: [{id: 'bt-fin', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-11-12'}],
    progress: {agents: []},
    result: {present: false},
    counts: {all: 1, running: 0, queued: 0, failed: 0, finished: 1},
  };
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': finished});
  try {
    await openRunFromList(ui, 'bt-fin');
    const step = ui.doc.querySelector('#next-step').textContent;
    assert.match(step, /不表示回测完成/);
    assert.doesNotMatch(step, /不表示分析完成|不把它写成分析完成/);
    const analysis = ui.doc.createElement('div');
    analysis.innerHTML = ui.window.uiTest.nextStepPanel({status: 'finished', kind: 'analysis'}, {present: false}, {});
    assert.match(analysis.textContent, /不表示分析完成/);
    assert.doesNotMatch(analysis.textContent, /不表示回测完成|不把它写成分析完成/);
    const other = ui.doc.createElement('div');
    other.innerHTML = ui.window.uiTest.nextStepPanel({status: 'finished', kind: 'index'}, {present: false}, {});
    assert.match(other.textContent, /不把它写成分析完成/);
    assert.doesNotMatch(other.textContent, /不表示回测完成|不表示分析完成/);

    ui.routes['/api/runs'] = {
      run: {id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}},
      runs: [{id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'NVDA', date: '2026-11-12'}],
      progress: {agents: [], report: '回测日志'},
      result: {present: false},
      counts: {all: 1, running: 1, queued: 0, failed: 0, finished: 0},
    };
    ui.ui.state.selectedRunId = 'bt-live';
    await ui.ui.renderRuns();
    const summary = ui.doc.querySelector('#current-report summary').textContent;
    assert.match(summary, /查看当前回测内容/);
    assert.doesNotMatch(summary, /查看当前分析内容/);
    assert.match(ui.doc.querySelector('#current-report').textContent, /回测过程中/);
    assert.doesNotMatch(ui.doc.querySelector('#current-report').textContent, /分析过程中/);

    ui.routes['/api/runs'] = {
      run: {id: 'an-live', status: 'running', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}},
      runs: [{id: 'an-live', status: 'running', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12'}],
      progress: {agents: [], report: '分析日志'},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'an-live';
    await ui.ui.renderRuns();
    assert.match(ui.doc.querySelector('#current-report summary').textContent, /查看当前分析内容/);
    assert.doesNotMatch(ui.doc.querySelector('#current-report summary').textContent, /查看当前回测内容/);
  } finally { ui.dom.window.close(); }
});

test('a backtest failure and an unknown live report do not split 信号 or 写成', async () => {
  const detail = '这个任务记录的进程号现在属于另一个程序（启动时间对不上），原来的分析进程已经不在，没有写出结果。控制台没有向那个程序发送任何信号。';
  const failed = {id: 'bt-fail', status: 'failed', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}, failure: {summary: '超时'}, detail};
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {run: failed, runs: [failed], progress: {agents: []}, result: {present: false}, counts: {all: 1, running: 0, queued: 0, failed: 1, finished: 0}}});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'bt-fail');
    const banner = ui.doc.querySelector('#run-failure');
    assert.ok(banner);
    assert.match(banner.textContent, /原来的回测进程已经不在/);
    assert.match(banner.textContent, /没有向那个程序发送信号/);
    assert.doesNotMatch(banner.textContent, /原来的分析进程/);
    assert.match(banner.innerHTML, /启动时间对不上。<br class="notice-break">原来的回测进程已经不在，<br class="notice-break">没有写出结果。<br class="notice-break">没有向那个程序发送信号。/);
    assert.equal(banner.querySelector('img, script'), null);

    const live = {id: 'idx-live', status: 'running', kind: 'index', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}};
    ui.routes['/api/runs'] = {run: live, runs: [live], progress: {agents: []}, result: {present: false}, counts: {all: 1, running: 1, queued: 0, failed: 0, finished: 0}};
    ui.ui.state.selectedRunId = 'idx-live';
    await ui.ui.renderRuns();
    const waiting = ui.doc.querySelector('#current-report-waiting');
    assert.equal(waiting.textContent, '任务过程中，最新内容会显示在这里。不把它写成分析。');
    assert.equal(waiting.innerHTML, '任务过程中，<br class="notice-break">最新内容会显示在这里。<br class="notice-break">不把它写成分析。');
  } finally { ui.dom.window.close(); }
});

test('paste, crawl, and analyst notes do not split 地址, 用量, or 一名', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=accounts]');
    const paste = ui.doc.querySelector('#paste-label');
    assert.match(paste.textContent, /当前没有等待粘贴的 ChatGPT 登录/);
    assert.match(paste.innerHTML, /登录。<br class="notice-break">/);
    assert.doesNotMatch(paste.innerHTML, /<script|onerror/);
    const crawl = ui.doc.createElement('div');
    crawl.innerHTML = ui.window.uiTest.infoItemsHtml({job: {status: 'done'}, items: [{id: '1', title: '条目', published_at: '2026-11-12T00:00:00Z', source_name: '来源', kind: 'news'}], picked: []});
    const hint = crawl.querySelector('#ic-pick-hint');
    assert.equal(hint.textContent, '默认不带入；只有勾选的条目会作为上下文加入分析，会增加模型用量。');
    assert.equal(hint.innerHTML, '默认不带入；<br class="notice-break">只有勾选的条目会作为上下文加入分析，<br class="notice-break">会增加模型用量。');
    const analyst = ui.doc.createElement('div');
    analyst.innerHTML = ui.window.uiTest.analystRequiredHtml('请至少选择一名分析师。');
    assert.equal(analyst.textContent, '至少选择一名分析师。对应命令行：请至少选择一名分析师。页面没有取消这一问，不会退出。');
    assert.match(analyst.innerHTML, /至少选择一名分析师。<br class="notice-break">对应命令行：请至少选择一名分析师。<br class="notice-break">页面没有取消这一问，不会退出。/);
  } finally { ui.dom.window.close(); }
});

test('custom model, empty backtest dates, and attached crawl do not split 手填, 日期, or 引用', async () => {
  const ui = await setup({'/api/backtests/validate': {cells: 0, analysts: ['market']}});
  try {
    const row = ui.doc.createElement('div');
    row.innerHTML = ui.window.uiTest.customModelRow('ollama');
    const label = row.querySelector('label');
    assert.match(label.textContent, /列表失败或选择自定义时手填/);
    assert.match(label.innerHTML, /<br class="notice-break">列表失败或选择自定义时手填。/);
    ui.ui.state.infoAttach = {job: {status: 'done'}, keyword: '贵州茅台', items: [{id: '1'}]};
    // AUDIT-CHECK-02 merge: with news selected the own-line no-news hint stays empty and hidden.
    ui.ui.state.form.analysts = ['market', 'news'];
    const attach = ui.doc.createElement('div');
    attach.innerHTML = ui.window.uiTest.infoAttachHtml();
    assert.equal(attach.querySelector('#info-attach').textContent.replace(/\s+/g, ''), '已带入信息抓取「贵州茅台」中勾选的1条资讯，报告会附来源引用。不带入');
    assert.match(attach.innerHTML, /<br class="notice-break">报告会附来源引用。/);
    ui.ui.state.backtest = {tickers: 'NVDA', start: '2026-10-01', end: '2026-10-08', every: 7, asset_type: 'stock', analysts: ['market'], run_id: '', portfolio: ''};
    await ui.click('[data-page=backtest]');
    ui.fill('#bt-tickers', 'NVDA');
    ui.fill('#bt-start', '2026-10-01');
    ui.fill('#bt-end', '2026-10-08');
    await ui.click('#check-backtest');
    const estimate = ui.doc.querySelector('#backtest-result');
    assert.equal(estimate.textContent, '共 0 次分析。所选范围没有可分析的日期，请调整日期。');
    assert.match(estimate.innerHTML, /共 0 次分析。<br class="notice-break">所选范围没有可分析的日期，<br class="notice-break">请调整日期。/);
  } finally { ui.dom.window.close(); }
});

test('asset, holding, import, and restart notes do not split 成本, 使用, or 模型', async () => {
  const ui = await setup();
  try {
    ui.ui.state.form.asset_type = 'commodity';
    await ui.click('[data-page=analyze]');
    await ui.click('.fw[data-framework=tradingagents]');
    const asset = ui.doc.querySelector('#asset-hint');
    assert.equal(asset.textContent, '按代码识别为贵金属/商品期货（如 XAUUSD→GC=F），不做公司基本面分析。');
    assert.match(asset.innerHTML, /<br class="notice-break">（如 XAUUSD→GC=F），<br class="notice-break">不做公司基本面分析。/);
    const holding = ui.doc.querySelector('#holding-hint');
    assert.equal(holding.textContent, '用于组合经理给出加仓或减仓建议。持仓价格按买入成本填写。');
    assert.match(holding.innerHTML, /减仓建议。<br class="notice-break">持仓价格按买入成本填写。/);
    const note = ui.doc.createElement('div');
    note.innerHTML = ui.window.uiTest.formNoteHtml('未选新闻分析师，本地导入的资料不会被使用。请勾选新闻分析师，或清除导入。');
    assert.match(note.innerHTML, /新闻分析师，<br class="notice-break">本地导入的资料不会被使用。<br class="notice-break">/);
    ui.window.uiTest.showToast('已从断点重新启动，已完成的角色不会再调用模型');
    const toast = ui.doc.querySelector('#toast-host .toast');
    assert.equal(toast.textContent.replace(/\s+/g, ''), '✓已从断点重新启动，已完成的角色不会再调用模型');
    assert.match(toast.innerHTML, /重新启动，<br class="toast-break">已完成的角色不会再调用模型/);
  } finally { ui.dom.window.close(); }
});

test('a compare card does not split 评级 when the saved rating is not structured', async () => {
  const ui = await setup();
  try {
    const card = ui.doc.createElement('div');
    card.innerHTML = ui.window.uiTest.compareCard({rating: 'Overweight', source: 'text', evidence_note: '没有结构化证据引用，不从正文猜测。'});
    const line = card.querySelector('p');
    assert.equal(line.textContent, '记录里有 Overweight，但不把它当成结构化评级。');
    assert.match(line.innerHTML, /记录里有 Overweight，<br class="notice-break">但不把它当成结构化评级。/);
  } finally { ui.dom.window.close(); }
});

test('a cost match note does not split 核对', async () => {
  const ui = await setup({'/api/cost': {runs: [{
    id: 'c-match', ticker: 'NVDA', date: '2026-11-12',
    api_text: '费用无法核对。',
    match_text: '有用量没有归到这次运行的角色，不把部分合计当成已核对。',
    health_text: '登录失效或无权限，不是预算超限。',
    roles: [],
  }], note: ''}});
  try {
    await ui.click('[data-page=cost]');
    const hint = ui.doc.querySelector('.cost-card .form-hint');
    assert.equal(hint.textContent, '有用量没有归到这次运行的角色，不把部分合计当成已核对。');
    assert.match(hint.innerHTML, /这次运行的角色，<br class="notice-break">不把部分合计当成已核对。/);
    const health = ui.doc.querySelector('.cost-health');
    assert.equal(health.textContent, '登录失效或无权限，不是预算超限。');
    assert.match(health.innerHTML, /无权限，<br class="notice-break">不是预算超限。/);
  } finally { ui.dom.window.close(); }
});

test('an unsaved crawl count does not split 结果', async () => {
  const ui = await setup();
  try {
    const html = ui.window.uiTest.infoStatusHtml({status: 'failed', sources: {sec: {status: 'ok', count: 12}}}, []);
    const row = ui.doc.createElement('div');
    row.innerHTML = html;
    const item = row.querySelector('[data-source-status=sec]');
    assert.equal(item.textContent.replace(/\s+/g, ''), 'sec来源返回了12条，但结果没有保存，不把条数当成已有结果。');
    assert.match(item.innerHTML, /来源返回了 12 条，<br class="notice-break">但结果没有保存，<br class="notice-break">不把条数当成已有结果。/);
  } finally { ui.dom.window.close(); }
});

test('replay and wizard hints do not split 提示词 or leave 页面 alone', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=compare]');
    ui.ui.state.compareReplay = {
      note: '回放使用这次运行保存的提示词版本，不用当前提示词。',
      steps: [{agent: 'market', zh: '市场分析师', status_zh: '完成', time: '09:00'}],
      prompts: [{agent: 'market', zh: '市场分析师', sha256: 'abc', saved: true, changed: null}],
    };
    ui.ui.state.compareStep = 0;
    ui.window.uiTest.paintReplay();
    const hint = ui.doc.querySelector('#compare-replay .form-hint');
    assert.equal(hint.textContent, '回放使用这次运行保存的提示词版本，不用当前提示词。');
    assert.match(hint.innerHTML, /提示词版本，<br class="notice-break">不用当前提示词。/);
    const saved = ui.doc.querySelector('.compare-prompt span:last-child');
    assert.match(saved.textContent, /当前源码没有这个角色，回放仍用保存版本/);
    assert.match(saved.innerHTML, /这个角色，<br class="notice-break">回放仍用保存版本/);
    await ui.click('[data-page=analyze]');
    await ui.click('.fw[data-framework=tradingagents]');
    await fillToReview(ui);
    const step = ui.doc.querySelector('#step-cli');
    assert.equal(step.textContent, '启动后会自动打开查看状态，可以随时离开这个页面。');
    assert.match(step.innerHTML, /查看状态，<br class="notice-break">可以随时离开这个页面。/);
  } finally { ui.dom.window.close(); }
});

test('an ended run list does not show an unrecorded completion count', async () => {
  const finished = {
    id: 'bt-list', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08',
    done: 0, total: 12, team_zh: '进程已结束', unrecorded: true, started_at: '2026-11-12T09:00:00',
  };
  const running = {
    id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'AAPL', date: '2026-11-12',
    done: 0, total: 12, team_zh: '准备中', started_at: '2026-11-12T09:10:00',
  };
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: finished,
    runs: [running, finished],
    progress: {agents: []},
    result: {present: false},
    counts: {all: 2, running: 1, queued: 0, failed: 0, finished: 1},
  }});
  try {
    await ui.click('[data-page=runs]');
    const list = ui.doc.querySelector('#run-list').textContent;
    assert.match(list, /没有记录/);
    assert.match(ui.doc.querySelector('#unrecorded-list').textContent, /不写成完成数/);
    const rows = [...ui.doc.querySelectorAll('#run-list [data-run-id]')];
    const ended = rows.find((row) => row.dataset.runId === 'bt-list');
    const live = rows.find((row) => row.dataset.runId === 'bt-live');
    assert.match(ended.textContent, /没有记录/);
    assert.doesNotMatch(ended.textContent, /0\/12/);
    assert.match(live.textContent, /0\/12/);
    assert.doesNotMatch(live.querySelector('.pg').textContent, /没有记录/);
  } finally { ui.dom.window.close(); }
});

test('an ended run without role records is not given a completion count', async () => {
  const pending = {team: 'Analyst Team', team_zh: '分析师团队', agent: 'Market Analyst', zh: '市场分析师', status: 'pending', status_zh: '等待'};
  const finished = {
    run: {id: 'bt-count', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}},
    runs: [{id: 'bt-count', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08'}],
    progress: {agents: [pending, {team: 'Research Team', team_zh: '研究团队', agent: 'Bull Researcher', zh: '多头研究员', status: 'pending', status_zh: '等待'}]},
    result: {present: false},
  };
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': finished});
  try {
    await openRunFromList(ui, 'bt-count');
    const progress = ui.doc.querySelector('#task-progress').textContent;
    assert.match(progress, /没有角色进度，不写成已完成/);
    assert.doesNotMatch(progress, /个角色完成|等待进度|0\/2/);
    ui.routes['/api/runs'] = {
      run: {id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'NVDA', selections: {ticker: 'NVDA'}},
      runs: [{id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'NVDA'}],
      progress: {agents: [pending]},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'bt-live';
    await ui.ui.renderRuns();
    assert.match(ui.doc.querySelector('#task-progress').textContent, /0\/1 个角色完成/);
    assert.doesNotMatch(ui.doc.querySelector('#task-progress').textContent, /不写成已完成/);

    ui.routes['/api/runs'] = {
      run: {id: 'an-blank', status: 'finished', kind: 'analysis', ticker: 'NVDA', selections: {ticker: 'NVDA'}},
      runs: [{id: 'an-blank', status: 'finished', kind: 'analysis', ticker: 'NVDA'}],
      progress: {agents: [pending]},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'an-blank';
    await ui.ui.renderRuns();
    assert.match(ui.doc.querySelector('#role-progress').textContent, /没有角色进度，不写成已完成/);
    assert.equal(ui.doc.querySelector('#role-progress .bar, #current-task .bar'), null);
    ui.routes['/api/runs'] = {
      run: {id: 'an-live', status: 'running', kind: 'analysis', ticker: 'NVDA', selections: {ticker: 'NVDA'}},
      runs: [{id: 'an-live', status: 'running', kind: 'analysis', ticker: 'NVDA'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'an-live';
    await ui.ui.renderRuns();
    assert.match(ui.doc.querySelector('#waiting-progress').textContent, /等待进度/);
    assert.doesNotMatch(ui.doc.querySelector('#waiting-progress').textContent, /不写成已完成/);
  } finally { ui.dom.window.close(); }
});

test('a backtest date span is not called an analysis date', async () => {
  const backtest = {
    run: {id: 'bt-span', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08', selections: {ticker: 'NVDA'}},
    runs: [{id: 'bt-span', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-10-01..2026-10-08'}],
    progress: {agents: []},
    result: {present: false},
    counts: {all: 1, running: 0, queued: 0, failed: 0, finished: 1},
  };
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': backtest});
  try {
    await ui.click('[data-page=runs]');
    assert.match(ui.doc.querySelector('#run-list').textContent, /回测区间 2026-10-01\.\.2026-10-08/);
    assert.doesNotMatch(ui.doc.querySelector('#run-list').textContent, /分析日期/);
    await openRunFromList(ui, 'bt-span');
    const summary = ui.doc.querySelector('#current-task').textContent;
    assert.match(summary, /回测区间/);
    assert.match(summary, /2026-10-01\.\.2026-10-08/);
    assert.doesNotMatch(summary, /分析日期/);
    ui.routes['/api/runs'] = {
      run: {id: 'an-date', status: 'finished', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA', date: '2026-11-12'}},
      runs: [{id: 'an-date', status: 'finished', kind: 'analysis', ticker: 'NVDA', date: '2026-11-12'}],
      progress: {agents: []},
      result: {present: false},
    };
    ui.ui.state.runDetailOpen = false;
    ui.ui.state.selectedRunId = null;
    await ui.ui.renderRuns();
    assert.match(ui.doc.querySelector('#run-list').textContent, /分析日期 2026-11-12/);
    ui.routes['/api/runs'] = {
      run: {id: 'kind-date', status: 'running', kind: 'index', ticker: 'AAA', date: '2026-11-12', selections: {ticker: 'AAA'}},
      runs: [{id: 'kind-date', status: 'running', kind: 'index', ticker: 'AAA', date: '2026-11-12'}],
      progress: {agents: []},
      result: {present: false},
    };
    await ui.ui.renderRuns();
    assert.match(ui.doc.querySelector('#run-list').textContent, /日期，不把它写成分析日期/);
    assert.doesNotMatch(ui.doc.querySelector('#run-list small').textContent, /^分析日期/);
  } finally { ui.dom.window.close(); }
});

test('an ended run does not call an unrecorded role still waiting', async () => {
  const pending = {team: 'Trading Team', team_zh: '交易团队', agent: 'Trader', zh: '交易员', status: 'pending', status_zh: '等待'};
  const finished = {
    run: {id: 'bt-wait', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-11-12', selections: {ticker: 'NVDA'}},
    runs: [{id: 'bt-wait', status: 'finished', kind: 'backtest', ticker: 'NVDA', date: '2026-11-12'}],
    progress: {agents: [pending]},
    result: {present: false},
    counts: {all: 1, running: 0, queued: 0, failed: 0, finished: 1},
  };
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': finished});
  try {
    await openRunFromList(ui, 'bt-wait');
    const badge = ui.doc.querySelector('.progress-table .status-badge').textContent;
    assert.equal(badge, '未记录');
    assert.doesNotMatch(ui.doc.querySelector('.progress-table').textContent, /等待/);
    assert.match(ui.doc.querySelector('#unrecorded-roles').textContent, /不把它们写成等待/);
    ui.routes['/api/runs'] = {
      run: {id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'NVDA', selections: {ticker: 'NVDA'}},
      runs: [{id: 'bt-live', status: 'running', kind: 'backtest', ticker: 'NVDA'}],
      progress: {agents: [pending]},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'bt-live';
    await ui.ui.renderRuns();
    assert.equal(ui.doc.querySelector('.progress-table .status-badge').textContent, '等待');
    assert.equal(ui.doc.querySelector('#unrecorded-roles'), null);

    ui.routes['/api/runs'] = {
      run: {id: 'failed-wait', status: 'failed', kind: 'analysis', selections: {ticker: 'NVDA', date: '2026-11-12'}},
      runs: [{id: 'failed-wait', status: 'failed', kind: 'analysis', ticker: 'NVDA'}],
      progress: {agents: [
        {team: 'Research Team', team_zh: '研究团队', agent: 'Bull Researcher', zh: '多头研究员', status: 'error', status_zh: '错误'},
        pending,
      ]},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'failed-wait';
    await ui.ui.renderRuns();
    assert.match(ui.doc.querySelector('.team.bad').textContent, /失败/);
    const later = ui.doc.querySelector('.team[data-team="Trading Team"]');
    assert.equal(later.querySelector('.pill').textContent, '未记录');
    assert.match(later.textContent, /不把它们写成等待/);
    assert.doesNotMatch(later.querySelector('.pill').textContent, /等待/);
  } finally { ui.dom.window.close(); }
});

test('a failed run with a completion line is not called analysis complete', async () => {
  const failed = failedRun({saved: true});
  failed.progress.complete = 'Analysis Complete!';
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': failed});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'failed-fixture');
    const note = ui.doc.querySelector('#completion-note');
    assert.ok(note);
    assert.match(note.textContent, /日志里有完成句，但任务状态是失败，不把它写成分析已完成/);
    assert.match(note.innerHTML, /日志里有完成句，<br class="notice-break">但任务状态是失败，<br class="notice-break">不把它写成分析已完成。/);
    assert.doesNotMatch(note.textContent, /^分析已完成/);
    const finished = {run: {id: 'done-fixture', status: 'finished', kind: 'analysis', selections: {ticker: 'NVDA', date: '2026-11-11'}}, progress: {agents: [], complete: 'Analysis Complete!'}, result: {present: false}, runs: [{id: 'done-fixture', status: 'finished', ticker: 'NVDA', date: '2026-11-11'}]};
    ui.routes['/api/runs'] = finished;
    ui.ui.state.selectedRunId = 'done-fixture';
    await ui.ui.renderRuns();
    assert.equal(ui.doc.querySelector('#completion-note').textContent, '分析已完成。');
  } finally { ui.dom.window.close(); }
});

test('a stopped or finished restart is not called a failed restart', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'fin', status: 'finished', kind: 'analysis', selections: {ticker: 'AAPL', date: '2026-11-11'}},
    progress: {agents: []}, result: {present: false},
    runs: [{id: 'fin', status: 'finished', ticker: 'AAPL', date: '2026-11-11'}],
  }});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'fin');
    const cases = [
      ['stopped', /任务已停止，不把它写成已重新启动/],
      ['finished', /任务已经结束，不把它写成已重新启动/],
      ['cancelled', /任务已取消，不把它写成已重新启动/],
      ['stopping', /任务正在停止，不把它写成已重新启动/],
      ['mystery', /重新启动结果状态未知，不把它写成已启动/],
    ];
    for (const [status, pattern] of cases) {
      ui.routes['/api/runs/restart'] = {id: 'fin', status, zh: '已重新启动'};
      ui.routes['/api/runs'] = {run: {id: 'fin', status: 'finished', kind: 'analysis', selections: {ticker: 'AAPL'}}, progress: {agents: []}, result: {present: false}, runs: [{id: 'fin', status: 'finished', ticker: 'AAPL'}]};
      await ui.ui.renderRuns();
      await ui.click('#restart-run');
      await settleMany();
      const toast = ui.doc.querySelector('#toast-host .toast');
      assert.match(toast.textContent, pattern, status);
      assert.doesNotMatch(toast.textContent, /没有重新启动成功|已重新启动任务/);
      assert.equal(toast.classList.contains('toast--unknown'), status !== 'stopping', status);
      assert.equal(toast.classList.contains('toast--wait'), status === 'stopping', status);
    }
  } finally { ui.dom.window.close(); }
});

test('a restart without a status is not called submitted', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'fin', status: 'finished', kind: 'analysis', selections: {ticker: 'AAPL', date: '2026-11-11'}},
    progress: {agents: []}, result: {present: false},
    runs: [{id: 'fin', status: 'finished', ticker: 'AAPL', date: '2026-11-11'}],
  }});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'fin');
    ui.routes['/api/runs/restart'] = {id: 'fin', zh: '已重新启动'};
    await ui.click('#restart-run');
    await settleMany();
    const toast = ui.doc.querySelector('#toast-host .toast');
    assert.match(toast.textContent, /不把它写成已提交或已重新启动/);
    assert.doesNotMatch(toast.textContent, /任务已提交|已重新启动任务/);
    assert.equal(toast.classList.contains('toast--unknown'), true);
    assert.doesNotMatch(toast.querySelector('.ck').textContent, /✓/);
  } finally { ui.dom.window.close(); }
});

test('a resume without a status is not called submitted or resumed', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': failedRun({saved: true})});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'failed-fixture');
    ui.routes['/api/runs/restart'] = {id: 'failed-fixture', zh: '已从断点重新启动'};
    await ui.click('#resume-run');
    await settleMany();
    const toast = ui.doc.querySelector('#toast-host .toast');
    assert.match(toast.textContent, /不把它写成已提交或已从断点重新启动/);
    assert.doesNotMatch(toast.textContent, /任务已提交/);
    assert.equal(toast.classList.contains('toast--unknown'), true);
  } finally { ui.dom.window.close(); }
});

test('a queued resume is not called already restarted', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': failedRun({saved: true})});
  try {
    await ui.click('[data-page=runs]');
    await openRunFromList(ui, 'failed-fixture');
    ui.routes['/api/runs/restart'] = {id: 'queued-resume', status: 'queued', zh: '已加入队列，前面还有 1 个。有任务结束后会自动开始。', queue_ahead: 1};
    ui.routes['/api/runs'] = {run: {id: 'queued-resume', status: 'queued', kind: 'analysis', selections: {ticker: 'NVDA'}}, progress: {agents: []}, result: {present: false}};
    await ui.click('#resume-run');
    await settleMany();
    const toastEl = ui.doc.querySelector('#toast-host .toast');
    const toast = toastEl.textContent;
    assert.match(toast, /已加入队列/);
    assert.match(toast, /还没有|有任务结束后会自动开始/);
    assert.doesNotMatch(toast, /已从断点重新启动|已重新启动/);
    assert.equal(toastEl.classList.contains('toast--wait'), true);
    assert.doesNotMatch(toastEl.querySelector('.ck').textContent, /✓/);
    ui.routes['/api/runs'] = failedRun({saved: true});
    ui.routes['/api/runs/restart'] = {id: 'running-resume', status: 'running', zh: '已在后台启动。进程启动不等于分析完成。'};
    await ui.ui.renderRuns();
    await ui.click('#resume-run');
    await settleMany();
    const runningToast = ui.doc.querySelector('#toast-host .toast');
    assert.match(runningToast.textContent, /已从断点重新启动/);
    assert.equal(runningToast.querySelector('.ck').textContent, '✓');
    assert.equal(runningToast.classList.contains('toast--wait'), false);
  } finally { ui.dom.window.close(); }
});

test('a failed info crawl does not call an unsaved source count available', async () => {
  const sources = {inject_default: false, existing: [], sources: [
    {id: 'sec_edgar', name: 'SEC EDGAR 公告', kind: 'filing', assets: ['stock'], terms_url: 'https://www.sec.gov/os/accessing-edgar-data', description: '官方披露', optional: false, available: true, reason: ''},
    {id: 'gdelt', name: 'GDELT 全球新闻索引', kind: 'news', assets: ['stock'], terms_url: 'https://www.gdeltproject.org/about.html', description: '新闻', optional: false, available: true, reason: ''},
  ]};
  const failed = {id: 'ic-20261111-save0001', keyword: 'NVDA', asset_type: 'stock', hours: 72, status: 'failed',
    reason: '抓取结果没有保存成功（OSError），请重新抓取。',
    sources: {sec_edgar: {status: 'ok', count: 1, reason: ''}, gdelt: {status: 'unavailable', count: 0, reason: '来源暂不可用'}}};
  const ui = await setup({
    '/api/frameworks': {...FRAMEWORKS, tools: [{id: 'info-crawl', name: '信息抓取', description: '官方来源', icon: '/icons/logo.svg', tags: ['公告'], tool: true}]},
    '/api/info-crawl/sources': sources,
    '/api/info-crawl/jobs': () => ({job: failed}),
    '/api/info-crawl/items': {job: failed, items: []},
  }, {url: 'http://localhost/#start'});
  try {
    const {doc, click, fill} = ui;
    await click('.fw[data-framework="info-crawl"]');
    for (let i = 0; i < 6; i++) await settle();
    ui.ui.state.infoPollMs = 0;
    fill('#ic-keyword', 'NVDA');
    doc.querySelector('#ic-form').dispatchEvent(new ui.window.Event('submit', {cancelable: true}));
    for (let i = 0; i < 3; i++) { await new Promise((r) => setTimeout(r, 15)); await settleMany(); }
    const edgar = doc.querySelector('[data-source-status="sec_edgar"]').textContent;
    assert.match(edgar, /来源返回了 1 条，但结果没有保存，不把条数当成已有结果/);
    assert.doesNotMatch(edgar, /公告 1 条$/);
    assert.match(doc.querySelector('[data-source-status="gdelt"]').textContent, /不可用/);
    assert.doesNotMatch(doc.querySelector('#ic-status').textContent, /抓取中/);
    assert.match(doc.querySelector('#ic-job-note').textContent, /没有保存成功/);
    assert.equal(doc.querySelector('#ic-items'), null);
    assert.equal(doc.querySelector('#ic-start').textContent, '开始抓取');
  } finally { ui.window.close(); }
});

test('K 线行情: 查询 button + Enter, typing marks old chart 未更新, suggestions pick then fetch (V2-UI-FIX-1)', async () => {
  const ui = await klineSetup('stock', 60);
  try {
    const doc = ui.doc;
    const flush = async () => { for (let i = 0; i < 8; i++) await settle(); };
    const klineCalls = () => ui.calls.filter((c) => c.path.startsWith('/api/kline?')).map((c) => c.path);
    assert.equal(doc.querySelector('#kline-go').textContent, '查询');
    assert.equal(doc.querySelector('#kline-go').type, 'submit');
    const before = klineCalls().length;
    const input = doc.querySelector('#kline-symbol');
    input.value = '002323';
    input.dispatchEvent(new ui.window.Event('input'));
    input.dispatchEvent(new ui.window.Event('change'));
    await flush();
    assert.equal(klineCalls().length, before, 'typing / change alone does not fetch');
    assert.equal(doc.querySelector('#kline-stale').hidden, false);
    assert.match(doc.querySelector('#kline-stale').textContent, /未更新/);
    assert.ok(doc.querySelector('#kline-card').classList.contains('kline-card--stale'));
    assert.equal(ui.timers.size >= 1, true);
    await ui.runTimers();
    assert.ok(ui.calls.some((c) => c.path.startsWith('/api/kline/search?q=002323')));
    doc.querySelector('#kline-form').dispatchEvent(new ui.window.Event('submit', {cancelable: true}));
    await flush();
    assert.match(klineCalls().at(-1), /symbol=002323&/);
    assert.equal(doc.querySelector('#kline-stale').hidden, true, 'after 查询 the label clears');
  } finally { ui.dom.window.close(); }
});

test('K 线行情: draw → loading keeps 未更新 dim, draw → fail clears old chart/title/source and shows Chinese error (V2-UI-FIX-1 F1)', async () => {
  const bars = [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}];
  let mode = 'ok'; let release;
  const route = async (_o, path) => {
    if (mode === 'ok') return {symbol: 'NVDA', canonical: 'NVDA', asset_type: 'stock', tf: '1D', provider: 'yahoo', provider_symbol: 'NVDA', bars};
    if (mode === 'hang') { await new Promise((r) => { release = r; }); return {__status: 400, body: {error: '行情源暂时没有这只股票的数据（代码格式正确：830799.BJ），请稍后再试。'}}; }
    return {__status: 400, body: {error: '行情源暂时没有这只股票的数据（代码格式正确：002323.SZ），请稍后再试。'}};
  };
  const ui = await klineSetup('stock', 60, route);
  try {
    const doc = ui.doc;
    const flush = async () => { for (let i = 0; i < 8; i++) await settle(); };
    assert.match(doc.querySelector('#kline-source').textContent, /NVDA/);
    const input = doc.querySelector('#kline-symbol');
    const submit = async () => { doc.querySelector('#kline-form').dispatchEvent(new ui.window.Event('submit', {cancelable: true})); await flush(); };
    // loading
    mode = 'hang'; input.value = '830799'; input.dispatchEvent(new ui.window.Event('input')); await submit();
    assert.equal(doc.querySelector('#kline-stale').hidden, false);
    assert.match(doc.querySelector('#kline-stale').textContent, /加载中：830799.*未更新/);
    assert.ok(doc.querySelector('#kline-card').classList.contains('kline-card--stale'));
    // fail after loading
    release(); await flush();
    const status = doc.querySelector('#kline-status');
    assert.equal(status.hidden, false);
    assert.match(status.textContent, /行情源暂时没有这只股票的数据（代码格式正确/);
    assert.doesNotMatch(status.textContent, /Error/);
    assert.equal(doc.querySelector('#kline-name').textContent, '830799');
    assert.equal(doc.querySelector('#kline-source').textContent, '数据源：—');
    assert.equal(doc.querySelector('#kline-price').textContent, '—');
    assert.equal(doc.querySelector('#kline-stale').hidden, true);
    // draw again, then a direct failure
    mode = 'ok'; input.value = 'NVDA'; await submit();
    assert.equal(doc.querySelector('#kline-status').hidden, true);
    assert.match(doc.querySelector('#kline-source').textContent, /NVDA/);
    mode = 'fail'; input.value = '002323'; await submit();
    assert.match(doc.querySelector('#kline-status').textContent, /代码格式正确：002323\.SZ/);
    assert.doesNotMatch(doc.querySelector('#kline-source').textContent, /NVDA/);
    assert.equal(doc.querySelector('#kline-name').textContent, '002323');
    assert.equal(doc.querySelector('#kline-note').textContent, '没有拿到行情。');
    assert.equal(doc.querySelector('#kline-live').hidden, true);
    assert.equal(doc.querySelector('#kline-live').textContent, '');
    assert.equal(doc.querySelector('#kline-future').textContent, '');
  } finally { ui.dom.window.close(); }
});

test('an unrecognized kline timeframe is not called daily', async () => {
  const bars = [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}];
  const ui = await klineSetup('stock', 60, async () => ({
    symbol: 'NVDA', canonical: 'NVDA', asset_type: 'stock', tf: '15m',
    provider: 'yahoo', provider_symbol: 'NVDA', bars,
    span_note: '小时线，请求近 60 天。',
  }));
  try {
    const source = ui.doc.querySelector('#kline-source').textContent;
    const note = ui.doc.querySelector('#kline-note').textContent;
    assert.match(source, /周期未知，不把它写成日线/);
    assert.match(note, /周期未知，不把它写成日线/);
    assert.match(ui.doc.querySelector('#kline-note').innerHTML, /不把它写成日线。<br class="notice-break">/);
    assert.match(note, /不按日线刷新/);
    assert.doesNotMatch(note, /知情|60 秒|小时线|周线|月线|5分钟/);
    assert.doesNotMatch(source, /股票日线|期货日线|加密货币日线|小时|周线|月线|5分钟/);
    assert.equal(ui.doc.querySelector('#kline-tf .seg-item.on'), null);
    assert.match(ui.doc.querySelector('#kline-live').textContent, /不把它写成日线/);
    assert.equal(ui.timers.size, 0);
    await ui.runTimers();
    assert.equal(ui.calls.some((item) => item.path.includes('/api/kline/latest') && item.path.includes('tf=1D')), false);
  } finally { ui.dom.window.close(); }
});

test('a kline response without a timeframe is not called daily', async () => {
  const bars = [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}];
  const ui = await klineSetup('stock', 60, async () => ({
    symbol: 'NVDA', canonical: 'NVDA', asset_type: 'stock',
    provider: 'yahoo', provider_symbol: 'NVDA', bars,
  }));
  try {
    const source = ui.doc.querySelector('#kline-source').textContent;
    const note = ui.doc.querySelector('#kline-note').textContent;
    assert.match(source, /周期未知，不把它写成日线/);
    assert.match(note, /周期未知，不把它写成日线/);
    assert.match(note, /不按日线刷新/);
    assert.doesNotMatch(note, /知情|60 秒/);
    assert.doesNotMatch(source, /股票日线/);
    assert.equal(ui.doc.querySelector('#kline-tf .seg-item.on'), null);
    assert.match(ui.doc.querySelector('#kline-live').textContent, /不把它写成日线/);
    await ui.runTimers();
    assert.equal(ui.calls.some((item) => item.path.includes('/api/kline/latest') && item.path.includes('tf=1D')), false);
  } finally { ui.dom.window.close(); }
});

test('a known daily kline is still called daily', async () => {
  const ui = await klineSetup('stock', 60);
  try {
    assert.match(ui.doc.querySelector('#kline-source').textContent, /股票日线/);
    assert.match(ui.doc.querySelector('#kline-note').textContent, /日线/);
    assert.doesNotMatch(ui.doc.querySelector('#kline-note').textContent, /不把它写成日线/);
    assert.equal(ui.doc.querySelector('#kline-tf .seg-item.on')?.textContent, '日线');
    assert.doesNotMatch(ui.doc.querySelector('#kline-live').textContent, /不把它写成日线/);
    await ui.runTimers();
    assert.equal(ui.calls.some((item) => item.path.includes('/api/kline/latest') && item.path.includes('tf=1D')), true);
  } finally { ui.dom.window.close(); }
});

test('a five minute kline is still called five minutes', async () => {
  const bars = [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}];
  const ui = await klineSetup('stock', 60, async () => ({
    symbol: 'NVDA', canonical: 'NVDA', asset_type: 'stock', tf: '5m',
    provider: 'yahoo', provider_symbol: 'NVDA', bars,
    span_note: '5分钟线，请求近 5 天，长度以实际返回为准。',
  }));
  try {
    assert.match(ui.doc.querySelector('#kline-source').textContent, /股票5分钟线 NVDA/);
    assert.match(ui.doc.querySelector('#kline-note').textContent, /5分钟线/);
    assert.doesNotMatch(ui.doc.querySelector('#kline-source').textContent, /日线/);
    assert.doesNotMatch(ui.doc.querySelector('#kline-note').textContent, /不把它写成日线/);
    assert.equal(ui.doc.querySelector('#kline-tf .seg-item.on')?.textContent, '5分钟线');
  } finally { ui.dom.window.close(); }
});

test('a kline without an asset type is not called a stock', async () => {
  const bars = [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}];
  const ui = await klineSetup('stock', 60, async () => ({symbol: 'NVDA', canonical: 'NVDA', tf: '1D', provider: 'yahoo', provider_symbol: 'NVDA', bars}));
  try {
    const source = ui.doc.querySelector('#kline-source').textContent;
    const note = ui.doc.querySelector('#kline-note').textContent;
    const live = ui.doc.querySelector('#kline-live').textContent;
    assert.match(source, /不把它写成股票/);
    assert.doesNotMatch(source, /数据源：股票|数据源：期货|数据源：加密货币/);
    assert.match(note, /不把它写成股票，也不写成延时行情/);
    assert.doesNotMatch(note, /知情：最新价约每 60 秒/);
    assert.match(live, /不把它写成股票/);
    assert.doesNotMatch(live, /知情|约 60 秒|实时 ·/);
  } finally { ui.dom.window.close(); }
});

test('a later kline tick without an asset type is not called delayed stock', async () => {
  const bars = [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}, {time: 1762041600, open: 10.5, high: 12, low: 10, close: 11, volume: 6}];
  const ui = await klineSetup('stock', 60, async () => ({symbol: 'NVDA', canonical: 'NVDA', tf: '1D', provider: 'yahoo', provider_symbol: 'NVDA', bars}), {
    '/api/kline/latest': {poll_seconds: 60, provider: 'yahoo', provider_symbol: 'NVDA', delayed: true, bar: {time: 1762041600, open: 10.5, high: 13, low: 10, close: 12.5, volume: 9}},
  });
  try {
    await ui.runTimers();
    const live = ui.doc.querySelector('#kline-live').textContent;
    const note = ui.doc.querySelector('#kline-note').textContent;
    assert.match(live, /不把它写成股票/);
    assert.doesNotMatch(live, /知情|约 60 秒|实时 ·/);
    assert.doesNotMatch(note, /知情：延时行情|知情：最新价约每 60 秒/);
  } finally { ui.dom.window.close(); }
});

test('an unrecognized asset type is not called a stock', async () => {
  const ui = await setup({'/api/asset': {asset_type: 'index', analysts: ['market', 'news'], fundamentals_hidden: false, ticker: 'AAA'}});
  try {
    ui.fill('#ticker', 'AAA');
    ui.doc.querySelector('#ticker').dispatchEvent(new ui.window.Event('change', {bubbles: true}));
    for (let i = 0; i < 8; i++) await settle();
    const hint = ui.doc.querySelector('#asset-hint').textContent;
    assert.match(hint, /不把它写成股票/);
    assert.doesNotMatch(hint, /按代码识别为股票/);
    assert.equal(ui.doc.querySelector('#asset-seg .seg-item.on'), null);
  } finally { ui.dom.window.close(); }
});

test('a detected stock is still called a stock', async () => {
  const ui = await setup({'/api/asset': {asset_type: 'stock', analysts: ['market', 'news', 'fundamentals'], fundamentals_hidden: false, ticker: 'AAPL'}});
  try {
    ui.fill('#ticker', 'AAPL');
    ui.doc.querySelector('#ticker').dispatchEvent(new ui.window.Event('change', {bubbles: true}));
    for (let i = 0; i < 8; i++) await settle();
    assert.match(ui.doc.querySelector('#asset-hint').textContent, /按代码识别为股票/);
    assert.equal(ui.doc.querySelector('#asset-seg [data-asset="stock"]').classList.contains('on'), true);
  } finally { ui.dom.window.close(); }
});

test('opening a watchlist kline without an asset type does not select stock', async () => {
  const ui = await setup({
    '/api/watchlist': {mode: 'observe', mode_label: '观察', rule_version: 'watch-1', note: '', items: [
      {symbol: '600519', enabled: true, last_price: 10},
    ], triggers: [], visible_triggers: []},
    '/api/watchlist/kline': {mode: 'observe', items: [], triggers: [], visible_triggers: []},
  });
  try {
    await ui.click('[data-page=watchlist]');
    for (let i = 0; i < 6; i++) await settle();
    const button = ui.doc.querySelector('[data-watch-kline]');
    assert.equal(button.dataset.watchAsset, undefined);
    assert.doesNotMatch(ui.doc.querySelector('.watch-table').textContent, /股票/);
    await ui.click('[data-watch-kline]');
    for (let i = 0; i < 6; i++) await settle();
    assert.equal(ui.doc.querySelector('#kline-asset .seg-item.on'), null);
    assert.equal(ui.ui.state.kline.asset, '');
    assert.equal(ui.ui.state.kline.symbol, '600519');
  } finally { ui.dom.window.close(); }
});

test('opening a stock watchlist kline still selects stock', async () => {
  const ui = await setup({
    '/api/watchlist': {mode: 'observe', mode_label: '观察', rule_version: 'watch-1', note: '', items: [
      {symbol: '600519', asset_type: 'stock', asset_label: '股票', enabled: true, last_price: 10},
    ], triggers: [], visible_triggers: []},
    '/api/watchlist/kline': {mode: 'observe', items: [], triggers: [], visible_triggers: []},
  });
  try {
    await ui.click('[data-page=watchlist]');
    for (let i = 0; i < 6; i++) await settle();
    assert.equal(ui.doc.querySelector('[data-watch-kline]').dataset.watchAsset, 'stock');
    await ui.click('[data-watch-kline]');
    for (let i = 0; i < 6; i++) await settle();
    assert.equal(ui.doc.querySelector('#kline-asset .seg-item.on').textContent, '股票');
    assert.equal(ui.ui.state.kline.asset, 'stock');
  } finally { ui.dom.window.close(); }
});

test('K 线行情: Sina source pill + 未复权 note on fallback; unknown Chinese name shows server hint in the dropdown (W174/W168/W176)', async () => {
  const bars = [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}];
  let resp = {symbol: '002323', canonical: '002323.SZ', asset_type: 'stock', tf: '1D', provider: 'sina', provider_symbol: 'sz002323', source_label: '新浪（非官方，不复权）', fallback: true, adjusted: false, bars};
  const ui = await klineSetup('stock', 60, async () => resp, {
    '/api/kline/search': {query: '不存在', results: [], hint: '没有找到这个名称，请用 6 位代码查询。'},
  });
  try {
    const doc = ui.doc;
    const flush = async () => { for (let i = 0; i < 8; i++) await settle(); };
    assert.equal(doc.querySelector('#kline-source').textContent, '数据源：新浪（非官方，不复权）');
    assert.doesNotMatch(doc.querySelector('#kline-source').textContent, /sz002323/);
    assert.equal(doc.querySelector('#kline-adjnote').hidden, false);
    assert.equal(doc.querySelector('#kline-adjnote').textContent, '价格未复权，可能与复权价不一致');
    assert.ok(doc.querySelector('#kline-vol'), 'separate volume pane');
    resp = {...resp, symbol: '920002', canonical: '920002.BJ', provider_symbol: 'bj920002', fallback: false};
    const input = doc.querySelector('#kline-symbol');
    input.value = '920002';
    doc.querySelector('#kline-form').dispatchEvent(new ui.window.Event('submit', {cancelable: true})); await flush();
    assert.equal(doc.querySelector('#kline-source').textContent, '数据源：新浪（非官方，不复权）');
    assert.equal(doc.querySelector('#kline-adjnote').hidden, true, 'BJ is Sina-only, not a fallback');
    input.value = '不存在';
    input.dispatchEvent(new ui.window.Event('input'));
    await ui.runTimers();
    const list = doc.querySelector('#kline-suggest');
    assert.equal(list.hidden, false, 'dropdown stays open with the hint');
    assert.equal(doc.querySelector('#kline-suggest-hint').textContent, '没有找到这个名称，请用 6 位代码查询。');
  } finally { ui.dom.window.close(); }
});

// V2-AUTH-LOGIN: the login page (two steps, empty-field error, eye toggle, same wrong-credential
// message) and the account menu. Passwords are random per run.
async function loginPage(mode = 'login', reply = {status: 401, body: {error: '邮箱或密码错误'}}) {
  const html = readFileSync(resolve(root, 'webui/static/login.html'), 'utf8')
    .replace('<meta charset="utf-8">', `<meta charset="utf-8"><meta name="tradingagents-login-mode" content="${mode}"><meta name="tradingagents-token" content="${mode === 'login' ? '' : 'tok-fixture'}">`)
    .replace('<script src="/login.js"></script>', '');
  const dom = new JSDOM(html, {url: 'http://localhost/login', runScripts: 'outside-only'});
  const {window} = dom;
  const calls = [];
  const assigned = [];
  window.fetch = async (path, options = {}) => {
    calls.push({path, ...options});
    return {ok: reply.status < 300, status: reply.status, json: async () => structuredClone(reply.body)};
  };
  window.__assign = (url) => assigned.push(url);   // jsdom cannot navigate; record where the page would go
  window.eval(readFileSync(resolve(root, 'webui/static/login.js'), 'utf8').replaceAll('window.location.assign(', 'window.__assign('));
  return {window, doc: window.document, calls, assigned};
}
const randomPassword = () => require('node:crypto').randomBytes(12).toString('base64url');
const submit = async (doc, id) => { doc.getElementById(id).requestSubmit(); for (let i = 0; i < 6; i++) await settle(); };

test('登录页: step 1 empty email shows 此字段不能为空, then step 2 通过密码验证 with eye toggle and 返回登录页 (V2-AUTH-LOGIN)', async () => {
  const {doc, calls} = await loginPage();
  assert.equal(doc.getElementById('step-email').hidden, false);
  assert.equal(doc.getElementById('email-next').textContent, '下一步');
  await submit(doc, 'email-form');
  assert.equal(doc.getElementById('email-error').hidden, false);
  assert.match(doc.getElementById('email-error').textContent, /此字段不能为空/);
  assert.equal(calls.length, 0, 'step 1 never asks the server (no user enumeration)');
  doc.getElementById('email').value = 'owner@example.invalid';
  await submit(doc, 'email-form');
  assert.equal(doc.getElementById('step-password').hidden, false);
  assert.match(doc.querySelector('#step-password h1').textContent, /通过密码验证/);
  const input = doc.getElementById('password');
  const eye = doc.querySelector('#step-password [data-eye]');
  eye.click();
  assert.equal(input.type, 'text');
  eye.click();
  assert.equal(input.type, 'password');
  await submit(doc, 'password-form');
  assert.match(doc.getElementById('password-error').textContent, /此字段不能为空/);
  doc.getElementById('back-to-email').click();
  assert.equal(doc.getElementById('step-email').hidden, false);
  assert.equal(doc.body.textContent.includes('注册') || doc.body.textContent.includes('忘记密码') || doc.body.textContent.includes('解锁'), false);
});

test('登录页: wrong credentials show 邮箱或密码错误 and clear the field; success goes to the console (V2-AUTH-LOGIN)', async () => {
  const wrong = await loginPage();
  wrong.doc.getElementById('email').value = 'owner@example.invalid';
  await submit(wrong.doc, 'email-form');
  wrong.doc.getElementById('password').value = randomPassword();
  await submit(wrong.doc, 'password-form');
  assert.equal(wrong.calls[0].path, '/api/account/login');
  assert.match(wrong.doc.getElementById('password-error').textContent, /邮箱或密码错误/);
  assert.equal(wrong.doc.getElementById('password').value, '');
  const ok = await loginPage('login', {status: 200, body: {ok: true, must_change: true}});
  ok.doc.getElementById('email').value = 'owner@example.invalid';
  await submit(ok.doc, 'email-form');
  ok.doc.getElementById('password').value = randomPassword();
  await submit(ok.doc, 'password-form');
  assert.deepEqual(ok.assigned, ['/login'], 'must_change → back to /login for the forced change');
});

test('首次登录: forced change checks length and match, sends the session token (V2-AUTH-LOGIN)', async () => {
  const {doc, calls, assigned} = await loginPage('force', {status: 200, body: {ok: true}});
  assert.equal(doc.getElementById('step-change').hidden, false);
  assert.match(doc.getElementById('change-title').textContent, /首次登录请修改密码/);
  assert.equal(doc.getElementById('old-row').hidden, true);
  const next = randomPassword();
  doc.getElementById('new-password').value = next;
  doc.getElementById('confirm-password').value = next + 'x';
  await submit(doc, 'change-form');
  assert.match(doc.getElementById('change-error').textContent, /两次输入的密码不一致/);
  doc.getElementById('confirm-password').value = next;
  await submit(doc, 'change-form');
  assert.equal(calls[0].path, '/api/account/password');
  assert.equal(calls[0].headers['X-TradingAgents-Token'], 'tok-fixture');
  assert.deepEqual(assigned, ['/']);
  const change = await loginPage('change');
  assert.equal(change.doc.getElementById('old-row').hidden, false);
  assert.match(change.doc.getElementById('change-title').textContent, /修改密码/);
});

test('账户菜单: bottom-left button opens 修改密码 / 退出登录 (V2-AUTH-LOGIN)', async () => {
  const html = markup.replace('<meta charset="utf-8">', '<meta charset="utf-8"><meta name="tradingagents-account" content="owner@example.invalid">');
  const ui = await setup({'/api/account/logout': {ok: true}}, {html});
  const menu = ui.doc.querySelector('#account-menu');
  assert.equal(menu.hidden, true);
  assert.equal(ui.doc.querySelector('#account-name').textContent, 'owner@example.invalid');
  assert.match(ui.doc.querySelector('#identity-name').textContent, /^模型服务/, 'top-right is model-service status, not console login');
  await ui.click('#account-button');
  assert.equal(menu.hidden, false);
  assert.equal(ui.doc.querySelector('#account-change-password').getAttribute('href'), '/login?change=1');
  assert.match(ui.doc.querySelector('#account-logout').textContent, /退出登录/);
});

test('kline errors, live marks, and fallback headings do not split 老代码 or leave 查询 alone', async () => {
  const bj = '行情源近一年没有 830000.BJ 的数据：北交所 4/8 开头的老代码已切换为 920 开头的新代码，请用新代码查询。';
  const empty = '行情源暂时没有这只股票的数据（代码格式正确：600519.SS）。';
  const bars = [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5}];
  const ok = {symbol: 'XAUUSD', canonical: 'XAUUSD', asset_type: 'commodity', tf: '1D', provider: 'yahoo', provider_symbol: 'XAUUSD', bars};
  let mode = 'ok';
  let release;
  const ui = await klineSetup('commodity', 60, async () => {
    if (mode === 'hang') {
      await new Promise((done) => { release = done; });
      return {__status: 400, body: {error: bj}};
    }
    if (mode === 'empty') return {__status: 400, body: {error: empty}};
    return ok;
  }, {'/api/kline/latest': {
    asset_type: 'commodity', poll_seconds: 60, delayed: true, stale_bar: true, freshness_note: '可能未更新',
    bar: {time: 1762041600, open: 10.5, high: 13, low: 10, close: 12.5, volume: 9},
  }});
  try {
    const doc = ui.doc;
    const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((done) => setImmediate(done)); };
    await ui.runTimers();
    const live = doc.querySelector('#kline-live');
    assert.equal(live.textContent, '最新一根已经过去一段时间，不据此判断是否开市。 知情 · 约 60 秒询问 · 可能未更新');
    assert.match(live.innerHTML, /一段时间，<br class="notice-break">不据此判断是否开市。/);
    const input = doc.querySelector('#kline-symbol');
    input.value = '贵州茅台';
    input.dispatchEvent(new ui.window.Event('input'));
    const stale = doc.querySelector('#kline-stale');
    assert.equal(stale.textContent, '未更新：当前仍是 XAUUSD，点「查询」或按回车查看 贵州茅台');
    assert.match(stale.innerHTML, /XAUUSD，<br class="notice-break">点「查询」或按回车查看 贵州茅台/);

    mode = 'hang';
    input.value = '830799';
    doc.querySelector('#kline-form').dispatchEvent(new ui.window.Event('submit', {cancelable: true}));
    await flush();
    assert.equal(stale.textContent, '加载中：830799…（下图仍是 XAUUSD，未更新）');
    assert.match(stale.innerHTML, /830799…<br class="notice-break">（下图仍是 XAUUSD，<br class="notice-break">未更新）/);
    release();
    await flush();
    const status = doc.querySelector('#kline-status');
    assert.equal(status.textContent, bj);
    assert.match(status.innerHTML, /数据：<br class="notice-break">北交所 4\/8 开头的老代码<br class="notice-break">已切换为 920 开头的新代码，<br class="notice-break">请用新代码查询。/);
    assert.doesNotMatch(status.textContent, /<br/);

    mode = 'empty';
    input.value = '600519';
    doc.querySelector('#kline-form').dispatchEvent(new ui.window.Event('submit', {cancelable: true}));
    await flush();
    assert.equal(status.textContent, empty);
    assert.match(status.innerHTML, /这只股票的数据<br class="notice-break">（代码格式正确：600519\.SS）。/);

    ui.routes['/api/watchlist'] = () => { throw new Error('列表暂时读不到。'); };
    await ui.click('[data-page=watchlist]');
    const watch = doc.querySelector('.page-description');
    assert.equal(watch.textContent, '股票、加密货币和贵金属。规则命中时记一条触发并提醒。观察模式不会自动启动分析。');
    assert.match(watch.innerHTML, /贵金属。<br class="notice-break">规则命中时记一条触发并提醒。<br class="notice-break">观察模式不会自动启动分析。/);

    ui.routes['/api/schedule/tick'] = () => { throw new Error('定时暂时读不到。'); };
    await ui.click('[data-page=schedule]');
    const schedule = doc.querySelector('.page-description');
    assert.equal(schedule.textContent, '到点只记录触发。观察模式不会自动启动分析。去分析只打开向导。提醒先写入，再显示在本页。');
    assert.match(schedule.innerHTML, /触发。<br class="notice-break">观察模式不会自动启动分析。<br class="notice-break">去分析只打开向导。<br class="notice-break">提醒先写入，<br class="notice-break">再显示在本页。/);
  } finally { ui.dom.window.close(); }
});

test('key status and kline asset buttons do not split 填写 or 贵金属', async () => {
  const ui = await setup({'/api/config': {saved: {}, keys: [
    {provider: 'openai', configured: false, env_var: 'OPENAI_API_KEY'},
  ], data_env: [], cloud_env: []}, '/api/kline': {symbol: 'X', canonical: 'X', asset_type: 'commodity', tf: '1D', provider: 'yahoo', provider_symbol: 'X', bars: []}});
  try {
    await ui.click('[data-page=accounts]');
    const status = ui.doc.querySelector('#key-status');
    assert.equal(status.textContent, '尚未配置 API Key。使用订阅账户时无需填写。');
    assert.match(status.innerHTML, /API Key。<br class="notice-break">使用订阅账户时无需填写。/);
    assert.doesNotMatch(status.innerHTML, /填<br|写<br/);
    await ui.click('[data-page=kline]');
    const metal = [...ui.doc.querySelectorAll('#kline-asset .seg-item')].find((button) => button.textContent === '贵金属/商品期货');
    assert.ok(metal, 'commodity asset button');
    const css = [...ui.doc.styleSheets].map((sheet) => [...sheet.cssRules].map((rule) => rule.cssText).join('\n')).join('\n');
    assert.match(css, /\.kline-controls \.seg-item\s*\{[^}]*white-space:\s*nowrap/);
    assert.match(css, /\.kline-controls \.seg-item\s*\{[^}]*min-width:\s*max-content/);
  } finally { ui.dom.window.close(); }
});

test('update check and account load errors do not leave 信息 or 重试 alone', async () => {
  const ui = await setup({'/api/updates/refresh': () => new Promise(() => {})});
  try {
    await ui.click('[data-page=accounts]');
    await ui.click('#check-updates');
    const note = ui.doc.querySelector('#updates-note');
    assert.equal(note.textContent, '正在检查更新。这会询问 GitHub 和公告服务，不发送身份信息。');
    assert.match(note.innerHTML, /更新。<br class="notice-break">这会询问 GitHub 和公告服务，<br class="notice-break">不发送身份信息。/);
    ui.routes['/api/config'] = () => { throw new Error(''); };
    await ui.click('[data-page=accounts]');
    const banner = ui.doc.querySelector('#account-load-error .error-banner');
    assert.equal(banner.textContent, '操作未完成。请检查网络或本机登录材料后重试。');
    assert.match(banner.innerHTML, /操作未完成。<br class="notice-break">请检查网络或本机登录材料后重试。/);
  } finally { ui.dom.window.close(); }
});

test('a fixed kline price line and a commodity note do not split 高低价 or 期货', async () => {
  const ui = await klineSetup('stock', 60, {
    symbol: 'X', canonical: 'X', asset_type: 'stock', tf: '1D', provider: 'yahoo', provider_symbol: 'X',
    ohlc_note: '源数据不一致，已修正高低价',
    bars: [{time: 1761955200, open: 10, high: 11, low: 9, close: 10.5, volume: 5, ohlc_fixed: true}],
  });
  try {
    const ohlc = ui.doc.querySelector('#kline-ohlc');
    assert.match(ohlc.textContent, /收 10\.50/);
    assert.match(ohlc.textContent, /源数据不一致，已修正高低价/);
    assert.match(ohlc.innerHTML, /<br class="notice-break">源数据不一致，已修正高低价/);
    assert.doesNotMatch(ohlc.innerHTML, /高低<br|价<br/);
    ui.routes['/api/asset'] = {asset_type: 'commodity', analysts: ['market'], fundamentals_hidden: true};
    await ui.click('[data-page=analyze]');
    await ui.click('.fw[data-framework=tradingagents]');
    const ticker = ui.doc.querySelector('#ticker');
    ticker.value = 'XAUUSD';
    ticker.dispatchEvent(new ui.window.Event('change'));
    for (let i = 0; i < 6; i++) await new Promise((done) => setImmediate(done));
    const note = ui.doc.querySelector('#form-note');
    assert.equal(note.textContent, '已识别为贵金属/商品期货，不使用基本面分析。');
    assert.match(note.innerHTML, /贵金属\/商品期货，<br class="notice-break">不使用基本面分析。/);
  } finally { ui.dom.window.close(); }
});

test('status notes and the watch rule fallback do not split 完成 or 检查', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'bt-note', status: 'finished', kind: 'backtest', ticker: 'NVDA', selections: {ticker: 'NVDA'}, started_at: '2026-11-11T10:00:00'},
    runs: [{id: 'bt-note', status: 'finished', kind: 'backtest', ticker: 'NVDA'}],
    progress: {complete: 'Analysis Complete!', agents: [
      {team: 'Analyst Team', team_zh: '分析师团队', agent: 'Market Analyst', zh: '市场分析师', status: 'mystery', status_zh: 'mystery'},
    ]},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'bt-note');
    const done = ui.doc.querySelector('#completion-note');
    assert.equal(done.textContent, '日志里有分析完成句。这不表示整次回测已完成。');
    assert.match(done.innerHTML, /分析完成句。<br class="notice-break">这不表示整次回测已完成。/);
    const role = ui.doc.querySelector('#unknown-role-status');
    assert.equal(role.textContent, '角色状态未知，不把它写成进行中或已完成。');
    assert.match(role.innerHTML, /状态未知，<br class="notice-break">不把它写成进行中或已完成。/);
    ui.routes['/api/runs'] = {
      run: {id: 'ended', status: 'failed', kind: 'analysis', selections: {ticker: '600519', date: '2026-11-11'}, started_at: '2026-11-10T10:00:00'},
      runs: [{id: 'ended', status: 'failed', kind: 'analysis', ticker: '600519'}],
      progress: {agents: [], timeline: []},
      result: {present: false},
    };
    ui.ui.state.selectedRunId = 'ended';
    await ui.ui.renderRuns();
    const end = ui.doc.querySelector('#missing-end');
    assert.equal(end.textContent, '没有结束时间，不按当前时间计算已用时间。');
    assert.match(end.innerHTML, /结束时间，<br class="notice-break">不按当前时间计算已用时间。/);
    ui.routes['/api/watchlist'] = () => { throw new Error('列表暂时读不到。'); };
    await ui.click('[data-page=watchlist]');
    const rule = ui.doc.querySelector('#watch-rule');
    assert.equal(rule.textContent, '打开 K 线不消耗触发。未检查表示还没有规则检查；已检查并不表示已启动。');
    assert.match(rule.innerHTML, /不消耗触发。<br class="notice-break">未检查表示还没有规则检查；<br class="notice-break">已检查并不表示已启动。/);
  } finally { ui.dom.window.close(); }
});

test('an idle backtest next step does not split 结束', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {id: 'bt-idle', status: 'idle', kind: 'backtest', ticker: 'NVDA', selections: {ticker: 'NVDA'}},
    runs: [{id: 'bt-idle', status: 'idle', kind: 'backtest', ticker: 'NVDA'}],
    progress: {agents: []},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'bt-idle');
    const note = ui.doc.querySelector('#next-step .notice');
    assert.equal(note.textContent, '任务状态是空闲，暂时没有可显示的结果。不把它写成已结束。');
    assert.match(note.innerHTML, /空闲，<br class="notice-break">暂时没有可显示的结果。<br class="notice-break">不把它写成已结束。/);
    assert.doesNotMatch(note.innerHTML, /已<br|结<br/);
  } finally { ui.dom.window.close(); }
});

test('a restart without an id does not split 刷新状态', async () => {
  const ui = await setup({'/api/logs': {lines: []}, '/api/runs': {
    run: {status: 'finished', kind: 'analysis', ticker: 'NVDA', selections: {ticker: 'NVDA'}},
    runs: [{id: 'listed', status: 'finished', kind: 'analysis', ticker: 'NVDA'}],
    progress: {agents: []},
    result: {present: false},
  }});
  try {
    await openRunFromList(ui, 'listed');
    await ui.click('#restart-run');
    const banner = ui.doc.querySelector('.error-banner');
    assert.equal(banner.textContent, '这个任务没有 id，无法操作。请刷新状态后再试。');
    assert.match(banner.innerHTML, /没有 id，<br class="notice-break">无法操作。<br class="notice-break">请刷新状态后再试。/);
    assert.doesNotMatch(banner.innerHTML, /刷<br|新<br/);
  } finally { ui.dom.window.close(); }
});

test('the gpt-5 temperature note does not split 推理模型', async () => {
  const ui = await setup();
  try {
    ui.ui.state.form.quick_think_llm = 'gpt-5.4';
    ui.ui.state.form.deep_think_llm = 'gpt-5-chat-latest';
    await ui.click('[data-page=models]');
    const note = ui.doc.querySelector('#gpt5-temperature-note');
    assert.equal(note.textContent, '所选模型 gpt-5.4 属于 gpt-5 系列推理模型（非 chat 版）：temperature 只接受 1，填写其他值会被直接去掉，不会生效。');
    assert.match(note.innerHTML, /gpt-5\.4 <br class="notice-break">属于 gpt-5 系列推理模型（非 chat 版）：<br class="notice-break">temperature 只接受 1，<br class="notice-break">填写其他值会被直接去掉，<br class="notice-break">不会生效。/);
    assert.doesNotMatch(note.innerHTML, /推理<br|模型<br/);
  } finally { ui.dom.window.close(); }
});

test('the ChatGPT refusal note does not split 订阅', async () => {
  const ui = await setup();
  try {
    ui.ui.state.form.llm_provider = 'chatgpt';
    ui.ui.state.form.quick_think_provider = 'chatgpt';
    ui.ui.state.form.deep_think_provider = 'chatgpt';
    await ui.click('[data-page=models]');
    const note = ui.doc.querySelector('#model-refusal-note');
    assert.match(note.textContent, /当前有一层使用 ChatGPT 订阅：订阅不接受 temperature 和 max_tokens，填写了也不会发送。/);
    assert.match(note.innerHTML, /ChatGPT 订阅：<br class="notice-break">订阅不接受 temperature 和 max_tokens，<br class="notice-break">填写了也不会发送。<br class="notice-break">日志会写<br class="notice-break">/);
    assert.doesNotMatch(note.innerHTML, /订<br|阅<br/);
    assert.match(note.textContent, /A ChatGPT plan does not accept temperature/);
  } finally { ui.dom.window.close(); }
});

test('a compare summary does not split 证据 on a narrow screen', async () => {
  const ui = await setup();
  try {
    const card = ui.doc.createElement('div');
    card.innerHTML = ui.window.uiTest.compareCard({
      source: 'structured',
      rating: 'Hold',
      summary: '建议持有，但要等待更多证据，不能现在下结论。',
      sections: [{heading: '要点', text: '第一条已经写出。第二条证据不足。'}],
      evidence: ['没有结构化证据引用，不从正文猜测。'],
    });
    const summary = card.querySelectorAll('article > p')[1];
    assert.equal(summary.textContent, '建议持有，但要等待更多证据，不能现在下结论。');
    assert.match(summary.innerHTML, /建议持有，<br class="notice-break">但要等待更多证据，<br class="notice-break">不能现在下结论。/);
    const section = card.querySelectorAll('article > p')[2];
    assert.equal(section.textContent, '要点 第一条已经写出。第二条证据不足。');
    assert.match(section.innerHTML, /已经写出。<br class="notice-break">第二条证据不足。/);
    const quote = card.querySelector('.compare-quotes li');
    assert.equal(quote.textContent, '没有结构化证据引用，不从正文猜测。');
    assert.match(quote.innerHTML, /证据引用，<br class="notice-break">不从正文猜测。/);
    assert.doesNotMatch(card.textContent, /已经猜测|证据已改/);
  } finally { ui.dom.window.close(); }
});

test('the account material note does not leave 校验 alone', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=accounts]');
    const notes = [...ui.doc.querySelectorAll('.account-provider-meta')].filter((node) => node.textContent.includes('不会联网校验'));
    assert.ok(notes.length >= 1);
    for (const note of notes) {
      assert.equal(note.textContent, '登录状态只读取本机材料，页面打开时不会联网校验。');
      assert.match(note.innerHTML, /本机材料，<br class="notice-break">页面打开时不会联网校验。/);
      assert.doesNotMatch(note.innerHTML, /校<br|验<br/);
    }
  } finally { ui.dom.window.close(); }
});

test('a failed crawl save note does not split 保存成功', async () => {
  const reason = '抓取任务没有保存成功（VendorUnavailableError），请重新抓取。';
  const ui = await setup();
  try {
    ui.ui.state.info = {
      keyword: 'NVDA', asset: 'stock', hours: 72, sources: [], selected: [],
      job: {id: 'job-1', status: 'failed', reason, sources: {}},
      items: [], error: '', picked: [], catalog: {sources: [], existing: []},
    };
    ui.ui.showPage('analyze', {framework: 'info-crawl'});
    const note = ui.doc.querySelector('#ic-job-note');
    assert.equal(note.textContent, reason);
    assert.match(note.innerHTML, /保存成功<br class="notice-break">（VendorUnavailableError），<br class="notice-break">请重新抓取。/);
    assert.doesNotMatch(note.innerHTML, /保<br|存<br/);
  } finally { ui.dom.window.close(); }
});

test('a failed source list does not split 来源列表', async () => {
  const error = '没能读取来源列表：不要把这次失败写成已经抓到条目。';
  const ui = await setup();
  try {
    ui.ui.state.info = {
      keyword: '', asset: 'stock', hours: 72, sources: [], selected: [],
      job: null, items: [], error, picked: [], catalog: {sources: [], existing: []},
    };
    ui.ui.showPage('analyze', {framework: 'info-crawl'});
    const banner = ui.doc.querySelector('#ic-error');
    assert.equal(banner.textContent, error);
    assert.match(banner.innerHTML, /来源列表：<br class="notice-break">不要把这次失败写成已经抓到条目。/);
    assert.doesNotMatch(banner.innerHTML, /来<br|源<br|列<br/);
  } finally { ui.dom.window.close(); }
});

test('a schedule reminder does not split 结构化评级', async () => {
  const body = '600519.SS 的分析已完成，记录里有 Overweight，但不把它当成结构化评级。';
  const ui = await setup({'/api/schedule/tick': {mode: 'observe', mode_label: '观察', rule_version: 'sched-1', note: '', schedules: [], reminders: [
    {id: 'r1', body, status: 'pending', status_label: '待发送', system_problem: true},
  ]}});
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    const row = ui.doc.querySelector('.schedule-reminder-main');
    assert.equal(row.textContent, `${body} 发送失败，仍会重试。`);
    assert.match(row.innerHTML, /600519\.SS 的分析已完成，<br class="notice-break">记录里有 Overweight，<br class="notice-break">但不把它当成结构化评级。<br class="notice-break"> 发送失败，<br class="notice-break">仍会重试。/);
    assert.doesNotMatch(row.innerHTML, /结<br|构<br|评<br|级<br/);
  } finally { ui.dom.window.close(); }
});

test('a kline name hint does not split 港股', async () => {
  const hint = '港股请用 4-5 位代码，例如 0700 或 00700；A 股请用 6 位代码。';
  const ui = await klineSetup('stock', 60, null, {
    '/api/kline/search': {query: '07', results: [], hint},
  });
  try {
    const input = ui.doc.querySelector('#kline-symbol');
    input.value = '07';
    input.dispatchEvent(new ui.window.Event('input'));
    await ui.runTimers();
    const row = ui.doc.querySelector('#kline-suggest-hint');
    assert.equal(row.textContent, hint);
    assert.match(row.innerHTML, /港股请用 4-5 位代码，<br class="notice-break">例如 0700 或 00700；<br class="notice-break">A 股请用 6 位代码。/);
    assert.doesNotMatch(row.innerHTML, /港<br|股<br/);
  } finally { ui.dom.window.close(); }
});

test('an unsupported kline code does not split 沪市 or 深市', async () => {
  const error = '暂不支持这个 6 位代码（支持沪市 6/90、深市 0/2/3、北交所 4/8/92 开头）。';
  const ui = await klineSetup('stock', 60, async () => ({__status: 400, body: {error}}));
  try {
    const input = ui.doc.querySelector('#kline-symbol');
    input.value = '123456';
    ui.doc.querySelector('#kline-form').dispatchEvent(new ui.window.Event('submit', {cancelable: true}));
    for (let i = 0; i < 8; i++) await settle();
    const status = ui.doc.querySelector('#kline-status');
    assert.equal(status.textContent, error);
    assert.match(status.innerHTML, /支持<br class="notice-break">沪市 6\/90、<br class="notice-break">深市 0\/2\/3、<br class="notice-break">北交所 4\/8\/92 开头）。/);
    assert.doesNotMatch(status.innerHTML, /沪<br|深<br|北<br|交<br/);
  } finally { ui.dom.window.close(); }
});

test('an auto schedule rule does not split 已启动', async () => {
  const ui = await setup({'/api/schedule/tick': {mode: 'auto', mode_label: '自动', rule_version: 'sched-1', note: '', schedules: [], reminders: []}});
  try {
    await ui.click('[data-page=schedule]');
    for (let i = 0; i < 6; i++) await settle();
    const rule = ui.doc.querySelector('#sched-rule');
    assert.equal(rule.textContent, '规则 sched-1 · 自动。到点会检查规则。没有运行编号不算已启动。');
    assert.match(rule.innerHTML, /自动。<br class="notice-break">到点会检查规则。<br class="notice-break">没有运行编号不算已启动。/);
    assert.doesNotMatch(rule.innerHTML, /已<br|启<br|动<br/);
  } finally { ui.dom.window.close(); }
});

test('a missing log path does not split 路径', async () => {
  const sentence = '这次控制台任务还没有日志路径。不编造命令行的路径句。';
  const ui = await setup({'/api/logs': {lines: [], path: null, cli: null}});
  try {
    await ui.click('[data-page=reports]');
    const details = ui.doc.querySelector('#report-log-details');
    details.open = true;
    details.dispatchEvent(new ui.window.Event('toggle'));
    for (let i = 0; i < 8; i++) await settle();
    const note = ui.doc.querySelector('#report-log-path');
    assert.equal(note.textContent, sentence);
    assert.match(note.innerHTML, /这次控制台任务还没有日志路径。<br class="notice-break">不编造命令行的路径句。/);
    assert.doesNotMatch(note.innerHTML, /路<br|径<br/);
  } finally { ui.dom.window.close(); }
});

test('a checkpoint note does not split 断点库', async () => {
  const note = '这些文件对应 --clear-checkpoints 会删除的断点库。是否续跑要等启动时用和 CLI 相同的签名判断，页面不提前声称已恢复。';
  const cleared = '已清除 2 个断点。这是删除结果，不是恢复，也没有启动模型。';
  const ui = await setup({'/api/checkpoints': {directory: '/tmp/checkpoints', note, files: []}, '/api/checkpoints/clear': {cleared: 2, zh: cleared}});
  try {
    ui.ui.openSaveDrawer(ui.doc.querySelector('#step-next'));
    for (let i = 0; i < 4; i++) await settle();
    await ui.click('#list-checkpoints');
    const list = ui.doc.querySelector('#checkpoint-list');
    assert.equal(list.textContent, `目录 /tmp/checkpoints${note}没有断点文件。`);
    assert.match(list.innerHTML, /会删除的<br class="notice-break">断点库。<br class="notice-break">是否续跑要等启动时用和 CLI 相同的签名判断，<br class="notice-break">页面不提前声称已恢复。/);
    assert.doesNotMatch(list.innerHTML, /断<br|点<br|库<br/);
    await ui.click('#clear-checkpoints-now');
    await ui.click('#clear-checkpoints-yes');
    assert.equal(list.textContent, cleared);
    assert.match(list.innerHTML, /已清除 2 个断点。<br class="notice-break">这是删除结果，<br class="notice-break">不是恢复，<br class="notice-break">也没有启动模型。/);
  } finally { ui.dom.window.close(); }
});

test('a vendor menu hint does not split 供应商', async () => {
  const vendor = '空格切换，a 全选或取消全选，Enter 完成。命令行没有供应商菜单，这里改的是 data_vendors。';
  const tool = '空格切换。留空沿用类别链。tool_vendors 优先于 data_vendors。';
  const ui = await setup();
  try {
    await ui.click('[data-page=accounts]');
    ui.doc.querySelector('#data-sources-details').open = true;
    await ui.click('#vendor-select-core_stock_apis .custom-select-trigger');
    const vendorHint = ui.doc.querySelector('.custom-select-menu .form-hint');
    assert.equal(vendorHint.textContent, vendor);
    assert.match(vendorHint.innerHTML, /空格切换，<br class="notice-break">a 全选或取消全选，<br class="notice-break">Enter 完成。<br class="notice-break">命令行没有供应商菜单，<br class="notice-break">这里改的是 data_vendors。/);
    assert.doesNotMatch(vendorHint.innerHTML, /供<br|应<br|商<br/);
    ui.doc.querySelector('#tool-vendor-details').open = true;
    await ui.click('#tool-select-get_stock_data .custom-select-trigger');
    const toolHint = ui.doc.querySelector('.custom-select-menu .form-hint');
    assert.equal(toolHint.textContent, tool);
    assert.match(toolHint.innerHTML, /空格切换。<br class="notice-break">留空沿用类别链。<br class="notice-break">tool_vendors 优先于 data_vendors。/);
    assert.doesNotMatch(toolHint.innerHTML, /留<br|沿<br/);
  } finally { ui.dom.window.close(); }
});

test('a failed model list does not split 模型列表', async () => {
  const detail = '模型列表没有读到。命令行会继续提供 Custom model ID，这里也可以手填。';
  const ui = await setup();
  try {
    const host = ui.doc.createElement('div');
    ui.doc.body.append(host);
    host.innerHTML = ui.window.uiTest.modelTable('chatgpt', {
      cli: 'Could not list models',
      detail,
      models: [],
      title: '快模型',
      mode: 'quick',
    });
    const warning = host.querySelector('#model-list-warning');
    assert.equal(warning.textContent, `${detail} 对应命令行：Could not list models`);
    assert.match(warning.innerHTML, /模型列表没有读到。<br class="notice-break">命令行会继续提供 Custom model ID，<br class="notice-break">这里也可以手填。 <br class="notice-break">对应命令行：Could not list models/);
    assert.doesNotMatch(warning.innerHTML, /模<br|型<br|列<br|表<br/);
    const list = ui.doc.createElement('div');
    ui.doc.body.append(list);
    ui.window.uiTest.renderModelChoices(list, {provider: 'chatgpt', models: [], detail}, 'quick');
    assert.match(list.textContent, /模型列表没有读到。命令行会继续提供 Custom model ID，这里也可以手填。/);
    assert.match(list.innerHTML, /模型列表没有读到。<br class="notice-break">命令行会继续提供 Custom model ID，<br class="notice-break">这里也可以手填。/);
    assert.doesNotMatch(list.innerHTML, /模<br|型<br|列<br|表<br/);
  } finally { ui.dom.window.close(); }
});

test('a save path refusal does not split 目录', async () => {
  const reason = '保存路径必须在结果目录的 reports 下。命令行可以选其他文件夹，控制台不写出这个目录。';
  const ui = await setup({'/api/runs': (options) => options.method ? {__status: 400, body: {error: reason}} : {run: null, progress: {}}});
  try {
    await fillToReview(ui);
    await ui.click('#run-analysis');
    await settleMany();
    const error = ui.doc.querySelector('#launch-error');
    assert.equal(error.textContent, reason);
    assert.match(error.innerHTML, /reports 下。<br class="notice-break">命令行可以选其他文件夹，<br class="notice-break">控制台不写出这个目录。/);
    assert.doesNotMatch(error.innerHTML, /目<br|录<br/);
  } finally { ui.dom.window.close(); }
});

// ---- V2-KLINE-INTRADAY mock v1 (Eric 2026-11-13): Beijing clock, trading day,
// day separators, 160/60-bar default view, 万/亿 volume.
const ET_1440 = Date.UTC(2026, 10, 12, 19, 40) / 1000; // 11-12 14:40 ET = 11-13 03:40 Beijing
const DAY_1112 = Date.UTC(2026, 10, 12) / 1000;
function intradayBars(count, {tradingDay = '2026-11-12', volume = 12345} = {}) {
  return Array.from({length: count}, (_, i) => {
    const back = (count - 1 - i) * 300;
    const close = 170 + (i % 7);
    return {time: ET_1440 - back, chart_time: DAY_1112 + (18 * 3600 + 40 * 60) - back, trading_day: tradingDay,
      open: close - 0.5, high: close + 1, low: close - 1, close, volume};
  });
}

async function intradaySetup(count, {narrow = false, route = {}, latest} = {}) {
  const bars = intradayBars(count);
  const payload = {symbol: 'NVDA', canonical: 'NVDA', asset_type: 'stock', tf: '5m', provider: 'yahoo', provider_symbol: 'NVDA',
    bars, stale_bar: false, span_note: '5分钟线，请求近 5 天，长度以实际返回为准。', history_label: '近5天', ...route};
  const ui = await setup({
    '/api/kline': (options, path) => (typeof payload === 'function' ? payload(path) : (path.includes('tf=1D') ? {...payload, tf: '1D', bars: [{time: DAY_1112, open: 1, high: 2, low: 0.5, close: 1.5, volume: 9}]} : payload)),
    '/api/kline/latest': latest || {asset_type: 'stock', poll_seconds: 60, delayed: true, bar: bars[bars.length - 1]},
  });
  ui.window.matchMedia = (query) => ({matches: narrow && /max-width: 700px/.test(query), addEventListener() {}, removeEventListener() {}});
  const charts = [];
  const ranges = [];
  let fits = 0;
  const makeSeries = (kind, opts) => ({kind, opts, data: [], setData(p) { this.data = p; }, update() {}, priceScale: () => ({applyOptions() {}})});
  ui.window.LightweightCharts = {createChart: (host, options) => {
    const chart = {host, options, series: [], subscribeCrosshairMove() {}, remove() {}, applyOptions() {},
      addCandlestickSeries(o) { const s = makeSeries('candles', o); this.series.push(s); return s; },
      addHistogramSeries(o) { const s = makeSeries('volume', o); this.series.push(s); return s; },
      timeScale: () => ({fitContent() { fits += 1; }, subscribeVisibleLogicalRangeChange() {}, setVisibleLogicalRange(r) { ranges.push(r); }})};
    charts.push(chart);
    return chart;
  }};
  ui.window.setTimeout = () => 0; // no polling in these tests
  ui.window.uiTest.state.kline = {symbol: 'NVDA', asset: 'stock', tf: '5m'};
  await ui.click('.nav-item[data-page=kline]');
  for (let i = 0; i < 8; i++) await settle();
  return {...ui, bars, charts, ranges, fits: () => fits};
}

test('K 线 volume reads in 万 and 亿 at the boundaries (V2-KLINE-INTRADAY)', async () => {
  const ui = await setup();
  try {
    const v = ui.window.klineVolume;
    assert.equal(v(0), '0');
    assert.equal(v(9999), '9999');
    assert.equal(v(9999.4), '9999');
    assert.equal(v(9999.6), '1.0万', 'rounds up into 万, never 10000');
    assert.equal(v(10000), '1.0万');
    assert.equal(v(17_729_000), '1772.9万');
    assert.equal(v(99_940_000), '9994.0万');
    assert.equal(v(99_999_999), '1.00亿', 'never 10000.0万');
    assert.equal(v(1e8), '1.00亿');
    assert.equal(v(2.5e9), '25.00亿');
    assert.equal(v(-1), '—');
    assert.equal(v(Number.NaN), '—');
  } finally { ui.dom.window.close(); }
});

test('K 线 intraday opens on the newest 160 bars, 60 on a narrow screen, and fits a short series (V2-KLINE-INTRADAY)', async () => {
  const pure = await setup();
  try {
    const w = pure.window;
    assert.equal(w.klineDefaultBars(false), 160);
    assert.equal(w.klineDefaultBars(true), 60);
    assert.deepEqual({...w.klineDefaultRange(390, false)}, {from: 230, to: 393});
    assert.deepEqual({...w.klineDefaultRange(390, true)}, {from: 330, to: 393});
    assert.equal(w.klineDefaultRange(160, false), null, '160 bars or fewer: fit them all');
    assert.equal(w.klineDefaultRange(60, true), null);
  } finally { pure.dom.window.close(); }
  const wide = await intradaySetup(200);
  try {
    assert.deepEqual(wide.ranges.map((r) => ({...r})), [{from: 40, to: 203}, {from: 40, to: 203}], 'price and volume panes both');
    assert.equal(wide.fits(), 0);
    assert.equal(wide.charts[0].options.rightPriceScale.minimumWidth, 72);
    assert.equal(wide.charts[1].options.rightPriceScale.minimumWidth, 72);
  } finally { wide.dom.window.close(); }
  const narrow = await intradaySetup(200, {narrow: true});
  try {
    assert.deepEqual({...narrow.ranges[0]}, {from: 140, to: 203});
  } finally { narrow.dom.window.close(); }
  const short = await intradaySetup(100);
  try {
    assert.equal(short.ranges.length, 0);
    assert.equal(short.fits(), 2);
  } finally { short.dom.window.close(); }
});

test('K 线 intraday axis and crosshair read Beijing time, day ticks and the readout read the trading day (V2-KLINE-INTRADAY)', async () => {
  const ui = await intradaySetup(200);
  try {
    const [price, volume] = ui.charts;
    const last = ui.bars[ui.bars.length - 1];
    const tick = price.options.timeScale.tickMarkFormatter;
    assert.equal(tick(last.chart_time, 3), '03:40', 'a time tick is the Beijing clock, not New York');
    assert.equal(tick(last.chart_time, 2), '11-12', 'a day tick is the trading day, not the Beijing date 11-13');
    assert.equal(price.options.localization.timeFormatter(last.chart_time), '11-13 03:40');
    assert.equal(price.options.timeScale.timeVisible, true);
    assert.equal(price.series[0].data.at(-1).time, last.chart_time, 'candles sit on chart_time');
    // the volume pane: no price label on its crosshair, 万/亿 on its axis
    assert.deepEqual({...volume.options.crosshair.horzLine}, {visible: false, labelVisible: false});
    assert.equal(volume.series[0].opts.priceFormat.type, 'custom');
    assert.equal(volume.series[0].opts.priceFormat.formatter(17_729_000), '1772.9万');
    const ohlc = ui.doc.querySelector('#kline-ohlc');
    assert.ok(ohlc.classList.contains('kline-ohlc--ro'), 'the readout row may wrap between items');
    assert.equal([...ohlc.children].map((c) => c.textContent).join('|'),
      '11-13 03:40 北京|交易日 11-12周四|形成中|开 172.50高 174.00低 172.00收 173.00|量 1.2万');
    assert.match(ui.doc.querySelector('#kline-note').textContent, /时间为北京时间，日期为交易日。/);
  } finally { ui.dom.window.close(); }
  const daily = await setup();
  try {
    assert.equal(daily.window.klineTickLabel(DAY_1112, '1D'), '11-12', 'daily ticks are unchanged');
  } finally { daily.dom.window.close(); }
});

test('K 线 day separators sit at trading-day changes; weekends say 周末休市 (周末 when narrow), a weekday holiday 休市 (V2-KLINE-INTRADAY)', async () => {
  const ui = await setup();
  try {
    const breaks = (days, narrow = false) => [...ui.window.klineDayBreaks(days.map((d, i) => ({time: 1_800_000_000 + i * 300, trading_day: d})), narrow)].map((b) => ({...b}));
    assert.deepEqual(breaks(['2026-11-11', '2026-11-11', '2026-11-12', '2026-11-12']), [{index: 2, day: '2026-11-12', label: ''}]);
    assert.deepEqual(breaks(['2026-11-13', '2026-11-16']), [{index: 1, day: '2026-11-16', label: '周末休市'}]);
    assert.deepEqual(breaks(['2026-11-13', '2026-11-16'], true), [{index: 1, day: '2026-11-16', label: '周末'}]);
    assert.deepEqual(breaks(['2026-11-25', '2026-11-27']), [{index: 1, day: '2026-11-27', label: '休市'}], 'Thanksgiving is not a weekend');
    assert.deepEqual(breaks(['2026-04-02', '2026-04-06']), [{index: 1, day: '2026-04-06', label: '周末休市'}], 'Good Friday + weekend');
    assert.deepEqual(breaks(['2026-11-12', '2026-11-12', '2026-11-12']), [], 'an overnight bar inside one trading day is no separator');
    // An older answer without trading_day falls back to the Beijing date.
    const old = [...ui.window.klineDayBreaks([{time: Date.UTC(2026, 10, 12, 15, 55) / 1000}, {time: Date.UTC(2026, 10, 12, 16, 0) / 1000}], false)];
    assert.deepEqual(old.map((b) => b.day), ['2026-11-13'], '16:00 UTC is 00:00 Beijing');
    assert.match(readFileSync(resolve(root, 'webui/static/app.css'), 'utf8'), /\.kline-gap \{[^}]*border-left: 1px dashed/);
  } finally { ui.dom.window.close(); }
});

test('K 线 period switch on the same chart fades it and names the coming period; an empty minute answer offers 日线 and 重试 (V2-KLINE-INTRADAY)', async () => {
  let release;
  const ui = await intradaySetup(200);
  try {
    ui.routes['/api/kline'] = () => new Promise((done) => { release = done; });
    await ui.click('#kline-tf [data-tf="1H"]');
    assert.match(ui.doc.querySelector('#kline-loading').textContent, /正在加载小时线…/);
    assert.ok(ui.doc.querySelector('#kline-card').classList.contains('kline-card--loading'));
    assert.equal(ui.doc.querySelector('#kline-tf [data-tf="1H"]').getAttribute('aria-busy'), 'true');
    assert.equal(ui.doc.querySelector('#kline-source').textContent, '数据源：正在加载…');
    assert.equal(ui.doc.querySelector('#kline-stale').hidden, true, 'same symbol: no 「未更新」 badge');
    release({symbol: 'NVDA', canonical: 'NVDA', asset_type: 'stock', tf: '1H', provider: 'yahoo', provider_symbol: 'NVDA', bars: intradayBars(10)});
    for (let i = 0; i < 8; i++) await settle();
    assert.equal(ui.doc.querySelector('#kline-loading'), null);
    assert.ok(!ui.doc.querySelector('#kline-card').classList.contains('kline-card--loading'));
    assert.equal(ui.doc.querySelector('#kline-tf [data-tf="1H"]').getAttribute('aria-busy'), null);
    assert.match(ui.doc.querySelector('#kline-source').textContent, /数据源：股票小时线 NVDA/);
  } finally { ui.dom.window.close(); }
  const empty = await intradaySetup(0);
  try {
    const box = empty.doc.querySelector('#kline-empty');
    assert.match(box.textContent, /还没有5分钟线K线/);
    assert.match(box.textContent, /数据源这次没有返回 NVDA 的5分钟线数据/);
    assert.match(empty.doc.querySelector('#kline-live').textContent, /没有分钟数据，暂不刷新/);
    await empty.click('#kline-empty [data-kline-tf="1D"]');
    for (let i = 0; i < 8; i++) await settle();
    assert.ok(empty.calls.some((c) => c.path.startsWith('/api/kline?') && c.path.includes('tf=1D')), '改看日线 asks for daily bars');
  } finally { empty.dom.window.close(); }
});

// V2-INPROGRESS-2 (V2-REPORT-COMPARE): two finished runs written by the real result
// payload (tests/v2_compare_fixtures.py, show off, no model call) render on the compare
// page with ratings, evidence and report sections; the replay steps across server pages.
test('compare shows two finished runs with sections and replays across pages', async () => {
  const {mkdtempSync, rmSync} = require('node:fs');
  const {tmpdir} = require('node:os');
  const home = mkdtempSync(resolve(tmpdir(), 'ta-compare-'));
  let data;
  try {
    data = JSON.parse(execFileSync(process.env.TRADINGAGENTS_TEST_PYTHON || 'python3',
      ['-m', 'tests.v2_compare_fixtures', home],
      {cwd: root, encoding: 'utf8', env: {...process.env, TRADINGAGENTS_WEBUI_HOME: home}}));
  } finally { rmSync(home, {recursive: true, force: true}); }
  const replayPaths = [];
  const ui = await setup({
    '/api/compare': (_options, path) => (path.includes('left=') ? data.pair : data.list),
    '/api/compare/replay': (_options, path) => {
      replayPaths.push(path);
      const page = new URL(path, 'http://localhost').searchParams.get('page') || '1';
      return data.replay[page] || {__status: 404, body: {error: '回放没有这一页。'}};
    },
  });
  try {
    const pass = async () => { for (let i = 0; i < 8; i++) await settle(); };
    await ui.click('.nav-item[data-page=compare]');
    await pass();
    const cards = [...ui.doc.querySelectorAll('#compare-grid .compare-card')];
    assert.equal(cards.length, 2);
    const [left, right] = cards.map((card) => card.textContent);
    assert.match(left, /评级 Buy/);
    assert.match(left, /ATR 4\.2/);
    assert.match(left, /一、分析师团队报告/);
    assert.match(left, /NVDA 行情/);
    assert.match(right, /评级 Hold/);
    assert.match(right, /PE 位于五年中位/);
    assert.match(right, /五、组合经理决定/);
    assert.match(right, /600519 组合经理/);
    assert.ok(cards.every((card) => card.querySelectorAll('.compare-section').length >= 3));

    const total = data.replay['1'].total_steps;
    const pages = data.replay['1'].pages;
    assert.ok(pages >= 2 && total > 40);
    const position = () => ui.doc.querySelector('#compare-position').textContent;
    assert.equal(position(), `第 1 / ${total} 步 · 第 1 / ${pages} 页`);
    assert.ok(ui.doc.querySelector('#compare-prev').disabled);
    for (let i = 0; i < 40; i++) { ui.doc.querySelector('#compare-next').click(); await pass(); }
    assert.equal(position(), `第 41 / ${total} 步 · 第 2 / ${pages} 页`);
    assert.ok(replayPaths.some((path) => path.endsWith('&page=2')));
    ui.doc.querySelector('#compare-prev').click();
    await pass();
    assert.equal(position(), `第 40 / ${total} 步 · 第 1 / ${pages} 页`);
    for (let i = 0; i < total; i++) {
      const next = ui.doc.querySelector('#compare-next');
      if (next.disabled) break;
      next.click();
      await pass();
    }
    assert.equal(position(), `第 ${total} / ${total} 步 · 第 ${pages} / ${pages} 页`);
    assert.ok(ui.doc.querySelector('#compare-next').disabled);
    assert.match(ui.doc.querySelector('.compare-step').textContent, /组合经理/);
    assert.ok(ui.doc.querySelectorAll('.compare-prompts li').length >= 12);
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST R7: api() keeps the HTTP status and the server code on a failure', async () => {
  const ui = await setup({
    '/api/kline/latest': {__status: 429, body: {error: '行情源限流，稍后重试。', code: 'rate_limited'}},
    '/api/fixture-bad': {__status: 400, body: {error: '不支持的代码。'}},
  });
  try {
    const limited = await ui.ui.api('/api/kline/latest?symbol=NVDA').catch((error) => error);
    assert.equal(limited.status, 429);
    assert.equal(limited.code, 'rate_limited');
    assert.equal(limited.message, '行情源限流，稍后重试。');
    const bad = await ui.ui.api('/api/fixture-bad').catch((error) => error);
    assert.equal(bad.status, 400);
    assert.equal(bad.code, undefined);
  } finally { ui.dom.window.close(); }
});

// ---- V2-WATCHLIST W4: rules column, price labels, rule drawer, 查看全部 (Proto mock v1).
const WL_RULES = {
  NVDA: [
    {id: 'r1', kind: 'above', value: 190, enabled: true, state: 'hit', label: '高于 190.00', chip: '高于 190.00', state_label: '已触发（北京 11-13 22:40）。价格回到 190.00 以下后重新生效。'},
    {id: 'r2', kind: 'pct', value: 3, enabled: true, state: 'armed', label: '涨跌 ≥ 3%（较昨收）', chip: '涨跌 ≥ 3%', state_label: '生效中。'},
    {id: 'r3', kind: 'win', value: 1.5, enabled: false, state: 'off', label: '一个刷新间隔内涨跌 ≥ 1.5%', chip: '间隔内涨跌 ≥ 1.5%', state_label: '已停用。'},
  ],
  XAUUSD: [{id: 'r4', kind: 'pct', value: 1, enabled: true, state: 'today', label: '涨跌 ≥ 1%（较昨收）', chip: '涨跌 ≥ 1%', state_label: '已触发（北京 11-12 23:15）。同一方向每个交易日只提醒一次，下个交易日重新生效。'}],
};

function wlItems() {
  return [
    {symbol: 'NVDA', asset_label: '股票', asset_type: 'stock', market: 'us', enabled: true, last_price: 190.62, change_pct: 2.39, delayed: true,
      quote_at: '2026-11-13T14:40:05+00:00', trading_day: '2099-01-01', rules: WL_RULES.NVDA, rule_cap: 5, at_cap: false},
    {symbol: 'BTC-USDT', asset_label: '加密货币', asset_type: 'crypto', market: 'crypto', enabled: true, last_price: 91240.5, change_pct: -2.35, delayed: false,
      quote_at: '2026-11-13T14:35:04+00:00', trading_day: '2026-11-13', rules: [], rule_cap: 5, at_cap: false},
    {symbol: 'XAUUSD', asset_label: '贵金属/商品期货', asset_type: 'commodity', market: 'commodity', enabled: true, last_price: 4082.6, change_pct: 0.42, delayed: true,
      quote_at: '2026-11-13T14:35:04+00:00', trading_day: '2026-11-13', rules: WL_RULES.XAUUSD, rule_cap: 5, at_cap: false},
    {symbol: '600519', asset_label: '股票', asset_type: 'stock', market: 'cn', enabled: true, last_price: 1452, change_pct: -0.65, delayed: true,
      quote_at: '2026-11-13T07:01:00+00:00', trading_day: '2026-11-12', rules: [], rule_cap: 5, at_cap: false},
  ];
}

function wlData(extra = {}) {
  return {mode: 'observe', mode_label: '观察', rule_version: 'watch-1', note: '', items: wlItems(), triggers: [], visible_triggers: [],
    poll_minutes: 5, poll_stored: false, has_rules: true, rule_cap: 5, unseen_count: 0, trigger_total: 0, ...extra};
}

test('V2-WATCHLIST: the table shows rules, the hit row, change labels and the Beijing fetch time; no UTC', async () => {
  const ui = await setup({'/api/watchlist': wlData(), '/api/watchlist/seen': wlData()});
  try {
    await ui.click('[data-page=watchlist]');
    const head = [...ui.doc.querySelectorAll('.watch-table th')].map((node) => node.textContent);
    assert.deepEqual(head, ['代码', '最近价格', '提醒规则', '操作']);
    const rows = [...ui.doc.querySelectorAll('.watch-table tbody tr')];
    assert.equal(rows.length, 4);
    assert.ok(rows[0].classList.contains('wl-row--hit'));
    assert.equal(rows[0].querySelector('.status-badge--alert').textContent, '已触发');
    assert.deepEqual([...rows[0].querySelectorAll('.wl-chip')].map((node) => node.textContent), ['高于 190.00', '涨跌 ≥ 3%', '间隔内涨跌 ≥ 1.5%']);
    assert.ok(rows[0].querySelector('.wl-chip--hit') && rows[0].querySelector('.wl-chip--off'));
    assert.equal(rows[0].querySelector('[data-watch-rules]').textContent, '提醒规则');
    assert.match(rows[0].textContent, /190\.62 \+2\.39%延迟较昨收 · 取价 北京 22:40/);
    assert.equal(rows[1].querySelector('.wl-meta').textContent, '较昨收（北京 08:00） · 取价 北京 22:35');
    assert.equal(rows[1].querySelector('.wl-none').textContent, '还没有规则');
    assert.equal(rows[1].querySelector('[data-watch-rules]').textContent, '添加规则');
    assert.equal(rows[1].querySelector('.wl-tag'), null);
    assert.equal(rows[2].querySelector('.wl-chip--wait').textContent, '涨跌 ≥ 1% · 今天已提醒');
    assert.equal(rows[2].querySelector('.wl-meta').textContent, '较昨收 · 取价 北京 22:35');
    // R3: an A-share price from an earlier trading day is that day's close.
    assert.equal(rows[3].querySelector('.wl-meta').textContent, '较前一交易日收盘 · 收盘价 · 北京 11-12 15:00');
    assert.doesNotMatch(ui.doc.querySelector('#page').textContent, /UTC/);
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST R3: close and session labels follow each market clock', async () => {
  const ui = await setup();
  try {
    const ctx = ui.ui.watchQuoteContext;
    const cn = {market: 'cn', asset_type: 'stock', trading_day: '2026-11-13'};
    assert.deepEqual([ctx(cn, new Date('2026-11-13T03:00:00Z')).label, ctx(cn, new Date('2026-11-13T03:00:00Z')).closed], ['较昨收', false]);
    const after = ctx(cn, new Date('2026-11-13T08:00:00Z'));
    assert.equal(after.closed, true);
    assert.equal(ui.ui.watchClock(after.closeAt), '15:00');
    const preOpen = ctx(cn, new Date('2026-11-16T00:30:00Z'));  // Monday 08:30 Beijing, Friday's bar
    assert.equal(preOpen.label, '较前一交易日收盘');
    const us = ctx({market: 'us', asset_type: 'stock', trading_day: '2026-11-13'}, new Date('2026-11-13T22:00:00Z'));
    assert.equal(us.closed, true);
    assert.equal(ui.ui.watchClock(us.closeAt), '05:00');  // 16:00 New York (EST) is 05:00 Beijing
    const summer = ctx({market: 'us', asset_type: 'stock', trading_day: '2026-07-01'}, new Date('2026-07-01T21:00:00Z'));
    assert.equal(ui.ui.watchClock(summer.closeAt), '04:00');  // EDT
    assert.equal(ctx({market: 'crypto', asset_type: 'crypto', trading_day: '2026-11-12'}).label, '较昨收（北京 08:00）');
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST R9: watchWhen is Beijing time even when the browser runs in New York', async () => {
  const before = process.env.TZ;
  process.env.TZ = 'America/New_York';
  const ui = await setup();
  try {
    assert.equal(new Date('2026-11-13T14:40:00Z').getHours(), 9);  // the zone really changed
    assert.equal(ui.ui.watchWhen('2026-11-13T14:40:00+00:00'), '11-13 22:40');
    assert.equal(ui.ui.watchWhen('2026-11-13T17:05:00Z'), '11-14 01:05');
    assert.equal(ui.ui.watchWhen('not a time'), '');
  } finally {
    ui.dom.window.close();
    if (before === undefined) delete process.env.TZ; else process.env.TZ = before;
  }
});

test('V2-WATCHLIST: the rule drawer lists rules, validates, saves and stops at five', async () => {
  const posts = [];
  let data = wlData();
  const ui = await setup({
    '/api/watchlist': () => data,
    '/api/watchlist/seen': () => data,
    '/api/watchlist/rules': (options) => {
      const body = JSON.parse(options.body);
      posts.push(body);
      if (body.op === 'add' && body.value === '3') return {__status: 400, body: {error: '已有一条「涨跌 ≥ 3%（较昨收）」，不用重复加。', code: 'duplicate'}};
      if (body.op === 'add') {
        data = structuredClone(data);
        const item = data.items[0];
        item.rules = item.rules.concat([{id: 'r9', kind: body.kind, value: body.value, enabled: true, state: 'armed', label: '低于 180.00', chip: '低于 180.00', state_label: '生效中。'},
          {id: 'r10', kind: 'below', value: 170, enabled: true, state: 'armed', label: '低于 170.00', chip: '低于 170.00', state_label: '生效中。'}]);
        item.at_cap = true;
      }
      return data;
    },
  });
  try {
    await ui.click('[data-page=watchlist]');
    await ui.click('[data-watch-rules="NVDA"]');
    const drawer = () => ui.doc.querySelector('.rule-drawer');
    assert.equal(ui.doc.querySelector('#rule-drawer-title').textContent, 'NVDA 提醒规则');
    assert.match(drawer().querySelector('.rule-now').textContent, /现价 190\.62 \+2\.39% 较昨收 · 取价 北京 22:40 · 可能延迟/);
    const items = [...drawer().querySelectorAll('.rule-item')];
    assert.equal(items.length, 3);
    assert.equal(items[0].querySelector('.rule-state').textContent, '已触发（北京 11-13 22:40）。价格回到 190.00 以下后重新生效。');
    assert.equal(items[2].querySelector('[role=switch]').getAttribute('aria-checked'), 'false');
    // Editing to -5 is refused before any request.
    await ui.click('[data-rule-edit="r1"]');
    ui.fill('#rule-edit-value', '-5');
    await ui.click('#rule-edit-save');
    assert.equal(drawer().querySelector('.rule-item--editing .rule-err').textContent, '请输入大于 0 的价格。');
    assert.ok(drawer().querySelector('#rule-edit-value').classList.contains('bad'));
    assert.equal(posts.length, 0);
    await ui.click('#rule-edit-cancel');
    // A duplicate is refused by the server with its own text.
    await ui.click('[data-rule-kinds="add"] [data-rule-kind="pct"]');
    ui.fill('#rule-add-value', '3');
    assert.equal(drawer().querySelector('#rule-preview').textContent, '较昨收涨或跌 3% 及以上时提醒。同一方向每个交易日只提醒一次。');
    assert.equal(drawer().querySelector('.unit').textContent, '%（较昨收，涨或跌都算）');
    await ui.click('#rule-add');
    assert.equal(drawer().querySelector('.rule-err').textContent, '已有一条「涨跌 ≥ 3%（较昨收）」，不用重复加。');
    assert.deepEqual(posts.at(-1), {symbol: 'NVDA', op: 'add', kind: 'pct', value: '3'});  // W259: the typed text
    await ui.click('[data-rule-kinds="add"] [data-rule-kind="below"]');
    assert.equal(drawer().querySelector('.unit').textContent, '美元');
    ui.fill('#rule-add-value', '180');
    assert.equal(drawer().querySelector('#rule-preview').textContent, '价格跌到 180.00 或以下时提醒一次。回到 180.00 以上后重新生效。');
    await ui.click('#rule-add');
    // R11: at five rules 添加规则 is disabled with the cap beside it.
    assert.equal(drawer().querySelectorAll('.rule-item').length, 5);
    const add = drawer().querySelector('#rule-add');
    assert.equal(add.disabled, true);
    assert.equal(add.textContent, '添加规则');
    assert.equal(add.nextElementSibling.textContent, '每个代码最多 5 条');
    assert.ok(add.nextElementSibling.classList.contains('form-hint'));
    assert.equal(drawer().querySelector('#rule-add-value'), null);
    assert.equal(ui.doc.querySelectorAll('.watch-table tbody tr')[0].querySelectorAll('.wl-chip').length, 5);
    await ui.click('#rule-drawer-done');
    assert.equal(drawer(), null);
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST R6: twenty triggers, then 查看全部 with the drawer link style; new hits have a dot', async () => {
  const triggers = Array.from({length: 25}, (_, i) => ({id: `t${i}`, symbol: 'NVDA', kind: 'rule', text: `高于 ${i}.00 · 现价 ${i + 1}.00`,
    created_at: `2026-11-13T14:${String(i).padStart(2, '0')}:00+00:00`, checked: false, status_label: '未检查', unseen: i >= 23}));
  const visible = triggers.slice(-20).reverse();
  const ui = await setup({'/api/watchlist': wlData({triggers, visible_triggers: visible, trigger_total: 25}), '/api/watchlist/seen': wlData()});
  try {
    await ui.click('[data-page=watchlist]');
    assert.equal(ui.doc.querySelectorAll('.watch-triggers li').length, 20);
    const first = ui.doc.querySelector('.watch-triggers li');
    assert.ok(first.classList.contains('wl-log-hit') && first.querySelector('.wl-new'));
    assert.match(first.textContent, /NVDA 高于 24\.00 · 现价 25\.00 11-13 22:24/);
    const more = ui.doc.querySelector('#watch-triggers-all');
    assert.equal(more.textContent, '查看全部（25 条）');
    assert.ok(more.classList.contains('link-button'));
    await ui.click('#watch-triggers-all');
    assert.equal(ui.doc.querySelectorAll('.watch-triggers li').length, 25);
    await ui.click('#watch-triggers-less');
    assert.equal(ui.doc.querySelectorAll('.watch-triggers li').length, 20);
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST: no symbols shows the empty state; a price reminder on 定时 carries 自选', async () => {
  const ui = await setup({
    '/api/watchlist': wlData({items: [], has_rules: false, poll_minutes: 0}),
    '/api/watchlist/seen': wlData({items: []}),
    '/api/schedule/tick': {schedules: [], checks: [], mode: 'observe', mode_label: '观察', rule_version: 'sched-1', reminders: [
      {id: 'rule|NVDA|r1#1', kind: 'price', symbol: 'NVDA', body: 'NVDA 高于 190.00，现价 190.62（北京 11-13 22:40）', status: 'sent'},
      {id: 'decision|NVDA', kind: 'decision', symbol: 'NVDA', body: 'NVDA 的决定从 持有 变为 买入。', status: 'sent'}]},
  });
  try {
    await ui.click('[data-page=watchlist]');
    assert.match(ui.doc.querySelector('.wl-empty').textContent, /^还没有自选在上面输入代码加入/);
    await ui.click('[data-page=schedule]');
    const rows = [...ui.doc.querySelectorAll('.schedule-reminders li')];
    assert.equal(rows[0].querySelector('.wl-tag').textContent, '自选');
    assert.match(rows[0].textContent, /^自选NVDA 高于 190\.00，现价 190\.62（北京 11-13 22:40）/);
    assert.equal(rows[1].querySelector('.wl-tag'), null);
  } finally { ui.dom.window.close(); }
});

// ---- V2-WATCHLIST W5: auto refresh, rate limits, alert toast and the 自选 badge.
function wlLatest(price, extra = {}) {
  return {bar: {close: price, time: 1794528000}, prev_close: price * 0.98, trading_day: '2026-11-13', fetched_at: 1794580000, delayed: true, ...extra};
}

test('V2-WATCHLIST R10/R7: one request at a time, stop at the first 429, back off 2/4/8/15 minutes', async () => {
  const asked = [];
  let inFlight = 0;
  let most = 0;
  const posted = [];
  const ui = await setup({
    '/api/watchlist': wlData(),
    '/api/watchlist/refresh': (options) => { posted.push(JSON.parse(options.body)); return wlData(); },
    '/api/kline/latest': async (options, path) => {
      const symbol = new URL(path, 'http://x').searchParams.get('symbol');
      asked.push(symbol);
      inFlight += 1; most = Math.max(most, inFlight);
      await new Promise((done) => setImmediate(done));
      inFlight -= 1;
      if (symbol === 'BTC-USDT') return {__status: 429, body: {error: '行情源限流，稍后重试。', code: 'rate_limited'}};
      return wlLatest(190.62);
    },
  });
  try {
    await ui.click('[data-page=watchlist]');
    await ui.click('#watch-refresh');
    for (let i = 0; i < 20; i++) await settle();
    assert.deepEqual(asked, ['NVDA', 'BTC-USDT']);
    assert.equal(most, 1);
    assert.deepEqual(Object.keys(posted[0].quotes), ['NVDA']);
    assert.deepEqual(posted[0].quotes.NVDA, {price: 190.62, prev_close: 190.62 * 0.98, trading_day: '2026-11-13', fetched_at: 1794580000, delayed: true});
    const rows = [...ui.doc.querySelectorAll('.watch-table tbody tr')];
    for (const row of rows.slice(1)) {
      assert.match(row.querySelector('.wl-meta').textContent, /限流，稍后重试$/);
      assert.equal(row.querySelector('.wl-tag').textContent, '旧价');
    }
    const status = ui.doc.querySelector('.wl-status');
    assert.ok(status.classList.contains('wl-status--warn'));
    assert.match(status.textContent, /已暂停：行情源限流/);
    assert.match(ui.doc.querySelector('#watch-error').textContent, /BTC-USDT、XAUUSD、600519 这次没拿到最新价：行情源限流。表里是上次的价格，不拿旧价判断规则。/);
    const poll = ui.ui.state.watchPoll;
    const waits = [];
    waits.push(Math.round((poll.retryAt - poll.lastAt) / 60000));
    for (let i = 0; i < 4; i++) {
      await ui.click('#watch-refresh');
      for (let k = 0; k < 20; k++) await settle();
      waits.push(Math.round((poll.retryAt - poll.lastAt) / 60000));
    }
    assert.deepEqual(waits, [2, 4, 8, 15, 15]);
    assert.match(status.ownerDocument.querySelector('.wl-status').textContent, /\d\d:\d\d 自动重试/);
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST R10: at most 30 symbols per refresh', async () => {
  const many = Array.from({length: 35}, (_, i) => ({symbol: `S${i}`, asset_label: '股票', asset_type: 'stock', market: 'us', enabled: true, last_price: 10, rules: []}));
  const asked = [];
  const ui = await setup({
    '/api/watchlist': wlData({items: many}),
    '/api/watchlist/refresh': () => wlData({items: many}),
    '/api/kline/latest': (options, path) => { asked.push(path); return wlLatest(11); },
  });
  try {
    await ui.click('[data-page=watchlist]');
    await ui.click('#watch-refresh');
    for (let i = 0; i < 80; i++) await settle();
    assert.equal(asked.length, 30);
    assert.match(ui.doc.querySelector('#watch-error').textContent, /自选超过 30 个，这次只刷新前 30 个。/);
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST: a refresh with hits shows one 「!」 toast; the 自选 badge counts unseen hits and clears on the page', async () => {
  const fired = [{id: 'a', symbol: 'NVDA', label: '高于 190.00', text: 'NVDA 高于 190.00'}, {id: 'b', symbol: 'BTC-USDT', label: '低于 90000.0', text: 'BTC-USDT 低于 90000.0'}];
  let unseen = 0;
  const seen = [];
  const ui = await setup({
    '/api/watchlist': () => wlData({unseen_count: unseen}),
    '/api/watchlist/refresh': () => { unseen = 2; return wlData({fired, unseen_count: 2}); },
    '/api/watchlist/seen': () => { seen.push(1); unseen = 0; return wlData({unseen_count: 0}); },
    '/api/kline/latest': () => wlLatest(190.62),
  });
  try {
    await ui.click('[data-page=watchlist]');
    ui.ui.state.page = 'runs';  // a hit while another page is open
    await ui.window.eval('runWatchRefresh({manual: false})');
    for (let i = 0; i < 20; i++) await settle();
    const toast = ui.doc.querySelector('#toast-host .toast');
    assert.ok(toast.classList.contains('toast--alert'));
    assert.equal(toast.querySelector('.ck').textContent, '!');
    assert.equal(toast.textContent.replace('!', ''), '2 条提醒：NVDA 高于 190.00，BTC-USDT 低于 90000.0');
    const badge = ui.doc.querySelector('.nav-item[data-page="watchlist"] .nav-badge');
    assert.equal(badge.textContent, '2');
    assert.equal(badge.getAttribute('aria-label'), '2 条新提醒');
    await ui.click('[data-page=watchlist]');
    assert.equal(seen.length, 1);
    assert.equal(ui.doc.querySelector('.nav-item[data-page="watchlist"] .nav-badge'), null);
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST R2: the status line and choice follow rules; the choice is saved; hidden pauses', async () => {
  const polls = [];
  let data = wlData({poll_minutes: 5});
  const ui = await setup({
    '/api/watchlist': () => data,
    '/api/watchlist/poll': (options) => { const body = JSON.parse(options.body); polls.push(body.minutes); data = wlData({poll_minutes: body.minutes, poll_stored: true}); return data; },
  });
  try {
    await ui.click('[data-page=watchlist]');
    const on = () => ui.doc.querySelector('#watch-poll .seg-item.on').textContent;
    assert.equal(on(), '5 分钟');
    // 下次 comes from the real clock while date labels compare with the pinned day (W-WL1), so it may carry a date here.
    assert.match(ui.doc.querySelector('.wl-status').textContent, /^自动刷新中 · 每 5 分钟上次 北京 22:40:05下次 (\d\d-\d\d )?\d\d:\d\d$/);
    assert.match(ui.doc.querySelector('#watch-poll-hint').textContent, /只在控制台页面开着时刷新/);
    await ui.click('[data-poll="0"]');
    assert.deepEqual(polls, [0]);
    assert.equal(on(), '关');
    assert.match(ui.doc.querySelector('.wl-status').textContent, /^自动刷新已关/);
    assert.equal(ui.ui.state.watchPoll.timer, null);
    await ui.click('[data-poll="1"]');
    assert.ok(ui.ui.state.watchPoll.timer);
    Object.defineProperty(ui.doc, 'visibilityState', {value: 'hidden', configurable: true});
    ui.doc.dispatchEvent(new ui.window.Event('visibilitychange'));
    assert.match(ui.doc.querySelector('.wl-status').textContent, /已暂停：页面在后台.*回到页面后马上刷新一次/);
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST: no enabled rules says auto refresh only updates prices; no symbols disables everything', async () => {
  const ui = await setup({'/api/watchlist': wlData({has_rules: false, poll_minutes: 0, items: wlItems().map((item) => ({...item, rules: []}))})});
  try {
    await ui.click('[data-page=watchlist]');
    assert.equal(ui.doc.querySelector('#watch-poll-hint').textContent, '还没有启用的规则。打开自动刷新也只会更新价格，不会提醒。');
    assert.equal(ui.doc.querySelector('#watch-poll .seg-item.on').textContent, '关');
  } finally { ui.dom.window.close(); }
  const empty = await setup({'/api/watchlist': wlData({items: [], has_rules: false, poll_minutes: 0})});
  try {
    await empty.click('[data-page=watchlist]');
    assert.equal(empty.doc.querySelector('.wl-status').textContent, '还没有自选，不会刷新。');
    assert.ok([...empty.doc.querySelectorAll('#watch-poll .seg-item')].every((node) => node.disabled));
    assert.equal(empty.doc.querySelector('#watch-refresh').disabled, true);
  } finally { empty.dom.window.close(); }
});

test('V2-WATCHLIST-POLISH W261: a broken watchlist.json shows its Chinese error, no table, and no action that writes', async () => {
  const sentence = '自选数据文件 watchlist.json 已损坏，没有按空列表读取，也不会写入。原文件已备份为 watchlist.json.corrupt-20261114-093000。请修复或移走 watchlist.json 后刷新页面。';
  const broken = {__status: 409, body: {error: sentence, code: 'watchlist_corrupt'}};
  const ui = await setup({'/api/watchlist': broken});
  try {
    await ui.click('[data-page=watchlist]');
    const banner = ui.doc.querySelector('#watch-error');
    assert.equal(banner.hidden, false);
    assert.equal(banner.textContent, sentence);
    assert.equal(ui.doc.querySelector('.watch-table'), null);
    assert.match(ui.doc.querySelector('#watch-broken').textContent, /自选数据暂时不能用没有把它当成空列表，也不会写入。/);
    assert.doesNotMatch(ui.doc.querySelector('#watch-table').textContent, /还没有自选/);
    assert.ok([...ui.doc.querySelectorAll('#watch-form input, #watch-form button')].every((node) => node.disabled));
    assert.equal(ui.doc.querySelector('#watch-refresh').disabled, true);
    assert.match(ui.doc.querySelector('#page').textContent, /已停止：自选数据文件损坏/);
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST-POLISH W261: a write that finds the file broken drops the stale table instead of keeping it', async () => {
  const sentence = '自选数据文件 watchlist.json 已损坏，没有按空列表读取，也不会写入。原文件已备份为 watchlist.json.corrupt-20261114-093000。请修复或移走 watchlist.json 后刷新页面。';
  const ui = await setup({'/api/watchlist': (options) => (options.method === 'POST' ? {__status: 409, body: {error: sentence, code: 'watchlist_corrupt'}} : wlData()), '/api/watchlist/seen': wlData()});
  try {
    await ui.click('[data-page=watchlist]');
    assert.equal(ui.doc.querySelectorAll('.watch-table tbody tr').length, 4);
    ui.doc.querySelector('#watch-symbol').value = 'TSLA';
    ui.doc.querySelector('#watch-form').dispatchEvent(new ui.window.Event('submit', {cancelable: true}));
    for (let i = 0; i < 8; i++) await settle();
    assert.equal(ui.doc.querySelector('#watch-error').textContent, sentence);
    assert.equal(ui.doc.querySelector('.watch-table'), null);
    assert.ok(ui.doc.querySelector('#watch-broken'));
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST-POLISH W262: opening 自选 marks seen up to the highest trigger number the page shows, not by time', async () => {
  const at = '2026-11-13T14:40:05+00:00';
  const trig = (seq, unseen) => ({id: `rule|NVDA|r${seq}#1`, seq, symbol: 'NVDA', kind: 'rule', text: '高于 1.00 · 现价 5.00', created_at: at, unseen, started: false});
  const posted = [];
  const ui = await setup({
    '/api/watchlist': wlData({triggers: [trig(7, false), trig(8, true), trig(9, true)], visible_triggers: [trig(9, true), trig(8, true), trig(7, false)], unseen_count: 2, trigger_total: 3}),
    '/api/watchlist/seen': (options) => { posted.push(JSON.parse(options.body)); return wlData({unseen_count: 0}); },
  });
  try {
    ui.ui.state.watchlist = null;
    await ui.click('[data-page=watchlist]');
    for (let i = 0; i < 8; i++) await settle();
    assert.deepEqual(posted, [{upto_seq: 9}]);
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST-POLISH W259/W260: the drawer refuses a price over 1e12, previews the typed digits, and an edit sends the rule type', async () => {
  const posts = [];
  const data = wlData();
  const ui = await setup({
    '/api/watchlist': () => data,
    '/api/watchlist/seen': () => data,
    '/api/watchlist/rules': (options) => { posts.push(JSON.parse(options.body)); return data; },
  });
  try {
    await ui.click('[data-page=watchlist]');
    await ui.click('[data-watch-rules="NVDA"]');
    const drawer = () => ui.doc.querySelector('.rule-drawer');
    await ui.click('[data-rule-edit="r1"]');
    ui.fill('#rule-edit-value', '2000000000000');  // commit 8: 2e12 is now 「只能填半角数字」
    await ui.click('#rule-edit-save');
    assert.equal(drawer().querySelector('.rule-item--editing .rule-err').textContent, '价格不能超过 1 万亿（1e12）。');
    assert.equal(posts.length, 0);
    ui.fill('#rule-edit-value', '191.5');
    await ui.click('#rule-edit-save');
    assert.deepEqual(posts.at(-1), {op: 'update', rule_id: 'r1', kind: 'above', value: '191.5', symbol: 'NVDA'});
    await ui.click('[data-rule-kinds="add"] [data-rule-kind="pct"]');
    ui.fill('#rule-add-value', '3.14159');
    assert.equal(drawer().querySelector('#rule-preview').textContent, '较昨收涨或跌 3.14159% 及以上时提醒。同一方向每个交易日只提醒一次。');
    await ui.click('[data-rule-kinds="add"] [data-rule-kind="above"]');
    ui.fill('#rule-add-value', '190.123456');
    assert.equal(drawer().querySelector('#rule-preview').textContent, '价格涨到 190.123456 或以上时提醒一次。回到 190.123456 以下后重新生效。');
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST-POLISH W-WL1: a last refresh or fetch time from another Beijing day carries its date', async () => {
  const ui = await setup({'/api/watchlist': wlData(), '/api/watchlist/seen': wlData()});
  try {
    assert.equal(ui.ui.watchStamp('2026-11-13T14:35:04+00:00', true), '22:35:04');  // same Beijing day as the pinned now
    assert.equal(ui.ui.watchStamp('2026-11-13T15:59:59+00:00'), '23:59');
    assert.equal(ui.ui.watchStamp('2026-11-13T16:00:00+00:00'), '11-14 00:00');    // Beijing midnight, not UTC
    ui.window.eval("watchNowMs = () => Date.parse('2026-11-16T00:50:05Z')");      // Monday 08:50 Beijing, before the open
    await ui.click('[data-page=watchlist]');
    assert.match(ui.doc.querySelector('.wl-status').textContent, /上次 北京 11-13 22:40:05/);
    const rows = [...ui.doc.querySelectorAll('.watch-table tbody tr')];
    assert.equal(rows[1].querySelector('.wl-meta').textContent, '较昨收（北京 08:00） · 取价 北京 11-13 22:35');
    assert.doesNotMatch(ui.doc.querySelector('#page').textContent, /UTC/);
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST-POLISH W-WL2: triggers from the same second follow the table order, in the short and the full list', async () => {
  const at = '2026-11-13T14:40:05+00:00';
  const trig = (seq, symbol, created_at = at) => ({id: `rule|${symbol}|r${seq}#1`, seq, symbol, kind: 'rule', text: `${symbol} 命中`, created_at, checked: false, status_label: '未检查', unseen: true});
  const triggers = [trig(1, '600519', '2026-11-13T14:00:00+00:00'), trig(2, 'BTC-USDT'), trig(3, 'XAUUSD'), trig(4, 'NVDA'), trig(5, 'NVDA')];
  const order = () => [...ui.doc.querySelectorAll('.watch-triggers li .watch-symbol')].map((node) => node.textContent);
  // without visible_triggers (an older server) the page orders the short list itself
  const ui = await setup({'/api/watchlist': wlData({triggers, visible_triggers: undefined, trigger_total: 5}), '/api/watchlist/seen': wlData()});
  try {
    await ui.click('[data-page=watchlist]');
    assert.deepEqual(order(), ['NVDA', 'NVDA', 'BTC-USDT', 'XAUUSD', '600519']);
    assert.deepEqual([...ui.doc.querySelectorAll('.watch-triggers li')].slice(0, 2).map((li) => li.querySelector('.watch-change').textContent), ['NVDA 命中', 'NVDA 命中']);
    ui.ui.state.watchlist.triggers = triggers.concat(Array.from({length: 20}, (_, i) => trig(10 + i, 'NVDA', `2026-11-13T15:${String(i).padStart(2, '0')}:00+00:00`)));
    ui.ui.state.watchlist.trigger_total = 25;
    ui.ui.state.watchAllTriggers = true;
    ui.window.eval('paintWatchTable()');
    assert.deepEqual(order().slice(-5), ['NVDA', 'NVDA', 'BTC-USDT', 'XAUUSD', '600519']);  // the full list too
    const rows = ui.ui.watchTableOrder(triggers.slice().reverse(), wlItems());
    assert.deepEqual(Array.from(rows, (row) => row.seq), [4, 5, 2, 3, 1]);  // same symbol: the order recorded
    assert.deepEqual(Array.from(ui.ui.watchTableOrder(triggers.slice().reverse(), []), (row) => row.seq), [2, 3, 4, 5, 1]);
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST-POLISH W-WL3: at phone width every trigger row is the same grid: text, time on its own line, status at the top right', () => {
  const css = readFileSync(resolve(root, 'webui/static/app.css'), 'utf8');
  const block = css.match(/W-WL3[\s\S]*?@media \(max-width: 700px\) \{([\s\S]*?)\n\}/);
  assert.ok(block, 'the W-WL3 phone block exists');
  assert.match(block[1], /\.watch-triggers li \{ display: grid; grid-template-columns: minmax\(0, 1fr\) auto;/);
  assert.match(block[1], /\.watch-trigger-main \.watch-change \{ white-space: normal;/);
  assert.match(block[1], /\.watch-trigger-main \.watch-when \{ display: block;/);
  assert.match(block[1], /\.watch-triggers li > \.status-badge \{ justify-self: end;/);
});

test('V2-WATCHLIST-POLISH W259 ruling: labels keep the typed digits; more than 6 decimals is refused before any request', async () => {
  const posts = [];
  const data = wlData();
  const ui = await setup({
    '/api/watchlist': () => data,
    '/api/watchlist/seen': () => data,
    '/api/watchlist/rules': (options) => { posts.push(JSON.parse(options.body)); return data; },
  });
  try {
    assert.equal(ui.ui.watchTypedText(' 0190.50 '), '190.50');
    assert.equal(ui.ui.watchTypedText('.5'), '0.5');
    assert.equal(ui.ui.watchTypedText('1e3'), '1000');
    assert.equal(ui.ui.watchTypedText('abc'), null);
    assert.equal(ui.ui.watchTypedText('-5'), null);
    assert.deepEqual(['1452.35', '1452.30', '190', '0.5', '0.00001', '90000'].map((t) => ui.ui.watchThresholdText(t)),
      ['1452.35', '1452.30', '190.00', '0.5000', '0.00001000', '90000.0']);
    await ui.click('[data-page=watchlist]');
    await ui.click('[data-watch-rules="NVDA"]');
    const drawer = () => ui.doc.querySelector('.rule-drawer');
    await ui.click('[data-rule-kinds="add"] [data-rule-kind="below"]');
    ui.fill('#rule-add-value', '1452.30');
    assert.equal(drawer().querySelector('#rule-preview').textContent, '价格跌到 1452.30 或以下时提醒一次。回到 1452.30 以上后重新生效。');
    ui.fill('#rule-add-value', '1452.1234567');
    await ui.click('#rule-add');
    assert.equal(drawer().querySelector('.rule-err').textContent, '小数最多 6 位。');
    assert.equal(posts.length, 0);
    await ui.click('[data-rule-edit="r1"]');
    ui.fill('#rule-edit-value', '0.1234567');
    await ui.click('#rule-edit-save');
    assert.equal(drawer().querySelector('.rule-item--editing .rule-err').textContent, '小数最多 6 位。');
    assert.equal(posts.length, 0);
    ui.fill('#rule-edit-value', '1452.30');
    await ui.click('#rule-edit-save');
    assert.equal(posts.at(-1).value, '1452.30');
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST-POLISH commit 7: rule add and edit send the typed text as a string, never Number(value)', async () => {
  const posts = [];
  const data = wlData();
  const ui = await setup({
    '/api/watchlist': () => data,
    '/api/watchlist/seen': () => data,
    '/api/watchlist/rules': (options) => { posts.push(JSON.parse(options.body)); return data; },
  });
  try {
    await ui.click('[data-page=watchlist]');
    await ui.click('[data-watch-rules="NVDA"]');
    await ui.click('[data-rule-kinds="add"] [data-rule-kind="below"]');
    ui.fill('#rule-add-value', '1452.30');
    await ui.click('#rule-add');
    await ui.click('[data-rule-kinds="add"] [data-rule-kind="above"]');
    ui.fill('#rule-add-value', '0190.50');
    await ui.click('#rule-add');
    await ui.click('[data-rule-kinds="add"] [data-rule-kind="pct"]');
    ui.fill('#rule-add-value', ' 2.50 ');
    await ui.click('#rule-add');
    await ui.click('[data-rule-edit="r1"]');
    ui.fill('#rule-edit-value', '190.10');
    await ui.click('#rule-edit-save');
    assert.deepEqual(posts.map((body) => [body.op, body.kind, body.value]),
      [['add', 'below', '1452.30'], ['add', 'above', '0190.50'], ['add', 'pct', '2.50'], ['update', 'above', '190.10']]);
    assert.ok(posts.every((body) => typeof body.value === 'string'));
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST-POLISH commit 8: the drawer refuses exactly what the server refuses, with the same sentence', async () => {
  const inputs = ['1452.30', ' +1452.30 ', '0190.50', '１２３', '１４５２.３０', '1452.３', '٣.٥', '१२३', '1_000', '1e-7', '1E3',
    '1e309', '.5', '5.', '1.2.3', '0x10', '1,000', '++5', '', 'abc', 'inf', '-5', '0', '0.000000', '1452.1234567',
    '1000000000000', '1000000000000.000000', '1000000000000.000001', '01000000000000.1', '0.000001', '50', '50.000001',
    '0.1', '0.099999', '\u30001452.3\u3000'];
  const kinds = ['above', 'below', 'pct', 'win'];
  const server = JSON.parse(execFileSync(process.env.TRADINGAGENTS_TEST_PYTHON || 'python3', ['-c', `
import json, sys
from webui import watchlist
out = {}
for kind in ${JSON.stringify(kinds)}:
    for raw in json.loads(sys.argv[1]):
        try:
            watchlist._rule_value(kind, raw)
            out[kind + "|" + raw] = ""
        except watchlist.WatchlistError as exc:
            out[kind + "|" + raw] = str(exc)
print(json.dumps(out, ensure_ascii=False))
`, JSON.stringify(inputs)], {cwd: root, encoding: 'utf8'}));
  const ui = await setup({'/api/watchlist': wlData(), '/api/watchlist/seen': wlData()});
  try {
    for (const kind of kinds) {
      for (const raw of inputs) {
        assert.equal(ui.ui.ruleValueError(kind, raw), server[`${kind}|${raw}`], `${kind} ${JSON.stringify(raw)}`);
      }
    }
    assert.equal(ui.ui.ruleValueError('above', '１４５２.３０'), '只能填半角数字和小数点，例如 1452.30。');
    assert.equal(ui.ui.rulePreview('below', '1e-7'), '');  // no 「1e-7.0」 preview
    assert.equal(ui.ui.ruleValueError('above', '1000000000000.000001'), '价格不能超过 1 万亿（1e12）。');
  } finally { ui.dom.window.close(); }
});

test('V2-WATCHLIST-POLISH commit 8: a full-width or exponent number is refused in the drawer before any request', async () => {
  const posts = [];
  const data = wlData();
  const ui = await setup({
    '/api/watchlist': () => data,
    '/api/watchlist/seen': () => data,
    '/api/watchlist/rules': (options) => { posts.push(JSON.parse(options.body)); return data; },
  });
  try {
    await ui.click('[data-page=watchlist]');
    await ui.click('[data-watch-rules="NVDA"]');
    const drawer = () => ui.doc.querySelector('.rule-drawer');
    await ui.click('[data-rule-kinds="add"] [data-rule-kind="below"]');
    ui.fill('#rule-add-value', '1e-7');
    assert.equal(drawer().querySelector('#rule-preview'), null);
    await ui.click('#rule-add');
    assert.equal(drawer().querySelector('.rule-err').textContent, '只能填半角数字和小数点，例如 1452.30。');
    ui.fill('#rule-add-value', '１４５２.３０');
    await ui.click('#rule-add');
    assert.equal(drawer().querySelector('.rule-err').textContent, '只能填半角数字和小数点，例如 1452.30。');
    await ui.click('[data-rule-edit="r1"]');
    ui.fill('#rule-edit-value', '1000000000000.000001');
    await ui.click('#rule-edit-save');
    assert.equal(drawer().querySelector('.rule-item--editing .rule-err').textContent, '价格不能超过 1 万亿（1e12）。');
    assert.equal(posts.length, 0);
  } finally { ui.dom.window.close(); }
});
