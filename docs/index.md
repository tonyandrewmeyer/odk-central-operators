# ODK Central charms

Juju charms for [ODK Central](https://getodk.org/), an open-source platform for
collecting, managing and using survey and field data.

ODK Central is the server half of the ODK ecosystem. Forms authored as XLSForm
spreadsheets are converted to XForms, filled in offline on a mobile client or in
a browser, and submitted back to Central, which manages projects, form versions,
submissions, attachments and users.

## Why three charms?

Upstream ships Central as an eight-container Docker Compose stack. This
repository deploys it as three Juju applications, because the pieces have
genuinely different lifecycles:

- **`odk-central-k8s`** runs the API (`service`) and nginx serving the built
  frontend (`nginx`). These two are released together upstream and are
  version-locked to the same image tag — the frontend is baked into the nginx
  image at build time — so they belong in one application.
- **`enketo-k8s`** runs [Enketo](https://github.com/enketo/enketo), which
  renders XForms as web forms. It is a separate upstream project on its own
  release cadence, and it has its own state, so it scales and upgrades
  independently.
- **`pyxform-k8s`** runs the stateless XLSForm-to-XForm converter. Separating it
  makes the conversion service independently scalable and replaceable.

Upstream's one-shot `secrets` container has no charm equivalent. It writes three
files to a shared Docker volume, and Juju has no shared volume between
applications. Instead `odk-central-k8s` generates those three values itself,
stores them as application-owned Juju secrets, and distributes them to
`enketo-k8s` over the `odk-enketo` relation.

## Bootstrap ordering

```text
pyxform-k8s ──(xlsform)──▶ odk-central-k8s ──(odk-enketo)──▶ enketo-k8s
                                  ▲                                │
                                  └──── enketo URL sent back ──────┘
```

This is a genuine two-way bootstrap. Central cannot render a complete config
until it knows both pyxform's address and Enketo's URL — but Enketo cannot start
until Central has published the three shared secrets. Central therefore starts
with a placeholder Enketo URL and re-renders once Enketo answers, rather than
blocking on it, which would deadlock the group.

[Architecture](architecture.md) has the full detail.

## Where to go next

- [Quick start](quickstart.md) — an empty model to a first submission.
- [Relations](relations.md) — what to deploy alongside these charms, and why.
- [Day-2 operations](day2-ops.md) — backup, restore, upgrades, secret rotation.
- [Observability](observability.md) — COS-Lite wiring and shipped dashboards.
