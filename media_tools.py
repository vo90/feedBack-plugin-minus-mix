"""Resolve FFmpeg without weakening the managed-runtime trust boundary.

Packaged FeedBack builds provide FFmpeg through the host ``audio`` module.  A
source-based development runtime may not have that Desktop bundle, while Stem
Splitter can still own a verified FFmpeg pair inside its active generation.
MinusMix may reuse that executable only after independently checking the local
activation pointer, ownership receipt, containment, size and SHA-256 digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_GENERATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_TOOLSET_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA256 = re.compile(r"^[A-Fa-f0-9]{64}$")
_MAX_TOOL_BYTES = 1024**3
_MAX_JSON_BYTES = 2 * 1024**2


def _read_object(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _architecture() -> str:
    machine = platform.machine().strip().lower().replace("-", "_")
    if machine in {"amd64", "x64", "x86_64"}:
        return "x86_64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    return machine


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_link_like(path: Path) -> bool:
    """Treat POSIX links and Windows junctions as untrusted runtime content."""
    try:
        return path.is_symlink() or bool(
            getattr(os.path, "isjunction", lambda _path: False)(path)
        )
    except OSError:
        return True


def _contained(base: Path, candidate: Path) -> Path | None:
    """Resolve an existing descendant while rejecting every link-like component."""
    try:
        resolved_base = base.resolve(strict=True)
        relative = candidate.relative_to(base)
        cursor = base
        for part in relative.parts:
            cursor = cursor / part
            if _is_link_like(cursor):
                return None
        resolved = candidate.resolve(strict=True)
    except (OSError, ValueError):
        return None
    if not resolved.is_relative_to(resolved_base):
        return None
    return resolved


class _ContractError(ValueError):
    """A safe, user-facing refusal to consume managed runtime content."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise _ContractError(reason)


@dataclass(frozen=True)
class _ToolContract:
    path: Path
    stat: os.stat_result
    sha256: str


@dataclass(frozen=True)
class _RuntimeContract:
    pointer_path: Path
    generation: str
    receipt_path: Path
    receipt_digest: str
    manifest_path: Path
    manifest_digest: str
    tools: dict[str, _ToolContract]
    ffmpeg_name: str
    cache_key: tuple[Any, ...]


def _active_generation(config_dir: Path) -> tuple[Path, Path, str, Path]:
    base = config_dir / "demucs-server"
    pointer_path = base / "active.json"
    _require(
        not _is_link_like(base) and not _is_link_like(pointer_path),
        "Stem Splitter's active runtime pointer is not trusted.",
    )
    pointer = _read_object(pointer_path)
    generation = pointer.get("generation_id")
    valid_pointer = (
        type(pointer.get("schema_version")) is int
        and pointer.get("schema_version") == 1
        and isinstance(generation, str)
        and generation != "legacy"
        and _GENERATION_ID.fullmatch(generation) is not None
    )
    _require(
        valid_pointer,
        "FFmpeg is unavailable. Install or update Stem Splitter's managed server, "
        "or repair the desktop app.",
    )
    generations = base / "generations"
    _require(
        not _is_link_like(generations),
        "Stem Splitter's managed runtime folder is not trusted.",
    )
    root = _contained(generations, generations / generation)
    _require(
        root is not None and root.is_dir(),
        "Stem Splitter's active managed runtime is missing.",
    )
    return base, pointer_path, generation, root


def _receipt_contract(root: Path, generation: str) -> tuple[Path, dict, dict, Path]:
    receipt_path = root / "receipt.json"
    _require(
        _contained(root, receipt_path) is not None,
        "Stem Splitter's managed runtime receipt is not trusted.",
    )
    receipt = _read_object(receipt_path)
    valid_receipt = (
        type(receipt.get("schema_version")) is int
        and receipt.get("schema_version") == 2
        and receipt.get("owner") == "stem_splitter"
        and receipt.get("generation_id") == generation
        and receipt.get("prepared") is True
        and receipt.get("validated") is True
        and receipt.get("platform") == sys.platform
    )
    _require(
        valid_receipt,
        "Stem Splitter's active managed runtime is not verified.",
    )
    media = receipt.get("media_tools")
    _require(
        isinstance(media, dict),
        "Stem Splitter's active runtime has no verified media tools.",
    )
    _require(
        media.get("platform") == sys.platform
        and media.get("architecture") == _architecture(),
        "Stem Splitter's verified media tools do not match this computer.",
    )
    _require(
        media.get("asset_dir") == "tools/bin",
        "Stem Splitter's media-tool receipt is incomplete.",
    )
    asset_root = _contained(root, root / "tools" / "bin")
    _require(
        asset_root is not None and asset_root.is_dir(),
        "Stem Splitter's verified media-tool folder is missing.",
    )
    return receipt_path, receipt, media, asset_root


def _archive_digest(item: dict[str, Any]) -> str:
    return str(item["sha256"])


def _manifest_archive(item: Any) -> dict[str, Any]:
    valid = (
        isinstance(item, dict)
        and isinstance(item.get("sha256"), str)
        and _SHA256.fullmatch(item["sha256"]) is not None
        and type(item.get("size")) is int
        and item["size"] >= 1
    )
    _require(valid, "Stem Splitter's FFmpeg manifest is invalid.")
    return {"sha256": item["sha256"].lower(), "size": item["size"]}


def _receipt_archive(item: Any) -> dict[str, Any]:
    valid = (
        isinstance(item, dict)
        and set(item) == {"sha256", "size"}
        and isinstance(item.get("sha256"), str)
        and _SHA256.fullmatch(item["sha256"]) is not None
        and type(item.get("size")) is int
        and item["size"] >= 1
    )
    _require(valid, "Stem Splitter's FFmpeg archive receipt is invalid.")
    return {"sha256": item["sha256"].lower(), "size": item["size"]}


def _verify_archives(spec: dict, media: dict) -> None:
    manifest_rows = spec.get("archives")
    receipt_rows = media.get("archives")
    _require(
        isinstance(manifest_rows, list) and isinstance(receipt_rows, list),
        "Stem Splitter's FFmpeg archive receipt is incomplete.",
    )
    expected = sorted((_manifest_archive(item) for item in manifest_rows), key=_archive_digest)
    actual = sorted((_receipt_archive(item) for item in receipt_rows), key=_archive_digest)
    _require(
        actual == expected,
        "Stem Splitter's FFmpeg archives do not match their manifest.",
    )


def _manifest_contract(root: Path, receipt: dict, media: dict) -> tuple[Path, dict]:
    manifest_path = root / "src" / "runtime-manifest.json"
    _require(
        _contained(root, manifest_path) is not None,
        "Stem Splitter's runtime manifest is not trusted.",
    )
    manifest = _read_object(manifest_path)
    manifest_sha256 = receipt.get("manifest_sha256")
    valid_manifest = (
        type(manifest.get("schema_version")) is int
        and manifest.get("schema_version") == 2
        and isinstance(manifest_sha256, str)
        and _SHA256.fullmatch(manifest_sha256) is not None
        and manifest_sha256.lower() == _digest(manifest)
    )
    _require(valid_manifest, "Stem Splitter's runtime manifest failed verification.")
    set_id = media.get("set_id")
    catalog = manifest.get("media_tools")
    valid_set_id = isinstance(set_id, str) and _TOOLSET_ID.fullmatch(set_id) is not None
    spec = catalog.get(set_id) if isinstance(catalog, dict) and valid_set_id else None
    _require(
        isinstance(spec, dict),
        "Stem Splitter's FFmpeg toolset is absent from its manifest.",
    )
    profile_id = receipt.get("profile_id")
    profiles = spec.get("profiles")
    matches_manifest = (
        isinstance(profile_id, str)
        and isinstance(profiles, list)
        and profile_id in profiles
        and spec.get("revision") == media.get("revision")
        and spec.get("platform") == media.get("platform")
        and spec.get("architecture") == media.get("architecture")
    )
    _require(
        matches_manifest,
        "Stem Splitter's FFmpeg receipt does not match its manifest.",
    )
    _verify_archives(spec, media)
    return manifest_path, manifest


def _tool_entry(
    root: Path,
    asset_root: Path,
    expected_name: str,
    entry: Any,
) -> _ToolContract:
    valid_entry = (
        isinstance(entry, dict)
        and set(entry) == {"path", "sha256", "size"}
        and entry.get("path") == expected_name
        and isinstance(entry.get("sha256"), str)
        and _SHA256.fullmatch(entry["sha256"]) is not None
        and type(entry.get("size")) is int
        and 0 < entry["size"] <= _MAX_TOOL_BYTES
    )
    _require(valid_entry, "Stem Splitter's FFmpeg receipt is invalid.")
    executable = _contained(root, asset_root / expected_name)
    executable_ok = executable is not None and executable.is_file()
    if executable_ok and sys.platform != "win32":
        executable_ok = os.access(executable, os.X_OK)
    _require(
        executable_ok,
        "Stem Splitter's verified media-tool executable is missing.",
    )
    try:
        tool_stat = executable.stat()
    except OSError as exc:
        raise _ContractError(
            "Stem Splitter's media-tool executable is unavailable."
        ) from exc
    _require(
        tool_stat.st_size == entry["size"],
        "Stem Splitter's media-tool executable failed its size check.",
    )
    return _ToolContract(executable, tool_stat, entry["sha256"].lower())


def _tools_contract(root: Path, asset_root: Path, media: dict) -> tuple[dict[str, _ToolContract], str]:
    suffix = ".exe" if sys.platform == "win32" else ""
    expected_names = {f"ffmpeg{suffix}", f"ffprobe{suffix}"}
    entries = media.get("files")
    _require(
        isinstance(entries, list) and len(entries) == len(expected_names),
        "Stem Splitter's media-tool receipt is incomplete.",
    )
    by_name: dict[str, dict] = {}
    for entry in entries:
        _require(
            isinstance(entry, dict) and isinstance(entry.get("path"), str),
            "Stem Splitter's FFmpeg receipt is invalid.",
        )
        name = entry["path"]
        _require(
            name not in by_name,
            "Stem Splitter's media-tool receipt must identify one canonical pair.",
        )
        by_name[name] = entry
    _require(
        set(by_name) == expected_names,
        "Stem Splitter's media-tool receipt must identify one canonical pair.",
    )
    tools = {
        name: _tool_entry(root, asset_root, name, by_name[name])
        for name in sorted(expected_names)
    }
    return tools, f"ffmpeg{suffix}"


def _stat_key(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _build_contract(config_dir: Path) -> _RuntimeContract:
    _, pointer_path, generation, root = _active_generation(config_dir)
    receipt_path, receipt, media, asset_root = _receipt_contract(root, generation)
    manifest_path, manifest = _manifest_contract(root, receipt, media)
    tools, ffmpeg_name = _tools_contract(root, asset_root, media)
    try:
        receipt_stat = receipt_path.stat()
        manifest_stat = manifest_path.stat()
    except OSError as exc:
        raise _ContractError(
            "Stem Splitter's verified FFmpeg receipt is unavailable."
        ) from exc
    receipt_digest = _digest(receipt)
    manifest_digest = _digest(manifest)
    cache_key = (
        generation,
        _stat_key(receipt_stat),
        _stat_key(manifest_stat),
        receipt_digest,
        manifest_digest,
        tuple(
            (name, _stat_key(tool.stat), tool.sha256)
            for name, tool in sorted(tools.items())
        ),
    )
    return _RuntimeContract(
        pointer_path,
        generation,
        receipt_path,
        receipt_digest,
        manifest_path,
        manifest_digest,
        tools,
        ffmpeg_name,
        cache_key,
    )


def _rehash_tools(contract: _RuntimeContract) -> bool:
    for tool in contract.tools.values():
        try:
            actual_hash = _hash_file(tool.path)
            stable_stat = tool.path.stat()
        except OSError:
            return False
        if actual_hash != tool.sha256 or _stat_key(stable_stat) != _stat_key(tool.stat):
            return False
    return True


def _contract_unchanged(contract: _RuntimeContract) -> bool:
    if _is_link_like(contract.pointer_path):
        return False
    pointer = _read_object(contract.pointer_path)
    pointer_current = (
        type(pointer.get("schema_version")) is int
        and pointer.get("schema_version") == 1
        and pointer.get("generation_id") == contract.generation
    )
    if not pointer_current:
        return False
    if _digest(_read_object(contract.receipt_path)) != contract.receipt_digest:
        return False
    if _digest(_read_object(contract.manifest_path)) != contract.manifest_digest:
        return False
    try:
        return all(
            _stat_key(tool.path.stat()) == _stat_key(tool.stat)
            for tool in contract.tools.values()
        )
    except OSError:
        return False


class FFmpegResolver:
    """Prefer the host binary, then an active receipt-verified managed binary."""

    def __init__(
        self,
        config_dir: Path,
        host_resolver: Callable[[], str | None],
    ) -> None:
        self.config_dir = Path(config_dir).resolve()
        self.host_resolver = host_resolver
        self._lock = threading.RLock()
        self._cache_key: tuple[Any, ...] | None = None
        self._cache_path: str | None = None
        self._cache_generation: str | None = None

    @staticmethod
    def _unavailable(reason: str) -> dict[str, Any]:
        return {
            "available": False,
            "path": None,
            "source": "unavailable",
            "reason": reason,
            "generation_id": None,
        }

    @staticmethod
    def _available(path: str, source: str, reason: str, generation_id=None) -> dict[str, Any]:
        return {
            "available": True,
            "path": path,
            "source": source,
            "reason": reason,
            "generation_id": generation_id,
        }

    def _host_status(self) -> dict[str, Any] | None:
        try:
            value = self.host_resolver()
        except Exception:
            value = None
        if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
            return None
        return self._available(
            str(value),
            "desktop",
            "FeedBack's bundled FFmpeg is ready.",
        )

    def _managed_status(self, *, force_verify: bool) -> dict[str, Any]:
        try:
            contract = _build_contract(self.config_dir)
        except _ContractError as exc:
            return self._unavailable(str(exc))

        with self._lock:
            cache_hit = bool(
                not force_verify
                and contract.cache_key == self._cache_key
                and self._cache_path
            )
            if not cache_hit and not _rehash_tools(contract):
                self._cache_key = None
                self._cache_path = None
                self._cache_generation = None
                return self._unavailable(
                    "Stem Splitter's media-tool executable failed verification."
                )
            if not _contract_unchanged(contract):
                self._cache_key = None
                self._cache_path = None
                self._cache_generation = None
                return self._unavailable(
                    "Stem Splitter changed runtimes while FFmpeg was verified; retry."
                )
            if cache_hit:
                return self._available(
                    self._cache_path,
                    "stem_splitter_managed",
                    "Stem Splitter's verified managed FFmpeg is ready.",
                    self._cache_generation,
                )

            self._cache_key = contract.cache_key
            self._cache_path = str(contract.tools[contract.ffmpeg_name].path)
            self._cache_generation = contract.generation
            return self._available(
                self._cache_path,
                "stem_splitter_managed",
                "Stem Splitter's verified managed FFmpeg is ready.",
                contract.generation,
            )

    def inspect(self, *, force_verify: bool = False) -> dict[str, Any]:
        host = self._host_status()
        return host if host is not None else self._managed_status(force_verify=force_verify)

    def resolve(self) -> str | None:
        return self.inspect().get("path")

    def require_verified(self) -> str:
        status = self.inspect(force_verify=True)
        if not status["available"]:
            raise RuntimeError(status["reason"])
        return str(status["path"])

    def public_status(self) -> dict[str, Any]:
        status = self.inspect()
        return {key: value for key, value in status.items() if key != "path"}
