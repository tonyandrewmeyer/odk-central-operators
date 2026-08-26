## What does this change?

<!-- One or two sentences. Link the issue if there is one. -->

Fixes #

## Which charms are affected?

- [ ] `odk-central-k8s`
- [ ] `enketo-k8s`
- [ ] `pyxform-k8s`
- [ ] Shared libraries (`odk_enketo`, `xlsform`)
- [ ] Docs / CI only

## Checklist

- [ ] The commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/)
- [ ] Unit tests cover the change
- [ ] `uv run pre-commit run --all-files` passes
- [ ] `CHANGELOG.md` updated under `[Unreleased]` (or the change is trivial)
- [ ] Docs updated if config, relations or actions changed
- [ ] If `charmcraft.yaml` config changed, `uv run python scripts/check-docs.py` passes

## Integration evidence

<!-- Required if this touches relations, Pebble layers, actions or the
     rendered workload config. Attach `juju status --relations` from a model
     where the change is deployed, and say what you exercised. -->

<details>
<summary><code>juju status --relations</code></summary>

```
paste here
```

</details>
