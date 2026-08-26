# Day-2 operations

## Secrets

Every credential the charm group handles is a Juju secret rather than a
configuration option, so nothing sensitive appears in
`juju config <charm> --format=json`.

`odk-central-k8s` generates three secrets on first install and keeps them for
the life of the application:

| Label | Bytes | Purpose |
|---|---|---|
| `odk-enketo-api-key` | 128 | Authenticates Central to Enketo, and appears in Central's own `enketo.apiKey` |
| `odk-enketo-secret` | 64 | Enketo's encryption key |
| `odk-enketo-less-secret` | 32 | Enketo's "less secure" encryption key |

**The lengths are load-bearing.** Upstream's `start-enketo.sh` stats the three
files under `/etc/secrets/` and refuses to start on any other size, so a stray
trailing newline turns a 64-byte secret into a 65-byte file and Enketo aborts
with a message that does not obviously point at the cause. The charm generates
the values at exactly these lengths, re-checks them before writing anything to
a container, and writes them without a trailing newline.

They are shared with `enketo-k8s` over the `odk-enketo` relation. The values
themselves never cross the relation: Central grants the Juju secrets to the
related application and publishes only their IDs.

### Rotating them

```bash
juju run odk-central-k8s/0 rotate-enketo-secrets
```

This regenerates all three, republishes them, and restarts both workloads.
**Every in-progress web form session is invalidated** — a respondent part-way
through a form will have to start again. A rotation that reaches Central but
not Enketo leaves every web form returning 403, which is very hard to diagnose
from the outside, so the integration suite guards that path explicitly.

## Redis backing

`enketo-k8s` needs two Redis instances with different persistence semantics: a
durable one holding in-flight form and instance state, and an ephemeral one
caching transformed forms. By default both run as sidecar containers inside
`enketo-k8s`, because Charmhub has no Redis charm in a stable channel.

The charm also declares optional `redis-main` and `redis-cache` relations. When
either is related, the corresponding sidecar is stopped, Enketo's config is
pointed at the relation endpoint, and the unit status names which instances are
relation-backed.

### What happened when we tried the edge charm

Tested against `redis-k8s --channel latest/edge`, **revision 42**, on Canonical
Kubernetes 1.32 with Juju 3.6.27:

- It deploys and reaches `active` without intervention. (The revision is newer
  than the 2022-era revision 23 that this charm group was scoped against, so
  the situation is better than expected — but the channel is still `edge`.)
- `juju integrate enketo-k8s:redis-cache redis-k8s` works. The relation carries
  `hostname` and `port` in the unit databag, which is what
  `charms.redis_k8s.v0.redis` reads.
- The instance accepts unauthenticated connections from the Enketo pod, so no
  password handling was needed.
- With the relation in place, Enketo serves normally and the in-charm
  `redis-cache` sidecar is stopped.
- Removing the relation puts the sidecar back and Enketo returns to using it.

One caveat found while testing, now fixed in the charm: Enketo reads its
`config.json` once, at startup, and Pebble's `replan` only restarts a service
whose *layer* changed. Pointing the cache at a new endpoint changes a file, not
the layer, so without an explicit restart the workload kept talking to the
sidecar that had just been stopped and every request returned 502. The same
applies to the Redis sidecars' own config files.

**Recommendation.** The in-charm default is still the right one for most
deployments: it is one less application to operate, and the durable instance
gets Juju storage without any extra wiring. Relate an external Redis when you
already run one, or when you need the cache to survive an `enketo-k8s` refresh.
Either way, `latest/edge` is not a channel to depend on for production.

### `redis-main` is the group's only fragile state

Everything else in this charm group lives in PostgreSQL or, once the `s3`
relation exists, the blob store. `redis-main` is the exception: it holds
Enketo's in-flight form and instance state, which is to say submissions that
respondents have started but not yet sent. Losing it loses those. It is backed
by a `filesystem` storage of at least 1G, mounted at `/data`, with upstream's
persistence settings (`save 300 1`, `appendonly no`,
`dbfilename enketo-main.rdb`).

The cache instance is deliberately ephemeral and has no storage. Losing it
costs one re-transformation per form.

## Diagnosing Redis

```bash
juju run enketo-k8s/0 redis-info
juju run enketo-k8s/0 flush-cache
```

`flush-cache` only ever touches the cache instance. It refuses to run when the
cache and durable endpoints resolve to the same place, and when the cache is
relation-backed it tells you to use the providing charm instead.

## Backup and restore

```bash
juju run odk-central-k8s/0 backup destination=nightly
juju run odk-central-k8s/0 restore source=nightly/20260826T083747Z force=true
```

`backup` takes a custom-format `pg_dump` of the Central database inside the
service container and uploads it to `<destination>/<timestamp>/central.dump` in
the bucket from the `s3` relation. `restore` reverses that: it stops the API and
nginx, downloads the dump, restores it, re-runs migrations and brings Central
back up.

`restore` refuses to run against a database that already has tables unless you
pass `force=true`, and it refuses if it cannot check — a check that fails is not
evidence that the database is empty.

### Deploy PostgreSQL 14, not 16

ODK Central targets PostgreSQL 14: upstream's compose file runs `postgres14`,
and the published `central-service` image ships `postgresql-client-14`.

`pg_dump` refuses to dump a server newer than itself. On
`postgresql-k8s --channel 16/stable` the application runs perfectly well — the
Node client does not care about the server version — but every `pg_dump`-based
feature stops working:

```
pg_dump: error: server version: 16.14; pg_dump version: 14.24
pg_dump: error: aborting because of server version mismatch
```

That takes out this charm's `backup` and `restore` actions **and ODK Central's
own `/v1/backup` endpoint**, which shells out to the same binary. The charm
detects this specific failure and says so rather than reporting a raw
`pg_dump` error.

So: **deploy `postgresql-k8s --channel 14/stable`.** If you are already on a
16-series database, take backups with the database charm instead:

```bash
juju run postgresql-k8s/leader create-backup
```

### What is not in a backup

The dump is the database only.

- **Submission attachments already moved to S3 are not included.** Once the
  `s3` relation exists, new blobs go to the bucket and the dump no longer
  contains them. The bucket needs its own versioning or lifecycle policy.
- Blobs still in PostgreSQL — because `upload-pending-blobs` has not moved them
  yet — *are* in the dump. A deployment can sit in this partially-migrated state
  indefinitely, so which of the two applies depends on when you look.
- `redis-main` is not included. See above: it holds Enketo's in-flight form
  state, and losing it loses submissions that respondents have started but not
  sent.

A complete disaster-recovery posture for this group is therefore three things:
a Juju model backup, the database dump, and versioning on the S3 bucket.

### Restoring into a charm-managed database

`pg_restore --clean` emits a `DROP` for every object in the archive, extensions
included. The database charm installs extensions such as `pgaudit` as a
superuser, and the relation user does not own them, so a naive restore fails on
an object the application never created:

```
pg_restore: error: could not execute query: ERROR:  must be owner of extension pgaudit
```

The charm filters every `EXTENSION` entry out of the archive's table of
contents before restoring, which drops both the `DROP` and the `CREATE` and
leaves the extensions already in the database untouched.
