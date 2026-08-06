"""Standalone Practice Mix Exporter server-client compatibility tests."""
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
        return self.get_responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        assert self.post_responses, f"unexpected POST {url}"
        return self.post_responses.pop(0)

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        assert self.delete_responses, f"unexpected DELETE {url}"
        return self.delete_responses.pop(0)


def _log():
    return SimpleNamespace(warning=lambda *args, **kwargs: None)


def _write_config(root: Path, *, port=7865, model="bs_roformer_sw"):
    (root / "stem_splitter.json").write_text(json.dumps({
        "local_server_port": port,
        "remote_model": model,
    }), encoding="utf-8")
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


def test_status_without_server_is_actionable(tmp_path):
    requests = FakeRequests()
    requests.get_responses.append(FakeResponse(status_code=503, payload={}))

    status = separator_client.SeparationClient(tmp_path, _log(), requests).status()

    assert status["ready"] is False
    assert "start the local server" in status["reason"]


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


def test_api_key_never_follows_cross_origin_download_redirect(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "demucs_server_url": "https://split.example.test",
        "demucs_api_key": "secret-value",
    }), encoding="utf-8")
    # The default local probe fails; the configured remote probe succeeds.
    requests = FakeRequests()
    requests.get_responses.extend([
        FakeResponse(status_code=503, payload={}),
        FakeResponse(payload={"status": "ok"}),
        FakeResponse(status_code=302, headers={"location": "https://files.example.test/guitar.flac"}),
        FakeResponse(chunks=[b"stem"]),
    ])
    requests.post_responses.append(FakeResponse(payload={
        "job_id": "job-safe",
        "stems": {"guitar": "https://split.example.test/download/guitar.flac"},
    }))
    requests.delete_responses.append(FakeResponse(payload={"ok": True}))
    mix = tmp_path / "full.ogg"
    mix.write_bytes(b"mix")

    result = separator_client.SeparationClient(tmp_path, _log(), requests).separate(
        mix, tmp_path / "work", ("guitar",),
    )

    assert result["guitar"].read_bytes() == b"stem"
    redirect_call = next(call for call in requests.calls if call[1].startswith("https://files."))
    assert redirect_call[2].get("headers") is None
    origin_call = next(call for call in requests.calls if "/download/guitar.flac" in call[1])
    assert origin_call[2]["headers"] == {"X-API-Key": "secret-value"}


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
