# Configuration

Every configuration option of the three charms. **These tables are generated
from the charms' `charmcraft.yaml` files** by `scripts/check-docs.py`, and CI
fails if they drift, so edit `charmcraft.yaml` rather than this page.

No credential is ever a configuration option. Passwords, API keys, client
secrets and DSNs are all Juju secrets — see
[Secrets](day2-ops.md#secrets) — so that nothing sensitive appears in
`juju config <charm> --format=json`.

Two options are of type `secret`: they hold a Juju secret **URI**, not a value.

```bash
SECRET_ID=$(juju add-secret odk-admin-email email=ops-team@example.com)
juju grant-secret "$SECRET_ID" odk-central-k8s
juju config odk-central-k8s admin-email-secret="$SECRET_ID"
```

## Options worth reading twice

Most of these are self-explanatory. These four are not.

**`external-hostname`** takes precedence over the hostname the `ingress`
relation supplies, for when your users type a different name than traefik knows
about. ODK Central will not work with a bare IP: the frontend bakes its origin
into the client configuration the browser reads at page load, and Enketo's
linked-server URL has to match.

**`oidc-enabled`** is effectively a one-way door. Turning it on disables
password authentication entirely — `POST /v1/sessions` stops existing and every
existing password user is locked out. Turning it back off does not restore
their sessions. Setting it without a usable provider blocks rather than
advertising a login that cannot work.

**`error-reporting-dsn`** is empty by default, and that default is
load-bearing. Upstream's shipped compose configuration contains the ODK
project's own Sentry organisation, key and project id, so a deployment that
inherited them would report its errors to ODK's telemetry. These charms never
do; leaving this empty blanks every Sentry value and replaces the frontend's
report endpoint with a local `204`.

**`conversion-timeout`** on `pyxform-k8s` defaults to 60 seconds, where the
published image's own default is 600. The lower value surfaces a wedged
converter quickly, but large forms with many translations legitimately take
tens of seconds — raise it if publishing a real form starts timing out.

## `odk-central-k8s`

<!-- BEGIN GENERATED CONFIG: odk-central-k8s -->

| Key | Type | Default | Description |
|---|---|---|---|
| `admin-email-secret` | `secret` | _(none)_ | A Juju user secret holding the initial administrator's email address under the key "email". Consumed by the create-admin action. This is a secret rather than a config option so that the address does not appear in `juju config`. |
| `db-pool-size` | `int` | `10` | Maximum PostgreSQL connections per worker. Central runs several workers, so the total connection count is a multiple of this; check it against the database's own connection limit before raising it. |
| `email-from` | `string` | `""` | Envelope From address for outbound mail. Defaults to no-reply@<external-hostname> when empty. |
| `error-reporting-dsn` | `string` | `""` | Your own Sentry DSN for workload error reporting. Empty — the default — blanks every Sentry value and removes the frontend's CSP report endpoint. This matters: upstream's shipped compose defaults contain the ODK project's own Sentry organisation and project key, so a deployment that inherits them reports its errors to ODK's telemetry. This charm never inherits them. |
| `external-hostname` | `string` | `""` | The public fully-qualified domain name this deployment is reached at. Overrides the hostname supplied by the ingress relation when set. ODK Central will not work with a bare IP address: the frontend bakes the origin into its client config, and Enketo's linked-server URL must match it. |
| `frontend-analytics-enabled` | `boolean` | `false` | Whether Central offers administrators the upstream usage-reporting prompt. This is opt-in: enabling it only makes the prompt available, and an administrator still has to agree. |
| `log-level` | `string` | `INFO` | Workload log level. One of DEBUG, INFO, WARN or ERROR. |
| `oidc-client-id` | `string` | `""` | OIDC client ID. This is a public value, not a credential. Ignored when the oauth relation is present. |
| `oidc-client-secret` | `secret` | _(none)_ | A Juju user secret holding the OIDC client secret under the key "client-secret". Only needed when oidc-enabled is true and there is no oauth relation to supply it. |
| `oidc-enabled` | `boolean` | `false` | Enable OpenID Connect authentication. This is mutually exclusive with password authentication: with OIDC enabled, HTTP Basic auth and POST /v1/session stop working entirely, and the create-admin, promote-user and reset-password actions refuse to run. Turning this on for a running deployment locks out every existing password user, so in practice it is a one-way door. |
| `oidc-issuer-url` | `string` | `""` | OIDC issuer URL. Ignored when the oauth relation is present, which supplies it. |
| `s3-bucket-name` | `string` | `""` | Override the bucket name supplied by the s3 relation. Leave empty to use whatever the relation provides. |
| `s3-upload-interval-minutes` | `int` | `15` | How often the charm moves submission attachments still held in PostgreSQL to the S3 blob store. Has no effect without an s3 relation. |
| `session-lifetime` | `int` | `86400` | Web session lifetime, in seconds. |
| `sysadmin-email` | `string` | `""` | Address shown to users as the system contact, and forwarded to Enketo as its support address. This is not the administrator account — that is created by the create-admin action from a Juju secret. |

<!-- END GENERATED CONFIG: odk-central-k8s -->

## `enketo-k8s`

<!-- BEGIN GENERATED CONFIG: enketo-k8s -->

| Key | Type | Default | Description |
|---|---|---|---|
| `exclude-non-relevant` | `boolean` | `true` | Omit values for questions hidden by relevance logic instead of submitting them. Changing this changes what ends up in your data. |
| `log-level` | `string` | `INFO` | Workload log level. One of DEBUG, INFO, WARN or ERROR. |
| `offline-enabled` | `boolean` | `true` | Allow offline-capable web forms, which install a service worker so a respondent can keep filling in a form after losing connectivity. |
| `payload-limit` | `string` | `1mb` | Maximum submission payload Enketo will accept, as a size string such as "1mb" or "10mb". Raise it for forms that collect large attachments. |
| `redis-cache-maxmemory` | `string` | `256mb` | `maxmemory` for the in-charm cache Redis instance. Eviction here is harmless: cached forms are simply re-transformed. Ignored when the redis-cache relation is in use. |
| `redis-main-maxmemory` | `string` | `""` | Optional `maxmemory` for the in-charm durable Redis instance, as a size string such as "512mb". Empty means unlimited, which is upstream's behaviour. Setting a limit on the durable instance risks evicting in-flight form state; prefer growing the storage instead. Ignored when the redis-main relation is in use. |
| `text-field-character-limit` | `int` | `1000000` | Maximum characters accepted in a single text field. Lower it for constrained deployments. |

<!-- END GENERATED CONFIG: enketo-k8s -->

## `pyxform-k8s`

<!-- BEGIN GENERATED CONFIG: pyxform-k8s -->

| Key | Type | Default | Description |
|---|---|---|---|
| `conversion-timeout` | `int` | `60` | Seconds before an in-flight XLSForm conversion is abandoned. Large forms with many translations legitimately take tens of seconds; raise this if form publishing in Central times out. |
| `log-level` | `string` | `INFO` | Workload log level. One of DEBUG, INFO, WARN or ERROR. |
| `workers` | `int` | `2` | Number of HTTP worker processes. Conversion is CPU-bound, so there is little benefit in setting this above the CPU limit of the container. |

<!-- END GENERATED CONFIG: pyxform-k8s -->
