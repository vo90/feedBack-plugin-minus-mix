from __future__ import annotations

import copy
import zipfile

import pytest

import exporter
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


@pytest.mark.parametrize("stems", [[stem] for stem in exporter.KNOWN_LABELS] + [
    ["guitar", "bass"], ["synth_pad", "lead guitar", "percussion-2"],
])
def test_every_declared_variant_and_custom_ids_are_detected_from_metadata(tmp_path, stems):
    path = package(tmp_path / "misleading (No Banjo).feedpak", donor=True, excluded_stems=stems)
    donor = match.inspect_package(path, donor=True, stem_label=exporter.stem_label)
    assert donor["excluded_stems"] == sorted(stems)
    assert donor["variant_suffix"] == exporter._suffix(sorted(stems))
    assert donor["title"] == "Song"
    assert donor["identity"] == ["artist", "song"]


@pytest.mark.parametrize("value", [None, [], "guitar", {}, [None], [1], [True], [""], [" "],
                                        ["full"], ["guitar", "full"], ["guitar", " GUITAR "], ["bad\nid"]])
def test_malformed_excluded_stem_metadata_is_not_guessed(tmp_path, value):
    path = package(tmp_path / "Song (No Guitar).feedpak", donor=True, manifest_changes={
        "minus_mix": {"excluded_stems": value, "source_title": "Song"},
    })
    with pytest.raises(match.ReuseError, match="excluded_stems"):
        match.inspect_package(path, donor=True)


def test_ids_canonicalize_case_space_and_order_but_variant_identity_stays_distinct(tmp_path):
    variants = []
    for index, stems in enumerate(([" GUITAR ", "Bass"], ["bass", "guitar"], ["vocals"])):
        path = package(tmp_path / f"{index}.feedpak", donor=True, excluded_stems=stems)
        variants.append(match.inspect_package(path, donor=True))
    assert match.audio_identity(variants[0]) == match.audio_identity(variants[1])
    assert match.audio_identity(variants[0]) != match.audio_identity(variants[2])


def test_legacy_suffix_fallback_matches_exact_metadata_order_and_shared_labels(tmp_path):
    stems = ["guitar", "bass", "synth_pad"]
    marker = {"excluded_stems": stems, "generator": "minus_mix"}
    path = package(tmp_path / "renamed.feedpak", donor=True, excluded_stems=stems,
                   manifest_changes={"minus_mix": marker})
    donor = match.inspect_package(path, donor=True, stem_label=exporter.stem_label)
    assert donor["title"] == "Song"
    assert donor["variant_suffix"] == "No Bass + Guitar + Synth Pad"
    marker["excluded_stems"] = ["bass"]
    path = package(path, donor=True, excluded_stems=stems, manifest_changes={"minus_mix": marker})
    with pytest.raises(match.ReuseError, match="metadata-consistent suffix"):
        match.inspect_package(path, donor=True, stem_label=exporter.stem_label)


def test_source_title_is_authoritative_and_ordinary_parenthesized_title_is_preserved(tmp_path):
    title = "Song (No Guitar)"
    fresh = match.inspect_package(package(tmp_path / "fresh.feedpak", title=title))
    donor = match.inspect_package(package(tmp_path / "donor.feedpak", donor=True, title=title,
                                          excluded_stems=["vocals"], manifest_changes={"title": "Renamed"}), donor=True)
    assert fresh["title"] == title == donor["title"]
    assert match.compatible(fresh, donor)


def test_legacy_suffix_uses_supplied_normal_exporter_label_callback():
    manifest = {"title": "Song (No Custom Label)", "minus_mix": {"excluded_stems": ["custom"]}}

    def label(stem):
        assert stem == "custom"
        return "Custom Label"

    assert match.base_title(manifest, stem_label=label) == "Song"
    assert match.variant_suffix(["custom"], stem_label=label) == "No Custom Label"
