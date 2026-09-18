"""Case studies as regression tests: what happened, and what the rules say.

Report §6: "Case studies become regression tests: any plan the system blesses
must survive the scenarios that have killed people before." A case study here
is therefore not an essay. It is a facility record as a published
investigation recorded it, plus the finding status each scenario question
*should* come to, so that a change to a rule that would have missed a real
failure fails the suite instead.

Three rules keep the library honest:

1. **Every asserted fact cites a source index.** `event`, `hazard` and `dates`
   are objects with a `text` and a `source` index into the study's own
   `sources` list, and every evidence entry of the facility record must name a
   source document. A case study that cannot cite a published investigation
   for a fact does not belong in the library — the README said so before this
   module existed.
2. **The expected findings are the test.** `check()` runs the scenario's rules
   over the recorded facility and compares statuses. No probabilities are
   involved: a case study has no issued risk layer, so the rules run against an
   empty one and the absence is stated in the prose, exactly as it would be
   for a live report in a county nothing has been issued for.
3. **Unknown keys are refused, and so are places.** This is the one plans
   directory that is committed, so it gets the facility record's own two
   refusals: a key the schema does not define at any level it defines, and a
   string value that reads as a street address, a ZIP+4 or a coordinate pair.

Zero case studies ship. Each is an example added deliberately, by a person who
has read the investigation; `tests/fixtures_plans.py::synthetic_case_study`
exercises the mechanism without pretending to be one.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Mapping, Sequence

from readiness.plans import facility as facility_mod
from readiness.plans import rules as rules_mod
from readiness.plans import scenarios as scenarios_mod
from readiness.plans.facility import Facility, FacilityError
from readiness.plans.risk import RiskLayer
from readiness.plans.scenarios import ScenarioError

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
CASE_STUDIES_DIR = REPO_ROOT / "plans" / "case-studies"

#: The fields that assert something about the world and must cite a source.
FACT_FIELDS: tuple[str, ...] = ("event", "hazard", "dates")

#: What a source entry must carry for a reader to find it again.
SOURCE_KEYS: tuple[str, ...] = ("title", "publisher", "year", "url")

#: What a source entry may also carry.
SOURCE_OPTIONAL_KEYS: tuple[str, ...] = ("note",)

#: The top-level keys a case study has, and no others. `Facility` refuses an
#: unknown key by name and so does this: `plans/case-studies/` is the one
#: plans directory that *is* committed, so a key nothing reads is a key that
#: could carry anything into git unexamined.
STUDY_KEYS: tuple[str, ...] = (
    "slug", "scenario", "sources", "facility_as_recorded", "expected_findings",
    "event", "hazard", "dates",
)

#: A fact's own keys.
FACT_KEYS: tuple[str, ...] = ("text", "source")

#: The period label a case study's rules run under. No issuance exists for a
#: past event in this repository, and inventing one would be the opposite of
#: the point, so the risk layer is empty and says so.
NO_PERIOD = "no issued period"


class CaseStudyError(ValueError):
    """A case study file we will not load. Every message names the field."""


@dataclasses.dataclass(frozen=True)
class Fact:
    """One asserted fact and the index of the source that supports it."""

    text: str
    source: int


@dataclasses.dataclass(frozen=True)
class Source:
    title: str
    publisher: str
    year: int | None
    url: str
    note: str = ""


@dataclasses.dataclass(frozen=True)
class CaseStudy:
    """A real event, as recorded, with the findings its record should produce."""

    slug: str
    scenario_id: str
    event: Fact
    hazard: Fact
    dates: Fact
    sources: tuple[Source, ...]
    facility: Facility
    expected_findings: dict[str, str]
    path: pathlib.Path | None = None

    def facts(self) -> dict[str, Fact]:
        return {"event": self.event, "hazard": self.hazard, "dates": self.dates}

    @classmethod
    def from_json(cls, raw: Mapping, *, where: str = "<case study>") -> "CaseStudy":
        """Validate a parsed study. Every refusal is a `CaseStudyError`.

        Unknown keys are refused at every level this schema defines, and every
        string in the file goes through the same place scan a facility record
        does: a case study is committed, so an address in a free-text note
        would be an address in git.
        """
        if not isinstance(raw, Mapping):
            raise CaseStudyError(
                f"{where}: expected a JSON object, got {type(raw).__name__}"
            )
        _check_keys(raw, STUDY_KEYS, where, "the study")
        found = facility_mod.forbidden_in(
            {k: v for k, v in raw.items() if k != "facility_as_recorded"}
        )
        if found:
            spot, what = found[0]
            raise CaseStudyError(
                f"{where}: field {spot!r} is refused, {what} — "
                f"{facility_mod.FORBIDDEN_REASON}"
            )
        for key in STUDY_KEYS:
            if key not in raw:
                raise CaseStudyError(f"{where}: missing {key!r}")
        sources = _read_sources(raw["sources"], where)
        facts = {name: _read_fact(name, raw[name], len(sources), where)
                 for name in FACT_FIELDS}
        try:
            facility = Facility.from_json(raw["facility_as_recorded"], where=where)
        except FacilityError as exc:
            raise CaseStudyError(f"{where}: facility_as_recorded is refused: {exc}") from None
        expected = raw["expected_findings"]
        if not isinstance(expected, Mapping) or not expected:
            raise CaseStudyError(
                f"{where}: expected_findings must map question ids to statuses"
            )
        for question_id, status in expected.items():
            if status not in scenarios_mod.statuses():
                raise CaseStudyError(
                    f"{where}: expected_findings[{question_id!r}] is {status!r}; "
                    f"expected one of {list(scenarios_mod.statuses())}"
                )
        return cls(
            slug=str(raw["slug"]),
            scenario_id=str(raw["scenario"]),
            event=facts["event"], hazard=facts["hazard"], dates=facts["dates"],
            sources=sources, facility=facility,
            expected_findings={str(k): str(v) for k, v in expected.items()},
        )

    @classmethod
    def from_path(cls, path: pathlib.Path | str) -> "CaseStudy":
        path = pathlib.Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CaseStudyError(f"{path}: cannot be read ({exc})") from None
        except ValueError as exc:
            raise CaseStudyError(f"{path}: not valid JSON ({exc})") from None
        study = cls.from_json(raw, where=str(path))
        return dataclasses.replace(study, path=path)


def _check_keys(
    raw: Mapping, known: Sequence[str], where: str, what: str
) -> None:
    for key in raw:
        if key not in known:
            raise CaseStudyError(
                f"{where}: unknown field {key!r} in {what}; the schema knows "
                f"{sorted(known)} here, and a field no rule reads must not be "
                "silently accepted into a committed file"
            )


def _read_sources(raw: object, where: str) -> tuple[Source, ...]:
    if not isinstance(raw, list) or not raw:
        raise CaseStudyError(
            f"{where}: 'sources' must be a non-empty list; every fact cites one"
        )
    out = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise CaseStudyError(f"{where}: sources[{i}] must be an object")
        _check_keys(entry, (*SOURCE_KEYS, *SOURCE_OPTIONAL_KEYS), where,
                    f"sources[{i}]")
        for key in SOURCE_KEYS:
            if key not in entry:
                raise CaseStudyError(f"{where}: sources[{i}] is missing {key!r}")
        out.append(Source(
            title=str(entry["title"]), publisher=str(entry["publisher"]),
            year=entry["year"], url=str(entry["url"]),
            note=str(entry.get("note", "")),
        ))
    return tuple(out)


def _read_fact(name: str, raw: object, n_sources: int, where: str) -> Fact:
    if isinstance(raw, Mapping):
        _check_keys(raw, FACT_KEYS, where, f"{name!r}")
    if not isinstance(raw, Mapping) or "text" not in raw or "source" not in raw:
        raise CaseStudyError(
            f"{where}: {name!r} must be an object with 'text' and a 'source' index "
            "into 'sources' — a fact without a source does not belong in the library"
        )
    index = raw["source"]
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < n_sources:
        raise CaseStudyError(
            f"{where}: {name!r} cites source {index!r}, which is not one of the "
            f"{n_sources} source(s) listed"
        )
    return Fact(text=str(raw["text"]), source=index)


def facts_with_sources(study: CaseStudy) -> list[str]:
    """Any fact whose source index does not resolve. Empty means the study is sound.

    `from_json` already refuses a bad index, so this is the same rule stated
    where a reader looks for it — and it is reachable, because a study built
    through the dataclass rather than through `from_json` has not been past
    that refusal. It is what `readiness scenarios check` prints when a study
    is rejected.

    There is deliberately no second loop over the facility's evidence: a
    `Facility` cannot exist with a blank `source_doc`, so a check for one here
    would be a branch no test could reach and no reader could trust.
    """
    return [
        f"{name}: cites source {fact.source}, which does not exist"
        for name, fact in study.facts().items()
        if not 0 <= fact.source < len(study.sources)
    ]


# --------------------------------------------------------------------------- #
# Running one
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class CaseStudyResult:
    """What one case study came to when its scenario's rules ran over it."""

    slug: str
    scenario_id: str
    observed: dict[str, str]
    expected: dict[str, str]
    mismatches: tuple[str, ...]
    problems: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.mismatches and not self.problems

    def format(self) -> str:
        head = (
            f"  [{'ok' if self.passed else 'FAIL'}] {self.slug} "
            f"({self.scenario_id}, {len(self.expected)} expected finding(s))"
        )
        return "\n".join([head, *(f"      {m}" for m in (*self.problems, *self.mismatches))])


def check(
    path: pathlib.Path | str, *, scenarios_dir: pathlib.Path | None = None
) -> CaseStudyResult:
    """Run the study's scenario over its recorded facility and compare statuses."""
    study = CaseStudy.from_path(path)
    return check_study(study, scenarios_dir=scenarios_dir)


def check_study(
    study: CaseStudy, *, scenarios_dir: pathlib.Path | None = None
) -> CaseStudyResult:
    problems = list(facts_with_sources(study))
    try:
        scenario = scenarios_mod.load(study.scenario_id, scenarios_dir)
    except ScenarioError as exc:
        return CaseStudyResult(
            study.slug, study.scenario_id, {}, dict(study.expected_findings),
            (), tuple(problems + [str(exc)]),
        )
    problems += scenarios_mod.check_shape(scenario, rules_mod.RULES)
    known = {q.id for q in scenario.questions}
    unknown = sorted(set(study.expected_findings) - known)
    if unknown:
        problems.append(
            f"expected_findings names question(s) {unknown} that scenario "
            f"{scenario.id} does not ask"
        )
    missing = sorted(known - set(study.expected_findings))
    if missing:
        problems.append(
            f"expected_findings does not say what question(s) {missing} should come to"
        )
    findings = rules_mod.run(study.facility, RiskLayer.empty(NO_PERIOD), scenario)
    observed = rules_mod.statuses(findings)
    unrun = sorted(known - set(observed))
    if unrun:
        problems.append(
            f"the rules produced no finding for question(s) {unrun}, so their "
            "expectations were never checked"
        )
    mismatches = tuple(
        f"{qid}: expected {study.expected_findings[qid]!r}, rules said {observed[qid]!r}"
        for qid in sorted(set(observed) & set(study.expected_findings))
        if observed[qid] != study.expected_findings[qid]
    )
    return CaseStudyResult(
        study.slug, study.scenario_id, observed, dict(study.expected_findings),
        mismatches, tuple(problems),
    )


# --------------------------------------------------------------------------- #
# The library on disk
# --------------------------------------------------------------------------- #


def case_studies_dir(directory: pathlib.Path | None = None) -> pathlib.Path:
    return pathlib.Path(directory) if directory is not None else CASE_STUDIES_DIR


def paths(directory: pathlib.Path | None = None) -> list[pathlib.Path]:
    root = case_studies_dir(directory)
    return sorted(root.glob("*.json")) if root.exists() else []


def check_all(
    directory: pathlib.Path | None = None, *, scenarios_dir: pathlib.Path | None = None
) -> list[CaseStudyResult]:
    """Every committed case study, in file order. Zero of them is a pass."""
    out = []
    for path in paths(directory):
        try:
            out.append(check(path, scenarios_dir=scenarios_dir))
        except CaseStudyError as exc:
            out.append(CaseStudyResult(path.stem, "", {}, {}, (), (str(exc),)))
    return out


__all__ = [
    "CASE_STUDIES_DIR",
    "FACT_FIELDS",
    "FACT_KEYS",
    "NO_PERIOD",
    "SOURCE_KEYS",
    "SOURCE_OPTIONAL_KEYS",
    "STUDY_KEYS",
    "CaseStudy",
    "CaseStudyError",
    "CaseStudyResult",
    "Fact",
    "Source",
    "case_studies_dir",
    "check",
    "check_all",
    "check_study",
    "facts_with_sources",
    "paths",
]
