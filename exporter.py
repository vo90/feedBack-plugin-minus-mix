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
_ARCHIVE_EXTS = {".feedpak", ".sloppak"}
_ALREADY_COMPRESSED = {".ogg", ".mp3", ".flac", ".png", ".jpg", ".jpeg", ".webp", ".zip"}
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_EXPORT_LOCK = threading.Lock()


class ExportError(RuntimeError):
    """An expected, user-actionable export refusal."""


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
    if full is None:
        raise ExportError("the source feedpak has no full mix to subtract from")
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
        stems=(full,) + instruments,
        arrangements=arrangements,
        full_mix_file=full.file,
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
        started = time.monotonic()
        with tempfile.TemporaryFile() as error_log:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=error_log,
            )
            try:
                while proc.poll() is None:
                    if cancel_cb:
                        try:
                            cancel_cb()
                        except BaseException:
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except Exception:
                                proc.kill()
                                proc.wait()
                            raise
                    if time.monotonic() - started > timeout:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            proc.kill()
                            proc.wait()
                        raise ExportError("audio rendering timed out")
                    time.sleep(0.2)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            last_returncode = proc.returncode
            error_log.seek(0, os.SEEK_END)
            size = error_log.tell()
            error_log.seek(max(0, size - 64 * 1024))
            last_stderr = error_log.read()

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


def _render_mix(ffmpeg: str, full_mix: Path, excluded: list[Path], output: Path,
                cancel_cb: CancelCallback | None = None) -> None:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(full_mix)]
    for stem in excluded:
        cmd.extend(["-i", str(stem)])

    filters: list[str] = []
    negative_labels: list[str] = []
    for index in range(1, len(excluded) + 1):
        label = f"neg{index}"
        filters.append(f"[{index}:a]volume=-1:precision=double[{label}]")
        negative_labels.append(f"[{label}]")
    inputs = "[0:a]" + "".join(negative_labels)
    filters.append(
        f"{inputs}amix=inputs={1 + len(excluded)}:duration=first:"
        "dropout_transition=0:normalize=0[out]"
    )
    cmd.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[out]", "-vn", "-sn", "-dn", "-map_metadata", "-1",
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
        "-af", f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={fade_out:.3f}:d={fade:.3f}",
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


def _render_mix_and_preview(ffmpeg: str, full_mix: Path, excluded: list[Path],
                            mix_output: Path, preview_output: Path,
                            duration_value,
                            cancel_cb: CancelCallback | None = None) -> bool:
    """Render the playable mix and optional preview from one decoded graph."""
    window = _preview_window(duration_value)
    if window is None:
        _render_mix(ffmpeg, full_mix, excluded, mix_output, cancel_cb=cancel_cb)
        return False

    start, clip, fade, fade_out = window
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(full_mix)]
    for stem in excluded:
        cmd.extend(["-i", str(stem)])

    filters: list[str] = []
    negative_labels: list[str] = []
    for index in range(1, len(excluded) + 1):
        label = f"neg{index}"
        filters.append(f"[{index}:a]volume=-1:precision=double[{label}]")
        negative_labels.append(f"[{label}]")
    inputs = "[0:a]" + "".join(negative_labels)
    filters.extend([
        (
            f"{inputs}amix=inputs={1 + len(excluded)}:duration=first:"
            "dropout_transition=0:normalize=0[mixed]"
        ),
        "[mixed]asplit=2[fullout][previewbase]",
        (
            f"[previewbase]atrim=start={start:.3f}:duration={clip:.3f},"
            f"asetpts=PTS-STARTPTS,afade=t=in:st=0:d={fade:.3f},"
            f"afade=t=out:st={fade_out:.3f}:d={fade:.3f}[previewout]"
        ),
    ])
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
        _render_mix(ffmpeg, full_mix, excluded, mix_output, cancel_cb=cancel_cb)
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
        if stem_id and stem_id != "full" and stem_id not in selected:
            selected.append(stem_id)
    if not selected:
        raise ExportError("choose at least one instrument stem to exclude")
    return tuple(selected)


def _extract_source_audio(prepared: PreparedSource, selected: tuple[str, ...],
                          missing: tuple[str, ...], work: Path) -> ExtractedAudio:
    full_suffix = Path(prepared.info.full_mix_file).suffix or ".audio"
    full_local = work / f"full{full_suffix}"
    saved: dict[str, Path] = {}
    with SourcePackage(prepared.source) as package:
        digest = package.copy_member(
            prepared.info.full_mix_file, full_local,
            calculate_digest=bool(missing),
        )
        for index, stem_id in enumerate(selected, 1):
            stem = prepared.stem_map.get(stem_id)
            if stem is None:
                continue
            local = work / f"excluded_{index}{Path(stem.file).suffix or '.audio'}"
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
            + " stem; start Stem Splitter's server and try again"
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
    return temporary


def _render_export_audio(ffmpeg: str, prepared: PreparedSource,
                         selected: tuple[str, ...], extracted: ExtractedAudio,
                         temporary: dict[str, Path], work: Path,
                         cancel_cb: CancelCallback | None) -> RenderedAudio:
    excluded = [
        extracted.saved_stems.get(stem_id) or temporary[stem_id]
        for stem_id in selected
    ]
    full_output = work / "minus-mix-full.ogg"
    preview_output = work / "preview.ogg"
    preview_created = _render_mix_and_preview(
        ffmpeg, extracted.full_mix, excluded, full_output, preview_output,
        prepared.manifest.get("duration"), cancel_cb=cancel_cb,
    )
    return RenderedAudio(full_output, preview_output, preview_created)


def _package_plan(prepared: PreparedSource, selected: tuple[str, ...],
                  rendered: RenderedAudio) -> PackagePlan:
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
    }
    manifest.pop("original_audio", None)

    old_preview = manifest.get("preview")
    if rendered.preview_created:
        manifest["preview"] = PREVIEW_REL
    else:
        manifest.pop("preview", None)
    remove = {stem.file for stem in prepared.stem_map.values()}
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

    ffmpeg = _ffmpeg_cmd()
    if not ffmpeg:
        raise ExportError("ffmpeg is not available; repair or reinstall the desktop app")

    prepared = _resolve_prepared_source(source, prepared_source)
    selected = _selected_exclusions(excluded_stems)
    missing = tuple(stem_id for stem_id in selected if stem_id not in prepared.stem_map)
    provider = stem_provider
    if provider is None and separate_missing is not None:
        provider = CallbackStemProvider(separate_missing)
    if missing and provider is None:
        raise ExportError(
            "this source has no saved " + ", ".join(missing)
            + " stem; start Stem Splitter's server and try again"
        )

    with tempfile.TemporaryDirectory(prefix="feedback_minus_mix_") as td:
        work = Path(td)
        _checkpoint(cancel_cb)
        _report(progress_cb, "extracting", 0.04, "Reading the full mix")
        extracted = _extract_source_audio(prepared, selected, missing, work)
        if missing:
            _checkpoint(cancel_cb)
            _report(progress_cb, "separating", 0.08, "Separating selected audio temporarily")
        temporary = _obtain_missing_stems(provider, extracted, missing, work)

        _checkpoint(cancel_cb)
        _report(progress_cb, "rendering", 0.78, "Rendering the MinusMix backing track")
        rendered = _render_export_audio(
            ffmpeg, prepared, selected, extracted, temporary, work, cancel_cb,
        )
        _checkpoint(cancel_cb)
        _report(progress_cb, "preview", 0.88, "Finalizing the preview")
        if not rendered.preview_created and log:
            log.warning("minus_mix: preview render failed; exporting without a preview")

        plan = _package_plan(prepared, selected, rendered)
        _checkpoint(cancel_cb)
        _report(progress_cb, "packaging", 0.94, "Packaging the new feedpak")
        final_path = _publish_package(prepared, output_dir, plan)

    _report(progress_cb, "done", 1.0, "Practice feedpak created")
    return ExportResult(
        output_path=final_path,
        output_filename=final_path.name,
        title=plan.manifest["title"],
        excluded_stems=selected,
        preview_created=rendered.preview_created,
        temporary_separation_used=bool(missing),
    )
