"""Independent integration checks for variant receipt and plan boundaries."""
from __future__ import annotations

import copy
import json

import pytest

import reuse_batch
from tests.reuse_fixtures import fast_resources, manager, package, wait_job


def reviewed_variant(tmp_path, monkeypatch, stems):
    fast_resources(monkeypatch)
    old, fresh, output = [tmp_path / name for name in ("old", "fresh", "output")]
    output.mkdir()
    package(old / "donor.feedpak", donor=True, excluded_stems=stems)
    package(fresh / "song.feedpak")
    instance = manager(tmp_path / "config")
    job = instance.start_scan(old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output))
    preview = wait_job(instance, job["id"])
    assert preview["counts"]["ready"] == 1, preview
    return instance, preview


def test_plan_headroom_includes_full_custom_variant_receipt(tmp_path, monkeypatch):
    # Normal export supports custom IDs; labels truncate while provenance keeps
    # exact IDs. The completed receipt can therefore be larger than 4 KiB.
    instance, _ = reviewed_variant(tmp_path, monkeypatch, ["custom_" + "x" * 10_000])
    initial = len(json.dumps(instance.job, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    monkeypatch.setattr(reuse_batch, "MAX_CHECKPOINT_BYTES", initial + 6000)
    with pytest.raises(reuse_batch.PreviewLimitError, match="size limit"):
        instance._check_plan_size()


def test_explicit_receipt_variant_must_match_approved_row(tmp_path, monkeypatch):
    instance, preview = reviewed_variant(tmp_path, monkeypatch, ["bass", "guitar"])
    result = wait_job(instance, instance.apply(preview["id"])["id"])
    assert result["counts"]["done"] == 1
    row = instance.job["items"][0]
    receipt = copy.deepcopy(row["receipt"])
    assert instance._receipt_valid(instance.job, row, receipt)
    receipt["excluded_stems"] = ["guitar"]
    assert not instance._receipt_valid(instance.job, row, receipt)


def test_distinct_custom_ids_with_same_display_label_have_separate_outputs(tmp_path, monkeypatch):
    fast_resources(monkeypatch)
    old, fresh, output = [tmp_path / name for name in ("old", "fresh", "output")]
    output.mkdir()
    for number, stem in enumerate(("lead_guitar", "lead guitar")):
        package(old / f"{number}.feedpak", donor=True, excluded_stems=[stem], audio=b"same audio")
    package(fresh / "song.feedpak")
    instance = manager(tmp_path / "config")
    preview = wait_job(instance, instance.start_scan(old_dir=str(old), fresh_dir=str(fresh),
                                                    output_dir=str(output))["id"])
    assert preview["counts"]["ready"] == 2
    assert len({row["id"] for row in preview["items"]}) == 2
    assert len({row["output_relative"] for row in preview["items"]}) == 2
    assert {row["variant_label"] for row in preview["items"]} == {"No Lead Guitar"}
    result = wait_job(instance, instance.apply(preview["id"])["id"])
    assert result["counts"]["done"] == 2
    assert {tuple(row["receipt"]["excluded_stems"]) for row in result["items"]} == {
        ("lead_guitar",), ("lead guitar",),
    }
