"""Sim source interface. The rest of the app only talks to this."""

from __future__ import annotations

from typing import Protocol

from atc.models import OwnState, Traffic


class SimSource(Protocol):
    def own(self) -> OwnState:
        """Current state of the player's aircraft."""

    def traffic(self, center_lat: float, center_lon: float, radius_nm: float) -> list[Traffic]:
        """Other aircraft within radius_nm of a point (normally the airport)."""

    def close(self) -> None: ...
