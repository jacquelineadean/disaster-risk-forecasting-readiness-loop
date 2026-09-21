"""Citations: the rules every human-facing document passes before it is shown.

Report §7: "Numbers come from the harness; prose comes from the model — and
prose can hallucinate. Require every plan sentence to cite either a computed
number, a dataset row, or a named guidance document, validated by rules
before display." And, from the same section: this is decision support, never
a warning channel; region-period probabilities must never be phrased as
predictions of specific events.

So a `Document` is sentences plus the claims they cite, and `validate` checks
what rules can check: every sentence cites at least one claim, every cited
claim exists, every claim's source resolves through a `Resolver`, every cited
value equals what the artefact holds where the reference names one number,
every number in the prose is the rendered value of a cited claim, and no
forbidden phrasing appears. It is a smoke alarm for words — it proves a
number was not invented, not that the citation supports the sentence; that
remains a human's job, and the brief and the gap report both say so.

Two things are the caller's, not this module's: the guidance registry (the
browser sandbox has no `plans/` directory, so it passes one in) and the
`identifiers` a document may spell out although they look like numbers — a
brief passes the county FIPS it is about, and a document that passes nothing
has every five-digit number read as a number.

This module knows nothing about ledgers, issued files, manifests or facilities
beyond what a `Resolver` answers. Standard library only, no LLM client, no
labels, no scoring: it is used to check model prose, so it must not depend on
either side of that seam.
"""

from __future__ import annotations

import dataclasses
import html
import json
import math
import pathlib
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Protocol

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
GUIDANCE_PATH = REPO_ROOT / "plans" / "guidance.json"

#: Where a claim can come from. `computed` refers to other claims by id.
KINDS: frozenset[str] = frozenset(
    {"ledger", "issued", "manifest", "guidance", "facility", "scenario", "computed"}
)

UNCITED = "UNCITED"
UNKNOWN_CLAIM = "UNKNOWN_CLAIM"
UNRESOLVED = "UNRESOLVED"
#: The claim resolved, and the artefact holds a different number.
VALUE_MISMATCH = "VALUE_MISMATCH"
NUMBER_WITHOUT_CLAIM = "NUMBER_WITHOUT_CLAIM"
FORBIDDEN_PHRASE = "FORBIDDEN_PHRASE"

#: Report §7: probabilities are never phrased as predictions of specific
#: events, and the product is never a warning channel. Matched case-insensitively
#: on word boundaries, with any suffix ("warnings", "alerted").
FORBIDDEN_PHRASES: tuple[str, ...] = (
    "will occur", "will hit", "will strike", "is predicted to hit", "warning", "alert",
)

#: The one sentence allowed to say "warning" and "alert": the disclaimer every
#: surface carries, verbatim, citing the guidance entry for official alerting.
NOT_A_WARNING_SENTENCE = (
    "Not a warning product; official alerts come from the NWS and IPAWS"
)
NOT_A_WARNING_GUIDANCE = "nws-ipaws"

_MARKER = re.compile(r"\s*\[c:([^\]\s]+)\]")
_CLAIM_ID = re.compile(r"[A-Za-z0-9_.:-]+")
#: A numeric token: digits with thousands separators, an optional fraction and
#: an optional percent sign, with a sign only when it does not follow a word
#: ("+0.23" is a signed skill score; the "+12" in "T+12" is not).
_NUMBER = re.compile(r"(?:(?<!\w)[-+])?\d[\d,]*(?:\.\d+)?%?")
_FORBIDDEN = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in FORBIDDEN_PHRASES) + r")\w*", re.IGNORECASE
)
#: Identifier exemptions: strings that contain digits but are names, not
#: quantities, whatever the document is. Guidance aliases and the caller's own
#: `identifiers` are added at validation time.
#: Each may end a sentence, so a trailing "." is fine but ".5" or ",000" is not.
#:
#: A bare five-digit number is *not* here. It is a county FIPS only in a
#: document that is about that county, and the document says which one: a
#: brief passes its own FIPS to `validate`, and a document that passes
#: nothing has five-digit numbers read as numbers, like any other.
_IDENTIFIERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?!\w|\.\d)"),  # semantic versions ("v1.2.0")
    re.compile(r"(?<!\w)exp-\d{4}(?!\w)"),  # card ids
    re.compile(r"(?<![\w.])\d{4}-[QMP]\d+(?!\w|\.\d)"),  # period labels
    re.compile(r"(?<![\d.,])(?:19|20)\d{2}(?!\d|[.,]\d|%)"),  # bare years 1900-2099
)


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Source:
    """Where a claim comes from: a kind in `KINDS` and a reference string.

    The reference grammar is per kind and belongs to the resolver, not here:
    a card id optionally followed by `#field`, an issued file path plus its
    period, a manifest key, a guidance id, a facility field path such as
    `power.fuel_hours`, a scenario id plus constant name, or for `computed`
    the ids of the claims it was derived from joined by `+`.
    """

    kind: str
    ref: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown source kind {self.kind!r}; expected one of "
                             f"{sorted(KINDS)}")
        if not isinstance(self.ref, str) or not self.ref.strip():
            raise ValueError(f"source ref must be a non-empty string, got {self.ref!r}")


@dataclasses.dataclass(frozen=True)
class Claim:
    """One thing a sentence may lean on: a value, where it came from, how it reads."""

    id: str
    text: str
    value: int | float | str | None
    source: Source
    fmt: str = ""

    def __post_init__(self) -> None:
        if not _CLAIM_ID.fullmatch(self.id):
            raise ValueError(f"claim id {self.id!r} must match {_CLAIM_ID.pattern}")
        if isinstance(self.value, bool) or not isinstance(
            self.value, (int, float, str, type(None))
        ):
            raise ValueError(f"claim {self.id}: value must be a number, string or None")


@dataclasses.dataclass(frozen=True)
class Sentence:
    """Prose with inline markers `[c:ID]` after the clause each claim supports.

    `claim_ids` lists the citations; `from_text` derives them from the markers
    so a caller need not say the same thing twice. Validation counts both, so
    a marker in the text and an id in the list are each a citation.
    """

    text: str
    claim_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_ids", tuple(self.claim_ids))

    @classmethod
    def from_text(cls, text: str) -> "Sentence":
        return cls(text, markers_in(text))

    def cited(self) -> tuple[str, ...]:
        """Every id this sentence cites, listed once, in order of appearance."""
        return tuple(dict.fromkeys((*self.claim_ids, *markers_in(self.text))))


@dataclasses.dataclass(frozen=True)
class Document:
    """A human-facing document: what it says, what it leans on, where it came from."""

    title: str
    kind: str
    sentences: tuple[Sentence, ...]
    claims: tuple[Claim, ...]
    generated_at: str
    inputs: dict[str, str] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sentences", tuple(self.sentences))
        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(self, "inputs", dict(self.inputs))
        seen: set[str] = set()
        for claim in self.claims:
            if claim.id in seen:
                raise ValueError(f"duplicate claim id {claim.id!r}")
            seen.add(claim.id)

    def claim_index(self) -> dict[str, Claim]:
        return {claim.id: claim for claim in self.claims}


@dataclasses.dataclass(frozen=True)
class Violation:
    """One rule broken. `sentence_index` is None for document-level findings."""

    code: str
    sentence_index: int | None
    detail: str

    def __str__(self) -> str:
        where = "document" if self.sentence_index is None else f"sentence {self.sentence_index}"
        return f"{self.code} ({where}): {self.detail}"


# --------------------------------------------------------------------------- #
# Resolvers
# --------------------------------------------------------------------------- #


class Resolver(Protocol):
    """Answers what a source holds, in three ways.

    A **string** is a refusal: the source does not exist, and it says why.
    A **number** is the leaf the reference names — the field of a card, the
    county's probability in an issued file — and `validate` checks the claim's
    value against it. **None** means the source resolves but there is no single
    value under it to check (a whole card, a manifest key, a guidance entry),
    which is "resolvable, unchecked".

    Only numbers come back as leaves: a failure is a string on this channel,
    so a string leaf could not be told from one.
    """

    def resolve(self, source: Source) -> str | int | float | None: ...


def load_guidance(path: pathlib.Path = GUIDANCE_PATH) -> dict[str, dict]:
    """The named guidance documents a sentence may cite, by id.

    The file is the registry: a citation to a guidance id that is not in it
    does not resolve, so prose cannot invent a standard.
    """
    raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    entries = raw.get("documents") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{path}: expected an object with a 'documents' list")
    out: dict[str, dict] = {}
    for entry in entries:
        for key in ("id", "title", "publisher", "year", "url", "section", "aliases"):
            if key not in entry:
                raise ValueError(f"{path}: guidance entry missing {key!r}: {entry}")
        if entry["id"] in out:
            raise ValueError(f"{path}: duplicate guidance id {entry['id']!r}")
        out[entry["id"]] = entry
    return out


def guidance_aliases(guidance: Mapping[str, Mapping]) -> tuple[str, ...]:
    """Every alias, longest first, so "42 CFR 482.15" is removed before "482.15"."""
    aliases = {alias for entry in guidance.values() for alias in entry.get("aliases", ())}
    return tuple(sorted(aliases, key=lambda a: (-len(a), a)))


def _walk(node: object, path: str) -> tuple[bool, object]:
    """Follow a dotted field path through nested mappings: (found, what is there)."""
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _leaf(value: object) -> int | float | None:
    """The value a claim can be checked against: a number, or nothing at all."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


class DictResolver:
    """A resolver over in-memory sets, for tests and simple callers.

    `known` maps a kind to what exists: a collection of refs, or a mapping from
    a base ref to either a nested mapping (a card, a facility, an issued file's
    periods and the probability each holds per county) whose field paths may be
    cited with `#field` or `#field.subfield`, or a collection of accepted
    qualifiers. Guidance ids come from `plans/guidance.json` unless given.
    `computed` claims are checked against the document by `validate`, not here.

    Where a reference lands on a number, that number is returned, so the claim
    that cites it is checked against it rather than merely against the fact
    that something is there.
    """

    def __init__(
        self,
        known: Mapping[str, Collection[str] | Mapping[str, object]] | None = None,
        guidance: Mapping[str, Mapping] | None = None,
    ) -> None:
        self.known = dict(known or {})
        self.guidance = dict(guidance) if guidance is not None else load_guidance()

    def resolve(self, source: Source) -> str | int | float | None:
        if source.kind == "computed":
            return None
        if source.kind == "guidance":
            if source.ref in self.guidance or source.ref in self.known.get("guidance", ()):
                return None
            return f"guidance id {source.ref!r} is not in the registry"
        refs = self.known.get(source.kind)
        if refs is None:
            return f"no {source.kind} sources are known"
        base, _, qualifier = source.ref.partition("#")
        if isinstance(refs, Mapping):
            # The whole base first: a path is a key here ("issued/x/2026-Q4.json"
            # has dots in it), and only then a dotted walk ("power.fuel_hours").
            found, target = (True, refs[base]) if base in refs else _walk(refs, base)
        else:
            found, target = base in refs, None
        if not found:
            return f"{source.kind} {base!r} not found"
        if qualifier:
            return _check_qualifier(source, target, qualifier)
        return _leaf(target)


def _check_qualifier(
    source: Source, target: object, qualifier: str
) -> str | int | float | None:
    if isinstance(target, Mapping):
        found, value = _walk(target, qualifier)
        if found:
            return _leaf(value)
        return f"{source.kind} {source.ref!r}: no field {qualifier!r}"
    if isinstance(target, Collection) and not isinstance(target, str):
        if qualifier in target:
            return None
        return f"{source.kind} {source.ref!r}: {qualifier!r} not covered"
    return None


# --------------------------------------------------------------------------- #
# Rendering values and finding numbers
# --------------------------------------------------------------------------- #


def render_value(claim: Claim) -> str:
    """The value as the prose must spell it: through `fmt`, or plainly."""
    if claim.value is None:
        return ""
    if claim.fmt:
        return claim.fmt.format(claim.value)
    return str(claim.value)


def markers_in(text: str) -> tuple[str, ...]:
    return tuple(_MARKER.findall(text))


def strip_markers(text: str) -> str:
    return _MARKER.sub("", text)


def _normalise(token: str) -> str:
    return token.lstrip("+").rstrip(".,")


def numbers_in(
    text: str,
    aliases: Iterable[str] = (),
    identifiers: Iterable[str] = (),
    *,
    exempt_identifiers: bool = True,
) -> list[str]:
    """Numeric tokens in prose, once markers and identifiers are set aside.

    Identifiers contain digits but are not quantities: guidance aliases such
    as "CPG 101" or "42 CFR 482.15", semantic versions, card ids, period
    labels and bare four-digit years. `identifiers` are the caller's own — the
    county FIPS a brief is about, say — because whether "48201" is a name or a
    number depends on the document, not on the shape of the digits. A scenario
    constant ("96" in "96-hour") is deliberately not an identifier — it is a
    number the plan leans on, so it must be a claim.

    `exempt_identifiers=False` turns every exemption off and returns the raw
    digit runs. That is what a caller scanning **model output** wants: a ZIP
    code reads as a county FIPS and an invented date reads as a year, so a
    rewriter checked with the exemptions on can introduce either. The
    document's own validation keeps them on; the rewrite gate turns them off.
    """
    clean = strip_markers(text)
    if exempt_identifiers:
        for alias in aliases:
            clean = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", " ", clean)
        for name in identifiers:
            clean = re.sub(
                r"(?<![\w.,])" + re.escape(str(name)) + r"(?![\w]|[.,]\d)", " ", clean
            )
        for pattern in _IDENTIFIERS:
            clean = pattern.sub(" ", clean)
    tokens = (_normalise(m.group(0)) for m in _NUMBER.finditer(clean))
    return [t for t in tokens if t]


def _accepted_numbers(claims: Iterable[Claim]) -> set[str]:
    """Every spelling a cited value licenses: itself, and the numbers within it."""
    accepted: set[str] = set()
    for claim in claims:
        rendered = render_value(claim)
        if rendered:
            accepted.add(_normalise(rendered))
            accepted.update(_normalise(m.group(0)) for m in _NUMBER.finditer(rendered))
    return accepted


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate(
    doc: Document,
    resolver: Resolver,
    guidance: Mapping[str, Mapping] | None = None,
    *,
    identifiers: Sequence[str] = (),
) -> list[Violation]:
    """Every rule, in order; an empty list means the document may be shown.

    `guidance` supplies the alias exemptions and defaults to the registry on
    disk; a caller without the file (the browser sandbox) passes it in.
    `identifiers` are strings this document may spell out although they look
    like numbers — the county FIPS a brief is about. A caller that passes
    none gets no such exemption, which is the point: the exemption belongs to
    a document that names its subject, not to every five-digit number.
    """
    guidance = guidance if guidance is not None else load_guidance()
    aliases = guidance_aliases(guidance)
    claims = doc.claim_index()
    found = _claim_violations(doc, resolver)
    for index, sentence in enumerate(doc.sentences):
        found.extend(
            _sentence_violations(index, sentence, claims, aliases, identifiers)
        )
    return found


def _grounded(claim_id: str, claims: Mapping[str, Claim], seen: frozenset[str]) -> bool:
    """Whether a claim's `+` chain reaches a source outside the document.

    A `computed` claim is arithmetic on other claims, so somewhere under it
    there must be a card, a file or a document — otherwise it is a number
    derived from nothing, and two claims computed from each other are exactly
    that. Walking the graph is also what catches the cycle.
    """
    claim = claims.get(claim_id)
    if claim is None:
        return False
    if claim.source.kind != "computed":
        return True
    if claim_id in seen:
        return False
    seen = seen | {claim_id}
    return any(_grounded(part, claims, seen) for part in claim.source.ref.split("+"))


def _claim_violations(doc: Document, resolver: Resolver) -> list[Violation]:
    """Every claim resolves, and every value is the one the artefact holds.

    `computed` claims resolve inside the document: their inputs must be claims
    it carries, and the chain must bottom out in something outside it.
    """
    claims = doc.claim_index()
    found: list[Violation] = []
    for claim in doc.claims:
        where = f"claim {claim.id} ({claim.source.kind} {claim.source.ref!r})"
        if claim.source.kind == "computed":
            parts = claim.source.ref.split("+")
            missing = [p for p in parts if p not in claims or p == claim.id]
            if missing:
                reason = f"computed from unknown claims {missing}"
            elif not _grounded(claim.id, claims, frozenset()):
                reason = (
                    "computed from claims that are themselves computed, with nothing "
                    "outside the document under them (a cycle, or a derivation with "
                    "no source)"
                )
            else:
                reason = None
            if reason:
                found.append(Violation(UNRESOLVED, None, f"{where}: {reason}"))
            continue
        outcome = resolver.resolve(claim.source)
        if isinstance(outcome, str):
            found.append(Violation(UNRESOLVED, None, f"{where}: {outcome}"))
        elif outcome is not None and claim.value is not None:
            if not _same_value(claim.value, outcome):
                found.append(Violation(
                    VALUE_MISMATCH, None,
                    f"{where}: the document says {claim.value!r}, the source holds "
                    f"{outcome!r}",
                ))
    return found


def _same_value(claimed: int | float | str, resolved: int | float) -> bool:
    """Numbers to a tolerance, everything else exactly.

    Floats because a JSON round trip and a recomputation need not agree in the
    last bit; a string or an int must match exactly, so a claim whose value is
    a word cannot pass against a number that happens to be there.
    """
    if isinstance(claimed, bool):
        return False
    if isinstance(claimed, (int, float)):
        return math.isclose(claimed, resolved, rel_tol=1e-9, abs_tol=1e-9)
    return claimed == resolved


def _sentence_violations(
    index: int,
    sentence: Sentence,
    claims: Mapping[str, Claim],
    aliases: Sequence[str],
    identifiers: Sequence[str] = (),
) -> list[Violation]:
    found: list[Violation] = []
    cited = sentence.cited()
    if not cited:
        found.append(Violation(UNCITED, index, f"no claim cited: {sentence.text!r}"))
    unknown = [c for c in cited if c not in claims]
    for claim_id in unknown:
        found.append(Violation(UNKNOWN_CLAIM, index, f"claim {claim_id!r} is not in the document"))
    known = [claims[c] for c in cited if c in claims]
    accepted = _accepted_numbers(known)
    for token in numbers_in(sentence.text, aliases, identifiers):
        if token not in accepted:
            found.append(Violation(
                NUMBER_WITHOUT_CLAIM, index,
                f"{token!r} is not the rendered value of a cited claim "
                f"(cited: {sorted(accepted)})",
            ))
    found.extend(_phrase_violations(index, sentence, known))
    return found


def _phrase_violations(
    index: int, sentence: Sentence, cited: Sequence[Claim]
) -> list[Violation]:
    """No warning language, except the fixed disclaimer with its citation."""
    prose = " ".join(strip_markers(sentence.text).split())
    if prose.rstrip(".") == NOT_A_WARNING_SENTENCE:
        cites_nws = any(
            c.source.kind == "guidance" and c.source.ref == NOT_A_WARNING_GUIDANCE
            for c in cited
        )
        if cites_nws:
            return []
        return [Violation(
            FORBIDDEN_PHRASE, index,
            f"the disclaimer must cite guidance {NOT_A_WARNING_GUIDANCE!r}",
        )]
    return [
        Violation(FORBIDDEN_PHRASE, index, f"{m.group(0)!r} in {prose!r}")
        for m in _FORBIDDEN.finditer(prose)
    ]


# --------------------------------------------------------------------------- #
# Rendering and serialisation
# --------------------------------------------------------------------------- #

_CSS = """
body{font:16px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;
color:#1b1b1b;background:#fff}
h1{font-size:1.5rem}
p.sentence{margin:0 0 .8rem}
sup a{text-decoration:none;color:#1a4d8f;padding:0 .1em}
ol.sources{padding-left:1.4rem}
ol.sources li{margin:0 0 .5rem}
.value{font-variant-numeric:tabular-nums;font-weight:600}
.source{color:#555;font-size:.9rem}
footer{margin-top:2rem;color:#555;font-size:.85rem;border-top:1px solid #ddd;
padding-top:.6rem}
""".strip()


def _sentence_html(sentence: Sentence) -> str:
    """Escaped prose with each marker turned into a superscript link."""
    parts: list[str] = []
    last = 0
    for m in _MARKER.finditer(sentence.text):
        parts.append(html.escape(sentence.text[last:m.start()]))
        cid = html.escape(m.group(1))
        parts.append(f'<sup><a href="#claim-{cid}">[{cid}]</a></sup>')
        last = m.end()
    parts.append(html.escape(sentence.text[last:]))
    return "".join(parts)


def _claim_html(claim: Claim) -> str:
    value = html.escape(render_value(claim))
    value_html = f' <span class="value">{value}</span>' if value else ""
    return (
        f'<li id="claim-{html.escape(claim.id)}"><strong>{html.escape(claim.id)}</strong>: '
        f"{html.escape(claim.text)}{value_html} "
        f'<span class="source">({html.escape(claim.source.kind)} '
        f"{html.escape(claim.source.ref)})</span></li>"
    )


def render_html(doc: Document) -> str:
    """A self-contained page: prose with superscript markers, then the sources.

    Rendering is pure; the caller runs `validate` first and writes nothing
    when it reports a violation. Every claim is listed, cited or not, so the
    reader sees everything the document leaned on.
    """
    sentences = "\n".join(
        f'<p class="sentence" id="s{i}">{_sentence_html(s)}</p>'
        for i, s in enumerate(doc.sentences)
    )
    claims = "\n".join(_claim_html(c) for c in doc.claims)
    inputs = ", ".join(
        f"{html.escape(k)}: {html.escape(v)}" for k, v in sorted(doc.inputs.items())
    )
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(doc.title)}</title><style>{_CSS}</style></head>\n<body>\n"
        f"<h1>{html.escape(doc.title)}</h1>\n"
        f'<p class="source">{html.escape(doc.kind)}</p>\n'
        f"{sentences}\n<h2>Sources</h2>\n<ol class=\"sources\">\n{claims}\n</ol>\n"
        f"<footer>Generated {html.escape(doc.generated_at)}"
        f"{' from ' + inputs if inputs else ''}</footer>\n</body></html>\n"
    )


def render_text(doc: Document) -> str:
    """The same document as plain text, markers kept so the trace survives."""
    lines = [doc.title, "=" * len(doc.title), f"({doc.kind})", ""]
    lines.extend(s.text for s in doc.sentences)
    lines += ["", "Sources", "-------"]
    for claim in doc.claims:
        value = render_value(claim)
        shown = f" = {value}" if value else ""
        lines.append(f"[{claim.id}] {claim.text}{shown} ({claim.source.kind} {claim.source.ref})")
    lines += ["", f"Generated {doc.generated_at}"]
    lines.extend(f"  {k}: {v}" for k, v in sorted(doc.inputs.items()))
    return "\n".join(lines) + "\n"


def to_dict(doc: Document) -> dict:
    return {
        "title": doc.title,
        "kind": doc.kind,
        "generated_at": doc.generated_at,
        "inputs": dict(doc.inputs),
        "sentences": [{"text": s.text, "claim_ids": list(s.claim_ids)} for s in doc.sentences],
        "claims": [
            {
                "id": c.id, "text": c.text, "value": c.value, "fmt": c.fmt,
                "source": {"kind": c.source.kind, "ref": c.source.ref},
            }
            for c in doc.claims
        ],
    }


def from_dict(raw: Mapping) -> Document:
    return Document(
        title=raw["title"],
        kind=raw["kind"],
        sentences=tuple(Sentence(s["text"], tuple(s.get("claim_ids", ()))) for s in raw["sentences"]),
        claims=tuple(
            Claim(
                c["id"], c["text"], c["value"],
                Source(c["source"]["kind"], c["source"]["ref"]), c.get("fmt", ""),
            )
            for c in raw["claims"]
        ),
        generated_at=raw["generated_at"],
        inputs=dict(raw.get("inputs", {})),
    )


def to_json(doc: Document) -> str:
    """Stable bytes: sorted keys, so two renders of one document are one file."""
    return json.dumps(to_dict(doc), indent=2, sort_keys=True) + "\n"


def from_json(text: str) -> Document:
    return from_dict(json.loads(text))
