"""Strict shape checks for private resumable queue checkpoints."""
from __future__ import annotations

import re
from pathlib import Path

STATUSES = {"scanning", "running", "canceling", "ready", "canceled", "failed", "completed", "interrupted"}
ROW_STATUSES = {"ready", "review", "blocked", "done", "failed", "skipped", "running"}


def validate(data, policy, limit):
    if not isinstance(data, dict) or data.get("policy") != policy:
        raise ValueError("Checkpoint policy/shape is unsupported.")
    if not re.fullmatch(r"[a-f0-9]{32}", str(data.get("id", ""))) or data.get("status") not in STATUSES:
        raise ValueError("Checkpoint identity/status is invalid.")
    for key in ("old_dir", "fresh_dir", "output_dir"):
        if not isinstance(data.get(key), str) or not Path(data[key]).is_absolute():
            raise ValueError("Checkpoint folders are invalid.")
    for key, expected in (("groups", dict), ("_donors", dict), ("resources", dict), ("source_errors", list)):
        if not isinstance(data.get(key), expected) or len(data[key]) > limit:
            raise ValueError("Checkpoint collection is invalid or oversized.")
    if not isinstance(data.get("_scan_complete"), bool) or "_workers" not in data:
        raise ValueError("Checkpoint review state is missing.")
    if not isinstance(data.get("_root_ids"), list) or len(data["_root_ids"]) != 3:
        raise ValueError("Checkpoint root identities are missing.")
    if not isinstance(data.get("items"), list) or len(data["items"]) > limit:
        raise ValueError("Checkpoint target list is invalid or oversized.")
    seen = set()
    for row in data["items"]:
        if not isinstance(row, dict) or row.get("status") not in ROW_STATUSES:
            raise ValueError("Checkpoint target state is invalid.")
        for key in ("id", "relative_path", "title", "reason"):
            if not isinstance(row.get(key), str):
                raise ValueError("Checkpoint target fields are missing.")
        if row["id"] in seen:
            raise ValueError("Checkpoint has duplicate targets.")
        seen.add(row["id"])
        if not all(key in row for key in ("group_id", "donor_relative", "output_relative", "_fresh")):
            raise ValueError("Checkpoint target bindings are missing.")
        if row["donor_relative"] is not None and row["donor_relative"] not in data["_donors"]:
            raise ValueError("Checkpoint donor binding is missing.")
        if row["group_id"] is not None and row["group_id"] not in data["groups"]:
            raise ValueError("Checkpoint review group is missing.")
    _validate_groups(data)
    return data


def _validate_groups(data):
    for key, group in data["groups"].items():
        if not isinstance(group, dict) or group.get("id") != key or not isinstance(group.get("candidates"), list):
            raise ValueError("Checkpoint recording group is invalid.")
        if any(not isinstance(item, dict) or item.get("id") not in data["_donors"]
               for item in group["candidates"]):
            raise ValueError("Checkpoint recording choices are invalid.")
