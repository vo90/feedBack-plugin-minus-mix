"""MinusMix: real-audio and package-safety integration tests."""
from __future__ import annotations

import array
import hashlib
import json
import math
import multiprocessing
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
import yaml
from audio import _ffmpeg_cmd

import exporter
from tests.publish_worker import publish_worker

FFMPEG = _ffmpeg_cmd()

def _run(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")[-1000:]


def _ogg_sine(path: Path, frequency: int, duration: float = 2.0) -> None:
    _run([
        FFMPEG, "-hide_banner", "-nostdin", "-y",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={duration}:sample_rate=44100",
        "-c:a", "libvorbis", "-q:a", "7", str(path),
    ])


def _ogg_two_sines(path: Path, low: int = 110, high: int = 440, duration: float = 2.0) -> None:
    _run([
        FFMPEG, "-hide_banner", "-nostdin", "-y",
        "-f", "lavfi", "-i", f"sine=frequency={low}:duration={duration}:sample_rate=44100",
        "-f", "lavfi", "-i", f"sine=frequency={high}:duration={duration}:sample_rate=44100",
        "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[mix]",
        "-map", "[mix]", "-c:a", "libvorbis", "-q:a", "7", str(path),
    ])


def _make_source(tmp_path: Path, *, unsafe_member: bool = False, include_guitar: bool = True) -> Path:
    full = tmp_path / "full.ogg"
    guitar = tmp_path / "guitar.ogg"
    preview = tmp_path / "old-preview.ogg"
    _ogg_two_sines(full)
    _ogg_sine(guitar, 440)
    _ogg_sine(preview, 440, duration=1.0)
    manifest = {
        "feedpak_version": "1.14.0",
        "title": "Test Song",
        "artist": "Test Artist",
        "duration": 2.0,
        "arrangements": [{"id": "lead", "name": "Lead", "file": "arrangements/lead.json", "type": "guitar"}],
        "stems": ([{"id": "guitar", "file": "stems/guitar.ogg", "default": True}]
                  if include_guitar else []) + [
            {"id": "full", "file": "stems/full.ogg", "codec": "vorbis", "default": True},
        ],
        "preview": "preview.ogg",
        # Legacy input is supported, but a new pack must not perpetuate it.
        "original_audio": "stems/full.ogg",
        "stem_separation": {"engine": "bs-roformer", "model": "bs_roformer_sw", "version": "1.0.0"},
    }
    source = tmp_path / "Test Artist - Test Song.feedpak"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr("manifest.yaml", yaml.safe_dump(manifest, sort_keys=False))
        zf.writestr("arrangements/lead.json", json.dumps({"events": [{"time": 1.25, "fret": 3}]}))
        zf.writestr("cover.png", b"unchanged-cover-bytes")
        zf.write(full, "stems/full.ogg", compress_type=zipfile.ZIP_STORED)
        if include_guitar:
            zf.write(guitar, "stems/guitar.ogg", compress_type=zipfile.ZIP_STORED)
        zf.write(preview, "preview.ogg", compress_type=zipfile.ZIP_STORED)
        if unsafe_member:
            zf.writestr("../outside.txt", b"must not be preserved")
    return source


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tone_amplitude(audio: Path, frequency: float) -> float:
    result = subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(audio),
        "-f", "f32le", "-ac", "1", "-ar", "44100", "-",
    ], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    samples = array.array("f")
    samples.frombytes(result.stdout)
    # Ignore codec edge samples. Projection at an integer-Hz tone over nearly
    # two seconds is enough to distinguish retained bass from removed guitar.
    samples = samples[1000:-1000]
    omega = 2.0 * math.pi * frequency / 44100.0
    re = sum(sample * math.cos(omega * i) for i, sample in enumerate(samples))
    im = sum(sample * math.sin(omega * i) for i, sample in enumerate(samples))
    return 2.0 * math.hypot(re, im) / len(samples)


def test_atomic_publish_retries_if_first_output_appears_during_publication(
        tmp_path, monkeypatch):
    output_dir = tmp_path / "exports"
    output_dir.mkdir()
    source = tmp_path / "Artist - Song.feedpak"
    output_tmp = output_dir / ".complete.tmp"
    output_tmp.write_bytes(b"complete archive")
    desired = exporter.desired_output_path(
        output_dir, source, suffix="No Guitar",
    )
    real_link = exporter.os.link
    raced = False

    def racing_link(source_path, destination_path):
        nonlocal raced
        if not raced:
            raced = True
            Path(destination_path).write_bytes(b"another process")
            raise FileExistsError("destination appeared during publication")
        return real_link(source_path, destination_path)

    monkeypatch.setattr(exporter.os, "link", racing_link)

    published = exporter._publish_unique_output(
        output_tmp, output_dir, source, "No Guitar",
    )

    assert desired.read_bytes() == b"another process"
    assert published.name == "Artist - Song (No Guitar) (2).feedpak"
    assert published.read_bytes() == b"complete archive"
    assert not output_tmp.exists()


def test_atomic_publish_is_no_replace_across_processes(tmp_path):
    output_dir = tmp_path / "exports"
    output_dir.mkdir()
    source = tmp_path / "Artist - Song.feedpak"
    first_tmp = output_dir / ".first.tmp"
    second_tmp = output_dir / ".second.tmp"
    first_tmp.write_bytes(b"first complete archive")
    second_tmp.write_bytes(b"second complete archive")
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=publish_worker,
            args=(str(temp), str(output_dir), str(source), ready, start, results),
        )
        for temp in (first_tmp, second_tmp)
    ]
    for worker in workers:
        worker.start()
    assert ready.get(timeout=10) is True
    assert ready.get(timeout=10) is True
    start.set()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0

    published = [results.get(timeout=5) for _worker in workers]
    assert all(error is None for _name, _content, error in published)
    assert {name for name, _content, _error in published} == {
        "Artist - Song (No Guitar).feedpak",
        "Artist - Song (No Guitar) (2).feedpak",
    }
    assert {content for _name, content, _error in published} == {
        "first complete archive", "second complete archive",
    }


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is required for a real-audio export")
def test_export_removes_selected_audio_preserves_assets_and_never_mutates_source(tmp_path):
    source = _make_source(tmp_path)
    source_hash = _sha256(source)
    out_dir = tmp_path / "exports"
    out_dir.mkdir()

    result = exporter.export_minus_mix(source, out_dir, ["guitar"])

    assert _sha256(source) == source_hash
    assert result.output_path.parent == out_dir
    assert result.output_filename == "Test Artist - Test Song (No Guitar).feedpak"
    assert result.preview_created is True
    assert not list(out_dir.glob("*.tmp"))

    with zipfile.ZipFile(result.output_path) as zf:
        names = zf.namelist()
        assert len(names) == len(set(names))
        assert "stems/full.ogg" in names
        assert "stems/guitar.ogg" not in names
        assert "preview.ogg" in names
        assert zf.read("cover.png") == b"unchanged-cover-bytes"
        assert json.loads(zf.read("arrangements/lead.json"))["events"][0]["time"] == 1.25
        manifest = yaml.safe_load(zf.read("manifest.yaml"))
        assert manifest["title"] == "Test Song (No Guitar)"
        assert manifest["stems"] == [{
            "id": "full", "file": "stems/full.ogg", "codec": "vorbis", "default": True,
        }]
        assert "original_audio" not in manifest
        assert manifest["minus_mix"]["excluded_stems"] == ["guitar"]
        assert manifest["minus_mix"]["source_title"] == "Test Song"
        assert manifest["minus_mix"]["generator"] == "minus_mix"
        assert manifest["stem_separation"]["model"] == "bs_roformer_sw"
        rendered = tmp_path / "rendered.ogg"
        rendered.write_bytes(zf.read("stems/full.ogg"))
        rendered_preview = tmp_path / "rendered-preview.ogg"
        rendered_preview.write_bytes(zf.read("preview.ogg"))

    low = _tone_amplitude(rendered, 110)
    high = _tone_amplitude(rendered, 440)
    assert low > 0.05
    assert high < low / 8.0
    preview_low = _tone_amplitude(rendered_preview, 110)
    preview_high = _tone_amplitude(rendered_preview, 440)
    assert preview_low > 0.01
    assert preview_high < preview_low / 6.0

    # A second export chooses a new name; it never asks whether overwriting is OK.
    second = exporter.export_minus_mix(source, out_dir, ["guitar"])
    assert second.output_filename == "Test Artist - Test Song (No Guitar) (2).feedpak"
    assert result.output_path.is_file() and second.output_path.is_file()
    assert _sha256(source) == source_hash


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is required for a real-audio export")
def test_export_parses_manifest_once(tmp_path, monkeypatch):
    source = _make_source(tmp_path)
    out_dir = tmp_path / "exports"
    out_dir.mkdir()
    real_manifest = exporter._manifest
    calls = 0

    def counted_manifest(path):
        nonlocal calls
        calls += 1
        return real_manifest(path)

    monkeypatch.setattr(exporter, "_manifest", counted_manifest)
    result = exporter.export_minus_mix(source, out_dir, ["guitar"])

    assert result.output_path.is_file()
    assert calls == 1


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is required for a real-audio export")
def test_single_stem_source_uses_temporary_separator_and_discards_its_outputs(tmp_path):
    source = _make_source(tmp_path, include_guitar=False)
    source_hash = _sha256(source)
    out_dir = tmp_path / "exports"
    out_dir.mkdir()
    temporary_dirs: list[Path] = []

    class TemporaryProvider:
        @staticmethod
        def obtain(full_mix: Path, separation_dir: Path,
                   requested: tuple[str, ...], full_digest: str | None):
            assert full_mix.is_file()
            assert requested == ("guitar",)
            assert full_digest == _sha256(full_mix)
            temporary_dirs.append(separation_dir)
            guitar = separation_dir / "guitar.ogg"
            _ogg_sine(guitar, 440)
            # A six-stem engine may also return files the exporter did not request.
            other = separation_dir / "drums.ogg"
            _ogg_sine(other, 220)
            return {"guitar": guitar, "drums": other}

    result = exporter.export_minus_mix(
        source, out_dir, ["guitar"], stem_provider=TemporaryProvider(),
    )

    assert result.temporary_separation_used is True
    assert _sha256(source) == source_hash
    assert temporary_dirs and all(not path.exists() for path in temporary_dirs)
    with zipfile.ZipFile(result.output_path) as zf:
        names = zf.namelist()
        assert "stems/full.ogg" in names
        assert "stems/guitar.ogg" not in names
        assert "stems/drums.ogg" not in names
        manifest = yaml.safe_load(zf.read("manifest.yaml"))
        assert [stem["id"] for stem in manifest["stems"]] == ["full"]
        rendered = tmp_path / "direct-rendered.ogg"
        rendered.write_bytes(zf.read("stems/full.ogg"))
    assert _tone_amplitude(rendered, 440) < _tone_amplitude(rendered, 110) / 8.0


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is required for export validation")
def test_export_refuses_unsafe_source_archive_and_leaves_no_partial_output(tmp_path):
    source = _make_source(tmp_path, unsafe_member=True)
    source_hash = _sha256(source)
    out_dir = tmp_path / "exports"
    out_dir.mkdir()

    with pytest.raises(exporter.ExportError, match="unsafe member path"):
        exporter.export_minus_mix(source, out_dir, ["guitar"])

    assert _sha256(source) == source_hash
    assert list(out_dir.iterdir()) == []


def test_source_inspection_and_selection_validation(tmp_path, monkeypatch):
    source = tmp_path / "song.feedpak"
    manifest = {
        "title": "Song",
        "minus_mix": {
            "excluded_stems": ["guitar"],
            "generator": "minus_mix",
        },
        "stems": [
            {"id": "full", "file": "stems/full.ogg"},
            {"id": "guitar", "file": "stems/guitar.ogg"},
        ],
    }
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr("manifest.yaml", yaml.safe_dump(manifest))
        zf.writestr("stems/full.ogg", b"fake")
        zf.writestr("stems/guitar.ogg", b"fake")

    info = exporter.inspect_source(source)
    assert info.full_mix_file == "stems/full.ogg"
    assert [stem.id for stem in info.stems] == ["full", "guitar"]
    assert info.derived_exclusions == ("guitar",)

    monkeypatch.setattr(exporter, "_ffmpeg_cmd", lambda: "ffmpeg")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with pytest.raises(exporter.ExportError, match="at least one"):
        exporter.export_minus_mix(source, out_dir, ["full"])
    with pytest.raises(exporter.ExportError, match="start Stem Splitter"):
        exporter.export_minus_mix(source, out_dir, ["vocals"])


def test_ogg_process_is_terminated_at_cancellation_checkpoint(tmp_path):
    class StopExport(RuntimeError):
        pass

    started = time.monotonic()
    with pytest.raises(StopExport):
        exporter._run_ogg_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path / "never-created.ogg",
            cancel_cb=lambda: (_ for _ in ()).throw(StopExport("stop")),
        )

    assert time.monotonic() - started < 5.0
    assert not (tmp_path / "never-created.ogg").exists()
