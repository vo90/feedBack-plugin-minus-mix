"""Real Vorbis renders must preserve odd sample counts without timestamp rounding."""
import pytest

import exporter
from tests.test_exporter import FFMPEG, FFPROBE, _run, _timestamp_regressions

pytestmark = pytest.mark.skipif(not FFMPEG, reason="FFmpeg is required for audio rendering")


@pytest.mark.parametrize("combined", [False, True])
def test_44100hz_render_preserves_exact_sample_count(tmp_path, combined):
    # This length exposes a one-sample loss when N/SR/TB rounds a packet's PTS
    # down before Vorbis encoding. Use synthesized audio, never library content.
    frames = 8_087_014
    rate = 44100
    source = tmp_path / "sum.wav"
    output = tmp_path / "backing.ogg"
    preview = tmp_path / "preview.ogg"
    _run([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
          f"sine=frequency=220:sample_rate={rate}:duration=184",
          "-af", f"atrim=end_sample={frames}", "-ac", "2", "-c:a", "pcm_f32le", str(source)])
    assert exporter._analyze_audio(FFMPEG, source).timeline == (rate, 2, frames)

    if combined:
        assert exporter._render_mix_and_preview(
            FFMPEG, source, output, preview, frames / rate,
        )
    else:
        exporter._render_mix(FFMPEG, source, output)
        assert exporter._render_preview(FFMPEG, output, preview, frames / rate)

    assert exporter._analyze_audio(FFMPEG, output).timeline == (rate, 2, frames)
    assert exporter._analyze_audio(FFMPEG, preview).timeline == (rate, 2, 30 * rate)
    if FFPROBE:
        for audio in (output, preview):
            assert _timestamp_regressions(audio) == {"pts": 0, "dts": 0}
