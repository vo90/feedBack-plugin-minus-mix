"""Standalone MinusMix server-client compatibility tests."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import separator_client


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, text="", headers=None, chunks=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self._chunks = list(chunks or [])
        self.closed = False

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    def iter_content(self, chunk_size):
        assert chunk_size == 1024 * 1024
        yield from self._chunks

    def close(self):
        self.closed = True


class FakeRequests:
    def __init__(self):
        self.calls = []
        self.get_responses = []
        self.post_responses = []
        self.delete_responses = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        assert self.get_responses, f"unexpected GET {url}"
        response = self.get_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        assert self.post_responses, f"unexpected POST {url}"
        response = self.post_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        assert self.delete_responses, f"unexpected DELETE {url}"
        response = self.delete_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _log():
    return SimpleNamespace(warning=lambda *args, **kwargs: None)


def _write_config(root: Path, *, port=7865, model="bs_roformer_sw", live_state=True):
    (root / "stem_splitter.json").write_text(json.dumps({
        "local_server_port": port,
        "remote_model": model,
    }), encoding="utf-8")
    if live_state:
        (root / "stem_splitter_server.json").write_text(json.dumps({
            "pid": 1234,
            "port": port,
        }), encoding="utf-8")


def test_status_discovers_current_stem_splitter_state_without_importing_plugin(tmp_path):
    _write_config(tmp_path, port=9123)
    requests = FakeRequests()
    health = FakeResponse(payload={
        "status": "ok",
        "device": "cuda",
        "gpu": True,
        "warmup": {"bs_roformer_sw": "ready"},
    })
    requests.get_responses.append(health)

    status = separator_client.SeparationClient(tmp_path, _log(), requests).status()

    assert status["ready"] is True
    assert status["source"] == "managed-local"
    assert status["device"] == "cuda"
    assert status["supported_stems"] == list(separator_client.SUPPORTED_STEMS)
    assert requests.calls[0][1] == "http://127.0.0.1:9123/health"
    assert health.closed is True


def test_ready_server_resolution_is_reused_briefly(tmp_path):
    _write_config(tmp_path)
    requests = FakeRequests()
    requests.get_responses.append(FakeResponse(payload={
        "status": "ok",
        "warmup": {"bs_roformer_sw": "ready"},
    }))
    client = separator_client.SeparationClient(tmp_path, _log(), requests)

    first = client.status()
    second = client.status()

    assert first["ready"] is True
    assert second["ready"] is True
    assert len(requests.calls) == 1


def test_configured_managed_local_port_works_without_live_state(tmp_path):
    _write_config(tmp_path, port=9124, live_state=False)
    requests = FakeRequests()
    requests.get_responses.append(FakeResponse(payload={
        "status": "ok",
        "warmup": {"bs_roformer_sw": "ready"},
    }))

    status = separator_client.SeparationClient(tmp_path, _log(), requests).status()

    assert status["ready"] is True
    assert requests.calls[0][1] == "http://127.0.0.1:9124/health"


def test_status_explains_model_download_in_progress(tmp_path):
    _write_config(tmp_path)
    requests = FakeRequests()
    requests.get_responses.append(FakeResponse(payload={
        "status": "ok",
        "warmup": {"bs_roformer_sw": "downloading"},
    }))

    status = separator_client.SeparationClient(tmp_path, _log(), requests).status()

    assert status["ready"] is False
    assert "downloading" in status["reason"]
    assert "finish downloading models" in status["reason"]


def test_status_ignores_configured_remote_when_local_server_is_not_ready(tmp_path):
    _write_config(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "demucs_server_url": "https://split.example.test",
    }), encoding="utf-8")
    requests = FakeRequests()
    local_health = FakeResponse(payload={
        "status": "ok",
        "warmup": {"bs_roformer_sw": "downloading"},
    })
    requests.get_responses.append(local_health)

    status = separator_client.SeparationClient(tmp_path, _log(), requests).status()

    assert status["ready"] is False
    assert status["source"] is None
    assert "downloading" in status["reason"]
    assert [call[1] for call in requests.calls] == [
        "http://127.0.0.1:7865/health",
    ]
    assert local_health.closed is True


def test_status_without_server_is_actionable(tmp_path):
    requests = FakeRequests()
    requests.get_responses.append(FakeResponse(status_code=503, payload={}))

    status = separator_client.SeparationClient(tmp_path, _log(), requests).status()

    assert status["ready"] is False
    assert "managed local server is not running" in status["reason"]


def test_direct_cached_response_streams_only_requested_stem_and_cleans_cache(tmp_path):
    _write_config(tmp_path)
    requests = FakeRequests()
    health = FakeResponse(payload={"status": "ok", "warmup": {"bs_roformer_sw": "ready"}})
    accepted = FakeResponse(payload={
        "job_id": "abc-123",
        "stems": {
            "guitar": "/download/abc-123/guitar.flac",
            "vocals": "/download/abc-123/vocals.flac",
        },
        "cached": True,
    })
    downloaded = FakeResponse(chunks=[b"guitar-", b"audio"])
    cleaned = FakeResponse(payload={"ok": True})
    requests.get_responses.extend([health, downloaded])
    requests.post_responses.append(accepted)
    requests.delete_responses.append(cleaned)
    mix = tmp_path / "full.ogg"
    mix.write_bytes(b"mix")

    result = separator_client.SeparationClient(tmp_path, _log(), requests).separate(
        mix, tmp_path / "work", ("guitar",),
    )

    assert set(result) == {"guitar"}
    assert result["guitar"].read_bytes() == b"guitar-audio"
    assert not any("vocals.flac" in call[1] for call in requests.calls)
    assert any(call[0] == "DELETE" and call[1].endswith("/cache/abc-123")
               for call in requests.calls)
    assert all(response.closed for response in (health, accepted, downloaded, cleaned))


def test_async_current_nightly_job_is_polled_and_progress_is_reported(tmp_path, monkeypatch):
    _write_config(tmp_path)
    monkeypatch.setattr(
        separator_client, "_interruptible_wait",
        lambda _seconds, cancel_cb: cancel_cb() if cancel_cb else None,
    )
    requests = FakeRequests()
    requests.get_responses.extend([
        FakeResponse(payload={"status": "ok", "warmup": {"bs_roformer_sw": "ready"}}),
        FakeResponse(payload={"status": "processing", "progress": 20}),
        FakeResponse(payload={
            "status": "complete", "progress": 100,
            "stems": {"guitar": "/download/job-9/guitar.flac"},
        }),
        FakeResponse(chunks=[b"stem"]),
    ])
    requests.post_responses.append(FakeResponse(payload={
        "job_id": "job-9", "status": "processing",
    }))
    requests.delete_responses.append(FakeResponse(payload={"ok": True}))
    mix = tmp_path / "full.flac"
    mix.write_bytes(b"mix")
    progress = []

    result = separator_client.SeparationClient(tmp_path, _log(), requests).separate(
        mix, tmp_path / "work", ("guitar",),
        progress_cb=lambda value, message: progress.append((value, message)),
    )

    assert result["guitar"].read_bytes() == b"stem"
    assert any("20%" in message for _value, message in progress)
    assert requests.calls[1][0] == "POST"
    assert requests.calls[1][2]["params"]["stems"] == "guitar"


def test_remote_url_and_api_key_are_never_used_for_separation(tmp_path):
    _write_config(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "demucs_server_url": "https://split.example.test",
        "demucs_api_key": "secret-value",
    }), encoding="utf-8")
    requests = FakeRequests()
    requests.get_responses.extend([
        FakeResponse(payload={"status": "ok", "warmup": {"bs_roformer_sw": "ready"}}),
        FakeResponse(chunks=[b"stem"]),
    ])
    requests.post_responses.append(FakeResponse(payload={
        "job_id": "job-safe",
        "stems": {"guitar": "/download/job-safe/guitar.flac"},
    }))
    requests.delete_responses.append(FakeResponse(payload={"ok": True}))
    mix = tmp_path / "full.ogg"
    mix.write_bytes(b"mix")

    result = separator_client.SeparationClient(tmp_path, _log(), requests).separate(
        mix, tmp_path / "work", ("guitar",),
    )

    assert result["guitar"].read_bytes() == b"stem"
    assert all(call[1].startswith("http://127.0.0.1:7865/") for call in requests.calls)
    assert all(call[2].get("headers") is None for call in requests.calls)


def test_connection_loss_reports_managed_local_server_action(tmp_path):
    _write_config(tmp_path)
    requests = FakeRequests()
    requests.get_responses.append(FakeResponse(payload={
        "status": "ok", "warmup": {"bs_roformer_sw": "ready"},
    }))
    requests.post_responses.append(ConnectionError("offline"))
    mix = tmp_path / "full.ogg"
    mix.write_bytes(b"mix")

    with pytest.raises(
        separator_client.SeparationUnavailable,
        match="managed local server; start or restart it",
    ):
        separator_client.SeparationClient(tmp_path, _log(), requests).separate(
            mix, tmp_path / "work", ("guitar",),
        )


def test_busy_server_retries_then_returns_its_error(tmp_path, monkeypatch):
    _write_config(tmp_path)
    monkeypatch.setattr(separator_client, "_interruptible_wait", lambda *_args: None)
    requests = FakeRequests()
    requests.get_responses.append(FakeResponse(payload={
        "status": "ok", "warmup": {"bs_roformer_sw": "ready"},
    }))
    requests.post_responses.extend([
        FakeResponse(status_code=503, text="GPU queue full")
        for _attempt in range(separator_client.BUSY_RETRIES)
    ])
    mix = tmp_path / "full.ogg"
    mix.write_bytes(b"mix")
    progress = []

    with pytest.raises(
        separator_client.SeparationUnavailable,
        match=r"split server error \(503\): GPU queue full",
    ):
        separator_client.SeparationClient(tmp_path, _log(), requests).separate(
            mix, tmp_path / "work", ("guitar",),
            progress_cb=lambda _value, message: progress.append(message),
        )

    assert sum(call[0] == "POST" for call in requests.calls) == separator_client.BUSY_RETRIES
    assert any("managed local server is busy" in message for message in progress)


def test_non_json_submit_response_is_actionable(tmp_path):
    _write_config(tmp_path)
    requests = FakeRequests()
    requests.get_responses.append(FakeResponse(payload={
        "status": "ok", "warmup": {"bs_roformer_sw": "ready"},
    }))
    requests.post_responses.append(FakeResponse(
        payload=ValueError("not json"), text="upstream proxy error",
    ))
    mix = tmp_path / "full.ogg"
    mix.write_bytes(b"mix")

    with pytest.raises(
        separator_client.SeparationUnavailable,
        match="non-JSON response: upstream proxy error",
    ):
        separator_client.SeparationClient(tmp_path, _log(), requests).separate(
            mix, tmp_path / "work", ("guitar",),
        )


def test_missing_requested_stem_is_reported_and_server_cache_is_cleaned(tmp_path):
    _write_config(tmp_path)
    requests = FakeRequests()
    requests.get_responses.extend([
        FakeResponse(payload={
            "status": "ok", "warmup": {"bs_roformer_sw": "ready"},
        }),
        FakeResponse(chunks=[b"vocal stem"]),
    ])
    requests.post_responses.append(FakeResponse(payload={
        "job_id": "job-missing",
        "stems": {"vocals": "/download/job-missing/vocals.flac"},
    }))
    requests.delete_responses.append(FakeResponse(payload={"ok": True}))
    mix = tmp_path / "full.ogg"
    mix.write_bytes(b"mix")

    with pytest.raises(
        separator_client.SeparationUnavailable,
        match="the selected model did not produce: guitar",
    ):
        separator_client.SeparationClient(tmp_path, _log(), requests).separate(
            mix, tmp_path / "work", ("guitar",),
        )

    assert any(
        call[0] == "DELETE" and call[1].endswith("/cache/job-missing")
        for call in requests.calls
    )


def test_failed_and_timed_out_jobs_report_clear_errors(tmp_path, monkeypatch):
    target = separator_client.ServerTarget(
        "http://127.0.0.1:7865", "bs_roformer_sw", None, "managed-local",
    )
    requests = FakeRequests()
    requests.get_responses.append(FakeResponse(payload={
        "status": "failed", "error": "GPU out of memory",
    }))
    client = separator_client.SeparationClient(tmp_path, _log(), requests)
    monkeypatch.setattr(separator_client, "_interruptible_wait", lambda *_args: None)

    with pytest.raises(
        separator_client.SeparationUnavailable,
        match="split server job failed: GPU out of memory",
    ):
        client._poll_job(target, {"job_id": "failed-job"}, None, None)

    monkeypatch.setattr(separator_client, "JOB_TIMEOUT_SECONDS", 0)
    with pytest.raises(
        separator_client.SeparationUnavailable,
        match="split server job timed out after 0 minutes",
    ):
        client._poll_job(target, {"job_id": "slow-job"}, None, None)


def test_canceled_stream_removes_partial_output(tmp_path):
    _write_config(tmp_path)

    class CancelAfterFirstChunk:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            if self.calls >= 5:
                raise RuntimeError("canceled")

    requests = FakeRequests()
    requests.get_responses.extend([
        FakeResponse(payload={"status": "ok", "warmup": {"bs_roformer_sw": "ready"}}),
        FakeResponse(chunks=[b"partial", b"more"]),
    ])
    requests.post_responses.append(FakeResponse(payload={
        "job_id": "job-cancel", "stems": {"guitar": "/download/job-cancel/guitar.flac"},
    }))
    requests.delete_responses.append(FakeResponse(payload={"ok": True}))
    mix = tmp_path / "full.ogg"
    mix.write_bytes(b"mix")

    with pytest.raises(RuntimeError, match="canceled"):
        separator_client.SeparationClient(tmp_path, _log(), requests).separate(
            mix, tmp_path / "work", ("guitar",), cancel_cb=CancelAfterFirstChunk(),
        )

    assert not list((tmp_path / "work").rglob("*.flac"))
