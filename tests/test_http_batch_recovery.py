"""A real three-song export queue survives an HTTP interruption on song two."""
from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
import time
import zipfile
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

import batch
import exporter
import separator_client as client_module
from tests.test_exporter import FFMPEG, _ogg_sine, _ogg_two_sines

pytestmark = pytest.mark.skipif(not FFMPEG, reason="FFmpeg is required for real batch audio exports")


def _sources(tmp_path):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    paths = []
    for index in range(1, 4):
        full = tmp_path / f"full-{index}.ogg"
        # Each song has its own identifiable full mix and the same guitar component.
        _ogg_two_sines(full, low=100 + index * 50, high=440)
        source = source_root / f"song-{index}.feedpak"
        manifest = {
            "feedpak_version": "1.14.0", "title": f"Song {index}", "artist": "HTTP Test",
            "duration": 2.0,
            "stems": [{"id": "full", "file": "stems/full.ogg", "default": True}],
            "arrangements": [{"id": "lead", "type": "guitar", "file": "arrangements/lead.json"}],
        }
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("manifest.yaml", yaml.safe_dump(manifest))
            archive.writestr("arrangements/lead.json", json.dumps({"events": [{"time": 1.0, "fret": index}]}))
            archive.write(full, "stems/full.ogg")
        paths.append(source)
    guitar = tmp_path / "guitar.ogg"
    _ogg_sine(guitar, 440)
    return source_root, paths, guitar.read_bytes()


@contextmanager
def _interrupted_service(audio):
    calls = Counter()
    lock = threading.Lock()
    interrupted, resume = threading.Event(), threading.Event()

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

        def do_GET(self):
            with lock:
                calls[self.path] += 1
            if self.path == "/health":
                if interrupted.is_set() and not resume.is_set():
                    return self.reply({"detail": "Runtime is draining", "code": "runtime_draining"}, 503)
                return self.reply({
                    "status": "ok", "device": "cpu", "gpu": False,
                    "warmup": {"bs_roformer_sw": "skipped"},
                    "runtime": {
                        "schema_version": 1, "managed": True,
                        "generation_id": "replacement" if resume.is_set() else "original",
                        "capabilities": ["verified_models_v1", "runtime_identity_v1", "persisted_results_v1"],
                        "activity": {"draining": False, "sealed": False},
                        "models": {"bs_roformer_sw": {
                            "verified": True, "engine": "audio-separator", "revision": "weights-1",
                            "stems": list(client_module.SUPPORTED_STEMS),
                        }},
                    },
                })
            if self.path.startswith("/jobs/"):
                job_id = self.path.rsplit("/", 1)[1]
                if job_id == "job-2" and not resume.is_set():
                    interrupted.set()
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return None
                return self.reply({
                    "job_id": job_id, "status": "complete", "progress": 100,
                    "stems": {"guitar": f"/download/{job_id}/guitar.ogg"},
                })
            if self.path.startswith("/download/") and self.path.endswith("/guitar.ogg"):
                self.send_response(200)
                self.send_header("Content-Type", "audio/ogg")
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)
                return None
            return self.reply({"detail": "not found"}, 404)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = urlsplit(self.path)
            if request.path != "/separate":
                return self.reply({"detail": "not found"}, 404)
            if parse_qs(request.query) != {"model": ["bs_roformer_sw"], "stems": ["guitar"]}:
                return self.reply({"detail": "unexpected model or stems"}, 422)
            with lock:
                calls["POST /separate"] += 1
                job_id = f"job-{calls['POST /separate']}"
            return self.reply({"job_id": job_id})

        def do_DELETE(self):
            with lock:
                calls["DELETE"] += 1
            self.reply({"ok": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port, interrupted, resume, calls
    finally:
        resume.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


def _await_job(manager, job_id, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    latest = manager.get(job_id)
    while time.monotonic() < deadline:
        latest = manager.get(job_id)
        if predicate(latest):
            return latest
        time.sleep(0.01)
    raise AssertionError(f"Batch did not reach expected state: {latest}")


def test_three_real_exports_wait_for_second_song_and_resume_without_duplicates(tmp_path, monkeypatch):
    def short_wait(seconds, cancel_cb):
        if cancel_cb:
            cancel_cb()
        time.sleep(min(seconds, 0.02))

    monkeypatch.setattr(client_module, "_interruptible_wait", short_wait)
    monkeypatch.setattr(client_module, "RECOVERY_TIMEOUT_SECONDS", 20)
    monkeypatch.setattr(client_module, "JOB_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(client_module, "TOTAL_TIMEOUT_SECONDS", 50)
    source_root, sources, guitar = _sources(tmp_path)
    original_bytes = {source: source.read_bytes() for source in sources}
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    log = logging.getLogger("http-batch-test")
    with _interrupted_service(guitar) as (port, interrupted, resume, calls):
        (config / "stem_splitter.json").write_text(json.dumps({
            "local_server_port": port, "remote_model": "bs_roformer_sw",
        }))
        client = client_module.SeparationClient(config, log)
        manager = batch.BatchManager(exporter, client, config, log)
        job = manager.start(input_dir=str(source_root), output_dir=str(output_root),
                            excluded_stems=["guitar"], recursive=True, skip_existing=True,
                            skip_derived=True, preserve_structure=True)
        try:
            waiting = _await_job(manager, job["id"], lambda value:
                                 any(item.get("stage") == "waiting_for_server"
                                     for item in value.get("items", []))
                                 or value["status"] not in batch.ACTIVE_STATUSES)
            assert interrupted.is_set(), f"The actual client never polled song two: {waiting}"
            assert waiting["status"] == "running"
            assert [item["status"] for item in waiting["items"]] == ["done", "running", "queued"]
            assert waiting["items"][1]["stage"] == "waiting_for_server"
            assert waiting["counts"]["done"] == waiting["counts"]["queued"] == 1
            assert waiting["counts"]["failed"] == waiting["counts"]["blocked"] == 0
            assert 1 / 3 <= waiting["overall_progress"] < 1
            assert waiting["cancel_requested"] is False
            first_output = next(output_root.glob("*.feedpak"))
            first_output_bytes = first_output.read_bytes()
            assert len(list(output_root.glob("*.feedpak"))) == 1
            assert calls["POST /separate"] == 2
            resume.set()
            complete = _await_job(manager, job["id"], lambda value: value["status"] not in batch.ACTIVE_STATUSES)
            assert complete["status"] == "completed", complete
            assert complete["counts"]["done"] == complete["counts"]["temporary_separations"] == 3
            assert complete["counts"]["failed"] == complete["counts"]["queued"] == complete["counts"]["blocked"] == 0
            assert complete["counts"]["duplicate_audio_reused"] == 0
            assert complete["overall_progress"] == 1
            assert first_output.read_bytes() == first_output_bytes
        finally:
            resume.set()
            if manager.get(job["id"])["status"] in batch.ACTIVE_STATUSES:
                manager.cancel(job["id"])
            for worker in threading.enumerate():
                if worker.name == f"minus-mix-batch-{job['id'][:8]}":
                    worker.join(timeout=10)
                    assert not worker.is_alive(), "Owned batch worker did not stop"

    outputs = sorted(output_root.glob("*.feedpak"))
    assert len(outputs) == 3
    assert calls["POST /separate"] == 3
    assert calls["/jobs/job-2"] >= 2, "Recovery must poll the original accepted job"
    assert calls["DELETE"] == 0
    for index, output in enumerate(outputs, start=1):
        assert calls[f"/download/job-{index}/guitar.ogg"] == 1
        with zipfile.ZipFile(output) as archive:
            assert archive.testzip() is None
            manifest = yaml.safe_load(archive.read("manifest.yaml"))
            assert [stem["id"] for stem in manifest["stems"]] == ["full"]
            assert manifest["minus_mix"]["excluded_stems"] == ["guitar"]
            assert manifest["minus_mix"]["source_title"] == f"Song {index}"
            assert json.loads(archive.read("arrangements/lead.json"))["events"][0]["fret"] == index
            audio = archive.read("stems/full.ogg")
        decoded = subprocess.run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", "pipe:0",
            "-f", "f32le", "-ac", "1", "-ar", "44100", "pipe:1",
        ], input=audio, capture_output=True, timeout=15, check=False)
        assert decoded.returncode == 0, decoded.stderr.decode("utf-8", "replace")
        assert len(decoded.stdout) >= 44100 * 4, "Each output contains at least one second of playable audio"
    assert {source: source.read_bytes() for source in sources} == original_bytes
