"""Pinned local-server contract and bounded restart recovery regressions."""
from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

import separator_client as client_module

MODEL = "bs_roformer_sw"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, headers=None, chunks=None, text=""):
        self.status_code, self.payload = status_code, payload
        self.headers, self.chunks, self.text = headers or {}, chunks or [], text
        self.closed = False

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield from self.chunks

    def close(self):
        self.closed = True


def _log():
    return SimpleNamespace(warning=lambda *args: None)


def _write_config(root, *, port=9123, model=MODEL):
    (root / "stem_splitter.json").write_text(json.dumps({"local_server_port": port, "remote_model": model}))
    (root / "stem_splitter_server.json").write_text(json.dumps({"port": port}))


def legacy(state="skipped", **extras):
    return {"status": "ok", "warmup": {MODEL: state}, **extras}


def managed(*, model=MODEL, stems=None, verified=True, engine="audio-separator", revision="one", draining=False):
    return legacy(runtime={
        "schema_version": 1, "managed": True,
        "capabilities": ["verified_models_v1", "unrelated_future_capability"],
        "activity": {"draining": draining, "sealed": False},
        "models": {model: {"verified": verified, "stems": stems if stems is not None else list(client_module.SUPPORTED_STEMS),
                           "engine": engine, "revision": revision}},
    })


class Script:
    def __init__(self, health=None):
        self.health = health or (lambda: legacy())
        self.posts = []
        self.routes = {}
        self.calls = []
        self.uploads = []

    def get(self, url, **_kwargs):
        path = urlsplit(url).path
        self.calls.append(("GET", url))
        if path == "/health":
            value = self.health()
            if value is None:
                raise ConnectionError("offline")
            return value if isinstance(value, FakeResponse) else FakeResponse(payload=value)
        assert path in self.routes and self.routes[path], f"Unexpected GET {path}"
        return self._reply(self.routes[path].pop(0))

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        self.uploads.append((kwargs["params"].copy(), kwargs["files"]["file"][1].read()))
        assert self.posts, "Unexpected recomputation"
        return self._reply(self.posts.pop(0))

    def delete(self, url, **_kwargs):
        pytest.fail(f"The client does not own the shared cache: DELETE {url}")

    @staticmethod
    def _reply(value):
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, FakeResponse) else FakeResponse(payload=value)


@pytest.fixture
def clock(monkeypatch):
    value = [0.0]
    monkeypatch.setattr(client_module.time, "monotonic", lambda: value[0])
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: value.__setitem__(0, value[0] + seconds))
    return value


@pytest.fixture
def transport(tmp_path, monkeypatch, clock):
    _write_config(tmp_path, port=9123)
    mix = tmp_path / "mix.wav"
    mix.write_bytes(b"original input")
    script = Script()
    client = client_module.SeparationClient(tmp_path, _log(), script)
    # Unit fixtures target transport and use recognizable synthetic audio bytes;
    # production validation is covered by test_separator_http_compat.py.
    monkeypatch.setattr(client_module, "_validate_audio", lambda path, _cancel=None: path.read_bytes().startswith(b"audio:"))
    return client, script, mix, tmp_path / "work"


def complete(job="original", stems=("guitar",)):
    return {"job_id": job, "status": "complete", "stems": {stem: f"/download/{job}/{stem}.wav" for stem in stems}}


def downloaded(label="guitar"):
    return FakeResponse(chunks=[f"audio:{label}".encode()])


@pytest.mark.parametrize("health,expected,verified", [
    ({"status": "ok"}, "on_demand", False),
    (legacy(), "on_demand", False),
    (legacy("ready"), "ready", False),
    (legacy("pending"), "warming", False),
    (legacy("downloading"), "warming", False),
    (legacy("loading"), "warming", False),
    (legacy("failed: CUDA error"), "missing_model", False),
    (managed(), "ready", True),
    (managed(draining=True), "updating", True),
    (managed(verified=False), "missing_model", False),
    (managed(model="htdemucs_6s"), "missing_model", False),
    (managed(stems=["vocals", "drums"]), "unsupported_stems", True),
    (legacy(runtime={"schema_version": 27, "models": False}), "on_demand", False),
    (legacy(runtime={"schema_version": 1, "capabilities": []}), "on_demand", False),
])
def test_legacy_and_verified_contract_matrix(health, expected, verified):
    target = client_module.ServerTarget("http://127.0.0.1:9123", MODEL, None, "managed-local")
    assessed = client_module.SeparationClient._assessment(target, health, ("guitar",))
    assert assessed["state"] == expected
    assert assessed["inventory_verified"] is verified
    assert assessed["ready"] is (expected in {"ready", "on_demand"})
    assert assessed["waitable"] is (expected in {"warming", "updating"})


@pytest.mark.parametrize("path,value", [
    (("runtime",), []),
    (("runtime", "schema_version"), True),
    (("runtime", "schema_version"), "1"),
    (("runtime", "capabilities"), "verified_models_v1"),
    (("runtime", "capabilities"), [123]),
    (("runtime", "managed"), "true"),
    (("runtime", "models"), []),
    (("runtime", "models", MODEL), []),
    (("runtime", "models", MODEL, "stems"), "guitar"),
    (("runtime", "models", MODEL, "stems"), [1]),
    (("runtime", "models", MODEL, "engine"), {}),
    (("runtime", "models", MODEL, "revision"), []),
    (("runtime", "activity"), []),
    (("runtime", "activity", "draining"), "false"),
])
def test_recognized_metadata_has_strict_types(path, value):
    health = managed()
    container = health
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = value
    target = client_module.ServerTarget("http://127.0.0.1:9123", MODEL, None, "managed-local")
    assessed = client_module.SeparationClient._assessment(target, health, ("guitar",))
    assert assessed["state"] == "incompatible"
    assert not assessed["ready"] and not assessed["waitable"]


@pytest.mark.parametrize("selected,warmed,expected", [
    ("htdemucs_6s", "htdemucs_6s", "ready"),
    ("htdemucs_6s", "htdemucs", "on_demand"),
    ("htdemucs", "htdemucs_6s", "on_demand"),
])
def test_legacy_demucs_alias_only_describes_matching_model(selected, warmed, expected):
    target = client_module.ServerTarget("http://127.0.0.1:9123", selected, None, "managed-local")
    health = {"status": "ok", "demucs_model": warmed, "warmup": {"demucs": "ready"}}
    assert client_module.SeparationClient._assessment(target, health)["state"] == expected


@pytest.mark.parametrize("health", [None, legacy("loading")])
def test_explicit_server_never_falls_through_to_unrelated_default_port(tmp_path, health):
    _write_config(tmp_path, port=9123)
    script = Script(lambda: health)
    status = client_module.SeparationClient(tmp_path, _log(), script).status()
    assert status["waitable"] and not status["ready"]
    assert status["supported_stems"]  # unknown is not advertised as known empty
    assert script.calls == [("GET", "http://127.0.0.1:9123/health")]


def test_recorded_endpoint_wins_while_alive_but_conflict_is_actionable_when_offline(tmp_path):
    _write_config(tmp_path, port=9123)
    (tmp_path / "stem_splitter.json").write_text(json.dumps({"local_server_port": 9124}))
    script = Script()
    client = client_module.SeparationClient(tmp_path, _log(), script)
    assert client.status()["ready"]
    assert "configured port is 9124" in client.status()["reason"]
    client._invalidate_resolution()
    script.health = lambda: None
    status = client.status()
    assert status["state"] == "endpoint_changed" and not status["waitable"]
    assert {url for _method, url in script.calls} == {"http://127.0.0.1:9123/health"}


@pytest.mark.parametrize("code,state", [("model_not_installed", "missing_model"), ("unsupported_stems", "unsupported_stems")])
def test_model_inventory_race_in_upload_blocks_batch(transport, code, state):
    client, script, mix, out = transport
    script.health = managed
    script.posts = [FakeResponse(409, {"code": code, "error": "The selected model cannot be used"}, text="The selected model cannot be used")]
    assert client.status()["ready"]
    with pytest.raises(client_module.SeparationServiceBlocked) as raised:
        client.separate(mix, out, ("guitar",))
    assert raised.value.state == state
    assert raised.value.blocks_batch
    assert len(script.uploads) == 1


def test_ordinary_inference_failure_does_not_block_other_songs(transport):
    client, script, mix, out = transport
    script.posts = [{"job_id": "failed", "status": "failed", "error": "CUDA out of memory"}]
    with pytest.raises(client_module.SeparationUnavailable, match="CUDA out of memory") as raised:
        client.separate(mix, out, ("guitar",))
    assert not raised.value.blocks_batch
    assert len(script.uploads) == 1


def test_thirty_minute_update_recovers_without_charging_useful_work(transport, clock):
    client, script, mix, out = transport
    script.health = lambda: managed(draining=clock[0] < 1800)
    script.posts = [complete()]
    script.routes["/download/original/guitar.wav"] = [downloaded()]
    states = []
    result = client.separate(mix, out, ("guitar",), state_cb=states.append)
    assert result["guitar"].read_bytes() == b"audio:guitar"
    assert 1800 <= clock[0] < 2100
    assert states[0]["state"] == "waiting_for_server"
    assert any(state["state"] == "downloading" for state in states)
    assert len(script.uploads) == 1


def test_recovery_budget_accumulates_across_distinct_waits(monkeypatch, clock):
    target = client_module.ServerTarget("http://127.0.0.1:9123", MODEL, None, "managed-local")
    operation = client_module._Operation(target, ("guitar",))
    operation.waiting("first update")
    clock[0] = 1200
    operation.resume("separating", "working")
    clock[0] = 1260
    operation.waiting("second update")
    clock[0] = 2160
    with pytest.raises(client_module.SeparationServiceBlocked) as raised:
        operation.check()
    assert raised.value.state == "recovery_exhausted"


def test_useful_work_timeout_is_not_reset_by_recovery(clock):
    target = client_module.ServerTarget("http://127.0.0.1:9123", MODEL, None, "managed-local")
    operation = client_module._Operation(target, ("guitar",))
    clock[0] = 1800
    operation.waiting("update")
    clock[0] = 1860
    operation.resume("separating", "work")
    clock[0] = 2160
    with pytest.raises(client_module.SeparationUnavailable, match="job timed out") as raised:
        operation.check()
    assert not raised.value.blocks_batch


def test_cancel_wait_checks_at_most_quarter_second(transport, clock):
    client, script, mix, out = transport
    script.health = lambda: managed(draining=True)
    def cancel():
        if clock[0] >= 1:
            raise RuntimeError("canceled")
    with pytest.raises(RuntimeError, match="canceled"):
        client.separate(mix, out, ("guitar",), cancel_cb=cancel)
    assert 1 <= clock[0] <= 1.25
    assert not script.uploads


def test_config_change_during_wait_blocks_without_following_new_endpoint(transport, clock):
    client, script, mix, out = transport
    script.health = lambda: managed(draining=True)
    def steer(_event):
        (client.config_dir / "stem_splitter.json").write_text(json.dumps({"local_server_port": 9124}))
    with pytest.raises(client_module.SeparationServiceBlocked) as raised:
        client.separate(mix, out, ("guitar",), state_cb=steer)
    assert raised.value.state == "endpoint_changed"
    assert not script.uploads
    assert all(":9123/" in url for _method, url in script.calls)


def test_retained_opaque_job_survives_missing_current_model(transport):
    client, script, mix, out = transport
    current = [managed()]
    script.health = lambda: current[0]
    job = "opaque id:/?#"
    def interrupt():
        current[0] = managed(model="htdemucs_6s")
        return ConnectionError("restarting")
    script.posts = [{"job_id": job}]
    script.routes["/jobs/opaque%20id%3A%2F%3F%23"] = [interrupt, complete(job)]
    result_payload = complete(job)
    result_payload["stems"]["guitar"] = "/download/retained/guitar.wav"
    script.routes["/jobs/opaque%20id%3A%2F%3F%23"][-1] = result_payload
    script.routes["/download/retained/guitar.wav"] = [downloaded()]
    result = client.separate(mix, out, ("guitar",))
    assert result["guitar"].is_file()
    assert len(script.uploads) == 1
    assert len([url for _method, url in script.calls if "/jobs/" in url]) == 2


def test_confirmed_lost_job_recomputes_once_using_same_input_and_model(transport):
    client, script, mix, out = transport
    script.posts = [{"job_id": "lost"}, complete("replacement")]
    script.routes["/jobs/lost"] = [FakeResponse(404), FakeResponse(404)]
    script.routes["/download/replacement/guitar.wav"] = [downloaded()]
    result = client.separate(mix, out, ("guitar",))
    assert result["guitar"].is_file()
    assert script.uploads == [({"model": MODEL, "stems": "guitar"}, b"original input")] * 2
    assert len([url for _method, url in script.calls if "/jobs/lost" in url]) == 2


def test_second_lost_job_exhausts_shared_recomputation_cap(transport):
    client, script, mix, out = transport
    script.posts = [{"job_id": "lost1"}, {"job_id": "lost2"}]
    for job in ("lost1", "lost2"):
        script.routes[f"/jobs/{job}"] = [FakeResponse(404), FakeResponse(404)]
    with pytest.raises(client_module.SeparationServiceBlocked) as raised:
        client.separate(mix, out, ("guitar",))
    assert raised.value.state == "recovery_exhausted"
    assert len(script.uploads) == 2
    assert not list(out.iterdir())


def test_ambiguous_upload_and_missing_result_share_one_retry_cap(transport):
    client, script, mix, out = transport
    script.posts = [ConnectionError("lost response"), complete(stems=("vocals",))]
    with pytest.raises(client_module.IncompleteSeparationError) as raised:
        client.separate(mix, out, ("guitar",))
    assert raised.value.attempts == 2
    assert len(script.uploads) == 2


def test_explicit_rejected_uploads_do_not_use_recomputation_allowance(transport):
    client, script, mix, out = transport
    script.posts = [FakeResponse(503, headers={"Retry-After": "1"}) for _ in range(8)]
    script.posts += [ConnectionError("lost response"), complete()]
    script.routes["/download/original/guitar.wav"] = [downloaded()]
    result = client.separate(mix, out, ("guitar",))
    assert result["guitar"].is_file()
    assert len(script.uploads) == 10


def test_recompute_rechecks_selected_model_but_does_not_substitute(transport):
    client, script, mix, out = transport
    current = [managed()]
    script.health = lambda: current[0]
    def missing():
        current[0] = managed(model="htdemucs_6s")
        return complete(stems=("vocals",))
    script.posts = [missing]
    with pytest.raises(client_module.SeparationServiceBlocked) as raised:
        client.separate(mix, out, ("guitar",))
    assert raised.value.state == "missing_model"
    assert len(script.uploads) == 1


def test_recompute_rejects_changed_engine_contract(transport):
    client, script, mix, out = transport
    current = [managed()]
    script.health = lambda: current[0]
    def missing():
        current[0] = managed(engine="different-engine")
        return complete(stems=("vocals",))
    script.posts = [missing]
    with pytest.raises(client_module.SeparationServiceBlocked, match="engine contract changed"):
        client.separate(mix, out, ("guitar",))
    assert len(script.uploads) == 1


def test_partial_download_restarts_at_byte_zero(transport):
    client, script, mix, out = transport
    class Interrupted(FakeResponse):
        def iter_content(self, chunk_size):
            yield b"wrong partial bytes"
            raise ConnectionError("stream disconnected")
    script.posts = [complete()]
    script.routes["/download/original/guitar.wav"] = [Interrupted(), downloaded()]
    result = client.separate(mix, out, ("guitar",))
    assert result["guitar"].read_bytes() == b"audio:guitar"
    assert not list(out.rglob("*.part"))
    assert len(script.uploads) == 1


def test_recomputation_discards_all_old_stems_never_mixes_attempts(transport):
    client, script, mix, out = transport
    script.posts = [complete("first", ("guitar", "bass")), complete("second", ("guitar", "bass"))]
    script.routes["/download/first/guitar.wav"] = [downloaded("first guitar")]
    script.routes["/download/first/bass.wav"] = [FakeResponse(chunks=[b"not audio"])]
    script.routes["/download/second/guitar.wav"] = [downloaded("second guitar")]
    script.routes["/download/second/bass.wav"] = [downloaded("second bass")]
    result = client.separate(mix, out, ("guitar", "bass"))
    assert result["guitar"].read_bytes() == b"audio:second guitar"
    assert result["bass"].read_bytes() == b"audio:second bass"
    assert len(list(out.iterdir())) == 1
    assert not list(out.rglob("*.part"))


def test_changed_input_cannot_be_resubmitted(transport):
    client, script, mix, out = transport
    def interrupted():
        original = mix.stat()
        mix.write_bytes(b"changed source")  # deliberately the same byte count
        os.utime(mix, ns=(original.st_atime_ns, original.st_mtime_ns))
        return ConnectionError("lost response")
    script.posts = [interrupted]
    with pytest.raises(client_module.SeparationUnavailable, match="input changed"):
        client.separate(mix, out, ("guitar",))
    assert len(script.uploads) == 1


def test_model_setting_change_does_not_change_original_request(transport):
    client, script, mix, out = transport
    def incomplete():
        _write_config(client.config_dir, port=9123, model="htdemucs_6s")
        return complete(stems=("vocals",))
    script.posts = [incomplete, complete()]
    script.routes["/download/original/guitar.wav"] = [downloaded()]
    client.separate(mix, out, ("guitar",))
    assert [params["model"] for params, _audio in script.uploads] == [MODEL, MODEL]


def test_download_off_origin_redirect_stops_without_contacting_other_server(transport):
    client, script, mix, out = transport
    script.posts = [complete()]
    script.routes["/download/original/guitar.wav"] = [FakeResponse(302, headers={"Location": "http://127.0.0.1:7865/other.wav"})]
    with pytest.raises(client_module.SeparationServiceBlocked, match="another origin"):
        client.separate(mix, out, ("guitar",))
    assert all(":9123/" in url for _method, url in script.calls)
    assert not list(out.iterdir())


def test_retry_after_is_bounded_and_accepts_http_dates(monkeypatch):
    monkeypatch.setattr(client_module.time, "time", lambda: 0)
    for raw, expected in [("2", 2), ("-4", 0), ("999999", 60), ("NaN", 0), ("invalid", 0),
                          ("Thu, 01 Jan 1970 00:00:30 GMT", 30)]:
        assert client_module.SeparationClient._retry_after(FakeResponse(headers={"Retry-After": raw})) == expected


def test_verified_inventory_includes_precise_stems_not_all_defaults(transport):
    client, script, _mix, _out = transport
    script.health = lambda: managed(stems=["guitar", "vocals"])
    status = client.status()
    assert status["supported_stems"] == ["guitar", "vocals"]
    assert status["inventory_verified"]
    original = copy.deepcopy(status)
    status["supported_stems"].append("drums")
    assert client.status() == original


@pytest.mark.parametrize("job", [".", ".."])
def test_dot_segment_job_ids_cannot_escape_job_endpoint(transport, job):
    client, script, mix, out = transport
    script.posts = [{"job_id": job}]
    with pytest.raises(client_module.SeparationServiceBlocked, match="valid job ID"):
        client.separate(mix, out, ("guitar",))
    assert all(url.endswith(("/health", "/separate")) for _method, url in script.calls)


def test_cancel_during_successful_stream_handoff_closes_response(transport):
    client, script, mix, out = transport
    response = downloaded()
    script.posts = [complete()]
    script.routes["/download/original/guitar.wav"] = [response]
    def cancel_on_response(state):
        if state["state"] == "downloading" and state["detail"].startswith("Resuming"):
            raise RuntimeError("canceled")
    with pytest.raises(RuntimeError, match="canceled"):
        client.separate(mix, out, ("guitar",), state_cb=cancel_on_response)
    assert response.closed
    assert not list(out.iterdir())


def test_endpoint_change_during_healthy_poll_blocks_before_next_get(transport):
    client, script, mix, out = transport
    script.posts = [{"job_id": "job"}]
    def still_running():
        _write_config(client.config_dir, port=9124)
        return {"job_id": "job", "status": "processing"}
    script.routes["/jobs/job"] = [still_running]
    with pytest.raises(client_module.SeparationServiceBlocked) as raised:
        client.separate(mix, out, ("guitar",))
    assert raised.value.state == "endpoint_changed"
    assert len([url for _method, url in script.calls if "/jobs/" in url]) == 1
    assert all(":9123/" in url for _method, url in script.calls)
