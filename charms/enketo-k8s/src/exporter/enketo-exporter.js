// Copyright 2026 Canonical Ltd.
// See LICENSE file for licensing details.
//
// A Prometheus exporter for Enketo and its two Redis instances.
//
// Enketo exposes no metrics of its own. What matters operationally is whether
// it is serving, and the state of the two Redis instances behind it -- in
// particular the durable one, which holds in-flight form and instance state and
// is the only thing in the ODK Central charm group that is not in PostgreSQL or
// the blob store.
//
// Redis is queried by speaking RESP over a plain socket rather than by shelling
// out to redis-cli: the Enketo container is a Node image and has no redis-cli,
// and the sidecars are reachable over the pod network in any case, which is
// also what makes this work unchanged when a redis relation replaces one.

'use strict';

const http = require('node:http');
const net = require('node:net');

const PORT = Number(process.env.EXPORTER_PORT || 9104);
const ENKETO_PORT = Number(process.env.ENKETO_PORT || 8005);
const INTERVAL_MS = Math.max(30, Number(process.env.EXPORTER_INTERVAL || 60)) * 1000;
const TIMEOUT_MS = 5000;

// Supplied by the charm so that a relation-backed instance is scraped in
// exactly the same way as a sidecar.
const INSTANCES = [
  { label: 'main', host: process.env.REDIS_MAIN_HOST || '127.0.0.1', port: Number(process.env.REDIS_MAIN_PORT || 6379) },
  { label: 'cache', host: process.env.REDIS_CACHE_HOST || '127.0.0.1', port: Number(process.env.REDIS_CACHE_PORT || 6380) },
];

// INFO fields worth alerting or graphing on, and the metric each becomes.
const INFO_FIELDS = {
  used_memory: { name: 'enketo_redis_memory_used_bytes', help: 'Memory in use.' },
  maxmemory: { name: 'enketo_redis_memory_max_bytes', help: 'Configured memory limit, 0 for unlimited.' },
  connected_clients: { name: 'enketo_redis_connected_clients', help: 'Currently connected clients.' },
  evicted_keys: { name: 'enketo_redis_evicted_keys', help: 'Keys evicted because of the memory limit.' },
  keyspace_hits: { name: 'enketo_redis_keyspace_hits', help: 'Successful key lookups.' },
  keyspace_misses: { name: 'enketo_redis_keyspace_misses', help: 'Failed key lookups.' },
  rdb_last_bgsave_status: { name: null },
  uptime_in_seconds: { name: 'enketo_redis_uptime_seconds', help: 'Seconds since the instance started.' },
};

let cache = { enketoUp: 0, instances: {}, ok: 0 };

function redisInfo(host, port) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host, port });
    let buffer = '';
    const fail = (err) => { socket.destroy(); reject(err); };

    socket.setTimeout(TIMEOUT_MS, () => fail(new Error('timeout')));
    socket.on('error', fail);
    socket.on('connect', () => socket.write('INFO\r\n'));
    socket.on('data', (chunk) => {
      buffer += chunk.toString('utf8');
      // RESP bulk string: $<length>\r\n<payload>\r\n
      const headerEnd = buffer.indexOf('\r\n');
      if (headerEnd === -1) return;
      const length = Number(buffer.slice(1, headerEnd));
      if (!Number.isFinite(length)) return fail(new Error(`unexpected reply: ${buffer.slice(0, 40)}`));
      if (buffer.length >= headerEnd + 2 + length) {
        socket.end();
        resolve(buffer.slice(headerEnd + 2, headerEnd + 2 + length));
      }
      return undefined;
    });
  });
}

function parseInfo(text) {
  const fields = {};
  for (const line of text.split('\r\n')) {
    if (!line || line.startsWith('#')) continue;
    const index = line.indexOf(':');
    if (index === -1) continue;
    fields[line.slice(0, index)] = line.slice(index + 1);
  }
  return fields;
}

function tcpProbe(host, port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host, port });
    socket.setTimeout(TIMEOUT_MS, () => { socket.destroy(); resolve(0); });
    socket.on('error', () => resolve(0));
    socket.on('connect', () => { socket.end(); resolve(1); });
  });
}

async function collect() {
  const instances = {};
  let ok = 1;
  for (const instance of INSTANCES) {
    try {
      instances[instance.label] = { up: 1, fields: parseInfo(await redisInfo(instance.host, instance.port)) };
    } catch (err) {
      console.error(`[enketo-exporter] redis ${instance.label} (${instance.host}:${instance.port}): ${err.message}`);
      instances[instance.label] = { up: 0, fields: {} };
      ok = 0;
    }
  }
  cache = { enketoUp: await tcpProbe('127.0.0.1', ENKETO_PORT), instances, ok };
}

function render() {
  const lines = [
    '# HELP enketo_up Whether Enketo is accepting connections.',
    '# TYPE enketo_up gauge',
    `enketo_up ${cache.enketoUp}`,
    '# HELP enketo_redis_up Whether the Redis instance answered INFO.',
    '# TYPE enketo_redis_up gauge',
  ];
  for (const [label, instance] of Object.entries(cache.instances)) {
    lines.push(`enketo_redis_up{instance="${label}"} ${instance.up}`);
  }

  for (const [field, metric] of Object.entries(INFO_FIELDS)) {
    if (!metric.name) continue;
    lines.push(`# HELP ${metric.name} ${metric.help}`);
    lines.push(`# TYPE ${metric.name} gauge`);
    for (const [label, instance] of Object.entries(cache.instances)) {
      const raw = Number(instance.fields[field]);
      if (Number.isFinite(raw)) lines.push(`${metric.name}{instance="${label}"} ${raw}`);
    }
  }

  // Reported separately because it is a status word, not a number, and a
  // failing background save on the durable instance means the state that is
  // not in PostgreSQL is not being written to disk.
  lines.push('# HELP enketo_redis_last_save_ok Whether the last background save succeeded.');
  lines.push('# TYPE enketo_redis_last_save_ok gauge');
  for (const [label, instance] of Object.entries(cache.instances)) {
    const status = instance.fields.rdb_last_bgsave_status;
    if (status) lines.push(`enketo_redis_last_save_ok{instance="${label}"} ${status === 'ok' ? 1 : 0}`);
  }

  lines.push('# HELP enketo_scrape_success Whether the last collection succeeded.');
  lines.push('# TYPE enketo_scrape_success gauge');
  lines.push(`enketo_scrape_success ${cache.ok}`);
  return `${lines.join('\n')}\n`;
}

async function main() {
  await collect();
  setInterval(collect, INTERVAL_MS).unref?.();

  http
    .createServer((request, response) => {
      if (request.url !== '/metrics') {
        response.writeHead(404).end('not found\n');
        return;
      }
      response.writeHead(200, { 'Content-Type': 'text/plain; version=0.0.4' });
      response.end(render());
    })
    .listen(PORT, () => console.log(`[enketo-exporter] listening on ${PORT}`));
}

main();
