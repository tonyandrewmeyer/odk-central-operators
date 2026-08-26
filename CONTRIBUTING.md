# Contributing

Thanks for your interest in improving the ODK Central charms.

## Repository layout

```
charms/odk-central-k8s/   # Central API + nginx/frontend
charms/enketo-k8s/        # Enketo web forms + its two Redis instances
charms/pyxform-k8s/       # XLSForm -> XForm conversion service
hack/upstream/            # Verbatim upstream reference files, pinned to the targeted release
docs/                     # mkdocs-material site covering all three charms
tests/integration/        # Group-level jubilant tests; there are no per-charm integration tests
scripts/                  # Repository tooling (docs checks, and so on)
```

The three charms live in one repository because they are deployed, released and
version-locked as a group. See [RELEASE.md](RELEASE.md).

## Development setup

Dependencies are managed with [uv](https://docs.astral.sh/uv/), which drives a
workspace spanning the three charm directories.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --all-packages
uv run pre-commit install --install-hooks
uv run pre-commit install --hook-type commit-msg
```

## Tests

Unit tests use [`ops.testing`](https://ops.readthedocs.io/en/latest/reference/ops-testing.html).
Do not add `pytest-operator`; this repository does not use it.

```bash
uv run pytest charms/odk-central-k8s/tests/unit
uv run pytest charms/enketo-k8s/tests/unit
uv run pytest charms/pyxform-k8s/tests/unit
```

Run them one charm at a time. All three have a `src/charm.py`, so a single
invocation over the repository sees three modules named `charm` and refuses to
collect anything.

CI requires 95% branch coverage of each `src/charm.py`:

```bash
uv run pytest charms/<name>/tests/unit \
  --cov=charm --cov-branch --cov-report=term-missing --cov-fail-under=95
```

Integration tests use [`jubilant`](https://github.com/canonical/jubilant) and
exercise the whole group at once, because the interesting failures in this
charm group only happen across charm boundaries.

```bash
uv run pytest tests/integration -v
```

They need a bootstrapped Kubernetes controller and take roughly half an hour.

### The controller runs out of disk before you expect it to

Every `juju refresh --path=...` uploads the whole charm — around 20 MB for
`odk-central-k8s` — into the controller's blob store, and old revisions are not
reclaimed. A day of iterating on a charm fills a default 2 GiB controller
volume, at which point MongoDB starts crash-looping with
`Failed to write to /var/lib/juju/db/...` and every `juju` command hangs on
"waiting for pod controller-0 to become ready".

Bootstrap with more room than the default:

```bash
juju bootstrap k8s --storage-pool-size=16G
```

If you hit it, the volume can be grown in place on Canonical Kubernetes, whose
storage class does not support expansion but does keep each volume as a plain
image file:

```bash
sudo k8s kubectl scale statefulset controller -n controller-<name> --replicas=0
IMG=/var/snap/k8s/common/rawfile-storage/<pvc>/disk.img
sudo truncate -s 8G "$IMG"
sudo e2fsck -f -p "$IMG"
sudo resize2fs "$IMG"
sudo k8s kubectl scale statefulset controller -n controller-<name> --replicas=1
```

### Kubernetes flavour

The dev-box workflow targets [Canonical
Kubernetes](https://documentation.ubuntu.com/canonical-kubernetes/) (the `k8s`
snap), which is what the charms are developed against. CI runs the integration
job on **microk8s** instead, because Canonical Kubernetes is not yet available
as a packaged GitHub Actions provider. If you hit a failure that reproduces on
one and not the other, say which flavour in the issue — that difference is
worth knowing about.

## Building

```bash
cd charms/odk-central-k8s && charmcraft pack
```

`charmcraft` 4.x and `ops` 3.x are required; do not pin below those majors.

## Upstream reference files

`hack/upstream/` holds verbatim copies of the upstream `getodk/central` files
that determine how the workload starts: the compose file, the config templates,
the nginx setup script and the Redis configs. They are pinned to the release the
charms currently target.

**Upstream's files are authoritative.** When they disagree with anything in this
repository's docs or code, upstream wins and the charm is the thing that needs
fixing. When bumping the targeted release, refresh `hack/upstream/` first and
read the diff before touching charm code.

## Style

- `ruff` for linting and formatting, line length 99, target `py312`.
- `mypy --strict` on charm source. Vendored charm libraries under
  `charms/*/lib/` are excluded; they are not ours to type.
- `yamllint` and `actionlint` on YAML and workflows.

`uv run pre-commit run --all-files` runs the lot, and CI runs exactly that.

## Commits and pull requests

This repository uses [Conventional Commits](https://www.conventionalcommits.org/)
and [release-please](https://github.com/googleapis/release-please). The commit
subject determines the changelog entry and the version bump, so it matters.

```
feat(enketo): stop the redis sidecar when a redis relation is present
fix(central): blank the upstream Sentry DSN in the nginx environment
docs: explain why rotating Enketo secrets logs everyone out
```

Scopes in use: `central`, `enketo`, `pyxform`, `charms` (cross-cutting), `docs`,
`ci`.

Development happens directly on `main`. Pull requests are welcome from outside
contributors; please make sure `uv run pre-commit run --all-files` passes and
that unit tests cover the change.

## Licensing

Everything here is Apache-2.0, and — unusually for this software category — so
is everything it deploys. `getodk/central`, `central-backend`,
`central-frontend`, `pyxform` and `enketo/enketo` are all Apache-2.0, and the
published container images contain no enterprise-gated or
source-available-but-not-open code. There is no licence divergence between the
charms and the workload to document, and no feature behind a commercial licence
that these charms cannot reach.

That is a large part of why this upstream was chosen.
