"""Background single-export job tests without running a real separator."""
from __future__ import annotations

import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import yaml

import exporter
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
    def export_minus_mix(source, output_dir, selected, *, stem_provider,
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


def test_single_export_reuses_its_unchanged_prepared_source(tmp_path):
    source = tmp_path / "song.feedpak"
    source.write_bytes(b"source")
    prepared = SimpleNamespace(info=SimpleNamespace(title="Prepared Song"))
    received = []

    class PreparedExporter(CompletingExporter):
        @staticmethod
        def prepare_source(source_path):
            assert source_path == source.resolve()
            return prepared

        @staticmethod
        def inspect_source(source_path):
            raise AssertionError("prepared source should avoid a second inspection")

        @staticmethod
        def export_minus_mix(source, output_dir, selected, *, prepared_source,
                             stem_provider, progress_cb, cancel_cb, log):
            received.append(prepared_source)
            return CompletingExporter.export_minus_mix(
                source, output_dir, selected, stem_provider=stem_provider,
                progress_cb=progress_cb,
                cancel_cb=cancel_cb, log=log,
            )

    manager = single.SingleExportManager(
        PreparedExporter(), SimpleNamespace(), _log(),
    )
    started = manager.start(source, tmp_path, ["guitar"])
    completed = _wait(manager, started["id"])

    assert completed["status"] == "completed"
    assert received == [prepared]


def test_cancel_after_atomic_publication_keeps_single_export_completed(
        tmp_path, monkeypatch):
    source = tmp_path / "song.feedpak"
    manifest = {
        "title": "Test Song",
        "artist": "Test Artist",
        "stems": [
            {"id": "full", "file": "stems/full.ogg"},
            {"id": "guitar", "file": "stems/guitar.ogg"},
        ],
    }
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("manifest.yaml", yaml.safe_dump(manifest))
        archive.writestr("stems/full.ogg", b"full")
        archive.writestr("stems/guitar.ogg", b"guitar")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    published = threading.Event()
    release = threading.Event()

    def render_without_ffmpeg(
            ffmpeg, prepared, selected, extracted, temporary, work, cancel_cb):
        del ffmpeg, prepared, selected, extracted, temporary, cancel_cb
        full_mix = work / "minus-mix-full.ogg"
        preview = work / "preview.ogg"
        full_mix.write_bytes(b"rendered")
        preview.write_bytes(b"preview")
        return exporter.RenderedAudio(full_mix, preview, True)

    def publish_then_wait(prepared, destination, plan):
        target = exporter.desired_output_path(
            destination, prepared.source, suffix=plan.suffix,
        )
        target.write_bytes(b"published")
        published.set()
        assert release.wait(1.0)
        return target

    monkeypatch.setattr(exporter, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(exporter, "_render_export_audio", render_without_ffmpeg)
    monkeypatch.setattr(exporter, "_publish_package", publish_then_wait)
    manager = single.SingleExportManager(
        exporter, SimpleNamespace(status=lambda: {"ready": False}), _log(),
    )

    started = manager.start(source, output_dir, ["guitar"])
    assert published.wait(1.0)
    canceling = manager.cancel(started["id"])
    release.set()
    completed = _wait(manager, started["id"])

    assert canceling["status"] == "canceling"
    assert completed["status"] == "completed"
    assert completed["result"]["filename"] == "song (No Guitar).feedpak"
    assert (output_dir / completed["result"]["filename"]).read_bytes() == b"published"


def test_single_export_cancel_reaches_worker_checkpoint(tmp_path):
    source = tmp_path / "song.feedpak"
    source.write_bytes(b"source")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    entered = threading.Event()

    class BlockingExporter(CompletingExporter):
        @staticmethod
        def export_minus_mix(source, output_dir, selected, *, stem_provider,
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
