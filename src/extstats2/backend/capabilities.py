"""Cross-backend statistical capabilities.

v1 hard-coded PostgreSQL's three extended-statistic kinds
(``dependencies`` / ``ndistinct`` / ``mcv``). v2 lifts these into a *capability*
model: each capability expresses *what kind of column correlation the statistic
captures*, and every backend maps it to its own native object / DDL / catalog
representation.

The core algorithm (`core/`) reasons only about :class:`Capability` (via its
core name, e.g. ``"mcv"``) and an *abstract capacity level index*. Each backend
is responsible for translating those into concrete parameters
(e.g. PG ``statistics_target``, Oracle ``estimate_percent`` / ``BUCKETS``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Capability definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Capability:
    """A statistical capability a backend may support.

    Attributes
    ----------
    name:
        Core (cross-backend) capability name. One of the canonical names in
        ``CANONICAL_CAPABILITIES``. The core never sees other values.
    native_kind:
        This backend's name/identifier for the object that implements the
        capability (e.g. PG ``"dependencies"``; Oracle ``"column_group"``).
    capacity_param:
        Human-readable name of the backend parameter that controls how much
        the statistic's capacity (sampling / storage) is:
        ``"statistics_target"`` (PG) or ``"estimate_percent"`` (Oracle).
    supported:
        Whether this backend can actually build the capability. A backend may
        declare a canonical capability but mark it unsupported (e.g. Oracle
        ``dependency`` in the first cut).
    primary:
        Whether this capability is the *core* one that directly repairs
        selection cardinality q-error. Empirical evidence (v1) shows only the
        multi-column value-distribution capability (PG ``mcv`` / Oracle column
        group histogram) directly fixes selection cardinality; the others
        (dependency/ndistinct) are secondary. By default, measurement probes
        only ``primary`` capabilities.
    """

    name: str
    native_kind: str
    capacity_param: str
    supported: bool = True
    primary: bool = False

    def __str__(self) -> str:
        flag = "" if self.supported else " (unsupported)"
        mark = "*" if self.primary else ""
        return f"{mark}{self.name}<{self.native_kind}/{self.capacity_param}>{flag}"


# Canonical cross-backend capability names (the only ones core understands).
CANONICAL_CAPABILITIES = ("dependency", "ndistinct", "mcv")


def default_measure_capabilities(caps: list["Capability"]) -> list[str]:
    """Return the capability names to measure by default: the `primary` ones,
    or all supported ones if none is marked primary."""
    prim = [c.name for c in caps if c.primary and c.supported]
    return prim or [c.name for c in caps if c.supported]


# ---------------------------------------------------------------------------
# Capacity model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Capacity:
    """An abstract capacity level.

    ``level`` is a *level index* into a backend's capacity ladder
    (e.g. ``(100, 1000, 10000)`` for PG ``statistics_target``). The core never
    interprets the numeric value; it only passes it through so the backend can
    map it to a native parameter.

    ``label`` is optional and used for result metadata / reporting.
    """

    level: int
    label: str = ""

    def __repr__(self) -> str:
        return f"Capacity({self.level}{',' + repr(self.label) if self.label else ''})"

    @classmethod
    def index(cls, level: int) -> "Capacity":
        return cls(level)


# A convenient "no capacity" sentinel (e.g. baseline measurement with no ext stat).
CAPACITY_NONE = Capacity(0, label="none")
