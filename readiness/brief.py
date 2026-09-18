"""The county brief: one cited paragraph per hazard, validated before it is written.

Report §7: "Numbers come from the harness; prose comes from the model — and
prose can hallucinate. Require every plan sentence to cite either a computed
number, a dataset row, or a named guidance document, validated by rules before
display." And, from the same section: this is decision support, never a
warning channel, and county aggregates only.

So the prose here is not written by a language model at all. It is four
sentence templates per contract, filled from values that each carry a source
`readiness.cite` can resolve — an issued file, a ledger card, a manifest key,
a guidance document — and the whole document goes through `cite.validate`
before anything is written. A violation means nothing is written, and the
command prints the rule that was broken.

What a brief never contains, by construction rather than by review:

* anything below the county — the issued file is keyed by county FIPS, the
  exposure row has no finer field, and `sub_county_keys` re-checks the
  rendered JSON before it is written;
* "would touch" — a probability of at least one damaging event in the county
  is a statement about occurrence, not about a footprint, so the brief says
  what the county *holds* in a separate, separately cited sentence;
* a prediction of a specific event, or warning language, except the one fixed
  disclaimer that points at the NWS and IPAWS (`cite.FORBIDDEN_PHRASES`);
* a substitute for a missing input: with no pinned exposure extract for the
  county's state, the brief says so and reports no counts.

`docs/brief.md` is the prose specification of this module. Two places where
the implementation had to be more explicit than that page: every number
carries its own marker (a sentence with two numbers cites two claims, because
a claim has one value), and the tolerance sentence cites a `computed` claim
derived from the contract's tolerance as the card applied it.
"""

from __future__ import annotations

import datetime as _dt
import html
import os
import pathlib
import re
from typing import Mapping, Sequence

from readiness import cite
from readiness import data as data_mod
from readiness import issue as issue_mod
from readiness.connectors import usa_structures
from readiness.connectors.base import Manifest
from readiness.contracts import Contract
from readiness.exposure.table import CountyExposure
from readiness.harness.ledger import ExperimentCard, Ledger
from readiness.issue import Issued

REPO_ROOT = data_mod.REPO_ROOT
#: Where `readiness brief` writes: briefs/<fips>/<period>.html and .json.
BRIEFS_DIR = REPO_ROOT / "briefs"
BRIEFS_DIR_ENV = "READINESS_BRIEFS_DIR"
LICENSES_PATH = REPO_ROOT / "DATA-LICENSES.md"

#: The document kind the website lists and the sandbox recognises.
KIND = "county-brief"

#: Keys that would name something finer than a county. None can be produced by
#: this module; the check is a tripwire on the rendered JSON, so a later field
#: added anywhere upstream cannot reach a published brief unnoticed.
SUB_COUNTY_KEYS: frozenset[str] = frozenset({
    "address", "addr", "street", "lat", "latitude", "lon", "lng", "longitude",
    "point", "geometry", "geom", "x", "y", "tract", "blockgroup", "block",
    "parcel", "building", "footprint", "facility", "facility_name", "owner",
})

#: Lines that always apply when `DATA-LICENSES.md` cannot be read (the browser
#: sandbox packs the package, not the docs).
FALLBACK_ATTRIBUTION: tuple[str, ...] = (
    "Hazard history: NOAA National Centers for Environmental Information, "
    "Storm Events Database (public domain).",
    "Geography: US Census Bureau (public domain).",
    "Weather data by Open-Meteo.com (CC BY 4.0); ERA5 by ECMWF/Copernicus.",
    "Not an official warning product. Official alerts come from the US National "
    "Weather Service and IPAWS.",
)


class BriefError(ValueError):
    """A brief that cannot be built honestly: a missing card, contract or value."""


class BriefRefused(RuntimeError):
    """A brief the validator rejected. It carries the violations; nothing was written."""

    def __init__(self, violations: Sequence[cite.Violation], detail: str = "") -> None:
        self.violations = list(violations)
        lines = [detail] if detail else []
        lines += [f"  {v}" for v in self.violations]
        super().__init__("\n".join(lines))


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def briefs_root(out_dir: pathlib.Path | None = None) -> pathlib.Path:
    if out_dir is not None:
        return pathlib.Path(out_dir)
    override = os.environ.get(BRIEFS_DIR_ENV)
    return pathlib.Path(override) if override else BRIEFS_DIR


def county_label(name: str | None, state: str | None, fips: str) -> str:
    """"Adair County, OK" when the county file is loaded, else the FIPS code."""
    if name and state:
        return f"{name}, {state}"
    return name or fips


def hazard_words(hazard: str) -> str:
    """`inland_flood` -> "inland flood": the hazard as a sentence says it."""
    return hazard.replace("_", " ")


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def _probability_claim(issued: Issued, fips: str) -> cite.Claim:
    value = issued.probability(fips)
    if value is None:
        raise BriefError(
            f"issued file for {issued.contract} {issued.period_label} carries no "
            f"probability for county {fips}"
        )
    return cite.Claim(
        id=f"{issued.contract}-p",
        text=(
            f"probability of at least one damaging event in {fips} during "
            f"{issued.period_label}, from the issued file"
        ),
        value=value,
        source=cite.Source(
            "issued", issue_mod.issued_ref(issued.contract, issued.period_label)
        ),
        fmt="{:.0%}",
    )


def _backtest_claims(
    name: str, card: ExperimentCard, contract: Contract
) -> list[cite.Claim]:
    """The test card's skill, and the tolerance the verdict applied to it."""
    bss = (card.scorecard or {}).get("brier_skill_score")
    if bss is None:
        raise BriefError(f"test card {card.experiment_id} carries no brier skill score")
    tolerance = contract.reliability_tolerance_pp
    return [
        cite.Claim(
            id=f"{name}-bss",
            text=(
                f"Brier skill score of {card.model}@{card.version} on the test split, "
                f"card {card.experiment_id}"
            ),
            value=bss,
            source=cite.Source(
                "ledger", f"{name}/{card.experiment_id}#scorecard.brier_skill_score"
            ),
            fmt="{:+.2f}",
        ),
        cite.Claim(
            id=f"{name}-tolerance",
            text=(
                f"reliability tolerance contract {name} applied to card "
                f"{card.experiment_id}, in probability units"
            ),
            value=tolerance,
            source=cite.Source("ledger", f"{name}/{card.experiment_id}#verdict"),
        ),
        cite.Claim(
            id=f"{name}-tolerance-pp",
            text="the same tolerance in percentage points",
            value=tolerance * 100,
            source=cite.Source("computed", f"{name}-tolerance"),
            fmt="{:.0f}",
        ),
    ]


def _exposure_claims(row: CountyExposure) -> list[cite.Claim]:
    """Four counts, all from the one pinned state extract the row names."""
    source = cite.Source("manifest", row.source_key)
    return [
        cite.Claim("exposure-total", "structures in the county", row.total, source, "{:,}"),
        cite.Claim(
            "exposure-unclassified",
            "share of them whose occupancy the mapping did not recognise",
            row.unclassified_share,
            source,
            "{:.0%}",
        ),
        cite.Claim(
            "exposure-schools", "schools among them", row.by_class.get("school", 0),
            source, "{:,}",
        ),
        cite.Claim(
            "exposure-hospitals", "hospitals among them",
            row.by_class.get("hospital", 0), source, "{:,}",
        ),
    ]


def _no_exposure_claim(fips: str, derived_from: str) -> cite.Claim:
    """A statement of absence. There is no artefact to point at — that is the point.

    Nothing is pinned for this state, so there is no manifest key to cite; the
    claim is `computed`, derived from the claim the paragraph does have, and
    its text says exactly what was looked for and not found. The brief's
    footer lists the extracts that were loaded.
    """
    return cite.Claim(
        id="exposure-absent",
        text=(
            f"no pinned USA Structures extract holds a row for county {fips} "
            f"(state {fips[:2]}), so no structure counts are reported"
        ),
        value=None,
        source=cite.Source("computed", derived_from),
    )


def _disclaimer_claim() -> cite.Claim:
    return cite.Claim(
        id=cite.NOT_A_WARNING_GUIDANCE,
        text="official alerting: NWS watches and warnings, and IPAWS",
        value=None,
        source=cite.Source("guidance", cite.NOT_A_WARNING_GUIDANCE),
    )


def _test_years(contract: Contract) -> str:
    years = contract.test_years
    return f"{years[0]}-{years[-1]}" if len(years) > 1 else f"{years[0]}"


def _paragraph(
    issued: Issued,
    contract: Contract,
    card: ExperimentCard,
    fips: str,
    county: str,
    exposure: CountyExposure | None,
) -> tuple[list[cite.Sentence], list[cite.Claim]]:
    """One contract's four sentences, and the claims only this contract needs.

    The exposure claims are the county's, not the contract's, so they are
    shared across paragraphs and added once by `build`; this returns the
    sentences that cite them.
    """
    name = issued.contract
    probability = _probability_claim(issued, fips)
    bss, tolerance, tolerance_pp = _backtest_claims(name, card, contract)
    sentences = [
        cite.Sentence.from_text(
            f"{cite.render_value(probability)} chance of at least one damaging "
            f"{hazard_words(contract.hazard)} event in {county} during "
            f"{issued.period_label} [c:{probability.id}]."
        ),
        cite.Sentence.from_text(
            f"This comes from {issued.model}@{issued.version}, which scored a Brier "
            f"skill score of {cite.render_value(bss)} on the untouched "
            f"{_test_years(contract)} with every populated reliability bin within "
            f"{cite.render_value(tolerance_pp)} points "
            f"[c:{bss.id}][c:{tolerance_pp.id}]."
        ),
        _exposure_sentence(county, exposure),
        cite.Sentence.from_text(
            f"{cite.NOT_A_WARNING_SENTENCE} [c:{cite.NOT_A_WARNING_GUIDANCE}]."
        ),
    ]
    return sentences, [probability, bss, tolerance, tolerance_pp]


def _exposure_sentence(county: str, row: CountyExposure | None) -> cite.Sentence:
    """What the county holds — never what an event "would touch".

    Occurrence is not footprint: the probability is about at least one damaging
    event in the county, and this sentence is about what stands there. With no
    pinned extract the brief says so; it never substitutes a neighbour's counts
    or a national average.
    """
    if row is None:
        return cite.Sentence.from_text(
            f"No exposure layer is pinned for {county}; structure counts are not "
            "reported [c:exposure-absent]."
        )
    total, unclassified, schools, hospitals = _exposure_claims(row)
    return cite.Sentence.from_text(
        f"{county} holds {cite.render_value(total)} structures "
        f"({cite.render_value(unclassified)} unclassified) "
        f"[c:{total.id}][c:{unclassified.id}], including "
        f"{cite.render_value(schools)} schools and {cite.render_value(hospitals)} "
        f"hospitals [c:{schools.id}][c:{hospitals.id}]."
    )


def build(
    fips: str,
    period_label: str,
    issued: Sequence[Issued],
    exposure: CountyExposure | None,
    cards: Mapping[str, ExperimentCard],
    county_name: str,
    contracts: Mapping[str, Contract],
) -> cite.Document:
    """One county's brief for one period, as a `cite.Document`.

    `issued` is every issued file covering the county for this period, one per
    contract; `cards` and `contracts` are keyed by contract name and supply the
    test card each probability leans on and the criteria it was judged against.
    `exposure` is the county's row, or None when no extract is pinned for its
    state — in which case the brief says so and reports no counts.
    """
    covering = sorted(
        (i for i in issued if i.period_label == period_label and i.covers(fips)),
        key=lambda i: i.contract,
    )
    if not covering:
        raise BriefError(
            f"no issued file for period {period_label} carries a probability for "
            f"county {fips}; run `readiness issue` first"
        )
    county = county_name or fips
    sentences: list[cite.Sentence] = []
    claims: list[cite.Claim] = []
    for one in covering:
        contract = contracts.get(one.contract)
        card = cards.get(one.contract)
        if contract is None:
            raise BriefError(f"contract {one.contract!r} is not registered")
        if card is None:
            raise BriefError(
                f"no test card for contract {one.contract!r}; the issued file names "
                f"{one.validated_by}"
            )
        if card.experiment_id != one.validated_by:
            raise BriefError(
                f"issued file for {one.contract} was validated by {one.validated_by}, "
                f"but the card supplied is {card.experiment_id}"
            )
        para, para_claims = _paragraph(one, contract, card, fips, county, exposure)
        sentences.extend(para)
        claims.extend(para_claims)

    if exposure is not None:
        claims.extend(_exposure_claims(exposure))
    else:
        claims.append(_no_exposure_claim(fips, f"{covering[0].contract}-p"))
    claims.append(_disclaimer_claim())

    return cite.Document(
        title=f"{county} — {period_label}",
        kind=KIND,
        sentences=tuple(sentences),
        claims=tuple(claims),
        generated_at=utc_now(),
        inputs=_inputs(fips, period_label, covering, cards, exposure),
    )


def _inputs(
    fips: str,
    period_label: str,
    covering: Sequence[Issued],
    cards: Mapping[str, ExperimentCard],
    exposure: CountyExposure | None,
) -> dict[str, str]:
    """What the brief was built from, for its footer: files, cards, extracts."""
    out = {"county": fips, "period": period_label}
    for one in covering:
        card = cards[one.contract]
        out[f"issued:{one.contract}"] = (
            f"issued/{one.contract}/{one.period_label}.json "
            f"(data version sha256:{one.data_version})"
        )
        out[f"card:{one.contract}"] = (
            f"{card.experiment_id} sha256:{card.card_hash[:12]} "
            f"(contract sha256:{one.contract_digest})"
        )
    out["exposure"] = (
        f"{exposure.source_key}, layer vintage {exposure.vintage}"
        if exposure is not None
        else "no USA Structures extract pinned for this state"
    )
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def ledger_refs(
    registry: Mapping[str, Contract], *, experiments_dir: pathlib.Path | None = None
) -> dict[str, dict]:
    """Card records a brief may cite, by `exp-NNNN` and by `<contract>/exp-NNNN`.

    The same grammar `tools/build_site.py` accepts, so a brief written here
    still resolves when the website re-validates it from the committed tree.
    """
    refs: dict[str, dict] = {}
    for name, contract in registry.items():
        where = data_mod.paths(contract, experiments_dir=experiments_dir)
        for card in Ledger(where.ledger).read():
            record = card.record()
            refs[card.experiment_id] = record
            refs[f"{name}/{card.experiment_id}"] = record
    return refs


def issued_refs(issued_dir: pathlib.Path | None = None) -> dict[str, set[str]]:
    """Issued files by every spelling of their path a brief may use."""
    root = issue_mod.issued_root(issued_dir)
    refs: dict[str, set[str]] = {}
    for path in sorted(root.glob("*/*.json")) if root.exists() else []:
        labels = {path.stem}
        try:
            labels.add(issue_mod.Issued.read(path).period_label)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        rel = path.relative_to(root).as_posix()
        for spelling in (f"issued/{rel}", rel, rel[: -len(".json")]):
            refs[spelling] = labels
    return refs


def resolver(
    registry: Mapping[str, Contract],
    *,
    issued_dir: pathlib.Path | None = None,
    snapshot_dir: pathlib.Path | None = None,
    experiments_dir: pathlib.Path | None = None,
) -> cite.DictResolver:
    """What a brief may lean on: the ledgers, the issued files, the manifest, guidance.

    `snapshot_dir` is read at call time, not bound at import, so a caller (or a
    test) that points the data plane elsewhere is followed here too.
    """
    manifest_path = (snapshot_dir or data_mod.SNAPSHOT_DIR) / "manifest.json"
    manifest = Manifest.load(manifest_path) if manifest_path.exists() else None
    return cite.DictResolver({
        "ledger": ledger_refs(registry, experiments_dir=experiments_dir),
        "issued": issued_refs(issued_dir),
        "manifest": set(manifest.records) if manifest else set(),
    })


def sub_county_keys(payload: object, path: str = "") -> list[str]:
    """Every key in the rendered document that would name something below a county."""
    found: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key).casefold() in SUB_COUNTY_KEYS:
                found.append(here)
            found.extend(sub_county_keys(value, here))
    elif isinstance(payload, (list, tuple)):
        for i, value in enumerate(payload):
            found.extend(sub_county_keys(value, f"{path}[{i}]"))
    return found


def check(
    doc: cite.Document,
    resolve: cite.Resolver,
    guidance: Mapping[str, Mapping] | None = None,
) -> list[cite.Violation]:
    """Every citation rule, then the county-only rule, as one list of violations."""
    found = cite.validate(doc, resolve, guidance)
    for key in sub_county_keys(cite.to_dict(doc)):
        found.append(
            cite.Violation(
                cite.UNRESOLVED, None,
                f"the document carries a sub-county field ({key}); report §7 publishes "
                "county aggregates only",
            )
        )
    return found


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def attribution(
    contracts: Sequence[Contract],
    *,
    exposure_joined: bool,
    path: pathlib.Path = LICENSES_PATH,
) -> list[str]:
    """`DATA-LICENSES.md`'s attribution block, resolved for this brief.

    The conditional lines are decided by what the brief actually leaned on: the
    zone crosswalk when any contract expands zone events, the USA Structures
    line when an exposure row was joined. The footprint line stays out —
    nothing here reads a footprint, which is why the exposure join is county
    counts only.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    match = re.search(
        r"## Attribution block for published outputs.*?```\n(.*?)```", text, re.S
    )
    if match is None:
        lines = list(FALLBACK_ATTRIBUTION)
        return lines + [usa_structures.ATTRIBUTION + "."] if exposure_joined else lines
    entries: list[str] = []
    for line in match.group(1).splitlines():
        if line.startswith("  ") and entries:
            entries[-1] += " " + line.strip()
        elif line.strip():
            entries.append(line.rstrip())
    applies = {
        "[if zone events expanded]": any(c.zone_policy == "expand" for c in contracts),
        "[if NRI shown]": False,
        "[if exposure joined]": exposure_joined,
        "[if risk engine used]": False,
    }
    out = []
    for entry in entries:
        tag = re.match(r"\[if [^\]]+\]", entry)
        rest = entry[tag.end():].strip() if tag else entry
        if tag is not None and not applies.get(tag.group(0), False):
            continue
        # Footprints are an ODbL layer this repository never downloads; the
        # exposure join is USA Structures county counts and nothing else.
        if "Building footprints" in rest:
            continue
        out.append(rest)
    return out


def render_html(doc: cite.Document, lines: Sequence[str]) -> str:
    """The validator's page, with the licence block appended under the footer."""
    page = cite.render_html(doc)
    block = "\n".join(html.escape(line) for line in lines)
    footer = (
        '<footer class="attribution"><p>Attribution and licences</p>'
        f"<pre>{block}</pre></footer>\n"
    )
    return page.replace("</body></html>", footer + "</body></html>")


def write(
    doc: cite.Document,
    out_dir: pathlib.Path | None = None,
    *,
    attribution_lines: Sequence[str] = FALLBACK_ATTRIBUTION,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Write `briefs/<fips>/<period>.html` and `.json`; return both paths.

    The JSON is the document the HTML was rendered from, so anyone can re-run
    the validator on the committed file — which is exactly what the website
    does before it lists a brief.
    """
    fips = doc.inputs.get("county") or ""
    period = doc.inputs.get("period") or ""
    if not (fips and period):
        raise BriefError(
            "a brief's inputs must name the county and the period it was built for"
        )
    directory = briefs_root(out_dir) / fips
    directory.mkdir(parents=True, exist_ok=True)
    html_path = directory / f"{period}.html"
    json_path = directory / f"{period}.json"
    html_path.write_text(render_html(doc, attribution_lines), encoding="utf-8")
    json_path.write_text(cite.to_json(doc), encoding="utf-8")
    return html_path, json_path


def write_validated(
    doc: cite.Document,
    resolve: cite.Resolver,
    out_dir: pathlib.Path | None = None,
    *,
    contracts: Sequence[Contract] = (),
    exposure_joined: bool = False,
    guidance: Mapping[str, Mapping] | None = None,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Validate, then write — or raise `BriefRefused` and write nothing at all."""
    violations = check(doc, resolve, guidance)
    if violations:
        raise BriefRefused(
            violations,
            f"refusing to write {doc.title}: {len(violations)} violation(s)",
        )
    return write(
        doc,
        out_dir,
        attribution_lines=attribution(contracts, exposure_joined=exposure_joined),
    )


__all__ = [
    "BRIEFS_DIR",
    "BriefError",
    "BriefRefused",
    "KIND",
    "SUB_COUNTY_KEYS",
    "attribution",
    "briefs_root",
    "build",
    "check",
    "county_label",
    "issued_refs",
    "ledger_refs",
    "render_html",
    "resolver",
    "sub_county_keys",
    "write",
    "write_validated",
]
