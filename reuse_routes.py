"""Loopback-only transport for reviewed existing-audio reuse jobs."""
from __future__ import annotations

from fastapi import HTTPException, Query, Request


def register(app, *, api, manager, error_type):
    prefix = api.API if hasattr(api, "API") else "/api/plugins/minus_mix"

    def local(request):
        api._require_loopback(request, "Existing-audio reuse is only available on this computer")

    def invoke(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except error_type as exc:
            raise HTTPException(409, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, "The app cannot access one of the selected folders") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    def idle_exports():
        if api.single_manager.is_active() or api.batch_manager.is_active():
            raise HTTPException(409, "Wait for the active MinusMix export to finish or cancel it first")

    @app.post(prefix + "/reuse/scan", status_code=202)
    def reuse_scan(body: dict, request: Request):
        local(request)
        folders = {}
        for field in ("old_dir", "fresh_dir", "output_dir"):
            value = body.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\0" in value:
                raise HTTPException(400, "Choose all three folders before scanning")
            folders[field] = value.strip()
        workers = body.get("workers", "auto")
        if workers != "auto" and (type(workers) is not int or not 1 <= workers <= 16):
            raise HTTPException(400, "Workers must be Auto or a whole number from 1 to 16")
        with api.operation_start_lock:
            idle_exports()
            return invoke(manager.start_scan, **folders, workers=workers)

    @app.get(prefix + "/reuse/latest")
    def reuse_latest(request: Request, offset: int = Query(0, ge=0, le=10000),
                     limit: int = Query(100, ge=1, le=100)):
        local(request)
        return {"job": invoke(manager.latest, offset=offset, limit=limit)}

    @app.get(prefix + "/reuse/{job_id}")
    def reuse_status(job_id: str, request: Request, offset: int = Query(0, ge=0, le=10000),
                     limit: int = Query(100, ge=1, le=100)):
        local(request)
        job = invoke(manager.get, job_id, offset=offset, limit=limit)
        if job is None:
            raise HTTPException(404, "Reuse job not found")
        return job

    @app.post(prefix + "/reuse/{job_id}/choose")
    def reuse_choose(job_id: str, body: dict, request: Request):
        local(request)
        choices = body.get("choices")
        if (not isinstance(choices, dict) or not 1 <= len(choices) <= 10000
                or any(not isinstance(key, str) or not key or len(key) > 4096
                       or not isinstance(value, str) or not value or len(value) > 4096
                       for key, value in choices.items())):
            raise HTTPException(400, "Choose a listed audio version or skip its group")
        return invoke(manager.choose, job_id, choices)

    @app.post(prefix + "/reuse/{job_id}/apply", status_code=202)
    def reuse_apply(job_id: str, request: Request):
        local(request)
        with api.operation_start_lock:
            idle_exports()
            return invoke(manager.apply, job_id)

    @app.post(prefix + "/reuse/{job_id}/cancel", status_code=202)
    def reuse_cancel(job_id: str, request: Request):
        local(request)
        return invoke(manager.cancel, job_id)
