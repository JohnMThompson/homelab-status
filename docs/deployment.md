# Deployment guide

## Data contract and privacy boundary

The public document is `status.json`, schema version 1. It contains only an ISO-8601 UTC `generated_at` and services with an authored public ID, display name, fixed category, short public description, status, 24-hour uptime percentage, 24 hourly history buckets, and sample count. Raw Kuma monitor names, URLs, IPs, heartbeat messages, latency, IDs, credentials, and other source fields are discarded. Configure the explicit mapping in `config/public-services.json`; use non-sensitive display labels and descriptions. The IDs there are Kuma internal IDs and stay in the private exporter configuration.

The exporter reads the published status page at `/api/status-page/homelab-status` and heartbeat data at `/api/status-page/heartbeat/homelab-status`. Kuma returns at most 100 recent heartbeats, so this response is not treated as a 24-hour log. A private local state file merges heartbeat observations across exporter runs, deduplicated by monitor ID and timestamp and retained for 25 hours. The public hourly bars show observed states only; an hour without an observation is shown as unknown. The continuous services' displayed 24-hour percentages come directly from Kuma's `uptimeList[{monitor_id}_24]` fraction.

`config/public-services.json` is the explicit monitor allowlist and maps Kuma's private IDs to reviewed public labels. It also configures monitor type. Monitors 1, 2, 3, 6, 7, 8, and 9 are continuous; monitor 11 (nightly backup) and 12 (weekly backup maintenance) use scheduled freshness. Scheduled freshness services publish their last successful observation and no uptime percentage. Their freshness cutoffs are 36 hours and 216 hours respectively. Confirm these intervals reflect the actual schedule before production use. Unexpected Kuma monitor IDs are ignored; the exporter fails if any allowlisted monitor is absent from the published page.

Do not expose Kuma to the internet to make the exporter work: use its existing private address from Chickadee. The public status page response may include internal monitor metadata; the exporter only forwards explicitly mapped names, authored descriptions, sanitized state summaries, timestamps, and Kuma's authoritative uptime fraction. It never copies the raw API payload.

History bars mark any hourly bucket that had an observed outage. They do not infer unobserved hours as healthy. Historical annotations in `data/annotations/events.json` are manually authored and do not modify measured percentages.

If a snapshot is older than 45 minutes, the site displays **Status unavailable — monitoring data stale** and marks current service states unknown. Existing history and annotations remain visible. If fetching fails, current status is unavailable; the static fixture is only a build-time seed.

## Local development and checks

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r exporter/requirements.txt
python -m exporter.status_exporter --fixture
python -m unittest discover -s exporter/tests -v
cd site && npm ci && npm run build
```

The fixture command reads only `data/fixtures/kuma-history.json` and writes `site/public/data/status.json`; it never contacts Kuma or R2. Do not commit a generated production snapshot if it contains anything beyond the reviewed schema.

### Live dry run from Loon

Set `UPTIME_KUMA_URL` to the existing private Kuma URL that Loon can already reach through the homelab network, then run this from the repository root:

```sh
UPTIME_KUMA_URL="$UPTIME_KUMA_URL" UPTIME_KUMA_STATUS_SLUG=homelab-status \
  PUBLIC_SERVICE_MAP="$PWD/config/public-services.json" \
  python3 -m exporter.status_exporter --dry-run \
  --state-file /tmp/homelab-status-loon-history.json \
  --output /tmp/homelab-status-live-dry-run.json
```

This fetches live Kuma data and merges prior heartbeats, but writes only the sanitized JSON and local history state. It does not call the R2 publisher and does not need R2 credentials. Reuse the same state-file path to accumulate observations between dry runs. `--fixture` and `--dry-run` are mutually exclusive.

## GitHub Pages and domain

1. In repository Settings → Pages, select **GitHub Actions** as the build source.
2. Push changes to `main`; `.github/workflows/deploy.yml` runs only when `site/**`, `data/annotations/**`, or the workflow changes. Monitoring snapshots in R2 do not trigger a build.
3. Set the custom domain to `status.johnthompson.io` in Pages and enable HTTPS when available.
4. Keep DNS management at Squarespace. Add the GitHub Pages CNAME/verification records there according to the current Pages instructions; no home-network port forwarding is needed.

## Chickadee exporter setup

Install Python 3.11+, create a dedicated unprivileged account, and copy the repository to `/opt/homelab-status`. On Chickadee:

```sh
python3 -m venv /opt/homelab-status/.venv
/opt/homelab-status/.venv/bin/pip install -r /opt/homelab-status/exporter/requirements.txt
sudo install -d -m 700 /etc/homelab-status
sudo install -m 600 exporter/systemd/homelab-status-exporter.service /etc/systemd/system/
sudo install -m 644 exporter/systemd/homelab-status-exporter.timer /etc/systemd/system/
```

Create `/etc/homelab-status/exporter.env` owned by root and readable only by the exporter account. Set `UPTIME_KUMA_URL`, `UPTIME_KUMA_STATUS_SLUG=homelab-status`, `PUBLIC_SERVICE_MAP=/etc/homelab-status/public-services.json`, `STATUS_HISTORY_STATE=/var/lib/homelab-status/heartbeat-state.json`, and the R2 settings. Copy the reviewed `config/public-services.json` to the configured path. Use a bucket-scoped R2 token with write access to the one object. Keep credentials out of the repository, shell history, service logs, and command arguments. Configure a secure route from Chickadee to its private Kuma instance. No inbound firewall/router changes are needed; outbound HTTPS to Kuma and R2 is sufficient. The systemd unit creates the private state directory using `StateDirectory`.

The exporter service template uses `status-exporter` as its account and `/opt/homelab-status` as its checkout. Create that account and adjust ownership before enabling. Confirm the source endpoint and mapping with a controlled first run; then:

```sh
sudo systemctl daemon-reload
sudo systemctl start homelab-status-exporter.service
sudo systemctl enable --now homelab-status-exporter.timer
systemctl list-timers homelab-status-exporter.timer
journalctl -u homelab-status-exporter.service
```

The service returns non-zero on missing history, source errors, schema errors, or upload failures, leaving the last good R2 object unchanged. Verify the JSON body and freshness at the public R2 URL after the first successful upload.

## Rollback

For a site regression, revert the last site/annotation commit and push; Pages deploys the previous code. For an exporter regression, stop the timer (`sudo systemctl disable --now homelab-status-exporter.timer`), restore the previous exporter checkout/configuration, then run one controlled export and re-enable the timer. R2's last good object remains in place when an export fails. To restore a prior object, use the R2 object version if bucket versioning is enabled, or upload a reviewed backup. Do not replace it with fixture data in production.
