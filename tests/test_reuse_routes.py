"""Reuse boundaries reject remote writes and arbitrate all export modes."""
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import reuse_routes
import routes


class Refused(ValueError):
    pass


class Manager:
    def __init__(self):
        self.calls = []
        self.active = False

    def is_active(self):
        return self.active

    def get(self, job_id, **options):
        self.calls.append(("get", job_id, options))
        return None if job_id == "missing" else {"id": job_id, "status": "ready", **options}

    def latest(self, **options):
        return self.get("latest-job", **options)

    def start_scan(self, **options):
        self.calls.append(("scan", options))
        return {"id": "new-job", "status": "scanning"}

    def choose(self, job_id, choices):
        self.calls.append(("choose", job_id, choices))
        if "unknown" in choices:
            raise Refused("Choose a version from the reviewed candidate list")
        return {"id": job_id, "status": "ready"}

    def apply(self, job_id):
        self.calls.append(("apply", job_id))
        return {"id": job_id, "status": "running"}

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        return {"id": job_id, "status": "canceling"}


def app_and_manager():
    manager = Manager()
    inactive = SimpleNamespace(is_active=lambda: False)
    api = SimpleNamespace(_require_loopback=routes.MinusMixAPI._require_loopback,
                          single_manager=inactive, batch_manager=inactive,
                          operation_start_lock=threading.RLock())
    app = FastAPI()
    reuse_routes.register(app, api=api, manager=manager, error_type=Refused)
    return app, manager, api


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/reuse/latest", None), ("GET", "/reuse/job", None),
    ("POST", "/reuse/scan", {}), ("POST", "/reuse/job/choose", {}),
    ("POST", "/reuse/job/apply", {}), ("POST", "/reuse/job/cancel", {}),
])
def test_every_reuse_endpoint_requires_loopback(method, path, payload):
    app, manager, _ = app_and_manager()
    with TestClient(app, client=("192.0.2.1", 1000)) as client:
        response = client.request(method, routes.API + path, json=payload)
    assert response.status_code == 403 and not manager.calls


def test_scan_choices_and_apply_are_separate_explicit_actions():
    app, manager, _ = app_and_manager()
    with TestClient(app, client=("::1", 1000)) as client:
        payload = {"old_dir": "C:/old", "fresh_dir": "C:/fresh", "output_dir": "C:/new", "workers": 16}
        assert client.post(routes.API + "/reuse/scan", json=payload).status_code == 202
        assert manager.calls == [("scan", payload)]
        assert client.post(routes.API + "/reuse/new-job/choose", json={"choices": {"group": "folder/source.feedpak"}}).status_code == 200
        assert all(call[0] != "apply" for call in manager.calls)
        assert client.post(routes.API + "/reuse/new-job/apply").status_code == 202
        assert manager.calls[-1] == ("apply", "new-job")
        assert client.post(routes.API + "/reuse/new-job/cancel").status_code == 202


@pytest.mark.parametrize("workers", [True, 0, 17, "16", None, 2.5, "unlimited"])
def test_worker_values_are_validated_before_scheduling(workers):
    app, manager, _ = app_and_manager()
    with TestClient(app, client=("127.0.0.1", 1000)) as client:
        response = client.post(routes.API + "/reuse/scan", json={
            "old_dir": "C:/old", "fresh_dir": "C:/fresh", "output_dir": "C:/new", "workers": workers})
    assert response.status_code == 400 and not manager.calls


def test_pagination_is_bounded_and_latest_route_has_priority():
    app, manager, _ = app_and_manager()
    with TestClient(app, client=("127.0.0.1", 1000)) as client:
        response = client.get(routes.API + "/reuse/latest?offset=100&limit=100")
        assert response.json()["job"]["id"] == "latest-job"
        assert manager.calls[-1] == ("get", "latest-job", {"offset": 100, "limit": 100})
        assert client.get(routes.API + "/reuse/job?limit=101").status_code == 422
        assert client.get(routes.API + "/reuse/job?offset=-1").status_code == 422
        assert client.get(routes.API + "/reuse/missing").status_code == 404


@pytest.mark.parametrize("active_mode", ["single_manager", "batch_manager"])
def test_reuse_admission_cannot_overlap_an_ordinary_export(active_mode):
    app, manager, api = app_and_manager()
    setattr(api, active_mode, SimpleNamespace(is_active=lambda: True))
    with TestClient(app, client=("127.0.0.1", 1000)) as client:
        assert client.post(routes.API + "/reuse/scan", json={
            "old_dir": "C:/old", "fresh_dir": "C:/fresh", "output_dir": "C:/new"}).status_code == 409
        assert client.post(routes.API + "/reuse/job/apply").status_code == 409
    assert not manager.calls


def test_backend_choice_refusal_is_actionable_and_does_not_start_apply():
    app, manager, _ = app_and_manager()
    with TestClient(app, client=("127.0.0.1", 1000)) as client:
        result = client.post(routes.API + "/reuse/job/choose", json={"choices": {"unknown": "unreviewed.feedpak"}})
    assert result.status_code == 409 and "reviewed candidate" in result.json()["detail"]
    assert all(call[0] != "apply" for call in manager.calls)


def test_ordinary_starts_reject_an_active_reuse_job(tmp_path):
    from tests.test_routes import _app, _request
    app = _app(tmp_path)
    api = next(route.endpoint.__self__ for route in app.routes
               if getattr(route, "path", None) == routes.API + "/export")
    api.reuse_manager.is_active = lambda: True
    api._resolve_source = lambda _name: tmp_path / "input.feedpak"
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as single:
        api.export({"filename": "input.feedpak", "output_dir": str(tmp_path), "excluded_stems": ["guitar"]}, _request("127.0.0.1"))
    assert single.value.status_code == 409
    with pytest.raises(HTTPException) as batch:
        api.batch_start({}, _request("127.0.0.1"))
    assert batch.value.status_code == 409
