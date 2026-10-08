"""Small valid chart packages for byte-copy and orchestration tests."""
from __future__ import annotations

import json
import logging
import time
import zipfile

import yaml

import exporter
import reuse_batch
import reuse_export
import reuse_match
import reuse_support


def chart():
    return {"tuning": [0] * 6, "capo": 0, "notes": [
        {"t": float(t), "s": t % 6, "f": t % 12, "sus": 0.5, "bn": 1}
        for t in range(1, 13)], "chords": [], "templates": []}


def package(path, *, donor=False, document=None, audio=b"encoded backing audio",
            preview=b"encoded preview", title="Song", artist="Artist", duration=15.0,
            extras=None, manifest_changes=None, excluded_stems=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"feedpak_version": "1.14.0", "title": title, "artist": artist,
                "duration": duration, "album": "Album", "year": 2000,
                "arrangements": [{"id": "lead", "name": "Lead", "file": "arrangements/lead.json",
                                   "type": "guitar", "tuning": [0] * 6, "capo": 0}],
                "stems": [{"id": "full", "file": "stems/full.ogg", "codec": "vorbis", "default": True}],
                "cover": "cover.png"}
    if donor:
        excluded = ["guitar"] if excluded_stems is None else excluded_stems
        manifest["title"] += " (" + exporter._suffix(excluded) + ")"
        manifest["minus_mix"] = {"source_title": title, "excluded_stems": excluded, "generator": "minus_mix"}
    if preview is not None:
        manifest["preview"] = "preview.ogg"
    manifest.update(manifest_changes or {})
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.yaml", yaml.safe_dump(manifest))
        archive.writestr("arrangements/lead.json", json.dumps(document or chart()))
        archive.writestr("stems/full.ogg", audio)
        archive.writestr("cover.png", b"fresh cover")
        if preview is not None:
            archive.writestr("preview.ogg", preview)
        for name, data in (extras or {}).items():
            archive.writestr(name, data)
    return path


def manager(config):
    return reuse_batch.ReuseManager(match=reuse_match, packing=reuse_export, exporter=exporter,
                                    support=reuse_support, config_dir=config,
                                    log=logging.getLogger("reuse-test"))


def wait_job(instance, job_id, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = instance.get(job_id, limit=1)
        if job["status"] not in reuse_batch.ACTIVE:
            return instance.get(job_id)
        time.sleep(0.05)
    snapshot = instance.get(job_id, limit=1)
    instance.cancel(job_id)
    raise AssertionError({key: snapshot[key] for key in ("status", "detail", "counts")})


def fast_resources(monkeypatch):
    original = reuse_support.resources

    def fixed(requested="auto", **kwargs):
        return original(requested, hardware={"cpu_count": 12, "memory_available": 8 * 1024**3,
                                             "storage": "ssd"}, **kwargs)

    monkeypatch.setattr(reuse_support, "resources", fixed)
