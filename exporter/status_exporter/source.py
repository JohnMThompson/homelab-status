from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("status-exporter")
KUMA_NAIVE_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$")
KUMA_AWARE_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})$")


class SourceError(RuntimeError):
    pass


class KumaClient:
    """Fetch the two Uptime Kuma 2 published status-page endpoints."""
    def __init__(self, base_url: str, slug: str, api_key: str | None = None,
                 service_map: dict[str, dict[str, Any]] | None = None):
        self.base_url = base_url.rstrip("/")
        self.slug = slug
        self.api_key = api_key
        self.service_map = service_map if service_map is not None else load_public_mapping()

    @classmethod
    def from_env(cls) -> "KumaClient":
        base_url = os.getenv("UPTIME_KUMA_URL")
        slug = os.getenv("UPTIME_KUMA_STATUS_SLUG", "homelab-status")
        if not base_url:
            raise SourceError("Missing Kuma configuration: UPTIME_KUMA_URL")
        return cls(base_url, slug, os.getenv("UPTIME_KUMA_API_KEY"))

    def fetch(self) -> dict[str, Any]:
        try:
            import requests
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            status_response = requests.get(f"{self.base_url}/api/status-page/{self.slug}", headers=headers, timeout=15)
            status_response.raise_for_status()
            heartbeat_response = requests.get(f"{self.base_url}/api/status-page/heartbeat/{self.slug}", headers=headers, timeout=15)
            heartbeat_response.raise_for_status()
            status_page = status_response.json()
            heartbeat_data = heartbeat_response.json()
        except Exception as exc:
            raise SourceError(f"Kuma request failed ({type(exc).__name__})") from None

        groups = status_page.get("publicGroupList")
        heartbeat_list = heartbeat_data.get("heartbeatList")
        uptime_list = heartbeat_data.get("uptimeList")
        if not isinstance(groups, list) or not isinstance(heartbeat_list, dict) or not isinstance(uptime_list, dict):
            raise SourceError("Kuma returned an invalid status-page or heartbeat response")

        monitors: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group in groups:
            for monitor in group.get("monitorList", []):
                monitor_id = str(monitor.get("id", ""))
                authored = self.service_map.get(monitor_id)
                if not authored:
                    LOGGER.warning("Ignoring Kuma monitor that is not in the public allowlist")
                    continue
                if monitor_id in seen:
                    raise SourceError("Kuma returned a duplicate allowed monitor")
                seen.add(monitor_id)
                beats = heartbeat_list.get(monitor_id, [])
                if not isinstance(beats, list):
                    raise SourceError("Kuma returned malformed heartbeat history")
                samples = []
                for beat in beats:
                    if not isinstance(beat, dict):
                        raise SourceError("Kuma returned a malformed heartbeat record")
                    state = {0: "down", 1: "up", 3: "maintenance"}.get(beat.get("status"))
                    # Pending/unknown Kuma states are not evidence of either uptime or outage.
                    if state:
                        samples.append({"at": normalize_kuma_timestamp(beat.get("time")), "state": state})
                fraction = uptime_list.get(f"{monitor_id}_24")
                if authored["tracking"] == "continuous" and fraction is None:
                    raise SourceError("Kuma omitted authoritative 24-hour uptime for an allowed continuous monitor")
                monitors.append({"monitor_id": monitor_id, "public_id": authored["id"],
                                 "public_name": authored["name"], "public_description": authored["description"],
                                 "group": authored["group"], "tracking": authored["tracking"],
                                 "max_age_minutes": authored.get("max_age_minutes"),
                                 "freshness_hours": authored.get("freshness_hours"),
                                 "samples": samples, "uptime_fraction": fraction})

        if not monitors:
            raise SourceError("Kuma status page returned no allowlisted monitors")
        missing = set(self.service_map) - seen
        if missing:
            raise SourceError("Kuma status page is missing one or more configured public monitors")
        return {"monitors": monitors}


def normalize_kuma_timestamp(value: Any) -> str:
    """Convert a verified Kuma heartbeat timestamp into an aware UTC ISO timestamp.

    Kuma 2 creates heartbeat times from ``dayjs.utc()`` and serializes its SQL-style
    ``YYYY-MM-DD HH:mm:ss.SSS`` value without an offset. That specific naive shape is
    therefore UTC. Other naive or non-ISO shapes remain ambiguous and are rejected.
    """
    if not isinstance(value, str):
        raise SourceError("Kuma heartbeat timestamp is missing or invalid")
    naive = KUMA_NAIVE_TIMESTAMP.fullmatch(value)
    aware = KUMA_AWARE_TIMESTAMP.fullmatch(value)
    if not naive and not aware:
        raise SourceError("Kuma heartbeat timestamp has an unsupported or ambiguous format")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SourceError("Kuma heartbeat timestamp is invalid") from None
    if parsed.tzinfo is None:
        if not naive:
            raise SourceError("Kuma heartbeat timestamp is ambiguous")
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def load_public_mapping(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    source = Path(path or os.getenv("PUBLIC_SERVICE_MAP") or Path(__file__).resolve().parents[2] / "config" / "public-services.json")
    try:
        data = json.loads(source.read_text())
        services = data["services"]
        mapping = {str(item["monitor_id"]): item for item in services}
        if len(mapping) != len(services):
            raise ValueError("duplicate monitor IDs")
        required = {"monitor_id", "id", "name", "group", "description", "tracking"}
        allowed_groups = {"Infrastructure", "Applications", "Backup & Recovery"}
        for item in services:
            if not required <= item.keys() or item["group"] not in allowed_groups:
                raise ValueError("invalid public service mapping")
            if item["tracking"] == "continuous" and not isinstance(item.get("max_age_minutes"), int):
                raise ValueError("continuous mapping needs max_age_minutes")
            if item["tracking"] == "scheduled_freshness" and not isinstance(item.get("freshness_hours"), int):
                raise ValueError("scheduled mapping needs freshness_hours")
            if item["tracking"] not in {"continuous", "scheduled_freshness"}:
                raise ValueError("invalid tracking mode")
        return mapping
    except Exception:
        raise SourceError("Unable to load valid explicit public service mapping") from None
