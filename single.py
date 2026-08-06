"""Background job orchestration for one MinusMix export.

The audio/package implementation stays in :mod:`exporter`.  This layer only
turns that blocking operation into a small observable job so the browser can
show real stages, survive screen navigation, and request safe cancellation.
"""
from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


ACTIVE_STATUSES = {"queued", "running", "canceling"}


class SingleExportError(RuntimeError):
    """Expected refusal to start or address a single-export job."""


class SingleExportCanceled(RuntimeError):
    """Internal cancellation checkpoint."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SingleExportManager:
    """Run at most one single-song export at a time per app process."""

    def __init__(self, exporter, separator, log):
        self.exporter = exporter
        self.separator = separator
        self.log = log
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.active_id: str | None = None

    def _snapshot_locked(self, job: dict) -> dict:
        return copy.deepcopy(job)

    def latest(self) -> dict | None:
        with self.lock:
            if not self.jobs:
                return None
            job = max(self.jobs.values(), key=lambda value: value.get("created_at", ""))
            return self._snapshot_locked(job)

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return self._snapshot_locked(job) if job else None

    def is_active(self) -> bool:
        with self.lock:
            return bool(
                self.active_id
                and self.jobs.get(self.active_id, {}).get("status") in ACTIVE_STATUSES
            )

    def start(self, source: Path, output_dir: Path, excluded_stems) -> dict:
        selected: list[str] = []
        for value in excluded_stems or ():
            if not isinstance(value, str):
                raise SingleExportError("excluded stems must be instrument ids")
            stem = value.strip().lower()
            if stem and stem != "full" and stem not in selected:
                selected.append(stem)
        if not selected:
            raise SingleExportError("choose at least one instrument stem to exclude")

        source = Path(source).resolve()
        output_dir = Path(output_dir).resolve()
        info = self.exporter.inspect_source(source)
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "status": "queued",
            "created_at": _now(),
            "started_at": None,
            "completed_at": None,
            "source_filename": source.name,
            "source_title": info.title,
            "output_dir": str(output_dir),
            "excluded_stems": selected,
            "stage": "queued",
            "progress": 0.0,
            "detail": "Waiting to start",
            "cancel_requested": False,
            "result": None,
        }
        event = threading.Event()
        with self.lock:
            if self.is_active():
                raise SingleExportError("another single-song MinusMix export is already running")
            self.jobs[job_id] = job
            self.cancel_events[job_id] = event
            self.active_id = job_id
            # Keep bounded navigation history without touching an active job.
            ordered = sorted(self.jobs.values(), key=lambda value: value.get("created_at", ""))
            for old in ordered[:-10]:
                if old.get("id") != self.active_id:
                    self.jobs.pop(old.get("id"), None)

        thread = threading.Thread(
            target=self._run,
            args=(job_id, source, output_dir, tuple(selected)),
            name=f"minus-mix-single-{job_id[:8]}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.jobs[job_id].update({
                    "status": "failed", "stage": "failed", "progress": 1.0,
                    "detail": "The export worker could not be started", "completed_at": _now(),
                })
                self.cancel_events.pop(job_id, None)
                if self.active_id == job_id:
                    self.active_id = None
            raise
        return self.get(job_id)

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise SingleExportError("single-song export job not found")
            if job.get("status") not in ACTIVE_STATUSES:
                return self._snapshot_locked(job)
            job["cancel_requested"] = True
            job["status"] = "canceling"
            job["detail"] = "Cancel requested; stopping safely"
            event = self.cancel_events.get(job_id)
            if event:
                event.set()
            return self._snapshot_locked(job)

    def _update(self, job_id: str, *, stage: str, progress: float, detail: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job["stage"] = stage
            job["progress"] = max(0.0, min(1.0, float(progress)))
            job["detail"] = str(detail or "")[:500]

    def _run(self, job_id: str, source: Path, output_dir: Path,
             selected: tuple[str, ...]) -> None:
        with self.lock:
            job = self.jobs[job_id]
            event = self.cancel_events[job_id]
            job.update({
                "status": "running", "started_at": _now(),
                "stage": "validating", "progress": 0.01,
                "detail": "Checking source feedpak",
            })

        def checkpoint() -> None:
            if event.is_set():
                raise SingleExportCanceled("export canceled")

        def progress(stage: str, fraction: float, detail: str) -> None:
            checkpoint()
            self._update(job_id, stage=stage, progress=fraction, detail=detail)

        def separate_missing(mix: Path, work: Path,
                             stems: tuple[str, ...]) -> dict[str, Path]:
            checkpoint()
            status = self.separator.status()
            if not status.get("ready"):
                raise SingleExportError(str(
                    status.get("reason") or "Stem Splitter server unavailable"
                ))

            def separation_progress(value, message) -> None:
                checkpoint()
                mapped = 0.08 + max(0.0, min(1.0, float(value))) * 0.66
                self._update(
                    job_id, stage="separating", progress=mapped,
                    detail=str(message or "Separating audio"),
                )

            return self.separator.separate(
                mix, work, stems,
                progress_cb=separation_progress, cancel_cb=checkpoint,
            )

        try:
            result = self.exporter.export_minus_mix(
                source, output_dir, selected,
                separate_missing=separate_missing,
                progress_cb=progress, cancel_cb=checkpoint, log=self.log,
            )
            # Once export_minus_mix returns, its atomic rename has completed.
            # A late cancel must not claim that the already-created file vanished.
            payload = {
                "filename": result.output_filename,
                "path": str(result.output_path),
                "title": result.title,
                "excluded_stems": list(result.excluded_stems),
                "preview_created": result.preview_created,
                "temporary_separation_used": result.temporary_separation_used,
                "source_unchanged": True,
            }
            with self.lock:
                self.jobs[job_id].update({
                    "status": "completed", "stage": "done", "progress": 1.0,
                    "detail": f"Created {result.output_filename}",
                    "result": payload, "completed_at": _now(),
                })
        except Exception as exc:
            with self.lock:
                canceled = event.is_set()
                self.jobs[job_id].update({
                    "status": "canceled" if canceled else "failed",
                    "stage": "canceled" if canceled else "failed",
                    "progress": 0.0 if canceled else 1.0,
                    "detail": "Canceled" if canceled else (str(exc)[:500] or type(exc).__name__),
                    "completed_at": _now(),
                })
            if not canceled:
                self.log.exception("minus_mix: single export failed")
        finally:
            with self.lock:
                if self.active_id == job_id:
                    self.active_id = None
                self.cancel_events.pop(job_id, None)
