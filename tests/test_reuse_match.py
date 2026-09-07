from __future__ import annotations

import copy
import zipfile

import pytest

import reuse_match as match
from tests.reuse_fixtures import chart, package


def test_known_conversion_repairs_do_not_change_sounding_identity(tmp_path):
    old, fresh = chart(), chart()
    old["notes"][0].update(f=127, mt=True)
    fresh["notes"][0].update(f=0, mt=True, bnv=[{"t": 0, "v": 0}, {"t": 0.25, "v": 1}])
    fresh["notes"][1]["sus"] = 4
    fresh["notes"][2].pop("bn")
    fresh.update(anchors=[{"t": 1, "f": 2}], beats=[{"t": 2, "m": 1}], phrases=[{"levels": []}])
    # Representation change: grouped chord notes replace standalone notes.
    fresh["chords"] = [{"t": n["t"], "notes": [n]} for n in fresh.pop("notes")]
    donor = match.inspect_package(package(tmp_path / "old.feedpak", donor=True, document=old), donor=True)
    target = match.inspect_package(package(tmp_path / "new.feedpak", document=fresh))
    assert match.compatible(target, donor)


@pytest.mark.parametrize("change", ["onset", "pitch", "tuning", "capo", "album", "year", "offset", "title"])
def test_same_filename_never_overrides_version_or_musical_mismatch(tmp_path, change):
    donor = match.inspect_package(package(tmp_path / "old/Song.feedpak", donor=True), donor=True)
    document = chart()
    manifest = {}
    if change == "onset":
        document["notes"][0]["t"] += 0.1
    elif change == "pitch":
        document["notes"][0]["f"] += 1
    elif change == "tuning":
        document["tuning"][0] = -1
    elif change == "capo":
        document["capo"] = 2
    else:
        manifest[change] = {"album": "Other", "year": 2024, "offset": 0.1, "title": "Song (Live)"}[change]
    target = match.inspect_package(package(tmp_path / "new/Song.feedpak", document=document,
                                           manifest_changes=manifest))
    assert not match.compatible(target, donor)


def test_legacy_none_offsets_and_repaired_chords_match(tmp_path):
    document = chart()
    document["templates"] = [{"frets": [-1, 1, -1, -1, -1, -1]}]
    document["chords"] = [{"t": 1, "chordId": 0}]
    document["notes"] = document["notes"][1:]
    donor = match.inspect_package(package(tmp_path / "old.feedpak", donor=True,
                                          manifest_changes={"offset": None}), donor=True)
    fresh = match.inspect_package(package(tmp_path / "new.feedpak", document=document))
    assert match.compatible(fresh, donor)


def test_sparse_or_ignored_only_chart_cannot_identify_a_song():
    value = chart()
    for note in value["notes"]:
        note["ig"] = True
    with pytest.raises(match.ReuseError, match="Too few"):
        match.event_fingerprint(value, {})


@pytest.mark.parametrize("name", ["../x", "/x", "a\\b", "a//b", "a/./b", "C:x"])
def test_member_paths_are_strict(name):
    with pytest.raises(match.ReuseError):
        match.member_name(name)


def test_duplicate_archive_names_are_rejected(tmp_path):
    path = package(tmp_path / "duplicate.feedpak")
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("MANIFEST.yaml", b"duplicate")
    with pytest.raises(match.ReuseError, match="duplicate"):
        match.inspect_package(path)


def test_unrelated_display_and_difficulty_metadata_is_not_required():
    left, right = chart(), copy.deepcopy(chart())
    right.update(handshapes=[{"arp": True}], phrases=[{"name": "fixed layout"}])
    assert match.event_fingerprint(left, {"id": "lead"}) == match.event_fingerprint(right, {"id": "lead"})


def test_old_missing_role_provenance_matches_corrected_fresh_labels(tmp_path):
    old, fresh = chart(), chart()
    fresh["ext"] = {"source": {"format": "psarc-manifest2014", "arrangement_properties": {
        "pathLead": 1, "pathRhythm": 0, "pathBass": 0, "bonusArr": 1}}}
    donor = match.inspect_package(package(tmp_path / "old.feedpak", donor=True, document=old), donor=True)
    target = match.inspect_package(package(tmp_path / "new.feedpak", document=fresh))
    assert match.compatible(target, donor)
