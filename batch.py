"""Recursive, resumable-status batch orchestration for MinusMix exports.

The queue is intentionally sequential.  Stem separation is normally GPU-bound,
and concurrent BS-RoFormer jobs make a 4 GB card slower and less reliable.  A
batch-scoped temporary cache still avoids repeating separation when two packs
contain byte-identical full mixes; that cache is deleted when the batch ends.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

ARCHIVE_EXTENSIONS = {".feedpak", ".sloppak"}
MAX_BATCH_FILES = 10_000
MAX_PERSISTED_ITEMS = 400
MAX_RETAINED_JOBS = 5
MAX_CACHED_SCANS = 2
ACTIVE_STATUSES = {"queued", "running", "canceling"}
TERMINAL_ITEM_STATUSES = {"done", "skipped", "failed", "canceled"}


class BatchCounts(TypedDict, total=False):
    total: int
    queued: int
    done: int
    skipped: int
    failed: int
    canceled: int
    temporary_separations: int
    duplicate_audio_reused: int
    preview_failures: int


class BatchItem(TypedDict, total=False):
    relative_path: str
    output_relative: str | None
    status: str
    stage: str
    progress: float
    detail: str
    reason: str | None
    preview_created: bool
    temporary_separation_used: bool


class BatchJob(TypedDict, total=False):
    id: str
    status: str
    created_at: str
    completed_at: str | None
    detail: str
    overall_progress: float
    counts: BatchCounts
    items: list[BatchItem]
    items_total: int
    items_truncated: bool


class BatchError(RuntimeError):
    """Expected, user-actionable batch request failure."""


class BatchCanceled(RuntimeError):
    """Internal cancellation checkpoint."""


class ScanCanceled(RuntimeError):
    """Internal folder-scan cancellation checkpoint."""


class BatchCachingStemProvider:
    """Provide server stems while reusing identical audio within one batch."""

    def __init__(self, separator, cache_root: Path,
                 cached_audio: dict[str, dict[str, Path]],
                 checkpoint: Callable[[], None],
                 progress: Callable[[str, float, str], None],
                 cache_hit: Callable[[], None]):
        self.separator = separator
        self.cache_root = cache_root
        self.cached_audio = cached_audio
        self.checkpoint = checkpoint
        self.progress = progress
        self.cache_hit = cache_hit

    def obtain(self, mix: Path, work: Path, stems: tuple[str, ...],
               full_digest: str | None) -> dict[str, Path]:
        self.checkpoint()
        if not full_digest:
            raise BatchError("the source audio digest is unavailable")
        cached = self.cached_audio.setdefault(full_digest, {})
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
            self.cache_hit()
            self.progress(
                "separating", 0.74,
                "Reused identical audio separated earlier in this batch",
            )
            return produced

        def separation_progress(value, message) -> None:
            self.checkpoint()
            mapped = 0.08 + max(0.0, min(1.0, float(value))) * 0.66
            self.progress("separating", mapped, str(message or "Separating audio"))

        raw = self.separator.separate(
            mix, work, tuple(absent),
            progress_cb=separation_progress, cancel_cb=self.checkpoint,
        )
        cache_dir = self.cache_root / full_digest
        cache_dir.mkdir(parents=True, exist_ok=True)
        for stem, raw_path in raw.items():
            path = Path(raw_path)
            if not path.is_file():
                continue
            produced.setdefault(stem, path)
            # Cache only requested outputs; some six-source servers return all.
            if stem in stems:
                cache_path = cache_dir / f"{stem}{path.suffix or '.audio'}"
                shutil.copyfile(path, cache_path)
                cached.setdefault(stem, cache_path)
        return produced


@dataclass(frozen=True)
class BatchRunContext:
    job_id: str
    input_root: Path
    output_root: Path
    selected: tuple[str, ...]
    skip_existing: bool
    event: threading.Event
    cache_root: Path
    cached_audio: dict[str, dict[str, Path]]


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


def _destination_dir(output_root: Path, relative: Path, preserve_structure: bool) -> Path:
    return output_root / relative.parent if preserve_structure else output_root


def _numbered_output(desired: Path, number: int) -> Path:
    if number <= 1:
        return desired
    return desired.with_name(f"{desired.stem} ({number}){desired.suffix}")


def _reserve_output(desired: Path, reserved: set[str], *, avoid_existing: bool) -> tuple[Path, bool]:
    """Reserve a deterministic collision-safe output name for the preview.

    With skip-existing enabled, numbering is based only on source order so a
    later scan maps the same source rows back to the same existing outputs.
    Without it, existing files are avoided because the exporter never
    overwrites and will choose the same next available name while the queue runs.
    """
    for number in range(1, 10_000):
        candidate = _numbered_output(desired, number)
        key = str(candidate).casefold()
        if key in reserved or (avoid_existing and candidate.exists()):
            continue
        reserved.add(key)
        return candidate, number > 1
    raise BatchError("could not find an unused output filename")


def scan_sources(exporter, input_dir: str, output_dir: str, excluded_stems,
                  *, recursive: bool = True, skip_existing: bool = True,
                  skip_derived: bool = True, preserve_structure: bool = True,
                  progress_cb=None, cancel_cb=None) -> dict:
    """Inspect a folder tree and return an authoritative conversion preview."""
    def checkpoint() -> None:
        if cancel_cb:
            cancel_cb()

    def report(value: float, detail: str) -> None:
        if progress_cb:
            progress_cb(max(0.0, min(1.0, value)), detail)

    checkpoint()
    report(0.0, "Finding FeedPaks")
    input_root = _root(input_dir, "source")
    output_root = _root(output_dir, "output")
    if input_root == output_root:
        raise BatchError("source and output folders must be different")
    selected = _selected_stems(excluded_stems)

    files: list[Path] = []
    truncated = False
    for path in _candidate_files(input_root, output_root, bool(recursive)):
        checkpoint()
        if len(files) >= MAX_BATCH_FILES:
            truncated = True
            break
        files.append(path)
    files.sort(key=lambda p: p.relative_to(input_root).as_posix().casefold())
    checkpoint()
    report(0.05, f"Found {len(files)} FeedPaks; reading manifests")

    items: list[dict] = []
    targets: set[str] = set()
    counts = {
        "found": len(files), "ready": 0, "needs_separation": 0,
        "uses_saved_stems": 0, "skipped_existing": 0,
        "skipped_derived": 0, "invalid": 0, "duplicate_target": 0,
        "renamed_collisions": 0,
    }
    for index, source in enumerate(files):
        checkpoint()
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
            if skip_derived and _looks_derived(info, source, exporter, selected):
                item["scan_status"] = "skipped"
                item["reason"] = "already a derived MinusMix song"
                counts["skipped_derived"] += 1
            else:
                destination_dir = _destination_dir(
                    output_root, relative, bool(preserve_structure),
                )
                first_choice = exporter.desired_output_path(destination_dir, source, selected)
                desired, renamed = _reserve_output(
                    first_choice, targets, avoid_existing=not bool(skip_existing),
                )
                item["output_relative"] = desired.relative_to(output_root).as_posix()
                item["output_name_collision"] = renamed
                if renamed:
                    counts["renamed_collisions"] += 1

                if skip_existing and desired.exists():
                    item["scan_status"] = "skipped"
                    item["reason"] = "output already exists"
                    counts["skipped_existing"] += 1
                else:
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
        report(
            0.05 + (0.95 * (index + 1) / max(1, len(files))),
            f"Checked {index + 1} of {len(files)}: {relative.as_posix()}",
        )

    return {
        "input_dir": str(input_root),
        "output_dir": str(output_root),
        "excluded_stems": list(selected),
        "recursive": bool(recursive),
        "skip_existing": bool(skip_existing),
        "skip_derived": bool(skip_derived),
        "preserve_structure": bool(preserve_structure),
        "preserves_relative_folders": bool(preserve_structure),
        "truncated": truncated,
        "limit": MAX_BATCH_FILES,
        "counts": counts,
        "items": items,
    }


class BatchManager:
    """One persisted, sequential conversion queue per app process."""

    def __init__(self, exporter, separator, config_dir: Path, log):
        self.exporter = exporter
        self.separator = separator
        self.log = log
        self.state_file = Path(config_dir) / "minus_mix_batch_jobs.json"
        self.lock = threading.RLock()
        self.persist_lock = threading.Lock()
        self.jobs: dict[str, BatchJob] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.scan_cache: dict[str, dict] = {}
        self.scan_jobs: dict[str, dict] = {}
        self.scan_events: dict[str, threading.Event] = {}
        self.active_scan_id: str | None = None
        self.active_id: str | None = None
        self.starting = False
        self._last_persist = 0.0
        self._persist_revision = 0
        self._persist_written_revision = 0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            return
        for raw in jobs[-MAX_RETAINED_JOBS:]:
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

    def _prepare_persist_locked(self, force: bool = False) -> tuple[int, list[dict]] | None:
        now = time.monotonic()
        if not force and now - self._last_persist < 2.0:
            return None
        self._last_persist = now
        ordered = sorted(
            self.jobs.values(), key=lambda job: job.get("created_at", "")
        )[-MAX_RETAINED_JOBS:]
        persisted = [
            self._snapshot_locked(
                job,
                item_limit=(
                    MAX_PERSISTED_ITEMS
                    if job.get("status") in ACTIVE_STATUSES else 0
                ),
            )
            for job in ordered
        ]
        self._persist_revision += 1
        return self._persist_revision, persisted

    def _persist(self, force: bool = False) -> None:
        """Snapshot briefly under the manager lock, then encode/write outside it."""
        with self.lock:
            prepared = self._prepare_persist_locked(force=force)
        if prepared is None:
            return
        revision, persisted = prepared
        with self.persist_lock:
            if revision <= self._persist_written_revision:
                return
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                payload = json.dumps(
                    {"version": 2, "jobs": persisted}, ensure_ascii=False, indent=2,
                )
                tmp = self.state_file.with_suffix(".tmp")
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, self.state_file)
                self._persist_written_revision = revision
            except OSError as exc:
                # Status persistence is useful for navigation/restart recovery,
                # but losing it must never fail an otherwise valid audio export.
                self.log.warning("minus_mix: could not persist batch status: %s", exc)

    @staticmethod
    def _selected_item_indexes(items: list[dict], limit: int) -> list[int]:
        """Choose a bounded, source-ordered view that keeps actionable rows."""
        if limit <= 0:
            return []
        if len(items) <= limit:
            return list(range(len(items)))

        selected: set[int] = set()
        for index, item in enumerate(items):
            if len(selected) >= limit:
                break
            if item.get("status") == "running":
                selected.add(index)
        for index in range(len(items) - 1, -1, -1):
            if len(selected) >= limit:
                break
            if items[index].get("status") == "failed":
                selected.add(index)
        for index in range(len(items) - 1, -1, -1):
            if len(selected) >= limit:
                break
            selected.add(index)
        return sorted(selected)

    def _compact_terminal_job_locked(self, job: dict) -> None:
        """Release large terminal queues while retaining useful recent history."""
        items = job.get("items") or []
        stored_total = job.get("items_total")
        total = stored_total if isinstance(stored_total, int) else len(items)
        indexes = self._selected_item_indexes(items, MAX_PERSISTED_ITEMS)
        job["items"] = [items[index] for index in indexes]
        job["items_total"] = total
        job["items_truncated"] = bool(job.get("items_truncated")) or total > len(indexes)
        job.pop("_progress_values", None)
        job.pop("_progress_units", None)

    def _prune_jobs_locked(self) -> None:
        """Keep active work plus the newest bounded terminal summaries."""
        while len(self.jobs) > MAX_RETAINED_JOBS:
            candidates = sorted(
                (
                    job for job in self.jobs.values()
                    if job.get("status") not in ACTIVE_STATUSES
                    and job.get("id") != self.active_id
                ),
                key=lambda job: job.get("created_at", ""),
            )
            if not candidates:
                return
            self.jobs.pop(candidates[0]["id"], None)

    def _snapshot_locked(self, job: dict, *, item_limit: int | None = None) -> dict:
        """Copy a job without copying rows the caller will immediately discard.

        Full snapshots remain available to internal callers and tests.  The HTTP
        status routes pass an item limit, which keeps polling cost bounded even
        when a batch contains the supported maximum of 10,000 FeedPaks.
        """
        items = job.get("items") or []
        result = copy.deepcopy({
            key: value for key, value in job.items()
            if key != "items" and not key.startswith("_")
        })
        if item_limit is None:
            result["items"] = copy.deepcopy(items)
            return result

        limit = max(0, int(item_limit))
        stored_total = job.get("items_total")
        total = stored_total if isinstance(stored_total, int) else len(items)
        was_truncated = bool(job.get("items_truncated"))
        result["items_total"] = total
        result["items_truncated"] = was_truncated or total > limit
        if len(items) <= limit:
            result["items"] = copy.deepcopy(items)
            return result

        selected = self._selected_item_indexes(items, limit)
        result["items"] = copy.deepcopy([items[index] for index in selected])
        return result

    def latest(self, *, item_limit: int | None = None) -> dict | None:
        with self.lock:
            if not self.jobs:
                return None
            job = max(self.jobs.values(), key=lambda value: value.get("created_at", ""))
            return self._snapshot_locked(job, item_limit=item_limit)

    def get(self, job_id: str, *, item_limit: int | None = None) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return self._snapshot_locked(job, item_limit=item_limit) if job else None

    def is_active(self) -> bool:
        with self.lock:
            return self.starting or bool(
                self.active_id
                and self.jobs.get(self.active_id, {}).get("status") in ACTIVE_STATUSES
            )

    @staticmethod
    def _normalized_scan_options(options: dict) -> dict:
        return {
            "input_dir": str(_root(options.get("input_dir"), "source")),
            "output_dir": str(_root(options.get("output_dir"), "output")),
            "excluded_stems": list(_selected_stems(options.get("excluded_stems"))),
            "recursive": bool(options.get("recursive", True)),
            "skip_existing": bool(options.get("skip_existing", True)),
            "skip_derived": bool(options.get("skip_derived", True)),
            "preserve_structure": bool(options.get("preserve_structure", True)),
        }

    @staticmethod
    def _scan_signature(scan: dict) -> tuple | None:
        if scan.get("truncated"):
            return None
        input_root = Path(scan["input_dir"])
        output_root = Path(scan["output_dir"])
        rows: list[tuple] = []
        try:
            for path in _candidate_files(
                    input_root, output_root, bool(scan.get("recursive", True))):
                if len(rows) >= MAX_BATCH_FILES:
                    return None
                stat = path.stat()
                rows.append((
                    path.relative_to(input_root).as_posix(),
                    stat.st_size,
                    stat.st_mtime_ns,
                ))
        except OSError:
            return None
        rows.sort(key=lambda row: row[0].casefold())
        outputs = tuple(sorted(
            (
                str(item.get("output_relative") or ""),
                bool(item.get("output_relative") and (
                    output_root / Path(*str(item["output_relative"]).split("/"))
                ).exists()),
            )
            for item in (scan.get("items") or [])
            if item.get("output_relative")
        ))
        return tuple(rows), outputs

    def _reuse_scan(self, scan_id: str | None, options: dict) -> dict | None:
        if not isinstance(scan_id, str) or not scan_id:
            return None
        with self.lock:
            record = self.scan_cache.get(scan_id)
            if not record:
                return None
            cached_options = record["options"]
            cached_scan = copy.deepcopy(record["scan"])
            cached_signature = record["signature"]
        if self._normalized_scan_options(options) != cached_options:
            return None
        if self._scan_signature(cached_scan) != cached_signature:
            with self.lock:
                self.scan_cache.pop(scan_id, None)
            return None
        return cached_scan

    def scan(self, *, progress_cb=None, cancel_cb=None, **options) -> dict:
        result = scan_sources(
            self.exporter, progress_cb=progress_cb, cancel_cb=cancel_cb, **options,
        )
        scan_id = uuid.uuid4().hex
        result["scan_id"] = scan_id
        record = {
            "options": self._normalized_scan_options(options),
            "signature": self._scan_signature(result),
            # Scan results are immutable after publication. Sharing this object
            # with the short job history avoids retaining a duplicate 10k-row list.
            "scan": result,
        }
        with self.lock:
            self.scan_cache[scan_id] = record
            while len(self.scan_cache) > MAX_CACHED_SCANS:
                self.scan_cache.pop(next(iter(self.scan_cache)))
        return result

    def start_scan(self, **options) -> dict:
        """Start one observable, cancelable source scan in a background thread."""
        normalized = self._normalized_scan_options(options)
        with self.lock:
            if self.active_scan_id:
                active = self.scan_jobs.get(self.active_scan_id)
                if active and active.get("status") in ACTIVE_STATUSES:
                    raise BatchError("another MinusMix folder scan is already running")
            scan_job_id = uuid.uuid4().hex
            event = threading.Event()
            job = {
                "id": scan_job_id, "status": "queued", "progress": 0.0,
                "detail": "Waiting to scan", "created_at": _now(),
                "completed_at": None, "result": None,
            }
            self.scan_jobs[scan_job_id] = job
            self.scan_events[scan_job_id] = event
            self.active_scan_id = scan_job_id
            while len(self.scan_jobs) > 3:
                removable = [
                    entry for entry in self.scan_jobs.values()
                    if entry["id"] != self.active_scan_id
                ]
                if not removable:
                    break
                oldest = min(removable, key=lambda entry: entry.get("created_at", ""))
                self.scan_jobs.pop(oldest["id"], None)

        thread = threading.Thread(
            target=self._run_scan, args=(scan_job_id, normalized),
            name=f"minus-mix-scan-{scan_job_id[:8]}", daemon=True,
        )
        try:
            thread.start()
        except Exception:
            with self.lock:
                job.update({
                    "status": "failed", "detail": "The scan worker could not be started",
                    "completed_at": _now(),
                })
                self.active_scan_id = None
                self.scan_events.pop(scan_job_id, None)
            raise
        return self.get_scan(scan_job_id)

    def _run_scan(self, scan_job_id: str, options: dict) -> None:
        with self.lock:
            job = self.scan_jobs[scan_job_id]
            event = self.scan_events[scan_job_id]
            job.update({"status": "running", "detail": "Finding FeedPaks"})

        def checkpoint() -> None:
            if event.is_set():
                raise ScanCanceled("folder scan canceled")

        def progress(value: float, detail: str) -> None:
            checkpoint()
            with self.lock:
                current = self.scan_jobs[scan_job_id]
                current["progress"] = max(0.0, min(1.0, float(value)))
                current["detail"] = str(detail)[:500]

        try:
            result = self.scan(
                progress_cb=progress, cancel_cb=checkpoint, **options,
            )
            checkpoint()
            with self.lock:
                job.update({
                    "status": "completed", "progress": 1.0,
                    "detail": f"Scan complete: {result['counts']['found']} FeedPaks found",
                    "completed_at": _now(), "result": result,
                })
        except ScanCanceled:
            with self.lock:
                job.update({
                    "status": "canceled", "detail": "Folder scan canceled",
                    "completed_at": _now(),
                })
        except Exception as exc:
            self.log.exception("minus_mix: folder scan failed")
            with self.lock:
                job.update({
                    "status": "failed", "detail": str(exc)[:500] or "Folder scan failed",
                    "completed_at": _now(),
                })
        finally:
            with self.lock:
                if self.active_scan_id == scan_job_id:
                    self.active_scan_id = None
                self.scan_events.pop(scan_job_id, None)

    @staticmethod
    def _scan_job_snapshot(job: dict, result_item_limit: int | None = None) -> dict:
        result = job.get("result")
        snapshot = copy.deepcopy({key: value for key, value in job.items() if key != "result"})
        if not isinstance(result, dict) or result_item_limit is None:
            snapshot["result"] = copy.deepcopy(result)
            return snapshot
        scan = copy.deepcopy({key: value for key, value in result.items() if key != "items"})
        items = result.get("items") or []
        limit = max(0, int(result_item_limit))
        scan["items_total"] = len(items)
        scan["items_truncated"] = len(items) > limit
        scan["items"] = copy.deepcopy(items[:limit])
        snapshot["result"] = scan
        return snapshot

    def get_scan(self, scan_job_id: str, *, result_item_limit: int | None = None) -> dict | None:
        with self.lock:
            job = self.scan_jobs.get(scan_job_id)
            return self._scan_job_snapshot(job, result_item_limit) if job else None

    def cancel_scan(self, scan_job_id: str) -> dict:
        with self.lock:
            job = self.scan_jobs.get(scan_job_id)
            if not job:
                raise BatchError("folder scan job not found")
            if job.get("status") not in ACTIVE_STATUSES:
                return copy.deepcopy(job)
            job.update({"status": "canceling", "detail": "Cancel requested"})
            event = self.scan_events.get(scan_job_id)
            if event:
                event.set()
            return copy.deepcopy(job)

    def start(self, *, scan_id: str | None = None,
              snapshot_item_limit: int | None = None, **options) -> dict:
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
            scan = self._reuse_scan(scan_id, options)
            scan_reused = scan is not None
            if scan is None:
                scan = self.scan(**options)
            if scan["truncated"]:
                raise BatchError(
                    f"the folder contains more than {scan['limit']} feedpaks; choose a smaller source folder"
                )
            if scan["counts"]["ready"] <= 0:
                raise BatchError("the scan found no new feedpaks to convert")
            try:
                self.exporter.validate_output_directory(Path(scan["output_dir"]))
            except self.exporter.ExportError as exc:
                raise BatchError(str(exc)) from exc
            if scan["counts"]["needs_separation"]:
                status = self.separator.status()
                if not status.get("ready"):
                    reason = status.get("reason") or (
                        "Stem Splitter's managed local server is unavailable"
                    )
                    raise BatchError(
                        "start Stem Splitter's managed local server before this batch: "
                        f"{reason}"
                    )
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
            "preserve_structure": scan["preserve_structure"],
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
                "preview_failures": 0,
            },
            "cancel_requested": False,
            "scan_reused": scan_reused,
            "items": items,
            "_progress_units": sum(
                1.0 if item["status"] in TERMINAL_ITEM_STATUSES
                else float(item.get("progress") or 0.0)
                for item in items
            ),
            "_progress_values": [
                1.0 if item["status"] in TERMINAL_ITEM_STATUSES
                else float(item.get("progress") or 0.0)
                for item in items
            ],
        }
        job["overall_progress"] = job["_progress_units"] / max(1, total)
        event = threading.Event()
        with self.lock:
            self.jobs[job_id] = job
            self.cancel_events[job_id] = event
            self.active_id = job_id
            self.starting = False
            self._prune_jobs_locked()
        self._persist(force=True)
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
                self._compact_terminal_job_locked(job)
                self._prune_jobs_locked()
            self._persist(force=True)
            raise
        return self.get(job_id, item_limit=snapshot_item_limit)

    def cancel(self, job_id: str, *, snapshot_item_limit: int | None = None) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise BatchError("batch job not found")
            if job.get("status") not in ACTIVE_STATUSES:
                return self._snapshot_locked(job, item_limit=snapshot_item_limit)
            job["cancel_requested"] = True
            job["status"] = "canceling"
            job["detail"] = "Cancel requested; stopping at a safe checkpoint"
            event = self.cancel_events.get(job_id)
            if event:
                event.set()
            snapshot = self._snapshot_locked(job, item_limit=snapshot_item_limit)
        self._persist(force=True)
        return snapshot

    def _update_item(self, job_id: str, index: int, *, stage: str,
                     progress: float, detail: str, force: bool = False) -> None:
        with self.lock:
            job = self.jobs[job_id]
            item = job["items"][index]
            item["stage"] = stage
            item["progress"] = max(0.0, min(1.0, float(progress)))
            item["detail"] = detail[:500]
            current = (
                1.0 if item.get("status") in TERMINAL_ITEM_STATUSES
                else item["progress"]
            )
            previous = job["_progress_values"][index]
            job["_progress_values"][index] = current
            job["_progress_units"] += current - previous
            job["overall_progress"] = job["_progress_units"] / max(1, len(job["items"]))
            job["detail"] = detail[:500]
        self._persist(force=force)

    @staticmethod
    def _checkpoint(context: BatchRunContext) -> None:
        if context.event.is_set():
            raise BatchCanceled("batch canceled")

    def _mark_item_running(self, context: BatchRunContext, index: int) -> tuple[str, Path, Path]:
        with self.lock:
            item = self.jobs[context.job_id]["items"][index]
            relative_value = item["relative_path"]
            output_relative = item.get("output_relative")
            item["status"] = "running"
            self.jobs[context.job_id]["current_relative_path"] = relative_value
            self.jobs[context.job_id]["current_item_number"] = index + 1
            self.jobs[context.job_id]["counts"]["queued"] -= 1
        if not isinstance(output_relative, str) or not output_relative:
            raise BatchError("batch item has no planned output path")
        source = (
            context.input_root / Path(*relative_value.split("/"))
        ).resolve()
        planned_output = (
            context.output_root / Path(*output_relative.split("/"))
        ).resolve()
        self._update_item(
            context.job_id, index, stage="validating", progress=0.01,
            detail=f"Checking {relative_value}", force=True,
        )
        return relative_value, source, planned_output

    def _mark_item_canceled(self, context: BatchRunContext, index: int) -> None:
        with self.lock:
            item = self.jobs[context.job_id]["items"][index]
            item.update({
                "status": "canceled", "stage": "canceled", "progress": 0.0,
                "detail": "Canceled", "reason": "canceled",
            })
            self.jobs[context.job_id]["counts"]["canceled"] += 1
        self._update_item(
            context.job_id, index, stage="canceled", progress=0.0,
            detail="Canceled", force=True,
        )

    def _mark_item_failed(self, context: BatchRunContext, index: int,
                          relative_value: str, exc: Exception) -> None:
        message = str(exc)[:500] or type(exc).__name__
        self.log.exception("minus_mix: batch item failed: %s", relative_value)
        with self.lock:
            item = self.jobs[context.job_id]["items"][index]
            item.update({
                "status": "failed", "stage": "failed", "progress": 1.0,
                "detail": message, "reason": message,
            })
            self.jobs[context.job_id]["counts"]["failed"] += 1
        self._update_item(
            context.job_id, index, stage="failed", progress=1.0,
            detail=message, force=True,
        )

    def _process_item(self, context: BatchRunContext, index: int) -> bool:
        """Process one queued row; return False when cancellation stops the queue."""
        relative_value = "unknown source"
        try:
            relative_value, source, planned_output = self._mark_item_running(context, index)
            self._checkpoint(context)
            if not source.is_file() or not _inside(source, context.input_root):
                raise BatchError("source moved or is no longer inside the selected folder")
            if not _inside(planned_output, context.output_root):
                raise BatchError("unsafe output subfolder")
            output_dir = planned_output.parent
            output_dir.mkdir(parents=True, exist_ok=True)
            if context.skip_existing and planned_output.exists():
                with self.lock:
                    item = self.jobs[context.job_id]["items"][index]
                    item.update({
                        "status": "skipped", "stage": "skipped", "progress": 1.0,
                        "detail": "Output already exists", "reason": "output already exists",
                    })
                    self.jobs[context.job_id]["counts"]["skipped"] += 1
                self._update_item(
                    context.job_id, index, stage="skipped", progress=1.0,
                    detail="Output already exists", force=True,
                )
                return True

            def progress(stage: str, fraction: float, detail: str) -> None:
                self._checkpoint(context)
                self._update_item(
                    context.job_id, index, stage=stage,
                    progress=fraction, detail=detail,
                )

            def cache_hit() -> None:
                with self.lock:
                    counts = self.jobs[context.job_id]["counts"]
                    counts["duplicate_audio_reused"] += 1

            provider = BatchCachingStemProvider(
                self.separator, context.cache_root, context.cached_audio,
                lambda: self._checkpoint(context), progress, cache_hit,
            )
            result = self.exporter.export_minus_mix(
                source, output_dir, context.selected,
                stem_provider=provider, progress_cb=progress,
                cancel_cb=lambda: self._checkpoint(context), log=self.log,
            )
            with self.lock:
                item = self.jobs[context.job_id]["items"][index]
                item.update({
                    "status": "done", "stage": "done", "progress": 1.0,
                    "detail": f"Created {result.output_filename}",
                    "output_relative": result.output_path.relative_to(
                        context.output_root,
                    ).as_posix(),
                    "preview_created": result.preview_created,
                    "temporary_separation_used": result.temporary_separation_used,
                })
                counts = self.jobs[context.job_id]["counts"]
                counts["done"] += 1
                if result.temporary_separation_used:
                    counts["temporary_separations"] += 1
                if not result.preview_created:
                    counts["preview_failures"] += 1
            self._update_item(
                context.job_id, index, stage="done", progress=1.0,
                detail=f"Created {result.output_filename}", force=True,
            )
            return True
        except Exception as exc:
            if context.event.is_set():
                self._mark_item_canceled(context, index)
                return False
            self._mark_item_failed(context, index, relative_value, exc)
            return True

    def _finish_run(self, context: BatchRunContext) -> None:
        with self.lock:
            job = self.jobs[context.job_id]
            if context.event.is_set():
                for index, item in enumerate(job["items"]):
                    if item["status"] != "queued":
                        continue
                    item.update({
                        "status": "canceled", "stage": "canceled",
                        "progress": 0.0, "detail": "Canceled before starting",
                    })
                    job["_progress_values"][index] = 1.0
                    job["counts"]["queued"] -= 1
                    job["counts"]["canceled"] += 1
                job["status"] = "canceled"
                job["detail"] = "Batch canceled"
            else:
                job["status"] = "completed"
                failed = job["counts"]["failed"]
                preview_failures = job["counts"]["preview_failures"]
                job["detail"] = (
                    f"Batch completed with {failed} failure{'s' if failed != 1 else ''}"
                    if failed else "Batch completed"
                )
                if preview_failures:
                    job["detail"] += (
                        f"; {preview_failures} optional preview"
                        f"{'s' if preview_failures != 1 else ''} could not be created"
                    )
            job["_progress_units"] = float(len(job["items"]))
            job["overall_progress"] = 1.0
            job["current_relative_path"] = None
            job["current_item_number"] = None
            job["completed_at"] = _now()
            self._compact_terminal_job_locked(job)
            self._prune_jobs_locked()
        self._persist(force=True)

    def _run(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job["status"] = "running"
            job["started_at"] = _now()
            job["detail"] = "Starting batch"
            event = self.cancel_events[job_id]
        self._persist(force=True)

        try:
            with tempfile.TemporaryDirectory(prefix="feedback_practice_batch_") as batch_temp:
                context = BatchRunContext(
                    job_id=job_id,
                    input_root=Path(job["input_dir"]),
                    output_root=Path(job["output_dir"]),
                    selected=tuple(job["excluded_stems"]),
                    skip_existing=bool(job["skip_existing"]),
                    event=event,
                    cache_root=Path(batch_temp) / "separation-cache",
                    cached_audio={},
                )
                for index in range(len(job["items"])):
                    with self.lock:
                        item = self.jobs[job_id]["items"][index]
                        if item["status"] != "queued":
                            continue
                    if event.is_set():
                        break
                    if not self._process_item(context, index):
                        break
                self._finish_run(context)
        except Exception as exc:
            self.log.exception("minus_mix: batch worker failed")
            with self.lock:
                current_job = self.jobs[job_id]
                current_job["status"] = "failed"
                current_job["detail"] = str(exc)[:500] or "Batch worker failed"
                current_job["completed_at"] = _now()
                self._compact_terminal_job_locked(current_job)
                self._prune_jobs_locked()
            self._persist(force=True)
        finally:
            with self.lock:
                if self.active_id == job_id:
                    self.active_id = None
                self.cancel_events.pop(job_id, None)
            self._persist(force=True)
