#!/usr/bin/env python3
"""Generate the minimal XLSForm fixture used by the tests.

``minimal.xlsx`` is the smallest form that exercises a real conversion: one
question and the settings sheet that gives the form an id and a title. It is
used three times — by the pyxform-k8s charm's readiness probe, by that charm's
unit tests, and by the group integration suite's form lifecycle test — so it is
generated here rather than being an opaque committed binary nobody can audit.

Run with ``--write`` to regenerate the fixtures after changing this script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from openpyxl import Workbook

REPO_ROOT = Path(__file__).resolve().parent.parent

TARGETS = (
    # Shipped inside the charm: the readiness probe converts it on every
    # reconcile to prove the workload can actually do its job.
    REPO_ROOT / "charms" / "pyxform-k8s" / "src" / "probe-form.xlsx",
    REPO_ROOT / "charms" / "pyxform-k8s" / "tests" / "data" / "minimal.xlsx",
    REPO_ROOT / "tests" / "data" / "minimal.xlsx",
)

ATTACHMENT_TARGETS = (REPO_ROOT / "tests" / "data" / "with-attachment.xlsx",)

SURVEY = [
    ["type", "name", "label"],
    ["text", "name_of_respondent", "What is your name?"],
]
SETTINGS = [
    ["form_title", "form_id", "version"],
    ["Minimal test form", "minimal_test_form", "1"],
]

# A second form with a media question, so that the integration suite can submit
# a real attachment. Attachments are what the S3 blob store is for, and the
# partially-migrated state between PostgreSQL and the bucket cannot be exercised
# without one.
ATTACHMENT_SURVEY = [
    ["type", "name", "label"],
    ["text", "name_of_respondent", "What is your name?"],
    ["image", "photo", "Take a photo"],
]
ATTACHMENT_SETTINGS = [
    ["form_title", "form_id", "version"],
    ["Attachment test form", "attachment_test_form", "1"],
]


def build(path: Path, survey_rows: list[list[str]], settings_rows: list[list[str]]) -> None:
    """Write an XLSForm workbook to ``path``."""
    workbook = Workbook()
    survey = workbook.active
    assert survey is not None
    survey.title = "survey"
    for row in survey_rows:
        survey.append(row)

    settings = workbook.create_sheet("settings")
    for row in settings_rows:
        settings.append(row)

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def main() -> int:
    """Generate the fixture files, or report that they are missing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the fixture files")
    args = parser.parse_args()

    missing = [str(t) for t in (*TARGETS, *ATTACHMENT_TARGETS) if not t.exists()]
    if not args.write:
        if missing:
            print(
                "Missing test fixtures:\n  "
                + "\n  ".join(missing)
                + "\nRun: uv run python scripts/make-fixtures.py --write",
                file=sys.stderr,
            )
            return 1
        print("Test fixtures are present.")
        return 0

    for target in TARGETS:
        build(target, SURVEY, SETTINGS)
        print(f"wrote {target.relative_to(REPO_ROOT)}")

    for target in ATTACHMENT_TARGETS:
        build(target, ATTACHMENT_SURVEY, ATTACHMENT_SETTINGS)
        print(f"wrote {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
