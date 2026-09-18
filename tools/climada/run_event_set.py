#!/usr/bin/env python3
"""Produce a pinned CLIMADA layer for one contract. A stub; the seam is the file.

This script is NOT part of the `readiness` package and is never imported by
it. CLIMADA (ETH Zurich) is GPL-3.0 and viral across a linked work, and the
package is Apache-2.0 and must run in a browser sandbox with no third-party
code, so the integration is kept at arm's length (DATA-LICENSES.md, "GPL-3.0
(CLIMADA)"): a separate process, with the `climada` extra installed, writes a
file, and `readiness.connectors.climada_layer` reads and pins that file.

How the layer is produced (outside the sandbox, `pip install climada`):

1. Build the hazard event set for the contract's hazard and scope with a
   fixed seed and a fixed year range of input tracks/gauges/reanalysis,
   e.g. `TropCyclone.from_tracks(TCTracks.from_ibtracs_netcdf(year_range=(a, b)))`
   or `RiverFlood` from the GloFAS/ISIMIP inputs for the same years.
2. Compute the local return-period intensities at the centroids of the
   scope's counties (`Hazard.local_exceedance_intensity([10, 50, 100])`),
   one row per county FIPS.
3. Write `snapshots/climada/<hazard>_<scope_key>.jsonl`:

       {"event_set_years": [a, b], "seed": 20260101, "climada_version": "5.0.0",
        "hazard": "inland_flood"}
       {"region": "22001", "rp10": 0.8, "rp50": 1.6, "rp100": 2.1}
       ...

`event_set_years[1]` becomes the layer's `derived_through` year, and the
harness refuses the layer for any contract whose validate split starts at or
before it. Build the set from inputs that end before the first validate year
(2015 for the current contracts) or the layer is inadmissible by design; a
set built through 2020 is refused, not silently used. The values in the file
are the layer's own output and carry CC BY 4.0; the tool that made them is
GPL-3.0, which is why it lives here and not in the package.
"""

import sys

USAGE = (
    "usage: tools/climada/run_event_set.py --hazard HAZARD --scope SCOPE_KEY "
    "--years A B --seed N --out snapshots/climada/\n"
    "This is a documented stub: install the climada extra outside the sandbox "
    "and follow the module docstring to produce the layer file."
)


def main(argv: list[str]) -> int:
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
