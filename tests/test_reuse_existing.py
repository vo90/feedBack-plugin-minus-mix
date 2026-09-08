"""Repeat scans recognize verified outputs without writing or hiding conflicts."""
from __future__ import annotations

import zipfile

import pytest

import exporter
import reuse_export
import reuse_match
from tests.reuse_fixtures import fast_resources, manager, package, wait_job


def scan(instance, old, fresh, output):
    return wait_job(instance, instance.start_scan(
        old_dir=str(old), fresh_dir=str(fresh), output_dir=str(output),
    )["id"])


def prepared(tmp_path, monkeypatch):
    fast_resources(monkeypatch)
    old, fresh, output = [tmp_path / name for name in ("old", "fresh", "output")]
    output.mkdir()
    package(old / "Artist" / "original.feedpak", donor=True, audio=b"reused guitar mix")
    package(fresh / "Artist" / "Album" / "song.feedpak")
    instance = manager(tmp_path / "config")
    preview = scan(instance, old, fresh, output)
    assert preview["counts"]["ready"] == 1
    return instance, preview, old, fresh, output


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): (reuse_match.digest_file(path), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


def finish(instance, preview):
    return wait_job(instance, instance.apply(preview["id"])["id"])


def replace_member(path, member, contents):
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members[member] = contents
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def test_repeat_scan_recognizes_exact_nested_output_without_writing(tmp_path, monkeypatch):
    instance, preview, old, fresh, output = prepared(tmp_path, monkeypatch)
    completed = finish(instance, preview)
    assert completed["counts"]["created"] == 1
    assert completed["counts"]["existing"] == 0
    expected = "Artist/Album/song (No Guitar).feedpak"
    assert completed["items"][0]["output_relative"] == expected
    # An unrelated output is neither a source nor a reason to block this target.
    (output / "unrelated.feedpak").write_bytes(b"not a valid package")
    before = {root: snapshot(root) for root in (old, fresh, output)}

    def forbidden(*args, **kwargs):
        raise AssertionError("A read-only scan must not export packages")

    monkeypatch.setattr(reuse_export, "export_reuse", forbidden)
    repeated = scan(instance, old, fresh, output)
    assert repeated["status"] == "ready"
    assert repeated["counts"]["ready"] == 0
    assert repeated["counts"]["done"] == repeated["counts"]["existing"] == 1
    assert repeated["counts"]["created"] == 0
    assert repeated["items"][0]["receipt"]["recovered"] is True
    assert repeated["items"][0]["output_relative"] == expected
    assert before == {root: snapshot(root) for root in (old, fresh, output)}
    restarted = manager(tmp_path / "config").latest()
    assert restarted["counts"] == repeated["counts"]


def test_mixed_scan_and_apply_distinguish_existing_from_new_outputs(tmp_path, monkeypatch):
    instance, preview, old, fresh, output = prepared(tmp_path, monkeypatch)
    finish(instance, preview)
    prior = snapshot(output)
    package(old / "second.feedpak", donor=True, title="Second song", audio=b"second mix")
    package(fresh / "Other Album" / "second.feedpak", title="Second song")
    repeated = scan(instance, old, fresh, output)
    assert repeated["counts"]["ready"] == repeated["counts"]["existing"] == 1
    assert repeated["counts"]["created"] == 0
    completed = finish(instance, repeated)
    assert completed["counts"]["done"] == 2
    assert completed["counts"]["created"] == completed["counts"]["existing"] == 1
    assert completed["counts"]["failed"] == 0
    after = snapshot(output)
    assert len(after) == 2
    assert all(after[name] == value for name, value in prior.items())
    restarted = manager(tmp_path / "config").latest()
    assert restarted["counts"] == completed["counts"]


@pytest.mark.parametrize("damage", ["invalid_zip", "audio", "retained_asset"])
def test_scan_blocks_damaged_or_different_output_without_overwriting(
    tmp_path, monkeypatch, damage,
):
    instance, preview, old, fresh, output = prepared(tmp_path, monkeypatch)
    completed = finish(instance, preview)
    target = output / completed["items"][0]["output_relative"]
    if damage == "invalid_zip":
        target.write_bytes(b"incomplete or corrupt archive")
    elif damage == "audio":
        replace_member(target, "stems/full.ogg", b"different audio")
    else:
        replace_member(target, "cover.png", b"changed retained asset")
    before = snapshot(output)
    repeated = scan(instance, old, fresh, output)
    assert repeated["counts"]["blocked"] == 1
    assert repeated["counts"]["ready"] == repeated["counts"]["existing"] == 0
    assert repeated["items"][0]["output_relative"] == completed["items"][0]["output_relative"]
    assert "output" in repeated["items"][0]["reason"].lower()
    with pytest.raises(reuse_match.ReuseError):
        instance.apply(repeated["id"])
    assert snapshot(output) == before


@pytest.mark.parametrize("changed", ["fresh", "donor"])
def test_new_scan_does_not_skip_output_from_changed_input(tmp_path, monkeypatch, changed):
    instance, preview, old, fresh, output = prepared(tmp_path, monkeypatch)
    finish(instance, preview)
    before = snapshot(output)
    if changed == "fresh":
        package(fresh / "Artist" / "Album" / "song.feedpak", extras={"new-asset.txt": b"new"})
    else:
        package(old / "Artist" / "original.feedpak", donor=True, audio=b"new donor mix")
    repeated = scan(instance, old, fresh, output)
    assert repeated["counts"]["blocked"] == 1
    assert repeated["counts"]["existing"] == repeated["counts"]["ready"] == 0
    assert snapshot(output) == before


def test_output_appearing_after_preview_is_reported_as_existing_on_apply(tmp_path, monkeypatch):
    instance, preview, _, _, output = prepared(tmp_path, monkeypatch)
    row = instance.job["items"][0]
    fresh = reuse_match.inspect_package(row["_fresh"]["path"], donor=False, include_payload=True)
    donor = instance.job["_donors"][row["donor_relative"]]
    target = output / row["output_relative"]
    target.parent.mkdir(parents=True)
    reuse_export.export_reuse(fresh, donor, target, match=reuse_match, exporter=exporter)
    before = snapshot(output)
    completed = finish(instance, preview)
    assert completed["counts"]["existing"] == 1
    assert completed["counts"]["created"] == completed["counts"]["failed"] == 0
    assert completed["items"][0]["receipt"]["recovered"] is True
    assert "created" not in completed["items"][0]["reason"].lower()
    assert snapshot(output) == before


@pytest.mark.parametrize("changed", ["fresh", "output"])
def test_apply_rechecks_completed_preview_before_accepting_existing_output(
    tmp_path, monkeypatch, changed,
):
    instance, preview, old, fresh, output = prepared(tmp_path, monkeypatch)
    finish(instance, preview)
    repeated = scan(instance, old, fresh, output)
    assert repeated["counts"]["existing"] == 1
    if changed == "fresh":
        package(fresh / "Artist" / "Album" / "song.feedpak", extras={"new.txt": b"new"})
    else:
        target = output / repeated["items"][0]["output_relative"]
        replace_member(target, "cover.png", b"changed since scan")
    before = snapshot(output)
    completed = finish(instance, repeated)
    assert completed["counts"]["failed"] == 1
    assert completed["counts"]["existing"] == completed["counts"]["created"] == 0
    assert snapshot(output) == before


@pytest.mark.parametrize("choice", ["Artist/original.feedpak", "alternate.feedpak"])
def test_recording_choice_verifies_existing_output_for_selected_donor(
    tmp_path, monkeypatch, choice,
):
    instance, preview, old, fresh, output = prepared(tmp_path, monkeypatch)
    finish(instance, preview)
    package(old / "alternate.feedpak", donor=True, audio=b"alternate donor mix")
    before = snapshot(output)
    repeated = scan(instance, old, fresh, output)
    assert repeated["counts"]["review"] == 1
    group = repeated["groups"][0]
    selected = wait_job(instance, instance.choose(repeated["id"], {group["id"]: choice})["id"])
    assert selected["counts"]["review"] == selected["counts"]["ready"] == 0
    expected_status = "existing" if choice == "Artist/original.feedpak" else "blocked"
    assert selected["counts"][expected_status] == 1
    assert selected["counts"]["created"] == 0
    assert snapshot(output) == before
