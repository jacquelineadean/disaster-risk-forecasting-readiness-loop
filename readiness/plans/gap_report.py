"""The gap report: one cited document per facility, per period, per scenario.

The shape is the county brief's, because the rule is the same one (report §7):
every sentence cites a computed number, a dataset row or a named guidance
document, the whole document goes through `readiness.cite.validate`, and if a
single rule is broken nothing is written and the violations are printed.

Three files come out of a clean run, in two trees:

* `<slug>/<period>.json` — the `cite.Document` itself, so anyone can re-run
  the validator on the file and get the answer the command got;
* `<slug>/<period>.html` — the rendered page the planner reads, with the
  legend saying which building `PARTNER-1` and `COUNTY-A` are;
* `blinded/<label>/<period>.blind.html` — the same document with no legend,
  no slug, no names, no county FIPS and no timestamp. It sits in its own
  directory because a path is as good as a name: nothing under `blinded/`
  carries the slug in a file name, a title or a meta tag. That file is what
  goes to a reviewer, and its sha256 is what a review record binds a rating
  to.

Blinding is **structural**, not a search-and-replace over prose. No rule ever
writes a name: sentences carry `PARTNER-n`, `COUNTY-x` and `DOCUMENT-n`, the
names live in the claims those sentences cite, and the blinded document drops
the naming tail of each claim, rebuilds the title and the footer from the
labels and re-keys the county-bearing claim ids. `render_blinded` then checks
its own output: if any string that identifies the record — the slug, a
partner's name or any two consecutive words of it, an evidence document, a
county FIPS — survives into the page, it raises and nothing is written.

The blinded page carries **no `generated_at`**, so the same record and the
same period render to the same bytes and the same sha every time. Re-running
`readiness gap-report` after a review therefore does not orphan that review;
only a change to what the report *says* does, which is the property the
binding exists for. The JSON and the plain HTML keep their timestamp.

None of this is a warning product and none of it is in charge. A gap report is
an analysis a practising emergency manager reads, argues with and signs — the
agent's job is to make that analysis cheap, which is what report §6 asks for.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import html
import os
import pathlib
import re
from typing import Callable, Mapping, Sequence

from readiness import cite
from readiness.plans import draft as draft_mod
from readiness.plans import rules as rules_mod
from readiness.plans import scenarios as scenarios_mod
from readiness.plans import facility as facility_mod
from readiness.plans.facility import Facility, FacilityError
from readiness.plans.risk import RiskLayer
from readiness.plans.scenarios import Scenario

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
#: Where `readiness gap-report` writes. Never committed: a real facility's
#: report names a real building, and `.gitignore` keeps the tree empty.
REPORTS_DIR = REPO_ROOT / "plans" / "reports"
REPORTS_DIR_ENV = "READINESS_REPORTS_DIR"

#: The document kind a review record and `verify --phase 3` look for.
KIND = "gap-report"

#: Where the blinded renders go, under the reports root. Nothing below it
#: carries the slug — not in a directory name, a file name, a title or a tag.
BLINDED_DIR = "blinded"

#: A period label: `YYYY`, `YYYY-Qn` or `YYYY-Mnn`. The same grammar
#: `readiness.issue.parse_period` enforces, restated because Phase 3 has no
#: contract in scope to ask.
PERIOD_RE = re.compile(r"^\d{4}(-(Q[1-4]|M(0[1-9]|1[0-2])))?$")

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
    scenario: Scenario, drafter: str, refused: int, offered: int
) -> tuple[list[cite.Sentence], list[cite.Claim]]:
    """Two sentences a reader needs to trust the rest: what ran, and what was refused.

    A refused rewrite is never a deletion. The body has exactly the local
    report's sentences whichever drafter ran; some of them were reworded, and
    this line says how many were not.
    """
    what = cite.Claim(
        id="provenance-scenario",
        text=f"the scenario this report runs: {scenario.id} ({scenario.title})",
        value=None,
        source=cite.Source("scenario", scenario.id),
    )
    kept = cite.Claim(
        id="provenance-refused",
        text=(
            "drafted sentences the citation validator refused, which kept the local "
            "drafter's wording instead"
        ),
        value=int(refused),
        source=cite.Source("computed", what.id),
        fmt="{:d}",
    )
    total = cite.Claim(
        id="provenance-drafted",
        text="sentences offered to the drafter for rewording",
        value=int(offered),
        source=cite.Source("computed", what.id),
        fmt="{:d}",
    )
    sentences = [
        cite.Sentence.from_text(
            f"This report runs the scenario and the facility record named in its "
            f"footer, with prose from the {drafter} drafter [c:{what.id}]."
        ),
        cite.Sentence.from_text(
            f"{cite.render_value(kept)} of {cite.render_value(total)} drafted "
            f"sentences were refused and kept their local wording "
            f"[c:{kept.id}][c:{total.id}]."
        ),
    ]
    return sentences, [what, kept, total]


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
            raise GapReportError(
                f"scenario {scenario.id} asks question {question_id} and no rule "
                "produced a finding for it; a question that silently vanishes from "
                "the report is not a question the plan was asked"
            )
        question_claim = _question_claim(scenario, question_id)
        claims.append(question_claim)
        body.append(_lead_sentence(index, finding, question_claim.id))
        body.extend(finding.sentences)
        claims.extend(finding.claims)
        section_sentences, section_claims = drafted.get(question_id, ([], []))
        body.extend(section_sentences)
        claims.extend(section_claims)

    offered = len(body)
    refused = 0
    if rewriter is not None:
        body, refused = rewriter(body, _dedupe(claims))
        if len(body) != offered:
            raise GapReportError(
                f"the drafter returned {len(body)} sentences for {offered}; a "
                "rewrite is a rewording, and a refused one keeps its local wording"
            )

    provenance, provenance_claims = _provenance(scenario, drafter, refused, offered)
    disclaimer, disclaimer_claim = draft_mod.disclaimer()
    sentences = [*body, *provenance, disclaimer]
    claims = [*claims, *provenance_claims, disclaimer_claim]

    return cite.Document(
        title=f"Gap report: {facility.slug} — {period_label}",
        kind=KIND,
        sentences=tuple(sentences),
        claims=tuple(_dedupe(claims)),
        generated_at=utc_now(),
        inputs=_inputs(
            facility, scenario, risk, period_label, drafter, refused, offered
        ),
    )


def _dedupe(claims: Sequence[cite.Claim]) -> list[cite.Claim]:
    """One claim per id, first spelling wins — two rules may cite one field."""
    out: dict[str, cite.Claim] = {}
    for claim in claims:
        out.setdefault(claim.id, claim)
    return list(out.values())


#: How a legend entry is spelled in `inputs`, and how the blinded render finds
#: the strings it must not let through: `LABEL = what the record calls it`.
LEGEND_SEP = " = "
LEGEND_JOIN = "; "

#: The `inputs` keys that hold `LABEL = name` legends. Each is rewritten to
#: labels alone in the blinded document, and each is a source of the strings
#: `render_blinded` refuses to let through.
LEGEND_KEYS: tuple[str, ...] = ("partners", "counties", "documents")


def _legend(labels: Mapping[str, str]) -> str:
    """`{name: label}` as `LABEL = name; LABEL = name`, in label order."""
    return LEGEND_JOIN.join(
        f"{label}{LEGEND_SEP}{name}" for name, label in labels.items()
    )


def read_legend(value: str) -> list[tuple[str, str]]:
    """A legend input back as `(label, name)` pairs; a blinded one has no names."""
    out: list[tuple[str, str]] = []
    for entry in value.split(LEGEND_JOIN):
        entry = entry.strip()
        if not entry:
            continue
        label, _sep, name = entry.partition(LEGEND_SEP)
        out.append((label.strip(), name.strip()))
    return out


def _inputs(
    facility: Facility,
    scenario: Scenario,
    risk: RiskLayer,
    period_label: str,
    drafter: str,
    refused: int,
    offered: int,
) -> dict[str, str]:
    """The footer: what this was built from, and the legend for its labels.

    The three legends — partners, counties, documents — are the only place a
    name appears outside a claim's text, and they are what `blinded_document`
    rewrites and `render_blinded` checks its output against. They are data,
    not decoration: the unblinded page renders them as a legend so a planner
    can read `PARTNER-2` as a building, and the blinded page carries the
    labels alone.
    """
    issued = ", ".join(
        f"issued/{name}/{one.period_label}.json ({one.validated_by})"
        for name, one in sorted(risk.issued.items())
    )
    return {
        "facility": facility.slug,
        "facility_blind": facility.blind_label,
        "partners": _legend(facility.partner_labels()),
        "counties": _legend(facility.county_labels()),
        "documents": _legend(facility.document_labels()),
        "period": period_label,
        "scenario": f"{scenario.id} ({scenario.source_doc})",
        "drafter": drafter,
        "refused_sentences": str(int(refused)),
        "drafted_sentences": str(int(offered)),
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
    """Every place a rendered document names, by key **or** by value.

    The key scan is the tripwire the facility schema also carries; the value
    scan is the one that matters here, because a document's keys are fixed
    (`cite.to_dict` decides them) and its *values* are whatever a record, a
    legend or a drafter put there. A street address in an evidence document's
    title is an address however the key is spelled.

    A bare five-digit number is not a finding: that is a county FIPS, the one
    geography report §7 publishes. The name is historical; it scans both.
    """
    return [
        f"{spot}: {what}" for spot, what in facility_mod.forbidden_in(payload, path)
    ]


def identifiers(doc: cite.Document) -> tuple[str, ...]:
    """The strings this document may spell out although they look like numbers.

    Its own labels — `PARTNER-2`, `COUNTY-A`, `DOCUMENT-3`, `FACILITY-<12
    hex>` — and the county FIPS its legend names. Every one of them is a name
    this document assigns and prints; any *other* number in a sentence is a
    quantity and must be the rendered value of a claim it cites.
    """
    found: list[str] = []
    for key in LEGEND_KEYS:
        for label, name in read_legend(doc.inputs.get(key, "")):
            found.append(label)
            if key == "counties" and name:
                found.append(name)
    blind = doc.inputs.get("facility_blind") or ""
    if blind:
        found.append(blind)
    facility = doc.inputs.get("facility") or ""
    if facility.startswith("FACILITY-"):
        found.append(facility)
    return tuple(dict.fromkeys(f for f in found if f))


def check(
    doc: cite.Document,
    resolve: cite.Resolver,
    guidance: Mapping[str, Mapping] | None = None,
    *,
    exempt: Sequence[str] | None = None,
) -> list[cite.Violation]:
    """Every citation rule, then the no-place rule, as one list.

    `exempt` defaults to the document's own labels, the same way a brief's
    check defaults to the county it is about: a report that says `PARTNER-2`
    is naming a partner, and every other number in it is a quantity.
    """
    exempt = identifiers(doc) if exempt is None else tuple(exempt)
    found = cite.validate(doc, resolve, guidance, identifiers=exempt)
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


def legend(doc: cite.Document) -> list[tuple[str, str, str]]:
    """`(what, label, name)` for every label this document uses, or `[]`.

    Empty for a blinded document, which is the point: the legend is the map
    from `PARTNER-2` to a building, and it is the one thing a reviewer must
    not be handed.
    """
    out: list[tuple[str, str, str]] = []
    for key in LEGEND_KEYS:
        for label, name in read_legend(doc.inputs.get(key, "")):
            if label and name:
                out.append((key, label, name))
    return out


def _legend_html(doc: cite.Document) -> str:
    entries = legend(doc)
    if not entries:
        return ""
    rows = "\n".join(
        f"<li><strong>{html.escape(label)}</strong> — {html.escape(name)} "
        f'<span class="source">({html.escape(what)})</span></li>'
        for what, label, name in entries
    )
    return (
        '<h2>Labels</h2>\n<p class="source">Every sentence above refers to a '
        "partner, a county or a document by a label. This legend is on the plain "
        "report only; the blinded rendering a reviewer reads has no legend and no "
        "names.</p>\n"
        f'<ul class="legend">\n{rows}\n</ul>\n'
    )


def render_html(doc: cite.Document) -> str:
    """The page a planner reads: `cite`'s renderer, the legend, the review note."""
    page = cite.render_html(doc)
    note = (
        '<footer class="review"><p>Decision support, not a decision. A practising '
        "emergency manager reviews every finding here; this report exists to make "
        "that review cheap, not to take it over. Every sentence above cites a field "
        "of the facility record, a constant of the scenario, a validated issuance or "
        "a named guidance document, and the document was written only after those "
        "citations validated.</p></footer>\n"
    )
    page = page.replace("<h2>Sources</h2>", _legend_html(doc) + "<h2>Sources</h2>", 1)
    return page.replace("</body></html>", note + "</body></html>")


def blind_map(doc: cite.Document) -> dict[str, str]:
    """Every identifying string this document holds, to the label that replaces it.

    Built from the document's own legends and its slug — from the record, in
    other words, not from a scan of the prose. Longest first, so a name that
    contains another is replaced before its own substring is.
    """
    mapping: dict[str, str] = {}
    for key in LEGEND_KEYS:
        for label, name in read_legend(doc.inputs.get(key, "")):
            if name:
                mapping[name] = label
    slug = doc.inputs.get("facility", "")
    label = doc.inputs.get("facility_blind", "")
    if slug and label:
        mapping[slug] = label
    return dict(sorted(mapping.items(), key=lambda kv: (-len(kv[0]), kv[0])))


def _blind(text: str, mapping: Mapping[str, str]) -> str:
    for name, replacement in mapping.items():
        text = text.replace(name, replacement)
    return text


def _blind_claim_id(claim_id: str, mapping: Mapping[str, str]) -> str:
    """A claim id with any county FIPS in it replaced by the county's label.

    `risk-flood-zz-99001` names the county in the one place a reader of the
    blinded page cannot miss: the superscript link beside the sentence.
    """
    for name, replacement in mapping.items():
        if name in claim_id:
            claim_id = claim_id.replace(name, replacement)
    return claim_id


def blinded_document(doc: cite.Document) -> cite.Document:
    """The same document with every identifying string structurally removed.

    Four transformations, each on a part of the document the *builder* put the
    name into, so none of them depends on guessing where a name might be:

    * the title and the `facility`/legend inputs are rebuilt from the labels;
    * every claim's text is cut at `rules.NAME_MARK`, which is where a claim
      stops describing a field and starts quoting the record's name for it;
    * any remaining identifying string in a claim's text or string value is
      replaced by its label (belt and braces: a claim built elsewhere);
    * claim ids, the markers that cite them and `claim_ids` are re-keyed so a
      county FIPS does not survive in a link.

    `generated_at` is dropped: the blinded page is the thing a review binds to,
    and it must not move because the command was run twice.
    """
    mapping = blind_map(doc)
    counties = {
        name: label
        for label, name in read_legend(doc.inputs.get("counties", "")) if name
    }
    label = doc.inputs.get("facility_blind", "") or doc.inputs.get("facility", "")
    period = doc.inputs.get("period", "")

    inputs = {
        key: value for key, value in doc.inputs.items()
        if key not in ("facility_blind", *LEGEND_KEYS)
    }
    inputs["facility"] = label
    for key in LEGEND_KEYS:
        if key in doc.inputs:
            inputs[key] = LEGEND_JOIN.join(
                lab for lab, _name in read_legend(doc.inputs[key])
            )
    inputs = {key: _blind(value, mapping) for key, value in inputs.items()}

    ids = {c.id: _blind_claim_id(c.id, counties) for c in doc.claims}

    def sentence(one: cite.Sentence) -> cite.Sentence:
        text = one.text
        for old_id, new_id in ids.items():
            if old_id != new_id:
                text = text.replace(f"[c:{old_id}]", f"[c:{new_id}]")
        # Deliberately no string substitution here. A rule never writes a name
        # into a sentence, so a name in one is a defect — and replacing it
        # would hide the defect and mangle the prose of any record whose slug
        # happens to be an ordinary word. `render_blinded` refuses instead.
        return cite.Sentence(text, tuple(ids.get(cid, cid) for cid in one.claim_ids))

    def claim(one: cite.Claim) -> cite.Claim:
        text = one.text.split(rules_mod.NAME_MARK)[0]
        source = one.source
        if source.kind == "computed":
            source = cite.Source(
                "computed",
                "+".join(ids.get(part, part) for part in source.ref.split("+")),
            )
        return cite.Claim(
            ids.get(one.id, one.id),
            _blind(text, mapping),
            _blind(one.value, mapping) if isinstance(one.value, str) else one.value,
            source,
            one.fmt,
        )

    return cite.Document(
        title=f"Gap report: {label} — {period}" if period else f"Gap report: {label}",
        kind=doc.kind,
        sentences=tuple(sentence(s) for s in doc.sentences),
        claims=tuple(claim(c) for c in doc.claims),
        generated_at="",
        inputs=inputs,
    )


def identifying_strings(doc: cite.Document) -> tuple[str, ...]:
    """Every string that would identify the record, case-folded and collapsed.

    The slug, every partner and co-tenant name, every evidence document and
    every county FIPS — the keys of `blind_map`, normalised the way prose is
    normalised so that a re-cased or double-spaced spelling is the same
    string. Nothing shorter: a two-word fragment of a name is as often
    ordinary English ("the county") as it is identifying, and a guard that
    refuses good reports is a guard that gets turned off.
    """
    return tuple(dict.fromkeys(
        " ".join(value.split()).casefold() for value in blind_map(doc) if value.strip()
    ))


def _leaks(page: str, doc: cite.Document) -> list[str]:
    haystack = " ".join(page.split()).casefold()
    return [s for s in identifying_strings(doc) if s in haystack]


def render_blinded(doc: cite.Document) -> tuple[str, str]:
    """The blinded page and its sha256 — the pair a review record binds to.

    The page carries a `readiness-blind` meta tag holding the blinded label,
    the period and the kind, which is everything `readiness review record`
    needs and nothing a reviewer should not have. It carries no timestamp, so
    two renders of one document are one file.

    The render then checks itself: a page still holding the slug, a partner's
    name (or two consecutive words of one), an evidence document or a county
    FIPS raises `GapReportError` rather than being returned. A blinding that
    failed must refuse, not ship.
    """
    blinded = blinded_document(doc)
    meta = BLIND_META.format(
        label=html.escape(blinded.inputs.get("facility", ""), quote=True),
        period=html.escape(blinded.inputs.get("period", ""), quote=True),
        kind=html.escape(blinded.kind, quote=True),
    )
    page = render_html(blinded).replace("<style>", meta + "<style>", 1)
    leaks = _leaks(page, doc)
    if leaks:
        raise GapReportError(
            f"the blinded rendering of {doc.title!r} still holds "
            f"{leaks[:3]}, so it is not blinded and nothing is written. A slug or "
            "a partner name that is an ordinary English word cannot be removed "
            "from prose by any mechanism; give the record a distinctive slug, and "
            "check that no sentence was rewritten to name a building"
        )
    return page, hashlib.sha256(page.encode("utf-8")).hexdigest()


def blind_details(page: str) -> tuple[str, str, str] | None:
    """`(label, period, kind)` from a blinded page, or None if it is not one."""
    match = _BLIND_META_RE.search(page)
    return (match.group(1), match.group(2), match.group(3)) if match else None


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def check_period(period: str) -> str:
    """The period label, or a refusal. It is a path component and it is markup.

    `PERIOD_RE` is `readiness.issue`'s grammar restated rather than imported:
    a gap report has no contract in scope, so there is nothing to ask what
    shape a period takes here, and `tests/test_gap_report.py` asserts the two
    agree. Without this, `--period ../../case-studies/leaked` writes a report
    naming a building into a tracked directory.
    """
    if not PERIOD_RE.fullmatch(str(period)):
        raise GapReportError(
            f"period {period!r} is not a period label; expected YYYY, YYYY-Qn "
            f"(n 1-4) or YYYY-Mnn (nn 01-12). The label names the file this "
            f"report is written to and the issued files it reads, so it is "
            f"checked before either happens"
        )
    return str(period)


def _write_atomically(pairs: Sequence[tuple[pathlib.Path, str]]) -> None:
    """Write every file, or leave the tree exactly as it was.

    Each string goes to a `.tmp` sibling first and the temporaries are only
    swapped in once all of them are on disk, so a failure part-way through
    cannot leave `verify --phase 3` reading a report whose blinded page is
    missing. A target that is already a directory is refused before anything
    is written at all, because `os.replace` onto one fails after the earlier
    swaps have happened.
    """
    for path, _text in pairs:
        if path.is_dir():
            raise GapReportError(
                f"{path} is a directory, so the report cannot be written there"
            )
    temporaries = [path.with_name(path.name + ".tmp") for path, _ in pairs]
    try:
        for (path, text), tmp in zip(pairs, temporaries):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
        for (path, _text), tmp in zip(pairs, temporaries):
            os.replace(tmp, path)
    except BaseException:
        for tmp in temporaries:
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


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

    The blinded page goes under `blinded/<label>/`, not beside the plain pair:
    a directory named after the slug, holding the blinded page next to the
    named one, de-blinds the report without a hash being computed.
    """
    violations = check(doc, resolve, guidance)
    if violations:
        raise GapReportRefused(
            violations, f"refusing to write {doc.title}: {len(violations)} violation(s)"
        )
    slug = doc.inputs.get("facility") or ""
    period = doc.inputs.get("period") or ""
    label = doc.inputs.get("facility_blind") or ""
    if not (slug and period and label):
        raise GapReportError(
            "a gap report's inputs must name the facility, its blinded label and "
            "the period it covers"
        )
    check_period(period)
    if not facility_mod.BLIND_LABEL_RE.match(label):
        raise GapReportError(
            f"blinded label {label!r} is not {facility_mod.BLIND_LABEL_RE.pattern}"
        )
    root = reports_root(out_dir)
    html_path = root / slug / f"{period}.html"
    json_path = root / slug / f"{period}.json"
    blind_path = root / BLINDED_DIR / label / f"{period}.blind.html"
    page, _sha = render_blinded(doc)
    _write_atomically([
        (html_path, render_html(doc)),
        (json_path, cite.to_json(doc)),
        (blind_path, page),
    ])
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
    scenario we will not load, `GapReportError` for a period label that is not
    one, `GapReportRefused` when the document breaks a citation rule — in
    which case nothing at all is written.
    """
    check_period(period_label)
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
    "BLINDED_DIR",
    "BLIND_META",
    "DRAFTERS",
    "KIND",
    "LEGEND_JOIN",
    "LEGEND_KEYS",
    "LEGEND_SEP",
    "PERIOD_RE",
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
    "check_period",
    "forbidden_keys",
    "identifiers",
    "identifying_strings",
    "legend",
    "read_legend",
    "render_blinded",
    "render_html",
    "reports_root",
    "resolver",
    "run",
    "write",
]
