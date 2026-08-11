"""Small host shims for testing this optional plugin outside FeedBack.

FeedBack provides ``audio`` and ``sloppak`` at runtime.  A standalone checkout
does not, so the tests install only the narrow pieces exercised by the export
suite.  When the real host modules are importable, they are used unchanged.
"""
from __future__ import annotations

import shutil
import sys
import types
import zipfile
from pathlib import Path

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))


try:
    import audio
except ImportError:
    audio = types.ModuleType("audio")
    audio._ffmpeg_cmd = lambda: shutil.which("ffmpeg")

    def _scrub_paths(text, *paths):
        for path in paths:
            text = text.replace(str(path), "<path>")
        return text

    audio._scrub_paths = _scrub_paths
    sys.modules["audio"] = audio


try:
    import sloppak
except ImportError:
    sloppak = types.ModuleType("sloppak")

    def _load_manifest(source):
        with zipfile.ZipFile(source) as archive:
            for name in ("manifest.yaml", "manifest.yml"):
                try:
                    return yaml.safe_load(archive.read(name))
                except KeyError:
                    continue
        raise KeyError("manifest.yaml")

    sloppak.load_manifest = _load_manifest
    sys.modules["sloppak"] = sloppak

