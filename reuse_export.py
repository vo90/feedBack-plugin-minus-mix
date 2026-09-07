"""Stream finished donor audio into a fresh package without decoding it."""
from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path

import yaml


def publish_no_replace(temp, target):
    """Atomic no-replace publication; never reserve then replace a public path."""
    try:
        os.link(temp, target)
    except FileExistsError:
        return False
    except OSError:
        if os.name != "nt":
            raise OSError("Atomic no-replace publication is unavailable on this filesystem.") from None
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move.restype = ctypes.c_int
        if not move(str(temp), str(target), 0):
            error = ctypes.get_last_error()
            if error in (80, 183):
                return False
            raise ctypes.WinError(error) from None
        return True
    Path(temp).unlink()
    return True


def _copy(source, destination, match, cancel):
    digest = hashlib.sha256()
    while data := source.read(match.CHUNK):
        match.checkpoint(cancel)
        destination.write(data)
        digest.update(data)
    return digest.hexdigest()


def _clone(info, name=None):
    result = zipfile.ZipInfo(name or info.filename, date_time=info.date_time)
    result.compress_type = info.compress_type
    result.comment, result.extra = info.comment, info.extra
    result.external_attr = info.external_attr
    return result


def recheck(info, match, cancel=None):
    path = Path(info["path"])
    if match.signature(path) != info["signature"]:
        raise match.ReuseError("An input changed after preview; scan again.")
    if match.digest_file(path, cancel) != info["sha256"]:
        raise match.ReuseError("Input content changed after preview; scan again.")


def plan_key(fresh, donor, match):
    data = [match.POLICY, fresh["sha256"], donor["sha256"], match.audio_identity(donor)]
    return hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest()


def _manifest(fresh_manifest, fresh, donor, match):
    manifest = copy.deepcopy(fresh_manifest)
    old_audio = {match.member_name(stem["file"]) for stem in manifest.get("stems", [])}
    for key in ("preview", "original_audio"):
        if manifest.get(key):
            old_audio.add(match.member_name(manifest[key]))
    preserved_refs = [item["file"] for item in manifest.get("arrangements", [])]
    preserved_refs.extend(manifest[key] for key in ("cover", "lyrics", "rigs") if manifest.get(key))
    preserved_refs.extend(item["file"] for item in manifest.get("lyric_tracks", [])
                          if isinstance(item, dict) and item.get("file"))
    if old_audio.intersection(match.member_name(value) for value in preserved_refs):
        raise match.ReuseError("Fresh audio declarations overlap retained chart or asset references.")
    audio = donor["audio"]
    extension = Path(audio["full"]["member"]).suffix
    full_name = "stems/full" + extension
    replacement = {full_name: audio["full"]}
    manifest["title"] = fresh["title"] + " (No Guitar)"
    manifest["stems"] = [{"id": "full", "file": full_name,
                           "codec": audio["codec"], "default": True}]
    manifest.pop("original_audio", None)
    manifest.pop("stem_separation", None)
    manifest.pop("preview", None)
    if "preview" in audio:
        preview = "preview" + Path(audio["preview"]["member"]).suffix
        manifest["preview"] = preview
        replacement[preview] = audio["preview"]
    manifest["minus_mix"] = {
        "excluded_stems": ["guitar"], "source_title": fresh["title"],
        "generator": "minus_mix", "audio_reuse": {
            "policy": match.POLICY, "plan_key": plan_key(fresh, donor, match),
            "fresh_package_sha256": fresh["sha256"],
            "donor_package_sha256": donor["sha256"],
            "full_sha256": audio["full"]["sha256"],
            "preview_sha256": audio.get("preview", {}).get("sha256"),
        },
    }
    return manifest, old_audio, replacement


def _build(path, fresh, donor, match, cancel):
    copied = {}
    with zipfile.ZipFile(fresh["path"]) as source, zipfile.ZipFile(donor["path"]) as audio:
        source_entries, audio_entries = match.inventory(source), match.inventory(audio)
        manifest_name = next(name for name in ("manifest.yaml", "manifest.yml") if name in source_entries)
        if hashlib.sha256(source.read(manifest_name)).hexdigest() != fresh["manifest_sha256"]:
            raise match.ReuseError("Fresh manifest changed after verification.")
        manifest, remove, replace = _manifest(
            match.manifest_from(source, source_entries), fresh, donor, match,
        )
        if (set(replace) & set(source_entries)) - remove:
            raise match.ReuseError("Donor audio destination conflicts with a fresh non-audio member.")
        retained = set(source_entries) - {"manifest.yaml", "manifest.yml"} - remove - set(replace)
        if fresh.get("payload_hashes") is not None and retained != set(fresh["payload_hashes"]):
            raise match.ReuseError("Fresh retained-member inventory changed after verification.")
        with zipfile.ZipFile(path, "w", allowZip64=True) as output:
            for name, info in source_entries.items():
                if name in {"manifest.yaml", "manifest.yml"} or name in remove or name in replace:
                    continue
                with source.open(info) as src, output.open(_clone(info), "w", force_zip64=True) as dst:
                    copied[name] = _copy(src, dst, match, cancel)
                if fresh.get("payload_hashes") is not None and copied[name] != fresh["payload_hashes"].get(name):
                    raise match.ReuseError("Fresh retained member changed after verification.")
            for name, item in replace.items():
                info = audio_entries[item["member"]]
                clone = _clone(info, name)
                # Encoded audio needs no second compression pass.
                clone.compress_type = zipfile.ZIP_STORED
                with audio.open(info) as src, output.open(clone, "w", force_zip64=True) as dst:
                    copied[name] = _copy(src, dst, match, cancel)
                if copied[name] != item["sha256"]:
                    raise match.ReuseError("Donor audio changed after preview.")
            output.writestr("manifest.yaml", yaml.safe_dump(manifest, sort_keys=False,
                                                            allow_unicode=True))
    return manifest, copied


def _verify(path, manifest, copied, match, cancel):
    with zipfile.ZipFile(path) as archive:
        entries = match.inventory(archive)
        if match.manifest_from(archive, entries) != manifest:
            raise match.ReuseError("Output manifest verification failed.")
        for name, digest in copied.items():
            with archive.open(entries[name]) as stream:
                if match.digest_stream(stream, cancel) != digest:
                    raise match.ReuseError("Output member verification failed.")
        for arrangement in manifest.get("arrangements", []):
            if match.member_name(arrangement["file"]) not in entries:
                raise match.ReuseError("Output arrangement reference is missing.")
        references = [manifest[key] for key in ("cover", "lyrics", "rigs", "preview")
                      if manifest.get(key)]
        references.extend(track["file"] for track in manifest.get("lyric_tracks", [])
                          if isinstance(track, dict) and track.get("file"))
        for name in references:
            if match.member_name(name) not in entries:
                raise match.ReuseError("Output asset reference is missing.")


def export_reuse(fresh, donor, output_path, *, match, exporter, cancel=None, guard=None,
                 snapshot_verified=False):
    """Create exactly the reviewed path; an existing path is never replaced."""
    output_path = Path(output_path)
    match.checkpoint(cancel)
    if snapshot_verified and fresh.get("payload_hashes") is None:
        raise match.ReuseError("Verified snapshot is missing retained-member hashes.")
    for info in (fresh, donor):
        if snapshot_verified:
            if match.signature(info["path"]) != info["signature"]:
                raise match.ReuseError("Input changed after verification.")
        else:
            recheck(info, match, cancel)
    if not match.compatible(fresh, donor):
        raise match.ReuseError("Reviewed packages are not compatible.")
    if output_path.exists():
        raise match.ReuseError("Output already exists; it was not overwritten.")
    if guard:
        guard()
    fd, name = tempfile.mkstemp(prefix=".minusmix-reuse-", suffix=".tmp", dir=output_path.parent)
    os.close(fd)
    temp = Path(name)
    try:
        manifest, copied = _build(temp, fresh, donor, match, cancel)
        _verify(temp, manifest, copied, match, cancel)
        for info in (fresh, donor):
            if snapshot_verified:
                if match.signature(info["path"]) != info["signature"]:
                    raise match.ReuseError("Input changed during the copy.")
            else:
                recheck(info, match, cancel)
        match.checkpoint(cancel)
        output_hash = match.digest_file(temp, cancel)
        if guard:
            guard()
        if not publish_no_replace(temp, output_path):
            raise match.ReuseError("Output appeared during creation; it was not overwritten.")
        return {"output": str(output_path), "output_sha256": output_hash,
                "plan_key": plan_key(fresh, donor, match), "members_verified": len(copied),
                "audio_sha256": donor["audio"]["full"]["sha256"],
                "preview_sha256": donor["audio"].get("preview", {}).get("sha256")}
    finally:
        # Only our private unpublished temporary file is owned by this operation.
        if temp.exists():
            temp.unlink()


def completed_output(path, fresh, donor, *, match, cancel=None):
    """Recognize a crash-published output through provenance AND current bytes."""
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        entries = match.inventory(archive)
        manifest = match.manifest_from(archive, entries)
        receipt = manifest.get("minus_mix", {}).get("audio_reuse", {})
        if receipt.get("plan_key") != plan_key(fresh, donor, match):
            return None
    actual = match.inspect_package(path, donor=True, cancel=cancel)
    if not match.compatible(fresh, actual) or match.audio_identity(actual) != match.audio_identity(donor):
        return None
    # Preserve all fresh non-audio members, not just compatibility evidence.
    with zipfile.ZipFile(fresh["path"]) as source, zipfile.ZipFile(path) as output:
        src_entries = match.inventory(source)
        expected_manifest, removed, replaced = _manifest(match.manifest_from(source, src_entries), fresh, donor, match)
        if manifest != expected_manifest:
            return None
        expected = set(src_entries) - {"manifest.yaml", "manifest.yml"} - removed - set(replaced)
        if set(match.inventory(output)) != expected | set(replaced) | {"manifest.yaml"}:
            return None
        for name in expected:
            with source.open(name) as left, output.open(name) as right:
                if match.digest_stream(left, cancel) != match.digest_stream(right, cancel):
                    return None
    return {"output": str(path), "output_sha256": actual["sha256"],
            "plan_key": plan_key(fresh, donor, match), "recovered": True}
