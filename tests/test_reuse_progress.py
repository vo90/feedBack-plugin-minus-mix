"""Progress reflects actual phase work for one song and large collections alike."""
from __future__ import annotations

import json
import threading
from collections import Counter

import pytest

import reuse_support
from tests.reuse_fixtures import fast_resources, manager, package, wait_job


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.mark.parametrize("total", [1, 4_000])
def test_every_collection_size_gets_fraction_elapsed_and_rate(total):
    clock = FakeClock()
    phase = reuse_support.PhaseProgress("read_fresh", "Reading fresh packages", total, clock=clock)
    start = phase.snapshot()
    assert start["processed"] == 0
    assert start["total"] == total
    assert start["fraction"] == 0
    assert start["active"] is True
    assert start["elapsed_seconds"] == 0
    assert start["files_per_second"] is None
    assert start["eta_seconds"] is None

    clock.advance(4)
    phase.advance()
    current = phase.snapshot()
    assert current["processed"] == 1
    assert current["fraction"] == pytest.approx(1 / total)
    assert current["elapsed_seconds"] == 4
    assert current["files_per_second"] == pytest.approx(0.25)
    assert current["eta_seconds"] == pytest.approx((total - 1) * 4)


def test_estimate_waits_for_elapsed_time_and_completed_work():
    clock = FakeClock()
    phase = reuse_support.PhaseProgress("apply", "Creating packages", 4, clock=clock)
    clock.advance(20)
    assert phase.snapshot()["files_per_second"] is None
    assert phase.snapshot()["eta_seconds"] is None

    phase = reuse_support.PhaseProgress("apply", "Creating packages", 4, clock=clock)
    phase.advance()
    clock.advance(1)
    assert phase.snapshot()["files_per_second"] is None
    assert phase.snapshot()["eta_seconds"] is None
    clock.advance(1)
    current = phase.snapshot()
    assert current["files_per_second"] == pytest.approx(0.5)
    assert current["eta_seconds"] == pytest.approx(6)


def test_discovery_reports_count_and_rate_without_inventing_percentage_or_eta():
    clock = FakeClock()
    phase = reuse_support.PhaseProgress("discovery", "Finding packages", clock=clock)
    phase.advance()
    phase.advance()
    clock.advance(4)
    current = phase.snapshot()
    assert current["processed"] == 2
    assert current["total"] is None
    assert current["fraction"] is None
    assert current["files_per_second"] == pytest.approx(0.5)
    assert current["eta_seconds"] is None
    phase.finish()
    clock.advance(1_000)
    assert phase.snapshot()["elapsed_seconds"] == 4
    assert phase.snapshot()["eta_seconds"] is None
    assert phase.snapshot()["active"] is False


def test_empty_phase_is_complete_without_division_by_zero():
    clock = FakeClock()
    phase = reuse_support.PhaseProgress("check_outputs", "Checking outputs", 0, clock=clock)
    phase.finish()
    current = phase.snapshot()
    assert current["processed"] == current["total"] == 0
    assert current["fraction"] == 1
    assert current["eta_seconds"] is None
    assert current["files_per_second"] is None
    assert current["active"] is False


@pytest.mark.parametrize("complete", [False, True])
def test_finished_or_stopped_phase_freezes_without_including_idle_time(complete):
    clock = FakeClock()
    phase = reuse_support.PhaseProgress("apply", "Creating packages", 2, clock=clock)
    phase.advance()
    if complete:
        phase.advance()
    clock.advance(5)
    phase.finish()
    stopped = phase.snapshot()
    assert stopped["active"] is False
    assert stopped["eta_seconds"] is None
    clock.advance(3_600)
    assert phase.snapshot() == stopped
    resumed = reuse_support.PhaseProgress("apply", "Creating packages", 2, clock=clock)
    assert resumed.snapshot()["processed"] == 0
    assert resumed.snapshot()["elapsed_seconds"] == 0
    assert resumed.snapshot()["files_per_second"] is None


def configure_clock(monkeypatch):
    clock = FakeClock()
    original = reuse_support.PhaseProgress

    class ControlledPhase(original):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("clock", clock)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(reuse_support, "PhaseProgress", ControlledPhase)
    return clock


def input_folders(tmp_path, *, count=1):
    old, fresh, output = [tmp_path / name for name in ("old", "fresh", "output")]
    for root in (old, fresh, output):
        root.mkdir()
    package(old / "artist" / "donor.feedpak", donor=True)
    for index in range(count):
        package(fresh / "artist" / f"{index}.feedpak")
    return old, fresh, output


def start_scan(instance, roots):
    old, fresh, output = roots
    return instance.start_scan(old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output))


def test_single_song_scan_exposes_every_phase_without_rewalking_or_reinspecting(tmp_path, monkeypatch):
    fast_resources(monkeypatch)
    clock = configure_clock(monkeypatch)
    roots = input_folders(tmp_path)
    instance = manager(tmp_path / "config")
    names = ("discovery", "read_existing", "read_fresh", "check_outputs")
    entered = {name: threading.Event() for name in names}
    release = {name: threading.Event() for name in names}
    walks, inspections = Counter(), Counter()
    original_walk = reuse_support.walk
    original_inspect = instance._inspect
    original_preview = instance._preview_existing

    def gate(name):
        entered[name].set()
        assert release[name].wait(10), f"Test did not release {name}"

    def walk(root, **kwargs):
        walks[str(root)] += 1
        if root == roots[0]:
            gate("discovery")
        yield from original_walk(root, **kwargs)

    def inspect(entry, donor):
        inspections[(entry[0], donor)] += 1
        gate("read_existing" if donor else "read_fresh")
        return original_inspect(entry, donor)

    def preview(row):
        gate("check_outputs")
        return original_preview(row)

    monkeypatch.setattr(reuse_support, "walk", walk)
    monkeypatch.setattr(instance, "_inspect", inspect)
    monkeypatch.setattr(instance, "_preview_existing", preview)
    job = start_scan(instance, roots)
    try:
        for name in names:
            assert entered[name].wait(10), f"Scan did not reach {name}"
            current = instance.get(job["id"], limit=1)["phase_progress"]
            assert current["phase"] == name
            assert current["active"] is True
            assert current["processed"] == 0
            assert current["total"] == (None if name == "discovery" else 1)
            assert current["fraction"] == (None if name == "discovery" else 0)
            assert current["elapsed_seconds"] == 0
            clock.advance(3)
            assert instance.get(job["id"])["phase_progress"]["elapsed_seconds"] == 3
            release[name].set()
        result = wait_job(instance, job["id"])
    finally:
        for event in release.values():
            event.set()
        wait_job(instance, job["id"])

    assert result["counts"]["ready"] == 1
    phase = result["phase_progress"]
    assert phase["phase"] == "check_outputs"
    assert phase["processed"] == phase["total"] == 1
    assert phase["fraction"] == 1
    assert phase["active"] is False
    assert phase["eta_seconds"] is None
    assert walks == {str(roots[0]): 1, str(roots[1]): 1}
    assert inspections == {("artist/donor.feedpak", True): 1, ("artist/0.feedpak", False): 1}
    clock.advance(3_600)
    assert instance.get(job["id"])["phase_progress"] == phase
    assert manager(tmp_path / "config").get(job["id"])["phase_progress"] == phase


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_or_canceled_scan_stops_phase_clock(tmp_path, monkeypatch, cancel):
    fast_resources(monkeypatch)
    clock = configure_clock(monkeypatch)
    instance = manager(tmp_path / "config")
    roots = input_folders(tmp_path, count=2)
    entered, release = threading.Event(), threading.Event()

    def fail(entry, donor):
        entered.set()
        assert release.wait(10)
        raise RuntimeError("Synthetic scan failure")

    monkeypatch.setattr(instance, "_inspect", fail)
    job = start_scan(instance, roots)
    try:
        assert entered.wait(10)
        clock.advance(5)
        if cancel:
            instance.cancel(job["id"])
    finally:
        release.set()
    result = wait_job(instance, job["id"])
    assert result["status"] == ("canceled" if cancel else "failed")
    phase = result["phase_progress"]
    assert phase["active"] is False
    assert phase["fraction"] < 1
    assert phase["eta_seconds"] is None
    assert phase["elapsed_seconds"] == 5
    clock.advance(500)
    assert instance.get(job["id"])["phase_progress"] == phase


def test_resumed_apply_counts_current_work_globally_despite_existing_receipts_and_pagination(tmp_path, monkeypatch):
    fast_resources(monkeypatch)
    clock = configure_clock(monkeypatch)
    roots = input_folders(tmp_path, count=3)
    # This extra package is skipped and must not inflate Apply's work total.
    package(roots[1] / "derived.feedpak", donor=True)
    instance = manager(tmp_path / "config")
    preview = wait_job(instance, start_scan(instance, roots)["id"])
    assert preview["counts"]["skipped"] == 1
    completed = wait_job(instance, instance.apply(preview["id"])["id"])
    assert completed["counts"]["done"] == 3
    phase = completed["phase_progress"]
    assert phase["processed"] == phase["total"] == 3
    assert phase["active"] is False
    clock.advance(3_600)
    restarted = manager(tmp_path / "config")
    assert restarted.get(preview["id"])["phase_progress"] == phase

    entered, release = threading.Event(), threading.Event()
    original = restarted._process

    def delayed(row):
        entered.set()
        assert release.wait(10)
        return original(row)

    monkeypatch.setattr(restarted, "_process", delayed)
    resumed = restarted.apply(preview["id"])
    try:
        assert entered.wait(10)
        current = restarted.get(resumed["id"], limit=1)
        assert len(current["items"]) == 1
        assert current["counts"]["done"] == 3
        phase = current["phase_progress"]
        assert phase["phase"] == "apply"
        assert phase["processed"] == 0
        assert phase["total"] == 3
        assert phase["fraction"] == 0
        assert phase["elapsed_seconds"] == 0
        assert phase["files_per_second"] is None
        clock.advance(4)
    finally:
        release.set()
    result = wait_job(restarted, resumed["id"])
    assert result["counts"]["existing"] == 3
    assert result["counts"]["created"] == 0
    assert result["phase_progress"]["processed"] == 3
    assert result["phase_progress"]["fraction"] == 1
    assert result["phase_progress"]["elapsed_seconds"] == 4


def test_old_checkpoint_without_progress_still_loads_and_applies(tmp_path, monkeypatch):
    fast_resources(monkeypatch)
    roots = input_folders(tmp_path)
    instance = manager(tmp_path / "config")
    preview = wait_job(instance, start_scan(instance, roots)["id"])
    checkpoint = instance.state_file
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload.pop("phase_progress", None)
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    restarted = manager(tmp_path / "config")
    assert restarted.load_error is None
    assert restarted.get(preview["id"])["status"] == "ready"
    result = wait_job(restarted, restarted.apply(preview["id"])["id"])
    assert result["counts"]["created"] == 1
    assert result["phase_progress"]["processed"] == result["phase_progress"]["total"] == 1


def test_invalid_optional_progress_does_not_discard_reviewed_plan(tmp_path, monkeypatch):
    fast_resources(monkeypatch)
    instance = manager(tmp_path / "config")
    preview = wait_job(instance, start_scan(instance, input_folders(tmp_path))["id"])
    checkpoint = instance.state_file
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["phase_progress"] = {"phase": "apply", "processed": -3, "total": 1}
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    restarted = manager(tmp_path / "config")
    current = restarted.get(preview["id"])
    assert restarted.load_error is None
    assert current["status"] == "ready"
    assert current["counts"]["ready"] == 1
    assert current.get("phase_progress") is None


def test_empty_scan_finishes_with_zero_of_zero_and_no_estimate(tmp_path, monkeypatch):
    fast_resources(monkeypatch)
    roots = tuple(tmp_path / name for name in ("old", "fresh", "output"))
    for root in roots:
        root.mkdir()
    instance = manager(tmp_path / "config")
    result = wait_job(instance, start_scan(instance, roots)["id"])
    assert result["status"] == "ready"
    assert result["counts"]["total"] == 0
    phase = result["phase_progress"]
    assert phase["phase"] == "check_outputs"
    assert phase["processed"] == phase["total"] == 0
    assert phase["fraction"] == 1
    assert phase["active"] is False
    assert phase["eta_seconds"] is None


def test_interrupted_checkpoint_drops_stale_live_progress(tmp_path, monkeypatch):
    fast_resources(monkeypatch)
    instance = manager(tmp_path / "config")
    preview = wait_job(instance, start_scan(instance, input_folders(tmp_path))["id"])
    checkpoint = instance.state_file
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["status"] = "running"
    payload["phase_progress"] = {"phase": "apply", "label": "Creating packages", "processed": 0,
                                 "total": 1, "elapsed_seconds": 0, "active": True}
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    restarted = manager(tmp_path / "config")
    current = restarted.get(preview["id"])
    assert current["status"] == "interrupted"
    assert current.get("phase_progress") is None
    result = wait_job(restarted, restarted.apply(preview["id"])["id"])
    assert result["counts"]["created"] == 1
    assert result["phase_progress"]["processed"] == 1


def test_canceled_apply_freezes_clock_and_resume_excludes_waiting_time(tmp_path, monkeypatch):
    fast_resources(monkeypatch)
    clock = configure_clock(monkeypatch)
    instance = manager(tmp_path / "config")
    preview = wait_job(instance, start_scan(instance, input_folders(tmp_path, count=3))["id"])
    entered, release = threading.Event(), threading.Event()
    original = instance._process

    def delayed(row):
        entered.set()
        assert release.wait(10)
        return original(row)

    monkeypatch.setattr(instance, "_process", delayed)
    job = instance.apply(preview["id"])
    try:
        assert entered.wait(10)
        clock.advance(5)
        instance.cancel(job["id"])
    finally:
        release.set()
    canceled = wait_job(instance, job["id"])
    assert canceled["status"] == "canceled"
    phase = canceled["phase_progress"]
    assert phase["active"] is False
    assert phase["elapsed_seconds"] == 5
    assert phase["eta_seconds"] is None
    clock.advance(3_600)
    assert instance.get(job["id"])["phase_progress"] == phase
    monkeypatch.setattr(instance, "_process", original)
    result = wait_job(instance, instance.apply(job["id"])["id"])
    assert result["counts"]["created"] == 3
    assert result["phase_progress"]["processed"] == 3
    assert result["phase_progress"]["elapsed_seconds"] == 0
