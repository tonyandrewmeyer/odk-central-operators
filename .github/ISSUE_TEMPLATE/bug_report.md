---
name: Bug report
about: Something in one of the ODK Central charms is not working
title: ''
labels: bug
assignees: ''
---

## Which charm?

<!-- odk-central-k8s, enketo-k8s, pyxform-k8s, or "the group" if it is a
     cross-charm problem such as secrets not reaching Enketo. -->

## Versions

| | |
|---|---|
| Charm revision / channel | |
| ODK Central release deployed | |
| Enketo image tag | |
| pyxform image tag | |
| Juju version | |
| Kubernetes flavour and version | <!-- Canonical Kubernetes (k8s) or microk8s --> |

## What happened

## What you expected to happen

## Steps to reproduce

1.
2.
3.

## Output

<details>
<summary><code>juju status --relations</code></summary>

```
paste here
```

</details>

<details>
<summary>Charm logs (<code>juju debug-log --replay --include &lt;unit&gt;</code>)</summary>

```
paste here
```

</details>

<!-- Please redact secrets. Note that the Enketo API key appears in Central's
     rendered config.json and in Enketo's, so redact those files if you attach
     them. -->
