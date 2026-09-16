"""Exposure: what a county holds, so a probability can say what is at risk.

Report §7: "join to USA Structures so outputs become 'how many homes, schools,
hospitals'", and "publish county aggregates only". This package is the second
half of that sentence made structural. It reads the pinned county counts the
USA Structures connector wrote, rolls them up by a declared occupancy mapping,
and offers one table type whose row has no field finer than the county.

Exposure is a join, never a covariate: nothing here touches labels, scores or
the agent plane (`tests/test_boundaries.py`), and the connector's feature
source exists only to be refused by the firewall. The spot-check against
county assessor counts is the Phase 2 exit evidence that the join is real,
with its ratio band declared in `readiness.config` and printed per row.
"""

from readiness.exposure.occupancy import CLASSES, UNCLASSIFIED, classify
from readiness.exposure.table import CountyExposure, ExposureError, ExposureTable

__all__ = [
    "CLASSES",
    "UNCLASSIFIED",
    "classify",
    "CountyExposure",
    "ExposureError",
    "ExposureTable",
]
