"""Decode real integer stems without weakening non-finite audio rejection."""
import struct

import pytest

import exporter
from tests.test_exporter import FFMPEG, _run

pytestmark = pytest.mark.skipif(not FFMPEG, reason="FFmpeg is required for audio validation")


@pytest.mark.parametrize("suffix,codec,sample_fmt", [
    ("flac", "flac", "s16"),
    ("flac", "flac", "s32"),
    ("wav", "pcm_s16le", "s16"),
    ("wav", "pcm_s32le", "s32"),
])
@pytest.mark.parametrize("silent", [False, True])
def test_integer_audio_preserves_timeline_and_accepts_silence(tmp_path, suffix, codec,
                                                           sample_fmt, silent):
    audio = tmp_path / ("stem." + suffix)
    signal = "anullsrc=r=48000:cl=stereo" if silent else "sine=frequency=220:sample_rate=48000"
    _run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", signal, "-t", "0.25",
          "-ac", "2", "-c:a", codec, "-sample_fmt", sample_fmt, str(audio)])

    info = exporter._analyze_audio(FFMPEG, audio)

    assert info.timeline == (48000, 2, 12000)
    assert info.peak == 0 if silent else 0 < info.peak < 1


@pytest.mark.parametrize("invalid_sample", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_float_audio_is_still_rejected(tmp_path, invalid_sample):
    audio = tmp_path / "invalid.wav"
    samples = struct.pack("<4d", 0.0, 0.25, invalid_sample, -0.25)
    # IEEE float WAV, 64-bit mono. Do not pass through an encoder that might
    # sanitize non-finite samples before the validator sees them.
    fmt = struct.pack("<HHIIHH", 3, 1, 48000, 48000 * 8, 8, 64)
    audio.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVE"
                     + b"fmt " + struct.pack("<I", len(fmt)) + fmt
                     + b"data" + struct.pack("<I", len(samples)) + samples)

    with pytest.raises(exporter.ExportError, match="invalid or incomplete audio"):
        exporter._analyze_audio(FFMPEG, audio)
