const assert = require('node:assert/strict');
const fs = require('node:fs');
const ui = require('../assets/reuse_screen.js');

class Node {
  constructor(tag = 'div') { this.tag = tag; this.attributes = {}; this.value = ''; this.childNodes = []; this.listeners = {}; this.disabled = false; }
  set value(value) { this._value = value; this.attributes.value = String(value); }
  get value() { return this._value; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; if (name === 'value') this._value = 0; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  appendChild(node) { this.childNodes.push(node); return node; }
  replaceChildren(...nodes) { this.childNodes = nodes; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  fire(name) { if (this.disabled) return; for (const callback of this.listeners[name] || []) callback.call(this, {}); }
  find(tag) { return this.childNodes.flatMap(node => [node, ...node.find('*')]).filter(node => tag === '*' || node.tag === tag); }
}

function environment(initial, responder, preferences = {}) {
  const ids = [...fs.readFileSync(require.resolve('../screen.html'), 'utf8').matchAll(/id="([^"]+)"/g)].map(match => match[1]);
  const nodes = Object.fromEntries(ids.map(id => [id, new Node()]));
  const document = { hidden: false, getElementById: id => nodes[id], createElement: tag => new Node(tag), addEventListener() {} };
  const calls = [], storage = new Map();
  ['old', 'fresh', 'output'].forEach(key => storage.set('minus_mix.reuse.' + key, 'C:/' + key));
  Object.entries(preferences).forEach(([key, value]) => storage.set('minus_mix.reuse.' + key, value));
  let job = structuredClone(initial);
  const controller = ui.mount({ document, storage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    request(path, options) {
      calls.push({ path, options });
      if (responder) { const value = responder(path, options, () => job, value => { job = value; }); if (value !== undefined) return value; }
      if (path.includes('/latest')) return Promise.resolve({ job: structuredClone(job) });
      if (path.endsWith('/choose')) { job = { ...job, groups: [], counts: { ...job.counts, ready: 1, review: 0 } }; }
      return Promise.resolve(structuredClone(job));
    }, pickDirectory: () => Promise.resolve('C:/changed') });
  return { controller, nodes, calls, storage, setJob(value) { job = value; } };
}
const node = (env, name) => env.nodes['pmx-reuse-' + name];
const tick = () => new Promise(resolve => setImmediate(resolve));
const reviewed = { id: 'job', status: 'ready', detail: 'Review proposed output', old_dir: 'C:/old', fresh_dir: 'C:/fresh', output_dir: 'C:/output',
  input_packages_total: 1, output_variants_total: 1,
  counts: { total: 1, ready: 1, review: 0 }, items: [{ relative_path: 'song.feedpak', status: 'ready', title: '<untrusted title>',
    excluded_stems: ['guitar'], variant_label: 'No Guitar' }], items_total: 1,
  groups: [], resources: {}, source_errors: [] };
const ambiguous = { ...reviewed, counts: { total: 1, ready: 0, review: 1 }, groups: [
  { id: 'group', title: 'Song', targets_count: 1, excluded_stems: ['guitar'], variant_label: 'No Guitar',
    candidates: [{ id: 'old/song.feedpak', relative_path: 'old/song.feedpak', full_sha256: 'a'.repeat(64) }] }] };
const phaseProgress = { phase: 'reading_existing', label: 'Reading existing mixes', processed: 0, total: 1,
  elapsed_seconds: 0, files_per_second: null, eta_seconds: null, fraction: 0, active: true };

async function layoutChecks() {
  assert.equal(ui.layoutSetting(undefined), 'preserve');
  assert.equal(ui.layoutSetting('preserve'), 'preserve');
  assert.equal(ui.layoutSetting('flat'), 'flat');
  assert.equal(ui.layoutSetting('invalid'), 'preserve');

  const fresh = environment(null);
  await fresh.controller.show();
  assert.equal(node(fresh, 'layout').value, 'preserve', 'Preserve folders is the initial default');
  assert.equal(node(fresh, 'layout-help').textContent,
    'Recreate subfolders from Current original packages inside the output folder.');
  node(fresh, 'layout').value = 'flat'; node(fresh, 'layout').fire('change');
  assert.equal(fresh.storage.get('minus_mix.reuse.output_layout'), 'flat');
  assert(node(fresh, 'layout-help').textContent.includes('directly in the output folder'));
  assert(fresh.calls.every(call => !call.options), 'Selecting a layout never starts work');
  fresh.controller.hide();

  const saved = environment(null, null, { output_layout: 'flat' });
  await saved.controller.show();
  assert.equal(node(saved, 'layout').value, 'flat', 'Saved layout is used without a reviewed job');
  saved.controller.hide();

  for (const [job, preference, expected] of [
    [reviewed, 'flat', 'preserve'],
    [{ ...reviewed, output_layout: 'flat' }, 'preserve', 'flat'],
  ]) {
    const restore = environment(job, null, { output_layout: preference });
    await restore.controller.show();
    assert.equal(node(restore, 'layout').value, expected,
      'The reviewed job layout takes precedence; older jobs used preserve');
    assert.equal(node(restore, 'apply').disabled, false);
    restore.controller.hide();
  }

  for (const status of ['ready', 'interrupted']) {
    const dirty = environment({ ...reviewed, status });
    await dirty.controller.show();
    node(dirty, 'layout').value = 'flat'; node(dirty, 'layout').fire('change');
    assert.equal(node(dirty, 'apply').disabled, true);
    assert.equal(node(dirty, 'resume').disabled, true);
    assert(node(dirty, 'detail').textContent.includes('Scan again'));
    await dirty.controller.refresh();
    assert.equal(node(dirty, 'layout').value, 'flat', 'A status refresh preserves an unsaved layout choice');
    node(dirty, 'apply').fire('click'); node(dirty, 'resume').fire('click'); await tick();
    assert(dirty.calls.every(call => !call.options), 'A new structure needs an explicit rescan before Apply or Resume');
    dirty.controller.hide();
  }

  const scanned = environment(reviewed, (path, options, getJob, setJob) => {
    if (path.endsWith('/scan')) {
      const payload = JSON.parse(options.body);
      assert.deepEqual(payload, { old_dir: 'C:/old', fresh_dir: 'C:/fresh', output_dir: 'C:/output',
        workers: 'auto', output_layout: 'flat' });
      const next = { ...getJob(), ...payload };
      setJob(next); return Promise.resolve(next);
    }
  });
  await scanned.controller.show();
  node(scanned, 'layout').value = 'flat'; node(scanned, 'layout').fire('change');
  node(scanned, 'scan').fire('click');
  assert.equal(node(scanned, 'layout').disabled, true, 'The layout is locked while a request is pending');
  await tick();
  assert.equal(node(scanned, 'layout').value, 'flat');
  assert.equal(node(scanned, 'apply').disabled, false, 'A new reviewed scan authorizes its chosen output structure');
  assert.equal(scanned.calls.filter(call => call.path.endsWith('/scan')).length, 1);
  scanned.controller.hide();

  for (const status of ['scanning', 'running', 'canceling']) {
    const active = environment({ ...reviewed, status, output_layout: 'flat' });
    await active.controller.show();
    assert.equal(node(active, 'layout').disabled, true, status + ' jobs lock their reviewed structure');
    active.controller.hide();
  }
}

async function progressChecks() {
  const discovery = environment({ ...reviewed, status: 'scanning', phase_progress: {
    ...phaseProgress, phase: 'discovering', label: 'Finding input files', total: null, fraction: null } });
  await discovery.controller.show();
  assert.equal(node(discovery, 'phase').hidden, false);
  assert.equal(node(discovery, 'phase-label').textContent, 'Finding input files');
  assert.equal(node(discovery, 'phase-count').textContent, '0 files found · Counting files…');
  assert.equal(node(discovery, 'bar').hasAttribute('value'), false, 'Unknown discovery totals use a native indeterminate bar');
  assert.equal(node(discovery, 'phase-metrics').textContent, 'Phase elapsed: 0s · Estimating speed…');
  assert(!node(discovery, 'phase-count').textContent.includes('%'), 'Discovery must not invent a percentage');
  discovery.setJob({ ...reviewed, status: 'scanning', phase_progress: { ...phaseProgress, processed: 1,
    total: null, fraction: null, elapsed_seconds: 1, files_per_second: 1, eta_seconds: 40 } });
  await discovery.controller.refresh();
  assert.equal(node(discovery, 'bar').hasAttribute('value'), false);
  assert.equal(node(discovery, 'phase-metrics').textContent, 'Phase elapsed: 1s · 1.00 files/s',
    'Discovery never uses an ETA until its total is known');
  discovery.setJob({ ...reviewed, status: 'failed', phase_progress: { ...phaseProgress,
    processed: 1, total: null, fraction: null, active: false, elapsed_seconds: 1 } });
  await discovery.controller.refresh();
  assert.equal(node(discovery, 'phase-count').textContent, '1 file found · Total not determined');
  assert.equal(node(discovery, 'bar').hidden, true, 'Stopped discovery must not keep an animated indeterminate bar');
  assert.equal(node(discovery, 'phase-metrics').textContent, 'Phase elapsed: 1s');
  discovery.controller.hide();

  const small = environment({ ...reviewed, status: 'scanning', phase_progress: phaseProgress });
  await small.controller.show();
  assert.equal(node(small, 'phase-count').textContent, '0 / 1 file · 0%');
  assert.equal(node(small, 'bar').value, 0);
  assert.equal(node(small, 'bar').hasAttribute('value'), true, 'One-file jobs have the same determinate progress as large jobs');
  assert.equal(node(small, 'phase-metrics').textContent, 'Phase elapsed: 0s · Estimating remaining time…');
  small.setJob({ ...reviewed, phase_progress: { ...phaseProgress, processed: 1, fraction: 1,
    active: false, elapsed_seconds: 0.25, files_per_second: null } });
  await small.controller.refresh();
  assert.equal(node(small, 'phase-count').textContent, '1 / 1 file · 100%');
  assert.equal(node(small, 'phase-metrics').textContent, 'Phase elapsed: 0s', 'Quick completed jobs need no invented rate or ETA');
  small.controller.hide();

  const empty = environment({ ...reviewed, items: [], items_total: 0, counts: {}, phase_progress: {
    ...phaseProgress, total: 0, fraction: 1, active: false, elapsed_seconds: 0.01 } });
  await empty.controller.show();
  assert.equal(node(empty, 'phase-count').textContent, '0 / 0 files · 100%');
  assert.equal(node(empty, 'bar').value, 1, 'An empty completed phase is complete without division by zero');
  assert.equal(node(empty, 'phase-metrics').textContent, 'Phase elapsed: 0s');
  empty.controller.hide();

  const largePhase = { ...phaseProgress, processed: 1250, total: 4000, fraction: 0.3125,
    elapsed_seconds: 625, files_per_second: 2, eta_seconds: 1375 };
  const large = environment({ ...reviewed, status: 'scanning', items_total: 4000, phase_progress: largePhase },
    (path, options, getJob) => {
      if (path.includes('offset=100')) return Promise.resolve({ ...getJob(), offset: 100,
        items: [{ ...reviewed.items[0], title: 'Second review page' }] });
    });
  await large.controller.show();
  assert.equal(node(large, 'phase-count').textContent, '1,250 / 4,000 files · 31%');
  assert.equal(node(large, 'bar').value, 0.3125);
  assert.equal(node(large, 'phase-metrics').textContent,
    'Phase elapsed: 10m 25s · 2.00 files/s · Approx. remaining in this phase: 22m 55s');
  node(large, 'next').fire('click'); await tick();
  assert.equal(node(large, 'page').textContent, 'Showing rows 101–200 of 4000');
  assert.equal(node(large, 'phase-count').textContent, '1,250 / 4,000 files · 31%',
    'Paging review rows must not change global phase counts');
  assert.equal(node(large, 'bar').value, 0.3125);
  large.controller.hide();

  for (const status of ['ready', 'completed', 'canceled', 'interrupted', 'failed']) {
    const stopped = environment({ ...reviewed, status, phase_progress: { ...largePhase, active: false } });
    await stopped.controller.show();
    assert.equal(node(stopped, 'phase-metrics').textContent, 'Phase elapsed: 10m 25s · 2.00 files/s',
      status + ' has a frozen phase duration and measured speed, without an active ETA');
    const before = node(stopped, 'phase-metrics').textContent;
    await stopped.controller.refresh();
    assert.equal(node(stopped, 'phase-metrics').textContent, before, 'Refreshing a stopped snapshot never advances its clock');
    stopped.setJob({ ...reviewed, status, phase_progress: largePhase });
    await stopped.controller.refresh();
    assert.equal(node(stopped, 'phase-metrics').textContent, before, 'Terminal job status suppresses stale active-phase ETA');
    stopped.controller.hide();
  }

  const resumed = environment({ ...reviewed, status: 'running', progress: 0.75, counts: { done: 3000, ready: 1000 },
    phase_progress: { ...phaseProgress, phase: 'creating', label: 'Creating FeedPaks', total: 1000 } });
  await resumed.controller.show();
  assert.equal(node(resumed, 'phase-count').textContent, '0 / 1,000 files · 0%');
  assert.equal(node(resumed, 'bar').value, 0, 'Resume measures this Apply phase independently from historical completed files');
  assert.equal(node(resumed, 'phase-metrics').textContent, 'Phase elapsed: 0s · Estimating remaining time…');
  resumed.controller.hide();

  for (const status of ['scanning', 'running', 'ready', 'interrupted']) {
    const old = environment({ ...reviewed, status, progress: 0.5 });
    await old.controller.show();
    assert.equal(node(old, 'phase').hidden, true, 'Old snapshots do not invent unavailable measurements');
    if (status === 'scanning') assert.equal(node(old, 'bar').hasAttribute('value'), false);
    else assert.equal(node(old, 'bar').value, status === 'ready' ? 1 : 0.5);
    assert(node(old, 'counts').textContent.includes('ready: 1'), 'Old checkpoint counters remain usable');
    old.controller.hide();
  }
}

async function main() {
  await layoutChecks();
  await progressChecks();
  assert.equal(ui.workerSetting('auto'), 'auto');
  assert.equal(ui.workerSetting('16'), 16);
  assert.equal(ui.workerSetting('17'), 'auto');
  assert.equal(ui.jobActions(reviewed).apply, true);
  assert.equal(ui.jobActions(ambiguous).apply, false);
  assert.equal(ui.jobActions({ ...reviewed, status: 'interrupted' }).resume, true);
  assert.equal(ui.jobActions({ ...reviewed, status: 'completed', counts: { done: 1 } }).resume, false);
  assert.equal(ui.jobActions({ ...reviewed, status: 'running' }).cancel, true);
  assert.equal(ui.jobActions({ ...reviewed, status: 'canceling' }).cancel, false);

  for (const status of ['interrupted', 'canceled', 'failed']) {
    const allDone = { ...reviewed, status, counts: { total: 1, done: 1, ready: 0, failed: 0, review: 0 } };
    assert.equal(ui.jobActions(allDone).resume, true, status + ' receipts still need explicit verification and finalization');
    assert.equal(ui.jobActions(allDone, true).resume, false, 'Busy jobs cannot resume');
    assert.equal(ui.jobActions(allDone, false, true).resume, false, 'Dirty settings cannot resume');
    assert.equal(ui.jobActions({ ...allDone, counts: { ...allDone.counts, review: 1 } }).resume, false, 'Unreviewed rows block resume');
    assert.equal(ui.jobActions({ ...allDone, groups: ambiguous.groups }).resume, false, 'Unresolved choices block resume');
    const recovery = environment(allDone);
    await recovery.controller.show();
    assert.equal(node(recovery, 'resume').disabled, false);
    assert(recovery.calls.every(call => !call.options), 'Opening an interrupted job never resumes automatically');
    node(recovery, 'resume').fire('click'); await tick();
    assert.equal(recovery.calls.filter(call => call.path.endsWith('/apply')).length, 1, 'Resume explicitly asks the backend to verify existing output');
    recovery.controller.hide();
  }

  const complete = environment({ ...reviewed, status: 'completed', counts: { total: 1, done: 1 } });
  await complete.controller.show();
  assert.equal(node(complete, 'resume').disabled, true, 'A normally completed job has nothing to finalize');
  node(complete, 'resume').fire('click'); await tick();
  assert(complete.calls.every(call => !call.options));
  complete.controller.hide();

  // Repeated scans are read-only even when every planned output already exists.
  const existingRow = { ...reviewed.items[0], status: 'done', receipt: { recovered: true },
    reason: 'Already complete and byte-verified; no new file was created.' };
  for (const status of ['ready', 'completed']) {
    const existing = environment({ ...reviewed, status,
      detail: 'No new FeedPaks needed. 0 created · 10 already complete.',
      counts: { total: 10, ready: 0, done: 10, created: 0, existing: 10 },
      items: [existingRow], items_total: 10 });
    await existing.controller.show();
    assert.equal(node(existing, 'apply').disabled, true, 'All-existing scans cannot create files');
    assert.equal(node(existing, 'resume').disabled, true, 'Verified existing files need no resume');
    assert.equal(node(existing, 'scan').disabled, false, 'An explicit new scan remains available');
    assert.equal(node(existing, 'detail').textContent, 'No new FeedPaks needed. 0 created · 10 already complete.');
    assert(node(existing, 'counts').textContent.includes('created: 0 · already complete: 10'),
      'Global completion counts must not be inferred from the current page');
    assert(!node(existing, 'counts').textContent.includes('done:'), 'Done does not imply new files were created');
    assert.equal(node(existing, 'items').find('b')[0].textContent, 'already complete',
      'Recovered receipts from current or older completed jobs have an explicit label');
    node(existing, 'apply').fire('click'); node(existing, 'resume').fire('click'); await tick();
    assert(existing.calls.every(call => !call.options), 'An all-existing result never sends Apply');
    existing.controller.hide();
  }

  const partial = environment({ ...reviewed,
    counts: { total: 3, ready: 1, done: 2, created: 1, existing: 1 },
    items: [existingRow, { ...reviewed.items[0], status: 'done', receipt: { recovered: false } }, reviewed.items[0]],
    items_total: 3 });
  await partial.controller.show();
  assert(node(partial, 'counts').textContent.includes('ready: 1'));
  assert(node(partial, 'counts').textContent.includes('created: 1 · already complete: 1'));
  assert.deepEqual(node(partial, 'items').find('b').map(item => item.textContent), ['already complete', 'created', 'ready']);
  assert.equal(node(partial, 'apply').disabled, false, 'Existing outputs do not block remaining ready files');
  node(partial, 'apply').fire('click'); await tick();
  assert.equal(partial.calls.filter(call => call.path.endsWith('/apply')).length, 1);
  partial.controller.hide();

  const warning = '<journal record could not be read>';
  const journal = environment({ ...reviewed, journal_warning: warning });
  await journal.controller.show();
  assert.equal(node(journal, 'detail').textContent, reviewed.detail + ' Recovery warning: ' + warning);
  assert.equal(node(journal, 'detail').childNodes.length, 0, 'Journal warnings are displayed as plain text');
  node(journal, 'workers').value = '2'; node(journal, 'workers').fire('change');
  assert(node(journal, 'detail').textContent.includes(warning), 'Dirty settings must not hide the recovery warning');
  assert(journal.calls.every(call => !call.options));
  journal.controller.hide();

  const initial = environment(reviewed);
  assert.equal(initial.calls.length, 0, 'Mount must not automatically scan or apply');
  await initial.controller.show();
  assert(initial.calls.every(call => !call.options), 'Opening the screen is read-only');
  assert.equal(node(initial, 'bar').value, 1, 'A finished review has complete preview progress');
  assert.equal(node(initial, 'apply').disabled, false);
  assert.equal(node(initial, 'items').childNodes[0].childNodes[1].textContent, '<untrusted title>');
  assert.equal(node(initial, 'items').find('small')[1].textContent, 'Variant: No Guitar');
  node(initial, 'workers').value = '4'; node(initial, 'workers').fire('change');
  assert.equal(node(initial, 'apply').disabled, true, 'Changing settings invalidates Apply');

  const choices = environment(ambiguous);
  await choices.controller.show();
  let select = node(choices, 'groups').find('select')[0];
  let button = node(choices, 'groups').find('button')[0];
  select.value = 'old/song.feedpak'; select.fire('change');
  assert.equal(choices.calls.length, 1, 'Selecting an option does not submit it');
  button.fire('click'); await tick();
  assert.equal(choices.calls.filter(call => call.path.endsWith('/choose')).length, 1);
  assert.equal(choices.calls.filter(call => call.path.endsWith('/apply')).length, 0, 'Saving a choice never applies automatically');
  assert.equal(node(choices, 'apply').disabled, false);
  node(choices, 'apply').fire('click'); await tick();
  assert.equal(choices.calls.filter(call => call.path.endsWith('/apply')).length, 1);

  const variantRows = [
    { id: 'song:guitar', relative_path: 'song.feedpak', title: 'Song', status: 'review', excluded_stems: ['guitar'], variant_label: 'No Guitar' },
    { id: 'song:vocals', relative_path: 'song.feedpak', title: 'Song', status: 'review', excluded_stems: ['vocals'], variant_label: 'No Vocals' },
  ];
  const variantGroups = [
    { ...ambiguous.groups[0], id: 'guitar-group' },
    { ...ambiguous.groups[0], id: 'vocals-group', excluded_stems: ['vocals'], variant_label: 'No Vocals',
      candidates: [{ id: 'old/vocals.feedpak', relative_path: 'old/vocals.feedpak', full_sha256: 'b'.repeat(64) }] },
  ];
  const mixedJob = { ...reviewed, counts: { total: 2, ready: 0, review: 2 }, input_packages_total: 1,
    output_variants_total: 2, items_total: 2, items: variantRows, groups: variantGroups };
  const mixed = environment(mixedJob, (path, options, getJob, setJob) => {
    if (path.endsWith('/choose')) {
      assert.deepEqual(JSON.parse(options.body), { choices: { 'guitar-group': 'old/song.feedpak' } });
      const next = { ...getJob(), groups: [variantGroups[1]], counts: { total: 2, ready: 1, review: 1 },
        items: [{ ...variantRows[0], status: 'ready', donor_relative: 'old/song.feedpak', output_relative: 'Song (No Guitar).feedpak' }, variantRows[1]] };
      setJob(next); return Promise.resolve(next);
    }
  });
  await mixed.controller.show();
  assert(node(mixed, 'counts').textContent.startsWith('Input song packages: 1 · Output variants: 2'), 'One song can produce several output variants');
  assert.deepEqual(node(mixed, 'items').childNodes.map(row => row.find('small')[1].textContent), ['Variant: No Guitar', 'Variant: No Vocals']);
  assert.deepEqual(node(mixed, 'groups').childNodes.map(group => group.find('p')[0].textContent), ['Variant: No Guitar', 'Variant: No Vocals']);
  select = node(mixed, 'groups').find('select')[0]; button = node(mixed, 'groups').find('button')[0];
  select.value = 'old/song.feedpak'; select.fire('change'); button.fire('click'); await tick();
  assert.equal(node(mixed, 'groups').childNodes.length, 1, 'Choosing one variant leaves the other variant for review');
  assert.equal(node(mixed, 'groups').find('p')[0].textContent, 'Variant: No Vocals');
  assert.equal(node(mixed, 'apply').disabled, true, 'The remaining variant must still be reviewed');
  assert.equal(mixed.calls.filter(call => call.path.endsWith('/apply')).length, 0);
  mixed.controller.hide();

  const combined = environment({ ...reviewed, items: [{ ...reviewed.items[0], excluded_stems: ['guitar', 'vocals'], variant_label: 'No Guitar + Vocals' }] });
  await combined.controller.show();
  assert.equal(node(combined, 'items').find('small')[1].textContent, 'Variant: No Guitar + Vocals');
  combined.controller.hide();

  const oldPreview = environment({ id: 'checkpoint-unavailable', status: 'failed',
    detail: 'Previous preview uses the No Guitar-only policy. Scan again. Saved records and outputs were preserved.',
    items: [], groups: [], counts: {}, resources: {}, source_errors: [] });
  await oldPreview.controller.show();
  assert(node(oldPreview, 'detail').textContent.includes('Scan again.'));
  assert.equal(node(oldPreview, 'scan').disabled, false, 'An old preview can be replaced by an explicit new scan');
  assert.equal(node(oldPreview, 'apply').disabled, true);
  assert.equal(node(oldPreview, 'resume').disabled, true);
  assert(oldPreview.calls.every(call => !call.options), 'Opening an old preview never starts a new scan');
  oldPreview.controller.hide();

  const dirty = environment(ambiguous);
  await dirty.controller.show();
  select = node(dirty, 'groups').find('select')[0]; button = node(dirty, 'groups').find('button')[0];
  select.value = 'old/song.feedpak'; select.fire('change');
  node(dirty, 'old-browse').fire('click'); await tick();
  assert.equal(select.disabled, true); assert.equal(button.disabled, true);
  button.fire('click'); await tick();
  assert.equal(dirty.calls.filter(call => call.options).length, 0, 'A dirty job cannot save choices or restore old folders');
  assert.equal(node(dirty, 'old').value, 'C:/changed');

  const errors = environment({ ...reviewed, source_errors: [
    { relative_path: '<broken>', reason: '<error>' },
    { relative_path: 'missing.feedpak', reason: 'Missing removed-stem metadata.' },
    { relative_path: 'invalid.feedpak', reason: 'MinusMix excluded_stems contains full or duplicate stem IDs. <details>' },
  ] });
  await errors.controller.show();
  assert.equal(node(errors, 'errors-wrap').hidden, false);
  assert.equal(node(errors, 'apply').disabled, false, 'Unrelated source errors do not block already reviewed valid rows');
  assert.equal(node(errors, 'errors').childNodes[0].textContent, '<broken>: <error>');
  assert.equal(node(errors, 'errors').childNodes[1].textContent, 'missing.feedpak: Missing removed-stem metadata.');
  assert.equal(node(errors, 'errors').childNodes[2].textContent, 'invalid.feedpak: MinusMix excluded_stems contains full or duplicate stem IDs. <details>');
  assert(node(errors, 'errors').childNodes.every(error => error.childNodes.length === 0), 'Metadata diagnostics are displayed as plain text');

  let staleResolve, defer = false;
  const stale = environment(ambiguous, (path, options) => {
    if (!options && defer) return new Promise(resolve => { staleResolve = resolve; });
  });
  await stale.controller.show(); defer = true;
  const pending = stale.controller.refresh();
  select = node(stale, 'groups').find('select')[0]; button = node(stale, 'groups').find('button')[0];
  select.value = 'old/song.feedpak'; select.fire('change'); button.fire('click'); await tick();
  staleResolve(structuredClone(ambiguous)); await pending;
  assert.equal(node(stale, 'groups').childNodes.length, 0, 'A stale poll cannot undo a saved choice');
  assert.equal(node(stale, 'apply').disabled, false);

  const callPaths = [];
  const client = ui.createClient((path, options) => { callPaths.push({ path, options }); });
  client.status('id/slash', 100); client.choose('id/slash', { group: '__skip__' }); client.apply('id/slash');
  assert.equal(callPaths[0].path, '/reuse/id%2Fslash?offset=100&limit=100');
  assert.equal(callPaths[1].options.body, '{"choices":{"group":"__skip__"}}');
  assert.equal(callPaths[2].options.method, 'POST');
  for (const env of [initial, choices, dirty, errors, stale]) env.controller.hide();
  process.stdout.write('Reuse UI behavior checks passed\n');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
