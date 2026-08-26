# Observability

All three charms are COS-ready. What they can honestly report is shaped by what
the workloads expose, which is less than you might expect, so this page says
what each signal actually is and where it comes from.

## What the workloads expose

Nothing, is the short answer.

- **ODK Central** has no metrics endpoint. `GET /v1/metrics` is a 404 and
  `central-backend` has no instrumentation dependencies in its `package.json`.
- **Enketo** exposes no metrics either, and neither does the Redis image beyond
  the `INFO` command.
- **pyxform-http** is a small Flask application behind gunicorn with no
  counters.

So each charm ships a small exporter, written in whatever language its own
image already has, and runs it as an extra Pebble service. Nothing is added to
the images and no extra container resources are needed.

| Charm | Exporter | Port | How it collects |
|---|---|---|---|
| `odk-central-k8s` | Node | 9102 | Runs `psql` against the related database, using the same credentials the API uses |
| `enketo-k8s` | Node | 9104 | Speaks RESP to both Redis instances over a socket, and TCP-probes Enketo |
| `pyxform-k8s` | Python | 9103 | Converts a known-good XLSForm and checks the result is an XForm |

Each caches its results and never collects more often than once a minute,
however often Prometheus scrapes: these are whole-table counts and real
conversions, not cheap counters.

### Why these signals

The exporters deliberately do not report request rates, which is the usual
first instinct. For this workload the questions that matter are different:

- **Is data arriving?** `odk_central_submissions_recent` is submissions in the
  last hour. On a deployment that should be collecting continuously, a drop to
  zero is usually a client-side failure that no server-side metric would show.
- **Is data safe?** `odk_central_blobs_pending` is attachments still sitting in
  PostgreSQL rather than the blob store, and
  `enketo_redis_last_save_ok{instance="main"}` is whether Enketo's in-flight
  form state is reaching disk.
- **Can new forms be published?** `pyxform_conversion_success` is the only
  signal for this. A converter that is up but failing blocks all form
  publishing while every other part of the deployment looks perfectly healthy.

## Metrics

```bash
juju integrate odk-central-k8s:metrics-endpoint prometheus-k8s
juju integrate enketo-k8s:metrics-endpoint prometheus-k8s
juju integrate pyxform-k8s:metrics-endpoint prometheus-k8s
```

| Metric | Charm | Meaning |
|---|---|---|
| `odk_central_projects` | central | Projects not deleted |
| `odk_central_forms` | central | Forms not deleted |
| `odk_central_submissions` | central | Submissions not deleted |
| `odk_central_submissions_recent` | central | Submissions in the last hour |
| `odk_central_users` | central | Web users |
| `odk_central_blobs_pending` | central | Attachments awaiting upload to the blob store |
| `odk_central_blobs_failed` | central | Attachments whose upload failed |
| `odk_central_scrape_success` | central | Whether the exporter could reach the database |
| `enketo_up` | enketo | Whether Enketo accepts connections |
| `enketo_redis_up{instance}` | enketo | Whether each Redis instance answers `INFO` |
| `enketo_redis_memory_used_bytes{instance}` | enketo | Memory in use |
| `enketo_redis_keyspace_hits{instance}` / `_misses` | enketo | Cache effectiveness |
| `enketo_redis_evicted_keys{instance}` | enketo | Keys dropped under memory pressure |
| `enketo_redis_last_save_ok{instance}` | enketo | Whether the last background save worked |
| `pyxform_up` | pyxform | Whether the converter answers |
| `pyxform_conversion_success` | pyxform | Whether a known-good form still converts |
| `pyxform_conversion_duration_seconds` | pyxform | How long that took |

## Logs

```bash
juju integrate odk-central-k8s:logging loki-k8s
juju integrate enketo-k8s:logging loki-k8s
juju integrate pyxform-k8s:logging loki-k8s
```

The charms use `LogForwarder`, which configures Pebble's own log targets rather
than injecting a promtail sidecar. Every Pebble service's standard output is
forwarded, labelled with `pebble_service` and the Juju topology:

| `pebble_service` | Charm | What it is |
|---|---|---|
| `service` | central | The Central API, including its HTTP access log |
| `nginx` | central | The frontend proxy's access and error log |
| `enketo` | enketo | Enketo Express |
| `redis-main` / `redis-cache` | enketo | The two Redis instances |
| `pyxform` | pyxform | gunicorn's access and error log |
| `exporter` | all three | The charm's own exporter |

Central's HTTP request and error rates come from `service` and `nginx` logs via
Loki rather than from Prometheus, because there are no HTTP metrics to scrape:

```logql
{juju_application="odk-central-k8s", pebble_service="nginx"} |~ ` 5\d\d `
```

## Dashboards

`odk-central-k8s` and `enketo-k8s` each ship a dashboard, registered
automatically over the `grafana-dashboard` relation.

```bash
juju integrate odk-central-k8s:grafana-dashboard grafana-k8s
juju integrate enketo-k8s:grafana-dashboard grafana-k8s
```

**ODK Central — overview** covers deployment size, submissions per hour, the
pending and failed blob counts, and how long the exporter's own queries take,
which is an early sign of database pressure.

**Enketo — web forms and Redis** covers availability of Enketo and both Redis
instances, whether the durable instance is snapshotting, the cache hit ratio,
and memory against the configured limit.

`pyxform-k8s` ships alert rules but no dashboard: it has three metrics, and
they belong on the Central dashboard next to the form counts rather than on a
page of their own.

## Alert rules

Thirteen rules ship with the charms, and Prometheus picks them up over the same
`metrics-endpoint` relation with Juju topology labels applied.

| Alert | Charm | Severity | Fires when |
|---|---|---|---|
| `OdkCentralExporterDown` | central | critical | The API's pod has been unreachable for 2 minutes |
| `OdkCentralDatabaseUnreachable` | central | critical | The exporter cannot query PostgreSQL for 2 minutes |
| `OdkCentralBlobUploadsStalled` | central | warning | Pending attachments have not fallen for an hour |
| `OdkCentralBlobUploadsFailing` | central | warning | Any attachment upload has failed |
| `OdkCentralNoSubmissions` | central | info | No submissions for a day |
| `EnketoDown` | enketo | critical | Enketo has not served for 5 minutes |
| `EnketoDurableRedisUnreachable` | enketo | critical | The durable Redis is unreachable for 2 minutes |
| `EnketoDurableRedisNotSaving` | enketo | warning | Background saves have failed for 15 minutes |
| `EnketoCacheRedisUnreachable` | enketo | warning | The cache Redis is unreachable for 10 minutes |
| `EnketoCacheEvicting` | enketo | info | The cache is evicting under memory pressure |
| `PyxformConversionFailing` | pyxform | critical | A known-good form has not converted for 10 minutes |
| `PyxformDown` | pyxform | critical | The converter has not answered for 5 minutes |
| `PyxformConversionSlow` | pyxform | warning | A two-question form takes over 30 seconds |

The severities are deliberately not uniform across the two Redis instances.
Losing the cache costs a re-transformation; losing the durable instance loses
submissions people have started but not sent.

## Tracing

```bash
juju integrate odk-central-k8s:tracing tempo-coordinator-k8s
```

**The workload emits no spans.** `central-backend` has no OpenTelemetry
dependencies, so there is nothing in ODK Central to trace, and the charm does
not pretend otherwise by synthesising spans that would only describe itself.

What the relation does give you is *charm* tracing: `ops` emits spans for the
charm's own hook executions, which is genuinely useful for understanding why a
deployment is slow to settle or which relation event is taking the time. It
will not tell you anything about an individual form submission.

If upstream instruments `central-backend` in a future release, the relation is
already wired and the workload will only need the OTLP endpoint in its
environment.

## Deploying COS

The COS charms have moved to versioned tracks; `latest/stable` no longer
resolves to a current revision:

```bash
juju deploy loki-k8s       --channel 3.7/stable  --trust
juju deploy prometheus-k8s --channel 3.11/stable --trust
juju deploy grafana-k8s    --channel 12.4/stable --trust
```

For a production deployment, run COS in its own model and consume it across the
model boundary:

```bash
juju switch cos
juju offer prometheus-k8s:metrics-endpoint
juju offer loki-k8s:logging
juju offer grafana-k8s:grafana-dashboard

juju switch odk
juju consume cos.prometheus-k8s
juju integrate odk-central-k8s:metrics-endpoint prometheus-k8s
```

The charms behave identically either way — a cross-model relation is still just
a relation — so a single-model deployment is fine for testing.
