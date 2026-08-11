"""Public identity contract for the standalone plugin repository."""
from __future__ import annotations

import json
import re
from pathlib import Path


def test_public_identity_is_minus_mix_and_not_bundled():
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "plugin.json").read_text(encoding="utf-8")
    )

    assert manifest["id"] == "minus_mix"
    assert manifest["name"] == "MinusMix"
    assert manifest["category"] == "tools"
    assert manifest["nav"] == {"label": "MinusMix", "screen": "plugin-minus_mix"}
    assert manifest.get("bundled") is not True


def test_batch_output_layout_defaults_to_preserving_source_folders():
    html = (Path(__file__).resolve().parents[1] / "screen.html").read_text(encoding="utf-8")
    preserve = re.search(r'<input id="pmx-batch-layout-preserve"[^>]*>', html)
    flat = re.search(r'<input id="pmx-batch-layout-flat"[^>]*>', html)

    assert preserve and re.search(r"\bchecked\b", preserve.group(0))
    assert flat and not re.search(r"\bchecked\b", flat.group(0))


def test_screen_exposes_keyboard_and_accessibility_semantics():
    root = Path(__file__).resolve().parents[1]
    html = (root / "screen.html").read_text(encoding="utf-8")
    script = (root / "screen.js").read_text(encoding="utf-8")
    css = (root / "assets" / "plugin.css").read_text(encoding="utf-8")

    assert 'aria-controls="pmx-single-panel"' in html
    assert 'aria-controls="pmx-batch-panel"' in html
    assert 'aria-labelledby="pmx-mode-single"' in html
    assert 'aria-labelledby="pmx-mode-batch"' in html
    assert html.count("aria-labelledby=\"pmx-") >= 4
    assert html.count('<fieldset class="pmx-stem-options">') == 2
    assert "nextTabIndex(current, event.key, tabs.length)" in script
    assert "setAttribute('tabindex'" in script
    assert "/batch/scan-jobs" in script
    assert ":focus-visible" in css

