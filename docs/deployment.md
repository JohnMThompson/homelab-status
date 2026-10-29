# Deployment guide

## Data contract and privacy boundary

The public document is `status.json`, schema version 1. It contains only an ISO-8601 UTC `generated_at` and the eight explicitly allowlisted services with a public ID, display name, fixed category, short description, status, 24-hour uptime percentage, 24 hourly history buckets, sample count, and last success time. Services may include an optional `url` when a reviewed `public_url` is configured in `config/public-services.json`. This additive schema v1 field is ignored by older frontends. Raw Kuma monitor names, URLs, IPs, heartbeat messages, latency, monitor IDs, credentials, and other source fields are discarded; public application URLs are authored in configuration, not copied from Kuma. Kuma monitor IDs are maintained separately in a private, server-local file and are never part of the public repository or JSON.

The exporter reads the published status page at `/api/status-page/homelab-status` and heartbeat data at `/api/status-page/heartbeat/homelab-status`. Kuma returns at most 100 recent heartbeats per monitor, so this response is not treated as a 24-hour log. A private local state file merges heartbeat observations across exporter runs, deduplicated by monitor ID and timestamp and retained for 25 hours. The public hourly bars show observed states only; an hour without an observation is shown as unknown. The continuous services' displayed 24-hour percentages come directly from Kuma's `uptimeList[{monitor_id}_24]` fraction.

`config/public-services.json` is the public service allowlist and configures service metadata and tracking mode without storing Kuma monitor IDs. Infrastructure and application checks use continuous tracking; the nightly backup uses scheduled freshness with a 26-hour cutoff. Scheduled freshness is based on the latest recognized heartbeat: missing history is **Unknown**, a latest failed heartbeat is **Down**, maintenance is **Degraded**, and a latest successful heartbeat is **Operational** only while its age is within the configured threshold. An overdue success is **Degraded**; an older success cannot mask a newer failure. The nightly threshold remains 26 hours.

Weekly Backup Maintenance is intentionally omitted from the public allowlist. Investigation found that the exporter retained only 25 hours of heartbeats, while Kuma's published endpoint returns at most 100 recent records. This did not provide reliable evidence of a weekly success and later failure across exporter restarts, so the public dashboard omits the monitor instead of guessing. Its private Kuma monitor, push checks, and alerts remain unchanged. The reduced allowlist produces exactly eight public services. Unexpected Kuma monitor IDs are ignored; the exporter fails if any configured public monitor is absent.

The four application `public_url` values in the mapping are the only application links the exporter may publish. Mapping validation rejects non-HTTPS links, credentials, local or internal hostnames, and non-public IP addresses. The frontend creates anchors only when the service ID and URL match its four approved applications. Links open in a new tab with `noopener noreferrer`.

Do not expose Kuma to the internet to make the exporter work: the exporter uses its existing private network address. The public status page response may include internal monitor metadata; the exporter only forwards explicitly mapped names, authored descriptions, approved public URLs, sanitized state summaries, timestamps, and Kuma's authoritative uptime fraction. It never copies the raw API payload.

History bars mark any hourly bucket that had an observed outage. They do not infer unobserved hours as healthy. Historical annotations in `data/annotations/events.json` are manually authored and do not modify measured percentages.

If a snapshot is older than 45 minutes, malformed, or unavailable, the site displays **Status unavailable** and marks current service states unknown. For a fresh valid snapshot, down or degraded services produce **Some services need attention**; if no problems are confirmed but at least one service is unknown, the summary says **Status partially unknown**. Only an all-up snapshot produces **All systems operational**. Existing history and annotations remain visible. If fetching fails, current status is unavailable; the static fixture is only a build-time seed.

## Local development and checks

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r exporter/requirements.txt
python -m exporter.status_exporter --fixture
python -m unittest discover -s exporter/tests -v
cd site && npm ci && npm run build
```

The fixture command reads only `data/fixtures/kuma-history.json` and writes `site/public/data/status.json`; it never contacts Kuma or R2. Astro's production build does not require that generated runtime file. Do not commit a generated production snapshot if it contains anything beyond the reviewed schema.

### Live dry run from a trusted workstation

Set `UPTIME_KUMA_URL` to the private Kuma URL reachable from the monitoring network and `PRIVATE_MONITOR_MAP` to a private mapping file, then run this from the repository root:

```sh
UPTIME_KUMA_URL="$UPTIME_KUMA_URL" UPTIME_KUMA_STATUS_SLUG=homelab-status \
  PUBLIC_SERVICE_MAP="$PWD/config/public-services.json" \
  PRIVATE_MONITOR_MAP="/etc/homelab-status/private-monitor-map.json" \
  python3 -m exporter.status_exporter --dry-run \
  --state-file /tmp/homelab-status-dry-run-history.json \
  --output /tmp/homelab-status-live-dry-run.json
```

This fetches live Kuma data and merges prior heartbeats, but writes only the sanitized JSON and local history state. It does not call the R2 publisher and does not need R2 credentials. Reuse the same state-file path to accumulate observations between dry runs. `--fixture` and `--dry-run` are mutually exclusive.

## GitHub Pages and domain

1. In repository Settings → Pages, select **GitHub Actions** as the build source.
2. Push changes to `main`; `.github/workflows/deploy.yml` runs only when `site/**`, `data/annotations/**`, or the workflow changes. Monitoring snapshots in R2 do not trigger a build.
3. Set the custom domain to `status.johnthompson.io` in Pages and enable HTTPS when available.
4. Keep DNS management at Squarespace. Add the GitHub Pages CNAME/verification records there according to the current Pages instructions; no home-network port forwarding is needed.

## Monitoring server exporter setup

Install Python 3.11+, create a dedicated unprivileged account, and copy the repository to `/opt/homelab-status` on the monitoring server:

```sh
python3 -m venv /opt/homelab-status/.venv
/opt/homelab-status/.venv/bin/pip install -r /opt/homelab-status/exporter/requirements.txt
sudo install -d -m 700 /etc/homelab-status
sudo install -m 600 exporter/systemd/homelab-status-exporter.service /etc/systemd/system/
sudo install -m 644 exporter/systemd/homelab-status-exporter.timer /etc/systemd/system/
```

Create `/etc/homelab-status/exporter.env` with owner `root:root` and mode `0600`. The system service manager reads this file through the unit's `EnvironmentFile=` directive before starting the service, then supplies those variables to the process running as `status-exporter`; the exporter account does not need direct read access to this file. Set `UPTIME_KUMA_URL`, `UPTIME_KUMA_STATUS_SLUG=homelab-status`, `PUBLIC_SERVICE_MAP=/etc/homelab-status/public-services.json`, `PRIVATE_MONITOR_MAP=/etc/homelab-status/private-monitor-map.json`, `STATUS_HISTORY_STATE=/var/lib/homelab-status/heartbeat-state.json`, and the R2 settings.

Copy the reviewed `config/public-services.json` to the public mapping path. Create the private monitor map from `config/private-monitor-map.example.json`, replacing each placeholder with its real Kuma monitor ID on the server only. Set its owner to `status-exporter:status-exporter` and mode `0600` so the exporter process can read it while other users cannot. Keep this file, credentials, and all actual IDs out of the repository, shell history, service logs, and command arguments. Use a bucket-scoped R2 token with write access to the one object. Configure the existing private network route to Kuma. No inbound firewall/router changes are needed; outbound HTTPS to Kuma and R2 is sufficient. The systemd unit creates the private state directory using `StateDirectory`.

Set and verify the two file permissions as follows:

```sh
sudo chown root:root /etc/homelab-status/exporter.env
sudo chmod 600 /etc/homelab-status/exporter.env
sudo chown status-exporter:status-exporter /etc/homelab-status/private-monitor-map.json
sudo chmod 600 /etc/homelab-status/private-monitor-map.json
sudo -u status-exporter test -r /etc/homelab-status/private-monitor-map.json
stat -c '%U:%G %a %n' /etc/homelab-status/exporter.env /etc/homelab-status/private-monitor-map.json
```

The exporter service template uses `status-exporter` as its account and `/opt/homelab-status` as its checkout. Create that account and adjust ownership before enabling. Confirm the source endpoint and mapping with a controlled first run; then:

```sh
sudo systemctl daemon-reload
sudo systemctl start homelab-status-exporter.service
sudo systemctl enable --now homelab-status-exporter.timer
systemctl list-timers homelab-status-exporter.timer
journalctl -u homelab-status-exporter.service
```

The service returns non-zero on missing history, source errors, schema errors, or upload failures, leaving the last good R2 object unchanged. Verify the JSON body and freshness at the public R2 URL after the first successful upload.

For v1.1, publish the frontend through GitHub Actions first so it can render the optional `url` field. Then manually update the monitoring server's checkout to the release commit and copy the reviewed public-service mapping to `/etc/homelab-status/public-services.json`. Preserve the real ID-to-service assignments in `/etc/homelab-status/private-monitor-map.json`; they are removed from the repository config. Run one controlled exporter invocation before leaving the existing 15-minute timer enabled. A pull request merge does not update the monitoring server. Keep the dedicated `status-exporter` service account and existing systemd service/timer; no new process, container, inbound port, or production Kuma change is required.

## Rollback

For a site regression, revert the last site/annotation commit and push; Pages deploys the previous code. For an exporter regression, stop the timer (`sudo systemctl disable --now homelab-status-exporter.timer`), restore the previous exporter checkout/configuration, then run one controlled export and re-enable the timer. R2's last good object remains in place when an export fails. To restore a prior object, use the R2 object version if bucket versioning is enabled, or upload a reviewed backup. Do not replace it with fixture data in production.
