"""Independent reviewed audio-reuse queue with bounded workers and resumable receipts."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

ACTIVE = {"scanning", "running", "canceling"}
MAX_CHECKPOINT_BYTES = 64 * 1024**2
MAX_RECORD_BYTES = 2 * 1024**2
SNAPSHOT_CACHE_BYTES = 8 * 1024**2
SNAPSHOT_CACHE_ENTRIES = 32
EXISTING_REASON = "Already complete; existing FeedPak verified and left unchanged."


class PreviewLimitError(ValueError):
    """The complete reviewed plan must fit the bounded resume format."""


class ReuseManager:
    def __init__(self, *, match, packing, exporter, support, config_dir, log):
        self.match, self.packing, self.exporter, self.support = match, packing, exporter, support
        self.log = log
        self.state_file = Path(config_dir) / "audio_reuse_job.json"
        self.journal_file = Path(config_dir) / "audio_reuse_receipts.jsonl"
        self.lock, self.persist_lock = threading.RLock(), threading.Lock()
        self.event = threading.Event()
        self.job = None
        self.load_error = None
        self.legacy_checkpoint = False
        self._snapshots = OrderedDict()
        self._snapshot_bytes = 0
        self._plan_bytes = 0
        self._working = False
        self._worker_phase = None
        self._load()

    def _load(self):
        if not self.state_file.is_file():
            return
        try:
            if self.state_file.stat().st_size > MAX_CHECKPOINT_BYTES:
                raise ValueError("Checkpoint exceeds its size limit.")
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("policy") == "minusmix-audio-reuse-1":
                self.legacy_checkpoint = True
                raise ValueError("This preview used the earlier guitar-only rules. Scan again to detect all mix variants.")
            spec = importlib.util.spec_from_file_location("_minusmix_reuse_state", Path(__file__).with_name("reuse_state.py"))
            state = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(state)
            state.validate(data, self.match.POLICY, self.match.MAX_FILES, self.match.MAX_OUTPUT_ROWS)
            self._replay(data)
            for row in data["items"]:
                if row["status"] == "done" and not self._receipt_valid(data, row, row.get("receipt")):
                    row.update(status="ready" if row.get("donor_relative") else "blocked",
                               reason="Saved completion receipt is invalid; the output must be verified on resume.")
                    row.pop("receipt", None)
            if data.get("status") in ACTIVE:
                data["status"] = "interrupted"
                data["detail"] = "Interrupted. Review and resume; completed outputs will be verified."
                for row in data["items"]:
                    if row["status"] == "running":
                        row["status"] = "ready"
            self.job = data
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.load_error = str(exc)
            self.log.warning("minus_mix: audio reuse checkpoint unavailable: %s", exc)

    def _persist(self):
        # Serialize writes and snapshot only after acquiring this lock so an
        # older parallel worker snapshot can never replace newer receipts.
        with self.persist_lock:
            with self.lock:
                payload = json.dumps(self.job, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(payload) > MAX_CHECKPOINT_BYTES:
                raise PreviewLimitError("The saved job exceeds its size limit; use smaller input folders.")
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_file.with_suffix(".tmp")
            with temp.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.state_file)
            # The complete checkpoint now includes every preceding journaled
            # state. Holding persist_lock prevents an append during compaction.
            with self.journal_file.open("wb") as stream:
                stream.flush()
                os.fsync(stream.fileno())

    def _receipt_valid(self, job, row, receipt):
        if not isinstance(receipt, dict) or not isinstance(row.get("_fresh"), dict):
            return False
        donor = job["_donors"].get(row.get("donor_relative"))
        if not isinstance(donor, dict) or not isinstance(receipt.get("output_sha256"), str):
            return False
        try:
            return (re.fullmatch(r"[a-f0-9]{64}", receipt["output_sha256"]) is not None
                    and row["excluded_stems"] == donor["excluded_stems"]
                    and receipt.get("excluded_stems") == row["excluded_stems"]
                    and receipt.get("plan_key") == self.packing.plan_key(row["_fresh"], donor, self.match)
                    and receipt.get("output") == str(Path(job["output_dir"]) / row["output_relative"]))
        except (KeyError, ValueError, TypeError):
            return False

    def _replay(self, job):
        if not self.journal_file.is_file():
            return
        rows = {row["id"]: row for row in job["items"]}
        try:
            if self.journal_file.stat().st_size > MAX_CHECKPOINT_BYTES:
                raise ValueError("Receipt journal exceeds its size limit.")
            with self.journal_file.open("rb") as stream:
                while line := stream.readline(MAX_RECORD_BYTES + 1):
                    if len(line) > MAX_RECORD_BYTES or not line.endswith(b"\n"):
                        raise ValueError("Receipt journal has an incomplete final record.")
                    record = json.loads(line)
                    if not isinstance(record, dict) or record.get("job_id") != job["id"]:
                        raise ValueError("Receipt journal belongs to another job.")
                    row = rows.get(record.get("row_id"))
                    if (not row or record.get("status") not in ("done", "failed", "ready")
                            or not isinstance(record.get("reason"), str)
                            or len(record["reason"]) > 500):
                        raise ValueError("Receipt journal target/state is invalid.")
                    if record["status"] == "done" and not self._receipt_valid(job, row, record.get("receipt")):
                        raise ValueError("Receipt journal binding is invalid.")
                    row.update(status=record["status"], reason=record["reason"])
                    row.pop("receipt", None)
                    if record["status"] == "done":
                        row["receipt"] = record["receipt"]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # Keep the file and valid prefix. A bad tail never authorizes an
            # output: Resume rechecks every selected input and existing output.
            job["journal_warning"] = str(exc) + " Existing outputs will be verified on resume."

    def _record(self, row):
        with self.persist_lock:
            with self.lock:
                record = {"job_id": self.job["id"], "row_id": row["id"],
                          "status": row["status"], "reason": row["reason"]}
                if row["status"] == "done":
                    if not self._receipt_valid(self.job, row, row.get("receipt")):
                        raise self.match.ReuseError("Completion receipt does not match the reviewed plan.")
                    record["receipt"] = row["receipt"]
                payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            with self.journal_file.open("ab") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

    def _cancel(self):
        if self.event.is_set():
            raise self.match.ReuseError("Canceled safely.")

    def is_active(self):
        with self.lock:
            return self._working or bool(self.job and self.job["status"] in ACTIVE)

    def _require(self, job_id):
        if not self.job or self.job["id"] != job_id:
            raise self.match.ReuseError("Audio reuse job was not found.")
        return self.job

    def get(self, job_id, *, offset=0, limit=100):
        with self.lock:
            if not self.job or self.job["id"] != job_id:
                return None
            job = self.job
            offset, limit = max(0, int(offset)), max(1, min(500, int(limit)))
            rows = job["items"][offset:offset + limit]
            group_ids = {row.get("group_id") for row in rows if row["status"] == "review"}
            result = {key: value for key, value in job.items()
                      if not key.startswith("_") and key not in ("items", "groups", "source_errors")}
            if self._working and result["status"] not in ACTIVE:
                # Terminal status becomes public only after its checkpoint is
                # durable. A new scan/resume cannot race the preceding save.
                result["status"] = "canceling" if self.event.is_set() else self._worker_phase
            result["items"] = [{key: value for key, value in row.items() if not key.startswith("_")}
                               for row in rows]
            for row in result["items"]:
                if row["status"] == "done" and row.get("receipt", {}).get("recovered"):
                    row["reason"] = EXISTING_REASON
            result["groups"] = [value for key, value in job["groups"].items() if key in group_ids]
            result["source_errors"] = job["source_errors"][:100]
            result["source_errors_total"] = len(job["source_errors"])
            result.update({"items_total": len(job["items"]), "offset": offset, "limit": limit})
            result["output_variants_total"] = sum(bool(row["excluded_stems"]) for row in job["items"])
            counts = self._counts(job)
            result["counts"] = counts
            if result["status"] in ("ready", "completed"):
                result["detail"] = self._summary(job, result["status"])
            terminal = sum(counts[key] for key in ("done", "failed", "blocked", "skipped"))
            result["progress"] = terminal / max(1, counts["total"])
            return copy.deepcopy(result)

    @staticmethod
    def _counts(job):
        counts = {key: sum(row["status"] == key for row in job["items"])
                  for key in ("ready", "review", "blocked", "done", "failed", "skipped", "running")}
        counts["existing"] = sum(row["status"] == "done" and bool(row.get("receipt", {}).get("recovered"))
                                 for row in job["items"])
        counts["created"] = counts["done"] - counts["existing"]
        counts["total"] = len(job["items"])
        return counts

    def _summary(self, job, status):
        counts = self._counts(job)
        if status == "completed":
            detail = f"{counts['created']} created; {counts['existing']} already complete."
        elif counts["ready"]:
            detail = f"{counts['ready']} ready to create; {counts['existing']} already complete."
        elif counts["review"]:
            detail = f"Choose recordings for {counts['review']} variants; {counts['existing']} already complete."
        elif counts["existing"] and not counts["blocked"] and not counts["skipped"]:
            detail = f"No new FeedPaks needed; {counts['existing']} already complete."
        else:
            detail = f"No new FeedPaks ready to create; {counts['existing']} already complete."
        if counts["blocked"] or counts["failed"] or counts["skipped"]:
            detail += (f" {counts['blocked']} blocked; {counts['failed']} failed;"
                       f" {counts['skipped']} skipped. Review the listed reasons.")
        return detail

    def latest(self, *, offset=0, limit=100):
        with self.lock:
            if self.load_error and not self.job:
                return {"id": "checkpoint-unavailable", "status": "failed",
                        "detail": "Previous checkpoint is unavailable and was left unchanged: " + self.load_error,
                        "items": [], "groups": [], "resources": {}, "counts": {},
                        "items_total": 0, "source_errors": [], "progress": 0}
            return self.get(self.job["id"], offset=offset, limit=limit) if self.job else None

    def _launch(self, target):
        with self.lock:
            self._working = True
            self._worker_phase = self.job["status"]

        def execute():
            try:
                target()
            except Exception as exc:
                self.log.exception("minus_mix: audio reuse worker failed")
                with self.lock:
                    self.job.update(status="failed", detail=f"Could not finish saving this job: {exc}"[:500])
            finally:
                with self.lock:
                    self._working = False

        thread = threading.Thread(target=execute, name="minusmix-audio-reuse", daemon=True)
        try:
            thread.start()
        except Exception:
            with self.lock:
                self._working = False
                self.job.update(status="failed", detail="The background worker could not start.")
            self._persist()
            raise

    def _persist_start(self):
        try:
            self._persist()
        except (OSError, ValueError, TypeError) as exc:
            with self.lock:
                self.job.update(status="failed", detail=f"Job did not start: checkpoint could not be saved ({exc}).")
            raise self.match.ReuseError("Job did not start because its checkpoint could not be saved.") from exc

    def start_scan(self, *, old_dir, fresh_dir, output_dir, workers="auto"):
        roots = self.support.roots(old_dir, fresh_dir, output_dir, self.match.ReuseError)
        with self.lock:
            if self.is_active():
                raise self.match.ReuseError("An audio reuse job is already running.")
            if self.legacy_checkpoint:
                # Preserve v1 evidence once before replacing its active state.
                archive = self.state_file.parent / ("audio-reuse-v1-" + uuid.uuid4().hex)
                archive.mkdir()
                for path in (self.state_file, self.journal_file):
                    if path.is_file():
                        shutil.copy2(path, archive / path.name)
                self.legacy_checkpoint = False
            self.load_error = None
            # Reserve before hardware inspection or directory enumeration.
            self.event = threading.Event()
            self.job = {"id": uuid.uuid4().hex, "policy": self.match.POLICY,
                        "status": "scanning", "created_at": time.time(), "detail": "Reading folders",
                        "old_dir": str(roots[0]), "fresh_dir": str(roots[1]), "output_dir": str(roots[2]),
                        "items": [], "groups": {}, "source_errors": [], "resources": {},
                        "input_packages_total": 0,
                        "_donors": {}, "_root_ids": [self.match.signature(root)[:2] for root in roots],
                        "_workers": workers, "_scan_complete": False}
            self._plan_bytes = 4096 + len(json.dumps(self.job, ensure_ascii=False).encode("utf-8"))
            job_id = self.job["id"]
        self._persist_start()
        self._launch(self._scan)
        return self.get(job_id)

    def _guard(self):
        job = self.job
        roots = self.support.roots(job["old_dir"], job["fresh_dir"], job["output_dir"], self.match.ReuseError)
        if [self.match.signature(root)[:2] for root in roots] != job["_root_ids"]:
            raise self.match.ReuseError("A selected folder was replaced; scan again.")
        self._cancel()
        return roots

    def _inspect(self, entry, donor):
        relative, path = entry
        self._cancel()
        try:
            info = self.match.inspect_package(path, donor=donor, cancel=self._cancel,
                                              stem_label=self.exporter.stem_label)
            info["relative_path"] = relative
            return relative, info, None
        except Exception as exc:
            self._cancel()
            return relative, None, str(exc)[:500]

    def _bounded(self, entries, function, workers):
        iterator = iter(entries)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="minusmix-reuse") as pool:
            pending = set()
            while True:
                self._cancel()
                while len(pending) < workers:
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        break
                    pending.add(pool.submit(function, entry))
                if not pending:
                    return
                finished, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in finished:
                    yield future.result()

    def _scan(self):
        try:
            old, fresh, _ = self._guard()
            resources = self.support.resources(self.job["_workers"], error=self.match.ReuseError)
            with self.lock:
                self.job["resources"] = resources
            workers = min(4, resources["effective_workers"])
            for relative, info, error in self._bounded(
                self.support.walk(old, match=self.match, cancel=self._cancel),
                lambda entry: self._inspect(entry, True), workers,
            ):
                with self.lock:
                    if info:
                        self._account_plan({relative: info})
                        self.job["_donors"][relative] = info
                    elif error:
                        failure = {"relative_path": relative, "reason": error}
                        self._account_plan(failure)
                        self.job["source_errors"].append(failure)
                    self.job["detail"] = f"Read existing audio: {relative}"
            by_identity = {}
            for info in self.job["_donors"].values():
                by_identity.setdefault(tuple(info["identity"]), []).append(info)
            for relative, info, error in self._bounded(
                self.support.walk(fresh, match=self.match, cancel=self._cancel),
                lambda entry: self._inspect(entry, False), workers,
            ):
                with self.lock:
                    self.job["input_packages_total"] += 1
                    self._add_row(relative, info, error, by_identity)
            with self.lock:
                self.job["items"].sort(key=lambda row: (row["relative_path"].casefold(), row["excluded_stems"]))
                self._reserve_outputs()
                self._check_plan_size()
            self._verify_existing_rows(self.job["items"], workers)
            with self.lock:
                self.job.update(status="ready", detail=self._summary(self.job, "ready"), _scan_complete=True)
            self._persist()
        except Exception as exc:
            with self.lock:
                self.job["_scan_complete"] = False
                if isinstance(exc, PreviewLimitError):
                    # A partial oversized preview cannot authorize any output.
                    self.job.update(items=[], groups={}, _donors={})
                self.job.update(status="canceled" if self.event.is_set() else "failed", detail=str(exc)[:500])
            self._persist()

    def _verify_existing_rows(self, rows, workers):
        selected = (row for row in rows if row["status"] == "ready" and row["donor_relative"])
        for _ in self._bounded(selected, self._preview_existing, workers):
            pass

    def _preview_existing(self, row):
        try:
            old_root, fresh_root, output_root = self._guard()
            target = self.support.checked_path(output_root, row["output_relative"],
                                               self.match.ReuseError, exists=False)
            if not target.exists():
                return
            with self.lock:
                self.job["detail"] = "Checking existing output: " + row["output_relative"]
            fresh, donor = row["_fresh"], self.job["_donors"][row["donor_relative"]]
            # Reuse the scan's parsed inputs, but verify their current bytes and
            # guarded paths before recognizing an output as already complete.
            for root, relative, info in ((fresh_root, row["relative_path"], fresh),
                                         (old_root, row["donor_relative"], donor)):
                path = self.support.checked_path(root, relative, self.match.ReuseError)
                if path != Path(info["path"]):
                    raise self.match.ReuseError("Input path changed after inspection; scan again.")
                self.packing.recheck(info, self.match, self._cancel)
            receipt = self.packing.completed_output(target, fresh, donor, match=self.match,
                                                    cancel=self._cancel, stem_label=self.exporter.stem_label)
            for info in (fresh, donor):
                if self.match.signature(Path(info["path"])) != info["signature"]:
                    raise self.match.ReuseError("Input changed during verification; scan again.")
            self._guard()
            if not receipt:
                raise self.match.ReuseError("Output exists with different content; it was not overwritten.")
            with self.lock:
                row.update(status="done", reason=EXISTING_REASON, receipt=receipt)
        except Exception as exc:
            self._cancel()
            with self.lock:
                row.update(status="blocked", reason="Existing output could not be accepted: " + str(exc)[:440])
                row.pop("receipt", None)

    def _check_choices(self, rows):
        try:
            workers = min(4, self.job["resources"]["effective_workers"])
            self._verify_existing_rows(rows, workers)
            with self.lock:
                self.job.update(status="ready", detail=self._summary(self.job, "ready"))
        except Exception as exc:
            with self.lock:
                self.job.update(status="canceled" if self.event.is_set() else "failed", detail=str(exc)[:500])
        finally:
            self._persist()

    def _add_row(self, relative, info, error, by_identity):
        row = {"relative_path": relative, "title": info["title"] if info else relative,
               "status": "blocked", "reason": error or "No compatible existing MinusMix version found.",
               "group_id": None, "donor_relative": None, "output_relative": None, "_fresh": info,
               "excluded_stems": [], "variant_label": ""}
        variants = {}
        if info and info["derived"]:
            row.update(status="skipped", reason="Fresh input is already a derived mix; use its ordinary fresh package.")
        elif info:
            for donor in by_identity.get(tuple(info["identity"]), []):
                if self.match.compatible(info, donor):
                    variants.setdefault(tuple(donor["excluded_stems"]), []).append(donor)
        if len(self.job["items"]) + max(1, len(variants)) > self.match.MAX_OUTPUT_ROWS:
            raise PreviewLimitError(
                f"Preview exceeds the {self.match.MAX_OUTPUT_ROWS:,} output-row limit; use smaller input folders.")
        if not variants:
            row["id"] = self._row_id(relative, [])
            self._account_plan(row)
            self.job["items"].append(row)
        for stems, candidates in sorted(variants.items()):
            variant = {**row, "id": self._row_id(relative, stems), "excluded_stems": list(stems),
                       "variant_label": self.match.variant_suffix(list(stems), stem_label=self.exporter.stem_label)}
            unique = {}
            for donor in sorted(candidates, key=lambda value: value["relative_path"].casefold()):
                unique.setdefault(self.match.audio_identity(donor), donor)
            self._set_variant_group(variant, info, unique)
            self._account_plan(variant)
            self.job["items"].append(variant)
        self.job["detail"] = f"Matched fresh charts: {relative}"

    @staticmethod
    def _row_id(relative, stems):
        return hashlib.sha256(json.dumps([relative, list(stems)]).encode()).hexdigest()

    def _set_variant_group(self, row, info, unique):
        group_data = [info["identity"], info["arrangements"], info["duration"], info["offset"],
                      info["audio_offset"], row["excluded_stems"],
                      sorted(donor["relative_path"] for donor in unique.values())]
        group_id = hashlib.sha256(json.dumps(group_data).encode()).hexdigest()[:24]
        row["group_id"] = group_id
        manual = len(unique) > 1
        row["status"] = "review" if manual else "ready"
        row["reason"] = ("Choose a compatible recording: multiple audio versions of this mix variant match."
                         if manual else "Unique compatible recording among readable donors; known repair fields may differ.")
        if not manual:
            row["donor_relative"] = next(iter(unique.values()))["relative_path"]
        group = self.job["groups"].get(group_id)
        if group is None:
            group = self._new_group(group_id, row, info, unique)
            self._account_plan({group_id: group})
            self.job["groups"][group_id] = group
        group["targets_count"] += 1

    @staticmethod
    def _new_group(group_id, row, info, unique):
        return {
            "id": group_id, "title": info["title"], "targets_count": 0, "reason": row["reason"],
            "excluded_stems": row["excluded_stems"], "variant_label": row["variant_label"],
            "candidates": [{"id": value["relative_path"], "relative_path": value["relative_path"],
                            "full_sha256": value["audio"]["full"]["sha256"],
                            "preview_sha256": value["audio"].get("preview", {}).get("sha256")}
                           for value in unique.values()],
        }

    def _account_plan(self, value):
        # Incremental accounting avoids retaining an unbounded preview before
        # the final completion-headroom check. No quadratic whole-plan writes.
        self._plan_bytes += 64 + len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if self._plan_bytes > MAX_CHECKPOINT_BYTES:
            raise PreviewLimitError("Preview exceeds the saved-job size limit; use smaller input folders.")

    def _check_plan_size(self):
        size = len(json.dumps(self.job, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        # Reserve enough for every completion receipt, bounded error text and
        # output path before approving a plan that has to survive restart.
        reserve = 0
        for row in self.job["items"]:
            if not row["output_relative"]:
                continue
            row_reserve = 4096 + len(json.dumps({
                "output": str(Path(self.job["output_dir"]) / row["output_relative"]),
                "excluded_stems": row["excluded_stems"],
            }, ensure_ascii=False).encode("utf-8"))
            if row_reserve > MAX_RECORD_BYTES:
                raise PreviewLimitError("A mix variant exceeds the completion-record size limit.")
            reserve += row_reserve
        if size + reserve > MAX_CHECKPOINT_BYTES:
            raise PreviewLimitError("Preview and completion receipts exceed the saved-job size limit; use smaller input folders.")

    def _reserve_outputs(self):
        used = set()
        for row in self.job["items"]:
            if row["status"] in ("blocked", "skipped"):
                continue
            relative = Path(row["relative_path"])
            wanted = self.exporter.desired_output_path(relative.parent, relative,
                                                       excluded_stems=row["excluded_stems"])
            # Leave room for collision numbers and .feedpak on Windows, even
            # when a valid custom stem label or source name uses surrogate pairs.
            stem = wanted.stem.encode("utf-16-le")[:420].decode("utf-16-le", errors="ignore").rstrip(" .")
            for number in range(1, self.match.MAX_OUTPUT_ROWS + 1):
                suffix = "" if number == 1 else f" ({number})"
                output = wanted.with_name(stem + suffix + wanted.suffix).as_posix()
                if output.casefold() not in used:
                    used.add(output.casefold())
                    row["output_relative"] = output
                    break

    def choose(self, job_id, choices):
        with self.lock:
            job = self._require(job_id)
            if self.is_active() or not job["_scan_complete"]:
                raise self.match.ReuseError("Wait for the completed match preview.")
            if not isinstance(choices, dict):
                raise self.match.ReuseError("Invalid recording choices.")
            for group_id, donor_id in choices.items():
                group = job["groups"].get(group_id)
                if not group or donor_id not in {"__skip__", *(item["id"] for item in group["candidates"])}:
                    raise self.match.ReuseError("Choice is not a compatible preview candidate.")
            changed = []
            for row in job["items"]:
                if row["group_id"] not in choices or row["status"] == "done":
                    continue
                choice = choices[row["group_id"]]
                row.update(status="skipped" if choice == "__skip__" else "ready",
                           donor_relative=None if choice == "__skip__" else choice,
                           reason="Skipped by choice." if choice == "__skip__" else "Recording chosen in review.")
                changed.append(row)
            self.event = threading.Event()
            job.update(status="scanning", detail="Checking existing outputs for the chosen recordings.")
        self._persist_start()
        self._launch(lambda: self._check_choices(changed))
        return self.get(job_id)

    def apply(self, job_id):
        with self.lock:
            job = self._require(job_id)
            if self.is_active() or not job["_scan_complete"]:
                raise self.match.ReuseError("A completed idle match preview is required.")
            if not any(row["status"] in ("ready", "failed", "done") and row["donor_relative"]
                       for row in job["items"]):
                raise self.match.ReuseError("There are no reviewed matches to create or resume.")
            self.event = threading.Event()
            self._guard()
            job.update(status="running", detail="Rechecking selected inputs and creating new packages.")
        self._persist_start()
        self._launch(self._run)
        return self.get(job_id)

    def cancel(self, job_id):
        with self.lock:
            job = self._require(job_id)
            if job["status"] in ACTIVE:
                self.event.set()
                job.update(status="canceling", detail="Cancel requested; finishing active copy checkpoints.")
        return self.get(job_id)

    def _current_pair(self, row):
        old, fresh, output = self._guard()
        prior_fresh = row["_fresh"]
        prior_donor = self.job["_donors"][row["donor_relative"]]
        fresh_path = self.support.checked_path(fresh, row["relative_path"], self.match.ReuseError)
        donor_path = self.support.checked_path(old, row["donor_relative"], self.match.ReuseError)
        current_fresh = self._snapshot(fresh_path, prior_fresh, donor=False)
        current_donor = self._snapshot(donor_path, prior_donor, donor=True)
        if current_donor["excluded_stems"] != row["excluded_stems"]:
            raise self.match.ReuseError("Selected audio does not match the reviewed mix variant.")
        if not self.match.compatible(current_fresh, current_donor):
            raise self.match.ReuseError("Selected inputs no longer match.")
        target = self.support.checked_path(output, row["output_relative"], self.match.ReuseError, exists=False)
        return current_fresh, current_donor, target

    def _snapshot(self, path, reviewed, *, donor):
        # Only this Apply run shares verification. Guarded paths and signatures
        # are checked on every use; streamed output bytes are still verified.
        if self.match.signature(path) != reviewed["signature"]:
            raise self.match.ReuseError("Input changed after preview; scan again.")
        key = (str(path), reviewed["sha256"], donor)
        with self.lock:
            entry = self._snapshots.get(key)
            owner = entry is None
            if owner:
                entry = [Future(), 0]
                self._snapshots[key] = entry
            self._snapshots.move_to_end(key)
        future = entry[0]
        if owner:
            try:
                info = self.match.inspect_package(path, donor=donor, cancel=self._cancel,
                                                  include_payload=not donor, stem_label=self.exporter.stem_label)
                if info["sha256"] != reviewed["sha256"]:
                    raise self.match.ReuseError("Input changed after preview; scan again.")
                size = len(json.dumps(info, ensure_ascii=False).encode("utf-8"))
                with self.lock:
                    entry[1] = size
                    self._snapshot_bytes += size
                    future.set_result(info)
                    self._trim_snapshots()
            except Exception as exc:
                with self.lock:
                    future.set_exception(exc)
                    self._trim_snapshots()
        while True:
            self._cancel()
            try:
                result = future.result(timeout=0.2)
                break
            except TimeoutError:
                if future.done():
                    raise
        if not owner and self.match.digest_file(path, self._cancel) != reviewed["sha256"]:
            raise self.match.ReuseError("Input content changed after preview; scan again.")
        if self.match.signature(path) != result["signature"]:
            raise self.match.ReuseError("Input changed during verification.")
        return result

    def _trim_snapshots(self):
        for key, (future, size) in list(self._snapshots.items()):
            if len(self._snapshots) <= SNAPSHOT_CACHE_ENTRIES and self._snapshot_bytes <= SNAPSHOT_CACHE_BYTES:
                break
            if future.done():
                del self._snapshots[key]
                self._snapshot_bytes -= size

    def _process(self, row):
        try:
            self._cancel()
            with self.lock:
                row.update(status="running", reason="Verifying the approved inputs.")
            fresh, donor, target = self._current_pair(row)
            if target.exists():
                receipt = self.packing.completed_output(target, fresh, donor, match=self.match, cancel=self._cancel,
                                                        stem_label=self.exporter.stem_label)
                if not receipt:
                    raise self.match.ReuseError("Output exists with different content; it was not overwritten.")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)

                def guard():
                    self._guard()
                    self.support.checked_path(Path(self.job["output_dir"]), row["output_relative"],
                                              self.match.ReuseError, exists=False)

                receipt = self.packing.export_reuse(fresh, donor, target, match=self.match,
                                                    exporter=self.exporter, cancel=self._cancel, guard=guard,
                                                    snapshot_verified=True)
            with self.lock:
                reason = EXISTING_REASON if receipt.get("recovered") else "Created and byte-verified; audio was not encoded."
                row.update(status="done", reason=reason, receipt=receipt)
            self._record(row)
        except Exception as exc:
            with self.lock:
                row.update(status="ready" if self.event.is_set() else "failed", reason=str(exc)[:500])
                row.pop("receipt", None)
            self._record(row)
        return row["id"]

    def _run(self):
        with self.lock:
            self._snapshots.clear()
            self._snapshot_bytes = 0
        try:
            resources = self.support.resources(self.job["_workers"], error=self.match.ReuseError)
            with self.lock:
                self.job["resources"] = resources
                rows = [row for row in self.job["items"]
                        if row["status"] in ("ready", "failed", "done") and row["donor_relative"]]
            def admitted_rows():
                for row in rows:
                    self._guard()
                    donor = self.job["_donors"][row["donor_relative"]]
                    estimate = row["_fresh"]["signature"][2] + donor["audio"]["full"]["bytes"]
                    self.support.admission(self.job["output_dir"], estimate * resources["effective_workers"],
                                           resources["effective_workers"], self.match.ReuseError)
                    yield row

            for _ in self._bounded(admitted_rows(), self._process, resources["effective_workers"]):
                pass
            with self.lock:
                if all(row["status"] == "done" for row in rows):
                    self.job.pop("journal_warning", None)
                self.job.update(status="completed", detail=self._summary(self.job, "completed"))
        except Exception as exc:
            with self.lock:
                self.job.update(status="canceled" if self.event.is_set() else "failed", detail=str(exc)[:500])
        finally:
            with self.lock:
                self._snapshots.clear()
                self._snapshot_bytes = 0
            self._persist()
