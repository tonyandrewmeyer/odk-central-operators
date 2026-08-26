# Quick start

From an empty Juju model to a submission you can read back, in about twenty
minutes — most of which is waiting for images to pull.

## What you need

- A Kubernetes cloud with a Juju 3.6 controller bootstrapped against it, and a
  load balancer so that `traefik-k8s` can get an address.
- A DNS name you can point at that address. **ODK Central will not work with a
  bare IP**: the frontend bakes its origin into the client config the browser
  reads, and Enketo's linked-server URL has to match it. For a test deployment
  a `nip.io` name against the load balancer address is fine, and that is what
  this page uses.

## Deploy

```bash
juju add-model odk
```

The three charms of the group:

```bash
juju deploy odk-central-k8s --channel latest/edge --trust
juju deploy enketo-k8s      --channel latest/edge
juju deploy pyxform-k8s     --channel latest/edge
```

Then the things they depend on. **Use PostgreSQL 14**: ODK Central targets it,
and its image cannot back up a newer server — see
[Day-2 operations](day2-ops.md#deploy-postgresql-14-not-16).

```bash
juju deploy postgresql-k8s --channel 14/stable --trust
juju deploy traefik-k8s    --channel latest/stable --trust
```

Wire them together:

```bash
juju integrate odk-central-k8s postgresql-k8s
juju integrate odk-central-k8s pyxform-k8s
juju integrate odk-central-k8s enketo-k8s
juju integrate odk-central-k8s traefik-k8s
```

## Give it a hostname

ODK Central has to be served at the root of a hostname, so traefik needs
subdomain routing rather than its default path prefixes:

```bash
juju run traefik-k8s/0 show-proxied-endpoints   # note the address, e.g. 10.43.45.0
juju config traefik-k8s \
  external_hostname=10-43-45-0.nip.io \
  routing_mode=subdomain
```

Traefik will then serve the deployment at
`http://odk-odk-central-k8s.10-43-45-0.nip.io`. In production, set
`external-hostname` on the charm to the name your users actually type; it takes
precedence over whatever the ingress advertises.

```bash
juju config odk-central-k8s external-hostname=odk.example.com
```

## Wait

```bash
juju status --relations --watch 5s
```

You are looking for all five applications `active/idle`. Some intermediate
states are expected and not errors:

| Message | Meaning |
|---|---|
| `waiting for the postgresql relation` | The database has not finished provisioning |
| `api ready; waiting for enketo (web forms will not render)` | Central is up; Enketo has not answered yet |
| `waiting for the odk-enketo relation to odk-central-k8s` | Enketo is waiting for the shared secrets |

Central deliberately does not wait for Enketo. Enketo cannot start until
Central has published the secrets, so waiting for each other would deadlock the
group; Central starts with a placeholder and re-renders when Enketo answers.

An abbreviated view of a settled deployment:

```
App              Version  Status  Scale  Charm            Channel      Rev
enketo-k8s                active      1  enketo-k8s       latest/edge    1
odk-central-k8s           active      1  odk-central-k8s  latest/edge    1
postgresql-k8s   14.15    active      1  postgresql-k8s   14/stable    925
pyxform-k8s               active      1  pyxform-k8s      latest/edge    1
traefik-k8s               active      1  traefik-k8s      latest/stable 377

Integration provider          Requirer                     Interface
odk-central-k8s:odk-enketo    enketo-k8s:odk-enketo        odk-enketo
postgresql-k8s:database       odk-central-k8s:postgresql   postgresql_client
pyxform-k8s:xlsform           odk-central-k8s:xlsform      odk-xlsform
traefik-k8s:ingress           odk-central-k8s:ingress      ingress
```

## Create the administrator

The address is a Juju secret rather than a configuration option, so it does not
show up in `juju config`:

```bash
SECRET_ID=$(juju add-secret odk-admin-email email=ops-team@example.com)
juju grant-secret "$SECRET_ID" odk-central-k8s
juju config odk-central-k8s admin-email-secret="$SECRET_ID"

juju run odk-central-k8s/0 create-admin
```

The password comes back **once**:

```yaml
email: ops-team@example.com
password: IaAvwRjvhur8qWzOnhEVsH6S
note: This password is shown once. Store it now; it cannot be retrieved again.
```

Store it now. If you lose it, `juju run odk-central-k8s/0 reset-password
email=ops-team@example.com` issues a new one.

## Log in

Open `http://odk-odk-central-k8s.10-43-45-0.nip.io` and sign in.

## A form and a submission

You can do all of this in the web interface. Here it is through the API, so you
can see which charm each step goes through.

```bash
BASE=http://odk-odk-central-k8s.10-43-45-0.nip.io
TOKEN=$(curl -s -X POST "$BASE/v1/sessions" -H 'Content-Type: application/json' \
  -d '{"email":"ops-team@example.com","password":"<the password>"}' | jq -r .token)
AUTH="Authorization: Bearer $TOKEN"
```

Create a project:

```bash
PID=$(curl -s -X POST "$BASE/v1/projects" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"name":"Field work"}' | jq -r .id)
```

Upload a form. **This goes through `pyxform-k8s`** — the spreadsheet is
converted to an XForm before Central will take it:

```bash
curl -s -X POST "$BASE/v1/projects/$PID/forms?ignoreWarnings=true&publish=true" \
  -H "$AUTH" \
  -H 'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' \
  -H 'X-XlsForm-FormId-Fallback: my_form' \
  --data-binary @my-form.xlsx | jq '{xmlFormId, enketoId}'
```

```json
{ "xmlFormId": "my_form", "enketoId": "MLCgKpRnccA5htMjpN5d8qYOgPMixXk" }
```

**The `enketoId` is the interesting part.** It only appears if Central
successfully authenticated to `enketo-k8s` using the secret it generated and
shared over the relation. A null id there means the two ends disagree about the
shared key.

Submit an instance:

```bash
curl -s -X POST "$BASE/v1/projects/$PID/forms/my_form/submissions" \
  -H "$AUTH" -H 'Content-Type: application/xml' --data-binary @submission.xml
```

And read it back out of the OData feed:

```bash
curl -s "$BASE/v1/projects/$PID/forms/my_form.svc/Submissions" -H "$AUTH" \
  | jq '.value[0]'
```

## What to add next

Everything above is a working deployment, but not a production-shaped one.

| Add | Why | How |
|---|---|---|
| S3 | Keeps submission attachments out of the database | [Relations](relations.md#s3) |
| SMTP | Account creation and password reset email | [Relations](relations.md#smtp) |
| COS | Metrics, logs, dashboards and alerts | [Observability](observability.md) |
| Backups | The database is the only copy of your data | [Day-2 operations](day2-ops.md#backup-and-restore) |
