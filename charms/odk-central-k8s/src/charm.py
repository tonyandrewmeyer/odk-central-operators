#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for ODK Central: the API service and the nginx frontend proxy."""

from __future__ import annotations

import logging

import ops

logger = logging.getLogger(__name__)

SERVICE_CONTAINER = "service"
NGINX_CONTAINER = "nginx"


class OdkCentralCharm(ops.CharmBase):
    """Run the ODK Central API and frontend, and anchor the three-charm group."""

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
        """Bring the workloads into line with configuration and relation data.

        Every hook routes through here rather than each handler doing its own
        partial update. That matters more in this charm than in most: its
        configuration depends on two relations that settle in either order, so
        per-relation handlers would each see a different partial view.
        """
        self.unit.status = ops.WaitingStatus("waiting for relations")


if __name__ == "__main__":  # pragma: no cover
    ops.main(OdkCentralCharm)
