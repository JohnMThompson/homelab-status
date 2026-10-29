export const SERVICE_GROUPS = ['Infrastructure', 'Applications', 'Backup & Recovery'];
export const STALE_AFTER_MS = 45 * 60 * 1000;

const APPLICATION_URLS = {
  analytics: 'https://analytics.johnthompson.io',
  freight: 'https://freight.johnthompson.io',
  'swim-writer': 'https://swim-writer.johnthompson.io',
  diamonds: 'https://diamonds.johnthompson.io',
};

const STATUS_LABELS = {
  up: 'Operational',
  degraded: 'Degraded',
  down: 'Down',
  unknown: 'Unknown',
};
const HISTORY_STATES = new Set(['up', 'down', 'maintenance', 'unknown']);

export function getSnapshotState(data, now = Date.now()) {
  const hasServices = Boolean(data && data.schema_version === 1 && Array.isArray(data.services));
  const generatedAt = hasServices ? Date.parse(data.generated_at) : Number.NaN;
  const validServices = hasServices && data.services.every((service) => service &&
    typeof service.id === 'string' && typeof service.name === 'string' &&
    ['up', 'down', 'degraded', 'unknown'].includes(service.status));
  const valid = Boolean(validServices && Number.isFinite(generatedAt) && generatedAt <= now + 5 * 60 * 1000);
  const stale = !valid || now - generatedAt > STALE_AFTER_MS;
  return {
    valid,
    stale,
    empty: valid && data.services.length === 0,
    current: valid && !stale && data.services.length > 0,
  };
}

export function getOverallStatus(services, snapshot = { current: true }) {
  if (!snapshot.current || snapshot.empty) return 'unavailable';
  if (services.some((service) => service.status === 'down' || service.status === 'degraded')) return 'attention';
  if (services.some((service) => service.status !== 'up')) return 'unknown';
  return 'up';
}

export function renderServiceGroups(container, services, { current = true, formatTime = defaultFormatTime } = {}) {
  const document = container.ownerDocument;
  const groups = new Map(SERVICE_GROUPS.map((name) => [name, []]));
  if (Array.isArray(services)) {
    for (const service of services) {
      if (service && groups.has(service.group)) groups.get(service.group).push(service);
    }
  }

  const groupNodes = SERVICE_GROUPS.map((name) => {
    const groupServices = groups.get(name);
    const section = document.createElement('section');
    section.className = 'service-group';
    section.setAttribute('aria-label', name);

    const heading = document.createElement('div');
    heading.className = 'group-heading';
    const title = document.createElement('h3');
    title.textContent = name;
    const count = document.createElement('span');
    count.textContent = `${groupServices.length} ${groupServices.length === 1 ? 'service' : 'services'}`;
    heading.append(title, count);
    section.append(heading);

    const list = document.createElement('div');
    list.className = 'service-list';
    if (groupServices.length === 0) {
      const empty = document.createElement('p');
      empty.className = 'empty-services';
      empty.textContent = 'No services reported in this group.';
      list.append(empty);
    } else {
      for (const service of groupServices) list.append(createServiceRow(document, service, current, formatTime));
    }
    section.append(list);
    return section;
  });

  container.replaceChildren(...groupNodes);
}

function createServiceRow(document, service, current, formatTime) {
  const row = document.createElement('article');
  row.className = 'service-row';
  row.dataset.serviceId = safeText(service.id);

  const state = current && STATUS_LABELS[service.status] ? service.status : 'unknown';
  const dot = document.createElement('span');
  dot.className = 'service-dot';
  dot.dataset.state = state;
  dot.setAttribute('aria-hidden', 'true');

  const name = document.createElement('div');
  name.className = 'service-name';
  const serviceName = document.createElement('h4');
  const displayName = safeText(service.name) || 'Unnamed service';
  const approvedUrl = APPLICATION_URLS[service.id];
  const applicationUrl = approvedUrl && service.url === approvedUrl ? approvedUrl : null;
  if (applicationUrl) {
    const link = document.createElement('a');
    link.href = applicationUrl;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = displayName;
    link.setAttribute('aria-label', `${displayName} (opens in a new tab)`);
    serviceName.append(link);
  } else {
    serviceName.textContent = displayName;
  }
  const description = document.createElement('p');
  description.textContent = safeText(service.description);
  name.append(serviceName, description);

  const metric = document.createElement('div');
  metric.className = 'uptime-wrap';
  const metricTop = document.createElement('div');
  metricTop.className = 'uptime-top';
  const metricLabel = document.createElement('span');
  const metricValue = document.createElement('strong');
  if (service.metric_type === 'freshness' || service.uptime_24h == null) {
    metricLabel.textContent = 'Last successful run';
    metricValue.textContent = service.last_success_at ? formatTime(service.last_success_at) : 'No successful run';
  } else if (Number.isFinite(service.uptime_24h)) {
    metricLabel.textContent = '24 hour uptime';
    metricValue.textContent = `${service.uptime_24h.toFixed(2)}%`;
  } else {
    metricLabel.textContent = '24 hour uptime';
    metricValue.textContent = 'Unavailable';
  }
  metricTop.append(metricLabel, metricValue);

  const history = document.createElement('div');
  history.className = 'uptime-bar';
  history.setAttribute('role', 'img');
  history.setAttribute('aria-label', `${safeText(service.name) || 'Service'}: observed history over the last 24 hours`);
  const historyValues = Array.isArray(service.history_24h) ? service.history_24h : [];
  const bars = Array.from({ length: 24 }, (_, index) => {
    const bar = document.createElement('i');
    const observedState = HISTORY_STATES.has(historyValues[index]) ? historyValues[index] : 'unknown';
    bar.className = observedState;
    return bar;
  });
  history.replaceChildren(...bars);
  metric.append(metricTop, history);

  const pill = document.createElement('span');
  pill.className = 'status-pill';
  pill.dataset.state = state;
  pill.textContent = STATUS_LABELS[state];

  row.append(dot, name, metric, pill);
  return row;
}

function safeText(value) {
  return typeof value === 'string' ? value : '';
}

function defaultFormatTime(value) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));
}
