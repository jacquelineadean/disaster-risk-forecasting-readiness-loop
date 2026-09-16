"""The exposure table: one row per county, and nothing finer by construction.

Report §7: "publish county aggregates only". `CountyExposure` has exactly six
fields — the county, its total, the counts by declared class, the share the
mapping did not recognise, the layer vintage and the manifest key it came
from. There is no field in which a tract, a block, a parcel, a point or an
address could be carried, and `tests/test_exposure.py` pins that field list
so one cannot be added without saying so.

The table is built from pinned extracts only. `load` refuses an extract whose
bytes no longer hash to its manifest record, for the same reason every
connector does: data of unknown provenance reproduces nothing.
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from readiness.connectors import usa_structures
from readiness.connectors.base import Manifest, pinned_bytes
from readiness.exposure.occupancy import UNCLASSIFIED, class_names, classify


class ExposureError(ValueError):
    """A table that cannot be built honestly: unpinned, missing or inconsistent."""


@dataclass(frozen=True)
class CountyExposure:
    """What one county holds. The county is the finest key; there is no other."""

    fips: str
    total: int
    by_class: dict[str, int]
    unclassified_share: float
    vintage: int
    source_key: str

    def to_dict(self) -> dict:
        return {
            "fips": self.fips,
            "total": self.total,
            "by_class": dict(self.by_class),
            "unclassified_share": self.unclassified_share,
            "vintage": self.vintage,
            "source_key": self.source_key,
        }


def _county(fips: str, counts: Mapping[str, int], vintage: int, key: str) -> CountyExposure:
    by_class = {name: int(counts.get(name, 0)) for name in class_names()}
    total = sum(by_class.values())
    share = by_class[UNCLASSIFIED] / total if total else 0.0
    return CountyExposure(fips, total, by_class, share, vintage, key)


@dataclass
class ExposureTable:
    """County rows keyed by FIPS, from one or more pinned state extracts."""

    rows: dict[str, CountyExposure] = field(default_factory=dict)

    @classmethod
    def from_counts(
        cls, rows: Iterable[Mapping], vintage: int, source_key: str
    ) -> "ExposureTable":
        """Roll extract rows `{fips, occ_cls, prim_occ, n}` up to counties.

        Every row lands in exactly one class; what the mapping does not know
        goes to `unclassified` and still counts toward the total.
        """
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            name = classify(row.get("occ_cls"), row.get("prim_occ"))
            per = counts.setdefault(str(row["fips"]), {})
            per[name] = per.get(name, 0) + int(row["n"])
        return cls(
            {fips: _county(fips, per, vintage, source_key) for fips, per in sorted(counts.items())}
        )

    @classmethod
    def load(
        cls, snapshot_dir: pathlib.Path, manifest: Manifest, states: Sequence[str]
    ) -> "ExposureTable":
        """The table for `states` (two-digit FIPS) from their pinned extracts.

        An extract with no manifest record, or whose bytes do not match it,
        is refused: the manifest is what pins an experiment, and a table
        built from unpinned bytes could not be reproduced by anyone else.
        """
        table = cls()
        for state in states:
            key = usa_structures.manifest_key(state)
            record = manifest.records.get(key)
            if record is None:
                raise ExposureError(f"{key} is not pinned; run `readiness exposure snapshot`")
            path = usa_structures.extract_path(snapshot_dir, state)
            data = pinned_bytes(path, record, allow_fetch=False)
            rows = usa_structures.read_extract(data, str(path))
            part = cls.from_counts(rows, usa_structures.vintage_of(record), key)
            table.merge(part)
        return table

    def merge(self, other: "ExposureTable") -> None:
        clash = sorted(set(self.rows) & set(other.rows))
        if clash:
            raise ExposureError(f"county {clash[0]} appears in more than one extract")
        self.rows.update(other.rows)

    def for_county(self, fips: str) -> CountyExposure | None:
        return self.rows.get(fips)

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def vintage(self) -> int:
        """The latest layer year in the table: the strictest one for the firewall."""
        if not self.rows:
            raise ExposureError("an empty exposure table has no vintage")
        return max(r.vintage for r in self.rows.values())

    @property
    def source_keys(self) -> tuple[str, ...]:
        return tuple(sorted({r.source_key for r in self.rows.values()}))

    def totals(self) -> dict[str, dict[str, float]]:
        """Per county: the total and every class count, as floats for a static
        source. Handed to the connector's `source()` so the firewall can refuse it."""
        return {
            fips: {"total": float(r.total), **{k: float(v) for k, v in r.by_class.items()}}
            for fips, r in self.rows.items()
        }

    def digest(self) -> str:
        """Content hash over the sorted rows, 16 hex: the table's version."""
        h = hashlib.sha256()
        for fips in sorted(self.rows):
            r = self.rows[fips]
            classes = ",".join(f"{k}={r.by_class[k]}" for k in sorted(r.by_class))
            h.update(
                f"{fips}|{r.total}|{classes}|{r.unclassified_share!r}|"
                f"{r.vintage}|{r.source_key}\n".encode()
            )
        return h.hexdigest()[:16]

    def summary(self) -> str:
        if not self.rows:
            return "exposure: no counties"
        total = sum(r.total for r in self.rows.values())
        unclassified = sum(r.by_class[UNCLASSIFIED] for r in self.rows.values())
        share = unclassified / total if total else 0.0
        vintages = sorted({r.vintage for r in self.rows.values()})
        lines = [
            f"exposure: {len(self.rows):,} counties, {total:,} structures, "
            f"{share:.1%} unclassified, vintage {'/'.join(map(str, vintages))}, "
            f"digest {self.digest()}"
        ]
        by_class: dict[str, int] = {}
        for r in self.rows.values():
            for k, v in r.by_class.items():
                by_class[k] = by_class.get(k, 0) + v
        for name in class_names():
            lines.append(f"  {name:<14}{by_class.get(name, 0):>12,}")
        return "\n".join(lines)
