"""Backend API for the Practice Mix Exporter plugin."""
from __future__ import annotations

import ipaddress
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request


PLUGIN_ID = "practice_mix_exporter"
API = f"/api/plugins/{PLUGIN_ID}"
MAX_SOURCE_RESULTS = 250
DEFAULT_TARGETS = ("guitar", "bass", "drums", "vocals", "piano", "other")


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


def setup(app: FastAPI, context: dict) -> None:
    exporter = context["load_sibling"]("exporter")
    batch_module = context["load_sibling"]("batch")
    single_module = context["load_sibling"]("single")
    separator_module = context["load_sibling"]("separator_client")
    log = context["log"]
    get_dlc_dir = context.get("get_dlc_dir")
    meta_db = context.get("meta_db")

    separator = separator_module.SeparationClient(Path(context["config_dir"]), log)

    batch_manager = batch_module.BatchManager(
        exporter, separator, Path(context["config_dir"]), log,
    )
    single_manager = single_module.SingleExportManager(exporter, separator, log)
    operation_start_lock = threading.RLock()

    def batch_options(body: dict) -> dict:
        if not isinstance(body, dict):
            raise HTTPException(400, "invalid batch request")
        return {
            "input_dir": body.get("input_dir"),
            "output_dir": body.get("output_dir"),
            "excluded_stems": body.get("excluded_stems"),
            "recursive": body.get("recursive", True) is not False,
            "skip_existing": body.get("skip_existing", True) is not False,
            "skip_derived": body.get("skip_derived", True) is not False,
        }

    def public_batch(payload: dict | None, *, scan: bool = False) -> dict | None:
        if payload is None:
            return None
        result = dict(payload)
        items = list(payload.get("items") or [])
        result["items_total"] = len(items)
        limit = 400
        if len(items) > limit:
            if scan:
                visible = items[:limit]
            else:
                important = [item for item in items if item.get("status") in ("running", "failed")]
                visible = important + items[-limit:]
                deduped: list[dict] = []
                seen: set[str] = set()
                for item in visible:
                    key = str(item.get("relative_path") or "")
                    if key in seen:
                        continue
                    seen.add(key)
                    deduped.append(item)
                visible = deduped[-limit:]
            result["items"] = visible
            result["items_truncated"] = True
        else:
            result["items"] = items
            result["items_truncated"] = False
        return result

    def resolve_source(filename: str) -> Path:
        if not isinstance(filename, str) or not filename.strip():
            raise HTTPException(400, "choose a source feedpak")
        library = get_dlc_dir() if get_dlc_dir else None
        if not library:
            raise HTTPException(409, "configure a local song library first")
        from safepath import safe_join
        target = safe_join(Path(library).resolve(), filename)
        if target is None or not Path(target).exists():
            raise HTTPException(404, "the source feedpak is no longer in the library")
        return Path(target)

    @app.get(f"{API}/status")
    def status():
        from audio import _ffmpeg_cmd
        engine = separator.status()
        return {"ok": True, "ffmpeg_available": bool(_ffmpeg_cmd()), "separation": engine}

    @app.get(f"{API}/sources")
    def sources(q: str = ""):
        if meta_db is None:
            return {"songs": []}
        found: list[dict] = []
        page = 0
        try:
            while len(found) < MAX_SOURCE_RESULTS and page < 2000:
                songs, total = meta_db.query_page(q=q, page=page, size=500, sort="artist")
                for song in songs:
                    filename = song.get("filename")
                    if not isinstance(filename, str) or Path(filename).suffix.lower() not in (".feedpak", ".sloppak"):
                        continue
                    stem_ids = [str(s).lower() for s in (song.get("stem_ids") or [])]
                    available = [s for s in stem_ids if s != "full"]
                    found.append({
                        "filename": filename,
                        "title": song.get("title") or "Untitled",
                        "artist": song.get("artist") or "",
                        "stem_ids": available,
                        "already_split": bool(available),
                    })
                    if len(found) >= MAX_SOURCE_RESULTS:
                        break
                if not songs or len(songs) < 500 or (isinstance(total, int) and (page + 1) * 500 >= total):
                    break
                page += 1
        except Exception:
            log.exception("practice_mix_exporter: could not query feedpak sources")
            raise HTTPException(500, "could not read the local song index")
        return {"songs": found, "truncated": len(found) >= MAX_SOURCE_RESULTS}

    @app.get(f"{API}/source")
    def source(filename: str):
        try:
            info = exporter.inspect_source(resolve_source(filename))
        except HTTPException:
            raise
        except exporter.ExportError as exc:
            raise HTTPException(400, str(exc)) from exc
        saved = {s.id: s for s in info.stems if s.id != "full"}
        supported = list(getattr(separator_module, "SUPPORTED_STEMS", DEFAULT_TARGETS))
        target_ids = list(dict.fromkeys([*supported, *saved.keys()]))
        return {
            "filename": filename,
            "title": info.title,
            "artist": info.artist,
            "stems": [{
                "id": stem_id,
                "file": saved[stem_id].file if stem_id in saved else None,
                "label": exporter.stem_label(stem_id),
                "saved": stem_id in saved,
                "requires_separation": stem_id not in saved,
            } for stem_id in target_ids],
            "already_split": bool(saved),
            "arrangements": list(info.arrangements),
            "derived_exclusions": list(info.derived_exclusions),
        }

    @app.post(f"{API}/export")
    def export(body: dict, request: Request):
        # This endpoint accepts an absolute path selected by the native desktop
        # dialog. Never expose that write primitive to clients connected through
        # optional LAN mode.
        if not _is_loopback(request):
            raise HTTPException(403, "practice-mix export is only available on this computer")
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

        source_path = resolve_source(body.get("filename"))
        try:
            with operation_start_lock:
                if batch_manager.is_active():
                    raise single_module.SingleExportError(
                        "wait for the active practice-mix batch to finish or cancel it first"
                    )
                return single_manager.start(source_path, output_dir, excluded)
        except (exporter.ExportError, single_module.SingleExportError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, "the app cannot write to the chosen output folder") from exc
        except Exception as exc:
            log.exception("practice_mix_exporter: could not start single export")
            raise HTTPException(500, "the practice feedpak job could not be started") from exc

    @app.get(f"{API}/export/latest")
    def export_latest(request: Request):
        if not _is_loopback(request):
            raise HTTPException(403, "single-song export status is only available on this computer")
        return {"job": single_manager.latest()}

    @app.get(f"{API}/export/{{job_id}}")
    def export_status(job_id: str, request: Request):
        if not _is_loopback(request):
            raise HTTPException(403, "single-song export status is only available on this computer")
        job = single_manager.get(job_id)
        if job is None:
            raise HTTPException(404, "single-song export job not found")
        return job

    @app.post(f"{API}/export/{{job_id}}/cancel")
    def export_cancel(job_id: str, request: Request):
        if not _is_loopback(request):
            raise HTTPException(403, "single-song export cancellation is only available on this computer")
        try:
            return single_manager.cancel(job_id)
        except single_module.SingleExportError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post(f"{API}/batch/scan")
    def batch_scan(body: dict, request: Request):
        if not _is_loopback(request):
            raise HTTPException(403, "batch scanning is only available on this computer")
        try:
            return public_batch(batch_manager.scan(**batch_options(body)), scan=True)
        except batch_module.BatchError as exc:
            raise HTTPException(400, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, "the app cannot read one of the selected folders") from exc
        except OSError as exc:
            log.exception("practice_mix_exporter: batch scan failed")
            raise HTTPException(500, "the selected folders could not be scanned") from exc

    @app.post(f"{API}/batch/start")
    def batch_start(body: dict, request: Request):
        if not _is_loopback(request):
            raise HTTPException(403, "batch conversion is only available on this computer")
        try:
            with operation_start_lock:
                if single_manager.is_active():
                    raise batch_module.BatchError(
                        "wait for the active single-song export to finish or cancel it first"
                    )
                return public_batch(batch_manager.start(**batch_options(body)))
        except batch_module.BatchError as exc:
            status_code = 409 if "already running" in str(exc) or "no new feedpaks" in str(exc) else 400
            raise HTTPException(status_code, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, "the app cannot access one of the selected folders") from exc

    @app.get(f"{API}/batch/latest")
    def batch_latest(request: Request):
        if not _is_loopback(request):
            raise HTTPException(403, "batch status is only available on this computer")
        return {"job": public_batch(batch_manager.latest())}

    @app.get(f"{API}/batch/{{job_id}}")
    def batch_status(job_id: str, request: Request):
        if not _is_loopback(request):
            raise HTTPException(403, "batch status is only available on this computer")
        job = batch_manager.get(job_id)
        if job is None:
            raise HTTPException(404, "batch job not found")
        return public_batch(job)

    @app.post(f"{API}/batch/{{job_id}}/cancel")
    def batch_cancel(job_id: str, request: Request):
        if not _is_loopback(request):
            raise HTTPException(403, "batch cancellation is only available on this computer")
        try:
            return public_batch(batch_manager.cancel(job_id))
        except batch_module.BatchError as exc:
            raise HTTPException(404, str(exc)) from exc

    log.info("practice_mix_exporter: routes registered")
