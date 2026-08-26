#!/usr/bin/env python3
"""Keep ``docs/configuration.md`` in step with the charms' ``charmcraft.yaml``.

Each charm owns a generated block in ``docs/configuration.md`` delimited by::

    <!-- BEGIN GENERATED CONFIG: <charm-name> -->
    <!-- END GENERATED CONFIG: <charm-name> -->

Run with no arguments to verify the blocks match (this is what CI does, and it
fails when they have drifted). Run with ``--write`` to regenerate them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DOC = REPO_ROOT / "docs" / "configuration.md"
CHARMS = ("odk-central-k8s", "enketo-k8s", "pyxform-k8s")

BEGIN = "<!-- BEGIN GENERATED CONFIG: {charm} -->"
END = "<!-- END GENERATED CONFIG: {charm} -->"


def _format_default(option: dict[str, Any]) -> str:
    """Render a config option's default as a markdown table cell."""
    if "default" not in option:
        return "_(none)_"
    default = option["default"]
    if isinstance(default, bool):
        return f"`{str(default).lower()}`"
    if default == "":
        return '`""`'
    return f"`{default}`"


def _format_description(option: dict[str, Any]) -> str:
    """Collapse a config option's description into a single table cell."""
    description = str(option.get("description", "")).strip()
    # Table cells cannot contain raw newlines or unescaped pipes.
    return " ".join(description.split()).replace("|", r"\|")


def render_table(charm: str) -> str:
    """Build the markdown config table for one charm from its charmcraft.yaml."""
    charmcraft = REPO_ROOT / "charms" / charm / "charmcraft.yaml"
    if not charmcraft.exists():
        return f"_No `charmcraft.yaml` for `{charm}` yet._"

    metadata = yaml.safe_load(charmcraft.read_text()) or {}
    options: dict[str, Any] = (metadata.get("config") or {}).get("options") or {}
    if not options:
        return f"`{charm}` has no configuration options."

    rows = ["| Key | Type | Default | Description |", "|---|---|---|---|"]
    for key in sorted(options):
        option = options[key] or {}
        rows.append(
            f"| `{key}` | `{option.get('type', 'string')}` "
            f"| {_format_default(option)} | {_format_description(option)} |"
        )
    return "\n".join(rows)


def splice(text: str, charm: str, table: str) -> str:
    """Replace one charm's generated block within the configuration doc."""
    begin, end = BEGIN.format(charm=charm), END.format(charm=charm)
    try:
        head, rest = text.split(begin, 1)
        _stale, tail = rest.split(end, 1)
    except ValueError as exc:  # pragma: no cover - a doc structure error
        raise SystemExit(
            f"docs/configuration.md is missing the markers for {charm!r}. "
            f"Expected {begin!r} and {end!r}."
        ) from exc
    return f"{head}{begin}\n\n{table}\n\n{end}{tail}"


def main() -> int:
    """Verify or regenerate the generated config tables."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite docs/configuration.md instead of only checking it",
    )
    args = parser.parse_args()

    original = CONFIG_DOC.read_text()
    updated = original
    for charm in CHARMS:
        updated = splice(updated, charm, render_table(charm))

    if updated == original:
        print("docs/configuration.md is up to date.")
        return 0

    if args.write:
        CONFIG_DOC.write_text(updated)
        print("docs/configuration.md regenerated.")
        return 0

    print(
        "docs/configuration.md is out of date with the charmcraft.yaml files.\n"
        "Run: uv run python scripts/check-docs.py --write",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
