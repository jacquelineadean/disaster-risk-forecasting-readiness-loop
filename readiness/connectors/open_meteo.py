"""ERA5 reanalysis, monthly per county, through the Open-Meteo archive API.

Report §6 asks for "precipitation reanalysis" among the Phase 1 features. ERA5
is the reanalysis; Open-Meteo serves it point by point, daily, under CC BY 4.0,
which is why every published artefact must carry the attribution line in
`DATA-LICENSES.md`.

What is pinned is the *extract*, not the raw responses: one JSONL per state
(`snapshots/open_meteo/<state_fips>_era5_monthly.jsonl`), one line per county
holding the monthly precipitation sum and mean temperature over the whole
requested range, plus the elevation Open-Meteo reports for the point. The
national extract is about 3,100 counties x 360 months x 2 floats, roughly
25 MB, which a repository can carry; the daily responses behind it are ten
times that and are discarded unless `keep_raw` is set, in which case their
checksums (over the payload with `generationtime_ms` removed, which is timing
noise, not data) are written into the record's notes.

One request per county covers the whole year range, and the pull is
sequential on one keep-alive session: the archive API rate-limits by the
minute, so parallel workers only earn 429s. A 429 is slept through and retried.
The extract is resumable: a county already written is not fetched again unless
`refresh` is set or its line no longer covers the requested years. What is
never done is re-pinning an extract whose bytes have stopped matching the
manifest: the record is the provenance every card built on it names, so a
mismatch is an error and `refresh` is the way to say "discard it and pull
again".

The harness sees two sources from the same extract: a series source `era5`
(`precip_mm`, `tmean_c`, whose months speak for themselves) and a static
source `elevation`, timeless geometry on the harness's allow-list.
"""

from __future__ import annotations

import calendar
import json
import math
import pathlib
import time
from typing import Callable, Mapping, NamedTuple, Sequence

from readiness.connectors.base import (
    DEFAULT_SESSION,
    ConnectorError,
    Manifest,
    Session,
    SourceRecord,
    sha256_bytes,
    utc_now,
)
from readiness.harness.features import NAN, Series, month_index

API_URL = "https://archive-api.open-meteo.com/v1/archive"
LICENSE = "CC BY 4.0 (Open-Meteo; ERA5 by ECMWF/Copernicus)"
SOURCE = "Open-Meteo ERA5 archive"
KEY_PREFIX = "open-meteo/era5/"
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0); ERA5 by ECMWF/Copernicus"

#: Series variables the `era5` source serves, and the daily fields behind them.
VARIABLES = ("precip_mm", "tmean_c")
_DAILY = {"precip_mm": "precipitation_sum", "tmean_c": "temperature_2m_mean"}
_LINE_KEYS = ("id", "elevation_m", "month0", "precip_mm", "tmean_c")

#: How a 429 is waited out: the archive API's limits are per minute.
RATE_LIMIT_RETRIES = 6
RATE_LIMIT_BACKOFF_S = 10.0

Point = tuple[float, float]


class Monthly(NamedTuple):
    """One county's monthly aggregates, contiguous from `month0`."""

    month0: int
    precip_mm: list[float]
    tmean_c: list[float]
    elevation_m: float


def manifest_key(part: str) -> str:
    return f"{KEY_PREFIX}{part}"


def extract_path(snapshot_dir: pathlib.Path, part: str) -> pathlib.Path:
    return snapshot_dir / "open_meteo" / f"{part}_era5_monthly.jsonl"


def request_url(lat: float, lon: float, first_year: int, last_year: int) -> str:
    return (
        f"{API_URL}?latitude={lat:.4f}&longitude={lon:.4f}"
        f"&start_date={first_year}-01-01&end_date={last_year}-12-31"
        f"&daily={_DAILY['precip_mm']},{_DAILY['tmean_c']}&timezone=UTC"
    )


def _date(text: str) -> tuple[int, int, int]:
    try:
        y, m, d = (int(p) for p in text.split("-"))
    except ValueError:
        raise ConnectorError(f"Open-Meteo day is not YYYY-MM-DD: {text!r}") from None
    return y, m, d


def _daily(payload: dict) -> tuple[list[str], list, list, float]:
    """Pull the fields out of a response, or say precisely which one moved."""
    if not isinstance(payload, dict) or "daily" not in payload:
        raise ConnectorError("Open-Meteo schema drift: response has no 'daily' block")
    daily = payload["daily"]
    missing = [k for k in ("time", *_DAILY.values()) if k not in daily]
    if missing:
        raise ConnectorError(f"Open-Meteo schema drift: daily block lacks {missing}")
    if "elevation" not in payload:
        raise ConnectorError("Open-Meteo schema drift: response has no 'elevation'")
    time_ = daily["time"]
    precip, temp = daily[_DAILY["precip_mm"]], daily[_DAILY["tmean_c"]]
    if not (len(time_) == len(precip) == len(temp)):
        raise ConnectorError(
            f"Open-Meteo daily arrays differ in length: {len(time_)} days, "
            f"{len(precip)} precipitation, {len(temp)} temperature"
        )
    if not time_:
        raise ConnectorError("Open-Meteo response holds no days")
    return list(time_), list(precip), list(temp), float(payload["elevation"])


def parse_daily_to_monthly(payload: dict) -> Monthly:
    """Pure: daily response -> monthly precipitation sums and temperature means.

    A month is NaN when any of its calendar days is null or absent: a partial
    month is missing, never a smaller number. Months are contiguous from the
    first day's month to the last day's, so a gap in the response shows up as
    NaN months rather than as a shifted series.
    """
    days, precip, temp, elevation = _daily(payload)
    first = month_index(*_date(days[0])[:2])
    last = month_index(*_date(days[-1])[:2])
    n = last - first + 1
    seen = [0] * n
    broken = [False] * n
    psum = [0.0] * n
    tsum = [0.0] * n
    for text, p, t in zip(days, precip, temp):
        y, m, _d = _date(text)
        i = month_index(y, m) - first
        if not 0 <= i < n:
            raise ConnectorError(f"Open-Meteo days are not in order: {text!r}")
        seen[i] += 1
        if p is None or t is None:
            broken[i] = True
            continue
        psum[i] += float(p)
        tsum[i] += float(t)
    precip_mm, tmean_c = [], []
    for i in range(n):
        y, m = divmod(first + i, 12)
        complete = seen[i] == calendar.monthrange(y, m + 1)[1] and not broken[i]
        precip_mm.append(psum[i] if complete else NAN)
        tmean_c.append(tsum[i] / seen[i] if complete else NAN)
    return Monthly(first, precip_mm, tmean_c, elevation)


def _covers(line: dict, first_year: int, last_year: int) -> bool:
    start, n = line["month0"], len(line["precip_mm"])
    return start <= month_index(first_year, 1) and start + n > month_index(last_year, 12)


def _line(fips: str, monthly: Monthly) -> dict:
    return {
        "id": fips,
        "elevation_m": monthly.elevation_m,
        "month0": monthly.month0,
        "precip_mm": list(monthly.precip_mm),
        "tmean_c": list(monthly.tmean_c),
    }


def _dumps(line: dict) -> str:
    """One extract line. NaN is written as null: the file stays valid JSON."""
    out = {
        **line,
        "precip_mm": [None if math.isnan(v) else v for v in line["precip_mm"]],
        "tmean_c": [None if math.isnan(v) else v for v in line["tmean_c"]],
    }
    return json.dumps(out, separators=(",", ":")) + "\n"


def _loads(text: str, where: str) -> dict:
    try:
        line = json.loads(text)
    except ValueError as exc:
        raise ConnectorError(f"{where}: not JSON: {exc}") from None
    missing = [k for k in _LINE_KEYS if k not in line]
    if missing:
        raise ConnectorError(f"{where}: extract line lacks {missing}")
    same = len(line["precip_mm"]) == len(line["tmean_c"])
    if not same or not isinstance(line["month0"], int):
        raise ConnectorError(f"{where}: malformed extract line for {line['id']!r}")
    line["precip_mm"] = [NAN if v is None else float(v) for v in line["precip_mm"]]
    line["tmean_c"] = [NAN if v is None else float(v) for v in line["tmean_c"]]
    elevation = line["elevation_m"]
    line["elevation_m"] = NAN if elevation is None else float(elevation)
    return line


def read_extract(path: pathlib.Path) -> dict[str, dict]:
    """Every line of an extract keyed by county; the last line for an id wins."""
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for n, text in enumerate(fh, start=1):
            if text.strip():
                line = _loads(text, f"{path}:{n}")
                out[str(line["id"])] = line
    return out


def _get_json(session: Session, url: str, sleep: Callable[[float], None]) -> dict:
    """One request; a 429 from the session is slept through and retried."""
    for attempt in range(RATE_LIMIT_RETRIES):
        try:
            body = session.get(url)
        except ConnectorError as exc:
            if "HTTP 429" in str(exc) and attempt < RATE_LIMIT_RETRIES - 1:
                sleep(RATE_LIMIT_BACKOFF_S * 2**attempt)
                continue
            raise
        try:
            return json.loads(body)
        except ValueError as exc:
            raise ConnectorError(
                f"Open-Meteo returned non-JSON for {url}: {exc}"
            ) from None
    raise ConnectorError(f"rate-limited {RATE_LIMIT_RETRIES} times fetching {url}")


def _keep_raw(raw_dir: pathlib.Path, fips: str, payload: dict) -> str:
    """Write the response, minus its timing noise, and return its digest."""
    canonical = {k: v for k, v in payload.items() if k != "generationtime_ms"}
    data = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{fips}.json").write_bytes(data)
    return f"{fips}:{sha256_bytes(data)[:12]}"


def parts_for(
    centroids: Mapping[str, Point], scope: str | None
) -> dict[str, dict[str, Point]]:
    """Group counties into extracts: by state FIPS, or all under one `scope` label."""
    parts: dict[str, dict[str, Point]] = {}
    for fips in sorted(centroids):
        parts.setdefault(scope or fips[:2], {})[fips] = centroids[fips]
    return parts


def snapshot(
    centroids: Mapping[str, Point],
    years: Sequence[int],
    snapshot_dir: pathlib.Path,
    manifest: Manifest,
    *,
    scope: str | None = None,
    keep_raw: bool = False,
    refresh: bool = False,
    progress: Callable[[str], None] = lambda _msg: None,
    session: Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[pathlib.Path]:
    """Fetch what the extracts lack, write them, pin them. Returns their paths.

    The range requested starts on 1 January of `years[0] - 2`, which is what
    the firewall needs: the first period of the first year is cut a month
    before it starts (the minimum lag), and a trailing twelve-month window
    ending there reaches back into the year before *that*. A one-year
    lookback left the first period's twelve-month columns missing for every
    contract. The coverage check asks for the same range, so an extract
    pulled under the old rule is completed rather than silently reused.

    An extract whose bytes no longer match the manifest record is never
    re-pinned: that is either an edit or a corrupted resume, and pinning the
    new bytes would quietly re-bless it. It is an error unless `refresh` is
    set, which discards the file and fetches every region again.

    `scope` puts every county in one extract under that label (the national
    pull uses `"all"`, as Storm Events does); otherwise counties are grouped
    by state FIPS.
    """
    years = sorted(set(years))
    first_year, last_year = years[0] - 2, years[-1]
    session = session or DEFAULT_SESSION
    paths: list[pathlib.Path] = []
    for part, regions in parts_for(centroids, scope).items():
        path, key = extract_path(snapshot_dir, part), manifest_key(part)
        record = manifest.records.get(key)
        if record is not None and path.exists():
            found = sha256_bytes(path.read_bytes())
            if found != record.sha256 and not refresh:
                raise ConnectorError(
                    f"{path} does not match its manifest record: pinned "
                    f"sha256:{record.sha256}, on disk sha256:{found}. Every card "
                    "scored against this extract names the pinned bytes, so "
                    "re-pinning what is there now would rewrite that provenance. "
                    "Restore the file, or re-pull it with refresh=True "
                    "(`readiness snapshot --refresh`), which discards it and "
                    "fetches every region again."
                )
            if found != record.sha256:
                progress(f"open-meteo: {part} discarded (bytes did not match the pin)")
                path.unlink()
        lines = {} if refresh else read_extract(path)
        todo = [
            f for f in regions
            if f not in lines or not _covers(lines[f], first_year, last_year)
        ]
        if not todo and record is not None and path.exists():
            # The bytes were checked against the record above, so a complete
            # extract that is still what was pinned needs nothing at all.
            progress(f"open-meteo: {part} pinned ({len(regions)} counties)")
            paths.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        raw_notes: list[str] = []
        # Append as each county lands so an interrupted pull resumes where it
        # stopped; the file is rewritten in county order once it is complete.
        with path.open("w" if refresh else "a", encoding="utf-8") as fh:
            for i, fips in enumerate(todo, start=1):
                lat, lon = regions[fips]
                url = request_url(lat, lon, first_year, last_year)
                payload = _get_json(session, url, sleep)
                lines[fips] = _line(fips, parse_daily_to_monthly(payload))
                fh.write(_dumps(lines[fips]))
                fh.flush()
                if keep_raw:
                    raw_dir = snapshot_dir / "open_meteo_raw"
                    raw_notes.append(_keep_raw(raw_dir, fips, payload))
                if i % 25 == 0 or i == len(todo):
                    progress(f"open-meteo: {part} {i}/{len(todo)} counties")
        blob = "".join(_dumps(lines[f]) for f in sorted(lines)).encode("utf-8")
        path.write_bytes(blob)
        notes = (
            f"{len(lines)} counties, {first_year}-01 to {last_year}-12, daily -> monthly"
        )
        if raw_notes:
            notes += "; raw sha256 " + ", ".join(raw_notes)
        manifest.add(
            key,
            SourceRecord(
                source=SOURCE,
                url=API_URL,
                sha256=sha256_bytes(blob),
                bytes=len(blob),
                fetched_at=utc_now(),
                license=LICENSE,
                notes=notes,
            ),
        )
        paths.append(path)
    return paths


class Era5Series:
    """Monthly precipitation and temperature per county, from the extracts."""

    name = "era5"
    kind = "series"
    derived_through = None
    global_coverage = True

    def __init__(self, records: Mapping[str, dict], manifest_keys: Sequence[str]) -> None:
        self.manifest_keys = tuple(manifest_keys)
        self._series = {
            (fips, var): Series(fips, rec["month0"], tuple(rec[var]))
            for fips, rec in records.items()
            for var in VARIABLES
        }

    def series(self, region: str, variable: str) -> Series | None:
        return self._series.get((region, variable))

    def static(self, region: str) -> dict[str, float] | None:
        return None


class ElevationStatic:
    """The elevation Open-Meteo reports for each county's point: timeless geometry."""

    name = "elevation"
    kind = "static"
    derived_through = None
    global_coverage = True

    def __init__(self, records: Mapping[str, dict], manifest_keys: Sequence[str]) -> None:
        self.manifest_keys = tuple(manifest_keys)
        self._elevation = {fips: rec["elevation_m"] for fips, rec in records.items()}

    def series(self, region: str, variable: str) -> Series | None:
        return None

    def static(self, region: str) -> dict[str, float] | None:
        if region not in self._elevation:
            return None
        return {"elevation_m": self._elevation[region]}


def sources(
    extract_paths: Sequence[pathlib.Path], manifest_keys: Sequence[str]
) -> dict[str, Era5Series | ElevationStatic]:
    """The `era5` series and `elevation` static sources, from pinned extracts."""
    records: dict[str, dict] = {}
    for path in extract_paths:
        if not path.exists():
            raise ConnectorError(f"missing Open-Meteo extract {path}")
        records.update(read_extract(path))
    keys = tuple(sorted(set(manifest_keys)))
    return {
        "era5": Era5Series(records, keys),
        "elevation": ElevationStatic(records, keys),
    }
