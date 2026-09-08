"""Public identity contract for the standalone plugin repository."""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from scripts.build_release import build_release


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


def test_release_archive_contains_the_license(tmp_path):
    root = Path(__file__).resolve().parents[1]
    license_text = (root / "LICENSE").read_text(encoding="utf-8")

    archive_path = build_release(root, tmp_path)

    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Viktor Olausson" in license_text
    with zipfile.ZipFile(archive_path) as archive:
        archived_license = archive.read("minus_mix/LICENSE").decode("utf-8")
    assert archived_license.replace("\r\n", "\n") == license_text.replace("\r\n", "\n")


def test_frontend_sources_do_not_contain_known_mojibake():
    root = Path(__file__).resolve().parents[1]
    script = (root / "screen.js").read_text(encoding="utf-8")

    assert "â€¦" not in script
    assert "�" not in script


def test_reuse_loader_requests_a_host_served_asset_in_the_release(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = (root / "screen.js").read_text(encoding="utf-8")
    match = re.search(r"new URL\('([^']*reuse_screen\.js)', base\)", script)
    assert match, "The reuse tab must declare its helper script URL"
    relative = match.group(1)
    with zipfile.ZipFile(build_release(root, tmp_path)) as archive:
        for prefix in ("/api/plugins/minus_mix/", "/api/plugins/minus_mix/g/2/"):
            url = urlsplit(urljoin("http://localhost" + prefix + "screen.js?v=0.7.0", relative))
            # The host serves arbitrary plugin scripts only under assets/ or
            # src/. Sibling files beside screen.js are not an HTTP surface.
            assert url.path.startswith(prefix + "assets/")
            member = "minus_mix/" + url.path.removeprefix(prefix)
            assert archive.read(member) == (root / relative).read_bytes()

