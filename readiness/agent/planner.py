"""The optional `claude` drafter: prose the model rewrites, rules it must pass.

Report §7 is the whole design of this module in one line: "Numbers come from
the harness; prose comes from the model — and prose can hallucinate." So the
model here is never asked for a number and never asked for a claim. It is
handed sentences that already carry their citation markers and asked to say
the same thing better, keeping every `[c:ID]` exactly where it is.

What comes back is treated as untrusted text:

1. a returned sentence must carry **exactly** the markers the original
   carried — no fewer, so it cannot quietly shed a citation, and no more, so
   it cannot re-attribute itself to another claim in the document;
2. it may hold no URL, and no digit run that is not already in the original
   sentence or in the rendered value of a claim that sentence cites. The digit
   scan runs with `readiness.cite`'s identifier exemptions **off**: to a
   validator reading a document, a five-digit number is a county FIPS and
   `2031` is a year, and both are exactly what a model should not be able to
   introduce;
3. the sentence is then run through `readiness.cite.validate` against that
   claim set, so every number in it must be the rendered value of a claim it
   cites and no forbidden phrasing may appear;
4. anything that fails any of these is **refused**, and the refusal is a
   refusal to reword: the original local sentence stays exactly where it was.
   Nothing can be deleted, so a model cannot empty a report of its findings by
   returning rubbish — the worst it can do is leave the local prose alone, and
   the provenance line says how often it did.

The rewriter is never shown a name. Rules refer to a partner, a county or a
document by a label, so there is no name in the body for a model to reword,
shorten, re-case or leak — the property `tests/test_draft.py` asserts directly.

The SDK is imported lazily inside a `try:`, exactly as
`readiness.agent.orchestrator.run_claude` does, so the package imports and the
suite runs without it. Tests inject a fake model function and never touch the
SDK; nothing in `readiness/plans` imports this module except through the
deliberate `--drafter claude` path.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Sequence

from readiness import cite

#: A model backend: given a prompt, return the model's text. Injected in tests.
Model = Callable[[str], str]

#: The maximum number of sentences sent in one prompt. A gap report is small;
#: this exists so a pathological scenario cannot build an unbounded request.
MAX_SENTENCES = 200

SYSTEM_PROMPT = """\
You are rewriting the prose of a facility gap report for a practising \
emergency manager. Every sentence you are given carries citation markers of \
the form [c:ID]. Rules, all of them hard:

* Keep every [c:ID] marker exactly as it appears, attached to the same clause.
* Never introduce a marker that is not already in the sentence you are rewriting,
  and never drop one: the set of markers must come back exactly as it went out.
* Never introduce a number, a percentage, a date, a postcode or a URL that is
  not already there.
* Never write "warning", "alert", "will occur" or any prediction of a specific
  event: this is decision support, not a warning channel.
* Return one rewritten sentence per input sentence, in order, as a JSON array
  of strings and nothing else.

A sentence that breaks any of these is refused, not corrected, and the \
original wording is kept in its place."""


def build_prompt(sentences: Sequence[cite.Sentence]) -> str:
    """The request: the sentences as JSON, and the rules they must come back under."""
    payload = json.dumps([s.text for s in sentences], indent=2)
    return f"{SYSTEM_PROMPT}\n\nRewrite these sentences:\n{payload}\n"


def parse_response(text: str, expected: int) -> list[str]:
    """The model's array of sentences, or an empty list if it did not send one.

    A malformed response drops every sentence rather than half of them: a
    partially parsed array would silently reorder the report.
    """
    try:
        parsed = json.loads(text)
    except ValueError:
        return []
    if not isinstance(parsed, list) or len(parsed) != expected:
        return []
    return [s if isinstance(s, str) else "" for s in parsed]


#: A bare URL, in any of the shapes a model writes one. A gap report cites
#: guidance by registered id; a link is something a reader may follow out of
#: a validated document into somewhere nobody checked.
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)

#: A positional label a rule writes instead of a name: `PARTNER-2`,
#: `COUNTY-A`, `DOCUMENT-1`, `FACILITY-<hex>`. The ones the original sentence
#: already carried are names, not quantities, so `cite.validate` is told so.
_LABEL = re.compile(r"(?<!\w)[A-Z]{3,}-[0-9A-Za-z]+(?!\w)")

def _digit_runs(text: str) -> set[str]:
    """Every numeric token, with every identifier exemption turned off.

    The same scan `cite` runs, spelled for the opposite purpose. When a
    *document* is validated the exemptions are right: a five-digit number the
    document names is a county, a four-digit one is a year. When **model
    output** is checked they are precisely the hole — a ZIP code reads as a
    FIPS and an invented date reads as a year — so here they are off, and the
    question is only whether the original sentence or a value it cites
    already held that number.
    """
    return set(cite.numbers_in(text, exempt_identifiers=False))


def accept(
    candidate: str,
    original: cite.Sentence,
    claims: Sequence[cite.Claim],
    guidance=None,
) -> cite.Sentence | None:
    """One rewritten sentence, or None when it must be refused.

    The candidate must cite exactly what the original cited — a rewrite is a
    rewording of one sentence, not a re-attribution of it, and a candidate
    that keeps its own marker and splices in a second is re-attribution with
    extra steps. It may introduce no URL and no digit that was not already in
    the original or in a value it cites.
    """
    text = " ".join(candidate.split())
    if not text:
        return None
    markers = cite.markers_in(text)
    allowed = set(original.cited())
    if not markers or set(markers) != allowed:
        return None
    if _URL.search(text):
        return None
    index = {claim.id: claim for claim in claims}
    known = _digit_runs(original.text)
    for claim_id in allowed:
        claim = index.get(claim_id)
        if claim is not None:
            known |= _digit_runs(cite.render_value(claim))
    if any(
        run not in known
        for run in cite.numbers_in(text, exempt_identifiers=False)
    ):
        return None
    sentence = cite.Sentence.from_text(text)
    probe = cite.Document(
        title="candidate", kind="gap-report-candidate",
        sentences=(sentence,), claims=tuple(claims),
        generated_at="", inputs={},
    )
    resolver = _PermissiveResolver()
    labels = tuple(dict.fromkeys(_LABEL.findall(original.text)))
    if cite.validate(probe, resolver, guidance, identifiers=labels):
        return None
    return sentence


class _PermissiveResolver:
    """Resolves everything: the claims were already resolved for the local report.

    This probe is about the *sentence*, not about the sources — whether every
    number is a cited value, whether the citation exists in the claim set and
    whether the phrasing is allowed. The document the sentence goes into is
    validated again, for real, against the gap report's own resolver.
    """

    def resolve(self, source: cite.Source) -> str | None:  # noqa: D102 - protocol
        return None


def rewrite(
    sentences: Sequence[cite.Sentence],
    claims: Sequence[cite.Claim],
    *,
    model: Model | None = None,
    guidance=None,
) -> tuple[list[cite.Sentence], int]:
    """Reword the body of a gap report; return every sentence and the refusals.

    The list that comes back is always the list that went in, sentence for
    sentence: a candidate that fails a check is refused and the local
    sentence stays. A model therefore cannot delete a finding — not the
    `failed` ones, not the fail-closed `cannot_run` statement — and the
    refusal count on the provenance line is a statement about wording, not
    about what the report contains.

    With no `model`, the Claude Agent SDK is imported lazily and used. A
    response that cannot be parsed, or a model that raises, leaves the local
    prose exactly as it was and refuses nothing — the report is still the
    report, it simply was not improved.
    """
    body = list(sentences)
    if not body:
        return [], 0
    if len(body) > MAX_SENTENCES:
        raise ValueError(
            f"refusing to rewrite {len(body)} sentences; the limit is {MAX_SENTENCES}"
        )
    ask = model if model is not None else _sdk_model()
    try:
        answer = ask(build_prompt(body))
    except Exception:  # noqa: BLE001 - a backend that fails must not lose the report
        return body, 0
    candidates = parse_response(answer, len(body))
    if not candidates:
        return body, 0
    kept: list[cite.Sentence] = []
    refused = 0
    for candidate, original in zip(candidates, body):
        accepted = accept(candidate, original, claims, guidance)
        if accepted is None:
            refused += 1
            kept.append(original)
            continue
        kept.append(accepted)
    return kept, refused


def rewriter(*, model: Model | None = None, guidance=None):
    """A `gap_report.Rewriter` bound to a backend, for `--drafter claude`."""

    def _rewrite(
        sentences: Sequence[cite.Sentence], claims: Sequence[cite.Claim]
    ) -> tuple[list[cite.Sentence], int]:
        return rewrite(sentences, claims, model=model, guidance=guidance)

    return _rewrite


def _sdk_model() -> Model:
    """The Claude Agent SDK as a one-shot text backend, imported only when used."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions, query  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "the claude drafter needs the Claude Agent SDK:\n"
            "    pip install claude-agent-sdk\n"
            "and an ANTHROPIC_API_KEY in the environment.\n"
            "The gap report's own prose is written by the deterministic local "
            "drafter, which needs neither:\n"
            "    readiness gap-report --facility PATH --period YYYY-Qn "
            "--drafter local\n"
            f"({exc})"
        ) from exc

    import asyncio

    def ask(prompt: str) -> str:  # pragma: no cover - needs the SDK and a key
        options = ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT, max_turns=1, allowed_tools=[]
        )

        async def _go() -> str:
            parts: list[str] = []
            async for message in query(prompt=prompt, options=options):
                parts.append(str(message))
            return "\n".join(parts)

        return asyncio.run(_go())

    return ask


__all__ = [
    "MAX_SENTENCES",
    "SYSTEM_PROMPT",
    "Model",
    "accept",
    "build_prompt",
    "parse_response",
    "rewrite",
    "rewriter",
]
