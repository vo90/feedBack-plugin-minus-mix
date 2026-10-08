"""HTTP-route composition and host API contract tests."""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request

import batch
import exporter
import routes
import separator_client
import single


class FakeFFmpegResolver:
    """Deterministic setup seam: route tests never depend on host FFmpeg."""

    path = "C:/verified-tools/ffmpeg.exe"

    def __init__(self, config_dir, host_resolver):
        self.config_dir = config_dir
        self.host_resolver = host_resolver

    def resolve(self):
        return self.path

    def require_verified(self):
        if self.path is None:
            raise RuntimeError("No verified FFmpeg is available for MinusMix.")
        return self.path

    def public_status(self):
        available = self.path is not None
        return {
            "available": available,
            "source": "test" if available else "unavailable",
            "reason": (
                "Verified test FFmpeg is ready."
                if available else "No verified FFmpeg is available for MinusMix."
            ),
            "generation_id": "test-generation" if available else None,
        }


def _request(host: str) -> Request:
    return Request({"type": "http", "client": (host, 12345), "headers": []})


def _app(tmp_path, *, meta_db=None, ffmpeg_path="C:/verified-tools/ffmpeg.exe") -> FastAPI:
    log = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    modules = {
        "batch": batch,
        "exporter": exporter,
        "media_tools": SimpleNamespace(FFmpegResolver=FakeFFmpegResolver),
        "separator_client": separator_client,
        "single": single,
    }
    FakeFFmpegResolver.path = ffmpeg_path
    app = FastAPI()
    routes.setup(app, {
        "config_dir": str(tmp_path),
        "load_sibling": lambda name: modules.get(name) or importlib.import_module(name),
        "log": log,
        "meta_db": meta_db,
    })
    return app


def _endpoint(app: FastAPI, path: str):
    return next(
        route.endpoint for route in app.routes
        if getattr(route, "path", None) == path
    )


def test_setup_uses_supplied_available_ffmpeg_resolver_for_status(tmp_path):
    endpoint = _endpoint(_app(tmp_path), f"{routes.API}/status")

    result = endpoint()

    assert result["ffmpeg_available"] is True
    assert result["ffmpeg_source"] == "test"
    assert result["ffmpeg_reason"] == "Verified test FFmpeg is ready."


def test_batch_start_fails_closed_when_supplied_resolver_is_unavailable(tmp_path):
    endpoint = _endpoint(
        _app(tmp_path, ffmpeg_path=None), f"{routes.API}/batch/start",
    )

    with pytest.raises(HTTPException) as raised:
        endpoint(body={}, request=_request("127.0.0.1"))

    assert raised.value.status_code == 409
    assert raised.value.detail == "No verified FFmpeg is available for MinusMix."


def test_sources_filters_feedpaks_in_metadata_query(tmp_path):
    class FakeMetaDB:
        def __init__(self):
            self.calls = []

        def query_page(self, **kwargs):
            self.calls.append(kwargs)
            return ([{
                "filename": "Artist - Song.feedpak",
                "title": "Song",
                "artist": "Artist",
                "stem_ids": ["full", "guitar"],
            }], 1)

    meta_db = FakeMetaDB()
    app = _app(tmp_path, meta_db=meta_db)
    endpoint = next(
        route.endpoint for route in app.routes
        if getattr(route, "path", None) == "/api/plugins/minus_mix/sources"
    )

    result = endpoint(q="song")

    assert result["songs"][0]["filename"] == "Artist - Song.feedpak"
    assert meta_db.calls == [{
        "q": "song",
        "page": 0,
        "size": 500,
        "sort": "artist",
        "format_filter": "sloppak",
    }]


def test_loopback_guard_accepts_local_ipv4_and_ipv6_only():
    assert routes._is_loopback(_request("127.0.0.1")) is True
    assert routes._is_loopback(_request("::1")) is True
    assert routes._is_loopback(_request("192.0.2.10")) is False


def test_background_scan_route_rejects_remote_and_validates_before_start(tmp_path):
    app = _app(tmp_path)
    endpoint = next(
        route.endpoint for route in app.routes
        if getattr(route, "path", None) == "/api/plugins/minus_mix/batch/scan-jobs"
    )

    with pytest.raises(HTTPException) as remote:
        endpoint(body={}, request=_request("192.0.2.10"))
    assert remote.value.status_code == 403

    with pytest.raises(HTTPException) as invalid:
        endpoint(body={}, request=_request("127.0.0.1"))
    assert invalid.value.status_code == 400
    assert "source folder" in invalid.value.detail


def test_route_composer_registers_the_public_contract(tmp_path):
    app = _app(tmp_path)
    registered = {
        (route.path, method)
        for route in app.routes
        if route.path.startswith(routes.API)
        for method in route.methods
    }

    assert registered == {
        (f"{routes.API}/status", "GET"),
        (f"{routes.API}/sources", "GET"),
        (f"{routes.API}/source", "GET"),
        (f"{routes.API}/export", "POST"),
        (f"{routes.API}/export/latest", "GET"),
        (f"{routes.API}/export/{{job_id}}", "GET"),
        (f"{routes.API}/export/{{job_id}}/cancel", "POST"),
        (f"{routes.API}/batch/scan", "POST"),
        (f"{routes.API}/batch/scan-jobs", "POST"),
        (f"{routes.API}/batch/scan-jobs/{{scan_job_id}}", "GET"),
        (f"{routes.API}/batch/scan-jobs/{{scan_job_id}}/cancel", "POST"),
        (f"{routes.API}/batch/start", "POST"),
        (f"{routes.API}/batch/latest", "GET"),
        (f"{routes.API}/batch/{{job_id}}", "GET"),
        (f"{routes.API}/batch/{{job_id}}/cancel", "POST"),
        (f"{routes.API}/reuse/scan", "POST"),
        (f"{routes.API}/reuse/latest", "GET"),
        (f"{routes.API}/reuse/{{job_id}}", "GET"),
        (f"{routes.API}/reuse/{{job_id}}/choose", "POST"),
        (f"{routes.API}/reuse/{{job_id}}/apply", "POST"),
        (f"{routes.API}/reuse/{{job_id}}/cancel", "POST"),
    }
