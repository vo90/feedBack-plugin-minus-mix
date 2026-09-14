"""Background job orchestration for one MinusMix export.

The audio/package implementation stays in :mod:`exporter`.  This layer only
turns that blocking operation into a small observable job so the browser can
show real stages, survive screen navigation, and request safe cancellation.
"""
from __future__ import annotations

import copy
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

ACTIVE_STATUSES = {"queued", "running", "canceling"}


class SingleExportResult(TypedDict, total=False):
    filename: str
    path: str
    title: str
    excluded_stems: list[str]
    preview_created: bool
    temporary_separation_used: bool
    source_unchanged: bool


class SingleJob(TypedDict, total=False):
    id: str
    status: str
    created_at: str
    completed_at: str | None
    source_filename: str
    source_title: str
    output_dir: str
    excluded_stems: list[str]
    stage: str
    progress: float
    detail: str
    state: str
    result: SingleExportResult | None


class SingleExportError(RuntimeError):
    """Expected refusal to start or address a single-export job."""


class SingleExportCanceled(RuntimeError):
    """Internal cancellation checkpoint."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _service_block(exc: BaseException) -> BaseException | None:
    """Find a structured service block through the exporter's error wrapper."""
    seen: set[int] = set()
    for _ in range(32):
        if id(exc) in seen:
            break
        seen.add(id(exc))
        if getattr(exc, "blocks_batch", False) is True:
            return exc
        cause = exc.__cause__ or exc.__context__
        if cause is None:
            break
        exc = cause
    return None


class SeparatorStemProvider:
    """Adapt the server client to the exporter's stem-provider boundary."""

    def __init__(self, separator, checkpoint: Callable[[], None],
                 progress: Callable[[float, str], None],
                 state: Callable[[dict], None] | None = None):
        self.separator = separator
        self.checkpoint = checkpoint
        self.progress = progress
        self.state = state

    def obtain(self, mix: Path, work: Path, stems: tuple[str, ...],
               full_digest: str | None) -> dict[str, Path]:
        del full_digest  # Single exports do not need the batch duplicate cache.
        self.checkpoint()
        options = {"progress_cb": self.progress, "cancel_cb": self.checkpoint}
        if getattr(self.separator, "supports_state_callback", False):
            options["state_cb"] = self.state
        return self.separator.separate(mix, work, stems, **options)


class SingleExportManager:
    """Run at most one single-song export at a time per app process."""

    def __init__(self, exporter, separator, log):
        self.exporter = exporter
        self.separator = separator
        self.log = log
        self.lock = threading.RLock()
        self.jobs: dict[str, SingleJob] = {}
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
        prepare = getattr(self.exporter, "prepare_source", None)
        prepared_source = prepare(source) if callable(prepare) else None
        info = prepared_source.info if prepared_source is not None else self.exporter.inspect_source(source)
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
            args=(job_id, source, output_dir, tuple(selected), prepared_source),
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
             selected: tuple[str, ...], prepared_source=None) -> None:
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

        separation_stage = "separating"

        def separation_progress(value, message) -> None:
            checkpoint()
            mapped = 0.08 + max(0.0, min(1.0, float(value))) * 0.66
            with self.lock:
                mapped = max(mapped, self.jobs[job_id]["progress"])
                self._update(
                    job_id, stage=separation_stage, progress=mapped,
                    detail=str(message or "Separating audio"),
                )

        def separation_state(payload: dict) -> None:
            nonlocal separation_stage
            checkpoint()
            state = payload.get("state") if isinstance(payload, dict) else None
            if state not in {"waiting_for_server", "separating", "downloading"}:
                return
            separation_stage = "waiting_for_server" if state == "waiting_for_server" else "separating"
            with self.lock:
                self._update(
                    job_id, stage=separation_stage, progress=self.jobs[job_id]["progress"],
                    detail=str(payload.get("detail") or "Separating audio"),
                )

        stem_provider = SeparatorStemProvider(
            self.separator, checkpoint, separation_progress, separation_state,
        )

        try:
            export_options = {
                "stem_provider": stem_provider,
                "progress_cb": progress, "cancel_cb": checkpoint, "log": self.log,
            }
            if prepared_source is not None:
                export_options["prepared_source"] = prepared_source
            result = self.exporter.export_minus_mix(
                source, output_dir, selected, **export_options,
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
            blocked = _service_block(exc)
            with self.lock:
                canceled = event.is_set()
                status = "canceled" if canceled else "blocked" if blocked else "failed"
                failure = blocked or exc
                self.jobs[job_id].update({
                    "status": status, "stage": status,
                    "progress": (0.0 if canceled else self.jobs[job_id]["progress"]
                                 if blocked else 1.0),
                    "detail": "Canceled" if canceled else (str(failure)[:500] or type(failure).__name__),
                    "completed_at": _now(),
                })
                if blocked and not canceled:
                    self.jobs[job_id]["state"] = str(getattr(blocked, "state", "unavailable"))
            if not canceled and not blocked:
                self.log.exception("minus_mix: single export failed")
        finally:
            with self.lock:
                if self.active_id == job_id:
                    self.active_id = None
                self.cancel_events.pop(job_id, None)
