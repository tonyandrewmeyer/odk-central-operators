# Build manifest

Recorded at the start of the build so later phases have a single source of
truth for the decisions taken at Phase 0. Answers marked *(verified)* were
checked against upstream rather than taken on trust.

## Answers

| | Question | Answer |
|---|---|---|
| Q1 | GitHub namespace | `tonyandrewmeyer` — repository `odk-central-operators`. **Working locally**: no remote is configured yet, so each phase commits to local `main` and pushes once a remote exists. |
| Q2 | Juju k8s controller | Provisioned by `concierge prepare -p dev`, which installs **Canonical Kubernetes** (`k8s` snap, `1.32-classic/stable`) — the preferred substrate. |
| Q3 | ODK Central release | **`v2026.2.4`** *(verified: latest tag on `getodk/central`, and both `ghcr.io/getodk/central-service` and `ghcr.io/getodk/central-nginx` publish it)*. Pins **both** service and nginx; they must never be mismatched. |
| Q4 | Enketo / pyxform | **Enketo `7.6.1`**, **pyxform `v4.5.0`** *(verified against `enketo.dockerfile` and `docker-compose.yml` at the `v2026.2.4` tag — upstream agrees with the recommended defaults)*. Redis is **`8.6.4`** *(verified)*. |
| Q5 | Redis backing | **in-charm** — two sidecar containers in `enketo-k8s`; `redis-main` gets Juju `filesystem` storage. Optional `redis-main` / `redis-cache` relations are still declared and take precedence when present. Phase 6 probes `redis-k8s latest/edge` once and records the outcome. |
| Q6 | Public domain | **Auto `nip.io`** — derived from the `traefik-k8s` LoadBalancer address once it is allocated, e.g. `odk.10-0-0-5.nip.io`. ODK Central will not work with a bare IP. |
| Q7 | Initial admin email | `ops-team@example.com`, stored as the `odk-admin-email` Juju secret. |
| Q8 | Day-one auth | **password**. OIDC is still wired and tested in Phase 8. |
| Q9 | Charmhub names / track | `odk-central-k8s`, `enketo-k8s`, `pyxform-k8s` on the **`latest`** track; `latest/edge` → `latest/candidate` promotion path. |
| Q10 | Error reporting | **disabled** — every `SENTRY_*` value is blanked unless an operator sets `error-reporting-dsn`. |

## Tooling on the build host

| Tool | Version |
|---|---|
| `charmcraft` | 4.4.1 |
| `uv` | 0.12.3 |
| `python3` | 3.12.3 |
| `git` | 2.43.0 |
| `ops` | 3.x (3.8.1 at time of writing) |
| `jubilant` | 1.x (1.12.0 at time of writing) |
| Kubernetes | Canonical Kubernetes 1.32 (via `concierge`) |

## Verified upstream findings that changed the design

These were discovered while reading `hack/upstream/` and are the reason several
phases deviate from a naive reading of the component map.

1. **Upstream builds its own Enketo image.** `enketo.dockerfile` layers
   `start-enketo.sh` and `config.json.template` onto the stock
   `ghcr.io/enketo/enketo:7.6.1`. The stock image the charm consumes has
   neither, so `enketo-k8s` supplies the config and start command itself.
   The 64 / 32 / 128-byte assertions live in `start-enketo.sh`, which the charm
   does not run — the sizes are still honoured, for fidelity and so that the
   files are usable if upstream's script is ever reinstated.

2. **`start-odk.sh` does four things, not one.** It renders `local.json` from
   the template, waits for PostgreSQL, runs migrations, starts `cron`, and only
   then execs `pm2-runtime`. The charm bypasses it and drives config rendering,
   migrations and the server as separate steps, which is what lets migrations be
   a checkable one-shot Pebble service.

3. **nginx hardcodes its upstream hostnames.** `odk.conf.template` contains
   `proxy_pass http://service:8383` and `proxy_pass http://enketo:8005`. The
   first is fine as a same-pod localhost address, but `enketo` is a separate
   Juju application and that name will never resolve. The charm pushes its own
   templates into `/usr/share/odk/nginx/`, exactly as upstream's compose file
   bind-mounts them.

4. **Blanking Sentry naively breaks nginx.** `odk.conf.template` has
   `proxy_pass https://${SENTRY_ORG_SUBDOMAIN}.ingest.sentry.io/api/${SENTRY_PROJECT}/security/?sentry_key=${SENTRY_KEY};`.
   Empty values render as `https://.ingest.sentry.io/api//security/?sentry_key=`,
   which nginx will not load. The `/csp-report` block has to be **removed** when
   no DSN is configured, not merely emptied.

5. **Upstream's shipped Sentry defaults are ODK's own project** —
   `SENTRY_ORG_SUBDOMAIN=o130137`, `SENTRY_KEY=3cf75f54983e473da6bd07daddf0d2ee`,
   `SENTRY_PROJECT=1298632`. Confirmed present in `docker-compose.yml` at
   `v2026.2.4`. A naive deploy reports errors to the ODK project. Q10 is not
   theoretical.

6. **Central reads its database from libpq environment variables.**
   `config.json.template` leaves `database.host` / `.user` / `.password` /
   `.database` as empty strings, and compose supplies `PGHOST`, `PGUSER`,
   `PGPASSWORD`, `PGDATABASE`, `PGAPPNAME` instead. Which of the two paths the
   charm uses is settled empirically in Phase 4.
