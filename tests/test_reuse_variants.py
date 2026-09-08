"""Mixed donor folders recreate variants without crossing audio or resume bindings."""
from __future__ import annotations

import json
import os
import threading
from collections import Counter
from pathlib import Path

import pytest
import yaml

import reuse_batch
import reuse_match
import reuse_state
from tests.reuse_fixtures import fast_resources, manager, package, wait_job


def mixed_scan(tmp_path, monkeypatch, *, sets=None, workers=4):
    fast_resources(monkeypatch)
    old, fresh, output = [tmp_path / name for name in ("old", "fresh", "output")]
    output.mkdir()
    sets = sets or [[stem] for stem in ("guitar", "bass", "drums", "vocals", "piano", "other")]
    for index, stems in enumerate(sets):
        package(old / f"audio-{index}.feedpak", donor=True, excluded_stems=stems,
                audio=f"mix-{index}".encode(), preview=f"preview-{index}".encode())
    package(fresh / "Song.feedpak", extras={"tabs/custom.bin": b"fresh tab data"})
    instance = manager(tmp_path / "config")
    preview = wait_job(instance, instance.start_scan(old_dir=str(old), fresh_dir=str(fresh),
                                                    output_dir=str(output), workers=workers)["id"])
    assert preview["status"] == "ready", preview["detail"]
    return instance, preview, old, fresh, output


def test_all_standard_stems_and_combination_copy_exact_variant_and_fresh_assets(tmp_path, monkeypatch):
    import zipfile

    sets = [[stem] for stem in ("guitar", "bass", "drums", "vocals", "piano", "other")]
    sets += [["vocals", "guitar"]]
    instance, preview, old, fresh, output = mixed_scan(tmp_path, monkeypatch, sets=sets)
    originals = {path: reuse_match.digest_file(path) for root in (old, fresh) for path in root.glob("*.feedpak")}
    assert preview["input_packages_total"] == 1
    assert preview["output_variants_total"] == 7
    assert preview["counts"]["ready"] == 7
    assert len({row["id"] for row in preview["items"]}) == 7
    assert len({row["output_relative"].casefold() for row in preview["items"]}) == 7
    assert not list(output.iterdir())
    completed = wait_job(instance, instance.apply(preview["id"])["id"])
    assert completed["counts"]["done"] == 7, completed
    for row in completed["items"]:
        with zipfile.ZipFile(output / row["output_relative"]) as result, \
                zipfile.ZipFile(old / row["donor_relative"]) as donor, \
                zipfile.ZipFile(fresh / row["relative_path"]) as current:
            manifest = yaml.safe_load(result.read("manifest.yaml"))
            assert manifest["title"] == "Song (" + row["variant_label"] + ")"
            assert manifest["minus_mix"]["excluded_stems"] == row["excluded_stems"]
            assert row["receipt"]["excluded_stems"] == row["excluded_stems"]
            for name in ("stems/full.ogg", "preview.ogg"):
                assert result.read(name) == donor.read(name)
            for name in ("arrangements/lead.json", "cover.png", "tabs/custom.bin"):
                assert result.read(name) == current.read(name)
    assert originals == {path: reuse_match.digest_file(path) for path in originals}
    before = {path: reuse_match.digest_file(path) for path in output.glob("*.feedpak")}
    restarted = manager(tmp_path / "config")
    resumed = wait_job(restarted, restarted.apply(preview["id"])["id"])
    assert resumed["counts"]["done"] == 7
    assert all(row["receipt"]["recovered"] for row in resumed["items"])
    assert before == {path: reuse_match.digest_file(path) for path in before}


def test_identical_audio_only_collapses_within_same_normalized_variant(tmp_path, monkeypatch):
    instance, _, old, fresh, output = mixed_scan(tmp_path, monkeypatch, sets=[["guitar", "bass"], ["vocals"]])
    for name, stems in (("a", ["guitar", "bass"]), ("b", ["bass", "guitar"]), ("c", ["vocals"])):
        package(old / f"{name}.feedpak", donor=True, excluded_stems=stems, audio=b"same", preview=b"same")
    preview = wait_job(instance, instance.start_scan(old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output))["id"])
    assert preview["output_variants_total"] == 2
    assert preview["counts"]["review"] == 2
    assert all(len(group["candidates"]) == 2 for group in preview["groups"])
    guitar = next(group for group in preview["groups"] if "guitar" in group["excluded_stems"])
    with pytest.raises(reuse_match.ReuseError, match="compatible"):
        instance.choose(preview["id"], {guitar["id"]: "c.feedpak"})
    choice = wait_job(instance, instance.choose(preview["id"], {guitar["id"]: "a.feedpak"})["id"])
    assert choice["counts"]["ready"] == 1 and choice["counts"]["review"] == 1


def test_identical_audio_across_different_variants_produces_both_outputs(tmp_path, monkeypatch):
    instance, _, old, fresh, output = mixed_scan(tmp_path, monkeypatch, sets=[["guitar"], ["bass"]])
    for index, stems in enumerate((["guitar"], ["bass"])):
        package(old / f"audio-{index}.feedpak", donor=True, excluded_stems=stems, audio=b"same", preview=b"same")
    preview = wait_job(instance, instance.start_scan(old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output))["id"])
    assert preview["counts"]["ready"] == 2 and preview["counts"]["review"] == 0
    completed = wait_job(instance, instance.apply(preview["id"])["id"])
    assert completed["counts"]["done"] == 2


def test_parallel_variants_share_inspection_only_within_one_apply(tmp_path, monkeypatch):
    instance, preview, _, fresh, _ = mixed_scan(tmp_path, monkeypatch)
    original = reuse_match.inspect_package
    counts = Counter()
    lock = threading.Lock()

    def counted(path, **kwargs):
        with lock:
            counts[str(path)] += 1
        return original(path, **kwargs)

    monkeypatch.setattr(reuse_match, "inspect_package", counted)
    completed = wait_job(instance, instance.apply(preview["id"])["id"])
    assert completed["counts"]["done"] == 6
    assert counts[str(fresh / "Song.feedpak")] == 1
    assert sum(counts.values()) == 7
    assert not instance._snapshots and instance._snapshot_bytes == 0
    resumed = wait_job(instance, instance.apply(preview["id"])["id"])
    assert resumed["counts"]["done"] == 6
    assert counts[str(fresh / "Song.feedpak")] == 2


def test_cached_snapshot_detects_same_stat_change_even_in_discarded_audio(tmp_path, monkeypatch):
    instance, _, _, fresh, _ = mixed_scan(tmp_path, monkeypatch, sets=[["guitar"]])
    path = fresh / "Song.feedpak"
    prior = instance.job["items"][0]["_fresh"]
    instance._snapshot(path, prior, donor=False)
    previous = path.stat()
    data = bytearray(path.read_bytes())
    # The cache must reject any whole-archive content change, even if a file
    # editor restores its original timestamp and size.
    data[-1] ^= 1
    path.write_bytes(data)
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert reuse_match.signature(path) == prior["signature"]
    with pytest.raises(reuse_match.ReuseError, match="content changed"):
        instance._snapshot(path, prior, donor=False)


def test_old_policy_preview_is_preserved_before_new_scan(tmp_path, monkeypatch):
    instance, _, old, fresh, output = mixed_scan(tmp_path, monkeypatch, sets=[["guitar"]])
    instance.job["policy"] = "minusmix-audio-reuse-1"
    instance._persist()
    instance.journal_file.write_bytes(b"old receipt evidence\n")
    original = instance.state_file.read_bytes()
    restarted = manager(tmp_path / "config")
    assert "Scan again" in restarted.latest()["detail"]
    assert restarted.state_file.read_bytes() == original
    preview = wait_job(restarted, restarted.start_scan(old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output))["id"])
    assert preview["counts"]["ready"] == 1
    archives = list((tmp_path / "config").glob("audio-reuse-v1-*"))
    assert len(archives) == 1
    assert (archives[0] / "audio_reuse_job.json").read_bytes() == original
    assert (archives[0] / "audio_reuse_receipts.jsonl").read_bytes() == b"old receipt evidence\n"
    assert not list(output.iterdir())


@pytest.mark.parametrize("limit_kind", ["rows", "bytes"])
def test_oversized_fanout_preview_is_reloadable_failure_and_cannot_apply(tmp_path, monkeypatch, limit_kind):
    instance, _, old, fresh, output = mixed_scan(tmp_path, monkeypatch)
    if limit_kind == "rows":
        monkeypatch.setattr(reuse_match, "MAX_OUTPUT_ROWS", 2)
    else:
        monkeypatch.setattr(reuse_batch, "MAX_CHECKPOINT_BYTES", 9000)
    result = wait_job(instance, instance.start_scan(old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output))["id"])
    assert result["status"] == "failed" and "limit" in result["detail"]
    assert not result["items"]
    with pytest.raises(reuse_match.ReuseError, match="completed idle"):
        instance.apply(result["id"])
    restarted = manager(tmp_path / "config")
    assert restarted.latest()["id"] == result["id"]
    assert restarted.latest()["status"] == "failed"
    assert not list(output.iterdir())


def test_checkpoint_cannot_bind_target_to_another_variant(tmp_path, monkeypatch):
    instance, _, _, _, _ = mixed_scan(tmp_path, monkeypatch, sets=[["guitar"], ["bass"]])
    data = json.loads(json.dumps(instance.job))
    data["items"][0]["donor_relative"] = data["items"][1]["donor_relative"]
    with pytest.raises(ValueError, match="mix variant"):
        reuse_state.validate(data, reuse_match.POLICY, reuse_match.MAX_FILES, reuse_match.MAX_OUTPUT_ROWS)


def test_snapshot_cache_obeys_entry_and_byte_limits(tmp_path, monkeypatch):
    instance, _, old, _, _ = mixed_scan(tmp_path, monkeypatch)
    monkeypatch.setattr(reuse_batch, "SNAPSHOT_CACHE_ENTRIES", 2)
    monkeypatch.setattr(reuse_batch, "SNAPSHOT_CACHE_BYTES", 1500)
    for info in instance.job["_donors"].values():
        instance._snapshot(Path(old / info["relative_path"]), info, donor=True)
        assert len(instance._snapshots) <= 2
        assert instance._snapshot_bytes <= 1500


def test_ready_and_new_operations_wait_for_durable_preview(tmp_path, monkeypatch):
    instance, _, old, fresh, output = mixed_scan(tmp_path, monkeypatch, sets=[["guitar"]])
    saving, release = threading.Event(), threading.Event()
    original = instance._persist

    def delayed():
        if instance.job["status"] == "ready":
            saving.set()
            assert release.wait(timeout=5)
        original()

    monkeypatch.setattr(instance, "_persist", delayed)
    job = instance.start_scan(old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output))
    assert saving.wait(timeout=5)
    try:
        assert instance.latest()["status"] == "scanning"
        assert instance.is_active()
        with pytest.raises(reuse_match.ReuseError, match="already running"):
            instance.start_scan(old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output))
        with pytest.raises(reuse_match.ReuseError, match="completed idle"):
            instance.apply(job["id"])
    finally:
        release.set()
    result = wait_job(instance, job["id"])
    assert result["status"] == "ready"
    assert manager(tmp_path / "config").latest()["status"] == "ready"


def test_long_custom_variant_receipt_survives_crash_replay(tmp_path, monkeypatch):
    instance, preview, _, _, _ = mixed_scan(tmp_path, monkeypatch, sets=[["custom" + "x" * 40000]])
    instance.job["status"] = "running"
    instance._persist()
    instance._process(instance.job["items"][0])
    assert instance.journal_file.stat().st_size > 32768
    restarted = manager(tmp_path / "config")
    assert restarted.latest()["counts"]["done"] == 1
    assert "journal_warning" not in restarted.latest()
    completed = wait_job(restarted, restarted.apply(preview["id"])["id"])
    assert completed["counts"]["done"] == 1
