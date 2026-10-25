# Homelab Status

A public-facing status and reliability portfolio for a privately monitored homelab. The Astro site is deployed to GitHub Pages; a small Python exporter publishes a sanitized snapshot to Cloudflare R2 every 15 minutes. Uptime Kuma and the exporter remain private on Chickadee.

## Local development

Requirements: Node.js 20+ and Python 3.11+. Start with fixture data; this does not connect to Chickadee or Cloudflare.

```sh
cd site
npm install
npm run dev
```

The dashboard reads `site/public/data/status.json`. To regenerate it from the committed fixture, run from the repository root:

```sh
python -m exporter.status_exporter --fixture
```

To run exporter tests: `python -m unittest discover -s exporter/tests -v`. For a production frontend build: `cd site && npm run build`.

From Loon, run a live dry run against Chickadee by setting `UPTIME_KUMA_URL` to the existing private Kuma URL that Loon can already reach, then running from the repository root:

```sh
UPTIME_KUMA_URL="$UPTIME_KUMA_URL" UPTIME_KUMA_STATUS_SLUG=homelab-status \
  PUBLIC_SERVICE_MAP="$PWD/config/public-services.json" \
  python3 -m exporter.status_exporter --dry-run \
  --state-file /tmp/homelab-status-loon-history.json \
  --output /tmp/homelab-status-live-dry-run.json
```

The dry run reads live Kuma data and writes only sanitized JSON and heartbeat state to those local `/tmp` files. It does not need R2 credentials or upload anything. Reuse the state-file path to accumulate history across runs.

## Architecture and deployment

See [deployment guide](docs/deployment.md) for schema, exporter setup, GitHub Pages, systemd timer, and rollback steps. Production secrets belong only in Chickadee's environment file. DNS remains managed by Squarespace; no inbound network changes are needed.

Historical maintenance and incident annotations are version controlled in `data/annotations/events.json`; they are informational and do not change measured uptime.
