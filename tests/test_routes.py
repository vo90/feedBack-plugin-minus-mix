"""HTTP-route composition and host API contract tests."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request

import batch
import exporter
import routes
import separator_client
import single


def _request(host: str) -> Request:
    return Request({"type": "http", "client": (host, 12345), "headers": []})


def _app(tmp_path, *, meta_db=None) -> FastAPI:
    log = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    modules = {
        "batch": batch,
        "exporter": exporter,
        "separator_client": separator_client,
        "single": single,
    }
    app = FastAPI()
    routes.setup(app, {
        "config_dir": str(tmp_path),
        "load_sibling": modules.__getitem__,
        "log": log,
        "meta_db": meta_db,
    })
    return app


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
    }
