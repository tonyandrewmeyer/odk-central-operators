# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because the three charms in this repository are released as a group, one
changelog covers all of them. Entries name the charm they affect.

## [Unreleased]

## [0.1.0] - 2026-08-26

First release. Deploys ODK Central 2026.2.4 on Kubernetes as three charms.

### Added

#### The charm group

- **`odk-central-k8s`** runs the ODK Central API and the nginx frontend proxy,
  renders the workload's `config.json` from its relations, runs database
  migrations as a checkable step, and anchors the group.
- **`enketo-k8s`** runs Enketo Express and its two Redis instances, the durable
  one backed by Juju filesystem storage.
- **`pyxform-k8s`** runs the XLSForm conversion service and is the only member
  of the group that reaches active on its own.

#### Interfaces

- `odk-enketo`, in `lib/charms/odk_central_k8s/v0/odk_enketo.py`. Replaces
  upstream's one-shot `secrets` container, which has no Juju equivalent: Central
  generates the three shared secrets, stores them as application-owned Juju
  secrets, grants them to the related application and publishes only their IDs.
  Enketo answers with the URL Central should use to reach it.
- `odk-xlsform`, in `lib/charms/pyxform_k8s/v0/xlsform.py`. Advertises the
  conversion service's address, and withdraws it when the converter stops
  working.

#### Relations

- `postgresql_client`, `ingress`, `s3`, `smtp`, `oauth`, `loki_push_api`,
  `prometheus_scrape`, `grafana_dashboard` and `tracing`, plus optional `redis`
  endpoints on `enketo-k8s` that stand the in-charm sidecars down when used.

#### Actions

- `odk-central-k8s`: `create-admin`, `promote-user`, `reset-password`,
  `rotate-enketo-secrets`, `run-migrations`, `upload-pending-blobs`,
  `purge-deleted`, `backup`, `restore`.
- `enketo-k8s`: `flush-cache`, `redis-info`.
- `pyxform-k8s`: `convert`.

#### Observability

- A Prometheus exporter in each charm, written in the language its own image
  already has, because none of the three workloads exposes metrics.
- Two Grafana dashboards and thirteen alert rules.
- Log forwarding for all seven Pebble services across the group.

#### Documentation

- A mkdocs-material site covering quickstart, architecture, configuration,
  relations, day-2 operations, observability and migration from Docker Compose.

### Notes

Things worth knowing before deploying, each of which is documented in more
detail on the pages linked from the docs site:

- **Deploy `postgresql-k8s --channel 14/stable`.** ODK Central targets
  PostgreSQL 14 and its image ships `postgresql-client-14`. On a 16-series
  database the application runs fine, but `pg_dump` refuses to dump a newer
  server, which disables this charm's `backup` and `restore` actions and ODK
  Central's own `/v1/backup` endpoint.
- **Error reporting is off by default.** Upstream's shipped compose
  configuration contains the ODK project's own Sentry organisation, key and
  project id. These charms never inherit them.
- **Enabling OIDC is a one-way door in practice.** It disables password
  authentication entirely, and turning it back off does not restore anyone's
  access.
- **`redis-main` is the only state in the group that is neither in PostgreSQL
  nor in the blob store.** It holds submissions people have started but not
  sent, and it is not in any database backup.
- **Submission attachments migrate to S3 lazily.** New blobs go to the bucket
  as soon as the relation exists; existing ones stay in PostgreSQL until
  `upload-pending-blobs` moves them.
- The charms use the stock `enketo/enketo` image rather than the derived one
  upstream builds, so Enketo's client-side branding follows that image.

[Unreleased]: https://github.com/tonyandrewmeyer/odk-central-operators/compare/0.1.0...HEAD
[0.1.0]: https://github.com/tonyandrewmeyer/odk-central-operators/releases/tag/0.1.0
