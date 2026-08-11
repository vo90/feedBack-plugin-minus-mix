"""Spawn-safe helper for cross-process publication tests on Windows."""
# ruff: noqa: E402,I001
from __future__ import annotations

from pathlib import Path

from tests import conftest as host_shims

_ = host_shims  # Install FeedBack's standalone audio/sloppak shims before import.

import exporter  # noqa: E402


def publish_worker(output_tmp: str, output_dir: str, source: str,
                   ready, start, results) -> None:
    ready.put(True)
    start.wait(10)
    try:
        published = exporter._publish_unique_output(
            Path(output_tmp), Path(output_dir), Path(source), "No Guitar",
        )
        results.put((published.name, published.read_bytes().decode("ascii"), None))
    except Exception as exc:  # pragma: no cover - reported to the parent process
        results.put((None, None, repr(exc)))
