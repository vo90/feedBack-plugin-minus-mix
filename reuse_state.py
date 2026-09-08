"""Strict shape checks for private resumable queue checkpoints."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

STATUSES = {"scanning", "running", "canceling", "ready", "canceled", "failed", "completed", "interrupted"}
ROW_STATUSES = {"ready", "review", "blocked", "done", "failed", "skipped", "running"}


def validate(data, policy, limit, output_limit=50_000):
    if not isinstance(data, dict) or data.get("policy") != policy:
        raise ValueError("Checkpoint policy/shape is unsupported.")
    if not re.fullmatch(r"[a-f0-9]{32}", str(data.get("id", ""))) or data.get("status") not in STATUSES:
        raise ValueError("Checkpoint identity/status is invalid.")
    for key in ("old_dir", "fresh_dir", "output_dir"):
        if not isinstance(data.get(key), str) or not Path(data[key]).is_absolute():
            raise ValueError("Checkpoint folders are invalid.")
    for key, expected in (("_donors", dict), ("resources", dict), ("source_errors", list)):
        if not isinstance(data.get(key), expected) or len(data[key]) > limit:
            raise ValueError("Checkpoint collection is invalid or oversized.")
    if not isinstance(data.get("groups"), dict) or len(data["groups"]) > output_limit:
        raise ValueError("Checkpoint variant groups are invalid or oversized.")
    count = data.get("input_packages_total")
    if type(count) is not int or not 0 <= count <= limit:
        raise ValueError("Checkpoint input count is invalid.")
    if not isinstance(data.get("_scan_complete"), bool) or "_workers" not in data:
        raise ValueError("Checkpoint review state is missing.")
    if not isinstance(data.get("_root_ids"), list) or len(data["_root_ids"]) != 3:
        raise ValueError("Checkpoint root identities are missing.")
    if not isinstance(data.get("items"), list) or len(data["items"]) > output_limit:
        raise ValueError("Checkpoint target list is invalid or oversized.")
    seen = set()
    outputs = set()
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
        _validate_variant(row, data, outputs)
    _validate_groups(data)
    return data


def _validate_groups(data):
    for key, group in data["groups"].items():
        if not isinstance(group, dict) or group.get("id") != key or not isinstance(group.get("candidates"), list):
            raise ValueError("Checkpoint recording group is invalid.")
        if any(not isinstance(item, dict) or item.get("id") not in data["_donors"]
               for item in group["candidates"]):
            raise ValueError("Checkpoint recording choices are invalid.")
        stems = group.get("excluded_stems")
        if not _canonical(stems) or not stems or not isinstance(group.get("variant_label"), str):
            raise ValueError("Checkpoint recording variant is invalid.")
        if any(data["_donors"][item["id"]].get("excluded_stems") != stems
               for item in group["candidates"]):
            raise ValueError("Checkpoint recording choice crosses mix variants.")


def _canonical(stems):
    return (isinstance(stems, list)
            and all(isinstance(stem, str) and stem and stem == stem.strip().lower()
                    and stem != "full" for stem in stems)
            and stems == sorted(set(stems)))


def _validate_variant(row, data, outputs):
    stems = row.get("excluded_stems")
    if not _canonical(stems) or not isinstance(row.get("variant_label"), str):
        raise ValueError("Checkpoint target variant is invalid.")
    identity = hashlib.sha256(json.dumps([row["relative_path"], stems]).encode()).hexdigest()
    if row["id"] != identity:
        raise ValueError("Checkpoint target identity does not match its mix variant.")
    if row["donor_relative"] is not None:
        donor = data["_donors"][row["donor_relative"]]
        if not isinstance(donor, dict) or donor.get("excluded_stems") != stems or not stems:
            raise ValueError("Checkpoint donor does not match the target mix variant.")
    if row["group_id"] is not None:
        group = data["groups"][row["group_id"]]
        if not isinstance(group, dict) or group.get("excluded_stems") != stems:
            raise ValueError("Checkpoint group does not match the target mix variant.")
        if row["donor_relative"] is not None and row["donor_relative"] not in {
            candidate.get("id") for candidate in group.get("candidates", []) if isinstance(candidate, dict)
        }:
            raise ValueError("Checkpoint donor was not a reviewed candidate.")
    output = row["output_relative"]
    if output is not None:
        if not isinstance(output, str) or not output or output.casefold() in outputs:
            raise ValueError("Checkpoint has an invalid or duplicate output path.")
        outputs.add(output.casefold())
