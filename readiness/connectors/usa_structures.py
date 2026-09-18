"""FEMA / ORNL USA Structures: how many buildings a county holds, by occupancy.

Report §7 asks for exposure so a probability becomes "how many homes, schools,
hospitals", and it insists on publishing county aggregates only. USA Structures
(FEMA and Oak Ridge National Laboratory, public domain) attributes every
footprint with an occupancy class, which is what makes it the better join;
but a footprint is a point with an address, and nothing that fine may reach
this repository. So the connector never downloads footprints. It asks the
ArcGIS FeatureServer for *counts*, grouped by county FIPS, `OCC_CLS` and
`PRIM_OCC`, one paged statistics query per state, and pins the resulting
extract (`snapshots/usa_structures/<st>_counts.jsonl`) under
`fema/usa_structures/<st>`. The county is the finest key that ever exists.

Exposure is a join, never a covariate. The `source()` here is a static feature
source that declares the layer's edit year as `derived_through`, so
`readiness.harness.features.admit` refuses it under every current contract: a
building inventory maintained through 2024 has seen the holdout years, and the
count of hospitals in a county is not a forecast input in any case. The
refusal is the point, and the backtest report shows it as an INADMISSIBLE row.

What is confirmed offline: paging arithmetic, sorting, hashing, the schema
check. What is not: the layer URL and its field vocabulary, which is why the
URL is a constant every function lets the caller override and the occupancy
mapping (`readiness.exposure.occupancy`) is marked for confirmation on the
first real pull.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import urllib.parse
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from readiness.connectors.base import (
    DEFAULT_SESSION,
    ConnectorError,
    Manifest,
    Session,
    SourceRecord,
    sha256_bytes,
    utc_now,
)

#: The public USA Structures hosted feature layer on the FEMA GeoPlatform.
#: UNCONFIRMED_OFFLINE: this is the best-known endpoint; the first real pull
#: resolves it (a moved layer is the continuity risk of report §7) and every
#: function takes a `layer_url` override so a mirror can slot in.
LAYER_URL = (
    "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
    "USA_Structures_View/FeatureServer/0"
)
LICENSE = "US Government work, public domain (FEMA / ORNL USA Structures)"
SOURCE = "FEMA / ORNL USA Structures, county counts by occupancy"
KEY_PREFIX = "fema/usa_structures/"
ATTRIBUTION = (
    "Exposure: FEMA / Oak Ridge National Laboratory USA Structures (public domain), "
    "county counts only"
)

#: Records per statistics page. ArcGIS caps a page at the layer's
#: `maxRecordCount` (commonly 1,000 or 2,000); asking for more is harmless,
#: because paging stops on the server's `exceededTransferLimit`, not on a
#: page that came back as full as we asked for (see `_more`).
PAGE = 2000

#: The grouped fields, and the count field the query asks the server to name.
GROUP_FIELDS = ("FIPS", "OCC_CLS", "PRIM_OCC")
COUNT_FIELD = "n"
_LINE_KEYS = ("fips", "occ_cls", "prim_occ", "n")


def manifest_key(state_fips: str) -> str:
    return f"{KEY_PREFIX}{state_fips}"


def extract_path(snapshot_dir: pathlib.Path, state_fips: str) -> pathlib.Path:
    return snapshot_dir / "usa_structures" / f"{state_fips}_counts.jsonl"


def _state(state: str) -> str:
    """A two-digit state FIPS, or an error: this connector keys by FIPS."""
    st = str(state).strip()
    if not (st.isdigit() and len(st) == 2):
        raise ConnectorError(f"USA Structures wants a two-digit state FIPS, got {state!r}")
    return st


def counts_query(
    state_fips: str, offset: int = 0, page: int = PAGE, *, layer_url: str = LAYER_URL
) -> str:
    """The statistics query for one page of one state's county x occupancy counts.

    A `where` on the FIPS prefix, a `groupBy` on the three fields, and one
    `count(OBJECTID)` statistic: the server aggregates, and no footprint
    crosses the wire.
    """
    st = _state(state_fips)
    stats = [
        {
            "statisticType": "count",
            "onStatisticField": "OBJECTID",
            "outStatisticFieldName": COUNT_FIELD,
        }
    ]
    params = {
        "where": f"FIPS LIKE '{st}%'",
        "groupByFieldsForStatistics": ",".join(GROUP_FIELDS),
        "outStatistics": json.dumps(stats, separators=(",", ":")),
        "orderByFields": ",".join(GROUP_FIELDS),
        "returnGeometry": "false",
        "resultOffset": offset,
        "resultRecordCount": page,
        "f": "json",
    }
    return f"{layer_url.rstrip('/')}/query?{urllib.parse.urlencode(params)}"


def metadata_url(*, layer_url: str = LAYER_URL) -> str:
    return f"{layer_url.rstrip('/')}?f=json"


def _get_json(session: Session, url: str) -> dict:
    body = session.get(url)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ConnectorError(f"USA Structures returned non-JSON for {url}: {exc}") from None
    if not isinstance(payload, dict):
        raise ConnectorError(f"USA Structures returned a non-object for {url}")
    if "error" in payload:
        raise ConnectorError(f"USA Structures error for {url}: {payload['error']}")
    return payload


def parse_page(payload: Mapping) -> list[dict]:
    """Pure: one statistics response -> extract rows, or precisely what moved.

    Every feature must carry the three group fields and the count; a layer
    whose vocabulary has changed is refused rather than silently emptied.
    FIPS is zero-padded so the join to the Census universe is by value — and
    refused outright above five digits: a ten-digit tract id is not a county
    with a lost leading zero, it is a key finer than anything this repository
    may hold, and `int()` would have quietly turned it into one.
    """
    features = payload.get("features")
    if not isinstance(features, list):
        raise ConnectorError("USA Structures schema drift: response has no 'features'")
    rows = []
    for feature in features:
        attrs = feature.get("attributes") if isinstance(feature, Mapping) else None
        if not isinstance(attrs, Mapping):
            raise ConnectorError("USA Structures schema drift: feature has no attributes")
        missing = [k for k in (*GROUP_FIELDS, COUNT_FIELD) if k not in attrs]
        if missing:
            raise ConnectorError(
                f"USA Structures schema drift: attributes lack {missing}. A data-steward "
                "review is required before this layer can be used."
            )
        raw = str(attrs["FIPS"] or "").strip()
        if not raw.isdigit():
            raise ConnectorError(f"USA Structures FIPS is not numeric: {attrs['FIPS']!r}")
        if not 1 <= len(raw) <= 5:
            raise ConnectorError(
                f"USA Structures FIPS {raw!r} is {len(raw)} digits; a county code is "
                "five. Nothing finer than a county may enter this extract, so a "
                "tract or block id is refused rather than truncated."
            )
        rows.append(
            {
                "fips": f"{int(raw):05d}",
                "occ_cls": str(attrs["OCC_CLS"] or "").strip(),
                "prim_occ": str(attrs["PRIM_OCC"] or "").strip(),
                "n": int(attrs[COUNT_FIELD] or 0),
            }
        )
    return rows


def _more(payload: Mapping, got: int) -> bool:
    """Whether another page follows: the server says so, and this page moved.

    The server's own `exceededTransferLimit` is the signal; the row count is
    only a guard against an infinite loop on an empty page. A layer whose
    `maxRecordCount` is below the page we asked for answers a short page *and*
    sets the flag, so requiring a full page here would have stopped every such
    pull after page one and pinned a fraction of a state as if it were all of it.
    """
    return got > 0 and bool(payload.get("exceededTransferLimit", False))


def fetch_counts(
    state_fips: str,
    *,
    layer_url: str = LAYER_URL,
    session: Session | None = None,
    page: int = PAGE,
) -> tuple[list[dict], int]:
    """Every (county, occupancy) count for a state, and how many pages it took."""
    session = session or DEFAULT_SESSION
    rows: list[dict] = []
    offset, pages = 0, 0
    while True:
        payload = _get_json(session, counts_query(state_fips, offset, page, layer_url=layer_url))
        got = parse_page(payload)
        rows.extend(got)
        pages += 1
        if not _more(payload, len(got)):
            break
        offset += len(got)
    return sort_rows(rows), pages


def sort_rows(rows: Iterable[Mapping]) -> list[dict]:
    """Rows in (fips, occ_cls, prim_occ) order: the extract is content-addressed."""
    return sorted(
        ({k: r[k] for k in _LINE_KEYS} for r in rows),
        key=lambda r: (r["fips"], r["occ_cls"], r["prim_occ"]),
    )


def layer_vintage(
    *, layer_url: str = LAYER_URL, session: Session | None = None
) -> tuple[int, str]:
    """The layer's `lastEditDate` year, and a note saying where it came from.

    The harness trusts a static layer's `derived_through` year; this is the
    most honest value the service offers. When the metadata endpoint is
    unavailable the snapshot year stands in, and the note says so, because
    a later year only makes the firewall stricter.
    """
    session = session or DEFAULT_SESSION
    try:
        meta = _get_json(session, metadata_url(layer_url=layer_url))
        stamp = (meta.get("editingInfo") or {}).get("lastEditDate")
        if stamp is None:
            raise ConnectorError("layer metadata carries no editingInfo.lastEditDate")
        when = _dt.datetime.fromtimestamp(int(stamp) / 1000, _dt.timezone.utc)
        return when.year, f"lastEditDate {when.date().isoformat()}"
    except (ConnectorError, ValueError, OSError, TypeError) as exc:
        year = _dt.datetime.now(_dt.timezone.utc).year
        return year, f"lastEditDate unavailable ({exc}); snapshot year used"


def dumps_line(row: Mapping) -> str:
    return json.dumps({k: row[k] for k in _LINE_KEYS}, separators=(",", ":")) + "\n"


def _loads(text: str, where: str) -> dict:
    try:
        line = json.loads(text)
    except ValueError as exc:
        raise ConnectorError(f"{where}: not JSON: {exc}") from None
    missing = [k for k in _LINE_KEYS if k not in line]
    if missing:
        raise ConnectorError(f"{where}: extract line lacks {missing}")
    if not (isinstance(line["n"], int) and line["n"] >= 0):
        raise ConnectorError(f"{where}: count is not a non-negative integer")
    return {k: line[k] for k in _LINE_KEYS}


def read_extract(data: bytes, where: str = "extract") -> list[dict]:
    """Extract bytes -> rows. Takes bytes, so the caller decides what is pinned."""
    return [
        _loads(text, f"{where}:{n}")
        for n, text in enumerate(data.decode("utf-8").splitlines(), start=1)
        if text.strip()
    ]


def vintage_of(record: SourceRecord) -> int:
    """The `derived_through` year a pinned record declares, or an error."""
    for token in (record.notes or "").replace(";", " ").split():
        if token.startswith("derived_through="):
            year = token[len("derived_through="):]
            if year.isdigit() and len(year) == 4:
                return int(year)
    raise ConnectorError(f"record {record.url} declares no derived_through year")


def _pinned(path: pathlib.Path, record: SourceRecord | None) -> bool:
    return (
        record is not None
        and path.exists()
        and sha256_bytes(path.read_bytes()) == record.sha256
    )


def snapshot(
    states: Sequence[str],
    snapshot_dir: pathlib.Path,
    manifest: Manifest,
    *,
    layer_url: str = LAYER_URL,
    refresh: bool = False,
    progress: Callable[[str], None] = lambda _msg: None,
    session: Session | None = None,
    page: int = PAGE,
) -> list[pathlib.Path]:
    """Pull, write and pin one counts extract per state. Returns their paths.

    A state whose extract is already pinned and intact is skipped unless
    `refresh` is set. The layer's vintage is read once per call and written
    into every record's notes as `derived_through=<year>`, beside the resolved
    layer URL and the page count, so a reader of the manifest can tell where
    and when the counts came from without re-running anything.
    """
    session = session or DEFAULT_SESSION
    paths: list[pathlib.Path] = []
    vintage: tuple[int, str] | None = None
    for state in states:
        st = _state(state)
        path, key = extract_path(snapshot_dir, st), manifest_key(st)
        if not refresh and _pinned(path, manifest.records.get(key)):
            progress(f"usa-structures: {st} pinned")
            paths.append(path)
            continue
        if vintage is None:
            vintage = layer_vintage(layer_url=layer_url, session=session)
        rows, pages = fetch_counts(st, layer_url=layer_url, session=session, page=page)
        if not rows:
            raise ConnectorError(f"USA Structures returned no counts for state {st}")
        blob = "".join(dumps_line(r) for r in rows).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        year, how = vintage
        counties = len({r["fips"] for r in rows})
        manifest.add(
            key,
            SourceRecord(
                source=SOURCE,
                url=counts_query(st, layer_url=layer_url),
                sha256=sha256_bytes(blob),
                bytes=len(blob),
                fetched_at=utc_now(),
                license=LICENSE,
                notes=(
                    f"derived_through={year}; {how}; layer {layer_url}; "
                    f"{pages} page(s); {counties} counties, {len(rows)} groups"
                ),
            ),
        )
        progress(f"usa-structures: {st} {counties} counties in {pages} page(s)")
        paths.append(path)
    return paths


class CountyTable(Protocol):
    """What `source` reads: the exposure package's table, duck-typed.

    The exposure package reads this module's extracts; importing it back here
    would be a cycle, and the feature source needs only totals and a year.
    """

    vintage: int
    source_keys: tuple[str, ...]

    def totals(self) -> Mapping[str, Mapping[str, float]]: ...


class UsaStructuresSource:
    """Static counts per county, dated to the layer's edit year.

    Exists to be refused: `admit()` rejects a static layer whose year is not
    before the first validate year, and a maintained inventory never is.
    """

    name = "usa_structures"
    kind = "static"
    global_coverage = False

    def __init__(
        self,
        totals: Mapping[str, Mapping[str, float]],
        derived_through: int,
        manifest_keys: Sequence[str],
    ) -> None:
        self._totals = {k: dict(v) for k, v in totals.items()}
        self.derived_through = derived_through
        self.manifest_keys = tuple(sorted(set(manifest_keys)))

    def series(self, region: str, variable: str) -> None:
        return None

    def static(self, region: str) -> dict[str, float] | None:
        row = self._totals.get(region)
        return dict(row) if row is not None else None


def source(table: CountyTable) -> UsaStructuresSource:
    return UsaStructuresSource(table.totals(), table.vintage, table.source_keys)
