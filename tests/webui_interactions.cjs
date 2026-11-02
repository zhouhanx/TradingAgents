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
    '/api/config': {saved: {}, keys: [{provider: 'openai', configured: false, env_var: 'OPENAI_API_KEY'}], data_env: [], cloud_env: []},
    '/api/updates': {}, '/api/auth/login': {status: 'idle', messages: []},
    '/api/runs': {run: null, progress: {}, result: null},
    '/api/reports': {reports: []}, '/api/backtests': {sweeps: []},
    '/api/frameworks': FRAMEWORKS,
        ...overrides,
  };
  window.fetch = async (path, options = {}) => {
    calls.push({path, ...options});
    let value = routes[path] ?? routes[path.split('?')[0]];
    if (path.startsWith('/api/models?')) value = {models: [{id: 'fixture-model', zh: 'Fixture Model'}]};
    if (path.startsWith('/api/asset?')) value = {asset_type: 'stock', analysts: ['market', 'social', 'news', 'fundamentals']};
    if (typeof value === 'function') value = await value(options, path);
    if (value === undefined) throw new Error(`Unexpected fixture request: ${path}`);
    if (value && value.__status) return {ok: false, status: value.__status, json: async () => structuredClone(value.body)};
    return {ok: true, json: async () => structuredClone(value)};
  };
  window.eval(`${script}\nwindow.uiTest = {state, showPage, openRunDetail, closeRunDetail, readForm, renderAnalyze, renderRuns, renderAccounts, renderReports, submitAnalysis, resultPanel, renderMarkdown, openSaveDrawer, markScrollableTables, paintHolding, paintReview};`);
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
    const pages = ['analyze', 'runs', 'reports', 'backtest', 'accounts', 'models'];
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
    assert.match(ui.doc.querySelector('#backtest-result').textContent, /4 次分析/);
    assert.equal(ui.calls.filter((item) => item.path === '/api/backtests' && item.method === 'POST').length, 0);
  } finally { ui.dom.window.close(); }
});

test('status empty state and task errors are understandable; technical details start collapsed', async () => {
  const ui = await setup();
  try {
    await ui.click('[data-page=runs]');
    assert.match(ui.visibleText(), /还没有分析任务/);
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
    ? {id: '20261031_141327_59ee0ec2', ticker: 'BTC-USD', date: '2026-10-30', started_at: '2026-10-31T14:13:27', selections: {asset_type: 'crypto'}, ignored: []}
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
    assert.match(ui.visibleText(), /还没有分析任务/);
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
   ticker: 'NVDA', date: '2026-10-29', rating: 'Overweight', generated: '2026-10-29 21:12:00', deep_model: 'gpt-6-sol', quick_model: 'grok-4.7', debate_rounds: 3},
  {name: 'AAPL_20261028_100000', html: `${REPORT_DIR}/AAPL_20261028_100000/complete_report.html`, markdown: null,
   ticker: 'AAPL', date: '2026-10-28', rating: 'Sell', generated: '2026-10-28 10:00:00', debate_rounds: 1},
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

test('several runs show a filtered list with real counts and no detail until a row is clicked; the nav badge counts running (RUN-004, WEBUI-007)', async () => {
  const ui = await multiSetup();
  try {
    const {doc} = ui;
    await ui.click('.nav-item[data-page=runs]');
    await settleMany();
    assert.equal(doc.querySelector('.page-description').textContent, '2 个运行中 · 2 个排队中（最多同时运行 2 个，最多排队 5 个）');
    assert.deepEqual([...doc.querySelectorAll('#run-filters .ftab')].map((tab) => tab.textContent), ['全部6', '运行中2', '排队中2', '失败1', '已完成1']);
    const rows = [...doc.querySelectorAll('#run-list .tr[data-run-id]')];
    assert.deepEqual(rows.map((row) => row.dataset.runId), ['run-btc', 'run-nvda', 'run-eth', 'run-sol', 'run-tsla', 'run-aapl']);
    assert.equal(rows.filter((row) => row.classList.contains('on')).length, 0, 'nothing is highlighted before a run was opened');
    assert.match(rows[2].textContent, /排队中/);
    assert.match(rows[2].textContent, /0\/11/);
    assert.match(rows[2].textContent, /14:08 提交/);
    assert.ok(rows[2].querySelector('.qx'), 'a queued row has the red 取消');
    assert.equal(rows[0].querySelector('.qx'), null);
    assert.match(rows[5].textContent, /已完成/);
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
    assert.match(doc.querySelector('#runs-empty').textContent, /还没有分析任务/);
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
    assert.match(doc.querySelector('#run-detail-head .sub').textContent, /^TradingAgents · /, 'the detail subtitle starts with the framework name');
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
