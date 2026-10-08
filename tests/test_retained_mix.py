"""Audio regressions for retained-stem rendering, independent of exact subtraction."""
from __future__ import annotations

import zipfile

import pytest
import yaml

import batch
import exporter
from tests.test_batch import _completed, _pak
from tests.test_exporter import FFMPEG, _make_source, _ogg_sine, _run, _tone_amplitude


def rewrite(path, mutate, files=None, remove=()):
    with zipfile.ZipFile(path) as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    manifest = yaml.safe_load(payload.pop("manifest.yaml"))
    mutate(manifest)
    for name in remove:
        payload.pop(name, None)
    payload.update(files or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
        archive.writestr("manifest.yaml", yaml.safe_dump(manifest))


@pytest.mark.parametrize("exclude", [["guitar"], ["guitar", "vocals"], ["bass"]])
def test_partial_saved_set_requests_one_complete_inventory(tmp_path, exclude):
    source = _pak(tmp_path / "partial.feedpak", guitar=True)
    rewrite(source, lambda m: m.update(stems=[s for s in m["stems"] if s["id"] != "piano"]))
    plan = exporter.plan_stems(exporter.inspect_source(source), exclude)
    assert plan.requested == exporter.MIX_STEMS
    assert set(plan.included) == set(exporter.MIX_STEMS) - set(exclude)


def test_selection_rejects_all_excluded_or_unknown(tmp_path):
    info = exporter.inspect_source(_pak(tmp_path / "source.feedpak"))
    with pytest.raises(exporter.ExportError, match="keep at least one"):
        exporter.plan_stems(info, exporter.MIX_STEMS)
    with pytest.raises(exporter.ExportError, match="unsupported"):
        exporter.plan_stems(info, ["instrumental"])


def test_saved_instruments_must_not_alias_the_original_or_each_other(tmp_path):
    source = _pak(tmp_path / "source.feedpak", guitar=True)
    rewrite(source, lambda m: m["stems"][2].update(file="stems/full.ogg"))
    with pytest.raises(exporter.ExportError, match="same audio file"):
        exporter.plan_stems(exporter.inspect_source(source), ["guitar"])


def test_reusing_audio_preserves_method_without_upgrading_legacy(tmp_path):
    import reuse_export
    import reuse_match
    from tests.reuse_fixtures import package

    for method in (None, "retained_stem_sum"):
        root = tmp_path / str(method)
        source = package(root / "fresh.feedpak")
        donor = package(root / "donor.feedpak", donor=True)
        if method:
            rewrite(donor, lambda m, method=method: m["minus_mix"].update(
                render_method=method, render_version=1, included_stems=["bass", "drums", "vocals", "piano", "other"],
            ))
        fresh = reuse_match.inspect_package(source)
        original = reuse_match.inspect_package(donor, donor=True)
        result = reuse_export.export_reuse(fresh, original, root / "out.feedpak",
                                          match=reuse_match, exporter=exporter)
        with zipfile.ZipFile(result["output"]) as archive:
            marker = yaml.safe_load(archive.read("manifest.yaml"))["minus_mix"]
        assert marker.get("render_method") == method


def test_legacy_output_is_preserved_and_numbered_current_output_is_resumable(tmp_path):
    source = _pak(tmp_path / "sources/song.feedpak", guitar=True)
    out = tmp_path / "output"
    out.mkdir()
    legacy = out / "song (No Guitar).feedpak"
    _completed(legacy, source)
    rewrite(legacy, lambda m: m["minus_mix"].pop("render_method"))
    legacy_bytes = legacy.read_bytes()
    scan = batch.scan_sources(exporter, str(source.parent), str(out), ["guitar"])
    assert scan["counts"]["ready"] == 1
    assert scan["items"][0]["output_relative"] == "song (No Guitar) (2).feedpak"
    current = out / scan["items"][0]["output_relative"]
    _completed(current, source)
    again = batch.scan_sources(exporter, str(source.parent), str(out), ["guitar"])
    assert again["counts"]["skipped_existing"] == 1
    assert again["counts"]["ready"] == 0
    assert legacy.read_bytes() == legacy_bytes
    legacy.rename(out / "archived-legacy.feedpak")
    gap = batch.scan_sources(exporter, str(source.parent), str(out), ["guitar"])
    assert gap["counts"]["skipped_existing"] == 1
    assert gap["items"][0]["output_relative"] == current.name
    (out / "archived-legacy.feedpak").rename(legacy)
    rewrite(source, lambda m: m.update(title="Changed source"))
    changed = batch.scan_sources(exporter, str(source.parent), str(out), ["guitar"])
    assert changed["counts"]["ready"] == 1
    assert changed["items"][0]["output_relative"] == "song (No Guitar) (3).feedpak"


@pytest.mark.skipif(not FFMPEG, reason="FFmpeg is required for audio validation")
@pytest.mark.parametrize("exclude", [["guitar"], ["guitar", "vocals"],
                                     ["guitar", "drums", "vocals", "piano", "other"]])
def test_retained_mix_and_preview_exclude_only_selected_frequencies(tmp_path, exclude):
    source = _make_source(tmp_path)
    vocals = tmp_path / "vocals.ogg"
    _ogg_sine(vocals, 880)
    rewrite(source, lambda m: None, {"stems/vocals.ogg": vocals.read_bytes()})
    original = source.read_bytes()
    out = tmp_path / "outputs"
    out.mkdir()
    result = exporter.export_minus_mix(source, out, exclude)
    assert not result.temporary_separation_used
    with zipfile.ZipFile(result.output_path) as archive:
        marker = yaml.safe_load(archive.read("manifest.yaml"))["minus_mix"]
        assert marker["render_method"] == "retained_stem_sum"
        assert set(marker["included_stems"]) == set(exporter.MIX_STEMS) - set(exclude)
        for name in ("stems/full.ogg", "preview.ogg"):
            rendered = tmp_path / name.replace("/", "-")
            rendered.write_bytes(archive.read(name))
            bass = _tone_amplitude(rendered, 110)
            assert bass > 0.01
            assert _tone_amplitude(rendered, 440) < bass / 100
            vocal = _tone_amplitude(rendered, 880)
            assert vocal < bass / 100 if "vocals" in exclude else vocal > bass / 2
    assert source.read_bytes() == original
    assert exporter.is_current_output(result.output_path, source, exclude)


@pytest.mark.skipif(not FFMPEG, reason="FFmpeg is required for audio validation")
def test_silent_retained_stems_are_valid_and_keep_exact_timeline(tmp_path):
    source = _make_source(tmp_path)
    silent = tmp_path / "silent.ogg"
    rewrite(source, lambda m: None, {"stems/bass.ogg": silent.read_bytes()})
    out = tmp_path / "outputs"
    out.mkdir()
    result = exporter.export_minus_mix(source, out, ["guitar"])
    with zipfile.ZipFile(result.output_path) as archive:
        audio = tmp_path / "rendered.ogg"
        audio.write_bytes(archive.read("stems/full.ogg"))
    info = exporter._analyze_audio(FFMPEG, audio)
    assert info.peak == 0
    assert info.frames == 2 * 44100


@pytest.mark.skipif(not FFMPEG, reason="FFmpeg is required for audio validation")
@pytest.mark.parametrize("damage", ["short", "corrupt"])
def test_invalid_retained_audio_never_publishes_or_falls_back(tmp_path, damage):
    source = _make_source(tmp_path)
    short = tmp_path / "short.ogg"
    _ogg_sine(short, 110, duration=1)
    data = short.read_bytes() if damage == "short" else b"not audio"
    rewrite(source, lambda m: None, {"stems/bass.ogg": data})
    out = tmp_path / "outputs"
    out.mkdir()
    with pytest.raises(exporter.ExportError, match="durations differ|invalid or incomplete"):
        exporter.export_minus_mix(source, out, ["guitar"])
    assert not list(out.iterdir())


@pytest.mark.skipif(not FFMPEG, reason="FFmpeg is required for audio validation")
def test_hot_sum_uses_constant_attenuation_without_clipping(tmp_path):
    source = _make_source(tmp_path)
    hot = tmp_path / "hot.wav"
    _run([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
          "sine=frequency=110:duration=2:sample_rate=44100", "-af", "volume=6",
          "-c:a", "pcm_f32le", str(hot)])
    def update(manifest):
        for stem in manifest["stems"]:
            if stem["id"] in ("bass", "drums"):
                stem["file"] = f"stems/{stem['id']}.wav"
    rewrite(source, update, {"stems/bass.wav": hot.read_bytes(), "stems/drums.wav": hot.read_bytes()})
    out = tmp_path / "outputs"
    out.mkdir()
    result = exporter.export_minus_mix(source, out, ["guitar"])
    with zipfile.ZipFile(result.output_path) as archive:
        marker = yaml.safe_load(archive.read("manifest.yaml"))["minus_mix"]
        audio = tmp_path / "rendered.ogg"
        audio.write_bytes(archive.read("stems/full.ogg"))
    info = exporter._analyze_audio(FFMPEG, audio)
    assert 0 < marker["output_gain"] < 0.7
    assert 0.8 < info.peak <= 0.99
    assert info.frames == 88200


@pytest.mark.skipif(not FFMPEG, reason="FFmpeg is required for audio validation")
def test_complete_saved_set_without_original_mix_can_export(tmp_path):
    source = _make_source(tmp_path)
    rewrite(source, lambda m: (m.update(stems=[s for s in m["stems"] if s["id"] != "full"]),
                              m.pop("original_audio", None)), remove=["stems/full.ogg"])
    out = tmp_path / "outputs"
    out.mkdir()
    result = exporter.export_minus_mix(source, out, ["guitar"])
    assert result.output_path.is_file()
    assert not result.temporary_separation_used


@pytest.mark.skipif(not FFMPEG, reason="FFmpeg is required for audio validation")
@pytest.mark.parametrize("channels", [1, 2])
def test_server_stem_rate_and_channels_are_converted_to_original_timeline(tmp_path, channels):
    source = _make_source(tmp_path)
    full = tmp_path / "original-48k.wav"
    _run([FFMPEG, "-v", "error", "-i", str(tmp_path / "full.ogg"),
          "-ar", "48000", "-ac", str(channels), "-c:a", "pcm_f32le", str(full)])
    bass = tmp_path / "bass-other-layout.wav"
    _run([FFMPEG, "-v", "error", "-i", str(tmp_path / "bass.ogg"),
          "-ac", "2" if channels == 1 else "1", "-c:a", "pcm_f32le", str(bass)])
    def update(manifest):
        for stem in manifest["stems"]:
            if stem["id"] == "full":
                stem["file"] = "stems/full.wav"
            if stem["id"] == "bass":
                stem["file"] = "stems/bass.wav"
        manifest.pop("original_audio", None)
    rewrite(source, update, {"stems/full.wav": full.read_bytes(), "stems/bass.wav": bass.read_bytes()},
            remove=["stems/full.ogg", "stems/bass.ogg"])
    out = tmp_path / "outputs"
    out.mkdir()
    result = exporter.export_minus_mix(source, out, ["guitar"])
    with zipfile.ZipFile(result.output_path) as archive:
        audio = tmp_path / "rendered.ogg"
        audio.write_bytes(archive.read("stems/full.ogg"))
    info = exporter._analyze_audio(FFMPEG, audio)
    assert info.timeline == (48000, channels, 96000)
    assert _tone_amplitude(audio, 440) < _tone_amplitude(audio, 110) / 100
