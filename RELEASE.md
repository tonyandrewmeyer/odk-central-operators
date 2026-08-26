# Releasing

## The group constraint

**The three charms are released together.** This is not a convention, it is a
correctness requirement, and it is the way this charm group is most likely to be
broken.

- `odk-central-k8s` runs two containers, `service` and `nginx`, from images that
  upstream builds and releases as a pair. The web frontend is compiled into the
  nginx image at build time, so a `central-nginx` from one release in front of a
  `central-service` from another is a frontend calling an API it was not built
  against. **These two image tags must always match.**
- `odk-central-k8s` and `enketo-k8s` share three secrets over a relation, and
  the shape of that exchange is defined by a charm library that lives in this
  repository and is copied into both charms. A revision of one charm promoted
  without the matching revision of the other is running against a contract that
  may have changed.
- `pyxform-k8s` is the loosest coupling of the three — it advertises a host and
  a port — but its conversion output has to be a form the deployed Central
  release accepts.

So: promote all three, or promote none.

## Versioning

Releases are cut by [release-please](https://github.com/googleapis/release-please)
from [Conventional Commits](https://www.conventionalcommits.org/). One version
covers the whole repository; there is no per-charm version.

The version tracks *the charms*, not ODK Central. ODK Central uses date-based
versions (`2026.2.4`) which would need a new Charmhub track every quarter, so
the charms use the `latest` track and record the upstream release they target in
`charmcraft.yaml` as the `upstream-source` of each image resource.

## Cutting a release

1. Merge work to `main` with conventional commit messages. `release-please`
   keeps a release pull request up to date with the resulting changelog.
2. Merge the release pull request. That tags the repository.
3. The tag triggers `.github/workflows/charmhub-upload.yml`, which packs all
   three charms and uploads them. **It uploads only; it does not release to a
   channel.**
4. Check the revisions:

   ```bash
   for charm in odk-central-k8s enketo-k8s pyxform-k8s; do
     charmcraft status "$charm"
   done
   ```

5. Deploy those exact revisions into a clean model and run the integration
   suite against them before promoting anything.

## Promoting

Manual, on purpose, and all three at once:

```bash
charmcraft release odk-central-k8s --revision=<n> --channel=latest/edge \
  --resource=service-image:<n> --resource=nginx-image:<n>
charmcraft release enketo-k8s --revision=<n> --channel=latest/edge \
  --resource=enketo-image:<n> --resource=redis-image:<n>
charmcraft release pyxform-k8s --revision=<n> --channel=latest/edge \
  --resource=pyxform-image:<n>
```

The promotion path is `latest/edge` → `latest/candidate` → `latest/stable`.
Nothing reaches `candidate` without the integration suite passing against the
exact revisions, and nothing reaches `stable` without having been run somewhere
real.

### Resource revisions matter as much as charm revisions

A charm revision on Charmhub is released *with* specific OCI resource
revisions. Releasing `odk-central-k8s` with mismatched `service-image` and
`nginx-image` revisions produces exactly the frontend-against-wrong-API failure
described above, and it will not be obvious: the deployment comes up active and
only misbehaves in the browser.

## Bumping the ODK Central release

1. Refresh the upstream reference files first and read the diff:

   ```bash
   base=https://raw.githubusercontent.com/getodk/central/<new-tag>
   # refresh hack/upstream/ from that tag
   ```

   Upstream's files are authoritative. The charms make a small number of
   deliberate changes to them — the nginx proxy targets, the Redis `dir`, the
   server-name hash size — and those need re-applying to the new versions
   rather than the old files being kept.

2. Update `upstream-source` for **both** `service-image` and `nginx-image` to
   the same new tag.
3. Check what Enketo and pyxform versions the new release expects, in
   `enketo.dockerfile` and `docker-compose.yml` at that tag, and update those
   resources too.
4. Run the integration suite. The form lifecycle test is the one that matters:
   it exercises all three charms and the shared secret.
5. Commit as `feat(charms): target ODK Central <version>`.

## First release

Before the first upload, the three names have to exist on Charmhub. This cannot
be automated:

1. Register each at <https://charmhub.io/register-charm>:
   `odk-central-k8s`, `enketo-k8s`, `pyxform-k8s`.
2. Create a credential with the permissions the upload needs:

   ```bash
   charmcraft login --export=charmhub-token.txt \
     --charm=odk-central-k8s --charm=enketo-k8s --charm=pyxform-k8s \
     --permission=package-manage-revisions \
     --permission=package-manage-releases \
     --ttl=7776000
   ```

3. Add its contents to the repository as the `CHARMHUB_TOKEN` secret.
