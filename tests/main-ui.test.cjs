// Exercise the actual screen renderer, admission actions and timers with HTTP/DOM doubles.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() {
    this.value = ''; this.hidden = false; this.disabled = false; this.checked = false;
    this.className = ''; this.children = []; this.listeners = {}; this._text = '';
    this.classList = { contains: name => this.className.split(' ').includes(name) };
  }
  get textContent() { return this._text; }
  set textContent(value) { this._text = value; this.children = []; }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() {
    return this.parentNode?.children[this.parentNode.children.indexOf(this) + 1] || null;
  }
  appendChild(node) { return this.insertBefore(node, null); }
  insertBefore(node, next) {
    if (node.parentNode) node.parentNode.removeChild(node);
    const index = next ? this.children.indexOf(next) : this.children.length;
    this.children.splice(index, 0, node); node.parentNode = this; return node;
  }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); node.parentNode = null; }
  remove() { this.parentNode?.removeChild(this); }
  querySelector(selector) {
    return this.children.find(node => selector.startsWith('.') && node.classList.contains(selector.slice(1))) || null;
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}

function environment() {
  const ids = [...fs.readFileSync(require.resolve('../screen.html'), 'utf8').matchAll(/id="([^"]+)"/g)]
    .map(match => match[1]);
  const nodes = Object.fromEntries([...ids, 'plugin-minus_mix'].map(id => [id, new Element()]));
  const singleStems = [{ value: 'guitar', checked: true }];
  const batchStems = [{ value: 'guitar', checked: true }];
  const events = {}, timers = new Map(), calls = [], notifications = [];
  let nextTimer = 0;
  const payloads = {
    '/status': { ffmpeg_available: true, separation: { ready: false, state: 'unavailable', reason: 'Install the server.' } },
    '/export/latest': { job: null }, '/batch/latest': { job: null },
  };
  const document = {
    hidden: false,
    getElementById: id => nodes[id], createElement: () => new Element(),
    querySelectorAll(selector) {
      const stems = selector.startsWith('#pmx-batch-stems') ? batchStems
        : selector.startsWith('#pmx-stems') ? singleStems : [];
      return selector.endsWith(':checked') ? stems.filter(stem => stem.checked) : stems;
    },
    querySelector: () => ({ value: 'preserve' }),
    addEventListener: (event, callback) => { events[event] = callback; },
  };
  const window = { document, feedBack: { on: (event, callback) => { events[event] = callback; } },
    fbNotify: { show: notification => notifications.push(notification) } };
  const context = {
    window, document, console,
    localStorage: { getItem: () => null, setItem() {} },
    setTimeout(callback, delay) { timers.set(++nextTimer, { callback, delay }); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
    fetch: async (url, options) => {
      const path = url.replace('/api/plugins/minus_mix', '');
      calls.push({ path, options });
      assert(Object.hasOwn(payloads, path), 'Unexpected request: ' + path);
      const payload = typeof payloads[path] === 'function' ? await payloads[path](options) : payloads[path];
      return { ok: true, json: async () => structuredClone(payload) };
    },
  };
  // Capture private functions in the test copy only; shipping code has no test hooks.
  let source = fs.readFileSync(require.resolve('../screen.js'), 'utf8');
  const marker = '  if (fb && fb.on) boot();';
  assert(source.includes(marker));
  source = source.replace(marker, `  window.testUI = { state, updateReady, updateBatchReady,
    renderSingleJob, renderBatchJob, refreshStatus, createExport, cancelSingleExport,
    startBatch, cancelBatch, batchOptionsKey, loadLatestBatch, loadLatestSingleExport,
    pollSingleExport, pollBatch };
${marker}`);
  vm.runInNewContext(source, context, { filename: 'screen.js' });
  const ui = window.testUI;
  ui.state.inited = true;
  ui.state.selectedFilename = 'original.feedpak';
  ui.state.sourceInfo = { stems: [{ id: 'guitar', requires_separation: true }] };
  nodes['pmx-output'].value = 'C:/output';
  nodes['pmx-batch-input'].value = 'C:/source'; nodes['pmx-batch-output'].value = 'C:/output';
  nodes['pmx-batch-panel'].hidden = true;
  nodes['plugin-minus_mix'].className = 'active';
  return { ui, nodes, singleStems, batchStems, events, timers, payloads, calls, notifications, document };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const node = (env, id) => env.nodes['pmx-' + id];
const waitingEngine = { ready: false, waitable: true, state: 'updating', reason: 'The server is updating.' };
const singleWaiting = { id: 'single-1', status: 'running', stage: 'waiting_for_server', progress: 0.42,
  detail: 'Reconnecting; the export will continue automatically.' };
const batchWaiting = {
  id: 'batch-1', status: 'running', stage: 'waiting_for_server', overall_progress: 0.48,
  detail: 'Waiting for the server update.', current_item_number: 2, current_relative_path: 'two.feedpak',
  items_total: 3, counts: { done: 1, running: 1, queued: 1, skipped: 0, failed: 0, blocked: 0 },
  items: [
    { relative_path: 'one.feedpak', status: 'done', stage: 'done' },
    { relative_path: 'two.feedpak', status: 'running', stage: 'waiting_for_server' },
    { relative_path: 'three.feedpak', status: 'queued', stage: 'queued' },
  ],
};
function setScan(env, required = ['guitar']) {
  env.ui.state.batchScan = {
    scan_id: 'scan-1', required_separation_stems: required,
    counts: { found: 3, ready: 3, needs_separation: required.length ? 2 : 0, uses_saved_stems: 1 },
    items: [], items_truncated: true,
  };
  env.ui.state.batchScanKey = env.ui.batchOptionsKey();
}

async function admissionChecks() {
  const env = environment();
  for (const engine of [
    { ready: true, reason: 'Legacy model loaded.' },
    { ready: true, state: 'on_demand', reason: 'Model loads when needed; not verified.' },
    waitingEngine,
    { ready: false, waitable: true, state: 'warming', reason: 'Loading model.' },
    { ready: false, waitable: true, state: 'reconnecting', reason: 'Reconnecting.' },
  ]) {
    env.ui.state.separation = engine; setScan(env);
    env.ui.updateReady(); env.ui.updateBatchReady();
    assert.equal(node(env, 'export').disabled, false, engine.state || 'legacy ready');
    assert.equal(node(env, 'batch-start').disabled, false, engine.state || 'legacy ready');
    if (engine.state === 'on_demand') {
      assert.match(node(env, 'engine-text').textContent, /loads when needed; not verified/);
      assert(!node(env, 'engine-text').textContent.includes('server ready'));
    }
    if (engine.waitable) assert.match(node(env, 'summary-detail').textContent, /continues automatically/);
  }
  for (const state of ['missing_model', 'unsupported_stems', 'incompatible', 'unavailable']) {
    env.ui.state.separation = { ready: false, waitable: false, state, reason: state + ' action' };
    env.ui.updateReady(); env.ui.updateBatchReady();
    assert.equal(node(env, 'export').disabled, true, state);
    assert.equal(node(env, 'batch-start').disabled, true, state);
    assert.match(node(env, 'summary-detail').textContent, new RegExp(state));
    env.ui.createExport(); env.ui.startBatch(); await tick();
    assert.equal(env.calls.length, 0, 'Hard denial cannot submit through a stale click');
  }
  env.ui.state.sourceInfo.stems[0].requires_separation = false;
  setScan(env, []); env.ui.updateReady(); env.ui.updateBatchReady();
  assert.equal(node(env, 'export').disabled, false, 'Saved stems are independent of server availability');
  assert.equal(node(env, 'batch-start').disabled, false);
  assert.match(node(env, 'engine-text').textContent, /not required/);
  assert.equal(env.calls.length, 0, 'Readiness decisions do not contact the separator');

  env.ui.state.sourceInfo.stems = [{ id: 'guitar', requires_separation: true },
    { id: 'piano', requires_separation: false }];
  env.singleStems.push({ value: 'piano', checked: true });
  env.batchStems.push({ value: 'piano', checked: true });
  env.ui.state.separation = { ready: true, model: 'four-stem', supported_stems: ['guitar', 'bass'] };
  setScan(env); env.ui.updateReady(); env.ui.updateBatchReady();
  assert.equal(node(env, 'export').disabled, false, 'An unsupported saved stem does not need the model');
  assert.equal(node(env, 'batch-start').disabled, false, 'Aggregate missing stems governs truncated scan');
  env.ui.state.sourceInfo.stems[1].requires_separation = true;
  setScan(env, ['guitar', 'piano']); env.ui.updateReady(); env.ui.updateBatchReady();
  assert.equal(node(env, 'export').disabled, true);
  assert.equal(node(env, 'batch-start').disabled, true);
  assert.match(node(env, 'summary-detail').textContent, /four-stem.*does not provide piano/);
}

async function singleChecks() {
  const env = environment(); env.ui.state.separation = waitingEngine;
  env.payloads['/export'] = singleWaiting;
  env.ui.createExport(); await tick();
  assert.equal(env.calls.filter(call => call.options?.method === 'POST').length, 1);
  assert.equal(env.ui.state.busy, true);
  assert.equal(node(env, 'single-cancel').disabled, false);
  assert.equal(node(env, 'single-progress-bar').value, 0.42);
  assert.match(node(env, 'summary-title').textContent, /Waiting for the stem server/);
  assert.match(node(env, 'status').textContent, /continue automatically/);
  env.ui.renderSingleJob({ ...singleWaiting, stage: 'separating', detail: 'Separation resumed.' });
  assert.match(node(env, 'status').textContent, /resumed/);
  assert(!node(env, 'summary-title').textContent.includes('Waiting'));
  env.ui.renderSingleJob(singleWaiting);
  env.payloads['/export/single-1/cancel'] = { ...singleWaiting, status: 'canceling', detail: 'Stopping safely.' };
  env.ui.cancelSingleExport(); await tick();
  assert.equal(node(env, 'single-cancel').disabled, true);
  assert.match(node(env, 'single-progress-title').textContent, /Canceling/);
  assert.match(node(env, 'summary-title').textContent, /Canceling/);
  assert(!node(env, 'status').textContent.includes('continue automatically'));
  env.ui.renderSingleJob({ ...singleWaiting, status: 'canceled', stage: 'canceled' });
  assert.equal(env.ui.state.busy, false);
  assert.equal(env.ui.state.singlePollTimer, null);
  assert.equal(env.calls.filter(call => call.path === '/export').length, 1, 'Cancel does not resubmit');
  env.ui.renderSingleJob({ ...singleWaiting, status: 'blocked', stage: 'blocked', detail: 'The model was removed.' });
  assert.match(node(env, 'single-progress-title').textContent, /stopped/);
  assert.equal(node(env, 'single-progress-bar').value, 0.42);
  assert.match(node(env, 'status').textContent, /model was removed/);
  assert(!env.notifications.some(item => item.title.includes('created')));
}

async function batchChecks() {
  const env = environment(); env.ui.state.separation = waitingEngine;
  setScan(env); env.payloads['/batch/start'] = batchWaiting;
  env.ui.startBatch(); await tick();
  assert.equal(env.ui.state.batchActive, true);
  assert.equal(node(env, 'batch-cancel').disabled, false);
  assert.equal(node(env, 'batch-start').disabled, true);
  assert.equal(node(env, 'batch-progress-bar').value, 0.48);
  assert.match(node(env, 'batch-summary-title').textContent, /Waiting for the stem server/);
  assert.match(node(env, 'batch-counts').innerHTML, /1 created.*1 queued.*1 waiting for server.*0 failed/);
  assert.match(env.ui.state.batchItemRows['two.feedpak']._minusMixNodes.status.textContent, /waiting for server/);
  env.ui.renderBatchJob({ ...batchWaiting, stage: 'separating', detail: 'Resumed.', items: batchWaiting.items.map(
    item => item.status === 'running' ? { ...item, stage: 'separating' } : item) });
  assert(!node(env, 'batch-status').textContent.includes('Waiting'));
  const blocked = { ...batchWaiting, status: 'blocked', stage: 'blocked', detail: 'Selected model is missing.',
    counts: { ...batchWaiting.counts, running: 0, blocked: 1 },
    items: batchWaiting.items.map(item => item.status === 'running' ? { ...item, status: 'blocked', stage: 'blocked' } : item) };
  env.ui.renderBatchJob(blocked);
  assert.equal(env.ui.state.batchActive, false);
  assert.equal(env.ui.state.batchPollTimer, null);
  assert.equal(node(env, 'batch-cancel').disabled, true);
  assert.equal(node(env, 'batch-scan').disabled, false, 'Blocked batches release scan/retry controls');
  assert.match(node(env, 'batch-progress-title').textContent, /stopped.*action needed/);
  assert.equal(node(env, 'batch-progress-bar').value, 0.48, 'A blocked batch must not show completed progress');
  assert.match(node(env, 'batch-counts').innerHTML, /1 created.*1 queued.*0 failed.*1 blocked/);
  assert.equal(env.ui.state.batchItemRows['three.feedpak']._minusMixNodes.status.textContent, 'queued');
  assert.match(node(env, 'batch-current').textContent, /two.feedpak.*Stopped/);
  assert.match(node(env, 'batch-summary-detail').textContent, /Scan again/);
  assert(!env.notifications.some(item => item.title.includes('completed')));
  env.payloads['/batch/latest'] = { job: blocked };
  node(env, 'batch-status').textContent = '';
  env.ui.loadLatestBatch(); await tick();
  assert.match(node(env, 'batch-status').textContent, /Selected model is missing/,
    'Restoring the last job renders its reason even when notification is suppressed');
  assert.equal(env.calls.filter(call => call.path === '/batch/start').length, 1);
  env.ui.renderBatchJob(batchWaiting);
  env.payloads['/batch/batch-1/cancel'] = { ...batchWaiting, status: 'canceling', detail: 'Stopping safely.' };
  env.ui.cancelBatch(); await tick();
  assert.match(node(env, 'batch-summary-title').textContent, /Canceling/);
  assert.equal(node(env, 'batch-cancel').disabled, true);
  assert(!node(env, 'batch-status').textContent.includes('continue automatically'));
  assert(!node(env, 'batch-counts').innerHTML.includes('waiting for server'));
  env.ui.renderBatchJob({ ...batchWaiting, status: 'canceled', stage: 'canceled' });
  assert.equal(env.ui.state.batchActive, false);
  assert.equal(node(env, 'batch-cancel').disabled, true);
  assert.equal(env.ui.state.batchPollTimer, null);
  assert.match(node(env, 'batch-progress-title').textContent, /canceled/);
}

async function refreshChecks() {
  const env = environment();
  await env.ui.refreshStatus();
  assert.equal(node(env, 'export').disabled, true);
  assert.equal(env.timers.get(env.ui.state.statusRetryTimer).delay, 3000);
  env.payloads['/status'].separation = { ready: true, state: 'on_demand', reason: 'Model loads when needed.' };
  const next = env.timers.get(env.ui.state.statusRetryTimer);
  await next.callback();
  assert.equal(node(env, 'export').disabled, false, 'Installing the server while the page is open enables export');
  assert.equal(env.timers.get(env.ui.state.statusRetryTimer).delay, 10000);
  assert.equal(env.calls.filter(call => call.options?.method === 'POST').length, 0, 'Refresh never starts an export');
  env.nodes['plugin-minus_mix'].className = '';
  env.events['screen:changed']({ detail: { id: 'library' } });
  assert.equal(env.ui.state.statusRetryTimer, null);
  env.nodes['plugin-minus_mix'].className = 'active';
  env.events['screen:changed']({ detail: { id: 'plugin-minus_mix' } });
  const resume = [...env.timers.values()].find(timer => timer.delay === 0);
  resume.callback(); await tick();
  assert(env.calls.some(call => call.path === '/export/latest'));
  assert(env.calls.some(call => call.path === '/batch/latest'));
  assert.equal(env.calls.filter(call => call.options?.method === 'POST').length, 0,
    'Navigating away and back restores status without creating duplicate jobs');
}

async function restoreRaceChecks() {
  for (const batch of [false, true]) {
    const env = environment(); env.ui.state.separation = waitingEngine; setScan(env);
    const latestPath = batch ? '/batch/latest' : '/export/latest';
    const startPath = batch ? '/batch/start' : '/export';
    const active = batch ? batchWaiting : singleWaiting;
    const latest = batch ? env.ui.loadLatestBatch : env.ui.loadLatestSingleExport;
    const start = batch ? env.ui.startBatch : env.ui.createExport;
    const idKey = batch ? 'batchJobId' : 'singleJobId';
    const activeKey = batch ? 'batchActive' : 'busy';
    const timerKey = batch ? 'batchPollTimer' : 'singlePollTimer';
    let releaseLatest;
    env.payloads[latestPath] = () => new Promise(resolve => { releaseLatest = resolve; });
    latest();
    env.payloads[startPath] = active;
    start(); await tick();
    releaseLatest({ job: { ...active, id: 'older-job', status: 'blocked', stage: 'blocked' } });
    await tick();
    assert.equal(env.ui.state[idKey], active.id, 'Old restore cannot replace an accepted new export');
    assert.equal(env.ui.state[activeKey], true);
    assert(env.ui.state[timerKey], 'The newly accepted job remains polled');

    const poll = batch ? env.ui.pollBatch : env.ui.pollSingleExport;
    const cancel = batch ? env.ui.cancelBatch : env.ui.cancelSingleExport;
    const statusPath = batch ? '/batch/' + active.id : '/export/' + active.id;
    let releasePoll;
    env.payloads[statusPath] = () => new Promise(resolve => { releasePoll = resolve; });
    poll();
    env.payloads[statusPath + '/cancel'] = { ...active, status: 'canceled', stage: 'canceled' };
    cancel(); await tick();
    assert.equal(env.ui.state[activeKey], false);
    releasePoll(active); await tick();
    assert.equal(env.ui.state[activeKey], false, 'A stale running snapshot cannot resurrect a canceled job');
    assert.equal(env.ui.state[timerKey], null);

    let releaseStart;
    setScan(env);
    env.payloads[startPath] = () => new Promise(resolve => { releaseStart = resolve; });
    start();
    const callsBeforeRestore = env.calls.length;
    latest();
    assert.equal(env.calls.length, callsBeforeRestore, 'Restore waits until a start request has an accepted job ID');
    releaseStart({ ...active, id: 'newest-job' }); await tick();
    assert.equal(env.ui.state[idKey], 'newest-job');
    assert.equal(env.calls.filter(call => call.path === startPath).length, 2, 'Only explicit starts create work');
  }

  const env = environment(); env.ui.renderBatchJob(batchWaiting);
  let releasePoll;
  env.payloads['/batch/batch-1'] = () => new Promise(resolve => { releasePoll = resolve; });
  env.ui.pollBatch();
  env.payloads['/batch/batch-1/cancel'] = { ...batchWaiting, status: 'canceling', detail: 'Stopping safely.' };
  env.ui.cancelBatch(); await tick();
  const firstTimer = env.ui.state.batchPollTimer;
  env.timers.get(firstTimer).callback();
  assert.notEqual(env.ui.state.batchPollTimer, firstTimer,
    'An older pending status request must not consume the cancellation heartbeat');
  releasePoll(batchWaiting); await tick();
  assert.equal(env.ui.state.batchStatus, 'canceling');
  env.payloads['/batch/batch-1'] = { ...batchWaiting, status: 'canceled', stage: 'canceled' };
  env.timers.get(env.ui.state.batchPollTimer).callback(); await tick();
  assert.equal(env.ui.state.batchActive, false);
  assert.equal(env.ui.state.batchPollTimer, null);
}

function completedRecoveryChecks() {
  const env = environment();
  env.ui.renderSingleJob(singleWaiting);
  env.ui.renderSingleJob({ ...singleWaiting, status: 'completed', stage: 'done', progress: 1,
    result: { filename: 'No Guitar.feedpak', path: 'C:/output/No Guitar.feedpak' } });
  assert.equal(node(env, 'single-progress-bar').value, 1);
  assert.match(node(env, 'single-progress-title').textContent, /created/);
  env.ui.renderBatchJob(batchWaiting);
  env.ui.renderBatchJob({ ...batchWaiting, status: 'completed', stage: 'done', overall_progress: 1,
    detail: 'All three exports completed.', counts: { done: 3, queued: 0, failed: 0, blocked: 0 },
    items: batchWaiting.items.map(item => ({ ...item, status: 'done', stage: 'done' })) });
  assert.equal(node(env, 'batch-progress-bar').value, 1);
  assert.match(node(env, 'batch-progress-title').textContent, /completed/);
  assert.match(node(env, 'batch-counts').innerHTML, /3 created.*0 queued.*0 failed/);
  assert.equal(env.notifications.filter(item => item.title.includes('completed')).length, 1);
}

(async () => {
  await admissionChecks(); await singleChecks(); await batchChecks(); await refreshChecks();
  await restoreRaceChecks(); completedRecoveryChecks();
  console.log('Main UI admission, waiting, cancellation, blocked queues, restore and refresh passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
