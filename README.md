# odk-central-operators

Juju charms for [ODK Central](https://getodk.org/), an open-source platform for
collecting, managing and using survey and field data.

[![CI](https://github.com/tonyandrewmeyer/odk-central-operators/actions/workflows/ci.yml/badge.svg)](https://github.com/tonyandrewmeyer/odk-central-operators/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

## What is ODK Central?

ODK Central is the server half of the [ODK](https://getodk.org/) ecosystem:
forms authored as XLSForm spreadsheets are converted to XForms, filled in
offline on a mobile client or in a browser, and submitted back to Central. It
manages projects, form versions, submissions, attachments and users, and
exposes everything over a REST and OData API.

## The charm group

Upstream ships ODK Central as an eight-container Docker Compose stack. This
repository deploys it as **three Juju applications** that are designed to be
deployed together.

| Charm | Containers | What it runs |
|---|---|---|
| `odk-central-k8s` | `service`, `nginx` | The Central API (Node/pm2) and the nginx reverse proxy serving the built frontend. Released and version-locked together upstream. |
| `enketo-k8s` | `enketo`, `redis-main`, `redis-cache` | [Enketo](https://github.com/enketo/enketo), which renders XForms as web forms, plus its two Redis instances (durable and cache). |
| `pyxform-k8s` | `pyxform` | The stateless [pyxform](https://github.com/XLSForm/pyxform) HTTP service that converts XLSForm spreadsheets to XForms. |

Upstream's one-shot `secrets` container has no charm equivalent: Juju has no
shared volume between applications, so `odk-central-k8s` generates the three
shared Enketo secrets itself, stores them as application-owned Juju secrets and
distributes them over the `odk-enketo` relation. See
[`docs/architecture.md`](docs/architecture.md).

## Quick start

```bash
juju add-model odk

juju deploy odk-central-k8s --channel latest/edge --trust
juju deploy enketo-k8s      --channel latest/edge
juju deploy pyxform-k8s     --channel latest/edge

juju deploy postgresql-k8s --channel 16/stable --trust
juju deploy traefik-k8s    --channel latest/stable --trust

juju integrate odk-central-k8s postgresql-k8s
juju integrate odk-central-k8s pyxform-k8s
juju integrate odk-central-k8s enketo-k8s
juju integrate odk-central-k8s traefik-k8s

juju config odk-central-k8s external-hostname=odk.example.com

SECRET_ID=$(juju add-secret odk-admin-email email=ops-team@example.com)
juju grant-secret "$SECRET_ID" odk-central-k8s
juju config odk-central-k8s admin-email-secret="$SECRET_ID"
juju run odk-central-k8s/0 create-admin
```

The full walkthrough, from an empty model to a first submission, is in
[`docs/quickstart.md`](docs/quickstart.md).

## Relation matrix

| Charm | Endpoint | Interface | Role | Purpose |
|---|---|---|---|---|
| `odk-central-k8s` | `odk-enketo` | `odk-enketo` | provides | Ships the three shared secrets and the public base URL to Enketo; receives Enketo's in-cluster URL back |
| `odk-central-k8s` | `postgresql` | `postgresql_client` | requires | Central's application database |
| `odk-central-k8s` | `xlsform` | `odk-xlsform` | requires | pyxform host and port |
| `odk-central-k8s` | `ingress` | `ingress` | requires | Public HTTP ingress via traefik |
| `odk-central-k8s` | `s3` | `s3` | requires | Blob store for submission attachments and form media |
| `odk-central-k8s` | `smtp` | `smtp` | requires | Account creation and password reset email |
| `odk-central-k8s` | `oauth` | `oauth` | requires | OpenID Connect via the Canonical Identity Platform |
| `odk-central-k8s` | `logging` / `metrics-endpoint` / `grafana-dashboard` / `tracing` | COS | requires | Observability |
| `enketo-k8s` | `odk-enketo` | `odk-enketo` | requires | Shared secrets and base URL from Central |
| `enketo-k8s` | `redis-main` / `redis-cache` | `redis` | requires (optional) | External Redis instead of the in-charm sidecars |
| `enketo-k8s` | `logging` / `metrics-endpoint` / `grafana-dashboard` | COS | requires | Observability |
| `pyxform-k8s` | `xlsform` | `odk-xlsform` | provides | Advertises its host and port to Central |
| `pyxform-k8s` | `logging` / `metrics-endpoint` | COS | requires | Observability |

`pyxform-k8s` has no ingress endpoint on purpose: it is reached only by
Central, in-cluster, and must not be exposed.

## Day-2 operations

| Charm | Action | Description |
|---|---|---|
| `odk-central-k8s` | `create-admin` | Create the initial web user from the `odk-admin-email` Juju secret and promote it to administrator. Returns the password once. |
| `odk-central-k8s` | `promote-user` | Grant an existing user the administrator role. |
| `odk-central-k8s` | `reset-password` | Reset a user's password and return the new value once. |
| `odk-central-k8s` | `rotate-enketo-secrets` | Regenerate all three shared Enketo secrets, republish them over the relation and restart both workloads. |
| `odk-central-k8s` | `run-migrations` | Run database migrations manually. |
| `odk-central-k8s` | `upload-pending-blobs` | Move submission attachments still held in PostgreSQL to the S3 blob store. |
| `odk-central-k8s` | `purge-deleted` | Permanently remove soft-deleted forms and submissions. |
| `odk-central-k8s` | `backup` | `pg_dump` the Central database to the S3 blob store. |
| `odk-central-k8s` | `restore` | Restore the Central database from a backup snapshot. |
| `enketo-k8s` | `flush-cache` | Flush the cache Redis instance only, never the durable one. |
| `enketo-k8s` | `redis-info` | Return `INFO` output from both Redis instances. |
| `pyxform-k8s` | `convert` | Convert a base64-encoded XLSForm and return the XForm. Diagnostic only. |

Full detail, including what each one does to running users, is in
[`docs/day2-ops.md`](docs/day2-ops.md).

## Observability

All three charms are COS-Lite ready. `odk-central-k8s` and `enketo-k8s` forward
every Pebble service's logs to Loki, expose Prometheus metrics, and ship a
Grafana dashboard and alert rules covering API availability, pending blob
uploads, Redis reachability and pyxform conversion failures.

```bash
juju integrate odk-central-k8s grafana-k8s
juju integrate odk-central-k8s prometheus-k8s
juju integrate odk-central-k8s loki-k8s
```

See [`docs/observability.md`](docs/observability.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

Apache 2.0. So is every piece of the workload these charms deploy — see
[CONTRIBUTING.md](CONTRIBUTING.md#licensing).
