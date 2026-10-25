import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from status_exporter.exporter import ExportError, build_public_payload
from status_exporter.history import load_history_state, merge_heartbeat_history, save_history_state
from status_exporter.source import KumaClient, SourceError, load_public_mapping, normalize_kuma_timestamp
from unittest.mock import Mock, patch
from status_exporter.cli import main as exporter_main


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((Path(__file__).resolve().parents[2] / "data/fixtures/kuma-history.json").read_text())
        self.now = datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc)

    def test_fixture_is_allowlisted_and_keeps_short_outage(self):
        payload = build_public_payload(self.raw, self.now)
        self.assertEqual(payload["schema_version"], 1)
        service = next(s for s in payload["services"] if s["id"] == "home-assistant")
        self.assertIn("down", service["history_24h"])
        self.assertEqual(service["status"], "up")
        self.assertEqual(service["uptime_24h"], 92.07)
        self.assertEqual(len(service["history_24h"]), 24)
        self.assertEqual(set(service), {"id", "name", "group", "description", "status", "metric_type", "uptime_24h", "history_24h", "samples", "last_success_at"})

    def test_rejects_private_addressing_in_public_text(self):
        self.raw["monitors"][0]["public_description"] = "Internal 10.0.0.4 endpoint"
        with self.assertRaises(ExportError):
            build_public_payload(self.raw, self.now)

    def test_rejects_empty_data(self):
        with self.assertRaises(ExportError):
            build_public_payload({"monitors": []}, self.now)

    def test_partial_kuma_history_merges_prior_outage_and_deduplicates_timestamp(self):
        current = {"monitors": [{"monitor_id": "1", "public_id": "gateway", "public_name": "Gateway",
                                  "public_description": "Routing service", "group": "Infrastructure",
                                  "tracking": "continuous", "max_age_minutes": 30, "uptime_fraction": 0.92,
                                  "samples": [{"at": "2026-10-21T23:00:00Z", "state": "up"}]}]}
        prior = {"monitors": {"1": [
            {"at": "2026-10-21T10:00:00Z", "state": "down"},
            {"at": "2026-10-21T23:00:00Z", "state": "down"},
            {"at": "2026-10-20T20:00:00Z", "state": "down"}
        ]}}
        merged, state = merge_heartbeat_history(current, prior, self.now)
        samples = merged["monitors"][0]["samples"]
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[-1]["state"], "up")
        self.assertEqual(state["monitors"]["1"], samples)
        payload = build_public_payload(merged, self.now)
        gateway = payload["services"][0]
        self.assertIn("down", gateway["history_24h"])
        self.assertIn("unknown", gateway["history_24h"])
        self.assertEqual(gateway["uptime_24h"], 92.0)

    def test_history_state_is_persisted_and_reloaded(self):
        _, state = merge_heartbeat_history({"monitors": [{"monitor_id": "1", "samples": [
            {"at": "2026-10-21T23:00:00Z", "state": "up"}
        ]}]}, None, self.now)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat-state.json"
            save_history_state(path, state)
            self.assertEqual(load_history_state(path), state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_scheduled_monitor_uses_last_success_not_uptime(self):
        raw = {"monitors": [{"monitor_id": "11", "public_id": "nightly-backup", "public_name": "Nightly backup",
                             "public_description": "Latest scheduled backup check", "group": "Backup & Recovery",
                             "tracking": "scheduled_freshness", "freshness_hours": 36, "uptime_fraction": None,
                             "samples": [{"at": "2026-10-21T22:00:00Z", "state": "up"}]}]}
        service = build_public_payload(raw, self.now)["services"][0]
        self.assertEqual(service["metric_type"], "freshness")
        self.assertIsNone(service["uptime_24h"])
        self.assertEqual(service["last_success_at"], "2026-10-21T22:00:00Z")

    def test_unknown_history_does_not_look_healthy(self):
        raw = {"monitors": [{"monitor_id": "1", "public_id": "gateway", "public_name": "Gateway",
                             "public_description": "Routing service", "group": "Infrastructure",
                             "tracking": "continuous", "uptime_fraction": 0.95, "samples": []}]}
        service = build_public_payload(raw, self.now)["services"][0]
        self.assertEqual(service["status"], "unknown")
        self.assertEqual(set(service["history_24h"]), {"unknown"})

    def test_verified_allowlist_and_kuma_endpoint_adapter(self):
        mapping = load_public_mapping(Path(__file__).resolve().parents[2] / "config/public-services.json")
        self.assertEqual(set(mapping), {"1", "2", "3", "6", "7", "8", "9", "11", "12"})
        status = {"publicGroupList": [{"monitorList": [
            {"id": 1, "name": "PRIVATE HOSTNAME"}, {"id": 90, "name": "Unapproved internal monitor"}
        ]}]}
        heartbeat = {"heartbeatList": {"1": [{"status": 1, "time": "2026-10-21 23:29:00.000", "msg": "private"}]},
                     "uptimeList": {"1_24": 0.9207419898819561}}
        responses = [Mock(json=lambda: status), Mock(json=lambda: heartbeat)]
        for response in responses:
            response.raise_for_status.return_value = None
        with patch("requests.get", side_effect=responses) as get:
            result = KumaClient("http://private-kuma", "homelab-status", service_map={"1": mapping["1"]}).fetch()
        self.assertEqual(get.call_args_list[0].args[0], "http://private-kuma/api/status-page/homelab-status")
        self.assertEqual(get.call_args_list[1].args[0], "http://private-kuma/api/status-page/heartbeat/homelab-status")
        self.assertEqual(len(result["monitors"]), 1)
        self.assertEqual(result["monitors"][0]["public_name"], "Pileated host")
        self.assertEqual(result["monitors"][0]["samples"][0]["at"], "2026-10-21T23:29:00Z")
        self.assertNotIn("PRIVATE HOSTNAME", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))

    def test_kuma_naive_timestamp_format_is_interpreted_as_utc(self):
        self.assertEqual(normalize_kuma_timestamp("2026-10-22 02:53:04.158"), "2026-10-22T02:53:04.158000Z")

    def test_kuma_utc_z_and_explicit_offsets_normalize_to_utc(self):
        self.assertEqual(normalize_kuma_timestamp("2026-10-22T02:53:04.158Z"), "2026-10-22T02:53:04.158000Z")
        self.assertEqual(normalize_kuma_timestamp("2026-10-22 04:53:04.158+02:00"), "2026-10-22T02:53:04.158000Z")
        self.assertEqual(normalize_kuma_timestamp("2026-10-21T21:53:04.158-05:00"), "2026-10-22T02:53:04.158000Z")

    def test_ambiguous_or_malformed_kuma_timestamp_fails_safely(self):
        for value in ("2026-10-22T02:53:04.158", "2026/10/22 02:53:04", "not-a-timestamp", None):
            with self.subTest(value=value), self.assertRaises(SourceError):
                normalize_kuma_timestamp(value)

    def test_internal_history_still_rejects_naive_timestamp(self):
        with self.assertRaises(ExportError):
            merge_heartbeat_history({"monitors": [{"monitor_id": "1", "samples": [
                {"at": "2026-10-22 02:53:04.158", "state": "up"}
            ]}]}, None, self.now)


class ExporterCliTests(unittest.TestCase):
    def test_dry_run_fetches_merges_writes_local_payload_and_never_uploads(self):
        now = datetime.now(timezone.utc)
        raw = {"monitors": [{"monitor_id": "1", "public_id": "gateway", "public_name": "Gateway",
                             "public_description": "Routing service", "group": "Infrastructure",
                             "tracking": "continuous", "max_age_minutes": 30, "uptime_fraction": 0.98,
                             "samples": [{"at": (now - timedelta(minutes=1)).isoformat(), "state": "up"}]}]}
        client = Mock()
        client.fetch.return_value = raw
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dry-run" / "status.json"
            state_file = Path(directory) / "state" / "history.json"
            argv = ["status-exporter", "--dry-run", "--output", str(output), "--state-file", str(state_file)]
            with patch.dict(os.environ, {}, clear=True), patch("sys.argv", argv), \
                 patch("status_exporter.cli.KumaClient.from_env", return_value=client), \
                 patch("status_exporter.cli.publish_payload") as publish, \
                 patch("status_exporter.cli.datetime") as clock:
                clock.now.return_value = now
                clock.timezone = timezone
                exporter_main()
            client.fetch.assert_called_once_with()
            publish.assert_not_called()
            self.assertTrue(state_file.exists())
            payload = json.loads(output.read_text())
            self.assertEqual(payload["services"][0]["id"], "gateway")
            self.assertEqual(payload["services"][0]["uptime_24h"], 98.0)

    def test_dry_run_without_output_prints_payload_and_does_not_upload(self):
        now = datetime.now(timezone.utc)
        raw = {"monitors": [{"monitor_id": "1", "public_id": "gateway", "public_name": "Gateway",
                             "public_description": "Routing service", "group": "Infrastructure",
                             "tracking": "continuous", "max_age_minutes": 30, "uptime_fraction": 1.0,
                             "samples": [{"at": now.isoformat(), "state": "up"}]}]}
        client = Mock(); client.fetch.return_value = raw
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "history.json"
            output = StringIO()
            argv = ["status-exporter", "--dry-run", "--state-file", str(state_file)]
            with patch.dict(os.environ, {}, clear=True), patch("sys.argv", argv), \
                 patch("status_exporter.cli.KumaClient.from_env", return_value=client), \
                 patch("status_exporter.cli.publish_payload") as publish, \
                 redirect_stdout(output):
                exporter_main()
            publish.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["services"][0]["status"], "up")

    def test_fixture_and_dry_run_are_mutually_exclusive(self):
        with patch("sys.argv", ["status-exporter", "--fixture", "--dry-run"]):
            with self.assertRaises(SystemExit) as error:
                exporter_main()
        self.assertEqual(error.exception.code, 2)

    def test_default_mode_still_publishes(self):
        now = datetime.now(timezone.utc)
        raw = {"monitors": [{"monitor_id": "1", "public_id": "gateway", "public_name": "Gateway",
                             "public_description": "Routing service", "group": "Infrastructure",
                             "tracking": "continuous", "max_age_minutes": 30, "uptime_fraction": 1.0,
                             "samples": [{"at": now.isoformat(), "state": "up"}]}]}
        client = Mock(); client.fetch.return_value = raw
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "history.json"
            with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["status-exporter", "--state-file", str(state_file)]), \
                 patch("status_exporter.cli.KumaClient.from_env", return_value=client), \
                 patch("status_exporter.cli.publish_payload") as publish:
                exporter_main()
            publish.assert_called_once()


if __name__ == "__main__":
    unittest.main()
