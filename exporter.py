"""Safe, non-destructive single-stem practice-feedpak rendering.

The Stem Splitter produces standard feedpak stem entries.  This module consumes
that public format instead of importing Stem Splitter internals, so an already
split pack remains exportable even when the splitter is disabled or replaced.

Every export is a NEW zip-form ``.feedpak``.  The source is opened read-only,
the output is built beside its final destination, and the final name is made
unique rather than overwriting anything.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

import yaml

import sloppak
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


def inspect_source(source: Path) -> SourceInfo:
    source = Path(source)
    if not source.exists() or (source.is_file() and source.suffix.lower() not in _ARCHIVE_EXTS):
        raise ExportError("the selected source is not a feedpak")
    manifest = _manifest(source)
    stems = _stem_map(manifest)
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
    derived = manifest.get("practice_mix_export")
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


def _copy_member_to(source: Path, rel: str, destination: Path) -> None:
    rel = _member_name(rel)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        try:
            with zipfile.ZipFile(source, "r") as zf:
                infos = [i for i in zf.infolist() if i.filename == rel]
                if len(infos) != 1 or infos[0].is_dir():
                    raise ExportError(f"audio file '{rel}' is missing or duplicated in the source feedpak")
                with zf.open(infos[0], "r") as src, destination.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
        except zipfile.BadZipFile as exc:
            raise ExportError("the source feedpak is not a valid zip archive") from exc
        return

    root = source.resolve()
    target = (source / Path(*PurePosixPath(rel).parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ExportError(f"audio file '{rel}' escapes the source feedpak") from exc
    if not target.is_file():
        raise ExportError(f"audio file '{rel}' is missing from the source feedpak")
    shutil.copyfile(target, destination)


def _ffmpeg_detail(stderr: bytes, *paths: Path) -> str:
    text = (stderr or b"").decode("utf-8", "replace")
    text = _scrub_paths(text, *(str(p) for p in paths))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[-1] if lines else "unknown ffmpeg error")[:500]


def _run_ogg_command(command: list[str], output: Path, *, timeout: int = 1800,
                     cancel_cb: CancelCallback | None = None) -> None:
    """Run a cancelable Ogg encode, with a built-in Vorbis fallback."""
    attempts = [command]
    fallback = list(command)
    try:
        codec_at = fallback.index("libvorbis")
    except ValueError:
        codec_at = -1
    if codec_at >= 0:
        fallback[codec_at] = "vorbis"
        codec_flag_at = fallback.index("-c:a")
        fallback[codec_flag_at:codec_flag_at] = ["-strict", "experimental"]
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

        if last_returncode == 0 and output.is_file() and output.stat().st_size >= 100:
            return
        try:
            output.unlink()
        except OSError:
            pass
        # Only an unavailable encoder is helped by the lower-quality fallback.
        if b"Unknown encoder 'libvorbis'" not in last_stderr:
            break
    detail = _ffmpeg_detail(last_stderr, output)
    raise ExportError(f"ffmpeg could not render the practice mix: {detail}")


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
    return value[:150] or "Practice Mix"


def _unique_output(output_dir: Path, source: Path, suffix: str) -> Path:
    wanted = desired_output_path(output_dir, source, suffix=suffix)
    if not wanted.exists():
        return wanted
    for number in range(2, 10000):
        candidate = wanted.with_name(f"{wanted.stem} ({number}){wanted.suffix}")
        if not candidate.exists():
            return candidate
    raise ExportError("could not find an unused output filename")


def desired_output_path(output_dir: Path, source: Path,
                        excluded_stems: Iterable[str] = (), *, suffix: str | None = None) -> Path:
    """Return the deterministic first-choice output path without reserving it.

    Batch scans use this to skip completed work safely on a later run.  The
    single-song exporter still calls ``_unique_output`` and therefore never
    overwrites a pre-existing file.
    """
    if suffix is None:
        selected = [str(stem).strip().lower() for stem in excluded_stems if str(stem).strip()]
        suffix = _suffix(selected)
    base = _safe_filename_base(Path(source).stem)
    return Path(output_dir) / f"{base} ({_safe_filename_base(suffix)}).feedpak"


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


TemporarySeparator = Callable[[Path, Path, tuple[str, ...]], dict[str, Path]]
ProgressCallback = Callable[[str, float, str], None]
CancelCallback = Callable[[], None]


def _checkpoint(cancel_cb: CancelCallback | None) -> None:
    if cancel_cb:
        cancel_cb()


def _report(progress_cb: ProgressCallback | None, stage: str,
            progress: float, detail: str) -> None:
    if progress_cb:
        progress_cb(stage, max(0.0, min(1.0, float(progress))), detail)


def export_practice_mix(source: Path, output_dir: Path, excluded_stems: Iterable[str], *,
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

    manifest = _manifest(source)
    info = inspect_source(source)
    stem_map = _stem_map(manifest)
    selected: list[str] = []
    for raw in excluded_stems:
        stem_id = str(raw or "").strip().lower()
        if not stem_id or stem_id == "full" or stem_id in selected:
            continue
        selected.append(stem_id)
    if not selected:
        raise ExportError("choose at least one instrument stem to exclude")
    missing = [stem_id for stem_id in selected if stem_id not in stem_map]
    if missing and separate_missing is None:
        raise ExportError(
            "this source has no saved " + ", ".join(missing)
            + " stem; start Stem Splitter's server and try again"
        )

    suffix = _suffix(selected)
    with tempfile.TemporaryDirectory(prefix="feedback_practice_mix_") as td:
        work = Path(td)
        _checkpoint(cancel_cb)
        _report(progress_cb, "extracting", 0.04, "Reading the full mix")
        full_suffix = Path(info.full_mix_file).suffix or ".audio"
        full_local = work / f"full{full_suffix}"
        _copy_member_to(source, info.full_mix_file, full_local)

        temporary: dict[str, Path] = {}
        if missing:
            _checkpoint(cancel_cb)
            _report(progress_cb, "separating", 0.08, "Separating selected audio temporarily")
            separation_dir = work / "temporary-separation"
            separation_dir.mkdir()
            try:
                raw_temporary = separate_missing(full_local, separation_dir, tuple(missing))
            except ExportError:
                raise
            except Exception as exc:
                raise ExportError(f"temporary stem separation failed: {exc}") from exc
            if not isinstance(raw_temporary, dict):
                raise ExportError("temporary stem separation returned an invalid result")
            separation_root = separation_dir.resolve()
            for raw_id, raw_path in raw_temporary.items():
                stem_id = str(raw_id or "").strip().lower()
                path = Path(raw_path).resolve()
                try:
                    path.relative_to(separation_root)
                except ValueError as exc:
                    raise ExportError("temporary stem separation returned a file outside its workspace") from exc
                if stem_id and path.is_file():
                    temporary.setdefault(stem_id, path)
            still_missing = [stem_id for stem_id in missing if stem_id not in temporary]
            if still_missing:
                raise ExportError(
                    "the separation engine did not produce: " + ", ".join(still_missing)
                )

        _checkpoint(cancel_cb)
        _report(progress_cb, "rendering", 0.78, "Rendering the practice mix")

        excluded_local: list[Path] = []
        for index, stem_id in enumerate(selected, 1):
            if stem_id in stem_map:
                rel = stem_map[stem_id].file
                local = work / f"excluded_{index}{Path(rel).suffix or '.audio'}"
                _copy_member_to(source, rel, local)
                excluded_local.append(local)
            else:
                excluded_local.append(temporary[stem_id])

        practice_ogg = work / "practice-full.ogg"
        _render_mix(ffmpeg, full_local, excluded_local, practice_ogg, cancel_cb=cancel_cb)

        _checkpoint(cancel_cb)
        _report(progress_cb, "preview", 0.88, "Creating the preview")
        preview_ogg = work / "preview.ogg"
        preview_created = _render_preview(
            ffmpeg, practice_ogg, preview_ogg, manifest.get("duration"),
            cancel_cb=cancel_cb,
        )
        if not preview_created and log:
            log.warning("practice_mix_exporter: preview render failed; exporting without a preview")

        new_manifest = dict(manifest)
        source_title = str(manifest.get("title") or source.stem)
        new_manifest["title"] = f"{source_title} ({suffix})"
        new_manifest["stems"] = [{
            "id": "full", "file": FULL_MIX_REL, "codec": "vorbis", "default": True,
        }]
        new_manifest["practice_mix_export"] = {
            "excluded_stems": list(selected),
            "source_title": source_title,
            "generator": "practice_mix_exporter",
        }
        # Deprecated and now wrong: the pre-separation original is intentionally
        # absent from this derived, single-stem package.
        new_manifest.pop("original_audio", None)

        old_preview = new_manifest.get("preview")
        if preview_created:
            new_manifest["preview"] = PREVIEW_REL
        else:
            new_manifest.pop("preview", None)

        old_stem_files = {s.file for s in stem_map.values()}
        remove = set(old_stem_files)
        if isinstance(old_preview, str) and old_preview.strip():
            remove.add(_member_name(old_preview.strip()))
        replacements = {FULL_MIX_REL: practice_ogg}
        if preview_created:
            replacements[PREVIEW_REL] = preview_ogg

        # Name reservation and final replacement are serialised so two clicks
        # cannot race into the same unique name inside this app process.
        _checkpoint(cancel_cb)
        _report(progress_cb, "packaging", 0.94, "Packaging the new feedpak")
        with _EXPORT_LOCK:
            final_path = _unique_output(output_dir, source, suffix)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{final_path.stem}-", suffix=".tmp", dir=output_dir)
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                _build_zip(source, tmp_path, new_manifest, replacements, remove)
                if final_path.exists():
                    # Extremely defensive: another process may have created it
                    # while the archive was rendering. Never replace their file.
                    final_path = _unique_output(output_dir, source, suffix)
                os.replace(tmp_path, final_path)
            except Exception:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise

    _report(progress_cb, "done", 1.0, "Practice feedpak created")
    return ExportResult(
        output_path=final_path,
        output_filename=final_path.name,
        title=new_manifest["title"],
        excluded_stems=tuple(selected),
        preview_created=preview_created,
        temporary_separation_used=bool(missing),
    )
