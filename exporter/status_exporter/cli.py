from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .exporter import ExportError, build_public_payload, publish_payload
from .history import load_history_state, merge_heartbeat_history, save_history_state
from .source import KumaClient, SourceError

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "data" / "fixtures" / "kuma-history.json"
OUTPUT = ROOT / "site" / "public" / "data" / "status.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and optionally publish sanitized homelab status data")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--fixture", action="store_true", help="build output from local fixture; no network access or upload")
    modes.add_argument("--dry-run", action="store_true", help="fetch live Kuma data and build a sanitized payload without uploading; optionally write it with --output")
    parser.add_argument("--output", type=Path, help="local JSON output path for --fixture or --dry-run (dry-run prints JSON to stdout if omitted)")
    parser.add_argument("--state-file", type=Path, default=Path(os.getenv("STATUS_HISTORY_STATE", "/var/lib/homelab-status/heartbeat-state.json")), help="private persistent heartbeat-history state")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("status-exporter")
    try:
        if args.fixture:
            logger.info("Starting exporter in fixture mode")
            raw = json.loads(FIXTURE.read_text())
            fixture_now = datetime.now(timezone.utc)
            continuous_samples = [sample for item in raw["monitors"] if item.get("tracking", "continuous") == "continuous" for sample in item["samples"]]
            newest = max(datetime.fromisoformat(sample["at"].replace("Z", "+00:00")) for sample in continuous_samples)
            shift = fixture_now - newest
            for item in raw["monitors"]:
                for sample in item["samples"]:
                    stamp = datetime.fromisoformat(sample["at"].replace("Z", "+00:00")) + shift
                    sample["at"] = stamp.isoformat().replace("+00:00", "Z")
            payload = build_public_payload(raw, fixture_now)
            output = args.output or OUTPUT
            _write_payload(output, payload)
            logger.info("Wrote fixture status for %d services to %s", len(payload["services"]), output)
            return
        if args.output and not args.dry_run:
            parser.error("--output is only available with --fixture or --dry-run")
        mode = "dry-run" if args.dry_run else "production"
        logger.info("Starting exporter in %s mode", mode)
        now = datetime.now(timezone.utc)
        previous = load_history_state(args.state_file)
        client = KumaClient.from_env()
        raw = client.fetch()
        merged, updated_state = merge_heartbeat_history(raw, previous, now)
        payload = build_public_payload(merged, now)
        save_history_state(args.state_file, updated_state)
        if args.dry_run:
            if args.output:
                _write_payload(args.output, payload)
                logger.info("Dry run complete: built sanitized status for %d services; wrote local JSON to %s; no upload performed", len(payload["services"]), args.output)
            else:
                print(json.dumps(payload, indent=2))
                logger.info("Dry run complete: built sanitized status for %d services; wrote JSON to stdout; no upload performed", len(payload["services"]))
            return
        publish_payload(payload)
        logger.info("Published status for %d services using persistent heartbeat history", len(payload["services"]))
    except (ExportError, SourceError, OSError, ValueError) as exc:
        logger.error("Status export failed safely: %s", exc)
        raise SystemExit(1) from None


def _write_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


if __name__ == "__main__":
    main()
