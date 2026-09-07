/* Existing No Guitar audio: explicit review, bounded pages, no automatic writes. */
(function (root) {
  'use strict';

  function workerSetting(value) {
    if (value === 'auto') return 'auto';
    var number = Number(value);
    return Number.isInteger(number) && number >= 1 && number <= 16 ? number : 'auto';
  }

  function jobActions(job, busy, dirty) {
    job = job || {};
    var active = ['scanning', 'running', 'canceling'].indexOf(job.status) >= 0;
    var counts = job.counts || {};
    var pending = Number(counts.ready || 0) + Number(counts.failed || 0);
    var unfinishedDone = Number(counts.done || 0) > 0
      && ['canceled', 'interrupted', 'failed'].indexOf(job.status) >= 0;
    var reviewed = !(job.groups || []).length && !Number(counts.review || 0);
    return {
      active: active,
      apply: !busy && !dirty && reviewed && job.status === 'ready' && Number(counts.ready || 0) > 0,
      resume: !busy && !dirty && reviewed && (pending > 0 || unfinishedDone)
        && ['canceled', 'interrupted', 'completed', 'failed'].indexOf(job.status) >= 0,
      cancel: !busy && active && job.status !== 'canceling',
    };
  }

  function createClient(request) {
    function post(path, value) {
      return request('/reuse' + path, { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value || {}) });
    }
    return {
      latest: function () { return request('/reuse/latest?offset=0&limit=100'); },
      status: function (id, offset) {
        return request('/reuse/' + encodeURIComponent(id) + '?offset=' + Math.max(0, Number(offset) || 0) + '&limit=100');
      },
      scan: function (options) { return post('/scan', options); },
      choose: function (id, choices) { return post('/' + encodeURIComponent(id) + '/choose', { choices: choices }); },
      apply: function (id) { return post('/' + encodeURIComponent(id) + '/apply'); },
      cancel: function (id) { return post('/' + encodeURIComponent(id) + '/cancel'); },
    };
  }

  function mount(options) {
    var doc = options.document;
    var panel = doc.getElementById('pmx-reuse-panel');
    if (!panel || panel.__reuseController) return panel && panel.__reuseController;
    var client = createClient(options.request), job = null, busy = false, dirty = false;
    var timer = null, offset = 0, pollBusy = false, shown = false, failures = 0, requestVersion = 0;
    var choiceControls = [];
    var storage = options.storage;
    function $(name) { return doc.getElementById('pmx-reuse-' + name); }
    function text(name, value) { $(name).textContent = String(value == null ? '' : value); }
    function element(tag, value, className) {
      var node = doc.createElement(tag);
      if (value != null) node.textContent = String(value);
      if (className) node.className = className;
      return node;
    }
    function saved(key, fallback) {
      try { return storage.getItem('minus_mix.reuse.' + key) || fallback; } catch (_) { return fallback; }
    }
    function save(key, value) { try { storage.setItem('minus_mix.reuse.' + key, value); } catch (_) {} }
    function message(value, error) {
      text('status', value);
      $('status').className = 'pmx-status ' + (error ? 'error' : 'info');
    }
    function folders() {
      return { old_dir: $('old').value, fresh_dir: $('fresh').value, output_dir: $('output').value,
        workers: workerSetting($('workers').value) };
    }
    function updateControls() {
      var actions = jobActions(job, busy, dirty), selected = folders();
      $('scan').disabled = busy || actions.active || !selected.old_dir || !selected.fresh_dir || !selected.output_dir;
      $('apply').disabled = !actions.apply;
      $('resume').disabled = !actions.resume;
      $('resume').hidden = !(job && ['canceled', 'interrupted', 'completed', 'failed'].indexOf(job.status) >= 0);
      $('cancel').disabled = !actions.cancel;
      $('cancel').hidden = !actions.active;
      ['old', 'fresh', 'output'].forEach(function (name) { $(name + '-browse').disabled = busy || actions.active; });
      $('workers').disabled = busy || actions.active;
      $('refresh').disabled = busy || pollBusy;
      $('previous').disabled = busy || pollBusy || offset <= 0;
      $('next').disabled = busy || pollBusy || !job || offset + 100 >= Number(job.items_total || 0);
      choiceControls.forEach(function (choice) {
        choice.select.disabled = busy || dirty || actions.active;
        choice.button.disabled = busy || dirty || actions.active || !choice.select.value;
      });
    }
    function renderGroups() {
      var target = $('groups');
      // Do not replace a user's unfinished choice during a status poll.
      var key = JSON.stringify((job && job.groups) || []);
      if (target.__groupsKey === key) return;
      target.__groupsKey = key;
      target.replaceChildren();
      choiceControls = [];
      ((job && job.groups) || []).forEach(function (group) {
        var card = element('section', null, 'pmx-reuse-choice');
        card.appendChild(element('strong', group.title || 'Choose a compatible audio version'));
        card.appendChild(element('p', (group.targets_count || 1) + ' current package(s). ' + (group.reason || ''), 'pmx-help'));
        var label = element('label', 'Existing No Guitar version');
        var select = element('select');
        var first = element('option', 'Choose a version or skip this group'); first.value = ''; select.appendChild(first);
        (group.candidates || []).forEach(function (candidate) {
          var item = element('option', (candidate.relative_path || candidate.id) + ' — audio ' + String(candidate.full_sha256 || '').slice(0, 12));
          item.value = candidate.id; select.appendChild(item);
        });
        var skip = element('option', 'Skip this group'); skip.value = '__skip__'; select.appendChild(skip);
        label.appendChild(select); card.appendChild(label);
        var choose = element('button', 'Save choice', 'pmx-button pmx-secondary');
        choose.type = 'button'; choose.disabled = true;
        select.addEventListener('change', updateControls);
        choose.addEventListener('click', function () {
          if (!select.value || busy || dirty || !job || jobActions(job).active) return;
          var choices = {}; choices[group.id] = select.value;
          choose.disabled = true;
          run(function () { return client.choose(job.id, choices); });
        });
        choiceControls.push({ select: select, button: choose });
        card.appendChild(choose); target.appendChild(card);
      });
    }
    function render() {
      var counts = (job && job.counts) || {};
      text('headline', job ? 'Audio reuse — ' + job.status : 'Choose your three folders');
      var detail = dirty ? 'Folder or worker settings changed. Scan again before creating files.'
        : job ? job.detail || '' : 'Scan compares current charts with existing No Guitar packages. Review the matches before creating files.';
      if (job && job.journal_warning) detail += ' Recovery warning: ' + job.journal_warning;
      text('detail', detail);
      text('counts', ['total', 'ready', 'review', 'blocked', 'done', 'failed', 'skipped']
        .map(function (key) { return key + ': ' + Number(counts[key] || 0); }).join(' · '));
      var resources = (job && job.resources) || {};
      text('resources', resources.effective_workers ? 'Workers: ' + resources.effective_workers
        + ' (requested ' + (resources.requested_workers || 'Auto') + '). ' + (resources.reason || '') : 'Auto adapts to CPU, available memory and storage. Manual values are maximums.');
      $('bar').value = job && job.status === 'ready' ? 1
        : Math.max(0, Math.min(1, Number(job && job.progress) || 0));
      var list = $('items'); list.replaceChildren();
      ((job && job.items) || []).forEach(function (item) {
        var row = element('div', null, 'pmx-batch-item');
        row.appendChild(element('b', item.status));
        var description = element('div', item.title || item.relative_path);
        description.appendChild(element('small', item.relative_path || ''));
        if (item.donor_relative) description.appendChild(element('small', 'Audio: ' + item.donor_relative));
        if (item.output_relative) description.appendChild(element('small', 'Output: ' + item.output_relative));
        if (item.reason) description.appendChild(element('small', item.reason));
        row.appendChild(description); list.appendChild(row);
      });
      var total = Number(job && job.items_total) || 0;
      text('page', total ? 'Showing ' + (offset + 1) + '–' + Math.min(offset + 100, total) + ' of ' + total : 'No package rows yet');
      var errors = $('errors'); errors.replaceChildren();
      ((job && job.source_errors) || []).forEach(function (error) {
        errors.appendChild(element('p', (error.relative_path || '') + ': ' + (error.reason || 'Could not inspect this source')));
      });
      if (job && job.source_errors_total > (job.source_errors || []).length) {
        errors.appendChild(element('p', 'Showing the first ' + job.source_errors.length + ' of '
          + job.source_errors_total + ' source errors. Resolve the listed files and scan again.'));
      }
      $('errors-wrap').hidden = !errors.childNodes.length;
      renderGroups(); updateControls();
    }
    function adopt(value) {
      if (!value) return;
      job = value; offset = Number(job.offset) || 0;
      if (!dirty) {
        [['old', 'old_dir'], ['fresh', 'fresh_dir'], ['output', 'output_dir']].forEach(function (pair) {
          if (job[pair[1]]) $(pair[0]).value = job[pair[1]];
        });
      }
      render();
    }
    function schedule() {
      clearTimeout(timer);
      if (shown && !doc.hidden && jobActions(job).active) {
        timer = setTimeout(refresh, Math.min(15000, 2000 * Math.pow(2, failures)));
      }
    }
    function refresh() {
      if (pollBusy || busy) return Promise.resolve();
      var version = requestVersion;
      pollBusy = true; updateControls();
      var pending = job ? client.status(job.id, offset) : client.latest().then(function (data) { return data.job; });
      return pending.then(function (value) { failures = 0; if (version === requestVersion) adopt(value); })
        .catch(function (error) { failures += 1; message(error.message, true); })
        .finally(function () { pollBusy = false; updateControls(); schedule(); });
    }
    function run(operation) {
      if (busy) return Promise.resolve();
      requestVersion += 1;
      busy = true; clearTimeout(timer); updateControls();
      return Promise.resolve().then(operation).then(function (value) {
        dirty = false; adopt(value); message(value.detail || 'Updated the reviewed job.');
      })
        .catch(function (error) { message(error.message, true); })
        .finally(function () { busy = false; updateControls(); schedule(); });
    }
    ['old', 'fresh', 'output'].forEach(function (name) {
      $(name).value = saved(name, '');
      $(name + '-browse').addEventListener('click', function () {
        if (busy || jobActions(job).active) return;
        if (!options.pickDirectory) { message('Folder selection is available in the desktop app.', true); return; }
        Promise.resolve(options.pickDirectory()).then(function (path) {
          if (!path || busy || jobActions(job).active) return;
          $(name).value = path; save(name, path); dirty = true; render();
        }).catch(function (error) { message(error.message, true); });
      });
    });
    $('workers').value = String(workerSetting(saved('workers', 'auto')));
    $('workers').addEventListener('change', function () {
      save('workers', $('workers').value); dirty = true; render();
    });
    $('scan').addEventListener('click', function () {
      if ($('scan').disabled) return;
      var options = folders(); offset = 0;
      run(function () { return client.scan(options); });
    });
    $('apply').addEventListener('click', function () {
      if (jobActions(job, busy, dirty).apply) run(function () { return client.apply(job.id); });
    });
    $('resume').addEventListener('click', function () {
      if (jobActions(job, busy, dirty).resume) run(function () { return client.apply(job.id); });
    });
    $('cancel').addEventListener('click', function () {
      if (jobActions(job, busy, dirty).cancel) run(function () { return client.cancel(job.id); });
    });
    $('refresh').addEventListener('click', refresh);
    $('previous').addEventListener('click', function () { if (offset > 0) { offset -= 100; refresh(); } });
    $('next').addEventListener('click', function () { if (job && offset + 100 < job.items_total) { offset += 100; refresh(); } });
    doc.addEventListener('visibilitychange', function () { if (doc.hidden) clearTimeout(timer); else if (shown) refresh(); });
    panel.__reuseController = {
      show: function () { shown = true; return refresh(); },
      hide: function () { shown = false; clearTimeout(timer); },
      refresh: refresh,
    };
    render();
    return panel.__reuseController;
  }

  var exported = { workerSetting: workerSetting, jobActions: jobActions, createClient: createClient, mount: mount };
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
  else root.MinusMixReuse = exported;
})(typeof window !== 'undefined' ? window : globalThis);
