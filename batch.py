"""Recursive, resumable-status batch orchestration for MinusMix exports.

The queue is intentionally sequential.  Stem separation is normally GPU-bound,
and concurrent BS-RoFormer jobs make a 4 GB card slower and less reliable.  A
batch-scoped temporary cache still avoids repeating separation when two packs
contain byte-identical full mixes; that cache is deleted when the batch ends.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ARCHIVE_EXTENSIONS = {".feedpak", ".sloppak"}
MAX_BATCH_FILES = 10_000
ACTIVE_STATUSES = {"queued", "running", "canceling"}
TERMINAL_ITEM_STATUSES = {"done", "skipped", "failed", "canceled"}


class BatchError(RuntimeError):
    """Expected, user-actionable batch request failure."""


class BatchCanceled(RuntimeError):
    """Internal cancellation checkpoint."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _selected_stems(values) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise BatchError("choose at least one instrument stem")
    selected: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise BatchError("excluded stems must be instrument ids")
        stem = value.strip().lower()
        if stem and stem != "full" and stem not in selected:
            selected.append(stem)
    if not selected:
        raise BatchError("choose at least one instrument stem")
    return tuple(selected)


def _root(value, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise BatchError(f"choose a {label} folder")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise BatchError(f"the {label} folder does not exist")
    return path.resolve()


def _candidate_files(input_root: Path, output_root: Path, recursive: bool):
    """Yield archive files without following links or re-scanning output."""
    if not recursive:
        for path in sorted(input_root.iterdir(), key=lambda p: p.name.casefold()):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in ARCHIVE_EXTENSIONS:
                yield path
        return

    output_within_input = _inside(output_root, input_root)
    for current, dirnames, filenames in os.walk(input_root, followlinks=False):
        current_path = Path(current).resolve()
        kept_dirs: list[str] = []
        for name in dirnames:
            child = current_path / name
            if child.is_symlink():
                continue
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if not _inside(resolved, input_root):
                continue
            if output_within_input and _inside(resolved, output_root):
                continue
            kept_dirs.append(name)
        dirnames[:] = sorted(kept_dirs, key=str.casefold)
        for name in sorted(filenames, key=str.casefold):
            path = current_path / name
            if path.suffix.lower() not in ARCHIVE_EXTENSIONS or path.is_symlink():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if (resolved.is_file() and _inside(resolved, input_root)
                    and not (output_within_input and _inside(resolved, output_root))):
                yield resolved


def _looks_derived(info, source: Path, exporter, selected: tuple[str, ...]) -> bool:
    if getattr(info, "derived_exclusions", ()):
        return True
    marker = "(No " + " + ".join(exporter.stem_label(stem) for stem in selected) + ")"
    return str(info.title).casefold().endswith(marker.casefold()) or source.stem.casefold().endswith(marker.casefold())


def scan_sources(exporter, input_dir: str, output_dir: str, excluded_stems,
                 *, recursive: bool = True, skip_existing: bool = True,
                 skip_derived: bool = True) -> dict:
    """Inspect a folder tree and return an authoritative conversion preview."""
    input_root = _root(input_dir, "source")
    output_root = _root(output_dir, "output")
    if input_root == output_root:
        raise BatchError("source and output folders must be different")
    selected = _selected_stems(excluded_stems)

    files: list[Path] = []
    truncated = False
    for path in _candidate_files(input_root, output_root, bool(recursive)):
        if len(files) >= MAX_BATCH_FILES:
            truncated = True
            break
        files.append(path)
    files.sort(key=lambda p: p.relative_to(input_root).as_posix().casefold())

    items: list[dict] = []
    targets: set[str] = set()
    counts = {
        "found": len(files), "ready": 0, "needs_separation": 0,
        "uses_saved_stems": 0, "skipped_existing": 0,
        "skipped_derived": 0, "invalid": 0, "duplicate_target": 0,
    }
    for source in files:
        relative = source.relative_to(input_root)
        item = {
            "relative_path": relative.as_posix(),
            "title": source.stem,
            "artist": "",
            "saved_stems": [],
            "needs_separation": False,
            "output_relative": None,
            "scan_status": "ready",
            "reason": None,
        }
        try:
            info = exporter.inspect_source(source)
            item["title"] = info.title
            item["artist"] = info.artist
            saved = {stem.id for stem in info.stems if stem.id != "full"}
            item["saved_stems"] = sorted(saved)
            missing = [stem for stem in selected if stem not in saved]
            item["needs_separation"] = bool(missing)
            destination_dir = output_root / relative.parent
            desired = exporter.desired_output_path(destination_dir, source, selected)
            item["output_relative"] = desired.relative_to(output_root).as_posix()
            target_key = str(desired).casefold()

            if skip_derived and _looks_derived(info, source, exporter, selected):
                item["scan_status"] = "skipped"
                item["reason"] = "already a derived MinusMix song"
                counts["skipped_derived"] += 1
            elif skip_existing and desired.exists():
                item["scan_status"] = "skipped"
                item["reason"] = "output already exists"
                counts["skipped_existing"] += 1
            elif target_key in targets:
                item["scan_status"] = "skipped"
                item["reason"] = "another source maps to the same output name"
                counts["duplicate_target"] += 1
            else:
                targets.add(target_key)
                counts["ready"] += 1
                if missing:
                    counts["needs_separation"] += 1
                else:
                    counts["uses_saved_stems"] += 1
        except Exception as exc:
            item["scan_status"] = "invalid"
            item["reason"] = str(exc)[:500] or "feedpak could not be inspected"
            counts["invalid"] += 1
        items.append(item)

    return {
        "input_dir": str(input_root),
        "output_dir": str(output_root),
        "excluded_stems": list(selected),
        "recursive": bool(recursive),
        "skip_existing": bool(skip_existing),
        "skip_derived": bool(skip_derived),
        "preserves_relative_folders": True,
        "truncated": truncated,
        "limit": MAX_BATCH_FILES,
        "counts": counts,
        "items": items,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class BatchManager:
    """One persisted, sequential conversion queue per app process."""

    def __init__(self, exporter, separator, config_dir: Path, log):
        self.exporter = exporter
        self.separator = separator
        self.log = log
        self.state_file = Path(config_dir) / "minus_mix_batch_jobs.json"
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.active_id: str | None = None
        self.starting = False
        self._last_persist = 0.0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            return
        for raw in jobs[-5:]:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                continue
            job = raw
            if job.get("status") in ACTIVE_STATUSES:
                job["status"] = "interrupted"
                job["completed_at"] = _now()
                job["detail"] = "The app closed before this batch finished. Scan again to resume safely."
                for item in job.get("items") or []:
                    if item.get("status") not in TERMINAL_ITEM_STATUSES:
                        item["status"] = "canceled"
                        item["detail"] = "Interrupted by app restart"
            self.jobs[job["id"]] = job

    def _persist_locked(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_persist < 2.0:
            return
        self._last_persist = now
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            ordered = sorted(self.jobs.values(), key=lambda job: job.get("created_at", ""))[-5:]
            payload = json.dumps({"version": 1, "jobs": ordered}, ensure_ascii=False, indent=2)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.state_file)
        except OSError as exc:
            # Status persistence is useful for navigation/restart recovery, but
            # losing it must never fail an otherwise valid audio export.
            self.log.warning("minus_mix: could not persist batch status: %s", exc)

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
            return self.starting or bool(
                self.active_id
                and self.jobs.get(self.active_id, {}).get("status") in ACTIVE_STATUSES
            )

    def scan(self, **options) -> dict:
        return scan_sources(self.exporter, **options)

    def start(self, **options) -> dict:
        with self.lock:
            if self.starting or (
                self.active_id
                and self.jobs.get(self.active_id, {}).get("status") in ACTIVE_STATUSES
            ):
                raise BatchError("another MinusMix batch is already running")
            # Reserve the start before the potentially long authoritative scan.
            # Without this flag two simultaneous POSTs could both pass the
            # active-id check and create independent GPU queues.
            self.starting = True

        try:
            scan = self.scan(**options)
            if scan["truncated"]:
                raise BatchError(
                    f"the folder contains more than {scan['limit']} feedpaks; choose a smaller source folder"
                )
            if scan["counts"]["ready"] <= 0:
                raise BatchError("the scan found no new feedpaks to convert")
            if scan["counts"]["needs_separation"]:
                status = self.separator.status()
                if not status.get("ready"):
                    reason = status.get("reason") or "Stem Splitter server is unavailable"
                    raise BatchError(f"start the Stem Splitter server before this batch: {reason}")
        except Exception:
            with self.lock:
                self.starting = False
            raise

        job_id = uuid.uuid4().hex
        items: list[dict] = []
        for scanned in scan["items"]:
            item = copy.deepcopy(scanned)
            scan_status = item.pop("scan_status")
            if scan_status == "ready":
                item.update({"status": "queued", "stage": "queued", "progress": 0.0, "detail": "Waiting"})
            elif scan_status == "invalid":
                item.update({"status": "failed", "stage": "invalid", "progress": 0.0,
                             "detail": item.get("reason") or "Invalid feedpak"})
            else:
                item.update({"status": "skipped", "stage": "skipped", "progress": 1.0,
                             "detail": item.get("reason") or "Skipped"})
            items.append(item)

        total = len(items)
        job = {
            "id": job_id,
            "status": "queued",
            "created_at": _now(),
            "started_at": None,
            "completed_at": None,
            "input_dir": scan["input_dir"],
            "output_dir": scan["output_dir"],
            "excluded_stems": scan["excluded_stems"],
            "recursive": scan["recursive"],
            "skip_existing": scan["skip_existing"],
            "skip_derived": scan["skip_derived"],
            "current_relative_path": None,
            "current_item_number": None,
            "detail": "Queued",
            "overall_progress": 0.0,
            "counts": {
                "total": total,
                "queued": sum(item["status"] == "queued" for item in items),
                "done": 0,
                "skipped": sum(item["status"] == "skipped" for item in items),
                "failed": sum(item["status"] == "failed" for item in items),
                "canceled": 0,
                "temporary_separations": 0,
                "duplicate_audio_reused": 0,
            },
            "cancel_requested": False,
            "items": items,
        }
        event = threading.Event()
        with self.lock:
            self.jobs[job_id] = job
            self.cancel_events[job_id] = event
            self.active_id = job_id
            self.starting = False
            self._persist_locked(force=True)
        thread = threading.Thread(
            target=self._run, args=(job_id,), name=f"minus-mix-batch-{job_id[:8]}", daemon=True,
        )
        try:
            thread.start()
        except Exception:
            with self.lock:
                job["status"] = "failed"
                job["detail"] = "The batch worker could not be started"
                job["completed_at"] = _now()
                self.cancel_events.pop(job_id, None)
                if self.active_id == job_id:
                    self.active_id = None
                self._persist_locked(force=True)
            raise
        return self.get(job_id)

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise BatchError("batch job not found")
            if job.get("status") not in ACTIVE_STATUSES:
                return self._snapshot_locked(job)
            job["cancel_requested"] = True
            job["status"] = "canceling"
            job["detail"] = "Cancel requested; stopping at a safe checkpoint"
            event = self.cancel_events.get(job_id)
            if event:
                event.set()
            self._persist_locked(force=True)
            return self._snapshot_locked(job)

    def _update_item(self, job_id: str, index: int, *, stage: str,
                     progress: float, detail: str, force: bool = False) -> None:
        with self.lock:
            job = self.jobs[job_id]
            item = job["items"][index]
            item["stage"] = stage
            item["progress"] = max(0.0, min(1.0, float(progress)))
            item["detail"] = detail[:500]
            units = sum(
                1.0 if entry.get("status") in TERMINAL_ITEM_STATUSES
                else float(entry.get("progress") or 0.0)
                for entry in job["items"]
            )
            job["overall_progress"] = units / max(1, len(job["items"]))
            job["detail"] = detail[:500]
            self._persist_locked(force=force)

    def _run(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job["status"] = "running"
            job["started_at"] = _now()
            job["detail"] = "Starting batch"
            self._persist_locked(force=True)
            input_root = Path(job["input_dir"])
            output_root = Path(job["output_dir"])
            selected = tuple(job["excluded_stems"])
            skip_existing = bool(job["skip_existing"])
            event = self.cancel_events[job_id]

        def checkpoint() -> None:
            if event.is_set():
                raise BatchCanceled("batch canceled")

        try:
            with tempfile.TemporaryDirectory(prefix="feedback_practice_batch_") as batch_temp:
                cache_root = Path(batch_temp) / "separation-cache"
                cached_audio: dict[str, dict[str, Path]] = {}

                for index in range(len(job["items"])):
                    with self.lock:
                        item = self.jobs[job_id]["items"][index]
                        if item["status"] != "queued":
                            continue
                        relative_value = item["relative_path"]
                    if event.is_set():
                        break

                    source = (input_root / Path(*relative_value.split("/"))).resolve()
                    output_dir = (output_root / Path(*Path(relative_value).parent.parts)).resolve()
                    with self.lock:
                        current = self.jobs[job_id]["items"][index]
                        current["status"] = "running"
                        self.jobs[job_id]["current_relative_path"] = relative_value
                        self.jobs[job_id]["current_item_number"] = index + 1
                        self.jobs[job_id]["counts"]["queued"] -= 1
                    self._update_item(
                        job_id, index, stage="validating", progress=0.01,
                        detail=f"Checking {relative_value}", force=True,
                    )

                    try:
                        checkpoint()
                        if not source.is_file() or not _inside(source, input_root):
                            raise BatchError("source moved or is no longer inside the selected folder")
                        if not _inside(output_dir, output_root):
                            raise BatchError("unsafe output subfolder")
                        output_dir.mkdir(parents=True, exist_ok=True)
                        desired = self.exporter.desired_output_path(output_dir, source, selected)
                        if skip_existing and desired.exists():
                            with self.lock:
                                current = self.jobs[job_id]["items"][index]
                                current.update({
                                    "status": "skipped", "stage": "skipped", "progress": 1.0,
                                    "detail": "Output already exists", "reason": "output already exists",
                                })
                                self.jobs[job_id]["counts"]["skipped"] += 1
                            self._update_item(job_id, index, stage="skipped", progress=1.0,
                                              detail="Output already exists", force=True)
                            continue

                        def progress(stage: str, fraction: float, detail: str) -> None:
                            checkpoint()
                            self._update_item(job_id, index, stage=stage,
                                              progress=fraction, detail=detail)

                        def separate_missing(mix: Path, work: Path,
                                             stems: tuple[str, ...]) -> dict[str, Path]:
                            checkpoint()
                            digest = _sha256(mix)
                            cached = cached_audio.setdefault(digest, {})
                            produced: dict[str, Path] = {}
                            absent: list[str] = []
                            for stem in stems:
                                cached_path = cached.get(stem)
                                if cached_path and cached_path.is_file():
                                    target = work / f"cached_{stem}{cached_path.suffix}"
                                    shutil.copyfile(cached_path, target)
                                    produced[stem] = target
                                else:
                                    absent.append(stem)
                            if not absent:
                                with self.lock:
                                    self.jobs[job_id]["counts"]["duplicate_audio_reused"] += 1
                                progress("separating", 0.74, "Reused identical audio separated earlier in this batch")
                                return produced

                            status = self.separator.status()
                            if not status.get("ready"):
                                raise BatchError(
                                    status.get("reason") or "Stem Splitter server unavailable"
                                )

                            def separation_progress(value, message) -> None:
                                checkpoint()
                                mapped = 0.08 + max(0.0, min(1.0, float(value))) * 0.66
                                progress("separating", mapped, str(message or "Separating audio"))

                            raw = self.separator.separate(
                                mix, work, tuple(absent),
                                progress_cb=separation_progress, cancel_cb=checkpoint,
                            )
                            cache_dir = cache_root / digest
                            cache_dir.mkdir(parents=True, exist_ok=True)
                            for stem, raw_path in raw.items():
                                path = Path(raw_path)
                                if not path.is_file():
                                    continue
                                produced.setdefault(stem, path)
                                # Cache only stems this export requested. Some
                                # local six-source engines return every model
                                # output even when one instrument was requested;
                                # retaining those would defeat the batch's
                                # bounded temporary-disk design.
                                if stem in stems:
                                    cache_path = cache_dir / f"{stem}{path.suffix or '.audio'}"
                                    shutil.copyfile(path, cache_path)
                                    cached.setdefault(stem, cache_path)
                            return produced

                        result = self.exporter.export_minus_mix(
                            source, output_dir, selected,
                            separate_missing=separate_missing,
                            progress_cb=progress, cancel_cb=checkpoint, log=self.log,
                        )
                        checkpoint()
                        with self.lock:
                            current = self.jobs[job_id]["items"][index]
                            current.update({
                                "status": "done", "stage": "done", "progress": 1.0,
                                "detail": f"Created {result.output_filename}",
                                "output_relative": result.output_path.relative_to(output_root).as_posix(),
                                "temporary_separation_used": result.temporary_separation_used,
                            })
                            counts = self.jobs[job_id]["counts"]
                            counts["done"] += 1
                            if result.temporary_separation_used:
                                counts["temporary_separations"] += 1
                        self._update_item(
                            job_id, index, stage="done", progress=1.0,
                            detail=f"Created {result.output_filename}", force=True,
                        )
                    except Exception as exc:
                        if event.is_set():
                            with self.lock:
                                current = self.jobs[job_id]["items"][index]
                                current.update({
                                    "status": "canceled", "stage": "canceled", "progress": 0.0,
                                    "detail": "Canceled", "reason": "canceled",
                                })
                                self.jobs[job_id]["counts"]["canceled"] += 1
                            self._update_item(job_id, index, stage="canceled", progress=0.0,
                                              detail="Canceled", force=True)
                            break
                        message = str(exc)[:500] or type(exc).__name__
                        self.log.exception("minus_mix: batch item failed: %s", relative_value)
                        with self.lock:
                            current = self.jobs[job_id]["items"][index]
                            current.update({
                                "status": "failed", "stage": "failed", "progress": 1.0,
                                "detail": message, "reason": message,
                            })
                            self.jobs[job_id]["counts"]["failed"] += 1
                        self._update_item(job_id, index, stage="failed", progress=1.0,
                                          detail=message, force=True)

                with self.lock:
                    current_job = self.jobs[job_id]
                    if event.is_set():
                        for item in current_job["items"]:
                            if item["status"] == "queued":
                                item.update({"status": "canceled", "stage": "canceled",
                                             "progress": 0.0, "detail": "Canceled before starting"})
                                current_job["counts"]["queued"] -= 1
                                current_job["counts"]["canceled"] += 1
                        current_job["status"] = "canceled"
                        current_job["detail"] = "Batch canceled"
                    else:
                        current_job["status"] = "completed"
                        failed = current_job["counts"]["failed"]
                        current_job["detail"] = (
                            f"Batch completed with {failed} failure{'s' if failed != 1 else ''}"
                            if failed else "Batch completed"
                        )
                    current_job["overall_progress"] = 1.0
                    current_job["current_relative_path"] = None
                    current_job["current_item_number"] = None
                    current_job["completed_at"] = _now()
                    self._persist_locked(force=True)
        except Exception as exc:
            self.log.exception("minus_mix: batch worker failed")
            with self.lock:
                current_job = self.jobs[job_id]
                current_job["status"] = "failed"
                current_job["detail"] = str(exc)[:500] or "Batch worker failed"
                current_job["completed_at"] = _now()
                self._persist_locked(force=True)
        finally:
            with self.lock:
                if self.active_id == job_id:
                    self.active_id = None
                self.cancel_events.pop(job_id, None)
                self._persist_locked(force=True)
