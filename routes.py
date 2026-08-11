"""Backend API for the MinusMix plugin."""
from __future__ import annotations

import ipaddress
import threading
from pathlib import Path
from typing import Any, TypedDict

from fastapi import FastAPI, HTTPException, Request

PLUGIN_ID = "minus_mix"
API = f"/api/plugins/{PLUGIN_ID}"
MAX_SOURCE_RESULTS = 250
MAX_PUBLIC_BATCH_ITEMS = 400
DEFAULT_TARGETS = ("guitar", "bass", "drums", "vocals", "piano", "other")


class BatchRequestOptions(TypedDict):
    input_dir: object
    output_dir: object
    excluded_stems: object
    recursive: bool
    skip_existing: bool
    skip_derived: bool
    preserve_structure: bool


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host.lower() == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    return bool(getattr(addr, "ipv4_mapped", None) and addr.ipv4_mapped.is_loopback)


def _public_batch(payload: dict | None, *, scan: bool = False) -> dict | None:
    """Return a bounded UI snapshot while retaining actionable batch rows."""
    if payload is None:
        return None
    result = dict(payload)
    items = list(payload.get("items") or [])
    # BatchManager can now build the bounded snapshot while holding its lock,
    # before any discarded rows are deep-copied. Preserve that metadata rather
    # than treating the already-limited list as the complete job.
    if (
        isinstance(payload.get("items_total"), int)
        and isinstance(payload.get("items_truncated"), bool)
    ):
        result["items"] = items
        return result
    result["items_total"] = len(items)
    if len(items) <= MAX_PUBLIC_BATCH_ITEMS:
        result["items"] = items
        result["items_truncated"] = False
        return result

    if scan:
        visible = items[:MAX_PUBLIC_BATCH_ITEMS]
    else:
        selected: set[int] = set()

        # A running row must never disappear just because it is outside the
        # trailing results window. Failed rows are next in priority because
        # they contain the information a user can act on.
        for index, item in enumerate(items):
            if item.get("status") == "running":
                selected.add(index)
        for index in range(len(items) - 1, -1, -1):
            if len(selected) >= MAX_PUBLIC_BATCH_ITEMS:
                break
            if items[index].get("status") == "failed":
                selected.add(index)

        # Fill the remaining bounded view with the end of the queue. Sorting
        # the chosen indexes restores the source-folder order in the UI.
        for index in range(len(items) - 1, -1, -1):
            if len(selected) >= MAX_PUBLIC_BATCH_ITEMS:
                break
            selected.add(index)
        visible = [items[index] for index in sorted(selected)]

    result["items"] = visible
    result["items_truncated"] = True
    return result


class MinusMixAPI:
    """Own MinusMix route dependencies and expose small, testable handlers."""

    def __init__(
        self,
        *,
        exporter: Any,
        batch_module: Any,
        single_module: Any,
        separator_module: Any,
        separator: Any,
        batch_manager: Any,
        single_manager: Any,
        log: Any,
        get_dlc_dir: Any = None,
        meta_db: Any = None,
    ) -> None:
        self.exporter = exporter
        self.batch_module = batch_module
        self.single_module = single_module
        self.separator_module = separator_module
        self.separator = separator
        self.batch_manager = batch_manager
        self.single_manager = single_manager
        self.log = log
        self.get_dlc_dir = get_dlc_dir
        self.meta_db = meta_db
        self.operation_start_lock = threading.RLock()

    @staticmethod
    def _batch_options(body: dict) -> BatchRequestOptions:
        if not isinstance(body, dict):
            raise HTTPException(400, "invalid batch request")
        return {
            "input_dir": body.get("input_dir"),
            "output_dir": body.get("output_dir"),
            "excluded_stems": body.get("excluded_stems"),
            "recursive": body.get("recursive", True) is not False,
            "skip_existing": body.get("skip_existing", True) is not False,
            "skip_derived": body.get("skip_derived", True) is not False,
            # Preserve the pre-option API behaviour for older callers. The
            # current UI sends the same structure-preserving default explicitly.
            "preserve_structure": body.get("preserve_structure", True) is not False,
        }

    def _resolve_source(self, filename: str) -> Path:
        if not isinstance(filename, str) or not filename.strip():
            raise HTTPException(400, "choose a source feedpak")
        library = self.get_dlc_dir() if self.get_dlc_dir else None
        if not library:
            raise HTTPException(409, "configure a local song library first")
        from safepath import safe_join

        target = safe_join(Path(library).resolve(), filename)
        if target is None or not Path(target).exists():
            raise HTTPException(404, "the source feedpak is no longer in the library")
        return Path(target)

    @staticmethod
    def _require_loopback(request: Request, detail: str) -> None:
        if not _is_loopback(request):
            raise HTTPException(403, detail)

    def register(self, app: FastAPI) -> None:
        """Register routes in specificity order so fixed paths win over IDs."""
        app.add_api_route(f"{API}/status", self.status, methods=["GET"])
        app.add_api_route(f"{API}/sources", self.sources, methods=["GET"])
        app.add_api_route(f"{API}/source", self.source, methods=["GET"])
        app.add_api_route(f"{API}/export", self.export, methods=["POST"])
        app.add_api_route(f"{API}/export/latest", self.export_latest, methods=["GET"])
        app.add_api_route(f"{API}/export/{{job_id}}", self.export_status, methods=["GET"])
        app.add_api_route(
            f"{API}/export/{{job_id}}/cancel", self.export_cancel, methods=["POST"]
        )
        app.add_api_route(f"{API}/batch/scan", self.batch_scan, methods=["POST"])
        app.add_api_route(f"{API}/batch/scan-jobs", self.batch_scan_start, methods=["POST"])
        app.add_api_route(
            f"{API}/batch/scan-jobs/{{scan_job_id}}",
            self.batch_scan_status,
            methods=["GET"],
        )
        app.add_api_route(
            f"{API}/batch/scan-jobs/{{scan_job_id}}/cancel",
            self.batch_scan_cancel,
            methods=["POST"],
        )
        app.add_api_route(f"{API}/batch/start", self.batch_start, methods=["POST"])
        app.add_api_route(f"{API}/batch/latest", self.batch_latest, methods=["GET"])
        app.add_api_route(f"{API}/batch/{{job_id}}", self.batch_status, methods=["GET"])
        app.add_api_route(
            f"{API}/batch/{{job_id}}/cancel", self.batch_cancel, methods=["POST"]
        )

    def status(self):
        from audio import _ffmpeg_cmd

        engine = self.separator.status()
        return {"ok": True, "ffmpeg_available": bool(_ffmpeg_cmd()), "separation": engine}

    def sources(self, q: str = ""):
        if self.meta_db is None:
            return {"songs": []}
        found: list[dict] = []
        page = 0
        try:
            while len(found) < MAX_SOURCE_RESULTS and page < 2000:
                songs, total = self.meta_db.query_page(
                    q=q,
                    page=page,
                    size=500,
                    sort="artist",
                    format_filter="sloppak",
                )
                for song in songs:
                    filename = song.get("filename")
                    if not isinstance(filename, str) or Path(filename).suffix.lower() not in (
                        ".feedpak",
                        ".sloppak",
                    ):
                        continue
                    stem_ids = [str(s).lower() for s in (song.get("stem_ids") or [])]
                    available = [s for s in stem_ids if s != "full"]
                    found.append(
                        {
                            "filename": filename,
                            "title": song.get("title") or "Untitled",
                            "artist": song.get("artist") or "",
                            "stem_ids": available,
                            "already_split": bool(available),
                        }
                    )
                    if len(found) >= MAX_SOURCE_RESULTS:
                        break
                if (
                    not songs
                    or len(songs) < 500
                    or (isinstance(total, int) and (page + 1) * 500 >= total)
                ):
                    break
                page += 1
        except Exception as exc:
            self.log.exception("minus_mix: could not query feedpak sources")
            raise HTTPException(500, "could not read the local song index") from exc
        return {"songs": found, "truncated": len(found) >= MAX_SOURCE_RESULTS}

    def source(self, filename: str):
        try:
            info = self.exporter.inspect_source(self._resolve_source(filename))
        except HTTPException:
            raise
        except self.exporter.ExportError as exc:
            raise HTTPException(400, str(exc)) from exc
        saved = {s.id: s for s in info.stems if s.id != "full"}
        supported = list(
            getattr(self.separator_module, "SUPPORTED_STEMS", DEFAULT_TARGETS)
        )
        target_ids = list(dict.fromkeys([*supported, *saved.keys()]))
        return {
            "filename": filename,
            "title": info.title,
            "artist": info.artist,
            "stems": [
                {
                    "id": stem_id,
                    "file": saved[stem_id].file if stem_id in saved else None,
                    "label": self.exporter.stem_label(stem_id),
                    "saved": stem_id in saved,
                    "requires_separation": stem_id not in saved,
                }
                for stem_id in target_ids
            ],
            "already_split": bool(saved),
            "arrangements": list(info.arrangements),
            "derived_exclusions": list(info.derived_exclusions),
        }

    def export(self, body: dict, request: Request):
        # This endpoint accepts an absolute path selected by the native desktop
        # dialog. Never expose that write primitive to clients connected through
        # optional LAN mode.
        self._require_loopback(
            request, "MinusMix export is only available on this computer"
        )
        if not isinstance(body, dict):
            raise HTTPException(400, "invalid export request")
        output_raw = body.get("output_dir")
        if not isinstance(output_raw, str) or not output_raw.strip():
            raise HTTPException(400, "choose an output folder")
        output_dir = Path(output_raw.strip()).expanduser()
        if not output_dir.is_absolute() or not output_dir.is_dir():
            raise HTTPException(400, "the chosen output folder no longer exists")
        excluded = body.get("excluded_stems")
        if not isinstance(excluded, list) or not all(isinstance(s, str) for s in excluded):
            raise HTTPException(400, "excluded_stems must be a list of stem ids")

        source_path = self._resolve_source(body.get("filename"))
        try:
            with self.operation_start_lock:
                if self.batch_manager.is_active():
                    raise self.single_module.SingleExportError(
                        "wait for the active MinusMix batch to finish or cancel it first"
                    )
                return self.single_manager.start(source_path, output_dir, excluded)
        except (self.exporter.ExportError, self.single_module.SingleExportError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, "the app cannot write to the chosen output folder") from exc
        except Exception as exc:
            self.log.exception("minus_mix: could not start single export")
            raise HTTPException(500, "the practice feedpak job could not be started") from exc

    def export_latest(self, request: Request):
        self._require_loopback(
            request, "single-song export status is only available on this computer"
        )
        return {"job": self.single_manager.latest()}

    def export_status(self, job_id: str, request: Request):
        self._require_loopback(
            request, "single-song export status is only available on this computer"
        )
        job = self.single_manager.get(job_id)
        if job is None:
            raise HTTPException(404, "single-song export job not found")
        return job

    def export_cancel(self, job_id: str, request: Request):
        self._require_loopback(
            request, "single-song export cancellation is only available on this computer"
        )
        try:
            return self.single_manager.cancel(job_id)
        except self.single_module.SingleExportError as exc:
            raise HTTPException(404, str(exc)) from exc

    def batch_scan(self, body: dict, request: Request):
        self._require_loopback(request, "batch scanning is only available on this computer")
        try:
            return _public_batch(
                self.batch_manager.scan(**self._batch_options(body)), scan=True
            )
        except self.batch_module.BatchError as exc:
            raise HTTPException(400, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, "the app cannot read one of the selected folders") from exc
        except OSError as exc:
            self.log.exception("minus_mix: batch scan failed")
            raise HTTPException(500, "the selected folders could not be scanned") from exc

    def batch_scan_start(self, body: dict, request: Request):
        self._require_loopback(request, "batch scanning is only available on this computer")
        try:
            return self.batch_manager.start_scan(**self._batch_options(body))
        except self.batch_module.BatchError as exc:
            status_code = 409 if "already running" in str(exc) else 400
            raise HTTPException(status_code, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, "the app cannot read one of the selected folders") from exc

    def batch_scan_status(self, scan_job_id: str, request: Request):
        self._require_loopback(
            request, "batch scan status is only available on this computer"
        )
        job = self.batch_manager.get_scan(
            scan_job_id, result_item_limit=MAX_PUBLIC_BATCH_ITEMS,
        )
        if job is None:
            raise HTTPException(404, "folder scan job not found")
        if isinstance(job.get("result"), dict):
            job["result"] = _public_batch(job["result"], scan=True)
        return job

    def batch_scan_cancel(self, scan_job_id: str, request: Request):
        self._require_loopback(
            request, "batch scan cancellation is only available on this computer"
        )
        try:
            return self.batch_manager.cancel_scan(scan_job_id)
        except self.batch_module.BatchError as exc:
            raise HTTPException(404, str(exc)) from exc

    def batch_start(self, body: dict, request: Request):
        self._require_loopback(request, "batch conversion is only available on this computer")
        try:
            with self.operation_start_lock:
                if self.single_manager.is_active():
                    raise self.batch_module.BatchError(
                        "wait for the active single-song export to finish or cancel it first"
                    )
                scan_id = body.get("scan_id") if isinstance(body, dict) else None
                return _public_batch(
                    self.batch_manager.start(
                        scan_id=scan_id,
                        snapshot_item_limit=MAX_PUBLIC_BATCH_ITEMS,
                        **self._batch_options(body),
                    )
                )
        except self.batch_module.BatchError as exc:
            status_code = (
                409
                if "already running" in str(exc) or "no new feedpaks" in str(exc)
                else 400
            )
            raise HTTPException(status_code, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, "the app cannot access one of the selected folders") from exc

    def batch_latest(self, request: Request):
        self._require_loopback(request, "batch status is only available on this computer")
        return {
            "job": _public_batch(
                self.batch_manager.latest(item_limit=MAX_PUBLIC_BATCH_ITEMS)
            )
        }

    def batch_status(self, job_id: str, request: Request):
        self._require_loopback(request, "batch status is only available on this computer")
        job = self.batch_manager.get(job_id, item_limit=MAX_PUBLIC_BATCH_ITEMS)
        if job is None:
            raise HTTPException(404, "batch job not found")
        return _public_batch(job)

    def batch_cancel(self, job_id: str, request: Request):
        self._require_loopback(request, "batch cancellation is only available on this computer")
        try:
            return _public_batch(
                self.batch_manager.cancel(
                    job_id, snapshot_item_limit=MAX_PUBLIC_BATCH_ITEMS,
                )
            )
        except self.batch_module.BatchError as exc:
            raise HTTPException(404, str(exc)) from exc


def setup(app: FastAPI, context: dict) -> None:
    """Compose MinusMix dependencies, then register the transport layer."""
    exporter = context["load_sibling"]("exporter")
    batch_module = context["load_sibling"]("batch")
    single_module = context["load_sibling"]("single")
    separator_module = context["load_sibling"]("separator_client")
    log = context["log"]
    config_dir = Path(context["config_dir"])
    separator = separator_module.SeparationClient(config_dir, log)

    api = MinusMixAPI(
        exporter=exporter,
        batch_module=batch_module,
        single_module=single_module,
        separator_module=separator_module,
        separator=separator,
        batch_manager=batch_module.BatchManager(exporter, separator, config_dir, log),
        single_manager=single_module.SingleExportManager(exporter, separator, log),
        log=log,
        get_dlc_dir=context.get("get_dlc_dir"),
        meta_db=context.get("meta_db"),
    )
    api.register(app)
    log.info("minus_mix: routes registered")
