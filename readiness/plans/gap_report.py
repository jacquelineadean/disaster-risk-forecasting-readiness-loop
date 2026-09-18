"""The gap report: one cited document per facility, per period, per scenario.

The shape is the county brief's, because the rule is the same one (report §7):
every sentence cites a computed number, a dataset row or a named guidance
document, the whole document goes through `readiness.cite.validate`, and if a
single rule is broken nothing is written and the violations are printed.

Three files come out of a clean run:

* `<slug>/<period>.json` — the `cite.Document` itself, so anyone can re-run
  the validator on the file and get the answer the command got;
* `<slug>/<period>.html` — the rendered page the planner reads;
* `<slug>/<period>.blind.html` — the same page with the facility's slug
  replaced by `FACILITY-<6 hex>` and every partner and co-tenant by
  `PARTNER-n`. That file is what goes to a reviewer, and its sha256 is what a
  review record binds a rating to. A review is therefore an attestation about
  one exact rendering: change a number and the sha moves, and
  `verify --phase 3` stops finding the report the review claims to be about.

None of this is a warning product and none of it is in charge. A gap report is
an analysis a practising emergency manager reads, argues with and signs — the
agent's job is to make that analysis cheap, which is what report §6 asks for.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import os
import pathlib
import re
from typing import Callable, Mapping, Sequence

from readiness import cite
from readiness.plans import draft as draft_mod
from readiness.plans import rules as rules_mod
from readiness.plans import scenarios as scenarios_mod
from readiness.plans.facility import FORBIDDEN_KEYS, Facility, FacilityError
from readiness.plans.risk import RiskLayer
from readiness.plans.scenarios import Scenario

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
#: Where `readiness gap-report` writes. Never committed: a real facility's
#: report names a real building, and `.gitignore` keeps the tree empty.
REPORTS_DIR = REPO_ROOT / "plans" / "reports"
REPORTS_DIR_ENV = "READINESS_REPORTS_DIR"

#: The document kind a review record and `verify --phase 3` look for.
KIND = "gap-report"

#: The two drafters. `local` is deterministic and is what the tests and the
#: case-study regressions run; `claude` rewrites the local prose and is checked
#: sentence by sentence against this document's own claims.
DRAFTERS: tuple[str, ...] = ("local", "claude")

#: How the blinded page announces what it is, so `readiness review record` can
#: bind a rating to it without ever seeing the facility's name.
BLIND_META = '<meta name="readiness-blind" content="{label}|{period}|{kind}">'
_BLIND_META_RE = re.compile(
    r'<meta name="readiness-blind" content="([^"|]+)\|([^"|]*)\|([^"]*)">'
)

_ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth",
             "seventh", "eighth", "ninth", "tenth")

_STATUS_WORDS = {
    "answered": "answered from the record",
    "unanswered": "unanswered from the record, which is itself a finding",
    "failed": "a failure",
    "cannot_run": "unrunnable, and this report says so rather than guessing",
}


class GapReportError(ValueError):
    """A gap report that cannot be built honestly."""


class GapReportRefused(RuntimeError):
    """The validator said no. It carries the violations; nothing was written."""

    def __init__(self, violations: Sequence[cite.Violation], detail: str = "") -> None:
        self.violations = list(violations)
        lines = [detail] if detail else []
        lines += [f"  {v}" for v in self.violations]
        super().__init__("\n".join(lines))


@dataclasses.dataclass(frozen=True)
class Report:
    """What one run produced: the document, the findings and the files written."""

    document: cite.Document
    findings: tuple[rules_mod.Finding, ...]
    html_path: pathlib.Path
    json_path: pathlib.Path
    blind_path: pathlib.Path
    blind_sha256: str

    def statuses(self) -> dict[str, str]:
        return rules_mod.statuses(self.findings)

    def gaps(self) -> tuple[rules_mod.Finding, ...]:
        return tuple(f for f in self.findings if f.is_gap)


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def reports_root(out_dir: pathlib.Path | None = None) -> pathlib.Path:
    if out_dir is not None:
        return pathlib.Path(out_dir)
    override = os.environ.get(REPORTS_DIR_ENV)
    return pathlib.Path(override) if override else REPORTS_DIR


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #


def _ordinal(index: int) -> str:
    return _ORDINALS[index] if index < len(_ORDINALS) else "next"


def _lead_sentence(index: int, finding: rules_mod.Finding, claim_id: str) -> cite.Sentence:
    """One line per question: which question, and what it came to.

    The question's own wording is the specification's, and it contains the word
    "inject 3" — a digit that is not a quantity — so it lives in the claim the
    sentence cites rather than in the prose, where the validator would have to
    be told to ignore it.
    """
    words = _STATUS_WORDS.get(finding.status, finding.status)
    return cite.Sentence.from_text(
        f"On the scenario's {_ordinal(index)} question, quoted in full in the sources, "
        f"this record is {words} [c:{claim_id}]."
    )


def _question_claim(scenario: Scenario, question_id: str) -> cite.Claim:
    question = scenario.question(question_id)
    return cite.Claim(
        id=f"q-{question_id}",
        text=question.text,
        value=None,
        source=cite.Source("scenario", scenario.ref(question_id)),
    )


def _provenance(
    scenario: Scenario, drafter: str, dropped: int
) -> tuple[list[cite.Sentence], list[cite.Claim]]:
    """Two sentences a reader needs to trust the rest: what ran, and what was cut."""
    what = cite.Claim(
        id="provenance-scenario",
        text=f"the scenario this report runs: {scenario.id} ({scenario.title})",
        value=None,
        source=cite.Source("scenario", scenario.id),
    )
    cut = cite.Claim(
        id="provenance-dropped",
        text=(
            "drafted sentences dropped by the citation validator before this report "
            "was written"
        ),
        value=int(dropped),
        source=cite.Source("computed", what.id),
        fmt="{:d}",
    )
    sentences = [
        cite.Sentence.from_text(
            f"This report runs the scenario and the facility record named in its "
            f"footer, with prose from the {drafter} drafter [c:{what.id}]."
        ),
        cite.Sentence.from_text(
            f"{cite.render_value(cut)} drafted sentence(s) were dropped for failing "
            f"the citation rules before anything was written [c:{cut.id}]."
        ),
    ]
    return sentences, [what, cut]


#: A rewriter takes the report's body and its claims and returns a body plus
#: the number of sentences it dropped. `readiness.agent.planner` supplies one.
Rewriter = Callable[
    [Sequence[cite.Sentence], Sequence[cite.Claim]],
    "tuple[list[cite.Sentence], int]",
]


def build(
    facility: Facility,
    scenario: Scenario,
    findings: Sequence[rules_mod.Finding],
    risk: RiskLayer,
    period_label: str,
    *,
    drafter: str = "local",
    rewriter: Rewriter | None = None,
) -> cite.Document:
    """The gap report as a `cite.Document`, in the scenario's question order.

    Per question: one line saying what it came to, the rule's own sentences,
    then the local drafter's section for that finding. Then the provenance
    lines and the fixed not-a-warning disclaimer, which are added after any
    rewrite so a drafter cannot restate its own provenance.
    """
    if drafter not in DRAFTERS:
        raise GapReportError(f"unknown drafter {drafter!r}; expected one of {list(DRAFTERS)}")
    order = [q.id for q in scenario.questions]
    by_question = {f.question_id: f for f in findings}
    unknown = [f.question_id for f in findings if f.question_id not in order]
    if unknown:
        raise GapReportError(
            f"findings name questions scenario {scenario.id} does not have: {unknown}"
        )
    drafted = draft_mod.sections(facility, scenario, findings)

    body: list[cite.Sentence] = []
    claims: list[cite.Claim] = []
    for index, question_id in enumerate(order):
        finding = by_question.get(question_id)
        if finding is None:
            continue
        question_claim = _question_claim(scenario, question_id)
        claims.append(question_claim)
        body.append(_lead_sentence(index, finding, question_claim.id))
        body.extend(finding.sentences)
        claims.extend(finding.claims)
        section_sentences, section_claims = drafted.get(question_id, ([], []))
        body.extend(section_sentences)
        claims.extend(section_claims)

    dropped = 0
    if rewriter is not None:
        body, dropped = rewriter(body, _dedupe(claims))

    provenance, provenance_claims = _provenance(scenario, drafter, dropped)
    disclaimer, disclaimer_claim = draft_mod.disclaimer()
    sentences = [*body, *provenance, disclaimer]
    claims = [*claims, *provenance_claims, disclaimer_claim]

    return cite.Document(
        title=f"Gap report: {facility.slug} — {period_label}",
        kind=KIND,
        sentences=tuple(sentences),
        claims=tuple(_dedupe(claims)),
        generated_at=utc_now(),
        inputs=_inputs(facility, scenario, risk, period_label, drafter, dropped),
    )


def _dedupe(claims: Sequence[cite.Claim]) -> list[cite.Claim]:
    """One claim per id, first spelling wins — two rules may cite one field."""
    out: dict[str, cite.Claim] = {}
    for claim in claims:
        out.setdefault(claim.id, claim)
    return list(out.values())


def _inputs(
    facility: Facility,
    scenario: Scenario,
    risk: RiskLayer,
    period_label: str,
    drafter: str,
    dropped: int,
) -> dict[str, str]:
    """The footer: what this was built from, and the names the blinding replaces."""
    issued = ", ".join(
        f"issued/{name}/{one.period_label}.json ({one.validated_by})"
        for name, one in sorted(risk.issued.items())
    )
    return {
        "facility": facility.slug,
        "facility_blind": facility.blind_label,
        "partners": "; ".join(facility.partner_names()),
        "period": period_label,
        "scenario": f"{scenario.id} ({scenario.source_doc})",
        "drafter": drafter,
        "dropped_sentences": str(int(dropped)),
        "risk_layer": issued or f"nothing issued for {period_label}",
    }


# --------------------------------------------------------------------------- #
# Validating
# --------------------------------------------------------------------------- #


def resolver(
    facility: Facility,
    scenario: Scenario,
    risk: RiskLayer,
    *,
    guidance: Mapping[str, Mapping] | None = None,
) -> cite.DictResolver:
    """What a gap report may lean on: this record, this scenario, the layer, guidance."""
    return cite.DictResolver(
        {
            "facility": rules_mod.facility_refs(facility),
            "scenario": scenario.refs(),
            "issued": risk.issued_refs(),
            "ledger": risk.cards,
            "manifest": set(),
        },
        guidance,
    )


def forbidden_keys(payload: object, path: str = "") -> list[str]:
    """Every key in a rendered document that would name a place, if one appeared."""
    found: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.append(here)
            found.extend(forbidden_keys(value, here))
    elif isinstance(payload, (list, tuple)):
        for i, value in enumerate(payload):
            found.extend(forbidden_keys(value, f"{path}[{i}]"))
    return found


def check(
    doc: cite.Document,
    resolve: cite.Resolver,
    guidance: Mapping[str, Mapping] | None = None,
) -> list[cite.Violation]:
    """Every citation rule, then the no-coordinates rule, as one list."""
    found = cite.validate(doc, resolve, guidance)
    for key in forbidden_keys(cite.to_dict(doc)):
        found.append(cite.Violation(
            cite.UNRESOLVED, None,
            f"the document carries a forbidden field ({key}); the facility schema has "
            "no address or coordinate field and neither does a report built from it",
        ))
    return found


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_html(doc: cite.Document) -> str:
    """The page a planner reads: `cite`'s renderer, with the review note appended."""
    page = cite.render_html(doc)
    note = (
        '<footer class="review"><p>Decision support, not a decision. A practising '
        "emergency manager reviews every finding here; this report exists to make "
        "that review cheap, not to take it over. Every sentence above cites a field "
        "of the facility record, a constant of the scenario, a validated issuance or "
        "a named guidance document, and the document was written only after those "
        "citations validated.</p></footer>\n"
    )
    return page.replace("</body></html>", note + "</body></html>")


def blind_map(doc: cite.Document) -> dict[str, str]:
    """Slug and partner names to the labels a blinded reviewer sees, longest first."""
    slug = doc.inputs.get("facility", "")
    label = doc.inputs.get("facility_blind") or (
        f"FACILITY-{hashlib.sha256(slug.encode()).hexdigest()[:6]}" if slug else ""
    )
    mapping: dict[str, str] = {}
    partners = [p for p in doc.inputs.get("partners", "").split("; ") if p.strip()]
    for i, name in enumerate(partners, start=1):
        mapping[name] = f"PARTNER-{i}"
    if slug:
        mapping[slug] = label
    return dict(sorted(mapping.items(), key=lambda kv: (-len(kv[0]), kv[0])))


def _blind(text: str, mapping: Mapping[str, str]) -> str:
    for name, replacement in mapping.items():
        text = text.replace(name, replacement)
    return text


def blinded_document(doc: cite.Document) -> cite.Document:
    """The same document with every name replaced. Values that are names go too."""
    mapping = blind_map(doc)
    inputs = {
        key: _blind(value, mapping)
        for key, value in doc.inputs.items()
        if key != "facility_blind"
    }
    return cite.Document(
        title=_blind(doc.title, mapping),
        kind=doc.kind,
        sentences=tuple(
            cite.Sentence(_blind(s.text, mapping), s.claim_ids) for s in doc.sentences
        ),
        claims=tuple(
            cite.Claim(
                c.id, _blind(c.text, mapping),
                _blind(c.value, mapping) if isinstance(c.value, str) else c.value,
                c.source, c.fmt,
            )
            for c in doc.claims
        ),
        generated_at=doc.generated_at,
        inputs=inputs,
    )


def render_blinded(doc: cite.Document) -> tuple[str, str]:
    """The blinded page and its sha256 — the pair a review record binds to.

    The page carries a `readiness-blind` meta tag holding the blinded label,
    the period and the kind, which is everything `readiness review record`
    needs and nothing a reviewer should not have.
    """
    blinded = blinded_document(doc)
    meta = BLIND_META.format(
        label=blinded.inputs.get("facility", ""),
        period=blinded.inputs.get("period", ""),
        kind=blinded.kind,
    )
    page = render_html(blinded).replace("<style>", meta + "<style>", 1)
    return page, hashlib.sha256(page.encode("utf-8")).hexdigest()


def blind_details(page: str) -> tuple[str, str, str] | None:
    """`(label, period, kind)` from a blinded page, or None if it is not one."""
    match = _BLIND_META_RE.search(page)
    return (match.group(1), match.group(2), match.group(3)) if match else None


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def write(
    doc: cite.Document,
    out_dir: pathlib.Path | None = None,
    *,
    resolve: cite.Resolver,
    guidance: Mapping[str, Mapping] | None = None,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Validate, then write the three files — or raise and write nothing at all.

    `resolve` is required rather than derived from the document: a resolver
    built out of the document's own claims would accept whatever the document
    happened to say, which is the opposite of the point.
    """
    violations = check(doc, resolve, guidance)
    if violations:
        raise GapReportRefused(
            violations, f"refusing to write {doc.title}: {len(violations)} violation(s)"
        )
    slug = doc.inputs.get("facility") or ""
    period = doc.inputs.get("period") or ""
    if not (slug and period):
        raise GapReportError(
            "a gap report's inputs must name the facility and the period it covers"
        )
    directory = reports_root(out_dir) / slug
    directory.mkdir(parents=True, exist_ok=True)
    html_path = directory / f"{period}.html"
    json_path = directory / f"{period}.json"
    blind_path = directory / f"{period}.blind.html"
    page, _sha = render_blinded(doc)
    html_path.write_text(render_html(doc), encoding="utf-8")
    json_path.write_text(cite.to_json(doc), encoding="utf-8")
    blind_path.write_text(page, encoding="utf-8")
    return html_path, json_path, blind_path


# --------------------------------------------------------------------------- #
# The whole run
# --------------------------------------------------------------------------- #


def run(
    facility_path: pathlib.Path | str,
    period_label: str,
    *,
    scenario_id: str = scenarios_mod.DEFAULT_SCENARIO,
    out_dir: pathlib.Path | None = None,
    drafter: str = "local",
    scenarios_dir: pathlib.Path | None = None,
    issued_dir: pathlib.Path | None = None,
    experiments_dir: pathlib.Path | None = None,
    guidance: Mapping[str, Mapping] | None = None,
    rewriter: Rewriter | None = None,
) -> Report:
    """Load, run the rules, build, validate and write. Any refusal raises.

    `FacilityError` for a record we will not read, `ScenarioError` for a
    scenario we will not load, `GapReportRefused` when the document breaks a
    citation rule — in which case nothing at all is written.
    """
    facility = Facility.from_path(facility_path)
    scenario = scenarios_mod.load(scenario_id, scenarios_dir)
    problems = scenarios_mod.check_shape(scenario, rules_mod.RULES)
    if problems:
        raise GapReportError("; ".join(problems))
    counties = {facility.county_fips}
    counties |= {a.county_fips for a in facility.transfer_agreements}
    risk = RiskLayer.load(
        period_label, counties, issued_dir=issued_dir, experiments_dir=experiments_dir
    )
    if drafter == "claude" and rewriter is None:
        from readiness.agent import planner  # local import: the optional backend

        rewriter = planner.rewriter()
    findings = rules_mod.run(facility, risk, scenario)
    doc = build(
        facility, scenario, findings, risk, period_label,
        drafter=drafter, rewriter=rewriter,
    )
    resolve = resolver(facility, scenario, risk, guidance=guidance)
    html_path, json_path, blind_path = write(
        doc, out_dir, resolve=resolve, guidance=guidance
    )
    _page, sha = render_blinded(doc)
    return Report(
        document=doc, findings=tuple(findings), html_path=html_path,
        json_path=json_path, blind_path=blind_path, blind_sha256=sha,
    )


__all__ = [
    "BLIND_META",
    "DRAFTERS",
    "KIND",
    "REPORTS_DIR",
    "REPORTS_DIR_ENV",
    "FacilityError",
    "GapReportError",
    "GapReportRefused",
    "Report",
    "blind_details",
    "blind_map",
    "blinded_document",
    "build",
    "check",
    "forbidden_keys",
    "render_blinded",
    "render_html",
    "reports_root",
    "resolver",
    "run",
    "write",
]
