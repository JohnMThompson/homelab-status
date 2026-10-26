import test from 'node:test';
import assert from 'node:assert/strict';
import { getSnapshotState, renderServiceGroups, SERVICE_GROUPS } from './service-renderer.js';

class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = tagName;
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.className = '';
    this.textContent = '';
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
}

class FakeDocument {
  createElement(tagName) { return new FakeElement(tagName, this); }
}

function sampleServices() {
  const entries = [
    ['pileated-host', 'Pileated host', 'Infrastructure', 'uptime'],
    ['home-assistant', 'Home Assistant', 'Infrastructure', 'uptime'],
    ['forgejo', 'Forgejo', 'Infrastructure', 'uptime'],
    ['analytics', 'Analytics', 'Applications', 'uptime'],
    ['freight', 'Freight', 'Applications', 'uptime'],
    ['swim-writer', 'Swim Writer', 'Applications', 'uptime'],
    ['diamonds', 'Diamonds', 'Applications', 'uptime'],
    ['nightly-backup', 'Nightly backup', 'Backup & Recovery', 'freshness'],
    ['weekly-backup-maintenance', 'Weekly backup maintenance', 'Backup & Recovery', 'freshness'],
  ];
  return entries.map(([id, name, group, metric_type], index) => ({
    id, name, group, metric_type, description: `Description for ${name}`,
    status: index === 6 ? 'degraded' : 'up', uptime_24h: metric_type === 'uptime' ? 99.5 : null,
    last_success_at: metric_type === 'freshness' ? '2026-10-25T12:00:00Z' : null,
    history_24h: Array(24).fill(index === 6 ? 'unknown' : 'up'),
  }));
}

function mount() {
  const document = new FakeDocument();
  return document.createElement('div');
}

function allNodes(node) {
  return [node, ...node.children.flatMap(allNodes)];
}

test('an empty build fixture can be replaced by the live nine-service payload', () => {
  const container = mount();
  renderServiceGroups(container, []);
  assert.equal(allNodes(container).filter((node) => node.className === 'service-row').length, 0);

  const services = sampleServices();
  renderServiceGroups(container, services, { current: true, formatTime: (value) => value });
  const nodes = allNodes(container);
  assert.deepEqual(nodes.filter((node) => node.className === 'service-group').map((node) => node.attributes['aria-label']), SERVICE_GROUPS);
  assert.equal(nodes.filter((node) => node.className === 'service-row').length, 9);
  assert.ok(nodes.some((node) => node.tagName === 'h4' && node.textContent === 'Weekly backup maintenance'));
  assert.ok(nodes.some((node) => node.tagName === 'strong' && node.textContent === '99.50%'));
  assert.ok(nodes.some((node) => node.tagName === 'strong' && node.textContent === '2026-10-25T12:00:00Z'));
  assert.equal(nodes.filter((node) => node.className === 'up').length, 24 * 8);
});

test('service strings are inserted as text and history states are constrained', () => {
  const container = mount();
  renderServiceGroups(container, [{
    ...sampleServices()[0], name: '<img src=x onerror=alert(1)>', description: '<script>bad()</script>',
    history_24h: ['down', 'maintenance', 'not-a-state'],
  }]);
  const nodes = allNodes(container);
  const name = nodes.find((node) => node.tagName === 'h4');
  const description = nodes.find((node) => node.tagName === 'p' && node.textContent.includes('<script>'));
  assert.equal(name.textContent, '<img src=x onerror=alert(1)>');
  assert.equal(name.children.length, 0);
  assert.equal(description.textContent, '<script>bad()</script>');
  const bars = nodes.find((node) => node.className === 'uptime-bar').children;
  assert.deepEqual(bars.slice(0, 3).map((node) => node.className), ['down', 'maintenance', 'unknown']);
  assert.equal(bars.length, 24);
});

test('empty, stale, and unavailable snapshots never count as current service data', () => {
  const now = Date.parse('2026-10-25T14:00:00Z');
  const emptyFixture = { generated_at: '1970-01-01T00:00:00Z', services: [] };
  assert.deepEqual(getSnapshotState(emptyFixture, now), { valid: true, stale: true, empty: true, current: false });
  assert.deepEqual(getSnapshotState({ generated_at: '2026-10-25T12:00:00Z', services: sampleServices() }, now),
    { valid: true, stale: true, empty: false, current: false });
  assert.deepEqual(getSnapshotState(null, now), { valid: false, stale: true, empty: false, current: false });
  assert.deepEqual(getSnapshotState({ generated_at: '2026-10-25T13:30:00Z', services: sampleServices() }, now),
    { valid: true, stale: false, empty: false, current: true });
});
