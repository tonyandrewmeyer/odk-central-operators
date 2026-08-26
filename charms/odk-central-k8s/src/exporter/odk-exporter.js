// Copyright 2026 Canonical Ltd.
// See LICENSE file for licensing details.
//
// A Prometheus exporter for ODK Central.
//
// ODK Central exposes no metrics of its own: GET /v1/metrics is a 404 and
// central-backend has no instrumentation dependencies. This fills that gap with
// the numbers an operator actually watches -- how much data is in the
// deployment, how much is arriving, and how much is waiting to move to the blob
// store.
//
// It reads the database directly with psql, which the published image already
// contains, rather than through the API. That avoids having to create and hold
// a Central app-user token just to count rows, and it keeps working when the
// API itself is unhealthy, which is exactly when the numbers matter.
//
// Written in JavaScript because the service image is a Node image. The charm
// pushes this file in and runs it as a Pebble service.

'use strict';

const http = require('node:http');
const { execFile } = require('node:child_process');

const PORT = Number(process.env.EXPORTER_PORT || 9102);
// Never poll the database more often than this, however often Prometheus
// scrapes. These are counts over whole tables; they do not need to be fresh to
// the second, and a tight scrape interval should not turn into database load.
const MIN_INTERVAL_MS = Math.max(60, Number(process.env.EXPORTER_INTERVAL || 60)) * 1000;
const QUERY_TIMEOUT_MS = 30000;

// Each entry is a metric name, its HELP text, and the query behind it.
const METRICS = [
  {
    name: 'odk_central_projects',
    help: 'Number of projects that have not been deleted.',
    query: 'select count(*) from projects where "deletedAt" is null',
  },
  {
    name: 'odk_central_forms',
    help: 'Number of forms that have not been deleted.',
    query: 'select count(*) from forms where "deletedAt" is null',
  },
  {
    name: 'odk_central_submissions',
    help: 'Number of submissions that have not been deleted.',
    query: 'select count(*) from submissions where "deletedAt" is null',
  },
  {
    name: 'odk_central_submissions_recent',
    help: 'Submissions received in the last hour.',
    query: `select count(*) from submissions where "createdAt" > now() - interval '1 hour'`,
  },
  {
    name: 'odk_central_users',
    help: 'Number of web users.',
    query: 'select count(*) from users',
  },
  {
    name: 'odk_central_blobs_pending',
    help: 'Submission attachments still in PostgreSQL awaiting upload to the blob store.',
    query: `select count(*) from blobs where s3_status = 'pending'`,
  },
  {
    name: 'odk_central_blobs_failed',
    help: 'Submission attachments whose upload to the blob store failed.',
    query: `select count(*) from blobs where s3_status = 'failed'`,
  },
];

let cache = { at: 0, values: null, ok: 0, durationSeconds: 0 };

function readDatabaseEnvironment() {
  // The charm renders this file; it is the same configuration the API uses.
  const { database } = require('/usr/odk/config/local.json').default;
  return {
    ...process.env,
    PGHOST: String(database.host),
    PGPORT: String(database.port),
    PGUSER: String(database.user),
    PGPASSWORD: String(database.password),
    PGDATABASE: String(database.database),
    // Do not let a wedged query hold a connection open indefinitely.
    PGCONNECT_TIMEOUT: '10',
  };
}

function runQuery(env, sql) {
  return new Promise((resolve, reject) => {
    execFile(
      'psql',
      ['--no-psqlrc', '--tuples-only', '--no-align', '--command', sql],
      { env, timeout: QUERY_TIMEOUT_MS },
      (error, stdout) => {
        if (error) return reject(error);
        const value = Number(String(stdout).trim());
        return Number.isFinite(value) ? resolve(value) : reject(new Error(`not a number: ${stdout}`));
      },
    );
  });
}

async function collect() {
  const started = Date.now();
  try {
    const env = readDatabaseEnvironment();
    const values = {};
    for (const metric of METRICS) {
      values[metric.name] = await runQuery(env, metric.query);
    }
    cache = { at: Date.now(), values, ok: 1, durationSeconds: (Date.now() - started) / 1000 };
  } catch (err) {
    // Keep serving the previous values, but say the scrape failed: a stale
    // number reported as fresh is worse than an explicit failure.
    console.error(`[odk-exporter] collection failed: ${err.message}`);
    cache = { ...cache, at: Date.now(), ok: 0, durationSeconds: (Date.now() - started) / 1000 };
  }
}

function render() {
  const lines = [];
  if (cache.values) {
    for (const metric of METRICS) {
      lines.push(`# HELP ${metric.name} ${metric.help}`);
      lines.push(`# TYPE ${metric.name} gauge`);
      lines.push(`${metric.name} ${cache.values[metric.name]}`);
    }
  }
  lines.push('# HELP odk_central_scrape_success Whether the last collection succeeded.');
  lines.push('# TYPE odk_central_scrape_success gauge');
  lines.push(`odk_central_scrape_success ${cache.ok}`);
  lines.push('# HELP odk_central_scrape_duration_seconds How long the last collection took.');
  lines.push('# TYPE odk_central_scrape_duration_seconds gauge');
  lines.push(`odk_central_scrape_duration_seconds ${cache.durationSeconds}`);
  return `${lines.join('\n')}\n`;
}

async function main() {
  await collect();
  setInterval(collect, MIN_INTERVAL_MS).unref?.();

  http
    .createServer((request, response) => {
      if (request.url !== '/metrics') {
        response.writeHead(404).end('not found\n');
        return;
      }
      response.writeHead(200, { 'Content-Type': 'text/plain; version=0.0.4' });
      response.end(render());
    })
    .listen(PORT, () => console.log(`[odk-exporter] listening on ${PORT}`));
}

main();
