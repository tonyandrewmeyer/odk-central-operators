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
