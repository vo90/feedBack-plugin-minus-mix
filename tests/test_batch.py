"""Recursive batch queue tests without invoking a real separation model."""
from __future__ import annotations

import hashlib
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
import routes


def _pak(path: Path, *, full: bytes = b"same full audio", guitar: bool = False,
         derived: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    stems = [{"id": "full", "file": "stems/full.ogg"}]
    if guitar:
        stems.append({"id": "guitar", "file": "stems/guitar.ogg"})
    manifest = {"title": path.stem, "artist": "Test", "stems": stems}
    if derived:
        manifest["minus_mix"] = {
            "excluded_stems": ["guitar"], "generator": "minus_mix",
        }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.yaml", yaml.safe_dump(manifest, sort_keys=False))
        zf.writestr("stems/full.ogg", full)
        if guitar:
            zf.writestr("stems/guitar.ogg", b"guitar")
    return path


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeExporter:
    ExportError = exporter.ExportError
    inspect_source = staticmethod(exporter.inspect_source)
    desired_output_path = staticmethod(exporter.desired_output_path)
    stem_label = staticmethod(exporter.stem_label)
    validate_output_directory = staticmethod(exporter.validate_output_directory)

    @staticmethod
    def export_minus_mix(source, output_dir, selected, *, stem_provider,
                         progress_cb, cancel_cb, log):
        cancel_cb()
        info = exporter.inspect_source(source)
        saved = {stem.id for stem in info.stems}
        missing = tuple(stem for stem in selected if stem not in saved)
        if missing:
            with tempfile.TemporaryDirectory() as td:
                work = Path(td)
                full = work / "full.ogg"
                with zipfile.ZipFile(source) as zf:
                    full.write_bytes(zf.read(info.full_mix_file))
                separation = work / "separation"
                separation.mkdir()
                progress_cb("separating", 0.2, "fake separating")
                digest = hashlib.sha256(full.read_bytes()).hexdigest()
                produced = stem_provider.obtain(full, separation, missing, digest)
                assert all(stem in produced for stem in missing)
        progress_cb("packaging", 0.9, "fake packaging")
        wanted = exporter.desired_output_path(output_dir, source, selected)
        target = wanted
        for number in range(2, 10_000):
            if not target.exists():
                break
            target = wanted.with_name(f"{wanted.stem} ({number}){wanted.suffix}")
        else:
            raise AssertionError("fake exporter could not allocate an output name")
        target.write_bytes(b"compact practice pak")
        return SimpleNamespace(
            output_path=target, output_filename=target.name,
            temporary_separation_used=bool(missing),
            preview_created=True,
        )


class FakeService:
    def __init__(self):
        self.calls = 0
        self.returned_dirs: list[Path] = []

    def status(self):
        return {"ready": True, "reason": "fake GPU"}

    def separate(self, mix, work, stems, *, progress_cb, cancel_cb):
        self.calls += 1
        cancel_cb()
        progress_cb(0.5, "model running")
        self.returned_dirs.append(Path(work))
        result = {}
        for stem in stems:
            path = Path(work) / f"{stem}.flac"
            path.write_bytes((stem + " audio").encode())
            result[stem] = path
        progress_cb(1.0, "model done")
        return result


def _wait(manager: batch.BatchManager, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job["status"] not in batch.ACTIVE_STATUSES:
            return job
        time.sleep(0.02)
    raise AssertionError("batch did not finish")


def test_recursive_scan_preserves_structure_and_skips_existing_derived_and_invalid(tmp_path):
    source_root = tmp_path / "sources"
    output_root = source_root / "generated"
    output_root.mkdir(parents=True)
    normal = _pak(source_root / "Band" / "normal.feedpak")
    _pak(source_root / "Band" / "saved.feedpak", guitar=True)
    _pak(source_root / "derived.feedpak", derived=True)
    (source_root / "broken.feedpak").write_bytes(b"not a zip")
    # Output lives below the source tree and must never be scanned recursively.
    _pak(output_root / "old-output.feedpak")
    expected = exporter.desired_output_path(output_root / "Band", normal, ["guitar"])
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_bytes(b"already done")

    result = batch.scan_sources(
        exporter, str(source_root), str(output_root), ["guitar"],
        recursive=True, skip_existing=True, skip_derived=True,
    )

    assert result["counts"] == {
        "found": 4,
        "ready": 1,
        "needs_separation": 0,
        "uses_saved_stems": 1,
        "skipped_existing": 1,
        "skipped_derived": 1,
        "invalid": 1,
        "duplicate_target": 0,
        "renamed_collisions": 0,
    }
    assert result["preserve_structure"] is True
    assert result["preserves_relative_folders"] is True
    assert all(not item["relative_path"].startswith("generated/") for item in result["items"])
    saved = next(item for item in result["items"] if item["relative_path"].endswith("saved.feedpak"))
    assert saved["output_relative"] == "Band/saved (No Guitar).feedpak"
    assert saved["needs_separation"] is False


def test_recursive_scan_and_batch_can_flatten_outputs_with_numbered_collisions(tmp_path):
    source_root = tmp_path / "sources"
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    _pak(source_root / "Artist A" / "song.feedpak", guitar=True)
    _pak(source_root / "Artist B" / "song.feedpak", guitar=True)
    manager = batch.BatchManager(FakeExporter(), FakeService(), tmp_path / "config", SimpleNamespace(
        exception=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    ))

    scan = manager.scan(
        input_dir=str(source_root), output_dir=str(output_root), excluded_stems=["guitar"],
        recursive=True, skip_existing=True, skip_derived=True, preserve_structure=False,
    )

    assert scan["preserve_structure"] is False
    assert scan["preserves_relative_folders"] is False
    assert scan["counts"]["ready"] == 2
    assert scan["counts"]["renamed_collisions"] == 1
    assert [item["output_relative"] for item in scan["items"]] == [
        "song (No Guitar).feedpak",
        "song (No Guitar) (2).feedpak",
    ]

    started = manager.start(
        input_dir=str(source_root), output_dir=str(output_root), excluded_stems=["guitar"],
        recursive=True, skip_existing=True, skip_derived=True, preserve_structure=False,
    )
    completed = _wait(manager, started["id"])

    assert completed["status"] == "completed"
    assert completed["preserve_structure"] is False
    assert (output_root / "song (No Guitar).feedpak").is_file()
    assert (output_root / "song (No Guitar) (2).feedpak").is_file()
    assert not (output_root / "Artist A").exists()
    assert not (output_root / "Artist B").exists()

    resumed = manager.scan(
        input_dir=str(source_root), output_dir=str(output_root), excluded_stems=["guitar"],
        recursive=True, skip_existing=True, skip_derived=True, preserve_structure=False,
    )
    assert resumed["counts"]["ready"] == 0
    assert resumed["counts"]["skipped_existing"] == 2


def test_batch_reuses_identical_temporary_separation_and_persists_status(tmp_path):
    source_root = tmp_path / "sources"
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    first = _pak(source_root / "A" / "one.feedpak", full=b"identical audio")
    second = _pak(source_root / "B" / "two.feedpak", full=b"identical audio")
    original_hashes = {_hash(first), _hash(second)}
    service = FakeService()
    manager = batch.BatchManager(FakeExporter(), service, tmp_path / "config", SimpleNamespace(
        exception=lambda *args, **kwargs: None,
    ))

    started = manager.start(
        input_dir=str(source_root), output_dir=str(output_root), excluded_stems=["guitar"],
        recursive=True, skip_existing=True, skip_derived=True,
    )
    completed = _wait(manager, started["id"])

    assert completed["status"] == "completed"
    assert completed["counts"]["done"] == 2
    assert completed["counts"]["failed"] == 0
    assert completed["counts"]["temporary_separations"] == 2
    assert completed["counts"]["duplicate_audio_reused"] == 1
    assert service.calls == 1
    assert (output_root / "A" / "one (No Guitar).feedpak").is_file()
    assert (output_root / "B" / "two (No Guitar).feedpak").is_file()
    assert {_hash(first), _hash(second)} == original_hashes
    assert service.returned_dirs and all(not path.exists() for path in service.returned_dirs)
    assert (tmp_path / "config" / "minus_mix_batch_jobs.json").is_file()


def test_batch_start_reuses_an_unchanged_authoritative_scan(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    _pak(source_root / "Artist" / "song.feedpak", guitar=True)
    manager = batch.BatchManager(
        FakeExporter(), FakeService(), tmp_path / "config",
        SimpleNamespace(exception=lambda *args, **kwargs: None,
                        warning=lambda *args, **kwargs: None),
    )
    options = {
        "input_dir": str(source_root), "output_dir": str(output_root),
        "excluded_stems": ["guitar"], "recursive": True,
        "skip_existing": True, "skip_derived": True,
    }
    scanned = manager.scan(**options)

    monkeypatch.setattr(
        manager, "scan",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("source was parsed twice")),
    )
    started = manager.start(scan_id=scanned["scan_id"], **options)
    completed = _wait(manager, started["id"])

    assert completed["status"] == "completed"
    assert completed["scan_reused"] is True
    assert completed["counts"]["done"] == 1


def test_persistence_keeps_only_completed_job_summaries(tmp_path):
    manager = batch.BatchManager(
        FakeExporter(), FakeService(), tmp_path / "config",
        SimpleNamespace(exception=lambda *args, **kwargs: None,
                        warning=lambda *args, **kwargs: None),
    )
    items = [
        {"relative_path": f"Artist/song-{index}.feedpak", "status": "done"}
        for index in range(10_000)
    ]
    manager.jobs["large"] = {
        "id": "large", "created_at": "2026-08-11T00:00:00+00:00",
        "status": "completed", "counts": {"total": len(items)},
        "items": items, "_private_runtime_state": ["must not persist"],
    }

    manager._persist(force=True)
    saved = json.loads(manager.state_file.read_text(encoding="utf-8"))["jobs"][0]

    assert saved["items_total"] == 10_000
    assert saved["items_truncated"] is True
    assert saved["items"] == []
    assert "_private_runtime_state" not in saved


def test_terminal_jobs_release_large_in_memory_item_history(tmp_path):
    manager = batch.BatchManager(
        FakeExporter(), FakeService(), tmp_path / "config",
        SimpleNamespace(exception=lambda *args, **kwargs: None,
                        warning=lambda *args, **kwargs: None),
    )
    items = [
        {"relative_path": f"Artist/song-{index}.feedpak", "status": "done"}
        for index in range(10_000)
    ]
    items[3]["status"] = "failed"
    job = {
        "id": "large", "status": "completed", "items": items,
        "_progress_values": [1.0] * len(items), "_progress_units": float(len(items)),
    }

    with manager.lock:
        manager._compact_terminal_job_locked(job)

    assert job["items_total"] == 10_000
    assert job["items_truncated"] is True
    assert len(job["items"]) == batch.MAX_PERSISTED_ITEMS
    assert any(item["relative_path"] == "Artist/song-3.feedpak" for item in job["items"])
    assert "_progress_values" not in job
    assert "_progress_units" not in job


def test_in_memory_job_store_keeps_active_job_and_newest_summaries(tmp_path):
    manager = batch.BatchManager(
        FakeExporter(), FakeService(), tmp_path / "config",
        SimpleNamespace(exception=lambda *args, **kwargs: None,
                        warning=lambda *args, **kwargs: None),
    )
    manager.jobs = {
        f"job-{index}": {
            "id": f"job-{index}",
            "status": "running" if index == 0 else "completed",
            "created_at": f"2026-08-{index + 1:02d}T00:00:00+00:00",
            "items": [],
        }
        for index in range(7)
    }
    manager.active_id = "job-0"

    with manager.lock:
        manager._prune_jobs_locked()

    assert set(manager.jobs) == {"job-0", "job-3", "job-4", "job-5", "job-6"}


def test_persistence_failure_never_fails_the_audio_job(tmp_path, monkeypatch):
    warnings = []
    manager = batch.BatchManager(
        FakeExporter(), FakeService(), tmp_path / "config",
        SimpleNamespace(
            exception=lambda *args, **kwargs: None,
            warning=lambda *args: warnings.append(args),
        ),
    )
    manager.jobs["completed"] = {
        "id": "completed", "created_at": "2026-08-11T00:00:00+00:00",
        "status": "completed", "counts": {"done": 1}, "items": [],
    }

    monkeypatch.setattr(
        Path, "write_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    manager._persist(force=True)

    assert manager.jobs["completed"]["status"] == "completed"
    assert warnings and "could not persist" in warnings[0][0]


def test_background_scan_reports_progress_and_cancels_between_sources(tmp_path):
    source_root = tmp_path / "sources"
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    _pak(source_root / "one.feedpak", guitar=True)
    _pak(source_root / "two.feedpak", guitar=True)
    entered = threading.Event()
    release = threading.Event()

    class SlowInspectExporter(FakeExporter):
        calls = 0

        @staticmethod
        def inspect_source(source):
            SlowInspectExporter.calls += 1
            entered.set()
            release.wait(1.0)
            return exporter.inspect_source(source)

    manager = batch.BatchManager(
        SlowInspectExporter(), FakeService(), tmp_path / "config",
        SimpleNamespace(exception=lambda *args, **kwargs: None,
                        warning=lambda *args, **kwargs: None),
    )
    started = manager.start_scan(
        input_dir=str(source_root), output_dir=str(output_root),
        excluded_stems=["guitar"], recursive=True,
        skip_existing=True, skip_derived=True,
    )
    assert entered.wait(1.0)
    canceling = manager.cancel_scan(started["id"])
    assert canceling["status"] == "canceling"
    release.set()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        finished = manager.get_scan(started["id"])
        if finished["status"] == "canceled":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("folder scan did not cancel")

    assert 0.0 <= finished["progress"] < 1.0
    assert finished["result"] is None
    assert SlowInspectExporter.calls == 1


def test_batch_cancel_stops_at_model_checkpoint_and_cancels_waiting_files(tmp_path):
    source_root = tmp_path / "sources"
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    _pak(source_root / "one.feedpak")
    _pak(source_root / "two.feedpak", full=b"different")

    class SlowService(FakeService):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()

        def separate(self, mix, work, stems, *, progress_cb, cancel_cb):
            self.calls += 1
            self.started.set()
            while True:
                cancel_cb()
                time.sleep(0.01)

    service = SlowService()
    manager = batch.BatchManager(FakeExporter(), service, tmp_path / "config", SimpleNamespace(
        exception=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    ))
    started = manager.start(
        input_dir=str(source_root), output_dir=str(output_root), excluded_stems=["guitar"],
        recursive=True, skip_existing=True, skip_derived=True,
    )
    assert service.started.wait(1.0)
    manager.cancel(started["id"])
    canceled = _wait(manager, started["id"])

    assert canceled["status"] == "canceled"
    assert canceled["counts"]["done"] == 0
    assert canceled["counts"]["canceled"] == 2
    assert not list(output_root.rglob("*.feedpak"))


def test_batch_rejects_unwritable_destination_before_worker_starts(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    _pak(source_root / "one.feedpak", guitar=True)
    manager = batch.BatchManager(
        FakeExporter(), FakeService(), tmp_path / "config",
        SimpleNamespace(
            exception=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
        ),
    )

    monkeypatch.setattr(
        exporter.tempfile, "mkstemp",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("read only")),
    )

    with pytest.raises(batch.BatchError, match="cannot write"):
        manager.start(
            input_dir=str(source_root), output_dir=str(output_root),
            excluded_stems=["guitar"], recursive=True,
            skip_existing=True, skip_derived=True,
        )

    assert manager.starting is False
    assert manager.active_id is None
    assert manager.jobs == {}


def test_batch_cancel_after_publication_completes_current_and_cancels_rest(tmp_path):
    source_root = tmp_path / "sources"
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    _pak(source_root / "one.feedpak", guitar=True)
    _pak(source_root / "two.feedpak", guitar=True)

    class PublishingExporter(FakeExporter):
        def __init__(self):
            self.published = threading.Event()
            self.release = threading.Event()

        def export_minus_mix(self, source, output_dir, selected, *, stem_provider,
                             progress_cb, cancel_cb, log):
            del stem_provider, progress_cb, cancel_cb, log
            target = exporter.desired_output_path(output_dir, source, selected)
            target.write_bytes(b"published")
            self.published.set()
            assert self.release.wait(1.0)
            return SimpleNamespace(
                output_path=target, output_filename=target.name,
                temporary_separation_used=False, preview_created=True,
            )

    publishing_exporter = PublishingExporter()
    manager = batch.BatchManager(
        publishing_exporter, FakeService(), tmp_path / "config",
        SimpleNamespace(
            exception=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
        ),
    )
    started = manager.start(
        input_dir=str(source_root), output_dir=str(output_root),
        excluded_stems=["guitar"], recursive=True,
        skip_existing=True, skip_derived=True,
    )
    assert publishing_exporter.published.wait(1.0)

    canceling = manager.cancel(started["id"])
    publishing_exporter.release.set()
    canceled = _wait(manager, started["id"])

    assert canceling["status"] == "canceling"
    assert canceled["status"] == "canceled"
    assert canceled["counts"]["done"] == 1
    assert canceled["counts"]["canceled"] == 1
    assert (output_root / "one (No Guitar).feedpak").is_file()
    assert not (output_root / "two (No Guitar).feedpak").exists()


def test_batch_reserves_start_while_authoritative_scan_is_running(tmp_path):
    source_root = tmp_path / "sources"
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    _pak(source_root / "one.feedpak", guitar=True)
    manager = batch.BatchManager(FakeExporter(), FakeService(), tmp_path / "config", SimpleNamespace(
        exception=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    ))
    original_scan = manager.scan
    entered = threading.Event()
    release = threading.Event()
    started: list[dict] = []

    def slow_scan(**options):
        entered.set()
        assert release.wait(1.0)
        return original_scan(**options)

    manager.scan = slow_scan
    options = dict(
        input_dir=str(source_root), output_dir=str(output_root), excluded_stems=["guitar"],
        recursive=True, skip_existing=True, skip_derived=True,
    )
    worker = threading.Thread(target=lambda: started.append(manager.start(**options)))
    worker.start()
    assert entered.wait(1.0)

    try:
        with pytest.raises(batch.BatchError, match="already running"):
            manager.start(**options)
    finally:
        release.set()
        worker.join(2.0)

    assert not worker.is_alive()
    assert started
    _wait(manager, started[0]["id"])


def test_public_batch_snapshot_is_bounded_and_keeps_actionable_rows():
    items = [
        {"relative_path": f"song-{index}.feedpak", "status": "queued"}
        for index in range(routes.MAX_PUBLIC_BATCH_ITEMS + 25)
    ]
    items[2]["status"] = "running"
    items[3]["status"] = "failed"
    payload = {"id": "batch-1", "items": items}

    result = routes._public_batch(payload)

    assert result["items_total"] == len(items)
    assert result["items_truncated"] is True
    assert len(result["items"]) == routes.MAX_PUBLIC_BATCH_ITEMS
    visible_paths = {item["relative_path"] for item in result["items"]}
    assert "song-2.feedpak" in visible_paths
    assert "song-3.feedpak" in visible_paths
    assert f"song-{len(items) - 1}.feedpak" in visible_paths
    assert payload["items"] is items


def test_public_batch_scan_preview_keeps_source_order():
    items = [
        {"relative_path": f"song-{index}.feedpak", "scan_status": "ready"}
        for index in range(routes.MAX_PUBLIC_BATCH_ITEMS + 1)
    ]

    result = routes._public_batch({"items": items}, scan=True)

    assert [item["relative_path"] for item in result["items"]] == [
        f"song-{index}.feedpak" for index in range(routes.MAX_PUBLIC_BATCH_ITEMS)
    ]


def test_manager_bounds_items_before_deepcopying_status_snapshot():
    class MustRemainHidden:
        def __deepcopy__(self, memo):
            raise AssertionError("an omitted batch row was deep-copied")

    total = routes.MAX_PUBLIC_BATCH_ITEMS + 25
    items = [
        {"relative_path": f"song-{index}.feedpak", "status": "queued"}
        for index in range(total)
    ]
    items[0]["private_test_value"] = MustRemainHidden()
    items[2]["status"] = "running"
    items[3]["status"] = "failed"
    manager = batch.BatchManager.__new__(batch.BatchManager)
    manager.lock = threading.RLock()
    manager.jobs = {"batch-1": {"id": "batch-1", "status": "running", "items": items}}

    result = manager.get("batch-1", item_limit=routes.MAX_PUBLIC_BATCH_ITEMS)

    assert result["items_total"] == total
    assert result["items_truncated"] is True
    assert len(result["items"]) == routes.MAX_PUBLIC_BATCH_ITEMS
    visible_paths = {item["relative_path"] for item in result["items"]}
    assert "song-0.feedpak" not in visible_paths
    assert "song-2.feedpak" in visible_paths
    assert "song-3.feedpak" in visible_paths
    public = routes._public_batch(result)
    assert public["items_total"] == total
    assert public["items_truncated"] is True
