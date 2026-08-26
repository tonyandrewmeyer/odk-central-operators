# Relations

What to deploy alongside the charm group, why, and what happens when each
relation is missing or broken.

## The group's own relations

These two are what make the three charms one deployment. Neither has a
published charm library, so both are implemented in this repository.

### `odk-enketo`

`odk-central-k8s` **provides**, `enketo-k8s` **requires**. One relation only.

```bash
juju integrate odk-central-k8s enketo-k8s
```

Replaces upstream's one-shot `secrets` container, which writes three files to a
Docker volume that both services mount — something Juju has no equivalent of.
Central generates the values, stores them as application-owned Juju secrets,
grants them to the related application and publishes their IDs; the values
themselves never cross the relation. Enketo answers with the URL Central should
use to reach it.

| Databag key | Direction | Contents |
|---|---|---|
| `api-key-secret-id` | → Enketo | ID of the 128-byte shared key |
| `encryption-key-secret-id` | → Enketo | ID of the 64-byte encryption key |
| `less-secure-key-secret-id` | → Enketo | ID of the 32-byte key |
| `base-url` | → Enketo | The deployment's public URL |
| `support-email` | → Enketo | From `sysadmin-email` |
| `enketo-url` | → Central | Enketo's in-cluster URL, including `/-` |

**Without it:** Enketo blocks with `waiting for the odk-enketo relation to
odk-central-k8s` and does not start — serving with no credentials is worse than
not serving. Central stays active with a placeholder `enketo.url` and says
`waiting for enketo (web forms will not render)`. Forms can still be published
and submitted from the mobile client; only browser-based forms are unavailable.

### `odk-xlsform`

`pyxform-k8s` **provides**, `odk-central-k8s` **requires**. One relation only.

```bash
juju integrate odk-central-k8s pyxform-k8s
```

`pyxform-k8s` advertises the Kubernetes Service address Juju creates for it,
rather than a unit address, so the endpoint survives scaling and rescheduling.
It withdraws the address if its own conversion probe stops passing, so Central
stops sending work to a converter that cannot do it.

**Without it:** Central runs, and everything except publishing a *new* form
works. Status says `no xlsform relation, form publishing will fail`. This is
worth knowing because nothing else looks wrong: existing forms keep working and
submissions keep arriving.

`pyxform-k8s` has no ingress endpoint on purpose. It is reached only by
Central, in-cluster, and should not be exposed.

## PostgreSQL

```bash
juju deploy postgresql-k8s --channel 14/stable --trust
juju integrate odk-central-k8s postgresql-k8s
```

**Deploy the 14 series.** ODK Central targets PostgreSQL 14 — upstream's compose
file runs `postgres14` and the image ships `postgresql-client-14`. On a 16-series
database the application runs perfectly well, but `pg_dump` refuses to dump a
newer server, which takes out this charm's `backup` and `restore` actions *and*
ODK Central's own `/v1/backup` endpoint. See
[Day-2 operations](day2-ops.md#deploy-postgresql-14-not-16).

**Without it:** Central cannot start. `waiting for the postgresql relation`.

## Ingress

```bash
juju deploy traefik-k8s --channel latest/stable --trust
juju integrate odk-central-k8s traefik-k8s
```

The charm requests ingress for **nginx on port 80**, not the API on 8383:
Central's frontend and its API have to be same-origin. TLS terminates at the
ingress, and the workload runs with `SSL_TYPE=upstream` so that nginx serves
plain HTTP and trusts `X-Forwarded-Proto`. Certbot is never enabled inside the
charm.

Traefik's default path-based routing will not work — ODK Central has to be at
the root of a hostname:

```bash
juju config traefik-k8s external_hostname=example.com routing_mode=subdomain
```

`external-hostname` on the charm takes precedence over whatever the ingress
advertises, for when your users type a different name than traefik knows about.

**Without it:** Central is reachable only in-cluster. Set `external-hostname`
anyway, or the links Central generates in email and in Enketo will be wrong.

## S3

```bash
juju deploy s3-integrator --channel latest/stable
juju config s3-integrator endpoint=https://s3.example.com bucket=odk-central \
  region=us-east-1 path=/
juju run s3-integrator/leader sync-s3-credentials access-key=... secret-key=...
juju integrate odk-central-k8s s3-integrator
```

Submission attachments and form media go to the bucket instead of into
PostgreSQL rows.

**The migration is not automatic.** New blobs go to S3 as soon as the relation
exists; blobs already in the database stay there until you move them:

```bash
juju run odk-central-k8s/leader upload-pending-blobs
```

Until then the deployment is in a partially migrated state, which the charm
reports as `N blobs pending upload to s3` and exports as
`odk_central_blobs_pending`.

**Without it:** attachments live in PostgreSQL. That works, and it means they
are inside your database backups, but the database grows with every photo.

For testing, `hack/minio-test-deploy.yaml` stands up a MinIO to point
`s3-integrator` at.

## SMTP

```bash
juju deploy smtp-integrator --channel latest/stable
juju config smtp-integrator host=smtp.example.com port=587 auth_type=plain \
  transport_security=starttls
juju integrate odk-central-k8s:smtp smtp-integrator:smtp
```

Note the explicit endpoint names: `smtp-integrator` offers both `smtp` and
`smtp-legacy`, so an unqualified `juju integrate` is ambiguous.

Central sends mail only on account creation, password reset and project
invitation. A passive check will not tell you whether it works; trigger one:

```bash
curl -X POST "$BASE/v1/users/reset/initiate" \
  -H 'Content-Type: application/json' -d '{"email":"someone@example.com"}'
```

**Without it:** Central still reaches active, and only those three operations
fail — which matches upstream's behaviour. The status says
`no smtp relation, account email will not be sent`.

For testing, `hack/mailpit-test-deploy.yaml` stands up a mail sink with a web
interface.

## OpenID Connect

Either from a provider charm:

```bash
juju integrate odk-central-k8s:oauth hydra
```

or from configuration, for a provider with no charm:

```bash
SECRET_ID=$(juju add-secret odk-oidc client-secret=...)
juju grant-secret "$SECRET_ID" odk-central-k8s
juju config odk-central-k8s \
  oidc-enabled=true \
  oidc-issuer-url=https://idp.example.com \
  oidc-client-id=odk-central \
  oidc-client-secret="$SECRET_ID"
```

Relation data wins when both are present, and the status says which is in
effect. Your provider's discovery document must advertise `openid` and `email`
in `scopes_supported` and `email` and `email_verified` in `claims_supported`;
Central refuses providers that do not.

**Enabling OIDC turns off password authentication entirely.** `POST
/v1/sessions` stops existing, and `create-admin`, `promote-user` and
`reset-password` refuse to run. Setting `oidc-enabled` without a usable provider
blocks rather than advertising a login that cannot work. See
[Day-2 operations](day2-ops.md) — in practice this is a one-way door.

## Redis

Optional. Both Redis instances run as sidecars inside `enketo-k8s` by default,
because Charmhub has no Redis charm in a stable channel.

```bash
juju integrate enketo-k8s:redis-cache redis-k8s
juju integrate enketo-k8s:redis-main redis-k8s
```

When either is related the corresponding sidecar is stopped and the status
names which instances are relation-backed. See
[Day-2 operations](day2-ops.md#redis-backing) for what happened when we tested
this against `redis-k8s` from `latest/edge`.

## Observability

```bash
juju integrate odk-central-k8s:logging loki-k8s
juju integrate odk-central-k8s:metrics-endpoint prometheus-k8s
juju integrate odk-central-k8s:grafana-dashboard grafana-k8s
juju integrate odk-central-k8s:tracing tempo-coordinator-k8s
```

All three charms provide `metrics-endpoint` and `grafana-dashboard` and require
`logging`; only `odk-central-k8s` has `tracing`. See
[Observability](observability.md), including why the tracing relation carries
the charm's own spans and not the workload's.

## Summary

| Endpoint | Charm | Role | Required? | Status when missing |
|---|---|---|---|---|
| `postgresql` | central | requires | **yes** | `waiting for the postgresql relation` |
| `odk-enketo` | central / enketo | provides / requires | for web forms | `waiting for enketo` / Enketo blocked |
| `xlsform` | central | requires | to publish forms | `no xlsform relation` |
| `ingress` | central | requires | for public access | reachable in-cluster only |
| `s3` | central | requires | no | attachments stay in PostgreSQL |
| `smtp` | central | requires | no | `account email will not be sent` |
| `oauth` | central | requires | no | password authentication is used |
| `redis-main` / `redis-cache` | enketo | requires | no | in-charm sidecars are used |
| `logging` | all three | requires | no | logs stay in Pebble |
| `metrics-endpoint` | all three | provides | no | no metrics collected |
| `grafana-dashboard` | central, enketo | provides | no | no dashboards |
| `tracing` | central | requires | no | no charm traces |
