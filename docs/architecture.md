# Architecture

## Why three charms

Upstream ships ODK Central as an eight-container Docker Compose stack. This
repository deploys it as three Juju applications, because the pieces have
genuinely different lifecycles.

| Upstream service | Image | Charm | Pebble service |
|---|---|---|---|
| `service` | `ghcr.io/getodk/central-service` | `odk-central-k8s` | `service` |
| `nginx` | `ghcr.io/getodk/central-nginx` | `odk-central-k8s` | `nginx` |
| `enketo` | `ghcr.io/enketo/enketo` | `enketo-k8s` | `enketo` |
| `enketo_redis_main` | `redis` | `enketo-k8s` | `redis-main` |
| `enketo_redis_cache` | `redis` | `enketo-k8s` | `redis-cache` |
| `pyxform` | `ghcr.io/getodk/pyxform-http` | `pyxform-k8s` | `pyxform` |
| `secrets` | — | none | replaced by Juju secrets |
| `postgres14` | — | none | `postgresql-k8s` relation |
| `mail` | — | none | `smtp-integrator` relation |
| `postgres` (9.6→14 upgrade) | — | none | no equivalent, see [migrating](migrating-from-compose.md) |

`service` and `nginx` stay together because upstream releases them as a pair
and the frontend is compiled into the nginx image at build time: the two are
version-locked, and separating them would only create a way to get them out of
step. Enketo and pyxform are separate upstream projects on their own release
cadences, and Enketo has state of its own, so both scale and upgrade
independently.

Each charm also ships a small Prometheus exporter as an extra Pebble service —
see [Observability](observability.md), which explains why that is necessary.

## The bootstrap, and why it does not deadlock

```text
pyxform-k8s ──(xlsform)──▶ odk-central-k8s ──(odk-enketo)──▶ enketo-k8s
                                  ▲                                │
                                  └──── enketo URL sent back ──────┘
```

`pyxform-k8s` is the only member of the group that reaches active on its own.
It has no dependencies, advertises its address, and is done.

`odk-central-k8s` and `enketo-k8s` need each other, in both directions:

1. Central generates the three shared secrets and publishes them.
2. Enketo cannot start without them, so it waits.
3. Enketo starts, and publishes the URL Central should use to reach it.
4. Central cannot render a complete configuration without that URL.

Resolved by making step 4 non-blocking. Central starts with a placeholder
`enketo.url`, reports
`api ready; waiting for enketo (web forms will not render)`, and re-renders and
restarts when Enketo answers. If Central waited instead, neither would ever
start.

## The `secrets` container has no charm equivalent

Upstream runs a one-shot container that writes three files into a Docker volume
that both `service` and `enketo` mount. Juju has no shared volume between
applications, so that design does not transfer.

Instead `odk-central-k8s` generates the three values in charm code, stores them
as application-owned Juju secrets, grants them to the related application, and
publishes **only the secret IDs** over the relation. Enketo reads the secrets by
ID and writes them to the paths upstream's tooling expects.

```
odk-central-k8s                             enketo-k8s
    │                                            │
    ├─ generates 128/64/32-byte values           │
    ├─ stores them as Juju secrets               │
    ├─ grants them to the relation ─────────────▶│
    ├─ publishes IDs + base URL ────────────────▶│
    │                                            ├─ reads secrets by ID
    │                                            ├─ writes /etc/secrets/*
    │                                            ├─ renders config.json
    │                                            ├─ starts Enketo
    │◀──────────────── enketo-url ───────────────┤
    ├─ re-renders config.json                    │
    └─ restarts the API                          │
```

A rotation moves the *contents* of those secrets; the IDs do not change, so it
arrives at Enketo as a `secret-changed` event rather than a relation change.

### The secret lengths are load-bearing

Upstream's `start-enketo.sh` checks the files with `stat` and refuses to start
on any other size:

| File | Bytes |
|---|---|
| `/etc/secrets/enketo-secret` | 64 |
| `/etc/secrets/enketo-less-secret` | 32 |
| `/etc/secrets/enketo-api-key` | 128 |

A trailing newline makes a 64-byte secret a 65-byte file, and the resulting
failure does not obviously point at the cause. The charms generate the values at
exactly these lengths from an ASCII alphabet — so that a character count is also
a byte count — re-check them before anything is written to a container, and
write them without a trailing newline.

## What the charms had to change about the images

The published images are not quite the ones upstream runs, and the charms
compensate rather than pretending otherwise.

**The Enketo image is not upstream's.** `getodk/central` builds its own Enketo
image from `enketo.dockerfile`, layering `start-enketo.sh` and a config template
onto the stock `enketo/enketo`. The stock image the charm consumes has neither,
and its defaults are KoBoCAT's rather than ODK's, so the charm supplies the
start command and the whole configuration itself. One consequence: Enketo bakes
client-visible settings into its browser bundle at image build time, so branding
follows the stock image.

**The nginx image contains no site configuration.** `odk.conf.template` and
`client-config.json.template` are bind-mounted by upstream's compose file, not
copied into the image, so the charm ships and pushes both. It makes two changes
to the site template:

- `proxy_pass http://service:8383` becomes `127.0.0.1`, since the API is in the
  same pod;
- `proxy_pass http://enketo:8005` becomes a charm-supplied address, because
  `enketo` is a separate Juju application whose name will never resolve.

**Blanking Sentry breaks nginx if done naively.** Upstream's template proxies
`/csp-report` to `https://${SENTRY_ORG_SUBDOMAIN}.ingest.sentry.io/...`. With
the values emptied that renders as `https://.ingest.sentry.io/api//`, which
nginx refuses to load, so the charm replaces the location with `return 204`
when no DSN is configured.

**`start-odk.sh` does four things.** It renders the config, waits for
PostgreSQL, runs migrations, starts cron, and only then starts the server. The
charm drives those steps separately, which is what lets migrations be a
checkable operation with an exit code rather than a side effect of starting.

## Configuration is a file, not environment variables

ODK Central reads a JSON document, and the charm renders the whole thing:

| `config.json` path | Source |
|---|---|
| `database.*` | `postgresql` relation and `db-pool-size` |
| `email.*` | `smtp` relation and `email-from` |
| `xlsform.host` / `.port` | `xlsform` relation |
| `enketo.url` | `odk-enketo` relation |
| `enketo.apiKey` | generated Juju secret |
| `env.domain` | `ingress` relation or `external-hostname` |
| `oidc.*` | `oauth` relation, else configuration and a Juju secret |
| `external.sentry.*` | `error-reporting-dsn`, blank by default |
| `external.s3blobStore.*` | `s3` relation |

Two things about this are easy to get wrong:

- **`database.*` is translated into libpq environment variables by
  central-backend, and a `PG*` variable already in the environment wins.**
  Setting both would make the rendered configuration silently inert, so the
  charm sets none. central-backend also rejects unknown keys under `database`,
  which is why TLS options have to go in `PGSSLMODE` and `PGSSLROOTCERT`.
- **Central reads the file once, at startup.** Pebble's `replan` only restarts a
  service whose *layer* changed, and a new database endpoint or mail relay
  changes a file, not a layer. The charms compare what they are about to write
  with what is already there and restart the workload when it differs. The same
  applies to Enketo and to the Redis sidecars.

## State

| Where | What | If you lose it |
|---|---|---|
| PostgreSQL | Projects, forms, users, submissions | Everything |
| S3 bucket | Submission attachments, once uploaded | Attachments |
| `redis-main` storage | Enketo's in-flight form state | Submissions people started but did not send |
| `redis-cache` | Transformed forms | Nothing; they are re-transformed |

`redis-main` is the one that surprises people: it is the only state in the group
that is neither in PostgreSQL nor in the blob store, and it is not in any
database backup.

Attachments can also be in **both** places at once, or rather in neither
consistently: new blobs go to S3 as soon as the relation exists, but existing
ones stay in PostgreSQL until `upload-pending-blobs` moves them. A deployment
can sit in that state indefinitely, so the charm reports the pending count in
its status and exports it as a metric.
