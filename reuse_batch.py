"""Independent reviewed audio-reuse queue with bounded workers and resumable receipts."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

ACTIVE = {"scanning", "running", "canceling"}


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
        self._load()

    def _load(self):
        if not self.state_file.is_file():
            return
        try:
            if self.state_file.stat().st_size > 64 * 1024**2:
                raise ValueError("Checkpoint exceeds its size limit.")
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            spec = importlib.util.spec_from_file_location("_minusmix_reuse_state", Path(__file__).with_name("reuse_state.py"))
            state = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(state)
            state.validate(data, self.match.POLICY, self.match.MAX_FILES)
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
                payload = json.dumps(self.job, ensure_ascii=False, separators=(",", ":"))
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_file.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as stream:
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
                    and receipt.get("plan_key") == self.packing.plan_key(row["_fresh"], donor, self.match)
                    and receipt.get("output") == str(Path(job["output_dir"]) / row["output_relative"]))
        except (KeyError, ValueError, TypeError):
            return False

    def _replay(self, job):
        if not self.journal_file.is_file():
            return
        rows = {row["id"]: row for row in job["items"]}
        try:
            if self.journal_file.stat().st_size > 32 * 1024**2:
                raise ValueError("Receipt journal exceeds its size limit.")
            with self.journal_file.open("rb") as stream:
                while line := stream.readline(32769):
                    if len(line) > 32768 or not line.endswith(b"\n"):
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
            return bool(self.job and self.job["status"] in ACTIVE)

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
            result["items"] = [{key: value for key, value in row.items() if not key.startswith("_")}
                               for row in rows]
            result["groups"] = [value for key, value in job["groups"].items() if key in group_ids]
            result["source_errors"] = job["source_errors"][:100]
            result["source_errors_total"] = len(job["source_errors"])
            result.update({"items_total": len(job["items"]), "offset": offset, "limit": limit})
            counts = {key: sum(row["status"] == key for row in job["items"])
                      for key in ("ready", "review", "blocked", "done", "failed", "skipped", "running")}
            counts["total"] = len(job["items"])
            result["counts"] = counts
            terminal = sum(counts[key] for key in ("done", "failed", "blocked", "skipped"))
            result["progress"] = terminal / max(1, counts["total"])
            return copy.deepcopy(result)

    def latest(self, *, offset=0, limit=100):
        with self.lock:
            if self.load_error and not self.job:
                return {"id": "checkpoint-unavailable", "status": "failed",
                        "detail": "Previous checkpoint is unavailable and was left unchanged: " + self.load_error,
                        "items": [], "groups": [], "resources": {}, "counts": {},
                        "items_total": 0, "source_errors": [], "progress": 0}
            return self.get(self.job["id"], offset=offset, limit=limit) if self.job else None

    def _launch(self, target):
        thread = threading.Thread(target=target, name="minusmix-audio-reuse", daemon=True)
        try:
            thread.start()
        except Exception:
            with self.lock:
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
            # Reserve before hardware inspection or directory enumeration.
            self.event = threading.Event()
            self.job = {"id": uuid.uuid4().hex, "policy": self.match.POLICY,
                        "status": "scanning", "created_at": time.time(), "detail": "Reading folders",
                        "old_dir": str(roots[0]), "fresh_dir": str(roots[1]), "output_dir": str(roots[2]),
                        "items": [], "groups": {}, "source_errors": [], "resources": {},
                        "_donors": {}, "_root_ids": [self.match.signature(root)[:2] for root in roots],
                        "_workers": workers, "_scan_complete": False}
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
            info = self.match.inspect_package(path, donor=donor, cancel=self._cancel)
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
                        self.job["_donors"][relative] = info
                    elif error != "Not a declared No Guitar MinusMix package.":
                        self.job["source_errors"].append({"relative_path": relative, "reason": error})
                    self.job["detail"] = f"Read existing audio: {relative}"
            by_identity = {}
            for info in self.job["_donors"].values():
                by_identity.setdefault(tuple(info["identity"]), []).append(info)
            for relative, info, error in self._bounded(
                self.support.walk(fresh, match=self.match, cancel=self._cancel),
                lambda entry: self._inspect(entry, False), workers,
            ):
                with self.lock:
                    self._add_row(relative, info, error, by_identity)
            with self.lock:
                self.job["items"].sort(key=lambda row: row["relative_path"].casefold())
                self._reserve_outputs()
                self.job.update(status="ready", detail="Review matches, then create new No Guitar packages.",
                                _scan_complete=True)
            self._persist()
        except Exception as exc:
            with self.lock:
                self.job.update(status="canceled" if self.event.is_set() else "failed", detail=str(exc)[:500])
            self._persist()

    def _add_row(self, relative, info, error, by_identity):
        row = {"id": relative, "relative_path": relative, "title": info["title"] if info else relative,
               "status": "blocked", "reason": error or "No compatible existing No Guitar version found.",
               "group_id": None, "donor_relative": None, "output_relative": None, "_fresh": info}
        if info and info["derived"]:
            row.update(status="skipped", reason="Fresh input is already a derived mix; use its ordinary fresh package.")
        elif info:
            candidates = [donor for donor in by_identity.get(tuple(info["identity"]), [])
                          if self.match.compatible(info, donor)]
            unique = {}
            for donor in sorted(candidates, key=lambda value: value["relative_path"].casefold()):
                unique.setdefault(self.match.audio_identity(donor), donor)
            if unique:
                group_data = [info["identity"], info["arrangements"], info["duration"],
                              sorted(donor["relative_path"] for donor in unique.values())]
                group_id = hashlib.sha256(json.dumps(group_data).encode()).hexdigest()[:24]
                row["group_id"] = group_id
                manual = len(unique) > 1
                row["status"] = "review" if manual else "ready"
                row["reason"] = ("Choose a compatible recording: multiple audio versions match."
                                 if manual else "Unique compatible recording among readable donors; known repair fields may differ.")
                if not manual:
                    row["donor_relative"] = next(iter(unique.values()))["relative_path"]
                group = self.job["groups"].setdefault(group_id, {
                    "id": group_id, "title": info["title"], "targets_count": 0, "reason": row["reason"],
                    "candidates": [{"id": value["relative_path"], "relative_path": value["relative_path"],
                                    "full_sha256": value["audio"]["full"]["sha256"],
                                    "preview_sha256": value["audio"].get("preview", {}).get("sha256")}
                                   for value in unique.values()],
                })
                group["targets_count"] += 1
        with self.lock:
            self.job["items"].append(row)
            self.job["detail"] = f"Matched fresh charts: {relative}"

    def _reserve_outputs(self):
        used = set()
        for row in self.job["items"]:
            if row["status"] in ("blocked", "skipped"):
                continue
            relative = Path(row["relative_path"])
            stem = re.sub(r"(?i)\s*\(no guitar\)\s*$", "", relative.stem)
            for number in range(1, self.match.MAX_FILES + 1):
                suffix = "" if number == 1 else f" ({number})"
                output = relative.with_name(stem + " (No Guitar)" + suffix + ".feedpak").as_posix()
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
            for row in job["items"]:
                if row["group_id"] not in choices or row["status"] == "done":
                    continue
                choice = choices[row["group_id"]]
                row.update(status="skipped" if choice == "__skip__" else "ready",
                           donor_relative=None if choice == "__skip__" else choice,
                           reason="Skipped by choice." if choice == "__skip__" else "Recording chosen in review.")
        self._persist()
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
        current_fresh = self.match.inspect_package(fresh_path, cancel=self._cancel, include_payload=True)
        current_donor = self.match.inspect_package(donor_path, donor=True, cancel=self._cancel)
        if current_fresh["sha256"] != prior_fresh["sha256"] or current_donor["sha256"] != prior_donor["sha256"]:
            raise self.match.ReuseError("Input changed after preview; scan again.")
        if not self.match.compatible(current_fresh, current_donor):
            raise self.match.ReuseError("Selected inputs no longer match.")
        target = self.support.checked_path(output, row["output_relative"], self.match.ReuseError, exists=False)
        return current_fresh, current_donor, target

    def _process(self, row):
        try:
            self._cancel()
            with self.lock:
                row.update(status="running", reason="Verifying the approved inputs.")
            fresh, donor, target = self._current_pair(row)
            if target.exists():
                receipt = self.packing.completed_output(target, fresh, donor, match=self.match, cancel=self._cancel)
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
                row.update(status="done", reason="Created and byte-verified; audio was not encoded.", receipt=receipt)
            self._record(row)
        except Exception as exc:
            with self.lock:
                row.update(status="ready" if self.event.is_set() else "failed", reason=str(exc)[:500])
                row.pop("receipt", None)
            self._record(row)
        return row["id"]

    def _run(self):
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
                self.job.update(status="completed", detail="Audio reuse completed; review any skipped or failed items.")
        except Exception as exc:
            with self.lock:
                self.job.update(status="canceled" if self.event.is_set() else "failed", detail=str(exc)[:500])
        finally:
            self._persist()
