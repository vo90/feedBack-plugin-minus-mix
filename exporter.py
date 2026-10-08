"""Safe, non-destructive single-stem practice-feedpak rendering.

The Stem Splitter produces standard feedpak stem entries.  This module consumes
that public format instead of importing Stem Splitter internals, so an already
split pack remains exportable even when the splitter is disabled or replaced.

Every export is a NEW zip-form ``.feedpak``.  The source is opened read-only,
the output is built beside its final destination, and the final name is made
unique rather than overwriting anything.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

import sloppak
import yaml
from audio import _ffmpeg_cmd, _scrub_paths

MANIFEST_NAMES = ("manifest.yaml", "manifest.yml")
FULL_MIX_REL = "stems/full.ogg"
PREVIEW_REL = "preview.ogg"
KNOWN_LABELS = {
    "guitar": "Guitar",
    "bass": "Bass",
    "drums": "Drums",
    "vocals": "Vocals",
    "piano": "Piano",
    "other": "Other",
}
MIX_STEMS = tuple(KNOWN_LABELS)
RENDER_METHOD = "retained_stem_sum"
RENDER_VERSION = 1
_ARCHIVE_EXTS = {".feedpak", ".sloppak"}
_ALREADY_COMPRESSED = {".ogg", ".mp3", ".flac", ".png", ".jpg", ".jpeg", ".webp", ".zip"}
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_EXPORT_LOCK = threading.Lock()


class ExportError(RuntimeError):
    """An expected, user-actionable export refusal."""


def validate_output_directory(output_dir: Path) -> Path:
    """Resolve an output folder and prove that a temporary file can be published there."""
    output_dir = Path(output_dir).resolve()
    if not output_dir.is_absolute() or not output_dir.is_dir():
        raise ExportError("choose an existing output folder")

    fd: int | None = None
    probe_path: Path | None = None
    try:
        fd, probe_name = tempfile.mkstemp(
            prefix=".minus-mix-write-test-", suffix=".tmp", dir=output_dir,
        )
        probe_path = Path(probe_name)
        os.close(fd)
        fd = None
        probe_path.unlink()
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if probe_path is not None:
            try:
                probe_path.unlink()
            except OSError:
                pass
        raise ExportError(
            "MinusMix cannot write to the chosen output folder; "
            "choose another folder or update its permissions"
        ) from exc
    return output_dir


@dataclass(frozen=True)
class StemInfo:
    id: str
    file: str


@dataclass(frozen=True)
class SourceInfo:
    title: str
    artist: str
    stems: tuple[StemInfo, ...]
    arrangements: tuple[dict, ...]
    full_mix_file: str
    derived_exclusions: tuple[str, ...]


@dataclass(frozen=True)
class StemPlan:
    included: tuple[str, ...]
    requested: tuple[str, ...]


def plan_stems(info: SourceInfo, exclusions: Iterable[str]) -> StemPlan:
    selected = _selected_exclusions(exclusions)
    saved = {stem.id for stem in info.stems if stem.id != "full"}
    unknown = saved - set(MIX_STEMS)
    if unknown:
        raise ExportError("ambiguous saved stem set: " + ", ".join(sorted(unknown)))
    files = [stem.file for stem in info.stems]
    if len(files) != len(set(files)):
        raise ExportError("ambiguous stem set: different instruments reference the same audio file")
    included = tuple(stem for stem in MIX_STEMS if stem not in selected)
    # Never splice an older partial set together with a new separation. Asking
    # for the complete inventory also proves the model separates excluded parts
    # (a four-stem model's 'other' track can still contain all of the guitar).
    requested = () if set(MIX_STEMS).issubset(saved) else MIX_STEMS
    if requested and not info.full_mix_file:
        raise ExportError("incomplete saved stems and no original full mix for separation")
    return StemPlan(included, requested)


@dataclass(frozen=True)
class PreparedSource:
    source: Path
    manifest: dict
    info: SourceInfo
    stem_map: dict[str, StemInfo]
    signature: tuple[int, int] | None


@dataclass(frozen=True)
class ExportResult:
    output_path: Path
    output_filename: str
    title: str
    excluded_stems: tuple[str, ...]
    preview_created: bool
    temporary_separation_used: bool


def _member_name(raw: str) -> str:
    """Validate and canonicalise a pack-relative member name.

    Backslashes are rejected rather than normalised: in a zip they are legal
    bytes but ambiguous path separators on Windows, which is exactly where an
    otherwise harmless preserved entry can become traversal on extraction.
    """
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ExportError("the source feedpak contains an invalid member path")
    p = PurePosixPath(raw)
    if p.is_absolute() or any(part in ("", ".", "..") for part in p.parts):
        raise ExportError("the source feedpak contains an unsafe member path")
    return p.as_posix()


def _manifest(source: Path) -> dict:
    try:
        manifest = sloppak.load_manifest(source) or {}
    except Exception as exc:
        raise ExportError("the source feedpak manifest could not be read") from exc
    if not isinstance(manifest, dict):
        raise ExportError("the source feedpak manifest is not an object")
    return manifest


def _stem_map(manifest: dict) -> dict[str, StemInfo]:
    out: dict[str, StemInfo] = {}
    for entry in manifest.get("stems") or []:
        if not isinstance(entry, dict):
            continue
        stem_id = str(entry.get("id") or "").strip().lower()
        rel = entry.get("file")
        if not stem_id or not isinstance(rel, str) or not rel.strip():
            continue
        if stem_id in out:
            raise ExportError(f"the source feedpak declares the '{stem_id}' stem more than once")
        out[stem_id] = StemInfo(stem_id, _member_name(rel.strip()))
    return out


def _source_info(source: Path, manifest: dict,
                 stems: dict[str, StemInfo]) -> SourceInfo:
    full = stems.get("full")
    # Read the deprecated key only for old packs; new output never writes it.
    if full is None:
        legacy = manifest.get("original_audio")
        if isinstance(legacy, str) and legacy.strip():
            full = StemInfo("full", _member_name(legacy.strip()))
    if full is None and not set(MIX_STEMS).issubset(stems):
        raise ExportError("the source feedpak has no full mix and no complete saved stem set")
    instruments = tuple(s for sid, s in stems.items() if sid != "full")
    arrangements = tuple(a for a in (manifest.get("arrangements") or []) if isinstance(a, dict))
    derived = manifest.get("minus_mix")
    derived_exclusions: tuple[str, ...] = ()
    if isinstance(derived, dict) and isinstance(derived.get("excluded_stems"), list):
        derived_exclusions = tuple(
            str(stem).strip().lower() for stem in derived["excluded_stems"]
            if isinstance(stem, str) and stem.strip()
        )
    return SourceInfo(
        title=str(manifest.get("title") or source.stem),
        artist=str(manifest.get("artist") or ""),
        stems=((full,) if full else ()) + instruments,
        arrangements=arrangements,
        full_mix_file=full.file if full else "",
        derived_exclusions=derived_exclusions,
    )


def prepare_source(source: Path) -> PreparedSource:
    source = Path(source).resolve()
    if not source.exists() or (source.is_file() and source.suffix.lower() not in _ARCHIVE_EXTS):
        raise ExportError("the selected source is not a feedpak")
    manifest = _manifest(source)
    stems = _stem_map(manifest)
    stat = source.stat() if source.is_file() else None
    return PreparedSource(
        source=source,
        manifest=manifest,
        info=_source_info(source, manifest, stems),
        stem_map=stems,
        signature=(stat.st_size, stat.st_mtime_ns) if stat else None,
    )


def inspect_source(source: Path) -> SourceInfo:
    return prepare_source(source).info


def source_fingerprint(source: Path, cancel_cb=None) -> str:
    digest = hashlib.sha256()
    entries = _source_entries(source) if source.is_dir() else [("", source)]
    for name, path in entries:
        if source.is_dir():
            digest.update(name.encode("utf-8") + b"\0")
            digest.update(str(path.stat().st_size).encode("ascii") + b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                _checkpoint(cancel_cb)
                digest.update(chunk)
    return digest.hexdigest()


def is_current_output(output: Path, source: Path, exclusions, source_digest=None) -> bool:
    """Legacy subtraction outputs are preserved, but never count as this render."""
    try:
        selected = _selected_exclusions(exclusions)
        manifest = _manifest(output)
        marker = manifest.get("minus_mix") or {}
        if (marker.get("render_method") != RENDER_METHOD
                or marker.get("render_version") != RENDER_VERSION
                or set(marker.get("excluded_stems", [])) != set(selected)
                or set(marker.get("included_stems", [])) != set(MIX_STEMS) - set(selected)
                or marker.get("source_sha256") != (source_digest or source_fingerprint(source))):
            return False
        with zipfile.ZipFile(output) as archive:
            return archive.getinfo(FULL_MIX_REL).file_size > 100 and archive.testzip() is None
    except (OSError, ValueError, KeyError, AttributeError, TypeError, zipfile.BadZipFile, ExportError):
        return False


class SourcePackage:
    """Open one validated source package for a related set of member reads."""

    def __init__(self, source: Path):
        self.source = Path(source)
        self._zip: zipfile.ZipFile | None = None
        self._members: dict[str, zipfile.ZipInfo] = {}

    def __enter__(self):
        if not self.source.is_file():
            return self
        try:
            self._zip = zipfile.ZipFile(self.source, "r")
            for info in self._zip.infolist():
                raw = info.filename.rstrip("/")
                if not raw or info.is_dir():
                    continue
                name = _member_name(raw)
                if name in self._members:
                    raise ExportError(
                        f"the source feedpak contains duplicate member '{name}'"
                    )
                self._members[name] = info
        except zipfile.BadZipFile as exc:
            raise ExportError("the source feedpak is not a valid zip archive") from exc
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    @staticmethod
    def _copy_stream(src, dst, hasher) -> None:
        while True:
            block = src.read(1024 * 1024)
            if not block:
                return
            dst.write(block)
            if hasher is not None:
                hasher.update(block)

    def copy_member(self, rel: str, destination: Path, *,
                    calculate_digest: bool = False) -> str | None:
        rel = _member_name(rel)
        destination.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256() if calculate_digest else None
        if self.source.is_file():
            info = self._members.get(rel)
            if self._zip is None or info is None:
                raise ExportError(f"audio file '{rel}' is missing from the source feedpak")
            with self._zip.open(info, "r") as src, destination.open("wb") as dst:
                self._copy_stream(src, dst, hasher)
            return hasher.hexdigest() if hasher is not None else None

        root = self.source.resolve()
        target = (self.source / Path(*PurePosixPath(rel).parts)).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ExportError(f"audio file '{rel}' escapes the source feedpak") from exc
        if not target.is_file():
            raise ExportError(f"audio file '{rel}' is missing from the source feedpak")
        with target.open("rb") as src, destination.open("wb") as dst:
            self._copy_stream(src, dst, hasher)
        return hasher.hexdigest() if hasher is not None else None


def _ffmpeg_detail(stderr: bytes, *paths: Path) -> str:
    text = (stderr or b"").decode("utf-8", "replace")
    text = _scrub_paths(text, *(str(p) for p in paths))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[-1] if lines else "unknown ffmpeg error")[:500]


def _audio_process(command, cancel_cb=None, timeout=1800):
    """Bounded diagnostics and cancellation for both analysis and rendering."""
    started = time.monotonic()
    with tempfile.TemporaryFile() as error_log:
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=error_log, **flags)
        try:
            while proc.poll() is None:
                _checkpoint(cancel_cb)
                if time.monotonic() - started > timeout:
                    raise ExportError("audio rendering timed out")
                time.sleep(0.1)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        error_log.seek(0, os.SEEK_END)
        error_log.seek(max(0, error_log.tell() - 64 * 1024))
        return proc.returncode, error_log.read()


@dataclass(frozen=True)
class AudioInfo:
    rate: int
    channels: int
    frames: int
    peak: float

    @property
    def timeline(self):
        return self.rate, self.channels, self.frames


def _analyze_audio(ffmpeg, path, cancel_cb=None):
    """Decode fully, including silent tracks, without depending on ffprobe."""
    code, raw = _audio_process([
        ffmpeg, "-hide_banner", "-nostdin", "-xerror", "-i", str(path),
        "-map", "0:a:0", "-af", "ashowinfo,astats=measure_perchannel=none:"
        "measure_overall=Peak_level+Number_of_samples+Number_of_NaNs+Number_of_Infs:reset=0",
        "-f", "null", "-",
    ], cancel_cb)
    text = raw.decode("utf-8", "replace")
    layout = re.findall(r"channels:(\d+).*?rate:(\d+) nb_samples:", text)
    def metric(label):
        values = re.findall(re.escape(label) + r":\s*([^\s]+)", text)
        return float(values[-1]) if values else float("nan")
    frames, peak = metric("Number of samples"), metric("Peak level dB")
    if (code or not layout or not math.isfinite(frames) or frames <= 0
            or math.isnan(peak) or peak == float("inf")
            or metric("Number of NaNs") != 0 or metric("Number of Infs") != 0):
        raise ExportError("invalid or incomplete audio: " + _ffmpeg_detail(raw, path))
    channels, rate = map(int, layout[-1])
    if channels not in (1, 2):
        raise ExportError("MinusMix requires mono or stereo stems")
    return AudioInfo(rate, channels, int(frames), 10 ** (peak / 20))


def _sum_stems(ffmpeg, retained, output, reference, cancel_cb):
    if not retained:
        raise ExportError("keep at least one instrument stem in the backing track")
    infos = [_analyze_audio(ffmpeg, path, cancel_cb) for path in retained]
    expected = _analyze_audio(ffmpeg, reference, cancel_cb) if reference.is_file() else infos[0]
    # Separators commonly emit 44.1 kHz stereo even for 48 kHz or mono input.
    # Permit at most 1 ms of codec/resampling rounding at the tail, never a
    # materially shortened stem, and preserve the original decoded frame count.
    tolerance = max(2, math.ceil(expected.rate / 1000))
    if any(abs(round(info.frames * expected.rate / info.rate) - expected.frames) > tolerance
           for info in infos):
        raise ExportError("stem decoded durations differ; re-split the original song")
    command = [ffmpeg, "-hide_banner", "-nostdin", "-xerror", "-y"]
    for path in retained:
        command.extend(["-i", str(path)])
    inputs = ""
    for index, info in enumerate(infos):
        channels = ""
        if info.channels != expected.channels:
            channels = ("pan=mono|c0=0.5*c0+0.5*c1," if expected.channels == 1
                        else "pan=stereo|c0=c0|c1=c0,")
        inputs += (f"[{index}:a]aresample={expected.rate},{channels}"
                   f"apad=whole_len={expected.frames},atrim=end_sample={expected.frames},"
                   f"asetpts=N/SR/TB[s{index}];")
    inputs += "".join(f"[s{index}]" for index in range(len(retained)))
    graph = inputs + (f"amix=inputs={len(retained)}:duration=longest:"
                      "dropout_transition=0:normalize=0,asetpts=N/SR/TB[out]")
    command.extend(["-filter_complex", graph, "-map", "[out]", "-map_metadata", "-1",
                    "-c:a", "pcm_f32le", "-rf64", "auto", str(output)])
    code, detail = _audio_process(command, cancel_cb)
    if code:
        raise ExportError("could not mix retained stems: " + _ffmpeg_detail(detail, *retained))
    mixed = _analyze_audio(ffmpeg, output, cancel_cb)
    if mixed.timeline != expected.timeline:
        raise ExportError("mixed audio changed the source timeline")
    return mixed


def _run_ogg_command(command: list[str], output: Path | Iterable[Path], *, timeout: int = 1800,
                     cancel_cb: CancelCallback | None = None) -> None:
    """Run a cancelable Ogg encode, with a built-in Vorbis fallback."""
    outputs = (
        (Path(output),)
        if isinstance(output, (str, os.PathLike))
        else tuple(Path(path) for path in output)
    )
    attempts = [command]
    if "libvorbis" in command:
        fallback: list[str] = []
        for token in command:
            if token == "-c:a":
                fallback.extend(["-strict", "experimental"])
            fallback.append("vorbis" if token == "libvorbis" else token)
        attempts.append(fallback)

    last_returncode = None
    last_stderr = b""
    for cmd in attempts:
        last_returncode, last_stderr = _audio_process(cmd, cancel_cb, timeout)

        if last_returncode == 0 and all(
                path.is_file() and path.stat().st_size >= 100 for path in outputs):
            return
        for path in outputs:
            try:
                path.unlink()
            except OSError:
                pass
        # Only an unavailable encoder is helped by the lower-quality fallback.
        if b"Unknown encoder 'libvorbis'" not in last_stderr:
            break
    detail = _ffmpeg_detail(last_stderr, *outputs)
    raise ExportError(f"ffmpeg could not render the MinusMix audio: {detail}")


def _render_mix(ffmpeg: str, full_mix: Path, output: Path,
                cancel_cb: CancelCallback | None = None, gain: float = 1.0) -> None:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(full_mix)]
    cmd.extend([
        "-af", f"asetpts=N/SR/TB,volume={gain:.16g}",
        "-vn", "-sn", "-dn", "-map_metadata", "-1",
        "-c:a", "libvorbis", "-q:a", "5", str(output),
    ])
    _run_ogg_command(cmd, output, cancel_cb=cancel_cb)


def _render_preview(ffmpeg: str, mix: Path, output: Path, duration_value,
                    cancel_cb: CancelCallback | None = None) -> bool:
    try:
        duration = max(0.0, float(duration_value or 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    clip = min(30.0, duration) if duration > 0 else 30.0
    if clip < 1.0:
        return False
    start = min(max(0.0, duration * 0.25), max(0.0, duration - clip)) if duration > 0 else 0.0
    fade = min(1.0, clip / 4.0)
    fade_out = max(0.0, clip - fade)
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-ss", f"{start:.3f}",
        "-i", str(mix), "-t", f"{clip:.3f}",
        "-af", (
            f"asetpts=N/SR/TB,afade=t=in:st=0:d={fade:.3f},"
            f"afade=t=out:st={fade_out:.3f}:d={fade:.3f}"
        ),
        "-vn", "-sn", "-dn", "-map_metadata", "-1",
        "-c:a", "libvorbis", "-q:a", "3", str(output),
    ]
    try:
        _run_ogg_command(cmd, output, timeout=300, cancel_cb=cancel_cb)
        return True
    except ExportError:
        try:
            output.unlink()
        except OSError:
            pass
        return False


def _safe_title_piece(value: str) -> str:
    value = " ".join(str(value).split()).strip(" .")
    return value[:80] or "Stem"


def stem_label(stem_id: str) -> str:
    return KNOWN_LABELS.get(stem_id, _safe_title_piece(stem_id.replace("_", " ")).title())


def _suffix(stem_ids: Iterable[str]) -> str:
    return "No " + " + ".join(stem_label(s) for s in stem_ids)


def _safe_filename_base(value: str) -> str:
    value = _INVALID_FILENAME.sub("_", value)
    value = " ".join(value.split()).strip(" .")
    # Leave room for " (No …) (999).feedpak" on filesystems with a 255-byte-ish limit.
    return value[:150] or "MinusMix"


def desired_output_path(output_dir: Path, source: Path,
                        excluded_stems: Iterable[str] = (), *, suffix: str | None = None) -> Path:
    """Return the deterministic first-choice output path without reserving it.

    Batch scans use this to skip completed work safely on a later run.  The
    exporter atomically publishes the first available numbered candidate and
    therefore never overwrites a pre-existing file.
    """
    if suffix is None:
        selected = [str(stem).strip().lower() for stem in excluded_stems if str(stem).strip()]
        suffix = _suffix(selected)
    base = _safe_filename_base(Path(source).stem)
    return Path(output_dir) / f"{base} ({_safe_filename_base(suffix)}).feedpak"


def _atomic_publish(output_tmp: Path, destination: Path) -> bool:
    """Publish a complete archive if and only if destination is still unused.

    A hard link provides an atomic no-replace operation on the normal local
    filesystems used by FeedBack.  Some removable/network filesystems do not
    support hard links, so the fallback first reserves the exact destination
    with O_EXCL before replacing that reservation with our completed temp file.
    """
    try:
        os.link(output_tmp, destination)
    except FileExistsError:
        return False
    except OSError:
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        os.close(fd)
        try:
            os.replace(output_tmp, destination)
        except BaseException:
            try:
                destination.unlink()
            except OSError:
                pass
            raise
        return True

    # destination is now a second name for the complete temp archive. Removing
    # the hidden temp name leaves the published file intact.
    try:
        output_tmp.unlink()
    except OSError:
        pass
    return True


def _preview_window(duration_value) -> tuple[float, float, float, float] | None:
    try:
        duration = max(0.0, float(duration_value or 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    clip = min(30.0, duration) if duration > 0 else 30.0
    if clip < 1.0:
        return None
    start = min(max(0.0, duration * 0.25), max(0.0, duration - clip)) if duration > 0 else 0.0
    fade = min(1.0, clip / 4.0)
    return start, clip, fade, max(0.0, clip - fade)


def _render_mix_and_preview(ffmpeg: str, full_mix: Path,
                            mix_output: Path, preview_output: Path,
                            duration_value,
                            cancel_cb: CancelCallback | None = None, gain: float = 1.0) -> bool:
    """Render the playable mix and optional preview from one decoded graph."""
    window = _preview_window(duration_value)
    if window is None:
        _render_mix(ffmpeg, full_mix, mix_output, cancel_cb=cancel_cb, gain=gain)
        return False

    start, clip, fade, fade_out = window
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(full_mix)]
    filters = [
        f"[0:a]asetpts=N/SR/TB,volume={gain:.16g}[mixed]",
        "[mixed]asplit=2[fullout][previewbase]",
        (
            f"[previewbase]atrim=start={start:.3f}:duration={clip:.3f},"
            f"asetpts=N/SR/TB,afade=t=in:st=0:d={fade:.3f},"
            f"afade=t=out:st={fade_out:.3f}:d={fade:.3f}[previewout]"
        ),
    ]
    cmd.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[fullout]", "-vn", "-sn", "-dn", "-map_metadata", "-1",
        "-c:a", "libvorbis", "-q:a", "5", str(mix_output),
        "-map", "[previewout]", "-vn", "-sn", "-dn", "-map_metadata", "-1",
        "-c:a", "libvorbis", "-q:a", "3", str(preview_output),
    ])
    try:
        _run_ogg_command(
            cmd, (mix_output, preview_output), cancel_cb=cancel_cb,
        )
        return True
    except ExportError:
        # A preview is optional. Fall back to the established independent path
        # so a preview-filter incompatibility can never block the main export.
        _render_mix(ffmpeg, full_mix, mix_output, cancel_cb=cancel_cb, gain=gain)
        return _render_preview(
            ffmpeg, mix_output, preview_output, duration_value,
            cancel_cb=cancel_cb,
        )


def _publish_unique_output(output_tmp: Path, output_dir: Path, source: Path,
                           suffix: str) -> Path:
    wanted = desired_output_path(output_dir, source, suffix=suffix)
    for number in range(1, 10_000):
        candidate = wanted if number == 1 else wanted.with_name(
            f"{wanted.stem} ({number}){wanted.suffix}"
        )
        if _atomic_publish(output_tmp, candidate):
            return candidate
    raise ExportError("could not find an unused output filename")


def _source_entries(source: Path):
    """Yield ``(name, ZipInfo-or-Path)`` while rejecting ambiguous archives."""
    if source.is_file():
        zf = zipfile.ZipFile(source, "r")
        seen: set[str] = set()
        try:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = _member_name(info.filename.rstrip("/")) if info.filename.rstrip("/") else ""
                if not name:
                    continue
                if name in seen:
                    raise ExportError(f"the source feedpak contains duplicate member '{name}'")
                seen.add(name)
                yield name, (zf, info)
        finally:
            zf.close()
        return

    root = source.resolve()
    for path in sorted(source.rglob("*")):
        if path.is_dir():
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ExportError("the source feedpak contains a file link that escapes the package") from exc
        name = _member_name(path.relative_to(source).as_posix())
        yield name, resolved


def _copy_payload(src_handle, dst_handle) -> None:
    shutil.copyfileobj(src_handle, dst_handle, length=1024 * 1024)


def _build_zip(source: Path, output_tmp: Path, manifest: dict,
               replacements: dict[str, Path], remove: set[str]) -> None:
    replacements = {_member_name(k): Path(v) for k, v in replacements.items()}
    remove = {_member_name(k) for k in remove if k}
    with zipfile.ZipFile(output_tmp, "w", allowZip64=True) as zout:
        for name, entry in _source_entries(source):
            if name in MANIFEST_NAMES or name in remove or name in replacements:
                continue
            if isinstance(entry, tuple):
                zin, info = entry
                # _source_entries keeps zin open for the generator's lifetime.
                cloned = zipfile.ZipInfo(name, date_time=info.date_time)
                cloned.compress_type = info.compress_type
                cloned.comment = info.comment
                cloned.extra = info.extra
                cloned.external_attr = info.external_attr
                with zin.open(info, "r") as src, zout.open(cloned, "w", force_zip64=True) as dst:
                    _copy_payload(src, dst)
            else:
                comp = zipfile.ZIP_STORED if Path(name).suffix.lower() in _ALREADY_COMPRESSED else zipfile.ZIP_DEFLATED
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = comp
                with Path(entry).open("rb") as src, zout.open(info, "w", force_zip64=True) as dst:
                    _copy_payload(src, dst)

        for name, local_path in replacements.items():
            comp = zipfile.ZIP_STORED if Path(name).suffix.lower() in _ALREADY_COMPRESSED else zipfile.ZIP_DEFLATED
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = comp
            with local_path.open("rb") as src, zout.open(info, "w", force_zip64=True) as dst:
                _copy_payload(src, dst)

        manifest_info = zipfile.ZipInfo("manifest.yaml", date_time=(1980, 1, 1, 0, 0, 0))
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        zout.writestr(manifest_info, yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))


TemporarySeparator = Callable[[Path, Path, tuple[str, ...], str | None], dict[str, Path]]
ProgressCallback = Callable[[str, float, str], None]
CancelCallback = Callable[[], None]


class StemProvider(Protocol):
    """Obtain requested stems inside a caller-owned temporary workspace."""

    def obtain(self, mix: Path, work: Path, stems: tuple[str, ...],
               full_digest: str | None) -> dict[str, Path]: ...


@dataclass(frozen=True)
class CallbackStemProvider:
    callback: TemporarySeparator

    def obtain(self, mix: Path, work: Path, stems: tuple[str, ...],
               full_digest: str | None) -> dict[str, Path]:
        return self.callback(mix, work, stems, full_digest)


@dataclass(frozen=True)
class ExtractedAudio:
    full_mix: Path
    saved_stems: dict[str, Path]
    full_digest: str | None


@dataclass(frozen=True)
class RenderedAudio:
    full_mix: Path
    preview: Path
    preview_created: bool
    gain: float = 1.0


@dataclass(frozen=True)
class PackagePlan:
    manifest: dict
    replacements: dict[str, Path]
    remove: set[str]
    suffix: str


def _checkpoint(cancel_cb: CancelCallback | None) -> None:
    if cancel_cb:
        cancel_cb()


def _report(progress_cb: ProgressCallback | None, stage: str,
            progress: float, detail: str) -> None:
    if progress_cb:
        progress_cb(stage, max(0.0, min(1.0, float(progress))), detail)


def _resolve_prepared_source(source: Path,
                             prepared_source: PreparedSource | None) -> PreparedSource:
    prepared = prepared_source
    if prepared is not None:
        current_stat = source.stat() if source.is_file() else None
        current_signature = (
            (current_stat.st_size, current_stat.st_mtime_ns) if current_stat else None
        )
        if prepared.source != source or prepared.signature != current_signature:
            prepared = None
    return prepared if prepared is not None else prepare_source(source)


def _selected_exclusions(values: Iterable[str]) -> tuple[str, ...]:
    selected: list[str] = []
    for raw in values:
        stem_id = str(raw or "").strip().lower()
        if stem_id and stem_id != "full" and stem_id not in KNOWN_LABELS:
            raise ExportError("unsupported instrument stem: " + stem_id)
        if stem_id and stem_id != "full" and stem_id not in selected:
            selected.append(stem_id)
    if not selected:
        raise ExportError("choose at least one instrument stem to exclude")
    if set(selected) == set(MIX_STEMS):
        raise ExportError("keep at least one instrument stem in the backing track")
    return tuple(selected)


def _extract_source_audio(prepared: PreparedSource, selected: tuple[str, ...],
                          missing: tuple[str, ...], work: Path) -> ExtractedAudio:
    full_suffix = Path(prepared.info.full_mix_file).suffix or ".audio"
    full_local = work / f"full{full_suffix}"
    saved: dict[str, Path] = {}
    with SourcePackage(prepared.source) as package:
        digest = None
        if prepared.info.full_mix_file:
            digest = package.copy_member(
                prepared.info.full_mix_file, full_local,
                calculate_digest=bool(missing),
            )
        # A partial set is replaced in the temporary workspace as a unit.
        for index, stem_id in enumerate(() if missing else selected, 1):
            stem = prepared.stem_map.get(stem_id)
            if stem is None:
                continue
            local = work / f"retained_{index}{Path(stem.file).suffix or '.audio'}"
            package.copy_member(stem.file, local)
            saved[stem_id] = local
    return ExtractedAudio(full_local, saved, digest)


def _obtain_missing_stems(provider: StemProvider | None, extracted: ExtractedAudio,
                          missing: tuple[str, ...], work: Path) -> dict[str, Path]:
    if not missing:
        return {}
    if provider is None:
        raise ExportError(
            "this source has no saved " + ", ".join(missing)
            + " stem; start Stem Splitter's managed local server and try again"
        )
    separation_dir = work / "temporary-separation"
    separation_dir.mkdir()
    try:
        raw_temporary = provider.obtain(
            extracted.full_mix, separation_dir, missing, extracted.full_digest,
        )
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"temporary stem separation failed: {exc}") from exc
    if not isinstance(raw_temporary, dict):
        raise ExportError("temporary stem separation returned an invalid result")

    separation_root = separation_dir.resolve()
    temporary: dict[str, Path] = {}
    for raw_id, raw_path in raw_temporary.items():
        stem_id = str(raw_id or "").strip().lower()
        path = Path(raw_path).resolve()
        try:
            path.relative_to(separation_root)
        except ValueError as exc:
            raise ExportError(
                "temporary stem separation returned a file outside its workspace"
            ) from exc
        if stem_id and path.is_file():
            temporary.setdefault(stem_id, path)
    still_missing = [stem_id for stem_id in missing if stem_id not in temporary]
    if still_missing:
        raise ExportError(
            "the separation engine did not produce: " + ", ".join(still_missing)
        )
    paths = [temporary[stem] for stem in missing]
    if len(paths) != len(set(paths)):
        raise ExportError("separation returned the same audio file for different instruments")
    return temporary


def _render_export_audio(ffmpeg: str, prepared: PreparedSource,
                         selected: tuple[str, ...], extracted: ExtractedAudio,
                         temporary: dict[str, Path], work: Path,
                         cancel_cb: CancelCallback | None) -> RenderedAudio:
    retained = [
        extracted.saved_stems.get(stem_id) or temporary[stem_id]
        for stem_id in selected
    ]
    full_output = work / "minus-mix-full.ogg"
    preview_output = work / "preview.ogg"
    summed = work / "retained-sum.wav"
    info = _sum_stems(ffmpeg, retained, summed, extracted.full_mix, cancel_cb)
    gain = min(1.0, 0.98 / info.peak) if info.peak else 1.0
    for _attempt in range(3):
        preview_created = _render_mix_and_preview(
            ffmpeg, summed, full_output, preview_output,
            info.frames / info.rate, cancel_cb=cancel_cb, gain=gain,
        )
        encoded = _analyze_audio(ffmpeg, full_output, cancel_cb)
        if encoded.timeline != info.timeline:
            raise ExportError("encoded backing track changed the source timeline")
        peak = encoded.peak
        if preview_created:
            peak = max(peak, _analyze_audio(ffmpeg, preview_output, cancel_cb).peak)
        if peak <= 0.99:
            return RenderedAudio(full_output, preview_output, preview_created, gain)
        # Re-encode the lossless sum, never the lossy output. One constant gain
        # preserves balance and dynamics; do not boost a quiet or silent intro.
        gain *= 0.95 / peak
    raise ExportError("could not render a backing track without clipping")


def _package_plan(prepared: PreparedSource, selected: tuple[str, ...],
                  rendered: RenderedAudio, source_digest: str) -> PackagePlan:
    suffix = _suffix(selected)
    manifest = dict(prepared.manifest)
    source_title = str(prepared.manifest.get("title") or prepared.source.stem)
    manifest["title"] = f"{source_title} ({suffix})"
    manifest["stems"] = [{
        "id": "full", "file": FULL_MIX_REL, "codec": "vorbis", "default": True,
    }]
    manifest["minus_mix"] = {
        "excluded_stems": list(selected),
        "source_title": source_title,
        "generator": "minus_mix",
        "render_method": RENDER_METHOD,
        "render_version": RENDER_VERSION,
        "included_stems": [stem for stem in MIX_STEMS if stem not in selected],
        "source_sha256": source_digest,
        "output_gain": rendered.gain,
    }
    manifest.pop("original_audio", None)

    old_preview = manifest.get("preview")
    if rendered.preview_created:
        manifest["preview"] = PREVIEW_REL
    else:
        manifest.pop("preview", None)
    remove = {stem.file for stem in prepared.stem_map.values()}
    if prepared.info.full_mix_file:
        remove.add(prepared.info.full_mix_file)
    if isinstance(old_preview, str) and old_preview.strip():
        remove.add(_member_name(old_preview.strip()))
    replacements = {FULL_MIX_REL: rendered.full_mix}
    if rendered.preview_created:
        replacements[PREVIEW_REL] = rendered.preview
    return PackagePlan(manifest, replacements, remove, suffix)


def _publish_package(prepared: PreparedSource, output_dir: Path,
                     plan: PackagePlan) -> Path:
    with _EXPORT_LOCK:
        wanted = desired_output_path(output_dir, prepared.source, suffix=plan.suffix)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{wanted.stem}-", suffix=".tmp", dir=output_dir,
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            _build_zip(
                prepared.source, tmp_path, plan.manifest,
                plan.replacements, plan.remove,
            )
            return _publish_unique_output(
                tmp_path, output_dir, prepared.source, plan.suffix,
            )
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise


def export_minus_mix(source: Path, output_dir: Path, excluded_stems: Iterable[str], *,
                     prepared_source: PreparedSource | None = None,
                     stem_provider: StemProvider | None = None,
                     separate_missing: TemporarySeparator | None = None,
                     ffmpeg_resolver: Callable[[], str | None] | None = None,
                     progress_cb: ProgressCallback | None = None,
                     cancel_cb: CancelCallback | None = None,
                     log=None) -> ExportResult:
    _checkpoint(cancel_cb)
    _report(progress_cb, "validating", 0.01, "Checking source feedpak")
    source = Path(source).resolve()
    output_dir = Path(output_dir).resolve()
    if not output_dir.is_absolute() or not output_dir.is_dir():
        raise ExportError("choose an existing output folder")
    if source.is_dir():
        try:
            output_dir.relative_to(source)
        except ValueError:
            pass
        else:
            raise ExportError("the output folder cannot be inside a directory-form source feedpak")
    output_dir = validate_output_directory(output_dir)

    ffmpeg = ffmpeg_resolver() if ffmpeg_resolver is not None else _ffmpeg_cmd()
    if not ffmpeg:
        raise ExportError(
            "ffmpeg is unavailable; install or update Stem Splitter's managed server, "
            "or repair the desktop app"
        )

    prepared = _resolve_prepared_source(source, prepared_source)
    selected = _selected_exclusions(excluded_stems)
    stem_plan = plan_stems(prepared.info, selected)
    missing = stem_plan.requested
    provider = stem_provider
    if provider is None and separate_missing is not None:
        provider = CallbackStemProvider(separate_missing)
    if missing and provider is None:
        raise ExportError(
            "this source has no saved " + ", ".join(missing)
            + " stem; start Stem Splitter's managed local server and try again"
        )

    with tempfile.TemporaryDirectory(prefix="feedback_minus_mix_") as td:
        work = Path(td)
        source_digest = source_fingerprint(source, cancel_cb)
        _checkpoint(cancel_cb)
        _report(progress_cb, "extracting", 0.04, "Reading the full mix")
        extracted = _extract_source_audio(prepared, stem_plan.included, missing, work)
        if missing:
            _checkpoint(cancel_cb)
            _report(progress_cb, "separating", 0.08, "Obtaining a complete instrument stem set")
        temporary = _obtain_missing_stems(provider, extracted, missing, work)

        _checkpoint(cancel_cb)
        _report(progress_cb, "rendering", 0.78, "Rendering the MinusMix backing track")
        rendered = _render_export_audio(
            ffmpeg, prepared, stem_plan.included, extracted, temporary, work, cancel_cb,
        )
        _checkpoint(cancel_cb)
        _report(progress_cb, "preview", 0.88, "Finalizing the preview")
        if not rendered.preview_created and log:
            log.warning("minus_mix: preview render failed; exporting without a preview")

        plan = _package_plan(prepared, selected, rendered, source_digest)
        _checkpoint(cancel_cb)
        if source_fingerprint(source, cancel_cb) != source_digest:
            raise ExportError("source package changed during export; please retry")
        _report(progress_cb, "packaging", 0.94, "Packaging the new feedpak")
        final_path = _publish_package(prepared, output_dir, plan)

    return ExportResult(
        output_path=final_path,
        output_filename=final_path.name,
        title=plan.manifest["title"],
        excluded_stems=selected,
        preview_created=rendered.preview_created,
        temporary_separation_used=bool(missing),
    )
