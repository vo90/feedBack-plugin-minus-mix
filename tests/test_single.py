"""Background single-export job tests without running a real separator."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import single


def _wait(manager: single.SingleExportManager, job_id: str, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job["status"] not in single.ACTIVE_STATUSES:
            return job
        time.sleep(0.01)
    raise AssertionError("single export did not finish")


class CompletingExporter:
    @staticmethod
    def inspect_source(source: Path):
        return SimpleNamespace(title="Test Song")

    @staticmethod
    def export_minus_mix(source, output_dir, selected, *, separate_missing,
                         progress_cb, cancel_cb, log):
        cancel_cb()
        progress_cb("rendering", 0.78, "Creating backing track")
        target = Path(output_dir) / "Test Song (No Guitar).feedpak"
        target.write_bytes(b"finished")
        return SimpleNamespace(
            output_path=target,
            output_filename=target.name,
            title="Test Song (No Guitar)",
            excluded_stems=("guitar",),
            preview_created=True,
            temporary_separation_used=False,
        )


def _log():
    return SimpleNamespace(exception=lambda *args, **kwargs: None)


def test_single_export_runs_in_background_and_publishes_result(tmp_path):
    source = tmp_path / "song.feedpak"
    source.write_bytes(b"source")
    manager = single.SingleExportManager(
        CompletingExporter(), SimpleNamespace(status=lambda: {"ready": False}), _log(),
    )

    started = manager.start(source, tmp_path, ["guitar"])
    completed = _wait(manager, started["id"])

    assert completed["status"] == "completed"
    assert completed["stage"] == "done"
    assert completed["progress"] == 1.0
    assert completed["result"]["filename"] == "Test Song (No Guitar).feedpak"
    assert completed["result"]["source_unchanged"] is True
    assert manager.is_active() is False


def test_single_export_cancel_reaches_worker_checkpoint(tmp_path):
    source = tmp_path / "song.feedpak"
    source.write_bytes(b"source")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    entered = threading.Event()

    class BlockingExporter(CompletingExporter):
        @staticmethod
        def export_minus_mix(source, output_dir, selected, *, separate_missing,
                             progress_cb, cancel_cb, log):
            progress_cb("rendering", 0.78, "Creating backing track")
            entered.set()
            while True:
                cancel_cb()
                time.sleep(0.01)

    manager = single.SingleExportManager(
        BlockingExporter(), SimpleNamespace(status=lambda: {"ready": False}), _log(),
    )
    started = manager.start(source, output_dir, ["guitar"])
    assert entered.wait(1.0)

    canceling = manager.cancel(started["id"])
    canceled = _wait(manager, started["id"])

    assert canceling["status"] == "canceling"
    assert canceled["status"] == "canceled"
    assert canceled["detail"] == "Canceled"
    assert not list(output_dir.glob("*.feedpak"))
