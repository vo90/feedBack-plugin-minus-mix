"""Behavior tests for pure MinusMix UI state decisions executed by Node."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not NODE, reason="Node.js is required for frontend behavior tests")
def test_search_engine_and_tab_state_helpers():
    script = r"""
const helpers = require('./screen.js');
const songs = [{filename: 'new.feedpak'}];
process.stdout.write(JSON.stringify({
  current: helpers.sourceResultIsCurrent(4, 4),
  stale: helpers.sourceResultIsCurrent(3, 4),
  emptySelection: helpers.resolvedSourceSelection([], '', 'old.feedpak'),
  missingSelection: helpers.resolvedSourceSelection(songs, '', 'old.feedpak'),
  visibleSelection: helpers.resolvedSourceSelection(songs, '', 'new.feedpak'),
  cardSelection: helpers.resolvedSourceSelection(songs, 'card.feedpak', 'old.feedpak'),
  quietEngine: helpers.engineStatusPresentation(false, true, {}).kind,
  requiredEngine: helpers.engineStatusPresentation(true, true, {ready: false}).kind,
  readyEngine: helpers.engineStatusPresentation(true, true, {ready: true}).kind,
  wrapRight: helpers.nextTabIndex(1, 'ArrowRight', 2),
  wrapLeft: helpers.nextTabIndex(0, 'ArrowLeft', 2),
  ignoredKey: helpers.nextTabIndex(0, 'Enter', 2),
}));
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT,
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "current": True,
        "stale": False,
        "emptySelection": "",
        "missingSelection": "",
        "visibleSelection": "new.feedpak",
        "cardSelection": "card.feedpak",
        "quietEngine": "not-needed",
        "requiredEngine": "not-ready",
        "readyEngine": "ready",
        "wrapRight": 0,
        "wrapLeft": 1,
        "ignoredKey": None,
    }


@pytest.mark.skipif(not NODE, reason="Node.js is required for frontend behavior tests")
def test_api_client_owns_paths_methods_and_json_encoding():
    script = r"""
const helpers = require('./screen.js');
const calls = [];
const client = helpers.createApiClient(function(path, options) {
  calls.push({path: path, options: options || null});
  return {path: path};
});
client.sources('artist & title');
client.source('folder/song.feedpak');
client.startExport({filename: 'song.feedpak'});
client.cancelExport('single/id');
client.startScan({recursive: true});
client.scanStatus('scan/id');
client.cancelScan('scan/id');
client.startBatch({scan_id: 'scan/id'});
client.batchStatus('batch/id');
client.cancelBatch('batch/id');
process.stdout.write(JSON.stringify(calls));
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT,
        text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = json.loads(result.stdout)
    assert [call["path"] for call in calls] == [
        "/sources?q=artist%20%26%20title",
        "/source?filename=folder%2Fsong.feedpak",
        "/export",
        "/export/single%2Fid/cancel",
        "/batch/scan-jobs",
        "/batch/scan-jobs/scan%2Fid",
        "/batch/scan-jobs/scan%2Fid/cancel",
        "/batch/start",
        "/batch/batch%2Fid",
        "/batch/batch%2Fid/cancel",
    ]
    assert calls[2]["options"] == {
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body": '{"filename":"song.feedpak"}',
    }
    assert calls[3]["options"]["body"] == "{}"
