"""Observable queue recovery through the real exporter stem-provider boundary."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import batch
import exporter
import single


def _pak(path: Path, *, full=b"full", guitar=False, derived=False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    stems = [{"id": "full", "file": "stems/full.ogg"}]
    if guitar:
        stems.append({"id": "guitar", "file": "stems/guitar.ogg"})
    manifest = {"title": path.stem, "artist": "Test", "stems": stems}
    if derived:
        manifest["minus_mix"] = {"excluded_stems": ["guitar"], "generator": "minus_mix"}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.yaml", yaml.safe_dump(manifest))
        archive.writestr("stems/full.ogg", full)
        if guitar:
            archive.writestr("stems/guitar.ogg", b"guitar")
    return path


class ProviderExporter:
    """Use real source inspection and exception wrapping, with no audio process."""

    ExportError = exporter.ExportError
    inspect_source = staticmethod(exporter.inspect_source)
    desired_output_path = staticmethod(exporter.desired_output_path)
    stem_label = staticmethod(exporter.stem_label)
    validate_output_directory = staticmethod(exporter.validate_output_directory)

    @staticmethod
    def export_minus_mix(source, output_dir, selected, *, stem_provider,
                         progress_cb, cancel_cb, log):
        info = exporter.inspect_source(source)
        saved = {stem.id for stem in info.stems}
        missing = tuple(stem for stem in selected if stem not in saved)
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            mix = work / "full.ogg"
            with zipfile.ZipFile(source) as archive:
                mix.write_bytes(archive.read(info.full_mix_file))
            progress_cb("separating", 0.25, "Preparing selected audio")
            extracted = exporter.ExtractedAudio(mix, {}, "test-input")
            exporter._obtain_missing_stems(stem_provider, extracted, missing, work)
        cancel_cb()
        progress_cb("packaging", 0.9, "Creating package")
        target = exporter.desired_output_path(output_dir, source, selected)
        target.write_bytes(b"completed test package")
        return SimpleNamespace(
            output_path=target, output_filename=target.name, title=info.title,
            excluded_stems=selected, temporary_separation_used=bool(missing),
            preview_created=True,
        )


class ServiceBlock(RuntimeError):
    blocks_batch = True

    def __init__(self, state):
        self.state = state
        super().__init__(f"Stem server blocked: {state}")


class ControlledService:
    supports_state_callback = True

    def __init__(self, *, wait_on=1, failure=None, status=None):
        self.wait_on = wait_on
        self.failure = failure
        self.status_payload = status or {"ready": True}
        self.calls = 0
        self.waiting = threading.Event()
        self.release = threading.Event()

    def status(self):
        return dict(self.status_payload)

    def separate(self, mix, work, stems, *, progress_cb, cancel_cb, state_cb):
        self.calls += 1
        cancel_cb()
        progress_cb(0.5, "Separating audio")
        if self.calls == self.wait_on:
            state_cb({"state": "waiting_for_server", "detail": "Waiting for the server update"})
            # Progress text during recovery must not replace the waiting stage.
            progress_cb(0.1, "Reconnecting to the server")
            self.waiting.set()
            while not self.release.wait(0.01):
                cancel_cb()
            cancel_cb()
            if self.failure is not None:
                raise self.failure
            state_cb({"state": "separating", "detail": "Separation resumed"})
        state_cb({"state": "downloading", "detail": "Downloading audio"})
        result = {}
        for stem in stems:
            path = Path(work) / f"{stem}.flac"
            path.write_bytes(b"test stem")
            result[stem] = path
        return result


def _log():
    return SimpleNamespace(exception=lambda *args, **kwargs: None,
                           warning=lambda *args, **kwargs: None)


def _finished(manager, job_id):
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job["status"] not in {"queued", "running", "canceling"} and not manager.is_active():
            return job
        time.sleep(0.01)
    raise AssertionError("manager did not finish")


def _batch_case(tmp_path, service, *, saved=False):
    source_root, output_root = tmp_path / "sources", tmp_path / "outputs"
    output_root.mkdir()
    for name in ("one", "two", "zzz"):
        _pak(source_root / f"{name}.feedpak", full=name.encode(), guitar=saved)
    manager = batch.BatchManager(ProviderExporter(), service, tmp_path / "config", _log())
    options = dict(input_dir=str(source_root), output_dir=str(output_root),
                   excluded_stems=["guitar"], recursive=True,
                   skip_existing=True, skip_derived=True)
    return manager, options, output_root


def test_single_waiting_is_active_preserves_progress_and_resumes(tmp_path):
    source = _pak(tmp_path / "source.feedpak")
    output = tmp_path / "output"
    output.mkdir()
    service = ControlledService()
    manager = single.SingleExportManager(ProviderExporter(), service, _log())
    started = manager.start(source, output, ["guitar"])
    try:
        assert service.waiting.wait(1.0)
        waiting = manager.get(started["id"])
        assert waiting["status"] == "running"
        assert waiting["stage"] == "waiting_for_server"
        assert waiting["progress"] == pytest.approx(0.41)
        assert manager.latest() == waiting
        assert manager.is_active()
        with pytest.raises(single.SingleExportError, match="already running"):
            manager.start(source, output, ["guitar"])
        assert not list(output.iterdir())
    finally:
        service.release.set()
    completed = _finished(manager, started["id"])
    assert completed["status"] == "completed"
    assert completed["progress"] == 1.0
    assert service.calls == 1
    assert len(list(output.iterdir())) == 1


@pytest.mark.parametrize("state", ["missing_model", "recovery_exhausted"])
def test_single_service_block_survives_real_exporter_wrapper(tmp_path, state):
    source = _pak(tmp_path / "source.feedpak")
    output = tmp_path / "output"
    output.mkdir()
    service = ControlledService(failure=ServiceBlock(state))
    service.release.set()
    manager = single.SingleExportManager(ProviderExporter(), service, _log())
    blocked = _finished(manager, manager.start(source, output, ["guitar"])["id"])
    assert blocked["status"] == blocked["stage"] == "blocked"
    assert blocked["state"] == state
    assert blocked["progress"] == pytest.approx(0.41)
    assert blocked["result"] is None
    assert blocked["detail"] == f"Stem server blocked: {state}"
    assert not list(output.iterdir())


def test_single_cancel_during_wait_prevents_publication(tmp_path):
    source = _pak(tmp_path / "source.feedpak")
    output = tmp_path / "output"
    output.mkdir()
    service = ControlledService()
    manager = single.SingleExportManager(ProviderExporter(), service, _log())
    started = manager.start(source, output, ["guitar"])
    try:
        assert service.waiting.wait(1.0)
        manager.cancel(started["id"])
        canceled = _finished(manager, started["id"])
        assert canceled["status"] == "canceled"
        assert canceled["result"] is None
    finally:
        service.release.set()
    assert service.calls == 1
    assert not list(output.iterdir())


def test_batch_waitable_admission_holds_current_song_and_resumes(tmp_path):
    service = ControlledService(wait_on=2, status={
        "ready": False, "waitable": True, "state": "updating",
    })
    manager, options, output = _batch_case(tmp_path, service)
    started = manager.start(**options)
    try:
        assert service.waiting.wait(1.0)
        waiting = manager.get(started["id"])
        assert waiting["status"] == "running"
        assert [item["status"] for item in waiting["items"]] == ["done", "running", "queued"]
        assert waiting["items"][1]["stage"] == "waiting_for_server"
        assert waiting["items"][1]["progress"] == pytest.approx(0.41)
        assert waiting["counts"]["done"] == 1
        assert waiting["counts"]["queued"] == 1
        assert waiting["counts"]["failed"] == 0
        assert waiting["overall_progress"] == pytest.approx(1.41 / 3)
        assert manager.is_active()
        with pytest.raises(batch.BatchError, match="already running"):
            manager.start(**options)
    finally:
        service.release.set()
    completed = _finished(manager, started["id"])
    assert completed["status"] == "completed"
    assert completed["counts"]["done"] == 3
    assert completed["counts"]["failed"] == completed["counts"]["blocked"] == 0
    assert service.calls == 3
    assert len(list(output.glob("*.feedpak"))) == 3


@pytest.mark.parametrize("state", ["missing_model", "recovery_exhausted", "endpoint_changed"])
def test_batch_service_block_preserves_pending_rows_and_retry_skips_done(tmp_path, state):
    service = ControlledService(wait_on=2, failure=ServiceBlock(state))
    service.release.set()
    manager, options, output = _batch_case(tmp_path, service)
    blocked = _finished(manager, manager.start(**options)["id"])
    assert blocked["status"] == blocked["stage"] == "blocked"
    assert blocked["state"] == state
    assert [item["status"] for item in blocked["items"]] == ["done", "blocked", "queued"]
    assert blocked["counts"]["done"] == blocked["counts"]["queued"] == blocked["counts"]["blocked"] == 1
    assert blocked["counts"]["failed"] == blocked["counts"]["canceled"] == 0
    assert blocked["overall_progress"] == pytest.approx(1.41 / 3)
    assert service.calls == 2
    assert len(list(output.glob("*.feedpak"))) == 1
    original = next(output.glob("*.feedpak"))
    before = original.read_bytes(), original.stat().st_mtime_ns
    # Persisted blocked history must not be relabeled as an app-restart cancel.
    reloaded = batch.BatchManager(ProviderExporter(), service, tmp_path / "config", _log())
    assert reloaded.latest()["status"] == "blocked"
    assert reloaded.latest()["counts"] == blocked["counts"]
    service.failure = None
    completed = _finished(manager, manager.start(**options)["id"])
    assert completed["counts"]["done"] == 2
    assert completed["counts"]["skipped"] == 1
    assert original.read_bytes() == before[0]
    assert original.stat().st_mtime_ns == before[1]
    assert len(list(output.glob("*.feedpak"))) == 3


def test_batch_cancel_during_wait_preserves_completed_output(tmp_path):
    service = ControlledService(wait_on=2)
    manager, options, output = _batch_case(tmp_path, service)
    started = manager.start(**options)
    try:
        assert service.waiting.wait(1.0)
        manager.cancel(started["id"])
        canceled = _finished(manager, started["id"])
    finally:
        service.release.set()
    assert canceled["status"] == "canceled"
    assert canceled["counts"]["done"] == 1
    assert canceled["counts"]["canceled"] == 2
    assert canceled["counts"]["failed"] == canceled["counts"]["blocked"] == 0
    assert service.calls == 2
    assert len(list(output.glob("*.feedpak"))) == 1


def test_batch_source_failure_still_continues_to_next_song(tmp_path):
    service = ControlledService(wait_on=2, failure=RuntimeError("inference failed for this song"))
    service.release.set()
    manager, options, output = _batch_case(tmp_path, service)
    completed = _finished(manager, manager.start(**options)["id"])
    assert completed["status"] == "completed"
    assert completed["counts"]["done"] == 2
    assert completed["counts"]["failed"] == 1
    assert completed["counts"]["blocked"] == 0
    assert service.calls == 3
    assert len(list(output.glob("*.feedpak"))) == 2


def test_saved_stem_batch_never_checks_or_calls_server(tmp_path):
    class ForbiddenService:
        def status(self):
            raise AssertionError("saved stems must not check the server")

        def separate(self, *args, **kwargs):
            raise AssertionError("saved stems must not call the server")

    manager, options, output = _batch_case(tmp_path, ForbiddenService(), saved=True)
    completed = _finished(manager, manager.start(**options)["id"])
    assert completed["counts"]["done"] == 3
    assert completed["counts"]["temporary_separations"] == 0
    assert len(list(output.glob("*.feedpak"))) == 3


def test_saved_stem_single_never_checks_or_calls_server(tmp_path):
    class ForbiddenService:
        def status(self):
            raise AssertionError("saved stems must not check the server")

        def separate(self, *args, **kwargs):
            raise AssertionError("saved stems must not call the server")

    source = _pak(tmp_path / "source.feedpak", guitar=True)
    output = tmp_path / "output"
    output.mkdir()
    manager = single.SingleExportManager(ProviderExporter(), ForbiddenService(), _log())
    completed = _finished(manager, manager.start(source, output, ["guitar"])["id"])
    assert completed["status"] == "completed"
    assert completed["result"]["temporary_separation_used"] is False
    assert len(list(output.glob("*.feedpak"))) == 1


def test_batch_rejects_hard_setup_error_before_queue_creation(tmp_path):
    service = ControlledService(status={"ready": False, "waitable": False,
                                        "state": "missing_model", "reason": "Install selected model"})
    manager, options, _output = _batch_case(tmp_path, service)
    with pytest.raises(batch.BatchError, match="Install selected model"):
        manager.start(**options)
    assert manager.latest() is None
    assert not manager.is_active()
    assert service.calls == 0


def test_batch_scan_advertises_only_missing_stems_for_ready_rows(tmp_path):
    source_root, output_root = tmp_path / "sources", tmp_path / "outputs"
    output_root.mkdir()
    _pak(source_root / "saved.feedpak", guitar=True)
    _pak(source_root / "skipped.feedpak", derived=True)
    scan = batch.scan_sources(ProviderExporter(), str(source_root), str(output_root),
                              ["guitar", "piano"], skip_derived=True)
    assert scan["required_separation_stems"] == ["piano"]
    limited = dict(scan, items=scan["items"][:1])
    assert limited["required_separation_stems"] == ["piano"]


def test_batch_blocks_unsupported_missing_stem_without_blocking_saved_stem(tmp_path):
    service = ControlledService(status={"ready": True, "supported_stems": ["guitar"]})
    service.release.set()
    manager, options, _output = _batch_case(tmp_path, service, saved=True)
    options["excluded_stems"] = ["guitar", "piano"]
    with pytest.raises(batch.BatchError, match="does not provide: piano"):
        manager.start(**options)
    assert service.calls == 0


def test_batch_does_not_require_model_to_provide_saved_selected_stem(tmp_path):
    service = ControlledService(status={"ready": True, "supported_stems": ["piano"]})
    service.release.set()
    manager, options, _output = _batch_case(tmp_path, service, saved=True)
    options["excluded_stems"] = ["guitar", "piano"]
    completed = _finished(manager, manager.start(**options)["id"])
    assert completed["status"] == "completed"
    assert completed["counts"]["done"] == 3
    assert completed["counts"]["failed"] == 0
    assert service.calls == 3


@pytest.mark.parametrize("find", [single._service_block, batch._service_block])
def test_service_block_search_handles_context_and_exception_cycles(find):
    block = ServiceBlock("missing_model")
    wrapper = RuntimeError("wrapped")
    wrapper.__context__ = block
    assert find(wrapper) is block
    first, second = RuntimeError("one"), RuntimeError("two")
    first.__cause__ = second
    second.__cause__ = first
    assert find(first) is None
    outer = block
    for _ in range(40):
        wrapper = RuntimeError("wrapped")
        wrapper.__cause__ = outer
        outer = wrapper
    assert find(outer) is None


def test_bounded_batch_snapshot_keeps_blocked_row():
    items = [{"relative_path": f"song-{i}.feedpak", "status": "queued"} for i in range(20)]
    items[0]["status"] = "blocked"
    indexes = batch.BatchManager._selected_item_indexes(items, 3)
    assert 0 in indexes


def test_large_blocked_queue_compacts_and_persists_actionable_history(tmp_path):
    total, current = 10_000, 8
    progress = (current + 0.41) / total
    items = [
        {"relative_path": f"song-{index}.feedpak",
         "status": "done" if index < current else "running" if index == current else "queued",
         "stage": "waiting_for_server" if index == current else "queued",
         "progress": 1.0 if index < current else 0.41 if index == current else 0.0}
        for index in range(total)
    ]
    counts = {"total": total, "done": current, "queued": total - current - 1,
              "failed": 0, "skipped": 0, "canceled": 0, "blocked": 0}
    expected_counts = {**counts, "blocked": 1}
    manager = batch.BatchManager(ProviderExporter(), object(), tmp_path / "config", _log())
    manager.jobs = {
        f"older-{index}": {"id": f"older-{index}", "status": "completed", "items": [],
                           "created_at": f"2000-01-01T00:00:{index:02d}"}
        for index in range(batch.MAX_RETAINED_JOBS)
    }
    job = {"id": "blocked-large", "status": "running", "items": items,
           "created_at": "2026-09-14T00:00:00", "counts": counts,
           "current_relative_path": items[current]["relative_path"],
           "current_item_number": current + 1, "overall_progress": progress,
           "_progress_values": [row["progress"] for row in items],
           "_progress_units": current + 0.41}
    manager.jobs[job["id"]] = job
    manager.active_id = job["id"]
    context = batch.BatchRunContext(job["id"], tmp_path, tmp_path, ("guitar",), True,
                                    threading.Event())

    manager._mark_item_blocked(context, current, ServiceBlock("recovery_exhausted"))
    manager._finish_run(context)

    assert job["status"] == "blocked"
    assert job["counts"] == expected_counts
    assert job["overall_progress"] == progress
    assert job["items_total"] == total
    assert job["items_truncated"] is True
    assert len(job["items"]) == batch.MAX_PERSISTED_ITEMS
    assert job["items"][0]["relative_path"] == f"song-{current}.feedpak"
    assert job["items"][0]["status"] == "blocked"
    assert all(row["status"] == "queued" for row in job["items"][1:])
    assert "_progress_values" not in job and "_progress_units" not in job
    assert len(manager.jobs) == batch.MAX_RETAINED_JOBS
    assert "older-0" not in manager.jobs
    persisted = next(row for row in json.loads(manager.state_file.read_text())["jobs"]
                     if row["id"] == job["id"])
    assert len(persisted["items"]) == batch.MAX_PERSISTED_ITEMS
    assert persisted["items_total"] == total and persisted["items_truncated"] is True
    assert persisted["counts"] == job["counts"]
    assert persisted["overall_progress"] == progress
    restored = batch.BatchManager(ProviderExporter(), object(), tmp_path / "config", _log()).latest()
    assert restored["status"] == "blocked"
    assert restored["items"] == persisted["items"]
