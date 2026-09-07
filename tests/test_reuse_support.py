from __future__ import annotations

import ntpath
import os
from pathlib import Path

import pytest

import reuse_support


@pytest.mark.skipif(os.name != "nt", reason="Windows non-strict path resolution")
def test_output_parent_created_during_resolution_does_not_false_escape(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    relative = "new-folder/song.feedpak"
    target = root / relative
    original = ntpath._getfinalpathname
    errors = []
    created = False

    def resolve_with_sibling_creation(value):
        nonlocal created
        try:
            return original(value)
        except OSError as exc:
            if Path(value) == target:
                errors.append(exc.winerror)
                if not created and exc.winerror == 3:
                    # Another copy worker creates the shared output parent
                    # between genuine ERROR_PATH_NOT_FOUND/FILE_NOT_FOUND probes.
                    target.parent.mkdir()
                    created = True
            raise

    monkeypatch.setattr(ntpath, "_getfinalpathname", resolve_with_sibling_creation)
    assert reuse_support.checked_path(root, relative, ValueError, exists=False) == target
    assert created and errors[:3] == [3, 2, 2]
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path namespace")
@pytest.mark.parametrize("exists, attempts", [(False, 2), (True, 1)])
def test_persistent_extended_namespace_escape_remains_blocked(tmp_path, monkeypatch, exists, attempts):
    root = tmp_path.resolve()
    target = root / "song.feedpak"
    outside = Path("\\\\?\\" + str(root.parent / "outside" / "song.feedpak"))
    original = Path.resolve
    calls = 0

    def resolved_outside(value, *args, **kwargs):
        nonlocal calls
        if value == target:
            calls += 1
            return outside
        return original(value, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolved_outside)
    with pytest.raises(ValueError, match="outside"):
        reuse_support.checked_path(root, target.name, ValueError, exists=exists)
    assert calls == attempts


def test_output_parent_traversal_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unsafe"):
        reuse_support.checked_path(tmp_path, "../outside/song.feedpak", ValueError, exists=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction containment")
@pytest.mark.parametrize("created_on_retry", [False, True])
def test_output_junction_escape_is_rejected_including_retry(tmp_path, monkeypatch, created_on_retry):
    import _winapi

    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    root = root.resolve()
    payload = outside / "song.feedpak"
    payload.write_bytes(b"untouched outside input")
    target = root / "linked" / payload.name
    original = Path.resolve

    def create_junction():
        _winapi.CreateJunction(str(outside), str(target.parent))

    if created_on_retry:
        def first_resolution(value, *args, **kwargs):
            if value == target:
                create_junction()
                return Path("\\\\?\\" + str(target))
            return original(value, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", first_resolution)
    else:
        create_junction()
    with pytest.raises(ValueError, match="Linked"):
        reuse_support.checked_path(root, "linked/song.feedpak", ValueError, exists=False)
    assert payload.read_bytes() == b"untouched outside input"
