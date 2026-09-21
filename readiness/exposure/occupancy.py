"""The occupancy classes a brief speaks in, and how USA Structures maps to them.

A county brief says "holds N structures, including S schools and H hospitals"
(plan §2.5). USA Structures attributes each footprint with an occupancy class
(`OCC_CLS`, nine broad values) and a primary occupancy (`PRIM_OCC`, the finer
HAZUS-style use). `CLASSES` is the declared mapping from our class names to
those values; `classify` applies it.

CONFIRM_ON_FIRST_PULL: the values below are written from the ORNL data
dictionary as best known offline. The first real pull prints the layer's
distinct `OCC_CLS` / `PRIM_OCC` pairs so a person can confirm or amend this
table; until then it is a declared judgement, not a checked fact.

Two rules that matter for the arithmetic:

* Every structure counts once. The classes are checked in order, and the
  specific ones (`school`, `hospital`, `medical`) come before the broad ones
  they would otherwise fall into (`education`, `commercial`), so a hospital
  is a hospital, not a commercial building.
* Unknown values are never dropped. A value the mapping does not know counts
  toward the county total and toward an "unclassified" share that the table
  reports and the brief prints. A mapping that quietly discarded what it
  did not recognise would understate exposure exactly where the vocabulary
  had drifted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Rule:
    """The upstream values that map to one class: primary occupancies first,
    then whole occupancy classes."""

    prim_occ: frozenset[str]
    occ_cls: frozenset[str]


def _norm(value: str | None) -> str:
    """Case-, space- and hyphen-insensitive: 'Multi - Family' is 'Multi-Family'."""
    text = re.sub(r"\s*-\s*", "-", (value or "").strip().casefold())
    return re.sub(r"\s+", " ", text)


def _rule(prim_occ: tuple[str, ...] = (), occ_cls: tuple[str, ...] = ()) -> Rule:
    return Rule(frozenset(map(_norm, prim_occ)), frozenset(map(_norm, occ_cls)))


#: Ordered: the first matching class wins. CONFIRM_ON_FIRST_PULL (module doc).
CLASSES: dict[str, Rule] = {
    "school": _rule(prim_occ=("Grade Schools", "Schools", "School")),
    "hospital": _rule(prim_occ=("Hospital", "Hospitals")),
    "medical": _rule(prim_occ=("Medical Office/Clinic", "Medical Office", "Nursing Home")),
    "education": _rule(
        prim_occ=("Colleges/Universities", "College/University"), occ_cls=("Education",)
    ),
    "residential": _rule(occ_cls=("Residential",)),
    "commercial": _rule(occ_cls=("Commercial",)),
    "industrial": _rule(occ_cls=("Industrial",)),
    "government": _rule(
        prim_occ=("Emergency Response", "General Services"), occ_cls=("Government",)
    ),
    "agriculture": _rule(occ_cls=("Agriculture", "Agricultural")),
    "utility": _rule(occ_cls=("Utility and Misc", "Utility", "Utilities")),
    "other": _rule(occ_cls=("Assembly",)),
}


def classify(occ_cls: str | None, prim_occ: str | None) -> str:
    """Our class name for one upstream (OCC_CLS, PRIM_OCC) pair, or "unclassified"."""
    occ, prim = _norm(occ_cls), _norm(prim_occ)
    for name, rule in CLASSES.items():
        if prim and prim in rule.prim_occ:
            return name
        if occ and occ in rule.occ_cls:
            return name
    return UNCLASSIFIED


def class_names() -> tuple[str, ...]:
    """Every key a county's `by_class` carries, in declared order, plus the share
    of what the mapping did not recognise."""
    return (*CLASSES, UNCLASSIFIED)
