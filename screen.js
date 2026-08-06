/* Practice Mix Exporter — screen UI + library card action. */
(function () {
  'use strict';
  if (window.__practiceMixExporterLoaded) return;
  window.__practiceMixExporterLoaded = true;

  var API = '/api/plugins/practice_mix_exporter';
  var SCREEN_ID = 'plugin-practice_mix_exporter';
  var STORAGE_OUTPUT = 'practice_mix_exporter.output_dir';
  var STORAGE_BATCH_INPUT = 'practice_mix_exporter.batch_input_dir';
  var STORAGE_BATCH_OUTPUT = 'practice_mix_exporter.batch_output_dir';
  var STORAGE_MODE = 'practice_mix_exporter.mode';
  var fb = window.feedBack;
  var state = {
    inited: false, busy: false, selectedFilename: '', selectedLabel: '',
    sourceInfo: null, searchTimer: null, ffmpegAvailable: true,
    statusRetryTimer: null, statusLoading: false,
    singleJobId: '', singlePollTimer: null, singleNotifiedId: '',
    batchScan: null, batchScanKey: '', batchJobId: '', batchPollTimer: null,
    batchBusy: false, batchActive: false, batchNotifiedId: '',
    separation: { available: false, ready: false, reason: 'Checking the Stem Splitter server…' },
  };

  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function jsonFetch(path, options) {
    return fetch(API + path, options).then(async function (response) {
      var data = null;
      try { data = await response.json(); } catch (_) {}
      if (!response.ok) {
        var detail = data && data.detail;
        throw new Error(typeof detail === 'string' ? detail : 'Request failed (HTTP ' + response.status + ')');
      }
      return data || {};
    });
  }
  function notify(title, message, accent) {
    try {
      if (window.fbNotify && window.fbNotify.show) {
        window.fbNotify.show({ title: title, message: message || '', accent: accent || 'info' });
        return;
      }
    } catch (_) {}
    console.log('[practice_mix_exporter]', title, message || '');
  }
  function showStatus(kind, text) {
    var node = $('pmx-status');
    if (!node) return;
    node.className = 'pmx-status ' + kind;
    node.textContent = text || '';
  }
  function selectedStems() {
    return Array.prototype.slice.call(document.querySelectorAll('#pmx-stems input[type=checkbox]:checked'))
      .map(function (input) { return input.value; });
  }
  function selectedBatchStems() {
    return Array.prototype.slice.call(document.querySelectorAll('#pmx-batch-stems input[type=checkbox]:checked'))
      .map(function (input) { return input.value; });
  }
  function labelFor(id) {
    var labels = { guitar: 'Guitar', bass: 'Bass', drums: 'Drums', vocals: 'Vocals', piano: 'Piano', other: 'Other' };
    return labels[id] || String(id).replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }
  function stageLabel(stage) {
    var labels = {
      queued: 'Waiting', validating: 'Checking source', extracting: 'Reading audio',
      separating: 'Splitting audio with AI', rendering: 'Creating backing track',
      preview: 'Creating preview', packaging: 'Packaging FeedPak', done: 'Completed',
      canceling: 'Canceling', canceled: 'Canceled', failed: 'Failed', skipped: 'Skipped',
    };
    return labels[stage] || String(stage || 'Working').replace(/_/g, ' ');
  }
  function outputFolder() { return (($('pmx-output') && $('pmx-output').value) || '').trim(); }
  function batchInputFolder() { return (($('pmx-batch-input') && $('pmx-batch-input').value) || '').trim(); }
  function batchOutputFolder() { return (($('pmx-batch-output') && $('pmx-batch-output').value) || '').trim(); }

  function selectionNeedsSeparation(stems) {
    if (!state.sourceInfo || !Array.isArray(state.sourceInfo.stems)) return false;
    return stems.some(function (stemId) {
      var meta = state.sourceInfo.stems.find(function (stem) { return stem.id === stemId; });
      return !!(meta && meta.requires_separation);
    });
  }

  function renderEngineStatus() {
    var root = $('pmx-engine'), text = $('pmx-engine-text');
    if (!root || !text) return;
    var engine = state.separation || {};
    root.className = 'pmx-engine ' + (engine.ready ? 'ready' : 'not-ready');
    text.textContent = engine.ready
      ? 'Stem Splitter server ready — ' + (engine.reason || 'temporary separation available')
      : 'Temporary separation unavailable — ' + (engine.reason || 'start the server in Stem Splitter');
  }

  function renderSourceWarning(info) {
    var node = $('pmx-source-warning');
    if (!node) return;
    var exclusions = info && Array.isArray(info.derived_exclusions) ? info.derived_exclusions : [];
    node.hidden = !exclusions.length;
    node.textContent = exclusions.length
      ? 'This is already a practice mix with ' + exclusions.map(labelFor).join(' + ')
        + ' removed. For the best audio quality, select the original FeedPak instead of separating this copy again.'
      : '';
  }

  function updateReady() {
    var stems = selectedStems();
    var needsSeparation = selectionNeedsSeparation(stems);
    var engineOkay = !needsSeparation || !!(state.separation && state.separation.ready);
    var ready = !!state.selectedFilename && !!state.sourceInfo && stems.length > 0
      && !!outputFolder() && !state.busy && !state.batchActive && state.ffmpegAvailable && engineOkay;
    var button = $('pmx-export');
    if (button) button.disabled = !ready;
    ['pmx-source', 'pmx-search', 'pmx-refresh', 'pmx-browse'].forEach(function (id) {
      if ($(id)) $(id).disabled = state.busy || state.batchActive;
    });
    Array.prototype.forEach.call(document.querySelectorAll('#pmx-stems input'), function (input) {
      input.disabled = state.busy || state.batchActive;
    });
    var title = $('pmx-summary-title'), detail = $('pmx-summary-detail');
    if (!title || !detail) return;
    if (state.busy) {
      title.textContent = 'Creating practice feedpak…';
      detail.textContent = (needsSeparation ? 'Temporarily separating, then rendering' : 'Rendering')
        + ' the full mix minus ' + stems.map(labelFor).join(' + ') + '. The source remains untouched.';
    } else if (ready) {
      title.textContent = 'Create “No ' + stems.map(labelFor).join(' + ') + '” copy';
      detail.textContent = 'Single-stem output • native backing playback • source preserved'
        + (needsSeparation ? ' • temporary separation' : ' • existing stem reused');
    } else if (needsSeparation && !(state.separation && state.separation.ready)) {
      title.textContent = 'Start the Stem Splitter server first';
      detail.textContent = (state.separation && state.separation.reason) || 'The selected audio must be separated temporarily.';
    } else {
      title.textContent = 'Ready when you are';
      detail.textContent = 'Select a song, at least one stem, and an output folder.';
    }
  }

  function renderStems(info) {
    var root = $('pmx-stems');
    if (!root) return;
    var stems = (info && info.stems) || [];
    if (!stems.length) {
      root.innerHTML = '<p class="pmx-help">No exportable instrument targets were found.</p>';
      updateReady();
      return;
    }
    var defaultId = stems.some(function (s) { return s.id === 'guitar'; }) ? 'guitar' : stems[0].id;
    root.innerHTML = stems.map(function (stem) {
      var checked = stem.id === defaultId ? ' checked' : '';
      return '<label class="pmx-stem"><input type="checkbox" value="' + esc(stem.id) + '"' + checked + '>'
        + '<span>' + esc(stem.label || labelFor(stem.id)) + '</span>'
        + '<small>' + (stem.saved ? 'saved stem' : 'temporary split') + '</small></label>';
    }).join('');
    Array.prototype.forEach.call(root.querySelectorAll('input'), function (input) {
      input.addEventListener('change', updateReady);
    });
    updateReady();
  }

  function chooseSource(filename) {
    if (!filename) return;
    state.selectedFilename = filename;
    state.sourceInfo = null;
    renderSourceWarning(null);
    renderStems(null);
    updateReady();
    jsonFetch('/source?filename=' + encodeURIComponent(filename)).then(function (info) {
      if (state.selectedFilename !== filename) return;
      state.sourceInfo = info;
      renderStems(info);
      renderSourceWarning(info);
      showStatus('info', 'Selected ' + (info.artist ? info.artist + ' — ' : '') + info.title + '.');
    }).catch(function (error) {
      if (state.selectedFilename !== filename) return;
      state.sourceInfo = null;
      renderSourceWarning(null);
      renderStems(null);
      showStatus('error', error.message);
    });
  }

  function loadSources(preselect) {
    var select = $('pmx-source');
    if (!select) return Promise.resolve();
    var q = (($('pmx-search') && $('pmx-search').value) || '').trim();
    select.disabled = true;
    select.innerHTML = '<option>Loading feedpaks…</option>';
    return jsonFetch('/sources?q=' + encodeURIComponent(q)).then(function (data) {
      var songs = data.songs || [];
      select.innerHTML = songs.map(function (song) {
        var text = (song.artist ? song.artist + ' — ' : '') + song.title;
        return '<option value="' + esc(song.filename) + '">' + esc(text) + '</option>';
      }).join('');
      select.disabled = false;
      if (!songs.length) {
        select.innerHTML = '<option disabled>No feedpaks found</option>';
        return;
      }
      var wanted = preselect || state.selectedFilename;
      if (wanted && songs.some(function (song) { return song.filename === wanted; })) {
        select.value = wanted;
        chooseSource(wanted);
      } else if (wanted) {
        // A card action is authoritative even when this screen's source list is
        // capped for a very large library. Keep the selected song usable rather
        // than silently losing it because it sorted after the first 250 rows.
        var option = document.createElement('option');
        option.value = wanted;
        option.textContent = state.selectedLabel || wanted;
        select.insertBefore(option, select.firstChild);
        select.value = wanted;
        chooseSource(wanted);
      }
    }).catch(function (error) {
      select.innerHTML = '<option disabled>Could not load songs</option>';
      select.disabled = false;
      showStatus('error', error.message);
    });
  }

  function openForSong(song) {
    if (!song || !song.filename) return;
    state.selectedFilename = song.filename;
    state.selectedLabel = (song.artist ? song.artist + ' — ' : '') + (song.title || song.filename);
    if (fb && fb.navigate) fb.navigate(SCREEN_ID);
    else if (window.showScreen) window.showScreen(SCREEN_ID);
    // Navigation and plugin-screen hydration can complete on different ticks.
    setTimeout(function () { wireScreen(); loadSources(song.filename); }, 0);
  }

  function registerCardAction() {
    if (!fb || !fb.libraryCardActions) return;
    fb.libraryCardActions.register({
      id: 'practice_mix_exporter.create',
      pluginId: 'practice_mix_exporter',
      label: 'Create practice mix…',
      placement: 'menu',
      order: 31,
      applies: function (song) {
        return !!(song && typeof song.filename === 'string'
          && /\.(feedpak|sloppak)$/i.test(song.filename));
      },
      enabled: function () { return true; },
      run: openForSong,
    });
  }

  function browseOutput() {
    if (!window.feedBackDesktop || typeof window.feedBackDesktop.pickDirectory !== 'function') {
      showStatus('error', 'Choosing an arbitrary output folder requires the desktop app.');
      return;
    }
    window.feedBackDesktop.pickDirectory().then(function (path) {
      if (!path) return;
      $('pmx-output').value = path;
      try { localStorage.setItem(STORAGE_OUTPUT, path); } catch (_) {}
      updateReady();
    }).catch(function (error) { showStatus('error', String(error)); });
  }

  function renderSingleJob(job) {
    if (!job) return;
    state.singleJobId = job.id || '';
    var active = ['queued', 'running', 'canceling'].indexOf(job.status) >= 0;
    state.busy = active;
    var progress = $('pmx-single-progress');
    if (progress) progress.hidden = false;
    if ($('pmx-single-progress-title')) {
      $('pmx-single-progress-title').textContent = job.status === 'completed'
        ? 'Practice FeedPak created' : job.status === 'canceled' ? 'Export canceled'
          : job.status === 'failed' ? 'Export failed' : stageLabel(job.stage);
    }
    if ($('pmx-single-current')) {
      $('pmx-single-current').textContent = stageLabel(job.stage)
        + (job.detail ? ' — ' + job.detail : '');
    }
    if ($('pmx-single-progress-bar')) {
      $('pmx-single-progress-bar').value = Number(job.progress || 0);
    }
    if ($('pmx-single-cancel')) {
      $('pmx-single-cancel').disabled = !active || job.status === 'canceling';
    }

    clearTimeout(state.singlePollTimer);
    if (active) {
      state.singlePollTimer = setTimeout(pollSingleExport, 750);
    } else if (job.status === 'completed' && job.result) {
      var result = job.result;
      var temporary = result.temporary_separation_used ? ' Temporary separator files were deleted.' : '';
      showStatus('ok', 'Created ' + result.filename + ' at ' + result.path
        + '. The original feedpak was not changed.' + temporary);
      if (job.id && state.singleNotifiedId !== job.id) {
        state.singleNotifiedId = job.id;
        notify('Practice feedpak created', result.filename, 'ok');
      }
    } else if (job.status === 'canceled') {
      showStatus('info', 'Export canceled safely. The source FeedPak was not changed.');
    } else if (job.status === 'failed') {
      showStatus('error', job.detail || 'Practice mix export failed.');
      if (job.id && state.singleNotifiedId !== job.id) {
        state.singleNotifiedId = job.id;
        notify('Practice mix export failed', job.detail || 'Export failed', 'warn');
      }
    }
    updateReady();
    updateBatchReady();
  }

  function pollSingleExport() {
    if (!state.singleJobId) return;
    jsonFetch('/export/' + encodeURIComponent(state.singleJobId)).then(renderSingleJob).catch(function (error) {
      showStatus('error', 'Could not refresh export status: ' + error.message);
      state.singlePollTimer = setTimeout(pollSingleExport, 2500);
    });
  }

  function loadLatestSingleExport() {
    jsonFetch('/export/latest').then(function (data) {
      if (data.job) renderSingleJob(data.job);
    }).catch(function () {});
  }

  function cancelSingleExport() {
    if (!state.singleJobId || !state.busy) return;
    if ($('pmx-single-cancel')) $('pmx-single-cancel').disabled = true;
    showStatus('info', 'Cancel requested. The current operation is stopping safely…');
    jsonFetch('/export/' + encodeURIComponent(state.singleJobId) + '/cancel', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    }).then(renderSingleJob).catch(function (error) {
      showStatus('error', error.message);
      if ($('pmx-single-cancel')) $('pmx-single-cancel').disabled = false;
    });
  }

  function setMode(mode) {
    var batch = mode === 'batch';
    if ($('pmx-single-panel')) $('pmx-single-panel').hidden = batch;
    if ($('pmx-batch-panel')) $('pmx-batch-panel').hidden = !batch;
    if ($('pmx-mode-single')) {
      $('pmx-mode-single').classList.toggle('active', !batch);
      $('pmx-mode-single').setAttribute('aria-selected', batch ? 'false' : 'true');
    }
    if ($('pmx-mode-batch')) {
      $('pmx-mode-batch').classList.toggle('active', batch);
      $('pmx-mode-batch').setAttribute('aria-selected', batch ? 'true' : 'false');
    }
    try { localStorage.setItem(STORAGE_MODE, batch ? 'batch' : 'single'); } catch (_) {}
  }

  function showBatchStatus(kind, text) {
    var node = $('pmx-batch-status');
    if (!node) return;
    node.className = 'pmx-status ' + kind;
    node.textContent = text || '';
  }

  function batchOptions() {
    return {
      input_dir: batchInputFolder(),
      output_dir: batchOutputFolder(),
      excluded_stems: selectedBatchStems(),
      recursive: !!($('pmx-batch-recursive') && $('pmx-batch-recursive').checked),
      skip_existing: !!($('pmx-batch-skip-existing') && $('pmx-batch-skip-existing').checked),
      skip_derived: !!($('pmx-batch-skip-derived') && $('pmx-batch-skip-derived').checked),
    };
  }

  function batchOptionsKey() { return JSON.stringify(batchOptions()); }

  function invalidateBatchScan() {
    if (state.batchActive) return;
    state.batchScan = null;
    state.batchScanKey = '';
    updateBatchReady();
  }

  function updateBatchReady() {
    var options = batchOptions();
    var hasBasics = !!options.input_dir && !!options.output_dir && options.excluded_stems.length > 0;
    var currentScan = state.batchScan && state.batchScanKey === batchOptionsKey();
    var needsSeparation = !!(currentScan && currentScan.counts && currentScan.counts.needs_separation);
    var engineOkay = !needsSeparation || !!(state.separation && state.separation.ready);
    if ($('pmx-batch-scan')) $('pmx-batch-scan').disabled = !hasBasics || state.busy || state.batchBusy || state.batchActive;
    if ($('pmx-batch-start')) {
      $('pmx-batch-start').disabled = !currentScan || !state.batchScan.counts
        || state.batchScan.counts.ready < 1 || state.busy || state.batchBusy || state.batchActive
        || !state.ffmpegAvailable || !engineOkay;
    }
    ['pmx-batch-input-browse', 'pmx-batch-output-browse', 'pmx-batch-recursive',
      'pmx-batch-skip-existing', 'pmx-batch-skip-derived'].forEach(function (id) {
      if ($(id)) $(id).disabled = state.busy || state.batchBusy || state.batchActive;
    });
    Array.prototype.forEach.call(document.querySelectorAll('#pmx-batch-stems input'), function (input) {
      input.disabled = state.busy || state.batchBusy || state.batchActive;
    });
    var title = $('pmx-batch-summary-title'), detail = $('pmx-batch-summary-detail');
    if (!title || !detail) return;
    if (state.busy) {
      title.textContent = 'Single-song export is running';
      detail.textContent = 'Wait for it to finish or cancel it safely before starting a batch.';
    } else if (state.batchActive) {
      title.textContent = 'Batch conversion is running';
      detail.textContent = 'One GPU separation runs at a time. You can leave this screen and return later.';
    } else if (state.batchBusy) {
      title.textContent = 'Scanning folder…';
      detail.textContent = 'Reading feedpak manifests without changing any files.';
    } else if (currentScan) {
      var counts = state.batchScan.counts || {};
      title.textContent = (counts.ready || 0) + ' feedpak' + (counts.ready === 1 ? '' : 's') + ' ready';
      detail.textContent = (counts.needs_separation || 0) + ' need temporary AI separation • '
        + (counts.uses_saved_stems || 0) + ' can reuse saved stems • '
        + ((counts.skipped_existing || 0) + (counts.skipped_derived || 0)) + ' skipped safely';
      if (needsSeparation && !engineOkay) {
        detail.textContent += ' • start the Stem Splitter server before starting';
      }
    } else {
      title.textContent = hasBasics ? 'Scan before starting' : 'Choose source and output folders';
      detail.textContent = 'Scan first to preview how many files need AI separation.';
    }
  }

  function browseBatchFolder(inputId, storageKey) {
    if (!window.feedBackDesktop || typeof window.feedBackDesktop.pickDirectory !== 'function') {
      showBatchStatus('error', 'Choosing folders requires the desktop app.');
      return;
    }
    window.feedBackDesktop.pickDirectory().then(function (path) {
      if (!path) return;
      $(inputId).value = path;
      try { localStorage.setItem(storageKey, path); } catch (_) {}
      invalidateBatchScan();
    }).catch(function (error) { showBatchStatus('error', String(error)); });
  }

  function renderBatchItems(items, scanMode) {
    var root = $('pmx-batch-items');
    if (!root) return;
    items = Array.isArray(items) ? items : [];
    if (!items.length) {
      root.innerHTML = '<p class="pmx-help">No per-file results to show.</p>';
      return;
    }
    root.innerHTML = items.map(function (item) {
      var status = scanMode ? (item.scan_status || 'ready') : (item.status || 'queued');
      var detail = item.detail || item.reason || (item.needs_separation ? 'Temporary separation' : 'Saved stem');
      return '<div class="pmx-batch-item ' + esc(status) + '"><b>' + esc(status) + '</b><div>'
        + esc(item.relative_path || item.title || 'Feedpak') + '<small>' + esc(detail) + '</small></div></div>';
    }).join('');
  }

  function renderBatchScan(scan, optionsKey) {
    state.batchScan = scan;
    state.batchScanKey = optionsKey || batchOptionsKey();
    var progress = $('pmx-batch-progress');
    if (progress) progress.hidden = false;
    if ($('pmx-batch-progress-title')) $('pmx-batch-progress-title').textContent = 'Scan preview';
    if ($('pmx-batch-current')) {
      $('pmx-batch-current').textContent = scan.truncated
        ? 'The preview hit its safety limit; choose a smaller source folder.'
        : (scan.counts.found + ' feedpaks found'
          + (scan.recursive ? ' recursively.' : ' in the selected folder.'));
      if (scan.items_truncated) {
        $('pmx-batch-current').textContent += ' Showing the first ' + (scan.items || []).length + ' results.';
      }
    }
    if ($('pmx-batch-progress-bar')) $('pmx-batch-progress-bar').value = 0;
    var counts = scan.counts || {};
    if ($('pmx-batch-counts')) {
      $('pmx-batch-counts').innerHTML = '<span>' + (counts.ready || 0) + ' ready</span>'
        + '<span>' + (counts.needs_separation || 0) + ' AI splits</span>'
        + '<span>' + (counts.uses_saved_stems || 0) + ' saved stems</span>'
        + '<span>' + ((counts.skipped_existing || 0) + (counts.skipped_derived || 0)) + ' skipped</span>'
        + '<span>' + (counts.invalid || 0) + ' invalid</span>';
    }
    if ($('pmx-batch-cancel')) $('pmx-batch-cancel').disabled = true;
    renderBatchItems(scan.items, true);
    updateBatchReady();
  }

  function scanBatch() {
    if (state.batchBusy || state.batchActive) return;
    state.batchBusy = true;
    state.batchScan = null;
    var options = batchOptions();
    var optionsKey = JSON.stringify(options);
    updateBatchReady();
    showBatchStatus('info', 'Scanning feedpaks and checking which stems are already available…');
    jsonFetch('/batch/scan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(options),
    }).then(function (scan) {
      if (batchOptionsKey() !== optionsKey) {
        showBatchStatus('info', 'Folder options changed while scanning. Scan again for an up-to-date preview.');
        return;
      }
      renderBatchScan(scan, optionsKey);
      showBatchStatus('ok', 'Scan complete. Sources remain untouched.');
    }).catch(function (error) {
      showBatchStatus('error', error.message);
    }).finally(function () {
      state.batchBusy = false;
      updateBatchReady();
    });
  }

  function renderBatchJob(job) {
    if (!job) return;
    state.batchJobId = job.id || '';
    var active = ['queued', 'running', 'canceling'].indexOf(job.status) >= 0;
    state.batchActive = active;
    var progress = $('pmx-batch-progress');
    if (progress) progress.hidden = false;
    if ($('pmx-batch-progress-title')) {
      $('pmx-batch-progress-title').textContent = job.status === 'completed'
        ? 'Batch completed' : job.status === 'canceled' ? 'Batch canceled'
          : job.status === 'failed' || job.status === 'interrupted' ? 'Batch stopped' : 'Batch running';
    }
    if ($('pmx-batch-current')) {
      var runningItem = (job.items || []).find(function (item) { return item.status === 'running'; });
      var position = job.current_item_number
        ? 'Song ' + job.current_item_number + ' of ' + (job.items_total || (job.items || []).length) + ' — '
        : '';
      $('pmx-batch-current').textContent = job.current_relative_path
        ? position + job.current_relative_path + ' — '
          + stageLabel((runningItem && runningItem.stage) || job.status)
          + (job.detail ? ': ' + job.detail : '')
        : (job.detail || '');
    }
    if ($('pmx-batch-progress-bar')) $('pmx-batch-progress-bar').value = Number(job.overall_progress || 0);
    var counts = job.counts || {};
    if ($('pmx-batch-counts')) {
      $('pmx-batch-counts').innerHTML = '<span>' + (counts.done || 0) + ' created</span>'
        + '<span>' + (counts.queued || 0) + ' waiting</span>'
        + '<span>' + (counts.skipped || 0) + ' skipped</span>'
        + '<span>' + (counts.failed || 0) + ' failed</span>'
        + '<span>' + (counts.duplicate_audio_reused || 0) + ' duplicate splits reused</span>';
    }
    if ($('pmx-batch-cancel')) $('pmx-batch-cancel').disabled = !active || job.status === 'canceling';
    renderBatchItems(job.items, false);
    updateReady();
    updateBatchReady();

    clearTimeout(state.batchPollTimer);
    if (active) {
      state.batchPollTimer = setTimeout(pollBatch, 1000);
    } else if (job.id && state.batchNotifiedId !== job.id) {
      state.batchNotifiedId = job.id;
      if (job.status === 'completed') {
        showBatchStatus((counts.failed || 0) ? 'error' : 'ok', job.detail || 'Batch completed.');
        notify('Practice-mix batch completed', (counts.done || 0) + ' feedpaks created', (counts.failed || 0) ? 'warn' : 'ok');
      } else if (job.status === 'canceled') {
        showBatchStatus('info', 'Batch canceled safely. Completed output files were kept.');
      } else {
        showBatchStatus('error', job.detail || 'Batch stopped.');
      }
      state.batchScan = null;
      state.batchScanKey = '';
      updateBatchReady();
    }
  }

  function pollBatch() {
    if (!state.batchJobId) return;
    jsonFetch('/batch/' + encodeURIComponent(state.batchJobId)).then(renderBatchJob).catch(function (error) {
      showBatchStatus('error', 'Could not refresh batch status: ' + error.message);
      state.batchPollTimer = setTimeout(pollBatch, 2500);
    });
  }

  function loadLatestBatch() {
    jsonFetch('/batch/latest').then(function (data) {
      if (data.job) {
        if (['queued', 'running', 'canceling'].indexOf(data.job.status) < 0) {
          state.batchNotifiedId = data.job.id;
        }
        renderBatchJob(data.job);
      }
    }).catch(function () {});
  }

  function startBatch() {
    if (state.batchBusy || state.batchActive || !state.batchScan
        || state.batchScanKey !== batchOptionsKey()) return;
    state.batchBusy = true;
    updateBatchReady();
    showBatchStatus('info', 'Starting sequential conversion. Processing one song at a time for GPU stability…');
    jsonFetch('/batch/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(batchOptions()),
    }).then(function (job) {
      state.batchNotifiedId = '';
      renderBatchJob(job);
    }).catch(function (error) {
      showBatchStatus('error', error.message);
    }).finally(function () {
      state.batchBusy = false;
      updateBatchReady();
    });
  }

  function cancelBatch() {
    if (!state.batchJobId || !state.batchActive) return;
    $('pmx-batch-cancel').disabled = true;
    showBatchStatus('info', 'Cancel requested. The current operation will stop at a safe checkpoint…');
    jsonFetch('/batch/' + encodeURIComponent(state.batchJobId) + '/cancel', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    }).then(renderBatchJob).catch(function (error) {
      showBatchStatus('error', error.message);
      $('pmx-batch-cancel').disabled = false;
    });
  }

  function createExport() {
    if (state.busy || state.batchActive) return;
    var stems = selectedStems();
    if (!state.selectedFilename || !stems.length || !outputFolder()) return;
    state.busy = true;
    updateReady();
    showStatus('info', selectionNeedsSeparation(stems)
      ? 'Separating ' + stems.map(labelFor).join(' + ') + ' temporarily, then creating the single-stem feedpak. This can take several minutes…'
      : 'Rendering audio and packaging a new feedpak…');
    jsonFetch('/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: state.selectedFilename, excluded_stems: stems, output_dir: outputFolder() }),
    }).then(function (job) {
      state.singleNotifiedId = '';
      renderSingleJob(job);
    }).catch(function (error) {
      showStatus('error', error.message);
      notify('Practice mix export failed', error.message, 'warn');
      state.busy = false;
      updateReady();
      updateBatchReady();
    });
  }

  function refreshStatus() {
    clearTimeout(state.statusRetryTimer);
    if (state.statusLoading) return Promise.resolve();
    state.statusLoading = true;
    if ($('pmx-engine-refresh')) $('pmx-engine-refresh').disabled = true;
    return jsonFetch('/status').then(function (status) {
      state.ffmpegAvailable = !!status.ffmpeg_available;
      state.separation = status.separation || state.separation;
      renderEngineStatus();
      updateReady();
      updateBatchReady();
      if (!status.ffmpeg_available) {
        showStatus('error', 'FFmpeg is unavailable; repair or reinstall the desktop app before exporting.');
      }
    }).catch(function (error) {
      var text = $('pmx-engine-text');
      if (text) text.textContent = 'Could not refresh Stem Splitter server status — ' + error.message;
    }).finally(function () {
      state.statusLoading = false;
      if ($('pmx-engine-refresh')) $('pmx-engine-refresh').disabled = false;
      var screen = document.getElementById(SCREEN_ID);
      if (screen && screen.classList.contains('active')) {
        var delay = state.separation && state.separation.ready ? 10000 : 3000;
        state.statusRetryTimer = setTimeout(refreshStatus, delay);
      }
    });
  }

  function wireScreen() {
    if (state.inited) return;
    var root = document.querySelector('.practice-mix-exporter');
    if (!root) return;
    state.inited = true;
    var saved = '';
    var batchInput = '', batchOutput = '', savedMode = 'single';
    try { saved = localStorage.getItem(STORAGE_OUTPUT) || ''; } catch (_) {}
    try { batchInput = localStorage.getItem(STORAGE_BATCH_INPUT) || ''; } catch (_) {}
    try { batchOutput = localStorage.getItem(STORAGE_BATCH_OUTPUT) || ''; } catch (_) {}
    try { savedMode = localStorage.getItem(STORAGE_MODE) || 'single'; } catch (_) {}
    if ($('pmx-output')) $('pmx-output').value = saved;
    if ($('pmx-batch-input')) $('pmx-batch-input').value = batchInput;
    if ($('pmx-batch-output')) $('pmx-batch-output').value = batchOutput;
    setMode(savedMode === 'batch' ? 'batch' : 'single');
    $('pmx-source').addEventListener('change', function () { chooseSource(this.value); });
    $('pmx-refresh').addEventListener('click', function () { loadSources(); refreshStatus(); });
    $('pmx-engine-refresh').addEventListener('click', refreshStatus);
    $('pmx-browse').addEventListener('click', browseOutput);
    $('pmx-export').addEventListener('click', createExport);
    $('pmx-single-cancel').addEventListener('click', cancelSingleExport);
    $('pmx-mode-single').addEventListener('click', function () { setMode('single'); });
    $('pmx-mode-batch').addEventListener('click', function () { setMode('batch'); });
    $('pmx-batch-input-browse').addEventListener('click', function () {
      browseBatchFolder('pmx-batch-input', STORAGE_BATCH_INPUT);
    });
    $('pmx-batch-output-browse').addEventListener('click', function () {
      browseBatchFolder('pmx-batch-output', STORAGE_BATCH_OUTPUT);
    });
    $('pmx-batch-scan').addEventListener('click', scanBatch);
    $('pmx-batch-start').addEventListener('click', startBatch);
    $('pmx-batch-cancel').addEventListener('click', cancelBatch);
    ['pmx-batch-recursive', 'pmx-batch-skip-existing', 'pmx-batch-skip-derived'].forEach(function (id) {
      $(id).addEventListener('change', invalidateBatchScan);
    });
    Array.prototype.forEach.call(document.querySelectorAll('#pmx-batch-stems input'), function (input) {
      input.addEventListener('change', invalidateBatchScan);
    });
    $('pmx-search').addEventListener('input', function () {
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(function () { loadSources(); }, 250);
    });
    refreshStatus();
    loadSources(state.selectedFilename);
    updateReady();
    updateBatchReady();
    loadLatestSingleExport();
    loadLatestBatch();
  }

  function onScreenChanged(event) {
    var id = event && event.detail && event.detail.id;
    if (id === SCREEN_ID) { wireScreen(); refreshStatus(); }
    else clearTimeout(state.statusRetryTimer);
  }

  function boot() {
    registerCardAction();
    if (fb && fb.on) fb.on('screen:changed', onScreenChanged);
    var screen = document.getElementById(SCREEN_ID);
    if (screen && screen.classList.contains('active')) wireScreen();
  }

  if (fb && fb.on) boot();
  else {
    var tries = 0;
    var timer = setInterval(function () {
      fb = window.feedBack;
      if ((fb && fb.on) || tries++ > 50) { clearInterval(timer); boot(); }
    }, 100);
  }
})();
