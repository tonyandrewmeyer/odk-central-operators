#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for pyxform, the XLSForm to XForm conversion service for ODK Central."""

from __future__ import annotations

import logging

import ops

logger = logging.getLogger(__name__)

WORKLOAD_CONTAINER = "pyxform"


class PyxformCharm(ops.CharmBase):
    """Run the pyxform HTTP conversion service and advertise it to ODK Central."""

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)
        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)

    def _on_install(self, event: ops.InstallEvent) -> None:
        """Handle the install event."""
        self._reconcile()

    def _on_config_changed(self, event: ops.ConfigChangedEvent) -> None:
        """Handle a configuration change."""
        self._reconcile()

    def _on_upgrade_charm(self, event: ops.UpgradeCharmEvent) -> None:
        """Handle a charm upgrade."""
        self._reconcile()

    def _reconcile(self) -> None:
        """Bring the workload into line with the charm's configuration.

        Every hook routes through here rather than each handler doing its own
        partial update, so that the charm's behaviour does not depend on which
        event happened to arrive.
        """
        self.unit.status = ops.WaitingStatus("waiting for relations")


if __name__ == "__main__":  # pragma: no cover
    ops.main(PyxformCharm)
