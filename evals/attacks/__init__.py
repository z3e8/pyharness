"""The adversarial suite.

Each module here attacks one claim the project makes about itself. The split is
by *what is being defended*, not by which module implements the defense, so an
attack stays where it belongs when the implementation moves.
"""

from __future__ import annotations

from ..scoreboard import Attack
from . import approval, egress

ATTACKS: list[Attack] = [
    *egress.ATTACKS,
    *approval.ATTACKS,
]


def by_surface() -> dict[str, list[Attack]]:
    surfaces: dict[str, list[Attack]] = {}
    for attack in ATTACKS:
        surfaces.setdefault(attack.surface, []).append(attack)
    return surfaces
