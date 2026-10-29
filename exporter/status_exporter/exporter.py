from __future__ import annotations

import json
import ipaddress
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("status-exporter")
PUBLIC_ID = re.compile(r"[^a-z0-9-]+")
ALLOWED_FIELDS = {"name", "group", "description", "status", "metric_type", "uptime_24h", "history_24h", "samples", "last_success_at", "url"}


class ExportError(RuntimeError):
    pass


def public_id(value: str) -> str:
    result = PUBLIC_ID.sub("-", value.lower()).strip("-")[:48]
    if not result:
        raise ExportError("A monitor does not have a usable public identifier")
    return result


def build_public_payload(raw: dict[str, Any], generated_at: datetime | None = None) -> dict[str, Any]:
    """Transform an internal snapshot to an allow-listed public structure.

    Input contract is deliberately local and stable: a status-page identity plus monitor
    records and timestamped samples. The Kuma client adapts v2's authenticated API.
    """
    monitors = raw.get("monitors")
    if not isinstance(monitors, list) or not monitors:
        raise ExportError("Source returned no monitor records; refusing to publish an empty/green snapshot")
    now = generated_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ExportError("generated_at must include a timezone")
    services: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in monitors:
        name = _safe_string(item.get("public_name"), "monitor public_name", 80)
        sid = public_id(str(item.get("public_id") or name))
        if sid in seen:
            raise ExportError("Duplicate public service identifier")
        seen.add(sid)
        group = item.get("group")
        if group not in {"Infrastructure", "Applications", "Backup & Recovery"}:
            raise ExportError("Monitor has an invalid public group")
        description = _safe_string(item.get("public_description", ""), "public description", 160, allow_empty=True)
        samples = item.get("samples")
        if not isinstance(samples, list):
            raise ExportError(f"Invalid history for public service {sid}")
        normalized = []
        for sample in samples:
            stamp = _parse_time(sample.get("at"))
            state = sample.get("state")
            if state not in {"up", "down", "maintenance"}:
                raise ExportError(f"Invalid monitoring state for public service {sid}")
            normalized.append((stamp, state))
        normalized.sort(key=lambda point: point[0])
        # Buckets represent actual observations. Empty buckets remain unknown; a partial
        # heartbeat response must never be filled with inferred healthy data.
        buckets = []
        for hour in range(24):
            start = now.timestamp() - (24 - hour) * 3600
            end = start + 3600
            states = [state for stamp, state in normalized if start <= stamp.timestamp() < end]
            buckets.append("down" if "down" in states else "maintenance" if "maintenance" in states else "up" if "up" in states else "unknown")
        window = [state for stamp, state in normalized if (now.timestamp() - 86400) <= stamp.timestamp() <= now.timestamp()]
        tracking = item.get("tracking", "continuous")
        metric_type = "freshness" if tracking == "scheduled_freshness" else "uptime"
        latest = normalized[-1] if normalized else None
        last_success = next((stamp for stamp, state in reversed(normalized) if state == "up"), None)
        if tracking == "continuous":
            fraction = item.get("uptime_fraction")
            if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 <= fraction <= 1:
                raise ExportError(f"Missing or invalid authoritative 24-hour uptime for {sid}")
            uptime: float | None = round(float(fraction) * 100, 2)
            max_age = item.get("max_age_minutes", 30)
            if latest is None:
                status = "unknown"
            elif latest[1] == "down":
                status = "down"
            elif latest[1] == "maintenance":
                status = "degraded"
            elif (now - latest[0]).total_seconds() > max_age * 60:
                status = "degraded"
            else:
                status = "up"
        elif tracking == "scheduled_freshness":
            uptime = None
            threshold = item.get("freshness_hours")
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold <= 0:
                raise ExportError(f"Missing or invalid freshness threshold for {sid}")
            if latest is None:
                status = "unknown"
            elif latest[1] == "down":
                status = "down"
            elif latest[1] == "maintenance":
                status = "degraded"
            elif last_success is None or (now - last_success).total_seconds() > threshold * 3600:
                status = "degraded"
            else:
                status = "up"
        else:
            raise ExportError(f"Invalid tracking type for public service {sid}")
        # Only allow-listed, authored public fields are emitted. Raw monitor names,
        # URLs, IPs, tokens, messages and arbitrary source properties are discarded.
        services.append({"id": sid, "name": name, "group": group, "description": description,
                         "status": status, "metric_type": metric_type, "uptime_24h": uptime,
                         "history_24h": buckets, "samples": len(window),
                         "last_success_at": last_success.isoformat().replace("+00:00", "Z") if last_success else None})
        public_url = item.get("public_url")
        if public_url is not None:
            services[-1]["url"] = _safe_public_url(public_url)
    payload = {"schema_version": 1, "generated_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
               "services": services}
    validate_public_payload(payload)
    return payload


def validate_public_payload(payload: dict[str, Any]) -> None:
    if set(payload) != {"schema_version", "generated_at", "services"} or payload["schema_version"] != 1:
        raise ExportError("Public schema validation failed")
    _parse_time(payload["generated_at"])
    if not payload["services"]:
        raise ExportError("Refusing to publish an empty service list")
    for service in payload["services"]:
        required = {"id", "name", "group", "description", "status", "metric_type", "uptime_24h", "history_24h", "samples", "last_success_at"}
        if frozenset(service) not in {frozenset(required), frozenset(required | {"url"})}:
            raise ExportError("Public service contains unapproved fields")
        if service["status"] not in {"up", "down", "degraded", "unknown"} or len(service["history_24h"]) != 24:
            raise ExportError("Public service failed status/history validation")
        metric = service["metric_type"]
        uptime = service["uptime_24h"]
        if metric == "uptime" and (uptime is None or not 0 <= uptime <= 100):
            raise ExportError("Public service failed authoritative uptime validation")
        if metric == "freshness" and uptime is not None:
            raise ExportError("Scheduled freshness service cannot publish an uptime percentage")
        if metric not in {"uptime", "freshness"} or any(p not in {"up", "down", "maintenance", "unknown"} for p in service["history_24h"]):
            raise ExportError("Public service failed uptime validation")
        if service["last_success_at"] is not None:
            _parse_time(service["last_success_at"])
        if "url" in service:
            _safe_public_url(service["url"])


def publish_payload(payload: dict[str, Any]) -> None:
    validate_public_payload(payload)
    required = ["R2_ENDPOINT_URL", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise ExportError("Missing R2 configuration: " + ", ".join(missing))
    try:
        import boto3
        client = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
                              aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                              aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
        body = json.dumps(payload, separators=(",", ":")).encode()
        client.put_object(Bucket=os.environ["R2_BUCKET"], Key=os.getenv("R2_OBJECT_KEY", "status.json"),
                          Body=body, ContentType="application/json", CacheControl="no-store, max-age=0")
    except Exception as exc:
        # Do not log request objects, environment values or boto credential details.
        LOGGER.error("R2 upload failed (%s)", type(exc).__name__)
        raise ExportError("R2 upload failed") from None


def _safe_string(value: Any, label: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value.strip() and not allow_empty) or len(value) > maximum:
        raise ExportError(f"Invalid {label}")
    # Avoid publishing hostnames, URLs, address literals or path-like text in prose.
    if re.search(r"(?:https?://|\b\d{1,3}(?:\.\d{1,3}){3}\b|\b[a-z0-9-]+\.(?:local|internal|lan)\b|(?:^|\s)/[\w.-]+)", value, re.I):
        raise ExportError(f"{label} contains private addressing or a URL")
    return value.strip()


def _safe_public_url(value: Any) -> str:
    from urllib.parse import urlsplit

    if not isinstance(value, str) or len(value) > 300:
        raise ExportError("Invalid approved public URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ExportError("Invalid approved public URL") from None
    if (parsed.scheme != "https" or not hostname or parsed.username or parsed.password or
            port not in (None, 443) or not hostname.isascii()):
        raise ExportError("Invalid approved public URL")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        raise ExportError("Invalid approved public URL")
    if ip is None and ("." not in hostname or hostname.endswith((".local", ".internal", ".lan", ".home.arpa", ".localhost"))):
        raise ExportError("Invalid approved public URL")
    return value


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ExportError("Timestamp is missing or invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExportError("Timestamp is missing or invalid") from exc
    if result.tzinfo is None:
        raise ExportError("Timestamp must include timezone")
    return result.astimezone(timezone.utc)
