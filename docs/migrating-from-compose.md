# Migrating from Docker Compose

Moving an existing upstream `getodk/central` Docker Compose deployment onto
these charms. Read all of it before starting: two of these steps are much
easier to do before the charms have generated their own state than after.

!!! warning "Take a backup first"

    ```bash
    docker compose exec postgres14 pg_dump -U odk -d odk --format=custom \
      > central-$(date +%F).dump
    docker compose exec secrets tar -cf - /etc/secrets > secrets-$(date +%F).tar
    ```

    The second one matters as much as the first, for reasons the
    [Enketo secrets](#hand-over-the-enketo-secrets) section explains.

## What maps onto what

| Compose service | Becomes |
|---|---|
| `service`, `nginx` | `odk-central-k8s` |
| `enketo`, `enketo_redis_main`, `enketo_redis_cache` | `enketo-k8s` |
| `pyxform` | `pyxform-k8s` |
| `postgres14` | `postgresql-k8s --channel 14/stable` |
| `mail` | `smtp-integrator`, or your own relay |
| `secrets` | nothing — replaced by Juju secrets |
| `postgres` (the 9.6→14 upgrade helper) | **nothing. See below** |

Your `.env` file maps onto charm configuration:

| `.env` | Charm configuration |
|---|---|
| `DOMAIN` | `external-hostname` on `odk-central-k8s` |
| `SYSADMIN_EMAIL` | `sysadmin-email` |
| `EMAIL_FROM` | `email-from` |
| `SSL_TYPE`, `HTTP_PORT`, `HTTPS_PORT`, `CERTBOT_EMAIL` | nothing — TLS is `traefik-k8s`'s job |
| `DB_HOST`, `PGHOST` and friends | nothing — the `postgresql` relation |
| `EMAIL_HOST` and friends | nothing — the `smtp` relation |
| `OIDC_*` | `oidc-*` options and a Juju secret |
| `SENTRY_*` | `error-reporting-dsn`, and see below |
| `S3_*` | nothing — the `s3` relation |

### If you never changed `SENTRY_*`

Then your compose deployment has been reporting its errors to the ODK project's
own Sentry, because those are the values upstream ships as defaults. The charms
never inherit them: leave `error-reporting-dsn` empty and nothing is reported
anywhere, or set your own DSN.

## There is no 9.6 upgrade path

Upstream's `postgres` service exists only to upgrade a PostgreSQL 9.6 volume in
place to 14. It has no charm equivalent and there is no plan for one.

If your deployment still runs 9.6, **complete that upgrade in Compose first**,
using upstream's own tooling, and migrate afterwards. Attempting to move a 9.6
dump into `postgresql-k8s` directly is not a supported path.

```bash
docker compose exec postgres14 psql -U odk -d odk -c 'select version()'
```

## Move the database

Deploy the group as in the [quick start](quickstart.md), but do not create an
administrator: your existing users are coming with the data.

Take the dump from Compose, and restore it into the charm's database. The
easiest route is the charm's own `restore` action, which handles the details
that make a naive `pg_restore` fail — in particular that the relation user does
not own the extensions the database charm installs:

```bash
# Upload the dump to the bucket the charm can read.
aws s3 cp central-2026-08-26.dump s3://odk-central/migration/central.dump

juju integrate odk-central-k8s s3-integrator
juju run odk-central-k8s/leader restore source=migration force=true
```

If you would rather not go through S3, the same thing by hand:

```bash
juju scp central.dump odk-central-k8s/0:/tmp/central.dump --container service
juju ssh --container service odk-central-k8s/0
# then, inside, with PG* set from /usr/odk/config/local.json:
pg_restore --list /tmp/central.dump | grep -v ' EXTENSION ' > /tmp/toc
pg_restore --clean --if-exists --no-owner --no-privileges \
  --dbname "$PGDATABASE" --use-list /tmp/toc /tmp/central.dump
```

Then run migrations, because the charm's release may be newer than the one the
dump came from:

```bash
juju run odk-central-k8s/leader run-migrations
```

## Hand over the Enketo secrets

**This is the step people miss, and the one that is painful to undo.**

`odk-central-k8s` generates three shared secrets on first install. If you let it
do that, every web form link your respondents already have stops working: the
links are derived from Enketo's encryption key, and a new key means new ids.

To keep them, give the charm the values from your Compose deployment
*before* the group first settles. From the `secrets` volume:

```bash
docker compose exec secrets cat /etc/secrets/enketo-api-key      # 128 bytes
docker compose exec secrets cat /etc/secrets/enketo-secret       #  64 bytes
docker compose exec secrets cat /etc/secrets/enketo-less-secret  #  32 bytes
```

Check the sizes before you use them — `wc -c` should print exactly 128, 64 and
32, with no trailing newline:

```bash
docker compose exec secrets sh -c 'for f in /etc/secrets/*; do printf "%s " "$f"; wc -c < "$f"; done'
```

Then add each as a Juju secret with the label the charm looks for:

```bash
juju add-secret odk-enketo-api-key      value=<the 128-byte value>
juju add-secret odk-enketo-secret       value=<the 64-byte value>
juju add-secret odk-enketo-less-secret  value=<the 32-byte value>

for s in odk-enketo-api-key odk-enketo-secret odk-enketo-less-secret; do
  juju grant-secret "$s" odk-central-k8s
done
```

The charm only generates a secret it cannot already find by label, so it will
adopt these. If you have already let it generate its own, the honest answer is
that existing web form links are gone; the forms and all their submitted data
are unaffected, but you will need to reissue public links.

## Move the attachments

If your Compose deployment already used S3, point the charm at the same bucket
and nothing needs to move — the blob rows in the dump reference objects that are
already there.

If it did not, the attachments are inside the dump, in PostgreSQL, and they will
stay there until you ask for them to be moved:

```bash
juju integrate odk-central-k8s s3-integrator
juju run odk-central-k8s/leader upload-pending-blobs
```

Watch `odk_central_blobs_pending` reach zero. On a large deployment this takes a
while and can be run repeatedly.

## What you gain, and what you give up

Gained: rolling refreshes, secrets that are not files on a disk, the whole
observability stack over three relations, and a database with its own backup
and failover story.

Given up, honestly:

- **Let's Encrypt inside the deployment.** `SSL_TYPE=letsencrypt` is gone; TLS
  is `traefik-k8s`'s job now.
- **Enketo's ODK branding.** Upstream builds its own Enketo image; the charm
  uses the stock one, which bakes Enketo's own client-side defaults at image
  build time.
- **`/v1/backup`.** Only if you deploy PostgreSQL 16 — see
  [Day-2 operations](day2-ops.md#deploy-postgresql-14-not-16). On 14 it works
  as before.

## Check it worked

```bash
juju status --relations
```

All five applications active, then:

1. Log in as an existing user with their existing password.
2. Open a project and confirm the submission counts match what Compose showed.
3. Open an existing form's public link — this is what tells you the Enketo
   secrets came across.
4. Submit a test response and read it back through the OData feed.
