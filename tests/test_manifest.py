"""Public identity contract for the standalone plugin repository."""
from __future__ import annotations

import json
from pathlib import Path


def test_public_identity_is_minus_mix_and_not_bundled():
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "plugin.json").read_text(encoding="utf-8")
    )

    assert manifest["id"] == "minus_mix"
    assert manifest["name"] == "MinusMix"
    assert manifest["nav"] == {"label": "MinusMix", "screen": "plugin-minus_mix"}
    assert manifest.get("bundled") is not True

