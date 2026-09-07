"""Bounded package inspection and repair-tolerant audio compatibility evidence.

This proves compatibility of stored song timelines, not the recording master:
legacy MinusMix files do not retain an original recording hash. Names narrow the
search only; complete pitched event fingerprints establish candidate matches.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

import yaml

POLICY = "minusmix-audio-reuse-1"
MAX_FILES = 10_000
MAX_PACKAGE = 2 * 1024**3
MAX_CHART = 32 * 1024**2
MAX_CHARTS_BYTES = 128 * 1024**2
CHUNK = 1024 * 1024


class ReuseError(ValueError):
    """A readable explanation of a refused or stale reuse operation."""


def checkpoint(cancel=None):
    if cancel:
        cancel()


def digest_stream(stream, cancel=None):
    digest = hashlib.sha256()
    while data := stream.read(CHUNK):
        checkpoint(cancel)
        digest.update(data)
    return digest.hexdigest()


def digest_file(path, cancel=None):
    with Path(path).open("rb") as stream:
        return digest_stream(stream, cancel)


def signature(path):
    value = Path(path).stat()
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def member_name(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ReuseError("Invalid archive member path.")
    parts = value.split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ReuseError("Unsafe archive member path.")
    if PurePosixPath(value).is_absolute():
        raise ReuseError("Absolute archive member path.")
    return value


def inventory(archive):
    entries = {}
    folded = set()
    if len(archive.infolist()) > 4096:
        raise ReuseError("Package has too many members (limit 4096).")
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = member_name(info.filename)
        if name.casefold() in folded:
            raise ReuseError("Package contains duplicate or ambiguous member names.")
        if info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16):
            raise ReuseError("Encrypted or linked archive members are unsupported.")
        if info.file_size > MAX_PACKAGE:
            raise ReuseError("Archive member exceeds the 2 GiB expanded limit.")
        entries[name] = info
        folded.add(name.casefold())
    if sum(i.file_size for i in entries.values()) > MAX_PACKAGE:
        raise ReuseError("Package exceeds the 2 GiB expanded limit.")
    return entries


def _read(archive, entries, name, limit):
    name = member_name(name)
    if name not in entries or entries[name].file_size > limit:
        raise ReuseError(f"Missing or oversized package member: {name}")
    return archive.read(entries[name])


def manifest_from(archive, entries):
    names = [name for name in ("manifest.yaml", "manifest.yml") if name in entries]
    if len(names) != 1:
        raise ReuseError("Package must contain one unambiguous manifest.")
    manifest = yaml.safe_load(_read(archive, entries, names[0], CHUNK))
    if not isinstance(manifest, dict):
        raise ReuseError("Invalid package manifest.")
    return manifest


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def base_title(manifest):
    title = str(manifest.get("title") or "").strip()
    marker = manifest.get("minus_mix") or {}
    if isinstance(marker, dict) and marker.get("excluded_stems") == ["guitar"]:
        title = str(marker.get("source_title") or title)
    return re.sub(r"(?i)\s*\(no guitar\)\s*$", "", title).strip()


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ReuseError(f"Invalid {field} in chart/timeline.")
    return value


def _event(note, onset):
    string = _number(note.get("s"), "string")
    fret = _number(note.get("f"), "fret")
    if int(string) != string or not 0 <= string <= 11 or int(fret) != fret:
        raise ReuseError("Invalid note string or fret.")
    # Pitchless mute repairs can change sentinel/negative frets. Pitched frets
    # remain exact. Ignored editor notes are not sounding attacks.
    if note.get("ig") is True:
        return None
    fret = -1 if note.get("mt") is True else int(fret)
    if not -1 <= fret <= 48:
        raise ReuseError("Unsupported pitched fret in compatibility evidence.")
    time = _number(note.get("t", onset), "note onset")
    return (round(time * 1000), int(string), fret)


def event_fingerprint(document, context):
    """Flatten notes/chords without bend, sustain, measure or display metadata.

    Flat sounding attacks are invariant across the audited bend, per-string
    sustain, chord/arpeggio display and beat/anchor repairs. The difficulty
    ladder is deliberately not an identity requirement. No chart is rewritten.
    """
    if not isinstance(document, dict):
        raise ReuseError("Arrangement is not an object.")
    events = set()
    for note in document.get("notes", []):
        item = _event(note, note.get("t"))
        if item:
            events.add(item)
    templates = document.get("templates", [])
    for chord in document.get("chords", []):
        if chord.get("ig") is True:
            continue
        notes = chord.get("notes")
        if not notes:
            cid = chord.get("chordId", chord.get("cid", chord.get("id")))
            if not isinstance(cid, int) or not 0 <= cid < len(templates):
                raise ReuseError("Chord has neither playable notes nor a valid template.")
            frets = templates[cid].get("frets", [])
            notes = [{"s": s, "f": f, "mt": chord.get("mt", False)}
                     for s, f in enumerate(frets) if f >= 0]
        for note in notes:
            item = _event(note, chord.get("t"))
            if item:
                events.add(item)
    tuning = document.get("tuning", context.get("tuning", [0] * 6))
    if not isinstance(tuning, list) or not 4 <= len(tuning) <= 12:
        raise ReuseError("Unsupported arrangement tuning.")
    tuning = [_number(v, "tuning") for v in tuning]
    capo = _number(document.get("capo", context.get("capo", 0)), "capo")
    if len(events) < 8 or len({e[0] for e in events}) < 3:
        raise ReuseError("Too few sounding events to identify a recording timeline safely.")
    # Converter fidelity fixes also correct labels/bonus arrangement properties.
    # The complete MULTISET of musical charts identifies the backing timeline;
    # display-role metadata cannot be an invariant across those fixes.
    data = [tuning, capo, sorted(events)]
    return hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest()


def _audio(archive, entries, manifest, cancel):
    stems = manifest.get("stems") or []
    full = [stem for stem in stems if isinstance(stem, dict) and stem.get("id") == "full"]
    if len(full) != 1:
        raise ReuseError("Package must declare one full backing track.")
    selected = {"full": full[0].get("file")}
    if manifest.get("preview"):
        selected["preview"] = manifest["preview"]
    result = {}
    for kind, name in selected.items():
        name = member_name(name)
        if name not in entries or entries[name].file_size <= 0:
            raise ReuseError(f"Missing or empty {kind} audio.")
        with archive.open(entries[name]) as stream:
            digest = digest_stream(stream, cancel)
        result[kind] = {"member": name, "sha256": digest, "bytes": entries[name].file_size}
    result["codec"] = str(full[0].get("codec") or Path(selected["full"]).suffix.lstrip("."))
    return result


def inspect_package(path, *, donor=False, cancel=None, include_payload=False):
    path = Path(path)
    before = signature(path)
    if before[2] > MAX_PACKAGE:
        raise ReuseError("Package exceeds the 2 GiB file limit.")
    with zipfile.ZipFile(path) as archive:
        entries = inventory(archive)
        manifest = manifest_from(archive, entries)
        manifest_name = next(name for name in ("manifest.yaml", "manifest.yml") if name in entries)
        manifest_sha = hashlib.sha256(archive.read(manifest_name)).hexdigest()
        marker = manifest.get("minus_mix") or {}
        if donor and (not isinstance(marker, dict) or marker.get("excluded_stems") != ["guitar"]):
            raise ReuseError("Not a declared No Guitar MinusMix package.")
        duration = _number(manifest.get("duration"), "song duration")
        if duration <= 0:
            raise ReuseError("Song duration must be positive.")
        identity = [normalized(manifest.get("artist")), normalized(base_title(manifest))]
        if not all(identity):
            raise ReuseError("Artist and song title are required for matching.")
        arrangements, total = [], 0
        for context in manifest.get("arrangements") or []:
            checkpoint(cancel)
            if context.get("type") in ("vocals", "lyrics", "drums", "karaoke"):
                continue
            name = context.get("file")
            raw = _read(archive, entries, name, MAX_CHART)
            total += len(raw)
            if total > MAX_CHARTS_BYTES:
                raise ReuseError("Package chart data exceeds 128 MiB.")
            arrangements.append(event_fingerprint(json.loads(raw), context))
        if not arrangements:
            raise ReuseError("No pitched chart can establish audio compatibility.")
        audio = _audio(archive, entries, manifest, cancel) if donor else None
        payload = None
        if include_payload:
            removed = {member_name(stem["file"]) for stem in manifest.get("stems", [])}
            removed.update(member_name(manifest[key]) for key in ("preview", "original_audio")
                           if manifest.get(key))
            payload = {}
            for name, info in entries.items():
                if name in removed or name in ("manifest.yaml", "manifest.yml"):
                    continue
                with archive.open(info) as stream:
                    payload[name] = digest_stream(stream, cancel)
    digest = digest_file(path, cancel)
    if signature(path) != before:
        raise ReuseError("Package changed while being inspected.")
    return {"path": str(path), "sha256": digest, "signature": before,
            "identity": identity, "title": base_title(manifest),
            "artist": str(manifest.get("artist")), "duration": duration,
            "album": normalized(manifest.get("album")), "year": str(manifest.get("year") or ""),
            "offset": _number(0 if manifest.get("offset") is None else manifest["offset"], "offset"),
            "audio_offset": _number(0 if manifest.get("audio_offset") is None else manifest["audio_offset"], "audio offset"),
            "arrangements": sorted(arrangements), "audio": audio,
            "derived": bool(marker), "policy": POLICY,
            "manifest_sha256": manifest_sha, "payload_hashes": payload}


def compatible(fresh, donor):
    return (fresh["identity"] == donor["identity"]
            and not any(fresh.get(key) and donor.get(key) and fresh[key] != donor[key]
                        for key in ("album", "year"))
            and fresh["arrangements"] == donor["arrangements"]
            and abs(fresh["duration"] - donor["duration"]) <= 0.001
            and fresh["offset"] == donor["offset"]
            and fresh["audio_offset"] == donor["audio_offset"])


def audio_identity(donor):
    audio = donor["audio"]
    return (audio["full"]["sha256"], audio.get("preview", {}).get("sha256"), audio["codec"])
