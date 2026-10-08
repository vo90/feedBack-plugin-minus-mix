"""Trust, containment and cache contracts for managed FFmpeg discovery."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import media_tools

GENERATION = "g-test-generation"
PROFILE = "stem-runtime-v1"
SET_ID = "portable-media-test"
FFMPEG_BYTES = b"fixture ffmpeg executable"
FFPROBE_BYTES = b"fixture ffprobe executable"


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _make_executable(path: Path) -> None:
    if sys.platform != "win32":
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def managed_runtime(tmp_path):
    config = tmp_path / "config"
    base = config / "demucs-server"
    root = base / "generations" / GENERATION
    tool_dir = root / "tools" / "bin"
    tool_dir.mkdir(parents=True)
    suffix = ".exe" if sys.platform == "win32" else ""
    names = (f"ffmpeg{suffix}", f"ffprobe{suffix}")
    payloads = dict(zip(names, (FFMPEG_BYTES, FFPROBE_BYTES), strict=True))
    tool_paths = {}
    file_receipts = []
    for name, payload in payloads.items():
        path = tool_dir / name
        path.write_bytes(payload)
        _make_executable(path)
        tool_paths[name] = path
        file_receipts.append(
            {
                "path": name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )

    archive = {
        "sha256": hashlib.sha256(b"fixture archive").hexdigest(),
        "size": 12345,
    }
    manifest = {
        "schema_version": 2,
        "media_tools": {
            SET_ID: {
                "revision": "ffmpeg-test-v1",
                "platform": sys.platform,
                "architecture": media_tools._architecture(),
                "profiles": [PROFILE],
                "archives": [{**archive, "url": "https://invalid.example/fixture"}],
            }
        },
    }
    manifest_path = root / "src" / "runtime-manifest.json"
    _write_json(manifest_path, manifest)
    receipt = {
        "schema_version": 2,
        "owner": "stem_splitter",
        "generation_id": GENERATION,
        "profile_id": PROFILE,
        "platform": sys.platform,
        "prepared": True,
        "validated": True,
        "manifest_sha256": media_tools._digest(manifest),
        "media_tools": {
            "set_id": SET_ID,
            "revision": "ffmpeg-test-v1",
            "platform": sys.platform,
            "architecture": media_tools._architecture(),
            "asset_dir": "tools/bin",
            "archives": [archive],
            "files": file_receipts,
        },
    }
    receipt_path = root / "receipt.json"
    pointer_path = base / "active.json"
    _write_json(receipt_path, receipt)
    _write_json(pointer_path, {"schema_version": 1, "generation_id": GENERATION})
    return {
        "config": config,
        "base": base,
        "root": root,
        "tool_dir": tool_dir,
        "tool_paths": tool_paths,
        "ffmpeg": tool_paths[names[0]],
        "ffprobe": tool_paths[names[1]],
        "pointer_path": pointer_path,
        "receipt_path": receipt_path,
        "manifest_path": manifest_path,
    }


def _resolver(runtime, host=lambda: None):
    return media_tools.FFmpegResolver(runtime["config"], host)


def _rewrite(path: Path, transform) -> dict:
    value = _read_json(path)
    transform(value)
    _write_json(path, value)
    return value


def _rebind_manifest(runtime) -> None:
    manifest = _read_json(runtime["manifest_path"])
    _rewrite(
        runtime["receipt_path"],
        lambda receipt: receipt.update(manifest_sha256=media_tools._digest(manifest)),
    )


def test_host_ffmpeg_has_priority_without_reading_managed_state(tmp_path, monkeypatch):
    host_path = tmp_path / "desktop" / "ffmpeg.exe"
    calls = []
    resolver = media_tools.FFmpegResolver(tmp_path / "missing-config", lambda: host_path)
    monkeypatch.setattr(
        media_tools,
        "_read_object",
        lambda path: calls.append(path) or pytest.fail("managed state was read"),
    )

    status = resolver.inspect()

    assert status == {
        "available": True,
        "path": str(host_path),
        "source": "desktop",
        "reason": "FeedBack's bundled FFmpeg is ready.",
        "generation_id": None,
    }
    assert calls == []


def test_host_resolver_failure_falls_back_to_verified_generation(managed_runtime):
    def broken_host():
        raise RuntimeError("host lookup failed")

    status = _resolver(managed_runtime, broken_host).inspect()

    assert status["available"] is True
    assert status["source"] == "stem_splitter_managed"
    assert status["path"] == str(managed_runtime["ffmpeg"].resolve())


def test_v2_hash_text_is_case_insensitive_like_stem_splitter(managed_runtime):
    manifest = _read_json(managed_runtime["manifest_path"])
    manifest["media_tools"][SET_ID]["archives"][0]["sha256"] = (
        manifest["media_tools"][SET_ID]["archives"][0]["sha256"].upper()
    )
    _write_json(managed_runtime["manifest_path"], manifest)
    receipt = _read_json(managed_runtime["receipt_path"])
    receipt["manifest_sha256"] = media_tools._digest(manifest).upper()
    receipt["media_tools"]["archives"][0]["sha256"] = (
        receipt["media_tools"]["archives"][0]["sha256"].upper()
    )
    for entry in receipt["media_tools"]["files"]:
        entry["sha256"] = entry["sha256"].upper()
    _write_json(managed_runtime["receipt_path"], receipt)

    status = _resolver(managed_runtime).inspect()

    assert status["available"] is True
    assert status["path"] == str(managed_runtime["ffmpeg"].resolve())


def test_valid_generation_is_hashed_once_then_served_from_cache(managed_runtime, monkeypatch):
    calls = []
    real_hash = media_tools._hash_file

    def counted_hash(path):
        calls.append(Path(path).name)
        return real_hash(path)

    monkeypatch.setattr(media_tools, "_hash_file", counted_hash)
    resolver = _resolver(managed_runtime)

    first = resolver.inspect()
    second = resolver.inspect()

    assert first == second
    assert first["available"] is True
    assert first["generation_id"] == GENERATION
    assert sorted(calls) == sorted(path.name for path in managed_runtime["tool_paths"].values())


def test_force_verification_rehashes_the_canonical_pair(managed_runtime, monkeypatch):
    calls = []
    real_hash = media_tools._hash_file

    def counted_hash(path):
        calls.append(Path(path).name)
        return real_hash(path)

    monkeypatch.setattr(media_tools, "_hash_file", counted_hash)
    resolver = _resolver(managed_runtime)
    assert resolver.resolve() == str(managed_runtime["ffmpeg"].resolve())

    assert resolver.require_verified() == str(managed_runtime["ffmpeg"].resolve())
    assert len(calls) == 4


def test_concurrent_resolution_performs_one_hash_pass(managed_runtime, monkeypatch):
    calls = []
    real_hash = media_tools._hash_file

    def counted_hash(path):
        calls.append(Path(path).name)
        return real_hash(path)

    monkeypatch.setattr(media_tools, "_hash_file", counted_hash)
    resolver = _resolver(managed_runtime)
    with ThreadPoolExecutor(max_workers=4) as workers:
        statuses = list(workers.map(lambda _index: resolver.inspect(), range(4)))

    assert all(status["available"] for status in statuses)
    assert len(calls) == 2


def test_changed_tool_invalidates_cache_and_fails_closed(managed_runtime):
    resolver = _resolver(managed_runtime)
    assert resolver.inspect()["available"] is True
    original_stat = managed_runtime["ffmpeg"].stat()
    managed_runtime["ffmpeg"].write_bytes(b"X" * len(FFMPEG_BYTES))
    os.utime(
        managed_runtime["ffmpeg"],
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000),
    )

    status = resolver.inspect()

    assert status["available"] is False
    assert status["path"] is None
    assert "failed verification" in status["reason"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2.0),
        ("owner", "another_plugin"),
        ("generation_id", "g-other"),
        ("prepared", False),
        ("validated", False),
        ("platform", "another-platform"),
    ],
)
def test_receipt_identity_and_activation_must_match(managed_runtime, field, value):
    _rewrite(managed_runtime["receipt_path"], lambda receipt: receipt.__setitem__(field, value))

    status = _resolver(managed_runtime).inspect()

    assert status["available"] is False
    assert "not verified" in status["reason"]


@pytest.mark.parametrize(
    "pointer",
    [
        {"schema_version": 1.0, "generation_id": GENERATION},
        {"schema_version": 1, "generation_id": "../outside"},
        {"schema_version": 1, "generation_id": "legacy"},
        {"schema_version": 1, "generation_id": "g-missing"},
    ],
)
def test_pointer_must_select_a_strict_contained_generation(managed_runtime, pointer):
    _write_json(managed_runtime["pointer_path"], pointer)

    status = _resolver(managed_runtime).inspect()

    assert status["available"] is False
    assert status["path"] is None


def test_manifest_content_is_bound_to_receipt_digest(managed_runtime):
    _rewrite(
        managed_runtime["manifest_path"],
        lambda manifest: manifest["media_tools"][SET_ID].update(revision="tampered"),
    )

    status = _resolver(managed_runtime).inspect()

    assert status["available"] is False
    assert "manifest failed verification" in status["reason"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.update(schema_version=2.0),
        lambda manifest: manifest["media_tools"][SET_ID].update(revision="other-revision"),
        lambda manifest: manifest["media_tools"][SET_ID].update(platform="other-platform"),
        lambda manifest: manifest["media_tools"][SET_ID].update(profiles=["other-profile"]),
    ],
)
def test_manifest_contract_mismatch_fails_even_with_rebound_digest(managed_runtime, mutate):
    _rewrite(managed_runtime["manifest_path"], mutate)
    _rebind_manifest(managed_runtime)

    status = _resolver(managed_runtime).inspect()

    assert status["available"] is False
    assert status["path"] is None


def test_archive_receipt_must_match_manifest(managed_runtime):
    different_digest = hashlib.sha256(b"different archive").hexdigest()
    _rewrite(
        managed_runtime["receipt_path"],
        lambda receipt: receipt["media_tools"]["archives"][0].update(
            sha256=different_digest
        ),
    )

    status = _resolver(managed_runtime).inspect()

    assert status["available"] is False
    assert "archives do not match" in status["reason"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipt: receipt["media_tools"].update(asset_dir="../outside"),
        lambda receipt: receipt["media_tools"].update(architecture="other-architecture"),
        lambda receipt: receipt["media_tools"].update(files=[]),
        lambda receipt: receipt["media_tools"]["files"].append(
            receipt["media_tools"]["files"][0].copy()
        ),
    ],
)
def test_media_receipt_shape_tampering_fails_closed(managed_runtime, mutate):
    _rewrite(managed_runtime["receipt_path"], mutate)

    status = _resolver(managed_runtime).inspect()

    assert status["available"] is False
    assert status["path"] is None


def test_tool_size_and_hash_are_both_enforced(managed_runtime):
    receipt = _read_json(managed_runtime["receipt_path"])
    receipt["media_tools"]["files"][0]["size"] += 1
    _write_json(managed_runtime["receipt_path"], receipt)
    assert "size check" in _resolver(managed_runtime).inspect()["reason"]

    receipt["media_tools"]["files"][0]["size"] -= 1
    receipt["media_tools"]["files"][0]["sha256"] = "0" * 64
    _write_json(managed_runtime["receipt_path"], receipt)
    assert "failed verification" in _resolver(managed_runtime).inspect()["reason"]


def test_pointer_switch_during_hash_never_returns_old_path(managed_runtime, monkeypatch):
    real_hash = media_tools._hash_file
    switched = False

    def switch_pointer(path):
        nonlocal switched
        result = real_hash(path)
        if not switched:
            switched = True
            _write_json(
                managed_runtime["pointer_path"],
                {"schema_version": 1, "generation_id": "g-replacement"},
            )
        return result

    monkeypatch.setattr(media_tools, "_hash_file", switch_pointer)

    status = _resolver(managed_runtime).inspect()

    assert status["available"] is False
    assert status["path"] is None
    assert "changed runtimes" in status["reason"]


def test_generation_symlink_is_rejected_when_supported(managed_runtime):
    link = managed_runtime["base"] / "generations" / "g-linked"
    try:
        link.symlink_to(managed_runtime["root"], target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    _write_json(
        managed_runtime["pointer_path"],
        {"schema_version": 1, "generation_id": "g-linked"},
    )

    status = _resolver(managed_runtime).inspect()

    assert status["available"] is False
    assert status["path"] is None


def test_junction_like_tool_directory_is_rejected(managed_runtime, monkeypatch):
    real_isjunction = getattr(media_tools.os.path, "isjunction", lambda _path: False)

    def fake_isjunction(path):
        return Path(path) == managed_runtime["tool_dir"] or real_isjunction(path)

    monkeypatch.setattr(media_tools.os.path, "isjunction", fake_isjunction, raising=False)

    status = _resolver(managed_runtime).inspect()

    assert status["available"] is False
    assert status["path"] is None
    assert "folder is missing" in status["reason"]


def test_public_status_never_exposes_an_executable_or_config_path(managed_runtime):
    resolver = _resolver(managed_runtime)

    public = resolver.public_status()
    serialized = json.dumps(public)

    assert public["available"] is True
    assert public["source"] == "stem_splitter_managed"
    assert "path" not in public
    assert str(managed_runtime["config"]) not in serialized
    assert str(managed_runtime["ffmpeg"]) not in serialized


def test_unavailable_public_status_is_also_path_free(managed_runtime):
    managed_runtime["ffmpeg"].unlink()
    resolver = _resolver(managed_runtime)

    public = resolver.public_status()
    serialized = json.dumps(public)

    assert public["available"] is False
    assert "path" not in public
    assert str(managed_runtime["config"]) not in serialized
