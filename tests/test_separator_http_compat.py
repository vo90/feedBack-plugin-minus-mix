"""Real HTTP streams and FFmpeg validation across legacy/managed recovery."""
from __future__ import annotations

import io
import json
import logging
import socket
import threading
import time
import wave
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from audio import _ffmpeg_cmd

import separator_client as client_module

pytestmark = pytest.mark.skipif(not _ffmpeg_cmd(), reason="FFmpeg is required")


def _audio():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        audio.writeframes(b"\x01\x00\x02\x00" * 4410)
    return output.getvalue()


@contextmanager
def _service(mode):
    counts = Counter()
    payload = _audio()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, data, status=200):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(body)

        def drop(self):
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

        def do_GET(self):
            counts[self.path] += 1
            if self.path == "/health":
                health = {"status": "ok", "device": "cpu", "gpu": False,
                          "demucs_model": "bs_roformer_sw", "warmup": {"bs_roformer_sw": "skipped"}}
                if mode != "legacy":
                    removed = mode == "retained_after_update" and counts["/jobs/job-1"] > 0
                    health["runtime"] = {
                        "schema_version": 1, "managed": True,
                        "generation_id": "replacement" if removed else "original",
                        "fingerprint": "replacement" if removed else "original",
                        "capabilities": ["verified_models_v1", "runtime_identity_v1", "persisted_results_v1"],
                        "activity": {"draining": False, "sealed": False},
                        "models": {} if removed else {"bs_roformer_sw": {
                            "verified": True, "engine": "audio-separator", "revision": "weights-1",
                            "stems": list(client_module.SUPPORTED_STEMS)}},
                    }
                return self.reply(health)
            if self.path.startswith("/jobs/"):
                if mode == "retained_after_update" and counts[self.path] == 1:
                    return self.drop()
                return self.reply({"job_id": "job-1", "status": "complete", "progress": 100,
                                   "stems": {"guitar": "/download/job-1/guitar.wav"}})
            if self.path == "/download/job-1/guitar.wav":
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if mode == "truncated_download" and counts[self.path] == 1:
                    self.wfile.write(payload[:125])
                    self.wfile.flush()
                    return self.drop()
                self.wfile.write(payload)
                return None
            return self.reply({"error": "not found"}, 404)

        def do_POST(self):
            counts["POST"] += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if mode == "draining_upload" and counts["POST"] == 1:
                return self.reply({"error": "Runtime is draining", "code": "runtime_draining"}, 503)
            if mode == "lost_upload" and counts["POST"] == 1:
                return self.drop()
            return self.reply({"job_id": "job-1"})

        def do_DELETE(self):
            counts["DELETE"] += 1
            self.reply({"ok": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port, counts, payload
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("mode", [
    "legacy", "managed", "truncated_download", "retained_after_update", "draining_upload", "lost_upload",
])
def test_real_http_compatibility_and_recovery(tmp_path, monkeypatch, mode):
    def short_wait(seconds, cancel_cb):
        if cancel_cb:
            cancel_cb()
        time.sleep(min(seconds, 0.01))

    monkeypatch.setattr(client_module, "_interruptible_wait", short_wait)
    monkeypatch.setattr(client_module, "RECOVERY_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(client_module, "JOB_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(client_module, "TOTAL_TIMEOUT_SECONDS", 20)
    with _service(mode) as (port, calls, audio):
        (tmp_path / "stem_splitter.json").write_text(json.dumps({"local_server_port": port}))
        source = tmp_path / "mix.wav"
        source.write_bytes(audio)
        client = client_module.SeparationClient(tmp_path, logging.getLogger("http-test"))
        assert client.status()["ready"] is True
        states = []
        result = client.separate(source, tmp_path / "work", ("guitar",), state_cb=states.append)

    assert result["guitar"].read_bytes() == audio
    assert not list((tmp_path / "work").rglob("*.part"))
    assert calls["DELETE"] == 0
    assert calls["POST"] == (2 if mode in {"draining_upload", "lost_upload"} else 1)
    if mode == "truncated_download":
        assert calls["/download/job-1/guitar.wav"] == 2
    if mode in {"truncated_download", "retained_after_update", "draining_upload", "lost_upload"}:
        assert any(state["state"] == "waiting_for_server" for state in states)
