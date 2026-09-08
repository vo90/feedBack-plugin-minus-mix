"""Output layouts preserve reviewed paths and never replace unrelated packages."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import reuse_export
import reuse_match
from tests.reuse_fixtures import fast_resources, manager, package, wait_job


def prepared(tmp_path, monkeypatch):
    fast_resources(monkeypatch)
    old, fresh, output = [tmp_path / name for name in ("old", "fresh", "output")]
    output.mkdir()
    package(old / "Unrelated donor folders" / "mix.feedpak", donor=True,
            audio=b"reused mix", preview=b"reused preview")
    package(fresh / "Artist" / "Album" / "song.feedpak", extras={"fresh.txt": b"current chart asset"})
    return manager(tmp_path / "config"), old, fresh, output


def scan(instance, old, fresh, output, **options):
    return wait_job(instance, instance.start_scan(
        old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output), **options,
    )["id"])


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): (reuse_match.digest_file(path), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("options", [{}, {"output_layout": "preserve"}])
def test_preserve_layout_follows_current_originals_not_donor_tree(tmp_path, monkeypatch, options):
    instance, old, fresh, output = prepared(tmp_path, monkeypatch)
    before = {root: snapshot(root) for root in (old, fresh)}
    preview = scan(instance, old, fresh, output, **options)
    assert preview["output_layout"] == "preserve"
    assert preview["counts"]["ready"] == 1
    expected = "Artist/Album/song (No Guitar).feedpak"
    assert preview["items"][0]["output_relative"] == expected
    completed = wait_job(instance, instance.apply(preview["id"])["id"])
    assert completed["counts"]["created"] == 1
    assert list(snapshot(output)) == [expected]
    assert before == {root: snapshot(root) for root in (old, fresh)}


def test_flat_layout_survives_restart_and_repeat_scan_verifies_output(tmp_path, monkeypatch):
    instance, old, fresh, output = prepared(tmp_path, monkeypatch)
    preview = scan(instance, old, fresh, output, output_layout="flat")
    expected = "song (No Guitar).feedpak"
    assert preview["output_layout"] == "flat"
    assert preview["items"][0]["output_relative"] == expected

    restarted = manager(tmp_path / "config")
    assert restarted.latest()["output_layout"] == "flat"
    completed = wait_job(restarted, restarted.apply(preview["id"])["id"])
    assert completed["counts"]["created"] == 1
    assert list(snapshot(output)) == [expected]
    with zipfile.ZipFile(output / expected) as archive:
        assert archive.read("stems/full.ogg") == b"reused mix"
        assert archive.read("preview.ogg") == b"reused preview"
        assert archive.read("fresh.txt") == b"current chart asset"
    before = {root: snapshot(root) for root in (old, fresh, output)}

    def forbidden(*args, **kwargs):
        raise AssertionError("A repeated scan must not export packages")

    monkeypatch.setattr(reuse_export, "export_reuse", forbidden)
    repeated = scan(restarted, old, fresh, output, output_layout="flat")
    assert repeated["counts"]["existing"] == 1
    assert repeated["counts"]["ready"] == repeated["counts"]["created"] == 0
    assert repeated["items"][0]["output_relative"] == expected
    assert before == {root: snapshot(root) for root in (old, fresh, output)}


def test_flat_collisions_use_stable_safe_case_insensitive_names(tmp_path, monkeypatch):
    fast_resources(monkeypatch)
    old, fresh, output = [tmp_path / name for name in ("old", "fresh", "output")]
    output.mkdir()
    package(old / "donor.feedpak", donor=True, audio=b"reused mix")
    expected = {
        "A/Song  Title.feedpak": "Song Title (No Guitar).feedpak",
        "B/song title.feedpak": "song title (No Guitar) (2).feedpak",
        "C/Song Title.feedpak": "Song Title (No Guitar) (3).feedpak",
    }
    for relative in reversed(expected):
        package(fresh / relative, extras={"identity.txt": relative.encode()})
    instance = manager(tmp_path / "config")
    preview = scan(instance, old, fresh, output, output_layout="flat")
    planned = {row["relative_path"]: row["output_relative"] for row in preview["items"]}
    assert planned == expected
    assert len({name.casefold() for name in planned.values()}) == 3
    assert all(Path(name).parent == Path(".") for name in planned.values())
    completed = wait_job(instance, instance.apply(preview["id"])["id"])
    assert completed["counts"]["created"] == 3
    for relative, target in expected.items():
        with zipfile.ZipFile(output / target) as archive:
            assert archive.read("identity.txt") == relative.encode()
    before = snapshot(output)
    repeated = scan(instance, old, fresh, output, output_layout="flat")
    assert {row["relative_path"]: row["output_relative"] for row in repeated["items"]} == expected
    assert repeated["counts"]["existing"] == 3
    assert repeated["counts"]["ready"] == repeated["counts"]["blocked"] == 0
    assert snapshot(output) == before


def test_flat_layout_blocks_unrelated_existing_target_without_overwriting(tmp_path, monkeypatch):
    instance, old, fresh, output = prepared(tmp_path, monkeypatch)
    target = package(output / "song (No Guitar).feedpak", donor=True, audio=b"unrelated mix")
    before = snapshot(output)
    preview = scan(instance, old, fresh, output, output_layout="flat")
    assert preview["counts"]["blocked"] == 1
    assert preview["counts"]["ready"] == preview["counts"]["existing"] == 0
    assert preview["items"][0]["output_relative"] == target.name
    assert "output" in preview["items"][0]["reason"].lower()
    with pytest.raises(reuse_match.ReuseError):
        instance.apply(preview["id"])
    assert snapshot(output) == before


@pytest.mark.parametrize("layout", [None, True, 2, "", "folders", [], {}])
def test_invalid_layout_does_not_replace_reviewed_job(tmp_path, monkeypatch, layout):
    instance, old, fresh, output = prepared(tmp_path, monkeypatch)
    preview = scan(instance, old, fresh, output)
    before = instance.state_file.read_bytes()
    with pytest.raises(ValueError, match="layout"):
        instance.start_scan(old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output),
                            output_layout=layout)
    assert instance.latest()["id"] == preview["id"]
    assert instance.state_file.read_bytes() == before
    assert not instance.is_active()
    assert not list(output.iterdir())


def test_checkpoint_without_layout_retains_original_preserve_behavior(tmp_path, monkeypatch):
    instance, old, fresh, output = prepared(tmp_path, monkeypatch)
    preview = scan(instance, old, fresh, output)
    data = json.loads(instance.state_file.read_text(encoding="utf-8"))
    del data["output_layout"]
    instance.state_file.write_text(json.dumps(data), encoding="utf-8")
    before = instance.state_file.read_bytes()
    restarted = manager(tmp_path / "config")
    assert restarted.latest()["output_layout"] == "preserve"
    assert instance.state_file.read_bytes() == before
    completed = wait_job(restarted, restarted.apply(preview["id"])["id"])
    assert completed["counts"]["created"] == 1
    assert list(snapshot(output)) == ["Artist/Album/song (No Guitar).feedpak"]


@pytest.mark.parametrize("layout", [None, "unknown"])
def test_checkpoint_rejects_invalid_layout_without_altering_saved_data(tmp_path, monkeypatch, layout):
    instance, old, fresh, output = prepared(tmp_path, monkeypatch)
    scan(instance, old, fresh, output)
    data = json.loads(instance.state_file.read_text(encoding="utf-8"))
    data["output_layout"] = layout
    instance.state_file.write_text(json.dumps(data), encoding="utf-8")
    before = instance.state_file.read_bytes()
    restarted = manager(tmp_path / "config")
    assert restarted.latest()["status"] == "failed"
    assert "layout" in restarted.latest()["detail"].lower()
    assert instance.state_file.read_bytes() == before
    assert not list(output.iterdir())
