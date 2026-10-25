from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .exporter import ExportError, _parse_time

RETENTION = timedelta(hours=25)
STATE_VERSION = 1


def merge_heartbeat_history(source: dict[str, Any], previous: dict[str, Any] | None,
                            now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge recent Kuma observations with persistent local history, deduped per monitor/time."""
    if now.tzinfo is None:
        raise ExportError("History merge time must include timezone")
    cutoff = now.astimezone(timezone.utc) - RETENTION
    old_monitors = (previous or {}).get("monitors", {})
    merged_source = {**source, "monitors": []}
    state_monitors: dict[str, list[dict[str, str]]] = {}
    for monitor in source.get("monitors", []):
        monitor_id = str(monitor.get("monitor_id", ""))
        if not monitor_id:
            raise ExportError("Internal monitor ID is missing for history merge")
        by_timestamp: dict[str, dict[str, str]] = {}
        for sample in [*old_monitors.get(monitor_id, []), *monitor.get("samples", [])]:
            stamp = _parse_time(sample.get("at"))
            if stamp < cutoff or stamp > now.astimezone(timezone.utc) + timedelta(minutes=5):
                continue
            # Kuma heartbeat timestamps are stable across responses. Timestamp is the
            # dedupe key; the incoming value wins if the same event was replayed.
            key = stamp.isoformat(timespec="microseconds")
            state = sample.get("state")
            if state not in {"up", "down", "maintenance"}:
                raise ExportError("Invalid state in heartbeat history")
            by_timestamp[key] = {"at": stamp.isoformat().replace("+00:00", "Z"), "state": state}
        samples = sorted(by_timestamp.values(), key=lambda sample: sample["at"])
        state_monitors[monitor_id] = samples
        merged_source["monitors"].append({**monitor, "samples": samples})
    return merged_source, {"schema_version": STATE_VERSION, "updated_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "monitors": state_monitors}


def load_history_state(path: Path) -> dict[str, Any] | None:
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        raise ExportError("Unable to read exporter history state") from None
    if state.get("schema_version") != STATE_VERSION or not isinstance(state.get("monitors"), dict):
        raise ExportError("Exporter history state has an unsupported schema")
    # Validate all saved data before using it; corrupted state fails closed.
    for monitor_id, samples in state["monitors"].items():
        if not isinstance(monitor_id, str) or not isinstance(samples, list):
            raise ExportError("Exporter history state is malformed")
        for sample in samples:
            _parse_time(sample.get("at"))
            if sample.get("state") not in {"up", "down", "maintenance"}:
                raise ExportError("Exporter history state is malformed")
    return state


def save_history_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically persist rolling state with restrictive default file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        temp_name = None
    except OSError:
        raise ExportError("Unable to save exporter history state") from None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
