/* MinusMix — screen UI + library card action. */
(function (root) {
  'use strict';

  function sourceResultIsCurrent(requestId, currentRequestId) {
    return requestId === currentRequestId;
  }

  function resolvedSourceSelection(songs, explicitPreselect, selectedFilename) {
    if (!Array.isArray(songs) || !songs.length) return '';
    var wanted = explicitPreselect || selectedFilename || '';
    if (wanted && songs.some(function (song) { return song.filename === wanted; })) {
      return wanted;
    }
    // Card actions remain authoritative even beyond the bounded search result.
    return explicitPreselect || '';
  }

  function engineStatusPresentation(needsSeparation, contextKnown, engine) {
    engine = engine || {};
    if (!needsSeparation) {
      return {
        kind: 'not-needed',
        text: contextKnown
          ? 'The managed local Stem Splitter server is not required for the current selection.'
          : 'The managed local Stem Splitter server is only required when selected audio is not already saved.',
      };
    }
    return {
      kind: engine.ready ? 'ready' : 'not-ready',
      text: engine.ready
        ? 'Managed local Stem Splitter server ready — '
          + (engine.reason || 'temporary separation available')
        : 'Temporary separation unavailable — '
          + (engine.reason || 'start the managed local server in Stem Splitter'),
    };
  }

  function nextTabIndex(current, key, count) {
    if (key === 'ArrowRight' || key === 'ArrowDown') return (current + 1) % count;
    if (key === 'ArrowLeft' || key === 'ArrowUp') return (current + count - 1) % count;
    if (key === 'Home') return 0;
    if (key === 'End') return count - 1;
    return null;
  }

  function createApiClient(request) {
    function post(path, payload) {
      return request(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {}),
      });
    }
    return {
      status: function () { return request('/status'); },
      sources: function (query) { return request('/sources?q=' + encodeURIComponent(query || '')); },
      source: function (filename) {
        return request('/source?filename=' + encodeURIComponent(filename));
      },
      startExport: function (payload) { return post('/export', payload); },
      latestExport: function () { return request('/export/latest'); },
      exportStatus: function (jobId) { return request('/export/' + encodeURIComponent(jobId)); },
      cancelExport: function (jobId) {
        return post('/export/' + encodeURIComponent(jobId) + '/cancel');
      },
      startScan: function (payload) { return post('/batch/scan-jobs', payload); },
      scanStatus: function (jobId) {
        return request('/batch/scan-jobs/' + encodeURIComponent(jobId));
      },
      cancelScan: function (jobId) {
        return post('/batch/scan-jobs/' + encodeURIComponent(jobId) + '/cancel');
      },
      startBatch: function (payload) { return post('/batch/start', payload); },
      latestBatch: function () { return request('/batch/latest'); },
      batchStatus: function (jobId) { return request('/batch/' + encodeURIComponent(jobId)); },
      cancelBatch: function (jobId) {
        return post('/batch/' + encodeURIComponent(jobId) + '/cancel');
      },
    };
  }

  if (!root || !root.document) {
    if (typeof module !== 'undefined' && module.exports) {
      module.exports = {
        sourceResultIsCurrent, resolvedSourceSelection,
        engineStatusPresentation, nextTabIndex, createApiClient,
      };
    }
    return;
  }
  var window = root;
  var document = root.document;
  if (window.__minusMixLoaded) return;
  window.__minusMixLoaded = true;

  var API = '/api/plugins/minus_mix';
  var SCREEN_ID = 'plugin-minus_mix';
  var STORAGE_OUTPUT = 'minus_mix.output_dir';
  var STORAGE_BATCH_INPUT = 'minus_mix.batch_input_dir';
  var STORAGE_BATCH_OUTPUT = 'minus_mix.batch_output_dir';
  var STORAGE_BATCH_LAYOUT = 'minus_mix.batch_output_layout';
  var STORAGE_MODE = 'minus_mix.mode';
  var BATCH_POLL_MS = 2000;
  var BACKGROUND_POLL_MS = 15000;
  var POLL_RETRY_MAX_MS = 30000;
  var fb = window.feedBack;
  var state = {
    inited: false, busy: false, selectedFilename: '', selectedLabel: '',
    sourceInfo: null, searchTimer: null, sourceRequestId: 0, ffmpegAvailable: true,
    statusRetryTimer: null, statusLoading: false,
    singleJobId: '', singlePollTimer: null, singleNotifiedId: '',
    batchScan: null, batchScanKey: '', batchJobId: '', batchPollTimer: null,
    batchScanJobId: '', batchScanPollTimer: null, batchScanRequestKey: '',
    batchBusy: false, batchActive: false, batchNotifiedId: '',
    batchPollLoading: false, batchPollFailures: 0,
    batchRenderKey: '', batchItemRows: Object.create(null),
    separation: {
      available: false, ready: false,
      reason: 'Checking Stem Splitter\'s managed local server…',
    },
  };

  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function setNodeText(node, value) {
    if (!node) return;
    value = String(value == null ? '' : value);
    if (node.textContent !== value) node.textContent = value;
  }
  function setNodeHtml(node, value) {
    if (!node || node.innerHTML === value) return;
    node.innerHTML = value;
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
  var apiClient = createApiClient(jsonFetch);
  function notify(title, message, accent) {
    try {
      if (window.fbNotify && window.fbNotify.show) {
        window.fbNotify.show({ title: title, message: message || '', accent: accent || 'info' });
        return;
      }
    } catch (_) {}
    console.log('[minus_mix]', title, message || '');
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
  function batchPreservesStructure() {
    var selected = document.querySelector('input[name="pmx-batch-layout"]:checked');
    return !selected || selected.value === 'preserve';
  }
  function storedValue(key, fallback) {
    try { return localStorage.getItem(key) || fallback; }
    catch (_) { return fallback; }
  }

  function screenIsVisible() {
    var screen = document.getElementById(SCREEN_ID);
    return !!(screen && screen.classList.contains('active') && !document.hidden);
  }

  function pauseScreenPolling() {
    clearTimeout(state.statusRetryTimer);
    clearTimeout(state.singlePollTimer);
    clearTimeout(state.batchPollTimer);
    clearTimeout(state.batchScanPollTimer);
    state.statusRetryTimer = null;
    state.singlePollTimer = null;
    state.batchPollTimer = null;
    state.batchScanPollTimer = null;
    // Keep a low-frequency job heartbeat so completion notifications still
    // work on other screens without continuously updating the hidden UI.
    scheduleSinglePoll(BACKGROUND_POLL_MS);
    scheduleBatchPoll(BACKGROUND_POLL_MS);
    scheduleBatchScanPoll(BACKGROUND_POLL_MS);
  }

  function scheduleSinglePoll(delay) {
    clearTimeout(state.singlePollTimer);
    state.singlePollTimer = null;
    if (state.busy && state.singleJobId) {
      state.singlePollTimer = setTimeout(pollSingleExport,
        screenIsVisible() ? delay : Math.max(delay, BACKGROUND_POLL_MS));
    }
  }

  function scheduleBatchPoll(delay) {
    clearTimeout(state.batchPollTimer);
    state.batchPollTimer = null;
    if (state.batchActive && state.batchJobId) {
      state.batchPollTimer = setTimeout(pollBatch,
        screenIsVisible() ? delay : Math.max(delay, BACKGROUND_POLL_MS));
    }
  }

  function scheduleBatchScanPoll(delay) {
    clearTimeout(state.batchScanPollTimer);
    state.batchScanPollTimer = null;
    if (state.batchBusy && state.batchScanJobId) {
      state.batchScanPollTimer = setTimeout(pollBatchScan,
        screenIsVisible() ? delay : Math.max(delay, BACKGROUND_POLL_MS));
    }
  }

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
    var batchMode = !!($('pmx-batch-panel') && !$('pmx-batch-panel').hidden);
    var currentScan = state.batchScan && state.batchScanKey === batchOptionsKey();
    var contextKnown = batchMode ? !!currentScan : !!state.sourceInfo;
    var needsSeparation = batchMode
      ? !!(currentScan && currentScan.counts && currentScan.counts.needs_separation)
      : selectionNeedsSeparation(selectedStems());
    var presentation = engineStatusPresentation(needsSeparation, contextKnown, engine);
    root.className = 'pmx-engine ' + presentation.kind;
    text.textContent = presentation.text;
  }

  function renderSourceWarning(info) {
    var node = $('pmx-source-warning');
    if (!node) return;
    var exclusions = info && Array.isArray(info.derived_exclusions) ? info.derived_exclusions : [];
    node.hidden = !exclusions.length;
    node.textContent = exclusions.length
      ? 'This song was already created by MinusMix with ' + exclusions.map(labelFor).join(' + ')
        + ' removed. For the best audio quality, select the original FeedPak instead of separating this copy again.'
      : '';
  }

  function updateReady() {
    renderEngineStatus();
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
      title.textContent = 'Creating MinusMix FeedPak…';
      detail.textContent = (needsSeparation ? 'Temporarily separating, then rendering' : 'Rendering')
        + ' the full mix minus ' + stems.map(labelFor).join(' + ') + '. The source remains untouched.';
    } else if (ready) {
      title.textContent = 'Create “No ' + stems.map(labelFor).join(' + ') + '” copy';
      detail.textContent = 'Single-stem output • native backing playback • source preserved'
        + (needsSeparation ? ' • temporary separation' : ' • existing stem reused');
    } else if (needsSeparation && !(state.separation && state.separation.ready)) {
      title.textContent = 'Start the managed local Stem Splitter server first';
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
    var sourceRequestId = state.sourceRequestId;
    state.selectedFilename = filename;
    state.sourceInfo = null;
    renderSourceWarning(null);
    renderStems(null);
    updateReady();
    apiClient.source(filename).then(function (info) {
      if (state.selectedFilename !== filename || state.sourceRequestId !== sourceRequestId) return;
      state.sourceInfo = info;
      renderStems(info);
      renderSourceWarning(info);
      showStatus('info', 'Selected ' + (info.artist ? info.artist + ' — ' : '') + info.title + '.');
    }).catch(function (error) {
      if (state.selectedFilename !== filename || state.sourceRequestId !== sourceRequestId) return;
      state.sourceInfo = null;
      renderSourceWarning(null);
      renderStems(null);
      showStatus('error', error.message);
    });
  }

  function clearSourceSelection() {
    state.selectedFilename = '';
    state.selectedLabel = '';
    state.sourceInfo = null;
    renderSourceWarning(null);
    renderStems(null);
    updateReady();
  }

  function loadSources(preselect) {
    var select = $('pmx-source');
    if (!select) return Promise.resolve();
    var requestId = ++state.sourceRequestId;
    var explicitPreselect = typeof preselect === 'string' && preselect ? preselect : '';
    var q = (($('pmx-search') && $('pmx-search').value) || '').trim();
    // A new result set invalidates any in-flight source-detail response and
    // temporarily disables export until the visible selection is reconciled.
    state.sourceInfo = null;
    renderSourceWarning(null);
    renderStems(null);
    updateReady();
    select.disabled = true;
    select.innerHTML = '<option>Loading feedpaks…</option>';
    return apiClient.sources(q).then(function (data) {
      if (!sourceResultIsCurrent(requestId, state.sourceRequestId)) return;
      var songs = data.songs || [];
      select.innerHTML = songs.map(function (song) {
        var text = (song.artist ? song.artist + ' — ' : '') + song.title;
        return '<option value="' + esc(song.filename) + '">' + esc(text) + '</option>';
      }).join('');
      select.disabled = false;
      if (!songs.length) {
        select.innerHTML = '<option disabled>No feedpaks found</option>';
        clearSourceSelection();
        return;
      }
      var resolved = resolvedSourceSelection(songs, explicitPreselect, state.selectedFilename);
      if (resolved && songs.some(function (song) { return song.filename === resolved; })) {
        select.value = resolved;
        chooseSource(resolved);
      } else if (resolved) {
        // A card action is authoritative even when this screen's source list is
        // capped for a very large library. Keep the selected song usable rather
        // than silently losing it because it sorted after the first 250 rows.
        var option = document.createElement('option');
        option.value = resolved;
        option.textContent = state.selectedLabel || resolved;
        select.insertBefore(option, select.firstChild);
        select.value = resolved;
        chooseSource(resolved);
      } else {
        select.selectedIndex = -1;
        clearSourceSelection();
      }
    }).catch(function (error) {
      if (!sourceResultIsCurrent(requestId, state.sourceRequestId)) return;
      select.innerHTML = '<option disabled>Could not load songs</option>';
      select.disabled = false;
      clearSourceSelection();
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
      id: 'minus_mix.create',
      pluginId: 'minus_mix',
      label: 'Create MinusMix song…',
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
    setNodeText($('pmx-single-progress-title'), job.status === 'completed'
      ? 'MinusMix FeedPak created' : job.status === 'canceled' ? 'Export canceled'
        : job.status === 'failed' ? 'Export failed' : stageLabel(job.stage));
    setNodeText($('pmx-single-current'), stageLabel(job.stage)
      + (job.detail ? ' — ' + job.detail : ''));
    if ($('pmx-single-progress-bar')) {
      $('pmx-single-progress-bar').value = Number(job.progress || 0);
    }
    if ($('pmx-single-cancel')) {
      $('pmx-single-cancel').disabled = !active || job.status === 'canceling';
    }

    if (active) {
      scheduleSinglePoll(1000);
    } else {
      clearTimeout(state.singlePollTimer);
      state.singlePollTimer = null;
    }
    if (!active && job.status === 'completed' && job.result) {
      var result = job.result;
      var temporary = result.temporary_separation_used ? ' Temporary separator files were deleted.' : '';
      var preview = result.preview_created === false
        ? ' The FeedPak was created successfully, but its optional preview could not be generated.' : '';
      showStatus('ok', 'Created ' + result.filename + ' at ' + result.path
        + '. The original feedpak was not changed.' + temporary + preview);
      if (job.id && state.singleNotifiedId !== job.id) {
        state.singleNotifiedId = job.id;
        notify('MinusMix FeedPak created', result.filename, 'ok');
      }
    } else if (!active && job.status === 'canceled') {
      showStatus('info', 'Export canceled safely. The source FeedPak was not changed.');
    } else if (!active && job.status === 'failed') {
      showStatus('error', job.detail || 'MinusMix export failed.');
      if (job.id && state.singleNotifiedId !== job.id) {
        state.singleNotifiedId = job.id;
        notify('MinusMix export failed', job.detail || 'Export failed', 'warn');
      }
    }
    updateReady();
    updateBatchReady();
  }

  function pollSingleExport() {
    if (!state.singleJobId) return;
    apiClient.exportStatus(state.singleJobId).then(renderSingleJob).catch(function (error) {
      showStatus('error', 'Could not refresh export status: ' + error.message);
      scheduleSinglePoll(2500);
    });
  }

  function loadLatestSingleExport() {
    apiClient.latestExport().then(function (data) {
      if (data.job) renderSingleJob(data.job);
    }).catch(function () {});
  }

  function cancelSingleExport() {
    if (!state.singleJobId || !state.busy) return;
    if ($('pmx-single-cancel')) $('pmx-single-cancel').disabled = true;
    showStatus('info', 'Cancel requested. The current operation is stopping safely…');
    apiClient.cancelExport(state.singleJobId).then(renderSingleJob).catch(function (error) {
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
      $('pmx-mode-single').setAttribute('tabindex', batch ? '-1' : '0');
    }
    if ($('pmx-mode-batch')) {
      $('pmx-mode-batch').classList.toggle('active', batch);
      $('pmx-mode-batch').setAttribute('aria-selected', batch ? 'true' : 'false');
      $('pmx-mode-batch').setAttribute('tabindex', batch ? '0' : '-1');
    }
    try { localStorage.setItem(STORAGE_MODE, batch ? 'batch' : 'single'); } catch (_) {}
    renderEngineStatus();
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
      preserve_structure: batchPreservesStructure(),
    };
  }

  function batchOptionsKey() { return JSON.stringify(batchOptions()); }

  function invalidateBatchScan() {
    if (state.batchActive) return;
    state.batchScan = null;
    state.batchScanKey = '';
    updateBatchReady();
  }

  function updateBatchLayoutHelp() {
    var help = $('pmx-batch-layout-help');
    if (!help) return;
    help.textContent = batchPreservesStructure()
      ? 'Source subfolders will be recreated below the output folder. Sources are read-only and existing output files are never overwritten.'
      : 'Flat output is selected. Name collisions receive a numbered filename. Sources are read-only and existing output files are never overwritten.';
  }

  function updateBatchReady() {
    renderEngineStatus();
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
      'pmx-batch-skip-existing', 'pmx-batch-skip-derived',
      'pmx-batch-layout-flat', 'pmx-batch-layout-preserve'].forEach(function (id) {
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
        detail.textContent += ' • start the managed local Stem Splitter server before starting';
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

  function batchItemKey(item, index) {
    return String(item.relative_path || item.output_relative || item.title || ('Feedpak ' + index));
  }

  function resetBatchItems(renderKey) {
    var root = $('pmx-batch-items');
    if (root) root.textContent = '';
    state.batchRenderKey = renderKey || '';
    state.batchItemRows = Object.create(null);
  }

  function createBatchItemRow() {
    var row = document.createElement('div');
    var statusNode = document.createElement('b');
    var body = document.createElement('div');
    var nameNode = document.createElement('span');
    var detailNode = document.createElement('small');
    body.appendChild(nameNode);
    body.appendChild(detailNode);
    row.appendChild(statusNode);
    row.appendChild(body);
    row._minusMixNodes = { status: statusNode, name: nameNode, detail: detailNode };
    row._minusMixSignature = '';
    return row;
  }

  function updateBatchItemRow(row, item, scanMode) {
    var status = String(scanMode ? (item.scan_status || 'ready') : (item.status || 'queued'));
    var detail = String(item.detail || item.reason
      || (item.needs_separation ? 'Temporary separation' : 'Saved stem'));
    var name = String(item.relative_path || item.title || 'Feedpak');
    var signature = status + '\u0000' + name + '\u0000' + detail;
    if (row._minusMixSignature === signature) return;
    row._minusMixSignature = signature;
    row.className = 'pmx-batch-item ' + status.toLowerCase().replace(/[^a-z0-9_-]/g, '');
    row._minusMixNodes.status.textContent = status;
    row._minusMixNodes.name.textContent = name;
    row._minusMixNodes.detail.textContent = detail;
  }

  function renderBatchItems(items, scanMode, renderKey) {
    var root = $('pmx-batch-items');
    if (!root) return;
    items = Array.isArray(items) ? items : [];
    renderKey = renderKey || (scanMode ? 'scan' : 'job');
    if (state.batchRenderKey !== renderKey) resetBatchItems(renderKey);

    if (!items.length) {
      if (!root.firstChild || !root.firstChild.classList.contains('pmx-empty')) {
        root.textContent = '';
        var empty = document.createElement('p');
        empty.className = 'pmx-help pmx-empty';
        empty.textContent = 'No per-file results to show.';
        root.appendChild(empty);
      }
      state.batchItemRows = Object.create(null);
      return;
    }

    var seen = Object.create(null);
    var entries = items.map(function (item, index) {
      var key = batchItemKey(item, index);
      seen[key] = true;
      return { key: key, item: item };
    });

    Object.keys(state.batchItemRows).forEach(function (key) {
      if (seen[key]) return;
      var stale = state.batchItemRows[key];
      if (stale.parentNode === root) root.removeChild(stale);
      delete state.batchItemRows[key];
    });
    var emptyNode = root.querySelector('.pmx-empty');
    if (emptyNode) emptyNode.remove();

    var previous = null;
    entries.forEach(function (entry) {
      var row = state.batchItemRows[entry.key];
      if (!row) {
        row = createBatchItemRow();
        state.batchItemRows[entry.key] = row;
      }
      updateBatchItemRow(row, entry.item, scanMode);
      var expected = previous ? previous.nextSibling : root.firstChild;
      if (row !== expected) root.insertBefore(row, expected);
      previous = row;
    });
  }

  function renderBatchItemsNote(payload, scanMode) {
    var note = $('pmx-batch-items-note');
    if (!note) return;
    var shown = Array.isArray(payload.items) ? payload.items.length : 0;
    var total = Number(payload.items_total || shown);
    note.hidden = !payload.items_truncated;
    note.textContent = payload.items_truncated
      ? (scanMode ? 'Showing the first ' : 'Showing the most relevant ')
        + shown + ' of ' + total + ' per-file results. Summary counts include the whole batch.'
      : '';
  }

  function renderBatchHeartbeat(kind, text) {
    var node = $('pmx-batch-updated');
    if (!node) return;
    node.className = 'pmx-batch-updated ' + (kind || 'ok');
    node.textContent = text || '';
  }

  function refreshedAtText(active) {
    var time = new Date().toLocaleTimeString();
    return (active ? 'Live status checked ' : 'Final status confirmed ') + time
      + (active ? ' · Processing continues safely if you leave this screen.' : '');
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
    if ($('pmx-batch-progress-bar')) $('pmx-batch-progress-bar').value = 1;
    var counts = scan.counts || {};
    if ($('pmx-batch-counts')) {
      $('pmx-batch-counts').innerHTML = '<span>' + (counts.ready || 0) + ' ready</span>'
        + '<span>' + (counts.needs_separation || 0) + ' AI splits</span>'
        + '<span>' + (counts.uses_saved_stems || 0) + ' saved stems</span>'
        + '<span>' + ((counts.skipped_existing || 0) + (counts.skipped_derived || 0)) + ' skipped</span>'
        + '<span>' + (counts.invalid || 0) + ' invalid</span>';
    }
    if ($('pmx-batch-cancel')) $('pmx-batch-cancel').disabled = true;
    renderBatchItems(scan.items, true, 'scan:' + (optionsKey || batchOptionsKey()));
    renderBatchItemsNote(scan, true);
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
    state.batchScanRequestKey = optionsKey;
    apiClient.startScan(options).then(function (job) {
      state.batchScanJobId = job.id || '';
      renderBatchScanJob(job);
    }).catch(function (error) {
      showBatchStatus('error', error.message);
      state.batchBusy = false;
      updateBatchReady();
    });
  }

  function renderBatchScanJob(job) {
    if (!job) return;
    state.batchScanJobId = job.id || state.batchScanJobId;
    var active = ['queued', 'running', 'canceling'].indexOf(job.status) >= 0;
    state.batchBusy = active;
    if ($('pmx-batch-progress')) $('pmx-batch-progress').hidden = false;
    setNodeText($('pmx-batch-progress-title'), active ? 'Scanning folder'
      : job.status === 'completed' ? 'Scan complete'
        : job.status === 'canceled' ? 'Scan canceled' : 'Scan stopped');
    setNodeText($('pmx-batch-current'), job.detail || 'Reading FeedPak manifests');
    if ($('pmx-batch-progress-bar')) {
      $('pmx-batch-progress-bar').value = Number(job.progress || 0);
    }
    setNodeHtml($('pmx-batch-counts'), active ? '<span>Sources remain read-only</span>' : '');
    if ($('pmx-batch-cancel')) {
      $('pmx-batch-cancel').disabled = !active || job.status === 'canceling';
    }
    if ($('pmx-batch-refresh')) $('pmx-batch-refresh').disabled = active;
    updateBatchReady();

    if (active) {
      scheduleBatchScanPoll(500);
      return;
    }
    clearTimeout(state.batchScanPollTimer);
    state.batchScanPollTimer = null;
    state.batchScanJobId = '';
    if (job.status === 'completed' && job.result) {
      if (batchOptionsKey() === state.batchScanRequestKey) {
        renderBatchScan(job.result, state.batchScanRequestKey);
        showBatchStatus('ok', 'Scan complete. Sources remain untouched.');
      } else {
        showBatchStatus('info', 'Folder options changed while scanning. Scan again for an up-to-date preview.');
      }
    } else if (job.status === 'canceled') {
      showBatchStatus('info', 'Folder scan canceled. No source files were changed.');
    } else {
      showBatchStatus('error', job.detail || 'Folder scan failed.');
    }
    updateBatchReady();
  }

  function pollBatchScan() {
    if (!state.batchScanJobId) return;
    apiClient.scanStatus(state.batchScanJobId)
      .then(renderBatchScanJob)
      .catch(function (error) {
        showBatchStatus('error', 'Could not refresh folder scan: ' + error.message);
        scheduleBatchScanPoll(2000);
      });
  }

  function renderBatchJob(job) {
    if (!job) return;
    state.batchJobId = job.id || '';
    var active = ['queued', 'running', 'canceling'].indexOf(job.status) >= 0;
    state.batchActive = active;
    state.batchPollFailures = 0;
    var progress = $('pmx-batch-progress');
    if (progress) progress.hidden = false;
    setNodeText($('pmx-batch-progress-title'), job.status === 'completed'
      ? 'Batch completed' : job.status === 'canceled' ? 'Batch canceled'
        : job.status === 'failed' || job.status === 'interrupted' ? 'Batch stopped' : 'Batch running');
    if ($('pmx-batch-current')) {
      var runningItem = (job.items || []).find(function (item) { return item.status === 'running'; });
      var position = job.current_item_number
        ? 'Song ' + job.current_item_number + ' of ' + (job.items_total || (job.items || []).length) + ' — '
        : '';
      setNodeText($('pmx-batch-current'), job.current_relative_path
        ? position + job.current_relative_path + ' — '
          + stageLabel((runningItem && runningItem.stage) || job.status)
          + (job.detail ? ': ' + job.detail : '')
        : (job.detail || ''));
    }
    if ($('pmx-batch-progress-bar')) $('pmx-batch-progress-bar').value = Number(job.overall_progress || 0);
    var counts = job.counts || {};
    setNodeHtml($('pmx-batch-counts'), '<span>' + (counts.done || 0) + ' created</span>'
      + '<span>' + (counts.queued || 0) + ' waiting</span>'
      + '<span>' + (counts.skipped || 0) + ' skipped</span>'
      + '<span>' + (counts.failed || 0) + ' failed</span>'
      + '<span>' + (counts.duplicate_audio_reused || 0) + ' duplicate splits reused</span>'
      + ((counts.preview_failures || 0)
        ? '<span>' + counts.preview_failures + ' without preview</span>' : ''));
    if ($('pmx-batch-cancel')) $('pmx-batch-cancel').disabled = !active || job.status === 'canceling';
    if ($('pmx-batch-refresh')) $('pmx-batch-refresh').disabled = false;
    renderBatchItems(job.items, false, 'job:' + (job.id || 'latest'));
    renderBatchItemsNote(job, false);
    renderBatchHeartbeat('ok', refreshedAtText(active));
    updateReady();
    updateBatchReady();

    if (active) {
      scheduleBatchPoll(BATCH_POLL_MS);
    } else {
      clearTimeout(state.batchPollTimer);
      state.batchPollTimer = null;
    }
    if (!active && job.id && state.batchNotifiedId !== job.id) {
      state.batchNotifiedId = job.id;
      if (job.status === 'completed') {
        showBatchStatus((counts.failed || 0) ? 'error' : 'ok', job.detail || 'Batch completed.');
        notify('MinusMix batch completed', (counts.done || 0) + ' FeedPaks created', (counts.failed || 0) ? 'warn' : 'ok');
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
    var manual = arguments[0] === true;
    if (!state.batchJobId || state.batchPollLoading) return;
    state.batchPollLoading = true;
    if ($('pmx-batch-refresh')) $('pmx-batch-refresh').disabled = true;
    apiClient.batchStatus(state.batchJobId).then(renderBatchJob).catch(function (error) {
      state.batchPollFailures += 1;
      var delay = Math.min(POLL_RETRY_MAX_MS,
        2500 * Math.pow(2, Math.min(state.batchPollFailures - 1, 3)));
      renderBatchHeartbeat('error', 'Status refresh failed at ' + new Date().toLocaleTimeString()
        + '. The conversion may still be running; retrying automatically.');
      if (manual) showBatchStatus('error', 'Could not refresh batch status: ' + error.message);
      scheduleBatchPoll(delay);
    }).finally(function () {
      state.batchPollLoading = false;
      if ($('pmx-batch-refresh')) $('pmx-batch-refresh').disabled = false;
    });
  }

  function loadLatestBatch() {
    apiClient.latestBatch().then(function (data) {
      if (data.job) {
        if (['queued', 'running', 'canceling'].indexOf(data.job.status) < 0) {
          state.batchNotifiedId = data.job.id;
        }
        renderBatchJob(data.job);
      }
    }).catch(function (error) {
      renderBatchHeartbeat('error', 'Could not restore the latest batch status: ' + error.message);
    });
  }

  function startBatch() {
    if (state.batchBusy || state.batchActive || !state.batchScan
        || state.batchScanKey !== batchOptionsKey()) return;
    state.batchBusy = true;
    updateBatchReady();
    showBatchStatus('info', 'Starting sequential conversion. Processing one song at a time for GPU stability…');
    var options = batchOptions();
    options.scan_id = state.batchScan.scan_id || '';
    apiClient.startBatch(options).then(function (job) {
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
    if (state.batchBusy && state.batchScanJobId) {
      $('pmx-batch-cancel').disabled = true;
      showBatchStatus('info', 'Canceling folder scan safely…');
      apiClient.cancelScan(state.batchScanJobId).then(renderBatchScanJob).catch(function (error) {
        showBatchStatus('error', error.message);
        $('pmx-batch-cancel').disabled = false;
      });
      return;
    }
    if (!state.batchJobId || !state.batchActive) return;
    $('pmx-batch-cancel').disabled = true;
    showBatchStatus('info', 'Cancel requested. The current operation will stop at a safe checkpoint…');
    apiClient.cancelBatch(state.batchJobId).then(renderBatchJob).catch(function (error) {
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
    apiClient.startExport({
      filename: state.selectedFilename,
      excluded_stems: stems,
      output_dir: outputFolder(),
    }).then(function (job) {
      state.singleNotifiedId = '';
      renderSingleJob(job);
    }).catch(function (error) {
      showStatus('error', error.message);
      notify('MinusMix export failed', error.message, 'warn');
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
    return apiClient.status().then(function (status) {
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
      if (text) {
        text.textContent = 'Could not refresh the managed local Stem Splitter server status — '
          + error.message;
      }
    }).finally(function () {
      state.statusLoading = false;
      if ($('pmx-engine-refresh')) $('pmx-engine-refresh').disabled = false;
      if (screenIsVisible()) {
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
    var batchInput = '', batchOutput = '', batchLayout = 'preserve', savedMode = 'single';
    saved = storedValue(STORAGE_OUTPUT, '');
    batchInput = storedValue(STORAGE_BATCH_INPUT, '');
    batchOutput = storedValue(STORAGE_BATCH_OUTPUT, '');
    batchLayout = storedValue(STORAGE_BATCH_LAYOUT, 'preserve');
    savedMode = storedValue(STORAGE_MODE, 'single');
    if ($('pmx-output')) $('pmx-output').value = saved;
    if ($('pmx-batch-input')) $('pmx-batch-input').value = batchInput;
    if ($('pmx-batch-output')) $('pmx-batch-output').value = batchOutput;
    if (batchLayout === 'flat' && $('pmx-batch-layout-flat')) {
      $('pmx-batch-layout-flat').checked = true;
    } else if ($('pmx-batch-layout-preserve')) {
      $('pmx-batch-layout-preserve').checked = true;
    }
    updateBatchLayoutHelp();
    setMode(savedMode === 'batch' ? 'batch' : 'single');
    $('pmx-source').addEventListener('change', function () { chooseSource(this.value); });
    $('pmx-refresh').addEventListener('click', function () { loadSources(); refreshStatus(); });
    $('pmx-engine-refresh').addEventListener('click', refreshStatus);
    $('pmx-browse').addEventListener('click', browseOutput);
    $('pmx-export').addEventListener('click', createExport);
    $('pmx-single-cancel').addEventListener('click', cancelSingleExport);
    $('pmx-mode-single').addEventListener('click', function () { setMode('single'); });
    $('pmx-mode-batch').addEventListener('click', function () { setMode('batch'); });
    document.querySelector('.pmx-tabs').addEventListener('keydown', function (event) {
      var tabs = [$('pmx-mode-single'), $('pmx-mode-batch')];
      var current = tabs.indexOf(document.activeElement);
      if (current < 0) return;
      var next = nextTabIndex(current, event.key, tabs.length);
      if (next === null) return;
      event.preventDefault();
      setMode(next === 1 ? 'batch' : 'single');
      tabs[next].focus();
    });
    $('pmx-batch-input-browse').addEventListener('click', function () {
      browseBatchFolder('pmx-batch-input', STORAGE_BATCH_INPUT);
    });
    $('pmx-batch-output-browse').addEventListener('click', function () {
      browseBatchFolder('pmx-batch-output', STORAGE_BATCH_OUTPUT);
    });
    $('pmx-batch-scan').addEventListener('click', scanBatch);
    $('pmx-batch-start').addEventListener('click', startBatch);
    $('pmx-batch-cancel').addEventListener('click', cancelBatch);
    $('pmx-batch-refresh').addEventListener('click', function () {
      if (state.batchJobId) pollBatch(true);
      else loadLatestBatch();
    });
    ['pmx-batch-recursive', 'pmx-batch-skip-existing', 'pmx-batch-skip-derived'].forEach(function (id) {
      $(id).addEventListener('change', invalidateBatchScan);
    });
    ['pmx-batch-layout-flat', 'pmx-batch-layout-preserve'].forEach(function (id) {
      $(id).addEventListener('change', function () {
        if (!this.checked) return;
        try { localStorage.setItem(STORAGE_BATCH_LAYOUT, this.value); } catch (_) {}
        updateBatchLayoutHelp();
        invalidateBatchScan();
      });
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
    if (id === SCREEN_ID) {
      if (!state.inited) {
        wireScreen();
        return;
      }
      // Some host builds emit screen:changed immediately before applying the
      // active class. Deferring one tick makes resume reliable in both orders.
      setTimeout(function () {
        if (!screenIsVisible()) return;
        refreshStatus();
        loadLatestSingleExport();
        loadLatestBatch();
        if (state.batchBusy && state.batchScanJobId) pollBatchScan();
      }, 0);
    } else {
      pauseScreenPolling();
    }
  }

  function onVisibilityChanged() {
    if (!screenIsVisible()) {
      pauseScreenPolling();
      return;
    }
    refreshStatus();
    loadLatestSingleExport();
    loadLatestBatch();
    if (state.batchBusy && state.batchScanJobId) pollBatchScan();
  }

  function boot() {
    registerCardAction();
    if (fb && fb.on) fb.on('screen:changed', onScreenChanged);
    document.addEventListener('visibilitychange', onVisibilityChanged);
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
})(typeof window !== 'undefined' ? window : globalThis);
