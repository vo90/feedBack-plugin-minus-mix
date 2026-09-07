"""Filesystem containment and bounded resource policy for audio reuse jobs."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
from pathlib import Path


def inside(path, root):
    return Path(path).is_relative_to(Path(root))


def checked_root(value, error):
    if not isinstance(value, str) or not value.strip():
        raise error("Choose all three folders.")
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise error("Choose an existing absolute folder.")
    return path.resolve()


def checked_path(root, relative, error, *, exists=True):
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise error("Invalid relative package path.")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise error("Unsafe relative package path.")
    for attempt in range(2):
        target = Path(root)
        for part in parts:
            target = target / part
            if target.is_symlink() or getattr(target, "is_junction", lambda: False)():
                raise error("Linked source/output paths are not followed.")
        resolved = target.resolve()
        if inside(resolved, root):
            if exists and not resolved.is_file():
                break
            return resolved
        # On Windows, another worker creating a missing output parent can
        # change PATH_NOT_FOUND to FILE_NOT_FOUND inside non-strict resolve(),
        # leaving its extended prefix intact. Recheck every component once;
        # never strip namespaces or accept a persistent containment mismatch.
        if (attempt or exists or os.name != "nt"
                or not str(resolved).startswith("\\\\?\\")
                or str(root).startswith("\\\\?\\")):
            break
    raise error("Package moved outside its folder or is unavailable.")


def roots(old_dir, fresh_dir, output_dir, error):
    values = [checked_root(value, error) for value in (old_dir, fresh_dir, output_dir)]
    if any(inside(left, right) or inside(right, left)
           for index, left in enumerate(values) for right in values[index + 1:]):
        raise error("All three folders must be separate, non-overlapping trees.")
    return values


def walk(root, *, match, cancel=None):
    count = 0

    def failed(error):
        raise match.ReuseError(f"Folder scan could not read a directory: {error}")

    for current, directories, filenames in os.walk(root, followlinks=False, onerror=failed):
        match.checkpoint(cancel)
        directories[:] = sorted(
            name for name in directories
            if not (Path(current) / name).is_symlink()
            and not getattr(Path(current) / name, "is_junction", lambda: False)()
        )
        for name in sorted(filenames, key=str.casefold):
            if Path(name).suffix.lower() not in (".feedpak", ".sloppak"):
                continue
            match.checkpoint(cancel)
            relative = (Path(current) / name).relative_to(root).as_posix()
            path = checked_path(root, relative, match.ReuseError)
            count += 1
            if count > match.MAX_FILES:
                raise match.ReuseError(f"Folder exceeds the {match.MAX_FILES:,} package limit.")
            yield relative, path


def _available_memory():
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        *[(name, ctypes.c_ulonglong) for name in (
                            "total", "available", "page_total", "page_available",
                            "virtual_total", "virtual_available", "extended")]]
        value = MemoryStatus()
        value.length = ctypes.sizeof(value)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
            return value.available
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return 1024**3


def _storage_kind():
    # Be conservative on mixed, remote or unknown storage. No external tool is
    # installed; Windows' built-in read-only inventory is optional.
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "@(Get-PhysicalDisk | Select-Object -ExpandProperty MediaType) | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=8, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            values = json.loads(result.stdout)
            values = values if isinstance(values, list) else [values]
            if values and all(str(value).casefold() in ("ssd", "4") for value in values):
                return "ssd"
            if any(str(value).casefold() in ("hdd", "3") for value in values):
                return "hdd"
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return "unknown"


def resources(requested="auto", *, hardware=None, error=ValueError):
    if requested != "auto" and (isinstance(requested, bool)
                               or not str(requested).isdigit() or not 1 <= int(requested) <= 16):
        raise error("Workers must be Auto or an integer from 1 to 16.")
    hardware = hardware or {"cpu_count": os.cpu_count() or 1,
                            "memory_available": _available_memory(), "storage": _storage_kind()}
    cpu = max(1, int(hardware["cpu_count"]))
    available = max(0, int(hardware["memory_available"]))
    storage = hardware.get("storage", "unknown")
    # Reserve 512 MiB per active archive/chart worker and 512 MiB for the host.
    memory_cap = max(1, (available - 512 * 1024**2) // (512 * 1024**2))
    maximum = max(1, min(16, cpu, memory_cap, 8 if storage == "ssd" else 2))
    automatic = min(maximum, 2 if storage == "ssd" else 1)
    effective = automatic if requested == "auto" else min(int(requested), maximum)
    return {"requested_workers": requested, "effective_workers": effective,
            "auto_workers": automatic, "maximum_workers": maximum, "cpu_count": cpu,
            "memory_available": available, "storage": storage,
            "reason": "Bounded by CPU, available memory and storage; Auto favors steady disk throughput."}


def admission(output_root, estimated_bytes, workers, error=ValueError):
    reserve = max(256 * 1024**2, workers * 64 * 1024**2)
    if shutil.disk_usage(output_root).free < estimated_bytes + reserve:
        raise error("Insufficient output disk space for the next copies plus a safety reserve.")
    if _available_memory() < 256 * 1024**2 + workers * 128 * 1024**2:
        raise error("Available memory is too low for the selected workers; retry with fewer workers.")
