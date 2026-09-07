from __future__ import annotations

import zipfile

import pytest
import yaml

import exporter
import reuse_export as packing
import reuse_match as match
from tests.reuse_fixtures import package


def pair(tmp_path, **donor_options):
    fresh_path = package(tmp_path / "fresh.feedpak", extras={"custom.bin": b"user edit"})
    donor_path = package(tmp_path / "donor.feedpak", donor=True, audio=b"ready-made No Guitar", **donor_options)
    return match.inspect_package(fresh_path), match.inspect_package(donor_path, donor=True)


def test_exact_audio_preview_and_fresh_members_without_ffmpeg(tmp_path, monkeypatch):
    fresh, donor = pair(tmp_path)

    def forbidden():
        raise AssertionError("Encoding/separation must not run")

    monkeypatch.setattr(exporter, "_ffmpeg_cmd", forbidden)
    result = packing.export_reuse(fresh, donor, tmp_path / "output.feedpak", match=match, exporter=exporter)
    with zipfile.ZipFile(result["output"]) as output, zipfile.ZipFile(fresh["path"]) as source:
        for name in ("arrangements/lead.json", "cover.png", "custom.bin"):
            assert output.read(name) == source.read(name)
        assert output.read("stems/full.ogg") == b"ready-made No Guitar"
        assert output.read("preview.ogg") == b"encoded preview"
        manifest = yaml.safe_load(output.read("manifest.yaml"))
        assert manifest["title"] == "Song (No Guitar)"
        assert manifest["stems"] == [{"id": "full", "file": "stems/full.ogg", "codec": "vorbis", "default": True}]
    assert match.digest_file(fresh["path"]) == fresh["sha256"]
    assert match.digest_file(donor["path"]) == donor["sha256"]
    assert packing.completed_output(result["output"], fresh, donor, match=match)


def test_absent_donor_preview_does_not_retain_original_full_mix_preview(tmp_path):
    fresh, donor = pair(tmp_path, preview=None)
    target = tmp_path / "out.feedpak"
    packing.export_reuse(fresh, donor, target, match=match, exporter=exporter)
    with zipfile.ZipFile(target) as archive:
        assert "preview.ogg" not in archive.namelist()
        assert "preview" not in yaml.safe_load(archive.read("manifest.yaml"))


def test_existing_file_is_never_overwritten(tmp_path):
    fresh, donor = pair(tmp_path)
    target = tmp_path / "out.feedpak"
    target.write_bytes(b"other file")
    with pytest.raises(match.ReuseError, match="exists"):
        packing.export_reuse(fresh, donor, target, match=match, exporter=exporter)
    assert target.read_bytes() == b"other file"


def test_same_size_same_mtime_changed_input_fails_hash_guard(tmp_path):
    import os

    fresh, donor = pair(tmp_path)
    path = tmp_path / "fresh.feedpak"
    original_stat = path.stat()
    raw = bytearray(path.read_bytes())
    raw[10] ^= 1
    path.write_bytes(raw)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    with pytest.raises(match.ReuseError, match="content changed"):
        packing.export_reuse(fresh, donor, tmp_path / "out.feedpak", match=match, exporter=exporter)
    assert not (tmp_path / "out.feedpak").exists()


def test_chunk_cancellation_leaves_no_published_or_temporary_output(tmp_path):
    fresh, donor = pair(tmp_path)
    count = 0

    def cancel():
        nonlocal count
        count += 1
        if count > 4:
            raise match.ReuseError("stop")

    with pytest.raises(match.ReuseError, match="stop"):
        packing.export_reuse(fresh, donor, tmp_path / "out.feedpak", match=match, exporter=exporter, cancel=cancel)
    assert not (tmp_path / "out.feedpak").exists()
    assert not list(tmp_path.glob(".minusmix-reuse-*"))


def test_replacement_cannot_destroy_undeclared_fresh_asset(tmp_path):
    fresh_path = package(tmp_path / "fresh.feedpak", preview=None, extras={"preview.ogg": b"custom asset"})
    donor_path = package(tmp_path / "donor.feedpak", donor=True)
    with pytest.raises(match.ReuseError, match="conflicts"):
        packing.export_reuse(match.inspect_package(fresh_path), match.inspect_package(donor_path, donor=True),
                             tmp_path / "out.feedpak", match=match, exporter=exporter)


def test_forced_publication_fallback_cannot_replace_racing_file(tmp_path, monkeypatch):
    import os

    temporary, target = tmp_path / "private.tmp", tmp_path / "target.feedpak"
    temporary.write_bytes(b"our package")

    def race(*args):
        target.write_bytes(b"other process")
        raise OSError("hard links unavailable")

    monkeypatch.setattr(packing.os, "link", race)
    if os.name == "nt":
        assert packing.publish_no_replace(temporary, target) is False
    else:
        with pytest.raises(OSError, match="unavailable"):
            packing.publish_no_replace(temporary, target)
    assert target.read_bytes() == b"other process"
    assert temporary.read_bytes() == b"our package"


def test_verified_snapshot_requires_exact_retained_inventory(tmp_path):
    fresh, donor = pair(tmp_path)
    fresh = match.inspect_package(fresh["path"], include_payload=True)
    fresh["payload_hashes"]["missing-custom-file.bin"] = "0" * 64
    with pytest.raises(match.ReuseError, match="inventory"):
        packing.export_reuse(fresh, donor, tmp_path / "out.feedpak", match=match,
                             exporter=exporter, snapshot_verified=True)


def test_audio_declaration_overlapping_retained_asset_is_rejected(tmp_path):
    path = package(tmp_path / "fresh.feedpak", manifest_changes={"cover": "preview.ogg"})
    donor = package(tmp_path / "donor.feedpak", donor=True)
    with pytest.raises(match.ReuseError, match="overlap"):
        packing.export_reuse(match.inspect_package(path), match.inspect_package(donor, donor=True),
                             tmp_path / "out.feedpak", match=match, exporter=exporter)
