from __future__ import annotations

import json
import threading
import time
import zipfile

import pytest
import yaml

import reuse_export
import reuse_match
import reuse_state
import reuse_support
from tests.reuse_fixtures import fast_resources, manager, package, wait_job


def setup_scan(tmp_path, monkeypatch, *, count=1):
    fast_resources(monkeypatch)
    old, fresh, output = [tmp_path / name for name in ("old", "fresh", "output")]
    output.mkdir()
    package(old / "donor.feedpak", donor=True, audio=b"no guitar")
    for index in range(count):
        package(fresh / f"{index}.feedpak")
    instance = manager(tmp_path / "config")
    job = instance.start_scan(old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output))
    return instance, wait_job(instance, job["id"]), old, fresh, output


def test_complete_batch_restart_and_exact_verified_resume(tmp_path, monkeypatch):
    instance, preview, old, fresh, output = setup_scan(tmp_path, monkeypatch, count=3)
    originals = {str(path): reuse_match.digest_file(path) for root in (old, fresh) for path in root.glob("*.feedpak")}
    assert preview["counts"]["ready"] == 3
    job = wait_job(instance, instance.apply(preview["id"])["id"])
    assert job["counts"]["done"] == 3
    before = {str(path): reuse_match.digest_file(path) for path in output.glob("*.feedpak")}
    restarted = manager(tmp_path / "config")
    resumed = wait_job(restarted, restarted.apply(job["id"])["id"])
    assert resumed["counts"]["done"] == 3
    assert all(item["receipt"].get("recovered") for item in resumed["items"])
    assert before == {str(path): reuse_match.digest_file(path) for path in output.glob("*.feedpak")}
    assert originals == {path: reuse_match.digest_file(path) for path in originals}


def test_ambiguous_recording_choice_applies_to_all_matching_targets_once(tmp_path, monkeypatch):
    instance, first, old, fresh, output = setup_scan(tmp_path, monkeypatch, count=2)
    package(old / "alternate.feedpak", donor=True, audio=b"different recording")
    preview = wait_job(instance, instance.start_scan(old_dir=str(old), fresh_dir=str(fresh),
                                                    output_dir=str(output))["id"])
    assert preview["counts"]["review"] == 2
    assert len(preview["groups"]) == 1
    group = preview["groups"][0]
    assert group["targets_count"] == 2
    with pytest.raises(reuse_match.ReuseError, match="compatible"):
        instance.choose(preview["id"], {group["id"]: "../outside.feedpak"})
    chosen = instance.choose(preview["id"], {group["id"]: "alternate.feedpak"})
    assert chosen["counts"]["ready"] == 2
    completed = wait_job(instance, instance.apply(preview["id"])["id"])
    assert completed["counts"]["done"] == 2
    for path in output.glob("*.feedpak"):
        with zipfile.ZipFile(path) as archive:
            assert archive.read("stems/full.ogg") == b"different recording"


def test_identical_donor_bytes_collapse_without_manual_choice(tmp_path, monkeypatch):
    instance, _, old, fresh, output = setup_scan(tmp_path, monkeypatch)
    package(old / "duplicate.feedpak", donor=True, audio=b"no guitar")
    preview = wait_job(instance, instance.start_scan(old_dir=str(old), fresh_dir=str(fresh),
                                                    output_dir=str(output))["id"])
    assert preview["counts"]["ready"] == 1
    assert preview["counts"]["review"] == 0


def test_unreadable_unrelated_donor_does_not_stop_known_matches(tmp_path, monkeypatch):
    instance, _, old, fresh, output = setup_scan(tmp_path, monkeypatch)
    (old / "broken.feedpak").write_bytes(b"broken")
    preview = wait_job(instance, instance.start_scan(old_dir=str(old), fresh_dir=str(fresh),
                                                    output_dir=str(output))["id"])
    assert preview["source_errors_total"] == 1
    assert preview["counts"]["ready"] == 1
    assert "readable donors" in preview["items"][0]["reason"]


def test_fresh_derived_companions_are_skipped(tmp_path, monkeypatch):
    instance, _, old, fresh, output = setup_scan(tmp_path, monkeypatch)
    package(fresh / "0 (No Guitar).feedpak", donor=True)
    preview = wait_job(instance, instance.start_scan(old_dir=str(old), fresh_dir=str(fresh),
                                                    output_dir=str(output))["id"])
    assert preview["counts"]["ready"] == 1
    assert preview["counts"]["skipped"] == 1
    assert next(row for row in preview["items"] if row["status"] == "ready")["output_relative"] == "0 (No Guitar).feedpak"


def test_changed_inputs_only_fail_affected_row_and_no_full_rescan(tmp_path, monkeypatch):
    instance, preview, _, fresh, output = setup_scan(tmp_path, monkeypatch, count=2)
    package(fresh / "0.feedpak", audio=b"changed fresh full mix")

    def forbidden(*args, **kwargs):
        raise AssertionError("Apply must not rescan folders")

    monkeypatch.setattr(reuse_support, "walk", forbidden)
    result = wait_job(instance, instance.apply(preview["id"])["id"])
    assert result["counts"]["done"] == 1
    assert result["counts"]["failed"] == 1
    assert len(list(output.glob("*.feedpak"))) == 1


def test_copy_workers_really_overlap_and_cancel_can_resume(tmp_path, monkeypatch):
    instance, preview, _, _, output = setup_scan(tmp_path, monkeypatch, count=4)
    original = reuse_export.export_reuse
    both_entered = threading.Event()
    entered = threading.Barrier(2, action=both_entered.set)
    release = threading.Event()

    def delayed(*args, **kwargs):
        entered.wait(timeout=5)
        release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(reuse_export, "export_reuse", delayed)
    instance.apply(preview["id"])
    # The action runs only after both workers actually reach the barrier.
    assert both_entered.wait(timeout=5), "Two copy workers did not enter concurrently"
    instance.cancel(preview["id"])
    release.set()
    result = wait_job(instance, preview["id"])
    assert result["status"] == "canceled"
    assert not list(output.glob(".minusmix-reuse-*"))
    monkeypatch.setattr(reuse_export, "export_reuse", original)
    resumed = wait_job(instance, instance.apply(preview["id"])["id"])
    assert resumed["counts"]["done"] == 4


@pytest.mark.parametrize("payload", [[], {}, {"policy": reuse_match.POLICY, "items": [], "status": "ready"}])
def test_malformed_checkpoint_cannot_break_plugin_startup(tmp_path, payload):
    state = tmp_path / "audio_reuse_job.json"
    state.write_text(json.dumps(payload))
    original = state.read_bytes()
    with pytest.raises(ValueError):
        reuse_state.validate(payload, reuse_match.POLICY, reuse_match.MAX_FILES)
    instance = manager(tmp_path)
    assert not instance.is_active()
    assert instance.latest()["status"] == "failed"
    assert state.read_bytes() == original


def test_initial_persistence_failure_releases_busy_reservation(tmp_path, monkeypatch):
    instance, _, old, fresh, output = setup_scan(tmp_path, monkeypatch)

    def fail():
        raise OSError("config disk unavailable")

    monkeypatch.setattr(instance, "_persist", fail)
    with pytest.raises(reuse_match.ReuseError, match="checkpoint"):
        instance.start_scan(old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output))
    assert not instance.is_active()


def test_resume_refuses_edited_output_manifest(tmp_path, monkeypatch):
    instance, preview, _, _, output = setup_scan(tmp_path, monkeypatch)
    job = wait_job(instance, instance.apply(preview["id"])["id"])
    path = next(output.glob("*.feedpak"))
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = yaml.safe_load(members["manifest.yaml"])
    manifest["arrangements"][0]["name"] = "Edited name"
    members["manifest.yaml"] = yaml.safe_dump(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    altered = reuse_match.digest_file(path)
    resumed = wait_job(instance, instance.apply(job["id"])["id"])
    assert resumed["counts"]["failed"] == 1
    assert reuse_match.digest_file(path) == altered


def test_resource_admission_failure_creates_nothing(tmp_path, monkeypatch):
    instance, preview, _, _, output = setup_scan(tmp_path, monkeypatch)

    def refuse(*args, **kwargs):
        raise reuse_match.ReuseError("Insufficient output disk space")

    monkeypatch.setattr(reuse_support, "admission", refuse)
    result = wait_job(instance, instance.apply(preview["id"])["id"])
    assert result["status"] == "failed"
    assert not list(output.glob("*.feedpak"))


def test_root_overlap_is_rejected_before_scan(tmp_path):
    root = tmp_path / "old"
    nested = root / "fresh"
    output = tmp_path / "out"
    nested.mkdir(parents=True)
    output.mkdir()
    with pytest.raises(reuse_match.ReuseError, match="non-overlapping"):
        manager(tmp_path / "config").start_scan(old_dir=str(root), fresh_dir=str(nested), output_dir=str(output))


def test_resource_controls_honor_manual_range_and_report_clamping():
    hardware = {"cpu_count": 12, "memory_available": 8 * 1024**3, "storage": "ssd"}
    assert reuse_support.resources("auto", hardware=hardware)["effective_workers"] == 2
    assert reuse_support.resources(16, hardware=hardware)["effective_workers"] == 8
    assert reuse_support.resources(4, hardware={**hardware, "storage": "unknown"})["effective_workers"] == 2
    for value in (0, 17, True, "all"):
        with pytest.raises(ValueError):
            reuse_support.resources(value, hardware=hardware)


def test_300_outputs_append_receipts_without_300_full_plan_rewrites(tmp_path, monkeypatch):
    instance, preview, _, _, output = setup_scan(tmp_path, monkeypatch, count=300)
    original_persist, original_record = instance._persist, instance._record
    counts = {"checkpoint": 0, "record": 0}

    def persist():
        counts["checkpoint"] += 1
        return original_persist()

    def record(row):
        counts["record"] += 1
        return original_record(row)

    monkeypatch.setattr(instance, "_persist", persist)
    monkeypatch.setattr(instance, "_record", record)
    completed = wait_job(instance, instance.apply(preview["id"])["id"], timeout=180)
    assert completed["counts"]["done"] == 300
    # Public status may change immediately before the final durable write.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        saved = json.loads(instance.state_file.read_text())
        if saved["status"] == "completed" and instance.journal_file.read_bytes() == b"":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("Final completed checkpoint was not saved")
    assert counts == {"checkpoint": 2, "record": 300}
    before = {path.name: reuse_match.digest_file(path) for path in output.glob("*.feedpak")}
    restarted = manager(tmp_path / "config")
    resumed = wait_job(restarted, restarted.apply(preview["id"])["id"], timeout=180)
    assert resumed["counts"]["done"] == 300
    assert before == {path.name: reuse_match.digest_file(path) for path in output.glob("*.feedpak")}


def test_crash_journal_replays_valid_prefix_and_ignores_truncated_tail(tmp_path, monkeypatch):
    instance, preview, _, _, output = setup_scan(tmp_path, monkeypatch, count=3)
    instance.job["status"] = "running"
    instance._persist()
    for row in instance.job["items"][:2]:
        instance._process(row)
    lines = instance.journal_file.read_bytes()
    assert len(lines.splitlines()) == 2
    with instance.journal_file.open("ab") as stream:
        stream.write(b'{"job_id":')
    raw = instance.journal_file.read_bytes()
    restarted = manager(tmp_path / "config")
    recovered = restarted.latest()
    assert recovered["status"] == "interrupted"
    assert recovered["counts"]["done"] == 2
    assert "incomplete" in recovered["journal_warning"]
    assert instance.journal_file.read_bytes() == raw
    before = {path.name: reuse_match.digest_file(path) for path in output.glob("*.feedpak")}
    resumed = wait_job(restarted, restarted.apply(preview["id"])["id"])
    assert resumed["counts"]["done"] == 3
    assert "journal_warning" not in resumed
    assert all(reuse_match.digest_file(output / name) == digest for name, digest in before.items())


def test_journal_cannot_supply_forged_completion_binding(tmp_path, monkeypatch):
    instance, preview, _, _, _ = setup_scan(tmp_path, monkeypatch)
    instance.job["status"] = "running"
    instance._persist()
    instance._process(instance.job["items"][0])
    record = json.loads(instance.journal_file.read_bytes())
    record["receipt"]["plan_key"] = "f" * 64
    instance.journal_file.write_text(json.dumps(record) + "\n")
    restarted = manager(tmp_path / "config")
    assert restarted.latest()["counts"]["done"] == 0
    assert "binding" in restarted.latest()["journal_warning"]
    # The actual package can still be recovered, but only by checking its bytes.
    recovered = wait_job(restarted, restarted.apply(preview["id"])["id"])
    assert recovered["counts"]["done"] == 1
    assert recovered["items"][0]["receipt"]["recovered"] is True


def test_all_done_crash_can_resume_and_verify_outputs(tmp_path, monkeypatch):
    instance, preview, _, _, _ = setup_scan(tmp_path, monkeypatch)
    instance.job["status"] = "running"
    instance._persist()
    instance._process(instance.job["items"][0])
    restarted = manager(tmp_path / "config")
    assert restarted.latest()["status"] == "interrupted"
    assert restarted.latest()["counts"]["done"] == 1
    result = wait_job(restarted, restarted.apply(preview["id"])["id"])
    assert result["status"] == "completed"
    assert result["items"][0]["receipt"]["recovered"] is True


def test_checkpoint_done_without_receipt_is_reconciled_to_verification(tmp_path, monkeypatch):
    instance, preview, _, _, _ = setup_scan(tmp_path, monkeypatch)
    instance.job["items"][0]["status"] = "done"
    instance.job["status"] = "completed"
    instance._persist()
    restarted = manager(tmp_path / "config")
    assert restarted.latest()["counts"]["done"] == 0
    assert restarted.latest()["counts"]["ready"] == 1
    assert "invalid" in restarted.latest()["items"][0]["reason"]
    completed = wait_job(restarted, restarted.apply(preview["id"])["id"])
    assert completed["counts"]["done"] == 1
